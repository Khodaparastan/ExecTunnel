"""Unit tests for :mod:`exectunnel.session._routing`.

The host-exclusion routing predicate is a security boundary — a wrong
answer either leaks private traffic into the tunnel (false negative)
or breaks legitimate intra-cluster traffic (false positive).

We pin:

* IPv4 RFC 1918 ranges, IPv4 loopback.
* IPv6 loopback / link-local / ULA defaults.
* Public-IP non-exclusion.
* Domain-name pass-through (the documented security boundary).
* Empty exclusion list short-circuit.
* Default network parsing.
"""

from __future__ import annotations

import ipaddress

import pytest
from exectunnel.session._routing import (
    DEFAULT_EXCLUDE_CIDRS,
    get_default_exclusion_networks,
    is_host_excluded,
)


@pytest.fixture(scope="module")
def default_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """The pre-parsed default exclusion networks."""
    return get_default_exclusion_networks()


# ── IPv4 inclusion ────────────────────────────────────────────────────────────


class TestIPv4Excluded:
    @pytest.mark.parametrize(
        "host",
        [
            "10.0.0.1",
            "10.255.255.254",
            "172.16.0.1",
            "172.31.255.254",
            "192.168.0.1",
            "192.168.1.1",
            "127.0.0.1",
            "127.255.255.254",
        ],
    )
    def test_private_ipv4_is_excluded(self, default_networks, host: str) -> None:
        assert is_host_excluded(host, default_networks) is True


class TestIPv4Public:
    @pytest.mark.parametrize(
        "host",
        [
            "8.8.8.8",
            "1.1.1.1",
            "172.32.0.1",  # just outside 172.16.0.0/12
            "192.169.0.1",  # just outside 192.168.0.0/16
            "9.255.255.255",  # just outside 10.0.0.0/8
        ],
    )
    def test_public_ipv4_is_not_excluded(self, default_networks, host: str) -> None:
        assert is_host_excluded(host, default_networks) is False


# ── IPv6 inclusion ────────────────────────────────────────────────────────────


class TestIPv6Excluded:
    @pytest.mark.parametrize(
        "host",
        [
            "::1",  # loopback
            "fe80::1",  # link-local
            "fc00::1",  # unique-local
            "fdff:ffff:ffff:ffff::1",  # unique-local upper
        ],
    )
    def test_private_ipv6_is_excluded(self, default_networks, host: str) -> None:
        assert is_host_excluded(host, default_networks) is True


class TestIPv6Public:
    @pytest.mark.parametrize(
        "host",
        [
            "2001:4860:4860::8888",  # Google DNS
            "2606:4700:4700::1111",  # Cloudflare
        ],
    )
    def test_public_ipv6_is_not_excluded(self, default_networks, host: str) -> None:
        assert is_host_excluded(host, default_networks) is False


# ── Domain-name pass-through (documented security boundary) ───────────────────


class TestDomainNameNeverExcluded:
    """Domain names always pass through — the docstring's security boundary."""

    @pytest.mark.parametrize(
        "host",
        [
            "example.com",
            "internal.corp",
            "kubernetes.default.svc.cluster.local",
            "localhost",  # NB: literal "localhost" is a hostname, not 127.0.0.1
            "single-label",
        ],
    )
    def test_domain_passes_through(self, default_networks, host: str) -> None:
        # Even when the name resolves to a private IP, the routing
        # predicate cannot know that — it returns False.
        assert is_host_excluded(host, default_networks) is False


# ── Edge cases ────────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_exclusion_list_is_short_circuited(self) -> None:
        assert is_host_excluded("10.0.0.1", []) is False

    def test_invalid_host_string_returns_false(self, default_networks) -> None:
        # Garbage strings are not IPs and not domain names — the
        # function must not raise.
        assert is_host_excluded("not a host !!!", default_networks) is False

    def test_custom_exclusion_list(self) -> None:
        nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
            ipaddress.ip_network("100.64.0.0/10"),  # CG-NAT
        ]
        assert is_host_excluded("100.64.0.1", nets) is True
        assert is_host_excluded("100.128.0.1", nets) is False


# ── Default constant shape ────────────────────────────────────────────────────


class TestDefaults:
    def test_default_cidrs_is_tuple(self) -> None:
        assert isinstance(DEFAULT_EXCLUDE_CIDRS, tuple)

    def test_default_cidrs_includes_rfc1918(self) -> None:
        assert "10.0.0.0/8" in DEFAULT_EXCLUDE_CIDRS
        assert "172.16.0.0/12" in DEFAULT_EXCLUDE_CIDRS
        assert "192.168.0.0/16" in DEFAULT_EXCLUDE_CIDRS

    def test_default_cidrs_includes_loopback(self) -> None:
        assert "127.0.0.0/8" in DEFAULT_EXCLUDE_CIDRS

    def test_get_default_exclusion_networks_returns_parsed(self) -> None:
        nets = get_default_exclusion_networks()
        assert all(
            isinstance(n, (ipaddress.IPv4Network, ipaddress.IPv6Network)) for n in nets
        )
        assert len(nets) == len(DEFAULT_EXCLUDE_CIDRS)
