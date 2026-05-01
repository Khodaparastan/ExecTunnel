"""Unit tests for :mod:`exectunnel.observability.utils`.

The env-var parsing helpers are tiny but heavily relied on at module
import time (they read ``EXECTUNNEL_*`` knobs in default config
construction).  We pin every documented branch:

* well-formed values are accepted.
* empty / unset → default.
* non-numeric / non-finite → default + warning.
* below ``min_value`` / above ``max_value`` → default + warning.
* the *default* itself is clamped to the bounds (callers cannot
  accidentally return an out-of-range fallback).
"""

from __future__ import annotations

import logging

import pytest
from exectunnel.observability.utils import (
    parse_bool_env,
    parse_float_env,
    parse_int_env,
)

# ── parse_bool_env ────────────────────────────────────────────────────────────


class TestParseBoolEnv:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1", True),
            ("true", True),
            ("TRUE", True),
            ("yes", True),
            ("on", True),
            ("0", False),
            ("false", False),
            ("FALSE", False),
            ("no", False),
            ("off", False),
            ("", False),
        ],
    )
    def test_recognised_values(
        self, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
    ) -> None:
        monkeypatch.setenv("MY_FLAG", value)
        assert parse_bool_env("MY_FLAG") is expected

    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MY_FLAG", raising=False)
        assert parse_bool_env("MY_FLAG", default=True) is True
        assert parse_bool_env("MY_FLAG", default=False) is False

    def test_garbage_falls_back_with_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("MY_FLAG", "maybe")
        caplog.set_level(logging.WARNING, "exectunnel.observability.utils")
        assert parse_bool_env("MY_FLAG", default=True) is True
        assert any("Invalid boolean" in r.message for r in caplog.records)

    def test_strips_whitespace_and_lowercases(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MY_FLAG", "  TrUe  ")
        assert parse_bool_env("MY_FLAG") is True


# ── parse_int_env ─────────────────────────────────────────────────────────────


class TestParseIntEnv:
    def test_accepts_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("X", "42")
        assert parse_int_env("X", default=0) == 42

    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("X", raising=False)
        assert parse_int_env("X", default=7) == 7

    def test_non_numeric_returns_default_with_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("X", "not-a-number")
        caplog.set_level(logging.WARNING, "exectunnel.observability.utils")
        assert parse_int_env("X", default=5) == 5
        assert any("Invalid integer" in r.message for r in caplog.records)

    def test_below_minimum_returns_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("X", "1")
        assert parse_int_env("X", default=10, min_value=5, max_value=100) == 10

    def test_above_maximum_returns_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("X", "999")
        assert parse_int_env("X", default=10, min_value=5, max_value=100) == 10

    def test_default_is_clamped_to_bounds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Caller passed default=0 which is below min_value=10 — the
        # helper must clamp the *default* itself before returning.
        monkeypatch.delenv("X", raising=False)
        assert parse_int_env("X", default=0, min_value=10) == 10
        # Same for upper bound.
        assert parse_int_env("X", default=999, max_value=100) == 100


# ── parse_float_env ───────────────────────────────────────────────────────────


class TestParseFloatEnv:
    def test_accepts_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("Y", "3.14")
        assert parse_float_env("Y", default=0.0) == pytest.approx(3.14)

    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("Y", raising=False)
        assert parse_float_env("Y", default=2.5) == 2.5

    def test_non_finite_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("Y", "inf")
        assert parse_float_env("Y", default=1.0) == 1.0

    def test_nan_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("Y", "nan")
        assert parse_float_env("Y", default=1.0) == 1.0

    def test_below_minimum_returns_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("Y", "0.1")
        assert parse_float_env("Y", default=2.0, min_value=1.0, max_value=10.0) == 2.0

    def test_above_maximum_returns_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("Y", "100.0")
        assert parse_float_env("Y", default=2.0, min_value=1.0, max_value=10.0) == 2.0

    def test_default_is_clamped_to_bounds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("Y", raising=False)
        assert parse_float_env("Y", default=0.0, min_value=5.0) == 5.0
        assert parse_float_env("Y", default=999.0, max_value=100.0) == 100.0
