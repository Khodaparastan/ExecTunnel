# ExecTunnel — Session Package Architecture Document

```
exectunnel/session/  |  arch-doc v2.2  |  Python 3.13+
```

---

## 1. Purpose & Scope

The `exectunnel.session` package is the **top orchestration layer** of the ExecTunnel
stack. It owns the full lifecycle of a tunnel session: WebSocket/exec-channel
connection, agent bootstrap (upload + exec + `AGENT_READY` handshake), inbound frame
dispatch, outbound frame encoding, TCP and UDP handler wiring, DNS-over-tunnel
forwarding, reconnection with exponential back-off, and graceful shutdown.

This document covers:

* What the session layer is and is not responsible for
* How it fits into the full tunnel stack
* Internal package structure and module responsibilities
* The `TunnelSession` class design in full detail
* All data flows, lifecycle state machines, and concurrency models
* Bootstrap sequence and reconnection strategy
* Frame dispatch contract and routing rules
* Failure modes, error taxonomy, and backpressure strategy
* Observability surface (metrics, spans, structured logs)
* Extension points and invariants every caller must preserve

---

## 2. Position in the Stack

```
┌─────────────────────────────────────────────────────────────────────┐
│  CLI / application layer                                            │
│  ─────────────────────────────────────────────────────────────────  │
│  • Constructs TunnelSession(session_cfg, tun_cfg)                   │
│  • Calls await session.run()                                        │
│  • Handles CancelledError / BootstrapError / ReconnectExhaustedError│
└───────────────────────────┬─────────────────────────────────────────┘
                            │  asyncio task
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  session layer  ◄── THIS DOCUMENT                                   │
│  ─────────────────────────────────────────────────────────────────  │
│  TunnelSession (session.py)                                         │
│  • WebSocket / exec-channel lifecycle + reconnect loop              │
│  • Agent bootstrap — AgentBootstrapper (_bootstrap.py)              │
│  • Outbound frames — WsSender + KeepaliveLoop (_sender.py)          │
│  • Inbound frames — FrameReceiver (_receiver.py)                    │
│  • CONNECT / UDP dispatch — RequestDispatcher (_dispatcher.py)      │
│  • DNS-over-tunnel — DnsForwarder (_dns.py)                         │
│  • Host exclusion routing — is_host_excluded (_routing.py)          │
│  • Agent payload loading — load_agent_b64 (_payload.py)             │
│  • Per-host LRU structures — LruDict (_lru.py)                      │
│  • Per-connection ACK state — AckStatus, PendingConnect (_state.py) │
│  • Direct UDP socket helpers — make_udp_socket (_udp_socket.py)     │
│  • Shared constants — _constants.py                                 │
└──────────┬────────────────────────────────┬────────────────────────┘
           │  feed() / feed_async()          │  WsSendCallable
           │  try_feed() / on_remote_closed()│  (injected downward)
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

| Boundary                | Direction        | Mechanism                                                                                | Owner                               |
|-------------------------|------------------|------------------------------------------------------------------------------------------|-------------------------------------|
| `CLI` → `session`       | Downward call    | `session.run()`                                                                          | CLI calls, session implements       |
| `session` → `transport` | Downward call    | `feed()`, `try_feed()`, `feed_async()`, `on_remote_closed()`, `abort()`                  | session calls, transport implements |
| `transport` → `session` | Upward send      | `WsSendCallable` (injected coroutine)                                                    | session implements, transport calls |
| `session` → `protocol`  | Decode/encode    | `parse_frame()`, `encode_*_frame()`, `decode_binary_payload()`, `decode_error_payload()` | session calls, protocol is pure     |
| `session` → `proxy`     | Server lifecycle | `Socks5Server` async context manager + `async for`                                       | session owns proxy lifecycle        |

### What the Session Layer Knows

* WebSocket / exec-channel connection and reconnection
* Agent bootstrap: upload, exec, `AGENT_READY` handshake
* Frame parsing — calls `parse_frame()` on every inbound line
* Payload decoding — calls `decode_binary_payload()` / `decode_error_payload()` before
  feeding transport
* DNS-over-tunnel forwarding — `DnsForwarder` opens a `UdpFlow` per query
* TCP and UDP handler construction and registry management
* Outbound frame encoding — calls typed `encode_*_frame()` helpers directly
* Host exclusion routing — `is_host_excluded()` decides tunnel vs. direct
* Reconnection with exponential back-off and jitter
* SOCKS5 server lifecycle (started per session, stopped on teardown)
* KEEPALIVE heartbeat loop

### What the Session Layer Does Not Know

| Concern                           | Owned By                              |
|-----------------------------------|---------------------------------------|
| Frame wire encoding details       | `protocol`                            |
| TCP stream I/O                    | `transport.TcpConnection`             |
| UDP datagram I/O                  | `transport.UdpFlow`                   |
| SOCKS5 wire protocol              | `proxy`                               |
| DNS resolution of CONNECT targets | agent (resolves remotely)             |
| TLS / mTLS                        | Kubernetes API client (above session) |
| Agent-side socket management      | agent binary                          |
| Observability export              | `observability` package               |

---

## 3. Package Structure

```
exectunnel/session/
├── __init__.py        public re-export surface
├── session.py         TunnelSession — top-level orchestrator
├── _bootstrap.py      AgentBootstrapper — upload, exec, AGENT_READY wait
├── _sender.py         WsSender (WsSendCallable impl) + KeepaliveLoop
├── _receiver.py       FrameReceiver — inbound frame dispatch
├── _dispatcher.py     RequestDispatcher — CONNECT + UDP_ASSOCIATE handlers
├── _dns.py            DnsForwarder — DNS-over-tunnel UDP forwarder
├── _routing.py        is_host_excluded() + DEFAULT_EXCLUDE_CIDRS
├── _payload.py        load_agent_b64 / load_go_agent_b64 / load_pod_echo_b64
├── _state.py          AckStatus (StrEnum) + PendingConnect (frozen dataclass)
├── _lru.py            LruDict — bounded LRU OrderedDict for per-host structures
├── _udp_socket.py     make_udp_socket / resolve_address_family
├── _constants.py      shared numeric constants and string literals
└── _types.py          AgentStatsCallable, ReconnectCallable, KT/VT/DefaultT
```

### Module Responsibility Matrix

| Module           | Sync/Async        | I/O           | State            | Exported                                                        |
|------------------|-------------------|---------------|------------------|-----------------------------------------------------------------|
| `_constants.py`  | Sync (data only)  | None          | None             | No                                                              |
| `_types.py`      | Sync (types only) | None          | None             | No                                                              |
| `_state.py`      | Sync              | None          | Per-connection   | Yes (`AckStatus`)                                               |
| `_lru.py`        | Sync              | None          | Per-host LRU     | No                                                              |
| `_routing.py`    | Sync              | None          | None             | Yes (`DEFAULT_EXCLUDE_CIDRS`, `get_default_exclusion_networks`) |
| `_payload.py`    | Sync              | Disk (cached) | Cached payload   | No                                                              |
| `_udp_socket.py` | Async             | DNS + socket  | None             | No                                                              |
| `_bootstrap.py`  | Async             | WebSocket     | Bootstrap stash  | No                                                              |
| `_sender.py`     | Async             | WebSocket     | Queue + task     | No                                                              |
| `_receiver.py`   | Async             | WebSocket     | None             | No                                                              |
| `_dispatcher.py` | Async             | TCP + UDP     | Per-host gates   | No                                                              |
| `_dns.py`        | Async             | UDP socket    | In-flight tasks  | No                                                              |
| `session.py`     | Async             | WebSocket     | Session lifetime | Yes (`TunnelSession`)                                           |

### Internal Dependency Graph

```
session.py
    ├── _bootstrap.py
    │     ├── _constants.py
    │     ├── _payload.py
    │     └── exectunnel.protocol (is_ready_frame)
    ├── _sender.py
    │     ├── _config.py
    │     ├── _constants.py
    │     └── exectunnel.protocol (FRAME_PREFIX, FRAME_SUFFIX,
    │                              encode_keepalive_frame)
    ├── _receiver.py
    │     ├── _constants.py
    │     ├── _state.py
    │     ├── _types.py
    │     └── exectunnel.protocol (parse_frame, decode_binary_payload,
    │                              decode_error_payload, encode_error_frame,
    │                              SESSION_CONN_ID)
    ├── _dispatcher.py
    │     ├── _config.py
    │     ├── _constants.py
    │     ├── _lru.py
    │     ├── _routing.py
    │     ├── _state.py
    │     ├── _types.py
    │     ├── _udp_socket.py
    │     └── exectunnel.protocol (encode_conn_open_frame, new_conn_id,
    │                              new_flow_id, Reply)
    ├── _dns.py
    │     └── exectunnel.protocol (new_flow_id)
    ├── _config.py
    │     └── _routing.py (get_default_exclusion_networks)
    ├── _routing.py
    │     └── _constants.py (DEFAULT_EXCLUDE_IPV6_CIDRS)
    ├── _state.py          (stdlib only)
    ├── _lru.py            (stdlib only)
    ├── _types.py          (stdlib only)
    ├── _constants.py      (stdlib only)
    └── _udp_socket.py     (stdlib only)
