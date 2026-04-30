"""Tests for exectunnel.proxy._io."""

from __future__ import annotations

import asyncio
import ipaddress
import struct

import pytest
from exectunnel.exceptions import ProtocolError
from exectunnel.protocol import AddrType
from exectunnel.proxy._io import (
    close_writer,
    read_exact,
    read_socks5_addr,
    write_and_drain_silent,
)

from unit.proxy.conftest import make_stream_reader


def _ipv4_addr_bytes(host: str = "1.2.3.4", port: int = 80) -> bytes:
    return (
        bytes([int(AddrType.IPV4)])
        + ipaddress.IPv4Address(host).packed
        + struct.pack("!H", port)
    )


def _ipv6_addr_bytes(host: str = "::1", port: int = 443) -> bytes:
    return (
        bytes([int(AddrType.IPV6)])
        + ipaddress.IPv6Address(host).packed
        + struct.pack("!H", port)
    )


def _domain_addr_bytes(host: str = "example.com", port: int = 80) -> bytes:
    enc = host.encode()
    return bytes([int(AddrType.DOMAIN), len(enc)]) + enc + struct.pack("!H", port)


class TestReadExact:
    async def test_reads_exact_bytes(self):
        reader = make_stream_reader(b"hello world")
        result = await read_exact(reader, 5)
        assert result == b"hello"

    async def test_reads_all_available(self):
        reader = make_stream_reader(b"abcdef")
        result = await read_exact(reader, 6)
        assert result == b"abcdef"

    async def test_single_byte_read(self):
        reader = make_stream_reader(b"\xab\xcd")
        result = await read_exact(reader, 1)
        assert result == b"\xab"

    async def test_eof_mid_read_raises_protocol_error(self):
        reader = make_stream_reader(b"hi")
        with pytest.raises(ProtocolError):
            await read_exact(reader, 10)

    async def test_empty_stream_raises_protocol_error(self):
        reader = make_stream_reader(b"")
        with pytest.raises(ProtocolError):
            await read_exact(reader, 1)

    async def test_protocol_error_wraps_incomplete_read(self):
        reader = make_stream_reader(b"abc")
        with pytest.raises(ProtocolError) as exc_info:
            await read_exact(reader, 10)
        cause = exc_info.value.__cause__
        assert isinstance(cause, asyncio.IncompleteReadError)

    async def test_sequential_reads(self):
        reader = make_stream_reader(b"abcdef")
        first = await read_exact(reader, 3)
        second = await read_exact(reader, 3)
        assert first == b"abc"
        assert second == b"def"


