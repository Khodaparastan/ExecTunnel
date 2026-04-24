# ExecTunnel Proxy Package — Developer API Reference

```
exectunnel/proxy/  |  api-doc v2.1  |  Python 3.13+
```

---

## Quick-Start

```python
from exectunnel.proxy import Socks5ServerConfig, Socks5Server, TCPRelay, UDPRelay
```

These are the only four symbols you ever import. Everything else is internal.

---

## Mental Model

Before reading the API, fix this picture in your head:

```
Your session layer code
        │
        │  async for req in server:
        │      asyncio.create_task(handle(req))
        │
        ▼
┌───────────────────────────────────────────────────────┐
│  Socks5Server                                         │
│  ─────────────────────────────────────────────────── │
│  Binds TCP :1080.  Negotiates SOCKS5 handshakes.      │
│  Enqueues one TCPRelay per completed handshake.       │
└───────────────────────────────────────────────────────┘
        │
        │  yields TCPRelay objects
        ▼
┌───────────────────────────────────────────────────────┐
│  TCPRelay                                             │
│  ─────────────────────────────────────────────────── │
│  Owns the client TCP writer.                          │
│  Carries cmd / host / port.                           │
│  For UDP_ASSOCIATE: carries a bound UDPRelay.         │
│  You MUST send exactly one reply, then relay data.    │
└───────────────────────────────────────────────────────┘
        │
        │  (UDP_ASSOCIATE only)
        ▼
┌───────────────────────────────────────────────────────┐
│  UDPRelay                                             │
│  ─────────────────────────────────────────────────── │
│  Bound UDP socket on 127.0.0.1:<ephemeral>.           │
│  recv() → (payload, host, port)                       │
│  send_to_client(payload, src_host, src_port)          │
└───────────────────────────────────────────────────────┘
```

The proxy layer hands you a `TCPRelay`. You open a tunnel connection (session layer),
send the reply, then relay bytes. The proxy layer never touches the tunnel — that is
entirely your responsibility.

---

## `Socks5Server`

### Constructor

```python
Socks5Server(config: Socks5ServerConfig | None = None)
```

Pass a `Socks5ServerConfig` to customise behaviour. Omit it to use all defaults.

```python
from exectunnel.proxy import Socks5ServerConfig, Socks5Server

config = Socks5ServerConfig(
    host="127.0.0.1",
    port=1080,
    handshake_timeout=30.0,
    request_queue_capacity=256,
    udp_relay_queue_capacity=2_048,
    queue_put_timeout=5.0,
    udp_drop_warn_interval=1_000,
)
server = Socks5Server(config)
```

#### `Socks5ServerConfig` fields

| Field                      | Type    | Default       | Description                                                             |
|----------------------------|---------|---------------|-------------------------------------------------------------------------|
| `host`                     | `str`   | `"127.0.0.1"` | Bind address. Binding to anything non-loopback logs a `WARNING`.        |
| `port`                     | `int`   | `1080`        | TCP port to listen on. Must be in `[1, 65535]`.                         |
| `handshake_timeout`        | `float` | `30.0`        | Seconds allowed for a single SOCKS5 handshake.                          |
| `request_queue_capacity`   | `int`   | `256`         | Max completed handshakes buffered before backpressure.                  |
| `udp_relay_queue_capacity` | `int`   | `2_048`       | Max inbound datagrams buffered per `UDPRelay` instance before dropping. |
| `queue_put_timeout`        | `float` | `5.0`         | Max seconds to wait when enqueueing a completed handshake.              |
| `udp_drop_warn_interval`   | `int`   | `1_000`       | Log a `WARNING` every N UDP queue-full drops to avoid log flooding.     |

All fields are validated at construction time — `ConfigurationError` is raised on
invalid values.

> **`request_queue_capacity` vs `udp_relay_queue_capacity`:** These are two distinct
> bounded queues. `request_queue_capacity` caps the number of fully-negotiated TCP
> handshakes waiting to be consumed by your `async for` loop. `udp_relay_queue_capacity`
> caps the per-session inbound UDP datagram buffer inside each `UDPRelay` instance.

---

### Lifecycle methods

#### `await server.start() → None`

Binds the TCP listen socket and begins accepting connections.

```python
server = Socks5Server(Socks5ServerConfig(host="127.0.0.1", port=1080))
await server.start()
```

