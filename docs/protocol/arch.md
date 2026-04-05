# ExecTunnel — Protocol Package Architecture Document

```
exectunnel/protocol/  |  arch-doc v1.0  |  Python 3.13+
```

---

## 1. Purpose & Scope

The `protocol` package is the **lowest layer** of the ExecTunnel stack. It owns exactly two concerns:

1. **Wire encoding / decoding** — transforming typed Python values into newline-terminated ASCII frame strings and back.
2. **SOCKS5 enumeration** — providing RFC 1928 / RFC 1929 integer constants as typed Python enums.

It has **zero I/O dependencies**. No sockets, no asyncio, no WebSocket, no DNS, no threads. Every function is a pure transformation: bytes/strings in, bytes/strings/structs out. This makes the entire package synchronously testable in isolation and safe to import in any execution context including the in-pod agent.

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
├── enums.py        SOCKS5 IntEnum definitions
└── ids.py          Cryptographic ID generation + ID_RE validator
```

### Responsibility Matrix

| Module | Owns | Does not own |
|---|---|---|
| `frames.py` | Frame constants, wire format, encode/decode, host/port codec | ID generation, SOCKS5 semantics, I/O |
| `enums.py` | SOCKS5 RFC integer constants | Frame format, ID format, I/O |
| `ids.py` | ID generation, ID validation regex | Frame format, SOCKS5, I/O |
| `__init__.py` | Public API surface, `__all__` | All logic — zero lines of code beyond imports |

---

## 4. Wire Format Specification

### 4.1 Frame Grammar

```
frame       ::= FRAME_PREFIX msg_type [ ":" conn_id [ ":" payload ] ] FRAME_SUFFIX LF
FRAME_PREFIX ::= "<<<EXECTUNNEL:"
FRAME_SUFFIX ::= ">>>"
LF           ::= "\n"
msg_type    ::= "AGENT_READY" | "CONN_OPEN" | "CONN_CLOSE" | "DATA"
              | "UDP_OPEN"   | "UDP_DATA"  | "UDP_CLOSE"  | "ERROR"
conn_id     ::= [cu][0-9a-f]{24}
payload     ::= host_port | base64url_nopad
host_port   ::= bare_host ":" port | "[" ipv6_addr "]" ":" port
base64url_nopad ::= [A-Za-z0-9\-_]*
```

### 4.2 Frame Catalogue

| Frame | conn_id | Payload | Direction | Meaning |
|---|---|---|---|---|
| `AGENT_READY` | — | — | Agent → Client | Bootstrap complete; agent is ready |
| `CONN_OPEN` | TCP conn ID | `host:port` | Client → Agent | Open a TCP connection to target |
| `CONN_CLOSE` | TCP conn ID | — | Both | Explicit TCP teardown |
| `DATA` | TCP conn ID | base64url bytes | Both | TCP data chunk |
| `UDP_OPEN` | UDP flow ID | `host:port` | Client → Agent | Open a UDP flow to target |
| `UDP_DATA` | UDP flow ID | base64url bytes | Both | UDP datagram |
| `UDP_CLOSE` | UDP flow ID | — | Both | Explicit UDP flow teardown |
| `ERROR` | conn/flow ID or `SESSION_CONN_ID` | base64url UTF-8 | Both | Error report |

### 4.3 Concrete Frame Examples

```
# Agent signals readiness
<<<EXECTUNNEL:AGENT_READY>>>

# Client opens TCP connection c1a2b3... to redis:6379
<<<EXECTUNNEL:CONN_OPEN:ca1b2c3d4e5f6a7b8c9d0e1f2a3b:redis:6379>>>

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
```

### 4.4 Frame Length Budget

```
MAX_FRAME_LEN = 8,192 characters  (content only, excluding trailing \n)

