"""Shared async stream utilities used across layers.

This module contains generic asyncio stream helpers that have no knowledge of
SOCKS5, frames, or any domain-specific protocol.  It exists so that multiple
layers (proxy, session, …) can reuse the same battle-tested I/O primitives
without introducing circular dependencies.
"""

from __future__ import annotations

import asyncio
import contextlib

from exectunnel.exceptions import ProtocolError

__all__ = [
    "best_effort_write",
    "close_writer",
    "read_exact",
]

# Maximum number of bytes read_exact() will accept in a single call.
# Protects against a caller accidentally passing a huge length field.
_MAX_READ_BYTES: int = 65_535


async def read_exact(reader: asyncio.StreamReader, nbytes: int) -> bytes:
    """Read exactly *nbytes* bytes from *reader*.

    Args:
        reader: The asyncio stream to read from.
        nbytes: Number of bytes to read.  Must be in ``[1, 65535]``.

    Returns:
        Exactly *nbytes* bytes.

    Raises:
        ValueError:    If *nbytes* is outside ``[1, 65535]`` — indicates a
                       caller logic bug.
        ProtocolError: If the stream ends before *nbytes* bytes are available.
    """
    if not (1 <= nbytes <= _MAX_READ_BYTES):
        raise ValueError(
            f"read_exact() called with nbytes={nbytes}; must be in "
            f"[1, {_MAX_READ_BYTES}].  This is a caller bug — validate the "
            "length field before calling."
        )
    try:
        return await reader.readexactly(nbytes)
    except asyncio.IncompleteReadError as exc:
        received = len(exc.partial) if exc.partial else 0
        raise ProtocolError(
            f"Stream ended after {received} byte(s); expected {nbytes} byte(s).",
            error_code="protocol.truncated_read",
            details={
                "expected_bytes": nbytes,
                "received_bytes": received,
            },
            hint=(
                "The remote peer disconnected or sent a truncated message.  "
                "This is usually benign — the peer closed the connection early."
            ),
        ) from exc


async def close_writer(writer: asyncio.StreamWriter) -> None:
    """Close *writer*, suppressing ``OSError`` and ``RuntimeError``.

    Centralised so the identical close pattern is not repeated across layers.

    Args:
        writer: The asyncio stream writer to close.
    """
    with contextlib.suppress(OSError, RuntimeError):
        writer.close()
        await writer.wait_closed()


async def best_effort_write(
    writer: asyncio.StreamWriter,
    data: bytes,
) -> None:
    """Write *data* and drain, suppressing ``OSError`` and ``RuntimeError``.

    Used for best-effort error-reply writes where the connection may already
    be half-closed.

    Args:
        writer: The asyncio stream writer to write to.
        data:   Raw bytes to write.
    """
    with contextlib.suppress(OSError, RuntimeError):
        writer.write(data)
    with contextlib.suppress(OSError, RuntimeError):
        await writer.drain()
