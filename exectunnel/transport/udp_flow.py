"""UDP flow handler — local side of a SOCKS5 UDP ASSOCIATE flow."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from exectunnel.config.defaults import TCP_INBOUND_QUEUE_CAP, UDP_WARN_EVERY
from exectunnel.exceptions import (
    ConnectionClosedError,
    FrameDecodingError,
    TransportError,
    WebSocketSendTimeoutError,
)
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

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def open(self) -> None:
        """Send the UDP_OPEN control frame to the remote agent.

        Raises
        ------
        WebSocketSendTimeoutError
            If the control frame cannot be delivered within the configured
            send timeout.
        ConnectionClosedError
            If the underlying WebSocket connection is already closed when
            the open frame is attempted.
        TransportError
            For any other transport-level failure during the open handshake.
        """
        try:
            await self._ws_send(
                encode_udp_open_frame(self._id, self._host, self._port),
                control=True,
            )
        except WebSocketSendTimeoutError:
            raise
        except ConnectionClosedError:
            raise
        except Exception as exc:
            raise TransportError(
                f"UDP flow {self._id!r}: failed to send UDP_OPEN frame.",
                error_code="transport.udp_open_failed",
                details={
                    "flow_id": self._id,
                    "host": self._host,
                    "port": self._port,
                },
                hint="Check WebSocket connectivity to the remote agent.",
            ) from exc

    async def close(self) -> None:
        """Evict this flow from the registry and send the UDP_CLOSE control frame.

        Raises
        ------
        WebSocketSendTimeoutError
            If the close frame cannot be delivered within the configured
            send timeout.
        ConnectionClosedError
            If the connection is already gone — treated as a no-op because
            the remote side will time-out the flow independently.
        TransportError
            For any other transport-level failure during teardown.
        """
        self._registry.pop(self._id, None)
        try:
            await self._ws_send(encode_udp_close_frame(self._id), control=True)
        except ConnectionClosedError:
            # Remote is already gone; the flow is effectively closed.
            logger.debug(
                "udp flow %s: connection already closed while sending UDP_CLOSE "
                "(remote will time-out the flow independently)",
                self._id,
            )
        except WebSocketSendTimeoutError:
            raise
        except Exception as exc:
            raise TransportError(
                f"UDP flow {self._id!r}: failed to send UDP_CLOSE frame.",
                error_code="transport.udp_close_failed",
                details={"flow_id": self._id},
                hint="The remote agent will time-out the flow independently.",
            ) from exc

    # ── Inbound (remote → local) ──────────────────────────────────────────────

    def feed(self, data: bytes) -> None:
        """Enqueue an inbound datagram received from the remote agent.

        Silently drops the datagram and increments the drop counter when the
        inbound queue is full — this is intentional UDP semantics.

        Raises
        ------
        FrameDecodingError
            If *data* is ``None`` or not a ``bytes`` instance, indicating the
            caller passed a malformed or already-decoded payload.
        """
        if not isinstance(data, bytes):
            raise FrameDecodingError(
                f"UDP flow {self._id!r}: feed() received a non-bytes payload.",
                error_code="protocol.udp_feed_bad_type",
                details={
                    "flow_id": self._id,
                    "received_type": type(data).__name__,
                },
                hint="Ensure the frame decoder passes raw bytes to feed().",
            )
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

    async def recv_datagram(self) -> bytes | None:
        """Return the next inbound datagram, or ``None`` when the flow is closed.

        Drains any already-queued datagrams before honouring the close flag so
        that no data is lost when ``close_remote()`` races with a queued item.

        Raises
        ------
        TransportError
            If an unexpected error occurs while waiting on the inbound queue
            or the close event (e.g. the event loop is shutting down).
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

        try:
            done, pending = await asyncio.wait(
                {data_task, close_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except Exception as exc:
            data_task.cancel()
            close_task.cancel()
            raise TransportError(
                f"UDP flow {self._id!r}: unexpected error while waiting for datagram.",
                error_code="transport.udp_recv_wait_failed",
                details={"flow_id": self._id},
                hint="This may indicate an event-loop shutdown or task cancellation.",
            ) from exc

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

    # ── Outbound (local → remote) ─────────────────────────────────────────────

    async def send_datagram(self, data: bytes) -> None:
        """Encode *data* as a UDP_DATA frame and forward it to the remote agent.

        The frame is placed on the *data* send queue (not the control queue).
        When the data queue is full ``_ws_send`` drops the frame silently —
        this is intentional UDP semantics (datagrams may be lost).

        Raises
        ------
        FrameDecodingError
            If *data* is not a ``bytes`` instance.
        WebSocketSendTimeoutError
            If the WebSocket send stalls beyond the configured timeout.
        ConnectionClosedError
            If the connection is closed before the frame can be sent.
        TransportError
            For any other transport-level failure during the send.
        """
        if not isinstance(data, bytes):
            raise FrameDecodingError(
                f"UDP flow {self._id!r}: send_datagram() received a non-bytes payload.",
                error_code="protocol.udp_send_bad_type",
                details={
                    "flow_id": self._id,
                    "received_type": type(data).__name__,
                },
                hint="Ensure callers pass raw bytes to send_datagram().",
            )
        b64 = base64.b64encode(data).decode()
        try:
            await self._ws_send(encode_udp_data_frame(self._id, b64))
        except (WebSocketSendTimeoutError, ConnectionClosedError):
            raise
        except Exception as exc:
            raise TransportError(
                f"UDP flow {self._id!r}: failed to send UDP_DATA frame.",
                error_code="transport.udp_data_send_failed",
                details={
                    "flow_id": self._id,
                    "payload_bytes": len(data),
                },
                hint="Check WebSocket connectivity; datagram has been dropped.",
            ) from exc

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def flow_id(self) -> str:
        """The stable identifier for this UDP flow."""
        return self._id

    @property
    def drop_count(self) -> int:
        """Total number of inbound datagrams dropped due to a full queue."""
        return self._drop_count

    @property
    def is_closed(self) -> bool:
        """``True`` once ``close_remote()`` has been called."""
        return self._close_remote_requested
