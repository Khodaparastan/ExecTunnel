"""Bootstrap phase spinner — shows progress through the agent upload sequence."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from enum import Enum, auto

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from ._theme import Icons

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SPINNER_FRAMES: tuple[str, ...] = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_SPINNER_FPS: int = 10
_FRAME_INTERVAL: float = 1.0 / _SPINNER_FPS

# Ordered phase definitions: (internal_name, display_label)
_DEFAULT_PHASES: tuple[tuple[str, str], ...] = (
    ("stty", "Suppress terminal echo"),
    ("upload", "Upload agent payload"),
    ("decode", "Decode base64 on remote"),
    ("syntax", "Syntax check agent"),
    ("exec", "Execute agent"),
    ("ready", "Wait for AGENT_READY"),
)

# Exported phase name constants — use these instead of raw strings.
PHASE_NAMES: tuple[str, ...] = tuple(name for name, _ in _DEFAULT_PHASES)


# ---------------------------------------------------------------------------
# Phase state machine
# ---------------------------------------------------------------------------


class PhaseState(Enum):
    """Lifecycle state of a single bootstrap phase."""

    PENDING = auto()
    RUNNING = auto()
    DONE = auto()
    SKIPPED = auto()
    FAILED = auto()


@dataclass
class Phase:
    """Single bootstrap phase with its own state machine.

    State transitions
    -----------------
    ``PENDING → RUNNING`` (via :meth:`start`)
    ``PENDING → DONE``    (via :meth:`done` — zero-duration phases)
    ``PENDING → SKIPPED`` (via :meth:`skip`)
    ``RUNNING → DONE``    (via :meth:`done`)
    ``RUNNING → FAILED``  (via :meth:`fail`)
    ``RUNNING → SKIPPED`` (via :meth:`skip`)

    All other transitions are no-ops.
    """

    name: str
    label: str
    state: PhaseState = PhaseState.PENDING
    elapsed: float = 0.0
    detail: str = ""

    _start: float = field(
        default_factory=time.monotonic,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def elapsed_running(self) -> float:
        """Live elapsed seconds for a RUNNING phase; recorded elapsed otherwise."""
        if self.state is PhaseState.RUNNING:
            return time.monotonic() - self._start
        return self.elapsed

    # ── Transitions ───────────────────────────────────────────────────────

    def start(self) -> None:
        if self.state is PhaseState.PENDING:
            self._start = time.monotonic()
            self.state = PhaseState.RUNNING

    def done(self, detail: str = "") -> None:
        """Transition to DONE from PENDING or RUNNING.

        ``PENDING → DONE`` is allowed for phases that complete without a
        matching start event.  Elapsed is recorded as ``0.0`` in that case.
        """
        if self.state in (PhaseState.PENDING, PhaseState.RUNNING):
            self.elapsed = (
                time.monotonic() - self._start
                if self.state is PhaseState.RUNNING
                else 0.0
            )
            self.state = PhaseState.DONE
            self.detail = detail

    def skip(self, detail: str = "skipped") -> None:
        if self.state in (PhaseState.PENDING, PhaseState.RUNNING):
            self.elapsed = 0.0
            self.state = PhaseState.SKIPPED
            self.detail = detail

    def fail(self, detail: str = "") -> None:
        if self.state in (PhaseState.PENDING, PhaseState.RUNNING):
            self.elapsed = (
                time.monotonic() - self._start
                if self.state is PhaseState.RUNNING
                else 0.0
            )
            self.state = PhaseState.FAILED
            self.detail = detail


# ---------------------------------------------------------------------------
# BootstrapSpinner
# ---------------------------------------------------------------------------


class BootstrapSpinner:
    """Renders a live phase-by-phase bootstrap progress display.

    Usage::

        async with BootstrapSpinner(console) as spinner:
            spinner.start_phase("stty")
            await do_stty()
            spinner.done_phase("stty")

    On context exit, any PENDING phases are marked SKIPPED and any orphaned
    RUNNING phases are marked DONE so the final summary is always clean.

    The spinner uses ``transient=True`` so its output is cleared when stopped,
    allowing the dashboard to take over the full screen without artefacts.
    """

    __slots__ = (
        "_console",
        "_phases",
        "_phase_order",
        "_live",
        "_frame_idx",
        "_task",
        "_finished",
    )

    def __init__(self, console: Console) -> None:
        self._console = console
        self._phases: dict[str, Phase] = {
            name: Phase(name, label) for name, label in _DEFAULT_PHASES
        }
        self._phase_order: list[str] = [name for name, _ in _DEFAULT_PHASES]
        self._live: Live | None = None
        self._frame_idx: int = 0
        self._task: asyncio.Task[None] | None = None
        self._finished: bool = False

    async def __aenter__(self) -> BootstrapSpinner:
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=_SPINNER_FPS,
            transient=True,
        )
        self._live.start()
        self._task = asyncio.create_task(self._tick(), name="spinner-tick")
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        self._finalize_phases()

        if self._live is not None:
            self._live.stop()
            self._live = None

        # Print a permanent (non-transient) summary.
        self._console.print(self._render())

    # ── Background tick ───────────────────────────────────────────────────

    async def _tick(self) -> None:
        """Advance the spinner frame at a fixed rate using monotonic deadlines."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _FRAME_INTERVAL
        try:
            while True:
                sleep_for = max(0.0, deadline - loop.time())
                await asyncio.sleep(sleep_for)
                deadline += _FRAME_INTERVAL
                self._frame_idx = (self._frame_idx + 1) % len(_SPINNER_FRAMES)
                if self._live is not None:
                    self._live.update(self._render())
        except asyncio.CancelledError:
            return

    # ── Public API ────────────────────────────────────────────────────────

    def start_phase(self, name: str) -> None:
        """Begin animation for *name*. No-op for unknown phase names."""
        if phase := self._phases.get(name):
            phase.start()

    def done_phase(self, name: str, detail: str = "") -> None:
        if phase := self._phases.get(name):
            phase.done(detail)

    def skip_phase(self, name: str, detail: str = "skipped") -> None:
        if phase := self._phases.get(name):
            phase.skip(detail)

    def fail_phase(self, name: str, detail: str = "") -> None:
        if phase := self._phases.get(name):
            phase.fail(detail)

    def fail_current(self, detail: str = "") -> None:
        """Fail the active phase; skip all phases that were never reached.

        * ``RUNNING``  → ``FAILED``
        * ``PENDING``  → ``SKIPPED`` (or ``FAILED`` if no phase was running)
        * Terminal states → no-op
        """
        failed_one = False
        for name in self._phase_order:
            p = self._phases[name]
            if p.state is PhaseState.RUNNING:
                p.fail(detail)
                failed_one = True
            elif p.state is PhaseState.PENDING:
                if not failed_one:
                    p.fail(detail)
                    failed_one = True
                else:
                    p.skip("not reached")

    def finalize(self) -> None:
        """Mark all incomplete phases so the final summary is clean.

        Idempotent — safe to call multiple times.
        """
        self._finalize_phases()

    # ── Internal ──────────────────────────────────────────────────────────

    def _finalize_phases(self) -> None:
        if self._finished:
            return
        self._finished = True
        for name in self._phase_order:
            p = self._phases[name]
            if p.state is PhaseState.RUNNING:
                p.done("(completed)")
            elif p.state is PhaseState.PENDING:
                p.skip()

    def _render(self) -> Table:
        """Build the phase-grid renderable for the current frame."""
        t = Table.grid(padding=(0, 1))
        t.add_column(width=3)  # icon
        t.add_column(min_width=32)  # label
        t.add_column(style="et.muted", min_width=8)  # timer
        t.add_column(style="et.muted")  # detail

        spinner_char = _SPINNER_FRAMES[self._frame_idx]

        for name in self._phase_order:
            p = self._phases[name]
            match p.state:
                case PhaseState.PENDING:
                    icon = Text("○", style="et.muted")
                    label = Text(p.label, style="et.muted")
                    timer = Text()
                    detail = Text()

                case PhaseState.RUNNING:
                    icon = Text(spinner_char, style="et.phase.run")
                    label = Text(p.label, style="bold white")
                    timer = Text(f"{p.elapsed_running:.1f}s", style="et.phase.run")
                    detail = Text(p.detail, style="et.muted")

                case PhaseState.DONE:
                    icon = Text(Icons.CHECK, style="et.phase.done")
                    label = Text(p.label, style="et.phase.done")
                    timer = Text(f"{p.elapsed:.2f}s", style="et.muted")
                    detail = Text(p.detail, style="et.muted")

                case PhaseState.SKIPPED:
                    icon = Text("–", style="et.muted")
                    label = Text(p.label, style="et.muted")
                    timer = Text()
                    detail = Text(p.detail or "skipped", style="et.muted")

                case PhaseState.FAILED:
                    icon = Text(Icons.CROSS, style="et.phase.fail")
                    label = Text(p.label, style="et.phase.fail")
                    timer = Text(f"{p.elapsed:.2f}s", style="et.muted")
                    detail = Text(p.detail, style="et.phase.fail")

                case _:  # pragma: no cover
                    icon = label = timer = detail = Text()

            t.add_row(icon, label, timer, detail)

        return t
