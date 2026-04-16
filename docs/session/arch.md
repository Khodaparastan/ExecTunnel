# ExecTunnel — Session Package Architecture Document

```
exectunnel/session/  |  arch-doc v1.0  |  Python 3.13+
```

---

## 1. Purpose & Scope

The `exectunnel.session` package is the **top orchestration layer** of the ExecTunnel
stack. It owns the full lifecycle of a tunnel session: WebSocket/exec-channel
connection, agent bootstrap, inbound frame dispatch, outbound frame routing, TCP and UDP
handler wiring, DNS resolution, reconnection, and graceful shutdown.

This document covers:

* What the session layer is and is not responsible for
* How it fits into the full tunnel stack
* Internal package structure and module responsibilities
* The `TunnelSession` class design in full detail
* All data flows, lifecycle state machines, and concurrency models
* Bootstrap sequence and reconnection strategy
* Payload decoding contract and routing rules
* Failure modes, error taxonomy, and backpressure strategy
* Observability surface (metrics, spans, structured logs)
* Extension points and invariants every caller must preserve

---

## 2. Position in the Stack

```
┌─────────────────────────────────────────────────────────────────────┐
│  CLI / application layer                                            │
│  ─────────────────────────────────────────────────────────────────  │
│  • Constructs TunnelSession with config                             │
│  • Calls session.run() and awaits it                                │
│  • Handles KeyboardInterrupt / CancelledError for clean exit        │
└───────────────────────────┬─────────────────────────────────────────┘
                            │  asyncio task
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  session layer  ◄── THIS DOCUMENT                                   │
│  ─────────────────────────────────────────────────────────────────  │
│  TunnelSession                                                      │
│  • WebSocket / exec-channel lifecycle                               │
│  • Agent bootstrap (AGENT_READY handshake)                          │
│  • Inbound recv_loop — parse_frame() on every line                  │
│  • Frame dispatch → TCP / UDP handlers                              │
│  • DNS resolution (_dns.py)                                         │
│  • Outbound routing (_routing.py, _ws_sender.py)                    │
│  • Payload decoding (_payload.py)                                   │
│  • LRU error-frame deduplication (_lru.py)                          │
│  • Per-session state management (_state.py)                         │
│  • Reconnection loop with exponential back-off                      │
└──────────┬────────────────────────────────┬────────────────────────┘
           │  feed() / feed_async()          │  WsSendCallable
           │  on_remote_closed()             │  (injected downward)
           ▼                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  transport layer                                                    │
│  ─────────────────────────────────────────────────────────────────  │
│  TcpConnection  •  UdpFlow                                          │
│  • upstream / downstream tasks                                      │
│  • pre-ACK buffering, half-close, registry self-eviction            │
└──────────┬────────────────────────────────┬────────────────────────┘
           │  asyncio.StreamReader/Writer    │  asyncio.Queue[bytes]
           ▼                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│  proxy layer                                                        │
│  ─────────────────────────────────────────────────────────────────  │
│  • SOCKS5 wire protocol negotiation                                 │
│  • TCP connection acceptance → hands StreamReader/Writer up         │
│  • UDP ASSOCIATE relay → calls send_datagram / recv_datagram        │
└─────────────────────────────────────────────────────────────────────┘
```

### Layer Contract Table

| Boundary                | Direction     | Mechanism                                            | Owner                               |
|-------------------------|---------------|------------------------------------------------------|-------------------------------------|
| `CLI` → `session`       | Downward call | `session.run()`                                      | CLI calls, session implements       |
| `session` → `transport` | Downward call | `feed()`, `feed_async()`, `on_remote_closed()`       | session calls, transport implements |
| `transport` → `session` | Upward send   | `WsSendCallable` (injected coroutine)                | session implements, transport calls |
| `session` → `protocol`  | Decode/encode | `parse_frame()`, `encode_*_frame()`, payload helpers | session calls, protocol is pure     |
| `session` → `proxy`     | Server start  | `ProxyServer.start()` / `stop()`                     | session owns proxy lifecycle        |

### What the Session Layer Knows

