"""
``_TcpConnectionHandler`` bridges one local TCP stream to one agent-side TCP
connection via the WebSocket frame protocol.

Data flow
---------
* **upstream**:   local TCP → ``encode_data_frame`` → WebSocket
* **downstream**: WebSocket DATA frames (queued by recv_loop) → local TCP

Lifecycle
---------
1. Create handler, register it in the shared registry.
2. Call :meth:`start` once the agent ACKs the connection.
3. The agent signals close via ``CONN_CLOSE`` or ``ERROR`` →
   recv_loop calls :meth:`close_remote`, which sets ``_remote_closed``
   and wakes ``_downstream`` via ``_remote_closed_event``.
4. ``_downstream`` drains all queued data, writes EOF, then exits.
5. Either direction finishing triggers :meth:`_on_task_done`, which
   cancels the sibling task and schedules :meth:`_cleanup`.
6. :meth:`_cleanup` awaits both tasks, evicts from registry, closes writer.

Half-close note
---------------
When the local client sends EOF, ``_upstream`` finishes cleanly and sends
``CONN_CLOSE`` to the agent.  The agent does ``SHUT_WR`` on the remote socket
and waits up to 30 s for the remote to close its side before emitting
``CONN_CLOSED_ACK``.  During that window ``_downstream`` is blocked on
``_inbound.get()`` and the writer is held open.  If the remote never closes,
both the agent thread and the local ``_downstream`` task leak for up to 30 s
per connection — acceptable for normal traffic but can accumulate under high
connection churn with misbehaving remote servers.

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
from collections.abc import Coroutine
from typing import Any, Protocol, runtime_checkable

from exectunnel.config.defaults import PRE_ACK_BUFFER_CAP_BYTES, TCP_INBOUND_QUEUE_CAP
from exectunnel.exceptions import (
    ConnectionClosedError,
    ExecTunnelError,
    FrameDecodingError,
    TransportError,
    WebSocketSendTimeoutError,
)
from exectunnel.observability import metrics_inc, metrics_observe, span
from exectunnel.protocol.frames import (
    PIPE_READ_CHUNK_BYTES,
    encode_conn_close_frame,
    encode_data_frame,
)

logger = logging.getLogger("exectunnel.transport.connection")

# Timeout applied to writer.wait_closed() during cleanup to prevent indefinite
# hangs when the OS never delivers the FIN ACK.
_WRITER_CLOSE_TIMEOUT_SECS: float = 5.0

# Maximum number of chunks to batch-write before calling drain().
# Batching amortises the per-drain syscall overhead on high-throughput streams.
_DOWNSTREAM_BATCH_SIZE: int = 16


# ── ws_send callable type ─────────────────────────────────────────────────────


@runtime_checkable
class WsSendCallable(Protocol):
    """Structural type for the WebSocket send callable injected into the handler.

    Implementations must accept:

    * ``frame``       — the newline-terminated frame string to send.
    * ``must_queue``  — if ``True``, block until the frame is enqueued even
                        when the send queue is under backpressure.
    * ``control``     — if ``True``, the frame is a control/priority frame
                        that bypasses normal flow-control ordering.
    """

    def __call__(
        self,
        frame: str,
        *,
        must_queue: bool = False,
        control: bool = False,
    ) -> Coroutine[Any, Any, None]: ...


# ── Feed data validation ──────────────────────────────────────────────────────


def _require_bytes(data: object, conn_id: str, method: str) -> bytes:
    """Return *data* as :class:`bytes` or raise :class:`FrameDecodingError`.

    Args:
        data:    The value to validate.
        conn_id: Connection ID for error context.
        method:  Calling method name for error context (``"feed"`` etc.).

    Returns:
        *data* unchanged, typed as ``bytes``.

    Raises:
        FrameDecodingError: If *data* is not a ``bytes`` instance.
    """
    if not isinstance(data, bytes):
        raise FrameDecodingError(
            f"conn {conn_id!r}: {method}() received a non-bytes payload.",
            error_code=f"protocol.tcp_{method}_bad_type",
            details={
                "conn_id": conn_id,
                "received_type": type(data).__name__,
            },
            hint=f"Ensure the frame decoder always passes raw bytes to {method}().",
        )
    return data


# ── Handler ───────────────────────────────────────────────────────────────────


class _TcpConnectionHandler:
    """Bridges one local TCP connection to one agent-side TCP connection.

    Args:
        conn_id:               Stable identifier for this connection.
        reader:                asyncio stream reader for the local TCP client.
        writer:                asyncio stream writer for the local TCP client.
        ws_send:               Coroutine callable that sends a frame string over
                               the WebSocket / exec channel.  Must conform to
                               :class:`WsSendCallable`.
        registry:              Shared mapping of ``conn_id → handler``; the
                               handler removes itself on cleanup.
        pre_ack_buffer_cap_bytes: Maximum bytes to buffer before the agent ACKs
                               the connection.  Defaults to
                               :data:`~exectunnel.config.defaults.PRE_ACK_BUFFER_CAP_BYTES`.
    """

    def __init__(
        self,
        conn_id: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        ws_send: WsSendCallable,
        registry: dict[str, _TcpConnectionHandler],
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

        # Named task references — single source of truth; no parallel list.
        self._upstream_task: asyncio.Task[None] | None = None
        self._downstream_task: asyncio.Task[None] | None = None

        # Cleanup task reference kept so we can log unexpected failures.
        self._cleanup_task: asyncio.Task[None] | None = None

        # Lifecycle flags.
        self._closed = asyncio.Event()
        self._started = False
        self._conn_close_sent = False

        # Single event replaces both _close_remote_requested bool and
        # _close_event asyncio.Event — one piece of state, one source of truth.
        self._remote_closed = asyncio.Event()

        # Telemetry.
        self._drop_count = 0
        self._bytes_upstream = 0
        self._bytes_downstream = 0
        self._upstream_ended_cleanly = False
        self._downstream_ended_cleanly = False

        # Pre-ACK buffer: holds data that arrives before start() is called.
        self._pre_ack_buffer_cap_bytes = max(1, pre_ack_buffer_cap_bytes)
        self._pre_ack_buffer: list[bytes] = []
        self._pre_ack_buffer_bytes = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the upstream and downstream copy tasks.

        Idempotent — subsequent calls are logged and ignored.

        If :meth:`close_remote` was called before :meth:`start` (e.g. the
        agent rejected the connection immediately), ``_remote_closed`` is
        already set and ``_downstream`` will drain any buffered data then exit
        cleanly on its first iteration.

        Raises:
            FrameDecodingError: If any pre-ACK buffered chunk is not a
                ``bytes`` instance — indicates a protocol-layer bug.
        """
        if self._started:
            logger.debug(
                "conn %s: start() called more than once; ignoring",
                self._id,
                extra={"conn_id": self._id},
            )
            return
        self._started = True

        # Flush pre-ACK buffer into the inbound queue before starting tasks
        # so _downstream sees the data immediately on its first iteration.
        if self._pre_ack_buffer:
            for chunk in self._pre_ack_buffer:
                _require_bytes(chunk, self._id, "pre_ack_buffer")
                try:
                    self._inbound.put_nowait(chunk)
                except asyncio.QueueFull:
                    # Queue is full — write directly to the transport.
                    # _downstream has not started yet so there is no concurrent
                    # reader; this is safe.  Schedule a drain after task start
                    # via the normal downstream path — the writer's internal
                    # buffer will be flushed on the first drain() call.
                    self._drop_count += 1
                    metrics_inc("connection.inbound_queue.drop")
                    logger.debug(
                        "conn %s: pre-ACK queue full during flush, "
                        "writing %d bytes directly to transport buffer",
                        self._id,
                        len(chunk),
                        extra={"conn_id": self._id},
                    )
                    with contextlib.suppress(OSError):
                        self._writer.write(chunk)
            self._pre_ack_buffer.clear()
            self._pre_ack_buffer_bytes = 0

        # Create tasks and register done callbacks before storing references
        # so there is no window where a callback fires before _upstream_task /
        # _downstream_task are assigned.
        upstream_task: asyncio.Task[None] = asyncio.create_task(
            self._upstream(), name=f"conn-up-{self._id}"
        )
        downstream_task: asyncio.Task[None] = asyncio.create_task(
            self._downstream(), name=f"conn-down-{self._id}"
        )
        self._upstream_task = upstream_task
        self._downstream_task = downstream_task

        upstream_task.add_done_callback(self._on_task_done)
        downstream_task.add_done_callback(self._on_task_done)

    # ── Called by recv_loop ───────────────────────────────────────────────────

    def feed(self, data: bytes) -> bool:
        """Enqueue *data* received from the agent for the downstream task.

        Use only before ACK (pre-start path) or in contexts where backpressure
        is not required.  After ACK, prefer :meth:`feed_async` so the recv_loop
        applies proper backpressure.

        Args:
            data: Raw bytes decoded from a ``DATA`` frame payload.

        Returns:
            ``True`` if the data was accepted; ``False`` if the connection is
            closed or the buffer / queue is full.

        Raises:
            FrameDecodingError: If *data* is not a ``bytes`` instance.
        """
        _require_bytes(data, self._id, "feed")

        if self._closed.is_set():
            return False

        if not self._started:
            pending = self._pre_ack_buffer_bytes + len(data)
            if pending > self._pre_ack_buffer_cap_bytes:
                metrics_inc("connection.pre_ack_buffer.overflow")
                return False
            self._pre_ack_buffer.append(data)
            self._pre_ack_buffer_bytes = pending
            return True

        # Post-ACK non-blocking path — caller should use feed_async instead.
        try:
            self._inbound.put_nowait(data)
            return True
        except asyncio.QueueFull:
            metrics_inc("connection.inbound_queue.drop")
            self._drop_count += 1
            return False

    async def feed_async(self, data: bytes) -> bool:
        """Await space in the inbound queue, applying backpressure to the WS reader.

        Returns ``False`` if the connection was closed before data could be
        enqueued.  Re-checks ``_closed`` after the await so that data is never
        enqueued after cleanup has already run.

        The enqueued item is intentionally not rolled back when
        ``_remote_closed`` is set after the ``put()`` completes.  Rolling back
        had a TOCTOU race: if ``_downstream`` had already exited its drain loop
        by the time the rollback ran, the item was removed from the queue but
        never written to the local socket, silently truncating the stream.
        Instead we leave the item in the queue and let ``_downstream`` drain it
        before honouring the close flag — the drain-then-close ordering in
        ``_downstream`` guarantees every byte the agent sent is delivered before
        EOF is written.

        Args:
            data: Raw bytes decoded from a ``DATA`` frame payload.

        Returns:
            ``True`` if the data was accepted; ``False`` if the connection
            closed while waiting.

        Raises:
            FrameDecodingError:      If *data* is not a ``bytes`` instance.
            asyncio.CancelledError:  Propagated as-is — never suppressed.
        """
        _require_bytes(data, self._id, "feed_async")

        if self._closed.is_set():
            return False

        # Queue.put() only raises CancelledError — no broad except needed.
        await self._inbound.put(data)

        return not self._closed.is_set()

    def close_remote(self) -> None:
        """Signal that the agent has closed its side of the connection.

        Sets ``_remote_closed`` and wakes ``_downstream`` so it can drain
        remaining queued data and then write EOF to the local socket.

        Safe to call before :meth:`start` — ``_downstream`` checks the event
        on its first iteration and exits cleanly if no data is queued.
        Idempotent — subsequent calls are no-ops.
        """
        if self._closed.is_set():
            return
        self._remote_closed.set()

    # ── Copy tasks ────────────────────────────────────────────────────────────

    async def _upstream(self) -> None:
        """local TCP → WebSocket DATA frames.

        Reads chunks from the local TCP stream and encodes them as ``DATA``
        frames using :func:`~exectunnel.protocol.frames.encode_data_frame`,
        which applies ``urlsafe_b64encode`` with no padding internally.

        Byte accounting is performed **after** a successful send so that
        ``bytes_upstream`` reflects bytes that were actually delivered to the
        tunnel, not bytes that were read but may have been lost on send failure.

        Exception handling
        ------------------
        ``asyncio.CancelledError``
            Re-raised immediately — cancellation is always intentional.
        ``WebSocketSendTimeoutError``
            Tunnel send queue stalled; log at WARNING, exit task.
        ``ConnectionClosedError``
            WebSocket dropped mid-stream; log at WARNING, exit task.
        ``TransportError``
            Structured transport failure; log at WARNING.
        ``OSError``
            Local socket error; log at DEBUG (routine).
        ``ExecTunnelError``
            Library catch-all; log at WARNING.
        ``Exception``
            Truly unexpected; log at WARNING with traceback.
        """
        with span("connection.upstream"):
            start = asyncio.get_running_loop().time()
            metrics_inc("connection.upstream.started")
            try:
                while True:
                    chunk = await self._reader.read(PIPE_READ_CHUNK_BYTES)
                    if not chunk:
                        break

                    # encode_data_frame accepts raw bytes and applies
                    # urlsafe_b64encode with no padding — no manual encoding.
                    frame = encode_data_frame(self._id, chunk)
                    await self._ws_send(frame, must_queue=True)

                    # Account bytes only after successful send.
                    self._bytes_upstream += len(chunk)

                self._upstream_ended_cleanly = True

            except asyncio.CancelledError:
                metrics_inc("connection.upstream.cancelled")
                raise

            except WebSocketSendTimeoutError as exc:
                metrics_inc("connection.upstream.error", error="ws_send_timeout")
                logger.warning(
                    "conn %s: upstream stalled — WebSocket send timed out "
                    "[%s] (bytes_sent=%d, error_id=%s)",
                    self._id,
                    exc.error_code,
                    self._bytes_upstream,
                    exc.error_id,
                    extra={
                        "conn_id": self._id,
                        "direction": "upstream",
                        "bytes_sent": self._bytes_upstream,
                        "error_code": exc.error_code,
                        "error_id": exc.error_id,
                    },
                )

            except ConnectionClosedError as exc:
                metrics_inc("connection.upstream.error", error="connection_closed")
                logger.warning(
                    "conn %s: upstream ended — tunnel connection closed "
                    "[%s] (bytes_sent=%d, error_id=%s)",
                    self._id,
                    exc.error_code,
                    self._bytes_upstream,
                    exc.error_id,
                    extra={
                        "conn_id": self._id,
                        "direction": "upstream",
                        "bytes_sent": self._bytes_upstream,
                        "error_code": exc.error_code,
                        "error_id": exc.error_id,
                    },
                )

            except TransportError as exc:
                metrics_inc(
                    "connection.upstream.error",
                    error=exc.error_code.replace(".", "_"),
                )
                logger.warning(
                    "conn %s: upstream transport error [%s]: %s "
                    "(bytes_sent=%d, error_id=%s)",
                    self._id,
                    exc.error_code,
                    exc.message,
                    self._bytes_upstream,
                    exc.error_id,
                    extra={
                        "conn_id": self._id,
                        "direction": "upstream",
                        "bytes_sent": self._bytes_upstream,
                        "error_code": exc.error_code,
                        "error_id": exc.error_id,
                    },
                )

            except OSError as exc:
                metrics_inc("connection.upstream.error", error="os_error")
                logger.debug(
                    "conn %s: upstream socket error: %s",
                    self._id,
                    exc,
                    exc_info=True,
                    extra={
                        "conn_id": self._id,
                        "direction": "upstream",
                        "bytes_sent": self._bytes_upstream,
                    },
                )

            except ExecTunnelError as exc:
                metrics_inc(
                    "connection.upstream.error",
                    error=exc.error_code.replace(".", "_"),
                )
                logger.warning(
                    "conn %s: upstream library error [%s]: %s (error_id=%s)",
                    self._id,
                    exc.error_code,
                    exc.message,
                    exc.error_id,
                    extra={
                        "conn_id": self._id,
                        "direction": "upstream",
                        "bytes_sent": self._bytes_upstream,
                        "error_code": exc.error_code,
                        "error_id": exc.error_id,
                    },
                )

            except Exception as exc:
                metrics_inc(
                    "connection.upstream.error",
                    error=type(exc).__name__,
                )
                logger.warning(
                    "conn %s: upstream unexpected failure: %s",
                    self._id,
                    exc,
                    extra={
                        "conn_id": self._id,
                        "direction": "upstream",
                        "bytes_sent": self._bytes_upstream,
                    },
                )
                logger.debug(
                    "conn %s: upstream traceback",
                    self._id,
                    exc_info=True,
                    extra={"conn_id": self._id},
                )

            finally:
                elapsed = asyncio.get_running_loop().time() - start
                metrics_observe("connection.upstream.duration_sec", elapsed)
                metrics_observe(
                    "connection.upstream.bytes", float(self._bytes_upstream)
                )
                await self._send_close_frame_once()

    async def _downstream(self) -> None:
        """Inbound queue → local TCP.

        Drains the inbound queue in batches of up to :data:`_DOWNSTREAM_BATCH_SIZE`
        chunks per ``drain()`` call to amortise syscall overhead on high-throughput
        streams.

        Close sequencing
        ----------------
        When ``_remote_closed`` is set, the loop continues draining until the
        queue is empty, then writes EOF and exits.  This guarantees every byte
        the agent sent is delivered before the local socket is half-closed.

        Exception handling
        ------------------
        ``asyncio.CancelledError``
            Re-raised immediately — cancellation is always intentional.
        ``ConnectionClosedError``
            WebSocket dropped while waiting for data; exit task.
        ``TransportError``
            Structured transport failure; log at WARNING.
        ``OSError``
            Local socket error; log at DEBUG (routine).
        ``ExecTunnelError``
            Library catch-all; log at WARNING.
        ``Exception``
            Truly unexpected; log at WARNING with traceback.
        """
        with span("connection.downstream"):
            start = asyncio.get_running_loop().time()
            metrics_inc("connection.downstream.started")
            try:
                while True:
                    # ── Batch-drain available items ───────────────────────────
                    batch: list[bytes] = []

                    # Collect all immediately available items up to batch limit.
                    while len(batch) < _DOWNSTREAM_BATCH_SIZE:
                        try:
                            batch.append(self._inbound.get_nowait())
                        except asyncio.QueueEmpty:
                            break

                    if batch:
                        # Write the whole batch then drain once.
                        for chunk in batch:
                            self._bytes_downstream += len(chunk)
                            self._writer.write(chunk)
                        with contextlib.suppress(OSError):
                            await self._writer.drain()
                        continue

                    # ── Queue is empty — check close flag ─────────────────────
                    if self._remote_closed.is_set():
                        if self._writer.can_write_eof():
                            with contextlib.suppress(OSError):
                                self._writer.write_eof()
                                await self._writer.drain()
                        self._downstream_ended_cleanly = True
                        break

                    # ── Block until data arrives OR remote closes ──────────────
                    # Use asyncio.wait on two awaitables so we wake on whichever
                    # fires first without creating tasks on every iteration.
                    # asyncio.wait requires a set of awaitables; we wrap the
                    # coroutines in tasks once and reuse them across the wait.
                    get_task: asyncio.Task[bytes] = asyncio.create_task(
                        self._inbound.get(), name=f"conn-down-get-{self._id}"
                    )
                    close_task: asyncio.Task[None] = asyncio.create_task(
                        self._remote_closed.wait(),
                        name=f"conn-down-close-{self._id}",
                    )

                    done, pending = await asyncio.wait(
                        {get_task, close_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    # Cancel and await pending tasks to avoid resource leaks.
                    for t in pending:
                        t.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await t

                    if get_task in done and not get_task.cancelled():
                        # Data arrived — write it and loop to drain more.
                        chunk = get_task.result()
                        self._bytes_downstream += len(chunk)
                        self._writer.write(chunk)
                        with contextlib.suppress(OSError):
                            await self._writer.drain()
                    # If close_task fired (or both fired), loop back to the
                    # batch-drain path which will empty the queue before
                    # honouring the close flag.

            except asyncio.CancelledError:
                metrics_inc("connection.downstream.cancelled")
                raise

            except ConnectionClosedError as exc:
                metrics_inc("connection.downstream.error", error="connection_closed")
                logger.warning(
                    "conn %s: downstream ended — tunnel connection closed "
                    "[%s] (bytes_recv=%d, error_id=%s)",
                    self._id,
                    exc.error_code,
                    self._bytes_downstream,
                    exc.error_id,
                    extra={
                        "conn_id": self._id,
                        "direction": "downstream",
                        "bytes_recv": self._bytes_downstream,
                        "error_code": exc.error_code,
                        "error_id": exc.error_id,
                    },
                )

            except TransportError as exc:
                metrics_inc(
                    "connection.downstream.error",
                    error=exc.error_code.replace(".", "_"),
                )
                logger.warning(
                    "conn %s: downstream transport error [%s]: %s "
                    "(bytes_recv=%d, error_id=%s)",
                    self._id,
                    exc.error_code,
                    exc.message,
                    self._bytes_downstream,
                    exc.error_id,
                    extra={
                        "conn_id": self._id,
                        "direction": "downstream",
                        "bytes_recv": self._bytes_downstream,
                        "error_code": exc.error_code,
                        "error_id": exc.error_id,
                    },
                )

            except OSError as exc:
                metrics_inc("connection.downstream.error", error="os_error")
                logger.debug(
                    "conn %s: downstream socket error: %s",
                    self._id,
                    exc,
                    exc_info=True,
                    extra={
                        "conn_id": self._id,
                        "direction": "downstream",
                        "bytes_recv": self._bytes_downstream,
                    },
                )

            except ExecTunnelError as exc:
                metrics_inc(
                    "connection.downstream.error",
                    error=exc.error_code.replace(".", "_"),
                )
                logger.warning(
                    "conn %s: downstream library error [%s]: %s (error_id=%s)",
                    self._id,
                    exc.error_code,
                    exc.message,
                    exc.error_id,
                    extra={
                        "conn_id": self._id,
                        "direction": "downstream",
                        "bytes_recv": self._bytes_downstream,
                        "error_code": exc.error_code,
                        "error_id": exc.error_id,
                    },
                )

            except Exception as exc:
                metrics_inc(
                    "connection.downstream.error",
                    error=type(exc).__name__,
                )
                logger.warning(
                    "conn %s: downstream unexpected failure: %s",
                    self._id,
                    exc,
                    extra={
                        "conn_id": self._id,
                        "direction": "downstream",
                        "bytes_recv": self._bytes_downstream,
                    },
                )
                logger.debug(
                    "conn %s: downstream traceback",
                    self._id,
                    exc_info=True,
                    extra={"conn_id": self._id},
                )

            finally:
                elapsed = asyncio.get_running_loop().time() - start
                metrics_observe("connection.downstream.duration_sec", elapsed)
                metrics_observe(
                    "connection.downstream.bytes", float(self._bytes_downstream)
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
            metrics_inc("connection.conn_close.error", error="ws_send_timeout")
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
        except TransportError as exc:
            metrics_inc(
                "connection.conn_close.error",
                error=exc.error_code.replace(".", "_"),
            )
            logger.debug(
                "conn %s: failed to send CONN_CLOSE [%s]: %s (error_id=%s)",
                self._id,
                exc.error_code,
                exc.message,
                exc.error_id,
                exc_info=True,
                extra={
                    "conn_id": self._id,
                    "error_code": exc.error_code,
                    "error_id": exc.error_id,
                },
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

        Half-close logic
        ----------------
        When a task exits **cleanly** (no exception, not cancelled), the peer
        task is kept alive to avoid truncating in-flight data.  For example,
        when ``_upstream`` finishes because the local client sent EOF, we keep
        ``_downstream`` running so the remote server's response can still be
        delivered.

        Cleanup scheduling
        ------------------
        Cleanup is scheduled only when **both** tasks are done.  The
        ``_closed`` event is used as the gate — if it is already set, a second
        cleanup task is never created.
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

        # Determine whether the peer task should be cancelled.
        # A cleanly-ended task keeps its peer alive (half-close support).
        task_ended_cleanly = (
            not task.cancelled() and task.exception() is None
        )
        peer_survives = task_ended_cleanly and (
            (task is self._upstream_task and self._upstream_ended_cleanly)
            or (task is self._downstream_task and self._downstream_ended_cleanly)
        )

        if not peer_survives:
            # Cancel whichever task is not the one that just finished.
            for candidate in (self._upstream_task, self._downstream_task):
                if candidate is not None and candidate is not task and not candidate.done():
                    candidate.cancel()

        # Only schedule cleanup when both tasks are finished and cleanup has
        # not already started (_closed is the authoritative gate).
        both_done = (
            (self._upstream_task is None or self._upstream_task.done())
            and (self._downstream_task is None or self._downstream_task.done())
        )
        if both_done and not self._closed.is_set():
            self._cleanup_task = asyncio.create_task(
                self._cleanup(), name=f"conn-cleanup-{self._id}"
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

        Idempotent — ``_closed`` is set atomically at entry so concurrent
        calls (e.g. from a second ``_on_task_done`` firing before the first
        cleanup task runs) are no-ops.
        """
        if self._closed.is_set():
            return
        self._closed.set()
        metrics_inc("connection.cleanup")

        for task in (self._upstream_task, self._downstream_task):
            if task is None:
                continue
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError):
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

    # ── Public control ────────────────────────────────────────────────────────

    def cancel_upstream(self) -> None:
        """Cancel the upstream task so no more DATA frames are sent to the agent.

        Used when the agent signals an ``ERROR`` — the downstream path is
        closed via :meth:`close_remote`, and this stops the local→agent
        direction so we don't keep sending DATA frames to a connection the
        agent has already torn down.
        """
        if self._upstream_task is not None and not self._upstream_task.done():
            self._upstream_task.cancel()

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def closed(self) -> asyncio.Event:
        """Event that is set once :meth:`_cleanup` has completed."""
        return self._closed

    @property
    def conn_id(self) -> str:
        """The stable identifier for this TCP connection."""
        return self._id

    @property
    def bytes_upstream(self) -> int:
        """Total bytes successfully forwarded from local TCP to the tunnel."""
        return self._bytes_upstream

    @property
    def bytes_downstream(self) -> int:
        """Total bytes forwarded from the tunnel to local TCP."""
        return self._bytes_downstream

    @property
    def drop_count(self) -> int:
        """Total inbound chunks dropped due to queue saturation or buffer overflow."""
        return self._drop_count

    @property
    def is_started(self) -> bool:
        """``True`` once :meth:`start` has been called successfully."""
        return self._started

    @property
    def is_remote_closed(self) -> bool:
        """``True`` once :meth:`close_remote` has been called."""
        return self._remote_closed.is_set()
