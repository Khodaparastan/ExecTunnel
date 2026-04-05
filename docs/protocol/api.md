# ExecTunnel — Protocol Package API Reference

```
exectunnel/protocol/  |  api-doc v1.0  |  Python 3.13+
audience: developers building transport / proxy / session / agent layers
```

---

## How to Read This Document

This reference is written for developers who **consume** the protocol package from an upper layer. It answers three questions for every symbol:

1. **What does it do** — precise contract, not implementation detail
2. **What can go wrong** — every exception, when it fires, what `details` carries
3. **How to use it correctly** — copy-paste patterns, common mistakes, gotchas

Import everything from the package root:

```python
from exectunnel.protocol import (
    # constants
    FRAME_PREFIX, FRAME_SUFFIX, READY_FRAME, SESSION_CONN_ID, MAX_FRAME_LEN,
    # frame result type
    ParsedFrame,
    # encoders
    encode_conn_open_frame, encode_conn_close_frame,
    encode_data_frame, encode_error_frame,
    encode_udp_open_frame, encode_udp_data_frame, encode_udp_close_frame,
    # decoder
    parse_frame, is_ready_frame,
    # payload helpers
    decode_binary_payload, decode_error_payload,
    encode_host_port, parse_host_port,
    # id generators
    new_conn_id, new_flow_id, ID_RE,
    # socks5 enums
    AddrType, AuthMethod, Cmd, Reply, UserPassStatus,
)
```

Never import from sub-modules directly — the sub-module layout is an implementation detail and may change.

---

## Quick-Reference Card

```
GENERATE IDs ──────────────────────────────────────────────────────────────────
  new_conn_id()                     →  "c<24 hex>"   TCP connection ID
  new_flow_id()                     →  "u<24 hex>"   UDP flow ID
  ID_RE                             →  compiled re   validate an ID string

ENCODE FRAMES ─────────────────────────────────────────────────────────────────
  encode_conn_open_frame(id, h, p)  →  frame str     open TCP connection
  encode_conn_close_frame(id)       →  frame str     close TCP connection
  encode_data_frame(id, bytes)      →  frame str     TCP data chunk
  encode_udp_open_frame(id, h, p)   →  frame str     open UDP flow
  encode_udp_data_frame(id, bytes)  →  frame str     UDP datagram
  encode_udp_close_frame(id)        →  frame str     close UDP flow
  encode_error_frame(id, msg)       →  frame str     error report

DECODE FRAMES ─────────────────────────────────────────────────────────────────
  parse_frame(line)                 →  ParsedFrame | None
  is_ready_frame(line)              →  bool

DECODE PAYLOADS ───────────────────────────────────────────────────────────────
  decode_binary_payload(payload)    →  bytes         DATA / UDP_DATA frames
  decode_error_payload(payload)     →  str           ERROR frames
  parse_host_port(payload)          →  (host, port)  OPEN frames
  encode_host_port(host, port)      →  str           build OPEN payload manually

SOCKS5 ENUMS ──────────────────────────────────────────────────────────────────
  AuthMethod   Cmd   AddrType   Reply   UserPassStatus

EXCEPTIONS RAISED ─────────────────────────────────────────────────────────────
  ProtocolError        encoder received bad arguments (your bug)
  FrameDecodingError   decoder received corrupt wire data (peer's bug)
```

---

## Section 1 — ID Generators

### `new_conn_id() → str`

Generates a cryptographically random TCP connection ID.

**Returns:** `"c"` followed by 24 lowercase hex characters.
Example: `"ca1b2c3d4e5f6a7b8c9d0e1f2a3b"`

**Raises:** Nothing.

**When to call it:** Once per accepted SOCKS5 `CONNECT` request, before calling `encode_conn_open_frame`. Store the returned ID as the key in your connection table for the lifetime of that TCP connection.

```python
conn_id = new_conn_id()
frame   = encode_conn_open_frame(conn_id, "redis.default.svc", 6379)
await ws.send(frame)
connections[conn_id] = writer   # store for DATA / CONN_CLOSE dispatch
```

---

### `new_flow_id() → str`

Generates a cryptographically random UDP flow ID.

**Returns:** `"u"` followed by 24 lowercase hex characters.
Example: `"ua1b2c3d4e5f6a7b8c9d0e1f2a3b"`

**Raises:** Nothing.

**When to call it:** Once per accepted SOCKS5 `UDP_ASSOCIATE` request, before calling `encode_udp_open_frame`.

```python
flow_id = new_flow_id()
frame   = encode_udp_open_frame(flow_id, "8.8.8.8", 53)
await ws.send(frame)
udp_flows[flow_id] = remote_addr
```

---

### ID Format Contract

Both functions share the same format guarantee:

```
[cu][0-9a-f]{24}
│    └─────────── 24 lowercase hex chars (96-bit CSPRNG token)
└─────────────── "c" for TCP, "u" for UDP
```

**Never construct IDs manually.** The format is validated by every encoder — a hand-crafted string that fails `ID_RE` will raise `ProtocolError` immediately.

**`SESSION_CONN_ID` is not a real ID.** It is a reserved sentinel (`"c" + "0" * 24`) used exclusively in `encode_error_frame` for session-level errors. Do not store it in your connection table.

```python
# ✓ correct — session-level error
frame = encode_error_frame(SESSION_CONN_ID, "WebSocket closed unexpectedly")

# ✗ wrong — SESSION_CONN_ID is not a connection
connections[SESSION_CONN_ID] = writer
```

---

## Section 2 — Frame Encoders

All encoders share the same contract:

* **Return:** a newline-terminated ASCII string ready to write to the WebSocket channel.
* **Raise:** `ProtocolError` if any argument is invalid. This always indicates a bug in the calling layer, never a remote peer fault.
* **Thread / async safety:** all encoders are pure functions with no shared state — safe to call from any thread or coroutine concurrently.

---

### `encode_conn_open_frame(conn_id: str, host: str, port: int) → str`

Encodes a `CONN_OPEN` frame instructing the agent to open a TCP connection.

**Arguments:**

| Argument | Type | Constraints |
|---|---|---|
| `conn_id` | `str` | Must match `[cu][0-9a-f]{24}` — use `new_conn_id()` |
| `host` | `str` | Non-empty hostname, IPv4, or IPv6 literal |
| `port` | `int` | `1 ≤ port ≤ 65535` |

**Raises:** `ProtocolError` — bad `conn_id`, empty host, unsafe host chars, port out of range, or encoded frame exceeds `MAX_FRAME_LEN`.

```python
# IPv4
frame = encode_conn_open_frame(new_conn_id(), "10.0.0.5", 5432)

# Domain (Kubernetes service)
frame = encode_conn_open_frame(new_conn_id(), "postgres.default.svc", 5432)

# IPv6 — bracket-quoting is handled automatically
frame = encode_conn_open_frame(new_conn_id(), "2001:db8::1", 443)
# wire: <<<EXECTUNNEL:CONN_OPEN:c...:[ 2001:db8::1]:443>>>
```

---

### `encode_conn_close_frame(conn_id: str) → str`

Encodes a `CONN_CLOSE` frame signalling explicit TCP teardown.

**Arguments:**

| Argument | Type | Constraints |
|---|---|---|
| `conn_id` | `str` | Must match `[cu][0-9a-f]{24}` |

**Raises:** `ProtocolError` — bad `conn_id`.

**Important:** Always emit this frame when closing a connection. The agent releases its socket and connection-table entry only on receiving `CONN_CLOSE`. Omitting it causes resource leaks on the agent side that only resolve on timeout.

```python
# On SOCKS5 client disconnect:
frame = encode_conn_close_frame(conn_id)
await ws.send(frame)
del connections[conn_id]
```

---

### `encode_data_frame(conn_id: str, data: bytes) → str`

Encodes a `DATA` frame carrying a TCP data chunk.

**Arguments:**

| Argument | Type | Constraints |
|---|---|---|
| `conn_id` | `str` | Must match `[cu][0-9a-f]{24}` |
| `data` | `bytes` | Non-empty recommended; empty bytes produces a valid but useless frame |

**Raises:** `ProtocolError` — bad `conn_id`, or base64url-encoded payload causes frame to exceed `MAX_FRAME_LEN`.

**Maximum safe `data` size:** 6,108 bytes per frame. Larger chunks must be split by the caller before encoding.

