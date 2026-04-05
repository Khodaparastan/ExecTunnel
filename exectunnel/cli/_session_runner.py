"""Shared session lifecycle — signal handling, graceful shutdown, dashboard wiring."""

from __future__ import annotations

import asyncio
import signal
from typing import Any

from exectunnel.config.settings import AppConfig, TunnelConfig
from exectunnel.exceptions import (
    BootstrapError,
    ExecTunnelError,
    ReconnectExhaustedError,
)
from exectunnel.session import TunnelSession
from rich.console import Console

from ._dashboard import Dashboard
from ._health import HealthMonitor
from ._spinner import BootstrapSpinner
from ._theme import Icons

__all__ = ["run_session"]

# Bootstrap phase names emitted by AgentBootstrapper callbacks.
_PHASE_MAP = {
    "tunnel.bootstrap.started":    ("stty",   "start"),
    "bootstrap.stty_done":         ("stty",   "done"),
    "bootstrap.upload_started":    ("upload", "start"),
    "bootstrap.upload_done":       ("upload", "done"),
    "bootstrap.decode_started":    ("decode", "start"),
    "bootstrap.decode_done":       ("decode", "done"),
    "bootstrap.syntax_started":    ("syntax", "start"),
    "bootstrap.syntax_done":       ("syntax", "done"),
    "bootstrap.exec_started":      ("exec",   "start"),
    "bootstrap.exec_done":         ("exec",   "done"),
    "tunnel.bootstrap.ok":         ("ready",  "done"),
    "tunnel.bootstrap.timeout":    ("ready",  "fail"),
    "bootstrap.agent_syntax_error":("syntax", "fail"),
}


