"""Worker subprocess entry point — runs exactly one tunnel.

The worker is invoked by the parent supervisor as::

    exectunnel _worker --config PATH --name TUNNEL

...with a single JSON object on stdin carrying the serialized
:class:`~exectunnel.config.CLIOverrides`.

The worker:

* loads the tunnel config and resolves the named entry,
* registers a metric listener that emits :class:`MetricFrame` lines on stdout,
* runs the :class:`~exectunnel.session.TunnelSession` via
  :class:`~exectunnel.cli._runner.SessionRunner`,
* emits :class:`StatusFrame`, :class:`HealthFrame`, and (on auth failure)
  :class:`AuthFailureFrame` lifecycle transitions, plus a final
  :class:`ExitFrame`,
* exits with a meaningful return code (see :mod:`exectunnel.cli._exit_codes`).

The worker is deliberately ignorant of every other tunnel: one process, one
event loop, one session, one failure domain.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

from exectunnel.config import CLIOverrides, TunnelFile, load_config_file
from exectunnel.observability import MetricEvent

from .._exit_codes import EXIT_ERROR, EXIT_INTERRUPTED
from ..runner import (
    LifecycleEvent,
    SessionRunner,
)
from ..runner import (
    exception_exit_code as _runner_exception_exit_code,
)
from ..runner import (
    log_session_exception as _runner_log_session_exception,
)
from .ipc import (
    AuthFailureFrame,
    ExitFrame,
    Frame,
    HealthFrame,
    LogFrame,
    MetricFrame,
    StatusFrame,
    encode_frame,
)

__all__ = ["exception_exit_code", "log_session_exception", "run_worker"]

logger = logging.getLogger("exectunnel.cli.worker")


# Re-exported for backward compatibility with the in-process command path
# (``cli._commands.run`` imports both names from this module).
exception_exit_code = _runner_exception_exit_code
log_session_exception = _runner_log_session_exception


_STDIN_READ_TIMEOUT_SECS: Final[float] = 30.0

_VALID_LOG_LEVELS: Final[frozenset[str]] = frozenset({
    "DEBUG",
    "INFO",
    "WARNING",
    "ERROR",
    "CRITICAL",
})

# stdout writes may happen from both the asyncio loop and metric-listener
# callbacks dispatched from worker threads. A single lock guarantees that
# NDJSON lines are never interleaved.
_STDOUT_LOCK: Final[threading.Lock] = threading.Lock()

_METRIC_QUEUE_CAP: Final[int] = 8192
_METRIC_BATCH_MAX: Final[int] = 256
_METRIC_BATCH_TIMEOUT_SECS: Final[float] = 0.05


# ---------------------------------------------------------------------------
# Frame emission
# ---------------------------------------------------------------------------


def _emit(frame: Frame) -> None:
    """Write one NDJSON frame to stdout atomically."""
    _emit_many([frame])


def _emit_many(frames: list[Frame]) -> None:
    """Write one or more NDJSON frames to stdout atomically."""
    with _STDOUT_LOCK:
        try:
            for frame in frames:
                sys.stdout.buffer.write(encode_frame(frame))
            sys.stdout.buffer.flush()
        except (BrokenPipeError, ValueError):
            # Parent went away — nothing useful to do here.
            pass


# ---------------------------------------------------------------------------
# Logging bridge — forward worker log records to the parent as LogFrames.
# ---------------------------------------------------------------------------


class _LogFrameHandler(logging.Handler):
    """Logging handler that forwards records to the supervisor as ``LogFrame``."""

    def __init__(self, tunnel: str, level: int = logging.DEBUG) -> None:
        super().__init__(level=level)
        self._tunnel = tunnel

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = record.levelname
            if level not in _VALID_LOG_LEVELS:
                level = "INFO"
            _emit(
                LogFrame(
                    tunnel=self._tunnel,
                    level=level,
                    logger=record.name,
                    message=record.getMessage(),
                )
            )
        except Exception:
            # Logging must never crash the worker.
            pass


@contextlib.contextmanager
def _install_log_bridge(tunnel: str) -> Iterator[None]:
    """Route worker logging to stdout IPC frames for the lifetime of the run."""
    handler = _LogFrameHandler(tunnel=tunnel, level=logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))

    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_level = root.level

    try:
        for existing in list(root.handlers):
            root.removeHandler(existing)

        root.addHandler(handler)
        if root.level > logging.DEBUG:
            root.setLevel(logging.DEBUG)

        yield
    finally:
        with contextlib.suppress(Exception):
            root.removeHandler(handler)
        with contextlib.suppress(Exception):
            handler.close()

        for existing in previous_handlers:
            with contextlib.suppress(Exception):
                root.addHandler(existing)

        with contextlib.suppress(Exception):
            root.setLevel(previous_level)


# ---------------------------------------------------------------------------
# Metric listener
# ---------------------------------------------------------------------------


class _MetricForwarder:
    """Bounded async metric forwarder for worker → supervisor IPC.

    Metric listeners are synchronous and can be called from hot tunnel paths.
    This class makes listener work O(1), bounded, and non-blocking by enqueueing
    frames onto the event loop and batching stdout writes in a background task.
    """

    __slots__ = ("_tunnel", "_loop", "_queue", "_task", "_closed", "_drops")

    def __init__(self, tunnel: str, loop: asyncio.AbstractEventLoop) -> None:
        self._tunnel = tunnel
        self._loop = loop
        self._queue: asyncio.Queue[MetricFrame] = asyncio.Queue(_METRIC_QUEUE_CAP)
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._drops = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name=f"metric-forwarder-{self._tunnel}",
            )

    async def stop(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        await self._flush_remaining()

        if self._drops:
            _emit(
                LogFrame(
                    tunnel=self._tunnel,
                    level="WARNING",
                    logger=logger.name,
                    message=f"worker metric IPC dropped {self._drops} event(s)",
                )
            )

    def listener(self, event: MetricEvent) -> None:
        try:
            tags = {str(k): str(v) for k, v in event.tags.items()}
            frame = MetricFrame(
                tunnel=self._tunnel,
                name=event.name,
                kind=event.kind.value,
                value=float(event.value),
                operation=event.operation,
                current_value=event.current_value,
                tags=tags,
            )
            self._loop.call_soon_threadsafe(self._enqueue, frame)
        except Exception:
            # Never let observability crash the data path.
            pass

    def _enqueue(self, frame: MetricFrame) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self._drops += 1

    async def _run(self) -> None:
        while True:
            first = await self._queue.get()
            batch: list[Frame] = [first]

            deadline = self._loop.time() + _METRIC_BATCH_TIMEOUT_SECS
            while len(batch) < _METRIC_BATCH_MAX:
                timeout = deadline - self._loop.time()
                if timeout <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                except TimeoutError:
                    break
                batch.append(item)

            await asyncio.to_thread(_emit_many, batch)

    async def _flush_remaining(self) -> None:
        batch: list[Frame] = []
        while len(batch) < _METRIC_BATCH_MAX:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if batch:
            await asyncio.to_thread(_emit_many, batch)


# ---------------------------------------------------------------------------
# Worker-side SessionObserver — drives lifecycle frames on the wire
# ---------------------------------------------------------------------------


class _WorkerObserver:
    """Translate :class:`SessionRunner` lifecycle into NDJSON frames.

    Emits:

    * :class:`StatusFrame` for every lifecycle transition so the parent's
      slot status mirrors the worker's actual state without having to wait
      for the next discriminating metric;
    * :class:`HealthFrame` on ``running`` / ``bootstrap_ok`` / ``stopped`` /
      ``failed`` / ``auth_failed`` so the parent can update connectedness
      flags eagerly;
    * :class:`AuthFailureFrame` on ``auth_failed`` carrying the HTTP status
      and the exception message so the dashboard can render the failure
      reason before the worker even exits.
    """

    __slots__ = ("_tunnel", "_metric_forwarder", "_wss_url")

    def __init__(
        self,
        tunnel: str,
        metric_forwarder: _MetricForwarder,
        wss_url: str,
    ) -> None:
        self._tunnel = tunnel
        self._metric_forwarder = metric_forwarder
        self._wss_url = wss_url

    # SessionObserver protocol
    def metric_listener(self) -> Callable[[MetricEvent], None] | None:
        return self._metric_forwarder.listener

    async def on_lifecycle(self, event: LifecycleEvent) -> None:
        kind = event.kind

        if kind == "starting":
            _emit(StatusFrame(tunnel=self._tunnel, status="starting"))
            _emit(HealthFrame(tunnel=self._tunnel, connected=False))
            return

        if kind == "bootstrap_ok":
            _emit(
                HealthFrame(
                    tunnel=self._tunnel,
                    connected=True,
                    socks_ok=True,
                    bootstrap_ok=True,
                )
            )
            return

        if kind == "running":
            _emit(StatusFrame(tunnel=self._tunnel, status="running"))
            return

        if kind == "stopped":
            _emit(StatusFrame(tunnel=self._tunnel, status="stopped"))
            _emit(HealthFrame(tunnel=self._tunnel, connected=False))
            return

        if kind == "failed":
            _emit(StatusFrame(tunnel=self._tunnel, status="failed"))
            _emit(HealthFrame(tunnel=self._tunnel, connected=False))
            return

        if kind == "auth_failed":
            http_status = event.http_status if event.http_status is not None else 401
            _emit(
                AuthFailureFrame(
                    tunnel=self._tunnel,
                    http_status=http_status,
                    wss_url=self._wss_url,
                    message=event.message or "",
                )
            )
            _emit(StatusFrame(tunnel=self._tunnel, status="auth_failed"))
            _emit(HealthFrame(tunnel=self._tunnel, connected=False))
            return


# ---------------------------------------------------------------------------
# Stdin parsing — overrides come as a single JSON object.
# ---------------------------------------------------------------------------


async def _read_overrides_from_stdin_async(
    timeout: float = _STDIN_READ_TIMEOUT_SECS,
) -> CLIOverrides:
    """Read CLIOverrides JSON from stdin with a bounded timeout.

    The parent supervisor writes the overrides immediately after spawn and
    closes its end. If the parent crashes between spawn and write, a
    blocking ``sys.stdin.read()`` would hang forever — we time out and
    surface a worker-level failure instead.
    """

    def _read() -> str:
        return sys.stdin.read()

    try:
        raw = await asyncio.wait_for(asyncio.to_thread(_read), timeout=timeout)
    except TimeoutError as exc:
        raise TimeoutError(
            f"worker stdin overrides not received within {timeout:.1f}s"
        ) from exc

    if not raw.strip():
        return CLIOverrides()

    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("worker stdin payload must be a JSON object")
    return CLIOverrides.model_validate(payload)


def _load_tunnel_file(config_path: Path) -> TunnelFile:
    raw = load_config_file(config_path)
    return TunnelFile.model_validate(raw)


# ---------------------------------------------------------------------------
# Core async run loop
# ---------------------------------------------------------------------------


async def _run_session(
    tunnel: str,
    config_path: Path,
) -> int:
    """Run exactly one tunnel session inside the worker process.

    Reads :class:`CLIOverrides` from stdin (bounded), loads the config,
    constructs a :class:`SessionRunner` with a :class:`_WorkerObserver`, and
    drives it to completion.
    """
    loop = asyncio.get_running_loop()

    # ── Read CLIOverrides with a bounded read so we never hang on a dead
    # parent. The worker hasn't installed signal handlers yet, so SIGINT
    # would still terminate us via the default handler.
    try:
        cli_overrides = await _read_overrides_from_stdin_async()
    except TimeoutError as exc:
        logger.error("worker stdin timeout: %s", exc)
        _emit(StatusFrame(tunnel=tunnel, status="failed"))
        return EXIT_ERROR
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("worker invalid stdin overrides payload: %s", exc)
        _emit(StatusFrame(tunnel=tunnel, status="failed"))
        return EXIT_ERROR

    try:
        tunnel_file = _load_tunnel_file(config_path)
    except Exception as exc:
        logger.error("worker failed to load config %s: %s", config_path, exc)
        _emit(StatusFrame(tunnel=tunnel, status="failed"))
        return EXIT_ERROR

    try:
        entry = tunnel_file.get_tunnel(tunnel)
    except KeyError as exc:
        logger.error("worker tunnel %r not found in config: %s", tunnel, exc)
        _emit(StatusFrame(tunnel=tunnel, status="failed"))
        return EXIT_ERROR

    # Resolve once (early) just to learn the WSS URL for AuthFailureFrame
    # correlation. The runner re-resolves and surfaces the same error if the
    # config is invalid; we tolerate failure here.
    wss_url = ""
    try:
        session_cfg, _tun_cfg = tunnel_file.resolve(entry, cli_overrides)
        wss_url = str(session_cfg.wss_url)
    except Exception:
        wss_url = str(entry.wss_url) if entry.wss_url else ""

    metric_forwarder = _MetricForwarder(tunnel, loop)
    metric_forwarder.start()

    observer = _WorkerObserver(
        tunnel=tunnel,
        metric_forwarder=metric_forwarder,
        wss_url=wss_url,
    )

    runner = SessionRunner(
        name=tunnel,
        tunnel_file=tunnel_file,
        entry=entry,
        cli_overrides=cli_overrides,
        observers=[observer],
        install_signals=True,
    )

    try:
        return await runner.run()
    finally:
        with contextlib.suppress(Exception):
            await metric_forwarder.stop()


# ---------------------------------------------------------------------------
# Public entry point — invoked from the hidden ``_worker`` CLI command.
# ---------------------------------------------------------------------------


def run_worker(*, config_path: Path, tunnel: str) -> int:
    """Run a single tunnel and return its exit code.

    Args:
        config_path: Path to the tunnel config file.
        tunnel:      Name of the tunnel entry to run.
    """
    code = EXIT_ERROR

    with _install_log_bridge(tunnel):
        try:
            code = asyncio.run(_run_session(tunnel, config_path))
        except KeyboardInterrupt:
            code = EXIT_INTERRUPTED
        except Exception as exc:
            logger.error("worker fatal error: %s", exc, exc_info=True)
            _emit(StatusFrame(tunnel=tunnel, status="failed"))
            code = EXIT_ERROR

    # Always emit a final ExitFrame so the parent has a deterministic signal,
    # even if the session bootstrap failed before SessionRunner had a chance.
    _emit(ExitFrame(tunnel=tunnel, code=code))
    return code