```python
CHUNK = 4096   # kept in transport/session layer config, not here

while chunk := reader.read(CHUNK):
    frame = encode_data_frame(conn_id, chunk)
    await ws.send(frame)
```

> **Why 6,108 bytes?** Base64url expands 3 bytes → 4 chars. The frame envelope consumes 48 chars (prefix + msg_type + separators + conn_id + suffix). Available payload chars: `8192 − 48 = 8144`. Max raw bytes: `⌊8144 × 3/4⌋ = 6,108`.

---

### `encode_udp_open_frame(flow_id: str, host: str, port: int) → str`

Encodes a `UDP_OPEN` frame instructing the agent to open a UDP flow.

Same argument constraints and error behaviour as `encode_conn_open_frame`. Use `new_flow_id()` (not `new_conn_id()`) for the first argument.

```python
flow_id = new_flow_id()
frame   = encode_udp_open_frame(flow_id, "8.8.8.8", 53)
await ws.send(frame)
```

---

### `encode_udp_data_frame(flow_id: str, data: bytes) → str`

Encodes a `UDP_DATA` frame carrying a UDP datagram.

Same argument constraints and error behaviour as `encode_data_frame`. The `data` argument should be a **complete datagram** — UDP framing is the caller's responsibility.

```python
datagram = build_dns_query("example.com")
frame    = encode_udp_data_frame(flow_id, datagram)
await ws.send(frame)
```

---

### `encode_udp_close_frame(flow_id: str) → str`

Encodes a `UDP_CLOSE` frame signalling explicit UDP flow teardown.

Same argument constraints and error behaviour as `encode_conn_close_frame`.

```python
frame = encode_udp_close_frame(flow_id)
await ws.send(frame)
del udp_flows[flow_id]
```

---

### `encode_error_frame(conn_id: str, message: str) → str`

Encodes an `ERROR` frame carrying a human-readable error message.

**Arguments:**

| Argument | Type | Constraints |
|---|---|---|
| `conn_id` | `str` | Must match `[cu][0-9a-f]{24}`, or use `SESSION_CONN_ID` for session-level errors |
| `message` | `str` | Any UTF-8 string; newlines and non-ASCII are safe |

**Raises:** `ProtocolError` — bad `conn_id`, or encoded frame exceeds `MAX_FRAME_LEN`.

```python
# Per-connection error
frame = encode_error_frame(conn_id, "Connection refused by target")
await ws.send(frame)

# Session-level error (affects all connections)
frame = encode_error_frame(SESSION_CONN_ID, "Agent OOM — shutting down")
await ws.send(frame)
```

---

### Common Encoder Mistakes

```python
# ✗ wrong — hand-crafted ID bypasses validation until the encoder catches it
frame = encode_data_frame("myconn", data)
# → ProtocolError: Invalid tunnel id 'myconn': must match [cu][0-9a-f]{24}

# ✗ wrong — port 0 is rejected
frame = encode_conn_open_frame(conn_id, "redis", 0)
# → ProtocolError: Port 0 is out of range [1, 65535]

# ✗ wrong — colon in hostname would corrupt the frame
frame = encode_conn_open_frame(conn_id, "bad:host", 80)
# → ProtocolError: Host 'bad:host' contains frame-unsafe characters

# ✓ correct — IPv6 passed as a plain string, bracket-quoting is automatic
frame = encode_conn_open_frame(conn_id, "::1", 8080)
# wire payload: [::1]:8080
```

---

## Section 3 — Frame Decoder

### `parse_frame(line: str) → ParsedFrame | None`

Parses one line of text from the tunnel channel into a structured frame.

**Argument:** `line` — a single line from the WebSocket channel, with or without a trailing newline. Leading/trailing whitespace is stripped before parsing.

**Returns:**

| Return value | Meaning | Action |
|---|---|---|
| `None` | Line is not a tunnel frame (shell noise, blank line, bootstrap stdout) | Ignore silently |
| `ParsedFrame` | Structurally valid, recognised tunnel frame | Dispatch on `msg_type` |

**Raises:** `FrameDecodingError` — the line **is** a tunnel frame (has `FRAME_PREFIX` + `FRAME_SUFFIX`) but its internal structure is corrupt. This is a protocol violation from the remote peer. Do **not** catch and ignore — propagate upward.

```python
# Canonical inbound dispatch loop (transport / session layer)
async for message in websocket:
    try:
        frame = parse_frame(message)
    except FrameDecodingError as exc:
        log.error("corrupt tunnel frame", extra={"error": exc.to_dict()})
        raise ConnectionClosedError(
            "Remote peer sent a corrupt tunnel frame",
            details={"close_code": 1002, "close_reason": exc.message},
        ) from exc

    if frame is None:
        continue   # shell noise — normal during bootstrap

    match frame.msg_type:
        case "DATA":
            data = decode_binary_payload(frame.payload)
            await connections[frame.conn_id].write(data)
        case "CONN_CLOSE":
            await _close_connection(frame.conn_id)
        case "AGENT_READY":
            session.mark_ready()
        case "ERROR":
            msg = decode_error_payload(frame.payload)
            await _handle_agent_error(frame.conn_id, msg)
        case "UDP_DATA":
            data = decode_binary_payload(frame.payload)
            await udp_flows[frame.flow_id].sendto(data)
        case "UDP_CLOSE":
            await _close_udp_flow(frame.flow_id)
        case _:
            # Should never happen — parse_frame already validated msg_type.
            # Raise rather than silently drop.
            raise UnexpectedFrameError(
                f"Unhandled msg_type {frame.msg_type!r} in dispatch",
                details={"state": session.state, "frame_type": frame.msg_type},
            )
```

---

### `is_ready_frame(line: str) → bool`

Returns `True` if `line` is the `AGENT_READY` bootstrap sentinel.

**Argument:** `line` — a single line from the WebSocket channel.

**Returns:** `True` only for `<<<EXECTUNNEL:AGENT_READY>>>` (after whitespace stripping).

**Raises:** `FrameDecodingError` — propagated from `parse_frame` if the line has the tunnel prefix/suffix but is structurally corrupt.

**When to use:** In the bootstrap loop, before the session is established. Once `is_ready_frame` returns `True`, switch to `parse_frame` for all subsequent lines.

```python
# Bootstrap loop (session / transport layer)
async with asyncio.timeout(AGENT_READY_TIMEOUT):
    async for line in websocket:
        try:
            if is_ready_frame(line):
                break
        except FrameDecodingError as exc:
            raise BootstrapError(
                "Agent sent a corrupt frame during bootstrap",
                details={"host": pod_host, "elapsed_s": elapsed()},
            ) from exc
        # Non-tunnel lines during bootstrap are normal
        # (agent startup output, shell prompts, etc.) — log at DEBUG only
        log.debug("bootstrap noise: %r", line)
else:
    raise AgentReadyTimeoutError(
        "Agent did not send AGENT_READY within timeout",
        details={"timeout_s": AGENT_READY_TIMEOUT, "host": pod_host},
    )
```

---

### `ParsedFrame`

The structured result of `parse_frame`.

```python
@dataclass(frozen=True, slots=True)
class ParsedFrame:
    msg_type: str        # one of the 8 valid frame types
    conn_id:  str | None # "[cu][0-9a-f]{24}", or None for AGENT_READY
    payload:  str        # raw payload string or "" when absent
```

**Immutable** — `frozen=True` prevents accidental mutation after parsing.

**`payload` is always raw** — it is never decoded by `parse_frame`. Call the appropriate payload helper based on `msg_type`:

| `msg_type` | `conn_id` present | Payload helper |
|---|---|---|
| `AGENT_READY` | No (`conn_id` is `None`) | — (no payload) |
| `CONN_OPEN` | Yes | `parse_host_port(frame.payload)` |
| `CONN_CLOSE` | Yes | — (no payload) |
| `DATA` | Yes | `decode_binary_payload(frame.payload)` |
| `UDP_OPEN` | Yes | `parse_host_port(frame.payload)` |
| `UDP_DATA` | Yes | `decode_binary_payload(frame.payload)` |
| `UDP_CLOSE` | Yes | — (no payload) |
| `ERROR` | Yes | `decode_error_payload(frame.payload)` |

---

## Section 4 — Payload Helpers

### `decode_binary_payload(payload: str) → bytes`

Decodes the base64url (no-padding) payload of a `DATA` or `UDP_DATA` frame.

