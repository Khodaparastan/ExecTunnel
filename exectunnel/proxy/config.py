"""SOCKS5 server configuration.

:class:`Socks5ServerConfig` is the single source of truth for all
tuneable parameters of :class:`~exectunnel.proxy.server.Socks5Server`.
All fields are validated at construction time so that misconfiguration
is caught before any socket is opened.
"""

from __future__ import annotations

import ipaddress
import math
from dataclasses import dataclass
from typing import Final

from exectunnel.exceptions import ConfigurationError

from ._constants import (
    DEFAULT_DROP_WARN_INTERVAL,
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_HOST,
    DEFAULT_LISTEN_BACKLOG,
    DEFAULT_MAX_CONCURRENT_HANDSHAKES,
    DEFAULT_MAX_UDP_PAYLOAD_BYTES,
    DEFAULT_PORT,
    DEFAULT_QUEUE_PUT_TIMEOUT,
    DEFAULT_REQUEST_QUEUE_CAPACITY,
    DEFAULT_UDP_BIND_HOST,
    DEFAULT_UDP_QUEUE_CAPACITY,
    MAX_TCP_UDP_PORT,
    MAX_TUNNEL_UDP_PAYLOAD_BYTES,
)

__all__ = ["Socks5ServerConfig"]

_IPV6_FMT: Final[int] = 6


