"""exectunnel.observability — logging, metrics, tracing & reporting."""

from .exporters import Exporter, build_exporters, build_obs_payload
from .logging import (
    LevelName,
    LogEntry,
    LogRingBuffer,
    attach_rich_logging,
    configure_logging,
    install_ring_buffer,
)
from .metrics import (
    METRICS,
    MetricEvent,
    MetricKind,
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
    unregister_metric_listener,
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
from .utils import parse_bool_env, parse_float_env, parse_int_env

__all__ = [
    # exporters
    "Exporter",
    "build_exporters",
    "build_obs_payload",
    # logging
    "LevelName",
    "LogEntry",
    "LogRingBuffer",
    "attach_rich_logging",
    "configure_logging",
    "install_ring_buffer",
    "parse_bool_env",
    "parse_float_env",
    "parse_int_env",
    # metrics
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
    "unregister_metric_listener",
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
