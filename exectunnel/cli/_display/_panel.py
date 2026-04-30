"""Rich Live status panel for the ``run`` command.

:class:`LivePanel` is a lightweight async context manager used by the
``run`` and ``run-single`` commands when no :class:`HealthMonitor` is
available (i.e. before the session layer is wired up).  It renders a
simple status table from :class:`TunnelStatusRegistry`.

For full metric-aware dashboards, use :class:`~._dashboard.UnifiedDashboard`.

Thread-safety note
------------------
:class:`TunnelStatusRegistry` mutations are asyncio-safe (single event-loop,
cooperative multitasking) but are **not** thread-safe.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from types import TracebackType

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

__all__ = ["LivePanel", "TunnelStatus", "TunnelStatusRegistry"]

_REFRESH_INTERVAL: float = 1.0


# ---------------------------------------------------------------------------
# Status enum & style map
# ---------------------------------------------------------------------------


class TunnelStatus(Enum):
    """Lifecycle state of a single managed tunnel."""

    STARTING = auto()
    RUNNING = auto()
    RECONNECTING = auto()
    STOPPED = auto()
    FAILED = auto()
    DISABLED = auto()


_STATUS_STYLE: dict[TunnelStatus, tuple[str, str]] = {
    TunnelStatus.STARTING: ("●", "yellow"),
    TunnelStatus.RUNNING: ("●", "green"),
    TunnelStatus.RECONNECTING: ("↻", "yellow"),
    TunnelStatus.STOPPED: ("○", "dim"),
    TunnelStatus.FAILED: ("✗", "red"),
    TunnelStatus.DISABLED: ("–", "dim"),
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TunnelStatusEntry:
    """Mutable state for a single tunnel row in the status panel."""

    name: str
    socks_host: str
    socks_port: int
    status: TunnelStatus = TunnelStatus.STARTING
    started_at: float = field(default_factory=time.monotonic)
    reconnects: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TunnelStatusRegistry:
    """Asyncio-safe registry of :class:`TunnelStatusEntry` objects."""

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: dict[str, TunnelStatusEntry] = {}

    def register(
        self,
        name: str,
        socks_host: str,
        socks_port: int,
        *,
        disabled: bool = False,
    ) -> None:
        """Register a new tunnel entry (overwrites existing)."""
        self._entries[name] = TunnelStatusEntry(
            name=name,
            socks_host=socks_host,
            socks_port=socks_port,
            status=TunnelStatus.DISABLED if disabled else TunnelStatus.STARTING,
        )

    def set_status(self, name: str, status: TunnelStatus) -> None:
        if entry := self._entries.get(name):
            entry.status = status

    def increment_reconnects(self, name: str) -> None:
        if entry := self._entries.get(name):
            entry.reconnects += 1

    def set_error(self, name: str, error: str | None) -> None:
        if entry := self._entries.get(name):
            entry.error = error

    def entries(self) -> list[TunnelStatusEntry]:
        return list(self._entries.values())


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_uptime(started_at: float) -> str:
    elapsed = int(time.monotonic() - started_at)
    h, remainder = divmod(elapsed, 3600)
    m, s = divmod(remainder, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _build_table(entries: list[TunnelStatusEntry]) -> Table:
    table = Table(
        show_header=True,
        header_style="bold cyan",
        box=None,
        padding=(0, 2),
        expand=True,
    )
    table.add_column("NAME", style="bold", min_width=16)
    table.add_column("PORT", justify="right", min_width=6)
    table.add_column("STATUS", min_width=14)
    table.add_column("UPTIME", justify="right", min_width=10)
    table.add_column("RECONNECTS", justify="right", min_width=10)
    table.add_column("PROXY", min_width=22)

    _dim_dash = Text("—", style="dim")

    for entry in entries:
        icon, colour = _STATUS_STYLE[entry.status]
        status_text = Text(f"{icon} {entry.status.name.lower()}", style=colour)

        if entry.status is TunnelStatus.DISABLED:
            uptime = _dim_dash
            reconnects = _dim_dash
            proxy = _dim_dash
        else:
            uptime = _format_uptime(entry.started_at)
            reconnects = str(entry.reconnects)
            proxy = f"{entry.socks_host}:{entry.socks_port}"

        table.add_row(
            entry.name,
            str(entry.socks_port),
            status_text,
            uptime,
            reconnects,
            proxy,
        )

    return table


# ---------------------------------------------------------------------------
# LivePanel
# ---------------------------------------------------------------------------


class LivePanel:
    """Async context manager that renders a live-updating tunnel status panel.

    Lightweight alternative to :class:`~._dashboard.UnifiedDashboard` for
    use before a :class:`~exectunnel.cli.metrics.HealthMonitor` is available.

    Usage::

        async with LivePanel(registry, console):
            await run_all_tunnels(...)
    """

    __slots__ = ("_registry", "_console", "_live", "_task")

    def __init__(self, registry: TunnelStatusRegistry, console: Console) -> None:
        self._registry = registry
        self._console = console
        self._live: Live | None = None
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> LivePanel:
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=4,
            transient=False,
        )
        self._live.__enter__()
        self._task = asyncio.create_task(self._refresh_loop(), name="panel-refresh")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

        if self._live is not None:
            self._live.__exit__(exc_type, exc_val, exc_tb)

    def _render(self) -> Table:
        return _build_table(self._registry.entries())

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(_REFRESH_INTERVAL)
            if self._live is not None:
                self._live.update(self._render())
