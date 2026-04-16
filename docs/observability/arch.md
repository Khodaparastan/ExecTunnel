# ExecTunnel — Observability Package Architecture Document

exectunnel/observability/ | arch-doc v2.0 | Python 3.13+

---

## 1. Purpose & Scope

The `exectunnel.observability` package is the **cross-cutting telemetry layer** of the ExecTunnel stack. It provides structured logging, in-process metrics (counters, histograms, gauges), `contextvars`-based distributed tracing, and pluggable metric export — all without importing any other ExecTunnel package.

This document covers:

* What the observability layer is and is not responsible for
* How it fits into the full tunnel stack
* Internal package structure and module responsibilities
* All public types and their design in full detail
* Metric types, snapshot format, and thread-safety model
* Tracing context propagation and span lifecycle
* Exporter pipeline: log, file, and HTTP backends
* Metrics reporter loop and failure-throttling strategy
* Logging configuration: JSON and console formatters, trace injection, ring buffer
* Environment variable reference
* Failure modes and invariants every caller must preserve

---

## 2. Position in the Stack

```
┌─────────────────────────────────────────────────────────────────────┐
│  CLI / application layer                                            │
│  ─────────────────────────────────────────────────────────────────  │
│  • Calls configure_logging() at startup                             │
│  • Calls run_metrics_reporter() (reporter builds its own exporters) │
│  • Wraps top-level coroutines with start_trace()                    │
└───────────────────────────┬─────────────────────────────────────────┘
                            │  imports
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  session layer                                                      │
│  ─────────────────────────────────────────────────────────────────  │
│  • Calls metrics_inc / metrics_observe / metrics_gauge_*            │
│  • Wraps operations with span() or aspan()                          │
│  • Uses structured logger (name = "exectunnel.session.*")           │
└───────────────────────────┬─────────────────────────────────────────┘
                            │  imports
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  observability layer  ◄── THIS DOCUMENT                             │
│  ─────────────────────────────────────────────────────────────────  │
│  • configure_logging()    — stdlib / structlog setup                │
│  • LogRingBuffer          — in-memory log capture for dashboards    │
│  • MetricsRegistry        — thread-safe counters, histograms,gauges │
│  • span() / aspan()       — sync and async span context managers    │
│  • start_trace()          — contextvars trace propagation           │
│  • build_exporters()      — log / file / HTTP exporter pipeline     │
│  • run_metrics_reporter() — self-contained periodic snapshot loop   │
│  • utils.py               — self-contained env-parsing helpers      │
└─────────────────────────────────────────────────────────────────────┘
                            │  imports (stdlib only)
                            ▼
                     Python standard library
```

### Layer Contract Table

| Boundary                    | Direction     | Mechanism                                                           | Owner                                        |
|-----------------------------|---------------|---------------------------------------------------------------------|----------------------------------------------|
| `CLI` → `observability`     | Downward call | `configure_logging()`, `run_metrics_reporter()`                     | CLI calls, observability implements          |
| `session` → `observability` | Downward call | `metrics_inc/observe/gauge_*`, `span()`, `aspan()`, `start_trace()` | session calls, observability implements      |
| `observability` → stdlib    | Downward call | `os.environ`, `threading`, `contextvars`, `logging`, `asyncio`      | stdlib only — no ExecTunnel cross-layer deps |
| `observability` → external  | Async I/O     | file append, HTTP POST (via `asyncio.to_thread`)                    | exporters own I/O                            |

### What the Observability Layer Knows

* Logging configuration — stdlib and structlog setup, JSON and console formatters
* Trace and span ID generation and `contextvars` propagation
* In-process metric accumulation — counters, histograms, gauges, metric listeners
* Snapshot serialisation — flat `dict[str, object]` with tag rendering
* Exporter construction and dispatch — log, file, HTTP
* Platform-specific payload shaping — generic, Datadog, Splunk, New Relic
* Metrics reporter loop — self-contained exporter build, periodic emit, final flush,
  failure throttling
* Log ring buffer — bounded in-memory capture for dashboard/diagnostics use

### What the Observability Layer Does Not Know

| Concern | Owned By |
|---|---|
| Frame wire protocol | `protocol` |
| TCP / UDP I/O | `transport` |
| SOCKS5 negotiation | `proxy` |
| Session reconnection logic | `session` |
| Kubernetes API / TLS | above session |
| Agent-side telemetry | agent binary |
| Log rotation / file archival | external tooling (logrotate, etc.) |

---

## 3. Package Structure

