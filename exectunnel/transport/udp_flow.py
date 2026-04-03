"""UDP flow handler — local side of a SOCKS5 UDP ASSOCIATE flow.

Data flow
---------
* **outbound**: local SOCKS5 relay → ``encode_udp_data_frame`` → WebSocket
* **inbound**:  WebSocket UDP_DATA frames (queued by recv_loop) → local relay

Lifecycle
---------
1. Create handler, register it in the shared registry.
2. Call :meth:`open` to send ``UDP_OPEN`` to the agent.
3. Call :meth:`send_datagram` / :meth:`recv_datagram` to relay datagrams.
4. Call :meth:`close` for local teardown (sends ``UDP_CLOSE`` to agent).
   OR the agent signals closure → recv_loop calls :meth:`close_remote`.
5. Both paths set ``_remote_closed`` so :meth:`recv_datagram` unblocks.

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
from exectunnel.transport.connection import WsSendCallable

logger = logging.getLogger("exectunnel.transport.udp_flow")


class _UdpFlowHandler:
    """Bridges one SOCKS5 UDP ASSOCIATE flow through the tunnel.

    Args:
        flow_id:  Stable identifier for this UDP flow.
        host:     Destination hostname or IP address string.
        port:     Destination UDP port.
        ws_send:  Coroutine callable that sends a frame string over the
                  WebSocket / exec channel.  Must conform to
                  :class:`~exectunnel.transport.connection.WsSendCallable`.
        registry: Shared mapping of ``flow_id → handler``; the handler
                  removes itself on :meth:`close`.
    """

    def __init__(
        self,
        flow_id: str,
        host: str,
        port: int,
        ws_send: WsSendCallable,
        registry: dict[str, _UdpFlowHandler],
    ) -> None:
        self._id = flow_id
        self._host = host
        self._port = port
        self._ws_send = ws_send
        self._registry = registry

        # Inbound queue: agent → local relay.
        self._inbound: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=TCP_INBOUND_QUEUE_CAP
        )

        # Close event — set by both close_remote() (agent-initiated) and
        # close() (local-initiated).  recv_datagram() races this against the
        # inbound queue so it always unblocks promptly on closure.
        self._remote_closed: asyncio.Event = asyncio.Event()

        # Lifecycle guards.
        # _open_attempted: set at the start of open() to prevent concurrent
        #   open calls from racing.
        # _opened: set only after the UDP_OPEN frame is successfully sent.
        # _closed: set at the start of close() to prevent concurrent teardown.
        self._open_attempted: bool = False
        self._opened: bool = False
        self._closed: bool = False

        # Telemetry.
        self._drop_count: int = 0
        self._bytes_sent: int = 0
        self._bytes_recv: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def open(self) -> None:
        """Send the ``UDP_OPEN`` control frame to the remote agent.

        Idempotent — subsequent calls are no-ops.

        ``_opened`` is set only after the frame is successfully sent so that
        a failed open does not silently suppress a retry.

        Raises:
            TransportError:
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
        if self._open_attempted:
            return
        self._open_attempted = True

        try:
            frame = encode_udp_open_frame(self._id, self._host, self._port)
        except ValueError as exc:
            # encode_host_port raises ValueError for frame-unsafe host strings.
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

        Also sets ``_remote_closed`` so any coroutine blocked in
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
        self._remote_closed.set()
        self._registry.pop(self._id, None)
        metrics_inc("udp.flow.closed")
        logger.debug("udp flow %s closing", self._id)

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

        No-op if the flow has already been closed.

        Args:
            data: Raw bytes decoded from a ``UDP_DATA`` frame payload.

        Raises:
            FrameDecodingError: If *data* is not a ``bytes`` instance.
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

    def close_remote(self) -> None:
        """Signal that the agent has closed its side of the flow.

        Sets ``_remote_closed`` so :meth:`recv_datagram` drains remaining
        queued datagrams and then returns ``None``.

        Idempotent — subsequent calls are no-ops.
        Safe to call before :meth:`open` or after :meth:`close`.
        """
        self._remote_closed.set()

    async def recv_datagram(self) -> bytes | None:
        """Return the next inbound datagram, or ``None`` when the flow is closed.

        Drains any already-queued datagrams before honouring the close flag so
        that no data is lost when :meth:`close_remote` races with a queued item.

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
        if self._remote_closed.is_set():
            return None

        # Block until a datagram arrives OR the flow is closed.
        # Tasks are created once per blocking wait — not per loop iteration.
        get_task: asyncio.Task[bytes] = asyncio.create_task(
            self._inbound.get(),
            name=f"udp-flow-get-{self._id}",
        )
        close_task: asyncio.Task[None] = asyncio.create_task(
            self._remote_closed.wait(),
            name=f"udp-flow-close-{self._id}",
        )

        try:
            done, pending = await asyncio.wait(
                {get_task, close_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            get_task.cancel()
            close_task.cancel()
            # return_exceptions=True means gather never raises — suppress is
            # redundant but kept for clarity.
            await asyncio.gather(get_task, close_task, return_exceptions=True)
            raise

        # Cancel and await the loser to release its resources.
        for t in pending:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

        if get_task in done and not get_task.cancelled():
            # Data arrived — return it.
            return get_task.result()

        # close_task fired (or both fired simultaneously).
        # Rescue any item that get_task may have dequeued before being
        # cancelled — dropping it would silently truncate the flow.
        if not get_task.cancelled():
            try:
                item = get_task.result()
                # Queue.get() always returns bytes; result is never None.
                return item
            except Exception:
                pass

        # Drain one final time in case a datagram arrived between the
        # close signal and the task cancellation.
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
            FrameDecodingError:        If *data* is not a ``bytes`` instance.
            WebSocketSendTimeoutError: If the WebSocket send stalls beyond the
                                       configured timeout.
            ConnectionClosedError:     If the connection is closed before the
                                       frame can be sent.
            TransportError:            For any other transport-level failure.
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
    def bytes_sent(self) -> int:
        """Total bytes successfully forwarded to the agent (outbound direction)."""
        return self._bytes_sent

    @property
    def bytes_recv(self) -> int:
        """Total bytes received from the agent (inbound direction)."""
        return self._bytes_recv

    @property
    def is_closed(self) -> bool:
        """``True`` once :meth:`close` or :meth:`close_remote` has been called."""
        return self._remote_closed.is_set()

    @property
    def is_opened(self) -> bool:
        """``True`` once :meth:`open` has completed successfully."""
        return self._opened
