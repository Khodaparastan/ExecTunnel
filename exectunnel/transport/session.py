"""TunnelSession — core session orchestration, bootstrap, and SOCKS5 routing.

Bootstraps ``agent.py`` into the pod and runs a local SOCKS5 proxy that routes
all connections through the WebSocket tunnel.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import random
from collections import defaultdict

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from exectunnel.config.defaults import (
    BOOTSTRAP_DECODE_DELAY_SECS,
    BOOTSTRAP_DIAG_MAX_LINES,
    BOOTSTRAP_RM_DELAY_SECS,
    BOOTSTRAP_STTY_DELAY_SECS,
    CONNECT_FAILURE_WARN_EVERY,
    CONNECT_PACE_JITTER_CAP_SECS,
    SEND_DROP_LOG_EVERY,
    UDP_DIRECT_RECV_TIMEOUT_SECS,
    UDP_PUMP_POLL_TIMEOUT_SECS,
    WS_CLOSE_CODE_UNHEALTHY,
)
from exectunnel.config.settings import AppConfig, TunnelConfig
from exectunnel.exceptions import (
    AgentReadyTimeoutError,
    AgentSyntaxError,
    AgentVersionMismatchError,
    BootstrapError,
    ConnectionClosedError,
    ExecTunnelError,
    FrameDecodingError,
    ProtocolError,
    ReconnectExhaustedError,
    TransportError,
    WebSocketSendTimeoutError,
)
from exectunnel.helpers import (
    is_host_excluded,
    load_agent_b64,
    make_udp_socket,
)
from exectunnel.observability import metrics_inc, metrics_observe, span
from exectunnel.protocol.enums import Reply
from exectunnel.protocol.frames import (
    BOOTSTRAP_CHUNK_SIZE_CHARS as BOOTSTRAP_CHUNK_SIZE,
)
from exectunnel.protocol.frames import (
    FRAME_PREFIX,
    FRAME_SUFFIX,
    READY_FRAME,
    encode_conn_open_frame,
    parse_frame,
)
from exectunnel.protocol.frames import (
    PIPE_READ_CHUNK_BYTES as PIPE_CHUNK_SIZE,
)
from exectunnel.protocol.ids import new_conn_id, new_flow_id
from exectunnel.proxy.relay import UdpRelay
from exectunnel.proxy.request import Socks5Request
from exectunnel.proxy.server import Socks5Server
from exectunnel.transport.connection import _TcpConnectionHandler
from exectunnel.transport.dns_forwarder import _DnsForwarder
from exectunnel.transport.models import PendingConnectState
from exectunnel.transport.udp_flow import _UdpFlowHandler

logger = logging.getLogger("exectunnel.transport.session")


# ── Tunnel session ────────────────────────────────────────────────────────────


class TunnelSession:
    """
    Bootstraps ``agent.py`` (no-arg tunnel mode) into the pod and runs a
    local SOCKS5 proxy that routes connections through the WebSocket tunnel.

    Raises
    ------
    AgentReadyTimeoutError
        If the agent does not emit ``AGENT_READY`` within ``ready_timeout``.
    AgentSyntaxError
        If the agent script fails to parse on the remote end.
    AgentVersionMismatchError
        If the remote agent reports an incompatible version.
    BootstrapError
        For any other bootstrap failure.
    ReconnectExhaustedError
        If all reconnect attempts are exhausted after a healthy session ends.
    """

    def __init__(self, app_cfg: AppConfig, tun_cfg: TunnelConfig) -> None:
        self._app = app_cfg
        self._tun = tun_cfg
        self._ws: ClientConnection | None = None
        self._conn_handlers: dict[str, _TcpConnectionHandler] = {}
        self._pending_connects: dict[str, PendingConnectState] = {}
        self._udp_registry: dict[str, _UdpFlowHandler] = {}
        # Set when the WebSocket closes (signals waiters to abort).
        self._ws_closed: asyncio.Event = asyncio.Event()
        # Control frames and data frames use separate queues so control
        # messages are never dropped under data-plane pressure.
        # Each queue item is a (frame_str, is_data) tuple; None is the poison pill.
        self._send_ctrl_queue: asyncio.Queue[tuple[str, bool] | None] | None = None
        self._send_data_queue: asyncio.Queue[tuple[str, bool] | None] | None = None
        self._request_tasks: set[asyncio.Task[None]] = set()
        # Internal queue used to hand frames from _bootstrap to _recv_loop.
        self._pre_ready_buf: list[str] = []
        # Partial (unterminated) line left in _wait_ready's buffer after
        # AGENT_READY was found mid-message.  _recv_loop prepends this to its
        # own buf so the fragment is completed by the next WebSocket message.
        self._pre_ready_carry: str = ""
        self._bootstrap_diag: list[str] = []
        self._send_drop_count = 0
        self._ack_timeout_count = 0
        self._ack_timeout_suppressed = 0
        self._ack_timeout_window_start: float | None = None
        self._ack_timeout_window_count = 0
        self._ack_reconnect_requested = False
        self._ack_timeout_warn_every = tun_cfg.ack_timeout_warn_every
        self._ack_timeout_window_secs = tun_cfg.ack_timeout_window_secs
        self._ack_timeout_reconnect_threshold = tun_cfg.ack_timeout_reconnect_threshold
        self._connect_max_pending = tun_cfg.connect_max_pending
        self._connect_max_pending_per_host = tun_cfg.connect_max_pending_per_host
        self._connect_max_pending_cf = tun_cfg.connect_max_pending_cf
        self._connect_pace_cf_ms = tun_cfg.connect_pace_cf_ms
        self._pre_ack_buffer_cap_bytes = tun_cfg.pre_ack_buffer_cap_bytes
        self._connect_gate = asyncio.Semaphore(self._connect_max_pending)
        self._host_connect_gates: dict[str, asyncio.Semaphore] = {}
        self._host_connect_open_locks: dict[str, asyncio.Lock] = {}
        self._host_connect_last_open_at: dict[str, float] = {}
        self._connect_failures_by_host: defaultdict[str, int] = defaultdict(int)
        logger.info(
            "connect hardening: global=%d per_host=%d cf_host=%d cf_pace_ms=%d",
            self._connect_max_pending,
            self._connect_max_pending_per_host,
            self._connect_max_pending_cf,
            self._connect_pace_cf_ms,
        )

    # ── Top-level run ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect, bootstrap the agent, and serve.

        Retries on transport/session interruptions using ``BridgeConfig``
        reconnect settings.  Bootstrap failures remain fatal and are never
        retried.

        Raises
        ------
        BootstrapError
            (and subclasses) — propagated immediately; never retried.
        ReconnectExhaustedError
            When all reconnect attempts have been consumed.
        """
        ssl_ctx = self._app.ssl_context()
        retries = self._app.bridge.reconnect_max_retries
        base_delay = self._app.bridge.reconnect_base_delay
        max_delay = self._app.bridge.reconnect_max_delay
        attempt = 0

        with span("tunnel.session"):
            while True:
                reconnect_reason: str | None = None
                bootstrapped = False
                try:
                    metrics_inc("tunnel.connect.attempt")
                    async with connect(
                        self._app.wss_url,
                        ssl=ssl_ctx,
                        # Disable the websockets built-in ping: its background
                        # task competes for the internal write lock with
                        # ws.send() in _send_loop.  Under sustained data load
                        # the ping can't acquire the lock within ping_timeout
                        # and websockets closes the connection from the inside.
                        # We implement our own keepalive via _keepalive_loop
                        # which sends a control frame through _send_ctrl_queue
                        # so it is serialised safely by _send_loop.
                        ping_interval=None,
                        max_size=None,
                    ) as ws:
                        await self._run_session(ws)
                        bootstrapped = True
                        reconnect_reason = "session ended"

                except BootstrapError:
                    # Bootstrap failures (AgentReadyTimeoutError, AgentSyntaxError,
                    # AgentVersionMismatchError, …) are always fatal — re-raise
                    # immediately without consuming a retry slot.
                    metrics_inc("tunnel.bootstrap.error")
                    raise

                except WebSocketSendTimeoutError as exc:
                    metrics_inc("tunnel.connect.error", error="ws_send_timeout")
                    reconnect_reason = f"ws_send_timeout: {exc.message} (error_id={exc.error_id})"
                    logger.warning(
                        "WebSocket send timed out [%s] (error_id=%s) — will reconnect",
                        exc.error_code,
                        exc.error_id,
                    )

                except ConnectionClosedError as exc:
                    metrics_inc("tunnel.connect.error", error="connection_closed")
                    reconnect_reason = (
                        f"connection_closed: {exc.details.get('close_reason', '')} "
                        f"(error_id={exc.error_id})"
                    )
                    logger.warning(
                        "WebSocket connection closed [%s] close_code=%s (error_id=%s) "
                        "— will reconnect",
                        exc.error_code,
                        exc.details.get("close_code"),
                        exc.error_id,
                    )

                except TransportError as exc:
                    metrics_inc(
                        "tunnel.connect.error",
                        error=exc.error_code.replace(".", "_"),
                    )
                    reconnect_reason = f"{exc.error_code}: {exc.message} (error_id={exc.error_id})"
                    logger.warning(
                        "Transport error [%s]: %s (error_id=%s) — will reconnect",
                        exc.error_code,
                        exc.message,
                        exc.error_id,
                    )

                except (OSError, ConnectionClosed, TimeoutError) as exc:
                    # Third-party / stdlib transport errors that have not yet
                    # been wrapped by our exception hierarchy.
                    metrics_inc("tunnel.connect.error", error=exc.__class__.__name__)
                    reconnect_reason = str(exc) or exc.__class__.__name__

                finally:
                    self._ws = None

                if reconnect_reason is None:
                    return

                # Reset the backoff counter after each healthy session so the
                # retry budget is per-consecutive-failure-run, not lifetime.
                if bootstrapped:
                    attempt = 0

                if attempt >= retries:
                    metrics_inc("tunnel.reconnect.exhausted")
                    raise ReconnectExhaustedError(
                        f"WebSocket session terminated after {retries} reconnect attempts.",
                        error_code="transport.reconnect_exhausted",
                        details={
                            "attempts": retries,
                            "last_error": reconnect_reason,
                        },
                        hint=(
                            "Check network connectivity to the tunnel endpoint and "
                            "increase EXECTUNNEL_RECONNECT_MAX_RETRIES if transient "
                            "disruptions are expected."
                        ),
                    )

                delay = min(base_delay * (2**attempt), max_delay)
                attempt += 1
                metrics_inc("tunnel.reconnect.scheduled")
                metrics_observe("tunnel.reconnect.delay_sec", delay)
                logger.warning(
                    "WebSocket disconnected (%s), reconnecting in %.1fs (attempt %d/%d)",
                    reconnect_reason,
                    delay,
                    attempt,
                    retries,
                )
                await asyncio.sleep(delay)

    async def _run_session(self, ws: ClientConnection) -> None:
        """Initialise per-session state, bootstrap the agent, and serve.

        Extracted from ``run()`` so integration tests can inject a
        pre-connected ``ClientConnection`` without going through the real
        ``connect()`` call.
        """
        metrics_inc("tunnel.connect.ok")
        self._ws = ws
        self._ws_closed.clear()
        self._send_ctrl_queue: asyncio.Queue[tuple[str, bool] | None] = asyncio.Queue()
        self._send_data_queue: asyncio.Queue[tuple[str, bool] | None] = asyncio.Queue(
            maxsize=self._app.bridge.send_queue_cap
        )
        self._bootstrap_diag.clear()
        self._pre_ready_buf.clear()
        self._pre_ready_carry = ""
        await self._bootstrap()
        # A successful bootstrap resets reconnect backoff state.
        self._ack_reconnect_requested = False
        self._ack_timeout_window_start = None
        self._ack_timeout_window_count = 0
        await self._serve()

    # ── Bootstrap ─────────────────────────────────────────────────────────────

    async def _bootstrap(self) -> None:
        """Upload and start the agent script on the remote end.

        The agent script is base64-encoded, sent in chunks via ``printf``,
        decoded on the remote, syntax-checked, then ``exec``'d (replacing the
        shell process so its stdio becomes the WebSocket channel).

        After ``exec``, we wait for ``AGENT_READY`` by reading raw WebSocket
        messages.  Any frames that arrive *after* ``AGENT_READY`` but *before*
        ``_recv_loop`` starts are buffered in ``_pre_ready_buf`` and replayed
        by ``_recv_loop`` on startup.

        Raises
        ------
        AgentReadyTimeoutError
            ``AGENT_READY`` not received within ``ready_timeout``.
        AgentSyntaxError
            Remote Python raised ``SyntaxError`` while loading the agent.
        AgentVersionMismatchError
            Remote agent reports an incompatible version string.
        BootstrapError
            WebSocket closed before ``AGENT_READY`` or any other startup failure.
        """
        assert self._ws is not None
        ws = self._ws
        start = asyncio.get_running_loop().time()
        metrics_inc("tunnel.bootstrap.started")

        async def send_cmd(cmd: str) -> None:
            try:
                await ws.send((cmd + "\n").encode())
            except ConnectionClosed as exc:
                raise BootstrapError(
                    "WebSocket closed while sending bootstrap command.",
                    error_code="bootstrap.cmd_send_failed",
                    details={"command": cmd[:80]},
                    hint="Check that the remote shell is still alive.",
                ) from exc

        # Suppress terminal echo so shell output doesn't pollute the channel.
        await send_cmd("stty raw -echo")
        await asyncio.sleep(BOOTSTRAP_STTY_DELAY_SECS)

        agent_b64 = load_agent_b64()

        # Clean up any leftover files from a previous run.
        await send_cmd("rm -f /tmp/exectunnel_agent.py /tmp/exectunnel_agent.b64")
        await asyncio.sleep(BOOTSTRAP_RM_DELAY_SECS)

        # Upload in chunks — safe under all POSIX shell input-buffer limits.
        for i in range(0, len(agent_b64), BOOTSTRAP_CHUNK_SIZE):
            chunk = agent_b64[i : i + BOOTSTRAP_CHUNK_SIZE]
            await send_cmd(f"printf '%s' {chunk} >> /tmp/exectunnel_agent.b64")

        await send_cmd("base64 -d /tmp/exectunnel_agent.b64 > /tmp/exectunnel_agent.py")
        await asyncio.sleep(BOOTSTRAP_DECODE_DELAY_SECS)

        # Syntax-check before exec so a decode corruption is caught cleanly.
        await send_cmd(
            "python3 -c '"
            'import ast,sys; ast.parse(open("/tmp/exectunnel_agent.py").read()); '
            'sys.stdout.write("SYNTAX_OK\\n"); sys.stdout.flush()\''
        )

        # exec replaces the shell — after this the WebSocket channel IS the agent.
        await send_cmd("exec python3 /tmp/exectunnel_agent.py")

        logger.info("waiting for agent AGENT_READY…")
        try:
            await asyncio.wait_for(
                self._wait_ready(ws), timeout=self._tun.ready_timeout
            )
        except TimeoutError:
            detail = (
                f"; last output: {self._bootstrap_diag[-1]}"
                if self._bootstrap_diag
                else ""
            )
            metrics_inc("tunnel.bootstrap.timeout")
            raise AgentReadyTimeoutError(
                f"Agent did not signal AGENT_READY within {self._tun.ready_timeout}s{detail}",
                error_code="bootstrap.agent_ready_timeout",
                details={
                    "timeout_s": self._tun.ready_timeout,
                    "last_output": self._bootstrap_diag[-1] if self._bootstrap_diag else None,
                },
                hint=(
                    "Increase EXECTUNNEL_AGENT_TIMEOUT or check the remote Python "
                    "version and available memory."
                ),
            )

        logger.info(
            "agent ready — SOCKS5 on %s:%d",
            self._tun.socks_host,
            self._tun.socks_port,
        )
        metrics_inc("tunnel.bootstrap.ok")
        metrics_observe(
            "tunnel.bootstrap.duration_sec",
            asyncio.get_running_loop().time() - start,
        )

    async def _wait_ready(self, ws: ClientConnection) -> None:
        """Read raw WebSocket messages until ``AGENT_READY`` is seen.

        Any frames arriving *after* ``AGENT_READY`` in the same read loop are
        buffered into ``_pre_ready_buf`` and will be replayed by ``_recv_loop``
        before it starts consuming the live WebSocket iterator.

        Raises
        ------
        AgentSyntaxError
            Remote Python reported a ``SyntaxError`` while loading the agent.
        AgentVersionMismatchError
            Remote agent reported an incompatible version string.
        BootstrapError
            WebSocket closed before ``AGENT_READY`` was received.
        """
        buf = ""
        ready = False
        async for msg in ws:
            chunk = msg.decode() if isinstance(msg, bytes) else msg
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                stripped = line.strip()
                if not ready:
                    if stripped == READY_FRAME:
                        ready = True
                        continue
                    if stripped:
                        self._bootstrap_diag.append(stripped)
                        if len(self._bootstrap_diag) > BOOTSTRAP_DIAG_MAX_LINES:
                            self._bootstrap_diag.pop(0)
                    if stripped.startswith(("SyntaxError:", "Traceback (most recent")):
                        raise AgentSyntaxError(
                            f"Agent script error: {stripped}",
                            error_code="bootstrap.agent_syntax_error",
                            details={
                                "text": stripped,
                                "filename": "/tmp/exectunnel_agent.py",
                            },
                            hint=(
                                "The uploaded agent.py failed to parse. "
                                "Check for base64 decode corruption or "
                                "an incompatible remote Python version."
                            ),
                        )
                    if stripped.startswith("VERSION_MISMATCH:"):
                        # Agent emits VERSION_MISMATCH:<remote_version> when it
                        # detects an incompatible client protocol version.
                        remote_version = stripped.split(":", 1)[-1].strip()
                        raise AgentVersionMismatchError(
                            f"Remote agent version {remote_version!r} is incompatible "
                            f"with this client.",
                            error_code="bootstrap.version_mismatch",
                            details={
                                "remote_version": remote_version,
                                "local_version": self._app.version,
                            },
                            hint=(
                                "Upgrade the exectunnel client or redeploy the agent "
                                "to match the client version."
                            ),
                        )
                else:
                    # Buffer post-READY complete lines for _recv_loop.
                    self._pre_ready_buf.append(stripped)
            if ready:
                # Any bytes remaining in buf are a partial (unterminated) frame.
                # Store them so _recv_loop can prepend them to its own buffer
                # and complete the frame when the next WebSocket message arrives.
                self._pre_ready_carry = buf
                return

        detail = f": {self._bootstrap_diag[-1]}" if self._bootstrap_diag else ""
        raise BootstrapError(
            f"WebSocket closed before AGENT_READY was received{detail}",
            error_code="bootstrap.ws_closed_before_ready",
            details={
                "last_output": self._bootstrap_diag[-1] if self._bootstrap_diag else None,
            },
            hint="Check that the remote shell executed the agent script successfully.",
        )

    # ── Serve ─────────────────────────────────────────────────────────────────

    async def _serve(self) -> None:
        metrics_inc("tunnel.serve.started")
        socks = Socks5Server(self._tun.socks_host, self._tun.socks_port)
        await socks.start()

        dns_fwd: _DnsForwarder | None = None
        if self._tun.dns_upstream:
            dns_fwd = _DnsForwarder(
                self._tun.dns_local_port,
                self._tun.dns_upstream,
                self._ws_send,
                self._udp_registry,
                max_inflight=self._app.bridge.send_queue_cap,
            )
            await dns_fwd.start()

        recv_task = asyncio.create_task(self._recv_loop(), name="tun-recv-loop")
        send_task = asyncio.create_task(self._send_loop(), name="tun-send-loop")
        socks_task = asyncio.create_task(self._socks_loop(socks), name="tun-socks-loop")
        keepalive_task = asyncio.create_task(self._keepalive_loop(), name="tun-keepalive")

        try:
            done, _ = await asyncio.wait(
                {recv_task, send_task, socks_task, keepalive_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is None:
                    continue
                metrics_inc("tunnel.tasks.error", task=task.get_name())
                # Emit structured fields when the exception is a library error.
                if isinstance(exc, ExecTunnelError):
                    logger.error(
                        "task %s failed [%s]: %s (error_id=%s)",
                        task.get_name(),
                        exc.error_code,
                        exc.message,
                        exc.error_id,
                    )
                else:
                    logger.error("task %s failed: %s", task.get_name(), exc)
                logger.debug(
                    "task %s traceback",
                    task.get_name(),
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
        finally:
            for task in (recv_task, send_task, socks_task, keepalive_task):
                if not task.done():
                    task.cancel()
            # Send poison pills so _send_loop can unblock from queue.get().
            if self._send_ctrl_queue is not None:
                self._send_ctrl_queue.put_nowait(None)
            if self._send_data_queue is not None:
                with contextlib.suppress(asyncio.QueueFull):
                    self._send_data_queue.put_nowait(None)
            await asyncio.gather(
                recv_task, send_task, socks_task, keepalive_task,
                return_exceptions=True,
            )
            for task in list(self._request_tasks):
                task.cancel()
            if self._request_tasks:
                await asyncio.gather(*self._request_tasks, return_exceptions=True)

            await socks.stop()
            if dns_fwd:
                dns_fwd.stop()
            metrics_inc("tunnel.serve.stopped")

    async def _socks_loop(self, socks: Socks5Server) -> None:
        async for req in socks:
            task = asyncio.create_task(
                self._handle_request(req),
                name=f"req-{req.cmd.name}-{req.host}:{req.port}",
            )
            self._request_tasks.add(task)
            task.add_done_callback(self._on_request_complete)

    def _on_request_complete(self, task: asyncio.Task[None]) -> None:
        self._request_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        metrics_inc("tunnel.request.error", error=exc.__class__.__name__)
        if isinstance(exc, ExecTunnelError):
            logger.error(
                "request task %s failed [%s]: %s (error_id=%s)",
                task.get_name(),
                exc.error_code,
                exc.message,
                exc.error_id,
            )
        else:
            logger.error("request task %s failed: %s", task.get_name(), exc)
        logger.debug(
            "request task %s traceback",
            task.get_name(),
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    # ── Request handlers ──────────────────────────────────────────────────────

    async def _handle_request(self, req: Socks5Request) -> None:
        if req.is_connect:
            await self._handle_connect(req)
        elif req.is_udp:
            await self._handle_udp_associate(req)
        else:
            await req.send_reply_error(Reply.CMD_NOT_SUPPORTED)

    def _track_ack_failure(self, conn_id: str, reason: str) -> None:
        self._ack_timeout_count += 1
        metrics_inc("tunnel.conn_ack.failed", reason=reason)
        if reason == "timeout":
            now = asyncio.get_running_loop().time()
            if (
                self._ack_timeout_window_start is None
                or now - self._ack_timeout_window_start > self._ack_timeout_window_secs
            ):
                self._ack_timeout_window_start = now
                self._ack_timeout_window_count = 0
            self._ack_timeout_window_count += 1

        should_log = (
            self._ack_timeout_count == 1
            or self._ack_timeout_count % self._ack_timeout_warn_every == 0
            or reason != "timeout"
        )
        if not should_log:
            self._ack_timeout_suppressed += 1
            return

        suppressed = self._ack_timeout_suppressed
        self._ack_timeout_suppressed = 0
        extra = f", suppressed={suppressed}" if suppressed else ""
        logger.warning(
            "agent ACK failure conn=%s reason=%s total=%d pending=%d%s",
            conn_id,
            reason,
            self._ack_timeout_count,
            len(self._pending_connects),
            extra,
        )

        if reason != "timeout":
            return
        if self._ack_reconnect_requested:
            return
        if self._ack_timeout_window_count < self._ack_timeout_reconnect_threshold:
            return

        self._ack_reconnect_requested = True
        metrics_inc("tunnel.conn_ack.reconnect_triggered")
        logger.error(
            "agent appears unhealthy: %d ACK timeouts within %.0fs; forcing reconnect",
            self._ack_timeout_window_count,
            self._ack_timeout_window_secs,
        )
        ws = self._ws
        if ws is not None:
            task = asyncio.create_task(
                ws.close(code=WS_CLOSE_CODE_UNHEALTHY, reason="conn ack timeout surge")
            )
            self._request_tasks.add(task)
            task.add_done_callback(self._request_tasks.discard)

    @staticmethod
    def _connect_host_key(host: str) -> str:
        return host.lower().strip()

    def _connect_host_limit(self, host_key: str) -> int:
        if host_key == "challenges.cloudflare.com":
            return min(self._connect_max_pending_per_host, self._connect_max_pending_cf)
        return self._connect_max_pending_per_host

    def _connect_pace_interval_for_host(self, host_key: str) -> float:
        if host_key == "challenges.cloudflare.com":
            return self._connect_pace_cf_ms / 1000.0
        return 0.0

    def _connect_host_gate(self, host: str) -> asyncio.Semaphore:
        key = self._connect_host_key(host)
        gate = self._host_connect_gates.get(key)
        if gate is None:
            gate = asyncio.Semaphore(self._connect_host_limit(key))
            self._host_connect_gates[key] = gate
        return gate

    async def _apply_connect_pacing(self, host_key: str) -> None:
        interval = self._connect_pace_interval_for_host(host_key)
        if interval <= 0:
            return
        lock = self._host_connect_open_locks.get(host_key)
        if lock is None:
            lock = asyncio.Lock()
            self._host_connect_open_locks[host_key] = lock
        async with lock:
            now = asyncio.get_running_loop().time()
            last = self._host_connect_last_open_at.get(host_key)
            if last is not None:
                wait_for = interval - (now - last)
                if wait_for > 0:
                    await asyncio.sleep(
                        wait_for
                        + random.uniform(
                            0.0, min(CONNECT_PACE_JITTER_CAP_SECS, interval / 2.0)
                        )
                    )
            self._host_connect_last_open_at[host_key] = (
                asyncio.get_running_loop().time()
            )

    def _report_pending_connects_metric(self) -> None:
        metrics_observe("pending_connects", float(len(self._pending_connects)))

    def _record_connect_failure(self, host: str, reason: str, reply: Reply) -> None:
        host_key = self._connect_host_key(host)
        self._connect_failures_by_host[host_key] += 1
        total = self._connect_failures_by_host[host_key]
        metrics_inc("connect_fail_by_host", host=host_key, reason=reason)
        if host_key == "challenges.cloudflare.com" and (
            total == 1 or total % CONNECT_FAILURE_WARN_EVERY == 0
        ):
            logger.warning(
                "connect failures for %s: count=%d reason=%s pending=%d",
                host_key,
                total,
                reason,
                len(self._pending_connects),
            )
        metrics_inc("socks_reply_code", code=int(reply))

    async def _handle_connect(self, req: Socks5Request) -> None:
        host, port = req.host, req.port

        if is_host_excluded(host, self._tun.exclude):
            logger.debug("direct connect %s:%d (excluded)", host, port)
            try:
                rem_reader, rem_writer = await asyncio.open_connection(host, port)
            except OSError as exc:
                logger.debug("direct connect failed: %s", exc)
                metrics_inc("socks_reply_code", code=int(Reply.HOST_UNREACHABLE))
                await req.send_reply_error(Reply.HOST_UNREACHABLE)
                return
            metrics_inc("socks_reply_code", code=int(Reply.SUCCESS))
            await req.send_reply_success()
            await self._pipe(req.reader, req.writer, rem_reader, rem_writer)
            return

        # ── Route through tunnel ──────────────────────────────────────────────
        host_gate = self._connect_host_gate(host)
        conn_id = new_conn_id()
        loop = asyncio.get_running_loop()
        ack_future: asyncio.Future[str] = loop.create_future()
        handler = _TcpConnectionHandler(
            conn_id,
            req.reader,
            req.writer,
            self._ws_send,
            self._conn_handlers,
            pre_ack_buffer_cap_bytes=self._pre_ack_buffer_cap_bytes,
        )
        pending = PendingConnectState(host=host, ack_future=ack_future)
        self._conn_handlers[conn_id] = handler
        self._pending_connects[conn_id] = pending
        self._report_pending_connects_metric()

        ack_wait_start = loop.time()
        ack_status = "ok"
        reply = Reply.HOST_UNREACHABLE
        failure_reason: str | None = None

        try:
            async with self._connect_gate, host_gate:
                host_key = self._connect_host_key(host)
                await self._apply_connect_pacing(host_key)

                try:
                    await self._ws_send(
                        encode_conn_open_frame(conn_id, host, port), control=True
                    )
                except WebSocketSendTimeoutError as exc:
                    # Tunnel stalled before we could even open the connection.
                    ack_status = "ws_send_timeout"
                    failure_reason = "ws_send_timeout"
                    metrics_inc("tunnel.conn_open.error", error="ws_send_timeout")
                    logger.warning(
                        "conn %s: CONN_OPEN send timed out [%s] (error_id=%s)",
                        conn_id,
                        exc.error_code,
                        exc.error_id,
                        extra={"conn_id": conn_id, "error_id": exc.error_id},
                    )
                except ConnectionClosedError as exc:
                    ack_status = "ws_closed"
                    failure_reason = "ws_closed"
                    metrics_inc("tunnel.conn_open.error", error="connection_closed")
                    logger.warning(
                        "conn %s: CONN_OPEN failed — connection closed [%s] (error_id=%s)",
                        conn_id,
                        exc.error_code,
                        exc.error_id,
                        extra={"conn_id": conn_id, "error_id": exc.error_id},
                    )

                if failure_reason is None:
                    logger.debug(
                        "tunnel CONNECT %s:%d conn=%s",
                        host,
                        port,
                        conn_id,
                        extra={"conn_id": conn_id, "host": host, "port": port},
                    )

                    # Wait for agent ACK; abort immediately if the WS closes.
                    ws_future = asyncio.create_task(self._ws_closed.wait())
                    ack_task: asyncio.Task[str] = asyncio.ensure_future(ack_future)
                    done, pending_wait = await asyncio.wait(
                        {ack_task, ws_future},
                        timeout=self._tun.conn_ack_timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for fut in pending_wait:
                        fut.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await fut

                    if ws_future in done:
                        ack_status = "ws_closed"
                        failure_reason = "ws_closed"
                    elif ack_task in done:
                        ack_result = ack_future.result()
                        if ack_result != "ack":
                            ack_status = ack_result
                            failure_reason = ack_result
                    else:
                        ack_status = "timeout"
                        failure_reason = "timeout"
                        metrics_inc(
                            "connect_ack_timeout",
                            host=self._connect_host_key(host),
                        )
            # Semaphores released here — before starting data flow.

            if failure_reason is not None:
                self._track_ack_failure(conn_id, failure_reason)
                self._record_connect_failure(host, failure_reason, reply)
                # Pop before close_remote() so that any racing DATA /
                # CONN_CLOSED_ACK frames cannot re-enter a handler whose
                # start() was never called.
                self._conn_handlers.pop(conn_id, None)
                handler.close_remote()
                with contextlib.suppress(OSError):
                    handler._writer.close()
                    await handler._writer.wait_closed()
                await req.send_reply_error(reply)
                return

            metrics_inc("tunnel.conn_ack.ok")
            metrics_inc("socks_reply_code", code=int(Reply.SUCCESS))
            await req.send_reply_success()
            handler.start()

        except ExecTunnelError as exc:
            # Structured library error during ACK wait — log with full context.
            ack_status = "error"
            self._track_ack_failure(conn_id, "error")
            self._record_connect_failure(host, "error", reply)
            logger.warning(
                "conn %s: ACK wait library error [%s]: %s (error_id=%s)",
                conn_id,
                exc.error_code,
                exc.message,
                exc.error_id,
                extra={
                    "conn_id": conn_id,
                    "host": host,
                    "port": port,
                    "error_code": exc.error_code,
                    "error_id": exc.error_id,
                },
            )
            self._conn_handlers.pop(conn_id, None)
            handler.close_remote()
            with contextlib.suppress(OSError):
                handler._writer.close()
                await handler._writer.wait_closed()
            await req.send_reply_error(reply)

        except Exception as exc:
            # Non-library error — unexpected; log with traceback.
            ack_status = "error"
            self._track_ack_failure(conn_id, "error")
            self._record_connect_failure(host, "error", reply)
            logger.warning(
                "conn %s: ACK wait unexpected error: %s",
                conn_id,
                exc,
                extra={"conn_id": conn_id, "host": host, "port": port},
            )
            logger.debug(
                "conn %s ACK wait traceback",
                conn_id,
                exc_info=True,
                extra={"conn_id": conn_id},
            )
            self._conn_handlers.pop(conn_id, None)
            handler.close_remote()
            with contextlib.suppress(OSError):
                handler._writer.close()
                await handler._writer.wait_closed()
            await req.send_reply_error(reply)

        finally:
            if not ack_future.done():
                ack_future.cancel()
            self._pending_connects.pop(conn_id, None)
            self._report_pending_connects_metric()
            metrics_observe(
                "tunnel.conn_ack.wait_sec",
                loop.time() - ack_wait_start,
                status=ack_status,
            )

    async def _handle_udp_associate(self, req: Socks5Request) -> None:
        """UDP ASSOCIATE handler.

        Opens a local UDP socket and relays each datagram through the tunnel.
        Flows are keyed by ``(dst_host, dst_port)`` and reused for the
        lifetime of the association so multi-packet exchanges work correctly.
        """
        relay = req.udp_relay if req.udp_relay is not None else UdpRelay()
        if req.udp_relay is None:
            udp_port = await relay.start()
        else:
            udp_port = relay.local_port

        await req.send_reply_success("127.0.0.1", udp_port)
        logger.debug("UDP ASSOCIATE local port %d", udp_port)

        active_flows: dict[tuple[str, int], _UdpFlowHandler] = {}
        drain_tasks: list[asyncio.Task[None]] = []

        async def drain_flow(
            handler: _UdpFlowHandler,
            dst_host: str,
            dst_port: int,
        ) -> None:
            try:
                while True:
                    data = await handler.recv_datagram()
                    if data is None:
                        break
                    relay.send_to_client(data, dst_host, dst_port)
            except TransportError as exc:
                metrics_inc(
                    "udp.drain.error",
                    error=exc.error_code.replace(".", "_"),
                )
                logger.debug(
                    "udp drain flow=%s [%s]: %s (error_id=%s)",
                    handler.flow_id,
                    exc.error_code,
                    exc.message,
                    exc.error_id,
                )
            except asyncio.CancelledError:
                pass
            finally:
                active_flows.pop((dst_host, dst_port), None)

        async def pump() -> None:
            while True:
                try:
                    payload, dst_host, dst_port = await asyncio.wait_for(
                        relay.recv(), timeout=UDP_PUMP_POLL_TIMEOUT_SECS
                    )
                except TimeoutError:
                    if req.reader.at_eof():
                        break
                    continue

                if is_host_excluded(dst_host, self._tun.exclude):
                    # Direct UDP (excluded subnet) — use correct address family.
                    sock = make_udp_socket(dst_host)
                    sock.setblocking(False)
                    try:
                        loop = asyncio.get_running_loop()
                        await loop.sock_connect(sock, (dst_host, dst_port))
                        await loop.sock_sendall(sock, payload)
                        response = await asyncio.wait_for(
                            loop.sock_recv(sock, 65535),
                            timeout=UDP_DIRECT_RECV_TIMEOUT_SECS,
                        )
                        relay.send_to_client(response, dst_host, dst_port)
                    except (TimeoutError, OSError):
                        pass
                    except Exception:
                        logger.debug(
                            "unexpected error in UDP direct relay to %s:%d",
                            dst_host,
                            dst_port,
                            exc_info=True,
                        )
                    finally:
                        sock.close()
                    continue

                key = (dst_host, dst_port)
                handler = active_flows.get(key)
                if handler is None:
                    flow_id = new_flow_id()
                    handler = _UdpFlowHandler(
                        flow_id,
                        dst_host,
                        dst_port,
                        self._ws_send,
                        self._udp_registry,
                    )
                    self._udp_registry[flow_id] = handler
                    try:
                        await handler.open()
                    except WebSocketSendTimeoutError as exc:
                        metrics_inc("udp.flow.open.error", error="ws_send_timeout")
                        logger.warning(
                            "udp flow %s open timed out [%s] (error_id=%s) — dropping datagram",
                            flow_id,
                            exc.error_code,
                            exc.error_id,
                        )
                        self._udp_registry.pop(flow_id, None)
                        continue
                    except ConnectionClosedError as exc:
                        metrics_inc("udp.flow.open.error", error="connection_closed")
                        logger.warning(
                            "udp flow %s open failed — connection closed [%s] "
                            "(error_id=%s) — dropping datagram",
                            flow_id,
                            exc.error_code,
                            exc.error_id,
                        )
                        self._udp_registry.pop(flow_id, None)
                        continue
                    except TransportError as exc:
                        metrics_inc(
                            "udp.flow.open.error",
                            error=exc.error_code.replace(".", "_"),
                        )
                        logger.debug(
                            "udp flow %s open error [%s]: %s (error_id=%s)",
                            flow_id,
                            exc.error_code,
                            exc.message,
                            exc.error_id,
                        )
                        self._udp_registry.pop(flow_id, None)
                        continue

                    active_flows[key] = handler
                    task = asyncio.create_task(
                        drain_flow(handler, dst_host, dst_port),
                        name=f"udp-drain-{flow_id}",
                    )
                    drain_tasks.append(task)

                try:
                    await handler.send_datagram(payload)
                except WebSocketSendTimeoutError as exc:
                    metrics_inc("udp.datagram.send.error", error="ws_send_timeout")
                    logger.warning(
                        "udp flow %s send timed out [%s] (error_id=%s) — datagram dropped",
                        handler.flow_id,
                        exc.error_code,
                        exc.error_id,
                    )
                except ConnectionClosedError as exc:
                    metrics_inc("udp.datagram.send.error", error="connection_closed")
                    logger.warning(
                        "udp flow %s send failed — connection closed [%s] (error_id=%s)",
                        handler.flow_id,
                        exc.error_code,
                        exc.error_id,
                    )
                except TransportError as exc:
                    metrics_inc(
                        "udp.datagram.send.error",
                        error=exc.error_code.replace(".", "_"),
                    )
                    logger.debug(
                        "udp flow %s send error [%s]: %s (error_id=%s)",
                        handler.flow_id,
                        exc.error_code,
                        exc.message,
                        exc.error_id,
                    )

        try:
            await pump()
        finally:
            for handler in list(active_flows.values()):
                handler.close_remote()
                if handler.flow_id in self._udp_registry:
                    await handler.close()
            for task in drain_tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            relay.close()
            with contextlib.suppress(OSError):
                req.writer.close()
                await req.writer.wait_closed()

    # ── Direct pipe (for excluded TCP hosts) ──────────────────────────────────

    @staticmethod
    async def _pipe(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        remote_reader: asyncio.StreamReader,
        remote_writer: asyncio.StreamWriter,
    ) -> None:
        async def copy(
            src: asyncio.StreamReader,
            dst: asyncio.StreamWriter,
        ) -> None:
            try:
                while True:
                    chunk = await src.read(PIPE_CHUNK_SIZE)
                    if not chunk:
                        if dst.can_write_eof():
                            with contextlib.suppress(OSError):
                                dst.write_eof()
                                await dst.drain()
                        return
                    dst.write(chunk)
                    await dst.drain()
            except OSError:
                pass

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(copy(client_reader, remote_writer))
                tg.create_task(copy(remote_reader, client_writer))
        finally:
            with contextlib.suppress(OSError):
                remote_writer.close()
                await remote_writer.wait_closed()
            with contextlib.suppress(OSError):
                client_writer.close()
                await client_writer.wait_closed()

    # ── Recv loop ─────────────────────────────────────────────────────────────

    async def _recv_loop(self) -> None:
        """Read WebSocket messages and dispatch frames to registered handlers.

        Starts by replaying any frames buffered during ``_wait_ready``
        (frames that arrived after ``AGENT_READY`` but before this task
        started), then takes over the live WebSocket iterator.
        """
        assert self._ws is not None
        ws = self._ws
        buf = self._pre_ready_carry
        self._pre_ready_carry = ""

        for line in self._pre_ready_buf:
            try:
                await self._dispatch_frame_async(line)
            except (FrameDecodingError, ProtocolError) as exc:
                metrics_inc("tunnel.frames.decode_error")
                logger.debug(
                    "bad frame in pre-ready buffer [%s]: %s (error_id=%s) — skipping",
                    exc.error_code,
                    exc.message,
                    exc.error_id,
                )
        self._pre_ready_buf.clear()

        try:
            async for msg in ws:
                chunk = msg.decode() if isinstance(msg, bytes) else msg
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    try:
                        await self._dispatch_frame_async(line)
                    except (FrameDecodingError, ProtocolError) as exc:
                        metrics_inc("tunnel.frames.decode_error")
                        logger.debug(
                            "bad frame [%s]: %s (error_id=%s) — skipping",
                            exc.error_code,
                            exc.message,
                            exc.error_id,
                        )
        except ConnectionClosed:
            pass
        finally:
            for handler in list(self._conn_handlers.values()):
                handler.cancel_upstream()
                handler.close_remote()
            for flow in list(self._udp_registry.values()):
                flow.close_remote()
            self._ws_closed.set()

    async def _dispatch_frame_async(self, line: str) -> None:
        """Parse and dispatch one frame line.

        The sole dispatcher for all incoming frames — used by ``_recv_loop``
        for both the pre-ready-buffer replay and the live WebSocket iterator.
        DATA frames use ``feed_async`` to block the WS reader when the inbound
        queue is full, propagating backpressure all the way to the agent's TCP
        receive buffer.

        Raises
        ------
        FrameDecodingError
            Logged and suppressed internally — a single bad frame must never
            tear down the recv loop.
        ProtocolError
            Logged and suppressed internally — same rationale.
        """
        parsed = parse_frame(line)
        if parsed is None:
            metrics_inc("tunnel.frames.invalid")
            return

        msg_type, conn_id, payload = parsed
        metrics_inc("tunnel.frames.received", type=msg_type)

        if msg_type == "AGENT_READY":
            return

        if msg_type == "CONN_ACK":
            pending = self._pending_connects.get(conn_id)
            if pending is not None and not pending.ack_future.done():
                pending.ack_future.set_result("ack")
            return

        if msg_type == "DATA":
            handler = self._conn_handlers.get(conn_id)
            if handler is None:
                return
            try:
                data = base64.b64decode(payload)
            except Exception as exc:
                raise FrameDecodingError(
                    f"conn {conn_id!r}: invalid base64 in DATA frame.",
                    error_code="protocol.data_frame_bad_base64",
                    details={"conn_id": conn_id, "codec": "base64"},
                    hint="Check for frame corruption or an agent encoding bug.",
                ) from exc
            pending = self._pending_connects.get(conn_id)
            if pending is not None and not pending.ack_future.done():
                if handler._closed.is_set():
                    return
                accepted = handler.feed(data)
                if not accepted:
                    pending.ack_future.set_result("pre_ack_overflow")
                    metrics_inc("tunnel.pre_ack_buffer.overflow")
            else:
                await handler.feed_async(data)
            return

        if msg_type in ("CONN_CLOSED_ACK", "ERROR"):
            handler = self._conn_handlers.get(conn_id)
            if handler is None:
                return
            if msg_type == "ERROR":
                try:
                    reason = base64.b64decode(payload).decode(errors="replace")
                except Exception as exc:
                    raise FrameDecodingError(
                        f"conn {conn_id!r}: invalid base64 in ERROR frame.",
                        error_code="protocol.error_frame_bad_base64",
                        details={"conn_id": conn_id, "codec": "base64"},
                        hint="Check for frame corruption or an agent encoding bug.",
                    ) from exc
                logger.warning(
                    "conn %s agent error: %s",
                    conn_id,
                    reason,
                    extra={"conn_id": conn_id, "error_reason": reason},
                )
                handler.cancel_upstream()
            pending = self._pending_connects.get(conn_id)
            if pending is not None and not pending.ack_future.done():
                pending.ack_future.set_result(
                    "agent_error" if msg_type == "ERROR" else "agent_closed"
                )
            handler.close_remote()
            return

        if msg_type == "UDP_DATA":
            flow = self._udp_registry.get(conn_id)
            if flow is None:
                return
            try:
                data = base64.b64decode(payload)
            except Exception as exc:
                raise FrameDecodingError(
                    f"flow {conn_id!r}: invalid base64 in UDP_DATA frame.",
                    error_code="protocol.udp_data_frame_bad_base64",
                    details={"flow_id": conn_id, "codec": "base64"},
                    hint="Check for frame corruption or an agent encoding bug.",
                ) from exc
            flow.feed(data)
            return

        if msg_type == "UDP_CLOSED":
            flow = self._udp_registry.pop(conn_id, None)
            if flow:
                flow.close_remote()
            return

        # Unknown frame type — raise ProtocolError so the caller can metric it.
        raise ProtocolError(
            f"Unexpected frame type {msg_type!r} received from agent.",
            error_code="protocol.unexpected_frame",
            details={"frame_type": msg_type, "conn_id": conn_id},
            hint="This may indicate an agent/client version mismatch.",
        )

    # ── Send loop ─────────────────────────────────────────────────────────────

    async def _ws_send(
        self,
        frame: str,
        *,
        control: bool = False,
        must_queue: bool = False,
    ) -> None:
        """Enqueue a frame for the send loop.

        Each item placed on the queue is a ``(frame_str, is_data)`` tuple so
        ``_send_loop`` knows whether to apply a send timeout without having to
        inspect the frame content.  Control frames are never dropped.  Data
        frames are dropped when the bounded data queue is full.
        """
        if self._send_ctrl_queue is None or self._send_data_queue is None:
            return
        if control:
            if not self._ws_closed.is_set():
                self._send_ctrl_queue.put_nowait((frame, False))
            return
        if must_queue:
            await self._send_data_queue.put((frame, True))
            return
        try:
            self._send_data_queue.put_nowait((frame, True))
        except asyncio.QueueFull:
            self._send_drop_count += 1
            metrics_inc("tunnel.frames.send_drop")
            if (
                self._send_drop_count == 1
                or self._send_drop_count % SEND_DROP_LOG_EVERY == 0
            ):
                logger.warning(
                    "send data queue full, dropping frame (drops=%d)",
                    self._send_drop_count,
                )

    async def _keepalive_loop(self) -> None:
        """Send a KEEPALIVE control frame at ``ping_interval`` seconds.

        Replaces the websockets built-in ping, which contends for the internal
        write lock with ``ws.send()`` in ``_send_loop`` and can time out
        (closing the connection) when the send queue is busy.
        """
        interval = float(self._app.bridge.ping_interval)
        keepalive_frame = f"{FRAME_PREFIX}KEEPALIVE{FRAME_SUFFIX}"
        while not self._ws_closed.is_set():
            await asyncio.sleep(interval)
            await self._ws_send(keepalive_frame, control=True)

    async def _send_loop(self) -> None:
        """Single writer to the WebSocket — serialises all outgoing frames.

        Queue items are ``(frame_str, is_data)`` tuples.  ``is_data=True``
        means the frame is a DATA/UDP_DATA payload frame (send timeout applied);
        ``is_data=False`` means it is a control frame (same send timeout).
        Exits cleanly when it dequeues the ``None`` sentinel (sent by
        ``_serve``'s finally block) or when the WebSocket reports closed.

        Raises
        ------
        WebSocketSendTimeoutError
            Raised internally, logged, and causes the loop to exit so
            ``_serve`` can trigger a reconnect.
        ConnectionClosedError
            Same — logged at DEBUG and causes a clean exit.
        """
        assert self._ws is not None
        assert self._send_ctrl_queue is not None
        assert self._send_data_queue is not None
        ws = self._ws
        ctrl_q = self._send_ctrl_queue
        data_q = self._send_data_queue
        deferred_data_item: tuple[str, bool] | None = None
        try:
            while True:
                item: tuple[str, bool] | None
                try:
                    item = ctrl_q.get_nowait()
                except asyncio.QueueEmpty:
                    if deferred_data_item is not None:
                        try:
                            item = ctrl_q.get_nowait()
                        except asyncio.QueueEmpty:
                            item = deferred_data_item
                            deferred_data_item = None
                    else:
                        ctrl_wait = asyncio.create_task(ctrl_q.get())
                        data_wait = asyncio.create_task(data_q.get())
                        done, pending = await asyncio.wait(
                            {ctrl_wait, data_wait},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        rescued_ctrl: tuple[str, bool] | None = None
                        for pending_task in pending:
                            pending_task.cancel()
                            try:
                                await pending_task
                            except asyncio.CancelledError:
                                # cancel() beat get(): the item may have been
                                # placed into the queue at the same instant as
                                # the cancellation, in which case Python leaves
                                # it in the queue.  Recover it now so it is not
                                # silently lost (the primary download-drop bug).
                                if pending_task is data_wait:
                                    with contextlib.suppress(asyncio.QueueEmpty):
                                        deferred_data_item = data_q.get_nowait()
                                continue
                            # get() beat cancel(): task completed normally.
                            if not pending_task.cancelled():
                                with contextlib.suppress(Exception):
                                    rescued = pending_task.result()
                                    if rescued is not None:
                                        if pending_task is data_wait:
                                            deferred_data_item = rescued
                                        else:
                                            rescued_ctrl = rescued
                        if ctrl_wait in done:
                            item = ctrl_wait.result()
                            if data_wait in done:
                                data_item = data_wait.result()
                                if data_item is not None:
                                    deferred_data_item = data_item
                        else:
                            item = data_wait.result()
                            if rescued_ctrl is not None:
                                await ctrl_q.put(rescued_ctrl)

                if item is None:
                    return

                frame, is_data_frame = item
                try:
                    metrics_inc("tunnel.frames.sent")
                    send_timeout = self._app.bridge.send_timeout
                    await asyncio.wait_for(
                        ws.send(frame.encode()),
                        timeout=send_timeout,
                    )
                except TimeoutError:
                    metrics_inc("tunnel.frames.send_timeout")
                    raise WebSocketSendTimeoutError(
                        "WebSocket frame send timed out — connection stalled.",
                        error_code="transport.ws_send_timeout",
                        details={
                            "timeout_s": self._app.bridge.send_timeout,
                            "frame_prefix": frame[:40],
                        },
                        hint=(
                            "Increase EXECTUNNEL_SEND_TIMEOUT or check network "
                            "latency to the tunnel endpoint."
                        ),
                    )
                except ConnectionClosed as exc:
                    metrics_inc("tunnel.frames.send_closed")
                    raise ConnectionClosedError(
                        "WebSocket connection closed while sending frame.",
                        error_code="transport.connection_closed",
                        details={
                            "close_code": getattr(exc.rcvd, "code", None),
                            "close_reason": getattr(exc.rcvd, "reason", None),
                        },
                    ) from exc

        finally:
            self._ws_closed.set()