**Raises**

| Exception        | When                                                 |
|------------------|------------------------------------------------------|
| `RuntimeError`   | Called more than once on the same instance.          |
| `TransportError` | OS refused to bind (port in use, permission denied). |

> After `start()` returns the server is immediately accepting connections. Handshakes
> run concurrently in the background — you do not need to do anything to enable that.

---

#### `await server.stop() → None`

Closes the listen socket, force-closes all mid-handshake writers, cancels all in-flight
handshake tasks, drains and closes any unconsumed `TCPRelay` objects, then signals the
`async for` iterator to stop.

```python
await server.stop()
```

Safe to call before `start()` — a no-op. Idempotent.

> Always call `stop()` (or use the async context manager) to avoid leaking the listen
> socket and any in-flight writer handles.

---

### Async context manager

```python
async with Socks5Server(Socks5ServerConfig(host="127.0.0.1", port=1080)) as server:
    async for req in server:
        asyncio.create_task(handle(req))
# stop() is called automatically on exit, even if handle() raises
```

`__aenter__` calls `start()`. `__aexit__` calls `stop()`.

---

### Async iteration

```python
async for req in server:
    asyncio.create_task(handle(req))
```

Yields `TCPRelay` objects one at a time as handshakes complete. Stops when `stop()` is
called.

**Critical:** spawn each request as a `Task` immediately. Do not `await` anything inside
the loop body — that blocks the accept loop and starves all other clients.

```python
# ✓ correct
async for req in server:
    asyncio.create_task(handle(req))

# ✗ wrong — blocks the accept loop
async for req in server:
    await handle(req)
```

---

### Complete server example

```python
import asyncio
from exectunnel.proxy import Socks5Server, Socks5ServerConfig, TCPRelay
from exectunnel.exceptions import ExecTunnelError


async def handle(req: TCPRelay) -> None:
    async with req:
        if req.is_connect:
            await handle_connect(req)
        elif req.is_udp_associate:
            await handle_udp(req)


async def main() -> None:
    async with Socks5Server(Socks5ServerConfig(host="127.0.0.1", port=1080)) as server:
        async for req in server:
            asyncio.create_task(handle(req))


asyncio.run(main())
```

---

## `TCPRelay`

You never construct `TCPRelay` directly. You receive it from `async for req in server`.

### Attributes

| Attribute   | Type                   | Description                                                                                                                                  |
|-------------|------------------------|----------------------------------------------------------------------------------------------------------------------------------------------|
| `cmd`       | `Cmd`                  | The SOCKS5 command. Use `is_connect` / `is_udp_associate` instead of comparing directly.                                                     |
| `host`      | `str`                  | Destination hostname or IP address. Already validated and safe to pass to the session layer.                                                 |
| `port`      | `int`                  | Destination port. Always in `[1, 65535]` for `CONNECT`. For `UDP_ASSOCIATE`, port 0 in the original request is permitted and reflected here. |
| `reader`    | `asyncio.StreamReader` | Client TCP stream. Read from this after sending a success reply.                                                                             |
| `writer`    | `asyncio.StreamWriter` | Client TCP stream. Do not write to this directly — use the reply methods.                                                                    |
| `udp_relay` | `UDPRelay \| None`     | Bound relay for `UDP_ASSOCIATE`. Always `None` for `CONNECT`.                                                                                |
| `replied`   | `bool`                 | `True` once a reply has been sent. Read-only.                                                                                                |

---

### Command predicates

```python
req.is_connect  # True when cmd == Cmd.CONNECT
req.is_udp_associate  # True when cmd == Cmd.UDP_ASSOCIATE
```

Use these instead of `req.cmd == Cmd.CONNECT` — they are more readable and insulate you
from enum import boilerplate.

---

### Async context manager

```python
async with req:
    await req.send_reply_success()
    # … relay data …
# close() is called automatically — writer and relay are cleaned up
```

Always use `async with req` or call `await req.close()` explicitly. Never rely on
garbage collection to close the writer.

---

### Reply methods

You **must** call exactly one of `send_reply_success` or `send_reply_error` per request.
Calling neither leaks the connection. Calling both raises `ProtocolError`.

---

#### `await req.send_reply_success(bind_host="127.0.0.1", bind_port=0) → None`

