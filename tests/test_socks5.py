"""Tests for exectunnel.socks5 — SOCKS5 negotiation paths."""
from __future__ import annotations

import asyncio
import socket
import struct

import pytest

from exectunnel.core.consts import AddrType, AuthMethod, Cmd, Reply
from exectunnel.socks5 import (
    Socks5Request,
    Socks5Server,
    UdpRelay,
    _build_reply,
    _read_addr,
)

# ── _build_reply ──────────────────────────────────────────────────────────────


class TestBuildReply:
    def test_success_ipv4(self) -> None:
        data = _build_reply(Reply.SUCCESS, "127.0.0.1", 1080)
        assert data[0] == 0x05
        assert data[1] == int(Reply.SUCCESS)
        assert data[2] == 0x00
        assert data[3] == int(AddrType.IPV4)
        port = struct.unpack("!H", data[-2:])[0]
        assert port == 1080

    def test_general_failure(self) -> None:
        data = _build_reply(Reply.GENERAL_FAILURE)
        assert data[1] == int(Reply.GENERAL_FAILURE)

    def test_cmd_not_supported(self) -> None:
        data = _build_reply(Reply.CMD_NOT_SUPPORTED)
        assert data[1] == int(Reply.CMD_NOT_SUPPORTED)

    def test_ipv6_address(self) -> None:
        data = _build_reply(Reply.SUCCESS, "::1", 0)
        assert data[3] == int(AddrType.IPV6)

    def test_invalid_bind_port_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid bind_port"):
            _build_reply(Reply.SUCCESS, "127.0.0.1", -1)


# ── _read_addr ────────────────────────────────────────────────────────────────


class TestReadAddr:
    def _make_reader(self, data: bytes) -> asyncio.StreamReader:
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        return reader

    def _pack_ipv4(self, host: str, port: int) -> bytes:
        return (
            bytes([AddrType.IPV4])
            + socket.inet_pton(socket.AF_INET, host)
            + struct.pack("!H", port)
        )

    def _pack_domain(self, host: str, port: int) -> bytes:
        enc = host.encode()
        return (
            bytes([AddrType.DOMAIN, len(enc)])
            + enc
            + struct.pack("!H", port)
        )

    def _pack_ipv6(self, host: str, port: int) -> bytes:
        return (
            bytes([AddrType.IPV6])
            + socket.inet_pton(socket.AF_INET6, host)
            + struct.pack("!H", port)
        )

    async def test_ipv4(self) -> None:
        reader = self._make_reader(self._pack_ipv4("10.0.0.1", 8080))
        host, port = await _read_addr(reader)
        assert host == "10.0.0.1"
        assert port == 8080

    async def test_domain(self) -> None:
        reader = self._make_reader(self._pack_domain("example.com", 443))
        host, port = await _read_addr(reader)
        assert host == "example.com"
        assert port == 443

    async def test_ipv6(self) -> None:
        reader = self._make_reader(self._pack_ipv6("::1", 9000))
        host, port = await _read_addr(reader)
        assert host == "::1"
        assert port == 9000

    async def test_unsupported_atyp(self) -> None:
        reader = self._make_reader(bytes([0x02]) + b"\x00" * 6)
        with pytest.raises(ValueError, match="unsupported ATYP"):
            await _read_addr(reader)

    async def test_domain_with_invalid_utf8(self) -> None:
        reader = self._make_reader(
            bytes([AddrType.DOMAIN, 2]) + b"\xff\xff" + struct.pack("!H", 53)
        )
        with pytest.raises(ValueError, match="invalid DOMAIN bytes"):
            await _read_addr(reader)

    async def test_domain_with_empty_length(self) -> None:
        reader = self._make_reader(bytes([AddrType.DOMAIN, 0]) + struct.pack("!H", 53))
        with pytest.raises(ValueError, match="DOMAIN length"):
            await _read_addr(reader)


