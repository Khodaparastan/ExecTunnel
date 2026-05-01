"""Unit tests for :mod:`exectunnel.protocol.parser`.

The encoder/parse round-trip is already exercised by ``test_frames.py``;
this module focuses on the parser's *non*-round-trip behaviour:

* ``parse_frame`` returns ``None`` for shell noise (no prefix).
* Proxy-injected suffix material after ``>>>`` is stripped.
* Oversize frames raise :class:`FrameDecodingError`.
* Unknown / malformed msg_type, conn_id, namespace, payload-required
  and payload-forbidden combinations all raise.
* ``is_ready_frame`` is a pure predicate that handles arbitrary
  pre-handshake garbage without raising.
"""

from __future__ import annotations

import pytest
from exectunnel.exceptions import FrameDecodingError
from exectunnel.protocol import (
    FRAME_PREFIX,
    FRAME_SUFFIX,
    MAX_TUNNEL_FRAME_CHARS,
    READY_FRAME,
    SESSION_CONN_ID,
    is_ready_frame,
    parse_frame,
)

CONN_ID = "c" + "a" * 24
FLOW_ID = "u" + "b" * 24


def _frame(inner: str) -> str:
    """Return ``<<<EXECTUNNEL:{inner}>>>\\n`` for hand-built test frames."""
    return f"{FRAME_PREFIX}{inner}{FRAME_SUFFIX}\n"


# ── Non-frame inputs ──────────────────────────────────────────────────────────


class TestNonFrameLines:
    """Lines without the tunnel prefix must return ``None``, not raise."""

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "   ",
            "shell prompt $ ",
            "[2025-01-01] some log line",
            "<<NOT_OUR_PREFIX:foo>>",  # similar but different prefix
            ">>><<<EXECTUNNEL:DATA",  # suffix-then-prefix garbage
            ">>>",
        ],
    )
    def test_returns_none(self, line: str) -> None:
        assert parse_frame(line) is None


# ── Proxy-injected suffix stripping ───────────────────────────────────────────


class TestProxySuffixStripping:
    """Some reverse proxies append text after the closing ``>>>``."""

    def test_keepalive_with_appended_garbage(self) -> None:
        # The parser must strip everything after the first complete
        # FRAME_SUFFIX.  Use ``find`` (first occurrence), not ``rfind``,
        # because the trailing material itself can contain ``>>>``.
        result = parse_frame(_frame("KEEPALIVE").rstrip("\n") + ">>> stale prompt $")
        assert result is not None
        assert result.msg_type == "KEEPALIVE"

    def test_data_frame_with_garbage_after_suffix(self) -> None:
        result = parse_frame(_frame(f"DATA:{CONN_ID}:aGVsbG8") + "  trailing\n")
        assert result is not None
        assert result.msg_type == "DATA"
        assert result.conn_id == CONN_ID


# ── Oversize protection ───────────────────────────────────────────────────────


class TestOversizeFrames:
    """Frames longer than ``MAX_TUNNEL_FRAME_CHARS`` must raise."""

    def test_oversize_with_prefix_raises(self) -> None:
        # Construct a frame whose length exceeds the limit *after* the
        # FRAME_SUFFIX — we make the payload the "extra".
        oversize_payload = "A" * (MAX_TUNNEL_FRAME_CHARS + 1)
        line = _frame(f"DATA:{CONN_ID}:{oversize_payload}")
        with pytest.raises(FrameDecodingError, match="Oversized"):
            parse_frame(line)

    def test_oversize_without_prefix_returns_none(self) -> None:
        # No prefix → not a tunnel frame at all.  Must not raise.
        assert parse_frame("X" * (MAX_TUNNEL_FRAME_CHARS + 1)) is None


# ── Structural malformations ──────────────────────────────────────────────────


