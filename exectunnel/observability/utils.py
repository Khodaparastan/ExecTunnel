"""Environment-variable parsing helpers for exectunnel observability.

Delegates to shared CLI env parsers so semantics stay identical across
the codebase.
"""

from __future__ import annotations

import logging
import math
import os

logger = logging.getLogger(__name__)


def parse_bool_env(name: str, default: bool = False) -> bool:
    """Read an env var as a boolean flag."""
    return _parse_bool_env(name, default)


def _parse_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    token = value.strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off", ""}:
        return False
    logger.warning("Invalid boolean for %s=%r, using default %s", name, value, default)
    return default


def _parse_float_env(
    name: str,
    default: float,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        logger.warning(
            "Invalid value for %s=%r, using default %.1f", name, value, default
        )
        return default
    if not math.isfinite(parsed):
        logger.warning(
            "Invalid non-finite value for %s=%r, using default %.1f",
            name,
            value,
            default,
        )
        return default
    if min_value is not None and parsed < min_value:
        logger.warning(
            "Value for %s=%.3f below minimum %.3f, using default %.1f",
            name,
            parsed,
            min_value,
            default,
        )
        return default
    if max_value is not None and parsed > max_value:
        logger.warning(
            "Value for %s=%.3f above maximum %.3f, using default %.1f",
            name,
            parsed,
            max_value,
            default,
        )
        return default
    return parsed


def _parse_int_env(
    name: str,
    default: int,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        logger.warning(
            "Invalid integer for %s=%r, using default %d", name, value, default
        )
        return default
    if min_value is not None and parsed < min_value:
        logger.warning(
            "Value for %s=%d below minimum %d, using default %d",
            name,
            parsed,
            min_value,
            default,
        )
        return default
    if max_value is not None and parsed > max_value:
        logger.warning(
            "Value for %s=%d above maximum %d, using default %d",
            name,
            parsed,
            max_value,
            default,
        )
        return default
    return parsed


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
    if not math.isfinite(value):
        return lo if lo is not None else 0.0
    if lo is not None and value < lo:
        return lo
    if hi is not None and value > hi:
        return hi
    return value
