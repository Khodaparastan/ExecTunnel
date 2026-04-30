"""Tests for exectunnel.proxy._wire."""

from __future__ import annotations

import ipaddress
import struct

import pytest
from exectunnel.exceptions import ConfigurationError, ProtocolError
from exectunnel.protocol import AddrType, Reply
from exectunnel.proxy._wire import (
    build_socks5_reply,
    build_udp_header,
    parse_socks5_addr_buf,
    parse_udp_header,
    validate_socks5_domain,
)


def _ipv4_buf(host: str = "1.2.3.4", port: int = 80) -> bytes:
    return bytes([0x01]) + ipaddress.IPv4Address(host).packed + struct.pack("!H", port)


def _ipv6_buf(host: str = "::1", port: int = 443) -> bytes:
    return bytes([0x04]) + ipaddress.IPv6Address(host).packed + struct.pack("!H", port)


def _domain_buf(host: str = "example.com", port: int = 80) -> bytes:
    enc = host.encode()
    return bytes([0x03, len(enc)]) + enc + struct.pack("!H", port)


def _udp_dgram(addr_buf: bytes, payload: bytes = b"payload", frag: int = 0) -> bytes:
    return b"\x00\x00" + bytes([frag]) + addr_buf + payload


class TestValidateSocks5Domain:
    def test_simple_domain_passes(self):
        validate_socks5_domain("example.com")

    def test_subdomain_passes(self):
        validate_socks5_domain("redis.default.svc.cluster.local")

    def test_single_label_passes(self):
        validate_socks5_domain("localhost")

    def test_underscore_prefix_passes(self):
        validate_socks5_domain("_dmarc.example.com")

    def test_hyphen_interior_passes(self):
        validate_socks5_domain("my-service.example.com")

    def test_numeric_label_passes(self):
        validate_socks5_domain("123.example.com")

    def test_single_char_label_passes(self):
        validate_socks5_domain("a.b.c")

    def test_trailing_dot_stripped_and_valid(self):
        validate_socks5_domain("example.com.")

    def test_exactly_253_chars_passes(self):
        domain = "a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 61
        assert len(domain) == 253
        validate_socks5_domain(domain)

    def test_254_chars_raises(self):
        domain = "a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 62
        assert len(domain) == 254
        with pytest.raises(ProtocolError):
            validate_socks5_domain(domain)

    @pytest.mark.parametrize("char", [":", "<", ">", "\x00", "\r", "\n"])
    def test_frame_unsafe_char_raises(self, char: str):
        with pytest.raises(ProtocolError):
            validate_socks5_domain(f"example{char}com")

    def test_leading_dot_raises(self):
        with pytest.raises(ProtocolError):
            validate_socks5_domain(".example.com")

    def test_consecutive_dots_raise(self):
        with pytest.raises(ProtocolError):
            validate_socks5_domain("example..com")

    def test_label_starts_with_hyphen_raises(self):
        with pytest.raises(ProtocolError):
            validate_socks5_domain("-bad.example.com")

    def test_label_ends_with_hyphen_raises(self):
        with pytest.raises(ProtocolError):
            validate_socks5_domain("bad-.example.com")

    def test_label_64_chars_raises(self):
        with pytest.raises(ProtocolError):
            validate_socks5_domain("a" * 64 + ".example.com")

    def test_label_63_chars_passes(self):
        validate_socks5_domain("a" * 63 + ".example.com")