**Argument:** `payload` — the `ParsedFrame.payload` string from a `DATA` or `UDP_DATA` frame.

**Returns:** Raw decoded bytes.

**Raises:** `FrameDecodingError` — `payload` is not valid base64url.

```python
details = {
    "raw_bytes": str,   # hex-encoded excerpt of the bad payload (≤ 128 chars)
    "codec":     str,   # always "base64url"
}
```

```python
case "DATA":
    try:
        data = decode_binary_payload(frame.payload)
    except FrameDecodingError as exc:
        log.error("bad DATA payload", extra={"error": exc.to_dict()})
        # Close the affected connection — data integrity is compromised
        await _close_connection(frame.conn_id)
        return
    await connections[frame.conn_id].write(data)
```

---

### `decode_error_payload(payload: str) → str`

Decodes the base64url UTF-8 payload of an `ERROR` frame into a human-readable string.

**Argument:** `payload` — the `ParsedFrame.payload` string from an `ERROR` frame.

**Returns:** Decoded UTF-8 error message string.

**Raises:** `FrameDecodingError` — `payload` is not valid base64url, or the decoded bytes are not valid UTF-8.

```python
details = {
    "raw_bytes": str,   # hex-encoded excerpt (≤ 128 chars)
    "codec":     str,   # "base64url" (first stage) or "utf-8" (second stage)
}
```

```python
case "ERROR":
    try:
        msg = decode_error_payload(frame.payload)
    except FrameDecodingError as exc:
        # Even if we can't decode the message, we know an error occurred
        msg = f"<undecodable error payload: {exc.details.get('raw_bytes', '?')}>"
    await _handle_agent_error(frame.conn_id, msg)
```

---

### `encode_host_port(host: str, port: int) → str`

Encodes a host + port into the canonical wire payload string for `CONN_OPEN` / `UDP_OPEN` frames.

**You rarely need this directly.** `encode_conn_open_frame` and `encode_udp_open_frame` call it internally. Use it only when building a custom OPEN payload outside the standard encoders.

**Returns:**
* IPv6: `"[2001:db8::1]:8080"` (bracket-quoted, compressed)
* IPv4: `"192.168.1.1:8080"`
* Domain: `"redis.default.svc:6379"`

**Raises:** `ProtocolError` — empty host, port out of range, frame-unsafe characters in host, or invalid hostname structure.

---

### `parse_host_port(payload: str) → tuple[str, int]`

Parses a `[host]:port` or `host:port` payload string from a `CONN_OPEN` or `UDP_OPEN` frame.

**Argument:** `payload` — the `ParsedFrame.payload` string from a `CONN_OPEN` or `UDP_OPEN` frame.

**Returns:** `(host, port)` tuple where `host` is a plain string (no brackets) and `port` is an integer.

**Raises:** `FrameDecodingError` — malformed payload, empty host, non-numeric port, or port out of range.

```python
details = {
    "raw_bytes": str,   # hex-encoded excerpt (≤ 128 chars)
    "codec":     str,   # always "host:port"
}
```

```python
# Agent side — handling an incoming CONN_OPEN
case "CONN_OPEN":
    try:
        host, port = parse_host_port(frame.payload)
    except FrameDecodingError as exc:
        error_frame = encode_error_frame(
            frame.conn_id,
            f"Malformed CONN_OPEN payload: {exc.message}",
        )
        sys.stdout.write(error_frame)
        sys.stdout.flush()
        return
    sock = socket.create_connection((host, port))
```

---

### Round-Trip Guarantee

```python
host_in  = "2001:db8::1"
port_in  = 8080
payload  = encode_host_port(host_in, port_in)   # "[2001:db8::1]:8080"
host_out, port_out = parse_host_port(payload)

assert port_out == port_in                       # always True
assert host_out == "2001:db8::1"                 # normalised, no brackets
# host_out may differ from host_in for IPv6 (compressed form)
```

---

## Section 5 — SOCKS5 Enumerations

All enums are `IntEnum` — they compare equal to their integer wire values and can be used directly in `struct.pack` / byte comparisons.

### `AuthMethod`

RFC 1928 §3 — authentication method negotiation.

| Member | Value | Supported |
|---|---|---|
| `NO_AUTH` | `0x00` | ✓ |
| `GSSAPI` | `0x01` | ✗ wire-only |
| `USERNAME_PASSWORD` | `0x02` | ✓ |
| `NO_ACCEPT` | `0xFF` | ✓ (sent to reject) |

Use `is_supported()` to programmatically check whether a method is implemented before attempting negotiation:

```python
# Reject any method the tunnel does not implement
for method in client_methods:
    if not method.is_supported():
        writer.write(bytes([0x05, AuthMethod.NO_ACCEPT]))
        return
```

```python
# Proxy layer — method selection
client_methods = {AuthMethod(b) for b in offered_bytes}

if AuthMethod.USERNAME_PASSWORD in client_methods:
    chosen = AuthMethod.USERNAME_PASSWORD
elif AuthMethod.NO_AUTH in client_methods:
    chosen = AuthMethod.NO_AUTH
else:
    chosen = AuthMethod.NO_ACCEPT

writer.write(bytes([0x05, chosen]))
```

---

### `Cmd`

RFC 1928 §4 — client command codes.

| Member | Value | Supported |
|---|---|---|
| `CONNECT` | `0x01` | ✓ |
| `BIND` | `0x02` | ✗ wire-only |
| `UDP_ASSOCIATE` | `0x03` | ✓ |

Use `is_supported()` to guard dispatch before the `match` statement:

```python
cmd = Cmd(request_bytes[1])
if not cmd.is_supported():
    writer.write(_build_socks5_reply(Reply.CMD_NOT_SUPPORTED))
    return
```

```python
cmd = Cmd(request_bytes[1])

match cmd:
    case Cmd.CONNECT:
        conn_id = new_conn_id()
        frame   = encode_conn_open_frame(conn_id, host, port)
        await ws.send(frame)
    case Cmd.UDP_ASSOCIATE:
        flow_id = new_flow_id()
        frame   = encode_udp_open_frame(flow_id, host, port)
        await ws.send(frame)
    case Cmd.BIND:
        reply = _build_socks5_reply(Reply.CMD_NOT_SUPPORTED)
        writer.write(reply)
```

---

### `AddrType`

RFC 1928 §4 — address type codes in SOCKS5 requests.

| Member | Value | Meaning |
|---|---|---|
| `IPV4` | `0x01` | 4-byte IPv4 address follows |
| `DOMAIN` | `0x03` | 1-byte length + N-byte domain name follows |
| `IPV6` | `0x04` | 16-byte IPv6 address follows |

```python
atyp = AddrType(request_bytes[3])

match atyp:
    case AddrType.IPV4:
        host = str(ipaddress.IPv4Address(request_bytes[4:8]))
        port = int.from_bytes(request_bytes[8:10])
    case AddrType.DOMAIN:
        length = request_bytes[4]
        host   = request_bytes[5 : 5 + length].decode("ascii")
        port   = int.from_bytes(request_bytes[5 + length : 7 + length])
    case AddrType.IPV6:
        host = str(ipaddress.IPv6Address(request_bytes[4:20]))
        port = int.from_bytes(request_bytes[20:22])
```

---

### `Reply`

RFC 1928 §6 — reply codes sent back to the SOCKS5 client.

| Member | Value | Meaning |
|---|---|---|
| `SUCCESS` | `0x00` | Request granted |
| `GENERAL_FAILURE` | `0x01` | General SOCKS server failure |
| `NOT_ALLOWED` | `0x02` | Connection not allowed by ruleset |
| `NET_UNREACHABLE` | `0x03` | Network unreachable |
| `HOST_UNREACHABLE` | `0x04` | Host unreachable |
| `REFUSED` | `0x05` | Connection refused |
| `TTL_EXPIRED` | `0x06` | TTL expired |
| `CMD_NOT_SUPPORTED` | `0x07` | Command not supported |
| `ADDR_NOT_SUPPORTED` | `0x08` | Address type not supported |

```python
def _build_socks5_reply(
    reply: Reply,
    bind_addr: str = "0.0.0.0",
    bind_port: int = 0,
) -> bytes:
    addr_bytes = ipaddress.IPv4Address(bind_addr).packed
    return bytes([
        0x05,           # SOCKS version
        reply,          # reply code
        0x00,           # reserved
        0x01,           # ATYP: IPv4
        *addr_bytes,    # BND.ADDR
        *bind_port.to_bytes(2),  # BND.PORT
    ])
```

