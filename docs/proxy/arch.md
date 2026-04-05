# ExecTunnel Proxy Package — Architecture Document

```
exectunnel/proxy/  |  arch-doc v1.0  |  Python 3.13+
```

---

## 1. Purpose & Scope

The `proxy` package is the **SOCKS5 protocol boundary** of the ExecTunnel stack.
It translates between the SOCKS5 wire protocol spoken by local clients (browsers,
`curl`, `ssh`, etc.) and the abstract tunnel interface consumed by the session
layer.

The package has **no knowledge of WebSocket, frame encoding, Kubernetes, or
transport reconnection**. It speaks raw TCP bytes inbound and exposes clean
Python objects outbound. Everything above the `proxy` layer is invisible to it.

---

## 2. Position in the Stack

```
┌─────────────────────────────────────────────────────────────────────────┐
│  SOCKS5 clients                                                         │
│  (browser, curl, ssh, proxychains, …)                                   │
│                                                                         │
│       │ TCP stream (SOCKS5 wire — RFC 1928)                             │
│       │ UDP datagrams (SOCKS5 UDP — RFC 1928 §7)                        │
└───────┼─────────────────────────────────────────────────────────────────┘
        │
┌───────▼─────────────────────────────────────────────────────────────────┐
│  exectunnel.proxy                                                       │
│                                                                         │
│  ┌──────────────┐   ┌──────────────────┐   ┌────────────────────────┐  │
│  │ Socks5Server │──▶│  Socks5Request   │   │      UdpRelay          │  │
│  │  (server.py) │   │  (request.py)    │◀──│   (udp_relay.py)       │  │
│  └──────┬───────┘   └──────────────────┘   └────────────────────────┘  │
│         │                                                               │
│  ┌──────▼───────────────────────────────────────────────────────────┐  │
│  │  _io.py          _wire.py                                        │  │
│  │  (async I/O)     (pure sync wire helpers)                        │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└───────┬─────────────────────────────────────────────────────────────────┘
        │
        │  Socks5Request objects (enqueued)
        ▼
┌───────────────────────────────────────────────────────────────────────┐
│  exectunnel.session                                                   │
│  (consumes Socks5Request — opens tunnel connections, relays data)     │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 3. Layer Contract

```
proxy  →  exectunnel.protocol      PERMITTED  (AddrType, AuthMethod, Cmd, Reply)
proxy  →  exectunnel.exceptions    PERMITTED  (ProtocolError, TransportError, ConfigurationError)
proxy  →  stdlib asyncio           PERMITTED
proxy  →  stdlib socket / struct   PERMITTED
proxy  →  stdlib ipaddress         PERMITTED
proxy  →  stdlib logging           PERMITTED

proxy  ↛  exectunnel.transport     FORBIDDEN
proxy  ↛  exectunnel.session       FORBIDDEN
proxy  ↛  exectunnel.config        FORBIDDEN  (use constructor args / module constants)
proxy  ↛  exectunnel.observability FORBIDDEN  (session layer instruments; proxy only logs)
proxy  ↛  exectunnel.protocol.frames  FORBIDDEN  (no frame encoding/decoding here)
```

> **Rule:** If a new import from outside the permitted set is needed, it must be
> injected as a constructor argument or callback — never imported directly.

---

## 4. Module Map

```
exectunnel/proxy/
├── __init__.py      Public re-export surface — import everything from here
├── _wire.py         Pure sync wire helpers — no I/O, no asyncio
├── _io.py           Async I/O helpers — the only module that touches streams
├── request.py       Socks5Request dataclass — one completed handshake
├── udp_relay.py     UdpRelay — UDP datagram relay for UDP_ASSOCIATE
└── server.py        Socks5Server — async accept loop + SOCKS5 negotiation
```

### Module responsibility matrix

| Module | Sync / Async | I/O | State | Exported |
|---|---|---|---|---|
| `_wire.py` | Sync only | None | None | No (internal) |
| `_io.py` | Async | Stream read/write | None | No (internal) |
| `request.py` | Both | Writer buffer | Per-request | Yes |
| `udp_relay.py` | Both | UDP socket | Per-session | Yes |
| `server.py` | Async | TCP accept | Server lifetime | Yes |

---

## 5. Public API

```python
from exectunnel.proxy import Socks5Server, Socks5Request, UdpRelay
```

| Symbol | Kind | Description |
|---|---|---|
| `Socks5ServerConfig` | Frozen dataclass | Immutable validated server configuration |
| `Socks5Server` | Class | Async SOCKS5 accept loop |
| `Socks5Request` | Dataclass | One completed SOCKS5 handshake |
| `UdpRelay` | Class | UDP datagram relay for `UDP_ASSOCIATE` |

Everything else (`_wire`, `_io`, `_RelayDatagramProtocol`) is **package-internal**.
Never import from sub-modules directly.

---

## 6. Data Flow Diagrams

### 6.1 CONNECT handshake

```
SOCKS5 client                  Socks5Server              Session layer
     │                              │                          │
     │── TCP SYN ──────────────────▶│                          │
     │                              │  asyncio.start_server    │
     │                              │  spawns _handle_client   │
     │                              │                          │
     │── VER=5, NMETHODS, METHODS ─▶│                          │
     │                              │  _negotiate()            │
     │◀─ VER=5, METHOD=NO_AUTH ─────│  read_exact(2)           │
     │                              │  read_exact(nmethods)    │
     │                              │                          │
     │── VER=5, CMD=CONNECT,        │                          │
     │   RSV=0, ATYP, DST.ADDR,    │                          │
     │   DST.PORT ─────────────────▶│                          │
     │                              │  read_exact(3)           │
     │                              │  read_socks5_addr()      │
     │                              │                          │
     │                              │  Socks5Request created   │
     │                              │  enqueued ──────────────▶│
     │                              │                          │
     │                              │          async for req   │
     │                              │          in server:      │
     │                              │          handle(req)     │
     │                              │                          │
     │◀─ VER=5, REP=SUCCESS ────────│◀─ send_reply_success() ──│
     │                              │                          │
     │◀══════════════ TCP data relay (session layer) ══════════▶│
