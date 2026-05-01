"""Invariant pin tests for :class:`exectunnel.defaults.Defaults`.

``Defaults`` is a frozen dataclass acting as a single source of truth
for every numeric / string knob in the package.  These tests are
deliberately lightweight — they don't try to enforce a "correct" value
for any individual constant, only that:

1. The dataclass is genuinely frozen (mutating it is impossible).
2. Each constant has a defensible *type* and is non-negative where the
   unit (``_SECS``, ``_BYTES``, ``_CHARS``, ``_CAP``) makes negativity
   meaningless.
3. Pairs of constants that have a documented relationship hold that
   relationship at module-import time. A regression here means
   somebody changed one half of a coupled pair without updating the
   other — exactly the class of bug the codebase has had before
   (Finding 5 RX-liveness ratios; Finding 8 upload-cadence ratios).
"""

from __future__ import annotations

import dataclasses
from typing import Final

import pytest
from exectunnel.defaults import Defaults

# Constants whose names end with these suffixes must be ``>= 0``.  We
# keep this list **inside** the test rather than reading it from
# Defaults itself so that adding a new naming convention forces a
# conscious test update.
_NON_NEGATIVE_SUFFIXES: Final[tuple[str, ...]] = (
    "_SECS",
    "_MS",
    "_BYTES",
    "_CHARS",
    "_CAP",
    "_INTERVAL",
    "_TIMEOUT",
    "_THRESHOLD",
    "_MAX_RETRIES",
    "_RATIO",
    "_PORT",
    "_INFLIGHT",
    "_MAX_LINES",
    "_DIAG_MAX_LINES",
    "_QUEUE_CAP",
    "_PER_HOST",
    "_FLOWS_CAP",
    "_PENDING",
    "_EVERY",
    "_EVERY_CHUNKS",
)


def _is_numeric_constant(name: str, value: object) -> bool:
    """Whether *name*/*value* is a numeric constant we care to range-check."""
    if name.startswith("_") or not name.isupper():
        return False
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ── Frozen-ness ───────────────────────────────────────────────────────────────


class TestFrozenContract:
    """``Defaults`` must be a frozen dataclass — accidental mutation is a bug."""

    def test_is_dataclass(self) -> None:
        assert dataclasses.is_dataclass(Defaults)

    def test_is_frozen(self) -> None:
        params = Defaults.__dataclass_params__
        assert params.frozen is True

    def test_uses_slots(self) -> None:
        # ``slots=True`` blocks accidental attribute injection on instances.
        assert Defaults.__dataclass_params__.slots is True

    def test_instance_attribute_assignment_raises(self) -> None:
        """Mutating an instance attribute must raise.

        ``Defaults`` carries every value as a ``ClassVar``, so the
        instance has *no* instance attributes — combined with
        ``slots=True`` and ``frozen=True`` this produces a slightly
        unusual exception surface (``FrozenInstanceError`` /
        ``AttributeError`` / ``TypeError`` depending on Python
        version and call path).  We accept any of them.
        """
        instance = Defaults()
        with pytest.raises((
            dataclasses.FrozenInstanceError,
            AttributeError,
            TypeError,
        )):
            instance.WS_PING_INTERVAL_SECS = 0  # type: ignore[misc]


# ── Per-constant type / range invariants ──────────────────────────────────────


class TestNumericRanges:
    """Every numeric constant must be non-negative within its declared unit.

    The *only* legitimately-negative numeric defaults are those carrying
    a sign in their semantic (none today).  If you need one in future,
    add an explicit allow-list entry below.
    """

    @pytest.mark.parametrize(
        "name",
        [
            n
            for n, v in vars(Defaults).items()
            if _is_numeric_constant(n, v)
            and any(n.endswith(s) for s in _NON_NEGATIVE_SUFFIXES)
        ],
    )
    def test_constant_is_non_negative(self, name: str) -> None:
        value = getattr(Defaults, name)
        assert value >= 0, f"{name} = {value!r} must be non-negative"

    def test_string_constants_are_non_empty(self) -> None:
        # All ``str``-typed defaults model a path / URL / host and must
        # therefore be non-empty.
        for name, value in vars(Defaults).items():
            if name.startswith("_") or not name.isupper():
                continue
            if isinstance(value, str):
                assert value, f"{name} must not be the empty string"


# ── Cross-constant relational invariants ──────────────────────────────────────


