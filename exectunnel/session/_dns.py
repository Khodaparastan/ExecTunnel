"""DNS forwarder — forwards DNS queries through the tunnel via UDP flows.

Belongs to the ``session`` layer because it orchestrates transport-layer
primitives (:class:`~exectunnel.transport.UdpFlow`) in the same way the
session orchestrates TCP connections.
"""

import asyncio
import contextlib
import logging

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
from exectunnel.protocol import new_flow_id
from exectunnel.transport import UdpFlow, WsSendCallable

logger = logging.getLogger(__name__)


class _DnsDatagramProtocol(asyncio.DatagramProtocol):
    """asyncio datagram protocol that dispatches received DNS queries to :class:`DnsForwarder`.

    Args:
        forwarder: The :class:`DnsForwarder` instance that owns this protocol.
    """

    __slots__ = ("_forwarder",)

    def __init__(self, forwarder: "DnsForwarder") -> None:
        self._forwarder = forwarder

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Called by asyncio when the endpoint is ready.

        Args:
            transport: The transport created by asyncio.  Must be a
                       :class:`asyncio.DatagramTransport`.
        """
        if not isinstance(transport, asyncio.DatagramTransport):
            logger.error(
                "dns forwarder: expected DatagramTransport, got %s — "
                "forwarder will not function correctly.",
                type(transport).__name__,
            )
            metrics_inc("dns.forwarder.error", error="bad_transport_type")
            transport.close()
            return
        self._forwarder.on_transport_ready(transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Called by asyncio when a datagram is received.

        Args:
            data: Raw datagram bytes.
            addr: ``(host, port)`` of the sender.
        """
        self._forwarder.on_query(data, addr)

    def error_received(self, exc: Exception) -> None:
        """Called by asyncio on a non-fatal socket error.

        Args:
            exc: The exception reported by the OS.
        """
        metrics_inc(
            "dns.forwarder.error",
            error="socket_error",
            reason=type(exc).__name__,
        )
        logger.debug("dns forwarder socket error [%s]: %s", type(exc).__name__, exc)

    def connection_lost(self, exc: Exception | None) -> None:
        """Called by asyncio when the endpoint is closed.

        Args:
            exc: The exception that caused the closure, or ``None`` for a
                 clean close.
        """
        if exc is not None:
            logger.debug("dns forwarder connection lost: %s", exc)


