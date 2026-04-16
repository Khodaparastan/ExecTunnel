# ExecTunnel — Protocol Package Architecture Document

```
exectunnel/protocol/  |  arch-doc v1.2  |  Python 3.13+
```

---

## 1. Purpose & Scope

The `protocol` package is the **lowest layer** of the ExecTunnel stack. It owns
exactly two concerns:

1. **Wire encoding / decoding** — transforming typed Python values into
   newline-terminated ASCII frame strings and back.
2. **SOCKS5 enumeration** — providing RFC 1928 / RFC 1929 integer constants as
   typed Python enums.

It has **zero I/O dependencies**. No sockets, no asyncio, no WebSocket, no DNS,
no threads. Every function is a pure transformation: bytes/strings in,
bytes/strings/structs out. This makes the entire package synchronously testable
in isolation and safe to import in any execution context including the in-pod
agent.

---

## 2. Position in the Stack

```
┌─────────────────────────────────────────────────────────────────────┐
│  session          (wires all layers together)                       │
├─────────────────────────────────────────────────────────────────────┤
│  proxy            (SOCKS5 wire protocol, UDP relay)                 │
├─────────────────────────────────────────────────────────────────────┤
│  transport        (frame encoding, WebSocket send/recv)             │
├═════════════════════════════════════════════════════════════════════╡
║  protocol  ◄──── YOU ARE HERE                                       ║
║                                                                     ║
║   ┌──────────┐   ┌──────────┐   ┌──────────┐                       ║
║   │ frames   │   │  enums   │   │   ids    │                       ║
║   └──────────┘   └──────────┘   └──────────┘                       ║
╠═════════════════════════════════════════════════════════════════════╣
│  exceptions       (shared error hierarchy — no layer deps)          │
└─────────────────────────────────────────────────────────────────────┘
```

### Dependency Rule

```
protocol  →  exceptions          (raises ProtocolError / FrameDecodingError)
protocol  →  stdlib only         (base64, binascii, ipaddress, re, secrets)
protocol  ↛  transport           FORBIDDEN
protocol  ↛  proxy               FORBIDDEN
protocol  ↛  session             FORBIDDEN
protocol  ↛  asyncio / sockets   FORBIDDEN
```

Any import of an upper layer into `protocol` is an architecture violation.

---

## 3. Module Map

```
exectunnel/protocol/
├── __init__.py     Public re-export surface — no logic
├── frames.py       Wire codec: encode_*, parse_frame, host/port helpers
├── enums.py        SOCKS5 IntEnum definitions + _StrictIntEnum base
└── ids.py          Cryptographic ID generation + regex validators
```

### Responsibility Matrix

| Module        | Owns                                                         | Does not own                                  |
|---------------|--------------------------------------------------------------|-----------------------------------------------|
| `frames.py`   | Frame constants, wire format, encode/decode, host/port codec | ID generation, SOCKS5 semantics, I/O          |
| `enums.py`    | SOCKS5 RFC integer constants, `_StrictIntEnum` mixin         | Frame format, ID format, I/O                  |
| `ids.py`      | ID generation, ID validation regexes                         | Frame format, SOCKS5, I/O                     |
| `__init__.py` | Public API surface, `__all__`                                | All logic — zero lines of code beyond imports |

---

## 4. Wire Format Specification

### 4.1 Frame Grammar

```
frame        ::= FRAME_PREFIX msg_type [ ":" conn_id [ ":" payload ] ] FRAME_SUFFIX LF
FRAME_PREFIX ::= "<<<EXECTUNNEL:"
FRAME_SUFFIX ::= ">>>"
LF           ::= "\n"
msg_type     ::= "AGENT_READY" | "CONN_OPEN"  | "CONN_ACK"  | "CONN_CLOSE"
               | "DATA"        | "UDP_OPEN"   | "UDP_DATA"  | "UDP_CLOSE"
               | "ERROR"       | "KEEPALIVE"
conn_id      ::= [cu][0-9a-f]{24}
payload      ::= host_port | base64url_nopad
host_port    ::= bare_host ":" port | "[" ipv6_addr "]" ":" port
base64url_nopad ::= [A-Za-z0-9\-_]*
```

**No-conn_id frame types:** `AGENT_READY` and `KEEPALIVE` carry neither a
`conn_id` nor a payload. Both are represented as `<<<EXECTUNNEL:{msg_type}>>>`.
Any extra field on these types is a protocol error (see §4.2 and §13).

**Transport contract:** `parse_frame` expects a single complete line. The
trailing `\n` may be present or absent — `strip()` handles both. The transport
layer is responsible for buffering the byte stream and splitting on `\n` before
calling `parse_frame`. Passing a raw WebSocket message payload that contains
multiple newline-separated frames will silently misparse.

### 4.2 Frame Catalogue

| Frame         | conn_id                           | Payload         | Direction      | Meaning                                                  |
|---------------|-----------------------------------|-----------------|----------------|----------------------------------------------------------|
| `AGENT_READY` | —                                 | —               | Agent → Client | Bootstrap complete; agent is ready                       |
| `CONN_OPEN`   | TCP conn ID                       | `host:port`     | Client → Agent | Open a TCP connection to target                          |
| `CONN_ACK`    | TCP conn ID                       | —               | Agent → Client | Acknowledge `CONN_OPEN`; resolves pending-connect future |
| `CONN_CLOSE`  | TCP conn ID                       | —               | Both           | Explicit TCP teardown                                    |
| `DATA`        | TCP conn ID                       | base64url bytes | Both           | TCP data chunk                                           |
| `UDP_OPEN`    | UDP flow ID                       | `host:port`     | Client → Agent | Open a UDP flow to target                                |
| `UDP_DATA`    | UDP flow ID                       | base64url bytes | Both           | UDP datagram                                             |
| `UDP_CLOSE`   | UDP flow ID                       | —               | Both           | Explicit UDP flow teardown (advisory — no handshake)     |
| `ERROR`       | conn/flow ID or `SESSION_CONN_ID` | base64url UTF-8 | Both           | Error report                                             |
| `KEEPALIVE`   | —                                 | —               | Client → Agent | Session-level heartbeat; agent silently discards         |