```
exectunnel/observability/
├── __init__.py      public re-export surface (26 symbols)
├── logging.py       configure_logging(), LogRingBuffer, LogEntry, install_ring_buffer
├── metrics.py       MetricsRegistry, _Histogram, _Gauge, listeners, module-level helpers
├── tracing.py       start_trace(), span(), aspan(), current_trace/span/parent_span_id()
├── exporters.py     Exporter, build_exporters(), build_obs_payload()
├── reporter.py      run_metrics_reporter() — self-contained loop
└── utils.py         self-contained parse_bool/float/int_env (stdlib only)
```

### Module Dependency Graph

```
__init__.py
  ├── logging.py      (uses tracing — current_trace_id, current_span_id)
  ├── metrics.py      (stdlib only — threading, dataclasses, collections)
  ├── tracing.py      (uses metrics — metrics_inc, metrics_observe)
  ├── exporters.py    (uses tracing — current_trace/span_id; uses utils)
  ├── reporter.py     (uses metrics, exporters, utils)
  └── utils.py        (stdlib only — os.environ; no ExecTunnel imports)
```

### Import Rules

```
observability  →  stdlib only         (os, threading, contextvars, logging, asyncio)
observability  ↛  session             FORBIDDEN (no upward imports)
observability  ↛  transport           FORBIDDEN
observability  ↛  proxy               FORBIDDEN
observability  ↛  protocol            FORBIDDEN
```

---

## 4. `MetricsRegistry` — Design & Architecture

### 4.1 Responsibility

`MetricsRegistry` is the single in-process store for all runtime metrics. It:

1. Accumulates counters (monotonically increasing integers)
2. Accumulates histograms (count, sum, min, max, avg per named series)
3. Tracks gauges (arbitrary float, set/inc/dec)
4. Produces a flat snapshot dict for export
5. Resets all state on demand

### 4.2 Public Interface

```python
class MetricsRegistry:
    def inc(self, name: str, value: int = 1, tags: dict[str, object] | None = None) -> None: ...
    def observe(self, name: str, value: float, tags: dict[str, object] | None = None) -> None: ...
    def gauge_set(self, name: str, value: float, tags: dict[str, object] | None = None) -> None: ...
    def gauge_inc(self, name: str, delta: float = 1.0, tags: dict[str, object] | None = None) -> None: ...
    def gauge_dec(self, name: str, delta: float = 1.0, tags: dict[str, object] | None = None) -> None: ...
    def snapshot(self) -> dict[str, object]: ...
    def reset(self) -> None: ...
```

Module-level helpers delegate to the global `METRICS` singleton:

```python
def metrics_inc(metric: str, value: int = 1, **tags: object) -> None: ...
def metrics_observe(metric: str, value: float, **tags: object) -> None: ...
def metrics_gauge_set(metric: str, value: float, **tags: object) -> None: ...
def metrics_gauge_inc(metric: str, delta: float = 1.0, **tags: object) -> None: ...
def metrics_gauge_dec(metric: str, delta: float = 1.0, **tags: object) -> None: ...
def metrics_snapshot() -> dict[str, object]: ...
def metrics_reset() -> None: ...   # also calls unregister_all_listeners()
```

> **Note**: `metrics_reset()` calls both `METRICS.reset()` **and**
> `unregister_all_listeners()`. It is a full state wipe, including callbacks.

### 4.3 Metric Listeners

Listeners are lightweight callbacks invoked synchronously inside every `metrics_inc()`
call, immediately after the counter is incremented. They are designed for real-time
event hooks (e.g., dashboard refresh triggers) and must never perform I/O or block.

```python
def register_metric_listener(fn: Callable[..., None]) -> None: ...
def unregister_all_listeners() -> None: ...
```

The callback signature is `fn(name: str, **tags: object)` matching the `metrics_inc()`
arguments. Any exception raised by a listener is silently suppressed to protect the
caller.

### 4.4 Metric Key Format

Tags are normalised to a sorted tuple of `(str, str)` pairs and rendered into the snapshot key:

```
no tags:    "session_frames_inbound_total"
with tags:  "session_frames_inbound_total{msg_type=DATA,reason=ok}"
```

Histogram series expand to five sub-keys:

```
"span_duration_sec.count"
"span_duration_sec.sum"
"span_duration_sec.avg"
"span_duration_sec.min"
"span_duration_sec.max"
```

### 4.5 Thread Safety

All mutations and the snapshot operation are protected by a single `threading.Lock`. The lock is held only for the duration of the dict operation — no I/O or computation is performed under the lock.

---

## 5. Tracing — Context Propagation Model

