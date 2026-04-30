"""Per-tunnel configuration entry.

Each ``[[tunnels]]`` block in the config file maps to one :class:`TunnelEntry`.
Identity fields (``name``, ``wss_url``, ``socks_port``) are required.
Every other field is optional and falls back to :class:`~exectunnel.config.GlobalDefaults`
during resolution.

Note on SSL path fields
-----------------------
``ssl_ca_cert``, ``ssl_cert``, and ``ssl_key`` use ``FilePath`` which validates
that the file **exists on disk at parse time**.  This is intentional for
config-file paths (you want to catch missing certs early) but means tests must
provide real files or use ``model_construct()`` to bypass validation.
"""

from __future__ import annotations

from typing import Self

from pydantic import AnyWebsocketUrl, Field, FilePath, field_validator, model_validator

from ._mixin import TunnelOverrideMixin
from ._types import PortInt

__all__ = ["TunnelEntry"]


class TunnelEntry(TunnelOverrideMixin):
    """A single tunnel definition.

    Attributes:
        name:          Unique human-readable identifier used in logs and CLI selectors.
        wss_url:       Kubernetes exec WebSocket endpoint.
        socks_port:    Local port for the SOCKS5 listener.  Must be unique across
                       all tunnel entries.
        enabled:       Whether this tunnel starts when running all tunnels.
                       Inherits ``GlobalDefaults.default_enabled`` if not set.
        ssl_no_verify: Disable TLS certificate verification.  Insecure — dev only.
        ssl_ca_cert:   Path to a custom CA certificate bundle (PEM).
        ssl_cert:      Path to a client certificate (PEM).  Requires ``ssl_key``.
        ssl_key:       Path to the private key for ``ssl_cert`` (PEM).
    """

    # ── Identity (required) ───────────────────────────────────────────────────

    name: str = Field(
        description="Unique tunnel name used in logs and CLI selectors.",
    )
    wss_url: AnyWebsocketUrl = Field(
        description="Kubernetes exec WebSocket endpoint (wss:// or ws://).",
    )
    socks_port: PortInt = Field(
        description="Local SOCKS5 listener port.  Must be unique across all tunnels.",
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    enabled: bool | None = Field(
        default=None,
        description=(
            "Whether this tunnel is started by default. "
            "Inherits GlobalDefaults.default_enabled when None."
        ),
    )

    # ── SSL (tunnel-only, not in global defaults) ─────────────────────────────

    ssl_no_verify: bool | None = Field(
        default=None,
        description="Disable TLS certificate verification. INSECURE — dev only.",
    )
    ssl_ca_cert: FilePath | None = Field(
        default=None,
        description="Path to a custom CA certificate bundle (PEM format).",
    )
    ssl_cert: FilePath | None = Field(
        default=None,
        description="Path to a client certificate (PEM). Requires ssl_key.",
    )
    ssl_key: FilePath | None = Field(
        default=None,
        description="Path to the private key for ssl_cert (PEM).",
    )

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("name", mode="after")
    @classmethod
    def _validate_name_not_empty(cls, v: str) -> str:
        """Tunnel name must be a non-empty, non-whitespace string."""
        if not v.strip():
            raise ValueError("Tunnel name must not be empty or whitespace.")
        return v.strip()

    @model_validator(mode="after")
    def _validate_ssl_constraints(self) -> Self:
        """Enforce SSL field mutual constraints in a single pass.

        Rules:
        - ssl_cert and ssl_key must both be set or both absent.
        - ssl_no_verify cannot be combined with ssl_ca_cert or ssl_cert.
        """
        cert_set = self.ssl_cert is not None
        key_set = self.ssl_key is not None

        if cert_set != key_set:
            missing = "ssl_key" if cert_set else "ssl_cert"
            present = "ssl_cert" if cert_set else "ssl_key"
            raise ValueError(
                f"{present} is set but {missing} is missing — "
                "both must be provided together."
            )

        if self.ssl_no_verify is True and (
            self.ssl_ca_cert is not None or self.ssl_cert is not None
        ):
            raise ValueError(
                "ssl_no_verify cannot be combined with ssl_ca_cert or ssl_cert."
            )

        return self
