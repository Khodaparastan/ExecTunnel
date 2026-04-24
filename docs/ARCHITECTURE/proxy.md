# ExecTunnel Proxy Package — Architecture Document

```
exectunnel/proxy/  |  arch-doc v2.1  |  Python 3.13+
```

---

## 1. Purpose & Scope

The `proxy` package is the **SOCKS5 protocol boundary** of the ExecTunnel stack. It
translates between the SOCKS5 wire protocol spoken by local clients (browsers, `curl`,
`ssh`, etc.) and the abstract tunnel interface consumed by the session layer.

The package has **no knowledge of WebSocket, frame encoding, Kubernetes, or transport
reconnection**. It speaks raw TCP bytes inbound and exposes clean Python objects
outbound. Everything above the `proxy` layer is invisible to it.

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
│  │ Socks5Server │──▶│    TCPRelay      │   │      UDPRelay          │  │
│  │  (server.py) │   │  (tcp_relay.py)  │◀──│   (udp_relay.py)       │  │
│  └──────┬───────┘   └──────────────────┘   └────────────────────────┘  │
│         │                                                               │
│  ┌──────▼───────────────────────────────────────────────────────────┐  │
│  │  _handshake.py   _io.py        _wire.py      _errors.py          │  │
│  │  (SOCKS5 state   (async I/O)   (pure sync    (exception          │  │
│  │   machine)                      wire helpers)  dispatch)         │  │
│  │                                                                   │  │
│  │  _udp_protocol.py   _constants.py   config.py                    │  │
│  │  (datagram adapter) (magic numbers) (dataclass config)           │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└───────┬─────────────────────────────────────────────────────────────────┘
        │
        │  TCPRelay objects (enqueued)
        ▼
┌───────────────────────────────────────────────────────────────────────┐
│  exectunnel.session                                                   │
│  (consumes TCPRelay — opens tunnel connections, relays data)          │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 3. Layer Contract

```
proxy  →  exectunnel.protocol      PERMITTED  (AddrType, AuthMethod, Cmd, Reply)
proxy  →  exectunnel.exceptions    PERMITTED  (ProtocolError, TransportError, ConfigurationError)
proxy  →  exectunnel.observability PERMITTED  (metrics_inc, metrics_gauge_*, metrics_observe,
                                               aspan, start_trace — used in server, tcp_relay,
                                               udp_relay)
proxy  →  stdlib asyncio           PERMITTED
proxy  →  stdlib socket / struct   PERMITTED
proxy  →  stdlib ipaddress         PERMITTED
proxy  →  stdlib logging           PERMITTED

proxy  ↛  exectunnel.transport     FORBIDDEN
proxy  ↛  exectunnel.session       FORBIDDEN
proxy  ↛  exectunnel.defaults      FORBIDDEN  (use constructor args / _constants.py)
proxy  ↛  exectunnel.protocol.frames  FORBIDDEN  (no frame encoding/decoding here)
```

> **Rule:** If a new import from outside the permitted set is needed, it must be
> injected as a constructor argument or callback — never imported directly.

---

## 4. Module Map

```
exectunnel/proxy/
├── __init__.py        Public re-export surface — import everything from here
├── _constants.py      Shared numeric constants — single source of truth for all magic numbers
├── _wire.py           Pure sync wire helpers — no I/O, no asyncio
├── _io.py             Async I/O helpers — the only module that touches streams
├── _handshake.py      SOCKS5 negotiation state machine — extracted from server.py
├── _errors.py         Handshake exception dispatch — classify_handshake_error()
├── _udp_protocol.py   asyncio datagram protocol adapter — RelayDatagramProtocol
├── config.py          Socks5ServerConfig — immutable validated server configuration
├── tcp_relay.py       TCPRelay dataclass — one completed handshake
├── udp_relay.py       UDPRelay — UDP datagram relay for UDP_ASSOCIATE
└── server.py          Socks5Server — async accept loop + handshake orchestration
```

### Module responsibility matrix

| Module             | Sync / Async              | I/O                            | State           | Exported      |
|--------------------|---------------------------|--------------------------------|-----------------|---------------|
| `_constants.py`    | Sync (data only)          | None                           | None            | No (internal) |
| `_wire.py`         | Sync only                 | None                           | None            | No (internal) |
| `_io.py`           | Async                     | Stream read/write              | None            | No (internal) |
| `_handshake.py`    | Async                     | Stream read/write (via `_io`)  | None            | No (internal) |
| `_errors.py`       | Sync                      | None (logging only)            | None            | No (internal) |
| `_udp_protocol.py` | Sync (protocol callbacks) | None (delegates to `UDPRelay`) | None            | No (internal) |
| `config.py`        | Sync                      | None                           | None            | Yes           |
| `tcp_relay.py`     | Both                      | Writer buffer                  | Per-request     | Yes           |
| `udp_relay.py`     | Both                      | UDP socket                     | Per-session     | Yes           |
| `server.py`        | Async                     | TCP accept                     | Server lifetime | Yes           |

### Internal dependency graph

```
__init__.py
    ├── config.py
    │     └── _constants.py
    ├── server.py
    │     ├── _errors.py
    │     ├── _handshake.py
    │     │     ├── _io.py
    │     │     │     └── _wire.py
    │     │     ├── _wire.py
    │     │     ├── _constants.py
    │     │     ├── tcp_relay.py
    │     │     └── udp_relay.py
    │     ├── _io.py
    │     └── config.py
    ├── tcp_relay.py
    │     ├── _wire.py
    │     └── udp_relay.py
    └── udp_relay.py
          ├── _constants.py
          ├── _udp_protocol.py
          └── _wire.py
```

The graph is a strict DAG. `_constants.py`, `_wire.py`, `_errors.py`, and
`_udp_protocol.py` are leaf nodes with no intra-package dependencies.

---

## 5. Public API

```python
from exectunnel.proxy import Socks5Server, TCPRelay, Socks5ServerConfig, UDPRelay
```

| Symbol               | Kind             | Description                              |
|----------------------|------------------|------------------------------------------|
| `Socks5ServerConfig` | Frozen dataclass | Immutable validated server configuration |
| `Socks5Server`       | Class            | Async SOCKS5 accept loop                 |
| `TCPRelay`           | Dataclass        | One completed SOCKS5 handshake           |
| `UDPRelay`           | Class            | UDP datagram relay for `UDP_ASSOCIATE`   |

