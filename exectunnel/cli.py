"""
Command-line interface.  Entry point: ``exectunnel.cli:main``.

Commands
--------
tunnel
    Run a local SOCKS5 proxy that tunnels connections through the pod via
    WebSocket exec.

Environment variables
---------------------
EXECTUNNEL_WSS_URL
    WebSocket endpoint (required).  Legacy alias: ``WSS_URL``.
WSS_INSECURE
    Set to ``1`` to skip TLS verification.
WSS_PING_INTERVAL
    Application-level keepalive interval in seconds (default: 30).
WSS_SEND_TIMEOUT
    Maximum time in seconds to wait for a WebSocket send (default: 30).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import logging
import os
import sys

from exectunnel._version import __version__
from exectunnel.config import AppConfig, TunnelConfig
from exectunnel.config.defaults import (
    ACK_TIMEOUT_RECONNECT_THRESHOLD,
    ACK_TIMEOUT_WINDOW_SECS,
    ACK_TIMEOUT_WARN_EVERY,
    CONN_ACK_TIMEOUT_SECS,
    DNS_LOCAL_PORT,
    DNS_UPSTREAM_PORT,
    METRICS_REPORT_INTERVAL_SECS,
    READY_TIMEOUT_SECS,
    SOCKS_DEFAULT_HOST,
    SOCKS_DEFAULT_PORT,
    WS_PING_INTERVAL_SECS,
    WS_RECONNECT_BASE_DELAY_SECS,
    WS_RECONNECT_MAX_DELAY_SECS,
    WS_RECONNECT_MAX_RETRIES,
    WS_SEND_QUEUE_CAP,
    WS_SEND_TIMEOUT_SECS,
)
from exectunnel.config.exclusions import DEFAULT_EXCLUDE_CIDRS
from exectunnel.config.settings import TUNNEL_CONFIG, get_app_config, get_tunnel_config
from exectunnel.exceptions import (
    AgentSyntaxError,
    AgentVersionMismatchError,
    BootstrapError,
    ConfigurationError,
    ExecTunnelError,
    ReconnectExhaustedError,
)
from exectunnel.observability import (
    configure_logging,
    metrics_inc,
    run_metrics_reporter,
    span,
    start_trace,
)

logger = logging.getLogger(__name__)


# ── Argument type validators ──────────────────────────────────────────────────


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid port {value!r}: must be an integer"
        ) from exc
    if not (1 <= port <= 65535):
        raise argparse.ArgumentTypeError(
            f"invalid port {value!r}: must be in range 1–65535"
        )
    return port


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid number {value!r}: must be a positive float"
        ) from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(
            f"invalid number {value!r}: must be > 0"
        )
    return parsed


def _dns_upstream(value: str) -> str:
    """Accept an IP address or hostname as a DNS upstream.

    IP addresses are validated strictly.  Hostnames are accepted as-is
    (e.g. ``kube-dns.kube-system.svc.cluster.local``) — the resolver
    will reject invalid hostnames at connect time.
    """
    # Try IP first; fall back to accepting as hostname.
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    # Basic hostname sanity: non-empty, no spaces, no shell metacharacters.
    if not value or any(c in value for c in " \t\n;|&`$"):
        raise argparse.ArgumentTypeError(
            f"invalid DNS upstream {value!r}: must be an IP address or hostname"
        )
    return value


# ── Argument parser ───────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="exectunnel",
        description="SOCKS5 proxy tunnel over WebSocket into Kubernetes pods.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
examples:
  exectunnel tunnel
  exectunnel tunnel --dns 10.96.0.10
  exectunnel tunnel --socks-port 1080 --exclude 10.10.0.0/16

environment:
  EXECTUNNEL_WSS_URL      WebSocket endpoint (required; legacy alias: WSS_URL)
  WSS_INSECURE            set to 1 to skip TLS verification
  WSS_PING_INTERVAL       keepalive interval in seconds (default: {WS_PING_INTERVAL_SECS:.0f})
  WSS_SEND_TIMEOUT        send timeout in seconds (default: {WS_SEND_TIMEOUT_SECS:.0f})
  WSS_SEND_QUEUE_CAP      outbound frame queue capacity (default: {WS_SEND_QUEUE_CAP})
  WSS_RECONNECT_MAX_RETRIES   reconnect attempts after disconnect (default: {WS_RECONNECT_MAX_RETRIES})
  WSS_RECONNECT_BASE_DELAY    reconnect backoff base seconds (default: {WS_RECONNECT_BASE_DELAY_SECS:.1f})
  WSS_RECONNECT_MAX_DELAY     reconnect backoff max seconds (default: {WS_RECONNECT_MAX_DELAY_SECS:.0f})
  EXECTUNNEL_LOG_ENGINE       stdlib|structlog (default: stdlib)
  EXECTUNNEL_LOG_FORMAT       console|json (default: console)
  EXECTUNNEL_LOG_COLOR        auto|always|never (default: auto)
  EXECTUNNEL_METRICS_REPORT_INTERVAL  seconds; 0 disables reporter (default: {METRICS_REPORT_INTERVAL_SECS:.0f})
  EXECTUNNEL_METRICS_LOG_LEVEL        debug|info (default: debug)
  EXECTUNNEL_METRICS_VERBOSE          1 to include full metrics snapshot at DEBUG
  EXECTUNNEL_ACK_TIMEOUT_RECONNECT_THRESHOLD  timeout count before forced reconnect (default: {ACK_TIMEOUT_RECONNECT_THRESHOLD})
  EXECTUNNEL_ACK_TIMEOUT_WINDOW_SECS          rolling window for timeout threshold (default: {ACK_TIMEOUT_WINDOW_SECS:.0f})
  EXECTUNNEL_ACK_TIMEOUT_WARN_EVERY           warning cadence for ACK failures (default: {ACK_TIMEOUT_WARN_EVERY})
  EXECTUNNEL_OBS_EXPORTERS    comma list: log,file,http (default: log)
  EXECTUNNEL_OBS_PLATFORM     generic|datadog|splunk|newrelic (default: generic)
  EXECTUNNEL_OBS_SERVICE      service name in exported payloads (default: exectunnel)
  EXECTUNNEL_OBS_FILE_PATH    jsonl path for file exporter
  EXECTUNNEL_OBS_HTTP_URL     endpoint for http exporter
  EXECTUNNEL_OBS_HTTP_TIMEOUT http exporter timeout seconds (default: 5)
  EXECTUNNEL_OBS_HTTP_HEADERS semicolon header list, e.g. Authorization=Bearer token
""",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"exectunnel {__version__}",
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        default="info",
        metavar="LEVEL",
        help="logging verbosity: debug | info | warning | error (default: info)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # ── tunnel ────────────────────────────────────────────────────────────────
    tun = sub.add_parser(
        "tunnel",
        help="SOCKS5 proxy tunnel through the pod",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Run a local SOCKS5 proxy that tunnels connections through the pod.\n"
            "Set ALL_PROXY=socks5://127.0.0.1:1080 to route your shell traffic.\n\n"
            "RFC1918 + loopback CIDRs are bypassed by default:\n"
            + "\n".join(f"  {c}" for c in DEFAULT_EXCLUDE_CIDRS)
        ),
    )
    tun.add_argument(
        "--socks-port",
        type=_port,
        default=TUNNEL_CONFIG.socks_port,
        metavar="PORT",
        help=f"local SOCKS5 port (default: {TUNNEL_CONFIG.socks_port})",
    )
    tun.add_argument(
        "--socks-host",
        default=TUNNEL_CONFIG.socks_host,
        metavar="HOST",
        help=f"local SOCKS5 bind address (default: {TUNNEL_CONFIG.socks_host})",
    )
    tun.add_argument(
        "--dns",
        type=_dns_upstream,
        metavar="IP_OR_HOST",
        help="upstream DNS server to forward queries to through the tunnel "
        "(IP address or hostname)",
    )
    tun.add_argument(
        "--dns-port",
        type=_port,
        default=TUNNEL_CONFIG.dns_local_port,
        metavar="PORT",
        help=f"local DNS forwarder listen port (default: {TUNNEL_CONFIG.dns_local_port})",
    )
    tun.add_argument(
        "--exclude",
        metavar="CIDR",
        action="append",
        default=[],
        help=(
            "extra CIDR to bypass the tunnel (repeatable); e.g. 10.10.0.0/16. "
            "Default RFC1918 exclusions are always included unless "
            "--no-default-exclude is set."
        ),
    )
    tun.add_argument(
        "--no-default-exclude",
        action="store_true",
        help="clear the default RFC1918 + loopback exclusions",
    )
    tun.add_argument(
        "--ready-timeout",
        type=_positive_float,
        default=TUNNEL_CONFIG.ready_timeout,
        metavar="SECS",
        help=(
            f"seconds to wait for agent READY signal "
            f"(default: {TUNNEL_CONFIG.ready_timeout:.0f})"
        ),
    )
    tun.add_argument(
        "--conn-ack-timeout",
        type=_positive_float,
        default=TUNNEL_CONFIG.conn_ack_timeout,
        metavar="SECS",
        help=(
            f"seconds to wait for agent CONN_ACK per connection "
            f"(default: {TUNNEL_CONFIG.conn_ack_timeout:.0f})"
        ),
    )

    return parser


