"""
Backward-compat re-exports from protocol/ and utility functions.

All new code should import from exectunnel.protocol.frames,
exectunnel.protocol.ids, and use load_agent_b64 / is_host_excluded /
make_udp_socket from here (or the future exectunnel.transport.utils).
"""
from __future__ import annotations

import base64
import importlib.resources
import ipaddress
import socket

from exectunnel.exceptions import ConfigurationError
from exectunnel.protocol.frames import (
    encode_conn_open_frame,
    encode_data_frame,
    encode_frame,
    encode_udp_close_frame,
    encode_udp_data_frame,
    encode_udp_open_frame,
    parse_frame,
)
from exectunnel.protocol.ids import new_conn_id, new_flow_id

__all__ = [
    "encode_conn_open_frame",
    "encode_data_frame",
    "encode_frame",
    "encode_udp_close_frame",
    "encode_udp_data_frame",
    "encode_udp_open_frame",
    "is_host_excluded",
    "load_agent_b64",
    "make_udp_socket",
    "new_conn_id",
    "new_flow_id",
    "parse_frame",
]


def load_agent_b64() -> str:
    """Load ``payload/agent.py`` from package resources and return as a base64 string.

    Raises
    ------
    ConfigurationError
        If the ``payload/agent.py`` resource cannot be located or read.
        This indicates a broken or incomplete package installation.
    """
    try:
        pkg = importlib.resources.files("exectunnel")
        agent_bytes = (pkg / "payload/agent.py").read_bytes()
    except FileNotFoundError as exc:
        raise ConfigurationError(
            "Agent payload not found in package resources — "
            "the installation may be incomplete or corrupted.",
            error_code="config.agent_payload_missing",
            details={"resource_path": "exectunnel/payload/agent.py"},
            hint=(
                "Reinstall the package with `pip install --force-reinstall exectunnel` "
                "and verify that payload/agent.py is included in the wheel."
            ),
        ) from exc
    except PermissionError as exc:
        raise ConfigurationError(
            "Insufficient permissions to read the agent payload from package resources.",
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
            hint="Reinstall the package and check for filesystem or packaging issues.",
        ) from exc

    try:
        return base64.b64encode(agent_bytes).decode()
    except Exception as exc:
        raise ConfigurationError(
            "Failed to base64-encode the agent payload.",
            error_code="config.agent_payload_encode_failed",
            details={
                "payload_bytes": len(agent_bytes),
                "cause": repr(exc),
            },
            hint="This is an unexpected runtime error; please report it as a bug.",
        ) from exc


def is_host_excluded(
    host: str,
    exclusions: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    """Return ``True`` if *host* (an IP string) falls within any exclusion network.

    Domain names are never excluded — they are resolved remotely by the agent.

    Parameters
    ----------
    host:
        An IPv4 or IPv6 address string, or a domain name.
    exclusions:
        List of networks to test membership against.

    Raises
    ------
    ConfigurationError
        If *exclusions* contains an object that is not an
        ``IPv4Network`` or ``IPv6Network`` instance, indicating a
        misconfigured exclusion list.
    """
    for net in exclusions:
        if not isinstance(net, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            raise ConfigurationError(
                f"Exclusion list contains an invalid entry: {net!r}.",
                error_code="config.exclusion_list_invalid_entry",
                details={
                    "entry": repr(net),
                    "expected_type": "IPv4Network | IPv6Network",
                    "received_type": type(net).__name__,
                },
                hint=(
                    "Ensure all entries in the exclusion list are parsed with "
                    "ipaddress.ip_network() before being passed to is_host_excluded()."
                ),
            )

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # Domain names are never excluded — resolved remotely by the agent.
        return False

    return any(addr in net for net in exclusions)


def make_udp_socket(host: str) -> socket.socket:
    """Create a UDP socket with the correct address family for *host*.

    Parameters
    ----------
    host:
        An IPv4 or IPv6 address string, or a domain name.
        Domain names default to ``AF_INET``.

    Raises
    ------
    ConfigurationError
        If the operating system refuses to create a UDP socket for the
        required address family (e.g. IPv6 is disabled at the kernel level).
    """
    try:
        addr = ipaddress.ip_address(host)
        family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
    except ValueError:
        # Domain names are unusual in the excluded set; default to IPv4.
        family = socket.AF_INET

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
                "and that it is not disabled via sysctl."
            ),
        ) from exc
