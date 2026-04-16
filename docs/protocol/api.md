# ExecTunnel — Protocol Package API Reference

```
exectunnel/protocol/  |  api-doc v1.2  |  Python 3.13+
audience: developers building transport / proxy / session / agent layers
```

---

## How to Read This Document

This reference is written for developers who **consume** the protocol package
from an upper layer. It answers three questions for every symbol:

1. **What does it do** — precise contract, not implementation detail
2. **What can go wrong** — every exception, when it fires, what `details` carries
3. **How to use it correctly** — copy-paste patterns, common mistakes, gotchas

Import everything from the package root:

```python
from exectunnel.protocol import (
    # constants
    FRAME_PREFIX, FRAME_SUFFIX, READY_FRAME, SESSION_CONN_ID,
    MAX_FRAME_LEN, PORT_UNSPECIFIED,
    # frame result type
    ParsedFrame,
    # encoders
    encode_conn_open_frame, encode_conn_ack_frame, encode_conn_close_frame,
    encode_data_frame, encode_error_frame, encode_keepalive_frame,
    encode_udp_open_frame, encode_udp_data_frame, encode_udp_close_frame,
    # decoder
    parse_frame, is_ready_frame,
    # payload helpers
    decode_binary_payload, decode_error_payload,
    encode_host_port, parse_host_port,
    # id generators & validators
    new_conn_id, new_flow_id, new_session_id,
    CONN_FLOW_ID_RE, ID_RE, SESSION_ID_RE,
    # socks5 enums
    AddrType, AuthMethod, Cmd, Reply, UserPassStatus,
)
```

Never import from sub-modules directly — the sub-module layout is an
implementation detail and may change.

---

## Quick-Reference Card

```
GENERATE IDs ──────────────────────────────────────────────────────────────────
  new_conn_id()                     →  "c<24 hex>"   TCP connection ID
  new_flow_id()                     →  "u<24 hex>"   UDP flow ID
  new_session_id()                  →  "s<24 hex>"   session correlation ID
  CONN_FLOW_ID_RE                   →  compiled re   validate c/u ID strings
  ID_RE                             →  compiled re   alias for CONN_FLOW_ID_RE
  SESSION_ID_RE                     →  compiled re   validate s session IDs

ENCODE FRAMES ─────────────────────────────────────────────────────────────────
  encode_conn_open_frame(id, h, p)  →  frame str     open TCP connection
  encode_conn_ack_frame(id)         →  frame str     acknowledge CONN_OPEN
  encode_conn_close_frame(id)       →  frame str     close TCP connection
  encode_data_frame(id, bytes)      →  frame str     TCP data chunk
  encode_udp_open_frame(id, h, p)   →  frame str     open UDP flow
  encode_udp_data_frame(id, bytes)  →  frame str     UDP datagram
  encode_udp_close_frame(id)        →  frame str     close UDP flow
  encode_error_frame(id, msg)       →  frame str     error report
  encode_keepalive_frame()          →  frame str     session heartbeat (never raises)

DECODE FRAMES ─────────────────────────────────────────────────────────────────
  parse_frame(line)                 →  ParsedFrame | None
  is_ready_frame(line)              →  bool           never raises

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

**When to call it:** Once per accepted SOCKS5 `CONNECT` request, before calling
`encode_conn_open_frame`. Store the returned ID as the key in your connection
table for the lifetime of that TCP connection.

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

**When to call it:** Once per accepted SOCKS5 `UDP_ASSOCIATE` request, before
calling `encode_udp_open_frame`.

```python
flow_id = new_flow_id()
frame   = encode_udp_open_frame(flow_id, "8.8.8.8", 53)
await ws.send(frame)
udp_flows[flow_id] = remote_addr
```

---

### `new_session_id() → str`

Generates a cryptographically random session correlation ID.

**Returns:** `"s"` followed by 24 lowercase hex characters.
Example: `"s3f7a1c9e2b4d6f8a0c2e4b6d8"`

**Raises:** Nothing.

**Important:** Session IDs use the `s` prefix and are validated by
`SESSION_ID_RE`. They are **never embedded in tunnel frames** — they exist
solely for log and trace correlation at the session layer.

```python
session_id = new_session_id()
log.info("tunnel session opened", extra={"session_id": session_id})
# Pass to observability / tracing — never pass to encode_*_frame()
```

---

### ID Format Contract

All three generators share the same structural guarantee:

```
[cu][0-9a-f]{24}    ← TCP conn IDs and UDP flow IDs  (CONN_FLOW_ID_RE / ID_RE)
s[0-9a-f]{24}       ← session correlation IDs         (SESSION_ID_RE)
│    └─────────── 24 lowercase hex chars (96-bit CSPRNG token)
└─────────────── "c" TCP  |  "u" UDP  |  "s" session
```

**Never construct IDs manually.** The format is validated by every encoder — a
hand-crafted string that fails `CONN_FLOW_ID_RE` will raise `ProtocolError`
immediately.

**`CONN_FLOW_ID_RE` is the canonical name.** `ID_RE` is a backward-compatible
alias pointing to the same compiled pattern object. New code should prefer
`CONN_FLOW_ID_RE` for clarity.

**`SESSION_CONN_ID` is not a real ID.** It is a reserved sentinel
(`"c" + "0" * 24`) used exclusively in `encode_error_frame` for session-level
errors. Do not store it in your connection table.

```python
# ✓ correct — session-level error
frame = encode_error_frame(SESSION_CONN_ID, "WebSocket closed unexpectedly")

# ✗ wrong — SESSION_CONN_ID is not a real connection
connections[SESSION_CONN_ID] = writer

# ✗ wrong — session IDs must never be passed to frame encoders
frame = encode_conn_open_frame(new_session_id(), "redis", 6379)
# → ProtocolError: Invalid tunnel CONN_OPEN ID 's...': must match [cu][0-9a-f]{24}
```

---

## Section 2 — Frame Encoders

All encoders share the same contract:

* **Return:** a newline-terminated ASCII string ready to write to the WebSocket
  channel.
* **Raise:** `ProtocolError` if any argument is invalid. This always indicates a
  bug in the calling layer, never a remote peer fault.
* **Thread / async safety:** all encoders are pure functions with no shared
  state — safe to call from any thread or coroutine concurrently.

---

### `encode_conn_open_frame(conn_id: str, host: str, port: int) → str`

Encodes a `CONN_OPEN` frame instructing the agent to open a TCP connection.

**Arguments:**

| Argument | Type | Constraints |
|---|---|---|
| `conn_id` | `str` | Must match `[cu][0-9a-f]{24}` — use `new_conn_id()` |
| `host` | `str` | Non-empty hostname, IPv4, or IPv6 literal |
| `port` | `int` | `1 ≤ port ≤ 65535` |

**Raises:** `ProtocolError` — bad `conn_id`, empty host, unsafe host chars
(`:`/`<`/`>`), consecutive dots in hostname, invalid hostname structure, port
out of range `[1, 65535]`, or encoded frame exceeds `MAX_FRAME_LEN`.

```python
# IPv4
frame = encode_conn_open_frame(new_conn_id(), "10.0.0.5", 5432)

# Domain (Kubernetes service)
frame = encode_conn_open_frame(new_conn_id(), "postgres.default.svc", 5432)