# ── CLI helpers ───────────────────────────────────────────────────────────────


def _parse_tunnel_excludes(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse and validate CIDR exclusions from CLI arguments.

    Args:
        args:   Parsed argument namespace.
        parser: The argument parser — used for ``parser.error()`` so invalid
                CIDRs produce a proper usage message and exit code 2.

    Returns:
        List of network objects to bypass the tunnel.
    """
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    if not args.no_default_exclude:
        # Deferred import: avoids loading the exclusions module at CLI startup
        # before logging is configured.
        from exectunnel.config.exclusions import get_default_exclusion_networks

        nets.extend(get_default_exclusion_networks())
    for cidr in args.exclude:
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError as exc:
            parser.error(f"invalid CIDR {cidr!r}: {exc}")
    return nets


def _parse_metrics_interval(raw: str) -> float:
    """Parse and validate ``EXECTUNNEL_METRICS_REPORT_INTERVAL``.

    Returns a non-negative float.  Invalid or negative values fall back to
    sensible defaults with a WARNING log so the operator knows the env var
    was ignored.
    """
    try:
        interval = float(raw)
    except ValueError:
        logger.warning(
            "invalid EXECTUNNEL_METRICS_REPORT_INTERVAL=%r — "
            "expected a number; using %.0fs",
            raw,
            METRICS_REPORT_INTERVAL_SECS,
        )
        return METRICS_REPORT_INTERVAL_SECS
    if interval < 0:
        logger.warning(
            "negative EXECTUNNEL_METRICS_REPORT_INTERVAL=%s — "
            "using 0 (reporter disabled)",
            raw,
        )
        return 0.0
    return interval


# ── Async command runner ──────────────────────────────────────────────────────


async def run_tunnel_command(cfg: AppConfig, tun_cfg: TunnelConfig) -> int:
    """Bootstrap the agent and run the tunnel session.

    Returns an exit code rather than calling ``sys.exit()`` directly so the
    ``finally`` block always runs and the metrics reporter is cleanly stopped
    before the process exits.

    Exit codes
    ----------
    0   Clean exit (normal session end or ``KeyboardInterrupt``).
    1   Fatal error — bootstrap failure, reconnect exhaustion, or any other
        unrecoverable :class:`ExecTunnelError`.
    2   Unexpected non-library error (bug or environment issue).

    Exception handling strategy
    ---------------------------
    * :class:`AgentSyntaxError`         — fatal; agent payload corrupt/incompatible.
    * :class:`AgentVersionMismatchError`— fatal; operator must upgrade.
    * :class:`BootstrapError`           — fatal; agent failed to start.
    * :class:`ReconnectExhaustedError`  — fatal; all retries consumed.
    * :class:`ExecTunnelError`          — catch-all for any other library error.
    * ``KeyboardInterrupt``             — clean shutdown; exit 0.
    * ``Exception``                     — unexpected; exit 2.
    """
    # Deferred import: avoids loading the heavy session module at CLI startup
    # before logging is configured and config is validated.
    from exectunnel.session import TunnelSession

    trace_seed = os.getenv("EXECTUNNEL_TRACE_ID")
    exit_code = 0

    stop_event = asyncio.Event()
    reporter_task: asyncio.Task[None] | None = None

    metrics_interval = _parse_metrics_interval(
        os.getenv(
            "EXECTUNNEL_METRICS_REPORT_INTERVAL",
            str(METRICS_REPORT_INTERVAL_SECS),
        )
    )

    try:
        with start_trace(trace_seed), span("cli.run_tunnel_command"):
            metrics_inc("cli.commands.started", command="tunnel")
            logger.info(
                "starting tunnel socks=%s:%d dns=%s excludes=%d",
                tun_cfg.socks_host,
                tun_cfg.socks_port,
                f"{tun_cfg.dns_upstream}:{tun_cfg.dns_local_port}"
                if tun_cfg.dns_upstream
                else "disabled",
                len(tun_cfg.exclude),
            )

            session = TunnelSession(cfg, tun_cfg)

            if metrics_interval > 0:
                reporter_task = asyncio.create_task(
                    run_metrics_reporter(metrics_interval, stop_event),
                    name="metrics-reporter",
                )

            try:
                await session.run()
                metrics_inc("cli.commands.ok", command="tunnel")

            # ── Bootstrap failures — always fatal, never retried ──────────────
            except AgentSyntaxError as exc:
                metrics_inc(
                    "cli.commands.error",
                    command="tunnel",
                    error=exc.error_code.replace(".", "_"),
                )
                logger.error(
                    "fatal: agent script syntax error [%s]: %s\n"
                    "  file   : %s\n"
                    "  hint   : %s\n"
                    "  error_id: %s",
                    exc.error_code,
                    exc.message,
                    exc.details.get("filename", "unknown"),
                    exc.hint or "—",
                    exc.error_id,
                )
                logger.debug("bootstrap traceback", exc_info=True)
                exit_code = 1

            except AgentVersionMismatchError as exc:
                metrics_inc(
                    "cli.commands.error",
                    command="tunnel",
                    error=exc.error_code.replace(".", "_"),
                )
                logger.error(
                    "fatal: agent version mismatch [%s]: %s\n"
                    "  local  : %s\n"
                    "  remote : %s\n"
                    "  hint   : %s\n"
                    "  error_id: %s",
                    exc.error_code,
                    exc.message,
                    exc.details.get("local_version", "unknown"),
                    exc.details.get("remote_version", "unknown"),
                    exc.hint or "—",
                    exc.error_id,
                )
                logger.debug("bootstrap traceback", exc_info=True)
                exit_code = 1

            except BootstrapError as exc:
                # Catches AgentReadyTimeoutError and any other BootstrapError
                # subclass not handled above.
                metrics_inc(
                    "cli.commands.error",
                    command="tunnel",
                    error=exc.error_code.replace(".", "_"),
                )
                logger.error(
                    "fatal: bootstrap failed [%s]: %s\n"
                    "  hint   : %s\n"
                    "  error_id: %s",
                    exc.error_code,
                    exc.message,
                    exc.hint or "—",
                    exc.error_id,
                )
                logger.debug("bootstrap traceback", exc_info=True)
                exit_code = 1

            # ── Reconnect exhaustion ──────────────────────────────────────────
            except ReconnectExhaustedError as exc:
                metrics_inc(
                    "cli.commands.error",
                    command="tunnel",
                    error=exc.error_code.replace(".", "_"),
                )
                logger.error(
                    "fatal: reconnect exhausted [%s]: %s\n"
                    "  attempts: %s\n"
                    "  last    : %s\n"
                    "  hint    : %s\n"
                    "  error_id: %s",
                    exc.error_code,
                    exc.message,
                    exc.details.get("attempts", "?"),
                    exc.details.get("last_error", "—"),
                    exc.hint or "—",
                    exc.error_id,
                )
                logger.debug("reconnect traceback", exc_info=True)
                exit_code = 1

            # ── Catch-all for any other library error ─────────────────────────
            except ExecTunnelError as exc:
                metrics_inc(
                    "cli.commands.error",
                    command="tunnel",
                    error=exc.error_code.replace(".", "_"),
                )
                logger.error(
                    "fatal: tunnel error [%s]: %s\n"
                    "  hint   : %s\n"
                    "  error_id: %s",
                    exc.error_code,
                    exc.message,
                    exc.hint or "—",
                    exc.error_id,
                )
                logger.debug("tunnel traceback", exc_info=True)
                exit_code = 1

            # ── Clean shutdown ────────────────────────────────────────────────
            except KeyboardInterrupt:
                metrics_inc("cli.commands.interrupted", command="tunnel")
                logger.info("interrupted")
                exit_code = 0

            # ── Unexpected non-library error (bug / environment) ──────────────
            except Exception as exc:
                metrics_inc(
                    "cli.commands.error",
                    command="tunnel",
                    error=exc.__class__.__name__,
                )
                logger.error(
                    "fatal: unexpected error (%s): %s",
                    exc.__class__.__name__,
                    exc,
                )
                logger.debug("unexpected traceback", exc_info=True)
                exit_code = 2

    finally:
        # Always stop the metrics reporter cleanly before returning so no
        # background tasks outlive the event loop.
        stop_event.set()
        if reporter_task is not None:
            with contextlib.suppress(Exception):
                await reporter_task

    return exit_code


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    # Configure logging before anything else so early errors are visible.
    configure_logging(args.log_level)

    try:
        cfg = get_app_config()
    except ConfigurationError as exc:
        logger.debug(
            "configuration error [%s]: %s (error_id=%s)",
            exc.error_code,
            exc.message,
            exc.error_id,
        )
        parser.error(
            f"[{exc.error_code}] {exc.message}"
            + (f"\n  hint: {exc.hint}" if exc.hint else "")
        )

    if args.command == "tunnel":
        try:
            tun_cfg = get_tunnel_config(
                cfg,
                socks_host=args.socks_host,
                socks_port=args.socks_port,
                dns_upstream=args.dns,
                dns_local_port=args.dns_port,
                ready_timeout=args.ready_timeout,
                conn_ack_timeout=args.conn_ack_timeout,
                exclude=_parse_tunnel_excludes(args, parser),
            )
        except ConfigurationError as exc:
            logger.debug(
                "tunnel config error [%s]: %s (error_id=%s)",
                exc.error_code,
                exc.message,
                exc.error_id,
            )
            parser.error(
                f"[{exc.error_code}] {exc.message}"
                + (f"\n  hint: {exc.hint}" if exc.hint else "")
            )

        try:
            exit_code = asyncio.run(run_tunnel_command(cfg, tun_cfg))
        except KeyboardInterrupt:
            # asyncio.run() on Python 3.10 propagates KeyboardInterrupt.
            # On 3.11+ it is converted to CancelledError internally and
            # handled inside run_tunnel_command.  This catch ensures a
            # clean exit on both versions with a single log message.
            logger.info("interrupted")
            exit_code = 0

        if exit_code != 0:
            sys.exit(exit_code)

if __name__ == "__main__":
    main()
