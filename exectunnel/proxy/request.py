"""SOCKS5 request data class."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from exectunnel.exceptions import ProtocolError
from exectunnel.protocol.enums import Cmd, Reply
from exectunnel.proxy._codec import build_reply
from exectunnel.proxy.relay import UdpRelay


@dataclass
class Socks5Request:
    """Represents one completed SOCKS5 handshake.

    The handler MUST call :meth:`send_reply_success` **or**
    :meth:`send_reply_error` exactly once before transferring data.

    The request is also usable as an async context manager that guarantees
    cleanup even if the handler raises before sending a reply::

        async with request:
            await request.send_reply_success()
            ...  # relay data

    Attributes:
        cmd:       The SOCKS5 command (:class:`~exectunnel.protocol.enums.Cmd`).
        host:      Destination hostname or IP address string.
        port:      Destination port number.
        reader:    asyncio stream reader for the SOCKS5 client connection.
        writer:    asyncio stream writer for the SOCKS5 client connection.
        udp_relay: Bound :class:`UdpRelay` instance for ``UDP_ASSOCIATE``
                   requests; ``None`` for ``CONNECT``.
    """

    cmd: Cmd
    host: str
    port: int
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    udp_relay: UdpRelay | None = field(default=None)

    # Tracks whether a reply has already been sent so double-reply bugs are
    # caught at runtime rather than silently corrupting the SOCKS5 stream.
    _replied: bool = field(default=False, init=False, repr=False)

    # ── Async context manager ─────────────────────────────────────────────────

    async def __aenter__(self) -> Socks5Request:
        return self

    async def __aexit__(self, *_: object) -> None:
        """Guarantee writer and relay are closed on exit, regardless of outcome."""
        await self.close()

    # ── Cleanup ───────────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the writer and any associated UDP relay.

        Idempotent — safe to call multiple times.  All ``OSError`` exceptions
        are suppressed because the connection may already be half-closed by the
        time this is called.
        """
        if self.udp_relay is not None:
            self.udp_relay.close()
        with contextlib.suppress(OSError, RuntimeError):
            self.writer.close()
            await self.writer.wait_closed()

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def is_connect(self) -> bool:
        """``True`` when the SOCKS5 command is ``CONNECT``."""
        return self.cmd == Cmd.CONNECT

    @property
    def is_udp(self) -> bool:
        """``True`` when the SOCKS5 command is ``UDP_ASSOCIATE``."""
        return self.cmd == Cmd.UDP_ASSOCIATE

    @property
    def replied(self) -> bool:
        """``True`` once a reply has been sent to the SOCKS5 client."""
        return self._replied

    # ── Internal reply guard ──────────────────────────────────────────────────

    def _mark_replied(self) -> None:
        """Raise :class:`ProtocolError` if a reply has already been sent.

        Raises:
            ProtocolError: On double-reply attempt.
        """
        if self._replied:
            raise ProtocolError(
                f"A SOCKS5 reply has already been sent for "
                f"{self.host}:{self.port} — cannot send a second reply.",
                error_code="protocol.socks5_double_reply",
                details={
                    "host": self.host,
                    "port": self.port,
                    "cmd": self.cmd.name,
                },
                hint=(
                    "Ensure the request handler calls send_reply_success() or "
                    "send_reply_error() exactly once."
                ),
            )
        self._replied = True

    # ── Synchronous reply helpers (buffer only — caller must drain) ───────────

    def reply_success(
        self,
        bind_host: str = "127.0.0.1",
        bind_port: int = 0,
    ) -> None:
        """Queue a ``SUCCESS`` reply into the writer buffer.

        The caller is responsible for calling ``await writer.drain()``
        after this method returns.

        Args:
            bind_host: The ``BND.ADDR`` to advertise.  Defaults to
                       ``"127.0.0.1"``.
            bind_port: The ``BND.PORT`` to advertise.  Defaults to ``0``.

        Raises:
            ProtocolError:
                * If a reply has already been sent (double-reply guard).
                * If the writer transport is already closing.
            ConfigurationError:
                If *bind_host* or *bind_port* are invalid (propagated from
                :func:`build_reply`).
        """
        self._mark_replied()
        if self.writer.is_closing():
            raise ProtocolError(
                f"Cannot send SUCCESS reply for {self.host}:{self.port} — "
                "the writer transport is already closing.",
                error_code="protocol.socks5_reply_on_closed_writer",
                details={
                    "host": self.host,
                    "port": self.port,
                    "reply": "SUCCESS",
                },
                hint=(
                    "The SOCKS5 client disconnected before the reply could be sent. "
                    "This is usually benign."
                ),
            )
        self.writer.write(build_reply(Reply.SUCCESS, bind_host, bind_port))

    def reply_error(self, reply: Reply = Reply.GENERAL_FAILURE) -> None:
        """Queue an error reply into the writer buffer.

        The caller is responsible for calling ``await writer.drain()``
        after this method returns.

        Unlike :meth:`reply_success`, this method does **not** guard on
        ``writer.is_closing()`` — error replies must be attempted even on a
        half-closed connection so the client receives a well-formed SOCKS5
        rejection rather than a bare TCP RST.

        Args:
            reply: The :class:`~exectunnel.protocol.enums.Reply` code to send.
                   Defaults to :attr:`~exectunnel.protocol.enums.Reply.GENERAL_FAILURE`.

        Raises:
            ProtocolError:
                If a reply has already been sent (double-reply guard).
            ConfigurationError:
                If *reply* is not a valid :class:`Reply` member (propagated
                from :func:`build_reply`).
        """
        self._mark_replied()
        with contextlib.suppress(RuntimeError, OSError):
            self.writer.write(build_reply(reply))

    # ── Async reply helpers (write + drain + optional close) ─────────────────

    async def send_reply_success(
        self,
        bind_host: str = "127.0.0.1",
        bind_port: int = 0,
    ) -> None:
        """Write and flush a ``SUCCESS`` reply to the SOCKS5 client.

        Does **not** close the writer — the caller owns the connection after a
        successful reply and is responsible for data relay and teardown.

        Args:
            bind_host: The ``BND.ADDR`` to advertise.  Defaults to
                       ``"127.0.0.1"``.
            bind_port: The ``BND.PORT`` to advertise.  Defaults to ``0``.

        Raises:
            ProtocolError:
                * Double-reply guard (propagated from :meth:`reply_success`).
                * Writer already closing (propagated from :meth:`reply_success`).
            ConfigurationError:
                Invalid *bind_host* / *bind_port* (propagated from
                :func:`build_reply`).
            OSError:
                If the underlying socket write or drain fails.
        """
        self.reply_success(bind_host, bind_port)
        await self.writer.drain()

    async def send_reply_error(
        self,
        reply: Reply = Reply.GENERAL_FAILURE,
    ) -> None:
        """Write and flush an error reply, then close the writer and any UDP relay.

        Errors during drain or close are suppressed — the connection is being
        torn down regardless and a secondary ``OSError`` must not shadow the
        original failure that caused the error reply.

        Args:
            reply: The :class:`~exectunnel.protocol.enums.Reply` code to send.
                   Defaults to :attr:`~exectunnel.protocol.enums.Reply.GENERAL_FAILURE`.

        Raises:
            ProtocolError:
                Double-reply guard (propagated from :meth:`reply_error`).
            ConfigurationError:
                Invalid *reply* code (propagated from :func:`build_reply`).
                This is a caller bug and is **not** suppressed.
        """
        # reply_error() may raise ProtocolError (double-reply) or
        # ConfigurationError (bad Reply value) — both propagate to the caller.
        self.reply_error(reply)

        with contextlib.suppress(OSError):
            await self.writer.drain()

        await self.close()
