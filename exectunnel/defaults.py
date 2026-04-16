"""All numeric/string constants, split by domain.

Design rules
------------
* Every constant is typed explicitly.
* Units are encoded in the name (``_SECS``, ``_MS``, ``_BYTES``, ``_CHARS``).
* Dead / stale constants are removed.
* Values are derived from first principles — see inline comments.
* No constant depends on another constant at module level (avoids import-order
  issues and makes each value independently readable).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Defaults:
    # ── WebSocket / Bridge ────────────────────────────────────────────────────────

    # Application-level keepalive interval sent through _keepalive_loop.
    # The websockets built-in ping is disabled (ping_interval=None) because its
    # background task competes for the internal write lock under sustained load.
    # 20 s is well below the standard NAT/proxy idle timeout floor (30–60 s) and
    # gives a full 10 s of headroom before WS_SEND_TIMEOUT_SECS (30 s) fires,
    # preventing a slow ping send from spuriously triggering a tunnel teardown.
    WS_PING_INTERVAL_SECS: float = 20.0

    # Maximum time to wait for a single WebSocket frame send to complete.
    # Over a kubectl exec channel, a stalled send indicates a dead tunnel.
    # 30 s is generous enough to survive transient API server hiccups while
    # still detecting truly stalled connections before the OS TCP timeout (2 min).
    WS_SEND_TIMEOUT_SECS: float = 15.0

    # Bounded outbound data queue.  Control frames use a separate unbounded queue.
    # 512 items × ~4 KiB/frame ≈ 2 MiB peak in-flight before backpressure.
    # Sized to absorb one full TCP window of data without stalling the upstream
    # reader while the send loop drains.
    WS_SEND_QUEUE_CAP: int = 512

    # Reconnect policy — exponential backoff with jitter.
    # 5 retries × max_delay 30 s ≈ up to ~2.5 min of retry window.
    # Enough to survive a rolling API server restart without giving up.
    WS_RECONNECT_MAX_RETRIES: int = 10
    WS_RECONNECT_BASE_DELAY_SECS: float = 1.0
    WS_RECONNECT_MAX_DELAY_SECS: float = 15.0

    # RFC 6455 close code 1011 — "server encountered an unexpected condition".
    # Used when the agent appears unhealthy (ACK timeout surge) to force a clean
    # WebSocket close and trigger the reconnect loop.
    WS_CLOSE_CODE_UNHEALTHY: int = 1011

    # ── TCP connection / ACK hardening ────────────────────────────────────────────

    # How often to emit a warning log when ACK timeouts accumulate.
    # Suppresses log spam on sustained tunnel degradation while still providing
    # a signal every N failures.
    ACK_TIMEOUT_WARN_EVERY: int = 10

    # Sliding window for counting ACK timeouts before triggering a reconnect.
    # With CONN_ACK_TIMEOUT_SECS = 10 s and threshold = 5, the trigger fires
    # when 5 connections time out within 60 s — roughly one per 12 s average.
    # This indicates sustained tunnel degradation, not a transient blip.
    ACK_TIMEOUT_WINDOW_SECS: float = 30.0

    # Number of ACK timeouts within ACK_TIMEOUT_WINDOW_SECS that triggers a
    # forced reconnect.  5 is enough to distinguish a degraded tunnel from
    # normal variance (1–2 timeouts per window is expected on busy clusters).
    ACK_TIMEOUT_RECONNECT_THRESHOLD: int = 5

    # Global semaphore cap on concurrent in-flight CONN_OPEN frames.
    # Prevents thundering-herd on reconnect and limits tunnel frame burst.
    # 128 concurrent connects × ~200 bytes/CONN_OPEN = ~25 KiB burst.
    CONNECT_MAX_PENDING: int = 128

    # Per-host semaphore cap.  Prevents a single destination from consuming
    # all global slots (e.g. a misconfigured client hammering one host).
    CONNECT_MAX_PENDING_PER_HOST: int = 16

    # Log a warning every N connect failures per host.
    # Suppresses spam for persistently unreachable hosts.
    CONNECT_FAILURE_WARN_EVERY: int = 10

    # Cloudflare challenge endpoint pacing — minimum ms between CONN_OPENs.
    # challenges.cloudflare.com rate-limits aggressively; 120 ms ≈ 8 req/s,
    # well within their documented threshold.
    # Stored in milliseconds (integer) for direct use in connect_pace_cf_ms.
    CONNECT_PACE_CF_MS: int = 120

    # Maximum random jitter added on top of the pacing interval.
    # Kept small (20 ms) to avoid introducing noticeable latency while still
    # preventing perfectly synchronised bursts from multiple coroutines.
    CONNECT_PACE_JITTER_CAP_SECS: float = 0.02

    # Minimum seconds between successive CONN_OPEN frames to the same host.
    # Prevents thundering-herd bursts to a single destination on reconnect.
    # 0 disables pacing entirely (default — pacing caused SSL handshake aborts
    # for tools like aria2 that open many parallel connections to the same host,
    # because connections 2-N were delayed 3 s each before CONN_OPEN was sent).
    CONNECT_PACE_INTERVAL_SECS: float = 0.0

    # Timeout waiting for the agent to ACK a CONN_OPEN.
    # The agent should ACK within one tunnel RTT (50–500 ms on healthy clusters).
    # 10 s is generous enough to survive a congested cluster (3× worst-case RTT
    # of ~2 s + DNS resolution + TCP connect on the agent side) while still
    # detecting stalled connections promptly.
    #
    # Previous value was 30 s — too high.  At 30 s a stalled connection holds
    # a semaphore slot for 30 s, blocking up to CONNECT_MAX_PENDING other
    # connections.  At 10 s the slot is released 3× faster.
    #
    # Cross-check with ACK hardening:
    #   ACK_TIMEOUT_RECONNECT_THRESHOLD = 5 timeouts
    #   ACK_TIMEOUT_WINDOW_SECS = 60 s
    #   → trigger fires when 5 × 10 s = 50 s of ACK wait accumulates in 60 s
    #   → correctly identifies a degraded tunnel without false-positives on
    #     transient single-connection failures.
    CONN_ACK_TIMEOUT_SECS: float = 30.0

    # Pre-ACK buffer: bytes buffered before the agent ACKs CONN_OPEN.
    # 64 KiB = 16 × PIPE_READ_CHUNK_BYTES — holds a full TLS ClientHello +
    # HTTP request without stalling the upstream reader.
    # On flush this becomes 16 queue items, well within TCP_INBOUND_QUEUE_CAP.
    PRE_ACK_BUFFER_CAP_BYTES: int = 64 * 1024  # 65,536 bytes

    # asyncio.Queue capacity for DATA frames waiting to be written to the local
    # TCP socket.  256 items × 4 KiB/item ≈ 1 MiB per connection before
    # backpressure engages on the WebSocket recv loop.
    TCP_INBOUND_QUEUE_CAP: int = 256

    # ── SOCKS5 handshake ──────────────────────────────────────────────────────────

    # Maximum time for the full SOCKS5 handshake (greeting + method selection +
    # request).  All I/O is local loopback — no tunnel involvement.
    # 30 s matches OpenSSH DynamicForward / Dante / 3proxy industry standard.
    HANDSHAKE_TIMEOUT_SECS: float = 30.0

    # ── Bootstrap timing ─────────────────────────────────────────────────────────

    # Maximum time to wait for AGENT_READY after exec'ing the agent script.
    # Breakdown of what happens after `exec python3 agent.py`:
    #   Python startup + import:        ~200–500 ms
    #   Agent initialisation:           ~50–100 ms
    #   AGENT_READY write + flush:      ~10 ms
    #   Tunnel RTT back to client:      50–500 ms
    #   Total healthy:                  ~300 ms – 1.1 s
    #   Slow cluster / large agent:     up to ~5 s
    #
    # 30 s provides 6× headroom over the worst realistic case and matches the
    # SOCKS5 handshake timeout for operational consistency.
    #
    # Previous value was 15 s — too tight for slow clusters or large agent
    # scripts where Python startup alone can take 2–3 s.
    READY_TIMEOUT_SECS: float = 30.0

    # Fixed sleep after `stty raw -echo` to let the terminal mode change take
    # effect before sending the first printf command.  200 ms is empirically
    # sufficient across all tested shell implementations (bash, sh, busybox ash).
    BOOTSTRAP_STTY_DELAY_SECS: float = 0.2

    # Fixed sleep after `rm -f` to ensure the shell has processed the command
    # before the next printf appends to the (now-deleted) file.  50 ms is
    # sufficient because rm is synchronous in all POSIX shells.
    BOOTSTRAP_RM_DELAY_SECS: float = 0.05

    # Fixed sleep after the sed | base64 -d pipeline to let the decode complete
    # before the syntax-check reads the output file.  100 ms is sufficient for
    # agent scripts up to ~500 KiB; increase if using a very large agent.
    BOOTSTRAP_DECODE_DELAY_SECS: float = 0.1

    # Maximum number of pre-ready diagnostic lines to retain for error reporting.
    # 20 lines is enough to capture a full Python traceback (typically 10–15
    # lines) plus a few lines of context.
    BOOTSTRAP_DIAG_MAX_LINES: int = 20

    # ── Session / frame constants ─────────────────────────────────────────────────

    # Base64 chunk size for agent upload.
    # 200 chars is safely under the POSIX minimum shell input buffer (512 bytes)
    # and avoids splitting multi-byte sequences in the base64 alphabet.
    BOOTSTRAP_CHUNK_SIZE_CHARS: int = 200

    # Default fetch raw URL used to fetch the agent when bootstrap_delivery="fetch".
    # Points to the main branch of the canonical repository.  Override via
    # EXECTUNNEL_FETCH_AGENT_URL to pin a specific commit or use a fork.
    BOOTSTRAP_FETCH_AGENT_URL: str = "https://raw.githubusercontent.com/Khodaparastan/ExecTunnel/refs/heads/main/exectunnel/payload/agent.py"

    # Maximum time to wait for the curl/wget fetch to complete before proceeding.
    # The bootstrapper polls for file existence every BOOTSTRAP_FETCH_FETCH_POLL_SECS
    # and gives up after this many seconds (logging a warning and continuing).
    # 10 s is generous for a ~50 KiB file over a typical cluster egress path
    # (1–5 Mbit/s).  Increase via EXECTUNNEL_BOOTSTRAP_FETCH_FETCH_DELAY if
    # the pod's egress is unusually slow.
    BOOTSTRAP_FETCH_FETCH_DELAY_SECS: float = 10.0

    # Polling interval when waiting for the fetch-fetched agent file to appear.
    # 0.5 s gives sub-second detection on fast clusters without busy-looping.
    BOOTSTRAP_FETCH_FETCH_POLL_SECS: float = 0.5

    # Path of the sentinel file written inside the pod after a successful syntax
    # check.  When bootstrap_skip_if_present=True and bootstrap_syntax_check=True,
    # the bootstrapper checks for this file before running the syntax check again.
    # Avoids re-parsing a large agent script on every reconnect.
    BOOTSTRAP_SYNTAX_OK_SENTINEL: str = "/tmp/exectunnel_agent.syntax_ok"

    # Path of the agent script inside the pod.
    BOOTSTRAP_AGENT_PATH: str = "/tmp/exectunnel_agent.py"

    # Path of the Go agent binary inside the pod.
    # The Go agent is a static Linux/amd64 binary that replaces the Python script.
    # Override via EXECTUNNEL_BOOTSTRAP_GO_AGENT_PATH if /tmp is noexec.
    BOOTSTRAP_GO_AGENT_PATH: str = "/tmp/agent"

    # TCP read chunk size for upstream copy and direct pipe.
    # 4 KiB is the standard Linux socket buffer quantum and matches the typical
    # TLS record size, minimising partial-record reads.
    PIPE_READ_CHUNK_BYTES: int = 4_096

    # ── UDP / DNS ─────────────────────────────────────────────────────────────────

    # asyncio.Queue capacity for decoded UDP datagrams in UdpFlowHandler.
    # 32 items absorbs DNS response bursts and short QUIC handshake exchanges.
    # Worst-case memory: 32 × 8,192 B ≈ 256 KiB per flow.
    UDP_INBOUND_QUEUE_CAP: int = 32

    # asyncio.Queue capacity for parsed (payload, host, port) tuples in UdpRelay.
    # The consumer (UDP ASSOCIATE handler) is blocked on tunnel I/O (50–500 ms).
    # At 100 datagrams/s and 500 ms RTT, steady-state depth ≈ 50 items.
    # 64 provides headroom above the 50-item steady-state for QUIC bursts.
    UDP_RELAY_QUEUE_CAP: int = 64

    # Emit a logger.warning every N drops after the first (which always warns).
    # At ~1,000 datagrams/s burst rate, this yields ~1 warning/s under pressure.
    UDP_WARN_EVERY: int = 1_000

    # Polling interval for the UDP ASSOCIATE pump loop.
    # The pump calls relay.recv() with this timeout so it can periodically check
    # req.reader.at_eof() to detect client disconnection.
    # 1 s is short enough to detect disconnection promptly without busy-looping.
    UDP_PUMP_POLL_TIMEOUT_SECS: float = 1.0

    # Timeout for receiving a UDP response on directly-connected (excluded) hosts.
    # 2 s covers slow LAN responses while failing fast enough to not block the
    # pump loop for a full UDP_PUMP_POLL_TIMEOUT_SECS cycle.
    UDP_DIRECT_RECV_TIMEOUT_SECS: float = 2.0

    # Local port for the DNS forwarder UDP socket.
    # 5300 is an unprivileged port (> 1024) that avoids conflicts with system
    # DNS resolvers on port 53.  Override via EXECTUNNEL_DNS_LOCAL_PORT.
    DNS_LOCAL_PORT: int = 5300

    # Standard DNS port (RFC 1035 §4.2.1).
    DNS_UPSTREAM_PORT: int = 53

    # End-to-end DNS query timeout through the tunnel.
    # T_total = 3 × T_tunnel + T_dns
    # Healthy cluster:   3 × 100 ms + 10 ms  =  310 ms
    # Congested cluster: 3 × 500 ms + 50 ms  = 1,550 ms
    # 5 s matches the RFC 1035 DNS client retry interval (glibc/musl/Go default)
    # so a timeout causes a clean client retry rather than a duplicate in-flight.
    DNS_QUERY_TIMEOUT_SECS: float = 8.0

    # Maximum concurrent in-flight DNS queries.
    # At 100 q/s × 5 s timeout = 500 inflight at steady state.
    # 512 covers this with a small safety margin; memory ≈ 512 × 4 KiB ≈ 2 MiB.
    DNS_MAX_INFLIGHT: int = 1_024

    # ── Send / Metrics ────────────────────────────────────────────────────────────

    # Log a warning every N dropped send frames.
    # Matches UDP_WARN_EVERY (1,000) for consistent log volume across all
    # drop-counting paths.  At 1,000 drops/s this yields ~1 warning/s.
    SEND_DROP_LOG_EVERY: int = 1_000

    # Interval at which the observability layer reports aggregated metrics.
    METRICS_REPORT_INTERVAL_SECS: float = 60.0

    # ── SOCKS5 defaults ───────────────────────────────────────────────────────────

    # Default bind address for the local SOCKS5 proxy.
    # Loopback-only by default — prevents accidental open-proxy exposure.
    # Override via EXECTUNNEL_SOCKS_HOST (with a warning if non-loopback).
    SOCKS_DEFAULT_HOST: str = "127.0.0.1"

    # Default SOCKS5 listen port.  1080 is the IANA-registered SOCKS port.
    SOCKS_DEFAULT_PORT: int = 1080
    # asyncio.Queue capacity for completed SOCKS5 handshakes awaiting dispatch.
    # 256 absorbs short bursts of parallel browser connections (Chrome opens
    # up to 6 connections per host × ~40 tabs = ~240 simultaneous handshakes).
    SOCKS_REQUEST_QUEUE_CAP: int = 256
    # Max seconds to enqueue a completed handshake before dropping it.
    # 5 s matches the default TCP connect timeout on most OS stacks.
    SOCKS_QUEUE_PUT_TIMEOUT_SECS: float = 5.0
