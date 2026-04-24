"""Session-layer configuration dataclasses.

Declares exactly what the tunnel session consumes — flat, frozen,
no environment reading, no SSL logic.  All fields have documented defaults
sourced from :mod:`exectunnel.defaults`.
"""

from __future__ import annotations

import ipaddress
import ssl
from dataclasses import dataclass, field
from typing import Literal

from exectunnel.defaults import Defaults

from ._routing import get_default_exclusion_networks


@dataclass(slots=True, frozen=True)
class SessionConfig:
    """All tunables consumed by the tunnel session at the WebSocket/transport level.

    Attributes:
        wss_url:                 WebSocket URL of the Kubernetes exec endpoint.
        ws_headers:              Additional HTTP headers sent on the WebSocket
                                 upgrade request (e.g. ``Authorization``).
        ssl_context_override:    Explicit :class:`ssl.SSLContext` to use instead
                                 of the library default.  ``None`` defers to the
                                 ``websockets`` library defaults.
        version:                 Client version string sent to the agent for
                                 compatibility checking.
        reconnect_max_retries:   Maximum number of reconnect attempts before
                                 raising :exc:`~exectunnel.exceptions.ReconnectExhaustedError`.
        reconnect_base_delay:    Initial reconnect back-off delay in seconds.
        reconnect_max_delay:     Maximum reconnect back-off delay in seconds.
        ping_interval:           Seconds between KEEPALIVE frames sent to the
                                 agent to prevent NAT/proxy idle timeouts.
        send_timeout:            Maximum seconds to wait for a single WebSocket
                                 frame send before raising
                                 :exc:`~exectunnel.exceptions.WebSocketSendTimeoutError`.
        send_queue_cap:          Capacity of the bounded outbound data frame queue.
    """

    # ── WebSocket connection ──────────────────────────────────────────────────
    wss_url: str
    ws_headers: dict[str, str] = field(default_factory=dict)
    ssl_context_override: ssl.SSLContext | None = field(default=None, compare=False)
    version: str = "1.0"

    # ── Reconnect / transport ─────────────────────────────────────────────────
    reconnect_max_retries: int = Defaults.WS_RECONNECT_MAX_RETRIES
    reconnect_base_delay: float = Defaults.WS_RECONNECT_BASE_DELAY_SECS
    reconnect_max_delay: float = Defaults.WS_RECONNECT_MAX_DELAY_SECS
    ping_interval: float = Defaults.WS_PING_INTERVAL_SECS
    send_timeout: float = Defaults.WS_SEND_TIMEOUT_SECS
    send_queue_cap: int = Defaults.WS_SEND_QUEUE_CAP

    def ssl_context(self) -> ssl.SSLContext | None:
        """Return the explicit SSL context override, or ``None`` for library defaults.

        Returns:
            The configured :class:`ssl.SSLContext`, or ``None`` if the
            ``websockets`` library should select the context automatically.
        """
        return self.ssl_context_override


