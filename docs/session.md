# ExecTunnel — Session Package Developer API Reference

```
exectunnel/session/  |  api-doc v2.2  |  Python 3.13+
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
2. [Configuration — `SessionConfig`](#2-configuration--sessionconfig)
3. [Configuration — `TunnelConfig`](#3-configuration--tunnelconfig)
4. [`TunnelSession` — Full API](#4-tunnelsession--full-api)
5. [Exception Reference](#5-exception-reference)
6. [Reconnect Back-off Contract](#6-reconnect-back-off-contract)

---

## 1. Imports

```python
# ── Public surface — always import from the package root ─────────────────────
from exectunnel.session import (
    TunnelSession,
    SessionConfig,
    TunnelConfig,
    AckStatus,
    AgentStatsCallable,
    DEFAULT_EXCLUDE_CIDRS,
    get_default_exclusion_networks,
)

# ── Exceptions you must handle ────────────────────────────────────────────────
from exectunnel.exceptions import (
    # Bootstrap failures — never retried by run()
    BootstrapError,  # base class — catch this for all bootstrap failures
    AgentReadyTimeoutError,  # subclass of BootstrapError
    AgentSyntaxError,  # subclass of BootstrapError
    AgentVersionMismatchError,  # subclass of BootstrapError
    # Terminal transport failure
    ReconnectExhaustedError,
    # Informational — only surface inside ReconnectExhaustedError.details
    WebSocketSendTimeoutError,
    ConnectionClosedError,
    TransportError,
    # Configuration errors — raised by config constructors, not by run()
    ConfigurationError,
)
```

> **Rule**: Never import from sub-modules (`exectunnel.session.session`,
`exectunnel.session._dispatcher`, etc.). The package root is the only stable surface.

---

## 2. Configuration — `SessionConfig`

`SessionConfig` carries WebSocket-level and transport-level tunables. It is a frozen,
slotted dataclass — construct once, pass to `TunnelSession`.

### Fields

| Field                   | Type                     | Default                                 | Description                                                             |
|-------------------------|--------------------------|-----------------------------------------|-------------------------------------------------------------------------|
| `wss_url`               | `str`                    | *(required)*                            | WebSocket URL of the Kubernetes exec endpoint (`ws://` or `wss://`)     |
| `ws_headers`            | `dict[str, str]`         | `{}`                                    | Additional HTTP headers on the WebSocket upgrade (e.g. `Authorization`) |
| `ssl_context_override`  | `ssl.SSLContext \| None` | `None`                                  | Explicit SSL context; `None` defers to `websockets` library defaults    |
| `version`               | `str`                    | `"1.0"`                                 | Client version string sent to the agent for compatibility checking      |
| `reconnect_max_retries` | `int`                    | `Defaults.WS_RECONNECT_MAX_RETRIES`     | Max reconnect attempts before `ReconnectExhaustedError`                 |
| `reconnect_base_delay`  | `float`                  | `Defaults.WS_RECONNECT_BASE_DELAY_SECS` | Initial back-off delay in seconds                                       |
| `reconnect_max_delay`   | `float`                  | `Defaults.WS_RECONNECT_MAX_DELAY_SECS`  | Maximum back-off delay cap in seconds                                   |
| `ping_interval`         | `float`                  | `Defaults.WS_PING_INTERVAL_SECS`        | Seconds between KEEPALIVE frames sent to the agent                      |
| `send_timeout`          | `float`                  | `Defaults.WS_SEND_TIMEOUT_SECS`         | Max seconds for a single WebSocket frame send                           |
| `send_queue_cap`        | `int`                    | `Defaults.WS_SEND_QUEUE_CAP`            | Bounded data frame queue capacity                                       |

### Building Manually