### 5.1 ContextVars

Three `contextvars.ContextVar` instances carry trace context across async tasks:

| ContextVar | Default | Purpose |
|---|---|---|
| `exectunnel_trace_id` | `None` | Current trace identifier (128-bit hex) |
| `exectunnel_span_id` | `None` | Current span identifier (64-bit hex) |
| `exectunnel_parent_span_id` | `None` | Parent span identifier for nested spans |

`contextvars` are asyncio-safe: each `asyncio.Task` inherits a copy of the context from its creator, so spans opened in one task do not bleed into sibling tasks.

### 5.2 `start_trace()`

```python
def start_trace(trace_id: str | None = None) -> str: ...
```

* Plain function — **not** a context manager; call it directly, no `with` required.
* Generates a new 128-bit hex trace ID if none is supplied (for inbound propagation).
* Sets `trace_id`, clears `span_id` and `parent_span_id` in the current `contextvars` context.
* Returns the new `trace_id` so callers can log or propagate it.
* Because each `asyncio.Task` owns its own context copy, calling `start_trace()` at the top of a handler coroutine gives every concurrent connection a unique trace ID.

### 5.3 `span()` and `aspan()`

```python
@contextmanager
def span(name: str, **tags: object) -> Iterator[str]: ...

@asynccontextmanager
async def aspan(name: str, **tags: object) -> AsyncIterator[str]: ...
```

Both are structurally identical — they share the same enter/exit logic. Use `span()` in
synchronous code or inside `async` functions where you don't need an async `with`. Use
`aspan()` when the body requires `await` and you prefer the async context manager style.

Lifecycle per call (both variants):

```
1. Save current span_id as new parent_span_id
2. Generate new 64-bit hex span_id
3. Record trace.spans.started counter (name=, parent=bool)
4. yield / async yield span_id to caller
5. On clean exit  → record trace.spans.ok counter
   On exception   → record trace.spans.error counter; re-raise
6. Record trace.spans.duration_sec histogram (name=, **tags)
7. Restore span_id and parent_span_id via ContextVar.reset()
```

### 5.4 Span Metrics Emitted Automatically

| Metric | Type | Tags | Meaning |
|---|---|---|---|
| `trace.spans.started` | Counter | `name`, `parent` | Span opened |
| `trace.spans.ok` | Counter | `name` | Span completed without exception |
| `trace.spans.error` | Counter | `name` | Span raised an exception |
| `trace.spans.duration_sec` | Histogram | `name`, caller tags | Wall-clock duration |

---

## 6. Logging Configuration

### 6.1 `configure_logging()`

```python
def configure_logging(level: LevelName = "info") -> None: ...

LevelName = Literal["debug", "info", "warning", "error"]
```

Configures the `exectunnel` **package** logger hierarchy (not the root logger). Safe to
call multiple times — each call removes any previously attached exectunnel handler and
installs a fresh one, making re-configuration clean without duplicating output.

The output format and backend engine are controlled entirely by environment variables
read at call time:

| Env var                 | Default   | Effect                                                                           |
|-------------------------|-----------|----------------------------------------------------------------------------------|
| `EXECTUNNEL_LOG_FORMAT` | `console` | `console` → human-readable; `json` → machine-parseable JSON lines                |
| `EXECTUNNEL_LOG_ENGINE` | `stdlib`  | `stdlib` → standard formatter; `structlog` → delegates to structlog if installed |
| `EXECTUNNEL_LOG_COLOR`  | `auto`    | `auto` → colour when stderr is a TTY; `always`/`never` to force                  |

The handler is attached to the `exectunnel` logger with `propagate = False`, preventing
duplicate lines from the root logger.

### 6.2 Formatters

| Formatter           | Activated by                              | Timestamp                        |
|---------------------|-------------------------------------------|----------------------------------|
| `_ConsoleFormatter` | `EXECTUNNEL_LOG_FORMAT=console` (default) | `datetime.now(UTC)` — always UTC |
| `_JsonLogFormatter` | `EXECTUNNEL_LOG_FORMAT=json`              | `datetime.now(UTC)` — always UTC |

Both formatters emit: `ts`, `level`, `logger`, `message`, `trace_id`, `span_id`, plus any extra fields attached to the `LogRecord`.

**Console formatter detail**: at `DEBUG` level, `trace_id` and `span_id` are inlined
into the log line. At `INFO` and above they are present on the `LogRecord` (e.g., for
structlog) but not printed inline by the console renderer.

