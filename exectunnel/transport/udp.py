"""
UDP flow handler — local side of a SOCKS5 UDP ASSOCIATE flow.

Data flow
---------
* **outbound**: local SOCKS5 relay → ``encode_udp_data_frame`` → WebSocket
* **inbound**:  WebSocket UDP_DATA frames (queued by recv_loop) → local relay

Lifecycle
---------
1. Create handler, register it in the shared registry.
2. Call :meth:`~UdpFlow.open` to send ``UDP_OPEN`` to the agent.
3. Call :meth:`~UdpFlow.send_datagram` / :meth:`~UdpFlow.recv_datagram`
   to relay datagrams.
4. Call :meth:`~UdpFlow.close` for local teardown (sends ``UDP_CLOSE``
   to the agent).
   OR the agent signals closure → recv_loop calls
   :meth:`~UdpFlow.on_remote_closed`.
5. Both paths set ``_closed`` so :meth:`~UdpFlow.recv_datagram` unblocks.

Protocol alignment
------------------
All frame encoding uses the typed helpers from :mod:`exectunnel.protocol.frames`:

* :func:`~exectunnel.protocol.frames.encode_udp_open_frame`  — validates host
  via :func:`~exectunnel.protocol.frames.encode_host_port`.
* :func:`~exectunnel.protocol.frames.encode_udp_data_frame`  — accepts raw
  ``bytes``; applies ``urlsafe_b64encode`` with no padding internally.
* :func:`~exectunnel.protocol.frames.encode_udp_close_frame` — no payload.

No manual base64 encoding is performed in this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from exectunnel.config.defaults import UDP_INBOUND_QUEUE_CAP, UDP_WARN_EVERY
from exectunnel.exceptions import (
    ConnectionClosedError,
    TransportError,
    WebSocketSendTimeoutError,
)
from exectunnel.observability import metrics_inc
from exectunnel.protocol.frames import (
    encode_udp_close_frame,
    encode_udp_data_frame,
    encode_udp_open_frame,
)
from exectunnel.transport._types import UdpRegistry, WsSendCallable
from exectunnel.transport._validation import require_bytes

__all__ = ["UdpFlow"]

logger = logging.getLogger(__name__)


class UdpFlow:
    """Bridges one SOCKS5 UDP ASSOCIATE flow through the tunnel.

    Args:
        flow_id:  Stable identifier for this UDP flow.
        host:     Destination hostname or IP address string.
        port:     Destination UDP port.
        ws_send:  Coroutine callable that sends a frame string over the
                  WebSocket / exec channel. Must conform to
                  :class:`~exectunnel.transport._types.WsSendCallable`.
        registry: Shared mapping of ``flow_id → handler``; the handler
                  removes itself on :meth:`close` and :meth:`on_remote_closed`.

    Lifecycle flags
    ---------------
    A single ``_opened`` bool replaces the previous redundant pair of
    ``_open_attempted`` + ``_opened``. Idempotency of :meth:`open` is
    guarded by ``_opened``; retry-ability after a failed open is preserved
    because ``_opened`` is only set after a **successful** send.

    The ``_closed_event`` is set by both :meth:`close` (local teardown) and
    :meth:`on_remote_closed` (agent-initiated teardown). It is used by
    :meth:`recv_datagram` to unblock promptly on closure.
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

        # Inbound queue: agent → local relay.
        self._inbound: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=UDP_INBOUND_QUEUE_CAP
        )

        # Unified close event — set by both close() (local) and
        # on_remote_closed() (agent-initiated). recv_datagram() races this
        # against the inbound queue so it always unblocks promptly on closure.
        self._closed_event: asyncio.Event = asyncio.Event()

        # Reusable close_task for recv_datagram() — created once on first
        # blocking recv and reused across all subsequent calls to avoid
        # spawning a new Event.wait() coroutine per recv_datagram() call.
        # None until the first blocking recv_datagram() call.
        self._close_task: asyncio.Task[None] | None = None

        # Lifecycle flags.
        # _opened: set only AFTER UDP_OPEN is successfully sent so that a
        #   failed open can be retried.
        # _closed: set at the start of close() / on_remote_closed() to
        #   prevent concurrent teardown.
        self._opened: bool = False
        self._closed: bool = False

        # Telemetry.
        self._drop_count: int = 0
        self._bytes_sent: int = 0
        self._bytes_recv: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def open(self) -> None:
        """Send the ``UDP_OPEN`` control frame to the remote agent.

        Idempotent after a **successful** open — subsequent calls are no-ops.
        A failed open (e.g. invalid host, send timeout) does **not** set
        ``_opened``, so the caller may retry with corrected arguments.

        Guards against calling open() on an already-closed flow — a closed
        flow cannot be reopened and the call raises immediately.

        Raises:
            TransportError:
                * If the flow is already closed.
                * If *host* contains frame-unsafe characters (propagated from
                  :func:`~exectunnel.protocol.frames.encode_host_port` as
                  ``ValueError``, wrapped here).
                * For any other transport-level failure during the open
                  handshake.
            WebSocketSendTimeoutError:
                If the control frame cannot be delivered within the configured
                send timeout.
            ConnectionClosedError:
                If the underlying WebSocket connection is already closed.
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

        try:
            frame = encode_udp_open_frame(self._id, self._host, self._port)
        except ValueError as exc:
            # encode_host_port raises ValueError for frame-unsafe host strings.
            # Do NOT set _opened — the caller may retry with a valid host.
            raise TransportError(
                f"UDP flow {self._id!r}: invalid host {self._host!r} for UDP_OPEN frame.",
                error_code="transport.udp_open_invalid_host",
                details={
                    "flow_id": self._id,
                    "host": self._host,
                    "port": self._port,
                },
                hint=(
                    "The destination host contains characters that are unsafe in "
                    "the tunnel frame format. Validate the host before opening a flow."
                ),
            ) from exc

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

        # Mark as opened only after successful send.
        self._opened = True
        metrics_inc("udp.flow.opened")
        logger.debug(
            "udp flow %s opened → %s:%d",
            self._id,
            self._host,
            self._port,
        )

    async def close(self) -> None:
        """Evict this flow from the registry and send the ``UDP_CLOSE`` frame.

        Idempotent — subsequent calls are no-ops.

        Sets the closed event so any coroutine blocked in
        :meth:`recv_datagram` unblocks immediately.

        Any datagrams still queued in ``_inbound`` at close time are silently
        abandoned — this is intentional UDP semantics; datagrams may be lost.

        Raises:
            WebSocketSendTimeoutError:
                If the close frame cannot be delivered within the configured
                send timeout.
            TransportError:
                For any other transport-level failure during teardown.
        """
        if self._closed:
            return
        self._closed = True

        # Wake recv_datagram() before touching the network so the caller's
        # receive loop exits cleanly even if ws_send raises.
        self._closed_event.set()
        self._evict()
        metrics_inc("udp.flow.closed")
        logger.debug("udp flow %s closing (local)", self._id)

        try:
            await self._ws_send(encode_udp_close_frame(self._id), control=True)
        except ConnectionClosedError:
            # Remote is already gone; the flow is effectively closed.
            metrics_inc("udp.flow.close.connection_already_closed")
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

    def on_remote_closed(self) -> None:
        """Signal that the agent has closed its side of the flow.

        Renamed from ``close_remote()`` to ``on_remote_closed()`` to clearly
        express that this method *reacts to* a remote event rather than
        *initiating* a remote close.

        Sets the closed event so :meth:`recv_datagram` drains remaining
        queued datagrams and then returns ``None``.

        Idempotent — subsequent calls are no-ops.
        Safe to call before :meth:`open` or after :meth:`close`.
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

        Raises:
            TransportError: If *data* is not a ``bytes`` instance.
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
            if self._drop_count == 1 or self._drop_count % UDP_WARN_EVERY == 0:
                logger.warning(
                    "udp flow %s inbound queue full, dropping datagram "
                    "(total_drops=%d)",
                    self._id,
                    self._drop_count,
                )

    async def recv_datagram(self) -> bytes | None:
        """Return the next inbound datagram, or ``None`` when the flow is closed.

        Drains any already-queued datagrams before honouring the close flag so
        that no data is lost when :meth:`on_remote_closed` races with a queued
        item.

        The internal ``_close_task`` is created once on the first blocking call
        and reused across all subsequent calls — this avoids spawning a new
        ``asyncio.Event.wait()`` coroutine on every blocking recv, which would
        generate significant task churn on high-throughput flows.

        Returns:
            Raw datagram bytes, or ``None`` when the flow is closed and the
            inbound queue is empty.

        Raises:
            asyncio.CancelledError: Propagated as-is — never suppressed.
        """
        # Fast path: drain any immediately available datagrams first.
        try:
            return self._inbound.get_nowait()
        except asyncio.QueueEmpty:
            pass

        # If already closed and queue is empty, signal EOF.
        if self._closed_event.is_set():
            return None

        # Lazily create the close_task once and reuse it across all blocking
        # recv calls — avoids per-call Event.wait() coroutine allocation.
        if self._close_task is None or self._close_task.done():
            self._close_task = asyncio.create_task(
                self._closed_event.wait(),
                name=f"udp-flow-close-{self._id}",
            )

        get_task: asyncio.Task[bytes] = asyncio.create_task(
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
            # Do NOT cancel _close_task — it is reused across calls.
            with contextlib.suppress(asyncio.CancelledError):
                await get_task
            raise

        # Cancel the get_task loser only — _close_task is reused and must
        # not be cancelled here.
        if get_task in pending:
            get_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await get_task

        # get_task won — data arrived.
        if get_task in done and not get_task.cancelled():
            return get_task.result()

        # close_task won — drain one final time in case a datagram arrived
        # between the close signal and the task cancellation.
        try:
            return self._inbound.get_nowait()
        except asyncio.QueueEmpty:
            pass

        return None

    # ── Outbound (local → remote) ─────────────────────────────────────────────

    async def send_datagram(self, data: bytes) -> None:
        """Encode *data* as a ``UDP_DATA`` frame and forward it to the agent.

        Uses :func:`~exectunnel.protocol.frames.encode_udp_data_frame` which
        accepts raw ``bytes`` and applies ``urlsafe_b64encode`` with no padding
        internally — no manual base64 encoding is performed here.

        Args:
            data: Raw datagram bytes to forward to the agent.

        Raises:
            TransportError:            If *data* is not a ``bytes`` instance.
            WebSocketSendTimeoutError: If the WebSocket send stalls beyond the
                                       configured timeout.
            ConnectionClosedError:     If the connection is closed before the
                                       frame can be sent.
            TransportError:            For any other transport-level failure.
        """
        require_bytes(data, self._id, "send_datagram")

        frame = encode_udp_data_frame(self._id, data)

        try:
            await self._ws_send(frame)
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
        """Remove this flow from the shared registry.

        Extracted from both :meth:`close` and :meth:`on_remote_closed` to
        avoid duplicating the registry pop call.
        """
        self._registry.pop(self._id, None)

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
