"""Structured exception logging for the TCP copy tasks.

The :class:`~exectunnel.transport.tcp.TcpConnection` upstream and
downstream coroutines report their terminal exceptions through
:func:`log_task_exception`, which maps each exception class to the
appropriate metric counter, log level, and structured ``extra`` payload.

Keeping this dispatch out of ``tcp.py`` preserves that module's focus on
the connection state machine and makes the mapping trivially unit-testable.
"""

from __future__ import annotations

import logging
from typing import Final

from exectunnel.exceptions import (
    ConnectionClosedError,
    ExecTunnelError,
    TransportError,
    WebSocketSendTimeoutError,
)
from exectunnel.observability import metrics_inc

__all__ = ["log_task_exception"]

_log: Final[logging.Logger] = logging.getLogger("exectunnel.transport.tcp")


def log_task_exception(
    conn_id: str,
    direction: str,
    exc: BaseException,
    bytes_transferred: int,
) -> None:
    """Log a terminal copy-task exception with structured context and metrics.

    Dispatches on the concrete exception type to emit:

    * The correct metric counter label (``tcp.connection.{direction}.error``).
    * A log record at the appropriate level (``warning`` for tunnel /
      library errors, ``debug`` for expected socket errors).
    * Structured ``extra`` metadata including the connection ID,
      direction, transferred-byte count, and — where available — the
      ``error_code`` and ``error_id`` from
      :class:`~exectunnel.exceptions.ExecTunnelError`.

    Args:
        conn_id: Connection ID for log context.
        direction: Either ``"upstream"`` or ``"downstream"``.
        exc: The caught exception.
        bytes_transferred: Bytes transferred in this direction before the
            error occurred.
    """
    byte_key = "bytes_sent" if direction == "upstream" else "bytes_recv"
    base_extra: dict[str, object] = {
        "conn_id": conn_id,
        "direction": direction,
        byte_key: bytes_transferred,
    }

    match exc:
        case WebSocketSendTimeoutError():
            metrics_inc(f"tcp.connection.{direction}.error", error="ws_send_timeout")
            _log.warning(
                "conn %s: %s stalled — WebSocket send timed out [%s] "
                "(%s=%d, error_id=%s)",
                conn_id,
                direction,
                exc.error_code,
                byte_key,
                bytes_transferred,
                exc.error_id,
                extra={
                    **base_extra,
                    "error_code": exc.error_code,
                    "error_id": exc.error_id,
                },
            )

        case ConnectionClosedError():
            metrics_inc(f"tcp.connection.{direction}.error", error="connection_closed")
            _log.warning(
                "conn %s: %s ended — tunnel connection closed [%s] "
                "(%s=%d, error_id=%s)",
                conn_id,
                direction,
                exc.error_code,
                byte_key,
                bytes_transferred,
                exc.error_id,
                extra={
                    **base_extra,
                    "error_code": exc.error_code,
                    "error_id": exc.error_id,
                },
            )

        case TransportError():
            metrics_inc(
                f"tcp.connection.{direction}.error",
                error=exc.error_code.replace(".", "_"),
            )
            _log.warning(
                "conn %s: %s transport error [%s]: %s (%s=%d, error_id=%s)",
                conn_id,
                direction,
                exc.error_code,
                exc.message,
                byte_key,
                bytes_transferred,
                exc.error_id,
                extra={
                    **base_extra,
                    "error_code": exc.error_code,
                    "error_id": exc.error_id,
                },
            )

        case OSError():
            metrics_inc(f"tcp.connection.{direction}.error", error="os_error")
            _log.debug(
                "conn %s: %s socket error: %s",
                conn_id,
                direction,
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
                extra=base_extra,
            )

        case ExecTunnelError():
            metrics_inc(
                f"tcp.connection.{direction}.error",
                error=exc.error_code.replace(".", "_"),
            )
            _log.warning(
                "conn %s: %s library error [%s]: %s (error_id=%s)",
                conn_id,
                direction,
                exc.error_code,
                exc.message,
                exc.error_id,
                extra={
                    **base_extra,
                    "error_code": exc.error_code,
                    "error_id": exc.error_id,
                },
            )

        case _:
            metrics_inc(
                f"tcp.connection.{direction}.error",
                error=type(exc).__name__,
            )
            _log.warning(
                "conn %s: %s unexpected failure: %s",
                conn_id,
                direction,
                exc,
                extra=base_extra,
            )
            _log.debug(
                "conn %s: %s traceback",
                conn_id,
                direction,
                exc_info=(type(exc), exc, exc.__traceback__),
                extra={"conn_id": conn_id},
            )
