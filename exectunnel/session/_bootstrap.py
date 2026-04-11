"""Agent bootstrap — uploads and starts agent.py in the remote pod.

Responsibilities
----------------
* Send shell commands over the WebSocket exec channel.
* Deliver the agent payload via one of two delivery modes:

  ``upload``
      Encode the agent as URL-safe base64 and stream it to the pod in
      small ``printf`` chunks, then decode it with ``sed | base64 -d``.
      This is the default and works in any pod with no outbound network
      access.

  ``fetch``
      Fetch the agent directly inside the pod using ``curl`` (or ``wget``
      as a fallback) from a configurable raw fetch URL.  Requires the pod
      to have outbound HTTPS access.

* Optionally skip delivery if the agent is already present on the pod
  (``bootstrap_skip_if_present=True``).

* Optionally skip the syntax check (``bootstrap_syntax_check=False``).
  When both ``skip_if_present=True`` and ``syntax_check=True``, the
  bootstrapper checks for a sentinel file that records a previous
  successful syntax check.

* Wait for ``AGENT_READY`` with a configurable timeout.
* Detect and surface ``SyntaxError`` / version mismatch from the remote.
* Buffer frames received after ``AGENT_READY`` for replay by
  ``FrameReceiver``.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import math
import os
import uuid
from typing import TYPE_CHECKING, Final

from websockets.exceptions import ConnectionClosed

from exectunnel.config.defaults import Defaults
from exectunnel.exceptions import (
    AgentReadyTimeoutError,
    AgentSyntaxError,
    AgentVersionMismatchError,
    BootstrapError,
)
from exectunnel.observability import metrics_inc, metrics_observe, span
from exectunnel.protocol import is_ready_frame

from ._payload import load_agent_b64, load_go_agent_b64

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

    from exectunnel.config.settings import AppConfig, TunnelConfig

logger = logging.getLogger(__name__)

_VALID_DELIVERY_MODES: Final[frozenset[str]] = frozenset({"upload", "fetch"})

# Characters that are unsafe inside single-quoted POSIX shell strings or that
# could trigger shell expansion / history substitution in any context.
_SHELL_UNSAFE_CHARS: Final[frozenset[str]] = frozenset(
    {"'", "`", "$", "\\", "\n", "\r", "!"}
)

# Unique marker prefix for file-existence probes.
_MARKER_PREFIX: Final[str] = "EXECTUNNEL_EXISTS"
_MARKER_YES: Final[str] = f"{_MARKER_PREFIX}:1"
_MARKER_NO: Final[str] = f"{_MARKER_PREFIX}:0"

# Fence marker prefix for command synchronisation.
_FENCE_PREFIX: Final[str] = "EXECTUNNEL_FENCE"

# Progress logging interval for chunked uploads.
_UPLOAD_PROGRESS_LOG_INTERVAL: Final[int] = 50

# Timeout (seconds) for a single fenced command or file-existence probe.
_FENCE_TIMEOUT_SECS: Final[float] = 30.0

# Minimum timeout for any fenced command or probe.
_MIN_FENCE_TIMEOUT_SECS: Final[float] = 2.0

# Replacement character used when a WebSocket binary frame contains bytes
# that are not valid UTF-8.
_WS_DECODE_ERRORS: Final[str] = "replace"


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
        "_start",
        "_post_ready_lines",
        "_pre_ready_carry",
    )

    def __init__(
        self,
        ws: ClientConnection,
        app_cfg: AppConfig,
        tun_cfg: TunnelConfig,
    ) -> None:
        self._validate_shell_literals(tun_cfg)
        self._ws = ws
        self._app = app_cfg
        self._tun = tun_cfg
        self._diag: collections.deque[str] = collections.deque(
            maxlen=Defaults.BOOTSTRAP_DIAG_MAX_LINES,
        )
        self._start: float = 0.0
        self._post_ready_lines: list[str] = []
        self._pre_ready_carry: str = ""

    # ── Static validation ─────────────────────────────────────────────────────

    @staticmethod
    def _validate_shell_literals(tun_cfg: TunnelConfig) -> None:
        """Reject values that would break single-quoted shell interpolation."""
        values_to_check: tuple[tuple[str, str], ...] = (
            ("bootstrap_agent_path", tun_cfg.bootstrap_agent_path),
            ("bootstrap_go_agent_path", tun_cfg.bootstrap_go_agent_path),
            ("bootstrap_syntax_ok_sentinel", tun_cfg.bootstrap_syntax_ok_sentinel),
            ("fetch_agent_url", tun_cfg.fetch_agent_url),
        )
        for attr, value in values_to_check:
            bad = _SHELL_UNSAFE_CHARS.intersection(value)
            if bad:
                safe_repr = sorted(repr(c) for c in bad)
                raise BootstrapError(
                    f"Configuration {attr}={value!r} contains shell-unsafe "
                    f"character(s) {safe_repr} — this would break shell commands "
                    "constructed during bootstrap.",
                    details={
                        "attr": attr,
                        "value": value,
                        "unsafe_chars": sorted(bad),
                    },
                    hint=f"Remove unsafe characters {safe_repr} from the {attr} value.",
                )

    # ── Read-only properties consumed by TunnelSession ────────────────────────

    @property
    def post_ready_lines(self) -> list[str]:
        """Lines received after ``AGENT_READY``, buffered for replay."""
        return self._post_ready_lines

    @property
    def pre_ready_carry(self) -> str:
        """Partial (unterminated) frame fragment from the bootstrap read."""
        return self._pre_ready_carry

    # ── Elapsed helper ────────────────────────────────────────────────────────

    @property
    def _elapsed(self) -> float:
        """Seconds since bootstrap started."""
        return asyncio.get_running_loop().time() - self._start

    # ── Public entry point ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Deliver and start the agent.  Raises on any bootstrap failure.

        Raises:
            AgentReadyTimeoutError:    ``AGENT_READY`` not received in time.
            AgentSyntaxError:          Remote Python raised ``SyntaxError``.
            AgentVersionMismatchError: Remote agent version incompatible.
            BootstrapError:            Any other startup failure.
        """
        use_go = self._tun.bootstrap_use_go_agent
        delivery = self._tun.bootstrap_delivery

        if delivery not in _VALID_DELIVERY_MODES:
            raise BootstrapError(
                f"Unknown bootstrap_delivery {delivery!r}. "
                f"Valid values: {sorted(_VALID_DELIVERY_MODES)}",
                details={"bootstrap_delivery": delivery},
                hint="Set EXECTUNNEL_BOOTSTRAP_DELIVERY to one of: upload, fetch.",
            )

        self._start = asyncio.get_running_loop().time()
        metrics_inc("bootstrap.started")

        with span("session.bootstrap"):
            await self._send_command("stty raw -echo")
            await asyncio.sleep(Defaults.BOOTSTRAP_STTY_DELAY_SECS)
            metrics_inc("bootstrap.stty_done")

            if use_go:
                await self._run_go_bootstrap(delivery)
            else:
                await self._run_python_bootstrap(delivery)

            metrics_inc("bootstrap.exec_started")

            agent_path = (
                self._tun.bootstrap_go_agent_path
                if use_go
                else self._tun.bootstrap_agent_path
            )
            await self._await_agent_ready(agent_path)

            logger.info(
                "agent ready — SOCKS5 on %s:%d",
                self._tun.socks_host,
                self._tun.socks_port,
            )
            metrics_inc("bootstrap.exec_done")
            metrics_inc("bootstrap.ok")
            metrics_observe("bootstrap.duration_seconds", self._elapsed)

    # ── Bootstrap strategy helpers ────────────────────────────────────────────

    async def _run_python_bootstrap(self, delivery: str) -> None:
        """Deliver and exec the Python agent script."""
        agent_path = self._tun.bootstrap_agent_path
        sentinel_path = self._tun.bootstrap_syntax_ok_sentinel
        skip = self._tun.bootstrap_skip_if_present

        delivered = await self._ensure_agent_delivered(
            agent_path=agent_path,
            delivery=delivery,
            label="python",
        )

        # Decide whether to run the syntax check.
        run_syntax = self._tun.bootstrap_syntax_check
        if run_syntax and skip and not delivered:
            if await self._check_file_exists(sentinel_path):
                logger.info(
                    "bootstrap: syntax-OK sentinel present — skipping syntax check"
                )
                metrics_inc("bootstrap.syntax_cache_hit")
                run_syntax = False

        if run_syntax:
            await self._run_syntax_check(agent_path, sentinel_path)
        else:
            metrics_inc("bootstrap.syntax_skipped")

        await self._send_command(f"exec python3 '{agent_path}'")

    async def _run_go_bootstrap(self, delivery: str) -> None:
        """Deliver and exec the pre-built Go agent binary."""
        go_path = self._tun.bootstrap_go_agent_path

        await self._ensure_agent_delivered(
            agent_path=go_path,
            delivery=delivery,
            label="go",
        )

        await self._send_fenced_command(f"chmod +x '{go_path}'")
        await self._send_command(f"exec '{go_path}'")

    # ── Unified delivery orchestration ────────────────────────────────────────

    async def _ensure_agent_delivered(
        self,
        *,
        agent_path: str,
        delivery: str,
        label: str,
    ) -> bool:
        """Ensure the agent file is on the pod, returning whether delivery occurred.

        Returns:
            ``True`` if the agent was freshly delivered, ``False`` if delivery
            was skipped because the file already exists and skip_if_present
            is enabled.
        """
        skip = self._tun.bootstrap_skip_if_present
        agent_present = await self._check_file_exists(agent_path)

        if agent_present and skip:
            logger.info(
                "bootstrap(%s): agent already present at %s — skipping delivery",
                label,
                agent_path,
            )
            metrics_inc(f"bootstrap.{label}.skip_delivery")
            return False

        if agent_present:
            logger.info(
                "bootstrap(%s): removing existing agent at %s before re-delivery",
                label,
                agent_path,
            )
            await self._send_fenced_command(f"rm -f '{agent_path}'")

        if delivery == "upload":
            b64_data = load_go_agent_b64() if label == "go" else load_agent_b64()
            await self._deliver_via_upload(agent_path, b64_data, label)
        else:
            await self._deliver_via_fetch(
                agent_path, self._tun.fetch_agent_url, label
            )

        return True

    # ── Shell command helpers ─────────────────────────────────────────────────

    async def _send_command(self, cmd: str) -> None:
        """Send one shell command over the WebSocket exec channel.

        Raises:
            BootstrapError: If the WebSocket closes before the command is sent.
        """
        try:
            await self._ws.send(cmd + "\n")
        except ConnectionClosed as exc:
            raise BootstrapError(
                "WebSocket closed while sending bootstrap command.",
                details={
                    "command": cmd[:80],
                    "host": self._app.wss_url,
                    "elapsed_s": self._elapsed,
                },
                hint="Check that the remote shell is still alive.",
            ) from exc

    async def _send_fenced_command(
        self,
        cmd: str,
        timeout: float = _FENCE_TIMEOUT_SECS,
    ) -> None:
        """Send a command and wait for an echo-fence marker before returning.

        Args:
            cmd:     Shell command to execute.
            timeout: Maximum seconds to wait for the fence marker.

        Raises:
            BootstrapError: If the WebSocket closes or the timeout expires
                            before the fence marker arrives.
        """
        fence_id = uuid.uuid4().hex[:12]
        fence_marker = f"{_FENCE_PREFIX}:{fence_id}"

        await self._send_command(cmd)
        await self._send_command(f"printf '{fence_marker}\\n'")

        buf = ""
        try:
            async with asyncio.timeout(timeout):
                async for msg in self._ws:
                    chunk = self._decode_ws_message(msg)
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        stripped = line.strip()
                        if stripped == fence_marker:
                            if buf.strip():
                                logger.debug(
                                    "bootstrap: %d chars of residual data "
                                    "after fence marker discarded: %r",
                                    len(buf),
                                    buf[:80],
                                )
                            return
                        if stripped:
                            logger.debug("bootstrap fence output: %s", stripped)
                            self._diag.append(stripped)
        except TimeoutError:
            raise BootstrapError(
                f"Timed out after {timeout}s waiting for command fence "
                f"({cmd[:40]!r}).",
                details={
                    "command": cmd[:80],
                    "host": self._app.wss_url,
                    "elapsed_s": self._elapsed,
                    "fence_timeout_s": timeout,
                },
                hint=(
                    "The remote shell did not echo the fence marker in time. "
                    "Check pod responsiveness or increase the fence timeout."
                ),
            )

        raise BootstrapError(
            f"WebSocket closed while waiting for command fence ({cmd[:40]!r}).",
            details={
                "command": cmd[:80],
                "host": self._app.wss_url,
                "elapsed_s": self._elapsed,
            },
            hint="Check that the remote shell is still alive.",
        )

    # ── Pod file-existence probe ──────────────────────────────────────────────

    async def _check_file_exists(
        self,
        path: str,
        timeout: float = _FENCE_TIMEOUT_SECS,
    ) -> bool:
        """Return ``True`` if *path* exists on the pod.

        Args:
            path:    Absolute path to probe on the remote pod.
            timeout: Maximum seconds to wait for the probe response.

        Raises:
            BootstrapError: If the WebSocket closes or the timeout expires
                            before the marker arrives.
        """
        await self._send_command(
            f"[ -f '{path}' ] "
            f"&& printf '{_MARKER_YES}\\n' "
            f"|| printf '{_MARKER_NO}\\n'",
        )

        buf = ""
        try:
            async with asyncio.timeout(timeout):
                async for msg in self._ws:
                    chunk = self._decode_ws_message(msg)
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        stripped = line.strip()
                        if stripped == _MARKER_YES:
                            return True
                        if stripped == _MARKER_NO:
                            return False
                        if stripped:
                            logger.debug("bootstrap probe output: %s", stripped)
                            self._diag.append(stripped)
        except TimeoutError:
            raise BootstrapError(
                f"Timed out after {timeout}s waiting for file-existence "
                f"probe response for {path!r}.",
                details={
                    "path": path,
                    "host": self._app.wss_url,
                    "elapsed_s": self._elapsed,
                    "probe_timeout_s": timeout,
                },
                hint=(
                    "The remote shell did not respond to the probe in time. "
                    "Check pod responsiveness."
                ),
            )

        raise BootstrapError(
            f"WebSocket closed while probing for {path!r}.",
            details={
                "path": path,
                "host": self._app.wss_url,
                "elapsed_s": self._elapsed,
            },
            hint="Check that the remote shell is still alive.",
        )

    # ── Delivery: base64 upload ───────────────────────────────────────────────

    async def _deliver_via_upload(
        self,
        agent_path: str,
        b64_data: str,
        label: str,
    ) -> None:
        """Upload an agent as base64 chunks and decode it on the pod.

        The upload sequence is:
        1. Remove any stale staging file (fenced).
        2. Stream all base64 chunks via ``printf … >> staging``.
        3. Fence after the final chunk to guarantee all writes are complete.
        4. Decode staging → agent_path via ``sed | base64 -d`` (fenced).
        5. Remove the staging file (fenced).
        """
        assert len(b64_data) % 4 == 0, (  # noqa: S101
            f"base64 payload length {len(b64_data)} is not a multiple of 4 — "
            "this indicates a bug in _encode_urlsafe_b64"
        )

        b64_path = self._b64_staging_path(agent_path)
        chunk_size = Defaults.BOOTSTRAP_CHUNK_SIZE_CHARS
        total_chunks = math.ceil(len(b64_data) / chunk_size)

        metrics_observe(f"bootstrap.{label}.chunks", float(total_chunks))
        logger.info(
            "bootstrap(%s): uploading as %d base64 chunks (%d bytes encoded)",
            label,
            total_chunks,
            len(b64_data),
        )

        # 1. Clear stale staging file.
        await self._send_fenced_command(f"rm -f '{b64_path}'")

        # 2. Stream chunks — fire-and-forget per chunk for throughput;
        #    we fence once after the last chunk (step 3).
        metrics_inc(f"bootstrap.{label}.upload_started")
        for idx, offset in enumerate(range(0, len(b64_data), chunk_size)):
            chunk = b64_data[offset : offset + chunk_size]
            await self._send_command(f"printf '%s' '{chunk}' >> '{b64_path}'")
            if (idx + 1) % _UPLOAD_PROGRESS_LOG_INTERVAL == 0:
                logger.debug(
                    "bootstrap(%s): upload progress %d/%d chunks",
                    label,
                    idx + 1,
                    total_chunks,
                )

        # 3. Fence after the last chunk.
        await self._send_fenced_command(f"sync '{b64_path}' 2>/dev/null || true")
        metrics_inc(f"bootstrap.{label}.upload_done")

        # 4. Decode: convert URL-safe base64 back to standard alphabet.
        await self._send_fenced_command(
            f"sed 's/-/+/g; s/_/\\//g' '{b64_path}' "
            f"| base64 -d > '{agent_path}'"
        )
        metrics_inc(f"bootstrap.{label}.decode_done")

        # 5. Clean up staging file.
        await self._send_fenced_command(f"rm -f '{b64_path}'")

    # ── Delivery: Fetch ───────────────────────────────────────────────────────

    async def _deliver_via_fetch(
        self,
        agent_path: str,
        url: str,
        label: str,
    ) -> None:
        """Fetch the agent inside the pod from a URL using curl/wget."""
        logger.info("bootstrap(%s/fetch): fetching agent from %s", label, url)

        fetch_cmd = (
            f"curl -fsSL '{url}' -o '{agent_path}' 2>/dev/null"
            f" || wget -qO '{agent_path}' '{url}'"
        )
        await self._send_command(fetch_cmd)
        metrics_inc(f"bootstrap.{label}.fetch_started")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + Defaults.BOOTSTRAP_FETCH_FETCH_DELAY_SECS
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    "bootstrap(%s/fetch): agent file %r not found after %.0f s — "
                    "proceeding anyway (download may still be in progress)",
                    label,
                    agent_path,
                    Defaults.BOOTSTRAP_FETCH_FETCH_DELAY_SECS,
                )
                break

            probe_timeout = max(_MIN_FENCE_TIMEOUT_SECS, remaining)
            try:
                if await self._check_file_exists(agent_path, timeout=probe_timeout):
                    break
            except BootstrapError:
                logger.debug(
                    "bootstrap(%s/fetch): file probe timed out after %.1fs "
                    "(remaining=%.1fs) — retrying",
                    label,
                    probe_timeout,
                    deadline - loop.time(),
                )
                continue

            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    "bootstrap(%s/fetch): agent file %r not found after %.0f s — "
                    "proceeding anyway (download may still be in progress)",
                    label,
                    agent_path,
                    Defaults.BOOTSTRAP_FETCH_FETCH_DELAY_SECS,
                )
                break
            await asyncio.sleep(
                min(Defaults.BOOTSTRAP_FETCH_FETCH_POLL_SECS, remaining),
            )

        metrics_inc(f"bootstrap.{label}.fetch_done")

    # ── Syntax check ──────────────────────────────────────────────────────────

    async def _run_syntax_check(
        self,
        agent_path: str,
        sentinel_path: str,
    ) -> None:
        """Run a remote ``ast.parse`` syntax check and write the sentinel on success."""
        metrics_inc("bootstrap.syntax_started")
        check_cmd = (
            f'python3 -c "import ast, sys; '
            f"ast.parse(open('{agent_path}').read()); "
            f"sys.stdout.write('SYNTAX_OK\\\\n'); "
            f"sys.stdout.flush(); "
            f"open('{sentinel_path}', 'w').close()\""
        )
        await self._send_fenced_command(check_cmd)

    # ── Wait for AGENT_READY ──────────────────────────────────────────────────

    async def _await_agent_ready(self, agent_path: str) -> None:
        """Wait for ``AGENT_READY`` with timeout, then buffer post-ready lines."""
        logger.info("waiting for AGENT_READY…")
        try:
            async with asyncio.timeout(self._tun.ready_timeout):
                await self._wait_for_ready(agent_path)
        except TimeoutError as exc:
            detail = f"; last output: {self._diag[-1]}" if self._diag else ""
            metrics_inc("bootstrap.timeout")
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
                    "environment (Python version, available memory, etc.)."
                ),
            ) from exc

    async def _wait_for_ready(self, agent_path: str) -> None:
        """Read WebSocket messages until ``AGENT_READY`` is seen.

        Raises:
            AgentSyntaxError:          Remote Python reported a ``SyntaxError``.
            AgentVersionMismatchError: Remote agent version incompatible.
            BootstrapError:            WebSocket closed before ``AGENT_READY``.
        """
        buf = ""
        ready = False

        async for msg in self._ws:
            chunk = self._decode_ws_message(msg)
            buf += chunk

            while "\n" in buf:
                line, buf = buf.split("\n", 1)

                if not ready:
                    if is_ready_frame(line):
                        ready = True
                        continue
                    self._handle_pre_ready_line(line.strip(), agent_path)
                else:
                    self._post_ready_lines.append(line)

            if ready:
                self._pre_ready_carry = buf
                return

        detail = f": {self._diag[-1]}" if self._diag else ""
        raise BootstrapError(
            f"WebSocket closed before AGENT_READY was received{detail}",
            details={
                "host": self._app.wss_url,
                "elapsed_s": self._elapsed,
                "last_output": self._diag[-1] if self._diag else None,
            },
            hint="Check that the remote shell executed the agent script successfully.",
        )

    def _handle_pre_ready_line(self, stripped: str, agent_path: str) -> None:
        """Process a single line received before ``AGENT_READY``.

        Raises:
            AgentSyntaxError:          Remote Python reported ``SyntaxError``.
            AgentVersionMismatchError: Remote agent version incompatible.
        """
        if stripped == "SYNTAX_OK":
            logger.debug("bootstrap: remote syntax check passed")
            metrics_inc("bootstrap.syntax_done")
            return

        if stripped:
            self._diag.append(stripped)

        if "SyntaxError:" in stripped:
            raise AgentSyntaxError(
                f"Agent script error: {stripped}",
                details={
                    "text": stripped,
                    "filename": agent_path,
                    "lineno": 0,
                    "diag": list(self._diag),
                },
                hint=(
                    "The uploaded agent.py failed to parse. "
                    "Check for base64 decode corruption or an "
                    "incompatible remote Python version."
                ),
            )

        if stripped.startswith("Traceback (most recent call last):"):
            logger.debug("bootstrap: traceback header seen, buffering")
            return

        if stripped.startswith("VERSION_MISMATCH:"):
            remote_version = stripped.split(":", 1)[-1].strip()
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

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _decode_ws_message(msg: str | bytes) -> str:
        """Decode a WebSocket message to ``str``."""
        if isinstance(msg, str):
            return msg
        return msg.decode("utf-8", errors=_WS_DECODE_ERRORS)

    @staticmethod
    def _b64_staging_path(agent_path: str) -> str:
        """Derive a staging path for the intermediate base64 file."""
        root, _ext = os.path.splitext(agent_path)
        return root + ".b64"

    # ── Debug ─────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        delivery = self._tun.bootstrap_delivery
        agent_type = "go" if self._tun.bootstrap_use_go_agent else "python"
        elapsed = f"{self._elapsed:.1f}s" if self._start else "not started"
        return (
            f"<AgentBootstrapper {agent_type}/{delivery} "
            f"elapsed={elapsed} "
            f"diag_lines={len(self._diag)} "
            f"post_ready={len(self._post_ready_lines)}>"
        )