```

The graph is a strict DAG. `_constants.py`, `_types.py`, `_state.py`, `_lru.py` are leaf
nodes with zero internal dependencies.

### Import Rules

```python
# ✅ Always import from the package root
from exectunnel.session import TunnelSession, SessionConfig, TunnelConfig, AckStatus

# ✅ Routing helpers — exported from package root
from exectunnel.session import DEFAULT_EXCLUDE_CIDRS, get_default_exclusion_networks

# ✗ Never import from sub-modules directly
from exectunnel.session.session import TunnelSession  # forbidden
from exectunnel.session._dispatcher import RequestDispatcher  # forbidden
```

---

## 4. `TunnelSession` — Design & Architecture

### 4.1 Responsibility

`TunnelSession` is the single public entry point for the tunnel. It:

1. Manages the outer reconnection loop
2. Opens the WebSocket / exec-channel connection
3. Runs the bootstrap sequence via `AgentBootstrapper`
4. Constructs `WsSender` and `RequestDispatcher` per session
5. Starts `FrameReceiver`, `KeepaliveLoop`, `Socks5Server`, and `DnsForwarder` tasks
6. Holds `_tcp_registry`, `_udp_registry`, and `_pending_connects` across reconnects
7. Clears all per-session state on reconnect via `_clear_session_state()`

### 4.2 Public Interface

```python
class TunnelSession:
    def __init__(
            self,
            session_cfg: SessionConfig,
            tun_cfg: TunnelConfig,
    ) -> None: ...

    def set_agent_stats_listener(
            self,
            callback: AgentStatsCallable | None,
    ) -> None: ...

    async def run(self) -> None: ...  # outer reconnect loop — never returns normally
```

There is **no `shutdown()` method**. Shutdown is achieved by cancelling the task running
`run()`.

### 4.3 Per-Session State

| Attribute           | Type                         | Lifetime          | Purpose                                   |
|---------------------|------------------------------|-------------------|-------------------------------------------|
| `_tcp_registry`     | `dict[str, TcpConnection]`   | Across reconnects | Live TCP handlers keyed by `conn_id`      |
| `_udp_registry`     | `dict[str, UdpFlow]`         | Across reconnects | Live UDP flows keyed by `flow_id`         |
| `_pending_connects` | `dict[str, PendingConnect]`  | Across reconnects | In-flight `CONN_OPEN` futures             |
| `_ws_closed`        | `asyncio.Event`              | Per session       | Set by `FrameReceiver` on WebSocket close |
| `_request_tasks`    | `set[asyncio.Task]`          | Per session       | Active per-request tasks for cancellation |
| `_sender`           | `WsSender \| None`           | Per session       | Concurrency-safe frame sender             |
| `_dispatcher`       | `RequestDispatcher \| None`  | Per session       | CONNECT/UDP_ASSOCIATE handler             |
| `_on_agent_stats`   | `AgentStatsCallable \| None` | Across reconnects | Optional STATS snapshot listener          |

All per-session state (except `_on_agent_stats`) is cleared by `_clear_session_state()`
before each reconnect attempt.

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
              ┌────►│        CONNECTING            │
              │     │  websockets.connect(wss_url) │
              │     └──────────────┬───────────────┘
              │                    │ connection established
              │                    ▼
              │     ┌──────────────────────────────┐
              │     │       BOOTSTRAPPING           │
              │     │  AgentBootstrapper.run()      │
              │     │  upload + exec + AGENT_READY  │
              │     └──────────────┬───────────────┘
              │                    │ AGENT_READY received
              │                    ▼
              │     ┌──────────────────────────────┐
              │     │          RUNNING             │
              │     │  WsSender + FrameReceiver    │
              │     │  + KeepaliveLoop + Socks5    │
              │     │  + DnsForwarder (optional)   │
              │     └──────────────┬───────────────┘
              │                    │ task error / WS disconnect
              │                    ▼
              │     ┌──────────────────────────────┐
              │     │         RESETTING            │
              │     │  _clear_session_state()      │
              │     │  cancel _request_tasks       │
              │     └──────────────┬───────────────┘
              │                    │
              │     ┌──────────────▼───────────────┐
              │     │       BACK-OFF WAIT           │
              │     │  exponential delay + jitter  ├──────────────────┐
              │     └──────────────────────────────┘  retry           │
              │                                                        │
              └────────────────────────────────────────────────────────┘

  BootstrapError / CancelledError / attempt >= max_retries
  → propagated immediately → caller handles
```

### 5.2 Back-off Parameters

| Parameter     | Default                                 | Source                                |
|---------------|-----------------------------------------|---------------------------------------|
| Initial delay | `Defaults.WS_RECONNECT_BASE_DELAY_SECS` | `SessionConfig.reconnect_base_delay`  |
| Multiplier    | 2×                                      | hardcoded (`2**attempt`)              |
| Maximum delay | `Defaults.WS_RECONNECT_MAX_DELAY_SECS`  | `SessionConfig.reconnect_max_delay`   |
| Jitter        | 25% of capped delay                     | `_RECONNECT_JITTER = 0.25`            |
| Max retries   | `Defaults.WS_RECONNECT_MAX_RETRIES`     | `SessionConfig.reconnect_max_retries` |

**Exact formula:**

\[
\text{delay} = \min(\text{base\_delay} \times 2^{\text{attempt}},\ \text{max\_delay})
\]

\[
\text{actual\_delay} = \text{delay} + \text{uniform}(0,\ \text{delay} \times 0.25)
\]

A clean session (no error) resets `attempt` to `0`. Each transport error increments
`attempt`. When `attempt >= max_retries`, `ReconnectExhaustedError` is raised.

---

## 6. Bootstrap Sequence

```
Client (session layer)                        Remote pod shell
     │                                              │
     │── websockets.connect(wss_url) ──────────────►│
     │                                              │
     │  AgentBootstrapper.run():                    │
     │  1. send stty raw + disable bracketed-paste  │
     │  2. resolve Python interpreter               │
     │  3. upload/fetch agent payload               │
     │  4. optional syntax check                    │
     │  5. exec python agent.py                     │
     │                                              │── agent starts
     │◄─────────────── AGENT_READY ─────────────────│
     │                                              │
     │  _start_session():                           │
     │  • construct WsSender, RequestDispatcher     │
     │  • sender.start()                            │
     │  • _run_tasks():                             │
     │    – FrameReceiver.run()   (tun-recv-loop)   │
     │    – WsSender._run()       (tun-send-loop)   │
     │    – _accept_loop()        (tun-socks-accept) │
     │    – KeepaliveLoop.run()   (tun-keepalive)   │
     │    – DnsForwarder (optional, async context)  │
     │                                              │
     │  [session is now RUNNING]                    │
```

`AgentBootstrapper` owns the full bootstrap protocol:

* **Terminal setup**: sends
  `stty cs8 -icanon min 1 time 0 -isig -xcase -inpck -opost -echo; printf '\e[?2004l'`
  as the first fenced command to put the PTY into raw mode and disable bracketed-paste.
* **Delivery modes**: `"upload"` (base64 chunks via `printf`) or `"fetch"` (curl/wget
  inside pod).
* **Python or Go agent**: controlled by `TunnelConfig.bootstrap_use_go_agent`.
* **Syntax check**: optional `ast.parse` check before exec, with sentinel file caching.
* **Post-ready buffering**: lines received after `AGENT_READY` but before
  `FrameReceiver` takes over are buffered in `post_ready_lines` and replayed by
  `FrameReceiver.run()`.

---

## 7. Inbound Frame Dispatch

### 7.1 `FrameReceiver` Responsibility

`FrameReceiver` owns the inbound path:

1. Replays `post_ready_lines` buffered during bootstrap.
2. Reads raw WebSocket messages, splits on `\n`, calls `parse_frame()` on each line.
3. `None` (non-frame line) → logged at DEBUG, counted as `session.frames.noise`.
4. `FrameDecodingError` from `parse_frame()` → wrapped in `ConnectionClosedError` (
   protocol violation, session cannot continue).
5. Dispatches `ParsedFrame` to the appropriate per-type callback method.
6. On WebSocket close: sets `_ws_closed` event, then calls `_force_cleanup()`.

### 7.2 Frame Dispatch Table

| `msg_type`    | Direction      | Handler          | Action                                                                                                                                                            |
|---------------|----------------|------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `CONN_ACK`    | Agent → Client | `_on_conn_ack`   | Resolves `PendingConnect.ack_future` with `AckStatus.OK`                                                                                                          |
| `DATA`        | Agent → Client | `_on_data`       | Decodes payload, feeds `TcpConnection` via `try_feed` or `feed_async`                                                                                             |
| `CONN_CLOSE`  | Agent → Client | `_on_conn_close` | Calls `handler.on_remote_closed()`; resolves pending future with `AckStatus.AGENT_CLOSED` if pre-ACK                                                              |
| `UDP_DATA`    | Agent → Client | `_on_udp_data`   | Decodes payload, calls `flow.feed(data)`                                                                                                                          |
| `UDP_CLOSE`   | Agent → Client | `_on_udp_close`  | Calls `flow.on_remote_closed()`                                                                                                                                   |
| `ERROR`       | Agent → Client | `_on_error`      | Session-level: raises `ConnectionClosedError`; per-conn: resolves future with `AckStatus.AGENT_ERROR`, aborts upstream; per-flow: calls `flow.on_remote_closed()` |
| `STATS`       | Agent → Client | `_on_stats`      | Decodes base64url-JSON, invokes `AgentStatsCallable` if registered                                                                                                |
| `AGENT_READY` | —              | raises           | `UnexpectedFrameError` — must never arrive post-bootstrap                                                                                                         |
| `KEEPALIVE`   | —              | raises           | `UnexpectedFrameError` — must never arrive from agent                                                                                                             |
| `CONN_OPEN`   | —              | raises           | `UnexpectedFrameError` — client→agent only                                                                                                                        |
| `UDP_OPEN`    | —              | raises           | `UnexpectedFrameError` — client→agent only                                                                                                                        |

### 7.3 Head-of-Line Blocking Guard

When `ws_send` is wired into `FrameReceiver` (the normal path), `DATA` frames use
`try_feed()` (non-blocking). If the inbound queue is full:

1. An `ERROR` frame is sent to the agent (`control=True`) to signal saturation.
2. `handler.abort_upstream()` stops the upstream task.
3. `handler.on_remote_closed()` wakes the downstream task to drain and exit.
4. The recv loop continues dispatching frames for other multiplexed connections.

This prevents a single slow local consumer from stalling the entire WebSocket
dispatcher.

### 7.4 Dispatch Rule: Unknown `conn_id`

| Frame type              | Unknown `conn_id` action                               |
|-------------------------|--------------------------------------------------------|
| `CONN_ACK`              | Logged at DEBUG, counted as `session.frames.orphaned`  |
| `DATA`                  | Silently dropped, counted as `session.frames.orphaned` |
| `CONN_CLOSE`            | Silently dropped, counted as `session.frames.orphaned` |
| `UDP_DATA`              | Silently dropped, counted as `session.frames.orphaned` |
| `UDP_CLOSE`             | Silently dropped, counted as `session.frames.orphaned` |
| `ERROR` (session-level) | Raises `ConnectionClosedError` regardless              |
| `ERROR` (per-conn/flow) | Logged at DEBUG, counted as `session.frames.orphaned`  |

---

## 8. Payload Decoding Contract

**Rule: All payload decoding happens in the session layer, never in transport.**

`FrameReceiver` calls protocol-layer helpers directly before feeding transport:

```python
# DATA / UDP_DATA frames
data: bytes = decode_binary_payload(frame.payload)
handler.feed(data)  # or try_feed / feed_async

# ERROR frames
message: str = decode_error_payload(frame.payload)

# STATS frames (inline in _on_stats)
raw = base64.urlsafe_b64decode(payload + padding)
snapshot = json.loads(raw.decode("utf-8"))
```

`FrameDecodingError` from any decode call is caught around the `match` block and wrapped
in `ConnectionClosedError` — a corrupt payload from the agent is a protocol violation.

Transport handlers receive **already-decoded `bytes`** via `feed()` / `try_feed()` /
`feed_async()` — they never see raw base64url strings.

---

## 9. Outbound Frame Routing

The session layer calls typed `encode_*_frame()` helpers from `exectunnel.protocol`
directly — there is no intermediate routing module for frame encoding.

### 9.1 `WsSender` — Priority Queue Model

`WsSender` implements `WsSendCallable` and owns two asyncio queues:

| Queue         | Bounded                | Used for                                                                                        |
|---------------|------------------------|-------------------------------------------------------------------------------------------------|
| `_ctrl_queue` | No (unbounded)         | `control=True` frames: `CONN_OPEN`, `CONN_CLOSE`, `UDP_OPEN`, `UDP_CLOSE`, `ERROR`, `KEEPALIVE` |
| `_data_queue` | Yes (`send_queue_cap`) | `must_queue=True` or best-effort `DATA` / `UDP_DATA` frames                                     |

The send loop (`tun-send-loop` task) drains the control queue before the data queue on
every iteration, guaranteeing control frames are delivered ahead of bulk data.

An `asyncio.Event` (`_frame_ready`) is used for idle-wait instead of racing two tasks,
eliminating per-iteration task overhead.

### 9.2 `send()` Semantics

| Call                           | Behaviour                                                                          |
|--------------------------------|------------------------------------------------------------------------------------|
| `send(frame, control=True)`    | `_ctrl_queue.put_nowait()` — never blocks, never drops                             |
| `send(frame, must_queue=True)` | Blocks until enqueued; raises `ConnectionClosedError` if WS closes while waiting   |
| `send(frame)`                  | `_data_queue.put_nowait()` — drops silently if full, increments `_send_drop_count` |

### 9.3 `KeepaliveLoop`

Sends `encode_keepalive_frame()` (a pre-computed constant) at
`SessionConfig.ping_interval` seconds via `send(frame, control=True)`. Uses
`asyncio.timeout` on `ws_closed.wait()` so the loop exits promptly when the WebSocket
closes rather than sleeping a full interval. The agent silently discards `KEEPALIVE`
frames — they exist solely to prevent NAT/proxy idle timeouts.

---

## 10. `RequestDispatcher` — CONNECT and UDP_ASSOCIATE

`RequestDispatcher` is constructed fresh per session and injected with shared registries
by reference. All dispatch methods are coroutines safe to run as independent asyncio
tasks.

### 10.1 CONNECT Flow

```
_accept_loop yields TCPRelay
    │
    ▼
_handle_connect(req)
    │
    ├─ is_host_excluded(host, tun_cfg.exclude)?
    │   └─ Yes → _handle_direct_connect()
    │              asyncio.open_connection(host, port)
    │              send_reply_success()
    │              _pipe(client ↔ remote)
    │
    └─ No  → _handle_tunnel_connect()
               │
               ├─ _acquire_host_gate(host)   ← ref-counted semaphore
               ├─ new_conn_id()
               ├─ TcpConnection(conn_id, reader, writer, ws_send, registry)
               ├─ tcp_registry[conn_id] = handler
               ├─ metrics_gauge_inc("session.active.tcp_connections")
               ├─ pending_connects[conn_id] = PendingConnect(host, port, future)
               │
               ├─ async with _connect_gate, host_gate:
               │   ├─ _pace_connection(host_key)   ← per-host rate limiter
               │   └─ ws_send(encode_conn_open_frame(...), control=True)
               │
               ├─ _await_conn_ack(conn_id, pending, ack_future)
               │   ├─ asyncio.wait({ack_future, ws_closed_task}, timeout=conn_ack_timeout)
               │   ├─ ack_future wins → AckStatus.OK → None, Reply.SUCCESS
               │   ├─ ws_closed_task wins → AckStatus.WS_CLOSED
               │   └─ TimeoutError → AckStatus.TIMEOUT
               │
               ├─ failure? → _teardown_failed_connection() + send_reply_error()
               │
               └─ success → send_reply_success()
                            handler.start()
                            await handler.closed_event.wait()
```

**Semaphore scope**: `_connect_gate` (global) and `host_gate` (per-host) are released
immediately after `CONN_OPEN` is sent. The ACK wait runs outside the semaphore scope so
a slow agent ACK does not block new connections from other hosts.

