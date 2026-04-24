"""Structured exception dispatch for the SOCKS5 handshake path.

The handshake driver in :mod:`exectunnel.proxy.server` must terminate
every failure mode with the same four-step pattern: record a failure
reason metric, record a rejection metric, observe the handshake
duration, and close the writer. This module centralises that pattern so
``_do_handshake`` can be a flat dispatch instead of five near-identical
``except`` blocks.
"""

from __future__ import annotations

import logging
from typing import Final

from exectunnel.exceptions import ExecTunnelError, ProtocolError, TransportError

__all__ = ["classify_handshake_error"]

_log: Final[logging.Logger] = logging.getLogger("exectunnel.proxy.server")


def classify_handshake_error(exc: BaseException) -> tuple[str, int]:
    """Classify *exc* and emit the appropriate log line.

    Args:
        exc: The exception raised during the SOCKS5 handshake.

    Returns:
        A ``(reason, log_level)`` tuple. *reason* is the label used for
        the ``socks5.handshakes.error`` metric counter. *log_level* is
        the :mod:`logging` level used for the emitted record — retained
        for callers that wish to correlate the metric with structured
        logs.
    """
    match exc:
        case TimeoutError():
            return "timeout", logging.WARNING

        case ProtocolError():
            _log.debug(
                "socks5 handshake rejected [%s]: %s (error_id=%s)",
                exc.error_code,
                exc.message,
                exc.error_id,
            )
            return "protocol", logging.DEBUG

        case TransportError():
            _log.warning(
                "socks5 handshake transport error [%s]: %s (error_id=%s)",
                exc.error_code,
                exc.message,
                exc.error_id,
            )
            return "transport", logging.WARNING

        case ExecTunnelError():
            _log.warning(
                "socks5 handshake library error [%s]: %s (error_id=%s)",
                exc.error_code,
                exc.message,
                exc.error_id,
            )
            return "library", logging.WARNING

        case OSError():
            _log.debug("socks5 handshake I/O error: %s", exc)
            return "os_error", logging.DEBUG

        case _:
            _log.warning("socks5 handshake unexpected failure: %s", exc)
            _log.debug(
                "socks5 handshake traceback",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return "unexpected", logging.WARNING
