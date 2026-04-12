"""Configurable logging subsystem for exectunnel.

Supports three modes selected via environment variables:

* ``EXECTUNNEL_LOG_FORMAT=console`` (default) — human-friendly output.
* ``EXECTUNNEL_LOG_FORMAT=json`` — machine-parseable JSON lines.
* ``EXECTUNNEL_LOG_ENGINE=structlog`` — delegates to *structlog* if installed.

Trace and span IDs are automatically injected via :mod:`.tracing` context
variables.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any, Literal

from .tracing import current_span_id, current_trace_id

__all__ = [
    "LevelName",
    "configure_logging",
]

_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

LevelName = Literal["debug", "info", "warning", "error"]

# ------------------------------------------------------------------
# Optional dependency imports
# ------------------------------------------------------------------

try:
    from colorama import Fore, Style  # type: ignore[import-untyped]
    from colorama import init as colorama_init  # type: ignore[import-untyped]
except ImportError:
    Fore = None  # type: ignore[assignment]
    Style = None  # type: ignore[assignment]
    colorama_init = None  # type: ignore[assignment]

try:
    import structlog as _structlog  # type: ignore[import-untyped]
except ImportError:
    _structlog = None  # type: ignore[assignment]


# ------------------------------------------------------------------
# Trace context filter
# ------------------------------------------------------------------

# Standard LogRecord attribute names — never re-emitted as caller extras.
_LOG_RECORD_BUILTIN_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    | {"message", "asctime", "trace_id", "span_id", "taskName"}
)


class _TraceContextFilter(logging.Filter):
    """Inject trace/span IDs from context-vars into every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = current_trace_id() or "-"  # type: ignore[attr-defined]
        record.span_id = current_span_id() or "-"  # type: ignore[attr-defined]
        return True


# ------------------------------------------------------------------
# Formatters
# ------------------------------------------------------------------

class _JsonLogFormatter(logging.Formatter):
    """Emit each log record as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", "-"),
            "span_id": getattr(record, "span_id", "-"),
        }
        for key, val in record.__dict__.items():
            if key not in _LOG_RECORD_BUILTIN_ATTRS:
                payload[key] = val
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


class _ConsoleFormatter(logging.Formatter):
    """Human-friendly single-line formatter with optional ANSI colour."""

    def __init__(self, *, enable_color: bool) -> None:
        super().__init__()
        self._enable_color = enable_color

    def _colorize(self, level: int, level_text: str, message: str) -> tuple[str, str]:
        if not self._enable_color or Fore is None or Style is None:
            return level_text, message
        if level >= logging.ERROR:
            color = Fore.RED
        elif level >= logging.WARNING:
            color = Fore.YELLOW
        elif level >= logging.INFO:
            color = Fore.GREEN
        else:
            color = Fore.CYAN
        return (
            f"{color}{level_text}{Style.RESET_ALL}",
            f"{color}{message}{Style.RESET_ALL}",
        )

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(UTC).strftime("%H:%M:%S")
        level = f"{record.levelname:<7}"
        message = record.getMessage()
        level, message = self._colorize(record.levelno, level, message)

        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _LOG_RECORD_BUILTIN_ATTRS
        }

        if record.levelno <= logging.DEBUG:
            trace = getattr(record, "trace_id", "-")
            span_ = getattr(record, "span_id", "-")
            if extras:
                kv = " ".join(f"{k}={v}" for k, v in extras.items())
                return f"{ts} {level} {record.name}: {message} [{kv} trace={trace} span={span_}]"
            return f"{ts} {level} {record.name}: {message} [trace={trace} span={span_}]"

        if extras:
            kv = " ".join(f"{k}={v}" for k, v in extras.items())
            return f"{ts} {level} {message} [{kv}]"
        return f"{ts} {level} {message}"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _color_enabled() -> bool:
    mode = os.getenv("EXECTUNNEL_LOG_COLOR", "auto").strip().lower()
    if mode in {"0", "false", "no", "off", "never"}:
        return False
    if mode in {"1", "true", "yes", "on", "always"}:
        return True
    return sys.stderr.isatty()


def _add_observability_context(
    _logger: Any, _method_name: str, event_dict: dict[str, Any],
) -> dict[str, Any]:
    event_dict.setdefault("trace_id", current_trace_id() or "-")
    event_dict.setdefault("span_id", current_span_id() or "-")
    return event_dict


def _configure_structlog(
    *,
    handler: logging.Handler,
    log_format: str,
    enable_color: bool,
) -> bool:
    """Set up *structlog* as the logging backend.  Returns ``True`` on success."""
    if _structlog is None:
        return False

    pre_chain = [
        _structlog.stdlib.add_log_level,
        _structlog.stdlib.add_logger_name,
        _add_observability_context,
    ]
    if log_format == "json":
        renderer = _structlog.processors.JSONRenderer()
    else:
        renderer = _structlog.dev.ConsoleRenderer(colors=enable_color)

    handler.setFormatter(
        _structlog.stdlib.ProcessorFormatter(
            processor=renderer,
            foreign_pre_chain=pre_chain,
        ),
    )

    _structlog.configure(
        processors=[
            _structlog.contextvars.merge_contextvars,
            _structlog.stdlib.add_log_level,
            _structlog.stdlib.add_logger_name,
            _add_observability_context,
            _structlog.stdlib.PositionalArgumentsFormatter(),
            _structlog.processors.StackInfoRenderer(),
            _structlog.processors.format_exc_info,
            _structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=_structlog.stdlib.LoggerFactory(),
        wrapper_class=_structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    return True


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

_HANDLER_ATTR = "_exectunnel_handler"


def configure_logging(level: LevelName = "info") -> None:
    """Bootstrap the ``exectunnel`` logger hierarchy.

    Safe to call multiple times — previous exectunnel handlers are replaced.
    """
    numeric = _LEVELS.get(level.lower(), logging.INFO)
    log_format = os.getenv("EXECTUNNEL_LOG_FORMAT", "console").strip().lower()
    log_engine = os.getenv("EXECTUNNEL_LOG_ENGINE", "stdlib").strip().lower()
    enable_color = _color_enabled()

    if colorama_init is not None:
        colorama_init()

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(numeric)
    handler.addFilter(_TraceContextFilter())

    structlog_active = log_engine == "structlog" and _configure_structlog(
        handler=handler,
        log_format=log_format,
        enable_color=enable_color,
    )

    if not structlog_active:
        if log_format == "json":
            handler.setFormatter(_JsonLogFormatter())
        else:
            handler.setFormatter(_ConsoleFormatter(enable_color=enable_color))

    setattr(handler, _HANDLER_ATTR, True)

    pkg_logger = logging.getLogger("exectunnel")
    pkg_logger.setLevel(numeric)
    for existing in list(pkg_logger.handlers):
        if getattr(existing, _HANDLER_ATTR, False):
            pkg_logger.removeHandler(existing)
    pkg_logger.addHandler(handler)
    pkg_logger.propagate = False

    if log_engine == "structlog" and not structlog_active:
        pkg_logger.warning(
            "EXECTUNNEL_LOG_ENGINE=structlog requested, but structlog is not "
            "installed; falling back to stdlib logging",
        )
