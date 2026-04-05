from __future__ import annotations

import contextvars
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager

from .metrics import metrics_inc, metrics_observe

_trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "exectunnel_trace_id", default=None
)
_span_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "exectunnel_span_id", default=None
)
_parent_span_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "exectunnel_parent_span_id", default=None
)


def current_trace_id() -> str | None:
    return _trace_id_var.get()


def current_span_id() -> str | None:
    return _span_id_var.get()


def current_parent_span_id() -> str | None:
    return _parent_span_id_var.get()


def _new_trace_id() -> str:
    return secrets.token_hex(16)


def _new_span_id() -> str:
    return secrets.token_hex(8)


def start_trace(trace_id: str | None = None) -> str:
    """Start a new trace in the current context (fire-and-forget).

    Sets a fresh trace_id and clears span/parent-span in the current
    ``contextvars`` context.  Each asyncio Task has its own copy of the
    context, so concurrent connections never share IDs.

    Returns the new trace_id so callers can log or propagate it.
    """
    trace = trace_id or _new_trace_id()
    _trace_id_var.set(trace)
    _span_id_var.set(None)
    _parent_span_id_var.set(None)
    return trace


@contextmanager
def span(name: str, **tags: object) -> Iterator[str]:
    """Open a child span, propagating the current span as parent."""
    parent_span = _span_id_var.get()
    span_id = _new_span_id()
    span_token = _span_id_var.set(span_id)
    parent_token = _parent_span_id_var.set(parent_span)
    start = time.perf_counter()
    metrics_inc("trace.spans.started", name=name, parent=bool(parent_span))
    try:
        yield span_id
        metrics_inc("trace.spans.ok", name=name)
    except Exception:
        metrics_inc("trace.spans.error", name=name)
        raise
    finally:
        duration = time.perf_counter() - start
        metrics_observe("trace.spans.duration_sec", duration, name=name, **tags)
        _parent_span_id_var.reset(parent_token)
        _span_id_var.reset(span_token)
