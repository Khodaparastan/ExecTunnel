"""
Utility functions for the exectunnel package.

``load_agent_b64``
    Load and base64url-encode the bundled agent payload for bootstrap upload.

``is_host_excluded``
    Test whether an IP address falls within a configured exclusion network.

``make_udp_socket``
    Create a UDP socket with the correct address family for a given host.

``validate_exclusions``
    Validate a raw exclusion list into typed network objects.

Note on ``make_udp_socket``
---------------------------
``make_udp_socket`` performs a synchronous ``socket.getaddrinfo`` call for
domain name inputs.  It must **not** be called from the asyncio event loop
thread for domain names — wrap the call in ``asyncio.to_thread()`` or resolve
the address family before entering the async context.  For IP literal inputs
the function is safe to call from any thread.
"""

from __future__ import annotations

import base64
import functools
import importlib.resources
import ipaddress
import socket
from collections.abc import Sequence

from exectunnel.exceptions import ConfigurationError

__all__ = [
    "is_host_excluded",
    "load_agent_b64",
    "make_udp_socket",
    "validate_exclusions",
]


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
            * ``config.agent_payload_missing`` — resource not found.
            * ``config.agent_payload_permission_denied`` — unreadable.
            * ``config.agent_payload_load_failed`` — any other I/O error.
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
    except Exception as exc:
        raise ConfigurationError(
            "Unexpected error while loading the agent payload from package resources.",
            error_code="config.agent_payload_load_failed",
            details={
                "resource_path": "exectunnel/payload/agent.py",
                "cause": repr(exc),
            },
            hint=(
                "Reinstall the package and check for filesystem or packaging issues."
            ),
        ) from exc

    # urlsafe_b64encode never raises on bytes input — no try/except needed.
    return base64.urlsafe_b64encode(agent_bytes).rstrip(b"=").decode("ascii")


def validate_exclusions(
    exclusions: Sequence[object],
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Validate and return a typed exclusion list.

    Call this once at configuration load time rather than inside
    :func:`is_host_excluded` on every connection attempt.

    Args:
        exclusions: Raw sequence of objects to validate.

    Returns:
        A typed list of ``IPv4Network`` / ``IPv6Network`` instances.

    Raises:
        ConfigurationError: If any entry is not a valid network object.
    """
    validated: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in exclusions:
        if not isinstance(entry, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            raise ConfigurationError(
                f"Exclusion list contains an invalid entry: {entry!r}.",
                error_code="config.exclusion_list_invalid_entry",
                details={
                    "entry": repr(entry),
                    "expected_type": "IPv4Network | IPv6Network",
                    "received_type": type(entry).__name__,
                },
                hint=(
                    "Parse all exclusion entries with "
                    "``ipaddress.ip_network()`` before passing them to "
                    "``validate_exclusions()``."
                ),
            )
        validated.append(entry)
    return validated


def is_host_excluded(
    host: str,
    exclusions: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    """Return ``True`` if *host* falls within any configured exclusion network.

    Domain names are never excluded — they are resolved remotely by the agent
    and their IP addresses are not known at routing time.

    The exclusion list is assumed to be pre-validated (all entries are
    ``IPv4Network`` or ``IPv6Network`` instances).  Validation at
    configuration load time rather than per-call avoids repeated
    ``isinstance`` checks on every connection attempt.

    The membership test is O(n) in the number of exclusion networks.  For
    typical deployments (< 20 networks) this is negligible.  If the exclusion
    list grows to hundreds of entries, consider a prefix-tree structure.

    Args:
        host:       An IPv4 or IPv6 address string, or a domain name.
        exclusions: Pre-validated sequence of networks to test against.

    Returns:
        ``True`` if *host* is an IP address that falls within any exclusion
        network; ``False`` for domain names and non-matching addresses.
    """
    if not exclusions:
        return False

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # Domain names are never excluded.
        return False

    return any(addr in net for net in exclusions)


def make_udp_socket(host: str) -> socket.socket:
    """Create a UDP socket with the correct address family for *host*.

    Resolves the address family from the IP literal.  For domain names,
    performs a synchronous ``getaddrinfo`` lookup — **do not call this
    function from the asyncio event loop thread for domain name inputs**.
    For IP literal inputs the function is safe to call from any thread.

    The caller is responsible for closing the returned socket.

    Args:
        host: An IPv4 or IPv6 address string, or a domain name.

    Returns:
        An unconnected, blocking UDP socket with the correct address family.

    Raises:
        ConfigurationError:
            * If the OS refuses to create a UDP socket for the required
              address family (e.g. IPv6 disabled at the kernel level).
            * If ``getaddrinfo`` fails to resolve a domain name.
    """
    # Determine address family from IP literal first (fast path, no I/O).
    try:
        addr = ipaddress.ip_address(host)
        family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
    except ValueError:
        # Domain name — resolve to determine the correct address family.
        # This is a blocking syscall; callers must not invoke this from the
        # event loop thread for domain name inputs.
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_DGRAM)
            if not infos:
                raise OSError(f"getaddrinfo returned no results for {host!r}")
            family = infos[0][0]
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

    try:
        return socket.socket(family, socket.SOCK_DGRAM)
    except OSError as exc:
        family_name = "AF_INET6" if family == socket.AF_INET6 else "AF_INET"
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
