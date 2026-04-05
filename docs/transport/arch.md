# ExecTunnel Transport Package — Architectural Document

```
exectunnel/transport/  |  arch-doc v1.0  |  Python 3.13+
```

---

## 1. Purpose & Scope

The `exectunnel.transport` package is the **frame I/O and connection lifecycle layer** of the ExecTunnel stack. It owns the boundary between the raw WebSocket send callable (injected from above by the `session` layer) and the local OS socket (TCP stream or UDP datagram socket managed by the `proxy` layer below).

This document covers:

* What the transport layer is and is not responsible for
* How it fits into the full tunnel stack
* Internal package structure and module responsibilities
* The two concrete handler designs (`TcpConnection`, `UdpFlow`) in full detail
* All data flows, lifecycle state machines, and concurrency models
* The shared type system and validation contract
* Failure modes, error taxonomy, and backpressure strategy
* Observability surface (metrics, spans, structured logs)
* Extension points and invariants every caller must preserve

---

## 2. Position in the Stack

```
┌─────────────────────────────────────────────────────────────────────┐
│  session layer                                                      │
│  ─────────────────────────────────────────────────────────────────  │
│  • Owns the WebSocket / exec channel lifecycle                      │
│  • Runs the inbound frame recv_loop                                 │
│  • Calls parse_frame() on every inbound line                        │
│  • Dispatches decoded payloads to transport handlers                │
│  • Wires ws_send callable into each handler at construction         │
│  • Holds TcpRegistry / UdpRegistry                                  │
└───────────────────────────┬─────────────────────────────────────────┘
                            │  WsSendCallable (injected downward)
                            │  feed() / feed_async() (called downward)
                            │  on_remote_closed() (called downward)
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  transport layer  ◄── THIS DOCUMENT                                 │
│  ─────────────────────────────────────────────────────────────────  │
│  TcpConnection                    UdpFlow                           │
│  • upstream task                  • send_datagram()                 │
│  • downstream task                • recv_datagram()                 │
│  • pre-ACK buffer                 • open() / close()                │
│  • half-close semantics           • inbound queue                   │
│  • registry self-eviction         • registry self-eviction          │
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

| Boundary | Direction | Mechanism | Owner |
|---|---|---|---|
| `session` → `transport` | Downward call | `feed()`, `feed_async()`, `on_remote_closed()` | `session` calls, `transport` implements |
| `transport` → `session` | Upward send | `WsSendCallable` (injected coroutine) | `session` implements, `transport` calls |
| `transport` → `proxy` | Downward write | `asyncio.StreamWriter.write()` + `drain()` | `transport` calls OS |
| `proxy` → `transport` | Upward read | `asyncio.StreamReader.read()` | `transport` calls OS |
| `transport` → `protocol` | Encoding only | `encode_*_frame()` functions | `transport` calls, `protocol` is pure |

### What the Transport Layer Knows

* Frame encoding — calls typed helpers from `exectunnel.protocol`
* WebSocket send — via injected `WsSendCallable`
* Local socket I/O — `asyncio.StreamReader` / `asyncio.StreamWriter` for TCP; `asyncio.Queue` bridging for UDP
* Connection and flow lifecycle — start, feed, close, cleanup
* Backpressure — inbound queue caps, pre-ACK buffering, `feed_async` blocking

### What the Transport Layer Does Not Know

| Concern | Owned By |
|---|---|
| SOCKS5 wire protocol negotiation | `proxy` |
| DNS resolution | `session` |
| Frame parsing / decoding | `session` (calls `parse_frame`, payload helpers) |
| WebSocket channel lifecycle | `session` |
| Bootstrap / agent readiness | `session` |
| Reconnection logic | `session` |
| Agent-side socket management | `agent` |

---

## 3. Package Structure

```
exectunnel/transport/
├── __init__.py       public re-export surface
├── _types.py         WsSendCallable, TransportHandler, registry aliases
├── _validation.py    require_bytes() — shared payload type guard
├── tcp.py            TcpConnection implementation
└── udp.py            UdpFlow implementation
```

### Module Dependency Graph

```
__init__.py
    ├── _types.py          (no intra-package deps)
    ├── _validation.py     (no intra-package deps)
    ├── tcp.py
    │     ├── _types.py
    │     └── _validation.py
    └── udp.py
          ├── _types.py
          └── _validation.py
```

**Key property**: `_types.py` and `_validation.py` import nothing from within the transport package. This makes the dependency graph a strict DAG with no cycles possible.

### Import Rules

```
# ✅ Always import from the package root
from exectunnel.transport import TcpConnection, UdpFlow, WsSendCallable

# ✅ Registry aliases — prefer explicit dict annotation at call sites
from exectunnel.transport import TcpRegistry, UdpRegistry

# ✗ Never import from sub-modules directly
from exectunnel.transport.tcp import TcpConnection   # forbidden
from exectunnel.transport._types import WsSendCallable  # forbidden
```

---

## 4. Public API Surface

```
exectunnel.transport
├── WsSendCallable          Protocol — annotate ws_send injection points
├── TransportHandler        Protocol — annotate session-layer registries
├── TcpConnection           Concrete — one per active TCP CONNECT
├── UdpFlow                 Concrete — one per active UDP ASSOCIATE
├── TcpRegistry             Alias    — dict[str, TcpConnection]
└── UdpRegistry             Alias    — dict[str, UdpFlow]
```

### `WsSendCallable`

```
Structural Protocol  |  @runtime_checkable  |  _types.py

Signature:
    async def __call__(
        frame: str,
        *,
        must_queue: bool = False,
        control:    bool = False,
    ) -> None

Semantics:
    frame       — newline-terminated frame string from encode_*_frame()
    must_queue  — block until enqueued under backpressure (data frames)
    control     — bypass flow-control ordering (CONN_CLOSE, UDP_OPEN, UDP_CLOSE)
    precedence  — control=True always wins over must_queue