**JSON formatter detail**: `trace_id` and `span_id` are always present as top-level
keys. Additional `LogRecord` attributes not in the standard built-in set are also
serialised as extra keys.

### 6.3 `_TraceContextFilter`

Attached to every handler configured by `configure_logging()`. On each log record it injects:

* `record.trace_id` — from `current_trace_id()` or `"-"`
* `record.span_id` — from `current_span_id()` or `"-"`

This means every log line automatically carries the active trace/span context with zero caller effort.

### 6.4 `LogRingBuffer` and `LogEntry`

A `logging.Handler` subclass that captures the most recent log records in a bounded
in-memory deque. Designed for dashboard or status-page display.

```python
@dataclass(frozen=True, slots=True)
class LogEntry:
    ts: str        # formatted as "%H:%M:%S" (UTC)
    level: str     # e.g. "INFO", "WARNING"
    logger: str    # "exectunnel." prefix is stripped automatically
    message: str

class LogRingBuffer(logging.Handler):
    def __init__(self, maxlen: int = 200, level: int = logging.DEBUG) -> None: ...
    def entries(self) -> list[LogEntry]: ...   # snapshot, oldest first
    def clear(self) -> None: ...

def install_ring_buffer(
    maxlen: int = 200,
    level: int = logging.DEBUG,
) -> LogRingBuffer: ...
```

`install_ring_buffer()` creates a `LogRingBuffer` and attaches it to the `exectunnel`
logger. It returns the buffer instance so callers can poll `entries()`. All methods are
thread-safe.

### 6.5 structlog Integration

When `EXECTUNNEL_LOG_ENGINE=structlog` and the `structlog` package is installed,
`configure_logging()` configures structlog's processor chain (including trace context
injection) and attaches it to the exectunnel handler. If `structlog` is not installed,
the call logs a warning and falls back to stdlib logging transparently.

### 6.6 Optional Dependencies

| Package     | Effect if absent                                                |
|-------------|-----------------------------------------------------------------|
| `colorama`  | Console formatter renders without ANSI colour codes             |
| `structlog` | `EXECTUNNEL_LOG_ENGINE=structlog` silently falls back to stdlib |

---

## 7. Exporter Pipeline

### 7.1 `Exporter` Dataclass

```python
@dataclass
class Exporter:
    name: str
    emit: Callable[[dict[str, object]], Awaitable[None]]
    failures: int = 0
```

`failures` is incremented by the reporter on each emit error and used for
log-throttling. It is owned exclusively by the reporter and must not be mutated
externally.

### 7.2 `build_exporters()`

```python
def build_exporters(
    logger: logging.Logger,
    *,
    log_emit: Callable[[dict[str, object], dict[str, object]], object],
) -> list[Exporter]: ...
```

Reads `EXECTUNNEL_OBS_EXPORTERS` (comma-separated) and constructs one `Exporter` per
name. Unknown names are logged as WARNING and skipped. If the env var is unset or empty,
defaults to `["log"]`.

> The reporter (`run_metrics_reporter`) calls `build_exporters` internally with its own
`log_emit` implementation. Callers do **not** need to call `build_exporters` for
> standard use — it is public for advanced/custom integrations only.

### 7.3 Exporter Backends

| Name | Mechanism | Key env vars |
|---|---|---|
| `log` | Calls `log_emit(snapshot, payload)` synchronously | — |
| `file` | Appends JSONL line via `asyncio.to_thread` | `EXECTUNNEL_OBS_FILE_PATH`, `EXECTUNNEL_OBS_FILE_MAX_BYTES` |
| `http` | HTTP POST via `urllib` in `asyncio.to_thread`, with retry | `EXECTUNNEL_OBS_HTTP_URL`, `EXECTUNNEL_OBS_HTTP_TIMEOUT`, `EXECTUNNEL_OBS_HTTP_MAX_RETRIES`, `EXECTUNNEL_OBS_HTTP_HEADERS` |

### 7.4 File Exporter — Size Guard

Before each write the file exporter checks `os.path.getsize(file_path)`. If the file has
reached `EXECTUNNEL_OBS_FILE_MAX_BYTES` (default 256 MiB, minimum 1 KiB) the write is
skipped and an `OSError` is raised. This prevents unbounded disk growth when external
rotation (logrotate) is responsible for archival.

> There is an inherent TOCTOU race between the size check and the subsequent open. This
> is intentional and documented as a best-effort guard; exact enforcement would require
> advisory locking.

### 7.5 HTTP Exporter — Retry Strategy

