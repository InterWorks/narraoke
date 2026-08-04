"""Tests for stage timing and the end-of-run warning summary.

A run prints hundreds of lines over ~16 minutes. Two problems followed from
that: the slowest stages had the least granularity, so a long silence looked
like a hang; and warnings sat mid-log at the same visual weight as routine
chatter, so anything genuinely wrong was easy to miss.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.argv = ["pytest"]

import utils  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    utils.reset_stage_timings()
    utils.reset_warnings()
    yield
    utils.reset_stage_timings()
    utils.reset_warnings()


# ── duration formatting ──────────────────────────────────────────────────────

@pytest.mark.parametrize("seconds,expected", [
    (0.0, "0.0s"),
    (0.5, "0.5s"),
    (45.2, "45.2s"),
    (59.9, "59.9s"),
    (60, "1m 00s"),
    (95, "1m 35s"),
    (412, "6m 52s"),          # the sequential per-section cost
    (962, "16m 02s"),         # the reference full-render wall clock
    (5400, "1h 30m"),
])
def test_duration_scales_with_magnitude(seconds, expected) -> None:
    assert utils._format_duration(seconds) == expected


# ── stage timing ─────────────────────────────────────────────────────────────

def test_each_stage_is_timed(capsys) -> None:
    utils.step("First …")
    utils.step("Second …")
    utils.finish_stages()
    labels = [label for label, _ in utils.stage_timings()]
    assert labels == ["First", "Second"]


def test_timing_is_printed_as_the_next_stage_starts(capsys) -> None:
    """Feedback arrives during the run, not only in the final summary."""
    utils.step("Slow thing …")
    time.sleep(0.02)
    utils.step("Next thing …")
    output = capsys.readouterr().out
    assert ">>> Slow thing" in output
    assert "s)" in output, "expected an elapsed-time line after the first stage"


def test_finish_stages_closes_the_last_stage() -> None:
    """Without this the final stage — often the longest — is never recorded."""
    utils.step("Only stage …")
    assert utils.stage_timings() == []
    utils.finish_stages()
    assert [label for label, _ in utils.stage_timings()] == ["Only stage"]


def test_finish_stages_is_idempotent() -> None:
    utils.step("Stage …")
    utils.finish_stages()
    utils.finish_stages()
    assert len(utils.stage_timings()) == 1


def test_ellipsis_is_stripped_from_labels() -> None:
    """Stage messages end with ' …'; the summary reads better without it."""
    utils.step("Encoding video …")
    utils.finish_stages()
    assert utils.stage_timings()[0][0] == "Encoding video"


def test_timings_are_positive() -> None:
    utils.step("Work …")
    time.sleep(0.01)
    utils.finish_stages()
    assert utils.stage_timings()[0][1] > 0


# ── warning collection ───────────────────────────────────────────────────────

def test_warnings_are_collected_in_order() -> None:
    utils.warn("first problem")
    utils.warn("second problem")
    assert utils.collected_warnings() == ["first problem", "second problem"]


def test_warnings_are_still_printed_when_raised(capsys) -> None:
    """Collecting them for the summary must not silence them in place."""
    utils.warn("visible now")
    assert "visible now" in capsys.readouterr().out


def test_a_warning_survives_a_wall_of_routine_output() -> None:
    """A warning must still be findable after hundreds of INFO lines.

    The example text is the old coord-count warning, kept because it is
    realistic in shape and length. That particular warning turned out to be
    a false alarm and has since been replaced — see
    `tests/test_phrase_coverage.py` — but the plumbing tested here is what
    makes any genuine warning visible at the end of a ~16 minute run.
    """
    utils.warn(
        "Coord count (1044) doesn't match phrase count (1071). "
        "Highlight alignment may drift."
    )
    for _ in range(50):
        utils.info("routine chatter")
    summary = utils.collected_warnings()
    assert len(summary) == 1
    assert "1044" in summary[0]


def test_info_is_not_collected() -> None:
    """Only warnings belong in the summary."""
    utils.info("just a note")
    assert utils.collected_warnings() == []


def test_no_warnings_means_an_empty_summary() -> None:
    assert utils.collected_warnings() == []
