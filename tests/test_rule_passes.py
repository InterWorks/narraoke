"""Tests for the tier-4 pass registry.

The word-level IPA passes used to be functions in narraoke.py, invoked by
name from a hard-coded sequence in `rewrite_for_tts`. Adding one meant
editing that sequence. They now live in `rules/passes.py` and register
themselves against a named stage.

What must stay true after that move:

  * order comes from ORDERED_PASSES, never from definition or import order
  * every registered pass actually runs
  * a pass cannot register against a stage that does not exist
  * the stage mechanism stays separate from the tier 1-3 REGEX_STAGES, which
    is a public file format and deliberately exposes only two hooks
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.argv = ["pytest"]

import narraoke as h  # noqa: E402
import rules  # noqa: E402
from rules import passes  # noqa: E402


def test_passes_run_in_declared_order() -> None:
    """Application order is ORDERED_PASSES order, for each stage."""
    for stage in passes.PASS_STAGES:
        declared = [p.name for p in passes.ORDERED_PASSES if p.stage == stage]
        assert [p.name for p in passes.passes_for(stage)] == declared


def test_definition_order_is_not_application_order() -> None:
    """The registry, not the source layout, decides when a pass runs.

    `fix_copied` is defined first in the module but registered last among the
    word_ipa passes. If this ever coincides, the test has stopped proving
    anything and the registration list should be re-checked.
    """
    word_ipa = [p.name for p in passes.passes_for("word_ipa")]
    assert word_ipa[-1] == "copied", (
        "copied is defined first but registered last; if that changed, this "
        "test no longer demonstrates that the two orders are independent"
    )


def test_every_registered_pass_is_reachable_from_a_stage() -> None:
    """A pass whose stage is never applied would be silently dead."""
    applied: list[str] = []
    for stage in passes.PASS_STAGES:
        applied.extend(p.name for p in passes.passes_for(stage))
    assert sorted(applied) == sorted(p.name for p in passes.ORDERED_PASSES)


def test_every_stage_is_applied_by_rewrite_for_tts() -> None:
    """Declaring a stage the pipeline never calls would silently drop passes.

    Asserted against the source of `rewrite_for_tts` rather than by running
    it, because a stage with no observable rewrite would pass a behavioural
    check while still being dead.
    """
    import inspect

    src = inspect.getsource(h.rewrite_for_tts)
    for stage in passes.PASS_STAGES:
        assert f'"{stage}"' in src, (
            f"stage {stage!r} is declared in PASS_STAGES but never applied in "
            f"rewrite_for_tts — its passes would never run"
        )


def test_pass_names_are_unique() -> None:
    """Names identify a pass in warnings and in these tests."""
    names = [p.name for p in passes.ORDERED_PASSES]
    assert len(names) == len(set(names))


def test_an_unknown_stage_is_rejected_at_construction() -> None:
    """Fail at import time, naming the pass, rather than silently never
    running it."""
    with pytest.raises(ValueError, match="unknown stage"):
        passes.Pass(name="bogus", fn=lambda s: s, stage="nonexistent")


def test_passes_for_rejects_an_unknown_stage() -> None:
    with pytest.raises(ValueError, match="unknown stage"):
        passes.passes_for("nonexistent")


def test_pass_stages_are_independent_of_the_public_regex_stages() -> None:
    """The two mechanisms must not be conflated.

    `rules/stack.py` REGEX_STAGES is a *public file format* for tiers 1-3 and
    exposes only two hooks on purpose — every hook exposed there freezes an
    internal step into a format outside this repo. PASS_STAGES names internal
    positions for reviewed in-repo code, so it is free to grow. Sharing a
    vocabulary would invite one to be changed for the other's reasons.
    """
    from rules import stack

    assert set(passes.PASS_STAGES).isdisjoint(set(stack.REGEX_STAGES))


def test_the_registry_is_exported_from_the_package() -> None:
    """narraoke.py reaches these through `rules`, not the submodule."""
    assert rules.apply_passes is passes.apply_passes
    assert rules.ORDERED_PASSES is passes.ORDERED_PASSES


def test_apply_passes_composes_every_pass_in_the_stage() -> None:
    """An end-to-end check that registration actually wires a pass in."""
    out = rules.apply_passes("A transient enum was copied.", "word_ipa")
    for fragment in ("tɹˈænziənt", "ˈinʌm", "kˈɒpid"):
        assert fragment in out


def test_moved_passes_keep_their_narraoke_aliases() -> None:
    """The private names are referenced by existing tests and callers; the
    move must not silently break them."""
    assert h._fix_copied is passes.fix_copied
    assert h._fix_transient is passes.fix_transient
    assert h._fix_enum is passes.fix_enum
    assert h._fix_retryable is passes.fix_retryable
    assert h._force_verb_stress_heteronyms is passes.force_verb_stress_heteronyms


def test_passes_module_carries_no_literals() -> None:
    """`passes` is not a literal-bearing module and must stay out of
    ORDERED_RULE_SOURCES, which assembles `LITERALS` only."""
    assert not hasattr(passes, "LITERALS")
    assert "passes" not in rules.ORDERED_RULE_SOURCES
