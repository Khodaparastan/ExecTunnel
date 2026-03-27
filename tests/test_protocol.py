"""
Tests for the frame protocol — round-trip encode/decode, edge cases,
and tunnel exclusion logic.
"""
from __future__ import annotations

import ipaddress

import pytest

from exectunnel.helpers import encode_frame, is_host_excluded, parse_frame

# ── Round-trip ────────────────────────────────────────────────────────────────


class TestRoundTrip:
    @pytest.mark.parametrize(
        "msg_type,conn_id,payload",
        [
            ("DATA", "c1a2b3", "aGVsbG8="),
            ("CONN_ACK", "caabbcc", ""),
            ("CONN_CLOSE", "c000001", ""),
            ("CONN_CLOSED_ACK", "cdeadbe", ""),
            ("ERROR", "c123456", "c29tZXRoaW5nIHdlbnQgd3Jvbmc="),
            ("UDP_DATA", "u1a2b3c", "dGVzdA=="),
            ("UDP_OPEN", "u999999", "10.0.0.1:53"),
            ("UDP_CLOSE", "u888888", ""),
            ("UDP_CLOSED", "u777777", ""),
        ],
    )
    def test_round_trip(self, msg_type: str, conn_id: str, payload: str) -> None:
        frame = encode_frame(msg_type, conn_id, payload)
        # Strip trailing newline before parsing
        parsed = parse_frame(frame.rstrip("\n"))
        assert parsed is not None
        assert parsed == (msg_type, conn_id, payload)


# ── Edge cases ────────────────────────────────────────────────────────────────


class TestParseEdgeCases:
    def test_payload_preserves_colons(self) -> None:
        """Payload that contains ':' (e.g. host:port) must not be split."""
        frame = "<<<EXECTUNNEL:UDP_OPEN:u123456:10.0.0.1:53>>>"
        result = parse_frame(frame)
        assert result == ("UDP_OPEN", "u123456", "10.0.0.1:53")

    def test_ipv6_payload(self) -> None:
        """IPv6 address in payload contains many colons."""
        frame = "<<<EXECTUNNEL:CONN_OPEN:c123456:2001:db8::1:443>>>"
        result = parse_frame(frame)
        assert result is not None
        msg_type, conn_id, payload = result
        assert msg_type == "CONN_OPEN"
        assert conn_id == "c123456"
        assert payload == "2001:db8::1:443"

    def test_unknown_frame_type_parses_ok(self) -> None:
        """Unknown types are parsed but callers ignore them."""
        frame = "<<<EXECTUNNEL:FUTURE_TYPE:cXXXXXX:some_payload>>>"
        result = parse_frame(frame)
        assert result == ("FUTURE_TYPE", "cXXXXXX", "some_payload")

    def test_empty_conn_id(self) -> None:
        result = parse_frame("<<<EXECTUNNEL:AGENT_READY>>>")
        assert result == ("AGENT_READY", "", "")

    def test_frame_with_trailing_newline(self) -> None:
        result = parse_frame("<<<EXECTUNNEL:CONN_ACK:c1a2b3>>>\n")
        assert result == ("CONN_ACK", "c1a2b3", "")

    def test_frame_with_crlf(self) -> None:
        result = parse_frame("<<<EXECTUNNEL:CONN_ACK:c1a2b3>>>\r\n")
        assert result == ("CONN_ACK", "c1a2b3", "")

    def test_none_on_empty(self) -> None:
        assert parse_frame("") is None

    def test_none_on_partial_prefix(self) -> None:
        assert parse_frame("<<<EXECTUNNEL:") is None

    def test_none_on_shell_echo(self) -> None:
        """Shell echo lines must not be confused for frames."""
        assert parse_frame("stty -echo") is None
        assert parse_frame("$ exec python3 /tmp/ttyrelay.py") is None


# ── _is_excluded ──────────────────────────────────────────────────────────────


class TestIsExcluded:
    def _nets(
        self, *cidrs: str
    ) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(c, strict=False) for c in cidrs]

    def test_rfc1918_excluded(self) -> None:
        nets = self._nets("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        assert is_host_excluded("10.0.0.1", nets) is True
        assert is_host_excluded("172.31.255.255", nets) is True
        assert is_host_excluded("192.168.1.1", nets) is True

    def test_public_ip_not_excluded(self) -> None:
        nets = self._nets("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        assert is_host_excluded("8.8.8.8", nets) is False
        assert is_host_excluded("1.1.1.1", nets) is False

    def test_domain_names_not_excluded(self) -> None:
        """Domain names are always routed through the tunnel."""
        nets = self._nets("10.0.0.0/8")
        assert is_host_excluded("example.com", nets) is False
        assert is_host_excluded("kubernetes.default.svc", nets) is False

    def test_empty_exclusion_list(self) -> None:
        assert is_host_excluded("10.0.0.1", []) is False

    def test_loopback_excluded(self) -> None:
        nets = self._nets("127.0.0.0/8")
        assert is_host_excluded("127.0.0.1", nets) is True
        assert is_host_excluded("127.255.255.254", nets) is True

    def test_invalid_ip_string(self) -> None:
        """Garbage host strings must not raise; they return False."""
        nets = self._nets("10.0.0.0/8")
        assert is_host_excluded("not-an-ip", nets) is False
        assert is_host_excluded("", nets) is False