```

### `TransportHandler`

```
Structural Protocol  |  NOT @runtime_checkable  |  _types.py

Properties:
    is_closed   → bool    True once fully torn down
    drop_count  → int     Total dropped chunks / datagrams

Methods:
    on_remote_closed() → None    React to agent-initiated teardown

Use: annotate session-layer registries without importing concrete classes.
Type-narrow to TcpConnection / UdpFlow before calling handler-specific methods.
```

---

## 5. `TcpConnection` — Design & Architecture

### 5.1 Responsibility

`TcpConnection` bridges **one** local TCP stream (from the SOCKS5 `CONNECT` handler in the proxy layer) to **one** agent-side TCP connection (identified by a `conn_id` from `new_conn_id()`). It owns:

* Two concurrent asyncio copy tasks (`_upstream`, `_downstream`)
* A pre-ACK buffer for data arriving before the agent ACKs the connection
* Half-close semantics matching TCP's full-duplex close model
* Self-eviction from the shared `TcpRegistry` on cleanup

### 5.2 Data Flow

```
LOCAL CLIENT (browser / curl / ssh)
        │
        │  TCP stream bytes
        ▼
asyncio.StreamReader  ──────────────────────────────────────────────────┐
                                                                        │
                              _upstream task                            │
                              ─────────────                             │
                              reader.read(PIPE_READ_CHUNK_BYTES)        │
                              encode_data_frame(conn_id, chunk)         │
                              ws_send(frame, must_queue=True)           │
                                                                        │
                                                                        ▼
                                                              WebSocket / exec channel
                                                                        │
                              _downstream task                          │
                              ───────────────                           │
                              inbound.get() / get_nowait()              │
                              writer.write(chunk)                       │
                              writer.drain()                            │
                                                                        │
asyncio.StreamWriter  ◄─────────────────────────────────────────────────┘
        │
        │  TCP stream bytes
        ▼
LOCAL CLIENT
```

```
AGENT (remote pod)
        │
        │  DATA frames over WebSocket
        ▼
session.recv_loop
        │
        │  decode_binary_payload(frame.payload) → bytes
        │  conn.feed(data)  or  conn.feed_async(data)
        ▼
asyncio.Queue[bytes]  (_inbound, maxsize=TCP_INBOUND_QUEUE_CAP)
        │
        │  _downstream task drains
        ▼
asyncio.StreamWriter → local client
```

### 5.3 Lifecycle State Machine

```
                    ┌─────────────┐
                    │  CREATED    │
                    │             │
                    │ _started=F  │
                    │ _closed=F   │
                    └──────┬──────┘
                           │
              start() called by session layer
              (after agent ACKs CONN_OPEN)
                           │
                           ▼
                    ┌─────────────┐
                    │  RUNNING    │◄────────────────────────────────┐
                    │             │                                 │
                    │ _started=T  │  feed() / feed_async()          │
                    │ _closed=F   │  on_remote_closed()             │
                    │             │  abort_upstream()               │
                    │ upstream ──►│  abort_downstream()             │
                    │ downstream  │                                 │
                    └──────┬──────┘                                 │
                           │                                        │
           ┌───────────────┼───────────────────────────┐           │
           │               │                           │           │
    upstream EOF     upstream error            downstream done     │
    (clean)          or cancel                 (any reason)        │
           │               │                           │           │
           ▼               ▼                           ▼           │
    keep downstream   cancel both              cancel upstream     │
    alive (half-close) → TEARING DOWN         → TEARING DOWN      │
           │               │                           │           │
           └───────────────┴───────────────────────────┘           │
                           │                                        │
                    both tasks done                                 │
                           │                                        │
                           ▼                                        │
                    ┌─────────────┐                                 │
                    │  TEARING    │                                 │
                    │  DOWN       │                                 │
                    │             │                                 │
                    │ _cleanup()  │                                 │
                    │ scheduled   │                                 │
                    └──────┬──────┘                                 │
                           │                                        │
              registry.pop(conn_id)                                 │
              writer.close()                                        │
              writer.wait_closed() [timeout=5s]                     │
                           │                                        │
                           ▼                                        │
                    ┌─────────────┐                                 │
                    │  CLOSED     │                                 │
                    │             │                                 │
                    │ _closed=T   │                                 │
                    │ (Event set) │                                 │
                    └─────────────┘                                 │
                                                                    │
           close_unstarted() path (never started):                  │
           CREATED ──────────────────────────────────► CLOSED ──────┘
```

### 5.4 Pre-ACK Buffer

The agent may send `DATA` frames for a connection before the session layer has called `start()` — this happens when the agent processes the `CONN_OPEN` and immediately begins forwarding data from the target service before the local ACK round-trip completes.

```
Timeline:

  session         transport           agent
     │                │                 │
     │──CONN_OPEN────►│                 │
     │                │──UDP_OPEN──────►│
     │                │                 │ (agent connects to target)
     │                │◄──DATA──────────│ (target responds immediately)
     │                │                 │
     │  feed(data)    │                 │
     │───────────────►│                 │
     │                │ _started=False  │
     │                │ → pre_ack_buffer│
     │                │                 │
     │  start()       │                 │
     │───────────────►│                 │
     │                │ flush buffer    │
     │                │ → _inbound      │
     │                │ spawn tasks     │
```

```
Pre-ACK buffer constraints:

  cap  = max(PIPE_READ_CHUNK_BYTES, PRE_ACK_BUFFER_CAP_BYTES)
  type = list[bytes]   (ordered, preserves chunk boundaries)

  Overflow → TransportError(error_code="transport.pre_ack_buffer_overflow")
           → cleanup scheduled immediately
           → session layer catches, tears down connection

  Flush    → chunks enqueued into _inbound via put_nowait()
           → QueueFull here is a defensive invariant violation
             (pre-ACK cap < queue cap by design)
