#!/usr/bin/env python3
"""
Shared infrastructure for WebSocket / Kubernetes exec probing.

This module is the single source of truth for:
  - Protocol constants, enums, and channel IDs
  - Data classes (Frame, K8sCapture, ConnectionProfile, assessment results)
  - ShellProtocol table + PROTOCOL_CANDIDATES (unified, no duplicates)
  - Transport  — channel-aware + codec-aware WebSocket I/O
  - WebSocket connection factories and receive helpers
  - K8s exec URL building and channel-frame parsing
  - Connection probing and shell protocol discovery
  - Statistics (no numpy)
  - Formatting utilities
  - Shared CLI argument builder
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import ssl
import struct
import time
import urllib.parse
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import websocket

__all__ = [
    # constants
    "CHANNEL_STDIN",
    "CHANNEL_STDOUT",
    "CHANNEL_STDERR",
    "CHANNEL_ERROR",
    "CHANNEL_RESIZE",
    "K8S_EXEC_SUBPROTOCOLS",
    "PROBE_MAGIC",
    # enums
    "FrameMode",
    "ExecMode",
    # type aliases
    "Headers",
    "SslOpt",
    # data classes
    "Frame",
    "K8sCapture",
    "ConnectionProfile",
    "LatencyStats",
    "FrameSizeStats",
    "ThroughputResult",
    "ConnectionTiming",
    "AssessmentReport",
    # shell protocol
    "ShellProtocol",
    "PROTOCOL_CANDIDATES",
    "get_protocol",
    # transport
    "Transport",
    # builders
    "build_sslopt",
    "build_headers",
    "resolve_bearer_token",
    "build_k8s_exec_url",
    # ws factories
    "ws_connect",
    "ws_connect_adaptive",
    "open_spawn_transport",
    "wrap_ws_as_transport",
    "open_fresh_shell_transport",
    # recv helpers
    "recv_frames",
    "recv_all_bytes",
    "normalize_k8s_payload",
    "recv_k8s_capture",
    # probe functions
    "probe_connection",
    "discover_shell_protocol",
    # stats
    "lat_stats",
    "frame_stats",
    # formatting
    "sha256_hex",
    "json_compact",
    "printable",
    "fmt_bytes",
    "make_binary_payload",
    "make_latency_probe",
    # cli
    "parse_header",
    "read_file_stripped",
    "add_connection_args",
]

logger = logging.getLogger("exectunnel.probe")

# ─────────────────────────────────────────────────────────────────────────────
# Type aliases  (Python 3.12+)
# ─────────────────────────────────────────────────────────────────────────────

type Headers = list[str]
type SslOpt = dict[str, Any]

# ─────────────────────────────────────────────────────────────────────────────
# Protocol constants
# ─────────────────────────────────────────────────────────────────────────────

K8S_EXEC_SUBPROTOCOLS: list[str] = [
    "v5.channel.k8s.io",
    "v4.channel.k8s.io",
    "v3.channel.k8s.io",
    "v2.channel.k8s.io",
    "v1.channel.k8s.io",
    "channel.k8s.io",
]

CHANNEL_STDIN: int = 0
CHANNEL_STDOUT: int = 1
CHANNEL_STDERR: int = 2
CHANNEL_ERROR: int = 3
CHANNEL_RESIZE: int = 4

PROBE_MAGIC: bytes = b"\xee\xcc\x00\x01"  # 4-byte magic prefix for echo probes
_PROBE_SEQ: struct.Struct = struct.Struct(">I")  # big-endian uint32 sequence

# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────


class FrameMode(StrEnum):
    """How WebSocket frames are interpreted on this channel."""

    K8S = "k8s"  # binary, first byte = K8s channel number
    RAW = "raw"  # raw bytes / text, no channel prefix
    AUTO = "auto"  # auto-detect from negotiated subprotocol


class ExecMode(StrEnum):
    """How remote commands are executed."""

    SPAWN = "spawn"  # dedicated process per test via ?command= URL params
    SHELL = "shell"  # shared interactive shell; commands sent as text


# ─────────────────────────────────────────────────────────────────────────────
# Core data classes
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Frame:
    """A single WebSocket frame received from the peer."""

    kind: str  # "text" | "binary"
    payload: bytes


@dataclass(slots=True)
class K8sCapture:
    """Accumulated channel output from a Kubernetes exec session."""

    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    error: bytearray = field(default_factory=bytearray)
    selected_subprotocol: str = ""


@dataclass(slots=True)
class ConnectionProfile:
    """Negotiated connection settings produced by :func:`probe_connection`."""

    url: str
    subprotocols: list[str] | None
    headers: Headers
    sslopt: SslOpt
    timeout: float
    frame_mode: FrameMode
    exec_mode: ExecMode
    selected_subprotocol: str | None
    handshake_ms: float
    warnings: list[str] = field(default_factory=list)


# ── Assessment result types ───────────────────────────────────────────────────


@dataclass(slots=True)
class LatencyStats:
    count: int
    min_ms: float
    max_ms: float
    mean_ms: float
    median_ms: float
    p95_ms: float
    p99_ms: float
    stddev_ms: float
    jitter_ms: float
    samples_ms: list[float] = field(repr=False, default_factory=list)


@dataclass(slots=True)
class FrameSizeStats:
    count: int
    min_bytes: int
    max_bytes: int
    mean_bytes: float
    median_bytes: float


@dataclass(slots=True)
class ThroughputResult:
    direction: str  # "upstream" | "downstream"
    total_bytes: int
    elapsed_s: float
    throughput_bps: float
    throughput_mbps: float
    frame_stats: FrameSizeStats | None


@dataclass(slots=True)
class ConnectionTiming:
    handshake_ms: float
    selected_subprotocol: str | None
    negotiated_frame_mode: str
    negotiated_exec_mode: str
    shell_protocol: str | None


@dataclass(slots=True)
class AssessmentReport:
    connection: ConnectionTiming
    latency: LatencyStats | None = None
    downstream: ThroughputResult | None = None
    upstream: ThroughputResult | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    verdict: str = "UNKNOWN"
    verdict_details: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Shell protocol definitions
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ShellProtocol:
    """Encodes how shell commands and stdin bytes are framed through WebSocket.

    Covers raw text/binary, JSON-wrapped, and K8s channel-byte-prefixed frames.
    This is the unified definition shared by all probe tools.
    """

    name: str
    use_binary_frame: bool
    line_ending: str
    json_key: str | None = None  # discriminator key:   "type", "op", "event" …
    json_value: str | None = None  # discriminator value: "input", "stdin" …
    k8s_prefix: bool = False  # prepend CHANNEL_STDIN byte (0x00)

    def send_command(self, ws: websocket.WebSocket, cmd: str) -> None:
        """Send *cmd* with this protocol's line ending appended."""
        self._send(ws, self._wrap(cmd + self.line_ending))

    def send_bytes(self, ws: websocket.WebSocket, data: bytes) -> None:
        """Send raw stdin bytes.

        .. warning::
            JSON-mode transports encode *data* as latin-1.
            Only pass printable ASCII (e.g. ``b"A" * N``) to avoid corruption.
        """
        if self.json_key is not None:
            self._send(ws, self._wrap(data.decode("latin-1")))
        else:
            payload = (bytes([CHANNEL_STDIN]) + data) if self.k8s_prefix else data
            ws.send_binary(payload)

    # ── internal ──────────────────────────────────────────────────────────────

    def _wrap(self, text: str) -> str | bytes:
        if self.json_key is not None:
            s = json.dumps(
                {self.json_key: self.json_value, "data": text},
                separators=(",", ":"),
            )
            if self.k8s_prefix:
                return bytes([CHANNEL_STDIN]) + s.encode()
            return s.encode() if self.use_binary_frame else s

        if self.k8s_prefix:
            return bytes([CHANNEL_STDIN]) + text.encode("utf-8")
        return text.encode("utf-8") if self.use_binary_frame else text

    def _send(self, ws: websocket.WebSocket, payload: str | bytes) -> None:
        if isinstance(payload, bytes):
            ws.send_binary(payload)
        elif self.use_binary_frame:
            ws.send_binary(payload.encode("utf-8"))
        else:
            ws.send(payload)


