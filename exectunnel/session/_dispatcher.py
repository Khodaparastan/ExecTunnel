"""SOCKS5 request dispatcher — CONNECT, UDP_ASSOCIATE, and direct pipe.

:class:`RequestDispatcher` is called by
:meth:`~exectunnel.session._session.TunnelSession._accept_loop` for each
accepted :class:`~exectunnel.proxy.TCPRelay`.  It is responsible for:

* Opening tunnel connections / UDP flows.
* Sending the SOCKS5 reply (success or error) exactly once.
* Relaying data until one side closes.
* Sending ``CONN_CLOSE`` / ``UDP_CLOSE`` on teardown.
"""

import asyncio
import contextlib
import ipaddress
import logging
import random
from dataclasses import dataclass
from typing import Final

from exectunnel.defaults import Defaults
from exectunnel.exceptions import (
    ConnectionClosedError,
    ExecTunnelError,
    TransportError,
    WebSocketSendTimeoutError,
)
from exectunnel.observability import (
    aspan,
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
from exectunnel.proxy import TCPRelay, UDPRelay
from exectunnel.transport import TcpConnection, UdpFlow, WsSendCallable

from ._config import TunnelConfig
from ._constants import (
    DIRECT_CONNECT_TIMEOUT_SECS,
    HOST_SEMAPHORE_CAPACITY,
    PIPE_WRITER_CLOSE_TIMEOUT_SECS,
    UDP_ACTIVE_FLOWS_CAP,
    UDP_MAX_DATAGRAM_SIZE,
)
from ._lru import LruDict
from ._routing import is_host_excluded
from ._state import AckStatus, PendingConnect
from ._types import AgentRecentlyActiveCallable, ReconnectCallable
from ._udp_socket import make_udp_socket

logger = logging.getLogger(__name__)

# Compiled regex for sanitising task-name host components.
_SAFE_HOST_RE: Final = __import__("re").compile(r"[^a-zA-Z0-9_.\-]")


@dataclass(slots=True)
class _ActiveUdpFlow:
    handler: UdpFlow
    src_ip: str
    dst_port: int
    last_used_at: float


class _HostGate:
    """Ref-counted wrapper around an :class:`asyncio.Semaphore` for per-host gating.

    The gate is inserted into :class:`_HostGateRegistry` and removed only when
    the last holder releases it, guaranteeing that the per-host cap is never
    breached by eviction races under sustained fan-out.

    Args:
        limit: Maximum number of concurrent holders of this gate.
    """

    __slots__ = ("sem", "refs")

    def __init__(self, limit: int) -> None:
        self.sem: asyncio.Semaphore = asyncio.Semaphore(limit)
        self.refs: int = 0


class _HostGateRegistry:
    """Registry of ref-counted per-host semaphores.

    Unlike an LRU-bounded mapping, entries here are *pinned* for as long as
    any coroutine holds the gate — eviction of in-use entries would reset
    the semaphore's counter and temporarily exceed ``limit``.

    Idle entries (``refs == 0``) are eligible for collection.  To keep the
    working set bounded in pathological many-host workloads, the caller
    prunes idle entries when the total size exceeds *max_size*.

    Args:
        limit:    Per-host concurrency limit (semaphore capacity).
        max_size: Maximum number of entries before idle pruning is triggered.
    """

    __slots__ = ("_entries", "_limit", "_max_size")

    def __init__(self, limit: int, max_size: int = HOST_SEMAPHORE_CAPACITY) -> None:
        self._entries: dict[str, _HostGate] = {}
        self._limit = limit
        self._max_size = max_size

    def __len__(self) -> int:
        return len(self._entries)

    def _prune_idle(self) -> None:
        """Drop idle entries (``refs == 0``) in insertion order until under cap."""
        if len(self._entries) <= self._max_size:
            return
        for key in list(self._entries.keys()):
            if len(self._entries) <= self._max_size:
                return
            entry = self._entries[key]
            if entry.refs == 0:
                del self._entries[key]

    def acquire_gate(self, key: str) -> _HostGate:
        """Return the gate for *key*, creating it if absent, and increment its refcount.

        Args:
            key: Normalised host key (see :func:`_normalise_host_key`).

        Returns:
            The :class:`_HostGate` for *key* with its refcount incremented.
        """
        entry = self._entries.get(key)
        if entry is None:
            entry = _HostGate(self._limit)
            self._entries[key] = entry
            self._prune_idle()
        entry.refs += 1
        return entry

    def release_gate(self, key: str) -> None:
        """Decrement the refcount for *key* and delete the entry when idle.

        Args:
            key: Normalised host key previously passed to :meth:`acquire_gate`.
        """
        entry = self._entries.get(key)
        if entry is None:
            return
        entry.refs -= 1
        if entry.refs <= 0:
            # Only safe to drop when nobody holds or waits — refs covers both,
            # since callers increment before awaiting ``sem.acquire()``.
            del self._entries[key]


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
        pre_ack_buffer_cap_bytes: Pre-ACK buffer cap forwarded to
                                  :class:`~exectunnel.transport.TcpConnection`.
        request_reconnect:        Optional callback to force a session reconnect.
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
        "_request_reconnect",
        "_agent_recently_active",
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
        request_reconnect: ReconnectCallable | None = None,
        agent_recently_active: AgentRecentlyActiveCallable | None = None,
    ) -> None:
        self._tun = tun_cfg
        self._ws_send = ws_send
        self._ws_closed = ws_closed
        self._tcp_registry = tcp_registry
        self._pending_connects = pending_connects
        self._udp_registry = udp_registry
        self._pre_ack_buffer_cap_bytes = pre_ack_buffer_cap_bytes

        self._connect_gate = asyncio.Semaphore(tun_cfg.connect_max_pending)
        # Ref-counted per-host gates: entries are pinned while any holder or
        # waiter exists, guaranteeing strict ``connect_max_pending_per_host``
        # enforcement even under pathological host cardinality.
        self._host_semaphores: _HostGateRegistry = _HostGateRegistry(
            tun_cfg.connect_max_pending_per_host
        )
        self._host_connect_open_locks: LruDict[str, asyncio.Lock] = LruDict(
            HOST_SEMAPHORE_CAPACITY
        )
        self._host_connect_last_open_at: LruDict[str, float] = LruDict(
            HOST_SEMAPHORE_CAPACITY
        )
        self._connect_failures_by_host: LruDict[str, int] = LruDict(
            HOST_SEMAPHORE_CAPACITY
        )

        self._ack_failure_count: int = 0
        self._ack_failure_suppressed: int = 0
        self._ack_timeout_window_start: float | None = None
        self._ack_timeout_window_count: int = 0
        self._ack_agent_error_window_start: float | None = None
        self._ack_agent_error_window_count: int = 0
        self._ack_reconnect_requested: bool = False
        self._request_reconnect = request_reconnect
        self._agent_recently_active = agent_recently_active

        # Created lazily on first use by :meth:`_get_ws_closed_task` so that
        # ``__init__`` can be called outside a running event loop.
        self._ws_closed_task: asyncio.Task[bool] | None = None

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def _get_ws_closed_task(self) -> asyncio.Task[bool]:
        """Lazily create the shared ``ws_closed.wait()`` sentinel task.

        Creating it on first use (inside :meth:`_await_conn_ack`) guarantees
        an event loop is running, avoiding the ``RuntimeError`` that would
        otherwise be raised if :class:`RequestDispatcher` is constructed
        before the loop starts.

        Returns:
            A running :class:`asyncio.Task` that resolves when ``ws_closed``
            is set.
        """
        task = self._ws_closed_task
        if task is None or task.done():
            task = asyncio.get_running_loop().create_task(
                self._ws_closed.wait(),
                name="ws-closed-sentinel",
            )
            self._ws_closed_task = task
        return task

    def close(self) -> None:
        """Cancel the shared ``_ws_closed_task`` sentinel, if it was created.

        Must be called when the session is torn down to prevent the sentinel
        task from leaking.  Safe to call multiple times.
        """
        task = self._ws_closed_task
        if task is not None and not task.done():
            task.cancel()

    # ── Public dispatch ───────────────────────────────────────────────────────

    async def dispatch(self, req: TCPRelay) -> None:
        """Dispatch a SOCKS5 request to the appropriate handler.

        Args:
            req: Completed SOCKS5 handshake to dispatch.
        """
        if req.is_connect:
            async with aspan("socks.connect", host=req.host, port=req.port):
                await self._handle_connect(req)
        elif req.is_udp_associate:
            async with aspan("socks.udp_associate"):
                await self._handle_udp_associate(req)
        elif not req.replied:
            await req.send_reply_error(Reply.CMD_NOT_SUPPORTED)

    # ── CONNECT ───────────────────────────────────────────────────────────────

    async def _handle_connect(self, req: TCPRelay) -> None:
        """Handle a SOCKS5 CONNECT request.

        Args:
            req: Completed SOCKS5 handshake with CONNECT command.
        """
        async with req:
            host, port = req.host, req.port
            if is_host_excluded(host, self._tun.exclude):
                await self._handle_direct_connect(req, host, port)
                return
            await self._handle_tunnel_connect(req, host, port)

    @staticmethod
    async def _handle_direct_connect(
        req: TCPRelay,
        host: str,
        port: int,
    ) -> None:
        """Open a direct TCP connection for excluded hosts.

        Args:
            req:  Completed SOCKS5 handshake (lifecycle owned by caller).
            host: Destination hostname or IP.
            port: Destination port.
        """
        async with aspan("socks.connect.direct", host=host, port=port):
            logger.debug("direct connect %s:%d (excluded)", host, port)
            try:
                async with asyncio.timeout(DIRECT_CONNECT_TIMEOUT_SECS):
                    rem_reader, rem_writer = await asyncio.open_connection(host, port)
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
        req: TCPRelay,
        host: str,
        port: int,
    ) -> None:
        """Open a tunnel CONNECT and relay data.

        The request lifecycle (``async with req:``) is owned by the caller
        :meth:`_handle_connect`.  This method must not re-enter the context manager.

        ``CONN_OPEN`` is sent as a control frame (``control=True``) to guarantee
        it is never dropped by the bounded data queue under backpressure.  The
        send enqueues synchronously to the unbounded control queue and never
        raises :exc:`~exectunnel.exceptions.WebSocketSendTimeoutError` or
        :exc:`~exectunnel.exceptions.ConnectionClosedError` at the call site —
        those failures surface through ``_ws_closed_task`` resolving in
        :meth:`_await_conn_ack`.

        The ``session.active.tcp_connections`` gauge is incremented here on
        registration and decremented by the transport layer on registry eviction.

        Args:
            req:  Completed SOCKS5 handshake (lifecycle owned by caller).
            host: Destination hostname or IP.
            port: Destination port.
        """
        host_key, host_gate_entry = self._acquire_host_gate(host)
        host_gate = host_gate_entry.sem
        loop = asyncio.get_running_loop()

        conn_id: str | None = None
        handler: TcpConnection | None = None
        ack_future: asyncio.Future[AckStatus] | None = None

        ack_wait_start = loop.time()
        ack_status: AckStatus = AckStatus.OK
        failure_reason: AckStatus | None = None
        reply = Reply.HOST_UNREACHABLE
        registered = False

        try:
            # Hold both semaphores across the *entire* CONN_OPEN -> CONN_ACK
            # interval.  This makes ``connect_max_pending`` and
            # ``connect_max_pending_per_host`` real pending-ACK caps, not
            # merely send-burst caps.
            async with self._connect_gate, host_gate:
                await self._pace_connection(host_key)

                conn_id = new_conn_id()
                ack_future = loop.create_future()

                handler = TcpConnection(
                    conn_id,
                    req.reader,
                    req.writer,
                    self._ws_send,
                    self._tcp_registry,
                    pre_ack_buffer_cap_bytes=self._pre_ack_buffer_cap_bytes,
                )
                pending = PendingConnect(
                    host=host,
                    port=port,
                    ack_future=ack_future,
                )

                # Register immediately *before* sending CONN_OPEN so a fast
                # ACK cannot race ahead of registry insertion.  Registration
                # only happens once the gates are held — locally queued
                # requests are no longer counted as agent-pending.
                self._tcp_registry[conn_id] = handler
                metrics_gauge_inc("session.active.tcp_connections")
                self._pending_connects[conn_id] = pending
                registered = True
                self._emit_pending_gauge()

                async with aspan(
                    "tunnel.conn_open",
                    conn_id=conn_id,
                    host=host,
                    port=port,
                ):
                    # control=True: enqueues to the unbounded priority queue —
                    # never dropped, never raises a send-level exception.
                    await self._ws_send(
                        encode_conn_open_frame(conn_id, host, port),
                        control=True,
                    )
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

                # Keep gates held until ACK resolves or times out.
                async with aspan("tunnel.conn_ack.wait", conn_id=conn_id):
                    failure_reason, reply = await self._await_conn_ack(
                        conn_id,
                        pending,
                        ack_future,
                    )

            # Gates released here, after ACK wait.

            if failure_reason is not None:
                ack_status = failure_reason
                self._record_ack_failure(
                    conn_id,
                    failure_reason,
                    host=host,
                    port=port,
                )
                self._record_connect_failure(host, failure_reason, reply)
                if handler is not None:
                    await self._teardown_failed_connection(conn_id, handler)
                if not req.replied:
                    await req.send_reply_error(reply)
                return

            metrics_inc("tunnel.conn_ack.ok", conn_id=conn_id, host=host, port=port)

            try:
                await req.send_reply_success()
            except OSError as exc:
                logger.debug(
                    "conn %s: SOCKS5 client closed before reply: %s",
                    conn_id,
                    exc,
                    extra={"conn_id": conn_id},
                )
                metrics_inc("socks.reply", code=int(Reply.GENERAL_FAILURE))
                if handler is not None:
                    await self._teardown_failed_connection(conn_id, handler)
                return

            metrics_inc("socks.reply", code=int(Reply.SUCCESS))

            if handler is None:
                return

            handler.start()
            await handler.closed_event.wait()
            metrics_inc("tunnel.conn.completed", conn_id=conn_id, host=host, port=port)

        except ExecTunnelError as exc:
            ack_status = AckStatus.LIBRARY_ERROR
            reply = Reply.GENERAL_FAILURE
            if conn_id is not None:
                self._record_ack_failure(
                    conn_id, AckStatus.LIBRARY_ERROR, host=host, port=port
                )
                self._record_connect_failure(host, AckStatus.LIBRARY_ERROR, reply)
            metrics_inc(
                "tunnel.conn.error",
                conn_id=conn_id or "",
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
            if conn_id is not None and handler is not None:
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
            if conn_id is not None and handler is not None:
                await self._teardown_failed_connection(conn_id, handler)
            if not req.replied:
                with contextlib.suppress(OSError):
                    await req.send_reply_error(Reply.GENERAL_FAILURE)

        except Exception as exc:
            ack_status = AckStatus.UNEXPECTED_ERROR
            reply = Reply.GENERAL_FAILURE
            if conn_id is not None:
                self._record_ack_failure(
                    conn_id, AckStatus.UNEXPECTED_ERROR, host=host, port=port
                )
                self._record_connect_failure(host, AckStatus.UNEXPECTED_ERROR, reply)
            metrics_inc(
                "tunnel.conn.error",
                conn_id=conn_id or "",
                host=host,
                port=port,
                error="unexpected",
            )
            logger.error(
                "conn %s: unexpected error during tunnel connect: %s",
                conn_id,
                exc,
                exc_info=True,
                extra={"conn_id": conn_id, "host": host, "port": port},
            )
            if conn_id is not None and handler is not None:
                await self._teardown_failed_connection(conn_id, handler)
            if not req.replied:
                await req.send_reply_error(reply)

        finally:
            if conn_id is not None:
                self._pending_connects.pop(conn_id, None)
            if ack_future is not None and not ack_future.done():
                ack_future.cancel()
            if registered:
                self._emit_pending_gauge()
                metrics_observe(
                    "tunnel.conn_ack.wait_sec",
                    loop.time() - ack_wait_start,
                    status=ack_status.value,
                    host=host_key,
                )
            # Release the per-host gate reference acquired at the top of the
            # method.  Must happen after every code path — success, ACK failure,
            # exceptions, cancellation — to keep refcount balance exact.
            self._release_host_gate(host_key)

    async def _await_conn_ack(
        self,
        conn_id: str,
        pending: PendingConnect,
        ack_future: asyncio.Future[AckStatus],
    ) -> tuple[AckStatus | None, Reply]:
        """Wait for ``CONN_ACK``, WebSocket close, or timeout.

        ``ack_future`` is checked before ``ws_closed_task`` when both resolve
        in the same event-loop iteration so a valid ACK is never discarded due
        to a concurrent close signal.

        Args:
            conn_id:    Connection ID for metrics tags.
            pending:    Pending connect record for metrics tags.
            ack_future: Future resolved by :class:`~exectunnel.session._receiver.FrameReceiver`
                        on ACK or error.

        Returns:
            A ``(failure_reason, reply)`` tuple where *failure_reason* is
            ``None`` on success or an :class:`~exectunnel.session._state.AckStatus`
            member on failure.
        """
        ws_closed_task = self._get_ws_closed_task()
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

        # Check ack_future FIRST: when both futures resolve in the same
        # event-loop tick a valid ACK must take precedence over ws_closed.
        if ack_future in done:
            try:
                ack_result = ack_future.result()
            except asyncio.CancelledError:
                return AckStatus.WS_CLOSED, Reply.NET_UNREACHABLE
            if ack_result == AckStatus.OK:
                return None, Reply.SUCCESS
            return ack_result, Reply.HOST_UNREACHABLE

        return AckStatus.WS_CLOSED, Reply.NET_UNREACHABLE

    async def _teardown_failed_connection(
        self,
        conn_id: str,
        handler: TcpConnection,
    ) -> None:
        """Tear down a handler that never reached the running state.

        Args:
            conn_id: Connection ID for log context.
            handler: The :class:`~exectunnel.transport.TcpConnection` to tear down.
        """
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

    async def _handle_udp_associate(self, req: TCPRelay) -> None:
        """Handle a SOCKS5 UDP_ASSOCIATE request.

        Args:
            req: Completed SOCKS5 handshake with UDP_ASSOCIATE command.
        """
        async with req:
            if req.udp_relay is None:
                logger.error(
                    "UDP ASSOCIATE request has no relay — this is a bug in _negotiate()"
                )
                if not req.replied:
                    await req.send_reply_error(Reply.GENERAL_FAILURE)
                return

            relay = req.udp_relay

            try:
                await req.send_reply_success(
                    bind_host=relay.advertise_host,
                    bind_port=relay.local_port,
                )
            except OSError as exc:
                logger.debug(
                    "UDP ASSOCIATE: SOCKS5 client closed before reply: %s", exc
                )
                relay.close()
                return

            logger.debug("UDP ASSOCIATE local port %d", relay.local_port)

            # Maps (dst_host, dst_port) → active UDP flow state.
            active_flows: dict[tuple[str, int], _ActiveUdpFlow] = {}
            drain_tasks: set[asyncio.Task[None]] = set()

            await self._run_udp_pump(relay, req, active_flows, drain_tasks)

    async def _drain_udp_flow(
        self,
        flow_handler: UdpFlow,
        relay: UDPRelay,
        src_ip: str,
        dst_port: int,
        flow_key: tuple[str, int],
        active_flows: dict[tuple[str, int], _ActiveUdpFlow],
    ) -> None:
        """Drain inbound datagrams from a :class:`~exectunnel.transport.UdpFlow` to the relay client.

        Args:
            flow_handler: The UDP flow to drain.
            relay:        The SOCKS5 UDP relay to forward responses to.
            src_ip:       Source IP to report to the relay client.
            dst_port:     Destination port for the relay client header.
            flow_key:     Key to remove from *active_flows* on exit.
            active_flows: Shared mapping of active flows for this session.
        """
        try:
            while (data := await flow_handler.recv_datagram()) is not None:
                entry = active_flows.get(flow_key)
                if entry is not None:
                    entry.last_used_at = asyncio.get_running_loop().time()
                relay.send_to_client(data, src_ip, dst_port)
        except OSError:
            metrics_inc("udp.drain.error", error="os_error")
        except (TransportError, ExecTunnelError) as exc:
            err = getattr(exc, "error_code", type(exc).__name__).replace(".", "_")
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
            entry = active_flows.get(flow_key)
            if entry is not None and entry.handler is flow_handler:
                active_flows.pop(flow_key, None)
            with contextlib.suppress(ExecTunnelError, OSError):
                await flow_handler.close()

    async def _run_udp_pump(
        self,
        relay: UDPRelay,
        req: TCPRelay,
        active_flows: dict[tuple[str, int], _ActiveUdpFlow],
        drain_tasks: set[asyncio.Task[None]],
    ) -> None:
        """Read datagrams from the SOCKS5 relay and forward via tunnel.

        Args:
            relay:        The SOCKS5 UDP relay to read from.
            req:          The originating TCP relay (used for EOF detection).
            active_flows: Shared mapping of active flows for this session.
            drain_tasks:  Set of active drain tasks for cleanup.
        """
        try:
            while True:
                await self._reap_idle_udp_flows(active_flows)

                try:
                    async with asyncio.timeout(self._tun.udp_pump_poll_timeout):
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
                    flow_handler, src_ip = await self._open_udp_flow(
                        dst_host, dst_port, key, active_flows, drain_tasks, relay
                    )
                    if flow_handler is None:
                        continue
                else:
                    flow_handler = flow_entry.handler
                    src_ip = flow_entry.src_ip

                try:
                    await flow_handler.send_datagram(payload)
                    entry = active_flows.get(key)
                    if entry is not None:
                        entry.last_used_at = asyncio.get_running_loop().time()
                except (
                    WebSocketSendTimeoutError,
                    ConnectionClosedError,
                    TransportError,
                    ExecTunnelError,
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

        finally:
            for entry in list(active_flows.values()):
                h = entry.handler
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

    async def _reap_idle_udp_flows(
        self,
        active_flows: dict[tuple[str, int], _ActiveUdpFlow],
    ) -> None:
        """Close UDP flows that have been idle for longer than the timeout.

        Args:
            active_flows: Shared mapping of active flows for this session.
        """
        timeout = self._tun.udp_flow_idle_timeout
        if timeout <= 0 or not active_flows:
            return

        now = asyncio.get_running_loop().time()
        for key, entry in list(active_flows.items()):
            if now - entry.last_used_at < timeout:
                continue

            active_flows.pop(key, None)
            metrics_inc("udp.flow.idle_reaped")
            logger.debug(
                "udp flow %s:%d idle for %.1fs — closing",
                key[0],
                key[1],
                now - entry.last_used_at,
            )
            with contextlib.suppress(
                WebSocketSendTimeoutError,
                TransportError,
                ExecTunnelError,
                OSError,
            ):
                await entry.handler.close()

    async def _open_udp_flow(
        self,
        dst_host: str,
        dst_port: int,
        key: tuple[str, int],
        active_flows: dict[tuple[str, int], _ActiveUdpFlow],
        drain_tasks: set[asyncio.Task[None]],
        relay: UDPRelay,
    ) -> tuple[UdpFlow | None, str]:
        """Open a new :class:`~exectunnel.transport.UdpFlow` for a UDP destination.

        Enforces the per-session active-flows cap by evicting the oldest flow
        when the cap is reached.

        Args:
            dst_host:     Destination hostname or IP.
            dst_port:     Destination UDP port.
            key:          ``(dst_host, dst_port)`` tuple used as the flow key.
            active_flows: Shared mapping of active flows for this session.
            drain_tasks:  Set of active drain tasks for cleanup.
            relay:        The SOCKS5 UDP relay (used for drain task creation).

        Returns:
            A ``(flow_handler, src_ip)`` tuple on success, or ``(None, "")``
            if the flow could not be opened.
        """
        # Enforce a per-session cap on concurrent UDP flows by evicting the
        # oldest flow (insertion order) when at the cap.
        if len(active_flows) >= UDP_ACTIVE_FLOWS_CAP:
            oldest_key = next(iter(active_flows))
            oldest_entry = active_flows.pop(oldest_key)
            metrics_inc("udp.flow.evicted_over_cap")
            logger.info(
                "udp flows at cap=%d — evicting oldest %s:%d",
                UDP_ACTIVE_FLOWS_CAP,
                oldest_key[0],
                oldest_key[1],
            )
            with contextlib.suppress(
                WebSocketSendTimeoutError,
                TransportError,
                ExecTunnelError,
                OSError,
            ):
                await oldest_entry.handler.close()

        try:
            src_ip = _require_ip_literal(dst_host)
        except ValueError:
            # Production requirement: UDPRelay.send_to_client() must support
            # SOCKS5 DOMAINNAME ATYP for this path. If it does not, UDP
            # responses for domain destinations will be lost.
            logger.debug(
                "udp flow %s:%d uses domain-name source in SOCKS5 UDP response",
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
            err = getattr(exc, "error_code", type(exc).__name__).replace(".", "_")
            metrics_inc("udp.flow.open.error", error=err)
            logger.warning(
                "udp flow %s open error [%s] — dropping datagram",
                flow_id,
                err,
            )
            with contextlib.suppress(ExecTunnelError, OSError):
                await flow_handler.close()
            return None, ""

        active_flows[key] = _ActiveUdpFlow(
            handler=flow_handler,
            src_ip=src_ip,
            dst_port=dst_port,
            last_used_at=asyncio.get_running_loop().time(),
        )
        task = asyncio.create_task(
            self._drain_udp_flow(
                flow_handler, relay, src_ip, dst_port, key, active_flows
            ),
            name=f"udp-drain-{flow_id}",
        )
        drain_tasks.add(task)
        task.add_done_callback(drain_tasks.discard)
        return flow_handler, src_ip

    # ── ACK failure tracking ──────────────────────────────────────────────────

    def _record_ack_failure(
        self,
        conn_id: str,
        reason: AckStatus,
        *,
        host: str = "",
        port: int = 0,
    ) -> None:
        """Record an ACK failure and trigger reconnect if the threshold is exceeded.

        Args:
            conn_id: Connection ID for metrics tags.
            reason:  The failure reason.
            host:    Destination host for metrics tags.
            port:    Destination port for metrics tags.
        """
        # WS_CLOSED ACK failures are pure teardown noise — the WebSocket has
        # already been declared closed and any in-flight CONN_OPEN naturally
        # cannot ACK.  Suppress them at the source instead of logging each
        # one and never let them feed the health-reconnect counters.
        if reason == AckStatus.WS_CLOSED:
            self._ack_failure_suppressed += 1
            metrics_inc(
                "tunnel.conn_ack.failed_after_ws_closed",
                conn_id=conn_id,
                host=host,
                port=port,
            )
            return

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

        # Only ACK timeouts can trigger forced reconnect.  AGENT_ERROR means
        # the agent is alive enough to report errors per-connection — that
        # is not a session-health signal and must never tear the tunnel down.
        if reason != AckStatus.TIMEOUT:
            return
        if self._ack_reconnect_requested:
            return

        window_count = self._ack_timeout_window_count
        if window_count < self._tun.ack_timeout_reconnect_threshold:
            return

        # If the agent has emitted any inbound frame within the configured
        # grace window, the tunnel is still alive and what we are observing
        # is congestion / stdout head-of-line blocking, not a wedged agent.
        # Suppress the forced reconnect in that case.
        if self._agent_recently_active is not None and self._agent_recently_active():
            metrics_inc("tunnel.conn_ack.reconnect_suppressed_agent_active")
            logger.warning(
                "agent ACK timeout threshold reached (%d within %.0fs), but "
                "agent is still sending frames; suppressing forced reconnect "
                "and treating this as tunnel congestion",
                window_count,
                self._tun.ack_timeout_window_secs,
            )
            return

        self._ack_reconnect_requested = True
        metrics_inc("tunnel.conn_ack.reconnect_triggered")
        logger.error(
            "agent appears unhealthy: %d ACK timeouts within %.0fs and no "
            "recent agent activity; forcing reconnect",
            window_count,
            self._tun.ack_timeout_window_secs,
        )
        if self._request_reconnect is not None:
            asyncio.create_task(
                self._request_reconnect(f"ack_health:{reason.value}"),
                name="request-reconnect",
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

    def _acquire_host_gate(self, host: str) -> tuple[str, _HostGate]:
        """Ref-count and return the per-host concurrency gate.

        The caller **must** pair this with :meth:`_release_host_gate` in a
        ``try/finally`` to guarantee refcount balance even on cancellation.

        Args:
            host: Destination hostname or IP.

        Returns:
            A ``(host_key, gate)`` tuple where *host_key* is the normalised
            host string and *gate* is the pinned :class:`_HostGate`.
        """
        key = _normalise_host_key(host)
        gate = self._host_semaphores.acquire_gate(key)
        return key, gate

    def _release_host_gate(self, key: str) -> None:
        """Release the per-host gate reference acquired by :meth:`_acquire_host_gate`.

        Args:
            key: Normalised host key returned by :meth:`_acquire_host_gate`.
        """
        self._host_semaphores.release_gate(key)

    async def _pace_connection(self, host_key: str) -> None:
        """Enforce per-host connection pacing with jitter.

        Args:
            host_key: Normalised host key from :func:`_normalise_host_key`.
        """
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
                        )
                    )
            self._host_connect_last_open_at[host_key] = (
                asyncio.get_running_loop().time()
            )

    def _emit_pending_gauge(self) -> None:
        """Emit the current pending-connect count as a gauge metric."""
        metrics_gauge_set(
            "session.pending_connects", float(len(self._pending_connects))
        )

    def _record_connect_failure(
        self,
        host: str,
        reason: AckStatus,
        reply: Reply,
    ) -> None:
        """Increment the per-host failure counter and emit metrics.

        Args:
            host:   Destination hostname or IP.
            reason: The failure reason.
            reply:  The SOCKS5 reply code sent to the client.
        """
        host_key = _normalise_host_key(host)
        self._connect_failures_by_host[host_key] = (
            self._connect_failures_by_host.get(host_key, 0) + 1
        )
        metrics_inc(
            "tunnel.connect.fail_by_host",
            host=host_key,
            reason=reason.value,
        )
        metrics_inc("socks.reply", code=int(reply))

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
    """Normalise a host string for use as a dict or metrics key.

    Args:
        host: An IPv4/IPv6 address string or domain name.

    Returns:
        Compressed IP notation for addresses, or lowercased and stripped domain.
    """
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        return host.lower().strip()


def _require_ip_literal(host: str) -> str:
    """Return *host* unchanged if it is an IP literal, else raise ``ValueError``.

    Args:
        host: String to validate as an IP address.

    Returns:
        The original *host* string.

    Raises:
        ValueError: If *host* is not a valid IP address.
    """
    ipaddress.ip_address(host)
    return host


async def _pipe(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    remote_reader: asyncio.StreamReader,
    remote_writer: asyncio.StreamWriter,
) -> None:
    """Bidirectional byte copy for directly-connected (excluded) hosts.

    Uses :class:`asyncio.TaskGroup` for half-close semantics: when one
    direction hits EOF the other continues until it also finishes.

    Args:
        client_reader: Stream from the local SOCKS5 client.
        client_writer: Stream to the local SOCKS5 client.
        remote_reader: Stream from the remote host.
        remote_writer: Stream to the remote host.
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
                async with asyncio.timeout(PIPE_WRITER_CLOSE_TIMEOUT_SECS):
                    await writer.wait_closed()


async def _direct_udp_relay(
    relay: UDPRelay,
    payload: bytes,
    dst_host: str,
    dst_port: int,
    recv_timeout: float = Defaults.UDP_DIRECT_RECV_TIMEOUT_SECS,
) -> None:
    """Send a single UDP datagram directly (excluded host) and relay the response.

    ``dst_host`` is guaranteed to be an IP literal — :func:`~exectunnel.session._routing.is_host_excluded`
    only returns ``True`` for IP addresses, never for domain names.

    Only the first response datagram is relayed back to the client.

    Args:
        relay:        The SOCKS5 UDP relay to send the response to.
        payload:      The UDP datagram payload to forward.
        dst_host:     Destination IP address (guaranteed IP literal).
        dst_port:     Destination UDP port.
        recv_timeout: Seconds to wait for a response before giving up.
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

        sent = await loop.sock_sendto(sock, payload, (dst_host, dst_port))
        if sent is not None and sent != len(payload):
            metrics_inc("udp.direct.short_send")
            logger.debug(
                "udp direct relay short send to %s:%d: sent=%s expected=%d",
                dst_host,
                dst_port,
                sent,
                len(payload),
            )
            return

        try:
            async with asyncio.timeout(recv_timeout):
                response, _addr = await loop.sock_recvfrom(sock, UDP_MAX_DATAGRAM_SIZE)
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
