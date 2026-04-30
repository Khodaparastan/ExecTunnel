"""Tests for exectunnel.proxy.udp_relay."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import struct
from unittest.mock import MagicMock, patch

import pytest
from exectunnel.exceptions import ProtocolError, TransportError
from exectunnel.protocol import AddrType
from exectunnel.proxy._wire import (
    build_udp_header,
    build_udp_header_for_host,
    parse_udp_header,
)
from exectunnel.proxy.udp_relay import UDPRelay


def _socks5_udp_dgram(host: str, port: int, payload: bytes) -> bytes:
    try:
        addr = ipaddress.ip_address(host)
        if addr.version == 4:
            atyp = bytes([int(AddrType.IPV4)])
            addr_bytes = addr.packed
        else:
            atyp = bytes([int(AddrType.IPV6)])
            addr_bytes = addr.packed
    except ValueError:
        enc = host.encode()
        atyp = bytes([int(AddrType.DOMAIN), len(enc)])
        addr_bytes = enc
    return b"\x00\x00\x00" + atyp + addr_bytes + struct.pack("!H", port) + payload


def _mock_transport() -> MagicMock:
    transport = MagicMock(spec=asyncio.DatagramTransport)
    transport.is_closing.return_value = False
    return transport


class TestUDPRelayLifecycle:
    async def test_start_returns_nonzero_port(self):
        relay = UDPRelay()
        port = await relay.start()
        relay.close()
        assert 1 <= port <= 65_535

    async def test_start_twice_raises_runtime_error(self):
        relay = UDPRelay()
        await relay.start()
        relay.close()
        with pytest.raises(RuntimeError):
            await relay.start()

    async def test_local_port_matches_returned_port(self):
        relay = UDPRelay()
        port = await relay.start()
        assert relay.local_port == port
        relay.close()

    async def test_is_running_true_after_start(self):
        relay = UDPRelay()
        await relay.start()
        assert relay.is_running is True
        relay.close()

    async def test_is_running_false_before_start(self):
        relay = UDPRelay()
        assert relay.is_running is False

    async def test_is_running_false_after_close(self):
        relay = UDPRelay()
        await relay.start()
        relay.close()
        assert relay.is_running is False

    async def test_close_idempotent(self):
        relay = UDPRelay()
        await relay.start()
        relay.close()
        relay.close()

    async def test_close_before_start_is_safe(self):
        relay = UDPRelay()
        relay.close()

    async def test_context_manager_starts_and_closes(self):
        async with UDPRelay() as relay:
            assert relay.is_running is True
        assert relay.is_running is False

    async def test_expected_client_addr_unspecified_ignored(self):
        relay = UDPRelay()
        await relay.start(expected_client_addr=("0.0.0.0", 1234))
        assert relay._expected_client_addr is None
        relay.close()

    async def test_expected_client_addr_port_zero_ignored(self):
        relay = UDPRelay()
        await relay.start(expected_client_addr=("1.2.3.4", 0))
        assert relay._expected_client_addr is None
        relay.close()

    async def test_expected_client_addr_valid_stored(self):
        relay = UDPRelay()
        await relay.start(expected_client_addr=("1.2.3.4", 12345))
        assert relay._expected_client_addr == ("1.2.3.4", 12345)
        relay.close()

    async def test_malformed_expected_client_addr_host_ignored(self):
        relay = UDPRelay()
        await relay.start(expected_client_addr=("not-an-ip", 1234))
        assert relay._expected_client_addr is None
        relay.close()


class TestUDPRelayOnDatagram:
    def _relay_with_transport(self) -> UDPRelay:
        relay = UDPRelay()
        relay._started = True
        relay._queue = asyncio.Queue(100)
        relay._close_event = asyncio.Event()
        relay._transport = _mock_transport()
        return relay

    def test_valid_ipv4_datagram_enqueued(self):
        relay = self._relay_with_transport()
        dgram = _socks5_udp_dgram("8.8.8.8", 53, b"dns")
        relay.on_datagram(dgram, ("127.0.0.1", 10000))
        assert relay._queue.qsize() == 1
        payload, host, port = relay._queue.get_nowait()
        assert payload == b"dns"
        assert host == "8.8.8.8"
        assert port == 53

    def test_valid_domain_datagram_enqueued(self):
        relay = self._relay_with_transport()
        dgram = _socks5_udp_dgram("example.com", 80, b"http")
        relay.on_datagram(dgram, ("127.0.0.1", 10000))
        _, host, port = relay._queue.get_nowait()
        assert host == "example.com"
        assert port == 80

    def test_first_datagram_binds_client_addr(self):
        relay = self._relay_with_transport()
        dgram = _socks5_udp_dgram("1.1.1.1", 53, b"x")
        relay.on_datagram(dgram, ("127.0.0.1", 9999))
        assert relay._client_addr == ("127.0.0.1", 9999)

    def test_datagram_from_second_client_dropped(self):
        relay = self._relay_with_transport()
        dgram = _socks5_udp_dgram("1.1.1.1", 53, b"x")
        relay.on_datagram(dgram, ("127.0.0.1", 9999))
        relay._queue.get_nowait()
        relay.on_datagram(dgram, ("127.0.0.1", 8888))
        assert relay._queue.qsize() == 0
        assert relay.foreign_client_count == 1

    def test_expected_client_addr_hint_enforced(self):
        relay = self._relay_with_transport()
        relay._expected_client_addr = ("1.2.3.4", 9999)
        dgram = _socks5_udp_dgram("8.8.8.8", 53, b"x")
        relay.on_datagram(dgram, ("5.6.7.8", 1111))
        assert relay._queue.qsize() == 0
        assert relay.foreign_client_count == 1

    def test_expected_client_addr_matching_accepted(self):
        relay = self._relay_with_transport()
        relay._expected_client_addr = ("1.2.3.4", 9999)
        dgram = _socks5_udp_dgram("8.8.8.8", 53, b"data")
        relay.on_datagram(dgram, ("1.2.3.4", 9999))
        assert relay._queue.qsize() == 1

    def test_malformed_header_dropped(self):
        relay = self._relay_with_transport()
        relay.on_datagram(b"\x00\x00\x00\xff" + b"\x00" * 10, ("127.0.0.1", 9999))
        assert relay._queue.qsize() == 0

    def test_non_zero_frag_dropped(self):
        relay = self._relay_with_transport()
        dgram = (
            b"\x00\x00\x01"
            + bytes([int(AddrType.IPV4)])
            + b"\x01\x02\x03\x04"
            + struct.pack("!H", 80)
            + b"x"
        )
        relay.on_datagram(dgram, ("127.0.0.1", 9999))
        assert relay._queue.qsize() == 0

    def test_oversized_payload_dropped(self):
        relay = self._relay_with_transport()
        big_payload = b"x" * 70_000
        dgram = _socks5_udp_dgram("1.2.3.4", 80, big_payload)
        relay.on_datagram(dgram, ("127.0.0.1", 9999))
        assert relay._queue.qsize() == 0

    def test_queue_full_increments_drop_count(self):
        relay = UDPRelay(queue_capacity=1)
        relay._started = True
        relay._queue = asyncio.Queue(1)
        relay._close_event = asyncio.Event()
        relay._transport = _mock_transport()
        dgram = _socks5_udp_dgram("1.2.3.4", 80, b"x")
        relay.on_datagram(dgram, ("127.0.0.1", 9999))
        relay.on_datagram(dgram, ("127.0.0.1", 9999))
        assert relay.drop_count == 1

    def test_closed_relay_silently_ignores_datagram(self):
        relay = self._relay_with_transport()
        relay._closed = True
        dgram = _socks5_udp_dgram("1.2.3.4", 80, b"x")
        relay.on_datagram(dgram, ("127.0.0.1", 9999))
        assert relay._queue.qsize() == 0

    def test_accepted_count_incremented(self):
        relay = self._relay_with_transport()
        dgram = _socks5_udp_dgram("1.2.3.4", 80, b"x")
        relay.on_datagram(dgram, ("127.0.0.1", 9999))
        assert relay.accepted_count == 1


class TestUDPRelayRecv:
    def _started_relay(self) -> UDPRelay:
        relay = UDPRelay()
        relay._started = True
        relay._queue = asyncio.Queue(100)
        relay._close_event = asyncio.Event()
        relay._transport = _mock_transport()
        return relay

    async def test_recv_before_start_raises(self):
        relay = UDPRelay()
        with pytest.raises(RuntimeError):
            await relay.recv()

    async def test_recv_fast_path_returns_queued_item(self):
        relay = self._started_relay()
        item = (b"payload", "8.8.8.8", 53)
        relay._queue.put_nowait(item)
        result = await relay.recv()
        assert result == item

    async def test_recv_returns_none_when_closed_and_empty(self):
        relay = self._started_relay()
        relay._close_event.set()
        result = await relay.recv()
        assert result is None

    async def test_recv_drains_item_then_returns_none(self):
        relay = self._started_relay()
        item = (b"last", "1.2.3.4", 80)
        relay._queue.put_nowait(item)
        relay._close_event.set()
        result = await relay.recv()
        assert result == item
        result2 = await relay.recv()
        assert result2 is None

    async def test_recv_waits_for_item(self):
        relay = self._started_relay()

        async def _enqueue():
            await asyncio.sleep(0.05)
            relay._queue.put_nowait((b"delayed", "1.1.1.1", 53))

        asyncio.create_task(_enqueue())
        result = await asyncio.wait_for(relay.recv(), timeout=1.0)
        assert result == (b"delayed", "1.1.1.1", 53)

    async def test_recv_returns_none_on_close_while_waiting(self):
        relay = self._started_relay()

        async def _close():
            await asyncio.sleep(0.05)
            relay.close()

        asyncio.create_task(_close())
        result = await asyncio.wait_for(relay.recv(), timeout=1.0)
        assert result is None

    async def test_recv_propagates_cancellation(self):
        relay = self._started_relay()

        async def _recv():
            return await relay.recv()

        task = asyncio.create_task(_recv())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        relay.close()

    async def test_close_wait_task_reused_across_calls(self):
        relay = self._started_relay()

        relay._queue.put_nowait((b"a", "1.2.3.4", 80))
        await relay.recv()

        relay._queue.put_nowait((b"b", "1.2.3.4", 80))
        task_before = relay._close_wait_task
        await relay.recv()

        if task_before is not None:
            assert (
                relay._close_wait_task is task_before or relay._close_wait_task is None
            )

        relay.close()


class TestUDPRelaySendToClient:
    def _relay_with_client(self) -> tuple[UDPRelay, MagicMock]:
        relay = UDPRelay()
        relay._started = True
        relay._queue = asyncio.Queue(100)
        relay._close_event = asyncio.Event()
        transport = _mock_transport()
        relay._transport = transport
        relay._client_addr = ("127.0.0.1", 12345)
        return relay, transport

    def test_ipv4_sendto_called_with_correct_args(self):
        relay, transport = self._relay_with_client()
        relay.send_to_client(b"data", "8.8.8.8", 53)
        expected_header = build_udp_header(
            AddrType.IPV4, ipaddress.IPv4Address("8.8.8.8").packed, 53
        )
        transport.sendto.assert_called_once_with(
            expected_header + b"data", ("127.0.0.1", 12345)
        )

    def test_ipv6_sendto_called_with_correct_args(self):
        relay, transport = self._relay_with_client()
        relay.send_to_client(b"resp", "::1", 443)
        expected_header = build_udp_header(
            AddrType.IPV6, ipaddress.IPv6Address("::1").packed, 443
        )
        transport.sendto.assert_called_once_with(
            expected_header + b"resp", ("127.0.0.1", 12345)
        )

    def test_non_bytes_payload_raises_transport_error(self):
        relay, _ = self._relay_with_client()
        with pytest.raises(TransportError):
            relay.send_to_client("not bytes", "1.2.3.4", 80)  # type: ignore[arg-type]

    def test_port_negative_raises_transport_error(self):
        relay, transport = self._relay_with_client()
        relay.send_to_client(b"x", "1.2.3.4", -1)
        transport.sendto.assert_not_called()

    def test_port_above_65535_raises_transport_error(self):
        relay, transport = self._relay_with_client()
        relay.send_to_client(b"x", "1.2.3.4", 65_536)
        transport.sendto.assert_not_called()

    def test_no_transport_silently_returns(self):
        relay, _ = self._relay_with_client()
        relay._transport = None
        relay.send_to_client(b"x", "1.2.3.4", 80)

    def test_closed_relay_silently_returns(self):
        relay, transport = self._relay_with_client()
        relay._closed = True
        relay.send_to_client(b"x", "1.2.3.4", 80)
        transport.sendto.assert_not_called()

    def test_client_not_bound_logs_and_returns(self):
        relay, transport = self._relay_with_client()
        relay._client_addr = None
        relay.send_to_client(b"x", "1.2.3.4", 80)
        transport.sendto.assert_not_called()

    def test_domain_host_supported(self):
        relay, transport = self._relay_with_client()
        relay.send_to_client(b"data", "example.com", 80)
        expected_header = build_udp_header_for_host("example.com", 80)
        transport.sendto.assert_called_once_with(
            expected_header + b"data", ("127.0.0.1", 12345)
        )

    def test_os_error_suppressed(self):
        relay, transport = self._relay_with_client()
        transport.sendto.side_effect = OSError("network unreachable")
        relay.send_to_client(b"x", "1.2.3.4", 80)