Breakdown for a DATA frame at the limit:
  FRAME_PREFIX  = 14 chars   "<<<EXECTUNNEL:"
  msg_type      =  4 chars   "DATA"
  separator     =  1 char    ":"
  conn_id       = 25 chars   "c" + 24 hex
  separator     =  1 char    ":"
  payload       = 8,147 chars  base64url
  FRAME_SUFFIX  =  3 chars   ">>>"
  ─────────────────────────────────────
  Total content = 8,195 chars  → exceeds limit → ProtocolError

Maximum safe DATA payload:
  available_for_payload = 8192 - 14 - 4 - 1 - 25 - 1 - 3 = 8,144 chars
  base64url overhead    = 4/3 ratio
  max raw bytes         = floor(8144 * 3 / 4) = 6,108 bytes
```

> **Transport note:** `PIPE_READ_CHUNK_BYTES` (4,096) is deliberately set below this limit. It lives in the transport/session layer, not here.

---

## 5. ID System

### 5.1 Format

```
conn_id  ::=  "c"  <24 lowercase hex chars>   (TCP connections)
flow_id  ::=  "u"  <24 lowercase hex chars>   (UDP flows)
```

### 5.2 Entropy & Collision Analysis

```
Token source  : secrets.token_hex(12)  →  12 bytes  →  96 bits of entropy
Birthday bound: P(collision) ≈ 0.5 at n ≈ 2^48 ≈ 281 trillion IDs

At 10,000 concurrent connections/s:
  Time to 50% collision probability ≈ 2^48 / 10,000 / 86,400 / 365
                                    ≈ 891,000 years
```

### 5.3 Prefix Namespace Isolation

The `c` / `u` prefix ensures that a TCP conn_id and a UDP flow_id can **never collide** even if their 96-bit tokens are identical. This matters in the session layer's connection table, which stores both types under the same dict.

```
"c" + token_hex(12)  →  TCP namespace
"u" + token_hex(12)  →  UDP namespace
```

### 5.4 `SESSION_CONN_ID` Sentinel

```python
SESSION_CONN_ID = "c" + "0" * 24   # = "c000000000000000000000000"
```

This is a **structurally valid** conn_id (passes `ID_RE`) that is **semantically reserved** — it can never be produced by `new_conn_id()` because `secrets.token_hex` never returns all-zero output for a 12-byte token (probability \(2^{-96}\), effectively impossible).

Callers use it to distinguish:

```
conn_id == SESSION_CONN_ID  →  session-level error (affects all connections)
conn_id != SESSION_CONN_ID  →  per-connection error (affects one connection)
```

---

## 6. Host / Port Codec

### 6.1 Encoding Rules

```
IPv4 literal   →  bare:          "192.168.1.1:8080"
IPv6 literal   →  bracket-quoted: "[2001:db8::1]:8080"
Domain name    →  bare:          "redis.default.svc:6379"
```

IPv6 addresses are **always** normalised to compressed form via `ipaddress.IPv6Address.compressed` before embedding. This prevents ambiguity between `::1` and `0:0:0:0:0:0:0:1`.

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
* Any of `:`, `<`, `>` (frame-unsafe, caught before regex)

Full RFC 1123 compliance (label length ≤ 63, total ≤ 253) is **deliberately delegated** to the resolver. The protocol layer's job is only to ensure the host string cannot corrupt the frame wire format.

### 6.3 Codec Symmetry Contract

```
parse_host_port(encode_host_port(host, port)) == (normalised_host, port)
```

The `host` returned by `parse_host_port` may differ from the input to `encode_host_port` for IPv6 addresses because `ipaddress` normalises them. Callers must not assume round-trip identity of the raw string — only of the semantic value.

---

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

Padding restoration formula: `(4 - len(s) % 4) % 4` produces `0, 1, 2, or 3` padding chars. The `% 4` at the end ensures that a string already aligned to 4 chars gets `0` padding, not `4`.

### 7.3 Typed Decode Functions

| Function | Input | Output | Used for |
|---|---|---|---|
| `decode_binary_payload` | base64url `str` | `bytes` | `DATA`, `UDP_DATA` frames |
| `decode_error_payload` | base64url `str` | `str` (UTF-8) | `ERROR` frames |

`decode_error_payload` chains two decode steps and raises `FrameDecodingError` at either step, preserving the original cause via `from exc`.

---

## 8. SOCKS5 Enumerations

### 8.1 RFC Coverage

| Enum | RFC | Section | Values |
|---|---|---|---|
| `AuthMethod` | RFC 1928 | §3 | `NO_AUTH`, `GSSAPI`, `USERNAME_PASSWORD`, `NO_ACCEPT` |
| `Cmd` | RFC 1928 | §4 | `CONNECT`, `BIND`, `UDP_ASSOCIATE` |
| `AddrType` | RFC 1928 | §4 | `IPV4`, `DOMAIN`, `IPV6` |
| `Reply` | RFC 1928 | §6 | `SUCCESS` … `ADDR_NOT_SUPPORTED` (9 codes) |
| `UserPassStatus` | RFC 1929 | §2 | `SUCCESS`, `FAILURE` |

### 8.2 Unsupported-but-defined Values

Two values are defined for **wire-format completeness** but are not implemented:

| Value | Enum | Reason defined | Behaviour when received |
|---|---|---|---|
| `GSSAPI` | `AuthMethod` | RFC 1928 §3 requires it in the negotiation byte | Proxy layer responds with `NO_ACCEPT` |
| `BIND` | `Cmd` | RFC 1928 §4 defines it | Proxy layer responds with `CMD_NOT_SUPPORTED` |

### 8.3 `_missing_` Contract

All enums override `_missing_` to raise `ValueError` immediately on unknown wire values rather than returning `None`. This surfaces protocol violations at the earliest possible point — the moment the byte is read off the wire — rather than propagating a `None` that causes a confusing `AttributeError` later.

```python
# Without _missing_ override:
AuthMethod(0x99)   # → None  (silent, dangerous)

