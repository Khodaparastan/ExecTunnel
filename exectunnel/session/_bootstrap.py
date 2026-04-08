"""Agent bootstrap — uploads and starts agent.py in the remote pod.

Responsibilities
----------------
* Send shell commands over the WebSocket exec channel.
* Upload the agent payload in base64 chunks.
* Wait for ``AGENT_READY`` with a configurable timeout.
* Detect and surface ``SyntaxError`` / version mismatch from the remote.
* Buffer frames received after ``AGENT_READY`` for replay by ``FrameReceiver``.
"""

import asyncio
import collections
import logging

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from exectunnel.config.defaults import (
    BOOTSTRAP_CHUNK_SIZE_CHARS,
    BOOTSTRAP_DECODE_DELAY_SECS,
    BOOTSTRAP_DIAG_MAX_LINES,
    BOOTSTRAP_RM_DELAY_SECS,
    BOOTSTRAP_STTY_DELAY_SECS,
)
from exectunnel.config.settings import AppConfig, TunnelConfig
from exectunnel.exceptions import (
    AgentReadyTimeoutError,
    AgentSyntaxError,
    AgentVersionMismatchError,
    BootstrapError,
    FrameDecodingError,
)
from exectunnel.observability import metrics_inc, metrics_observe, span
from exectunnel.protocol import is_ready_frame

from ._payload import load_agent_b64

__all__: list[str] = []  # internal module

logger = logging.getLogger(__name__)


