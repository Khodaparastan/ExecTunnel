"""exectunnel ws — establish tunnel via a raw WebSocket URL."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from exectunnel import __version__
from exectunnel.observability.logging import configure_logging
from exectunnel.session import TunnelConfig

from .._config import build_session_config
from ..runner import run_session
from ..ui import BANNER, THEME, BootstrapSpinner, Icons

__all__ = ["connect"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_URL_DISPLAY_MAX = 90
_VALID_SCHEMES = ("ws://", "wss://")
_VALID_LOG_LEVELS = frozenset({"debug", "info", "warning", "error"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_console() -> Console:
    return Console(theme=THEME, highlight=False)


def _print_banner(con: Console) -> None:

    con.print(BANNER.format(version=__version__))


def _normalize_log_level(value: str) -> Literal["debug", "info", "warning", "error"]:
    level = value.lower().strip()
    if level == "warn":
        level = "warning"
    return level


def _parse_headers(
    token: str | None,
    raw_headers: list[str],
    con: Console,
) -> dict[str, str]:
    """Build the WS header dict from --token and --header flags."""
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for raw in raw_headers:
        if ":" not in raw:
            con.print(
                f"[et.warn]{Icons.WARN} Skipping malformed header: "
                f"{raw!r} (expected 'Name: Value')[/et.warn]"
            )
            continue
        name, _, value = raw.partition(":")
        headers[name.strip()] = value.strip()

    return headers


def _print_ws_summary(
    con: Console,
    url: str,
    headers: dict[str, str],
    socks_host: str,
    socks_port: int,
) -> None:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="et.label", min_width=12)
    t.add_column(style="et.value")

    url_display = (
        url[:_URL_DISPLAY_MAX] + "…" if len(url) > _URL_DISPLAY_MAX + 3 else url
    )
    has_auth = "Authorization" in headers
    extra_count = len(headers) - (1 if has_auth else 0)

    t.add_row("URL", url_display)
    t.add_row("SOCKS5", f"{socks_host}:{socks_port}")
    t.add_row(
        "Auth",
        Text("Bearer token ✓", style="et.ok")
        if has_auth
        else Text("None", style="et.warn"),
    )
    t.add_row("Extra headers", str(extra_count) if extra_count else "—")

    con.print(f"\n[et.brand]{Icons.GLOBE} WebSocket Mode[/et.brand]")
    con.print(t)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def connect(
    url: Annotated[
        str,
        typer.Argument(
            help=(
                "WebSocket exec URL.  Example:\n\n"
                "  wss://k8s.example.com/api/v1/namespaces/default"
                "/pods/mypod/exec?command=/bin/sh&stdin=true&stdout=true"
            ),
            metavar="URL",
        ),
    ],
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            "-t",
            help="Bearer token for Kubernetes API authentication.",
            envvar="EXECTUNNEL_TOKEN",
        ),
    ] = None,
    header: Annotated[
        list[str] | None,
        typer.Option(
            "--header",
            "-H",
            help="Extra HTTP header in 'Name: Value' format. Repeatable.",
        ),
    ] = None,
    socks_port: Annotated[
        int,
        typer.Option("--socks-port", "-p", help="Local SOCKS5 port.", min=1, max=65535),
    ] = 1080,
    socks_host: Annotated[
        str,
        typer.Option("--socks-host", help="Local SOCKS5 bind address."),
    ] = "127.0.0.1",
    ready_timeout: Annotated[
        float,
        typer.Option("--ready-timeout", help="Seconds to wait for AGENT_READY."),
    ] = 30.0,
    reconnect_retries: Annotated[
        int,
        typer.Option(
            "--reconnect-retries",
            help="Max reconnect attempts (0 = no retries).",
            min=0,
        ),
    ] = 5,
    insecure: Annotated[
        bool,
        typer.Option("--insecure", help="Skip TLS certificate verification."),
    ] = False,
    no_dashboard: Annotated[
        bool,
        typer.Option("--no-dashboard", help="Disable the live dashboard."),
    ] = False,
    log_level: Annotated[
        str,
        typer.Option(
            "--log-level",
            "-l",
            help="Logging verbosity: debug | info | warning | error.",
            envvar="EXECTUNNEL_LOG_LEVEL",
        ),
    ] = "info",
    show_logs: Annotated[
        bool,
        typer.Option(
            "--show-logs/--no-show-logs",
            help="Show live log tail in the dashboard.",
        ),
    ] = False,
) -> None:
    """Connect via a raw WebSocket exec URL."""
    normalized_log_level = _normalize_log_level(log_level)
    if normalized_log_level not in _VALID_LOG_LEVELS:
        raise typer.BadParameter(
            f"Invalid log level {log_level!r}. "
            f"Choose from: {', '.join(sorted(_VALID_LOG_LEVELS))}",
            param_hint="'--log-level'",
        )

    configure_logging(normalized_log_level)

    con = _get_console()
    _print_banner(con)

    try:
        exit_code = asyncio.run(
            _ws_async(
                con=con,
                url=url,
                token=token,
                extra_headers=header or [],
                socks_port=socks_port,
                socks_host=socks_host,
                ready_timeout=ready_timeout,
                reconnect_retries=reconnect_retries,
                insecure=insecure,
                no_dashboard=no_dashboard,
                show_logs=show_logs,
            )
        )
    except KeyboardInterrupt:
        con.print(f"\n[et.warn]{Icons.WARN} Interrupted.[/et.warn]")
        exit_code = 0

    raise typer.Exit(exit_code)


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------


async def _ws_async(
    *,
    con: Console,
    url: str,
    token: str | None,
    extra_headers: list[str],
    socks_port: int,
    socks_host: str,
    ready_timeout: float,
    reconnect_retries: int,
    insecure: bool,
    no_dashboard: bool,
    show_logs: bool = False,
) -> int:
    if not url.startswith(_VALID_SCHEMES):
        con.print(
            f"[et.error]{Icons.CROSS} URL must start with ws:// or wss://\n"
            f"  Got: {url!r}[/et.error]"
        )
        con.print(
            "[et.muted]  Hint: if you meant to use a Kubernetes API server, "
            "try:\n"
            "  exectunnel ws wss://<server>/api/v1/namespaces/<ns>"
            "/pods/<pod>/exec?command=/bin/sh&stdin=true&stdout=true"
            "&stderr=true&tty=false[/et.muted]"
        )
        return 1

    ws_headers = _parse_headers(token, extra_headers, con)

    if insecure:
        con.print(f"[et.warn]{Icons.WARN} TLS verification disabled.[/et.warn]")

    _print_ws_summary(con, url, ws_headers, socks_host, socks_port)

    # Build configs

    session_cfg = build_session_config(
        wss_url=url,
        ws_headers=ws_headers,
        insecure=insecure,
        reconnect_max_retries=reconnect_retries,
    )

    tun_cfg = TunnelConfig(
        socks_host=socks_host,
        socks_port=socks_port,
        ready_timeout=ready_timeout,
    )

    con.print()
    async with BootstrapSpinner(con) as spinner:
        spinner.start_phase("stty")
        return await run_session(
            session_cfg=session_cfg,
            tun_cfg=tun_cfg,
            ws_url=url,
            pod_spec=None,
            console=con,
            no_dashboard=no_dashboard,
            show_logs=show_logs,
            spinner=spinner,
        )
