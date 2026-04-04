from __future__ import annotations

import ipaddress
from collections.abc import Sequence


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
