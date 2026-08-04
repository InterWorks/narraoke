"""Tests for data-driven regex rules.

The security property these pin down: a rule file may supply a *pattern and a
string replacement*, and nothing else. A user config directory or a cloned
company repo is a path by which someone else's file reaches this machine, so
accepting Python callables would make either an arbitrary-code-execution
vector.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.argv = ["pytest"]

import narraoke as h  # noqa: E402
from rules.stack import (  # noqa: E402
    ALLOWED_REGEX_FLAGS,
    REGEX_STAGES,
    RegexRule,
    RegexRuleError,
    RuleSet,
    RuleStack,
)


def _stack_with(*regexes: RegexRule, tier: str = "user") -> RuleStack:
    return RuleStack.builtin_only(h._LITERAL_TTS_OVERRIDES).with_tier(
        tier, RuleSet(tier=tier, source="<test>", regexes=tuple(regexes))
    )


def _rewrite_with(stack: RuleStack, text: str) -> str:
    previous = h.active_rule_stack()
    try:
        h.set_rule_stack(stack)
        return h.rewrite_for_tts(text)
    finally:
        h.set_rule_stack(previous)


# ── the safety boundary ──────────────────────────────────────────────────────

def test_callable_replacement_is_rejected() -> None:
    """The whole reason rule files are safe to load from a shared repo."""
    with pytest.raises(RegexRuleError, match="callables are never accepted"):
        RegexRule(pattern=r"x", replacement=lambda m: "boom")  # type: ignore[arg-type]


def test_non_string_replacements_are_rejected() -> None:
    for bad in [None, 42, ["a"], {"a": 1}]:
        with pytest.raises(RegexRuleError):
            RegexRule(pattern=r"x", replacement=bad)  # type: ignore[arg-type]


def test_only_allowlisted_flags_are_accepted() -> None:
    """`re.DEBUG` prints to stdout; `re.VERBOSE` changes how a pattern parses.

    An allowlist keeps a rule file reviewable by reading it.
    """
    with pytest.raises(RegexRuleError, match="not permitted"):
        RegexRule(pattern=r"x", replacement="y", flags=("DEBUG",))
    with pytest.raises(RegexRuleError, match="not permitted"):
        RegexRule(pattern=r"x", replacement="y", flags=("VERBOSE",))
    # ... and the permitted ones do work
    rule = RegexRule(pattern=r"x", replacement="y", flags=tuple(ALLOWED_REGEX_FLAGS))
    assert rule.flag_bits()


def test_unknown_stage_is_rejected() -> None:
    with pytest.raises(RegexRuleError, match="unknown stage"):
        RegexRule(pattern=r"x", replacement="y", stage="whenever")


def test_invalid_pattern_fails_at_load_time() -> None:
    """A bad pattern must name its source file, not surface mid-render."""
    with pytest.raises(RegexRuleError, match="invalid pattern"):
        RegexRule(pattern=r"(unclosed", replacement="y", origin="user:bad.json")


def test_empty_pattern_is_rejected() -> None:
    with pytest.raises(RegexRuleError, match="non-empty"):
        RegexRule(pattern="", replacement="y")


# ── behaviour ────────────────────────────────────────────────────────────────

def test_regex_rule_rewrites_text() -> None:
    stack = _stack_with(RegexRule(pattern=r"\bCI/CD\b", replacement="C.I. C.D."))
    assert _rewrite_with(stack, "Our CI/CD pipeline") == "Our C.I. C.D. pipeline"


def test_backreferences_work() -> None:
    """`re.sub` still receives a plain string, so `\\1` behaves normally."""
    stack = _stack_with(RegexRule(pattern=r"\b(\w+)_flag\b", replacement=r"\1 flag"))
    assert _rewrite_with(stack, "The debug_flag is set") == "The debug flag is set"


def test_flags_take_effect() -> None:
    stack = _stack_with(
        RegexRule(pattern=r"\bk8s\b", replacement="kubernetes", flags=("IGNORECASE",))
    )
    assert "kubernetes" in _rewrite_with(stack, "K8S and k8s")


def test_no_regex_rules_is_a_no_op() -> None:
    """Installing the stages changed nothing for anyone without regex rules."""
    bare = RuleStack.builtin_only(h._LITERAL_TTS_OVERRIDES)
    sample = "Send JSON and YAML to the lockfile."
    assert _rewrite_with(bare, sample) == _rewrite_with(_stack_with(), sample)


def test_runtime_failure_degrades_to_no_op() -> None:
    """A bad backreference must not abort a 16-minute render."""
    rule = RegexRule(pattern=r"(a)", replacement=r"\9")
    assert rule.apply("banana") == "banana"


# ── stages ───────────────────────────────────────────────────────────────────

def test_stages_are_exactly_two() -> None:
    """Exposing every internal step would freeze the pipeline's order into
    the public file format."""
    assert REGEX_STAGES == ("pre_ipa", "post")


def test_pre_ipa_output_feeds_the_builtin_pattern_rules() -> None:
    """A pre_ipa rule runs early enough that the pattern rules see its output.

    Here the rule emits "retryable", which `_fix_retryable` then wraps in IPA.
    """
    stack = _stack_with(RegexRule(pattern=r"\bRA\b", replacement="retryable"))
    out = _rewrite_with(stack, "The RA flag is set.")
    assert "ɹitɹˈaɪəbəl" in out, "pre_ipa output should reach _fix_retryable"


def test_pre_ipa_output_does_not_reach_the_literal_pass() -> None:
    """Scope boundary worth knowing: literals run BEFORE pre_ipa.

    A pre_ipa rule that emits text a *literal* rule would have matched does
    not get that literal applied — the literal pass has already run. Emitting
    "JSON" therefore yields the bare word, not the IPA escape.

    This is not a defect; it is what "pre_ipa" means. A rule needing the
    literal treatment should emit the final form itself.
    """
    stack = _stack_with(RegexRule(pattern=r"\bJSObj\b", replacement="JSON"))
    out = _rewrite_with(stack, "Send JSObj now.")
    assert out == "Send JSON now."
    assert "ʤˈeɪsˌɑn" not in out


def test_post_runs_after_builtin_rules() -> None:
    """A post rule sees the built-ins' output, including their IPA escapes."""
    stack = _stack_with(
        RegexRule(pattern=r"lock file", replacement="LOCKFILE", stage="post")
    )
    assert "LOCKFILE" in _rewrite_with(stack, "Check the lockfile.")