# With _missing_ override:
AuthMethod(0x99)   # → ValueError: 0x99 is not a valid AuthMethod ...
                   #   (immediate, informative)
```

The proxy layer catches this `ValueError` and maps it to the appropriate SOCKS5 reply code.

---

## 9. Exception Contract

The protocol layer raises exactly two exception types, both from `exectunnel.exceptions`:

### 9.1 `ProtocolError` — encoder-side faults

Raised when a **caller passes invalid arguments** to an encoder. This indicates a programming error in the calling layer (session, proxy, transport), not a wire-format violation from a remote peer.

| Trigger | Function | `details` keys |
|---|---|---|
| Unknown `msg_type` | `_validate_msg_type` | `frame_type`, `expected` |
| Malformed `conn_id` / `flow_id` | `_validate_id` | `frame_type`, `expected` |
| Empty host | `encode_host_port` | `frame_type`, `expected` |
| Port out of range | `encode_host_port` | `frame_type`, `expected` |
| Frame-unsafe chars in host | `encode_host_port` | `frame_type`, `expected` |
| Invalid hostname structure | `encode_host_port` | `frame_type`, `expected` |
| Payload contains frame suffix/prefix | `_encode_frame` | `frame_type`, `expected` |
| Encoded frame exceeds `MAX_FRAME_LEN` | `_encode_frame` | `frame_type`, `expected` |

### 9.2 `FrameDecodingError` — decoder-side faults

Raised when **data arriving from the wire** is structurally corrupt. This indicates a remote peer violation or channel corruption, not a local programming error.

| Trigger | Function | `details` keys |
|---|---|---|
| Malformed bracketed IPv6 in OPEN payload | `parse_host_port` | `raw_bytes`, `codec="host:port"` |
| Missing port separator in OPEN payload | `parse_host_port` | `raw_bytes`, `codec="host:port"` |
| Empty host in OPEN payload | `parse_host_port` | `raw_bytes`, `codec="host:port"` |
| Non-numeric port in OPEN payload | `parse_host_port` | `raw_bytes`, `codec="host:port"` |
| Port out of range in OPEN payload | `parse_host_port` | `raw_bytes`, `codec="host:port"` |
| Invalid base64url in DATA/UDP_DATA | `decode_binary_payload` | `raw_bytes`, `codec="base64url"` |
| Non-UTF-8 bytes in ERROR payload | `decode_error_payload` | `raw_bytes`, `codec="utf-8"` |
| Unrecognised `msg_type` in tunnel frame | `parse_frame` | `raw_bytes`, `codec="frame"` |
| Malformed `conn_id` in tunnel frame | `parse_frame` | `raw_bytes`, `codec="frame"` |

### 9.3 `None` Return — not-a-frame

`parse_frame` returns `None` (never raises) when the input line does not carry the tunnel prefix/suffix. This is the normal case for shell noise, blank lines, and bootstrap stdout during agent startup.

```
Input line                          parse_frame result
──────────────────────────────────  ──────────────────────────────────
""                                  None   (blank line)
"bash-5.1$"                         None   (shell prompt)
"<<<EXECTUNNEL:AGENT_READY>>>"      ParsedFrame(msg_type="AGENT_READY", ...)
"<<<EXECTUNNEL:BADTYPE:cXXX>>>"     FrameDecodingError  (tunnel frame, bad type)
"<<<EXECTUNNEL:DATA:BADID:abc>>>"   FrameDecodingError  (tunnel frame, bad ID)
"<<<EXECTUNNEL:DATA:cXXX:abc>>>"    ParsedFrame(msg_type="DATA", ...)
```

### 9.4 Exception Chaining

All `FrameDecodingError` raises that wrap a stdlib exception use `raise ... from exc`:

```
binascii.Error          →  FrameDecodingError  (decode_binary_payload)
ValueError (int())      →  FrameDecodingError  (parse_host_port)
UnicodeDecodeError      →  FrameDecodingError  (decode_error_payload)
```

This preserves the full traceback chain for structured logging via `exc.to_dict()`.

---

## 10. Data Flow Diagrams

### 10.1 Outbound Path (Client → Agent)

```
Session / Proxy layer
        │
        │  encode_conn_open_frame(conn_id, host, port)
        │  encode_data_frame(conn_id, data)
        │  encode_udp_data_frame(flow_id, data)
        │  ...
        ▼
  encode_host_port(host, port)          ← validates host/port, returns wire string
  base64.urlsafe_b64encode(data)        ← encodes bytes to ASCII-safe string
        │
        ▼
  _encode_frame(msg_type, conn_id, payload)
        │  validates msg_type, conn_id
        │  checks payload for injection chars
        │  checks total length ≤ MAX_FRAME_LEN
        ▼
  "<<<EXECTUNNEL:DATA:c1a2b3...:UEVSR1xy>>>\n"
        │
        ▼
  Transport layer  (sends over WebSocket)
