"""CLI override model.

:class:`CLIOverrides` carries the subset of fields that CLI flags can set.
It is passed to :meth:`~exectunnel.config.TunnelFile.resolve` as the
highest-priority layer, sitting above both the config file and env vars.

Only fields that are actually exposed as CLI flags appear here.  Internal
tunables (e.g. ``ack_timeout_warn_every``) are config-file/env-only and
are intentionally absent.

Validator messages use ``--flag-name`` style to match CLI output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import (
    AnyHttpUrl,
    AnyWebsocketUrl,
    BaseModel,
    ConfigDict,
    IPvAnyAddress,
    IPvAnyNetwork,
    model_validator,
)

from ._types import NonNegFloat, NonNegInt, PortInt, PosFloat, PosInt

__all__ = ["CLIOverrides"]


class CLIOverrides(BaseModel):
    """Typed representation of CLI flag values passed to ``resolve()``.

    All fields are ``Optional`` with ``None`` defaults.  ``None`` means
    "this flag was not passed on the command line — do not override."

    Attributes:
        wss_url:                      WebSocket URL (zero-config positional arg).
        socks_port:                   Local SOCKS5 listener port.
        socks_host:                   Local SOCKS5 bind address.
        socks_handshake_timeout:      Max seconds for SOCKS5 handshake.
        dns_upstream:                 Upstream DNS server IP.
        dns_local_port:               Local DNS forwarder port.
        dns_upstream_port:            Upstream DNS server port.
        dns_query_timeout:            End-to-end DNS query timeout.
        dns_max_inflight:             Max concurrent DNS queries.
        reconnect_max_retries:        Max reconnect attempts.
        reconnect_base_delay:         Initial reconnect back-off delay.
        reconnect_max_delay:          Maximum reconnect back-off delay.
        ping_interval:                Seconds between KEEPALIVE frames.
        send_timeout:                 WebSocket frame send timeout.
        conn_ack_timeout:             Seconds to wait for CONN_ACK.
        connect_max_pending:          Global in-flight CONNECT cap.
        connect_max_pending_per_host: Per-host in-flight CONNECT cap.
        ready_timeout:                Seconds to wait for AGENT_READY.
        bootstrap_delivery:           Agent delivery mode.
        bootstrap_fetch_url:          URL for fetch delivery mode.
        bootstrap_skip_if_present:    Skip delivery if agent exists.
        bootstrap_syntax_check:       Run syntax check after upload.
        bootstrap_agent_path:         Agent path on the pod.
        bootstrap_use_go_agent:       Use Go agent binary.
        bootstrap_go_agent_path:      Go agent path on the pod.
        no_default_exclude:           Clear RFC-1918 exclusion defaults.
        extra_excludes:               Additional CIDRs to bypass tunnel.
        ws_headers:                   Additional WebSocket headers.
        ssl_no_verify:                Disable TLS verification.
        ssl_ca_cert:                  Custom CA certificate path.
        ssl_cert:                     Client certificate path.
        ssl_key:                      Client key path.
        log_level:                    Log verbosity.
        log_format:                   Log output format.
        log_file:                     Log file path.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # ── Identity ──────────────────────────────────────────────────────────────

    wss_url: AnyWebsocketUrl | None = None
    socks_port: PortInt | None = None

    # ── SOCKS5 ────────────────────────────────────────────────────────────────

    socks_host: str | None = None
    socks_allow_non_loopback: bool | None = None
    socks_handshake_timeout: PosFloat | None = None
    socks_udp_associate_enabled: bool | None = None
    socks_udp_bind_host: str | None = None
    socks_udp_advertise_host: IPvAnyAddress | None = None

    # ── DNS ───────────────────────────────────────────────────────────────────

    dns_upstream: IPvAnyAddress | None = None
    dns_local_port: PortInt | None = None
    dns_upstream_port: PortInt | None = None
    dns_query_timeout: PosFloat | None = None
    dns_max_inflight: PosInt | None = None

    # ── Reconnect ─────────────────────────────────────────────────────────────

    reconnect_max_retries: NonNegInt | None = None
    reconnect_base_delay: PosFloat | None = None
    reconnect_max_delay: PosFloat | None = None

    # ── Transport ─────────────────────────────────────────────────────────────

    ping_interval: PosFloat | None = None
    send_timeout: PosFloat | None = None
    conn_ack_timeout: PosFloat | None = None
    connect_max_pending: PosInt | None = None
    connect_max_pending_per_host: PosInt | None = None
    ready_timeout: PosFloat | None = None
    udp_flow_idle_timeout: NonNegFloat | None = None

    # ── Bootstrap ─────────────────────────────────────────────────────────────

    bootstrap_delivery: Literal["upload", "fetch"] | None = None
    bootstrap_fetch_url: AnyHttpUrl | None = None
    bootstrap_skip_if_present: bool | None = None
    bootstrap_syntax_check: bool | None = None
    bootstrap_agent_path: str | None = None
    bootstrap_use_go_agent: bool | None = None
    bootstrap_go_agent_path: str | None = None

    # ── Routing ───────────────────────────────────────────────────────────────

    no_default_exclude: bool | None = None
    extra_excludes: list[IPvAnyNetwork] | None = None

    # ── Headers ───────────────────────────────────────────────────────────────

    ws_headers: dict[str, str] | None = None

    # ── SSL ───────────────────────────────────────────────────────────────────

    # Path (not FilePath) — CLI paths are validated at use-time, not parse-time,
    # to allow test construction without real files on disk.
    ssl_no_verify: bool | None = None
    ssl_ca_cert: Path | None = None
    ssl_cert: Path | None = None
    ssl_key: Path | None = None

    # ── Logging ───────────────────────────────────────────────────────────────

    log_level: Literal["debug", "info", "warning", "error"] | None = None
    log_format: Literal["text", "json"] | None = None
    log_file: Path | None = None

    # ── Validators ────────────────────────────────────────────────────────────

    @model_validator(mode="after")
    def _validate_ssl_constraints(self) -> Self:
        """Enforce SSL field mutual constraints.

        Rules:
        - ssl_cert and ssl_key must both be set or both absent.
        - ssl_no_verify cannot be combined with ssl_ca_cert or ssl_cert.

        Error messages use ``--flag-name`` style to match CLI output.
        """
        cert_set = self.ssl_cert is not None
        key_set = self.ssl_key is not None

        if cert_set != key_set:
            missing = "ssl_key" if cert_set else "ssl_cert"
            present = "ssl_cert" if cert_set else "ssl_key"
            raise ValueError(
                f"--{present.replace('_', '-')} is set but "
                f"--{missing.replace('_', '-')} is missing — "
                "both must be provided together."
            )

        if self.ssl_no_verify is True and (
            self.ssl_ca_cert is not None or self.ssl_cert is not None
        ):
            raise ValueError(
                "--ssl-no-verify cannot be combined with --ssl-ca-cert or --ssl-cert."
            )

        return self

    @model_validator(mode="after")
    def _validate_reconnect_delays(self) -> Self:
        """Ensure reconnect_base_delay < reconnect_max_delay when both are set."""
        base = self.reconnect_base_delay
        maximum = self.reconnect_max_delay
        if base is not None and maximum is not None and base >= maximum:
            raise ValueError(
                f"--reconnect-base-delay ({base}) must be less than "
                f"--reconnect-max-delay ({maximum})"
            )
        return self
