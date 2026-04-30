"""Unified live dashboard for ExecTunnel — handles single and multi-tunnel sessions."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final

from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ._formatting import (
    LOG_LEVEL_STYLES,
    ack_rate_str,
    err_style,
    fmt_bytes,
    fmt_uptime,
    label_col,
    status_dot,
    truncate_url,
)
from ._theme import Icons

if TYPE_CHECKING:
    from exectunnel.cli.metrics import HealthMonitor, TunnelHealth

__all__ = ["DashboardMode", "TunnelSlot", "UnifiedDashboard"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REFRESH_HZ: Final[int] = 4
_FRAME_INTERVAL: Final[float] = 1.0 / _REFRESH_HZ
_MAX_VISIBLE_CONNS: Final[int] = 18
_ACK_WARN_THRESHOLD: Final[int] = 5
_LOG_TAIL_LIMIT: Final[int] = 50

_ACTIVE_CONN_STATES: Final[frozenset[str]] = frozenset({"open", "pending"})

_CONN_STATE_STYLES: Final[dict[str, str]] = {
    "open": "et.conn.open",
    "pending": "et.conn.pend",
    "closing": "et.warn",
    "closed": "et.conn.closed",
}

_POD_PHASE_STYLES: Final[dict[str, str]] = {
    "Running": "et.stat.good",
    "Pending": "et.stat.warn",
    "Failed": "et.stat.bad",
}

_TUNNEL_STATUS_DISPLAY: Final[dict[str, tuple[str, str]]] = {
    "starting": ("◌", "et.conn.pend"),
    "running": (Icons.PULSE, "et.conn.pend"),
    "healthy": ("●", "et.stat.good"),
    "unhealthy": (Icons.CROSS, "et.stat.bad"),
    "restarting": (Icons.RECONNECT, "et.warn"),
    "stopped": ("○", "et.conn.closed"),
    "failed": (Icons.CROSS, "et.stat.bad"),
    "disabled": ("–", "et.muted"),
}
_TUNNEL_STATUS_DEFAULT: Final[tuple[str, str]] = ("?", "et.muted")

_DIM_DASH: Final[Text] = Text("—", style="dim")


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------


class DashboardMode(Enum):
    """Dashboard rendering mode."""

    SINGLE = auto()
    """Single tunnel — full detail panels."""

    MULTI = auto()
    """Multiple tunnels — overview + per-tunnel cards + aggregate metrics."""


# ---------------------------------------------------------------------------
# TunnelSlot — per-tunnel state container
# ---------------------------------------------------------------------------


@dataclass
class TunnelSlot:
    """Mutable state container for one tunnel in the dashboard.

    Attributes:
        name:           Human-readable tunnel name.
        socks_host:     SOCKS5 bind address.
        socks_port:     SOCKS5 listener port.
        wss_url:        WebSocket endpoint URL (display only).
        monitor:        Live :class:`~exectunnel.cli.metrics.HealthMonitor`
                        instance, or ``None`` before the session starts.
        status:         Lifecycle status string.
        log_buffer:     Optional ring-buffer for log tail rendering.
        registered_at:  Monotonic timestamp of slot registration.
    """

    name: str
    socks_host: str
    socks_port: int
    wss_url: str = ""
    monitor: HealthMonitor | None = None
    status: str = "starting"
    log_buffer: Any = None  # LogRingBuffer | None — avoid hard import
    registered_at: float = field(default_factory=time.monotonic)
    process: Any = None  # ProcessHealth | None — supervisor-owned, optional

    def snapshot(self) -> TunnelHealth | None:
        """Return a health snapshot, or ``None`` if no monitor is attached."""
        if self.monitor is None:
            return None
        return self.monitor.snapshot()


# ---------------------------------------------------------------------------
# UnifiedDashboard
# ---------------------------------------------------------------------------


class UnifiedDashboard:
    """Async context manager that renders a live-updating tunnel dashboard.

    Supports both single-tunnel detail view and multi-tunnel overview.

    Args:
        slots:      One or more :class:`TunnelSlot` instances.
        console:    Rich :class:`~rich.console.Console` to render into.
        mode:       :class:`DashboardMode` — ``SINGLE`` or ``MULTI``.
        show_logs:  When ``True``, render a log-tail panel.
        start_time: Optional override for the dashboard start timestamp.
    """

    __slots__ = (
        "_slots",
        "_console",
        "_mode",
        "_show_logs",
        "_start",
        "_live",
        "_task",
    )

    def __init__(
        self,
        slots: list[TunnelSlot],
        console: Console,
        mode: DashboardMode = DashboardMode.SINGLE,
        *,
        show_logs: bool = False,
        start_time: float | None = None,
    ) -> None:
        if not slots:
            raise ValueError("UnifiedDashboard requires at least one TunnelSlot.")
        self._slots = slots
        self._console = console
        self._mode = mode
        self._show_logs = show_logs
        self._start: float = start_time if start_time is not None else time.monotonic()
        self._live: Live | None = None
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> UnifiedDashboard:
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=_REFRESH_HZ,
            screen=False,
            transient=False,
        )
        self._live.__enter__()
        self._task = asyncio.create_task(self._refresh_loop(), name="dashboard-refresh")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        if self._live is not None:
            with contextlib.suppress(Exception):
                self._live.update(self._render())
            # Pass None to avoid propagating cancelled/exception state into Rich.
            self._live.__exit__(None, None, None)
            self._live = None

    # ------------------------------------------------------------------
    # Slot management (call from event loop only)
    # ------------------------------------------------------------------

    def update_slot_status(self, name: str, status: str) -> None:
        """Update the lifecycle status of a named slot. Silently ignored if not found."""
        for slot in self._slots:
            if slot.name == name:
                slot.status = status
                return

    def attach_monitor(self, name: str, monitor: HealthMonitor) -> None:
        """Attach a :class:`~exectunnel.cli.metrics.HealthMonitor` to a slot."""
        for slot in self._slots:
            if slot.name == name:
                slot.monitor = monitor
                return

    # ------------------------------------------------------------------
    # Internal refresh loop
    # ------------------------------------------------------------------

    async def _refresh_loop(self) -> None:
        """Push a fresh render into the live display at ``_REFRESH_HZ``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _FRAME_INTERVAL
        try:
            while True:
                sleep_for = max(0.0, deadline - loop.time())
                await asyncio.sleep(sleep_for)
                deadline += _FRAME_INTERVAL
                if self._live is not None:
                    self._live.update(self._render())
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Top-level render dispatcher
    # ------------------------------------------------------------------

    def _render(self) -> Layout:
        """Build the full layout for the current frame."""
        match self._mode:
            case DashboardMode.SINGLE:
                return self._render_single()
            case DashboardMode.MULTI:
                return self._render_multi()
            case _:  # pragma: no cover
                return self._render_single()

    # ==================================================================
    # SINGLE MODE
    # ==================================================================

    def _render_single(self) -> Layout:
        """Full-detail layout for a single tunnel session."""
        slot = self._slots[0]
        h = slot.snapshot()
        layout = Layout()

        log_sections: list[Layout] = []
        if self._show_logs and slot.log_buffer is not None:
            log_sections = [Layout(name="logs", ratio=2)]

        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="stats", size=16),
            Layout(name="lower"),
            Layout(name="footer", size=3),
        )

        if log_sections:
            layout["lower"].split_row(
                Layout(name="conns", ratio=3),
                *log_sections,
            )
            layout["lower"]["logs"].update(self._render_log_tail(slot))
        else:
            layout["lower"].update(self._render_connections_panel(h))

        layout["stats"].split_row(
            Layout(name="tunnel", ratio=1),
            Layout(name="traffic", ratio=1),
            Layout(name="dns_pod", ratio=1),
        )

        layout["header"].update(self._render_single_header(slot, h))
        layout["stats"]["tunnel"].update(self._render_tunnel_panel(h))
        layout["stats"]["traffic"].update(self._render_traffic_panel(h))
        layout["stats"]["dns_pod"].update(self._render_dns_pod_panel(h))
        if log_sections:
            layout["lower"]["conns"].update(self._render_connections_panel(h))
        layout["footer"].update(self._render_single_footer(slot, h))

        return layout

    # ── Single: Header ────────────────────────────────────────────────────

    def _render_single_header(self, slot: TunnelSlot, h: TunnelHealth | None) -> Panel:
        url_short = truncate_url(slot.wss_url)
        uptime = fmt_uptime(time.monotonic() - self._start)

        parts: list[Text] = [
            Text(f" {Icons.TUNNEL} ExecTunnel  ", style="et.brand"),
        ]

        if h is not None:
            parts.append(status_dot(h.connected))
            parts.append(Text(f"  {Icons.CLOCK} {uptime}", style="et.muted"))
            if h.reconnect_count:
                parts.append(
                    Text(f"  {Icons.RECONNECT} {h.reconnect_count}", style="et.warn")
                )
        else:
            parts.append(Text(slot.status, style="et.conn.pend"))
            parts.append(Text(f"  {Icons.CLOCK} {uptime}", style="et.muted"))

        parts.append(Text(f"  {Icons.GLOBE} {url_short}", style="et.muted"))

        return Panel(
            Align.left(Text.assemble(*parts)),
            style="et.border",
            padding=(0, 1),
        )

    # ── Single: Tunnel panel ──────────────────────────────────────────────

    @staticmethod
    def _render_tunnel_panel(h: TunnelHealth | None) -> Panel:
        t = label_col()

        if h is None:
            t.add_row("Status", Text("awaiting session…", style="et.muted"))
            return Panel(
                t,
                title=f"[et.brand]{Icons.TUNNEL} Tunnel[/et.brand]",
                border_style="et.border",
                padding=(0, 1),
            )

        socks_style = "et.stat.good" if h.socks_ok else "et.stat.bad"
        t.add_row(
            f"{Icons.SOCKS} SOCKS5",
            Text(f"{h.socks_host}:{h.socks_port}", style=socks_style),
        )
        t.add_row(
            f"{Icons.LOCK} Bootstrap",
            Text("OK", style="et.stat.good")
            if h.bootstrap_ok
            else Text("pending…", style="et.stat.warn"),
        )
        if h.bootstrap_duration is not None:
            t.add_row(
                "  boot time", Text(f"{h.bootstrap_duration:.1f}s", style="et.muted")
            )

        t.add_row(f"{Icons.BOLT} Sent", str(h.frames_sent))
        t.add_row(f"{Icons.BOLT} Recv", str(h.frames_recv))
        t.add_row(
            f"{Icons.WARN} Dropped",
            Text(str(h.frames_dropped), style=err_style(h.frames_dropped)),
        )

        frame_errs = (
            h.frames_decode_errors
            + h.frames_orphaned
            + h.frames_noise
            + h.frames_outbound_timeout
            + h.frames_outbound_ws_closed
        )
        if frame_errs:
            t.add_row(
                f"{Icons.WARN} Frame errs",
                Text(str(frame_errs), style="et.stat.bad"),
            )

        queue_str = (
            f"{h.send_queue_depth}/{h.send_queue_cap}" if h.send_queue_cap else "—"
        )
        t.add_row(f"{Icons.ARROW_UP} Queue", queue_str)
        if h.request_tasks:
            t.add_row("  tasks", str(h.request_tasks))

        t.add_row("", "")

        hs_err = h.socks5_handshakes_error + h.socks5_handshakes_timeout
        t.add_row(
            f"{Icons.SOCKS} S5 accept",
            Text(str(h.socks5_accepted), style="et.value"),
        )
        if h.socks5_active:
            t.add_row(
                f"{Icons.SOCKS} S5 active",
                Text(str(h.socks5_active), style="et.conn.open"),
            )
        t.add_row(
            f"{Icons.SOCKS} S5 hs ok",
            Text(
                str(h.socks5_handshakes_ok),
                style="et.stat.good" if h.socks5_handshakes_ok else "et.muted",
            ),
        )
        t.add_row(
            f"{Icons.WARN} S5 hs err",
            Text(str(hs_err), style=err_style(hs_err)),
        )
        if h.socks5_cmd_udp:
            t.add_row(
                f"{Icons.GLOBE} UDP relays",
                f"{h.socks5_udp_relays_active} active / {h.socks5_cmd_udp} total",
            )

        return Panel(
            t,
            title=f"[et.brand]{Icons.TUNNEL} Tunnel[/et.brand]",
            border_style="et.border",
            padding=(0, 1),
        )

    # ── Single: Traffic panel ─────────────────────────────────────────────

    @staticmethod
    def _render_traffic_panel(h: TunnelHealth | None) -> Panel:
        t = label_col()

        if h is None:
            t.add_row("", Text("no data yet", style="et.muted"))
            return Panel(
                t,
                title=f"[et.brand]{Icons.BOLT} Traffic[/et.brand]",
                border_style="et.border",
                padding=(0, 1),
            )

        t.add_row(
            f"{Icons.BYTES_UP} TCP open", Text(str(h.tcp_open), style="et.conn.open")
        )
        t.add_row(
            f"{Icons.BYTES_DOWN} TCP pend",
            Text(str(h.tcp_pending), style="et.conn.pend"),
        )
        t.add_row("TCP total", str(h.tcp_total))
        t.add_row("TCP failed", Text(str(h.tcp_failed), style=err_style(h.tcp_failed)))
        if h.tcp_completed:
            t.add_row("TCP done", str(h.tcp_completed))
        if h.tcp_errors:
            t.add_row("TCP errors", Text(str(h.tcp_errors), style="et.stat.bad"))

        t.add_row(
            f"{Icons.BYTES_UP} Bytes up",
            Text(fmt_bytes(h.bytes_up_total), style="et.value"),
        )
        t.add_row(
            f"{Icons.BYTES_DOWN} Bytes dn",
            Text(fmt_bytes(h.bytes_down_total), style="et.value"),
        )

        ack_ok = h.ack_ok
        ack_failed = h.ack_failed
        if ack_failed == 0:
            ack_style = "et.stat.good"
        elif ack_failed < _ACK_WARN_THRESHOLD:
            ack_style = "et.stat.warn"
        else:
            ack_style = "et.stat.bad"

        t.add_row(
            f"{Icons.HEART} ACK rate",
            Text(ack_rate_str(ack_ok, ack_failed), style=ack_style),
        )
        if h.ack_timeout:
            t.add_row("  ACK timeout", Text(str(h.ack_timeout), style="et.stat.warn"))

        t.add_row("", "")
        t.add_row("Connects", f"{h.session_connect_ok}/{h.session_connect_attempts}")
        if h.session_reconnects:
            t.add_row(
                f"{Icons.RECONNECT} Reconnects",
                Text(str(h.session_reconnects), style="et.warn"),
            )
            if h.reconnect_delay_avg is not None:
                t.add_row(
                    "  avg delay",
                    Text(f"{h.reconnect_delay_avg:.1f}s", style="et.muted"),
                )

        total_cleanup = h.cleanup_tcp + h.cleanup_pending + h.cleanup_udp
        if total_cleanup:
            t.add_row(
                "Cleanup",
                Text(
                    f"tcp={h.cleanup_tcp} pend={h.cleanup_pending} udp={h.cleanup_udp}",
                    style="et.stat.warn",
                ),
            )

        return Panel(
            t,
            title=f"[et.brand]{Icons.BOLT} Traffic[/et.brand]",
            border_style="et.border",
            padding=(0, 1),
        )

    # ── Single: DNS & Pod panel ───────────────────────────────────────────

    @staticmethod
    def _render_dns_pod_panel(h: TunnelHealth | None) -> Panel:
        t = label_col()

        if h is None:
            t.add_row(f"{Icons.DNS} DNS", Text("pending", style="et.muted"))
            return Panel(
                t,
                title=f"[et.brand]{Icons.DNS} DNS & {Icons.POD} Pod[/et.brand]",
                border_style="et.border",
                padding=(0, 1),
            )

        if h.dns_enabled:
            ok_rate = f"{h.dns_ok / h.dns_queries * 100:.1f}%" if h.dns_queries else "—"
            t.add_row(f"{Icons.DNS} Queries", str(h.dns_queries))
            t.add_row("DNS OK", Text(ok_rate, style="et.stat.good"))
            t.add_row(
                "DNS drops", Text(str(h.dns_dropped), style=err_style(h.dns_dropped))
            )
        else:
            t.add_row(f"{Icons.DNS} DNS", Text("disabled", style="et.muted"))

        t.add_row("", "")

        if h.pod_name:
            t.add_row(f"{Icons.POD} Pod", h.pod_name)
            t.add_row("Namespace", h.pod_namespace)
            phase_style = _POD_PHASE_STYLES.get(h.pod_phase, "et.muted")
            t.add_row("Phase", Text(h.pod_phase or "—", style=phase_style))
            if h.pod_node:
                t.add_row("Node", Text(h.pod_node, style="et.muted"))
        else:
            t.add_row(f"{Icons.POD} Pod", Text("raw WS mode", style="et.muted"))

        t.add_row("", "")
        t.add_row(f"{Icons.GLOBE} UDP", f"{h.udp_open} open / {h.udp_total} total")
        if h.udp_flows_opened:
            t.add_row("  opened", str(h.udp_flows_opened))
            t.add_row("  closed", str(h.udp_flows_closed))
        if h.udp_datagrams_sent or h.udp_datagrams_accepted:
            t.add_row(
                "  dgrams",
                f"{Icons.ARROW_UP}{h.udp_datagrams_sent} "
                f"{Icons.ARROW_DOWN}{h.udp_datagrams_accepted}",
            )
        if h.udp_datagrams_dropped:
            t.add_row(
                "  drops",
                Text(str(h.udp_datagrams_dropped), style="et.stat.bad"),
            )

        return Panel(
            t,
            title=f"[et.brand]{Icons.DNS} DNS & {Icons.POD} Pod[/et.brand]",
            border_style="et.border",
            padding=(0, 1),
        )

    # ── Single: Connections panel ─────────────────────────────────────────

    @staticmethod
    def _render_connections_panel(h: TunnelHealth | None) -> Panel:
        table = Table(
            box=box.SIMPLE_HEAD,
            header_style="et.label",
            border_style="et.border",
            expand=True,
            padding=(0, 1),
        )
        table.add_column("ID", style="et.muted", width=10, no_wrap=True)
        table.add_column("Host", style="et.value", ratio=3, no_wrap=True)
        table.add_column("Port", style="et.value", width=6)
        table.add_column("State", width=9)
        table.add_column(Icons.BYTES_UP, style="et.value", width=9, justify="right")
        table.add_column(Icons.BYTES_DOWN, style="et.value", width=9, justify="right")
        table.add_column("Drops", style="et.stat.bad", width=6, justify="right")

        if h is None or not h.recent_conns:
            table.add_row("—", "No connections yet", "—", "—", "—", "—", "—")
            return Panel(
                table,
                title=f"[et.brand]{Icons.GLOBE} Active Connections[/et.brand]",
                border_style="et.border",
                padding=(0, 0),
            )

        active = [c for c in h.recent_conns if c.state in _ACTIVE_CONN_STATES]
        closed = [c for c in h.recent_conns if c.state not in _ACTIVE_CONN_STATES]
        remaining = max(0, _MAX_VISIBLE_CONNS - len(active))
        visible = (
            (active + closed[-remaining:]) if remaining else active[:_MAX_VISIBLE_CONNS]
        )

        for conn in visible:
            state_style = _CONN_STATE_STYLES.get(conn.state, "et.muted")
            table.add_row(
                conn.conn_id[:8],
                conn.host,
                str(conn.port),
                Text(conn.state, style=state_style),
                fmt_bytes(conn.bytes_up),
                fmt_bytes(conn.bytes_down),
                str(conn.drop_count) if conn.drop_count else "—",
            )

        return Panel(
            table,
            title=f"[et.brand]{Icons.GLOBE} Active Connections[/et.brand]",
            border_style="et.border",
            padding=(0, 0),
        )

    # ── Single: Log tail panel ────────────────────────────────────────────

    @staticmethod
    def _render_log_tail(slot: TunnelSlot) -> Panel:
        buf = slot.log_buffer
        if buf is None:
            return Panel(
                Text("Log buffer not initialised.", style="et.muted"),
                title=f"[et.brand]{Icons.CLOCK} Log Tail[/et.brand]",
                border_style="et.border",
                padding=(0, 1),
            )

        try:
            entries = buf.entries()
        except Exception:  # noqa: BLE001
            entries = []

        tail = entries[-_LOG_TAIL_LIMIT:]
        lines: list[Text] = []

        for e in tail:
            lvl_style = LOG_LEVEL_STYLES.get(getattr(e, "level", ""), "et.muted")
            line = Text.assemble(
                Text(f"{getattr(e, 'ts', '')} ", style="et.muted"),
                Text(f"{getattr(e, 'level', ''):<7} ", style=lvl_style),
                Text(f"{getattr(e, 'logger', '')} ", style="dim"),
                Text(getattr(e, "message", str(e))),
            )
            lines.append(line)

        if not lines:
            lines.append(Text("No log entries yet.", style="et.muted"))

        return Panel(
            Group(*lines),
            title=f"[et.brand]{Icons.CLOCK} Log Tail[/et.brand]",
            border_style="et.border",
            padding=(0, 1),
        )

    # ── Single: Footer ────────────────────────────────────────────────────

    @staticmethod
    def _render_single_footer(slot: TunnelSlot, h: TunnelHealth | None) -> Panel:
        host = h.socks_host if h else slot.socks_host
        port = h.socks_port if h else slot.socks_port
        proxy_env = f"socks5://{host}:{port}"
        footer = Text.assemble(
            Text("  export https_proxy=", style="et.muted"),
            Text(proxy_env, style="et.value"),
            Text("  │  Ctrl+C to stop", style="et.muted"),
        )
        return Panel(footer, style="et.border", padding=(0, 1))

    # ==================================================================
    # MULTI MODE
    # ==================================================================

    def _render_multi(self) -> Layout:
        """Overview layout for multiple concurrent tunnel sessions."""
        layout = Layout()

        sections: list[Layout] = [
            Layout(name="header", size=3),
            Layout(name="top", size=11),
            Layout(name="middle"),
        ]
        if self._show_logs:
            sections.append(Layout(name="logs"))
        sections.append(Layout(name="footer", size=3))

        layout.split_column(*sections)

        layout["top"].split_row(
            Layout(name="summary", ratio=1),
            Layout(name="tunnels", ratio=2),
        )
        layout["middle"].split_column(
            Layout(name="aggregate", size=12),
            Layout(name="pod_cards"),
        )

        layout["header"].update(self._render_multi_header())
        layout["top"]["summary"].update(self._render_multi_summary())
        layout["top"]["tunnels"].update(self._render_multi_tunnel_table())
        layout["middle"]["aggregate"].update(self._render_aggregate())
        layout["middle"]["pod_cards"].update(self._render_pod_cards())

        if self._show_logs:
            layout["logs"].update(self._render_multi_logs())

        layout["footer"].update(self._render_multi_footer())
        return layout

    # ── Multi: Header ─────────────────────────────────────────────────────

    def _render_multi_header(self) -> Panel:
        uptime = fmt_uptime(time.monotonic() - self._start)
        n = len(self._slots)
        parts = [
            Text(f" {Icons.TUNNEL} ExecTunnel  ", style="et.brand"),
            Text(f"{Icons.CLOCK} {uptime}", style="et.muted"),
            Text(
                f"  {Icons.BULLET} {n} tunnel{'s' if n != 1 else ''}",
                style="et.muted",
            ),
        ]
        return Panel(
            Align.left(Text.assemble(*parts)),
            style="et.border",
            padding=(0, 1),
        )

    # ── Multi: Summary panel ──────────────────────────────────────────────

    def _render_multi_summary(self) -> Panel:
        """Cluster-level health summary."""
        total = len(self._slots)
        healthy = sum(1 for s in self._slots if s.status == "healthy")
        running = sum(1 for s in self._slots if s.status in {"running", "healthy"})
        unhealthy = sum(1 for s in self._slots if s.status == "unhealthy")
        restarting = sum(1 for s in self._slots if s.status == "restarting")
        stopped = sum(1 for s in self._slots if s.status == "stopped")
        failed = sum(1 for s in self._slots if s.status == "failed")

        t = label_col()
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
            "○ Stopped",
            Text(str(stopped), style="et.conn.closed" if stopped else "et.muted"),
        )
        if failed:
            t.add_row(f"{Icons.CROSS} Failed", Text(str(failed), style="et.stat.bad"))

        return Panel(
            t,
            title=f"[et.brand]{Icons.PULSE} Cluster[/et.brand]",
            border_style="et.border",
            padding=(0, 1),
        )

    # ── Multi: Tunnel table ───────────────────────────────────────────────

    def _render_multi_tunnel_table(self) -> Panel:
        """Per-tunnel status overview table."""
        table = Table(
            box=box.SIMPLE_HEAD,
            header_style="et.label",
            border_style="et.border",
            expand=True,
            padding=(0, 1),
        )
        table.add_column("Tunnel", style="et.value", min_width=12)
        table.add_column("Status", width=13)
        table.add_column("SOCKS", style="et.value", width=7)
        table.add_column(f"{Icons.HEART} HP", width=4)
        table.add_column(f"{Icons.CLOCK} Up", width=11, justify="right")
        table.add_column(
            f"{Icons.BYTES_UP}", style="et.value", width=9, justify="right"
        )
        table.add_column(
            f"{Icons.BYTES_DOWN}", style="et.value", width=9, justify="right"
        )
        table.add_column("Conns", style="et.value", width=6, justify="right")

        for slot in self._slots:
            h = slot.snapshot()
            icon, style = _TUNNEL_STATUS_DISPLAY.get(
                slot.status, _TUNNEL_STATUS_DEFAULT
            )
            status_text = Text(f"{icon} {slot.status}", style=style)

            socks_ok_text = (
                Text(f"{Icons.CHECK}", style="et.stat.good")
                if (h is not None and h.socks_ok)
                else Text("—", style="et.muted")
            )

            uptime_str = fmt_uptime(h.uptime_secs) if h else "—"
            bytes_up = fmt_bytes(h.bytes_up_total) if h else "—"
            bytes_down = fmt_bytes(h.bytes_down_total) if h else "—"
            conns_str = str(h.tcp_open + h.tcp_pending) if h else "—"

            table.add_row(
                slot.name,
                status_text,
                str(slot.socks_port),
                socks_ok_text,
                uptime_str,
                bytes_up,
                bytes_down,
                conns_str,
            )

        if not self._slots:
            table.add_row("—", "—", "—", "—", "—", "—", "—", "—")

        return Panel(
            table,
            title=f"[et.brand]{Icons.TUNNEL} Tunnel Status[/et.brand]",
            border_style="et.border",
            padding=(0, 0),
        )

    # ── Multi: Aggregate metrics panel ────────────────────────────────────

    def _render_aggregate(self) -> Panel:
        """Aggregate metrics summed across all tunnel slots."""
        snapshots = [h for s in self._slots if (h := s.snapshot()) is not None]

        def _sum(attr: str) -> int:
            return sum(getattr(h, attr, 0) for h in snapshots)

        bytes_up = _sum("bytes_up_total")
        bytes_down = _sum("bytes_down_total")
        frames_sent = _sum("frames_sent")
        frames_recv = _sum("frames_recv")
        frames_drop = _sum("frames_dropped")
        tcp_open = _sum("tcp_open")
        tcp_total = _sum("tcp_total")
        tcp_done = _sum("tcp_completed")
        tcp_transport_err = _sum("tcp_errors")
        tcp_ack_fail = _sum("tcp_failed")
        udp_open = _sum("udp_open")
        ack_ok = _sum("ack_ok")
        ack_to = _sum("ack_timeout")
        ack_fail = _sum("ack_failed")
        s5_active = _sum("socks5_active")
        s5_acc = _sum("socks5_accepted")
        s5_rej = _sum("socks5_rejected")
        s5_hs_ok = _sum("socks5_handshakes_ok")
        s5_hs_err = _sum("socks5_handshakes_error")
        reconnects = _sum("session_reconnects")
        dns_q = _sum("dns_queries")
        dns_ok = _sum("dns_ok")
        dns_en = any(getattr(h, "dns_enabled", False) for h in snapshots)
        frame_errs = (
            _sum("frames_decode_errors")
            + _sum("frames_orphaned")
            + _sum("frames_noise")
        )
        queue_d = _sum("send_queue_depth")
        req_tasks = _sum("request_tasks")

        t = Table.grid(padding=(0, 2))
        for _ in range(4):
            t.add_column(style="et.label", min_width=16)
            t.add_column(style="et.value")

        t.add_row(
            f"{Icons.UP} Bytes Up",
            Text(fmt_bytes(bytes_up), style="et.value"),
            f"{Icons.DOWN} Bytes Dn",
            Text(fmt_bytes(bytes_down), style="et.value"),
            f"{Icons.FRAME} Frames",
            Text(
                f"{frames_sent}{Icons.ARROW_UP} {frames_recv}{Icons.ARROW_DOWN}",
                style="et.value",
            ),
            f"{Icons.WARN} Dropped",
            Text(str(frames_drop), style=err_style(frames_drop)),
        )
        t.add_row(
            f"{Icons.CONN} TCP Open",
            Text(str(tcp_open), style="et.conn.open" if tcp_open else "et.muted"),
            f"{Icons.CONN} TCP Total",
            Text(str(tcp_total), style="et.value"),
            f"{Icons.CONN} TCP Done",
            Text(str(tcp_done), style="et.value"),
            f"{Icons.WARN} TCP Err",
            Text(
                f"{tcp_transport_err}err {tcp_ack_fail}ack",
                style=err_style(tcp_transport_err + tcp_ack_fail),
            ),
        )
        t.add_row(
            f"{Icons.UDP} UDP Open",
            Text(str(udp_open), style="et.conn.open" if udp_open else "et.muted"),
            f"{Icons.CHECK} ACK",
            Text(
                f"{ack_ok}ok {ack_to}to {ack_fail}f",
                style="et.stat.good" if ack_fail == 0 else "et.stat.bad",
            ),
            f"{Icons.SOCKS} S5 Active",
            Text(str(s5_active), style="et.conn.open" if s5_active else "et.muted"),
            f"{Icons.SOCKS} S5 Acc/Rej",
            Text(f"{s5_acc}a/{s5_rej}r", style="et.value"),
        )
        t.add_row(
            f"{Icons.SOCKS} S5 HS",
            Text(
                f"{s5_hs_ok}ok/{s5_hs_err}err",
                style="et.stat.good" if s5_hs_err == 0 else "et.stat.bad",
            ),
            f"{Icons.RECONNECT} Reconnects",
            Text(str(reconnects), style="et.warn" if reconnects else "et.muted"),
            f"{Icons.DNS} DNS",
            Text(
                f"{dns_ok}/{dns_q}" if dns_en else "off",
                style="et.value" if dns_en else "et.muted",
            ),
            f"{Icons.WARN} Frame Errs",
            Text(str(frame_errs), style=err_style(frame_errs)),
        )
        t.add_row(
            f"{Icons.BOLT} Queue",
            Text(f"{queue_d}q/{req_tasks}t", style="et.value"),
            "",
            Text(""),
            "",
            Text(""),
            "",
            Text(""),
        )

        return Panel(
            t,
            title=f"[et.brand]{Icons.FRAME} Aggregate Metrics (all tunnels)[/et.brand]",
            border_style="et.border",
            padding=(0, 1),
        )

    # ── Multi: Per-tunnel cards ───────────────────────────────────────────

    def _render_pod_cards(self) -> Panel:
        """Per-tunnel detail cards rendered as a Columns layout."""
        if not self._slots:
            return Panel(
                Text("No tunnels configured.", style="et.muted"),
                title=f"[et.brand]{Icons.BOLT} Per-Tunnel Detail[/et.brand]",
                border_style="et.border",
            )

        cards: list[Panel] = [self._build_pod_card(slot) for slot in self._slots]
        return Panel(
            Columns(cards, equal=True, expand=True),
            title=f"[et.brand]{Icons.BOLT} Per-Tunnel Detail[/et.brand]",
            border_style="et.border",
            padding=(0, 0),
        )

    @staticmethod
    def _build_pod_card(slot: TunnelSlot) -> Panel:
        """Build a single per-tunnel detail card."""
        h = slot.snapshot()
        icon, style = _TUNNEL_STATUS_DISPLAY.get(slot.status, _TUNNEL_STATUS_DEFAULT)

        t = Table.grid(padding=(0, 1))
        t.add_column(style="et.label", min_width=14)
        t.add_column(style="et.value")

        t.add_row("Status", Text(f"{icon} {slot.status}", style=style))
        t.add_row(
            "SOCKS",
            Text(
                f":{slot.socks_port} {Icons.CHECK}"
                if (h is not None and h.socks_ok)
                else f":{slot.socks_port} —",
                style="et.stat.good" if (h is not None and h.socks_ok) else "et.muted",
            ),
        )

        if h is None:
            t.add_row("", Text("awaiting data…", style="et.muted"))
            return Panel(
                t,
                title=f"[et.brand]{slot.name}[/et.brand]",
                border_style="et.border",
                padding=(0, 1),
                width=42,
            )

        has_data = h.frames_sent > 0 or h.frames_recv > 0 or h.bytes_up_total > 0

        if h.bootstrap_ok:
            boot_str = f"{Icons.CHECK} OK"
            if h.bootstrap_duration is not None:
                boot_str += f" ({h.bootstrap_duration:.1f}s)"
            t.add_row(f"{Icons.LOCK} Bootstrap", Text(boot_str, style="et.stat.good"))
        else:
            t.add_row(f"{Icons.LOCK} Bootstrap", Text("pending", style="et.stat.warn"))

        if not has_data:
            t.add_row("", Text("awaiting traffic…", style="et.muted"))
            return Panel(
                t,
                title=f"[et.brand]{slot.name}[/et.brand]",
                border_style="et.border",
                padding=(0, 1),
                width=42,
            )

        t.add_row(
            f"{Icons.FRAME} Frames",
            Text(
                f"{h.frames_sent}{Icons.ARROW_UP} {h.frames_recv}{Icons.ARROW_DOWN}",
                style="et.value",
            ),
        )
        t.add_row(
            f"{Icons.UP} Traffic",
            Text(
                f"{fmt_bytes(h.bytes_up_total)}{Icons.ARROW_UP} "
                f"{fmt_bytes(h.bytes_down_total)}{Icons.ARROW_DOWN}",
                style="et.value",
            ),
        )

        if h.frames_dropped:
            t.add_row(
                f"{Icons.WARN} Dropped",
                Text(str(h.frames_dropped), style=err_style(h.frames_dropped)),
            )

        frame_errs = h.frames_decode_errors + h.frames_orphaned + h.frames_noise
        if frame_errs:
            t.add_row(
                f"{Icons.WARN} Frame Errs",
                Text(str(frame_errs), style="et.stat.bad"),
            )

        t.add_row(
            f"{Icons.CONN} TCP",
            Text(
                f"{h.tcp_open}open {h.tcp_pending}pend {h.tcp_total}tot",
                style="et.conn.open" if h.tcp_open else "et.value",
            ),
        )

        tcp_transport_err = h.tcp_errors
        tcp_ack_fail = h.tcp_failed
        if h.tcp_completed or tcp_transport_err or tcp_ack_fail:
            t.add_row(
                "  done/err",
                Text(
                    f"{h.tcp_completed}done {tcp_transport_err}err {tcp_ack_fail}ack",
                    style="et.stat.good"
                    if (tcp_transport_err + tcp_ack_fail) == 0
                    else "et.stat.bad",
                ),
            )

        if h.udp_open or h.udp_total or h.udp_flows_opened:
            t.add_row(
                f"{Icons.UDP} UDP",
                Text(
                    f"{h.udp_open}open {h.udp_total}tot",
                    style="et.conn.open" if h.udp_open else "et.value",
                ),
            )
            if h.udp_flows_opened:
                t.add_row(
                    "  flows",
                    Text(
                        f"{h.udp_flows_opened}{Icons.ARROW_UP} "
                        f"{h.udp_flows_closed}{Icons.ARROW_DOWN}"
                    ),
                )

        ack_total = h.ack_ok + h.ack_timeout + h.ack_failed
        if ack_total:
            t.add_row(
                f"{Icons.CHECK} ACK",
                Text(
                    f"{h.ack_ok}ok {h.ack_timeout}to {h.ack_failed}f",
                    style="et.stat.good" if h.ack_failed == 0 else "et.stat.bad",
                ),
            )

        if h.socks5_accepted or h.socks5_active:
            t.add_row(
                f"{Icons.SOCKS} SOCKS5",
                Text(
                    f"{h.socks5_active}act {h.socks5_accepted}acc {h.socks5_rejected}rej",
                    style="et.conn.open" if h.socks5_active else "et.value",
                ),
            )
            hs_err = h.socks5_handshakes_error
            if h.socks5_handshakes_ok or hs_err:
                t.add_row(
                    "  handshake",
                    Text(
                        f"{h.socks5_handshakes_ok}ok {hs_err}err",
                        style="et.stat.good" if hs_err == 0 else "et.stat.bad",
                    ),
                )

        if h.dns_enabled:
            t.add_row(
                f"{Icons.DNS} DNS",
                Text(
                    f"{h.dns_ok}/{h.dns_queries} ok ({h.dns_dropped}drop)",
                    style="et.value",
                ),
            )

        if h.session_reconnects:
            parts = str(h.session_reconnects)
            if h.reconnect_delay_avg is not None:
                parts += f" (avg {h.reconnect_delay_avg:.1f}s)"
            t.add_row(f"{Icons.RECONNECT} Reconnect", Text(parts, style="et.warn"))

        total_cleanup = h.cleanup_tcp + h.cleanup_pending + h.cleanup_udp
        if total_cleanup:
            t.add_row(
                f"{Icons.WARN} Cleanup",
                Text(
                    f"tcp={h.cleanup_tcp} pend={h.cleanup_pending} udp={h.cleanup_udp}",
                    style="et.stat.warn",
                ),
            )

        if h.send_queue_depth or h.request_tasks:
            t.add_row(
                f"{Icons.BOLT} Queue",
                Text(f"{h.send_queue_depth}q {h.request_tasks}tasks", style="et.value"),
            )

        return Panel(
            t,
            title=f"[et.brand]{slot.name}[/et.brand]",
            border_style="et.border",
            padding=(0, 1),
            width=42,
        )

    # ── Multi: Log tail panel ─────────────────────────────────────────────

    def _render_multi_logs(self) -> Panel:
        """Interleaved log tail from all slots that have a log buffer."""
        lines: list[Text] = []
        max_lines_per_slot = max(5, 40 // max(len(self._slots), 1))

        for slot in self._slots:
            buf = slot.log_buffer
            if buf is None:
                continue
            try:
                entries = buf.entries()
            except Exception:  # noqa: BLE001, S112
                continue

            recent = entries[-max_lines_per_slot:]
            if not recent:
                continue

            lines.append(Text(f"── {slot.name} ──", style="et.brand"))
            for e in recent:
                if hasattr(e, "level"):
                    lvl_style = LOG_LEVEL_STYLES.get(
                        getattr(e, "level", ""), "et.muted"
                    )
                    line = Text.assemble(
                        Text(f"{getattr(e, 'ts', '')} ", style="et.muted"),
                        Text(f"{getattr(e, 'level', ''):<7} ", style=lvl_style),
                        Text(
                            getattr(e, "message", str(e)),
                            overflow="ellipsis",
                            no_wrap=True,
                        ),
                    )
                else:
                    raw = str(e)
                    style = "et.muted"
                    upper = raw[:40].upper()
                    for lvl, lvl_style in LOG_LEVEL_STYLES.items():
                        if lvl in upper:
                            style = lvl_style
                            break
                    line = Text(raw, style=style, overflow="ellipsis", no_wrap=True)
                lines.append(line)

        if not lines:
            lines.append(Text("No log output yet.", style="et.muted"))

        return Panel(
            Group(*lines),
            title=f"[et.brand]{Icons.CLOCK} Log Tail[/et.brand]",
            border_style="et.border",
            padding=(0, 1),
        )

    # ── Multi: Footer ─────────────────────────────────────────────────────

    def _render_multi_footer(self) -> Panel:
        active_proxies = "  ".join(
            f"socks5://{s.socks_host}:{s.socks_port}"
            for s in self._slots
            if s.status in {"running", "healthy"}
        )
        footer = Text.assemble(
            Text("  Active: ", style="et.muted"),
            Text(active_proxies or "none", style="et.value"),
            Text("  │  Ctrl+C to stop all tunnels", style="et.muted"),
        )
        return Panel(footer, style="et.border", padding=(0, 1))
