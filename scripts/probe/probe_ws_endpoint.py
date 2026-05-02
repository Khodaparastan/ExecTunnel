#!/usr/bin/env python3
"""Connect, TTY-probe, or binary-smoke-test a generic WebSocket endpoint.

Designed for the nginx reverse-proxy path (stream.runflare.com) where the
K8s channel-byte framing is stripped and replaced with a raw pipe.  Works
equally against any browser-terminal WebSocket.

Modes
-----
connect       Open a connection, print the negotiated subprotocol and any
              initial data, then exit.
tty-probe     Send a shell TTY-detection script using the discovered input
              format and report whether a TTY is present.
binary-smoke  Send 256 bytes of non-trivial binary and check if they echo
              back intact.  Only conclusive if the remote echoes stdin.
"""

from __future__ import annotations

import argparse
import logging
import sys

import websocket

from probe_common import (
    PROTOCOL_CANDIDATES,
    Frame,
    ShellProtocol,
    add_connection_args,
    build_headers,
    build_sslopt,
    discover_shell_protocol,
    make_binary_payload,
    parse_header,
    printable,
    probe_connection,
    recv_all_bytes,
    recv_frames,
    ws_connect,
    FrameMode,
    ExecMode,
)

logger = logging.getLogger("exectunnel.probe.ws_endpoint")

# ─────────────────────────────────────────────────────────────────────────────
# Shared connection helper
# ─────────────────────────────────────────────────────────────────────────────


def _connect(args: argparse.Namespace) -> websocket.WebSocket:
    headers = build_headers(
        args.header,
        origin=args.origin,
        user_agent=args.user_agent,
    )
    sslopt = build_sslopt(args.url, insecure=args.insecure, ca_file=args.ca_file)
    subprotocols = args.subprotocol or None
    return ws_connect(
        args.url,
        headers=headers,
        subprotocols=subprotocols,
        sslopt=sslopt,
        timeout=args.timeout,
    )


