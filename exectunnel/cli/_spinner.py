"""Bootstrap phase spinner — shows progress through the agent upload sequence."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum, auto

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from ._theme import Icons

__all__ = ["BootstrapSpinner", "Phase", "PhaseState"]


class PhaseState(Enum):
    PENDING = auto()
    RUNNING = auto()
    DONE    = auto()
    FAILED  = auto()


@dataclass
class Phase:
    name:    str
    label:   str
    state:   PhaseState = PhaseState.PENDING
    elapsed: float      = 0.0
    detail:  str        = ""
    _start:  float      = field(default_factory=time.monotonic, repr=False)

    def start(self) -> None:
        self.state  = PhaseState.RUNNING
        self._start = time.monotonic()

    def done(self, detail: str = "") -> None:
        self.state   = PhaseState.DONE
        self.elapsed = time.monotonic() - self._start
        self.detail  = detail

    def fail(self, detail: str = "") -> None:
        self.state   = PhaseState.FAILED
        self.elapsed = time.monotonic() - self._start
        self.detail  = detail


_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class BootstrapSpinner:
    """Renders a live phase-by-phase bootstrap progress display.

    Usage::

        async with BootstrapSpinner(console) as spinner:
            spinner.start_phase("stty")
            await do_stty()
            spinner.done_phase("stty")
            ...
    """

    PHASES = [
        Phase("stty",    "Suppress terminal echo"),
        Phase("upload",  "Upload agent payload"),
        Phase("decode",  "Decode base64 on remote"),
        Phase("syntax",  "Syntax check agent.py"),
        Phase("exec",    "Execute agent"),
        Phase("ready",   "Wait for AGENT_READY"),
    ]

    __slots__ = ("_console", "_phases", "_live", "_frame_idx", "_task")

    def __init__(self, console: Console) -> None:
        self._console   = console
        self._phases    = [Phase(p.name, p.label) for p in self.PHASES]
        self._live: Live | None = None
        self._frame_idx = 0
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "BootstrapSpinner":
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=10,
            transient=False,
        )
        self._live.__enter__()
        self._task = asyncio.create_task(self._tick(), name="spinner-tick")
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._live:
            self._live.__exit__(None, None, None)

    async def _tick(self) -> None:
        while True:
            self._frame_idx = (self._frame_idx + 1) % len(_SPINNER_FRAMES)
            if self._live:
                self._live.update(self._render())
            await asyncio.sleep(0.1)

    def start_phase(self, name: str) -> None:
        for p in self._phases:
            if p.name == name:
                p.start()
                break

    def done_phase(self, name: str, detail: str = "") -> None:
        for p in self._phases:
            if p.name == name:
                p.done(detail)
                break

    def fail_phase(self, name: str, detail: str = "") -> None:
        for p in self._phases:
            if p.name == name:
                p.fail(detail)
                break

    def _render(self) -> Table:
        t = Table.grid(padding=(0, 1))
        t.add_column(width=3)
        t.add_column(min_width=32)
        t.add_column(style="et.muted", min_width=8)
        t.add_column(style="et.muted")

        spinner = _SPINNER_FRAMES[self._frame_idx]

        for p in self._phases:
            match p.state:
                case PhaseState.PENDING:
                    icon  = Text("○", style="et.muted")
                    label = Text(p.label, style="et.muted")
                    timer = Text("")
                case PhaseState.RUNNING:
                    icon  = Text(spinner, style="et.phase.run")
                    label = Text(p.label, style="bold white")
                    timer = Text(
                        f"{time.monotonic() - p._start:.1f}s",
                        style="et.phase.run",
                    )
                case PhaseState.DONE:
                    icon  = Text(Icons.CHECK, style="et.phase.done")
                    label = Text(p.label, style="et.phase.done")
                    timer = Text(f"{p.elapsed:.2f}s", style="et.muted")
                case PhaseState.FAILED:
                    icon  = Text(Icons.CROSS, style="et.phase.fail")
                    label = Text(p.label, style="et.phase.fail")
                    timer = Text(f"{p.elapsed:.2f}s", style="et.muted")

            t.add_row(icon, label, timer, Text(p.detail, style="et.muted"))

        return t