# IPv6 — bracket-quoting and compression are handled automatically
frame = encode_conn_open_frame(new_conn_id(), "2001:db8::1", 443)
# wire: <<<EXECTUNNEL:CONN_OPEN:c...:[2001:db8::1]:443>>>
```

---

### `encode_conn_ack_frame(conn_id: str) → str`

Encodes a `CONN_ACK` frame acknowledging a `CONN_OPEN` request.

Sent by the **agent** once the target TCP connection is established. The client
uses the echoed `conn_id` to resolve the pending-connect future and begin
sending `DATA` frames.

**Arguments:**

| Argument  | Type  | Constraints                                                          |
|-----------|-------|----------------------------------------------------------------------|
| `conn_id` | `str` | Must match `[cu][0-9a-f]{24}` — echoed from the original `CONN_OPEN` |

**Raises:** `ProtocolError` — bad `conn_id`.

**Important:** The client must not send `DATA` frames for a connection until
`CONN_ACK` is received. Sending data before `CONN_ACK` may arrive at the agent
before the socket is ready.

```python
# Agent side — after successfully connecting to the target host
sock = socket.create_connection((host, port))
frame = encode_conn_ack_frame(conn_id)
sys.stdout.write(frame)
sys.stdout.flush()
```

---

### `encode_conn_close_frame(conn_id: str) → str`

Encodes a `CONN_CLOSE` frame signalling explicit TCP teardown.

**Arguments:**

| Argument | Type | Constraints |
|---|---|---|
| `conn_id` | `str` | Must match `[cu][0-9a-f]{24}` |

**Raises:** `ProtocolError` — bad `conn_id`.

**Important:** Always emit this frame when closing a connection. The agent
releases its socket and connection-table entry only on receiving `CONN_CLOSE`.
Omitting it causes resource leaks on the agent side that only resolve on
timeout.

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

**Raises:** `ProtocolError` — bad `conn_id`, or base64url-encoded payload causes
frame to exceed `MAX_FRAME_LEN`.

**Maximum safe `data` size:** 6,108 bytes per frame. Larger chunks must be split
by the caller before encoding.

```python
CHUNK = 4096   # kept in transport/session layer config, not here

while chunk := reader.read(CHUNK):
    frame = encode_data_frame(conn_id, chunk)
    await ws.send(frame)
```

> **Why 6,108 bytes?** Base64url expands 3 bytes → 4 chars. The frame envelope
> consumes 48 chars (`FRAME_PREFIX` + `msg_type` + separators + `conn_id` +
> `FRAME_SUFFIX`). Available payload chars: `8192 − 48 = 8144`. Max raw bytes:
> `⌊8144 × 3/4⌋ = 6,108`.

---

### `encode_udp_open_frame(flow_id: str, host: str, port: int) → str`

Encodes a `UDP_OPEN` frame instructing the agent to open a UDP flow.

Same argument constraints and error behaviour as `encode_conn_open_frame`. Use
`new_flow_id()` (not `new_conn_id()`) for the first argument.

```python
flow_id = new_flow_id()
frame   = encode_udp_open_frame(flow_id, "8.8.8.8", 53)
await ws.send(frame)
```

---

### `encode_udp_data_frame(flow_id: str, data: bytes) → str`

Encodes a `UDP_DATA` frame carrying a UDP datagram.

Same argument constraints and error behaviour as `encode_data_frame`. The `data`
argument should be a **complete datagram** — UDP framing is the caller's
responsibility. UDP datagrams are never split; a datagram larger than 6,108
bytes raises `ProtocolError`. The proxy layer must enforce MTU limits before
calling this function.

```python
datagram = build_dns_query("example.com")
frame    = encode_udp_data_frame(flow_id, datagram)
await ws.send(frame)
```

---

### `encode_udp_close_frame(flow_id: str) → str`

Encodes a `UDP_CLOSE` frame signalling explicit UDP flow teardown.

Same argument constraints and error behaviour as `encode_conn_close_frame`.

`UDP_CLOSE` is an **advisory close with no handshake**. The protocol makes no
ordering guarantee between a `UDP_CLOSE` frame and `UDP_DATA` frames already in
flight. The session layer must silently discard `UDP_DATA` frames received after
`UDP_CLOSE` rather than treating them as errors.

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

**Raises:** `ProtocolError` — bad `conn_id`, or encoded frame exceeds
`MAX_FRAME_LEN`.

```python
# Per-connection error
frame = encode_error_frame(conn_id, "Connection refused by target")
await ws.send(frame)