```
attempt 0  →  immediate POST
attempt 1  →  sleep 0.5 s, retry
attempt 2  →  sleep 1.0 s, retry
attempt 3  →  sleep 2.0 s, retry
(up to EXECTUNNEL_OBS_HTTP_MAX_RETRIES retries, default 3, clamped 0–10)
```

Backoff formula: \(0.5 \times 2^{\text{attempt}}\) seconds. HTTP responses with status ≥
400 are treated as errors. On final failure the last exception is re-raised to the
reporter.

### 7.6 `build_obs_payload()`

```python
def build_obs_payload(
    snapshot: dict[str, object],
    *,
    final: bool,
    platform: str,
    service: str,
) -> dict[str, object]: ...
```

Shapes the payload for the target platform:

| `platform` | Envelope |
|---|---|
| `generic` (default) | `{timestamp, service, platform, trace_id, span_id, final, metrics}` |
| `datadog` | `{ddsource, service, message, attributes: <generic>}` |
| `splunk` | `{time, host, source, event: <generic>}` |
| `newrelic` | `{common: {attributes}, events: [<generic>]}` |

---

## 8. Metrics Reporter

### 8.1 `run_metrics_reporter()`

```python
async def run_metrics_reporter(
    interval_sec: float | None = None,
    stop_event: asyncio.Event | None = None,
    *,
    logger_name: str = "exectunnel.metrics",
) -> None: ...
```

The reporter is **self-contained**: it calls `build_exporters()` internally using its
own `log_emit` implementation, reads all configuration from environment variables, and
manages the entire snapshot-emit lifecycle. Callers need only provide an optional
`stop_event`.

| Parameter      | Default                                                             | Meaning                              |
|----------------|---------------------------------------------------------------------|--------------------------------------|
| `interval_sec` | `None` → env `EXECTUNNEL_METRICS_INTERVAL_SEC` (30 s, range 1–3600) | Seconds between snapshots            |
| `stop_event`   | `None` → private event (runs until cancelled)                       | Set to trigger final flush and exit  |
| `logger_name`  | `"exectunnel.metrics"`                                              | Logger used for reporter diagnostics |

### 8.2 Reporter Loop

```
1. Build exporters internally via build_exporters(logger, log_emit=_emit_log)
   Fallback to a private log-only exporter if build_exporters returns empty list
2. Wait for stop_event OR interval_sec timeout (whichever fires first)
3. If stop_event is set → emit final snapshot (final=True), return
4. Otherwise → emit periodic snapshot (final=False), loop
5. On task cancellation → break loop, then emit final snapshot
```

### 8.3 Log Exporter Behaviour (Reporter-Internal)

The reporter's internal `_emit_log` function produces a structured summary per interval:

* **Always**: logs a summary with total metric count, a sample of up to
  `EXECTUNNEL_METRICS_TOP_N` keys (default 12), and whether the list was truncated.
* **When `EXECTUNNEL_METRICS_VERBOSE=true`**: also logs the full snapshot at DEBUG.
* **Log level**: DEBUG by default; INFO when `EXECTUNNEL_METRICS_LOG_LEVEL_INFO=true`.

### 8.4 `emit_snapshot()`

For each `Exporter` in the list:

1. Call `METRICS.snapshot()` to get current values.
2. Build payload via `build_obs_payload()` using `EXECTUNNEL_OBS_PLATFORM` and
   `EXECTUNNEL_OBS_SERVICE`.
3. `await exporter.emit(payload)`.
4. On success → if `exporter.failures > 0`, log INFO recovery message and reset
   `exporter.failures` to 0.
5. On exception → increment `exporter.failures`; throttled log (see §8.5).

### 8.5 Failure Throttling

| `exporter.failures` value | Log action |
|---|---|
| 1 | Log WARNING (first failure) |
| 20, 40, 60, … | Log WARNING (every 20th) |
| All others | Silently increment counter |

Expected error types (`OSError`, `RuntimeError`, `urllib.error.URLError`) are logged
without traceback. Unexpected exceptions also log a DEBUG traceback for diagnosis.

---

## 9. Concurrency Model

### 9.1 Task Structure

```
asyncio event loop
│
├── run_metrics_reporter()          ← 1 long-lived task (caller-managed)
│     ├── build_exporters()         ← called once at startup
│     └── _emit_snapshot()          ← called inline each interval
│           └── exporter.emit()     ← awaited; file/HTTP use asyncio.to_thread
│
└── span() / aspan() / start_trace() ← context managers / plain function
      └── metrics_inc/observe()      ← synchronous, thread-safe
```

### 9.2 Thread Safety

