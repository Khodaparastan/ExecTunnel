"""DNS forwarder — forwards queries through the tunnel via UDP flows."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from exectunnel.config.defaults import (
    DNS_MAX_INFLIGHT,
    DNS_QUERY_TIMEOUT_SECS,
    DNS_UPSTREAM_PORT,
    UDP_WARN_EVERY,
)
from exectunnel.exceptions import (
    ConnectionClosedError,
    ExecTunnelError,
    TransportError,
    WebSocketSendTimeoutError,
)
from exectunnel.observability import metrics_inc, metrics_observe
from exectunnel.protocol.ids import new_flow_id
from exectunnel.transport import WsSendCallable
from exectunnel.transport import UdpFlow

__all__ = ["DnsForwarder"]

logger = logging.getLogger(__name__)


# ── Datagram protocol ─────────────────────────────────────────────────────────


class _DnsDatagramProtocol(asyncio.DatagramProtocol):
    """asyncio datagram protocol that dispatches received DNS queries to
    :class:`DnsForwarder`.
    """

    def __init__(self, forwarder: DnsForwarder) -> None:
        self._fwd = forwarder

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # Validate transport type without raising — raising from connection_made
        # is not safe; asyncio may not handle it gracefully.
        if not isinstance(transport, asyncio.DatagramTransport):
            logger.error(
                "dns forwarder: expected DatagramTransport, got %s — "
                "forwarder will not function correctly.",
                type(transport).__name__,
            )
            metrics_inc("dns.forwarder.bad_transport_type")
            transport.close()
            return
        self._fwd._on_transport_ready(transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._fwd._on_query(data, addr)

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


# ── Forwarder ─────────────────────────────────────────────────────────────────


class DnsForwarder:
    """Minimal DNS UDP forwarder.

    Listens locally; forwards each query as a UDP flow through the tunnel to
    ``upstream:upstream_port`` and returns the response to the client.

    Args:
        local_port:    Port to bind the local UDP socket on ``127.0.0.1``.
        upstream:      IP or hostname of the upstream DNS server accessed
                       through the tunnel.
        ws_send:       Coroutine callable conforming to
                       :class:`~exectunnel.transport.connection.WsSendCallable`.
        udp_registry:  Shared mutable dict mapping flow IDs to
                       :class:`~exectunnel.transport.udp_flow.UdpFlow`.
        max_inflight:  Maximum number of concurrent in-flight DNS queries.
                       Defaults to :data:`~exectunnel.config.defaults.DNS_MAX_INFLIGHT`.
        upstream_port: UDP port of the upstream DNS server.
                       Defaults to :data:`~exectunnel.config.defaults.DNS_UPSTREAM_PORT`.
    """

    def __init__(
        self,
        local_port: int,
        upstream: str,
        ws_send: WsSendCallable,
        udp_registry: dict[str, UdpFlow],
        max_inflight: int = DNS_MAX_INFLIGHT,
        upstream_port: int = DNS_UPSTREAM_PORT,
    ) -> None:
        self._local_port = local_port
        self._upstream = upstream
        self._upstream_port = upstream_port
        self._ws_send = ws_send
        self._udp_registry = udp_registry
        self._max_inflight = max(1, max_inflight)
        self._transport: asyncio.DatagramTransport | None = None
        self._tasks: set[asyncio.Task[None]] = set()

        # Lifecycle guards.
        self._started: bool = False
        self._stopped: bool = False

        # Telemetry — split by drop category for accurate dashboards.
        self._saturation_drop_count: int = 0  # inflight limit exceeded
        self._error_drop_count: int = 0       # transport / protocol errors
        self._total_drop_count: int = 0       # all drops combined
        self._query_count: int = 0            # queries that passed saturation check
        self._ok_count: int = 0               # queries with a response sent
        self._bytes_in: int = 0               # query bytes received
        self._bytes_out: int = 0              # response bytes sent

    # ── Async context manager ─────────────────────────────────────────────────

    async def __aenter__(self) -> DnsForwarder:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    # ── Package-internal callbacks (called by _DnsDatagramProtocol) ──────────

    def _on_transport_ready(self, transport: asyncio.DatagramTransport) -> None:
        """Store the transport once asyncio confirms the socket is ready."""
        self._transport = transport

    def _on_query(self, data: bytes, addr: tuple[str, int]) -> None:
        """Dispatch an incoming DNS query — called from the datagram protocol."""
        metrics_inc("dns.query.received")

        if len(self._tasks) >= self._max_inflight:
            self._saturation_drop_count += 1
            self._total_drop_count += 1
            metrics_inc("dns.query.drop.saturated")
            if (
                self._saturation_drop_count == 1
                or self._saturation_drop_count % UDP_WARN_EVERY == 0
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

        # Include flow_id in task name for unique identification in task dumps.
        flow_id = new_flow_id()
        task = asyncio.create_task(
            self._forward(data, addr, flow_id),
            name=f"dns-fwd-{addr[0]}_{addr[1]}-{flow_id[:8]}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind the local UDP socket and begin accepting DNS queries.

        Idempotent — subsequent calls are no-ops.

        Raises:
            TransportError: If the local UDP endpoint cannot be created
                (e.g. port already in use, insufficient permissions).
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
        except Exception as exc:
            raise TransportError(
                f"DNS forwarder failed to bind on 127.0.0.1:{self._local_port}.",
                error_code="transport.dns_bind_failed",
                details={
                    "local_port": self._local_port,
                    "upstream": self._upstream,
                    "upstream_port": self._upstream_port,
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
                details={"transport_type": type(transport).__name__},
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

        Awaiting task cancellation ensures that no in-flight ``_forward``
        coroutine attempts to use the closed transport after ``stop()`` returns.
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

    async def _forward(
        self,
        query: bytes,
        client_addr: tuple[str, int],
        flow_id: str,
    ) -> None:
        """Open a UDP flow, forward *query*, wait for the response, reply to client.

        ``flow_id`` is pre-generated by :meth:`_on_query` so the task name
        and the flow ID are consistent in logs and task dumps.

        All structured exceptions are caught here and converted into metrics +
        log entries so that a single bad query never tears down the forwarder.

        ``_query_count`` and ``_bytes_in`` are incremented only after the
        handler is successfully opened — saturation drops are counted
        separately in ``_saturation_drop_count``.

        Exception hierarchy
        -------------------
        ``asyncio.TimeoutError``
            Query timed out; DNS client will retry naturally.
        ``WebSocketSendTimeoutError``
            Tunnel send queue stalled; warn loudly.
        ``ConnectionClosedError``
            Tunnel is gone; warn loudly.
        ``TransportError``
            Other transport blip; log at DEBUG.
        ``ExecTunnelError``
            Any other library error; log at WARNING.
        ``Exception``
            Truly unexpected; log at WARNING with traceback.
        """
        start = asyncio.get_running_loop().time()

        handler = UdpFlow(
            flow_id,
            self._upstream,
            self._upstream_port,
            self._ws_send,
            self._udp_registry,
        )
        # Register in registry only after construction — open() may fail and
        # the finally block will pop it regardless.
        self._udp_registry[flow_id] = handler

        # Tracks whether the agent already tore down the flow so we skip
        # sending UDP_CLOSE (which would be spurious on a dead connection).
        agent_closed = False

        try:
            # ── Open + send ───────────────────────────────────────────────────
            await handler.open()

            # Count the query only after open() succeeds.
            self._query_count += 1
            self._bytes_in += len(query)
            metrics_observe("dns.query.bytes.in", float(len(query)))

            await handler.send_datagram(query)

            # ── Receive response ──────────────────────────────────────────────
            response = await asyncio.wait_for(
                handler.recv_datagram(),
                timeout=DNS_QUERY_TIMEOUT_SECS,
            )

            if response is None:
                # Agent closed the flow before sending a response.
                agent_closed = True
                metrics_inc("dns.query.error", error="agent_closed_no_response")
                logger.debug(
                    "dns query for %s:%d — agent closed flow before response (flow=%s)",
                    client_addr[0],
                    client_addr[1],
                    flow_id,
                )
                return

            # ── Reply to client ───────────────────────────────────────────────
            if self._transport is not None and not self._transport.is_closing():
                self._transport.sendto(response, client_addr)
                self._ok_count += 1
                self._bytes_out += len(response)
                metrics_inc("dns.query.ok")
                metrics_observe("dns.query.bytes.out", float(len(response)))
            else:
                logger.debug(
                    "dns query for %s:%d — transport closed before reply could "
                    "be sent (flow=%s)",
                    client_addr[0],
                    client_addr[1],
                    flow_id,
                )

        except asyncio.TimeoutError:
            # DNS clients retry naturally — log at DEBUG, not WARNING.
            self._error_drop_count += 1
            self._total_drop_count += 1
            metrics_inc("dns.query.timeout")
            logger.debug(
                "dns query timed out for %s:%d (flow=%s, timeout_s=%s)",
                client_addr[0],
                client_addr[1],
                flow_id,
                DNS_QUERY_TIMEOUT_SECS,
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
            )
            logger.debug(
                "dns query traceback (flow=%s)",
                flow_id,
                exc_info=True,
            )

        finally:
            metrics_observe(
                "dns.query.duration_sec",
                asyncio.get_running_loop().time() - start,
            )
            # Close the flow unless the agent already tore it down.
            # Suppress ExecTunnelError (covers ConnectionClosedError,
            # WebSocketSendTimeoutError, TransportError) and OSError so
            # close() never suppresses the original exception.
            if not agent_closed:
                with contextlib.suppress(ExecTunnelError, OSError):
                    await handler.close()
            else:
                # Agent closed — mark the handler as remote-closed so it
                # releases its internal state cleanly without sending UDP_CLOSE.
                handler.close_remote()

            # Always remove from registry to prevent memory leaks.
            self._udp_registry.pop(flow_id, None)

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
        """``True`` once the local UDP socket has been successfully bound
        and before :meth:`stop` has been called."""
        return (
            self._started
            and not self._stopped
            and self._transport is not None
            and not self._transport.is_closing()
        )
