# ExecTunnel — Observability Package API Reference
```
exectunnel/observability/  |  api-doc v1.0  |  Python 3.13+
audience: developers wiring observability into a session-layer handler, CLI command, or test harness
```
---
## How to Read This Document
This reference is written for developers who **use** `exectunnel.observability` from the session layer or CLI — not for developers maintaining the observability package itself. It answers:
* What do I import and from where?
* How do I configure logging, start a trace, open a span, and record metrics?
* How do I wire up the metrics reporter and exporters?
* What are the exact contracts I must not violate?

Every section is self-contained. Cross-references are explicit. No knowledge of observability internals is assumed.

---
## Table of Contents
1. [Imports](#1-imports)
2. [Logging — `configure_logging()`](#2-logging--configure_logging)
3. [Tracing — `start_trace()` and `span()`](#3-tracing--start_trace-and-span)
   * 3.1 [`start_trace()`](#31-start_trace)
   * 3.2 [`span()`](#32-span)
   * 3.3 [Reading Active IDs](#33-reading-active-ids)
   * 3.4 [Complete Tracing Example](#34-complete-tracing-example)
4. [Metrics — Counters, Histograms, Gauges](#4-metrics--counters-histograms-gauges)
   * 4.1 [Counters — `metrics_inc()`](#41-counters--metrics_inc)
   * 4.2 [Histograms — `metrics_observe()`](#42-histograms--metrics_observe)
   * 4.3 [Gauges — `metrics_gauge_*()`](#43-gauges--metrics_gauge_)
   * 4.4 [Snapshot and Reset](#44-snapshot-and-reset)
   * 4.5 [Metric Key Format](#45-metric-key-format)
5. [Exporters — `build_exporters()` and `build_obs_payload()`](#5-exporters--build_exporters-and-build_obs_payload)
   * 5.1 [`Exporter` Dataclass](#51-exporter-dataclass)
   * 5.2 [`build_exporters()`](#52-build_exporters)
   * 5.3 [`build_obs_payload()`](#53-build_obs_payload)
6. [Reporter — `run_metrics_reporter()`](#6-reporter--run_metrics_reporter)
7. [Environment Variable Reference](#7-environment-variable-reference)
8. [End-to-End Wiring Example](#8-end-to-end-wiring-example)
9. [Checklist — Before You Ship](#9-checklist--before-you-ship)

---
## 1. Imports

```python
# ── Always import from the package root ──────────────────────────────────────
from exectunnel.observability import (
    # logging
    configure_logging,
    LevelName,

    # tracing
    start_trace,
    span,
    current_trace_id,
    current_span_id,
    current_parent_span_id,

    # metrics
    METRICS,
    MetricsRegistry,
    metrics_inc,
    metrics_observe,
    metrics_gauge_set,
    metrics_gauge_inc,
    metrics_gauge_dec,
    metrics_snapshot,
    metrics_reset,

    # exporters
    Exporter,
    build_exporters,
    build_obs_payload,

    # reporter
    run_metrics_reporter,
)
```

> **Rule**: Never import from sub-modules (`exectunnel.observability.metrics`,
> `exectunnel.observability.tracing`, etc.). The package root is the only stable
> surface.

> **Layer rule**: Only the session layer and above may import
> `exectunnel.observability`. The proxy and transport layers must not.

---
## 2. Logging — `configure_logging()`

```python
def configure_logging(level: LevelName = "info") -> None: ...

LevelName = Literal["debug", "info", "warning", "error"]
```

Configures the root logger once. Subsequent calls are **idempotent** — if handlers are already attached the call is a no-op.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `level` | `LevelName` | `"info"` | Minimum log level for all handlers |

**What it sets up:**
* A `StreamHandler` writing to `stderr`.
* A `_JsonLogFormatter` emitting single-line JSON with UTC timestamps.
* A `_TraceContextFilter` that injects `trace_id` and `span_id` into every log record automatically — no caller effort required.

**Optional dependencies:**
* `colorama` — if installed, the console formatter renders ANSI colour codes; absent silently.
* `structlog` — not exposed via this function; structlog integration is internal.

**Usage:**
```python
# At application startup — call exactly once before any logging
configure_logging(level="debug")
```

---
## 3. Tracing — `start_trace()` and `span()`

### 3.1 `start_trace()`

```python
def start_trace(trace_id: str | None = None) -> str: ...
```

**Plain function — not a context manager.** Call it directly; do not use `with`.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `trace_id` | `str \| None` | `None` | Supply an existing ID for inbound propagation; `None` generates a new 128-bit hex ID |

**Returns:** the active `trace_id` (`str`).

**What it does:**
* Sets `trace_id` in the current `contextvars` context.
* Clears `span_id` and `parent_span_id`.
* Because each `asyncio.Task` owns its own context copy, calling `start_trace()` at the top of a handler coroutine gives every concurrent connection a unique, isolated trace ID.

**Usage:**
```python
async def _handle_tunnel_connect(...):
    trace_id = start_trace()          # unique per connection
    logger.debug("new connection", extra={"trace_id": trace_id})
    ...
```

> **Never** call `start_trace()` once at module level and share it across
> concurrent handlers — each handler must call it independently.

### 3.2 `span()`

```python
@contextmanager
def span(name: str, **tags: object) -> Iterator[str]: ...
```

Opens a child span within the current trace. Must be used as a **synchronous context manager** (`async with` is not required — `span` is sync).

| Parameter | Type | Meaning |
|---|---|---|
| `name` | `str` | Span name, used as a tag on all auto-emitted metrics |
| `**tags` | `object` | Arbitrary key-value tags attached to the duration histogram |

**Yields:** the new `span_id` (`str`, 64-bit hex).

**Auto-emitted metrics:**

| Metric | Type | Tags |
|---|---|---|
| `trace.spans.started` | Counter | `name`, `parent` (bool) |
| `trace.spans.ok` | Counter | `name` |
| `trace.spans.error` | Counter | `name` |
| `trace.spans.duration_sec` | Histogram | `name`, caller `**tags` |

**Usage:**
```python
trace_id = start_trace()
with span("session.run", host=host, port=port) as span_id:
    ...
    with span("transport.connect"):
        ...   # parent_span_id == outer span_id here
```

> `span()` without a preceding `start_trace()` still works but `trace_id`
> will be `None` in log records.

### 3.3 Reading Active IDs

```python
def current_trace_id() -> str | None: ...
def current_span_id() -> str | None: ...
def current_parent_span_id() -> str | None: ...
```

All three read from `contextvars` — safe to call from any coroutine or thread that shares the same context. Return `None` when no trace/span is active.

These are injected automatically into every log record by `_TraceContextFilter`; you only need to call them explicitly when you want to propagate IDs outward (e.g., into a request header or a structured log field).

### 3.4 Complete Tracing Example

```python
from exectunnel.observability import start_trace, span, current_trace_id

async def handle_connect(host: str, port: int) -> None:
    trace_id = start_trace()
    with span("tunnel.connect", host=host, port=port) as span_id:
        logger.debug(
            "opening tunnel",
            extra={"host": host, "port": port, "trace_id": trace_id, "span_id": span_id},
        )
        with span("transport.tcp.connect"):
            await _open_tcp(host, port)
```

---
## 4. Metrics — Counters, Histograms, Gauges

All metric helpers operate on the global `METRICS` singleton (`MetricsRegistry`). Thread-safe; safe to call from async code.

### 4.1 Counters — `metrics_inc()`

```python
def metrics_inc(metric: str, value: int = 1, **tags: object) -> None: ...
```

Increments a named counter by `value` (default 1). Tags are arbitrary key-value pairs that become part of the metric key.

```python
metrics_inc("tunnel.connections.total", host=host)
metrics_inc("tunnel.bytes.sent", value=n_bytes, direction="up")
```

### 4.2 Histograms — `metrics_observe()`

```python
def metrics_observe(metric: str, value: float, **tags: object) -> None: ...
```

Records a single observation into a histogram. The snapshot exposes `.count`, `.sum`, `.avg`, `.min`, `.max`.

```python
metrics_observe("tunnel.connect.duration_sec", elapsed, host=host)
metrics_observe("session.frame.size_bytes", len(frame))
```

### 4.3 Gauges — `metrics_gauge_*()`

```python
def metrics_gauge_set(metric: str, value: float, **tags: object) -> None: ...
def metrics_gauge_inc(metric: str, delta: float = 1.0, **tags: object) -> None: ...
def metrics_gauge_dec(metric: str, delta: float = 1.0, **tags: object) -> None: ...
```

Gauges track an arbitrary floating-point level (e.g., active connection count).

| Function | Effect |
|---|---|
| `metrics_gauge_set` | Set gauge to an absolute value |
| `metrics_gauge_inc` | Add `delta` to current value |
| `metrics_gauge_dec` | Subtract `delta` from current value |

```python
metrics_gauge_inc("session.active_connections")
try:
    await handle(conn)
finally:
    metrics_gauge_dec("session.active_connections")
```

### 4.4 Snapshot and Reset

```python
def metrics_snapshot() -> dict[str, object]: ...
def metrics_reset() -> None: ...
```

`metrics_snapshot()` returns a shallow copy of all current metric values. The reporter calls this automatically; you rarely need it directly.

`metrics_reset()` clears all metrics. **Only the reporter loop should call this** — never call it from application code during normal operation.

### 4.5 Metric Key Format

Metric keys in the snapshot follow this pattern:

| Metric type | Snapshot key pattern | Value type |
|---|---|---|
| Counter | `"name"` or `"name{tag=val,…}"` | `int` |
| Histogram | `"name.count"`, `"name.sum"`, `"name.avg"`, `"name.min"`, `"name.max"` | `float` |
| Gauge | `"name"` or `"name{tag=val,…}"` | `float` |

Tags are sorted alphabetically and rendered as `{k=v,k=v}` appended to the base name.

---
## 5. Exporters — `build_exporters()` and `build_obs_payload()`

### 5.1 `Exporter` Dataclass

```python
@dataclass
class Exporter:
    name: str
    emit: Callable[[dict[str, object]], Awaitable[None]]
    failures: int = 0
```

| Field | Meaning |
|---|---|
| `name` | Human-readable backend name (`"log"`, `"file"`, `"http"`) |
| `emit` | Async callable that receives a shaped payload dict |
| `failures` | Failure counter owned by the reporter — **do not mutate directly** |

### 5.2 `build_exporters()`

```python
def build_exporters(
    logger: logging.Logger,
    *,
    log_emit: Callable[[dict[str, object], dict[str, object]], object],
) -> list[Exporter]: ...
```

Reads `EXECTUNNEL_OBS_EXPORTERS` from the environment and constructs the exporter list. Returns an empty list if the env var is unset or empty.

| Parameter | Meaning |
|---|---|
| `logger` | Logger used for the `log` exporter and internal diagnostics |
| `log_emit` | Callback invoked by the `log` exporter: `(snapshot, payload) -> None` |

**Supported backends** (set via `EXECTUNNEL_OBS_EXPORTERS`, comma-separated):

| Backend | Env var required | Behaviour |
|---|---|---|
| `log` | — | Calls `log_emit(snapshot, payload)` synchronously |
| `file` | `EXECTUNNEL_OBS_FILE_PATH` | Appends JSONL via `asyncio.to_thread`; honours `EXECTUNNEL_OBS_FILE_MAX_BYTES` size guard |
| `http` | `EXECTUNNEL_OBS_HTTP_URL` | POSTs JSON via `asyncio.to_thread`; retries with exponential back-off up to `EXECTUNNEL_OBS_HTTP_MAX_RETRIES` |

```python
import logging
from exectunnel.observability import build_exporters

logger = logging.getLogger("exectunnel.metrics")

def my_log_emit(snapshot: dict, payload: dict) -> None:
    logger.info("metrics snapshot", extra={"payload": payload})

exporters = build_exporters(logger, log_emit=my_log_emit)
```

### 5.3 `build_obs_payload()`

```python
def build_obs_payload(
    snapshot: dict[str, object],
    *,
    final: bool,
    platform: str,
    service: str,
) -> dict[str, object]: ...
```

Shapes a raw snapshot dict into a platform-specific payload. Called internally by each exporter; you only need this if building a custom exporter.

| `platform` value | Output shape |
|---|---|
| `"generic"` | `{ts, service, final, metrics}` |
| `"datadog"` | Datadog series format |
| `"splunk"` | Splunk HEC event format |
| `"newrelic"` | New Relic metric API format |

---
## 6. Reporter — `run_metrics_reporter()`

```python
async def run_metrics_reporter(
    interval_sec: float | None = None,
    stop_event: asyncio.Event | None = None,
    *,
    logger_name: str = "exectunnel.metrics",
) -> None: ...
```

Async loop that emits a metrics snapshot to all configured exporters every `interval_sec` seconds. Designed to run as a long-lived `asyncio.Task`.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `interval_sec` | `float \| None` | `None` → env `EXECTUNNEL_METRICS_INTERVAL_SEC` (default 30 s) | Seconds between snapshots |
| `stop_event` | `asyncio.Event \| None` | `None` → private event (run until cancelled) | Set to trigger a final flush and clean exit |
| `logger_name` | `str` | `"exectunnel.metrics"` | Logger used for reporter diagnostics |

**Lifecycle:**
1. Waits `interval_sec` seconds.
2. Calls `emit_snapshot(final=False)` — emits to all exporters, resets `METRICS`.
3. Repeats until `stop_event` is set or the task is cancelled.
4. On exit: emits a final snapshot with `final=True`.

**Failure handling:** per-exporter failure counts are tracked. A warning is logged on the 1st failure and every 20th thereafter — the reporter never crashes due to exporter errors.

**Usage:**
```python
import asyncio
from exectunnel.observability import build_exporters, run_metrics_reporter

stop_event = asyncio.Event()
exporters = build_exporters(logger, log_emit=my_log_emit)

reporter_task = asyncio.create_task(
    run_metrics_reporter(exporters, stop_event=stop_event)
)

# ... application runs ...

# Graceful shutdown
stop_event.set()
await reporter_task
```

> **Rule**: Never share a `stop_event` across multiple reporter instances.

---
## 7. Environment Variable Reference

| Variable | Default | Meaning |
|---|---|---|
| `EXECTUNNEL_OBS_EXPORTERS` | `""` (none) | Comma-separated list of backends: `log`, `file`, `http` |
| `EXECTUNNEL_OBS_FILE_PATH` | `""` | Path for the `file` exporter JSONL output |
| `EXECTUNNEL_OBS_FILE_MAX_BYTES` | `268435456` (256 MiB) | Maximum file size; writes are skipped when exceeded |
| `EXECTUNNEL_OBS_HTTP_URL` | `""` | URL for the `http` exporter POST target |
| `EXECTUNNEL_OBS_HTTP_MAX_RETRIES` | `3` | Maximum HTTP retry attempts with exponential back-off |
| `EXECTUNNEL_OBS_PLATFORM` | `"generic"` | Payload shape: `generic`, `datadog`, `splunk`, `newrelic` |
| `EXECTUNNEL_OBS_SERVICE` | `"exectunnel"` | Service name embedded in every exported payload |
| `EXECTUNNEL_METRICS_INTERVAL_SEC` | `30.0` | Reporter emit interval in seconds (min 1, max 3600) |

---
## 8. End-to-End Wiring Example

```python
import asyncio
import logging
from exectunnel.observability import (
    configure_logging,
    build_exporters,
    run_metrics_reporter,
    start_trace,
    span,
    metrics_inc,
    metrics_observe,
    metrics_gauge_inc,
    metrics_gauge_dec,
)

# ── 1. Configure logging (CLI startup, once) ──────────────────────────────────
configure_logging(level="info")
logger = logging.getLogger("myapp")

# ── 2. Build exporters and start reporter (CLI startup) ───────────────────────
def _log_emit(snapshot: dict, payload: dict) -> None:
    logger.info("metrics", extra={"payload": payload})

exporters = build_exporters(logger, log_emit=_log_emit)
stop_event = asyncio.Event()
reporter_task = asyncio.create_task(
    run_metrics_reporter(exporters, stop_event=stop_event)
)

# ── 3. Per-connection handler (session layer) ─────────────────────────────────
async def handle_connect(host: str, port: int) -> None:
    trace_id = start_trace()                          # unique per connection
    metrics_gauge_inc("session.active_connections")
    try:
        with span("tunnel.connect", host=host, port=port):
            metrics_inc("tunnel.connections.total", host=host)
            t0 = asyncio.get_event_loop().time()
            await _do_connect(host, port)
            metrics_observe("tunnel.connect.duration_sec",
                            asyncio.get_event_loop().time() - t0)
    finally:
        metrics_gauge_dec("session.active_connections")

# ── 4. Graceful shutdown ──────────────────────────────────────────────────────
stop_event.set()
await reporter_task
```

---
## 9. Checklist — Before You Ship

```
□  configure_logging() called exactly once at startup, before any log calls.
□  start_trace() called at the top of every connection handler coroutine.
□  span() used as a synchronous context manager (with span(...) as sid:).
□  build_exporters() called with log_emit as a keyword argument.
□  run_metrics_reporter() running as an asyncio.Task with a dedicated stop_event.
□  stop_event.set() + await reporter_task called on graceful shutdown.
□  metrics_reset() never called from application code (reporter owns it).
□  Exporter.failures never mutated directly.
□  exporter.emit() never called directly (use the reporter).
□  observability never imported from proxy or transport layers.
□  EXECTUNNEL_OBS_HTTP_URL set before listing 'http' in EXECTUNNEL_OBS_EXPORTERS.
```