def test_ordered_regexes_filters_by_stage() -> None:
    stack = _stack_with(
        RegexRule(pattern=r"a", replacement="A", stage="pre_ipa"),
        RegexRule(pattern=r"b", replacement="B", stage="post"),
    )
    assert len(stack.ordered_regexes("pre_ipa")) == 1
    assert len(stack.ordered_regexes("post")) == 1


def test_ordered_regexes_rejects_unknown_stage() -> None:
    with pytest.raises(ValueError, match="unknown stage"):
        RuleStack().ordered_regexes("nope")


def test_regex_precedence_follows_tier_order() -> None:
    """Most-specific tier runs first, same as literals."""
    stack = (
        RuleStack.builtin_only([])
        .with_tier("project", RuleSet(tier="project", regexes=(
            RegexRule(pattern=r"\bTOKEN\b", replacement="project", origin="project"),)))
        .with_tier("company", RuleSet(tier="company", regexes=(
            RegexRule(pattern=r"\bTOKEN\b", replacement="company", origin="company"),)))
    )
    assert stack.apply_regexes("TOKEN", "pre_ipa") == "project"


# ── loading from a file ──────────────────────────────────────────────────────

def test_regex_section_loads_from_a_rule_file(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({
        "regex": [
            {"pattern": r"\bCI/CD\b", "replacement": "C.I. C.D.",
             "stage": "pre_ipa", "why": "initialism pair"},
            {"pattern": r"\bk8s\b", "replacement": "kubernetes",
             "flags": ["IGNORECASE"]},
        ]
    }), encoding="utf-8")
    rule_set = h.load_rule_file(path, tier="company")
    assert len(rule_set.regexes) == 2
    assert rule_set.regexes[0].why == "initialism pair"
    assert rule_set.regexes[0].origin.startswith("company:")


def test_a_single_bad_rule_does_not_lose_the_good_ones(tmp_path: Path) -> None:
    """One malformed entry must not cost the whole file."""
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({
        "regex": [
            {"pattern": r"(unclosed", "replacement": "x"},
            {"pattern": r"\bok\b", "replacement": "fine"},
        ]
    }), encoding="utf-8")
    rule_set = h.load_rule_file(path, tier="user")
    assert len(rule_set.regexes) == 1
    assert rule_set.regexes[0].replacement == "fine"


def test_flags_accepts_a_bare_string(tmp_path: Path) -> None:
    """`"flags": "IGNORECASE"` is the obvious mistake; accept it."""
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({
        "regex": [{"pattern": "x", "replacement": "y", "flags": "IGNORECASE"}]
    }), encoding="utf-8")
    assert h.load_rule_file(path, tier="user").regexes[0].flags == ("IGNORECASE",)


def test_regexes_survive_directory_composition(tmp_path: Path) -> None:
    """Regression: `_load_rules_from_dir` once dropped regexes silently.

    Unit tests that build a RuleSet directly cannot catch this — the rules
    loaded fine from a single file and vanished only when composed from a
    directory, which is how tiers 2 and 3 actually load.
    """
    directory = tmp_path / "rules.d"
    directory.mkdir()
    (directory / "10-a.json").write_text(json.dumps({
        "regex": [{"pattern": r"\bA\b", "replacement": "alpha"}],
        "literal": [{"from": "L", "to": "ell"}],
    }), encoding="utf-8")
    (directory / "20-b.json").write_text(json.dumps({
        "regex": [{"pattern": r"\bB\b", "replacement": "bravo"}],
    }), encoding="utf-8")

    rule_set = h._load_rules_from_dir(directory, "user")
    assert len(rule_set.regexes) == 2, "regexes lost during directory composition"
    assert len(rule_set.literals) == 1
    # ... and they compose in sorted filename order
    assert [r.replacement for r in rule_set.regexes] == ["alpha", "bravo"]


def test_summary_reports_regex_counts() -> None:
    """An operator must see that regex rules loaded, not just literals."""
    stack = _stack_with(RegexRule(pattern=r"x", replacement="y"))
    line = next(l for l in stack.summary() if l.strip().startswith("user"))
    assert "1 regex" in line


def test_files_without_a_regex_section_still_load(tmp_path: Path) -> None:
    """Backward compatible in both directions — the loader ignores absence."""
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({
        "literal": [{"from": "A", "to": "alpha"}]
    }), encoding="utf-8")
    rule_set = h.load_rule_file(path, tier="project")
    assert rule_set.regexes == ()
    assert len(rule_set.literals) == 1
