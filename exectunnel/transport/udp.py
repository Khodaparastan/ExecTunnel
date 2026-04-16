"""
UDP flow handler — local side of a SOCKS5 UDP ASSOCIATE flow.

Data flow
---------
* **outbound**: local SOCKS5 relay → ``encode_udp_data_frame`` → WebSocket
* **inbound**:  WebSocket ``UDP_DATA`` frames (queued by recv_loop) → local relay

Lifecycle
---------
1. Create handler, register it in the shared registry.
2. Call :meth:`~UdpFlow.open` to send ``UDP_OPEN`` to the agent.
3. Call :meth:`~UdpFlow.send_datagram` / :meth:`~UdpFlow.recv_datagram`
   to relay datagrams.
4. Call :meth:`~UdpFlow.close` for local teardown (sends ``UDP_CLOSE``
   to the agent only if ``open()`` succeeded).
   OR the agent signals closure → recv_loop calls
   :meth:`~UdpFlow.on_remote_closed`.
5. Both paths set ``_closed`` so :meth:`~UdpFlow.recv_datagram` unblocks.

Protocol alignment
------------------
All frame encoding uses the typed helpers from :mod:`exectunnel.protocol`
(imported from the package root per Golden Rule #1):

* :func:`~exectunnel.protocol.encode_udp_open_frame`  — validates host/port
  via :func:`~exectunnel.protocol.encode_host_port`.
* :func:`~exectunnel.protocol.encode_udp_data_frame`  — accepts raw ``bytes``;
  applies ``urlsafe_b64encode`` with no padding internally.
* :func:`~exectunnel.protocol.encode_udp_close_frame` — no payload.

No manual base64 encoding is performed in this module.

Layer contract
--------------
This module must never import from:

* ``exectunnel.protocol.frames``  (use the package root)
* ``exectunnel.proxy``            (SOCKS5 is not this layer's concern)
* ``exectunnel.session``          (session orchestration is above this layer)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

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

__all__ = ["UdpFlow"]

logger = logging.getLogger(__name__)


class UdpFlow:
    """Bridges one SOCKS5 UDP ASSOCIATE flow through the tunnel.

    Args:
        flow_id:  Stable identifier for this UDP flow (from
                  :func:`~exectunnel.protocol.new_flow_id`).
        host:     Destination hostname or IP address string.
        port:     Destination UDP port (1–65535).
        ws_send:  Coroutine callable that sends a frame string over the
                  WebSocket / exec channel. Must conform to
                  :class:`~exectunnel.transport._types.WsSendCallable`.
        registry: Shared mapping of ``flow_id → UdpFlow``; the handler
                  removes itself on :meth:`close` and :meth:`on_remote_closed`.

    Lifecycle flags
    ---------------
    * ``_opened`` — set only **after** ``UDP_OPEN`` is successfully sent so
      that a failed open can be retried.  Idempotency of :meth:`open` is
      guarded by this flag.
    * ``_closed`` — set at the start of :meth:`close` / :meth:`on_remote_closed`
      to prevent concurrent teardown.
    * ``_closed_event`` — set by both teardown paths; used by
      :meth:`recv_datagram` to unblock promptly on closure.

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
        "_close_task",
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
        self._close_task: asyncio.Task[None] | None = None

        self._opened = False
        self._closed = False

        self._drop_count = 0
        self._bytes_sent = 0
        self._bytes_recv = 0

    async def open(self) -> None:
        """Send the ``UDP_OPEN`` control frame to the remote agent.

        Idempotent after a **successful** open — subsequent calls are no-ops.
        A failed open (e.g. send timeout) does **not** set ``_opened``, so
        the caller may retry.

        Host/port validation
        --------------------
        :func:`~exectunnel.protocol.encode_udp_open_frame` raises
        :class:`~exectunnel.exceptions.ProtocolError` when the host or port
        is frame-unsafe.  Per Golden Rule #6, ``ProtocolError`` indicates a
        bug in the calling layer and must **not** be caught here — it
        propagates to the session layer which is responsible for ensuring only
        valid hosts are passed to this method.

        Raises:
            TransportError:
                If the flow is already closed (``error_code``
                ``"transport.udp_open_on_closed"``).
            ProtocolError:
                If *host* or *port* is frame-unsafe — propagated from
                :func:`~exectunnel.protocol.encode_udp_open_frame` without
                wrapping.  This is a caller bug, not a transport error.
            WebSocketSendTimeoutError:
                If the control frame cannot be delivered within the configured
                send timeout.
            ConnectionClosedError:
                If the underlying WebSocket connection is already closed.
            TransportError:
                For any other unexpected transport-level failure during the
                open handshake (``error_code`` ``"transport.udp_open_failed"``).
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

        # ProtocolError from encode_udp_open_frame propagates uncaught —
        # it signals a bug in the caller (invalid host/port), not a
        # transport failure.  See Golden Rule #6.
        frame = encode_udp_open_frame(self._id, self._host, self._port)

        try:
            await self._ws_send(frame, control=True)
        except (WebSocketSendTimeoutError, ConnectionClosedError):
            # Do NOT set _opened — allow retry after transient failure.
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

        self._opened = True
        metrics_inc("udp.flow.opened")
        logger.debug("udp flow %s opened → %s:%d", self._id, self._host, self._port)

    async def close(self) -> None:
        """Evict this flow from the registry and send the ``UDP_CLOSE`` frame.

        Idempotent — subsequent calls are no-ops.

        Sets the closed event so any coroutine blocked in
        :meth:`recv_datagram` unblocks immediately.

        Any datagrams still queued in ``_inbound`` at close time are silently
        abandoned — this is intentional UDP semantics; datagrams may be lost.

        ``UDP_CLOSE`` is only sent when :meth:`open` completed successfully
        (``_opened is True``).  If ``open()`` was never called or failed, the
        agent has no record of this flow and sending a close frame for an
        unknown flow ID would be a wasted frame (the agent drops it, but the
        log noise is undesirable).

        Raises:
            WebSocketSendTimeoutError:
                If the close frame cannot be delivered within the configured
                send timeout.
            TransportError:
                For any other transport-level failure during teardown
                (``error_code`` ``"transport.udp_close_failed"``).
        """
        if self._closed:
            return
        self._closed = True

        self._closed_event.set()
        self._evict()
        metrics_inc("udp.flow.closed")
        logger.debug("udp flow %s closing (local)", self._id)

        if not self._opened:
            logger.debug(
                "udp flow %s: skipping UDP_CLOSE — open() never completed",
                self._id,
            )
            return

        try:
            await self._ws_send(encode_udp_close_frame(self._id), control=True)
        except ConnectionClosedError:
            metrics_inc("udp.flow.close.connection_already_closed")
            logger.debug(
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

        This method *reacts to* a remote ``UDP_CLOSE`` event — it does not
        initiate a remote close.  The session layer calls this when it
        receives a ``UDP_CLOSE`` frame for this flow's ID.

        Sets the closed event so :meth:`recv_datagram` drains remaining
        queued datagrams and then returns ``None``.

        Idempotent — subsequent calls are no-ops.
        Safe to call before :meth:`open` or after :meth:`close`.

        Satisfies :class:`~exectunnel.transport._types.TransportHandler`.
        """
        if not self._closed:
            self._closed = True
            self._evict()
            metrics_inc("udp.flow.closed_remote")
            logger.debug("udp flow %s closed by remote", self._id)
        self._closed_event.set()

    # ── Inbound (remote → local) ──────────────────────────────────────────────

    def feed(self, data: bytes) -> None:
        """Enqueue an inbound datagram received from the remote agent.

        Silently drops the datagram and increments the drop counter when the
        inbound queue is full — this is intentional UDP semantics.

        No-op if the flow has already been closed.

        Args:
            data: Raw bytes decoded from a ``UDP_DATA`` frame payload.
                  Must be a ``bytes`` instance — never ``str`` or ``bytearray``.

        Raises:
            TransportError: If *data* is not a ``bytes`` instance
                (``error_code`` ``"transport.invalid_payload_type"``).
        """
        require_bytes(data, self._id, "feed")

        # Guard against feeding a closed flow — data would sit in the queue
        # forever since recv_datagram() will never be called again.
        if self._closed:
            metrics_inc("udp.flow.feed_after_close.drop")
            return

        try:
            self._inbound.put_nowait(data)
            self._bytes_recv += len(data)
            metrics_inc("udp.flow.datagram.accepted")
        except asyncio.QueueFull:
            self._drop_count += 1
            metrics_inc("udp.flow.inbound_queue.drop")
            if self._drop_count == 1 or self._drop_count % Defaults.UDP_WARN_EVERY == 0:
                logger.warning(
                    "udp flow %s inbound queue full, dropping datagram "
                    "(total_drops=%d)",
                    self._id,
                    self._drop_count,
                )

    async def recv_datagram(self) -> bytes | None:
        """Return next inbound datagram or None when closed."""
        try:
            return self._inbound.get_nowait()
        except asyncio.QueueEmpty:
            pass

        if self._closed_event.is_set():
            return None

        # Lazily create the close_task once and reuse it across all blocking
        # recv calls.  Recreate only if the previous task completed (which
        # happens when the close event fires) — in that case the fast-path
        # check above would normally catch it, but a narrow race exists when
        # the event is set between get_nowait() and is_set().
        if self._close_task is None or self._close_task.done():
            self._close_task = asyncio.create_task(
                self._closed_event.wait(),
                name=f"udp-flow-close-{self._id}",
            )

        get_task = asyncio.create_task(
            self._inbound.get(),
            name=f"udp-flow-get-{self._id}",
        )

        try:
            done, pending = await asyncio.wait(
                {get_task, self._close_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            get_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await get_task
            raise

        if get_task in pending:
            get_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await get_task

        if get_task in done and not get_task.cancelled():
            return get_task.result()

        try:
            return self._inbound.get_nowait()
        except asyncio.QueueEmpty:
            return None

    # ── Outbound (local → remote) ─────────────────────────────────────────────

    async def send_datagram(self, data: bytes) -> None:
        """Encode *data* as a ``UDP_DATA`` frame and forward it to the agent.

        Uses :func:`~exectunnel.protocol.encode_udp_data_frame` which accepts
        raw ``bytes`` and applies ``urlsafe_b64encode`` with no padding
        internally — no manual base64 encoding is performed here.

        UDP datagrams are **never split** — one datagram = one frame.  The
        caller is responsible for ensuring *data* fits within the protocol's
        maximum payload budget (≤ 6,108 bytes).

        Guards against sending before :meth:`open` completes — the agent
        would not recognise the flow ID and silently drop the datagram.

        Args:
            data: Raw datagram bytes to forward to the agent.

        Raises:
            TransportError:
                If *data* is not a ``bytes`` instance
                (``error_code`` ``"transport.invalid_payload_type"``).
            TransportError:
                If the flow has not been opened yet
                (``error_code`` ``"transport.udp_send_before_open"``).
            WebSocketSendTimeoutError:
                If the WebSocket send stalls beyond the configured timeout.
            ConnectionClosedError:
                If the connection is closed before the frame can be sent.
            TransportError:
                For any other transport-level failure
                (``error_code`` ``"transport.udp_data_send_failed"``).
        """
        require_bytes(data, self._id, "send_datagram")

        # Guard against sending after close — the agent will silently drop
        # UDP_DATA frames received after UDP_CLOSE, but skipping the send
        # avoids a wasted frame on an already-torn-down flow.
        if self._closed:
            return

        # Guard against sending before the agent knows about this flow.
        # The agent would drop the datagram for an unregistered flow ID,
        # wasting bandwidth and generating agent-side warnings.
        if not self._opened:
            raise TransportError(
                f"UDP flow {self._id!r}: send_datagram() called before open() completed.",
                error_code="transport.udp_send_before_open",
                details={"flow_id": self._id},
                hint=(
                    "Await open() before sending datagrams. The agent does not "
                    "recognise this flow ID until it receives the UDP_OPEN frame."
                ),
            )

        try:
            await self._ws_send(encode_udp_data_frame(self._id, data))
            self._bytes_sent += len(data)
            metrics_inc("udp.flow.datagram.sent")
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

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _evict(self) -> None:
        """Remove this flow from the shared registry and decrement the active-flow gauge."""
        if self._registry.pop(self._id, None) is not None:
            metrics_gauge_dec("session.active.udp_flows")

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
        """Total number of inbound datagrams dropped due to a full queue."""
        return self._drop_count

    @property
    def bytes_sent(self) -> int:
        """Total raw bytes successfully forwarded to the agent (outbound)."""
        return self._bytes_sent

    @property
    def bytes_recv(self) -> int:
        """Total raw bytes received from the agent (inbound)."""
        return self._bytes_recv

    # ── Debug ─────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        state = "closed" if self._closed else ("opened" if self._opened else "new")
        return (
            f"<UdpFlow id={self._id} state={state} "
            f"dest={self._host}:{self._port} "
            f"sent={self._bytes_sent} recv={self._bytes_recv} "
            f"drops={self._drop_count}>"
        )
