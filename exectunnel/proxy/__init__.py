"""Proxy domain: SOCKS5 server, relay, request handling, and DNS forwarding."""

from exectunnel.proxy._codec import build_reply, read_addr, read_exact, validate_domain
from exectunnel.proxy.dns_forwarder import DnsForwarder
from exectunnel.proxy.relay import UdpRelay
from exectunnel.proxy.request import Socks5Request
from exectunnel.proxy.server import Socks5Server

__all__ = [
    # ── Server ─────────────────────────────────────────────────────────────
    "Socks5Server",
    # ── Request ────────────────────────────────────────────────────────────
    "Socks5Request",
    # ── UDP relay ──────────────────────────────────────────────────────────
    "UdpRelay",
    # ── DNS forwarding ─────────────────────────────────────────────────────
    "DnsForwarder",
    # ── Codec helpers ──────────────────────────────────────────────────────
    "build_reply",
    "read_addr",
    "read_exact",
    "validate_domain",
]