```

### 5.5 Half-Close Semantics

TCP is full-duplex. A local client sending EOF does not mean the remote server has finished responding. The transport layer models this correctly:

```
Normal full close:
  upstream EOF → send CONN_CLOSE → cancel downstream → cleanup

Half-close (server still responding):
  upstream EOF (clean) → send CONN_CLOSE → KEEP downstream alive
  downstream continues draining until:
    - on_remote_closed() called (agent sent CONN_CLOSE back)
    - downstream task exits for any reason
  then → cleanup

Decision matrix in _on_task_done():

  Finishing task    | Ended cleanly?  | upstream_ended_cleanly? | Cancel peer?
  ──────────────────┼─────────────────┼─────────────────────────┼─────────────
  upstream          | Yes             | Yes                     | No  (half-close)
  upstream          | Yes             | No                       | Yes
  upstream          | No (error/cancel)| any                    | Yes
  downstream        | any             | any                     | Yes (always)
```

### 5.6 Concurrency Model

```
Event loop thread
│
├── _upstream task  (tcp-up-{conn_id})
│     Reads: self._reader (asyncio StreamReader — single consumer)
│     Writes: ws_send (shared, but WsSendCallable is concurrency-safe)
│     Writes: self._bytes_upstream (single writer)
│     Writes: self._upstream_ended_cleanly (single writer)
│     Writes: self._conn_close_sent (via _send_close_frame_once)
│
├── _downstream task  (tcp-down-{conn_id})
│     Reads: self._inbound (asyncio Queue — single consumer)
│     Reads: self._remote_closed (Event — read-only after set)
│     Writes: self._writer (asyncio StreamWriter — single writer)
│     Writes: self._bytes_downstream (single writer)
│     Writes: self._downstream_ended_cleanly (single writer)
│     Creates: get_task (per blocking iteration — cancelled on exit)
│     Reuses:  close_task (created once, cancelled in finally)
│
├── _cleanup task  (tcp-cleanup-{conn_id})
│     Awaits: _upstream_task, _downstream_task
│     Writes: self._closed (Event — set once)
│     Writes: self._registry (pop — dict is not thread-safe but
│             all access is from the same event loop thread)
│
└── Sync callbacks (called by asyncio from event loop thread)
      _on_task_done(task)  — schedules _cleanup_task
      _on_cleanup_done(task) — logs unexpected exceptions
```

**No locks are used.** All state is accessed from the single asyncio event loop thread. The only shared mutable state between tasks is:

| Shared state | Writer | Readers | Safety mechanism |
|---|---|---|---|
| `_inbound` | `feed()` / `feed_async()` (session) | `_downstream` | `asyncio.Queue` is event-loop-safe |
| `_remote_closed` | `on_remote_closed()` (session) | `_downstream` | `asyncio.Event` is event-loop-safe |
| `_closed` | `_cleanup()` | all paths | `asyncio.Event` is event-loop-safe |
| `_drop_count` | `feed()`, `start()` | `drop_count` property | single event loop thread |

### 5.7 Downstream Batch Drain

```
_downstream inner loop:

  while True:
    ┌─────────────────────────────────────────────────────┐
    │  BATCH DRAIN                                        │
    │  collect up to _DOWNSTREAM_BATCH_SIZE chunks        │
    │  via get_nowait() (non-blocking)                    │
    │  writer.write(chunk) for each                       │
    │  writer.drain() once per batch                      │
    │  → amortises per-drain syscall overhead             │
    └──────────────────────────┬──────────────────────────┘
                               │ batch empty
                               ▼
    ┌─────────────────────────────────────────────────────┐
    │  CLOSE CHECK                                        │
    │  if _remote_closed.is_set():                        │
    │    drain queue fully (loop back to BATCH DRAIN)     │
    │    write_eof() if can_write_eof()                   │
    │    break                                            │
    └──────────────────────────┬──────────────────────────┘
                               │ not closed
                               ▼
    ┌─────────────────────────────────────────────────────┐
    │  BLOCKING WAIT                                      │
    │  asyncio.wait({get_task, close_task},               │
    │               return_when=FIRST_COMPLETED)          │
    │  get_task:   fresh per iteration (Queue.get())      │
    │  close_task: reused across all iterations           │
    │  → loop back to BATCH DRAIN on either completion    │
    └─────────────────────────────────────────────────────┘
```

**Why `close_task` is reused**: `asyncio.Event.wait()` is a coroutine that suspends until the event is set. Creating a new one per iteration would allocate a new coroutine object, a new `Task`, and register a new wakeup callback on the `Event` — on a 10 Gbps stream with 6 KB chunks this is ~1.6 million allocations per second. Reusing the task reduces this to one allocation for the lifetime of the connection.

**Why `get_task` is fresh per iteration**: `asyncio.Queue.get()` is a one-shot coroutine — once it completes or is cancelled, it cannot be awaited again. A new coroutine must be created for each blocking wait.

### 5.8 Cleanup Sequencing

```
_cleanup() is idempotent via _closed.is_set() guard.
_cleanup_task is None guard in _on_task_done prevents double-scheduling.
Both guards are necessary and complementary.

Cleanup sequence:
  1. _closed.set()                    ← authoritative gate
  2. cancel + await _upstream_task    ← suppress CancelledError
  3. cancel + await _downstream_task  ← suppress CancelledError
  4. registry.pop(conn_id, None)      ← self-eviction
  5. writer.close()                   ← suppress OSError, RuntimeError
  6. async with asyncio.timeout(5.0): ← Python 3.13+ idiom
       await writer.wait_closed()     ← suppress OSError, RuntimeError, TimeoutError
  7. log debug with byte/drop counters