class TestReadSocks5Addr:
    async def test_ipv4_happy_path(self):
        reader = make_stream_reader(_ipv4_addr_bytes("192.168.1.1", 8080))
        host, port = await read_socks5_addr(reader)
        assert host == "192.168.1.1"
        assert port == 8080

    async def test_ipv6_happy_path(self):
        reader = make_stream_reader(_ipv6_addr_bytes("2001:db8::1", 443))
        host, port = await read_socks5_addr(reader)
        assert host == "2001:db8::1"
        assert port == 443

    async def test_ipv6_compressed_form_returned(self):
        reader = make_stream_reader(_ipv6_addr_bytes("0:0:0:0:0:0:0:1", 80))
        host, _ = await read_socks5_addr(reader)
        assert host == "::1"

    async def test_domain_happy_path(self):
        reader = make_stream_reader(_domain_addr_bytes("redis.default.svc", 6379))
        host, port = await read_socks5_addr(reader)
        assert host == "redis.default.svc"
        assert port == 6379

    async def test_domain_with_underscore_passes(self):
        reader = make_stream_reader(_domain_addr_bytes("_dmarc.example.com", 80))
        host, _ = await read_socks5_addr(reader)
        assert host == "_dmarc.example.com"

    async def test_port_zero_raises_by_default(self):
        reader = make_stream_reader(_ipv4_addr_bytes("1.2.3.4", 0))
        with pytest.raises(ProtocolError):
            await read_socks5_addr(reader)

    async def test_port_zero_allowed_with_flag(self):
        reader = make_stream_reader(_ipv4_addr_bytes("1.2.3.4", 0))
        host, port = await read_socks5_addr(reader, allow_port_zero=True)
        assert port == 0

    async def test_unknown_atyp_raises_protocol_error(self):
        reader = make_stream_reader(bytes([0x02]) + b"\x00" * 6)
        with pytest.raises(ProtocolError):
            await read_socks5_addr(reader)

    async def test_atyp_0x05_raises_protocol_error(self):
        reader = make_stream_reader(bytes([0x05]) + b"\x00" * 6)
        with pytest.raises(ProtocolError):
            await read_socks5_addr(reader)

    async def test_ipv4_truncated_stream_raises(self):
        reader = make_stream_reader(bytes([0x01]) + b"\x01\x02\x03")
        with pytest.raises(ProtocolError):
            await read_socks5_addr(reader)

    async def test_ipv6_truncated_stream_raises(self):
        reader = make_stream_reader(bytes([0x04]) + b"\x00" * 10)
        with pytest.raises(ProtocolError):
            await read_socks5_addr(reader)

    async def test_domain_zero_length_raises(self):
        data = bytes([int(AddrType.DOMAIN), 0x00]) + struct.pack("!H", 80)
        reader = make_stream_reader(data)
        with pytest.raises(ProtocolError):
            await read_socks5_addr(reader)

    async def test_domain_truncated_body_raises(self):
        data = bytes([int(AddrType.DOMAIN), 20]) + b"tooshort" + struct.pack("!H", 80)
        reader = make_stream_reader(data)
        with pytest.raises(ProtocolError):
            await read_socks5_addr(reader)

    async def test_domain_invalid_label_raises(self):
        reader = make_stream_reader(_domain_addr_bytes("-invalid.example.com", 80))
        with pytest.raises(ProtocolError):
            await read_socks5_addr(reader)

    async def test_empty_stream_raises(self):
        reader = make_stream_reader(b"")
        with pytest.raises(ProtocolError):
            await read_socks5_addr(reader)


class TestCloseWriter:
    async def test_closes_open_writer(self, mock_writer):
        await close_writer(mock_writer)
        mock_writer.close.assert_called_once()
        mock_writer.wait_closed.assert_awaited_once()

    async def test_suppresses_os_error_on_close(self, mock_writer):
        mock_writer.close.side_effect = OSError("broken pipe")
        await close_writer(mock_writer)

    async def test_suppresses_os_error_on_wait_closed(self, mock_writer):
        mock_writer.wait_closed.side_effect = OSError("closed")
        await close_writer(mock_writer)

    async def test_suppresses_runtime_error(self, mock_writer):
        mock_writer.close.side_effect = RuntimeError("event loop closed")
        await close_writer(mock_writer)

    async def test_suppresses_runtime_error_on_wait_closed(self, mock_writer):
        mock_writer.wait_closed.side_effect = RuntimeError("loop gone")
        await close_writer(mock_writer)


class TestWriteAndDrainSilent:
    async def test_writes_and_drains(self, mock_writer):
        await write_and_drain_silent(mock_writer, b"hello")
        mock_writer.write.assert_called_once_with(b"hello")
        mock_writer.drain.assert_awaited_once()

    async def test_suppresses_os_error_on_write(self, mock_writer):
        mock_writer.write.side_effect = OSError("broken pipe")
        await write_and_drain_silent(mock_writer, b"data")

    async def test_suppresses_os_error_on_drain(self, mock_writer):
        mock_writer.drain.side_effect = OSError("broken pipe")
        await write_and_drain_silent(mock_writer, b"data")

    async def test_empty_bytes_written(self, mock_writer):
        await write_and_drain_silent(mock_writer, b"")
        mock_writer.write.assert_called_once_with(b"")
