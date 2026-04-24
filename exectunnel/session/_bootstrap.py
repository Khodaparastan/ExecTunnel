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
  :class:`~exectunnel.session._receiver.FrameReceiver`.

Terminal setup
--------------
The bootstrapper sends the following as the very first fenced command::

    stty cs8 -icanon min 1 time 0 -isig -xcase -inpck -opost -echo; printf '\\e[?2004l'

This does two things:

* ``stty cs8 -icanon min 1 time 0 -isig -xcase -inpck -opost -echo`` —
  puts the terminal into raw mode (equivalent to ``stty raw -echo``) by
  setting each flag explicitly.  The individual flags are:

  * ``cs8`` — 8-bit character size; passes all byte values through
    without stripping the high bit.
  * ``-icanon min 1 time 0`` — disables canonical (line-buffered) input
    so characters are delivered to the reader one at a time as they
    arrive, without waiting for a newline.  ``min 1`` means
    :func:`read` blocks until at least one byte is available;
    ``time 0`` disables the read timeout.
  * ``-isig`` — disables signal generation from control characters so
    ``Ctrl+C``, ``Ctrl+Z``, and ``Ctrl+\\`` are passed through as raw
    bytes rather than raising ``SIGINT``, ``SIGTSTP``, or ``SIGQUIT``.
  * ``-xcase`` — disables canonical upper/lower-case mapping so mixed-
    case output is not mangled.
  * ``-inpck`` — disables input parity checking so no bytes are silently
    dropped or marked due to parity errors.
  * ``-opost`` — disables all output post-processing (including
    ``\\n`` → ``\\r\\n`` translation) so output bytes are written
    exactly as produced.
  * ``-echo`` — disables terminal echo so that commands sent over the
    WebSocket exec channel are not reflected back as output, which would
    corrupt the bootstrap marker matching logic.

* ``printf '\\e[?2004l'`` — disables bracketed-paste mode so the shell
  stops wrapping rapidly-sent input in VT100 escape sequences that
  prevent marker matching.

All received lines are passed through :func:`_strip_ansi` before any
marker comparison so that residual VT100 decoration cannot cause false
negatives.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import math
import os
import re
import shlex
import uuid
from typing import TYPE_CHECKING, Final

from websockets.exceptions import ConnectionClosed

from exectunnel.defaults import Defaults
from exectunnel.exceptions import (
    AgentReadyTimeoutError,
    AgentSyntaxError,
    AgentVersionMismatchError,
    BootstrapError,
)
from exectunnel.observability import metrics_inc, metrics_observe, span
from exectunnel.protocol import is_ready_frame

from ._constants import (
    FENCE_PREFIX,
    FENCE_TIMEOUT_SECS,
    MARKER_NO,
    MARKER_YES,
    MAX_STASH_LINES,
    MIN_FENCE_TIMEOUT_SECS,
    PYTHON_CANDIDATES,
    UPLOAD_PROGRESS_LOG_INTERVAL,
    VALID_DELIVERY_MODES,
    WS_DECODE_ERRORS,
)
from ._payload import load_agent_b64, load_go_agent_b64

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

    from ._config import SessionConfig, TunnelConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Raw-mode stty command sent as the very first fenced command.
#
# Equivalent to ``stty raw -echo`` but spelled out flag-by-flag so it works
# on BusyBox stty (which does not accept the ``raw`` meta-flag):
#
#   cs8        — 8-bit characters; no high-bit stripping
#   -icanon    — disable line-buffering (canonical mode off)
#   min 1      — read(2) blocks until ≥ 1 byte available
#   time 0     — no read timeout
#   -isig      — Ctrl+C / Ctrl+Z / Ctrl+\ passed as raw bytes
#   -xcase     — no upper/lower-case mapping
#   -inpck     — no input parity checking
#   -opost     — no output post-processing (no \n→\r\n)
#   -echo      — suppress terminal echo of sent commands
# ---------------------------------------------------------------------------
_STTY_RAW_CMD: Final[str] = (
    "stty cs8 -icanon min 1 time 0 -isig -xcase -inpck -opost -echo"
)

# Matches all ANSI/VT100 CSI sequences (ESC [ … ) and bare ESC + one char.
_ANSI_ESCAPE_RE: Final[re.Pattern[str]] = re.compile(
    r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])"
)


