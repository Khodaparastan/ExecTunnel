# ExecTunnel Protocol Layer — Developer Documentation

```
exectunnel/protocol/  |  api-doc v2.0  |  Python 3.13+
```

--
---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Wire Format](#3-wire-format)
4. [Frame Catalogue](#4-frame-catalogue)
5. [Exception Contract](#5-exception-contract)
6. [ID Specification](#6-id-specification)
7. [SOCKS5 Enumerations](#7-socks5-enumerations)
8. [API Reference](#8-api-reference)

---

## 1. Overview

The protocol layer is the **sole authority** on how bytes are arranged on the ExecTunnel
wire. It owns:

* Frame encoding and decoding
* Host/port wire serialisation
* ID generation and format validation
* SOCKS5 wire enumerations per RFC 1928 / RFC 1929

It explicitly does **not** own:

* I/O of any kind — no sockets, file handles, or asyncio
* DNS resolution
* Session or connection lifecycle state
* Threading or concurrency primitives
* WebSocket or Kubernetes exec channel framing

Every public function is a **pure transformation**. Given the same inputs it produces
the same outputs, with no side effects. This makes the layer safe to call from any
execution context, including the in-pod agent running as a plain synchronous subprocess.

---

## 2. Architecture

### 2.1 Module Structure

```
exectunnel/protocol/
├── __init__.py    — public API surface, re-exports only
├── constants.py   — wire constants, size limits, message-type classification sets
├── enums.py       — SOCKS5 wire enumerations (RFC 1928 / RFC 1929)
├── ids.py         — ID generation, validation patterns, SESSION_CONN_ID sentinel
├── types.py       — ParsedFrame frozen dataclass
├── codecs.py      — host:port codec, base64url codec (pure, stateless)
├── encoders.py    — typed encode_*_frame() public functions
└── parser.py      — parse_frame(), is_ready_frame()
```

### 2.2 Internal Dependency Graph

```
__init__.py
    ├── enums.py          (no internal deps)
    ├── ids.py            (no internal deps)
    ├── types.py          (no internal deps)
    ├── constants.py
    │       └── ids.py    (SESSION_CONN_ID re-export)
    ├── codecs.py
    │       └── constants.py
    ├── encoders.py
    │       ├── codecs.py
    │       ├── constants.py
    │       └── ids.py    (CONN_FLOW_ID_RE)
    └── parser.py
            ├── codecs.py (_hex_preview)
            ├── constants.py
            └── ids.py    (CONN_FLOW_ID_RE)
```

`enums.py`, `ids.py`, and `types.py` are leaf nodes with zero internal dependencies. The
graph is a strict DAG with no cycles.

### 2.3 Position in the Client Stack

```
 ┌─────────────────────┐
 │   Session Layer     │  connection/flow lifecycle, SOCKS5 state machine
 ├─────────────────────┤
 │   Proxy Layer       │  SOCKS5 handshake, auth, command dispatch
 ├─────────────────────┤
 │   Transport Layer   │  WebSocket / kubectl-exec channel I/O, line buffering
 ├─────────────────────┤
 │  Protocol Layer  ◄──┼── you are here
 └─────────────────────┘
        ▲           ▲
   parse_frame()   encode_*_frame()
   (decode path)   (encode path)
```

The transport layer owns line-buffering. It delivers one complete stripped line per call
to `parse_frame`, and writes the newline-terminated string returned by each encoder
directly to the channel.

### 2.4 In-Pod Agent Usage

The in-pod agent uses the same protocol layer as the client. Because the layer is
I/O-free and has no external dependencies beyond the standard library, it can be copied
into the pod as a standalone script without the full client package.

---

## 3. Wire Format

### 3.1 Frame Structure

```
<<<EXECTUNNEL:{msg_type}:{conn_id}[:{payload}]>>>\n
└─────────────┘└────────┘└───────┘ └───────┘└──┘└┘
  FRAME_PREFIX  msg_type  conn_id   payload SUFFIX \n
```

All frames are **newline-terminated** (`\n`). The transport layer is responsible for
line-buffering. The protocol layer encodes and decodes individual complete lines only.

### 3.2 Field Rules

| Field      | Charset            | Constraints                                                                         |
|------------|--------------------|-------------------------------------------------------------------------------------|
| `msg_type` | `[A-Z_]`           | Must be a recognised member of the frame catalogue                                  |
| `conn_id`  | `[cu][0-9a-f]{24}` | Absent for `AGENT_READY`, `KEEPALIVE`, and `STATS` only                             |
| `payload`  | Varies by type     | See §3.3 — payload must never contain `FRAME_PREFIX`, `FRAME_SUFFIX`, `\n`, or `\r` |

The frame parser splits on `:` with `maxsplit=2`. This preserves colons inside
bracket-quoted IPv6 addresses within OPEN frame payloads (e.g. `[2001:db8::1]:443`) and
colons inside `STATS` JSON payloads, without any special-casing. base64url payloads
never contain `:`, so `DATA`, `UDP_DATA`, and `ERROR` frames are unaffected.

### 3.3 Payload Encoding by Frame Type

| Frame type                            | Payload format                      | Invariant                                |
|---------------------------------------|-------------------------------------|------------------------------------------|
| `CONN_OPEN`, `UDP_OPEN`               | `[host]:port` or `host:port`        | —                                        |
| `DATA`, `UDP_DATA`                    | base64url, no padding (RFC 4648 §5) | Always non-empty — encoders reject `b""` |
| `ERROR`                               | base64url-encoded UTF-8, no padding | —                                        |
| `STATS`                               | base64url-encoded JSON, no padding  | No public encoder — agent internal only  |
| `CONN_ACK`, `CONN_CLOSE`, `UDP_CLOSE` | *(none)*                            | —                                        |
| `AGENT_READY`, `KEEPALIVE`            | *(none, no conn_id either)*         | —                                        |

### 3.4 Host / Port Encoding

OPEN frame payloads follow these rules:

* **IPv6 addresses** are always bracket-quoted and normalised to compressed form:
  `[2001:db8::1]:443`
* **IPv4 addresses** are normalised to dotted-decimal: `192.168.1.1:80`
* **Domain names** are left bare: `redis.default.svc.cluster.local:6379`
* **Underscores** are permitted in any label position for Kubernetes / SRV / DMARC
  compatibility: `_dmarc.example.com`, `my_service`
* **Trailing dots** (FQDN form) are not supported — strip before calling the encoder

### 3.5 Frame Length Limit

`MAX_FRAME_LEN = 8192` characters, excluding the trailing `\n`.

The practical limit for raw bytes per `DATA` or `UDP_DATA` frame is:

\[
\text{max\_raw\_bytes} = \left\lfloor \frac{(8192 - 14 - 4 - 2 - 25 - 3) \times 3}{4}
\right\rfloor = 6108 \text{ bytes}
\]

Where the subtracted constants are: `FRAME_PREFIX` (14), `"DATA"` (4), two `:`
separators (2), `conn_id` (25), `FRAME_SUFFIX` (3).

Frames exceeding this limit are handled as follows:

| Context  | Frame is tunnel frame? | Outcome                          |
|----------|------------------------|----------------------------------|
| Encoding | —                      | `ProtocolError`                  |
| Decoding | Yes                    | `FrameDecodingError`             |
| Decoding | No                     | Logged at DEBUG, `None` returned |

### 3.6 Proxy Suffix Handling

Some HTTP reverse proxies inject additional bytes after the closing `>>>`. The parser
strips all characters after the rightmost occurrence of `FRAME_SUFFIX` before
interpreting a frame, as long as the line also starts with `FRAME_PREFIX`. Non-tunnel
lines are never modified. `is_ready_frame` applies the same stripping for consistency.

### 3.7 `READY_FRAME` and `encode_agent_ready_frame`

`READY_FRAME` is the bare comparison constant used internally by `is_ready_frame`:

```python
READY_FRAME = "<<<EXECTUNNEL:AGENT_READY>>>"  # no \n
```

The in-pod agent must call `encode_agent_ready_frame()` to obtain the newline-terminated
wire form. This is consistent with every other frame encoder in the layer and prevents
the silent framing bug that would occur if the agent wrote `READY_FRAME` directly.

---

## 4. Frame Catalogue

| Frame         | Direction      | conn_id      | Payload         | Purpose                                            |
|---------------|----------------|--------------|-----------------|----------------------------------------------------|
| `AGENT_READY` | agent → client | ✗            | ✗               | Agent bootstrap complete; listener ready           |
| `KEEPALIVE`   | client → agent | ✗            | ✗               | Heartbeat to keep the exec channel alive           |
| `CONN_OPEN`   | client → agent | ✓ `c`-prefix | `host:port`     | Request a TCP connection to destination            |
| `CONN_ACK`    | agent → client | ✓            | ✗               | TCP connection established                         |
| `CONN_CLOSE`  | bidirectional  | ✓            | ✗               | Close or half-close a TCP connection               |
| `DATA`        | bidirectional  | ✓            | base64url       | TCP data chunk — always non-empty                  |
| `UDP_OPEN`    | client → agent | ✓ `u`-prefix | `host:port`     | Open a UDP flow to destination                     |
| `UDP_DATA`    | bidirectional  | ✓            | base64url       | UDP datagram — always non-empty                    |
| `UDP_CLOSE`   | bidirectional  | ✓            | ✗               | Close a UDP flow                                   |
| `ERROR`       | bidirectional  | ✓            | base64url UTF-8 | Error report, per-connection or session-level      |
| `STATS`       | agent → client | ✗            | base64url JSON  | Session observability snapshot — no public encoder |

### conn_id Prefix Conventions

| Prefix     | Semantic meaning             | Generator                  |
|------------|------------------------------|----------------------------|
| `c`        | TCP connection ID            | `new_conn_id()`            |
| `u`        | UDP flow ID                  | `new_flow_id()`            |
| `c000...0` | Session-level error sentinel | `SESSION_CONN_ID` constant |

**Important:** The protocol layer validates that `conn_id` matches `CONN_FLOW_ID_RE` (
`[cu][0-9a-f]{24}`) but does **not** enforce which prefix belongs to which frame type.
Enforcing that `UDP_OPEN` carries a `u`-prefixed ID is a semantic concern owned by the
session layer. This is a deliberate design boundary — the wire layer is a pure codec.

**`STATS` note:** `STATS` has no public encoder. It is produced exclusively by the
in-pod agent's bench/observability module via the private `_encode_frame` path.
Client-side code must never construct a `STATS` frame. `parse_frame` correctly parses
`STATS` frames received from the agent.

---

## 5. Exception Contract

The protocol layer raises exactly two exception types. There are no other exceptions
that cross its boundary.

### 5.1 `ProtocolError`

Raised exclusively by **encoders** when the caller provides arguments that would produce
an invalid, unsafe, or unsendable frame.

**This is always a bug in the calling layer.** Never catch silently.

Conditions that trigger `ProtocolError`:

* Invalid `conn_id` format passed to a frame encoder
* `conn_id` provided for a frame type that must not carry one (`AGENT_READY`,
  `KEEPALIVE`)
* Port out of `[1, 65535]`
* Empty or structurally invalid hostname
* Hostname containing frame-unsafe characters (`:`, `<`, `>`)
* Hostname containing consecutive dots (`..`)
* Hostname that fails the structural domain check
* Empty `bytes` passed to `encode_data_frame` or `encode_udp_data_frame` — use
  `encode_conn_close_frame` or `encode_udp_close_frame` to signal end of stream
* Payload that would contain `FRAME_PREFIX`, `FRAME_SUFFIX`, a newline (`\n`), or a
  carriage return (`\r`)
* Encoded frame length exceeds `MAX_FRAME_LEN`

**Caller action:** surface to the owning task. Terminate the connection or flow. Do not
retry — the same arguments will produce the same error.

### 5.2 `FrameDecodingError`

Raised exclusively by **decoders** (`parse_frame`, `parse_host_port`,
`decode_binary_payload`, `decode_error_payload`) when a confirmed tunnel frame carries a
payload that cannot be decoded.

This indicates wire corruption, a protocol version mismatch, or an adversarial peer.

Conditions that trigger `FrameDecodingError`:

* Tunnel frame exceeds `MAX_FRAME_LEN`
* Unrecognised `msg_type`
* `AGENT_READY` or `KEEPALIVE` frame carries unexpected extra fields
* `STATS` frame carries no payload
* Missing `conn_id` on a frame type that requires one
* `conn_id` does not match `CONN_FLOW_ID_RE`
* Required payload absent (`CONN_OPEN`, `DATA`, `UDP_OPEN`, `UDP_DATA`, `ERROR`)
* Forbidden payload present (`CONN_ACK`, `CONN_CLOSE`, `UDP_CLOSE`)
* Non-base64url characters in a `DATA`, `UDP_DATA`, `ERROR`, or `STATS` payload
* Malformed `host:port` payload in a `CONN_OPEN` or `UDP_OPEN` frame
* `ERROR` frame payload decodes to invalid UTF-8

**Note on base64url decoding:** Python's `urlsafe_b64decode` silently discards
characters outside the URL-safe alphabet rather than raising an exception.
`decode_binary_payload` performs an explicit alphabet pre-check against
`[A-Za-z0-9_-]` (RFC 4648 §5) before calling the decoder. Any out-of-alphabet character
raises `FrameDecodingError` immediately rather than producing silently corrupt output.

**Caller action:** always propagate. Never discard. Treat as a fatal condition for the
session.

### 5.3 `None` from `parse_frame`

`parse_frame` returns `None` — and **never raises** — for lines that are not tunnel
frames: shell noise, container log output, blank lines, or oversized non-frame lines.
Callers must handle `None` as a routine condition, not an error.

---

## 6. ID Specification

### 6.1 Format

```
<prefix><24 lowercase hex chars>

TCP connection ID : c<24 hex>   →  ca1b2c3d4e5f6a7b8c9d0e1f23
UDP flow ID       : u<24 hex>   →  ua1b2c3d4e5f6a7b8c9d0e1f23
Session ID        : s<24 hex>   →  s3f7a1c9e2b4d6f8a0c2e4b623
```

### 6.2 Entropy

12 bytes of CSPRNG output (`secrets.token_hex`) produce 24 lowercase hex characters and
96 bits of entropy.

\[
\text{birthday bound} \approx 2^{48} \text{ IDs before 50\% collision probability}
\]

Safe for high-concurrency, long-lived tunnel sessions without coordination.

### 6.3 Validation Patterns

| Constant          | Pattern                    | Matches                                            |
|-------------------|----------------------------|----------------------------------------------------|
| `CONN_FLOW_ID_RE` | `[cu][0-9a-f]{24}`         | TCP connection IDs and UDP flow IDs                |
| `SESSION_ID_RE`   | `s[0-9a-f]{24}`            | Session IDs                                        |
| `ID_RE`           | alias of `CONN_FLOW_ID_RE` | **Deprecated** — use `CONN_FLOW_ID_RE` in new code |

All patterns are compiled with `re.ASCII` to prevent Unicode digit classes from matching
on non-standard Python builds.

### 6.4 `SESSION_CONN_ID` Sentinel

```python
SESSION_CONN_ID = "c" + "0" * 24
# → "c000000000000000000000000"
```

A valid `CONN_FLOW_ID_RE`-shaped value that is **deliberately outside the random space
** — `secrets.token_hex` draws from a CSPRNG and will never produce the all-zero string
in practice.

Used as the `conn_id` in `ERROR` frames that report session-level failures rather than
per-connection failures.

Callers that need to distinguish between the two must compare explicitly:

```python
frame = parse_frame(line)
if frame.msg_type == "ERROR":
    if frame.conn_id == SESSION_CONN_ID:
    # fatal session-level error — tear down everything
    else:
# per-connection error for conn_id = frame.conn_id
```

`SESSION_CONN_ID` passes `CONN_FLOW_ID_RE` because it uses the `c` prefix. It does **not
** match `SESSION_ID_RE` (which requires the `s` prefix).

---

## 7. SOCKS5 Enumerations

### 7.1 Strict vs Permissive Base Classes

All enums except `UserPassStatus` inherit from `_StrictIntEnum`, which raises
`ValueError` immediately on any unknown wire value. This surfaces protocol violations at
the earliest possible point — the moment the byte is read off the wire.

`UserPassStatus` inherits plain `IntEnum` with a permissive `_missing_` because RFC 1929
§2 explicitly requires that **any** non-zero status byte be treated as failure rather
than rejected as unknown.

The proxy layer must catch `ValueError` from strict enum construction and map it to the
appropriate `Reply` code.

### 7.2 `AuthMethod` (RFC 1928 §3)

| Member              | Value  | `is_supported()` | Notes                                               |
|---------------------|--------|------------------|-----------------------------------------------------|
| `NO_AUTH`           | `0x00` | `True`           | Only negotiated method in this tunnel               |
| `GSSAPI`            | `0x01` | `False`          | Wire-defined; not implemented                       |
| `USERNAME_PASSWORD` | `0x02` | `False`          | Wire-defined; not implemented                       |
| `NO_ACCEPT`         | `0xFF` | `False`          | Server rejection sentinel — not a negotiable method |

`is_supported()` returns `True` only for `NO_AUTH`. `NO_ACCEPT` is included in the
unsupported set because it is a **server-side rejection response**, not a method that
can be proposed by a client. Peers advertising only unsupported methods receive
`NO_ACCEPT` in reply.

Unknown wire values raise `ValueError` immediately.

### 7.3 `Cmd` (RFC 1928 §4)

| Member          | Value  | `is_supported()` | Notes                                         |
|-----------------|--------|------------------|-----------------------------------------------|
| `CONNECT`       | `0x01` | `True`           | TCP proxy                                     |
| `BIND`          | `0x02` | `False`          | Not implemented — replies `CMD_NOT_SUPPORTED` |
| `UDP_ASSOCIATE` | `0x03` | `True`           | UDP proxy                                     |

Unknown wire values raise `ValueError` immediately.

### 7.4 `AddrType` (RFC 1928 §4)

| Member   | Value  |
|----------|--------|
| `IPV4`   | `0x01` |
| `DOMAIN` | `0x03` |
| `IPV6`   | `0x04` |

Unknown wire values raise `ValueError` immediately.

### 7.5 `Reply` (RFC 1928 §6)

| Member               | Value  | Meaning                           |
|----------------------|--------|-----------------------------------|
| `SUCCESS`            | `0x00` | Request granted                   |
| `GENERAL_FAILURE`    | `0x01` | General SOCKS server failure      |
| `NOT_ALLOWED`        | `0x02` | Connection not allowed by ruleset |
| `NET_UNREACHABLE`    | `0x03` | Network unreachable               |
| `HOST_UNREACHABLE`   | `0x04` | Host unreachable                  |
| `REFUSED`            | `0x05` | Connection refused                |
| `TTL_EXPIRED`        | `0x06` | TTL expired                       |
| `CMD_NOT_SUPPORTED`  | `0x07` | Command not supported             |
| `ADDR_NOT_SUPPORTED` | `0x08` | Address type not supported        |

Unknown wire values raise `ValueError` immediately.

### 7.6 `UserPassStatus` (RFC 1929 §2)

| Member    | Value  | Condition                |
|-----------|--------|--------------------------|
| `SUCCESS` | `0x00` | Authentication succeeded |
| `FAILURE` | `0xFF` | Authentication failed    |

Any byte in `[0x01, 0xFE]` maps to `FAILURE`. Values outside `[0x00, 0xFF]` raise
`ValueError`. This is the only enum in the protocol layer with a permissive `_missing_`.

---

## 8. API Reference

### 8.1 Constants

| Symbol                | Type           | Value                            | Description                                                 |
|-----------------------|----------------|----------------------------------|-------------------------------------------------------------|
| `FRAME_PREFIX`        | `str`          | `"<<<EXECTUNNEL:"`               | Tunnel frame opening delimiter                              |
| `FRAME_SUFFIX`        | `str`          | `">>>"`                          | Tunnel frame closing delimiter                              |
| `READY_FRAME`         | `str`          | `"<<<EXECTUNNEL:AGENT_READY>>>"` | Bare comparison constant — no `\n`                          |
| `MAX_FRAME_LEN`       | `int`          | `8192`                           | Maximum frame length in chars, excluding `\n`               |
| `MIN_TCP_UDP_PORT`    | `int`          | `1`                              | Inclusive lower bound of valid port range                   |
| `MAX_TCP_UDP_PORT`    | `int`          | `65535`                          | Inclusive upper bound of valid port range                   |
| `PORT_UNSPECIFIED`    | `int`          | `0`                              | Sentinel for an absent or unspecified port — proxy use only |
| `PAYLOAD_PREVIEW_LEN` | `int`          | `64`                             | Max chars of a bad payload included in error telemetry      |
| `SESSION_CONN_ID`     | `str`          | `"c" + "0"*24`                   | Session-level error sentinel conn_id                        |
| `CONN_FLOW_ID_RE`     | `Pattern[str]` | `[cu][0-9a-f]{24}`               | Validates TCP connection IDs and UDP flow IDs               |
| `SESSION_ID_RE`       | `Pattern[str]` | `s[0-9a-f]{24}`                  | Validates session IDs                                       |
| `ID_RE`               | `Pattern[str]` | alias of `CONN_FLOW_ID_RE`       | **Deprecated** — use `CONN_FLOW_ID_RE`                      |

### 8.2 Message-Type Classification Sets

These frozensets are exported from `exectunnel.protocol.constants` and drive both the
encoder validation pipeline and the parser dispatch logic.

| Constant                        | Members                                              | Role                                  |
|---------------------------------|------------------------------------------------------|---------------------------------------|
| `VALID_MSG_TYPES`               | All 11 frame types                                   | Closed set of recognised type strings |
| `NO_CONN_ID_TYPES`              | `AGENT_READY`, `KEEPALIVE`                           | No conn_id, no payload                |
| `NO_CONN_ID_WITH_PAYLOAD_TYPES` | `STATS`                                              | Payload present, conn_id absent       |
| `PAYLOAD_REQUIRED_TYPES`        | `CONN_OPEN`, `DATA`, `UDP_OPEN`, `UDP_DATA`, `ERROR` | Payload must be non-empty             |
| `PAYLOAD_FORBIDDEN_TYPES`       | `CONN_ACK`, `CONN_CLOSE`, `UDP_CLOSE`                | Payload must be absent                |

### 8.3 Frame Encoders

All encoders return a **newline-terminated** `str`. All raise `ProtocolError` on invalid
input unless noted.

| Function                   | Signature                                     | Raises          | Sent by |
|----------------------------|-----------------------------------------------|-----------------|---------|
| `encode_agent_ready_frame` | `() -> str`                                   | never           | agent   |
| `encode_keepalive_frame`   | `() -> str`                                   | never           | client  |
| `encode_conn_open_frame`   | `(conn_id: str, host: str, port: int) -> str` | `ProtocolError` | client  |
| `encode_conn_ack_frame`    | `(conn_id: str) -> str`                       | `ProtocolError` | agent   |
| `encode_conn_close_frame`  | `(conn_id: str) -> str`                       | `ProtocolError` | both    |
| `encode_data_frame`        | `(conn_id: str, data: bytes) -> str`          | `ProtocolError` | both    |
| `encode_udp_open_frame`    | `(flow_id: str, host: str, port: int) -> str` | `ProtocolError` | client  |
| `encode_udp_data_frame`    | `(flow_id: str, data: bytes) -> str`          | `ProtocolError` | both    |
| `encode_udp_close_frame`   | `(flow_id: str) -> str`                       | `ProtocolError` | both    |
| `encode_error_frame`       | `(conn_id: str, message: str) -> str`         | `ProtocolError` | both    |

**`encode_data_frame` and `encode_udp_data_frame`** raise `ProtocolError` when
`data = b""`. To signal end of stream, use `encode_conn_close_frame` or
`encode_udp_close_frame` respectively.

**`encode_keepalive_frame` and `encode_agent_ready_frame`** return pre-computed
module-level constants. They are safe to call at high frequency without allocation cost
and never raise.

### 8.4 Frame Decoder

| Function         | Signature                            | Returns                 | Raises               |
|------------------|--------------------------------------|-------------------------|----------------------|
| `parse_frame`    | `(line: str) -> ParsedFrame \| None` | `ParsedFrame` or `None` | `FrameDecodingError` |
| `is_ready_frame` | `(line: str) -> bool`                | `bool`                  | never                |

**`parse_frame` return semantics:**

| Return value  | Meaning                                                                |
|---------------|------------------------------------------------------------------------|
| `ParsedFrame` | Structurally valid, recognised tunnel frame                            |
| `None`        | Not a tunnel frame — shell noise, blank line, oversized non-frame line |
| *(raises)*    | Confirmed tunnel frame with corrupt or invalid structure               |

**`ParsedFrame` fields:**

| Field      | Type          | Description                                                               |
|------------|---------------|---------------------------------------------------------------------------|
| `msg_type` | `str`         | Frame type string from the catalogue                                      |
| `conn_id`  | `str \| None` | Connection or flow ID; `None` for `AGENT_READY`, `KEEPALIVE`, and `STATS` |
| `payload`  | `str`         | Raw payload string; `""` when absent                                      |

`ParsedFrame` is a frozen dataclass (`frozen=True, slots=True`). Instances are immutable
and hashable.

### 8.5 Payload Helpers

| Function                | Signature                           | Returns             | Raises               |
|-------------------------|-------------------------------------|---------------------|----------------------|
| `encode_host_port`      | `(host: str, port: int) -> str`     | Wire payload string | `ProtocolError`      |
| `parse_host_port`       | `(payload: str) -> tuple[str, int]` | `(host, port)`      | `FrameDecodingError` |
| `decode_binary_payload` | `(payload: str) -> bytes`           | Raw bytes           | `FrameDecodingError` |
| `decode_error_payload`  | `(payload: str) -> str`             | UTF-8 string        | `FrameDecodingError` |

`encode_host_port` and `parse_host_port` are strict inverses on the semantic value.
`decode_error_payload` is a convenience wrapper around `decode_binary_payload` that
additionally decodes the result as UTF-8.

### 8.6 ID Generators

| Function         | Signature   | Returns     |
|------------------|-------------|-------------|
| `new_conn_id`    | `() -> str` | `c<24 hex>` |
| `new_flow_id`    | `() -> str` | `u<24 hex>` |
| `new_session_id` | `() -> str` | `s<24 hex>` |

All generators use `secrets.token_hex(12)` and include development-time assertions (
stripped by `python -O`) that the output matches the expected pattern.