| Component              | Thread-safe?     | Mechanism                                               |
|------------------------|------------------|---------------------------------------------------------|
| `MetricsRegistry`      | Yes              | `threading.Lock` on all mutations and snapshot          |
| `ContextVar` (tracing) | Yes              | Per-task copy via asyncio context inheritance           |
| `configure_logging()`  | Yes (re-entrant) | Replaces previous exectunnel handlers cleanly each call |
| `LogRingBuffer`        | Yes              | `threading.Lock` on all deque mutations                 |
| File exporter          | Yes              | `asyncio.to_thread` — blocking I/O off event loop       |
| HTTP exporter          | Yes              | `asyncio.to_thread` — blocking I/O off event loop       |

### 9.3 asyncio Safety

`MetricsRegistry` uses `threading.Lock` (not `asyncio.Lock`) so it is safe to call from both sync and async contexts, including from threads spawned by `asyncio.to_thread`. The lock is never held across an `await`.

---

## 10. Environment Variable Reference

### 10.1 Logging

| Variable                | Default   | Purpose                                                                             |
|-------------------------|-----------|-------------------------------------------------------------------------------------|
| `EXECTUNNEL_LOG_FORMAT` | `console` | Output format: `console` (human-readable) or `json` (JSON lines)                    |
| `EXECTUNNEL_LOG_ENGINE` | `stdlib`  | Logging backend: `stdlib` or `structlog` (falls back to stdlib if structlog absent) |
| `EXECTUNNEL_LOG_COLOR`  | `auto`    | ANSI colour: `auto` (TTY detection), `always`/`1`/`true`, or `never`/`0`/`false`    |

The log **level** is set via the `level` argument to `configure_logging()`, not an env
var.

### 10.2 Exporters

| Variable                          | Default                          | Purpose                                                            |
|-----------------------------------|----------------------------------|--------------------------------------------------------------------|
| `EXECTUNNEL_OBS_EXPORTERS`        | `log`                            | Comma-separated list of exporter names (`log`, `file`, `http`)     |
| `EXECTUNNEL_OBS_FILE_PATH`        | `exectunnel-observability.jsonl` | Output path for file exporter                                      |
| `EXECTUNNEL_OBS_FILE_MAX_BYTES`   | `268435456` (256 MiB)            | File size limit (min 1024 bytes); writes stop when reached         |
| `EXECTUNNEL_OBS_HTTP_URL`         | _(empty)_                        | HTTP POST endpoint for http exporter; required if `http` is listed |
| `EXECTUNNEL_OBS_HTTP_TIMEOUT`     | `5`                              | HTTP request timeout in seconds (min 0.1 s)                        |
| `EXECTUNNEL_OBS_HTTP_MAX_RETRIES` | `3`                              | Max retry attempts for HTTP exporter (range 0–10)                  |
| `EXECTUNNEL_OBS_HTTP_HEADERS`     | _(empty)_                        | Extra HTTP headers, semicolon-separated `Key=Value` pairs          |
| `EXECTUNNEL_OBS_PLATFORM`         | `generic`                        | Payload shape: `generic`, `datadog`, `splunk`, `newrelic`          |
| `EXECTUNNEL_OBS_SERVICE`          | `exectunnel`                     | Service name embedded in every exported payload                    |

### 10.3 Reporter

| Variable                            | Default | Purpose                                                               |
|-------------------------------------|---------|-----------------------------------------------------------------------|
| `EXECTUNNEL_METRICS_INTERVAL_SEC`   | `30.0`  | Seconds between periodic snapshots (min 1.0, max 3600.0)              |
| `EXECTUNNEL_METRICS_VERBOSE`        | `false` | If `true`, also logs the full metrics snapshot at DEBUG each interval |
| `EXECTUNNEL_METRICS_TOP_N`          | `12`    | Number of metric keys included in the log summary (min 1)             |
| `EXECTUNNEL_METRICS_LOG_LEVEL_INFO` | `false` | If `true`, emit metric summaries at INFO level instead of DEBUG       |

---

## 11. Error Taxonomy

### 11.1 Errors the Observability Layer Raises

| Error class             | Trigger                                                            | Notes                                |
|-------------------------|--------------------------------------------------------------------|--------------------------------------|
| `OSError`               | File exporter: size limit reached or I/O failure                   | Re-raised to reporter; throttled log |
| `RuntimeError`          | HTTP exporter: URL not set, or HTTP status ≥ 400 after all retries | Re-raised to reporter; throttled log |
| `urllib.error.URLError` | HTTP exporter: network failure after all retries                   | Re-raised to reporter; throttled log |

