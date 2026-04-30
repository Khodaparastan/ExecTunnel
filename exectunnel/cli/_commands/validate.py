"""``validate`` command — load, validate, and summarise the config file."""

from __future__ import annotations

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table
from rich.text import Text

from exectunnel.config import ConfigFileError, TunnelFile, load_config_file

from .._context import AppContext

__all__: list[str] = []


def validate_command(ctx: typer.Context) -> None:
    """Validate the config file and print a summary table.

    Re-parses the config file from disk to give a fresh, authoritative
    validation result independent of what :class:`AppContext` cached.
    Exits with code ``0`` on success, ``1`` on any failure.
    """
    app_ctx: AppContext = ctx.obj
    # validate writes to stdout so it can be piped/redirected independently
    # of the stderr log stream.
    console = Console(stderr=False)

    if app_ctx.config_path is None:
        console.print(
            "[bold red]✗[/bold red] No config file found. "
            "Pass [bold]--config[/bold] or create "
            "[dim]~/.config/exectunnel/config.toml[/dim]."
        )
        raise typer.Exit(1)

    # ── Load ──────────────────────────────────────────────────────────────────

    try:
        raw = load_config_file(app_ctx.config_path)
    except ConfigFileError as exc:
        console.print(f"[bold red]✗[/bold red] {exc}")
        raise typer.Exit(1) from exc

    # ── Validate ──────────────────────────────────────────────────────────────

    try:
        tunnel_file = TunnelFile.model_validate(raw)
    except ValidationError as exc:
        console.print(
            f"[bold red]✗[/bold red] Config validation failed "
            f"([dim]{app_ctx.config_path}[/dim]):\n"
        )
        for error in exc.errors():
            loc = " → ".join(str(part) for part in error["loc"])
            console.print(f"  [red]•[/red] [bold]{loc}[/bold]: {error['msg']}")
        raise typer.Exit(1) from exc

    # ── Summary ───────────────────────────────────────────────────────────────

    console.print(
        f"\n[bold green]✓[/bold green] Config valid "
        f"([dim]{app_ctx.config_path}[/dim])\n"
    )

    # Global config summary
    g = tunnel_file.global_config
    console.print("[bold cyan]Global defaults[/bold cyan]")
    global_table = Table(show_header=False, box=None, padding=(0, 2))
    global_table.add_column("key", style="dim")
    global_table.add_column("value")

    _dim_default = Text("(default)", style="dim")
    global_fields: dict[str, object] = {
        "log_level": g.log_level,
        "log_format": g.log_format,
        "socks_host": g.socks_host,
        "reconnect_max_retries": g.reconnect_max_retries,
        "bootstrap_delivery": g.bootstrap_delivery,
        "dns_upstream": g.dns_upstream,
    }
    for key, val in global_fields.items():
        global_table.add_row(key, str(val) if val is not None else _dim_default)

    console.print(global_table)
    console.print()

    # Tunnels summary
    enabled_names = {e.name for e in tunnel_file.enabled_tunnels()}
    console.print(
        f"[bold cyan]Tunnels[/bold cyan] "
        f"([dim]{len(tunnel_file.tunnels)} defined, "
        f"{len(enabled_names)} enabled[/dim])\n"
    )

    tun_table = Table(
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 2),
    )
    tun_table.add_column("NAME", style="bold")
    tun_table.add_column("PORT", justify="right")
    tun_table.add_column("ENABLED")
    tun_table.add_column("WSS URL", overflow="fold")
    tun_table.add_column("DNS")
    tun_table.add_column("SSL")

    _dim_dash = Text("—", style="dim")

    for t in tunnel_file.tunnels:
        enabled_text = (
            Text("yes", style="green")
            if t.name in enabled_names
            else Text("no", style="dim")
        )

        ssl_parts: list[Text] = []
        if t.ssl_no_verify:
            ssl_parts.append(Text("no-verify", style="red"))
        elif t.ssl_ca_cert:
            ssl_parts.append(Text("custom-ca", style="yellow"))
        if t.ssl_cert:
            ssl_parts.append(Text("client-cert", style="yellow"))
        ssl_text: Text = (
            Text(", ").join(ssl_parts) if ssl_parts else Text("default", style="dim")
        )

        dns_text: Text | str = (
            str(t.dns_upstream) if t.dns_upstream is not None else _dim_dash
        )

        tun_table.add_row(
            t.name,
            str(t.socks_port),
            enabled_text,
            str(t.wss_url),
            dns_text,
            ssl_text,
        )

    console.print(tun_table)
    console.print()
