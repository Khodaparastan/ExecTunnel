"""Host-exclusion routing — determines whether a destination bypasses the tunnel.

Excluded hosts are connected directly by the local process rather than being
forwarded through the WebSocket exec channel.  The default exclusion list
covers RFC 1918 private ranges, loopback, and the most common IPv6 private
and link-local ranges.
"""

import ipaddress
from collections.abc import Sequence
from typing import Final

from ._constants import DEFAULT_EXCLUDE_IPV6_CIDRS

# ── Default CIDR exclusions ───────────────────────────────────────────────────

DEFAULT_EXCLUDE_CIDRS: Final[tuple[str, ...]] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    *DEFAULT_EXCLUDE_IPV6_CIDRS,
)
"""Default RFC 1918 private ranges, loopback, and IPv6 private/link-local
networks excluded from tunnelling."""


def get_default_exclusion_networks() -> list[
    ipaddress.IPv4Network | ipaddress.IPv6Network
]:
    """Return the default exclusion list as pre-parsed network objects.

    Parses :data:`DEFAULT_EXCLUDE_CIDRS` into ``ipaddress`` network objects
    suitable for use as ``TunnelConfig.exclude``.

    Returns:
        A list of :class:`~ipaddress.IPv4Network` and
        :class:`~ipaddress.IPv6Network` objects representing the default
        exclusion networks.
    """
    return [ipaddress.ip_network(cidr, strict=False) for cidr in DEFAULT_EXCLUDE_CIDRS]


def is_host_excluded(
    host: str,
    exclusions: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    """Return ``True`` if *host* falls within any configured exclusion network.

    Domain names are never excluded — they are resolved remotely by the agent
    and their IP addresses are not known at routing time.

    Warning:
        **Security boundary**: a domain name that resolves to a private IP
        (e.g. ``internal.corp`` → ``10.0.0.1``) will *not* be excluded even
        if ``10.0.0.1/8`` is in the exclusion list.  The exclusion list only
        protects against direct IP-literal connections.  DNS-based bypasses
        require a separate DNS-level policy.

    Note:
        The exclusion list is assumed to be pre-validated (all entries are
        ``IPv4Network`` or ``IPv6Network`` instances).  Validation at
        configuration load time rather than per-call avoids repeated
        ``isinstance`` checks on every connection attempt.

        The membership test is O(n) in the number of exclusion networks.  For
        typical deployments (fewer than 20 networks) this is negligible.  If
        the exclusion list grows to hundreds of entries, consider a prefix-tree
        structure.

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