* WebSocket / exec-channel connection and reconnection
* Agent bootstrap handshake (`AGENT_READY`)
* Frame parsing — calls `parse_frame()` on every inbound line
* Payload decoding — calls `decode_binary_payload()` / `parse_host_port()`
* DNS resolution — resolves hostnames before handing to transport
* TCP and UDP handler construction and registry management
* Outbound frame routing — `_routing.py` selects the right `encode_*` call
* LRU deduplication of error frames to suppress log spam
* Reconnection with exponential back-off and jitter
* Proxy server lifecycle (start before session, stop on shutdown)

### What the Session Layer Does Not Know

| Concern                      | Owned By                              |
|------------------------------|---------------------------------------|
| Frame wire encoding details  | `protocol`                            |
| TCP stream I/O               | `transport.TcpConnection`             |
| UDP datagram I/O             | `transport.UdpFlow`                   |
| SOCKS5 wire protocol         | `proxy`                               |
| TLS / mTLS                   | Kubernetes API client (above session) |
| Agent-side socket management | agent binary                          |
| Observability export         | `observability` package               |

---

## 3. Package Structure

```
exectunnel/session/
├── __init__.py        public re-export surface
├── session.py         TunnelSession — top-level orchestrator
├── _bootstrap.py      agent bootstrap: AGENT_READY wait + proxy start
├── _recv_loop.py      inbound frame receiver and dispatcher
├── _handlers.py       per-frame-type handler functions
├── _routing.py        outbound frame type → encode_* selector
├── _ws_sender.py      WsSendCallable factory + send helpers
├── _payload.py        payload decoding helpers (binary + host:port)
├── _state.py          PendingConnect frozen dataclass
├── _lru.py            LRU cache for error-frame deduplication
└── _dns.py            async DNS resolution helper
```

### Module Dependency Graph

```
session.py
  ├── _bootstrap.py      (uses _ws_sender, protocol.frames)
  ├── _recv_loop.py      (uses _handlers, protocol.frames)
  │     └── _handlers.py (uses _payload, _routing, _state, transport)
  ├── _routing.py        (uses protocol.frames — pure, no I/O)
  ├── _ws_sender.py      (wraps WsSendCallable — no protocol dep)
  ├── _payload.py        (uses protocol.frames — pure decode)
  ├── _state.py          (stdlib only — asyncio.Future, dataclasses)
  ├── _lru.py            (stdlib only — collections.OrderedDict)
  └── _dns.py            (asyncio, socket — no protocol dep)
```

### Import Rules

```
session  →  protocol          (parse_frame, encode_*, payload helpers, IDs)
session  →  transport         (TcpConnection, UdpFlow, registries)
session  →  proxy             (ProxyServer)
session  →  exceptions        (ExecTunnelError hierarchy)
session  →  observability     (metrics, spans, logger)
session  ↛  agent             FORBIDDEN (agent is a separate binary)
session  ↛  cli               FORBIDDEN (no upward imports)
```

---

## 4. `TunnelSession` — Design & Architecture

### 4.1 Responsibility

`TunnelSession` is the single entry point for the tunnel. It:

1. Manages the outer reconnection loop
2. Opens the WebSocket / exec-channel connection
3. Runs the bootstrap sequence (waits for `AGENT_READY`)
4. Starts the inbound `recv_loop`
5. Holds `TcpRegistry` and `UdpRegistry` (dicts keyed by conn/flow ID)
6. Holds `_pending_connects` — futures awaiting agent ACK
7. Clears all per-session state on reconnect via `_clear_session_state`

### 4.2 Public Interface

```python
class TunnelSession:
    def __init__(self, config: Settings) -> None: ...

    async def run(self) -> None: ...  # outer reconnect loop

    async def shutdown(self) -> None: ...  # graceful stop
```

### 4.3 Per-Session State