```python
from exectunnel.session import SessionConfig

session_cfg = SessionConfig(
    wss_url="wss://my-cluster/api/v1/namespaces/default/pods/mypod/exec"
            "?command=sh&stdin=true&stdout=true&stderr=false&tty=true",
    ws_headers={"Authorization": "Bearer <token>"},
    reconnect_max_retries=5,
    send_timeout=30.0,
)
```

### Building for Tests

```python
session_cfg = SessionConfig(
    wss_url="ws://localhost:8765",
    reconnect_max_retries=0,  # fail fast — no retries in tests
    send_timeout=5.0,
    ping_interval=60.0,  # reduce noise

    ```python
ping_interval = 60.0,  # reduce noise
)
```

> **Note**: `SessionConfig` is a frozen dataclass — all fields not listed in the
> constructor call receive their defaults. You only need to override what differs from
> the
> defaults.

### `ssl_context()` Method

```python
session_cfg.ssl_context() -> ssl.SSLContext | None
```

Returns `ssl_context_override` if set, or `None` to let `websockets` select the context
automatically. Called internally by `TunnelSession.run()` — you do not need to call it
directly.

---

## 3. Configuration — `TunnelConfig`

`TunnelConfig` carries tunnel-level tunables: SOCKS5 bind address, bootstrap settings,
DNS forwarding, timeouts, and connect-hardening limits. It is a frozen, slotted
dataclass.

### Fields

| Field                             | Type                               | Default                                    | Description                                                                                           |
|-----------------------------------|------------------------------------|--------------------------------------------|-------------------------------------------------------------------------------------------------------|
| `socks_host`                      | `str`                              | `Defaults.SOCKS_DEFAULT_HOST`              | SOCKS5 proxy bind address                                                                             |
| `socks_port`                      | `int`                              | `Defaults.SOCKS_DEFAULT_PORT`              | SOCKS5 proxy listen port                                                                              |
| `dns_upstream`                    | `str \| None`                      | `None`                                     | Upstream DNS IP forwarded through the tunnel (e.g. `"10.96.0.10"`). `None` disables the DNS forwarder |
| `dns_local_port`                  | `int`                              | `Defaults.DNS_LOCAL_PORT`                  | Local UDP port for the DNS relay on `127.0.0.1`                                                       |
| `ready_timeout`                   | `float`                            | `Defaults.READY_TIMEOUT_SECS`              | Max seconds to wait for `AGENT_READY` after exec                                                      |
| `conn_ack_timeout`                | `float`                            | `Defaults.CONN_ACK_TIMEOUT_SECS`           | Max seconds to wait for `CONN_ACK` per connection                                                     |
| `exclude`                         | `list[IPv4Network \| IPv6Network]` | RFC 1918 + loopback + IPv6 private         | CIDRs that bypass the tunnel and connect directly                                                     |
| `ack_timeout_warn_every`          | `int`                              | `Defaults.ACK_TIMEOUT_WARN_EVERY`          | Log a warning every N ACK timeouts (suppresses spam)                                                  |
| `ack_timeout_window_secs`         | `float`                            | `Defaults.ACK_TIMEOUT_WINDOW_SECS`         | Sliding window (seconds) for ACK-timeout reconnect trigger                                            |
| `ack_timeout_reconnect_threshold` | `int`                              | `Defaults.ACK_TIMEOUT_RECONNECT_THRESHOLD` | ACK failures within the window that force a reconnect                                                 |
| `connect_max_pending`             | `int`                              | `Defaults.CONNECT_MAX_PENDING`             | Global cap on concurrent in-flight `CONN_OPEN` frames                                                 |
| `connect_max_pending_per_host`    | `int`                              | `Defaults.CONNECT_MAX_PENDING_PER_HOST`    | Per-host cap on concurrent in-flight `CONN_OPEN` frames                                               |
| `pre_ack_buffer_cap_bytes`        | `int`                              | `Defaults.PRE_ACK_BUFFER_CAP_BYTES`        | Max bytes buffered per connection before `CONN_ACK` arrives                                           |
| `connect_pace_interval_secs`      | `float`                            | `Defaults.CONNECT_PACE_INTERVAL_SECS`      | Min seconds between successive `CONN_OPEN` frames to the same host                                    |
| `bootstrap_delivery`              | `Literal["upload", "fetch"]`       | `"fetch"`                                  | Agent delivery mode                                                                                   |
| `bootstrap_fetch_url`             | `str`                              | `Defaults.BOOTSTRAP_FETCH_AGENT_URL`       | URL used when `bootstrap_delivery="fetch"`                                                            |
| `bootstrap_skip_if_present`       | `bool`                             | `False`                                    | Skip delivery if the agent file already exists on the pod                                             |
| `bootstrap_syntax_check`          | `bool`                             | `False`                                    | Run `ast.parse` on the agent before exec                                                              |
| `bootstrap_agent_path`            | `str`                              | `Defaults.BOOTSTRAP_AGENT_PATH`            | Absolute path of the agent script inside the pod                                                      |
| `bootstrap_syntax_ok_sentinel`    | `str`                              | `Defaults.BOOTSTRAP_SYNTAX_OK_SENTINEL`    | Sentinel file path written after a successful syntax check                                            |
| `bootstrap_use_go_agent`          | `bool`                             | `False`                                    | Upload and run the pre-built Go agent binary instead of `agent.py`                                    |
| `bootstrap_go_agent_path`         | `str`                              | `Defaults.BOOTSTRAP_GO_AGENT_PATH`         | Absolute path of the Go agent binary inside the pod                                                   |
| `socks_handshake_timeout`         | `float`                            | `Defaults.HANDSHAKE_TIMEOUT_SECS`          | Max seconds to complete a SOCKS5 handshake                                                            |
| `socks_request_queue_cap`         | `int`                              | `Defaults.SOCKS_REQUEST_QUEUE_CAP`         | Queue capacity for completed handshakes awaiting dispatch                                             |
| `socks_queue_put_timeout`         | `float`                            | `Defaults.SOCKS_QUEUE_PUT_TIMEOUT_SECS`    | Max seconds to enqueue a completed handshake                                                          |
| `udp_relay_queue_cap`             | `int`                              | `Defaults.UDP_RELAY_QUEUE_CAP`             | Per-relay inbound datagram queue depth                                                                |
| `udp_drop_warn_every`             | `int`                              | `Defaults.UDP_WARN_EVERY`                  | Log a warning every N UDP queue-full drops                                                            |
| `udp_pump_poll_timeout`           | `float`                            | `Defaults.UDP_PUMP_POLL_TIMEOUT_SECS`      | Polling interval for the UDP ASSOCIATE pump loop (seconds)                                            |
| `udp_direct_recv_timeout`         | `float`                            | `Defaults.UDP_DIRECT_RECV_TIMEOUT_SECS`    | Timeout for receiving a UDP response on directly-connected hosts                                      |
| `dns_max_inflight`                | `int`                              | `Defaults.DNS_MAX_INFLIGHT`                | Maximum concurrent in-flight DNS queries                                                              |
| `dns_upstream_port`               | `int`                              | `Defaults.DNS_UPSTREAM_PORT`               | Upstream DNS server port                                                                              |
| `dns_query_timeout`               | `float`                            | `Defaults.DNS_QUERY_TIMEOUT_SECS`          | End-to-end DNS query timeout through the tunnel (seconds)                                             |

### Building Manually

```python
from exectunnel.session import TunnelConfig

