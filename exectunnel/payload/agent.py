#!/usr/bin/env python3
from __future__ import annotations

import base64
import binascii
import contextlib
import errno
import ipaddress
import json
import math
import os
import queue
import re
import select
import signal
import socket
import sys
import tempfile
import termios
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol


class _WritableStream(Protocol):
    def write(self, s: str, /) -> int: ...

    def flush(self) -> None: ...


_FRAME_PREFIX: Final = "<<<EXECTUNNEL:"
_FRAME_SUFFIX: Final = ">>>"
_AGENT_VERSION: Final = "2"

_AGENT_PATHS: Final = ("/tmp/exectunnel_agent.py", "/tmp/exectunnel_agent.b64")

_MAX_TCP_UDP_PORT: Final = 65_535
_MIN_PARTS_WITH_CONN_ID: Final = 2
_MIN_PARTS_WITH_PAYLOAD: Final = 3
_PAYLOAD_PART_INDEX: Final = 2
_ARGC_FIXED_TARGET: Final = 3

_ID_RE: Final = re.compile(r"^[cu][0-9a-f]{24}$")
_B64URL_RE: Final = re.compile(r"^[A-Za-z0-9_-]*$")

_FRAME_UNSAFE_RE: Final = re.compile(r"[:<>]", re.ASCII)
_DOMAIN_RE: Final = re.compile(
    r"^[A-Za-z0-9_]([A-Za-z0-9\-_.]*[A-Za-z0-9_])?$",
    re.ASCII,
)

_CONN_FLOW_ID_CHARS: Final = 25
_DATA_FRAME_OVERHEAD_CHARS: Final = (
    len(_FRAME_PREFIX) + len("DATA") + 2 + _CONN_FLOW_ID_CHARS + len(_FRAME_SUFFIX)
)
_UDP_DATA_FRAME_OVERHEAD_CHARS: Final = (
    len(_FRAME_PREFIX) + len("UDP_DATA") + 2 + _CONN_FLOW_ID_CHARS + len(_FRAME_SUFFIX)
)


def _max_binary_payload_bytes(max_frame_chars: int, overhead_chars: int) -> int:
    available = max_frame_chars - overhead_chars
    return max(1, available * 3 // 4)


_LOG_LEVELS: Final = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
}

