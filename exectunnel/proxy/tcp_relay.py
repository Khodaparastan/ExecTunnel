"""SOCKS5 request — one completed SOCKS5 handshake.

:class:`TCPRelay` is produced by
:class:`~exectunnel.proxy.server.Socks5Server` after a successful SOCKS5
negotiation and handed to the session layer for data relay.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from exectunnel.exceptions import ProtocolError
from exectunnel.observability import metrics_inc
from exectunnel.protocol import Cmd, Reply

from ._wire import build_socks5_reply
from .udp_relay import UDPRelay

__all__ = ["TCPRelay"]


@dataclass(eq=False)
class TCPRelay:
    """One completed SOCKS5 handshake, ready for data relay.

    The handler **must** call :meth:`send_reply_success` **or**
    :meth:`send_reply_error` exactly once before transferring data.

    Usable as an async context manager::

        async with request:
            await request.send_reply_success(bind_port=request.udp_relay.local_port)
            # … relay data …

    Attributes:
        cmd: The SOCKS5 command (``CONNECT`` or ``UDP_ASSOCIATE``).
        host: Destination hostname or IP address string.
        port: Destination port number.
        reader: asyncio stream reader for the client connection.
        writer: asyncio stream writer for the client connection.
        udp_relay: Bound :class:`UDPRelay` for ``UDP_ASSOCIATE``;
            ``None`` for ``CONNECT``.
    """

    cmd: Cmd
    host: str
    port: int
    reader: asyncio.StreamReader = field(repr=False)
    writer: asyncio.StreamWriter = field(repr=False)
    udp_relay: UDPRelay | None = field(default=None, repr=False)

    _replied: bool = field(default=False, init=False, repr=False)

    async def __aenter__(self) -> TCPRelay:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the TCP writer and any associated UDP relay.

        Idempotent. :exc:`OSError` and :exc:`RuntimeError` are
        suppressed — the connection may already be torn down by the
        time this is called.
        """
        if self.udp_relay is not None:
            self.udp_relay.close()

        with contextlib.suppress(OSError, RuntimeError):
            self.writer.close()
            await self.writer.wait_closed()

    # ── Query properties ──────────────────────────────────────────────────────

    @property
    def is_connect(self) -> bool:
        """``True`` when the SOCKS5 command is ``CONNECT``."""
        return self.cmd == Cmd.CONNECT

    @property
    def is_udp_associate(self) -> bool:
        """``True`` when the SOCKS5 command is ``UDP_ASSOCIATE``."""
        return self.cmd == Cmd.UDP_ASSOCIATE

    @property
    def replied(self) -> bool:
        """``True`` once a reply has been sent to the SOCKS5 client."""
        return self._replied

    # ── Reply API ─────────────────────────────────────────────────────────────

    def _consume_reply_slot(self) -> None:
        """Mark the reply slot consumed, raising on a double-reply.

        Raises:
            ProtocolError: If a reply has already been sent — always a
                caller bug.
        """
        if self._replied:
            raise ProtocolError(
                f"A SOCKS5 reply has already been sent for "
                f"{self.host}:{self.port} — cannot send a second reply.",
                details={
                    "socks5_field": "SOCKS5_REPLY",
                    "expected": "exactly one reply per request",
                },
                hint=(
                    "Ensure the request handler calls send_reply_success() or "
                    "send_reply_error() exactly once."
                ),
            )
        self._replied = True

    async def send_reply_success(
        self,
        bind_host: str = "127.0.0.1",
        bind_port: int = 0,
    ) -> None:
        """Write and flush a ``SUCCESS`` reply to the SOCKS5 client.

        The writer-closing check is performed *before* the double-reply
        guard so that a closing writer raises without consuming the
        reply slot.

        Args:
            bind_host: The ``BND.ADDR`` to advertise.
            bind_port: The ``BND.PORT`` to advertise.

        Raises:
            ProtocolError: Writer is closing or double-reply detected.
            ConfigurationError: Invalid *bind_host* or *bind_port*.
            OSError: Socket write or drain failure.
        """
        if self.writer.is_closing():
            raise ProtocolError(
                f"Cannot send SUCCESS reply for {self.host}:{self.port} — "
                "the writer transport is already closing.",
                details={
                    "socks5_field": "SOCKS5_REPLY",
                    "expected": "open writer transport",
                },
                hint=(
                    "The SOCKS5 client disconnected before the reply could be "
                    "sent. This is usually benign."
                ),
            )
        self._consume_reply_slot()
        self.writer.write(build_socks5_reply(Reply.SUCCESS, bind_host, bind_port))
        await self.writer.drain()
        metrics_inc("socks5.replies.success", cmd=self.cmd.name)

    async def send_reply_error(
        self,
        reply: Reply = Reply.GENERAL_FAILURE,
    ) -> None:
        """Write and flush an error reply, then close the writer and any UDP relay.

        Cleanup always runs even when a double-reply is detected: the
        write is skipped, but ``drain`` and ``close`` still execute
        before :exc:`ProtocolError` propagates to the caller.

        Args:
            reply: The reply code. Defaults to
                :attr:`Reply.GENERAL_FAILURE`.

        Raises:
            ProtocolError: Double-reply detected — always a caller bug.
            ConfigurationError: Invalid *reply* code — always a caller
                bug.
        """
        try:
            self._consume_reply_slot()
            packet = build_socks5_reply(reply)
            with contextlib.suppress(OSError):
                self.writer.write(packet)
            metrics_inc("socks5.replies.error", reply=reply.name, cmd=self.cmd.name)
        finally:
            with contextlib.suppress(OSError):
                await self.writer.drain()
            await self.close()