tun_cfg = TunnelConfig(
    socks_host="127.0.0.1",
    socks_port=1080,
    dns_upstream="10.96.0.10",  # Kubernetes cluster DNS
    dns_local_port=5353,
    ready_timeout=30.0,
    conn_ack_timeout=10.0,
    bootstrap_delivery="upload",
    bootstrap_syntax_check=True,
)
```

### Building for Tests

```python
tun_cfg = TunnelConfig(
    socks_host="127.0.0.1",
    socks_port=19080,  # non-standard port to avoid conflicts
    ready_timeout=5.0,  # fail fast
    conn_ack_timeout=2.0,
    exclude=[],  # route everything through tunnel
    bootstrap_delivery="upload",
    bootstrap_skip_if_present=False,
    bootstrap_syntax_check=False,
)
```

### Default Exclusion Networks

```python
from exectunnel.session import DEFAULT_EXCLUDE_CIDRS, get_default_exclusion_networks

# String CIDRs (for display / logging)
print(DEFAULT_EXCLUDE_CIDRS)
# ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
#  "127.0.0.0/8", "::1/128", "fc00::/7", "fe80::/10")

# Pre-parsed network objects (for TunnelConfig.exclude)
networks = get_default_exclusion_networks()
tun_cfg = TunnelConfig(exclude=networks)
```

> **Security note**: `TunnelConfig.exclude` only bypasses IP literals. A domain name
> that resolves to a private IP (e.g. `internal.corp` → `10.0.0.1`) will **not** be
> excluded — the agent resolves it remotely. The exclusion list only protects against
> direct IP-literal connections.

---

## 4. `TunnelSession` — Full API

`TunnelSession` is the single public class exported by `exectunnel.session`. It
orchestrates the full tunnel lifecycle: WebSocket connection, agent bootstrap, SOCKS5
proxy, frame dispatch, DNS forwarding, and reconnection.

### 4.1 Construction

```python
from exectunnel.session import TunnelSession, SessionConfig, TunnelConfig