def _strip_ansi(text: str) -> str:
    """Remove ANSI/VT100 escape sequences and bare carriage returns.

    Applied to every received line before marker comparison so that terminal
    decoration — bracketed-paste markers, readline cursor-control sequences,
    colour codes — cannot prevent exact-string matching.

    Args:
        text: Raw line from the WebSocket receive buffer.

    Returns:
        The line with all ANSI escape sequences and ``\\r`` characters removed.
    """
    return _ANSI_ESCAPE_RE.sub("", text).replace("\r", "")


class AgentBootstrapper:
    """Uploads and starts the agent script on the remote pod.

    After a successful :meth:`run`, callers must read :attr:`post_ready_lines`
    and :attr:`pre_ready_carry` to replay any frames that arrived between
    ``AGENT_READY`` and :class:`~exectunnel.session._receiver.FrameReceiver`
    taking over the WebSocket iterator.

    Args:
        ws:          Live WebSocket connection to the pod exec channel.
        session_cfg: Session-level configuration.
        tun_cfg:     Tunnel-level configuration.

    Raises:
        BootstrapError: If *tun_cfg* contains invalid shell-bound values.
    """

    __slots__ = (
        "_ws",
        "_cfg",
        "_tun",
        "_diag",
        "_start",
        "_stash_lines",
        "_stash_carry",
        "_post_ready_lines",
        "_pre_ready_carry",
    )

    def __init__(
        self,
        ws: ClientConnection,
        session_cfg: SessionConfig,
        tun_cfg: TunnelConfig,
    ) -> None:
        self._validate_shell_literals(tun_cfg)
        self._ws = ws
        self._cfg = session_cfg
        self._tun = tun_cfg
        self._diag: collections.deque[str] = collections.deque(
            maxlen=Defaults.BOOTSTRAP_DIAG_MAX_LINES,
        )
        self._start: float = 0.0
        self._stash_lines: collections.deque[str] = collections.deque(
            maxlen=MAX_STASH_LINES,
        )
        self._stash_carry: str = ""
        self._post_ready_lines: list[str] = []
        self._pre_ready_carry: str = ""

    # ── Static validation ─────────────────────────────────────────────────────

    @staticmethod
    def _validate_shell_literals(tun_cfg: TunnelConfig) -> None:
        """Validate semantic constraints for shell-bound configuration values.

        Checks that each path/URL value used verbatim in shell commands is
        non-empty (after stripping whitespace) and contains no NUL bytes,
        both of which would silently corrupt the generated shell commands.

        Args:
            tun_cfg: Tunnel configuration to validate.

        Raises:
            BootstrapError: If any shell-bound value is empty or contains a
                            NUL byte.
        """
        values_to_check: tuple[tuple[str, str], ...] = (
            ("bootstrap_agent_path", tun_cfg.bootstrap_agent_path),
            ("bootstrap_go_agent_path", tun_cfg.bootstrap_go_agent_path),
            ("bootstrap_syntax_ok_sentinel", tun_cfg.bootstrap_syntax_ok_sentinel),
            ("bootstrap_fetch_url", tun_cfg.bootstrap_fetch_url),
        )
        for attr, value in values_to_check:
            if not value.strip():
                raise BootstrapError(
                    f"Configuration {attr} must not be empty.",
                    details={"attr": attr, "value": value},
                    hint=f"Set a non-empty value for {attr}.",
                )
            if "\x00" in value:
                raise BootstrapError(
                    f"Configuration {attr} contains a NUL byte, which is "
                    "invalid for shell command construction.",
                    details={"attr": attr, "value": value},
                    hint=f"Remove NUL bytes from {attr}.",
                )

    @staticmethod
    def _shq(value: str) -> str:
        """Return a POSIX-shell-escaped single token.

        Args:
            value: The string to escape.

        Returns:
            A safely quoted shell token suitable for inclusion in a command
            string.
        """
        return shlex.quote(value)

    # ── Read-only properties consumed by TunnelSession ────────────────────────

    @property
    def post_ready_lines(self) -> list[str]:
        """Lines received after ``AGENT_READY``, buffered for replay.

        These lines are consumed by
        :class:`~exectunnel.session._receiver.FrameReceiver` immediately after
        bootstrap completes so that no protocol frames are lost during the
        hand-off.

        Returns:
            Ordered list of raw (un-stripped) lines received after the
            ``AGENT_READY`` marker and before ``FrameReceiver`` takes over.
        """
        return self._post_ready_lines

    @property
    def pre_ready_carry(self) -> str:
        """Partial (unterminated) frame fragment from the bootstrap read loop.

        The bootstrap read loop splits on ``\\n``; any bytes that arrived after
        the last newline but before ``AGENT_READY`` are stored here so that
        ``FrameReceiver`` can prepend them to its own receive buffer.

        Returns:
            A (possibly empty) string containing the incomplete trailing chunk.
        """
        return self._pre_ready_carry

    # ── Elapsed helper ────────────────────────────────────────────────────────

    @property
    def _elapsed(self) -> float:
        """Seconds elapsed since :meth:`run` was called.

        Returns:
            Elapsed wall-clock seconds as a float, or ``0.0`` if called
            outside a running event loop or before :meth:`run` has started.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return 0.0
        if self._start == 0.0:
            return 0.0
        return loop.time() - self._start

    # ── Public entry point ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Deliver and start the agent on the remote pod.

        Execution order
        ~~~~~~~~~~~~~~~
        1. Validate the delivery mode.
        2. Put the remote PTY into raw mode and disable bracketed-paste
           (first fenced command).
        3. Deliver and exec the agent (Python or Go path).
        4. Wait for ``AGENT_READY``.

        Raises:
            AgentReadyTimeoutError:    ``AGENT_READY`` not received within the
                                       configured timeout.
            AgentSyntaxError:          Remote Python raised ``SyntaxError``
                                       during the syntax check.
            AgentVersionMismatchError: Remote agent version is incompatible
                                       with this client.
            BootstrapError:            Any other startup failure, including
                                       invalid delivery mode, WebSocket closure,
                                       or shell command timeout.
        """
        use_go = self._tun.bootstrap_use_go_agent
        delivery = self._tun.bootstrap_delivery

        if delivery not in VALID_DELIVERY_MODES:
            raise BootstrapError(
                f"Unknown bootstrap_delivery {delivery!r}. "
                f"Valid values: {sorted(VALID_DELIVERY_MODES)}",
                details={"bootstrap_delivery": delivery},
                hint="Set EXECTUNNEL_BOOTSTRAP_DELIVERY to one of: upload, fetch.",
            )

        self._start = asyncio.get_running_loop().time()
        metrics_inc("bootstrap.started")

        with span("session.bootstrap"):
            # Put the remote PTY into raw mode (cs8 -icanon min 1 time 0
            # -isig -xcase -inpck -opost -echo) and disable bracketed-paste.
            # This must be the very first fenced command so that all
            # subsequent output is free of echo and bracketed-paste wrappers.
            await self._send_fenced_command(f"{_STTY_RAW_CMD}; printf '\\e[?2004l'")
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
        """Deliver and exec the Python agent script.

        Steps
        ~~~~~
        1. Resolve the remote Python interpreter.
        2. Ensure the agent script is present (upload or fetch).
        3. Optionally run a remote ``ast.parse`` syntax check.
        4. ``exec`` the interpreter with the agent path so the agent
           process replaces the shell.

        The ``SYNTAX_OK`` acknowledgement emitted by the remote syntax-check
        command is detected downstream in :meth:`_handle_pre_ready_line`
        while waiting for ``AGENT_READY``.

        Args:
            delivery: Delivery mode — ``"upload"`` or ``"fetch"``.
        """
        agent_path = self._tun.bootstrap_agent_path
        sentinel_path = self._tun.bootstrap_syntax_ok_sentinel
        skip = self._tun.bootstrap_skip_if_present

        # Resolve the Python interpreter once up-front so it is available for
        # both the syntax check and the final exec command.
        py = await self._resolve_remote_python()

        delivered = await self._ensure_agent_delivered(
            agent_path=agent_path,
            delivery=delivery,
            label="python",
            py=py,
        )

        run_syntax = self._tun.bootstrap_syntax_check
        if (
            run_syntax
            and skip
            and not delivered
            and await self._check_file_exists(sentinel_path)
        ):
            logger.info("bootstrap: syntax-OK sentinel present — skipping syntax check")
            metrics_inc("bootstrap.syntax_cache_hit")
            run_syntax = False

        if run_syntax:
            await self._run_syntax_check(agent_path, sentinel_path, py)
        else:
            metrics_inc("bootstrap.syntax_skipped")

        await self._send_command(f"exec {self._shq(py)} {self._shq(agent_path)}")

    async def _run_go_bootstrap(self, delivery: str) -> None:
        """Deliver and exec the pre-built Go agent binary.

        Steps
        ~~~~~
        1. Ensure the Go binary is present (upload or fetch).
        2. ``chmod +x`` the binary (fenced).
        3. ``exec`` the binary so it replaces the shell process.

        No syntax check is performed for Go binaries.

        Args:
            delivery: Delivery mode — ``"upload"`` or ``"fetch"``.
        """
        go_path = self._tun.bootstrap_go_agent_path

        await self._ensure_agent_delivered(
            agent_path=go_path,
            delivery=delivery,
            label="go",
            py=None,
        )

        await self._send_fenced_command(f"chmod +x {self._shq(go_path)}")
        await self._send_command(f"exec {self._shq(go_path)}")

    # ── Unified delivery orchestration ────────────────────────────────────────

    async def _ensure_agent_delivered(
        self,
        *,
        agent_path: str,
        delivery: str,
        label: str,
        py: str | None,
    ) -> bool:
        """Ensure the agent file is present on the pod.

        If the file already exists and ``skip_if_present`` is enabled the
        method returns immediately without re-uploading.  If the file exists
        but ``skip_if_present`` is *disabled* it is removed before
        re-delivery so that a truncated or corrupt previous upload cannot
        be silently reused.

        Args:
            agent_path: Absolute path for the agent on the remote pod.
            delivery:   Delivery mode — ``"upload"`` or ``"fetch"``.
            label:      ``"python"`` or ``"go"`` for metrics and log context.
            py:         Python interpreter path used for the base64 decode
                        fallback during upload (required when
                        ``label="python"``).  Pass ``None`` for Go; the
                        upload helper will fall back to ``"python3"``.

        Returns:
            ``True`` if the agent was freshly delivered; ``False`` if delivery
            was skipped because the file already exists and
            ``skip_if_present`` is enabled.
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
            await self._send_fenced_command(f"rm -f {self._shq(agent_path)}")

        if delivery == "upload":
            b64_data = load_go_agent_b64() if label == "go" else load_agent_b64()
            await self._deliver_via_upload(agent_path, b64_data, label, py=py)
        else:
            await self._deliver_via_fetch(
                agent_path, self._tun.bootstrap_fetch_url, label
            )

        return True

    # ── Shell command helpers ─────────────────────────────────────────────────

    async def _send_command(self, cmd: str) -> None:
        """Send one shell command over the WebSocket exec channel.

        Appends a trailing newline so the remote shell executes the command
        immediately.  This method does **not** wait for any output; use
        :meth:`_send_fenced_command` when you need to synchronise on
        completion.

        Args:
            cmd: Shell command string to send (without trailing newline).

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
                    "host": self._cfg.wss_url,
                    "elapsed_s": self._elapsed,
                },
                hint="Check that the remote shell is still alive.",
            ) from exc

    async def _send_fenced_command(
        self,
        cmd: str,
        timeout: float = FENCE_TIMEOUT_SECS,
    ) -> None:
        """Send a command and wait for an echo-fence marker before returning.

        Sends the command followed immediately by a ``printf`` that emits a
        unique fence marker.  Reads the WebSocket until the fence marker
        arrives, discarding all other output.  All received lines are passed
        through :func:`_strip_ansi` before comparison so that VT100 decoration
        does not prevent the fence from matching.

        The carry buffer (``_stash_carry``) is preserved across calls so that
        a partial line received at the end of one fenced command is prepended
        to the next read.

        Args:
            cmd:     Shell command to execute.
            timeout: Maximum seconds to wait for the fence marker.  Clamped
                     to at least :data:`MIN_FENCE_TIMEOUT_SECS` to guard
                     against accidental zero/negative values.

        Raises:
            BootstrapError: If the WebSocket closes or the timeout expires
                            before the fence marker arrives.
        """
        effective_timeout = max(MIN_FENCE_TIMEOUT_SECS, timeout)
        fence_id = uuid.uuid4().hex[:12]
        fence_marker = f"{FENCE_PREFIX}:{fence_id}"

        await self._send_command(cmd)
        await self._send_command(f"printf '%s\\n' {self._shq(fence_marker)}")

        buf = self._stash_carry
        self._stash_carry = ""
        try:
            async with asyncio.timeout(effective_timeout):
                async for msg in self._ws:
                    chunk = self._decode_ws_message(msg)
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        stripped = _strip_ansi(line).strip()
                        if stripped == fence_marker:
                            self._stash_carry = buf
                            return
                        if stripped:
                            logger.debug("bootstrap fence output: %s", stripped)
                            self._diag.append(stripped)
                            self._stash_lines.append(line)
        except TimeoutError:
            self._stash_carry = buf
            raise BootstrapError(
                f"Timed out after {effective_timeout}s waiting for command "
                f"fence ({cmd[:40]!r}).",
                details={
                    "command": cmd[:80],
                    "host": self._cfg.wss_url,
                    "elapsed_s": self._elapsed,
                    "fence_timeout_s": effective_timeout,
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
                "host": self._cfg.wss_url,
                "elapsed_s": self._elapsed,
            },
            hint="Check that the remote shell is still alive.",
        )

    # ── Pod file-existence probe ──────────────────────────────────────────────

    async def _check_file_exists(
        self,
        path: str,
        timeout: float = FENCE_TIMEOUT_SECS,
    ) -> bool:
        """Return ``True`` if *path* exists on the pod.

        Sends a one-liner that prints :data:`MARKER_YES` or :data:`MARKER_NO`
        depending on whether the file exists, then reads the WebSocket until
        one of those markers arrives.

        Args:
            path:    Absolute path to probe on the remote pod.
            timeout: Maximum seconds to wait for the probe response.  Clamped
                     to at least :data:`MIN_FENCE_TIMEOUT_SECS`.

        Returns:
            ``True`` if the file exists, ``False`` otherwise.

        Raises:
            BootstrapError: If the WebSocket closes or the timeout expires
                            before the marker arrives.
        """
        effective_timeout = max(MIN_FENCE_TIMEOUT_SECS, timeout)
        path_q = self._shq(path)
        await self._send_command(
            f"[ -f {path_q} ] && printf '%s\\n' {self._shq(MARKER_YES)} "
            f"|| printf '%s\\n' {self._shq(MARKER_NO)}",
        )

        buf = self._stash_carry
        self._stash_carry = ""
        try:
            async with asyncio.timeout(effective_timeout):
                async for msg in self._ws:
                    chunk = self._decode_ws_message(msg)
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        stripped = _strip_ansi(line).strip()
                        if stripped == MARKER_YES:
                            self._stash_carry = buf
                            return True
                        if stripped == MARKER_NO:
                            self._stash_carry = buf
                            return False
                        if stripped:
                            logger.debug("bootstrap probe output: %s", stripped)
                            self._diag.append(stripped)
                            self._stash_lines.append(line)
        except TimeoutError:
            self._stash_carry = buf
            raise BootstrapError(
                f"Timed out after {effective_timeout}s waiting for file-existence "
                f"probe response for {path!r}.",
                details={
                    "path": path,
                    "host": self._cfg.wss_url,
                    "elapsed_s": self._elapsed,
                    "probe_timeout_s": effective_timeout,
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
                "host": self._cfg.wss_url,
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
        *,
        py: str | None,
    ) -> None:
        """Upload an agent as base64 chunks and decode it on the pod.

        Upload sequence
        ~~~~~~~~~~~~~~~
        1. Remove any stale staging file (fenced).
        2. Stream all base64 chunks via ``printf … >> staging`` (unfenced,
           for throughput).
        3. Fence after the final chunk to guarantee all writes are complete
           before decoding begins.
        4. Decode staging → *agent_path* via ``sed | base64 -d`` (fenced),
           with a Python fallback using *effective_py* for pods where
           ``base64`` is unavailable.
        5. Remove the staging file (fenced).

        The base64 payload must use URL-safe alphabet (``+`` → ``-``,
        ``/`` → ``_``) with standard ``=`` padding, as produced by
        :func:`~exectunnel.session._payload.load_agent_b64` and
        :func:`~exectunnel.session._payload.load_go_agent_b64`.  The ``sed``
        step on the pod reverses the URL-safe substitution before piping to
        ``base64 -d``.

        Args:
            agent_path: Absolute destination path on the remote pod.
            b64_data:   URL-safe base64-encoded agent payload (padded).
            label:      ``"python"`` or ``"go"`` for metrics and log context.
            py:         Python interpreter path for the decode fallback.
                        Pass ``None`` for Go agents; ``"python3"`` is used
                        as the fallback in that case.

        Raises:
            BootstrapError: If the base64 payload length is not a multiple of
                            4, indicating a bug in the payload encoder
                            (:func:`~exectunnel.session._payload.load_agent_b64`
                            or :func:`~exectunnel.session._payload.load_go_agent_b64`).
        """
        if len(b64_data) % 4 != 0:
            raise BootstrapError(
                f"base64 payload length {len(b64_data)} is not a multiple of 4 — "
                "this indicates a bug in the payload encoder.",
                details={"label": label, "b64_len": len(b64_data)},
                hint="Report this as a bug; do not retry.",
            )

        b64_path = self._b64_staging_path(agent_path)
        b64_path_q = self._shq(b64_path)
        agent_path_q = self._shq(agent_path)
        chunk_size = Defaults.BOOTSTRAP_CHUNK_SIZE_CHARS
        total_chunks = math.ceil(len(b64_data) / chunk_size)

        # Use the provided interpreter; fall back to "python3" for Go agent
        # uploads where no specific interpreter was resolved.
        effective_py = py if py is not None else "python3"
        py_q = self._shq(effective_py)

        metrics_observe(f"bootstrap.{label}.chunks", float(total_chunks))
        logger.info(
            "bootstrap(%s): uploading as %d base64 chunks (%d bytes encoded)",
            label,
            total_chunks,
            len(b64_data),
        )

        await self._send_fenced_command(f"rm -f {b64_path_q}")

        metrics_inc(f"bootstrap.{label}.upload_started")
        for idx, offset in enumerate(range(0, len(b64_data), chunk_size)):
            chunk = b64_data[offset : offset + chunk_size]
            await self._send_command(f"printf '%s' {self._shq(chunk)} >> {b64_path_q}")
            if (idx + 1) % UPLOAD_PROGRESS_LOG_INTERVAL == 0:
                logger.debug(
                    "bootstrap(%s): upload progress %d/%d chunks",
                    label,
                    idx + 1,
                    total_chunks,
                )

        await self._send_fenced_command(f"sync {b64_path_q} 2>/dev/null || true")
        metrics_inc(f"bootstrap.{label}.upload_done")

        decode_cmd = (
            f"(sed 's/-/+/g; s/_/\\//g' {b64_path_q} | base64 -d > {agent_path_q}) "
            f"|| {py_q} -c "
            f"{self._shq(f'import base64; open({agent_path!r},"wb").write(base64.urlsafe_b64decode(open({b64_path!r},"rb").read()))')}"
        )
        await self._send_fenced_command(decode_cmd)
        metrics_inc(f"bootstrap.{label}.decode_done")

        await self._send_fenced_command(f"rm -f {b64_path_q}")

    # ── Delivery: fetch ───────────────────────────────────────────────────────

    async def _deliver_via_fetch(
        self,
        agent_path: str,
        url: str,
        label: str,
    ) -> None:
        """Fetch the agent inside the pod from a URL using curl or wget.

        Sends the fetch command (unfenced) then polls for the output file
        using :meth:`_check_file_exists` until the file appears or the
        deadline (:attr:`~exectunnel.defaults.Defaults.BOOTSTRAP_FETCH_FETCH_DELAY_SECS`)
        is reached.  If the deadline expires without the file appearing a
        warning is logged and execution continues — the download may still
        be in flight and the subsequent ``exec`` will fail with a clear error
        if the file is truly absent.

        Args:
            agent_path: Absolute destination path on the remote pod.
            url:        HTTP/HTTPS URL to fetch the agent from.  Must be
                        reachable from inside the pod.
            label:      ``"python"`` or ``"go"`` for metrics and log context.
        """
        logger.info("bootstrap(%s/fetch): fetching agent from %s", label, url)
        agent_path_q = self._shq(agent_path)
        url_q = self._shq(url)

        fetch_cmd = (
            f"curl -fsSL {url_q} -o {agent_path_q} 2>/dev/null"
            f" || wget -qO {agent_path_q} {url_q}"
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

            probe_timeout = max(MIN_FENCE_TIMEOUT_SECS, remaining)
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
                # Re-check deadline immediately before sleeping.
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
                continue

            # File not yet present — wait before next probe.
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
                min(Defaults.BOOTSTRAP_FETCH_FETCH_POLL_SECS, remaining)
            )

        metrics_inc(f"bootstrap.{label}.fetch_done")

    # ── Syntax check ──────────────────────────────────────────────────────────

    async def _run_syntax_check(
        self,
        agent_path: str,
        sentinel_path: str,
        py: str,
    ) -> None:
        """Run a remote ``ast.parse`` syntax check and write the sentinel file.

        Sends a fenced one-liner that:

        1. Parses the agent script with ``ast.parse``.
        2. Prints ``SYNTAX_OK`` to stdout and flushes.
        3. Creates the sentinel file at *sentinel_path* on success.

        The ``SYNTAX_OK`` acknowledgement is consumed downstream by
        :meth:`_handle_pre_ready_line` while waiting for ``AGENT_READY``.
        If the parse raises ``SyntaxError`` the traceback will also be
        detected by :meth:`_handle_pre_ready_line` and re-raised as
        :exc:`~exectunnel.exceptions.AgentSyntaxError`.

        Args:
            agent_path:    Absolute path of the agent script on the remote pod.
            sentinel_path: Path to write the syntax-OK sentinel file on success.
            py:            Python interpreter path on the remote pod.
        """
        metrics_inc("bootstrap.syntax_started")
        check_cmd = (
            f"{self._shq(py)} -c "
            f"{self._shq(f'import ast, sys; ast.parse(open({agent_path!r}).read()); print("SYNTAX_OK"); sys.stdout.flush(); open({sentinel_path!r}, "w").close()')}"
        )
        await self._send_fenced_command(check_cmd)

    async def _resolve_remote_python(self) -> str:
        """Return the first usable Python interpreter found on the remote pod.

        Probes each candidate in :data:`~exectunnel.session._constants.PYTHON_CANDIDATES`
        in order by sending a ``command -v`` check followed by a unique marker
        ``printf``.  All received lines are passed through :func:`_strip_ansi`
        before comparison.

        The receive buffer (``buf``) is **reset** between candidates so that a
        partial or timed-out response from one candidate does not contaminate
        the next probe.  On timeout the carry buffer is also cleared for the
        same reason — any bytes that arrived late belong to the timed-out
        probe and must not be interpreted as part of the next candidate's
        response.

        Returns:
            The name of the first available Python interpreter (e.g.
            ``"python3.12"``).

        Raises:
            BootstrapError: If no supported interpreter is found on the remote
                            pod.
        """
        for candidate in PYTHON_CANDIDATES:
            marker = f"{FENCE_PREFIX}:PY:{candidate}"
            cmd = (
                f"command -v {candidate} >/dev/null 2>&1 "
                f"&& printf '%s\\n' {self._shq(marker)} || true"
            )
            await self._send_command(cmd)

            # Reset buf for each candidate so a timed-out partial response
            # from the previous probe does not leak into this iteration.
            buf = self._stash_carry
            self._stash_carry = ""
            found = False

            try:
                async with asyncio.timeout(MIN_FENCE_TIMEOUT_SECS):
                    async for msg in self._ws:
                        chunk = self._decode_ws_message(msg)
                        buf += chunk
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            stripped = _strip_ansi(line).strip()
                            if stripped == marker:
                                self._stash_carry = buf
                                found = True
                                break
                            if stripped:
                                self._diag.append(stripped)
                                self._stash_lines.append(line)
                        if found:
                            break
            except TimeoutError:
                # Discard any partial data accumulated for this candidate.
                # Do NOT carry buf forward — it may contain a partial or
                # stale response that would corrupt the next probe.
                self._stash_carry = ""
                continue

            if found:
                return candidate

        raise BootstrapError(
            "No supported Python interpreter found on remote pod.",
            details={
                "candidates": list(PYTHON_CANDIDATES),
                "host": self._cfg.wss_url,
                "elapsed_s": self._elapsed,
            },
            hint=(
                "Install Python 3.12+ on the target pod or enable the Go agent "
                "delivery path."
            ),
        )

    # ── Wait for AGENT_READY ──────────────────────────────────────────────────

    async def _await_agent_ready(self, agent_path: str) -> None:
        """Wait for ``AGENT_READY`` with timeout, then buffer post-ready lines.

        Wraps :meth:`_wait_for_ready` with an
        :func:`asyncio.timeout` guard.  On expiry the last line seen in the
        diagnostic buffer is included in the error message to aid debugging.

        Args:
            agent_path: Agent path forwarded to :meth:`_wait_for_ready` for
                        error context in pre-ready line handling.

        Raises:
            AgentReadyTimeoutError: ``AGENT_READY`` not received within
                                    :attr:`~._config.TunnelConfig.ready_timeout`
                                    seconds.
            AgentSyntaxError:          Propagated from :meth:`_wait_for_ready`.
            AgentVersionMismatchError: Propagated from :meth:`_wait_for_ready`.
            BootstrapError:            Propagated from :meth:`_wait_for_ready`.
        """
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
                    "host": self._cfg.wss_url,
                    "last_output": self._diag[-1] if self._diag else None,
                },
                hint=(
                    "Increase EXECTUNNEL_AGENT_TIMEOUT or check the remote "
                    "environment (Python version, available memory, etc.)."
                ),
            ) from exc

    async def _wait_for_ready(self, agent_path: str) -> None:
        """Read WebSocket messages until ``AGENT_READY`` is seen.

        Drains stashed lines first (accumulated during fenced commands), then
        switches to live WebSocket iteration.  All lines are passed through
        :func:`_strip_ansi` before :func:`~exectunnel.protocol.is_ready_frame`
        and :meth:`_handle_pre_ready_line` so that terminal decoration does
        not prevent detection.

        Once ``AGENT_READY`` is detected any remaining stash lines and all
        subsequent WebSocket messages are appended to
        :attr:`_post_ready_lines` for replay by ``FrameReceiver``.  The
        partial trailing chunk at the point of detection is stored in
        :attr:`_pre_ready_carry`.

        Args:
            agent_path: Agent path used for error context in
                        :meth:`_handle_pre_ready_line`.

        Raises:
            AgentSyntaxError:          Remote Python reported ``SyntaxError``.
            AgentVersionMismatchError: Remote agent version is incompatible.
            BootstrapError:            WebSocket closed before ``AGENT_READY``.
        """
        buf = self._stash_carry
        self._stash_carry = ""
        ready = False

        # popleft() is O(1) on deque; pop(0) on a list would be O(n).
        while self._stash_lines and not ready:
            line = self._stash_lines.popleft()
            if is_ready_frame(_strip_ansi(line)):
                ready = True
                break
            self._handle_pre_ready_line(_strip_ansi(line).strip(), agent_path)

        if ready:
            # Drain any remaining stash lines as post-ready.
            while self._stash_lines:
                self._post_ready_lines.append(self._stash_lines.popleft())
            self._pre_ready_carry = buf
            return

        async for msg in self._ws:
            chunk = self._decode_ws_message(msg)
            buf += chunk

            while "\n" in buf:
                line, buf = buf.split("\n", 1)

                if not ready:
                    if is_ready_frame(_strip_ansi(line)):
                        ready = True
                        continue
                    self._handle_pre_ready_line(_strip_ansi(line).strip(), agent_path)
                else:
                    self._post_ready_lines.append(line)

            if ready:
                self._pre_ready_carry = buf
                return

        detail = f": {self._diag[-1]}" if self._diag else ""
        raise BootstrapError(
            f"WebSocket closed before AGENT_READY was received{detail}",
            details={
                "host": self._cfg.wss_url,
                "elapsed_s": self._elapsed,
                "last_output": self._diag[-1] if self._diag else None,
            },
            hint="Check that the remote shell executed the agent script successfully.",
        )

    def _handle_pre_ready_line(self, stripped: str, agent_path: str) -> None:
        """Process a single line received before ``AGENT_READY``.

        Handles the following special tokens:

        * ``SYNTAX_OK`` — logs the successful syntax check and increments the
          metric counter.
        * ``SyntaxError: …`` — raises :exc:`~exectunnel.exceptions.AgentSyntaxError`.
        * ``Traceback (most recent call last):`` — logs the header and
          continues buffering so the full traceback is captured in
          :attr:`_diag`.
        * ``VERSION_MISMATCH: <version>`` — raises
          :exc:`~exectunnel.exceptions.AgentVersionMismatchError`.
        * Any other non-empty line — appended to the diagnostic deque.

        Args:
            stripped:   The stripped and ANSI-cleaned line text.
            agent_path: Agent path used for error context.

        Raises:
            AgentSyntaxError:          Remote Python reported ``SyntaxError``.
            AgentVersionMismatchError: Remote agent version is incompatible.
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
                    "local_version": self._cfg.version,
                    "minimum_version": self._cfg.version,
                },
                hint=(
                    "Upgrade the exectunnel client or redeploy "
                    "the agent to match the client version."
                ),
            )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _decode_ws_message(msg: str | bytes) -> str:
        """Decode a WebSocket message to ``str``.

        Args:
            msg: A WebSocket message, either ``str`` or ``bytes``.

        Returns:
            The message decoded to ``str``.  Bytes are decoded with the
            ``"replace"`` error handler to avoid crashing on malformed input.
        """
        if isinstance(msg, str):
            return msg
        return msg.decode(errors=WS_DECODE_ERRORS)

    @staticmethod
    def _b64_staging_path(agent_path: str) -> str:
        """Derive a staging path for the intermediate base64 file.

        Args:
            agent_path: The target agent path on the remote pod.

        Returns:
            The staging path with the original extension replaced by ``.b64``
            (e.g. ``/tmp/agent.py`` → ``/tmp/agent.b64``,
            ``/tmp/agent`` → ``/tmp/agent.b64``).
        """
        root, _ext = os.path.splitext(agent_path)
        return root + ".b64"

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
