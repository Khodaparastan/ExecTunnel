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

_SPINNER_FRAMES: tuple[str, ...] = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_SPINNER_FPS: int = 10
_FRAME_INTERVAL: float = 1.0 / _SPINNER_FPS


class PhaseState(Enum):
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
    PENDING → RUNNING (via start)
    PENDING → DONE    (via done — for zero-duration phases)
    PENDING → SKIPPED (via skip)
    RUNNING → DONE    (via done)
    RUNNING → FAILED  (via fail)
    RUNNING → SKIPPED (via skip)

    All other transitions are no-ops.
    """

    name: str
    label: str
    state: PhaseState = PhaseState.PENDING
    elapsed: float = 0.0
    detail: str = ""

    # Private — excluded from __init__ and repr.
    # Set to current time at construction; reset to now() in start().
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

        PENDING → DONE is allowed for phases that complete without a matching
        start event (e.g. ``decode_done`` fires without ``decode_start``).
        Elapsed is recorded as 0.0 in that case.
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


# Ordered phase definitions: (internal_name, display_label)
_DEFAULT_PHASES: tuple[tuple[str, str], ...] = (
    ("stty", "Suppress terminal echo"),
    ("upload", "Upload agent payload"),
    ("decode", "Decode base64 on remote"),
    ("syntax", "Syntax check agent"),
    ("exec", "Execute agent"),
    ("ready", "Wait for AGENT_READY"),
)


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
        # Cancel tick first to prevent update-after-stop races.
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # Ensure a clean final state before printing the summary.
        self._finalize_phases()

        if self._live is not None:
            self._live.stop()
            self._live = None

        # Print a permanent (non-transient) summary so the result is visible.
        self._console.print(self._render())

    # ── Background tick ───────────────────────────────────────────────────

    async def _tick(self) -> None:
        """Advance the spinner frame at a fixed rate.

        Uses a monotonic deadline instead of a naïve ``sleep(interval)``
        to prevent cumulative drift over long bootstrap sequences.
        Uses ``get_running_loop()`` (preferred over deprecated
        ``get_event_loop()`` inside async contexts).
        """
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
        phase = self._phases.get(name)
        if phase is not None:
            phase.start()

    def done_phase(self, name: str, detail: str = "") -> None:
        phase = self._phases.get(name)
        if phase is not None:
            phase.done(detail)

    def skip_phase(self, name: str, detail: str = "skipped") -> None:
        phase = self._phases.get(name)
        if phase is not None:
            phase.skip(detail)

    def fail_phase(self, name: str, detail: str = "") -> None:
        phase = self._phases.get(name)
        if phase is not None:
            phase.fail(detail)

    def fail_current(self, detail: str = "") -> None:
        """Fail the active phase; skip all phases that were never reached.

        B2 fix: replaces the old pattern of calling ``fail_phase()`` for every
        phase name, which incorrectly turned PENDING phases into FAILED (red ✗).

        Logic
        -----
        * RUNNING phase  → FAILED  (the phase where the error occurred)
        * PENDING phases → SKIPPED (never reached, shown as –)
        * DONE / SKIPPED / FAILED  → no-op (already terminal)

        If no phase is currently RUNNING (error before any phase started),
        the first PENDING phase is marked FAILED so there is always one
        visible error indicator.
        """
        failed_one = False
        for name in self._phase_order:
            p = self._phases[name]
            if p.state is PhaseState.RUNNING:
                p.fail(detail)
                failed_one = True
            elif p.state is PhaseState.PENDING:
                if not failed_one:
                    # Mark the first unstarted phase as failed (error before
                    # bootstrap could even begin).
                    p.fail(detail)
                    failed_one = True
                else:
                    p.skip("not reached")
            # DONE / SKIPPED / FAILED are terminal — no action.

    def finalize(self) -> None:
        """Mark all incomplete phases so the final summary is clean.

        Call this explicitly when bootstrap is done and the dashboard is
        about to take over.  Idempotent — safe to call multiple times.
        """
        self._finalize_phases()

    # ── Internal ──────────────────────────────────────────────────────────

    def _finalize_phases(self) -> None:
        """Settle any phases that did not reach a terminal state.

        * RUNNING  → DONE    (phase completed but we missed the done event)
        * PENDING  → SKIPPED (phase was never started — not applicable here)
        """
        if self._finished:
            return
        self._finished = True

        for name in self._phase_order:
            p = self._phases[name]
            if p.state is PhaseState.RUNNING:
                # Completed without an explicit done event.
                p.done("(completed)")
            elif p.state is PhaseState.PENDING:
                p.skip()
            # DONE / FAILED / SKIPPED phases are already terminal — no action.

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