_BOOT_WARNINGS: list[str] = []


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default

    try:
        value = int(raw, 10)
    except ValueError:
        _BOOT_WARNINGS.append(f"{name}={raw!r} is not an integer; using {default!r}")
        return default

    if minimum is not None and value < minimum:
        _BOOT_WARNINGS.append(
            f"{name}={raw!r} is below minimum {minimum}; using {default!r}"
        )
        return default

    if maximum is not None and value > maximum:
        _BOOT_WARNINGS.append(
            f"{name}={raw!r} is above maximum {maximum}; using {default!r}"
        )
        return default

    return value


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default

    try:
        value = float(raw)
    except ValueError:
        _BOOT_WARNINGS.append(f"{name}={raw!r} is not a float; using {default!r}")
        return default

    if not math.isfinite(value):
        _BOOT_WARNINGS.append(f"{name}={raw!r} is not finite; using {default!r}")
        return default

    if minimum is not None and value < minimum:
        _BOOT_WARNINGS.append(
            f"{name}={raw!r} is below minimum {minimum}; using {default!r}"
        )
        return default

    if maximum is not None and value > maximum:
        _BOOT_WARNINGS.append(
            f"{name}={raw!r} is above maximum {maximum}; using {default!r}"
        )
        return default

    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default

    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_set(name: str, default: str) -> frozenset[str]:
    raw = os.getenv(name, default)
    return frozenset(item.strip().lower() for item in raw.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class _AgentConfig:
    max_tcp_workers: int = _env_int(
        "EXECTUNNEL_AGENT_MAX_TCP_WORKERS",
        512,
        minimum=1,
    )
    max_udp_workers: int = _env_int(
        "EXECTUNNEL_AGENT_MAX_UDP_WORKERS",
        512,
        minimum=1,
    )

    max_tcp_inbound_chunks: int = _env_int(
        "EXECTUNNEL_AGENT_MAX_TCP_INBOUND_CHUNKS",
        1024,
        minimum=1,
    )

    max_tcp_inbound_bytes: int = _env_int(
        "EXECTUNNEL_AGENT_MAX_TCP_INBOUND_BYTES",
        8 * 1024 * 1024,
        minimum=1,
    )

    max_udp_inbound_dgrams: int = _env_int(
        "EXECTUNNEL_AGENT_MAX_UDP_INBOUND_DGRAMS",
        2048,
        minimum=1,
    )

    max_udp_inbound_bytes: int = _env_int(
        "EXECTUNNEL_AGENT_MAX_UDP_INBOUND_BYTES",
        8 * 1024 * 1024,
        minimum=1,
    )

    stdout_data_queue_cap: int = _env_int(
        "EXECTUNNEL_AGENT_STDOUT_DATA_QUEUE_CAP",
        2048,
        minimum=1,
    )
    stdout_backpressure_ratio: float = _env_float(
        "EXECTUNNEL_AGENT_STDOUT_BACKPRESSURE_RATIO",
        0.75,
        minimum=0.0,
        maximum=1.0,
    )

    writer_ctrl_batch_size: int = _env_int(
        "EXECTUNNEL_AGENT_WRITER_CTRL_BATCH_SIZE",
        256,
        minimum=1,
    )
    writer_data_batch_size: int = _env_int(
        "EXECTUNNEL_AGENT_WRITER_DATA_BATCH_SIZE",
        64,
        minimum=1,
    )
    writer_ctrl_poll_secs: float = _env_float(
        "EXECTUNNEL_AGENT_WRITER_CTRL_POLL_SECS",
        0.05,
        minimum=0.001,
    )
    writer_join_timeout_secs: float = _env_float(
        "EXECTUNNEL_AGENT_WRITER_JOIN_TIMEOUT_SECS",
        5.0,
        minimum=0.0,
    )

    tcp_connect_timeout_secs: float = _env_float(
        "EXECTUNNEL_AGENT_TCP_CONNECT_TIMEOUT_SECS",
        8.0,
        minimum=0.1,
    )
    tcp_recv_chunk_bytes: int = _env_int(
        "EXECTUNNEL_AGENT_TCP_RECV_CHUNK_BYTES",
        8192,
        minimum=1,
    )
    max_udp_dgram_bytes: int = _env_int(
        "EXECTUNNEL_AGENT_MAX_UDP_DGRAM_BYTES",
        4096,
        minimum=1,
        maximum=65535,
    )
    udp_idle_timeout_secs: float = _env_float(
        "EXECTUNNEL_AGENT_UDP_IDLE_TIMEOUT_SECS",
        120.0,
        minimum=0.0,
    )
    udp_drop_warn_every: int = _env_int(
        "EXECTUNNEL_AGENT_UDP_DROP_WARN_EVERY",
        1000,
        minimum=1,
    )
    select_timeout_secs: float = _env_float(
        "EXECTUNNEL_AGENT_SELECT_TIMEOUT_SECS",
        0.01,
        minimum=0.001,
    )
    half_close_deadline_secs: float = _env_float(
        "EXECTUNNEL_AGENT_HALF_CLOSE_DEADLINE_SECS",
        30.0,
        minimum=0.0,
    )
    send_writable_timeout_secs: float = _env_float(
        "EXECTUNNEL_AGENT_SEND_WRITABLE_TIMEOUT_SECS",
        5.0,
        minimum=0.001,
    )
    emit_data_put_timeout_secs: float = _env_float(
        "EXECTUNNEL_AGENT_EMIT_DATA_PUT_TIMEOUT_SECS",
        1.0,
        minimum=0.001,
    )
    emit_data_hard_deadline_secs: float = _env_float(
        "EXECTUNNEL_AGENT_EMIT_DATA_DEADLINE_SECS",
        30.0,
        minimum=0.001,
    )
    worker_shutdown_grace_secs: float = _env_float(
        "EXECTUNNEL_AGENT_WORKER_SHUTDOWN_GRACE_SECS",
        3.0,
        minimum=0.0,
    )

    max_inbound_frame_len: int = _env_int(
        "EXECTUNNEL_AGENT_MAX_INBOUND_FRAME_LEN",
        262144,
        minimum=1024,
    )

    max_outbound_frame_len: int = _env_int(
        "EXECTUNNEL_AGENT_MAX_OUTBOUND_FRAME_LEN",
        262144,
        minimum=max(_DATA_FRAME_OVERHEAD_CHARS + 4, _UDP_DATA_FRAME_OVERHEAD_CHARS + 4),
    )

    dns_cache_max_entries: int = _env_int(
        "EXECTUNNEL_AGENT_DNS_CACHE_MAX_ENTRIES",
        4096,
        minimum=1,
    )
    dns_cache_ttl_secs: float = _env_float(
        "EXECTUNNEL_AGENT_DNS_CACHE_TTL_SECS",
        300.0,
    )

    stats_enabled: bool = _env_bool("EXECTUNNEL_AGENT_STATS_ENABLED", False)
    stats_sinks: frozenset[str] = _env_set(
        "EXECTUNNEL_AGENT_STATS_SINKS",
        "stdout",
    )
    stats_sample_interval_secs: float = _env_float(
        "EXECTUNNEL_AGENT_STATS_INTERVAL_SECS",
        5.0,
        minimum=0.1,
    )
    stats_file_path: str = os.getenv("EXECTUNNEL_AGENT_STATS_FILE_PATH", "")
    stats_api_url: str = os.getenv("EXECTUNNEL_AGENT_STATS_API_URL", "")
    stats_api_timeout_secs: float = _env_float(
        "EXECTUNNEL_AGENT_STATS_API_TIMEOUT_SECS",
        2.0,
        minimum=0.1,
    )
    stats_api_headers_json: str = os.getenv(
        "EXECTUNNEL_AGENT_STATS_API_HEADERS_JSON",
        "",
    )
    dispatch_samples_max: int = _env_int(
        "EXECTUNNEL_AGENT_DISPATCH_SAMPLES_MAX",
        1024,
        minimum=1,
    )

    @property
    def stdout_backpressure_threshold(self) -> int:
        threshold = int(self.stdout_data_queue_cap * self.stdout_backpressure_ratio)
        return max(0, min(self.stdout_data_queue_cap, threshold))

    @property
    def max_data_payload_bytes(self) -> int:
        return _max_binary_payload_bytes(
            self.max_outbound_frame_len,
            _DATA_FRAME_OVERHEAD_CHARS,
        )

    @property
    def max_udp_data_payload_bytes(self) -> int:
        return _max_binary_payload_bytes(
            self.max_outbound_frame_len,
            _UDP_DATA_FRAME_OVERHEAD_CHARS,
        )

    @property
    def tcp_recv_chunk_bytes_safe(self) -> int:
        return max(1, min(self.tcp_recv_chunk_bytes, self.max_data_payload_bytes))


CONFIG: Final = _AgentConfig()

_LOG_LEVEL = _LOG_LEVELS.get(
    os.getenv("EXECTUNNEL_AGENT_LOG_LEVEL", "warning").strip().lower(),
    _LOG_LEVELS["warning"],
)
_LOG_LOCK = threading.Lock()

_TERMINATE = threading.Event()

_COUNTER_LOCK = threading.Lock()
_TX_BYTES_TOTAL = 0
_RX_BYTES_TOTAL = 0
_FRAMES_TX_TOTAL = 0
_FRAMES_RX_TOTAL = 0

_DISPATCH_SAMPLES: deque[float] = deque(maxlen=CONFIG.dispatch_samples_max)
_DISPATCH_SAMPLES_LOCK = threading.Lock()


def _log(level: str, msg: str, *args: object) -> None:
    lvl = _LOG_LEVELS.get(level.lower(), _LOG_LEVELS["warning"])
    if lvl < _LOG_LEVEL:
        return

    text = msg % args if args else msg
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    with _LOG_LOCK:
        sys.stderr.write(f"{ts} {level.upper():7s} agent: {text}\n")
        sys.stderr.flush()


def _record_tx_frame(line: str) -> None:
    global _TX_BYTES_TOTAL, _FRAMES_TX_TOTAL

    with _COUNTER_LOCK:
        _TX_BYTES_TOTAL += len(line) + 1
        _FRAMES_TX_TOTAL += 1


def _record_rx_frame(line: str) -> None:
    global _RX_BYTES_TOTAL, _FRAMES_RX_TOTAL

    with _COUNTER_LOCK:
        _RX_BYTES_TOTAL += len(line) + 1
        _FRAMES_RX_TOTAL += 1


def _counter_snapshot() -> tuple[int, int, int, int]:
    with _COUNTER_LOCK:
        return _TX_BYTES_TOTAL, _RX_BYTES_TOTAL, _FRAMES_TX_TOTAL, _FRAMES_RX_TOTAL


def _record_dispatch_sample(seconds: float) -> None:
    with _DISPATCH_SAMPLES_LOCK:
        _DISPATCH_SAMPLES.append(seconds)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(s: str) -> bytes:
    if not _B64URL_RE.fullmatch(s):
        raise ValueError(f"invalid base64url alphabet: {s!r}")
    if len(s) % 4 == 1:
        raise ValueError(f"invalid base64url length: {len(s)}")
    padding = (4 - len(s) % 4) % 4
    translated = s.translate(str.maketrans("-_", "+/"))

    try:
        return base64.b64decode(translated + "=" * padding, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64url: {s!r}") from exc


def _make_frame(*parts: str) -> str:
    return f"{_FRAME_PREFIX}{':'.join(parts)}{_FRAME_SUFFIX}"


def _make_error_frame(conn_id: str, reason: str) -> str:
    payload = _b64encode(reason.encode("utf-8", "replace"))
    return _make_frame("ERROR", conn_id, payload)


def _is_valid_id(value: str) -> bool:
    return bool(_ID_RE.fullmatch(value))


def _extract_frame(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None

    start = stripped.find(_FRAME_PREFIX)
    if start < 0:
        return None

    end = stripped.find(_FRAME_SUFFIX, start + len(_FRAME_PREFIX))
    if end < 0:
        return None

    frame = stripped[start : end + len(_FRAME_SUFFIX)]

    if len(frame) > CONFIG.max_inbound_frame_len:
        _log("debug", "oversized frame dropped: %d chars", len(frame))
        return None

    return frame


def _parse_frame_line(line: str) -> list[str] | None:
    frame = _extract_frame(line)
    if frame is None:
        return None

    inner = frame[len(_FRAME_PREFIX) : -len(_FRAME_SUFFIX)]
    parts = inner.split(":", 2)

    if not parts or not parts[0]:
        return None

    return parts


def _parse_host_port(
    raw: str, frame_id: str, frame_type: str
) -> tuple[str, int] | None:
    value = raw.strip()
    if not value:
        return None
    bracketed = value.startswith("[")

    if bracketed:
        bracket_end = value.find("]")
        if bracket_end == -1 or value[bracket_end + 1 : bracket_end + 2] != ":":
            _log(
                "debug",
                "malformed bracketed host in %s for %s: %r",
                frame_type,
                frame_id,
                raw,
            )
            return None

        host = value[1:bracket_end]
        port_str = value[bracket_end + 2 :]
    else:
        host, sep, port_str = value.rpartition(":")
        if not sep or not host:
            _log("debug", "invalid %s payload for %s: %r", frame_type, frame_id, raw)
            return None

    if not host or not port_str:
        return None

    try:
        port = int(port_str, 10)
    except ValueError:
        return None

    if port <= 0 or port > _MAX_TCP_UDP_PORT:
        return None

    if bracketed:
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            _log(
                "debug",
                "bracketed %s host is not an IP for %s: %r",
                frame_type,
                frame_id,
                host,
            )
            return None
        if addr.version != 6:
            _log(
                "debug",
                "bracketed %s host is not IPv6 for %s: %r",
                frame_type,
                frame_id,
                host,
            )
            return None
        return addr.compressed, port

    if ":" in host:
        _log(
            "debug",
            "unbracketed %s host contains ':' for %s: %r",
            frame_type,
            frame_id,
            host,
        )
        return None

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        if (
            _FRAME_UNSAFE_RE.search(host)
            or ".." in host
            or not _DOMAIN_RE.fullmatch(host)
        ):
            _log("debug", "invalid %s hostname for %s: %r", frame_type, frame_id, host)
            return None
        return host, port

    return addr.compressed, port


def _install_sigpipe_handler() -> None:
    with contextlib.suppress(OSError, ValueError):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)


def _install_termination_handlers() -> None:
    def _handle(_signum: int, _frame: object | None) -> None:
        _TERMINATE.set()
        with contextlib.suppress(OSError, ValueError, AttributeError):
            os.close(sys.stdin.fileno())

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(OSError, ValueError):
            signal.signal(sig, _handle)


def _disable_echo() -> None:
    try:
        fd = sys.stdin.fileno()
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~(termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error:
        _log("debug", "stdin is not a tty; skipping echo disable")
    except OSError as exc:
        _log("debug", "could not disable stdin echo: %s", exc)


_DnsKey = tuple[str, int, int]


class _DnsCache:
    def __init__(
        self,
        max_entries: int = CONFIG.dns_cache_max_entries,
        ttl_secs: float = CONFIG.dns_cache_ttl_secs,
    ) -> None:
        self._cache: dict[_DnsKey, tuple[float, list[tuple]]] = {}
        self._key_locks: dict[_DnsKey, threading.Lock] = {}
        self._mu = threading.Lock()
        self._max_entries = max_entries
        self._ttl_secs = ttl_secs

    def _evict_if_needed_locked(self) -> None:
        while len(self._cache) > self._max_entries:
            oldest_key = next(iter(self._cache))
            self._cache.pop(oldest_key, None)
            self._key_locks.pop(oldest_key, None)

    def _get_cached_locked(self, key: _DnsKey, now: float) -> list[tuple] | None:
        cached = self._cache.get(key)
        if cached is None:
            return None

        ts, infos = cached
        if self._ttl_secs <= 0 or now - ts <= self._ttl_secs:
            self._cache.pop(key, None)
            self._cache[key] = (ts, infos)
            return list(infos)

        self._cache.pop(key, None)
        self._key_locks.pop(key, None)
        return None

    def getaddrinfo(self, host: str, port: int, *, socktype: int = 0) -> list[tuple]:
        key: _DnsKey = (host, port, socktype)
        now = time.monotonic()

        with self._mu:
            cached = self._get_cached_locked(key, now)
            if cached is not None:
                return cached

            key_lock = self._key_locks.get(key)
            if key_lock is None:
                key_lock = threading.Lock()
                self._key_locks[key] = key_lock

        with key_lock:
            now = time.monotonic()

            with self._mu:
                cached = self._get_cached_locked(key, now)
                if cached is not None:
                    return cached

            try:
                infos = socket.getaddrinfo(host, port, type=socktype)
            except OSError:
                with self._mu:
                    if self._key_locks.get(key) is key_lock and key not in self._cache:
                        self._key_locks.pop(key, None)
                raise

            with self._mu:
                self._cache[key] = (time.monotonic(), infos)
                self._evict_if_needed_locked()

            return list(infos)


def _create_connection_cached(
    dns_cache: _DnsCache,
    host: str,
    port: int,
    timeout: float,
) -> socket.socket:
    infos = dns_cache.getaddrinfo(host, port, socktype=socket.SOCK_STREAM)
    if not infos:
        raise OSError(f"getaddrinfo returned no results for {host!r}:{port}")

    last_exc: OSError | None = None

    for family, socktype, proto, _, addr in infos:
        try:
            sock = socket.socket(family, socktype, proto)
        except OSError as exc:
            last_exc = exc
            continue

        try:
            sock.settimeout(timeout)
            sock.connect(addr)
            return sock
        except OSError as exc:
            last_exc = exc
            with contextlib.suppress(OSError):
                sock.close()

    raise last_exc or OSError(f"could not connect to {host}:{port}")


class _FaultTolerantStdout:
    def __init__(self, inner: _WritableStream) -> None:
        self._inner = inner
        self._dead = threading.Event()

    def write(self, s: str) -> int:
        if self._dead.is_set():
            return 0

        try:
            return self._inner.write(s)
        except (OSError, ValueError):
            self._dead.set()
            return 0

    def flush(self) -> None:
        if self._dead.is_set():
            return

        try:
            self._inner.flush()
        except (OSError, ValueError):
            self._dead.set()

    @property
    def is_dead(self) -> bool:
        return self._dead.is_set()

    def mark_dead(self) -> None:
        self._dead.set()


class _FrameWriter:
    _STOP = object()

    def __init__(self) -> None:
        self._out = _FaultTolerantStdout(sys.stdout)
        self._ctrl: queue.SimpleQueue[str | object] = queue.SimpleQueue()
        self._data: queue.Queue[str | object] = queue.Queue(
            maxsize=CONFIG.stdout_data_queue_cap
        )

        self._telemetry_lock = threading.Lock()
        self._latest_telemetry: str | None = None

        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="stdout-writer",
        )
        self._thread.start()

    def emit_ctrl(self, line: str) -> None:
        if self._stopping.is_set() or self._out.is_dead:
            return

        self._ctrl.put(line)

    def emit_data(self, line: str) -> None:
        if self._stopping.is_set() or self._out.is_dead:
            return

        deadline = time.monotonic() + CONFIG.emit_data_hard_deadline_secs

        while (
            not self._out.is_dead
            and not self._stopping.is_set()
            and not _TERMINATE.is_set()
        ):
            try:
                self._data.put(line, timeout=CONFIG.emit_data_put_timeout_secs)
                return
            except queue.Full:
                if time.monotonic() >= deadline:
                    _log(
                        "warning",
                        "emit_data deadline %.1fs exceeded; marking stdout dead",
                        CONFIG.emit_data_hard_deadline_secs,
                    )
                    self._out.mark_dead()
                    return

    def emit_telemetry(self, line: str) -> None:
        if self._stopping.is_set() or self._out.is_dead:
            return

        if self.data_queue_size >= CONFIG.stdout_backpressure_threshold:
            return

        with self._telemetry_lock:
            self._latest_telemetry = line

    def _pop_telemetry(self) -> str | None:
        if self.data_queue_size >= CONFIG.stdout_backpressure_threshold:
            return None

        with self._telemetry_lock:
            line = self._latest_telemetry
            self._latest_telemetry = None
            return line

    def stop(self) -> None:
        if self._stopping.is_set():
            return

        self._stopping.set()
        self._ctrl.put(self._STOP)

        with contextlib.suppress(queue.Full):
            self._data.put_nowait(self._STOP)

        self._thread.join(timeout=CONFIG.writer_join_timeout_secs)

    @property
    def data_queue_size(self) -> int:
        return self._data.qsize()

    @property
    def is_dead(self) -> bool:
        return self._out.is_dead

    def _write_line(self, line: str) -> bool:
        self._out.write(line + "\n")
        if self._out.is_dead:
            return False

        _record_tx_frame(line)
        return True

    def _run(self) -> None:
        while True:
            if self._out.is_dead:
                return

            saw_activity = False

            try:
                item = self._ctrl.get(timeout=CONFIG.writer_ctrl_poll_secs)
            except queue.Empty:
                item = None

            ctrl_processed = 0
            while item is not None and ctrl_processed < CONFIG.writer_ctrl_batch_size:
                if item is self._STOP:
                    self._drain_remaining()
                    return

                if isinstance(item, str):
                    if not self._write_line(item):
                        return
                    saw_activity = True

                ctrl_processed += 1

                try:
                    item = self._ctrl.get_nowait()
                except queue.Empty:
                    item = None

            if item is not None:
                self._ctrl.put(item)

            data_processed = 0
            while data_processed < CONFIG.writer_data_batch_size:
                if self._out.is_dead:
                    return

                try:
                    d = self._data.get_nowait()
                except queue.Empty:
                    break

                if d is self._STOP:
                    self._ctrl.put(self._STOP)
                    break

                if isinstance(d, str):
                    if not self._write_line(d):
                        return
                    saw_activity = True

                data_processed += 1

            telemetry = self._pop_telemetry()
            if telemetry is not None and not self._out.is_dead:
                if not self._write_line(telemetry):
                    return
                saw_activity = True

            if saw_activity:
                self._out.flush()

    def _drain_remaining(self) -> None:
        while not self._out.is_dead:
            try:
                item = self._ctrl.get_nowait()
            except queue.Empty:
                break

            if item is self._STOP:
                continue

            if isinstance(item, str):
                self._write_line(item)

        while not self._out.is_dead:
            try:
                item = self._data.get_nowait()
            except queue.Empty:
                break

            if item is self._STOP:
                continue

            if isinstance(item, str):
                self._write_line(item)

        telemetry = self._pop_telemetry()
        if telemetry is not None and not self._out.is_dead:
            self._write_line(telemetry)

        self._out.flush()


_writer: _FrameWriter | None = None


def _emit_direct(line: str) -> None:
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        _record_tx_frame(line)
    except (OSError, ValueError):
        return


def _emit_ctrl(line: str) -> None:
    if _writer is not None:
        _writer.emit_ctrl(line)
        return

    _emit_direct(line)


def _emit_data(line: str) -> None:

    if len(line) > CONFIG.max_outbound_frame_len:
        _log(
            "warning",
            "dropping oversized outbound DATA frame: %d chars > %d",
            len(line),
            CONFIG.max_outbound_frame_len,
        )
        return

    if _writer is not None:
        _writer.emit_data(line)
        return

    _emit_direct(line)


def _emit_telemetry(line: str) -> None:
    if len(line) > CONFIG.max_outbound_frame_len:
        return
    if _writer is not None:
        _writer.emit_telemetry(line)


def _stdout_backpressure_active() -> bool:
    if _writer is None:
        return False

    if _writer.is_dead:
        return True

    return _writer.data_queue_size >= CONFIG.stdout_backpressure_threshold


def _stdout_is_dead() -> bool:
    return _writer is not None and _writer.is_dead


def _send_all_nonblocking(sock: socket.socket, data: bytes) -> bool:
    if not data:
        return True

    view = memoryview(data)
    offset = 0

    while offset < len(view):
        if _TERMINATE.is_set():
            return False

        try:
            sent = sock.send(view[offset:])
            if sent == 0:
                return False
            offset += sent
        except InterruptedError:
            continue
        except BlockingIOError:
            try:
                _, writable, _ = select.select(
                    [],
                    [sock],
                    [],
                    CONFIG.send_writable_timeout_secs,
                )
            except OSError:
                return False

            if not writable:
                return False
        except OSError:
            return False

    return True


class TcpConnectionWorker:
    def __init__(
        self,
        conn_id: str,
        host: str,
        port: int,
        dns_cache: _DnsCache,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self.conn_id = conn_id
        self._host = host
        self._port = port
        self._dns_cache = dns_cache
        self._on_close = on_close

        self._sock: socket.socket | None = None
        self._inbound: list[bytes] = []
        self._inbound_bytes = 0
        self._inbound_lock = threading.Lock()
        self._final_closed = False

        self._notify_r, self._notify_w = os.pipe()
        os.set_blocking(self._notify_r, False)
        os.set_blocking(self._notify_w, False)
        self._pipe_lock = threading.Lock()
        self._pipe_closed = False

        self._closed = threading.Event()
        self._aborted = threading.Event()
        self._saturated = False

        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"tcp-conn-{conn_id}",
        )

    def dispose_unstarted(self) -> None:
        self._close_pipe()
        with self._inbound_lock:
            self._final_closed = True
            self._inbound.clear()
            self._inbound_bytes = 0

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout=timeout)

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _wake(self) -> None:
        with self._pipe_lock:
            if self._pipe_closed:
                return

            with contextlib.suppress(OSError):
                os.write(self._notify_w, b"\x00")

    def _close_pipe(self) -> None:
        with self._pipe_lock:
            if self._pipe_closed:
                return

            self._pipe_closed = True

            with contextlib.suppress(OSError):
                os.close(self._notify_r)

            with contextlib.suppress(OSError):
                os.close(self._notify_w)

    def feed(self, data: bytes) -> None:
        saturated = False

        with self._inbound_lock:
            pending_bytes = self._inbound_bytes + len(data)

            if (
                len(self._inbound) >= CONFIG.max_tcp_inbound_chunks
                or pending_bytes > CONFIG.max_tcp_inbound_bytes
            ):
                _log(
                    "debug",
                    "conn %s late feed dropped (%d bytes)",
                    self.conn_id,
                    len(data),
                )
                return

            if len(self._inbound) >= CONFIG.max_tcp_inbound_chunks:
                self._saturated = True
                self._closed.set()
                saturated = True
            else:
                self._inbound.append(data)
                self._inbound_bytes = pending_bytes

        if saturated:
            _emit_ctrl(_make_error_frame(self.conn_id, "agent tcp inbound saturated"))
            _log("warning", "conn %s inbound saturated; closing", self.conn_id)
            self._wake()
            return

        self._wake()

    def signal_eof(self) -> None:
        self._closed.set()
        self._wake()

    def abort(self) -> None:
        """Hard-close this TCP connection.

        This is intentionally stronger than signal_eof(). It is used when the
        client-side receiver aborts a connection because its local inbound queue
        is saturated. In that case a graceful half-close would keep the pod
        reading remote data and would continue to flood stdout.
        """
        self._aborted.set()
        self._closed.set()

        sock = self._sock
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()

        self._wake()

    def _take_inbound(self) -> list[bytes]:
        with self._inbound_lock:
            if not self._inbound:
                return []

            pending = self._inbound
            self._inbound = []
            self._inbound_bytes = 0
            return pending

    def _invoke_on_close(self) -> None:
        if self._on_close is None:
            return

        try:
            self._on_close()
        except Exception as exc:
            _log("debug", "conn %s on_close callback failed: %s", self.conn_id, exc)

    def _run(self) -> None:
        cid = self.conn_id
        sock: socket.socket | None = None
        established = False

        try:
            try:
                sock = _create_connection_cached(
                    self._dns_cache,
                    self._host,
                    self._port,
                    timeout=CONFIG.tcp_connect_timeout_secs,
                )
                sock.setblocking(False)
            except OSError as exc:
                _emit_ctrl(_make_error_frame(cid, str(exc)))
                _log("warning", "conn %s connect failed: %s", cid, exc)
                return

            if (
                self._closed.is_set()
                or self._aborted.is_set()
                or _TERMINATE.is_set()
                or _stdout_is_dead()
            ):
                return

            self._sock = sock
            established = True
            _emit_ctrl(_make_frame("CONN_ACK", cid))

            self._io_loop(sock, cid)
        finally:
            with self._inbound_lock:
                dropped = sum(len(c) for c in self._inbound)
                self._inbound.clear()
                self._inbound_bytes = 0
                self._final_closed = True

            if dropped:
                _log("debug", "conn %s closed with %d undelivered bytes", cid, dropped)

            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()

            self._close_pipe()

            if established:
                _emit_ctrl(_make_frame("CONN_CLOSE", cid))

            self._invoke_on_close()

    def _drain_notify(self) -> None:
        with contextlib.suppress(OSError):
            os.read(self._notify_r, 4096)

    def _io_loop(self, sock: socket.socket, cid: str) -> None:
        local_shut = False
        local_shut_deadline: float | None = None

        while (
            not self._aborted.is_set()
            and not _TERMINATE.is_set()
            and not _stdout_is_dead()
        ):
            read_fds: list[int | socket.socket] = [self._notify_r]
            if not _stdout_backpressure_active():
                read_fds.append(sock)

            try:
                readable, _, errored = select.select(
                    read_fds,
                    [],
                    [sock],
                    CONFIG.select_timeout_secs,
                )
            except (OSError, ValueError):
                break

            if errored:
                break

            remote_closed = False

            if sock in readable:
                if self._aborted.is_set():
                    break
                try:
                    chunk = sock.recv(CONFIG.tcp_recv_chunk_bytes_safe)
                except (InterruptedError, BlockingIOError):
                    chunk = None
                except OSError:
                    break

                if chunk == b"":
                    remote_closed = True
                elif chunk is not None:
                    _emit_data(_make_frame("DATA", cid, _b64encode(chunk)))

            if self._notify_r in readable:
                self._drain_notify()

            if not local_shut:
                pending = self._take_inbound()
                for chunk in pending:
                    if not _send_all_nonblocking(sock, chunk):
                        return

            if remote_closed:
                if not local_shut:
                    remaining = self._take_inbound()
                    for chunk in remaining:
                        if not _send_all_nonblocking(sock, chunk):
                            break

                break

            if self._closed.is_set() and not local_shut:
                with self._inbound_lock:
                    still_pending = bool(self._inbound)

                if not still_pending:
                    local_shut = True
                    local_shut_deadline = (
                        time.monotonic() + CONFIG.half_close_deadline_secs
                    )

                    with contextlib.suppress(OSError):
                        sock.shutdown(socket.SHUT_WR)

            if (
                local_shut
                and local_shut_deadline is not None
                and time.monotonic() >= local_shut_deadline
            ):
                break


class UdpFlowWorker:
    def __init__(
        self,
        flow_id: str,
        host: str,
        port: int,
        dns_cache: _DnsCache,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._id = flow_id
        self._host = host
        self._port = port
        self._dns_cache = dns_cache
        self._on_close = on_close

        self._sock: socket.socket | None = None
        self._inbound: list[bytes] = []
        self._inbound_bytes = 0
        self._inbound_lock = threading.Lock()
        self._final_closed = False

        self._notify_r, self._notify_w = os.pipe()
        os.set_blocking(self._notify_r, False)
        os.set_blocking(self._notify_w, False)
        self._pipe_lock = threading.Lock()
        self._pipe_closed = False

        self._closed = threading.Event()
        self._drop_count = 0
        self._send_drop_count = 0

        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"udp-flow-{flow_id}",
        )

    def dispose_unstarted(self) -> None:
        self._close_pipe()
        with self._inbound_lock:
            self._final_closed = True
            self._inbound.clear()
            self._inbound_bytes = 0

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout=timeout)

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _wake(self) -> None:
        with self._pipe_lock:
            if self._pipe_closed:
                return

            with contextlib.suppress(OSError):
                os.write(self._notify_w, b"\x00")

    def _close_pipe(self) -> None:
        with self._pipe_lock:
            if self._pipe_closed:
                return

            self._pipe_closed = True

            with contextlib.suppress(OSError):
                os.close(self._notify_r)

            with contextlib.suppress(OSError):
                os.close(self._notify_w)

    def feed(self, data: bytes) -> None:
        if len(data) > CONFIG.max_udp_dgram_bytes:
            return

        with self._inbound_lock:
            if self._final_closed or self._closed.is_set():
                return
            pending_bytes = self._inbound_bytes + len(data)
            if (
                len(self._inbound) >= CONFIG.max_udp_inbound_dgrams
                or pending_bytes > CONFIG.max_udp_inbound_bytes
            ):
                self._drop_count += 1
                if (
                    self._drop_count == 1
                    or self._drop_count % CONFIG.udp_drop_warn_every == 0
                ):
                    _log(
                        "warning",
                        "udp flow %s inbound saturated; dropping datagram drops=%d",
                        self._id,
                        self._drop_count,
                    )
                return

            self._inbound.append(data)
            self._inbound_bytes = pending_bytes

        self._wake()

    def close(self) -> None:
        self._closed.set()
        self._wake()

    def _take_inbound(self) -> list[bytes]:
        with self._inbound_lock:
            if not self._inbound:
                return []

            pending = self._inbound
            self._inbound = []
            self._inbound_bytes = 0
            return pending

    def _invoke_on_close(self) -> None:
        if self._on_close is None:
            return

        try:
            self._on_close()
        except Exception as exc:
            _log("debug", "udp flow %s on_close callback failed: %s", self._id, exc)

    def _note_send_drop(self, fid: str) -> None:
        self._send_drop_count += 1
        if (
            self._send_drop_count == 1
            or self._send_drop_count % CONFIG.udp_drop_warn_every == 0
        ):
            _log(
                "debug",
                "udp flow %s send drops=%d",
                fid,
                self._send_drop_count,
            )

    def _run(self) -> None:
        fid = self._id
        sock: socket.socket | None = None
        close_frame_emitted = False

        try:
            try:
                infos = self._dns_cache.getaddrinfo(
                    self._host,
                    self._port,
                    socktype=socket.SOCK_DGRAM,
                )
                if not infos:
                    raise OSError(f"could not resolve {self._host!r}")
            except OSError as exc:
                _emit_ctrl(_make_frame("UDP_CLOSE", fid))
                close_frame_emitted = True
                _log("warning", "udp flow %s resolve failed: %s", fid, exc)
                return

            last_exc: OSError | None = None

            for family, _, proto, _, addr in infos:
                try:
                    candidate = socket.socket(family, socket.SOCK_DGRAM, proto)
                except OSError as exc:
                    last_exc = exc
                    continue

                try:
                    candidate.setblocking(False)
                    candidate.connect(addr)
                    sock = candidate
                    break
                except OSError as exc:
                    last_exc = exc
                    with contextlib.suppress(OSError):
                        candidate.close()

            if sock is None:
                _emit_ctrl(_make_frame("UDP_CLOSE", fid))
                close_frame_emitted = True
                _log("warning", "udp flow %s open failed: %s", fid, last_exc)
                return

            self._sock = sock
            self._io_loop(sock, fid)
        finally:
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()

            self._close_pipe()

            with self._inbound_lock:
                self._final_closed = True
                self._inbound.clear()
                self._inbound_bytes = 0

            if not close_frame_emitted:
                _emit_ctrl(_make_frame("UDP_CLOSE", fid))

            self._invoke_on_close()

    def _drain_notify(self) -> None:
        with contextlib.suppress(OSError):
            os.read(self._notify_r, 4096)

    def _io_loop(self, sock: socket.socket, fid: str) -> None:
        transient_send_errors = {
            errno.ECONNREFUSED,
            errno.EMSGSIZE,
            errno.ENETUNREACH,
            errno.EHOSTUNREACH,
            errno.ENETDOWN,
            errno.EHOSTDOWN,
        }
        transient_recv_errors = {
            errno.EAGAIN,
            errno.EWOULDBLOCK,
            errno.ECONNREFUSED,
            errno.ENETUNREACH,
            errno.EHOSTUNREACH,
            errno.ENETDOWN,
            errno.EHOSTDOWN,
        }
        last_activity = time.monotonic()
        while (
            not self._closed.is_set()
            and not _TERMINATE.is_set()
            and not _stdout_is_dead()
        ):
            if (
                CONFIG.udp_idle_timeout_secs > 0
                and time.monotonic() - last_activity >= CONFIG.udp_idle_timeout_secs
            ):
                _log("debug", "udp flow %s idle timeout; closing", fid)
                break
            read_fds: list[int | socket.socket] = [self._notify_r]
            if not _stdout_backpressure_active():
                read_fds.append(sock)

            try:
                readable, _, errored = select.select(
                    read_fds,
                    [],
                    [sock],
                    CONFIG.select_timeout_secs,
                )
            except (OSError, ValueError):
                break

            if errored:
                break

            if sock in readable:
                try:
                    chunk = sock.recv(CONFIG.max_udp_dgram_bytes)
                except (InterruptedError, BlockingIOError):
                    chunk = None
                except OSError as exc:
                    if exc.errno in transient_recv_errors:
                        chunk = None
                    else:
                        break

                if chunk is not None:
                    last_activity = time.monotonic()
                    if len(chunk) > CONFIG.max_udp_data_payload_bytes:
                        self._note_send_drop(fid)
                    else:
                        _emit_data(_make_frame("UDP_DATA", fid, _b64encode(chunk)))

            if self._notify_r in readable:
                self._drain_notify()

            pending = self._take_inbound()
            if pending:
                last_activity = time.monotonic()
            for dgram in pending:
                if len(dgram) > CONFIG.max_udp_dgram_bytes:
                    self._note_send_drop(fid)
                    continue

                try:
                    sent = sock.send(dgram)
                    if sent != len(dgram):
                        self._note_send_drop(fid)
                    else:
                        last_activity = time.monotonic()
                except (InterruptedError, BlockingIOError):
                    self._note_send_drop(fid)
                except OSError as exc:
                    if exc.errno in transient_send_errors:
                        self._note_send_drop(fid)
                        continue
                    return


