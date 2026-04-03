"""
TCP connection handler — bridges one local TCP stream to one agent-side TCP
connection via the WebSocket frame protocol.

Data flow
---------
* **upstream**:   local TCP → ``encode_data_frame`` → WebSocket
* **downstream**: WebSocket DATA frames (queued by recv_loop) → local TCP

Lifecycle
---------
1. Create handler, register it in the shared registry.
2. Call :meth:`~TcpConnection.start` once the agent ACKs the connection.
3. The agent signals close via ``CONN_CLOSE`` or ``ERROR`` →
   recv_loop calls :meth:`~TcpConnection.close_remote`, which sets
   ``_remote_closed`` and wakes ``_downstream`` via the close event.
4. ``_downstream`` drains all queued data, writes EOF, then exits.
5. Either direction finishing triggers :meth:`~TcpConnection._on_task_done`,
   which cancels the sibling task (unless half-close applies) and schedules
   :meth:`~TcpConnection._cleanup`.
6. :meth:`~TcpConnection._cleanup` awaits both tasks, evicts from registry,
   closes writer.

Half-close semantics
--------------------
When the local client sends EOF, ``_upstream`` finishes cleanly
(``_upstream_ended_cleanly = True``) and sends ``CONN_CLOSE`` to the agent.
``_on_task_done`` detects this and keeps ``_downstream`` alive so the remote
server's response can still be delivered. The peer task is only cancelled
when the finishing task ended with an error or cancellation.

Protocol alignment
------------------
All frame encoding uses the typed helpers from :mod:`exectunnel.protocol.frames`:

* :func:`~exectunnel.protocol.frames.encode_data_frame` — accepts raw
  ``bytes``; applies ``urlsafe_b64encode`` with no padding internally.
* :func:`~exectunnel.protocol.frames.encode_conn_close_frame` — emits the
  ``CONN_CLOSE`` control frame.

No manual base64 encoding is performed in this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Final

from exectunnel.config.defaults import PRE_ACK_BUFFER_CAP_BYTES, TCP_INBOUND_QUEUE_CAP
from exectunnel.exceptions import (
    ConnectionClosedError,
    ExecTunnelError,
    TransportError,
    WebSocketSendTimeoutError,
)
from exectunnel.observability import metrics_inc, metrics_observe, span
from exectunnel.protocol.frames import (
    PIPE_READ_CHUNK_BYTES,
    encode_conn_close_frame,
    encode_data_frame,
)
from exectunnel.transport._types import TcpRegistry, WsSendCallable
from exectunnel.transport._validation import require_bytes

__all__ = ["TcpConnection"]

logger = logging.getLogger(__name__)

# ── Module-level private constants ────────────────────────────────────────────

# Timeout applied to writer.wait_closed() during cleanup to prevent indefinite
# hangs when the OS never delivers the FIN ACK.
_WRITER_CLOSE_TIMEOUT_SECS: Final[float] = 5.0

# Maximum number of chunks to batch-write before calling drain().
# Batching amortises the per-drain syscall overhead on high-throughput streams.
_DOWNSTREAM_BATCH_SIZE: Final[int] = 16

# Minimum sensible pre-ACK buffer cap — one full read chunk.
_MIN_PRE_ACK_BUFFER_CAP: Final[int] = PIPE_READ_CHUNK_BYTES


# ── Exception handling helper ─────────────────────────────────────────────────


def _log_task_exception(
    conn_id: str,
    direction: str,
    exc: BaseException,
    bytes_transferred: int,
) -> None:
    """Log a task exception with structured context.

    Centralises the 6-case exception ladder that was previously duplicated
    verbatim between ``_upstream`` and ``_downstream``.  Each case maps to
    the same log level and metric key regardless of direction — only the
    ``direction`` label and ``bytes_transferred`` value differ.

    Cases handled:
    * :class:`~exectunnel.exceptions.WebSocketSendTimeoutError` — WARNING
    * :class:`~exectunnel.exceptions.ConnectionClosedError`     — WARNING
    * :class:`~exectunnel.exceptions.TransportError`            — WARNING
    * :class:`OSError`                                          — DEBUG
    * :class:`~exectunnel.exceptions.ExecTunnelError`           — WARNING
    * :class:`Exception`                                        — WARNING + DEBUG traceback

    Args:
        conn_id:           Connection ID for log context.
        direction:         ``"upstream"`` or ``"downstream"``.
        exc:               The caught exception.
        bytes_transferred: Bytes moved in this direction so far.
    """
    byte_key = "bytes_sent" if direction == "upstream" else "bytes_recv"
    base_extra: dict[str, object] = {
        "conn_id": conn_id,
        "direction": direction,
        byte_key: bytes_transferred,
    }

    match exc:
        case WebSocketSendTimeoutError():
            metrics_inc(f"tcp.connection.{direction}.error", error="ws_send_timeout")
            logger.warning(
                "conn %s: %s stalled — WebSocket send timed out "
                "[%s] (%s=%d, error_id=%s)",
                conn_id,
                direction,
                exc.error_code,
                byte_key,
                bytes_transferred,
                exc.error_id,
                extra={**base_extra, "error_code": exc.error_code, "error_id": exc.error_id},
            )

        case ConnectionClosedError():
            metrics_inc(f"tcp.connection.{direction}.error", error="connection_closed")
            logger.warning(
                "conn %s: %s ended — tunnel connection closed "
                "[%s] (%s=%d, error_id=%s)",
                conn_id,
                direction,
                exc.error_code,
                byte_key,
                bytes_transferred,
                exc.error_id,
                extra={**base_extra, "error_code": exc.error_code, "error_id": exc.error_id},
            )

        case TransportError():
            metrics_inc(
                f"tcp.connection.{direction}.error",
                error=exc.error_code.replace(".", "_"),
            )
            logger.warning(
                "conn %s: %s transport error [%s]: %s (%s=%d, error_id=%s)",
                conn_id,
                direction,
                exc.error_code,
                exc.message,
                byte_key,
                bytes_transferred,
                exc.error_id,
                extra={**base_extra, "error_code": exc.error_code, "error_id": exc.error_id},
            )

        case OSError():
            metrics_inc(f"tcp.connection.{direction}.error", error="os_error")
            logger.debug(
                "conn %s: %s socket error: %s",
                conn_id,
                direction,
                exc,
                exc_info=True,
                extra=base_extra,
            )

        case ExecTunnelError():
            metrics_inc(
                f"tcp.connection.{direction}.error",
                error=exc.error_code.replace(".", "_"),
            )
            logger.warning(
                "conn %s: %s library error [%s]: %s (error_id=%s)",
                conn_id,
                direction,
                exc.error_code,
                exc.message,
                exc.error_id,
                extra={**base_extra, "error_code": exc.error_code, "error_id": exc.error_id},
            )

        case _:
            metrics_inc(
                f"tcp.connection.{direction}.error",
                error=type(exc).__name__,
            )
            logger.warning(
                "conn %s: %s unexpected failure: %s",
                conn_id,
                direction,
                exc,
                extra=base_extra,
            )
            logger.debug(
                "conn %s: %s traceback",
                conn_id,
                direction,
                exc_info=True,
                extra={"conn_id": conn_id},
            )


# ── Handler ───────────────────────────────────────────────────────────────────


class TcpConnection:
    """Bridges one local TCP connection to one agent-side TCP connection.

    Args:
        conn_id:                  Stable identifier for this connection.
        reader:                   asyncio stream reader for the local TCP client.
        writer:                   asyncio stream writer for the local TCP client.
        ws_send:                  Coroutine callable that sends a frame string
                                  over the WebSocket / exec channel. Must
                                  conform to :class:`~exectunnel.transport._types.WsSendCallable`.
        registry:                 Shared mapping of ``conn_id → handler``; the
                                  handler removes itself on cleanup.
        pre_ack_buffer_cap_bytes: Maximum bytes to buffer before the agent ACKs
                                  the connection. Clamped to a minimum of
                                  :data:`~exectunnel.protocol.frames.PIPE_READ_CHUNK_BYTES`.
                                  Defaults to
                                  :data:`~exectunnel.config.defaults.PRE_ACK_BUFFER_CAP_BYTES`.

    Lifecycle flags
    ---------------
    Two kinds of state are tracked:

    * Plain ``bool`` flags (``_started``, ``_conn_close_sent``,
      ``_upstream_ended_cleanly``, ``_downstream_ended_cleanly``) — these are
      only ever read/written from within a single task or from sync call sites
      that do not overlap with the tasks, so no ``Event`` is needed.

    * ``asyncio.Event`` objects (``_closed``, ``_remote_closed``) — used
      wherever one coroutine needs to *await* a state change driven by another
      coroutine or a sync callback.  ``_closed`` additionally doubles as the
      authoritative cleanup gate exposed via :attr:`closed_event`.
    """

    def __init__(
        self,
        conn_id: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        ws_send: WsSendCallable,
        registry: TcpRegistry,
        *,
        pre_ack_buffer_cap_bytes: int = PRE_ACK_BUFFER_CAP_BYTES,
    ) -> None:
        self._id = conn_id
        self._reader = reader
        self._writer = writer
        self._ws_send = ws_send
        self._registry = registry

        # Inbound queue: agent → local TCP.
        self._inbound: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=TCP_INBOUND_QUEUE_CAP
        )

        # Named task references — single source of truth.
        self._upstream_task: asyncio.Task[None] | None = None
        self._downstream_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None

        # asyncio.Event lifecycle gates:
        # _closed:        set by _cleanup(); awaitable via closed_event property.
        # _remote_closed: set by close_remote(); wakes _downstream drain loop.
        self._closed: asyncio.Event = asyncio.Event()
        self._remote_closed: asyncio.Event = asyncio.Event()

        # Plain bool lifecycle flags (single-task or non-overlapping access).
        self._started: bool = False
        self._conn_close_sent: bool = False
        self._upstream_ended_cleanly: bool = False
        self._downstream_ended_cleanly: bool = False

        # Telemetry.
        self._drop_count: int = 0
        self._bytes_upstream: int = 0
        self._bytes_downstream: int = 0

        # Pre-ACK buffer: holds data that arrives before start() is called.
        # Clamped to a meaningful minimum so a cap of 0 or 1 is never used.
        self._pre_ack_buffer_cap_bytes: int = max(
            _MIN_PRE_ACK_BUFFER_CAP, pre_ack_buffer_cap_bytes
        )
        self._pre_ack_buffer: list[bytes] = []
        self._pre_ack_buffer_bytes: int = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the upstream and downstream copy tasks.

        Idempotent — subsequent calls are logged and ignored.

        If :meth:`close_remote` was called before :meth:`start` (e.g. the
        agent rejected the connection immediately), ``_remote_closed`` is
        already set and ``_downstream`` will drain any buffered data then exit
        cleanly on its first iteration.

        Pre-ACK buffer flushing
        -----------------------
        Buffered chunks are enqueued into ``_inbound`` before the tasks start
        so ``_downstream`` sees them immediately. The pre-ACK buffer cap is
        always smaller than the inbound queue cap, so ``QueueFull`` cannot
        occur here — the guard is retained as a defensive invariant check
        rather than expected-path logic.

        Done-callback registration
        --------------------------
        Callbacks are registered on the local task variables *before* being
        assigned to ``self._upstream_task`` / ``self._downstream_task`` to
        eliminate the window where a callback could fire before the instance
        attributes are set.
        """
        if self._started:
            logger.debug(
                "conn %s: start() called more than once; ignoring",
                self._id,
                extra={"conn_id": self._id},
            )
            return
        self._started = True

        for chunk in self._pre_ack_buffer:
            try:
                self._inbound.put_nowait(chunk)
            except asyncio.QueueFull:
                # Defensive: pre-ACK cap < queue cap so this should never fire.
                self._drop_count += 1
                metrics_inc("tcp.connection.pre_ack_buffer.overflow")
                logger.warning(
                    "conn %s: pre-ACK queue full during flush, "
                    "dropping %d bytes (total_drops=%d)",
                    self._id,
                    len(chunk),
                    self._drop_count,
                    extra={"conn_id": self._id},
                )
        self._pre_ack_buffer.clear()
        self._pre_ack_buffer_bytes = 0

        upstream_task: asyncio.Task[None] = asyncio.create_task(
            self._upstream(), name=f"tcp-up-{self._id}"
        )
        downstream_task: asyncio.Task[None] = asyncio.create_task(
            self._downstream(), name=f"tcp-down-{self._id}"
        )

        # Register callbacks before storing to instance — eliminates the
        # window where _on_task_done fires before self._*_task is assigned.
        upstream_task.add_done_callback(self._on_task_done)
        downstream_task.add_done_callback(self._on_task_done)

        self._upstream_task = upstream_task
        self._downstream_task = downstream_task

    # ── Called by recv_loop ───────────────────────────────────────────────────

    def feed(self, data: bytes) -> None:
        """Enqueue *data* received from the agent for the downstream task.

        Use :meth:`feed_async` post-ACK so the recv_loop applies proper
        backpressure. This synchronous variant is for pre-ACK buffering and
        callers that cannot await.

        Pre-ACK overflow is treated as a fatal connection error — the
        connection is closed immediately rather than silently dropping data,
        because a pre-ACK overflow means the agent is sending data faster
        than the local client can consume it before even acknowledging the
        connection, which is a protocol violation.

        Args:
            data: Raw bytes decoded from a ``DATA`` frame payload.

        Raises:
            TransportError:     If the pre-ACK buffer is full.
            TransportError:     If the post-ACK inbound queue is full.
            TransportError:     If *data* is not a ``bytes`` instance.
        """
        require_bytes(data, self._id, "feed")

        if self._closed.is_set():
            return

        if not self._started:
            pending = self._pre_ack_buffer_bytes + len(data)
            if pending > self._pre_ack_buffer_cap_bytes:
                metrics_inc("tcp.connection.pre_ack_buffer.overflow")
                logger.warning(
                    "conn %s: pre-ACK buffer full — closing connection "
                    "(cap=%d, attempted=%d)",
                    self._id,
                    self._pre_ack_buffer_cap_bytes,
                    pending,
                    extra={"conn_id": self._id},
                )
                # Schedule cleanup — connection is unrecoverable at this point.
                # _cleanup guards against double-execution via _closed event.
                if self._cleanup_task is None:
                    self._cleanup_task = asyncio.create_task(
                        self._cleanup(),
                        name=f"tcp-cleanup-{self._id}",
                    )
                    self._cleanup_task.add_done_callback(self._on_cleanup_done)
                raise TransportError(
                    f"conn {self._id!r}: pre-ACK buffer full; connection closed.",
                    error_code="transport.pre_ack_buffer_overflow",
                    details={
                        "conn_id": self._id,
                        "cap_bytes": self._pre_ack_buffer_cap_bytes,
                        "attempted_bytes": pending,
                    },
                    hint=(
                        "The agent sent data before the connection was ACKed "
                        "and exceeded the pre-ACK buffer cap. "
                        "Increase PRE_ACK_BUFFER_CAP_BYTES or investigate "
                        "agent-side flow control."
                    ),
                )
            self._pre_ack_buffer.append(data)
            self._pre_ack_buffer_bytes = pending
            return

        # Post-ACK non-blocking path.
        try:
            self._inbound.put_nowait(data)
        except asyncio.QueueFull:
            metrics_inc("tcp.connection.inbound_queue.drop")
            self._drop_count += 1
            raise TransportError(
                f"conn {self._id!r}: inbound queue full; datagram dropped.",
                error_code="transport.inbound_queue_full",
                details={
                    "conn_id": self._id,
                    "queue_cap": TCP_INBOUND_QUEUE_CAP,
                    "drop_count": self._drop_count,
                },
                hint=(
                    "The downstream task is not draining fast enough. "
                    "Consider increasing TCP_INBOUND_QUEUE_CAP or "
                    "investigating local socket write latency."
                ),
            )

    async def feed_async(self, data: bytes) -> None:
        """Await space in the inbound queue, applying backpressure to the WS reader.

        Raises :class:`~exectunnel.exceptions.ConnectionClosedError` if the
        connection was closed before or during the enqueue, forcing the caller
        to handle the closed case explicitly rather than silently discarding
        data.

        Ordering guarantee
        ------------------
        The enqueued item is intentionally not rolled back when ``_closed`` is
        set after the ``put()`` completes. Rolling back had a TOCTOU race: if
        ``_downstream`` had already exited its drain loop by the time the
        rollback ran, the item was removed from the queue but never written to
        the local socket, silently truncating the stream. Instead we leave the
        item in the queue and let ``_downstream`` drain it before honouring
        the close flag — the drain-then-close ordering in ``_downstream``
        guarantees every byte the agent sent is delivered before EOF is written.

        Args:
            data: Raw bytes decoded from a ``DATA`` frame payload.

        Raises:
            TransportError:         If *data* is not a ``bytes`` instance.
            ConnectionClosedError:  If the connection is closed before or
                                    during enqueue.
            asyncio.CancelledError: Propagated as-is — never suppressed.
        """
        require_bytes(data, self._id, "feed_async")

        if self._closed.is_set():
            raise ConnectionClosedError(
                f"conn {self._id!r}: feed_async() called on a closed connection.",
                error_code="transport.feed_async_on_closed",
                details={"conn_id": self._id},
                hint="Check is_closed before calling feed_async().",
            )

        # Queue.put() only raises CancelledError — no broad except needed.
        await self._inbound.put(data)

        if self._closed.is_set():
            raise ConnectionClosedError(
                f"conn {self._id!r}: connection closed while enqueuing data.",
                error_code="transport.feed_async_closed_during_enqueue",
                details={"conn_id": self._id},
                hint=(
                    "The connection was closed concurrently with feed_async(). "
                    "The enqueued item will be drained by _downstream before EOF."
                ),
            )

    def close_remote(self) -> None:
        """Signal that the agent has closed its side of the connection.

        Sets ``_remote_closed`` and wakes ``_downstream`` so it can drain
        remaining queued data and then write EOF to the local socket.

        Safe to call before :meth:`start` — ``_downstream`` checks the event
        on its first iteration and exits cleanly if no data is queued.
        Idempotent — subsequent calls are no-ops.
        """
        if not self._closed.is_set():
            self._remote_closed.set()

    def abort(self) -> None:
        """Hard-cancel both copy tasks immediately.

        Used when the agent signals an unrecoverable ``ERROR`` and both
        directions must be torn down without waiting for graceful drain.

        For half-close teardown (agent closed only one direction), use
        :meth:`close_remote` to stop the downstream path while keeping
        upstream alive, or call :meth:`abort` to stop everything.

        Idempotent — cancelling an already-done task is a no-op.
        """
        for task in (self._upstream_task, self._downstream_task):
            if task is not None and not task.done():
                task.cancel()

    def abort_upstream(self) -> None:
        """Cancel the upstream task only.
        Used when the agent signals ERROR — stops sending DATA frames to a
        connection the agent has already torn down, while keeping downstream
        alive to drain any queued response data to the local client."""
        if self._upstream_task is not None and not self._upstream_task.done():
            self._upstream_task.cancel()

    def abort_downstream(self) -> None:
        """Cancel the downstream task only.
        Used for hard teardown when the local socket is known to be dead
        and draining remaining queued data would be pointless."""
        if self._downstream_task is not None and not self._downstream_task.done():
            self._downstream_task.cancel()
    # ── Copy tasks ────────────────────────────────────────────────────────────

    async def _upstream(self) -> None:
        """local TCP → WebSocket DATA frames.

        Reads chunks from the local TCP stream and encodes them as ``DATA``
        frames using :func:`~exectunnel.protocol.frames.encode_data_frame`,
        which applies ``urlsafe_b64encode`` with no padding internally.

        Byte accounting is performed **after** a successful send so that
        ``bytes_upstream`` reflects bytes that were actually delivered to the
        tunnel, not bytes that were read but may have been lost on send failure.

        ``CONN_CLOSE`` is sent in the ``finally`` block only when the upstream
        task was not cancelled — a cancelled upstream means the connection is
        already being torn down from another path and the close frame will be
        (or has already been) sent by that path.
        """
        with span("tcp.connection.upstream"):
            start = asyncio.get_running_loop().time()
            metrics_inc("tcp.connection.upstream.started")
            _cancelled = False
            try:
                while True:
                    chunk = await self._reader.read(PIPE_READ_CHUNK_BYTES)
                    if not chunk:
                        # Local client sent EOF.
                        break

                    frame = encode_data_frame(self._id, chunk)
                    await self._ws_send(frame, must_queue=True)

                    # Account bytes only after successful send.
                    self._bytes_upstream += len(chunk)

                self._upstream_ended_cleanly = True

            except asyncio.CancelledError:
                _cancelled = True
                metrics_inc("tcp.connection.upstream.cancelled")
                raise

            except Exception as exc:
                _log_task_exception(self._id, "upstream", exc, self._bytes_upstream)

            finally:
                elapsed = asyncio.get_running_loop().time() - start
                metrics_observe("tcp.connection.upstream.duration_sec", elapsed)
                metrics_observe(
                    "tcp.connection.upstream.bytes", float(self._bytes_upstream)
                )
                # Send CONN_CLOSE only when not cancelled — a cancelled upstream
                # means teardown is already in progress from another path.
                if not _cancelled:
                    await self._send_close_frame_once()

    async def _downstream(self) -> None:
        """Inbound queue → local TCP.

        Drains the inbound queue in batches of up to :data:`_DOWNSTREAM_BATCH_SIZE`
        chunks per ``drain()`` call to amortise syscall overhead on
        high-throughput streams.

        Close sequencing
        ----------------
        When ``_remote_closed`` is set, the loop continues draining until the
        queue is empty, then writes EOF and exits. This guarantees every byte
        the agent sent is delivered before the local socket is half-closed.

        Task reuse strategy
        -------------------
        The ``close_task`` (waiting on ``_remote_closed``) is created **once**
        outside the blocking-wait branch and reused across iterations. This
        avoids creating a new ``asyncio.Event.wait()`` coroutine on every
        blocking iteration, which would generate significant task churn on
        high-throughput connections.

        CancelledError handling
        -----------------------
        When the outer task is cancelled while blocked in ``asyncio.wait``,
        both inner tasks are cancelled and awaited before re-raising so no
        tasks leak.
        """
        with span("tcp.connection.downstream"):
            start = asyncio.get_running_loop().time()
            metrics_inc("tcp.connection.downstream.started")

            # Created once and reused across all blocking waits to avoid
            # spawning a new Event.wait() coroutine per iteration.
            close_task: asyncio.Task[None] = asyncio.create_task(
                self._remote_closed.wait(),
                name=f"tcp-down-close-{self._id}",
            )

            try:
                while True:
                    # ── Batch-drain available items ───────────────────────────
                    batch: list[bytes] = []
                    while len(batch) < _DOWNSTREAM_BATCH_SIZE:
                        try:
                            batch.append(self._inbound.get_nowait())
                        except asyncio.QueueEmpty:
                            break

                    if batch:
                        for chunk in batch:
                            self._writer.write(chunk)
                        try:
                            await self._writer.drain()
                            for chunk in batch:
                                self._bytes_downstream += len(chunk)
                        except OSError as exc:
                            metrics_inc(
                                "tcp.connection.downstream.error", error="os_drain"
                            )
                            logger.debug(
                                "conn %s: downstream drain error: %s",
                                self._id,
                                exc,
                                exc_info=True,
                                extra={"conn_id": self._id},
                            )
                        continue

                    # ── Queue empty — check close flag ────────────────────────
                    if self._remote_closed.is_set():
                        if self._writer.can_write_eof():
                            try:
                                self._writer.write_eof()
                                await self._writer.drain()
                            except OSError as exc:
                                logger.debug(
                                    "conn %s: downstream write_eof error: %s",
                                    self._id,
                                    exc,
                                    exc_info=True,
                                    extra={"conn_id": self._id},
                                )
                        self._downstream_ended_cleanly = True
                        break

                    # ── Block until data arrives OR remote closes ─────────────
                    # close_task is reused; get_task is fresh each iteration
                    # since Queue.get() is consumed on completion.
                    get_task: asyncio.Task[bytes] = asyncio.create_task(
                        self._inbound.get(),
                        name=f"tcp-down-get-{self._id}",
                    )

                    try:
                        done, pending = await asyncio.wait(
                            {get_task, close_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    except asyncio.CancelledError:
                        get_task.cancel()
                        close_task.cancel()
                        await asyncio.gather(
                            get_task, close_task, return_exceptions=True
                        )
                        raise

                    # Cancel the get_task loser only — close_task is reused
                    # and must not be cancelled here.
                    if get_task in pending:
                        get_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await get_task

                    if get_task in done and not get_task.cancelled():
                        chunk = get_task.result()
                        self._writer.write(chunk)
                        try:
                            await self._writer.drain()
                            self._bytes_downstream += len(chunk)
                        except OSError as exc:
                            metrics_inc(
                                "tcp.connection.downstream.error", error="os_drain"
                            )
                            logger.debug(
                                "conn %s: downstream drain error: %s",
                                self._id,
                                exc,
                                exc_info=True,
                                extra={"conn_id": self._id},
                            )
                    # If close_task fired (or both fired), loop back to the
                    # batch-drain path which will empty the queue before
                    # honouring the close flag.

            except asyncio.CancelledError:
                metrics_inc("tcp.connection.downstream.cancelled")
                raise

            except Exception as exc:
                _log_task_exception(
                    self._id, "downstream", exc, self._bytes_downstream
                )

            finally:
                # Always cancel the reused close_task on exit so it does not
                # leak if _downstream exits via an exception path.
                if not close_task.done():
                    close_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await close_task

                elapsed = asyncio.get_running_loop().time() - start
                metrics_observe("tcp.connection.downstream.duration_sec", elapsed)
                metrics_observe(
                    "tcp.connection.downstream.bytes", float(self._bytes_downstream)
                )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _send_close_frame_once(self) -> None:
        """Send ``CONN_CLOSE`` exactly once using the typed protocol helper.

        Uses :func:`~exectunnel.protocol.frames.encode_conn_close_frame` so
        the frame is encoded consistently with the rest of the protocol layer.

        All exceptions are suppressed — teardown must always complete.
        """
        if self._conn_close_sent:
            return
        self._conn_close_sent = True

        frame = encode_conn_close_frame(self._id)
        try:
            await self._ws_send(frame, control=True)
        except WebSocketSendTimeoutError as exc:
            metrics_inc("tcp.connection.conn_close.error", error="ws_send_timeout")
            logger.warning(
                "conn %s: CONN_CLOSE send timed out — agent will time-out "
                "the connection independently (error_id=%s)",
                self._id,
                exc.error_id,
                extra={
                    "conn_id": self._id,
                    "error_code": exc.error_code,
                    "error_id": exc.error_id,
                },
            )
        except ConnectionClosedError as exc:
            logger.debug(
                "conn %s: CONN_CLOSE skipped — connection already closed (error_id=%s)",
                self._id,
                exc.error_id,
                extra={"conn_id": self._id, "error_id": exc.error_id},
            )
        except Exception as exc:
            logger.debug(
                "conn %s: failed to send CONN_CLOSE: %s",
                self._id,
                exc,
                exc_info=True,
                extra={"conn_id": self._id},
            )

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        """Called when either copy task exits; cancel the sibling if needed.

        This is a sync done-callback scheduled by asyncio — it cannot be a
        coroutine. Async work (cleanup) is dispatched via
        ``asyncio.create_task`` from within this callback, which is the
        correct pattern for bridging sync callbacks into the async world.

        Half-close logic
        ----------------
        When ``_upstream`` exits **cleanly** (local client sent EOF, no error,
        not cancelled), ``_downstream`` is kept alive so the remote server's
        response can still be delivered to the local client.

        When ``_downstream`` exits (cleanly or otherwise), ``_upstream`` is
        always cancelled — there is no more data to receive so the
        local→agent direction can be torn down.

        When ``_upstream`` exits with an error or is cancelled, ``_downstream``
        is cancelled immediately.

        Cleanup scheduling
        ------------------
        ``_cleanup_task is None`` prevents double-*scheduling* (both callbacks
        firing before the first cleanup task runs).
        ``_closed.is_set()`` inside ``_cleanup`` prevents double-*execution*
        (e.g. if ``_cleanup`` is called directly from an external path).
        Both guards are necessary and complementary.
        """
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                if isinstance(exc, ExecTunnelError):
                    logger.debug(
                        "conn %s task %s ended with library error [%s] (error_id=%s): %s",
                        self._id,
                        task.get_name(),
                        exc.error_code,
                        exc.error_id,
                        exc,
                    )
                else:
                    logger.debug(
                        "conn %s task %s ended with error: %s",
                        self._id,
                        task.get_name(),
                        exc,
                    )

        # A task ended cleanly iff it was not cancelled and raised no exception.
        task_ended_cleanly = not task.cancelled() and task.exception() is None

        should_cancel_peer: bool
        if task is self._upstream_task:
            # Keep downstream alive only when upstream ended cleanly
            # (local client sent EOF — server may still reply).
            should_cancel_peer = not (
                task_ended_cleanly and self._upstream_ended_cleanly
            )
        else:
            # Downstream finished (cleanly or otherwise) → always cancel upstream.
            # There is no more inbound data to relay.
            should_cancel_peer = True

        if should_cancel_peer:
            for candidate in (self._upstream_task, self._downstream_task):
                if (
                    candidate is not None
                    and candidate is not task
                    and not candidate.done()
                ):
                    candidate.cancel()

        # Schedule cleanup only when both tasks are finished, cleanup has not
        # already been scheduled, and the closed gate has not been set.
        both_done = (
            self._upstream_task is None or self._upstream_task.done()
        ) and (self._downstream_task is None or self._downstream_task.done())
        if both_done and not self._closed.is_set() and self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(
                self._cleanup(), name=f"tcp-cleanup-{self._id}"
            )
            self._cleanup_task.add_done_callback(self._on_cleanup_done)

    @staticmethod
    def _on_cleanup_done(task: asyncio.Task[None]) -> None:
        """Log unexpected exceptions from the cleanup task."""
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.warning(
                    "conn cleanup task %s raised an unexpected exception: %s",
                    task.get_name(),
                    exc,
                    exc_info=True,
                )

    async def _cleanup(self) -> None:
        """Await all tasks, remove from registry, close the local writer.

        ``_closed`` is set at entry as the authoritative gate — concurrent
        calls (e.g. from ``_on_task_done`` and a direct external call) are
        no-ops after the first execution. The ``_cleanup_task is None`` guard
        in ``_on_task_done`` prevents double-*scheduling*; this guard prevents
        double-*execution*.
        """
        if self._closed.is_set():
            return
        self._closed.set()
        metrics_inc("tcp.connection.cleanup")

        for task in (self._upstream_task, self._downstream_task):
            if task is None:
                continue
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        self._registry.pop(self._id, None)

        # Close the writer with a timeout so a stalled OS never blocks cleanup.
        with contextlib.suppress(OSError, RuntimeError):
            self._writer.close()
        with contextlib.suppress(OSError, RuntimeError, asyncio.TimeoutError):
            await asyncio.wait_for(
                self._writer.wait_closed(),
                timeout=_WRITER_CLOSE_TIMEOUT_SECS,
            )

        logger.debug(
            "conn %s cleaned up (bytes_up=%d, bytes_down=%d, drops=%d)",
            self._id,
            self._bytes_upstream,
            self._bytes_downstream,
            self._drop_count,
            extra={"conn_id": self._id},
        )

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def conn_id(self) -> str:
        """The stable identifier for this TCP connection."""
        return self._id

    @property
    def is_started(self) -> bool:
        """``True`` once :meth:`start` has been called successfully."""
        return self._started

    @property
    def is_closed(self) -> bool:
        """``True`` once :meth:`_cleanup` has completed."""
        return self._closed.is_set()

    @property
    def is_remote_closed(self) -> bool:
        """``True`` once :meth:`close_remote` has been called."""
        return self._remote_closed.is_set()

    @property
    def closed_event(self) -> asyncio.Event:
        """Read-only view of the closed event for external waiters.

        Prefer :attr:`is_closed` for simple boolean checks. Use this only
        when you need to ``await`` the event directly.

        Warning:
            Do not call ``.set()`` on the returned event directly — always
            go through the lifecycle methods (:meth:`abort`, :meth:`close_remote`)
            to ensure cleanup runs correctly.
        """
        return self._closed

    @property
    def bytes_upstream(self) -> int:
        """Total raw bytes successfully flushed from local TCP to the tunnel."""
        return self._bytes_upstream

    @property
    def bytes_downstream(self) -> int:
        """Total raw bytes successfully flushed from the tunnel to local TCP."""
        return self._bytes_downstream

    @property
    def drop_count(self) -> int:
        """Total inbound chunks dropped due to queue saturation or buffer overflow."""
        return self._drop_count