class TestStructuralMalformations:
    """Lines that *look* like tunnel frames but fail validation must raise."""

    def test_unknown_msg_type_raises(self) -> None:
        with pytest.raises(FrameDecodingError, match="unrecognised msg_type"):
            parse_frame(_frame(f"FOO_BAR:{CONN_ID}:x"))

    def test_no_conn_id_type_with_extra_field_raises(self) -> None:
        # KEEPALIVE / LIVENESS / AGENT_READY accept neither conn_id nor
        # payload.
        with pytest.raises(FrameDecodingError, match="must not carry"):
            parse_frame(_frame(f"KEEPALIVE:{CONN_ID}"))

    def test_conn_open_without_conn_id_raises(self) -> None:
        with pytest.raises(FrameDecodingError, match="requires a conn_id"):
            parse_frame(_frame("CONN_OPEN"))

    def test_malformed_conn_id_raises(self) -> None:
        with pytest.raises(FrameDecodingError, match="malformed conn_id"):
            parse_frame(_frame("DATA:not-an-id:payload"))

    def test_tcp_frame_with_udp_prefix_id_raises(self) -> None:
        with pytest.raises(FrameDecodingError, match="TCP connection ID"):
            parse_frame(_frame(f"DATA:{FLOW_ID}:payload"))

    def test_udp_frame_with_tcp_prefix_id_raises(self) -> None:
        with pytest.raises(FrameDecodingError, match="UDP flow ID"):
            parse_frame(_frame(f"UDP_DATA:{CONN_ID}:payload"))

    def test_session_sentinel_outside_error_raises(self) -> None:
        # The session-level all-zero sentinel is only valid for ERROR
        # frames; using it for DATA must raise.
        with pytest.raises(FrameDecodingError, match="session-level sentinel"):
            parse_frame(_frame(f"DATA:{SESSION_CONN_ID}:payload"))

    def test_payload_required_but_empty_raises(self) -> None:
        # CONN_OPEN requires a payload.
        with pytest.raises(FrameDecodingError, match="requires a payload"):
            parse_frame(_frame(f"CONN_OPEN:{CONN_ID}:"))

    def test_payload_forbidden_but_present_raises(self) -> None:
        # CONN_ACK must not carry a payload.
        with pytest.raises(FrameDecodingError, match="must not carry"):
            parse_frame(_frame(f"CONN_ACK:{CONN_ID}:unwanted"))

    def test_no_conn_id_with_payload_but_empty_raises(self) -> None:
        # STATS requires a payload.
        with pytest.raises(FrameDecodingError, match="requires a payload"):
            parse_frame(_frame("STATS:"))


# ── Happy-path positive cases ─────────────────────────────────────────────────


class TestHappyPath:
    """Sanity-pin the parsed-shape contract for representative frames."""

    def test_agent_ready(self) -> None:
        result = parse_frame(_frame("AGENT_READY"))
        assert result is not None
        assert result.msg_type == "AGENT_READY"
        assert result.conn_id is None
        assert result.payload == ""

    def test_keepalive(self) -> None:
        result = parse_frame(_frame("KEEPALIVE"))
        assert result is not None
        assert result.msg_type == "KEEPALIVE"

    def test_liveness(self) -> None:
        result = parse_frame(_frame("LIVENESS"))
        assert result is not None
        assert result.msg_type == "LIVENESS"

    def test_stats_session_scoped(self) -> None:
        # STATS carries payload but no conn_id.
        result = parse_frame(_frame("STATS:eyJrZXkiOiJ2YWwifQ"))
        assert result is not None
        assert result.msg_type == "STATS"
        assert result.conn_id is None
        assert result.payload == "eyJrZXkiOiJ2YWwifQ"

    def test_session_sentinel_is_valid_for_error(self) -> None:
        result = parse_frame(_frame(f"ERROR:{SESSION_CONN_ID}:b29wcw"))
        assert result is not None
        assert result.msg_type == "ERROR"
        assert result.conn_id == SESSION_CONN_ID

    def test_payload_with_colons_preserved(self) -> None:
        # The split is bounded so payload may contain ':'.
        host_port = "[2001:db8::1]:443"
        result = parse_frame(_frame(f"CONN_OPEN:{CONN_ID}:{host_port}"))
        assert result is not None
        assert result.payload == host_port


# ── is_ready_frame predicate ──────────────────────────────────────────────────


class TestIsReadyFrame:
    @pytest.mark.parametrize(
        "line",
        [
            READY_FRAME,
            READY_FRAME + "\n",
            "  " + READY_FRAME,
            READY_FRAME + ">>> trailing prompt $",
        ],
    )
    def test_recognises(self, line: str) -> None:
        assert is_ready_frame(line) is True

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "AGENT_READY",
            "<<<EXECTUNNEL:KEEPALIVE>>>",
            "shell noise",
            "<<<EXECTUNNEL:NOT_READY>>>",
        ],
    )
    def test_rejects(self, line: str) -> None:
        assert is_ready_frame(line) is False

    def test_does_not_raise_on_arbitrary_input(self) -> None:
        # The bootstrap scanner must tolerate any pre-handshake garbage.
        is_ready_frame("\x00\x01\x02 binary trash" * 1000)
        is_ready_frame("")
