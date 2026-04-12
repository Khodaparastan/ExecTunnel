"""TunnelSession — core session orchestration.

Bootstraps ``agent.py`` into the pod and runs a local SOCKS5 proxy that
routes all connections through the WebSocket exec tunnel.

Architecture
------------
``TunnelSession`` wires together five sub-components:

* ``AgentBootstrapper``  — uploads and starts the agent script.
* ``WsSender``           — concurrency-safe WebSocket frame sender.
* ``KeepaliveLoop``      — sends heartbeat frames to prevent idle timeouts.
* ``FrameReceiver``      — reads inbound frames and dispatches to handlers.
* ``RequestDispatcher``  — handles CONNECT and UDP_ASSOCIATE requests.

Each sub-component is constructed fresh per session (per ``_start_session``
call) so reconnects start with clean state.
"""

import asyncio
import contextlib
import logging
import random
import re
import time
from typing import TYPE_CHECKING

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from exectunnel.config.settings import AppConfig, TunnelConfig
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
from exectunnel.proxy import Socks5Request, Socks5Server, Socks5ServerConfig
from exectunnel.transport import TcpConnection, UdpFlow

from ._bootstrap import AgentBootstrapper
from ._handlers import RequestDispatcher
from ._recv_loop import FrameReceiver
from ._state import PendingConnect
from ._ws_sender import KeepaliveLoop, WsSender

if TYPE_CHECKING:
    from ._dns import DnsForwarder

__all__ = ["TunnelSession"]

logger = logging.getLogger(__name__)

# Reconnect jitter fraction — delay is multiplied by U(0, _RECONNECT_JITTER).
_RECONNECT_JITTER: float = 0.25


def _reconnect_reason_tag(reason: str) -> str:
    """Extract a stable, short tag from a reconnect reason string.

    Returns the portion before the first colon, stripped and lowercased,
    with dots replaced by underscores for metric-tag safety.
    """
    tag = reason.split(":", 1)[0].strip().lower().replace(".", "_")
    return tag or "unknown"