**`ws_closed_task` reuse**: A single `asyncio.Task` wrapping `ws_closed.wait()` is
created lazily on first use and reused across all `_await_conn_ack` calls. This avoids
per-connection task allocation overhead. It is cancelled by `dispatcher.close()` on
session teardown.

**ACK priority**: When both `ack_future` and `ws_closed_task` resolve in the same
event-loop tick, `ack_future` is checked first so a valid ACK is never discarded due to
a concurrent close signal.

### 10.2 ACK Failure Tracking

`_record_ack_failure()` maintains two sliding windows:

| Window                      | Tracks                         | Threshold field                   |
|-----------------------------|--------------------------------|-----------------------------------|
| `_ack_timeout_window_*`     | `AckStatus.TIMEOUT` events     | `ack_timeout_reconnect_threshold` |
| `_ack_agent_error_window_*` | `AckStatus.AGENT_ERROR` events | `ack_timeout_reconnect_threshold` |

When either window count reaches `ack_timeout_reconnect_threshold` within
`ack_timeout_window_secs`, a reconnect is requested via `_request_reconnect()` (
force-closes the WebSocket). `reset_ack_state()` is called after each successful
bootstrap.

### 10.3 UDP_ASSOCIATE Flow

```
_accept_loop yields TCPRelay (cmd=UDP_ASSOCIATE)
    │
    ▼
_handle_udp_associate(req)
    │
    ├─ send_reply_success(bind_host="127.0.0.1", bind_port=relay.local_port)
    │
    └─ _run_udp_pump(relay, req, active_flows, drain_tasks)
        │
        │  loop:
        ├─ relay.recv() with udp_pump_poll_timeout
        │   ├─ TimeoutError + req.reader.at_eof() → break
        │   └─ (payload, dst_host, dst_port)
        │
        ├─ is_host_excluded(dst_host)?
        │   └─ Yes → _direct_udp_relay(relay, payload, dst_host, dst_port)
        │
        └─ No → active_flows.get((dst_host, dst_port))
                ├─ exists → flow_handler.send_datagram(payload)
                └─ new    → _open_udp_flow(...)
                             new_flow_id()
                             UdpFlow(flow_id, dst_host, dst_port, ws_send, registry)
                             udp_registry[flow_id] = flow_handler
                             flow_handler.open()
                             asyncio.create_task(_drain_udp_flow(...))
```

**Per-session flow cap**: `UDP_ACTIVE_FLOWS_CAP = 256`. When reached, the oldest flow (
insertion order) is evicted before opening a new one.

**Control connection monitoring**: `relay.recv()` is polled with `udp_pump_poll_timeout`
timeout. On each timeout, `req.reader.at_eof()` is checked — when the TCP control
connection closes (RFC 1928 §7), the UDP pump exits.

### 10.4 Per-Host Concurrency Gating

`_HostGateRegistry` maintains ref-counted per-host `asyncio.Semaphore` entries:

* Entries are **pinned** while any coroutine holds the gate — eviction of in-use entries
  would reset the semaphore counter and temporarily exceed the limit.
* Idle entries (`refs == 0`) are pruned when the registry exceeds
  `HOST_SEMAPHORE_CAPACITY = 4096`.
* `_acquire_host_gate()` must always be paired with `_release_host_gate()` in a
  `try/finally` to guarantee refcount balance even on cancellation.

### 10.5 Per-Host Connection Pacing

`_pace_connection()` enforces `connect_pace_interval_secs` between successive
`CONN_OPEN` frames to the same host, with jitter capped at
`min(CONNECT_PACE_JITTER_CAP_SECS, interval / 2.0)`. A per-host `asyncio.Lock` (stored
in `LruDict`) serialises pacing checks for the same host.

---

## 11. `DnsForwarder` — DNS-over-Tunnel

`DnsForwarder` listens on `127.0.0.1:dns_local_port` (UDP) and forwards each DNS query
through the tunnel as a dedicated `UdpFlow`. It is started as an async context manager
inside `_run_tasks()` when `tun_cfg.dns_upstream` is set.

```
Local DNS client
    │  UDP query
    ▼
_DnsDatagramProtocol.datagram_received()
    │
    ▼
DnsForwarder.on_query(data, addr)
    │
    ├─ stopped or inflight >= max_inflight → drop + metric
    │
    └─ asyncio.create_task(_forward_query(data, addr, flow_id))
           │
           ├─ UdpFlow(flow_id, upstream, upstream_port, ws_send, udp_registry)
           ├─ handler.open()
           ├─ handler.send_datagram(query)
           ├─ _recv_response() with query_timeout
           │   ├─ TimeoutError → drop + metric
           │   └─ response bytes
           ├─ transport.sendto(response, client_addr)
           └─ finally: handler.close()  ← always, idempotent
```

Each query opens a fresh `UdpFlow`, sends exactly one datagram, waits for exactly one
response, then closes the flow. `close()` is always called in `finally` — it is
idempotent and skips `UDP_CLOSE` if `open()` never succeeded.

---

## 12. `AgentBootstrapper` — Bootstrap Protocol

### 12.1 Delivery Modes

| Mode       | Mechanism                                                                                         | Requires                   |
|------------|---------------------------------------------------------------------------------------------------|----------------------------|
| `"upload"` | Base64-encode agent, stream via `printf` chunks, decode with `sed \| base64 -d` (Python fallback) | Nothing — works in any pod |
| `"fetch"`  | `curl -fsSL URL -o path \|\| wget -qO path URL` inside pod                                        | Pod outbound HTTPS access  |

### 12.2 Fenced Command Protocol

Every shell command that must complete before the next step uses a **fence marker**:

```
send: <cmd>\n
send: printf '%s\n' EXECTUNNEL_FENCE:<uuid12>\n
read: discard lines until EXECTUNNEL_FENCE:<uuid12> is seen
```

All received lines are passed through `_strip_ansi()` before comparison so VT100
decoration cannot prevent fence matching.

### 12.3 Post-Ready Buffering

The bootstrap read loop may receive protocol frames between `AGENT_READY` and
`FrameReceiver` taking over the WebSocket iterator. These are buffered in:

* `post_ready_lines` — complete lines after `AGENT_READY`
* `pre_ready_carry` — partial (unterminated) chunk at the point of detection

`FrameReceiver.run()` replays `post_ready_lines` before switching to live WebSocket
iteration, and prepends `pre_ready_carry` to its own receive buffer.

---

## 13. `PendingConnect` — ACK Tracking

```python
@dataclass(frozen=True, slots=True)
class PendingConnect:
    host: str
    port: int
    ack_future: asyncio.Future[AckStatus]
```

Lifecycle:

1. `RequestDispatcher` creates `PendingConnect`, stores in `_pending_connects[conn_id]`.
2. `CONN_OPEN` frame sent to agent (`control=True`).
3. `FrameReceiver._on_conn_ack()` resolves `ack_future` with `AckStatus.OK`.
4. `FrameReceiver._on_error()` resolves `ack_future` with `AckStatus.AGENT_ERROR`.
5. `FrameReceiver._on_conn_close()` resolves `ack_future` with `AckStatus.AGENT_CLOSED`.
6. `FrameReceiver._on_data()` pre-ACK path: overflow resolves with
   `AckStatus.PRE_ACK_OVERFLOW`.
7. On session reset: `ack_future.cancel()` for all pending entries.

`_pending_connects` is keyed by `conn_id` (not stored inside `PendingConnect`). The
`conn_id` is the dict key.

---

## 14. `AckStatus` — ACK Result Enum

```python
class AckStatus(StrEnum):
    OK = "ok"
    TIMEOUT = "timeout"
    WS_CLOSED = "ws_closed"
    AGENT_ERROR = "agent_error"
    AGENT_CLOSED = "agent_closed"
    WS_SEND_TIMEOUT = "ws_send_timeout"
    PRE_ACK_OVERFLOW = "pre_ack_overflow"
    LIBRARY_ERROR = "library_error"
    UNEXPECTED_ERROR = "unexpected_error"
```

Each member is used as the resolved value of `asyncio.Future[AckStatus]` and as a
`status` label in metrics. Always match on the enum member, never on the raw string
value.

---

## 15. Session Reset — `_clear_session_state()`

Called before every reconnect attempt. Sequence:

```
1. dispatcher.close()
   └─ cancels _ws_closed_task sentinel

2. await sender.stop()
   └─ drains send queues, cancels send-loop task

3. cancel + gather all _request_tasks

4. For each TcpConnection in _tcp_registry:
     if conn.is_started  →  conn.abort()
     else                →  await conn.close_unstarted()
   _tcp_registry.clear()

5. For each PendingConnect in _pending_connects:
     if not pending.ack_future.done()  →  pending.ack_future.cancel()
   _pending_connects.clear()

6. For each UdpFlow in _udp_registry:
     flow.on_remote_closed()   ← sync, not async
   _udp_registry.clear()

7. _emit_registry_gauges()   ← zero all registry gauges
```

**Why `on_remote_closed()` not `await flow.close()`**: `on_remote_closed()` is
synchronous and sufficient for cleanup during session reset — it sets the closed event
and evicts from the registry. `close()` would additionally send a `UDP_CLOSE` frame over
a WebSocket that may already be dead.

**Why `abort()` not `close()`**: `TcpConnection` has no `close()` method. Started
connections must be aborted (hard cancel of both tasks); unstarted connections use
`close_unstarted()` to release the writer without starting tasks.

---

## 16. Concurrency Model

### 16.1 Task Structure per Session

```
asyncio event loop
│
├── TunnelSession.run()                      ← outer reconnect loop (caller's task)
│     │
│     └── _start_session()
│           │
│           ├── AgentBootstrapper.run()      ← sequential, no extra task
│           │
│           └── _run_tasks()
│                 │
│                 ├── tun-recv-loop          ← FrameReceiver.run()
│                 ├── tun-send-loop          ← WsSender._run()
│                 ├── tun-socks-accept       ← _accept_loop(socks)
│                 ├── tun-keepalive          ← KeepaliveLoop.run()
│                 │
│                 ├── (optional) DnsForwarder
│                 │     └── dns-fwd-*        ← per-query tasks
│                 │
│                 └── Socks5Server (asyncio.Server — internal tasks)
│                       │
│                       └── req-CONNECT-*   ← per-request tasks (_request_tasks)
│                       └── req-UDP_ASSOCIATE-*
│                             │
│                             └── TcpConnection (transport layer)
│                                   ├── tcp-up-{conn_id}
│                                   └── tcp-down-{conn_id}
│
└── ws-closed-sentinel                       ← shared, created lazily by dispatcher
```

### 16.2 Synchronisation Points

| Mechanism                      | Purpose                                                                      |
|--------------------------------|------------------------------------------------------------------------------|
| `PendingConnect.ack_future`    | `_await_conn_ack` waits for agent ACK before starting data relay             |
| `TcpConnection.closed_event`   | `_handle_tunnel_connect` awaits full TCP cleanup after `handler.start()`     |
| `asyncio.Event` (`_ws_closed`) | Set by `FrameReceiver` on WS close; read by `WsSender` and `_await_conn_ack` |
| `_request_tasks` set           | Bulk cancel on reconnect or shutdown                                         |
| `_connect_gate` semaphore      | Global cap on concurrent in-flight `CONN_OPEN` frames                        |
| `_HostGateRegistry` semaphores | Per-host cap on concurrent in-flight `CONN_OPEN` frames                      |

### 16.3 Thread Safety

The session layer is **single-threaded asyncio**. No locks, no thread pools, no
`run_in_executor` except inside `_udp_socket.py` for `getaddrinfo` on domain names. All
state mutations happen on the event loop thread.

---

## 17. Error Taxonomy

### 17.1 Errors the Session Layer Raises to the Caller

| Exception                   | Trigger                                           | Retried by `run()`?         |
|-----------------------------|---------------------------------------------------|-----------------------------|
| `BootstrapError`            | Any bootstrap failure not covered below           | No — propagated immediately |
| `AgentReadyTimeoutError`    | `AGENT_READY` not received within `ready_timeout` | No — propagated immediately |
| `AgentSyntaxError`          | Remote Python reported `SyntaxError`              | No — propagated immediately |
| `AgentVersionMismatchError` | Remote agent version incompatible                 | No — propagated immediately |
| `ReconnectExhaustedError`   | All reconnect attempts consumed                   | — (terminal)                |
| `asyncio.CancelledError`    | Caller cancelled `run()`                          | — (always re-raised)        |

### 17.2 Errors the Session Layer Handles Internally (Trigger Reconnect)

| Source                 | Exception                      | Action                                                            |
|------------------------|--------------------------------|-------------------------------------------------------------------|
| `WsSender._run()`      | `WebSocketSendTimeoutError`    | Sets `_ws_closed`, propagates to `_run_tasks`, triggers reconnect |
| `WsSender._run()`      | `ConnectionClosedError`        | Sets `_ws_closed`, propagates to `_run_tasks`, triggers reconnect |
| `FrameReceiver.run()`  | `ConnectionClosedError`        | Propagates to `_run_tasks`, triggers reconnect                    |
| `FrameReceiver.run()`  | `UnexpectedFrameError`         | Propagates to `_run_tasks`, triggers reconnect                    |
| `websockets.connect()` | `OSError` / `ConnectionClosed` | Caught in `run()`, triggers reconnect                             |
| `websockets.connect()` | `TimeoutError`                 | Caught in `run()`, triggers reconnect                             |
| `websockets.connect()` | `TransportError`               | Caught in `run()`, triggers reconnect                             |

### 17.3 Errors the Session Layer Handles Internally (Per-Connection)

| Source                    | Exception                                    | Action                                                              |
|---------------------------|----------------------------------------------|---------------------------------------------------------------------|
| `parse_frame()`           | `FrameDecodingError`                         | Wrapped in `ConnectionClosedError` → session reconnect              |
| `decode_binary_payload()` | `FrameDecodingError`                         | Wrapped in `ConnectionClosedError` → session reconnect              |
| `handler.feed()`          | `TransportError` (`pre_ack_buffer_overflow`) | Resolves `ack_future` with `AckStatus.PRE_ACK_OVERFLOW`             |
| `handler.try_feed()`      | Returns `False`                              | Sends `ERROR` frame, aborts connection, recv loop continues         |
| Agent `ERROR` frame       | Session-level (`SESSION_CONN_ID`)            | Raises `ConnectionClosedError` → session reconnect                  |
| Agent `ERROR` frame       | Per-connection                               | Resolves `ack_future` with `AckStatus.AGENT_ERROR`, aborts upstream |
| Agent `ERROR` frame       | Per-flow                                     | Calls `flow.on_remote_closed()`                                     |

### 17.4 Exception Chaining Rule

All session-layer exceptions follow the project-wide rule:

* `raise SomeError(...) from exc` — **only inside an `except` block** with a live `exc`.
* `raise SomeError(...)` — when there is no causal exception to chain.
* Never `raise SomeError(...) from None` unless explicitly suppressing a confusing
  chain.

---

## 18. Observability Surface

### 18.1 Metrics

#### Session-Level Metrics

| Metric                              | Type      | Labels   | Meaning                                                           |
|-------------------------------------|-----------|----------|-------------------------------------------------------------------|
| `session.connect.attempt`           | counter   | —        | WebSocket connection attempts                                     |
| `session.connect.ok`                | counter   | —        | Successful WebSocket connections                                  |
| `session.connect.error`             | counter   | `error`  | Failed connection attempts by error type                          |
| `session.reconnect`                 | counter   | `reason` | Reconnect attempts by reason tag                                  |
| `session.reconnect.exhausted`       | counter   | —        | `ReconnectExhaustedError` raised                                  |
| `session.reconnect.delay_sec`       | histogram | —        | Back-off delay before reconnect                                   |
| `session.duration_sec`              | histogram | —        | Per-session wall-clock duration                                   |
| `session.serve.started`             | counter   | —        | Session entered RUNNING state                                     |
| `session.serve.stopped`             | counter   | —        | Session exited RUNNING state                                      |
| `session.task.error`                | counter   | `task`   | Task failure by task name                                         |
| `session.request.error`             | counter   | `error`  | Request task failure by exception type                            |
| `session.request_tasks`             | gauge     | —        | Active per-request task count                                     |
| `session.registry.tcp`              | gauge     | —        | `_tcp_registry` size                                              |
| `session.registry.udp`              | gauge     | —        | `_udp_registry` size                                              |
| `session.registry.pending_connects` | gauge     | —        | `_pending_connects` size                                          |
| `session.pending_connects`          | gauge     | —        | In-flight `CONN_OPEN` count (dispatcher)                          |
| `session.active.tcp_connections`    | gauge     | —        | Incremented on registration, decremented by transport on eviction |
| `session.active.udp_flows`          | gauge     | —        | Incremented on registration, decremented by transport on eviction |
| `session.cleanup.tcp`               | counter   | —        | TCP connections cleaned on session reset                          |
| `session.cleanup.pending`           | counter   | —        | Pending connects cancelled on session reset                       |
| `session.cleanup.udp`               | counter   | —        | UDP flows cleaned on session reset                                |

