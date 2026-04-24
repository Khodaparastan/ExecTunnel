"""Shared log-level normalisation and validation for CLI commands."""

from __future__ import annotations

from typing import Final, Literal

__all__ = ["VALID_LOG_LEVELS", "normalize_log_level"]

VALID_LOG_LEVELS: Final[frozenset[str]] = frozenset({
    "debug",
    "info",
    "warning",
    "error",
})


def normalize_log_level(value: str) -> str:
    """Lowercase and normalise *value*; maps ``'warn'`` → ``'warning'``."""
    level = value.lower().strip()
    if level == "warn":
        level = "warning"
    return level
