"""Async-safe UDP socket helpers for the session layer.

``make_udp_socket`` and ``resolve_address_family`` are used by the
direct-UDP relay path in ``_dispatcher.py``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket

from exectunnel.exceptions import ConfigurationError

__all__ = [
    "make_udp_socket",
    "resolve_address_family",
]

logger = logging.getLogger(__name__)


async def resolve_address_family(
    host: str,
    *,
    prefer_ipv4: bool = False,
) -> socket.AddressFamily:
    """Resolve the correct socket address family for *host* without blocking.

    For IP literals the resolution is synchronous (no I/O).
    For domain names ``getaddrinfo`` is dispatched to the default executor
    so the event loop is never blocked.

    Args:
        host:         An IPv4 or IPv6 address string, or a domain name.
        prefer_ipv4:  When *host* is a domain that resolves to both IPv4 and
                      IPv6 addresses, prefer ``AF_INET``.  Default ``False``
                      (use whatever ``getaddrinfo`` returns first — typically
                      IPv6 on dual-stack systems).

    Returns:
        ``socket.AF_INET`` or ``socket.AF_INET6``.

    Raises:
        ConfigurationError: If the address family cannot be determined.
    """
    # Fast path for IP literals — no async I/O needed.
    try:
        addr = ipaddress.ip_address(host)
        return socket.AF_INET6 if addr.version == 6 else socket.AF_INET
    except ValueError:
        pass

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_DGRAM)
        if not infos:
            raise OSError(f"getaddrinfo returned no results for {host!r}")
    except OSError as exc:
        raise ConfigurationError(
            f"Failed to resolve address family for host {host!r}.",
            error_code="config.udp_socket_resolve_failed",
            details={"host": host, "os_error": str(exc)},
            hint=(
                "Ensure the host is reachable and DNS is functioning. "
                "Domain names in the exclusion list are resolved locally "
                "to determine the UDP socket address family."
            ),
        ) from exc

    families = {info[0] for info in infos}
    if prefer_ipv4 and socket.AF_INET in families:
        return socket.AF_INET

    # Return whatever getaddrinfo considers highest-priority.
    family = infos[0][0]
    logger.debug(
        "resolved %s → %s (available: %s)",
        host,
        family.name,
        ", ".join(sorted(f.name for f in families)),
    )
    return family


async def make_udp_socket(
    host: str,
    *,
    prefer_ipv4: bool = False,
) -> socket.socket:
    """Create a non-blocking UDP socket with the correct address family for *host*.

    Always safe to call from the asyncio event loop thread — domain name
    resolution is dispatched to the executor.

    The returned socket is set to non-blocking mode for direct use with
    ``asyncio`` transport APIs.

    The caller is responsible for closing the returned socket.

    Args:
        host:         An IPv4 or IPv6 address string, or a domain name.
        prefer_ipv4:  Prefer ``AF_INET`` for dual-stack domain names.

    Returns:
        A non-blocking, unconnected UDP socket.

    Raises:
        ConfigurationError: If address family resolution fails or the OS
            refuses to create a UDP socket.
    """
    family = await resolve_address_family(host, prefer_ipv4=prefer_ipv4)
    try:
        sock = socket.socket(family, socket.SOCK_DGRAM)
    except OSError as exc:
        raise ConfigurationError(
            f"Failed to create a UDP socket for address family {family.name}.",
            error_code="config.udp_socket_create_failed",
            details={
                "host": host,
                "address_family": family.name,
                "os_error": str(exc),
            },
            hint=(
                f"Ensure {family.name} is supported and enabled on this host. "
                "For IPv6, check that the kernel has IPv6 support compiled in "
                "and that it is not disabled via "
                "sysctl net.ipv6.conf.all.disable_ipv6."
            ),
        ) from exc

    sock.setblocking(False)
    logger.debug("created UDP socket: family=%s, fd=%d", family.name, sock.fileno())
    return sock
