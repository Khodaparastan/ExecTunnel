"""Default CIDR exclusions (RFC 1918 + loopback)."""
from __future__ import annotations

import ipaddress

# Stored as plain strings so callers choose how to parse them.
DEFAULT_EXCLUDE_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
)


def get_default_exclusion_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Return the default RFC1918 + loopback exclusion list as network objects."""
    return [ipaddress.ip_network(cidr, strict=False) for cidr in DEFAULT_EXCLUDE_CIDRS]
