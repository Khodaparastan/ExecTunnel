# ExecTunnel Transport Package — Developer API Reference

```
exectunnel/transport/  |  api-doc v2.0  |  Python 3.13+
audience: developers building session / proxy layers
```

---

## How to Read This Document

This reference is written for developers **consuming** the transport layer from the
`session` or `proxy` layers. It answers:

* What do I import and when?
* What do I construct and how?
* What methods do I call, in what order, under what conditions?
* What exceptions must I handle and what do they mean?
* What are the exact contracts I must not violate?

Every section is self-contained. Cross-references are explicit. No knowledge of the
transport layer internals is assumed.

---

## Table of Contents

1. [Imports](#1-imports)
2. [WsSendCallable — Implementing the Injection Point](#2-wssenddcallable--implementing-the-injection-point)
3. [TransportHandler — Typing Your Registries](#3-transporthandler--typing-your-registries)
4. [TcpConnection — Full API](#4-tcpconnection--full-api)
    * 4.1 [Construction](#41-construction)
    * 4.2 [Lifecycle Methods](#42-lifecycle-methods)
    * 4.3 [Data Methods](#43-data-methods)
    * 4.4 [Properties](#44-properties)
    * 4.5 [Exception Reference](#45-exception-reference)
    * 4.6 [Complete Usage Example](#46-complete-usage-example)
5. [UdpFlow — Full API](#5-udpflow--full-api)
    * 5.1 [Construction](#51-construction)
    * 5.2 [Lifecycle Methods](#52-lifecycle-methods)
    * 5.3 [Data Methods](#53-data-methods)
    * 5.4 [Properties](#54-properties)
    * 5.5 [Exception Reference](#55-exception-reference)
    * 5.6 [Complete Usage Example](#56-complete-usage-example)
6. [Registry Management](#6-registry-management)
7. [Error Handling Patterns](#7-error-handling-patterns)
8. [Checklist — Before You Ship](#8-checklist--before-you-ship)

---

## 1. Imports

```python
# ── Public surface — always import from the package root ─────────────────────

from exectunnel.transport import (
    # Protocols — use these to annotate your own code
    WsSendCallable,
    TransportHandler,
    # Concrete handlers — construct these directly
    TcpConnection,
    UdpFlow,
    # Registry aliases — optional; explicit dict[str, X] is equally valid
    TcpRegistry,
    UdpRegistry,
)

# ── Exceptions you must handle ────────────────────────────────────────────────

from exectunnel.exceptions import (
    TransportError,
    ConnectionClosedError,
    WebSocketSendTimeoutError,
    ProtocolError,  # propagated from encode_udp_open_frame — do not catch
)

# ── Protocol helpers you call before passing data to transport ────────────────

from exectunnel.protocol import (
    new_conn_id,
    new_flow_id,
    encode_conn_open_frame,
    decode_binary_payload,  # call this BEFORE feed() / feed_async()
)
```

> **Rule**: Never import from sub-modules (`exectunnel.transport.tcp`,
`exectunnel.transport._types`, etc.). The package root is the only stable surface.

---

## 2. `WsSendCallable` — Implementing the Injection Point

Both `TcpConnection` and `UdpFlow` receive a `ws_send` argument at construction. This
argument must satisfy the `WsSendCallable` protocol.

### Signature

```python
async def ws_send(
        frame: str,
        *,
        must_queue: bool = False,
        control: bool = False,
) -> None: ...
```

### Parameter Semantics

| Parameter    | Type   | Meaning                                                                                                                                            |
|--------------|--------|----------------------------------------------------------------------------------------------------------------------------------------------------|
| `frame`      | `str`  | Newline-terminated frame string produced by `encode_*_frame()`. Never construct this manually.                                                     |
| `must_queue` | `bool` | If `True`, block until the frame is enqueued even under send-queue backpressure. Used for data frames where dropping is not acceptable.            |
| `control`    | `bool` | If `True`, bypass normal flow-control ordering. Used for `CONN_OPEN`, `CONN_CLOSE`, `UDP_OPEN`, `UDP_CLOSE`. When `True`, `must_queue` is ignored. |

### Who Passes What

| Caller                                 | `must_queue` | `control` | Frame type   |
|----------------------------------------|--------------|-----------|--------------|
| `TcpConnection._upstream`              | `True`       | `False`   | `DATA`       |
| `TcpConnection._send_close_frame_once` | `False`      | `True`    | `CONN_CLOSE` |
| `UdpFlow.open()`                       | `False`      | `True`    | `UDP_OPEN`   |
| `UdpFlow.close()`                      | `False`      | `True`    | `UDP_CLOSE`  |
| `UdpFlow.send_datagram()`              | `False`      | `False`   | `UDP_DATA`   |

### Exceptions Your Implementation Must Raise

Your `ws_send` implementation must raise these — and only these — on failure. The
transport layer handles them explicitly:

| Exception                   | When to raise                          |
|-----------------------------|----------------------------------------|
| `WebSocketSendTimeoutError` | Send stalled beyond configured timeout |
| `ConnectionClosedError`     | WebSocket is already closed            |

Any other exception raised by your implementation will be caught as a bare `Exception`
and wrapped in `TransportError` by the transport layer.

### Concurrency Requirement

`ws_send` **will be called concurrently** from multiple asyncio tasks within the same
connection:

* `_upstream` calls it for every `DATA` frame
* `_send_close_frame_once` calls it for `CONN_CLOSE` from the `finally` block

Your implementation must be safe for concurrent calls from the same event loop thread (
i.e., it must not use non-reentrant shared state without proper guards).

### Minimal Implementation Skeleton

```python
from exectunnel.exceptions import ConnectionClosedError, WebSocketSendTimeoutError
from exectunnel.transport import WsSendCallable


class MyWsSend:
    """Satisfies WsSendCallable."""

    def __init__(self, websocket: MyWebSocket, send_timeout: float) -> None:
        self._ws = websocket
        self._timeout = send_timeout

    async def __call__(
            self,
            frame: str,
            *,
            must_queue: bool = False,
            control: bool = False,
    ) -> None:
        try:
            async with asyncio.timeout(self._timeout):
                await self._ws.send(frame)
        except TimeoutError as exc:
            raise WebSocketSendTimeoutError(
                "WebSocket send stalled",
                details={"timeout_s": self._timeout, "payload_bytes": len(frame)},
            ) from exc
        except WebSocketClosedError as exc:  # your WS library's closed exception
            raise ConnectionClosedError(
                "WebSocket already closed",
                details={"close_code": exc.code, "close_reason": str(exc)},
            ) from exc


# Verify at wiring time (optional but recommended in tests)
assert isinstance(MyWsSend(ws, 5.0), WsSendCallable)
```

---

## 3. `TransportHandler` — Typing Your Registries

`TransportHandler` is a structural protocol that both `TcpConnection` and `UdpFlow`
satisfy. Use it when you need a single type for a registry that holds both kinds of
handlers, or when you want to write generic session-layer code that works on either.

### Interface

```python
class TransportHandler(Protocol):
    @property
    def is_closed(self) -> bool: ...

    @property
    def drop_count(self) -> int: ...

    def on_remote_closed(self) -> None: ...
```

### When to Use It

```python
# ✅ Use TransportHandler when your code only needs the generic interface
def log_all_handlers(handlers: dict[str, TransportHandler]) -> None:
    for hid, h in handlers.items():
        logger.info("%s: closed=%s drops=%d", hid, h._closed, h.drop_count)


# ✅ Use concrete types when you need handler-specific methods
def handle_data_frame(
        frame: ParsedFrame, tcp_registry: dict[str, TcpConnection]
) -> None:
    conn = tcp_registry.get(frame.conn_id)
    if conn is None:
        return
    conn.feed(data)  # TcpConnection-specific method


# ✅ Type-narrow when dispatching from a generic registry
def dispatch(handler: TransportHandler, data: bytes) -> None:
    match handler:
        case TcpConnection():
            handler.feed(data)
        case UdpFlow():
            handler.feed(data)
```

> **Note**: `TransportHandler` is not `@runtime_checkable`. Do not use
`isinstance(x, TransportHandler)` — use `isinstance(x, TcpConnection)` or
`isinstance(x, UdpFlow)` for runtime type narrowing.

---

## 4. `TcpConnection` — Full API

### 4.1 Construction

```python
TcpConnection(
    conn_id: str,
reader: asyncio.StreamReader,
writer: asyncio.StreamWriter,
ws_send: WsSendCallable,
registry: dict[str, TcpConnection],
*,
pre_ack_buffer_cap_bytes: int = PRE_ACK_BUFFER_CAP_BYTES,
)
```

#### Parameters

| Parameter                  | Type                       | Required | Description                                                                                                 |
|----------------------------|----------------------------|----------|-------------------------------------------------------------------------------------------------------------|
| `conn_id`                  | `str`                      | ✅        | Stable ID from `new_conn_id()`. Never construct manually. Never reuse after close.                          |
| `reader`                   | `asyncio.StreamReader`     | ✅        | Stream reader for the local TCP client (from `asyncio.start_server` callback or `asyncio.open_connection`). |
| `writer`                   | `asyncio.StreamWriter`     | ✅        | Stream writer for the local TCP client. Closed by the handler during cleanup — do not close it yourself.    |
| `ws_send`                  | `WsSendCallable`           | ✅        | Coroutine callable for sending frames. See [§2](#2-wssenddcallable--implementing-the-injection-point).      |
| `registry`                 | `dict[str, TcpConnection]` | ✅        | Shared registry. The handler inserts itself — you must insert it **before** calling `start()`.              |
| `pre_ack_buffer_cap_bytes` | `int`                      | ❌        | Max bytes to buffer before `start()` is called. Clamped to `≥ PIPE_READ_CHUNK_BYTES`.                       |

#### Construction Pattern

```python
conn_id = new_conn_id()

conn = TcpConnection(
    conn_id=conn_id,
    reader=reader,
    writer=writer,
    ws_send=ws_send,
    registry=tcp_registry,
)

# Insert BEFORE sending CONN_OPEN — agent may reply before you return
tcp_registry[conn_id] = conn

# Now send CONN_OPEN to the agent
await ws_send(encode_conn_open_frame(conn_id, host, port))
```

> **Why insert before sending**: The agent may send a `DATA` frame or `CONN_CLOSE`
> before your `await` returns. If the handler is not in the registry, those frames are
> silently dropped.

---

### 4.2 Lifecycle Methods

#### `start() → None`

Spawn the upstream and downstream copy tasks. Call this exactly once, after the agent
has acknowledged the connection.

```python
def start(self) -> None
```

**When to call**: After receiving confirmation that the agent has opened its side of the
connection (your session-layer ACK mechanism).

**Idempotency**: Subsequent calls are logged at `DEBUG` and ignored. Do not rely on
this — calling `start()` more than once is a caller bug.

**Pre-ACK buffer flush**: Any data that arrived via `feed()` before `start()` is flushed
into the inbound queue before the tasks are spawned. The downstream task sees it
immediately.

**If `on_remote_closed()` was called before `start()`**: The downstream task will drain
any buffered data and exit cleanly on its first iteration. This handles the case where
the agent rejects the connection immediately.

```python
# After agent ACK
conn.start()
```

---

#### `on_remote_closed() → None`

Signal that the agent has closed its side of the connection. Call this when you receive
a `CONN_CLOSE` or `ERROR` frame for this `conn_id`.

```python
def on_remote_closed(self) -> None
```

**What it does**:

* Sets the internal `_remote_closed` event
* Wakes the downstream task so it drains remaining queued data and writes EOF to the
  local socket

**Idempotency**: Safe to call multiple times. Subsequent calls are no-ops.

**Safe before `start()`**: If called before `start()`, the downstream task will honour
the close flag on its first iteration.

**Does not send any frame**: This method reacts to a remote close — it does not initiate
one. The `CONN_CLOSE` frame was already sent by `_upstream` when the local client sent
EOF, or will be sent by the agent's own teardown.

```python
# In your recv_loop, on CONN_CLOSE frame
conn = tcp_registry.get(frame.conn_id)
if conn is not None:
    conn.on_remote_closed()

# On ERROR frame for a specific conn_id
conn = tcp_registry.get(frame.conn_id)
if conn is not None:
    conn.on_remote_closed()
    conn.abort_upstream()  # stop sending DATA to a dead connection
```

---

#### `abort() → None`

Hard-cancel both the upstream and downstream tasks immediately. No draining occurs.

```python
def abort(self) -> None
```

**When to use**:

* Session-level shutdown (all connections must close immediately)
* Unrecoverable protocol error
* Timeout waiting for graceful close

**Idempotency**: Cancelling an already-done task is a no-op.

**Does not await cleanup**: `abort()` cancels the tasks but does not wait for cleanup to
complete. If you need to know when cleanup is done, await `closed_event`:

```python
conn.abort()
await conn.closed_event.wait()  # optional — only if you need to know it's done
```

---

#### `abort_upstream() → None`

Cancel the upstream task only, leaving the downstream task alive.

```python
def abort_upstream(self) -> None
```

**When to use**: When the agent signals `ERROR` for a connection — the agent has already
torn down its socket, so sending more `DATA` frames is pointless. But the agent may have
already sent response data that is queued in `_inbound`, so the downstream task should
continue draining.

```python
# On ERROR frame for a specific conn_id
conn = tcp_registry.get(frame.conn_id)
if conn is not None:
    conn.on_remote_closed()  # wake downstream to drain + EOF
    conn.abort_upstream()  # stop sending DATA to dead connection
```

> **Note**: Not part of `TransportHandler`. Type-narrow to `TcpConnection` before
> calling.

---

#### `abort_downstream() → None`

Cancel the downstream task only, leaving the upstream task alive.

```python
def abort_downstream(self) -> None
```

**When to use**: When the local socket is known to be dead (e.g. `OSError` on
`writer.drain()`) and draining remaining queued data would be pointless.

> **Note**: Not part of `TransportHandler`. Type-narrow to `TcpConnection` before
> calling.

---

#### `async close_unstarted() → None`

Close the writer and evict from registry when `start()` was never called.

```python
async def close_unstarted(self) -> None
```

**When to use**: When the agent rejects the connection (sends `ERROR` or `CONN_CLOSE`)
before you call `start()`, and you need to clean up the writer.

**Raises**: `RuntimeError` if called after `start()`. Use `abort()` instead for started
handlers.

**Idempotent**: Safe to call multiple times — `_cleanup` is idempotent via `_closed`.

```python
# Agent rejected connection before ACK
conn = tcp_registry.get(frame.conn_id)
if conn is not None and not conn.is_started:
    await conn.close_unstarted()
```

---

### 4.3 Data Methods

#### `feed(data: bytes) → None`

Synchronously enqueue data from the agent for the downstream task.

```python
def feed(self, data: bytes) -> None
```

**When to use**:

* Pre-ACK: when data arrives before `start()` is called (buffered internally)
* Post-ACK: when your recv_loop cannot `await` (rare — prefer `feed_async`)

**You must decode first**: Call `decode_binary_payload(frame.payload)` in your session
layer before passing data here. The transport layer receives raw bytes only.

```python
# In recv_loop, on DATA frame
data = decode_binary_payload(frame.payload)  # session layer decodes
conn = tcp_registry.get(frame.conn_id)
if conn is not None:
    try:
        conn.feed(data)
    except TransportError as exc:
        match exc.error_code:
            case "transport.pre_ack_buffer_overflow":
                # Connection is being torn down — signal your ACK future
                ack_futures[frame.conn_id].set_exception(exc)
            case "transport.inbound_queue_full":
                # Log and drop — downstream is slow but connection is alive
                logger.warning(
                    "inbound queue full for %s",
                    frame.conn_id,
                    extra={"error": exc.to_dict()},
                )
```

**Raises**:

| Exception        | `error_code`                        | Meaning              | Your action                  |
|------------------|-------------------------------------|----------------------|------------------------------|
| `TransportError` | `transport.invalid_payload_type`    | You passed non-bytes | Fix your decode step         |
| `TransportError` | `transport.pre_ack_buffer_overflow` | Pre-ACK buffer full  | Signal ACK future, tear down |
| `TransportError` | `transport.inbound_queue_full`      | Post-ACK queue full  | Log and drop                 |

**Silent no-op when**: `is_closed` is `True` — data is silently discarded.

---

#### `async feed_async(data: bytes) → None`

Await space in the inbound queue, applying backpressure to the WebSocket reader.

```python
async def feed_async(self, data: bytes) -> None
```

**When to use**: Post-ACK, in your recv_loop, when you want the WebSocket read rate to
slow down when the local client is slow. This is the preferred method for post-ACK data
delivery.

**Backpressure chain**: When the inbound queue is full, `feed_async` blocks. Your
recv_loop is blocked. The WebSocket is not being read. TCP flow control propagates back
to the agent.

**You must decode first**: Same as `feed()`.

```python
# In recv_loop, on DATA frame (preferred post-ACK pattern)
data = decode_binary_payload(frame.payload)
conn = tcp_registry.get(frame.conn_id)
if conn is not None:
    try:
        await conn.feed_async(data)
    except ConnectionClosedError:
        # Connection was closed while we were waiting — normal teardown race
        pass
```

**Raises**:

| Exception                | `error_code`                                 | Meaning                     | Your action                    |
|--------------------------|----------------------------------------------|-----------------------------|--------------------------------|
| `TransportError`         | `transport.invalid_payload_type`             | You passed non-bytes        | Fix your decode step           |
| `ConnectionClosedError`  | `transport.feed_async_on_closed`             | Called on closed connection | Discard — normal teardown race |
| `ConnectionClosedError`  | `transport.feed_async_closed_during_enqueue` | Closed while waiting        | Discard — data will be drained |
| `asyncio.CancelledError` | —                                            | Your task was cancelled     | Re-raise — never suppress      |

**Ordering guarantee**: If `feed_async` raises `ConnectionClosedError` with code
`transport.feed_async_closed_during_enqueue`, the data was already enqueued. The
downstream task will drain it before writing EOF. Do not retry the enqueue.

---

### 4.4 Properties

| Property           | Type            | Description                                                                                                                                                      |
|--------------------|-----------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `conn_id`          | `str`           | The stable connection ID passed at construction.                                                                                                                 |
| `is_started`       | `bool`          | `True` once `start()` has been called.                                                                                                                           |
| `is_closed`        | `bool`          | `True` once `_cleanup()` has completed. Registry entry is gone. Writer is closed.                                                                                |
| `is_remote_closed` | `bool`          | `True` once `on_remote_closed()` has been called.                                                                                                                |
| `closed_event`     | `asyncio.Event` | Awaitable gate for cleanup completion. Use `await conn.closed_event.wait()` when you need to block until cleanup is done. Do not call `.set()` on it directly.   |
| `bytes_upstream`   | `int`           | Total raw bytes successfully delivered from local TCP to the tunnel. Reflects bytes that were actually sent — not bytes that were read but lost on send failure. |
| `bytes_downstream` | `int`           | Total raw bytes successfully delivered from the tunnel to local TCP.                                                                                             |
| `drop_count`       | `int`           | Total chunks dropped due to queue saturation or pre-ACK buffer overflow.                                                                                         |

---

### 4.5 Exception Reference

Complete table of every exception `TcpConnection` can raise to your code:

| Method               | Exception                | `error_code`                                 | Retryable | Your action                |
|----------------------|--------------------------|----------------------------------------------|-----------|----------------------------|
| `feed()`             | `TransportError`         | `transport.invalid_payload_type`             | No        | Fix caller — pass bytes    |
| `feed()`             | `TransportError`         | `transport.pre_ack_buffer_overflow`          | No        | Tear down connection       |
| `feed()`             | `TransportError`         | `transport.inbound_queue_full`               | No        | Log and drop chunk         |
| `feed_async()`       | `TransportError`         | `transport.invalid_payload_type`             | No        | Fix caller — pass bytes    |
| `feed_async()`       | `ConnectionClosedError`  | `transport.feed_async_on_closed`             | No        | Discard                    |
| `feed_async()`       | `ConnectionClosedError`  | `transport.feed_async_closed_during_enqueue` | No        | Discard                    |
| `feed_async()`       | `asyncio.CancelledError` | —                                            | —         | Re-raise                   |
| `close_unstarted()`  | `RuntimeError`           | —                                            | No        | Fix caller — use `abort()` |
| `start()`            | *(none)*                 | —                                            | —         | —                          |
| `on_remote_closed()` | *(none)*                 | —                                            | —         | —                          |
| `abort()`            | *(none)*                 | —                                            | —         | —                          |
| `abort_upstream()`   | *(none)*                 | —                                            | —         | —                          |
| `abort_downstream()` | *(none)*                 | —                                            | —         | —                          |

---

### 4.6 Complete Usage Example

```python
import asyncio
from exectunnel.exceptions import ConnectionClosedError, TransportError
from exectunnel.protocol import (
    new_conn_id,
    encode_conn_open_frame,
    decode_binary_payload,
    parse_host_port,
    SESSION_CONN_ID,
)
from exectunnel.transport import TcpConnection

# ── Session layer wiring ──────────────────────────────────────────────────────

tcp_registry: dict[str, TcpConnection] = {}


async def handle_conn_open(
        frame: ParsedFrame,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        ws_send: WsSendCallable,
) -> None:
    """Called by the proxy layer when a SOCKS5 CONNECT is accepted."""
    host, port = parse_host_port(frame.payload)  # session layer decodes
    conn_id = new_conn_id()

    conn = TcpConnection(
        conn_id=conn_id,
        reader=reader,
        writer=writer,
        ws_send=ws_send,
        registry=tcp_registry,
    )

    # Insert BEFORE sending CONN_OPEN — agent may reply before we return
    tcp_registry[conn_id] = conn

    await ws_send(encode_conn_open_frame(conn_id, host, port))
    conn.start()


async def recv_loop(ws_send: WsSendCallable) -> None:
    """Main inbound frame dispatch loop."""
    async for raw_line in websocket:
        for line in raw_line.splitlines():
            try:
                frame = parse_frame(line)
            except FrameDecodingError as exc:
                raise ConnectionClosedError(
                    "Agent sent corrupt frame",
                    details={"close_code": 1002, "close_reason": exc.message},
                ) from exc

            if frame is None:
                continue  # shell noise — not an error

            match frame.msg_type:
                case "DATA":
                    conn = tcp_registry.get(frame.conn_id)
                    if conn is None:
                        continue
                    data = decode_binary_payload(frame.payload)
                    try:
                        await conn.feed_async(data)
                    except ConnectionClosedError:
                        pass  # normal teardown race

                case "CONN_CLOSE":
                    conn = tcp_registry.get(frame.conn_id)
                    if conn is not None:
                        conn.on_remote_closed()

                case "ERROR":
                    msg = decode_error_payload(frame.payload)
                    if frame.conn_id == SESSION_CONN_ID:
                        raise ConnectionClosedError(
                            f"Agent session error: {msg}",
                            details={"close_code": 1011, "close_reason": msg},
                        )
                    conn = tcp_registry.get(frame.conn_id)
                    if conn is not None:
                        conn.on_remote_closed()
                        conn.abort_upstream()


async def shutdown_all() -> None:
    """Hard-cancel all active connections on session teardown."""
    for conn in list(tcp_registry.values()):
        conn.abort()
    # Optionally wait for all cleanups
    await asyncio.gather(
        *(conn.closed_event.wait() for conn in tcp_registry.values()),
        return_exceptions=True,
    )
```

---

## 5. `UdpFlow` — Full API

### 5.1 Construction

```python
UdpFlow(
    flow_id: str,
host: str,
port: int,
ws_send: WsSendCallable,
registry: dict[str, UdpFlow],
)
```

#### Parameters

| Parameter  | Type                 | Required | Description                                                                                                                                            |
|------------|----------------------|----------|--------------------------------------------------------------------------------------------------------------------------------------------------------|
| `flow_id`  | `str`                | ✅        | Stable ID from `new_flow_id()`. Never construct manually. Never reuse after close.                                                                     |
| `host`     | `str`                | ✅        | Destination hostname or IP address. Must be frame-safe (no `:` separators). Validated by `encode_udp_open_frame` — a bad value raises `ProtocolError`. |
| `port`     | `int`                | ✅        | Destination UDP port (1–65535). Validated by `encode_udp_open_frame`.                                                                                  |
| `ws_send`  | `WsSendCallable`     | ✅        | Coroutine callable for sending frames. Same interface as TCP.                                                                                          |
| `registry` | `dict[str, UdpFlow]` | ✅        | Shared registry. Insert the handler yourself before calling `open()`.                                                                                  |

#### Construction Pattern

```python
flow_id = new_flow_id()

flow = UdpFlow(
    flow_id=flow_id,
    host=host,
    port=port,
    ws_send=ws_send,
    registry=udp_registry,
)

# Insert BEFORE open() — agent may send UDP_DATA before open() returns
udp_registry[flow_id] = flow

await flow.open()
```

---

### 5.2 Lifecycle Methods

#### `async open() → None`

Send the `UDP_OPEN` control frame to the agent.

```python
async def open(self) -> None
```

**Idempotency**: After a **successful** open, subsequent calls are no-ops. After a *
*failed** open (exception raised), `_opened` is not set — you may retry.

**`ProtocolError` propagation**: If `host` or `port` is frame-unsafe,
`encode_udp_open_frame` raises `ProtocolError`. This is a **caller bug** — do not catch
it. Fix the host/port values before constructing `UdpFlow`.

**Raises**:

| Exception                   | `error_code`                   | Meaning                    | Your action                    |
|-----------------------------|--------------------------------|----------------------------|--------------------------------|
| `TransportError`            | `transport.udp_open_on_closed` | Flow already closed        | Do not retry — create new flow |
| `ProtocolError`             | `protocol.error`               | Bad host/port — caller bug | Fix host/port — do not catch   |
| `WebSocketSendTimeoutError` | `transport.ws_send_timeout`    | Send stalled               | Retry or abort                 |
| `ConnectionClosedError`     | `transport.connection_closed`  | WS already closed          | Abort session                  |
| `TransportError`            | `transport.udp_open_failed`    | Unexpected send failure    | Retry or abort                 |

```python
try:
    await flow.open()
except WebSocketSendTimeoutError:
    # Transient — _opened is False, retry is safe
    await flow.close()  # clean up registry entry
    raise
except ConnectionClosedError:
    # Session is gone
    raise
```

---

#### `async close() → None`

Evict from registry and send `UDP_CLOSE` to the agent.

```python
async def close(self) -> None
```

**Idempotency**: Subsequent calls are no-ops.

**Close event**: Sets `_closed_event` before touching the network so `recv_datagram()`
unblocks immediately even if `ws_send` raises.

**Queued datagrams**: Any datagrams still in `_inbound` at close time are silently
abandoned — intentional UDP semantics.

**Raises**:

| Exception                   | `error_code`                 | Meaning                 | Your action                             |
|-----------------------------|------------------------------|-------------------------|-----------------------------------------|
| `WebSocketSendTimeoutError` | `transport.ws_send_timeout`  | Close frame stalled     | Log — agent will time out independently |
| `TransportError`            | `transport.udp_close_failed` | Unexpected send failure | Log — agent will time out independently |

`ConnectionClosedError` is **not raised** by `close()` — it is caught internally and
logged at `DEBUG`. The flow is considered closed regardless.

```python
try:
    await flow.close()
except WebSocketSendTimeoutError as exc:
    logger.warning(
        "UDP_CLOSE timed out for %s — agent will clean up",
        flow.flow_id,
        extra={"error": exc.to_dict()},
    )
```

---

#### `on_remote_closed() → None`

Signal that the agent has closed its side of the flow.

```python
def on_remote_closed(self) -> None
```

**What it does**:

* Sets `_closed = True`
* Evicts from registry
* Sets `_closed_event` so `recv_datagram()` unblocks

**Idempotency**: Safe to call multiple times. Subsequent calls only ensure
`_closed_event` is set.

**Safe before `open()`**: Can be called at any point in the lifecycle.

```python
# In recv_loop, on UDP_CLOSE frame
flow = udp_registry.get(frame.conn_id)
if flow is not None:
    flow.on_remote_closed()
```

---

### 5.3 Data Methods

#### `feed(data: bytes) → None`

Enqueue an inbound datagram from the agent.

```python
def feed(self, data: bytes) -> None
```

**Drop semantics**: If the inbound queue is full, the datagram is silently dropped and
`drop_count` is incremented. This is intentional UDP behaviour — do not treat drops as
errors.

**No-op when closed**: If the flow is already closed, the datagram is dropped silently (
feeding a closed flow would leave data in a queue that will never be drained).

**You must decode first**: Call `decode_binary_payload(frame.payload)` before passing
data here.

```python
# In recv_loop, on UDP_DATA frame
flow = udp_registry.get(frame.conn_id)
if flow is not None:
    data = decode_binary_payload(frame.payload)
    flow.feed(data)  # non-blocking, may drop silently
```

**Raises**:

| Exception        | `error_code`                     | Meaning              | Your action          |
|------------------|----------------------------------|----------------------|----------------------|
| `TransportError` | `transport.invalid_payload_type` | You passed non-bytes | Fix your decode step |

---

#### `async recv_datagram() → bytes | None`

Return the next inbound datagram, or `None` when the flow is closed and the queue is
empty.

```python
async def recv_datagram(self) -> bytes | None
```

**Returns one datagram per call.** After the flow closes, continue calling until `None`
is returned to drain all queued datagrams:

```python
# Correct drain loop — called by the proxy layer's UDP relay
while (datagram := await flow.recv_datagram()) is not None:
    await local_udp_socket.sendto(datagram, client_addr)
# Flow is closed and fully drained
```

**Fast path**: If a datagram is immediately available in the queue, it is returned
without suspending.

**Blocking path**: If the queue is empty and the flow is not closed, suspends until
either a datagram arrives or the flow closes.

**Task reuse**: Internally reuses a single `asyncio.Task` for the close-event wait
across all blocking calls. You do not need to manage this.

**Raises**:

| Exception                | Meaning                 | Your action               |
|--------------------------|-------------------------|---------------------------|
| `asyncio.CancelledError` | Your task was cancelled | Re-raise — never suppress |

---

#### `async send_datagram(data: bytes) → None`

Encode and forward a datagram to the agent.

```python
async def send_datagram(self, data: bytes) -> None
```

**Never split**: One datagram = one call. Do not split a datagram across multiple
`send_datagram()` calls. The proxy layer is responsible for ensuring datagrams fit
within the 6,108-byte payload budget.

**Raises**:

| Exception                   | `error_code`                     | Meaning              | Your action            |
|-----------------------------|----------------------------------|----------------------|------------------------|
| `TransportError`            | `transport.invalid_payload_type` | You passed non-bytes | Fix caller             |
| `WebSocketSendTimeoutError` | `transport.ws_send_timeout`      | Send stalled         | Log — datagram is lost |
| `ConnectionClosedError`     | `transport.connection_closed`    | WS closed            | Abort session          |
| `TransportError`            | `transport.udp_data_send_failed` | Unexpected failure   | Log — datagram is lost |

```python
try:
    await flow.send_datagram(datagram)
except (WebSocketSendTimeoutError, TransportError) as exc:
    # UDP is best-effort — log and continue
    logger.warning(
        "UDP datagram lost for %s: %s",
        flow.flow_id,
        exc.error_code,
        extra={"error": exc.to_dict()},
    )
except ConnectionClosedError:
    # Session is gone — propagate upward
    raise
```

---

### 5.4 Properties

| Property     | Type   | Description                                                                                                    |
|--------------|--------|----------------------------------------------------------------------------------------------------------------|
| `flow_id`    | `str`  | The stable flow ID passed at construction.                                                                     |
| `is_opened`  | `bool` | `True` once `open()` has completed successfully.                                                               |
| `is_closed`  | `bool` | `True` once `close()` or `on_remote_closed()` has been called.                                                 |
| `drop_count` | `int`  | Total inbound datagrams dropped due to a full queue.                                                           |
| `bytes_sent` | `int`  | Total raw bytes successfully forwarded to the agent (outbound).                                                |
| `bytes_recv` | `int`  | Total raw bytes received from the agent (inbound), including datagrams that were later dropped from the queue. |

---

### 5.5 Exception Reference

| Method            | Exception                   | `error_code`                     | Retryable | Your action                  |
|-------------------|-----------------------------|----------------------------------|-----------|------------------------------|
| `open()`          | `TransportError`            | `transport.udp_open_on_closed`   | No        | Create new flow              |
| `open()`          | `ProtocolError`             | `protocol.error`                 | No        | Fix host/port — do not catch |
| `open()`          | `WebSocketSendTimeoutError` | `transport.ws_send_timeout`      | Yes       | Retry or abort               |
| `open()`          | `ConnectionClosedError`     | `transport.connection_closed`    | Yes       | Abort session                |
| `open()`          | `TransportError`            | `transport.udp_open_failed`      | Yes       | Retry or abort               |
| `close()`         | `WebSocketSendTimeoutError` | `transport.ws_send_timeout`      | No        | Log — agent cleans up        |
| `close()`         | `TransportError`            | `transport.udp_close_failed`     | No        | Log — agent cleans up        |
| `feed()`          | `TransportError`            | `transport.invalid_payload_type` | No        | Fix caller                   |
| `recv_datagram()` | `asyncio.CancelledError`    | —                                | —         | Re-raise                     |
| `send_datagram()` | `TransportError`            | `transport.invalid_payload_type` | No        | Fix caller                   |
| `send_datagram()` | `WebSocketSendTimeoutError` | `transport.ws_send_timeout`      | No        | Log — datagram lost          |
| `send_datagram()` | `ConnectionClosedError`     | `transport.connection_closed`    | Yes       | Abort session                |
| `send_datagram()` | `TransportError`            | `transport.udp_data_send_failed` | No        | Log — datagram lost          |

---

### 5.6 Complete Usage Example

```python
import asyncio
from exectunnel.exceptions import (
    ConnectionClosedError,
    TransportError,
    WebSocketSendTimeoutError,
)
from exectunnel.protocol import (
    new_flow_id,
    decode_binary_payload,
    parse_host_port,
    SESSION_CONN_ID,
)
from exectunnel.transport import UdpFlow

udp_registry: dict[str, UdpFlow] = {}


async def handle_udp_associate(
        host: str,
        port: int,
        local_udp_socket: asyncio.DatagramTransport,
        ws_send: WsSendCallable,
) -> None:
    """Called by the proxy layer when a SOCKS5 UDP ASSOCIATE is accepted."""
    flow_id = new_flow_id()

    flow = UdpFlow(
        flow_id=flow_id,
        host=host,
        port=port,
        ws_send=ws_send,
        registry=udp_registry,
    )

    # Insert BEFORE open() — agent may send UDP_DATA before open() returns
    udp_registry[flow_id] = flow

    await flow.open()

    # Spawn the inbound relay task
    asyncio.create_task(
        _inbound_relay(flow, local_udp_socket),
        name=f"udp-relay-{flow_id}",
    )


async def _inbound_relay(
        flow: UdpFlow,
        local_udp_socket: asyncio.DatagramTransport,
) -> None:
    """Drain inbound datagrams from the agent to the local UDP client."""
    while (datagram := await flow.recv_datagram()) is not None:
        local_udp_socket.sendto(datagram)
    # Flow is closed and fully drained — relay task exits cleanly


async def recv_loop_udp_section(frame: ParsedFrame) -> None:
    """UDP frame handling inside the main recv_loop."""
    match frame.msg_type:
        case "UDP_DATA":
            flow = udp_registry.get(frame.conn_id)
            if flow is None:
                return
            data = decode_binary_payload(frame.payload)
            flow.feed(data)  # non-blocking, may drop silently

        case "UDP_CLOSE":
            flow = udp_registry.get(frame.conn_id)
            if flow is not None:
                flow.on_remote_closed()


async def relay_outbound_datagram(flow_id: str, datagram: bytes) -> None:
    """Called by the proxy layer's UDP relay when a local datagram arrives."""
    flow = udp_registry.get(flow_id)
    if flow is None or flow.is_closed:
        return

    try:
        await flow.send_datagram(datagram)
    except (WebSocketSendTimeoutError, TransportError) as exc:
        # UDP is best-effort — log and continue
        logger.warning(
            "UDP datagram lost for flow %s: %s",
            flow_id,
            exc.error_code,
            extra={"error": exc.to_dict()},
        )
    except ConnectionClosedError:
        raise  # session is gone — propagate


async def shutdown_all_udp() -> None:
    """Close all active UDP flows on session teardown."""
    for flow in list(udp_registry.values()):
        try:
            await flow.close()
        except (WebSocketSendTimeoutError, TransportError):
            pass  # best-effort on shutdown
```

---

## 6. Registry Management

### Rules

```
INSERT   You insert the handler into the registry immediately after
         construction, before sending any frame to the agent.

         tcp_registry[conn_id] = conn   # before encode_conn_open_frame
         udp_registry[flow_id] = flow   # before flow.open()

LOOKUP   Look up by conn_id / flow_id on every inbound frame.
         A missing key means the handler was already cleaned up — skip silently.

         conn = tcp_registry.get(frame.conn_id)
         if conn is None:
             continue

REMOVE   Handlers self-evict. Never call del tcp_registry[conn_id] yourself.
         Self-eviction happens in:
           TcpConnection._cleanup()          (after both tasks finish)
           UdpFlow._evict()                  (called by close() and on_remote_closed())

NEVER    Do not store SESSION_CONN_ID as a registry key.
         SESSION_CONN_ID is reserved for session-level ERROR frames only.
```

### Registry Lifecycle Diagram

```
                  You                          Handler
                   │                              │
  construction     │──── tcp_registry[id] = conn ─┤
                   │                              │
  CONN_OPEN sent   │──── ws_send(open_frame) ─────┤
                   │                              │
  agent ACK        │──── conn.start() ────────────┤
                   │                              │
  DATA frames      │──── conn.feed_async(data) ───┤
                   │                              │
  CONN_CLOSE       │──── conn.on_remote_closed() ─┤
                   │                              │
  cleanup          │                              │──── registry.pop(id) ──►
                   │                              │     (self-eviction)
                   │                              │
  after cleanup    │──── tcp_registry.get(id)     │
                   │     → None                   │
```

### Handling Unknown IDs

```python
# Always use .get() — never [] — for registry lookups
conn = tcp_registry.get(frame.conn_id)
if conn is None:
    # Handler already cleaned up — this is normal, not an error
    logger.debug(
        "DATA frame for unknown conn_id %s — already cleaned up", frame.conn_id
    )
    continue
```

---

## 7. Error Handling Patterns

### Pattern 1 — Recv Loop Dispatch

```python
async for raw in websocket:
    for line in raw.splitlines():
        try:
            frame = parse_frame(line)
        except FrameDecodingError as exc:
            # Protocol violation from agent — close the session
            raise ConnectionClosedError(
                "Agent sent corrupt frame",
                details={"close_code": 1002, "close_reason": exc.message},
            ) from exc

        if frame is None:
            continue  # shell noise — log at DEBUG only, never WARNING

        match frame.msg_type:
            case "DATA":
                conn = tcp_registry.get(frame.conn_id)
                if conn is None:
                    continue
                data = decode_binary_payload(frame.payload)
                try:
                    await conn.feed_async(data)
                except ConnectionClosedError:
                    pass  # normal teardown race — not an error

            case "CONN_CLOSE":
                conn = tcp_registry.get(frame.conn_id)
                if conn is not None:
                    conn.on_remote_closed()

            case "ERROR":
                msg = decode_error_payload(frame.payload)
                if frame.conn_id == SESSION_CONN_ID:
                    raise ConnectionClosedError(
                        f"Agent session error: {msg}",
                        details={"close_code": 1011, "close_reason": msg},
                    )
                conn = tcp_registry.get(frame.conn_id)
                if conn is not None:
                    conn.on_remote_closed()
                    conn.abort_upstream()

            case "UDP_DATA":
                flow = udp_registry.get(frame.conn_id)
                if flow is None:
                    continue
                data = decode_binary_payload(frame.payload)
                flow.feed(data)

            case "UDP_CLOSE":
                flow = udp_registry.get(frame.conn_id)
                if flow is not None:
                    flow.on_remote_closed()
```

### Pattern 2 — Retry-Aware `open()`

```python
async def open_udp_flow_with_retry(
        flow: UdpFlow,
        max_attempts: int = 3,
        backoff_s: float = 1.0,
) -> None:
    for attempt in range(1, max_attempts + 1):
        try:
            await flow.open()
            return
        except WebSocketSendTimeoutError as exc:
            if attempt == max_attempts:
                raise
            logger.warning(
                "UDP_OPEN timed out (attempt %d/%d), retrying in %.1fs",
                attempt,
                max_attempts,
                backoff_s,
                extra={"error": exc.to_dict()},
            )
            await asyncio.sleep(backoff_s)
        except ConnectionClosedError:
            raise  # session is gone — no point retrying
```

### Pattern 3 — Graceful Session Shutdown

```python
async def shutdown_session(
        tcp_registry: dict[str, TcpConnection],
        udp_registry: dict[str, UdpFlow],
        grace_period_s: float = 5.0,
) -> None:
    # 1. Signal all TCP connections to stop
    for conn in list(tcp_registry.values()):
        conn.abort()

    # 2. Close all UDP flows (best-effort)
    for flow in list(udp_registry.values()):
        with contextlib.suppress(Exception):
            await flow.close()

    # 3. Wait for TCP cleanup with a deadline
    if tcp_registry:
        try:
            async with asyncio.timeout(grace_period_s):
                await asyncio.gather(
                    *(conn.closed_event.wait() for conn in tcp_registry.values()),
                    return_exceptions=True,
                )
        except TimeoutError:
            logger.warning(
                "%d TCP connections did not clean up within %.1fs",
                len(tcp_registry),
                grace_period_s,
            )
```

### Pattern 4 — Structured Error Logging

```python
# Always use exc.to_dict() for structured logging
except ExecTunnelError as exc:
logger.error(
    "transport failure on conn %s",
    conn_id,
    extra={"error": exc.to_dict()},
)

# Match on error_code — never on message strings
except TransportError as exc:
match exc.error_code:
    case "transport.pre_ack_buffer_overflow":
        # Fatal for this connection — tear down
        await conn.close_unstarted()
    case "transport.inbound_queue_full":
        # Non-fatal — log and continue
        logger.warning("queue full", extra={"error": exc.to_dict()})
    case _:
        raise  # unexpected — propagate
```

### Pattern 5 — Checking `retryable`

```python
except ExecTunnelError as exc:
if exc.retryable:
    await asyncio.sleep(backoff)
    continue
raise
```

---

## 8. Checklist — Before You Ship

Use this checklist when reviewing session-layer or proxy-layer code that uses the
transport package.

### Construction

- [ ] `conn_id` comes from `new_conn_id()` — never constructed manually
- [ ] `flow_id` comes from `new_flow_id()` — never constructed manually
- [ ] Handler inserted into registry **before** sending `CONN_OPEN` / calling `open()`
- [ ] `ws_send` implementation raises only `WebSocketSendTimeoutError` or
  `ConnectionClosedError` on failure
- [ ] `ws_send` implementation is safe for concurrent calls from the same event loop

### TCP Lifecycle

- [ ] `start()` called at most once per `TcpConnection`
- [ ] `close_unstarted()` only called when `is_started` is `False`
- [ ] `on_remote_closed()` called on every `CONN_CLOSE` and `ERROR` frame
- [ ] `abort_upstream()` called on `ERROR` frames (not just `on_remote_closed()`)
- [ ] `abort()` used for hard shutdown — not `close_unstarted()`

### UDP Lifecycle

- [ ] `open()` called before any `send_datagram()` or `feed()` calls
- [ ] `recv_datagram()` loop continues until `None` is returned — not stopped on first
  `None`
- [ ] Datagrams are never split across multiple `send_datagram()` calls
- [ ] `close()` called on local teardown; `on_remote_closed()` called on `UDP_CLOSE`
  frame

### Data Handling

- [ ] `decode_binary_payload()` called in the session layer **before** `feed()` /
  `feed_async()`
- [ ] `feed_async()` used post-ACK for TCP (not `feed()`) to enable backpressure
- [ ] `ConnectionClosedError` from `feed_async()` is caught and discarded (not
  re-raised)
- [ ] `asyncio.CancelledError` from `feed_async()` / `recv_datagram()` is re-raised

### Registry

- [ ] All registry lookups use `.get()` — never `[]`
- [ ] Missing registry key is treated as a no-op — not an error
- [ ] `SESSION_CONN_ID` is never stored as a registry key
- [ ] Registry entries are never manually deleted — self-eviction only

### Error Handling

- [ ] `ProtocolError` from `UdpFlow.open()` is **not caught** — it is a caller bug
- [ ] `FrameDecodingError` from `parse_frame()` is always propagated — never silently
  discarded
- [ ] `parse_frame()` returning `None` is logged at `DEBUG` only — never `WARNING` or
  above
- [ ] All `ExecTunnelError` catches match on `error_code` — never on message strings
- [ ] Structured logging uses `exc.to_dict()` — never `str(exc)`

