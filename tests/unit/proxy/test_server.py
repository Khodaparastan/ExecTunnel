"""Integration tests for exectunnel.proxy.server."""

from __future__ import annotations

import asyncio
import ipaddress
import struct

import pytest
from exectunnel.exceptions import TransportError
from exectunnel.protocol import AddrType, AuthMethod, Cmd, Reply
from exectunnel.proxy._wire import build_socks5_reply
from exectunnel.proxy.config import Socks5ServerConfig
from exectunnel.proxy.server import Socks5Server
from exectunnel.proxy.tcp_relay import TCPRelay

from unit.proxy.conftest import free_port


def _greeting(methods: list[int] | None = None) -> bytes:
    if methods is None:
        methods = [int(AuthMethod.NO_AUTH)]
    return bytes([0x05, len(methods)] + methods)


def _connect_request(host: str, port: int) -> bytes:
    enc = host.encode()
    return (
        bytes([0x05, int(Cmd.CONNECT), 0x00, 0x03, len(enc)])
        + enc
        + struct.pack("!H", port)
    )


def _udp_associate_request(host: str = "0.0.0.0", port: int = 0) -> bytes:
    addr = ipaddress.IPv4Address(host).packed
    return (
        bytes([0x05, int(Cmd.UDP_ASSOCIATE), 0x00, 0x01])
        + addr
        + struct.pack("!H", port)
    )


def _bind_request() -> bytes:
    addr = ipaddress.IPv4Address("0.0.0.0").packed
    return bytes([0x05, int(Cmd.BIND), 0x00, 0x01]) + addr + struct.pack("!H", 0)


async def _negotiate_no_auth(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    methods: list[int] | None = None,
) -> None:
    """Send SOCKS5 greeting and verify the method-selection reply.

    Does NOT send a command request.
    """
    writer.write(_greeting(methods))
    await writer.drain()
    reply = await asyncio.wait_for(reader.readexactly(2), timeout=2.0)
    assert reply == bytes([0x05, int(AuthMethod.NO_AUTH)])


async def _full_handshake_no_reply(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    request_bytes: bytes,
) -> None:
    """Complete greeting + method selection + send command.

    Does NOT read the server reply. CONNECT and UDP_ASSOCIATE never produce
    a server-side reply at this stage — that is the session layer's
    responsibility via ``send_reply_success`` / ``send_reply_error``.
    """
    await _negotiate_no_auth(reader, writer)
    writer.write(request_bytes)
    await writer.drain()


async def _get_request(server: Socks5Server, timeout: float = 2.0) -> TCPRelay:
    """Wait for a completed request to appear in the server queue."""
    return await asyncio.wait_for(server._queue.get(), timeout=timeout)


class TestSocks5ServerLifecycle:
    async def test_start_succeeds(self):
        port = free_port()
        server = Socks5Server(Socks5ServerConfig(host="127.0.0.1", port=port))
        await server.start()
        await server.stop()

    async def test_start_twice_raises_runtime_error(self):
        port = free_port()
        server = Socks5Server(Socks5ServerConfig(host="127.0.0.1", port=port))
        await server.start()
        with pytest.raises(RuntimeError):
            await server.start()
        await server.stop()

    async def test_stop_before_start_is_safe(self):
        await Socks5Server().stop()

    async def test_stop_idempotent(self):
        port = free_port()
        server = Socks5Server(Socks5ServerConfig(host="127.0.0.1", port=port))
        await server.start()
        await server.stop()
        await server.stop()

    async def test_bind_failure_raises_transport_error(self):
        """Use a port conflict to guarantee a reliable bind failure."""
        port = free_port()
        cfg = Socks5ServerConfig(host="127.0.0.1", port=port)
        server1 = Socks5Server(cfg)
        await server1.start()
        try:
            with pytest.raises(TransportError):
                await Socks5Server(cfg).start()
        finally:
            await server1.stop()

    async def test_non_loopback_logs_warning(self, caplog):
        import logging

        port = free_port()
        server = Socks5Server(
            Socks5ServerConfig(
                host="0.0.0.0",
                port=port,
                allow_non_loopback=True,
                udp_bind_host="0.0.0.0",
                udp_advertise_host="1.1.1.1",
            )
        )
        with caplog.at_level(logging.WARNING, logger="exectunnel.proxy.server"):
            await server.start()
            await server.stop()
        assert any("non-loopback" in r.message for r in caplog.records)

    async def test_context_manager_starts_and_stops(self):
        port = free_port()
        async with Socks5Server(
            Socks5ServerConfig(host="127.0.0.1", port=port)
        ) as server:
            assert server._started is True
        assert server._stopped is True

    async def test_default_config_used_when_none_given(self):
        assert Socks5Server(None)._config is not None

    async def test_async_iter_stops_on_sentinel(self):
        port = free_port()
        server = Socks5Server(Socks5ServerConfig(host="127.0.0.1", port=port))
        await server.start()
        await server.stop()
        collected = [req async for req in server]
        assert collected == []


