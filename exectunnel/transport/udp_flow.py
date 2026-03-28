"""UDP flow handler — local side of a SOCKS5 UDP ASSOCIATE flow."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from exectunnel.config.defaults import TCP_INBOUND_QUEUE_CAP, UDP_WARN_EVERY
from exectunnel.observability import metrics_inc
from exectunnel.protocol.frames import (
    encode_udp_close_frame,
    encode_udp_data_frame,
    encode_udp_open_frame,
)

logger = logging.getLogger("exectunnel.transport.udp_flow")


class _UdpFlowHandler:
    """Bridges one SOCKS5 UDP ASSOCIATE flow through the tunnel."""

    def __init__(
        self,
        flow_id: str,
        host: str,
        port: int,
        ws_send: Callable[..., Coroutine[Any, Any, None]],
        registry: dict[str, _UdpFlowHandler],
    ) -> None:
        self._id = flow_id
        self._host = host
        self._port = port
        self._ws_send = ws_send
        self._registry = registry
        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=TCP_INBOUND_QUEUE_CAP)
        self._drop_count = 0

    async def open(self) -> None:
        await self._ws_send(
            encode_udp_open_frame(self._id, self._host, self._port), control=True
        )

    def feed(self, data: bytes) -> None:
        try:
            self._inbound.put_nowait(data)
        except asyncio.QueueFull:
            self._drop_count += 1
            metrics_inc("udp.flow.inbound_queue.drop")
            if self._drop_count == 1 or self._drop_count % UDP_WARN_EVERY == 0:
                logger.warning(
                    "udp flow %s inbound queue full, dropping datagram (drops=%d)",
                    self._id,
                    self._drop_count,
                )

    def close_remote(self) -> None:
        """Deliver a ``None`` sentinel even if the queue is full."""
        if self._inbound.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._inbound.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._inbound.put_nowait(None)

    async def send_datagram(self, data: bytes) -> None:
        """Encode *data* as a UDP_DATA frame and enqueue it for sending.

        The frame is placed on the *data* send queue (``_send_data_queue``), not
        the control queue.  When the data queue is full, ``_ws_send`` drops the
        frame silently — this is intentional UDP semantics (datagrams may be
        lost).
        """
        b64 = base64.b64encode(data).decode()
        await self._ws_send(encode_udp_data_frame(self._id, b64))

    async def recv_datagram(self) -> bytes | None:
        return await self._inbound.get()

    async def close(self) -> None:
        self._registry.pop(self._id, None)
        await self._ws_send(encode_udp_close_frame(self._id), control=True)
