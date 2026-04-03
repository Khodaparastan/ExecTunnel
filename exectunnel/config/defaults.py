"""All numeric/string constants, split by domain."""

from __future__ import annotations

# ── WebSocket / Bridge ────────────────────────────────────────────────────────

WS_PING_INTERVAL_SECS: int = 30
WS_PING_TIMEOUT_SECS: int = 10
WS_SEND_TIMEOUT_SECS: float = 30.0
WS_SEND_QUEUE_CAP: int = 512
WS_RECONNECT_MAX_RETRIES: int = 5
WS_RECONNECT_BASE_DELAY_SECS: float = 1.0
WS_RECONNECT_MAX_DELAY_SECS: float = 30.0
WS_CLOSE_CODE_UNHEALTHY: int = 1011

# ── TcpConnectionWorker / ACK hardening ────────────────────────────────────────────────

ACK_TIMEOUT_WARN_EVERY: int = 10
ACK_TIMEOUT_WINDOW_SECS: float = 60.0
ACK_TIMEOUT_RECONNECT_THRESHOLD: int = 10
CONNECT_MAX_PENDING: int = 128
CONNECT_MAX_PENDING_PER_HOST: int = 16
CONNECT_MAX_PENDING_CF: int = 1
CONNECT_FAILURE_WARN_EVERY: int = 10
CONNECT_PACE_CF_INTERVAL_SECS: float = 0.120
CONNECT_PACE_JITTER_CAP_SECS: float = 0.02
PRE_ACK_BUFFER_CAP_BYTES: int = 256 * 1024
TCP_INBOUND_QUEUE_CAP: int = 256
HANDSHAKE_TIMEOUT_SECS: float = 10.0
READY_TIMEOUT_SECS: float = 15.0
CONN_ACK_TIMEOUT_SECS: float = 30.0

# ── UDP / DNS ─────────────────────────────────────────────────────────────────

UDP_SEND_QUEUE_CAP: int = 256
UDP_WARN_EVERY: int = 100
UDP_PUMP_POLL_TIMEOUT_SECS: float = 1.0
UDP_DIRECT_RECV_TIMEOUT_SECS: float = 2.0
DNS_LOCAL_PORT: int = 5300
DNS_UPSTREAM_PORT: int = 53
DNS_QUERY_TIMEOUT_SECS: float = 5.0
DNS_MAX_INFLIGHT: int = 256

# ── Bootstrap timing ─────────────────────────────────────────────────────────

BOOTSTRAP_STTY_DELAY_SECS: float = 0.2
BOOTSTRAP_RM_DELAY_SECS: float = 0.05
BOOTSTRAP_DECODE_DELAY_SECS: float = 0.1
BOOTSTRAP_DIAG_MAX_LINES: int = 20

# -- Session consts
BOOTSTRAP_CHUNK_SIZE_CHARS: int = 200
PIPE_READ_CHUNK_BYTES: int = 4_096
# ── Send / Metrics ────────────────────────────────────────────────────────────

SEND_DROP_LOG_EVERY: int = 100
METRICS_REPORT_INTERVAL_SECS: float = 60.0

# ── SOCKS5 defaults ───────────────────────────────────────────────────────────

SOCKS_DEFAULT_HOST: str = "127.0.0.1"
SOCKS_DEFAULT_PORT: int = 1080
