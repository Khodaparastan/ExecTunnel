"""WebSocket send abstraction — concurrency-safe frame sender for TunnelSession.

Implements the ``WsSendCallable`` protocol expected by the transport layer.
All outgoing frames are serialised through a single asyncio task (_run)
that owns the WebSocket write path.

Priority model
--------------
Control frames (``control=True``) are enqueued into an unbounded queue and
are never dropped.  Data frames use a bounded queue; when full they are either
dropped silently or block the caller (``must_queue=True``).

The ``_run`` loop drains the control queue before the data queue on every
iteration, guaranteeing that ``CONN_CLOSE`` / ``UDP_CLOSE`` / ``UDP_OPEN``
frames are delivered ahead of bulk data.
"""

import asyncio
import contextlib
import logging

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from exectunnel.config.defaults import SEND_DROP_LOG_EVERY
from exectunnel.config.settings import AppConfig
from exectunnel.exceptions import (
    ConnectionClosedError,
    WebSocketSendTimeoutError,
)
from exectunnel.observability import metrics_inc
from exectunnel.protocol import FRAME_PREFIX, FRAME_SUFFIX

__all__: list[str] = []  # internal module

logger = logging.getLogger(__name__)


class _StopSentinel:
    """Singleton stop signal for the send-loop queues."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "<QUEUE_STOP>"


_QUEUE_STOP = _StopSentinel()


# Type alias for queue items: a frame string or the stop sentinel.
_SendQueueItem = str | _StopSentinel


class WsSender:
    """Concurrency-safe ``WsSendCallable`` implementation.

    Owns two asyncio queues and a background send-loop task.  Callers
    enqueue frames via :meth:`send`; the loop drains them in priority order
    and writes to the WebSocket.

    Lifecycle
    ---------
    1. Construct with ``WsSender(ws, app_cfg, ws_closed_event)``.
    2. Call ``start()`` to spawn the send loop task.
    3. Enqueue frames via ``await sender.send(frame, ...)``.
    4. Call ``stop()`` to drain and shut down.

    The ``ws_closed`` event is set by ``FrameReceiver`` when the WebSocket
    closes.  ``WsSender`` reads it to reject ``must_queue`` calls on a dead
    connection.

    The send loop sets ``ws_closed`` in its ``finally`` block **only** when
    it exits due to an unexpected error (``WebSocketSendTimeoutError`` or
    ``ConnectionClosedError``).  A clean ``stop()`` call does not set the
    event — ``FrameReceiver`` is the authoritative setter for normal teardown.
    """

    __slots__ = (
        "_ws",
        "_app",
        "_ws_closed",
        "_ctrl_queue",
        "_data_queue",
        "_loop_task",
        "_send_drop_count",
        "_started",
        "_stopped",
    )

    def __init__(
        self,
        ws: ClientConnection,
        app_cfg: AppConfig,
        ws_closed: asyncio.Event,
    ) -> None:
        self._ws = ws
        self._app = app_cfg
        self._ws_closed = ws_closed

        # Control queue is unbounded — teardown sentinels must never be dropped.
        self._ctrl_queue: asyncio.Queue[_SendQueueItem] = asyncio.Queue(maxsize=0)
        self._data_queue: asyncio.Queue[_SendQueueItem] = asyncio.Queue(
            maxsize=app_cfg.bridge.send_queue_cap
        )

        self._loop_task: asyncio.Task[None] | None = None
        self._send_drop_count: int = 0
        self._started: bool = False
        self._stopped: bool = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the background send loop task.  Call once per session."""
        if self._started:
            return
        self._started = True
        self._loop_task = asyncio.create_task(
            self._run(), name="tun-send-loop"
        )

    async def stop(self) -> None:
        """Signal the send loop to exit and await its completion.

        Inserts the stop sentinel into the control queue (always succeeds —
        unbounded) then cancels the task as a safety net in case the loop
        is blocked on a slow send.  Does NOT set ``ws_closed`` — that is
        ``FrameReceiver``'s responsibility.
        """
        if self._stopped:
            return
        self._stopped = True

        # Insert stop sentinel into ctrl queue — always succeeds (unbounded).
        self._ctrl_queue.put_nowait(_QUEUE_STOP)

        if self._loop_task is not None and not self._loop_task.done():
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._loop_task

    @property
    def task(self) -> asyncio.Task[None] | None:
        """The underlying send loop task, for inclusion in ``asyncio.wait``."""
        return self._loop_task

    # ── WsSendCallable implementation ─────────────────────────────────────────

    async def send(
        self,
        frame: str,
        *,
        must_queue: bool = False,
        control: bool = False,
    ) -> None:
        """Enqueue *frame* for delivery by the send loop.

        Parameters mirror the ``WsSendCallable`` protocol exactly.

        Args:
            frame:      Newline-terminated frame string from ``encode_*_frame()``.
            must_queue: Block until enqueued under backpressure (DATA frames).
            control:    Bypass flow-control ordering (CONN_CLOSE, UDP_* frames).
                        When ``True``, ``must_queue`` is ignored.

        Raises:
            ConnectionClosedError: When ``must_queue=True`` and the WebSocket
                is already closed — signals the caller that the frame was not
                delivered.
        """
        if control:
            # Unbounded queue — put_nowait never raises QueueFull.
            self._ctrl_queue.put_nowait(frame)
            return

        if must_queue:
            if self._ws_closed.is_set():
                raise ConnectionClosedError(
                    "WebSocket is closed — cannot enqueue data frame.",
                    details={
                        "close_code": 0,
                        "close_reason": "ws_closed_before_enqueue",
                    },
                )
            await self._data_queue.put(frame)
            return

        try:
            self._data_queue.put_nowait(frame)
        except asyncio.QueueFull:
            self._send_drop_count += 1
            metrics_inc("tunnel.frames.send_drop")
            if (
                self._send_drop_count == 1
                or self._send_drop_count % SEND_DROP_LOG_EVERY == 0
            ):
                logger.warning(
                    "send data queue full, dropping frame (drops=%d)",
                    self._send_drop_count,
                )

    # ── Send loop ─────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Single writer to the WebSocket — serialises all outgoing frames.

        Priority: control queue is drained before data queue on every
        iteration.  Exits on the ``_QUEUE_STOP`` sentinel or on WebSocket close.

        Sends frames as text (``str``) — the kubectl exec channel is text-mode.

        On unexpected exit (timeout or connection closed) the ``ws_closed``
        event is set so that ``_await_conn_ack``'s ``ws_closed_task`` unblocks
        promptly.  On clean ``stop()`` the event is NOT set here —
        ``FrameReceiver`` is the authoritative setter.

        Raises:
            WebSocketSendTimeoutError: Propagated to ``_run_tasks`` to trigger reconnect.
            ConnectionClosedError:     Same.
        """
        ws = self._ws
        ctrl_q = self._ctrl_queue
        data_q = self._data_queue
        send_timeout = self._app.bridge.send_timeout
        unexpected_exit = False

        try:
            while True:
                item = await self._dequeue_next_frame(ctrl_q, data_q)
                if isinstance(item, _StopSentinel):
                    return  # Clean exit.

                frame: str = item  # type: ignore[assignment]
                # Extract msg_type for outbound metric — format: <<<EXECTUNNEL:{TYPE}:...
                _parts = frame.split(":", 2)
                _msg_type = _parts[1] if len(_parts) >= 2 else "unknown"
                try:
                    metrics_inc("tunnel.frames.sent")
                    metrics_inc("session_frames_outbound_total", msg_type=_msg_type)
                    async with asyncio.timeout(send_timeout):
                        await ws.send(frame)
                except TimeoutError as exc:
                    unexpected_exit = True
                    metrics_inc("tunnel.frames.send_timeout")
                    raise WebSocketSendTimeoutError(
                        "WebSocket frame send timed out — connection stalled.",
                        details={
                            "timeout_s": send_timeout,
                            "payload_bytes": len(frame.encode()),
                        },
                        hint=(
                            "Increase EXECTUNNEL_SEND_TIMEOUT or check network "
                            "latency to the tunnel endpoint."
                        ),
                    ) from exc
                except ConnectionClosed as exc:
                    unexpected_exit = True
                    metrics_inc("tunnel.frames.send_closed")
                    raise ConnectionClosedError(
                        "WebSocket connection closed while sending frame.",
                        details={
                            "close_code": getattr(exc.rcvd, "code", 0) or 0,
                            "close_reason": getattr(exc.rcvd, "reason", "") or "",
                        },
                    ) from exc
        finally:
            # Only set ws_closed on unexpected exit — clean stop() does not
            # set it here; FrameReceiver.run() is the authoritative setter.
            if unexpected_exit:
                self._ws_closed.set()

    @staticmethod
    async def _dequeue_next_frame(
        ctrl_q: asyncio.Queue[_SendQueueItem],
        data_q: asyncio.Queue[_SendQueueItem],
    ) -> _SendQueueItem:
        """Return the next item to send, prioritising control over data.

        Drains the control queue completely before touching the data queue.
        When both queues are empty, blocks on whichever fires first, then
        re-checks the control queue before returning a data item.
        """
        # Fast path: drain control queue first.
        try:
            return ctrl_q.get_nowait()
        except asyncio.QueueEmpty:
            pass

        # Try data queue without blocking.
        try:
            data_item = data_q.get_nowait()
        except asyncio.QueueEmpty:
            data_item = None

        if data_item is not None:
            # One final ctrl check before committing to the data item.
            try:
                return ctrl_q.get_nowait()
            except asyncio.QueueEmpty:
                return data_item

        # Both queues empty — block on whichever fires first.
        ctrl_task: asyncio.Task[_SendQueueItem] = asyncio.create_task(ctrl_q.get())
        data_task: asyncio.Task[_SendQueueItem] = asyncio.create_task(data_q.get())

        try:
            done, _ = await asyncio.wait(
                {ctrl_task, data_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            ctrl_task.cancel()
            data_task.cancel()
            await asyncio.gather(ctrl_task, data_task, return_exceptions=True)
            raise

        # Cancel the loser; rescue its result if it completed concurrently.
        rescued_data: _SendQueueItem | None = None
        for pending_task in (t for t in {ctrl_task, data_task} if t not in done):
            pending_task.cancel()
            try:
                await pending_task
            except asyncio.CancelledError:
                # Cancelled before completing — peek the queue directly.
                if pending_task is data_task:
                    with contextlib.suppress(asyncio.QueueEmpty):
                        rescued_data = data_q.get_nowait()
            else:
                result = pending_task.result()
                if pending_task is data_task:
                    rescued_data = result
                else:
                    # Rescued ctrl item — re-queue at front (unbounded).
                    ctrl_q.put_nowait(result)

        if ctrl_task in done:
            winner = ctrl_task.result()
            # If data also completed, re-queue it for the next iteration.
            if rescued_data is not None:
                with contextlib.suppress(asyncio.QueueFull):
                    data_q.put_nowait(rescued_data)
            return winner

        # Data task won — do one final ctrl check.
        try:
            ctrl_item = ctrl_q.get_nowait()
            # Re-queue the data item for next iteration.
            if rescued_data is not None:
                with contextlib.suppress(asyncio.QueueFull):
                    data_q.put_nowait(rescued_data)
            return ctrl_item
        except asyncio.QueueEmpty:
            return data_task.result()


class KeepaliveLoop:
    """Sends a KEEPALIVE control frame at a fixed interval.

    The KEEPALIVE frame is not a registered protocol frame type — the agent
    discards it.  It exists solely to keep the WebSocket alive through
    NAT/proxy idle timeouts.

    Uses ``asyncio.timeout`` on ``ws_closed.wait()`` so the loop exits
    promptly when the WebSocket closes rather than sleeping for a full
    interval.
    """

    # Frame constructed from imported protocol constants so the format stays
    # in sync if FRAME_PREFIX / FRAME_SUFFIX ever change.
    # NOTE: KEEPALIVE is intentionally not a registered protocol frame type.
    # It is a session-layer heartbeat only.  The agent discards it.
    _KEEPALIVE_FRAME: str = f"{FRAME_PREFIX}KEEPALIVE{FRAME_SUFFIX}\n"

    __slots__ = ("_sender", "_ws_closed", "_interval")

    def __init__(
        self,
        sender: WsSender,
        ws_closed: asyncio.Event,
        interval: float,
    ) -> None:
        self._sender = sender
        self._ws_closed = ws_closed
        self._interval = interval

    async def run(self) -> None:
        """Run the keepalive loop until the WebSocket closes."""
        while True:
            try:
                async with asyncio.timeout(self._interval):
                    await self._ws_closed.wait()
                return  # WebSocket closed — exit cleanly.
            except TimeoutError:
                await self._sender.send(self._KEEPALIVE_FRAME, control=True)
