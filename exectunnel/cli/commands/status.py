"""exectunnel status — inspect a running session via its IPC socket."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .._theme import THEME, Icons

__all__ = ["status"]

console = Console(theme=THEME, highlight=False)

_DEFAULT_SOCKET = "/tmp/exectunnel.sock"


def status(
    socket_path: Annotated[
        str,
        typer.Option(
            "--socket", "-s",
            help="Path to the session IPC socket.",
            envvar="EXECTUNNEL_SOCKET",
        ),
    ] = _DEFAULT_SOCKET,
    watch: Annotated[
        bool,
        typer.Option("--watch", "-w", help="Refresh every second."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output raw JSON."),
    ] = False,
) -> None:
    """Display health and statistics for a running tunnel session."""
    try:
        exit_code = asyncio.run(
            _status_async(
                socket_path=socket_path,
                watch=watch,
                json_output=json_output,
            )
        )
    except KeyboardInterrupt:
        exit_code = 0
    raise typer.Exit(exit_code)


async def _status_async(
    *,
    socket_path: str,
    watch: bool,
    json_output: bool,
) -> int:
    if not Path(socket_path).exists():
        console.print(
            f"[et.error]{Icons.CROSS} No session socket found at "
            f"{socket_path!r}[/et.error]"
        )
        console.print(
            "[et.muted]  Is ExecTunnel running? "
            "Start with: exectunnel connect …[/et.muted]"
        )
        return 1

    try:
        data = await _query_socket(socket_path)
    except Exception as exc:
        console.print(
            f"[et.error]{Icons.CROSS} Failed to query session: {exc}[/et.error]"
        )
        return 1

    if json_output:
        console.print_json(json.dumps(data))
        return 0

    console.print(_build_status_panel(data))

    if watch:
        from rich.live import Live
        with Live(
            _build_status_panel(data),
            console=console,
            refresh_per_second=1,
            screen=False,
        ) as live:
            try:
                while True:
                    await asyncio.sleep(1.0)
                    try:
                        data = await _query_socket(socket_path)
                        live.update(_build_status_panel(data))
                    except Exception:
                        break
            except asyncio.CancelledError:
                pass

    return 0


async def _query_socket(socket_path: str) -> dict:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write(b'{"cmd":"status"}\n')
        await writer.drain()
        async with asyncio.timeout(5.0):
            raw = await reader.readline()
        return json.loads(raw)
    finally:
        writer.close()
        await writer.wait_closed()


def _build_status_panel(data: dict) -> Panel:
    t = Table(
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="et.label",
        border_style="et.border",
        expand=True,
    )
    t.add_column("Metric", style="et.label", ratio=2)
    t.add_column("Value",  style="et.value", ratio=3)
    t.add_column("Metric", style="et.label", ratio=2)
    t.add_column("Value",  style="et.value", ratio=3)

    rows = [
        ("Connected",   data.get("connected", "—"),
         "Uptime",      data.get("uptime", "—")),
        ("SOCKS5",      data.get("socks_addr", "—"),
         "Reconnects",  data.get("reconnect_count", 0)),
        ("TCP open",    data.get("tcp_open", 0),
         "TCP total",   data.get("tcp_total", 0)),
        ("UDP open",    data.get("udp_open", 0),
         "Frames sent", data.get("frames_sent", 0)),
        ("ACK ok",      data.get("ack_ok", 0),
         "ACK timeout", data.get("ack_timeout", 0)),
        ("DNS queries", data.get("dns_queries", 0),
         "DNS ok",      data.get("dns_ok", 0)),
    ]
    for k1, v1, k2, v2 in rows:
        t.add_row(k1, str(v1), k2, str(v2))

    return Panel(
        t,
        title=f"[et.brand]{Icons.PULSE} ExecTunnel Session Status[/et.brand]",
        border_style="et.border",
    )
