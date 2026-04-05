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
import logging
import random
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
from exectunnel.observability import metrics_inc, metrics_observe, span
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

        Handles ``CancelledError`` explicitly to ensure cleanup runs before
        the cancellation propagates.

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

        with span("session.run"):
            while True:
                reconnect_reason: str | None = None
                try:
                    metrics_inc("tunnel.connect.attempt")
                    async with connect(
                        self._app.wss_url,
                        ssl=ssl_ctx,
                        # Disable built-in ping — we implement our own via
                        # KeepaliveLoop which serialises through WsSender.
                        ping_interval=None,
                        max_size=None,
                    ) as ws:
                        await self._start_session(ws)
                        attempt = 0  # Clean session — reset retry counter.

                except BootstrapError:
                    metrics_inc("tunnel.bootstrap.error")
                    raise  # Never retried.

                except asyncio.CancelledError:
                    # Caller cancelled run() — ensure session state is cleaned
                    # up before propagating.  _run_tasks' finally block handles
                    # in-session cleanup; this handles cancellation that hits
                    # between sessions (e.g. during reconnect sleep or connect).
                    logger.info("tunnel session cancelled")
                    raise

                except WebSocketSendTimeoutError as exc:
                    metrics_inc("tunnel.connect.error", error="ws_send_timeout")
                    reconnect_reason = (
                        f"ws_send_timeout: {exc.message} (error_id={exc.error_id})"
                    )
                    logger.warning(
                        "WebSocket send timed out [%s] (error_id=%s) — "
                        "will reconnect",
                        exc.error_code,
                        exc.error_id,
                    )

                except ConnectionClosedError as exc:
                    metrics_inc("tunnel.connect.error", error="connection_closed")
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
                    raise  # Truly unexpected — propagate.

                finally:
                    self._ws = None

                if reconnect_reason is None:
                    return  # Clean exit.

                if attempt >= retries:
                    metrics_inc("tunnel.reconnect.exhausted")
                    raise ReconnectExhaustedError(
                        f"WebSocket session terminated after {retries} "
                        "reconnect attempts.",
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
                jitter = random.uniform(0, delay * _RECONNECT_JITTER)
                # Cap total delay to max_delay so jitter never overshoots.
                delay = min(delay + jitter, max_delay)
                attempt += 1
                metrics_inc("session_reconnects_total", reason=reconnect_reason.split(":")[0] if reconnect_reason else "unknown")
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

    # ── Session initialisation ────────────────────────────────────────────────

    async def _clear_session_state(self) -> None:
        """Abort/close all per-session state before a new session starts.

        Aborts any lingering TCP connections, cancels pending connect futures,
        and closes UDP flows from a previous session to prevent resource leaks
        on reconnect.  Request tasks from the previous session are cancelled by
        ``_run_tasks`` before this is called, so the set should already be
        empty — clearing it here is a safety net.
        """
        # Abort TCP connections from the previous session.
        # TcpConnection has no close() — use abort() for started handlers and
        # close_unstarted() for handlers that never reached the RUNNING state.
        for conn in self._tcp_registry.values():
            try:
                if conn.is_started:
                    conn.abort()
                else:
                    await conn.close_unstarted()
            except Exception:  # noqa: BLE001
                logger.debug("error aborting stale tcp connection during session reset", exc_info=True)
        self._tcp_registry.clear()

        # Cancel pending connect futures.
        for pending in self._pending_connects.values():
            if not pending.ack_future.done():
                pending.ack_future.cancel()
        self._pending_connects.clear()

        # Close UDP flows from the previous session.
        for flow in self._udp_registry.values():
            try:
                await flow.close()
            except Exception:  # noqa: BLE001
                logger.debug("error closing stale udp flow during session reset", exc_info=True)
        self._udp_registry.clear()

        # Safety net — should already be empty.
        for task in list(self._request_tasks):
            if not task.done():
                task.cancel()
        if self._request_tasks:
            await asyncio.gather(*self._request_tasks, return_exceptions=True)
        self._request_tasks.clear()

    async def _start_session(self, ws: ClientConnection) -> None:
        """Initialise per-session state, bootstrap the agent, and run tasks.

        Extracted from ``run()`` so integration tests can inject a
        pre-connected ``ClientConnection`` without going through the real
        ``connect()`` call.
        """
        metrics_inc("tunnel.connect.ok")
        self._ws = ws

        # Recreate the event — prevents races from stale references held by
        # previous-session sub-components that might call .set() late.
        self._ws_closed = asyncio.Event()

        await self._clear_session_state()

        # Bootstrap — raises BootstrapError subclasses on failure.
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
        reconnect loop.  If ``Socks5Server.__aexit__`` itself raises, both
        exceptions are logged but the *original* session exception takes
        precedence.
        """
        if self._sender is None:
            raise RuntimeError(
                "_run_tasks called before _sender was initialised — "
                "call _start_session() instead of _run_tasks() directly."
            )
        if self._dispatcher is None:
            raise RuntimeError(
                "_run_tasks called before _dispatcher was initialised — "
                "call _start_session() instead of _run_tasks() directly."
            )

        metrics_inc("tunnel.serve.started")

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
        )

        first_exc: BaseException | None = None

        try:
            async with Socks5Server(socks_cfg) as socks:
                dns_fwd: DnsForwarder | None = None
                dns_started = False
                try:
                    if self._tun.dns_upstream:
                        # Deferred import — avoids circular import at module level.
                        from ._dns import DnsForwarder  # noqa: PLC0415

                        dns_fwd = DnsForwarder(
                            self._tun.dns_local_port,
                            self._tun.dns_upstream,
                            self._sender.send,
                            self._udp_registry,
                            max_inflight=self._app.bridge.dns_max_inflight,
                        )
                        await dns_fwd.start()
                        dns_started = True

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
                        interval=float(self._app.bridge.ping_interval),
                    )
                    keepalive_task = asyncio.create_task(
                        keepalive.run(), name="tun-keepalive"
                    )

                    all_tasks = {recv_task, send_task, socks_task, keepalive_task}

                    try:
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
                            metrics_inc("tunnel.tasks.error", task=task.get_name())
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
                                    "task %s failed: %s",
                                    task.get_name(),
                                    exc,
                                )
                            logger.debug(
                                "task %s traceback",
                                task.get_name(),
                                exc_info=(type(exc), exc, exc.__traceback__),
                            )
                            if first_exc is None:
                                first_exc = exc

                    finally:
                        # Cancel all core tasks.
                        for task in all_tasks:
                            if not task.done():
                                task.cancel()

                        # Unblock WsSender from queue.get().
                        await self._sender.stop()

                        await asyncio.gather(*all_tasks, return_exceptions=True)

                        # Cancel and await all in-flight request tasks.
                        for task in list(self._request_tasks):
                            if not task.done():
                                task.cancel()
                        if self._request_tasks:
                            await asyncio.gather(
                                *self._request_tasks, return_exceptions=True
                            )

                        metrics_inc("tunnel.serve.stopped")

                finally:
                    # Stop DNS forwarder — only if it was successfully started.
                    if dns_fwd is not None and dns_started:
                        try:
                            await dns_fwd.stop()
                        except Exception:  # noqa: BLE001
                            logger.debug("error stopping DNS forwarder", exc_info=True)

        except BaseException as socks_exit_exc:
            # If Socks5Server.__aexit__ raises AND we already captured a session
            # exception, log the __aexit__ error but preserve the original.
            if first_exc is not None and socks_exit_exc is not first_exc:
                logger.error(
                    "Socks5Server cleanup raised %s while handling session "
                    "exception — original exception takes precedence: %s",
                    socks_exit_exc,
                    first_exc,
                )
                raise first_exc from socks_exit_exc
            raise

        # Re-raise the first task exception AFTER cleanup so the reconnect
        # loop in run() can classify and handle it.
        if first_exc is not None:
            raise first_exc

    async def _accept_loop(self, socks: Socks5Server) -> None:
        """Accept SOCKS5 requests and dispatch each as an independent task.

        Logs when the SOCKS5 iterator ends — this is always triggered by
        :meth:`stop` during shutdown and is expected, but logging it at
        DEBUG aids debugging if the iterator ends unexpectedly.
        """
        if self._dispatcher is None:
            raise RuntimeError(
                "_accept_loop called before _dispatcher was initialised."
            )
        async for req in socks:
            # Sanitise task name — IPv6 colons replaced; truncated for readability.
            safe_host = req.host.replace(":", "_")[:40]
            task = asyncio.create_task(
                self._handle_request(req),
                name=f"req-{req.cmd.name}-{safe_host}_{req.port}",
            )
            self._request_tasks.add(task)
            task.add_done_callback(self._on_request_task_done)

        logger.debug("socks5 accept loop ended")

    def _on_request_task_done(self, task: asyncio.Task[None]) -> None:
        """Callback invoked when a request task completes."""
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
                extra=exc.to_dict(),
            )
        else:
            logger.error("request task %s failed: %s", task.get_name(), exc)
        logger.debug(
            "request task %s traceback",
            task.get_name(),
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    async def _handle_request(self, req: Socks5Request) -> None:
        """Dispatch a single SOCKS5 request to the appropriate handler."""
        if self._dispatcher is None:
            raise RuntimeError(
                "_handle_request called before _dispatcher was initialised."
            )
        await self._dispatcher.dispatch(req)