| Attribute           | Type                        | Purpose                                         |
|---------------------|-----------------------------|-------------------------------------------------|
| `_tcp_registry`     | `TcpRegistry`               | Live `TcpConnection` objects keyed by `conn_id` |
| `_udp_registry`     | `UdpRegistry`               | Live `UdpFlow` objects keyed by `flow_id`       |
| `_pending_connects` | `dict[str, PendingConnect]` | Futures awaiting `CONN_OPEN` ACK from agent     |
| `_ws_send`          | `WsSendCallable`            | Injected into every transport handler           |
| `_request_tasks`    | `set[asyncio.Task]`         | Active per-request tasks for cancellation       |

All per-session state is cleared by `_clear_session_state()` before each reconnect
attempt.

---

## 5. Lifecycle State Machine

### 5.1 Outer Reconnection Loop

```
                    ┌──────────────────────────────┐
                    │           IDLE               │
                    │  (before first run() call)   │
                    └──────────────┬───────────────┘
                                   │ run() called
                                   ▼
                    ┌──────────────────────────────┐
              ┌────►│        CONNECTING            │◄────────────────┐
              │     │  open WebSocket / exec chan  │                 │
              │     └──────────────┬───────────────┘                 │
              │                    │ connection established           │
              │                    ▼                                  │
              │     ┌──────────────────────────────┐                 │
              │     │       BOOTSTRAPPING           │                 │
              │     │  wait for AGENT_READY frame  │                 │
              │     └──────────────┬───────────────┘                 │
              │                    │ AGENT_READY received             │
              │                    ▼                                  │
              │     ┌──────────────────────────────┐                 │
              │     │          RUNNING             │                 │
              │     │  recv_loop + proxy active    │                 │
              │     └──────────────┬───────────────┘                 │
              │                    │ error / disconnect               │
              │                    ▼                                  │
              │     ┌──────────────────────────────┐                 │
              │     │         RESETTING            │                 │
              │     │  _clear_session_state()      │                 │
              │     │  cancel _request_tasks       │                 │
              │     └──────────────┬───────────────┘                 │
              │                    │                                  │
              │     ┌──────────────▼───────────────┐                 │
              │     │       BACK-OFF WAIT           ├─────────────────┘
              │     │  exponential delay + jitter  │  retry
              │     └──────────────────────────────┘
              │
              │  shutdown() called at any state
              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          STOPPED                                    │
└─────────────────────────────────────────────────────────────────────┘
```

### 5.2 Back-off Parameters

| Parameter     | Value | Notes                                     |
|---------------|-------|-------------------------------------------|
| Initial delay | 1 s   | First retry after 1 second                |
| Multiplier    | 2×    | Doubles on each consecutive failure       |
| Maximum delay | 60 s  | Capped to prevent unbounded waits         |
| Jitter        | ±20 % | Uniform random to prevent thundering herd |

---

## 6. Bootstrap Sequence

```
Client (session)                              Agent (in-pod)
     │                                              │
     │── open WebSocket / exec channel ────────────►│
     │                                              │── start listener
     │                                              │── resolve DNS
     │◄─────────────── AGENT_READY ─────────────────│
     │                                              │
     │── start ProxyServer ────────────────────────►│ (local only)
     │── start recv_loop                            │
     │                                              │
     │  [session is now RUNNING]                    │
```

`_bootstrap.py` owns this sequence:

1. Reads lines from the channel until `AGENT_READY` is received or timeout fires.
2. Any non-frame line before `AGENT_READY` is logged and discarded.
3. An `ERROR` frame before `AGENT_READY` raises `SessionBootstrapError`.
4. Timeout raises `SessionBootstrapError` with a descriptive message.
5. On success, `ProxyServer.start()` is called and `recv_loop` is scheduled.

---

## 7. Inbound Frame Dispatch

### 7.1 `recv_loop` Responsibility

`_recv_loop.py` owns the inbound path:

1. Reads raw lines from the channel (one line = one frame candidate).
2. Calls `parse_frame()` on each line — returns `ParsedFrame | None`.
3. `None` (non-frame line) → logged at DEBUG and discarded.
4. Dispatches `ParsedFrame` to the appropriate handler in `_handlers.py`.

### 7.2 Frame Dispatch Table