class TestParseSocks5AddrBuf:
    def test_ipv4_returns_host_port_offset(self):
        data = _ipv4_buf("192.168.1.1", 8080)
        host, port, offset = parse_socks5_addr_buf(data, 0)
        assert host == "192.168.1.1"
        assert port == 8080
        assert offset == 7  # 1 ATYP + 4 addr + 2 port

    def test_ipv6_returns_host_port_offset(self):
        data = _ipv6_buf("2001:db8::1", 443)
        host, port, offset = parse_socks5_addr_buf(data, 0)
        assert host == "2001:db8::1"
        assert port == 443
        assert offset == 19  # 1 + 16 + 2

    def test_ipv6_returned_in_compressed_form(self):
        data = _ipv6_buf("0:0:0:0:0:0:0:1", 80)
        host, _, _ = parse_socks5_addr_buf(data, 0)
        assert host == "::1"

    def test_domain_returns_host_port_offset(self):
        data = _domain_buf("example.com", 9090)
        host, port, offset = parse_socks5_addr_buf(data, 0)
        assert host == "example.com"
        assert port == 9090
        assert offset == 1 + 1 + len(b"example.com") + 2

    def test_non_zero_offset_parsed_correctly(self):
        prefix = b"\xde\xad\xbe"
        data = prefix + _ipv4_buf("1.2.3.4", 80)
        host, port, offset = parse_socks5_addr_buf(data, 3)
        assert host == "1.2.3.4"
        assert port == 80
        assert offset == 10  # 3 prefix + 1 ATYP + 4 addr + 2 port

    def test_context_string_appears_in_error(self):
        with pytest.raises(ProtocolError) as exc_info:
            parse_socks5_addr_buf(b"", 0, context="MY_CONTEXT")
        assert "MY_CONTEXT" in exc_info.value.args[0]

    def test_trailing_bytes_not_consumed(self):
        data = _ipv4_buf() + b"\xff\xff"
        _, _, offset = parse_socks5_addr_buf(data, 0)
        assert data[offset:] == b"\xff\xff"

    def test_domain_trailing_bytes_not_consumed(self):
        data = _domain_buf() + b"\xab\xcd"
        _, _, offset = parse_socks5_addr_buf(data, 0)
        assert data[offset:] == b"\xab\xcd"

    def test_truncated_before_atyp_raises(self):
        with pytest.raises(ProtocolError):
            parse_socks5_addr_buf(b"", 0)

    def test_unknown_atyp_0x02_raises(self):
        with pytest.raises(ProtocolError):
            parse_socks5_addr_buf(bytes([0x02]) + b"\x00" * 6, 0)

    def test_unknown_atyp_0x05_raises(self):
        with pytest.raises(ProtocolError):
            parse_socks5_addr_buf(bytes([0x05]) + b"\x00" * 6, 0)

    def test_ipv4_truncated_address_raises(self):
        with pytest.raises(ProtocolError):
            parse_socks5_addr_buf(bytes([0x01]) + b"\x01\x02\x03", 0)

    def test_ipv6_truncated_address_raises(self):
        with pytest.raises(ProtocolError):
            parse_socks5_addr_buf(bytes([0x04]) + b"\x00" * 10, 0)

    def test_domain_no_length_byte_raises(self):
        with pytest.raises(ProtocolError):
            parse_socks5_addr_buf(bytes([0x03]), 0)

    def test_domain_zero_length_raises(self):
        with pytest.raises(ProtocolError):
            parse_socks5_addr_buf(bytes([0x03, 0x00]) + struct.pack("!H", 80), 0)

    def test_domain_truncated_body_raises(self):
        with pytest.raises(ProtocolError):
            parse_socks5_addr_buf(bytes([0x03, 0x0A]) + b"hello", 0)

    def test_domain_non_utf8_raises(self):
        invalid = bytes([0xFF, 0xFE])
        data = bytes([0x03, len(invalid)]) + invalid + struct.pack("!H", 80)
        with pytest.raises(ProtocolError):
            parse_socks5_addr_buf(data, 0)

    def test_domain_invalid_label_raises(self):
        bad = b"-starts-with-hyphen.example.com"
        data = bytes([0x03, len(bad)]) + bad + struct.pack("!H", 80)
        with pytest.raises(ProtocolError):
            parse_socks5_addr_buf(data, 0)

    def test_truncated_before_port_raises(self):
        with pytest.raises(ProtocolError):
            parse_socks5_addr_buf(bytes([0x01]) + b"\x01\x02\x03\x04", 0)

    def test_port_zero_raises_by_default(self):
        data = _ipv4_buf("1.2.3.4", 0)
        with pytest.raises(ProtocolError):
            parse_socks5_addr_buf(data, 0)

    def test_port_zero_allowed_with_flag(self):
        data = _ipv4_buf("1.2.3.4", 0)
        _, port, _ = parse_socks5_addr_buf(data, 0, allow_port_zero=True)
        assert port == 0

    def test_port_65535_passes(self):
        data = _ipv4_buf("1.2.3.4", 65_535)
        _, port, _ = parse_socks5_addr_buf(data, 0)
        assert port == 65_535

    def test_port_1_passes(self):
        data = _ipv4_buf("1.2.3.4", 1)
        _, port, _ = parse_socks5_addr_buf(data, 0)
        assert port == 1