class DnsForwarder:
    """Minimal DNS UDP forwarder that routes queries through the tunnel.

    Listens locally on ``127.0.0.1``; forwards each query as a UDP flow
    through the tunnel to ``upstream:upstream_port`` and returns the response
    to the client.

    Each query opens a dedicated :class:`~exectunnel.transport.UdpFlow`,
    sends the query datagram, waits for exactly one response datagram, then
    closes the flow.

    Args:
        local_port:    Port to bind the local UDP socket on ``127.0.0.1``.
        upstream:      IP or hostname of the upstream DNS server.
        ws_send:       Coroutine callable conforming to ``WsSendCallable``.
        udp_registry:  Shared mutable dict mapping flow IDs to
                       :class:`~exectunnel.transport.UdpFlow` instances.
        max_inflight:  Maximum number of concurrent in-flight DNS queries.
        upstream_port: UDP port of the upstream DNS server.  Defaults to 53.
        query_timeout: Seconds to wait for a DNS response before timing out.
    """

    __slots__ = (
        "_local_port",
        "_upstream",
        "_upstream_port",
        "_ws_send",
        "_udp_registry",
        "_max_inflight",
        "_query_timeout",
        "_transport",
        "_tasks",
        "_started",
        "_stopped",
        "_saturation_drop_count",
        "_error_drop_count",
        "_total_drop_count",
        "_query_count",
        "_ok_count",
        "_bytes_in",
        "_bytes_out",
    )

    def __init__(
        self,
        local_port: int,
        upstream: str,
        ws_send: WsSendCallable,
        udp_registry: dict[str, UdpFlow],
        max_inflight: int = Defaults.DNS_MAX_INFLIGHT,
        upstream_port: int = Defaults.DNS_UPSTREAM_PORT,
        query_timeout: float = Defaults.DNS_QUERY_TIMEOUT_SECS,
    ) -> None:
        self._local_port = local_port
        self._upstream = upstream
        self._upstream_port = upstream_port
        self._ws_send = ws_send
        self._udp_registry = udp_registry
        self._max_inflight = max(1, max_inflight)
        self._query_timeout = query_timeout
        self._transport: asyncio.DatagramTransport | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._started: bool = False
        self._stopped: bool = False
        self._saturation_drop_count: int = 0
        self._error_drop_count: int = 0
        self._total_drop_count: int = 0
        self._query_count: int = 0
        self._ok_count: int = 0
        self._bytes_in: int = 0
        self._bytes_out: int = 0

    # ── Async context manager ─────────────────────────────────────────────────

    async def __aenter__(self) -> "DnsForwarder":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    # ── Package-internal callbacks ────────────────────────────────────────────

    def on_transport_ready(self, transport: asyncio.DatagramTransport) -> None:
        """Store the datagram transport once the endpoint is ready.

        Called by :class:`_DnsDatagramProtocol` when asyncio confirms the
        endpoint is bound and ready to receive datagrams.

        Args:
            transport: The datagram transport created by asyncio.
        """
        self._transport = transport

    def on_query(self, data: bytes, addr: tuple[str, int]) -> None:
        """Dispatch a DNS query received on the local socket.

        Drops the query with a metric increment if the forwarder has been
        stopped or the in-flight limit is reached.

        Args:
            data: Raw DNS query bytes.
            addr: ``(host, port)`` of the querying client.
        """
        if self._stopped:
            metrics_inc("dns.query.drop", reason="after_stop")
            return

        metrics_inc("dns.query.received")

        if len(self._tasks) >= self._max_inflight:
            self._saturation_drop_count += 1
            self._total_drop_count += 1
            metrics_inc("dns.query.drop", reason="saturated")
            if (
                self._saturation_drop_count == 1
                or self._saturation_drop_count % Defaults.UDP_WARN_EVERY == 0
            ):
                logger.warning(
                    "dns forwarder saturated (%d inflight), dropping query "
                    "from %s:%d (saturation_drops=%d)",
                    self._max_inflight,
                    addr[0],
                    addr[1],
                    self._saturation_drop_count,
                )
            return

        flow_id = new_flow_id()
        task = asyncio.create_task(
            self._forward_query(data, addr, flow_id),
            name=f"dns-fwd-{addr[0]}_{addr[1]}-{flow_id[:8]}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind the local UDP socket and begin accepting DNS queries.

        Idempotent — subsequent calls are no-ops.

        Raises:
            TransportError: If the local UDP endpoint cannot be created, either
                            because the port is already in use or the process
                            lacks permission to bind UDP sockets.
        """
        if self._started:
            return

        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _DnsDatagramProtocol(self),
                local_addr=("127.0.0.1", self._local_port),
            )
        except OSError as exc:
            raise TransportError(
                f"DNS forwarder failed to bind on 127.0.0.1:{self._local_port}.",
                error_code="transport.dns_bind_failed",
                details={
                    "host": "127.0.0.1",
                    "port": self._local_port,
                    "url": f"udp://127.0.0.1:{self._local_port}",
                },
                hint=(
                    "Ensure the port is not already in use and the process has "
                    "permission to bind UDP sockets."
                ),
            ) from exc

        if not isinstance(transport, asyncio.DatagramTransport):
            transport.close()
            raise TransportError(
                "DNS forwarder received an unexpected transport type from asyncio.",
                error_code="transport.dns_bad_transport_type",
                details={
                    "host": "127.0.0.1",
                    "port": self._local_port,
                    "url": f"udp://127.0.0.1:{self._local_port}",
                },
                hint="This is an asyncio internal error. Please report it.",
            )

        self._started = True
        self._transport = transport
        metrics_inc("dns.forwarder.started")
        metrics_gauge_set("dns.forwarder.inflight", 0.0)
        logger.info(
            "DNS forwarder listening on 127.0.0.1:%d → %s:%d",
            self._local_port,
            self._upstream,
            self._upstream_port,
        )

    async def stop(self) -> None:
        """Close the local UDP socket and cancel all in-flight tasks.

        Idempotent — subsequent calls are no-ops.
        """
        if self._stopped:
            return
        self._stopped = True
        metrics_inc("dns.forwarder.stopped")

        if self._transport is not None:
            self._transport.close()

        if self._tasks:
            snapshot = list(self._tasks)
            for task in snapshot:
                task.cancel()
            await asyncio.gather(*snapshot, return_exceptions=True)

        metrics_gauge_set("dns.forwarder.inflight", 0.0)

    # ── Task bookkeeping ──────────────────────────────────────────────────────

    def _task_done(self, task: asyncio.Task[None]) -> None:
        """Remove a completed forwarding task from the in-flight set.

        Args:
            task: The completed forwarding task.
        """
        self._tasks.discard(task)
        self._emit_inflight_gauge()

    def _emit_inflight_gauge(self) -> None:
        """Publish the current number of in-flight DNS queries as a gauge metric."""
        metrics_gauge_set("dns.forwarder.inflight", float(len(self._tasks)))

    # ── Core forwarding logic ─────────────────────────────────────────────────

    async def _forward_query(
        self,
        query: bytes,
        client_addr: tuple[str, int],
        flow_id: str,
    ) -> None:
        """Open a UDP flow, forward *query*, wait for the response, and reply.

        All structured exceptions are caught and converted to metrics and log
        entries so a single bad query never tears down the forwarder.

        :meth:`~exectunnel.transport.UdpFlow.close` is always called in the
        ``finally`` block.  Since ``UdpFlow.close()`` is idempotent and skips
        ``UDP_CLOSE`` when the flow was never opened, this single path handles
        all lifecycle outcomes correctly and guarantees registry eviction.

        Args:
            query:       Raw DNS query bytes.
            client_addr: ``(host, port)`` of the querying client.
            flow_id:     Pre-generated flow ID to use for this query.
        """
        start = asyncio.get_running_loop().time()

        self._emit_inflight_gauge()
        metrics_gauge_inc("session.active.udp_flows")

        handler = UdpFlow(
            flow_id,
            self._upstream,
            self._upstream_port,
            self._ws_send,
            self._udp_registry,
        )
        self._udp_registry[flow_id] = handler

        try:
            async with aspan(
                "dns.forward",
                flow_id=flow_id,
                upstream=self._upstream,
                port=self._upstream_port,
            ):
                await handler.open()

                self._query_count += 1
                self._bytes_in += len(query)
                metrics_inc("dns.query.bytes.in.total", value=len(query))
                metrics_observe("dns.query.bytes.in", float(len(query)))

                await handler.send_datagram(query, must_queue=True)

                response = await self._recv_response(handler, client_addr, flow_id)
                if response is None:
                    return

                if self._transport is not None and not self._transport.is_closing():
                    self._transport.sendto(response, client_addr)
                    self._ok_count += 1
                    self._bytes_out += len(response)
                    metrics_inc("dns.query.ok")
                    metrics_inc("dns.query.bytes.out.total", value=len(response))
                    metrics_observe("dns.query.bytes.out", float(len(response)))
                else:
                    logger.debug(
                        "dns query for %s:%d — transport closed before reply (flow=%s)",
                        client_addr[0],
                        client_addr[1],
                        flow_id,
                    )

        except (
            WebSocketSendTimeoutError,
            ConnectionClosedError,
            TransportError,
            ExecTunnelError,
        ) as exc:
            self._record_query_error(exc, client_addr, flow_id)

        except Exception as exc:
            self._record_query_error(exc, client_addr, flow_id)

        finally:
            metrics_observe(
                "dns.query.duration_sec",
                asyncio.get_running_loop().time() - start,
            )
            # close() is idempotent: skips UDP_CLOSE if open() never succeeded,
            # evicts from registry, and decrements the active-flows gauge.
            with contextlib.suppress(ExecTunnelError, OSError):
                await handler.close()

    async def _recv_response(
        self,
        handler: UdpFlow,
        client_addr: tuple[str, int],
        flow_id: str,
    ) -> bytes | None:
        """Wait for a single DNS response datagram.

        Args:
            handler:     The UDP flow to read from.
            client_addr: Client address used for log context.
            flow_id:     Flow ID used for log context.

        Returns:
            The response bytes, or ``None`` on timeout or if the agent closed
            the flow before a response arrived.
        """
        try:
            async with asyncio.timeout(self._query_timeout):
                response = await handler.recv_datagram()
        except TimeoutError:
            self._error_drop_count += 1
            self._total_drop_count += 1
            metrics_inc("dns.query.timeout")
            logger.debug(
                "dns query timed out for %s:%d (flow=%s, timeout_s=%s)",
                client_addr[0],
                client_addr[1],
                flow_id,
                self._query_timeout,
            )
            return None

        if response is None:
            metrics_inc("dns.query.error", error="agent_closed_no_response")
            logger.debug(
                "dns query for %s:%d — agent closed flow before response (flow=%s)",
                client_addr[0],
                client_addr[1],
                flow_id,
            )
            return None

        return response

    def _record_query_error(
        self,
        exc: Exception,
        client_addr: tuple[str, int],
        flow_id: str,
    ) -> None:
        """Record metrics and emit a log entry for a failed DNS query.

        Args:
            exc:         The exception that caused the failure.
            client_addr: Client address used for log context.
            flow_id:     Flow ID used for log context.
        """
        self._error_drop_count += 1
        self._total_drop_count += 1

        if isinstance(exc, WebSocketSendTimeoutError):
            error_tag = "ws_send_timeout"
            log_fn = logger.warning
            msg = (
                "dns query for %s:%d dropped — WebSocket send timed out "
                "(flow=%s, error_id=%s)"
            )
            args: tuple[object, ...] = (
                client_addr[0],
                client_addr[1],
                flow_id,
                exc.error_id,
            )
        elif isinstance(exc, ConnectionClosedError):
            error_tag = "connection_closed"
            log_fn = logger.warning
            msg = (
                "dns query for %s:%d dropped — tunnel connection closed "
                "(flow=%s, error_id=%s)"
            )
            args = (client_addr[0], client_addr[1], flow_id, exc.error_id)
        elif isinstance(exc, TransportError):
            error_tag = exc.error_code.replace(".", "_")
            log_fn = logger.debug
            msg = (
                "dns query for %s:%d dropped — transport error [%s]: %s "
                "(flow=%s, error_id=%s)"
            )
            args = (
                client_addr[0],
                client_addr[1],
                exc.error_code,
                exc.message,
                flow_id,
                exc.error_id,
            )
        elif isinstance(exc, ExecTunnelError):
            error_tag = exc.error_code.replace(".", "_")
            log_fn = logger.warning
            msg = "dns query for %s:%d failed [%s]: %s (flow=%s, error_id=%s)"
            args = (
                client_addr[0],
                client_addr[1],
                exc.error_code,
                exc.message,
                flow_id,
                exc.error_id,
            )
        else:
            error_tag = type(exc).__name__
            log_fn = logger.warning
            msg = "dns query for %s:%d unexpected failure: %s (flow=%s)"
            args = (client_addr[0], client_addr[1], exc, flow_id)

        metrics_inc("dns.query.error", error=error_tag)
        log_fn(msg, *args, exc_info=not isinstance(exc, ExecTunnelError))

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def saturation_drop_count(self) -> int:
        """Total queries dropped because the in-flight limit was exceeded."""
        return self._saturation_drop_count

    @property
    def error_drop_count(self) -> int:
        """Total queries dropped due to transport/protocol errors or timeouts."""
        return self._error_drop_count

    @property
    def total_drop_count(self) -> int:
        """Total queries dropped for any reason."""
        return self._total_drop_count

    @property
    def query_count(self) -> int:
        """Total DNS queries successfully opened (excludes saturation drops)."""
        return self._query_count

    @property
    def ok_count(self) -> int:
        """Total DNS queries that received a response and replied to the client."""
        return self._ok_count

    @property
    def bytes_in(self) -> int:
        """Total query bytes received from local DNS clients."""
        return self._bytes_in

    @property
    def bytes_out(self) -> int:
        """Total response bytes sent back to local DNS clients."""
        return self._bytes_out

    @property
    def inflight_count(self) -> int:
        """Number of DNS queries currently in flight."""
        return len(self._tasks)

    @property
    def is_running(self) -> bool:
        """``True`` once the local UDP socket is bound and before :meth:`stop` is called."""
        return (
            self._started
            and not self._stopped
            and self._transport is not None
            and not self._transport.is_closing()
        )

    def __repr__(self) -> str:
        state = (
            "running" if self.is_running else ("stopped" if self._stopped else "new")
        )
        return (
            f"<DnsForwarder 127.0.0.1:{self._local_port} → "
            f"{self._upstream}:{self._upstream_port} "
            f"state={state} inflight={self.inflight_count} "
            f"ok={self._ok_count} drops={self._total_drop_count}>"
        )