**`UDP_CLOSE` ordering note:** `UDP_CLOSE` is an advisory close with no
handshake. The protocol makes no ordering guarantee between a `UDP_CLOSE` frame
and `UDP_DATA` frames already in flight. The session layer must be prepared to
receive `UDP_DATA` frames after sending or receiving `UDP_CLOSE` and must
discard them silently rather than treating them as errors.

**`CONN_ACK` ordering note:** The agent emits `CONN_ACK` once the target TCP
connection is established. The client must not send `DATA` frames for a
`conn_id` until the corresponding `CONN_ACK` is received; this avoids a race
between `DATA` and the agent's connection setup.

### 4.3 Concrete Frame Examples

```
# Agent signals readiness
<<<EXECTUNNEL:AGENT_READY>>>

# Client opens TCP connection c1a2b3... to redis:6379
<<<EXECTUNNEL:CONN_OPEN:ca1b2c3d4e5f6a7b8c9d0e1f2a3b:redis:6379>>>

# Agent acknowledges that TCP connection is established
<<<EXECTUNNEL:CONN_ACK:ca1b2c3d4e5f6a7b8c9d0e1f2a3b>>>

# Client sends "PING\r\n" over that connection
<<<EXECTUNNEL:DATA:ca1b2c3d4e5f6a7b8c9d0e1f2a3b:UEVSR1xy>>>

# Agent closes the connection
<<<EXECTUNNEL:CONN_CLOSE:ca1b2c3d4e5f6a7b8c9d0e1f2a3b>>>

# Agent reports a session-level error
<<<EXECTUNNEL:ERROR:c000000000000000000000000:Y29ubmVjdGlvbiByZWZ1c2Vk>>>

# Client opens UDP flow u9f8e7d... to 8.8.8.8:53
<<<EXECTUNNEL:UDP_OPEN:u9f8e7d6c5b4a3f2e1d0c9b8a7:8.8.8.8:53>>>

# IPv6 target — bracket-quoted
<<<EXECTUNNEL:CONN_OPEN:ca1b2c3d4e5f6a7b8c9d0e1f2a3b:[2001:db8::1]:443>>>

# Client heartbeat — no conn_id, no payload
<<<EXECTUNNEL:KEEPALIVE>>>
```

### 4.4 Frame Length Budget

```
MAX_FRAME_LEN = 8,192 characters  (content only, excluding trailing \n)

Maximum safe DATA payload:
  available_for_payload = 8192 - 14 - 4 - 1 - 25 - 1 - 3 = 8,144 chars
                          ^^^^   ^^   ^   ^^   ^    ^^
                          PREFIX DATA : conn : SUFFIX
  base64url overhead    = 4/3 ratio
  max raw bytes         = floor(8144 * 3 / 4) = 6,108 bytes
```

Derivation:

| Component      | Chars     | Value                    |
|----------------|-----------|--------------------------|
| `FRAME_PREFIX` | 14        | `<<<EXECTUNNEL:`         |
| `msg_type`     | 4         | `DATA`                   |
| separator      | 1         | `:`                      |
| `conn_id`      | 25        | `c` + 24 hex             |
| separator      | 1         | `:`                      |
| `payload`      | **8,144** | base64url (maximum safe) |
| `FRAME_SUFFIX` | 3         | `>>>`                    |
| **Total**      | **8,192** | exactly at limit         |

> **Transport note:** `PIPE_READ_CHUNK_BYTES` (4,096) is deliberately set below
> this limit. It lives in the transport/session layer, not here.

---

## 5. ID System

### 5.1 Format

```
conn_id    ::=  "c"  <24 lowercase hex chars>   (TCP connections)
flow_id    ::=  "u"  <24 lowercase hex chars>   (UDP flows)
session_id ::=  "s"  <24 lowercase hex chars>   (session correlation / logging)
```

TCP and UDP IDs are validated by `CONN_FLOW_ID_RE` (alias: `ID_RE`).
Session IDs are validated by `SESSION_ID_RE`. The two patterns are
mutually exclusive by prefix — a session ID will never match `CONN_FLOW_ID_RE`
and vice versa.

### 5.2 Entropy & Collision Analysis

```
Token source  : secrets.token_hex(12)  →  12 bytes  →  96 bits of entropy
Birthday bound: P(collision) ≈ 0.5 at n ≈ 2^48 ≈ 281 trillion IDs

At 10,000 concurrent connections/s:
  Time to 50% collision probability ≈ 2^48 / 10,000 / 86,400 / 365
                                    ≈ 891,000 years
```

### 5.3 Prefix Namespace Isolation

The `c` / `u` / `s` prefix ensures that IDs of different types can **never
collide** even if their 96-bit tokens are identical.

```
"c" + token_hex(12)  →  TCP conn namespace  (validated by CONN_FLOW_ID_RE)
"u" + token_hex(12)  →  UDP flow namespace  (validated by CONN_FLOW_ID_RE)
"s" + token_hex(12)  →  session namespace   (validated by SESSION_ID_RE)
```

Only `c` and `u` prefixes are valid in frame `conn_id` fields. Session IDs
(`s` prefix) are used solely for log/trace correlation at the session layer and
are never embedded in tunnel frames.

### 5.4 Compiled Patterns

| Name              | Pattern              | Canonical / alias     | Validates                     |
|-------------------|----------------------|-----------------------|-------------------------------|
| `CONN_FLOW_ID_RE` | `^[cu][0-9a-f]{24}$` | **canonical**         | TCP conn IDs and UDP flow IDs |
| `ID_RE`           | (same object)        | backward-compat alias | Same as above                 |
| `SESSION_ID_RE`   | `^s[0-9a-f]{24}$`    | canonical             | Session IDs                   |

All patterns use `re.ASCII` so `[0-9a-f]` never matches Unicode digits.

### 5.5 `SESSION_CONN_ID` Sentinel

```python
SESSION_CONN_ID = "c" + "0" * 24   # = "c000000000000000000000000"
```

