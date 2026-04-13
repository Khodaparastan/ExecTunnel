"""exectunnel.manager — multi-tunnel supervisor."""

from __future__ import annotations

import asyncio
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
        valid = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in valid})


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


# ── .env parser ───────────────────────────────────────────────────────────────


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a shell-style .env file into a dict (strips export, quotes)."""
    result: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^export\s+", "", line)
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # Strip surrounding quotes
        if len(val) >= 2 and val[0] in ('"', "'") and val[-1] == val[0]:
            val = val[1:-1]
        result[key] = val
    return result


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

    tunnels: list[TunnelSpec] = []
    for i, idx in enumerate(indices):
        wss_url = _get(f"WSS_URL_{idx}")
        if not wss_url:
            continue
        socks_port = _geti(f"SOCKS_PORT_{idx}", _DEFAULT_SOCKS_BASE_PORT + i)
        socks_host = _get(f"SOCKS_HOST_{idx}") or _get("SOCKS_HOST", "127.0.0.1")
        insecure_str = _get(f"WSS_INSECURE_{idx}") or _get("WSS_INSECURE", "false")
        insecure = insecure_str.lower() in ("1", "true", "yes")
        tun_log_level = _get(f"LOG_LEVEL_{idx}") or _get("LOG_LEVEL", log_level)
        label = _get(f"LABEL_{idx}", f"tunnel-{idx}")
        tunnels.append(
            TunnelSpec(
                index=idx,
                wss_url=wss_url,
                socks_port=socks_port,
                socks_host=socks_host,
                insecure=insecure,
                log_level=tun_log_level,
                label=label,
            )
        )

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
        self._on_state_change(self._state)

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
        ]
        if self._spec.insecure:
            cmd.append("--insecure")
        return cmd

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["EXECTUNNEL_WSS_URL"] = self._spec.wss_url
        env["WSS_INSECURE"] = "1" if self._spec.insecure else "0"
        env["EXECTUNNEL_METRICS_REPORT"] = "1"
        return env

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self._run_once(stop_event)
            if stop_event.is_set():
                break
            # Backoff restart
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
            self._state.restart_count += 1

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

        done, _ = await asyncio.wait(
            {wait_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        health_task.cancel()
        log_task.cancel()

        if stop_task in done:
            # Graceful shutdown
            wait_task.cancel()
            try:
                proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except (TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        else:
            stop_task.cancel()

        try:
            await asyncio.gather(health_task, log_task, return_exceptions=True)
        except Exception:
            pass

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
        except asyncio.CancelledError:
            pass
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
                        try:
                            self._proc.send_signal(signal.SIGTERM)
                        except ProcessLookupError:
                            pass
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
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    if cfg.tui:
        from .dashboards import ManagerDashboard

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
        try:
            await dashboard_task
        except asyncio.CancelledError:
            pass
        stop_watcher.cancel()
    else:
        await stop_event.wait()

    await asyncio.gather(*worker_tasks, return_exceptions=True)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.remove_signal_handler(sig)
        except (NotImplementedError, RuntimeError):
            pass

    logger.info("Manager stopped.")
