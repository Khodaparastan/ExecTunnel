"""TunnelSession — core session orchestration.

Bootstraps ``agent.py`` into the pod and runs a local SOCKS5 proxy that
routes all connections through the WebSocket exec tunnel.

Architecture
------------
:class:`TunnelSession` wires together five sub-components:

* :class:`~exectunnel.session._bootstrap.AgentBootstrapper`  — uploads and starts the agent script.
* :class:`~exectunnel.session._sender.WsSender`              — concurrency-safe WebSocket frame sender.
* :class:`~exectunnel.session._sender.KeepaliveLoop`         — sends heartbeat frames to prevent idle timeouts.
* :class:`~exectunnel.session._receiver.FrameReceiver`       — reads inbound frames and dispatches to handlers.
* :class:`~exectunnel.session._dispatcher.RequestDispatcher` — handles CONNECT and UDP_ASSOCIATE requests.

Each sub-component is constructed fresh per session (per :meth:`TunnelSession._start_session`
call) so reconnects start with clean state.
"""

import asyncio
import contextlib
import logging
import random
import re
import socket
import ssl
import time
from typing import TYPE_CHECKING, Final

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from exectunnel.defaults import Defaults
from exectunnel.exceptions import (
    BootstrapError,
    ConnectionClosedError,
    ExecTunnelError,
    ReconnectExhaustedError,
    TransportError,
    WebSocketSendTimeoutError,
)
from exectunnel.observability import (
    aspan,
    metrics_gauge_set,
    metrics_inc,
    metrics_observe,
)
from exectunnel.proxy import Socks5Server, Socks5ServerConfig, TCPRelay
from exectunnel.transport import TcpConnection, UdpFlow

from ._bootstrap import AgentBootstrapper
from ._config import SessionConfig, TunnelConfig
from ._constants import PIPE_WRITER_CLOSE_TIMEOUT_SECS
from ._dispatcher import RequestDispatcher
from ._receiver import FrameReceiver
from ._sender import KeepaliveLoop, WsSender
from ._state import PendingConnect
from ._types import AgentStatsCallable


class _AgentActivity:
    """Tracks the timestamp of the most recent inbound frame from the agent.

    Used by :class:`~exectunnel.session._dispatcher.RequestDispatcher` to
    suppress forced reconnects driven by ACK timeouts when the agent is
    still actively pushing data — that pattern is tunnel congestion, not
    a wedged agent.
    """

    __slots__ = ("_last_rx_at",)

    def __init__(self) -> None:
        self._last_rx_at: float = 0.0

    def mark_rx(self) -> None:
        """Record that an inbound chunk has just been received."""
        self._last_rx_at = asyncio.get_running_loop().time()

    def recently_active(self, window_secs: float) -> bool:
        """Return ``True`` if a frame was received within *window_secs*."""
        if self._last_rx_at <= 0.0:
            return False
        return asyncio.get_running_loop().time() - self._last_rx_at <= window_secs


if TYPE_CHECKING:
    from ._dns import DnsForwarder

__all__ = ["TunnelSession"]

logger = logging.getLogger(__name__)

_RECONNECT_JITTER: Final[float] = 0.25
"""Jitter factor applied to the reconnect back-off delay."""

_SAFE_HOST_RE: Final[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9_.\-]")
"""Compiled pattern for sanitising host strings used in asyncio task names."""


