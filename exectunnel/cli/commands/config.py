"""exectunnel config — show and validate configuration."""

from __future__ import annotations

import os
from typing import Annotated

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .._theme import THEME, Icons

__all__ = ["app"]

app = typer.Typer(
    name="config",
    help="Show and validate ExecTunnel configuration.",
)

console = Console(theme=THEME, highlight=False)

_ENV_VARS = [
    ("EXECTUNNEL_SOCKS_HOST",              "SOCKS5 bind host",              "127.0.0.1"),
    ("EXECTUNNEL_SOCKS_PORT",              "SOCKS5 bind port",              "1080"),
    ("EXECTUNNEL_AGENT_TIMEOUT",           "Agent ready timeout (s)",       "30"),
    ("EXECTUNNEL_SEND_TIMEOUT",            "WS send timeout (s)",           "10"),
    ("EXECTUNNEL_RECONNECT_MAX_RETRIES",   "Max reconnect retries",         "5"),
    ("EXECTUNNEL_RECONNECT_BASE_DELAY",    "Reconnect base delay (s)",      "1.0"),
    ("EXECTUNNEL_RECONNECT_MAX_DELAY",     "Reconnect max delay (s)",       "60.0"),
    ("EXECTUNNEL_SEND_QUEUE_CAP",          "Send queue capacity",           "512"),
    ("EXECTUNNEL_CONNECT_MAX_PENDING",     "Max pending connects (global)", "64"),
    ("EXECTUNNEL_CONNECT_MAX_PER_HOST",    "Max pending connects (per host)","8"),
    ("EXECTUNNEL_PRE_ACK_BUFFER_CAP",      "Pre-ACK buffer cap (bytes)",    "65536"),
    ("EXECTUNNEL_DNS_UPSTREAM",            "DNS upstream host",             "(disabled)"),
    ("EXECTUNNEL_DNS_LOCAL_PORT",          "DNS local port",                "5353"),
    ("EXECTUNNEL_DNS_MAX_INFLIGHT",        "DNS max inflight queries",      "64"),
    ("EXECTUNNEL_PING_INTERVAL",           "Keepalive ping interval (s)",   "20"),
    ("EXECTUNNEL_TOKEN",                   "Bearer token (raw WS mode)",    "(not set)"),
    ("KUBECONFIG",                         "kubeconfig path",               "~/.kube/config"),
]


@app.command("show")
def show_config(
    redact: Annotated[
        bool,
        typer.Option("--redact/--no-redact", help="Redact sensitive values."),
    ] = True,
) -> None:
    """Display all ExecTunnel environment variables and their current values."""
    _SENSITIVE = {"EXECTUNNEL_TOKEN"}

    table = Table(
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="et.label",
        border_style="et.border",
        expand=True,
    )
    table.add_column("Variable",    style="et.value",  ratio=3)
    table.add_column("Description", style="et.muted",  ratio=3)
    table.add_column("Default",     style="et.muted",  ratio=2)
    table.add_column("Current",     ratio=2)

    for env_var, description, default in _ENV_VARS:
        current = os.environ.get(env_var)
        if current is None:
            current_text = Text("(default)", style="et.muted")
        elif redact and env_var in _SENSITIVE:
            current_text = Text("***", style="et.warn")
        else:
            current_text = Text(current, style="et.ok")

        table.add_row(env_var, description, default, current_text)

    console.print(
        Panel(
            table,
            title=f"[et.brand]{Icons.BOLT} ExecTunnel Configuration[/et.brand]",
            border_style="et.border",
        )
    )


@app.command("validate")
def validate_config(
    kubeconfig: Annotated[
        str | None,
        typer.Option("--kubeconfig", help="Path to kubeconfig to validate."),
    ] = None,
    context: Annotated[
        str | None,
        typer.Option("--context", help="kubeconfig context to validate."),
    ] = None,
) -> None:
    """Validate kubeconfig and environment configuration."""
    import asyncio

    from exectunnel.exceptions import ConfigurationError

    from .._kubectl import KubectlDiscovery

    errors: list[str] = []
    warnings: list[str] = []
    ok_items: list[str] = []

    # ── Port range checks ─────────────────────────────────────────────────────
    socks_port_str = os.environ.get("EXECTUNNEL_SOCKS_PORT", "1080")
    try:
        socks_port = int(socks_port_str)
        if not 1 <= socks_port <= 65535:
            raise ValueError
        ok_items.append(f"SOCKS5 port {socks_port} is valid")
    except ValueError:
        errors.append(f"EXECTUNNEL_SOCKS_PORT={socks_port_str!r} is not a valid port")

    # ── kubeconfig check ──────────────────────────────────────────────────────
    discovery = KubectlDiscovery(kubeconfig=kubeconfig, context=context)
    try:
        ctx = discovery.load_context()
        ok_items.append(f"kubeconfig context {ctx.name!r} loaded successfully")
        ok_items.append(f"Cluster server: {ctx.server}")
        if ctx.insecure_skip:
            warnings.append("insecure-skip-tls-verify is enabled — not recommended")
        if not ctx.ca_data and not ctx.insecure_skip:
            warnings.append("No CA certificate found — using system trust store")
    except ConfigurationError as exc:
        errors.append(f"kubeconfig: {exc.message}")

    # ── Timeout sanity ────────────────────────────────────────────────────────
    timeout_str = os.environ.get("EXECTUNNEL_AGENT_TIMEOUT", "30")
    try:
        timeout = float(timeout_str)
        if timeout < 5:
            warnings.append(
                f"EXECTUNNEL_AGENT_TIMEOUT={timeout}s is very low — "
                "bootstrap may time out on slow networks"
            )
        else:
            ok_items.append(f"Agent timeout {timeout}s is reasonable")
    except ValueError:
        errors.append(
            f"EXECTUNNEL_AGENT_TIMEOUT={timeout_str!r} is not a valid number"
        )

    # ── Render results ────────────────────────────────────────────────────────
    console.print(
        f"\n[et.brand]{Icons.CHECK} Configuration Validation[/et.brand]\n"
    )
    for item in ok_items:
        console.print(f"  [et.ok]{Icons.CHECK}[/et.ok] {item}")
    for item in warnings:
        console.print(f"  [et.warn]{Icons.WARN}[/et.warn] {item}")
    for item in errors:
        console.print(f"  [et.error]{Icons.CROSS}[/et.error] {item}")

    if errors:
        console.print(
            f"\n[et.error]{Icons.CROSS} Validation failed with "
            f"{len(errors)} error(s).[/et.error]"
        )
        raise typer.Exit(1)
    else:
        console.print(
            f"\n[et.ok]{Icons.CHECK} All checks passed.[/et.ok]"
        )