```

### 6.2 UDP_ASSOCIATE handshake

```
SOCKS5 client                  Socks5Server         UdpRelay      Session layer
     │                              │                   │               │
     │── VER=5, CMD=UDP_ASSOCIATE,  │                   │               │
     │   client_addr:port ─────────▶│                   │               │
     │                              │  UdpRelay()        │               │
     │                              │──start()──────────▶│               │
     │                              │  bind 127.0.0.1:0  │               │
     │                              │◀─ ephemeral_port ──│               │
     │                              │                   │               │
     │                              │  Socks5Request(udp_relay=relay)    │
     │                              │  enqueued ─────────────────────────▶
     │                              │                   │               │
     │◀─ REP=SUCCESS,               │◀─ send_reply_success(             │
     │   BND.PORT=ephemeral_port ───│     bind_port=relay.local_port) ──│
     │                              │                   │               │
     │── UDP datagram ─────────────────────────────────▶│               │
     │   (SOCKS5 header + payload)  │  _on_datagram()   │               │
     │                              │  parse_udp_header()│               │
     │                              │  queue.put_nowait()│               │
     │                              │                   │               │
     │                              │                   │◀─ relay.recv()─│
     │                              │                   │  (payload,     │
     │                              │                   │   host, port)  │
     │                              │                   │               │
     │                              │                   │  session opens │
     │                              │                   │  UDP flow ────▶│
     │                              │                   │               │
     │◀─ UDP datagram ──────────────────────────────────│◀─ send_to_     │
     │   (SOCKS5 header + payload)  │  send_to_client() │   client()    │
```

### 6.3 Inbound UDP datagram path (detail)

```
OS UDP socket
     │
     │  raw bytes + (src_ip, src_port)
     ▼
_RelayDatagramProtocol.datagram_received()
     │
     │  delegates to
     ▼
UdpRelay._on_datagram()
     │
     ├─ [size > MAX_UDP_PAYLOAD_BYTES] ──▶ drop + DEBUG log
     │
     ├─ [client_addr not yet bound]
     │   ├─ [expected_client_addr set AND addr != expected] ──▶ drop + DEBUG log
     │   └─ [otherwise] ──▶ bind client_addr = addr
     │
     ├─ [addr != client_addr] ──▶ drop + WARNING log (throttled)
     │
     ├─ parse_udp_header(data)
     │   ├─ [ProtocolError] ──▶ drop + DEBUG log (error_code logged)
     │   └─ (payload, host, port)
     │
     └─ queue.put_nowait((payload, host, port))
         ├─ [QueueFull] ──▶ drop + WARNING log (throttled)
         └─ [ok] ──▶ accepted_count += 1
