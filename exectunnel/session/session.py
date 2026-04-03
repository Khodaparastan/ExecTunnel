"""TunnelSession — core session orchestration, bootstrap, and SOCKS5 routing.

Bootstraps ``agent.py`` into the pod and runs a local SOCKS5 proxy that routes
all connections through the WebSocket tunnel.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import random
from collections import defaultdict
from dataclasses import dataclass

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from exectunnel.config.defaults import (
    BOOTSTRAP_CHUNK_SIZE_CHARS as BOOTSTRAP_CHUNK_SIZE,
)
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
from exectunnel.config.defaults import (
    PIPE_READ_CHUNK_BYTES as PIPE_CHUNK_SIZE,
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
from exectunnel.observability import metrics_inc, metrics_observe, span
from exectunnel.protocol.enums import Reply
from exectunnel.protocol.frames import (
    FRAME_PREFIX,
    FRAME_SUFFIX,
    decode_data_payload,
    encode_conn_open_frame,
    is_ready_frame,
    parse_frame,
)
from exectunnel.protocol.ids import new_conn_id, new_flow_id
from exectunnel.proxy.dns_forwarder import _DnsForwarder
from exectunnel.proxy.relay import UdpRelay
from exectunnel.proxy.request import Socks5Request
from exectunnel.proxy.server import Socks5Server
from utils import is_host_excluded, load_agent_b64, make_udp_socket
from exectunnel.transport.connection import _TcpConnectionHandler
from exectunnel.transport.udp_flow import _UdpFlowHandler

logger = logging.getLogger("exectunnel.transport.session")

# Maximum size of the control send queue.  Unbounded growth is prevented by
# capping at a generous limit — control frames are small and infrequent.
_CTRL_QUEUE_CAP = 1_024

# Maximum number of distinct hosts tracked in per-host rate-limit structures.
# Oldest entries are evicted when the limit is reached (LRU via OrderedDict).
_HOST_GATE_MAX = 4_096

# Reconnect jitter fraction — delay is multiplied by U(0, _RECONNECT_JITTER).
_RECONNECT_JITTER = 0.1


# ── Pending connect state ─────────────────────────────────────────────────────


@dataclass(slots=True)
class PendingConnectState:
    """Tracks one in-flight CONN_OPEN that has not yet received CONN_ACK."""

    host: str
    ack_future: asyncio.Future[str]


# ── LRU dict helper ───────────────────────────────────────────────────────────


class _LruDict(collections.OrderedDict):
    """OrderedDict that evicts the oldest entry when ``maxsize`` is exceeded."""

    def __init__(self, maxsize: int) -> None:
        super().__init__()
        self._maxsize = maxsize

    def __setitem__(self, key: object, value: object) -> None:
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self._maxsize:
            self.popitem(last=False)


# ── Tunnel session ────────────────────────────────────────────────────────────


class TunnelSession:
    """Bootstraps ``agent.py`` into the pod and runs a local SOCKS5 proxy.

    Raises:
        AgentReadyTimeoutError:    Agent did not emit ``AGENT_READY`` in time.
        AgentSyntaxError:          Agent script failed to parse on the remote.
        AgentVersionMismatchError: Remote agent reports an incompatible version.
        BootstrapError:            Any other bootstrap failure.
        ReconnectExhaustedError:   All reconnect attempts exhausted.
    """

    def __init__(self, app_cfg: AppConfig, tun_cfg: TunnelConfig) -> None:
        self._app = app_cfg
        self._tun = tun_cfg
        self._ws: ClientConnection | None = None

        # Per-session handler registries — cleared in _reset_session_state().
        self._conn_handlers: dict[str, _TcpConnectionHandler] = {}
        self._pending_connects: dict[str, PendingConnectState] = {}
        self._udp_registry: dict[str, _UdpFlowHandler] = {}

        # WebSocket closed signal — set by _recv_loop / _send_loop finally.
        self._ws_closed: asyncio.Event = asyncio.Event()

        # Send queues — initialised per session in _run_session().
        self._send_ctrl_queue: asyncio.Queue[tuple[str, bool] | None] | None = None
        self._send_data_queue: asyncio.Queue[tuple[str, bool] | None] | None = None

        # In-flight request tasks.
        self._request_tasks: set[asyncio.Task[None]] = set()

        # Bootstrap carry-over buffers.
        self._pre_ready_buf: list[str] = []
        self._pre_ready_carry: str = ""
        self._bootstrap_diag: collections.deque[str] = collections.deque(
            maxlen=BOOTSTRAP_DIAG_MAX_LINES
        )

        # Send-drop telemetry.
        self._send_drop_count = 0

        # ACK timeout / reconnect hardening.
        self._ack_timeout_count = 0
        self._ack_timeout_suppressed = 0
        self._ack_timeout_window_start: float | None = None
        self._ack_timeout_window_count = 0
        self._ack_reconnect_requested = False
        self._ack_timeout_warn_every = tun_cfg.ack_timeout_warn_every
        self._ack_timeout_window_secs = tun_cfg.ack_timeout_window_secs
        self._ack_timeout_reconnect_threshold = tun_cfg.ack_timeout_reconnect_threshold

        # Connect hardening — semaphores and pacing.
        self._connect_max_pending = tun_cfg.connect_max_pending
        self._connect_max_pending_per_host = tun_cfg.connect_max_pending_per_host
        self._connect_pace_cf_ms = tun_cfg.connect_pace_cf_ms
        self._pre_ack_buffer_cap_bytes = tun_cfg.pre_ack_buffer_cap_bytes
        self._connect_gate = asyncio.Semaphore(self._connect_max_pending)

        # LRU-bounded per-host structures to prevent unbounded memory growth.
        self._host_connect_gates: _LruDict = _LruDict(_HOST_GATE_MAX)
        self._host_connect_open_locks: _LruDict = _LruDict(_HOST_GATE_MAX)
        self._host_connect_last_open_at: _LruDict = _LruDict(_HOST_GATE_MAX)
        self._connect_failures_by_host: defaultdict[str, int] = defaultdict(int)

    # ── Top-level run ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect, bootstrap the agent, and serve.

        Retries on transport/session interruptions using ``BridgeConfig``
        reconnect settings.  Bootstrap failures are always fatal and never
        retried.

        Raises:
            BootstrapError:          Propagated immediately; never retried.
            ReconnectExhaustedError: All reconnect attempts consumed.
        """
        ssl_ctx = self._app.ssl_context()
        retries = self._app.bridge.reconnect_max_retries
        base_delay = self._app.bridge.reconnect_base_delay
        max_delay = self._app.bridge.reconnect_max_delay
        attempt = 0

        logger.info(
            "connect hardening: global=%d per_host=%d cf_pace_ms=%d",
            self._connect_max_pending,
            self._connect_max_pending_per_host,
            self._connect_pace_cf_ms,
        )

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
                        # A clean session exit does not trigger reconnect.
                        reconnect_reason = None

                except BootstrapError:
                    metrics_inc("tunnel.bootstrap.error")
                    raise

                except WebSocketSendTimeoutError as exc:
                    metrics_inc("tunnel.connect.error", error="ws_send_timeout")
                    reconnect_reason = (
                        f"ws_send_timeout: {exc.message} (error_id={exc.error_id})"
                    )
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
                        "WebSocket connection closed [%s] close_code=%s "
                        "(error_id=%s) — will reconnect",
                        exc.error_code,
                        exc.details.get("close_code"),
                        exc.error_id,
                    )

                except TransportError as exc:
                    metrics_inc(
                        "tunnel.connect.error",
                        error=exc.error_code.replace(".", "_"),
                    )
                    reconnect_reason = (
                        f"{exc.error_code}: {exc.message} (error_id={exc.error_id})"
                    )
                    logger.warning(
                        "Transport error [%s]: %s (error_id=%s) — will reconnect",
                        exc.error_code,
                        exc.message,
                        exc.error_id,
                    )

                except (OSError, ConnectionClosed) as exc:
                    # Third-party / stdlib transport errors not yet wrapped by
                    # our exception hierarchy.
                    metrics_inc("tunnel.connect.error", error=type(exc).__name__)
                    reconnect_reason = str(exc) or type(exc).__name__

                except TimeoutError as exc:
                    metrics_inc("tunnel.connect.error", error="timeout")
                    reconnect_reason = f"timeout: {exc}"

                finally:
                    self._ws = None

                if reconnect_reason is None:
                    # Clean exit — do not reconnect.
                    return

                # Reset backoff counter after each healthy session so the
                # retry budget is per-consecutive-failure-run, not lifetime.
                if bootstrapped:
                    attempt = 0

                if attempt >= retries:
                    metrics_inc("tunnel.reconnect.exhausted")
                    raise ReconnectExhaustedError(
                        f"WebSocket session terminated after {retries} "
                        "reconnect attempts.",
                        error_code="transport.reconnect_exhausted",
                        details={
                            "attempts": retries,
                            "last_error": reconnect_reason,
                        },
                        hint=(
                            "Check network connectivity to the tunnel endpoint "
                            "and increase EXECTUNNEL_RECONNECT_MAX_RETRIES if "
                            "transient disruptions are expected."
                        ),
                    )

                delay = min(base_delay * (2**attempt), max_delay)
                # Add jitter to prevent thundering-herd reconnects.
                delay += random.uniform(0, delay * _RECONNECT_JITTER)
                attempt += 1
                metrics_inc("tunnel.reconnect.scheduled")
                metrics_observe("tunnel.reconnect.delay_sec", delay)
                logger.warning(
                    "WebSocket disconnected (%s), reconnecting in %.1fs "
                    "(attempt %d/%d)",
                    reconnect_reason,
                    delay,
                    attempt,
                    retries,
                )
                await asyncio.sleep(delay)

    # ── Session init ──────────────────────────────────────────────────────────

    def _reset_session_state(self) -> None:
        """Clear all per-session handler registries before a new session starts.

        Stale handlers from a previous session must not receive frames from
        the new session's ``_recv_loop``.
        """
        self._conn_handlers.clear()
        self._pending_connects.clear()
        self._udp_registry.clear()
        self._pre_ready_buf.clear()
        self._pre_ready_carry = ""
        self._bootstrap_diag.clear()

    async def _run_session(self, ws: ClientConnection) -> None:
        """Initialise per-session state, bootstrap the agent, and serve.

        Extracted from ``run()`` so integration tests can inject a
        pre-connected ``ClientConnection`` without going through the real
        ``connect()`` call.
        """
        metrics_inc("tunnel.connect.ok")
        self._ws = ws
        self._ws_closed.clear()
        self._reset_session_state()

        self._send_ctrl_queue = asyncio.Queue(maxsize=_CTRL_QUEUE_CAP)
        self._send_data_queue = asyncio.Queue(maxsize=self._app.bridge.send_queue_cap)

        await self._bootstrap()

        # Successful bootstrap resets reconnect / ACK window state.
        self._ack_reconnect_requested = False
        self._ack_timeout_window_start = None
        self._ack_timeout_window_count = 0

        await self._serve()

    # ── Bootstrap ─────────────────────────────────────────────────────────────

    async def _bootstrap(self) -> None:
        """Upload and start the agent script on the remote end.

        Raises:
            AgentReadyTimeoutError:    ``AGENT_READY`` not received in time.
            AgentSyntaxError:          Remote Python raised ``SyntaxError``.
            AgentVersionMismatchError: Remote agent version incompatible.
            BootstrapError:            Any other startup failure.
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
        metrics_observe(
            "tunnel.bootstrap.chunks",
            float(len(range(0, len(agent_b64), BOOTSTRAP_CHUNK_SIZE))),
        )

        # Clean up any leftover files from a previous run.
        await send_cmd("rm -f '/tmp/exectunnel_agent.py' '/tmp/exectunnel_agent.b64'")
        await asyncio.sleep(BOOTSTRAP_RM_DELAY_SECS)

        # Upload in chunks — safe under all POSIX shell input-buffer limits.
        # base64 alphabet ([A-Za-z0-9+/=] or urlsafe [-_]) contains no shell
        # metacharacters so the printf argument is injection-safe.
        for i in range(0, len(agent_b64), BOOTSTRAP_CHUNK_SIZE):
            chunk = agent_b64[i : i + BOOTSTRAP_CHUNK_SIZE]
            await send_cmd(f"printf '%s' '{chunk}' >> '/tmp/exectunnel_agent.b64'")

        await send_cmd(
            "base64 -d '/tmp/exectunnel_agent.b64' > '/tmp/exectunnel_agent.py'"
        )
        await asyncio.sleep(BOOTSTRAP_DECODE_DELAY_SECS)

        # Syntax-check before exec so decode corruption is caught cleanly.
        await send_cmd(
            "python3 -c '"
            'import ast,sys; ast.parse(open("/tmp/exectunnel_agent.py").read()); '
            'sys.stdout.write("SYNTAX_OK\\n"); sys.stdout.flush()\''
        )

        # exec replaces the shell — after this the WebSocket channel IS the agent.
        await send_cmd("exec python3 '/tmp/exectunnel_agent.py'")

        logger.info("waiting for agent AGENT_READY…")
        try:
            await asyncio.wait_for(
                self._wait_ready(ws),
                timeout=self._tun.ready_timeout,
            )
        except TimeoutError:
            detail = (
                f"; last output: {self._bootstrap_diag[-1]}"
                if self._bootstrap_diag
                else ""
            )
            metrics_inc("tunnel.bootstrap.timeout")
            raise AgentReadyTimeoutError(
                f"Agent did not signal AGENT_READY within "
                f"{self._tun.ready_timeout}s{detail}",
                error_code="bootstrap.agent_ready_timeout",
                details={
                    "timeout_s": self._tun.ready_timeout,
                    "last_output": (
                        self._bootstrap_diag[-1] if self._bootstrap_diag else None
                    ),
                },
                hint=(
                    "Increase EXECTUNNEL_AGENT_TIMEOUT or check the remote "
                    "Python version and available memory."
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

        Uses :func:`~exectunnel.protocol.frames.is_ready_frame` for robust
        detection that is immune to whitespace and future format changes.

        Raises:
            AgentSyntaxError:          Remote Python reported a ``SyntaxError``.
            AgentVersionMismatchError: Remote agent version incompatible.
            BootstrapError:            WebSocket closed before ``AGENT_READY``.
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
                    if is_ready_frame(stripped):
                        ready = True
                        continue
                    if stripped:
                        self._bootstrap_diag.append(stripped)
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
                                "Check for base64 decode corruption or an "
                                "incompatible remote Python version."
                            ),
                        )
                    if stripped.startswith("VERSION_MISMATCH:"):
                        remote_version = stripped.split(":", 1)[-1].strip()
                        raise AgentVersionMismatchError(
                            f"Remote agent version {remote_version!r} is "
                            "incompatible with this client.",
                            error_code="bootstrap.version_mismatch",
                            details={
                                "remote_version": remote_version,
                                "local_version": self._app.version,
                            },
                            hint=(
                                "Upgrade the exectunnel client or redeploy "
                                "the agent to match the client version."
                            ),
                        )
                else:
                    self._pre_ready_buf.append(stripped)
            if ready:
                # Remaining bytes are a partial (unterminated) frame.
                # _recv_loop prepends this to its own buffer so the fragment
                # is completed by the next WebSocket message.
                self._pre_ready_carry = buf
                return

        detail = f": {self._bootstrap_diag[-1]}" if self._bootstrap_diag else ""
        raise BootstrapError(
            f"WebSocket closed before AGENT_READY was received{detail}",
            error_code="bootstrap.ws_closed_before_ready",
            details={
                "last_output": (
                    self._bootstrap_diag[-1] if self._bootstrap_diag else None
                ),
            },
            hint=(
                "Check that the remote shell executed the agent script successfully."
            ),
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
        keepalive_task = asyncio.create_task(
            self._keepalive_loop(), name="tun-keepalive"
        )

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

            # Unblock _send_loop from queue.get().
            if self._send_ctrl_queue is not None:
                with contextlib.suppress(asyncio.QueueFull):
                    self._send_ctrl_queue.put_nowait(None)
            if self._send_data_queue is not None:
                with contextlib.suppress(asyncio.QueueFull):
                    self._send_data_queue.put_nowait(None)

            await asyncio.gather(
                recv_task,
                send_task,
                socks_task,
                keepalive_task,
                return_exceptions=True,
            )

            # Cancel and await all in-flight request tasks.
            for task in list(self._request_tasks):
                task.cancel()
            if self._request_tasks:
                await asyncio.gather(*self._request_tasks, return_exceptions=True)

            await socks.stop()
            if dns_fwd is not None:
                await dns_fwd.stop()

            metrics_inc("tunnel.serve.stopped")

    async def _socks_loop(self, socks: Socks5Server) -> None:
        async for req in socks:
            task = asyncio.create_task(
                self._handle_request(req),
                name=f"req-{req.cmd.name}-{req.host}_{req.port}",
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
        metrics_inc("tunnel.request.error", error=type(exc).__name__)
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

    # ── ACK failure tracking ──────────────────────────────────────────────────

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
                ws.close(
                    code=WS_CLOSE_CODE_UNHEALTHY,
                    reason="conn ack timeout surge",
                )
            )
            self._request_tasks.add(task)
            task.add_done_callback(self._request_tasks.discard)

    # ── Connect hardening helpers ─────────────────────────────────────────────

    @staticmethod
    def _connect_host_key(host: str) -> str:
        return host.lower().strip()

    def _connect_pace_interval_for_host(self, host_key: str) -> float:
        if host_key == "challenges.cloudflare.com":
            return self._connect_pace_cf_ms / 1000.0
        return 0.0

    def _connect_host_gate(self, host: str) -> asyncio.Semaphore:
        key = self._connect_host_key(host)
        gate = self._host_connect_gates.get(key)
        if gate is None:
            gate = asyncio.Semaphore(self._connect_max_pending_per_host)
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
                            0.0,
                            min(CONNECT_PACE_JITTER_CAP_SECS, interval / 2.0),
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

    # ── Failed connect cleanup helper ─────────────────────────────────────────

    async def _cleanup_failed_connect(
        self,
        conn_id: str,
        handler: _TcpConnectionHandler,
        reply: Reply,
    ) -> None:
        """Evict handler, signal close, close writer, send SOCKS5 error reply."""
        self._conn_handlers.pop(conn_id, None)
        handler.close_remote()
        # Use the public closed event and the handler's own writer close path.
        with contextlib.suppress(OSError, RuntimeError):
            handler._writer.close()  # noqa: SLF001 — no public close() on handler
            await asyncio.wait_for(
                handler._writer.wait_closed(),  # noqa: SLF001
                timeout=5.0,
            )

    # ── CONNECT handler ───────────────────────────────────────────────────────

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
        # Default reply — overridden per failure reason below.
        reply = Reply.HOST_UNREACHABLE
        failure_reason: str | None = None

        try:
            async with self._connect_gate, host_gate:
                host_key = self._connect_host_key(host)
                await self._apply_connect_pacing(host_key)

                try:
                    await self._ws_send(
                        encode_conn_open_frame(conn_id, host, port),
                        control=True,
                    )
                except WebSocketSendTimeoutError as exc:
                    ack_status = "ws_send_timeout"
                    failure_reason = "ws_send_timeout"
                    reply = Reply.GENERAL_FAILURE
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
                    reply = Reply.NET_UNREACHABLE
                    metrics_inc("tunnel.conn_open.error", error="connection_closed")
                    logger.warning(
                        "conn %s: CONN_OPEN failed — connection closed [%s] "
                        "(error_id=%s)",
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
                        extra={
                            "conn_id": conn_id,
                            "host": host,
                            "port": port,
                        },
                    )

                    # Wait for agent ACK; abort immediately if WS closes.
                    # Use ack_future directly in asyncio.wait — no wrapping needed.
                    ws_closed_task = asyncio.create_task(
                        self._ws_closed.wait(),
                        name=f"ws-closed-wait-{conn_id}",
                    )
                    done, pending_wait = await asyncio.wait(
                        {ack_future, ws_closed_task},
                        timeout=self._tun.conn_ack_timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for fut in pending_wait:
                        fut.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await fut

                    if ws_closed_task in done:
                        ack_status = "ws_closed"
                        failure_reason = "ws_closed"
                        reply = Reply.NET_UNREACHABLE
                    elif ack_future in done:
                        ack_result = ack_future.result()
                        if ack_result != "ack":
                            ack_status = ack_result
                            failure_reason = ack_result
                            reply = Reply.HOST_UNREACHABLE
                    else:
                        # Timeout — neither future completed.
                        ack_status = "timeout"
                        failure_reason = "timeout"
                        reply = Reply.HOST_UNREACHABLE
                        metrics_inc(
                            "connect_ack_timeout",
                            host=self._connect_host_key(host),
                        )
            # Semaphores released here — before starting data flow.

            if failure_reason is not None:
                self._track_ack_failure(conn_id, failure_reason)
                self._record_connect_failure(host, failure_reason, reply)
                await self._cleanup_failed_connect(conn_id, handler, reply)
                await req.send_reply_error(reply)
                return

            metrics_inc("tunnel.conn_ack.ok")
            metrics_inc("socks_reply_code", code=int(Reply.SUCCESS))
            await req.send_reply_success()
            handler.start()

        except ExecTunnelError as exc:
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
            await self._cleanup_failed_connect(conn_id, handler, reply)
            await req.send_reply_error(reply)

        except Exception as exc:
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
            await self._cleanup_failed_connect(conn_id, handler, reply)
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

    # ── UDP ASSOCIATE handler ─────────────────────────────────────────────────

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
        # Use a set with done-callback discard to prevent unbounded growth.
        drain_tasks: set[asyncio.Task[None]] = set()

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
            except (TransportError, ExecTunnelError) as exc:
                metrics_inc(
                    "udp.drain.error",
                    error=getattr(exc, "error_code", type(exc).__name__).replace(
                        ".", "_"
                    ),
                )
                logger.debug(
                    "udp drain flow=%s [%s]: %s",
                    handler.flow_id,
                    getattr(exc, "error_code", type(exc).__name__),
                    exc,
                )
            except asyncio.CancelledError:
                pass
            finally:
                active_flows.pop((dst_host, dst_port), None)

        async def pump() -> None:
            while True:
                try:
                    result = await asyncio.wait_for(
                        relay.recv(),
                        timeout=UDP_PUMP_POLL_TIMEOUT_SECS,
                    )
                except TimeoutError:
                    if req.reader.at_eof():
                        break
                    continue

                # relay.recv() returns None when the relay is closed.
                if result is None:
                    break

                payload, dst_host, dst_port = result

                if is_host_excluded(dst_host, self._tun.exclude):
                    # Direct UDP (excluded subnet).
                    sock = None
                    try:
                        sock = make_udp_socket(dst_host)
                        sock.setblocking(False)
                        loop = asyncio.get_running_loop()
                        await loop.sock_connect(sock, (dst_host, dst_port))
                        await loop.sock_sendall(sock, payload)
                        response = await asyncio.wait_for(
                            loop.sock_recv(sock, 65_535),
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
                        if sock is not None:
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
                    except (
                        WebSocketSendTimeoutError,
                        ConnectionClosedError,
                        TransportError,
                        ExecTunnelError,
                    ) as exc:
                        err = getattr(exc, "error_code", type(exc).__name__).replace(
                            ".", "_"
                        )
                        metrics_inc("udp.flow.open.error", error=err)
                        logger.warning(
                            "udp flow %s open error [%s] — dropping datagram",
                            flow_id,
                            err,
                        )
                        self._udp_registry.pop(flow_id, None)
                        continue

                    active_flows[key] = handler
                    task = asyncio.create_task(
                        drain_flow(handler, dst_host, dst_port),
                        name=f"udp-drain-{flow_id}",
                    )
                    drain_tasks.add(task)
                    task.add_done_callback(drain_tasks.discard)

                try:
                    await handler.send_datagram(payload)
                except (
                    WebSocketSendTimeoutError,
                    ConnectionClosedError,
                    TransportError,
                ) as exc:
                    err = getattr(exc, "error_code", type(exc).__name__).replace(
                        ".", "_"
                    )
                    metrics_inc("udp.datagram.send.error", error=err)
                    logger.warning(
                        "udp flow %s send error [%s] — datagram dropped",
                        handler.flow_id,
                        err,
                    )

        try:
            await pump()
        finally:
            for h in list(active_flows.values()):
                h.close_remote()
                if h.flow_id in self._udp_registry:
                    with contextlib.suppress(
                        WebSocketSendTimeoutError,
                        TransportError,
                        ExecTunnelError,
                        OSError,
                    ):
                        await h.close()
            for task in list(drain_tasks):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            relay.close()
            with contextlib.suppress(OSError, RuntimeError):
                req.writer.close()
                await req.writer.wait_closed()

    # ── Direct pipe (excluded TCP hosts) ─────────────────────────────────────

    @staticmethod
    async def _pipe(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        remote_reader: asyncio.StreamReader,
        remote_writer: asyncio.StreamWriter,
    ) -> None:
        """Bidirectional byte copy for directly-connected (excluded) hosts."""

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

        Starts by replaying frames buffered during ``_wait_ready``, then takes
        over the live WebSocket iterator.
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

        Uses :func:`~exectunnel.protocol.frames.parse_frame` which returns a
        :class:`~exectunnel.protocol.frames.ParsedFrame` NamedTuple.  Unknown
        frame types are already filtered by ``parse_frame`` (returns ``None``).

        DATA and UDP_DATA payloads are decoded with
        :func:`~exectunnel.protocol.frames.decode_data_payload` which handles
        ``urlsafe_b64encode`` with no-padding — consistent with the protocol
        package's encoding.

        Raises:
            FrameDecodingError: On base64 decode failure.
            ProtocolError:      On structural frame violations.
        """
        parsed = parse_frame(line)
        if parsed is None:
            metrics_inc("tunnel.frames.invalid")
            return

        msg_type = parsed.msg_type
        conn_id = parsed.conn_id
        payload = parsed.payload
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
                data = decode_data_payload(payload)
            except ValueError as exc:
                raise FrameDecodingError(
                    f"conn {conn_id!r}: invalid base64url in DATA frame.",
                    error_code="protocol.data_frame_bad_base64",
                    details={"conn_id": conn_id, "codec": "urlsafe_b64"},
                    hint="Check for frame corruption or an agent encoding bug.",
                ) from exc
            pending = self._pending_connects.get(conn_id)
            if pending is not None and not pending.ack_future.done():
                if handler.closed.is_set():
                    return
                accepted = handler.feed(data)
                if not accepted:
                    pending.ack_future.set_result("pre_ack_overflow")
                    metrics_inc("tunnel.pre_ack_buffer.overflow")
            else:
                await handler.feed_async(data)
            return

        if msg_type in ("CONN_CLOSE", "ERROR"):
            # CONN_CLOSE: agent signals it has closed the remote TCP connection.
            # ERROR: agent signals a connection-level error.
            handler = self._conn_handlers.get(conn_id)
            if handler is None:
                return
            if msg_type == "ERROR":
                try:
                    reason = decode_data_payload(payload).decode(errors="replace")
                except ValueError as exc:
                    raise FrameDecodingError(
                        f"conn {conn_id!r}: invalid base64url in ERROR frame.",
                        error_code="protocol.error_frame_bad_base64",
                        details={"conn_id": conn_id, "codec": "urlsafe_b64"},
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
                data = decode_data_payload(payload)
            except ValueError as exc:
                raise FrameDecodingError(
                    f"flow {conn_id!r}: invalid base64url in UDP_DATA frame.",
                    error_code="protocol.udp_data_frame_bad_base64",
                    details={"flow_id": conn_id, "codec": "urlsafe_b64"},
                    hint="Check for frame corruption or an agent encoding bug.",
                ) from exc
            flow.feed(data)
            return

        if msg_type == "UDP_CLOSE":
            flow = self._udp_registry.pop(conn_id, None)
            if flow is not None:
                flow.close_remote()
            return

        # parse_frame already filters unknown msg_types — this is unreachable
        # under normal operation but kept as a safety net for future frame types
        # that parse_frame accepts before this dispatcher is updated.
        logger.debug(
            "unhandled frame type %r for conn/flow %r — ignoring",
            msg_type,
            conn_id,
        )

    # ── Send helpers ──────────────────────────────────────────────────────────

    async def _ws_send(
        self,
        frame: str,
        *,
        control: bool = False,
        must_queue: bool = False,
    ) -> None:
        """Enqueue a frame for the send loop.

        Control frames are never dropped and bypass the bounded data queue.
        Data frames are dropped when the bounded data queue is full (unless
        ``must_queue=True``, which blocks until space is available).

        After the WebSocket closes, ``must_queue`` callers are unblocked by
        the poison-pill sentinel sent by ``_serve``'s finally block.
        """
        if self._send_ctrl_queue is None or self._send_data_queue is None:
            return

        if control:
            if not self._ws_closed.is_set():
                with contextlib.suppress(asyncio.QueueFull):
                    self._send_ctrl_queue.put_nowait((frame, False))
            return

        if must_queue:
            # Check _ws_closed before blocking to avoid hanging after teardown.
            if self._ws_closed.is_set():
                return
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

        Uses ``asyncio.wait_for`` on ``_ws_closed.wait()`` so the loop exits
        promptly when the WebSocket closes rather than sleeping for a full
        interval.

        The KEEPALIVE frame is intentionally not in ``_VALID_MSG_TYPES`` —
        the agent ignores it; it exists solely to keep the WebSocket alive
        through NAT/proxy idle timeouts.
        """
        interval = float(self._app.bridge.ping_interval)
        keepalive_frame = f"{FRAME_PREFIX}KEEPALIVE{FRAME_SUFFIX}\n"
        while True:
            try:
                await asyncio.wait_for(
                    self._ws_closed.wait(),
                    timeout=interval,
                )
                # _ws_closed was set — exit the loop.
                return
            except TimeoutError:
                # Interval elapsed without close — send keepalive.
                await self._ws_send(keepalive_frame, control=True)

    async def _send_loop(self) -> None:
        """Single writer to the WebSocket — serialises all outgoing frames.

        Prioritises control frames over data frames.  Exits cleanly on the
        ``None`` sentinel or when the WebSocket reports closed.

        Raises:
            WebSocketSendTimeoutError: Propagated to ``_serve`` to trigger reconnect.
            ConnectionClosedError:     Same.
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

                # ── Priority: drain control queue first ───────────────────────
                try:
                    item = ctrl_q.get_nowait()
                except asyncio.QueueEmpty:
                    if deferred_data_item is not None:
                        # Check ctrl_q once more before sending deferred data.
                        try:
                            item = ctrl_q.get_nowait()
                        except asyncio.QueueEmpty:
                            item = deferred_data_item
                            deferred_data_item = None
                    else:
                        # Both queues empty — block on whichever fires first.
                        ctrl_wait = asyncio.create_task(
                            ctrl_q.get(), name="send-ctrl-wait"
                        )
                        data_wait = asyncio.create_task(
                            data_q.get(), name="send-data-wait"
                        )
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
                                # cancel() may have raced with a successful
                                # get() — recover the item so it is not lost.
                                if pending_task is data_wait:
                                    with contextlib.suppress(asyncio.QueueEmpty):
                                        deferred_data_item = data_q.get_nowait()
                                continue
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
                                result = data_wait.result()
                                if result is not None:
                                    deferred_data_item = result
                        else:
                            item = data_wait.result()
                            if rescued_ctrl is not None:
                                with contextlib.suppress(asyncio.QueueFull):
                                    ctrl_q.put_nowait(rescued_ctrl)

                if item is None:
                    return

                frame, _ = item  # is_data_frame unused — removed dead variable
                try:
                    metrics_inc("tunnel.frames.sent")
                    await asyncio.wait_for(
                        ws.send(frame.encode()),
                        timeout=self._app.bridge.send_timeout,
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
