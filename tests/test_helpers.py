"""Tests for exectunnel.helpers — frame codec and ID generation."""
from __future__ import annotations

from exectunnel.helpers import encode_frame, new_conn_id, new_flow_id, parse_frame

# ── encode_frame ──────────────────────────────────────────────────────────────


def test_encode_frame_with_payload() -> None:
    frame = encode_frame("DATA", "c1a2b3", "aGVsbG8=")
    assert frame == "<<<EXECTUNNEL:DATA:c1a2b3:aGVsbG8=>>>\n"


def test_encode_frame_no_payload() -> None:
    frame = encode_frame("CONN_CLOSE", "c1a2b3")
    assert frame == "<<<EXECTUNNEL:CONN_CLOSE:c1a2b3>>>\n"


def test_encode_frame_relay_ready() -> None:
    # AGENT_READY has no conn_id — conn_id can be empty string
    frame = encode_frame("AGENT_READY", "")
    assert frame == "<<<EXECTUNNEL:AGENT_READY:>>>\n"


# ── _parse_frame ──────────────────────────────────────────────────────────────


class TestParseFrame:
    def test_data_frame(self) -> None:
        result = parse_frame("<<<EXECTUNNEL:DATA:c1a2b3:aGVsbG8=>>>")
        assert result == ("DATA", "c1a2b3", "aGVsbG8=")

    def test_conn_close_frame(self) -> None:
        result = parse_frame("<<<EXECTUNNEL:CONN_CLOSE:c1a2b3>>>")
        assert result == ("CONN_CLOSE", "c1a2b3", "")

    def test_relay_ready(self) -> None:
        result = parse_frame("<<<EXECTUNNEL:AGENT_READY>>>")
        assert result == ("AGENT_READY", "", "")

    def test_payload_with_colon(self) -> None:
        # base64 payloads should not be split on internal colons
        result = parse_frame("<<<EXECTUNNEL:DATA:cXXX:abc:def>>>")
        assert result == ("DATA", "cXXX", "abc:def")

    def test_strips_whitespace(self) -> None:
        result = parse_frame("  <<<EXECTUNNEL:DATA:c1:abc>>>  ")
        assert result == ("DATA", "c1", "abc")

    def test_returns_none_for_garbage(self) -> None:
        assert parse_frame("not a frame") is None

    def test_returns_none_for_wrong_prefix(self) -> None:
        assert parse_frame("<<<OTHER:DATA:c1:abc>>>") is None

    def test_returns_none_for_missing_suffix(self) -> None:
        assert parse_frame("<<<EXECTUNNEL:DATA:c1:abc") is None

    def test_empty_string(self) -> None:
        assert parse_frame("") is None

    def test_newline_stripped(self) -> None:
        result = parse_frame("<<<EXECTUNNEL:CONN_ACK:c1>>>\n")
        assert result == ("CONN_ACK", "c1", "")


# ── ID generators ─────────────────────────────────────────────────────────────


def test_new_conn_id_prefix() -> None:
    for _ in range(20):
        cid = new_conn_id()
        assert cid.startswith("c"), f"expected 'c' prefix, got {cid!r}"
        assert len(cid) == 7  # 'c' + 6 hex chars


def test_new_flow_id_prefix() -> None:
    for _ in range(20):
        fid = new_flow_id()
        assert fid.startswith("u"), f"expected 'u' prefix, got {fid!r}"
        assert len(fid) == 7


def test_ids_are_unique() -> None:
    # Generate 500 IDs from a ~16M-value space (6 hex chars).
    # The birthday-paradox probability of ≥1 collision is ~0.75%, so we allow
    # ≤2 collisions to make the test robust while still catching broken RNG.
    ids = [new_conn_id() for _ in range(500)]
    duplicates = len(ids) - len(set(ids))
    assert duplicates <= 2, f"too many collisions in conn IDs: {duplicates}"
