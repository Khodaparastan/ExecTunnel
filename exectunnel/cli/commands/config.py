"""exectunnel config — show and validate configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Annotated

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..ui import THEME, Icons

__all__ = ["app"]

app = typer.Typer(
    name="config",
    help="Show and validate ExecTunnel configuration.",
)

# Shared console — one instance for all config sub-commands.
_console = Console(theme=THEME, highlight=False)


# ── Environment variable registry ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _EnvEntry:
    """Metadata for a single ExecTunnel environment variable."""

    name: str
    description: str
    default: str = "(not set)"
    sensitive: bool = False


_ENV_VARS: tuple[_EnvEntry, ...] = (
    _EnvEntry("EXECTUNNEL_WSS_URL", "WebSocket endpoint (or WSS_URL)"),
    _EnvEntry("WSS_INSECURE", "Skip TLS verification", "false"),
    _EnvEntry("EXECTUNNEL_PING_INTERVAL", "Keepalive ping interval (s)", "20"),
    _EnvEntry("EXECTUNNEL_SEND_TIMEOUT", "WS send timeout (s)", "30"),
    _EnvEntry("EXECTUNNEL_SEND_QUEUE_CAP", "Send queue capacity", "512"),
    _EnvEntry("EXECTUNNEL_RECONNECT_MAX_RETRIES", "Max reconnect retries", "10"),
    _EnvEntry("EXECTUNNEL_RECONNECT_BASE_DELAY", "Reconnect base delay (s)", "1.0"),
    _EnvEntry("EXECTUNNEL_RECONNECT_MAX_DELAY", "Reconnect max delay (s)", "30.0"),
    _EnvEntry("EXECTUNNEL_DNS_MAX_INFLIGHT", "DNS max inflight queries", "1024"),
    _EnvEntry("EXECTUNNEL_CONNECT_MAX_PENDING", "Max pending connects (global)", "128"),
    _EnvEntry(
        "EXECTUNNEL_CONNECT_MAX_PENDING_PER_HOST",
        "Max pending connects (per host)",
        "16",
    ),
    _EnvEntry(
        "EXECTUNNEL_PRE_ACK_BUFFER_CAP_BYTES", "Pre-ACK buffer cap (bytes)", "65536"
    ),
    _EnvEntry("EXECTUNNEL_BOOTSTRAP_DELIVERY", "Agent delivery mode", "upload"),
    _EnvEntry("EXECTUNNEL_BOOTSTRAP_USE_GO_AGENT", "Use Go agent binary", "false"),
    _EnvEntry(
        "EXECTUNNEL_FETCH_AGENT_URL", "Fetch URL for fetch delivery", "(default)"
    ),
    _EnvEntry("EXECTUNNEL_TOKEN", "Bearer token (raw WS mode)", sensitive=True),
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _current_value_text(entry: _EnvEntry, *, redact: bool) -> Text:
    """Read the env var for *entry* and return a styled Text renderable.

    * Unset      → ``(default)`` (muted)
    * Sensitive  → ``***`` (warn) when *redact* is True
    * Set        → raw value (ok)
    """
    raw = os.environ.get(entry.name)
    if raw is None:
        return Text("(default)", style="et.muted")
    if redact and entry.sensitive:
        return Text("***", style="et.warn")
    return Text(raw, style="et.ok")


# ── Commands ──────────────────────────────────────────────────────────────────


@app.command("show")
def show_config(
    redact: Annotated[
        bool,
        typer.Option("--redact/--no-redact", help="Redact sensitive values."),
    ] = True,
) -> None:
    """Display all ExecTunnel environment variables and their current values."""
    table = Table(
        box=box.SIMPLE_HEAD,
        header_style="et.label",
        border_style="et.border",
        expand=True,
    )
    table.add_column("Variable", style="et.value", ratio=3)
    table.add_column("Description", style="et.muted", ratio=3)
    table.add_column("Default", style="et.muted", ratio=2)
    table.add_column("Current", ratio=2)

    for entry in _ENV_VARS:
        table.add_row(
            entry.name,
            entry.description,
            entry.default,
            _current_value_text(entry, redact=redact),
        )

    _console.print(
        Panel(
            table,
            title=f"[et.brand]{Icons.BOLT} ExecTunnel Configuration[/et.brand]",
            border_style="et.border",
        )
    )


@app.command("validate")
def validate_config() -> None:
    """Validate current ExecTunnel configuration and report any issues."""
    errors: list[str] = []
    warnings: list[str] = []
    ok_items: list[str] = []

    # ── WebSocket URL ─────────────────────────────────────────────────────
    wss_url = os.environ.get("EXECTUNNEL_WSS_URL") or os.environ.get("WSS_URL")
    if wss_url:
        if wss_url.startswith(("ws://", "wss://")):
            ok_items.append("WSS URL is set and has valid scheme")
        else:
            errors.append(
                f"WSS URL must start with ws:// or wss://, got: {wss_url[:40]}"
            )
    else:
        warnings.append("No WSS URL set (EXECTUNNEL_WSS_URL / WSS_URL)")

    # ── Full SessionConfig round-trip ──────────────────────────────────────
    try:
        from .._config import build_session_config

        build_session_config()
        ok_items.append("Full SessionConfig validation passed")
    except ImportError as exc:
        # Structural problem — always an error regardless of URL presence.
        errors.append(f"Cannot import settings module: {exc}")
    except Exception as exc:  # noqa: BLE001
        # If the URL is configured, config should succeed → real error.
        # If the URL is absent, the failure is expected (URL is required) →
        # downgrade to a warning to avoid redundant noise alongside the
        # already-flagged "No WSS URL set" warning above.
        if wss_url:
            errors.append(f"SessionConfig validation failed: {exc}")
        else:
            warnings.append(f"SessionConfig incomplete (URL not set): {exc}")

    # ── Render results ────────────────────────────────────────────────────
    _console.print(f"\n[et.brand]{Icons.BOLT} Configuration Validation[/et.brand]\n")
    for item in ok_items:
        _console.print(f"  [et.ok]{Icons.CHECK}[/et.ok] {item}")
    for item in warnings:
        _console.print(f"  [et.warn]{Icons.WARN}[/et.warn] {item}")
    for item in errors:
        _console.print(f"  [et.error]{Icons.CROSS}[/et.error] {item}")

    if errors:
        _console.print(
            f"\n[et.error]{Icons.CROSS} Validation failed with "
            f"{len(errors)} error(s).[/et.error]"
        )
        raise typer.Exit(1)
    _console.print(f"\n[et.ok]{Icons.CHECK} All checks passed.[/et.ok]")
