#!/usr/bin/env python3
"""Probe a native Kubernetes exec ws/wss endpoint for TTY mode and binary cleanliness.

Uses direct K8s channel framing (channel-byte prefix on every binary frame).
Run this against the real K8s API server exec endpoint, not the nginx proxy path.
"""

from __future__ import annotations

import argparse
import logging
import sys

import websocket

from probe_common import (
    CHANNEL_STDIN,
    K8S_EXEC_SUBPROTOCOLS,
    K8sCapture,
    add_connection_args,
    build_headers,
    build_k8s_exec_url,
    build_sslopt,
    make_binary_payload,
    printable,
    recv_k8s_capture,
    resolve_bearer_token,
    sha256_hex,
    ws_connect,
)

logger = logging.getLogger("exectunnel.probe.k8s_exec")

# ─────────────────────────────────────────────────────────────────────────────
# Connection helper
# ─────────────────────────────────────────────────────────────────────────────


def _connect(url: str, args: argparse.Namespace) -> websocket.WebSocket:
    token = resolve_bearer_token(
        getattr(args, "token", None), getattr(args, "token_file", None)
    )
    headers = build_headers(args.header, bearer_token=token)
    sslopt = build_sslopt(url, insecure=args.insecure, ca_file=args.ca_file)

    ws = ws_connect(
        url,
        headers=headers,
        subprotocols=K8S_EXEC_SUBPROTOCOLS,
        sslopt=sslopt,
        timeout=args.timeout,
    )

    if not ws.getsubprotocol():
        ws.close()
        raise RuntimeError(
            "Server did not negotiate a Kubernetes exec channel subprotocol. "
            "Is this a real K8s exec endpoint?"
        )
    return ws


# ─────────────────────────────────────────────────────────────────────────────
# TTY detection
# ─────────────────────────────────────────────────────────────────────────────

_TTY_DETECT_SCRIPT = r"""
echo "probe=tty-detect"
echo "term=${TERM:-}"
if [ -t 0 ]; then echo "stdin_is_tty=true";  else echo "stdin_is_tty=false";  fi
if [ -t 1 ]; then echo "stdout_is_tty=true"; else echo "stdout_is_tty=false"; fi
if [ -t 2 ]; then echo "stderr_is_tty=true"; else echo "stderr_is_tty=false"; fi
if command -v tty >/dev/null 2>&1; then printf "tty_stdin="; tty 2>/dev/null || true; fi
if command -v stty >/dev/null 2>&1; then
    echo "stty_available=true"; stty -a 2>/dev/null || true
else
    echo "stty_available=false"
fi
""".strip()


def run_tty_detect(args: argparse.Namespace, *, tty: bool) -> K8sCapture:
    url = build_k8s_exec_url(args.url, ["sh", "-c", _TTY_DETECT_SCRIPT], tty=tty)
    ws = _connect(url, args)
    try:
        return recv_k8s_capture(
            ws,
            selected_subprotocol=ws.getsubprotocol() or "",
            timeout=args.timeout,
            idle_after_first_stdout=0.5,
        )
    finally:
        ws.close()


# ─────────────────────────────────────────────────────────────────────────────
# Binary integrity test
# ─────────────────────────────────────────────────────────────────────────────


def run_binary_test(args: argparse.Namespace) -> tuple[bool, bytes, K8sCapture]:
    size = args.binary_size
    payload = make_binary_payload(size, seed=42)

    # head -c reads from stdin (no file arg); dd is the universal fallback
    cmd = f"head -c {size} 2>/dev/null || dd bs={size} count=1 2>/dev/null"
    url = build_k8s_exec_url(args.url, ["sh", "-c", cmd], tty=False)
    ws = _connect(url, args)

    try:
        # Send probe payload as stdin with the K8s STDIN channel byte prefix
        ws.send_binary(bytes([CHANNEL_STDIN]) + payload)
        cap = recv_k8s_capture(
            ws,
            selected_subprotocol=ws.getsubprotocol() or "",
            timeout=args.timeout,
            want_stdout_len=size,
        )
        got = bytes(cap.stdout[:size])
        return got == payload, payload, cap
    finally:
        ws.close()


# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────


def _print_capture(title: str, cap: K8sCapture) -> None:
    print(f"== {title} ==")
    print(f"selected_subprotocol={cap.selected_subprotocol}")
    if cap.stdout:
        print("-- stdout --")
        print(printable(cap.stdout).rstrip())
    if cap.stderr:
        print("-- stderr --")
        print(printable(cap.stderr).rstrip())
    if cap.error:
        print("-- error channel --")
        print(printable(cap.error).rstrip())
    print()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe a native Kubernetes exec ws/wss endpoint for TTY mode and binary cleanliness.",
    )
    add_connection_args(
        parser, include_subprotocol=False, include_origin=False, default_timeout=10.0
    )

    tg = parser.add_argument_group("test parameters")
    tg.add_argument(
        "--binary-size",
        type=int,
        default=65_536,
        help="Payload size for binary integrity test (default: 65536)",
    )
    tg.add_argument(
        "--tty-test", action="store_true", help="Also run TTY detection with tty=true"
    )
    tg.add_argument(
        "--skip-binary", action="store_true", help="Skip the binary integrity test"
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.binary_size <= 0:
        print("--binary-size must be > 0", file=sys.stderr)
        return 2

    print("Kubernetes raw exec WebSocket probe")
    ### `probe_k8s_exec.py` (continued)

    print(f"url={args.url}")
    print()

    try:
        # ── TTY detection (tty=false) ─────────────────────────────────────────
        non_tty = run_tty_detect(args, tty=False)
        _print_capture("TTY detection with tty=false", non_tty)

        non_tty_text = printable(non_tty.stdout)
        for expected in ("stdin_is_tty=false", "stdout_is_tty=false"):
            if expected not in non_tty_text:
                print(
                    f"WARNING: expected {expected!r} in tty=false output — "
                    "check that the endpoint is not forcing a TTY"
                )

        # ── Optional TTY detection (tty=true) ────────────────────────────────
        if args.tty_test:
            tty_cap = run_tty_detect(args, tty=True)
            _print_capture("TTY detection with tty=true", tty_cap)

        # ── Binary integrity ──────────────────────────────────────────────────
        if not args.skip_binary:
            print("== Binary integrity test with tty=false ==")
            ok, payload, cap = run_binary_test(args)
            got = bytes(cap.stdout[: len(payload)])

            print(f"selected_subprotocol={cap.selected_subprotocol}")
            print(f"payload_size={len(payload)}")
            print(f"payload_sha256={sha256_hex(payload)}")
            print(f"output_size={len(got)}")
            print(f"output_sha256={sha256_hex(got)}")

            if cap.stderr:
                print("-- stderr --")
                print(printable(cap.stderr).rstrip())
            if cap.error:
                print("-- error channel --")
                print(printable(cap.error).rstrip())

            if ok:
                print("BINARY_TEST=PASS")
                print("Exec WebSocket path is binary-clean with tty=false.")
            else:
                print("BINARY_TEST=FAIL")
                print("Bytes were changed, truncated, expanded, or reordered.")
                if len(got) == 0:
                    print("HINT: zero bytes received — is stdin piped correctly?")
                elif len(got) != len(payload):
                    print(f"HINT: size mismatch got={len(got)} want={len(payload)}")
                return 1

        print()
        print("DONE")
        return 0

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except websocket.WebSocketException as exc:
        print(f"WEBSOCKET_ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        logger.exception("unhandled error")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