class AgentBootstrapper:
    """Uploads and starts the agent script on the remote pod.

    After a successful :meth:`run`, callers must read
    :attr:`post_ready_lines` and :attr:`pre_ready_carry` to replay any
    frames that arrived between ``AGENT_READY`` and ``FrameReceiver``
    taking over the WebSocket iterator.

    Args:
        ws:       Live WebSocket connection to the pod exec channel.
        app_cfg:  Application-level configuration.
        tun_cfg:  Tunnel-level configuration.
    """

    __slots__ = (
        "_ws",
        "_app",
        "_tun",
        "_diag",
        "_post_ready_lines",
        "_pre_ready_carry",
    )

    def __init__(
        self,
        ws: ClientConnection,
        app_cfg: AppConfig,
        tun_cfg: TunnelConfig,
    ) -> None:
        self._ws = ws
        self._app = app_cfg
        self._tun = tun_cfg
        self._diag: collections.deque[str] = collections.deque(
            maxlen=BOOTSTRAP_DIAG_MAX_LINES
        )
        self._post_ready_lines: list[str] = []
        self._pre_ready_carry: str = ""

    # ── Read-only properties consumed by TunnelSession ────────────────────────

    @property
    def post_ready_lines(self) -> list[str]:
        """Lines received after ``AGENT_READY``, buffered for replay."""
        return self._post_ready_lines

    @property
    def pre_ready_carry(self) -> str:
        """Partial (unterminated) frame fragment from the bootstrap read."""
        return self._pre_ready_carry

    # ── Public entry point ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Upload and start the agent.  Raises on any bootstrap failure.

        Raises:
            AgentReadyTimeoutError:    ``AGENT_READY`` not received in time.
            AgentSyntaxError:          Remote Python raised ``SyntaxError``.
            AgentVersionMismatchError: Remote agent version incompatible.
            BootstrapError:            Any other startup failure.
        """
        loop = asyncio.get_running_loop()
        start = loop.time()
        metrics_inc("tunnel.bootstrap.started")

        with span("session.bootstrap"):
            await self._upload_agent(start=start)

            logger.info("waiting for agent AGENT_READY…")
            try:
                async with asyncio.timeout(self._tun.ready_timeout):
                    await self._wait_for_ready(start=start)
            except TimeoutError as exc:
                detail = (
                    f"; last output: {self._diag[-1]}" if self._diag else ""
                )
                metrics_inc("tunnel.bootstrap.timeout")
                raise AgentReadyTimeoutError(
                    f"Agent did not signal AGENT_READY within "
                    f"{self._tun.ready_timeout}s{detail}",
                    details={
                        "timeout_s": self._tun.ready_timeout,
                        "host": self._app.wss_url,
                        "last_output": self._diag[-1] if self._diag else None,
                    },
                    hint=(
                        "Increase EXECTUNNEL_AGENT_TIMEOUT or check the remote "
                        "Python version and available memory."
                    ),
                ) from exc

            logger.info(
                "agent ready — SOCKS5 on %s:%d",
                self._tun.socks_host,
                self._tun.socks_port,
            )
            metrics_inc("bootstrap.exec_done")
            metrics_inc("tunnel.bootstrap.ok")
            metrics_observe(
                "session_bootstrap_duration_seconds",
                asyncio.get_running_loop().time() - start,
            )

    # ── Upload ────────────────────────────────────────────────────────────────

    async def _send_command(self, cmd: str, *, start: float) -> None:
        """Send one shell command over the WebSocket exec channel.

        Sends as a text frame — the kubectl exec channel is text-mode.

        Args:
            cmd:   Shell command string (no trailing newline needed).
            start: Bootstrap start time from ``loop.time()`` for elapsed_s.

        Raises:
            BootstrapError: If the WebSocket closes before the command is sent.
        """
        try:
            await self._ws.send(cmd + "\n")
        except ConnectionClosed as exc:
            elapsed = asyncio.get_running_loop().time() - start
            raise BootstrapError(
                "WebSocket closed while sending bootstrap command.",
                details={
                    "command": cmd[:80],
                    "host": self._app.wss_url,
                    "elapsed_s": elapsed,
                },
                hint="Check that the remote shell is still alive.",
            ) from exc

    async def _upload_agent(self, *, start: float) -> None:
        """Suppress terminal echo, upload the agent payload, and exec it."""
        await self._send_command("stty raw -echo", start=start)
        await asyncio.sleep(BOOTSTRAP_STTY_DELAY_SECS)
        metrics_inc("bootstrap.stty_done")

        agent_b64 = load_agent_b64()
        chunk_count = len(range(0, len(agent_b64), BOOTSTRAP_CHUNK_SIZE_CHARS))
        metrics_observe("tunnel.bootstrap.chunks", float(chunk_count))

        await self._send_command(
            "rm -f '/tmp/exectunnel_agent.py' '/tmp/exectunnel_agent.b64'",
            start=start,
        )
        await asyncio.sleep(BOOTSTRAP_RM_DELAY_SECS)

        metrics_inc("bootstrap.upload_started")
        for i in range(0, len(agent_b64), BOOTSTRAP_CHUNK_SIZE_CHARS):
            chunk = agent_b64[i : i + BOOTSTRAP_CHUNK_SIZE_CHARS]
            await self._send_command(
                f"printf '%s' '{chunk}' >> '/tmp/exectunnel_agent.b64'",
                start=start,
            )

        metrics_inc("bootstrap.upload_done")
        await self._send_command(
            "sed 's/-/+/g; s/_/\\//g' '/tmp/exectunnel_agent.b64'"
            " | base64 -d > '/tmp/exectunnel_agent.py'",
            start=start,
        )
        metrics_inc("bootstrap.decode_started")
        await asyncio.sleep(BOOTSTRAP_DECODE_DELAY_SECS)
        metrics_inc("bootstrap.decode_done")

        metrics_inc("bootstrap.syntax_started")
        await self._send_command(
            "python3 -c '"
            'import ast,sys; ast.parse(open("/tmp/exectunnel_agent.py").read()); '
            'sys.stdout.write("SYNTAX_OK\\n"); sys.stdout.flush()\'',
            start=start,
        )
        await self._send_command(
            "exec python3 '/tmp/exectunnel_agent.py'",
            start=start,
        )

    # ── Wait for AGENT_READY ──────────────────────────────────────────────────

    async def _wait_for_ready(self, *, start: float) -> None:
        """Read WebSocket messages until ``AGENT_READY`` is seen.

        Lines received after ``AGENT_READY`` are stored in
        :attr:`post_ready_lines` for replay by ``FrameReceiver``.

        The raw (un-stripped) line is passed to ``is_ready_frame`` so that
        the check is consistent with how ``FrameReceiver`` processes lines —
        ``is_ready_frame`` internally calls ``parse_frame`` which handles
        leading/trailing whitespace correctly via the prefix/suffix sentinel
        check.

        Args:
            start: Bootstrap start time from ``loop.time()`` for elapsed_s.

        Raises:
            AgentSyntaxError:          Remote Python reported a ``SyntaxError``.
            AgentVersionMismatchError: Remote agent version incompatible.
            BootstrapError:            WebSocket closed before ``AGENT_READY``.
            FrameDecodingError:        Agent sent a corrupt tunnel frame during
                                       bootstrap — propagated to caller.
        """
        buf = ""
        ready = False

        async for msg in self._ws:
            chunk = msg if isinstance(msg, str) else msg.decode()
            buf += chunk

            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                stripped = line.strip()

                if not ready:
                    # Pass the raw line to is_ready_frame — consistent with
                    # how FrameReceiver processes lines.  is_ready_frame calls
                    # parse_frame internally which handles whitespace via the
                    # FRAME_PREFIX / FRAME_SUFFIX sentinel check.
                    #
                    # FrameDecodingError propagates to run() which wraps it in
                    # AgentReadyTimeoutError via the asyncio.timeout context.
                    # Per protocol.md: never catch FrameDecodingError silently.
                    try:
                        if is_ready_frame(line):
                            ready = True
                            continue
                    except FrameDecodingError as exc:
                        elapsed = asyncio.get_running_loop().time() - start
                        raise BootstrapError(
                            "Agent sent a corrupt tunnel frame during bootstrap.",
                            details={
                                "host": self._app.wss_url,
                                "elapsed_s": elapsed,
                            },
                            hint=(
                                "Check for base64 decode corruption or an "
                                "incompatible remote Python version."
                            ),
                        ) from exc

                    if stripped == "SYNTAX_OK":
                        logger.debug("bootstrap: remote syntax check passed")
                        metrics_inc("bootstrap.syntax_done")
                        metrics_inc("bootstrap.exec_started")
                        continue

                    if stripped:
                        self._diag.append(stripped)

                    # Detect SyntaxError on its own line or as the first line
                    # of a traceback header.
                    if stripped.startswith(
                        "SyntaxError:"
                    ) or stripped.startswith("Traceback (most recent call last):"):
                        # No underlying exception to chain from — the error
                        # information comes from the remote stderr text.
                        raise AgentSyntaxError(
                            f"Agent script error: {stripped}",
                            details={
                                "text": stripped,
                                "filename": "/tmp/exectunnel_agent.py",
                                "lineno": 0,
                            },
                            hint=(
                                "The uploaded agent.py failed to parse. "
                                "Check for base64 decode corruption or an "
                                "incompatible remote Python version."
                            ),
                        )

                    if stripped.startswith("VERSION_MISMATCH:"):
                        remote_version = stripped.split(":", 1)[-1].strip()
                        # No underlying exception to chain from — the version
                        # information comes from the remote stdout text.
                        raise AgentVersionMismatchError(
                            f"Remote agent version {remote_version!r} is "
                            "incompatible with this client.",
                            details={
                                "remote_version": remote_version,
                                "local_version": self._app.version,
                                "minimum_version": self._app.version,
                            },
                            hint=(
                                "Upgrade the exectunnel client or redeploy "
                                "the agent to match the client version."
                            ),
                        )
                else:
                    # Post-ready lines buffered for FrameReceiver replay.
                    # Store raw (un-stripped) lines — FrameReceiver._dispatch_frame
                    # handles normalisation consistently.
                    self._post_ready_lines.append(line)

            if ready:
                # Remaining bytes are a partial (unterminated) frame fragment.
                self._pre_ready_carry = buf
                return

        # WebSocket closed before AGENT_READY was received.
        elapsed = asyncio.get_running_loop().time() - start
        detail = f": {self._diag[-1]}" if self._diag else ""
        # No underlying exception to chain from — the WebSocket iterator
        # exhausted normally (clean close from the remote side).
        raise BootstrapError(
            f"WebSocket closed before AGENT_READY was received{detail}",
            details={
                "host": self._app.wss_url,
                "elapsed_s": elapsed,
                "last_output": self._diag[-1] if self._diag else None,
            },
            hint=(
                "Check that the remote shell executed the agent script successfully."
            ),
        )