---

### `UserPassStatus`

RFC 1929 §2 — username/password sub-negotiation reply.

| Member | Value | Meaning |
|---|---|---|
| `SUCCESS` | `0x00` | Credentials accepted |
| `FAILURE` | `0xFF` | Credentials rejected (canonical wire value) |

> **RFC 1929 §2 note:** Any non-zero status byte indicates failure, not just `0xFF`. `UserPassStatus._missing_` maps any value in `[0x01, 0xFE]` to `FAILURE` so that non-standard peers are handled correctly rather than raising `ValueError`.

```python
if await _verify_credentials(username, password):
    writer.write(bytes([0x01, UserPassStatus.SUCCESS]))
else:
    writer.write(bytes([0x01, UserPassStatus.FAILURE]))
    raise AuthenticationError(
        "SOCKS5 username/password authentication failed",
        details={"host": peer_addr, "auth_method": "username_password"},
    )
```

---

### `_missing_` Behaviour

All five enums raise `ValueError` immediately on unknown wire values:

```python
AuthMethod(0x99)
# ValueError: 0x99 is not a valid AuthMethod (expected one of [0, 1, 2, 255])
```

The proxy layer must catch this and map it to the appropriate SOCKS5 reply:

```python
try:
    cmd = Cmd(request_bytes[1])
except ValueError as exc:
    writer.write(_build_socks5_reply(Reply.CMD_NOT_SUPPORTED))
    return

try:
    atyp = AddrType(request_bytes[3])
except ValueError as exc:
    writer.write(_build_socks5_reply(Reply.ADDR_NOT_SUPPORTED))
    return
```

---

## Section 6 — Constants

### Frame Delimiters

| Constant | Value | Use |
|---|---|---|
| `FRAME_PREFIX` | `"<<<EXECTUNNEL:"` | Start sentinel — check with `line.startswith(FRAME_PREFIX)` |
| `FRAME_SUFFIX` | `">>>"` | End sentinel — check with `line.endswith(FRAME_SUFFIX)` |
| `READY_FRAME` | `"<<<EXECTUNNEL:AGENT_READY>>>"` | Compare directly with `is_ready_frame()` — do not string-compare manually |
| `MAX_FRAME_LEN` | `8_192` | Maximum frame content length in characters, excluding `\n` |

### Sentinel IDs

| Constant | Value | Use |
|---|---|---|
| `SESSION_CONN_ID` | `"c" + "0" × 24` | Pass to `encode_error_frame` for session-level errors; check with `frame.conn_id == SESSION_CONN_ID` |

```python
# Dispatching on session vs per-connection errors
case "ERROR":
    msg = decode_error_payload(frame.payload)
    if frame.conn_id == SESSION_CONN_ID:
        # Affects the whole tunnel — tear down the session
        raise ConnectionClosedError(
            f"Agent reported session error: {msg}",
            details={"close_code": 1011, "close_reason": msg},
        )
    else:
        # Affects only one connection — close it, keep the session alive
        await _close_connection(frame.conn_id, reason=msg)
```

---

## Section 7 — Exception Handling Guide

### The Two Exception Types

```
ProtocolError        →  YOU passed bad arguments to an encoder
                        Fix: validate inputs before calling encoders
                        Never catch silently — it is always a code bug

FrameDecodingError   →  THE REMOTE PEER sent corrupt data
                        Fix: close the affected connection or session
                        Always chain when re-raising
```

### Full Exception Handling Pattern

```python
from exectunnel.exceptions import (
    FrameDecodingError,
    ProtocolError,
    ConnectionClosedError,
    BootstrapError,
    AgentReadyTimeoutError,
)
from exectunnel.protocol import parse_frame, decode_binary_payload, decode_error_payload

# ── Inbound (decoding) ────────────────────────────────────────────────────────

async def handle_inbound(line: str) -> None:
    try:
        frame = parse_frame(line)
    except FrameDecodingError as exc:
        # Corrupt tunnel frame from remote peer — log and escalate
        log.error("protocol violation from agent", extra={"error": exc.to_dict()})
        raise ConnectionClosedError(
            "Agent sent a structurally corrupt tunnel frame",
            details={
                "close_code": 1002,
                "close_reason": exc.message,
            },
        ) from exc

    if frame is None:
        return  # shell noise — not an error

    if frame.msg_type == "DATA":
        try:
            data = decode_binary_payload(frame.payload)
        except FrameDecodingError as exc:
            # Bad payload in an otherwise valid frame — close this connection
            log.warning(
                "bad DATA payload for conn %s",
                frame.conn_id,
                extra={"error": exc.to_dict()},
            )
            await _close_connection(frame.conn_id)
            return
        await connections[frame.conn_id].write(data)

# ── Outbound (encoding) ───────────────────────────────────────────────────────

async def send_data(conn_id: str, data: bytes) -> None:
    # ProtocolError here means a bug in this layer — let it propagate
    frame = encode_data_frame(conn_id, data)
    await ws.send(frame)

# ── Bootstrap ─────────────────────────────────────────────────────────────────

async def wait_for_agent(websocket, pod_host: str) -> None:
    start = time.monotonic()
    try:
        async with asyncio.timeout(AGENT_READY_TIMEOUT):
            async for line in websocket:
                try:
                    if is_ready_frame(line):
                        return
                except FrameDecodingError as exc:
                    raise BootstrapError(
                        "Agent sent corrupt frame during bootstrap",
                        details={
                            "host": pod_host,
                            "elapsed_s": time.monotonic() - start,
                        },
                    ) from exc
    except TimeoutError as exc:
        raise AgentReadyTimeoutError(
            "Agent did not become ready within timeout",
            details={
                "timeout_s": AGENT_READY_TIMEOUT,
                "host": pod_host,
            },
        ) from exc
```

### What Not To Do

```python
# ✗ swallowing FrameDecodingError — hides protocol violations
try:
    frame = parse_frame(line)
except FrameDecodingError:
    pass   # WRONG — this is a peer bug, not noise

# ✗ catching ProtocolError in normal flow — it is always a code bug
try:
    frame = encode_data_frame(conn_id, data)
except ProtocolError:
    pass   # WRONG — fix the bug, don't catch it

# ✗ matching on message strings
except FrameDecodingError as exc:
    if "base64" in exc.message:   # WRONG — match on error_code or class
        ...

# ✓ correct — match on error_code
except FrameDecodingError as exc:
    match exc.error_code:
        case "protocol.frame_decoding_error":
            ...
```

---

## Section 8 — Integration Checklist

Use this checklist when integrating the protocol package into a new layer:

```
IDs
  □  Call new_conn_id() once per TCP connection — never reuse IDs
  □  Call new_flow_id() once per UDP flow — never reuse IDs
  □  Store IDs as dict keys — they are hashable strings
  □  Use SESSION_CONN_ID only for session-level encode_error_frame calls

ENCODING
  □  Never construct frame strings manually — always use encode_*_frame()
  □  Never construct IDs manually — always use new_conn_id() / new_flow_id()
  □  Always emit CONN_CLOSE / UDP_CLOSE on teardown — never rely on timeout alone
  □  Split data > 6,108 bytes before calling encode_data_frame()

DECODING
  □  Call parse_frame() on every inbound line
  □  Treat None return as normal — do not log at WARNING or above
  □  Treat FrameDecodingError as a protocol violation — always propagate
  □  Always call the correct payload helper for each msg_type
  □  Always check frame.conn_id == SESSION_CONN_ID in ERROR handler

EXCEPTIONS
  □  Always chain: raise XxxError(...) from exc
  □  Never match on exc.message strings — use exc.error_code or class
  □  Always include details= dict when raising ExecTunnelError subclasses
  □  Use exc.to_dict() for structured logging

SOCKS5 ENUMS
  □  Wrap Cmd(...) / AddrType(...) / AuthMethod(...) in try/except ValueError
  □  Map ValueError from _missing_ to the appropriate Reply code
  □  Never use raw integer literals where an enum member exists
```


## Section 9 — Complete Layer Integration Examples

These are full, copy-paste-ready patterns showing exactly how the protocol package is consumed at each layer boundary.

---

### 9.1 Transport Layer — Inbound Frame Pump