This is a **structurally valid** conn_id (passes `CONN_FLOW_ID_RE`) that is
**semantically reserved** — it can never be produced by `new_conn_id()` because
`secrets.token_hex` never returns all-zero output for a 12-byte token
(probability \(2^{-96}\), effectively impossible).

`SESSION_CONN_ID` is derived from `_TOKEN_BYTES` so that if the token length
ever changes, the sentinel stays structurally consistent with `CONN_FLOW_ID_RE`
automatically.

Callers use it to distinguish:

```
conn_id == SESSION_CONN_ID  →  session-level error (affects all connections)
conn_id != SESSION_CONN_ID  →  per-connection error (affects one connection)
```

### 5.6 `new_session_id()` and Session IDs

Session IDs are **not embedded in tunnel frames**. They exist solely for log
and task correlation at the session layer (structured logging, tracing spans,
CLI dashboards).

```python
session_id = new_session_id()   # "s3f7a1c9e2b4d6f8a0c2e4b6"
log.info("tunnel opened", extra={"session_id": session_id})
```

Session IDs share the same 96-bit entropy as TCP/UDP IDs. They are validated
by `SESSION_ID_RE` (`^s[0-9a-f]{24}$`), which is mutually exclusive with
`CONN_FLOW_ID_RE`.

### 5.7 Development-time Assert

Each generator function (`new_conn_id`, `new_flow_id`, `new_session_id`)
contains an `assert` that verifies the produced ID matches its respective
pattern. This assert:

* Is a **development-time sanity check only** — it will never fire in practice
  because `secrets.token_hex` is guaranteed to return lowercase hex.
* Is **stripped in optimised builds** (`python -O`) and must not be relied upon
  as a runtime guard.

---

## 6. Host / Port Codec

### 6.1 Encoding Rules

```
IPv4 literal   →  bare:           "192.168.1.1:8080"
IPv6 literal   →  bracket-quoted: "[2001:db8::1]:8080"
Domain name    →  bare:           "redis.default.svc:6379"
```

IPv6 addresses are **always** normalised to compressed form via
`ipaddress.IPv6Address.compressed` before embedding. This prevents ambiguity
between `::1` and `0:0:0:0:0:0:0:1`.

### 6.2 Domain Name Validation

The domain validator is **intentionally loose**:

```
_DOMAIN_RE = r"^[A-Za-z0-9]([A-Za-z0-9\-.]*[A-Za-z0-9])?$"
```

It accepts:
* Single-label names: `redis`, `postgres` (common in Kubernetes)
* Multi-label FQDNs: `redis.default.svc.cluster.local`
* Numeric labels: `10in-addr` (valid RFC 1123)

It rejects:
* Empty string (caught before regex)
* Labels starting/ending with `-`
* Consecutive dots `..` (caught before regex — explicit `".." in host` check)
* Any of `:`, `<`, `>` (frame-unsafe, caught before regex by `_FRAME_UNSAFE_RE`)

Full RFC 1123 compliance (label length ≤ 63, total ≤ 253) is **deliberately
delegated** to the resolver. The protocol layer's job is only to ensure the host
string cannot corrupt the frame wire format.

### 6.3 Port Range

Both `encode_host_port` and `parse_host_port` enforce `[1, 65535]`. Port `0`
is explicitly excluded because it is not a valid destination port for `OPEN`
frames.

`PORT_UNSPECIFIED = 0` is exported as a named constant for the asymmetric
use-case in `build_socks5_reply` (proxy layer), which uses port `0` as the
RFC 1928 §6 "unspecified" sentinel for error replies. That path does **not**
go through `encode_host_port` or `parse_host_port`.

### 6.4 Codec Symmetry Contract

```
parse_host_port(encode_host_port(host, port)) == (normalised_host, port)
```

The `host` returned by `parse_host_port` may differ from the input to
`encode_host_port` for IPv6 addresses because `ipaddress` normalises them.
Callers must not assume round-trip identity of the raw string — only of the
semantic value


## 7. Base64url Payload Codec

### 7.1 Encoding

```
raw bytes
    │
    ▼  base64.urlsafe_b64encode(data)
standard base64url with padding
    │
    ▼  .rstrip(b"=")
base64url without padding  (safe to embed in frame — no "=" or "+" or "/")
    │
    ▼  .decode("ascii")
ASCII string ready for frame embedding
```

### 7.2 Decoding

```
base64url string (no padding)
    │
    ▼  + "=" * ((4 - len(s) % 4) % 4)
base64url string with padding restored
    │
    ▼  base64.urlsafe_b64decode(...)
raw bytes
```

Padding restoration formula: `(4 - len(s) % 4) % 4` produces `0`, `1`, or `2`
padding chars. The outer `% 4` ensures a string already aligned to 4 chars gets
`0` padding, not `4`.

**Note:** base64url payloads never contain `:`. The `maxsplit=2` in
`parse_frame`'s `inner.split(":", 2)` is not needed for base64url safety — it
exists solely to preserve colons inside bracket-quoted IPv6 addresses in `OPEN`
frame payloads (e.g. `[2001:db8::1]:443`).

### 7.3 Typed Decode Functions

| Function | Input | Output | Used for |
|---|---|---|---|
| `decode_binary_payload` | base64url `str` | `bytes` | `DATA`, `UDP_DATA` frames |
| `decode_error_payload` | base64url `str` | `str` (UTF-8) | `ERROR` frames |

`decode_error_payload` chains two decode steps and raises `FrameDecodingError`
at either step, preserving the original cause via `from exc`.

---

## 8. SOCKS5 Enumerations

### 8.1 `_StrictIntEnum` Base Mixin

All SOCKS5 enums except `UserPassStatus` inherit from `_StrictIntEnum`, a
private `IntEnum` subclass that provides a shared `_missing_` implementation:

```python
class _StrictIntEnum(IntEnum):
    @classmethod
    def _missing_(cls, value: object) -> Never:
        raise ValueError(
            f"{value!r} is not a valid {cls.__name__} "
            f"(expected one of {[m.value for m in cls]})"
        )
```

