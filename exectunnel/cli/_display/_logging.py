"""Rich-aware logging configuration."""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from rich.console import Console
from rich.logging import RichHandler

from ._theme import THEME

__all__ = ["configure_logging", "get_stderr_console"]

LogLevel = Literal["debug", "info", "warning", "error"]
LogFormat = Literal["text", "json"]

# ---------------------------------------------------------------------------
# Shared console singleton
# ---------------------------------------------------------------------------

_stderr_console: Console = Console(stderr=True, theme=THEME, highlight=False)


def get_stderr_console() -> Console:
    """Return the shared stderr :class:`rich.console.Console` instance."""
    return _stderr_console


# ---------------------------------------------------------------------------
# Standard LogRecord attribute names — computed once at import time.
# ---------------------------------------------------------------------------

_STANDARD_LOG_RECORD_KEYS: Final[frozenset[str]] = frozenset(
    logging.makeLogRecord({}).__dict__.keys()
)

_RESERVED_JSON_KEYS: Final[frozenset[str]] = frozenset({
    "ts",
    "level",
    "logger",
    "msg",
    "exc",
    "stack",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_level(level: str) -> int:
    normalized = level.strip().lower()
    match normalized:
        case "debug":
            return logging.DEBUG
        case "info":
            return logging.INFO
        case "warning":
            return logging.WARNING
        case "error":
            return logging.ERROR
        case _:
            raise ValueError(
                f"Invalid log level {level!r}; expected one of: "
                "'debug', 'info', 'warning', 'error'"
            )


def _normalize_format(fmt: str) -> LogFormat:
    normalized = fmt.strip().lower()
    if normalized not in {"text", "json"}:
        raise ValueError(f"Invalid log format {fmt!r}; expected one of: 'text', 'json'")
    return normalized


def _record_timestamp(record: logging.LogRecord) -> str:
    return (
        datetime
        .fromtimestamp(record.created, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _replace_root_handlers(
    root: logging.Logger, new_handlers: list[logging.Handler]
) -> None:
    old_handlers = list(root.handlers)
    for handler in old_handlers:
        root.removeHandler(handler)

    for handler in new_handlers:
        root.addHandler(handler)

    for handler in old_handlers:
        with contextlib.suppress(Exception):
            handler.close()


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """Emit one compact JSON object per log record (NDJSON)."""

    def format(self, record: logging.LogRecord) -> str:
        document: dict[str, object] = {
            "ts": _record_timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        if record.exc_info:
            document["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            document["stack"] = record.stack_info

        document.update({
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_LOG_RECORD_KEYS
            and key not in _RESERVED_JSON_KEYS
            and not key.startswith("_")
        })

        return json.dumps(
            document,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def configure_logging(
    level: LogLevel,
    fmt: LogFormat,
    log_file: Path | None = None,
) -> None:
    """Configure the root logger with Rich or JSON output.

    Existing root handlers are removed and closed, so repeated calls are safe.
    If *log_file* is provided, logs are additionally written there as NDJSON.
    """
    numeric_level = _normalize_level(level)
    normalized_format = _normalize_format(fmt)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    new_handlers: list[logging.Handler] = []

    if normalized_format == "json":
        stderr_handler: logging.Handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(numeric_level)
        stderr_handler.setFormatter(_JsonFormatter())
    else:
        stderr_handler = RichHandler(
            console=_stderr_console,
            show_time=True,
            show_path=False,
            rich_tracebacks=True,
            tracebacks_show_locals=False,
            markup=False,
        )
        stderr_handler.setLevel(numeric_level)

    new_handlers.append(stderr_handler)

    if log_file is not None:
        normalized_log_file = log_file.expanduser().resolve(strict=False)
        normalized_log_file.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(normalized_log_file, encoding="utf-8")
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(_JsonFormatter())
        new_handlers.append(file_handler)

    _replace_root_handlers(root, new_handlers)
