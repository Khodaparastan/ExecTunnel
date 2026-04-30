"""Supervisor-side process observability and enforcement.

This module is strictly part of the control plane. It is imported and used only
by the parent supervisor — never by workers, tunnel sessions, or any of the
four core pillars (``protocol``, ``proxy``, ``transport``, ``session``).

Responsibilities:

* sample OS-level process facts for each worker (PID, RSS, VMS, CPU,
  threads, file descriptors, child count, status) via :mod:`psutil`,
* expose those facts as immutable :class:`ProcessHealth` snapshots,
* provide :class:`ProcessSampler` — a low-cadence asyncio task that keeps
  per-worker snapshots fresh,
* provide :func:`enforce_termination` — a psutil-aware graceful-stop /
  kill-escalation / descendant-cleanup helper.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal

import psutil
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CAPABILITIES",
    "Capabilities",
    "ProcessHealth",
    "ProcessIdentity",
    "ProcessLineage",
    "ProcessLiveness",
    "ProcessResources",
    "ProcessSampler",
    "ProcessSupervision",
    "TerminationOutcome",
    "enforce_termination",
]

logger = logging.getLogger("exectunnel.cli.supervisor.process_health")


# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Runtime capability flags for process introspection."""

    process_introspection: Literal["full"]
    psutil_version: str


CAPABILITIES: Final[Capabilities] = Capabilities(
    process_introspection="full",
    psutil_version=psutil.__version__,
)


# ---------------------------------------------------------------------------
# Process-health model
# ---------------------------------------------------------------------------


_FROZEN_MODEL_CONFIG: Final[ConfigDict] = ConfigDict(
    frozen=True,
    extra="forbid",
    str_strip_whitespace=True,
)


class ProcessIdentity(BaseModel):
    """Stable process identity fields."""

    model_config = _FROZEN_MODEL_CONFIG

    pid: int
    started_at: float | None = None
    """Unix timestamp the process started, if known."""


class ProcessLiveness(BaseModel):
    """Liveness fields — refreshed every sample."""

    model_config = _FROZEN_MODEL_CONFIG

    alive: bool
    status: str | None = None
    """psutil status string: running, sleeping, zombie, stopped, …"""
    last_sample_at: float


class ProcessResources(BaseModel):
    """Resource-usage fields.

    Every field is optional to tolerate permission-restricted hosts and
    PID-namespaced containers.
    """

    model_config = _FROZEN_MODEL_CONFIG

    rss_bytes: int | None = None
    vms_bytes: int | None = None
    cpu_percent: float | None = None
    num_threads: int | None = None
    num_fds: int | None = None


class ProcessLineage(BaseModel):
    """Process tree fields."""

    model_config = _FROZEN_MODEL_CONFIG

    children_count: int | None = None


class ProcessSupervision(BaseModel):
    """Supervisor-tracked counters, not observed via psutil."""

    model_config = _FROZEN_MODEL_CONFIG

    restart_count: int = 0
    last_exit_code: int | None = None
    grace_termination_succeeded: bool | None = None
    kill_escalation_count: int = 0


class ProcessHealth(BaseModel):
    """Aggregate process-health snapshot for one worker."""

    model_config = _FROZEN_MODEL_CONFIG

    identity: ProcessIdentity
    liveness: ProcessLiveness
    resources: ProcessResources = Field(default_factory=ProcessResources)
    lineage: ProcessLineage = Field(default_factory=ProcessLineage)
    supervision: ProcessSupervision = Field(default_factory=ProcessSupervision)


# ---------------------------------------------------------------------------
# Internal sampler target
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _SampleTarget:
    """Supervisor-local sampling target state."""

    pid: int
    proc: psutil.Process | None = None


# ---------------------------------------------------------------------------
# Sampler
# ---------------------------------------------------------------------------


_DEFAULT_INTERVAL_S: Final[float] = 1.0


