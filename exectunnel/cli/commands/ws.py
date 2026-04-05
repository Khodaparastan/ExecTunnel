"""exectunnel ws — establish tunnel via a raw WebSocket URL."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from exectunnel.config.settings import AppConfig, TunnelConfig
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .._session_runner import run_session
from .._spinner import BootstrapSpinner
from .._theme import BANNER, THEME, Icons

__all__ = ["ws"]

console = Console(theme=THEME, highlight=False)


def ws(
    url: Annotated[
        str,
        typer.Argument(
            help=(
                "WebSocket exec URL.  Example:\n\n"
                "  wss://k8s.example.com/api/v1/namespaces/default"
                "/pods/mypod/exec?command=/bin/sh&stdin=true&stdout=true"
            ),
            # Prevent Typer from treating the URL as a sub-command.
            metavar="URL",
        ),
    ],
    token: Annotated[
        str | None,
        typer.Option(
            "--token", "-t",
            help="Bearer token for Kubernetes API authentication.",
            envvar="EXECTUNNEL_TOKEN",
        ),
    ] = None,
    header: Annotated[
        list[str] | None,
        typer.Option(
            "--header", "-H",
            help="Extra HTTP header in 'Name: Value' format. Repeatable.",
        ),
    ] = None,
    socks_port: Annotated[
        int,
        typer.Option(
            "--socks-port", "-p",
            help="Local SOCKS5 port.",
            min=1,
            max=65535,
        ),
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
            help="Max reconnect attempts (0 = unlimited).",
        ),
    ] = 5,
    insecure: Annotated[
        bool,
        typer.Option(
            "--insecure",
            help="Skip TLS certificate verification.",
        ),
    ] = False,
    no_dashboard: Annotated[
        bool,
        typer.Option("--no-dashboard", help="Disable the live dashboard."),
    ] = False,
) -> None:
    """Connect via a raw WebSocket exec URL (no kubectl required)."""
    _print_banner()

    try:
        exit_code = asyncio.run(
            _ws_async(
                url=url,
                token=token,
                extra_headers=header or [],
                socks_port=socks_port,
                socks_host=socks_host,
                ready_timeout=ready_timeout,
                reconnect_retries=reconnect_retries,
                insecure=insecure,
                no_dashboard=no_dashboard,
            )
        )
    except KeyboardInterrupt:
        console.print(f"\n[et.warn]{Icons.WARN} Interrupted.[/et.warn]")
        exit_code = 0

    raise typer.Exit(exit_code)


async def _ws_async(
    *,
    url: str,
    token: str | None,
    extra_headers: list[str],
    socks_port: int,
    socks_host: str,
    ready_timeout: float,
    reconnect_retries: int,
    insecure: bool,
    no_dashboard: bool,
) -> int:
    # ── Validate URL scheme ───────────────────────────────────────────────────
    if not url.startswith(("ws://", "wss://")):
        console.print(
            f"[et.error]{Icons.CROSS} URL must start with ws:// or wss://\n"
            f"  Got: {url!r}[/et.error]"
        )
        console.print(
            f"[et.muted]  Hint: if you meant to use a Kubernetes API server, "
            f"try:\n"
            f"  exectunnel ws wss://<server>/api/v1/namespaces/<ns>"
            f"/pods/<pod>/exec?command=/bin/sh&stdin=true&stdout=true"
            f"&stderr=true&tty=false[/et.muted]"
        )
        return 1

    # ── Parse extra headers ───────────────────────────────────────────────────
    ws_headers: dict[str, str] = {}
    if token:
        ws_headers["Authorization"] = f"Bearer {token}"

    for raw in extra_headers:
        if ":" not in raw:
            console.print(
                f"[et.warn]{Icons.WARN} Skipping malformed header: "
                f"{raw!r} (expected 'Name: Value')[/et.warn]"
            )
            continue
        name, _, value = raw.partition(":")
        ws_headers[name.strip()] = value.strip()

    # ── SSL context ───────────────────────────────────────────────────────────
    import ssl as _ssl

    if insecure:
        ssl_ctx = _ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = _ssl.CERT_NONE
        console.print(
            f"[et.warn]{Icons.WARN} TLS verification disabled.[/et.warn]"
        )
    else:
        ssl_ctx = _ssl.create_default_context()

    # ── Summary ───────────────────────────────────────────────────────────────
    _print_ws_summary(url, ws_headers, socks_host, socks_port)

    # ── Config ────────────────────────────────────────────────────────────────
    from exectunnel.config import get_app_config
    app_cfg = AppConfig.from_env(get_app_config())
    tun_cfg = TunnelConfig(
        socks_host=socks_host,
        socks_port=socks_port,
        ready_timeout=ready_timeout,
    )

    # ── Bootstrap with spinner then dashboard ─────────────────────────────────
    console.print()
    async with BootstrapSpinner(console) as spinner:
        spinner.start_phase("stty")
        return await run_session(
            app_cfg=app_cfg,
            tun_cfg=tun_cfg,
            ws_url=url,
            pod_spec=None,
            kubectl_ctx=None,
            console=console,
            no_dashboard=no_dashboard,
            spinner=spinner,
        )


def _print_banner() -> None:
    from exectunnel import __version__
    console.print(BANNER.format(version=__version__))


def _print_ws_summary(
    url: str,
    headers: dict[str, str],
    socks_host: str,
    socks_port: int,
) -> None:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="et.label", min_width=12)
    t.add_column(style="et.value")

    url_display = url[:90] + "…" if len(url) > 93 else url
    auth_count = 1 if "Authorization" in headers else 0

    t.add_row("URL",        url_display)
    t.add_row("SOCKS5",     f"{socks_host}:{socks_port}")
    t.add_row(
        "Auth",
        Text("Bearer token ✓", style="et.ok")
        if "Authorization" in headers
        else Text("None", style="et.warn"),
    )
    t.add_row(
        "Extra headers",
        str(len(headers) - auth_count) if len(headers) - auth_count else "—",
    )

    console.print(f"\n[et.brand]{Icons.GLOBE} WebSocket Mode[/et.brand]")
    console.print(t)
