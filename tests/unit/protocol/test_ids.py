"""Tests for exectunnel.protocol.ids."""

from __future__ import annotations

import re

import pytest
from exectunnel.protocol import (
    CONN_FLOW_ID_RE,
    ID_RE,
    SESSION_CONN_ID,
    SESSION_ID_RE,
    new_conn_id,
    new_flow_id,
    new_session_id,
)


class TestGenerators:
    """ID generators produce correctly-formatted, unique IDs."""

    def test_new_conn_id_prefix(self):
        assert new_conn_id().startswith("c")

    def test_new_flow_id_prefix(self):
        assert new_flow_id().startswith("u")

    def test_new_session_id_prefix(self):
        assert new_session_id().startswith("s")

    def test_new_conn_id_length(self):
        assert len(new_conn_id()) == 25

    def test_new_flow_id_length(self):
        assert len(new_flow_id()) == 25

    def test_new_session_id_length(self):
        assert len(new_session_id()) == 25

    def test_new_conn_id_matches_pattern(self):
        assert CONN_FLOW_ID_RE.match(new_conn_id())

    def test_new_flow_id_matches_pattern(self):
        assert CONN_FLOW_ID_RE.match(new_flow_id())

    def test_new_session_id_matches_pattern(self):
        assert SESSION_ID_RE.match(new_session_id())

    def test_new_conn_id_uniqueness(self):
        ids = {new_conn_id() for _ in range(1000)}
        assert len(ids) == 1000

    def test_new_flow_id_uniqueness(self):
        ids = {new_flow_id() for _ in range(1000)}
        assert len(ids) == 1000

    def test_new_session_id_uniqueness(self):
        ids = {new_session_id() for _ in range(1000)}
        assert len(ids) == 1000

    def test_conn_id_is_lowercase_hex(self):
        assert re.fullmatch(r"c[0-9a-f]{24}", new_conn_id())

    def test_flow_id_is_lowercase_hex(self):
        assert re.fullmatch(r"u[0-9a-f]{24}", new_flow_id())

    def test_session_id_is_lowercase_hex(self):
        assert re.fullmatch(r"s[0-9a-f]{24}", new_session_id())


class TestPatterns:
    """CONN_FLOW_ID_RE and SESSION_ID_RE accept and reject correctly."""

    @pytest.mark.parametrize(
        "value",
        [
            "c" + "a" * 24,
            "c" + "0" * 24,
            "u" + "f" * 24,
            "u" + "1234567890abcdef" * 1 + "12345678",
        ],
    )
    def test_conn_flow_id_re_accepts_valid(self, value):
        assert CONN_FLOW_ID_RE.match(value)

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "c" + "a" * 23,
            "c" + "a" * 25,
            "s" + "a" * 24,
            "C" + "a" * 24,
            "c" + "A" * 24,
            "c" + "g" * 24,
            "ca1b2c3d4e5f6a7b8c9d0e1g",
        ],
    )
    def test_conn_flow_id_re_rejects_invalid(self, value):
        assert not CONN_FLOW_ID_RE.match(value)

    @pytest.mark.parametrize(
        "value",
        [
            "s" + "0" * 24,
            "s" + "abcdef1234567890" * 1 + "abcdef12",
        ],
    )
    def test_session_id_re_accepts_valid(self, value):
        assert SESSION_ID_RE.match(value)

    @pytest.mark.parametrize(
        "value",
        [
            "c" + "0" * 24,
            "u" + "0" * 24,
            "s" + "0" * 23,
            "s" + "0" * 25,
            "S" + "0" * 24,
        ],
    )
    def test_session_id_re_rejects_invalid(self, value):
        assert not SESSION_ID_RE.match(value)


class TestSentinel:
    """SESSION_CONN_ID is a valid CONN_FLOW_ID_RE-shaped sentinel."""

    def test_session_conn_id_matches_conn_flow_id_re(self):
        assert CONN_FLOW_ID_RE.match(SESSION_CONN_ID)

    def test_session_conn_id_does_not_match_session_id_re(self):
        assert not SESSION_ID_RE.match(SESSION_CONN_ID)

    def test_session_conn_id_format(self):
        assert SESSION_CONN_ID == "c" + "0" * 24

    def test_session_conn_id_not_produced_by_new_conn_id(self):
        ids = {new_conn_id() for _ in range(10_000)}
        assert SESSION_CONN_ID not in ids


class TestDeprecatedAlias:
    """ID_RE is the same object as CONN_FLOW_ID_RE."""

    def test_id_re_is_conn_flow_id_re(self):
        assert ID_RE is CONN_FLOW_ID_RE
