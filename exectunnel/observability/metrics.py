from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass


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


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: defaultdict[tuple[str, tuple[tuple[str, str], ...]], int] = (
            defaultdict(int)
        )
        self._hists: dict[tuple[str, tuple[tuple[str, str], ...]], _Histogram] = {}

    def inc(
        self, name: str, value: int = 1, tags: dict[str, object] | None = None
    ) -> None:
        key = (name, _normalize_tags(tags))
        with self._lock:
            self._counters[key] += value

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

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            counters = dict(self._counters)
            hists = dict(self._hists)
        out: dict[str, object] = {}
        for (name, tags), value in counters.items():
            out[_render_metric_key(name, tags)] = value
        for (name, tags), hist in hists.items():
            prefix = _render_metric_key(name, tags)
            out[f"{prefix}.count"] = hist.count
            out[f"{prefix}.sum"] = round(hist.total, 6)
            if hist.min is not None:
                out[f"{prefix}.min"] = round(hist.min, 6)
            if hist.max is not None:
                out[f"{prefix}.max"] = round(hist.max, 6)
        return out

    def clear(self) -> None:
        with self._lock:
            self._counters.clear()
            self._hists.clear()


METRICS = MetricsRegistry()


def metrics_inc(metric: str, value: int = 1, **tags: object) -> None:
    METRICS.inc(metric, value=value, tags=tags or None)


def metrics_observe(metric: str, value: float, **tags: object) -> None:
    METRICS.observe(metric, value=value, tags=tags or None)


def metrics_snapshot() -> dict[str, object]:
    return METRICS.snapshot()


def metrics_reset() -> None:
    METRICS.clear()