Everything else (`_wire`, `_io`, `_handshake`, `_errors`, `_udp_protocol`, `_constants`,
`RelayDatagramProtocol`) is **package-internal**. Never import from sub-modules
directly.

---

## 6. Data Flow Diagrams

### 6.1 CONNECT handshake

```
SOCKS5 client                  Socks5Server              _handshake.negotiate()   Session layer
     │                              │                              │                    │
     │── TCP SYN ──────────────────▶│                              │                    │
     │                              │  asyncio.start_server        │                    │
     │                              │  spawns _handle_client       │                    │
     │                              │  → _do_handshake()           │                    │
     │                              │  → negotiate() ─────────────▶│                    │
     │                              │                              │                    │
     │── VER=5, NMETHODS, METHODS ─▶│                              │                    │
     │                              │                    _negotiate_auth()               │
     │◀─ VER=5, METHOD=NO_AUTH ─────│                    read_exact(2)                  │
     │                              │                    read_exact(nmethods)            │
     │                              │                              │                    │
     │── VER=5, CMD=CONNECT,        │                              │                    │
     │   RSV=0, ATYP, DST.ADDR,    │                              │                    │
     │   DST.PORT ─────────────────▶│                    _read_request()                │
     │                              │                    read_exact(3)                  │
     │                              │                    read_socks5_addr()             │
     │                              │                              │                    │
     │                              │                    TCPRelay created               │
     │                              │◀─────────────────────────────│                    │
     │                              │  enqueued ───────────────────────────────────────▶│
     │                              │                              │                    │
     │                              │                              │  async for req      │
     │                              │                              │  in server:         │
     │                              │                              │  handle(req)        │
     │                              │                              │                    │
     │◀─ VER=5, REP=SUCCESS ────────│◀──────────────────────────────────────────────────│
     │                              │                              │  send_reply_success()
     │                              │                              │                    │
     │◀══════════════ TCP data relay (session layer) ══════════════════════════════════▶│
```

### 6.2 UDP_ASSOCIATE handshake

```
SOCKS5 client              Socks5Server    _handshake.negotiate()   UDPRelay    Session layer
     │                          │                   │                  │              │
     │── VER=5, CMD=UDP_ASSOC,  │                   │                  │              │
     │   client_addr:port ─────▶│                   │                  │              │
     │                          │         _build_udp_request()         │              │
     │                          │                   │── UDPRelay() ───▶│              │
     │                          │                   │── start() ──────▶│              │
     │                          │                   │  bind 127.0.0.1:0│              │
     │                          │                   │◀─ ephemeral_port ─│              │
     │                          │                   │                  │              │
     │                          │  TCPRelay(udp_relay=relay)           │              │
     │                          │◀──────────────────│                  │              │
     │                          │  enqueued ──────────────────────────────────────────▶
     │                          │                   │                  │              │
     │◀─ REP=SUCCESS,           │◀──────────────────────────────────────────────────── │
     │   BND.PORT=ephemeral ────│                              send_reply_success(      │
     │                          │                              bind_port=relay.local_port)
     │                          │                   │                  │              │
     │── UDP datagram ──────────────────────────────────────────────────▶              │
     │   (SOCKS5 header+payload)│                   │  on_datagram()   │              │
     │                          │                   │  parse_udp_header│              │
     │                          │                   │  queue.put_nowait│              │
     │                          │                   │                  │              │
     │                          │                   │                  │◀─ relay.recv()│
     │                          │                   │                  │  (payload,    │
     │                          │                   │                  │   host, port) │
     │                          │                   │                  │              │
     │◀─ UDP datagram ──────────────────────────────────────────────────│◀─ send_to_   │
     │   (SOCKS5 header+payload)│                   │  send_to_client() │   client()  │
```

### 6.3 Inbound UDP datagram path (detail)

```
OS UDP socket
     │
     │  raw bytes + (src_ip, src_port)
     ▼
_RelayDatagramProtocol.datagram_received()   [_udp_protocol.py]
     │
     │  delegates to
     ▼
UDPRelay.on_datagram()
     │
     ├─ [closed or queue is None] ──▶ return (silent)
     │
     ├─ _check_and_bind_client(addr)
     │   ├─ [client not yet bound, expected hint set, addr != expected] ──▶ drop + DEBUG log
     │   ├─ [client not yet bound, no conflict] ──▶ bind _client_addr = addr
     │   └─ [addr != _client_addr] ──▶ drop + WARNING log (throttled)
     │
     ├─ parse_udp_header(data)   [in _wire.py]
     │   ├─ [ProtocolError] ──▶ drop + DEBUG log (error_code logged)
     │   └─ (payload, host, port)
     │
     ├─ [len(payload) > MAX_UDP_PAYLOAD_BYTES] ──▶ drop + DEBUG log
     │
     └─ queue.put_nowait((payload, host, port))
         ├─ [QueueFull] ──▶ _record_queue_drop() + WARNING log (throttled)
         └─ [ok] ──▶ accepted_count += 1, metrics_inc, metrics_observe
```

---

## 7. Module Deep-Dives

### 7.1 `_constants.py` — Shared Numeric Constants

**Contract:** Pure data. No imports from any other `exectunnel` sub-package except
`exectunnel.protocol.constants.MAX_TCP_UDP_PORT` (re-exported). Single source of truth
for every proxy-layer magic number. Imported by `_wire.py`, `udp_relay.py`, `config.py`,
and `_handshake.py`.

#### Constants

| Name                             | Value         | Description                                     |
|----------------------------------|---------------|-------------------------------------------------|
| `MAX_TCP_UDP_PORT`               | `65_535`      | Re-export from `exectunnel.protocol.constants`  |
| `MAX_UDP_PAYLOAD_BYTES`          | `65_507`      | Max UDP payload (65535 − 20 IP hdr − 8 UDP hdr) |
| `SOCKS5_VERSION`                 | `0x05`        | SOCKS5 protocol version byte (RFC 1928 §3)      |
| `DEFAULT_HANDSHAKE_TIMEOUT`      | `30.0`        | Max seconds per SOCKS5 handshake                |
| `DEFAULT_REQUEST_QUEUE_CAPACITY` | `256`         | Bounded queue depth in `Socks5Server`           |
| `DEFAULT_UDP_QUEUE_CAPACITY`     | `2_048`       | Per-relay inbound datagram queue depth          |
| `DEFAULT_DROP_WARN_INTERVAL`     | `1_000`       | Throttle drop-warning log spam                  |
| `DEFAULT_QUEUE_PUT_TIMEOUT`      | `5.0`         | Max wait to enqueue a completed handshake       |
| `DEFAULT_HOST`                   | `"127.0.0.1"` | Default bind address                            |
| `DEFAULT_PORT`                   | `1080`        | Default bind port                               |