```

### 10.2 Inbound Path (Agent → Client)

```
Transport layer  (receives from WebSocket)
        │
        │  raw line: "<<<EXECTUNNEL:DATA:c1a2b3...:UEVSR1xy>>>"
        ▼
  parse_frame(line)
        │  strips whitespace
        │  checks prefix / suffix  →  None if absent (not a tunnel frame)
        │  checks length ≤ MAX_FRAME_LEN  →  FrameDecodingError if tunnel frame is oversized
        │  validates msg_type      →  FrameDecodingError if unknown
        │  validates conn_id       →  FrameDecodingError if malformed
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
        └─ ERROR  →  decode_error_payload(payload)  →  str
                        FrameDecodingError if bad base64url or bad UTF-8
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
```

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

| Decision | Rationale |
|---|---|
| `frozen=True` | Frames are immutable value objects; mutation after parsing is a bug |
| `slots=True` | Eliminates `__dict__` per instance; significant on the hot inbound path where thousands of frames/second may be parsed |
| `dataclass` over `NamedTuple` | Better `repr`, supports `field()` metadata, forward-compatible with `__post_init__` validation if needed |
| `conn_id` is `str | None` | `None` makes the absence of an ID explicit for `AGENT_READY`; callers cannot accidentally treat an absent ID as a valid empty string |

### Field Invariants

| Field | Value | Meaning |
|---|---|---|
| `msg_type` | Never empty (validated) | One of `_VALID_MSG_TYPES` |
| `conn_id` | `None` for `AGENT_READY`; `str` otherwise | Validated against `ID_RE` when present |
| `payload` | `""` when absent | Raw string; not yet decoded |

---

## 12. Public API Reference

### Constants

| Name | Type | Value | Purpose |
|---|---|---|---|
| `FRAME_PREFIX` | `str` | `"<<<EXECTUNNEL:"` | Frame start sentinel |
| `FRAME_SUFFIX` | `str` | `">>>"` | Frame end sentinel |
| `READY_FRAME` | `str` | `"<<<EXECTUNNEL:AGENT_READY>>>"` | Bootstrap complete sentinel |
| `SESSION_CONN_ID` | `str` | `"c" + "0"×24` | Session-level error conn_id |
| `MAX_FRAME_LEN` | `int` | `8_192` | Max frame content chars |

### Frame Encoders

| Function | Arguments | Returns | Raises |
|---|---|---|---|
| `encode_conn_open_frame` | `conn_id, host, port` | `str` | `ProtocolError` |
| `encode_conn_close_frame` | `conn_id` | `str` | `ProtocolError` |
| `encode_data_frame` | `conn_id, data: bytes` | `str` | `ProtocolError` |
| `encode_udp_open_frame` | `flow_id, host, port` | `str` | `ProtocolError` |
| `encode_udp_data_frame` | `flow_id, data: bytes` | `str` | `ProtocolError` |
| `encode_udp_close_frame` | `flow_id` | `str` | `ProtocolError` |
| `encode_error_frame` | `conn_id, message: str` | `str` | `ProtocolError` |

### Frame Decoder

| Function | Arguments | Returns | Raises |
|---|---|---|---|
| `parse_frame` | `line: str` | `ParsedFrame \| None` | `FrameDecodingError` |
| `is_ready_frame` | `line: str` | `bool` | `FrameDecodingError` |

### Payload Helpers

| Function | Arguments | Returns | Raises |
|---|---|---|---|
| `decode_binary_payload` | `payload: str` | `bytes` | `FrameDecodingError` |
| `decode_error_payload` | `payload: str` | `str` | `FrameDecodingError` |
| `encode_host_port` | `host: str, port: int` | `str` | `ProtocolError` |
| `parse_host_port` | `payload: str` | `tuple[str, int]` | `FrameDecodingError` |

### ID Generators & Validators

| Symbol | Kind | Returns | Notes |
|---|---|---|---|
| `new_conn_id` | function | `str` | `c[0-9a-f]{24}` — TCP connection ID |
| `new_flow_id` | function | `str` | `u[0-9a-f]{24}` — UDP flow ID |
| `ID_RE` | constant | `re.Pattern[str]` | Compiled validator; shared by encoders and agent |
| `SESSION_CONN_ID` | constant | `str` | `"c" + "0"×24` — session-level error sentinel |

### SOCKS5 Enums

| Enum | Members | RFC | Notes |
|---|---|---|---|
| `AuthMethod` | `NO_AUTH`, `GSSAPI`, `USERNAME_PASSWORD`, `NO_ACCEPT` | RFC 1928 §3 | `GSSAPI` wire-only; `is_supported()` returns `False` |
| `Cmd` | `CONNECT`, `BIND`, `UDP_ASSOCIATE` | RFC 1928 §4 | `BIND` wire-only; `is_supported()` returns `False` |
| `AddrType` | `IPV4`, `DOMAIN`, `IPV6` | RFC 1928 §4 | — |
| `Reply` | `SUCCESS` … `ADDR_NOT_SUPPORTED` | RFC 1928 §6 | — |
| `UserPassStatus` | `SUCCESS`, `FAILURE` | RFC 1929 §2 | Any non-zero byte maps to `FAILURE` per RFC 1929 §2 |

---

## 13. Invariants & Constraints

These are hard invariants that all layers must respect:

```
1.  parse_frame(encode_*(args)) is never None
    — every encoded frame round-trips through the parser