This eliminates four identical `_missing_` implementations and makes
`AddrType` and `Reply` — which have no other methods — read as pure data.

`UserPassStatus` inherits directly from `IntEnum` because RFC 1929 §2 requires
a **permissive** `_missing_` that maps any non-zero byte to `FAILURE` rather
than rejecting it. Overriding `_StrictIntEnum` for this case would be more
confusing than not inheriting it.

### 8.2 RFC Coverage

| Enum             | Base             | RFC      | Section | Values                                                |
|------------------|------------------|----------|---------|-------------------------------------------------------|
| `AuthMethod`     | `_StrictIntEnum` | RFC 1928 | §3      | `NO_AUTH`, `GSSAPI`, `USERNAME_PASSWORD`, `NO_ACCEPT` |
| `Cmd`            | `_StrictIntEnum` | RFC 1928 | §4      | `CONNECT`, `BIND`, `UDP_ASSOCIATE`                    |
| `AddrType`       | `_StrictIntEnum` | RFC 1928 | §4      | `IPV4`, `DOMAIN`, `IPV6`                              |
| `Reply`          | `_StrictIntEnum` | RFC 1928 | §6      | `SUCCESS` … `ADDR_NOT_SUPPORTED` (9 codes)            |
| `UserPassStatus` | `IntEnum`        | RFC 1929 | §2      | `SUCCESS`, `FAILURE`                                  |

### 8.3 Unsupported-but-defined Values

Three values are defined for **wire-format completeness** but are not implemented
by this tunnel:

| Value               | Enum         | Reason defined                                  | Behaviour when received                       |
|---------------------|--------------|-------------------------------------------------|-----------------------------------------------|
| `GSSAPI`            | `AuthMethod` | RFC 1928 §3 requires it in the negotiation byte | Proxy layer responds with `NO_ACCEPT`         |
| `USERNAME_PASSWORD` | `AuthMethod` | RFC 1928 §3 requires it in the negotiation byte | Proxy layer responds with `NO_ACCEPT`         |
| `BIND`              | `Cmd`        | RFC 1928 §4 defines it                          | Proxy layer responds with `CMD_NOT_SUPPORTED` |

The tunnel accepts **only `NO_AUTH`** during method negotiation.
`GSSAPI` and `USERNAME_PASSWORD` are both in `_AUTH_METHOD_UNSUPPORTED` and
their `is_supported()` returns `False`.

```python
_AUTH_METHOD_UNSUPPORTED: Final[frozenset[AuthMethod]] = frozenset({
    AuthMethod.GSSAPI,
    AuthMethod.USERNAME_PASSWORD,
})

_CMD_UNSUPPORTED: Final[frozenset[Cmd]] = frozenset({Cmd.BIND})
```

### 8.4 `_missing_` Contract

```python
# Without _missing_ override:
AuthMethod(0x99)   # → None  (silent, dangerous)

# With _StrictIntEnum:
AuthMethod(0x99)   # → ValueError: 0x99 is not a valid AuthMethod ...
                   #   (immediate, informative)

# UserPassStatus permissive mapping (RFC 1929 §2):
UserPassStatus(0x01)  # → UserPassStatus.FAILURE  (any non-zero byte)
UserPassStatus(0xFE)  # → UserPassStatus.FAILURE
UserPassStatus(0x00)  # → UserPassStatus.SUCCESS
```

The proxy layer catches `ValueError` from `_StrictIntEnum` subclasses and maps
it to the appropriate SOCKS5 reply code.

### 8.5 `is_supported()` Method

`AuthMethod` and `Cmd` expose `is_supported()` on the concrete class (not on
the mixin) because each method closes over a **different** module-level
`frozenset`:

```python
_AUTH_METHOD_UNSUPPORTED: Final[frozenset[AuthMethod]] = frozenset({
    AuthMethod.GSSAPI,
    AuthMethod.USERNAME_PASSWORD,
})

_CMD_UNSUPPORTED: Final[frozenset[Cmd]] = frozenset({Cmd.BIND})
```

Hoisting `is_supported()` into `_StrictIntEnum` would require injecting the
unsupported set via a class variable or abstract property, adding complexity
that outweighs the savings. The two concrete implementations are intentionally
kept on their respective classes.

---

## 9. Exception Contract

The protocol layer raises exactly two exception types, both from
`exectunnel.exceptions`:

### 9.1 `ProtocolError` — encoder-side faults

Raised when a **caller passes invalid arguments** to an encoder. This indicates
a programming error in the calling layer (session, proxy, transport), not a
wire-format violation from a remote peer.

| Trigger                                          | Function           | `details` keys           |
|--------------------------------------------------|--------------------|--------------------------|
| Unknown `msg_type`                               | `_encode_frame`    | `frame_type`, `expected` |
| Non-no-conn_id frame missing `conn_id`           | `_encode_frame`    | `frame_type`, `expected` |
| `AGENT_READY` / `KEEPALIVE` frame with `conn_id` | `_encode_frame`    | `frame_type`, `expected` |
| `AGENT_READY` / `KEEPALIVE` frame with payload   | `_encode_frame`    | `frame_type`, `expected` |
| Malformed `conn_id` / `flow_id`                  | `_validate_id`     | `frame_type`, `expected` |
| Empty host                                       | `encode_host_port` | `frame_type`, `expected` |
| Port out of range `[1, 65535]`                   | `encode_host_port` | `frame_type`, `expected` |
| Frame-unsafe chars in host                       | `encode_host_port` | `frame_type`, `expected` |
| Consecutive dots in hostname                     | `encode_host_port` | `frame_type`, `expected` |
| Invalid hostname structure                       | `encode_host_port` | `frame_type`, `expected` |
| Payload contains frame suffix/prefix             | `_encode_frame`    | `frame_type`, `expected` |
| Payload contains newline                         | `_encode_frame`    | `frame_type`, `expected` |
| Encoded frame exceeds `MAX_FRAME_LEN`            | `_encode_frame`    | `frame_type`, `expected` |

### 9.2 `FrameDecodingError` — decoder-side faults

