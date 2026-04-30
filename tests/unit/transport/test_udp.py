"""Tests for ``exectunnel.transport.udp.UdpFlow``."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from exectunnel.exceptions import (
    ConnectionClosedError,
    TransportError,
    WebSocketSendTimeoutError,
)
from exectunnel.transport import UdpFlow

from ._helpers import UDP_FLOW_ID

# ── Construction ──────────────────────────────────────────────────────────────


class TestConstruction:
    def test_initial_flags(self, make_udp_flow):
        flow = make_udp_flow()
        assert not flow.is_opened
        assert not flow.is_closed
        assert flow.drop_count == 0
        assert flow.bytes_sent == 0
        assert flow.bytes_recv == 0

    def test_flow_id_property(self, make_udp_flow):
        flow = make_udp_flow()
        assert flow.flow_id == UDP_FLOW_ID

    def test_repr_new_state(self, make_udp_flow):
        flow = make_udp_flow(host="10.0.0.1", port=80)
        r = repr(flow)
        assert "new" in r
        assert UDP_FLOW_ID in r
        assert "10.0.0.1:80" in r

    def test_repr_opened_state(self, make_udp_flow):
        flow = make_udp_flow()
        flow._opened = True
        assert "opened" in repr(flow)

    def test_repr_closed_state(self, make_udp_flow):
        flow = make_udp_flow()
        flow._closed = True
        assert "closed" in repr(flow)


# ── open() ────────────────────────────────────────────────────────────────────


class TestOpen:
    async def test_sends_udp_open_frame(self, make_udp_flow, ws_send):
        flow = make_udp_flow()
        await flow.open()
        assert ws_send.has_frame_type("UDP_OPEN")

    async def test_sets_opened_flag(self, make_udp_flow):
        flow = make_udp_flow()
        await flow.open()
        assert flow.is_opened

    async def test_idempotent_after_success(self, make_udp_flow, ws_send):
        flow = make_udp_flow()
        await flow.open()
        await flow.open()
        assert len(ws_send.frames_of_type("UDP_OPEN")) == 1

    async def test_raises_transport_error_if_already_closed(self, make_udp_flow):
        flow = make_udp_flow()
        flow._closed = True
        with pytest.raises(TransportError) as exc_info:
            await flow.open()
        assert exc_info.value.error_code == "transport.udp_open_on_closed"

    async def test_protocol_error_propagates_for_invalid_host(
        self, ws_send, udp_registry
    ):
        from exectunnel.exceptions import ProtocolError

        flow = UdpFlow(UDP_FLOW_ID, "bad:host", 80, ws_send, udp_registry)
        with pytest.raises(ProtocolError):
            await flow.open()
        assert not flow.is_opened

    async def test_opened_not_set_on_ws_send_timeout(self, ws_send, udp_registry):
        ws_send.side_effect = WebSocketSendTimeoutError(
            "timeout", error_code="ws.timeout"
        )
        flow = UdpFlow(UDP_FLOW_ID, "127.0.0.1", 53, ws_send, udp_registry)
        with pytest.raises(WebSocketSendTimeoutError):
            await flow.open()
        assert not flow.is_opened

    async def test_opened_not_set_on_connection_closed_error(
        self, ws_send, udp_registry
    ):
        ws_send.side_effect = ConnectionClosedError(
            "closed", error_code="transport.closed"
        )
        flow = UdpFlow(UDP_FLOW_ID, "127.0.0.1", 53, ws_send, udp_registry)
        with pytest.raises(ConnectionClosedError):
            await flow.open()
        assert not flow.is_opened

    async def test_generic_exception_wrapped_as_transport_error(
        self, ws_send, udp_registry
    ):
        ws_send.side_effect = RuntimeError("unexpected")
        flow = UdpFlow(UDP_FLOW_ID, "127.0.0.1", 53, ws_send, udp_registry)
        with pytest.raises(TransportError) as exc_info:
            await flow.open()
        assert exc_info.value.error_code == "transport.udp_open_failed"
        assert not flow.is_opened

    async def test_retry_succeeds_after_transient_failure(self, ws_send, udp_registry):
        call_count = 0

        async def flaky_send(
            frame: str, *, must_queue: bool = False, control: bool = False
        ) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionClosedError(
                    "first attempt", error_code="transport.closed"
                )
            ws_send.frames.append(frame)

        flow = UdpFlow(UDP_FLOW_ID, "127.0.0.1", 53, flaky_send, udp_registry)
        with pytest.raises(ConnectionClosedError):
            await flow.open()
        assert not flow.is_opened

        flow._ws_send = flaky_send
        await flow.open()
        assert flow.is_opened


# ── close() ───────────────────────────────────────────────────────────────────


class TestClose:
    async def test_sends_udp_close_when_opened(self, make_udp_flow, ws_send):
        flow = make_udp_flow()
        await flow.open()
        await flow.close()
        assert ws_send.has_frame_type("UDP_CLOSE")

    async def test_skips_udp_close_when_not_opened(self, make_udp_flow, ws_send):
        flow = make_udp_flow()
        await flow.close()
        assert not ws_send.has_frame_type("UDP_CLOSE")

    async def test_sets_closed_flag(self, make_udp_flow):
        flow = make_udp_flow()
        await flow.close()
        assert flow.is_closed

    async def test_sets_closed_event(self, make_udp_flow):
        flow = make_udp_flow()
        await flow.close()
        assert flow._closed_event.is_set()

    async def test_idempotent(self, make_udp_flow, ws_send):
        flow = make_udp_flow()
        await flow.open()
        await flow.close()
        await flow.close()
        assert len(ws_send.frames_of_type("UDP_CLOSE")) == 1

    async def test_evicts_from_registry(self, make_udp_flow, udp_registry):
        flow = make_udp_flow()
        assert UDP_FLOW_ID in udp_registry
        await flow.close()
        assert UDP_FLOW_ID not in udp_registry

    async def test_connection_closed_error_suppressed(self, ws_send, udp_registry):
        flow = UdpFlow(UDP_FLOW_ID, "127.0.0.1", 53, ws_send, udp_registry)
        flow._opened = True

        async def closing_send(
            frame: str, *, must_queue: bool = False, control: bool = False
        ) -> None:
            if ":UDP_CLOSE:" in frame:
                raise ConnectionClosedError("gone", error_code="transport.closed")

        flow._ws_send = closing_send
        await flow.close()
        assert flow.is_closed

    async def test_ws_send_timeout_propagates(self, ws_send, udp_registry):
        flow = UdpFlow(UDP_FLOW_ID, "127.0.0.1", 53, ws_send, udp_registry)
        flow._opened = True

        async def timing_out(
            frame: str, *, must_queue: bool = False, control: bool = False
        ) -> None:
            if ":UDP_CLOSE:" in frame:
                raise WebSocketSendTimeoutError("timeout", error_code="ws.timeout")

        flow._ws_send = timing_out
        with pytest.raises(WebSocketSendTimeoutError):
            await flow.close()

    async def test_generic_exception_wrapped_as_transport_error(
        self, ws_send, udp_registry
    ):
        flow = UdpFlow(UDP_FLOW_ID, "127.0.0.1", 53, ws_send, udp_registry)
        flow._opened = True

        async def bad_send(
            frame: str, *, must_queue: bool = False, control: bool = False
        ) -> None:
            raise RuntimeError("unexpected")

        flow._ws_send = bad_send
        with pytest.raises(TransportError) as exc_info:
            await flow.close()
        assert exc_info.value.error_code == "transport.udp_close_failed"


# ── on_remote_closed() ────────────────────────────────────────────────────────


class TestOnRemoteClosed:
    def test_sets_closed_flag(self, make_udp_flow):
        flow = make_udp_flow()
        flow.on_remote_closed()
        assert flow.is_closed

    def test_sets_closed_event(self, make_udp_flow):
        flow = make_udp_flow()
        flow.on_remote_closed()
        assert flow._closed_event.is_set()

    def test_evicts_from_registry(self, make_udp_flow, udp_registry):
        flow = make_udp_flow()
        assert UDP_FLOW_ID in udp_registry
        flow.on_remote_closed()
        assert UDP_FLOW_ID not in udp_registry

    def test_idempotent_second_call_does_not_re_evict(
        self, make_udp_flow, udp_registry
    ):
        flow = make_udp_flow()
        flow.on_remote_closed()
        flow.on_remote_closed()
        assert flow.is_closed

    def test_always_sets_event_even_when_already_closed(self, make_udp_flow):
        flow = make_udp_flow()
        flow._closed = True
        flow.on_remote_closed()
        assert flow._closed_event.is_set()

    def test_safe_before_open(self, make_udp_flow):
        flow = make_udp_flow()
        assert not flow.is_opened
        flow.on_remote_closed()
        assert flow.is_closed


# ── feed() ────────────────────────────────────────────────────────────────────


class TestFeed:
    def test_invalid_type_raises(self, make_udp_flow):
        flow = make_udp_flow()
        with pytest.raises(TransportError) as exc_info:
            flow.feed("not bytes")
        assert exc_info.value.error_code == "transport.invalid_payload_type"

    def test_accepted_datagram_enqueued(self, make_udp_flow):
        flow = make_udp_flow()
        flow.feed(b"datagram")
        assert flow._inbound.qsize() == 1

    def test_bytes_recv_tracked(self, make_udp_flow):
        flow = make_udp_flow()
        flow.feed(b"hello")
        flow.feed(b"world")
        assert flow.bytes_recv == 10

    def test_noop_when_closed(self, make_udp_flow):
        flow = make_udp_flow()
        flow._closed = True
        flow.feed(b"data")
        assert flow._inbound.empty()

    def test_queue_full_drops_and_increments_drop_count(self, make_udp_flow):
        from exectunnel.defaults import Defaults

        flow = make_udp_flow()
        for _ in range(Defaults.UDP_INBOUND_QUEUE_CAP):
            flow.feed(b"fill")
        flow.feed(b"overflow")
        assert flow.drop_count == 1

    def test_drop_warning_logged_on_first_drop(self, make_udp_flow):
        from exectunnel.defaults import Defaults

        flow = make_udp_flow()
        for _ in range(Defaults.UDP_INBOUND_QUEUE_CAP):
            flow.feed(b"fill")
        with patch("exectunnel.transport.udp._log") as mock_logger:
            flow.feed(b"overflow")
            mock_logger.warning.assert_called_once()

    def test_drop_warning_logged_at_warn_every_interval(self, make_udp_flow):
        from exectunnel.defaults import Defaults

        flow = make_udp_flow()
        for _ in range(Defaults.UDP_INBOUND_QUEUE_CAP):
            flow.feed(b"fill")

        # Trigger the first drop outside the mock so _drop_count reaches 1.
        # The first-drop warning is already covered by the test above.
        flow.feed(b"first overflow")
        assert flow.drop_count == 1

        with patch("exectunnel.transport.udp.Defaults") as mock_defaults:
            mock_defaults.UDP_WARN_EVERY = 3
            with patch("exectunnel.transport.udp._log") as mock_logger:
                for _ in range(6):
                    flow.feed(b"overflow")
                # _drop_count goes 2→3→4→5→6→7.
                # Warnings fire at count=3 (3%3==0) and count=6 (6%3==0).
                assert mock_logger.warning.call_count == 2

    def test_no_warning_between_intervals(self, make_udp_flow):
        from exectunnel.defaults import Defaults

        flow = make_udp_flow()
        for _ in range(Defaults.UDP_INBOUND_QUEUE_CAP):
            flow.feed(b"fill")

        with patch("exectunnel.transport.udp.Defaults") as mock_defaults:
            mock_defaults.UDP_WARN_EVERY = 10
            with patch("exectunnel.transport.udp._log") as mock_logger:
                for _ in range(5):
                    flow.feed(b"overflow")
                # _drop_count goes 1→2→3→4→5.
                # Only the first drop (count=1) triggers a warning.
                assert mock_logger.warning.call_count == 1


# ── recv_datagram() ───────────────────────────────────────────────────────────


class TestRecvDatagram:
    async def test_fast_path_returns_queued_item(self, make_udp_flow):
        flow = make_udp_flow()
        flow.feed(b"immediate")
        result = await flow.recv_datagram()
        assert result == b"immediate"

    async def test_returns_none_when_closed_and_queue_empty(self, make_udp_flow):
        flow = make_udp_flow()
        flow.on_remote_closed()
        result = await flow.recv_datagram()
        assert result is None

    async def test_blocking_wait_returns_data_when_fed(self, make_udp_flow):
        flow = make_udp_flow()
        task = asyncio.create_task(flow.recv_datagram())
        await asyncio.sleep(0)
        flow.feed(b"late arrival")
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result == b"late arrival"

    async def test_blocking_wait_returns_none_when_close_fires(self, make_udp_flow):
        flow = make_udp_flow()
        task = asyncio.create_task(flow.recv_datagram())
        await asyncio.sleep(0)
        flow.on_remote_closed()
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is None

    async def test_drains_remaining_item_after_close_fires(self, make_udp_flow):
        flow = make_udp_flow()
        task = asyncio.create_task(flow.recv_datagram())
        await asyncio.sleep(0)

        flow.feed(b"final packet")
        flow.on_remote_closed()

        result = await asyncio.wait_for(task, timeout=1.0)
        assert result == b"final packet"

    async def test_cancelled_error_propagates(self, make_udp_flow):
        flow = make_udp_flow()
        task = asyncio.create_task(flow.recv_datagram())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ── send_datagram() ───────────────────────────────────────────────────────────


class TestSendDatagram:
    async def test_invalid_type_raises(self, make_udp_flow):
        flow = make_udp_flow()
        with pytest.raises(TransportError) as exc_info:
            await flow.send_datagram("not bytes")
        assert exc_info.value.error_code == "transport.invalid_payload_type"

    async def test_noop_when_closed(self, make_udp_flow, ws_send):
        flow = make_udp_flow()
        flow._closed = True
        await flow.send_datagram(b"data")
        assert not ws_send.has_frame_type("UDP_DATA")

    async def test_raises_if_not_opened(self, make_udp_flow):
        flow = make_udp_flow()
        with pytest.raises(TransportError) as exc_info:
            await flow.send_datagram(b"data")
        assert exc_info.value.error_code == "transport.udp_send_before_open"

    async def test_sends_udp_data_frame(self, make_udp_flow, ws_send):
        flow = make_udp_flow()
        await flow.open()
        await flow.send_datagram(b"payload")
        assert ws_send.has_frame_type("UDP_DATA")

    async def test_bytes_sent_tracked(self, make_udp_flow):
        flow = make_udp_flow()
        await flow.open()
        await flow.send_datagram(b"hello")
        await flow.send_datagram(b"world")
        assert flow.bytes_sent == 10

    async def test_bytes_not_counted_on_failure(self, ws_send, udp_registry):
        flow = UdpFlow(UDP_FLOW_ID, "127.0.0.1", 53, ws_send, udp_registry)
        flow._opened = True

        async def failing_send(
            frame: str, *, must_queue: bool = False, control: bool = False
        ) -> None:
            if ":UDP_DATA:" in frame:
                raise ConnectionClosedError("closed", error_code="transport.closed")
            ws_send.frames.append(frame)

        flow._ws_send = failing_send
        with pytest.raises(ConnectionClosedError):
            await flow.send_datagram(b"data")
        assert flow.bytes_sent == 0

    async def test_ws_send_timeout_propagates(self, ws_send, udp_registry):
        ws_send.side_effect = WebSocketSendTimeoutError(
            "timeout", error_code="ws.timeout"
        )
        flow = UdpFlow(UDP_FLOW_ID, "127.0.0.1", 53, ws_send, udp_registry)
        flow._opened = True
        with pytest.raises(WebSocketSendTimeoutError):
            await flow.send_datagram(b"data")

    async def test_connection_closed_error_propagates(self, ws_send, udp_registry):
        ws_send.side_effect = ConnectionClosedError(
            "closed", error_code="transport.closed"
        )
        flow = UdpFlow(UDP_FLOW_ID, "127.0.0.1", 53, ws_send, udp_registry)
        flow._opened = True
        with pytest.raises(ConnectionClosedError):
            await flow.send_datagram(b"data")

    async def test_generic_exception_wrapped_as_transport_error(
        self, ws_send, udp_registry
    ):
        ws_send.side_effect = RuntimeError("socket error")
        flow = UdpFlow(UDP_FLOW_ID, "127.0.0.1", 53, ws_send, udp_registry)
        flow._opened = True
        with pytest.raises(TransportError) as exc_info:
            await flow.send_datagram(b"data")
        assert exc_info.value.error_code == "transport.udp_data_send_failed"


# ── _evict() ──────────────────────────────────────────────────────────────────


class TestEvict:
    def test_removes_flow_from_registry(self, make_udp_flow, udp_registry):
        flow = make_udp_flow()
        assert UDP_FLOW_ID in udp_registry
        flow._evict()
        assert UDP_FLOW_ID not in udp_registry

    def test_noop_when_not_in_registry(self, make_udp_flow, udp_registry):
        flow = make_udp_flow()
        udp_registry.clear()
        flow._evict()

    def test_second_evict_noop(self, make_udp_flow, udp_registry):
        flow = make_udp_flow()
        flow._evict()
        flow._evict()
        assert UDP_FLOW_ID not in udp_registry


# ── Full lifecycle ────────────────────────────────────────────────────────────


class TestLifecycle:
    async def test_open_send_recv_close(self, make_udp_flow, ws_send, udp_registry):
        flow = make_udp_flow()
        await flow.open()
        assert flow.is_opened
        assert ws_send.has_frame_type("UDP_OPEN")

        flow.feed(b"inbound datagram")
        data = await flow.recv_datagram()
        assert data == b"inbound datagram"
        assert flow.bytes_recv == len(b"inbound datagram")

        await flow.send_datagram(b"outbound datagram")
        assert flow.bytes_sent == len(b"outbound datagram")
        assert ws_send.has_frame_type("UDP_DATA")

        await flow.close()
        assert flow.is_closed
        assert ws_send.has_frame_type("UDP_CLOSE")
        assert UDP_FLOW_ID not in udp_registry

    async def test_remote_close_unblocks_recv(self, make_udp_flow):
        flow = make_udp_flow()
        await flow.open()
        task = asyncio.create_task(flow.recv_datagram())
        await asyncio.sleep(0)
        flow.on_remote_closed()
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result is None
        assert flow.is_closed
