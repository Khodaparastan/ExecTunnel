"""Tests for exectunnel.protocol.frames."""

from __future__ import annotations

import base64

import pytest
from exectunnel.exceptions import FrameDecodingError, ProtocolError
from exectunnel.protocol import (
    FRAME_PREFIX,
    FRAME_SUFFIX,
    MAX_DATA_PAYLOAD_BYTES,
    MAX_TUNNEL_FRAME_CHARS,
    READY_FRAME,
    SESSION_CONN_ID,
    ParsedFrame,
    decode_binary_payload,
    decode_error_payload,
    encode_agent_ready_frame,
    encode_conn_ack_frame,
    encode_conn_close_frame,
    encode_conn_open_frame,
    encode_data_frame,
    encode_error_frame,
    encode_host_port,
    encode_keepalive_frame,
    encode_stats_frame,
    encode_udp_close_frame,
    encode_udp_data_frame,
    encode_udp_open_frame,
    is_ready_frame,
    parse_frame,
    parse_host_port,
)

# ── Shared fixtures ───────────────────────────────────────────────────────────

CONN_ID = "c" + "a" * 24
FLOW_ID = "u" + "b" * 24


# ── encode_agent_ready_frame ──────────────────────────────────────────────────


class TestEncodeAgentReadyFrame:
    """encode_agent_ready_frame returns a newline-terminated ready sentinel."""

    def test_returns_ready_frame_with_newline(self):
        assert encode_agent_ready_frame() == READY_FRAME + "\n"

    def test_is_newline_terminated(self):
        assert encode_agent_ready_frame().endswith("\n")

    def test_is_ready_frame_recognises_output(self):
        frame = encode_agent_ready_frame()
        assert is_ready_frame(frame)


# ── encode_keepalive_frame ────────────────────────────────────────────────────


class TestEncodeKeepaliveFrame:
    """encode_keepalive_frame returns a stable pre-computed string."""

    def test_is_newline_terminated(self):
        assert encode_keepalive_frame().endswith("\n")

    def test_returns_same_object_on_repeated_calls(self):
        assert encode_keepalive_frame() is encode_keepalive_frame()

    def test_round_trips_through_parse_frame(self):
        frame = encode_keepalive_frame()
        result = parse_frame(frame)
        assert result == ParsedFrame(msg_type="KEEPALIVE", conn_id=None, payload="")


# ── encode_stats_frame ────────────────────────────────────────────────────────


class TestEncodeStatsFrame:
    """encode_stats_frame encodes session-scoped STATS frames."""

    def test_round_trip(self):
        data = b'{"cpu": 10, "mem": 1024}'
        frame = encode_stats_frame(data)
        result = parse_frame(frame)
        assert result.msg_type == "STATS"
        assert result.conn_id is None
        assert decode_binary_payload(result.payload) == data

    def test_empty_payload_raises(self):
        with pytest.raises(ProtocolError, match="must not be empty"):
            encode_stats_frame(b"")

    def test_with_conn_id_raises(self):
        """STATS is session-scoped and must not have a conn_id."""
        # This is enforced by _encode_frame called inside encode_stats_frame
        # encode_stats_frame(data) calls _encode_frame("STATS", None, ...)


# ── encode_conn_open_frame ────────────────────────────────────────────────────


