"""Live Rich dashboard — renders tunnel health as a full-screen TUI."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import TYPE_CHECKING

from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from ._health import HealthMonitor, TunnelHealth
from ._theme import THEME, Icons

if TYPE_CHECKING:
    pass

__all__ = ["Dashboard"]

_REFRESH_HZ = 4  # renders per second


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n} PB"


def _fmt_uptime(secs: float) -> str:
    td = timedelta(seconds=int(secs))
    h, rem = divmod(td.seconds, 3600)
    m, s   = divmod(rem, 60)
    days   = td.days
    if days:
        return f"{days}d {h:02d}:{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def _status_dot(ok: bool) -> Text:
    if ok:
        return Text("● ", style="et.ok") + Text("Connected", style="et.ok")
    return Text("● ", style="et.error") + Text("Disconnected", style="et.error")


def _ack_rate(h: TunnelHealth) -> str:
    total = h.ack_ok + h.ack_failed
    if total == 0:
        return "—"
    rate = h.ack_ok / total * 100
    return f"{rate:.1f}%"


class Dashboard:
    """Full-screen live dashboard for an active tunnel session.

    Args:
        monitor:    ``HealthMonitor`` instance to poll.
        console:    Rich console (shared with CLI).
        ws_url:     WebSocket URL displayed in the header.
        kubectl_ctx: kubectl context name, or ``None`` for raw WS mode.
    """

    __slots__ = (
        "_monitor",
        "_console",
        "_ws_url",
        "_kubectl_ctx",
        "_live",
        "_running",
    )

    def __init__(
        self,
        monitor: HealthMonitor,
        console: Console,
        ws_url: str,
        kubectl_ctx: str | None = None,
    ) -> None:
        self._monitor    = monitor
        self._console    = console
        self._ws_url     = ws_url
        self._kubectl_ctx = kubectl_ctx
        self._live: Live | None = None
        self._running    = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def run_until_cancelled(self) -> None:
        """Render the dashboard until the task is cancelled."""
        self._running = True
        with Live(
            self._render(),
            console=self._console,
            refresh_per_second=_REFRESH_HZ,
            screen=True,
        ) as live:
            self._live = live
            try:
                while self._running:
                    live.update(self._render())
                    await asyncio.sleep(1.0 / _REFRESH_HZ)
            except asyncio.CancelledError:
                pass
            finally:
                self._running = False

    def stop(self) -> None:
        self._running = False

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _render(self) -> Layout:
        h = self._monitor.snapshot()
        layout = Layout()

        layout.split_column(
            Layout(name="header",  size=4),
            Layout(name="body",    ratio=1),
            Layout(name="footer",  size=3),
        )
        layout["body"].split_row(
            Layout(name="left",   ratio=2),
            Layout(name="right",  ratio=3),
        )
        layout["left"].split_column(
            Layout(name="tunnel",  ratio=2),
            Layout(name="pod",     ratio=2),
            Layout(name="dns",     ratio=1),
        )
        layout["right"].split_column(
            Layout(name="traffic", ratio=2),
            Layout(name="conns",   ratio=3),
        )

        layout["header"].update(self._render_header(h))
        layout["tunnel"].update(self._render_tunnel(h))
        layout["pod"].update(self._render_pod(h))
        layout["dns"].update(self._render_dns(h))
        layout["traffic"].update(self._render_traffic(h))
        layout["conns"].update(self._render_connections(h))
        layout["footer"].update(self._render_footer(h))

        return layout

    # ── Header ────────────────────────────────────────────────────────────────

    def _render_header(self, h: TunnelHealth) -> Panel:
        mode = (
            f"[et.muted]kubectl ctx:[/et.muted] [et.value]{self._kubectl_ctx}[/et.value]  "
            if self._kubectl_ctx
            else "[et.muted]mode:[/et.muted] [et.value]raw WebSocket[/et.value]  "
        )
        url_short = (
            self._ws_url[:72] + "…"
            if len(self._ws_url) > 75
            else self._ws_url
        )
        status = _status_dot(h.connected)
        uptime = Text(
            f"  {Icons.CLOCK} {_fmt_uptime(h.uptime_secs)}",
            style="et.muted",
        )
        reconnects = (
            Text(
                f"  {Icons.RECONNECT} {h.reconnect_count}",
                style="et.warn" if h.reconnect_count else "et.muted",
            )
        )

        title_row = Text.assemble(
            Text(f" {Icons.TUNNEL} ExecTunnel  ", style="et.brand"),
            status,
            uptime,
            reconnects,
            Text(f"  {mode}", style=""),
            Text(f"[et.muted]{Icons.GLOBE} {url_short}[/et.muted]"),
        )
        return Panel(
            Align.left(title_row),
            style="et.border",
            padding=(0, 1),
        )

    # ── Tunnel panel ──────────────────────────────────────────────────────────

    def _render_tunnel(self, h: TunnelHealth) -> Panel:
        t = Table.grid(padding=(0, 2))
        t.add_column(style="et.label",  min_width=18)
        t.add_column(style="et.value",  min_width=16)
        t.add_column(style="et.label",  min_width=18)
        t.add_column(style="et.value")

        socks_val = Text(
            f"{h.socks_host}:{h.socks_port}",
            style="et.stat.good" if h.socks_ok else "et.stat.bad",
        )
        ack_style = (
            "et.stat.good" if h.ack_failed == 0
            else "et.stat.warn" if h.ack_failed < 5
            else "et.stat.bad"
        )

        t.add_row(
            f"{Icons.SOCKS} SOCKS5",       socks_val,
            f"{Icons.LOCK} Bootstrap",
            Text("OK", style="et.stat.good") if h.bootstrap_ok
            else Text("—", style="et.stat.idle"),
        )
        t.add_row(
            f"{Icons.BOLT} Frames sent",   str(h.frames_sent),
            f"{Icons.BOLT} Frames recv",   str(h.frames_recv),
        )
        t.add_row(
            f"{Icons.WARN} Dropped",
            Text(str(h.frames_dropped), style="et.stat.bad" if h.frames_dropped else "et.muted"),
            f"{Icons.HEART} ACK rate",
            Text(_ack_rate(h), style=ack_style),
        )
        t.add_row(
            f"{Icons.ARROW_UP} Queue",
            f"{h.send_queue_depth}/{h.send_queue_cap}" if h.send_queue_cap else "—",
            f"{Icons.RECONNECT} Reconnects",
            Text(str(h.reconnect_count), style="et.warn" if h.reconnect_count else "et.muted"),
        )

        return Panel(
            t,
            title=f"[et.brand]{Icons.TUNNEL} Tunnel[/et.brand]",
            border_style="et.border",
            padding=(0, 1),
        )

    # ── Pod panel ─────────────────────────────────────────────────────────────

    def _render_pod(self, h: TunnelHealth) -> Panel:
        if not h.pod_name:
            return Panel(
                Align.center(
                    Text("No pod metadata (raw WS mode)", style="et.muted")
                ),
                title=f"[et.brand]{Icons.POD} Pod[/et.brand]",
                border_style="et.border",
                padding=(0, 1),
            )

        phase_style = {
            "Running":   "et.stat.good",
            "Pending":   "et.stat.warn",
            "Failed":    "et.stat.bad",
            "Succeeded": "et.muted",
            "Unknown":   "et.stat.warn",
        }.get(h.pod_phase, "et.muted")

        t = Table.grid(padding=(0, 2))
        t.add_column(style="et.label", min_width=14)
        t.add_column(style="et.value", min_width=20)
        t.add_column(style="et.label", min_width=14)
        t.add_column(style="et.value")

        t.add_row(
            "Name",      h.pod_name,
            "Namespace", h.pod_namespace,
        )
        t.add_row(
            "Node",      h.pod_node or "—",
            "IP",        h.pod_ip or "—",
        )
        t.add_row(
            "Phase",
            Text(h.pod_phase or "—", style=phase_style),
            "Conditions", "",
        )

        # Conditions row
        cond_parts = []
        for cond in h.pod_conditions:
            ctype  = cond.get("type", "")
            status = cond.get("status", "")
            style  = "et.stat.good" if status == "True" else "et.stat.bad"
            cond_parts.append(Text(f"{ctype}:{status} ", style=style))

        if cond_parts:
            cond_text = Text.assemble(*cond_parts)
            t.add_row("", "", "", cond_text)

        return Panel(
            t,
            title=f"[et.brand]{Icons.POD} Pod[/et.brand]",
            border_style="et.border",
            padding=(0, 1),
        )

    # ── DNS panel ─────────────────────────────────────────────────────────────

    def _render_dns(self, h: TunnelHealth) -> Panel:
        if not h.dns_enabled:
            body = Text("DNS forwarding disabled", style="et.muted")
        else:
            ok_rate = (
                f"{h.dns_ok / h.dns_queries * 100:.1f}%"
                if h.dns_queries
                else "—"
            )
            body = Text.assemble(
                Text(f"{Icons.DNS} Queries: ", style="et.label"),
                Text(str(h.dns_queries), style="et.value"),
                Text("  OK: ", style="et.label"),
                Text(ok_rate, style="et.stat.good"),
                Text("  Dropped: ", style="et.label"),
                Text(
                    str(h.dns_dropped),
                    style="et.stat.bad" if h.dns_dropped else "et.muted",
                ),
            )

        return Panel(
            body,
            title=f"[et.brand]{Icons.DNS} DNS[/et.brand]",
            border_style="et.border",
            padding=(0, 1),
        )

    # ── Traffic panel ─────────────────────────────────────────────────────────

    def _render_traffic(self, h: TunnelHealth) -> Panel:
        t = Table.grid(padding=(0, 2))
        t.add_column(style="et.label", min_width=16)
        t.add_column(style="et.value", min_width=12)
        t.add_column(style="et.label", min_width=16)
        t.add_column(style="et.value")

        t.add_row(
            f"{Icons.BYTES_UP} TCP open",
            Text(str(h.tcp_open), style="et.conn.open"),
            f"{Icons.BYTES_DOWN} TCP pending",
            Text(str(h.tcp_pending), style="et.conn.pend"),
        )
        t.add_row(
            "TCP total",    str(h.tcp_total),
            "TCP failed",
            Text(str(h.tcp_failed), style="et.stat.bad" if h.tcp_failed else "et.muted"),
        )
        t.add_row(
            f"{Icons.GLOBE} UDP open",
            Text(str(h.udp_open), style="et.conn.open"),
            "UDP total",    str(h.udp_total),
        )
        t.add_row(
            f"{Icons.BYTES_UP} ACK ok",
            Text(str(h.ack_ok), style="et.stat.good"),
            f"{Icons.WARN} ACK timeout",
            Text(str(h.ack_timeout), style="et.stat.bad" if h.ack_timeout else "et.muted"),
        )

        return Panel(
            t,
            title=f"[et.brand]{Icons.BOLT} Traffic[/et.brand]",
            border_style="et.border",
            padding=(0, 1),
        )

    # ── Connections panel ─────────────────────────────────────────────────────

    def _render_connections(self, h: TunnelHealth) -> Panel:
        table = Table(
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style="et.label",
            border_style="et.border",
            expand=True,
            padding=(0, 1),
        )
        table.add_column("ID",       style="et.muted",  width=10)
        table.add_column("Host",     style="et.value",  ratio=3)
        table.add_column("Port",     style="et.value",  width=6)
        table.add_column("State",    width=9)
        table.add_column(f"{Icons.BYTES_UP}",  style="et.value", width=9, justify="right")
        table.add_column(f"{Icons.BYTES_DOWN}", style="et.value", width=9, justify="right")
        table.add_column("Drops",    style="et.stat.bad", width=6, justify="right")

        state_styles = {
            "open":    "et.conn.open",
            "pending": "et.conn.pend",
            "closing": "et.warn",
            "closed":  "et.conn.closed",
        }

        for conn in h.recent_conns[-18:]:
            style = state_styles.get(conn.state, "et.muted")
            table.add_row(
                conn.conn_id[:8],
                conn.host,
                str(conn.port),
                Text(conn.state, style=style),
                _fmt_bytes(conn.bytes_up),
                _fmt_bytes(conn.bytes_down),
                str(conn.drop_count) if conn.drop_count else "—",
            )

        if not h.recent_conns:
            table.add_row(
                "—", "No connections yet", "—", "—", "—", "—", "—"
            )

        return Panel(
            table,
            title=f"[et.brand]{Icons.GLOBE} Active Connections[/et.brand]",
            border_style="et.border",
            padding=(0, 0),
        )

    # ── Footer ────────────────────────────────────────────────────────────────

    def _render_footer(self, h: TunnelHealth) -> Panel:
        keys = Text.assemble(
            Text(" [q] ", style="et.highlight"),
            Text("quit  ", style="et.muted"),
            Text("[r] ", style="et.highlight"),
            Text("reconnect  ", style="et.muted"),
            Text("[c] ", style="et.highlight"),
            Text("copy proxy env  ", style="et.muted"),
            Text("[?] ", style="et.highlight"),
            Text("help  ", style="et.muted"),
        )
        proxy_hint = Text(
            f"  export https_proxy=socks5://{h.socks_host}:{h.socks_port}",
            style="et.muted",
        )
        return Panel(
            Text.assemble(keys, proxy_hint),
            style="et.border",
            padding=(0, 1),
        )
