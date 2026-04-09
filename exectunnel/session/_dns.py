"""DNS forwarder — forwards DNS queries through the tunnel via UDP flows.

Belongs to the ``session`` layer because it orchestrates transport-layer
primitives (:class:`~exectunnel.transport.UdpFlow`) in the same way the
session orchestrates TCP connections — it is not a SOCKS5 wire-protocol
concern and does not belong in ``proxy``.
"""

import asyncio
import contextlib
import logging

from exectunnel.config.defaults import Defaults
from exectunnel.exceptions import (
    ConnectionClosedError,
    ExecTunnelError,
    TransportError,
    WebSocketSendTimeoutError,
)
from exectunnel.observability import metrics_inc, metrics_observe
from exectunnel.protocol import new_flow_id
from exectunnel.transport import UdpFlow, WsSendCallable

logger = logging.getLogger(__name__)


class _DnsDatagramProtocol(asyncio.DatagramProtocol):
    """asyncio datagram protocol that dispatches received DNS queries."""

    __slots__ = ("_forwarder",)

    def __init__(self, forwarder: "DnsForwarder") -> None:
        self._forwarder = forwarder

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        if not isinstance(transport, asyncio.DatagramTransport):
            logger.error(
                "dns forwarder: expected DatagramTransport, got %s — "
                "forwarder will not function correctly.",
                type(transport).__name__,
            )
            metrics_inc("dns.forwarder.bad_transport_type")
            transport.close()
            return
        self._forwarder.on_transport_ready(transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._forwarder.on_query(data, addr)

    def error_received(self, exc: Exception) -> None:
        metrics_inc("dns.forwarder.socket_error", reason=type(exc).__name__)
        logger.debug(
            "dns forwarder socket error [%s]: %s",
            type(exc).__name__,
            exc,
        )

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None:
            logger.debug("dns forwarder connection lost: %s", exc)


class DnsForwarder:
    """Minimal DNS UDP forwarder.

    Listens locally; forwards each query as a UDP flow through the tunnel to
    ``upstream:upstream_port`` and returns the response to the client.

    Each query opens a dedicated ``UdpFlow``, sends the query datagram, waits
    for exactly one response datagram, then closes the flow.  The
    ``recv_datagram()`` call is intentionally not looped — DNS is a
    single-request/single-response protocol and the flow is closed immediately
    after the first response.  Any additional datagrams that arrive before
    close are discarded by the flow's queue drain.

    Args:
        local_port:    Port to bind the local UDP socket on ``127.0.0.1``.
        upstream:      IP or hostname of the upstream DNS server accessed
                       through the tunnel.
        ws_send:       Coroutine callable conforming to ``WsSendCallable``.
        udp_registry:  Shared mutable dict mapping flow IDs to ``UdpFlow``.
        max_inflight:  Maximum number of concurrent in-flight DNS queries.
        upstream_port: UDP port of the upstream DNS server.
    """

    __slots__ = (
        "_local_port",
        "_upstream",
        "_upstream_port",
        "_ws_send",
        "_udp_registry",
        "_max_inflight",
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
    ) -> None:
        self._local_port = local_port
        self._upstream = upstream
        self._upstream_port = upstream_port
        self._ws_send = ws_send
        self._udp_registry = udp_registry
        self._max_inflight = max(1, max_inflight)
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
        self._transport = transport

    def on_query(self, data: bytes, addr: tuple[str, int]) -> None:
        metrics_inc("dns.query.received")

        if len(self._tasks) >= self._max_inflight:
            self._saturation_drop_count += 1
            self._total_drop_count += 1
            metrics_inc("dns.query.drop.saturated")
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
        task.add_done_callback(self._tasks.discard)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind the local UDP socket and begin accepting DNS queries.

        Idempotent — subsequent calls are no-ops.

        Raises:
            TransportError: If the local UDP endpoint cannot be created.
        """
        if self._started:
            return
        self._started = True

        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _DnsDatagramProtocol(self),
                local_addr=("127.0.0.1", self._local_port),
            )
        except OSError as exc:
            # Narrow to OSError — the only failure mode for create_datagram_endpoint
            # on a valid address is an OS-level bind failure.
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

        self._transport = transport
        metrics_inc("dns.forwarder.started")
        logger.info(
            "DNS forwarder listening on 127.0.0.1:%d → %s:%d",
            self._local_port,
            self._upstream,
            self._upstream_port,
        )

    async def stop(self) -> None:
        """Close the local UDP socket and await cancellation of all in-flight tasks.

        Idempotent — subsequent calls are no-ops.
        """
        if self._stopped:
            return
        self._stopped = True
        metrics_inc("dns.forwarder.stopped")

        if self._transport is not None:
            self._transport.close()

        if self._tasks:
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ── Core forwarding logic ─────────────────────────────────────────────────

    async def _forward_query(
        self,
        query: bytes,
        client_addr: tuple[str, int],
        flow_id: str,
    ) -> None:
        """Open a UDP flow, forward *query*, wait for the response, reply to client.

        DNS is a single-request/single-response protocol.  One ``recv_datagram()``
        call is made intentionally — the flow is closed immediately after the
        first response.  This is a documented deviation from the transport
        invariant that requires looping until ``None``; the loop is omitted
        because the flow is closed synchronously after the single recv.

        All structured exceptions are caught here and converted into metrics +
        log entries so that a single bad query never tears down the forwarder.
        """
        start = asyncio.get_running_loop().time()
        agent_closed = False

        handler = UdpFlow(
            flow_id,
            self._upstream,
            self._upstream_port,
            self._ws_send,
            self._udp_registry,
        )
        # Insert into registry BEFORE open() — agent may reply immediately.
        self._udp_registry[flow_id] = handler

        try:
            await handler.open()

            self._query_count += 1
            self._bytes_in += len(query)
            metrics_observe("dns.query.bytes.in", float(len(query)))

            await handler.send_datagram(query)

            # Single recv — DNS is request/response.  Flow is closed in the
            # finally block immediately after, so no further datagrams can
            try:
                async with asyncio.timeout(Defaults.DNS_QUERY_TIMEOUT_SECS):
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
                    Defaults.DNS_QUERY_TIMEOUT_SECS,
                )
                return  # finally block handles cleanup.

            if response is None:
                agent_closed = True
                metrics_inc("dns.query.error", error="agent_closed_no_response")
                logger.debug(
                    "dns query for %s:%d — agent closed flow before response (flow=%s)",
                    client_addr[0],
                    client_addr[1],
                    flow_id,
                )
                return

            if self._transport is not None and not self._transport.is_closing():
                self._transport.sendto(response, client_addr)
                self._ok_count += 1
                self._bytes_out += len(response)
                metrics_inc("dns.query.ok")
                metrics_observe("dns.query.bytes.out", float(len(response)))
            else:
                logger.debug(
                    "dns query for %s:%d — transport closed before reply (flow=%s)",
                    client_addr[0],
                    client_addr[1],
                    flow_id,
                )

        except WebSocketSendTimeoutError as exc:
            self._error_drop_count += 1
            self._total_drop_count += 1
            metrics_inc("dns.query.error", error="ws_send_timeout")
            logger.warning(
                "dns query for %s:%d dropped — WebSocket send timed out "
                "(flow=%s, error_id=%s)",
                client_addr[0],
                client_addr[1],
                flow_id,
                exc.error_id,
            )

        except ConnectionClosedError as exc:
            self._error_drop_count += 1
            self._total_drop_count += 1
            agent_closed = True
            metrics_inc("dns.query.error", error="connection_closed")
            logger.warning(
                "dns query for %s:%d dropped — tunnel connection closed "
                "(flow=%s, error_id=%s)",
                client_addr[0],
                client_addr[1],
                flow_id,
                exc.error_id,
            )

        except TransportError as exc:
            self._error_drop_count += 1
            self._total_drop_count += 1
            metrics_inc("dns.query.error", error=exc.error_code.replace(".", "_"))
            logger.debug(
                "dns query for %s:%d dropped — transport error [%s]: %s "
                "(flow=%s, error_id=%s)",
                client_addr[0],
                client_addr[1],
                exc.error_code,
                exc.message,
                flow_id,
                exc.error_id,
            )

        except ExecTunnelError as exc:
            self._error_drop_count += 1
            self._total_drop_count += 1
            metrics_inc("dns.query.error", error=exc.error_code.replace(".", "_"))
            logger.warning(
                "dns query for %s:%d failed [%s]: %s (flow=%s, error_id=%s)",
                client_addr[0],
                client_addr[1],
                exc.error_code,
                exc.message,
                flow_id,
                exc.error_id,
            )

        except Exception as exc:
            self._error_drop_count += 1
            self._total_drop_count += 1
            metrics_inc("dns.query.error", error=type(exc).__name__)
            logger.warning(
                "dns query for %s:%d unexpected failure: %s (flow=%s)",
                client_addr[0],
                client_addr[1],
                exc,
                flow_id,
                exc_info=True,
            )

        finally:
            metrics_observe(
                "dns.query.duration_sec",
                asyncio.get_running_loop().time() - start,
            )
            # Close the flow unless the agent already tore it down.
            if not agent_closed:
                with contextlib.suppress(ExecTunnelError, OSError):
                    await handler.close()
            else:
                handler.on_remote_closed()

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def saturation_drop_count(self) -> int:
        """Total queries dropped because the inflight limit was exceeded."""
        return self._saturation_drop_count

    @property
    def error_drop_count(self) -> int:
        """Total queries dropped due to transport / protocol errors or timeouts."""
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
        """``True`` once the local UDP socket has been bound and before stop()."""
        return (
            self._started
            and not self._stopped
            and self._transport is not None
            and not self._transport.is_closing()
        )
