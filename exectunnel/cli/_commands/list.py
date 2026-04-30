"""``list`` command — show all tunnels with their effective resolved config."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from exectunnel.config import CLIOverrides

from .._context import AppContext

__all__: list[str] = []


def list_command(ctx: typer.Context) -> None:
    """List all defined tunnels and their effective resolved configuration.

    Resolves each tunnel through the full merge chain so the displayed
    values reflect what the session layer will actually receive.
    Exits with code ``1`` if no config file is available or loading failed.
    """
    app_ctx: AppContext = ctx.obj
    console = Console(stderr=False)

    if app_ctx.config_load_error is not None:
        console.print(f"[bold red]✗[/bold red] {app_ctx.config_load_error}")
        raise typer.Exit(1)

    if app_ctx.tunnel_file is None:
        console.print(
            "[bold red]✗[/bold red] No config file loaded. "
            "Pass [bold]--config[/bold] or create "
            "[dim]~/.config/exectunnel/config.toml[/dim]."
        )
        raise typer.Exit(1)

    tf = app_ctx.tunnel_file

    if not tf.tunnels:
        console.print("[dim]No tunnels defined.[/dim]")
        raise typer.Exit(0)

    table = Table(
        show_header=True,
        header_style="bold cyan",
        box=None,
        padding=(0, 2),
        expand=True,
    )
    table.add_column("NAME", style="bold", min_width=16)
    table.add_column("PORT", justify="right", min_width=6)
    table.add_column("ENABLED", min_width=8)
    table.add_column("SOCKS HOST", min_width=14)
    table.add_column("DNS UPSTREAM", min_width=14)
    table.add_column("BOOTSTRAP", min_width=10)
    table.add_column("RECONNECTS", justify="right", min_width=10)
    table.add_column("WSS URL", overflow="fold")

    enabled_names = {e.name for e in tf.enabled_tunnels()}
    _empty_overrides = CLIOverrides()
    _dim_dash = Text("—", style="dim")

    for entry in tf.tunnels:
        try:
            session_cfg, tun_cfg = tf.resolve(entry, _empty_overrides)
        except Exception as exc:  # noqa: BLE001
            table.add_row(
                entry.name,
                str(entry.socks_port),
                Text("error", style="red"),
                "—",
                "—",
                "—",
                "—",
                Text(str(exc), style="red"),
            )
            continue

        enabled_text = (
            Text("yes", style="green")
            if entry.name in enabled_names
            else Text("no", style="dim")
        )
        dns_text: Text | str = (
            tun_cfg.dns_upstream if tun_cfg.dns_upstream is not None else _dim_dash
        )

        table.add_row(
            entry.name,
            str(tun_cfg.socks_port),
            enabled_text,
            tun_cfg.socks_host,
            dns_text,
            tun_cfg.bootstrap_delivery,
            str(session_cfg.reconnect_max_retries),
            str(session_cfg.wss_url),
        )

    console.print()
    console.print(table)
    console.print()
