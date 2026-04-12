"""Environment-variable parsing helpers for exectunnel observability."""

from __future__ import annotations

import os

__all__ = ["parse_bool_env", "parse_float_env", "parse_int_env"]


def parse_bool_env(name: str, default: bool = False) -> bool:
    """Read an env var as a boolean flag."""
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def parse_int_env(
    name: str,
    default: int = 0,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    """Read an env var as an integer, clamped to *[min_value, max_value]*.

    Returns *default* when the variable is unset, empty, non-numeric, or
    outside the specified bounds.  The *default* itself is also validated
    against the bounds — callers cannot accidentally return an out-of-range
    fallback.
    """
    val = os.environ.get(name, "").strip()
    if not val:
        return _clamp_int(default, min_value, max_value)
    try:
        result = int(val)
    except ValueError:
        return _clamp_int(default, min_value, max_value)
    if min_value is not None and result < min_value:
        return _clamp_int(default, min_value, max_value)
    if max_value is not None and result > max_value:
        return _clamp_int(default, min_value, max_value)
    return result


def parse_float_env(
    name: str,
    default: float = 0.0,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    """Read an env var as a float, clamped to *[min_value, max_value]*.

    Same semantics as :func:`parse_int_env` but for floating-point values.
    """
    val = os.environ.get(name, "").strip()
    if not val:
        return _clamp_float(default, min_value, max_value)
    try:
        result = float(val)
    except ValueError:
        return _clamp_float(default, min_value, max_value)
    if min_value is not None and result < min_value:
        return _clamp_float(default, min_value, max_value)
    if max_value is not None and result > max_value:
        return _clamp_float(default, min_value, max_value)
    return result

# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _clamp_int(value: int, lo: int | None, hi: int | None) -> int:
    if lo is not None and value < lo:
        return lo
    if hi is not None and value > hi:
        return hi
    return value


def _clamp_float(value: float, lo: float | None, hi: float | None) -> float:
    if lo is not None and value < lo:
        return lo
    if hi is not None and value > hi:
        return hi
    return value
