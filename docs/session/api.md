# ExecTunnel — Session Package API Reference

```
exectunnel/session/  |  api-doc v1.0  |  Python 3.13+
audience: developers wiring TunnelSession into a CLI command or test harness
```

---

## How to Read This Document

This reference is written for developers who **construct and drive** `TunnelSession`
from the outside — typically a CLI command, an integration test, or a higher-level
orchestrator. It answers:

* What do I import and from where?
* How do I build the configuration objects?
* What does `run()` do, when does it return, and when does it raise?
* What exceptions must I handle and what do they mean?
* What are the exact contracts I must not violate?

Every section is self-contained. Cross-references are explicit. No knowledge of session
layer internals is assumed.

---

## Table of Contents

1. [Imports](#1-imports)
2. [Configuration — `AppConfig`](#2-configuration--appconfig)
    * 2.1 [Fields](#21-fields)
    * 2.2 [`BridgeConfig` Fields](#22-bridgeconfig-fields)
    * 2.3 [Building from the Environment](#23-building-from-the-environment)
    * 2.4 [Building Manually (Tests)](#24-building-manually-tests)
3. [Configuration — `TunnelConfig`](#3-configuration--tunnelconfig)
    * 3.1 [Fields](#31-fields)
    * 3.2 [Building from the Environment](#32-building-from-the-environment)
    * 3.3 [Building Manually (Tests)](#33-building-manually-tests)
4. [`TunnelSession` — Full API](#4-tunnelsession--full-api)
    * 4.1 [Construction](#41-construction)
    * 4.2 [`run()` — Lifecycle Method](#42-run--lifecycle-method)
    * 4.3 [Cancellation](#43-cancellation)
    * 4.4 [Complete Usage Example](#44-complete-usage-example)
5. [Exception Reference](#5-exception-reference)
    * 5.1 [Bootstrap Exceptions](#51-bootstrap-exceptions)
    * 5.2 [Transport / Reconnect Exceptions](#52-transport--reconnect-exceptions)
    * 5.3 [Exception Hierarchy](#53-exception-hierarchy)
    * 5.4 [Full Handler Pattern](#54-full-handler-pattern)
6. [Reconnect Back-off Contract](#6-reconnect-back-off-contract)
7. [Error Handling Patterns](#7-error-handling-patterns)
    * Pattern 1 — Minimal CLI Driver
    * Pattern 2 — Cancellable Long-Running Service
    * Pattern 3 — Test Harness with Timeout
    * Pattern 4 — Structured Error Logging
8. [Checklist — Before You Ship](#8-checklist--before-you-ship)

---

## 1. Imports

```python
# ── Public surface — always import from the package root ─────────────────────
from exectunnel.session import TunnelSession

# ── Configuration — import from config.settings ──────────────────────────────
from exectunnel.config.settings import (
    AppConfig,
    BridgeConfig,
    TunnelConfig,
    get_app_config,       # builds AppConfig from environment variables
    get_tunnel_config,    # builds TunnelConfig from environment variables
)

# ── Exceptions you must handle ────────────────────────────────────────────────
from exectunnel.exceptions import (
    # Bootstrap failures — never retried (except AgentReadyTimeoutError)
    BootstrapError,
    AgentReadyTimeoutError,
    AgentSyntaxError,
    AgentVersionMismatchError,
    # Transport / reconnect failures
    ReconnectExhaustedError,
    WebSocketSendTimeoutError,
    ConnectionClosedError,
    TransportError,
)
```

> **Rule**: Never import from sub-modules (`exectunnel.session.session`,
> `exectunnel.session._handlers`, etc.). The package root is the only stable
> surface. `TunnelSession` is the only exported name.

---

## 2. Configuration — `AppConfig`

`AppConfig` carries application-level settings: the WebSocket URL, TLS policy,
and bridge tunables. It is a frozen dataclass — construct once, pass everywhere.

### 2.1 Fields

| Field      | Type           | Default      | Description                                   |
|------------|----------------|--------------|-----------------------------------------------|
| `wss_url`  | `str`          | *(required)* | WebSocket URL (`ws://` or `wss://`).          |
| `insecure` | `bool`         | *(required)* | If `True`, skip TLS certificate verification. |
| `bridge`   | `BridgeConfig` | *(required)* | WebSocket bridge tunables (see §2.2).         |
| `version`  | `str`          | `"1.0"`      | Protocol version string. Do not change.       |

### 2.2 `BridgeConfig` Fields

| Field                   | Type    | Default  | Description                                              |
|-------------------------|---------|----------|----------------------------------------------------------|
| `ping_interval`         | `float` | `20.0` s | Keepalive ping interval.                                 |
| `send_timeout`          | `float` | `30.0` s | Max time for a single `ws.send()` call.                  |
| `send_queue_cap`        | `int`   | `512`    | Max outbound frames queued before backpressure.          |
| `reconnect_max_retries` | `int`   | `5`      | Max reconnect attempts before `ReconnectExhaustedError`. |
| `reconnect_base_delay`  | `float` | `1.0` s  | Initial back-off delay.                                  |
| `reconnect_max_delay`   | `float` | `30.0` s | Maximum back-off delay (cap).                            |
| `dns_max_inflight`      | `int`   | `64`     | Max concurrent DNS resolutions through the tunnel.       |

### 2.3 Building from the Environment

```python
from exectunnel.config.settings import get_app_config
from exectunnel.exceptions import ConfigurationError

try:
    app_cfg = get_app_config()
except ConfigurationError as exc:
    # EXECTUNNEL_WSS_URL missing or malformed
    sys.exit(f"Configuration error: {exc.message}")
```

`get_app_config()` reads the following environment variables:

| Variable                            | Maps to                        | Notes                                  |
|-------------------------------------|--------------------------------|----------------------------------------|
| `EXECTUNNEL_WSS_URL` (or `WSS_URL`) | `wss_url`                      | Required. Must be `ws://` or `wss://`. |
| `WSS_INSECURE`                      | `insecure`                     | `"true"` / `"1"` / `"yes"` → `True`.   |
| `WSS_PING_INTERVAL`                 | `bridge.ping_interval`         | Integer seconds, min 1.                |
| `WSS_SEND_TIMEOUT`                  | `bridge.send_timeout`          | Float seconds, min 0.1.                |
| `WSS_SEND_QUEUE_CAP`                | `bridge.send_queue_cap`        | Integer, min 1.                        |
| `WSS_RECONNECT_MAX_RETRIES`         | `bridge.reconnect_max_retries` | Integer, min 0.                        |
| `WSS_RECONNECT_BASE_DELAY`          | `bridge.reconnect_base_delay`  | Float seconds, min 0.1.                |
| `WSS_RECONNECT_MAX_DELAY`           | `bridge.reconnect_max_delay`   | Float seconds, min 0.1.                |

### 2.4 Building Manually (Tests)

```python
from exectunnel.config.settings import AppConfig, BridgeConfig

app_cfg = AppConfig(
    wss_url="ws://localhost:8765",
    insecure=False,
    bridge=BridgeConfig(
        reconnect_max_retries=0,   # fail fast in tests
        send_timeout=5.0,
    ),
)
```

> **Note**: `AppConfig` and `BridgeConfig` are frozen dataclasses — all fields
> not listed in the constructor call receive their defaults. You only need to
> override what differs from the defaults.

---

## 3. Configuration — `TunnelConfig`

`TunnelConfig` carries tunnel-level settings: SOCKS5 bind address, DNS
forwarding, timeouts, and connect-hardening limits. It is a frozen dataclass.

### 3.1 Fields

| Field                             | Type                                       | Default                 | Description                                                                                         |
|-----------------------------------|--------------------------------------------|-------------------------|-----------------------------------------------------------------------------------------------------|
| `socks_host`                      | `str`                                      | `"127.0.0.1"`           | SOCKS5 proxy bind address.                                                                          |
| `socks_port`                      | `int`                                      | `1080`                  | SOCKS5 proxy listen port.                                                                           |
| `dns_upstream`                    | `str \| None`                              | `None`                  | Upstream DNS IP forwarded through the tunnel (e.g. `"10.96.0.10"`). `None` disables DNS forwarding. |
| `dns_local_port`                  | `int`                                      | `5353`                  | Local UDP port for the DNS relay.                                                                   |
| `ready_timeout`                   | `float`                                    | `30.0` s                | Max time to wait for `AGENT_READY` after bootstrap.                                                 |
| `conn_ack_timeout`                | `float`                                    | `10.0` s                | Max time to wait for `CONN_ACK` per connection.                                                     |
| `exclude`                         | `list[IPv4Network \| IPv6Network] \| None` | *(RFC 1918 + loopback)* | CIDRs that bypass the tunnel and connect directly.                                                  |
| `ack_timeout_warn_every`          | `int`                                      | `10`                    | Log a warning every N ACK timeouts (suppresses spam).                                               |
| `ack_timeout_window_secs`         | `float`                                    | `60.0` s                | Sliding window for ACK-timeout reconnect trigger.                                                   |
| `ack_timeout_reconnect_threshold` | `int`                                      | `5`                     | ACK timeouts within the window that force a reconnect.                                              |
| `connect_max_pending`             | `int`                                      | `128`                   | Global cap on concurrent in-flight `CONN_OPEN` frames.                                              |
| `connect_max_pending_per_host`    | `int`                                      | `16`                    | Per-host cap on concurrent in-flight `CONN_OPEN` frames.                                            |
| `pre_ack_buffer_cap_bytes`        | `int`                                      | `262144`                | Max bytes buffered per connection before `CONN_ACK` arrives.                                        |
| `connect_pace_interval_secs`      | `int`                                      | `3`                     | Interval (seconds) for the connect-pacing rate limiter.                                             |

### 3.2 Building from the Environment

```python
from exectunnel.config.settings import get_tunnel_config

tun_cfg = get_tunnel_config(app_cfg)
```

`get_tunnel_config(app_cfg, ...)` accepts keyword overrides for every field and
additionally reads environment variables for the hardening tunables:

| Variable                                     | Maps to                           |
|----------------------------------------------|-----------------------------------|
| `EXECTUNNEL_SOCKS_HOST`                      | `socks_host`                      |
| `EXECTUNNEL_SOCKS_PORT`                      | `socks_port`                      |
| `EXECTUNNEL_DNS_UPSTREAM`                    | `dns_upstream`                    |
| `EXECTUNNEL_CONN_ACK_TIMEOUT`                | `conn_ack_timeout`                |
| `EXECTUNNEL_CONNECT_MAX_PENDING`             | `connect_max_pending`             |
| `EXECTUNNEL_CONNECT_MAX_PENDING_PER_HOST`    | `connect_max_pending_per_host`    |
| `EXECTUNNEL_ACK_TIMEOUT_RECONNECT_THRESHOLD` | `ack_timeout_reconnect_threshold` |

### 3.3 Building Manually (Tests)

```python
from exectunnel.config.settings import TunnelConfig

tun_cfg = TunnelConfig(
    socks_host="127.0.0.1",
    socks_port=10800,          # non-standard port for test isolation
    ready_timeout=5.0,         # fail fast in tests
    conn_ack_timeout=2.0,
    exclude=None,              # no bypass — route everything through tunnel
)
```

---

## 4. `TunnelSession` — Full API

`TunnelSession` is the single public class exported by `exectunnel.session`.
It orchestrates the full tunnel lifecycle: WebSocket connection, agent
bootstrap, SOCKS5 proxy, frame dispatch, and reconnection.

### 4.1 Construction

```python
session = TunnelSession(app_cfg=app_cfg, tun_cfg=tun_cfg)
```

| Parameter | Type           | Description                                                  |
|-----------|----------------|--------------------------------------------------------------|
| `app_cfg` | `AppConfig`    | Application-level configuration (URL, TLS, bridge tunables). |
| `tun_cfg` | `TunnelConfig` | Tunnel-level configuration (SOCKS5, DNS, timeouts, limits).  |

**Construction is synchronous and never raises.** All I/O happens inside
`run()`. You may construct `TunnelSession` before entering an event loop.

**Do not reuse a `TunnelSession` instance** after `run()` returns or raises.
Construct a new instance for each top-level invocation.

### 4.2 `run()` — Lifecycle Method

```python
await session.run()
```

`run()` is the only public method. It:

1. Opens a WebSocket connection to `app_cfg.wss_url`.
2. Bootstraps the remote agent (`agent.py`) into the pod.
3. Starts the local SOCKS5 proxy on `tun_cfg.socks_host:tun_cfg.socks_port`.
4. Serves SOCKS5 requests indefinitely, routing TCP and UDP through the tunnel.
5. On WebSocket disconnection, clears session state and reconnects with
   exponential back-off (see §6).
6. Returns only on clean cancellation (`CancelledError` propagated) or when
   all reconnect attempts are exhausted (`ReconnectExhaustedError`).

**Return value**: `None`. `run()` never returns normally — it either raises or
propagates `CancelledError`.

**Exceptions raised** (see §5 for full details):

| Exception                   | Retried? | Meaning                                        |
|-----------------------------|----------|------------------------------------------------|
| `BootstrapError`            | No       | Agent failed to start. Propagated immediately. |
| `AgentReadyTimeoutError`    | Yes      | Agent did not emit `AGENT_READY` in time.      |
| `AgentSyntaxError`          | No       | Agent script has a syntax error on the remote. |
| `AgentVersionMismatchError` | No       | Remote agent version is incompatible.          |
| `ReconnectExhaustedError`   | —        | All reconnect attempts consumed.               |
| `asyncio.CancelledError`    | —        | Caller cancelled `run()`. Always propagated.   |

> **Note**: `WebSocketSendTimeoutError`, `ConnectionClosedError`, and other
> `TransportError` subclasses are caught internally and trigger a reconnect.
> They are only raised as the `last_error` detail inside
> `ReconnectExhaustedError` after all retries are consumed.

### 4.3 Cancellation

`run()` handles `asyncio.CancelledError` explicitly:

* All in-flight SOCKS5 request tasks are cancelled.
* All open TCP connections are aborted.
* All pending `CONN_OPEN` ACK futures are cancelled.
* The WebSocket is closed cleanly.
* `CancelledError` is re-raised after cleanup completes.

**You must not suppress `CancelledError`** in your caller. Always let it
propagate or re-raise it after your own cleanup.

```python
task = asyncio.create_task(session.run())
# ... later ...
task.cancel()
try:
    await task
except asyncio.CancelledError:
    pass  # expected — cleanup already done inside run()
```

### 4.4 Complete Usage Example

```python
import asyncio
import sys

from exectunnel.session import TunnelSession
from exectunnel.config.settings import get_app_config, get_tunnel_config
from exectunnel.exceptions import (
    ConfigurationError,
    BootstrapError,
    AgentSyntaxError,
    AgentVersionMismatchError,
    ReconnectExhaustedError,
)


async def main() -> None:
    try:
        app_cfg = get_app_config()
    except ConfigurationError as exc:
        sys.exit(f"[config] {exc.message}")

    tun_cfg = get_tunnel_config(app_cfg)
    session = TunnelSession(app_cfg=app_cfg, tun_cfg=tun_cfg)

    try:
        await session.run()
    except AgentSyntaxError as exc:
        sys.exit(
            f"[bootstrap] Agent syntax error on remote "
            f"(line {exc.details.get('lineno', '?')}): {exc.message}"
        )
    except AgentVersionMismatchError as exc:
        sys.exit(
            f"[bootstrap] Version mismatch — "
            f"local={exc.details.get('local_version')} "
            f"remote={exc.details.get('remote_version')}: {exc.message}"
        )
    except BootstrapError as exc:
        # Catches AgentReadyTimeoutError and any other bootstrap failure.
        sys.exit(f"[bootstrap] {exc.message} (error_id={exc.error_id})")
    except ReconnectExhaustedError as exc:
        sys.exit(
            f"[transport] Reconnect exhausted after "
            f"{exc.details.get('attempts')} attempts. "
            f"Last error: {exc.details.get('last_error')}"
        )
    except asyncio.CancelledError:
        raise  # propagate — do not swallow


asyncio.run(main())
```

---

## 5. Exception Reference

All exceptions inherit from `ExecTunnelError` which provides three attributes
available on every instance:

| Attribute    | Type             | Description                                                                                   |
|--------------|------------------|-----------------------------------------------------------------------------------------------|
| `message`    | `str`            | Human-readable description.                                                                   |
| `error_code` | `str`            | Stable dot-namespaced identifier (e.g. `"bootstrap.failed"`). Safe to match programmatically. |
| `error_id`   | `str`            | Unique UUID for this specific occurrence. Use in logs and bug reports.                        |
| `details`    | `dict[str, Any]` | Structured context. Keys are documented per exception below.                                  |
| `hint`       | `str \| None`    | Optional remediation hint for the operator.                                                   |
| `retryable`  | `bool`           | Whether the operation that raised this exception is safe to retry.                            |

### 5.1 Bootstrap Exceptions

All bootstrap exceptions inherit from `BootstrapError(ExecTunnelError)`.

---

#### `BootstrapError`

```
error_code: "bootstrap.failed"
retryable:  True
```

Raised when the remote agent script fails to start for any reason not covered
by a more specific subclass.

| `details` key | Type    | Description                                  |
|---------------|---------|----------------------------------------------|
| `host`        | `str`   | Remote host address.                         |
| `elapsed_s`   | `float` | Seconds elapsed before failure was detected. |

> **Behaviour in `run()`**: Propagated immediately. Never retried.

---

#### `AgentReadyTimeoutError`

```
error_code: "bootstrap.agent_ready_timeout"
retryable:  True
```

`AGENT_READY` was not received within `tun_cfg.ready_timeout` seconds.

| `details` key | Type    | Description                    |
|---------------|---------|--------------------------------|
| `timeout_s`   | `float` | The timeout that was exceeded. |
| `host`        | `str`   | Remote host address.           |

> **Behaviour in `run()`**: Treated as a `BootstrapError` — propagated
> immediately, not retried. The `retryable=True` flag is informational for
> callers that implement their own retry loop above `run()`.

---

#### `AgentSyntaxError`

```
error_code: "bootstrap.agent_syntax_error"
retryable:  False
```

The remote Python interpreter reported a `SyntaxError` while loading
`agent.py`. This indicates a packaging or deployment problem.

| `details` key | Type  | Description                      |
|---------------|-------|----------------------------------|
| `lineno`      | `int` | Line number of the syntax error. |
| `filename`    | `str` | Remote file path.                |
| `text`        | `str` | Offending source line.           |

> **Behaviour in `run()`**: Propagated immediately. Never retried.

---

#### `AgentVersionMismatchError`

```
error_code: "bootstrap.version_mismatch"
retryable:  False
```

The remote agent reports a version incompatible with the local client.
Upgrade the agent or the client to a matching version.

| `details` key     | Type  | Description                                   |
|-------------------|-------|-----------------------------------------------|
| `local_version`   | `str` | Version string of the local client.           |
| `remote_version`  | `str` | Version string reported by the remote agent.  |
| `minimum_version` | `str` | Minimum agent version required by the client. |

> **Behaviour in `run()`**: Propagated immediately. Never retried.

---

### 5.2 Transport / Reconnect Exceptions

---

#### `ReconnectExhaustedError`

```
error_code: "transport.reconnect_exhausted"
retryable:  False
```

All reconnect attempts (`bridge.reconnect_max_retries`) have been consumed.
The tunnel cannot recover without operator intervention.

| `details` key | Type  | Description                                         |
|---------------|-------|-----------------------------------------------------|
| `attempts`    | `int` | Total number of reconnection attempts made.         |
| `last_error`  | `str` | String representation of the last underlying error. |

> **Hint**: Check network connectivity to the tunnel endpoint and consider
> increasing `WSS_RECONNECT_MAX_RETRIES` if transient disruptions are expected.

---

#### `WebSocketSendTimeoutError`

```
error_code: "transport.ws_send_timeout"
retryable:  True
```

A WebSocket send operation stalled beyond `bridge.send_timeout` seconds.
Caught internally by `run()` and triggers a reconnect. Only surfaces to the
caller inside `ReconnectExhaustedError.details["last_error"]`.

| `details` key   | Type    | Description                               |
|-----------------|---------|-------------------------------------------|
| `timeout_s`     | `float` | Configured send timeout in seconds.       |
| `payload_bytes` | `int`   | Size of the frame that could not be sent. |

---

#### `ConnectionClosedError`

```
error_code: "transport.connection_closed"
retryable:  True
```

The WebSocket connection was closed unexpectedly. Caught internally by `run()`
and triggers a reconnect. Only surfaces to the caller inside
`ReconnectExhaustedError.details["last_error"]`.

| `details` key  | Type  | Description                         |
|----------------|-------|-------------------------------------|
| `close_code`   | `int` | WebSocket close code (RFC 6455).    |
| `close_reason` | `str` | Human-readable close reason string. |

---

### 5.3 Exception Hierarchy

```
Exception
└── ExecTunnelError
    ├── ConfigurationError          (raised by get_app_config(), not by run())
    ├── BootstrapError              ← catch this to handle all bootstrap failures
    │   ├── AgentReadyTimeoutError
    │   ├── AgentSyntaxError
    │   └── AgentVersionMismatchError
    └── TransportError              ← never raised directly by run()
        ├── WebSocketSendTimeoutError   (internal — triggers reconnect)
        ├── ConnectionClosedError       (internal — triggers reconnect)
        └── ReconnectExhaustedError ← catch this when all retries are consumed
```

### 5.4 Full Handler Pattern

```python
try:
    await session.run()
except AgentSyntaxError as exc:
    # Non-retryable deployment problem — exit immediately
    log.critical("agent syntax error", extra={"details": exc.details,
                                               "error_id": exc.error_id})
    sys.exit(1)
except AgentVersionMismatchError as exc:
    # Non-retryable version problem — exit immediately
    log.critical("agent version mismatch", extra={"details": exc.details,
                                                   "error_id": exc.error_id})
    sys.exit(1)
except BootstrapError as exc:
    # Catches AgentReadyTimeoutError and generic BootstrapError
    log.error("bootstrap failed [%s]: %s", exc.error_code, exc.message,
              extra={"error_id": exc.error_id})
    sys.exit(1)
except ReconnectExhaustedError as exc:
    log.error(
        "reconnect exhausted after %d attempts, last error: %s",
        exc.details.get("attempts"),
        exc.details.get("last_error"),
        extra={"error_id": exc.error_id},
    )
    sys.exit(1)
except asyncio.CancelledError:
    raise  # always propagate
```

---

## 6. Reconnect Back-off Contract

When a WebSocket session ends due to a transport error, `run()` schedules a
reconnect using **exponential back-off with full jitter**:

```
delay = min(base_delay × 2^attempt, max_delay)
jitter = uniform(0, delay × 0.25)
actual_delay = min(delay + jitter, max_delay)
```

| Parameter     | Default  | Env variable                |
|---------------|----------|-----------------------------|
| `base_delay`  | `1.0` s  | `WSS_RECONNECT_BASE_DELAY`  |
| `max_delay`   | `30.0` s | `WSS_RECONNECT_MAX_DELAY`   |
| `max_retries` | `5`      | `WSS_RECONNECT_MAX_RETRIES` |

**Attempt counter behaviour**:

* A clean session (no error) resets the attempt counter to `0`.
* Each transport error increments the counter.
* When `attempt >= max_retries`, `ReconnectExhaustedError` is raised.

**What triggers a reconnect** (caught internally, never raised to caller):

* `WebSocketSendTimeoutError`
* `ConnectionClosedError`
* Any `TransportError` subclass
* `OSError` / `websockets.ConnectionClosed`
* `TimeoutError`

**What does NOT trigger a reconnect** (always propagated):

* `BootstrapError` and all subclasses
* `asyncio.CancelledError`
* Any unexpected `Exception` not in the list above

---

## 7. Error Handling Patterns

### Pattern 1 — Minimal CLI Driver

```python
async def run_tunnel(app_cfg: AppConfig, tun_cfg: TunnelConfig) -> None:
    """Minimal driver: exit on any fatal error."""
    session = TunnelSession(app_cfg=app_cfg, tun_cfg=tun_cfg)
    try:
        await session.run()
    except BootstrapError as exc:
        sys.exit(f"Bootstrap failed: {exc.message}")
    except ReconnectExhaustedError as exc:
        sys.exit(
            f"Connection lost after {exc.details.get('attempts')} retries: "
            f"{exc.details.get('last_error')}"
        )
    except asyncio.CancelledError:
        raise
```

### Pattern 2 — Cancellable Long-Running Service

```python
async def run_service(app_cfg: AppConfig, tun_cfg: TunnelConfig) -> None:
    """Run until SIGINT/SIGTERM or fatal error."""
    loop = asyncio.get_running_loop()
    session = TunnelSession(app_cfg=app_cfg, tun_cfg=tun_cfg)
    task = asyncio.create_task(session.run(), name="tunnel-session")

    def _shutdown(sig: signal.Signals) -> None:
        log.info("received %s — shutting down", sig.name)
        task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    try:
        await task
    except asyncio.CancelledError:
        log.info("tunnel session cancelled — exiting cleanly")
    except BootstrapError as exc:
        log.critical("bootstrap failed: %s (error_id=%s)", exc.message, exc.error_id)
        raise SystemExit(1)
    except ReconnectExhaustedError as exc:
        log.error(
            "reconnect exhausted: %s (error_id=%s)",
            exc.details.get("last_error"),
            exc.error_id,
        )
        raise SystemExit(1)
```

### Pattern 3 — Test Harness with Timeout

```python
import asyncio
import pytest
from exectunnel.session import TunnelSession
from exectunnel.config.settings import AppConfig, BridgeConfig, TunnelConfig
from exectunnel.exceptions import BootstrapError

@pytest.mark.asyncio
async def test_session_bootstrap_failure(mock_ws_server):
    """Verify that a bootstrap failure propagates immediately."""
    app_cfg = AppConfig(
        wss_url=mock_ws_server.url,
        insecure=True,
        bridge=BridgeConfig(reconnect_max_retries=0),
    )
    tun_cfg = TunnelConfig(ready_timeout=1.0, socks_port=19080)
    session = TunnelSession(app_cfg=app_cfg, tun_cfg=tun_cfg)

    with pytest.raises(BootstrapError):
        async with asyncio.timeout(5.0):
            await session.run()
```

### Pattern 4 — Structured Error Logging

```python
import structlog
from exectunnel.exceptions import ExecTunnelError

log = structlog.get_logger()

async def run_with_structured_logging(session: TunnelSession) -> None:
    try:
        await session.run()
    except ExecTunnelError as exc:
        log.error(
            "tunnel error",
            error_code=exc.error_code,
            error_id=exc.error_id,
            message=exc.message,
            retryable=exc.retryable,
            **exc.details,
        )
        raise
    except asyncio.CancelledError:
        log.info("tunnel cancelled")
        raise
```

---

## 8. Checklist — Before You Ship

### Construction

- [ ] `AppConfig` constructed with a valid `wss_url` (`ws://` or `wss://`).
- [ ] `BridgeConfig.reconnect_max_retries` set appropriately for your environment.
- [ ] `TunnelConfig.socks_host` is loopback (`127.0.0.1`) unless you explicitly intend
  an open proxy.
- [ ] `TunnelConfig.exclude` reviewed — default excludes RFC 1918 and loopback; set to
  `None` only in tests.

### Exception Handling

- [ ] `BootstrapError` (and subclasses) caught and handled — these are never retried by
  `run()`.
- [ ] `ReconnectExhaustedError` caught — this is the terminal transport failure.
- [ ] `asyncio.CancelledError` is **not** suppressed — always re-raised.
- [ ] `AgentSyntaxError` and `AgentVersionMismatchError` handled separately if you need
  specific exit codes or messages.

### Cancellation

- [ ] `run()` is driven from an `asyncio.Task` if you need external cancellation.
- [ ] Signal handlers call `task.cancel()`, not `sys.exit()` directly.
- [ ] `CancelledError` from `await task` is caught and treated as a clean exit.

### Observability

- [ ] `error_id` from every caught `ExecTunnelError` is included in logs and alerts.
- [ ] `error_code` is used for metric labels, not `message` (which may change).
- [ ] `details` dict is logged in structured form for post-mortem analysis.

### Testing

- [ ] Tests use `BridgeConfig(reconnect_max_retries=0)` to fail fast.
- [ ] Tests use `TunnelConfig(ready_timeout=1.0, conn_ack_timeout=1.0)` to avoid long
  waits.
- [ ] Tests use a non-standard `socks_port` to avoid conflicts with system SOCKS
  proxies.
- [ ] `asyncio.timeout()` wraps `session.run()` in tests to prevent hangs.
