"""SOCKS5 request dispatcher — CONNECT, UDP_ASSOCIATE, and direct pipe.

``RequestDispatcher`` is called by ``TunnelSession._accept_loop`` for each
accepted ``Socks5Request``.  It is responsible for:

* Opening tunnel connections / UDP flows.
* Sending the SOCKS5 reply (success or error) exactly once.
* Relaying data until one side closes.
* Sending CONN_CLOSE / UDP_CLOSE on teardown.
"""

import asyncio
import contextlib
import ipaddress
import logging
import random
from typing import Final

from exectunnel.config.defaults import Defaults
from exectunnel.config.settings import TunnelConfig
from exectunnel.exceptions import (
    ConnectionClosedError,
    ExecTunnelError,
    TransportError,
    WebSocketSendTimeoutError,
)
from exectunnel.observability import (
    aspan,
    metrics_gauge_dec,
    metrics_gauge_inc,
    metrics_gauge_set,
    metrics_inc,
    metrics_observe,
)
from exectunnel.protocol import (
    Reply,
    encode_conn_open_frame,
    new_conn_id,
    new_flow_id,
)
from exectunnel.proxy import Socks5Request, UdpRelay
from exectunnel.transport import TcpConnection, UdpFlow, WsSendCallable

from ._lru import LruDict
from ._routing import is_host_excluded
from ._state import AckStatus, PendingConnect
from ._udp import make_udp_socket

logger = logging.getLogger(__name__)

# Maximum number of distinct hosts tracked in per-host rate-limit structures.
_HOST_SEMAPHORE_CAPACITY: Final[int] = 4_096

# Timeout applied to writer.wait_closed() during _pipe cleanup to prevent
# indefinite hangs when the OS never delivers the FIN ACK.
_PIPE_WRITER_CLOSE_TIMEOUT_SECS: Final[float] = 5.0

# Timeout for direct (excluded-host) TCP connect attempts.  Prevents a hung
# DNS lookup or TCP handshake from blocking the dispatch task indefinitely.
_DIRECT_CONNECT_TIMEOUT_SECS: Final[float] = 15.0


