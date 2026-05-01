"""All numeric/string defaults, split by domain.

Design rules
------------
* Every constant is typed explicitly.
* Units are encoded in the name: ``_SECS``, ``_MS``, ``_BYTES``, ``_CHARS``.
* Values are chosen for Kubernetes exec/WebSocket tunnels, where all streams
  share one constrained stdout/stdin path.
* No constant value is derived from another constant at module import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

__all__ = ["Defaults"]


@dataclass(frozen=True, slots=True)
class Defaults:
    # ── WebSocket / bridge ────────────────────────────────────────────────────

    # Application-level keepalive interval sent through the tunnel.
    #
    # websockets' built-in ping is disabled in the session layer because its
    # background task can compete with tunnel frames for the internal write lock
    # under sustained load.
    #
    # 20s is below common proxy/NAT idle floors of 30–60s while remaining above
    # WS_SEND_TIMEOUT_SECS so a slow keepalive send does not dominate teardown.
    WS_PING_INTERVAL_SECS: ClassVar[float] = 20.0

    # Maximum time for one WebSocket frame send.
    #
    # Over Kubernetes exec or a reverse-proxied exec bridge, a single send
    # taking longer than 15s usually indicates a wedged downstream path.
    WS_SEND_TIMEOUT_SECS: ClassVar[float] = 15.0

    # Bounded outbound data-frame queue.
    #
    # Control frames use a separate unbounded priority queue. With the default
    # PIPE_READ_CHUNK_BYTES of 8192, this is approximately 4 MiB of raw payload,
    # plus base64/string overhead, which is acceptable for a local client.
    WS_SEND_QUEUE_CAP: ClassVar[int] = 512

    # Reconnect policy.
    #
    # 10 retries with exponential backoff capped at 15s gives roughly a couple
    # of minutes of recovery window with jitter, enough for transient API server
    # restarts and proxy reloads.
    WS_RECONNECT_MAX_RETRIES: ClassVar[int] = 10
    WS_RECONNECT_BASE_DELAY_SECS: ClassVar[float] = 1.0
    WS_RECONNECT_MAX_DELAY_SECS: ClassVar[float] = 15.0

    # Private-use RFC 6455 close code.
    #
    # 4001 means this client intentionally closed the tunnel because the agent
    # health policy requested a reconnect. Do not use 1011 here; 1011 implies
    # a remote/internal server failure and makes logs misleading.
    WS_CLOSE_CODE_UNHEALTHY: ClassVar[int] = 4001

    # ── Tunnel frame sizing ───────────────────────────────────────────────────

    # Maximum full tunnel frame line length accepted by the client parser.
    #
    # 32 KiB TCP payload → ~43.7 KiB base64 frame.
    # 65,535-byte UDP payload → ~87.5 KiB base64 frame.
    # 262 KiB leaves safe headroom while still bounding memory exposure.
    TUNNEL_MAX_FRAME_CHARS: ClassVar[int] = 262_144

    # Maximum unterminated receive buffer before the receiver treats it as a
    # protocol violation. Must be at least TUNNEL_MAX_FRAME_CHARS in practice;
    # kept as an independent literal by design.
    TUNNEL_MAX_UNTERMINATED_CHARS: ClassVar[int] = 262_144

    # ── TCP connection / ACK hardening ────────────────────────────────────────

    # Global cap on simultaneous in-flight CONN_OPEN frames.
    #
    # Kubernetes exec/WebSocket is a single multiplexed pipe; 128+ pending opens
    # creates ACK storms and worsens stdout head-of-line blocking. 64 is a safer
    # production default. Browser-heavy deployments may prefer 32.
    CONNECT_MAX_PENDING: ClassVar[int] = 64

    # Per-host in-flight CONN_OPEN cap.
    #
    # Prevents one destination from consuming all global slots.
    CONNECT_MAX_PENDING_PER_HOST: ClassVar[int] = 8

    # Small per-host pacing interval for CONN_OPEN frames.
    #
    # 20ms smooths connection bursts without adding meaningful latency. This is
    # intentionally not a multi-second delay.
    CONNECT_PACE_INTERVAL_SECS: ClassVar[float] = 0.02

    # Maximum random jitter added to per-host pacing.
    CONNECT_PACE_JITTER_CAP_SECS: ClassVar[float] = 0.02

    # Timeout waiting for the agent to ACK a CONN_OPEN.
    #
    # ACK includes agent-side DNS resolution and TCP connect from inside the pod.
    # Must be greater than agent TCP connect timeout plus tunnel/proxy latency.
    CONN_ACK_TIMEOUT_SECS: ClassVar[float] = 30.0

    # Bytes buffered from the local client before CONN_ACK arrives.
    #
    # 64 KiB covers TLS ClientHello plus early HTTP request data without allowing
    # unbounded pre-ACK memory growth.
    PRE_ACK_BUFFER_CAP_BYTES: ClassVar[int] = 65_536

    # Per-connection queue capacity for decoded DATA frames waiting to be written
    # to the local TCP socket.
    TCP_INBOUND_QUEUE_CAP: ClassVar[int] = 1_024

    # ── SOCKS5 handshake / listener ───────────────────────────────────────────

    # Maximum time for local SOCKS5 greeting and request negotiation.
    HANDSHAKE_TIMEOUT_SECS: ClassVar[float] = 30.0

    # Default bind address for the local SOCKS5 proxy.
    #
    # Loopback-only by default to avoid accidental open-proxy exposure.
    SOCKS_DEFAULT_HOST: ClassVar[str] = "127.0.0.1"

    # IANA-registered SOCKS port.
    SOCKS_DEFAULT_PORT: ClassVar[int] = 1080

    # Completed SOCKS5 handshakes awaiting dispatch.
    #
    # 256 absorbs short browser bursts without allowing unbounded local memory
    # growth if the tunnel is slow.
    SOCKS_REQUEST_QUEUE_CAP: ClassVar[int] = 256

    # Maximum time to enqueue a completed SOCKS request before dropping it.
    SOCKS_QUEUE_PUT_TIMEOUT_SECS: ClassVar[float] = 5.0

    # ── Bootstrap ─────────────────────────────────────────────────────────────

    # Maximum time to wait for AGENT_READY after exec.
    READY_TIMEOUT_SECS: ClassVar[float] = 30.0

    # Maximum number of pre-ready diagnostic lines retained for errors.
    BOOTSTRAP_DIAG_MAX_LINES: ClassVar[int] = 20

    # Base64 upload chunk size for shell delivery.
    BOOTSTRAP_CHUNK_SIZE_CHARS: ClassVar[int] = 4_096

    # Restricted-environment safe default.
    #
    # "upload" requires only shell + Python/base64 fallback inside the pod.
    # "fetch" requires pod egress and should be opt-in.
    BOOTSTRAP_DELIVERY: ClassVar[Literal["fetch", "upload"]] = "fetch"

    # Optional raw URL used when BOOTSTRAP_DELIVERY is "fetch".
    #
    # Production deployments should pin this to an immutable commit/object URL
    # or serve it from an internal trusted bucket.
    BOOTSTRAP_FETCH_AGENT_URL: ClassVar[str] = (
        "https://pub-0047b2c90ee14a2fbbcb586f5f1ebbef.r2.dev/agent.py"
    )

    # Kept for compatibility with the current bootstrapper fetch polling path.
    # If using the atomic checked-fetch bootstrap refactor, use this as the
    # total fetch command timeout.
    BOOTSTRAP_FETCH_FETCH_DELAY_SECS: ClassVar[float] = 30.0

    # Kept for compatibility with the current polling fetch path.
    BOOTSTRAP_FETCH_FETCH_POLL_SECS: ClassVar[float] = 0.5

    # Path of the syntax-OK sentinel inside the pod.
    BOOTSTRAP_SYNTAX_OK_SENTINEL: ClassVar[str] = "/tmp/exectunnel_agent.syntax_ok"  # noqa: S108

    # Path of the Python agent inside the pod.
    BOOTSTRAP_AGENT_PATH: ClassVar[str] = "/tmp/exectunnel_agent.py"  # noqa: S108

    # Path of the optional Go agent binary inside the pod.
    BOOTSTRAP_GO_AGENT_PATH: ClassVar[str] = "/tmp/exectunnel_agent"  # noqa: S108

    # ── TCP pipe sizing ───────────────────────────────────────────────────────

    # Read size for local upstream copy and direct TCP pipe.
    #
    # 8192 bytes produces ~10.9 KiB DATA frames after base64, small enough to
    # avoid severe exec/WebSocket stdout head-of-line blocking while preserving
    # reasonable throughput. The old 6108-byte value only made sense with an
    # 8192-character parser limit.
    PIPE_READ_CHUNK_BYTES: ClassVar[int] = 8_192

    # ── UDP / DNS ─────────────────────────────────────────────────────────────

    # Per-UDP-flow inbound queue capacity.
    #
    # 32 items absorbs DNS response bursts and short QUIC handshake bursts.
    UDP_INBOUND_QUEUE_CAP: ClassVar[int] = 32

    # Queue capacity for parsed SOCKS5 UDP datagrams awaiting tunnel dispatch.
    UDP_RELAY_QUEUE_CAP: ClassVar[int] = 64

    # Emit one warning for the first drop and every N drops afterward.
    UDP_WARN_EVERY: ClassVar[int] = 1_000

    # Poll interval for UDP ASSOCIATE pump EOF checks.
    UDP_PUMP_POLL_TIMEOUT_SECS: ClassVar[float] = 1.0

    # Timeout for directly-connected UDP responses on excluded hosts.
    UDP_DIRECT_RECV_TIMEOUT_SECS: ClassVar[float] = 2.0

    # Local DNS forwarder bind port.
    DNS_LOCAL_PORT: ClassVar[int] = 5300

    # Standard DNS upstream port.
    DNS_UPSTREAM_PORT: ClassVar[int] = 53

    # End-to-end DNS timeout through the tunnel.
    DNS_QUERY_TIMEOUT_SECS: ClassVar[float] = 8.0

    # Maximum concurrent DNS queries.
    DNS_MAX_INFLIGHT: ClassVar[int] = 1_024

    # ── Send / metrics ────────────────────────────────────────────────────────

    # Warning cadence for dropped outbound data frames.
    SEND_DROP_LOG_EVERY: ClassVar[int] = 1_000

    # Metrics reporting interval.
    METRICS_REPORT_INTERVAL_SECS: ClassVar[float] = 60.0

    # Liveness

    # Cadence at which the agent is expected to emit a LIVENESS frame when
    # idle.
    LIVENESS_INTERVAL_SECS: ClassVar[float] = 5.0

    # Maximum permitted RX silence before the client triggers reconnect
    RX_LIVENESS_TIMEOUT_SECS: ClassVar[float] = 15.0

    # Wake interval of the RX-liveness watchdog task (half the cadence so the
    # watchdog observes a stale ``last_rx_at`` within at most one cycle).
    RX_LIVENESS_CHECK_INTERVAL_SECS: ClassVar[float] = 2.5




    # Sender interleaving

    # Number of control frames the WebSocket sender drains per data frame in
    # one scheduling cycle.  Replaces strict ctrl-over-data priority with
    # weighted interleaving so a control-frame burst (e.g. CONN_OPEN storm,
    # saturation ERROR+CONN_CLOSE pairs) cannot starve the data path.
    # Within each queue FIFO ordering is preserved; only cross-queue ordering
    # is relaxed.
    WS_SEND_CTRL_BURST_RATIO: ClassVar[int] = 8

    # ── Bootstrap upload tuning (deferred-brief polish wave — Finding 8) ──────

    # Synchronous fence cadence during shell-upload of the agent binary.
    # Combined with the larger ``BOOTSTRAP_CHUNK_SIZE_CHARS`` (4096) this
    # yields ~64× fewer fence round-trips for a Go agent upload.
    UPLOAD_FENCE_EVERY_CHUNKS: ClassVar[int] = 64

    # ── UDP active-flow cap (deferred-brief polish wave — Finding 12) ─────────

    # Per UDP_ASSOCIATE session, maximum number of simultaneously open UDP
    # flows keyed by ``(dst_host, dst_port)``.  When exceeded the LRU flow
    # is force-closed; eviction is logged at WARNING level and surfaced via
    # the ``udp.flow.evicted_over_cap`` counter and the ``udp.flow.active``
    # gauge.
    UDP_ACTIVE_FLOWS_CAP: ClassVar[int] = 256

    # ── Bootstrap stash deque (deferred-brief polish wave — Finding 19) ───────

    # Maximum number of pre-AGENT_READY stdout lines retained for diagnostic
    # error context.  Raised from 256 → 1024 to defend against pathological
    # PS1 / fence-output volumes that could otherwise evict ``AGENT_READY``
    # itself before the bootstrapper sees it.
    BOOTSTRAP_MAX_STASH_LINES: ClassVar[int] = 1_024
