#!/usr/bin/env python3
"""bench_layered — layered throughput decomposition (L1..L4).

Produces a JSON/Markdown/CSV triple under ``./bench-reports/`` and answers
the question "where is the cost?" by isolating:

* **L1** — client ↔ runflare edge raw WebSocket echo (pure network baseline).
* **L2** — client ↔ pod stdout via full agent stack, external target.
* **L3** — client ↔ pod stdout via full agent, target = in-pod pod_echo.
* **L4** — in-process base64 encode/decode micro-benchmark.

L1 and L4 run without requiring a live session. L2 and L3 require a
configured ``TunnelConfig`` + session endpoint; when ``--endpoint`` is
omitted, only L1 and L4 run and the report is flagged accordingly.

Usage
-----

    python bench_layered.py --label baseline-py --bench-host-kind laptop
    python bench_layered.py --label go-run --agent go --endpoint wss://...
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as _dt
import hashlib
import json
import os
import socket
import statistics
import sys
import time
import uuid
from pathlib import Path

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore[assignment]

from exectunnel.bench import (
    SCHEMA_VERSION,
    BenchReport,
    LayerStats,
    write_csv,
    write_json,
    write_markdown,
)

# ── L1 (raw WebSocket echo) ──────────────────────────────────────────────────


async def run_l1_network(
    endpoint: str, duration_sec: float, payload_size: int
) -> LayerStats:
    """Echo raw WebSocket frames to *endpoint* for *duration_sec* seconds.

    Measures bidirectional throughput + per-message RTT. Requires the
    target to act as a simple echo server. When ``websockets`` is not
    installed or the endpoint is unreachable, returns a LayerStats with
    ``errors`` and a note.
    """
    if websockets is None:
        return LayerStats(
            errors=1, notes="L1 skipped: websockets package not installed"
        )

    payload = os.urandom(payload_size)
    latencies_ms: list[float] = []
    bytes_sent = 0
    errors = 0

    try:
        async with websockets.connect(endpoint, max_size=payload_size * 4) as ws:
            deadline = time.monotonic() + duration_sec
            while time.monotonic() < deadline:
                t0 = time.monotonic()
                try:
                    await ws.send(payload)
                    resp = await ws.recv()
                except Exception:  # noqa: BLE001
                    errors += 1
                    continue
                latencies_ms.append((time.monotonic() - t0) * 1000.0)
                bytes_sent += len(payload) + (
                    len(resp) if isinstance(resp, bytes) else 0
                )
    except Exception as exc:  # noqa: BLE001
        return LayerStats(
            errors=1,
            notes=f"L1 failed to connect: {exc}",
        )

    elapsed = sum(latencies_ms) / 1000.0 if latencies_ms else duration_sec
    throughput_mbps = (bytes_sent * 8 / 1_000_000) / max(elapsed, 1e-9)
    return _latency_to_layer(
        latencies_ms, throughput_mbps=throughput_mbps, errors=errors
    )


def _latency_to_layer(
    samples_ms: list[float], *, throughput_mbps: float, errors: int
) -> LayerStats:
    if not samples_ms:
        return LayerStats(
            throughput_mbps=0.0,
            errors=max(errors, 1),
            notes="no successful samples",
        )
    s = sorted(samples_ms)
    n = len(s)

    def pct(p: float) -> float:
        idx = min(n - 1, int(p * n))
        return s[idx]

    return LayerStats(
        throughput_mbps=throughput_mbps,
        latency_ms={
            "p50": pct(0.50),
            "p95": pct(0.95),
            "p99": pct(0.99),
            "mean": statistics.fmean(s),
            "stdev": statistics.pstdev(s) if n > 1 else 0.0,
        },
        errors=errors,
    )


# ── L4 (base64 micro) ────────────────────────────────────────────────────────


def run_l4_base64_micro(payload_size: int, duration_sec: float) -> LayerStats:
    """Measure base64 encode/decode throughput in-process (no session)."""
    payload = os.urandom(payload_size)
    # Warmup.
    _ = base64.urlsafe_b64encode(payload)

    # Encode
    t_end = time.monotonic() + duration_sec / 2.0
    enc_bytes = 0
    while time.monotonic() < t_end:
        _ = base64.urlsafe_b64encode(payload)
        enc_bytes += len(payload)
    enc_elapsed = duration_sec / 2.0

    # Decode (prepare one encoded payload to decode repeatedly)
    encoded = base64.urlsafe_b64encode(payload)
    t_end = time.monotonic() + duration_sec / 2.0
    dec_bytes = 0
    while time.monotonic() < t_end:
        _ = base64.urlsafe_b64decode(encoded)
        dec_bytes += len(encoded)
    dec_elapsed = duration_sec / 2.0

    encode_mbps = (enc_bytes * 8 / 1_000_000) / max(enc_elapsed, 1e-9)
    decode_mbps = (dec_bytes * 8 / 1_000_000) / max(dec_elapsed, 1e-9)
    return LayerStats(encode_mbps=encode_mbps, decode_mbps=decode_mbps, errors=0)


# ── Repeats + CV helpers ─────────────────────────────────────────────────────


def _aggregate_repeats(runs: list[LayerStats]) -> LayerStats:
    """Mean throughput, CV, aggregated latency percentiles over *runs*."""
    tps = [
        r["throughput_mbps"]
        for r in runs
        if isinstance(r.get("throughput_mbps"), (int, float))
    ]
    out: LayerStats = {}
    if tps:
        mean_tp = statistics.fmean(tps)
        out["throughput_mbps"] = mean_tp
        out["throughput_mbps_samples"] = tps
        if len(tps) > 1 and mean_tp > 0:
            out["throughput_cv"] = statistics.pstdev(tps) / mean_tp
        else:
            out["throughput_cv"] = 0.0
    # Aggregate latency: take the mean across runs of each percentile.
    percentile_keys = ("p50", "p95", "p99", "mean", "stdev")
    agg_lat: dict[str, float] = {}
    for k in percentile_keys:
        vals = [
            r["latency_ms"][k]  # type: ignore[typeddict-item]
            for r in runs
            if r.get("latency_ms") and k in r["latency_ms"]
        ]
        if vals:
            agg_lat[k] = statistics.fmean(vals)
    if agg_lat:
        out["latency_ms"] = agg_lat
    # L4 encode/decode
    encs = [r["encode_mbps"] for r in runs if "encode_mbps" in r]
    decs = [r["decode_mbps"] for r in runs if "decode_mbps" in r]
    if encs:
        out["encode_mbps"] = statistics.fmean(encs)
    if decs:
        out["decode_mbps"] = statistics.fmean(decs)
    out["errors"] = sum(int(r.get("errors", 0)) for r in runs)
    notes = [r["notes"] for r in runs if r.get("notes")]
    if notes:
        out["notes"] = "; ".join(notes)
    return out


# ── Agent hash (for reproducibility) ─────────────────────────────────────────


def _agent_hash_for(kind: str) -> str:
    try:
        from exectunnel.session._payload import (  # noqa: PLC0415
            load_agent_b64,
            load_go_agent_b64,
        )

        if kind == "python":
            raw = base64.urlsafe_b64decode(load_agent_b64())
        elif kind == "go":
            raw = base64.urlsafe_b64decode(load_go_agent_b64())
        else:
            return ""
    except Exception:  # noqa: BLE001
        return ""
    return "sha256:" + hashlib.sha256(raw).hexdigest()


# ── Orchestrator ─────────────────────────────────────────────────────────────


async def _main(args: argparse.Namespace) -> int:
    run_id = (
        _dt.datetime.now(tz=_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        + "-"
        + uuid.uuid4().hex[:6]
    )

    warnings: list[str] = []
    errors: list[str] = []

    layers: dict[str, LayerStats] = {}

    # L1
    if args.endpoint:
        l1_runs: list[LayerStats] = []
        for i in range(args.repeats):
            print(f"[L1] run {i + 1}/{args.repeats}…", file=sys.stderr)
            l1_runs.append(
                await run_l1_network(
                    args.endpoint, args.duration_sec, args.payload_size
                )
            )
        layers["L1_network"] = _aggregate_repeats(l1_runs)
    else:
        warnings.append("L1 skipped: --endpoint not provided")

    # L2 / L3 — require a full session; out of scope for the MVP without a
    # live TunnelConfig. We document this clearly instead of silently
    # producing zero values.
    if args.endpoint:
        warnings.append(
            "L2/L3 require a bootstrapped TunnelSession + pod_echo helper — "
            "not yet wired into this orchestrator. Use bench_exectunnel.py "
            "for now or extend bench_layered with your TunnelConfig factory."
        )

    # L4 — always runs (CPU-local).
    l4_runs: list[LayerStats] = []
    for i in range(args.repeats):
        print(f"[L4] run {i + 1}/{args.repeats}…", file=sys.stderr)
        l4_runs.append(run_l4_base64_micro(args.payload_size, args.duration_sec))
    layers["L4_base64_micro"] = _aggregate_repeats(l4_runs)

    # Derived
    derived: dict[str, float] = {}
    l3 = layers.get("L3_pod_local", {})
    l2 = layers.get("L2_full_tunnel", {})
    l4 = layers.get("L4_base64_micro", {})
    if isinstance(l3.get("throughput_mbps"), (int, float)) and isinstance(
        l2.get("throughput_mbps"), (int, float)
    ):
        derived["network_overhead_mbps"] = float(l2["throughput_mbps"]) - float(
            l3["throughput_mbps"]
        )

    report: BenchReport = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "label": args.label,
        "agent": {
            "kind": args.agent,
            "version": "1",
            "hash": _agent_hash_for(args.agent),
        },
        "bench_host": {
            "kind": args.bench_host_kind,
            "hostname": socket.gethostname(),
        },
        "provider": {
            "name": "runflare" if args.endpoint and "runflare" in args.endpoint else "",
            "endpoint": args.endpoint or "",
        },
        "workload": {
            "name": "layered-decomposition",
            "params": {
                "payload_size": args.payload_size,
                "duration_sec": args.duration_sec,
                "repeats": args.repeats,
            },
        },
        "layers": layers,
        "derived": derived,
        "errors": errors,
        "warnings": warnings,
    }

    # Write report triple
    out_dir = Path(args.output_dir)
    base = f"bench-report-{run_id}-{args.label}"
    json_path = out_dir / f"{base}.json"
    md_path = out_dir / f"{base}.md"
    csv_path = out_dir / f"{base}.csv"

    write_json(json_path, report)
    write_markdown(md_path, report)
    write_csv(csv_path, report)

    print(f"Wrote {json_path}", file=sys.stderr)
    print(f"Wrote {md_path}", file=sys.stderr)
    print(f"Wrote {csv_path}", file=sys.stderr)
    print(json.dumps({"run_id": run_id, "label": args.label, "path": str(json_path)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", required=True, help="Human-readable run label.")
    ap.add_argument(
        "--endpoint",
        default=None,
        help="WebSocket endpoint (for L1; optional). When unset, L1 is skipped.",
    )
    ap.add_argument(
        "--agent",
        choices=("python", "go"),
        default="python",
        help="Which agent variant is under test (recorded in report metadata).",
    )
    ap.add_argument(
        "--duration-sec", type=float, default=30.0, help="Per-run duration."
    )
    ap.add_argument(
        "--payload-size",
        type=int,
        default=64 * 1024,
        help="Per-message payload size (bytes). Default 64 KiB.",
    )
    ap.add_argument(
        "--bench-host-kind",
        choices=("laptop", "vm"),
        default="laptop",
    )
    ap.add_argument("--output-dir", default="./bench-reports")
    ap.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="How many times to repeat each layer (CV computed across repeats).",
    )
    args = ap.parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