def _is_port_available(host: str, port: int) -> bool:
    """Return ``True`` if *host*:*port* can be bound right now.

    Uses a non-blocking probe socket so the check is instantaneous and
    leaves no lingering listener.

    Args:
        host: Bind address to probe (e.g. ``"127.0.0.1"`` or ``"::1"``).
        port: TCP port number to probe.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _reconnect_reason_tag(reason: str) -> str:
    """Extract a stable metric-safe tag from a reconnect reason string.

    Args:
        reason: Free-form reconnect reason string.

    Returns:
        The portion before the first colon, stripped, lowercased, with dots
        replaced by underscores.  Falls back to ``"unknown"`` if empty.
    """
    tag = reason.split(":", 1)[0].strip().lower().replace(".", "_")
    return tag or "unknown"


class TunnelSession:
    """Bootstraps ``agent.py`` into the pod and runs a local SOCKS5 proxy.

    Args:
        session_cfg: Session-level configuration (WebSocket, reconnect, transport).
        tun_cfg:     Tunnel-level configuration (SOCKS, bootstrap, UDP, DNS).

    Raises:
        AgentReadyTimeoutError:    Agent did not emit ``AGENT_READY`` in time.
        AgentSyntaxError:          Agent script failed to parse on the remote.
        AgentVersionMismatchError: Remote agent reports an incompatible version.
        BootstrapError:            Any other bootstrap failure.
        ReconnectExhaustedError:   All reconnect attempts exhausted.
    """

    __slots__ = (
        "_cfg",
        "_tun",
        "_ws",
        "_tcp_registry",
        "_pending_connects",
        "_udp_registry",
        "_ws_closed",
        "_request_tasks",
        "_sender",
        "_dispatcher",
        "_on_agent_stats",
        "_agent_activity",
    )

    def __init__(self, session_cfg: SessionConfig, tun_cfg: TunnelConfig) -> None:
        self._cfg = session_cfg
        self._tun = tun_cfg
        self._ws: ClientConnection | None = None
        self._on_agent_stats: AgentStatsCallable | None = None

        self._tcp_registry: dict[str, TcpConnection] = {}
        self._pending_connects: dict[str, PendingConnect] = {}
        self._udp_registry: dict[str, UdpFlow] = {}

        self._ws_closed: asyncio.Event = asyncio.Event()
        self._request_tasks: set[asyncio.Task[None]] = set()

        self._sender: WsSender | None = None
        self._dispatcher: RequestDispatcher | None = None
        self._agent_activity: _AgentActivity = _AgentActivity()

    def set_agent_stats_listener(self, callback: AgentStatsCallable | None) -> None:
        """Register or clear a listener for agent-emitted STATS snapshots.

        STATS frames are periodic observability snapshots (queue depth,
        byte/frame counters, dispatch latency percentiles, worker counts)
        emitted by the agent roughly once per second.  Production sessions
        leave this unset so STATS traffic is a near-free no-op; the
        measurement framework registers a listener that aggregates snapshots
        into benchmark reports.

        The listener takes effect on the **next** WebSocket (re)connect,
        because :class:`~exectunnel.session._receiver.FrameReceiver` captures
        the callback by value at construction time.

        Args:
            callback: A callable taking one decoded snapshot ``dict``, or
                      ``None`` to disable the listener.  Must not block or
                      raise — exceptions are logged and suppressed.
        """
        self._on_agent_stats = callback

    # ── Top-level run ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect, bootstrap the agent, and serve SOCKS5 requests.

        Retries on transport/session interruptions using reconnect settings
        from :class:`~exectunnel.session._config.SessionConfig`.  Bootstrap
        failures are always fatal and are never retried.

        Raises:
            BootstrapError:          Propagated immediately; never retried.
            ReconnectExhaustedError: All reconnect attempts consumed.
        """
        ssl_ctx = self._cfg.ssl_context()
        retries = self._cfg.reconnect_max_retries
        base_delay = self._cfg.reconnect_base_delay
        max_delay = self._cfg.reconnect_max_delay
        attempt = 0
        async with aspan("session.run"):
            while True:
                reconnect_reason: str | None = None
                session_start = time.monotonic()

                # Pre-flight: verify the SOCKS port is available before
                # spending time on WebSocket handshake + agent bootstrap.
                # A port conflict is a local configuration problem — fail
                # immediately with a clear message instead of connecting,
                # bootstrapping, and then failing deep inside _run_tasks.
                socks_host = self._tun.socks_host
                socks_port = self._tun.socks_port
                if not _is_port_available(socks_host, socks_port):
                    raise TransportError(
                        f"SOCKS5 port {socks_host}:{socks_port} is already in use.",
                        details={"host": socks_host, "port": socks_port},
                        hint=(
                            f"Ensure no other process is listening on "
                            f"{socks_host}:{socks_port} before starting the tunnel."
                        ),
                        retryable=False,
                    )

                try:
                    metrics_inc("session.connect.attempt")
                    async with connect(
                        self._cfg.wss_url,
                        ssl=ssl_ctx,
                        additional_headers=self._cfg.ws_headers or None,
                        ping_interval=None,
                        max_size=None,
                    ) as ws:
                        await self._start_session(ws)
                        attempt = 0

                except BootstrapError:
                    metrics_inc("session.bootstrap.error")
                    raise

                except asyncio.CancelledError:
                    logger.info("tunnel session cancelled")
                    raise

                except WebSocketSendTimeoutError as exc:
                    metrics_inc("session.connect.error", error="ws_send_timeout")
                    reconnect_reason = (
                        f"ws_send_timeout: {exc.message} (error_id={exc.error_id})"
                    )
                    logger.warning(
                        "WebSocket send timed out [%s] (error_id=%s) — will reconnect",
                        exc.error_code,
                        exc.error_id,
                    )

                except ConnectionClosedError as exc:
                    metrics_inc("session.connect.error", error="connection_closed")
                    reconnect_reason = (
                        f"connection_closed: "
                        f"{exc.details.get('close_reason', '')} "
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
                        "session.connect.error",
                        error=exc.error_code.replace(".", "_"),
                    )
                    if exc.retryable is False:
                        # Non-retryable transport errors (e.g. port already in
                        # use) are configuration problems — propagate immediately.
                        raise
                    reconnect_reason = (
                        f"{exc.error_code}: {exc.message} (error_id={exc.error_id})"
                    )
                    logger.warning(
                        "Transport error [%s]: %s (error_id=%s) — will reconnect",
                        exc.error_code,
                        exc.message,
                        exc.error_id,
                    )

                except InvalidStatus as exc:
                    status = exc.response.status_code
                    metrics_inc("session.connect.error", error=f"http_{status}")
                    if status in (401, 403):
                        # Authentication/authorisation failures are permanent —
                        # retrying will not help; surface as a fatal error.
                        raise BootstrapError(
                            f"WebSocket connection rejected: HTTP {status}.",
                            details={"http_status": status},
                            hint=(
                                "Verify that the token in your config is valid "
                                "and has not expired."
                            ),
                        ) from exc
                    # Other HTTP errors (e.g. 502, 503) — reconnect.
                    reconnect_reason = (
                        f"http_{status}: server rejected WebSocket (HTTP {status})"
                    )
                    logger.warning(
                        "WebSocket handshake rejected (HTTP %d) — will reconnect",
                        status,
                    )

                except ssl.SSLError as exc:
                    metrics_inc("session.connect.error", error="ssl_error")
                    reconnect_reason = f"ssl_error: {exc.reason or exc}"
                    logger.warning(
                        "SSL error connecting to WebSocket (%s) — will reconnect",
                        exc.reason or exc,
                    )

                except (OSError, ConnectionClosed) as exc:
                    metrics_inc("session.connect.error", error=type(exc).__name__)
                    reconnect_reason = str(exc) or type(exc).__name__

                except TimeoutError as exc:
                    metrics_inc("session.connect.error", error="timeout")
                    reconnect_reason = f"timeout: {exc}"

                except Exception:
                    raise

                finally:
                    self._ws = None
                    with contextlib.suppress(Exception):
                        await self._clear_session_state()
                    metrics_observe(
                        "session.duration_sec", time.monotonic() - session_start
                    )
                    self._zero_gauges()

                if reconnect_reason is None:
                    return

                if attempt >= retries:
                    metrics_inc("session.reconnect.exhausted")
                    raise ReconnectExhaustedError(
                        f"WebSocket session terminated after {retries} "
                        "reconnect attempts.",
                        details={"attempts": retries, "last_error": reconnect_reason},
                        hint=(
                            "Check network connectivity to the tunnel endpoint "
                            "and increase EXECTUNNEL_RECONNECT_MAX_RETRIES if "
                            "transient disruptions are expected."
                        ),
                    )

                delay = min(base_delay * (2**attempt), max_delay)
                delay += random.uniform(0, delay * _RECONNECT_JITTER)
                attempt += 1
                metrics_inc(
                    "session.reconnect",
                    reason=_reconnect_reason_tag(reconnect_reason),
                )
                metrics_observe("session.reconnect.delay_sec", delay)
                logger.warning(
                    "WebSocket disconnected (%s), reconnecting in %.1fs "
                    "(attempt %d/%d)",
                    reconnect_reason,
                    delay,
                    attempt,
                    retries,
                )
                await asyncio.sleep(delay)

    # ── Gauge management ──────────────────────────────────────────────────────

    def _emit_registry_gauges(self) -> None:
        """Publish current sizes of all session registries as gauge metrics."""
        metrics_gauge_set("session.registry.tcp", float(len(self._tcp_registry)))
        metrics_gauge_set(
            "session.registry.pending_connects",
            float(len(self._pending_connects)),
        )
        metrics_gauge_set("session.registry.udp", float(len(self._udp_registry)))
        metrics_gauge_set("session.request_tasks", float(len(self._request_tasks)))

    @staticmethod
    def _zero_gauges() -> None:
        """Zero all session-level gauges on session teardown."""
        for name in (
            "session.registry.tcp",
            "session.registry.pending_connects",
            "session.registry.udp",
            "session.request_tasks",
            "session.active.tcp_connections",
            "session.active.udp_flows",
            "session.pending_connects",
            "session.send.queue.data",
            "session.send.queue.ctrl",
        ):
            metrics_gauge_set(name, 0.0)

    # ── Session initialisation ────────────────────────────────────────────────

    async def _request_reconnect(self, reason: str) -> None:
        """Force-close the current WebSocket so the reconnect loop proceeds.

        Args:
            reason: Short human-readable reason string logged and sent as the
                    WebSocket close reason (truncated to 120 characters).
        """
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.close(
                code=Defaults.WS_CLOSE_CODE_UNHEALTHY,
                reason=reason[:120],
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "failed to close websocket during forced reconnect request",
                exc_info=True,
            )

    async def _clear_session_state(self) -> None:
        """Abort all per-session state before a new session starts.

        Aborts lingering TCP connections, cancels pending connect futures,
        and closes UDP flows from a previous session to prevent resource
        leaks on reconnect.
        """
        if self._dispatcher is not None:
            self._dispatcher.close()
            self._dispatcher = None

        if self._sender is not None:
            with contextlib.suppress(Exception):
                await self._sender.stop()
            self._sender = None

        for task in list(self._request_tasks):
            if not task.done():
                task.cancel()
        if self._request_tasks:
            await asyncio.gather(*self._request_tasks, return_exceptions=True)
        self._request_tasks.clear()

        tcp_cleaned = 0
        tcp_snapshot = list(self._tcp_registry.values())
        for conn in tcp_snapshot:
            try:
                if conn.is_started:
                    conn.abort()
                else:
                    await conn.close_unstarted()
                tcp_cleaned += 1
            except Exception:  # noqa: BLE001
                logger.debug(
                    "error aborting stale tcp connection during session reset",
                    exc_info=True,
                )

        for conn in tcp_snapshot:
            if not conn.is_started or conn.is_closed:
                continue
            with contextlib.suppress(TimeoutError, OSError, RuntimeError):
                async with asyncio.timeout(PIPE_WRITER_CLOSE_TIMEOUT_SECS):
                    await conn.closed_event.wait()

        self._tcp_registry.clear()

        pending_cleaned = len(self._pending_connects)
        for pending in list(self._pending_connects.values()):
            if not pending.ack_future.done():
                pending.ack_future.cancel()
        self._pending_connects.clear()

        udp_cleaned = 0
        for flow in list(self._udp_registry.values()):
            try:
                flow.on_remote_closed()
                udp_cleaned += 1
            except Exception:  # noqa: BLE001
                logger.debug(
                    "error closing stale udp flow during session reset",
                    exc_info=True,
                )
        self._udp_registry.clear()

        if tcp_cleaned or pending_cleaned or udp_cleaned:
            metrics_inc("session.cleanup.tcp", value=tcp_cleaned)
            metrics_inc("session.cleanup.pending", value=pending_cleaned)
            metrics_inc("session.cleanup.udp", value=udp_cleaned)
            logger.info(
                "session reset: cleaned %d TCP, %d pending, %d UDP",
                tcp_cleaned,
                pending_cleaned,
                udp_cleaned,
            )

        self._emit_registry_gauges()

    async def _start_session(self, ws: ClientConnection) -> None:
        """Initialise per-session state, bootstrap the agent, and run tasks.

        Args:
            ws: Live WebSocket connection to the pod exec channel.
        """
        async with aspan("session.start"):
            metrics_inc("session.connect.ok")
            self._ws = ws

            self._ws_closed = asyncio.Event()

            await self._clear_session_state()

            async with aspan("session.bootstrap"):
                bootstrapper = AgentBootstrapper(ws, self._cfg, self._tun)
                await bootstrapper.run()

            # Reset the activity tracker on every (re)connect so a stale
            # timestamp from a previous session can never suppress an early
            # health-reconnect on the new tunnel.
            self._agent_activity = _AgentActivity()

            self._sender = WsSender(ws, self._cfg, self._ws_closed)
            self._dispatcher = RequestDispatcher(
                tun_cfg=self._tun,
                ws_send=self._sender.send,
                ws_closed=self._ws_closed,
                tcp_registry=self._tcp_registry,
                pending_connects=self._pending_connects,
                udp_registry=self._udp_registry,
                pre_ack_buffer_cap_bytes=self._tun.pre_ack_buffer_cap_bytes,
                request_reconnect=self._request_reconnect,
                agent_recently_active=lambda: self._agent_activity.recently_active(
                    Defaults.ACK_HEALTH_ACTIVITY_GRACE_SECS
                ),
            )

            self._dispatcher.reset_ack_state()
            self._sender.start()
            await self._run_tasks(
                ws,
                bootstrapper.post_ready_lines,
                bootstrapper.pre_ready_carry,
            )

    # ── Task orchestration ────────────────────────────────────────────────────

    async def _run_tasks(
        self,
        ws: ClientConnection,
        post_ready_lines: list[str],
        pre_ready_carry: str,
    ) -> None:
        """Start all session tasks and wait for the first to fail or finish.

        The ``first_exc`` is captured inside the ``async with`` block and
        re-raised after the SOCKS5 server context manager exits, ensuring the
        server is fully stopped before the exception propagates to the
        reconnect loop.

        Args:
            ws:               Live WebSocket connection.
            post_ready_lines: Lines buffered during bootstrap for replay.
            pre_ready_carry:  Partial frame fragment from bootstrap.

        Raises:
            RuntimeError: If called before :meth:`_start_session` has
                          initialised ``_sender`` and ``_dispatcher``.
        """
        if self._sender is None:
            raise RuntimeError(
                "_run_tasks called before _sender was initialised — "
                "call _start_session() instead."
            )
        if self._dispatcher is None:
            raise RuntimeError(
                "_run_tasks called before _dispatcher was initialised — "
                "call _start_session() instead."
            )

        metrics_inc("session.serve.started")

        receiver = FrameReceiver(
            ws=ws,
            ws_closed=self._ws_closed,
            tcp_registry=self._tcp_registry,
            pending_connects=self._pending_connects,
            udp_registry=self._udp_registry,
            post_ready_lines=post_ready_lines,
            pre_ready_carry=pre_ready_carry,
            # Pass the sender so the receiver can emit ERROR frames on
            # per-connection inbound saturation without stalling the WS
            # dispatcher for other muxed connections (head-of-line guard).
            ws_send=self._sender.send,
            on_agent_stats=self._on_agent_stats,
            mark_agent_rx=self._agent_activity.mark_rx,
        )

        socks_cfg = Socks5ServerConfig(
            host=self._tun.socks_host,
            port=self._tun.socks_port,
            allow_non_loopback=self._tun.socks_allow_non_loopback,
            handshake_timeout=self._tun.socks_handshake_timeout,
            request_queue_capacity=self._tun.socks_request_queue_cap,
            queue_put_timeout=self._tun.socks_queue_put_timeout,
            udp_relay_queue_capacity=self._tun.udp_relay_queue_cap,
            udp_drop_warn_interval=self._tun.udp_drop_warn_every,
            udp_associate_enabled=self._tun.socks_udp_associate_enabled,
            udp_bind_host=self._tun.socks_udp_bind_host,
            udp_advertise_host=self._tun.socks_udp_advertise_host,
        )

        first_exc: BaseException | None = None

        try:
            async with Socks5Server(socks_cfg) as socks:
                dns_fwd: DnsForwarder | None = None
                if self._tun.dns_upstream:
                    from ._dns import DnsForwarder  # noqa: PLC0415

                    dns_fwd = DnsForwarder(
                        self._tun.dns_local_port,
                        self._tun.dns_upstream,
                        self._sender.send,
                        self._udp_registry,
                        max_inflight=self._tun.dns_max_inflight,
                        upstream_port=self._tun.dns_upstream_port,
                        query_timeout=self._tun.dns_query_timeout,
                    )

                # ``DnsForwarder`` is an async context manager; ``nullcontext()``
                # is typed as a *sync* ``AbstractContextManager`` in typeshed
                # (despite supporting ``async with`` at runtime since 3.10), so
                # the conditional union does not satisfy
                # ``AbstractAsyncContextManager``.  Use ``AsyncExitStack`` —
                # the stdlib's canonical "optionally enter an async CM"
                # pattern — which is strictly-typed and avoids the union.
                async with contextlib.AsyncExitStack() as dns_stack:
                    if dns_fwd is not None:
                        await dns_stack.enter_async_context(dns_fwd)
                    recv_task = asyncio.create_task(
                        receiver.run(), name="tun-recv-loop"
                    )
                    send_task = self._sender.task
                    if send_task is None:
                        raise RuntimeError(
                            "WsSender.task is None after start() — this is a bug."
                        )
                    socks_task = asyncio.create_task(
                        self._accept_loop(socks), name="tun-socks-accept"
                    )
                    keepalive = KeepaliveLoop(
                        sender=self._sender,
                        ws_closed=self._ws_closed,
                        interval=float(self._cfg.ping_interval),
                    )
                    keepalive_task = asyncio.create_task(
                        keepalive.run(), name="tun-keepalive"
                    )

                    all_tasks = {recv_task, send_task, socks_task, keepalive_task}

                    try:
                        async with aspan("session.serve"):
                            pending_tasks = set(all_tasks)

                            while pending_tasks and first_exc is None:
                                done, pending_tasks = await asyncio.wait(
                                    pending_tasks,
                                    return_when=asyncio.FIRST_COMPLETED,
                                )

                                for task in done:
                                    if task.cancelled():
                                        continue

                                    exc = task.exception()

                                    if exc is None:
                                        if (
                                            task is keepalive_task
                                            and self._ws_closed.is_set()
                                        ):
                                            # Teardown noise. Keep waiting for recv/send,
                                            # which should report the primary close cause.
                                            continue

                                        if (
                                            task is send_task
                                            and self._ws_closed.is_set()
                                        ):
                                            # Sender can exit cleanly after receiver has
                                            # declared the WebSocket closed. Keep waiting
                                            # for receiver unless no tasks remain.
                                            continue

                                        if task is recv_task:
                                            exc = ConnectionClosedError(
                                                "WebSocket receive task ended.",
                                                details={
                                                    "close_code": 0,
                                                    "close_reason": "recv_task_ended",
                                                },
                                            )
                                        elif task is send_task:
                                            exc = ConnectionClosedError(
                                                "WebSocket send task ended unexpectedly.",
                                                details={
                                                    "close_code": 0,
                                                    "close_reason": "send_task_ended",
                                                },
                                            )
                                        elif task is socks_task:
                                            exc = TransportError(
                                                "SOCKS5 server stopped unexpectedly.",
                                                error_code="transport.socks_server_stopped",
                                                details={
                                                    "host": self._tun.socks_host,
                                                    "port": self._tun.socks_port,
                                                },
                                                hint=(
                                                    "Check local listener lifecycle and "
                                                    "port ownership."
                                                ),
                                                retryable=False,
                                            )
                                        else:
                                            continue

                                    metrics_inc(
                                        "session.task.error", task=task.get_name()
                                    )

                                    if isinstance(exc, ExecTunnelError):
                                        logger.error(
                                            "task %s failed [%s]: %s (error_id=%s)",
                                            task.get_name(),
                                            exc.error_code,
                                            exc.message,
                                            exc.error_id,
                                            extra={"error": exc.to_dict()},
                                        )
                                    else:
                                        logger.error(
                                            "task %s failed: %s", task.get_name(), exc
                                        )

                                    logger.debug(
                                        "task %s traceback",
                                        task.get_name(),
                                        exc_info=(type(exc), exc, exc.__traceback__),
                                    )

                                    first_exc = exc
                                    break

                            if first_exc is None and self._ws_closed.is_set():
                                first_exc = ConnectionClosedError(
                                    "WebSocket closed but no primary task reported an error.",
                                    details={
                                        "close_code": 0,
                                        "close_reason": "ws_closed_without_primary_error",
                                    },
                                )

                    finally:
                        for task in all_tasks:
                            if not task.done():
                                task.cancel()

                        await self._sender.stop()

                        await asyncio.gather(*all_tasks, return_exceptions=True)

                        for task in list(self._request_tasks):
                            if not task.done():
                                task.cancel()
                        if self._request_tasks:
                            await asyncio.gather(
                                *self._request_tasks, return_exceptions=True
                            )

                        if self._dispatcher is not None:
                            self._dispatcher.close()
                            self._dispatcher = None
                        self._sender = None

                        metrics_inc("session.serve.stopped")

        except BaseException as socks_exit_exc:
            if first_exc is not None and socks_exit_exc is not first_exc:
                logger.error(
                    "Socks5Server cleanup raised %s while handling session "
                    "exception — original exception takes precedence: %s",
                    socks_exit_exc,
                    first_exc,
                )
                raise first_exc from socks_exit_exc
            raise

        if first_exc is not None:
            raise first_exc

    async def _accept_loop(self, socks: Socks5Server) -> None:
        """Accept SOCKS5 requests and dispatch each as an independent task.

        Args:
            socks: Running SOCKS5 server to iterate.

        Raises:
            RuntimeError: If called before :meth:`_start_session` has
                          initialised ``_dispatcher``.
        """
        if self._dispatcher is None:
            raise RuntimeError(
                "_accept_loop called before _dispatcher was initialised."
            )
        async for req in socks:
            safe_host = _SAFE_HOST_RE.sub("_", req.host)[:40]
            task = asyncio.create_task(
                self._handle_request(req),
                name=f"req-{req.cmd.name}-{safe_host}_{req.port}",
            )
            self._request_tasks.add(task)
            task.add_done_callback(self._on_request_task_done)
            self._emit_registry_gauges()

        logger.debug("socks5 accept loop ended")

    def _on_request_task_done(self, task: asyncio.Task[None]) -> None:
        """Callback invoked when a request task completes.

        Args:
            task: The completed request task.
        """
        self._request_tasks.discard(task)
        self._emit_registry_gauges()

        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return

        metrics_inc("session.request.error", error=type(exc).__name__)
        if isinstance(exc, ExecTunnelError):
            logger.error(
                "request task %s failed [%s]: %s (error_id=%s)",
                task.get_name(),
                exc.error_code,
                exc.message,
                exc.error_id,
                extra={"error": exc.to_dict()},
            )
        elif isinstance(exc, OSError):
            logger.warning("request task %s failed: %s", task.get_name(), exc)
        else:
            logger.error("request task %s failed: %s", task.get_name(), exc)
        logger.debug(
            "request task %s traceback",
            task.get_name(),
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    async def _handle_request(self, req: TCPRelay) -> None:
        """Dispatch a single SOCKS5 request to the appropriate handler.

        Args:
            req: Completed SOCKS5 handshake to dispatch.

        Raises:
            RuntimeError: If called before :meth:`_start_session` has
                          initialised ``_dispatcher``.
        """
        if self._dispatcher is None:
            raise RuntimeError(
                "_handle_request called before _dispatcher was initialised."
            )
        await self._dispatcher.dispatch(req)
