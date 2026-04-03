"""DNS forwarder — forwards queries through the tunnel via UDP flows."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from exectunnel.config.defaults import (
    DNS_MAX_INFLIGHT,
    DNS_QUERY_TIMEOUT_SECS,
    DNS_UPSTREAM_PORT,
    UDP_WARN_EVERY,
)
from exectunnel.exceptions import (
    ConnectionClosedError,
    ExecTunnelError,
    ExecutionTimeoutError,
    TransportError,
    WebSocketSendTimeoutError,
)
from exectunnel.observability import metrics_inc, metrics_observe
from exectunnel.protocol.ids import new_flow_id
from exectunnel.transport.udp_flow import _UdpFlowHandler

if TYPE_CHECKING:
    pass

logger = logging.getLogger("exectunnel.transport.dns_forwarder")


class _DnsForwarder:
    """
    Minimal DNS UDP forwarder.

    Listens locally; forwards each query as a UDP flow through the tunnel to
    ``dns_upstream:53`` and returns the response to the client.

    Parameters
    ----------
    local_port:
        Port to bind the local UDP socket on ``127.0.0.1``.
    upstream:
        IP (or hostname) of the upstream DNS server accessed through the tunnel.
    ws_send:
        Coroutine callable with the same signature as ``TunnelSession._ws_send``.
    udp_registry:
        Shared mutable dict mapping flow IDs to :class:`_UdpFlowHandler`.
    max_inflight:
        Maximum number of concurrent in-flight DNS queries.
    """

    def __init__(
        self,
        local_port: int,
        upstream: str,
        ws_send: Callable[..., Coroutine[Any, Any, None]],
        udp_registry: dict[str, _UdpFlowHandler],
        max_inflight: int = DNS_MAX_INFLIGHT,
    ) -> None:
        self._local_port = local_port
        self._upstream = upstream
        self._ws_send = ws_send
        self._udp_registry = udp_registry
        self._max_inflight = max(1, max_inflight)
        self._transport: asyncio.DatagramTransport | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._drop_count = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bind the local UDP socket and begin accepting DNS queries.

        Raises
        ------
        TransportError
            If the local UDP endpoint cannot be created (e.g. port already in
            use, insufficient permissions).
        """
        loop = asyncio.get_running_loop()
        metrics_inc("dns.forwarder.started")

        class _Proto(asyncio.DatagramProtocol):
            def __init__(self, fwd: _DnsForwarder) -> None:
                self._fwd = fwd

            def connection_made(self, transport: asyncio.BaseTransport) -> None:
                assert isinstance(transport, asyncio.DatagramTransport)
                self._fwd._transport = transport

            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                metrics_inc("dns.query.received")
                if len(self._fwd._tasks) >= self._fwd._max_inflight:
                    self._fwd._drop_count += 1
                    metrics_inc("dns.query.drop.saturated")
                    if (
                        self._fwd._drop_count == 1
                        or self._fwd._drop_count % UDP_WARN_EVERY == 0
                    ):
                        logger.warning(
                            "dns forwarder saturated (%d inflight), dropping query "
                            "from %s:%d (drops=%d)",
                            self._fwd._max_inflight,
                            addr[0],
                            addr[1],
                            self._fwd._drop_count,
                        )
                    return

                task = asyncio.create_task(
                    self._fwd._forward(data, addr),
                    name=f"dns-fwd-{addr[0]}:{addr[1]}",
                )
                self._fwd._tasks.add(task)
                task.add_done_callback(self._fwd._tasks.discard)

            def error_received(self, exc: Exception) -> None:
                # asyncio delivers OS-level socket errors here; they are not
                # actionable per-query so we log at debug and emit a metric.
                metrics_inc("dns.forwarder.socket_error")
                logger.debug(
                    "dns forwarder socket error: %s (%s)",
                    exc,
                    type(exc).__name__,
                )

        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _Proto(self),
                local_addr=("127.0.0.1", self._local_port),
            )
        except Exception as exc:
            raise TransportError(
                f"DNS forwarder failed to bind on 127.0.0.1:{self._local_port}.",
                error_code="transport.dns_bind_failed",
                details={
                    "local_port": self._local_port,
                    "upstream": self._upstream,
                },
                hint=(
                    "Ensure the port is not already in use and the process has "
                    "permission to bind UDP sockets."
                ),
            ) from exc

        logger.info(
            "DNS forwarder listening on 127.0.0.1:%d → %s:%d",
            self._local_port,
            self._upstream,
            DNS_UPSTREAM_PORT,
        )

    def stop(self) -> None:
        """Close the local UDP socket and cancel all in-flight query tasks."""
        metrics_inc("dns.forwarder.stopped")
        if self._transport:
            self._transport.close()
        for task in list(self._tasks):
            task.cancel()

    # ── Core forwarding logic ─────────────────────────────────────────────────

    async def _forward(self, query: bytes, client_addr: tuple[str, int]) -> None:
        """Open a UDP flow, forward *query*, wait for the response, reply to client.

        All structured exceptions from :class:`_UdpFlowHandler` are caught here
        and converted into metrics + log entries so that a single bad query never
        tears down the forwarder.  The exception hierarchy drives distinct
        handling per failure domain:

        * :class:`ExecutionTimeoutError`     – query timed out; DNS client will retry.
        * :class:`WebSocketSendTimeoutError` – tunnel is stalled; warn loudly.
        * :class:`ConnectionClosedError`     – tunnel is gone; warn loudly.
        * :class:`TransportError`            – other transport blip; debug log.
        * :class:`ExecTunnelError`           – any other library error; warning log.
        """
        start = asyncio.get_running_loop().time()
        metrics_observe("dns.query.bytes.in", float(len(query)))

        flow_id = new_flow_id()
        handler = _UdpFlowHandler(
            flow_id,
            self._upstream,
            DNS_UPSTREAM_PORT,
            self._ws_send,
            self._udp_registry,
        )
        self._udp_registry[flow_id] = handler

        # ── Open + send ───────────────────────────────────────────────────────
        try:
            await handler.open()
            await handler.send_datagram(query)
        except WebSocketSendTimeoutError as exc:
            metrics_inc("dns.query.error", error="ws_send_timeout")
            logger.warning(
                "dns query for %s:%d dropped — WebSocket send timed out "
                "(flow=%s, error_id=%s)",
                client_addr[0],
                client_addr[1],
                flow_id,
                exc.error_id,
            )
            self._udp_registry.pop(flow_id, None)
            return
        except ConnectionClosedError as exc:
            metrics_inc("dns.query.error", error="connection_closed")
            logger.warning(
                "dns query for %s:%d dropped — tunnel connection closed "
                "(flow=%s, error_id=%s)",
                client_addr[0],
                client_addr[1],
                flow_id,
                exc.error_id,
            )
            self._udp_registry.pop(flow_id, None)
            return
        except TransportError as exc:
            metrics_inc("dns.query.error", error="transport_error")
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
            self._udp_registry.pop(flow_id, None)
            return

        # ── Receive response ──────────────────────────────────────────────────
        #
        # ``agent_closed`` tracks whether the remote agent already tore down the
        # flow (recv_datagram returned None).  In that case we must NOT send a
        # UDP_CLOSE frame — the agent already cleaned up and the frame would be
        # spurious.
        agent_closed = False
        response: bytes | None = None

        try:
            response = await asyncio.wait_for(
                handler.recv_datagram(),
                timeout=DNS_QUERY_TIMEOUT_SECS,
            )
            if response is None:
                agent_closed = True

        except TimeoutError:
            # Map stdlib timeout → structured ExecutionTimeoutError for uniform
            # metrics/logging, but do not re-raise — DNS clients retry naturally.
            timeout_exc = ExecutionTimeoutError(
                f"DNS query timed out waiting for upstream response (flow={flow_id}).",
                error_code="execution.dns_query_timeout",
                details={
                    "flow_id": flow_id,
                    "client_addr": f"{client_addr[0]}:{client_addr[1]}",
                    "upstream": self._upstream,
                    "timeout_s": DNS_QUERY_TIMEOUT_SECS,
                },
                hint="Increase DNS_QUERY_TIMEOUT_SECS or check upstream DNS reachability.",
            )
            metrics_inc("dns.query.timeout")
            logger.debug(
                "dns query timed out for %s:%d (flow=%s, timeout_s=%s, error_id=%s)",
                client_addr[0],
                client_addr[1],
                flow_id,
                DNS_QUERY_TIMEOUT_SECS,
                timeout_exc.error_id,
            )
            return

        except WebSocketSendTimeoutError as exc:
            metrics_inc("dns.query.error", error="ws_send_timeout")
            logger.warning(
                "dns query for %s:%d failed during recv — WebSocket send timed out "
                "(flow=%s, error_id=%s)",
                client_addr[0],
                client_addr[1],
                flow_id,
                exc.error_id,
            )
            return

        except ConnectionClosedError as exc:
            metrics_inc("dns.query.error", error="connection_closed")
            logger.warning(
                "dns query for %s:%d failed during recv — tunnel connection closed "
                "(flow=%s, error_id=%s)",
                client_addr[0],
                client_addr[1],
                flow_id,
                exc.error_id,
            )
            agent_closed = True  # Connection gone; skip UDP_CLOSE.
            return

        except TransportError as exc:
            metrics_inc("dns.query.error", error="transport_error")
            logger.debug(
                "dns query for %s:%d failed during recv — transport error [%s]: %s "
                "(flow=%s, error_id=%s)",
                client_addr[0],
                client_addr[1],
                exc.error_code,
                exc.message,
                flow_id,
                exc.error_id,
            )
            return

        except ExecTunnelError as exc:
            # Catch-all for any other library error (ProtocolError, etc.).
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
            return

        finally:
            if not agent_closed:
                await handler.close()
            metrics_observe(
                "dns.query.duration_sec",
                asyncio.get_running_loop().time() - start,
            )

        # ── Reply to client ───────────────────────────────────────────────────
        if response is not None and self._transport is not None:
            metrics_inc("dns.query.ok")
            metrics_observe("dns.query.bytes.out", float(len(response)))
            self._transport.sendto(response, client_addr)

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def drop_count(self) -> int:
        """Total number of queries dropped due to inflight saturation."""
        return self._drop_count

    @property
    def inflight_count(self) -> int:
        """Number of DNS queries currently in flight."""
        return len(self._tasks)

    @property
    def is_running(self) -> bool:
        """``True`` once the local UDP socket has been successfully bound."""
        return self._transport is not None and not self._transport.is_closing()