class TestUdpRelay:
    def _udp_packet_ipv4(self, host: str, port: int, payload: bytes) -> bytes:
        return (
            b"\x00\x00\x00"
            + bytes([int(AddrType.IPV4)])
            + socket.inet_pton(socket.AF_INET, host)
            + struct.pack("!H", port)
            + payload
        )

    def test_pins_first_client_addr(self) -> None:
        relay = UdpRelay()
        first = ("127.0.0.1", 10001)
        second = ("127.0.0.1", 10002)
        pkt = self._udp_packet_ipv4("8.8.8.8", 53, b"abc")

        relay._on_datagram(pkt, first)  # type: ignore[attr-defined]
        relay._on_datagram(pkt, second)  # type: ignore[attr-defined]

        item = relay._queue.get_nowait()  # type: ignore[attr-defined]
        assert item == (b"abc", "8.8.8.8", 53)
        with pytest.raises(asyncio.QueueEmpty):
            relay._queue.get_nowait()  # type: ignore[attr-defined]


# ── Socks5Server negotiation ──────────────────────────────────────────────────


def _build_greeting(methods: list[int]) -> bytes:
    return bytes([0x05, len(methods)] + methods)


def _build_connect_request(host: str = "127.0.0.1", port: int = 80) -> bytes:
    addr = socket.inet_pton(socket.AF_INET, host)
    return (
        bytes([0x05, int(Cmd.CONNECT), 0x00, int(AddrType.IPV4)])
        + addr
        + struct.pack("!H", port)
    )


def _build_udp_request() -> bytes:
    return (
        bytes([0x05, int(Cmd.UDP_ASSOCIATE), 0x00, int(AddrType.IPV4)])
        + b"\x00\x00\x00\x00"
        + struct.pack("!H", 0)
    )


def _build_domain_connect_request(domain: str, port: int) -> bytes:
    enc = domain.encode()
    return (
        bytes([0x05, int(Cmd.CONNECT), 0x00, int(AddrType.DOMAIN), len(enc)])
        + enc
        + struct.pack("!H", port)
    )


class TestSocks5Negotiation:
    async def _negotiate(self, data: bytes) -> tuple[bytes, bytes]:
        """
        Helper: feed *data* to the server's _negotiate coroutine and collect
        all bytes written back, plus return the resulting Socks5Request (if any).
        """
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()

        output = bytearray()

        class _Writer:
            def write(self, d: bytes) -> None:
                output.extend(d)

            async def drain(self) -> None:
                pass

            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                pass

        req = await Socks5Server._negotiate(reader, _Writer())  # type: ignore[arg-type]
        return bytes(output), req  # type: ignore[return-value]

    async def test_connect_happy_path(self) -> None:
        data = _build_greeting([AuthMethod.NO_AUTH]) + _build_connect_request("1.2.3.4", 80)
        output, req = await self._negotiate(data)
        # Server selects NO_AUTH
        assert output[:2] == bytes([0x05, AuthMethod.NO_AUTH])
        assert req is not None
        assert req.cmd == Cmd.CONNECT
        assert req.host == "1.2.3.4"
        assert req.port == 80

    async def test_domain_connect(self) -> None:
        data = (
            _build_greeting([AuthMethod.NO_AUTH])
            + _build_domain_connect_request("example.com", 443)
        )
        _, req = await self._negotiate(data)
        assert req is not None
        assert req.host == "example.com"
        assert req.port == 443

    async def test_no_auth_rejected(self) -> None:
        # Client only offers GSSAPI (0x01) — server writes NO_ACCEPT then raises.
        data = _build_greeting([0x01])
        with pytest.raises(ValueError, match="NO_AUTH"):
            await self._negotiate(data)

    async def test_wrong_socks_version(self) -> None:
        data = bytes([0x04, 0x01, 0x00])  # SOCKS4
        with pytest.raises(ValueError, match="not SOCKS5"):
            await self._negotiate(data)

    async def test_unsupported_command(self) -> None:
        # CMD=0x02 (BIND) — not supported
        data = (
            _build_greeting([AuthMethod.NO_AUTH])
            + bytes([0x05, 0x02, 0x00, int(AddrType.IPV4)])
            + b"\x7f\x00\x00\x01"
            + struct.pack("!H", 80)
        )
        output, req = await self._negotiate(data)
        # Reply byte should be CMD_NOT_SUPPORTED
        assert output[3] == int(Reply.CMD_NOT_SUPPORTED)
        assert req is None

    async def test_udp_associate(self) -> None:
        data = _build_greeting([AuthMethod.NO_AUTH]) + _build_udp_request()
        # UdpRelay.start() binds a real socket — skip in sandboxed CI.
        try:
            _, req = await self._negotiate(data)
        except PermissionError:
            pytest.skip("UDP socket bind not permitted in this environment")
        assert req is not None
        assert req.cmd == Cmd.UDP_ASSOCIATE
        assert req.udp_relay is not None
        req.udp_relay.close()

    async def test_nonzero_rsv_rejected(self) -> None:
        data = (
            _build_greeting([AuthMethod.NO_AUTH])
            + bytes([0x05, int(Cmd.CONNECT), 0x01, int(AddrType.IPV4)])
            + b"\x7f\x00\x00\x01"
            + struct.pack("!H", 80)
        )
        output, req = await self._negotiate(data)
        assert output[1] == int(AuthMethod.NO_AUTH)  # auth selection first
        assert output[3] == int(Reply.GENERAL_FAILURE)
        assert req is None

    async def test_connect_port_zero_rejected(self) -> None:
        data = (
            _build_greeting([AuthMethod.NO_AUTH])
            + bytes([0x05, int(Cmd.CONNECT), 0x00, int(AddrType.IPV4)])
            + b"\x7f\x00\x00\x01"
            + struct.pack("!H", 0)
        )
        output, req = await self._negotiate(data)
        assert output[1] == int(AuthMethod.NO_AUTH)
        assert output[3] == int(Reply.GENERAL_FAILURE)
        assert req is None