```

---

## 6. `UdpFlow` — Design & Architecture

### 6.1 Responsibility

`UdpFlow` bridges **one** SOCKS5 `UDP ASSOCIATE` flow through the tunnel. Unlike TCP, UDP has no stream — each datagram is an independent unit. `UdpFlow` owns:

* The `UDP_OPEN` / `UDP_CLOSE` handshake with the agent
* An inbound `asyncio.Queue[bytes]` for datagrams from the agent
* `send_datagram()` for outbound datagrams to the agent
* `recv_datagram()` for the proxy layer to consume inbound datagrams
* Self-eviction from the shared `UdpRegistry` on close

### 6.2 Data Flow

```
LOCAL SOCKS5 UDP RELAY (proxy layer)
        │
        │  raw datagram bytes
        ▼
send_datagram(data)
        │
        │  encode_udp_data_frame(flow_id, data)
        │  ws_send(frame)
        ▼
WebSocket / exec channel ──────────────────────────────────► AGENT
                                                               │
                                                               │ UDP socket
                                                               ▼
                                                         TARGET SERVICE
                                                               │
                                                               │ UDP response
                                                               ▼
AGENT ─────────────────────────────────────────────────────────┐
        │                                                      │
        │  UDP_DATA frames over WebSocket                      │
        ▼                                                      │
session.recv_loop                                              │
        │                                                      │
        │  decode_binary_payload(frame.payload) → bytes        │
        │  flow.feed(data)                                     │
        ▼                                                      │
asyncio.Queue[bytes]  (_inbound, maxsize=UDP_INBOUND_QUEUE_CAP)│
        │                                                      │
        │  recv_datagram() called by proxy layer               │
        ▼                                                      │
LOCAL SOCKS5 UDP RELAY ◄───────────────────────────────────────┘
```

### 6.3 Lifecycle State Machine

```
                    ┌─────────────┐
                    │  CREATED    │
                    │             │
                    │ _opened=F   │
                    │ _closed=F   │
                    └──────┬──────┘
                           │
                      open() called
                      UDP_OPEN sent
                           │
                           ▼
                    ┌─────────────┐
                    │  OPEN       │◄──────────────────────────────┐
                    │             │                               │
                    │ _opened=T   │  send_datagram()              │
                    │ _closed=F   │  feed()                       │
                    │             │  recv_datagram()              │
                    └──────┬──────┘                               │
                           │                                      │
              ┌────────────┴────────────┐                         │
              │                         │                         │
         close()                on_remote_closed()                │
         (local teardown)       (agent-initiated)                 │
              │                         │                         │
              ▼                         ▼                         │
         _closed=True            _closed=True                     │
         _closed_event.set()     _closed_event.set()              │
         _evict()                _evict()                         │
         send UDP_CLOSE          (no frame sent)                  │
              │                         │                         │
              └────────────┬────────────┘                         │
                           │                                      │
                           ▼                                      │
                    ┌─────────────┐                               │
                    │  CLOSED     │                               │
                    │             │                               │
                    │ _closed=T   │                               │
                    │ _closed_    │                               │
                    │ event set   │                               │
                    └─────────────┘                               │
                                                                  │
           open() failed (send error):                            │
           CREATED ──────────────────────────────────────────────►│
           (_opened stays False — retry is permitted)
```

### 6.4 `recv_datagram` — Blocking Wait with Task Reuse

```
recv_datagram() call sequence:

  ┌─────────────────────────────────────────────────────────────┐
  │  FAST PATH                                                  │
  │  inbound.get_nowait()                                       │
  │  → return bytes immediately if data is available            │
  └──────────────────────────┬──────────────────────────────────┘
                             │ QueueEmpty
                             ▼
  ┌─────────────────────────────────────────────────────────────┐
  │  CLOSED CHECK                                               │
  │  if _closed_event.is_set(): return None                     │
  └──────────────────────────┬──────────────────────────────────┘
                             │ not closed
                             ▼
  ┌─────────────────────────────────────────────────────────────┐
  │  LAZY TASK INIT                                             │
  │  if _close_task is None or _close_task.done():              │
  │    _close_task = create_task(_closed_event.wait())          │
  │  get_task = create_task(inbound.get())   ← fresh each call  │
  └──────────────────────────┬──────────────────────────────────┘
                             │
                             ▼
  ┌─────────────────────────────────────────────────────────────┐
  │  RACE                                                       │
  │  done, pending = await asyncio.wait(                        │
  │      {get_task, _close_task},                               │
  │      return_when=FIRST_COMPLETED,                           │
  │  )                                                          │
  └──────────────────────────┬──────────────────────────────────┘
                             │
              ┌──────────────┴──────────────┐
              │                             │
         get_task won                 close_task won
              │                             │
              ▼                             ▼
         return bytes              final get_nowait()
                                   → bytes if available
                                   → None if empty
```

**Drain contract**: `recv_datagram()` returns **one datagram per call**. After the flow closes, the caller must loop until `None` is returned:

```python
# Correct caller pattern
while (datagram := await flow.recv_datagram()) is not None:
    relay_to_local_client(datagram)
# flow is closed and queue is fully drained
```

### 6.5 UDP vs TCP — Key Differences

| Concern | `TcpConnection` | `UdpFlow` |
|---|---|---|
| Data unit | Byte stream (chunked) | Independent datagrams |
| Splitting | Chunks ≤ 6,108 bytes | Never split — 1 datagram = 1 frame |
| Copy tasks | Two asyncio tasks (`_upstream`, `_downstream`) | No tasks — caller drives send/recv |
| Pre-ACK buffer | Yes — `list[bytes]` | No — open() must succeed before data |
| Half-close | Yes — TCP FIN semantics | No — close is atomic |
| Cleanup | `_cleanup()` task, writer.close() | `_evict()` only — no writer to close |
| `__slots__` | Yes | Yes |
| Backpressure | `feed_async()` blocks caller | `feed()` drops silently (UDP semantics) |
| Drop policy | Queue full → `TransportError` raised | Queue full → silent drop + counter |

---

## 7. Shared Infrastructure

### 7.1 `_types.py` — Type System

```
WsSendCallable
├── @runtime_checkable Protocol
├── Used to annotate ws_send parameter in TcpConnection.__init__ and UdpFlow.__init__
├── isinstance() check valid in tests and session-layer wiring
└── Coroutine[Any, Any, None] return — precise; Awaitable[None] would also be valid

