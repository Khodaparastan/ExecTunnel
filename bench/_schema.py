"""Bench report schema v1 — the canonical shape of every ``BenchReport``.

Design notes
------------

* ``SCHEMA_VERSION`` is a string (not an int) so future extensions can use
  dotted sub-versions (``"1.1"``) without breaking existing parsers.
* All percentile / statistic dicts are open-ended: the writer emits
  whatever the orchestrator populates. Tools that consume the JSON must
  treat missing keys as ``None`` rather than assuming exhaustive shape.
* TypedDicts use ``total=False`` so optional sections (e.g. ``agent_telemetry``
  when STATS was never received) can be omitted entirely.
* CV (coefficient of variation) is expressed as a fraction (0.12 == 12%).
"""

from __future__ import annotations

from typing import Any, Final, TypedDict

SCHEMA_VERSION: Final[str] = "1"

# Threshold above which a layer's coefficient-of-variation flags the run
# as unstable in the Markdown summary. Orchestrator may override when
# filling ``BenchReport.warnings`` — this is the rendering default.
CV_UNSTABLE_THRESHOLD: Final[float] = 0.15


class LatencyPercentiles(TypedDict, total=False):
    """Latency percentiles in milliseconds (population of one run)."""

    p50: float
    p95: float
    p99: float
    mean: float
    stdev: float


class LayerStats(TypedDict, total=False):
    """Statistics for one measurement layer (L1..L4)."""

    throughput_mbps: float
    throughput_mbps_samples: list[float]
    throughput_cv: float
    latency_ms: LatencyPercentiles
    errors: int
    notes: str
    # L4-only extension: L4 is a micro-bench with distinct encode/decode
    # ceilings; other layers leave these unset.
    encode_mbps: float
    decode_mbps: float


class AgentMeta(TypedDict, total=False):
    """Identity of the agent the report was produced against."""

    kind: str  # "python" | "go"
    version: str
    hash: str  # sha256:... of the agent source/binary


class BenchHost(TypedDict, total=False):
    """Identity of the machine that ran the bench."""

    kind: str  # "laptop" | "vm"
    region: str
    hostname: str


class ProviderMeta(TypedDict, total=False):
    """Runflare / provider identity."""

    name: str
    endpoint: str


class PodMeta(TypedDict, total=False):
    """Pod identity & resource limits (when retrievable)."""

    image: str
    cpu_limit: str | None
    mem_limit: str | None


class WorkloadMeta(TypedDict, total=False):
    """Workload descriptor (the shape of the traffic we pushed)."""

    name: str
    params: dict[str, Any]


class AgentTelemetry(TypedDict, total=False):
    """Aggregated agent-emitted STATS snapshots over the run."""

    samples: int
    tx_bytes_total: int
    rx_bytes_total: int
    frames_tx_total: int
    frames_rx_total: int
    stdout_queue_depth_max: int
    stdout_queue_depth_mean: float
    frames_tx_per_sec_mean: float
    frames_tx_per_sec_p95: float
    frames_rx_per_sec_mean: float
    frames_rx_per_sec_p95: float
    dispatch_ms_p50_mean: float
    dispatch_ms_p95_mean: float
    tcp_worker_count_max: int
    udp_worker_count_max: int


class DerivedMetrics(TypedDict, total=False):
    """Cross-layer derived numbers (rendered after the layer table)."""

    agent_overhead_mbps: float  # L3 - L4
    network_overhead_mbps: float  # L2 - L3
    agent_efficiency_ratio: float  # L3 / L4 (close to 1.0 = near-ceiling)


class BenchReport(TypedDict, total=False):
    """Top-level bench report (schema v1).

    Minimum keys for a valid report: ``schema_version``, ``run_id``,
    ``label``. Everything else is optional so partial / failed runs still
    produce a diagnostic artefact.
    """

    schema_version: str
    run_id: str
    label: str
    agent: AgentMeta
    bench_host: BenchHost
    provider: ProviderMeta
    pod: PodMeta
    workload: WorkloadMeta
    layers: dict[str, LayerStats]
    derived: DerivedMetrics
    agent_telemetry: AgentTelemetry
    errors: list[str]
    warnings: list[str]
