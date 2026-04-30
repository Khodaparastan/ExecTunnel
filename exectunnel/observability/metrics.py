from __future__ import annotations

import contextlib
import logging
import math
import os
import random
import threading
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum, unique
from typing import Final, NamedTuple, TypeAlias

__all__ = [
    "METRICS",
    "MetricEvent",
    "MetricKind",
    "MetricsRegistry",
    "metrics_gauge_dec",
    "metrics_gauge_inc",
    "metrics_gauge_set",
    "metrics_inc",
    "metrics_observe",
    "metrics_reset",
    "metrics_snapshot",
    "register_metric_listener",
    "unregister_all_listeners",
    "unregister_metric_listener",
]

logger = logging.getLogger("exectunnel.metrics")

_RESERVOIR_SIZE: Final[int] = 1_024
_SNAPSHOT_FLOAT_PRECISION: Final[int] = 6

_NormalizedTags: TypeAlias = tuple[tuple[str, str], ...]
_MetricKey: TypeAlias = tuple[str, _NormalizedTags]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw, 10)
    except ValueError:
        return default
    return value if value >= minimum else default


def _env_set(name: str, default: str) -> frozenset[str]:
    raw = os.getenv(name, default)
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


_METRICS_STRICT: Final[bool] = _env_bool("EXECTUNNEL_METRICS_STRICT", False)
_MAX_TAGS: Final[int] = _env_int("EXECTUNNEL_METRICS_MAX_TAGS", 8, minimum=0)
_MAX_TAG_VALUE_CHARS: Final[int] = _env_int(
    "EXECTUNNEL_METRICS_MAX_TAG_VALUE_CHARS",
    80,
    minimum=8,
)
_DROP_TAG_KEYS: Final[frozenset[str]] = _env_set(
    "EXECTUNNEL_METRICS_DROP_TAGS",
    "conn_id,flow_id,handler_id,host,port,bytes",
)


# ------------------------------------------------------------------
# Tag + validation helpers
# ------------------------------------------------------------------


def _validate_metric_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("Metric name must be a string")
    normalized = name.strip()
    if not normalized:
        raise ValueError("Metric name must not be empty")
    return normalized


def _validate_counter_delta(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Counter increment must be an int")
    if value < 0:
        raise ValueError("Counter increment must be >= 0")
    return value


def _validate_finite_number(value: float | int, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field_name} must be an int or float")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{field_name} must be finite")
    return numeric


def _validate_non_negative_delta(value: float | int, *, field_name: str) -> float:
    numeric = _validate_finite_number(value, field_name=field_name)
    if numeric < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return numeric


