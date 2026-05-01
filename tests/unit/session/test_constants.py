"""Unit tests for :mod:`exectunnel.session._constants`.

These constants are referenced from many sites — small drift here
silently breaks bootstrap fence parsing, UDP flow caps, etc.  These
tests are pure pin tests: every value is asserted against its
documented contract.
"""

from __future__ import annotations

import re

import pytest
from exectunnel.defaults import Defaults
from exectunnel.session import _constants as C

# ── Bootstrap markers ─────────────────────────────────────────────────────────


class TestBootstrapMarkers:
    def test_fence_prefix(self) -> None:
        assert C.FENCE_PREFIX == "EXECTUNNEL_FENCE"

    def test_marker_yes_and_no_are_built_from_marker_prefix(self) -> None:
        assert f"{C.MARKER_PREFIX}:1" == C.MARKER_YES
        assert f"{C.MARKER_PREFIX}:0" == C.MARKER_NO

    def test_marker_yes_no_distinct(self) -> None:
        assert C.MARKER_YES != C.MARKER_NO


# ── Delivery modes ────────────────────────────────────────────────────────────


class TestDeliveryModes:
    def test_valid_delivery_set(self) -> None:
        assert frozenset({"upload", "fetch"}) == C.VALID_DELIVERY_MODES

    def test_default_is_in_valid_set(self) -> None:
        assert Defaults.BOOTSTRAP_DELIVERY in C.VALID_DELIVERY_MODES


# ── Timeouts & ratios ─────────────────────────────────────────────────────────


class TestTimeouts:
    def test_min_fence_timeout_below_full_fence_timeout(self) -> None:
        assert C.MIN_FENCE_TIMEOUT_SECS < C.FENCE_TIMEOUT_SECS

    def test_max_stash_lines_matches_defaults(self) -> None:
        # Pinned through Defaults so the constant cannot diverge.
        assert C.MAX_STASH_LINES == Defaults.BOOTSTRAP_MAX_STASH_LINES

    def test_upload_fence_every_chunks_matches_defaults(self) -> None:
        assert C.UPLOAD_FENCE_EVERY_CHUNKS == Defaults.UPLOAD_FENCE_EVERY_CHUNKS

    def test_udp_active_flows_cap_matches_defaults(self) -> None:
        assert C.UDP_ACTIVE_FLOWS_CAP == Defaults.UDP_ACTIVE_FLOWS_CAP


# ── Python interpreter probing ────────────────────────────────────────────────


class TestPythonProbing:
    def test_python_candidates_is_ordered_tuple(self) -> None:
        assert isinstance(C.PYTHON_CANDIDATES, tuple)
        # py3.13 must come first per docstring (deployment target).
        assert C.PYTHON_CANDIDATES[0] == "python3.13"
        # All entries non-empty.
        assert all(c for c in C.PYTHON_CANDIDATES)

    def test_min_remote_python_version_is_tuple_3_x(self) -> None:
        assert isinstance(C.MIN_REMOTE_PYTHON_VERSION, tuple)
        assert C.MIN_REMOTE_PYTHON_VERSION[0] == 3
        assert C.MIN_REMOTE_PYTHON_VERSION[1] >= 11


# ── WebSocket close codes ─────────────────────────────────────────────────────


class TestWsCloseCodes:
    def test_protocol_error_code_per_rfc6455(self) -> None:
        # RFC 6455 §7.4.1 — 1002 is "protocol error".
        assert C.WS_CLOSE_CODE_PROTOCOL_ERROR == 1002

    def test_internal_error_code_per_rfc6455(self) -> None:
        # 1011 is "internal error".
        assert C.WS_CLOSE_CODE_INTERNAL_ERROR == 1011


# ── Receiver: unexpected no-conn-id frames ────────────────────────────────────


class TestUnexpectedFrames:
    def test_includes_agent_ready(self) -> None:
        # AGENT_READY may only appear once during bootstrap; subsequent
        # appearances are protocol violations.
        assert "AGENT_READY" in C.UNEXPECTED_NO_CONN_ID_FRAMES

    def test_includes_keepalive(self) -> None:
        # KEEPALIVE is client-only; agent must not echo it.
        assert "KEEPALIVE" in C.UNEXPECTED_NO_CONN_ID_FRAMES

    def test_does_not_include_liveness(self) -> None:
        # LIVENESS is agent-emitted; receiving it is *expected*.
        assert "LIVENESS" not in C.UNEXPECTED_NO_CONN_ID_FRAMES


# ── Misc ──────────────────────────────────────────────────────────────────────


class TestMisc:
    def test_ws_decode_errors_is_replace(self) -> None:
        # Receiving non-UTF8 bytes is a wire violation but must not
        # crash the receive loop.
        assert C.WS_DECODE_ERRORS == "replace"

    def test_log_truncate_len_positive(self) -> None:
        assert C.LOG_TRUNCATE_LEN > 0

    def test_udp_max_datagram_size_is_uint16_max(self) -> None:
        assert C.UDP_MAX_DATAGRAM_SIZE == 65_535

    def test_safe_host_re_blocks_special_chars(self) -> None:
        # The pattern matches *unsafe* characters — the chars to strip.
        assert isinstance(C.SAFE_HOST_RE, re.Pattern)
        assert C.SAFE_HOST_RE.search("safe.host") is None
        assert C.SAFE_HOST_RE.search("with space") is not None
        assert C.SAFE_HOST_RE.search("with:colon") is not None

    @pytest.mark.parametrize(
        "cidr",
        [
            "::1/128",
            "fc00::/7",
            "fe80::/10",
        ],
    )
    def test_default_exclude_ipv6_cidrs(self, cidr: str) -> None:
        assert cidr in C.DEFAULT_EXCLUDE_IPV6_CIDRS

    def test_host_semaphore_capacity_positive(self) -> None:
        assert C.HOST_SEMAPHORE_CAPACITY > 0
