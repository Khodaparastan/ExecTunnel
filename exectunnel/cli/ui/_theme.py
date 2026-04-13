"""Rich theme, style constants, and icon set for the ExecTunnel CLI."""

from __future__ import annotations

from rich.theme import Theme

__all__ = ["BANNER", "THEME", "Icons"]

THEME = Theme({
    "et.brand": "bold cyan",
    "et.ok": "bold green",
    "et.warn": "bold yellow",
    "et.error": "bold red",
    "et.muted": "dim white",
    "et.label": "bold white",
    "et.value": "cyan",
    "et.highlight": "bold magenta",
    "et.border": "bright_black",
    "et.phase.done": "green",
    "et.phase.run": "yellow",
    "et.phase.fail": "red",
    "et.stat.good": "bold green",
    "et.stat.warn": "bold yellow",
    "et.stat.bad": "bold red",
    "et.stat.idle": "dim white",
    "et.conn.open": "green",
    "et.conn.closed": "dim red",
    "et.conn.pend": "yellow",
})


class Icons:
    """Unicode glyphs used throughout the CLI.

    This class is intentionally used as a namespace; do not instantiate it.
    """

    __slots__ = ()

    ROCKET = "🚀"
    CHECK = "✓"
    CROSS = "✗"
    WARN = "⚠"
    ARROW_RIGHT = "→"
    ARROW_UP = "↑"
    ARROW_DOWN = "↓"
    BOLT = "⚡"
    LOCK = "🔒"
    GLOBE = "🌐"
    POD = "☸"
    TUNNEL = "⬡"
    SOCKS = "🧦"
    DNS = "🔍"
    CLOCK = "⏱"
    HEART = "♥"
    PULSE = "◉"
    RECONNECT = "↻"
    BYTES_UP = "▲"
    BYTES_DOWN = "▼"
    SEPARATOR = "─"
    BULLET = "•"

    # Additional icons for manager dashboard
    FRAME = "▦"
    CONN = "⇌"
    UDP = "◇"
    UP = "▲"
    DOWN = "▼"


# Callers MUST call  BANNER.format(version=__version__)  before printing.
BANNER = (
    "\n"
    "[et.brand]"
    "  ___                 _____                       _\n"
    " | __| __ ___ ___  |_   _|  _ _ _ _ _ _  ___| |\n"
    " | _|  \\ \\/ -_) __| _|| || | ' \\ ' \\/ -_) |\n"
    " |___/_\\_\\___\\___||_|  \\_,_|_||_|_||_\\___|_|"
    "[/et.brand]\n"
    "[et.muted]  Kubernetes exec → SOCKS5 tunnel  │  v{version}[/et.muted]\n"
)
