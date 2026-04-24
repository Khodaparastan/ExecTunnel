"""SOCKS5 handshake state machine.

Extracted from :mod:`exectunnel.proxy.server` so that the accept loop
can focus on lifecycle management while this module owns the pure
protocol negotiation. Every function here reads from the stream, writes
the correct reply on failure paths, and raises :exc:`ProtocolError` on
client-facing errors.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

from exectunnel.exceptions import ProtocolError
from exectunnel.observability import metrics_inc
from exectunnel.protocol import AuthMethod, Cmd, Reply

from ._constants import SOCKS5_VERSION
from ._io import read_exact, read_socks5_addr, write_and_drain_silent
from ._wire import build_socks5_reply
from .tcp_relay import TCPRelay
from .udp_relay import UDPRelay

__all__ = ["negotiate"]

_log: Final[logging.Logger] = logging.getLogger(__name__)


async def negotiate(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    udp_relay_queue_capacity: int,
    udp_drop_warn_interval: int,
) -> TCPRelay | None:
    """Perform the full SOCKS5 handshake and return a ready :class:`TCPRelay`.

    Returns ``None`` for ``BIND`` (error reply already sent to the
    client).

    Args:
        reader: asyncio stream reader for the accepted connection.
        writer: asyncio stream writer for the accepted connection.
        udp_relay_queue_capacity: Inbound datagram queue capacity for
            any spawned :class:`UDPRelay`.
        udp_drop_warn_interval: Drop-warning throttle for any spawned
            :class:`UDPRelay`.

    Returns:
        A fully-negotiated :class:`TCPRelay`, or ``None`` for ``BIND``.

    Raises:
        ProtocolError: Malformed greeting or request from the client.
        TransportError: UDP relay socket bind failure.
    """
    await _negotiate_auth(reader, writer)
    cmd, host, port = await _read_request(reader, writer)

    if cmd is Cmd.CONNECT:
        metrics_inc("socks5.commands.connect")
        return TCPRelay(cmd=cmd, host=host, port=port, reader=reader, writer=writer)

    if cmd is Cmd.UDP_ASSOCIATE:
        metrics_inc("socks5.commands.udp_associate")
        return await _build_udp_request(
            reader=reader,
            writer=writer,
            host=host,
            port=port,
            queue_capacity=udp_relay_queue_capacity,
            drop_warn_interval=udp_drop_warn_interval,
        )

    # BIND — the only remaining variant after strict Cmd construction.
    metrics_inc("socks5.commands.bind_rejected")
    await write_and_drain_silent(writer, build_socks5_reply(Reply.CMD_NOT_SUPPORTED))
    _log.debug("socks5 BIND command rejected (not implemented) for %s:%d", host, port)
    return None


async def _negotiate_auth(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Perform RFC 1928 §3 method-selection, accepting only ``NO_AUTH``.

    Args:
        reader: asyncio stream reader for the accepted connection.
        writer: asyncio stream writer for the accepted connection.

    Raises:
        ProtocolError: On any greeting-level failure. An appropriate
            refusal is written to *writer* before the exception is
            raised so the client sees a valid SOCKS5 rejection.
    """
    header = await read_exact(reader, 2)
    if header[0] != SOCKS5_VERSION:
        raise ProtocolError(
            f"Not a SOCKS5 client: version byte is {header[0]:#x}, expected "
            f"{SOCKS5_VERSION:#x}.",
            details={
                "socks5_field": "VER",
                "expected": f"version byte {SOCKS5_VERSION:#x}",
            },
            hint=(
                "Ensure the connecting client is configured to use SOCKS5. "
                "SOCKS4 and HTTP CONNECT proxies are not supported."
            ),
        )

    nmethods = header[1]
    if nmethods == 0:
        await write_and_drain_silent(
            writer, bytes([SOCKS5_VERSION, int(AuthMethod.NO_ACCEPT)])
        )
        raise ProtocolError(
            "SOCKS5 greeting lists zero authentication methods.",
            details={
                "socks5_field": "NMETHODS",
                "expected": "at least one authentication method",
            },
            hint=("The SOCKS5 client sent a greeting with no authentication methods."),
        )

    methods = await read_exact(reader, nmethods)
    if int(AuthMethod.NO_AUTH) not in methods:
        await write_and_drain_silent(
            writer, bytes([SOCKS5_VERSION, int(AuthMethod.NO_ACCEPT)])
        )
        raise ProtocolError(
            "SOCKS5 client does not offer NO_AUTH (method 0x00).",
            details={
                "socks5_field": "METHODS",
                "expected": (
                    f"method 0x{int(AuthMethod.NO_AUTH):02x} (NO_AUTH) in offered set"
                ),
            },
            hint=(
                "Configure the SOCKS5 client to use no-authentication mode. "
                "Username/password and GSSAPI authentication are not "
                "supported."
            ),
        )

    await write_and_drain_silent(
        writer, bytes([SOCKS5_VERSION, int(AuthMethod.NO_AUTH)])
    )


