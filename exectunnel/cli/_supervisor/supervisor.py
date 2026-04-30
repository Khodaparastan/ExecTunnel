"""Parent-side supervisor — spawns one worker subprocess per tunnel.

Responsibilities:

* spawn one ``exectunnel _worker`` subprocess per tunnel,
* feed each worker its :class:`~exectunnel.config.CLIOverrides` via stdin,
* read NDJSON :mod:`.ipc` frames from each worker's stdout,
* update parent-owned dashboard state (``TunnelSlot`` + passive
  :class:`~exectunnel.cli.metrics.HealthMonitor`) from those frames,
* fan SIGINT / SIGTERM out to all children and wait for them to exit,
* return an aggregate exit code.

There is no automatic restart logic here; that belongs to a higher phase.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import psutil

from exectunnel.observability import MetricEvent
from exectunnel.observability.metrics import MetricKind

from ..metrics import HealthMonitor
from .ipc import (
    ExitFrame,
    Frame,
    HealthFrame,
    LogFrame,
    MetricFrame,
    StatusFrame,
    decode_frame,
)
from .process_health import (
    CAPABILITIES,
    ProcessHealth,
    ProcessSampler,
    enforce_termination,
)

if TYPE_CHECKING:
    from exectunnel.config import CLIOverrides, TunnelEntry

    from .._display import TunnelSlot

__all__ = ["Supervisor", "SupervisorResult"]

logger = logging.getLogger("exectunnel.cli.supervisor")

_EXIT_OK: Final[int] = 0
_EXIT_ERROR: Final[int] = 1
_EXIT_INTERRUPTED: Final[int] = 130

_SHUTDOWN_SIGNALS: Final[tuple[signal.Signals, ...]] = tuple(
    sig for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)) if sig is not None
)

_GRACEFUL_SHUTDOWN_TIMEOUT: Final[float] = 10.0
_FORCE_KILL_TIMEOUT: Final[float] = 3.0
_DRAIN_TIMEOUT: Final[float] = 2.0

_LOG_LEVEL_MAP: Final[dict[str, int]] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_STATUS_BY_METRIC: Final[dict[str, str]] = {
    "session.connect.attempt": "starting",
    "session.connect.ok": "running",
    "session.reconnect": "restarting",
    "bootstrap.ok": "healthy",
    "bootstrap.timeout": "failed",
    "session.serve.stopped": "stopped",
}

_CONNECTED_TRUE_METRICS: Final[frozenset[str]] = frozenset({
    "session.connect.ok",
    "bootstrap.ok",
})

_CONNECTED_FALSE_METRICS: Final[frozenset[str]] = frozenset({
    "session.connect.attempt",
    "session.reconnect",
    "bootstrap.timeout",
    "session.serve.stopped",
})


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SupervisorResult:
    """Outcome of a supervisor run."""

    exit_code: int
    per_tunnel_codes: dict[str, int]


# ---------------------------------------------------------------------------
# Per-child state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ChildState:
    name: str
    proc: asyncio.subprocess.Process
    stdout_task: asyncio.Task[None]
    stderr_task: asyncio.Task[None]
    exit_code: int | None = None
    psutil_proc: psutil.Process | None = None
    """Underlying :class:`psutil.Process` handle for introspection."""


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class Supervisor:
    """Process supervisor — runs one worker subprocess per tunnel."""

    __slots__ = (
        "_config_path",
        "_entries",
        "_cli_overrides",
        "_slots",
        "_children",
        "_result_codes",
        "_shutdown",
        "_sampler",
    )

    def __init__(
        self,
        *,
        config_path: Path,
        entries: list[TunnelEntry],
        cli_overrides: CLIOverrides,
        slots: dict[str, TunnelSlot],
    ) -> None:
        self._config_path: Path = config_path
        self._entries: list[TunnelEntry] = entries
        self._cli_overrides: CLIOverrides = cli_overrides
        self._slots: dict[str, TunnelSlot] = slots
        self._children: dict[str, _ChildState] = {}
        self._result_codes: dict[str, int] = {}
        self._shutdown: asyncio.Event = asyncio.Event()
        self._sampler: ProcessSampler = ProcessSampler(
            publish=self._on_process_health,
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> SupervisorResult:
        """Spawn workers, ingest IPC frames, and wait for all children to exit."""
        loop = asyncio.get_running_loop()

        def _on_signal(sig: signal.Signals) -> None:
            logger.info("received %s — shutting down all tunnels", sig.name)
            self._shutdown.set()

        installed: list[signal.Signals] = []
        for sig in _SHUTDOWN_SIGNALS:
            try:
                loop.add_signal_handler(sig, _on_signal, sig)
            except (NotImplementedError, RuntimeError, ValueError):
                logger.debug("signal handler not supported for %s", sig.name)
            else:
                installed.append(sig)

        logger.info(
            "process introspection capability: %s (psutil=%s)",
            CAPABILITIES.process_introspection,
            CAPABILITIES.psutil_version,
        )

        try:
            await self._spawn_all()
            self._sampler.start()

            shutdown_task = asyncio.create_task(
                self._shutdown.wait(),
                name="supervisor-shutdown",
            )
            wait_tasks: list[asyncio.Task[None]] = [
                asyncio.create_task(
                    self._await_child(child),
                    name=f"await-{child.name}",
                )
                for child in self._children.values()
            ]

            try:
                while wait_tasks:
                    done, _pending = await asyncio.wait(
                        {shutdown_task, *wait_tasks},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if shutdown_task in done:
                        await self._terminate_all()

                        for task in wait_tasks:
                            if not task.done():
                                with contextlib.suppress(Exception):
                                    await task
                        break

                    for task in list(done):
                        if task is shutdown_task:
                            continue
                        with contextlib.suppress(Exception):
                            await task

                    wait_tasks = [task for task in wait_tasks if not task.done()]

            finally:
                if not shutdown_task.done():
                    shutdown_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await shutdown_task

            return self._build_result()

        finally:
            await self._sampler.stop()
            for sig in installed:
                with contextlib.suppress(Exception):
                    loop.remove_signal_handler(sig)

    # ------------------------------------------------------------------
    # Spawning
    # ------------------------------------------------------------------

    async def _spawn_all(self) -> None:
        for entry in self._entries:
            try:
                child = await self._spawn_child(entry)
            except Exception as exc:
                logger.error("[%s] failed to spawn worker: %s", entry.name, exc)
                self._result_codes[entry.name] = _EXIT_ERROR

                slot = self._slots.get(entry.name)
                if slot is not None:
                    slot.status = "failed"
                    if slot.monitor is not None:
                        slot.monitor.set_connected(False)
                continue

            self._children[entry.name] = child

    async def _spawn_child(self, entry: TunnelEntry) -> _ChildState:
        argv: list[str] = [
            sys.executable,
            "-m",
            "exectunnel.cli",
            "_worker",
            "--config",
            str(self._config_path),
            "--name",
            entry.name,
        ]

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            assert proc.stdin is not None
            payload = self._cli_overrides.model_dump_json().encode("utf-8")

            try:
                proc.stdin.write(payload)
                await proc.stdin.drain()
            finally:
                proc.stdin.close()
                with contextlib.suppress(Exception):
                    await proc.stdin.wait_closed()

            slot = self._slots.get(entry.name)
            if slot is not None:
                slot.status = "starting"
                if slot.monitor is None:
                    slot.monitor = HealthMonitor(
                        pod_spec=None,
                        ws_url=slot.wss_url,
                        socks_host=slot.socks_host,
                        socks_port=slot.socks_port,
                    )
                slot.monitor.set_connected(False)

            assert proc.stdout is not None
            assert proc.stderr is not None

            stdout_task = asyncio.create_task(
                self._read_stdout(entry.name, proc.stdout),
                name=f"stdout-{entry.name}",
            )
            stderr_task = asyncio.create_task(
                self._read_stderr(entry.name, proc.stderr),
                name=f"stderr-{entry.name}",
            )

            psutil_proc = self._sampler.register(entry.name, proc.pid)

            logger.info(
                "[%s] worker started pid=%d introspection=%s",
                entry.name,
                proc.pid,
                CAPABILITIES.process_introspection,
            )

            return _ChildState(
                name=entry.name,
                proc=proc,
                stdout_task=stdout_task,
                stderr_task=stderr_task,
                psutil_proc=psutil_proc,
            )

        except Exception:
            with contextlib.suppress(Exception):
                if proc.returncode is None:
                    proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise

    # ------------------------------------------------------------------
    # I/O readers
    # ------------------------------------------------------------------

    async def _read_stdout(
        self,
        tunnel: str,
        stream: asyncio.StreamReader,
    ) -> None:
        while True:
            try:
                line = await stream.readline()
            except Exception as exc:
                logger.debug("[%s] stdout read error: %s", tunnel, exc)
                return

            if not line:
                return

            frame = decode_frame(line)
            if frame is None:
                continue

            self._handle_frame(tunnel, frame)

    async def _read_stderr(
        self,
        tunnel: str,
        stream: asyncio.StreamReader,
    ) -> None:
        while True:
            try:
                line = await stream.readline()
            except Exception:
                return

            if not line:
                return

            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.warning("[%s/stderr] %s", tunnel, text)

    # ------------------------------------------------------------------
    # Frame dispatch
    # ------------------------------------------------------------------

    def _handle_frame(self, tunnel: str, frame: Frame) -> None:
        if frame.tunnel != tunnel:
            logger.debug(
                "frame tunnel mismatch: got %r expected %r — ignoring",
                frame.tunnel,
                tunnel,
            )
            return

        slot = self._slots.get(tunnel)

        if isinstance(frame, StatusFrame):
            if slot is not None:
                slot.status = frame.status
            return

        if isinstance(frame, MetricFrame):
            if slot is not None and slot.monitor is not None:
                self._apply_metric_frame(slot.monitor, frame)
                self._apply_status_from_metric(slot, frame)
            return

        if isinstance(frame, HealthFrame):
            if slot is not None and slot.monitor is not None:
                slot.monitor.set_connected(frame.connected)
            return

        if isinstance(frame, LogFrame):
            level = _LOG_LEVEL_MAP.get(frame.level, logging.INFO)
            logger.log(level, "[%s/%s] %s", tunnel, frame.logger, frame.message)
            return

        if isinstance(frame, ExitFrame):
            child = self._children.get(tunnel)
            if child is not None:
                child.exit_code = frame.code
            self._result_codes[tunnel] = frame.code

    # ------------------------------------------------------------------
    # Process-health publish — invoked by ProcessSampler
    # ------------------------------------------------------------------

    def _on_process_health(self, name: str, snapshot: ProcessHealth) -> None:
        slot = self._slots.get(name)
        if slot is not None:
            slot.process = snapshot

    @staticmethod
    def _apply_metric_frame(monitor: HealthMonitor, frame: MetricFrame) -> None:
        try:
            kind = MetricKind(frame.kind)
        except ValueError:
            return

        event = MetricEvent(
            kind=kind,
            name=frame.name,
            value=float(frame.value),
            tags=dict(frame.tags),
            operation=frame.operation,
            current_value=frame.current_value,
        )
        with contextlib.suppress(Exception):
            monitor.on_metric(event)

    @staticmethod
    def _apply_status_from_metric(slot: TunnelSlot, frame: MetricFrame) -> None:
        status = _STATUS_BY_METRIC.get(frame.name)
        if status is not None:
            slot.status = status

        if slot.monitor is None:
            return

        if frame.name in _CONNECTED_TRUE_METRICS:
            slot.monitor.set_connected(True)
        elif frame.name in _CONNECTED_FALSE_METRICS:
            slot.monitor.set_connected(False)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _drain_reader_task(
        self,
        *,
        tunnel: str,
        stream_name: str,
        task: asyncio.Task[None],
    ) -> None:
        if task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            return

        try:
            await asyncio.wait_for(task, timeout=_DRAIN_TIMEOUT)
        except TimeoutError:
            logger.debug(
                "[%s] %s drain timed out; cancelling reader", tunnel, stream_name
            )
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        except Exception:
            logger.debug(
                "[%s] ignored %s drain exception", tunnel, stream_name, exc_info=True
            )

    async def _await_child(self, child: _ChildState) -> None:
        code = await child.proc.wait()
        if child.exit_code is None:
            child.exit_code = code

        self._result_codes[child.name] = child.exit_code

        await self._drain_reader_task(
            tunnel=child.name,
            stream_name="stdout",
            task=child.stdout_task,
        )
        await self._drain_reader_task(
            tunnel=child.name,
            stream_name="stderr",
            task=child.stderr_task,
        )

        self._sampler.record_exit(child.name, child.exit_code)
        self._sampler.unregister(child.name)

        if child.exit_code == 0:
            logger.info("[%s] worker exited cleanly", child.name)
        else:
            logger.warning(
                "[%s] worker exited unexpectedly code=%d",
                child.name,
                child.exit_code,
            )

        slot = self._slots.get(child.name)
        if slot is not None:
            if slot.monitor is not None:
                slot.monitor.set_connected(False)
            if slot.status not in {"failed", "stopped"}:
                slot.status = "stopped" if child.exit_code == 0 else "failed"

    async def _terminate_all(self) -> None:
        """Politely terminate every still-running child, then escalate."""
        live = [
            child for child in self._children.values() if child.proc.returncode is None
        ]
        if not live:
            return

        outcomes = await asyncio.gather(
            *(
                enforce_termination(
                    name=child.name,
                    proc=child.proc,
                    psutil_proc=child.psutil_proc,
                    graceful_timeout_s=_GRACEFUL_SHUTDOWN_TIMEOUT,
                    force_kill_timeout_s=_FORCE_KILL_TIMEOUT,
                )
                for child in live
            ),
            return_exceptions=True,
        )

        for child, outcome in zip(live, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                logger.error(
                    "[%s] termination enforcement raised: %r",
                    child.name,
                    outcome,
                )
                continue

            self._sampler.note_grace_outcome(child.name, succeeded=outcome.graceful)
            if outcome.escalated:
                self._sampler.note_kill_escalation(child.name)

            if outcome.descendants_killed > 0:
                logger.info(
                    "[%s] descendant_cleanup_performed killed=%d",
                    child.name,
                    outcome.descendants_killed,
                )

            if outcome.zombie_seen:
                logger.warning("[%s] worker zombie detected", child.name)

    # ------------------------------------------------------------------
    # Result aggregation
    # ------------------------------------------------------------------

    def _build_result(self) -> SupervisorResult:
        per_tunnel: dict[str, int] = {}

        for entry in self._entries:
            name = entry.name

            code = self._result_codes.get(name)
            if code is None:
                child = self._children.get(name)
                if child is not None and child.exit_code is not None:
                    code = child.exit_code
                else:
                    code = _EXIT_ERROR

            per_tunnel[name] = code

        if self._shutdown.is_set():
            return SupervisorResult(
                exit_code=_EXIT_INTERRUPTED,
                per_tunnel_codes=per_tunnel,
            )

        worst = _EXIT_OK
        for code in per_tunnel.values():
            worst = max(worst, code)

        return SupervisorResult(
            exit_code=worst,
            per_tunnel_codes=per_tunnel,
        )
