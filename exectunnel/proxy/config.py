"""SOCKS5 server configuration.

:class:`Socks5ServerConfig` is the single source of truth for all tuneable
parameters of :class:`~exectunnel.proxy.server.Socks5Server`.  Validated at
construction time so that misconfiguration is caught before any socket is
opened.
"""

from __future__ import annotations

from dataclasses import dataclass

from exectunnel.exceptions import ConfigurationError

from ._constants import (
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_QUEUE_CAPACITY,
    DROP_WARN_INTERVAL,
    LOOPBACK_ADDRS,
    QUEUE_PUT_TIMEOUT,
)

__all__: list[str] = ["Socks5ServerConfig"]


@dataclass(frozen=True, slots=True)
class Socks5ServerConfig:
    """Immutable configuration for :class:`~exectunnel.proxy.server.Socks5Server`.

    All fields are validated at construction time via ``__post_init__``.

    Attributes:
        host:                   Bind address.  Defaults to ``"127.0.0.1"``.
        port:                   Bind port in ``[1, 65535]``.  Defaults to ``1080``.
        handshake_timeout:      Max seconds for a SOCKS5 handshake.  Positive.
        queue_capacity:         Max buffered requests before backpressure.  ≥ 1.
        queue_put_timeout:      Max seconds to wait when enqueueing a completed
                                handshake.  Positive.
        udp_drop_warn_interval: Log a warning every *N* UDP queue-full drops.  ≥ 1.

    Raises:
        ConfigurationError: If any field fails validation.
    """

    host: str = "127.0.0.1"
    port: int = 1080
    handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT
    queue_capacity: int = DEFAULT_QUEUE_CAPACITY
    queue_put_timeout: float = QUEUE_PUT_TIMEOUT
    udp_drop_warn_interval: int = DROP_WARN_INTERVAL

    def __post_init__(self) -> None:
        """Validate all fields."""
        if not self.host:
            raise ConfigurationError(
                "Socks5ServerConfig.host must not be empty.",
                details={
                    "field": "host",
                    "value": self.host,
                    "expected": "non-empty string",
                },
                hint="Provide a valid bind address, e.g. '127.0.0.1' or '::1'.",
            )

        if not (1 <= self.port <= 65_535):
            raise ConfigurationError(
                f"Socks5ServerConfig.port {self.port!r} is outside [1, 65535].",
                details={
                    "field": "port",
                    "value": self.port,
                    "expected": "integer in [1, 65535]",
                },
                hint="Use a valid TCP port number between 1 and 65535.",
            )

        if self.handshake_timeout <= 0:
            raise ConfigurationError(
                f"Socks5ServerConfig.handshake_timeout {self.handshake_timeout!r} "
                "must be a positive number.",
                details={
                    "field": "handshake_timeout",
                    "value": self.handshake_timeout,
                    "expected": "positive float (seconds)",
                },
                hint="Set handshake_timeout to a positive number of seconds.",
            )

        if self.queue_capacity < 1:
            raise ConfigurationError(
                f"Socks5ServerConfig.queue_capacity {self.queue_capacity!r} must be ≥ 1.",
                details={
                    "field": "queue_capacity",
                    "value": self.queue_capacity,
                    "expected": "integer ≥ 1",
                },
                hint="Set queue_capacity to at least 1.",
            )

        if self.queue_put_timeout <= 0:
            raise ConfigurationError(
                f"Socks5ServerConfig.queue_put_timeout {self.queue_put_timeout!r} "
                "must be a positive number.",
                details={
                    "field": "queue_put_timeout",
                    "value": self.queue_put_timeout,
                    "expected": "positive float (seconds)",
                },
                hint="Set queue_put_timeout to a positive number of seconds.",
            )

        if self.udp_drop_warn_interval < 1:
            raise ConfigurationError(
                f"Socks5ServerConfig.udp_drop_warn_interval "
                f"{self.udp_drop_warn_interval!r} must be ≥ 1.",
                details={
                    "field": "udp_drop_warn_interval",
                    "value": self.udp_drop_warn_interval,
                    "expected": "integer ≥ 1",
                },
                hint="Set udp_drop_warn_interval to at least 1.",
            )

    @property
    def is_loopback(self) -> bool:
        """``True`` when :attr:`host` is a known loopback address."""
        return self.host in LOOPBACK_ADDRS

    @property
    def url(self) -> str:
        """Human-readable ``tcp://host:port`` string for logging."""
        return f"tcp://{self.host}:{self.port}"