class TestSocks5ServerConnectHandshake:
    async def test_connect_request_enqueued(self):
        port = free_port()
        async with Socks5Server(
            Socks5ServerConfig(host="127.0.0.1", port=port)
        ) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                await _full_handshake_no_reply(
                    reader, writer, _connect_request("example.com", 80)
                )
                req = await _get_request(server)
                assert req.cmd is Cmd.CONNECT
                assert req.host == "example.com"
                assert req.port == 80
                assert req.udp_relay is None
                await req.close()
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_connect_reader_and_writer_accessible(self):
        port = free_port()
        async with Socks5Server(
            Socks5ServerConfig(host="127.0.0.1", port=port)
        ) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                await _full_handshake_no_reply(
                    reader, writer, _connect_request("example.com", 443)
                )
                req = await _get_request(server)
                assert req.reader is not None
                assert req.writer is not None
                await req.close()
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_connect_ipv4_destination(self):
        port = free_port()
        async with Socks5Server(
            Socks5ServerConfig(host="127.0.0.1", port=port)
        ) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                req_bytes = (
                    bytes([0x05, int(Cmd.CONNECT), 0x00, int(AddrType.IPV4)])
                    + ipaddress.IPv4Address("1.2.3.4").packed
                    + struct.pack("!H", 80)
                )
                await _full_handshake_no_reply(reader, writer, req_bytes)
                req = await _get_request(server)
                assert req.host == "1.2.3.4"
                await req.close()
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_multiple_sequential_connections_enqueued(self):
        port = free_port()
        cfg = Socks5ServerConfig(host="127.0.0.1", port=port, request_queue_capacity=10)
        async with Socks5Server(cfg) as server:
            connections = []
            for i in range(3):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                await _full_handshake_no_reply(
                    reader, writer, _connect_request(f"host{i}.example.com", 80 + i)
                )
                connections.append((reader, writer))

            requests = [await _get_request(server) for _ in range(3)]
            hosts = {req.host for req in requests}
            assert hosts == {
                "host0.example.com",
                "host1.example.com",
                "host2.example.com",
            }

            for req in requests:
                await req.close()
            for _, writer in connections:
                writer.close()
                await writer.wait_closed()


class TestSocks5ServerUdpAssociate:
    async def test_udp_associate_creates_relay(self):
        port = free_port()
        async with Socks5Server(
            Socks5ServerConfig(host="127.0.0.1", port=port)
        ) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                await _full_handshake_no_reply(reader, writer, _udp_associate_request())
                req = await _get_request(server)
                assert req.cmd is Cmd.UDP_ASSOCIATE
                assert req.udp_relay is not None
                assert req.udp_relay.local_port > 0
                await req.close()
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_udp_associate_relay_is_running(self):
        port = free_port()
        async with Socks5Server(
            Socks5ServerConfig(host="127.0.0.1", port=port)
        ) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                await _full_handshake_no_reply(reader, writer, _udp_associate_request())
                req = await _get_request(server)
                assert req.udp_relay.is_running is True
                await req.close()
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_udp_associate_relay_closed_with_request(self):
        port = free_port()
        async with Socks5Server(
            Socks5ServerConfig(host="127.0.0.1", port=port)
        ) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                await _full_handshake_no_reply(reader, writer, _udp_associate_request())
                req = await _get_request(server)
                relay = req.udp_relay
                await req.close()
                assert relay.is_running is False
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_udp_associate_zero_addr_produces_no_hint(self):
        """Client sending 0.0.0.0:0 means it doesn't know its own address."""
        port = free_port()
        async with Socks5Server(
            Socks5ServerConfig(host="127.0.0.1", port=port)
        ) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                await _full_handshake_no_reply(
                    reader, writer, _udp_associate_request("0.0.0.0", 0)
                )
                req = await _get_request(server)
                assert req.udp_relay._expected_client_addr is None
                await req.close()
            finally:
                writer.close()
                await writer.wait_closed()


