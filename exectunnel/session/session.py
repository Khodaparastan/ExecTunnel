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

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from exectunnel.config.defaults import (
    BOOTSTRAP_CHUNK_SIZE_CHARS,
    BOOTSTRAP_DECODE_DELAY_SECS,
    BOOTSTRAP_DIAG_MAX_LINES,
    BOOTSTRAP_RM_DELAY_SECS,
    BOOTSTRAP_STTY_DELAY_SECS,
    CONNECT_FAILURE_WARN_EVERY,
    CONNECT_PACE_JITTER_CAP_SECS,
    PIPE_READ_CHUNK_BYTES,
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
from exectunnel.protocol.ids import ID_RE, new_conn_id, new_flow_id
from exectunnel.proxy.request import Socks5Request
from exectunnel.proxy.server import Socks5Server
from exectunnel.transport.tcp import TcpConnection
from exectunnel.transport.udp import UdpFlow

from ._io import load_agent_b64, make_udp_socket
from ._routing import is_host_excluded
from ._state import PendingConnectState, _LruDict
from .dns import DnsForwarder

logger = logging.getLogger(__name__)

# Maximum size of the control send queue.  Unbounded growth is prevented by
# capping at a generous limit — control frames are small and infrequent.
# The queue is sized generously so the None sentinel can always be inserted
# during teardown without being dropped.
_CTRL_QUEUE_CAP = 1_024

# Maximum number of distinct hosts tracked in per-host rate-limit structures.
# Oldest entries are evicted when the limit is reached (LRU via OrderedDict).
_HOST_GATE_MAX = 4_096

# Reconnect jitter fraction — delay is multiplied by U(0, _RECONNECT_JITTER).
_RECONNECT_JITTER = 0.25

# Cloudflare challenge hostname that requires connection pacing.
_CF_CHALLENGE_HOST = "challenges.cloudflare.com"


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
        self._conn_handlers: dict[str, TcpConnection] = {}
        self._pending_connects: dict[str, PendingConnectState] = {}
        self._udp_registry: dict[str, UdpFlow] = {}

        # WebSocket closed signal — set by _recv_loop and _send_loop finally
        # blocks.  Set BEFORE closing handlers so _handle_connect's
        # ws_closed_task fires promptly and unblocks any pending ack_future
        # waits.
        self._ws_closed: asyncio.Event = asyncio.Event()

        # Send queues — initialised per session in _run_session().
        # _send_ctrl_queue is unbounded so the None teardown sentinel can
        # always be inserted without being dropped.
        self._send_ctrl_queue: asyncio.Queue[tuple[str, bool] | None] | None = None
        self._send_data_queue: asyncio.Queue[tuple[str, bool] | None] | None = None

        # In-flight request tasks.
        self._request_tasks: set[asyncio.Task[None]] = set()

        # Bootstrap carry-over buffers.
        # _post_ready_buf: frames received after AGENT_READY but before
        #   _recv_loop takes over the WebSocket iterator.
        # _pre_ready_carry: partial (unterminated) frame fragment at the
        #   point AGENT_READY was seen; prepended to _recv_loop's buffer.
        self._post_ready_buf: list[str] = []
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
        self._pre_ack_buffer_cap_bytes = tun_cfg.pre_ack_buffer_cap_bytes
        self._connect_gate = asyncio.Semaphore(self._connect_max_pending)

        # LRU-bounded per-host structures to prevent unbounded memory growth.
        self._host_connect_gates: _LruDict[str, asyncio.Semaphore] = _LruDict(
            _HOST_GATE_MAX
        )
        self._host_connect_open_locks: _LruDict[str, asyncio.Lock] = _LruDict(
            _HOST_GATE_MAX
        )
        self._host_connect_last_open_at: _LruDict[str, float] = _LruDict(_HOST_GATE_MAX)
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
            "connect hardening: global=%d per_host=%d",
            self._connect_max_pending,
            self._connect_max_pending_per_host,
        )

        with span("tunnel.session"):
            while True:
                reconnect_reason: str | None = None
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
                        # Clean session — reset retry counter so the budget
                        # is per-consecutive-failure-run, not lifetime.
                        attempt = 0

                except BootstrapError:
                    metrics_inc("tunnel.bootstrap.error")
                    raise  # Never retried.

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
                    metrics_inc("tunnel.connect.error", error=type(exc).__name__)
                    reconnect_reason = str(exc) or type(exc).__name__

                except TimeoutError as exc:
                    metrics_inc("tunnel.connect.error", error="timeout")
                    reconnect_reason = f"timeout: {exc}"

                except Exception:
                    # Truly unexpected — propagate rather than silently
                    # swallowing and returning as if the session ended cleanly.
                    raise

                finally:
                    self._ws = None

                if reconnect_reason is None:
                    # _run_session returned cleanly with no error.
                    return

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
        self._post_ready_buf.clear()
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

        # _send_ctrl_queue is unbounded (maxsize=0) so the None teardown
        # sentinel inserted by _serve's finally block is never dropped even
        # when the queue is under load.
        self._send_ctrl_queue = asyncio.Queue(maxsize=0)
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
        if self._ws is None:
            raise BootstrapError(
                "Cannot bootstrap — WebSocket connection is not established.",
                error_code="bootstrap.no_connection",
                details={},
                hint="Ensure _run_session() sets self._ws before calling _bootstrap().",
            )
        ws = self._ws
        start = asyncio.get_running_loop().time()
        metrics_inc("tunnel.bootstrap.started")

        async def send_cmd(cmd: str) -> None:
            """Send one shell command over the WebSocket channel."""
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
            float(len(range(0, len(agent_b64), BOOTSTRAP_CHUNK_SIZE_CHARS))),
        )

        # Clean up any leftover files from a previous run.
        await send_cmd("rm -f '/tmp/exectunnel_agent.py' '/tmp/exectunnel_agent.b64'")
        await asyncio.sleep(BOOTSTRAP_RM_DELAY_SECS)

        # Upload in chunks — safe under all POSIX shell input-buffer limits.
        # URL-safe base64 alphabet ([A-Za-z0-9_-]) contains no shell
        # metacharacters so the printf argument is injection-safe.
        for i in range(0, len(agent_b64), BOOTSTRAP_CHUNK_SIZE_CHARS):
            chunk = agent_b64[i : i + BOOTSTRAP_CHUNK_SIZE_CHARS]
            await send_cmd(f"printf '%s' '{chunk}' >> '/tmp/exectunnel_agent.b64'")

        # sed converts URL-safe base64 (-_) to standard base64 (+/) before
        # decoding.  sed is POSIX-standard and available on all containers
        # including busybox, unlike `tr --` which uses a GNU extension.
        await send_cmd(
            "sed 's/-/+/g; s/_/\\//g' '/tmp/exectunnel_agent.b64'"
            " | base64 -d > '/tmp/exectunnel_agent.py'"
        )
        await asyncio.sleep(BOOTSTRAP_DECODE_DELAY_SECS)

        # Syntax-check before exec so decode corruption is caught cleanly.
        # SYNTAX_OK is consumed by _wait_ready as a positive signal.
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

        Lines received after ``AGENT_READY`` (but before ``_recv_loop`` takes
        over) are stored in ``_post_ready_buf`` for replay by ``_recv_loop``.

        Raises:
            AgentSyntaxError:          Remote Python reported a ``SyntaxError``.
            AgentVersionMismatchError: Remote agent version incompatible.
            BootstrapError:            WebSocket closed before ``AGENT_READY``.
        """
        buf = ""
        ready = False
        async for msg in ws:
            chunk = msg.decode() if isinstance(msg, bytes) else str(msg)
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                stripped = line.strip()
                if not ready:
                    if is_ready_frame(stripped):
                        ready = True
                        continue
                    if stripped == "SYNTAX_OK":
                        # Positive signal from the syntax-check command —
                        # log at DEBUG and continue waiting for AGENT_READY.
                        logger.debug("bootstrap: remote syntax check passed")
                        continue
                    if stripped:
                        self._bootstrap_diag.append(stripped)
                    # Detect SyntaxError by prefix; Traceback detection uses
                    # the full canonical prefix to avoid false positives.
                    if stripped.startswith("SyntaxError:") or stripped.startswith(
                        "Traceback (most recent call last):"
                    ):
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
                    # Post-ready frames buffered for _recv_loop replay.
                    self._post_ready_buf.append(stripped)
            if ready:
                # Remaining bytes are a partial (unterminated) frame fragment.
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

        dns_fwd: DnsForwarder | None = None
        if self._tun.dns_upstream:
            dns_fwd = DnsForwarder(
                self._tun.dns_local_port,
                self._tun.dns_upstream,
                self._ws_send,
                self._udp_registry,
                # Use the dedicated DNS inflight cap, not the WS send queue cap.
                max_inflight=self._app.bridge.dns_max_inflight,
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
            # _send_ctrl_queue is unbounded so put_nowait never raises QueueFull.
            if self._send_ctrl_queue is not None:
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
        """Return the minimum inter-connect interval for *host_key* in seconds."""
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
        if host_key == _CF_CHALLENGE_HOST and (
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

    # ── Failed connect cleanup ────────────────────────────────────────────────

    async def _cleanup_failed_connect(
        self,
        conn_id: str,
        handler: TcpConnection,
        reply: Reply,
    ) -> None:
        """Evict handler from registry and tear down cleanly.

        For handlers that were never started, delegates writer teardown to
        :meth:`~exectunnel.transport.tcp.TcpConnection.close_unstarted`
        which encapsulates the writer lifecycle without requiring private
        attribute access.

        For started handlers, :meth:`~exectunnel.transport.tcp.TcpConnection.close_remote`
        sets ``_remote_closed`` which causes ``_downstream`` to drain and
        exit, after which ``_on_task_done`` schedules ``_cleanup`` which
        closes the writer through the handler's own lifecycle.
        """
        self._conn_handlers.pop(conn_id, None)
        handler.close_remote()

        if not handler.is_started:
            with contextlib.suppress(OSError, RuntimeError):
                await handler.close_unstarted()

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
        handler = TcpConnection(
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
            # Pop from _pending_connects BEFORE cancelling ack_future so
            # _dispatch_frame_async cannot race to set_result on a cancelled
            # future and raise InvalidStateError.
            self._pending_connects.pop(conn_id, None)
            if not ack_future.done():
                ack_future.cancel()
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

        When a flow is closed by the agent (``drain_flow`` exits), the key is
        removed from ``active_flows``.  A subsequent datagram to the same
        destination creates a new flow — this is correct UDP semantics.
        """
        if req.udp_relay is None:
            logger.error(
                "UDP ASSOCIATE request has no relay — this is a bug in _negotiate()"
            )
            await req.send_reply_error(Reply.GENERAL_FAILURE)
            return

        relay = req.udp_relay
        udp_port = relay.local_port

        await req.send_reply_success(bind_host="127.0.0.1", bind_port=udp_port)
        logger.debug("UDP ASSOCIATE local port %d", udp_port)

        active_flows: dict[tuple[str, int], UdpFlow] = {}
        drain_tasks: set[asyncio.Task[None]] = set()

        async def drain_flow(
            handler: UdpFlow,
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
                raise  # Always propagate cancellation.
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

                if result is None:
                    break

                payload, dst_host, dst_port = result

                if is_host_excluded(dst_host, self._tun.exclude):
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
                    handler = UdpFlow(
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
            # Close all active flows — close() is a no-op if close_remote()
            # was already called (e.g. by _recv_loop on WS close).
            for h in list(active_flows.values()):
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
                    chunk = await src.read(PIPE_READ_CHUNK_BYTES)
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

        Starts by replaying frames buffered during ``_wait_ready`` (stored in
        ``_post_ready_buf``), then takes over the live WebSocket iterator.

        Close sequencing
        ----------------
        ``_ws_closed`` is set **before** closing handlers so that any
        coroutine blocked on ``_ws_closed.wait()`` (e.g. the ``ws_closed_task``
        in ``_handle_connect``) unblocks immediately and does not race with
        handler teardown.
        """
        if self._ws is None:
            return
        ws = self._ws
        buf = self._pre_ready_carry
        self._pre_ready_carry = ""

        for line in self._post_ready_buf:
            try:
                await self._dispatch_frame_async(line)
            except (FrameDecodingError, ProtocolError) as exc:
                metrics_inc("tunnel.frames.decode_error")
                logger.debug(
                    "bad frame in post-ready buffer [%s]: %s (error_id=%s) — skipping",
                    exc.error_code,
                    exc.message,
                    exc.error_id,
                )
        self._post_ready_buf.clear()

        try:
            async for msg in ws:
                chunk = msg.decode() if isinstance(msg, bytes) else str(msg)
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
            # Set _ws_closed FIRST so _handle_connect's ws_closed_task
            # unblocks before we start closing handlers — prevents a race
            # where _handle_connect blocks indefinitely on ack_future.
            self._ws_closed.set()

            for handler in list(self._conn_handlers.values()):
                handler.abort_upstream()
                handler.close_remote()
            for flow in list(self._udp_registry.values()):
                flow.on_remote_closed()

    async def _dispatch_frame_async(self, line: str) -> None:
        """Parse and dispatch one frame line.

        CONN_ACK fast path
        ------------------
        ``CONN_ACK`` is not in ``_VALID_MSG_TYPES`` so ``parse_frame`` would
        silently drop it.  It is intercepted here before ``parse_frame`` is
        called.  The ``conn_id`` is validated against ``ID_RE`` before lookup
        to prevent malformed frames from causing unexpected behaviour.

        DATA / UDP_DATA payloads are decoded with
        :func:`~exectunnel.protocol.frames.decode_data_payload` which handles
        ``urlsafe_b64encode`` with no-padding — consistent with the protocol
        package's encoding.

        Raises:
            FrameDecodingError: On base64 decode failure.
            ProtocolError:      On structural frame violations.
        """
        stripped = line.strip()

        # ── CONN_ACK fast path ────────────────────────────────────────────────
        if stripped.startswith(f"{FRAME_PREFIX}CONN_ACK:") and stripped.endswith(
            FRAME_SUFFIX
        ):
            inner = stripped[len(FRAME_PREFIX) : -len(FRAME_SUFFIX)]
            parts = inner.split(":", 1)
            if len(parts) == 2:
                conn_id = parts[1]
                # Validate conn_id before lookup to reject malformed frames.
                if ID_RE.match(conn_id):
                    pending = self._pending_connects.get(conn_id)
                    if pending is not None and not pending.ack_future.done():
                        pending.ack_future.set_result("ack")
                    metrics_inc("tunnel.frames.received", type="CONN_ACK")
            return

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
                if handler.is_closed:
                    return
                try:
                    handler.feed(data)
                except TransportError as exc:
                    if exc.error_code == "transport.pre_ack_buffer_overflow":
                        if not pending.ack_future.done():
                            pending.ack_future.set_result("pre_ack_overflow")
                        metrics_inc("tunnel.pre_ack_buffer.overflow")
                    # transport.inbound_queue_full: log and drop — do not signal ack_future
                    # since the connection is post-ACK and the queue being full is transient.
                    else:
                        metrics_inc(
                            "tunnel.inbound_queue.drop",
                            error=exc.error_code.replace(".", "_"),
                        )
                        logger.debug(
                            "conn %s: feed() dropped chunk [%s] — %s",
                            conn_id,
                            exc.error_code,
                            exc.message,
                            extra={"conn_id": conn_id},
                        )
            else:
                await handler.feed_async(data)
            return

        if msg_type in ("CONN_CLOSE", "ERROR"):
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
                handler.abort_upstream()
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
                flow.on_remote_closed()
            return

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

        Control frames (``control=True``) are enqueued into the unbounded
        control queue and are never dropped.  They are delivered even after
        ``_ws_closed`` is set so that teardown frames (``CONN_CLOSE``,
        ``UDP_CLOSE``) reach the agent.

        Data frames are dropped when the bounded data queue is full (unless
        ``must_queue=True``, which blocks until space is available).

        When ``must_queue=True`` and the WebSocket is closed, a
        ``ConnectionClosedError`` is raised so the upstream caller
        (``TcpConnection._upstream``) can handle the failure correctly
        rather than silently dropping the frame.

        ``control`` takes precedence over ``must_queue`` — if both are
        ``True``, the frame is treated as a control frame.
        """
        if self._send_ctrl_queue is None or self._send_data_queue is None:
            return

        if control:
            # Control frames are always enqueued — never dropped, never
            # gated on _ws_closed.  The unbounded ctrl queue guarantees
            # put_nowait never raises QueueFull.
            self._send_ctrl_queue.put_nowait((frame, False))
            return

        if must_queue:
            if self._ws_closed.is_set():
                # Raise ConnectionClosedError so the caller knows the frame
                # was not delivered — silent drop would cause data loss.
                raise ConnectionClosedError(
                    "WebSocket is closed — cannot enqueue data frame.",
                    error_code="transport.connection_closed",
                    details={"frame_prefix": frame[:40]},
                )
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
                return
            except TimeoutError:
                await self._ws_send(keepalive_frame, control=True)

    async def _send_loop(self) -> None:
        """Single writer to the WebSocket — serialises all outgoing frames.

        Prioritises control frames over data frames.  Exits cleanly on the
        ``None`` sentinel or when the WebSocket reports closed.

        Deferred data item
        ------------------
        When both queues fire simultaneously in ``asyncio.wait``, the control
        item is sent first and the data item is saved as ``deferred_data_item``
        for the next iteration.  This avoids re-queuing the data item (which
        would change its position relative to other queued items) and prevents
        it from being lost if the queue is full.

        Raises:
            WebSocketSendTimeoutError: Propagated to ``_serve`` to trigger reconnect.
            ConnectionClosedError:     Same.
        """
        if self._ws is None:
            return
        if self._send_ctrl_queue is None or self._send_data_queue is None:
            return

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
                        # One final ctrl_q check before sending deferred data.
                        try:
                            item = ctrl_q.get_nowait()
                        except asyncio.QueueEmpty:
                            item = deferred_data_item
                            deferred_data_item = None
                    else:
                        # Both queues empty — block on whichever fires first.
                        ctrl_wait: asyncio.Task[tuple[str, bool] | None] = (
                            asyncio.create_task(ctrl_q.get(), name="send-ctrl-wait")
                        )
                        data_wait: asyncio.Task[tuple[str, bool] | None] = (
                            asyncio.create_task(data_q.get(), name="send-data-wait")
                        )

                        try:
                            done, pending = await asyncio.wait(
                                {ctrl_wait, data_wait},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                        except asyncio.CancelledError:
                            ctrl_wait.cancel()
                            data_wait.cancel()
                            await asyncio.gather(
                                ctrl_wait, data_wait, return_exceptions=True
                            )
                            raise

                        # Cancel the loser and rescue its result if it
                        # completed before cancellation.
                        for pending_task in pending:
                            pending_task.cancel()
                            try:
                                await pending_task
                            except asyncio.CancelledError:
                                # Task cancelled before completing — check
                                # the queue directly to avoid losing an item
                                # that was enqueued but not dequeued.
                                if pending_task is data_wait:
                                    with contextlib.suppress(asyncio.QueueEmpty):
                                        rescued = data_q.get_nowait()
                                        if deferred_data_item is None:
                                            deferred_data_item = rescued
                            else:
                                # Task completed before cancel() took effect.
                                try:
                                    rescued = pending_task.result()
                                    if rescued is not None:
                                        if pending_task is data_wait:
                                            if deferred_data_item is None:
                                                deferred_data_item = rescued
                                        else:
                                            # Rescued ctrl item — re-queue at
                                            # front; ctrl queue is unbounded.
                                            ctrl_q.put_nowait(rescued)
                                except Exception:
                                    pass

                        if ctrl_wait in done:
                            item = ctrl_wait.result()
                            # If data also completed, save it as deferred.
                            if data_wait in done:
                                try:
                                    result = data_wait.result()
                                    if (
                                        result is not None
                                        and deferred_data_item is None
                                    ):
                                        deferred_data_item = result
                                except Exception:
                                    pass
                        else:
                            item = data_wait.result()

                if item is None:
                    return

                frame, _ = item
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