class _Dispatcher:
    def __init__(self, fixed_host: str | None, fixed_port: int | None) -> None:
        self._fixed_host = fixed_host
        self._fixed_port = fixed_port

        self._conn_map: dict[str, TcpConnectionWorker] = {}
        self._udp_map: dict[str, UdpFlowWorker] = {}
        self._conn_lock = threading.Lock()
        self._udp_lock = threading.Lock()
        self._dns_cache = _DnsCache()

        self._stats_sampler: _StatsSampler | None = None

    def set_stats_sampler(self, sampler: _StatsSampler) -> None:
        self._stats_sampler = sampler

    def dispatch(self, line: str) -> None:
        _record_rx_frame(line)

        started = time.monotonic()
        try:
            self._dispatch_inner(line)
        finally:
            _record_dispatch_sample(time.monotonic() - started)

    def _dispatch_inner(self, line: str) -> None:
        parts = _parse_frame_line(line)
        if parts is None:
            return

        match parts[0]:
            case "CONN_OPEN":
                self._on_conn_open(parts)
            case "DATA":
                self._on_data(parts)
            case "CONN_CLOSE":
                self._on_conn_close(parts)
            case "UDP_OPEN":
                self._on_udp_open(parts)
            case "UDP_DATA":
                self._on_udp_data(parts)
            case "UDP_CLOSE":
                self._on_udp_close(parts)
            case "ERROR":
                self._on_error(parts)
            case "KEEPALIVE":
                return
            case "STATS_REQ":
                self._on_stats_req()
            case _:
                _log("debug", "unknown frame type ignored: %s", parts[0])

    def worker_counts(self) -> tuple[int, int]:
        with self._conn_lock:
            tcp = len(self._conn_map)

        with self._udp_lock:
            udp = len(self._udp_map)

        return tcp, udp

    def shutdown(self) -> None:
        with self._conn_lock:
            tcp_workers = list(self._conn_map.values())

        with self._udp_lock:
            udp_workers = list(self._udp_map.values())

        for worker in tcp_workers:
            worker.signal_eof()

        for flow in udp_workers:
            flow.close()

        deadline = time.monotonic() + CONFIG.worker_shutdown_grace_secs

        for worker in tcp_workers:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            worker.join(timeout=remaining)

        for flow in udp_workers:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            flow.join(timeout=remaining)

        still_alive = sum(1 for w in tcp_workers if w.is_alive) + sum(
            1 for f in udp_workers if f.is_alive
        )

        if still_alive:
            _log(
                "debug",
                "shutdown: %d worker(s) still alive after grace period",
                still_alive,
            )

    def _on_stats_req(self) -> None:
        if not CONFIG.stats_enabled:
            return

        if self._stats_sampler is not None:
            self._stats_sampler.emit_once()

    def _on_conn_open(self, parts: list[str]) -> None:
        if len(parts) < _MIN_PARTS_WITH_CONN_ID:
            return

        cid = parts[1]
        if not _is_valid_id(cid):
            _log("debug", "CONN_OPEN invalid conn_id: %r", cid)
            return

        with self._conn_lock:
            if cid in self._conn_map:
                _emit_ctrl(_make_error_frame(cid, "duplicate CONN_OPEN"))
                return

            if len(self._conn_map) >= CONFIG.max_tcp_workers:
                _emit_ctrl(
                    _make_error_frame(cid, "agent: too many concurrent connections")
                )
                return

        if self._fixed_host is not None:
            if self._fixed_port is None:
                _emit_ctrl(_make_error_frame(cid, "portforward: fixed_port not set"))
                return

            host, port = self._fixed_host, self._fixed_port
        elif len(parts) >= _MIN_PARTS_WITH_PAYLOAD:
            parsed = _parse_host_port(parts[_PAYLOAD_PART_INDEX], cid, "CONN_OPEN")
            if parsed is None:
                _emit_ctrl(
                    _make_error_frame(
                        cid,
                        f"invalid CONN_OPEN host:port: {parts[_PAYLOAD_PART_INDEX]!r}",
                    )
                )
                return

            host, port = parsed
        else:
            _emit_ctrl(_make_error_frame(cid, "CONN_OPEN missing host:port"))
            return

        def on_close(conn_id: str = cid) -> None:
            with self._conn_lock:
                self._conn_map.pop(conn_id, None)

        try:
            worker = TcpConnectionWorker(
                cid,
                host,
                port,
                dns_cache=self._dns_cache,
                on_close=on_close,
            )
        except OSError as exc:
            _emit_ctrl(_make_error_frame(cid, f"agent: could not create worker: {exc}"))
            return

        with self._conn_lock:
            if cid in self._conn_map:
                worker.dispose_unstarted()
                _emit_ctrl(_make_error_frame(cid, "duplicate CONN_OPEN"))
                return

            if len(self._conn_map) >= CONFIG.max_tcp_workers:
                worker.dispose_unstarted()
                _emit_ctrl(
                    _make_error_frame(cid, "agent: too many concurrent connections")
                )
                return

            self._conn_map[cid] = worker

        try:
            worker.start()
        except RuntimeError as exc:
            with self._conn_lock:
                if self._conn_map.get(cid) is worker:
                    self._conn_map.pop(cid, None)

            worker.dispose_unstarted()
            _emit_ctrl(_make_error_frame(cid, f"agent: could not start worker: {exc}"))

    def _on_data(self, parts: list[str]) -> None:
        if len(parts) < _MIN_PARTS_WITH_PAYLOAD:
            return

        cid = parts[1]
        if not _is_valid_id(cid):
            return

        with self._conn_lock:
            worker = self._conn_map.get(cid)

        if worker is None:
            return

        try:
            data = _b64decode(parts[_PAYLOAD_PART_INDEX])
        except ValueError:
            _emit_ctrl(_make_error_frame(cid, "invalid base64url in DATA frame"))
            return

        worker.feed(data)

    def _on_conn_close(self, parts: list[str]) -> None:
        if len(parts) < _MIN_PARTS_WITH_CONN_ID:
            return

        cid = parts[1]
        if not _is_valid_id(cid):
            return

        with self._conn_lock:
            worker = self._conn_map.get(cid)

        if worker is not None:
            worker.signal_eof()

    def _on_error(self, parts: list[str]) -> None:
        """Handle a client-originated hard error.

        The current client receiver sends ERROR followed by CONN_CLOSE when a
        local inbound queue saturates. CONN_CLOSE is graceful EOF; ERROR is the
        hard-abort signal. Older agents ignored ERROR, which allowed remote TCP
        sockets to keep producing DATA after the client had already aborted.
        """
        if len(parts) < _MIN_PARTS_WITH_CONN_ID:
            return

        tunnel_id = parts[1]
        if not _is_valid_id(tunnel_id):
            return

        reason = ""
        if len(parts) >= _MIN_PARTS_WITH_PAYLOAD:
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                reason = _b64decode(parts[_PAYLOAD_PART_INDEX]).decode(
                    "utf-8",
                    "replace",
                )

        if tunnel_id.startswith("c"):
            with self._conn_lock:
                worker = self._conn_map.get(tunnel_id)
            if worker is not None:
                _log("debug", "conn %s hard abort requested: %s", tunnel_id, reason)
                worker.abort()
            return

        if tunnel_id.startswith("u"):
            with self._udp_lock:
                flow = self._udp_map.get(tunnel_id)
            if flow is not None:
                _log(
                    "debug",
                    "udp flow %s close requested by ERROR: %s",
                    tunnel_id,
                    reason,
                )
                flow.close()

    def _on_udp_open(self, parts: list[str]) -> None:
        if len(parts) < _MIN_PARTS_WITH_PAYLOAD:
            return

        fid = parts[1]
        if not _is_valid_id(fid):
            _log("debug", "UDP_OPEN invalid flow_id: %r", fid)
            return

        parsed = _parse_host_port(parts[_PAYLOAD_PART_INDEX], fid, "UDP_OPEN")
        if parsed is None:
            _emit_ctrl(_make_frame("UDP_CLOSE", fid))
            return

        host, port = parsed

        with self._udp_lock:
            if fid in self._udp_map:
                _emit_ctrl(_make_frame("UDP_CLOSE", fid))
                return

            if len(self._udp_map) >= CONFIG.max_udp_workers:
                _emit_ctrl(_make_frame("UDP_CLOSE", fid))
                return

        def on_close(flow_id: str = fid) -> None:
            with self._udp_lock:
                self._udp_map.pop(flow_id, None)

        try:
            flow = UdpFlowWorker(
                fid,
                host,
                port,
                dns_cache=self._dns_cache,
                on_close=on_close,
            )
        except OSError as exc:
            _log("warning", "udp flow %s worker create failed: %s", fid, exc)
            _emit_ctrl(_make_frame("UDP_CLOSE", fid))
            return

        with self._udp_lock:
            if fid in self._udp_map:
                flow.dispose_unstarted()
                _emit_ctrl(_make_frame("UDP_CLOSE", fid))
                return

            if len(self._udp_map) >= CONFIG.max_udp_workers:
                flow.dispose_unstarted()
                _emit_ctrl(_make_frame("UDP_CLOSE", fid))
                return

            self._udp_map[fid] = flow

        try:
            flow.start()
        except RuntimeError as exc:
            with self._udp_lock:
                if self._udp_map.get(fid) is flow:
                    self._udp_map.pop(fid, None)

            flow.dispose_unstarted()
            _log("warning", "udp flow %s worker start failed: %s", fid, exc)
            _emit_ctrl(_make_frame("UDP_CLOSE", fid))

    def _on_udp_data(self, parts: list[str]) -> None:
        if len(parts) < _MIN_PARTS_WITH_PAYLOAD:
            return

        fid = parts[1]
        if not _is_valid_id(fid):
            return

        with self._udp_lock:
            flow = self._udp_map.get(fid)

        if flow is None:
            return

        try:
            data = _b64decode(parts[_PAYLOAD_PART_INDEX])
        except ValueError:
            flow.close()
            return

        flow.feed(data)

    def _on_udp_close(self, parts: list[str]) -> None:
        if len(parts) < _MIN_PARTS_WITH_CONN_ID:
            return

        fid = parts[1]
        if not _is_valid_id(fid):
            return

        with self._udp_lock:
            flow = self._udp_map.get(fid)

        if flow is not None:
            flow.close()

    def run(self) -> None:
        while not _TERMINATE.is_set():
            try:
                raw_line = sys.stdin.readline()
            except OSError:
                break

            if raw_line == "":
                break

            self.dispatch(raw_line.rstrip("\n\r"))

        _log("info", "stdin EOF; dispatcher exiting")