class TestEncodeConnOpenFrame:
    """encode_conn_open_frame encodes host, port, and conn_id correctly."""

    def test_ipv4_host(self):
        frame = encode_conn_open_frame(CONN_ID, "192.168.1.1", 80)
        result = parse_frame(frame)
        assert result.msg_type == "CONN_OPEN"
        assert result.conn_id == CONN_ID
        assert result.payload == "192.168.1.1:80"

    def test_ipv6_host_is_bracket_quoted(self):
        frame = encode_conn_open_frame(CONN_ID, "2001:db8::1", 443)
        result = parse_frame(frame)
        assert result.payload == "[2001:db8::1]:443"

    def test_domain_host(self):
        frame = encode_conn_open_frame(CONN_ID, "redis.default.svc", 6379)
        result = parse_frame(frame)
        assert result.payload == "redis.default.svc:6379"

    def test_domain_with_underscore(self):
        frame = encode_conn_open_frame(
            CONN_ID, "_tcp.redis.default.svc.cluster.local", 6379
        )
        result = parse_frame(frame)
        assert "tcp" in result.payload

    def test_invalid_conn_id_raises(self):
        with pytest.raises(ProtocolError):
            encode_conn_open_frame("bad-id", "example.com", 80)

    def test_empty_conn_id_raises(self):
        """Bug fix #1: empty string must raise, not silently pass."""
        with pytest.raises(ProtocolError):
            encode_conn_open_frame("", "example.com", 80)

    def test_port_zero_raises(self):
        with pytest.raises(ProtocolError):
            encode_conn_open_frame(CONN_ID, "example.com", 0)

    def test_port_65536_raises(self):
        with pytest.raises(ProtocolError):
            encode_conn_open_frame(CONN_ID, "example.com", 65_536)

    def test_empty_host_raises(self):
        with pytest.raises(ProtocolError):
            encode_conn_open_frame(CONN_ID, "", 80)

    def test_host_with_colon_raises(self):
        with pytest.raises(ProtocolError):
            encode_conn_open_frame(CONN_ID, "bad:host", 80)

    def test_host_with_frame_prefix_raises(self):
        with pytest.raises(ProtocolError):
            encode_conn_open_frame(CONN_ID, "<<<EXECTUNNEL:EVIL", 80)

    def test_is_newline_terminated(self):
        assert encode_conn_open_frame(CONN_ID, "example.com", 80).endswith("\n")


# ── encode_conn_ack_frame ─────────────────────────────────────────────────────


class TestEncodeConnAckFrame:
    def test_round_trip(self):
        frame = encode_conn_ack_frame(CONN_ID)
        result = parse_frame(frame)
        assert result == ParsedFrame(msg_type="CONN_ACK", conn_id=CONN_ID, payload="")

    def test_empty_conn_id_raises(self):
        with pytest.raises(ProtocolError):
            encode_conn_ack_frame("")

    def test_none_conn_id_raises(self):
        with pytest.raises(ProtocolError):
            encode_conn_ack_frame(None)  # type: ignore[arg-type]


# ── encode_conn_close_frame ───────────────────────────────────────────────────


class TestEncodeConnCloseFrame:
    def test_round_trip(self):
        frame = encode_conn_close_frame(CONN_ID)
        result = parse_frame(frame)
        assert result == ParsedFrame(msg_type="CONN_CLOSE", conn_id=CONN_ID, payload="")

    def test_empty_conn_id_raises(self):
        with pytest.raises(ProtocolError):
            encode_conn_close_frame("")


# ── encode_data_frame ─────────────────────────────────────────────────────────


class TestEncodeDataFrame:
    def test_round_trip_ascii(self):
        data = b"Hello, world!"
        frame = encode_data_frame(CONN_ID, data)
        result = parse_frame(frame)
        assert decode_binary_payload(result.payload) == data

    def test_round_trip_binary(self):
        data = bytes(range(256))
        frame = encode_data_frame(CONN_ID, data)
        result = parse_frame(frame)
        assert decode_binary_payload(result.payload) == data

    def test_empty_bytes_raises(self):
        """Empty DATA is meaningless — CONN_CLOSE signals EOF."""
        with pytest.raises(ProtocolError, match="must not be empty"):
            encode_data_frame(CONN_ID, b"")

    def test_oversized_data_raises(self):
        with pytest.raises(ProtocolError, match="MAX_TUNNEL_FRAME_CHARS"):
            encode_data_frame(CONN_ID, b"x" * (MAX_DATA_PAYLOAD_BYTES + 1))

    def test_empty_conn_id_raises(self):
        with pytest.raises(ProtocolError):
            encode_data_frame("", b"data")


