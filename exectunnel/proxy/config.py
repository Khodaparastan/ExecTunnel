"""SOCKS5 server configuration.

:class:`Socks5ServerConfig` is the single source of truth for all
tuneable parameters of :class:`~exectunnel.proxy.server.Socks5Server`.
All fields are validated at construction time so that misconfiguration
is caught before any socket is opened.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Final

from exectunnel.exceptions import ConfigurationError

from ._constants import (
    DEFAULT_DROP_WARN_INTERVAL,
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_QUEUE_PUT_TIMEOUT,
    DEFAULT_REQUEST_QUEUE_CAPACITY,
    DEFAULT_UDP_QUEUE_CAPACITY,
    MAX_TCP_UDP_PORT,
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


@dataclass(frozen=True, slots=True)
class Socks5ServerConfig:
    """Immutable configuration for :class:`~exectunnel.proxy.server.Socks5Server`.

    All fields are validated at construction time via
    :meth:`__post_init__`.

    Attributes:
        host: Bind address. Defaults to ``"127.0.0.1"``.
        port: Bind port in ``[1, 65535]``. Defaults to ``1080``.
        handshake_timeout: Max seconds for a SOCKS5 handshake. Must be
            positive.
        request_queue_capacity: Max buffered, fully-negotiated SOCKS5
            requests before backpressure. Must be ≥ 1.
        udp_relay_queue_capacity: Max buffered inbound datagrams per
            :class:`~exectunnel.proxy.udp_relay.UDPRelay` instance before
            dropping. Must be ≥ 1.
        queue_put_timeout: Max seconds to wait when enqueueing a
            completed handshake. Must be positive.
        udp_drop_warn_interval: Log a warning every *N* UDP queue-full
            drops. Must be ≥ 1.

    Raises:
        ConfigurationError: If any field fails validation.
    """

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT
    request_queue_capacity: int = DEFAULT_REQUEST_QUEUE_CAPACITY
    udp_relay_queue_capacity: int = DEFAULT_UDP_QUEUE_CAPACITY
    queue_put_timeout: float = DEFAULT_QUEUE_PUT_TIMEOUT
    udp_drop_warn_interval: int = DEFAULT_DROP_WARN_INTERVAL

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
            1 <= self.port <= MAX_TCP_UDP_PORT,
            field="port",
            value=self.port,
            expected="integer in [1, 65535]",
            hint="Use a valid TCP port number between 1 and 65535.",
        )
        _require(
            self.handshake_timeout > 0,
            field="handshake_timeout",
            value=self.handshake_timeout,
            expected="positive float (seconds)",
            hint="Set handshake_timeout to a positive number of seconds.",
        )
        _require(
            self.request_queue_capacity >= 1,
            field="request_queue_capacity",
            value=self.request_queue_capacity,
            expected="integer ≥ 1",
            hint="Set request_queue_capacity to at least 1.",
        )
        _require(
            self.udp_relay_queue_capacity >= 1,
            field="udp_relay_queue_capacity",
            value=self.udp_relay_queue_capacity,
            expected="integer ≥ 1",
            hint="Set udp_relay_queue_capacity to at least 1.",
        )
        _require(
            self.queue_put_timeout > 0,
            field="queue_put_timeout",
            value=self.queue_put_timeout,
            expected="positive float (seconds)",
            hint="Set queue_put_timeout to a positive number of seconds.",
        )
        _require(
            self.udp_drop_warn_interval >= 1,
            field="udp_drop_warn_interval",
            value=self.udp_drop_warn_interval,
            expected="integer ≥ 1",
            hint="Set udp_drop_warn_interval to at least 1.",
        )

    @property
    def is_loopback(self) -> bool:
        """Whether :attr:`host` resolves to a loopback address.

        Non-IP strings (e.g. ``"localhost"``) return ``False`` because
        they require DNS resolution.
        """
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False

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
