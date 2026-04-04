"""
Input validation utilities for the ``exectunnel.transport`` package.

All helpers in this module are:

* **Pure** — no I/O, no asyncio, no side effects.
* **Reusable** — consumed by both :mod:`~exectunnel.transport.tcp` and
  :mod:`~exectunnel.transport.udp` so that payload validation logic is
  never duplicated.
* **Raises typed exceptions** — every helper raises a domain exception from
  :mod:`exectunnel.exceptions` rather than a bare built-in, so callers get
  structured error context (``error_code``, ``error_id``, ``hint``) for free.

Design notes
------------
``require_bytes`` previously lived in ``tcp.py`` as a module-level free
function and was only used by ``TcpConnection``.  Moving it here:

1. Eliminates the duplicate validation that ``UdpFlow`` performed inline
   (with slightly different error codes and messages).
2. Makes the validation contract explicit and testable in isolation.
3. Keeps ``tcp.py`` and ``udp.py`` focused on lifecycle and I/O logic.

Adding new validators
---------------------
All validators must follow the same contract:

* Accept the raw value as the first positional argument.
* Accept ``handler_id: str`` and ``method: str`` for error context.
* Return the validated value typed correctly on success.
* Raise a typed exception from :mod:`exectunnel.exceptions` on failure —
  never a bare ``ValueError`` or ``TypeError``.
* Never use ``raise ... from exc`` unless re-raising inside an ``except``
  block — a direct raise has no cause to chain.
"""

from __future__ import annotations

from exectunnel.exceptions import TransportError

__all__ = ["require_bytes"]


def require_bytes(value: object, handler_id: str, method: str) -> bytes:
    """Return *value* as :class:`bytes` or raise :class:`TransportError`.

    Both :class:`~exectunnel.transport.tcp.TcpConnection` and
    :class:`~exectunnel.transport.udp.UdpFlow` call this before processing
    any inbound or outbound payload to ensure the frame encoder always
    receives raw bytes — never ``str``, ``memoryview``, or ``bytearray``.

    Raises :class:`~exectunnel.exceptions.TransportError` rather than
    :class:`~exectunnel.exceptions.FrameDecodingError` because a non-``bytes``
    payload at this boundary is a **programming error** in the caller (wrong
    type passed to a public method), not a malformed frame received from the
    wire.  The transport layer is the gatekeeper; the protocol layer is not
    involved.

    The ``error_code`` ``"transport.invalid_payload_type"`` is a custom
    override of :attr:`TransportError.default_error_code` (``"transport.error"``),
    valid per the :class:`~exectunnel.exceptions.ExecTunnelError` constructor
    contract.  The ``details`` key ``handler_id`` is intentionally generic —
    it covers both ``conn_id`` (TCP) and ``flow_id`` (UDP) without requiring
    separate validators.

    Args:
        value:      The value to validate.
        handler_id: The connection or flow ID for structured error context.
        method:     The calling method name (e.g. ``"feed"``,
                    ``"send_datagram"``) for error context.

    Returns:
        *value* unchanged, typed as ``bytes``.

    Raises:
        TransportError: If *value* is not a ``bytes`` instance.
            ``error_code`` is ``"transport.invalid_payload_type"``.

    Example::

        chunk = require_bytes(data, self._id, "feed")
        # chunk is now typed as bytes
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
        hint=(
            f"Ensure all callers of {method}() pass raw bytes. "
            "str, bytearray, and memoryview are not accepted."
        ),
    )
