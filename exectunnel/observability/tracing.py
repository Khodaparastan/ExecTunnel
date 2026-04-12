"""Lightweight context-variable-based distributed tracing for exectunnel.

Each asyncio Task inherits its own ``contextvars`` snapshot, so concurrent
connections never share trace/span IDs.  Both synchronous and asynchronous
span context managers are provided.
"""

from __future__ import annotations

import contextvars
import secrets
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

from .metrics import metrics_inc, metrics_observe

__all__ = [
    "current_parent_span_id",
    "current_span_id",
    "current_trace_id",
    "span",
    "aspan",
    "start_trace",
]

_trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "exectunnel_trace_id", default=None,
)
_span_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "exectunnel_span_id", default=None,
)
_parent_span_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "exectunnel_parent_span_id", default=None,
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


# ------------------------------------------------------------------
# Trace lifecycle
# ------------------------------------------------------------------

def start_trace(trace_id: str | None = None) -> str:
    """Start a new trace in the current context.

    Sets a fresh *trace_id* and clears span / parent-span in the current
    ``contextvars`` context.  Returns the new trace_id.
    """
    trace = trace_id or _new_trace_id()
    _trace_id_var.set(trace)
    _span_id_var.set(None)
    _parent_span_id_var.set(None)
    return trace


# ------------------------------------------------------------------
# Span helpers (shared logic)
# ------------------------------------------------------------------

def _enter_span(name: str) -> tuple[str, str | None, contextvars.Token[str | None], contextvars.Token[str | None], float]:
    """Push a new span onto the context-var stack and return bookkeeping state."""
    parent_span = _span_id_var.get()
    span_id = _new_span_id()
    span_token = _span_id_var.set(span_id)
    parent_token = _parent_span_id_var.set(parent_span)
    start = time.perf_counter()
    metrics_inc("trace.spans.started", name=name, parent=parent_span is not None)
    return span_id, parent_span, span_token, parent_token, start


def _exit_span(
    name: str,
    *,
    ok: bool,
    span_token: contextvars.Token[str | None],
    parent_token: contextvars.Token[str | None],
    start: float,
    tags: dict[str, object],
) -> None:
    """Record span metrics and restore the context-var stack."""
    if ok:
        metrics_inc("trace.spans.ok", name=name)
    else:
        metrics_inc("trace.spans.error", name=name)
    duration = time.perf_counter() - start
    metrics_observe("trace.spans.duration_sec", duration, name=name, **tags)
    _parent_span_id_var.reset(parent_token)
    _span_id_var.reset(span_token)


# ------------------------------------------------------------------
# Synchronous span
# ------------------------------------------------------------------

@contextmanager
def span(name: str, **tags: object) -> Iterator[str]:
    """Open a child span (synchronous context manager)."""
    span_id, _parent, span_tok, parent_tok, start = _enter_span(name)
    ok = False
    try:
        yield span_id
        ok = True
    finally:
        _exit_span(
            name, ok=ok, span_token=span_tok, parent_token=parent_tok,
            start=start, tags=tags,
        )


# ------------------------------------------------------------------
# Asynchronous span
# ------------------------------------------------------------------

@asynccontextmanager
async def aspan(name: str, **tags: object) -> AsyncIterator[str]:
    """Open a child span (asynchronous context manager)."""
    span_id, _parent, span_tok, parent_tok, start = _enter_span(name)
    ok = False
    try:
        yield span_id
        ok = True
    finally:
        _exit_span(
            name, ok=ok, span_token=span_tok, parent_token=parent_tok,
            start=start, tags=tags,
        )
