#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any

import aiohttp
from aiohttp import ClientTimeout
from aiohttp_socks import ProxyConnector

try:
    import orjson
except ImportError:
    orjson = None  # ty:ignore[invalid-assignment]


@dataclass(slots=True)
class LatencyStats:
    count: int
    ok: int
    errors: int
    min_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    mean_ms: float
    max_ms: float

    @classmethod
    def from_samples(cls, samples_ms: list[float], errors: int) -> LatencyStats:
        if not samples_ms:
            return cls(
                count=errors,
                ok=0,
                errors=errors,
                min_ms=math.nan,
                p50_ms=math.nan,
                p95_ms=math.nan,
                p99_ms=math.nan,
                mean_ms=math.nan,
                max_ms=math.nan,
            )
        s = sorted(samples_ms)
        return cls(
            count=len(samples_ms) + errors,
            ok=len(samples_ms),
            errors=errors,
            min_ms=s[0],
            p50_ms=_percentile_sorted(s, 50),
            p95_ms=_percentile_sorted(s, 95),
            p99_ms=_percentile_sorted(s, 99),
            mean_ms=statistics.fmean(s),
            max_ms=s[-1],
        )


@dataclass(slots=True)
class ThroughputStats:
    total_requests: int
    ok_requests: int
    error_requests: int
    total_bytes: int
    wall_seconds: float
    bytes_per_sec: float
    mib_per_sec: float
    req_per_sec: float


@dataclass(slots=True)
class TcpConnectStats:
    count: int
    ok: int
    errors: int
    min_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    mean_ms: float
    max_ms: float

    @classmethod
    def from_samples(cls, samples_ms: list[float], errors: int) -> TcpConnectStats:
        if not samples_ms:
            return cls(
                count=errors,
                ok=0,
                errors=errors,
                min_ms=math.nan,
                p50_ms=math.nan,
                p95_ms=math.nan,
                p99_ms=math.nan,
                mean_ms=math.nan,
                max_ms=math.nan,
            )
        s = sorted(samples_ms)
        return cls(
            count=len(samples_ms) + errors,
            ok=len(samples_ms),
            errors=errors,
            min_ms=s[0],
            p50_ms=_percentile_sorted(s, 50),
            p95_ms=_percentile_sorted(s, 95),
            p99_ms=_percentile_sorted(s, 99),
            mean_ms=statistics.fmean(s),
            max_ms=s[-1],
        )


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


def _dump_json(data: dict[str, Any]) -> bytes:
    if orjson is not None:
        return orjson.dumps(data, option=orjson.OPT_INDENT_2)
    return json.dumps(data, indent=2).encode("utf-8")


async def tcp_connect_once(host: str, port: int, timeout: float) -> float:
    start = _now()
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port),
        timeout=timeout,
    )
    writer.close()
    await writer.wait_closed()
    return (_now() - start) * 1000.0


async def tcp_connect_samples(
    host: str,
    port: int,
    timeout: float,
    samples: int,
) -> TcpConnectStats:
    latencies: list[float] = []
    errors = 0
    for i in range(samples):
        try:
            ms = await tcp_connect_once(host, port, timeout)
            latencies.append(ms)
            print(f"tcp_connect [{i + 1}/{samples}] {ms:.2f} ms")
        except Exception as exc:
            errors += 1
            print(f"tcp_connect [{i + 1}/{samples}] ERROR: {exc}", file=sys.stderr)
    return TcpConnectStats.from_samples(latencies, errors)


async def http_get_once(
    session: aiohttp.ClientSession,
    url: str,
    read_body: bool = True,
) -> tuple[float, int]:
    start = _now()
    async with session.get(url) as resp:
        resp.raise_for_status()
        if read_body:
            body = await resp.read()
            size = len(body)
        else:
            await resp.release()
            size = 0
    elapsed_ms = (_now() - start) * 1000.0
    return elapsed_ms, size


async def latency_worker(
    worker_id: int,
    session: aiohttp.ClientSession,
    url: str,
    requests: int,
    samples_out: list[float],
    errors_box: list[int],
) -> None:
    for i in range(requests):
        try:
            ms, _ = await http_get_once(session, url, read_body=True)
            samples_out.append(ms)
            print(f"latency worker={worker_id} req={i + 1}/{requests} {ms:.2f} ms")
        except Exception as exc:
            errors_box[0] += 1
            print(
                f"latency worker={worker_id} req={i + 1}/{requests} ERROR: {exc}",
                file=sys.stderr,
            )


async def run_latency_benchmark(
    url: str,
    concurrency: int,
    requests_per_worker: int,
    connector: aiohttp.BaseConnector,
    timeout: float,
) -> LatencyStats:
    samples: list[float] = []
    errors_box = [0]

    client_timeout = ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=client_timeout,
        raise_for_status=True,
    ) as session:
        tasks = [
            asyncio.create_task(
                latency_worker(
                    worker_id=i,
                    session=session,
                    url=url,
                    requests=requests_per_worker,
                    samples_out=samples,
                    errors_box=errors_box,
                )
            )
            for i in range(concurrency)
        ]
        await asyncio.gather(*tasks)

    return LatencyStats.from_samples(samples, errors_box[0])


