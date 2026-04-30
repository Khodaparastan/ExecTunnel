"""Shared overridable tunnel fields.

Every field here can be set at the ``[global]`` level **or** overridden
per-``[[tunnels]]`` entry.  ``None`` is the universal sentinel meaning
"not set at this layer — inherit from the layer below".

Validators defined here apply to both :class:`~exectunnel.config.GlobalDefaults`
and :class:`~exectunnel.config.TunnelEntry`.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    IPvAnyAddress,
    IPvAnyNetwork,
    model_validator,
)

from ._types import NonNegFloat, NonNegInt, PortInt, PosFloat, PosInt

__all__ = ["TunnelOverrideMixin"]


class TunnelOverrideMixin(BaseModel):
    """All fields that can be overridden at both global and per-tunnel scope.

    All fields are ``Optional`` with a ``None`` default.  ``None`` means
    "not configured at this layer" and is resolved to the next layer down
    by :meth:`~exectunnel.config.TunnelFile.resolve`.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # ── WebSocket / reconnect ─────────────────────────────────────────────────

    reconnect_max_retries: NonNegInt | None = Field(
        default=None,
        description="Maximum reconnect attempts before raising ReconnectExhaustedError.",
    )
    reconnect_base_delay: PosFloat | None = Field(
        default=None,
        description="Initial reconnect back-off delay in seconds.",
    )
    reconnect_max_delay: PosFloat | None = Field(
        default=None,
        description="Maximum reconnect back-off delay in seconds.",
    )
    ping_interval: PosFloat | None = Field(
        default=None,
        description="Seconds between KEEPALIVE frames sent to the agent.",
    )
    send_timeout: PosFloat | None = Field(
        default=None,
        description="Maximum seconds to wait for a single WebSocket frame send.",
    )
    send_queue_cap: PosInt | None = Field(
        default=None,
        description="Capacity of the bounded outbound data frame queue.",
    )

    # ── SOCKS5 ────────────────────────────────────────────────────────────────

    socks_host: str | None = Field(
        default=None,
        description="Bind address for the local SOCKS5 listener.",
    )
    socks_allow_non_loopback: bool | None = Field(
        default=None,
        description=(
            "Allow the SOCKS5 TCP listener to bind to non-loopback addresses. "
            "NO_AUTH proxy exposure; protect with firewall/LB auth."
        ),
    )
    socks_handshake_timeout: PosFloat | None = Field(
        default=None,
        description="Max seconds to complete a SOCKS5 handshake before dropping.",
    )
    socks_request_queue_cap: PosInt | None = Field(
        default=None,
        description="asyncio.Queue capacity for completed handshakes awaiting dispatch.",
    )
    socks_queue_put_timeout: PosFloat | None = Field(
        default=None,
        description="Max seconds to enqueue a completed handshake before dropping.",
    )
    socks_udp_associate_enabled: bool | None = Field(
        default=None,
        description="Whether the local SOCKS5 server accepts UDP_ASSOCIATE.",
    )
    socks_udp_bind_host: str | None = Field(
        default=None,
        description="Local bind address for per-association SOCKS5 UDP relays.",
    )
    socks_udp_advertise_host: IPvAnyAddress | None = Field(
        default=None,
        description="IP address advertised to SOCKS5 clients for UDP_ASSOCIATE.",
    )

    # ── Bootstrap ─────────────────────────────────────────────────────────────

    bootstrap_delivery: Literal["upload", "fetch"] | None = Field(
        default=None,
        description="Agent delivery mode: 'upload' streams base64; 'fetch' uses curl/wget.",
    )
    bootstrap_fetch_url: AnyHttpUrl | None = Field(
        default=None,
        description="URL to fetch the agent from when bootstrap_delivery='fetch'.",
    )
    bootstrap_skip_if_present: bool | None = Field(
        default=None,
        description="Skip delivery if the agent file already exists on the pod.",
    )
    bootstrap_syntax_check: bool | None = Field(
        default=None,
        description="Run ast.parse on the agent before exec'ing it.",
    )
    bootstrap_agent_path: str | None = Field(
        default=None,
        description="Absolute path of the agent script inside the pod.",
    )
    bootstrap_syntax_ok_sentinel: str | None = Field(
        default=None,
        description="Path of the syntax-OK sentinel file written after a successful check.",
    )
    bootstrap_use_go_agent: bool | None = Field(
        default=None,
        description="Upload and run the pre-built Go agent binary instead of agent.py.",
    )
    bootstrap_go_agent_path: str | None = Field(
        default=None,
        description="Absolute path of the Go agent binary inside the pod.",
    )

    # ── Transport / ACK ───────────────────────────────────────────────────────

    ready_timeout: PosFloat | None = Field(
        default=None,
        description="Seconds to wait for AGENT_READY after exec.",
    )
    conn_ack_timeout: PosFloat | None = Field(
        default=None,
        description="Seconds to wait for the agent to acknowledge a CONN_OPEN.",
    )
    ack_timeout_warn_every: PosInt | None = Field(
        default=None,
        description="Log a warning every N ACK timeouts.",
    )
    ack_timeout_window_secs: PosFloat | None = Field(
        default=None,
        description="Rolling window in seconds for ACK timeout threshold counting.",
    )
    ack_timeout_reconnect_threshold: PosInt | None = Field(
        default=None,
        description="ACK timeouts within the window that triggers a forced reconnect.",
    )
    connect_max_pending: PosInt | None = Field(
        default=None,
        description="Global cap on simultaneous in-flight CONN_OPEN frames.",
    )
    connect_max_pending_per_host: PosInt | None = Field(
        default=None,
        description="Per-host cap on simultaneous in-flight CONN_OPEN frames.",
    )
    pre_ack_buffer_cap_bytes: PosInt | None = Field(
        default=None,
        description="Pre-ACK receive buffer cap in bytes.",
    )
    connect_pace_interval_secs: NonNegFloat | None = Field(
        default=None,
        description="Minimum seconds between successive CONN_OPEN frames to the same host.",
    )

    # ── UDP ───────────────────────────────────────────────────────────────────

    udp_relay_queue_cap: PosInt | None = Field(
        default=None,
        description="asyncio.Queue capacity for inbound datagrams per UDP_ASSOCIATE session.",
    )
    udp_drop_warn_every: PosInt | None = Field(
        default=None,
        description="Log a warning every N UDP queue-full drops.",
    )
    udp_pump_poll_timeout: PosFloat | None = Field(
        default=None,
        description="Polling interval for the UDP ASSOCIATE pump loop in seconds.",
    )
    udp_direct_recv_timeout: PosFloat | None = Field(
        default=None,
        description="Timeout for receiving a UDP response on directly-connected hosts.",
    )
    udp_flow_idle_timeout: NonNegFloat | None = Field(
        default=None,
        description="Idle timeout in seconds for tunneled UDP flows. 0 disables reaping.",
    )

    # ── DNS ───────────────────────────────────────────────────────────────────

    dns_upstream: IPvAnyAddress | None = Field(
        default=None,
        description="IP of the upstream DNS server forwarded through the tunnel.",
    )
    dns_local_port: PortInt | None = Field(
        default=None,
        description="Local UDP port the DNS forwarder binds on 127.0.0.1.",
    )
    dns_upstream_port: PortInt | None = Field(
        default=None,
        description="Upstream DNS server port.",
    )
    dns_max_inflight: PosInt | None = Field(
        default=None,
        description="Maximum concurrent in-flight DNS queries.",
    )
    dns_query_timeout: PosFloat | None = Field(
        default=None,
        description="End-to-end DNS query timeout through the tunnel in seconds.",
    )

    # ── Routing ───────────────────────────────────────────────────────────────

    no_default_exclude: bool | None = Field(
        default=None,
        description="Clear the RFC-1918 + loopback exclusion defaults.",
    )
    extra_excludes: list[IPvAnyNetwork] | None = Field(
        default=None,
        description=(
            "CIDRs that bypass the tunnel and connect directly. "
            "When set at tunnel level, replaces the global list entirely."
        ),
    )

    # ── Headers ───────────────────────────────────────────────────────────────

    ws_headers: dict[str, str] | None = Field(
        default=None,
        description="Additional HTTP headers sent on the WebSocket upgrade request.",
    )

    # ── Validators ────────────────────────────────────────────────────────────

    @model_validator(mode="after")
    def _validate_reconnect_delays(self) -> Self:
        """Ensure reconnect_base_delay < reconnect_max_delay when both are set."""
        base = self.reconnect_base_delay
        maximum = self.reconnect_max_delay
        if base is not None and maximum is not None and base >= maximum:
            raise ValueError(
                f"reconnect_base_delay ({base}) must be less than "
                f"reconnect_max_delay ({maximum})"
            )
        return self