| `msg_type`    | Handler function           | Notes                                                  |
|---------------|----------------------------|--------------------------------------------------------|
| `CONN_OPEN`   | `handle_conn_open`         | Agent ACK — resolves `PendingConnect` future           |
| `CONN_CLOSE`  | `handle_conn_close`        | Calls `conn.on_remote_closed()`                        |
| `DATA`        | `handle_data`              | Decodes payload, calls `conn.feed()` or `feed_async()` |
| `UDP_OPEN`    | `handle_udp_open`          | Agent ACK for UDP flow                                 |
| `UDP_DATA`    | `handle_udp_data`          | Decodes payload, calls `flow.feed()`                   |
| `UDP_CLOSE`   | `handle_udp_close`         | Calls `flow.on_remote_closed()`                        |
| `ERROR`       | `handle_error`             | Decodes message, logs, resolves/rejects pending        |
| `AGENT_READY` | _(ignored post-bootstrap)_ | Logged at DEBUG, no action                             |

### 7.3 Dispatch Rule: Unknown `conn_id`

If a frame arrives with a `conn_id` not present in `_tcp_registry` or `_udp_registry`:

* `DATA` / `CONN_CLOSE` / `UDP_DATA` / `UDP_CLOSE` → logged at WARNING, discarded.
* `ERROR` with `SESSION_CONN_ID` → session-level error, triggers reconnect.
* `ERROR` with unknown per-connection ID → logged at WARNING, discarded.

---

## 8. Payload Decoding Contract

**Rule: All payload decoding happens in the session layer, never in transport.**

`_payload.py` provides two helpers:

```python
def decode_binary_payload(frame: ParsedFrame) -> bytes:
    """Decode base64url payload from a DATA / UDP_DATA / ERROR frame."""


def decode_host_port_payload(frame: ParsedFrame) -> tuple[str, int]:
    """Decode host:port payload from a CONN_OPEN / UDP_OPEN frame."""
```

Both raise `FrameDecodingError` (a `ProtocolError` subclass) on malformed input. The
session layer catches this and either discards the frame or triggers reconnect depending
on severity.

Transport handlers receive **already-decoded `bytes`** via `feed()` / `feed_async()` —
they never see raw base64url strings.

---

## 9. Outbound Frame Routing

`_routing.py` maps a logical operation to the correct `encode_*_frame()` call:

```python
def route_conn_open(conn_id: str, host: str, port: int) -> str: ...


def route_conn_close(conn_id: str) -> str: ...


def route_data(conn_id: str, data: bytes) -> str: ...


def route_udp_open(flow_id: str, host: str, port: int) -> str: ...


def route_udp_data(flow_id: str, data: bytes) -> str: ...


def route_udp_close(flow_id: str) -> str: ...


def route_error(conn_id: str, message: str) -> str: ...
```

All functions are **pure** (no I/O, no asyncio). They return an encoded frame string
ready to pass to `WsSendCallable`.

`_ws_sender.py` wraps the raw WebSocket send callable and provides:

```python
async def ws_send(frame: str) -> None: ...
```

This is the `WsSendCallable` injected into every `TcpConnection` and `UdpFlow` at
construction time.

---

## 10. DNS Resolution

`_dns.py` provides:

```python
async def resolve_host(host: str) -> str:
    """Resolve hostname to IP string. Returns host unchanged if already an IP."""
```

Resolution rules:

* IPv4 and IPv6 literals pass through unchanged (detected via `ipaddress.ip_address`).
* Hostnames are resolved via `asyncio.get_event_loop().getaddrinfo()`.
* Resolution failure raises `ExecTunnelError` with the original hostname in `details`.
* The session layer resolves **before** constructing transport handlers — transport
  never sees unresolved hostnames.

---

## 11. `PendingConnect` — ACK Tracking

`_state.py` defines:

```python
@dataclasses.dataclass(frozen=True)
class PendingConnect:
    conn_id: str
    ack_future: asyncio.Future[None]
```

Lifecycle:

