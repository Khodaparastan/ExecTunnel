"""Inbound frame receiver and dispatcher for TunnelSession.

Reads WebSocket messages, splits on newlines, parses frames via
``parse_frame``, and dispatches to the correct handler.

Frame dispatch table
--------------------
``CONN_ACK``   → resolve pending connect future (fast path before parse_frame)
``DATA``       → feed bytes to TcpConnection via feed_async (post-ACK backpressure)
``CONN_CLOSE`` → on_remote_closed() on TcpConnection
``UDP_DATA``   → feed datagram to UdpFlow
``UDP_CLOSE``  → on_remote_closed() on UdpFlow
``ERROR``      → session-level: raise ConnectionClosedError
               → connection-level: abort_upstream + on_remote_closed
               → flow-level: on_remote_closed
``AGENT_READY``→ unexpected post-bootstrap: raise UnexpectedFrameError
``CONN_OPEN``  → unexpected from agent: raise UnexpectedFrameError
``UDP_OPEN``   → unexpected from agent: raise UnexpectedFrameError
"""

import asyncio
import logging
import re

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from exectunnel.exceptions import (
    ConnectionClosedError,
    FrameDecodingError,
    TransportError,
    UnexpectedFrameError,
)
from exectunnel.observability import metrics_inc, span
from exectunnel.protocol import (
    FRAME_PREFIX,
    FRAME_SUFFIX,
    SESSION_CONN_ID,
    decode_binary_payload,
    decode_error_payload,
    parse_frame,
)
from exectunnel.transport import TcpConnection, UdpFlow

from ._state import AckStatus, PendingConnect


logger = logging.getLogger(__name__)

# CONN_ACK is a session-layer extension not registered in the protocol
# package's _VALID_MSG_TYPES.  It is intercepted before parse_frame to
# avoid adding it to the wire codec.
#
# Format: <<<EXECTUNNEL:CONN_ACK:{conn_id}>>>
# Constructed from imported protocol constants — stays in sync if they change.
_CONN_ACK_PREFIX: str = f"{FRAME_PREFIX}CONN_ACK:"
_CONN_ACK_SUFFIX: str = FRAME_SUFFIX

# Local regex for CONN_ACK conn_id validation.
# Mirrors the protocol package's ID format without importing the internal ID_RE.
# TCP conn IDs: c[0-9a-f]{24}
# UDP flow IDs: u[0-9a-f]{24}
_CONN_ID_RE: re.Pattern[str] = re.compile(r"^[cu][0-9a-f]{24}$")