Raised when **data arriving from the wire** is structurally corrupt. This
indicates a remote peer violation or channel corruption, not a local programming
error.

| Trigger                                  | Function                | `details` keys                   |
|------------------------------------------|-------------------------|----------------------------------|
| Malformed bracketed IPv6 in OPEN payload | `parse_host_port`       | `raw_bytes`, `codec="host:port"` |
| Missing port separator in OPEN payload   | `parse_host_port`       | `raw_bytes`, `codec="host:port"` |
| Empty host in OPEN payload               | `parse_host_port`       | `raw_bytes`, `codec="host:port"` |
| Non-numeric port in OPEN payload         | `parse_host_port`       | `raw_bytes`, `codec="host:port"` |
| Port out of range in OPEN payload        | `parse_host_port`       | `raw_bytes`, `codec="host:port"` |
| Invalid base64url in DATA/UDP_DATA       | `decode_binary_payload` | `raw_bytes`, `codec="base64url"` |
| Non-UTF-8 bytes in ERROR payload         | `decode_error_payload`  | `raw_bytes`, `codec="utf-8"`     |
| Unrecognised `msg_type` in tunnel frame  | `parse_frame`           | `raw_bytes`, `codec="frame"`     |
| Malformed `conn_id` in tunnel frame      | `parse_frame`           | `raw_bytes`, `codec="frame"`     |
| Extra fields on no-conn_id frame type    | `parse_frame`           | `raw_bytes`, `codec="frame"`     |
| Missing `conn_id` on conn_id frame type  | `parse_frame`           | `raw_bytes`, `codec="frame"`     |
| Oversized tunnel frame                   | `parse_frame`           | `raw_bytes`, `codec="frame"`     |

### 9.3 `None` Return — not-a-frame

`parse_frame` returns `None` (never raises) when the input line does not carry
the tunnel prefix/suffix. This is the normal case for shell noise, blank lines,
and bootstrap stdout during agent startup.

```
Input line                              parse_frame result
──────────────────────────────────────  ──────────────────────────────────────
""                                      None   (blank line)
"bash-5.1$"                             None   (shell prompt)
"<<<EXECTUNNEL:AGENT_READY>>>"          ParsedFrame(msg_type="AGENT_READY", ...)
"<<<EXECTUNNEL:KEEPALIVE>>>"            ParsedFrame(msg_type="KEEPALIVE", conn_id=None, payload="")
"<<<EXECTUNNEL:BADTYPE:cXXX>>>"         FrameDecodingError  (tunnel frame, bad type)
"<<<EXECTUNNEL:DATA:BADID:abc>>>"       FrameDecodingError  (tunnel frame, bad ID)
"<<<EXECTUNNEL:DATA:cXXX:abc>>>"        ParsedFrame(msg_type="DATA", ...)
"x" * 9000                              None + debug log  (oversized non-frame)
"<<<EXECTUNNEL:" + "x"*9000 + ">>>"    FrameDecodingError  (oversized tunnel frame)
```

### 9.4 Exception Chaining

All `FrameDecodingError` raises that wrap a stdlib exception use
`raise ... from exc`:

```
binascii.Error        →  FrameDecodingError  (decode_binary_payload)
ValueError (int())    →  FrameDecodingError  (parse_host_port)
UnicodeDecodeError    →  FrameDecodingError  (decode_error_payload)
```

This preserves the full traceback chain for structured logging via
`exc.to_dict()`.

---

## 10. Data Flow Diagrams

### 10.1 Outbound Path (Client → Agent)

```
Session / Proxy layer
        │
        │  encode_conn_open_frame(conn_id, host, port)
        │  encode_conn_ack_frame(conn_id)
        │  encode_data_frame(conn_id, data)
        │  encode_udp_data_frame(flow_id, data)
        │  encode_keepalive_frame()
        │  ...
        ▼
  encode_host_port(host, port)          [OPEN frames only]
        │  rejects empty host, port ∉ [1,65535], frame-unsafe chars,
        │  consecutive dots, invalid hostname structure
        │  normalises IPv6 to compressed bracket-quoted form
        ▼
  base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
        │                                 [DATA / UDP_DATA / ERROR frames only]
        ▼
  _encode_frame(msg_type, conn_id, payload)
        │  validates msg_type ∈ _VALID_MSG_TYPES
        │  enforces conn_id presence/absence per _NO_CONN_ID_TYPES
        │  validates conn_id against CONN_FLOW_ID_RE
        │  defence-in-depth: rejects payload containing FRAME_PREFIX/SUFFIX/newline
        │  checks total length ≤ MAX_FRAME_LEN
        ▼
  "<<<EXECTUNNEL:DATA:c1a2b3...:UEVSR1xy>>>\n"
        │
        ▼
  Transport layer  (sends over WebSocket)
```

### 10.2 Inbound Path (Agent → Client)

```
Transport layer  (receives from WebSocket, splits on \n)
        │
        │  raw line: "<<<EXECTUNNEL:DATA:c1a2b3...:UEVSR1xy>>>"
        ▼
  parse_frame(line)
        │  1. strip()
        │  2. check prefix + suffix  →  None if absent (non-frame, never an error)
        │  3. check length ≤ MAX_FRAME_LEN  →  FrameDecodingError if oversized tunnel frame
        │  4. split(":", 2) on inner content
        │  5. validate msg_type  →  FrameDecodingError if unknown
        │  6. enforce _NO_CONN_ID_TYPES carry no extra fields
        │  7. validate conn_id   →  FrameDecodingError if malformed or missing
        ▼
  ParsedFrame(msg_type="DATA", conn_id="c1a2b3...", payload="UEVSR1xy")
        │
        ▼
  Session layer dispatches on msg_type:
        │
        ├─ DATA / UDP_DATA  →  decode_binary_payload(payload)  →  bytes
        │                         FrameDecodingError if bad base64url
        │
        ├─ CONN_OPEN / UDP_OPEN  →  parse_host_port(payload)  →  (host, port)
        │                              FrameDecodingError if malformed
        │
        ├─ ERROR  →  decode_error_payload(payload)  →  str
        │               FrameDecodingError if bad base64url or bad UTF-8
        │
        ├─ CONN_ACK / CONN_CLOSE / UDP_CLOSE  →  no payload decode needed
        │
        └─ AGENT_READY / KEEPALIVE  →  conn_id is None, payload is ""
```