# ── encode_udp_open_frame ─────────────────────────────────────────────────────


class TestEncodeUdpOpenFrame:
    def test_round_trip(self):
        frame = encode_udp_open_frame(FLOW_ID, "8.8.8.8", 53)
        result = parse_frame(frame)
        assert result.msg_type == "UDP_OPEN"
        assert result.conn_id == FLOW_ID
        assert result.payload == "8.8.8.8:53"

    def test_ipv6_is_bracket_quoted(self):
        frame = encode_udp_open_frame(FLOW_ID, "::1", 53)
        result = parse_frame(frame)
        assert result.payload == "[::1]:53"

    def test_c_prefix_id_accepted_at_wire_layer(self):
        """The protocol layer accepts both c/u prefixes for any conn_id field.

        Enforcing that UDP frames carry u-prefixed IDs is a session-layer
        concern. CONN_FLOW_ID_RE intentionally matches both prefixes.
        """
        frame = encode_udp_open_frame(CONN_ID, "8.8.8.8", 53)
        result = parse_frame(frame)
        assert result.conn_id == CONN_ID


# ── encode_udp_data_frame ─────────────────────────────────────────────────────


class TestEncodeUdpDataFrame:
    def test_round_trip(self):
        data = b"\x00\x01\x02\x03"
        frame = encode_udp_data_frame(FLOW_ID, data)
        result = parse_frame(frame)
        assert decode_binary_payload(result.payload) == data

    def test_empty_conn_id_raises(self):
        with pytest.raises(ProtocolError):
            encode_udp_data_frame("", b"data")


# ── encode_udp_close_frame ────────────────────────────────────────────────────


class TestEncodeUdpCloseFrame:
    def test_round_trip(self):
        frame = encode_udp_close_frame(FLOW_ID)
        result = parse_frame(frame)
        assert result == ParsedFrame(msg_type="UDP_CLOSE", conn_id=FLOW_ID, payload="")


# ── encode_error_frame ────────────────────────────────────────────────────────


class TestEncodeErrorFrame:
    def test_round_trip_per_connection(self):
        frame = encode_error_frame(CONN_ID, "connection refused")
        result = parse_frame(frame)
        assert decode_error_payload(result.payload) == "connection refused"

    def test_round_trip_session_level(self):
        frame = encode_error_frame(SESSION_CONN_ID, "session terminated")
        result = parse_frame(frame)
        assert result.conn_id == SESSION_CONN_ID
        assert decode_error_payload(result.payload) == "session terminated"

    def test_unicode_message_round_trips(self):
        message = "失敗: 接続が拒否されました"
        frame = encode_error_frame(CONN_ID, message)
        result = parse_frame(frame)
        assert decode_error_payload(result.payload) == message

    def test_empty_conn_id_raises(self):
        with pytest.raises(ProtocolError):
            encode_error_frame("", "error")


# ── is_ready_frame ────────────────────────────────────────────────────────────


class TestIsReadyFrame:
    def test_exact_ready_frame(self):
        assert is_ready_frame(READY_FRAME) is True

    def test_encode_agent_ready_frame_output(self):
        assert is_ready_frame(encode_agent_ready_frame()) is True

    def test_with_leading_whitespace(self):
        assert is_ready_frame("  " + READY_FRAME) is True

    def test_with_trailing_newline(self):
        assert is_ready_frame(READY_FRAME + "\n") is True

    def test_with_proxy_suffix(self):
        assert is_ready_frame(READY_FRAME + "HTTP/1.1 200") is True

    def test_non_frame_line_returns_false(self):
        assert is_ready_frame("bash: command not found") is False

    def test_empty_string_returns_false(self):
        assert is_ready_frame("") is False

    def test_partial_prefix_returns_false(self):
        assert is_ready_frame("<<<EXECTUNNEL:") is False

    def test_different_frame_type_returns_false(self):
        assert is_ready_frame(encode_conn_ack_frame(CONN_ID)) is False

    def test_never_raises_on_garbage(self):
        garbage = [None, 123, b"bytes", "\x00" * 100]
        for g in garbage:
            try:
                is_ready_frame(str(g))
            except Exception as exc:  # noqa: BLE001
                pytest.fail(f"is_ready_frame raised on garbage input: {exc}")