def _tag_str(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _escape_tag_component(value: str) -> str:
    return (
        value
        .replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace(",", "\\,")
        .replace("=", "\\=")
    )


def _normalize_tags(tags: Mapping[str, object] | None) -> _NormalizedTags:
    if not tags:
        return ()

    normalized: list[tuple[str, str]] = []
    for key, value in tags.items():
        if not isinstance(key, str):
            raise TypeError("Metric tag keys must be strings")
        key_text = key.strip()
        if not key_text:
            raise ValueError("Metric tag keys must not be empty")
        if key_text in _DROP_TAG_KEYS:
            continue
        if _MAX_TAGS and len(normalized) >= _MAX_TAGS:
            continue
        value_text = _tag_str(value)
        if len(value_text) > _MAX_TAG_VALUE_CHARS:
            value_text = value_text[: _MAX_TAG_VALUE_CHARS - 3] + "..."
        normalized.append((key_text, value_text))

    normalized.sort()
    return tuple(normalized)


def _render_metric_key(name: str, tags: _NormalizedTags) -> str:
    if not tags:
        return name
    joined = ",".join(
        f"{_escape_tag_component(key)}={_escape_tag_component(value)}"
        for key, value in tags
    )
    return f"{name}{{{joined}}}"


def _round_snapshot_value(value: float) -> float:
    return round(value, _SNAPSHOT_FLOAT_PRECISION)


# ------------------------------------------------------------------
# Metric kind enum
# ------------------------------------------------------------------


@unique
class MetricKind(Enum):
    COUNTER = "counter"
    HISTOGRAM = "histogram"
    GAUGE = "gauge"


# ------------------------------------------------------------------
# Histogram
# ------------------------------------------------------------------


class HistogramSnapshot(NamedTuple):
    count: int
    total: float
    min: float | None
    max: float | None
    p50: float | None
    p95: float | None
    p99: float | None

    @property
    def avg(self) -> float | None:
        return (self.total / self.count) if self.count else None


@dataclass(slots=True)
class _Histogram:
    count: int = 0
    total: float = 0.0
    min: float | None = None
    max: float | None = None
    _reservoir: list[float] = field(default_factory=list)
    _random: random.Random = field(default_factory=random.Random, repr=False)

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        if self.min is None or value < self.min:
            self.min = value
        if self.max is None or value > self.max:
            self.max = value

        if len(self._reservoir) < _RESERVOIR_SIZE:
            self._reservoir.append(value)
            return

        # Vitter's Algorithm R.
        index = self._random.randrange(self.count)
        if index < _RESERVOIR_SIZE:
            self._reservoir[index] = value

    def snapshot(self) -> HistogramSnapshot:
        sorted_values = sorted(self._reservoir)
        return HistogramSnapshot(
            count=self.count,
            total=self.total,
            min=self.min,
            max=self.max,
            p50=_percentile(sorted_values, 50.0),
            p95=_percentile(sorted_values, 95.0),
            p99=_percentile(sorted_values, 99.0),
        )


def _percentile(sorted_values: list[float], percentile: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]

    rank = (percentile / 100.0) * (len(sorted_values) - 1)
    lower_index = math.floor(rank)
    upper_index = math.ceil(rank)

    lower_value = sorted_values[lower_index]
    upper_value = sorted_values[upper_index]

    if lower_index == upper_index:
        return lower_value

    fraction = rank - lower_index
    return lower_value + (upper_value - lower_value) * fraction


# ------------------------------------------------------------------
# Gauge
# ------------------------------------------------------------------


@dataclass(slots=True)
class _Gauge:
    value: float = 0.0

    def set(self, value: float) -> float:
        self.value = value
        return self.value

    def inc(self, delta: float = 1.0) -> float:
        self.value += delta
        return self.value

    def dec(self, delta: float = 1.0) -> float:
        self.value -= delta
        return self.value


# ------------------------------------------------------------------
# Listener event
# ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetricEvent:
    """Payload delivered to metric listeners on every metric mutation."""

    kind: MetricKind
    name: str
    value: float
    tags: dict[str, object]
    operation: str = "update"
    current_value: float | None = None


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------


class MetricsRegistry:
    """Thread-safe registry for counters, histograms, and gauges.

    Metric-type collisions for the same ``(name, tags)`` key raise ``ValueError``.
    All public mutation methods notify registered listeners after the internal
    state update completes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: defaultdict[_MetricKey, int] = defaultdict(int)
        self._hists: defaultdict[_MetricKey, _Histogram] = defaultdict(_Histogram)
        self._gauges: defaultdict[_MetricKey, _Gauge] = defaultdict(_Gauge)
        self._kinds: dict[_MetricKey, MetricKind] = {}
        self._name_kinds: dict[str, MetricKind] = {}

    # ---- internal helpers ------------------------------------------

    def _check_kind(self, key: _MetricKey, expected: MetricKind) -> None:
        existing_for_name = self._name_kinds.get(key[0])
        if existing_for_name is not None and existing_for_name is not expected:
            raise ValueError(
                f"Metric {key[0]!r} already registered as "
                f"{existing_for_name.value}; cannot use as {expected.value}"
            )
        self._name_kinds[key[0]] = expected

        existing = self._kinds.get(key)
        if existing is not None and existing is not expected:
            rendered = _render_metric_key(key[0], key[1])
            raise ValueError(
                f"Metric {rendered!r} already registered as {existing.value}; "
                f"cannot use as {expected.value}"
            )
        self._kinds[key] = expected

    # ---- counters --------------------------------------------------

    def inc(
        self,
        name: str,
        value: int = 1,
        tags: Mapping[str, object] | None = None,
    ) -> None:
        metric_name = _validate_metric_name(name)
        delta = _validate_counter_delta(value)
        event_tags = dict(tags or {})
        key: _MetricKey = (metric_name, _normalize_tags(event_tags))

        with self._lock:
            self._check_kind(key, MetricKind.COUNTER)
            self._counters[key] += delta
            current = self._counters[key]

        _notify_listeners(
            MetricEvent(
                kind=MetricKind.COUNTER,
                name=metric_name,
                value=float(delta),
                tags=event_tags,
                operation="inc",
                current_value=float(current),
            )
        )

    # ---- histograms ------------------------------------------------

    def observe(
        self,
        name: str,
        value: float,
        tags: Mapping[str, object] | None = None,
    ) -> None:
        metric_name = _validate_metric_name(name)
        numeric = _validate_finite_number(value, field_name="Histogram value")
        event_tags = dict(tags or {})
        key: _MetricKey = (metric_name, _normalize_tags(event_tags))

        with self._lock:
            self._check_kind(key, MetricKind.HISTOGRAM)
            hist = self._hists[key]
            hist.observe(numeric)

        _notify_listeners(
            MetricEvent(
                kind=MetricKind.HISTOGRAM,
                name=metric_name,
                value=numeric,
                tags=event_tags,
                operation="observe",
                current_value=numeric,
            )
        )

    # ---- gauges ----------------------------------------------------

    def gauge_set(
        self,
        name: str,
        value: float,
        tags: Mapping[str, object] | None = None,
    ) -> None:
        metric_name = _validate_metric_name(name)
        numeric = _validate_finite_number(value, field_name="Gauge value")
        event_tags = dict(tags or {})
        key: _MetricKey = (metric_name, _normalize_tags(event_tags))

        with self._lock:
            self._check_kind(key, MetricKind.GAUGE)
            current = self._gauges[key].set(numeric)

        _notify_listeners(
            MetricEvent(
                kind=MetricKind.GAUGE,
                name=metric_name,
                value=numeric,
                tags=event_tags,
                operation="set",
                current_value=current,
            )
        )

    def gauge_inc(
        self,
        name: str,
        delta: float = 1.0,
        tags: Mapping[str, object] | None = None,
    ) -> None:
        metric_name = _validate_metric_name(name)
        numeric = _validate_non_negative_delta(delta, field_name="Gauge delta")
        event_tags = dict(tags or {})
        key: _MetricKey = (metric_name, _normalize_tags(event_tags))

        with self._lock:
            self._check_kind(key, MetricKind.GAUGE)
            current = self._gauges[key].inc(numeric)

        _notify_listeners(
            MetricEvent(
                kind=MetricKind.GAUGE,
                name=metric_name,
                value=numeric,
                tags=event_tags,
                operation="inc",
                current_value=current,
            )
        )

    def gauge_dec(
        self,
        name: str,
        delta: float = 1.0,
        tags: Mapping[str, object] | None = None,
    ) -> None:
        metric_name = _validate_metric_name(name)
        numeric = _validate_non_negative_delta(delta, field_name="Gauge delta")
        event_tags = dict(tags or {})
        key: _MetricKey = (metric_name, _normalize_tags(event_tags))

        with self._lock:
            self._check_kind(key, MetricKind.GAUGE)
            current = self._gauges[key].dec(numeric)

        _notify_listeners(
            MetricEvent(
                kind=MetricKind.GAUGE,
                name=metric_name,
                value=-numeric,
                tags=event_tags,
                operation="dec",
                current_value=current,
            )
        )

    # ---- queries ---------------------------------------------------

    def registered_names(self) -> set[str]:
        with self._lock:
            return {key[0] for key in self._kinds}

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            counters = dict(self._counters)
            hist_snapshots = {key: hist.snapshot() for key, hist in self._hists.items()}
            gauges = {key: gauge.value for key, gauge in self._gauges.items()}

        out: dict[str, object] = {}

        for (name, tags), value in counters.items():
            out[_render_metric_key(name, tags)] = value

        for (name, tags), snap in hist_snapshots.items():
            prefix = _render_metric_key(name, tags)
            out[f"{prefix}.count"] = snap.count
            out[f"{prefix}.sum"] = _round_snapshot_value(snap.total)

            if snap.avg is not None:
                out[f"{prefix}.avg"] = _round_snapshot_value(snap.avg)
            if snap.min is not None:
                out[f"{prefix}.min"] = _round_snapshot_value(snap.min)
            if snap.max is not None:
                out[f"{prefix}.max"] = _round_snapshot_value(snap.max)
            if snap.p50 is not None:
                out[f"{prefix}.p50"] = _round_snapshot_value(snap.p50)
            if snap.p95 is not None:
                out[f"{prefix}.p95"] = _round_snapshot_value(snap.p95)
            if snap.p99 is not None:
                out[f"{prefix}.p99"] = _round_snapshot_value(snap.p99)

        for (name, tags), value in gauges.items():
            out[_render_metric_key(name, tags)] = _round_snapshot_value(value)

        return out

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._hists.clear()
            self._gauges.clear()
            self._kinds.clear()
            self._name_kinds.clear()


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------


METRICS = MetricsRegistry()


# ------------------------------------------------------------------
# Listener subsystem
# ------------------------------------------------------------------


_listener_lock = threading.Lock()
_listeners: list[Callable[[MetricEvent], None]] = []


def register_metric_listener(fn: Callable[[MetricEvent], None]) -> None:
    """Register a callback invoked on every metric mutation."""
    with _listener_lock:
        if fn not in _listeners:
            _listeners.append(fn)


def unregister_metric_listener(fn: Callable[[MetricEvent], None]) -> None:
    """Remove a previously registered listener. Safe if *fn* is absent."""
    with _listener_lock, contextlib.suppress(ValueError):
        _listeners.remove(fn)


def unregister_all_listeners() -> None:
    """Remove all registered metric listeners."""
    with _listener_lock:
        _listeners.clear()


def _notify_listeners(event: MetricEvent) -> None:
    """Dispatch *event* to all registered listeners.

    Listener failures are logged and suppressed so observability never breaks
    the data path.
    """
    with _listener_lock:
        listeners = tuple(_listeners)

    for fn in listeners:
        try:
            fn(event)
        except Exception:
            logger.debug(
                "Metric listener %r raised; suppressing",
                fn,
                exc_info=True,
            )


# ------------------------------------------------------------------
# Public convenience helpers
# ------------------------------------------------------------------


def _handle_metric_error(operation: str, metric: str, exc: Exception) -> None:
    if _METRICS_STRICT:
        raise exc
    logger.debug(
        "Metric %s(%r) failed; suppressing because EXECTUNNEL_METRICS_STRICT is false",
        operation,
        metric,
        exc_info=(type(exc), exc, exc.__traceback__),
    )


def metrics_inc(metric: str, value: int = 1, **tags: object) -> None:
    try:
        METRICS.inc(metric, value=value, tags=tags or None)
    except Exception as exc:  # noqa: BLE001
        _handle_metric_error("inc", metric, exc)


def metrics_observe(metric: str, value: float, **tags: object) -> None:
    try:
        METRICS.observe(metric, value=value, tags=tags or None)
    except Exception as exc:  # noqa: BLE001
        _handle_metric_error("observe", metric, exc)


def metrics_gauge_set(metric: str, value: float, **tags: object) -> None:
    try:
        METRICS.gauge_set(metric, value=value, tags=tags or None)
    except Exception as exc:  # noqa: BLE001
        _handle_metric_error("gauge_set", metric, exc)


def metrics_gauge_inc(metric: str, delta: float = 1.0, **tags: object) -> None:
    try:
        METRICS.gauge_inc(metric, delta=delta, tags=tags or None)
    except Exception as exc:  # noqa: BLE001
        _handle_metric_error("gauge_inc", metric, exc)


def metrics_gauge_dec(metric: str, delta: float = 1.0, **tags: object) -> None:
    try:
        METRICS.gauge_dec(metric, delta=delta, tags=tags or None)
    except Exception as exc:  # noqa: BLE001
        _handle_metric_error("gauge_dec", metric, exc)


def metrics_snapshot() -> dict[str, object]:
    return METRICS.snapshot()


def metrics_reset() -> None:
    """Reset all metric data.

    Listeners are intentionally preserved.
    """
    METRICS.reset()