Sends a SOCKS5 `SUCCESS` reply and flushes the writer. Does **not** close the writer —
you own the connection after this call.

```python
await req.send_reply_success()
# now relay data via req.reader / req.writer
```

For `UDP_ASSOCIATE` you **must** pass the relay port:

```python
await req.send_reply_success(bind_port=req.udp_relay.local_port)
```

Forgetting `bind_port` for UDP means the client sends datagrams to port 0 and the relay
never receives them.

**Parameters**

| Parameter   | Default       | Description                                                                                  |
|-------------|---------------|----------------------------------------------------------------------------------------------|
| `bind_host` | `"127.0.0.1"` | `BND.ADDR` advertised to the client. Must be a valid IP address — domain names are rejected. |
| `bind_port` | `0`           | `BND.PORT` advertised to the client. For `UDP_ASSOCIATE`: `req.udp_relay.local_port`.        |

**Raises**

| Exception            | When                                       |
|----------------------|--------------------------------------------|
| `ProtocolError`      | Writer is already closing when called.     |
| `ProtocolError`      | Called a second time (double-reply guard). |
| `ConfigurationError` | `bind_host` is not a valid IP address.     |
| `ConfigurationError` | `bind_port` outside `[0, 65535]`.          |
| `OSError`            | Underlying socket write or drain failed.   |

---

#### `await req.send_reply_error(reply=Reply.GENERAL_FAILURE) → None`

Sends a SOCKS5 error reply, flushes the writer, then closes the writer and any UDP
relay. Use this when you cannot fulfil the request.

```python
try:
    tunnel_id = await session.open_connection(req.host, req.port)
except ExecTunnelError:
    await req.send_reply_error(Reply.HOST_UNREACHABLE)
    return
```

Drain and close always run in the `finally` block — even when a double-reply is
detected, cleanup completes before `ProtocolError` propagates.

**Parameters**

| Parameter | Default                 | Description                    |
|-----------|-------------------------|--------------------------------|
| `reply`   | `Reply.GENERAL_FAILURE` | The SOCKS5 error code to send. |

**Common reply codes**

| `Reply` member                 | Wire value | Use when                           |
|--------------------------------|------------|------------------------------------|
| `Reply.GENERAL_FAILURE`        | `0x01`     | Unclassified tunnel error          |
| `Reply.CONNECTION_NOT_ALLOWED` | `0x02`     | Policy rejection                   |
| `Reply.NETWORK_UNREACHABLE`    | `0x03`     | No route to destination network    |
| `Reply.HOST_UNREACHABLE`       | `0x04`     | DNS resolved but host unreachable  |
| `Reply.CONNECTION_REFUSED`     | `0x05`     | Target port refused the connection |
| `Reply.TTL_EXPIRED`            | `0x06`     | Hop limit exceeded                 |
| `Reply.CMD_NOT_SUPPORTED`      | `0x07`     | Command not implemented            |
| `Reply.ADDR_NOT_SUPPORTED`     | `0x08`     | Address type not supported         |

```python
from exectunnel.protocol import Reply

await req.send_reply_error(Reply.HOST_UNREACHABLE)
```

**Raises**

| Exception            | When                                                                         |
|----------------------|------------------------------------------------------------------------------|
| `ProtocolError`      | Called a second time (double-reply guard).                                   |
| `ConfigurationError` | `reply` produces an invalid `build_socks5_reply` call — always a caller bug. |

---

#### `await req.close() → None`

Closes the writer and any associated UDP relay. Idempotent — safe to call multiple
times. `OSError` and `RuntimeError` are suppressed.

Called automatically by `__aexit__` and `send_reply_error`. You only need to call this
directly if you are not using the context manager and not calling `send_reply_error`.

---

### CONNECT handler pattern

```python
from exectunnel.proxy import TCPRelay
from exectunnel.exceptions import ExecTunnelError
from exectunnel.protocol import Reply


async def handle_connect(req: TCPRelay) -> None:
    async with req:
        # 1. Open the tunnel connection BEFORE sending the reply.
        #    If this fails, send an error reply and return.
        try:
            conn_id = await session.open_connection(req.host, req.port)
        except ExecTunnelError:
            await req.send_reply_error(Reply.HOST_UNREACHABLE)
            return

        # 2. Send the success reply. After this the client starts sending data.
        await req.send_reply_success()

        # 3. Relay data in both directions until one side closes.
        await relay_tcp(req.reader, req.writer, conn_id, session)
        # close() is called by __aexit__
```

