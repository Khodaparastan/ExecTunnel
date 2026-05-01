"""Unit tests for :mod:`exectunnel.protocol.codecs`.

The codec layer is the only piece of the protocol package that touches
binary data and host/port formatting; bugs here surface as silent wire
corruption.  These tests exercise every documented edge case in the
encode / parse pairs:

* :func:`encode_host_port` / :func:`parse_host_port` round-trip.
* IPv6 bracketing and compression.
* Hostname validation (RFC 1123 + underscore + Kubernetes service style).
* Frame-unsafe character rejection (``:``, ``<``, ``>``).
* Port range enforcement.
* ``encode_binary_payload`` / ``decode_binary_payload`` round-trip with
  no padding (RFC 4648 §5).
* :func:`decode_error_payload` UTF-8 wrapper behaviour.

Hypothesis-driven property tests for the binary codec live in
:mod:`tests.unit.protocol.test_codecs_property`.
"""

from __future__ import annotations

import pytest
from exectunnel.exceptions import FrameDecodingError, ProtocolError
from exectunnel.protocol.codecs import (
    decode_binary_payload,
    decode_error_payload,
    encode_binary_payload,
    encode_host_port,
    parse_host_port,
)

# ── encode_host_port ──────────────────────────────────────────────────────────


class TestEncodeHostPort:
    """``encode_host_port`` must accept canonical inputs and reject malicious ones."""

    @pytest.mark.parametrize(
        "host,port,expected",
        [
            ("8.8.8.8", 53, "8.8.8.8:53"),
            ("example.com", 80, "example.com:80"),
            # Kubernetes service-style underscore — explicitly permitted.
            ("my_service", 8080, "my_service:8080"),
            # IPv4 normalised through ipaddress.compressed (idempotent).
            ("127.0.0.1", 1, "127.0.0.1:1"),
            ("127.0.0.1", 65535, "127.0.0.1:65535"),
            # IPv6 bracketed and compressed.
            ("::1", 80, "[::1]:80"),
            ("2001:db8::1", 443, "[2001:db8::1]:443"),
            ("2001:0db8:0000:0000:0000:0000:0000:0001", 443, "[2001:db8::1]:443"),
        ],
    )
    def test_round_trip_through_parse(
        self, host: str, port: int, expected: str
    ) -> None:
        encoded = encode_host_port(host, port)
        assert encoded == expected
        parsed_host, parsed_port = parse_host_port(encoded)
        # The decoded host equals the encoder's normalised form, not
        # necessarily the original input — that is the documented
        # contract (e.g. IPv6 compression).  We re-encode the parsed
        # value to compare against the canonical form.
        assert parsed_port == port
        assert encode_host_port(parsed_host, parsed_port) == encoded

    def test_rejects_empty_host(self) -> None:
        with pytest.raises(ProtocolError, match="must not be empty"):
            encode_host_port("", 80)

    @pytest.mark.parametrize("port", [-1, 0, 65536, 100_000])
    def test_rejects_out_of_range_port(self, port: int) -> None:
        with pytest.raises(ProtocolError, match="out of range"):
            encode_host_port("example.com", port)

    @pytest.mark.parametrize(
        "host",
        [
            "evil:host",  # frame-unsafe ':'
            "<script>",  # frame-unsafe '<' '>'
            "host>tag",
            "double..dot.example",  # consecutive dots
            ".starts-with-dot",
            "ends-with-dot.",
            "spaces in host",
        ],
    )
    def test_rejects_invalid_hostnames(self, host: str) -> None:
        with pytest.raises(ProtocolError):
            encode_host_port(host, 80)


# ── parse_host_port ───────────────────────────────────────────────────────────


class TestParseHostPort:
    """``parse_host_port`` must reject everything that isn't canonical."""

    @pytest.mark.parametrize(
        "payload",
        [
            "no-port",  # missing colon
            ":80",  # empty host
            "[::1",  # missing closing bracket
            "[::1]",  # missing port after bracket
            "[::1]80",  # missing colon after bracket
            "[example.com]:80",  # bracketed non-IP
            "[127.0.0.1]:80",  # bracketed IPv4 (must be IPv6)
            "host:notaport",  # non-numeric port
            "host:99999",  # port > 65535
            "host:0",  # port == 0
            "::1:80",  # IPv6 must be bracketed
        ],
    )
    def test_rejects_malformed(self, payload: str) -> None:
        with pytest.raises(FrameDecodingError):
            parse_host_port(payload)

    def test_round_trips_max_port(self) -> None:
        host, port = parse_host_port("example.com:65535")
        assert (host, port) == ("example.com", 65535)

    def test_ipv6_compressed_round_trip(self) -> None:
        host, port = parse_host_port("[2001:db8::1]:443")
        assert (host, port) == ("2001:db8::1", 443)


# ── encode_binary_payload / decode_binary_payload ─────────────────────────────


class TestBinaryCodec:
    """base64url, no padding (RFC 4648 §5)."""

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"a",
            b"ab",
            b"abc",
            b"abcd",
            b"\x00\x01\x02\x03",
            bytes(range(256)),
            b"\xff" * 1024,
        ],
    )
    def test_round_trip(self, data: bytes) -> None:
        assert decode_binary_payload(encode_binary_payload(data)) == data

    def test_encoder_does_not_emit_padding(self) -> None:
        # All inputs of length not divisible by 3 would normally have
        # one or two ``=`` chars; the encoder strips them.
        assert "=" not in encode_binary_payload(b"abcd")
        assert "=" not in encode_binary_payload(b"ab")

    def test_encoder_uses_urlsafe_alphabet(self) -> None:
        # 0xFF / 0xFE are the bytes that would otherwise produce ``+`` / ``/``
        # in standard base64; verify the urlsafe alphabet is used.
        encoded = encode_binary_payload(b"\xff\xfe\xfd")
        assert "+" not in encoded
        assert "/" not in encoded

    @pytest.mark.parametrize(
        "payload",
        [
            "+invalid",  # standard-base64 char outside urlsafe set
            "abc/def",  # standard-base64 '/'
            "abc def",  # space
            "ab==cd",  # padding mid-string
            "AB CD",
            "—em-dash",  # non-ASCII
        ],
    )
    def test_rejects_non_base64url(self, payload: str) -> None:
        with pytest.raises(FrameDecodingError):
            decode_binary_payload(payload)


# ── decode_error_payload ──────────────────────────────────────────────────────


class TestDecodeErrorPayload:
    """``decode_error_payload`` is a UTF-8 wrapper around the binary codec."""

    @pytest.mark.parametrize(
        "message",
        [
            "connection refused",
            "ホストに到達できません",
            "🚫 access denied",
            "",
        ],
    )
    def test_round_trip(self, message: str) -> None:
        encoded = encode_binary_payload(message.encode("utf-8"))
        assert decode_error_payload(encoded) == message

    def test_rejects_invalid_utf8(self) -> None:
        """Invalid UTF-8 surfaces as :class:`FrameDecodingError`.

        The codec wraps the underlying :class:`UnicodeDecodeError` in a
        :class:`FrameDecodingError` so callers handle a single exception
        family for any wire-format problem.
        """
        # 0xff is never valid as the first byte of a UTF-8 sequence.
        encoded = encode_binary_payload(b"\xff\xfe\xfd")
        with pytest.raises(FrameDecodingError, match="UTF-8"):
            decode_error_payload(encoded)
