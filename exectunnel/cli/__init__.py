"""ExecTunnel CLI — public entry point."""

from ._app import app

__all__ = ["app", "main"]


def main() -> None:
    """Setuptools entry point — delegates to the Typer app."""
    app()