---

### 7.2 `_wire.py` — Pure Sync Wire Helpers

**Contract:** Zero I/O. Zero asyncio. Zero state. Every function is a pure
transformation from bytes/strings to bytes/strings. Safe to call from any execution
context including the in-pod agent and synchronous unit tests. Used by `_io.py` (for
`parse_socks5_addr_buf`) and `udp_relay.py` (for `build_udp_header`,
`parse_udp_header`).

#### Functions

| Function                 | Input                      | Output              | Raises               |
|--------------------------|----------------------------|---------------------|----------------------|
| `validate_socks5_domain` | `str`                      | `None`              | `ProtocolError`      |
| `parse_socks5_addr_buf`  | `bytes, int, *, str, bool` | `(str, int, int)`   | `ProtocolError`      |
| `parse_udp_header`       | `bytes`                    | `(bytes, str, int)` | `ProtocolError`      |
| `build_socks5_reply`     | `Reply, str, int`          | `bytes`             | `ConfigurationError` |
| `build_udp_header`       | `AddrType, bytes, int`     | `bytes`             | `ConfigurationError` |

#### `validate_socks5_domain` — validation pipeline

```
domain: str
    │
    ├─ len > 253 ──────────────────────────────▶ ProtocolError
    │
    ├─ _DOMAIN_UNSAFE_RE matches (\x00 : \r \n < >) ─▶ ProtocolError
    │
    ├─ domain.rstrip(".").split(".")
    │   └─ for each label:
    │       ├─ label == "" ───────────────────────▶ ProtocolError
    │       └─ not _DOMAIN_LABEL_RE.match(label) ─▶ ProtocolError
    │
    └─ None  (valid)
```

#### `parse_socks5_addr_buf` — sync buffer parser

```
(data: bytes, offset: int, *, context: str, allow_port_zero: bool)
    │
    ├─ data[offset] == IPV4  ──▶ decode 4 bytes → IPv4Address str
    ├─ data[offset] == IPV6  ──▶ decode 16 bytes → IPv6Address.compressed str
    ├─ data[offset] == DOMAIN
    │   ├─ read length byte (dlen)
    │   ├─ dlen == 0 ──────────────────────────────────▶ ProtocolError
    │   ├─ read dlen bytes → UTF-8 decode ──[error]───▶ ProtocolError
    │   └─ validate_socks5_domain(host)
    └─ unknown ATYP ──────────────────────────────────▶ ProtocolError
    │
    ├─ read 2 bytes → port (network byte order)
    ├─ port == 0 AND NOT allow_port_zero ─────────────▶ ProtocolError
    └─ return (host, port, new_offset)
```

#### `parse_udp_header` — wire layout consumed

```
Offset  Field         Size      Notes
──────  ─────         ────      ─────
0       RSV           2 bytes   Must be 0x0000 — validated; raises ProtocolError if non-zero
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

#### `build_udp_header` — wire layout produced

```
Offset  Field         Size      Notes
──────  ─────         ────      ─────
0       RSV           2 bytes   Always 0x0000
2       FRAG          1 byte    Always 0x00
3       ATYP          1 byte    Only IPV4 (0x01) or IPV6 (0x04) — DOMAIN forbidden
4       ADDR          4/16 bytes addr_packed
4+addr  PORT          2 bytes   Network byte order
```

#### Module-internal constants

| Name                | Value                                               | Purpose                             |
|---------------------|-----------------------------------------------------|-------------------------------------|
| `_DOMAIN_MAX_LEN`   | `253`                                               | RFC 1035 §2.3.4 DNS name limit      |
| `_DOMAIN_LABEL_RE`  | `^[A-Za-z0-9_]([A-Za-z0-9\-_]{0,61}[A-Za-z0-9_])?$` | RFC 1123 + underscore-relaxed label |
| `_DOMAIN_UNSAFE_RE` | `[\x00:\r\n<>]`                                     | Frame-unsafe + NUL + CRLF guard     |
| `_IPV4_LEN`         | `4`                                                 | IPv4 address byte length            |
| `_IPV6_LEN`         | `16`                                                | IPv6 address byte length            |
| `_PORT_LEN`         | `2`                                                 | Port field byte length              |

> **Note on underscores:** `_DOMAIN_LABEL_RE` permits underscores (`_`) in domain labels
> for real-world SRV/DMARC/ACME compatibility (`_dmarc.example.com`,
`_acme-challenge.example.com`). RFC 952 forbids them; RFC 1123 is silent; real-world DNS
> uses them widely.

> **Note on `\r\n` rejection:** `_DOMAIN_UNSAFE_RE` rejects carriage-return and newline
> in addition to NUL and frame-delimiters, guarding against HTTP-injection via CRLF
> smuggling in domain names.

---

### 7.3 `_io.py` — Async I/O Helpers

**Contract:** The **only** module in the proxy package that performs stream I/O. All
stream reads and best-effort writes are centralised here. Delegates all wire-format
parsing to `parse_socks5_addr_buf` in `_wire.py`. No SOCKS5 state machine logic lives
here — only byte counting and delegation.

#### Functions

| Function                 | Async | Purpose                                                                                            |
|--------------------------|-------|----------------------------------------------------------------------------------------------------|
| `read_exact`             | Yes   | Read exactly *n* bytes from a stream; raises `ProtocolError` on truncation                         |
| `read_socks5_addr`       | Yes   | Read raw `ATYP + addr + port` bytes from stream; delegate parsing to `_wire.parse_socks5_addr_buf` |
| `close_writer`           | Yes   | Close a `StreamWriter`, suppress `OSError`/`RuntimeError`                                          |
| `write_and_drain_silent` | Yes   | Best-effort write+drain for error replies                                                          |

#### `read_socks5_addr` — I/O phase only (parsing delegated)

```
reader (asyncio.StreamReader)
    │
    ├─ read_exact(1) → atyp_byte
    │   └─ AddrType(atyp_byte[0]) ──[ValueError]──▶ ProtocolError
    │
    ├─ atyp == IPV4 (0x01)
    │   └─ read_exact(6) → rest   [4 bytes addr + 2 bytes port]
    │
    ├─ atyp == IPV6 (0x04)
    │   └─ read_exact(18) → rest  [16 bytes addr + 2 bytes port]
    │
    ├─ atyp == DOMAIN (0x03)
    │   ├─ read_exact(1) → len_byte
    │   └─ rest = len_byte + read_exact(len_byte[0] + 2)
    │
    └─ buf = atyp_byte + rest
       parse_socks5_addr_buf(buf, offset=0,
                             context="SOCKS5",
                             allow_port_zero=allow_port_zero)
       │  (all addr decoding, UTF-8 decode, domain validation,
       │   port extraction, port-zero check live in _wire.py)
       │
       ├─ ProtocolError ──────────────────────────────────▶ propagated
       └─ return (host, port)