def _require(
    condition: bool,
    *,
    field: str,
    value: object,
    expected: str,
    hint: str,
) -> None:
    """Raise :exc:`ConfigurationError` when *condition* is falsy.

    Args:
        condition: The predicate that must hold.
        field: Dataclass field name for the error message.
        value: The offending value.
        expected: Human-readable description of the accepted range.
        hint: Actionable remediation hint.

    Raises:
        ConfigurationError: When *condition* is ``False``.
    """
    if condition:
        return
    raise ConfigurationError(
        f"Socks5ServerConfig.{field} {value!r} is invalid.",
        details={"field": field, "value": value, "expected": expected},
        hint=hint,
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_positive(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class Socks5ServerConfig:
    """Immutable configuration for :class:`~exectunnel.proxy.server.Socks5Server`.

    All fields are validated at construction time via
    :meth:`__post_init__`.

    Attributes:
        host: Bind address. Defaults to ``"127.0.0.1"``.
        port: Bind port in ``[1, 65535]``. Defaults to ``1080``.
        allow_non_loopback: Permit binding to non-loopback addresses. Disabled
            by default because this proxy is NO_AUTH only.
        listen_backlog: TCP listen backlog passed to ``asyncio.start_server``.
        max_concurrent_handshakes: Maximum in-progress SOCKS5 handshakes.
        handshake_timeout: Max seconds for a SOCKS5 handshake. Must be
            positive.
        request_queue_capacity: Max buffered, fully-negotiated SOCKS5
            requests before backpressure. Must be ≥ 1.
        udp_relay_queue_capacity: Max buffered inbound datagrams per
            :class:`~exectunnel.proxy.udp_relay.UDPRelay` instance before
            dropping. Must be ≥ 1.
        udp_max_payload_bytes: Maximum SOCKS5 UDP payload bytes accepted from
            clients and sent back to clients, excluding the SOCKS5 UDP header.
        queue_put_timeout: Max seconds to wait when enqueueing a
            completed handshake. Must be positive.
        udp_drop_warn_interval: Log a warning every *N* UDP queue-full
            drops. Must be ≥ 1.
        udp_associate_enabled: Whether to accept SOCKS5 UDP_ASSOCIATE.
        udp_bind_host: Local address used by UDP relays.
        udp_advertise_host: Address advertised in UDP_ASSOCIATE replies.
            Defaults to ``udp_bind_host``. Must be an IP literal and not
            unspecified.

    Raises:
        ConfigurationError: If any field fails validation.
    """

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    allow_non_loopback: bool = False
    listen_backlog: int = DEFAULT_LISTEN_BACKLOG
    max_concurrent_handshakes: int = DEFAULT_MAX_CONCURRENT_HANDSHAKES
    handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT
    request_queue_capacity: int = DEFAULT_REQUEST_QUEUE_CAPACITY
    udp_relay_queue_capacity: int = DEFAULT_UDP_QUEUE_CAPACITY
    udp_max_payload_bytes: int = DEFAULT_MAX_UDP_PAYLOAD_BYTES
    queue_put_timeout: float = DEFAULT_QUEUE_PUT_TIMEOUT
    udp_drop_warn_interval: int = DEFAULT_DROP_WARN_INTERVAL
    udp_associate_enabled: bool = True
    udp_bind_host: str = DEFAULT_UDP_BIND_HOST
    udp_advertise_host: str | None = None

    def __post_init__(self) -> None:
        """Validate all fields, raising on the first failure."""
        _require(
            bool(self.host),
            field="host",
            value=self.host,
            expected="non-empty string",
            hint="Provide a valid bind address, e.g. '127.0.0.1' or '::1'.",
        )
        _require(
            _is_int(self.port) and 1 <= self.port <= MAX_TCP_UDP_PORT,
            field="port",
            value=self.port,
            expected="integer in [1, 65535]",
            hint="Use a valid TCP port number between 1 and 65535.",
        )
        _require(
            self.allow_non_loopback or self.is_loopback,
            field="host",
            value=self.host,
            expected="loopback bind address unless allow_non_loopback=True",
            hint=(
                "This SOCKS5 proxy supports NO_AUTH only. Bind to 127.0.0.1 "
                "or ::1, or explicitly set allow_non_loopback=True and protect "
                "the listener with a firewall/load balancer authentication layer."
            ),
        )
        _require(
            _is_int(self.listen_backlog) and self.listen_backlog >= 1,
            field="listen_backlog",
            value=self.listen_backlog,
            expected="integer ≥ 1",
            hint="Set listen_backlog to at least 1.",
        )
        _require(
            _is_int(self.max_concurrent_handshakes)
            and self.max_concurrent_handshakes >= 1,
            field="max_concurrent_handshakes",
            value=self.max_concurrent_handshakes,
            expected="integer ≥ 1",
            hint="Set max_concurrent_handshakes to at least 1.",
        )
        _require(
            _is_finite_positive(self.handshake_timeout),
            field="handshake_timeout",
            value=self.handshake_timeout,
            expected="positive float (seconds)",
            hint="Set handshake_timeout to a positive number of seconds.",
        )
        _require(
            _is_int(self.request_queue_capacity) and self.request_queue_capacity >= 1,
            field="request_queue_capacity",
            value=self.request_queue_capacity,
            expected="integer ≥ 1",
            hint="Set request_queue_capacity to at least 1.",
        )
        _require(
            _is_int(self.udp_relay_queue_capacity)
            and self.udp_relay_queue_capacity >= 1,
            field="udp_relay_queue_capacity",
            value=self.udp_relay_queue_capacity,
            expected="integer ≥ 1",
            hint="Set udp_relay_queue_capacity to at least 1.",
        )
        _require(
            _is_int(self.udp_max_payload_bytes)
            and 1 <= self.udp_max_payload_bytes <= MAX_TUNNEL_UDP_PAYLOAD_BYTES,
            field="udp_max_payload_bytes",
            value=self.udp_max_payload_bytes,
            expected=f"integer in [1, {MAX_TUNNEL_UDP_PAYLOAD_BYTES}]",
            hint=(
                "Use a UDP payload cap that matches your workload. For DNS-only "
                "use, 4096 is usually enough. For low-MTU UDP, consider 1200."
            ),
        )
        _require(
            _is_finite_positive(self.queue_put_timeout),
            field="queue_put_timeout",
            value=self.queue_put_timeout,
            expected="positive float (seconds)",
            hint="Set queue_put_timeout to a positive number of seconds.",
        )
        _require(
            _is_int(self.udp_drop_warn_interval) and self.udp_drop_warn_interval >= 1,
            field="udp_drop_warn_interval",
            value=self.udp_drop_warn_interval,
            expected="integer ≥ 1",
            hint="Set udp_drop_warn_interval to at least 1.",
        )
        _require(
            bool(self.udp_bind_host),
            field="udp_bind_host",
            value=self.udp_bind_host,
            expected="non-empty string",
            hint="Set udp_bind_host to a bind address such as '127.0.0.1'.",
        )

        _require(
            self.allow_non_loopback or _is_loopback_host(self.udp_bind_host),
            field="udp_bind_host",
            value=self.udp_bind_host,
            expected="loopback UDP bind address unless allow_non_loopback=True",
            hint=(
                "This SOCKS5 proxy supports NO_AUTH only. Bind UDP relays to "
                "127.0.0.1 or ::1, or explicitly set allow_non_loopback=True "
                "and protect both TCP and UDP listeners with a firewall or "
                "authenticated load balancer."
            ),
        )

        effective_udp_advertise_host = self.effective_udp_advertise_host
        try:
            udp_advertise_ip = ipaddress.ip_address(effective_udp_advertise_host)
        except ValueError:
            _require(
                False,
                field="udp_advertise_host",
                value=effective_udp_advertise_host,
                expected="IP address string",
                hint=(
                    "SOCKS5 replies need an IP literal for BND.ADDR in this "
                    "implementation. Set udp_advertise_host to a reachable IP."
                ),
            )
        else:
            _require(
                not udp_advertise_ip.is_unspecified,
                field="udp_advertise_host",
                value=effective_udp_advertise_host,
                expected="non-unspecified IP address",
                hint=(
                    "Do not advertise 0.0.0.0 or :: in UDP_ASSOCIATE replies; "
                    "advertise a concrete address the client can reach."
                ),
            )

            if self.allow_non_loopback and not self.is_loopback:
                _require(
                    not udp_advertise_ip.is_loopback,
                    field="udp_advertise_host",
                    value=effective_udp_advertise_host,
                    expected="non-loopback IP when TCP listener is non-loopback",
                    hint=(
                        "When exposing SOCKS on a non-loopback address, the UDP "
                        "relay advertised address must also be reachable by that "
                        "client, not 127.0.0.1."
                    ),
                )

    @property
    def is_loopback(self) -> bool:
        """Whether :attr:`host` resolves to a loopback address.

        ``"localhost"`` is treated as loopback. Other non-IP strings return
        ``False`` because they require DNS resolution.
        """
        if self.host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False

    @property
    def effective_udp_advertise_host(self) -> str:
        """UDP address advertised to SOCKS5 clients."""
        return self.udp_advertise_host or self.udp_bind_host

    @property
    def url(self) -> str:
        """Human-readable ``tcp://host:port`` string for logging."""
        try:
            ip = ipaddress.ip_address(self.host)
        except ValueError:
            return f"tcp://{self.host}:{self.port}"
        if ip.version == _IPV6_FMT:
            return f"tcp://[{self.host}]:{self.port}"
        return f"tcp://{self.host}:{self.port}"