# ── Socks5Request reply helpers ───────────────────────────────────────────────


class TestSocks5RequestReplies:
    def _make_request(self) -> tuple[Socks5Request, bytearray]:
        output: bytearray = bytearray()

        class _Writer:
            async def drain(self) -> None:
                pass

            def write(self, data: bytes) -> None:
                output.extend(data)

            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                pass

        req = Socks5Request(
            cmd=Cmd.CONNECT,
            host="127.0.0.1",
            port=80,
            reader=asyncio.StreamReader(),
            writer=_Writer(),  # type: ignore[arg-type]
        )
        return req, output

    def test_reply_success_queues_success(self) -> None:
        req, output = self._make_request()
        req.reply_success()
        assert output[1] == int(Reply.SUCCESS)

    def test_reply_error_queues_error(self) -> None:
        req, output = self._make_request()
        req.reply_error(Reply.HOST_UNREACHABLE)
        assert output[1] == int(Reply.HOST_UNREACHABLE)


class TestSocks5ServerStop:
    def _make_request(self) -> tuple[Socks5Request, bytearray]:
        output: bytearray = bytearray()

        class _Writer:
            async def drain(self) -> None:
                pass

            def write(self, data: bytes) -> None:
                output.extend(data)

            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                pass

        req = Socks5Request(
            cmd=Cmd.CONNECT,
            host="127.0.0.1",
            port=80,
            reader=asyncio.StreamReader(),
            writer=_Writer(),  # type: ignore[arg-type]
        )
        return req, output

    async def test_stop_ignores_malformed_pending_item(self) -> None:
        server = Socks5Server()
        await server._queue.put(object())  # type: ignore[arg-type]
        await server.stop()

    async def test_send_reply_success_drains(self) -> None:
        req, output = self._make_request()
        await req.send_reply_success()
        assert output[1] == int(Reply.SUCCESS)

    async def test_send_reply_error_closes(self) -> None:
        req, output = self._make_request()
        await req.send_reply_error(Reply.GENERAL_FAILURE)
        assert output[1] == int(Reply.GENERAL_FAILURE)