session = TunnelSession(
    session_cfg=session_cfg,
    tun_cfg=tun_cfg,
)
```

| Parameter     | Type            | Description                                                           |
|---------------|-----------------|-----------------------------------------------------------------------|
| `session_cfg` | `SessionConfig` | WebSocket-level and transport-level configuration                     |
| `tun_cfg`     | `TunnelConfig`  | Tunnel-level configuration (SOCKS5, bootstrap, DNS, timeouts, limits) |

**Construction is synchronous and never raises.** All I/O happens inside `run()`. You
may construct `TunnelSession` before entering an event loop.

**Do not reuse a `TunnelSession` instance** after `run()` returns or raises. Construct a
new instance for each top-level invocation.

### 4.2 `set_agent_stats_listener()` — Optional STATS Callback

```python
session.set_agent_stats_listener(callback: AgentStatsCallable | None) -> None
```

Register or clear a listener for agent-emitted `STATS` snapshots. `STATS` frames are
periodic observability snapshots (queue depths, byte/frame counters, dispatch latency
percentiles) emitted by the agent roughly once per second.

```python
from exectunnel.session import AgentStatsCallable
from typing import Any


def my_stats_listener(snapshot: dict[str, Any]) -> None:
    print(f"agent stats: {snapshot}")


session.set_agent_stats_listener(my_stats_listener)
```

**Contract**:

* The listener must not block or raise — exceptions are logged and suppressed by
  `FrameReceiver`.
* Pass `None` to disable the listener.
* Takes effect on the **next** WebSocket (re)connect — `FrameReceiver` captures the
  callback at construction time.
* Production sessions leave this unset; the measurement framework registers a listener
  that aggregates snapshots into benchmark reports.

### 4.3 `run()` — Lifecycle Method

```python
await session.run() -> None
```

`run()` is the primary public method. It:

1. Opens a WebSocket connection to `session_cfg.wss_url`.
2. Bootstraps the remote agent (`agent.py` or Go binary) into the pod.
3. Starts the local SOCKS5 proxy on `tun_cfg.socks_host:tun_cfg.socks_port`.
4. Optionally starts the DNS forwarder on `127.0.0.1:tun_cfg.dns_local_port`.
5. Serves SOCKS5 requests indefinitely, routing TCP and UDP through the tunnel.
6. On WebSocket disconnection, clears session state and reconnects with exponential
   back-off (see §6).
7. Never returns normally — it either raises or propagates `CancelledError`.

**Return value**: `None`. `run()` never returns normally.

**Exceptions raised** (see §5 for full details):

| Exception                   | Retried? | Meaning                                                     |
|-----------------------------|----------|-------------------------------------------------------------|
| `BootstrapError`            | No       | Agent failed to start — propagated immediately              |
| `AgentReadyTimeoutError`    | No       | `AGENT_READY` not received in time — propagated immediately |
| `AgentSyntaxError`          | No       | Agent script has a syntax error on the remote               |
| `AgentVersionMismatchError` | No       | Remote agent version is incompatible                        |
| `ReconnectExhaustedError`   | —        | All reconnect attempts consumed                             |
| `asyncio.CancelledError`    | —        | Caller cancelled `run()` — always propagated                |

> **Note**: `WebSocketSendTimeoutError`, `ConnectionClosedError`, `TransportError`,
`OSError`, and `TimeoutError` are caught internally and trigger a reconnect. They only
> surface to the caller as the `last_error` detail inside `ReconnectExhaustedError`
> after
> all retries are consumed.

### 4.4 Cancellation

`run()` handles `asyncio.CancelledError` explicitly:

* All in-flight SOCKS5 request tasks are cancelled.
* All open TCP connections are aborted.
* All pending `CONN_OPEN` ACK futures are cancelled.
* All UDP flows receive `on_remote_closed()`.
* The `WsSender` is stopped (send loop drained).
* The `Socks5Server` is stopped.
* `CancelledError` is re-raised after cleanup completes.

**You must not suppress `CancelledError`** in your caller. Always let it propagate or
re-raise it after your own cleanup.

```python
task = asyncio.create_task(session.run())
# ... later ...
task.cancel()
try:
    await task
