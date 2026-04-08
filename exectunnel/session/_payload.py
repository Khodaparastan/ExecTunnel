"""Agent payload loading and UDP socket helpers for the session layer.

``load_agent_b64`` is called once per process lifetime (LRU-cached).
``make_udp_socket`` / ``resolve_address_family`` are async-safe helpers
used by the direct-UDP relay path in ``_handlers.py``.
"""

import asyncio
import base64
import functools
import importlib.resources
import ipaddress
import socket

from exectunnel.exceptions import ConfigurationError



@functools.lru_cache(maxsize=1)
def load_agent_b64() -> str:
    """Load ``payload/agent.py`` and return it as a URL-safe base64 string.

    The result is cached after the first call — the agent payload never
    changes at runtime and re-reading + re-encoding on every session
    reconnect is unnecessary I/O.

    URL-safe base64 (``urlsafe_b64encode``) is used so the encoded string
    contains only ``[A-Za-z0-9_-]`` characters, which are unconditionally
    safe as ``printf '%s'`` arguments in all POSIX shell environments.
    Standard base64 produces ``+`` and ``/`` which are valid in most shells
    but can be misinterpreted by some ``printf`` implementations when they
    appear at chunk boundaries.

    Returns:
        The agent source as a URL-safe base64 string with no padding.

    Raises:
        ConfigurationError:
            * ``config.agent_payload_missing``          — resource not found.
            * ``config.agent_payload_permission_denied`` — unreadable.
            * ``config.agent_payload_load_failed``       — any other I/O error.
    """
    try:
        pkg = importlib.resources.files("exectunnel")
        # Two separate / joins — the API contract does not guarantee that a
        # single string with "/" is treated as a path separator.
        agent_bytes = (pkg / "payload" / "agent.py").read_bytes()
    except FileNotFoundError as exc:
        raise ConfigurationError(
            "Agent payload not found in package resources — "
            "the installation may be incomplete or corrupted.",
            error_code="config.agent_payload_missing",
            details={"resource_path": "exectunnel/payload/agent.py"},
            hint=(
                "Reinstall the package and verify that payload/agent.py is "
                "included in the distribution. If using an editable install, "
                "ensure the source tree is intact."
            ),
        ) from exc
    except PermissionError as exc:
        raise ConfigurationError(
            "Insufficient permissions to read the agent payload from "
            "package resources.",
            error_code="config.agent_payload_permission_denied",
            details={"resource_path": "exectunnel/payload/agent.py"},
            hint=(
                "Check the file permissions of the installed package directory "
                "and ensure the current user can read it."
            ),
        ) from exc
    except OSError as exc:
        # Covers all remaining I/O errors (e.g. EIO, ENOSPC) without
        # accidentally catching KeyboardInterrupt / SystemExit.
        raise ConfigurationError(
            "Unexpected I/O error while loading the agent payload from "
            "package resources.",
            error_code="config.agent_payload_load_failed",
            details={
                "resource_path": "exectunnel/payload/agent.py",
                "cause": repr(exc),
            },
            hint="Reinstall the package and check for filesystem or packaging issues.",
        ) from exc

    # urlsafe_b64encode never raises on bytes input — no try/except needed.
    return base64.urlsafe_b64encode(agent_bytes).rstrip(b"=").decode("ascii")


async def resolve_address_family(host: str) -> socket.AddressFamily:
    """Resolve the correct socket address family for *host* without blocking.

    For IP literals the resolution is synchronous (no I/O).
    For domain names ``getaddrinfo`` is dispatched to the default executor
    so the event loop is never blocked.

    Args:
        host: An IPv4 or IPv6 address string, or a domain name.

    Returns:
        ``socket.AF_INET`` or ``socket.AF_INET6``.

    Raises:
        ConfigurationError: If the address family cannot be determined.
    """
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
        return infos[0][0]
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


async def make_udp_socket(host: str) -> socket.socket:
    """Create a UDP socket with the correct address family for *host*.

    Always safe to call from the asyncio event loop thread — domain name
    resolution is dispatched to the executor.

    The caller is responsible for closing the returned socket.

    Args:
        host: An IPv4 or IPv6 address string, or a domain name.

    Returns:
        An unconnected UDP socket with the correct address family.

    Raises:
        ConfigurationError: If the OS refuses to create a UDP socket or
            address family resolution fails.
    """
    family = await resolve_address_family(host)
    family_name = "AF_INET6" if family == socket.AF_INET6 else "AF_INET"
    try:
        return socket.socket(family, socket.SOCK_DGRAM)
    except OSError as exc:
        raise ConfigurationError(
            f"Failed to create a UDP socket for address family {family_name}.",
            error_code="config.udp_socket_create_failed",
            details={
                "host": host,
                "address_family": family_name,
                "os_error": str(exc),
            },
            hint=(
                f"Ensure {family_name} is supported and enabled on this host. "
                "For IPv6, check that the kernel has IPv6 support compiled in "
                "and that it is not disabled via "
                "sysctl net.ipv6.conf.all.disable_ipv6."
            ),
        ) from exc