# Ordered by real-world likelihood.
# Reverse proxies stripping K8s subprotocols may still channel-prefix frames
# internally → the k8s-ch0-* variants cover that path.
PROTOCOL_CANDIDATES: list[ShellProtocol] = [
    # ── Raw text ──────────────────────────────────────────────────────────────
    ShellProtocol("text-CR", False, "\r"),
    ShellProtocol("text-LF", False, "\n"),
    # ── Raw binary ────────────────────────────────────────────────────────────
    ShellProtocol("binary-CR", True, "\r"),
    ShellProtocol("binary-LF", True, "\n"),
    # ── K8s channel-byte prefix without negotiated subprotocol ────────────────
    ShellProtocol("k8s-ch0-CR", True, "\r", k8s_prefix=True),
    ShellProtocol("k8s-ch0-LF", True, "\n", k8s_prefix=True),
    # ── CRLF (Windows / Hyper terminals) ──────────────────────────────────────
    ShellProtocol("text-CRLF", False, "\r\n"),
    ShellProtocol("binary-CRLF", True, "\r\n"),
    # ── JSON { "type": …, "data": … } ────────────────────────────────────────
    ShellProtocol("json-type-input-CR", False, "\r", "type", "input"),
    ShellProtocol("json-type-stdin-CR", False, "\r", "type", "stdin"),
    ShellProtocol("json-type-data-CR", False, "\r", "type", "data"),
    ShellProtocol("json-type-terminal-input-CR", False, "\r", "type", "terminal-input"),
    # ── JSON { "op": …, "data": … } ──────────────────────────────────────────
    ShellProtocol("json-op-stdin-CR", False, "\r", "op", "stdin"),
    ShellProtocol("json-op-input-CR", False, "\r", "op", "input"),
    ShellProtocol("json-op-data-CR", False, "\r", "op", "data"),
    # ── JSON with LF ──────────────────────────────────────────────────────────
    ShellProtocol("json-type-input-LF", False, "\n", "type", "input"),
    ShellProtocol("json-type-stdin-LF", False, "\n", "type", "stdin"),
    # ── JSON { "event": …, "data": … } ───────────────────────────────────────
    ShellProtocol("json-event-input-CR", False, "\r", "event", "input"),
    ShellProtocol("json-event-stdin-CR", False, "\r", "event", "stdin"),
    ShellProtocol("json-event-data-CR", False, "\r", "event", "data"),
    # ── Misc JSON shapes ──────────────────────────────────────────────────────
    ShellProtocol("json-message-input-CR", False, "\r", "message", "input"),
    ShellProtocol("json-action-input-CR", False, "\r", "action", "input"),
]

