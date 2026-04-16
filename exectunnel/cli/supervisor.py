"""exectunnel.manager — multi-tunnel supervisor."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import re
import shlex
import signal
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ManagerConfig",
    "TunnelMetrics",
    "TunnelSpec",
    "TunnelRuntimeState",
    "run_manager",
]

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────

_DEFAULT_SOCKS_BASE_PORT = 1081
_DEFAULT_RESTART_DELAY = 5.0
_DEFAULT_MAX_RESTART_DELAY = 60.0
_DEFAULT_BACKOFF_MULTIPLIER = 1.5
_DEFAULT_HEALTH_CHECK_INTERVAL = 15.0
_DEFAULT_MAX_HEALTH_FAILURES = 3
_DEFAULT_STARTUP_GRACE_PERIOD = 20.0
_VALID_LOG_LEVELS: frozenset[str] = frozenset({"debug", "info", "warning", "error"})
_TRUE_TOKENS: frozenset[str] = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS: frozenset[str] = frozenset({"0", "false", "no", "off", ""})

_PASSTHROUGH_ENV_PREFIXES: tuple[str, ...] = ("EXECTUNNEL_",)

_PER_TUNNEL_OVERRIDE_KEYS: tuple[str, ...] = (
    "EXECTUNNEL_WSS_URL",
    "EXECTUNNEL_TOKEN",
    "EXECTUNNEL_LOG_LEVEL",
    "EXECTUNNEL_PING_INTERVAL",
    "EXECTUNNEL_SEND_TIMEOUT",
    "EXECTUNNEL_SEND_QUEUE_CAP",
    "EXECTUNNEL_RECONNECT_MAX_RETRIES",
    "EXECTUNNEL_RECONNECT_BASE_DELAY",
    "EXECTUNNEL_RECONNECT_MAX_DELAY",
    "EXECTUNNEL_DNS_MAX_INFLIGHT",
    "EXECTUNNEL_DNS_UPSTREAM_PORT",
    "EXECTUNNEL_DNS_QUERY_TIMEOUT",
    "EXECTUNNEL_BOOTSTRAP_DELIVERY",
    "EXECTUNNEL_BOOTSTRAP_USE_GO_AGENT",
    "EXECTUNNEL_FETCH_AGENT_URL",
    "EXECTUNNEL_WSS_INSECURE",
)


# ── Data models ───────────────────────────────────────────────────────────────


@dataclass
class TunnelSpec:
    """Static configuration for a single managed tunnel."""

    index: int
    wss_url: str
    socks_port: int
    socks_host: str = "127.0.0.1"
    insecure: bool = False
    log_level: str = "info"
    label: str = ""
    env_overrides: dict[str, str] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.label or f"tunnel-{self.index}"


@dataclass
class TunnelMetrics:
    """Metrics snapshot received from a managed tunnel subprocess.

    Fields mirror :meth:`TunnelHealth.to_report_dict` keys.
    """

    connected: bool = False
    bootstrap_ok: bool = False
    socks_ok: bool = False
    uptime_secs: float = 0.0
    # Frames
    frames_sent: int = 0
    frames_recv: int = 0
    frames_dropped: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    # Frame errors
    frames_decode_errors: int = 0
    frames_orphaned: int = 0
    frames_noise: int = 0
    # TCP
    tcp_open: int = 0
    tcp_pending: int = 0
    tcp_total: int = 0
    tcp_failed: int = 0
    tcp_completed: int = 0
    tcp_errors: int = 0
    # UDP
    udp_open: int = 0
    udp_total: int = 0
    udp_flows_opened: int = 0
    udp_flows_closed: int = 0
    udp_datagrams_sent: int = 0
    udp_datagrams_accepted: int = 0
    udp_datagrams_dropped: int = 0
    # ACK
    ack_ok: int = 0
    ack_timeout: int = 0
    ack_failed: int = 0
    # DNS
    dns_enabled: bool = False
    dns_queries: int = 0
    dns_ok: int = 0
    dns_dropped: int = 0
    # SOCKS5
    socks5_accepted: int = 0
    socks5_rejected: int = 0
    socks5_active: int = 0
    socks5_handshakes_ok: int = 0
    socks5_handshakes_error: int = 0
    socks5_cmd_connect: int = 0
    socks5_cmd_udp: int = 0
    socks5_udp_relays_active: int = 0
    socks5_udp_datagrams: int = 0
    socks5_udp_dropped: int = 0
    # Session
    session_connect_attempts: int = 0
    session_connect_ok: int = 0
    session_reconnects: int = 0
    reconnect_delay_avg: float | None = None
    reconnect_delay_max: float | None = None
    session_serve_started: int = 0
    session_serve_stopped: int = 0
    # Cleanup
    cleanup_tcp: int = 0
    cleanup_pending: int = 0
    cleanup_udp: int = 0
    # Bootstrap
    bootstrap_duration: float | None = None
    # Queue & tasks
    send_queue_depth: int = 0
    send_queue_cap: int = 0
    request_tasks: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> TunnelMetrics:
        """Build from a JSON-decoded dict, ignoring unknown keys."""
        valid = cls.__dataclass_fields__
        cleaned: dict[str, object] = {}
        for key in valid:
            if key not in d:
                continue
            value = d[key]
            try:
                if key in _BOOL_METRIC_FIELDS:
                    cleaned[key] = _coerce_bool_metric(value)
                elif key in _INT_METRIC_FIELDS:
                    cleaned[key] = int(value)
                elif key in _FLOAT_METRIC_FIELDS:
                    cleaned[key] = float(value)
                else:
                    cleaned[key] = value
            except (TypeError, ValueError):
                continue
        return cls(**cleaned)


def _coerce_bool_metric(value: object) -> bool:
    """Coerce manager metrics payload values to bool safely."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
    return False