The transport layer's sole job with respect to the protocol package is to split the raw WebSocket text stream into lines and hand each line to `parse_frame`. It must not decode payloads — that is the session layer's responsibility.

```python
"""
transport/receiver.py — inbound frame pump

Responsibilities
────────────────
  - Split raw WebSocket messages into lines
  - Call parse_frame on each line
  - Distinguish noise (None) from protocol violations (FrameDecodingError)
  - Hand valid ParsedFrame objects to the session layer via a callback
  - Never decode payloads — that is the session layer's job
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from exectunnel.exceptions import ConnectionClosedError, FrameDecodingError
from exectunnel.protocol import ParsedFrame, parse_frame

log = logging.getLogger(__name__)

# Type alias for the session-layer callback
FrameCallback = Callable[[ParsedFrame], Awaitable[None]]


async def run_frame_pump(
    websocket,                        # websockets.ClientConnection
    on_frame: FrameCallback,
    *,
    ws_url: str,
    ws_host: str,
    ws_port: int,
) -> None:
    """Read frames from *websocket* and dispatch each to *on_frame*.

    Runs until the WebSocket closes or a protocol violation is detected.

    Raises:
        ConnectionClosedError: On WebSocket close or corrupt tunnel frame.
    """
    async for raw_message in websocket:
        # A single WebSocket message may contain multiple newline-delimited
        # frames if the sender batched them (uncommon but valid).
        for line in raw_message.splitlines():
            try:
                frame = parse_frame(line)
            except FrameDecodingError as exc:
                log.error(
                    "protocol violation — corrupt tunnel frame",
                    extra={"error": exc.to_dict()},
                )
                raise ConnectionClosedError(
                    "Remote agent sent a structurally corrupt tunnel frame.",
                    details={
                        "close_code": 1002,
                        "close_reason": exc.message,
                        "host": ws_host,
                        "port": ws_port,
                        "url":  ws_url,
                    },
                ) from exc

            if frame is None:
                # Shell noise, blank lines, bootstrap stdout — normal.
                log.debug("transport: non-frame line: %r", line[:80])
                continue

            log.debug(
                "transport: rx  msg_type=%-12s conn_id=%.8s…",
                frame.msg_type,
                frame.conn_id or "(none)",
            )
            await on_frame(frame)
```

---

### 9.2 Transport Layer — Outbound Frame Sender

```python
"""
transport/sender.py — outbound frame sender

Responsibilities
────────────────
  - Accept pre-encoded frame strings from the session layer
  - Send them over the WebSocket with a configurable timeout
  - Map WebSocket-level errors to TransportError subclasses
  - Never call encode_* directly — that is the session layer's job
"""

from __future__ import annotations

import asyncio
import logging

from exectunnel.exceptions import WebSocketSendTimeoutError

log = logging.getLogger(__name__)


async def send_frame(
    websocket,          # websockets.ClientConnection
    frame: str,
    *,
    timeout_s: float,
    ws_host: str,
    ws_port: int,
    ws_url: str,
) -> None:
    """Send a pre-encoded *frame* string over *websocket*.

    Args:
        websocket:  Open WebSocket connection.
        frame:      Newline-terminated frame string from any encode_*_frame().
        timeout_s:  Maximum seconds to wait for the send to complete.

    Raises:
        WebSocketSendTimeoutError: If the send does not complete within
            *timeout_s* seconds.
    """
    payload_bytes = len(frame.encode("utf-8"))
    log.debug("transport: tx  %d bytes", payload_bytes)

    try:
        async with asyncio.timeout(timeout_s):
            await websocket.send(frame)
    except TimeoutError as exc:
        raise WebSocketSendTimeoutError(
            "WebSocket send stalled — channel may be congested.",
            details={
                "timeout_s":     timeout_s,
                "payload_bytes": payload_bytes,
                "host":          ws_host,
                "port":          ws_port,
                "url":           ws_url,
            },
            hint=(
                f"The frame could not be sent within {timeout_s}s. "
                "Check network stability or increase WS_SEND_TIMEOUT."
            ),
        ) from exc
```

---

### 9.3 Session Layer — Full Inbound Dispatcher

The session layer receives `ParsedFrame` objects from the transport layer and is responsible for all payload decoding and connection-table dispatch.

```python
"""
session/dispatcher.py — inbound frame dispatcher

Responsibilities
────────────────
  - Decode payloads using the correct protocol helper per msg_type
  - Dispatch to the appropriate connection / flow handler
  - Distinguish session-level errors (SESSION_CONN_ID) from
    per-connection errors
  - Raise UnexpectedFrameError for frames that arrive in the wrong state
"""

from __future__ import annotations

import logging

from exectunnel.exceptions import (
    ConnectionClosedError,
    FrameDecodingError,
    UnexpectedFrameError,
)
from exectunnel.protocol import (
    SESSION_CONN_ID,
    ParsedFrame,
    decode_binary_payload,
    decode_error_payload,
    parse_host_port,
)

log = logging.getLogger(__name__)


class InboundDispatcher:
    """Dispatches inbound :class:`ParsedFrame` objects to the correct handler.

    Args:
        connections: Live TCP connection table ``{conn_id: asyncio.StreamWriter}``.
        udp_flows:   Live UDP flow table ``{flow_id: UdpFlow}``.
        state:       Current session state string (for UnexpectedFrameError).
    """

    def __init__(
        self,
        connections: dict,
        udp_flows: dict,
        state: str,
    ) -> None:
        self._connections = connections
        self._udp_flows   = udp_flows
        self._state       = state

    async def dispatch(self, frame: ParsedFrame) -> None:
        """Dispatch *frame* to the appropriate handler.

        Raises:
            ConnectionClosedError:  On session-level ERROR frame.
            FrameDecodingError:     On corrupt payload (propagated).
            UnexpectedFrameError:   On frames that cannot be handled in the
                                    current session state.
        """
        match frame.msg_type:

            case "DATA":
                await self._handle_data(frame)

            case "UDP_DATA":
                await self._handle_udp_data(frame)

            case "CONN_CLOSE":
                await self._handle_conn_close(frame)

            case "UDP_CLOSE":
                await self._handle_udp_close(frame)

            case "ERROR":
                await self._handle_error(frame)

            case "AGENT_READY":
                # AGENT_READY is only valid during bootstrap — not here.
                raise UnexpectedFrameError(
                    "Received AGENT_READY after session was already established.",
                    details={
                        "state":      self._state,
                        "frame_type": frame.msg_type,
                    },
                )

            case "CONN_OPEN" | "UDP_OPEN":
                # These are client→agent only; receiving them from the agent
                # is a protocol violation.
                raise UnexpectedFrameError(
                    f"Agent sent a client-only frame type: {frame.msg_type!r}.",
                    details={
                        "state":      self._state,
                        "frame_type": frame.msg_type,
                    },
                )

            case _:
                # parse_frame already validated msg_type — this branch should
                # never be reached. Raise rather than silently drop.
                raise UnexpectedFrameError(
                    f"Unhandled msg_type {frame.msg_type!r} in dispatcher.",
                    details={
                        "state":      self._state,
                        "frame_type": frame.msg_type,
                    },
                )

    # ── Per-type handlers ─────────────────────────────────────────────────────

    async def _handle_data(self, frame: ParsedFrame) -> None:
        writer = self._connections.get(frame.conn_id)
        if writer is None:
            log.warning(
                "DATA for unknown conn_id %.8s… — dropping",
                frame.conn_id,
            )
            return

        try:
            data = decode_binary_payload(frame.payload)
        except FrameDecodingError as exc:
            log.error(
                "corrupt DATA payload for conn %.8s…",
                frame.conn_id,
                extra={"error": exc.to_dict()},
            )
            # Data integrity is compromised — close this connection only.
            await self._close_connection(frame.conn_id)
            return

        writer.write(data)
        await writer.drain()

    async def _handle_udp_data(self, frame: ParsedFrame) -> None:
        flow = self._udp_flows.get(frame.conn_id)
        if flow is None:
            log.warning(
                "UDP_DATA for unknown flow_id %.8s… — dropping",
                frame.conn_id,
            )
            return

        try:
            data = decode_binary_payload(frame.payload)
        except FrameDecodingError as exc:
            log.error(
                "corrupt UDP_DATA payload for flow %.8s…",
                frame.conn_id,
                extra={"error": exc.to_dict()},
            )
            await self._close_udp_flow(frame.conn_id)
            return

        await flow.send(data)

    async def _handle_conn_close(self, frame: ParsedFrame) -> None:
        if frame.conn_id not in self._connections:
            log.debug(
                "CONN_CLOSE for unknown/already-closed conn %.8s… — ignoring",
                frame.conn_id,
            )
            return
        await self._close_connection(frame.conn_id)

    async def _handle_udp_close(self, frame: ParsedFrame) -> None:
        if frame.conn_id not in self._udp_flows:
            log.debug(
                "UDP_CLOSE for unknown/already-closed flow %.8s… — ignoring",
                frame.conn_id,
            )
            return
        await self._close_udp_flow(frame.conn_id)

    async def _handle_error(self, frame: ParsedFrame) -> None:
        try:
            message = decode_error_payload(frame.payload)
        except FrameDecodingError as exc:
            # We know an error occurred even if we cannot decode the message.
            message = (
                f"<undecodable error payload — "
                f"raw_bytes={exc.details.get('raw_bytes', '?')!r}>"
            )
            log.warning(
                "could not decode ERROR payload for conn %.8s…",
                frame.conn_id,
                extra={"error": exc.to_dict()},
            )

        if frame.conn_id == SESSION_CONN_ID:
            # Session-level error — the entire tunnel is compromised.
            log.error("agent reported session-level error: %s", message)
            raise ConnectionClosedError(
                f"Agent reported a session-level error: {message}",
                details={
                    "close_code":   1011,
                    "close_reason": message,
                },
            )

        # Per-connection error — close only the affected connection.
        log.warning(
            "agent reported error for conn %.8s…: %s",
            frame.conn_id,
            message,
        )
        await self._close_connection(frame.conn_id)

    # ── Teardown helpers ──────────────────────────────────────────────────────

    async def _close_connection(self, conn_id: str) -> None:
        writer = self._connections.pop(conn_id, None)
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass

    async def _close_udp_flow(self, flow_id: str) -> None:
        flow = self._udp_flows.pop(flow_id, None)
        if flow is not None:
            await flow.close()
```