_PROTOCOL_BY_NAME: dict[str, ShellProtocol] = {p.name: p for p in PROTOCOL_CANDIDATES}


def get_protocol(name: str) -> ShellProtocol:
    """Return the :class:`ShellProtocol` with *name*; raises ``KeyError`` if absent."""
    return _PROTOCOL_BY_NAME[name]


# ─────────────────────────────────────────────────────────────────────────────
# CLI argument helpers
# ─────────────────────────────────────────────────────────────────────────────


def parse_header(raw: str) -> tuple[str, str]:
    """Parse ``Name:Value`` for argparse ``type=`` callbacks."""
    if ":" not in raw:
        raise argparse.ArgumentTypeError("headers must be in Name:Value form")
    name, _, value = raw.partition(":")
    name = name.strip()
    value = value.strip()
    if not name:
        raise argparse.ArgumentTypeError("header name cannot be empty")
    return name, value


def read_file_stripped(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


def add_connection_args(
    parser: argparse.ArgumentParser,
    *,
    include_token: bool = True,
    include_subprotocol: bool = True,
    include_origin: bool = True,
    default_timeout: float = 10.0,
    default_user_agent: str = "Mozilla/5.0",
) -> None:
    """Add the standard WebSocket connection arguments to *parser* (in-place)."""
    g = parser.add_argument_group("connection")
    g.add_argument("--url", required=True, help="WebSocket URL (ws:// or wss://)")
    if include_token:
        g.add_argument(
            "--token", default=None, metavar="TOKEN", help="Bearer token string"
        )
        g.add_argument(
            "--token-file",
            default=None,
            metavar="FILE",
            help="File containing bearer token",
        )
    g.add_argument(
        "--ca-file", default=None, metavar="FILE", help="CA certificate file for TLS"
    )
    g.add_argument(
        "--insecure", action="store_true", help="Skip TLS certificate verification"
    )
    g.add_argument(
        "--header",
        action="append",
        type=parse_header,
        default=[],
        metavar="Name:Value",
        help="Extra HTTP header (repeatable)",
    )
    if include_origin:
        g.add_argument("--origin", default=None, help="Override the Origin header")
        g.add_argument(
            "--user-agent",
            default=default_user_agent,
            help=f"User-Agent string (default: {default_user_agent!r})",
        )
    if include_subprotocol:
        g.add_argument(
            "--subprotocol",
            action="append",
            default=[],
            metavar="PROTO",
            help="Requested WebSocket subprotocol (repeatable)",
        )
    g.add_argument(
        "--timeout",
        type=float,
        default=default_timeout,
        help=f"Connection/operation timeout in seconds (default: {default_timeout})",
    )
    g.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")


# ─────────────────────────────────────────────────────────────────────────────
# SSL / header / URL builders
# ─────────────────────────────────────────────────────────────────────────────


def build_sslopt(
    url: str,
    *,
    insecure: bool = False,
    ca_file: str | None = None,
) -> SslOpt:
    if url.startswith("ws://"):
        return {}
    if insecure:
        return {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
    opt: SslOpt = {"cert_reqs": ssl.CERT_REQUIRED, "check_hostname": True}
    if ca_file:
        opt["ca_certs"] = ca_file
    return opt


def build_headers(
    extra: list[tuple[str, str]] | None = None,
    *,
    origin: str | None = None,
    user_agent: str | None = None,
    bearer_token: str | None = None,
) -> Headers:
    headers: Headers = []
    for name, value in extra or []:
        headers.append(f"{name}: {value}")
    if origin:
        headers.append(f"Origin: {origin}")
    if user_agent:
        headers.append(f"User-Agent: {user_agent}")
    if bearer_token and not any(
        h.lower().startswith("authorization:") for h in headers
    ):
        headers.append(f"Authorization: Bearer {bearer_token}")
    return headers


def resolve_bearer_token(
    token: str | None = None,
    token_file: str | None = None,
) -> str | None:
    if token:
        return token
    if token_file:
        return read_file_stripped(token_file)
    return None


def build_k8s_exec_url(
    base_url: str,
    command: list[str],
    *,
    tty: bool,
    stdin: bool = True,
    stdout: bool = True,
    stderr: bool = True,
) -> str:
    """Construct a K8s exec WebSocket URL with the required query parameters."""
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in ("ws", "wss"):
        raise ValueError(f"URL scheme must be ws or wss, got {parsed.scheme!r}")

    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    query["stdin"] = ["true" if stdin else "false"]
    query["stdout"] = ["true" if stdout else "false"]
    query["stderr"] = ["true" if stderr else "false"]
    query["tty"] = ["true" if tty else "false"]
    query.pop("command", None)
    query["command"] = list(command)

    return urllib.parse.urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        "",
        urllib.parse.urlencode(query, doseq=True),
        "",
    ))


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket connection factories
# ─────────────────────────────────────────────────────────────────────────────