class TestSocks5ServerRejectionPaths:
    async def test_wrong_socks_version_closes_connection(self):
        port = free_port()
        async with Socks5Server(
            Socks5ServerConfig(host="127.0.0.1", port=port)
        ) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(bytes([0x04, 0x01, 0x00]))
            await writer.drain()
            data = await asyncio.wait_for(reader.read(1024), timeout=2.0)
            assert data == b""
            writer.close()
            await writer.wait_closed()

    async def test_zero_nmethods_sends_no_accept(self):
        port = free_port()
        async with Socks5Server(
            Socks5ServerConfig(host="127.0.0.1", port=port)
        ) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(bytes([0x05, 0x00]))
            await writer.drain()
            data = await asyncio.wait_for(reader.readexactly(2), timeout=2.0)
            assert data == bytes([0x05, int(AuthMethod.NO_ACCEPT)])
            writer.close()
            await writer.wait_closed()

    async def test_no_auth_not_offered_sends_no_accept(self):
        port = free_port()
        async with Socks5Server(
            Socks5ServerConfig(host="127.0.0.1", port=port)
        ) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(bytes([0x05, 0x01, int(AuthMethod.GSSAPI)]))
            await writer.drain()
            data = await asyncio.wait_for(reader.readexactly(2), timeout=2.0)
            assert data == bytes([0x05, int(AuthMethod.NO_ACCEPT)])
            writer.close()
            await writer.wait_closed()

    async def test_non_zero_rsv_sends_general_failure(self):
        port = free_port()
        async with Socks5Server(
            Socks5ServerConfig(host="127.0.0.1", port=port)
        ) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await _negotiate_no_auth(reader, writer)
            enc = b"example.com"
            writer.write(
                bytes([0x05, int(Cmd.CONNECT), 0x01, 0x03, len(enc)])
                + enc
                + struct.pack("!H", 80)
            )
            await writer.drain()
            data = await asyncio.wait_for(reader.read(1024), timeout=2.0)
            assert data[: len(build_socks5_reply(Reply.GENERAL_FAILURE))] == (
                build_socks5_reply(Reply.GENERAL_FAILURE)
            )
            writer.close()
            await writer.wait_closed()

    async def test_unknown_command_sends_cmd_not_supported(self):
        port = free_port()
        async with Socks5Server(
            Socks5ServerConfig(host="127.0.0.1", port=port)
        ) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await _negotiate_no_auth(reader, writer)
            writer.write(
                bytes([0x05, 0x7F, 0x00, 0x01])
                + ipaddress.IPv4Address("0.0.0.0").packed
                + struct.pack("!H", 0)
            )
            await writer.drain()
            data = await asyncio.wait_for(reader.read(1024), timeout=2.0)
            expected = build_socks5_reply(Reply.CMD_NOT_SUPPORTED)
            assert data[: len(expected)] == expected
            writer.close()
            await writer.wait_closed()

    async def test_bind_command_sends_cmd_not_supported(self):
        port = free_port()
        async with Socks5Server(
            Socks5ServerConfig(host="127.0.0.1", port=port)
        ) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await _negotiate_no_auth(reader, writer)
            writer.write(_bind_request())
            await writer.drain()
            data = await asyncio.wait_for(reader.read(1024), timeout=2.0)
            expected = build_socks5_reply(Reply.CMD_NOT_SUPPORTED)
            assert data[: len(expected)] == expected
            writer.close()
            await writer.wait_closed()

    async def test_bind_does_not_enqueue_request(self):
        port = free_port()
        async with Socks5Server(
            Socks5ServerConfig(host="127.0.0.1", port=port)
        ) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await _negotiate_no_auth(reader, writer)
            writer.write(_bind_request())
            await writer.drain()
            await asyncio.wait_for(reader.read(1024), timeout=2.0)
            await asyncio.sleep(0.05)
            assert server._queue.qsize() == 0
            writer.close()
            await writer.wait_closed()

    async def test_handshake_timeout_closes_connection(self):
        port = free_port()
        cfg = Socks5ServerConfig(host="127.0.0.1", port=port, handshake_timeout=0.1)
        async with Socks5Server(cfg) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            data = await asyncio.wait_for(reader.read(1024), timeout=2.0)
            assert data == b""
            writer.close()
            await writer.wait_closed()

    async def test_client_disconnect_mid_handshake_tolerated(self):
        port = free_port()
        async with Socks5Server(
            Socks5ServerConfig(host="127.0.0.1", port=port)
        ) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(bytes([0x05]))
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.1)
            assert server._started is True