class ProcessSampler:
    """Low-cadence sampler that keeps per-worker :class:`ProcessHealth`
    snapshots fresh.

    The sampler does not decide policy. It produces snapshots and invokes a
    publish callback. Higher layers decide what to do with the data.

    Args:
        interval_s: Sampling cadence in seconds. Defaults to ``1.0``.
        publish:    Callback invoked once per sampled worker with
                    ``(name, ProcessHealth)``. Must be cheap and non-blocking.
    """

    __slots__ = (
        "_interval_s",
        "_publish",
        "_targets",
        "_supervision",
        "_task",
        "_stop",
    )

    def __init__(
        self,
        *,
        interval_s: float = _DEFAULT_INTERVAL_S,
        publish: Callable[[str, ProcessHealth], None],
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        self._interval_s: float = interval_s
        self._publish: Callable[[str, ProcessHealth], None] = publish
        self._targets: dict[str, _SampleTarget] = {}
        self._supervision: dict[str, ProcessSupervision] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Target registration
    # ------------------------------------------------------------------

    def register(self, name: str, pid: int) -> psutil.Process | None:
        """Register a worker PID for sampling.

        Returns the attached :class:`psutil.Process` on success, or ``None`` if
        the PID could not be attached immediately. The sampler preserves the PID
        and will retry attachment during future samples.
        """
        if pid <= 0:
            raise ValueError("pid must be positive")

        self._supervision.setdefault(name, ProcessSupervision())

        proc = self._attach_process(name, pid)
        self._targets[name] = _SampleTarget(pid=pid, proc=proc)
        return proc

    def unregister(self, name: str) -> None:
        """Remove a worker from the sampling loop."""
        self._targets.pop(name, None)

    def record_exit(self, name: str, code: int | None) -> None:
        """Update supervision counters when a child exits."""
        cur = self._supervision.get(name, ProcessSupervision())
        self._supervision[name] = cur.model_copy(update={"last_exit_code": code})

    def note_kill_escalation(self, name: str) -> None:
        """Increment the kill-escalation counter for *name*."""
        cur = self._supervision.get(name, ProcessSupervision())
        self._supervision[name] = cur.model_copy(
            update={"kill_escalation_count": cur.kill_escalation_count + 1}
        )

    def note_grace_outcome(self, name: str, *, succeeded: bool) -> None:
        """Record whether graceful termination succeeded for *name*."""
        cur = self._supervision.get(name, ProcessSupervision())
        self._supervision[name] = cur.model_copy(
            update={"grace_termination_succeeded": succeeded}
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the background sampling task, idempotently."""
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="process-sampler")

    async def stop(self) -> None:
        """Signal the sampling task to stop and await completion."""
        if self._task is None:
            return

        self._stop.set()
        task, self._task = self._task, None

        try:
            await asyncio.wait_for(task, timeout=max(1.0, self._interval_s * 2.0))
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("process sampler stop ignored task exception", exc_info=True)

    async def _run(self) -> None:
        while not self._stop.is_set():
            for name, target in list(self._targets.items()):
                snapshot = self._sample_one(name, target)
                if snapshot is None:
                    continue
                try:
                    self._publish(name, snapshot)
                except Exception:
                    logger.debug(
                        "[%s] process-health publish failed", name, exc_info=True
                    )

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue
            else:
                return

    # ------------------------------------------------------------------
    # Sampling primitives
    # ------------------------------------------------------------------

    @staticmethod
    def _attach_process(name: str, pid: int) -> psutil.Process | None:
        try:
            proc = psutil.Process(pid)
            with contextlib.suppress(Exception):
                proc.cpu_percent(interval=None)
            return proc
        except Exception as exc:
            logger.debug("[%s] psutil attach failed pid=%d: %s", name, pid, exc)
            return None

    def _sample_one(self, name: str, target: _SampleTarget) -> ProcessHealth | None:
        now = time.time()
        supervision = self._supervision.get(name, ProcessSupervision())

        if target.proc is None:
            target.proc = self._attach_process(name, target.pid)

        if target.proc is None:
            alive = False
            with contextlib.suppress(Exception):
                alive = bool(psutil.pid_exists(target.pid))
            return ProcessHealth(
                identity=ProcessIdentity(pid=target.pid),
                liveness=ProcessLiveness(
                    alive=alive,
                    status=None,
                    last_sample_at=now,
                ),
                supervision=supervision,
            )

        proc = target.proc

        alive = False
        try:
            alive = proc.is_running()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            target.proc = None
        except Exception:
            alive = False

        if not alive:
            return ProcessHealth(
                identity=ProcessIdentity(pid=target.pid),
                liveness=ProcessLiveness(
                    alive=False,
                    status=None,
                    last_sample_at=now,
                ),
                supervision=supervision,
            )

        pid = target.pid
        started_at: float | None = None
        status: str | None = None
        rss: int | None = None
        vms: int | None = None
        cpu_pct: float | None = None
        threads: int | None = None
        fds: int | None = None
        children_count: int | None = None

        try:
            with proc.oneshot():
                with contextlib.suppress(Exception):
                    pid = int(proc.pid)

                with contextlib.suppress(Exception):
                    started_at = float(proc.create_time())

                with contextlib.suppress(Exception):
                    status = proc.status()

                with contextlib.suppress(Exception):
                    mi = proc.memory_info()
                    rss = int(mi.rss) or None
                    vms = int(mi.vms) or None

                with contextlib.suppress(Exception):
                    cpu_pct = float(proc.cpu_percent(interval=None))

                with contextlib.suppress(Exception):
                    threads = int(proc.num_threads())

                with contextlib.suppress(Exception):
                    fds = int(proc.num_fds())

                with contextlib.suppress(Exception):
                    children_count = len(proc.children(recursive=False))
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            target.proc = None
            alive = False

        return ProcessHealth(
            identity=ProcessIdentity(pid=pid, started_at=started_at),
            liveness=ProcessLiveness(
                alive=alive,
                status=status,
                last_sample_at=now,
            ),
            resources=ProcessResources(
                rss_bytes=rss,
                vms_bytes=vms,
                cpu_percent=cpu_pct,
                num_threads=threads,
                num_fds=fds,
            ),
            lineage=ProcessLineage(children_count=children_count),
            supervision=supervision,
        )


# ---------------------------------------------------------------------------
# Termination enforcement
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TerminationOutcome:
    """Result of :func:`enforce_termination`."""

    name: str
    graceful: bool
    """``True`` if the worker exited within the graceful window."""
    escalated: bool
    """``True`` if SIGKILL was sent."""
    descendants_killed: int
    """Number of descendant PIDs killed during cleanup."""
    zombie_seen: bool


async def enforce_termination(
    *,
    name: str,
    proc: asyncio.subprocess.Process,
    psutil_proc: psutil.Process | None,
    graceful_timeout_s: float,
    force_kill_timeout_s: float,
) -> TerminationOutcome:
    """Politely terminate a worker, escalating to SIGKILL on timeout.

    Always sends SIGTERM first and waits up to ``graceful_timeout_s`` for the
    process to exit. On timeout it snapshots descendants from ``psutil_proc``,
    sends SIGKILL to the parent, then SIGKILLs any descendants still alive.
    """
    if graceful_timeout_s <= 0:
        raise ValueError("graceful_timeout_s must be positive")
    if force_kill_timeout_s <= 0:
        raise ValueError("force_kill_timeout_s must be positive")

    if proc.returncode is not None:
        return TerminationOutcome(
            name=name,
            graceful=True,
            escalated=False,
            descendants_killed=0,
            zombie_seen=False,
        )

    descendants: list[psutil.Process] = []
    if psutil_proc is not None:
        with contextlib.suppress(Exception):
            descendants = psutil_proc.children(recursive=True)

    with contextlib.suppress(ProcessLookupError):
        proc.terminate()

    graceful = False
    escalated = False

    try:
        await asyncio.wait_for(proc.wait(), timeout=graceful_timeout_s)
        graceful = True
    except TimeoutError:
        escalated = True
        logger.warning(
            "[%s] worker did not exit within %.1fs — escalating to SIGKILL",
            name,
            graceful_timeout_s,
        )
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=force_kill_timeout_s)
        except TimeoutError:
            logger.error(
                "[%s] worker did not exit after SIGKILL within %.1fs",
                name,
                force_kill_timeout_s,
            )
        except Exception:
            logger.debug(
                "[%s] ignored exception while waiting after SIGKILL",
                name,
                exc_info=True,
            )

    zombie_seen = False
    if psutil_proc is not None:
        with contextlib.suppress(Exception):
            zombie_seen = psutil_proc.status() == psutil.STATUS_ZOMBIE
            if zombie_seen:
                logger.warning("[%s] worker process is zombie", name)

    descendants_killed = 0
    descendants_to_wait: list[psutil.Process] = []

    for child in descendants:
        try:
            if not child.is_running():
                continue
            with contextlib.suppress(Exception):
                if child.status() == psutil.STATUS_ZOMBIE:
                    continue
            child.kill()
            descendants_killed += 1
            descendants_to_wait.append(child)
        except Exception:
            continue

    if descendants_to_wait:
        with contextlib.suppress(Exception):
            psutil.wait_procs(descendants_to_wait, timeout=force_kill_timeout_s)

    if descendants_killed > 0:
        logger.warning(
            "[%s] descendant_cleanup_performed killed=%d",
            name,
            descendants_killed,
        )

    return TerminationOutcome(
        name=name,
        graceful=graceful,
        escalated=escalated,
        descendants_killed=descendants_killed,
        zombie_seen=zombie_seen,
    )