def ws_connect(
    url: str,
    *,
    headers: Headers | None = None,
    subprotocols: list[str] | None = None,
    sslopt: SslOpt | None = None,
    timeout: float = 10.0,
) -> websocket.WebSocket:
    """Open a synchronous WebSocket connection."""
    ws = websocket.create_connection(
        url,
        timeout=timeout,
        header=headers or [],
        subprotocols=subprotocols,
        sslopt=sslopt or {},
        enable_multithread=True,
    )
    logger.debug("connected url=%s subprotocol=%s", url, ws.getsubprotocol())
    return ws


def ws_connect_adaptive(
    url: str,
    *,
    headers: Headers | None = None,
    preferred_subprotocols: list[str] | None = None,
    sslopt: SslOpt | None = None,
    timeout: float = 10.0,
) -> websocket.WebSocket:
    """Connect with *preferred_subprotocols*; fall back without them on failure.

    Many reverse proxies (including stream.runflare.com) reject or strip the
    ``Sec-WebSocket-Protocol`` header — this helper retries plain on failure.
    """
    if preferred_subprotocols:
        try:
            return ws_connect(
                url,
                headers=headers,
                subprotocols=preferred_subprotocols,
                sslopt=sslopt,
                timeout=timeout,
            )
        except Exception as exc:
            logger.info("subprotocol negotiation failed (%s); retrying plain", exc)
    return ws_connect(url, headers=headers, sslopt=sslopt, timeout=timeout)


# ─────────────────────────────────────────────────────────────────────────────
# Receive helpers
# ─────────────────────────────────────────────────────────────────────────────


def recv_frames(ws: websocket.WebSocket, seconds: float) -> list[Frame]:
    """Collect all frames arriving within *seconds*."""
    deadline = time.monotonic() + seconds
    frames: list[Frame] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ws.settimeout(max(0.01, min(1.0, remaining)))
        try:
            msg = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        except websocket.WebSocketConnectionClosedException:
            logger.debug("connection closed during recv_frames")
            break
        frames.append(
            Frame("binary", msg)
            if isinstance(msg, bytes)
            else Frame("text", msg.encode("utf-8", errors="replace"))
        )
    return frames


def recv_all_bytes(ws: websocket.WebSocket, seconds: float) -> bytes:
    """Concatenate all frame payloads received within *seconds*."""
    return b"".join(f.payload for f in recv_frames(ws, seconds))


