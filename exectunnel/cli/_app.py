"""Root Typer application — registers all sub-commands."""

from __future__ import annotations

import typer
from rich.console import Console

from ._theme import THEME

__all__ = ["app"]

app = typer.Typer(
    name="exectunnel",
    help=(
        "ExecTunnel — SOCKS5 proxy tunnel through Kubernetes exec WebSocket.\n\n"
        "[bold]Quick start:[/bold]\n\n"
        "  [cyan]exectunnel connect myapp-pod-abc123[/cyan]\n\n"
        "  [cyan]exectunnel connect --selector app=myapp[/cyan]\n\n"
        "  [cyan]exectunnel ws wss://k8s.example.com/api/v1/namespaces/default"
        "/pods/mypod/exec?command=/bin/sh[/cyan]\n\n"
        "  [cyan]exectunnel config show[/cyan]\n\n"
        "  [cyan]exectunnel status[/cyan]\n"
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_enable=True,
    pretty_exceptions_show_locals=False,
)

# ── config is a group (has sub-commands: show, validate) ─────────────────────
from .commands.config import app as _config_app  # noqa: E402

app.add_typer(_config_app, name="config")

# ── connect, ws, status are direct commands ───────────────────────────────────
from .commands.connect import connect  # noqa: E402
from .commands.status import status  # noqa: E402
from .commands.ws import ws  # noqa: E402

app.command("connect")(connect)
app.command("ws")(ws)
app.command("status")(status)


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show version and exit.",
        is_eager=True,
    ),
) -> None:
    if version:
        from exectunnel import __version__
        console = Console(theme=THEME, highlight=False)
        console.print(
            f"[et.brand]ExecTunnel[/et.brand] [et.value]{__version__}[/et.value]"
        )
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        console = Console(theme=THEME, highlight=False)
        console.print(ctx.get_help())
