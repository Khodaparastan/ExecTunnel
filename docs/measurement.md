# Measurement framework

ExecTunnel's measurement framework decomposes end-to-end performance into
four layers so you can attribute cost to the right subsystem before
investing in any large rewrite or optimization.

## The four layers

| Layer                    | Measures                                                              | Requires                         |
|--------------------------|-----------------------------------------------------------------------|----------------------------------|
| **L1** `L1_network`      | Raw WebSocket echo to the provider edge. Pure network baseline.       | `--endpoint wss://…`             |
| **L2** `L2_full_tunnel`  | Full client → runflare → pod → external target stack.                 | Live `TunnelSession` + target    |
| **L3** `L3_pod_local`    | Full tunnel, target = `127.0.0.1:<port>` inside the pod via pod_echo. | Live session + `pod_echo` helper |
| **L4** `L4_base64_micro` | In-process base64 encode/decode throughput. CPU-local only.           | Nothing                          |

Derived ratios computed automatically when the relevant layers are present:

- **`L2 − L3`** → time spent in the external network path (agent cannot fix).
- **`L3 − L4`** → time spent in the agent's interpreter + threading
  (a Go/Rust rewrite primarily attacks this).
- **`L3 / L4`** → how close the agent gets to the theoretical CPU ceiling.

## Running a baseline

```bash
make bench-baseline BENCH_LABEL=baseline-py BENCH_DURATION_SEC=30
```

Or directly:

```bash
python bench_layered.py --label baseline-py --bench-host-kind laptop \
    --duration-sec 30 --repeats 3
```

Optional `--endpoint wss://stream.runflare.com/…` enables the L1
measurement. L4 always runs; L2 / L3 require a session factory (not
wired in the MVP — use `bench_exectunnel.py` for those today).

Output lands in `./bench-reports/` as three files per run:

- `bench-report-<ts>-<label>.json` — canonical, versioned, diffable.
- `bench-report-<ts>-<label>.md`   — one-page human summary.
- `bench-report-<ts>-<label>.csv`  — long-format, spreadsheet-ready.

## Comparing runs

```bash
make bench-compare A=bench-reports/…-py.json B=bench-reports/…-go.json
```

Or:

```bash
python bench_compare.py a.json b.json [c.json ...] \
    --throughput-threshold-pct 5 --latency-threshold-ms 10
```

`bench_compare` refuses to diff reports with mismatched `schema_version`
and flags (⚠) metrics whose delta exceeds the configured thresholds.

## Agent telemetry

The Python agent emits a `STATS` control frame roughly once per second
(disable with `EXECTUNNEL_AGENT_STATS_ENABLED=0`). Each frame carries:

| Field              | Meaning                                                           |
|--------------------|-------------------------------------------------------------------|
| `ts`               | Snapshot timestamp (Unix seconds, float).                         |
| `tx_bytes_total`   | Monotonic bytes written to stdout (wire-level including b64).     |
| `rx_bytes_total`   | Monotonic bytes read from stdin.                                  |
| `frames_tx_total`  | Count of DATA/CONN_ACK/UDP_DATA/… frames emitted.                 |
| `frames_rx_total`  | Count of frames parsed from stdin.                                |
| `data_queue_depth` | Current backlog in the bounded stdout data queue.                 |
| `data_queue_cap`   | Queue capacity (default 2048). Depth ≥ 75% triggers backpressure. |
| `tcp_worker_count` | Live TCP connection worker threads.                               |
| `udp_worker_count` | Live UDP flow worker threads.                                     |
| `dispatch_ms_p50`  | P50 of recent dispatch latencies (last ~1024 samples).            |
| `dispatch_ms_p95`  | P95 of recent dispatch latencies.                                 |
| `dispatch_ms_p99`  | P99 of recent dispatch latencies.                                 |
| `dispatch_samples` | Size of the sample ring at snapshot time.                         |

On the client, register a listener via
`TunnelSession.set_agent_stats_listener(cb)` before calling
`await session.run()`. The listener is invoked on the asyncio thread
with a decoded `dict[str, Any]`; listeners must not block.

Sessions that never register a listener still pay a tiny, constant cost
per STATS frame (one `metrics_inc`) — safe to leave on in production.

## Interpreting the CV flag

Each layer runs `--repeats` times (default 3). The Markdown summary
shows the coefficient of variation (CV = stdev / mean); the run is
flagged **unstable** when CV > 15% for any layer. Common causes:

- Noisy home/office network → use `--bench-host-kind vm` on a fixed EC2/GCE host.
- Pod CPU throttling → check the pod's CPU limits and load.
- Provider-side variance → runflare itself can jitter ±15% on a cold run.

When a layer is flagged, re-run with `--repeats 5` (or higher) and
discard outliers manually by inspecting `throughput_mbps_samples`.

## Known variance sources

- **Runflare jitter**: single-tunnel throughput fluctuates hour-to-hour.
  Run A/B comparisons back-to-back rather than across days.
- **Laptop network**: any home/office NAT, Wi-Fi congestion, VPN, or
  captive portal will dominate L1/L2 numbers. For authoritative runs,
  use a VM in the same region as the provider.
- **Python base64 is already fast**: the L4 micro-benchmark typically
  shows 5–10 Gbps on modern CPUs. This is why the Markdown summary
  explicitly warns **not** to motivate a Go rewrite from L4 alone.

## Extending the framework (future work)

The MVP ships L1 + L4 wired end-to-end. L2 and L3 are scoped into the
schema and `bench_layered.py` but their orchestration is left for a
follow-up once the measurement shape proves its value. When adding:

- **L2**: reuse `Socks5Server` + aiohttp on the client side; pick a
  stable public mirror (pypi.org release files work well) as target.
- **L3**: bootstrap `pod_echo` inside the pod (via `load_pod_echo_b64`
  from the session bootstrap) and point the client at
  `127.0.0.1:17001` (echo) or `:17002` (source) / `:17003` (sink).
- Register the session's `set_agent_stats_listener` before
  `session.run()` to collect STATS snapshots; aggregate in
  `_aggregate_repeats` and fill `report["agent_telemetry"]`.