class FrameReceiver:
    """Reads WebSocket messages and dispatches frames to registered handlers.

    Shared state (registries, pending connects) is owned by ``TunnelSession``
    and passed in by reference — ``FrameReceiver`` never copies them.

    Args:
        ws:               Live WebSocket connection.
        ws_closed:        Event set when the WebSocket closes.
        tcp_registry:     Shared TCP connection registry.
        pending_connects: Shared pending-connect registry.
        udp_registry:     Shared UDP flow registry.
        post_ready_lines: Lines buffered during bootstrap for replay.
        pre_ready_carry:  Partial frame fragment from bootstrap.
    """

    __slots__ = (
        "_ws",
        "_ws_closed",
        "_tcp_registry",
        "_pending_connects",
        "_udp_registry",
        "_post_ready_lines",
        "_pre_ready_carry",
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
    ) -> None:
        self._ws = ws
        self._ws_closed = ws_closed
        self._tcp_registry = tcp_registry
        self._pending_connects = pending_connects
        self._udp_registry = udp_registry
        self._post_ready_lines = post_ready_lines
        self._pre_ready_carry = pre_ready_carry

    # ── Entry point ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Run the receive loop until the WebSocket closes.

        Replays buffered post-ready lines first, then takes over the live
        WebSocket iterator.

        Close sequencing
        ----------------
        ``_ws_closed`` is set **before** closing handlers so that any
        coroutine blocked on ``_ws_closed.wait()`` (e.g. the ws_closed_task
        in ``_handle_tunnel_connect``) unblocks immediately and does not race
        with handler teardown.

        Raises:
            ConnectionClosedError: On session-level ERROR frame or corrupt frame.
            UnexpectedFrameError:  On AGENT_READY / CONN_OPEN / UDP_OPEN from agent.
        """
        # Replay frames buffered during bootstrap.
        for line in self._post_ready_lines:
            await self._dispatch_frame(line)
        self._post_ready_lines.clear()

        buf = self._pre_ready_carry
        self._pre_ready_carry = ""

        try:
            async for msg in self._ws:
                chunk = msg if isinstance(msg, str) else msg.decode()
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    await self._dispatch_frame(line)
        except ConnectionClosed:
            pass
        finally:
            # Set ws_closed FIRST — unblocks _await_conn_ack's ws_closed_task
            # before handler teardown begins.
            self._ws_closed.set()

            for handler in list(self._tcp_registry.values()):
                handler.abort_upstream()
                handler.on_remote_closed()
            for flow in list(self._udp_registry.values()):
                flow.on_remote_closed()

    # ── Frame dispatcher ──────────────────────────────────────────────────────

    async def _dispatch_frame(self, line: str) -> None:
        """Parse and dispatch one frame line.

        Raises:
            ConnectionClosedError: On session-level ERROR frame or corrupt frame.
            UnexpectedFrameError:  On unexpected frame type for current state.
            FrameDecodingError:    Propagated from parse_frame / payload helpers.
        """
        stripped = line.strip()

        # ── CONN_ACK fast path ────────────────────────────────────────────────
        # CONN_ACK is a session-layer extension not in the protocol package's
        # _VALID_MSG_TYPES.  Intercept before parse_frame.
        if stripped.startswith(_CONN_ACK_PREFIX) and stripped.endswith(_CONN_ACK_SUFFIX):
            self._on_conn_ack(stripped)
            return

        # ── Standard frame parsing ────────────────────────────────────────────
        try:
            frame = parse_frame(line)
        except FrameDecodingError as exc:
            metrics_inc("tunnel.frames.decode_error")
            raise ConnectionClosedError(
                "Agent sent a corrupt tunnel frame.",
                details={
                    "close_code": 1002,
                    "close_reason": exc.message,
                },
            ) from exc

        if frame is None:
            # Shell noise — not a tunnel frame.  DEBUG only, never WARNING.
            logger.debug("recv: non-frame line ignored: %r", line[:80])
            metrics_inc("tunnel.frames.noise")
            return

        msg_type = frame.msg_type
        conn_id = frame.conn_id
        metrics_inc("session_frames_inbound_total", msg_type=msg_type)

        with span("session.handle_frame", msg_type=msg_type, conn_id=conn_id):
            match msg_type:
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

                case "AGENT_READY":
                    raise UnexpectedFrameError(
                        "AGENT_READY received after session was established.",
                        details={
                            "state": "running",
                            "frame_type": "AGENT_READY",
                        },
                    )

                case "CONN_OPEN" | "UDP_OPEN":
                    # Agent never sends these — they are client→agent only.
                    raise UnexpectedFrameError(
                        f"{msg_type} received from agent — unexpected direction.",
                        details={
                            "state": "running",
                            "frame_type": msg_type,
                        },
                    )

                case _:
                    # parse_frame guarantees msg_type is valid — this branch
                    # can only be reached if a new frame type was added to the
                    # protocol package without updating this dispatcher.
                    raise UnexpectedFrameError(
                        f"Unhandled frame type {msg_type!r} — update session dispatcher.",
                        details={
                            "state": "running",
                            "frame_type": msg_type,
                        },
                    )

    # ── Per-type callbacks ────────────────────────────────────────────────────

    def _on_conn_ack(self, stripped: str) -> None:
        """Resolve the pending connect future for a CONN_ACK frame.

        Uses a local regex for conn_id validation — does not import the
        protocol package's internal ``ID_RE``.  An invalid conn_id is
        silently ignored (DEBUG log only) — it cannot match any registry
        entry so no state is corrupted.
        """
        # Extract conn_id from: <<<EXECTUNNEL:CONN_ACK:{conn_id}>>>
        inner = stripped[len(_CONN_ACK_PREFIX) : -len(_CONN_ACK_SUFFIX)]

        if not _CONN_ID_RE.match(inner):
            logger.debug(
                "recv: CONN_ACK with invalid conn_id format %r — ignoring",
                inner[:32],
            )
            metrics_inc("tunnel.frames.conn_ack.invalid_id")
            return

        pending = self._pending_connects.get(inner)
        if pending is not None and not pending.ack_future.done():
            pending.ack_future.set_result(AckStatus.OK)
            metrics_inc("session_frames_inbound_total", msg_type="CONN_ACK")
        else:
            logger.debug(
                "recv: CONN_ACK for unknown or already-resolved conn_id %r — ignoring",
                inner,
            )
            metrics_inc("tunnel.frames.conn_ack.stale")

    async def _on_data(self, conn_id: str, payload: str) -> None:
        """Feed decoded bytes to the TcpConnection handler.

        Pre-ACK: uses synchronous ``feed()`` with overflow detection.
        Post-ACK: uses ``feed_async()`` for backpressure.
        """
        handler = self._tcp_registry.get(conn_id)
        if handler is None:
            return

        # decode_binary_payload raises FrameDecodingError on bad base64 —
        # propagate to run() which wraps it in ConnectionClosedError.
        data = decode_binary_payload(payload)

        pending = self._pending_connects.get(conn_id)
        if pending is not None and not pending.ack_future.done():
            # Pre-ACK: use synchronous feed() with overflow detection.
            if handler.is_closed:
                return
            try:
                handler.feed(data)
            except TransportError as exc:
                if exc.error_code == "transport.pre_ack_buffer_overflow":
                    if not pending.ack_future.done():
                        pending.ack_future.set_result(AckStatus.PRE_ACK_OVERFLOW)
                    metrics_inc("tunnel.pre_ack_buffer.overflow")
                else:
                    metrics_inc(
                        "tunnel.inbound_queue.drop",
                        error=exc.error_code.replace(".", "_"),
                    )
                    logger.debug(
                        "conn %s: feed() dropped chunk [%s]: %s",
                        conn_id,
                        exc.error_code,
                        exc.message,
                        extra={"conn_id": conn_id, "error_code": exc.error_code},
                    )
        else:
            # Post-ACK: use async feed with backpressure.
            try:
                await handler.feed_async(data)
            except ConnectionClosedError:
                pass  # Normal teardown race — handler already closed.

    def _on_conn_close(self, conn_id: str) -> None:
        """React to CONN_CLOSE from agent."""
        handler = self._tcp_registry.get(conn_id)
        if handler is None:
            return
        pending = self._pending_connects.get(conn_id)
        if pending is not None and not pending.ack_future.done():
            pending.ack_future.set_result(AckStatus.AGENT_CLOSED)
        handler.on_remote_closed()

    def _on_udp_data(self, flow_id: str, payload: str) -> None:
        """Feed decoded datagram to the UdpFlow handler."""
        flow = self._udp_registry.get(flow_id)
        if flow is None:
            return
        # decode_binary_payload raises FrameDecodingError — propagate.
        data = decode_binary_payload(payload)
        flow.feed(data)

    def _on_udp_close(self, flow_id: str) -> None:
        """React to UDP_CLOSE from agent."""
        flow = self._udp_registry.get(flow_id)
        if flow is not None:
            flow.on_remote_closed()

    async def _on_error(self, conn_id: str, payload: str) -> None:
        """React to ERROR frame from agent.

        Session-level errors (SESSION_CONN_ID) raise ConnectionClosedError.
        Connection-level errors abort the upstream and signal the handler.
        Flow-level errors signal the UdpFlow.
        """
        # decode_error_payload raises FrameDecodingError — propagate.
        message = decode_error_payload(payload)

        if conn_id == SESSION_CONN_ID:
            raise ConnectionClosedError(
                f"Agent session error: {message}",
                details={
                    "close_code": 1011,
                    "close_reason": message,
                },
            )

        # Connection-level error.
        handler = self._tcp_registry.get(conn_id)
        if handler is not None:
            logger.warning(
                "conn %s agent error: %s",
                conn_id,
                message,
                extra={"conn_id": conn_id, "error_reason": message},
            )
            pending = self._pending_connects.get(conn_id)
            if pending is not None and not pending.ack_future.done():
                pending.ack_future.set_result(AckStatus.AGENT_ERROR)
            handler.abort_upstream()
            handler.on_remote_closed()
            return

        # Flow-level error — DEBUG only (expected race condition).
        flow = self._udp_registry.get(conn_id)
        if flow is not None:
            logger.debug(
                "flow %s agent error: %s",
                conn_id,
                message,
                extra={"flow_id": conn_id, "error_reason": message},
            )
            flow.on_remote_closed()
