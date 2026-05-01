"""Backwards-compatible shim — delegates to :mod:`tests._helpers`.

This module pre-dates the consolidated ``tests/_helpers/`` package; it
is kept solely so existing relative imports under
``tests.unit.transport`` continue to work.  New code should import
from :mod:`tests._helpers` directly::

    from tests._helpers import (
        TCP_CONN_ID,
        UDP_FLOW_ID,
        MockWsSend,
        make_stream_reader,
        make_mock_writer,
    )

The ``make_reader(*chunks)`` variadic helper has no analogue in the
new package because variadic positional bytes are awkward to type;
prefer ``make_stream_reader([chunk1, chunk2, ...])`` instead.
"""

from __future__ import annotations

import asyncio

from tests._helpers import (
    TCP_CONN_ID,
    UDP_FLOW_ID,
    MockWsSend,
    make_mock_writer,
)
from tests._helpers.streams import make_stream_reader

__all__ = [
    "TCP_CONN_ID",
    "UDP_FLOW_ID",
    "MockWsSend",
    "make_mock_writer",
    "make_reader",
    "make_stream_reader",
]


def make_reader(*chunks: bytes, eof: bool = True) -> asyncio.StreamReader:
    """Variadic-chunks adapter around :func:`make_stream_reader`.

    Kept for back-compat with the older transport tests.  Equivalent to
    ``make_stream_reader(list(chunks), eof=eof)``.
    """
    return make_stream_reader(list(chunks) if chunks else b"", eof=eof)