# Session-level error (affects all connections)
frame = encode_error_frame(SESSION_CONN_ID, "Agent OOM — shutting down")
await ws.send(frame)
```

---

### `encode_keepalive_frame() → str`

Encodes a `KEEPALIVE` frame — a session-level heartbeat sent client → agent.

The frame carries no `conn_id` and no payload. The agent silently discards it
to confirm the WebSocket channel is still alive.

**Arguments:** None.

**Raises:** Nothing. This encoder accepts no arguments and always produces a
structurally valid, length-safe frame.

```python
# Periodic heartbeat loop (session layer)
async def _keepalive_loop(ws, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        await ws.send(encode_keepalive_frame())
```

---

### Common Encoder Mistakes

```python
# ✗ wrong — hand-crafted ID bypasses validation until the encoder catches it
frame = encode_data_frame("myconn", data)
# → ProtocolError: Invalid tunnel DATA ID 'myconn': must match [cu][0-9a-f]{24}

# ✗ wrong — session ID passed as conn_id — s-prefix rejected by CONN_FLOW_ID_RE
frame = encode_conn_open_frame(new_session_id(), "redis", 6379)
# → ProtocolError: Invalid tunnel CONN_OPEN ID 's...': must match [cu][0-9a-f]{24}

# ✗ wrong — port 0 is rejected (not a valid destination port for OPEN frames)
frame = encode_conn_open_frame(conn_id, "redis", 0)
# → ProtocolError: Port 0 is out of range [1, 65535]

# ✗ wrong — colon in hostname would corrupt the frame
frame = encode_conn_open_frame(conn_id, "bad:host", 80)
# → ProtocolError: Host 'bad:host' contains frame-unsafe characters

# ✗ wrong — consecutive dots are not a valid hostname
frame = encode_conn_open_frame(conn_id, "redis..default", 6379)
# → ProtocolError: Host 'redis..default' contains consecutive dots

# ✓ correct — IPv6 passed as a plain string, bracket-quoting is automatic
frame = encode_conn_open_frame(conn_id, "::1", 8080)
# wire payload: [::1]:8080

# ✗ wrong — AGENT_READY is a constant emitted by the agent, never by the client
# There is no encode_agent_ready_frame() function — use READY_FRAME directly
sys.stdout.write(READY_FRAME + "\n")

# ✓ correct — keepalive takes no arguments
frame = encode_keepalive_frame()  # always succeeds
```

---

## Section 3 — Frame Decoder

### `parse_frame(line: str) → ParsedFrame | None`

Parses one line of text from the tunnel channel into a structured frame.

**Argument:** `line` — a single line from the WebSocket channel, with or without
a trailing newline. Leading/trailing whitespace is stripped before parsing.
Proxy-injected suffixes after `>>>` are silently stripped before validation.

**Returns:**

| Return value | Meaning | Action |
|---|---|---|
| `None` | Line is not a tunnel frame (shell noise, blank line, bootstrap stdout) | Ignore silently |
| `ParsedFrame` | Structurally valid, recognised tunnel frame | Dispatch on `msg_type` |

**Raises:** `FrameDecodingError` — the line **is** a tunnel frame (has
`FRAME_PREFIX` + `FRAME_SUFFIX`) but its internal structure is corrupt. This is
a protocol violation from the remote peer. Do **not** catch and ignore —
propagate upward.

**Check order (load-bearing):**

1. Strip whitespace; strip proxy-injected suffix if present
2. Check for `FRAME_PREFIX` + `FRAME_SUFFIX` — if absent, return `None`
   regardless of line length
3. Check length against `MAX_FRAME_LEN` — oversized tunnel frames raise
   `FrameDecodingError`; oversized non-frame lines are logged at DEBUG and
   return `None`
4. Parse `msg_type`, `conn_id`, `payload` via `split(":", 2)`
5. Validate `msg_type` against `_VALID_MSG_TYPES`
6. Enforce `_NO_CONN_ID_TYPES` frames carry no extra fields
7. Validate `conn_id` format against `CONN_FLOW_ID_RE`

**Transport contract:** expects a single complete line. The transport layer is
responsible for buffering the byte stream and splitting on `\n`. Passing a raw
WebSocket message containing multiple newline-separated frames will silently
misparse.

```python
# Canonical inbound dispatch loop (transport / session layer)
async for message in websocket:
    for line in message.splitlines():
        try:
            frame = parse_frame(line)
        except FrameDecodingError as exc:
            log.error("corrupt tunnel frame", extra={"error": exc.to_dict()})
            raise ConnectionClosedError(
                "Remote peer sent a corrupt tunnel frame",
                details={"close_code": 1002, "close_reason": exc.message},
            ) from exc

        if frame is None:
            continue  # shell noise — normal during bootstrap

        match frame.msg_type:
            case "DATA":
                data = decode_binary_payload(frame.payload)
                await connections[frame.conn_id].write(data)
            case "CONN_ACK":
                pending_connects[frame.conn_id].set_result(None)
            case "CONN_CLOSE":
                await _close_connection(frame.conn_id)
            case "AGENT_READY":
                session.mark_ready()
            case "KEEPALIVE":
                pass  # client→agent only; agent discards silently
            case "ERROR":
                msg = decode_error_payload(frame.payload)
                await _handle_agent_error(frame.conn_id, msg)
            case "UDP_DATA":
                data = decode_binary_payload(frame.payload)
                await udp_flows[frame.conn_id].sendto(data)
            case "UDP_CLOSE":
                await _close_udp_flow(frame.conn_id)
            case _:
                # parse_frame already validated msg_type — this branch
                # should never be reached. Raise rather than silently drop.
                raise UnexpectedFrameError(
                    f"Unhandled msg_type {frame.msg_type!r} in dispatch",
                    details={
                        "state": session.state,
                        "frame_type": frame.msg_type,
                    },
                )
```

---

### `is_ready_frame(line: str) → bool`

Returns `True` if `line` is the `AGENT_READY` bootstrap sentinel.

**Argument:** `line` — a single line from the WebSocket channel.

**Returns:** `True` only for `<<<EXECTUNNEL:AGENT_READY>>>` (after whitespace
stripping and proxy-suffix truncation). `False` for everything else.

**Raises:** Nothing. This is a **pure string predicate** — it never raises.

**Design note:** `is_ready_frame` does not call `parse_frame` internally. The
bootstrap scanner must be maximally tolerant of garbage lines, including lines
that accidentally carry the tunnel prefix/suffix. Raising during the pre-ready
scan would be incorrect. The decision to propagate or log-and-skip
`FrameDecodingError` during bootstrap belongs to the **bootstrap layer**, not
to this function.

**When to use:** In the bootstrap loop, before the session is established. Once
`is_ready_frame` returns `True`, switch to `parse_frame` for all subsequent
lines.

```python
# Bootstrap loop (session / transport layer)
async with asyncio.timeout(AGENT_READY_TIMEOUT):
    async for line in websocket:
        if is_ready_frame(line):
            break
        # Optionally detect early protocol faults during bootstrap:
        try:
            parse_frame(line)
        except FrameDecodingError as exc:
            raise BootstrapError(
                "Agent sent a corrupt frame during bootstrap",
                details={
                    "host": pod_host,
                    "elapsed_s": elapsed(),
                },
            ) from exc
        # Non-tunnel lines during bootstrap are normal — log at DEBUG only
        log.debug("bootstrap noise: %r", line)
```

---

### `ParsedFrame`

The structured result of `parse_frame`.

```python
@dataclass(frozen=True, slots=True)
class ParsedFrame:
    msg_type: str  # one of the 10 valid frame types
    conn_id: str | None  # "[cu][0-9a-f]{24}", or None for AGENT_READY / KEEPALIVE
    payload:  str        # raw payload string or "" when absent
```

**Immutable** — `frozen=True` prevents accidental mutation after parsing.

**`conn_id` is `str | None`** — it is `None` if and only if `msg_type` is in
`_NO_CONN_ID_TYPES` (`"AGENT_READY"` or `"KEEPALIVE"`). Type checkers correctly
flag missing `None`-checks and will not produce false positives on
`if frame.conn_id is None` guards.

**`payload` is always raw** — it is never decoded by `parse_frame`. Call the
appropriate payload helper based on `msg_type`:

| `msg_type`    | `conn_id` | Payload helper                         |
|---------------|-----------|----------------------------------------|
| `AGENT_READY` | `None`    | — (no payload)                         |
| `KEEPALIVE`   | `None`    | — (no payload)                         |
| `CONN_OPEN`   | `str`     | `parse_host_port(frame.payload)`       |
| `CONN_ACK`    | `str`     | — (no payload)                         |
| `CONN_CLOSE`  | `str`     | — (no payload)                         |
| `DATA`        | `str`     | `decode_binary_payload(frame.payload)` |
| `UDP_OPEN`    | `str`     | `parse_host_port(frame.payload)`       |
| `UDP_DATA`    | `str`     | `decode_binary_payload(frame.payload)` |
| `UDP_CLOSE`   | `str`     | — (no payload)                         |
| `ERROR`       | `str`     | `decode_error_payload(frame.payload)`  |

---

## Section 4 — Payload Helpers

### `decode_binary_payload(payload: str) → bytes`

Decodes the base64url (no-padding) payload of a `DATA` or `UDP_DATA` frame.

**Argument:** `payload` — the `ParsedFrame.payload` string from a `DATA` or
`UDP_DATA` frame.

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

Decodes the base64url UTF-8 payload of an `ERROR` frame into a human-readable
string.

**Argument:** `payload` — the `ParsedFrame.payload` string from an `ERROR`
frame.

**Returns:** Decoded UTF-8 error message string.

**Raises:** `FrameDecodingError` — `payload` is not valid base64url, or the
decoded bytes are not valid UTF-8. The original exception is chained via
`raise ... from exc`.

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
        # Even if we cannot decode the message, we know an error occurred
        msg = f"<undecodable error payload: {exc.details.get('raw_bytes', '?')}>"
    await _handle_agent_error(frame.conn_id, msg)
```

---

### `encode_host_port(host: str, port: int) → str`

Encodes a host + port into the canonical wire payload string for `CONN_OPEN` /
`UDP_OPEN` frames.

**You rarely need this directly.** `encode_conn_open_frame` and
`encode_udp_open_frame` call it internally. Use it only when building a custom
OPEN payload outside the standard encoders.

**Returns:**

* IPv6: `"[2001:db8::1]:8080"` (bracket-quoted, compressed form)
* IPv4: `"192.168.1.1:8080"`
* Domain: `"redis.default.svc:6379"`

**Port range:** `[1, 65535]`. Port `0` is rejected — it is not a valid
destination port for `OPEN` frames. The `PORT_UNSPECIFIED` constant (`0`) is
provided for the separate use-case where port `0` is the RFC 1928 §6
"unspecified" sentinel in `build_socks5_reply` (proxy layer only).

**Raises:** `ProtocolError` — empty host, port out of range, frame-unsafe
characters (`:`/`<`/`>`) in host, consecutive dots in hostname, or invalid
hostname structure.

---

### `parse_host_port(payload: str) → tuple[str, int]`

Parses a `[host]:port` or `host:port` payload string from a `CONN_OPEN` or
`UDP_OPEN` frame.

**Argument:** `payload` — the `ParsedFrame.payload` string from a `CONN_OPEN`
or `UDP_OPEN` frame.

**Returns:** `(host, port)` tuple where `host` is a plain string (no brackets)
and `port` is an integer in `[1, 65535]`.

**Port range:** `[1, 65535]`. Port `0` is rejected for the same reason as in
`encode_host_port`.

**Raises:** `FrameDecodingError` — malformed payload, empty host, non-numeric
port, or port out of range.

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
# For domain names: host_out == host_in exactly
```

---

## Section 5 — SOCKS5 Enumerations

All enums are `IntEnum` — they compare equal to their integer wire values and
can be used directly in `struct.pack` / byte comparisons.

### Enum Hierarchy

`AuthMethod`, `Cmd`, `AddrType`, and `Reply` inherit from `_StrictIntEnum`, a
private base that provides a shared `_missing_` raising `ValueError` immediately
on unknown wire values. `UserPassStatus` inherits directly from `IntEnum`
because RFC 1929 §2 requires a **permissive** `_missing_` that maps any non-zero
byte to `FAILURE`.

```python
# _StrictIntEnum behaviour (AuthMethod, Cmd, AddrType, Reply):
AuthMethod(0x99)
# → ValueError: 0x99 is not a valid AuthMethod (expected one of [0, 1, 2, 255])

# UserPassStatus permissive mapping (RFC 1929 §2):
UserPassStatus(0x01)  # → UserPassStatus.FAILURE  (any non-zero byte)
UserPassStatus(0xFE)  # → UserPassStatus.FAILURE
UserPassStatus(0x00)  # → UserPassStatus.SUCCESS
```

---

### `AuthMethod`

RFC 1928 §3 — authentication method negotiation.

This tunnel accepts **only `NO_AUTH`**. Both `GSSAPI` and `USERNAME_PASSWORD`
are defined for wire-format completeness and their `is_supported()` returns
`False`. A peer that offers only unsupported methods receives `NO_ACCEPT`.

| Member              | Value  | `is_supported()` | Notes                                        |
|---------------------|--------|------------------|----------------------------------------------|
| `NO_AUTH`           | `0x00` | `True`           | Only accepted tunnel auth method             |
| `GSSAPI`            | `0x01` | `False`          | Wire-only — never negotiated                 |
| `USERNAME_PASSWORD` | `0x02` | `False`          | Wire-only — never negotiated                 |
| `NO_ACCEPT`         | `0xFF` | `True`           | Sent by server to reject all offered methods |

Use `is_supported()` to programmatically check whether a method is implemented
before attempting negotiation:

```python
# Proxy layer — method selection (tunnel only negotiates NO_AUTH)
offered: set[AuthMethod] = set()
for b in offered_bytes:
    try:
        offered.add(AuthMethod(b))
    except ValueError:
        pass  # unknown method bytes are ignored per RFC 1928 §3

if AuthMethod.NO_AUTH in offered:
    chosen = AuthMethod.NO_AUTH
else:
    chosen = AuthMethod.NO_ACCEPT

writer.write(bytes([0x05, int(chosen)]))
```

---

### `Cmd`

RFC 1928 §4 — client command codes.

| Member          | Value  | `is_supported()` | Notes                       |
|-----------------|--------|------------------|-----------------------------|
| `CONNECT`       | `0x01` | `True`           | TCP tunnel connection       |
| `BIND`          | `0x02` | `False`          | Wire-only — not implemented |
| `UDP_ASSOCIATE` | `0x03` | `True`           | UDP tunnel flow             |

Use `is_supported()` to guard dispatch before the `match` statement:

```python
try:
    cmd = Cmd(request_bytes[1])
except ValueError:
    writer.write(_reply_bytes(Reply.CMD_NOT_SUPPORTED))
    return

if not cmd.is_supported():
    writer.write(_reply_bytes(Reply.CMD_NOT_SUPPORTED))
    return

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
        # is_supported() already caught this — unreachable
        writer.write(_reply_bytes(Reply.CMD_NOT_SUPPORTED))
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
try:
    atyp = AddrType(request_bytes[3])
except ValueError:
    writer.write(_reply_bytes(Reply.ADDR_NOT_SUPPORTED))
    return

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
def _reply_bytes(
    reply: Reply,
    bind_addr: str = "0.0.0.0",
        bind_port: int = PORT_UNSPECIFIED,  # 0 valid here — RFC 1928 §6 "unspecified"
) -> bytes:
    addr_bytes = ipaddress.IPv4Address(bind_addr).packed
    return bytes([
        0x05,           # SOCKS version
        int(reply),     # reply code
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

> **RFC 1929 §2 note:** Any non-zero status byte indicates failure, not just
> `0xFF`. `UserPassStatus._missing_` maps any value in `[0x01, 0xFE]` to
> `FAILURE` so that non-standard peers are handled correctly rather than raising
> `ValueError`. This is why `UserPassStatus` inherits plain `IntEnum` rather
> than `_StrictIntEnum`.

```python
if await _verify_credentials(username, password):
    writer.write(bytes([0x01, int(UserPassStatus.SUCCESS)]))
else:
    writer.write(bytes([0x01, int(UserPassStatus.FAILURE)]))
    raise AuthenticationError(
        "SOCKS5 username/password authentication failed",
        details={"host": peer_addr, "auth_method": "username_password"},
    )
```

---

### `_missing_` Behaviour Summary

The proxy layer must catch `ValueError` from `_StrictIntEnum` subclasses and
map it to the appropriate SOCKS5 reply:

```python
try:
    cmd = Cmd(request_bytes[1])
except ValueError:
    writer.write(_reply_bytes(Reply.CMD_NOT_SUPPORTED))
    return

try:
    atyp = AddrType(request_bytes[3])
except ValueError:
    writer.write(_reply_bytes(Reply.ADDR_NOT_SUPPORTED))
    return
```

---

## Section 6 — Constants

### Frame Delimiters

| Constant        | Value                            | Use                                                     |
|-----------------|----------------------------------|---------------------------------------------------------|
| `FRAME_PREFIX`  | `"<<<EXECTUNNEL:"`               | Start sentinel                                          |
| `FRAME_SUFFIX`  | `">>>"`                          | End sentinel                                            |
| `READY_FRAME`   | `"<<<EXECTUNNEL:AGENT_READY>>>"` | Use `is_ready_frame()` — do not string-compare manually |
| `MAX_FRAME_LEN` | `8_192`                          | Maximum frame content length in chars, excluding `\n`   |

### Sentinel Values

| Constant           | Value            | Use                                                                                                                            |
|--------------------|------------------|--------------------------------------------------------------------------------------------------------------------------------|
| `SESSION_CONN_ID`  | `"c" + "0" × 24` | Pass to `encode_error_frame` for session-level errors; check with `frame.conn_id == SESSION_CONN_ID`                           |
| `PORT_UNSPECIFIED` | `0`              | RFC 1928 §6 "unspecified" bind port in `_reply_bytes` (proxy layer); **never** pass to `encode_host_port` or `parse_host_port` |

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
from exectunnel.protocol import (
    parse_frame, is_ready_frame,
    decode_binary_payload, decode_error_payload,
)

# ── Inbound (decoding) ────────────────────────────────────────────────────────

async def handle_inbound(line: str) -> None:
    try:
        frame = parse_frame(line)
    except FrameDecodingError as exc:
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
                if is_ready_frame(line):
                    return
                # Optionally detect early protocol faults:
                try:
                    parse_frame(line)
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

# ✗ wrong — is_ready_frame is a pure predicate, wrapping it in
#   try/except FrameDecodingError is unnecessary and misleading
try:
    if is_ready_frame(line):  # never raises
        break
except FrameDecodingError:
    ...  # WRONG — dead code

# ✓ correct — call parse_frame separately if you want fault detection
if is_ready_frame(line):
    break
try:
    parse_frame(line)
except FrameDecodingError as exc:
    ...

# ✗ wrong — passing a session ID as conn_id
frame = encode_conn_open_frame(new_session_id(), "redis", 6379)
# → ProtocolError: s-prefix rejected by CONN_FLOW_ID_RE

# ✗ wrong — passing PORT_UNSPECIFIED to encode_host_port
encode_host_port("redis", PORT_UNSPECIFIED)
# → ProtocolError: Port 0 is out of range [1, 65535]
# Use PORT_UNSPECIFIED only in _reply_bytes at the proxy layer
```

---

## Section 8 — Integration Checklist

```
IDs
  □  Call new_conn_id() once per TCP connection — never reuse IDs
  □  Call new_flow_id() once per UDP flow — never reuse IDs
  □  Call new_session_id() once per tunnel session — for logging/tracing only
  □  Store conn/flow IDs as dict keys — they are hashable strings
  □  Use SESSION_CONN_ID only for session-level encode_error_frame calls
  □  Never store SESSION_CONN_ID in the connection or flow table
  □  Never pass a session ID (s-prefix) to any frame encoder
  □  Use CONN_FLOW_ID_RE for new code; ID_RE is a backward-compat alias

ENCODING
  □  Never construct frame strings manually — always use encode_*_frame()
  □  Never construct IDs manually — always use new_conn_id() / new_flow_id()
  □  Always emit CONN_CLOSE / UDP_CLOSE on teardown — never rely on timeout alone
  □  Split data > 6,108 bytes before calling encode_data_frame()
  □  Never split UDP datagrams — enforce MTU limits before encode_udp_data_frame()
  □  Do not send DATA frames for a conn_id until CONN_ACK is received
  □  Emit encode_keepalive_frame() periodically to keep the channel alive
  □  Never pass PORT_UNSPECIFIED to encode_conn_open_frame or encode_udp_open_frame

DECODING
  □  Call parse_frame() on every inbound line
  □  Treat None return as normal — do not log at WARNING or above
  □  Treat FrameDecodingError as a protocol violation — always propagate
  □  Always call the correct payload helper for each msg_type
  □  Always check frame.conn_id == SESSION_CONN_ID in ERROR handler
  □  Never pass a multi-line string to parse_frame() — split on \n first
  □  frame.conn_id is None for AGENT_READY and KEEPALIVE — guard before use

BOOTSTRAP
  □  Use is_ready_frame() in the pre-ready scan — it never raises
  □  Call parse_frame() separately if you want early fault detection
  □  The bootstrap layer owns the FrameDecodingError policy during pre-ready scan

EXCEPTIONS
  □  Always chain: raise XxxError(...) from exc
  □  Never match on exc.message strings — use exc.error_code or class
  □  Always include details= dict when raising ExecTunnelError subclasses
  □  Use exc.to_dict() for structured logging

SOCKS5 ENUMS
  □  Wrap Cmd(...) / AddrType(...) / AuthMethod(...) in try/except ValueError
  □  Map ValueError from _missing_ to the appropriate Reply code
  □  Never use raw integer literals where an enum member exists
  □  Note: UserPassStatus never raises ValueError for valid byte values
  □  Note: only NO_AUTH is negotiated by this tunnel — USERNAME_PASSWORD
     and GSSAPI are wire-only and their is_supported() returns False
```

---

## Section 9 — Complete Layer Integration Examples

### 9.1 Transport Layer — Inbound Frame Pump

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

FrameCallback = Callable[[ParsedFrame], Awaitable[None]]


async def run_frame_pump(
        websocket,
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
                        "url": ws_url,
                    },
                ) from exc

            if frame is None:
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
        websocket,
    frame: str,
    *,
    timeout_s: float,
    ws_host: str,
    ws_port: int,
    ws_url: str,
) -> None:
    """Send a pre-encoded *frame* string over *websocket*.

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
    """Dispatches inbound :class:`ParsedFrame` objects to the correct handler."""

    def __init__(
        self,
        connections: dict,
        udp_flows: dict,
            pending_connects: dict,
        state: str,
    ) -> None:
        self._connections = connections
        self._udp_flows = udp_flows
        self._pending_connects = pending_connects
        self._state = state

    async def dispatch(self, frame: ParsedFrame) -> None:
        match frame.msg_type:

            case "DATA":
                await self._handle_data(frame)

            case "UDP_DATA":
                await self._handle_udp_data(frame)

            case "CONN_ACK":
                # Resolve the pending-connect future so the client can
                # begin sending DATA frames.
                fut = self._pending_connects.pop(frame.conn_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(None)
                else:
                    log.debug(
                        "CONN_ACK for unknown/already-resolved conn %.8s… — ignoring",
                        frame.conn_id,
                    )

            case "CONN_CLOSE":
                await self._handle_conn_close(frame)

            case "UDP_CLOSE":
                await self._handle_udp_close(frame)

            case "ERROR":
                await self._handle_error(frame)

            case "KEEPALIVE":
                # KEEPALIVE is client→agent only; the agent silently discards it.
                # Receiving it from the agent is unexpected but harmless — log only.
                log.debug("received unexpected inbound KEEPALIVE — ignoring")

            case "AGENT_READY":
                raise UnexpectedFrameError(
                    "Received AGENT_READY after session was already established.",
                    details={
                        "state":      self._state,
                        "frame_type": frame.msg_type,
                    },
                )

            case "CONN_OPEN" | "UDP_OPEN":
                raise UnexpectedFrameError(
                    f"Agent sent a client-only frame type: {frame.msg_type!r}.",
                    details={
                        "state":      self._state,
                        "frame_type": frame.msg_type,
                    },
                )

            case _:
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
            log.warning("DATA for unknown conn_id %.8s… — dropping", frame.conn_id)
            return
        try:
            data = decode_binary_payload(frame.payload)
        except FrameDecodingError as exc:
            log.error(
                "corrupt DATA payload for conn %.8s…",
                frame.conn_id,
                extra={"error": exc.to_dict()},
            )
            await self._close_connection(frame.conn_id)
            return
        writer.write(data)
        await writer.drain()

    async def _handle_udp_data(self, frame: ParsedFrame) -> None:
        flow = self._udp_flows.get(frame.conn_id)
        if flow is None:
            log.debug(
                "UDP_DATA for unknown/closed flow_id %.8s… — dropping", frame.conn_id
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
            log.error("agent reported session-level error: %s", message)
            raise ConnectionClosedError(
                f"Agent reported a session-level error: {message}",
                details={"close_code": 1011, "close_reason": message},
            )

        log.warning(
            "agent reported error for conn %.8s…: %s", frame.conn_id, message
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
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from exectunnel.protocol import (
    SESSION_CONN_ID,
    encode_conn_ack_frame,
    encode_conn_close_frame,
    encode_conn_open_frame,
    encode_data_frame,
    encode_error_frame,
    encode_keepalive_frame,
    encode_udp_close_frame,
    encode_udp_data_frame,
    encode_udp_open_frame,
    new_conn_id,
    new_flow_id,
)

log = logging.getLogger(__name__)

SendCallback = Callable[[str], Awaitable[None]]

# Maximum raw bytes per DATA frame — derived from MAX_FRAME_LEN budget (§4.4).
_MAX_DATA_BYTES: int = 6_108


class OutboundFrameBuilder:
    """Builds and sends outbound frames on behalf of the session layer."""

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

    async def ack_connection(self, conn_id: str) -> None:
        """Acknowledge a CONN_OPEN from the client (agent side)."""
        frame = encode_conn_ack_frame(conn_id)
        log.debug("session: CONN_ACK   %.8s…", conn_id)
        await self._send(frame)

    async def close_connection(self, conn_id: str) -> None:
        """Close an existing TCP connection through the tunnel."""
        frame = encode_conn_close_frame(conn_id)
        log.debug("session: CONN_CLOSE %.8s…", conn_id)
        await self._send(frame)

    async def send_data(self, conn_id: str, data: bytes) -> None:
        """Send *data* over an existing TCP connection.

        Automatically splits *data* into chunks that fit within MAX_FRAME_LEN.
        """
        for offset in range(0, max(len(data), 1), _MAX_DATA_BYTES):
            chunk = data[offset : offset + _MAX_DATA_BYTES]
            frame = encode_data_frame(conn_id, chunk)
            log.debug("session: DATA       %.8s…  %d bytes", conn_id, len(chunk))
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
            ``_MAX_DATA_BYTES`` raises ``ProtocolError``. The proxy layer
            must enforce MTU limits before calling this method.
        """
        frame = encode_udp_data_frame(flow_id, data)
        log.debug("session: UDP_DATA   %.8s…  %d bytes", flow_id, len(data))
        await self._send(frame)

    # ── Session heartbeat ─────────────────────────────────────────────────────

    async def send_keepalive(self) -> None:
        """Send a KEEPALIVE heartbeat to the agent."""
        frame = encode_keepalive_frame()
        log.debug("session: KEEPALIVE")
        await self._send(frame)

    # ── Errors ────────────────────────────────────────────────────────────────

    async def send_error(self, conn_id: str, message: str) -> None:
        """Send an ERROR frame for a specific connection or the whole session."""
        frame = encode_error_frame(conn_id, message)
        log.debug("session: ERROR      %.8s…  %r", conn_id, message[:60])
        await self._send(frame)

    async def send_session_error(self, message: str) -> None:
        """Convenience wrapper — send a session-level ERROR frame."""
        await self.send_error(SESSION_CONN_ID, message)
```

---

### 9.5 Proxy Layer — SOCKS5 Request Parser

```python
"""
proxy/socks5.py — SOCKS5 request parser

Note on auth:
    This tunnel negotiates only NO_AUTH with remote agents.
    The `require_auth` parameter here controls optional local-proxy
    authentication — credentials are validated between the SOCKS5 client
    and this proxy server only, independent of the tunnel protocol.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging

from exectunnel.exceptions import AuthenticationError
from exectunnel.protocol import (
    PORT_UNSPECIFIED,
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
        bind_port: int = PORT_UNSPECIFIED,  # RFC 1928 §6 "unspecified"
) -> bytes:
    addr = ipaddress.IPv4Address(bind_host).packed
    return bytes([
        _SOCKS_VERSION,
        int(reply),
        0x00,
        int(AddrType.IPV4),
        *addr,
        *bind_port.to_bytes(2),
    ])


async def handle_socks5_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
        outbound,
    *,
    require_auth: bool = False,
    username: str = "",
    password: str = "",
) -> str | None:
    peer = writer.get_extra_info("peername")

    # ── Phase 1: Method negotiation ───────────────────────────────────────────
    header = await reader.readexactly(2)
    if header[0] != _SOCKS_VERSION:
        writer.close()
        return None

    n_methods = header[1]
    method_bytes = await reader.readexactly(n_methods)

    offered: set[AuthMethod] = set()
    for b in method_bytes:
        try:
            offered.add(AuthMethod(b))
        except ValueError:
            pass  # unknown method bytes ignored per RFC 1928 §3

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
                details={"host": str(peer), "auth_method": "username_password"},
            )

    # ── Phase 3: Request ──────────────────────────────────────────────────────
    req_header = await reader.readexactly(4)
    if req_header[0] != _SOCKS_VERSION:
        writer.write(_reply_bytes(Reply.GENERAL_FAILURE))
        await writer.drain()
        writer.close()
        return None

    try:
        cmd = Cmd(req_header[1])
    except ValueError:
        writer.write(_reply_bytes(Reply.CMD_NOT_SUPPORTED))
        await writer.drain()
        writer.close()
        return None

    try:
        atyp = AddrType(req_header[3])
    except ValueError:
        writer.write(_reply_bytes(Reply.ADDR_NOT_SUPPORTED))
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
    if not cmd.is_supported():
        writer.write(_reply_bytes(Reply.CMD_NOT_SUPPORTED))
        await writer.drain()
        writer.close()
        return None

    match cmd:
        case Cmd.CONNECT:
            tunnel_id = await outbound.open_connection(host, port)
            writer.write(_reply_bytes(Reply.SUCCESS))
            await writer.drain()
            log.info(
                "SOCKS5 CONNECT   %.8s… → %s:%d  peer=%s",
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
            # is_supported() already caught this — unreachable
            writer.write(_reply_bytes(Reply.CMD_NOT_SUPPORTED))
            await writer.drain()
            writer.close()
            log.warning("SOCKS5 BIND rejected — not supported  peer=%s", peer)
            return None
```

---

### 9.6 Agent Side — Inbound Frame Handler

```python
"""
agent/handler.py — agent-side inbound frame handler

Responsibilities
────────────────
  - Parse frames arriving on stdin
  - Dispatch CONN_OPEN / UDP_OPEN to raw socket creation
  - Emit CONN_ACK after successful TCP connect
  - Forward DATA / UDP_DATA to the appropriate socket
  - Emit DATA / ERROR / CONN_CLOSE frames back on stdout
  - Silently discard KEEPALIVE frames
  - Use only stdlib — no asyncio, no websockets library
"""

from __future__ import annotations

import logging
import socket
import sys

from exectunnel.exceptions import FrameDecodingError
from exectunnel.protocol import (
    SESSION_CONN_ID,
    ParsedFrame,
    decode_binary_payload,
    encode_conn_ack_frame,
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
        ``FrameDecodingError`` causes an ERROR frame to be emitted and the
        affected connection to be closed — the agent never crashes on a
        single bad frame.
        """
        try:
            frame = parse_frame(line)
        except FrameDecodingError as exc:
            log.error("corrupt frame from client", extra={"error": exc.to_dict()})
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
            case "KEEPALIVE":
                # Session-level heartbeat from client — silently discard.
                log.debug("agent: KEEPALIVE received — discarding")
            case "ERROR":
                log.warning(
                    "client reported error for conn %.8s…", frame.conn_id
                )
                self._close_tcp(frame.conn_id)
            case "CONN_ACK":
                # CONN_ACK is agent→client only; receiving it from the
                # client is a protocol violation — emit a session error.
                _emit_error(
                    SESSION_CONN_ID,
                    f"Unexpected CONN_ACK received from client on agent side",
                )
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
        # Acknowledge the connection so the client can begin sending DATA.
        _emit(encode_conn_ack_frame(frame.conn_id))
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
            # UDP_DATA after UDP_CLOSE is expected — drop silently.
            log.debug(
                "UDP_DATA for unknown/closed flow %.8s… — dropping", frame.conn_id
            )
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

### 10.1 Frame Round-Trip Tests

```python
import ipaddress
import pytest
from exectunnel.protocol import (
    SESSION_CONN_ID,
    decode_binary_payload,
    decode_error_payload,
    encode_conn_ack_frame,
    encode_conn_open_frame,
    encode_data_frame,
    encode_error_frame,
    encode_keepalive_frame,
    encode_udp_open_frame,
    is_ready_frame,
    new_conn_id,
    new_flow_id,
    new_session_id,
    parse_frame,
    parse_host_port,
    CONN_FLOW_ID_RE,
    ID_RE,
    SESSION_ID_RE,
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
    try:
        assert ipaddress.ip_address(decoded_host) == ipaddress.ip_address(host)
    except ValueError:
        assert decoded_host == host  # domain name — exact match


def test_conn_ack_round_trip() -> None:
    conn_id = new_conn_id()
    frame = encode_conn_ack_frame(conn_id)
    parsed = parse_frame(frame.strip())

    assert parsed is not None
    assert parsed.msg_type == "CONN_ACK"
    assert parsed.conn_id == conn_id
    assert parsed.payload == ""


def test_keepalive_round_trip() -> None:
    frame = encode_keepalive_frame()
    parsed = parse_frame(frame.strip())

    assert parsed is not None
    assert parsed.msg_type == "KEEPALIVE"
    assert parsed.conn_id is None
    assert parsed.payload == ""


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


def test_udp_open_round_trip() -> None:
    flow_id = new_flow_id()
    frame = encode_udp_open_frame(flow_id, "8.8.8.8", 53)
    parsed = parse_frame(frame.strip())

    assert parsed is not None
    assert parsed.msg_type == "UDP_OPEN"
    assert parsed.conn_id == flow_id
    host, port = parse_host_port(parsed.payload)
    assert host == "8.8.8.8"
    assert port == 53
```

---

### 10.2 `parse_frame` Boundary Tests

```python
def test_parse_frame_returns_none_for_noise() -> None:
    for line in ["", "bash-5.1$", "  ", "some random output", ">>>", "<<<"]:
        assert parse_frame(line) is None


def test_parse_frame_returns_none_for_oversized_non_frame() -> None:
    # Oversized line with no tunnel markers — must return None, never raise
    assert parse_frame("x" * 9000) is None


def test_parse_frame_raises_on_oversized_tunnel_frame() -> None:
    oversized = "<<<EXECTUNNEL:DATA:c" + "a" * 24 + ":" + "x" * 9000 + ">>>"
    with pytest.raises(FrameDecodingError) as exc_info:
        parse_frame(oversized)
    assert exc_info.value.details["codec"] == "frame"


def test_parse_frame_raises_on_unrecognised_msg_type() -> None:
    with pytest.raises(FrameDecodingError) as exc_info:
        parse_frame("<<<EXECTUNNEL:BADTYPE:ca1b2c3d4e5f6a7b8c9d0e1f2a3b>>>")
    assert exc_info.value.details["codec"] == "frame"


def test_parse_frame_raises_on_malformed_conn_id() -> None:
    with pytest.raises(FrameDecodingError):
        parse_frame("<<<EXECTUNNEL:DATA:NOTANID:abc>>>")


def test_parse_frame_raises_on_extra_fields_for_keepalive() -> None:
    # KEEPALIVE must not carry a conn_id — extra field is a protocol error
    with pytest.raises(FrameDecodingError) as exc_info:
        parse_frame("<<<EXECTUNNEL:KEEPALIVE:ca1b2c3d4e5f6a7b8c9d0e1f2a3b>>>")
    assert exc_info.value.details["codec"] == "frame"


def test_parse_frame_raises_on_extra_fields_for_agent_ready() -> None:
    # AGENT_READY must not carry a conn_id — extra field is a protocol error
    with pytest.raises(FrameDecodingError):
        parse_frame("<<<EXECTUNNEL:AGENT_READY:ca1b2c3d4e5f6a7b8c9d0e1f2a3b>>>")


def test_parse_frame_keepalive_conn_id_is_none() -> None:
    parsed = parse_frame("<<<EXECTUNNEL:KEEPALIVE>>>")
    assert parsed is not None
    assert parsed.conn_id is None
    assert parsed.payload == ""


def test_parse_frame_strips_proxy_suffix() -> None:
    # Proxy-injected metadata appended after >>> must be silently stripped
    line = "<<<EXECTUNNEL:KEEPALIVE>>> proxy-trace-id=abc123"
    parsed = parse_frame(line)
    assert parsed is not None
    assert parsed.msg_type == "KEEPALIVE"


def test_is_ready_frame_never_raises() -> None:
    # Must not raise even for lines that look like corrupt tunnel frames
    for line in [
        "",
        "<<<EXECTUNNEL:BADTYPE>>>",
        "<<<EXECTUNNEL:DATA:BADID:abc>>>",
        "x" * 9000,
        "<<<EXECTUNNEL:AGENT_READY:unexpected_extra>>>",
    ]:
        result = is_ready_frame(line)
        assert isinstance(result, bool)  # no exception raised
```

---

### 10.3 Encoder Validation Tests

```python
def test_encoder_rejects_bad_conn_id() -> None:
    with pytest.raises(ProtocolError) as exc_info:
        encode_data_frame("not-an-id", b"hello")
    assert "frame_type" in exc_info.value.details
    assert "expected" in exc_info.value.details


def test_encoder_rejects_session_id_as_conn_id() -> None:
    # s-prefix IDs must be rejected by all frame encoders
    with pytest.raises(ProtocolError):
        encode_conn_open_frame(new_session_id(), "redis", 6379)


def test_encoder_rejects_port_zero() -> None:
    with pytest.raises(ProtocolError):
        encode_conn_open_frame(new_conn_id(), "redis", 0)


def test_encoder_rejects_port_unspecified_in_open_frame() -> None:
    from exectunnel.protocol import PORT_UNSPECIFIED
    with pytest.raises(ProtocolError):
        encode_conn_open_frame(new_conn_id(), "redis", PORT_UNSPECIFIED)


def test_encoder_rejects_frame_unsafe_host() -> None:
    with pytest.raises(ProtocolError):
        encode_conn_open_frame(new_conn_id(), "bad:host", 80)


def test_encoder_rejects_consecutive_dots() -> None:
    with pytest.raises(ProtocolError):
        encode_conn_open_frame(new_conn_id(), "redis..default", 6379)


def test_encoder_rejects_oversized_data() -> None:
    with pytest.raises(ProtocolError):
        encode_data_frame(new_conn_id(), b"x" * 10_000)


def test_encoder_accepts_session_conn_id_for_error_frame() -> None:
    frame = encode_error_frame(SESSION_CONN_ID, "test session error")
    parsed = parse_frame(frame.strip())
    assert parsed is not None
    assert parsed.conn_id == SESSION_CONN_ID


def test_keepalive_encoder_never_raises() -> None:
    # encode_keepalive_frame() takes no arguments and must always succeed
    frame = encode_keepalive_frame()
    assert frame.endswith("\n")
    parsed = parse_frame(frame.strip())
    assert parsed is not None
    assert parsed.msg_type == "KEEPALIVE"
    assert parsed.conn_id is None


def test_conn_ack_encoder_rejects_bad_conn_id() -> None:
    with pytest.raises(ProtocolError):
        encode_conn_ack_frame("not-an-id")


def test_conn_ack_encoder_rejects_session_id() -> None:
    with pytest.raises(ProtocolError):
        encode_conn_ack_frame(new_session_id())
```

---

### 10.4 ID Generator & Validator Tests

```python
def test_new_conn_id_matches_pattern() -> None:
    for _ in range(100):
        cid = new_conn_id()
        assert CONN_FLOW_ID_RE.match(cid), f"new_conn_id() produced invalid ID: {cid!r}"
        assert cid.startswith("c")


def test_new_flow_id_matches_pattern() -> None:
    for _ in range(100):
        fid = new_flow_id()
        assert CONN_FLOW_ID_RE.match(fid), f"new_flow_id() produced invalid ID: {fid!r}"
        assert fid.startswith("u")


def test_new_session_id_matches_pattern() -> None:
    for _ in range(100):
        sid = new_session_id()
        assert SESSION_ID_RE.match(
            sid), f"new_session_id() produced invalid ID: {sid!r}"
        assert sid.startswith("s")


def test_id_re_is_alias_for_conn_flow_id_re() -> None:
    # ID_RE must be the exact same compiled object as CONN_FLOW_ID_RE
    assert ID_RE is CONN_FLOW_ID_RE


def test_conn_flow_id_re_rejects_session_id() -> None:
    # s-prefix session IDs must not match CONN_FLOW_ID_RE
    assert CONN_FLOW_ID_RE.match(new_session_id()) is None


def test_session_id_re_rejects_conn_id() -> None:
    # c-prefix conn IDs must not match SESSION_ID_RE
    assert SESSION_ID_RE.match(new_conn_id()) is None


def test_session_id_re_rejects_flow_id() -> None:
    # u-prefix flow IDs must not match SESSION_ID_RE
    assert SESSION_ID_RE.match(new_flow_id()) is None


def test_session_conn_id_passes_conn_flow_id_re() -> None:
    assert CONN_FLOW_ID_RE.match(SESSION_CONN_ID) is not None


def test_session_conn_id_not_produced_by_new_conn_id() -> None:
    # Statistical check — none of 10,000 generated IDs should equal the sentinel
    for _ in range(10_000):
        assert new_conn_id() != SESSION_CONN_ID


def test_conn_id_and_flow_id_prefix_namespace_isolation() -> None:
    from exectunnel.protocol.ids import _TCP_PREFIX, _UDP_PREFIX, _SESSION_PREFIX
    assert _TCP_PREFIX != _UDP_PREFIX
    assert _TCP_PREFIX != _SESSION_PREFIX
    assert _UDP_PREFIX != _SESSION_PREFIX


def test_conn_id_and_flow_id_are_unique() -> None:
    ids = {new_conn_id() for _ in range(1_000)}
    ids |= {new_flow_id() for _ in range(1_000)}
    assert len(ids) == 2_000  # no collisions in 2,000 samples
```

---

### 10.5 Exception Contract Tests

```python
def test_frame_decoding_error_is_chained() -> None:
    with pytest.raises(FrameDecodingError) as exc_info:
        decode_binary_payload("!!!not-base64!!!")
    assert exc_info.value.__cause__ is not None


def test_frame_decoding_error_details_keys_base64url() -> None:
    with pytest.raises(FrameDecodingError) as exc_info:
        decode_binary_payload("!!!not-base64!!!")
    details = exc_info.value.details
    assert "raw_bytes" in details
    assert "codec"     in details
    assert details["codec"] == "base64url"


def test_frame_decoding_error_details_keys_host_port() -> None:
    with pytest.raises(FrameDecodingError) as exc_info:
        parse_host_port("no-port-here")
    details = exc_info.value.details
    assert "raw_bytes" in details
    assert "codec" in details
    assert details["codec"] == "host:port"


def test_frame_decoding_error_details_keys_frame() -> None:
    with pytest.raises(FrameDecodingError) as exc_info:
        parse_frame("<<<EXECTUNNEL:BADTYPE:ca1b2c3d4e5f6a7b8c9d0e1f2a3b>>>")
    details = exc_info.value.details
    assert "raw_bytes" in details
    assert "codec" in details
    assert details["codec"] == "frame"


def test_protocol_error_details_keys() -> None:
    with pytest.raises(ProtocolError) as exc_info:
        encode_conn_open_frame(new_conn_id(), "", 80)
    details = exc_info.value.details
    assert "frame_type" in details
    assert "expected"   in details


def test_decode_error_payload_chains_utf8_error() -> None:
    # Encode raw non-UTF-8 bytes as base64url, then try to decode as error payload
    import base64
    bad_utf8 = b"\xff\xfe"
    bad_payload = base64.urlsafe_b64encode(bad_utf8).rstrip(b"=").decode("ascii")
    with pytest.raises(FrameDecodingError) as exc_info:
        decode_error_payload(bad_payload)
    assert exc_info.value.__cause__ is not None
    assert exc_info.value.details["codec"] == "utf-8"


def test_parse_host_port_rejects_port_zero() -> None:
    with pytest.raises(FrameDecodingError):
        parse_host_port("redis:0")


def test_parse_host_port_rejects_port_above_range() -> None:
    with pytest.raises(FrameDecodingError):
        parse_host_port("redis:65536")


def test_parse_host_port_ipv6_round_trip() -> None:
    host, port = parse_host_port("[2001:db8::1]:443")
    assert host == "2001:db8::1"
    assert port == 443


def test_parse_host_port_rejects_malformed_bracket() -> None:
    with pytest.raises(FrameDecodingError):
        parse_host_port("[2001:db8::1:443")  # missing closing bracket
```

---

## Summary of Changes from v1.1

| Section                        | Change                                                                                                                                                                                                                                                                                            |
|--------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Imports block                  | Added `PORT_UNSPECIFIED`, `encode_conn_ack_frame`, `encode_keepalive_frame`, `new_session_id`, `CONN_FLOW_ID_RE`, `SESSION_ID_RE`                                                                                                                                                                 |
| Quick-Reference Card           | Added `new_session_id`, `CONN_FLOW_ID_RE`, `SESSION_ID_RE`, `encode_conn_ack_frame`, `encode_keepalive_frame`; annotated `encode_keepalive_frame` as never raises                                                                                                                                 |
| §1 `new_session_id`            | New subsection — purpose, return format, raises nothing, must not be passed to frame encoders                                                                                                                                                                                                     |
| §1 ID Format Contract          | Added `s`-prefix row; documented `CONN_FLOW_ID_RE` as canonical and `ID_RE` as alias; added session ID misuse example                                                                                                                                                                             |
| §2 `encode_conn_ack_frame`     | New subsection — purpose, CONN_ACK ordering note, agent-side example                                                                                                                                                                                                                              |
| §2 `encode_keepalive_frame`    | New subsection — purpose, never-raises contract, keepalive loop example                                                                                                                                                                                                                           |
| §2 Common Encoder Mistakes     | Added session-ID-as-conn_id example; added `PORT_UNSPECIFIED` misuse example; added `AGENT_READY` note                                                                                                                                                                                            |
| §3 `parse_frame` check order   | Step 6 (no-conn_id enforcement) and step 7 (conn_id validation) separated; proxy-suffix note added                                                                                                                                                                                                |
| §3 dispatch example            | Added `CONN_ACK` and `KEEPALIVE` `case` branches                                                                                                                                                                                                                                                  |
| §3 `ParsedFrame` intro         | Updated "one of the 10 valid frame types"; updated `conn_id` note to cover `AGENT_READY` and `KEEPALIVE`                                                                                                                                                                                          |
| §3 `ParsedFrame` payload table | Added `KEEPALIVE` and `CONN_ACK` rows                                                                                                                                                                                                                                                             |
| §5 `AuthMethod` table          | Corrected `USERNAME_PASSWORD` from ✓ to ✗ wire-only; added tunnel-only note; simplified negotiation example to `NO_AUTH` only                                                                                                                                                                     |
| §5 `Reply` `_reply_bytes`      | Changed `bind_port=0` literal to `PORT_UNSPECIFIED`                                                                                                                                                                                                                                               |
| §6 Constants — Sentinel Values | Renamed section; added `PORT_UNSPECIFIED` row with proxy-layer restriction note                                                                                                                                                                                                                   |
| §7 What Not To Do              | Added session-ID-as-conn_id anti-pattern; added `PORT_UNSPECIFIED` misuse anti-pattern                                                                                                                                                                                                            |
| §8 Checklist — IDs             | Added `new_session_id` item; added session-ID-in-encoder warning; added `CONN_FLOW_ID_RE` / `ID_RE` alias note                                                                                                                                                                                    |
| §8 Checklist — Encoding        | Added `CONN_ACK` wait note; added `encode_keepalive_frame` item; added `PORT_UNSPECIFIED` warning                                                                                                                                                                                                 |
| §8 Checklist — Decoding        | Added `conn_id is None` guard note for `AGENT_READY` and `KEEPALIVE`                                                                                                                                                                                                                              |
| §9.3 `InboundDispatcher`       | Added `pending_connects` to constructor; added `CONN_ACK` dispatch case; added `KEEPALIVE` case (log + discard); added `CONN_ACK` error on agent receiving it                                                                                                                                     |
| §9.4 `OutboundFrameBuilder`    | Added `ack_connection()` and `send_keepalive()` methods                                                                                                                                                                                                                                           |
| §9.5 Proxy `_reply_bytes`      | Changed `bind_port=0` to `bind_port=PORT_UNSPECIFIED`; added tunnel auth note                                                                                                                                                                                                                     |
| §9.6 Agent `handle_line`       | Added `KEEPALIVE` case (discard); added `CONN_ACK` error case; added `encode_conn_ack_frame` emit in `_handle_conn_open`                                                                                                                                                                          |
| §10.1 Tests                    | Added `test_conn_ack_round_trip`, `test_keepalive_round_trip`, `test_udp_open_round_trip`                                                                                                                                                                                                         |
| §10.2 Tests                    | Added `test_parse_frame_raises_on_extra_fields_for_keepalive`, `test_parse_frame_raises_on_extra_fields_for_agent_ready`, `test_parse_frame_keepalive_conn_id_is_none`, `test_parse_frame_strips_proxy_suffix`; updated `test_is_ready_frame_never_raises`                                        |
| §10.3 Tests                    | Added `test_encoder_rejects_session_id_as_conn_id`, `test_encoder_rejects_port_unspecified_in_open_frame`, `test_keepalive_encoder_never_raises`, `test_conn_ack_encoder_rejects_bad_conn_id`, `test_conn_ack_encoder_rejects_session_id`                                                         |
| §10.4 (new)                    | Full ID generator & validator test suite: pattern matching, alias identity, mutual exclusion, sentinel safety, namespace isolation, uniqueness                                                                                                                                                    |
| §10.5 (renumbered)             | Former §10.4; added `test_decode_error_payload_chains_utf8_error`, `test_parse_host_port_rejects_port_zero/above_range`, `test_parse_host_port_ipv6_round_trip`, `test_parse_host_port_rejects_malformed_bracket`; added `test_frame_decoding_error_details_keys_host_port` and `_frame` variants |
```