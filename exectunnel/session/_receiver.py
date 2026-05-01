"""Inbound frame receiver and dispatcher for TunnelSession.

Frame dispatch table
--------------------

.. list-table::
   :header-rows: 1

   * - Frame type
     - Action
   * - ``CONN_ACK``
     - Resolve the pending connect future.
   * - ``DATA``
     - Feed bytes to :class:`~exectunnel.transport.TcpConnection` via ``feed()``.
   * - ``CONN_CLOSE``
     - Signal the handler that the remote side closed.
   * - ``UDP_DATA``
     - Feed a datagram to :class:`~exectunnel.transport.UdpFlow`.
   * - ``UDP_CLOSE``
     - Signal the flow that the remote side closed.
   * - ``ERROR``
     - Session-level or per-connection/flow error handling.
   * - ``STATS``
     - Decode and forward to the optional stats listener.
"""

import asyncio
import json
import logging

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from exectunnel.exceptions import (
    ConnectionClosedError,
    CtrlBackpressureError,
    FrameDecodingError,
    PreAckBufferOverflowError,
    TransportError,
    UnexpectedFrameError,
)
from exectunnel.observability import aspan, metrics_inc, metrics_observe
from exectunnel.protocol import (
    FRAME_PREFIX,
    FRAME_SUFFIX,
    MAX_TUNNEL_FRAME_CHARS,
    SESSION_CONN_ID,
    decode_binary_payload,
    decode_error_payload,
    encode_conn_close_frame,
    encode_error_frame,
    parse_frame,
)
from exectunnel.transport import TcpConnection, UdpFlow
from exectunnel.transport._types import WsSendCallable

from ._constants import (
    UNEXPECTED_NO_CONN_ID_FRAMES,
    WS_CLOSE_CODE_INTERNAL_ERROR,
    WS_CLOSE_CODE_PROTOCOL_ERROR,
)
from ._state import AckStatus, PendingConnect
from ._types import AgentStatsCallable, MarkAgentRxCallable

logger = logging.getLogger(__name__)


def _extract_frame_line(line: str) -> str | None:
    """Extract one tunnel frame from a potentially noisy shell line."""

    stripped = line.strip()
    if not stripped:
        return None

    start = stripped.find(FRAME_PREFIX)
    if start < 0:
        return None

    end = stripped.find(FRAME_SUFFIX, start + len(FRAME_PREFIX))
    if end < 0:
        return None

    return stripped[start : end + len(FRAME_SUFFIX)]


