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
from exectunnel.transport.connection import WsSendCallable
from exectunnel.transport.udp_flow import _UdpFlowHandler

logger = logging.getLogger("exectunnel.transport.dns_forwarder")


# ── Datagram protocol (module-level, not recreated per start()) ───────────────


class _DnsDatagramProtocol(asyncio.DatagramProtocol):
    """asyncio datagram protocol that dispatches received DNS queries to
    :class:`_DnsForwarder`.
    """

    def __init__(self, forwarder: _DnsForwarder) -> None:
        self._fwd = forwarder

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        if not isinstance(transport, asyncio.DatagramTransport):
            raise TypeError(
                f"Expected DatagramTransport, got {type(transport).__name__}. "
                "This is an asyncio internal error."
            )
        self._fwd._transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        metrics_inc("dns.query.received")

        if len(self._fwd._tasks) >= self._fwd._max_inflight:
            self._fwd._saturation_drop_count += 1
            self._fwd._total_drop_count += 1
            metrics_inc("dns.query.drop.saturated")
            if (
                self._fwd._saturation_drop_count == 1
                or self._fwd._saturation_drop_count % UDP_WARN_EVERY == 0
            ):
                logger.warning(
                    "dns forwarder saturated (%d inflight), dropping query "
                    "from %s:%d (saturation_drops=%d)",
                    self._fwd._max_inflight,
                    addr[0],
                    addr[1],
                    self._fwd._saturation_drop_count,
                )
            return

        # Use underscore in task name to avoid colon-delimited log parser issues.
        task = asyncio.create_task(
            self._fwd._forward(data, addr),
            name=f"dns-fwd-{addr[0]}_{addr[1]}",
        )
        self._fwd._tasks.add(task)
        # set.discard receives the completed task as its argument — correct.
        task.add_done_callback(self._fwd._tasks.discard)

    def error_received(self, exc: Exception) -> None:
        # OS-level socket errors are not actionable per-query.
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


class _DnsForwarder:
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
                       :class:`~exectunnel.transport.udp_flow._UdpFlowHandler`.
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
        udp_registry: dict[str, _UdpFlowHandler],
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
        self._error_drop_count: int = 0  # transport / protocol errors
        self._total_drop_count: int = 0  # all drops combined
        self._query_count: int = 0  # total queries attempted
        self._ok_count: int = 0  # queries with a response sent
        self._bytes_in: int = 0  # query bytes received
        self._bytes_out: int = 0  # response bytes sent

    # ── Async context manager ─────────────────────────────────────────────────

    async def __aenter__(self) -> _DnsForwarder:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

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

        # Metric fires only after successful bind.
        assert isinstance(transport, asyncio.DatagramTransport)
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
            self._tasks.clear()

    # ── Core forwarding logic ─────────────────────────────────────────────────

    async def _forward(self, query: bytes, client_addr: tuple[str, int]) -> None:
        """Open a UDP flow, forward *query*, wait for the response, reply to client.

        All structured exceptions are caught here and converted into metrics +
        log entries so that a single bad query never tears down the forwarder.

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
            Any other library error (``ProtocolError``, ``FrameDecodingError``,
            etc.); log at WARNING.
        ``Exception``
            Truly unexpected; log at WARNING with traceback.
        """
        start = asyncio.get_running_loop().time()
        self._query_count += 1
        self._bytes_in += len(query)
        metrics_observe("dns.query.bytes.in", float(len(query)))

        flow_id = new_flow_id()
        handler = _UdpFlowHandler(
            flow_id,
            self._upstream,
            self._upstream_port,
            self._ws_send,
            self._udp_registry,
        )
        self._udp_registry[flow_id] = handler

        # Tracks whether the agent already tore down the flow so we skip
        # sending UDP_CLOSE (which would be spurious on a dead connection).
        agent_closed = False

        try:
            # ── Open + send ───────────────────────────────────────────────────
            await handler.open()
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

        except TimeoutError:
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
            # Connection is gone — skip UDP_CLOSE in finally.
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
            # Covers ProtocolError, FrameDecodingError, and any other library error.
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
            # Always record duration.
            metrics_observe(
                "dns.query.duration_sec",
                asyncio.get_running_loop().time() - start,
            )
            # Close the flow unless the agent already tore it down.
            # Suppress all exceptions from close() so they never suppress the
            # original exception that caused us to enter the finally block.
            if not agent_closed:
                with contextlib.suppress(
                    WebSocketSendTimeoutError,
                    TransportError,
                    ExecTunnelError,
                    OSError,
                ):
                    await handler.close()

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
        """Total DNS queries attempted (excludes saturation drops)."""
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
