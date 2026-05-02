#!/usr/bin/env python3
"""Brute-force discover which input format a browser-terminal WebSocket accepts.

Each candidate is tested on a **fresh** connection to prevent shell state
pollution from previously sent (and possibly partially executed) commands.

On success, prints the working format name and TTY status, then exits 0.
On failure (no format matched), exits 1.

All format definitions live in probe_common.PROTOCOL_CANDIDATES — this tool
adds no new candidates of its own, ensuring the discovery logic is always
identical across all probe tools.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

import websocket

from probe_common import (
    Frame,
    PROTOCOL_CANDIDATES,
    ShellProtocol,
    add_connection_args,
    build_headers,
    build_sslopt,
    json_compact,
    printable,
    recv_frames,
    ws_connect,
)

logger = logging.getLogger("exectunnel.probe.ws_input")

# ─────────────────────────────────────────────────────────────────────────────
# Probe script
# ─────────────────────────────────────────────────────────────────────────────

_PROBE_SCRIPT = (
    "printf '\\n__TTY_PROBE_START__\\n'; "
    "if [ -t 0 ]; then echo stdin_is_tty=true;  else echo stdin_is_tty=false;  fi; "
    "if [ -t 1 ]; then echo stdout_is_tty=true; else echo stdout_is_tty=false; fi; "
    "if [ -t 2 ]; then echo stderr_is_tty=true; else echo stderr_is_tty=false; fi; "
    'echo "term=${TERM:-}"; '
    "printf '__TTY_PROBE_END__\\n'"
)


def _build_probe_payloads() -> list[tuple[str, str | bytes]]:
    """Build (name, payload) pairs from PROTOCOL_CANDIDATES.

    The payload for each candidate is the probe script framed exactly as that
    protocol would frame it when sending a shell command.  This guarantees
    probe_ws_input and probe_net_metrics test identical input formats.
    """
    import io, types

    results: list[tuple[str, str | bytes]] = []
    for proto in PROTOCOL_CANDIDATES:
        # Simulate what ShellProtocol.send_command would actually put on the wire
        text = _PROBE_SCRIPT + proto.line_ending
        framed: str | bytes = proto._wrap(text)  # type: ignore[attr-defined]
        results.append((proto.name, framed))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Per-candidate trial
# ─────────────────────────────────────────────────────────────────────────────


def _try_candidate(
    args: argparse.Namespace,
    name: str,
    payload: str | bytes,
    read_seconds: float,
) -> tuple[list[Frame], bool]:
    """Open a fresh connection, send *payload*, collect response, close.

    Returns ``(frames, hit)`` where *hit* is True when probe markers appear.
    """
    headers = build_headers(
        args.header,
        origin=args.origin,
        user_agent=args.user_agent,
    )
    sslopt = build_sslopt(args.url, insecure=args.insecure, ca_file=args.ca_file)

    try:
        ws = ws_connect(args.url, headers=headers, sslopt=sslopt, timeout=args.timeout)
    except Exception as exc:
        logger.warning("connection failed for %s: %s", name, exc)
        return [], False

    try:
        # Drain banner / prompt before sending
        recv_frames(ws, min(1.0, read_seconds / 2))

        if isinstance(payload, bytes):
            ws.send_binary(payload)
        else:
            ws.send(payload)

        frames = recv_frames(ws, read_seconds)
        combined = b"".join(f.payload for f in frames).decode("utf-8", errors="replace")
        hit = "__TTY_PROBE_START__" in combined or "stdin_is_tty=" in combined
        return frames, hit
    finally:
        try:
            ws.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────────────────────


def _print_frames(frames: list[Frame]) -> None:
    if not frames:
        print("  <no response>")
        return
    for i, fr in enumerate(frames, 1):
        print(f"  frame[{i}] type={fr.kind} len={len(fr.payload)}")
        preview = (
            printable(fr.payload)[:300] if fr.kind == "text" else repr(fr.payload[:256])
        )
        print(f"    {preview}")


def _report_tty(text: str) -> None:
    if "stdin_is_tty=true" in text or "stdout_is_tty=true" in text:
        print("TTY_DETECTED=true")
        print("Interactive terminal — not safe for raw binary framing.")
    elif "stdin_is_tty=false" in text and "stdout_is_tty=false" in text:
        print("TTY_DETECTED=false")
        print("Non-TTY detected — run binary-clean test to confirm.")
    else:
        print("TTY_DETECTED=unknown")


# ─────────────────────────────────────────────────────────────────────────────
# Main discovery loop
# ─────────────────────────────────────────────────────────────────────────────


def run(args: argparse.Namespace) -> int:
    candidates = _build_probe_payloads()

    print(f"Probing {len(candidates)} input format candidates against {args.url}")
    print()

    for name, payload in candidates:
        label = (
            f"binary, len={len(payload)}"
            if isinstance(payload, bytes)
            else f"text, len={len(payload)}"
        )
        print(f"== trying {name} ({label}) ==")

        frames, hit = _try_candidate(args, name, payload, args.read_seconds)
        _print_frames(frames)

        if hit:
            combined = b"".join(f.payload for f in frames).decode(
                "utf-8", errors="replace"
            )
            print()
            print("FOUND_WORKING_INPUT_FORMAT")
            print(f"format={name}")
            _report_tty(combined)
            return 0

    print()
    print("NO_WORKING_INPUT_FORMAT_FOUND")
    print("Troubleshooting:")
    print(
        "  * Increase --read-seconds (currently: "
        f"{args.read_seconds:.1f}s per candidate)"
    )
    print("  * Run with -v to see per-candidate wire details")
    print("  * Verify the URL actually reaches an interactive shell")
    return 1


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Brute-force discover which input format a browser-terminal "
            "WebSocket accepts. Each candidate is tested on a fresh connection."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s --url wss://stream.runflare.com/ws
  %(prog)s --url wss://stream.runflare.com/ws --read-seconds 3 -v
""",
    )
    add_connection_args(
        parser, include_token=True, include_subprotocol=False, default_timeout=10.0
    )

    og = parser.add_argument_group("operation")
    og.add_argument(
        "--read-seconds",
        type=float,
        default=2.0,
        help="Seconds to wait for response per candidate (default: 2)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return run(args)
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