def normalize_k8s_payload(data: str | bytes | bytearray) -> bytes:
    """Normalize a WebSocket message to bytes for K8s channel parsing.

    K8s exec always uses binary frames.  If a misbehaving proxy delivers them
    as text, decode via latin-1 (identity map for 0x00–0xFF).
    """
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    logger.warning("text frame on K8s exec channel — converting via latin-1")
    return data.encode("latin-1", errors="surrogateescape")


def recv_k8s_capture(
    ws: websocket.WebSocket,
    *,
    selected_subprotocol: str,
    timeout: float,
    want_stdout_len: int | None = None,
    idle_after_first_stdout: float | None = None,
) -> K8sCapture:
    """Demultiplex K8s exec channel frames into a :class:`K8sCapture`.

    Args:
        want_stdout_len: Exit early once this many stdout bytes accumulate.
        idle_after_first_stdout: Exit this many seconds after the first stdout
            chunk arrives — useful for commands with open-ended output.
    """
    cap = K8sCapture(selected_subprotocol=selected_subprotocol)
    hard_deadline = time.monotonic() + timeout
    idle_deadline: float | None = None

    while True:
        now = time.monotonic()
        effective = (
            min(hard_deadline, idle_deadline) if idle_deadline else hard_deadline
        )
        if now >= effective:
            break

        ws.settimeout(max(0.01, min(1.0, effective - now)))

        try:
            msg = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        except websocket.WebSocketConnectionClosedException:
            break

        payload = normalize_k8s_payload(msg)
        if not payload:
            continue

        ch, body = payload[0], payload[1:]

        if ch == CHANNEL_STDOUT:
            cap.stdout.extend(body)
            if idle_after_first_stdout is not None and idle_deadline is None:
                idle_deadline = time.monotonic() + idle_after_first_stdout
            if want_stdout_len is not None and len(cap.stdout) >= want_stdout_len:
                break
        elif ch == CHANNEL_STDERR:
            cap.stderr.extend(body)
        elif ch == CHANNEL_ERROR:
            cap.error.extend(body)
            break

    return cap


# ─────────────────────────────────────────────────────────────────────────────
# Transport — channel-aware + codec-aware WebSocket I/O
# ─────────────────────────────────────────────────────────────────────────────


class Transport:
    """Unified WebSocket I/O for both K8s channel framing and raw pipe mode.

    In K8S mode: prepends CHANNEL_STDIN on send; demultiplexes channel
    bytes on receive (stdout/stderr/error).

    In RAW mode: delegates to a :class:`ShellProtocol` for command encoding;
    passes through received frames verbatim.
    """

    def __init__(
        self,
        ws: websocket.WebSocket,
        frame_mode: FrameMode,
        shell_protocol: ShellProtocol | None = None,
    ) -> None:
        self._ws = ws
        self.mode = frame_mode
        self._proto = shell_protocol
        self._closed = False
        self.stderr_buf = bytearray()

    @property
    def subprotocol(self) -> str | None:
        return self._ws.getsubprotocol()

    @property
    def closed(self) -> bool:
        return self._closed

    # ── send ──────────────────────────────────────────────────────────────────

    def send_command(self, cmd: str) -> None:
        """Send *cmd* via the appropriate framing."""
        if self.mode is FrameMode.K8S:
            ending = self._proto.line_ending if self._proto else "\r"
            self._ws.send_binary(bytes([CHANNEL_STDIN]) + (cmd + ending).encode())
        elif self._proto is not None:
            self._proto.send_command(self._ws, cmd)
        else:
            raise RuntimeError("no ShellProtocol configured for RAW frame mode")

    def send_stdin(self, data: bytes) -> None:
        """Send *data* to the remote stdin channel."""
        if self.mode is FrameMode.K8S:
            self._ws.send_binary(bytes([CHANNEL_STDIN]) + data)
        elif self._proto is not None:
            self._proto.send_bytes(self._ws, data)
        else:
            self._ws.send_binary(data)

    # ── receive ───────────────────────────────────────────────────────────────

    def recv_stdout(self, deadline: float) -> tuple[bytes, float] | None:
        """Return the next ``(data, monotonic_ts)`` stdout chunk, or ``None``."""
        while not self._closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._ws.settimeout(max(0.005, min(1.0, remaining)))

            try:
                msg = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except websocket.WebSocketConnectionClosedException:
                self._closed = True
                return None

            ts = time.monotonic()

            if self.mode is FrameMode.K8S:
                raw = normalize_k8s_payload(msg)
                if not raw:
                    continue
                ch, body = raw[0], raw[1:]
                if ch == CHANNEL_STDOUT:
                    return body, ts
                if ch == CHANNEL_STDERR:
                    self.stderr_buf.extend(body)
                elif ch == CHANNEL_ERROR:
                    self._closed = True
                    err = body.decode("utf-8", errors="replace")
                    # K8s sends a success JSON on channel 3 at clean exit — only warn on errors
                    if '"status":"Success"' not in err and '"exitCode":0' not in err:
                        logger.warning("k8s error channel: %s", err)
                    else:
                        logger.debug("k8s error channel (success): %s", err)
                    return None
            else:
                data = (
                    msg
                    if isinstance(msg, bytes)
                    else msg.encode("utf-8", errors="replace")
                )
                return data, ts

        return None

    def drain(self, seconds: float) -> bytes:
        """Read and return all stdout available within *seconds*."""
        deadline = time.monotonic() + seconds
        out = bytearray()
        while (r := self.recv_stdout(deadline)) is not None:
            out.extend(r[0])
        return bytes(out)

    def recv_until(self, marker: bytes, deadline: float) -> tuple[bytearray, bool]:
        """Accumulate stdout until *marker* appears or *deadline* expires."""
        buf = bytearray()
        while marker not in buf:
            r = self.recv_stdout(deadline)
            if r is None:
                return buf, False
            buf.extend(r[0])
        return buf, True

    def recv_counted(
        self,
        want: int,
        deadline: float,
    ) -> tuple[int, list[int], float | None, float | None]:
        """Receive up to *want* stdout bytes; return ``(got, frame_sizes, t_first, t_last)``."""
        got = 0
        sizes: list[int] = []
        t_first: float | None = None
        t_last: float | None = None
        while got < want:
            r = self.recv_stdout(deadline)
            if r is None:
                break
            body, ts = r
            n = len(body)
            if not n:
                continue
            got += n
            sizes.append(n)
            if t_first is None:
                t_first = ts
            t_last = ts
        return got, sizes, t_first, t_last

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._ws.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Transport factories
# ─────────────────────────────────────────────────────────────────────────────


