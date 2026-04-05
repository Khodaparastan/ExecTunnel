from __future__ import annotations

import os


def parse_bool_env(name: str, default: bool = False) -> bool:
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
    val = os.environ.get(name, "").strip()
    if not val:
        return default
    try:
        result = int(val)
    except ValueError:
        return default
    if min_value is not None and result < min_value:
        return default
    if max_value is not None and result > max_value:
        return default
    return result


def parse_float_env(
    name: str,
    default: float = 0.0,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    val = os.environ.get(name, "").strip()
    if not val:
        return default
    try:
        result = float(val)
    except ValueError:
        return default
    if min_value is not None and result < min_value:
        return default
    if max_value is not None and result > max_value:
        return default
    return result


__all__ = ["parse_bool_env", "parse_float_env", "parse_int_env"]