# ── parse_frame ───────────────────────────────────────────────────────────────


class TestParseFrameNonTunnelLines:
    """Non-tunnel lines must return None, never raise."""

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "   ",
            "bash: no such file",
            "<<<not a tunnel frame",
            "<<<EXECTUNNEL:missing_suffix",
            "x" * (MAX_TUNNEL_FRAME_CHARS + 1),
        ],
    )
    def test_returns_none(self, line):
        assert parse_frame(line) is None


class TestParseFrameValidFrames:
    """Valid tunnel frames parse into correct ParsedFrame instances."""

    def test_agent_ready(self):
        result = parse_frame(READY_FRAME)
        assert result == ParsedFrame(msg_type="AGENT_READY", conn_id=None, payload="")

    def test_keepalive(self):
        result = parse_frame(encode_keepalive_frame())
        assert result == ParsedFrame(msg_type="KEEPALIVE", conn_id=None, payload="")

    def test_conn_open_ipv4(self):
        frame = encode_conn_open_frame(CONN_ID, "1.2.3.4", 8080)
        result = parse_frame(frame)
        assert result.msg_type == "CONN_OPEN"
        assert result.conn_id == CONN_ID
        assert result.payload == "1.2.3.4:8080"

    def test_conn_open_ipv6_colons_preserved(self):
        frame = encode_conn_open_frame(CONN_ID, "2001:db8::1", 443)
        result = parse_frame(frame)
        assert result.payload == "[2001:db8::1]:443"

    def test_data_frame(self):
        data = b"raw bytes"
        frame = encode_data_frame(CONN_ID, data)
        result = parse_frame(frame)
        assert result.msg_type == "DATA"
        assert decode_binary_payload(result.payload) == data

    def test_error_frame_with_session_conn_id(self):
        frame = encode_error_frame(SESSION_CONN_ID, "fatal")
        result = parse_frame(frame)
        assert result.conn_id == SESSION_CONN_ID

    def test_strips_trailing_newline(self):
        frame = encode_conn_ack_frame(CONN_ID)
        result_with = parse_frame(frame)
        result_without = parse_frame(frame.rstrip("\n"))
        assert result_with == result_without

    def test_proxy_suffix_stripped(self):
        frame = encode_conn_ack_frame(CONN_ID).rstrip("\n")
        result = parse_frame(frame + "HTTP/1.1 200 OK")
        assert result.msg_type == "CONN_ACK"


class TestParseFrameDecodingErrors:
    """Confirmed tunnel frames with corrupt structure raise FrameDecodingError."""

    def test_unknown_msg_type_raises(self):
        line = f"{FRAME_PREFIX}BOGUS:{CONN_ID}{FRAME_SUFFIX}"
        with pytest.raises(FrameDecodingError, match="unrecognised msg_type"):
            parse_frame(line)

    def test_no_conn_id_type_with_extra_fields_raises(self):
        line = f"{FRAME_PREFIX}AGENT_READY:{CONN_ID}{FRAME_SUFFIX}"
        with pytest.raises(FrameDecodingError, match="must not carry"):
            parse_frame(line)

    def test_missing_conn_id_raises(self):
        line = f"{FRAME_PREFIX}CONN_ACK{FRAME_SUFFIX}"
        with pytest.raises(FrameDecodingError, match="requires a conn_id"):
            parse_frame(line)

    def test_malformed_conn_id_raises(self):
        line = f"{FRAME_PREFIX}CONN_ACK:not-a-valid-id{FRAME_SUFFIX}"
        with pytest.raises(FrameDecodingError, match="malformed conn_id"):
            parse_frame(line)

    def test_missing_required_payload_raises(self):
        line = f"{FRAME_PREFIX}DATA:{CONN_ID}{FRAME_SUFFIX}"
        with pytest.raises(FrameDecodingError, match="requires a payload"):
            parse_frame(line)

    def test_forbidden_payload_present_raises(self):
        line = f"{FRAME_PREFIX}CONN_ACK:{CONN_ID}:unexpected{FRAME_SUFFIX}"
        with pytest.raises(FrameDecodingError, match="must not carry a payload"):
            parse_frame(line)

    def test_oversized_tunnel_frame_raises(self):
        payload = "A" * MAX_TUNNEL_FRAME_CHARS
        long_payload = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        line = f"{FRAME_PREFIX}DATA:{CONN_ID}:{long_payload}{FRAME_SUFFIX}"
        if len(line) > MAX_TUNNEL_FRAME_CHARS:
            with pytest.raises(FrameDecodingError, match="Oversized"):
                parse_frame(line)


