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

from exectunnel.config.defaults import Defaults
from exectunnel.config.settings import TunnelConfig
from exectunnel.exceptions import (
    ConnectionClosedError,
    ExecTunnelError,
    TransportError,
    WebSocketSendTimeoutError,
)
from exectunnel.observability import (
    metrics_gauge_inc,
    metrics_inc,
    metrics_observe,
    span,
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
from ._payload import make_udp_socket
from ._routing import is_host_excluded
from ._state import AckStatus, PendingConnect

logger = logging.getLogger(__name__)

# Maximum number of distinct hosts tracked in per-host rate-limit structures.
_HOST_SEMAPHORE_CAPACITY = 4_096


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
        "_ack_reconnect_requested",
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
            _HOST_SEMAPHORE_CAPACITY
        )
        self._host_connect_open_locks: LruDict[str, asyncio.Lock] = LruDict(
            _HOST_SEMAPHORE_CAPACITY
        )
        self._host_connect_last_open_at: LruDict[str, float] = LruDict(
            _HOST_SEMAPHORE_CAPACITY
        )
        self._connect_failures_by_host: dict[str, int] = {}

        # ACK failure tracking.
        self._ack_failure_count: int = 0
        self._ack_failure_suppressed: int = 0
        self._ack_timeout_window_start: float | None = None
        self._ack_timeout_window_count: int = 0
        self._ack_reconnect_requested: bool = False

    # ── Public dispatch ───────────────────────────────────────────────────────

    async def dispatch(self, req: Socks5Request) -> None:
        """Dispatch a SOCKS5 request to the appropriate handler."""
        # Each request task inherits the session's trace context via asyncio
        # context copy.  We open a fresh child span so per-request logs carry
        # a unique span_id while sharing the session trace_id.
        if req.is_connect:
            with span("socks.connect", host=req.host, port=req.port):
                await self._handle_connect(req)
        elif req.is_udp_associate:
            with span("socks.udp_associate"):
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
        logger.debug("direct connect %s:%d (excluded)", host, port)
        try:
            rem_reader, rem_writer = await asyncio.open_connection(host, port)
        except OSError as exc:
            logger.debug("direct connect failed: %s", exc)
            metrics_inc("socks_reply_code", code=int(Reply.HOST_UNREACHABLE))
            if not req.replied:
                await req.send_reply_error(Reply.HOST_UNREACHABLE)
            return

        metrics_inc("socks_reply_code", code=int(Reply.SUCCESS))
        await req.send_reply_success()
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
        pending = PendingConnect(host=host, ack_future=ack_future)

        # Insert into registry BEFORE sending CONN_OPEN — agent may reply
        # before the await returns.
        self._tcp_registry[conn_id] = handler
        metrics_gauge_inc("session_active_tcp_connections")
        self._pending_connects[conn_id] = pending
        self._emit_pending_gauge()

        ack_wait_start = loop.time()
        ack_status: AckStatus = AckStatus.OK
        reply = Reply.HOST_UNREACHABLE
        failure_reason: AckStatus | None = None

        try:
            async with self._connect_gate, host_gate:
                host_key = _normalise_host_key(host)
                await self._pace_connection(host_key)

                try:
                    # CONN_OPEN is not a control frame — use default flags.
                    await self._ws_send(encode_conn_open_frame(conn_id, host, port))
                except WebSocketSendTimeoutError as exc:
                    failure_reason = AckStatus.WS_SEND_TIMEOUT
                    ack_status = AckStatus.WS_SEND_TIMEOUT
                    reply = Reply.GENERAL_FAILURE
                    metrics_inc("tunnel.conn_open.error", error="ws_send_timeout")
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
                    metrics_inc("tunnel.conn_open.error", error="connection_closed")
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

                if failure_reason is None:
                    logger.debug(
                        "tunnel CONNECT %s:%d conn=%s",
                        host,
                        port,
                        conn_id,
                        extra={"conn_id": conn_id, "host": host, "port": port},
                    )
                    failure_reason, reply = await self._await_conn_ack(
                        conn_id, pending, ack_future
                    )
                    if failure_reason is not None:
                        ack_status = failure_reason

            # Semaphores released — before starting data flow.

            if failure_reason is not None:
                self._record_ack_failure(conn_id, failure_reason)
                self._record_connect_failure(host, failure_reason, reply)
                await self._teardown_failed_connection(conn_id, handler)
                if not req.replied:
                    await req.send_reply_error(reply)
                return

            metrics_inc("tunnel.conn_ack.ok")
            metrics_inc("socks_reply_code", code=int(Reply.SUCCESS))

            # send_reply_success() can raise OSError when the SOCKS5 client
            # closes its socket before we send the reply — this is a normal
            # race condition (e.g. aria2 probing connections, client timeout).
            # Treat it as a silent teardown, not an ACK failure or tunnel error.
            try:
                await req.send_reply_success()
            except OSError as exc:
                logger.debug(
                    "conn %s: SOCKS5 client closed before reply could be sent: %s",
                    conn_id,
                    exc,
                    extra={"conn_id": conn_id},
                )
                metrics_inc("socks_reply_code", code=int(Reply.GENERAL_FAILURE))
                await self._teardown_failed_connection(conn_id, handler)
                return

            # Per transport.md: start() called after agent ACK and after
            # send_reply_success() — client starts sending data after the reply.
            handler.start()
            await handler.closed_event.wait()

        except ExecTunnelError as exc:
            ack_status = AckStatus.ERROR
            self._record_ack_failure(conn_id, AckStatus.ERROR)
            self._record_connect_failure(host, AckStatus.ERROR, reply)
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
            # OSError during the connect/ACK/relay sequence means the SOCKS5
            # client socket died — normal race condition, not a tunnel error.
            # Do NOT count as an ACK failure; do NOT log at ERROR.
            # Examples: ConnectionResetError("Connection lost") from drain(),
            # BrokenPipeError from write(), ECONNRESET from the local socket.
            logger.debug(
                "conn %s: SOCKS5 client I/O error during connect: %s",
                conn_id,
                exc,
                extra={"conn_id": conn_id},
            )
            metrics_inc("socks_reply_code", code=int(Reply.GENERAL_FAILURE))
            await self._teardown_failed_connection(conn_id, handler)
            if not req.replied:
                with contextlib.suppress(OSError):
                    await req.send_reply_error(reply)

        except Exception as exc:
            # Truly unexpected — log at ERROR and count as ACK failure.
            ack_status = AckStatus.ERROR
            self._record_ack_failure(conn_id, AckStatus.ERROR)
            self._record_connect_failure(host, AckStatus.ERROR, reply)
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
            # Pop from _pending_connects BEFORE cancelling ack_future so
            # _dispatch_frame cannot race to set_result on a cancelled future.
            self._pending_connects.pop(conn_id, None)
            if not ack_future.done():
                ack_future.cancel()
            self._emit_pending_gauge()
            metrics_observe(
                "tunnel.conn_ack.wait_sec",
                loop.time() - ack_wait_start,
                status=ack_status,
            )

    async def _await_conn_ack(
        self,
        conn_id: str,
        pending: PendingConnect,
        ack_future: asyncio.Future[AckStatus],
    ) -> tuple[AckStatus | None, Reply]:
        """Wait for CONN_ACK, WS close, or timeout.

        Args:
            conn_id:    Connection ID (for metrics labelling only).
            pending:    PendingConnect record — provides ``host`` for metrics.
            ack_future: Future resolved by FrameReceiver on CONN_ACK / ERROR.

        Returns:
            ``(failure_reason, reply)`` where ``failure_reason`` is ``None``
            on success or an ``AckStatus`` member on failure.
        """
        ws_closed_task: asyncio.Task[None] = asyncio.create_task(
            self._ws_closed.wait(),
            name=f"ws-closed-{conn_id}",
        )
        try:
            async with asyncio.timeout(self._tun.conn_ack_timeout):
                done, _ = await asyncio.wait(
                    {ack_future, ws_closed_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
        except TimeoutError:
            metrics_inc("connect_ack_timeout", host=_normalise_host_key(pending.host))
            return AckStatus.TIMEOUT, Reply.HOST_UNREACHABLE
        finally:
            if not ws_closed_task.done():
                ws_closed_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ws_closed_task

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
        """Tear down a handler that never reached the RUNNING state.

        For pre-start handlers, ``close_unstarted()`` is the correct teardown
        path — it self-evicts from the registry.  Never manually pop the
        registry entry here; ``close_unstarted()`` handles eviction.

        For handlers that somehow reached started state (race), ``abort()``
        is used instead.
        """
        if not handler.is_started:
            with contextlib.suppress(OSError, RuntimeError):
                await handler.close_unstarted()
        else:
            # Rare race: start() was called between failure detection and here.
            handler.abort()

    # ── UDP ASSOCIATE ─────────────────────────────────────────────────────────

    async def _handle_udp_associate(self, req: Socks5Request) -> None:
        """Handle a SOCKS5 UDP_ASSOCIATE request."""
        async with req:
            if req.udp_relay is None:
                logger.error(
                    "UDP ASSOCIATE request has no relay — this is a bug in _negotiate()"
                )
                if not req.replied:
                    await req.send_reply_error(Reply.GENERAL_FAILURE)
                return

            relay = req.udp_relay

            # MUST pass local_port — client sends datagrams to this port.
            await req.send_reply_success(
                bind_host="127.0.0.1",
                bind_port=relay.local_port,
            )
            logger.debug("UDP ASSOCIATE local port %d", relay.local_port)

            # Maps (dst_host, dst_port) → (UdpFlow, resolved_src_ip).
            # resolved_src_ip is the IP address to pass to send_to_client()
            # per proxy.md rule 9 — domain names are silently dropped.
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
                except (TransportError, ExecTunnelError) as exc:
                    metrics_inc(
                        "udp.drain.error",
                        error=getattr(exc, "error_code", type(exc).__name__).replace(
                            ".", "_"
                        ),
                    )
                    logger.debug(
                        "udp drain flow=%s [%s]: %s",
                        flow_handler.flow_id,
                        getattr(exc, "error_code", type(exc).__name__),
                        exc,
                    )
                except asyncio.CancelledError:
                    raise
                finally:
                    # Use the captured key — O(1), no linear scan.
                    active_flows.pop(flow_key, None)

            async def _pump_inbound() -> None:
                """Read datagrams from the SOCKS5 relay and forward through tunnel."""
                while True:
                    try:
                        async with asyncio.timeout(Defaults.UDP_PUMP_POLL_TIMEOUT_SECS):
                            result = await relay.recv()
                    except TimeoutError:
                        if req.reader.at_eof():
                            break
                        continue

                    if result is None:
                        break

                    payload, dst_host, dst_port = result

                    if is_host_excluded(dst_host, self._tun.exclude):
                        await _direct_udp_relay(relay, payload, dst_host, dst_port)
                        continue

                    key = (dst_host, dst_port)
                    flow_entry = active_flows.get(key)

                    if flow_entry is None:
                        # Resolve dst_host to an IP address so that
                        # send_to_client() receives a valid IP (proxy.md rule 9).
                        # For IP literals this is a no-op; for domains we resolve
                        # once per (host, port) pair and cache the result.
                        try:
                            src_ip = _require_ip_literal(dst_host)
                        except ValueError:
                            # Domain name — cannot resolve synchronously here.
                            # Use dst_host as-is; send_to_client will drop it
                            # silently if it is not an IP.  Log at DEBUG only.
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
                        # Insert BEFORE open() — agent may reply immediately.
                        self._udp_registry[flow_id] = flow_handler
                        metrics_gauge_inc("session_active_udp_flows")
                        try:
                            await flow_handler.open()
                        except (
                            WebSocketSendTimeoutError,
                            ConnectionClosedError,
                            TransportError,
                            ExecTunnelError,
                        ) as exc:
                            err = getattr(
                                exc, "error_code", type(exc).__name__
                            ).replace(".", "_")
                            metrics_inc("udp.flow.open.error", error=err)
                            logger.warning(
                                "udp flow %s open error [%s] — dropping datagram",
                                flow_id,
                                err,
                            )
                            # Handler self-evicts via on_remote_closed().
                            flow_handler.on_remote_closed()
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
                        err = getattr(exc, "error_code", type(exc).__name__).replace(
                            ".", "_"
                        )
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
                # req.close() is called by async with req __aexit__.

    # ── ACK failure tracking ──────────────────────────────────────────────────

    def _record_ack_failure(self, conn_id: str, reason: AckStatus) -> None:
        """Record an ACK failure and trigger reconnect if threshold is exceeded."""
        self._ack_failure_count += 1
        metrics_inc("tunnel.conn_ack.failed", reason=reason.value)

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

        should_log = (
            self._ack_failure_count == 1
            or self._ack_failure_count % self._tun.ack_timeout_warn_every == 0
            or reason != AckStatus.TIMEOUT
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

        if reason != AckStatus.TIMEOUT:
            return
        if self._ack_reconnect_requested:
            return
        if self._ack_timeout_window_count < self._tun.ack_timeout_reconnect_threshold:
            return

        self._ack_reconnect_requested = True
        metrics_inc("tunnel.conn_ack.reconnect_triggered")
        logger.error(
            "agent appears unhealthy: %d ACK timeouts within %.0fs; forcing reconnect",
            self._ack_timeout_window_count,
            self._tun.ack_timeout_window_secs,
        )

    def reset_ack_state(self) -> None:
        """Reset all ACK failure tracking state after a successful bootstrap.

        Called at the start of each session so that failure counts from a
        previous (failed) session do not inflate totals or suppress log lines
        in the new session.
        """
        self._ack_reconnect_requested = False
        self._ack_timeout_window_start = None
        self._ack_timeout_window_count = 0
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
                            min(Defaults.CONNECT_PACE_JITTER_CAP_SECS, interval / 2.0),
                        )
                    )
            self._host_connect_last_open_at[host_key] = (
                asyncio.get_running_loop().time()
            )

    def _emit_pending_gauge(self) -> None:
        """Emit the current pending-connect count as a gauge metric."""
        metrics_observe("pending_connects", float(len(self._pending_connects)))

    def _record_connect_failure(
        self, host: str, reason: AckStatus, reply: Reply
    ) -> None:
        """Increment per-host failure counter and emit metrics."""
        host_key = _normalise_host_key(host)
        self._connect_failures_by_host[host_key] = (
            self._connect_failures_by_host.get(host_key, 0) + 1
        )
        metrics_inc("connect_fail_by_host", host=host_key, reason=reason.value)
        metrics_inc("socks_reply_code", code=int(reply))


# ── Module-level helpers ──────────────────────────────────────────────────────


def _normalise_host_key(host: str) -> str:
    """Normalise a host string for use as a dict / metrics key."""
    return host.lower().strip()


def _require_ip_literal(host: str) -> str:
    """Return *host* unchanged if it is an IP literal, else raise ``ValueError``.

    Used to validate that ``send_to_client`` receives an IP address per
    proxy.md rule 9.  Domain names raise ``ValueError`` — the caller decides
    how to handle them.

    Args:
        host: An IPv4 or IPv6 address string, or a domain name.

    Returns:
        The host string unchanged if it is a valid IP literal.

    Raises:
        ValueError: If *host* is not a valid IP address.
    """
    ipaddress.ip_address(host)  # raises ValueError for non-IP
    return host


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


async def _direct_udp_relay(
    relay: UdpRelay,
    payload: bytes,
    dst_host: str,
    dst_port: int,
) -> None:
    """Send a single UDP datagram directly (excluded host) and relay the response.

    ``dst_host`` is guaranteed to be an IP literal here — ``is_host_excluded``
    only returns ``True`` for IP addresses, never for domain names.
    """
    # Invariant: is_host_excluded only matches IP literals.
    # Assert here to catch any future regression where a domain reaches this path.
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
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        await loop.sock_connect(sock, (dst_host, dst_port))
        await loop.sock_sendall(sock, payload)
        try:
            async with asyncio.timeout(Defaults.UDP_DIRECT_RECV_TIMEOUT_SECS):
                response = await loop.sock_recv(sock, 65_535)
            relay.send_to_client(response, dst_host, dst_port)
        except TimeoutError:
            pass
    except (OSError, ExecTunnelError):
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