TransportHandler
├── NOT @runtime_checkable — async method inspection is fragile at runtime
├── Used to annotate session-layer registries: dict[str, TransportHandler]
├── Declares: is_closed, drop_count, on_remote_closed()
└── Type-narrow to TcpConnection / UdpFlow for handler-specific methods

TcpRegistry  =  dict[str, TcpConnection]   (type alias, Python 3.12+ syntax)
UdpRegistry  =  dict[str, UdpFlow]         (type alias, Python 3.12+ syntax)

TYPE_CHECKING guard:
  Concrete handler imports are erased at runtime → no circular import.
  type alias statement is lazily evaluated → forward references resolve
  at static-analysis time without runtime cost.
```

### 7.2 `_validation.py` — Payload Type Guard

```
require_bytes(value, handler_id, method) → bytes

Purpose:
  Ensure the frame encoder always receives raw bytes.
  A non-bytes payload here is a programming error in the caller,
  not a malformed frame from the wire — hence TransportError,
  not FrameDecodingError.

Error contract:
  error_code = "transport.invalid_payload_type"
  details    = {handler_id, method, received_type}
  hint       = instructs caller to pass raw bytes

Call sites:
  TcpConnection.feed()        → require_bytes(data, conn_id, "feed")
  TcpConnection.feed_async()  → require_bytes(data, conn_id, "feed_async")
  UdpFlow.feed()              → require_bytes(data, flow_id, "feed")
  UdpFlow.send_datagram()     → require_bytes(data, flow_id, "send_datagram")

Why handler_id not conn_id/flow_id:
  Generic name covers both TCP and UDP without requiring separate validators.
  The actual ID value (conn_id or flow_id) is passed as the argument.
```

---

## 8. Backpressure Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  INBOUND BACKPRESSURE (agent → local)                               │
│                                                                     │
│  WebSocket recv_loop (session)                                      │
│       │                                                             │
│       │  feed_async(data)  ← blocks here when queue is full        │
│       ▼                                                             │
│  asyncio.Queue[bytes]  (TCP_INBOUND_QUEUE_CAP)                      │
│       │                                                             │
│       │  _downstream drains                                         │
│       ▼                                                             │
│  asyncio.StreamWriter.drain()  ← blocks here when OS buffer full   │
│       │                                                             │
│       ▼                                                             │
│  Local TCP socket kernel buffer                                     │
│                                                                     │
│  Backpressure chain:                                                │
│  slow local client → drain() blocks → _downstream blocks →         │
│  Queue fills → feed_async() blocks → recv_loop pauses →            │
│  WebSocket read rate drops → TCP flow control to agent →            │
│  agent slows reads from target service                              │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  OUTBOUND BACKPRESSURE (local → agent)                              │
│                                                                     │
│  asyncio.StreamReader.read()  ← blocks when local client pauses    │
│       │                                                             │
│       │  encode_data_frame()                                        │
│       ▼                                                             │
│  ws_send(frame, must_queue=True)  ← blocks under WS backpressure   │
│       │                                                             │
│       ▼                                                             │
│  WebSocket send queue (session layer)                               │
│       │                                                             │
│       ▼                                                             │
│  Kubernetes API server → agent                                      │
│                                                                     │
│  Backpressure chain:                                                │
│  slow agent / network → WS queue fills → ws_send blocks →          │
│  _upstream blocks → StreamReader.read() not called →               │
│  local client TCP receive window fills → local client pauses send  │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  UDP — NO BACKPRESSURE (intentional)                                │
│                                                                     │
│  UDP is a best-effort protocol. Backpressure would change its       │
│  semantics. Instead:                                                │
│                                                                     │
│  Inbound queue full → silent drop + drop_count++                   │
│  Outbound send fail → TransportError raised to caller               │
│                                                                     │
│  Monitoring: udp.flow.inbound_queue.drop metric                     │
│  Alerting:   WARNING log every UDP_WARN_EVERY drops                 │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 9. Error Taxonomy

### 9.1 Errors Raised by the Transport Layer

| `error_code` | Class | Raised By | Meaning | Retryable |
|---|---|---|---|---|
| `transport.invalid_payload_type` | `TransportError` | `require_bytes()` | Caller passed non-bytes | No |
| `transport.pre_ack_buffer_overflow` | `TransportError` | `TcpConnection.feed()` | Pre-ACK buffer cap exceeded | No |
| `transport.inbound_queue_full` | `TransportError` | `TcpConnection.feed()` | Post-ACK queue saturated | No |
| `transport.feed_async_on_closed` | `ConnectionClosedError` | `TcpConnection.feed_async()` | Called on closed connection | No |
| `transport.feed_async_closed_during_enqueue` | `ConnectionClosedError` | `TcpConnection.feed_async()` | Closed concurrently | No |
| `transport.udp_open_on_closed` | `TransportError` | `UdpFlow.open()` | Flow already closed | No |
| `transport.udp_open_failed` | `TransportError` | `UdpFlow.open()` | Unexpected send failure | Yes |
| `transport.udp_close_failed` | `TransportError` | `UdpFlow.close()` | Unexpected close send failure | No |
| `transport.udp_data_send_failed` | `TransportError` | `UdpFlow.send_datagram()` | Unexpected data send failure | No |

### 9.2 Errors Propagated Through the Transport Layer

These originate in the `session` layer's `WsSendCallable` implementation and are propagated upward by the transport layer without wrapping:

| Class | Propagated By | Action |
|---|---|---|
| `WebSocketSendTimeoutError` | `_upstream`, `_send_close_frame_once`, `UdpFlow.open/close/send_datagram` | Logged at WARNING; teardown continues |
| `ConnectionClosedError` | `_upstream`, `_send_close_frame_once`, `UdpFlow.open/close/send_datagram` | Logged at DEBUG for close frames; WARNING for data |
| `ProtocolError` | `UdpFlow.open()` (from `encode_udp_open_frame`) | Propagated uncaught — caller bug |

### 9.3 Error Handling Decision Tree

```
Exception in _upstream or _downstream task:
│
├── asyncio.CancelledError
│     → set _cancelled=True (upstream only)
│     → metrics_inc cancelled
│     → re-raise (never suppress)
│
├── WebSocketSendTimeoutError
│     → _log_task_exception (WARNING)
│     → task exits → _on_task_done → cleanup
│
├── ConnectionClosedError
│     → _log_task_exception (WARNING)
│     → task exits → _on_task_done → cleanup
│
├── TransportError (other)
│     → _log_task_exception (WARNING)
│     → task exits → _on_task_done → cleanup
│
├── OSError
│     → _log_task_exception (DEBUG)
│     → task exits → _on_task_done → cleanup
│
├── ExecTunnelError (other)
│     → _log_task_exception (WARNING)
│     → task exits → _on_task_done → cleanup
│
└── Exception (unexpected)
      → _log_task_exception (WARNING + DEBUG traceback)
      → task exits → _on_task_done → cleanup