def open_spawn_transport(
    profile: ConnectionProfile,
    command: list[str],
    *,
    stdin: bool = True,
) -> Transport:
    """Open a dedicated K8s exec connection for *command* as a Transport."""
    url = build_k8s_exec_url(profile.url, command, tty=False, stdin=stdin)
    ws = ws_connect(
        url,
        headers=profile.headers,
        subprotocols=profile.subprotocols,
        sslopt=profile.sslopt,
        timeout=profile.timeout,
    )
    return Transport(ws, profile.frame_mode)


def wrap_ws_as_transport(
    ws: websocket.WebSocket,
    profile: ConnectionProfile,
    proto: ShellProtocol,
) -> Transport:
    """Wrap an already-open WebSocket (e.g. from discovery) as a Transport."""
    return Transport(ws, profile.frame_mode, shell_protocol=proto)


def open_fresh_shell_transport(
    profile: ConnectionProfile,
    proto: ShellProtocol,
    initial_wait: float = 1.5,
) -> Transport:
    """Open a new shell WebSocket connection and drain the startup banner."""
    ws = ws_connect(
        profile.url,
        headers=profile.headers,
        subprotocols=profile.subprotocols,
        sslopt=profile.sslopt,
        timeout=profile.timeout,
    )
    t = Transport(ws, profile.frame_mode, shell_protocol=proto)
    t.drain(initial_wait)
    return t


# ─────────────────────────────────────────────────────────────────────────────
# Connection probing
# ─────────────────────────────────────────────────────────────────────────────