class TunnelSession:
    """Bootstraps ``agent.py`` into the pod and runs a local SOCKS5 proxy.

    Args:
        app_cfg: Application-level configuration.
        tun_cfg: Tunnel-level configuration.

    Raises:
        AgentReadyTimeoutError:    Agent did not emit ``AGENT_READY`` in time.
        AgentSyntaxError:          Agent script failed to parse on the remote.
        AgentVersionMismatchError: Remote agent reports an incompatible version.
        BootstrapError:            Any other bootstrap failure.
        ReconnectExhaustedError:   All reconnect attempts exhausted.
    """

    __slots__ = (
        "_app",
        "_tun",
        "_ws",
        # Per-session handler registries — cleared on each reconnect.
        "_tcp_registry",
        "_pending_connects",
        "_udp_registry",
        # WebSocket closed signal — recreated on each session.
        "_ws_closed",
        # In-flight request tasks.
        "_request_tasks",
        # Sub-components — constructed per session in _start_session().
        "_sender",
        "_dispatcher",
    )

    def __init__(self, app_cfg: AppConfig, tun_cfg: TunnelConfig) -> None:
        self._app = app_cfg
        self._tun = tun_cfg
        self._ws: ClientConnection | None = None

        self._tcp_registry: dict[str, TcpConnection] = {}
        self._pending_connects: dict[str, PendingConnect] = {}
        self._udp_registry: dict[str, UdpFlow] = {}

        self._ws_closed: asyncio.Event = asyncio.Event()
        self._request_tasks: set[asyncio.Task[None]] = set()

        # Populated in _start_session — typed here for IDE support.
        self._sender: WsSender | None = None
        self._dispatcher: RequestDispatcher | None = None

    # ── Top-level run ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect, bootstrap the agent, and serve.

        Retries on transport/session interruptions using reconnect settings
        from ``AppConfig``.  Bootstrap failures are always fatal.

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
                        self._app.wss_url,
                        ssl=ssl_ctx,
                        additional_headers=self._app.ws_headers or None,
                        # Disable built-in ping — we implement our own via
                        # KeepaliveLoop which serialises through WsSender.
                        ping_interval=None,
                        max_size=None,
                    ) as ws:
                        await self._start_session(ws)
                        attempt = 0  # Clean session — reset retry counter.

                except BootstrapError:
                    metrics_inc("session.bootstrap.error")
                    raise  # Never retried.

                except asyncio.CancelledError:
                    logger.info("tunnel session cancelled")
                    raise

                except WebSocketSendTimeoutError as exc:
                    metrics_inc(
                        "session.connect.error", error="ws_send_timeout",
                    )
                    reconnect_reason = (
                        f"ws_send_timeout: {exc.message} "
                        f"(error_id={exc.error_id})"
                    )
                    logger.warning(
                        "WebSocket send timed out [%s] (error_id=%s) "
                        "— will reconnect",
                        exc.error_code,
                        exc.error_id,
                    )

                except ConnectionClosedError as exc:
                    metrics_inc(
                        "session.connect.error", error="connection_closed",
                    )
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
                        f"{exc.error_code}: {exc.message} "
                        f"(error_id={exc.error_id})"
                    )
                    logger.warning(
                        "Transport error [%s]: %s (error_id=%s) "
                        "— will reconnect",
                        exc.error_code,
                        exc.message,
                        exc.error_id,
                    )

                except (OSError, ConnectionClosed) as exc:
                    metrics_inc(
                        "session.connect.error",
                        error=type(exc).__name__,
                    )
                    reconnect_reason = str(exc) or type(exc).__name__

                except TimeoutError as exc:
                    metrics_inc(
                        "session.connect.error", error="timeout",
                    )
                    reconnect_reason = f"timeout: {exc}"

                except Exception:
                    raise  # Truly unexpected — propagate.

                finally:
                    self._ws = None
                    session_duration = time.monotonic() - session_start
                    metrics_observe(
                        "session.duration_sec", session_duration,
                    )
                    # Zero out all session-level gauges on exit.
                    self._zero_gauges()

                if reconnect_reason is None:
                    return  # Clean exit.

                if attempt >= retries:
                    metrics_inc("session.reconnect.exhausted")
                    raise ReconnectExhaustedError(
                        f"WebSocket session terminated after {retries} "
                        "reconnect attempts.",
                        details={
                            "attempts": retries,
                            "last_error": reconnect_reason,
                        },
                        hint=(
                            "Check network connectivity to the tunnel "
                            "endpoint and increase "
                            "EXECTUNNEL_RECONNECT_MAX_RETRIES if transient "
                            "disruptions are expected."
                        ),
                    )

                delay = min(base_delay * (2 ** attempt), max_delay)
                jitter = random.uniform(0, delay * _RECONNECT_JITTER)
                # Do NOT cap after adding jitter — capping would collapse
                # the jitter range to zero when delay is close to
                # max_delay, defeating the purpose of jitter at high retry
                # counts.
                delay = delay + jitter
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
        """Publish current sizes of all session registries as gauges."""
        metrics_gauge_set(
            "session.registry.tcp",
            float(len(self._tcp_registry)),
        )
        metrics_gauge_set(
            "session.registry.pending_connects",
            float(len(self._pending_connects)),
        )
        metrics_gauge_set(
            "session.registry.udp",
            float(len(self._udp_registry)),
        )
        metrics_gauge_set(
            "session.request_tasks",
            float(len(self._request_tasks)),
        )

    def _zero_gauges(self) -> None:
        """Zero all session-level gauges — called on session teardown."""
        for name in (
            "session.registry.tcp",
            "session.registry.pending_connects",
            "session.registry.udp",
            "session.request_tasks",
        ):
            metrics_gauge_set(name, 0.0)

    # ── Session initialisation ────────────────────────────────────────────────

    async def _clear_session_state(self) -> None:
        """Abort/close all per-session state before a new session starts.

        Aborts any lingering TCP connections, cancels pending connect
        futures, and closes UDP flows from a previous session to prevent
        resource leaks on reconnect.
        """
        # ── Close the previous session's dispatcher sentinel task ─────
        if self._dispatcher is not None:
            self._dispatcher.close()
            self._dispatcher = None

        # Cancel and await all in-flight request tasks BEFORE clearing
        # registries — request tasks may reference registry entries.
        for task in list(self._request_tasks):
            if not task.done():
                task.cancel()
        if self._request_tasks:
            await asyncio.gather(
                *self._request_tasks, return_exceptions=True,
            )
        self._request_tasks.clear()

        # Abort TCP connections from the previous session.
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
                    "error aborting stale tcp connection during session "
                    "reset",
                    exc_info=True,
                )
        self._tcp_registry.clear()

        # Cancel pending connect futures.
        pending_cleaned = len(self._pending_connects)
        for pending in self._pending_connects.values():
            if not pending.ack_future.done():
                pending.ack_future.cancel()
        self._pending_connects.clear()

        # Signal UDP flows that the remote side is gone.
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
            metrics_inc(
                "session.cleanup.tcp", value=tcp_cleaned,
            )
            metrics_inc(
                "session.cleanup.pending", value=pending_cleaned,
            )
            metrics_inc(
                "session.cleanup.udp", value=udp_cleaned,
            )
            logger.info(
                "session reset: cleaned %d TCP, %d pending, %d UDP",
                tcp_cleaned,
                pending_cleaned,
                udp_cleaned,
            )

        self._emit_registry_gauges()

    async def _start_session(self, ws: ClientConnection) -> None:
        """Initialise per-session state, bootstrap agent, and run tasks."""
        async with aspan("session.start"):
            metrics_inc("session.connect.ok")
            self._ws = ws

            # Recreate the event — prevents races from stale references
            # held by previous-session sub-components that might call
            # .set() late.
            self._ws_closed = asyncio.Event()

            await self._clear_session_state()

            # Bootstrap — raises BootstrapError subclasses on failure.
            async with aspan("session.bootstrap"):
                bootstrapper = AgentBootstrapper(ws, self._app, self._tun)
                await bootstrapper.run()

            # Construct per-session sub-components.
            self._sender = WsSender(ws, self._app, self._ws_closed)
            self._dispatcher = RequestDispatcher(
                tun_cfg=self._tun,
                ws_send=self._sender.send,
                ws_closed=self._ws_closed,
                tcp_registry=self._tcp_registry,
                pending_connects=self._pending_connects,
                udp_registry=self._udp_registry,
                pre_ack_buffer_cap_bytes=self._tun.pre_ack_buffer_cap_bytes,
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
        re-raised *after* the SOCKS5 server context manager exits, ensuring
        the server is fully stopped before the exception propagates to the
        reconnect loop.
        """
        if self._sender is None:
            raise RuntimeError(
                "_run_tasks called before _sender was initialised — "
                "call _start_session() instead of _run_tasks() directly.",
            )
        if self._dispatcher is None:
            raise RuntimeError(
                "_run_tasks called before _dispatcher was initialised — "
                "call _start_session() instead of _run_tasks() directly.",
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
                        max_inflight=self._app.bridge.dns_max_inflight,
                        upstream_port=self._tun.dns_upstream_port,
                        query_timeout=self._tun.dns_query_timeout,
                    )

                async with contextlib.AsyncExitStack() as _dns_stack:
                    if dns_fwd is not None:
                        await _dns_stack.enter_async_context(dns_fwd)

                    recv_task = asyncio.create_task(
                        receiver.run(), name="tun-recv-loop",
                    )
                    send_task = self._sender.task
                    if send_task is None:
                        raise RuntimeError(
                            "WsSender.task is None after start() — "
                            "this is a bug.",
                        )
                    socks_task = asyncio.create_task(
                        self._accept_loop(socks),
                        name="tun-socks-accept",
                    )
                    keepalive = KeepaliveLoop(
                        sender=self._sender,
                        ws_closed=self._ws_closed,
                        interval=float(self._app.bridge.ping_interval),
                    )
                    keepalive_task = asyncio.create_task(
                        keepalive.run(), name="tun-keepalive",
                    )

                    all_tasks = {
                        recv_task, send_task, socks_task, keepalive_task,
                    }

                    try:
                        async with aspan("session.serve"):
                            done, _ = await asyncio.wait(
                                all_tasks,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            # Surface the first non-cancelled exception.
                            for task in done:
                                if task.cancelled():
                                    continue
                                exc = task.exception()
                                if exc is None:
                                    continue
                                metrics_inc(
                                    "session.task.error",
                                    task=task.get_name(),
                                )
                                if isinstance(exc, ExecTunnelError):
                                    logger.error(
                                        "task %s failed [%s]: %s "
                                        "(error_id=%s)",
                                        task.get_name(),
                                        exc.error_code,
                                        exc.message,
                                        exc.error_id,
                                        extra=exc.to_dict(),
                                    )
                                else:
                                    logger.error(
                                        "task %s failed: %s",
                                        task.get_name(),
                                        exc,
                                    )
                                logger.debug(
                                    "task %s traceback",
                                    task.get_name(),
                                    exc_info=(
                                        type(exc), exc, exc.__traceback__,
                                    ),
                                )
                                if first_exc is None:
                                    first_exc = exc

                    finally:
                        for task in all_tasks:
                            if not task.done():
                                task.cancel()

                        await self._sender.stop()

                        await asyncio.gather(
                            *all_tasks, return_exceptions=True,
                        )

                        for task in list(self._request_tasks):
                            if not task.done():
                                task.cancel()
                        if self._request_tasks:
                            await asyncio.gather(
                                *self._request_tasks,
                                return_exceptions=True,
                            )

                        if self._dispatcher is not None:
                            self._dispatcher.close()

                        metrics_inc("session.serve.stopped")

        except BaseException as socks_exit_exc:
            if first_exc is not None and socks_exit_exc is not first_exc:
                logger.error(
                    "Socks5Server cleanup raised %s while handling "
                    "session exception — original exception takes "
                    "precedence: %s",
                    socks_exit_exc,
                    first_exc,
                )
                raise first_exc from socks_exit_exc
            raise

        if first_exc is not None:
            raise first_exc

    async def _accept_loop(self, socks: Socks5Server) -> None:
        """Accept SOCKS5 requests and dispatch each as an independent task."""
        if self._dispatcher is None:
            raise RuntimeError(
                "_accept_loop called before _dispatcher was initialised.",
            )
        async for req in socks:
            safe_host = re.sub(r"[^a-zA-Z0-9_.\-]", "_", req.host)[:40]
            task = asyncio.create_task(
                self._handle_request(req),
                name=f"req-{req.cmd.name}-{safe_host}_{req.port}",
            )
            self._request_tasks.add(task)
            task.add_done_callback(self._on_request_task_done)
            self._emit_registry_gauges()

        logger.debug("socks5 accept loop ended")

    def _on_request_task_done(self, task: asyncio.Task[None]) -> None:
        """Callback invoked when a request task completes."""
        self._request_tasks.discard(task)
        self._emit_registry_gauges()

        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return

        metrics_inc(
            "session.request.error", error=type(exc).__name__,
        )
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
            logger.warning(
                "request task %s failed: %s", task.get_name(), exc,
            )
        else:
            logger.error(
                "request task %s failed: %s", task.get_name(), exc,
            )
        logger.debug(
            "request task %s traceback",
            task.get_name(),
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    async def _handle_request(self, req: Socks5Request) -> None:
        """Dispatch a single SOCKS5 request to the appropriate handler."""
        if self._dispatcher is None:
            raise RuntimeError(
                "_handle_request called before _dispatcher was "
                "initialised.",
            )
        await self._dispatcher.dispatch(req)
