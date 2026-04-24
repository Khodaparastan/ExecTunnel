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
import time
from typing import TYPE_CHECKING, Final

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

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
from ._dispatcher import RequestDispatcher
from ._receiver import FrameReceiver
from ._sender import KeepaliveLoop, WsSender
from ._state import PendingConnect
from ._types import AgentStatsCallable

if TYPE_CHECKING:
    from ._dns import DnsForwarder

__all__ = ["TunnelSession"]

logger = logging.getLogger(__name__)

_RECONNECT_JITTER: Final[float] = 0.25
"""Jitter factor applied to the reconnect back-off delay."""

_SAFE_HOST_RE: Final[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9_.\-]")
"""Compiled pattern for sanitising host strings used in asyncio task names."""


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

        logger.info(
            "connect hardening: global=%d per_host=%d",
            self._tun.connect_max_pending,
            self._tun.connect_max_pending_per_host,
        )

        async with aspan("session.run"):
            while True:
                reconnect_reason: str | None = None
                session_start = time.monotonic()
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
                    metrics_inc("session.connect.error", error=type(exc).__name__)
                    reconnect_reason = str(exc) or type(exc).__name__

                except TimeoutError as exc:
                    metrics_inc("session.connect.error", error="timeout")
                    reconnect_reason = f"timeout: {exc}"

                except Exception:
                    raise

                finally:
                    self._ws = None
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
        for conn in self._tcp_registry.values():
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
        self._tcp_registry.clear()

        pending_cleaned = len(self._pending_connects)
        for pending in self._pending_connects.values():
            if not pending.ack_future.done():
                pending.ack_future.cancel()
        self._pending_connects.clear()

        udp_cleaned = 0
        for flow in self._udp_registry.values():
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
        )

        socks_cfg = Socks5ServerConfig(
            host=self._tun.socks_host,
            port=self._tun.socks_port,
            handshake_timeout=self._tun.socks_handshake_timeout,
            request_queue_capacity=self._tun.socks_request_queue_cap,
            queue_put_timeout=self._tun.socks_queue_put_timeout,
            udp_relay_queue_capacity=self._tun.udp_relay_queue_cap,
            udp_drop_warn_interval=self._tun.udp_drop_warn_every,
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

                async with dns_fwd if dns_fwd is not None else contextlib.nullcontext():
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
                            done, _ = await asyncio.wait(
                                all_tasks,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            for task in done:
                                if task.cancelled():
                                    continue
                                exc = task.exception()
                                if exc is None:
                                    continue
                                metrics_inc("session.task.error", task=task.get_name())
                                if isinstance(exc, ExecTunnelError):
                                    logger.error(
                                        "task %s failed [%s]: %s (error_id=%s)",
                                        task.get_name(),
                                        exc.error_code,
                                        exc.message,
                                        exc.error_id,
                                        extra=exc.to_dict(),
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
                                if first_exc is None:
                                    first_exc = exc

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
                extra=exc.to_dict(),
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