---

### 9.4 Session Layer — Outbound Frame Builder

```python
"""
session/outbound.py — outbound frame builder

Responsibilities
────────────────
  - Translate session-layer intent into encoded frame strings
  - Call encode_*_frame() with validated arguments
  - Hand the resulting frame string to the transport sender
  - Never write to the WebSocket directly
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from exectunnel.protocol import (
    SESSION_CONN_ID,
    encode_conn_close_frame,
    encode_conn_open_frame,
    encode_data_frame,
    encode_error_frame,
    encode_udp_close_frame,
    encode_udp_data_frame,
    encode_udp_open_frame,
    new_conn_id,
    new_flow_id,
)

log = logging.getLogger(__name__)

# Injected by the session — calls transport/sender.send_frame
SendCallback = Callable[[str], Awaitable[None]]

# Maximum raw bytes per DATA / UDP_DATA frame.
# Derived from MAX_FRAME_LEN budget — see architecture doc §4.4.
_MAX_DATA_BYTES: int = 6_108


class OutboundFrameBuilder:
    """Builds and sends outbound frames on behalf of the session layer.

    Args:
        send: Coroutine that accepts a pre-encoded frame string and sends
              it over the WebSocket (injected from transport layer).
    """

    def __init__(self, send: SendCallback) -> None:
        self._send = send

    # ── TCP ───────────────────────────────────────────────────────────────────

    async def open_connection(self, host: str, port: int) -> str:
        """Open a new TCP connection through the tunnel.

        Returns:
            The ``conn_id`` assigned to this connection.
        """
        conn_id = new_conn_id()
        frame   = encode_conn_open_frame(conn_id, host, port)
        log.debug("session: CONN_OPEN  %.8s… → %s:%d", conn_id, host, port)
        await self._send(frame)
        return conn_id

    async def close_connection(self, conn_id: str) -> None:
        """Close an existing TCP connection through the tunnel."""
        frame = encode_conn_close_frame(conn_id)
        log.debug("session: CONN_CLOSE %.8s…", conn_id)
        await self._send(frame)

    async def send_data(self, conn_id: str, data: bytes) -> None:
        """Send *data* over an existing TCP connection.

        Automatically splits *data* into chunks that fit within
        ``MAX_FRAME_LEN``.
        """
        for offset in range(0, max(len(data), 1), _MAX_DATA_BYTES):
            chunk = data[offset : offset + _MAX_DATA_BYTES]
            frame = encode_data_frame(conn_id, chunk)
            log.debug(
                "session: DATA       %.8s…  %d bytes",
                conn_id,
                len(chunk),
            )
            await self._send(frame)

    # ── UDP ───────────────────────────────────────────────────────────────────

    async def open_udp_flow(self, host: str, port: int) -> str:
        """Open a new UDP flow through the tunnel.

        Returns:
            The ``flow_id`` assigned to this flow.
        """
        flow_id = new_flow_id()
        frame   = encode_udp_open_frame(flow_id, host, port)
        log.debug("session: UDP_OPEN   %.8s… → %s:%d", flow_id, host, port)
        await self._send(frame)
        return flow_id

    async def close_udp_flow(self, flow_id: str) -> None:
        """Close an existing UDP flow through the tunnel."""
        frame = encode_udp_close_frame(flow_id)
        log.debug("session: UDP_CLOSE  %.8s…", flow_id)
        await self._send(frame)

    async def send_udp_datagram(self, flow_id: str, data: bytes) -> None:
        """Send a UDP datagram over an existing flow.

        Note:
            UDP datagrams are not split — a datagram larger than
            ``_MAX_DATA_BYTES`` will raise ``ProtocolError``. The proxy
            layer must enforce MTU limits before calling this method.
        """
        frame = encode_udp_data_frame(flow_id, data)
        log.debug(
            "session: UDP_DATA   %.8s…  %d bytes",
            flow_id,
            len(data),
        )
        await self._send(frame)

    # ── Errors ────────────────────────────────────────────────────────────────

    async def send_error(self, conn_id: str, message: str) -> None:
        """Send an ERROR frame for a specific connection or the whole session.

        Args:
            conn_id: Connection ID, or ``SESSION_CONN_ID`` for session errors.
            message: Human-readable error description.
        """
        frame = encode_error_frame(conn_id, message)
        log.debug(
            "session: ERROR      %.8s…  %r",
            conn_id,
            message[:60],
        )
        await self._send(frame)

    async def send_session_error(self, message: str) -> None:
        """Convenience wrapper — send a session-level ERROR frame."""
        await self.send_error(SESSION_CONN_ID, message)
```

---

### 9.5 Proxy Layer — SOCKS5 Request Parser

The proxy layer uses the SOCKS5 enums to parse client requests and then calls the session layer's outbound builder. It never touches frame encoding directly.