1. Proxy layer calls into session to open a TCP connection.
2. Session creates `PendingConnect`, stores in `_pending_connects[conn_id]`.
3. Session sends `CONN_OPEN` frame to agent.
4. `recv_loop` receives `CONN_OPEN` ACK → resolves `ack_future`.
5. Proxy awaits `ack_future` before starting data transfer.
6. On `ERROR` frame for that `conn_id` → `ack_future` is set with exception.
7. On session reset → `ack_future.cancel()` for all pending entries.

---

## 12. LRU Error Deduplication

`_lru.py` provides `LruCache[K, V]` — a fixed-capacity ordered dict evicting the
least-recently-used entry on overflow.

The session layer uses it to suppress duplicate `ERROR` frame log lines:

```python
_error_lru: LruCache[str, str]  # conn_id → last error message
```

If the same `(conn_id, message)` pair arrives again within the LRU window, the log line
is suppressed. This prevents log flooding when an agent repeatedly sends the same error
for a stale connection.

---

## 13. Session Reset — `_clear_session_state`

Called before every reconnect attempt. Sequence:

```
1. For each TcpConnection in _tcp_registry:
     if conn.is_started  →  conn.abort()
     else                →  await conn.close_unstarted()
2. _tcp_registry.clear()

3. For each PendingConnect in _pending_connects:
     if not pending.ack_future.done()  →  pending.ack_future.cancel()
4. _pending_connects.clear()

5. For each UdpFlow in _udp_registry:
     await flow.close()
6. _udp_registry.clear()

7. _request_tasks is already cancelled by _run_tasks() before this call.
   _request_tasks.clear() here is a safety net only.
```

**Why `abort()` not `close()`:** `TcpConnection` has no `close()` method. Started
connections must be aborted (hard cancel); unstarted connections use `close_unstarted()`
to drain the pre-ACK buffer and release resources without starting tasks.

---

## 14. Concurrency Model

### 14.1 Task Structure

```
asyncio event loop
│
├── TunnelSession.run()                    ← outer reconnect loop (1 task)
│     │
│     ├── _bootstrap()                     ← sequential, no task
│     │
│     └── _recv_loop()                     ← 1 task per session
│           │
│           └── _handlers.*()              ← called inline (no extra task)
│                 │
│                 └── TcpConnection.start() ← spawns 2 tasks per connection
│                       ├── _upstream()    ← reads from proxy, sends to agent
│                       └── _downstream() ← reads from agent, writes to proxy
│
├── ProxyServer                            ← 1 task (asyncio server)
│     └── per-connection handler          ← 1 task per SOCKS5 client
│
└── _request_tasks (set)                  ← tracked for bulk cancellation
```

### 14.2 Synchronisation Points

| Mechanism                    | Purpose                                        |
|------------------------------|------------------------------------------------|
| `PendingConnect.ack_future`  | Proxy waits for agent ACK before data transfer |
| `TcpConnection.closed_event` | Session can await full TCP cleanup             |
| `asyncio.Event` (shutdown)   | Signals all loops to exit cleanly              |
| `_request_tasks` set         | Bulk cancel on reconnect or shutdown           |

### 14.3 Thread Safety

The session layer is **single-threaded asyncio**. No locks, no thread pools, no
`run_in_executor` except inside `_dns.py` for `getaddrinfo`. All state mutations happen
on the event loop thread.

---

## 15. Error Taxonomy

### 15.1 Errors the Session Layer Raises

| Error class             | Trigger                                       | `details` keys     |
|-------------------------|-----------------------------------------------|--------------------|
| `SessionBootstrapError` | Timeout or `ERROR` frame before `AGENT_READY` | `timeout`, `frame` |
| `FrameDecodingError`    | Malformed payload in inbound frame            | `raw`, `codec`     |
| `ExecTunnelError`       | DNS resolution failure                        | `host`, `reason`   |

### 15.2 Errors the Session Layer Handles