_BOOL_METRIC_FIELDS: frozenset[str] = frozenset({
    "connected",
    "bootstrap_ok",
    "socks_ok",
    "dns_enabled",
})

_INT_METRIC_FIELDS: frozenset[str] = frozenset({
    "frames_sent",
    "frames_recv",
    "frames_dropped",
    "bytes_up",
    "bytes_down",
    "frames_decode_errors",
    "frames_orphaned",
    "frames_noise",
    "tcp_open",
    "tcp_pending",
    "tcp_total",
    "tcp_failed",
    "tcp_completed",
    "tcp_errors",
    "udp_open",
    "udp_total",
    "udp_flows_opened",
    "udp_flows_closed",
    "udp_datagrams_sent",
    "udp_datagrams_accepted",
    "udp_datagrams_dropped",
    "ack_ok",
    "ack_timeout",
    "ack_failed",
    "dns_queries",
    "dns_ok",
    "dns_dropped",
    "socks5_accepted",
    "socks5_rejected",
    "socks5_active",
    "socks5_handshakes_ok",
    "socks5_handshakes_error",
    "socks5_cmd_connect",
    "socks5_cmd_udp",
    "socks5_udp_relays_active",
    "socks5_udp_datagrams",
    "socks5_udp_dropped",
    "session_connect_attempts",
    "session_connect_ok",
    "session_reconnects",
    "session_serve_started",
    "session_serve_stopped",
    "cleanup_tcp",
    "cleanup_pending",
    "cleanup_udp",
    "send_queue_depth",
    "send_queue_cap",
    "request_tasks",
})

_FLOAT_METRIC_FIELDS: frozenset[str] = frozenset({
    "uptime_secs",
    "reconnect_delay_avg",
    "reconnect_delay_max",
    "bootstrap_duration",
})


_DEFAULT_LOG_LINES_CAP = 200


@dataclass
class TunnelRuntimeState:
    """Live runtime state for a single managed tunnel."""

    spec: TunnelSpec
    status: str = (
        "starting"  # starting | running | healthy | unhealthy | restarting | stopped
    )
    pid: int | None = None
    socks_ok: bool = False
    restart_count: int = 0
    health_failures: int = 0  # total lifetime health failures
    consecutive_health_failures: int = 0
    uptime_secs: float = 0.0
    next_restart_in: float = 0.0
    exit_code: int | None = None
    metrics: TunnelMetrics = field(default_factory=TunnelMetrics)
    log_lines: deque[str] = field(
        default_factory=lambda: deque(maxlen=_DEFAULT_LOG_LINES_CAP)
    )

    @property
    def display_name(self) -> str:
        return self.spec.display_name


