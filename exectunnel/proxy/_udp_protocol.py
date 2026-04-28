"""asyncio datagram protocol used by :class:`UDPRelay`.

Separated from :mod:`exectunnel.proxy.udp_relay` to keep the relay class
focused on queue management and SOCKS5 header logic. The protocol
itself is a thin adapter that forwards received datagrams to the owning
relay and logs transport errors.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .udp_relay import UDPRelay

__all__ = ["RelayDatagramProtocol"]

_log: Final[logging.Logger] = logging.getLogger("exectunnel.proxy.udp_relay")


class RelayDatagramProtocol(asyncio.DatagramProtocol):
    """Forward received datagrams to the owning :class:`UDPRelay`."""

    __slots__ = ("_relay",)

    def __init__(self, relay: UDPRelay) -> None:
        self._relay = relay

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        """Forward *data* from *addr* to the owning relay."""
        # IPv4 addr is (host, port). IPv6 addr is commonly
        # (host, port, flowinfo, scopeid). UDPRelay only needs host/port.
        self._relay.on_datagram(data, (addr[0], addr[1]))

    def error_received(self, exc: Exception) -> None:
        """Log non-fatal transport errors reported by asyncio."""
        _log.debug("udp relay socket error [%s]: %s", type(exc).__name__, exc)

    def connection_lost(self, exc: Exception | None) -> None:
        """Log abnormal transport teardown; normal closures are silent."""
        if exc is not None:
            _log.debug("udp relay connection lost: %s", exc)
