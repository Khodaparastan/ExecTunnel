"""Measurement framework — layered throughput decomposition.

See ``docs/measurement.md`` for the conceptual overview. This package
holds the report schema and JSON/Markdown/CSV writers used by
``bench_layered.py`` and ``bench_compare.py`` at the repository root.
"""

from exectunnel.bench._report import write_csv, write_json, write_markdown
from exectunnel.bench._schema import (
    SCHEMA_VERSION,
    AgentMeta,
    AgentTelemetry,
    BenchHost,
    BenchReport,
    DerivedMetrics,
    LatencyPercentiles,
    LayerStats,
    PodMeta,
    ProviderMeta,
    WorkloadMeta,
)

__all__ = [
    "AgentMeta",
    "AgentTelemetry",
    "BenchHost",
    "BenchReport",
    "DerivedMetrics",
    "LatencyPercentiles",
    "LayerStats",
    "PodMeta",
    "ProviderMeta",
    "SCHEMA_VERSION",
    "WorkloadMeta",
    "write_csv",
    "write_json",
    "write_markdown",
]
