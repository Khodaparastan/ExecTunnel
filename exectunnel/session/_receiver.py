"""Inbound frame receiver and dispatcher for TunnelSession.

Frame dispatch table
--------------------
``CONN_ACK``   → resolve pending connect future
``DATA``       → feed bytes to TcpConnection via synchronous feed()
``CONN_CLOSE`` → signal handler remote-closed
``UDP_DATA``   → feed datagram to UdpFlow
``UDP_CLOSE``  → signal flow remote-closed
``ERROR``      → session-level or per-connection/flow error handling
"""

import asyncio
import logging

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from exectunnel.exceptions import (
    ConnectionClosedError,
    FrameDecodingError,
    UnexpectedFrameError,
)
from exectunnel.observability import (
    aspan,
    metrics_inc,
    metrics_observe,
)
from exectunnel.protocol import (
    SESSION_CONN_ID,
    decode_binary_payload,
    decode_error_payload,
    parse_frame,
)
from exectunnel.transport import TcpConnection, UdpFlow

from ._state import AckStatus, PendingConnect

logger = logging.getLogger(__name__)


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
        self._post_ready_lines = list(post_ready_lines)
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
            ConnectionClosedError: On session-level ERROR frame or corrupt
                                   frame.
            UnexpectedFrameError:  On AGENT_READY / CONN_OPEN / UDP_OPEN /
                                   KEEPALIVE from agent.
        """
        async with aspan("session.recv_loop"):
            for line in self._post_ready_lines:
                await self._dispatch_frame(line)
            self._post_ready_lines.clear()

            buf = self._pre_ready_carry
            self._pre_ready_carry = ""

            try:
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
                                    "close_code": 1002,
                                    "close_reason": "non_utf8_frame_bytes",
                                },
                            ) from exc
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        await self._dispatch_frame(line)
            except ConnectionClosed:
                pass
            finally:
                if buf.strip():
                    logger.debug(
                        "recv: WebSocket closed with %d chars of residual buffered data dropped: %r",
                        len(buf),
                        buf[:120],
                    )
                    metrics_inc("session.recv.trailing_partial_frame")
                    metrics_observe("session.recv.residual_bytes", float(len(buf)))

                self._ws_closed.set()
                self._force_cleanup()

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def _force_cleanup(self) -> None:
        """Abort all TCP handlers and close all UDP flows on WS disconnect.

        Emits cleanup metrics so operators can see the blast radius of an
        unclean disconnect.
        """
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

        Two layers of ``FrameDecodingError`` handling:

        1. **Frame structure** — ``parse_frame`` rejects malformed frames
           (bad conn_id, unknown msg_type).  Caught here and wrapped.
        2. **Payload content** — ``decode_binary_payload`` /
           ``decode_error_payload`` reject corrupt base64 / UTF-8.  Caught
           around the ``match`` block and wrapped identically.

        Both layers produce ``ConnectionClosedError`` because a corrupt frame
        from the agent indicates either a protocol violation or proxy
        corruption — the session cannot continue safely.

        Raises:
            ConnectionClosedError: On corrupt frame structure or payload.
            UnexpectedFrameError:  On unexpected frame type for current state.
        """
        try:
            frame = parse_frame(line)
        except FrameDecodingError as exc:
            metrics_inc("session.frames.decode_error")
            raise ConnectionClosedError(
                "Agent sent a corrupt tunnel frame.",
                details={
                    "close_code": 1002,
                    "close_reason": exc.message,
                },
            ) from exc

        if frame is None:
            logger.debug("recv: non-frame line ignored: %r", line[:80])
            metrics_inc("session.frames.noise")
            return

        msg_type = frame.msg_type
        conn_id = frame.conn_id
        metrics_inc("session.frames.inbound", msg_type=msg_type)

        try:
            if conn_id is None:
                logger.debug("recv: frame with no conn_id: %r", frame)
                metrics_inc("session.frames.no_conn_id")
                return

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

                    case "AGENT_READY":
                        raise UnexpectedFrameError(
                            "AGENT_READY received after session was established.",
                            details={"state": "running", "frame_type": "AGENT_READY"},
                        )

                    case "CONN_OPEN" | "UDP_OPEN" | "KEEPALIVE":
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
                f"Agent sent a frame with a corrupt payload (msg_type={msg_type!r}, conn_id={conn_id!r}).",
                details={
                    "close_code": 1002,
                    "close_reason": exc.message,
                    "msg_type": msg_type,
                    "conn_id": conn_id,
                },
            ) from exc

    # ── Per-type callbacks ────────────────────────────────────────────────────

    def _on_conn_ack(self, conn_id: str) -> None:
        """Resolve the pending connect future for a CONN_ACK frame.

        ``conn_id`` is already validated by ``parse_frame`` via ``ID_RE`` —
        no additional format check is needed here.
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
        handler = self._tcp_registry.get(conn_id)
        if handler is None:
            metrics_inc("session.frames.orphaned", frame_type="DATA")
            return

        data = decode_binary_payload(payload)
        metrics_inc("session.frames.bytes.in", value=len(data))

        pending = self._pending_connects.get(conn_id)
        if pending is not None and not pending.ack_future.done():
            # Pre-ACK: synchronous feed with overflow detection.
            if handler.is_closed:
                return
            try:
                handler.feed(data)
            except Exception as exc:
                if (
                    hasattr(exc, "error_code")
                    and getattr(exc, "error_code")
                    == "transport.pre_ack_buffer_overflow"
                ):
                    if not pending.ack_future.done():
                        pending.ack_future.set_result(AckStatus.PRE_ACK_OVERFLOW)
                    metrics_inc("tunnel.pre_ack_buffer.overflow")
                else:
                    metrics_inc(
                        "tunnel.inbound_queue.drop",
                        error=getattr(exc, "error_code", type(exc).__name__).replace(
                            ".", "_"
                        ),
                    )
                    logger.debug(
                        "conn %s: feed() dropped chunk: %s",
                        conn_id,
                        exc,
                        extra={"conn_id": conn_id},
                    )
            return

        try:
            await handler.feed_async(data)
        except ConnectionClosedError:
            metrics_inc("session.frames.orphaned", frame_type="DATA")
        except Exception as exc:
            metrics_inc(
                "tunnel.inbound_queue.drop",
                error=getattr(exc, "error_code", type(exc).__name__).replace(".", "_"),
            )
            logger.debug(
                "conn %s: post-ACK feed_async() dropped chunk: %s",
                conn_id,
                exc,
                extra={"conn_id": conn_id},
            )

    def _on_conn_close(self, conn_id: str) -> None:
        """React to CONN_CLOSE from agent."""
        handler = self._tcp_registry.get(conn_id)
        if handler is None:
            metrics_inc("session.frames.orphaned", frame_type="CONN_CLOSE")
            return
        pending = self._pending_connects.get(conn_id)
        if pending is not None and not pending.ack_future.done():
            pending.ack_future.set_result(AckStatus.AGENT_CLOSED)
        handler.on_remote_closed()

    def _on_udp_data(self, flow_id: str, payload: str) -> None:
        """Feed decoded datagram to the UdpFlow handler.

        ``decode_binary_payload`` raises ``FrameDecodingError`` on corrupt
        base64 — propagated to ``_dispatch_frame`` which wraps it in
        ``ConnectionClosedError``.
        """
        flow = self._udp_registry.get(flow_id)
        if flow is None:
            metrics_inc("session.frames.orphaned", frame_type="UDP_DATA")
            return
        data = decode_binary_payload(payload)
        metrics_inc("session.frames.bytes.in", value=len(data))
        flow.feed(data)

    def _on_udp_close(self, flow_id: str) -> None:
        """React to UDP_CLOSE from agent."""
        flow = self._udp_registry.get(flow_id)
        if flow is not None:
            flow.on_remote_closed()
        else:
            metrics_inc("session.frames.orphaned", frame_type="UDP_CLOSE")

    async def _on_error(self, conn_id: str, payload: str) -> None:
        """React to ERROR frame from agent.

        Session-level errors (SESSION_CONN_ID) raise ConnectionClosedError.
        Connection-level errors abort the upstream and signal the handler.
        Flow-level errors signal the UdpFlow.

        ``decode_error_payload`` raises ``FrameDecodingError`` on corrupt
        base64 / UTF-8 — propagated to ``_dispatch_frame`` which wraps it
        in ``ConnectionClosedError``.
        """
        message = decode_error_payload(payload)

        if conn_id == SESSION_CONN_ID:
            metrics_inc("session.frames.error.session")
            raise ConnectionClosedError(
                f"Agent session error: {message}",
                details={
                    "close_code": 1011,
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
            "recv: ERROR frame for unknown conn_id %r — handler already cleaned up (message: %s)",
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