### 10.3 Bootstrap Sequence

```
Client                                    Agent (exec'd into pod)
  │                                           │
  │  kubectl exec → WebSocket open            │
  │ ─────────────────────────────────────►   │
  │                                           │  agent.py starts
  │                                           │  sets up sockets
  │                                           │  prints READY_FRAME
  │  "<<<EXECTUNNEL:AGENT_READY>>>\n"         │
  │ ◄─────────────────────────────────────   │
  │                                           │
  │  is_ready_frame(line) → True              │
  │  TunnelSession begins                     │
  │                                           │
  │  CONN_OPEN / UDP_OPEN frames              │
  │ ─────────────────────────────────────►   │
  │                                           │
  │  CONN_ACK frames                          │
  │ ◄─────────────────────────────────────   │
  │                                           │
  │  KEEPALIVE frames (periodic)              │
  │ ─────────────────────────────────────►   │
  │                                           │  (silently discarded)
```

**Bootstrap loop pattern** (session/transport layer responsibility):

```python
async for line in channel:
    try:
        if is_ready_frame(line):
            break
        # Optionally call parse_frame(line) here to detect and log
        # early protocol faults before the tunnel is up.
    except FrameDecodingError:
        log.warning("corrupt frame during bootstrap: %r", line)
```

`is_ready_frame` is a **pure string predicate** — it never raises. The decision
to propagate or swallow `FrameDecodingError` during the pre-ready scan belongs
to the bootstrap layer, not to `is_ready_frame`.

---

## 11. `ParsedFrame` Design

```python
@dataclass(frozen=True, slots=True)
class ParsedFrame:
    msg_type: str
    conn_id:  str | None
    payload:  str
```

### Design Decisions

| Decision                      | Rationale                                                                                                                                                                                              |
|-------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `frozen=True`                 | Frames are immutable value objects; mutation after parsing is a bug                                                                                                                                    |
| `slots=True`                  | Eliminates `__dict__` per instance; significant on the hot inbound path where thousands of frames/second may be parsed                                                                                 |
| `dataclass` over `NamedTuple` | Better `repr`, supports `field()` metadata, forward-compatible with `__post_init__` validation if needed                                                                                               |
| `conn_id: str \| None`        | `None` makes the absence of an ID explicit for `AGENT_READY` and `KEEPALIVE`; callers cannot accidentally treat an absent ID as a valid empty string; type checkers correctly flag missing None-checks |

### Field Invariants

| Field      | Value                                                     | Meaning                                                                                    |
|------------|-----------------------------------------------------------|--------------------------------------------------------------------------------------------|
| `msg_type` | Never empty (validated)                                   | One of `_VALID_MSG_TYPES`                                                                  |
| `conn_id`  | `None` for `AGENT_READY` and `KEEPALIVE`; `str` otherwise | Validated against `CONN_FLOW_ID_RE` when present                                           |
| `payload`  | `""` when absent                                          | Raw string; not yet decoded — callers must pass it through the appropriate decode function |

---

## 12. Public API Reference

### Constants

| Name               | Type  | Value                            | Purpose                                         |
|--------------------|-------|----------------------------------|-------------------------------------------------|
| `FRAME_PREFIX`     | `str` | `"<<<EXECTUNNEL:"`               | Frame start sentinel                            |
| `FRAME_SUFFIX`     | `str` | `">>>"`                          | Frame end sentinel                              |
| `READY_FRAME`      | `str` | `"<<<EXECTUNNEL:AGENT_READY>>>"` | Bootstrap complete sentinel                     |
| `SESSION_CONN_ID`  | `str` | `"c" + "0"×24`                   | Session-level error conn_id                     |
| `MAX_FRAME_LEN`    | `int` | `8_192`                          | Max frame content chars (excl. `\n`)            |
| `PORT_UNSPECIFIED` | `int` | `0`                              | RFC 1928 §6 "unspecified" port — proxy use only |

### Frame Encoders

| Function                  | Arguments               | Returns | Raises           |
|---------------------------|-------------------------|---------|------------------|
| `encode_conn_open_frame`  | `conn_id, host, port`   | `str`   | `ProtocolError`  |
| `encode_conn_ack_frame`   | `conn_id`               | `str`   | `ProtocolError`  |
| `encode_conn_close_frame` | `conn_id`               | `str`   | `ProtocolError`  |
| `encode_data_frame`       | `conn_id, data: bytes`  | `str`   | `ProtocolError`  |
| `encode_udp_open_frame`   | `flow_id, host, port`   | `str`   | `ProtocolError`  |
| `encode_udp_data_frame`   | `flow_id, data: bytes`  | `str`   | `ProtocolError`  |
| `encode_udp_close_frame`  | `flow_id`               | `str`   | `ProtocolError`  |
| `encode_error_frame`      | `conn_id, message: str` | `str`   | `ProtocolError`  |
| `encode_keepalive_frame`  | *(none)*                | `str`   | — (never raises) |

### Frame Decoder

| Function         | Arguments   | Returns               | Raises               |
|------------------|-------------|-----------------------|----------------------|
| `parse_frame`    | `line: str` | `ParsedFrame \| None` | `FrameDecodingError` |
| `is_ready_frame` | `line: str` | `bool`                | — (never raises)     |

### Payload Helpers

| Function                | Arguments              | Returns           | Raises               |
|-------------------------|------------------------|-------------------|----------------------|
| `decode_binary_payload` | `payload: str`         | `bytes`           | `FrameDecodingError` |
| `decode_error_payload`  | `payload: str`         | `str`             | `FrameDecodingError` |
| `encode_host_port`      | `host: str, port: int` | `str`             | `ProtocolError`      |
| `parse_host_port`       | `payload: str`         | `tuple[str, int]` | `FrameDecodingError` |