#### Frame Metrics

| Metric                                | Type      | Labels       | Meaning                                         |
|---------------------------------------|-----------|--------------|-------------------------------------------------|
| `session.frames.inbound`              | counter   | `msg_type`   | Inbound frames dispatched                       |
| `session.frames.noise`                | counter   | —            | Non-frame lines discarded                       |
| `session.frames.decode_error`         | counter   | —            | `FrameDecodingError` from `parse_frame()`       |
| `session.frames.payload_decode_error` | counter   | —            | `FrameDecodingError` from payload decode        |
| `session.frames.no_conn_id`           | counter   | —            | Frames with unexpected `None` conn_id           |
| `session.frames.orphaned`             | counter   | `frame_type` | Frames for unknown conn/flow IDs                |
| `session.frames.bytes.in`             | counter   | —            | Raw bytes received (DATA + UDP_DATA)            |
| `session.frames.outbound`             | counter   | `msg_type`   | Outbound frames sent                            |
| `session.frames.outbound.bytes`       | counter   | —            | Outbound frame bytes                            |
| `session.frames.outbound.drop`        | counter   | —            | Frames dropped due to full data queue           |
| `session.frames.outbound.timeout`     | counter   | —            | Send timeout events                             |
| `session.frames.outbound.ws_closed`   | counter   | —            | Send failures due to closed WS                  |
| `session.frames.stats`                | counter   | —            | `STATS` frames received                         |
| `session.frames.stats_decode_error`   | counter   | —            | `STATS` payload decode failures                 |
| `session.frames.error.session`        | counter   | —            | Session-level `ERROR` frames                    |
| `session.frames.error.conn`           | counter   | —            | Per-connection `ERROR` frames                   |
| `session.frames.error.flow`           | counter   | —            | Per-flow `ERROR` frames                         |
| `session.recv.hol_abort`              | counter   | —            | Connections aborted for HoL blocking prevention |
| `session.recv.cleanup.tcp`            | counter   | —            | TCP handlers force-cleaned on WS close          |
| `session.recv.cleanup.udp`            | counter   | —            | UDP flows force-cleaned on WS close             |
| `session.recv.trailing_partial_frame` | counter   | —            | Residual data dropped on WS close               |
| `session.recv.residual_bytes`         | histogram | —            | Residual byte count on WS close                 |

#### Tunnel / Transport Metrics

| Metric                                | Type      | Labels                              | Meaning                            |
|---------------------------------------|-----------|-------------------------------------|------------------------------------|
| `tunnel.conn_open`                    | counter   | `conn_id`, `host`, `port`           | `CONN_OPEN` frames sent            |
| `tunnel.conn_ack.ok`                  | counter   | `conn_id`, `host`, `port`           | Successful ACKs received           |
| `tunnel.conn_ack.timeout`             | counter   | `conn_id`, `host`, `port`           | ACK timeouts                       |
| `tunnel.conn_ack.failed`              | counter   | `conn_id`, `reason`, `host`, `port` | ACK failures by reason             |
| `tunnel.conn_ack.wait_sec`            | histogram | `status`, `host`                    | ACK wait duration                  |
| `tunnel.conn_ack.reconnect_triggered` | counter   | —                                   | Reconnects triggered by ACK health |
| `tunnel.conn.completed`               | counter   | `conn_id`, `host`, `port`           | Connections that ran to completion |
| `tunnel.conn.error`                   | counter   | `conn_id`, `host`, `port`, `error`  | Connection errors by type          |
| `tunnel.connect.fail_by_host`         | counter   | `host`, `reason`                    | Per-host connect failures          |
| `tunnel.pre_ack_buffer.overflow`      | counter   | —                                   | Pre-ACK buffer overflow events     |
| `tunnel.inbound_queue.drop`           | counter   | `error`                             | Inbound queue drop events          |
| `socks.reply`                         | counter   | `code`                              | SOCKS5 reply codes sent            |

#### Send Queue Metrics

| Metric                           | Type    | Labels | Meaning                         |
|----------------------------------|---------|--------|---------------------------------|
| `session.send.queue.data`        | gauge   | —      | Data queue depth                |
| `session.send.queue.ctrl`        | gauge   | —      | Control queue depth             |
| `session.send.queue.race_closed` | counter | —      | `must_queue` calls on closed WS |
| `session.send.unexpected_exit`   | counter | —      | Send loop unexpected exits      |
| `session.keepalive.sent`         | counter | —      | KEEPALIVE frames sent           |

#### Bootstrap Metrics

| Metric                             | Type      | Labels | Meaning                            |
|------------------------------------|-----------|--------|------------------------------------|
| `bootstrap.started`                | counter   | —      | Bootstrap attempts                 |
| `bootstrap.ok`                     | counter   | —      | Successful bootstraps              |
| `bootstrap.timeout`                | counter   | —      | `AgentReadyTimeoutError` events    |
| `bootstrap.stty_done`              | counter   | —      | Terminal raw-mode setup complete   |
| `bootstrap.exec_started`           | counter   | —      | Agent exec command sent            |
| `bootstrap.exec_done`              | counter   | —      | `AGENT_READY` received             |
| `bootstrap.duration_seconds`       | histogram | —      | End-to-end bootstrap duration      |
| `bootstrap.syntax_started`         | counter   | —      | Syntax check attempts              |
| `bootstrap.syntax_done`            | counter   | —      | Syntax checks passed               |
| `bootstrap.syntax_skipped`         | counter   | —      | Syntax checks skipped              |
| `bootstrap.syntax_cache_hit`       | counter   | —      | Sentinel file found, check skipped |
| `bootstrap.{label}.chunks`         | histogram | —      | Upload chunk count                 |
| `bootstrap.{label}.upload_started` | counter   | —      | Upload started                     |
| `bootstrap.{label}.upload_done`    | counter   | —      | Upload complete                    |
| `bootstrap.{label}.decode_done`    | counter   | —      | Base64 decode complete             |
| `bootstrap.{label}.fetch_started`  | counter   | —      | Fetch started                      |
| `bootstrap.{label}.fetch_done`     | counter   | —      | Fetch complete                     |
| `bootstrap.{label}.skip_delivery`  | counter   | —      | Delivery skipped (file present)    |

#### DNS Metrics

| Metric                      | Type      | Labels   | Meaning                                     |
|-----------------------------|-----------|----------|---------------------------------------------|
| `dns.forwarder.started`     | counter   | —        | Forwarder bound and ready                   |
| `dns.forwarder.stopped`     | counter   | —        | Forwarder stopped                           |
| `dns.forwarder.inflight`    | gauge     | —        | In-flight query count                       |
| `dns.forwarder.error`       | counter   | `error`  | Forwarder-level errors                      |
| `dns.query.received`        | counter   | —        | Queries received from local clients         |
| `dns.query.ok`              | counter   | —        | Queries successfully answered               |
| `dns.query.timeout`         | counter   | —        | Queries that timed out                      |
| `dns.query.drop`            | counter   | `reason` | Queries dropped (`saturated`, `after_stop`) |
| `dns.query.error`           | counter   | `error`  | Query errors by type                        |
| `dns.query.duration_sec`    | histogram | —        | Per-query end-to-end duration               |
| `dns.query.bytes.in`        | histogram | —        | Query size in bytes                         |
| `dns.query.bytes.out`       | histogram | —        | Response size in bytes                      |
| `dns.query.bytes.in.total`  | counter   | —        | Total query bytes                           |
| `dns.query.bytes.out.total` | counter   | —        | Total response bytes                        |

### 18.2 Spans

| Span                     | Wraps                                           |
|--------------------------|-------------------------------------------------|
| `session.run`            | Entire outer reconnect loop                     |
| `session.start`          | `_start_session()`                              |
| `session.bootstrap`      | `AgentBootstrapper.run()`                       |
| `session.serve`          | `asyncio.wait(all_tasks)` inside `_run_tasks()` |
| `session.recv_loop`      | `FrameReceiver.run()`                           |
| `session.send_loop`      | `WsSender._run()`                               |
| `session.keepalive_loop` | `KeepaliveLoop.run()`                           |
| `session.handle_frame`   | Per-frame dispatch in `_dispatch_frame()`       |
| `socks.connect`          | `_handle_connect()`                             |
| `socks.connect.direct`   | `_handle_direct_connect()`                      |
| `socks.udp_associate`    | `_handle_udp_associate()`                       |
| `tunnel.conn_open`       | `_handle_tunnel_connect()` outer                |
| `tunnel.conn_ack.wait`   | `_await_conn_ack()`                             |
| `dns.forward`            | `DnsForwarder._forward_query()`                 |

