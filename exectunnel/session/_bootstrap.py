"""Agent bootstrap — uploads and starts agent.py in the remote pod.

Responsibilities
----------------
* Send shell commands over the WebSocket exec channel.
* Deliver the agent payload via one of two delivery modes:

  ``upload``
      Encode ``payload/agent.py`` as URL-safe base64 and stream it to the
      pod in small ``printf`` chunks, then decode it with ``sed | base64 -d``.
      This is the default and works in any pod with no outbound network
      access.

  ``github``
      Fetch the agent directly inside the pod using ``curl`` (or ``wget``
      as a fallback) from a configurable raw GitHub URL.  Requires the pod
      to have outbound HTTPS access to ``raw.githubusercontent.com``.

* Optionally skip delivery if the agent is already present on the pod
  (``bootstrap_skip_if_present=True``).  When skip is enabled and the agent
  file exists, delivery is bypassed entirely.  When skip is disabled and the
  agent file exists, it is removed before re-delivering.

* Optionally skip the syntax check (``bootstrap_syntax_check=False``).
  When both ``skip_if_present=True`` and ``syntax_check=True``, the
  bootstrapper checks for a sentinel file on the pod
  (``bootstrap_syntax_ok_sentinel``) that records a previous successful
  syntax check — if the sentinel is present the syntax check is skipped.

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
    BOOTSTRAP_GITHUB_FETCH_DELAY_SECS,
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


logger = logging.getLogger(__name__)

# Valid delivery mode identifiers.
_VALID_DELIVERY_MODES = frozenset({"upload", "github"})


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
        """Deliver and start the agent.  Raises on any bootstrap failure.

        Delivery is controlled by two orthogonal settings:

        ``bootstrap_delivery`` (``"upload"`` | ``"github"``)
            How to get the agent onto the pod when delivery is needed.

        ``bootstrap_skip_if_present`` (bool, default ``False``)
            If ``True`` and the agent file already exists on the pod,
            skip delivery entirely and go straight to exec.
            If ``False`` and the agent file exists, remove it first and
            re-deliver.

        ``bootstrap_syntax_check`` (bool, default ``True``)
            Whether to run a remote syntax check before exec'ing the agent.
            When ``skip_if_present=True`` and the syntax-OK sentinel file
            is present on the pod, the syntax check is also skipped
            (cached result from a previous successful run).

        Raises:
            AgentReadyTimeoutError:    ``AGENT_READY`` not received in time.
            AgentSyntaxError:          Remote Python raised ``SyntaxError``.
            AgentVersionMismatchError: Remote agent version incompatible.
            BootstrapError:            Any other startup failure.
        """
        delivery = self._tun.bootstrap_delivery
        if delivery not in _VALID_DELIVERY_MODES:
            raise BootstrapError(
                f"Unknown bootstrap_delivery {delivery!r}. "
                f"Valid values: {sorted(_VALID_DELIVERY_MODES)}",
                details={"bootstrap_delivery": delivery},
                hint=(
                    "Set EXECTUNNEL_BOOTSTRAP_DELIVERY to one of: "
                    "upload, github."
                ),
            )

        loop = asyncio.get_running_loop()
        start = loop.time()
        metrics_inc("tunnel.bootstrap.started")

        with span("session.bootstrap"):
            await self._send_command("stty raw -echo", start=start)
            await asyncio.sleep(BOOTSTRAP_STTY_DELAY_SECS)
            metrics_inc("bootstrap.stty_done")

            agent_path: str = self._tun.bootstrap_agent_path
            sentinel_path: str = self._tun.bootstrap_syntax_ok_sentinel
            skip_if_present: bool = self._tun.bootstrap_skip_if_present

            # Determine whether the agent file is already present on the pod.
            # We use a shell one-liner that writes "1" if the file exists and
            # "0" otherwise, then read the response line from the WebSocket.
            agent_present = await self._check_file_exists(agent_path, start=start)

            if agent_present and skip_if_present:
                # Agent is present and user wants to reuse it — skip delivery.
                logger.info(
                    "bootstrap: agent already present at %s — skipping delivery",
                    agent_path,
                )
                metrics_inc("bootstrap.skip_delivery")
            else:
                # Either the agent is absent, or the user wants a fresh copy.
                if agent_present:
                    # Remove stale copy before re-delivering.
                    logger.info(
                        "bootstrap: removing existing agent at %s before re-delivery",
                        agent_path,
                    )
                    await self._send_command(
                        f"rm -f '{agent_path}'", start=start
                    )
                    await asyncio.sleep(BOOTSTRAP_RM_DELAY_SECS)

                if delivery == "upload":
                    await self._deliver_via_upload(start=start)
                else:
                    await self._deliver_via_github(start=start)

            # Decide whether to run the syntax check.
            run_syntax = self._tun.bootstrap_syntax_check
            if run_syntax and skip_if_present and agent_present:
                # Check whether the syntax-OK sentinel is present — if so,
                # we can trust the cached result and skip the check.
                sentinel_present = await self._check_file_exists(
                    sentinel_path, start=start
                )
                if sentinel_present:
                    logger.info(
                        "bootstrap: syntax-OK sentinel present — skipping syntax check"
                    )
                    metrics_inc("bootstrap.syntax_cache_hit")
                    run_syntax = False

            if run_syntax:
                await self._run_syntax_check(
                    agent_path=agent_path,
                    sentinel_path=sentinel_path,
                    start=start,
                )
            else:
                metrics_inc("bootstrap.syntax_skipped")

            # Exec the agent.
            await self._send_command(
                f"exec python3 '{agent_path}'", start=start
            )
            metrics_inc("bootstrap.exec_started")

            logger.info("waiting for agent AGENT_READY…")
            try:
                async with asyncio.timeout(self._tun.ready_timeout):
                    await self._wait_for_ready(agent_path=agent_path, start=start)
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

    # ── Shell command helper ──────────────────────────────────────────────────

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

    # ── Pod file-existence probe ──────────────────────────────────────────────

    async def _check_file_exists(self, path: str, *, start: float) -> bool:
        """Return ``True`` if *path* exists on the pod.

        Sends a shell one-liner that prints ``EXECTUNNEL_EXISTS:1`` when the
        file is present and ``EXECTUNNEL_EXISTS:0`` when it is absent, then
        reads WebSocket messages until that marker line is seen.

        The marker prefix is unique enough to avoid collision with normal
        shell output during bootstrap.

        Args:
            path:  Absolute path to check on the pod.
            start: Bootstrap start time for elapsed_s in error details.

        Returns:
            ``True`` if the file exists, ``False`` otherwise.

        Raises:
            BootstrapError: If the WebSocket closes before the marker arrives.
        """
        marker_yes = "EXECTUNNEL_EXISTS:1"
        marker_no = "EXECTUNNEL_EXISTS:0"
        await self._send_command(
            f"[ -f '{path}' ] "
            f"&& printf 'EXECTUNNEL_EXISTS:1\\n' "
            f"|| printf 'EXECTUNNEL_EXISTS:0\\n'",
            start=start,
        )

        buf = ""
        async for msg in self._ws:
            chunk = msg if isinstance(msg, str) else msg.decode()
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                stripped = line.strip()
                if stripped == marker_yes:
                    return True
                if stripped == marker_no:
                    return False
                # Any other output (shell prompt, echo artefacts) is logged
                # at debug level and added to the diagnostic buffer.
                if stripped:
                    logger.debug("bootstrap probe output: %s", stripped)
                    self._diag.append(stripped)

        elapsed = asyncio.get_running_loop().time() - start
        raise BootstrapError(
            f"WebSocket closed while probing for {path!r}.",
            details={"path": path, "host": self._app.wss_url, "elapsed_s": elapsed},
            hint="Check that the remote shell is still alive.",
        )

    # ── Delivery modes ────────────────────────────────────────────────────────

    async def _deliver_via_upload(self, *, start: float) -> None:
        """Upload the agent payload as base64 chunks and decode it on the pod.

        Encodes ``payload/agent.py`` as URL-safe base64 and streams it to
        the pod in ``BOOTSTRAP_CHUNK_SIZE_CHARS``-character ``printf``
        chunks, then decodes with ``sed | base64 -d``.  Works in any pod
        with no outbound network access.

        Note: stty and any pre-existing file removal are handled by the
        caller (``run()``).
        """
        agent_path = self._tun.bootstrap_agent_path
        b64_path = agent_path.replace(".py", ".b64")

        agent_b64 = load_agent_b64()
        chunk_count = len(range(0, len(agent_b64), BOOTSTRAP_CHUNK_SIZE_CHARS))
        metrics_observe("tunnel.bootstrap.chunks", float(chunk_count))

        await self._send_command(
            f"rm -f '{b64_path}'", start=start
        )
        await asyncio.sleep(BOOTSTRAP_RM_DELAY_SECS)

        metrics_inc("bootstrap.upload_started")
        for i in range(0, len(agent_b64), BOOTSTRAP_CHUNK_SIZE_CHARS):
            chunk = agent_b64[i : i + BOOTSTRAP_CHUNK_SIZE_CHARS]
            await self._send_command(
                f"printf '%s' '{chunk}' >> '{b64_path}'",
                start=start,
            )

        metrics_inc("bootstrap.upload_done")
        await self._send_command(
            f"sed 's/-/+/g; s/_/\\//g' '{b64_path}'"
            f" | base64 -d > '{agent_path}'",
            start=start,
        )
        metrics_inc("bootstrap.decode_started")
        await asyncio.sleep(BOOTSTRAP_DECODE_DELAY_SECS)
        metrics_inc("bootstrap.decode_done")

    async def _deliver_via_github(self, *, start: float) -> None:
        """Fetch the agent inside the pod from a raw GitHub URL.

        Tries ``curl`` first; falls back to ``wget`` if ``curl`` is not
        available.  The pod must have outbound HTTPS access to the URL
        (default: ``raw.githubusercontent.com``).

        The URL is taken from ``tun_cfg.github_agent_url`` and can be
        overridden via the ``EXECTUNNEL_GITHUB_AGENT_URL`` environment
        variable to pin a specific commit or use a fork.

        Note: stty and any pre-existing file removal are handled by the
        caller (``run()``).
        """
        url = self._tun.github_agent_url
        agent_path = self._tun.bootstrap_agent_path
        logger.info("bootstrap(github): fetching agent from %s", url)

        fetch_cmd = (
            f"curl -fsSL '{url}' -o '{agent_path}' 2>/dev/null"
            f" || wget -qO '{agent_path}' '{url}'"
        )
        await self._send_command(fetch_cmd, start=start)
        metrics_inc("bootstrap.github_fetch_started")

        # Give the download time to complete before the syntax check reads
        # the file.  BOOTSTRAP_GITHUB_FETCH_DELAY_SECS (default 10 s) is
        # much longer than the decode-delay because a network fetch can take
        # several seconds on slow cluster egress paths.
        await asyncio.sleep(BOOTSTRAP_GITHUB_FETCH_DELAY_SECS)
        metrics_inc("bootstrap.github_fetch_done")

    # ── Syntax check ─────────────────────────────────────────────────────────

    async def _run_syntax_check(
        self, *, agent_path: str, sentinel_path: str, start: float
    ) -> None:
        """Run the remote syntax check and write the syntax-OK sentinel.

        On success the remote shell writes ``SYNTAX_OK`` to stdout and
        touches the sentinel file so future runs with
        ``skip_if_present=True`` can skip the check.

        Args:
            agent_path:    Absolute path of the agent script on the pod.
            sentinel_path: Absolute path of the syntax-OK sentinel file.
            start:         Bootstrap start time for elapsed_s in errors.
        """
        metrics_inc("bootstrap.syntax_started")
        # The one-liner: parse the file, print SYNTAX_OK, touch the sentinel.
        await self._send_command(
            "python3 -c '"
            f'import ast,sys; ast.parse(open("{agent_path}").read()); '
            f'sys.stdout.write("SYNTAX_OK\\n"); sys.stdout.flush(); '
            f'open("{sentinel_path}", "w").close()\'',
            start=start,
        )

    # ── Wait for AGENT_READY ──────────────────────────────────────────────────

    async def _wait_for_ready(self, *, agent_path: str, start: float) -> None:
        """Read WebSocket messages until ``AGENT_READY`` is seen.

        Lines received after ``AGENT_READY`` are stored in
        :attr:`post_ready_lines` for replay by ``FrameReceiver``.

        The raw (un-stripped) line is passed to ``is_ready_frame`` so that
        the check is consistent with how ``FrameReceiver`` processes lines —
        ``is_ready_frame`` internally calls ``parse_frame`` which handles
        leading/trailing whitespace correctly via the prefix/suffix sentinel
        check.

        Args:
            agent_path: Absolute path of the agent script on the pod (used in
                        ``AgentSyntaxError`` details).
            start:      Bootstrap start time from ``loop.time()`` for elapsed_s.

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
                                "filename": agent_path,
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