@dataclass(slots=True, frozen=True)
class TunnelConfig:
    """Tunnel-specific tunables consumed by bootstrap, dispatcher, DNS, and UDP layers.

    Attributes:
        socks_host:                      Hostname or IP for the local SOCKS5 listener.
        socks_port:                      Port for the local SOCKS5 listener.
        dns_upstream:                    Optional upstream DNS server IP forwarded
                                         through the tunnel (e.g. ``"10.96.0.10"``).
                                         ``None`` disables the DNS forwarder.
        dns_local_port:                  Local UDP port the DNS forwarder binds on
                                         ``127.0.0.1``.
        ready_timeout:                   Seconds to wait for the remote agent to
                                         emit ``AGENT_READY`` after exec.
        conn_ack_timeout:                Seconds to wait for the remote agent to
                                         acknowledge a ``CONN_OPEN`` per connection.
        exclude:                         CIDRs that bypass the tunnel and connect
                                         directly.  Defaults to RFC 1918 + loopback.
        ack_timeout_warn_every:          Log a warning every N ACK timeouts.
        ack_timeout_window_secs:         Rolling window (seconds) for ACK timeout
                                         threshold counting.
        ack_timeout_reconnect_threshold: Number of ACK timeouts within the window
                                         that triggers a forced reconnect.
        connect_max_pending:             Global cap on simultaneous in-flight
                                         ``CONN_OPEN`` frames.
        connect_max_pending_per_host:    Per-host cap on simultaneous in-flight
                                         ``CONN_OPEN`` frames.
        pre_ack_buffer_cap_bytes:        Pre-ACK receive buffer cap in bytes.
        connect_pace_interval_secs:      Minimum seconds between successive
                                         ``CONN_OPEN`` frames to the same host.
        bootstrap_delivery:              How the agent payload is delivered.
                                         ``"upload"`` streams base64 chunks via
                                         ``printf``; ``"fetch"`` downloads via
                                         ``curl``/``wget``.
        bootstrap_fetch_url:             Raw URL used when ``bootstrap_delivery="fetch"``.
        bootstrap_skip_if_present:       When ``True``, skip delivery if the agent
                                         file already exists on the pod.
        bootstrap_syntax_check:          When ``True``, run ``ast.parse`` on the
                                         agent before exec'ing it.
        bootstrap_agent_path:            Absolute path of the agent script inside
                                         the pod.
        bootstrap_syntax_ok_sentinel:    Path of the syntax-OK sentinel file written
                                         after a successful syntax check.
        bootstrap_use_go_agent:          When ``True``, upload and run the pre-built
                                         Go agent binary instead of ``agent.py``.
        bootstrap_go_agent_path:         Absolute path of the Go agent binary inside
                                         the pod.
        socks_handshake_timeout:         Maximum seconds to complete a SOCKS5
                                         handshake before dropping the connection.
        socks_request_queue_cap:         ``asyncio.Queue`` capacity for completed
                                         handshakes awaiting dispatch.
        socks_queue_put_timeout:         Maximum seconds to enqueue a completed
                                         handshake before dropping it.
        udp_relay_queue_cap:             ``asyncio.Queue`` capacity for inbound
                                         datagrams per ``UDP_ASSOCIATE`` session.
        udp_drop_warn_every:             Log a warning every N UDP queue-full drops.
        udp_pump_poll_timeout:           Polling interval for the UDP ASSOCIATE pump
                                         loop in seconds.
        udp_direct_recv_timeout:         Timeout for receiving a UDP response on
                                         directly-connected (excluded) hosts.
        dns_max_inflight:                Maximum concurrent in-flight DNS queries.
        dns_upstream_port:               Upstream DNS server port (default: 53).
        dns_query_timeout:               End-to-end DNS query timeout through the
                                         tunnel in seconds.
    """

    socks_host: str = Defaults.SOCKS_DEFAULT_HOST
    socks_port: int = Defaults.SOCKS_DEFAULT_PORT
    dns_upstream: str | None = None
    dns_local_port: int = Defaults.DNS_LOCAL_PORT
    ready_timeout: float = Defaults.READY_TIMEOUT_SECS
    conn_ack_timeout: float = Defaults.CONN_ACK_TIMEOUT_SECS
    exclude: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = field(
        default_factory=get_default_exclusion_networks
    )
    ack_timeout_warn_every: int = Defaults.ACK_TIMEOUT_WARN_EVERY
    ack_timeout_window_secs: float = Defaults.ACK_TIMEOUT_WINDOW_SECS
    ack_timeout_reconnect_threshold: int = Defaults.ACK_TIMEOUT_RECONNECT_THRESHOLD
    connect_max_pending: int = Defaults.CONNECT_MAX_PENDING
    connect_max_pending_per_host: int = Defaults.CONNECT_MAX_PENDING_PER_HOST
    pre_ack_buffer_cap_bytes: int = Defaults.PRE_ACK_BUFFER_CAP_BYTES
    connect_pace_interval_secs: float = Defaults.CONNECT_PACE_INTERVAL_SECS
    bootstrap_delivery: Literal["upload", "fetch"] = "fetch"
    bootstrap_fetch_url: str = Defaults.BOOTSTRAP_FETCH_AGENT_URL
    bootstrap_skip_if_present: bool = False
    bootstrap_syntax_check: bool = False
    bootstrap_agent_path: str = Defaults.BOOTSTRAP_AGENT_PATH
    bootstrap_syntax_ok_sentinel: str = Defaults.BOOTSTRAP_SYNTAX_OK_SENTINEL
    bootstrap_use_go_agent: bool = False
    bootstrap_go_agent_path: str = Defaults.BOOTSTRAP_GO_AGENT_PATH
    socks_handshake_timeout: float = Defaults.HANDSHAKE_TIMEOUT_SECS
    socks_request_queue_cap: int = Defaults.SOCKS_REQUEST_QUEUE_CAP
    socks_queue_put_timeout: float = Defaults.SOCKS_QUEUE_PUT_TIMEOUT_SECS
    udp_relay_queue_cap: int = Defaults.UDP_RELAY_QUEUE_CAP
    udp_drop_warn_every: int = Defaults.UDP_WARN_EVERY
    udp_pump_poll_timeout: float = Defaults.UDP_PUMP_POLL_TIMEOUT_SECS
    udp_direct_recv_timeout: float = Defaults.UDP_DIRECT_RECV_TIMEOUT_SECS
    dns_max_inflight: int = Defaults.DNS_MAX_INFLIGHT
    dns_upstream_port: int = Defaults.DNS_UPSTREAM_PORT
    dns_query_timeout: float = Defaults.DNS_QUERY_TIMEOUT_SECS
