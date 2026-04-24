"""TCP connection handler.

Bridges one local TCP stream to one agent-side TCP connection via the
WebSocket frame protocol.

Data flow:
    * **upstream**:   local TCP → ``encode_data_frame`` → WebSocket.
    * **downstream**: WebSocket ``DATA`` frames → local TCP.

Lifecycle:
    1. Create handler and register it in the shared registry.
    2. Call :meth:`TcpConnection.start` once the agent ACKs the connection.
    3. The agent signals close via ``CONN_CLOSE`` or ``ERROR`` → session
       layer calls :meth:`TcpConnection.on_remote_closed`, which wakes
       the downstream task.
    4. The downstream task drains all queued data, writes EOF, then exits.
    5. Either direction finishing triggers
       :meth:`TcpConnection._on_task_done`, which cancels the sibling
       task (unless half-close applies) and schedules
       :meth:`TcpConnection._cleanup`.
    6. :meth:`TcpConnection._cleanup` awaits both tasks, sends
       ``CONN_CLOSE`` if not yet sent, evicts from the registry, and
       closes the local writer.
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
from exectunnel.observability import (
    aspan,
    metrics_gauge_dec,
    metrics_inc,
    metrics_observe,
)
from exectunnel.protocol import encode_conn_close_frame, encode_data_frame

from ._constants import (
    DOWNSTREAM_BATCH_SIZE,
    MIN_PRE_ACK_BUFFER_CAP,
    WRITER_CLOSE_TIMEOUT_SECS,
)
from ._errors import log_task_exception
from ._types import TcpRegistry, WsSendCallable
from ._validation import require_bytes
from ._waiting import wait_first

__all__ = ["TcpConnection"]

_log: Final[logging.Logger] = logging.getLogger(__name__)


class TcpConnection:
    """Bridges one local TCP connection to one agent-side TCP connection.

    Args:
        conn_id: Stable identifier for this connection (from
            :func:`~exectunnel.protocol.new_conn_id`).
        reader: :mod:`asyncio` stream reader for the local TCP client.
        writer: :mod:`asyncio` stream writer for the local TCP client.
        ws_send: Coroutine callable conforming to
            :class:`~exectunnel.transport._types.WsSendCallable`.
        registry: Shared ``conn_id → TcpConnection`` mapping; the
            handler removes itself on cleanup.
        pre_ack_buffer_cap_bytes: Maximum bytes to buffer before the
            agent ACKs the connection. Clamped to a minimum of
            :data:`~exectunnel.transport._constants.MIN_PRE_ACK_BUFFER_CAP`.

    Note:
        Satisfies :class:`~exectunnel.transport._types.TransportHandler`.
    """

    __slots__ = (
        "_id",
        "_reader",
        "_writer",
        "_ws_send",
        "_registry",
        "_inbound",
        "_upstream_task",
        "_downstream_task",
        "_cleanup_task",
        "_closed",
        "_remote_closed",
        "_started",
        "_conn_close_sent",
        "_upstream_ended_cleanly",
        "_downstream_ended_cleanly",
        "_drop_count",
        "_bytes_upstream",
        "_bytes_downstream",
        "_pre_ack_buffer_cap_bytes",
        "_pre_ack_buffer",
        "_pre_ack_buffer_bytes",
    )

    def __init__(
        self,
        conn_id: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        ws_send: WsSendCallable,
        registry: TcpRegistry,
        *,
        pre_ack_buffer_cap_bytes: int = Defaults.PRE_ACK_BUFFER_CAP_BYTES,
    ) -> None:
        self._id = conn_id
        self._reader = reader
        self._writer = writer
        self._ws_send = ws_send
        self._registry = registry

        self._inbound: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=Defaults.TCP_INBOUND_QUEUE_CAP
        )

        self._upstream_task: asyncio.Task[None] | None = None
        self._downstream_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None

        self._closed = asyncio.Event()
        self._remote_closed = asyncio.Event()

        self._started = False
        self._conn_close_sent = False
        self._upstream_ended_cleanly = False
        self._downstream_ended_cleanly = False

        self._drop_count = 0
        self._bytes_upstream = 0
        self._bytes_downstream = 0

        self._pre_ack_buffer_cap_bytes = max(
            MIN_PRE_ACK_BUFFER_CAP,
            pre_ack_buffer_cap_bytes,
        )
        self._pre_ack_buffer: list[bytes] = []
        self._pre_ack_buffer_bytes = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Flush the pre-ACK buffer and spawn the upstream and downstream tasks.

        Idempotent — subsequent calls are logged and ignored.

        Raises:
            TransportError: If the connection is already closed
                (``error_code`` ``"transport.start_on_closed"``).
        """
        if self._started:
            _log.debug(
                "conn %s: start() called more than once; ignoring",
                self._id,
                extra={"conn_id": self._id},
            )
            return

        if self._closed.is_set():
            raise TransportError(
                f"conn {self._id!r}: start() called on a closed connection.",
                error_code="transport.start_on_closed",
                details={"conn_id": self._id},
                hint="Do not call start() after cleanup.",
            )

        self._started = True

        for chunk in self._pre_ack_buffer:
            try:
                self._inbound.put_nowait(chunk)
            except asyncio.QueueFull:
                self._drop_count += 1
                metrics_inc("tcp.connection.pre_ack_buffer.overflow")
                _log.warning(
                    "conn %s: pre-ACK queue full during flush, dropping %d bytes "
                    "(total_drops=%d)",
                    self._id,
                    len(chunk),
                    self._drop_count,
                    extra={"conn_id": self._id},
                )
        self._pre_ack_buffer.clear()
        self._pre_ack_buffer_bytes = 0

        self._upstream_task = asyncio.create_task(
            self._upstream(), name=f"tcp-up-{self._id}"
        )
        self._downstream_task = asyncio.create_task(
            self._downstream(), name=f"tcp-down-{self._id}"
        )
        self._upstream_task.add_done_callback(self._on_task_done)
        self._downstream_task.add_done_callback(self._on_task_done)

    async def close_unstarted(self) -> None:
        """Close the writer directly when :meth:`start` was never called.

        Safe to call multiple times — :meth:`_cleanup` is idempotent.

        Raises:
            RuntimeError: If called after :meth:`start`. Use
                :meth:`abort` instead.
        """
        if self._started:
            raise RuntimeError(
                f"conn {self._id!r}: close_unstarted() called after start(). "
                "Use abort() instead."
            )
        await self._cleanup()

    def on_remote_closed(self) -> None:
        """Signal that the agent has closed its side of the connection.

        Sets ``_remote_closed`` so the downstream task drains remaining
        queued data and writes EOF to the local socket. Safe to call
        before :meth:`start`. Idempotent.

        Note:
            Satisfies :class:`~exectunnel.transport._types.TransportHandler`.
        """
        if not self._closed.is_set():
            self._remote_closed.set()
            metrics_inc("tcp.connection.closed_remote")
            _log.debug(
                "conn %s: remote closed signalled",
                self._id,
                extra={"conn_id": self._id},
            )

    def abort(self) -> None:
        """Hard-cancel both directions immediately.

        Idempotent — cancelling an already-done task is a no-op.
        """
        for task in (self._upstream_task, self._downstream_task):
            if task is not None and not task.done():
                task.cancel()

    def abort_upstream(self) -> None:
        """Cancel the upstream task only.

        Stops sending ``DATA`` frames while keeping the downstream task
        alive to drain any queued response data.

        Idempotent — cancelling an already-done task is a no-op.

        Note:
            Not part of :class:`~exectunnel.transport._types.TransportHandler`.
            The session layer must type-narrow to :class:`TcpConnection`
            before calling.
        """
        if self._upstream_task is not None and not self._upstream_task.done():
            self._upstream_task.cancel()

    def abort_downstream(self) -> None:
        """Cancel the downstream task only.

        Idempotent — cancelling an already-done task is a no-op.

        Note:
            Not part of :class:`~exectunnel.transport._types.TransportHandler`.
            The session layer must type-narrow to :class:`TcpConnection`
            before calling.
        """
        if self._downstream_task is not None and not self._downstream_task.done():
            self._downstream_task.cancel()

    # ── Inbound enqueue APIs ──────────────────────────────────────────────────

    def feed(self, data: bytes) -> None:
        """Enqueue *data* received from the agent for the downstream task.

        Before the agent ACK, data is held in a bounded pre-ACK buffer.
        After the ACK, data is placed directly into the inbound queue.
        Use :meth:`feed_async` post-ACK for proper recv-loop backpressure.

        Args:
            data: Raw bytes decoded from a ``DATA`` frame payload.

        Raises:
            TransportError: One of the following ``error_code`` values:

                * ``"transport.invalid_payload_type"`` — *data* is not
                  :class:`bytes`.
                * ``"transport.pre_ack_buffer_overflow"`` — pre-ACK
                  buffer is full. Cleanup is scheduled before raising.
                * ``"transport.inbound_queue_full"`` — post-ACK inbound
                  queue is full.
        """
        require_bytes(data, self._id, "feed")

        if self._closed.is_set():
            return

        if not self._started:
            self._feed_pre_ack(data)
            return

        try:
            self._inbound.put_nowait(data)
        except asyncio.QueueFull:
            metrics_inc("tcp.connection.inbound_queue.drop")
            self._drop_count += 1
            raise TransportError(
                f"conn {self._id!r}: inbound queue full; chunk dropped.",
                error_code="transport.inbound_queue_full",
                details={
                    "conn_id": self._id,
                    "queue_cap": Defaults.TCP_INBOUND_QUEUE_CAP,
                    "drop_count": self._drop_count,
                },
                hint=(
                    "The downstream task is not draining fast enough. "
                    "Consider increasing TCP_INBOUND_QUEUE_CAP or investigating "
                    "local socket write latency."
                ),
            ) from None

    def _feed_pre_ack(self, data: bytes) -> None:
        """Append *data* to the pre-ACK buffer or tear down on overflow.

        Args:
            data: Raw bytes decoded from a ``DATA`` frame payload.

        Raises:
            TransportError: ``error_code=`` ``"transport.pre_ack_buffer_overflow"``
                if the buffer would overflow. Cleanup is scheduled before
                the exception is raised.
        """
        pending = self._pre_ack_buffer_bytes + len(data)
        if pending > self._pre_ack_buffer_cap_bytes:
            metrics_inc("tcp.connection.pre_ack_buffer.overflow")
            _log.warning(
                "conn %s: pre-ACK buffer full — closing connection "
                "(cap=%d, attempted=%d)",
                self._id,
                self._pre_ack_buffer_cap_bytes,
                pending,
                extra={"conn_id": self._id},
            )
            if self._cleanup_task is None:
                self._cleanup_task = asyncio.create_task(
                    self._cleanup(), name=f"tcp-cleanup-{self._id}"
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
                    "Increase PRE_ACK_BUFFER_CAP_BYTES or investigate "
                    "agent-side flow control."
                ),
            )
        self._pre_ack_buffer.append(data)
        self._pre_ack_buffer_bytes = pending

    def try_feed(self, data: bytes) -> bool:
        """Non-blocking post-ACK enqueue — returns ``False`` on queue full.

        Used by the session receive loop to avoid head-of-line blocking:
        when a single connection's downstream consumer is slow, awaiting
        :meth:`feed_async` would stall dispatch of frames for *all*
        other multiplexed connections on the same WebSocket. This method
        lets the caller detect saturation and tear the offending
        connection down (via ``ERROR``) without blocking the dispatcher.

        Args:
            data: Raw bytes decoded from a ``DATA`` frame payload.

        Returns:
            ``True`` if the chunk was enqueued or the connection is
            already closed (the frame is silently dropped and the call
            is treated as accepted). ``False`` if the post-ACK inbound
            queue is full.

        Raises:
            TransportError: One of:

                * ``"transport.invalid_payload_type"`` — *data* is not
                  :class:`bytes`.
                * ``"transport.try_feed_pre_ack"`` — called before
                  :meth:`start`. The pre-ACK path must use :meth:`feed`
                  which honours the pre-ACK buffer semantics.
        """
        require_bytes(data, self._id, "try_feed")

        if self._closed.is_set():
            return True

        if not self._started:
            raise TransportError(
                f"conn {self._id!r}: try_feed() called pre-ACK.",
                error_code="transport.try_feed_pre_ack",
                details={"conn_id": self._id},
                hint="Use feed() before the agent ACKs the connection.",
            )

        try:
            self._inbound.put_nowait(data)
        except asyncio.QueueFull:
            metrics_inc("tcp.connection.inbound_queue.drop")
            self._drop_count += 1
            return False
        return True

    async def feed_async(self, data: bytes) -> None:
        """Await space in the inbound queue, applying backpressure to the WS reader.

        Once ``put()`` completes the item is never rolled back, even if
        ``_closed`` is set concurrently — the downstream task delivers
        all queued data before honouring the close flag.

        Args:
            data: Raw bytes decoded from a ``DATA`` frame payload.

        Raises:
            TransportError: ``"transport.invalid_payload_type"`` if
                *data* is not :class:`bytes`.
            ConnectionClosedError: ``"transport.feed_async_on_closed"``
                if the connection is already closed before the call, or
                ``"transport.feed_async_closed_during_enqueue"`` if the
                connection closes while waiting to enqueue.
            asyncio.CancelledError: Propagated unchanged.
        """
        require_bytes(data, self._id, "feed_async")

        if self._closed.is_set():
            raise ConnectionClosedError(
                f"conn {self._id!r}: feed_async() called on a closed connection.",
                error_code="transport.feed_async_on_closed",
                details={"conn_id": self._id},
                hint="Check is_closed before calling feed_async().",
            )

        put_won, _ = await wait_first(
            self._inbound.put(data),
            self._closed,
            primary_name=f"tcp-feed-put-{self._id}",
            event_name=f"tcp-feed-close-{self._id}",
        )
        if put_won:
            return

        raise ConnectionClosedError(
            f"conn {self._id!r}: connection closed while waiting to enqueue data.",
            error_code="transport.feed_async_closed_during_enqueue",
            details={"conn_id": self._id},
            hint=(
                "The connection closed concurrently with feed_async(). "
                "The chunk was not enqueued."
            ),
        )

    # ── Copy tasks ────────────────────────────────────────────────────────────

    async def _upstream(self) -> None:
        """Copy local TCP bytes to WebSocket ``DATA`` frames.

        Reads chunks from the local TCP stream and encodes them as
        ``DATA`` frames via :func:`~exectunnel.protocol.encode_data_frame`.
        Byte accounting occurs after each successful send. Sends
        ``CONN_CLOSE`` in the ``finally`` block unless cancelled — a
        cancelled upstream delegates that responsibility to
        :meth:`_cleanup`.
        """
        async with aspan("tcp.connection.upstream"):
            start = asyncio.get_running_loop().time()
            metrics_inc("tcp.connection.upstream.started")
            cancelled = False
            try:
                while True:
                    chunk = await self._reader.read(Defaults.PIPE_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    await self._ws_send(
                        encode_data_frame(self._id, chunk), must_queue=True
                    )
                    self._bytes_upstream += len(chunk)
                    metrics_inc(
                        "tcp.connection.upstream.bytes",
                        conn_id=self._id,
                        bytes=len(chunk),
                    )
                self._upstream_ended_cleanly = True

            except asyncio.CancelledError:
                cancelled = True
                metrics_inc("tcp.connection.upstream.cancelled")
                raise

            except Exception as exc:
                log_task_exception(self._id, "upstream", exc, self._bytes_upstream)

            finally:
                elapsed = asyncio.get_running_loop().time() - start
                metrics_observe("tcp.connection.upstream.duration_sec", elapsed)
                metrics_observe(
                    "tcp.connection.upstream.bytes", float(self._bytes_upstream)
                )
                if not cancelled:
                    await self._send_close_frame_once()

    async def _downstream(self) -> None:
        """Copy inbound queue bytes to the local TCP writer.

        Drains the inbound queue in batches of up to
        :data:`~exectunnel.transport._constants.DOWNSTREAM_BATCH_SIZE`
        chunks per ``drain()`` call. When ``_remote_closed`` is set the
        loop continues until the queue is empty, then writes EOF and
        exits.

        Any :exc:`OSError` from :meth:`~asyncio.StreamWriter.write` or
        :meth:`~asyncio.StreamWriter.drain` means the local socket is
        dead — the task returns immediately. ``close_task`` is created
        once and reused across all blocking-wait iterations to avoid
        task churn.
        """
        async with aspan("tcp.connection.downstream"):
            start = asyncio.get_running_loop().time()
            metrics_inc("tcp.connection.downstream.started")

            close_task = asyncio.create_task(
                self._remote_closed.wait(), name=f"tcp-down-close-{self._id}"
            )

            try:
                while True:
                    if await self._downstream_iteration(close_task):
                        break

            except asyncio.CancelledError:
                metrics_inc("tcp.connection.downstream.cancelled")
                raise

            except Exception as exc:
                log_task_exception(self._id, "downstream", exc, self._bytes_downstream)

            finally:
                if not close_task.done():
                    close_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await close_task
                elapsed = asyncio.get_running_loop().time() - start
                metrics_observe("tcp.connection.downstream.duration_sec", elapsed)
                metrics_observe(
                    "tcp.connection.downstream.bytes", float(self._bytes_downstream)
                )

    async def _downstream_iteration(self, close_task: asyncio.Task[bool]) -> bool:
        """Run one iteration of the downstream copy loop.

        Args:
            close_task: Long-lived task waiting on ``_remote_closed``.
                Reused across iterations to avoid task churn.

        Returns:
            ``True`` when the loop should exit (either EOF written or
            local socket dead); ``False`` to continue.
        """
        batch: list[bytes] = []
        while len(batch) < DOWNSTREAM_BATCH_SIZE:
            try:
                batch.append(self._inbound.get_nowait())
            except asyncio.QueueEmpty:
                break

        if batch:
            for chunk in batch:
                self._writer.write(chunk)
            try:
                await self._writer.drain()
            except OSError as exc:
                metrics_inc("tcp.connection.downstream.error", error="os_drain")
                _log.debug(
                    "conn %s: downstream write error: %s — local socket closed",
                    self._id,
                    exc,
                    extra={"conn_id": self._id},
                )
                return True
            batch_bytes = sum(len(c) for c in batch)
            self._bytes_downstream += batch_bytes
            metrics_inc(
                "tcp.connection.downstream.bytes",
                conn_id=self._id,
                bytes=batch_bytes,
            )
            return False

        if self._remote_closed.is_set():
            if self._writer.can_write_eof():
                try:
                    self._writer.write_eof()
                    await self._writer.drain()
                except OSError as exc:
                    _log.debug(
                        "conn %s: downstream write_eof error: %s",
                        self._id,
                        exc,
                        extra={"conn_id": self._id},
                    )
                    return True
            self._downstream_ended_cleanly = True
            return True

        get_task = asyncio.create_task(
            self._inbound.get(), name=f"tcp-down-get-{self._id}"
        )
        try:
            done, _ = await asyncio.wait(
                {get_task, close_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            get_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await get_task
            raise

        if get_task in done and not get_task.cancelled():
            chunk = get_task.result()
            self._writer.write(chunk)
            try:
                await self._writer.drain()
            except OSError as exc:
                metrics_inc("tcp.connection.downstream.error", error="os_drain")
                _log.debug(
                    "conn %s: downstream write error: %s — local socket closed",
                    self._id,
                    exc,
                    extra={"conn_id": self._id},
                )
                return True
            self._bytes_downstream += len(chunk)
            metrics_inc(
                "tcp.connection.downstream.bytes",
                conn_id=self._id,
                bytes=len(chunk),
            )
            return False

        get_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await get_task
        return False

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _send_close_frame_once(self) -> None:
        """Send ``CONN_CLOSE`` exactly once via the protocol helper.

        Idempotent via ``_conn_close_sent``. All exceptions are caught
        and logged — teardown must complete regardless of send failures.
        """
        if self._conn_close_sent:
            return
        self._conn_close_sent = True
        try:
            await self._ws_send(encode_conn_close_frame(self._id), control=True)
        except WebSocketSendTimeoutError as exc:
            metrics_inc("tcp.connection.conn_close.error", error="ws_send_timeout")
            _log.warning(
                "conn %s: CONN_CLOSE send timed out (error_id=%s)",
                self._id,
                exc.error_id,
                extra={
                    "conn_id": self._id,
                    "error_code": exc.error_code,
                    "error_id": exc.error_id,
                },
            )
        except ConnectionClosedError as exc:
            _log.debug(
                "conn %s: CONN_CLOSE skipped — connection already closed (error_id=%s)",
                self._id,
                exc.error_id,
                extra={"conn_id": self._id, "error_id": exc.error_id},
            )
        except Exception as exc:
            _log.warning("conn %s: failed to send CONN_CLOSE: %s", self._id, exc)
            _log.debug(
                "conn %s: CONN_CLOSE send failure traceback",
                self._id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        """Cancel the sibling task if needed and schedule cleanup when both are done.

        Half-close: when ``_upstream`` exits cleanly (local EOF, no
        error, not cancelled), ``_downstream`` is kept alive to deliver
        any remaining agent response. In all other cases the sibling is
        cancelled.

        ``_cleanup_task is None`` prevents double-scheduling;
        ``_closed.is_set()`` inside :meth:`_cleanup` prevents
        double-execution.
        """
        task_exc: BaseException | None = None
        if not task.cancelled():
            task_exc = task.exception()
            if task_exc is not None:
                _log.debug(
                    "conn %s task %s ended with error: %s",
                    self._id,
                    task.get_name(),
                    task_exc,
                )

        task_ended_cleanly = not task.cancelled() and task_exc is None

        if task is self._upstream_task:
            should_cancel_peer = not (
                task_ended_cleanly and self._upstream_ended_cleanly
            )
        else:
            should_cancel_peer = True

        if should_cancel_peer:
            for candidate in (self._upstream_task, self._downstream_task):
                if (
                    candidate is not None
                    and candidate is not task
                    and not candidate.done()
                ):
                    candidate.cancel()

        both_done = (self._upstream_task is None or self._upstream_task.done()) and (
            self._downstream_task is None or self._downstream_task.done()
        )
        if both_done and not self._closed.is_set() and self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(
                self._cleanup(), name=f"tcp-cleanup-{self._id}"
            )
            self._cleanup_task.add_done_callback(self._on_cleanup_done)

    @staticmethod
    def _on_cleanup_done(task: asyncio.Task[None]) -> None:
        """Log unexpected exceptions from the cleanup task.

        Args:
            task: The completed cleanup task.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.warning(
                "conn cleanup task %s raised an unexpected exception: %s",
                task.get_name(),
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def _cleanup(self) -> None:
        """Await all tasks, send ``CONN_CLOSE``, evict from registry, close writer.

        ``_closed`` is set at entry as the idempotency gate. Tasks
        already log their own exceptions — this method awaits them only
        to collect completion, then delegates the ``CONN_CLOSE`` safety
        net to :meth:`_send_close_frame_once`.
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

        await self._send_close_frame_once()

        if self._registry.pop(self._id, None) is not None:
            metrics_gauge_dec("session.active.tcp_connections")

        with contextlib.suppress(OSError, RuntimeError):
            self._writer.close()
        with contextlib.suppress(OSError, RuntimeError, asyncio.TimeoutError):
            async with asyncio.timeout(WRITER_CLOSE_TIMEOUT_SECS):
                await self._writer.wait_closed()

        _log.debug(
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
        """``True`` once :meth:`_cleanup` has run."""
        return self._closed.is_set()

    @property
    def is_remote_closed(self) -> bool:
        """``True`` once :meth:`on_remote_closed` has been called."""
        return self._remote_closed.is_set()

    @property
    def closed_event(self) -> asyncio.Event:
        """Read-only view of the closed event for external ``await`` use.

        Warning:
            Do not call ``.set()`` directly — use :meth:`abort` or
            :meth:`on_remote_closed` to ensure cleanup runs correctly.
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

    def __repr__(self) -> str:
        if self._closed.is_set():
            state = "closed"
        elif self._started:
            state = "started"
        else:
            state = "new"
        return (
            f"<TcpConnection id={self._id} state={state} "
            f"remote_closed={self._remote_closed.is_set()} "
            f"up={self._bytes_upstream} down={self._bytes_downstream} "
            f"drops={self._drop_count}>"
        )