class TestSocks5ServerQueueFull:
    async def test_queue_full_sends_general_failure(self):
        """
        Fill the queue (capacity=1) without consuming the request, then verify
        that a second connection receives GENERAL_FAILURE after queue_put_timeout.
        """
        port = free_port()
        cfg = Socks5ServerConfig(
            host="127.0.0.1",
            port=port,
            request_queue_capacity=1,
            queue_put_timeout=0.1,
        )
        async with Socks5Server(cfg) as server:
            # First connection fills the queue — do NOT consume it.
            reader1, writer1 = await asyncio.open_connection("127.0.0.1", port)
            await _full_handshake_no_reply(
                reader1, writer1, _connect_request("first.example.com", 80)
            )
            await asyncio.sleep(0.05)
            assert server._queue.qsize() == 1

            # Second connection: queue is full → GENERAL_FAILURE after timeout.
            reader2, writer2 = await asyncio.open_connection("127.0.0.1", port)
            await _negotiate_no_auth(reader2, writer2)
            writer2.write(_connect_request("second.example.com", 80))
            await writer2.drain()

            data = await asyncio.wait_for(reader2.read(1024), timeout=2.0)
            expected = build_socks5_reply(Reply.GENERAL_FAILURE)
            assert data[: len(expected)] == expected

            req1 = await _get_request(server)
            await req1.close()
            writer1.close()
            await writer1.wait_closed()
            writer2.close()
            await writer2.wait_closed()


class TestSocks5ServerStop:
    async def test_stop_cancels_in_flight_handshake(self):
        port = free_port()
        server = Socks5Server(Socks5ServerConfig(host="127.0.0.1", port=port))
        await server.start()

        # Send only half the greeting so the server blocks waiting for more bytes.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(bytes([0x05]))
        await writer.drain()
        await asyncio.sleep(0.05)

        assert len(server._handshake_tasks) > 0
        await asyncio.wait_for(server.stop(), timeout=2.0)
        assert len(server._handshake_tasks) == 0

        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=2.0)

    async def test_stop_drains_queued_requests(self):
        port = free_port()
        server = Socks5Server(
            Socks5ServerConfig(host="127.0.0.1", port=port, request_queue_capacity=10)
        )
        await server.start()

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await _full_handshake_no_reply(
            reader, writer, _connect_request("example.com", 80)
        )
        await asyncio.sleep(0.05)
        assert server._queue.qsize() == 1

        await asyncio.wait_for(server.stop(), timeout=2.0)
        assert server._queue.qsize() == 1
        assert server._queue.get_nowait() is None

        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=2.0)

    async def test_stop_puts_sentinel_for_iterator(self):
        port = free_port()
        server = Socks5Server(Socks5ServerConfig(host="127.0.0.1", port=port))
        await server.start()
        await server.stop()
        assert server._queue.get_nowait() is None

    async def test_stop_uses_put_nowait_not_blocking_put(self):
        """
        After draining, the queue is empty so put_nowait succeeds immediately.
        Verifying the sentinel is present and no blocking await was needed.
        """
        port = free_port()
        server = Socks5Server(
            Socks5ServerConfig(host="127.0.0.1", port=port, request_queue_capacity=256)
        )
        await server.start()
        await server.stop()
        assert server._queue.qsize() == 1
        assert server._queue.get_nowait() is None
