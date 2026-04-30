from __future__ import annotations

import json
import logging
import os
import sys
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from .tracing import current_parent_span_id, current_span_id, current_trace_id

__all__ = [
    "LevelName",
    "LogEntry",
    "LogRingBuffer",
    "attach_rich_logging",
    "configure_logging",
    "install_ring_buffer",
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
# Log ring buffer
# ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LogEntry:
    ts: str
    level: str
    logger: str
    message: str


class LogRingBuffer(logging.Handler):
    """A handler that stores the last *maxlen* rendered log entries."""

    def __init__(self, maxlen: int = 200, level: int = logging.DEBUG) -> None:
        if maxlen <= 0:
            raise ValueError("maxlen must be > 0")
        super().__init__(level)
        self._entries: deque[LogEntry] = deque(maxlen=maxlen)

    @property
    def maxlen(self) -> int:
        maxlen = self._entries.maxlen
        if maxlen is None:
            raise RuntimeError("Ring buffer maxlen unexpectedly unset")
        return maxlen

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                formatter = logging.Formatter()
                exc_text = formatter.formatException(record.exc_info).replace(
                    "\n", r"\n"
                )
                message = f"{message} | {exc_text}"

            ts = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S")
            short_name = (
                record.name.removeprefix("exectunnel.")
                if record.name.startswith("exectunnel.")
                else record.name
            )
            entry = LogEntry(
                ts=ts,
                level=record.levelname,
                logger=short_name,
                message=message,
            )

            self.acquire()
            try:
                self._entries.append(entry)
            finally:
                self.release()
        except Exception:
            self.handleError(record)

    def entries(self) -> list[LogEntry]:
        self.acquire()
        try:
            return list(self._entries)
        finally:
            self.release()

    def clear(self) -> None:
        self.acquire()
        try:
            self._entries.clear()
        finally:
            self.release()


_RING_BUFFER_ATTR = "_exectunnel_ring_buffer"


def install_ring_buffer(
    maxlen: int = 200,
    level: int = logging.DEBUG,
) -> LogRingBuffer:
    """Attach a ring buffer to the ``exectunnel`` logger.

    Reuses an existing compatible buffer. If configuration differs, the old
    buffer is replaced with a new one.
    """
    pkg_logger = logging.getLogger("exectunnel")

    for handler in list(pkg_logger.handlers):
        if not getattr(handler, _RING_BUFFER_ATTR, False):
            continue

        if not isinstance(handler, LogRingBuffer):
            raise TypeError(f"Expected LogRingBuffer, got {type(handler).__name__}")

        if handler.maxlen == maxlen and handler.level == level:
            return handler

        pkg_logger.removeHandler(handler)
        break

    buf = LogRingBuffer(maxlen=maxlen, level=level)
    setattr(buf, _RING_BUFFER_ATTR, True)
    pkg_logger.addHandler(buf)
    return buf


# ------------------------------------------------------------------
# Trace context filter
# ------------------------------------------------------------------


def _builtin_record_attrs() -> frozenset[str]:
    record = logging.makeLogRecord({})
    attrs = set(record.__dict__)
    attrs.update({"message", "asctime", "trace_id", "span_id", "parent_span_id"})
    return frozenset(attrs)


_LOG_RECORD_BUILTIN_ATTRS = _builtin_record_attrs()


class _TraceContextFilter(logging.Filter):
    """Inject trace/span IDs from context vars into every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = current_trace_id() or "-"  # type: ignore[attr-defined]
        record.span_id = current_span_id() or "-"  # type: ignore[attr-defined]
        record.parent_span_id = current_parent_span_id() or "-"  # type: ignore[attr-defined]
        return True


# ------------------------------------------------------------------
# Formatters
# ------------------------------------------------------------------


def _ts_from_record(record: logging.LogRecord, fmt: str) -> str:
    return datetime.fromtimestamp(record.created, tz=UTC).strftime(fmt)


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _LOG_RECORD_BUILTIN_ATTRS and not key.startswith("_")
    }


def _one_line_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", r"\n")


def _display_logger_name(name: str) -> str:
    if name.startswith("exectunnel."):
        return name.removeprefix("exectunnel.")
    return name


def _format_console_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if not text:
        return '""'
    if any(ch.isspace() for ch in text) or any(ch in text for ch in '[]"='):
        return json.dumps(text, ensure_ascii=False)
    return text


class _JsonLogFormatter(logging.Formatter):
    """Emit each log record as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": _ts_from_record(record, "%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", "-"),
            "span_id": getattr(record, "span_id", "-"),
            "parent_span_id": getattr(record, "parent_span_id", "-"),
        }

        payload.update(_extra_fields(record))

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = record.stack_info

        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )


class _ConsoleFormatter(logging.Formatter):
    """Human-friendly single-line formatter with optional ANSI color."""

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
        ts = _ts_from_record(record, "%H:%M:%S")
        level_text = f"{record.levelname:<7}"
        logger_name = _display_logger_name(record.name)
        message = record.getMessage()

        if record.exc_info:
            exc_text = _one_line_text(self.formatException(record.exc_info))
            message = f"{message} | {exc_text}"
        elif record.stack_info:
            stack_text = _one_line_text(record.stack_info)
            message = f"{message} | stack={stack_text}"

        level_text, message = self._colorize(record.levelno, level_text, message)

        extras = _extra_fields(record)
        context_parts: list[str] = []

        if extras:
            context_parts.append(
                " ".join(
                    f"{key}={_format_console_value(value)}"
                    for key, value in sorted(extras.items())
                )
            )

        if record.levelno <= logging.DEBUG:
            context_parts.append(f"trace={getattr(record, 'trace_id', '-')}")
            context_parts.append(f"span={getattr(record, 'span_id', '-')}")
            context_parts.append(f"parent={getattr(record, 'parent_span_id', '-')}")

        context = f" [{' '.join(context_parts)}]" if context_parts else ""
        return f"{ts} {level_text} {logger_name}: {message}{context}"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


_HANDLER_ATTR = "_exectunnel_handler"
_RICH_HANDLER_ATTR = "_exectunnel_rich_handler"


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False

    mode = os.getenv("EXECTUNNEL_LOG_COLOR", "auto").strip().lower()
    if mode in {"0", "false", "no", "off", "never"}:
        return False
    if mode in {"1", "true", "yes", "on", "always"}:
        return True

    return sys.stderr.isatty()


def _normalize_level(level: str) -> int:
    return _LEVELS.get(level.strip().lower(), logging.INFO)


def _add_observability_context(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    event_dict.setdefault("trace_id", current_trace_id() or "-")
    event_dict.setdefault("span_id", current_span_id() or "-")
    event_dict.setdefault("parent_span_id", current_parent_span_id() or "-")
    return event_dict


def _configure_structlog(
    *,
    handler: logging.Handler,
    log_format: str,
    enable_color: bool,
) -> bool:
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
        )
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


def _remove_exectunnel_handlers(pkg_logger: logging.Logger) -> None:
    for handler in list(pkg_logger.handlers):
        if getattr(handler, _HANDLER_ATTR, False) or getattr(
            handler,
            _RICH_HANDLER_ATTR,
            False,
        ):
            pkg_logger.removeHandler(handler)


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def attach_rich_logging(
    console: object,
    level: LevelName = "info",
) -> None:
    """Replace exectunnel stream/Rich handlers with a Rich-aware handler."""
    from rich.logging import RichHandler

    numeric = _normalize_level(level)
    pkg_logger = logging.getLogger("exectunnel")

    _remove_exectunnel_handlers(pkg_logger)

    rich_handler = RichHandler(
        level=numeric,
        console=console,  # type: ignore[arg-type]
        show_time=True,
        show_path=False,
        rich_tracebacks=True,
        markup=False,
    )
    rich_handler.addFilter(_TraceContextFilter())
    rich_handler.setFormatter(logging.Formatter("%(message)s"))
    setattr(rich_handler, _RICH_HANDLER_ATTR, True)

    pkg_logger.addHandler(rich_handler)
    pkg_logger.setLevel(numeric)
    pkg_logger.propagate = False


def configure_logging(level: LevelName = "info") -> None:
    """Bootstrap the ``exectunnel`` logger hierarchy."""
    numeric = _normalize_level(level)

    raw_log_format = os.getenv("EXECTUNNEL_LOG_FORMAT", "console").strip().lower()
    if raw_log_format == "text":
        raw_log_format = "console"
    raw_log_engine = os.getenv("EXECTUNNEL_LOG_ENGINE", "stdlib").strip().lower()

    log_format = raw_log_format if raw_log_format in {"console", "json"} else "console"
    log_engine = (
        raw_log_engine if raw_log_engine in {"stdlib", "structlog"} else "stdlib"
    )

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
    _remove_exectunnel_handlers(pkg_logger)
    pkg_logger.addHandler(handler)
    pkg_logger.propagate = False

    if raw_log_format != log_format:
        pkg_logger.warning(
            "Invalid EXECTUNNEL_LOG_FORMAT=%r; using %r",
            raw_log_format,
            log_format,
        )

    if raw_log_engine != log_engine:
        pkg_logger.warning(
            "Invalid EXECTUNNEL_LOG_ENGINE=%r; using %r",
            raw_log_engine,
            log_engine,
        )

    if raw_log_engine == "structlog" and not structlog_active:
        pkg_logger.warning(
            "EXECTUNNEL_LOG_ENGINE=structlog requested, but structlog is not "
            "installed; falling back to stdlib logging",
        )
