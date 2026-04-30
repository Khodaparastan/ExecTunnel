"""``run`` and ``run-single`` commands."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Final, Literal

import typer

from exectunnel.config import (
    CLIOverrides,
    TunnelEntry,
    TunnelFile,
    make_single_tunnel_file,
)
from exectunnel.observability import (
    MetricEvent,
    register_metric_listener,
    unregister_metric_listener,
)
from exectunnel.observability.logging import install_ring_buffer
from exectunnel.session import TunnelSession

from .._context import AppContext
from .._display import (
    BANNER,
    PHASE_NAMES,
    BootstrapSpinner,
    DashboardMode,
    TunnelSlot,
    UnifiedDashboard,
    get_stderr_console,
)
from .._supervisor.worker import exception_exit_code, log_session_exception
from ..metrics import HealthMonitor
from ..utils import (
    parse_existing_file,
    parse_http_url,
    parse_ip,
    parse_ws_headers,
    parse_wss_url,
)

__all__: list[str] = []

logger = logging.getLogger("exectunnel.cli.run")

_EXIT_OK: Final[int] = 0
_EXIT_ERROR: Final[int] = 1
_EXIT_BOOTSTRAP: Final[int] = 2
_EXIT_RECONNECT_EXHAUSTED: Final[int] = 3
_EXIT_INTERRUPTED: Final[int] = 130

_SHUTDOWN_SIGNALS: Final[tuple[signal.Signals, ...]] = tuple(
    sig for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)) if sig is not None
)

_PHASE_MAP: Final[dict[str, tuple[str, str]]] = {
    "bootstrap.started": ("stty", "start"),
    "bootstrap.stty_done": ("stty", "done"),
    "bootstrap.python.upload_started": ("upload", "start"),
    "bootstrap.python.upload_done": ("upload", "done"),
    "bootstrap.go.upload_started": ("upload", "start"),
    "bootstrap.go.upload_done": ("upload", "done"),
    "bootstrap.python.fetch_started": ("upload", "start"),
    "bootstrap.python.fetch_done": ("upload", "done"),
    "bootstrap.go.fetch_started": ("upload", "start"),
    "bootstrap.go.fetch_done": ("upload", "done"),
    "bootstrap.python.decode_done": ("decode", "done"),
    "bootstrap.python.skip_delivery": ("upload", "skip"),
    "bootstrap.go.skip_delivery": ("upload", "skip"),
    "bootstrap.syntax_started": ("syntax", "start"),
    "bootstrap.syntax_done": ("syntax", "done"),
    "bootstrap.syntax_skipped": ("syntax", "skip"),
    "bootstrap.syntax_cache_hit": ("syntax", "skip"),
    "bootstrap.exec_started": ("exec", "start"),
    "bootstrap.exec_done": ("exec", "done"),
    "bootstrap.ok": ("ready", "done"),
    "bootstrap.timeout": ("ready", "fail"),
}

_STATUS_EVENTS: Final[dict[str, str]] = {
    "session.connect.attempt": "starting",
    "session.connect.ok": "running",
    "session.reconnect": "restarting",
    "bootstrap.ok": "healthy",
    "bootstrap.timeout": "failed",
    "session.serve.stopped": "stopped",
}

_SHUTDOWN_TIMEOUT_SECS: Final[float] = 10.0

_SCOPE_NAME_KEYS: Final[frozenset[str]] = frozenset({
    "tunnel",
    "tunnel_name",
    "entry",
    "entry_name",
    "session",
    "session_name",
    "slot",
    "slot_name",
})

_SCOPE_URL_KEYS: Final[frozenset[str]] = frozenset({
    "wss_url",
    "ws_url",
    "url",
})


@dataclass(frozen=True, slots=True)
class _MetricScope:
    """Per-tunnel metric discriminator for global listener filtering."""

    tunnel_name: str
    wss_url: str
    accept_unscoped: bool

    def matches(self, event: MetricEvent) -> bool:
        if not event.tags:
            return self.accept_unscoped

        scope_tags_present = False

        for key in _SCOPE_NAME_KEYS:
            raw = event.tags.get(key)
            if raw is None:
                continue
            scope_tags_present = True
            if str(raw).strip() == self.tunnel_name:
                return True

        for key in _SCOPE_URL_KEYS:
            raw = event.tags.get(key)
            if raw is None:
                continue
            scope_tags_present = True
            if str(raw).strip() == self.wss_url:
                return True

        return self.accept_unscoped and not scope_tags_present


# ---------------------------------------------------------------------------
# Metric listener helpers
# ---------------------------------------------------------------------------


def _safe_unregister_metric_listener(
    callback: Callable[[MetricEvent], None],
) -> None:
    with contextlib.suppress(Exception):
        unregister_metric_listener(callback)


def _register_metric_listener(
    callback: Callable[[MetricEvent], None],
    cleanup_stack: contextlib.ExitStack,
) -> None:
    register_metric_listener(callback)
    cleanup_stack.callback(_safe_unregister_metric_listener, callback)


def _schedule_listener_on_loop(
    loop: asyncio.AbstractEventLoop,
    callback: Callable[..., object],
    *args: object,
) -> None:
    try:
        loop.call_soon_threadsafe(callback, *args)
    except RuntimeError as exc:
        if "closed" in str(exc).lower():
            return
        raise


def _make_monitor_listener(
    monitor: HealthMonitor,
    loop: asyncio.AbstractEventLoop,
    scope: _MetricScope,
) -> Callable[[MetricEvent], None]:
    def _on_metric(event: MetricEvent) -> None:
        if not scope.matches(event):
            return
        _schedule_listener_on_loop(loop, monitor.on_metric, event)

    return _on_metric


def _make_spinner_listener(
    spinner: BootstrapSpinner,
    loop: asyncio.AbstractEventLoop,
    scope: _MetricScope,
) -> Callable[[MetricEvent], None]:
    def _on_metric(event: MetricEvent) -> None:
        if not scope.matches(event):
            return

        phase = _PHASE_MAP.get(event.name)
        if phase is None:
            return

        phase_name, action = phase

        def _apply() -> None:
            match action:
                case "start":
                    spinner.start_phase(phase_name)
                case "done":
                    spinner.done_phase(phase_name)
                case "skip":
                    spinner.skip_phase(phase_name)
                case "fail":
                    spinner.fail_phase(phase_name)

        _schedule_listener_on_loop(loop, _apply)

    return _on_metric


def _make_bootstrap_done_listener(
    ready_event: asyncio.Event,
    loop: asyncio.AbstractEventLoop,
    scope: _MetricScope,
) -> Callable[[MetricEvent], None]:
    def _on_metric(event: MetricEvent) -> None:
        if event.name != "bootstrap.ok":
            return
        if not scope.matches(event):
            return
        _schedule_listener_on_loop(loop, ready_event.set)

    return _on_metric


def _make_state_listener(
    slot: TunnelSlot,
    monitor: HealthMonitor,
    loop: asyncio.AbstractEventLoop,
    scope: _MetricScope,
) -> Callable[[MetricEvent], None]:
    def _on_metric(event: MetricEvent) -> None:
        if not scope.matches(event):
            return

        status = _STATUS_EVENTS.get(event.name)
        if status is None:
            return

        def _apply() -> None:
            if event.name in {"session.connect.attempt", "session.reconnect"}:
                monitor.set_connected(False)
            elif event.name in {"session.connect.ok", "bootstrap.ok"}:
                monitor.set_connected(True)
            elif event.name in {"bootstrap.timeout", "session.serve.stopped"}:
                monitor.set_connected(False)

            slot.status = status

        _schedule_listener_on_loop(loop, _apply)

    return _on_metric


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _bool_flag_to_optional(value: bool) -> bool | None:
    return True if value else None


@contextlib.contextmanager
def _temporary_root_level(level: int):
    root = logging.getLogger()
    previous = root.level
    root.setLevel(level)
    try:
        yield
    finally:
        root.setLevel(previous)


def _dedupe_names(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


@cache
def _get_package_version() -> str:
    try:
        from importlib.metadata import version as _pkg_version  # noqa: PLC0415

        return _pkg_version("exectunnel")
    except Exception:
        return "dev"


def _print_startup_banner(entries: list[TunnelEntry]) -> None:
    console = get_stderr_console()
    version = _get_package_version()
    console.print(BANNER.format(version=version))
    console.print(
        f"[bold cyan]Starting[/bold cyan] [bold]{len(entries)}[/bold] tunnel(s)\n"
    )
    for entry in entries:
        console.print(
            f"  [green]→[/green] [bold]{entry.name}[/bold]  "
            f"[dim]port {entry.socks_port}[/dim]  "
            f"[dim]{entry.wss_url}[/dim]"
        )
    console.print()


async def _drain_task(task: asyncio.Task[Any]) -> None:
    if task.done():
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return

    try:
        async with asyncio.timeout(_SHUTDOWN_TIMEOUT_SECS):
            await task
    except TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("ignored task drain exception", exc_info=True)


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    on_signal: Callable[[signal.Signals], None],
) -> list[signal.Signals]:
    installed: list[signal.Signals] = []

    for sig in _SHUTDOWN_SIGNALS:
        try:
            loop.add_signal_handler(sig, on_signal, sig)
        except (NotImplementedError, RuntimeError, ValueError):
            logger.debug("Signal handler installation not supported for %s", sig.name)
        else:
            installed.append(sig)

    return installed


# ---------------------------------------------------------------------------
# Bootstrap phase — extracted to reduce nesting in _run_tunnel
# ---------------------------------------------------------------------------


async def _run_bootstrap_phase(
    name: str,
    session_task: asyncio.Task[None],
    bootstrap_done: asyncio.Event,
    scope: _MetricScope,
    monitor: HealthMonitor,
    slot: TunnelSlot,
) -> int | None:
    """Drive the bootstrap spinner and wait for completion or early failure.

    Returns ``None`` if bootstrap succeeded and the session is still running.
    Returns an ``int`` exit code if the session died before bootstrap completed.
    """
    with _temporary_root_level(logging.WARNING):
        loop = asyncio.get_running_loop()
        async with BootstrapSpinner(get_stderr_console()) as spinner:
            with contextlib.ExitStack() as spinner_stack:
                _register_metric_listener(
                    _make_spinner_listener(spinner, loop, scope),
                    spinner_stack,
                )

                spinner.start_phase(PHASE_NAMES[0])

                bootstrap_wait_task = asyncio.create_task(
                    bootstrap_done.wait(),
                    name=f"bootstrap-{name}",
                )
                try:
                    done, _ = await asyncio.wait(
                        {session_task, bootstrap_wait_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    bootstrap_wait_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await bootstrap_wait_task

                if session_task in done:
                    monitor.set_connected(False)
                    spinner.fail_current("session ended before bootstrap completed")

                    if session_task.cancelled():
                        logger.error(
                            "[%s] session cancelled before bootstrap completed",
                            name,
                        )
                        slot.status = "failed"
                        return _EXIT_ERROR

                    exc = session_task.exception()
                    if exc is None:
                        logger.error(
                            "[%s] session ended before bootstrap completed",
                            name,
                        )
                        slot.status = "failed"
                        return _EXIT_ERROR

                    log_session_exception(name, exc)
                    slot.status = "failed"
                    return exception_exit_code(exc)

                spinner.done_phase("ready")
                spinner.finalize()

    return None


# ---------------------------------------------------------------------------
# Core tunnel runner
# ---------------------------------------------------------------------------


async def _run_tunnel(
    name: str,
    tunnel_file: TunnelFile,
    entry: TunnelEntry,
    cli_overrides: CLIOverrides,
    slot: TunnelSlot,
    *,
    allow_unscoped_metrics: bool,
    show_logs: bool = False,
) -> int:
    """Run a single tunnel session with bootstrap spinner and health monitor."""
    loop = asyncio.get_running_loop()
    slot.status = "starting"

    try:
        session_cfg, tun_cfg = tunnel_file.resolve(entry, cli_overrides)
        session = TunnelSession(session_cfg, tun_cfg)
    except Exception as exc:
        logger.error("[%s] config/session setup failed: %s", name, exc)
        slot.status = "failed"
        return _EXIT_ERROR

    scope = _MetricScope(
        tunnel_name=entry.name,
        wss_url=str(session_cfg.wss_url),
        accept_unscoped=allow_unscoped_metrics,
    )

    monitor = HealthMonitor(
        pod_spec=None,
        ws_url=str(session_cfg.wss_url),
        socks_host=tun_cfg.socks_host,
        socks_port=tun_cfg.socks_port,
        send_queue_cap=session_cfg.send_queue_cap,
    )
    monitor.set_connected(False)
    slot.monitor = monitor

    if show_logs:
        try:
            slot.log_buffer = install_ring_buffer(maxlen=200)
        except Exception:
            logger.debug("[%s] log ring-buffer not available", name, exc_info=True)

    session_task: asyncio.Task[None] | None = None

    try:
        with contextlib.ExitStack() as listener_stack:
            bootstrap_done = asyncio.Event()

            _register_metric_listener(
                _make_monitor_listener(monitor, loop, scope),
                listener_stack,
            )
            _register_metric_listener(
                _make_state_listener(slot, monitor, loop, scope),
                listener_stack,
            )
            _register_metric_listener(
                _make_bootstrap_done_listener(bootstrap_done, loop, scope),
                listener_stack,
            )

            session_task = asyncio.create_task(
                session.run(),
                name=f"session-{name}",
            )

            try:
                bootstrap_result = await _run_bootstrap_phase(
                    name,
                    session_task,
                    bootstrap_done,
                    scope,
                    monitor,
                    slot,
                )
                if bootstrap_result is not None:
                    return bootstrap_result

            except asyncio.CancelledError:
                monitor.set_connected(False)
                slot.status = "stopped"
                if session_task is not None and not session_task.done():
                    session_task.cancel()
                    await _drain_task(session_task)
                raise

            if session_task is None:
                monitor.set_connected(False)
                slot.status = "failed"
                return _EXIT_ERROR

            if slot.status not in {"running", "healthy", "restarting"}:
                slot.status = "healthy"

            try:
                await session_task
            except asyncio.CancelledError:
                monitor.set_connected(False)
                slot.status = "stopped"
                raise
            except Exception as exc:
                log_session_exception(name, exc)
                monitor.set_connected(False)
                slot.status = "failed"
                return exception_exit_code(exc)

            monitor.set_connected(False)
            slot.status = "stopped"
            return _EXIT_OK

    finally:
        if session_task is not None and not session_task.done():
            session_task.cancel()
            await _drain_task(session_task)


# ---------------------------------------------------------------------------
# Single-tunnel in-process runner
# ---------------------------------------------------------------------------


async def _run_single_in_process(
    tunnel_file: TunnelFile,
    entry: TunnelEntry,
    cli_overrides: CLIOverrides,
    slot: TunnelSlot,
    *,
    show_logs: bool,
) -> int:
    """Run exactly one tunnel in this process.

    The supervisor/worker process model is reserved for the multi-tunnel
    ``run`` command. Single-tunnel zero-config mode stays in-process for
    simplicity: there is exactly one failure domain anyway.
    """
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _on_signal(sig: signal.Signals) -> None:
        logger.info("received %s — shutting down tunnel", sig.name)
        shutdown_event.set()

    installed_signals = _install_signal_handlers(loop, _on_signal)

    tunnel_task = asyncio.create_task(
        _run_tunnel(
            entry.name,
            tunnel_file,
            entry,
            cli_overrides,
            slot,
            allow_unscoped_metrics=True,
            show_logs=show_logs,
        ),
        name=f"tunnel-{entry.name}",
    )
    shutdown_task = asyncio.create_task(
        shutdown_event.wait(),
        name="shutdown-sentinel",
    )

    try:
        done, _ = await asyncio.wait(
            {tunnel_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if shutdown_task in done and not tunnel_task.done():
            tunnel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await tunnel_task
            return _EXIT_INTERRUPTED

        if not shutdown_task.done():
            shutdown_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await shutdown_task

        if tunnel_task.cancelled():
            return _EXIT_INTERRUPTED

        exc = tunnel_task.exception()
        if exc is not None:
            return exception_exit_code(exc)

        result = tunnel_task.result()
        return result if isinstance(result, int) else _EXIT_ERROR

    finally:
        for sig in installed_signals:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)


# ---------------------------------------------------------------------------
# Multi-tunnel runner (supervisor)
# ---------------------------------------------------------------------------


async def _run_all(
    config_path: Path,
    entries: list[TunnelEntry],
    cli_overrides: CLIOverrides,
    slots: dict[str, TunnelSlot],
) -> int:
    """Run all tunnels via the subprocess supervisor and return worst exit code.

    Each tunnel runs in its own ``exectunnel _worker`` subprocess. The parent
    ingests NDJSON IPC frames and drives the dashboard via parent-owned
    ``TunnelSlot`` snapshots — there is no in-process tunnel work here.
    """
    from .._supervisor import Supervisor  # noqa: PLC0415

    supervisor = Supervisor(
        config_path=config_path,
        entries=entries,
        cli_overrides=cli_overrides,
        slots=slots,
    )
    result = await supervisor.run()
    return result.exit_code


# ---------------------------------------------------------------------------
# Slot construction
# ---------------------------------------------------------------------------


def _make_slots(
    tunnel_file: TunnelFile,
    active_entries: list[TunnelEntry],
    cli_overrides: CLIOverrides,
) -> dict[str, TunnelSlot]:
    active_names = {entry.name for entry in active_entries}
    slots: dict[str, TunnelSlot] = {}

    for tunnel in tunnel_file.tunnels:
        fallback_host = tunnel.socks_host or "127.0.0.1"
        fallback_url = str(tunnel.wss_url) if tunnel.wss_url else ""

        if tunnel.name in active_names:
            try:
                session_cfg, tun_cfg = tunnel_file.resolve(tunnel, cli_overrides)
                slots[tunnel.name] = TunnelSlot(
                    name=tunnel.name,
                    socks_host=tun_cfg.socks_host,
                    socks_port=tun_cfg.socks_port,
                    wss_url=str(session_cfg.wss_url),
                    status="starting",
                )
            except Exception as exc:
                logger.debug(
                    "[%s] slot pre-population resolve failed (%s)",
                    tunnel.name,
                    exc,
                )
                slots[tunnel.name] = TunnelSlot(
                    name=tunnel.name,
                    socks_host=fallback_host,
                    socks_port=tunnel.socks_port,
                    wss_url=fallback_url,
                    status="starting",
                )
        else:
            slots[tunnel.name] = TunnelSlot(
                name=tunnel.name,
                socks_host=fallback_host,
                socks_port=tunnel.socks_port,
                wss_url=fallback_url,
                status="disabled",
            )

    return slots


# ---------------------------------------------------------------------------
# Dashboard wrapper
# ---------------------------------------------------------------------------


async def _run_dashboard(
    slots: list[TunnelSlot],
    console: Any,
    mode: DashboardMode,
    start_time: float,
    runner: Callable[[], Awaitable[int]],
    *,
    show_logs: bool,
) -> int:
    async with UnifiedDashboard(
        slots,
        console,
        mode,
        show_logs=show_logs,
        start_time=start_time,
    ):
        return await runner()


# ---------------------------------------------------------------------------
# Public command entry points
# ---------------------------------------------------------------------------


def run_command(
    ctx: typer.Context,
    names: list[str],
    socks_port: int | None,
    socks_host: str | None,
    socks_allow_non_loopback: bool,
    socks_udp_associate_enabled: bool | None,
    socks_udp_bind_host: str | None,
    socks_udp_advertise_host: str | None,
    dns_upstream: str | None,
    dns_local_port: int | None,
    reconnect_max_retries: int | None,
    ping_interval: float | None,
    udp_flow_idle_timeout: float | None,
    show_logs: bool,
) -> None:
    """Run one, several, or all enabled tunnels from the config file."""
    app_ctx: AppContext = ctx.obj
    console = get_stderr_console()

    if app_ctx.config_load_error is not None:
        console.print(f"[bold red]✗[/bold red] {app_ctx.config_load_error}")
        raise typer.Exit(_EXIT_ERROR)

    if app_ctx.tunnel_file is None:
        console.print(
            "[bold red]✗[/bold red] No config file loaded. "
            "Pass [bold]--config[/bold] or create one of:\n"
            "  [dim]~/.config/exectunnel/config.toml[/dim]\n"
            "  [dim]~/.config/exectunnel/config.yaml[/dim]\n"
            "  [dim]~/.config/exectunnel/config.yml[/dim]"
        )
        raise typer.Exit(_EXIT_ERROR)

    tunnel_file = app_ctx.tunnel_file
    requested_names = _dedupe_names(names)

    if requested_names:
        try:
            entries = [tunnel_file.get_tunnel(name) for name in requested_names]
        except KeyError as exc:
            console.print(f"[bold red]✗[/bold red] {exc}")
            raise typer.Exit(_EXIT_ERROR) from exc
    else:
        entries = tunnel_file.enabled_tunnels()
        if not entries:
            console.print(
                "[yellow]⚠[/yellow] No enabled tunnels found in config. "
                "Set [bold]enabled = true[/bold] or pass tunnel names explicitly."
            )
            raise typer.Exit(_EXIT_OK)

    cli_overrides = CLIOverrides(
        socks_port=socks_port,
        socks_host=socks_host,
        socks_allow_non_loopback=_bool_flag_to_optional(socks_allow_non_loopback),
        socks_udp_associate_enabled=socks_udp_associate_enabled,
        socks_udp_bind_host=socks_udp_bind_host,
        socks_udp_advertise_host=parse_ip(
            socks_udp_advertise_host,
            param_hint="--udp-advertise-host",
        ),
        dns_upstream=parse_ip(dns_upstream, param_hint="--dns-upstream"),
        dns_local_port=dns_local_port,
        reconnect_max_retries=reconnect_max_retries,
        ping_interval=ping_interval,
        udp_flow_idle_timeout=udp_flow_idle_timeout,
    )

    slots = _make_slots(tunnel_file, entries, cli_overrides)
    active_slots = [slots[entry.name] for entry in entries]
    mode = DashboardMode.SINGLE if len(entries) == 1 else DashboardMode.MULTI

    if app_ctx.config_path is None:
        console.print(
            "[bold red]✗[/bold red] Cannot launch supervisor: no config path resolved."
        )
        raise typer.Exit(_EXIT_ERROR)

    config_path = app_ctx.config_path

    _print_startup_banner(entries)
    dashboard_start = time.monotonic()

    async def _main() -> int:
        return await _run_dashboard(
            active_slots,
            console,
            mode,
            dashboard_start,
            lambda: _run_all(
                config_path,
                entries,
                cli_overrides,
                slots,
            ),
            show_logs=show_logs,
        )

    try:
        raise typer.Exit(asyncio.run(_main()))
    except KeyboardInterrupt:
        raise typer.Exit(_EXIT_INTERRUPTED) from None


def run_single_command(
    ctx: typer.Context,  # noqa: ARG001
    wss_url: str,
    socks_port: int,
    socks_host: str | None,
    socks_allow_non_loopback: bool,
    socks_udp_associate_enabled: bool | None,
    socks_udp_bind_host: str | None,
    socks_udp_advertise_host: str | None,
    dns_upstream: str | None,
    dns_local_port: int | None,
    reconnect_max_retries: int | None,
    ping_interval: float | None,
    send_timeout: float | None,
    conn_ack_timeout: float | None,
    ready_timeout: float | None,
    udp_flow_idle_timeout: float | None,
    bootstrap_delivery: Literal["fetch", "upload"] | None,
    bootstrap_fetch_url: str | None,
    bootstrap_skip_if_present: bool,
    bootstrap_use_go_agent: bool,
    no_default_exclude: bool,
    ssl_no_verify: bool,
    ssl_ca_cert: str | None,
    ssl_cert: str | None,
    ssl_key: str | None,
    ws_header: list[str],
    show_logs: bool,
) -> None:
    """Run a single tunnel without a config file (zero-config mode)."""
    console = get_stderr_console()

    parsed_wss_url = parse_wss_url(wss_url, param_hint="WSS_URL")
    parsed_dns_upstream = parse_ip(dns_upstream, param_hint="--dns-upstream")
    parsed_fetch_url = parse_http_url(
        bootstrap_fetch_url,
        param_hint="--bootstrap-fetch-url",
    )
    headers = parse_ws_headers(ws_header)

    cli_overrides = CLIOverrides(
        wss_url=parsed_wss_url,
        socks_port=socks_port,
        socks_host=socks_host,
        socks_allow_non_loopback=_bool_flag_to_optional(socks_allow_non_loopback),
        socks_udp_associate_enabled=socks_udp_associate_enabled,
        socks_udp_bind_host=socks_udp_bind_host,
        socks_udp_advertise_host=parse_ip(
            socks_udp_advertise_host,
            param_hint="--udp-advertise-host",
        ),
        dns_upstream=parsed_dns_upstream,
        dns_local_port=dns_local_port,
        reconnect_max_retries=reconnect_max_retries,
        ping_interval=ping_interval,
        send_timeout=send_timeout,
        conn_ack_timeout=conn_ack_timeout,
        ready_timeout=ready_timeout,
        udp_flow_idle_timeout=udp_flow_idle_timeout,
        bootstrap_delivery=bootstrap_delivery,
        bootstrap_fetch_url=parsed_fetch_url,
        bootstrap_skip_if_present=_bool_flag_to_optional(bootstrap_skip_if_present),
        bootstrap_use_go_agent=_bool_flag_to_optional(bootstrap_use_go_agent),
        no_default_exclude=_bool_flag_to_optional(no_default_exclude),
        ws_headers=headers or None,
        ssl_no_verify=_bool_flag_to_optional(ssl_no_verify),
        ssl_ca_cert=parse_existing_file(ssl_ca_cert, param_hint="--ssl-ca-cert"),
        ssl_cert=parse_existing_file(ssl_cert, param_hint="--ssl-cert"),
        ssl_key=parse_existing_file(ssl_key, param_hint="--ssl-key"),
    )

    try:
        tunnel_file = make_single_tunnel_file(
            wss_url=str(parsed_wss_url),
            socks_port=socks_port,
        )
    except Exception as exc:
        console.print(f"[bold red]✗[/bold red] Invalid configuration: {exc}")
        raise typer.Exit(_EXIT_ERROR) from exc

    entry = tunnel_file.tunnels[0]

    try:
        session_cfg, tun_cfg = tunnel_file.resolve(entry, cli_overrides)
        slot = TunnelSlot(
            name=entry.name,
            socks_host=tun_cfg.socks_host,
            socks_port=tun_cfg.socks_port,
            wss_url=str(session_cfg.wss_url),
            status="starting",
        )
    except Exception:
        slot = TunnelSlot(
            name=entry.name,
            socks_host=socks_host or "127.0.0.1",
            socks_port=socks_port,
            wss_url=str(parsed_wss_url),
            status="starting",
        )

    _print_startup_banner([entry])
    dashboard_start = time.monotonic()

    async def _main() -> int:
        return await _run_dashboard(
            [slot],
            console,
            DashboardMode.SINGLE,
            dashboard_start,
            lambda: _run_single_in_process(
                tunnel_file,
                entry,
                cli_overrides,
                slot,
                show_logs=show_logs,
            ),
            show_logs=show_logs,
        )

    try:
        raise typer.Exit(asyncio.run(_main()))
    except KeyboardInterrupt:
        raise typer.Exit(_EXIT_INTERRUPTED) from None