class TestParseUdpHeader:
    def test_ipv4_returns_payload_host_port(self):
        dgram = _udp_dgram(_ipv4_buf("10.0.0.1", 53), b"dns-payload")
        payload, host, port = parse_udp_header(dgram)
        assert payload == b"dns-payload"
        assert host == "10.0.0.1"
        assert port == 53

    def test_ipv6_returns_payload_host_port(self):
        dgram = _udp_dgram(_ipv6_buf("::1", 443), b"data")
        payload, host, port = parse_udp_header(dgram)
        assert payload == b"data"
        assert host == "::1"
        assert port == 443

    def test_domain_returns_payload_host_port(self):
        dgram = _udp_dgram(_domain_buf("example.com", 80), b"http")
        payload, host, port = parse_udp_header(dgram)
        assert payload == b"http"
        assert host == "example.com"
        assert port == 80

    def test_empty_payload_returned(self):
        dgram = _udp_dgram(_ipv4_buf(), b"")
        payload, _, _ = parse_udp_header(dgram)
        assert payload == b""

    def test_addr_bytes_excluded_from_payload(self):
        addr_buf = _ipv4_buf("1.2.3.4", 80)
        payload_data = b"hello world"
        dgram = b"\x00\x00\x00" + addr_buf + payload_data
        payload, _, _ = parse_udp_header(dgram)
        assert payload == payload_data

    def test_too_short_raises(self):
        with pytest.raises(ProtocolError):
            parse_udp_header(b"\x00\x00\x00")

    def test_non_zero_rsv_raises(self):
        dgram = b"\x00\x01\x00" + _ipv4_buf()
        with pytest.raises(ProtocolError):
            parse_udp_header(dgram)

    def test_rsv_first_byte_nonzero_raises(self):
        dgram = b"\x01\x00\x00" + _ipv4_buf()
        with pytest.raises(ProtocolError):
            parse_udp_header(dgram)

    def test_non_zero_frag_raises(self):
        dgram = b"\x00\x00\x01" + _ipv4_buf()
        with pytest.raises(ProtocolError):
            parse_udp_header(dgram)

    def test_invalid_atyp_raises(self):
        dgram = b"\x00\x00\x00" + bytes([0x02]) + b"\x00" * 6
        with pytest.raises(ProtocolError):
            parse_udp_header(dgram)