class _ApiStatsPusher:
    def __init__(self, url: str, timeout_secs: float) -> None:
        self._url = url
        self._timeout_secs = timeout_secs
        self._latest_lock = threading.Lock()
        self._latest_payload: bytes | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if CONFIG.stats_api_headers_json:
            try:
                parsed = json.loads(CONFIG.stats_api_headers_json)
                if isinstance(parsed, dict):
                    headers.update({str(k): str(v) for k, v in parsed.items()})
            except json.JSONDecodeError:
                _log(
                    "warning", "invalid EXECTUNNEL_AGENT_STATS_API_HEADERS_JSON ignored"
                )

        self._headers = headers
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="stats-api-pusher",
        )
        self._thread.start()

    def submit(self, snapshot: dict[str, object]) -> None:
        payload = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")

        with self._latest_lock:
            self._latest_payload = payload

        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=1.0)

    def _pop_latest(self) -> bytes | None:
        with self._latest_lock:
            payload = self._latest_payload
            self._latest_payload = None
            return payload

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=1.0)
            self._wake.clear()

            payload = self._pop_latest()
            if payload is None:
                continue

            req = urllib.request.Request(
                self._url,
                data=payload,
                headers=self._headers,
                method="POST",
            )

            try:
                with urllib.request.urlopen(req, timeout=self._timeout_secs) as resp:
                    resp.read(256)
            except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
                _log("debug", "stats API push failed: %s", exc)


