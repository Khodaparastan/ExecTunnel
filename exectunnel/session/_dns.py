"""DNS forwarder — forwards DNS queries through the tunnel via UDP flows.

Belongs to the ``session`` layer because it orchestrates transport-layer
primitives (:class:`~exectunnel.transport.UdpFlow`) in the same way the
session orchestrates TCP connections — it is not a SOCKS5 wire-protocol
concern and does not belong in ``proxy``.
"""

import asyncio
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
        # Defence-in-depth type guard — start() also checks the return value
        # of create_datagram_endpoint, but this guard covers hypothetical
        # reuse of the protocol outside start().
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
        """Called by :class:`_DnsDatagramProtocol` when the endpoint is ready.

        ``start()`` also assigns ``_transport`` from the return value of
        ``create_datagram_endpoint``.  Both assignments refer to the same
        object; this callback exists so the protocol can set the transport
        before ``start()``'s await returns (needed if the protocol fires
        other callbacks during endpoint creation).
        """
        self._transport = transport

    def on_query(self, data: bytes, addr: tuple[str, int]) -> None:
        """Dispatch a DNS query received on the local socket.

        Guards against dispatching after :meth:`stop` — the asyncio protocol's
        ``datagram_received`` callback can fire between ``stop()`` closing the
        transport and the transport actually shutting down.
        """
        if self._stopped:
            metrics_inc("dns.query.drop.after_stop")
            return

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

        # Authoritative assignment — on_transport_ready() may have already
        # set this during create_datagram_endpoint, but this is the canonical
        # assignment that start() guarantees.
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
            # Snapshot once — done-callbacks may mutate self._tasks between
            # the cancel loop and the gather call.
            snapshot = list(self._tasks)
            for task in snapshot:
                task.cancel()
            await asyncio.gather(*snapshot, return_exceptions=True)

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
        opened = False

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
            opened = True

            self._query_count += 1
            self._bytes_in += len(query)
            metrics_observe("dns.query.bytes.in", float(len(query)))

            await handler.send_datagram(query)

            # Single recv — DNS is request/response.  Flow is closed in the
            # finally block immediately after.
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
            if agent_closed:
                # Agent already tore down the flow — just clean up local state.
                handler.on_remote_closed()
            elif opened:
                # Flow was successfully opened — send UDP_CLOSE to the agent.
                try:
                    await handler.close()
                except (ExecTunnelError, OSError) as _close_exc:
                    logger.debug(
                        "dns flow %s: UDP_CLOSE send failed during cleanup: %s",
                        flow_id,
                        _close_exc,
                    )
            else:
                # open() never succeeded — agent doesn't know about this flow.
                # Just evict from registry; no UDP_CLOSE needed.
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

    # ── Debug ─────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        state = "running" if self.is_running else ("stopped" if self._stopped else "new")
        return (
            f"<DnsForwarder 127.0.0.1:{self._local_port} → "
            f"{self._upstream}:{self._upstream_port} "
            f"state={state} inflight={len(self._tasks)} "
            f"ok={self._ok_count} drops={self._total_drop_count}>"
        )
