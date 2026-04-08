from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field


def _normalize_tags(tags: dict[str, object] | None) -> tuple[tuple[str, str], ...]:
    if not tags:
        return ()
    return tuple(sorted((k, str(v)) for k, v in tags.items()))


def _render_metric_key(name: str, tags: tuple[tuple[str, str], ...]) -> str:
    if not tags:
        return name
    joined = ",".join(f"{k}={v}" for k, v in tags)
    return f"{name}{{{joined}}}"


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

    @property
    def avg(self) -> float | None:
        return (self.total / self.count) if self.count else None


@dataclass
class _Gauge:
    value: float = field(default=0.0)

    def set(self, value: float) -> None:
        self.value = value

    def inc(self, delta: float = 1.0) -> None:
        self.value += delta

    def dec(self, delta: float = 1.0) -> None:
        self.value -= delta


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: defaultdict[tuple[str, tuple[tuple[str, str], ...]], int] = (
            defaultdict(int)
        )
        self._hists: dict[tuple[str, tuple[tuple[str, str], ...]], _Histogram] = {}
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], _Gauge] = {}

    # ------------------------------------------------------------------
    # Counter
    # ------------------------------------------------------------------

    def inc(
        self, name: str, value: int = 1, tags: dict[str, object] | None = None
    ) -> None:
        key = (name, _normalize_tags(tags))
        with self._lock:
            self._counters[key] += value

    # ------------------------------------------------------------------
    # Histogram
    # ------------------------------------------------------------------

    def observe(
        self, name: str, value: float, tags: dict[str, object] | None = None
    ) -> None:
        key = (name, _normalize_tags(tags))
        with self._lock:
            hist = self._hists.get(key)
            if hist is None:
                hist = _Histogram()
                self._hists[key] = hist
            hist.observe(value)

    # ------------------------------------------------------------------
    # Gauge
    # ------------------------------------------------------------------

    def gauge_set(
        self, name: str, value: float, tags: dict[str, object] | None = None
    ) -> None:
        key = (name, _normalize_tags(tags))
        with self._lock:
            g = self._gauges.get(key)
            if g is None:
                g = _Gauge()
                self._gauges[key] = g
            g.set(value)

    def gauge_inc(
        self, name: str, delta: float = 1.0, tags: dict[str, object] | None = None
    ) -> None:
        key = (name, _normalize_tags(tags))
        with self._lock:
            g = self._gauges.get(key)
            if g is None:
                g = _Gauge()
                self._gauges[key] = g
            g.inc(delta)

    def gauge_dec(
        self, name: str, delta: float = 1.0, tags: dict[str, object] | None = None
    ) -> None:
        key = (name, _normalize_tags(tags))
        with self._lock:
            g = self._gauges.get(key)
            if g is None:
                g = _Gauge()
                self._gauges[key] = g
            g.dec(delta)

    # ------------------------------------------------------------------
    # Snapshot / reset
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            counters = dict(self._counters)
            hists = dict(self._hists)
            gauges = dict(self._gauges)
        out: dict[str, object] = {}
        for (name, tags), value in counters.items():
            out[_render_metric_key(name, tags)] = value
        for (name, tags), hist in hists.items():
            prefix = _render_metric_key(name, tags)
            out[f"{prefix}.count"] = hist.count
            out[f"{prefix}.sum"] = round(hist.total, 6)
            if hist.avg is not None:
                out[f"{prefix}.avg"] = round(hist.avg, 6)
            if hist.min is not None:
                out[f"{prefix}.min"] = round(hist.min, 6)
            if hist.max is not None:
                out[f"{prefix}.max"] = round(hist.max, 6)
        for (name, tags), gauge in gauges.items():
            out[_render_metric_key(name, tags)] = round(gauge.value, 6)
        return out

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._hists.clear()
            self._gauges.clear()

    # Keep backward-compat alias
    clear = reset


METRICS = MetricsRegistry()

# ── Metric listeners ─────────────────────────────────────────────────────────
# Listeners are called synchronously inside metrics_inc() after the counter
# is incremented.  Keep listeners fast — no I/O, no blocking.

from typing import Callable as _Callable  # noqa: E402

_listeners: list[_Callable[..., None]] = []


def register_metric_listener(fn: _Callable[..., None]) -> None:
    """Register a callback invoked on every ``metrics_inc()`` call.

    The callback receives ``(name: str, **tags)`` matching the arguments
    passed to ``metrics_inc()``.  Exceptions raised by the callback are
    silently suppressed to avoid disrupting the caller.
    """
    _listeners.append(fn)


def metrics_inc(metric: str, value: int = 1, **tags: object) -> None:
    METRICS.inc(metric, value=value, tags=tags or None)
    for _fn in _listeners:
        try:
            _fn(metric, **tags)
        except Exception:  # noqa: BLE001
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
    METRICS.reset()
    _listeners.clear()