async def _read_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> tuple[Cmd, str, int]:
    """Read and validate the SOCKS5 request header and destination.

    Args:
        reader: asyncio stream reader for the accepted connection.
        writer: asyncio stream writer for the accepted connection.

    Returns:
        A ``(cmd, host, port)`` tuple. ``host`` is a plain string (no
        brackets for IPv6).

    Raises:
        ProtocolError: On any request-level failure. An appropriate
            refusal is written to *writer* before the exception is
            raised.
    """
    req_header = await read_exact(reader, 3)

    if req_header[0] != SOCKS5_VERSION:
        raise ProtocolError(
            f"Bad SOCKS5 request version: {req_header[0]:#x}, expected "
            f"{SOCKS5_VERSION:#x}.",
            details={
                "socks5_field": "VER",
                "expected": f"version byte {SOCKS5_VERSION:#x}",
            },
            hint="The SOCKS5 client sent a malformed request header.",
        )

    if req_header[2] != 0x00:
        await write_and_drain_silent(writer, build_socks5_reply(Reply.GENERAL_FAILURE))
        raise ProtocolError(
            f"SOCKS5 request RSV byte is {req_header[2]:#x}, expected 0x00.",
            details={
                "socks5_field": "RSV",
                "expected": "RSV byte 0x00 (RFC 1928 §4)",
            },
            hint=("The SOCKS5 client sent a non-zero RSV byte, violating RFC 1928 §4."),
        )

    try:
        cmd = Cmd(req_header[1])
    except ValueError as exc:
        await write_and_drain_silent(
            writer, build_socks5_reply(Reply.CMD_NOT_SUPPORTED)
        )
        raise ProtocolError(
            f"Unsupported SOCKS5 command: {req_header[1]:#x}.",
            details={
                "socks5_field": "CMD",
                "expected": "CMD 0x01 (CONNECT) or 0x03 (UDP_ASSOCIATE)",
            },
            hint=(
                "Only CONNECT (0x01) and UDP_ASSOCIATE (0x03) are supported. "
                "BIND (0x02) is not implemented."
            ),
        ) from exc

    host, port = await read_socks5_addr(
        reader,
        allow_port_zero=cmd in (Cmd.UDP_ASSOCIATE, Cmd.BIND),
    )
    return cmd, host, port


async def _build_udp_request(
    *,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    host: str,
    port: int,
    queue_capacity: int,
    drop_warn_interval: int,
) -> TCPRelay:
    """Spawn a :class:`UDPRelay` and return the associated :class:`TCPRelay`.

    Args:
        reader: asyncio stream reader for the accepted connection.
        writer: asyncio stream writer for the accepted connection.
        host: Destination host from the request.
        port: Destination port from the request.
        queue_capacity: Inbound datagram queue capacity for the new
            relay.
        drop_warn_interval: Drop-warning throttle for the new relay.

    Returns:
        A :class:`TCPRelay` with ``udp_relay`` bound to the new
        :class:`UDPRelay` instance.

    Raises:
        TransportError: If the UDP relay fails to bind. A
            ``GENERAL_FAILURE`` reply is written before the exception
            propagates.
    """
    relay = UDPRelay(
        queue_capacity=queue_capacity,
        drop_warn_interval=drop_warn_interval,
    )
    try:
        await relay.start(expected_client_addr=(host, port) if port != 0 else None)
    except Exception:
        relay.close()
        await write_and_drain_silent(writer, build_socks5_reply(Reply.GENERAL_FAILURE))
        raise

    return TCPRelay(
        cmd=Cmd.UDP_ASSOCIATE,
        host=host,
        port=port,
        reader=reader,
        writer=writer,
        udp_relay=relay,
    )
