#!/usr/bin/env python3
"""Network throughput and latency assessment for WebSocket channels to pods.

Supports both deployment topologies:
  * Native K8s exec API  — binary channel-byte framing, SPAWN or SHELL exec mode
  * Proxy path           — nginx strips channel bytes; RAW framing, SHELL exec mode

Remote requirements: /bin/sh, cat, head, dd, printf, tr
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from typing import Any

import websocket
from probe_common import (
    # data classes
    AssessmentReport,
    ConnectionProfile,
    ConnectionTiming,
    # enums
    ExecMode,
    FrameMode,
    LatencyStats,
    ShellProtocol,
    ThroughputResult,
    # transport
    Transport,
    add_connection_args,
    discover_shell_protocol,
    fmt_bytes,
    frame_stats,
    lat_stats,
    make_latency_probe,
    # factories
    open_fresh_shell_transport,
    open_spawn_transport,
    printable,
    # probe
    probe_connection,
    wrap_ws_as_transport,
)

logger = logging.getLogger("exectunnel.probe.net_metrics")

# ─────────────────────────────────────────────────────────────────────────────
# Thresholds for SOCKS5 tunnel suitability rating
# ─────────────────────────────────────────────────────────────────────────────

_LAT_GOOD_MS = 50.0
_LAT_ACCEPT_MS = 150.0
_JITTER_GOOD_MS = 10.0
_JITTER_ACCEPT_MS = 40.0
_TPUT_GOOD_MBPS = 10.0
_TPUT_ACCEPT_MBPS = 1.0

# ─────────────────────────────────────────────────────────────────────────────
# Sentinel byte sequences
# ─────────────────────────────────────────────────────────────────────────────

_DS_MARKER = b"__DS_DONE__"  # downstream completion
_US_MARKER = b"__US_DONE__"  # upstream completion
_DS_READY = b"__DS_READY__"  # downstream ready
_US_READY = b"__US_READY__"  # upstream ready

# ─────────────────────────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_LATENCY_ROUNDS = 50
_DEFAULT_WARMUP_ROUNDS = 5
_DEFAULT_TRANSFER_SIZE = 1 << 20  # 1 MiB
_DEFAULT_CHUNK_SIZE = 32_768
_DEFAULT_BLOCK_SIZE = 32_768

# ─────────────────────────────────────────────────────────────────────────────
# Latency — SPAWN mode (cat echo, true RTT)
# ─────────────────────────────────────────────────────────────────────────────


def measure_latency_spawn(
    profile: ConnectionProfile,
    rounds: int,
    warmup: int,
) -> LatencyStats:
    """Measure RTT by sending an 8-byte magic probe to ``cat`` and timing the echo.

    Uses :func:`probe_common.make_latency_probe` which embeds a sequence number
    so bursted replies cannot be misidentified as earlier probes.
    """
    transport = open_spawn_transport(profile, ["cat"])
    try:
        buf = bytearray()
        samples: list[float] = []

        for seq in range(warmup + rounds):
            probe = make_latency_probe(seq)
            deadline = time.monotonic() + profile.timeout
            t0 = time.monotonic()
            transport.send_stdin(probe)

            while probe not in buf:
                r = transport.recv_stdout(deadline)
                if r is None:
                    raise RuntimeError(f"latency probe timeout seq={seq}")
                buf.extend(r[0])

            if seq >= warmup:
                samples.append((time.monotonic() - t0) * 1000.0)

            # Consume the echoed probe from the accumulation buffer
            idx = buf.find(probe)
            if idx >= 0:
                buf = buf[idx + len(probe) :]

        return lat_stats(samples)
    finally:
        transport.close()


# ─────────────────────────────────────────────────────────────────────────────
# Latency — SHELL mode (printf marker, includes shell interpretation overhead)
# ─────────────────────────────────────────────────────────────────────────────


def measure_latency_shell(
    profile: ConnectionProfile,
    transport: Transport,
    rounds: int,
    warmup: int,
) -> LatencyStats:
    """Measure shell round-trip time via ``printf`` marker echo.

    Includes shell interpreter overhead, making this the most realistic
    estimate of per-request latency through the SOCKS5 tunnel.
    """
    samples: list[float] = []
    transport.drain(0.3)  # clear any leftover banner / previous output

    for seq in range(warmup + rounds):
        marker = f"__LAT_{seq:06d}__"
        marker_b = marker.encode()
        deadline = time.monotonic() + profile.timeout

        t0 = time.monotonic()
        transport.send_command(f"printf '{marker}\\n'")

        buf = bytearray()
        while marker_b not in buf:
            r = transport.recv_stdout(deadline)
            if r is None:
                raise RuntimeError(f"latency probe timeout seq={seq}")
            buf.extend(r[0])

        if seq >= warmup:
            samples.append((time.monotonic() - t0) * 1000.0)
            logger.debug("latency seq=%d rtt=%.2fms", seq, samples[-1])

    return lat_stats(samples)


# ─────────────────────────────────────────────────────────────────────────────
# Downstream — SPAWN
# ─────────────────────────────────────────────────────────────────────────────


def measure_downstream_spawn(
    profile: ConnectionProfile,
    total_bytes: int,
    block_size: int,
) -> ThroughputResult:
    """Stream zeros (as printable 'A') from pod to client via dedicated process."""
    count = (total_bytes + block_size - 1) // block_size
    # tr converts null bytes to 'A' — ASCII-safe through any TTY line discipline
    cmd = f"dd if=/dev/zero bs={block_size} count={count} 2>/dev/null | tr '\\0' 'A'"
    transport = open_spawn_transport(profile, ["sh", "-c", cmd], stdin=False)
    try:
        deadline = time.monotonic() + profile.timeout
        got, sizes, t_first, t_last = transport.recv_counted(total_bytes, deadline)
        elapsed = max((t_last or 0) - (t_first or 0), 0.001)
        bps = (got * 8) / elapsed
        return ThroughputResult(
            direction="downstream",
            total_bytes=got,
            elapsed_s=round(elapsed, 6),
            throughput_bps=round(bps, 2),
            throughput_mbps=round(bps / 1e6, 3),
            frame_stats=frame_stats(sizes),
        )
    finally:
        transport.close()


# ─────────────────────────────────────────────────────────────────────────────
# Downstream — SHELL
# ─────────────────────────────────────────────────────────────────────────────


def measure_downstream_shell(
    profile: ConnectionProfile,
    transport: Transport,
    total_bytes: int,
    block_size: int,
) -> ThroughputResult:
    """Stream zeros (as 'A') from pod to client via the shared shell session.

    Protocol:
    1. Shell prints ``__DS_READY__``  → client is ready to time
    2. Shell streams N bytes of 'A'
    3. Shell prints ``\\n__DS_DONE__\\n`` → client stops timing
    """
    count = (total_bytes + block_size - 1) // block_size
    ds_str = _DS_MARKER.decode()
    ready_str = _DS_READY.decode()

    cmd = (
        f"printf '{ready_str}\\n'; "
        f"dd if=/dev/zero bs={block_size} count={count} 2>/dev/null "
        f"| tr '\\0' 'A'; "
        f"printf '\\n{ds_str}\\n'"
    )

    transport.drain(0.3)
    transport.send_command(cmd)

    _, ready = transport.recv_until(
        _DS_READY,
        time.monotonic() + transport._ws.gettimeout()
        if hasattr(transport._ws, "gettimeout")
        else time.monotonic() + 30.0,
    )
    # Use profile.timeout for the ready-wait deadline
    _, ready = transport.recv_until(_DS_READY, time.monotonic() + profile.timeout)
    if not ready:
        raise RuntimeError("downstream: shell did not emit ready marker")

    deadline = time.monotonic() + profile.timeout
    got = 0
    sizes: list[int] = []
    t_first: float | None = None
    t_last: float | None = None
    buf = bytearray()

    while _DS_MARKER not in buf:
        r = transport.recv_stdout(deadline)
        if r is None:
            logger.warning("downstream: timed out after %d bytes", got)
            break
        body, ts = r
        n = len(body)
        if not n:
            continue
        buf.extend(body)
        got += n
        sizes.append(n)
        if t_first is None:
            t_first = ts
        t_last = ts

    # Subtract marker + shell-echo overhead from the measured byte count
    overhead = len(_DS_MARKER) + 32
    measured = max(got - overhead, 0)
    elapsed = max((t_last or 0) - (t_first or 0), 0.001)
    bps = (measured * 8) / elapsed

    return ThroughputResult(
        direction="downstream",
        total_bytes=measured,
        elapsed_s=round(elapsed, 6),
        throughput_bps=round(bps, 2),
        throughput_mbps=round(bps / 1e6, 3),
        frame_stats=frame_stats(sizes),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Upstream — SPAWN
# ─────────────────────────────────────────────────────────────────────────────


def measure_upstream_spawn(
    profile: ConnectionProfile,
    total_bytes: int,
    chunk_size: int,
) -> ThroughputResult:
    """Push *total_bytes* of printable data to ``head -c`` and time to completion.

    Payload is ``b"A" * N`` — printable ASCII, safe through any TTY line discipline,
    eliminating corruption from CR/LF translation or 7-bit stripping.
    """
    us_str = _US_MARKER.decode()
    cmd = f"head -c {total_bytes} >/dev/null 2>/dev/null; printf '{us_str}\\n'"
    transport = open_spawn_transport(profile, ["sh", "-c", cmd])
    try:
        payload = b"A" * total_bytes  # printable — safe through TTY line discipline
        offset = 0
        t_start = time.monotonic()
        while offset < total_bytes:
            end = min(offset + chunk_size, total_bytes)
            transport.send_stdin(payload[offset:end])
            offset = end

        _, found = transport.recv_until(_US_MARKER, time.monotonic() + profile.timeout)
        t_done = time.monotonic()

        if not found:
            logger.warning("upstream: completion marker not received")

        elapsed = max(t_done - t_start, 0.001)
        bps = (total_bytes * 8) / elapsed
        return ThroughputResult(
            direction="upstream",
            total_bytes=total_bytes,
            elapsed_s=round(elapsed, 6),
            throughput_bps=round(bps, 2),
            throughput_mbps=round(bps / 1e6, 3),
            frame_stats=None,
        )
    finally:
        transport.close()


# ─────────────────────────────────────────────────────────────────────────────
# Upstream — SHELL
#
# Sequence:
#   1. Shell prints __US_READY__      → client knows head is about to read
#   2. Shell runs head -c N           → blocks on stdin
#   3. Client pushes N printable bytes
#   4. head exits                     → shell prints __US_DONE__
# ─────────────────────────────────────────────────────────────────────────────


def measure_upstream_shell(
    profile: ConnectionProfile,
    transport: Transport,
    total_bytes: int,
    chunk_size: int,
) -> ThroughputResult:
    """Push *total_bytes* through the shared shell session and time to completion."""
    us_str = _US_MARKER.decode()
    ready_str = _US_READY.decode()

    cmd = (
        f"printf '{ready_str}\\n'; "
        f"head -c {total_bytes} >/dev/null 2>/dev/null; "
        f"printf '{us_str}\\n'"
    )

    transport.drain(0.3)
    transport.send_command(cmd)

    _, ready = transport.recv_until(_US_READY, time.monotonic() + profile.timeout)
    if not ready:
        raise RuntimeError("upstream: shell did not emit ready marker")

    # Printable payload — safe through any TTY line discipline (no null bytes)
    payload = b"A" * total_bytes
    offset = 0
    t_start = time.monotonic()

    while offset < total_bytes:
        end = min(offset + chunk_size, total_bytes)
        transport.send_stdin(payload[offset:end])
        offset = end

    _, found = transport.recv_until(_US_MARKER, time.monotonic() + profile.timeout)
    t_done = time.monotonic()

    if not found:
        logger.warning("upstream: completion marker not received")

    elapsed = max(t_done - t_start, 0.001)
    bps = (total_bytes * 8) / elapsed
    return ThroughputResult(
        direction="upstream",
        total_bytes=total_bytes,
        elapsed_s=round(elapsed, 6),
        throughput_bps=round(bps, 2),
        throughput_mbps=round(bps / 1e6, 3),
        frame_stats=None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Verdict
# ─────────────────────────────────────────────────────────────────────────────


def _rate(
    label: str,
    value: float,
    good: float,
    acceptable: float,
    *,
    lower_is_better: bool = True,
) -> tuple[str, str]:
    if lower_is_better:
        r = "GOOD" if value <= good else "ACCEPTABLE" if value <= acceptable else "POOR"
    else:
        r = "GOOD" if value >= good else "ACCEPTABLE" if value >= acceptable else "POOR"
    return r, f"{label}: {value:.2f} → {r}"


def compute_verdict(report: AssessmentReport) -> None:
    """Populate *report*.verdict and *report*.verdict_details in-place."""
    ratings: list[str] = []
    details: list[str] = []

    if report.latency:
        r, d = _rate(
            "Latency median (ms)",
            report.latency.median_ms,
            _LAT_GOOD_MS,
            _LAT_ACCEPT_MS,
        )
        ratings.append(r)
        details.append(d)
        r, d = _rate(
            "Jitter (ms)", report.latency.jitter_ms, _JITTER_GOOD_MS, _JITTER_ACCEPT_MS
        )
        ratings.append(r)
        details.append(d)

    if report.downstream:
        r, d = _rate(
            "Downstream (Mbps)",
            report.downstream.throughput_mbps,
            _TPUT_GOOD_MBPS,
            _TPUT_ACCEPT_MBPS,
            lower_is_better=False,
        )
        ratings.append(r)
        details.append(d)

    if report.upstream:
        r, d = _rate(
            "Upstream (Mbps)",
            report.upstream.throughput_mbps,
            _TPUT_GOOD_MBPS,
            _TPUT_ACCEPT_MBPS,
            lower_is_better=False,
        )
        ratings.append(r)
        details.append(d)

    report.verdict_details = details

    if not ratings:
        report.verdict = "UNKNOWN"
    elif any(r == "POOR" for r in ratings):
        report.verdict = "POOR — expect timeouts or severe throughput limits for SOCKS5"
    elif any(r == "ACCEPTABLE" for r in ratings):
        report.verdict = "ACCEPTABLE — usable but expect reduced tunnel performance"
    else:
        report.verdict = "GOOD — suitable for SOCKS5 tunnel"


# ─────────────────────────────────────────────────────────────────────────────
# Report formatting
# ─────────────────────────────────────────────────────────────────────────────


def print_report(report: AssessmentReport, *, show_samples: bool = False) -> None:
    print()
    print("=" * 64)
    print("  NETWORK METRICS ASSESSMENT REPORT")
    print("=" * 64)

    c = report.connection
    print()
    print("── Connection ──")
    print(f"  Handshake time   : {c.handshake_ms:.1f} ms")
    print(f"  Subprotocol      : {c.selected_subprotocol or '<none>'}")
    print(f"  Frame mode       : {c.negotiated_frame_mode}")
    print(f"  Exec mode        : {c.negotiated_exec_mode}")
    print(f"  Shell protocol   : {c.shell_protocol or 'N/A'}")

    if report.latency:
        lat = report.latency
        print()
        print("── Latency ──")
        print(f"  Samples  : {lat.count}")
        print(f"  Min      : {lat.min_ms:.2f} ms")
        print(f"  Max      : {lat.max_ms:.2f} ms")
        print(f"  Mean     : {lat.mean_ms:.2f} ms")
        print(f"  Median   : {lat.median_ms:.2f} ms")
        print(f"  P95      : {lat.p95_ms:.2f} ms")
        print(f"  P99      : {lat.p99_ms:.2f} ms")
        print(f"  Stddev   : {lat.stddev_ms:.2f} ms")
        print(f"  Jitter   : {lat.jitter_ms:.2f} ms")
        if show_samples:
            print(f"  Samples  : {[round(s, 2) for s in lat.samples_ms]}")

    if report.downstream:
        ds = report.downstream
        print()
        print("── Downstream (pod → client) ──")
        print(f"  Transferred : {fmt_bytes(ds.total_bytes)}")
        print(f"  Elapsed     : {ds.elapsed_s:.3f} s")
        print(f"  Throughput  : {ds.throughput_mbps:.3f} Mbps")
        if ds.frame_stats:
            fs = ds.frame_stats
            print(f"  Frames      : {fs.count}")
            print(f"  Frame min   : {fmt_bytes(fs.min_bytes)}")
            print(f"  Frame max   : {fmt_bytes(fs.max_bytes)}")
            print(f"  Frame mean  : {fs.mean_bytes:.0f} B")
            print(f"  Frame median: {fs.median_bytes:.0f} B")

    if report.upstream:
        us = report.upstream
        print()
        print("── Upstream (client → pod) ──")
        print(f"  Transferred : {fmt_bytes(us.total_bytes)}")
        print(f"  Elapsed     : {us.elapsed_s:.3f} s")
        print(f"  Throughput  : {us.throughput_mbps:.3f} Mbps")

    if report.warnings:
        print()
        print("── Warnings ──")
        for w in report.warnings:
            print(f"  ⚠  {w}")

    if report.errors:
        print()
        print("── Errors ──")
        for e in report.errors:
            print(f"  ✗  {e}")

    print()
    print("── SOCKS5 Tunnel Suitability ──")
    for d in report.verdict_details:
        print(f"  {d}")
    print()
    print(f"  VERDICT: {report.verdict}")
    print()


def report_to_dict(r: AssessmentReport) -> dict[str, Any]:
    d = asdict(r)
    if d.get("latency") is not None:
        d["latency"].pop("samples_ms", None)
    return d


# ─────────────────────────────────────────────────────────────────────────────
# Assessment orchestrator
# ─────────────────────────────────────────────────────────────────────────────


def run_assessment(args: argparse.Namespace) -> AssessmentReport:
    # ── 1. Probe connection ──────────────────────────────────────────────────
    print("▸ Probing connection …")
    profile = probe_connection(args)
    for w in profile.warnings:
        print(f"  ⚠  {w}")
    print(
        f"  handshake={profile.handshake_ms:.1f}ms  "
        f"subprotocol={profile.selected_subprotocol or '<none>'}  "
        f"frame_mode={profile.frame_mode.value}  "
        f"exec_mode={profile.exec_mode.value}"
    )

    is_spawn = profile.exec_mode is ExecMode.SPAWN
    discovered_proto: ShellProtocol | None = None
    shell_transport: Transport | None = None

    # ── 2. Discover shell input protocol (shell mode only) ───────────────────
    if not is_spawn:
        n_candidates = len(__import__("probe_common").PROTOCOL_CANDIDATES)
        print(
            f"▸ Discovering shell input protocol "
            f"({n_candidates} candidates, "
            f"≤{args.per_candidate_wait:.0f}s each) …"
        )
        discovered_proto, reuse_ws, banner = discover_shell_protocol(
            profile,
            initial_wait=args.initial_wait,
            per_candidate_wait=args.per_candidate_wait,
        )

        if discovered_proto is None:
            print("  ✗ No working input protocol found.")
            if banner:
                print(f"  Banner ({len(banner)} bytes):")
                for line in printable(banner[:500]).splitlines()[:10]:
                    print(f"    {line}")
            else:
                print("    <empty — shell may not have started yet>")
            print()
            print("  Troubleshooting:")
            print(
                f"    * Increase --initial-wait    (current: {args.initial_wait:.1f}s)"
            )
            print(
                f"    * Increase --per-candidate-wait (current: {args.per_candidate_wait:.1f}s)"
            )
            print("    * Run with -v for per-candidate wire details")
            print("    * Verify the URL reaches an interactive shell")

            return AssessmentReport(
                connection=ConnectionTiming(
                    handshake_ms=profile.handshake_ms,
                    selected_subprotocol=profile.selected_subprotocol,
                    negotiated_frame_mode=profile.frame_mode.value,
                    negotiated_exec_mode=profile.exec_mode.value,
                    shell_protocol=None,
                ),
                errors=["shell protocol discovery failed"],
                warnings=list(profile.warnings),
            )

        print(f"  ✓ Found: {discovered_proto.name}")
        shell_transport = (
            wrap_ws_as_transport(reuse_ws, profile, discovered_proto)
            if reuse_ws is not None
            else open_fresh_shell_transport(
                profile, discovered_proto, args.initial_wait
            )
        )

    report = AssessmentReport(
        connection=ConnectionTiming(
            handshake_ms=profile.handshake_ms,
            selected_subprotocol=profile.selected_subprotocol,
            negotiated_frame_mode=profile.frame_mode.value,
            negotiated_exec_mode=profile.exec_mode.value,
            shell_protocol=discovered_proto.name if discovered_proto else None,
        ),
        warnings=list(profile.warnings),
    )

    try:
        # ── 3. Latency ────────────────────────────────────────────────────────
        if not args.skip_latency:
            method = "spawn/cat" if is_spawn else "shell/printf"
            print(
                f"▸ Measuring latency via {method} "
                f"({args.warmup}+{args.latency_rounds} rounds) …"
            )
            try:
                report.latency = (
                    measure_latency_spawn(profile, args.latency_rounds, args.warmup)
                    if is_spawn
                    else measure_latency_shell(
                        profile,
                        shell_transport,  # type: ignore[arg-type]
                        args.latency_rounds,
                        args.warmup,
                    )
                )
                lat = report.latency
                print(
                    f"  median={lat.median_ms:.2f}ms  "
                    f"p95={lat.p95_ms:.2f}ms  "
                    f"jitter={lat.jitter_ms:.2f}ms"
                )
            except Exception as exc:
                msg = f"latency test failed: {exc}"
                logger.warning(msg, exc_info=True)
                report.errors.append(msg)

        # ── 4. Downstream ─────────────────────────────────────────────────────
        if not args.skip_downstream:
            print(f"▸ Measuring downstream ({fmt_bytes(args.transfer_size)}) …")
            try:
                report.downstream = (
                    measure_downstream_spawn(
                        profile, args.transfer_size, args.block_size
                    )
                    if is_spawn
                    else measure_downstream_shell(
                        profile,
                        shell_transport,  # type: ignore[arg-type]
                        args.transfer_size,
                        args.block_size,
                    )
                )
                print(f"  {report.downstream.throughput_mbps:.3f} Mbps")
            except Exception as exc:
                msg = f"downstream test failed: {exc}"
                logger.warning(msg, exc_info=True)
                report.errors.append(msg)

        # ── 5. Upstream ───────────────────────────────────────────────────────
        if not args.skip_upstream:
            print(f"▸ Measuring upstream ({fmt_bytes(args.transfer_size)}) …")
            try:
                report.upstream = (
                    measure_upstream_spawn(profile, args.transfer_size, args.chunk_size)
                    if is_spawn
                    else measure_upstream_shell(
                        profile,
                        shell_transport,  # type: ignore[arg-type]
                        args.transfer_size,
                        args.chunk_size,
                    )
                )
                print(f"  {report.upstream.throughput_mbps:.3f} Mbps")
            except Exception as exc:
                msg = f"upstream test failed: {exc}"
                logger.warning(msg, exc_info=True)
                report.errors.append(msg)

        compute_verdict(report)
        return report

    finally:
        if shell_transport is not None:
            shell_transport.close()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Network throughput and latency assessment for any WebSocket "
            "channel that reaches a pod shell."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  # Native K8s exec API (SPAWN mode, channel framing)
  %(prog)s --url wss://kube:6443/api/v1/namespaces/ns/pods/p/exec \\
           --token-file ./token --insecure

  # Reverse proxy — auto-discovers framing and input format
  %(prog)s --url wss://stream.runflare.com/ws \\
           --header "Cookie:session=abc"

  # Slow proxy — give shell more time to start
  %(prog)s --url wss://slow-proxy.corp/ws \\
           --initial-wait 5 --per-candidate-wait 5

  # JSON output for CI
  %(prog)s --url wss://... --json
""",
    )

    add_connection_args(parser, include_token=True, default_timeout=30.0)

    mg = parser.add_argument_group("mode")
    mg.add_argument(
        "--frame-mode",
        choices=[m.value for m in FrameMode],
        default=FrameMode.AUTO.value,
        help="WebSocket frame interpretation (default: auto)",
    )
    mg.add_argument(
        "--exec-mode",
        choices=[m.value for m in ExecMode],
        default=ExecMode.SPAWN.value,
        help="Command execution strategy (default: spawn)",
    )

    tg = parser.add_argument_group("test parameters")
    tg.add_argument("--latency-rounds", type=int, default=_DEFAULT_LATENCY_ROUNDS)
    tg.add_argument("--warmup", type=int, default=_DEFAULT_WARMUP_ROUNDS)
    tg.add_argument("--transfer-size", type=int, default=_DEFAULT_TRANSFER_SIZE)
    tg.add_argument("--chunk-size", type=int, default=_DEFAULT_CHUNK_SIZE)
    tg.add_argument("--block-size", type=int, default=_DEFAULT_BLOCK_SIZE)
    tg.add_argument(
        "--initial-wait",
        type=float,
        default=3.0,
        help="Seconds to wait for shell startup banner (default: 3)",
    )
    tg.add_argument(
        "--per-candidate-wait",
        type=float,
        default=3.0,
        help="Seconds to wait per discovery candidate (default: 3)",
    )

    sg = parser.add_argument_group("skip")
    sg.add_argument("--skip-latency", action="store_true")
    sg.add_argument("--skip-downstream", action="store_true")
    sg.add_argument("--skip-upstream", action="store_true")

    og = parser.add_argument_group("output")
    og.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit JSON report to stdout",
    )
    og.add_argument(
        "--show-samples",
        action="store_true",
        help="Include per-sample latency list in report",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        logging.getLogger("websocket").setLevel(logging.CRITICAL)

    for name in ("transfer_size", "latency_rounds", "chunk_size", "block_size"):
        if getattr(args, name) <= 0:
            print(f"--{name.replace('_', '-')} must be > 0", file=sys.stderr)
            return 2

    try:
        report = run_assessment(args)

        if args.json_output:
            print(json.dumps(report_to_dict(report), indent=2))
        else:
            print_report(report, show_samples=args.show_samples)

        if report.errors:
            return 1
        return 1 if "POOR" in report.verdict else 0

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except websocket.WebSocketBadStatusException as exc:
        print(f"WEBSOCKET_HANDSHAKE=FAIL: {exc}", file=sys.stderr)
        return 1
    except websocket.WebSocketException as exc:
        print(f"WEBSOCKET_ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        logger.exception("unhandled error")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