class RequestDispatcher:
    """Dispatches CONNECT and UDP_ASSOCIATE requests on behalf of TunnelSession.

    Shared state (registries, ws_send, ws_closed) is injected at construction.
    All methods are coroutines safe to run as independent asyncio tasks.

    Args:
        tun_cfg:                  Tunnel configuration.
        ws_send:                  Concurrency-safe frame sender callable.
        ws_closed:                Event set when the WebSocket closes.
        tcp_registry:             Shared TCP connection registry.
        pending_connects:         Shared pending-connect registry.
        udp_registry:             Shared UDP flow registry.
        pre_ack_buffer_cap_bytes: Pre-ACK buffer cap forwarded to TcpConnection.
    """

    __slots__ = (
        "_tun",
        "_ws_send",
        "_ws_closed",
        "_tcp_registry",
        "_pending_connects",
        "_udp_registry",
        "_pre_ack_buffer_cap_bytes",
        "_connect_gate",
        "_host_semaphores",
        "_host_connect_open_locks",
        "_host_connect_last_open_at",
        "_connect_failures_by_host",
        "_ack_failure_count",
        "_ack_failure_suppressed",
        "_ack_timeout_window_start",
        "_ack_timeout_window_count",
        "_ack_agent_error_window_start",
        "_ack_agent_error_window_count",
        "_ack_reconnect_requested",
        "_ws_closed_task",
    )

    def __init__(
        self,
        tun_cfg: TunnelConfig,
        ws_send: WsSendCallable,
        ws_closed: asyncio.Event,
        tcp_registry: dict[str, TcpConnection],
        pending_connects: dict[str, PendingConnect],
        udp_registry: dict[str, UdpFlow],
        pre_ack_buffer_cap_bytes: int,
    ) -> None:
        self._tun = tun_cfg
        self._ws_send = ws_send
        self._ws_closed = ws_closed
        self._tcp_registry = tcp_registry
        self._pending_connects = pending_connects
        self._udp_registry = udp_registry
        self._pre_ack_buffer_cap_bytes = pre_ack_buffer_cap_bytes

        self._connect_gate = asyncio.Semaphore(tun_cfg.connect_max_pending)
        self._host_semaphores: LruDict[str, asyncio.Semaphore] = LruDict(
            _HOST_SEMAPHORE_CAPACITY,
        )
        self._host_connect_open_locks: LruDict[str, asyncio.Lock] = LruDict(
            _HOST_SEMAPHORE_CAPACITY,
        )
        self._host_connect_last_open_at: LruDict[str, float] = LruDict(
            _HOST_SEMAPHORE_CAPACITY,
        )
        self._connect_failures_by_host: LruDict[str, int] = LruDict(
            _HOST_SEMAPHORE_CAPACITY,
        )

        # ACK failure tracking.
        self._ack_failure_count: int = 0
        self._ack_failure_suppressed: int = 0
        self._ack_timeout_window_start: float | None = None
        self._ack_timeout_window_count: int = 0
        self._ack_agent_error_window_start: float | None = None
        self._ack_agent_error_window_count: int = 0
        self._ack_reconnect_requested: bool = False

        # Shared task that resolves when the WebSocket closes.  Created once
        # and reused by all concurrent _await_conn_ack calls.
        self._ws_closed_task: asyncio.Task[bool] = (
            asyncio.get_running_loop().create_task(
                ws_closed.wait(), name="ws-closed-sentinel",
            )
        )

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Cancel the shared ``_ws_closed_task`` sentinel.

        Must be called when the session is torn down to prevent the sentinel
        task from leaking.  Safe to call multiple times.
        """
        if not self._ws_closed_task.done():
            self._ws_closed_task.cancel()

    # ── Public dispatch ───────────────────────────────────────────────────────

    async def dispatch(self, req: Socks5Request) -> None:
        """Dispatch a SOCKS5 request to the appropriate handler."""
        if req.is_connect:
            async with aspan("socks.connect", host=req.host, port=req.port):
                await self._handle_connect(req)
        elif req.is_udp_associate:
            async with aspan("socks.udp_associate"):
                await self._handle_udp_associate(req)
        elif not req.replied:
            await req.send_reply_error(Reply.CMD_NOT_SUPPORTED)

    # ── CONNECT ───────────────────────────────────────────────────────────────

    async def _handle_connect(self, req: Socks5Request) -> None:
        """Handle a SOCKS5 CONNECT request."""
        async with req:
            host, port = req.host, req.port

            if is_host_excluded(host, self._tun.exclude):
                await self._handle_direct_connect(req, host, port)
                return

            await self._handle_tunnel_connect(req, host, port)

    async def _handle_direct_connect(
        self,
        req: Socks5Request,
        host: str,
        port: int,
    ) -> None:
        """Open a direct TCP connection for excluded hosts."""
        async with aspan("socks.connect.direct", host=host, port=port):
            logger.debug("direct connect %s:%d (excluded)", host, port)
            try:
                async with asyncio.timeout(_DIRECT_CONNECT_TIMEOUT_SECS):
                    rem_reader, rem_writer = await asyncio.open_connection(
                        host, port,
                    )
            except (OSError, TimeoutError) as exc:
                logger.debug("direct connect failed: %s", exc)
                metrics_inc("socks.reply", code=int(Reply.HOST_UNREACHABLE))
                if not req.replied:
                    await req.send_reply_error(Reply.HOST_UNREACHABLE)
                return

            try:
                await req.send_reply_success()
            except OSError as exc:
                logger.debug(
                    "direct connect %s:%d — client closed before reply: %s",
                    host,
                    port,
                    exc,
                )
                metrics_inc("socks.reply", code=int(Reply.GENERAL_FAILURE))
                rem_writer.close()
                with contextlib.suppress(OSError):
                    await rem_writer.wait_closed()
                return

            metrics_inc("socks.reply", code=int(Reply.SUCCESS))
            await _pipe(req.reader, req.writer, rem_reader, rem_writer)

    async def _handle_tunnel_connect(
        self,
        req: Socks5Request,
        host: str,
        port: int,
    ) -> None:
        """Open a tunnel CONNECT and relay data."""
        host_gate = self._get_host_semaphore(host)
        conn_id = new_conn_id()
        loop = asyncio.get_running_loop()
        ack_future: asyncio.Future[AckStatus] = loop.create_future()

        handler = TcpConnection(
            conn_id,
            req.reader,
            req.writer,
            self._ws_send,
            self._tcp_registry,
            pre_ack_buffer_cap_bytes=self._pre_ack_buffer_cap_bytes,
        )
        pending = PendingConnect(host=host, port=port, ack_future=ack_future)

        # Insert into registry BEFORE sending CONN_OPEN — agent may reply
        # before the await returns.
        self._tcp_registry[conn_id] = handler
        metrics_gauge_inc("session.active.tcp_connections")
        self._pending_connects[conn_id] = pending
        self._emit_pending_gauge()

        ack_wait_start = loop.time()
        ack_status: AckStatus = AckStatus.OK
        reply = Reply.HOST_UNREACHABLE
        failure_reason: AckStatus | None = None

        try:
            async with aspan("tunnel.conn_open", conn_id=conn_id, host=host, port=port):
                async with self._connect_gate, host_gate:
                    host_key = _normalise_host_key(host)
                    await self._pace_connection(host_key)

                    try:
                        await self._ws_send(
                            encode_conn_open_frame(conn_id, host, port),
                        )
                    except WebSocketSendTimeoutError as exc:
                        failure_reason = AckStatus.WS_SEND_TIMEOUT
                        ack_status = AckStatus.WS_SEND_TIMEOUT
                        reply = Reply.GENERAL_FAILURE
                        metrics_inc(
                            "tunnel.conn_open.error", error="ws_send_timeout",
                        )
                        logger.warning(
                            "conn %s: CONN_OPEN send timed out [%s] (error_id=%s)",
                            conn_id,
                            exc.error_code,
                            exc.error_id,
                            extra={
                                "conn_id": conn_id,
                                "error_code": exc.error_code,
                                "error_id": exc.error_id,
                            },
                        )
                    except ConnectionClosedError as exc:
                        failure_reason = AckStatus.WS_CLOSED
                        ack_status = AckStatus.WS_CLOSED
                        reply = Reply.NET_UNREACHABLE
                        metrics_inc(
                            "tunnel.conn_open.error", error="connection_closed",
                        )
                        logger.warning(
                            "conn %s: CONN_OPEN failed — connection closed [%s] "
                            "(error_id=%s)",
                            conn_id,
                            exc.error_code,
                            exc.error_id,
                            extra={
                                "conn_id": conn_id,
                                "error_code": exc.error_code,
                                "error_id": exc.error_id,
                            },
                        )
                    else:
                        metrics_inc(
                            "tunnel.conn_open",
                            conn_id=conn_id,
                            host=host,
                            port=port,
                        )
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

                # Semaphores released after CONN_OPEN is sent — ACK wait
                # happens outside the semaphore scope so a slow agent ACK
                # does not block new connections.
                if failure_reason is None:
                    async with aspan(
                        "tunnel.conn_ack.wait", conn_id=conn_id,
                    ):
                        failure_reason, reply = await self._await_conn_ack(
                            conn_id, pending, ack_future,
                        )
                    if failure_reason is not None:
                        ack_status = failure_reason

            if failure_reason is not None:
                self._record_ack_failure(conn_id, failure_reason, host=host, port=port)
                self._record_connect_failure(host, failure_reason, reply)
                await self._teardown_failed_connection(conn_id, handler)
                if not req.replied:
                    await req.send_reply_error(reply)
                return

            metrics_inc("tunnel.conn_ack.ok", conn_id=conn_id, host=host, port=port)

            # send_reply_success() can raise OSError when the SOCKS5 client
            # closes its socket before we send the reply — normal race.
            try:
                await req.send_reply_success()
            except OSError as exc:
                logger.debug(
                    "conn %s: SOCKS5 client closed before reply could be sent: %s",
                    conn_id,
                    exc,
                    extra={"conn_id": conn_id},
                )
                metrics_inc("socks.reply", code=int(Reply.GENERAL_FAILURE))
                await self._teardown_failed_connection(conn_id, handler)
                return

            metrics_inc("socks.reply", code=int(Reply.SUCCESS))
            handler.start()
            await handler.closed_event.wait()
            metrics_inc("tunnel.conn.completed", conn_id=conn_id, host=host, port=port)

        except ExecTunnelError as exc:
            ack_status = AckStatus.LIBRARY_ERROR
            reply = Reply.GENERAL_FAILURE
            self._record_ack_failure(conn_id, AckStatus.LIBRARY_ERROR, host=host, port=port)
            self._record_connect_failure(host, AckStatus.LIBRARY_ERROR, reply)
            metrics_inc(
                "tunnel.conn.error",
                conn_id=conn_id,
                host=host,
                port=port,
                error=exc.error_code.replace(".", "_"),
            )
            logger.warning(
                "conn %s: tunnel error [%s]: %s (error_id=%s)",
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
            await self._teardown_failed_connection(conn_id, handler)
            if not req.replied:
                await req.send_reply_error(reply)

        except OSError as exc:
            logger.debug(
                "conn %s: SOCKS5 client I/O error during connect: %s",
                conn_id,
                exc,
                extra={"conn_id": conn_id},
            )
            metrics_inc("socks.reply", code=int(Reply.GENERAL_FAILURE))
            await self._teardown_failed_connection(conn_id, handler)
            if not req.replied:
                with contextlib.suppress(OSError):
                    await req.send_reply_error(Reply.GENERAL_FAILURE)

        except Exception as exc:
            ack_status = AckStatus.UNEXPECTED_ERROR
            reply = Reply.GENERAL_FAILURE
            self._record_ack_failure(conn_id, AckStatus.UNEXPECTED_ERROR, host=host, port=port)
            self._record_connect_failure(host, AckStatus.UNEXPECTED_ERROR, reply)
            metrics_inc("tunnel.conn.error", conn_id=conn_id, host=host, port=port, error="unexpected")
            logger.error(
                "conn %s: unexpected error during tunnel connect: %s",
                conn_id,
                exc,
                exc_info=True,
                extra={"conn_id": conn_id, "host": host, "port": port},
            )
            await self._teardown_failed_connection(conn_id, handler)
            if not req.replied:
                await req.send_reply_error(reply)

        finally:
            self._pending_connects.pop(conn_id, None)
            if conn_id not in self._tcp_registry:
                # Handler was removed from registry (closed or aborted) →
                # decrement the gauge that was incremented on insertion.
                metrics_gauge_dec("session.active.tcp_connections")
            if not ack_future.done():
                ack_future.cancel()
            self._emit_pending_gauge()
            metrics_observe(
                "tunnel.conn_ack.wait_sec",
                loop.time() - ack_wait_start,
                status=ack_status.value,
            )

    async def _await_conn_ack(
        self,
        conn_id: str,
        pending: PendingConnect,
        ack_future: asyncio.Future[AckStatus],
    ) -> tuple[AckStatus | None, Reply]:
        """Wait for CONN_ACK, WS close, or timeout.

        Returns:
            ``(failure_reason, reply)`` where ``failure_reason`` is ``None``
            on success or an ``AckStatus`` member on failure.
        """
        ws_closed_task = self._ws_closed_task
        try:
            async with asyncio.timeout(self._tun.conn_ack_timeout):
                done, _ = await asyncio.wait(
                    {ack_future, ws_closed_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
        except TimeoutError:
            metrics_inc(
                "tunnel.conn_ack.timeout",
                conn_id=conn_id,
                host=pending.host,
                port=pending.port,
            )
            return AckStatus.TIMEOUT, Reply.HOST_UNREACHABLE

        if ws_closed_task in done:
            return AckStatus.WS_CLOSED, Reply.NET_UNREACHABLE

        ack_result = ack_future.result()
        if ack_result == AckStatus.OK:
            return None, Reply.SUCCESS

        return ack_result, Reply.HOST_UNREACHABLE

    async def _teardown_failed_connection(
        self,
        conn_id: str,
        handler: TcpConnection,
    ) -> None:
        """Tear down a handler that never reached the RUNNING state."""
        if not handler.is_started:
            try:
                await handler.close_unstarted()
            except RuntimeError:
                logger.warning(
                    "conn %s: close_unstarted() called after start() — "
                    "falling back to abort().",
                    conn_id,
                    extra={"conn_id": conn_id},
                )
                handler.abort()
            except OSError:
                pass
        else:
            handler.abort()

    # ── UDP ASSOCIATE ─────────────────────────────────────────────────────────

    async def _handle_udp_associate(self, req: Socks5Request) -> None:
        """Handle a SOCKS5 UDP_ASSOCIATE request."""
        async with req:
            if req.udp_relay is None:
                logger.error(
                    "UDP ASSOCIATE request has no relay — this is a bug in "
                    "_negotiate()",
                )
                if not req.replied:
                    await req.send_reply_error(Reply.GENERAL_FAILURE)
                return

            relay = req.udp_relay

            await req.send_reply_success(
                bind_host="127.0.0.1",
                bind_port=relay.local_port,
            )
            logger.debug("UDP ASSOCIATE local port %d", relay.local_port)

            # Maps (dst_host, dst_port) → (UdpFlow, resolved_src_ip).
            active_flows: dict[tuple[str, int], tuple[UdpFlow, str]] = {}
            drain_tasks: set[asyncio.Task[None]] = set()

            async def _drain_flow(
                flow_handler: UdpFlow,
                src_ip: str,
                dst_port: int,
                flow_key: tuple[str, int],
            ) -> None:
                """Drain inbound datagrams from a UdpFlow to the relay client."""
                try:
                    while (data := await flow_handler.recv_datagram()) is not None:
                        relay.send_to_client(data, src_ip, dst_port)
                except OSError:
                    metrics_inc("udp.drain.error", error="os_error")
                except (TransportError, ExecTunnelError) as exc:
                    err = getattr(
                        exc, "error_code", type(exc).__name__,
                    ).replace(".", "_")
                    metrics_inc("udp.drain.error", error=err)
                    logger.debug(
                        "udp drain flow=%s [%s]: %s",
                        flow_handler.flow_id,
                        err,
                        exc,
                    )
                except asyncio.CancelledError:
                    raise
                finally:
                    active_flows.pop(flow_key, None)
                    metrics_gauge_dec("session.active.udp_flows")

            async def _pump_inbound() -> None:
                """Read datagrams from the SOCKS5 relay and forward via tunnel."""
                while True:
                    try:
                        async with asyncio.timeout(
                            self._tun.udp_pump_poll_timeout,
                        ):
                            result = await relay.recv()
                    except TimeoutError:
                        if req.reader.at_eof():
                            break
                        continue

                    if result is None:
                        break

                    payload, dst_host, dst_port = result

                    if is_host_excluded(dst_host, self._tun.exclude):
                        await _direct_udp_relay(
                            relay,
                            payload,
                            dst_host,
                            dst_port,
                            recv_timeout=self._tun.udp_direct_recv_timeout,
                        )
                        continue

                    key = (dst_host, dst_port)
                    flow_entry = active_flows.get(key)

                    if flow_entry is None:
                        try:
                            src_ip = _require_ip_literal(dst_host)
                        except ValueError:
                            logger.debug(
                                "udp flow %s:%d — dst_host is a domain name; "
                                "send_to_client will drop responses silently",
                                dst_host,
                                dst_port,
                            )
                            src_ip = dst_host

                        flow_id = new_flow_id()
                        flow_handler = UdpFlow(
                            flow_id,
                            dst_host,
                            dst_port,
                            self._ws_send,
                            self._udp_registry,
                        )
                        self._udp_registry[flow_id] = flow_handler
                        metrics_gauge_inc("session.active.udp_flows")
                        try:
                            await flow_handler.open()
                        except (
                            WebSocketSendTimeoutError,
                            ConnectionClosedError,
                            TransportError,
                            ExecTunnelError,
                        ) as exc:
                            err = getattr(
                                exc, "error_code", type(exc).__name__,
                            ).replace(".", "_")
                            metrics_inc("udp.flow.open.error", error=err)
                            logger.warning(
                                "udp flow %s open error [%s] — dropping datagram",
                                flow_id,
                                err,
                            )
                            flow_handler.on_remote_closed()
                            metrics_gauge_dec("session.active.udp_flows")
                            continue

                        active_flows[key] = (flow_handler, src_ip)
                        task = asyncio.create_task(
                            _drain_flow(flow_handler, src_ip, dst_port, key),
                            name=f"udp-drain-{flow_id}",
                        )
                        drain_tasks.add(task)
                        task.add_done_callback(drain_tasks.discard)
                    else:
                        flow_handler, src_ip = flow_entry

                    try:
                        await flow_handler.send_datagram(payload)
                    except (
                        WebSocketSendTimeoutError,
                        ConnectionClosedError,
                        TransportError,
                    ) as exc:
                        err = getattr(
                            exc, "error_code", type(exc).__name__,
                        ).replace(".", "_")
                        metrics_inc("udp.datagram.send.error", error=err)
                        logger.warning(
                            "udp flow %s send error [%s] — datagram dropped",
                            flow_handler.flow_id,
                            err,
                        )

            try:
                await _pump_inbound()
            finally:
                for entry in list(active_flows.values()):
                    h, _ = entry
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

    # ── ACK failure tracking ──────────────────────────────────────────────────

    def _record_ack_failure(
        self,
        conn_id: str,
        reason: AckStatus,
        *,
        host: str = "",
        port: int = 0,
    ) -> None:
        """Record an ACK failure and trigger reconnect if threshold exceeded."""
        self._ack_failure_count += 1
        metrics_inc(
            "tunnel.conn_ack.failed",
            conn_id=conn_id,
            reason=reason.value,
            host=host,
            port=port,
        )

        if reason == AckStatus.TIMEOUT:
            now = asyncio.get_running_loop().time()
            if (
                self._ack_timeout_window_start is None
                or now - self._ack_timeout_window_start
                > self._tun.ack_timeout_window_secs
            ):
                self._ack_timeout_window_start = now
                self._ack_timeout_window_count = 0
            self._ack_timeout_window_count += 1

        if reason == AckStatus.AGENT_ERROR:
            now = asyncio.get_running_loop().time()
            if (
                self._ack_agent_error_window_start is None
                or now - self._ack_agent_error_window_start
                > self._tun.ack_timeout_window_secs
            ):
                self._ack_agent_error_window_start = now
                self._ack_agent_error_window_count = 0
            self._ack_agent_error_window_count += 1

        should_log = (
            self._ack_failure_count == 1
            or self._ack_failure_count % self._tun.ack_timeout_warn_every == 0
            or reason not in (AckStatus.TIMEOUT, AckStatus.AGENT_ERROR)
        )
        if not should_log:
            self._ack_failure_suppressed += 1
            return

        suppressed = self._ack_failure_suppressed
        self._ack_failure_suppressed = 0
        extra_msg = f", suppressed={suppressed}" if suppressed else ""
        logger.warning(
            "agent ACK failure conn=%s reason=%s total=%d pending=%d%s",
            conn_id,
            reason.value,
            self._ack_failure_count,
            len(self._pending_connects),
            extra_msg,
        )

        if reason not in (AckStatus.TIMEOUT, AckStatus.AGENT_ERROR):
            return
        if self._ack_reconnect_requested:
            return

        if reason == AckStatus.TIMEOUT:
            window_count = self._ack_timeout_window_count
            label = "ACK timeouts"
        else:
            window_count = self._ack_agent_error_window_count
            label = "agent errors"

        if window_count < self._tun.ack_timeout_reconnect_threshold:
            return

        self._ack_reconnect_requested = True
        metrics_inc("tunnel.conn_ack.reconnect_triggered")
        logger.error(
            "agent appears unhealthy: %d %s within %.0fs; forcing reconnect",
            window_count,
            label,
            self._tun.ack_timeout_window_secs,
        )

    def reset_ack_state(self) -> None:
        """Reset all ACK failure tracking state after a successful bootstrap."""
        self._ack_reconnect_requested = False
        self._ack_timeout_window_start = None
        self._ack_timeout_window_count = 0
        self._ack_agent_error_window_start = None
        self._ack_agent_error_window_count = 0
        self._ack_failure_count = 0
        self._ack_failure_suppressed = 0

    # ── Connection hardening helpers ──────────────────────────────────────────

    def _get_host_semaphore(self, host: str) -> asyncio.Semaphore:
        """Return (or create) the per-host concurrency semaphore."""
        key = _normalise_host_key(host)
        gate = self._host_semaphores.get(key)
        if gate is None:
            gate = asyncio.Semaphore(self._tun.connect_max_pending_per_host)
            self._host_semaphores[key] = gate
        return gate

    async def _pace_connection(self, host_key: str) -> None:
        """Enforce per-host connection pacing with jitter."""
        interval = self._tun.connect_pace_interval_secs
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
                            min(
                                Defaults.CONNECT_PACE_JITTER_CAP_SECS,
                                interval / 2.0,
                            ),
                        ),
                    )
            self._host_connect_last_open_at[host_key] = (
                asyncio.get_running_loop().time()
            )

    def _emit_pending_gauge(self) -> None:
        """Emit the current pending-connect count as a gauge metric."""
        metrics_gauge_set(
            "session.pending_connects",
            float(len(self._pending_connects)),
        )

    def _record_connect_failure(
        self, host: str, reason: AckStatus, reply: Reply,
    ) -> None:
        """Increment per-host failure counter and emit metrics."""
        host_key = _normalise_host_key(host)
        self._connect_failures_by_host[host_key] = (
            self._connect_failures_by_host.get(host_key, 0) + 1
        )
        metrics_inc(
            "tunnel.connect.fail_by_host", host=host_key, reason=reason.value,
        )
        metrics_inc("socks.reply", code=int(reply))

    # ── Debug ─────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"<RequestDispatcher "
            f"pending={len(self._pending_connects)} "
            f"tcp={len(self._tcp_registry)} "
            f"udp={len(self._udp_registry)} "
            f"ack_failures={self._ack_failure_count} "
            f"reconnect_requested={self._ack_reconnect_requested}>"
        )


# ── Module-level helpers ──────────────────────────────────────────────────────


def _normalise_host_key(host: str) -> str:
    """Normalise a host string for use as a dict / metrics key."""
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        return host.lower().strip()


def _require_ip_literal(host: str) -> str:
    """Return *host* unchanged if it is an IP literal, else raise ValueError."""
    ipaddress.ip_address(host)  # raises ValueError for non-IP
    return host


async def _pipe(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    remote_reader: asyncio.StreamReader,
    remote_writer: asyncio.StreamWriter,
) -> None:
    """Bidirectional byte copy for directly-connected (excluded) hosts.

    Uses :class:`asyncio.TaskGroup` for half-close semantics: when one
    direction hits EOF and returns normally, the other continues until it
    also finishes.
    """

    async def copy(
        src: asyncio.StreamReader,
        dst: asyncio.StreamWriter,
    ) -> None:
        try:
            while True:
                chunk = await src.read(Defaults.PIPE_READ_CHUNK_BYTES)
                if not chunk:
                    if dst.can_write_eof():
                        with contextlib.suppress(OSError):
                            dst.write_eof()
                            await dst.drain()
                    return
                dst.write(chunk)
                await dst.drain()
        except OSError:
            metrics_inc("pipe.copy.os_error")
        except Exception:
            # Defensive: catch non-OSError exceptions (e.g. RuntimeError from
            # drain() on a closing transport) to prevent TaskGroup from
            # wrapping them in an ExceptionGroup.
            metrics_inc("pipe.copy.unexpected_error")
            logger.debug("pipe copy unexpected error", exc_info=True)

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(copy(client_reader, remote_writer))
            tg.create_task(copy(remote_reader, client_writer))
    finally:
        for writer in (remote_writer, client_writer):
            with contextlib.suppress(OSError, RuntimeError):
                writer.close()
            with contextlib.suppress(OSError, RuntimeError, asyncio.TimeoutError):
                async with asyncio.timeout(_PIPE_WRITER_CLOSE_TIMEOUT_SECS):
                    await writer.wait_closed()


async def _direct_udp_relay(
    relay: UdpRelay,
    payload: bytes,
    dst_host: str,
    dst_port: int,
    recv_timeout: float = Defaults.UDP_DIRECT_RECV_TIMEOUT_SECS,
) -> None:
    """Send a single UDP datagram directly (excluded host) and relay the response.

    ``dst_host`` is guaranteed to be an IP literal here — ``is_host_excluded``
    only returns ``True`` for IP addresses, never for domain names.

    .. note::
        Only the **first** response datagram is relayed back to the client.
    """
    try:
        ipaddress.ip_address(dst_host)
    except ValueError:
        logger.error(
            "_direct_udp_relay called with non-IP dst_host=%r — dropping datagram",
            dst_host,
        )
        return

    sock = None
    try:
        sock = await make_udp_socket(dst_host)
        loop = asyncio.get_running_loop()
        await loop.sock_connect(sock, (dst_host, dst_port))
        await loop.sock_sendall(sock, payload)
        try:
            async with asyncio.timeout(recv_timeout):
                response = await loop.sock_recv(sock, 65_535)
            relay.send_to_client(response, dst_host, dst_port)
        except TimeoutError:
            pass
    except (OSError, ExecTunnelError):
        metrics_inc("udp.direct.error")
    except Exception:
        metrics_inc("udp.direct.unexpected_error")
        logger.debug(
            "unexpected error in UDP direct relay to %s:%d",
            dst_host,
            dst_port,
            exc_info=True,
        )
    finally:
        if sock is not None:
            sock.close()