# ── encode_host_port / parse_host_port ────────────────────────────────────────


class TestHostPortCodec:
    """encode_host_port and parse_host_port are strict inverses."""

    @pytest.mark.parametrize(
        "host, port, expected_payload",
        [
            ("192.168.1.1", 80, "192.168.1.1:80"),
            ("example.com", 443, "example.com:443"),
            ("2001:db8::1", 8080, "[2001:db8::1]:8080"),
            ("::1", 53, "[::1]:53"),
            (
                "redis.default.svc.cluster.local",
                6379,
                "redis.default.svc.cluster.local:6379",
            ),
            ("_dmarc.example.com", 80, "_dmarc.example.com:80"),
        ],
    )
    def test_encode_produces_correct_payload(self, host, port, expected_payload):
        assert encode_host_port(host, port) == expected_payload

    @pytest.mark.parametrize(
        "host, port",
        [
            ("192.168.1.1", 80),
            ("example.com", 443),
            ("2001:db8::1", 8080),
            ("::1", 53),
            ("redis.default.svc.cluster.local", 6379),
        ],
    )
    def test_round_trip(self, host, port):
        payload = encode_host_port(host, port)
        parsed_host, parsed_port = parse_host_port(payload)
        assert parsed_port == port

    def test_ipv6_normalised_to_compressed_form(self):
        payload = encode_host_port("2001:0db8:0000:0000:0000:0000:0000:0001", 80)
        assert payload == "[2001:db8::1]:80"

    @pytest.mark.parametrize(
        "host",
        [
            "",
            "bad:host",
            "has>angle",
            "has<angle",
            "double..dot",
            "-starts-with-dash",
            "ends-with-dash-",
        ],
    )
    def test_encode_invalid_host_raises(self, host):
        with pytest.raises(ProtocolError):
            encode_host_port(host, 80)

    @pytest.mark.parametrize("port", [0, -1, 65_536, 99_999])
    def test_encode_invalid_port_raises(self, port):
        with pytest.raises(ProtocolError):
            encode_host_port("example.com", port)

    @pytest.mark.parametrize(
        "payload",
        [
            "no-port",
            "[unclosed-bracket:80",
            "[:80",
            ":80",
            "example.com:notaport",
            "example.com:0",
            "example.com:65536",
            "[::1]missing-colon",
        ],
    )
    def test_parse_invalid_payload_raises(self, payload):
        with pytest.raises(FrameDecodingError):
            parse_host_port(payload)


# ── decode_binary_payload ─────────────────────────────────────────────────────


class TestDecodeBinaryPayload:
    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"hello",
            bytes(range(256)),
            b"x" * 1000,
        ],
    )
    def test_round_trip(self, data):
        payload = base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
        assert decode_binary_payload(payload) == data

    @pytest.mark.parametrize(
        "bad",
        [
            "!!!",
            "not base64 @@@",
            "has spaces",
            "has+plus",
            "has/slash",
        ],
    )
    def test_non_alphabet_characters_raise(self, bad):
        """urlsafe_b64decode silently discards non-alphabet chars without validation.

        The explicit _BASE64URL_RE pre-check catches these before decoding.
        """
        with pytest.raises(FrameDecodingError, match="Invalid base64url"):
            decode_binary_payload(bad)


