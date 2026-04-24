"""exectunnel status — inspect a running session via its IPC socket.

Experimental: the IPC server is not implemented in the current session layer.
This command is therefore informational only and will fail until IPC support
is added.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Annotated, Any

import typer
from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from ..ui import THEME, Icons

__all__ = ["status"]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_XDG_RUNTIME = os.environ.get("XDG_RUNTIME_DIR", "/tmp")  # noqa: S108
_DEFAULT_SOCKET = str(Path(_XDG_RUNTIME) / "exectunnel.sock")
_QUERY_TIMEOUT_SECS = 5.0
_CONNECT_TIMEOUT_SECS = 3.0
_WATCH_INTERVAL_SECS = 1.0
_MAX_CONSECUTIVE_FAILURES = 3

_STATUS_KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("Connected", "connected", "Uptime", "uptime"),
    ("SOCKS5", "socks_addr", "Reconnects", "reconnect_count"),
    ("TCP open", "tcp_open", "TCP total", "tcp_total"),
    ("UDP open", "udp_open", "Frames sent", "frames_sent"),
    ("ACK ok", "ack_ok", "ACK timeout", "ack_timeout"),
    ("DNS queries", "dns_queries", "DNS ok", "dns_ok"),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_console() -> Console:
    return Console(theme=THEME, highlight=False)


async def _query_socket(socket_path: str) -> dict[str, Any]:
    """Send a status query over the Unix domain socket and return parsed JSON."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(socket_path),
        timeout=_CONNECT_TIMEOUT_SECS,
    )
    try:
        writer.write(b'{"cmd":"status"}\n')
        await writer.drain()
        raw = await asyncio.wait_for(
            reader.readline(),
            timeout=_QUERY_TIMEOUT_SECS,
        )
        if not raw:
            raise ConnectionError("Empty response from session socket")
        return json.loads(raw)  # type: ignore[no-any-return]
    finally:
        writer.close()
        await writer.wait_closed()


def _build_status_panel(data: dict[str, Any]) -> Panel:
    """Render session metrics into a Rich panel."""
    t = Table(
        box=box.SIMPLE_HEAD,
        header_style="et.label",
        border_style="et.border",
        expand=True,
    )
    t.add_column("Metric", style="et.label", ratio=2)
    t.add_column("Value", style="et.value", ratio=3)
    t.add_column("Metric", style="et.label", ratio=2)
    t.add_column("Value", style="et.value", ratio=3)

    for label1, key1, label2, key2 in _STATUS_KEYS:
        t.add_row(
            label1,
            str(data.get(key1, "—")),
            label2,
            str(data.get(key2, "—")),
        )

    return Panel(
        t,
        title=f"[et.brand]{Icons.PULSE} ExecTunnel Session Status[/et.brand]",
        border_style="et.border",
    )


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def status(
    socket_path: Annotated[
        str,
        typer.Option(
            "--socket",
            "-s",
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
    """Display health and statistics for a running tunnel session.

    Note: session IPC support is not implemented yet; this command is
    currently experimental and will usually return an error.
    """
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


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------


async def _status_async(
    *,
    socket_path: str,
    watch: bool,
    json_output: bool,
) -> int:
    con = _make_console()

    if not Path(socket_path).exists():
        con.print(
            f"[et.error]{Icons.CROSS} No session socket found at "
            f"{socket_path!r}[/et.error]"
        )
        con.print(
            "[et.muted]  Is ExecTunnel running? "
            "Start with: exectunnel tunnel …[/et.muted]"
        )
        con.print(
            "[et.muted]  Note: IPC socket support is not yet implemented "
            "in the session layer.[/et.muted]"
        )
        return 1

    try:
        data = await _query_socket(socket_path)
    except (TimeoutError, OSError, json.JSONDecodeError) as exc:
        con.print(f"[et.error]{Icons.CROSS} Failed to query session: {exc}[/et.error]")
        return 1

    if json_output:
        con.print_json(json.dumps(data))
        return 0

    con.print(_build_status_panel(data))

    if not watch:
        return 0

    # -- Live watch loop --
    with Live(
        _build_status_panel(data),
        console=con,
        refresh_per_second=1,
    ) as live:
        consecutive_failures = 0

        try:
            while True:
                await asyncio.sleep(_WATCH_INTERVAL_SECS)
                try:
                    data = await _query_socket(socket_path)
                    live.update(_build_status_panel(data))
                    consecutive_failures = 0
                except (TimeoutError, OSError, json.JSONDecodeError) as exc:
                    consecutive_failures += 1
                    logger.debug(
                        "Watch query failed (%d/%d): %s",
                        consecutive_failures,
                        _MAX_CONSECUTIVE_FAILURES,
                        exc,
                    )
                    if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                        con.print(
                            f"\n[et.error]{Icons.CROSS} Lost connection to "
                            f"session after {_MAX_CONSECUTIVE_FAILURES} failures: "
                            f"{exc}[/et.error]"
                        )
                        return 1
        except asyncio.CancelledError:
            pass

    return 0