2.  parse_frame(line) returns None  iff  line has no FRAME_PREFIX+FRAME_SUFFIX
    — None is exclusively "not a tunnel frame", never "corrupt tunnel frame"

3.  FrameDecodingError is raised  iff  line IS a tunnel frame but is corrupt
    — the distinction between None and FrameDecodingError is load-bearing

4.  encode_host_port / parse_host_port are strict inverses on the semantic value
    — parse_host_port(encode_host_port(h, p))[1] == p  always
    — parse_host_port(encode_host_port(h, p))[0] == normalise(h)

5.  SESSION_CONN_ID passes ID_RE but is never produced by new_conn_id()
    — callers may use `conn_id == SESSION_CONN_ID` as a reliable sentinel check

6.  All encoded frames are newline-terminated
    — transport layer may split on "\n" without any other framing

7.  All payload bytes in DATA/UDP_DATA/ERROR frames are base64url (no padding)
    — the payload field of ParsedFrame is always safe to pass to decode_binary_payload

8.  No frame field ever contains FRAME_PREFIX or FRAME_SUFFIX
    — enforced by _encode_frame; violation raises ProtocolError
```

---

## 14. Extension Points

### Adding a New Frame Type

```
1.  Add the string to _VALID_MSG_TYPES in frames.py
2.  Add a typed encode_<name>_frame() function
3.  Add the new function to __all__ in frames.py and __init__.py
4.  Update the frame catalogue docstring in frames.py
5.  Update the Frame Catalogue table in this document (§4.2)
6.  Update the session layer dispatcher to handle the new msg_type
7.  Update the agent to emit / consume the new frame type
```

### Adding a New SOCKS5 Auth Method

```
1.  Add the value to AuthMethod in enums.py
2.  Add a note if it is defined-but-not-implemented (like GSSAPI)
3.  Update the proxy layer negotiation handler
4.  Update §8.1 and §8.2 in this document
```

### Changing the ID Format

```
1.  Update _TCP_PREFIX, _UDP_PREFIX, _TOKEN_BYTES in ids.py
2.  Update ID_RE and SESSION_CONN_ID in ids.py  ← both are public constants defined there
3.  Verify SESSION_CONN_ID still cannot be produced by new_conn_id()
4.  Update §5 in this document
5.  Bump the agent version — ID format is part of the wire protocol
```

---

## 15. Security Considerations

| Threat | Mitigation |
|---|---|
| Frame injection via crafted payload | `_encode_frame` rejects payloads containing `FRAME_PREFIX` or `FRAME_SUFFIX` → `ProtocolError` |
| Frame injection via crafted hostname | `encode_host_port` rejects `:`, `<`, `>` in domain names → `ProtocolError` |
| Memory exhaustion via oversized frame | `parse_frame` raises `FrameDecodingError` for oversized tunnel frames; silently drops oversized non-frame lines → `None` |
| ID collision / prediction | `secrets.token_hex` (CSPRNG); 96-bit entropy; birthday bound ≈ 2^48 |
| IPv6 ambiguity / confusion | All IPv6 literals normalised to compressed form and bracket-quoted by `encode_host_port` |
| Corrupt base64url crashing decoder | `binascii.Error` caught and re-raised as `FrameDecodingError` with truncated hex excerpt |
| Non-UTF-8 error messages crashing decoder | `UnicodeDecodeError` caught and re-raised as `FrameDecodingError` |
| `SESSION_CONN_ID` collision with real ID | All-zero token is outside CSPRNG output space; probability ≈ \(2^{-96}\) |