def probe_connection(args: argparse.Namespace) -> ConnectionProfile:
    """Open (and immediately close) a probe WebSocket to negotiate settings.

    Required *args* attributes:
        ``url``, ``insecure``, ``header``, ``timeout``, ``frame_mode``, ``exec_mode``

    Optional *args* attributes (``None`` / ``[]`` when absent):
        ``token``, ``token_file``, ``ca_file``, ``origin``, ``user_agent``, ``subprotocol``
    """
    token = resolve_bearer_token(
        getattr(args, "token", None),
        getattr(args, "token_file", None),
    )
    headers = build_headers(
        args.header,
        origin=getattr(args, "origin", None),
        user_agent=getattr(args, "user_agent", None),
        bearer_token=token,
    )
    sslopt = build_sslopt(
        args.url,
        insecure=args.insecure,
        ca_file=getattr(args, "ca_file", None),
    )

    requested_frame = FrameMode(args.frame_mode)
    requested_exec = ExecMode(args.exec_mode)
    explicit_sp: list[str] | None = args.subprotocol or None
    warnings: list[str] = []

    if explicit_sp is not None:
        try_sp: list[str] | None = explicit_sp
    elif requested_frame is FrameMode.K8S:
        try_sp = K8S_EXEC_SUBPROTOCOLS
    elif requested_frame is FrameMode.RAW:
        try_sp = None
    else:  # AUTO
        try_sp = K8S_EXEC_SUBPROTOCOLS

    resolved_sp: list[str] | None = None
    selected: str | None = None
    t0 = time.monotonic()

    if try_sp is not None:
        try:
            ws = ws_connect(
                args.url,
                headers=headers,
                subprotocols=try_sp,
                sslopt=sslopt,
                timeout=args.timeout,
            )
            selected = ws.getsubprotocol()
            resolved_sp = try_sp
            ws.close()
        except Exception as exc:
            if requested_frame is FrameMode.K8S:
                raise RuntimeError(
                    f"K8s subprotocol negotiation failed (--frame-mode=k8s explicit): {exc}"
                ) from exc
            warnings.append(
                f"K8s subprotocol negotiation failed ({exc}); falling back to RAW."
            )
            ws = ws_connect(
                args.url, headers=headers, sslopt=sslopt, timeout=args.timeout
            )
            selected = ws.getsubprotocol()
            ws.close()
    else:
        ws = ws_connect(args.url, headers=headers, sslopt=sslopt, timeout=args.timeout)
        selected = ws.getsubprotocol()
        ws.close()

    handshake_ms = (time.monotonic() - t0) * 1000.0

    resolved_frame = (
        (FrameMode.K8S if (selected and "k8s.io" in selected) else FrameMode.RAW)
        if requested_frame is FrameMode.AUTO
        else requested_frame
    )

    # RAW + SPAWN is impossible: proxy won't honour ?command= URL params
    resolved_exec = requested_exec
    if resolved_frame is FrameMode.RAW and requested_exec is ExecMode.SPAWN:
        warnings.append(
            "RAW frame mode + --exec-mode=spawn is unsupported: proxy ignores "
            "?command= params. Auto-switching to --exec-mode=shell."
        )
        resolved_exec = ExecMode.SHELL

    return ConnectionProfile(
        url=args.url,
        subprotocols=resolved_sp,
        headers=headers,
        sslopt=sslopt,
        timeout=args.timeout,
        frame_mode=resolved_frame,
        exec_mode=resolved_exec,
        selected_subprotocol=selected,
        handshake_ms=round(handshake_ms, 2),
        warnings=warnings,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Shell protocol discovery
# ─────────────────────────────────────────────────────────────────────────────


def _drain_raw(ws: websocket.WebSocket, seconds: float) -> bytes:
    """Read all raw frames for *seconds* with no channel decoding."""
    deadline = time.monotonic() + seconds
    out = bytearray()
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ws.settimeout(max(0.01, min(1.0, remaining)))
        try:
            msg = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        except websocket.WebSocketConnectionClosedException:
            break
        out.extend(
            msg if isinstance(msg, bytes) else msg.encode("utf-8", errors="replace")
        )
    return bytes(out)


def discover_shell_protocol(
    profile: ConnectionProfile,
    *,
    initial_wait: float = 3.0,
    per_candidate_wait: float = 3.0,
) -> tuple[ShellProtocol | None, websocket.WebSocket | None, bytes]:
    """Try each candidate in :data:`PROTOCOL_CANDIDATES` on a single connection.

    Returns ``(protocol, ws, banner)`` on success — the WebSocket is left open
    for the caller to reuse, saving a round-trip handshake.

    Returns ``(None, None, banner)`` if no candidate works.
    """
    ws = ws_connect(
        profile.url,
        headers=profile.headers,
        subprotocols=profile.subprotocols,
        sslopt=profile.sslopt,
        timeout=profile.timeout,
    )

    banner = _drain_raw(ws, initial_wait)
    logger.info("shell banner (%d bytes): %r", len(banner), banner[:500])

    def _probe_once(proto: ShellProtocol, marker: str, wait: float) -> bool:
        try:
            proto.send_command(ws, f"echo '{marker}'")
        except Exception as exc:
            logger.debug("discovery send failed for %s: %s", proto.name, exc)
            return False
        response = _drain_raw(ws, wait)
        logger.debug(
            "discovery %s → %d bytes preview=%r",
            proto.name,
            len(response),
            response[:300],
        )
        return marker.encode() in response

    def _stabilize(chosen: ShellProtocol) -> ShellProtocol:
        """Cross-verify text line-ending variants; return the most reliable one."""
        text_names = ["text-CR", "text-LF", "text-CRLF"]
        if chosen.name not in text_names:
            return chosen
        wait = max(0.2, min(0.8, per_candidate_wait / 3.0))
        for name in text_names:
            proto = _PROTOCOL_BY_NAME[name]
            if _probe_once(proto, f"__PROBE_STABLE_{name}__", wait):
                if proto.name != chosen.name:
                    logger.info(
                        "discovery: stabilized %s → %s", chosen.name, proto.name
                    )
                return proto
        return chosen

    for i, proto in enumerate(PROTOCOL_CANDIDATES):
        logger.debug("discovery: trying %s", proto.name)
        if _probe_once(proto, f"__PROBE_{i:04d}_OK__", per_candidate_wait):
            selected = _stabilize(proto)
            logger.info("discovery: SUCCESS → %s", selected.name)
            # Flush any shell echo from probe commands
            try:
                proto.send_command(ws, "true")
                proto.send_command(ws, "true")
            except Exception:
                pass
            _drain_raw(ws, 0.5)
            return selected, ws, banner

    try:
        ws.close()
    except Exception:
        pass
    return None, None, banner


# ─────────────────────────────────────────────────────────────────────────────
# Statistics — pure Python, no third-party deps
# ─────────────────────────────────────────────────────────────────────────────


def _pct(s: list[float], p: float) -> float:
    """Linear-interpolation percentile of a *sorted* list at *p* (0–100)."""
    if not s:
        return 0.0
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100.0
    f = int(math.floor(k))
    c = int(math.ceil(k))
    return s[f] if f == c else s[f] * (c - k) + s[c] * (k - f)


def _mean(v: list[float]) -> float:
    return sum(v) / len(v) if v else 0.0


def _stddev(v: list[float]) -> float:
    if len(v) < 2:
        return 0.0
    m = _mean(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))


