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

    async def start(self) -> None:
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
                    if self._fwd._drop_count == 1 or self._fwd._drop_count % UDP_WARN_EVERY == 0:
                        logger.warning(
                            "dns forwarder saturated (%d inflight), dropping query from %s:%d "
                            "(drops=%d)",
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
                logger.debug("dns forwarder error: %s", exc)

        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Proto(self),
            local_addr=("127.0.0.1", self._local_port),
        )
        logger.info(
            "DNS forwarder listening on 127.0.0.1:%d → %s:53",
            self._local_port,
            self._upstream,
        )

    async def _forward(self, query: bytes, client_addr: tuple[str, int]) -> None:
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
        await handler.open()
        await handler.send_datagram(query)

        # Track whether the agent closed the flow first (response == None means
        # UDP_CLOSED was received).  In that case we must NOT send UDP_CLOSE back
        # — the agent already tore down the flow and the frame would be spurious.
        agent_closed = False
        try:
            response = await asyncio.wait_for(
                handler.recv_datagram(), timeout=DNS_QUERY_TIMEOUT_SECS
            )
            if response is None:
                agent_closed = True
        except TimeoutError:
            metrics_inc("dns.query.timeout")
            logger.debug("dns query timed out for %s:%d", *client_addr)
            return
        except Exception as exc:
            metrics_inc("dns.query.error", error=exc.__class__.__name__)
            logger.warning(
                "dns query failed for %s:%d: %s", client_addr[0], client_addr[1], exc
            )
            logger.debug(
                "dns query traceback for %s:%d",
                client_addr[0],
                client_addr[1],
                exc_info=True,
            )
            return
        finally:
            if not agent_closed:
                await handler.close()
            metrics_observe(
                "dns.query.duration_sec", asyncio.get_running_loop().time() - start
            )

        if response is not None and self._transport is not None:
            metrics_inc("dns.query.ok")
            metrics_observe("dns.query.bytes.out", float(len(response)))
            self._transport.sendto(response, client_addr)

    def stop(self) -> None:
        metrics_inc("dns.forwarder.stopped")
        if self._transport:
            self._transport.close()
        for task in list(self._tasks):
            task.cancel()
