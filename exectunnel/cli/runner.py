"""Shared session lifecycle — signal handling, graceful shutdown, dashboard wiring."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from typing import Any

from rich.console import Console

from exectunnel.exceptions import (
    BootstrapError,
    ExecTunnelError,
    ReconnectExhaustedError,
)
from exectunnel.observability import metrics_inc, metrics_observe
from exectunnel.session import SessionConfig, TunnelConfig

from .dashboards.tunnel import TunnelDashboard
from .metrics import HealthMonitor
from .ui import BootstrapSpinner, Icons

__all__ = ["run_session"]

logger = logging.getLogger(__name__)

# ── Observability listener API ────────────────────────────────────────────────
#
# Resolved once at import time.  If the observability module does not expose
# the listener API (older build / minimal test stub), listener registration
# degrades gracefully to a no-op.

try:
    from exectunnel.observability import (
        register_metric_listener as _register_metric_listener,
    )
    from exectunnel.observability.metrics import _listeners as _metric_listeners

    _HAS_LISTENER_API: bool = True
except (ImportError, AttributeError):
    _register_metric_listener = None
    _metric_listeners = None
    _HAS_LISTENER_API = False

# ── Spinner ↔ metric mapping ──────────────────────────────────────────────────
#
# Maps observability metric names to (spinner_phase, action) pairs.
# action ∈ {"start", "done", "skip", "fail"}

_PHASE_MAP: dict[str, tuple[str, str]] = {
    # stty
    "bootstrap.started": ("stty", "start"),
    "bootstrap.stty_done": ("stty", "done"),
    # upload — Python and Go share the same spinner phase
    "bootstrap.python.upload_started": ("upload", "start"),
    "bootstrap.python.upload_done": ("upload", "done"),
    "bootstrap.go.upload_started": ("upload", "start"),
    "bootstrap.go.upload_done": ("upload", "done"),
    # fetch delivery → maps to upload phase
    "bootstrap.python.fetch_started": ("upload", "start"),
    "bootstrap.python.fetch_done": ("upload", "done"),
    "bootstrap.go.fetch_started": ("upload", "start"),
    "bootstrap.go.fetch_done": ("upload", "done"),
    # decode (only for upload delivery; fetch skips this phase)
    "bootstrap.python.decode_done": ("decode", "done"),
    # skip signals (emitted when skip_if_present reuses the cached agent)
    "bootstrap.python.skip_delivery": ("upload", "skip"),
    "bootstrap.go.skip_delivery": ("upload", "skip"),
    # syntax
    "bootstrap.syntax_started": ("syntax", "start"),
    "bootstrap.syntax_done": ("syntax", "done"),
    "bootstrap.syntax_skipped": ("syntax", "skip"),
    "bootstrap.syntax_cache_hit": ("syntax", "skip"),
    # exec
    "bootstrap.exec_started": ("exec", "start"),
    "bootstrap.exec_done": ("exec", "done"),
    # ready
    "bootstrap.ok": ("ready", "done"),
    "bootstrap.timeout": ("ready", "fail"),
}

_SHUTDOWN_TIMEOUT_SECS: float = 10.0

# Env-var flag set by the manager to request periodic JSON metrics on stdout.
_METRICS_REPORT_ENV = "EXECTUNNEL_METRICS_REPORT"
_METRICS_REPORT_INTERVAL = 2.0  # seconds
_METRICS_LINE_PREFIX = "__ET_METRICS__:"


# ── Entry point ───────────────────────────────────────────────────────────────


async def run_session(
    session_cfg: SessionConfig,
    tun_cfg: TunnelConfig,
    ws_url: str,
    pod_spec: Any | None,
    console: Console,
    *,
    no_dashboard: bool = False,
    show_logs: bool = False,
    spinner: BootstrapSpinner | None = None,
) -> int:
    """Run a tunnel session with dashboard and signal handling.

    Returns
    -------
    int
        Exit code — ``0`` on clean exit or graceful shutdown, ``1`` on error.

    Bootstrap timeout
    -----------------
    The function waits indefinitely for ``bootstrap.ok`` during Phase 1.
    If the agent never becomes ready the session task will eventually fail
    (connection timeout / exec error) and Phase 1 resolves naturally.
    An explicit bootstrap timeout can be added to ``TunnelConfig`` if needed.
    """
    from exectunnel.session import TunnelSession  # local to avoid circular import

    _session_start = time.monotonic()
    metrics_inc("cli.session.start")

    session = TunnelSession(session_cfg, tun_cfg)
    monitor = HealthMonitor(
        pod_spec=pod_spec,
        ws_url=ws_url,
        socks_host=tun_cfg.socks_host,
        socks_port=tun_cfg.socks_port,
    )

    # ── Listener management ───────────────────────────────────────────────

    registered_listeners: list[Callable[..., None]] = []

    def _add_listener(callback: Callable[..., None]) -> None:
        if _try_register_listener(callback):
            registered_listeners.append(callback)

    _add_listener(monitor.on_metric)

    if spinner is not None:
        _add_listener(_make_spinner_listener(spinner))

    # ── Bootstrap-done signal ─────────────────────────────────────────────
    # A separate lightweight listener that gates Phase 1 → Phase 2 transition.
    bootstrap_done = asyncio.Event()

    def _on_bootstrap_done(name: str, **_: object) -> None:
        if name == "bootstrap.ok":
            bootstrap_done.set()

    _add_listener(_on_bootstrap_done)

    # ── Signal handling ───────────────────────────────────────────────────

    stop_event = asyncio.Event()

    def _handle_signal(sig: int) -> None:
        console.print(
            f"\n[et.warn]{Icons.WARN} Signal {sig} received — shutting down…[/et.warn]"
        )
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except (NotImplementedError, RuntimeError):
            pass  # Windows / environments that restrict signal handling

    # ── Task setup ────────────────────────────────────────────────────────

    session_task = asyncio.create_task(session.run(), name="tunnel-session")
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-signal")
    bootstrap_task = asyncio.create_task(bootstrap_done.wait(), name="bootstrap-wait")
    dashboard_task: asyncio.Task[None] | None = None
    metrics_reporter_task: asyncio.Task[None] | None = None

    def _record_exit(label: str) -> None:
        metrics_inc(label)
        metrics_observe("cli.session.duration_sec", time.monotonic() - _session_start)

    try:
        # ── Phase 1: wait for bootstrap, stop signal, or early failure ────

        done, _ = await asyncio.wait(
            {session_task, stop_task, bootstrap_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if stop_task in done:
            _cancel_tasks(session_task, bootstrap_task)
            await _drain(session_task)
            _record_exit("cli.session.exit.signal")
            return 0

        if session_task in done:
            bootstrap_task.cancel()
            result = _extract_result(session_task, console, spinner)
            _record_exit(
                "cli.session.exit.error" if result else "cli.session.exit.clean"
            )
            return result

        # Bootstrap succeeded — settle the spinner before handing off.
        bootstrap_task.cancel()
        if spinner is not None:
            spinner.done_phase("ready")
            spinner.finalize()

        if not no_dashboard:
            # Install a log ring buffer when the user wants log tail in the dashboard.
            log_buf = None
            if show_logs:
                from exectunnel.observability.logging import install_ring_buffer

                log_buf = install_ring_buffer(maxlen=200)

            dashboard = TunnelDashboard(
                monitor=monitor,
                console=console,
                ws_url=ws_url,
                log_buffer=log_buf,
            )
            dashboard_task = asyncio.create_task(
                dashboard.run_until_cancelled(), name="dashboard"
            )
        else:
            _print_proxy_hint(console, tun_cfg)

        # When running under the manager, start a background task that
        # periodically dumps metrics as JSON lines on stdout.
        if os.environ.get(_METRICS_REPORT_ENV) == "1":
            metrics_reporter_task = asyncio.create_task(
                _metrics_reporter(monitor), name="metrics-reporter"
            )

        # ── Phase 2: wait for session end or stop signal ──────────────────

        done, _ = await asyncio.wait(
            {session_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if stop_task in done:
            session_task.cancel()
            with console.status("[et.warn]Shutting down tunnel…[/et.warn]"):
                await _drain(session_task)
            _record_exit("cli.session.exit.signal")
            return 0

        result = _extract_result(session_task, console, spinner)
        _record_exit("cli.session.exit.error" if result else "cli.session.exit.clean")
        return result

    except KeyboardInterrupt:
        session_task.cancel()
        _record_exit("cli.session.exit.signal")
        return 0

    finally:
        stop_task.cancel()
        bootstrap_task.cancel()

        if dashboard_task is not None:
            dashboard_task.cancel()
            try:
                await dashboard_task
            except asyncio.CancelledError:
                pass

        if metrics_reporter_task is not None:
            metrics_reporter_task.cancel()
            try:
                await metrics_reporter_task
            except asyncio.CancelledError:
                pass

        # Unregister all listeners to prevent memory leaks.
        for cb in registered_listeners:
            _try_unregister_listener(cb)
        registered_listeners.clear()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):
                pass


# ── Internal helpers ──────────────────────────────────────────────────────────


def _try_register_listener(callback: Callable[..., None]) -> bool:
    """Register *callback* with the observability layer.

    Returns ``True`` on success, ``False`` if the listener API is unavailable.
    Uses the module-level resolved reference to avoid repeated import overhead.
    """
    if not _HAS_LISTENER_API or _register_metric_listener is None:
        return False
    _register_metric_listener(callback)  # type: ignore[arg-type]
    return True


def _try_unregister_listener(callback: Callable[..., None]) -> None:
    if not _HAS_LISTENER_API or _metric_listeners is None:
        return
    try:
        _metric_listeners.remove(callback)
    except (ValueError, AttributeError):
        # ValueError  : already removed or never added
        # AttributeError: container type changed in the observability layer
        pass


def _make_spinner_listener(spinner: BootstrapSpinner) -> Callable[..., None]:
    """Return a metric listener that advances spinner phases."""

    def _on_metric(name: str, **_: object) -> None:
        entry = _PHASE_MAP.get(name)
        if entry is None:
            return
        phase_name, action = entry
        match action:
            case "start":
                spinner.start_phase(phase_name)
            case "done":
                spinner.done_phase(phase_name)
            case "skip":
                spinner.skip_phase(phase_name)
            case "fail":
                spinner.fail_phase(phase_name)

    return _on_metric


async def _metrics_reporter(monitor: HealthMonitor) -> None:
    """Periodically write a JSON metrics line to stdout.

    Designed to run only when the tunnel subprocess is managed by the
    multi-tunnel manager (env ``EXECTUNNEL_METRICS_REPORT=1``).
    The manager's ``_drain_output`` parser picks up these lines and
    populates per-tunnel metrics for the manager dashboard.
    """
    try:
        while True:
            await asyncio.sleep(_METRICS_REPORT_INTERVAL)
            try:
                snap = monitor.snapshot()
                line = _METRICS_LINE_PREFIX + json.dumps(
                    snap.to_report_dict(),
                    separators=(",", ":"),
                )
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            except Exception:
                pass  # never crash the tunnel for a reporting glitch
    except asyncio.CancelledError:
        pass


def _cancel_tasks(*tasks: asyncio.Task[Any]) -> None:
    for t in tasks:
        if not t.done():
            t.cancel()


async def _drain(task: asyncio.Task[Any]) -> None:
    """Await *task* with a hard timeout; swallow all exceptions."""
    try:
        async with asyncio.timeout(_SHUTDOWN_TIMEOUT_SECS):
            await task
    except (TimeoutError, asyncio.CancelledError, Exception):
        pass


def _extract_result(
    task: asyncio.Task[Any],
    console: Console,
    spinner: BootstrapSpinner | None,
) -> int:
    """Translate a completed session task into an exit code."""
    if task.cancelled():
        return 0

    exc = task.exception()
    if exc is None:
        console.print(f"[et.ok]{Icons.CHECK} Session ended cleanly.[/et.ok]")
        return 0

    if spinner is not None:
        spinner.fail_current(str(exc))

    return _handle_session_error(exc, console)


def _print_proxy_hint(console: Console, tun_cfg: TunnelConfig) -> None:
    proxy = f"socks5://{tun_cfg.socks_host}:{tun_cfg.socks_port}"
    console.print(
        f"\n[et.ok]{Icons.CHECK} Tunnel active — "
        f"SOCKS5 on {tun_cfg.socks_host}:{tun_cfg.socks_port}[/et.ok]"
    )
    console.print(f"[et.muted]  export https_proxy={proxy}[/et.muted]")
    console.print(f"[et.muted]  export http_proxy={proxy}[/et.muted]")
    console.print("[et.muted]  Press Ctrl+C to stop.[/et.muted]\n")


def _handle_session_error(exc: BaseException, console: Console) -> int:
    """Print a structured error message and return exit code 1."""
    if isinstance(exc, ReconnectExhaustedError):
        console.print(f"\n[et.error]{Icons.CROSS} Reconnect exhausted[/et.error]")
        console.print(
            f"  [et.label]Attempts:[/et.label] "
            f"[et.value]{exc.details.get('attempts')}[/et.value]"
        )
        console.print(
            f"  [et.label]Last error:[/et.label] "
            f"[et.value]{exc.details.get('last_error')}[/et.value]"
        )
        if exc.hint:
            console.print(f"  [et.muted]{Icons.BULLET} {exc.hint}[/et.muted]")
        return 1

    if isinstance(exc, BootstrapError):
        console.print(
            f"\n[et.error]{Icons.CROSS} Bootstrap failed [{exc.error_code}][/et.error]"
        )
        console.print(f"  [et.value]{exc.message}[/et.value]")
        for k, v in exc.details.items():
            if v is not None:
                console.print(f"  [et.label]{k}:[/et.label] [et.muted]{v}[/et.muted]")
        if exc.hint:
            console.print(f"  [et.muted]{Icons.BULLET} {exc.hint}[/et.muted]")
        return 1

    if isinstance(exc, ExecTunnelError):
        console.print(
            f"\n[et.error]{Icons.CROSS} Tunnel error "
            f"[{exc.error_code}] (error_id={exc.error_id})[/et.error]"
        )
        console.print(f"  [et.value]{exc.message}[/et.value]")
        for k, v in exc.details.items():
            if v is not None:
                console.print(f"  [et.label]{k}:[/et.label] [et.muted]{v}[/et.muted]")
        if exc.hint:
            console.print(f"  [et.muted]{Icons.BULLET} {exc.hint}[/et.muted]")
        return 1

    console.print(f"\n[et.error]{Icons.CROSS} Unexpected error: {exc}[/et.error]")
    from rich.traceback import Traceback

    console.print(Traceback.from_exception(type(exc), exc, exc.__traceback__))
    return 1