### 18.3 Log Levels

| Level     | Used For                                                                                                              |
|-----------|-----------------------------------------------------------------------------------------------------------------------|
| `DEBUG`   | Non-frame lines discarded, orphaned frames, normal lifecycle transitions, direct connect failures, pre-ACK feed drops |
| `INFO`    | Agent ready, DNS forwarder bound, session reset summary, UDP ASSOCIATE local port                                     |
| `WARNING` | ACK failures, send queue drops, HoL abort, UDP flow errors, DNS saturation                                            |
| `ERROR`   | Unexpected task failures, session-level `ERROR` frames, unrecoverable errors                                          |

---

## 19. Configuration Reference

### `SessionConfig` Fields

| Field                   | Type                     | Default                                 | Description                                                  |
|-------------------------|--------------------------|-----------------------------------------|--------------------------------------------------------------|
| `wss_url`               | `str`                    | *(required)*                            | WebSocket URL of the Kubernetes exec endpoint                |
| `ws_headers`            | `dict[str, str]`         | `{}`                                    | Additional HTTP headers on the WebSocket upgrade             |
| `ssl_context_override`  | `ssl.SSLContext \| None` | `None`                                  | Explicit SSL context; `None` defers to `websockets` defaults |
| `version`               | `str`                    | `"1.0"`                                 | Client version string for agent compatibility check          |
| `reconnect_max_retries` | `int`                    | `Defaults.WS_RECONNECT_MAX_RETRIES`     | Max reconnect attempts                                       |
| `reconnect_base_delay`  | `float`                  | `Defaults.WS_RECONNECT_BASE_DELAY_SECS` | Initial back-off delay (s)                                   |
| `reconnect_max_delay`   | `float`                  | `Defaults.WS_RECONNECT_MAX_DELAY_SECS`  | Maximum back-off delay (s)                                   |
| `ping_interval`         | `float`                  | `Defaults.WS_PING_INTERVAL_SECS`        | Seconds between KEEPALIVE frames                             |
| `send_timeout`          | `float`                  | `Defaults.WS_SEND_TIMEOUT_SECS`         | Max seconds per WebSocket frame send                         |
| `send_queue_cap`        | `int`                    | `Defaults.WS_SEND_QUEUE_CAP`            | Bounded data queue capacity                                  |

### `TunnelConfig` Fields

| Field                             | Type                               | Default                                    | Description                                    |
|-----------------------------------|------------------------------------|--------------------------------------------|------------------------------------------------|
| `socks_host`                      | `str`                              | `Defaults.SOCKS_DEFAULT_HOST`              | SOCKS5 bind address                            |
| `socks_port`                      | `int`                              | `Defaults.SOCKS_DEFAULT_PORT`              | SOCKS5 listen port                             |
| `dns_upstream`                    | `str \| None`                      | `None`                                     | Upstream DNS IP; `None` disables DNS forwarder |
| `dns_local_port`                  | `int`                              | `Defaults.DNS_LOCAL_PORT`                  | Local UDP port for DNS relay                   |
| `ready_timeout`                   | `float`                            | `Defaults.READY_TIMEOUT_SECS`              | Max seconds to wait for `AGENT_READY`          |
| `conn_ack_timeout`                | `float`                            | `Defaults.CONN_ACK_TIMEOUT_SECS`           | Max seconds to wait for `CONN_ACK`             |
| `exclude`                         | `list[IPv4Network \| IPv6Network]` | RFC 1918 + loopback + IPv6 private         | CIDRs that bypass the tunnel                   |
| `ack_timeout_warn_every`          | `int`                              | `Defaults.ACK_TIMEOUT_WARN_EVERY`          | Log warning every N ACK timeouts               |
| `ack_timeout_window_secs`         | `float`                            | `Defaults.ACK_TIMEOUT_WINDOW_SECS`         | Sliding window for reconnect trigger           |
| `ack_timeout_reconnect_threshold` | `int`                              | `Defaults.ACK_TIMEOUT_RECONNECT_THRESHOLD` | Failures within window to force reconnect      |
| `connect_max_pending`             | `int`                              | `Defaults.CONNECT_MAX_PENDING`             | Global in-flight `CONN_OPEN` cap               |
| `connect_max_pending_per_host`    | `int`                              | `Defaults.CONNECT_MAX_PENDING_PER_HOST`    | Per-host in-flight `CONN_OPEN` cap             |
| `pre_ack_buffer_cap_bytes`        | `int`                              | `Defaults.PRE_ACK_BUFFER_CAP_BYTES`        | Pre-ACK receive buffer cap per connection      |
| `connect_pace_interval_secs`      | `float`                            | `Defaults.CONNECT_PACE_INTERVAL_SECS`      | Min seconds between `CONN_OPEN` to same host   |
| `bootstrap_delivery`              | `Literal["upload", "fetch"]`       | `"fetch"`                                  | Agent delivery mode                            |
| `bootstrap_fetch_url`             | `str`                              | `Defaults.BOOTSTRAP_FETCH_AGENT_URL`       | URL for fetch delivery                         |
| `bootstrap_skip_if_present`       | `bool`                             | `False`                                    | Skip delivery if agent file exists             |
| `bootstrap_syntax_check`          | `bool`                             | `False`                                    | Run `ast.parse` before exec                    |
| `bootstrap_agent_path`            | `str`                              | `Defaults.BOOTSTRAP_AGENT_PATH`            | Agent script path on pod                       |
| `bootstrap_syntax_ok_sentinel`    | `str`                              | `Defaults.BOOTSTRAP_SYNTAX_OK_SENTINEL`    | Sentinel file path for syntax check cache      |
| `bootstrap_use_go_agent`          | `bool`                             | `False`                                    | Upload and run Go binary instead of Python     |
| `bootstrap_go_agent_path`         | `str`                              | `Defaults.BOOTSTRAP_GO_AGENT_PATH`         | Go binary path on pod                          |
| `socks_handshake_timeout`         | `float`                            | `Defaults.HANDSHAKE_TIMEOUT_SECS`          | Max seconds per SOCKS5 handshake               |
| `socks_request_queue_cap`         | `int`                              | `Defaults.SOCKS_REQUEST_QUEUE_CAP`         | Completed-handshake queue capacity             |
| `socks_queue_put_timeout`         | `float`                            | `Defaults.SOCKS_QUEUE_PUT_TIMEOUT_SECS`    | Max seconds to enqueue a handshake             |
| `udp_relay_queue_cap`             | `int`                              | `Defaults.UDP_RELAY_QUEUE_CAP`             | Per-relay inbound datagram queue depth         |
| `udp_drop_warn_every`             | `int`                              | `Defaults.UDP_WARN_EVERY`                  | Log warning every N UDP queue drops            |
| `udp_pump_poll_timeout`           | `float`                            | `Defaults.UDP_PUMP_POLL_TIMEOUT_SECS`      | UDP pump poll interval (s)                     |
| `udp_direct_recv_timeout`         | `float`                            | `Defaults.UDP_DIRECT_RECV_TIMEOUT_SECS`    | Direct UDP response timeout (s)                |
| `dns_max_inflight`                | `int`                              | `Defaults.DNS_MAX_INFLIGHT`                | Max concurrent DNS queries                     |
| `dns_upstream_port`               | `int`                              | `Defaults.DNS_UPSTREAM_PORT`               | Upstream DNS server port                       |
| `dns_query_timeout`               | `float`                            | `Defaults.DNS_QUERY_TIMEOUT_SECS`          | End-to-end DNS query timeout (s)               |

### Session-Level Constants (`_constants.py`)