### 11.2 Errors the Observability Layer Handles

| Source                             | Error         | Action                                                                |
|------------------------------------|---------------|-----------------------------------------------------------------------|
| Any exporter `emit()`              | Any exception | Increment `exporter.failures`; log WARNING (throttled); continue loop |
| `configure_logging()` called again | _(no error)_  | Replaces previous exectunnel handler; clean re-configuration          |
| Unknown exporter name in env var   | _(no error)_  | Log WARNING, skip                                                     |
| `http` exporter with empty URL     | _(no error)_  | Log WARNING at `build_exporters()` time, skip                         |
| Metric listener raises exception   | Any exception | Silently suppressed; counter still incremented                        |

### 11.3 Exception Chaining Rule

All observability exceptions follow the project-wide rule:

* `raise SomeError(...) from exc` — only inside an `except` block with a live `exc`.
* `raise SomeError(...)` — when there is no causal exception to chain.
* Never `raise SomeError(...) from None` unless explicitly suppressing a confusing chain.

---

## 12. Public API Surface

### 12.1 `__init__.py` Exports (26 symbols)

```python
# logging
configure_logging        # set up exectunnel logger (stdlib or structlog, env-driven)
LevelName                # Literal["debug", "info", "warning", "error"]
LogEntry                 # frozen dataclass — single captured log record (ts, level, logger, message)
LogRingBuffer            # logging.Handler subclass — bounded in-memory log capture
install_ring_buffer      # create LogRingBuffer and attach to exectunnel logger

# metrics
METRICS                  # global MetricsRegistry singleton
MetricsRegistry          # class — for custom registries
metrics_inc              # METRICS.inc shorthand (also fires listeners)
metrics_observe          # METRICS.observe shorthand
metrics_gauge_set        # METRICS.gauge_set shorthand
metrics_gauge_inc        # METRICS.gauge_inc shorthand
metrics_gauge_dec        # METRICS.gauge_dec shorthand
metrics_snapshot         # METRICS.snapshot() shorthand
metrics_reset            # METRICS.reset() + unregister_all_listeners()
register_metric_listener # register callback on every metrics_inc() call
unregister_all_listeners # remove all metric listeners

# tracing
start_trace              # start a new trace (plain function, returns str)
span                     # open a child span (synchronous context manager)
aspan                    # open a child span (asynchronous context manager)
current_trace_id         # read active trace ID
current_span_id          # read active span ID
current_parent_span_id   # read active parent span ID

# exporters
Exporter                 # dataclass — name, emit, failures
build_exporters          # construct exporter list from env (advanced use)
build_obs_payload        # shape snapshot into platform payload (advanced use)

# reporter
run_metrics_reporter     # self-contained async loop — builds exporters, emits snapshots
```

### 12.2 Typical Caller Pattern

```python
# At application startup (CLI layer)
configure_logging(level="info")          # env vars control format, engine, color

stop_event = asyncio.Event()
asyncio.create_task(
    run_metrics_reporter(stop_event=stop_event)   # self-contained; reads all config from env
)

# At session layer entry point — each handler coroutine / asyncio.Task
trace_id = start_trace()
with span("session.run"):
    metrics_inc("session_reconnects_total", reason="initial")
    ...

# For async-heavy code:
async with aspan("session.read_frame") as span_id:
    data = await socket.read()

# Dashboard / status page — attach ring buffer at startup
ring = install_ring_buffer(maxlen=500)
# later: ring.entries() returns list[LogEntry]

# At shutdown
stop_event.set()
await reporter_task
```

---

## 13. Invariants Every Caller Must Preserve

```
1.  Never import observability from proxy or transport — session layer and above only.
2.  Always call configure_logging() before any log calls. It may be called again
    to reconfigure — each call cleanly replaces the previous exectunnel handler.
3.  Never call METRICS.reset() or metrics_reset() from application code during
    normal operation — the reporter owns the reset lifecycle.
4.  Always call start_trace() before opening any span() / aspan() — spans without
    a trace context still work but trace_id will be None in log records.
    start_trace() is a plain function, not a context manager; do not use `with`.
5.  Never share an asyncio.Event stop_event across multiple reporter instances.
6.  Never mutate Exporter.failures directly — it is owned by the reporter.
7.  Never call exporter.emit() directly — use run_metrics_reporter().
8.  Never hold a reference to a snapshot dict after passing it to an exporter —
    the reporter may reset METRICS immediately after.
9.  Always set EXECTUNNEL_OBS_HTTP_URL before listing 'http' in EXECTUNNEL_OBS_EXPORTERS.
10. Never register metric listeners that perform I/O or blocking operations —
    listeners are called synchronously on every metrics_inc() call.
11. build_exporters() is for advanced use only. In normal operation the reporter
    calls it internally; callers should not duplicate this call.
```

