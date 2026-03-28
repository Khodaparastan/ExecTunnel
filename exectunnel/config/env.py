"""Environment variable parsing helpers."""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("exectunnel")


def parse_bool_env(name: str, default: bool = False) -> bool:
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


def parse_float_env(
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


def parse_int_env(
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