async def throughput_worker(
    worker_id: int,
    session: aiohttp.ClientSession,
    url: str,
    requests: int,
    bytes_box: list[int],
    ok_box: list[int],
    err_box: list[int],
) -> None:
    for i in range(requests):
        try:
            _ms, size = await http_get_once(session, url, read_body=True)
            bytes_box[0] += size
            ok_box[0] += 1
            print(f"throughput worker={worker_id} req={i + 1}/{requests} bytes={size}")
        except Exception as exc:
            err_box[0] += 1
            print(
                f"throughput worker={worker_id} req={i + 1}/{requests} ERROR: {exc}",
                file=sys.stderr,
            )


async def run_throughput_benchmark(
    url: str,
    concurrency: int,
    requests_per_worker: int,
    connector: aiohttp.BaseConnector,
    timeout: float,
) -> ThroughputStats:
    bytes_box = [0]
    ok_box = [0]
    err_box = [0]

    client_timeout = ClientTimeout(total=timeout)
    start = _now()
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=client_timeout,
        raise_for_status=True,
    ) as session:
        tasks = [
            asyncio.create_task(
                throughput_worker(
                    worker_id=i,
                    session=session,
                    url=url,
                    requests=requests_per_worker,
                    bytes_box=bytes_box,
                    ok_box=ok_box,
                    err_box=err_box,
                )
            )
            for i in range(concurrency)
        ]
        await asyncio.gather(*tasks)

    wall = _now() - start
    total_bytes = bytes_box[0]
    total_requests = ok_box[0] + err_box[0]
    bps = total_bytes / wall if wall > 0 else math.inf

    return ThroughputStats(
        total_requests=total_requests,
        ok_requests=ok_box[0],
        error_requests=err_box[0],
        total_bytes=total_bytes,
        wall_seconds=wall,
        bytes_per_sec=bps,
        mib_per_sec=bps / (1024 * 1024),
        req_per_sec=total_requests / wall if wall > 0 else math.inf,
    )


def make_proxy_connector(
    socks_host: str,
    socks_port: int,
    limit: int,
    verify_ssl: bool,
) -> ProxyConnector:
    return ProxyConnector.from_url(
        f"socks5://{socks_host}:{socks_port}",
        limit=limit,
        ssl=verify_ssl,
        rdns=True,
    )


def make_direct_connector(limit: int, verify_ssl: bool) -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(limit=limit, ssl=verify_ssl)


def print_latency_stats(label: str, stats: LatencyStats | TcpConnectStats) -> None:
    print(
        f"{label}: "
        f"count={stats.count} "
        f"ok={stats.ok} "
        f"errors={stats.errors} "
        f"min={stats.min_ms:.2f}ms "
        f"p50={stats.p50_ms:.2f}ms "
        f"p95={stats.p95_ms:.2f}ms "
        f"p99={stats.p99_ms:.2f}ms "
        f"mean={stats.mean_ms:.2f}ms "
        f"max={stats.max_ms:.2f}ms"
    )


def print_throughput_stats(label: str, stats: ThroughputStats) -> None:
    print(
        f"{label}: "
        f"requests={stats.total_requests} "
        f"ok={stats.ok_requests} "
        f"errors={stats.error_requests} "
        f"bytes={stats.total_bytes} "
        f"seconds={stats.wall_seconds:.3f} "
        f"req_per_sec={stats.req_per_sec:.2f} "
        f"mib_per_sec={stats.mib_per_sec:.2f}"
    )


