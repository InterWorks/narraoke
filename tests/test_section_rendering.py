"""Tests for per-section rendering: the `--sections` filter and concurrency.

These cover the argument parsing and worker-count logic. The encode itself is
verified by rendering: sections produced concurrently are pixel-identical to
the sequential ones (mse 0.00 / psnr inf on every frame), with only NVENC
rate-control nondeterminism showing up as a few bytes of file-size difference.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.argv = ["pytest"]

import narraoke as h  # noqa: E402


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
