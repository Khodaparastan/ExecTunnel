"""Live Rich dashboard — renders tunnel health as a full-screen TUI."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Final

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..metrics import HealthMonitor, TunnelHealth
from ..ui import Icons

__all__ = ["TunnelDashboard"]

try:
    from exectunnel.observability.logging import LogRingBuffer
except ImportError:  # pragma: no cover
    LogRingBuffer = None  # type: ignore[assignment,misc]

_REFRESH_HZ: int = 4
_FRAME_INTERVAL: float = 1.0 / _REFRESH_HZ
_MAX_VISIBLE_CONNS: int = 18
_URL_MAX_DISPLAY: int = 72
_BYTES_UNIT_BASE: float = 1024.0
_ACK_WARN_THRESHOLD: int = 5
_LOG_TAIL_LIMIT: int = 50

_ACTIVE_CONN_STATES: frozenset[str] = frozenset({"open", "pending"})

_CONN_STATE_STYLES: dict[str, str] = {
    "open": "et.conn.open",
    "pending": "et.conn.pend",
    "closing": "et.warn",
    "closed": "et.conn.closed",
}

_POD_PHASE_STYLES: dict[str, str] = {
    "Running": "et.stat.good",
    "Pending": "et.stat.warn",
    "Failed": "et.stat.bad",
}

# Hoisted to module level — avoids re-creating on every render frame.
_LOG_LEVEL_STYLES: Final[dict[str, str]] = {
    "DEBUG": "dim cyan",
    "INFO": "green",
    "WARNING": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold red reverse",
}


# ── Pure formatting helpers ───────────────────────────────────────────────────


def _fmt_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < _BYTES_UNIT_BASE:
            return f"{value:.1f} {unit}"
        value /= _BYTES_UNIT_BASE
    return f"{value:.1f} PB"


def _fmt_uptime(secs: float) -> str:
    td = timedelta(seconds=int(secs))
    h, rem = divmod(td.seconds, 3600)
    m, s = divmod(rem, 60)
    if td.days:
        return f"{td.days}d {h:02d}:{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def _status_dot(ok: bool) -> Text:
    style = "et.ok" if ok else "et.error"
    label = "Connected" if ok else "Disconnected"
    return Text.assemble(Text("● ", style=style), Text(label, style=style))


def _ack_rate(h: TunnelHealth) -> str:
    total = h.ack_ok + h.ack_failed
    if total == 0:
        return "—"
    return f"{h.ack_ok / total * 100:.1f}%"


def _truncate_url(url: str, max_len: int = _URL_MAX_DISPLAY) -> str:
    if len(url) > max_len + 3:
        return url[:max_len] + "…"
    return url


def _label_col() -> Table:
    """Return a two-column grid configured for key/value rows."""
    t = Table.grid(padding=(0, 1))
    t.add_column(style="et.label", min_width=14)
    t.add_column(style="et.value")
    return t


def _err_style(count: int) -> str:
    return "et.stat.bad" if count else "et.muted"


# ── TunnelDashboard ───────────────────────────────────────────────────────────


class TunnelDashboard:
    """Full-screen live dashboard for an active tunnel session."""

    __slots__ = ("_monitor", "_console", "_ws_url", "_stop_event", "_log_buffer")

    def __init__(
        self,
        monitor: HealthMonitor,
        console: Console,
        ws_url: str,
        log_buffer: LogRingBuffer | None = None,
    ) -> None:
        self._monitor = monitor
        self._console = console
        self._ws_url = ws_url
        self._stop_event = asyncio.Event()
        self._log_buffer = log_buffer

    async def run_until_cancelled(self) -> None:
        """Drive the live display until cancelled or :meth:`stop` is called."""
        loop = asyncio.get_running_loop()
        with Live(
            self._render(),
            console=self._console,
            refresh_per_second=_REFRESH_HZ,
            screen=True,
        ) as live:
            try:
                deadline = loop.time() + _FRAME_INTERVAL
                while not self._stop_event.is_set():
                    live.update(self._render())
                    sleep_for = max(0.0, deadline - loop.time())
                    await asyncio.sleep(sleep_for)
                    deadline += _FRAME_INTERVAL
            except asyncio.CancelledError:
                pass

    def stop(self) -> None:
        """Request a clean exit from :meth:`run_until_cancelled`."""
        self._stop_event.set()

    # ── Layout ────────────────────────────────────────────────────────────

    def _render(self) -> Layout:
        h = self._monitor.snapshot()
        layout = Layout()

        has_logs = self._log_buffer is not None

        if has_logs:
            layout.split_column(
                Layout(name="header", size=3),
                Layout(name="stats", size=16),
                Layout(name="lower"),
                Layout(name="footer", size=3),
            )
            layout["lower"].split_row(
                Layout(name="conns"),
                Layout(name="logs"),
            )
            layout["logs"].update(self._render_logs())
        else:
            layout.split_column(
                Layout(name="header", size=3),
                Layout(name="stats", size=16),
                Layout(name="conns"),
                Layout(name="footer", size=3),
            )

        layout["stats"].split_row(
            Layout(name="tunnel"),
            Layout(name="traffic"),
            Layout(name="dns_pod"),
        )

        layout["header"].update(self._render_header(h))
        layout["tunnel"].update(self._render_tunnel(h))
        layout["traffic"].update(self._render_traffic(h))
        layout["dns_pod"].update(self._render_dns_pod(h))
        layout["conns"].update(self._render_connections(h))
        layout["footer"].update(self._render_footer(h))

        return layout

    # ── Panels ────────────────────────────────────────────────────────────

    def _render_header(self, h: TunnelHealth) -> Panel:
        url_short = _truncate_url(self._ws_url)
        parts: list[Text] = [
            Text(f" {Icons.TUNNEL} ExecTunnel  ", style="et.brand"),
            _status_dot(h.connected),
            Text(f"  {Icons.CLOCK} {_fmt_uptime(h.uptime_secs)}", style="et.muted"),
        ]
        if h.reconnect_count:
            parts.append(
                Text(f"  {Icons.RECONNECT} {h.reconnect_count}", style="et.warn")
            )
        parts.append(Text(f"  {Icons.GLOBE} {url_short}", style="et.muted"))

        return Panel(
            Align.left(Text.assemble(*parts)),
            style="et.border",
            padding=(0, 1),
        )

    @staticmethod
    def _render_tunnel(h: TunnelHealth) -> Panel:
        t = _label_col()

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
                "  boot time",
                Text(f"{h.bootstrap_duration:.1f}s", style="et.muted"),
            )
        t.add_row(f"{Icons.BOLT} Sent", str(h.frames_sent))
        t.add_row(f"{Icons.BOLT} Recv", str(h.frames_recv))
        t.add_row(
            f"{Icons.WARN} Dropped",
            Text(str(h.frames_dropped), style=_err_style(h.frames_dropped)),
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
        hs_err_style = _err_style(hs_err)

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
            Text(str(hs_err), style=hs_err_style),
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

    @staticmethod
    def _render_traffic(h: TunnelHealth) -> Panel:
        t = _label_col()

        t.add_row(
            f"{Icons.BYTES_UP} TCP open", Text(str(h.tcp_open), style="et.conn.open")
        )
        t.add_row(
            f"{Icons.BYTES_DOWN} TCP pending",
            Text(str(h.tcp_pending), style="et.conn.pend"),
        )
        t.add_row("TCP total", str(h.tcp_total))
        t.add_row("TCP failed", Text(str(h.tcp_failed), style=_err_style(h.tcp_failed)))
        if h.tcp_completed:
            t.add_row("TCP completed", str(h.tcp_completed))
        if h.tcp_errors:
            t.add_row("TCP errors", Text(str(h.tcp_errors), style="et.stat.bad"))
        t.add_row(
            f"{Icons.BYTES_UP} Bytes up",
            Text(_fmt_bytes(h.bytes_up_total), style="et.value"),
        )
        t.add_row(
            f"{Icons.BYTES_DOWN} Bytes down",
            Text(_fmt_bytes(h.bytes_down_total), style="et.value"),
        )

        if h.ack_failed == 0:
            ack_style = "et.stat.good"
        elif h.ack_failed < _ACK_WARN_THRESHOLD:
            ack_style = "et.stat.warn"
        else:
            ack_style = "et.stat.bad"

        t.add_row(f"{Icons.HEART} ACK rate", Text(_ack_rate(h), style=ack_style))
        if h.ack_timeout:
            t.add_row(
                "  ACK timeout",
                Text(str(h.ack_timeout), style="et.stat.warn"),
            )

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

    @staticmethod
    def _render_dns_pod(h: TunnelHealth) -> Panel:
        t = _label_col()

        if h.dns_enabled:
            ok_rate = f"{h.dns_ok / h.dns_queries * 100:.1f}%" if h.dns_queries else "—"
            t.add_row(f"{Icons.DNS} Queries", str(h.dns_queries))
            t.add_row("DNS OK", Text(ok_rate, style="et.stat.good"))
            t.add_row(
                "DNS drops", Text(str(h.dns_dropped), style=_err_style(h.dns_dropped))
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

    @staticmethod
    def _render_connections(h: TunnelHealth) -> Panel:
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
                _fmt_bytes(conn.bytes_up),
                _fmt_bytes(conn.bytes_down),
                str(conn.drop_count) if conn.drop_count else "—",
            )

        if not visible:
            table.add_row("—", "No connections yet", "—", "—", "—", "—", "—")

        return Panel(
            table,
            title=f"[et.brand]{Icons.GLOBE} Active Connections[/et.brand]",
            border_style="et.border",
            padding=(0, 0),
        )

    def _render_logs(self) -> Panel:
        """Render a scrolling log tail panel."""
        assert self._log_buffer is not None
        entries = self._log_buffer.entries()
        tail = entries[-_LOG_TAIL_LIMIT:] if len(entries) > _LOG_TAIL_LIMIT else entries

        lines: list[Text] = []
        for e in tail:
            lvl_style = _LOG_LEVEL_STYLES.get(e.level, "et.muted")
            line = Text.assemble(
                Text(f"{e.ts} ", style="et.muted"),
                Text(f"{e.level:<7} ", style=lvl_style),
                Text(f"{e.logger} ", style="dim"),
                Text(e.message),
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

    @staticmethod
    def _render_footer(h: TunnelHealth) -> Panel:
        proxy_env = f"socks5://{h.socks_host}:{h.socks_port}"
        footer = Text.assemble(
            Text("  export https_proxy=", style="et.muted"),
            Text(proxy_env, style="et.value"),
            Text("  │  Ctrl+C to stop", style="et.muted"),
        )
        return Panel(footer, style="et.border", padding=(0, 1))
