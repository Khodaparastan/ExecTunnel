"""Tests for ``exectunnel.transport.tcp.TcpConnection``."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from exectunnel.exceptions import (
    ConnectionClosedError,
    TransportError,
    WebSocketSendTimeoutError,
)
from exectunnel.transport import TcpConnection

from ._helpers import TCP_CONN_ID, make_mock_writer, make_reader


def _make_eof_reader() -> MagicMock:
    reader = MagicMock(spec=asyncio.StreamReader)
    reader.read = AsyncMock(return_value=b"")
    return reader


# ── Construction ──────────────────────────────────────────────────────────────


class TestConstruction:
    def test_initial_flags(self, make_tcp_conn):
        conn = make_tcp_conn()
        assert not conn.is_started
        assert not conn.is_closed
        assert not conn.is_remote_closed
        assert conn.bytes_upstream == 0
        assert conn.bytes_downstream == 0
        assert conn.drop_count == 0

    def test_conn_id_property(self, make_tcp_conn):
        conn = make_tcp_conn()
        assert conn.conn_id == TCP_CONN_ID

    def test_pre_ack_cap_clamped_to_minimum(self, ws_send, tcp_writer, tcp_registry):
        from exectunnel.defaults import Defaults

        conn = TcpConnection(
            TCP_CONN_ID,
            _make_eof_reader(),
            tcp_writer,
            ws_send,
            tcp_registry,
            pre_ack_buffer_cap_bytes=1,
        )
        assert conn._pre_ack_buffer_cap_bytes == Defaults.PIPE_READ_CHUNK_BYTES

    def test_pre_ack_cap_accepts_large_value(self, ws_send, tcp_writer, tcp_registry):
        conn = TcpConnection(
            TCP_CONN_ID,
            _make_eof_reader(),
            tcp_writer,
            ws_send,
            tcp_registry,
            pre_ack_buffer_cap_bytes=1_000_000,
        )
        assert conn._pre_ack_buffer_cap_bytes == 1_000_000


# ── start() ───────────────────────────────────────────────────────────────────


class TestStart:
    async def test_creates_both_tasks(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn.on_remote_closed()
        conn.start()
        assert conn._upstream_task is not None
        assert conn._downstream_task is not None
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)

    async def test_sets_started_flag(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn.on_remote_closed()
        conn.start()
        assert conn.is_started
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)

    async def test_idempotent_second_call_is_noop(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn.on_remote_closed()
        conn.start()
        task_before = conn._upstream_task
        conn.start()
        assert conn._upstream_task is task_before
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)

    def test_raises_if_already_closed(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn._closed.set()
        with pytest.raises(TransportError) as exc_info:
            conn.start()
        assert exc_info.value.error_code == "transport.start_on_closed"

    async def test_flushes_pre_ack_buffer_into_inbound_queue(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn.feed(b"pre-ack chunk")
        assert conn._pre_ack_buffer_bytes > 0
        conn.on_remote_closed()
        conn.start()
        # Assert before yielding — tasks have not yet consumed the queue.
        assert conn._pre_ack_buffer == []
        assert conn._pre_ack_buffer_bytes == 0
        assert not conn._inbound.empty()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)

    async def test_pre_ack_flush_queue_full_raises_and_schedules_cleanup(
        self, make_tcp_conn
    ):
        from exectunnel.defaults import Defaults

        conn = make_tcp_conn()
        conn.feed(b"pre-ack chunk")
        for _ in range(Defaults.TCP_INBOUND_QUEUE_CAP):
            conn._inbound.put_nowait(b"x")
        conn.on_remote_closed()
        with pytest.raises(TransportError) as exc_info:
            conn.start()
        assert exc_info.value.error_code == "transport.pre_ack_flush_overflow"
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)

    async def test_remote_closed_before_start_exits_cleanly(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn.on_remote_closed()
        conn.start()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        assert conn.is_closed


# ── feed() ────────────────────────────────────────────────────────────────────


class TestFeed:
    def test_invalid_type_raises(self, make_tcp_conn):
        conn = make_tcp_conn()
        with pytest.raises(TransportError) as exc_info:
            conn.feed("not bytes")
        assert exc_info.value.error_code == "transport.invalid_payload_type"

    def test_noop_when_closed(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn._closed.set()
        conn.feed(b"data")

    def test_pre_ack_accumulates_chunks(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn.feed(b"aaa")
        conn.feed(b"bb")
        assert len(conn._pre_ack_buffer) == 2
        assert conn._pre_ack_buffer_bytes == 5

    async def test_pre_ack_overflow_raises_and_schedules_cleanup(
        self, ws_send, tcp_writer, tcp_registry
    ):
        from exectunnel.defaults import Defaults

        cap = Defaults.PIPE_READ_CHUNK_BYTES
        conn = TcpConnection(
            TCP_CONN_ID,
            _make_eof_reader(),
            tcp_writer,
            ws_send,
            tcp_registry,
            pre_ack_buffer_cap_bytes=cap,
        )
        with pytest.raises(TransportError) as exc_info:
            conn.feed(b"x" * (cap + 1))
        assert exc_info.value.error_code == "transport.pre_ack_buffer_overflow"
        assert conn._cleanup_task is not None
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)

    def test_post_ack_enqueues_data(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn._started = True
        conn.feed(b"hello")
        assert conn._inbound.qsize() == 1

    async def test_post_ack_queue_full_raises_and_increments_drop_count(
        self, make_tcp_conn
    ):
        """``feed()`` must reject post-ACK overflow and tear down cleanly.

        The overflow path schedules ``_cleanup`` via ``asyncio.create_task``
        — the test must therefore run under an event loop, and must drain
        the resulting cleanup task before returning so it does not leak
        as an unawaited-coroutine ``RuntimeWarning`` flushed during a
        later test.
        """
        from exectunnel.defaults import Defaults

        conn = make_tcp_conn()
        conn._started = True
        for _ in range(Defaults.TCP_INBOUND_QUEUE_CAP):
            conn._inbound.put_nowait(b"x")
        with pytest.raises(TransportError) as exc_info:
            conn.feed(b"overflow")
        assert exc_info.value.error_code == "transport.inbound_queue_full"
        assert conn.drop_count == 1
        # Drain the cleanup task scheduled by the overflow path so it
        # does not leak as an unawaited-coroutine warning.
        if conn._cleanup_task is not None:
            await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)


# ── feed_async() ──────────────────────────────────────────────────────────────


class TestFeedAsync:
    async def test_invalid_type_raises(self, make_tcp_conn):
        conn = make_tcp_conn()
        with pytest.raises(TransportError) as exc_info:
            await conn.feed_async("not bytes")
        assert exc_info.value.error_code == "transport.invalid_payload_type"

    async def test_raises_if_already_closed(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn._closed.set()
        with pytest.raises(ConnectionClosedError) as exc_info:
            await conn.feed_async(b"data")
        assert exc_info.value.error_code == "transport.feed_async_on_closed"

    async def test_enqueues_data_when_queue_has_space(self, make_tcp_conn):
        conn = make_tcp_conn()
        await conn.feed_async(b"hello")
        assert conn._inbound.qsize() == 1

    async def test_raises_when_connection_closes_during_enqueue(self, make_tcp_conn):
        from exectunnel.defaults import Defaults

        conn = make_tcp_conn()
        for _ in range(Defaults.TCP_INBOUND_QUEUE_CAP):
            conn._inbound.put_nowait(b"x")

        async def _close_soon() -> None:
            await asyncio.sleep(0)
            conn._closed.set()

        asyncio.create_task(_close_soon())
        with pytest.raises(ConnectionClosedError) as exc_info:
            await conn.feed_async(b"blocked")
        assert exc_info.value.error_code == "transport.feed_async_closed_during_enqueue"

    async def test_cancelled_error_propagates(self, make_tcp_conn):
        from exectunnel.defaults import Defaults

        conn = make_tcp_conn()
        for _ in range(Defaults.TCP_INBOUND_QUEUE_CAP):
            conn._inbound.put_nowait(b"x")
        task = asyncio.create_task(conn.feed_async(b"blocked"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ── close_unstarted() ─────────────────────────────────────────────────────────


class TestCloseUnstarted:
    async def test_closes_writer(self, make_tcp_conn, tcp_writer):
        conn = make_tcp_conn()
        await conn.close_unstarted()
        tcp_writer.close.assert_called_once()

    async def test_sets_closed_flag(self, make_tcp_conn):
        conn = make_tcp_conn()
        await conn.close_unstarted()
        assert conn.is_closed

    async def test_idempotent(self, make_tcp_conn):
        conn = make_tcp_conn()
        await conn.close_unstarted()
        await conn.close_unstarted()
        assert conn.is_closed

    async def test_raises_runtime_error_if_started(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn._started = True
        with pytest.raises(RuntimeError, match="close_unstarted"):
            await conn.close_unstarted()


# ── on_remote_closed() ────────────────────────────────────────────────────────


class TestOnRemoteClosed:
    def test_sets_remote_closed_event(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn.on_remote_closed()
        assert conn.is_remote_closed

    def test_noop_when_already_closed(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn._closed.set()
        conn.on_remote_closed()
        assert not conn.is_remote_closed

    def test_idempotent_second_call(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn.on_remote_closed()
        conn.on_remote_closed()
        assert conn.is_remote_closed


# ── abort() / abort_upstream() / abort_downstream() ──────────────────────────


class TestAbort:
    async def test_abort_noop_with_no_tasks(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn.abort()

    async def test_abort_cancels_both_tasks(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn._started = True
        up = MagicMock(spec=asyncio.Task)
        up.done.return_value = False
        down = MagicMock(spec=asyncio.Task)
        down.done.return_value = False
        conn._upstream_task = up
        conn._downstream_task = down
        conn.abort()
        up.cancel.assert_called_once()
        down.cancel.assert_called_once()

    async def test_abort_skips_done_tasks(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn._started = True
        up = MagicMock(spec=asyncio.Task)
        up.done.return_value = True
        conn._upstream_task = up
        conn.abort()
        up.cancel.assert_not_called()

    async def test_abort_upstream_cancels_only_upstream(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn._started = True
        up = MagicMock(spec=asyncio.Task)
        up.done.return_value = False
        down = MagicMock(spec=asyncio.Task)
        down.done.return_value = False
        conn._upstream_task = up
        conn._downstream_task = down
        conn.abort_upstream()
        up.cancel.assert_called_once()
        down.cancel.assert_not_called()

    async def test_abort_downstream_cancels_only_downstream(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn._started = True
        up = MagicMock(spec=asyncio.Task)
        up.done.return_value = False
        down = MagicMock(spec=asyncio.Task)
        down.done.return_value = False
        conn._upstream_task = up
        conn._downstream_task = down
        conn.abort_downstream()
        down.cancel.assert_called_once()
        up.cancel.assert_not_called()

    async def test_abort_upstream_noop_when_task_is_none(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn.abort_upstream()

    async def test_abort_downstream_noop_when_task_is_none(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn.abort_downstream()

    async def test_abort_before_start_schedules_cleanup(self, make_tcp_conn):
        conn = make_tcp_conn()
        assert not conn._started
        conn.abort()
        assert conn._cleanup_task is not None
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        assert conn.is_closed


# ── _upstream task ────────────────────────────────────────────────────────────


class TestUpstreamTask:
    async def test_sends_data_frame_for_each_read_chunk(
        self, ws_send, tcp_writer, tcp_registry
    ):
        reader = make_reader(b"hello", b"world")
        conn = TcpConnection(TCP_CONN_ID, reader, tcp_writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        conn.on_remote_closed()
        conn.start()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        assert ws_send.has_frame_type("DATA")

    async def test_sends_conn_close_on_clean_eof(
        self, ws_send, tcp_writer, tcp_registry
    ):
        reader = make_reader(b"data")
        conn = TcpConnection(TCP_CONN_ID, reader, tcp_writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        conn.on_remote_closed()
        conn.start()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        assert ws_send.has_frame_type("CONN_CLOSE")

    async def test_cleanup_sends_conn_close_when_upstream_cancelled(
        self, ws_send, tcp_writer, tcp_registry
    ):
        reader = asyncio.StreamReader()  # Never feeds — blocks read()
        conn = TcpConnection(TCP_CONN_ID, reader, tcp_writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        conn.start()
        await asyncio.sleep(0)
        conn.abort()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        assert ws_send.has_frame_type("CONN_CLOSE")

    async def test_ws_send_timeout_exits_cleanly(
        self, ws_send, tcp_writer, tcp_registry
    ):
        ws_send.side_effect = WebSocketSendTimeoutError(
            "timed out", error_code="ws.timeout"
        )
        reader = make_reader(b"data")
        conn = TcpConnection(TCP_CONN_ID, reader, tcp_writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        conn.on_remote_closed()
        conn.start()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        assert conn.is_closed

    async def test_connection_closed_error_exits_cleanly(
        self, ws_send, tcp_writer, tcp_registry
    ):
        ws_send.side_effect = ConnectionClosedError(
            "closed", error_code="transport.closed"
        )
        reader = make_reader(b"data")
        conn = TcpConnection(TCP_CONN_ID, reader, tcp_writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        conn.on_remote_closed()
        conn.start()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        assert conn.is_closed

    async def test_os_error_during_read_exits_cleanly(
        self, ws_send, tcp_writer, tcp_registry
    ):
        reader = MagicMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(side_effect=OSError("connection reset"))
        conn = TcpConnection(TCP_CONN_ID, reader, tcp_writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        conn.on_remote_closed()
        conn.start()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        assert conn.is_closed

    async def test_bytes_upstream_counted_after_successful_send(
        self, ws_send, tcp_writer, tcp_registry
    ):
        data = b"hello world"
        reader = make_reader(data)
        conn = TcpConnection(TCP_CONN_ID, reader, tcp_writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        conn.on_remote_closed()
        conn.start()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        assert conn.bytes_upstream == len(data)

    async def test_bytes_upstream_not_counted_on_send_failure(
        self, ws_send, tcp_writer, tcp_registry
    ):
        ws_send.side_effect = ConnectionClosedError(
            "closed", error_code="transport.closed"
        )
        reader = make_reader(b"data")
        conn = TcpConnection(TCP_CONN_ID, reader, tcp_writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        conn.on_remote_closed()
        conn.start()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        assert conn.bytes_upstream == 0

    async def test_half_close_keeps_downstream_alive(
        self, ws_send, tcp_writer, tcp_registry
    ):
        reader = make_reader(eof=True)
        conn = TcpConnection(TCP_CONN_ID, reader, tcp_writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        conn.start()

        done, _ = await asyncio.wait({conn._upstream_task}, timeout=1.0)
        assert conn._upstream_task in done
        assert conn._upstream_ended_cleanly
        assert not conn._downstream_task.done()

        conn.feed(b"server response")
        conn.on_remote_closed()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)

        tcp_writer.write.assert_called_with(b"server response")

    async def test_error_in_upstream_cancels_downstream(
        self, ws_send, tcp_writer, tcp_registry
    ):
        ws_send.side_effect = RuntimeError("unexpected")
        reader = make_reader(b"data")
        conn = TcpConnection(TCP_CONN_ID, reader, tcp_writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        conn.on_remote_closed()
        conn.start()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        assert conn._downstream_task.done()


# ── _downstream task ──────────────────────────────────────────────────────────


class TestDownstreamTask:
    async def test_writes_queued_data_to_local_socket(
        self, ws_send, tcp_writer, tcp_registry
    ):
        reader = make_reader(eof=True)
        conn = TcpConnection(TCP_CONN_ID, reader, tcp_writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        conn.feed(b"agent payload")
        conn.on_remote_closed()
        conn.start()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        tcp_writer.write.assert_called_with(b"agent payload")

    async def test_writes_eof_after_draining_queue(
        self, ws_send, tcp_writer, tcp_registry
    ):
        reader = make_reader(eof=True)
        conn = TcpConnection(TCP_CONN_ID, reader, tcp_writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        conn.on_remote_closed()
        conn.start()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        tcp_writer.write_eof.assert_called_once()

    async def test_skips_eof_when_can_write_eof_false(self, ws_send, tcp_registry):
        writer = make_mock_writer()
        writer.can_write_eof.return_value = False
        reader = make_reader(eof=True)
        conn = TcpConnection(TCP_CONN_ID, reader, writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        conn.on_remote_closed()
        conn.start()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        writer.write_eof.assert_not_called()

    async def test_os_error_on_drain_causes_early_return(self, ws_send, tcp_registry):
        writer = make_mock_writer()
        writer.drain.side_effect = OSError("broken pipe")
        reader = make_reader(eof=True)
        conn = TcpConnection(TCP_CONN_ID, reader, writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        conn.feed(b"data")
        conn.on_remote_closed()
        conn.start()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        assert conn.is_closed

    async def test_os_error_on_write_eof_causes_early_return(
        self, ws_send, tcp_registry
    ):
        writer = make_mock_writer()
        writer.write_eof.side_effect = OSError("broken pipe")
        reader = make_reader(eof=True)
        conn = TcpConnection(TCP_CONN_ID, reader, writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        conn.on_remote_closed()
        conn.start()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        assert conn.is_closed

    async def test_bytes_downstream_tracked(self, ws_send, tcp_registry):
        writer = make_mock_writer()
        data = b"payload from agent"
        reader = make_reader(eof=True)
        conn = TcpConnection(TCP_CONN_ID, reader, writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        conn.feed(data)
        conn.on_remote_closed()
        conn.start()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        assert conn.bytes_downstream == len(data)

    async def test_batch_drain_handles_more_than_batch_size_chunks(
        self, ws_send, tcp_registry
    ):
        writer = make_mock_writer()
        reader = make_reader(eof=True)
        conn = TcpConnection(TCP_CONN_ID, reader, writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        for i in range(20):
            conn.feed(f"chunk{i}".encode())
        conn.on_remote_closed()
        conn.start()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        assert writer.write.call_count == 20

    async def test_blocking_wait_delivers_late_fed_data(self, ws_send, tcp_registry):
        """Data fed after start() is delivered through the blocking-wait branch."""
        writer = make_mock_writer()
        reader = make_reader(eof=True)
        conn = TcpConnection(TCP_CONN_ID, reader, writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        conn.start()

        done, _ = await asyncio.wait({conn._upstream_task}, timeout=1.0)
        assert conn._upstream_task in done

        conn.feed(b"late data")
        conn.on_remote_closed()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        writer.write.assert_called_with(b"late data")

    async def test_downstream_exit_cancels_upstream(self, ws_send, tcp_registry):
        writer = make_mock_writer()
        writer.drain.side_effect = OSError("dead socket")
        blocking_reader = asyncio.StreamReader()  # Never sends EOF
        conn = TcpConnection(
            TCP_CONN_ID, blocking_reader, writer, ws_send, tcp_registry
        )
        tcp_registry[TCP_CONN_ID] = conn
        conn.feed(b"data")
        conn.on_remote_closed()
        conn.start()
        await asyncio.wait_for(conn.closed_event.wait(), timeout=2.0)
        assert conn._upstream_task.cancelled()


# ── _send_close_frame_once() ─────────────────────────────────────────────────


class TestSendCloseFrameOnce:
    async def test_sends_exactly_one_conn_close(self, make_tcp_conn, ws_send):
        conn = make_tcp_conn()
        await conn._send_close_frame_once()
        await conn._send_close_frame_once()
        assert len(ws_send.frames_of_type("CONN_CLOSE")) == 1

    async def test_ws_send_timeout_does_not_propagate(
        self, ws_send, tcp_writer, tcp_registry
    ):
        ws_send.side_effect = WebSocketSendTimeoutError(
            "timeout", error_code="ws.timeout"
        )
        conn = TcpConnection(
            TCP_CONN_ID, make_reader(), tcp_writer, ws_send, tcp_registry
        )
        await conn._send_close_frame_once()

    async def test_connection_closed_error_does_not_propagate(
        self, ws_send, tcp_writer, tcp_registry
    ):
        ws_send.side_effect = ConnectionClosedError(
            "closed", error_code="transport.closed"
        )
        conn = TcpConnection(
            TCP_CONN_ID, make_reader(), tcp_writer, ws_send, tcp_registry
        )
        await conn._send_close_frame_once()

    async def test_unexpected_exception_does_not_propagate(
        self, ws_send, tcp_writer, tcp_registry
    ):
        ws_send.side_effect = RuntimeError("unexpected")
        conn = TcpConnection(
            TCP_CONN_ID, make_reader(), tcp_writer, ws_send, tcp_registry
        )
        await conn._send_close_frame_once()


# ── _cleanup() ────────────────────────────────────────────────────────────────


class TestCleanup:
    async def test_evicts_from_registry(self, make_tcp_conn, tcp_registry):
        conn = make_tcp_conn()
        assert TCP_CONN_ID in tcp_registry
        await conn.close_unstarted()
        assert TCP_CONN_ID not in tcp_registry

    async def test_closes_writer(self, make_tcp_conn, tcp_writer):
        conn = make_tcp_conn()
        await conn.close_unstarted()
        tcp_writer.close.assert_called_once()

    async def test_idempotent(self, make_tcp_conn):
        conn = make_tcp_conn()
        await conn._cleanup()
        await conn._cleanup()
        assert conn.is_closed

    async def test_writer_wait_closed_timeout_suppressed(self, ws_send, tcp_registry):
        writer = make_mock_writer()
        writer.wait_closed = AsyncMock(side_effect=asyncio.TimeoutError)
        conn = TcpConnection(TCP_CONN_ID, make_reader(), writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        await conn.close_unstarted()
        assert conn.is_closed

    async def test_writer_close_os_error_suppressed(self, ws_send, tcp_registry):
        writer = make_mock_writer()
        writer.close.side_effect = OSError("already closed")
        conn = TcpConnection(TCP_CONN_ID, make_reader(), writer, ws_send, tcp_registry)
        tcp_registry[TCP_CONN_ID] = conn
        await conn.close_unstarted()
        assert conn.is_closed

    async def test_registry_not_in_dict_noop_no_raise(self, ws_send, tcp_writer):
        registry: dict = {}
        conn = TcpConnection(TCP_CONN_ID, make_reader(), tcp_writer, ws_send, registry)
        await conn._cleanup()
        assert conn.is_closed


# ── Properties and repr ───────────────────────────────────────────────────────


class TestPropertiesAndRepr:
    def test_repr_new_state(self, make_tcp_conn):
        conn = make_tcp_conn()
        r = repr(conn)
        assert "new" in r
        assert TCP_CONN_ID in r

    def test_repr_started_state(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn._started = True
        assert "started" in repr(conn)

    def test_repr_closed_state(self, make_tcp_conn):
        conn = make_tcp_conn()
        conn._closed.set()
        assert "closed" in repr(conn)

    def test_closed_event_returns_internal_event(self, make_tcp_conn):
        conn = make_tcp_conn()
        assert conn.closed_event is conn._closed
        assert isinstance(conn.closed_event, asyncio.Event)