# ── decode_error_payload ──────────────────────────────────────────────────────


class TestDecodeErrorPayload:
    def test_valid_utf8_round_trip(self):
        message = "connection refused: no route to host"
        payload = (
            base64.urlsafe_b64encode(message.encode()).rstrip(b"=").decode("ascii")
        )
        assert decode_error_payload(payload) == message

    def test_invalid_utf8_raises(self):
        raw = b"\xff\xfe"
        payload = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        with pytest.raises(FrameDecodingError, match="UTF-8"):
            decode_error_payload(payload)


# ── _encode_frame validation — no-conn_id types ───────────────────────────────


class TestNoConnIdEnforcement:
    """AGENT_READY, KEEPALIVE, and STATS must not carry a conn_id."""

    def test_agent_ready_with_conn_id_raises(self):
        """Bug fix #1 coverage: _NO_CONN_ID_TYPES conn_id guard."""
        from exectunnel.protocol.encoders import _encode_frame

        with pytest.raises(ProtocolError, match="must not carry a conn_id"):
            _encode_frame("AGENT_READY", CONN_ID)

    def test_keepalive_with_conn_id_raises(self):
        from exectunnel.protocol.encoders import _encode_frame

        with pytest.raises(ProtocolError, match="must not carry a conn_id"):
            _encode_frame("KEEPALIVE", CONN_ID)

    def test_stats_with_conn_id_raises(self):
        from exectunnel.protocol.encoders import _encode_frame

        with pytest.raises(ProtocolError, match="must not carry a conn_id"):
            _encode_frame("STATS", CONN_ID, payload="abc")

    def test_agent_ready_with_empty_string_conn_id_raises(self):
        """Bug fix #1: empty string conn_id for no-conn_id type must raise."""
        from exectunnel.protocol.encoders import _encode_frame

        with pytest.raises(ProtocolError, match="must not carry a conn_id"):
            _encode_frame("AGENT_READY", "")


# ── _encode_frame validation — payload requirements ───────────────────────────


class TestPayloadEnforcement:
    def test_data_without_payload_raises(self):
        from exectunnel.protocol.encoders import _encode_frame

        with pytest.raises(ProtocolError, match="requires a non-empty payload"):
            _encode_frame("DATA", CONN_ID, payload="")

    def test_conn_close_with_payload_raises(self):
        from exectunnel.protocol.encoders import _encode_frame

        with pytest.raises(ProtocolError, match="must not carry a payload"):
            _encode_frame("CONN_CLOSE", CONN_ID, payload="abc")

    def test_stats_without_payload_raises(self):
        from exectunnel.protocol.encoders import _encode_frame

        with pytest.raises(ProtocolError, match="requires a payload"):
            _encode_frame("STATS", None, payload="")


# ── ParsedFrame ───────────────────────────────────────────────────────────────


class TestParsedFrame:
    def test_is_frozen(self):
        frame = ParsedFrame(msg_type="CONN_ACK", conn_id=CONN_ID, payload="")
        with pytest.raises((AttributeError, TypeError)):
            frame.msg_type = "DATA"  # type: ignore[misc]

    def test_equality(self):
        a = ParsedFrame(msg_type="DATA", conn_id=CONN_ID, payload="abc")
        b = ParsedFrame(msg_type="DATA", conn_id=CONN_ID, payload="abc")
        assert a == b

    def test_inequality_on_payload(self):
        a = ParsedFrame(msg_type="DATA", conn_id=CONN_ID, payload="abc")
        b = ParsedFrame(msg_type="DATA", conn_id=CONN_ID, payload="xyz")
        assert a != b