| Source                    | Error                                        | Action                                   |
|---------------------------|----------------------------------------------|------------------------------------------|
| `parse_frame()`           | `FrameDecodingError`                         | Log WARNING, discard frame               |
| `decode_binary_payload()` | `FrameDecodingError`                         | Log WARNING, discard frame               |
| `TcpConnection.feed()`    | `TypeError` (non-bytes)                      | Caught by `require_bytes()` in transport |
| Agent `ERROR` frame       | Session-level (`SESSION_CONN_ID`)            | Log ERROR, trigger reconnect             |
| Agent `ERROR` frame       | Per-connection                               | Reject `ack_future`, log WARNING         |
| WebSocket disconnect      | `ConnectionError` / `asyncio.CancelledError` | Trigger reconnect loop                   |

### 15.3 Exception Chaining Rule

All session-layer exceptions follow the project-wide rule:

* `raise SomeError(...) from exc` — **only inside an `except` block** with a live `exc`.
* `raise SomeError(...)` — when there is no causal exception to chain.
* Never `raise SomeError(...) from None` unless explicitly suppressing a confusing
  chain.

---

## 16. Observability Surface

### 16.1 Metrics

| Metric                               | Type      | Labels     | Meaning                            |
|--------------------------------------|-----------|------------|------------------------------------|
| `session_reconnects_total`           | Counter   | `reason`   | Number of reconnect attempts       |
| `session_active_tcp_connections`     | Gauge     | —          | Current entries in `_tcp_registry` |
| `session_active_udp_flows`           | Gauge     | —          | Current entries in `_udp_registry` |
| `session_frames_inbound_total`       | Counter   | `msg_type` | Inbound frames dispatched          |
| `session_frames_outbound_total`      | Counter   | `msg_type` | Outbound frames sent               |
| `session_bootstrap_duration_seconds` | Histogram | —          | Time from connect to `AGENT_READY` |

### 16.2 Spans

| Span                   | Attributes                             |
|------------------------|----------------------------------------|
| `session.run`          | `reconnect_count`, `disconnect_reason` |
| `session.bootstrap`    | `duration_ms`                          |
| `session.handle_frame` | `msg_type`, `conn_id`                  |

### 16.3 Structured Log Fields

| Field               | Type    | Present On                   |
|---------------------|---------|------------------------------|
| `conn_id`           | `str`   | All per-connection log lines |
| `flow_id`           | `str`   | All per-flow log lines       |
| `msg_type`          | `str`   | Frame dispatch log lines     |
| `reconnect_attempt` | `int`   | Reconnect loop log lines     |
| `back_off_seconds`  | `float` | Back-off wait log lines      |
| `error_message`     | `str`   | ERROR frame log lines        |

### 16.4 Log Levels

| Level     | Usage                                                                     |
|-----------|---------------------------------------------------------------------------|
| `DEBUG`   | Non-frame lines discarded, duplicate error suppression, state transitions |
| `INFO`    | Session connected, `AGENT_READY` received, session disconnected           |
| `WARNING` | Unknown `conn_id` in frame, malformed payload discarded                   |
| `ERROR`   | Session-level `ERROR` frame, bootstrap failure, unrecoverable disconnect  |

---

## 17. Configuration Constants

| Constant                    | Location     | Value  | Purpose                          |
|-----------------------------|--------------|--------|----------------------------------|
| `BOOTSTRAP_TIMEOUT_SECONDS` | `session.py` | `30`   | Max wait for `AGENT_READY`       |
| `RECONNECT_INITIAL_DELAY`   | `session.py` | `1.0`  | First back-off delay (seconds)   |
| `RECONNECT_MAX_DELAY`       | `session.py` | `60.0` | Maximum back-off delay (seconds) |
| `RECONNECT_MULTIPLIER`      | `session.py` | `2.0`  | Back-off growth factor           |
| `RECONNECT_JITTER`          | `session.py` | `0.2`  | Jitter fraction (±20 %)          |
| `ERROR_LRU_CAPACITY`        | `_lru.py`    | `256`  | Max unique error entries tracked |

---

## 18. Invariants Every Caller Must Preserve