---

### UDP_ASSOCIATE handler pattern

```python
from exectunnel.proxy import TCPRelay
from exectunnel.protocol import Reply


async def handle_udp(req: TCPRelay) -> None:
    async with req:
        relay = req.udp_relay  # already bound by the server

        # 1. Send the success reply with the relay's ephemeral port.
        #    The client will send datagrams to 127.0.0.1:<local_port>.
        await req.send_reply_success(
            bind_host="127.0.0.1",
            bind_port=relay.local_port,
        )

        # 2. Relay datagrams until the TCP control connection closes
        #    or the relay is shut down.
        await relay_udp(relay, session, req.reader)
        # close() is called by __aexit__ — also closes the relay
```

---

## `UDPRelay`

You never construct `UDPRelay` directly in normal usage. The server creates and starts
it for you inside `_handshake._build_udp_request()`; you receive it via `req.udp_relay`.

The only case where you construct it manually is in tests or if you are building a
custom server that bypasses `Socks5Server`.

---

### Constructor

```python
UDPRelay(
    *,
    queue_capacity: int = 2_048,
drop_warn_interval: int = 1_000,
)
```

| Parameter            | Description                                                                                    |
|----------------------|------------------------------------------------------------------------------------------------|
| `queue_capacity`     | Max inbound datagrams buffered before dropping. Tune upward for high-throughput UDP workloads. |
| `drop_warn_interval` | Log a `WARNING` every N queue-full drops to avoid log flooding.                                |

---

### Lifecycle

#### `await relay.start(expected_client_addr=None) → int`

Binds the UDP socket on `127.0.0.1:<ephemeral>` and returns the port.

In normal usage this is called by the server's internal handshake logic before the
`TCPRelay` is enqueued. You do not call this yourself unless you are constructing
`UDPRelay` manually.

```python
relay = UDPRelay()
port = await relay.start()
# relay.local_port == port
```

**Parameters**

| Parameter              | Type                      | Description                                                                                                                                                                                                                                                                                     |
|------------------------|---------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `expected_client_addr` | `tuple[str, int] \| None` | Optional `(host, port)` hint from the `UDP_ASSOCIATE` request. When set, non-unspecified, and non-zero-port, datagrams from other addresses are dropped before the first datagram binds the client address. Hints with an unspecified host (`0.0.0.0`/`::`) or port `0` are silently discarded. |

**Returns** the ephemeral port number.

**Raises**

| Exception        | When                                        |
|------------------|---------------------------------------------|
| `RuntimeError`   | Called more than once on the same instance. |
| `TransportError` | OS refused to bind the UDP socket.          |

---

#### `relay.close() → None`

Closes the UDP socket and unblocks any coroutine awaiting `recv()`. Idempotent.

```python
relay.close()
```

After `close()`, `recv()` drains any remaining queued items and then returns `None` to
signal EOF.

---

### Async context manager

```python
async with UDPRelay() as relay:
    port = relay.local_port
    # … relay data …
# close() called automatically
```

`__aenter__` calls `start()`. `__aexit__` calls `close()`.

---

### Data methods

#### `await relay.recv() → tuple[bytes, str, int] | None`

Returns the next `(payload, dst_host, dst_port)` from the SOCKS5 client.

Blocks until a datagram is available or the relay is closed. Returns `None` when the
relay has been closed and the queue is fully drained — treat this as EOF and stop the
relay loop.

```python
while True:
    item = await relay.recv()
    if item is None:
        break  # relay closed — stop
    payload, host, port = item
    await session.send_udp(host, port, payload)
```

The `payload` is the raw datagram bytes with the SOCKS5 UDP header already stripped.
`host` and `port` are the **destination** the client wants to reach.

**Returns** `(payload, dst_host, dst_port)` or `None`.

**Raises**

| Exception                | When                                                      |
|--------------------------|-----------------------------------------------------------|
| `RuntimeError`           | Called before `start()`.                                  |
| `asyncio.CancelledError` | Task was cancelled — always propagated, never suppressed. |

---

#### `relay.send_to_client(payload, src_host, src_port) → None`