class _StatsSampler:
    def __init__(
        self,
        dispatcher: _Dispatcher,
        interval_secs: float = CONFIG.stats_sample_interval_secs,
    ) -> None:
        self._dispatcher = dispatcher
        self._interval = interval_secs
        self._stop = threading.Event()
        self._api_pusher: _ApiStatsPusher | None = None

        if "api" in CONFIG.stats_sinks and CONFIG.stats_api_url:
            self._api_pusher = _ApiStatsPusher(
                CONFIG.stats_api_url,
                CONFIG.stats_api_timeout_secs,
            )

        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="stats-sampler",
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

        if self._api_pusher is not None:
            self._api_pusher.stop()

    def emit_once(self) -> None:
        self._emit_snapshot()

    def _snapshot(self) -> dict[str, object]:
        with _DISPATCH_SAMPLES_LOCK:
            samples = list(_DISPATCH_SAMPLES)

        samples.sort()
        n = len(samples)

        def pct(p: float) -> float:
            if n == 0:
                return 0.0

            idx = min(n - 1, int(p * (n - 1)))
            return samples[idx]

        tx_bytes, rx_bytes, frames_tx, frames_rx = _counter_snapshot()
        tcp_count, udp_count = self._dispatcher.worker_counts()

        return {
            "ts": time.time(),
            "agent_version": _AGENT_VERSION,
            "python_version": sys.version.split()[0],
            "pid": os.getpid(),
            "tx_bytes_total": tx_bytes,
            "rx_bytes_total": rx_bytes,
            "frames_tx_total": frames_tx,
            "frames_rx_total": frames_rx,
            "stdout_dead": _writer.is_dead if _writer is not None else False,
            "data_queue_depth": _writer.data_queue_size if _writer is not None else 0,
            "data_queue_cap": CONFIG.stdout_data_queue_cap,
            "tcp_worker_count": tcp_count,
            "udp_worker_count": udp_count,
            "tcp_worker_cap": CONFIG.max_tcp_workers,
            "udp_worker_cap": CONFIG.max_udp_workers,
            "dispatch_ms_p50": pct(0.50) * 1000.0,
            "dispatch_ms_p95": pct(0.95) * 1000.0,
            "dispatch_ms_p99": pct(0.99) * 1000.0,
            "dispatch_samples": n,
        }

    def _write_file_snapshot(self, snapshot: dict[str, object]) -> None:
        if not CONFIG.stats_file_path:
            return

        directory = os.path.dirname(CONFIG.stats_file_path) or "."
        basename = os.path.basename(CONFIG.stats_file_path)
        tmp_path: str | None = None

        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{basename}.",
                suffix=".tmp",
                dir=directory,
                text=True,
            )

            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(snapshot, fp, separators=(",", ":"))
                fp.write("\n")
                fp.flush()
                os.fsync(fp.fileno())

            os.replace(tmp_path, CONFIG.stats_file_path)
            tmp_path = None
        except OSError as exc:
            _log("debug", "stats file write failed: %s", exc)
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

    def _emit_snapshot(self) -> None:
        try:
            snapshot = self._snapshot()

            if "stdout" in CONFIG.stats_sinks:
                payload = _b64encode(
                    json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
                )
                _emit_telemetry(_make_frame("STATS", payload))

            if "file" in CONFIG.stats_sinks:
                self._write_file_snapshot(snapshot)

            if "api" in CONFIG.stats_sinks and self._api_pusher is not None:
                self._api_pusher.submit(snapshot)
        except Exception as exc:
            _log("debug", "stats snapshot failed: %s", exc)

    def _run(self) -> None:
        if self._stop.wait(timeout=self._interval):
            return

        while not self._stop.is_set() and not _TERMINATE.is_set():
            self._emit_snapshot()

            if self._stop.wait(timeout=self._interval):
                return