class TestRelationalInvariants:
    """Pin documented relationships between coupled defaults.

    Each test is one ratio — a regression points directly at the
    pair the developer broke.
    """

    def test_ws_send_timeout_below_ping_interval(self) -> None:
        # A single send taking longer than the ping interval would mean
        # the keepalive can't make progress.  See WS_PING_INTERVAL_SECS
        # docstring.
        assert Defaults.WS_SEND_TIMEOUT_SECS < Defaults.WS_PING_INTERVAL_SECS

    def test_rx_liveness_timeout_above_interval(self) -> None:
        # Finding 5 — RX timeout must cover at least one full liveness
        # cycle plus jitter buffer.
        assert (
            Defaults.RX_LIVENESS_TIMEOUT_SECS > Defaults.LIVENESS_INTERVAL_SECS * 2
        ), (
            "RX_LIVENESS_TIMEOUT_SECS should comfortably exceed 2× LIVENESS_INTERVAL_SECS"
        )

    def test_rx_liveness_check_interval_is_subharmonic(self) -> None:
        # The watchdog must wake at least twice per liveness cycle so a
        # missed beat is observed within at most one cycle.
        assert (
            Defaults.RX_LIVENESS_CHECK_INTERVAL_SECS
            <= Defaults.LIVENESS_INTERVAL_SECS / 2
        )

    def test_per_host_pending_below_global(self) -> None:
        assert Defaults.CONNECT_MAX_PENDING_PER_HOST <= Defaults.CONNECT_MAX_PENDING

    def test_unterminated_buffer_at_least_one_frame(self) -> None:
        # The unterminated receive buffer must be at least
        # TUNNEL_MAX_FRAME_CHARS (parser docstring) so a single
        # max-sized frame cannot trip the limit.
        assert Defaults.TUNNEL_MAX_UNTERMINATED_CHARS >= Defaults.TUNNEL_MAX_FRAME_CHARS

    def test_dns_local_port_is_above_well_known(self) -> None:
        # IANA-reserved well-known range is 0..1023.  Local DNS forwarder
        # binds without root privileges, so it must be ephemeral.
        assert Defaults.DNS_LOCAL_PORT > 1023

    def test_dns_upstream_port_is_53(self) -> None:
        # Pinned because changing this silently would steer all DNS to
        # a wrong upstream.
        assert Defaults.DNS_UPSTREAM_PORT == 53

    def test_socks_default_host_is_loopback(self) -> None:
        # Security-critical: the SOCKS5 server defaults to loopback to
        # avoid accidentally becoming an open proxy.
        assert Defaults.SOCKS_DEFAULT_HOST == "127.0.0.1"

    def test_socks_default_port_is_iana_registered(self) -> None:
        assert Defaults.SOCKS_DEFAULT_PORT == 1080

    def test_bootstrap_delivery_literal(self) -> None:
        assert Defaults.BOOTSTRAP_DELIVERY in {"fetch", "upload"}

    def test_ws_close_code_in_private_use_range(self) -> None:
        # RFC 6455 §11.7 — 4000..4999 is the private-use range.
        assert 4000 <= Defaults.WS_CLOSE_CODE_UNHEALTHY < 5000

    def test_reconnect_delay_caps_above_base(self) -> None:
        assert (
            Defaults.WS_RECONNECT_MAX_DELAY_SECS
            >= Defaults.WS_RECONNECT_BASE_DELAY_SECS
        )

    def test_pace_jitter_below_pace_interval(self) -> None:
        assert (
            Defaults.CONNECT_PACE_JITTER_CAP_SECS <= Defaults.CONNECT_PACE_INTERVAL_SECS
        )

    def test_max_inflight_dns_positive(self) -> None:
        assert Defaults.DNS_MAX_INFLIGHT > 0

    def test_inbound_tcp_queue_cap_at_least_chunks_for_64kib(self) -> None:
        # Queue cap × pipe-read chunk size must accommodate at least one
        # MSS-rounded TCP receive window (~64 KiB) without dropping.
        assert Defaults.TCP_INBOUND_QUEUE_CAP * Defaults.PIPE_READ_CHUNK_BYTES >= 65_536

    def test_udp_active_flows_cap_positive(self) -> None:
        assert Defaults.UDP_ACTIVE_FLOWS_CAP > 0

    def test_bootstrap_chunk_size_below_arg_max_floor(self) -> None:
        # busybox ash ARG_MAX floor is 128 KiB; a single shell chunk
        # must stay comfortably under that.
        assert Defaults.BOOTSTRAP_CHUNK_SIZE_CHARS <= 65_536


# ── Stable identifiers / paths ────────────────────────────────────────────────


class TestSentinelPaths:
    """Bootstrap sentinels live under ``/tmp`` (writable in every distro)."""

    @pytest.mark.parametrize(
        "name",
        [
            "BOOTSTRAP_SYNTAX_OK_SENTINEL",
            "BOOTSTRAP_AGENT_PATH",
            "BOOTSTRAP_GO_AGENT_PATH",
        ],
    )
    def test_path_under_tmp(self, name: str) -> None:
        value = getattr(Defaults, name)
        assert value.startswith("/tmp/"), f"{name} = {value!r}"

    def test_fetch_url_uses_https(self) -> None:
        assert Defaults.BOOTSTRAP_FETCH_AGENT_URL.startswith("https://")