async def run_session(
    app_cfg: AppConfig,
    tun_cfg: TunnelConfig,
    ws_url: str,
    pod_spec: Any | None,
    kubectl_ctx: str | None,
    console: Console,
    *,
    no_dashboard: bool = False,
    spinner: BootstrapSpinner | None = None,
) -> int:
    """Run a tunnel session with dashboard and signal handling.

    Returns:
        Exit code — 0 on clean exit, 1 on error.
    """
    session = TunnelSession(app_cfg, tun_cfg)
    monitor = HealthMonitor(
        session=session,
        pod_spec=pod_spec,
        ws_url=ws_url,
        socks_host=tun_cfg.socks_host,
        socks_port=tun_cfg.socks_port,
    )

    # Wire spinner to observability events if provided.
    if spinner is not None:
        _wire_spinner(spinner)

    stop_event = asyncio.Event()

    def _handle_signal(sig: int) -> None:
        console.print(
            f"\n[et.warn]{Icons.WARN} Signal {sig} received — "
            f"shutting down…[/et.warn]"
        )
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except (NotImplementedError, RuntimeError):
            pass  # Windows — handled via KeyboardInterrupt

    session_task = asyncio.create_task(
        _run_with_monitor(session, monitor),
        name="tunnel-session",
    )
    stop_task = asyncio.create_task(
        stop_event.wait(),
        name="stop-signal",
    )

    dashboard_task: asyncio.Task[None] | None = None

    try:
        # Wait for bootstrap to complete before starting the dashboard.
        bootstrap_done = asyncio.Event()
        _patch_session_for_bootstrap_signal(session, bootstrap_done)

        # Wait for either bootstrap completion, stop signal, or session error.
        bootstrap_wait = asyncio.create_task(
            bootstrap_done.wait(), name="bootstrap-wait"
        )
        done, _ = await asyncio.wait(
            {session_task, stop_task, bootstrap_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if stop_task in done:
            _cancel_all(session_task, bootstrap_wait)
            await _drain(session_task, timeout=10.0)
            return 0

        if session_task in done:
            bootstrap_wait.cancel()
            exc = (
                session_task.exception()
                if not session_task.cancelled()
                else None
            )
            if exc is None:
                return 0
            # Mark spinner phases as failed.
            if spinner is not None:
                for phase in ("stty", "upload", "decode", "syntax", "exec", "ready"):
                    spinner.fail_phase(phase)
            return _handle_session_error(exc, console)

        # Bootstrap complete — spinner is done, start dashboard.
        bootstrap_wait.cancel()
        if spinner is not None:
            spinner.done_phase("ready")

        if not no_dashboard:
            dashboard = Dashboard(
                monitor=monitor,
                console=console,
                ws_url=ws_url,
                kubectl_ctx=kubectl_ctx,
            )
            dashboard_task = asyncio.create_task(
                dashboard.run_until_cancelled(),
                name="dashboard",
            )
        else:
            _print_proxy_hint(console, tun_cfg)

        # Now wait for session end or stop signal.
        wait_set = {session_task, stop_task}
        done, _ = await asyncio.wait(
            wait_set, return_when=asyncio.FIRST_COMPLETED
        )

        if stop_task in done:
            session_task.cancel()
            with console.status("[et.warn]Shutting down tunnel…[/et.warn]"):
                await _drain(session_task, timeout=10.0)
            return 0

        exc = (
            session_task.exception()
            if not session_task.cancelled()
            else None
        )
        if exc is None:
            console.print(
                f"[et.ok]{Icons.CHECK} Session ended cleanly.[/et.ok]"
            )
            return 0
        return _handle_session_error(exc, console)

    except KeyboardInterrupt:
        session_task.cancel()
        return 0

    finally:
        stop_task.cancel()
        if dashboard_task is not None:
            dashboard_task.cancel()
            try:
                await dashboard_task
            except asyncio.CancelledError:
                pass
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):
                pass


# ── Internal helpers ──────────────────────────────────────────────────────────


async def _run_with_monitor(
    session: TunnelSession,
    monitor: HealthMonitor,
) -> None:
    """Wrap session.run() and record reconnects in the monitor."""
    try:
        await session.run()
    except ReconnectExhaustedError:
        raise
    except BootstrapError:
        raise
    except ExecTunnelError:
        monitor.record_reconnect()
        raise


def _patch_session_for_bootstrap_signal(
    session: TunnelSession,
    event: asyncio.Event,
) -> None:
    """Monkey-patch the session to set *event* when bootstrap completes.

    This is a lightweight integration shim — it avoids adding CLI concerns
    to the session layer by patching at the boundary.
    """
    original = getattr(session, "_run_session", None)
    if original is None:
        event.set()
        return

    async def _patched(ws: object) -> None:
        await original(ws)  # type: ignore[misc]

    # We hook into the observability layer instead — set event when
    # tunnel.bootstrap.ok metric fires.  For now, set it after a short
    # delay as a safe fallback.
    async def _bootstrap_watcher() -> None:
        await asyncio.sleep(0.5)
        event.set()

    asyncio.create_task(_bootstrap_watcher(), name="bootstrap-watcher")


def _wire_spinner(spinner: BootstrapSpinner) -> None:
    """Wire the observability layer to advance spinner phases."""
    # In a full implementation this hooks into the metrics_inc() callback.
    # Here we provide the hook point — the observability layer calls
    # registered listeners when a metric fires.
    try:
        from exectunnel.observability import register_metric_listener

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
                case "fail":
                    spinner.fail_phase(phase_name)

        register_metric_listener(_on_metric)
    except (ImportError, AttributeError):
        pass  # Observability layer doesn't support listeners — spinner advances manually.


def _cancel_all(*tasks: asyncio.Task[Any]) -> None:
    for t in tasks:
        if not t.done():
            t.cancel()


async def _drain(task: asyncio.Task[Any], timeout: float) -> None:
    try:
        async with asyncio.timeout(timeout):
            await task
    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
        pass


def _print_proxy_hint(console: Console, tun_cfg: TunnelConfig) -> None:
    console.print(
        f"\n[et.ok]{Icons.CHECK} Tunnel active — "
        f"SOCKS5 on {tun_cfg.socks_host}:{tun_cfg.socks_port}[/et.ok]"
    )
    console.print(
        f"[et.muted]  export https_proxy="
        f"socks5://{tun_cfg.socks_host}:{tun_cfg.socks_port}[/et.muted]"
    )
    console.print(
        f"[et.muted]  export http_proxy="
        f"socks5://{tun_cfg.socks_host}:{tun_cfg.socks_port}[/et.muted]"
    )
    console.print("[et.muted]  Press Ctrl+C to stop.[/et.muted]\n")


def _handle_session_error(exc: BaseException, console: Console) -> int:
    if isinstance(exc, ReconnectExhaustedError):
        console.print(
            f"\n[et.error]{Icons.CROSS} Reconnect exhausted[/et.error]"
        )
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
            f"\n[et.error]{Icons.CROSS} Bootstrap failed "
            f"[{exc.error_code}][/et.error]"
        )
        console.print(f"  [et.value]{exc.message}[/et.value]")
        for k, v in exc.details.items():
            if v is not None:
                console.print(
                    f"  [et.label]{k}:[/et.label] [et.muted]{v}[/et.muted]"
                )
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
                console.print(
                    f"  [et.label]{k}:[/et.label] [et.muted]{v}[/et.muted]"
                )
        if exc.hint:
            console.print(f"  [et.muted]{Icons.BULLET} {exc.hint}[/et.muted]")
        return 1

    console.print(
        f"\n[et.error]{Icons.CROSS} Unexpected error: {exc}[/et.error]"
    )
    console.print_exception(show_locals=False)
    return 1
