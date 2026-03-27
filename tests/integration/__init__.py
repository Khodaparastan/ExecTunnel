"""Integration test package for ExecTunnel.

These tests spin up a real in-process WebSocket server that speaks the
agent protocol, connect a ``TunnelSession`` to it, and drive real SOCKS5
traffic through the tunnel.  They are kept separate from the unit tests
so they can be run selectively:

    python -m pytest tests/integration/ -v

All tests in this package require no external services — everything runs
in-process using asyncio and real TCP sockets on loopback.
"""
