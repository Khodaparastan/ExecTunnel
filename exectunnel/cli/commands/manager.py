"""exectunnel manager — run and supervise multiple tunnels from a .env config file."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

__all__ = ["manager"]

_VALID_LOG_LEVELS: frozenset[str] = frozenset({"debug", "info", "warning", "error"})


def manager(
    env_file: Annotated[
        Path,
        typer.Option(
            "--env-file",
            "-e",
            help="Path to the .env config file.",
            envvar="EXECTUNNEL_MANAGER_ENV_FILE",
        ),
    ] = Path(".env"),
    log_level: Annotated[
        str,
        typer.Option(
            "--log-level",
            "-l",
            help="Logging verbosity: debug | info | warning | error.",
            envvar="EXECTUNNEL_MANAGER_LOG_LEVEL",
        ),
    ] = "info",
    tui: Annotated[
        bool,
        typer.Option(
            "--tui/--no-tui",
            help="Show live Rich TUI dashboard (default: on).",
            envvar="EXECTUNNEL_MANAGER_TUI",
        ),
    ] = True,
    show_logs: Annotated[
        bool,
        typer.Option(
            "--show-logs/--no-show-logs",
            help="Show per-pod log tail in the TUI dashboard.",
            envvar="EXECTUNNEL_MANAGER_SHOW_LOGS",
        ),
    ] = False,
) -> None:
    """Run and supervise multiple tunnels from a .env config file.

    Reads WSS_URL_1, WSS_URL_2, … from the config file, starts one tunnel
    subprocess per entry, monitors SOCKS port health, and automatically
    restarts any tunnel that exits or becomes unresponsive.

    \b
    Config file keys (suffix _N is per-tunnel; omit for global default):
      WSS_URL_<N>            WebSocket URL for tunnel N  [required]
      SOCKS_PORT_<N>         local SOCKS5 port           [default: 1081, 1082, …]
      WSS_INSECURE[_<N>]     skip TLS verification       [default: false]
      LOG_LEVEL[_<N>]        debug|info|warning|error    [default: info]
      LABEL_<N>              human-readable name in logs
      RESTART_DELAY          initial restart back-off (s) [default: 5]
      MAX_RESTART_DELAY      max restart back-off (s)     [default: 60]
      BACKOFF_MULTIPLIER     back-off growth factor       [default: 1.5]
      HEALTH_CHECK_INTERVAL  seconds between port probes  [default: 15]
      MAX_HEALTH_FAILURES    failures before restart      [default: 3]
      STARTUP_GRACE_PERIOD   seconds before first probe   [default: 20]

    \b
    Example:
      cp .env.manager.example .env
      exectunnel manager --env-file .env
      exectunnel manager --env-file .env --no-tui   # log-only mode
    """
    if log_level not in _VALID_LOG_LEVELS:
        raise typer.BadParameter(
            f"Invalid log level {log_level!r}. "
            f"Choose from: {', '.join(sorted(_VALID_LOG_LEVELS))}",
            param_hint="'--log-level'",
        )

    # Deferred import to avoid circular dependencies.
    from ..supervisor import run_manager

    run_manager(env_file, log_level=log_level, tui=tui, show_logs=show_logs)