class FrameReceiver:
    """Reads WebSocket messages and dispatches frames to registered handlers.

    Shared state (registries, pending connects) is owned by
    :class:`~exectunnel.session._session.TunnelSession` and passed in by
    reference — :class:`FrameReceiver` never copies them.

    Args:
        ws:               Live WebSocket connection.
        ws_closed:        Event set when the WebSocket closes.
        tcp_registry:     Shared TCP connection registry mapping conn IDs to handlers.
        pending_connects: Shared pending-connect registry mapping conn IDs to futures.
        udp_registry:     Shared UDP flow registry mapping flow IDs to flows.
        post_ready_lines: Lines buffered during bootstrap for replay before live
                          WebSocket iteration begins.
        pre_ready_carry:  Partial frame fragment accumulated during bootstrap.
        ws_send:          Sender used to emit ``ERROR``/``CONN_CLOSE`` frames
                          on per-connection inbound saturation (head-of-line
                          guard).
        on_agent_stats:   Optional listener invoked for each decoded ``STATS``
                          snapshot.  Must not block or raise.
    """

    __slots__ = (
        "_ws",
        "_ws_closed",
        "_tcp_registry",
        "_pending_connects",
        "_udp_registry",
        "_post_ready_lines",
        "_pre_ready_carry",
        "_ws_send",
        "_on_agent_stats_cb",
        "_mark_agent_rx",
    )

    def __init__(
        self,
        ws: ClientConnection,
        ws_closed: asyncio.Event,
        tcp_registry: dict[str, TcpConnection],
        pending_connects: dict[str, PendingConnect],
        udp_registry: dict[str, UdpFlow],
        post_ready_lines: list[str],
        pre_ready_carry: str,
        ws_send: WsSendCallable,
        on_agent_stats: AgentStatsCallable | None = None,
        mark_agent_rx: MarkAgentRxCallable | None = None,
    ) -> None:
        self._ws = ws
        self._ws_closed = ws_closed
        self._tcp_registry = tcp_registry
        self._pending_connects = pending_connects
        self._udp_registry = udp_registry
        self._post_ready_lines = list(post_ready_lines)
        self._pre_ready_carry = pre_ready_carry
        self._ws_send = ws_send
        self._on_agent_stats_cb = on_agent_stats
        self._mark_agent_rx = mark_agent_rx

    # Local Utils
    async def _notify_agent_conn_closed(self, conn_id: str, reason: str) -> None:
        """Tell the agent to close a TCP connection.

        The Python agent does not consume inbound ERROR frames, so CONN_CLOSE is
        mandatory. ERROR is still sent first for future agents that understand it.
        """
        frames = (
            encode_error_frame(conn_id, reason),
            encode_conn_close_frame(conn_id),
        )

        for frame in frames:
            try:
                await self._ws_send(frame, control=True)
            except CtrlBackpressureError as exc:
                # This is per-call backpressure
                # the connection is already being torn down so we drop
                # the notification frame and let the existing teardown
                # path complete.  The peer will observe the close as a
                # WS-level connection drop or, more likely, the next
                # agent-initiated CONN_CLOSE flowing through.
                metrics_inc(
                    "session.notify_agent.dropped_backpressure",
                    cap=str(exc.details.get("control_queue_cap", "unknown")),
                )
                logger.debug(
                    "conn %s: dropping local-close notification frame: "
                    "control queue full (cap=%s)",
                    conn_id,
                    exc.details.get("control_queue_cap"),
                    extra={"conn_id": conn_id},
                )
                # No point trying the second frame if the first was
                # rejected — give up on the whole notification.
                return
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "conn %s: failed to emit local-close notification frame: %s",
                    conn_id,
                    exc,
                    extra={"conn_id": conn_id},
                )

    # ── Entry point ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Run the receive loop until the WebSocket closes.

        Replays buffered post-ready lines first, then takes over the live
        WebSocket iterator.

        Close sequencing: ``_ws_closed`` is set **before** closing handlers so
        any coroutine blocked on ``_ws_closed.wait()`` unblocks immediately and
        does not race with handler teardown.

        Raises:
            ConnectionClosedError: On a session-level ``ERROR`` frame or a
                                   corrupt/non-UTF-8 frame.
            UnexpectedFrameError:  On ``AGENT_READY``, ``KEEPALIVE``,
                                   ``CONN_OPEN``, or ``UDP_OPEN`` received from
                                   the agent post-bootstrap.
        """
        close_exc: ConnectionClosed | None = None

        async with aspan("session.recv_loop"):
            try:
                for line in self._post_ready_lines:
                    self._check_line_size(line)
                    await self._dispatch_frame(line)
                self._post_ready_lines.clear()

                buf = self._pre_ready_carry
                self._pre_ready_carry = ""

                async for msg in self._ws:
                    if isinstance(msg, str):
                        chunk = msg
                    else:
                        try:
                            chunk = msg.decode("utf-8")
                        except UnicodeDecodeError as exc:
                            metrics_inc("session.frames.decode_error")
                            raise ConnectionClosedError(
                                "Agent sent non-UTF-8 frame bytes.",
                                details={
                                    "close_code": WS_CLOSE_CODE_PROTOCOL_ERROR,
                                    "close_reason": "non_utf8_frame_bytes",
                                },
                            ) from exc

                    # Refresh the shared agent-activity timestamp on every
                    # inbound chunk so the dispatcher can distinguish tunnel
                    # congestion from a wedged agent before declaring the
                    # session unhealthy.
                    if self._mark_agent_rx is not None:
                        self._mark_agent_rx()

                    buf += chunk
                    if len(buf) > MAX_TUNNEL_FRAME_CHARS and "\n" not in buf:
                        metrics_inc("session.recv.oversized_unterminated_frame")
                        raise ConnectionClosedError(
                            "Agent sent an oversized unterminated tunnel frame.",
                            details={
                                "close_code": WS_CLOSE_CODE_PROTOCOL_ERROR,
                                "close_reason": "oversized_unterminated_frame",
                                "buffer_chars": len(buf),
                                "limit": MAX_TUNNEL_FRAME_CHARS,
                            },
                        )

                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        self._check_line_size(line)
                        await self._dispatch_frame(line)

                if buf.strip():
                    logger.debug(
                        "recv: WebSocket ended with %d chars of residual data dropped: %r",
                        len(buf),
                        buf[:120],
                    )
                    metrics_inc("session.recv.trailing_partial_frame")
                    metrics_observe("session.recv.residual_bytes", float(len(buf)))

            except ConnectionClosed as exc:
                close_exc = exc

            finally:
                self._ws_closed.set()
                self._force_cleanup()

        if close_exc is not None:
            raise ConnectionClosedError(
                "WebSocket closed while receiving agent frames.",
                details={
                    "close_code": getattr(getattr(close_exc, "rcvd", None), "code", 0)
                    or 0,
                    "close_reason": getattr(
                        getattr(close_exc, "rcvd", None), "reason", ""
                    )
                    or "",
                },
            ) from close_exc

        raise ConnectionClosedError(
            "WebSocket receive loop ended.",
            details={
                "close_code": 0,
                "close_reason": "recv_loop_ended",
            },
        )

    @staticmethod
    def _check_line_size(line: str) -> None:
        """Reject a single newline-terminated frame line that is too large."""
        if len(line) <= MAX_TUNNEL_FRAME_CHARS:
            return

        metrics_inc("session.recv.oversized_frame_line")
        raise ConnectionClosedError(
            "Agent sent an oversized tunnel frame line.",
            details={
                "close_code": WS_CLOSE_CODE_PROTOCOL_ERROR,
                "close_reason": "oversized_frame_line",
                "line_chars": len(line),
                "limit": MAX_TUNNEL_FRAME_CHARS,
            },
        )

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def _force_cleanup(self) -> None:
        """Abort all TCP handlers and signal close to all UDP flows on WebSocket disconnect."""
        tcp_count = 0
        for handler in list(self._tcp_registry.values()):
            handler.abort_upstream()
            handler.on_remote_closed()
            tcp_count += 1

        udp_count = 0
        for flow in list(self._udp_registry.values()):
            flow.on_remote_closed()
            udp_count += 1

        if tcp_count:
            metrics_inc("session.cleanup.tcp", value=tcp_count)
            metrics_inc("session.recv.cleanup.tcp", value=tcp_count)
        if udp_count:
            metrics_inc("session.cleanup.udp", value=udp_count)
            metrics_inc("session.recv.cleanup.udp", value=udp_count)

        if tcp_count or udp_count:
            logger.info(
                "recv: force-cleaned %d TCP handler(s) and %d UDP flow(s) on WebSocket close",
                tcp_count,
                udp_count,
            )

    # ── Frame dispatcher ──────────────────────────────────────────────────────

    async def _dispatch_frame(self, line: str) -> None:
        """Parse and dispatch one frame line.

        Two layers of :exc:`~exectunnel.exceptions.FrameDecodingError` handling:

        1. **Frame structure** — ``parse_frame`` rejects malformed frames.
           Caught here and wrapped in :exc:`~exectunnel.exceptions.ConnectionClosedError`.
        2. **Payload content** — decode helpers reject corrupt base64/UTF-8.
           Caught around the ``match`` block and wrapped identically.

        Both produce :exc:`~exectunnel.exceptions.ConnectionClosedError` because
        a corrupt frame from the agent signals a protocol violation the session
        cannot safely continue.

        Args:
            line: A single newline-delimited frame string.

        Raises:
            ConnectionClosedError: On corrupt frame structure or payload.
            UnexpectedFrameError:  On unexpected frame type.
        """
        extracted = _extract_frame_line(line)
        if extracted is None:
            logger.debug("recv: non-frame line ignored: %r", line[:80])
            metrics_inc("session.frames.noise")
            return

        try:
            frame = parse_frame(extracted)
        except FrameDecodingError as exc:
            metrics_inc("session.frames.decode_error")
            raise ConnectionClosedError(
                "Agent sent a corrupt tunnel frame.",
                details={
                    "close_code": WS_CLOSE_CODE_PROTOCOL_ERROR,
                    "close_reason": exc.message,
                },
            ) from exc

        if frame is None:
            logger.debug(
                "recv: parse_frame returned None for extracted frame: %r",
                extracted[:80],
            )
            metrics_inc("session.frames.noise")
            return

        msg_type = frame.msg_type
        conn_id = frame.conn_id
        metrics_inc("session.frames.inbound", msg_type=msg_type)

        # Must be checked before the conn_id guard — AGENT_READY and KEEPALIVE
        # have conn_id=None by design, so the guard below would silently drop
        # them instead of surfacing the protocol violation.
        if msg_type in UNEXPECTED_NO_CONN_ID_FRAMES:
            raise UnexpectedFrameError(
                f"{msg_type} received from agent after session was established.",
                details={"state": "running", "frame_type": msg_type},
            )

        # STATS is a session-scoped observability snapshot (no conn_id).
        if msg_type == "STATS":
            self._on_stats(frame.payload)
            return

        # LIVENESS is a zero-dispatch agent→client heartbeat The actual liveness
        # signal is the ``mark_agent_rx`` callback fired earlier in
        # :meth:`run` for every inbound chunk; this branch only records the
        # observability counter and returns.
        if msg_type == "LIVENESS":
            metrics_inc("session.frames.liveness")
            return

        if conn_id is None:
            logger.warning(
                "recv: frame %r arrived with no conn_id — dropping", msg_type
            )
            metrics_inc("session.frames.no_conn_id")
            return

        try:
            async with aspan(
                "session.handle_frame", msg_type=msg_type, conn_id=conn_id
            ):
                match msg_type:
                    case "CONN_ACK":
                        self._on_conn_ack(conn_id)

                    case "DATA":
                        await self._on_data(conn_id, frame.payload)

                    case "CONN_CLOSE":
                        self._on_conn_close(conn_id)

                    case "UDP_DATA":
                        self._on_udp_data(conn_id, frame.payload)

                    case "UDP_CLOSE":
                        self._on_udp_close(conn_id)

                    case "ERROR":
                        await self._on_error(conn_id, frame.payload)

                    case "CONN_OPEN" | "UDP_OPEN":
                        raise UnexpectedFrameError(
                            f"{msg_type} received from agent — unexpected direction.",
                            details={"state": "running", "frame_type": msg_type},
                        )

                    case _:
                        raise UnexpectedFrameError(
                            f"Unhandled frame type {msg_type!r}.",
                            details={"state": "running", "frame_type": msg_type},
                        )

        except FrameDecodingError as exc:
            metrics_inc("session.frames.payload_decode_error")
            raise ConnectionClosedError(
                f"Agent sent a frame with a corrupt payload "
                f"(msg_type={msg_type!r}, conn_id={conn_id!r}).",
                details={
                    "close_code": WS_CLOSE_CODE_PROTOCOL_ERROR,
                    "close_reason": exc.message,
                    "msg_type": msg_type,
                    "conn_id": conn_id,
                },
            ) from exc

    # ── Per-type callbacks ────────────────────────────────────────────────────

    def _on_conn_ack(self, conn_id: str) -> None:
        """Resolve the pending connect future for a ``CONN_ACK`` frame.

        Args:
            conn_id: Connection ID from the received frame.
        """
        pending = self._pending_connects.get(conn_id)
        if pending is not None and not pending.ack_future.done():
            pending.ack_future.set_result(AckStatus.OK)
        else:
            logger.debug(
                "recv: CONN_ACK for unknown or already-resolved conn_id %r — ignoring",
                conn_id,
            )
            metrics_inc("session.frames.orphaned", frame_type="CONN_ACK")

    async def _on_data(self, conn_id: str, payload: str) -> None:
        """Feed decoded data to the :class:`~exectunnel.transport.TcpConnection` handler.

        Args:
            conn_id: Connection ID from the received frame.
            payload: Base64-encoded data payload from the frame.
        """
        handler = self._tcp_registry.get(conn_id)
        if handler is None:
            metrics_inc("session.frames.orphaned", frame_type="DATA")
            return

        data = decode_binary_payload(payload)
        metrics_inc("session.frames.bytes.in", value=len(data))

        pending = self._pending_connects.get(conn_id)
        if pending is not None and not pending.ack_future.done():
            if handler.is_closed:
                return
            try:
                handler.feed(data)
            except PreAckBufferOverflowError:
                if not pending.ack_future.done():
                    pending.ack_future.set_result(AckStatus.PRE_ACK_OVERFLOW)
                metrics_inc("tunnel.pre_ack_buffer.overflow")
            except TransportError as exc:
                metrics_inc(
                    "tunnel.inbound_queue.drop",
                    error=exc.error_code.replace(".", "_"),
                )
                logger.debug(
                    "conn %s: pre-ACK feed() dropped chunk: %s",
                    conn_id,
                    exc,
                    extra={"conn_id": conn_id},
                )
            return

        # Non-blocking enqueue: a slow local consumer must not stall the
        # receive loop for other multiplexed connections.  On overflow we
        # tear down only this connection (ERROR + abort) and keep reading.
        try:
            accepted = handler.try_feed(data)
        except TransportError as exc:
            metrics_inc(
                "tunnel.inbound_queue.drop",
                error=exc.error_code.replace(".", "_"),
            )
            logger.debug(
                "conn %s: post-ACK try_feed() error: %s",
                conn_id,
                exc,
                extra={"conn_id": conn_id},
            )
            return
        if accepted:
            return
        # Queue full → signal the agent to close its side and locally
        # abort, so the slow consumer cannot stall the WS dispatcher.
        metrics_inc(
            "tunnel.inbound_queue.drop",
            error="transport_inbound_queue_full",
        )
        metrics_inc("session.recv.hol_abort")
        logger.warning(
            "conn %s: inbound queue full — aborting connection to avoid "
            "head-of-line blocking of other muxed connections",
            conn_id,
            extra={"conn_id": conn_id},
        )
        try:
            await self._notify_agent_conn_closed(
                conn_id, "client inbound saturated"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "conn %s: failed to emit saturation ERROR frame: %s",
                conn_id,
                exc,
                extra={"conn_id": conn_id},
            )
        handler.abort_upstream()
        handler.on_remote_closed()

    def _on_conn_close(self, conn_id: str) -> None:
        """React to a ``CONN_CLOSE`` frame from the agent.

        Args:
            conn_id: Connection ID from the received frame.
        """
        handler = self._tcp_registry.get(conn_id)
        if handler is None:
            metrics_inc("session.frames.orphaned", frame_type="CONN_CLOSE")
            return
        pending = self._pending_connects.get(conn_id)
        if pending is not None and not pending.ack_future.done():
            pending.ack_future.set_result(AckStatus.AGENT_CLOSED)
        handler.on_remote_closed()

    def _on_udp_data(self, flow_id: str, payload: str) -> None:
        """Feed a decoded datagram to the :class:`~exectunnel.transport.UdpFlow` handler.

        Args:
            flow_id: Flow ID from the received frame.
            payload: Base64-encoded datagram payload from the frame.
        """
        flow = self._udp_registry.get(flow_id)
        if flow is None:
            metrics_inc("session.frames.orphaned", frame_type="UDP_DATA")
            return
        data = decode_binary_payload(payload)
        metrics_inc("session.frames.bytes.in", value=len(data))
        flow.feed(data)

    def _on_stats(self, payload: str) -> None:
        """Decode a ``STATS`` payload (base64url-JSON) and invoke the listener.

        When no listener is registered, increments a metric counter so STATS
        traffic remains observable at near-zero cost.

        Args:
            payload: Base64url-encoded JSON snapshot string (no padding).
        """
        metrics_inc("session.frames.stats")
        if self._on_agent_stats_cb is None:
            return
        try:
            raw = decode_binary_payload(payload)
            snapshot = json.loads(raw.decode("utf-8"))
        except (FrameDecodingError, ValueError, UnicodeDecodeError) as exc:
            metrics_inc("session.frames.stats_decode_error")
            logger.debug("recv: failed to decode STATS payload: %s", exc)
            return
        if not isinstance(snapshot, dict):
            metrics_inc("session.frames.stats_decode_error")
            logger.debug(
                "recv: STATS payload decoded to %s, expected dict",
                type(snapshot).__name__,
            )
            return
        try:
            self._on_agent_stats_cb(snapshot)
        except Exception as exc:  # noqa: BLE001 — listener must never crash recv loop
            logger.warning("recv: on_agent_stats listener raised: %s", exc)

    def _on_udp_close(self, flow_id: str) -> None:
        """React to a ``UDP_CLOSE`` frame from the agent.

        Args:
            flow_id: Flow ID from the received frame.
        """
        flow = self._udp_registry.get(flow_id)
        if flow is not None:
            flow.on_remote_closed()
        else:
            metrics_inc("session.frames.orphaned", frame_type="UDP_CLOSE")

    async def _on_error(self, conn_id: str, payload: str) -> None:
        """React to an ``ERROR`` frame from the agent.

        Session-level errors (``SESSION_CONN_ID``) raise
        :exc:`~exectunnel.exceptions.ConnectionClosedError`.
        Connection-level errors abort the upstream and signal the handler.
        Flow-level errors signal the :class:`~exectunnel.transport.UdpFlow`.

        Args:
            conn_id: Connection or flow ID from the received frame.
            payload: Base64-encoded error message payload.

        Raises:
            ConnectionClosedError: On a session-level error frame.
        """
        message = decode_error_payload(payload)

        if conn_id == SESSION_CONN_ID:
            metrics_inc("session.frames.error.session")
            raise ConnectionClosedError(
                f"Agent session error: {message}",
                details={
                    "close_code": WS_CLOSE_CODE_INTERNAL_ERROR,
                    "close_reason": message,
                },
            )

        handler = self._tcp_registry.get(conn_id)
        if handler is not None:
            metrics_inc("session.frames.error.conn")
            logger.warning("conn %s agent error: %s", conn_id, message)
            pending = self._pending_connects.get(conn_id)
            if pending is not None and not pending.ack_future.done():
                pending.ack_future.set_result(AckStatus.AGENT_ERROR)
            handler.abort_upstream()
            handler.on_remote_closed()
            return

        flow = self._udp_registry.get(conn_id)
        if flow is not None:
            metrics_inc("session.frames.error.flow")
            logger.debug("flow %s agent error: %s", conn_id, message)
            flow.on_remote_closed()
            return

        logger.debug(
            "recv: ERROR for unknown conn_id %r — handler already cleaned up (message: %s)",
            conn_id,
            message[:120],
        )
        metrics_inc("session.frames.orphaned", frame_type="ERROR")

    def __repr__(self) -> str:
        return (
            f"<FrameReceiver tcp={len(self._tcp_registry)} "
            f"udp={len(self._udp_registry)} "
            f"pending={len(self._pending_connects)} "
            f"ws_closed={self._ws_closed.is_set()}>"
        )