Wraps `payload` in a SOCKS5 UDP header and sends it back to the SOCKS5 client.

Call this when the remote side sends a UDP datagram back toward the client. `src_host`
and `src_port` are the **source** of the reply (the remote service's address), not the
client's address.

```python
# Datagram arrived from the remote service:
relay.send_to_client(
    payload=datagram_bytes,
    src_host="10.0.0.5",  # where the reply came from
    src_port=53,  # source port of the reply
)
```

**Silently drops** (with a `DEBUG` log) if:

* The relay is closed.
* The client address has not yet been bound (no datagram received yet).
* `src_host` is not a valid IP address string — RFC 1928 §7 requires IP addresses in UDP
  reply headers; domain names are not permitted.

**Raises**

| Exception                                                             | When                                          |
|-----------------------------------------------------------------------|-----------------------------------------------|
| `TransportError` (`error_code="proxy.udp_send.invalid_payload_type"`) | `payload` is not `bytes` — caller bug.        |
| `TransportError` (`error_code="proxy.udp_send.port_out_of_range"`)    | `src_port` outside `[0, 65535]` — caller bug. |

---

### Properties

| Property               | Type   | Description                                                                          |
|------------------------|--------|--------------------------------------------------------------------------------------|
| `local_port`           | `int`  | Ephemeral port the relay is bound to. `0` before `start()`.                          |
| `is_running`           | `bool` | `True` after `start()` and before `close()`, and while the transport is not closing. |
| `accepted_count`       | `int`  | Total datagrams successfully enqueued.                                               |
| `drop_count`           | `int`  | Total datagrams dropped due to full queue.                                           |
| `foreign_client_count` | `int`  | Total datagrams dropped from unexpected source addresses.                            |

---

### Complete UDP relay loop pattern

```python
import asyncio
from exectunnel.proxy import TCPRelay


async def relay_udp(
        req: TCPRelay,
        session,
) -> None:
    relay = req.udp_relay

    async def pump_inbound() -> None:
        """Client → tunnel: read from relay, forward to session."""
        while True:
            item = await relay.recv()
            if item is None:
                return
            payload, host, port = item
            await session.send_udp(host, port, payload)

    async def pump_outbound() -> None:
        """Tunnel → client: read from session, forward to relay."""
        # session.udp_datagrams() is illustrative — replace with your
        # session layer's actual async datagram source
        async for datagram, src_host, src_port in session.udp_datagrams():
            relay.send_to_client(datagram, src_host, src_port)

    async def watch_control() -> None:
        """Stop when the TCP control connection closes (RFC 1928 §7)."""
        await req.reader.read()  # returns b"" on close

    tasks = {
        asyncio.create_task(pump_inbound(), name="udp-inbound"),
        asyncio.create_task(pump_outbound(), name="udp-outbound"),
        asyncio.create_task(watch_control(), name="udp-control"),
    }
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception():
                raise task.exception()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
```

---

## Exception Handling Guide

### What the proxy layer raises toward you

The proxy layer handles all SOCKS5 negotiation errors internally. By the time a
`TCPRelay` reaches your handler, the handshake has already succeeded. The exceptions
below are what you may encounter **in your handler code**:

| Exception            | Source                                                 | When you see it                                     |
|----------------------|--------------------------------------------------------|-----------------------------------------------------|
| `ProtocolError`      | `req.send_reply_success()`                             | Writer closed between handshake and your reply call |
| `ProtocolError`      | `req.send_reply_success()` or `req.send_reply_error()` | You called a reply method twice                     |
| `ConfigurationError` | `req.send_reply_success()`                             | You passed an invalid `bind_host` or `bind_port`    |
| `ConfigurationError` | `req.send_reply_error()`                               | You passed an invalid `Reply` value                 |
| `TransportError`     | `relay.send_to_client()`                               | You passed non-`bytes` payload or out-of-range port |
| `RuntimeError`       | `relay.recv()`                                         | You called `recv()` before `start()`                |
| `TransportError`     | `server.start()`                                       | OS refused to bind the listen socket                |
| `OSError`            | `req.send_reply_success()`                             | Socket write or drain failed                        |

### What the proxy layer never raises toward you

* `FrameDecodingError` — no frame decoding happens in the proxy layer.
* `WebSocketSendTimeoutError` — no WebSocket in the proxy layer.
* `ProtocolError` from SOCKS5 negotiation — all negotiation errors are caught internally
  and never propagate out of `async for req in server`.

### Recommended handler structure

```python
from exectunnel.proxy import TCPRelay
from exectunnel.exceptions import (
    ExecTunnelError,
    ProtocolError,
    TransportError,
)
from exectunnel.protocol import Reply
import logging

logger = logging.getLogger(__name__)


async def handle(req: TCPRelay) -> None:
    async with req:
        try:
            if req.is_connect:
                await _handle_connect(req)
            elif req.is_udp_associate:
                await _handle_udp(req)
            else:
                # Should never happen — server rejects BIND before enqueuing
                await req.send_reply_error(Reply.CMD_NOT_SUPPORTED)

        except ProtocolError as exc:
            # Client disconnected mid-reply or double-reply bug.
            # send_reply_error is not safe here — writer may be closing.
            logger.debug(
                "socks5 handler protocol error [%s]: %s",
                exc.error_code,
                exc.message,
            )

        except ExecTunnelError as exc:
            # Tunnel-layer failure — try to tell the client.
            logger.warning(
                "socks5 handler tunnel error [%s]: %s (error_id=%s)",
                exc.error_code,
                exc.message,
                exc.error_id,
            )
            if not req.replied:
                await req.send_reply_error(Reply.GENERAL_FAILURE)

        except OSError as exc:
            logger.debug("socks5 handler I/O error: %s", exc)
            if not req.replied:
                await req.send_reply_error(Reply.GENERAL_FAILURE)
```

> Use `req.replied` to guard `send_reply_error` in exception handlers — if
`send_reply_success` already succeeded, calling `send_reply_error` raises
`ProtocolError` (double-reply).

---

## Invariants Checklist

Before shipping any code that consumes this API, verify:

```
□  send_reply_success() OR send_reply_error() called exactly once per request.

□  For UDP_ASSOCIATE: bind_port=req.udp_relay.local_port passed to
   send_reply_success() — not 0, not 1080, not any other value.

□  async with req (or explicit close()) used in every handler — no bare
   try/finally that might skip close().

□  asyncio.create_task(handle(req)) used in the accept loop — never
   await handle(req) directly inside async for.

□  relay.recv() loop checks for None return and breaks — not an infinite loop.

□  send_to_client() called with the remote service's address as src_host /
   src_port — not the client's address, not a domain name.

□  One datagram = one send_to_client() call — never split a UDP datagram.

□  req.replied checked before calling send_reply_error() in exception
   handlers — prevents double-reply ProtocolError masking the real error.

□  Socks5ServerConfig constructed with request_queue_capacity (not
   queue_capacity) — incorrect field names raise TypeError at construction.

□  UDPRelay.recv() not called before start() — raises RuntimeError.

□  UDPRelay.start() not called more than once — raises RuntimeError.
```

---

## API at a Glance

```
Socks5ServerConfig
├── __init__(host, port, handshake_timeout,
│           request_queue_capacity, udp_relay_queue_capacity,
│           queue_put_timeout, udp_drop_warn_interval)
├── .is_loopback    bool
└── .url            str  ("tcp://host:port")

Socks5Server
├── __init__(config: Socks5ServerConfig | None)
├── await start()
├── await stop()
├── async with server
└── async for req in server → TCPRelay

TCPRelay                          (received from server, never constructed)
├── .cmd              Cmd
├── .host             str
├── .port             int
├── .reader           asyncio.StreamReader
├── .writer           asyncio.StreamWriter
├── .udp_relay        UDPRelay | None
├── .replied          bool  (read-only)
├── .is_connect       bool
├── .is_udp_associate bool
├── await send_reply_success(bind_host, bind_port)
├── await send_reply_error(reply)
├── await close()
└── async with req

UDPRelay                          (received via req.udp_relay, rarely constructed)
├── __init__(*, queue_capacity=2_048, drop_warn_interval=1_000)
├── await start(expected_client_addr) → int
├── close()
├── await recv() → (bytes, str, int) | None
├── send_to_client(payload, src_host, src_port)
├── .local_port           int
├── .is_running           bool
├── .accepted_count       int
├── .drop_count           int
├── .foreign_client_count int
└── async with relay
```