def _parse_args() -> tuple[str | None, int | None]:
    if len(sys.argv) == 1:
        return None, None

    if len(sys.argv) == _ARGC_FIXED_TARGET:
        fixed_host = sys.argv[1]
        if not fixed_host:
            sys.stderr.write("fixed host must not be empty\n")
            sys.exit(1)

        try:
            fixed_port = int(sys.argv[2], 10)
        except ValueError:
            sys.stderr.write(f"invalid port: {sys.argv[2]!r}\n")
            sys.exit(1)

        if fixed_port <= 0 or fixed_port > _MAX_TCP_UDP_PORT:
            sys.stderr.write(f"port out of range: {fixed_port}\n")
            sys.exit(1)

        return fixed_host, fixed_port

    sys.stderr.write("usage: exectunnel_agent.py [<host> <port>]\n")
    sys.exit(1)


def main() -> None:
    _install_sigpipe_handler()
    _install_termination_handlers()

    fixed_host, fixed_port = _parse_args()

    for warning in _BOOT_WARNINGS:
        _log("warning", warning)

    for path in _AGENT_PATHS:
        with contextlib.suppress(OSError):
            os.unlink(path)

    global _writer
    _disable_echo()
    _writer = _FrameWriter()

    _emit_ctrl(_make_frame("AGENT_READY"))

    dispatcher = _Dispatcher(fixed_host, fixed_port)
    stats_sampler: _StatsSampler | None = None

    if CONFIG.stats_enabled:
        stats_sampler = _StatsSampler(dispatcher)
        dispatcher.set_stats_sampler(stats_sampler)
        stats_sampler.start()

    try:
        dispatcher.run()
    finally:
        if stats_sampler is not None:
            stats_sampler.stop()

        dispatcher.shutdown()

        if _writer is not None:
            _writer.stop()


if __name__ == "__main__":
    main()