```

#### Why `_io.py` is separate from `_wire.py`

```
_wire.py                          _io.py
────────────────────────────      ────────────────────────────
Pure functions                    Async coroutines
No asyncio imports                Wraps asyncio.StreamReader
Testable without event loop       Requires running event loop
Used by _io.py and udp_relay.py   Used by _handshake.py only
Safe in agent (no asyncio)        Not safe in agent
```

---

### 7.4 `_handshake.py` — SOCKS5 Negotiation State Machine

**Contract:** Owns the complete SOCKS5 handshake protocol logic. Extracted from
`server.py` so the accept loop can focus on lifecycle management. Every function reads
from the stream, writes the correct reply on failure paths, and raises `ProtocolError`
on client-facing errors. Called exclusively by `server._do_handshake()`.

#### Public function

```python
async def negotiate(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        udp_relay_queue_capacity: int,
        udp_drop_warn_interval: int,
) -> TCPRelay | None
```

Returns a fully-negotiated `TCPRelay`, or `None` for `BIND` (error reply already sent).

#### Internal functions

| Function                                                                                | Purpose                                                     |
|-----------------------------------------------------------------------------------------|-------------------------------------------------------------|
| `_negotiate_auth(reader, writer)`                                                       | RFC 1928 §3 method-selection, accepting only `NO_AUTH`      |
| `_read_request(reader, writer)`                                                         | Read and validate the SOCKS5 request header and destination |
| `_build_udp_request(*, reader, writer, host, port, queue_capacity, drop_warn_interval)` | Spawn a `UDPRelay` and return the associated `TCPRelay`     |

#### `negotiate()` — full state machine

```
Phase 1 — Greeting  [_negotiate_auth()]
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

Phase 2 — Request  [_read_request()]
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
            └─ read_socks5_addr(reader,
                   allow_port_zero=(cmd in (UDP_ASSOCIATE, BIND)))
                   → (host, port)
                │
                ├─ CONNECT ──────────────────────────────▶ TCPRelay(cmd=CONNECT)
                │
                ├─ UDP_ASSOCIATE  [_build_udp_request()]
                │   ├─ UDPRelay(queue_capacity, drop_warn_interval)
                │   ├─ relay.start(expected_client_addr=
                │   │       (host, port) if port != 0 else None)
                │   │   └─ [Exception] ──▶ relay.close()
                │   │                      write GENERAL_FAILURE
                │   │                      re-raise
                │   └─ ─────────────────────────────────▶ TCPRelay(cmd=UDP_ASSOCIATE,
                │                                                   udp_relay=relay)
                │
                └─ BIND ──▶ write CMD_NOT_SUPPORTED ─────▶ return None
```

---

### 7.5 `_errors.py` — Handshake Exception Dispatch

**Contract:** Centralises the exception-to-metric-label-to-log-level mapping for the
handshake failure path. Keeps `server._do_handshake()` as a flat dispatch rather than
five near-identical `except` blocks. The function is trivially unit-testable in
isolation.

#### `classify_handshake_error(exc) → (reason, log_level)`

| Exception type            | `reason` label | Log level | Log action                                         |
|---------------------------|----------------|-----------|----------------------------------------------------|
| `TimeoutError`            | `"timeout"`    | `WARNING` | No log (caller logs)                               |
| `ProtocolError`           | `"protocol"`   | `DEBUG`   | `DEBUG` with `error_code`, `message`, `error_id`   |
| `TransportError`          | `"transport"`  | `WARNING` | `WARNING` with `error_code`, `message`, `error_id` |
| `ExecTunnelError` (other) | `"library"`    | `WARNING` | `WARNING` with `error_code`, `message`, `error_id` |
| `OSError`                 | `"os_error"`   | `DEBUG`   | `DEBUG` with exception string                      |
| Any other                 | `"unexpected"` | `WARNING` | `WARNING` + `DEBUG` traceback                      |

The `reason` string is used as the label for the `socks5.handshakes.error` metric
counter. The `log_level` integer is returned for callers that wish to correlate the
metric with structured logs.

---

### 7.6 `_udp_protocol.py` — asyncio Datagram Protocol Adapter

**Contract:** Thin adapter between asyncio's datagram protocol callbacks and
`UDPRelay.on_datagram()`. Separated from `udp_relay.py` to keep the relay class focused
on queue management and SOCKS5 header logic.

#### `RelayDatagramProtocol`

```python
class RelayDatagramProtocol(asyncio.DatagramProtocol):
    __slots__ = ("_relay",)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:

    # Forwards to self._relay.on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:

    # DEBUG log — non-fatal transport errors

    def connection_lost(self, exc: Exception | None) -> None:
# DEBUG log only if exc is not None — normal closures are silent
```

---

### 7.7 `config.py` — `Socks5ServerConfig`

**Contract:** Frozen, slotted dataclass. All 7 fields validated in `__post_init__` via
`_require()` helper; raises `ConfigurationError` on the first failure. The only module
in the proxy package that imports `ipaddress` for the `is_loopback` property. Does not
import from `exectunnel.observability` or `exectunnel.defaults`.

#### Fields and defaults

| Field                      | Type    | Default       | Constraint       |
|----------------------------|---------|---------------|------------------|
| `host`                     | `str`   | `"127.0.0.1"` | Non-empty string |
| `port`                     | `int`   | `1080`        | `[1, 65535]`     |
| `handshake_timeout`        | `float` | `30.0`        | `> 0`            |
| `request_queue_capacity`   | `int`   | `256`         | `≥ 1`            |
| `udp_relay_queue_capacity` | `int`   | `2_048`       | `≥ 1`            |
| `queue_put_timeout`        | `float` | `5.0`         | `> 0`            |
| `udp_drop_warn_interval`   | `int`   | `1_000`       | `≥ 1`            |

#### Properties

| Property      | Returns | Notes                                                                                                             |
|---------------|---------|-------------------------------------------------------------------------------------------------------------------|
| `is_loopback` | `bool`  | `True` when `host` is a loopback address. Non-IP strings (e.g. `"localhost"`) return `False` — no DNS resolution. |
| `url`         | `str`   | `"tcp://host:port"` for logging. IPv6 hosts are bracket-quoted.                                                   |

---

### 7.8 `tcp_relay.py` — `TCPRelay`

**Contract:** Owns the `asyncio.StreamWriter` and optional `UDPRelay` for one SOCKS5
session. Enforces the reply-exactly-once invariant via `_consume_reply_slot()`. Does not
perform any SOCKS5 negotiation — it is the **result** of negotiation.

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
                             → writer.close() + wait_closed()
                             → udp_relay.close()
```

#### Double-reply guard

```python
# _consume_reply_slot() is called inside both send_reply_success()
# and send_reply_error() — before any write is attempted.
#
# send_reply_success() additionally checks writer.is_closing() BEFORE
# _consume_reply_slot() so the caller can still call send_reply_error()
# if the writer closed between handshake and reply.
#
# send_reply_error() does NOT check writer.is_closing() — error replies
# must be attempted even on half-closed connections (RFC 1928 §6).
# Drain and close always run in the finally block even on double-reply.
```

#### Dataclass field layout

| Field       | Type                   | `init` | `repr` | Notes                         |
|-------------|------------------------|--------|--------|-------------------------------|
| `cmd`       | `Cmd`                  | Yes    | Yes    | SOCKS5 command                |
| `host`      | `str`                  | Yes    | Yes    | Destination host              |
| `port`      | `int`                  | Yes    | Yes    | Destination port              |
| `reader`    | `asyncio.StreamReader` | Yes    | No     | Client stream                 |
| `writer`    | `asyncio.StreamWriter` | Yes    | No     | Client stream                 |
| `udp_relay` | `UDPRelay \| None`     | Yes    | No     | `None` for CONNECT            |
| `_replied`  | `bool`                 | No     | No     | Internal guard (`init=False`) |

> `eq=False` — prevents dataclass auto-generating `__eq__`/`__hash__` that would compare
`StreamReader`/`StreamWriter` by value, which is unsafe.

#### Reply method matrix

| Method                 | Sync/Async | Drains                   | Closes           | Guards                                       |
|------------------------|------------|--------------------------|------------------|----------------------------------------------|
| `send_reply_success()` | Async      | Yes                      | No               | `is_closing` check + `_consume_reply_slot()` |
| `send_reply_error()`   | Async      | Yes (silent, in finally) | Yes (in finally) | `_consume_reply_slot()` only                 |

---

### 7.9 `udp_relay.py` — `UDPRelay`

**Contract:** Manages one UDP socket for one `UDP_ASSOCIATE` session. Strips inbound
SOCKS5 UDP headers, enqueues payloads for the session layer, and wraps outbound payloads
in SOCKS5 UDP headers before sending back to the client.

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
                         │               │  _close_wait_task cancelled
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

> **Lazy init:** `asyncio.Queue` and `asyncio.Event` are created in `start()`, not
`__init__`, so a `UDPRelay` can be constructed before the event loop is running.

#### `recv()` — close-race resolution

The close sentinel races against the inbound queue using `asyncio.wait`. The
`_close_wait_task` attribute is reused across `recv()` calls to avoid per-call
task-allocation overhead.

```
recv() called
    │
    ├─ [queue is None or close_event is None] ──▶ RuntimeError (start() not called)
    │
    ├─ queue.get_nowait() ──[item available]──▶ return item  (fast path)
    │
    ├─ close_event.is_set() AND queue empty ──▶ return None  (already closed)
    │
    └─ _race_get_vs_close()
        │
        │  queue_task = create_task(queue.get())
        │  if _close_wait_task is None or done:
        │      _close_wait_task = create_task(close_event.wait())
        │  close_task = _close_wait_task
        │
        └─ asyncio.wait({queue_task, close_task}, FIRST_COMPLETED)
            │
            ├─ queue_task wins ──▶ return queue_task.result()
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
    ├─ expected_client_addr is set AND addr != expected ──▶ drop (foreign, DEBUG log)
    │
    └─ bind: _client_addr = addr

Subsequent datagrams
    │
    ├─ addr == _client_addr ──▶ process normally
    │
    └─ addr != _client_addr ──▶ drop + WARNING (throttled every N drops)
```

The `expected_client_addr` hint comes from the `UDP_ASSOCIATE` request body (RFC 1928
§7). `_handshake._build_udp_request()` passes it as `(host, port)` only when
`port != 0`; `start()` additionally discards the hint if the host is an unspecified
address (`0.0.0.0` / `::`).

#### `send_to_client` — outbound header construction

```
(payload: bytes, src_host: str, src_port: int)
    │
    ├─ not isinstance(payload, bytes) ──▶ TransportError
    │   error_code="proxy.udp_send.invalid_payload_type"
    │   details={"field": "payload", "expected": "bytes"}
    │
    ├─ src_port not in [0, 65535]     ──▶ TransportError
    │   error_code="proxy.udp_send.port_out_of_range"
    │   details={"field": "src_port", "expected": "integer in [0, 65535]"}
    │
    ├─ _transport is None or _closed  ──▶ return (silent)
    ├─ _client_addr is None           ──▶ return + DEBUG log
    ├─ ipaddress.ip_address(src_host) ──[ValueError]──▶ return + DEBUG log
    │    (domain names silently dropped — RFC 1928 §7)
    │
    └─ build_udp_header(atyp, addr.packed, src_port)  [in _wire.py]
        │
        └─ transport.sendto(header + payload, _client_addr)
            └─ [OSError] ──▶ suppress (best-effort send)
```

#### Telemetry counters

| Property               | Incremented when                        |
|------------------------|-----------------------------------------|
| `accepted_count`       | Datagram successfully enqueued          |
| `drop_count`           | Inbound queue full                      |
| `foreign_client_count` | Datagram from unexpected source address |

