"""exectunnel.observability — logging, metrics, tracing & reporting."""

from .exporters import Exporter, build_exporters, build_obs_payload
from .logging import (
    LevelName,
    LogEntry,
    LogRingBuffer,
    configure_logging,
    install_ring_buffer,
)
from .metrics import (
    METRICS,
    MetricsRegistry,
    metrics_gauge_dec,
    metrics_gauge_inc,
    metrics_gauge_set,
    metrics_inc,
    metrics_observe,
    metrics_reset,
    metrics_snapshot,
    register_metric_listener,
    unregister_all_listeners,
)
from .reporter import run_metrics_reporter
from .tracing import (
    aspan,
    current_parent_span_id,
    current_span_id,
    current_trace_id,
    span,
    start_trace,
)

__all__ = [
    # exporters
    "Exporter",
    "build_exporters",
    "build_obs_payload",
    # logging
    "LevelName",
    "LogEntry",
    "LogRingBuffer",
    "configure_logging",
    "install_ring_buffer",
    # metrics
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
    "unregister_all_listeners",
    # reporter
    "run_metrics_reporter",
    # tracing
    "aspan",
    "current_parent_span_id",
    "current_span_id",
    "current_trace_id",
    "span",
    "start_trace",
]
