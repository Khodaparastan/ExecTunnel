"""Shared formatting helpers for CLI dashboards."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from rich.table import Table
from rich.text import Text

_BYTES_UNIT_BASE: Final[float] = 1024.0
_URL_MAX_DISPLAY: Final[int] = 72
_ERROR_BAD_STYLE_THRESHOLD: Final[int] = 5

LOG_LEVEL_STYLES: Final[dict[str, str]] = {
    "DEBUG": "dim cyan",
    "INFO": "green",
    "WARNING": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold red reverse",
}


def fmt_bytes(n: int) -> str:
    """Human-readable byte count."""
    value = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < _BYTES_UNIT_BASE:
            return f"{value:.1f} {unit}"
        value /= _BYTES_UNIT_BASE
    return f"{value:.1f} PB"


def fmt_uptime(secs: float) -> str:
    """Format seconds as ``HH:MM:SS`` or ``Xd HH:MM:SS``."""
    td = timedelta(seconds=max(0, int(secs)))
    h, rem = divmod(td.seconds, 3600)
    m, s = divmod(rem, 60)
    if td.days:
        return f"{td.days}d {h:02d}:{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def err_style(count: int) -> str:
    """Return a Rich style name based on error severity."""
    if count <= 0:
        return "et.muted"
    return "et.stat.bad" if count >= _ERROR_BAD_STYLE_THRESHOLD else "et.stat.warn"


def truncate_url(url: str, max_len: int = _URL_MAX_DISPLAY) -> str:
    """Truncate *url* to *max_len* characters, including the ellipsis."""
    if max_len <= 1:
        return "…" if url else ""
    if len(url) <= max_len:
        return url
    return f"{url[: max_len - 1]}…"


def label_col(*, min_width: int = 14) -> Table:
    """Return a two-column grid configured for key/value rows."""
    table = Table.grid(padding=(0, 1))
    table.add_column(style="et.label", min_width=min_width)
    table.add_column(style="et.value")
    return table


def status_dot(ok: bool) -> Text:
    """Return a coloured connection indicator."""
    style = "et.ok" if ok else "et.error"
    label = "Connected" if ok else "Disconnected"
    return Text.assemble(Text("● ", style=style), Text(label, style=style))


def ack_rate_str(ack_ok: int, ack_failed: int) -> str:
    """Return ACK success rate as a percentage string, or ``'—'``."""
    total = ack_ok + ack_failed
    if total == 0:
        return "—"
    return f"{ack_ok / total * 100:.1f}%"
