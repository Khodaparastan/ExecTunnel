"""Parent-side supervisor — spawns one worker subprocess per tunnel.

Responsibilities:

* spawn one ``exectunnel _worker`` subprocess per tunnel,
* feed each worker its :class:`~exectunnel.config.CLIOverrides` via stdin,
* read NDJSON :mod:`.ipc` frames from each worker's stdout,
* update parent-owned dashboard state (``TunnelSlot`` + passive
  :class:`~exectunnel.cli.metrics.HealthMonitor`) from those frames,
* fan SIGINT / SIGTERM out to all children and wait for them to exit,
* apply a :class:`RestartPolicy` to non-fatal worker exits,
* (optional) gate respawns on a :class:`ProviderHealthWatcher` so we don't
  burn restart budget when the provider is reported ``down``,
* (optional) refresh the local config from a :class:`RemoteConfigClient`
  on ``EXIT_AUTH_FAILURE`` exit codes so a stale token can be replaced
  without operator intervention,
* return an aggregate exit code.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

import psutil

from exectunnel.observability import MetricEvent
from exectunnel.observability.metrics import MetricKind

from .._exit_codes import (
    EXIT_AUTH_FAILURE,
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_OK,
    FATAL_EXIT_CODES,
    RETRYABLE_EXIT_CODES,
)
from ..metrics import HealthMonitor
from .ipc import (
    AuthFailureFrame,
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
    from exectunnel.config import CLIOverrides, TunnelEntry, TunnelFile

    from .._display import TunnelSlot
    from .._remote_config import (
        ProviderHealthWatcher,
        RemoteConfigClient,
    )

__all__ = ["RestartPolicy", "Supervisor", "SupervisorResult"]

logger = logging.getLogger("exectunnel.cli.supervisor")

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
# RestartPolicy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RestartPolicy:
    """Per-worker restart policy applied by the supervisor.

    Workers exiting with a code in :data:`FATAL_EXIT_CODES` are NEVER
    restarted. Workers exiting with a code in :data:`RETRYABLE_EXIT_CODES`
    are restarted up to ``max_restarts`` times within a sliding ``window_secs``
    window with exponential backoff and bounded jitter.

    A clean ``EXIT_OK`` exit is also restartable when ``restart_on_clean_exit``
    is true; by default, an explicit clean exit is honoured (no restart).
    """

    enabled: bool = True
    max_restarts: int = 5
    window_secs: float = 300.0
    base_backoff_secs: float = 1.0
    max_backoff_secs: float = 30.0
    jitter_ratio: float = 0.25
    restart_on_clean_exit: bool = False

    def should_restart(self, exit_code: int) -> bool:
        """Return ``True`` if ``exit_code`` is eligible for restart."""
        if not self.enabled:
            return False
        if exit_code in FATAL_EXIT_CODES:
            return False
        if exit_code == EXIT_OK:
            return self.restart_on_clean_exit
        return exit_code in RETRYABLE_EXIT_CODES

    def backoff_for(self, attempt: int) -> float:
        """Exponential backoff with bounded jitter for the n-th restart attempt.

        ``attempt`` is 1-based.
        """
        if attempt <= 0:
            return 0.0
        base = min(
            self.base_backoff_secs * (2 ** (attempt - 1)),
            self.max_backoff_secs,
        )
        jitter = base * self.jitter_ratio
        return base + random.uniform(0, jitter)


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
    """Mutable per-tunnel state owned by the supervisor.

    The supervisor recreates the subprocess on every restart attempt; the
    fields ``proc``, ``stdout_task``, ``stderr_task``, ``psutil_proc`` are
    refreshed on each respawn while ``restart_count`` and ``restart_window``
    persist across the entire ``_supervise_child`` lifetime.
    """

    name: str
    proc: asyncio.subprocess.Process
    stdout_task: asyncio.Task[None]
    stderr_task: asyncio.Task[None]
    exit_code: int | None = None
    psutil_proc: psutil.Process | None = None
    """Underlying :class:`psutil.Process` handle for introspection."""

    restart_count: int = 0
    """Number of restart attempts within the current window."""

    restart_window_start: float = field(default_factory=time.monotonic)
    """Monotonic timestamp at which the current restart window opened."""

    last_auth_failure_at: float | None = None
    """Monotonic timestamp of the most recent ``EXIT_AUTH_FAILURE``, if any."""


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


TunnelFileLoader = "Callable[[Path], TunnelFile] | None"


class Supervisor:
    """Process supervisor — runs one worker subprocess per tunnel.

    Optional dependencies (all parent-only, never wired into workers):

    * ``restart_policy``    — :class:`RestartPolicy` controlling per-worker
      respawn behaviour. ``None`` disables restarts (legacy behaviour).
    * ``remote_client``     — :class:`RemoteConfigClient` used to fetch a
      fresh tunnel config when a worker exits with ``EXIT_AUTH_FAILURE``.
    * ``health_watcher``    — :class:`ProviderHealthWatcher`. When the
      provider reports ``down``, new spawns and respawns are paused until
      it recovers; existing workers are NOT killed.
    * ``tunnel_file_loader``— Optional override that re-parses the on-disk
      config after a remote-config refresh. Defaults to a fresh
      :func:`load_config_file` + :meth:`TunnelFile.model_validate`.
    """

    __slots__ = (
        "_config_path",
        "_entries",
        "_cli_overrides",
        "_slots",
        "_children",
        "_result_codes",
        "_shutdown",
        "_sampler",
        "_restart_policy",
        "_remote_client",
        "_health_watcher",
        "_tunnel_file_loader",
        "_auth_refresh_lock",
        "_tunnel_file",
    )

    def __init__(
        self,
        *,
        config_path: Path,
        entries: list[TunnelEntry],
        cli_overrides: CLIOverrides,
        slots: dict[str, TunnelSlot],
        restart_policy: RestartPolicy | None = None,
        remote_client: RemoteConfigClient | None = None,
        health_watcher: ProviderHealthWatcher | None = None,
        tunnel_file: TunnelFile | None = None,
        tunnel_file_loader: object | None = None,
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
        self._restart_policy: RestartPolicy | None = restart_policy
        self._remote_client: RemoteConfigClient | None = remote_client
        self._health_watcher: ProviderHealthWatcher | None = health_watcher
        # ``tunnel_file_loader`` is intentionally typed as ``object`` to keep
        # the optional dependency out of the type-checking import graph;
        # callers pass a ``Callable[[Path], TunnelFile]`` or ``None``.
        self._tunnel_file_loader: object | None = tunnel_file_loader
        self._auth_refresh_lock: asyncio.Lock = asyncio.Lock()
        self._tunnel_file: TunnelFile | None = tunnel_file

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> SupervisorResult:
        """Spawn workers, ingest IPC frames, and supervise restarts."""
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

        if self._health_watcher is not None:
            with contextlib.suppress(Exception):
                await self._health_watcher.start()

        self._sampler.start()

        try:
            shutdown_task = asyncio.create_task(
                self._shutdown.wait(),
                name="supervisor-shutdown",
            )
            child_tasks: list[asyncio.Task[None]] = [
                asyncio.create_task(
                    self._supervise_child(entry),
                    name=f"supervise-{entry.name}",
                )
                for entry in self._entries
            ]

            try:
                # Wait for either every child supervision loop to finish
                # (workers either succeeded, failed terminally, or exhausted
                # their restart budget) or for shutdown to be requested.
                while child_tasks:
                    done, _pending = await asyncio.wait(
                        {shutdown_task, *child_tasks},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if shutdown_task in done:
                        await self._terminate_all()
                        for task in child_tasks:
                            if not task.done():
                                with contextlib.suppress(Exception):
                                    await task
                        break

                    for task in list(done):
                        if task is shutdown_task:
                            continue
                        with contextlib.suppress(Exception):
                            await task

                    child_tasks = [t for t in child_tasks if not t.done()]

            finally:
                if not shutdown_task.done():
                    shutdown_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await shutdown_task

            return self._build_result()

        finally:
            await self._sampler.stop()
            if self._health_watcher is not None:
                with contextlib.suppress(Exception):
                    await self._health_watcher.stop()
            for sig in installed:
                with contextlib.suppress(Exception):
                    loop.remove_signal_handler(sig)

    # ------------------------------------------------------------------
    # Per-tunnel supervision loop — replaces the one-shot _await_child
    # ------------------------------------------------------------------

    async def _supervise_child(self, entry: TunnelEntry) -> None:
        """Spawn ``entry``'s worker, await it, restart per policy, repeat."""
        policy = self._restart_policy

        while not self._shutdown.is_set():
            # ── Provider gate: wait until the watcher reports anything but
            # ``down`` before (re)spawning. Don't spin if shutdown beats us.
            if not await self._wait_until_unblocked(entry.name):
                return

            try:
                child = await self._spawn_child(entry)
            except Exception as exc:
                logger.error("[%s] failed to spawn worker: %s", entry.name, exc)
                self._result_codes[entry.name] = EXIT_ERROR

                slot = self._slots.get(entry.name)
                if slot is not None:
                    slot.status = "failed"
                    if slot.monitor is not None:
                        slot.monitor.set_connected(False)
                # Spawn-time failure is a configuration problem, not a
                # transient — only restart if the policy is enabled AND
                # we haven't blown the budget yet.
                if policy is None or not self._budget_allows(entry.name, policy):
                    return
                child_state_for_backoff = self._children.get(entry.name)
                attempt = (
                    child_state_for_backoff.restart_count + 1
                    if child_state_for_backoff is not None
                    else 1
                )
                await self._sleep_for_backoff(policy, attempt)
                continue

            self._children[entry.name] = child

            await self._await_child_exit(child)

            exit_code = child.exit_code if child.exit_code is not None else EXIT_ERROR
            self._result_codes[entry.name] = exit_code

            # ── Auth-failure refresh hook (parent-only) ──────────────────
            if exit_code == EXIT_AUTH_FAILURE and self._remote_client is not None:
                child.last_auth_failure_at = time.monotonic()
                refreshed = await self._handle_auth_failure_refresh(entry.name)
                if not refreshed:
                    logger.warning(
                        "[%s] remote-config refresh failed; not restarting",
                        entry.name,
                    )
                    return

            if self._shutdown.is_set():
                return

            if policy is None or not policy.should_restart(exit_code):
                return

            # ── Slide / increment the restart window ─────────────────────
            now = time.monotonic()
            if now - child.restart_window_start >= policy.window_secs:
                child.restart_window_start = now
                child.restart_count = 0

            if child.restart_count >= policy.max_restarts:
                logger.error(
                    "[%s] restart budget exhausted (%d/%d in %.1fs)",
                    entry.name,
                    child.restart_count,
                    policy.max_restarts,
                    policy.window_secs,
                )
                slot = self._slots.get(entry.name)
                if slot is not None and slot.status not in {"failed", "stopped"}:
                    slot.status = "failed"
                return

            child.restart_count += 1

            slot = self._slots.get(entry.name)
            if slot is not None:
                slot.status = "restarting"

            backoff = policy.backoff_for(child.restart_count)
            logger.warning(
                "[%s] worker exited code=%d — restart %d/%d in %.1fs",
                entry.name,
                exit_code,
                child.restart_count,
                policy.max_restarts,
                backoff,
            )
            await self._sleep_for_backoff(policy, child.restart_count, backoff)

    async def _wait_until_unblocked(self, name: str) -> bool:
        """Return ``True`` when the provider gate is open (or no watcher).

        Returns ``False`` if shutdown was requested while waiting.
        """
        watcher = self._health_watcher
        if watcher is None:
            return not self._shutdown.is_set()

        if not watcher.is_blocking():
            return not self._shutdown.is_set()

        slot = self._slots.get(name)
        if slot is not None:
            slot.status = "gated"

        logger.info(
            "[%s] gate held closed by provider health; waiting for recovery",
            name,
        )

        gate_task = asyncio.create_task(
            watcher.wait_until_unblocked(),
            name=f"gate-{name}",
        )
        shutdown_task = asyncio.create_task(
            self._shutdown.wait(),
            name=f"gate-shutdown-{name}",
        )
        try:
            done, _pending = await asyncio.wait(
                {gate_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (gate_task, shutdown_task):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

        return shutdown_task not in done

    @staticmethod
    def _budget_allows(name: str, policy: RestartPolicy) -> bool:
        # Conservative: allow at least one retry on spawn-time failure
        # because we have no _ChildState to consult yet.
        return policy.enabled and policy.max_restarts >= 1

    async def _sleep_for_backoff(
        self,
        policy: RestartPolicy,
        attempt: int,
        precomputed: float | None = None,
    ) -> None:
        delay = precomputed if precomputed is not None else policy.backoff_for(attempt)
        if delay <= 0:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._shutdown.wait(), timeout=delay)

    # ------------------------------------------------------------------
    # Auth-failure refresh — parent-only, exclusive across children
    # ------------------------------------------------------------------

    async def _handle_auth_failure_refresh(self, name: str) -> bool:
        """Fetch a fresh remote config and rewrite ``self._tunnel_file``.

        Returns ``True`` if the refresh succeeded, ``False`` otherwise.
        Concurrent auth failures across children are coalesced by
        ``self._auth_refresh_lock`` so only one HTTP fetch is in flight.
        """
        client = self._remote_client
        if client is None:
            return False

        async with self._auth_refresh_lock:
            try:
                body = await client.fetch_config()
            except Exception as exc:
                logger.warning("[%s] remote-config refresh failed: %s", name, exc)
                return False

            from .._remote_config import (  # noqa: PLC0415
                default_cache_path_for,
                write_cache_atomically,
            )

            try:
                cache_path = default_cache_path_for(client.source)
                write_cache_atomically(cache_path, body)
                logger.info(
                    "[%s] remote-config refreshed and cached at %s",
                    name,
                    cache_path,
                )
            except OSError as exc:
                logger.warning(
                    "[%s] remote-config write failed: %s — continuing in-memory",
                    name,
                    exc,
                )

            # Reload the on-disk file so any subsequent respawns see the new
            # config. We do this even if the on-disk write failed because the
            # parent's resolved ``self._tunnel_file`` should reflect the new
            # state regardless.
            try:
                self._tunnel_file = self._reload_tunnel_file()
            except Exception as exc:
                logger.warning(
                    "[%s] failed to reload TunnelFile after refresh: %s",
                    name,
                    exc,
                )
                return False

            # Update the per-tunnel entry slice so a respawn picks up
            # potentially-rotated wss_url / headers.
            try:
                self._refresh_entries_from_tunnel_file()
            except Exception as exc:
                logger.debug(
                    "[%s] entry refresh after reload had a non-fatal error: %s",
                    name,
                    exc,
                )

            return True

    def _reload_tunnel_file(self) -> TunnelFile:
        from exectunnel.config import (  # noqa: PLC0415
            TunnelFile,
            load_config_file,
        )

        loader = self._tunnel_file_loader
        if callable(loader):
            return loader(self._config_path)  # type: ignore[no-any-return]

        raw = load_config_file(self._config_path)
        return TunnelFile.model_validate(raw)

    def _refresh_entries_from_tunnel_file(self) -> None:
        if self._tunnel_file is None:
            return
        existing_names = {entry.name for entry in self._entries}
        refreshed: list[TunnelEntry] = []
        for entry in self._entries:
            try:
                fresh = self._tunnel_file.get_tunnel(entry.name)
            except KeyError:
                # Entry vanished from the new config — keep the old one;
                # the next respawn will surface the configuration error.
                refreshed.append(entry)
                continue
            refreshed.append(fresh)
            slot = self._slots.get(entry.name)
            if slot is not None and fresh.wss_url is not None:
                slot.wss_url = str(fresh.wss_url)
        self._entries = refreshed
        # Defensive: don't let stray new tunnels in the refreshed file leak
        # through; supervision is per-name from the original entry list.
        _ = existing_names

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

        if isinstance(frame, AuthFailureFrame):
            if slot is not None:
                slot.status = "auth_failed"
                if slot.monitor is not None:
                    slot.monitor.set_connected(False)
            logger.warning(
                "[%s] auth failure HTTP %d on %s — %s",
                tunnel,
                frame.http_status,
                frame.wss_url or "<unknown>",
                frame.message or "",
            )
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

    async def _await_child_exit(self, child: _ChildState) -> None:
        """Block until ``child.proc`` exits and reader streams drain.

        The supervise loop owns slot-status transitions across restarts; this
        method only finalises per-process bookkeeping and toggles the
        connectedness flag on the shared monitor.
        """
        code = await child.proc.wait()
        if child.exit_code is None:
            child.exit_code = code

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
        if slot is not None and slot.monitor is not None:
            slot.monitor.set_connected(False)

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
                    code = EXIT_ERROR

            per_tunnel[name] = code

        if self._shutdown.is_set():
            return SupervisorResult(
                exit_code=EXIT_INTERRUPTED,
                per_tunnel_codes=per_tunnel,
            )

        worst = EXIT_OK
        for code in per_tunnel.values():
            worst = max(worst, code)

        return SupervisorResult(
            exit_code=worst,
            per_tunnel_codes=per_tunnel,
        )
