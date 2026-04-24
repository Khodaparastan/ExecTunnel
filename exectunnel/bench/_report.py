"""Writers for the bench report schema v1: JSON, Markdown, CSV.

JSON is the source of truth. Markdown and CSV are rendered from the
same ``BenchReport`` dict and exist only for human / spreadsheet
consumption.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

from exectunnel.bench._schema import (
    CV_UNSTABLE_THRESHOLD,
    SCHEMA_VERSION,
    BenchReport,
    LayerStats,
)

__all__ = ["write_csv", "write_json", "write_markdown"]


# ── JSON ─────────────────────────────────────────────────────────────────────


def write_json(path: str | os.PathLike[str], report: BenchReport) -> None:
    """Write *report* to *path* as pretty-printed JSON.

    The report is normalised in-place: a missing ``schema_version`` is
    filled in before write so every emitted file can be diffed against
    the schema version unambiguously.
    """
    report.setdefault("schema_version", SCHEMA_VERSION)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=False)
        f.write("\n")


# ── Markdown ─────────────────────────────────────────────────────────────────


def _fmt_float(value: Any, places: int = 2) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{value:.{places}f}"


def _fmt_int(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{int(value):,}"


def _layer_row(name: str, stats: LayerStats) -> str:
    throughput = _fmt_float(stats.get("throughput_mbps"))
    cv = stats.get("throughput_cv")
    cv_str = _fmt_float(cv * 100, 1) + "%" if isinstance(cv, (int, float)) else "—"
    if isinstance(cv, (int, float)) and cv > CV_UNSTABLE_THRESHOLD:
        cv_str += " ⚠"
    latency = stats.get("latency_ms") or {}
    p50 = _fmt_float(latency.get("p50"))
    p95 = _fmt_float(latency.get("p95"))
    p99 = _fmt_float(latency.get("p99"))
    errors = _fmt_int(stats.get("errors", 0))
    return f"| {name} | {throughput} | {cv_str} | {p50} | {p95} | {p99} | {errors} |"


def _l4_row(stats: LayerStats) -> str:
    enc = _fmt_float(stats.get("encode_mbps"))
    dec = _fmt_float(stats.get("decode_mbps"))
    return f"| L4_base64_micro | encode={enc} / decode={dec} MB/s | — | — | — | — | — |"


def write_markdown(path: str | os.PathLike[str], report: BenchReport) -> None:
    """Write a single-page human-readable Markdown summary.

    Layout:
        1. Header (run metadata)
        2. Layer table (throughput, CV, latency percentiles, errors)
        3. Derived cross-layer metrics
        4. Agent telemetry summary (if present)
        5. Warnings / errors lists

    Missing values render as em-dash so the shape is stable regardless
    of what the orchestrator populated.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    agent = report.get("agent") or {}
    bench_host = report.get("bench_host") or {}
    provider = report.get("provider") or {}
    pod = report.get("pod") or {}
    workload = report.get("workload") or {}
    layers = report.get("layers") or {}
    derived = report.get("derived") or {}
    telemetry = report.get("agent_telemetry") or {}
    errors = report.get("errors") or []
    warnings = report.get("warnings") or []

    lines: list[str] = []
    lines.append(f"# Bench Report — {report.get('label', '(unlabeled)')}")
    lines.append("")
    lines.append(f"- **Run ID**: `{report.get('run_id', '—')}`")
    lines.append(f"- **Schema**: v{report.get('schema_version', SCHEMA_VERSION)}")
    lines.append(
        f"- **Agent**: {agent.get('kind', '—')} "
        f"v{agent.get('version', '—')} "
        f"(`{agent.get('hash', '—')[:19]}`)"
    )
    lines.append(
        f"- **Bench host**: {bench_host.get('kind', '—')} "
        f"({bench_host.get('hostname', '—')})"
    )
    lines.append(
        f"- **Provider**: {provider.get('name', '—')} → `{provider.get('endpoint', '—')}`"
    )
    if pod:
        lines.append(
            f"- **Pod**: image=`{pod.get('image', '—')}` "
            f"cpu={pod.get('cpu_limit') or '—'} "
            f"mem={pod.get('mem_limit') or '—'}"
        )
    if workload:
        lines.append(
            f"- **Workload**: `{workload.get('name', '—')}` "
            f"params={workload.get('params') or {}}"
        )
    lines.append("")

    # Layer table
    lines.append("## Layers")
    lines.append("")
    lines.append(
        "| Layer | Throughput (Mbps) | CV | p50 (ms) | p95 (ms) | p99 (ms) | Errors |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for layer_name in ("L1_network", "L2_full_tunnel", "L3_pod_local"):
        if layer_name in layers:
            lines.append(_layer_row(layer_name, layers[layer_name]))
    if "L4_base64_micro" in layers:
        lines.append(_l4_row(layers["L4_base64_micro"]))
    lines.append("")

    # Derived
    if derived:
        lines.append("## Derived")
        lines.append("")
        if "network_overhead_mbps" in derived:
            lines.append(
                f"- **Network overhead (L2 − L3)**: "
                f"{_fmt_float(derived['network_overhead_mbps'])} Mbps"
            )
        if "agent_overhead_mbps" in derived:
            lines.append(
                f"- **Agent overhead (L3 − L4)**: "
                f"{_fmt_float(derived['agent_overhead_mbps'])} Mbps"
            )
        if "agent_efficiency_ratio" in derived:
            lines.append(
                f"- **Agent efficiency (L3 / L4)**: "
                f"{_fmt_float(derived['agent_efficiency_ratio'], 3)}"
            )
        lines.append("")

    # Agent telemetry
    if telemetry:
        lines.append("## Agent telemetry")
        lines.append("")
        lines.append(f"- Samples: {_fmt_int(telemetry.get('samples'))}")
        lines.append(
            f"- Bytes: tx={_fmt_int(telemetry.get('tx_bytes_total'))} "
            f"rx={_fmt_int(telemetry.get('rx_bytes_total'))}"
        )
        lines.append(
            f"- Frames: tx={_fmt_int(telemetry.get('frames_tx_total'))} "
            f"rx={_fmt_int(telemetry.get('frames_rx_total'))}"
        )
        lines.append(
            f"- Stdout queue depth: max={_fmt_int(telemetry.get('stdout_queue_depth_max'))} "
            f"mean={_fmt_float(telemetry.get('stdout_queue_depth_mean'), 1)}"
        )
        lines.append(
            f"- Dispatch latency (ms, per-snapshot mean): "
            f"p50={_fmt_float(telemetry.get('dispatch_ms_p50_mean'), 3)} "
            f"p95={_fmt_float(telemetry.get('dispatch_ms_p95_mean'), 3)}"
        )
        lines.append(
            f"- Worker count max: tcp={_fmt_int(telemetry.get('tcp_worker_count_max'))} "
            f"udp={_fmt_int(telemetry.get('udp_worker_count_max'))}"
        )
        lines.append("")

    # CV stability note
    unstable = [
        name
        for name, st in layers.items()
        if isinstance(st.get("throughput_cv"), (int, float))
        and st["throughput_cv"] > CV_UNSTABLE_THRESHOLD
    ]
    if unstable:
        lines.append("## Stability")
        lines.append("")
        lines.append(
            f"⚠ The following layer(s) had CV > "
            f"{CV_UNSTABLE_THRESHOLD * 100:.0f}% across repeats "
            f"and should be considered **unstable**: {', '.join(unstable)}."
        )
        lines.append("")

    # Warnings / errors
    if warnings:
        lines.append("## Warnings")
        lines.append("")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")
    if errors:
        lines.append("## Errors")
        lines.append("")
        for e in errors:
            lines.append(f"- {e}")
        lines.append("")

    # L4 caveat — reminder that it does not motivate a rewrite.
    if "L4_base64_micro" in layers:
        lines.append(
            "> **Note on L4**: the L4 micro-bench is CPU-local. "
            "Python's `base64` module is C-backed and already fast, "
            "so a Go/Rust rewrite is unlikely to move L4 much. "
            "The real agent wins come from GIL-free framing and "
            "goroutine-native I/O, not from L4."
        )
        lines.append("")

    p.write_text("\n".join(lines), encoding="utf-8")


# ── CSV ──────────────────────────────────────────────────────────────────────


def _csv_rows(report: BenchReport) -> list[list[str]]:
    """Flatten the report into long-format rows.

    Columns: ``layer, metric, stat, value, unit, tags_json``.
    One row per (layer, metric, stat) tuple. Missing values are skipped.
    """
    rows: list[list[str]] = []
    run_tags = json.dumps(
        {
            "run_id": report.get("run_id", ""),
            "label": report.get("label", ""),
            "agent_kind": (report.get("agent") or {}).get("kind", ""),
            "agent_version": (report.get("agent") or {}).get("version", ""),
            "bench_host_kind": (report.get("bench_host") or {}).get("kind", ""),
        },
        sort_keys=True,
    )

    for layer, stats in (report.get("layers") or {}).items():
        if "throughput_mbps" in stats:
            rows.append([
                layer,
                "throughput",
                "mean",
                str(stats["throughput_mbps"]),
                "mbps",
                run_tags,
            ])
        if "throughput_cv" in stats:
            rows.append([
                layer,
                "throughput",
                "cv",
                str(stats["throughput_cv"]),
                "fraction",
                run_tags,
            ])
        if "encode_mbps" in stats:
            rows.append([
                layer,
                "encode",
                "mean",
                str(stats["encode_mbps"]),
                "mbps",
                run_tags,
            ])
        if "decode_mbps" in stats:
            rows.append([
                layer,
                "decode",
                "mean",
                str(stats["decode_mbps"]),
                "mbps",
                run_tags,
            ])
        latency = stats.get("latency_ms") or {}
        for stat, value in latency.items():
            rows.append([layer, "latency", stat, str(value), "ms", run_tags])
        if "errors" in stats:
            rows.append([
                layer,
                "errors",
                "count",
                str(stats["errors"]),
                "count",
                run_tags,
            ])

    for key, value in (report.get("derived") or {}).items():
        rows.append(["derived", key, "value", str(value), "mbps", run_tags])

    for key, value in (report.get("agent_telemetry") or {}).items():
        unit = _telemetry_unit(key)
        rows.append(["agent_telemetry", key, "value", str(value), unit, run_tags])

    return rows


def _telemetry_unit(key: str) -> str:
    if key.endswith("_bytes_total"):
        return "bytes"
    if key.endswith("_total"):
        return "count"
    if "per_sec" in key:
        return "frames_per_sec"
    if key.startswith("dispatch_ms"):
        return "ms"
    if "queue_depth" in key:
        return "frames"
    if key.endswith("_count_max"):
        return "count"
    if key == "samples":
        return "count"
    return ""


def write_csv(path: str | os.PathLike[str], report: BenchReport) -> None:
    """Write a long-format CSV suitable for spreadsheet pivoting.

    Columns: ``layer, metric, stat, value, unit, tags_json``. Empty
    cells are written for missing values rather than omitted, so every
    row has the same number of columns.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["layer", "metric", "stat", "value", "unit", "tags_json"])
        for row in _csv_rows(report):
            w.writerow(row)
