"""Network helpers — port allocation and loopback address resolution.

These functions are intentionally synchronous and side-effect-light so
they can be called from sync or async tests, fixtures, or
``conftest.py`` setup code without coupling to an event loop.
"""

from __future__ import annotations

import socket
from typing import Final

# IPv4 loopback only.  Tests that need IPv6 loopback should prefer
# ``::1`` directly; we deliberately do not auto-detect because some CI
# images disable IPv6 and we want failures to point at that, not at a
# silent re-bind to v4.
_LOOPBACK_V4: Final[str] = "127.0.0.1"


def loopback_addr() -> str:
    """Return the IPv4 loopback address used across the test suite."""
    return _LOOPBACK_V4


def free_port(*, host: str = _LOOPBACK_V4) -> int:
    """Return an available TCP port on *host*.

    The returned port is the one the kernel chose for an ephemeral
    bind; by the time the caller uses it the port is unbound, so a
    third-party process *could* race in. For unit tests this is
    acceptable — the alternative (keeping the socket alive) leaks
    descriptors into long pytest runs.

    Args:
        host: Hostname / IP to bind to.  Defaults to IPv4 loopback.

    Returns:
        A port number in the OS-defined ephemeral range.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def free_udp_port(*, host: str = _LOOPBACK_V4) -> int:
    """Return an available UDP port on *host*.

    Same caveats as :func:`free_port`; see that docstring.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]