async def async_main(args: argparse.Namespace) -> int:
    tcp_host, tcp_port_str = args.tcp_target.rsplit(":", 1)
    tcp_port = int(tcp_port_str)

    report: dict[str, Any] = {
        "config": {
            "socks_host": args.socks_host,
            "socks_port": args.socks_port,
            "tcp_target": args.tcp_target,
            "latency_url": args.latency_url,
            "throughput_url": args.throughput_url,
            "connect_samples": args.connect_samples,
            "latency_concurrency": args.latency_concurrency,
            "latency_requests_per_worker": args.latency_requests_per_worker,
            "throughput_concurrency": args.throughput_concurrency,
            "throughput_requests_per_worker": args.throughput_requests_per_worker,
            "timeout": args.timeout,
            "verify_ssl": not args.insecure,
        },
        "results": {},
    }

    print("== ExecTunnel benchmark ==")

    print("\n-- Tunnel TCP connect benchmark --")
    tcp_tunnel = await tcp_connect_samples(
        args.socks_host,
        args.socks_port,
        args.timeout,
        args.connect_samples,
    )
    print_latency_stats("tcp_tunnel_listener", tcp_tunnel)
    report["results"]["tcp_tunnel_listener"] = asdict(tcp_tunnel)

    print("\n-- Direct TCP connect benchmark to target --")
    tcp_direct = await tcp_connect_samples(
        tcp_host,
        tcp_port,
        args.timeout,
        args.connect_samples,
    )
    print_latency_stats("tcp_direct_target", tcp_direct)
    report["results"]["tcp_direct_target"] = asdict(tcp_direct)

    print("\n-- HTTP/HTTPS latency via tunnel --")
    tunnel_latency_connector = make_proxy_connector(
        args.socks_host,
        args.socks_port,
        limit=max(args.latency_concurrency, args.throughput_concurrency) * 2,
        verify_ssl=not args.insecure,
    )
    tunnel_latency = await run_latency_benchmark(
        url=args.latency_url,
        concurrency=args.latency_concurrency,
        requests_per_worker=args.latency_requests_per_worker,
        connector=tunnel_latency_connector,
        timeout=args.timeout,
    )
    print_latency_stats("http_tunnel", tunnel_latency)
    report["results"]["http_tunnel"] = asdict(tunnel_latency)

    print("\n-- HTTP/HTTPS throughput via tunnel --")
    tunnel_tp_connector = make_proxy_connector(
        args.socks_host,
        args.socks_port,
        limit=max(args.latency_concurrency, args.throughput_concurrency) * 2,
        verify_ssl=not args.insecure,
    )
    tunnel_throughput = await run_throughput_benchmark(
        url=args.throughput_url,
        concurrency=args.throughput_concurrency,
        requests_per_worker=args.throughput_requests_per_worker,
        connector=tunnel_tp_connector,
        timeout=args.timeout,
    )
    print_throughput_stats("throughput_tunnel", tunnel_throughput)
    report["results"]["throughput_tunnel"] = asdict(tunnel_throughput)

    if args.baseline_direct:
        print("\n== Direct HTTP/HTTPS baseline ==")

        # print("\n-- Direct latency baseline --")
        # direct_latency_connector = make_direct_connector(
        #     limit=max(args.latency_concurrency, args.throughput_concurrency) * 2,
        #     verify_ssl=not args.insecure,
        # )
        # direct_latency = await run_latency_benchmark(
        #     url=args.latency_url,
        #     concurrency=args.latency_concurrency,
        #     requests_per_worker=args.latency_requests_per_worker,
        #     connector=direct_latency_connector,
        #     timeout=args.timeout,
        # )
        # print_latency_stats("http_direct", direct_latency)
        # report["results"]["http_direct"] = asdict(direct_latency)

        # print("\n-- Direct throughput baseline --")
        # direct_tp_connector = make_direct_connector(
        #     limit=max(args.latency_concurrency, args.throughput_concurrency) * 2,
        #     verify_ssl=not args.insecure,
        # )
        # direct_throughput = await run_throughput_benchmark(
        #     url=args.throughput_url,
        #     concurrency=args.throughput_concurrency,
        #     requests_per_worker=args.throughput_requests_per_worker,
        #     connector=direct_tp_connector,
        #     timeout=args.timeout,
        # )
        # print_throughput_stats("throughput_direct", direct_throughput)
        # report["results"]["throughput_direct"] = asdict(direct_throughput)
        #
        # report["results"]["latency_p50_overhead_ms"] = (
        #     tunnel_latency.p50_ms - direct_latency.p50_ms
        # )
        # report["results"]["latency_p95_overhead_ms"] = (
        #     tunnel_latency.p95_ms - direct_latency.p95_ms
        # )
        # report["results"]["throughput_ratio"] = (
        #     tunnel_throughput.mib_per_sec / direct_throughput.mib_per_sec
        #     if direct_throughput.mib_per_sec > 0
        #     else math.nan
        # )

    if args.json_output:
        with open(args.json_output, "wb") as f:
            f.write(_dump_json(report))
        print(f"\nJSON report written to {args.json_output}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Benchmark an ExecTunnel SOCKS5 tunnel")
    p.add_argument("--socks-host", default="127.0.0.1")
    p.add_argument("--socks-port", type=int, default=1033)

    p.add_argument(
        "--tcp-target",
        default="1.1.1.1:443",
        help="host:port for direct TCP baseline check",
    )

    p.add_argument(
        "--latency-url",
        required=True,
        help="URL for latency benchmark, e.g. https://example.com/",
    )
    p.add_argument(
        "--throughput-url",
        required=True,
        help="URL for throughput benchmark, ideally a larger object",
    )

    p.add_argument("--connect-samples", type=int, default=20)

    p.add_argument("--latency-concurrency", type=int, default=10)
    p.add_argument("--latency-requests-per-worker", type=int, default=10)

    p.add_argument("--throughput-concurrency", type=int, default=4)
    p.add_argument("--throughput-requests-per-worker", type=int, default=4)

    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--baseline-direct", action="store_true")
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--json-output")

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