### ID Generators & Validators

| Symbol            | Kind     | Returns           | Notes                                                     |
|-------------------|----------|-------------------|-----------------------------------------------------------|
| `new_conn_id`     | function | `str`             | `c[0-9a-f]{24}` — TCP connection ID                       |
| `new_flow_id`     | function | `str`             | `u[0-9a-f]{24}` — UDP flow ID                             |
| `new_session_id`  | function | `str`             | `s[0-9a-f]{24}` — session correlation ID (not in frames)  |
| `CONN_FLOW_ID_RE` | constant | `re.Pattern[str]` | Canonical validator for `c`/`u` IDs; used by all encoders |
| `ID_RE`           | constant | `re.Pattern[str]` | Backward-compat alias for `CONN_FLOW_ID_RE` (same object) |
| `SESSION_ID_RE`   | constant | `re.Pattern[str]` | Validator for `s` session IDs                             |
| `SESSION_CONN_ID` | constant | `str`             | `"c" + "0"×24` — session-level error sentinel             |

### SOCKS5 Enums

| Enum             | Base             | Members                                               | RFC         | Notes                                                                        |
|------------------|------------------|-------------------------------------------------------|-------------|------------------------------------------------------------------------------|
| `AuthMethod`     | `_StrictIntEnum` | `NO_AUTH`, `GSSAPI`, `USERNAME_PASSWORD`, `NO_ACCEPT` | RFC 1928 §3 | `GSSAPI` and `USERNAME_PASSWORD` wire-only; both `is_supported()` → `False`  |
| `Cmd`            | `_StrictIntEnum` | `CONNECT`, `BIND`, `UDP_ASSOCIATE`                    | RFC 1928 §4 | `BIND` wire-only; `is_supported()` → `False`                                 |
| `AddrType`       | `_StrictIntEnum` | `IPV4`, `DOMAIN`, `IPV6`                              | RFC 1928 §4 | —                                                                            |
| `Reply`          | `_StrictIntEnum` | `SUCCESS` … `ADDR_NOT_SUPPORTED` (9 codes)            | RFC 1928 §6 | —                                                                            |
| `UserPassStatus` | `IntEnum`        | `SUCCESS`, `FAILURE`                                  | RFC 1929 §2 | Any non-zero byte → `FAILURE`; inherits plain `IntEnum` not `_StrictIntEnum` |

---

## 13. Invariants & Constraints

These are hard invariants that all layers must respect:

```
1.  parse_frame(encode_*(args)) is never None
    — every encoded frame round-trips through the parser

2.  parse_frame(line) returns None  iff  line has no FRAME_PREFIX+FRAME_SUFFIX
    — None is exclusively "not a tunnel frame", never "corrupt tunnel frame"
    — this holds regardless of line length

3.  FrameDecodingError is raised  iff  line IS a tunnel frame but is corrupt
    — the distinction between None and FrameDecodingError is load-bearing
    — the prefix/suffix check in parse_frame MUST precede the length check

4.  encode_host_port / parse_host_port are strict inverses on the semantic value
    — parse_host_port(encode_host_port(h, p))[1] == p  always
    — parse_host_port(encode_host_port(h, p))[0] == normalise(h)

5.  SESSION_CONN_ID passes CONN_FLOW_ID_RE but is never produced by new_conn_id()
    — callers may use `conn_id == SESSION_CONN_ID` as a reliable sentinel check

6.  All encoded frames are newline-terminated
    — transport layer may split on "\n" without any other framing

7.  All payload bytes in DATA/UDP_DATA/ERROR frames are base64url (no padding)
    — the payload field of ParsedFrame is always safe to pass to decode_binary_payload

8.  No frame field ever contains FRAME_PREFIX, FRAME_SUFFIX, or a newline
    — enforced by _encode_frame as defence-in-depth; raises ProtocolError

9.  AGENT_READY and KEEPALIVE are the only frame types with no conn_id
    — _encode_frame enforces this via _NO_CONN_ID_TYPES = {"AGENT_READY", "KEEPALIVE"}
    — ParsedFrame.conn_id is None if and only if msg_type ∈ _NO_CONN_ID_TYPES
    — parse_frame raises FrameDecodingError if extra fields appear on these types

10. is_ready_frame never raises
    — it is a pure string predicate; bootstrap policy is the caller's concern

11. SESSION_ID_RE and CONN_FLOW_ID_RE are mutually exclusive
    — a session ID (s-prefix) will never match CONN_FLOW_ID_RE
    — a conn/flow ID (c/u-prefix) will never match SESSION_ID_RE
    — session IDs must never be passed as conn_id to any frame encoder

12. encode_keepalive_frame() never raises
    — it accepts no arguments; the frame is hardcoded and always within limits
```

---

## 14. Extension Points

### Adding a New Frame Type

```
1.  Add the string to _VALID_MSG_TYPES in frames.py
2.  If the new frame carries no conn_id, add it to _NO_CONN_ID_TYPES in frames.py
    and update invariant 9 in §13 of this document
3.  Add a typed encode_<name>_frame() function
4.  Add the new function to __all__ in frames.py and __init__.py
5.  Update the frame catalogue docstring in frames.py
6.  Update the Frame Grammar (§4.1) and Frame Catalogue (§4.2) in this document
7.  Add a concrete wire example to §4.3
8.  Update the session layer dispatcher to handle the new msg_type
9.  Update the agent to emit / consume the new frame type
```

### Adding a New SOCKS5 Auth Method

```
1.  Add the value to AuthMethod in enums.py
2.  If it is defined-but-not-implemented (like GSSAPI / USERNAME_PASSWORD),
    document it in the class docstring and add it to _AUTH_METHOD_UNSUPPORTED
3.  Update the proxy layer negotiation handler
4.  Update §8.3 and §8.4 in this document
```

### Changing the ID Format