```python
"""
proxy/socks5.py — SOCKS5 request parser

Responsibilities
────────────────
  - Parse the SOCKS5 handshake from the local client
  - Map SOCKS5 commands to session-layer open_connection / open_udp_flow calls
  - Map SOCKS5 address types to host strings for encode_host_port
  - Send RFC 1928 reply bytes back to the local client
  - Never call encode_*_frame directly — delegate to session outbound builder
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging

from exectunnel.exceptions import AuthenticationError
from exectunnel.protocol import (
    AddrType,
    AuthMethod,
    Cmd,
    Reply,
    UserPassStatus,
)

log = logging.getLogger(__name__)

_SOCKS_VERSION = 0x05
_USERPASS_VERSION = 0x01


def _reply_bytes(
    reply: Reply,
    bind_host: str = "0.0.0.0",
    bind_port: int = 0,
) -> bytes:
    """Build a SOCKS5 reply packet (RFC 1928 §6)."""
    addr = ipaddress.IPv4Address(bind_host).packed
    return bytes([
        _SOCKS_VERSION,
        int(reply),
        0x00,           # RSV
        int(AddrType.IPV4),
        *addr,
        *bind_port.to_bytes(2),
    ])


async def handle_socks5_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    outbound,           # OutboundFrameBuilder
    *,
    require_auth: bool = False,
    username: str = "",
    password: str = "",
) -> str | None:
    """Perform the full SOCKS5 handshake and open the tunnel connection.

    Returns:
        The ``conn_id`` or ``flow_id`` assigned, or ``None`` if the
        handshake failed (reply already sent to client).

    Raises:
        AuthenticationError: If credentials are required and rejected.
    """
    peer = writer.get_extra_info("peername")

    # ── Phase 1: Method negotiation ───────────────────────────────────────────
    header = await reader.readexactly(2)
    if header[0] != _SOCKS_VERSION:
        writer.close()
        return None

    n_methods = header[1]
    method_bytes = await reader.readexactly(n_methods)

    try:
        offered = {AuthMethod(b) for b in method_bytes}
    except ValueError:
        # Unknown method bytes are simply ignored during negotiation —
        # RFC 1928 §3 says the server picks from the offered list.
        offered = {AuthMethod(b) for b in method_bytes if b in AuthMethod._value2member_map_}

    if require_auth and AuthMethod.USERNAME_PASSWORD in offered:
        chosen = AuthMethod.USERNAME_PASSWORD
    elif not require_auth and AuthMethod.NO_AUTH in offered:
        chosen = AuthMethod.NO_AUTH
    else:
        writer.write(bytes([_SOCKS_VERSION, int(AuthMethod.NO_ACCEPT)]))
        await writer.drain()
        writer.close()
        return None

    writer.write(bytes([_SOCKS_VERSION, int(chosen)]))
    await writer.drain()

    # ── Phase 2: Username/password sub-negotiation (RFC 1929) ─────────────────
    if chosen == AuthMethod.USERNAME_PASSWORD:
        sub_header = await reader.readexactly(2)
        ulen = sub_header[1]
        uname_bytes = await reader.readexactly(ulen)
        plen_byte = await reader.readexactly(1)
        passwd_bytes = await reader.readexactly(plen_byte[0])

        uname = uname_bytes.decode("utf-8", errors="replace")
        passwd = passwd_bytes.decode("utf-8", errors="replace")

        if uname == username and passwd == password:
            writer.write(bytes([_USERPASS_VERSION, int(UserPassStatus.SUCCESS)]))
            await writer.drain()
        else:
            writer.write(bytes([_USERPASS_VERSION, int(UserPassStatus.FAILURE)]))
            await writer.drain()
            writer.close()
            raise AuthenticationError(
                "SOCKS5 username/password authentication failed.",
                details={
                    "host":        str(peer),
                    "auth_method": "username_password",
                },
            )

    # ── Phase 3: Request ──────────────────────────────────────────────────────
    req_header = await reader.readexactly(4)
    if req_header[0] != _SOCKS_VERSION:
        writer.write(_reply_bytes(Reply.GENERAL_FAILURE))
        await writer.drain()
        writer.close()
        return None

    try:
        cmd  = Cmd(req_header[1])
        atyp = AddrType(req_header[3])
    except ValueError as exc:
        code = (
            Reply.CMD_NOT_SUPPORTED
            if "Cmd" in str(type(exc).__name__)
            else Reply.ADDR_NOT_SUPPORTED
        )
        writer.write(_reply_bytes(code))
        await writer.drain()
        writer.close()
        return None

    # ── Phase 4: Address parsing ──────────────────────────────────────────────
    match atyp:
        case AddrType.IPV4:
            raw  = await reader.readexactly(4)
            host = str(ipaddress.IPv4Address(raw))
        case AddrType.IPV6:
            raw  = await reader.readexactly(16)
            host = str(ipaddress.IPv6Address(raw))
        case AddrType.DOMAIN:
            length = (await reader.readexactly(1))[0]
            host   = (await reader.readexactly(length)).decode("ascii")

    port_bytes = await reader.readexactly(2)
    port = int.from_bytes(port_bytes)

    # ── Phase 5: Command dispatch ─────────────────────────────────────────────
    match cmd:
        case Cmd.CONNECT:
            tunnel_id = await outbound.open_connection(host, port)
            writer.write(_reply_bytes(Reply.SUCCESS))
            await writer.drain()
            log.info(
                "SOCKS5 CONNECT  %.8s… → %s:%d  peer=%s",
                tunnel_id, host, port, peer,
            )
            return tunnel_id

        case Cmd.UDP_ASSOCIATE:
            tunnel_id = await outbound.open_udp_flow(host, port)
            writer.write(_reply_bytes(Reply.SUCCESS))
            await writer.drain()
            log.info(
                "SOCKS5 UDP_ASSOC %.8s… → %s:%d  peer=%s",
                tunnel_id, host, port, peer,
            )
            return tunnel_id

        case Cmd.BIND:
            writer.write(_reply_bytes(Reply.CMD_NOT_SUPPORTED))
            await writer.drain()
            writer.close()
            log.warning("SOCKS5 BIND rejected — not supported  peer=%s", peer)
            return None
```

---

### 9.6 Agent Side — Inbound Frame Handler

The agent runs inside the pod. It uses the same protocol package (no asyncio, no websockets) and reads/writes frames over `stdin`/`stdout`.

```python
"""
agent/handler.py — agent-side inbound frame handler

Responsibilities
────────────────
  - Parse frames arriving on stdin
  - Dispatch CONN_OPEN / UDP_OPEN to raw socket creation
  - Forward DATA / UDP_DATA to the appropriate socket
  - Emit DATA / ERROR / CONN_CLOSE frames back on stdout
  - Use only stdlib — no asyncio, no websockets library
"""

from __future__ import annotations

import socket
import sys
import logging

from exectunnel.exceptions import FrameDecodingError
from exectunnel.protocol import (
    SESSION_CONN_ID,
    ParsedFrame,
    decode_binary_payload,
    encode_conn_close_frame,
    encode_data_frame,
    encode_error_frame,
    parse_frame,
    parse_host_port,
)

log = logging.getLogger(__name__)


def _emit(frame: str) -> None:
    """Write *frame* to stdout and flush immediately."""
    sys.stdout.write(frame)
    sys.stdout.flush()


def _emit_error(conn_id: str, message: str) -> None:
    """Encode and emit an ERROR frame, then log."""
    log.error("agent error  conn=%.8s…  %s", conn_id, message)
    _emit(encode_error_frame(conn_id, message))


class AgentFrameHandler:
    """Handles inbound frames on the agent side.

    Args:
        tcp_sockets: Live TCP socket table ``{conn_id: socket.socket}``.
        udp_sockets: Live UDP socket table ``{flow_id: socket.socket}``.
    """

    def __init__(
        self,
        tcp_sockets: dict[str, socket.socket],
        udp_sockets: dict[str, socket.socket],
    ) -> None:
        self._tcp = tcp_sockets
        self._udp = udp_sockets

    def handle_line(self, line: str) -> None:
        """Parse and dispatch one line from stdin.

        Non-tunnel lines are silently ignored.
        FrameDecodingError causes an ERROR frame to be emitted and the
        affected connection to be closed — the agent never crashes on
        a single bad frame.
        """
        try:
            frame = parse_frame(line)
        except FrameDecodingError as exc:
            # We cannot associate this with a specific connection — use
            # SESSION_CONN_ID so the client knows the whole session is at risk.
            log.error(
                "corrupt frame from client",
                extra={"error": exc.to_dict()},
            )
            _emit_error(SESSION_CONN_ID, f"Corrupt frame received: {exc.message}")
            return

        if frame is None:
            return  # not a tunnel frame — ignore

        match frame.msg_type:
            case "CONN_OPEN":
                self._handle_conn_open(frame)
            case "CONN_CLOSE":
                self._handle_conn_close(frame)
            case "DATA":
                self._handle_data(frame)
            case "UDP_OPEN":
                self._handle_udp_open(frame)
            case "UDP_DATA":
                self._handle_udp_data(frame)
            case "UDP_CLOSE":
                self._handle_udp_close(frame)
            case "ERROR":
                # Client sent an error — log and close the connection.
                log.warning(
                    "client reported error for conn %.8s…",
                    frame.conn_id,
                )
                self._close_tcp(frame.conn_id)
            case _:
                # parse_frame validated msg_type — this is unreachable.
                _emit_error(
                    SESSION_CONN_ID,
                    f"Unhandled msg_type {frame.msg_type!r} on agent",
                )

    # ── Handlers ──────────────────────────────────────────────────────────────

    def _handle_conn_open(self, frame: ParsedFrame) -> None:
        try:
            host, port = parse_host_port(frame.payload)
        except FrameDecodingError as exc:
            _emit_error(frame.conn_id, f"Malformed CONN_OPEN payload: {exc.message}")
            return

        try:
            sock = socket.create_connection((host, port), timeout=10)
            sock.setblocking(False)
        except OSError as exc:
            _emit_error(frame.conn_id, f"TCP connect failed: {exc}")
            return

        self._tcp[frame.conn_id] = sock
        log.info("agent: CONN_OPEN  %.8s… → %s:%d", frame.conn_id, host, port)

    def _handle_conn_close(self, frame: ParsedFrame) -> None:
        self._close_tcp(frame.conn_id)

    def _handle_data(self, frame: ParsedFrame) -> None:
        sock = self._tcp.get(frame.conn_id)
        if sock is None:
            _emit_error(frame.conn_id, "DATA for unknown connection")
            return

        try:
            data = decode_binary_payload(frame.payload)
        except FrameDecodingError as exc:
            _emit_error(
                frame.conn_id,
                f"Corrupt DATA payload: {exc.details.get('raw_bytes', '?')}",
            )
            self._close_tcp(frame.conn_id)
            return

        try:
            sock.sendall(data)
        except OSError as exc:
            _emit_error(frame.conn_id, f"TCP send failed: {exc}")
            self._close_tcp(frame.conn_id)

    def _handle_udp_open(self, frame: ParsedFrame) -> None:
        try:
            host, port = parse_host_port(frame.payload)
        except FrameDecodingError as exc:
            _emit_error(frame.conn_id, f"Malformed UDP_OPEN payload: {exc.message}")
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect((host, port))
        self._udp[frame.conn_id] = sock
        log.info("agent: UDP_OPEN   %.8s… → %s:%d", frame.conn_id, host, port)

    def _handle_udp_data(self, frame: ParsedFrame) -> None:
        sock = self._udp.get(frame.conn_id)
        if sock is None:
            _emit_error(frame.conn_id, "UDP_DATA for unknown flow")
            return

        try:
            data = decode_binary_payload(frame.payload)
        except FrameDecodingError as exc:
            _emit_error(
                frame.conn_id,
                f"Corrupt UDP_DATA payload: {exc.details.get('raw_bytes', '?')}",
            )
            self._close_udp(frame.conn_id)
            return

        try:
            sock.send(data)
        except OSError as exc:
            _emit_error(frame.conn_id, f"UDP send failed: {exc}")
            self._close_udp(frame.conn_id)

    def _handle_udp_close(self, frame: ParsedFrame) -> None:
        self._close_udp(frame.conn_id)

    # ── Teardown helpers ──────────────────────────────────────────────────────

    def _close_tcp(self, conn_id: str) -> None:
        sock = self._tcp.pop(conn_id, None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
            _emit(encode_conn_close_frame(conn_id))
            log.debug("agent: CONN_CLOSE %.8s…", conn_id)

    def _close_udp(self, flow_id: str) -> None:
        sock = self._udp.pop(flow_id, None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
            log.debug("agent: UDP_CLOSE  %.8s…", flow_id)
```