def _print_frames(frames: list[Frame]) -> None:
    if not frames:
        print("  <no response>")
        return
    for i, fr in enumerate(frames, 1):
        print(f"  frame[{i}] type={fr.kind} len={len(fr.payload)}")
        if fr.kind == "text":
            print(f"    {printable(fr.payload)[:500]}")
        else:
            print(f"    {fr.payload[:256]!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Mode: connect
# ─────────────────────────────────────────────────────────────────────────────


def run_connect(args: argparse.Namespace) -> int:
    """Open a WebSocket, report negotiated parameters, print initial data."""
    print("== WebSocket connect test ==")
    print(f"url={args.url}")

    ws = _connect(args)
    try:
        print("CONNECTED=true")
        print(f"selected_subprotocol={ws.getsubprotocol()!r}")

        data = recv_all_bytes(ws, args.read_seconds)
        if data:
            print("-- initial data --")
            print(printable(data))
        else:
            print("initial_data=<none>")

        return 0
    finally:
        ws.close()


# ─────────────────────────────────────────────────────────────────────────────
# Mode - tty-probe
# ─────────────────────────────────────────────────────────────────────────────

_TTY_PROBE_SCRIPT = (
    "printf '\\n__EXECTUNNEL_TTY_PROBE_START__\\n'; "
    "if [ -t 0 ]; then echo stdin_is_tty=true;  else echo stdin_is_tty=false;  fi; "
    "if [ -t 1 ]; then echo stdout_is_tty=true; else echo stdout_is_tty=false; fi; "
    "if [ -t 2 ]; then echo stderr_is_tty=true; else echo stderr_is_tty=false; fi; "
    'echo "term=${TERM:-}"; '
    "if command -v tty >/dev/null 2>&1; then printf 'tty_stdin='; tty 2>/dev/null || true; fi; "
    "printf '__EXECTUNNEL_TTY_PROBE_END__\\n'"
)


def run_tty_probe(args: argparse.Namespace) -> int:
    """Discover the input format then execute a TTY-detection script."""
    print("== Terminal TTY probe ==")
    print(f"url={args.url}")
    print()

    # Build a minimal ConnectionProfile from args so we can call
    # discover_shell_protocol without duplicating connection logic.
    # We override frame_mode / exec_mode to RAW / SHELL since this
    # tool targets the proxy (no channel bytes, no ?command=).
    import types

    proxy_args = types.SimpleNamespace(**vars(args))
    proxy_args.frame_mode = FrameMode.RAW.value
    proxy_args.exec_mode = ExecMode.SHELL.value
    proxy_args.token = getattr(args, "token", None)
    proxy_args.token_file = getattr(args, "token_file", None)

    try:
        profile = probe_connection(proxy_args)
    except Exception as exc:
        print(f"CONNECTION_PROBE_FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"CONNECTED=true")
    print(f"selected_subprotocol={profile.selected_subprotocol!r}")
    print(f"frame_mode={profile.frame_mode.value}")
    print()

    print(
        f"▸ Discovering input protocol "
        f"({len(PROTOCOL_CANDIDATES)} candidates, "
        f"≤{args.initial_wait:.1f}s each) …"
    )

    proto, ws, banner = discover_shell_protocol(
        profile,
        initial_wait=args.initial_wait,
        per_candidate_wait=args.initial_wait,
    )

    if proto is None:
        print("INPUT_FORMAT=unknown")
        print("Could not discover a working input format.")
        if banner:
            print(f"Banner ({len(banner)} bytes):")
            for line in printable(banner[:500]).splitlines()[:10]:
                print(f"  {line}")
        return 1

    print(f"INPUT_FORMAT={proto.name}")
    print()

    try:
        # Send the TTY probe script using the discovered protocol
        proto.send_command(ws, _TTY_PROBE_SCRIPT)
        frames = recv_frames(ws, args.read_seconds)
    finally:
        try:
            ws.close()
        except Exception:
            pass

    combined = b"".join(f.payload for f in frames)
    text = printable(combined)

    print("-- probe output --")
    _print_frames(frames)
    print()

    if "stdin_is_tty=true" in text or "stdout_is_tty=true" in text:
        print("TTY_DETECTED=true")
        print("Interactive terminal — not safe for arbitrary binary framing.")
        return 0

    if "stdin_is_tty=false" in text and "stdout_is_tty=false" in text:
        print("TTY_DETECTED=false")
        print("Non-TTY detected — run binary-clean test to confirm.")
        return 0

    if "__EXECTUNNEL_TTY_PROBE_START__" not in text:
        print("TTY_DETECTED=unknown")
        print(
            "Probe markers not found in response — shell may not have executed the script."
        )
        return 1

    print("TTY_DETECTED=unknown")
    return 1


# ─────────────────────────────────────────────────────────────────────────────
# Mode: binary-smoke
# ─────────────────────────────────────────────────────────────────────────────


def run_binary_smoke(args: argparse.Namespace) -> int:
    """Send 256 bytes of non-trivial binary; check if they echo back intact.

    Only conclusive when the remote side echoes stdin verbatim (e.g. ``cat``).
    A failure here does *not* prove corruption — it may just mean nothing echoes.
    """
    print("== Binary smoke test ==")
    print("NOTE: conclusive only if remote echoes stdin → stdout (e.g. via `cat`).")
    print()

    ws = _connect(args)
    try:
        print("CONNECTED=true")
        print(f"selected_subprotocol={ws.getsubprotocol()!r}")

        payload = make_binary_payload(256)
        ws.send_binary(payload)
        data = recv_all_bytes(ws, args.read_seconds)

        print(f"sent_len={len(payload)}")
        print(f"recv_len={len(data)}")

        if data:
            print("-- received preview --")
            print(repr(data[:256]))

        if payload in data:
            print("BINARY_SMOKE=PASS")
            return 0

        print("BINARY_SMOKE=INCONCLUSIVE")
        print("Remote side did not echo the payload — does not prove corruption.")
        return 1
    finally:
        ws.close()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

_MODES: dict[str, Any] = {
    "connect": run_connect,
    "tty-probe": run_tty_probe,
    "binary-smoke": run_binary_smoke,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe a generic wss:// terminal/exec endpoint (proxy path).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s --url wss://stream.runflare.com/ws --mode tty-probe
  %(prog)s --url wss://stream.runflare.com/ws --mode binary-smoke
  %(prog)s --url wss://stream.runflare.com/ws --mode connect --read-seconds 3
""",
    )
    add_connection_args(parser, include_token=True, default_timeout=10.0)

    og = parser.add_argument_group("operation")
    og.add_argument(
        "--mode",
        choices=list(_MODES),
        default="tty-probe",
        help="Probe mode (default: tty-probe)",
    )
    og.add_argument(
        "--read-seconds",
        type=float,
        default=5.0,
        help="How long to wait for response data (default: 5)",
    )
    og.add_argument(
        "--initial-wait",
        type=float,
        default=2.0,
        help="Seconds to wait for shell banner before probing (default: 2)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return _MODES[args.mode](args)

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
