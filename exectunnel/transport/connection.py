"""
``_TcpConnectionHandler`` bridges one local TCP stream to one agent-side TCP
connection via the WebSocket frame protocol.

Renamed from ``_ConnHandler`` (abbreviated) to ``_TcpConnectionHandler``
(fully spelled out) per the naming-convention report.

Data flow
---------
* **upstream**:   local TCP → base64 DATA frames → WebSocket
* **downstream**: WebSocket DATA frames (queued by recv_loop) → local TCP

Lifecycle
---------
1. Create handler, register it in the shared registry.
2. Call :meth:`start` once the agent ACKs the connection (``CONN_ACK``).
3. The agent signals close via ``CONN_CLOSED_ACK`` or ``ERROR`` → the
   recv_loop calls :meth:`close_remote`, which enqueues a ``None`` sentinel
   into the inbound queue.
4. Either direction finishing triggers :meth:`_cleanup`, which cancels the
   sibling task and **awaits** its completion before closing the writer.

Half-close note
---------------
When the local client sends EOF, ``_upstream`` finishes cleanly and sends
``CONN_CLOSE`` to the agent.  The agent does ``SHUT_WR`` on the remote socket
and waits up to 30 s for the remote to close its side before emitting
``CONN_CLOSED_ACK``.  During that window the local ``_downstream`` task is
blocked on ``_inbound.get()`` and the writer is held open.  If the remote
never closes, both the agent thread and the local ``_downstream`` task leak
for up to 30 s per connection — acceptable for normal traffic but can
accumulate under high connection churn with misbehaving remote servers.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from exectunnel.config.defaults import PRE_ACK_BUFFER_CAP_BYTES, TCP_INBOUND_QUEUE_CAP
from exectunnel.exceptions import (
    ConnectionClosedError,
    ExecTunnelError,
    FrameDecodingError,
    TransportError,
    WebSocketSendTimeoutError,
)
from exectunnel.observability import metrics_inc, metrics_observe, span
from exectunnel.protocol.frames import PIPE_READ_CHUNK_BYTES, encode_frame

logger = logging.getLogger("exectunnel.transport.connection")


class _TcpConnectionHandler:
    """Bridges one local TCP connection to one agent-side TCP connection."""

    def __init__(
        self,
        conn_id: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        ws_send: Callable[..., Coroutine[Any, Any, None]],
        registry: dict[str, _TcpConnectionHandler],
        *,
        pre_ack_buffer_cap_bytes: int = PRE_ACK_BUFFER_CAP_BYTES,
    ) -> None:
        self._id = conn_id
        self._reader = reader
        self._writer = writer
        self._ws_send = ws_send
        self._registry = registry
        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=TCP_INBOUND_QUEUE_CAP)
        self._tasks: list[asyncio.Task[None]] = []
        self._cleanup_task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self._started = False
        self._conn_close_sent = False
        self._drop_count = 0
        self._bytes_upstream = 0
        self._bytes_downstream = 0
        self._upstream_task: asyncio.Task[None] | None = None
        self._downstream_task: asyncio.Task[None] | None = None
        self._upstream_ended_cleanly = False
        self._downstream_ended_cleanly = False
        self._pre_ack_buffer_cap_bytes = max(1, pre_ack_buffer_cap_bytes)
        self._pre_ack_buffer: list[bytes] = []
        self._pre_ack_buffer_bytes = 0
        # Set by close_remote() to signal downstream EOF without evicting data.
        self._close_remote_requested = False
        self._close_event: asyncio.Event = asyncio.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the upstream and downstream copy tasks.

        Raises
        ------
        FrameDecodingError
            If any pre-ACK buffered chunk is not a ``bytes`` instance —
            indicates a protocol-layer bug where the frame decoder passed a
            malformed payload before the connection was acknowledged.
        """
        if self._started:
            logger.debug(
                "conn %s: start called more than once; ignoring",
                self._id,
                extra={"conn_id": self._id},
            )
            return
        self._started = True

        if self._pre_ack_buffer:
            for chunk in self._pre_ack_buffer:
                if not isinstance(chunk, bytes):
                    raise FrameDecodingError(
                        f"conn {self._id!r}: pre-ACK buffer contains a non-bytes "
                        f"chunk; frame decoder produced a malformed payload.",
                        error_code="protocol.pre_ack_bad_type",
                        details={
                            "conn_id": self._id,
                            "received_type": type(chunk).__name__,
                        },
                        hint="Ensure the frame decoder always passes raw bytes to feed().",
                    )
                try:
                    self._inbound.put_nowait(chunk)
                except asyncio.QueueFull:
                    # Queue is full — write directly to the transport instead
                    # of dropping.  _downstream has not started yet so there is
                    # no concurrent reader; this is safe.
                    self._drop_count += 1
                    metrics_inc("connection.inbound_queue.drop")
                    logger.debug(
                        "conn %s: pre-ACK queue full during flush, writing %d bytes directly",
                        self._id,
                        len(chunk),
                        extra={"conn_id": self._id},
                    )
                    with contextlib.suppress(OSError):
                        self._writer.write(chunk)
            self._pre_ack_buffer.clear()
            self._pre_ack_buffer_bytes = 0

        self._upstream_task = asyncio.create_task(
            self._upstream(), name=f"conn-up-{self._id}"
        )
        self._downstream_task = asyncio.create_task(
            self._downstream(), name=f"conn-down-{self._id}"
        )
        self._tasks = [self._upstream_task, self._downstream_task]
        for task in self._tasks:
            task.add_done_callback(self._on_task_done)

    # ── Called by recv_loop ───────────────────────────────────────────────────

    def feed(self, data: bytes) -> bool:
        """Enqueue data received from the agent for the downstream task.

        After ACK, use :meth:`feed_async` to apply proper backpressure.

        Raises
        ------
        FrameDecodingError
            If *data* is not a ``bytes`` instance.
        """
        if not isinstance(data, bytes):
            raise FrameDecodingError(
                f"conn {self._id!r}: feed() received a non-bytes payload.",
                error_code="protocol.tcp_feed_bad_type",
                details={
                    "conn_id": self._id,
                    "received_type": type(data).__name__,
                },
                hint="Ensure the frame decoder passes raw bytes to feed().",
            )
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
        # Post-ACK: caller should use feed_async for backpressure.
        # Fall back to put_nowait only if queue has space (e.g. replay path).
        try:
            self._inbound.put_nowait(data)
            return True
        except asyncio.QueueFull:
            return False

    async def feed_async(self, data: bytes) -> bool:
        """Await space in the inbound queue, applying backpressure to the WS reader.

        Returns ``False`` if the connection was closed before data could be
        enqueued.  Re-checks ``_closed`` after the await so that data is never
        enqueued *after* cleanup has already run.  Byte accounting is done in
        ``_downstream`` when each item is dequeued.

        We intentionally do NOT roll back the enqueued item when
        ``_close_remote_requested`` is set after the put().  The previous
        rollback via ``get_nowait()`` had a TOCTOU race: if ``_downstream``
        had already exited its drain loop by the time the rollback ran, the
        item was removed from the queue but never written to the local socket,
        silently truncating the stream.  Instead we leave the item in the
        queue and let ``_downstream`` drain it before honouring the close flag
        — the drain-then-close ordering in ``_downstream`` guarantees every
        byte the agent sent is delivered before EOF is written.

        Raises
        ------
        FrameDecodingError
            If *data* is not a ``bytes`` instance.
        TransportError
            If an unexpected error occurs while awaiting queue space (e.g.
            event-loop shutdown or task cancellation outside of normal flow).
        """
        if not isinstance(data, bytes):
            raise FrameDecodingError(
                f"conn {self._id!r}: feed_async() received a non-bytes payload.",
                error_code="protocol.tcp_feed_async_bad_type",
                details={
                    "conn_id": self._id,
                    "received_type": type(data).__name__,
                },
                hint="Ensure the frame decoder passes raw bytes to feed_async().",
            )
        if self._closed.is_set():
            return False
        try:
            await self._inbound.put(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise TransportError(
                f"conn {self._id!r}: unexpected error while awaiting inbound queue space.",
                error_code="transport.tcp_feed_async_failed",
                details={"conn_id": self._id},
                hint="This may indicate an event-loop shutdown or abnormal task cancellation.",
            ) from exc
        # If _cleanup() already ran while we were suspended on put(), the
        # connection is fully torn down — signal the caller to stop feeding.
        return not self._closed.is_set()

    def close_remote(self) -> None:
        """Signal that the agent has closed its side.

        Sets ``_close_remote_requested`` and wakes ``_downstream`` via
        ``_close_event`` instead of evicting a real data chunk to make room
        for a ``None`` sentinel.  ``_downstream`` drains all queued data first,
        then honours the flag — preserving every byte the agent sent.
        """
        if self._closed.is_set():
            return
        self._close_remote_requested = True
        self._close_event.set()

    # ── Copy tasks ────────────────────────────────────────────────────────────

    async def _upstream(self) -> None:
        """local TCP → WSS DATA frames.

        Exception handling strategy
        ---------------------------
        ``asyncio.CancelledError``
            Re-raised immediately — cancellation is always intentional here.
        ``WebSocketSendTimeoutError``
            The tunnel send queue stalled; log at WARNING with ``error_id`` for
            correlation, then let the task exit so ``_on_task_done`` triggers
            cleanup.
        ``ConnectionClosedError``
            The WebSocket connection dropped mid-stream; log at WARNING.
        ``TransportError``
            Any other structured transport failure; log at WARNING with
            ``error_code`` and ``error_id``.
        ``OSError``
            Local socket error (client disconnected, broken pipe, etc.);
            log at DEBUG — these are routine.
        ``ExecTunnelError``
            Catch-all for any other library error; log at WARNING.
        ``Exception``
            Truly unexpected errors; log at WARNING with full traceback.
        """
        with span("connection.upstream"):
            start = asyncio.get_running_loop().time()
            metrics_inc("connection.upstream.started")
            try:
                while True:
                    chunk = await self._reader.read(PIPE_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    self._bytes_upstream += len(chunk)
                    b64 = base64.b64encode(chunk).decode()
                    await self._ws_send(
                        encode_frame("DATA", self._id, b64),
                        must_queue=True,
                    )
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
                metrics_inc("connection.upstream.error", error=exc.error_code.replace(".", "_"))
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
                metrics_inc("connection.upstream.error", error="OSError")
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
                metrics_inc("connection.upstream.error", error=exc.error_code.replace(".", "_"))
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
                metrics_inc("connection.upstream.error", error=exc.__class__.__name__)
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
                metrics_observe(
                    "connection.upstream.duration_sec",
                    asyncio.get_running_loop().time() - start,
                )
                metrics_observe("connection.upstream.bytes", float(self._bytes_upstream))
                await self._send_close_frame_once()

    async def _downstream(self) -> None:
        """Inbound queue → local TCP.

        Exception handling strategy
        ---------------------------
        ``asyncio.CancelledError``
            Re-raised immediately — cancellation is always intentional here.
        ``ConnectionClosedError``
            The WebSocket connection dropped while waiting for data; the
            downstream task exits so ``_on_task_done`` triggers cleanup.
        ``TransportError``
            Any other structured transport failure from ``recv``-path helpers.
        ``OSError``
            Local socket error (client disconnected, broken pipe, etc.);
            log at DEBUG — these are routine.
        ``ExecTunnelError``
            Catch-all for any other library error; log at WARNING.
        ``Exception``
            Truly unexpected errors; log at WARNING with full traceback.
        """
        with span("connection.downstream"):
            start = asyncio.get_running_loop().time()
            metrics_inc("connection.downstream.started")
            try:
                while True:
                    # ── Wait for data or close signal ─────────────────────────
                    if self._inbound.empty():
                        if self._close_remote_requested:
                            # Queue is empty and remote has closed — we are done.
                            if self._writer.can_write_eof():
                                with contextlib.suppress(OSError):
                                    self._writer.write_eof()
                                    await self._writer.drain()
                                self._downstream_ended_cleanly = True
                            break

                        # Block until data arrives OR close_remote() fires.
                        data_task: asyncio.Task[bytes | None] = asyncio.create_task(
                            self._inbound.get()
                        )
                        close_task: asyncio.Task[None] = asyncio.create_task(
                            self._close_event.wait()
                        )
                        done, pending = await asyncio.wait(
                            {data_task, close_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for t in pending:
                            t.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await t

                        if data_task in done:
                            item = data_task.result()
                        else:
                            # close_event fired; rescue any item that data_task
                            # may have already dequeued before being cancelled —
                            # dropping it would truncate SSH sessions or corrupt
                            # SCP transfers at close time.
                            if not data_task.cancelled():
                                with contextlib.suppress(Exception):
                                    item = data_task.result()
                                    if item is not None:
                                        self._bytes_downstream += len(item)
                                        self._writer.write(item)
                                        await self._writer.drain()
                            # Loop back to drain any remaining items that arrived
                            # before the flag was set.
                            continue
                    else:
                        item = self._inbound.get_nowait()

                    # ── Write item to local socket ────────────────────────────
                    if item is None:
                        # Legacy sentinel path (pre-flag callers).
                        if self._writer.can_write_eof():
                            with contextlib.suppress(OSError):
                                self._writer.write_eof()
                                await self._writer.drain()
                            self._downstream_ended_cleanly = True
                        break

                    self._bytes_downstream += len(item)
                    self._writer.write(item)
                    await self._writer.drain()

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
                metrics_inc("connection.downstream.error", error="OSError")
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
                metrics_inc("connection.downstream.error", error=exc.__class__.__name__)
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
                metrics_observe(
                    "connection.downstream.duration_sec",
                    asyncio.get_running_loop().time() - start,
                )
                metrics_observe("connection.downstream.bytes", float(self._bytes_downstream))

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _send_close_frame_once(self) -> None:
        """Send ``CONN_CLOSE`` exactly once.

        Raises
        ------
        WebSocketSendTimeoutError
            Logged at WARNING and suppressed — the agent will time-out the
            connection independently; we must not block cleanup.
        ConnectionClosedError
            Logged at DEBUG and suppressed — the connection is already gone.
        TransportError
            Logged at DEBUG and suppressed — teardown must always complete.
        """
        if self._conn_close_sent:
            return
        self._conn_close_sent = True
        try:
            await self._ws_send(encode_frame("CONN_CLOSE", self._id), control=True)
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
            # Connection already gone — CONN_CLOSE is moot.
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
            # Non-library error — log and suppress so cleanup always completes.
            logger.debug(
                "conn %s: failed to send CONN_CLOSE: %s",
                self._id,
                exc,
                exc_info=True,
                extra={"conn_id": self._id},
            )

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        """Called when either copy task exits; cancel the other and clean up."""
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                # Emit error_code and error_id when available for log correlation.
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

        # Either direction ending cleanly is a valid half-close: keep the peer
        # alive to avoid truncating in-flight data (e.g. TLS responses or
        # pipelined requests from download managers).
        should_cancel_peer = True
        if not task.cancelled() and task.exception() is None:
            if task is self._upstream_task:
                should_cancel_peer = not self._upstream_ended_cleanly
            elif task is self._downstream_task:
                should_cancel_peer = not self._downstream_ended_cleanly

        if should_cancel_peer:
            for other in self._tasks:
                if other is not task and not other.done():
                    other.cancel()

        if any(not t.done() for t in self._tasks):
            return

        # Schedule cleanup; don't block the callback.
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(
                self._cleanup(), name=f"conn-cleanup-{self._id}"
            )

    async def _cleanup(self) -> None:
        """Await all tasks, remove from registry, close the local writer."""
        if self._closed.is_set():
            return
        self._closed.set()
        metrics_inc("connection.cleanup")

        for task in self._tasks:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError):
                await task

        self._registry.pop(self._id, None)
        with contextlib.suppress(OSError):
            self._writer.close()
            await self._writer.wait_closed()

    # ── Public control ────────────────────────────────────────────────────────

    def cancel_upstream(self) -> None:
        """Cancel the upstream task so no more DATA frames are sent to the agent.

        Used when the agent signals an ERROR — the downstream (agent→local) path
        is closed via :meth:`close_remote`, and this stops the local→agent
        direction so we don't keep sending DATA frames to a connection the agent
        has already torn down.
        """
        if self._upstream_task and not self._upstream_task.done():
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
        """Total bytes forwarded from local TCP to the tunnel (upstream direction)."""
        return self._bytes_upstream

    @property
    def bytes_downstream(self) -> int:
        """Total bytes forwarded from the tunnel to local TCP (downstream direction)."""
        return self._bytes_downstream

    @property
    def drop_count(self) -> int:
        """Total inbound chunks dropped due to queue saturation or buffer overflow."""
        return self._drop_count

    @property
    def is_started(self) -> bool:
        """``True`` once :meth:`start` has been called successfully."""
        return self._started