Exception in _send_close_frame_once:
│
├── WebSocketSendTimeoutError → WARNING log, teardown continues
├── ConnectionClosedError     → DEBUG log, teardown continues
└── Exception                 → WARNING log + DEBUG traceback, teardown continues

Note: _send_close_frame_once NEVER re-raises — teardown must always complete.
```

---

## 10. Observability Surface

### 10.1 Metrics

#### TCP Metrics

| Metric Key | Type | Labels | Meaning |
|---|---|---|---|
| `tcp.connection.upstream.started` | counter | — | Upstream task spawned |
| `tcp.connection.upstream.cancelled` | counter | — | Upstream task cancelled |
| `tcp.connection.upstream.duration_sec` | histogram | — | Upstream task lifetime |
| `tcp.connection.upstream.bytes` | histogram | — | Bytes sent upstream per connection |
| `tcp.connection.upstream.error` | counter | `error` | Upstream task error by type |
| `tcp.connection.downstream.started` | counter | — | Downstream task spawned |
| `tcp.connection.downstream.cancelled` | counter | — | Downstream task cancelled |
| `tcp.connection.downstream.duration_sec` | histogram | — | Downstream task lifetime |
| `tcp.connection.downstream.bytes` | histogram | — | Bytes received downstream per connection |
| `tcp.connection.downstream.error` | counter | `error` | Downstream task error by type |
| `tcp.connection.pre_ack_buffer.overflow` | counter | — | Pre-ACK buffer overflow events |
| `tcp.connection.inbound_queue.drop` | counter | — | Post-ACK queue drop events |
| `tcp.connection.conn_close.error` | counter | `error` | CONN_CLOSE send failures |
| `tcp.connection.cleanup` | counter | — | Cleanup executions |

#### UDP Metrics

| Metric Key | Type | Labels | Meaning |
|---|---|---|---|
| `udp.flow.opened` | counter | — | Flows successfully opened |
| `udp.flow.closed` | counter | — | Flows closed locally |
| `udp.flow.closed_remote` | counter | — | Flows closed by remote agent |
| `udp.flow.close.connection_already_closed` | counter | — | UDP_CLOSE skipped (WS already gone) |
| `udp.flow.datagram.accepted` | counter | — | Inbound datagrams enqueued |
| `udp.flow.datagram.sent` | counter | — | Outbound datagrams sent |
| `udp.flow.inbound_queue.drop` | counter | — | Inbound datagrams dropped |
| `udp.flow.feed_after_close.drop` | counter | — | Feed calls after close (dropped) |

#### Error Label Values

| `error` label value | Source |
|---|---|
| `ws_send_timeout` | `WebSocketSendTimeoutError` |
| `connection_closed` | `ConnectionClosedError` |
| `os_error` | `OSError` |
| `os_drain` | `OSError` during `writer.drain()` |
| `{error_code_with_underscores}` | Any `ExecTunnelError` subclass |
| `{ExceptionClassName}` | Unexpected exception types |

### 10.2 Spans

| Span Name | Wraps |
|---|---|
| `tcp.connection.upstream` | Entire `_upstream` task body |
| `tcp.connection.downstream` | Entire `_downstream` task body |

### 10.3 Structured Log Fields

All log calls include an `extra=` dict. Standard fields:

| Field | Present In | Value |
|---|---|---|
| `conn_id` | All TCP logs | The `conn_id` string |
| `direction` | Task error logs | `"upstream"` or `"downstream"` |
| `bytes_sent` | Upstream error logs | `_bytes_upstream` at time of error |
| `bytes_recv` | Downstream error logs | `_bytes_downstream` at time of error |
| `error_code` | ExecTunnelError logs | `exc.error_code` |
| `error_id` | ExecTunnelError logs | `exc.error_id` (UUID4 hex) |

### 10.4 Log Levels

| Level | Used For |
|---|---|
| `DEBUG` | Normal lifecycle events (start, cleanup, close), OSError in tasks, CONN_CLOSE skipped |
| `WARNING` | Dropped data, send timeouts, unexpected task failures, pre-ACK overflow |

---

## 11. Configuration Constants

All constants are imported from `exectunnel.config.defaults`. The transport layer does not define defaults — it consumes them.

| Constant | Used By | Meaning | Protocol Constraint |
|---|---|---|---|
| `PIPE_READ_CHUNK_BYTES` | `TcpConnection._upstream` | TCP read chunk size | Must be ≤ 6,108 bytes (enforced by `assert` at import time) |
| `PRE_ACK_BUFFER_CAP_BYTES` | `TcpConnection.__init__` | Default pre-ACK buffer cap | Must be ≥ `PIPE_READ_CHUNK_BYTES` (clamped) |
| `TCP_INBOUND_QUEUE_CAP` | `TcpConnection.__init__` | Inbound queue depth (chunks) | Must be > `PRE_ACK_BUFFER_CAP_BYTES / PIPE_READ_CHUNK_BYTES` |
| `UDP_INBOUND_QUEUE_CAP` | `UdpFlow.__init__` | Inbound queue depth (datagrams) | No protocol constraint |
| `UDP_WARN_EVERY` | `UdpFlow.feed()` | Drop warning frequency | No protocol constraint |

### Chunk Size Invariant Enforcement

```python
# Enforced at module import time in tcp.py:
_MAX_DATA_CHUNK_BYTES: Final[int] = 6_108

