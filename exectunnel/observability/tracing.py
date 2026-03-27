from __future__ import annotations

import contextvars
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager

from exectunnel.observability.metrics import metrics_inc, metrics_observe

_trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "exectunnel_trace_id", default=None
)
_span_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "exectunnel_span_id", default=None
)


def current_trace_id() -> str | None:
    return _trace_id_var.get()


def current_span_id() -> str | None:
    return _span_id_var.get()


def _new_trace_id() -> str:
    return secrets.token_hex(16)


def _new_span_id() -> str:
    return secrets.token_hex(8)


@contextmanager
def start_trace(trace_id: str | None = None) -> Iterator[str]:
    trace = trace_id or _new_trace_id()
    trace_token = _trace_id_var.set(trace)
    span_token = _span_id_var.set(None)
    try:
        yield trace
    finally:
        _span_id_var.reset(span_token)
        _trace_id_var.reset(trace_token)


@contextmanager
def span(name: str, **tags: object) -> Iterator[str]:
    parent_span = _span_id_var.get()
    span_id = _new_span_id()
    token = _span_id_var.set(span_id)
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
        _span_id_var.reset(token)