| Constant                         | Value                                               | Description                                                  |
|----------------------------------|-----------------------------------------------------|--------------------------------------------------------------|
| `FENCE_PREFIX`                   | `"EXECTUNNEL_FENCE"`                                | Bootstrap fence marker prefix                                |
| `MARKER_YES`                     | `"EXECTUNNEL_EXISTS:1"`                             | File-existence probe positive response                       |
| `MARKER_NO`                      | `"EXECTUNNEL_EXISTS:0"`                             | File-existence probe negative response                       |
| `VALID_DELIVERY_MODES`           | `frozenset({"upload", "fetch"})`                    | Accepted bootstrap delivery modes                            |
| `FENCE_TIMEOUT_SECS`             | `30.0`                                              | Max seconds to wait for a fence marker                       |
| `MIN_FENCE_TIMEOUT_SECS`         | `2.0`                                               | Minimum fence timeout for short probes                       |
| `MAX_STASH_LINES`                | `256`                                               | Bootstrap stash deque capacity                               |
| `PYTHON_CANDIDATES`              | `("python3.13", "python3.12", "python3", "python")` | Python interpreter probe order                               |
| `HOST_SEMAPHORE_CAPACITY`        | `4096`                                              | Max per-host semaphore entries                               |
| `UDP_ACTIVE_FLOWS_CAP`           | `256`                                               | Max simultaneous UDP flows per session                       |
| `PIPE_WRITER_CLOSE_TIMEOUT_SECS` | `5.0`                                               | `wait_closed()` timeout for direct pipe                      |
| `DIRECT_CONNECT_TIMEOUT_SECS`    | `15.0`                                              | Direct TCP connection timeout                                |
| `UNEXPECTED_NO_CONN_ID_FRAMES`   | `frozenset({"AGENT_READY", "KEEPALIVE"})`           | Frame types that raise `UnexpectedFrameError` post-bootstrap |
| `WS_CLOSE_CODE_PROTOCOL_ERROR`   | `1002`                                              | WebSocket close code for protocol errors                     |
| `WS_CLOSE_CODE_INTERNAL_ERROR`   | `1011`                                              | WebSocket close code for agent internal errors               |
| `STOP_GRACE_TIMEOUT_SECS`        | `5.0`                                               | Grace period for send loop drain                             |
| `UPLOAD_PROGRESS_LOG_INTERVAL`   | `50`                                                | Log progress every N upload chunks                           |

---

## 20. Invariants Every Caller Must Preserve

```
 1. Never call session.run() more than once concurrently on the same instance.
    Construct a new TunnelSession for each top-level invocation.

 2. Never call session.shutdown() — it does not exist.
    Shutdown is achieved by cancelling the task running run().

 3. Never mutate _tcp_registry, _udp_registry, or _pending_connects
    from outside the session layer.

 4. Never store SESSION_CONN_ID as a registry key.
    It is reserved for session-level ERROR frames only.

 5. Never call parse_frame() or decode_binary_payload() in transport or proxy
    — these are session-layer responsibilities.

 6. Always inject the same ws_send callable (WsSender.send) into all handlers
    within one session.

 7. Never call handler.start() before the corresponding ack_future resolves
    with AckStatus.OK.

 8. Always cancel ack_future (not the PendingConnect itself) on session reset.
    pending.ack_future.cancel() — not del _pending_connects[conn_id] alone.

 9. Never call conn.close() — use abort() for started connections,
    close_unstarted() for unstarted connections.

10. Never call flow.close() during session reset — use flow.on_remote_closed()
    (sync) to avoid sending UDP_CLOSE over a dead WebSocket.

11. Never suppress asyncio.CancelledError — always re-raise it.

12. Always use asyncio.create_task(handle(req)) in the accept loop —
    never await handle(req) directly inside async for.

13. The AgentStatsCallable must not block or raise — exceptions are logged
    and suppressed by FrameReceiver. Register it before calling run().

14. set_agent_stats_listener() takes effect on the next WebSocket (re)connect —
    FrameReceiver captures the callback at construction time.
```

---

## 21. What This Layer Explicitly Does Not Do

| Concern                           | Why Not Here                                    | Where It Lives            |
|-----------------------------------|-------------------------------------------------|---------------------------|
| Frame wire encoding details       | Session calls typed `encode_*_frame()` helpers  | `protocol`                |
| TCP stream I/O                    | Session feeds decoded bytes to handlers         | `transport.TcpConnection` |
| UDP datagram I/O                  | Session feeds decoded bytes to flows            | `transport.UdpFlow`       |
| SOCKS5 wire protocol              | Session is SOCKS5-agnostic                      | `proxy`                   |
| DNS resolution of CONNECT targets | Agent resolves remotely                         | agent binary              |
| TLS / mTLS                        | Handled by Kubernetes API client                | above session             |
| Agent-side socket management      | Agent is a separate binary                      | agent binary              |
| Observability export              | Session emits events; export is separate        | `observability`           |
| Frame length enforcement          | Enforced by `protocol._encode_frame`            | `protocol`                |
| ID generation                     | Session calls `new_conn_id()` / `new_flow_id()` | `protocol.ids`            |

---

## 22. Quick Reference Card

```
┌─────────────────────────────────────────────────────────────────────┐
│  SESSION LAYER QUICK REFERENCE                                      │
├─────────────────────────────────────────────────────────────────────┤
│  Entry point                                                        │
│    session = TunnelSession(session_cfg, tun_cfg)                    │
│    session.set_agent_stats_listener(cb)  ← optional                 │
│    await session.run()                   ← blocks until cancelled   │
│    task.cancel() + await task            ← shutdown                 │
│                                                                     │
│  Inbound path (FrameReceiver)                                       │
│    raw line → parse_frame() → ParsedFrame | None                   │
│    CONN_ACK  → ack_future.set_result(AckStatus.OK)                  │
│    DATA      → decode_binary_payload() → handler.try_feed(bytes)    │
│    CONN_CLOSE → handler.on_remote_closed()                          │
│    UDP_DATA  → decode_binary_payload() → flow.feed(bytes)           │
│    UDP_CLOSE → flow.on_remote_closed()                              │
│    ERROR     → session: raise ConnectionClosedError                 │
│              → conn: ack_future.set_result(AGENT_ERROR)             │
│              → flow: flow.on_remote_closed()                        │
│    STATS     → decode base64url-JSON → AgentStatsCallable           │
│    AGENT_READY / KEEPALIVE → UnexpectedFrameError                   │
│    CONN_OPEN / UDP_OPEN    → UnexpectedFrameError                   │
│                                                                     │
│  Outbound path (WsSender)                                           │
│    ws_send(frame, control=True)   ← CONN_OPEN, CONN_CLOSE, UDP_*   │
│    ws_send(frame, must_queue=True) ← DATA (backpressure)            │
│    ws_send(frame)                 ← best-effort drop on full queue  │
│                                                                     │
│  TCP connection lifecycle (session perspective)                     │
│    new_conn_id() → TcpConnection(conn_id, reader, writer, ...)      │
│    tcp_registry[conn_id] = handler                                  │
│    ws_send(encode_conn_open_frame(...), control=True)               │
│    await _await_conn_ack(...)    ← races ack_future vs ws_closed    │
│    handler.start()               ← after AckStatus.OK              │
│    await handler.closed_event.wait()                                │
│    reset → handler.abort() or await handler.close_unstarted()       │
│                                                                     │
│  UDP flow lifecycle (session perspective)                           │
│    new_flow_id() → UdpFlow(flow_id, host, port, ws_send, registry)  │
│    udp_registry[flow_id] = flow_handler                             │
│    await flow_handler.open()     ← sends UDP_OPEN (control=True)    │
│    flow_handler.feed(decoded_bytes)                                 │
│    flow_handler.on_remote_closed()  ← on UDP_CLOSE / ERROR         │
│    reset → flow.on_remote_closed()  ← sync, no UDP_CLOSE sent      │
│                                                                     │
│  PendingConnect                                                     │
│    PendingConnect(host, port, ack_future=loop.create_future())      │
│    _pending_connects[conn_id] = pending                             │
│    await asyncio.wait({ack_future, ws_closed_task}, ...)            │
│    reset → pending.ack_future.cancel()                              │
│                                                                     │
│  Never do                                                           │
│    ✗ session.shutdown() — does not exist; cancel the task           │
│    ✗ parse_frame() in transport or proxy — session layer only       │
│    ✗ decode_binary_payload() in transport — session layer only      │
│    ✗ conn.close() — use abort() or close_unstarted()                │
│    ✗ flow.close() on session reset — use on_remote_closed()         │
│    ✗ pending.ack_future.set_result() from outside session           │
│    ✗ mutate _tcp_registry / _udp_registry from outside session      │
│    ✗ store SESSION_CONN_ID as a registry key                        │
│    ✗ start TcpConnection before ack_future resolves AckStatus.OK    │
│    ✗ suppress asyncio.CancelledError                                │
│    ✗ await handle(req) inside async for — always create_task()      │
└─────────────────────────────────────────────────────────────────────┘
```