```
1.  Update _TCP_PREFIX, _UDP_PREFIX, _SESSION_PREFIX, or _TOKEN_BYTES in ids.py
2.  CONN_FLOW_ID_RE, SESSION_ID_RE, and SESSION_CONN_ID are derived from these
    constants and update automatically — verify they still satisfy their own
    patterns after the change
3.  Verify SESSION_CONN_ID still cannot be produced by new_conn_id()
4.  Update §5 in this document
5.  Bump the agent version — ID format is part of the wire protocol
```

---

## 15. Security Considerations

| Threat                                    | Mitigation                                                                                                                           |
|-------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------|
| Frame injection via crafted payload       | `_encode_frame` defence-in-depth check rejects payloads containing `FRAME_PREFIX`, `FRAME_SUFFIX`, or newlines → `ProtocolError`     |
| Frame injection via crafted hostname      | `encode_host_port` rejects `:`, `<`, `>` and consecutive dots in domain names → `ProtocolError`                                      |
| Memory exhaustion via oversized frame     | `parse_frame` raises `FrameDecodingError` for oversized tunnel frames; silently drops oversized non-frame lines → `None` + debug log |
| Memory exhaustion check order             | Prefix/suffix check precedes length check — oversized non-frame lines can never trigger `FrameDecodingError`                         |
| ID collision / prediction                 | `secrets.token_hex` (CSPRNG); 96-bit entropy; birthday bound ≈ 2^48                                                                  |
| IPv6 ambiguity / confusion                | All IPv6 literals normalised to compressed form and bracket-quoted by `encode_host_port`                                             |
| Corrupt base64url crashing decoder        | `binascii.Error` caught and re-raised as `FrameDecodingError` with truncated hex excerpt                                             |
| Non-UTF-8 error messages crashing decoder | `UnicodeDecodeError` caught and re-raised as `FrameDecodingError`                                                                    |
| `SESSION_CONN_ID` collision with real ID  | All-zero token is outside CSPRNG output space; probability ≈ \(2^{-96}\)                                                             |
| Missing conn_id on conn_id frame type     | `parse_frame` raises `FrameDecodingError`; `_encode_frame` raises `ProtocolError`                                                    |
| Unexpected conn_id on no-conn_id frame    | `_encode_frame` raises `ProtocolError`; `parse_frame` raises `FrameDecodingError` — enforced via `_NO_CONN_ID_TYPES`                 |
| Session ID embedded in frame              | `CONN_FLOW_ID_RE` rejects `s`-prefix IDs — `_encode_frame` raises `ProtocolError` if a session ID is passed as `conn_id`             |
| Proxy-injected suffix corruption          | `_strip_proxy_suffix` truncates at the last `>>>` before parsing — tolerates trace metadata appended by intermediate proxies         |

---

## Summary of Changes from v1.1

| Section                       | Change                                                                                                     |
|-------------------------------|------------------------------------------------------------------------------------------------------------|
| §4.1 Frame Grammar            | Added `CONN_ACK` and `KEEPALIVE` to `msg_type` production; added no-conn_id type note                      |
| §4.2 Frame Catalogue          | Added `CONN_ACK` and `KEEPALIVE` rows; added `CONN_ACK` ordering note                                      |
| §4.3 Concrete Examples        | Added `CONN_ACK` and `KEEPALIVE` wire examples                                                             |
| §5.1 ID Format                | Added `session_id` format (`s<24 hex>`) and clarified where session IDs are used                           |
| §5.3 Prefix Namespace         | Extended to include `s` prefix; clarified session IDs are never in frames                                  |
| §5.4 (new) Compiled Patterns  | Documents `CONN_FLOW_ID_RE` as canonical, `ID_RE` as alias, and `SESSION_ID_RE`                            |
| §5.5–5.7 (renumbered)         | Former §5.4–5.5 renumbered; `new_session_id()` section added as §5.6                                       |
| §6.3 Port Range               | Added `PORT_UNSPECIFIED = 0` explanation and proxy-layer asymmetry                                         |
| §8.3 Unsupported-but-defined  | Added `USERNAME_PASSWORD` row; corrected `_AUTH_METHOD_UNSUPPORTED` frozenset                              |
| §8.5 `is_supported()`         | Updated frozenset literal to include `USERNAME_PASSWORD`                                                   |
| §9.1 ProtocolError table      | Added `KEEPALIVE` to the no-conn_id checks; added payload-contains-newline trigger                         |
| §9.2 FrameDecodingError table | Added extra-fields-on-no-conn_id and missing-conn_id rows                                                  |
| §9.3 None Return table        | Added `KEEPALIVE` parse result row                                                                         |
| §10.1 Outbound diagram        | Added `encode_conn_ack_frame` and `encode_keepalive_frame`; added `CONN_FLOW_ID_RE` reference              |
| §10.2 Inbound diagram         | Added steps 6–7; added `CONN_ACK`, `CONN_CLOSE`, `UDP_CLOSE`, `AGENT_READY`, `KEEPALIVE` dispatch branches |
| §10.3 Bootstrap Sequence      | Added `CONN_ACK` and `KEEPALIVE` to sequence diagram                                                       |
| §11 ParsedFrame               | Updated `conn_id` rationale — now covers both `AGENT_READY` and `KEEPALIVE`                                |
| §11 Field Invariants          | `conn_id` row updated to reference `_NO_CONN_ID_TYPES` and `CONN_FLOW_ID_RE`                               |
| §12 Constants                 | Added `PORT_UNSPECIFIED`                                                                                   |
| §12 Frame Encoders            | Added `encode_conn_ack_frame` and `encode_keepalive_frame`                                                 |
| §12 ID Generators table       | Added `new_session_id`, `CONN_FLOW_ID_RE`, `SESSION_ID_RE`; clarified `ID_RE` alias                        |
| §12 SOCKS5 Enums table        | Corrected `AuthMethod` notes — both `GSSAPI` and `USERNAME_PASSWORD` marked wire-only                      |
| §13 Invariants                | Updated invariant 9 (KEEPALIVE added); added invariants 11 and 12                                          |
| §14 Extension Points          | Updated "Adding a New Frame Type" step 2; step numbering extended                                          |
| §15 Security                  | Added session-ID-in-frame and proxy-suffix-tolerance rows                                                  |

```