---

### 7.10 `server.py` — `Socks5Server`

**Contract:** Owns the TCP listen socket and the handshake orchestration lifecycle.
Delegates all SOCKS5 protocol logic to `_handshake.negotiate()`. Produces `TCPRelay`
objects and enqueues them for the session layer. Never touches frame encoding,
WebSocket, or session state.

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
                    │                    1. server.close()
                    │                    2. _close_active_writers()
                    │                    3. _cancel_handshake_tasks()
                    │                    4. _drain_pending_requests()
                    │                    5. server.wait_closed()
                    │                    6. queue.put_nowait(None)
                    │                           │
                    │                           ▼
                    │              ┌────────────────────────┐
                    │              │         STOPPED         │
                    │              │  _stopped=True          │
                    │              └────────────────────────┘
                    │
                    ▼
              _do_handshake() → negotiate() completes
              TCPRelay enqueued
              async for req in server: ...
```

#### `_do_handshake()` — exception handling matrix

All exceptions during negotiation are caught in `_do_handshake()` and routed through
`_handle_handshake_failure()` which calls `classify_handshake_error()` from
`_errors.py`. `_handle_client()` registers the current task in `_handshake_tasks` on
entry and removes it in `finally`.

| Exception                | Classified by                          | `reason` label | Action                                                   |
|--------------------------|----------------------------------------|----------------|----------------------------------------------------------|
| `TimeoutError`           | `_errors.classify_handshake_error`     | `"timeout"`    | WARNING log in `_handle_handshake_failure`, close writer |
| `ProtocolError`          | `_errors.classify_handshake_error`     | `"protocol"`   | DEBUG log in `_errors`, close writer                     |
| `TransportError`         | `_errors.classify_handshake_error`     | `"transport"`  | WARNING log in `_errors`, close writer                   |
| `ExecTunnelError`        | `_errors.classify_handshake_error`     | `"library"`    | WARNING log in `_errors`, close writer                   |
| `OSError`                | `_errors.classify_handshake_error`     | `"os_error"`   | DEBUG log in `_errors`, close writer                     |
| `asyncio.CancelledError` | `_handle_client` (not `_do_handshake`) | —              | Close writer, **re-raise**                               |
| `Exception` (bare)       | `_handle_client` (not `_do_handshake`) | —              | Close writer only (already logged)                       |

> `asyncio.CancelledError` is **always re-raised** — it is the mechanism by which
`stop()` terminates in-flight handshake tasks.

#### `stop()` sequence

```python
# stop() sequence (idempotent via _stopped guard):
# 1. server.close()                      — no new connections accepted
# 2. _close_active_writers()             — force-close mid-handshake writers
# 3. _cancel_handshake_tasks()           — cancel + await all in-flight tasks
# 4. _drain_pending_requests()           — close unconsumed TCPRelay objects
# 5. server.wait_closed()                — wait for OS socket teardown
# 6. queue.put_nowait(None)              — signal async iterator to stop
```

---

## 8. Exception Contract

### Exceptions raised by this package

| Exception            | Raised by                                                                    | Meaning                                                                    | Retryable                         |
|----------------------|------------------------------------------------------------------------------|----------------------------------------------------------------------------|-----------------------------------|
| `ProtocolError`      | `_wire`, `_io`, `_handshake`, `tcp_relay`, `server`                          | SOCKS5 wire violation from client                                          | No                                |
| `ConfigurationError` | `_wire.build_socks5_reply`, `_wire.build_udp_header`, `config.__post_init__` | Caller passed invalid arguments                                            | No                                |
| `TransportError`     | `udp_relay.start`, `udp_relay.send_to_client`, `server.start`                | OS refused to bind socket, or invalid caller arguments to `send_to_client` | Yes (bind); No (`send_to_client`) |

### `details` keys used by this package

All `ProtocolError` instances raised here use `socks5_field` and `expected`:

```python
details = {
    "socks5_field": "VER",  # SOCKS5 field name
    "expected": "version byte 0x05",  # human-readable expectation
}
```

All `ConfigurationError` instances use `field`, `value`, `expected`:

```python
details = {
    "field": "bind_port",
    "value": -1,
    "expected": "integer in [0, 65535]",
}
```

`TransportError` from bind failures uses `host`, `port`, `url`:

```python
details = {
    "host": "127.0.0.1",
    "port": 0,
    "url": "udp://127.0.0.1:0",
}
```

`TransportError` from `send_to_client` uses `field` and `expected`:

```python
# invalid_payload_type:
details = {"field": "payload", "expected": "bytes"}

