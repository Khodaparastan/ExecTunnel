"""Input validation utilities for the ``exectunnel.transport`` package."""

from __future__ import annotations

from exectunnel.exceptions import TransportError

__all__ = ["require_bytes"]


def require_bytes(value: object, handler_id: str, method: str) -> bytes:
    """Validate that *value* is a ``bytes`` instance.

    Used by both :class:`~exectunnel.transport.tcp.TcpConnection` and
    :class:`~exectunnel.transport.udp.UdpFlow` before passing any payload to
    the protocol layer encoders. A non-``bytes`` value at this boundary is a
    programming error in the caller, not a wire error, so
    :class:`~exectunnel.exceptions.TransportError` is raised rather than
    :class:`~exectunnel.exceptions.FrameDecodingError`.

    Args:
        value: The value to validate.
        handler_id: The connection or flow ID included in the error context.
        method: The calling method name included in the error context.

    Returns:
        *value* unchanged, typed as ``bytes``.

    Raises:
        TransportError: If *value* is not a ``bytes`` instance.
            ``error_code`` is ``"transport.invalid_payload_type"``.
    """
    if isinstance(value, bytes):
        return value
    raise TransportError(
        f"{handler_id!r}: {method}() requires a bytes payload; "
        f"got {type(value).__name__!r}.",
        error_code="transport.invalid_payload_type",
        details={
            "handler_id": handler_id,
            "method": method,
            "received_type": type(value).__name__,
        },
        hint=f"Pass raw bytes to {method}(). Convert explicitly with bytes(value) if needed.",
    )
