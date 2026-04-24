#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import shutil
import socket
import statistics
import subprocess
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class SampleStats:
    count: int
    min_ms: float
    p50_ms: float
    p95_ms: float
    mean_ms: float
    max_ms: float

    @classmethod
    def from_samples(cls, samples_ms: list[float]) -> SampleStats:
        if not samples_ms:
            return cls(0, math.nan, math.nan, math.nan, math.nan, math.nan)
        s = sorted(samples_ms)
        return cls(
            count=len(s),
            min_ms=s[0],
            p50_ms=_percentile_sorted(s, 50),
            p95_ms=_percentile_sorted(s, 95),
            mean_ms=statistics.fmean(s),
            max_ms=s[-1],
        )


@dataclass(slots=True)
class ThroughputResult:
    bytes_read: int
    seconds: float
    mib_per_sec: float


def _percentile_sorted(values: list[float], pct: float) -> float:
    if not values:
        return math.nan
    if len(values) == 1:
        return values[0]
    rank = (pct / 100.0) * (len(values) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return values[lo]
    frac = rank - lo
    return values[lo] * (1.0 - frac) + values[hi] * frac


def _now() -> float:
    return time.perf_counter()


def _require_curl() -> str:
    curl = shutil.which("curl")
    if not curl:
        raise SystemExit("curl not found in PATH")
    return curl


def _tcp_connect_once(host: str, port: int, timeout: float) -> float:
    start = _now()
    with socket.create_connection((host, port), timeout=timeout):
        pass
    return (_now() - start) * 1000.0


def _curl_http_latency_once(
    curl_bin: str,
    url: str,
    socks_host: str,
    socks_port: int,
    timeout: float,
    insecure: bool,
    direct: bool,
) -> float:
    cmd = [
        curl_bin,
        "--silent",
        "--show-error",
        "--output",
        "/dev/null",
        "--max-time",
        f"{timeout:.3f}",
        "--write-out",
        "%{time_total}",
    ]

    if not direct:
        cmd += ["--socks5-hostname", f"{socks_host}:{socks_port}"]

    if insecure:
        cmd.append("--insecure")

    cmd.append(url)

    start = _now()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = _now() - start

    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"curl exit code {proc.returncode}")

    raw = proc.stdout.strip()
    try:
        curl_reported = float(raw)
    except ValueError:
        curl_reported = elapsed

    return curl_reported * 1000.0


def _curl_download(
    curl_bin: str,
    url: str,
    socks_host: str,
    socks_port: int,
    timeout: float,
    insecure: bool,
    direct: bool,
) -> ThroughputResult:
    cmd = [
        curl_bin,
        "--silent",
        "--show-error",
        "--max-time",
        f"{timeout:.3f}",
    ]

    if not direct:
        cmd += ["--socks5-hostname", f"{socks_host}:{socks_port}"]

    if insecure:
        cmd.append("--insecure")

    cmd.append(url)

    start = _now()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        check=False,
    )
    elapsed = _now() - start

    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace").strip()
        raise RuntimeError(stderr or f"curl exit code {proc.returncode}")

    size = len(proc.stdout)
    mib_per_sec = (size / (1024 * 1024)) / elapsed if elapsed > 0 else math.inf
    return ThroughputResult(bytes_read=size, seconds=elapsed, mib_per_sec=mib_per_sec)


def _repeat(name: str, count: int, fn) -> list[float]:
    out: list[float] = []
    for i in range(count):
        try:
            v = fn()
            out.append(v)
            print(f"{name} [{i + 1}/{count}] {v:.2f} ms")
        except Exception as exc:
            print(f"{name} [{i + 1}/{count}] ERROR: {exc}", file=sys.stderr)
    return out