# port_out_of_range:
details = {"field": "src_port", "expected": "integer in [0, 65535]"}
```

### Exceptions this package propagates (never originates)

| Exception                          | Source           | Propagated through                    |
|------------------------------------|------------------|---------------------------------------|
| `ProtocolError` (stream truncated) | `_io.read_exact` | `_io.read_socks5_addr` → `_handshake` |
| `asyncio.CancelledError`           | asyncio runtime  | `server._handle_client` (re-raised)   |
| `OSError`                          | asyncio stream   | `tcp_relay.send_reply_success`        |

### Exceptions this package suppresses (intentional)

| Exception                 | Suppressed in                               | Reason                                          |
|---------------------------|---------------------------------------------|-------------------------------------------------|
| `OSError`, `RuntimeError` | `tcp_relay.close`                           | Connection may already be half-closed           |
| `OSError`                 | `tcp_relay.send_reply_error` writer.write   | Best-effort error reply                         |
| `OSError`                 | `udp_relay.send_to_client` sendto           | Best-effort UDP send                            |
| `OSError`, `RuntimeError` | `_io.close_writer`                          | Teardown — errors not actionable                |
| `OSError`                 | `_io.write_and_drain_silent`                | Best-effort error reply                         |
| `asyncio.QueueEmpty`      | `udp_relay.recv`                            | Normal fast-path miss                           |
| `asyncio.CancelledError`  | `udp_relay._race_get_vs_close` task cleanup | Cleanup only — outer `CancelledError` re-raised |

---

## 9. SOCKS5 Protocol Coverage

### RFC 1928 feature matrix

| Feature                    | Status          | Notes                                                       |
|----------------------------|-----------------|-------------------------------------------------------------|
| SOCKS5 version negotiation | ✅ Implemented   | VER=0x05 enforced on both greeting and request              |
| `NO_AUTH` (0x00)           | ✅ Implemented   | Only supported auth method                                  |
| `USERNAME_PASSWORD` (0x02) | ❌ Not supported | Responds `NO_ACCEPT`                                        |
| `GSSAPI` (0x01)            | ❌ Not supported | Responds `NO_ACCEPT`                                        |
| `CONNECT` (0x01)           | ✅ Implemented   | Full TCP tunnel                                             |
| `BIND` (0x02)              | ❌ Not supported | Responds `CMD_NOT_SUPPORTED`, returns `None` (not an error) |
| `UDP_ASSOCIATE` (0x03)     | ✅ Implemented   | Full UDP relay                                              |
| IPv4 address (0x01)        | ✅ Implemented   | All three commands                                          |
| DOMAIN name (0x03)         | ✅ Implemented   | RFC 1123 + underscore validated, frame-injection guarded    |
| IPv6 address (0x04)        | ✅ Implemented   | Compressed notation                                         |
| UDP fragmentation          | ❌ Not supported | FRAG != 0 drops datagram with `ProtocolError`               |
| UDP RSV validation         | ✅ Implemented   | RSV != 0x0000 raises `ProtocolError`                        |
| Reply with IPv4 BND.ADDR   | ✅ Implemented   | Default `0.0.0.0`                                           |
| Reply with IPv6 BND.ADDR   | ✅ Implemented   | Auto-detected from `bind_host`                              |
| Reply with DOMAIN BND.ADDR | ❌ Prohibited    | RFC 1928 §6 — replies must use IP addresses                 |

### Port zero policy

| Context                 | Port 0 in request | Behaviour                                                                        |
|-------------------------|-------------------|----------------------------------------------------------------------------------|
| `CONNECT`               | Rejected          | `ProtocolError` — port 0 is not a valid destination                              |
| `UDP_ASSOCIATE`         | Accepted          | Client does not know its sending port; `expected_client_addr` hint set to `None` |
| UDP datagram `DST.PORT` | Rejected          | `ProtocolError` — port 0 is not a valid destination                              |

---

## 10. Concurrency Model

```
asyncio event loop (single thread)
    │
    ├─ Socks5Server._server (asyncio.Server)
    │   └─ on each TCP accept: spawn _handle_client() as a Task
    │       └─ registered in _handshake_tasks set
    │
    ├─ _handle_client() Task  [one per connection]
    │   └─ asyncio.timeout(handshake_timeout)
    │       └─ _handshake.negotiate() — sequential stream reads, no parallelism
    │
    ├─ UDPRelay._transport (asyncio.DatagramTransport)
    │   └─ RelayDatagramProtocol.datagram_received() — sync callback
    │       └─ UDPRelay.on_datagram() — sync, queue.put_nowait()
    │
    └─ UDPRelay.recv() — awaited by session layer
        └─ asyncio.wait({queue_task, close_task}, FIRST_COMPLETED)
            └─ close_task reused (_close_wait_task) to avoid per-call allocation
```

### Thread safety

The proxy package is **not thread-safe**. All methods must be called from the same
asyncio event loop thread. `UDPRelay.on_datagram` is called by the asyncio datagram
protocol callback — always on the event loop thread — so `queue.put_nowait()` is safe
without a lock.

### Backpressure

```
SOCKS5 client ──UDP──▶ UDPRelay._queue (bounded: udp_relay_queue_capacity)
                                │
                    [QueueFull] ▼
                         drop + WARNING log
                         drop_count += 1

SOCKS5 client ──TCP──▶ Socks5Server._queue (bounded: request_queue_capacity)
                                │
                    [Full, queue_put_timeout exceeded] ▼
                         GENERAL_FAILURE reply to client
                         connection dropped

SOCKS5 client ──TCP──▶ asyncio.StreamReader (unbounded OS buffer)
                                │
                    [session layer slow] ▼
                         TCP receive window fills
                         OS applies backpressure to client
```

UDP drops are **intentional and expected** under load — UDP has no retransmission. TCP
backpressure is handled by the OS TCP stack automatically.

---

## 11. Security Considerations

### Domain injection guard

Domain names received from SOCKS5 clients are validated against `_DOMAIN_UNSAFE_RE`
before being passed to the session layer. The characters `\x00`, `:`, `\r`, `\n`, `<`,
`>` are rejected because they appear in the ExecTunnel frame format (
`<<<EXECTUNNEL:TYPE:ID:PAYLOAD>>>`) or can corrupt log entries and HTTP headers.

```
SOCKS5 client sends: "evil.com:8080"
                              ↑
                     colon rejected by _DOMAIN_UNSAFE_RE
                     → ProtocolError raised
                     → connection closed
                     → frame injection prevented