```
1. Never call session.run() more than once concurrently.
2. Always await session.shutdown() before discarding the object.
3. Never mutate _tcp_registry or _udp_registry directly from outside session.
4. Never store SESSION_CONN_ID as a registry key.
5. Never call parse_frame() or decode_binary_payload() in transport — session only.
6. Always inject the same ws_send callable into all handlers within one session.
7. Never start a TcpConnection before its PendingConnect.ack_future resolves.
8. Always cancel ack_future (not the PendingConnect itself) on session reset.
9. Never call conn.close() — use abort() for started, close_unstarted() for unstarted.
10. DNS resolution must complete before constructing TcpConnection or UdpFlow.
```

---

## 19. What This Layer Explicitly Does Not Do

| Concern                      | Why Not Here                                    | Where It Lives            |
|------------------------------|-------------------------------------------------|---------------------------|
| Frame wire encoding details  | Session calls typed helpers                     | `protocol`                |
| TCP stream I/O               | Session feeds decoded bytes to handlers         | `transport.TcpConnection` |
| UDP datagram I/O             | Session feeds decoded bytes to flows            | `transport.UdpFlow`       |
| SOCKS5 wire protocol         | Session is SOCKS5-agnostic                      | `proxy`                   |
| TLS / mTLS                   | Handled by Kubernetes API client                | above session             |
| Agent-side socket management | Agent is a separate binary                      | agent                     |
| Observability export         | Session emits events; export is separate        | `observability`           |
| Frame length enforcement     | Enforced by `protocol._encode_frame`            | `protocol`                |
| ID generation                | Session calls `new_conn_id()` / `new_flow_id()` | `protocol.ids`            |

---

## 20. Quick Reference Card

```
┌─────────────────────────────────────────────────────────────────────┐
│  SESSION LAYER QUICK REFERENCE                                      │
├─────────────────────────────────────────────────────────────────────┤
│  Entry point                                                        │
│    session = TunnelSession(config)                                  │
│    await session.run()           ← blocks until shutdown            │
│    await session.shutdown()      ← graceful stop                    │
│                                                                     │
│  Inbound path (recv_loop)                                           │
│    raw line → parse_frame() → ParsedFrame | None                   │
│    ParsedFrame → _handlers.handle_*(frame, registries, ...)         │
│    payload → decode_binary_payload() / decode_host_port_payload()   │
│    decoded bytes → conn.feed() / flow.feed()                        │
│                                                                     │
│  Outbound path (routing)                                            │
│    _routing.route_*() → encoded frame str                           │
│    ws_send(frame)     → WsSendCallable (injected into handlers)     │
│                                                                     │
│  TCP connection lifecycle (session perspective)                     │
│    new_conn_id() → send CONN_OPEN → await ack_future               │
│    CONN_OPEN ACK → conn.start() → data flows                        │
│    CONN_CLOSE / ERROR → conn.on_remote_closed()                     │
│    reset → conn.abort() or await conn.close_unstarted()             │
│                                                                     │
│  UDP flow lifecycle (session perspective)                           │
│    new_flow_id() → await flow.open() → sends UDP_OPEN               │
│    UDP_DATA → flow.feed(decoded_bytes)                              │
│    UDP_CLOSE → flow.on_remote_closed()                              │
│    reset → await flow.close()                                       │
│                                                                     │
│  PendingConnect                                                     │
│    PendingConnect(conn_id, ack_future=asyncio.Future())             │
│    _pending_connects[conn_id] = pending                             │
│    await pending.ack_future      ← proxy waits here                 │
│    pending.ack_future.cancel()   ← on session reset                 │
│                                                                     │
│  Never do                                                           │
│    ✗ parse_frame() in transport or proxy — session layer only       │
│    ✗ decode_binary_payload() in transport — session layer only      │
│    ✗ conn.close() — use abort() or close_unstarted()                │
│    ✗ pending.cancel() — cancel pending.ack_future instead           │
│    ✗ mutate _tcp_registry / _udp_registry from outside session      │
│    ✗ store SESSION_CONN_ID as a registry key                        │
│    ✗ start TcpConnection before ack_future resolves                 │
│    ✗ resolve DNS inside transport — resolve before constructing     │
└─────────────────────────────────────────────────────────────────────┘
```
