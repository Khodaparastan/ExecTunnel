"""UDP flow handler — local side of a SOCKS5 UDP ASSOCIATE flow.

Data flow:
    * **outbound**: local SOCKS5 relay → ``encode_udp_data_frame`` → WebSocket.
    * **inbound**:  WebSocket ``UDP_DATA`` frames → local relay.

Lifecycle:
    1. Create handler and register it in the shared registry.
    2. Call :meth:`UdpFlow.open` to send ``UDP_OPEN`` to the agent.
    3. Call :meth:`UdpFlow.send_datagram` / :meth:`UdpFlow.recv_datagram`
       to relay datagrams.
    4. Call :meth:`UdpFlow.close` for local teardown, or the agent signals
       closure → session layer calls :meth:`UdpFlow.on_remote_closed`.
    5. Both paths set ``_closed_event`` so :meth:`UdpFlow.recv_datagram`
       unblocks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Final

from exectunnel.defaults import Defaults
from exectunnel.exceptions import (
    ConnectionClosedError,
    TransportError,
    WebSocketSendTimeoutError,
)
from exectunnel.observability import metrics_gauge_dec, metrics_inc
from exectunnel.protocol import (
    encode_udp_close_frame,
    encode_udp_data_frame,
    encode_udp_open_frame,
)

from ._types import UdpRegistry, WsSendCallable
from ._validation import require_bytes
from ._waiting import wait_first

__all__ = ["UdpFlow"]

_log: Final[logging.Logger] = logging.getLogger(__name__)


class UdpFlow:
    """Bridges one SOCKS5 UDP ASSOCIATE flow through the tunnel.

    Args:
        flow_id: Stable identifier for this UDP flow (from
            :func:`~exectunnel.protocol.new_flow_id`).
        host: Destination hostname or IP address string.
        port: Destination UDP port (1–65535).
        ws_send: Coroutine callable conforming to
            :class:`~exectunnel.transport._types.WsSendCallable`.
        registry: Shared ``flow_id → UdpFlow`` mapping; the handler
            removes itself on :meth:`close` and :meth:`on_remote_closed`.

    Note:
        Satisfies :class:`~exectunnel.transport._types.TransportHandler`.
    """

    __slots__ = (
        "_id",
        "_host",
        "_port",
        "_ws_send",
        "_registry",
        "_inbound",
        "_closed_event",
        "_opened",
        "_closed",
        "_drop_count",
        "_bytes_sent",
        "_bytes_recv",
    )

    def __init__(
        self,
        flow_id: str,
        host: str,
        port: int,
        ws_send: WsSendCallable,
        registry: UdpRegistry,
    ) -> None:
        self._id = flow_id
        self._host = host
        self._port = port
        self._ws_send = ws_send
        self._registry = registry

        self._inbound: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=Defaults.UDP_INBOUND_QUEUE_CAP
        )
        self._closed_event = asyncio.Event()

        self._opened = False
        self._closed = False

        self._drop_count = 0
        self._bytes_sent = 0
        self._bytes_recv = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def open(self) -> None:
        """Send the ``UDP_OPEN`` control frame to the remote agent.

        Idempotent after a successful open — subsequent calls are no-ops.
        A failed open does not set ``_opened``, so the caller may retry.

        :exc:`~exectunnel.exceptions.ProtocolError` from the protocol
        layer is not caught — it signals an invalid host or port and
        must propagate to the session layer.

        Raises:
            TransportError: If the flow is already closed (``error_code``
                ``"transport.udp_open_on_closed"``), or for any send
                failure other than those listed below (``error_code``
                ``"transport.udp_open_failed"``).
            ProtocolError: If *host* or *port* is frame-unsafe —
                propagated from
                :func:`~exectunnel.protocol.encode_udp_open_frame`.
            WebSocketSendTimeoutError: If the frame cannot be delivered
                within the send timeout.
            ConnectionClosedError: If the WebSocket connection is already
                closed.
        """
        if self._opened:
            return

        if self._closed:
            raise TransportError(
                f"UDP flow {self._id!r}: open() called on a closed flow.",
                error_code="transport.udp_open_on_closed",
                details={"flow_id": self._id},
                hint="A closed flow cannot be reopened. Create a new UdpFlow instead.",
            )

        frame = encode_udp_open_frame(self._id, self._host, self._port)

        try:
            await self._ws_send(frame, control=True)
        except (WebSocketSendTimeoutError, ConnectionClosedError):
            raise
        except Exception as exc:
            raise TransportError(
                f"UDP flow {self._id!r}: failed to send UDP_OPEN frame.",
                error_code="transport.udp_open_failed",
                details={"flow_id": self._id, "host": self._host, "port": self._port},
                hint="Check WebSocket connectivity to the remote agent.",
            ) from exc

        self._opened = True
        metrics_inc("udp.flow.opened")
        _log.debug("udp flow %s opened → %s:%d", self._id, self._host, self._port)

    async def close(self) -> None:
        """Evict this flow from the registry and send the ``UDP_CLOSE`` frame.

        Idempotent — subsequent calls are no-ops. Unblocks any coroutine
        waiting in :meth:`recv_datagram`. Queued datagrams at close time
        are silently abandoned — intentional UDP semantics.

        ``UDP_CLOSE`` is only sent when :meth:`open` completed
        successfully.

        Raises:
            WebSocketSendTimeoutError: If the close frame send times out.
            TransportError: For any other send failure (``error_code``
                ``"transport.udp_close_failed"``).
        """
        if self._closed:
            return
        self._closed = True
        self._closed_event.set()
        self._evict()
        metrics_inc("udp.flow.closed")
        _log.debug("udp flow %s closing (local)", self._id)

        if not self._opened:
            _log.debug(
                "udp flow %s: skipping UDP_CLOSE — open() never completed", self._id
            )
            return

        try:
            await self._ws_send(encode_udp_close_frame(self._id), control=True)
        except ConnectionClosedError:
            metrics_inc("udp.flow.close.connection_already_closed")
            _log.debug(
                "udp flow %s: connection already closed while sending UDP_CLOSE",
                self._id,
            )
        except WebSocketSendTimeoutError:
            raise
        except Exception as exc:
            raise TransportError(
                f"UDP flow {self._id!r}: failed to send UDP_CLOSE frame.",
                error_code="transport.udp_close_failed",
                details={"flow_id": self._id},
                hint="The remote agent will time out the flow independently.",
            ) from exc

    def on_remote_closed(self) -> None:
        """Signal that the agent has closed its side of the flow.

        Unblocks any coroutine waiting in :meth:`recv_datagram`.
        Idempotent. Safe to call before :meth:`open` or after
        :meth:`close`.

        Note:
            Satisfies :class:`~exectunnel.transport._types.TransportHandler`.
        """
        if not self._closed:
            self._closed = True
            self._evict()
            metrics_inc("udp.flow.closed_remote")
            _log.debug("udp flow %s closed by remote", self._id)
        self._closed_event.set()

    # ── Inbound (remote → local) ──────────────────────────────────────────────

    def feed(self, data: bytes) -> None:
        """Enqueue an inbound datagram received from the remote agent.

        Silently drops when the inbound queue is full — intentional UDP
        semantics. No-op if the flow is already closed.

        Args:
            data: Raw bytes decoded from a ``UDP_DATA`` frame payload.

        Raises:
            TransportError: If *data* is not a :class:`bytes` instance
                (``error_code`` ``"transport.invalid_payload_type"``).
        """
        require_bytes(data, self._id, "feed")

        if self._closed:
            metrics_inc("udp.flow.feed_after_close.drop")
            return

        try:
            self._inbound.put_nowait(data)
        except asyncio.QueueFull:
            self._drop_count += 1
            metrics_inc("udp.flow.inbound_queue.drop")
            if self._drop_count == 1 or self._drop_count % Defaults.UDP_WARN_EVERY == 0:
                _log.warning(
                    "udp flow %s inbound queue full, dropping datagram "
                    "(total_drops=%d)",
                    self._id,
                    self._drop_count,
                )
            return

        self._bytes_recv += len(data)
        metrics_inc("udp.flow.datagram.accepted")

    async def recv_datagram(self) -> bytes | None:
        """Return the next inbound datagram, or ``None`` when the flow is closed.

        First drains the queue without blocking. When the queue is empty
        and the flow is not yet closed, waits on whichever arrives
        first: a new datagram or the close event.

        Returns:
            The next datagram bytes, or ``None`` if the flow has closed
            and no more datagrams remain.

        Raises:
            asyncio.CancelledError: Propagated unchanged.
        """
        try:
            return self._inbound.get_nowait()
        except asyncio.QueueEmpty:
            pass

        if self._closed_event.is_set():
            return None

        primary_won, value = await wait_first(
            self._inbound.get(),
            self._closed_event,
            primary_name=f"udp-flow-get-{self._id}",
            event_name=f"udp-flow-close-{self._id}",
        )
        if primary_won:
            return value

        # Close raced ahead of a queued put; last-chance drain.
        try:
            return self._inbound.get_nowait()
        except asyncio.QueueEmpty:
            return None

    # ── Outbound (local → remote) ─────────────────────────────────────────────

    async def send_datagram(self, data: bytes) -> None:
        """Encode *data* as a ``UDP_DATA`` frame and forward it to the agent.

        One datagram = one frame. The caller must ensure *data* fits
        within the protocol payload budget
        (:data:`~exectunnel.transport._constants.MAX_DATA_CHUNK_BYTES`
        raw bytes). No-op if the flow is already closed.

        Args:
            data: Raw datagram bytes to forward to the agent.

        Raises:
            TransportError: If *data* is not a :class:`bytes` instance
                (``"transport.invalid_payload_type"``), if :meth:`open`
                has not completed (``"transport.udp_send_before_open"``),
                or for any other send failure
                (``"transport.udp_data_send_failed"``).
            WebSocketSendTimeoutError: If the send stalls beyond the
                timeout.
            ConnectionClosedError: If the connection is closed before
                sending.
        """
        require_bytes(data, self._id, "send_datagram")

        if self._closed:
            return

        if not self._opened:
            raise TransportError(
                f"UDP flow {self._id!r}: send_datagram() called before "
                "open() completed.",
                error_code="transport.udp_send_before_open",
                details={"flow_id": self._id},
                hint="Await open() before sending datagrams.",
            )

        try:
            await self._ws_send(encode_udp_data_frame(self._id, data))
        except (WebSocketSendTimeoutError, ConnectionClosedError):
            raise
        except Exception as exc:
            raise TransportError(
                f"UDP flow {self._id!r}: failed to send UDP_DATA frame.",
                error_code="transport.udp_data_send_failed",
                details={"flow_id": self._id, "payload_bytes": len(data)},
                hint="Check WebSocket connectivity; datagram has been dropped.",
            ) from exc

        self._bytes_sent += len(data)
        metrics_inc("udp.flow.datagram.sent")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _evict(self) -> None:
        """Remove this flow from the registry and decrement the active gauge."""
        if self._registry.pop(self._id, None) is not None:
            metrics_gauge_dec("session.active.udp_flows")

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def flow_id(self) -> str:
        """The stable identifier for this UDP flow."""
        return self._id

    @property
    def is_opened(self) -> bool:
        """``True`` once :meth:`open` has completed successfully."""
        return self._opened

    @property
    def is_closed(self) -> bool:
        """``True`` once :meth:`close` or :meth:`on_remote_closed` has been called."""
        return self._closed

    @property
    def drop_count(self) -> int:
        """Total inbound datagrams dropped due to a full queue."""
        return self._drop_count

    @property
    def bytes_sent(self) -> int:
        """Total raw bytes successfully forwarded to the agent."""
        return self._bytes_sent

    @property
    def bytes_recv(self) -> int:
        """Total raw bytes received from the agent."""
        return self._bytes_recv

    def __repr__(self) -> str:
        state = "closed" if self._closed else ("opened" if self._opened else "new")
        return (
            f"<UdpFlow id={self._id} state={state} "
            f"dest={self._host}:{self._port} "
            f"sent={self._bytes_sent} recv={self._bytes_recv} "
            f"drops={self._drop_count}>"
        )
