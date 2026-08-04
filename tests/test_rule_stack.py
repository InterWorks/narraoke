"""Tests for the four-tier rule stack.

These pin the invariants the tier split actually rests on: precedence order,
that empty tiers are a no-op, that named pronunciations merge most-specific-
first, and that the substring-interference lint fires on a known-bad set.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.argv = ["pytest"]

import html_to_video as h  # noqa: E402
from rules.stack import (  # noqa: E402
    TIER_ORDER,
    LiteralRule,
    NamedPronunciation,
    RuleSet,
    RuleStack,
)


def _lit(frm: str, to: str, tier: str) -> LiteralRule:
    return LiteralRule(frm=frm, to=to, origin=tier)


def _set(tier: str, *pairs: tuple[str, str], named=()) -> RuleSet:
    return RuleSet(
        tier=tier,
        source=f"<{tier}>",
        literals=tuple(_lit(f, t, tier) for f, t in pairs),
        named=tuple(named),
    )


# ── precedence ───────────────────────────────────────────────────────────────

def test_tier_order_is_most_specific_first() -> None:
    assert TIER_ORDER == ("project", "user", "company", "universal")


def test_same_rule_in_all_four_tiers_resolves_to_project() -> None:
    """The whole point of the stack: most specific wins.

    Application order *is* precedence — the project rule runs first and
    consumes the text, so the tiers beneath it never see it.
    """
    stack = RuleStack(
        project=_set("project", ("TOKEN", "project wins")),
        user=_set("user", ("TOKEN", "user wins")),
        company=_set("company", ("TOKEN", "company wins")),
        universal=_set("universal", ("TOKEN", "universal wins")),
    )
    text = "TOKEN"
    for frm, to in stack.literal_pairs():
        text = text.replace(frm, to)
    assert text == "project wins"


def test_precedence_falls_through_to_the_next_tier() -> None:
    """With project absent, user wins; and so on down the stack."""
    def resolve(stack: RuleStack) -> str:
        text = "TOKEN"
        for frm, to in stack.literal_pairs():
            text = text.replace(frm, to)
        return text

    universal = _set("universal", ("TOKEN", "universal wins"))
    company = _set("company", ("TOKEN", "company wins"))
    user = _set("user", ("TOKEN", "user wins"))

    assert resolve(RuleStack(universal=universal)) == "universal wins"
    assert resolve(RuleStack(company=company, universal=universal)) == "company wins"
    assert resolve(
        RuleStack(user=user, company=company, universal=universal)
    ) == "user wins"


def test_empty_tiers_are_a_no_op() -> None:
    """Tiers 2 and 3 empty must leave universal output untouched.

    This is the property that let the tier machinery land before any rule
    actually moved.
    """
    universal = _set("universal", ("A", "a"), ("B", "b"))
    bare = RuleStack(universal=universal)
    padded = RuleStack(
        project=RuleSet(tier="project"),
        user=RuleSet(tier="user"),
        company=RuleSet(tier="company"),
        universal=universal,
    )
    assert bare.literal_pairs() == padded.literal_pairs()


# ── named pronunciations ─────────────────────────────────────────────────────

def test_named_pronunciations_merge_most_specific_first() -> None:
    """Names merge by identity, not by application order."""
    stack = RuleStack(
        project=RuleSet(
            tier="project",
            named=(NamedPronunciation(name="Acme", ipa="/proj/", origin="project"),),
        ),
        company=RuleSet(
            tier="company",
            named=(NamedPronunciation(name="Acme", ipa="/corp/", origin="company"),),
        ),
    )
    merged = stack.merged_named()
    assert len(merged) == 1
    assert merged[0].ipa == "/proj/"


def test_conflicting_names_are_reported() -> None:
    """Two tiers defining one name with *different* IPA is worth a warning."""
    stack = RuleStack(
        project=RuleSet(
            tier="project",
            named=(NamedPronunciation(name="Acme", ipa="/proj/", origin="project"),),
        ),
        company=RuleSet(
            tier="company",
            named=(NamedPronunciation(name="Acme", ipa="/corp/", origin="company"),),
        ),
    )
    conflicts = stack.conflicting_names()
    assert len(conflicts) == 1
    assert conflicts[0][0] == "Acme"


def test_identical_names_in_two_tiers_are_not_a_conflict() -> None:
    """Duplication is harmless when the definitions agree."""
    entry = dict(name="Acme", ipa="/same/")
    stack = RuleStack(
        project=RuleSet(
            tier="project",
            named=(NamedPronunciation(origin="project", **entry),),
        ),
        company=RuleSet(
            tier="company",
            named=(NamedPronunciation(origin="company", **entry),),
        ),
    )
    assert stack.conflicting_names() == []


# ── lint ─────────────────────────────────────────────────────────────────────

def test_lint_flags_a_shadowed_rule() -> None:
    """A rule whose text an earlier rule consumes can never fire."""
    stack = RuleStack(
        project=_set("project", ("JSON", "jay-sahn")),
        universal=_set("universal", ("Invalid JSON", "in-VAL-id jay-sahn")),
    )
    messages = stack.lint()
    assert messages, "expected the shadowed universal rule to be reported"
    assert "Invalid JSON" in messages[0]


def test_lint_is_quiet_on_the_real_builtin_stack() -> None:
    """The shipped tier-4 rules must not shadow one another.

    They are hand-ordered longest-first precisely to avoid this.
    """
    stack = RuleStack.builtin_only(h._LITERAL_TTS_OVERRIDES)
    assert stack.lint() == []


# ── immutability + construction ──────────────────────────────────────────────

def test_rule_stack_is_immutable() -> None:
    stack = RuleStack.builtin_only([("A", "a")])
    with pytest.raises(Exception):
        stack.universal = RuleSet(tier="universal")  # type: ignore[misc]


def test_with_tier_returns_a_copy() -> None:
    original = RuleStack.builtin_only([("A", "a")])
    updated = original.with_tier("company", _set("company", ("B", "b")))
    assert original.company.is_empty
    assert not updated.company.is_empty
    assert updated.universal == original.universal


def test_with_tier_rejects_an_unknown_tier() -> None:
    with pytest.raises(ValueError, match="unknown tier"):
        RuleStack().with_tier("nonsense", RuleSet(tier="nonsense"))


def test_builtin_only_carries_origin() -> None:
    """Every rule records where it came from, for warnings and the summary."""
    stack = RuleStack.builtin_only([("A", "a")])
    assert stack.universal.literals[0].origin == "universal"


def test_summary_names_every_tier() -> None:
    lines = RuleStack.builtin_only([("A", "a")]).summary()
    assert len(lines) == len(TIER_ORDER)
    assert any("universal" in line for line in lines)
    assert any("company" in line and "empty" in line for line in lines)