@dataclass
class ManagerConfig:
    """Global configuration for the multi-tunnel manager."""

    tunnels: list[TunnelSpec]
    restart_delay: float = _DEFAULT_RESTART_DELAY
    max_restart_delay: float = _DEFAULT_MAX_RESTART_DELAY
    backoff_multiplier: float = _DEFAULT_BACKOFF_MULTIPLIER
    health_check_interval: float = _DEFAULT_HEALTH_CHECK_INTERVAL
    max_health_failures: int = _DEFAULT_MAX_HEALTH_FAILURES
    startup_grace_period: float = _DEFAULT_STARTUP_GRACE_PERIOD
    log_level: str = "info"
    tui: bool = True
    show_logs: bool = False
    raw_env: dict[str, str] = field(default_factory=dict)


# ── .env parser ───────────────────────────────────────────────────────────────


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a shell-style .env file into a dict (strips export, quotes)."""
    result: dict[str, str] = {}
    for lineno, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^export\s+", "", line)
        line = _strip_inline_comment(line).strip()
        if not line:
            continue
        if "=" not in line:
            logger.warning(
                "Ignoring malformed .env line %d in %s: %r", lineno, path, raw
            )
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            logger.warning("Ignoring empty .env key at line %d in %s", lineno, path)
            continue
        val = val.strip()
        # Strip surrounding quotes / escapes.
        if len(val) >= 2 and val[0] in ('"', "'") and val[-1] == val[0]:
            try:
                parsed = (
                    json.loads(val) if val[0] == '"' else val[1:-1].replace("\\'", "'")
                )
            except Exception:
                logger.warning(
                    "Ignoring malformed quoted .env value for %s at line %d in %s",
                    key,
                    lineno,
                    path,
                )
                continue
            val = parsed
        result[key] = val
    return result


def _strip_inline_comment(line: str) -> str:
    out: list[str] = []
    in_single = False
    in_double = False
    escaped = False

    for ch in line:
        if escaped:
            out.append(ch)
            escaped = False
            continue

        if ch == "\\" and in_double:
            out.append(ch)
            escaped = True
            continue

        if ch == "'" and not in_double:
            in_single = not in_single
            out.append(ch)
            continue

        if ch == '"' and not in_single:
            in_double = not in_double
            out.append(ch)
            continue

        if ch == "#" and not in_single and not in_double:
            break

        out.append(ch)

    return "".join(out)


def _load_manager_config(
    env_file: Path, log_level: str, tui: bool, show_logs: bool = False
) -> ManagerConfig:
    """Build a ManagerConfig from a .env file."""
    env = _parse_env_file(env_file)

    def _get(key: str, default: str = "") -> str:
        return env.get(key, default)

    def _getf(key: str, default: float) -> float:
        try:
            return float(_get(key, str(default)))
        except ValueError:
            return default

    def _geti(key: str, default: int) -> int:
        try:
            return int(_get(key, str(default)))
        except ValueError:
            return default

    def _getb(key: str, default: bool) -> bool:
        raw = _get(key, str(default)).strip().lower()
        if raw in _TRUE_TOKENS:
            return True
        if raw in _FALSE_TOKENS:
            return False
        logger.warning(
            "Invalid boolean for %s=%r in %s; using default %s",
            key,
            raw,
            env_file,
            default,
        )
        return default

    restart_delay = _getf("RESTART_DELAY", _DEFAULT_RESTART_DELAY)
    max_restart_delay = _getf("MAX_RESTART_DELAY", _DEFAULT_MAX_RESTART_DELAY)
    backoff_multiplier = _getf("BACKOFF_MULTIPLIER", _DEFAULT_BACKOFF_MULTIPLIER)
    health_check_interval = _getf(
        "HEALTH_CHECK_INTERVAL", _DEFAULT_HEALTH_CHECK_INTERVAL
    )
    max_health_failures = _geti("MAX_HEALTH_FAILURES", _DEFAULT_MAX_HEALTH_FAILURES)
    startup_grace_period = _getf("STARTUP_GRACE_PERIOD", _DEFAULT_STARTUP_GRACE_PERIOD)

    # Discover tunnel indices from WSS_URL_<N> keys
    indices: list[int] = sorted(
        int(m.group(1)) for k in env if (m := re.fullmatch(r"WSS_URL_(\d+)", k))
    )

    if not indices:
        raise ValueError(
            f"No WSS_URL_<N> entries found in {env_file}. "
            "Add at least WSS_URL_1=wss://... to the config file."
        )

    fallback_log_level = (_get("LOG_LEVEL", log_level) or "info").lower()
    if fallback_log_level not in _VALID_LOG_LEVELS:
        logger.warning(
            "Invalid LOG_LEVEL=%r in %s; using %r",
            fallback_log_level,
            env_file,
            "info",
        )
        fallback_log_level = "info"

    tunnels: list[TunnelSpec] = []
    for i, idx in enumerate(indices):
        wss_url = _get(f"WSS_URL_{idx}")
        if not wss_url:
            continue
        if not wss_url.startswith(("ws://", "wss://")):
            raise ValueError(
                f"WSS_URL_{idx} must start with ws:// or wss://, got: {wss_url!r}"
            )
        socks_port = _geti(f"SOCKS_PORT_{idx}", _DEFAULT_SOCKS_BASE_PORT + i)
        socks_host = _get(f"SOCKS_HOST_{idx}") or _get("SOCKS_HOST", "127.0.0.1")
        insecure = _getb(f"WSS_INSECURE_{idx}", _getb("WSS_INSECURE", False))
        tun_log_level = (_get(f"LOG_LEVEL_{idx}") or fallback_log_level).lower()
        if tun_log_level not in _VALID_LOG_LEVELS:
            logger.warning(
                "Invalid LOG_LEVEL_%d=%r in %s; using %r",
                idx,
                tun_log_level,
                env_file,
                fallback_log_level,
            )
            tun_log_level = fallback_log_level

        label = _get(f"LABEL_{idx}", f"tunnel-{idx}")

        env_overrides: dict[str, str] = {}
        for key, value in env.items():
            if any(key.startswith(prefix) for prefix in _PASSTHROUGH_ENV_PREFIXES):
                env_overrides.setdefault(key, value)

        for base_key in _PER_TUNNEL_OVERRIDE_KEYS:
            per_tunnel_key = f"{base_key}_{idx}"
            if per_tunnel_key in env:
                env_overrides[base_key] = env[per_tunnel_key]

        legacy_insecure_key = f"WSS_INSECURE_{idx}"
        if legacy_insecure_key in env:
            env_overrides["WSS_INSECURE"] = env[legacy_insecure_key]
        elif "WSS_INSECURE" in env:
            env_overrides["WSS_INSECURE"] = env["WSS_INSECURE"]

        if f"LOG_LEVEL_{idx}" in env:
            env_overrides["EXECTUNNEL_LOG_LEVEL"] = env[f"LOG_LEVEL_{idx}"]
        elif "LOG_LEVEL" in env:
            env_overrides.setdefault("EXECTUNNEL_LOG_LEVEL", env["LOG_LEVEL"])

        tunnels.append(
            TunnelSpec(
                index=idx,
                wss_url=wss_url,
                socks_port=socks_port,
                socks_host=socks_host,
                insecure=insecure,
                log_level=tun_log_level,
                label=label,
                env_overrides=env_overrides,
            )
        )

    seen_ports: dict[int, str] = {}
    for tunnel in tunnels:
        prior = seen_ports.get(tunnel.socks_port)
        if prior is not None:
            raise ValueError(
                f"Duplicate SOCKS port {tunnel.socks_port} configured for "
                f"{prior!r} and {tunnel.display_name!r}. "
                "Each managed tunnel must use a unique local SOCKS port."
            )
        seen_ports[tunnel.socks_port] = tunnel.display_name

    return ManagerConfig(
        tunnels=tunnels,
        restart_delay=restart_delay,
        max_restart_delay=max_restart_delay,
        backoff_multiplier=backoff_multiplier,
        health_check_interval=health_check_interval,
        max_health_failures=max_health_failures,
        startup_grace_period=startup_grace_period,
        log_level=log_level,
        tui=tui,
        show_logs=show_logs,
        raw_env=env,
    )


# ── Health check ──────────────────────────────────────────────────────────────


async def _check_socks_port(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


# ── Tunnel worker ─────────────────────────────────────────────────────────────


class TunnelWorker:
    """Manages the lifecycle of a single tunnel subprocess."""

    def __init__(
        self,
        spec: TunnelSpec,
        cfg: ManagerConfig,
        on_state_change: Callable[[TunnelRuntimeState], None],
    ) -> None:
        self._spec = spec
        self._cfg = cfg
        self._on_state_change = on_state_change
        self._state = TunnelRuntimeState(spec=spec)
        self._proc: asyncio.subprocess.Process | None = None
        self._start_time: float = 0.0
        self._current_delay = cfg.restart_delay

    @property
    def display_name(self) -> str:
        """Public accessor for task naming — avoids exposing ``_spec``."""
        return self._spec.display_name

    def _emit(self) -> None:
        self._state.uptime_secs = (
            time.monotonic() - self._start_time if self._start_time else 0.0
        )
        self._on_state_change(copy.deepcopy(self._state))

    def _build_cmd(self) -> list[str]:
        cmd = [
            sys.executable,
            "-m",
            "exectunnel.cli",
            "tunnel",
            "--socks-host",
            self._spec.socks_host,
            "--socks-port",
            str(self._spec.socks_port),
            "--no-dashboard",
            "--log-level",
            self._spec.log_level
            if self._spec.log_level in _VALID_LOG_LEVELS
            else "info",
        ]
        if self._spec.insecure:
            cmd.append("--insecure")
        return cmd

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self._spec.env_overrides)
        env["EXECTUNNEL_WSS_URL"] = self._spec.wss_url
        env["WSS_INSECURE"] = "1" if self._spec.insecure else "0"
        env["EXECTUNNEL_METRICS_REPORT"] = "1"
        env["EXECTUNNEL_LOG_LEVEL"] = self._spec.log_level
        return env

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self._run_once(stop_event)
            if stop_event.is_set():
                break
            # Backoff restart
            self._state.restart_count += 1
            self._state.status = "restarting"
            self._state.next_restart_in = self._current_delay
            self._emit()
            logger.info(
                "[%s] restarting in %.1fs (attempt %d)",
                self._spec.display_name,
                self._current_delay,
                self._state.restart_count,
            )
            deadline = time.monotonic() + self._current_delay
            while not stop_event.is_set() and time.monotonic() < deadline:
                self._state.next_restart_in = max(0.0, deadline - time.monotonic())
                self._emit()
                await asyncio.sleep(0.5)
            self._current_delay = min(
                self._current_delay * self._cfg.backoff_multiplier,
                self._cfg.max_restart_delay,
            )

        self._state.status = "stopped"
        self._state.next_restart_in = 0.0
        self._emit()

    async def _run_once(self, stop_event: asyncio.Event) -> None:
        cmd = self._build_cmd()
        env = self._build_env()
        logger.info("[%s] starting: %s", self._spec.display_name, shlex.join(cmd))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as exc:
            logger.error("[%s] failed to start: %s", self._spec.display_name, exc)
            self._state.status = "unhealthy"
            self._state.exit_code = -1
            self._state.log_lines.append(f"failed to start subprocess: {exc}")
            self._emit()
            return

        self._proc = proc
        self._start_time = time.monotonic()
        self._state.pid = proc.pid
        self._state.status = "running"
        self._state.socks_ok = False
        self._state.consecutive_health_failures = 0
        self._state.exit_code = None
        self._emit()

        # Log stdout/stderr
        log_task = asyncio.create_task(self._drain_output(proc))
        health_task = asyncio.create_task(self._health_loop(stop_event))
        stop_task = asyncio.create_task(stop_event.wait())
        wait_task = asyncio.create_task(proc.wait())

        try:
            done, _ = await asyncio.wait(
                {wait_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if stop_task in done:
                # Graceful shutdown
                wait_task.cancel()
                with contextlib.suppress(ProcessLookupError):
                    proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except (TimeoutError, ProcessLookupError):
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    with contextlib.suppress(ProcessLookupError, TimeoutError):
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
            else:
                stop_task.cancel()
        finally:
            for task in (health_task, log_task, stop_task, wait_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                health_task,
                log_task,
                stop_task,
                wait_task,
                return_exceptions=True,
            )

        rc = proc.returncode
        self._state.exit_code = rc
        self._state.pid = None
        self._state.socks_ok = False
        if not stop_event.is_set():
            self._state.status = "unhealthy"
            logger.warning("[%s] exited with code %s", self._spec.display_name, rc)
        self._emit()
        self._proc = None

    _METRICS_PREFIX = "__ET_METRICS__:"

    async def _drain_output(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stdout is None:
            return
        try:
            async for line in proc.stdout:
                text = line.decode(errors="replace").rstrip()
                if text.startswith(self._METRICS_PREFIX):
                    self._parse_metrics(text[len(self._METRICS_PREFIX) :])
                else:
                    logger.debug("[%s] %s", self._spec.display_name, text)
                    self._state.log_lines.append(text)
                    self._emit()
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                while True:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=0.05)
                    if not line:
                        break
                    text = line.decode(errors="replace").rstrip()
                    if text.startswith(self._METRICS_PREFIX):
                        self._parse_metrics(text[len(self._METRICS_PREFIX) :])
                    else:
                        logger.debug("[%s] %s", self._spec.display_name, text)
                        self._state.log_lines.append(text)
                        self._emit()
        except Exception:
            pass

    def _parse_metrics(self, payload: str) -> None:
        """Decode a JSON metrics payload and update tunnel state."""
        try:
            data = json.loads(payload)
            self._state.metrics = TunnelMetrics.from_dict(data)
            self._emit()
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.debug("[%s] bad metrics payload", self._spec.display_name)

    async def _health_loop(self, stop_event: asyncio.Event) -> None:
        # Grace period before first check
        try:
            await asyncio.sleep(self._cfg.startup_grace_period)
        except asyncio.CancelledError:
            return

        while not stop_event.is_set():
            ok = await _check_socks_port(self._spec.socks_host, self._spec.socks_port)
            self._state.socks_ok = ok
            if ok:
                self._state.status = "healthy"
                self._state.consecutive_health_failures = 0
                self._current_delay = (
                    self._cfg.restart_delay
                )  # reset backoff on healthy
            else:
                self._state.consecutive_health_failures += 1
                self._state.health_failures += 1
                if (
                    self._state.consecutive_health_failures
                    >= self._cfg.max_health_failures
                ):
                    self._state.status = "unhealthy"
                    logger.warning(
                        "[%s] health check failed %d times — killing",
                        self._spec.display_name,
                        self._state.consecutive_health_failures,
                    )
                    if self._proc is not None:
                        with contextlib.suppress(ProcessLookupError):
                            self._proc.send_signal(signal.SIGTERM)
                    self._emit()
                    return
            self._emit()
            try:
                await asyncio.sleep(self._cfg.health_check_interval)
            except asyncio.CancelledError:
                return


# ── Entry point ───────────────────────────────────────────────────────────────


def run_manager(
    env_file: Path,
    *,
    log_level: str = "info",
    tui: bool = True,
    show_logs: bool = False,
) -> None:
    """Parse env_file, start all tunnel workers, and supervise them."""
    from exectunnel.observability.logging import configure_logging

    configure_logging(log_level)

    try:
        cfg = _load_manager_config(
            env_file, log_level=log_level, tui=tui, show_logs=show_logs
        )
    except (ValueError, FileNotFoundError, OSError) as exc:
        logger.error("Failed to load manager config: %s", exc)
        raise SystemExit(1) from exc

    logger.info(
        "Manager starting with %d tunnel(s) from %s", len(cfg.tunnels), env_file
    )

    asyncio.run(_run_manager_async(cfg))


async def _run_manager_async(cfg: ManagerConfig) -> None:
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)

    if cfg.tui:
        from .dashboards import ManagerDashboard  # noqa: PLC0415

        dashboard = ManagerDashboard(cfg)

        def _on_state(state: TunnelRuntimeState) -> None:
            dashboard.on_state_change(state)

    else:

        def _on_state(state: TunnelRuntimeState) -> None:
            logger.info(
                "[%s] status=%s socks_ok=%s restarts=%d",
                state.display_name,
                state.status,
                state.socks_ok,
                state.restart_count,
            )

    workers = [
        TunnelWorker(spec=spec, cfg=cfg, on_state_change=_on_state)
        for spec in cfg.tunnels
    ]

    worker_tasks = [
        asyncio.create_task(
            w.run_forever(stop_event),
            name=w.display_name,
        )
        for w in workers
    ]

    if cfg.tui:
        dashboard_task = asyncio.create_task(
            dashboard.run_until_cancelled(), name="dashboard"
        )
        stop_watcher = asyncio.create_task(stop_event.wait(), name="stop-watcher")

        done, _ = await asyncio.wait(
            {stop_watcher, *worker_tasks},
            return_when=asyncio.FIRST_COMPLETED,
        )

        stop_event.set()
        dashboard.stop()
        dashboard_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dashboard_task
        stop_watcher.cancel()
    else:
        stop_watcher = asyncio.create_task(stop_event.wait(), name="stop-watcher")
        await asyncio.wait(
            {stop_watcher, *worker_tasks},
            return_when=asyncio.FIRST_COMPLETED,
        )
        stop_event.set()
        stop_watcher.cancel()

    await asyncio.gather(*worker_tasks, return_exceptions=True)

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.remove_signal_handler(sig)

    logger.info("Manager stopped.")
