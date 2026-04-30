"""Rich theme, style constants, and icon set for the ExecTunnel CLI."""

from __future__ import annotations

from rich.theme import Theme

THEME = Theme({
    # Brand & status
    "et.brand": "bold cyan",
    "et.ok": "bold green",
    "et.warn": "bold yellow",
    "et.error": "bold red",
    "et.muted": "dim white",
    # Text roles
    "et.label": "bold white",
    "et.value": "cyan",
    "et.highlight": "bold magenta",
    # Structural
    "et.border": "bright_black",
    # Bootstrap phases
    "et.phase.done": "green",
    "et.phase.run": "yellow",
    "et.phase.fail": "red",
    # Stat severity
    "et.stat.good": "bold green",
    "et.stat.warn": "bold yellow",
    "et.stat.bad": "bold red",
    "et.stat.idle": "dim white",
    # Connection states
    "et.conn.open": "green",
    "et.conn.closed": "dim red",
    "et.conn.pend": "yellow",
})


class Icons:
    """Unicode glyphs used throughout the CLI.

    Intentionally used as a namespace — do not instantiate.
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