---

## 14. What This Layer Explicitly Does Not Do

| Concern | Why Not Here | Where It Lives |
|---|---|---|
| Frame wire protocol | Observability is protocol-agnostic | `protocol` |
| TCP / UDP I/O | Observability emits events; transport owns I/O | `transport` |
| SOCKS5 negotiation | Observability is transport-agnostic | `proxy` |
| Session reconnection | Observability records metrics; session decides | `session` |
| Log file rotation | Handled externally (logrotate, etc.) | ops tooling |
| Distributed trace collection | Observability emits IDs; collection is external | APM backend |
| Metric aggregation / percentiles | Snapshot is raw; aggregation is external | APM backend |
| Alert routing | Observability exports data; alerting is external | ops tooling |

---

## 15. Quick Reference Card

```
┌─────────────────────────────────────────────────────────────────────┐
│  OBSERVABILITY LAYER QUICK REFERENCE                                │
├─────────────────────────────────────────────────────────────────────┤
│  Startup (CLI layer)                                                │
│    configure_logging(level="info")   ← format/engine via env vars  │
│    asyncio.create_task(                                             │
│        run_metrics_reporter(stop_event=ev))  ← self-contained      │
│                                                                     │
│  Logging env vars                                                   │
│    EXECTUNNEL_LOG_FORMAT=console|json                               │
│    EXECTUNNEL_LOG_ENGINE=stdlib|structlog                           │
│    EXECTUNNEL_LOG_COLOR=auto|always|never                           │
│                                                                     │
│  Metrics (session layer)                                            │
│    metrics_inc("name", **tags)           ← counter                 │
│    metrics_observe("name", value, **tags) ← histogram              │
│    metrics_gauge_set("name", value, **tags) ← gauge absolute       │
│    metrics_gauge_inc("name", delta, **tags) ← gauge relative       │
│    metrics_gauge_dec("name", delta, **tags) ← gauge relative       │
│                                                                     │
│  Tracing (session layer)                                            │
│    trace_id = start_trace()              ← plain call, returns str │
│    with span("op.name", key=val) as sid: ...     ← sync            │
│    async with aspan("op.name", key=val) as sid: ... ← async        │
│    current_trace_id()       ← read active trace                    │
│    current_span_id()        ← read active span                     │
│    current_parent_span_id() ← read parent span                     │
│                                                                     │
│  Log ring buffer                                                    │
│    ring = install_ring_buffer(maxlen=500)                           │
│    ring.entries()   ← list[LogEntry] (oldest first)                │
│    ring.clear()                                                     │
│                                                                     │
│  Snapshot format                                                    │
│    "counter_name"                    → int                          │
│    "hist_name.count/sum/avg/min/max" → float                        │
│    "gauge_name"                      → float                        │
│    "metric{tag=val}"                 → tagged variant               │
│                                                                     │
│  Exporters (env-driven)                                             │
│    EXECTUNNEL_OBS_EXPORTERS=log,file,http                           │
│    EXECTUNNEL_OBS_FILE_PATH=out.jsonl                               │
│    EXECTUNNEL_OBS_HTTP_URL=https://ingest.example.com/metrics       │
│    EXECTUNNEL_OBS_PLATFORM=generic|datadog|splunk|newrelic          │
│    EXECTUNNEL_OBS_SERVICE=exectunnel                                │
│                                                                     │
│  Reporter                                                           │
│    EXECTUNNEL_METRICS_INTERVAL_SEC=30  (min 1, max 3600)           │
│    EXECTUNNEL_METRICS_VERBOSE=false                                 │
│    EXECTUNNEL_METRICS_TOP_N=12                                      │
│                                                                     │
│  Never do                                                           │
│    ✗ import observability from proxy or transport                   │
│    ✗ call METRICS.reset() outside the reporter loop                 │
│    ✗ call exporter.emit() directly                                  │
│    ✗ open span() without an enclosing start_trace()                 │
│    ✗ share stop_event across multiple reporter instances            │
│    ✗ pass fmt= or use_structlog= to configure_logging() (no-ops)   │
│    ✗ pass exporters to run_metrics_reporter() (it builds its own)  │
│    ✗ use I/O or blocking code inside metric listeners               │
└─────────────────────────────────────────────────────────────────────┘
```
