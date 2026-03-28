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

    def start(self) -> None:
        """Spawn the upstream and downstream copy tasks."""
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
                try:
                    self._inbound.put_nowait(chunk)
                except asyncio.QueueFull:
                    self._drop_count += 1
                    metrics_inc("connection.inbound_queue.drop")
                    break
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
        """Enqueue data received from the agent for the downstream task (pre-ACK only).

        After ACK, use ``feed_async`` to apply proper backpressure.
        """
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

        Returns False if the connection was closed before data could be enqueued.
        Byte accounting is done in ``_downstream`` when each item is dequeued.
        """
        if self._closed.is_set():
            return False
        await self._inbound.put(data)
        return True

    def close_remote(self) -> None:
        """
        Signal that the agent has closed its side.

        Delivers a ``None`` sentinel to the inbound queue.  If the queue is
        full, one buffered item is discarded to guarantee delivery.
        """
        if self._closed.is_set():
            return
        if self._inbound.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._inbound.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._inbound.put_nowait(None)

    # ── Copy tasks ────────────────────────────────────────────────────────────

    async def _upstream(self) -> None:
        """local TCP → WSS DATA frames."""
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
            except TimeoutError as exc:
                metrics_inc("connection.upstream.error", error="TimeoutError")
                logger.warning(
                    "conn %s: upstream send timed out (backpressure): %s",
                    self._id,
                    exc,
                    extra={
                        "conn_id": self._id,
                        "direction": "upstream",
                        "bytes_sent": self._bytes_upstream,
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
            except Exception as exc:
                metrics_inc("connection.upstream.error", error=exc.__class__.__name__)
                logger.warning(
                    "conn %s: upstream failure: %s",
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
        """Inbound queue → local TCP."""
        with span("connection.downstream"):
            start = asyncio.get_running_loop().time()
            metrics_inc("connection.downstream.started")
            try:
                while True:
                    item = await self._inbound.get()
                    if item is None:  # sentinel — agent closed its side
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
            except Exception as exc:
                metrics_inc("connection.downstream.error", error=exc.__class__.__name__)
                logger.warning(
                    "conn %s: downstream failure: %s",
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

    async def _send_close_frame_once(self) -> None:
        """Send CONN_CLOSE exactly once. Renamed from ``_send_conn_close_once``."""
        if self._conn_close_sent:
            return
        self._conn_close_sent = True
        try:
            await self._ws_send(encode_frame("CONN_CLOSE", self._id), control=True)
        except Exception as exc:
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

    def cancel_upstream(self) -> None:
        """Cancel the upstream task so no more DATA frames are sent to the agent.

        Used when the agent signals an ERROR — the downstream (agent→local) path
        is closed via ``close_remote()``, and this stops the local→agent direction
        so we don't keep sending DATA frames to a connection the agent has already
        torn down.
        """
        if self._upstream_task and not self._upstream_task.done():
            self._upstream_task.cancel()

    @property
    def closed(self) -> asyncio.Event:
        return self._closed

    @property
    def conn_id(self) -> str:
        return self._id