assert PIPE_READ_CHUNK_BYTES <= _MAX_DATA_CHUNK_BYTES, (
    f"PIPE_READ_CHUNK_BYTES ({PIPE_READ_CHUNK_BYTES}) exceeds the protocol "
    f"maximum of {_MAX_DATA_CHUNK_BYTES} bytes per DATA chunk. "
    "Reduce PIPE_READ_CHUNK_BYTES or the agent will reject oversized frames as FrameDecodingError."
)
```

A misconfigured value fails loudly at import time rather than silently producing oversized frames that the agent would reject as `FrameDecodingError`.

---

## 12. Session Layer Integration Contract

This section documents exactly what the `session` layer must do when interacting with the transport layer. It is the authoritative interface specification for the layer above.

### 12.1 TCP Connection Lifecycle (Session Perspective)

```python
# 1. Construct on CONN_OPEN from proxy layer
conn = TcpConnection(
    conn_id=new_conn_id(),
    reader=reader,
    writer=writer,
    ws_send=ws_send,          # injected WsSendCallable
    registry=tcp_registry,    # shared dict[str, TcpConnection]
)
tcp_registry[conn.conn_id] = conn

# 2. Send CONN_OPEN frame to agent
await ws_send(encode_conn_open_frame(conn.conn_id, host, port))

# 3a. Agent ACKs (session receives CONN_OPEN echo or proceeds)
conn.start()

# 3b. Agent rejects immediately (ERROR frame before ACK)
conn.on_remote_closed()
await conn.close_unstarted()

# 4. Inbound DATA frames from agent (in recv_loop)
data = decode_binary_payload(frame.payload)   # session layer decodes
try:
    await conn.feed_async(data)               # blocks under backpressure
except ConnectionClosedError:
    pass  # connection already torn down

# 5. Agent closes (CONN_CLOSE or ERROR frame)
conn.on_remote_closed()

# 6. Session shutdown — hard cancel
conn.abort()

# 7. Wait for cleanup
await conn.closed_event.wait()
```

### 12.2 UDP Flow Lifecycle (Session Perspective)

```python
# 1. Construct on UDP_ASSOCIATE from proxy layer
flow = UdpFlow(
    flow_id=new_flow_id(),
    host=host,
    port=port,
    ws_send=ws_send,
    registry=udp_registry,
)
udp_registry[flow.flow_id] = flow

# 2. Open (sends UDP_OPEN to agent)
await flow.open()

# 3. Relay outbound datagrams (proxy layer calls this)
await flow.send_datagram(datagram_bytes)

# 4. Inbound UDP_DATA frames from agent (in recv_loop)
data = decode_binary_payload(frame.payload)   # session layer decodes
flow.feed(data)                               # non-blocking, may drop

# 5. Proxy layer consumes inbound datagrams
while (datagram := await flow.recv_datagram()) is not None:
    relay_to_local_udp_client(datagram)

# 6. Agent closes (UDP_CLOSE frame)
flow.on_remote_closed()

# 7. Local teardown
await flow.close()
```

### 12.3 Registry Management Rules

```
TcpRegistry / UdpRegistry:

  INSERT:  session layer inserts immediately after construction,
           before sending CONN_OPEN / UDP_OPEN to the agent.

  LOOKUP:  session recv_loop looks up by conn_id / flow_id on every
           inbound DATA / UDP_DATA / CONN_CLOSE / UDP_CLOSE / ERROR frame.

  REMOVE:  handlers self-evict via registry.pop(id, None) in:
             TcpConnection._cleanup()
             UdpFlow._evict() (called by close() and on_remote_closed())

  NEVER:   session layer must not manually remove entries — self-eviction
           is the only correct removal path. Manual removal races with
           the handler's own cleanup.

  SESSION_CONN_ID must never be stored as a registry key.
  It is reserved for session-level ERROR frames only.
```

---

## 13. Invariants Every Caller Must Preserve

```
Transport layer invariants (complement to protocol layer invariants):

 1. TcpConnection is constructed with a conn_id from new_conn_id() only.
    Never reuse a conn_id after the connection is closed.

 2. UdpFlow is constructed with a flow_id from new_flow_id() only.
    Never reuse a flow_id after the flow is closed.

 3. start() is called at most once per TcpConnection instance.
    Subsequent calls are logged and ignored — but relying on idempotency
    is a caller bug.

 4. feed() and feed_async() must not be called after is_closed is True.
    feed() silently returns; feed_async() raises ConnectionClosedError.

 5. close_unstarted() must only be called when is_started is False.
    Raises RuntimeError otherwise.

 6. on_remote_closed() is idempotent — safe to call multiple times.
    The session layer must call it exactly once per CONN_CLOSE / UDP_CLOSE
    / ERROR frame, but double-calling is not fatal.

 7. decode_binary_payload() must be called by the session layer before
    passing data to feed() / feed_async(). The transport layer receives
    raw bytes only — never base64 strings.

 8. UDP datagrams must never be split across multiple send_datagram() calls.
    One datagram = one call. The proxy layer is responsible for ensuring
    datagrams fit within the 6,108-byte payload budget.

 9. The ws_send callable must be concurrency-safe — it will be called
    concurrently by _upstream (data frames) and _send_close_frame_once
    (control frames) from different asyncio tasks.