def _jitter(v: list[float]) -> float:
    """Mean absolute frame-to-frame delta (RFC 3550-style jitter estimate)."""
    if len(v) < 2:
        return 0.0
    return _mean([abs(v[i] - v[i - 1]) for i in range(1, len(v))])


def lat_stats(samples: list[float]) -> LatencyStats:
    """Compute :class:`LatencyStats` from RTT samples in milliseconds."""
    s = sorted(samples)
    return LatencyStats(
        count=len(s),
        min_ms=s[0] if s else 0.0,
        max_ms=s[-1] if s else 0.0,
        mean_ms=_mean(s),
        median_ms=_pct(s, 50),
        p95_ms=_pct(s, 95),
        p99_ms=_pct(s, 99),
        stddev_ms=_stddev(samples),
        jitter_ms=_jitter(samples),
        samples_ms=list(samples),
    )


def frame_stats(sizes: list[int]) -> FrameSizeStats | None:
    """Compute :class:`FrameSizeStats` from frame byte-counts, or ``None`` if empty."""
    if not sizes:
        return None
    s = sorted(sizes)
    fv = [float(x) for x in s]
    return FrameSizeStats(
        count=len(s),
        min_bytes=s[0],
        max_bytes=s[-1],
        mean_bytes=_mean(fv),
        median_bytes=_pct(fv, 50),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Formatting utilities
# ─────────────────────────────────────────────────────────────────────────────


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


def printable(data: bytes | bytearray) -> str:
    """Decode *data* as UTF-8, replacing un-decodable bytes."""
    return bytes(data).decode("utf-8", errors="replace")


def fmt_bytes(n: int) -> str:
    """Format a byte count as human-readable MiB / KiB / B."""
    if n >= 1 << 20:
        return f"{n / (1 << 20):.2f} MiB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.2f} KiB"
    return f"{n} B"


def make_binary_payload(size: int, *, seed: int = 0) -> bytes:
    """Generate a deterministic non-trivial binary payload of *size* bytes.

    Uses a SHA-256 chain seeded from *seed*.  The output would *not* survive
    common byte transforms (line-ending conversion, base64, 7-bit stripping),
    making it suitable for detecting channel corruption.
    """
    if size <= 0:
        raise ValueError("size must be > 0")
    chunks: list[bytes] = []
    state = seed.to_bytes(8, "big")
    total = 0
    while total < size:
        state = hashlib.sha256(state).digest()
        chunks.append(state)
        total += len(state)
    return b"".join(chunks)[:size]


def make_latency_probe(seq: int) -> bytes:
    """Return an 8-byte echo-probe: ``PROBE_MAGIC(4) + seq(4 big-endian)``."""
    return PROBE_MAGIC + _PROBE_SEQ.pack(seq)
