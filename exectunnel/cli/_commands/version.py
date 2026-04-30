"""``version`` command — print version and exit."""

from __future__ import annotations

import typer
from rich.console import Console

__all__: list[str] = []

try:
    from importlib.metadata import version as _pkg_version

    _VERSION: str = _pkg_version("exectunnel")
except Exception:
    _VERSION = "dev"


def version_command(ctx: typer.Context) -> None:  # noqa: ARG001
    """Print the ExecTunnel version and exit."""
    console = Console(stderr=False)
    console.print(f"ExecTunnel [bold cyan]{_VERSION}[/bold cyan]")
    raise typer.Exit(0)