class TestBuildUdpHeader:
    def test_ipv4_wire_structure(self):
        addr = ipaddress.IPv4Address("1.2.3.4").packed
        result = build_udp_header(AddrType.IPV4, addr, 80)
        assert result[:3] == b"\x00\x00\x00"
        assert result[3] == int(AddrType.IPV4)
        assert result[4:8] == addr
        assert result[8:] == struct.pack("!H", 80)
        assert len(result) == 10

    def test_ipv6_wire_structure(self):
        addr = ipaddress.IPv6Address("::1").packed
        result = build_udp_header(AddrType.IPV6, addr, 443)
        assert result[:3] == b"\x00\x00\x00"
        assert result[3] == int(AddrType.IPV6)
        assert result[4:20] == addr
        assert result[20:] == struct.pack("!H", 443)
        assert len(result) == 22

    def test_port_zero_is_valid(self):
        addr = ipaddress.IPv4Address("0.0.0.0").packed
        result = build_udp_header(AddrType.IPV4, addr, 0)
        assert result[-2:] == b"\x00\x00"

    def test_port_65535_is_valid(self):
        addr = ipaddress.IPv4Address("0.0.0.0").packed
        result = build_udp_header(AddrType.IPV4, addr, 65_535)
        assert struct.unpack("!H", result[-2:])[0] == 65_535

    def test_domain_atyp_raises(self):
        with pytest.raises(ConfigurationError):
            build_udp_header(AddrType.DOMAIN, b"example", 80)

    def test_ipv4_wrong_addr_length_raises(self):
        with pytest.raises(ConfigurationError):
            build_udp_header(AddrType.IPV4, b"\x01\x02\x03", 80)

    def test_ipv6_wrong_addr_length_raises(self):
        with pytest.raises(ConfigurationError):
            build_udp_header(AddrType.IPV6, b"\x00" * 15, 80)

    def test_port_negative_raises(self):
        with pytest.raises(ConfigurationError):
            build_udp_header(AddrType.IPV4, ipaddress.IPv4Address("0.0.0.0").packed, -1)

    def test_port_above_65535_raises(self):
        with pytest.raises(ConfigurationError):
            build_udp_header(
                AddrType.IPV4, ipaddress.IPv4Address("0.0.0.0").packed, 65_536
            )


class TestBuildSocks5Reply:
    def test_success_default_structure(self):
        result = build_socks5_reply(Reply.SUCCESS)
        assert result[0] == 0x05
        assert result[1] == int(Reply.SUCCESS)
        assert result[2] == 0x00
        assert result[3] == int(AddrType.IPV4)
        assert result[4:8] == b"\x00\x00\x00\x00"
        assert result[8:] == b"\x00\x00"
        assert len(result) == 10

    def test_general_failure_reply_code(self):
        result = build_socks5_reply(Reply.GENERAL_FAILURE)
        assert result[1] == int(Reply.GENERAL_FAILURE)

    def test_cmd_not_supported_reply_code(self):
        result = build_socks5_reply(Reply.CMD_NOT_SUPPORTED)
        assert result[1] == int(Reply.CMD_NOT_SUPPORTED)

    def test_host_unreachable_reply_code(self):
        result = build_socks5_reply(Reply.HOST_UNREACHABLE)
        assert result[1] == int(Reply.HOST_UNREACHABLE)

    def test_custom_ipv4_bind_host_and_port(self):
        result = build_socks5_reply(
            Reply.SUCCESS, bind_host="127.0.0.1", bind_port=1080
        )
        assert result[3] == int(AddrType.IPV4)
        assert result[4:8] == ipaddress.IPv4Address("127.0.0.1").packed
        assert struct.unpack("!H", result[8:])[0] == 1080

    def test_ipv6_bind_host_uses_ipv6_atyp(self):
        result = build_socks5_reply(Reply.SUCCESS, bind_host="::1")
        assert result[3] == int(AddrType.IPV6)
        assert result[4:20] == ipaddress.IPv6Address("::1").packed
        assert len(result) == 22

    def test_port_65535_serialised_correctly(self):
        result = build_socks5_reply(Reply.SUCCESS, bind_port=65_535)
        assert struct.unpack("!H", result[-2:])[0] == 65_535

    def test_port_zero_is_valid(self):
        result = build_socks5_reply(Reply.SUCCESS, bind_port=0)
        assert result[-2:] == b"\x00\x00"

    def test_hostname_bind_host_raises(self):
        with pytest.raises(ConfigurationError):
            build_socks5_reply(Reply.SUCCESS, bind_host="localhost")

    def test_invalid_bind_host_raises(self):
        with pytest.raises(ConfigurationError):
            build_socks5_reply(Reply.SUCCESS, bind_host="not-an-ip")

    def test_bind_port_negative_raises(self):
        with pytest.raises(ConfigurationError):
            build_socks5_reply(Reply.SUCCESS, bind_port=-1)

    def test_bind_port_above_65535_raises(self):
        with pytest.raises(ConfigurationError):
            build_socks5_reply(Reply.SUCCESS, bind_port=65_536)