def _print_stats(label: str, stats: SampleStats) -> None:
    print(
        f"{label}: "
        f"count={stats.count} "
        f"min={stats.min_ms:.2f}ms "
        f"p50={stats.p50_ms:.2f}ms "
        f"p95={stats.p95_ms:.2f}ms "
        f"mean={stats.mean_ms:.2f}ms "
        f"max={stats.max_ms:.2f}ms"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark an ExecTunnel SOCKS5 tunnel instance"
    )
    parser.add_argument("--socks-host", default="127.0.0.1")
    parser.add_argument("--socks-port", type=int, default=1081)

    parser.add_argument(
        "--tcp-target",
        default="1.1.1.1:443",
        help="host:port for raw TCP connect latency test",
    )
    parser.add_argument(
        "--http-url",
        required=True,
        help="HTTP/HTTPS URL for latency test, e.g. https://example.com/",
    )
    parser.add_argument(
        "--download-url",
        required=True,
        help="URL for throughput test, ideally a stable large file",
    )

    parser.add_argument("--connect-samples", type=int, default=20)
    parser.add_argument("--http-samples", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=15.0)

    parser.add_argument(
        "--baseline-direct",
        action="store_true",
        help="also run direct baseline without SOCKS",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="pass --insecure to curl for HTTPS endpoints with bad certs",
    )
    parser.add_argument(
        "--json-output",
        help="write machine-readable JSON report to this path",
    )

    args = parser.parse_args()

    curl_bin = _require_curl()

    tcp_host, tcp_port_raw = args.tcp_target.rsplit(":", 1)
    tcp_port = int(tcp_port_raw)

    urllib.parse.urlparse(args.http_url)
    urllib.parse.urlparse(args.download_url)

    report: dict[str, Any] = {
        "socks": {
            "host": args.socks_host,
            "port": args.socks_port,
        },
        "targets": {
            "tcp_target": args.tcp_target,
            "http_url": args.http_url,
            "download_url": args.download_url,
        },
        "samples": {
            "connect": args.connect_samples,
            "http": args.http_samples,
        },
        "results": {},
    }

    print("== Tunnel benchmark ==")

    print("\n-- TCP connect via tunnel --")
    tcp_samples = _repeat(
        "tcp_tunnel",
        args.connect_samples,
        lambda: _tcp_connect_once(args.socks_host, args.socks_port, args.timeout),
    )
    tcp_stats = SampleStats.from_samples(tcp_samples)
    _print_stats("tcp_tunnel", tcp_stats)
    report["results"]["tcp_tunnel"] = asdict(tcp_stats)

    print("\n-- HTTP latency via tunnel --")
    http_tunnel_samples = _repeat(
        "http_tunnel",
        args.http_samples,
        lambda: _curl_http_latency_once(
            curl_bin,
            args.http_url,
            args.socks_host,
            args.socks_port,
            args.timeout,
            args.insecure,
            direct=False,
        ),
    )
    http_tunnel_stats = SampleStats.from_samples(http_tunnel_samples)
    _print_stats("http_tunnel", http_tunnel_stats)
    report["results"]["http_tunnel"] = asdict(http_tunnel_stats)

    print("\n-- Download throughput via tunnel --")
    dl_tunnel = _curl_download(
        curl_bin,
        args.download_url,
        args.socks_host,
        args.socks_port,
        args.timeout,
        args.insecure,
        direct=False,
    )
    print(
        f"download_tunnel: bytes={dl_tunnel.bytes_read} "
        f"seconds={dl_tunnel.seconds:.3f} "
        f"throughput={dl_tunnel.mib_per_sec:.2f} MiB/s"
    )
    report["results"]["download_tunnel"] = asdict(dl_tunnel)

    if args.baseline_direct:
        print("\n== Direct baseline ==")

        print("\n-- TCP connect direct --")
        tcp_direct_samples = _repeat(
            "tcp_direct",
            args.connect_samples,
            lambda: _tcp_connect_once(tcp_host, tcp_port, args.timeout),
        )
        tcp_direct_stats = SampleStats.from_samples(tcp_direct_samples)
        _print_stats("tcp_direct", tcp_direct_stats)
        report["results"]["tcp_direct"] = asdict(tcp_direct_stats)

        print("\n-- HTTP latency direct --")
        http_direct_samples = _repeat(
            "http_direct",
            args.http_samples,
            lambda: _curl_http_latency_once(
                curl_bin,
                args.http_url,
                args.socks_host,
                args.socks_port,
                args.timeout,
                args.insecure,
                direct=True,
            ),
        )
        http_direct_stats = SampleStats.from_samples(http_direct_samples)
        _print_stats("http_direct", http_direct_stats)
        report["results"]["http_direct"] = asdict(http_direct_stats)

        print("\n-- Download throughput direct --")
        dl_direct = _curl_download(
            curl_bin,
            args.download_url,
            args.socks_host,
            args.socks_port,
            args.timeout,
            args.insecure,
            direct=True,
        )
        print(
            f"download_direct: bytes={dl_direct.bytes_read} "
            f"seconds={dl_direct.seconds:.3f} "
            f"throughput={dl_direct.mib_per_sec:.2f} MiB/s"
        )
        report["results"]["download_direct"] = asdict(dl_direct)

        if "http_tunnel" in report["results"] and "http_direct" in report["results"]:
            t = report["results"]["http_tunnel"]["p50_ms"]
            d = report["results"]["http_direct"]["p50_ms"]
            report["results"]["http_latency_overhead_p50_ms"] = t - d

        if (
            "download_tunnel" in report["results"]
            and "download_direct" in report["results"]
        ):
            t = report["results"]["download_tunnel"]["mib_per_sec"]
            d = report["results"]["download_direct"]["mib_per_sec"]
            report["results"]["download_throughput_ratio"] = (
                (t / d) if d > 0 else math.nan
            )

    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"\nJSON report written to {args.json_output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