```

### Open proxy warning

`Socks5Server.start()` calls `cfg.is_loopback` and emits a `WARNING` log if the result
is `False`. Binding to `0.0.0.0` or any non-loopback address makes the server an open
proxy reachable by any host on the network. Non-IP strings like `"localhost"` also
trigger the warning because DNS resolution is not performed at bind time.

### UDP client address binding

The relay accepts datagrams only from the address that sent the first datagram (or the
`expected_client_addr` hint if provided and specific). Datagrams from other addresses
are dropped. This prevents a third party from injecting UDP traffic into an active relay
session.

### Reply-only IP addresses

`build_socks5_reply` and `build_udp_header` reject domain names — RFC 1928 §6 requires
`BND.ADDR` in replies to be an IP address. Passing a domain raises `ConfigurationError`.
This is enforced by `ipaddress.ip_address()` in `build_socks5_reply` and by the
`atyp is AddrType.DOMAIN` guard in `build_udp_header`.

---

## 12. Configuration Reference

All configuration is passed as constructor arguments. The proxy package imports nothing
from `exectunnel.defaults`.

### `Socks5ServerConfig` (passed to `Socks5Server`)

| Parameter                  | Type    | Default       | Description                                                      |
|----------------------------|---------|---------------|------------------------------------------------------------------|
| `host`                     | `str`   | `"127.0.0.1"` | Bind address. Binding to anything non-loopback logs a `WARNING`. |
| `port`                     | `int`   | `1080`        | TCP port to listen on. Must be in `[1, 65535]`.                  |
| `handshake_timeout`        | `float` | `30.0`        | Seconds allowed for a single SOCKS5 handshake.                   |
| `request_queue_capacity`   | `int`   | `256`         | Max completed handshakes buffered before backpressure.           |
| `udp_relay_queue_capacity` | `int`   | `2_048`       | Max inbound datagrams buffered per `UDPRelay` instance.          |
| `queue_put_timeout`        | `float` | `5.0`         | Max seconds to wait when enqueueing a completed handshake.       |
| `udp_drop_warn_interval`   | `int`   | `1_000`       | Log a `WARNING` every N UDP queue-full drops.                    |

### `UDPRelay`

| Parameter            | Type  | Default | Description                                     |
|----------------------|-------|---------|-------------------------------------------------|
| `queue_capacity`     | `int` | `2_048` | Max buffered inbound datagrams before dropping. |
| `drop_warn_interval` | `int` | `1_000` | Log a `WARNING` every N queue-full drops.       |

### Module-level constants (not configurable at runtime)

| Constant                         | Module       | Value         | Description                                     |
|----------------------------------|--------------|---------------|-------------------------------------------------|
| `MAX_TCP_UDP_PORT`               | `_constants` | `65_535`      | Re-export from `exectunnel.protocol.constants`  |
| `MAX_UDP_PAYLOAD_BYTES`          | `_constants` | `65_507`      | Max UDP payload size (65535 − 28)               |
| `SOCKS5_VERSION`                 | `_constants` | `0x05`        | SOCKS5 protocol version byte                    |
| `DEFAULT_HANDSHAKE_TIMEOUT`      | `_constants` | `30.0`        | Default handshake timeout                       |
| `DEFAULT_REQUEST_QUEUE_CAPACITY` | `_constants` | `256`         | Default completed-handshake queue depth         |
| `DEFAULT_UDP_QUEUE_CAPACITY`     | `_constants` | `2_048`       | Default per-relay inbound datagram queue depth  |
| `DEFAULT_DROP_WARN_INTERVAL`     | `_constants` | `1_000`       | Default drop-warning throttle interval          |
| `DEFAULT_QUEUE_PUT_TIMEOUT`      | `_constants` | `5.0`         | Max seconds to wait when enqueueing a handshake |
| `DEFAULT_HOST`                   | `_constants` | `"127.0.0.1"` | Default bind address                            |
| `DEFAULT_PORT`                   | `_constants` | `1080`        | Default bind port                               |
| `_DOMAIN_MAX_LEN`                | `_wire`      | `253`         | Max DNS name length (RFC 1035 §2.3.4)           |

---

## 13. Invariants Every Caller Must Preserve

```
 1. Never import from sub-modules — always use `from exectunnel.proxy import ...`

 2. Always call send_reply_success() OR send_reply_error() exactly once
    per TCPRelay — never zero times, never twice.

 3. For UDP_ASSOCIATE, always pass bind_port=request.udp_relay.local_port
    to send_reply_success() — the client cannot send datagrams without it.

 4. Always call request.close() (or use async with) after data relay ends —
    never rely on GC to close the writer.

 5. Always call relay.close() when the UDP_ASSOCIATE session ends —
    the relay socket is not closed automatically (unless send_reply_error()
    was called, which closes it).

 6. Never call UDPRelay.recv() before UDPRelay.start() — raises RuntimeError.

 7. Never call UDPRelay.start() more than once — raises RuntimeError.

 8. Never reuse a TCPRelay or UDPRelay after close() — create new
    instances for each session.

 9. Never pass a domain name as bind_host to build_socks5_reply() —
    RFC 1928 §6 requires IP addresses in replies.

10. Never split UDP datagrams — one datagram received = one send_to_client()
    call = one encode_udp_data_frame() call in the session layer.

11. Never manually remove entries from the server's internal queue —
    the server manages its own queue lifecycle.

12. Always use asyncio.create_task(handle(req)) in the accept loop —
    never await handle(req) directly inside async for.
```

---

## 14. What NOT To Do

```python
# ✗ Importing from sub-modules
from exectunnel.proxy._wire import build_socks5_reply
from exectunnel.proxy._handshake import negotiate

# ✗ Forgetting to send a reply
async with request:
    tunnel_id = await session.open_connection(request.host, request.port)
    # missing send_reply_success() — client hangs forever

# ✗ Sending reply twice
await request.send_reply_success()
await request.send_reply_success()  # ProtocolError: double-reply

# ✗ Wrong bind_port for UDP_ASSOCIATE
await request.send_reply_success(bind_port=1080)  # wrong — client sends to 1080
# correct:
await request.send_reply_success(bind_port=request.udp_relay.local_port)

# ✗ Using UDPRelay before start()
relay = UDPRelay()
item = await relay.recv()  # RuntimeError — queue is None

# ✗ Calling start() twice
await relay.start()
await relay.start()  # RuntimeError

# ✗ Passing a domain to build_socks5_reply
build_socks5_reply(Reply.SUCCESS, bind_host="example.com")  # ConfigurationError

# ✗ Splitting a UDP datagram
for chunk in chunks(datagram, 4096):
    relay.send_to_client(chunk, host, port)  # wrong — one datagram = one call

# ✗ Importing from exectunnel.defaults
from exectunnel.defaults import HANDSHAKE_TIMEOUT_SECS  # forbidden

# ✗ Using a wrong field name on Socks5ServerConfig
Socks5ServerConfig(queue_capacity=512)  # TypeError — field does not exist
# correct:
Socks5ServerConfig(request_queue_capacity=512)

# ✗ Blocking the accept loop
async for req in server:
    await handle(req)  # blocks — starves all other clients
# correct:
async for req in server:
    asyncio.create_task(handle(req))
```

---

## 15. Adding New SOCKS5 Commands — Checklist

```
1.  Add the command value to exectunnel.protocol.enums.Cmd
2.  Add a handler branch in _handshake.negotiate() before the BIND fallback
3.  Add a private helper function in _handshake.py for the new command's
    setup logic (following the _build_udp_request pattern)
4.  Add the command to the "Supported commands" section of server.py docstring
5.  Add a new property to TCPRelay if the session layer needs to
    distinguish it (e.g. is_connect, is_udp_associate)
6.  Update the RFC 1928 feature matrix in this document (§9)
7.  Update the negotiate() state machine diagram in this document (§7.4)
8.  Write a unit test covering the full handshake for the new command
9.  Update the session layer dispatcher to handle the new TCPRelay type
```
