"""SOCKS5 server configuration.

:class:`Socks5ServerConfig` is the single source of truth for all tuneable
parameters of :class:`~exectunnel.proxy.server.Socks5Server`.  Validated at
construction time so that misconfiguration is caught before any socket is
opened.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from exectunnel.exceptions import ConfigurationError

from ._constants import (
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_QUEUE_CAPACITY,
    DROP_WARN_INTERVAL,
    QUEUE_PUT_TIMEOUT,
)

__all__: list[str] = ["Socks5ServerConfig"]


@dataclass(frozen=True, slots=True)
class Socks5ServerConfig:
    """Immutable configuration for :class:`~exectunnel.proxy.server.Socks5Server`.

    All fields are validated at construction time via ``__post_init__``.

    Attributes:
        host:                     Bind address.  Defaults to ``"127.0.0.1"``.
        port:                     Bind port in ``[1, 65535]``.  Defaults to ``1080``.
        handshake_timeout:        Max seconds for a SOCKS5 handshake.  Positive.
        request_queue_capacity:   Max buffered SOCKS5 requests before backpressure.
                                  ≥ 1.
        udp_relay_queue_capacity: Max buffered inbound datagrams per
                                  :class:`~exectunnel.proxy.udp_relay.UdpRelay`
                                  instance before dropping.  ≥ 1.
        queue_put_timeout:        Max seconds to wait when enqueueing a completed
                                  handshake.  Positive.
        udp_drop_warn_interval:   Log a warning every *N* UDP queue-full drops.  ≥ 1.

    Raises:
        ConfigurationError: If any field fails validation.
    """

    host: str = "127.0.0.1"
    port: int = 1080
    handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT
    request_queue_capacity: int = DEFAULT_QUEUE_CAPACITY
    udp_relay_queue_capacity: int = DEFAULT_QUEUE_CAPACITY
    queue_put_timeout: float = QUEUE_PUT_TIMEOUT
    udp_drop_warn_interval: int = DROP_WARN_INTERVAL

    def __post_init__(self) -> None:
        """Validate all fields."""
        validators: list[tuple[bool, str, object, str, str]] = [
            (
                not self.host,
                "host",
                self.host,
                "non-empty string",
                "Provide a valid bind address, e.g. '127.0.0.1' or '::1'.",
            ),
            (
                not (1 <= self.port <= 65_535),
                "port",
                self.port,
                "integer in [1, 65535]",
                "Use a valid TCP port number between 1 and 65535.",
            ),
            (
                self.handshake_timeout <= 0,
                "handshake_timeout",
                self.handshake_timeout,
                "positive float (seconds)",
                "Set handshake_timeout to a positive number of seconds.",
            ),
            (
                self.request_queue_capacity < 1,
                "request_queue_capacity",
                self.request_queue_capacity,
                "integer ≥ 1",
                "Set request_queue_capacity to at least 1.",
            ),
            (
                self.udp_relay_queue_capacity < 1,
                "udp_relay_queue_capacity",
                self.udp_relay_queue_capacity,
                "integer ≥ 1",
                "Set udp_relay_queue_capacity to at least 1.",
            ),
            (
                self.queue_put_timeout <= 0,
                "queue_put_timeout",
                self.queue_put_timeout,
                "positive float (seconds)",
                "Set queue_put_timeout to a positive number of seconds.",
            ),
            (
                self.udp_drop_warn_interval < 1,
                "udp_drop_warn_interval",
                self.udp_drop_warn_interval,
                "integer ≥ 1",
                "Set udp_drop_warn_interval to at least 1.",
            ),
        ]

        for failed, field, value, expected, hint in validators:
            if failed:
                raise ConfigurationError(
                    f"Socks5ServerConfig.{field} {value!r} is invalid.",
                    details={"field": field, "value": value, "expected": expected},
                    hint=hint,
                )

    @property
    def is_loopback(self) -> bool:
        """``True`` when :attr:`host` is a loopback address.

        Non-IP strings (e.g. ``"localhost"``) return ``False`` because they
        depend on DNS resolution.
        """
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False

    @property
    def url(self) -> str:
        """Human-readable ``tcp://host:port`` string for logging."""
        return f"tcp://{self.host}:{self.port}"
