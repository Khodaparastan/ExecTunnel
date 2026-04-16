"""Environment-variable parsing helpers for exectunnel observability.

Delegates to shared CLI env parsers so semantics stay identical across
the codebase.
"""

from __future__ import annotations

from exectunnel.cli.utils import (
    parse_bool_env as _parse_bool_env,
)
from exectunnel.cli.utils import (
    parse_float_env as _parse_float_env,
)
from exectunnel.cli.utils import (
    parse_int_env as _parse_int_env,
)

__all__ = ["parse_bool_env", "parse_float_env", "parse_int_env"]


def parse_bool_env(name: str, default: bool = False) -> bool:
    """Read an env var as a boolean flag."""
    return _parse_bool_env(name, default)


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
    return _parse_int_env(
        name,
        _clamp_int(default, min_value, max_value),
        min_value=min_value,
        max_value=max_value,
    )


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
    return _parse_float_env(
        name,
        _clamp_float(default, min_value, max_value),
        min_value=min_value,
        max_value=max_value,
    )


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