except asyncio.CancelledError:
    pass  # expected — cleanup already done inside run()
```

### 4.5 Complete Usage Example

```python
import asyncio
import sys

from exectunnel.session import TunnelSession, SessionConfig, TunnelConfig
from exectunnel.exceptions import (
    BootstrapError,
    AgentSyntaxError,
    AgentVersionMismatchError,
    ReconnectExhaustedError,
)


async def main() -> None:
    session_cfg = SessionConfig(
        wss_url=(
            "wss://my-cluster/api/v1/namespaces/default/pods/mypod/exec"
            "?command=sh&stdin=true&stdout=true&stderr=false&tty=true"
        ),
        ws_headers={"Authorization": "Bearer <token>"},
        reconnect_max_retries=5,
        send_timeout=30.0,
    )

    tun_cfg = TunnelConfig(
        socks_host="127.0.0.1",
        socks_port=1080,
        dns_upstream="10.96.0.10",
        ready_timeout=30.0,
        conn_ack_timeout=10.0,
        bootstrap_delivery="upload",
    )

    session = TunnelSession(session_cfg=session_cfg, tun_cfg=tun_cfg)

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

All exceptions inherit from `ExecTunnelError` which provides these attributes on every
instance:

| Attribute    | Type             | Description                                                                                               |
|--------------|------------------|-----------------------------------------------------------------------------------------------------------|
| `message`    | `str`            | Human-readable description                                                                                |
| `error_code` | `str`            | Stable dot-namespaced identifier (e.g. `"bootstrap.agent_ready_timeout"`). Safe to match programmatically |
| `error_id`   | `str`            | Unique UUID for this specific occurrence. Include in logs and bug reports                                 |
| `details`    | `dict[str, Any]` | Structured context. Keys documented per exception below                                                   |
| `hint`       | `str \| None`    | Optional remediation hint for the operator                                                                |
| `retryable`  | `bool`           | Whether the operation that raised this exception is safe to retry                                         |

### 5.1 Bootstrap Exceptions

