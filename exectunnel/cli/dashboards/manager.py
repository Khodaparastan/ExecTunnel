"""Live Rich TUI dashboard for the multi-tunnel manager."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta
from typing import Final

from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..supervisor import ManagerConfig, TunnelMetrics, TunnelRuntimeState
from ..ui import THEME, Icons

__all__ = ["ManagerDashboard"]

_REFRESH_HZ: int = 2
_FRAME_INTERVAL: float = 1.0 / _REFRESH_HZ
_BYTES_UNIT_BASE: int = 1024
_ERROR_BAD_STYLE_THRESHOLD: int = 5

_RUNNING_STATES: frozenset[str] = frozenset({"running", "healthy"})

_ICON_STOPPED: str = "○"

_STATUS_DISPLAY: dict[str, tuple[str, str]] = {
    "starting": ("◌", "et.conn.pend"),
    "running": (Icons.PULSE, "et.conn.pend"),
    "healthy": ("●", "et.stat.good"),
    "unhealthy": (Icons.CROSS, "et.stat.bad"),
    "restarting": (Icons.RECONNECT, "et.warn"),
    "stopped": (_ICON_STOPPED, "et.conn.closed"),
}
_STATUS_DISPLAY_DEFAULT: tuple[str, str] = ("?", "et.muted")
_LOG_LEVEL_STYLES: Final[dict[str, str]] = {
    "DEBUG": "dim cyan",
    "INFO": "green",
    "WARNING": "yellow",
    "ERROR": "bold red",
}


def _fmt_uptime(secs: float) -> str:
    td = timedelta(seconds=int(secs))
    h, rem = divmod(td.seconds, 3600)
    m, s = divmod(rem, 60)
    if td.days:
        return f"{td.days}d {h:02d}:{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_bytes(n: int) -> str:
    """Human-readable byte count (e.g. '1.2 MB')."""
    if n < _BYTES_UNIT_BASE:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= _BYTES_UNIT_BASE
        if abs(n) < _BYTES_UNIT_BASE:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


def _err_style(count: int) -> str:
    if count == 0:
        return "et.muted"
    return "et.stat.bad" if count >= _ERROR_BAD_STYLE_THRESHOLD else "et.stat.warn"


def _label_col() -> Table:
    t = Table.grid(padding=(0, 1))
    t.add_column(style="et.label", min_width=16)
    t.add_column(style="et.value")
    return t


def _aggregate_metrics(states: list[TunnelRuntimeState]) -> TunnelMetrics:
    """Sum per-pod metrics into a single aggregate snapshot.

    Integer fields are summed; ``dns_enabled`` is OR'd; float/Optional
    fields are left at their zero/None defaults (not meaningful to aggregate).
    """
    agg = TunnelMetrics()
    for s in states:
        m = s.metrics
        agg.frames_sent += m.frames_sent
        agg.frames_recv += m.frames_recv
        agg.frames_dropped += m.frames_dropped
        agg.bytes_up += m.bytes_up
        agg.bytes_down += m.bytes_down
        agg.frames_decode_errors += m.frames_decode_errors
        agg.frames_orphaned += m.frames_orphaned
        agg.frames_noise += m.frames_noise
        agg.tcp_open += m.tcp_open
        agg.tcp_pending += m.tcp_pending
        agg.tcp_total += m.tcp_total
        agg.tcp_failed += m.tcp_failed
        agg.tcp_completed += m.tcp_completed
        agg.tcp_errors += m.tcp_errors
        agg.udp_open += m.udp_open
        agg.udp_total += m.udp_total
        agg.udp_flows_opened += m.udp_flows_opened
        agg.udp_flows_closed += m.udp_flows_closed
        agg.udp_datagrams_sent += m.udp_datagrams_sent
        agg.udp_datagrams_accepted += m.udp_datagrams_accepted
        agg.udp_datagrams_dropped += m.udp_datagrams_dropped
        agg.ack_ok += m.ack_ok
        agg.ack_timeout += m.ack_timeout
        agg.ack_failed += m.ack_failed
        agg.dns_queries += m.dns_queries
        agg.dns_ok += m.dns_ok
        agg.dns_dropped += m.dns_dropped
        agg.socks5_accepted += m.socks5_accepted
        agg.socks5_rejected += m.socks5_rejected
        agg.socks5_active += m.socks5_active
        agg.socks5_handshakes_ok += m.socks5_handshakes_ok
        agg.socks5_handshakes_error += m.socks5_handshakes_error
        agg.socks5_cmd_connect += m.socks5_cmd_connect
        agg.socks5_cmd_udp += m.socks5_cmd_udp
        agg.socks5_udp_relays_active += m.socks5_udp_relays_active
        agg.socks5_udp_datagrams += m.socks5_udp_datagrams
        agg.socks5_udp_dropped += m.socks5_udp_dropped
        agg.session_connect_attempts += m.session_connect_attempts
        agg.session_connect_ok += m.session_connect_ok
        agg.session_reconnects += m.session_reconnects
        agg.session_serve_started += m.session_serve_started
        agg.session_serve_stopped += m.session_serve_stopped
        agg.cleanup_tcp += m.cleanup_tcp
        agg.cleanup_pending += m.cleanup_pending
        agg.cleanup_udp += m.cleanup_udp
        agg.send_queue_depth += m.send_queue_depth
        agg.request_tasks += m.request_tasks
        if m.dns_enabled:
            agg.dns_enabled = True  # boolean OR across all tunnels
    return agg


class ManagerDashboard:
    """Full-screen live dashboard for the multi-tunnel manager.

    Thread safety
    -------------
    :meth:`on_state_change` is protected by a ``threading.Lock`` and may be
    called from any thread or asyncio task.  The render loop acquires the same
    lock only to snapshot the state dict at the start of each frame.
    """

    def __init__(self, cfg: ManagerConfig) -> None:
        self._cfg = cfg
        self._console = Console(theme=THEME, highlight=False)
        self._states: dict[str, TunnelRuntimeState] = {
            spec.display_name: TunnelRuntimeState(spec=spec) for spec in cfg.tunnels
        }
        self._states_lock = threading.Lock()
        self._start = time.monotonic()
        self._stop_event: asyncio.Event | None = None

    def on_state_change(self, state: TunnelRuntimeState) -> None:
        with self._states_lock:
            self._states[state.display_name] = state

    async def run_until_cancelled(self) -> None:
        self._stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        with Live(
            self._render(self._snapshot()),
            console=self._console,
            refresh_per_second=_REFRESH_HZ,
            screen=True,
        ) as live:
            try:
                deadline = loop.time() + _FRAME_INTERVAL
                while not self._stop_event.is_set():
                    live.update(self._render(self._snapshot()))
                    sleep_for = max(0.0, deadline - loop.time())
                    await asyncio.sleep(sleep_for)
                    deadline += _FRAME_INTERVAL
            except asyncio.CancelledError:
                pass
            finally:
                self._stop_event = None

    def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    def _snapshot(self) -> list[TunnelRuntimeState]:
        with self._states_lock:
            return list(self._states.values())

    # ── Layout ────────────────────────────────────────────────────────────

    def _render(self, states: list[TunnelRuntimeState]) -> Layout:
        layout = Layout()
        show_logs = self._cfg.show_logs

        if show_logs:
            layout.split_column(
                Layout(name="header", size=3),
                Layout(name="top", size=11),
                Layout(name="middle"),
                Layout(name="logs"),
                Layout(name="footer", size=3),
            )
        else:
            layout.split_column(
                Layout(name="header", size=3),
                Layout(name="top", size=11),
                Layout(name="middle"),
                Layout(name="footer", size=3),
            )

        layout["top"].split_row(
            Layout(name="summary", ratio=1),
            Layout(name="tunnels", ratio=2),
        )
        layout["middle"].split_column(
            Layout(name="aggregate", size=12),
            Layout(name="pod_detail"),
        )
        layout["header"].update(self._render_header())
        layout["summary"].update(self._render_summary(states))
        layout["tunnels"].update(self._render_tunnels(states))
        layout["aggregate"].update(self._render_aggregate(states))
        layout["pod_detail"].update(self._render_pod_detail(states))
        if show_logs:
            layout["logs"].update(self._render_pod_logs(states))
        layout["footer"].update(self._render_footer(states))
        return layout

    # ── Panels ────────────────────────────────────────────────────────────

    def _render_header(self) -> Panel:
        uptime = _fmt_uptime(time.monotonic() - self._start)
        n = len(self._cfg.tunnels)
        parts = [
            Text(f" {Icons.TUNNEL} ExecTunnel Manager  ", style="et.brand"),
            Text(f"{Icons.CLOCK} {uptime}", style="et.muted"),
            Text(
                f"  {Icons.BULLET} {n} tunnel{'s' if n != 1 else ''}", style="et.muted"
            ),
        ]
        return Panel(
            Align.left(Text.assemble(*parts)),
            style="et.border",
            padding=(0, 1),
        )

    @staticmethod
    def _render_summary(states: list[TunnelRuntimeState]) -> Panel:
        total = len(states)
        healthy = sum(1 for s in states if s.status == "healthy")
        running = sum(1 for s in states if s.status in _RUNNING_STATES)
        unhealthy = sum(1 for s in states if s.status == "unhealthy")
        restarting = sum(1 for s in states if s.status == "restarting")
        stopped = sum(1 for s in states if s.status == "stopped")
        total_restarts = sum(s.restart_count for s in states)
        total_health_failures = sum(s.health_failures for s in states)

        t = _label_col()
        t.add_row(f"{Icons.PULSE} Total", str(total))
        t.add_row(
            f"{Icons.CHECK} Healthy",
            Text(
                str(healthy),
                style="et.stat.good" if healthy == total else "et.stat.warn",
            ),
        )
        t.add_row(
            f"{Icons.BOLT} Running",
            Text(str(running), style="et.conn.open" if running else "et.muted"),
        )
        t.add_row(
            f"{Icons.WARN} Unhealthy",
            Text(str(unhealthy), style="et.stat.bad" if unhealthy else "et.muted"),
        )
        t.add_row(
            f"{Icons.RECONNECT} Restarting",
            Text(str(restarting), style="et.warn" if restarting else "et.muted"),
        )
        t.add_row(
            f"{_ICON_STOPPED} Stopped",
            Text(str(stopped), style="et.conn.closed" if stopped else "et.muted"),
        )
        t.add_row(
            f"{Icons.RECONNECT} Restarts",
            Text(
                str(total_restarts), style="et.warn" if total_restarts else "et.muted"
            ),
        )
        t.add_row(
            f"{Icons.WARN} Health fails",
            Text(
                str(total_health_failures),
                style="et.stat.bad" if total_health_failures else "et.muted",
            ),
        )

        return Panel(
            t,
            title=f"[et.brand]{Icons.PULSE} Cluster[/et.brand]",
            border_style="et.border",
            padding=(0, 1),
        )

    @staticmethod
    def _render_aggregate(states: list[TunnelRuntimeState]) -> Panel:
        agg = _aggregate_metrics(states)

        t = Table.grid(padding=(0, 2))
        for _ in range(4):
            t.add_column(style="et.label", min_width=16)
            t.add_column(style="et.value")

        t.add_row(
            f"{Icons.UP} Bytes Up",
            Text(_fmt_bytes(agg.bytes_up), style="et.value"),
            f"{Icons.DOWN} Bytes Down",
            Text(_fmt_bytes(agg.bytes_down), style="et.value"),
            f"{Icons.FRAME} Frames",
            Text(
                f"{agg.frames_sent}{Icons.ARROW_UP} {agg.frames_recv}{Icons.ARROW_DOWN}",
                style="et.value",
            ),
            f"{Icons.WARN} Dropped",
            Text(str(agg.frames_dropped), style=_err_style(agg.frames_dropped)),
        )
        t.add_row(
            f"{Icons.CONN} TCP Open",
            Text(
                str(agg.tcp_open), style="et.conn.open" if agg.tcp_open else "et.muted"
            ),
            f"{Icons.CONN} TCP Total",
            Text(str(agg.tcp_total), style="et.value"),
            f"{Icons.CONN} TCP Done",
            Text(str(agg.tcp_completed), style="et.value"),
            f"{Icons.WARN} TCP Errors",
            Text(
                str(agg.tcp_errors + agg.tcp_failed),
                style=_err_style(agg.tcp_errors + agg.tcp_failed),
            ),
        )
        t.add_row(
            f"{Icons.UDP} UDP Open",
            Text(
                str(agg.udp_open), style="et.conn.open" if agg.udp_open else "et.muted"
            ),
            f"{Icons.UDP} Flows",
            Text(
                f"{agg.udp_flows_opened}{Icons.ARROW_UP} {agg.udp_flows_closed}{Icons.ARROW_DOWN}",
                style="et.value",
            ),
            f"{Icons.UDP} Datagrams",
            Text(
                f"{agg.udp_datagrams_sent}s/{agg.udp_datagrams_accepted}a",
                style="et.value",
            ),
            f"{Icons.WARN} DG Drop",
            Text(
                str(agg.udp_datagrams_dropped),
                style=_err_style(agg.udp_datagrams_dropped),
            ),
        )
        frame_errs = agg.frames_decode_errors + agg.frames_orphaned + agg.frames_noise
        hs_err = agg.socks5_handshakes_error
        t.add_row(
            f"{Icons.CHECK} ACK",
            Text(
                f"{agg.ack_ok}ok {agg.ack_timeout}to {agg.ack_failed}f",
                style="et.stat.good" if agg.ack_failed == 0 else "et.stat.bad",
            ),
            f"{Icons.SOCKS} S5 Active",
            Text(
                str(agg.socks5_active),
                style="et.conn.open" if agg.socks5_active else "et.muted",
            ),
            f"{Icons.SOCKS} S5 Accept",
            Text(f"{agg.socks5_accepted}a/{agg.socks5_rejected}r", style="et.value"),
            f"{Icons.SOCKS} S5 HS",
            Text(
                f"{agg.socks5_handshakes_ok}ok/{hs_err}err",
                style="et.stat.good" if hs_err == 0 else "et.stat.bad",
            ),
        )
        total_cleanup = agg.cleanup_tcp + agg.cleanup_pending + agg.cleanup_udp
        t.add_row(
            f"{Icons.RECONNECT} Reconnects",
            Text(
                str(agg.session_reconnects),
                style="et.warn" if agg.session_reconnects else "et.muted",
            ),
            f"{Icons.DNS} DNS",
            Text(
                f"{agg.dns_ok}/{agg.dns_queries}" if agg.dns_enabled else "off",
                style="et.value" if agg.dns_enabled else "et.muted",
            ),
            f"{Icons.WARN} Cleanup",
            Text(str(total_cleanup), style="et.warn" if total_cleanup else "et.muted"),
            f"{Icons.BOLT} Queue/Tasks",
            Text(f"{agg.send_queue_depth}q/{agg.request_tasks}t", style="et.value"),
        )
        t.add_row(
            f"{Icons.WARN} Frame Errs",
            Text(str(frame_errs), style=_err_style(frame_errs)),
            f"{Icons.BOLT} Connect",
            Text(
                f"{agg.session_connect_ok}/{agg.session_connect_attempts}",
                style="et.value",
            ),
            f"{Icons.BOLT} Serve",
            Text(
                f"{agg.session_serve_started}{Icons.ARROW_UP}/{agg.session_serve_stopped}{Icons.ARROW_DOWN}",
                style="et.value",
            ),
            f"{Icons.SOCKS} S5 UDP",
            Text(
                f"{agg.socks5_udp_datagrams}dg/{agg.socks5_udp_relays_active}r"
                if agg.socks5_cmd_udp
                else "—",
                style="et.value" if agg.socks5_cmd_udp else "et.muted",
            ),
        )

        return Panel(
            t,
            title=f"[et.brand]{Icons.FRAME} Aggregate Metrics (all tunnels)[/et.brand]",
            border_style="et.border",
            padding=(0, 1),
        )

    @staticmethod
    def _render_tunnels(states: list[TunnelRuntimeState]) -> Panel:
        """Per-tunnel status table."""
        table = Table(
            box=box.SIMPLE_HEAD,
            header_style="et.label",
            border_style="et.border",
            expand=True,
            padding=(0, 1),
        )
        table.add_column("Tunnel", style="et.value", min_width=12)
        table.add_column("Status", width=11)
        table.add_column("PID", style="et.muted", width=7)
        table.add_column("SOCKS", style="et.value", width=6)
        table.add_column(f"{Icons.HEART} HP", width=4)
        table.add_column(f"{Icons.RECONNECT}", width=4, justify="right")
        table.add_column(f"{Icons.CLOCK} Uptime", width=11, justify="right")
        table.add_column("Exit", style="et.muted", width=4, justify="right")

        for s in states:
            icon, style = _STATUS_DISPLAY.get(s.status, _STATUS_DISPLAY_DEFAULT)
            status_text = Text(f"{icon} {s.status}", style=style)
            socks_ok_text = (
                Text(f"{Icons.CHECK}", style="et.stat.good")
                if s.socks_ok
                else Text("—", style="et.muted")
            )
            exit_str = str(s.exit_code) if s.exit_code is not None else "—"
            table.add_row(
                s.display_name,
                status_text,
                str(s.pid) if s.pid else "—",
                str(s.spec.socks_port),
                socks_ok_text,
                str(s.restart_count),
                _fmt_uptime(s.uptime_secs),
                exit_str,
            )

        if not states:
            table.add_row("—", "—", "—", "—", "—", "—", "—", "—")

        return Panel(
            table,
            title=f"[et.brand]{Icons.TUNNEL} Tunnel Status[/et.brand]",
            border_style="et.border",
            padding=(0, 0),
        )

    @staticmethod
    def _render_pod_detail(states: list[TunnelRuntimeState]) -> Panel:
        """Per-pod comprehensive metrics — one card per tunnel."""
        if not states:
            return Panel(
                Text("No tunnels configured", style="et.muted"),
                title=f"[et.brand]{Icons.BOLT} Per-Pod Metrics[/et.brand]",
                border_style="et.border",
            )

        cards: list[Panel] = []
        for s in states:
            m = s.metrics
            has_data = m.frames_sent > 0 or m.frames_recv > 0 or m.bytes_up > 0

            icon, style = _STATUS_DISPLAY.get(s.status, _STATUS_DISPLAY_DEFAULT)

            t = Table.grid(padding=(0, 1))
            t.add_column(style="et.label", min_width=14)
            t.add_column(style="et.value")

            t.add_row("Status", Text(f"{icon} {s.status}", style=style))
            t.add_row(
                "SOCKS",
                Text(
                    f":{s.spec.socks_port} {Icons.CHECK}"
                    if s.socks_ok
                    else f":{s.spec.socks_port} —",
                    style="et.stat.good" if s.socks_ok else "et.muted",
                ),
            )

            if not has_data:
                t.add_row("", Text("awaiting data…", style="et.muted"))
                cards.append(
                    Panel(
                        t,
                        title=f"[et.brand]{s.display_name}[/et.brand]",
                        border_style="et.border",
                        padding=(0, 1),
                        width=42,
                    )
                )
                continue

            if m.bootstrap_ok:
                boot_str = f"{Icons.CHECK} OK"
                if m.bootstrap_duration is not None:
                    boot_str += f" ({m.bootstrap_duration:.1f}s)"
                t.add_row(
                    f"{Icons.LOCK} Bootstrap", Text(boot_str, style="et.stat.good")
                )
            else:
                t.add_row(
                    f"{Icons.LOCK} Bootstrap", Text("pending", style="et.stat.warn")
                )

            t.add_row(
                f"{Icons.FRAME} Frames",
                Text(
                    f"{m.frames_sent}{Icons.ARROW_UP} {m.frames_recv}{Icons.ARROW_DOWN}",
                    style="et.value",
                ),
            )
            t.add_row(
                f"{Icons.UP} Traffic",
                Text(
                    f"{_fmt_bytes(m.bytes_up)}{Icons.ARROW_UP} {_fmt_bytes(m.bytes_down)}{Icons.ARROW_DOWN}",
                    style="et.value",
                ),
            )
            if m.frames_dropped:
                t.add_row(
                    f"{Icons.WARN} Dropped",
                    Text(str(m.frames_dropped), style=_err_style(m.frames_dropped)),
                )
            frame_errs = m.frames_decode_errors + m.frames_orphaned + m.frames_noise
            if frame_errs:
                t.add_row(
                    f"{Icons.WARN} Frame Errs",
                    Text(str(frame_errs), style="et.stat.bad"),
                )

            t.add_row(
                f"{Icons.CONN} TCP",
                Text(
                    f"{m.tcp_open}open {m.tcp_pending}pend {m.tcp_total}tot",
                    style="et.conn.open" if m.tcp_open else "et.value",
                ),
            )
            if m.tcp_completed or m.tcp_errors or m.tcp_failed:
                errs = m.tcp_errors + m.tcp_failed
                t.add_row(
                    "  done/err",
                    Text(
                        f"{m.tcp_completed}done {errs}err",
                        style="et.stat.good" if errs == 0 else "et.stat.bad",
                    ),
                )

            if m.udp_open or m.udp_total or m.udp_flows_opened:
                t.add_row(
                    f"{Icons.UDP} UDP",
                    Text(
                        f"{m.udp_open}open {m.udp_total}tot",
                        style="et.conn.open" if m.udp_open else "et.value",
                    ),
                )
                if m.udp_flows_opened:
                    t.add_row(
                        "  flows",
                        Text(
                            f"{m.udp_flows_opened}{Icons.ARROW_UP} {m.udp_flows_closed}{Icons.ARROW_DOWN}"
                        ),
                    )
                if m.udp_datagrams_sent or m.udp_datagrams_accepted:
                    t.add_row(
                        "  datagrams",
                        Text(
                            f"{m.udp_datagrams_sent}s {m.udp_datagrams_accepted}a {m.udp_datagrams_dropped}d"
                        ),
                    )

            ack_total = m.ack_ok + m.ack_timeout + m.ack_failed
            if ack_total:
                t.add_row(
                    f"{Icons.CHECK} ACK",
                    Text(
                        f"{m.ack_ok}ok {m.ack_timeout}to {m.ack_failed}f",
                        style="et.stat.good" if m.ack_failed == 0 else "et.stat.bad",
                    ),
                )

            if m.socks5_accepted or m.socks5_active:
                t.add_row(
                    f"{Icons.SOCKS} SOCKS5",
                    Text(
                        f"{m.socks5_active}act {m.socks5_accepted}acc {m.socks5_rejected}rej",
                        style="et.conn.open" if m.socks5_active else "et.value",
                    ),
                )
                hs_err = m.socks5_handshakes_error
                if m.socks5_handshakes_ok or hs_err:
                    t.add_row(
                        "  handshake",
                        Text(
                            f"{m.socks5_handshakes_ok}ok {hs_err}err",
                            style="et.stat.good" if hs_err == 0 else "et.stat.bad",
                        ),
                    )
                if m.socks5_cmd_udp:
                    t.add_row(
                        "  UDP relay",
                        Text(
                            f"{m.socks5_udp_relays_active}act {m.socks5_udp_datagrams}dg {m.socks5_udp_dropped}drop"
                        ),
                    )

            if m.dns_enabled:
                t.add_row(
                    f"{Icons.DNS} DNS",
                    Text(
                        f"{m.dns_ok}/{m.dns_queries} ok ({m.dns_dropped}drop)",
                        style="et.value",
                    ),
                )

            if m.session_connect_attempts:
                t.add_row(
                    f"{Icons.BOLT} Connect",
                    Text(
                        f"{m.session_connect_ok}/{m.session_connect_attempts}",
                        style="et.value",
                    ),
                )
            if m.session_reconnects:
                parts = str(m.session_reconnects)
                if m.reconnect_delay_avg is not None:
                    parts += f" (avg {m.reconnect_delay_avg:.1f}s)"
                t.add_row(f"{Icons.RECONNECT} Reconnect", Text(parts, style="et.warn"))

            total_cleanup = m.cleanup_tcp + m.cleanup_pending + m.cleanup_udp
            if total_cleanup:
                t.add_row(
                    f"{Icons.WARN} Cleanup",
                    Text(
                        f"tcp={m.cleanup_tcp} pend={m.cleanup_pending} udp={m.cleanup_udp}",
                        style="et.stat.warn",
                    ),
                )

            if m.send_queue_depth or m.request_tasks:
                t.add_row(
                    f"{Icons.BOLT} Queue",
                    Text(
                        f"{m.send_queue_depth}q {m.request_tasks}tasks",
                        style="et.value",
                    ),
                )

            cards.append(
                Panel(
                    t,
                    title=f"[et.brand]{s.display_name}[/et.brand]",
                    border_style="et.border",
                    padding=(0, 1),
                    width=42,
                )
            )

        return Panel(
            Columns(cards, equal=True, expand=True),
            title=f"[et.brand]{Icons.BOLT} Per-Pod Metrics[/et.brand]",
            border_style="et.border",
            padding=(0, 0),
        )

    @staticmethod
    def _render_pod_logs(states: list[TunnelRuntimeState]) -> Panel:
        lines: list[Text] = []
        max_lines_per_pod = max(5, 40 // max(len(states), 1))

        for s in states:
            recent = list(s.log_lines)[-max_lines_per_pod:]
            if not recent:
                continue
            lines.append(Text(f"── {s.display_name} ──", style="et.brand"))
            for raw in recent:
                style = "et.muted"
                upper = raw[:40].upper()
                for lvl, lvl_style in _LOG_LEVEL_STYLES.items():
                    if lvl in upper:
                        style = lvl_style
                        break
                lines.append(Text(raw, style=style, overflow="ellipsis", no_wrap=True))

        if not lines:
            lines.append(Text("No pod output yet.", style="et.muted"))

        return Panel(
            Group(*lines),
            title=f"[et.brand]{Icons.CLOCK} Pod Logs[/et.brand]",
            border_style="et.border",
            padding=(0, 1),
        )

    @staticmethod
    def _render_footer(states: list[TunnelRuntimeState]) -> Panel:
        ports = "  ".join(
            f"socks5://{s.spec.socks_host}:{s.spec.socks_port}"
            for s in states
            if s.status in _RUNNING_STATES
        )
        footer = Text.assemble(
            Text("  Active: ", style="et.muted"),
            Text(ports or "none", style="et.value"),
            Text("  │  Ctrl+C to stop all tunnels", style="et.muted"),
        )
        return Panel(footer, style="et.border", padding=(0, 1))
