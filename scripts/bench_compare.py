#!/usr/bin/env python3
"""bench_compare — diff two or more bench JSON reports.

Usage
-----
    python bench_compare.py a.json b.json [c.json ...] [--output diff.md]
         [--throughput-threshold-pct 5] [--latency-threshold-ms 10]

Emits a Markdown delta table on stdout (or ``--output``) with one column
per report; deltas are computed relative to the first report. Metrics
whose delta exceeds the configured thresholds are flagged.

Refuses to diff reports with mismatched ``schema_version``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: not a JSON object")
    return data


def _get_layer_throughput(report: dict[str, Any], layer: str) -> float | None:
    layers = report.get("layers") or {}
    stats = layers.get(layer) or {}
    v = stats.get("throughput_mbps")
    return float(v) if isinstance(v, (int, float)) else None


def _get_layer_latency(report: dict[str, Any], layer: str, stat: str) -> float | None:
    layers = report.get("layers") or {}
    latency = (layers.get(layer) or {}).get("latency_ms") or {}
    v = latency.get(stat)
    return float(v) if isinstance(v, (int, float)) else None


def _fmt_delta(base: float | None, other: float | None, unit: str) -> str:
    if base is None or other is None:
        return "—"
    delta = other - base
    if unit == "pct" and base != 0:
        pct = (delta / base) * 100.0
        return f"{delta:+.2f} ({pct:+.1f}%)"
    return f"{delta:+.2f}"


def _check_thresholds(
    base: float | None,
    other: float | None,
    *,
    kind: str,
    pct_threshold: float,
    abs_threshold_ms: float,
) -> bool:
    if base is None or other is None or base == 0:
        return False
    if kind == "throughput":
        return abs((other - base) / base) * 100.0 > pct_threshold
    if kind == "latency":
        return abs(other - base) > abs_threshold_ms
    return False


def _compare(
    reports: list[dict[str, Any]],
    labels: list[str],
    *,
    pct_threshold: float,
    abs_threshold_ms: float,
) -> str:
    versions = {r.get("schema_version") for r in reports}
    if len(versions) != 1:
        raise SystemExit(
            f"Refusing to diff: mismatched schema_version across reports: {versions}"
        )

    lines: list[str] = []
    lines.append("# Bench diff")
    lines.append("")
    lines.append(f"- **Schema**: v{next(iter(versions))}")
    lines.append(f"- **Base**: `{labels[0]}`")
    for i, label in enumerate(labels[1:], start=1):
        lines.append(f"- **Compare #{i}**: `{label}`")
    lines.append("")

    # Throughput table
    layers_of_interest = ("L1_network", "L2_full_tunnel", "L3_pod_local")
    headers = ["Layer", "Base (Mbps)"] + [f"Δ vs `{label}`" for label in labels[1:]]
    lines.append("## Throughput")
    lines.append("")
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for layer in layers_of_interest:
        base_val = _get_layer_throughput(reports[0], layer)
        row = [layer, f"{base_val:.2f}" if base_val is not None else "—"]
        for r in reports[1:]:
            other = _get_layer_throughput(r, layer)
            flag = (
                " ⚠"
                if _check_thresholds(
                    base_val,
                    other,
                    kind="throughput",
                    pct_threshold=pct_threshold,
                    abs_threshold_ms=abs_threshold_ms,
                )
                else ""
            )
            row.append(_fmt_delta(base_val, other, "pct") + flag)
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # Latency table (p50/p95/p99)
    lines.append("## Latency (ms)")
    lines.append("")
    headers = ["Layer", "Stat", "Base"] + [f"Δ vs `{label}`" for label in labels[1:]]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for layer in layers_of_interest:
        for stat in ("p50", "p95", "p99"):
            base_val = _get_layer_latency(reports[0], layer, stat)
            row = [layer, stat, f"{base_val:.2f}" if base_val is not None else "—"]
            for r in reports[1:]:
                other = _get_layer_latency(r, layer, stat)
                flag = (
                    " ⚠"
                    if _check_thresholds(
                        base_val,
                        other,
                        kind="latency",
                        pct_threshold=pct_threshold,
                        abs_threshold_ms=abs_threshold_ms,
                    )
                    else ""
                )
                row.append(_fmt_delta(base_val, other, "abs") + flag)
            lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    lines.append(
        f"⚠ marks deltas exceeding "
        f"{pct_threshold:.1f}% throughput or {abs_threshold_ms:.1f} ms latency."
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("reports", nargs="+", help="Two or more JSON report paths.")
    ap.add_argument(
        "--output", "-o", help="Write Markdown to this file (default: stdout)."
    )
    ap.add_argument(
        "--throughput-threshold-pct",
        type=float,
        default=5.0,
        help="Flag throughput deltas exceeding this %% (default: 5).",
    )
    ap.add_argument(
        "--latency-threshold-ms",
        type=float,
        default=10.0,
        help="Flag latency deltas exceeding this ms (default: 10).",
    )
    args = ap.parse_args(argv)

    if len(args.reports) < 2:
        ap.error("Need at least two report files to diff.")

    paths = [Path(p) for p in args.reports]
    reports = [_load(p) for p in paths]
    labels = [r.get("label") or p.stem for r, p in zip(reports, paths, strict=True)]

    md = _compare(
        reports,
        labels,
        pct_threshold=args.throughput_threshold_pct,
        abs_threshold_ms=args.latency_threshold_ms,
    )

    if args.output:
        Path(args.output).write_text(md, encoding="utf-8")
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
