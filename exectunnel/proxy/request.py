"""SOCKS5 request — one completed SOCKS5 handshake.

:class:`Socks5Request` is produced by
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
from .udp_relay import UdpRelay

__all__: list[str] = ["Socks5Request"]


@dataclass(eq=False)
class Socks5Request:
    """One completed SOCKS5 handshake, ready for data relay.

    The handler **must** call :meth:`send_reply_success` **or**
    :meth:`send_reply_error` exactly once before transferring data.

    Usable as an async context manager::

        async with request:
            await request.send_reply_success(bind_port=request.udp_relay.local_port)
            # … relay data …

    Attributes:
        cmd:       The SOCKS5 command.
        host:      Destination hostname or IP address string.
        port:      Destination port number.
        reader:    asyncio stream reader for the client connection.
        writer:    asyncio stream writer for the client connection.
        udp_relay: Bound :class:`UdpRelay` for ``UDP_ASSOCIATE``; ``None`` for ``CONNECT``.
    """

    cmd: Cmd
    host: str
    port: int
    reader: asyncio.StreamReader = field(repr=False)
    writer: asyncio.StreamWriter = field(repr=False)
    udp_relay: UdpRelay | None = field(default=None, repr=False)

    _replied: bool = field(default=False, init=False, repr=False)

    # ── Async context manager ────────────────────────────────────────────────

    async def __aenter__(self) -> Socks5Request:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the writer and any associated UDP relay.

        Idempotent.  ``OSError``, ``RuntimeError``, and
        ``asyncio.CancelledError`` are suppressed because the connection may
        already be torn down.
        """
        if self.udp_relay is not None:
            self.udp_relay.close()

        with contextlib.suppress(OSError, RuntimeError):
            if not self.writer.is_closing():
                self.writer.close()
            await self.writer.wait_closed()

    # ── Properties ───────────────────────────────────────────────────────────

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

    # ── Internal reply guard ─────────────────────────────────────────────────

    def _assert_not_replied(self) -> None:
        """Raise :class:`ProtocolError` on double-reply."""
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

    # ── Async reply helpers ───────────────────────────────────────────────────

    async def send_reply_success(
        self,
        bind_host: str = "127.0.0.1",
        bind_port: int = 0,
    ) -> None:
        """Write and flush a ``SUCCESS`` reply to the SOCKS5 client.

        The writer-closing check is performed *before* the double-reply guard
        so that a closing writer raises without marking the request as replied.

        Args:
            bind_host: The ``BND.ADDR`` to advertise.
            bind_port: The ``BND.PORT`` to advertise.

        Raises:
            ProtocolError:      Writer closing or double-reply.
            ConfigurationError: Invalid bind params.
            OSError:            Socket write/drain failure.
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
                    "The SOCKS5 client disconnected before the reply could be sent. "
                    "This is usually benign."
                ),
            )
        self._assert_not_replied()
        self.writer.write(build_socks5_reply(Reply.SUCCESS, bind_host, bind_port))
        await self.writer.drain()
        metrics_inc("socks5.replies.success", cmd=self.cmd.name)

    async def send_reply_error(
        self,
        reply: Reply = Reply.GENERAL_FAILURE,
    ) -> None:
        """Write and flush an error reply, then close the writer and any UDP relay.

        On double-reply the write is skipped but cleanup still runs.

        Args:
            reply: The reply code.  Defaults to ``GENERAL_FAILURE``.

        Raises:
            ProtocolError:      Double-reply guard.
            ConfigurationError: Invalid reply code (caller bug).
        """
        try:
            self._assert_not_replied()
            packet = build_socks5_reply(reply)
            with contextlib.suppress(OSError):
                self.writer.write(packet)
            metrics_inc("socks5.replies.error", reply=reply.name, cmd=self.cmd.name)
        finally:
            with contextlib.suppress(OSError):
                await self.writer.drain()
            await self.close()
