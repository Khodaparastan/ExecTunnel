from __future__ import annotations

import contextvars
import logging
import secrets
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass

from .metrics import metrics_inc, metrics_observe

__all__ = [
    "current_parent_span_id",
    "current_span_id",
    "current_trace_id",
    "span",
    "aspan",
    "start_trace",
]

logger = logging.getLogger("exectunnel.tracing")

_trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "exectunnel_trace_id",
    default=None,
)
_span_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "exectunnel_span_id",
    default=None,
)
_parent_span_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "exectunnel_parent_span_id",
    default=None,
)


# ------------------------------------------------------------------
# Public accessors
# ------------------------------------------------------------------


def current_trace_id() -> str | None:
    return _trace_id_var.get()


def current_span_id() -> str | None:
    return _span_id_var.get()


def current_parent_span_id() -> str | None:
    return _parent_span_id_var.get()


# ------------------------------------------------------------------
# ID generators
# ------------------------------------------------------------------


def _new_trace_id() -> str:
    return secrets.token_hex(16)


def _new_span_id() -> str:
    return secrets.token_hex(8)


def _normalize_trace_id(trace_id: str | None) -> str:
    if trace_id is None:
        return _new_trace_id()
    if not isinstance(trace_id, str):
        raise TypeError("trace_id must be a string or None")
    normalized = trace_id.strip()
    if not normalized:
        raise ValueError("trace_id must not be empty")
    return normalized


def _ensure_trace_id() -> str:
    trace_id = current_trace_id()
    if trace_id is not None:
        return trace_id
    return start_trace()


# ------------------------------------------------------------------
# Safe observability helpers
# ------------------------------------------------------------------


def _safe_metrics_inc(metric: str, /, value: int = 1, **tags: object) -> None:
    try:
        metrics_inc(metric, value=value, **tags)
    except Exception:
        logger.debug(
            "Failed to emit tracing counter metric %r",
            metric,
            exc_info=True,
        )


def _safe_metrics_observe(metric: str, /, value: float, **tags: object) -> None:
    try:
        metrics_observe(metric, value=value, **tags)
    except Exception:
        logger.debug(
            "Failed to emit tracing histogram metric %r",
            metric,
            exc_info=True,
        )


# ------------------------------------------------------------------
# Trace lifecycle
# ------------------------------------------------------------------


def start_trace(trace_id: str | None = None) -> str:
    """Start a new trace in the current context."""
    trace = _normalize_trace_id(trace_id)
    _trace_id_var.set(trace)
    _span_id_var.set(None)
    _parent_span_id_var.set(None)
    _safe_metrics_inc("trace.started", provided=trace_id is not None)
    return trace


# ------------------------------------------------------------------
# Span helpers
# ------------------------------------------------------------------


@dataclass(slots=True)
class _SpanState:
    name: str
    span_id: str
    parent_span_id: str | None
    span_token: contextvars.Token[str | None]
    parent_token: contextvars.Token[str | None]
    started_at: float


def _validate_span_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("Span name must be a string")
    normalized = name.strip()
    if not normalized:
        raise ValueError("Span name must not be empty")
    return normalized


def _enter_span(name: str) -> _SpanState:
    span_name = _validate_span_name(name)
    _ensure_trace_id()

    parent_span_id = _span_id_var.get()
    span_id = _new_span_id()

    span_token = _span_id_var.set(span_id)
    parent_token = _parent_span_id_var.set(parent_span_id)

    state = _SpanState(
        name=span_name,
        span_id=span_id,
        parent_span_id=parent_span_id,
        span_token=span_token,
        parent_token=parent_token,
        started_at=time.perf_counter(),
    )

    _safe_metrics_inc(
        "trace.spans.started",
        name=span_name,
        parent=parent_span_id is not None,
    )
    return state


def _exit_span(
    state: _SpanState,
    *,
    ok: bool,
    tags: dict[str, object],
) -> None:
    duration = time.perf_counter() - state.started_at

    metric_tags = dict(tags)
    metric_tags["name"] = state.name
    metric_tags["ok"] = ok

    try:
        if ok:
            _safe_metrics_inc("trace.spans.ok", name=state.name)
        else:
            _safe_metrics_inc("trace.spans.error", name=state.name)

        _safe_metrics_observe(
            "trace.spans.duration_sec",
            duration,
            **metric_tags,
        )
    finally:
        _parent_span_id_var.reset(state.parent_token)
        _span_id_var.reset(state.span_token)


# ------------------------------------------------------------------
# Synchronous span
# ------------------------------------------------------------------


@contextmanager
def span(name: str, **tags: object) -> Iterator[str]:
    """Open a child span as a synchronous context manager."""
    state = _enter_span(name)
    ok = False
    try:
        yield state.span_id
        ok = True
    finally:
        _exit_span(state, ok=ok, tags=dict(tags))


# ------------------------------------------------------------------
# Asynchronous span
# ------------------------------------------------------------------


@asynccontextmanager
async def aspan(name: str, **tags: object) -> AsyncIterator[str]:
    """Open a child span as an asynchronous context manager."""
    state = _enter_span(name)
    ok = False
    try:
        yield state.span_id
        ok = True
    finally:
        _exit_span(state, ok=ok, tags=dict(tags))
