"""Regression tests for the karaoke highlight bounding box.

The box is fitted to the *actual glyph ink* in the rendered frame, not to the
captured span metrics — those come from a separate Chromium pass and drift by
up to ~a line, so trusting them for pixel geometry put the box half a line high
(or, for the height, a line too short). `_fit_highlight_box` is the pure core
of that logic; here we feed it synthetic frames with glyph rows painted at
known positions and assert the box lands on them.

Each test states the real bug it guards:
  - box rides a line high / low            -> vertical mis-centering
  - box too short to cover the last line   -> multi-line height under-fit
  - box swallows the next paragraph        -> missing n_lines cap
  - drifted metrics                         -> box must follow the ink, not `top`
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.argv = ["pytest"]

import narraoke as h  # noqa: E402


# ── synthetic frame helpers ──────────────────────────────────────────────────

_H, _W = 720, 1280
_BG = 13           # dark page background luminance
_INK = 230         # bright glyph luminance (well above the 110 threshold)
# Content column, matching build_keyframes.
_LEFT = (_W - 920) // 2 + 32
_RIGHT = _W - _LEFT

LINE = 37          # line-box height for 22px body text at line-height 1.7
FONT = 22          # em height


def _blank_frame() -> np.ndarray:
    return np.full((_H, _W, 3), _BG, dtype=np.uint8)


def _paint_line(frame: np.ndarray, top: int, height: int = FONT) -> int:
    """Paint one line of 'glyph' ink across the content column.

    Returns the last inked row (inclusive) — `top + height - 1` — which is what
    the ink scan reports as the line's bottom.
    """
    frame[top:top + height, _LEFT + 40:_RIGHT - 40, :] = _INK
    return top + height - 1


def _fit(frame, nominal_top, n_lines):
    return h._fit_highlight_box(
        frame, nominal_top, n_lines, LINE, FONT, _LEFT, _RIGHT, _H,
    )


# ── single line: hug it with equal padding ───────────────────────────────────

def test_single_line_box_hugs_the_glyphs() -> None:
    frame = _blank_frame()
    ink_top = 300
    ink_bot = _paint_line(frame, ink_top)    # glyphs at rows 300..321
    top, height = _fit(frame, nominal_top=ink_top, n_lines=1)
    pad = h.HIGHLIGHT_PAD
    assert top == ink_top - pad
    assert top + height == ink_bot + pad
    # Equal padding above the first glyph row and below the last.
    assert (ink_top - top) == ((top + height) - ink_bot)


def test_single_line_survives_drifted_metrics() -> None:
    """The captured `top` sits a line below the real glyphs (the drift we saw).
    The box must follow the INK, not the metrics."""
    frame = _blank_frame()
    ink_top = 300
    ink_bot = _paint_line(frame, ink_top)
    # Nominal top is ~a line too low — the box must still land on the ink.
    top, height = _fit(frame, nominal_top=ink_top + 24, n_lines=1)
    pad = h.HIGHLIGHT_PAD
    assert top == ink_top - pad
    assert top + height == ink_bot + pad


# ── multi line: cover EVERY line ─────────────────────────────────────────────

def test_two_line_box_covers_both_lines() -> None:
    """The reported bug: box top had good padding but the bottom fell short of
    the second line. The box must span first-line-top .. second-line-bottom."""
    frame = _blank_frame()
    l1 = 300
    l2 = l1 + LINE                            # next wrapped line
    _paint_line(frame, l1)
    l2_bot = _paint_line(frame, l2)
    top, height = _fit(frame, nominal_top=l1, n_lines=2)
    pad = h.HIGHLIGHT_PAD
    assert top == l1 - pad
    # Bottom must reach the last line's glyph bottom, plus symmetric pad.
    assert top + height == l2_bot + pad
    # Equal padding top and bottom.
    assert (l1 - top) == ((top + height) - l2_bot)


def test_three_line_box_covers_all_three() -> None:
    frame = _blank_frame()
    tops = [250, 250 + LINE, 250 + 2 * LINE]
    last_bot = 0
    for t in tops:
        last_bot = _paint_line(frame, t)
    top, height = _fit(frame, nominal_top=tops[0], n_lines=3)
    pad = h.HIGHLIGHT_PAD
    assert top == tops[0] - pad
    assert top + height == last_bot + pad


# ── the n_lines cap: don't swallow the next paragraph ─────────────────────────

def test_box_does_not_absorb_the_following_paragraph() -> None:
    """A single-line phrase followed by a blank gap and another paragraph: the
    box must cover ONLY the phrase's line, never the paragraph below."""
    frame = _blank_frame()
    phrase_line = 300
    next_para = phrase_line + LINE + 30       # separated by a clear gap
    phrase_bot = _paint_line(frame, phrase_line)
    _paint_line(frame, next_para)
    top, height = _fit(frame, nominal_top=phrase_line, n_lines=1)
    # Box must end above the next paragraph's glyphs.
    assert top + height <= next_para
    assert top + height == phrase_bot + h.HIGHLIGHT_PAD


def test_two_line_cap_stops_before_a_third_paragraph_line() -> None:
    frame = _blank_frame()
    l1, l2 = 300, 300 + LINE
    third = l2 + LINE + 30                     # unrelated line further down
    _paint_line(frame, l1)
    l2_bot = _paint_line(frame, l2)
    _paint_line(frame, third)
    top, height = _fit(frame, nominal_top=l1, n_lines=2)
    assert top + height <= third              # third line excluded
    assert top + height == l2_bot + h.HIGHLIGHT_PAD


# ── degenerate inputs ────────────────────────────────────────────────────────

def test_no_ink_falls_back_to_metrics_box() -> None:
    """A highlighted whitespace-only span has no ink — the box falls back to a
    metrics-only rect rather than crashing or returning nothing."""
    frame = _blank_frame()                    # entirely blank
    top, height = _fit(frame, nominal_top=300, n_lines=1)
    assert (top, height) == (300, max(FONT, LINE))


def test_box_never_reads_outside_the_frame() -> None:
    """A phrase near the bottom edge must not index past the frame height."""
    frame = _blank_frame()
    near_bottom = _H - 10
    _paint_line(frame, near_bottom, height=8)
    top, height = _fit(frame, nominal_top=near_bottom, n_lines=1)
    assert 0 <= top
    assert top + height <= _H + 2 * h.HIGHLIGHT_PAD  # box may pad past, draw clips
