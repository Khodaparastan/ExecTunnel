"""Root Typer application — registers all sub-commands."""

from __future__ import annotations

import typer
from rich.console import Console

from .ui import THEME

__all__ = ["app"]

# Shared console instance — avoids constructing a new Console on every
# callback invocation (--version, implicit help, etc.).
_console = Console(theme=THEME, highlight=False)

app = typer.Typer(
    name="exectunnel",
    help=(
        "ExecTunnel — SOCKS5 proxy tunnel through Kubernetes exec WebSocket.\n\n"
        "[bold]Quick start:[/bold]\n\n"
        "  [cyan]exectunnel tunnel[/cyan]\n\n"
        "  [cyan]exectunnel tunnel --dns 10.96.0.10[/cyan]\n\n"
        "  [cyan]exectunnel ws wss://k8s.example.com/…[/cyan]\n\n"
        "  [cyan]exectunnel config show[/cyan]\n\n"
        "  [cyan]exectunnel status[/cyan]\n"
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _register_commands() -> None:
    """Deferred import and registration to avoid circular imports.

    Command modules are imported here (rather than at the top of the file)
    because they in turn import from ``exectunnel.session``, which imports
    from ``exectunnel.cli`` — registering lazily breaks the cycle.
    """
    from .commands.config import app as _config_app
    from .commands.connect import connect
    from .commands.manager import manager
    from .commands.status import status
    from .commands.tunnel import tunnel

    app.add_typer(_config_app, name="config")
    app.command("tunnel")(tunnel)
    app.command("ws")(connect)
    app.command("status")(status)
    app.command("manager")(manager)


_register_commands()


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

        _console.print(
            f"[et.brand]ExecTunnel[/et.brand] [et.value]{__version__}[/et.value]"
        )
        raise typer.Exit()

    if ctx.invoked_subcommand is None:
        _console.print(ctx.get_help())
