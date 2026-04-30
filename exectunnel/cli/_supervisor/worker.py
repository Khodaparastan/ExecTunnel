"""Worker subprocess entry point — runs exactly one tunnel.

The worker is invoked by the parent supervisor as::

    exectunnel _worker --config PATH --name TUNNEL

...with a single JSON object on stdin carrying the serialized
:class:`~exectunnel.config.CLIOverrides`.

The worker:

* loads the tunnel config and resolves the named entry,
* registers a metric listener that emits :class:`MetricFrame` lines on stdout,
* runs the :class:`~exectunnel.session.TunnelSession`,
* emits :class:`StatusFrame` lifecycle transitions and a final
  :class:`ExitFrame`,
* exits with a meaningful return code.

The worker is deliberately ignorant of every other tunnel: one process, one
event loop, one session, one failure domain.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

from exectunnel.config import CLIOverrides, TunnelFile, load_config_file
from exectunnel.exceptions import (
    BootstrapError,
    ExecTunnelError,
    ReconnectExhaustedError,
)
from exectunnel.observability import (
    MetricEvent,
    register_metric_listener,
    unregister_metric_listener,
)
from exectunnel.session import TunnelSession

from .ipc import (
    ExitFrame,
    Frame,
    LogFrame,
    MetricFrame,
    StatusFrame,
    encode_frame,
)

__all__ = ["run_worker"]

logger = logging.getLogger("exectunnel.cli.worker")

_EXIT_OK: Final[int] = 0
_EXIT_ERROR: Final[int] = 1
_EXIT_BOOTSTRAP: Final[int] = 2
_EXIT_RECONNECT_EXHAUSTED: Final[int] = 3
_EXIT_INTERRUPTED: Final[int] = 130

_SHUTDOWN_SIGNALS: Final[tuple[signal.Signals, ...]] = tuple(
    sig for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)) if sig is not None
)

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
# Exit-code mapping
# ---------------------------------------------------------------------------


def exception_exit_code(exc: BaseException) -> int:
    """Map a session exception to the worker process exit code."""
    if isinstance(exc, asyncio.CancelledError):
        return _EXIT_INTERRUPTED
    if isinstance(exc, BootstrapError):
        return _EXIT_BOOTSTRAP
    if isinstance(exc, ReconnectExhaustedError):
        return _EXIT_RECONNECT_EXHAUSTED
    return _EXIT_ERROR


def log_session_exception(name: str, exc: BaseException) -> None:
    """Emit a structured log record for a session failure."""
    if isinstance(exc, ExecTunnelError):
        logger.error(
            "[%s] tunnel error [%s]: %s (error_id=%s)",
            name,
            exc.error_code,
            exc.message,
            exc.error_id,
        )
        if exc.hint:
            logger.info("[%s] hint: %s", name, exc.hint)
        return

    if isinstance(exc, asyncio.CancelledError):
        logger.debug("[%s] session cancelled", name)
        return

    logger.error(
        "[%s] unexpected error: %s",
        name,
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )


# ---------------------------------------------------------------------------
# Stdin parsing — overrides come as a single JSON object.
# ---------------------------------------------------------------------------


def _read_overrides_from_stdin() -> CLIOverrides:
    raw = sys.stdin.read()
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
    tunnel_file: TunnelFile,
    cli_overrides: CLIOverrides,
) -> int:
    """Run exactly one tunnel session inside the worker process."""
    try:
        entry = tunnel_file.get_tunnel(tunnel)
    except KeyError as exc:
        logger.error("worker tunnel %r not found in config: %s", tunnel, exc)
        _emit(StatusFrame(tunnel=tunnel, status="failed"))
        return _EXIT_ERROR

    _emit(StatusFrame(tunnel=tunnel, status="starting"))

    try:
        session_cfg, tun_cfg = tunnel_file.resolve(entry, cli_overrides)
        session = TunnelSession(session_cfg, tun_cfg)
    except Exception as exc:
        logger.error("[%s] config/session setup failed: %s", tunnel, exc)
        _emit(StatusFrame(tunnel=tunnel, status="failed"))
        return _EXIT_ERROR

    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()

    def _on_signal(sig: signal.Signals) -> None:
        logger.info("[%s] received %s — shutting down", tunnel, sig.name)
        shutdown.set()

    installed: list[signal.Signals] = []
    for sig in _SHUTDOWN_SIGNALS:
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (NotImplementedError, RuntimeError, ValueError):
            logger.debug("signal handler not supported for %s", sig.name)
        else:
            installed.append(sig)

    metric_forwarder = _MetricForwarder(tunnel, loop)
    metric_forwarder.start()
    listener = metric_forwarder.listener
    register_metric_listener(listener)

    session_task = asyncio.create_task(session.run(), name=f"session-{tunnel}")
    shutdown_task = asyncio.create_task(shutdown.wait(), name=f"shutdown-{tunnel}")

    try:
        done, pending = await asyncio.wait(
            {session_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if shutdown_task in done and not session_task.done():
            session_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await session_task

            _emit(StatusFrame(tunnel=tunnel, status="stopped"))
            return _EXIT_INTERRUPTED

        if session_task in done:
            if not shutdown_task.done():
                shutdown_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await shutdown_task

            if session_task.cancelled():
                _emit(StatusFrame(tunnel=tunnel, status="stopped"))
                return _EXIT_INTERRUPTED

            exc = session_task.exception()
            if exc is None:
                _emit(StatusFrame(tunnel=tunnel, status="stopped"))
                return _EXIT_OK

            log_session_exception(tunnel, exc)
            _emit(StatusFrame(tunnel=tunnel, status="failed"))
            return exception_exit_code(exc)

        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        _emit(StatusFrame(tunnel=tunnel, status="failed"))
        return _EXIT_ERROR

    finally:
        with contextlib.suppress(Exception):
            unregister_metric_listener(listener)

        with contextlib.suppress(Exception):
            await metric_forwarder.stop()

        for sig in installed:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)


# ---------------------------------------------------------------------------
# Public entry point — invoked from the hidden ``_worker`` CLI command.
# ---------------------------------------------------------------------------


def run_worker(*, config_path: Path, tunnel: str) -> int:
    """Run a single tunnel and return its exit code.

    Args:
        config_path: Path to the tunnel config file.
        tunnel:      Name of the tunnel entry to run.
    """
    code = _EXIT_ERROR

    with _install_log_bridge(tunnel):
        try:
            cli_overrides = _read_overrides_from_stdin()
            tunnel_file = _load_tunnel_file(config_path)
            code = asyncio.run(_run_session(tunnel, tunnel_file, cli_overrides))
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("worker invalid stdin overrides payload: %s", exc)
            _emit(StatusFrame(tunnel=tunnel, status="failed"))
            code = _EXIT_ERROR
        except Exception as exc:
            logger.error("worker failed to load config %s: %s", config_path, exc)
            _emit(StatusFrame(tunnel=tunnel, status="failed"))
            code = _EXIT_ERROR

    # If config/load/session bootstrap failed before asyncio.run() even started,
    # we still owe the parent a final exit frame. Emit it after the logging
    # bridge scope so the frame is guaranteed to be last on stdout.
    _emit(ExitFrame(tunnel=tunnel, code=code))
    return code
