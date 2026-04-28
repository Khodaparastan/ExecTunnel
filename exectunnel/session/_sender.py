"""WebSocket send abstraction — concurrency-safe frame sender for TunnelSession.

Implements the ``WsSendCallable`` protocol expected by the transport layer.
All outgoing frames are serialised through a single asyncio task (:meth:`WsSender._run`)
that owns the WebSocket write path.

Priority model
--------------
Control frames (``control=True``) are enqueued into a bounded priority queue.
They are never silently dropped: if the control queue overflows, the session is
declared unhealthy and callers receive ``ConnectionClosedError``. Data frames
use a bounded queue; when full they are either dropped silently or block the
caller (``must_queue=True``).

The :meth:`WsSender._run` loop drains the control queue before the data queue
on every iteration, guaranteeing that ``CONN_CLOSE`` / ``UDP_CLOSE`` /
``UDP_OPEN`` frames are delivered ahead of bulk data.

Dequeue strategy
----------------
The send loop uses an :class:`asyncio.Event` (``_frame_ready``) instead of
racing two :class:`asyncio.Task` objects.  :meth:`WsSender.send` and
:meth:`WsSender.stop` set the event after enqueuing; the loop clears it and
double-checks both queues before waiting (standard condition-variable pattern).
This eliminates per-iteration task creation, cancellation, and fragile
item-rescue logic.
"""

import asyncio
import contextlib
import logging
import re
from typing import Final

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from exectunnel.defaults import Defaults
from exectunnel.exceptions import (
    ConnectionClosedError,
    WebSocketSendTimeoutError,
)
from exectunnel.observability import aspan, metrics_gauge_set, metrics_inc
from exectunnel.protocol import FRAME_PREFIX, FRAME_SUFFIX, encode_keepalive_frame

from ._config import SessionConfig
from ._constants import STOP_GRACE_TIMEOUT_SECS

logger = logging.getLogger(__name__)

_FRAME_PREFIX_LEN: Final[int] = len(FRAME_PREFIX)
_MSG_TYPE_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Z_]+$")