10. abort() cancels tasks but does not await them. The session layer must
    await closed_event if it needs to know cleanup is complete.
```

---

## 14. Extension Points

### 14.1 Adding a New Handler Type

To add a new handler (e.g. a raw IP flow for ICMP tunneling):

```
1. Create exectunnel/transport/raw.py
2. Define class RawFlow with __slots__
3. Implement TransportHandler protocol:
     - is_closed property
     - drop_count property
     - on_remote_closed() method
4. Add handler-specific methods (feed, send, etc.)
5. Call require_bytes() on all payload inputs
6. Import WsSendCallable and the appropriate registry type from _types.py
7. Add RawFlow to __init__.py __all__
8. Add RawRegistry type alias to _types.py
9. Update session layer dispatch match statement
10. Update protocol layer with new frame types if needed
```

### 14.2 Adding a New Validation Helper

```
1. Add function to _validation.py
2. Follow the contract:
     - First arg: raw value
     - handler_id: str
     - method: str
     - Return validated value typed correctly
     - Raise ExecTunnelError subclass — never bare ValueError
     - Never use `raise ... from exc` unless inside an except block
3. Add to __all__ in _validation.py
4. Document in this architectural doc
```

### 14.3 Replacing the Inbound Queue

The `asyncio.Queue[bytes]` used for inbound buffering can be replaced with a bounded deque or a priority queue by changing the `_inbound` attribute type and updating `feed()`, `feed_async()`, and the drain logic in `_downstream` / `recv_datagram()`. The `TransportHandler` protocol does not expose the queue type — this is an internal implementation detail.

---

## 15. What This Layer Explicitly Does Not Do

| Concern | Why Not Here | Where It Lives |
|---|---|---|
| Parse inbound frames | Transport receives decoded bytes, not raw frames | `session` calls `parse_frame()` |
| Decode base64 payloads | `decode_binary_payload()` is a session-layer concern | `session` calls payload helpers |
| DNS resolution | Transport receives `host: str` already resolved or passed through | `session` resolves before constructing handlers |
| SOCKS5 negotiation | Transport is SOCKS5-agnostic | `proxy` |
| WebSocket channel management | Transport receives an injected callable | `session` |
| Reconnection / retry | Transport handlers are single-connection objects | `session` |
| Authentication | Transport has no concept of identity | `session` / `auth` |
| Agent bootstrap | Transport assumes the agent is already ready | `session` |
| Frame routing | Transport does not know about other connections | `session` registry dispatch |
| TLS / mTLS | Transport operates above the WebSocket layer | `session` / Kubernetes API client |

---

## 16. Quick Reference Card

```
┌─────────────────────────────────────────────────────────────────────┐
│  TRANSPORT LAYER QUICK REFERENCE                                    │
├─────────────────────────────────────────────────────────────────────┤
│  Construct                                                          │
│    TcpConnection(conn_id, reader, writer, ws_send, registry)        │
│    UdpFlow(flow_id, host, port, ws_send, registry)                  │
│                                                                     │
│  TCP lifecycle                                                      │
│    conn.start()                  ← after agent ACK                  │
│    conn.feed(data)               ← sync, pre-ACK or post-ACK        │
│    await conn.feed_async(data)   ← async, post-ACK with backpressure│
│    conn.on_remote_closed()       ← on CONN_CLOSE / ERROR frame      │
│    conn.abort()                  ← hard cancel both directions      │
│    conn.abort_upstream()         ← stop sending, keep receiving     │
│    await conn.close_unstarted()  ← if start() was never called      │
│    await conn.closed_event.wait()← wait for full cleanup            │
│                                                                     │
│  UDP lifecycle                                                      │
│    await flow.open()             ← sends UDP_OPEN                   │
│    flow.feed(data)               ← enqueue inbound datagram         │
│    await flow.recv_datagram()    ← returns bytes | None             │
│    await flow.send_datagram(data)← sends UDP_DATA                   │
│    flow.on_remote_closed()       ← on UDP_CLOSE frame               │
│    await flow.close()            ← sends UDP_CLOSE                  │
│                                                                     │
│  Properties (both)                                                  │
│    .is_closed    → bool                                             │
│    .drop_count   → int                                              │
│                                                                     │
│  TCP-only properties                                                │
│    .conn_id, .is_started, .is_remote_closed                         │
│    .bytes_upstream, .bytes_downstream                               │
│    .closed_event → asyncio.Event                                    │
│                                                                     │
│  UDP-only properties                                                │
│    .flow_id, .is_opened                                             │
│    .bytes_sent, .bytes_recv                                         │
│                                                                     │
│  Never do                                                           │
│    ✗ decode_binary_payload() in transport — session layer only      │
│    ✗ parse_frame() in transport — session layer only                │
│    ✗ split UDP datagrams across send_datagram() calls               │
│    ✗ manually remove entries from TcpRegistry / UdpRegistry         │
│    ✗ call start() more than once                                    │
│    ✗ call close_unstarted() after start()                           │
│    ✗ store SESSION_CONN_ID as a registry key                        │
└─────────────────────────────────────────────────────────────────────┘
```