All bootstrap exceptions inherit from `BootstrapError(ExecTunnelError)`. All are
propagated immediately by `run()` — none are retried.

---

#### `BootstrapError`

```
retryable: True
```

Raised when the remote agent fails to start for any reason not covered by a more
specific subclass. Common causes: WebSocket closed during bootstrap, fence timeout,
shell command failure.

| `details` key     | Type    | Description                                           |
|-------------------|---------|-------------------------------------------------------|
| `command`         | `str`   | Shell command that failed (truncated to 80 chars)     |
| `host`            | `str`   | Remote WebSocket URL                                  |
| `elapsed_s`       | `float` | Seconds elapsed before failure was detected           |
| `fence_timeout_s` | `float` | Fence timeout that was exceeded (fence failures only) |

> **Behaviour in `run()`**: Propagated immediately. Never retried regardless of
`retryable=True`. The flag is informational for callers that implement their own retry
> loop above `run()`.

---

#### `AgentReadyTimeoutError`

```
retryable: True
```

`AGENT_READY` was not received within `tun_cfg.ready_timeout` seconds.

| `details` key | Type          | Description                                            |
|---------------|---------------|--------------------------------------------------------|
| `timeout_s`   | `float`       | The `ready_timeout` value that was exceeded            |
| `host`        | `str`         | Remote WebSocket URL                                   |
| `last_output` | `str \| None` | Last line seen in the diagnostic buffer before timeout |

> **Behaviour in `run()`**: Propagated immediately as a `BootstrapError` subclass. Never
> retried.

---

#### `AgentSyntaxError`

```
retryable: False
```

The remote Python interpreter reported a `SyntaxError` while loading `agent.py`.
Indicates a packaging or deployment problem — the uploaded payload is corrupt or
incompatible with the remote Python version.

| `details` key | Type        | Description                                       |
|---------------|-------------|---------------------------------------------------|
| `text`        | `str`       | The offending line text from the remote traceback |
| `filename`    | `str`       | Remote agent file path                            |
| `lineno`      | `int`       | Line number of the syntax error (0 if not parsed) |
| `diag`        | `list[str]` | Full diagnostic buffer captured before the error  |

> **Behaviour in `run()`**: Propagated immediately. Never retried.

---

#### `AgentVersionMismatchError`

```
retryable: False
```

The remote agent reports a version incompatible with the local client. Upgrade the agent
or the client to a matching version.

| `details` key     | Type  | Description                                  |
|-------------------|-------|----------------------------------------------|
| `local_version`   | `str` | Version string of the local client           |
| `remote_version`  | `str` | Version string reported by the remote agent  |
| `minimum_version` | `str` | Minimum agent version required by the client |

> **Behaviour in `run()`**: Propagated immediately. Never retried.

---

### 5.2 Transport / Reconnect Exceptions

---

#### `ReconnectExhaustedError`

```
retryable: False
```

All reconnect attempts (`session_cfg.reconnect_max_retries`) have been consumed. The
tunnel cannot recover without operator intervention.

| `details` key | Type  | Description                                        |
|---------------|-------|----------------------------------------------------|
| `attempts`    | `int` | Total number of reconnection attempts made         |
| `last_error`  | `str` | String representation of the last underlying error |

> **Hint**: Check network connectivity to the tunnel endpoint and consider increasing
`reconnect_max_retries` if transient disruptions are expected.

---

#### `WebSocketSendTimeoutError`

```
retryable: True
```

A WebSocket send operation stalled beyond `session_cfg.send_timeout` seconds. Caught
internally by `run()` and triggers a reconnect. Only surfaces to the caller inside
`ReconnectExhaustedError.details["last_error"]`.

| `details` key   | Type    | Description                              |
|-----------------|---------|------------------------------------------|
| `timeout_s`     | `float` | Configured send timeout in seconds       |
| `payload_bytes` | `int`   | Size of the frame that could not be sent |
| `msg_type`      | `str`   | Frame type that timed out                |

