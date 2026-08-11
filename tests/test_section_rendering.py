"""Tests for per-section rendering: the `--sections` filter and concurrency.

These cover the argument parsing and worker-count logic. The encode itself is
verified by rendering: sections produced concurrently are pixel-identical to
the sequential ones (mse 0.00 / psnr inf on every frame), with only NVENC
rate-control nondeterminism showing up as a few bytes of file-size difference.

The frame-grouping tests below guard the audio/video sync invariant that the
encode relies on: every one of a phrase's frames (a summary's main hold PLUS
its dwell scroll/hold sub-frames) must reach the section concat. Dropping the
sub-frames made every per-section MP4 shorter than its audio and left the
highlight running ahead of the narration after each table/code block — a bug
that only showed up in the rendered video, never in the arg-parsing tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.argv = ["pytest"]

import narraoke as h  # noqa: E402


# ── frame grouping: sub-frames must survive ──────────────────────────────────

def _kf(name: str, dur: float) -> tuple[Path, float]:
    return (Path("/frames") / name, dur)


def test_grouping_keeps_a_plain_frame() -> None:
    grouped = h._group_frames_by_phrase([_kf("frame_00003.png", 2.0)])
    assert grouped == {3: [(Path("/frames/frame_00003.png"), 2.0)]}


def test_grouping_keeps_dwell_subframes_with_their_phrase() -> None:
    """The exact regression: `_001`/`_002` sub-frames belong to phrase 74.

    A regex of `frame_(\\d+)\\.png$` matches the main frame but silently drops
    the sub-frames, so this is the assertion that would have caught the bug.
    """
    grouped = h._group_frames_by_phrase([
        _kf("frame_00074.png", 3.14),
        _kf("frame_00074_001.png", 3.63),
    ])
    assert 74 in grouped
    assert grouped[74] == [
        (Path("/frames/frame_00074.png"), 3.14),
        (Path("/frames/frame_00074_001.png"), 3.63),
    ]


def test_grouping_orders_main_before_subframes_regardless_of_input_order() -> None:
    grouped = h._group_frames_by_phrase([
        _kf("frame_00010_002.png", 0.5),
        _kf("frame_00010.png", 1.0),
        _kf("frame_00010_001.png", 0.5),
    ])
    assert [p.name for p, _ in grouped[10]] == [
        "frame_00010.png",
        "frame_00010_001.png",
        "frame_00010_002.png",
    ]


def test_grouped_duration_equals_flat_keyframe_duration() -> None:
    """Section A/V sync invariant: grouping must neither drop nor duplicate
    time. The sum of every grouped frame's duration equals the sum of the
    input keyframes' durations — so a section's video track stays as long as
    the audio slice it is muxed against."""
    keyframes = [
        _kf("frame_00000.png", 1.9),
        _kf("frame_00001.png", 2.0),
        _kf("frame_00074.png", 3.14),
        _kf("frame_00074_001.png", 3.63),   # dwell hold — must be counted
        _kf("frame_00075.png", 2.04),
    ]
    grouped = h._group_frames_by_phrase(keyframes)
    grouped_total = sum(d for frames in grouped.values() for _, d in frames)
    assert grouped_total == pytest.approx(sum(d for _, d in keyframes))


def test_grouping_ignores_unrecognised_names() -> None:
    grouped = h._group_frames_by_phrase([
        _kf("frame_00001.png", 1.0),
        _kf("title_card.png", 5.0),
        _kf("intro.png", 2.0),
    ])
    assert set(grouped) == {1}


# ── --sections parsing ───────────────────────────────────────────────────────

def test_no_spec_means_every_section() -> None:
    assert h._parse_section_spec(None) is None
    assert h._parse_section_spec("") is None


def test_single_index() -> None:
    assert h._parse_section_spec("3") == {3}


def test_comma_separated_indices() -> None:
    assert h._parse_section_spec("0,2,5") == {0, 2, 5}


def test_inclusive_ranges() -> None:
    """`3-5` includes 5 — an operator counting sections means both ends."""
    assert h._parse_section_spec("3-5") == {3, 4, 5}


def test_mixed_indices_and_ranges() -> None:
    assert h._parse_section_spec("0,3-5,9") == {0, 3, 4, 5, 9}


def test_reversed_range_is_accepted() -> None:
    """`5-3` is a typo with an obvious intent; honour it rather than fail."""
    assert h._parse_section_spec("5-3") == {3, 4, 5}


def test_duplicates_collapse() -> None:
    assert h._parse_section_spec("2,2,2") == {2}


def test_whitespace_is_tolerated() -> None:
    assert h._parse_section_spec(" 0 , 3 - 4 ") == {0, 3, 4}


def test_unparseable_fragments_are_skipped_not_fatal() -> None:
    """A typo should cost one section, not the run."""
    assert h._parse_section_spec("1,bogus,3") == {1, 3}


def test_entirely_unparseable_spec_means_all() -> None:
    """Nothing usable parsed — fall back to rendering everything rather
    than silently rendering none."""
    assert h._parse_section_spec("bogus") is None


# ── worker count ─────────────────────────────────────────────────────────────

def test_default_worker_count_is_bounded() -> None:
    """Bounded by NVENC's concurrent-session limit, not by core count.

    Each ffmpeg is already multi-threaded, so oversubscribing slows every
    section rather than speeding the batch up.
    """
    workers = h._default_section_workers()
    assert 1 <= workers <= 4


def test_worker_count_never_drops_below_one(monkeypatch) -> None:
    monkeypatch.setattr(h.os, "cpu_count", lambda: 1)
    assert h._default_section_workers() >= 1


def test_worker_count_survives_unknown_cpu_count(monkeypatch) -> None:
    """`os.cpu_count()` returns None on some platforms."""
    monkeypatch.setattr(h.os, "cpu_count", lambda: None)
    assert h._default_section_workers() >= 1
