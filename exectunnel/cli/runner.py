"""``SessionRunner`` — single canonical lifecycle for one TunnelSession."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from exectunnel.exceptions import BootstrapError
from exectunnel.observability import (
    MetricEvent,
    register_metric_listener,
    unregister_metric_listener,
)
from exectunnel.session import TunnelSession

from ._exit_codes import (
    EXIT_AUTH_FAILURE,
    EXIT_BOOTSTRAP,
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_RECONNECT_EXHAUSTED,
)

if TYPE_CHECKING:
    from exectunnel.config import CLIOverrides, TunnelEntry, TunnelFile

__all__ = [
    "AUTH_FAILURE_HTTP_CODES",
    "LifecycleKind",
    "SessionObserver",
    "SessionRunner",
    "exception_exit_code",
    "log_session_exception",
]

logger = logging.getLogger("exectunnel.cli.runner")


LifecycleKind = Literal[
    "starting",
    "running",
    "bootstrap_ok",
    "stopped",
    "failed",
    "auth_failed",
]


#: HTTP status codes that the WSS handshake layer maps to "auth failure".
#: Sourced from ``exectunnel.session.session.py``.
AUTH_FAILURE_HTTP_CODES: Final[frozenset[int]] = frozenset({401, 403})


_SHUTDOWN_SIGNALS: Final[tuple[signal.Signals, ...]] = tuple(
    sig for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)) if sig is not None
)

_DRAIN_TIMEOUT_SECS: Final[float] = 10.0


def _is_auth_failure(exc: BaseException) -> bool:
    """Return ``True`` when ``exc`` is a WSS handshake auth/authz failure."""
    if not isinstance(exc, BootstrapError):
        return False
    status = exc.details.get("http_status") if isinstance(exc.details, dict) else None
    return isinstance(status, int) and status in AUTH_FAILURE_HTTP_CODES


def exception_exit_code(exc: BaseException) -> int:
    """Map a session exception to a worker / runner exit code.

    Special-cases :class:`BootstrapError` carrying ``http_status in {401, 403}``
    so the supervisor (and the hidden ``_worker`` exit code) can distinguish
    auth failures from other bootstrap errors and trigger an optional
    remote-config refresh.
    """
    if isinstance(exc, asyncio.CancelledError):
        return EXIT_INTERRUPTED
    if _is_auth_failure(exc):
        return EXIT_AUTH_FAILURE
    if isinstance(exc, BootstrapError):
        return EXIT_BOOTSTRAP

    # Avoid a hard import cycle with exectunnel.exceptions for the rare
    # ReconnectExhaustedError check; class-name fallback is safe because the
    # exception hierarchy is stable.
    if exc.__class__.__name__ == "ReconnectExhaustedError":
        return EXIT_RECONNECT_EXHAUSTED

    return EXIT_ERROR


def log_session_exception(name: str, exc: BaseException) -> None:
    """Emit a structured log record for a session failure."""
    from exectunnel.exceptions import ExecTunnelError  # noqa: PLC0415

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


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """Payload passed to :meth:`SessionObserver.on_lifecycle`.

    ``http_status`` is populated only when ``kind == "auth_failed"``.
    ``exit_code`` is populated for ``stopped``, ``failed``, and ``auth_failed``.
    ``exception`` is populated for ``failed`` and ``auth_failed``.
    """

    kind: LifecycleKind
    exit_code: int | None = None
    exception: BaseException | None = None
    http_status: int | None = None
    message: str = ""


class SessionObserver(Protocol):
    """Receives metric and lifecycle events from a :class:`SessionRunner`.

    Implementations MUST NOT raise. The runner wraps every callback in a
    ``contextlib.suppress(Exception)`` defensively but a misbehaving observer
    can still cause unbounded latency by blocking. Keep callbacks O(1).
    """

    def metric_listener(self) -> Callable[[MetricEvent], None] | None:
        """Return a synchronous metric listener, or ``None`` to skip.

        The runner registers this with the global metric registry for the
        duration of the session and unregisters it on teardown.
        """
        ...

    async def on_lifecycle(self, event: LifecycleEvent) -> None:
        """Best-effort lifecycle hook."""
        ...


class BootstrapDoneSignal:
    """Async event set when ``bootstrap.ok`` is observed.

    Wraps an :class:`asyncio.Event` and exposes a thread-safe ``set`` that the
    runner uses from the global metric callback (which fires from the
    observability worker thread).
    """

    __slots__ = ("_event", "_loop")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._event = asyncio.Event()
        self._loop = loop

    def listener(self) -> Callable[[MetricEvent], None]:
        def _on_metric(event: MetricEvent) -> None:
            if event.name == "bootstrap.ok":
                with contextlib.suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(self._event.set)

        return _on_metric

    async def wait(self) -> None:
        await self._event.wait()

    def is_set(self) -> bool:
        return self._event.is_set()


@dataclass(slots=True)
class SessionRunner:
    """Run exactly one :class:`TunnelSession` with shared lifecycle plumbing.

    Args:
        name:           Tunnel name (logging / observer correlation).
        tunnel_file:    Parsed :class:`TunnelFile`.
        entry:          Selected :class:`TunnelEntry` from ``tunnel_file``.
        cli_overrides:  CLI overrides resolved at the call site.
        observers:      :class:`SessionObserver` instances notified of lifecycle
                        and metric events.
        install_signals: When ``True`` (default), the runner installs SIGINT /
                        SIGTERM handlers and exits with ``EXIT_INTERRUPTED``
                        on receipt. Set to ``False`` when the caller already
                        owns signal handling (worker subprocess case).
    """

    name: str
    tunnel_file: TunnelFile
    entry: TunnelEntry
    cli_overrides: CLIOverrides
    observers: list[SessionObserver] = field(default_factory=list)
    install_signals: bool = True

    async def run(self) -> int:
        """Drive the session to completion and return its exit code."""
        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        installed_signals: list[signal.Signals] = []

        if self.install_signals:
            installed_signals = self._install_signals(loop, shutdown)

        try:
            return await self._run_inner(loop, shutdown)
        finally:
            for sig in installed_signals:
                with contextlib.suppress(Exception):
                    loop.remove_signal_handler(sig)

    def _install_signals(
        self,
        loop: asyncio.AbstractEventLoop,
        shutdown: asyncio.Event,
    ) -> list[signal.Signals]:
        installed: list[signal.Signals] = []

        def _on_signal(sig: signal.Signals) -> None:
            logger.info("[%s] received %s — shutting down", self.name, sig.name)
            shutdown.set()

        for sig in _SHUTDOWN_SIGNALS:
            try:
                loop.add_signal_handler(sig, _on_signal, sig)
            except (NotImplementedError, RuntimeError, ValueError):
                logger.debug(
                    "[%s] signal handler not supported for %s", self.name, sig.name
                )
            else:
                installed.append(sig)

        return installed

    async def _emit_lifecycle(self, event: LifecycleEvent) -> None:
        for observer in self.observers:
            try:
                await observer.on_lifecycle(event)
            except Exception:
                logger.debug(
                    "[%s] observer.on_lifecycle raised; ignored",
                    self.name,
                    exc_info=True,
                )

    @contextlib.contextmanager
    def _registered_listeners(
        self,
        listeners: Iterable[Callable[[MetricEvent], None]],
    ) -> Iterator[None]:
        registered: list[Callable[[MetricEvent], None]] = []
        try:
            for listener in listeners:
                register_metric_listener(listener)
                registered.append(listener)
            yield
        finally:
            for listener in registered:
                with contextlib.suppress(Exception):
                    unregister_metric_listener(listener)

    async def _run_inner(
        self,
        loop: asyncio.AbstractEventLoop,
        shutdown: asyncio.Event,
    ) -> int:
        # ── Resolve configuration & build the session ────────────────────
        try:
            session_cfg, tun_cfg = self.tunnel_file.resolve(
                self.entry, self.cli_overrides
            )
            session = TunnelSession(session_cfg, tun_cfg)
        except Exception as exc:
            logger.error("[%s] config/session setup failed: %s", self.name, exc)
            await self._emit_lifecycle(
                LifecycleEvent(kind="failed", exit_code=EXIT_ERROR, exception=exc)
            )
            return EXIT_ERROR

        # ── Collect observer listeners + an internal bootstrap sentinel ──
        bootstrap_signal = BootstrapDoneSignal(loop)
        listeners: list[Callable[[MetricEvent], None]] = [bootstrap_signal.listener()]
        for observer in self.observers:
            listener = observer.metric_listener()
            if listener is not None:
                listeners.append(listener)

        await self._emit_lifecycle(LifecycleEvent(kind="starting"))

        session_task = asyncio.create_task(
            session.run(),
            name=f"session-{self.name}",
        )
        shutdown_task = asyncio.create_task(
            shutdown.wait(),
            name=f"shutdown-{self.name}",
        )
        bootstrap_task = asyncio.create_task(
            bootstrap_signal.wait(),
            name=f"bootstrap-{self.name}",
        )

        bootstrap_signalled = False

        try:
            with self._registered_listeners(listeners):
                # ── Phase 1: race session vs shutdown vs bootstrap.ok ────
                done, _pending = await asyncio.wait(
                    {session_task, shutdown_task, bootstrap_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if shutdown_task in done and not session_task.done():
                    return await self._handle_shutdown(session_task)

                if bootstrap_task in done and not session_task.done():
                    bootstrap_signalled = True
                    await self._emit_lifecycle(LifecycleEvent(kind="bootstrap_ok"))
                    await self._emit_lifecycle(LifecycleEvent(kind="running"))

                    # ── Phase 2: serve until session ends or shutdown ────
                    done, _pending = await asyncio.wait(
                        {session_task, shutdown_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if shutdown_task in done and not session_task.done():
                        return await self._handle_shutdown(session_task)

                # ── Session task is done either way at this point ────────
                return await self._handle_session_completion(session_task)

        finally:
            for task in (bootstrap_task, shutdown_task):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

            if not session_task.done():
                session_task.cancel()
                await self._drain(session_task)

            # Avoid "unused" warning when bootstrap never fired.
            _ = bootstrap_signalled

    async def _handle_shutdown(self, session_task: asyncio.Task[None]) -> int:
        session_task.cancel()
        await self._drain(session_task)
        await self._emit_lifecycle(
            LifecycleEvent(kind="stopped", exit_code=EXIT_INTERRUPTED)
        )
        return EXIT_INTERRUPTED

    async def _handle_session_completion(
        self,
        session_task: asyncio.Task[None],
    ) -> int:
        if session_task.cancelled():
            await self._emit_lifecycle(
                LifecycleEvent(kind="stopped", exit_code=EXIT_INTERRUPTED)
            )
            return EXIT_INTERRUPTED

        exc = session_task.exception()
        if exc is None:
            await self._emit_lifecycle(
                LifecycleEvent(kind="stopped", exit_code=EXIT_OK)
            )
            return EXIT_OK

        log_session_exception(self.name, exc)
        code = exception_exit_code(exc)

        if code == EXIT_AUTH_FAILURE and isinstance(exc, BootstrapError):
            details: dict[str, Any] = (
                exc.details if isinstance(exc.details, dict) else {}
            )
            http_status = details.get("http_status")
            await self._emit_lifecycle(
                LifecycleEvent(
                    kind="auth_failed",
                    exit_code=code,
                    exception=exc,
                    http_status=(http_status if isinstance(http_status, int) else None),
                    message=str(exc.message or ""),
                )
            )
            return code

        await self._emit_lifecycle(
            LifecycleEvent(kind="failed", exit_code=code, exception=exc)
        )
        return code

    @staticmethod
    async def _drain(task: asyncio.Task[Any]) -> None:
        if task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            return

        try:
            async with asyncio.timeout(_DRAIN_TIMEOUT_SECS):
                await task
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("ignored task drain exception", exc_info=True)