---

## Section 10 — Testing Patterns

Canonical patterns for unit-testing code that uses the protocol package.

### 10.1 Frame Round-Trip Tests

```python
import pytest
from exectunnel.protocol import (
    encode_conn_open_frame, encode_data_frame, encode_error_frame,
    encode_udp_open_frame, encode_udp_data_frame,
    parse_frame, decode_binary_payload, decode_error_payload,
    parse_host_port, new_conn_id, new_flow_id,
    SESSION_CONN_ID,
)
from exectunnel.exceptions import FrameDecodingError, ProtocolError


@pytest.mark.parametrize("host,port", [
    ("redis.default.svc", 6379),
    ("192.168.1.1", 5432),
    ("2001:db8::1", 443),
    ("::1", 8080),
])
def test_conn_open_round_trip(host: str, port: int) -> None:
    conn_id = new_conn_id()
    frame   = encode_conn_open_frame(conn_id, host, port)
    parsed  = parse_frame(frame.strip())

    assert parsed is not None
    assert parsed.msg_type == "CONN_OPEN"
    assert parsed.conn_id  == conn_id

    decoded_host, decoded_port = parse_host_port(parsed.payload)
    assert decoded_port == port
    # Host may be normalised (IPv6 compression) — compare semantically
    import ipaddress
    try:
        assert ipaddress.ip_address(decoded_host) == ipaddress.ip_address(host)
    except ValueError:
        assert decoded_host == host  # domain name — exact match


def test_data_frame_round_trip() -> None:
    conn_id  = new_conn_id()
    original = b"\x00\x01\x02\xff" * 256
    frame    = encode_data_frame(conn_id, original)
    parsed   = parse_frame(frame.strip())

    assert parsed is not None
    assert parsed.msg_type == "DATA"
    assert decode_binary_payload(parsed.payload) == original


def test_error_frame_round_trip() -> None:
    message = "Connection refused — target unreachable\nLine 2 with unicode: café"
    frame   = encode_error_frame(SESSION_CONN_ID, message)
    parsed  = parse_frame(frame.strip())

    assert parsed is not None
    assert parsed.msg_type == "ERROR"
    assert parsed.conn_id  == SESSION_CONN_ID
    assert decode_error_payload(parsed.payload) == message
```

### 10.2 `parse_frame` Boundary Tests

```python
def test_parse_frame_returns_none_for_noise() -> None:
    for line in ["", "bash-5.1$", "  ", "some random output", ">>>", "<<<"]:
        assert parse_frame(line) is None


def test_parse_frame_raises_on_corrupt_tunnel_frame() -> None:
    # Has prefix+suffix but unknown msg_type
    with pytest.raises(FrameDecodingError) as exc_info:
        parse_frame("<<<EXECTUNNEL:BADTYPE:ca1b2c3d4e5f6a7b8c9d0e1f2a3b>>>")
    assert exc_info.value.error_code == "protocol.frame_decoding_error"
    assert "raw_bytes" in exc_info.value.details
    assert exc_info.value.details["codec"] == "frame"


def test_parse_frame_raises_on_malformed_conn_id() -> None:
    with pytest.raises(FrameDecodingError):
        parse_frame("<<<EXECTUNNEL:DATA:NOTANID:abc>>>")


def test_parse_frame_drops_oversized_line() -> None:
    oversized = "<<<EXECTUNNEL:DATA:c" + "a" * 24 + ":" + "x" * 9000 + ">>>"
    assert parse_frame(oversized) is None
```

### 10.3 Encoder Validation Tests

```python
def test_encoder_rejects_bad_conn_id() -> None:
    with pytest.raises(ProtocolError) as exc_info:
        encode_data_frame("not-an-id", b"hello")
    assert exc_info.value.error_code == "protocol.error"


def test_encoder_rejects_port_zero() -> None:
    with pytest.raises(ProtocolError):
        encode_conn_open_frame(new_conn_id(), "redis", 0)


def test_encoder_rejects_frame_unsafe_host() -> None:
    with pytest.raises(ProtocolError):
        encode_conn_open_frame(new_conn_id(), "bad:host", 80)


def test_encoder_rejects_oversized_data() -> None:
    with pytest.raises(ProtocolError):
        encode_data_frame(new_conn_id(), b"x" * 10_000)
```

### 10.4 Exception Contract Tests

```python
def test_frame_decoding_error_is_chained() -> None:
    """FrameDecodingError must chain the original stdlib exception."""
    with pytest.raises(FrameDecodingError) as exc_info:
        decode_binary_payload("!!!not-base64!!!")
    assert exc_info.value.__cause__ is not None


def test_frame_decoding_error_details_keys() -> None:
    with pytest.raises(FrameDecodingError) as exc_info:
        decode_binary_payload("!!!not-base64!!!")
    details = exc_info.value.details
    assert "raw_bytes" in details
    assert "codec"     in details
    assert details["codec"] == "base64url"


def test_protocol_error_details_keys() -> None:
    with pytest.raises(ProtocolError) as exc_info:
        encode_conn_open_frame(new_conn_id(), "", 80)
    details = exc_info.value.details
    assert "frame_type" in details
    assert "expected"   in details


def test_session_conn_id_is_structurally_valid() -> None:
    """SESSION_CONN_ID must pass parse_frame without raising."""
    frame  = encode_error_frame(SESSION_CONN_ID, "test")
    parsed = parse_frame(frame.strip())
    assert parsed is not None
    assert parsed.conn_id == SESSION_CONN_ID
```

