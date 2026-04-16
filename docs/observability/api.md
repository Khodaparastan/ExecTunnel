# ExecTunnel — Observability Package API Reference

exectunnel/observability/ | api-doc v2.0 | Python 3.13+
audience: developers wiring observability into a session-layer handler, CLI command, or test harness
---

## How to Read This Document

This reference is written for developers who **use** `exectunnel.observability` from the session layer or CLI — not for developers maintaining the observability package itself. It answers:

* What do I import and from where?
* How do I configure logging, start a trace, open a span, and record metrics?
* How do I wire up the metrics reporter?
* What are the exact contracts I must not violate?

Every section is self-contained. Cross-references are explicit. No knowledge of observability internals is assumed.

---

## Table of Contents

1. [Imports](#1-imports)
2. [Logging — `configure_logging()`](#2-logging--configure_logging)
   * 2.1 [Log Ring Buffer — `LogRingBuffer` /
     `install_ring_buffer()`](#21-log-ring-buffer)
3. [Tracing — `start_trace()`, `span()`, `aspan()`](#3-tracing)
   * 3.1 [`start_trace()`](#31-start_trace)
   * 3.2 [`span()`](#32-span)
   * 3.3 [`aspan()`](#33-aspan)
   * 3.4 [Reading Active IDs](#34-reading-active-ids)
   * 3.5 [Complete Tracing Example](#35-complete-tracing-example)
4. [Metrics — Counters, Histograms, Gauges](#4-metrics--counters-histograms-gauges)
   * 4.1 [Counters — `metrics_inc()`](#41-counters--metrics_inc)
   * 4.2 [Histograms — `metrics_observe()`](#42-histograms--metrics_observe)
   * 4.3 [Gauges — `metrics_gauge_*()`](#43-gauges--metrics_gauge_)
   * 4.4 [Snapshot and Reset](#44-snapshot-and-reset)
   * 4.5 [Metric Key Format](#45-metric-key-format)
   * 4.6 [Metric Listeners](#46-metric-listeners)
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
    LogEntry,
    LogRingBuffer,
    install_ring_buffer,

    # tracing
    start_trace,
    span,
    aspan,
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
    register_metric_listener,
    unregister_all_listeners,

   # exporters (advanced use)
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

Configures the `exectunnel` package logger. Safe to call multiple times — each call
replaces any previously installed exectunnel handler, making re-configuration clean
without duplicating output.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `level` | `LevelName` | `"info"` | Minimum log level for all handlers |

**Output format and engine are controlled by environment variables:**

| Variable                                                                         | Default   | Values                                                |
|----------------------------------------------------------------------------------|-----------|-------------------------------------------------------|
| `EXECTUNNEL_LOG_FORMAT`                                                          | `console` | `console` — human-readable with optional ANSI colour\ 
 `json` — machine-parseable JSON lines                                            |
| `EXECTUNNEL_LOG_ENGINE`                                                          | `stdlib`  | `stdlib` — standard Python logging\                   
 `structlog` — delegates to structlog if installed; falls back to stdlib silently |
| `EXECTUNNEL_LOG_COLOR`                                                           | `auto`    | `auto` — colour when stderr is a TTY\                 

`always`/`1`/`true` — force on\
`never`/`0`/`false` — force off |

**What it sets up:**

* A `StreamHandler` writing to `stderr`.
* `_ConsoleFormatter` (default) or `_JsonLogFormatter` (when
  `EXECTUNNEL_LOG_FORMAT=json`).
* A `_TraceContextFilter` that injects `trace_id` and `span_id` into every log record automatically — no caller effort required.
* `propagate = False` on the `exectunnel` logger to prevent duplicate lines from the
  root logger.

**Optional dependencies:**

* `colorama` — if installed, the console formatter renders ANSI colour; absent silently
  degrades.
* `structlog` — if absent and `EXECTUNNEL_LOG_ENGINE=structlog` is set, a warning is
  logged and stdlib is used.

**Usage:**

```python
# At application startup — call before any log calls
configure_logging(level="debug")

# To switch to JSON output, set the env var before calling:
# EXECTUNNEL_LOG_FORMAT=json
configure_logging(level="info")
```

---

### 2.1 Log Ring Buffer

For dashboard or status-page use, a bounded in-memory log buffer is available:

```python
@dataclass(frozen=True, slots=True)
class LogEntry:
    ts: str        # "%H:%M:%S" (UTC)
    level: str     # "INFO", "WARNING", etc.
    logger: str    # "exectunnel." prefix stripped automatically
    message: str

class LogRingBuffer(logging.Handler):
    def __init__(self, maxlen: int = 200, level: int = logging.DEBUG) -> None: ...
    def entries(self) -> list[LogEntry]: ...   # snapshot, oldest first; thread-safe
    def clear(self) -> None: ...               # thread-safe

def install_ring_buffer(
    maxlen: int = 200,
    level: int = logging.DEBUG,
) -> LogRingBuffer: ...
```

`install_ring_buffer()` creates a `LogRingBuffer`, attaches it to the `exectunnel`
logger, and returns it. Call it once at startup and keep the reference to poll
`entries()` at any time.

```python
ring = install_ring_buffer(maxlen=500)

# Later, in a status handler:
recent_logs = ring.entries()  # list[LogEntry], up to 500 entries
```

> `LogRingBuffer` and `configure_logging()` are independent — both can be active
> simultaneously.

---

## 3. Tracing

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

```python
async def _handle_tunnel_connect(...):
    trace_id = start_trace()          # unique per connection
    logger.debug("new connection", extra={"trace_id": trace_id})
    ...
```

> **Never** call `start_trace()` once at module level and share it across
> concurrent handlers — each handler must call it independently.

---

### 3.2 `span()`

```python
@contextmanager
def span(name: str, **tags: object) -> Iterator[str]: ...
```

Opens a child span within the current trace. Use as a **synchronous `with` statement** —
it works perfectly fine inside `async` functions.

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

```python
trace_id = start_trace()
with span("session.run", host=host, port=port) as span_id:
    ...
    with span("transport.connect"):
        ...   # parent_span_id == outer span_id here
```

> `span()` without a preceding `start_trace()` still works but `trace_id`
> will be `None` in log records.

---

### 3.3 `aspan()`

```python
@asynccontextmanager
async def aspan(name: str, **tags: object) -> AsyncIterator[str]: ...
```

Async variant of `span()`. Use when the body of the context must `await` and you prefer
the `async with` style. Identical semantics and auto-emitted metrics to `span()`.

```python
trace_id = start_trace()
async with aspan("session.read_frame", direction="inbound") as span_id:
    data = await socket.recv(4096)
    metrics_observe("session.frame.size_bytes", len(data))
```

> Both `span()` and `aspan()` are always safe to nest with each other — the
> parent/child relationship is tracked via `contextvars` regardless of which
> variant is used.

---

### 3.4 Reading Active IDs

```python
def current_trace_id() -> str | None: ...
def current_span_id() -> str | None: ...
def current_parent_span_id() -> str | None: ...
```

All three read from `contextvars` — safe to call from any coroutine or thread that shares the same context. Return `None` when no trace/span is active.

These IDs are injected automatically into every log record by `_TraceContextFilter`; you
only need to call them explicitly when you want to propagate IDs outward (e.g., into a
request header or a structured log field).

---

### 3.5 Complete Tracing Example

```python
from exectunnel.observability import start_trace, span, aspan, current_trace_id

async def handle_connect(host: str, port: int) -> None:
    trace_id = start_trace()
    with span("tunnel.connect", host=host, port=port) as span_id:
        logger.debug(
            "opening tunnel",
            extra={"host": host, "port": port},
        )
        async with aspan("transport.tcp.connect"):
            await _open_tcp(host, port)
```

---

## 4. Metrics — Counters, Histograms, Gauges

All metric helpers operate on the global `METRICS` singleton (`MetricsRegistry`).
Thread-safe; safe to call from async code or any thread.

### 4.1 Counters — `metrics_inc()`

```python
def metrics_inc(metric: str, value: int = 1, **tags: object) -> None: ...
```

Increments a named counter by `value` (default 1). Tags are arbitrary key-value pairs
that become part of the metric key. Also fires any registered metric listeners (see
§4.6).

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

| Function            | Effect                                              |
|---------------------|-----------------------------------------------------|
| `metrics_gauge_set` | Set gauge to an absolute value                      |
| `metrics_gauge_inc` | Add `delta` to current value (default `1.0`)        |
| `metrics_gauge_dec` | Subtract `delta` from current value (default `1.0`) |

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

`metrics_snapshot()` returns a point-in-time copy of all current metric values, safe to
read without further synchronisation. The reporter calls this automatically at each
interval; you rarely need it directly.

`metrics_reset()` clears all counters, histograms, and gauges **and also removes all
registered metric listeners**. **Only the reporter loop should call this** — never call
it from application code during normal operation.

### 4.5 Metric Key Format

| Metric type | Snapshot key pattern | Value type |
|---|---|---|
| Counter | `"name"` or `"name{tag=val,…}"` | `int` |
| Histogram | `"name.count"`, `"name.sum"`, `"name.avg"`, `"name.min"`, `"name.max"` | `float` |
| Gauge | `"name"` or `"name{tag=val,…}"` | `float` |

Tags are sorted alphabetically and rendered as `{k=v,k=v}` appended to the base name.
Boolean tag values are normalised to lowercase `"true"` / `"false"`.

### 4.6 Metric Listeners

Listeners are lightweight callbacks invoked synchronously inside every `metrics_inc()`
call, after the counter is incremented. Intended for real-time hooks such as dashboard
refresh triggers.

```python
def register_metric_listener(fn: Callable[..., None]) -> None: ...
def unregister_all_listeners() -> None: ...
```

The callback receives `(name: str, **tags: object)` matching the `metrics_inc()`
arguments. Any exception raised by a listener is silently suppressed.

```python
def _on_metric(name: str, **tags: object) -> None:
    if name == "tunnel.connections.total":
        dashboard.refresh()

register_metric_listener(_on_metric)
```

> **Never** register listeners that perform I/O or blocking operations — they
> run synchronously on every `metrics_inc()` call in the hot path.
>
> `metrics_reset()` calls `unregister_all_listeners()` as part of its full
> state wipe. Re-register any listeners you need after a reset.

---

## 5. Exporters — `build_exporters()` and `build_obs_payload()`

> **Standard use**: you do not need to call `build_exporters()` directly.
> `run_metrics_reporter()` builds its own exporters internally from env vars.
> These APIs are public for advanced integrations and custom reporters only.

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

Reads `EXECTUNNEL_OBS_EXPORTERS` from the environment and constructs the exporter list.
If the env var is unset or empty, **defaults to `["log"]`**.

| Parameter | Meaning |
|---|---|
| `logger` | Logger used for the `log` exporter and internal diagnostics |
| `log_emit` | Callback invoked by the `log` exporter: `(snapshot, payload) -> None` |

**Supported backends** (set via `EXECTUNNEL_OBS_EXPORTERS`, comma-separated):

| Backend | Env var required                                                      | Behaviour                                                                                                     |
|---------|-----------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------|
| `log`   | —                                                                     | Calls `log_emit(snapshot, payload)` synchronously                                                             |
| `file`  | `EXECTUNNEL_OBS_FILE_PATH` (default `exectunnel-observability.jsonl`) | Appends JSONL via `asyncio.to_thread`; honours `EXECTUNNEL_OBS_FILE_MAX_BYTES` size guard                     |
| `http`  | `EXECTUNNEL_OBS_HTTP_URL` (required; skipped with WARNING if empty)   | POSTs JSON via `asyncio.to_thread`; retries with exponential back-off up to `EXECTUNNEL_OBS_HTTP_MAX_RETRIES` |

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

Shapes a raw snapshot dict into a platform-specific payload. Called internally by the
reporter; you only need this if building a custom exporter or reporter.

| `platform` value | Output shape                                                        |
|------------------|---------------------------------------------------------------------|
| `"generic"`      | `{timestamp, service, platform, trace_id, span_id, final, metrics}` |
| `"datadog"`      | `{ddsource, service, message, attributes: <generic>}`               |
| `"splunk"`       | `{time, host, source, event: <generic>}`                            |
| `"newrelic"`     | `{common: {attributes}, events: [<generic>]}`                       |

Platform is matched case-insensitively. Unrecognised values fall through to `generic`.

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

Self-contained async loop. Builds its own exporters internally using `build_exporters()`
and reads all configuration from environment variables. Designed to run as a long-lived
`asyncio.Task`.

| Parameter      | Type                    | Default                                                         | Meaning                                          |
|----------------|-------------------------|-----------------------------------------------------------------|--------------------------------------------------|
| `interval_sec` | `float \| None`         | `None` → `EXECTUNNEL_METRICS_INTERVAL_SEC` (30 s, range 1–3600) | Seconds between snapshots                        |
| `stop_event`   | `asyncio.Event \| None` | `None` → private event (run until cancelled)                    | Set to trigger a final flush and clean exit      |
| `logger_name`  | `str`                   | `"exectunnel.metrics"`                                          | Logger used for reporter and summary diagnostics |

**Lifecycle:**

1. Reads `EXECTUNNEL_METRICS_INTERVAL_SEC`, `EXECTUNNEL_METRICS_VERBOSE`,
   `EXECTUNNEL_METRICS_TOP_N`, `EXECTUNNEL_METRICS_LOG_LEVEL_INFO` from env.
2. Calls `build_exporters(logger, log_emit=_emit_log)` internally. Falls back to a
   private log-only exporter if the list is empty.
3. Waits `interval_sec` seconds.
4. Emits `emit_snapshot(final=False)` to all exporters — repeats.
5. When `stop_event` is set or task is cancelled: emits a final
   `emit_snapshot(final=True)` and returns.

**Failure handling:** per-exporter failure counts are tracked. A warning is logged on the 1st failure and every 20th thereafter — the reporter never crashes due to exporter errors.

**Usage:**

```python
import asyncio
from exectunnel.observability import run_metrics_reporter

stop_event = asyncio.Event()

reporter_task = asyncio.create_task(
    run_metrics_reporter(stop_event=stop_event)
)

# ... application runs ...

# Graceful shutdown
stop_event.set()
await reporter_task
```

> **Rule**: Never share a `stop_event` across multiple reporter instances.
>
> **Rule**: Do not pass `exporters` to this function — it has no such parameter and
> builds its own.

---

## 7. Environment Variable Reference

### Logging

| Variable                | Default   | Meaning                                                       |
|-------------------------|-----------|---------------------------------------------------------------|
| `EXECTUNNEL_LOG_FORMAT` | `console` | Output format: `console` or `json`                            |
| `EXECTUNNEL_LOG_ENGINE` | `stdlib`  | Logging backend: `stdlib` or `structlog`                      |
| `EXECTUNNEL_LOG_COLOR`  | `auto`    | ANSI colour: `auto`, `always`/`1`/`true`, `never`/`0`/`false` |

### Exporters

| Variable                          | Default                          | Meaning                                                                                               |
|-----------------------------------|----------------------------------|-------------------------------------------------------------------------------------------------------|
| `EXECTUNNEL_OBS_EXPORTERS`        | `log`                            | Comma-separated list of backends: `log`, `file`, `http`                                               |
| `EXECTUNNEL_OBS_FILE_PATH`        | `exectunnel-observability.jsonl` | Path for the `file` exporter JSONL output                                                             |
| `EXECTUNNEL_OBS_FILE_MAX_BYTES`   | `268435456` (256 MiB)            | Maximum file size (min 1024 bytes); writes are skipped when exceeded                                  |
| `EXECTUNNEL_OBS_HTTP_URL`         | _(empty)_                        | URL for the `http` exporter POST target; required if `http` is listed                                 |
| `EXECTUNNEL_OBS_HTTP_TIMEOUT`     | `5`                              | HTTP request timeout in seconds (min 0.1 s)                                                           |
| `EXECTUNNEL_OBS_HTTP_MAX_RETRIES` | `3`                              | Maximum HTTP retry attempts with exponential back-off (range 0–10)                                    |
| `EXECTUNNEL_OBS_HTTP_HEADERS`     | _(empty)_                        | Extra HTTP headers: semicolon-separated `Key=Value` pairs, e.g. `Authorization=Bearer tok; X-Foo=bar` |
| `EXECTUNNEL_OBS_PLATFORM`         | `generic`                        | Payload shape: `generic`, `datadog`, `splunk`, `newrelic`                                             |
| `EXECTUNNEL_OBS_SERVICE`          | `exectunnel`                     | Service name embedded in every exported payload                                                       |

### Reporter

| Variable                            | Default | Meaning                                                       |
|-------------------------------------|---------|---------------------------------------------------------------|
| `EXECTUNNEL_METRICS_INTERVAL_SEC`   | `30.0`  | Reporter emit interval in seconds (min 1.0, max 3600.0)       |
| `EXECTUNNEL_METRICS_VERBOSE`        | `false` | If `true`, also logs the full snapshot at DEBUG each interval |
| `EXECTUNNEL_METRICS_TOP_N`          | `12`    | Number of metric keys shown in the log summary (min 1)        |
| `EXECTUNNEL_METRICS_LOG_LEVEL_INFO` | `false` | If `true`, emit metric summaries at INFO instead of DEBUG     |

---

## 8. End-to-End Wiring Example

```python
import asyncio
import logging
from exectunnel.observability import (
    configure_logging,
    install_ring_buffer,
    run_metrics_reporter,
    start_trace,
    span,
    aspan,
    metrics_inc,
    metrics_observe,
    metrics_gauge_inc,
    metrics_gauge_dec,
)

# ── 1. Configure logging (CLI startup, once) ─────────────────────────────────
# EXECTUNNEL_LOG_FORMAT=json and EXECTUNNEL_LOG_ENGINE can be set in the env
configure_logging(level="info")
logger = logging.getLogger("myapp")

# Optional: attach ring buffer for dashboard use
ring = install_ring_buffer(maxlen=500)

# ── 2. Start reporter (CLI startup) ──────────────────────────────────────────
# Reporter reads all exporter config from env vars automatically.
# Set EXECTUNNEL_OBS_EXPORTERS, EXECTUNNEL_OBS_HTTP_URL, etc. before starting.
stop_event = asyncio.Event()
reporter_task = asyncio.create_task(
    run_metrics_reporter(stop_event=stop_event)
)

# ── 3. Per-connection handler (session layer) ─────────────────────────────────
async def handle_connect(host: str, port: int) -> None:
    trace_id = start_trace()                          # unique per connection
    metrics_gauge_inc("session.active_connections")
    try:
        with span("tunnel.connect", host=host, port=port):
            metrics_inc("tunnel.connections.total", host=host)
            async with aspan("transport.tcp.connect"):
                t0 = asyncio.get_event_loop().time()
                await _do_connect(host, port)
                metrics_observe(
                    "tunnel.connect.duration_sec",
                    asyncio.get_event_loop().time() - t0,
                )
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
   (Re-calling it reconfigures cleanly — it won't duplicate output.)

□  EXECTUNNEL_LOG_FORMAT and EXECTUNNEL_LOG_ENGINE set appropriately for the
   deployment environment (e.g. json for log aggregators).

□  start_trace() called at the top of every connection handler coroutine.

□  span() or aspan() used as context managers (with / async with).

□  run_metrics_reporter() started as an asyncio.Task with a dedicated stop_event.
   The reporter builds its own exporters — do NOT pass exporters to it.

□  stop_event.set() + await reporter_task called on graceful shutdown.

□  EXECTUNNEL_OBS_HTTP_URL set before listing 'http' in EXECTUNNEL_OBS_EXPORTERS.

□  metrics_reset() never called from application code (reporter owns it).
   Remember: metrics_reset() also unregisters all metric listeners.

□  Exporter.failures never mutated directly.

□  exporter.emit() never called directly (use the reporter).

□  Metric listeners registered via register_metric_listener() contain no I/O
   or blocking calls.

□  observability never imported from proxy or transport layers.
```