class _StopSentinel:
    """Singleton stop signal inserted into the send-loop queues on shutdown."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<QUEUE_STOP>"


_QUEUE_STOP = _StopSentinel()
"""Singleton instance used to signal the send loop to exit."""

type _SendQueueItem = str | _StopSentinel


def _extract_msg_type(frame: str) -> str:
    """Extract the ``msg_type`` token from an encoded frame string for metrics.

    Args:
        frame: A newline-terminated encoded frame string.

    Returns:
        The ``msg_type`` token extracted from the frame prefix, or ``"unknown"``
        if the frame format is unexpected or the token contains invalid characters.
    """
    if not frame.startswith(FRAME_PREFIX):
        return "unknown"
    rest = frame[_FRAME_PREFIX_LEN:]
    colon_pos = rest.find(":")
    suffix_pos = rest.find(FRAME_SUFFIX)
    if colon_pos == -1 and suffix_pos == -1:
        return "unknown"
    if colon_pos == -1:
        end = suffix_pos
    elif suffix_pos == -1:
        end = colon_pos
    else:
        end = min(colon_pos, suffix_pos)
    token = rest[:end] if end > 0 else "unknown"
    return token if _MSG_TYPE_RE.match(token) else "unknown"


class WsSender:
    """Concurrency-safe ``WsSendCallable`` implementation.

    Owns two asyncio queues, a notification event, and a background send-loop
    task.  Callers enqueue frames via :meth:`send`; the loop drains them in
    priority order and writes to the WebSocket.

    Lifecycle:
        1. Construct with ``WsSender(ws, session_cfg, ws_closed_event)``.
        2. Call :meth:`start` to spawn the send loop task.
        3. Enqueue frames via ``await sender.send(frame, ...)``.
        4. Call :meth:`stop` to drain and shut down.

    The ``ws_closed`` event is set by
    :class:`~exectunnel.session._receiver.FrameReceiver` when the WebSocket
    closes.  :class:`WsSender` reads it to reject ``must_queue`` calls on a
    dead connection.

    The send loop sets ``ws_closed`` in its ``finally`` block **only** when it
    exits due to an unexpected error.  A clean :meth:`stop` call does not set
    the event — :class:`~exectunnel.session._receiver.FrameReceiver` is the
    authoritative setter for normal teardown.

    Args:
        ws:          Live WebSocket connection.
        session_cfg: Session-level configuration.
        ws_closed:   Shared closed event owned by ``FrameReceiver``.
    """

    __slots__ = (
        "_ws",
        "_cfg",
        "_ws_closed",
        "_ctrl_queue",
        "_data_queue",
        "_frame_ready",
        "_loop_task",
        "_send_drop_count",
        "_started",
        "_stopped",
    )

    def __init__(
        self,
        ws: ClientConnection,
        session_cfg: SessionConfig,
        ws_closed: asyncio.Event,
    ) -> None:
        self._ws = ws
        self._cfg = session_cfg
        self._ws_closed = ws_closed

        self._ctrl_queue: asyncio.Queue[_SendQueueItem] = asyncio.Queue(
            maxsize=session_cfg.control_queue_cap
        )
        self._data_queue: asyncio.Queue[_SendQueueItem] = asyncio.Queue(
            maxsize=session_cfg.send_queue_cap,
        )

        self._frame_ready = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._send_drop_count: int = 0
        self._started: bool = False
        self._stopped: bool = False

    # -- Helper -------------------------------

    def _closed_enqueue_error(self, reason: str) -> ConnectionClosedError:
        return ConnectionClosedError(
            "WebSocket sender is closed — cannot enqueue frame.",
            details={
                "close_code": 0,
                "close_reason": reason,
            },
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the background send loop task.

        Idempotent — subsequent calls are no-ops.
        """
        if self._started:
            return
        self._started = True
        self._loop_task = asyncio.create_task(self._run(), name="tun-send-loop")

    async def stop(self) -> None:
        """Signal the send loop to exit and await its completion.

        Inserts the stop sentinel into the control queue (always succeeds —
        unbounded) and sets the notification event so the loop wakes
        immediately.  Waits for graceful drain/exit first, then force-cancels
        after :data:`~exectunnel.session._constants.STOP_GRACE_TIMEOUT_SECS`.

        Does **not** set ``ws_closed`` — that is
        :class:`~exectunnel.session._receiver.FrameReceiver`'s responsibility.

        Idempotent — subsequent calls are no-ops.
        """
        if self._stopped:
            return
        self._stopped = True
        with contextlib.suppress(asyncio.QueueFull):
            self._ctrl_queue.put_nowait(_QUEUE_STOP)
        self._frame_ready.set()

        if self._loop_task is not None and not self._loop_task.done():
            try:
                async with asyncio.timeout(STOP_GRACE_TIMEOUT_SECS):
                    await self._loop_task
            except (TimeoutError, asyncio.CancelledError):
                self._loop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._loop_task

    @property
    def task(self) -> asyncio.Task[None] | None:
        """The underlying send loop task, for inclusion in ``asyncio.wait``."""
        return self._loop_task

    # ── Queue depth gauges ────────────────────────────────────────────────────

    def _emit_queue_gauges(self) -> None:
        """Publish current queue depths as gauge metrics."""
        metrics_gauge_set("session.send.queue.data", float(self._data_queue.qsize()))
        metrics_gauge_set("session.send.queue.ctrl", float(self._ctrl_queue.qsize()))

    # ── WsSendCallable implementation ─────────────────────────────────────────

    async def send(
        self,
        frame: str,
        *,
        must_queue: bool = False,
        control: bool = False,
    ) -> None:
        """Enqueue *frame* for delivery by the send loop.

        Args:
            frame:      Newline-terminated frame string from an ``encode_*_frame()``
                        helper.
            must_queue: Block until enqueued under backpressure (DATA frames).
                        Ignored when *control* is ``True``.
            control:    Bypass flow-control ordering (``CONN_OPEN``, ``CONN_CLOSE``,
                        ``UDP_*`` frames).  When ``True``, the frame is placed in
                        the unbounded control queue and *must_queue* is ignored.

        Raises:
            ConnectionClosedError: When ``must_queue=True`` and the WebSocket
                                   is already closed or closes while waiting to
                                   enqueue.
        """
        if self._stopped:
            metrics_inc("session.send.after_stop_drop")
            if must_queue:
                raise self._closed_enqueue_error("sender_stopped")
            return

        if self._ws_closed.is_set():
            metrics_inc("session.send.after_ws_closed_drop")
            if must_queue:
                raise self._closed_enqueue_error("ws_closed_before_enqueue")
            return

        if control:
            try:
                self._ctrl_queue.put_nowait(frame)
            except asyncio.QueueFull as exc:
                metrics_inc("session.send.ctrl_queue_full")
                self._ws_closed.set()
                raise ConnectionClosedError(
                    "WebSocket control queue is full — declaring session unhealthy.",
                    details={
                        "close_code": 0,
                        "close_reason": "control_queue_full",
                        "control_queue_cap": self._cfg.control_queue_cap,
                    },
                    hint=(
                        "The WebSocket is not draining control frames fast enough. "
                        "Reconnect the tunnel and inspect proxy/WebSocket latency."
                    ),
                ) from exc

            self._frame_ready.set()
            self._emit_queue_gauges()
            return

        if must_queue:
            if self._ws_closed.is_set():
                metrics_inc("session.send.queue.race_closed")
                raise ConnectionClosedError(
                    "WebSocket is closed — cannot enqueue data frame.",
                    details={
                        "close_code": 0,
                        "close_reason": "ws_closed_before_enqueue",
                    },
                )

            # Fast path — avoid creating auxiliary tasks when the data queue
            # has headroom.  Under high data rates this path is hit on nearly
            # every call and measurably reduces per-chunk overhead.
            try:
                self._data_queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass
            else:
                self._frame_ready.set()
                self._emit_queue_gauges()
                return

            # Slow path — the queue is full.  Race put() against ws_closed
            # so a dying WebSocket does not wedge the caller indefinitely.
            ws_wait = asyncio.create_task(self._ws_closed.wait(), name="ws-closed-wait")
            put_task = asyncio.create_task(
                self._data_queue.put(frame), name="data-queue-put"
            )
            try:
                done, _ = await asyncio.wait(
                    {ws_wait, put_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                put_task.cancel()
                ws_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await put_task
                with contextlib.suppress(asyncio.CancelledError):
                    await ws_wait
                raise

            if put_task in done:
                ws_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ws_wait
                self._frame_ready.set()
                self._emit_queue_gauges()
                return

            put_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await put_task
            ws_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ws_wait
            metrics_inc("session.send.queue.race_closed")
            raise ConnectionClosedError(
                "WebSocket closed while waiting to enqueue data frame.",
                details={
                    "close_code": 0,
                    "close_reason": "ws_closed_during_enqueue",
                },
            )

        try:
            self._data_queue.put_nowait(frame)
            self._frame_ready.set()
            self._emit_queue_gauges()
        except asyncio.QueueFull:
            self._send_drop_count += 1
            metrics_inc("session.frames.outbound.drop")
            if (
                self._send_drop_count == 1
                or self._send_drop_count % Defaults.SEND_DROP_LOG_EVERY == 0
            ):
                logger.warning(
                    "send data queue full, dropping frame (drops=%d)",
                    self._send_drop_count,
                )

    # ── Send loop ─────────────────────────────────────────────────────────────

    def _try_dequeue(self) -> _SendQueueItem | None:
        """Try to dequeue one item, preferring the control queue.

        Returns:
            The next item from the control queue if non-empty, then from the
            data queue, or ``None`` if both queues are empty.
        """
        try:
            return self._ctrl_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            return self._data_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def _run(self) -> None:
        """Single writer to the WebSocket — serialises all outgoing frames.

        Priority: control queue drained before data queue on every iteration.
        Exits on the :data:`_QUEUE_STOP` sentinel or on WebSocket close.

        Uses an :class:`asyncio.Event` for idle-wait (standard
        condition-variable double-check pattern) rather than racing two tasks,
        eliminating per-iteration task overhead.

        On unexpected exit (timeout or connection closed) ``ws_closed`` is set
        so that ``_await_conn_ack``'s ``ws_closed_task`` unblocks promptly.
        On clean :meth:`stop`, the event is **not** set here —
        :class:`~exectunnel.session._receiver.FrameReceiver` is the
        authoritative setter.

        Raises:
            WebSocketSendTimeoutError: Propagated to ``_run_tasks`` to trigger
                                       reconnect.
            ConnectionClosedError:     Propagated to ``_run_tasks`` to trigger
                                       reconnect.
        """
        ws = self._ws
        send_timeout = self._cfg.send_timeout
        unexpected_exit = False

        try:
            async with aspan("session.send_loop"):
                while True:
                    item = self._try_dequeue()
                    if item is None:
                        self._frame_ready.clear()
                        item = self._try_dequeue()
                        if item is None:
                            if self._stopped:
                                return
                            await self._frame_ready.wait()
                            continue

                    if isinstance(item, _StopSentinel):
                        return

                    frame: str = item
                    msg_type = _extract_msg_type(frame)
                    frame_bytes = len(frame.encode())

                    try:
                        async with asyncio.timeout(send_timeout):
                            await ws.send(frame)

                        metrics_inc("session.frames.outbound", msg_type=msg_type)
                        metrics_inc("session.frames.outbound.bytes", value=frame_bytes)
                        self._emit_queue_gauges()

                    except TimeoutError as exc:
                        unexpected_exit = True
                        metrics_inc("session.frames.outbound.timeout")
                        raise WebSocketSendTimeoutError(
                            "WebSocket frame send timed out — connection stalled.",
                            details={
                                "timeout_s": send_timeout,
                                "payload_bytes": frame_bytes,
                                "msg_type": msg_type,
                            },
                            hint=(
                                "Increase EXECTUNNEL_SEND_TIMEOUT or check "
                                "network latency to the tunnel endpoint."
                            ),
                        ) from exc

                    except ConnectionClosed as exc:
                        # If the sender has already been asked to stop, or
                        # the receiver has already declared the WebSocket
                        # closed, this is teardown noise — the recv task
                        # is the authoritative reporter for the close
                        # cause.  Exit cleanly instead of raising and
                        # producing duplicate primary-failure logs.
                        if self._stopped or self._ws_closed.is_set():
                            metrics_inc(
                                "session.frames.outbound.ws_closed_during_shutdown"
                            )
                            return

                        unexpected_exit = True
                        metrics_inc("session.frames.outbound.ws_closed")
                        raise ConnectionClosedError(
                            "WebSocket connection closed while sending frame.",
                            details={
                                "close_code": (getattr(exc.rcvd, "code", 0) or 0),
                                "close_reason": (getattr(exc.rcvd, "reason", "") or ""),
                                "msg_type": msg_type,
                            },
                        ) from exc

        finally:
            if unexpected_exit:
                metrics_inc("session.send.unexpected_exit")
                self._ws_closed.set()
            metrics_gauge_set("session.send.queue.data", 0.0)
            metrics_gauge_set("session.send.queue.ctrl", 0.0)


class KeepaliveLoop:
    """Sends a KEEPALIVE control frame at a fixed interval.

    The KEEPALIVE frame is discarded by the agent.  It exists solely to keep
    the WebSocket alive through NAT/proxy idle timeouts.

    Uses :func:`asyncio.timeout` on ``ws_closed.wait()`` so the loop exits
    promptly when the WebSocket closes rather than sleeping a full interval.

    Args:
        sender:    The concurrency-safe frame sender.
        ws_closed: Shared closed event owned by ``FrameReceiver``.
        interval:  Seconds between KEEPALIVE frames.
    """

    _KEEPALIVE_FRAME: Final[str] = encode_keepalive_frame()

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
        async with aspan("session.keepalive_loop"):
            while True:
                try:
                    async with asyncio.timeout(self._interval):
                        await self._ws_closed.wait()
                    return
                except TimeoutError:
                    metrics_inc("session.keepalive.sent")
                    await self._sender.send(self._KEEPALIVE_FRAME, control=True)
