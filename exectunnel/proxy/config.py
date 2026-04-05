"""SOCKS5 server configuration.

:class:`Socks5ServerConfig` is the single source of truth for all tuneable
parameters of :class:`~exectunnel.proxy.server.Socks5Server`.  Validated at
construction time so that misconfiguration is caught before any socket is
opened.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from exectunnel.exceptions import ConfigurationError
from exectunnel.proxy._constants import (
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_QUEUE_CAPACITY,
    DROP_WARN_INTERVAL,
    LOOPBACK_ADDRS,
)

__all__: list[str] = ["Socks5ServerConfig"]


@dataclass(frozen=True, slots=True)
class Socks5ServerConfig:
    """Immutable configuration for :class:`~exectunnel.proxy.server.Socks5Server`.

    All fields are validated at construction time via ``__post_init__``.

    Attributes:
        host:               Bind address for the SOCKS5 listen socket.
                            Defaults to ``"127.0.0.1"``.
        port:               Bind port.  Must be in ``[1, 65535]``.
                            Defaults to ``1080``.
        handshake_timeout:  Maximum seconds allowed for a single SOCKS5
                            handshake before the connection is dropped.
                            Must be positive.  Defaults to
                            :data:`~exectunnel.proxy._constants.DEFAULT_HANDSHAKE_TIMEOUT`.
        queue_capacity:     Maximum number of completed
                            :class:`~exectunnel.proxy.request.Socks5Request`
                            objects to buffer before back-pressuring the
                            accept loop.  Must be ≥ 1.  Defaults to
                            :data:`~exectunnel.proxy._constants.DEFAULT_QUEUE_CAPACITY`.
        udp_drop_warn_interval: Log a warning every *N* UDP queue-full drops
                            to avoid log flooding.  Must be ≥ 1.  Defaults to
                            :data:`~exectunnel.proxy._constants.DROP_WARN_INTERVAL`.

    Raises:
        ConfigurationError: If any field fails validation.

    Example::

        cfg = Socks5ServerConfig(host="127.0.0.1", port=1080)
        async with Socks5Server(cfg) as server:
            async for req in server:
                asyncio.create_task(handle(req))
    """

    host: str = "127.0.0.1"
    port: int = 1080
    handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT
    queue_capacity: int = DEFAULT_QUEUE_CAPACITY
    udp_drop_warn_interval: int = DROP_WARN_INTERVAL

    def __post_init__(self) -> None:
        """Validate all fields, raising :class:`~exectunnel.exceptions.ConfigurationError`
        on the first violation found."""
        if not self.host:
            raise ConfigurationError(
                "Socks5ServerConfig.host must not be empty.",
                details={"field": "host", "value": self.host, "expected": "non-empty string"},
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
