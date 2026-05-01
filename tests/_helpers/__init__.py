"""Shared test helpers.

Re-exports the small set of factories and stub classes that multiple
test modules rely on, so callers do not have to know which submodule
hosts a given helper. Keep this list short and stable — every name
here is part of the test-package's "ABI".

The submodules:

* :mod:`tests._helpers.network` — ``free_port``, ``free_udp_port``,
  ``loopback_addr``.
* :mod:`tests._helpers.streams` — ``make_stream_reader``, ``make_eof_reader``,
  ``DummyWriter``, ``DummyDatagramTransport``, ``make_mock_writer``.
* :mod:`tests._helpers.ws` — ``MockWsSend``, ``QueueWs`` (in-memory
  bidirectional websocket double for :mod:`tests.integration.test_session_inmemory`).
* :mod:`tests._helpers.ids` — canonical TCP/UDP IDs used across tests
  (``TCP_CONN_ID``, ``UDP_FLOW_ID``).
"""

from __future__ import annotations

from .ids import TCP_CONN_ID, UDP_FLOW_ID
from .network import free_port, free_udp_port, loopback_addr
from .streams import (
    DummyDatagramTransport,
    DummyWriter,
    make_eof_reader,
    make_mock_writer,
    make_stream_reader,
)
from .ws import MockWsSend, QueueWs

__all__ = [
    "DummyDatagramTransport",
    "DummyWriter",
    "MockWsSend",
    "QueueWs",
    "TCP_CONN_ID",
    "UDP_FLOW_ID",
    "free_port",
    "free_udp_port",
    "loopback_addr",
    "make_eof_reader",
    "make_mock_writer",
    "make_stream_reader",
]
