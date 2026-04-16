"""In-process metrics registry for exectunnel observability.

Provides thread-safe counters, histograms, and gauges with a tag system.
All public helpers operate on the module-level ``METRICS`` singleton.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import NamedTuple

__all__ = [
    "METRICS",
    "MetricsRegistry",
    "metrics_gauge_dec",
    "metrics_gauge_inc",
    "metrics_gauge_set",
    "metrics_inc",
    "metrics_observe",
    "metrics_reset",
    "metrics_snapshot",
    "register_metric_listener",
    "unregister_metric_listener",
    "unregister_all_listeners",
]


def _normalize_tags(tags: dict[str, object] | None) -> tuple[tuple[str, str], ...]:
    """Sort and stringify tag values.

    Booleans are lowercased (``"true"``/``"false"``) for consistency
    across callers that may pass Python ``True`` vs the string ``"true"``.
    """
    if not tags:
        return ()
    return tuple(sorted((k, _tag_str(v)) for k, v in tags.items()))


def _tag_str(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _render_metric_key(name: str, tags: tuple[tuple[str, str], ...]) -> str:
    if not tags:
        return name
    joined = ",".join(f"{k}={v}" for k, v in tags)
    return f"{name}{{{joined}}}"


class HistogramSnapshot(NamedTuple):
    """Immutable copy of a histogram's state at a point in time."""

    count: int
    total: float
    min: float | None
    max: float | None

    @property
    def avg(self) -> float | None:
        return (self.total / self.count) if self.count else None


@dataclass
class _Histogram:
    count: int = 0
    total: float = 0.0
    min: float | None = None
    max: float | None = None

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        if self.min is None or value < self.min:
            self.min = value
        if self.max is None or value > self.max:
            self.max = value

    def snapshot(self) -> HistogramSnapshot:
        return HistogramSnapshot(
            count=self.count,
            total=self.total,
            min=self.min,
            max=self.max,
        )


@dataclass
class _Gauge:
    value: float = field(default=0.0)

    def set(self, value: float) -> None:
        self.value = value

    def inc(self, delta: float = 1.0) -> None:
        self.value += delta

    def dec(self, delta: float = 1.0) -> None:
        self.value -= delta


_TagKey = tuple[str, tuple[tuple[str, str], ...]]


class MetricsRegistry:
    """Thread-safe registry for counters, histograms, and gauges."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: defaultdict[_TagKey, int] = defaultdict(int)
        self._hists: dict[_TagKey, _Histogram] = {}
        self._gauges: dict[_TagKey, _Gauge] = {}

    def inc(
        self,
        name: str,
        value: int = 1,
        tags: dict[str, object] | None = None,
    ) -> None:
        key: _TagKey = (name, _normalize_tags(tags))
        with self._lock:
            self._counters[key] += value

    def observe(
        self,
        name: str,
        value: float,
        tags: dict[str, object] | None = None,
    ) -> None:
        key: _TagKey = (name, _normalize_tags(tags))
        with self._lock:
            hist = self._hists.get(key)
            if hist is None:
                hist = _Histogram()
                self._hists[key] = hist
            hist.observe(value)

    def gauge_set(
        self,
        name: str,
        value: float,
        tags: dict[str, object] | None = None,
    ) -> None:
        key: _TagKey = (name, _normalize_tags(tags))
        with self._lock:
            gauge = self._gauges.setdefault(key, _Gauge())
            gauge.set(value)

    def gauge_inc(
        self,
        name: str,
        delta: float = 1.0,
        tags: dict[str, object] | None = None,
    ) -> None:
        key: _TagKey = (name, _normalize_tags(tags))
        with self._lock:
            gauge = self._gauges.setdefault(key, _Gauge())
            gauge.inc(delta)

    def gauge_dec(
        self,
        name: str,
        delta: float = 1.0,
        tags: dict[str, object] | None = None,
    ) -> None:
        key: _TagKey = (name, _normalize_tags(tags))
        with self._lock:
            gauge = self._gauges.setdefault(key, _Gauge())
            gauge.dec(delta)

    def snapshot(self) -> dict[str, object]:
        """Return a point-in-time copy of all metrics.

        All mutable state is copied **under the lock** so that the
        returned dict is safe to read without synchronisation.
        """
        with self._lock:
            counters = dict(self._counters)
            hist_snaps = {k: h.snapshot() for k, h in self._hists.items()}
            gauge_values = {k: round(g.value, 6) for k, g in self._gauges.items()}

        out: dict[str, object] = {}

        for (name, tags), value in counters.items():
            out[_render_metric_key(name, tags)] = value

        for (name, tags), snap in hist_snaps.items():
            prefix = _render_metric_key(name, tags)
            out[f"{prefix}.count"] = snap.count
            out[f"{prefix}.sum"] = round(snap.total, 6)
            if snap.avg is not None:
                out[f"{prefix}.avg"] = round(snap.avg, 6)
            if snap.min is not None:
                out[f"{prefix}.min"] = round(snap.min, 6)
            if snap.max is not None:
                out[f"{prefix}.max"] = round(snap.max, 6)

        for (name, tags), val in gauge_values.items():
            out[_render_metric_key(name, tags)] = val

        return out

    def reset(self) -> None:
        """Clear all counters, histograms, and gauges."""
        with self._lock:
            self._counters.clear()
            self._hists.clear()
            self._gauges.clear()


METRICS = MetricsRegistry()

_listener_lock = threading.Lock()
_listeners: list[Callable[..., None]] = []


def register_metric_listener(fn: Callable[..., None]) -> None:
    """Register a callback invoked on every ``metrics_inc()`` call."""
    with _listener_lock:
        _listeners.append(fn)


def unregister_metric_listener(fn: Callable[..., None]) -> None:
    """Remove one previously-registered metric listener.

    Safe to call even if *fn* is not currently registered.
    """
    with _listener_lock:
        try:
            _listeners.remove(fn)
        except ValueError:
            pass


def unregister_all_listeners() -> None:
    """Remove all metric listeners."""
    with _listener_lock:
        _listeners.clear()


def metrics_inc(metric: str, value: int = 1, **tags: object) -> None:
    METRICS.inc(metric, value=value, tags=tags or None)
    with _listener_lock:
        listeners = tuple(_listeners)
    for fn in listeners:
        try:
            fn(metric, **tags)
        except Exception:
            pass


def metrics_observe(metric: str, value: float, **tags: object) -> None:
    METRICS.observe(metric, value=value, tags=tags or None)


def metrics_gauge_set(metric: str, value: float, **tags: object) -> None:
    METRICS.gauge_set(metric, value=value, tags=tags or None)


def metrics_gauge_inc(metric: str, delta: float = 1.0, **tags: object) -> None:
    METRICS.gauge_inc(metric, delta=delta, tags=tags or None)


def metrics_gauge_dec(metric: str, delta: float = 1.0, **tags: object) -> None:
    METRICS.gauge_dec(metric, delta=delta, tags=tags or None)


def metrics_snapshot() -> dict[str, object]:
    return METRICS.snapshot()


def metrics_reset() -> None:
    """Reset all metrics and remove all listeners."""
    METRICS.reset()
    unregister_all_listeners()