---

#### `ConnectionClosedError`

```
retryable: True
```

The WebSocket connection was closed unexpectedly. Caught internally by `run()` and
triggers a reconnect. Only surfaces to the caller inside
`ReconnectExhaustedError.details["last_error"]`.

| `details` key  | Type  | Description                        |
|----------------|-------|------------------------------------|
| `close_code`   | `int` | WebSocket close code (RFC 6455)    |
| `close_reason` | `str` | Human-readable close reason string |

---

### 5.3 Exception Hierarchy

```
Exception
└── ExecTunnelError
    ├── ConfigurationError          (raised by config constructors, not by run())
    ├── BootstrapError              ← catch this to handle all bootstrap failures
    │   ├── AgentReadyTimeoutError
    │   ├── AgentSyntaxError
    │   └── AgentVersionMismatchError
    ├── TransportError              ← caught internally by run(); triggers reconnect
    │   ├── WebSocketSendTimeoutError
    │   ├── ConnectionClosedError
    │   └── ReconnectExhaustedError ← catch this when all retries are consumed
    └── UnexpectedFrameError        ← caught internally by run(); triggers reconnect
```

### 5.4 Full Handler Pattern

```python
try:
    await session.run()
except AgentSyntaxError as exc:
    # Non-retryable deployment problem — exit immediately
    logger.critical(
        "agent syntax error",
        extra={"details": exc.details, "error_id": exc.error_id},
    )
    sys.exit(1)
except AgentVersionMismatchError as exc:
    # Non-retryable version problem — exit immediately
    logger.critical(
        "agent version mismatch",
        extra={"details": exc.details, "error_id": exc.error_id},
    )
    sys.exit(1)
except BootstrapError as exc:
    # Catches AgentReadyTimeoutError and generic BootstrapError
    logger.error(
        "bootstrap failed [%s]: %s",
        exc.error_code,
        exc.message,
        extra={"error_id": exc.error_id},
    )
    sys.exit(1)
except ReconnectExhaustedError as exc:
    logger.error(
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

When a WebSocket session ends due to a transport error, `run()` schedules a reconnect
using **exponential back-off with additive jitter**:

\[
\text{delay} = \min(\text{base\_delay} \times 2^{\text{attempt}},\ \text{max\_delay})
\]

\[
\text{actual\_delay} = \text{delay} + \text{uniform}(0,\ \text{delay} \times 0.25)
\]

| Parameter     | Default                                 | `SessionConfig` field         |
|---------------|-----------------------------------------|-------------------------------|
| `base_delay`  | `Defaults.WS_RECONNECT_BASE_DELAY_SECS` | `reconnect_base_delay`        |
| `max_delay`   | `Defaults.WS_RECONNECT_MAX_DELAY_SECS`  | `reconnect_max_delay`         |
| `max_retries` | `Defaults.WS_RECONNECT_MAX_RETRIES`     | `reconnect_max_retries`       |
| Jitter factor | `0.25` (25%)                            | hardcoded `_RECONNECT_JITTER` |

**Attempt counter behaviour**:

* A clean session (no error) resets the attempt counter to `0`.
* Each transport error increments the counter.
* When `attempt >= max_retries`, `ReconnectExhaustedError` is raised.

**What triggers a reconnect** (caught internally, never raised to caller):

* `WebSocketSendTimeoutError`
* `ConnectionClosedError`
* Any `TransportError` subclass
* `UnexpectedFrameError`
* `OSError` / `websockets.ConnectionClosed`
* `TimeoutError`

**What does NOT trigger a reconnect** (always propagated immediately):

* `BootstrapError` and all subclasses (`AgentReadyTimeoutError`, `AgentSyntaxError`,
  `AgentVersionMismatchError`)
* `asyncio.CancelledError`
* Any `Exception` not in the reconnect list above