```

---

## 7. Module Deep-Dives

### 7.1 `_wire.py` — Pure Sync Wire Helpers

**Contract:** Zero I/O. Zero asyncio. Zero state. Every function is a pure
transformation from bytes/strings to bytes/strings. Safe to call from any
execution context including the in-pod agent and synchronous unit tests.

#### Functions

| Function | Input | Output | Raises |
|---|---|---|---|
| `validate_socks5_domain` | `str` | `None` | `ProtocolError` |
| `parse_udp_header` | `bytes` | `(bytes, str, int)` | `ProtocolError` |
| `build_socks5_reply` | `Reply\|int, str, int` | `bytes` | `ConfigurationError` |

#### `validate_socks5_domain` — validation pipeline

```
domain: str
    │
    ├─ len > 253 ──────────────────────────────▶ ProtocolError
    │
    ├─ _DOMAIN_UNSAFE_RE matches (\x00 : < >) ─▶ ProtocolError
    │
    ├─ domain.rstrip(".").split(".")
    │   └─ for each label:
    │       ├─ label == "" ───────────────────────▶ ProtocolError
    │       └─ not _DOMAIN_LABEL_RE.match(label) ─▶ ProtocolError
    │
    └─ None  (valid)
```

#### `parse_udp_header` — wire layout consumed

```
Offset  Field         Size      Notes
──────  ─────         ────      ─────
0       RSV           2 bytes   Must be 0x0000 (ignored — not validated)
2       FRAG          1 byte    Must be 0x00 — fragmentation not supported
3       ATYP          1 byte    0x01=IPv4, 0x03=DOMAIN, 0x04=IPv6
4       DST.ADDR      variable  4 / 16 / (1+N) bytes
4+addr  DST.PORT      2 bytes   Network byte order, must be non-zero
4+addr+2 DATA         variable  Returned as payload bytes
```

#### `build_socks5_reply` — wire layout produced

```
Offset  Field         Size      Notes
──────  ─────         ────      ─────
0       VER           1 byte    Always 0x05
1       REP           1 byte    Reply enum value
2       RSV           1 byte    Always 0x00
3       ATYP          1 byte    0x01=IPv4, 0x04=IPv6 (domain NEVER in replies)
4       BND.ADDR      4/16 bytes addr.packed
4+addr  BND.PORT      2 bytes   Network byte order
```

#### Constants

| Name | Value | Purpose |
|---|---|---|
| `MAX_UDP_PAYLOAD_BYTES` | `65_507` | Max UDP payload (65535 − IP hdr − UDP hdr) |
| `_DOMAIN_MAX_LEN` | `253` | RFC 1035 §2.3.4 DNS name limit |
| `_DOMAIN_LABEL_RE` | `^[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?$` | RFC 1123 label |
| `_DOMAIN_UNSAFE_RE` | `[\x00:<>]` | Frame-unsafe + NUL guard |

---

### 7.2 `_io.py` — Async I/O Helpers

**Contract:** The **only** module in the proxy package that performs stream
I/O. Defines its own `read_exact` coroutine wrapping `asyncio.StreamReader.readexactly`.
All stream reads and best-effort writes are centralised here. No SOCKS5 state machine logic lives here.

#### Functions

| Function | Async | Purpose |
|---|---|---|
| `read_exact` | Yes | Read exactly *n* bytes from a stream; raises `ProtocolError` on truncation |
| `read_socks5_addr` | Yes | Read `ATYP + addr + port` from a stream |
| `close_writer` | Yes | Close a `StreamWriter`, suppress `OSError`/`RuntimeError` |
| `write_and_drain_silent` | Yes | Best-effort write+drain for error replies |

#### `read_socks5_addr` — address parsing state machine

```
reader (asyncio.StreamReader)
    │
    ├─ read_exact(1) → atyp byte
    │
    ├─ atyp == IPV4 (0x01)
    │   └─ read_exact(4) → IPv4Address → host str
    │
    ├─ atyp == IPV6 (0x04)
    │   └─ read_exact(16) → IPv6Address.compressed → host str
    │
    ├─ atyp == DOMAIN (0x03)
    │   ├─ read_exact(1) → length byte
    │   ├─ length == 0 ──────────────────────────────▶ ProtocolError
    │   ├─ read_exact(length) → raw bytes
    │   ├─ decode("utf-8") ──[UnicodeDecodeError]────▶ ProtocolError (chained)
    │   └─ validate_socks5_domain(host)
    │
    └─ atyp unknown ────────────────────────────────▶ ProtocolError
    │
    ├─ read_exact(2) → port (network byte order)
    ├─ port == 0 ───────────────────────────────────▶ ProtocolError
    └─ return (host, port)
```

#### Why `_io.py` is separate from `_wire.py`

```
_wire.py                          _io.py
────────────────────────────      ────────────────────────────
Pure functions                    Async coroutines
No asyncio imports                Wraps asyncio.StreamReader
Testable without event loop       Requires running event loop
Used by udp_relay.py directly     Used by server.py only
Safe in agent (no asyncio)        Not safe in agent
```

---

### 7.3 `request.py` — `Socks5Request`

**Contract:** Owns the `asyncio.StreamWriter` and optional `UdpRelay` for one
SOCKS5 session. Enforces the reply-exactly-once invariant. Does not perform
any SOCKS5 negotiation — it is the **result** of negotiation.

#### State machine

```
                    ┌──────────────────────────────┐
                    │         CREATED               │
                    │  _replied = False             │
                    └──────────────┬───────────────┘
                                   │
               ┌───────────────────┼───────────────────┐
               │                   │                   │
               ▼                   ▼                   ▼
     send_reply_success()  send_reply_error()    (no reply)
               │                   │                   │
               ▼                   ▼                   │
        ┌─────────────┐     ┌─────────────┐           │
        │  REPLIED    │     │  REPLIED    │           │
        │  SUCCESS    │     │  ERROR      │           │
        └──────┬──────┘     └──────┬──────┘           │
               │                   │                   │
               │                   ▼                   │
               │          → drain + close()            │
               │                                       │
               └───────────────────┬───────────────────┘
                                   │
                                   ▼
                             close() / __aexit__
                             → writer.close()
                             → udp_relay.close()
```

#### Double-reply guard

```python
# _assert_not_replied() is called at the top of both send_reply_success()
# and send_reply_error() — before any write is attempted.
#
# send_reply_success() additionally checks writer.is_closing() BEFORE
# _assert_not_replied() so the caller can still call send_reply_error()
# if the writer closed between handshake and reply.
#
# send_reply_error() does NOT check writer.is_closing() — error replies
# must be attempted even on half-closed connections (RFC 1928 §6).
```

#### Dataclass field layout

| Field | Type | `init` | `repr` | Notes |
|---|---|---|---|---|
| `cmd` | `Cmd` | Yes | Yes | SOCKS5 command |
| `host` | `str` | Yes | Yes | Destination host |
| `port` | `int` | Yes | Yes | Destination port |
| `reader` | `asyncio.StreamReader` | Yes | No | Client stream |
| `writer` | `asyncio.StreamWriter` | Yes | No | Client stream |
| `udp_relay` | `UdpRelay \| None` | Yes | No | `None` for CONNECT |
| `_replied` | `bool` | No | No | Internal guard |

> `eq=False` — prevents dataclass auto-generating `__eq__`/`__hash__` that
> would compare `StreamReader`/`StreamWriter` by value, which is unsafe.

#### Reply method matrix

| Method | Sync/Async | Drains | Closes | Guards |
|---|---|---|---|---|
| `send_reply_success()` | Async | Yes | No | `is_closing` check + double-reply |
| `send_reply_error()` | Async | Yes (silent) | Yes | double-reply only |

---

### 7.4 `udp_relay.py` — `UdpRelay`

**Contract:** Manages one UDP socket for one `UDP_ASSOCIATE` session. Strips
inbound SOCKS5 UDP headers, enqueues payloads for the session layer, and wraps
outbound payloads in SOCKS5 UDP headers before sending back to the client.

#### Lifecycle state machine

```
                ┌─────────────────────────────────┐
                │           CONSTRUCTED            │
                │  _started=False  _closed=False   │
                │  _queue=None  _close_event=None  │
                └────────────────┬────────────────┘
                                 │
                                 │  await start()
                                 │  ├─ create asyncio.Queue
                                 │  ├─ create asyncio.Event
                                 │  └─ create_datagram_endpoint
                                 ▼
                ┌─────────────────────────────────┐
                │             RUNNING              │
                │  _started=True  _closed=False    │
                │  _transport set  _local_port set │
                └────────────────┬────────────────┘
                                 │
                         ┌───────┴───────┐
                         │               │
                    recv()           close()
                    (blocks)         (sync)
                         │               │
                         │               │  _close_event.set()
                         │               │  _transport.close()
                         │               ▼
                         │    ┌──────────────────────┐
                         │    │        CLOSED         │
                         │    │  _closed=True         │
                         │    └──────────────────────┘
                         │               │
                         └───────────────┘
                                 │
                           recv() returns None
                           (queue drained)
```

#### `recv()` — close-race resolution

The close sentinel races against the inbound queue using `asyncio.wait`. This
guarantees `recv()` unblocks promptly even when the queue is full.

```
recv() called
    │
    ├─ queue.get_nowait() ──[item available]──▶ return item  (fast path)
    │
    ├─ close_event.is_set() AND queue empty ──▶ return None  (already closed)
    │
    └─ asyncio.wait({queue_task, close_task}, FIRST_COMPLETED)
        │
        ├─ queue_task wins ──▶ cancel close_task ──▶ return item
        │
        └─ close_task wins ──▶ cancel queue_task
                               ──▶ queue.get_nowait()  (final drain)
                                   ├─ [item] ──▶ return item
                                   └─ [empty] ──▶ return None
```

#### Client address binding policy

```
First datagram arrives from addr
    │
    ├─ expected_client_addr is set AND addr != expected ──▶ drop (foreign)
    │
    └─ bind: _client_addr = addr

Subsequent datagrams
    │
    ├─ addr == _client_addr ──▶ process normally
    │
    └─ addr != _client_addr ──▶ drop + WARNING (throttled every N drops)
```

The `expected_client_addr` hint comes from the `UDP_ASSOCIATE` request body
(RFC 1928 §7). It may be `0.0.0.0:0` when the client does not know its sending
address — in that case the hint is discarded and the first datagram binds the
client address.

#### `send_to_client` — outbound header construction

```
(payload: bytes, src_host: str, src_port: int)
    │
    ├─ not isinstance(payload, bytes) ──▶ ProtocolError
    ├─ src_port not in [0, 65535]     ──▶ ProtocolError
    ├─ _transport is None or _closed  ──▶ return (silent)
    ├─ _client_addr is None           ──▶ return + DEBUG log
    ├─ ipaddress.ip_address(src_host) ──[ValueError]──▶ return + DEBUG log
    │
    └─ build header:
        b"\x00\x00\x00"          RSV(2) + FRAG(1)
        + bytes([int(atyp)])     ATYP
        + addr.packed            BND.ADDR (4 or 16 bytes)
        + struct.pack("!H", port) BND.PORT
        │
        └─ transport.sendto(header + payload, _client_addr)
            └─ [OSError] ──▶ suppress (best-effort send)
```

#### Telemetry counters (no external dependency)

| Property | Incremented when |
|---|---|
| `accepted_count` | Datagram successfully enqueued |
| `drop_count` | Inbound queue full |
| `foreign_client_count` | Datagram from unexpected source address |

---

### 7.5 `server.py` — `Socks5Server`

**Contract:** Owns the TCP listen socket and the SOCKS5 negotiation state
machine. Produces `Socks5Request` objects and enqueues them for the session
layer. Never touches frame encoding, WebSocket, or session state.

#### Server lifecycle state machine

```
                ┌──────────────────────────────────┐
                │           CONSTRUCTED             │
                │  _started=False  _stopped=False   │
                └─────────────────┬────────────────┘
                                  │
                                  │  await start()
                                  │  asyncio.start_server(...)
                                  ▼
                ┌──────────────────────────────────┐
                │            LISTENING              │
                │  _started=True  _stopped=False    │
                │  _server set                      │
                └─────────────────┬────────────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    │                           │
              connection arrives          await stop()
              _handle_client spawned            │
                    │                           │
                    │                    server.close()
                    │                    cancel tasks
                    │                    queue.put(None)
                    │                           │
                    │                           ▼
                    │              ┌────────────────────────┐
                    │              │         STOPPED         │
                    │              │  _stopped=True          │
                    │              └────────────────────────┘
                    │
                    ▼
              _negotiate() completes
              Socks5Request enqueued
              async for req in server: ...
```

#### `_negotiate()` — SOCKS5 state machine

```
Phase 1 — Greeting
──────────────────
read_exact(2) → [VER, NMETHODS]
    │
    ├─ VER != 0x05 ──────────────────────────────────────▶ ProtocolError
    ├─ NMETHODS == 0 ──▶ write NO_ACCEPT ───────────────▶ ProtocolError
    │
    └─ read_exact(NMETHODS) → methods bytes
        │
        ├─ NO_AUTH (0x00) not in methods
        │   └─ write NO_ACCEPT ──────────────────────────▶ ProtocolError
        │
        └─ write [0x05, NO_AUTH]  ✓

Phase 2 — Request
─────────────────
read_exact(3) → [VER, CMD, RSV]
    │
    ├─ VER != 0x05 ──────────────────────────────────────▶ ProtocolError
    ├─ RSV != 0x00 ──▶ write GENERAL_FAILURE ───────────▶ ProtocolError
    │
    └─ Cmd(CMD)
        ├─ ValueError ──▶ write CMD_NOT_SUPPORTED ───────▶ ProtocolError
        │
        └─ valid Cmd
            │
            └─ read_socks5_addr(reader) → (host, port)
                │
                ├─ CONNECT ──────────────────────────────▶ Socks5Request(cmd=CONNECT)
                │
                ├─ UDP_ASSOCIATE
                │   ├─ UdpRelay().start(expected_client_addr)
                │   └─ ─────────────────────────────────▶ Socks5Request(cmd=UDP_ASSOCIATE,
                │                                                         udp_relay=relay)
                │
                ├─ BIND ──▶ write CMD_NOT_SUPPORTED ─────▶ return None
                │
                └─ (unhandled enum) ──▶ write CMD_NOT_SUPPORTED ▶ ProtocolError
```

#### `_handle_client()` — exception handling matrix

| Exception | Log level | Action |
|---|---|---|
| `TimeoutError` | WARNING | Close writer |
| `ProtocolError` | DEBUG | Close writer |
| `TransportError` | WARNING | Close writer |
| `ExecTunnelError` | WARNING | Close writer |
| `OSError` | DEBUG | Close writer |
| `asyncio.CancelledError` | — | Close writer, **re-raise** |
| `Exception` (bare) | ERROR (with traceback) | Close writer |

> `asyncio.CancelledError` is **always re-raised** — it is the mechanism by
> which `stop()` terminates in-flight handshake tasks.

#### Task tracking

```python
# _handle_client registers itself in _handshake_tasks on entry
# and removes itself in the finally block.
#
# stop() iterates _handshake_tasks, cancels all, and awaits gather()
# before draining the queue — this ensures writers are closed before
# unconsumed Socks5Request objects are cleaned up.
```

---

## 8. Exception Contract

### Exceptions raised by this package

| Exception | Raised by | Meaning | Retryable |
|---|---|---|---|
| `ProtocolError` | `_wire`, `_io`, `request`, `server` | SOCKS5 wire violation from client | No |
| `ConfigurationError` | `_wire.build_socks5_reply` | Caller passed invalid reply/host/port | No |
| `TransportError` | `udp_relay.start`, `server.start` | OS refused to bind socket | Yes |

### Exceptions this package propagates (never originates)

| Exception | Source | Propagated through |
|---|---|---|
| `ProtocolError` (stream truncated) | `_io.read_exact` | `_io.read_socks5_addr` |
| `asyncio.CancelledError` | asyncio runtime | `server._handle_client` (re-raised) |
| `OSError` | asyncio stream | `request.send_reply_success` |

### Exceptions this package suppresses (intentional)

| Exception | Suppressed in | Reason |
|---|---|---|
| `OSError` | `request.close` | Connection may already be half-closed |
| `OSError` | `request.send_reply_error` writer.write | Best-effort error reply |
| `OSError` | `udp_relay.send_to_client` sendto | Best-effort UDP send |
| `OSError`, `RuntimeError` | `_io.close_writer` | Teardown — errors not actionable |
| `OSError`, `RuntimeError` | `_io.write_and_drain_silent` | Best-effort error reply |
| `asyncio.QueueEmpty` | `udp_relay.recv` | Normal fast-path miss |
| `asyncio.CancelledError` | `udp_relay.recv` task cleanup | Cleanup only — outer CancelledError re-raised |

### `details` keys used by this package

All `ProtocolError` instances raised here use the `exceptions.md` contract
keys `socks5_field` and `expected`:

```python
details={
    "socks5_field": "VER",              # SOCKS5 field name
    "expected":     "version byte 0x05",  # human-readable expectation
}
```

All `ConfigurationError` instances use `field`, `value`, `expected`:

```python
details={
    "field":    "bind_port",
    "value":    -1,
    "expected": "integer in [0, 65535]",
}
```

All `TransportError` instances use `host`, `port`, `url`:

```python
details={
    "host": "127.0.0.1",
    "port": 0,
    "url":  "udp://127.0.0.1:0",
}
```

---

## 9. SOCKS5 Protocol Coverage

### RFC 1928 feature matrix

| Feature | Status | Notes |
|---|---|---|
| SOCKS5 version negotiation | ✅ Implemented | VER=0x05 enforced on both greeting and request |
| `NO_AUTH` (0x00) | ✅ Implemented | Only supported auth method |
| `USERNAME_PASSWORD` (0x02) | ❌ Not supported | Responds `NO_ACCEPT` |
| `GSSAPI` (0x01) | ❌ Not supported | Responds `NO_ACCEPT` |
| `CONNECT` (0x01) | ✅ Implemented | Full TCP tunnel |
| `BIND` (0x02) | ❌ Not supported | Responds `CMD_NOT_SUPPORTED`, returns `None` |
| `UDP_ASSOCIATE` (0x03) | ✅ Implemented | Full UDP relay |
| IPv4 address (0x01) | ✅ Implemented | All three commands |
| DOMAIN name (0x03) | ✅ Implemented | RFC 1123 validated, frame-injection guarded |
| IPv6 address (0x04) | ✅ Implemented | Compressed notation |
| UDP fragmentation | ❌ Not supported | FRAG != 0 drops datagram with `ProtocolError` |
| Reply with IPv4 BND.ADDR | ✅ Implemented | Default `0.0.0.0` |
| Reply with IPv6 BND.ADDR | ✅ Implemented | Auto-detected from `bind_host` |
| Reply with DOMAIN BND.ADDR | ❌ Prohibited | RFC 1928 §6 — replies must use IP addresses |

### Port zero policy

| Context | Port 0 in request | Behaviour |
|---|---|---|
| `CONNECT` | Rejected | `ProtocolError` — port 0 is not a valid destination |
| `UDP_ASSOCIATE` | Accepted | Client does not know its sending port; `expected_client_addr` hint discarded |
| UDP datagram `DST.PORT` | Rejected | `ProtocolError` — port 0 is not a valid destination |

---

## 10. Concurrency Model

```
asyncio event loop (single thread)
    │
    ├─ Socks5Server._server (asyncio.Server)
    │   └─ on each TCP accept: spawn _handle_client() as a Task
    │
    ├─ _handle_client() Task  [one per connection]
    │   └─ asyncio.wait_for(_negotiate(), timeout=handshake_timeout)
    │       └─ _negotiate() — sequential stream reads, no parallelism
    │
    ├─ UdpRelay._transport (asyncio.DatagramTransport)
    │   └─ _RelayDatagramProtocol.datagram_received() — sync callback
    │       └─ UdpRelay._on_datagram() — sync, queue.put_nowait()
    │
    └─ UdpRelay.recv() — awaited by session layer
        └─ asyncio.wait({queue_task, close_task}, FIRST_COMPLETED)
```

### Thread safety

The proxy package is **not thread-safe**. All methods must be called from the
same asyncio event loop thread. `UdpRelay._on_datagram` is called by the
asyncio datagram protocol callback — always on the event loop thread — so
`queue.put_nowait()` is safe without a lock.

### Backpressure

```
SOCKS5 client ──UDP──▶ UdpRelay._queue (bounded: queue_capacity)
                                │
                    [QueueFull] ▼
                         drop + WARNING log
                         drop_count += 1

SOCKS5 client ──TCP──▶ asyncio.StreamReader (unbounded OS buffer)
                                │
                    [session layer slow] ▼
                         TCP receive window fills
                         OS applies backpressure to client
```

UDP drops are **intentional and expected** under load — UDP has no
retransmission. TCP backpressure is handled by the OS TCP stack automatically.

---

## 11. Security Considerations

### Domain injection guard

Domain names received from SOCKS5 clients are validated against
`_DOMAIN_UNSAFE_RE` before being passed to the session layer. The characters
`\x00`, `:`, `<`, `>` are rejected because they appear in the ExecTunnel frame
format (`<<<EXECTUNNEL:TYPE:ID:PAYLOAD>>>`) and could corrupt tunnel frames if
passed through unvalidated.

```
SOCKS5 client sends: "evil.com:8080"
                              ↑
                     colon rejected by _DOMAIN_UNSAFE_RE
                     → ProtocolError raised
                     → connection closed
                     → frame injection prevented
```

### Open proxy warning

`Socks5Server.start()` emits a `WARNING` log if the bind address is not in
`_LOOPBACK_ADDRS`. Binding to `0.0.0.0` or any non-loopback address makes the
server an open proxy reachable by any host on the network.

### UDP client address binding

The relay accepts datagrams only from the address that sent the first datagram
(or the `expected_client_addr` hint if provided and specific). Datagrams from
other addresses are dropped. This prevents a third party from injecting UDP
traffic into an active relay session.

### Reply-only IP addresses

`build_socks5_reply` rejects domain names as `bind_host` — RFC 1928 §6
requires `BND.ADDR` in replies to be an IP address. Passing a domain would
produce a malformed reply packet that could confuse or crash the SOCKS5 client.

---

## 12. Configuration Reference

All configuration is passed as constructor arguments. The proxy package imports
nothing from `exectunnel.config`.

### `Socks5ServerConfig` (passed to `Socks5Server`)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `host` | `str` | `"127.0.0.1"` | Bind address |
| `port` | `int` | `1080` | Bind port |
| `handshake_timeout` | `float` | `30.0` | Max seconds per SOCKS5 handshake |
| `queue_capacity` | `int` | `2_048` | Max buffered completed handshakes |
| `queue_put_timeout` | `float` | `5.0` | Max seconds to wait when enqueueing a handshake |
| `udp_drop_warn_interval` | `int` | `100` | Log a warning every N UDP queue-full drops |

### `UdpRelay`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `queue_capacity` | `int` | `2_048` | Max buffered inbound datagrams |
| `drop_warn_interval` | `int` | `100` | Log a warning every N queue-full drops |

### Module-level constants (not configurable at runtime)

| Constant | Module | Value | Description |
|---|---|---|---|
| `MAX_UDP_PAYLOAD_BYTES` | `_constants` | `65_507` | Max UDP payload size |
| `_DOMAIN_MAX_LEN` | `_wire` | `253` | Max DNS name length |
| `DEFAULT_HANDSHAKE_TIMEOUT` | `_constants` | `30.0` | Default handshake timeout |
| `DEFAULT_QUEUE_CAPACITY` | `_constants` | `2_048` | Default relay/server queue depth |
| `DROP_WARN_INTERVAL` | `_constants` | `100` | Default drop warning interval |
| `QUEUE_PUT_TIMEOUT` | `_constants` | `5.0` | Max seconds to enqueue a handshake |
| `LOOPBACK_ADDRS` | `_constants` | `{"127.0.0.1", "::1"}` | Non-warning bind addresses |

---

## 13. Invariants Every Caller Must Preserve

```
1.  Never import from sub-modules — always use `from exectunnel.proxy import ...`

2.  Always call send_reply_success() OR send_reply_error() exactly once
    per Socks5Request — never zero times, never twice.

3.  For UDP_ASSOCIATE, always pass bind_port=request.udp_relay.local_port
    to send_reply_success() — the client cannot send datagrams without it.

4.  Always call request.close() (or use async with) after data relay ends —
    never rely on GC to close the writer.

5.  Always call relay.close() when the UDP_ASSOCIATE session ends —
    the relay socket is not closed automatically.

6.  Never call UdpRelay.recv() before UdpRelay.start() — raises RuntimeError.

7.  Never call UdpRelay.start() more than once — raises RuntimeError.

8.  Never reuse a Socks5Request or UdpRelay after close() — create new
    instances for each session.

9.  Never pass a domain name as bind_host to build_socks5_reply() —
    RFC 1928 §6 requires IP addresses in replies.

10. Never split UDP datagrams — one datagram received = one send_to_client()
    call = one encode_udp_data_frame() call in the session layer.
```

---

## 14. What NOT To Do

```python
# ✗ Importing from sub-modules
from exectunnel.proxy._wire import build_socks5_reply
from exectunnel.proxy.server import _close_writer

# ✗ Forgetting to send a reply
async with request:
    tunnel_id = await session.open_connection(request.host, request.port)
    # missing send_reply_success() — client hangs forever

# ✗ Sending reply twice
await request.send_reply_success()
await request.send_reply_success()   # ProtocolError: double-reply

# ✗ Wrong bind_port for UDP_ASSOCIATE
await request.send_reply_success(bind_port=1080)  # wrong — client sends to 1080
# correct:
await request.send_reply_success(bind_port=request.udp_relay.local_port)

# ✗ Using UdpRelay before start()
relay = UdpRelay()
item = await relay.recv()   # RuntimeError — queue is None

# ✗ Calling start() twice
await relay.start()
await relay.start()   # RuntimeError

# ✗ Passing a domain to build_socks5_reply
build_socks5_reply(Reply.SUCCESS, bind_host="example.com")  # ConfigurationError

# ✗ Splitting a UDP datagram
for chunk in chunks(datagram, 4096):
    relay.send_to_client(chunk, host, port)   # wrong — one datagram = one call

# ✗ Importing config or observability
from exectunnel.config.defaults import HANDSHAKE_TIMEOUT_SECS  # forbidden
from exectunnel.observability import metrics_inc                # forbidden

# ✗ Catching ProtocolError silently in the session layer
try:
    async for req in server:
        ...
except ProtocolError:
    pass   # wrong — ProtocolError from negotiation is already handled inside
           # _handle_client(); it never propagates out of async for

# ✗ Ignoring the is_running property before sending
relay.send_to_client(data, host, port)   # safe — send_to_client checks internally
# but do not assume the datagram was delivered if is_running is False
```

---

## 15. Adding New SOCKS5 Commands — Checklist

```
1.  Add the command value to exectunnel.protocol.enums.Cmd
2.  Add a handler branch in Socks5Server._negotiate() before the
    exhaustive guard
3.  Add the command to the "Supported commands" section of server.py docstring
4.  Add a new property to Socks5Request if the session layer needs to
    distinguish it (e.g. is_connect, is_udp_associate)
5.  Update the RFC 1928 feature matrix in this document (§9)
6.  Update the _negotiate() state machine diagram in this document (§7.5)
7.  Write a unit test covering the full handshake for the new command
8.  Update the session layer dispatcher to handle the new Socks5Request type
```

