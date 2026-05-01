"""Tests for ``exectunnel.session._bootstrap._strip_ansi``.

Pins the comprehensive ECMA-48 / ISO 6429 escape-sequence stripping
introduced by the deferred-brief polish wave The earlier implementation only
recognised CSI sequences; modern shells emit OSC, DCS, Gn designators,
and other introducers that previously slipped through and broke the
bootstrap fence-marker exact-match.

The cases below are pinned in two groups:

* ``STRIPPED`` — inputs that contain escape sequences (or BOM / bare CR)
  whose visible payload must be preserved while the decoration is
  removed.
* ``PRESERVED`` — inputs that look like they might be over-stripped
  (lone ``ESC`` characters, escape-like printable text, control
  characters that are not ECMA-48 escapes) and must pass through
  untouched apart from documented stripping.
"""

from __future__ import annotations

import pytest
from exectunnel.session._bootstrap import _strip_ansi

# ── Stripping cases ──────────────────────────────────────────────────────────

STRIPPED: list[tuple[str, str, str]] = [
    # (label, input, expected)
    ("plain", "plain text", "plain text"),
    ("csi_sgr", "\x1b[31mred\x1b[0m", "red"),
    ("csi_cursor", "\x1b[2J\x1b[H>>>", ">>>"),
    ("csi_with_intermediates", "\x1b[?2004hbracketed", "bracketed"),
    ("osc_bel", "\x1b]0;window title\x07rest", "rest"),
    ("osc_st", "\x1b]0;title\x1b\\rest", "rest"),
    ("osc_ftcs", "\x1b]133;A\x07prompt> ", "prompt> "),
    ("dcs", "\x1bPdcs payload\x1b\\after", "after"),
    ("sos", "\x1bXsos payload\x1b\\after", "after"),
    ("pm", "\x1b^pm payload\x1b\\after", "after"),
    ("apc", "\x1b_apc payload\x1b\\after", "after"),
    ("g0_designator", "\x1b(Babc", "abc"),
    ("g1_designator", "\x1b)0abc", "abc"),
    ("g2_designator", "\x1b*Babc", "abc"),
    ("g3_designator", "\x1b+Babc", "abc"),
    ("bom", "\ufeffhello", "hello"),
    ("bare_cr", "one\rtwo", "onetwo"),
    (
        "mixed_full_dressing",
        "\x1b[2J\x1b[H\x1b]0;title\x07\x1bP1$r0\x1b\\\ufeffREADY\r",
        "READY",
    ),
    # "EXECTUNNEL_FENCE" markers are commonly preceded by colour prompts and
    # OSC title sets in interactive shells.  This pins the bootstrap-fence
    # use case explicitly.
    (
        "fence_after_decoration",
        "\x1b]0;user@host\x07\x1b[1;32m$\x1b[0m EXECTUNNEL_FENCE_OK",
        "$ EXECTUNNEL_FENCE_OK",
    ),
]


@pytest.mark.parametrize("label,raw,expected", STRIPPED, ids=[c[0] for c in STRIPPED])
def test_strip_ansi_strips(label: str, raw: str, expected: str) -> None:
    assert _strip_ansi(raw) == expected, label


# ── Preservation cases ───────────────────────────────────────────────────────

PRESERVED: list[tuple[str, str]] = [
    # Visible content that resembles escape sequences must be untouched.
    ("plain_brackets", "[31m not an escape"),
    ("printable_only", "no escapes here"),
    ("tab_and_unicode", "\thello \u2603 world"),
    # Newlines are explicitly preserved (bootstrap parses line-by-line).
    ("newline_preserved", "line1\nline2"),
]


@pytest.mark.parametrize("label,raw", PRESERVED, ids=[c[0] for c in PRESERVED])
def test_strip_ansi_preserves(label: str, raw: str) -> None:
    assert _strip_ansi(raw) == raw, label


# ── Property-style sanity checks ─────────────────────────────────────────────


def test_strip_ansi_idempotent() -> None:
    """Stripping twice yields the same result as stripping once."""
    raw = "\x1b]0;t\x07\x1b[31mred\x1b[0m\ufeff body\r"
    once = _strip_ansi(raw)
    twice = _strip_ansi(once)
    assert once == twice


def test_strip_ansi_does_not_consume_following_escape_payload() -> None:
    """Greedy OSC/DCS terminators must not swallow a following escape sequence."""
    # Two distinct OSCs back-to-back; the regex must not coalesce them.
    raw = "\x1b]0;a\x07\x1b]0;b\x07tail"
    assert _strip_ansi(raw) == "tail"
