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
        # Set by close_remote() to signal EOF without evicting queued datagrams.
        self._close_remote_requested = False
        self._close_event: asyncio.Event = asyncio.Event()

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
        """Signal remote closure without evicting queued datagrams.

        Sets ``_close_remote_requested`` and fires ``_close_event`` so that
        ``recv_datagram()`` can wake up and return ``None`` after draining any
        already-queued datagrams — no data is ever discarded.
        """
        self._close_remote_requested = True
        self._close_event.set()

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
        """Return the next inbound datagram, or ``None`` when the flow is closed.

        Drains any already-queued datagrams before honouring the close flag so
        that no data is lost when ``close_remote()`` races with a queued item.
        """
        if not self._inbound.empty():
            return self._inbound.get_nowait()
        if self._close_remote_requested:
            return None
        # Block until a datagram arrives OR the flow is closed.
        data_task: asyncio.Task[bytes | None] = asyncio.create_task(
            self._inbound.get()
        )
        close_task: asyncio.Task[None] = asyncio.create_task(
            self._close_event.wait()
        )
        done, pending = await asyncio.wait(
            {data_task, close_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        if data_task in done:
            return data_task.result()
        # close_event fired — drain one last time before returning None.
        if not self._inbound.empty():
            return self._inbound.get_nowait()
        return None

    @property
    def flow_id(self) -> str:
        return self._id

    async def close(self) -> None:
        self._registry.pop(self._id, None)
        await self._ws_send(encode_udp_close_frame(self._id), control=True)
