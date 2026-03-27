from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any, Literal, cast

from exectunnel.observability.tracing import current_span_id, current_trace_id

_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

LevelName = Literal["debug", "info", "warning", "error"]

try:
    from colorama import Fore, Style
    from colorama import init as colorama_init
except Exception:  # pragma: no cover - optional dependency
    Fore = None  # type: ignore[assignment]
    Style = None  # type: ignore[assignment]
    colorama_init = None  # type: ignore[assignment]
try:
    import structlog
except Exception:  # pragma: no cover - optional dependency
    structlog = None  # type: ignore[assignment]


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = current_trace_id() or "-"
        record.span_id = current_span_id() or "-"
        return True


# Fields that are standard LogRecord attributes — never re-emitted as extras.
_LOG_RECORD_BUILTIN_ATTRS: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    | {
        "message",
        "asctime",
        "trace_id",
        "span_id",
        "taskName",
    }
)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", "-"),
            "span_id": getattr(record, "span_id", "-"),
        }
        # Emit any structured extra= fields passed by the caller.
        for key, val in record.__dict__.items():
            if key not in _LOG_RECORD_BUILTIN_ATTRS:
                payload[key] = val
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True)


class _ConsoleFormatter(logging.Formatter):
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
        ts = datetime.now().strftime("%H:%M:%S")
        level = f"{record.levelname:<7}"
        message = record.getMessage()
        level, message = self._colorize(record.levelno, level, message)
        # Collect structured extra= fields for richer debug output.
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _LOG_RECORD_BUILTIN_ATTRS
        }
        if record.levelno <= logging.DEBUG:
            trace = getattr(record, "trace_id", "-")
            span = getattr(record, "span_id", "-")
            suffix = f" [trace={trace} span={span}]"
            if extras:
                kv = " ".join(f"{k}={v}" for k, v in extras.items())
                suffix = f" [{kv} trace={trace} span={span}]"
            return f"{ts} {level} {record.name}: {message}{suffix}"
        if extras:
            kv = " ".join(f"{k}={v}" for k, v in extras.items())
            return f"{ts} {level} {message} [{kv}]"
        return f"{ts} {level} {message}"


def _color_enabled() -> bool:
    mode = os.getenv("EXECTUNNEL_LOG_COLOR", "auto").strip().lower()
    if mode in {"0", "false", "no", "off", "never"}:
        return False
    if mode in {"1", "true", "yes", "on", "always"}:
        return True
    return sys.stderr.isatty()


def _add_observability_context(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
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
    if structlog is None:
        return False

    pre_chain = [
        cast(Any, structlog).stdlib.add_log_level,
        cast(Any, structlog).stdlib.add_logger_name,
        _add_observability_context,
    ]
    if log_format == "json":
        renderer = cast(Any, structlog).processors.JSONRenderer(serializer=json.dumps)
    else:
        renderer = cast(Any, structlog).dev.ConsoleRenderer(colors=enable_color)

    handler.setFormatter(
        cast(Any, structlog).stdlib.ProcessorFormatter(
            processor=renderer,
            foreign_pre_chain=pre_chain,
        )
    )

    cast(Any, structlog).configure(
        processors=[
            cast(Any, structlog).contextvars.merge_contextvars,
            cast(Any, structlog).stdlib.add_log_level,
            cast(Any, structlog).stdlib.add_logger_name,
            _add_observability_context,
            cast(Any, structlog).stdlib.PositionalArgumentsFormatter(),
            cast(Any, structlog).processors.StackInfoRenderer(),
            cast(Any, structlog).processors.format_exc_info,
            cast(Any, structlog).stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=cast(Any, structlog).stdlib.LoggerFactory(),
        wrapper_class=cast(Any, structlog).stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    return True


def configure_logging(level: LevelName = "info") -> None:
    numeric = _LEVELS.get(level.lower(), logging.INFO)
    log_format = os.getenv("EXECTUNNEL_LOG_FORMAT", "console").strip().lower()
    log_engine = os.getenv("EXECTUNNEL_LOG_ENGINE", "stdlib").strip().lower()
    enable_color = _color_enabled()
    if colorama_init is not None:
        colorama_init()

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(numeric)
    handler.addFilter(_ContextFilter())
    structlog_enabled = log_engine == "structlog" and _configure_structlog(
        handler=handler,
        log_format=log_format,
        enable_color=enable_color,
    )
    if not structlog_enabled and log_format == "json":
        handler.setFormatter(_JsonFormatter())
    elif not structlog_enabled:
        handler.setFormatter(_ConsoleFormatter(enable_color=enable_color))
    handler._exectunnel_handler = True  # type: ignore[attr-defined]

    pkg_logger = logging.getLogger("exectunnel")
    pkg_logger.setLevel(numeric)
    for existing in list(pkg_logger.handlers):
        if getattr(existing, "_exectunnel_handler", False):
            pkg_logger.removeHandler(existing)
    pkg_logger.addHandler(handler)
    pkg_logger.propagate = False
    if log_engine == "structlog" and not structlog_enabled:
        pkg_logger.warning(
            "EXECTUNNEL_LOG_ENGINE=structlog requested, but structlog is not installed; "
            "falling back to stdlib logging"
        )
