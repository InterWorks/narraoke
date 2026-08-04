"""Tests for tier-2 and tier-3 rule discovery.

The precedence chain is flag -> env var -> config.json -> default, and the two
tiers deliberately fail differently: an unconfigured company tier is silent,
but a *configured but missing* one is fatal.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.argv = ["pytest"]

import html_to_video as h  # noqa: E402
from rules import discovery as d  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Never let the developer's own environment leak into these tests."""
    monkeypatch.delenv(d.ENV_COMPANY_RULES, raising=False)
    monkeypatch.delenv(d.ENV_USER_RULES, raising=False)


def _rule_dir(tmp_path: Path, name: str, rules: list[dict]) -> Path:
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "10-rules.json").write_text(
        json.dumps({"literal": rules}), encoding="utf-8"
    )
    return directory


# ── company tier: the loud-failure contract ──────────────────────────────────

def test_unconfigured_company_tier_is_silently_empty() -> None:
    assert d.resolve_company_rules_dir(None, {}) is None


def test_configured_but_missing_company_dir_raises(tmp_path: Path) -> None:
    """A silently-absent NDA rule mispronounces a client name in a delivered
    video, so this must fail rather than warn."""
    missing = tmp_path / "not-here"
    with pytest.raises(d.CompanyRulesMissing, match="does not exist"):
        d.resolve_company_rules_dir(str(missing), {})


def test_company_flag_beats_env_beats_config(tmp_path: Path, monkeypatch) -> None:
    flag_dir = _rule_dir(tmp_path, "from-flag", [])
    env_dir = _rule_dir(tmp_path, "from-env", [])
    cfg_dir = _rule_dir(tmp_path, "from-config", [])
    monkeypatch.setenv(d.ENV_COMPANY_RULES, str(env_dir))
    config = {"company_rules_dir": str(cfg_dir)}

    assert d.resolve_company_rules_dir(str(flag_dir), config) == flag_dir
    assert d.resolve_company_rules_dir(None, config) == env_dir
    monkeypatch.delenv(d.ENV_COMPANY_RULES)
    assert d.resolve_company_rules_dir(None, config) == cfg_dir


def test_company_env_var_missing_dir_also_raises(tmp_path: Path, monkeypatch) -> None:
    """The loud failure applies however the path was configured."""
    monkeypatch.setenv(d.ENV_COMPANY_RULES, str(tmp_path / "gone"))
    with pytest.raises(d.CompanyRulesMissing):
        d.resolve_company_rules_dir(None, {})


# ── user tier: absent is always normal ───────────────────────────────────────

def test_missing_user_dir_is_silent(tmp_path: Path) -> None:
    """Tier 2 holds personal preferences; most machines will not have one."""
    assert d.resolve_user_rules_dir(str(tmp_path / "nope"), {}) is None


def test_user_flag_beats_env(tmp_path: Path, monkeypatch) -> None:
    flag_dir = _rule_dir(tmp_path, "user-flag", [])
    env_dir = _rule_dir(tmp_path, "user-env", [])
    monkeypatch.setenv(d.ENV_USER_RULES, str(env_dir))
    assert d.resolve_user_rules_dir(str(flag_dir), {}) == flag_dir
    assert d.resolve_user_rules_dir(None, {}) == env_dir


def test_user_dir_expands_tilde_and_env_vars(tmp_path: Path, monkeypatch) -> None:
    target = _rule_dir(tmp_path, "expanded", [])
    monkeypatch.setenv("NARRAOKE_TEST_ROOT", str(tmp_path))
    assert d.resolve_user_rules_dir("$NARRAOKE_TEST_ROOT/expanded", {}) == target


# ── file composition ─────────────────────────────────────────────────────────

def test_rule_files_apply_in_sorted_order(tmp_path: Path) -> None:
    """Sorted so a 10-/20- prefix convention fixes application order.

    Rule order is semantics, so it must never depend on filesystem order.
    """
    directory = tmp_path / "rules"
    directory.mkdir()
    for name in ["20-second.json", "10-first.json", "30-third.json"]:
        (directory / name).write_text('{"literal": []}', encoding="utf-8")
    names = [p.name for p in d.rule_files_in(directory)]
    assert names == ["10-first.json", "20-second.json", "30-third.json"]


def test_directory_composes_into_one_ruleset(tmp_path: Path) -> None:
    directory = tmp_path / "company"
    directory.mkdir()
    (directory / "10-a.json").write_text(
        json.dumps({"literal": [{"from": "A", "to": "alpha"}]}), encoding="utf-8"
    )
    (directory / "20-b.json").write_text(
        json.dumps({"literal": [{"from": "B", "to": "bravo"}]}), encoding="utf-8"
    )
    rule_set = h._load_rules_from_dir(directory, "company")
    assert [r.frm for r in rule_set.literals] == ["A", "B"]
    assert rule_set.tier == "company"


# ── the whole stack ──────────────────────────────────────────────────────────

def test_build_stack_with_empty_tiers_matches_builtin_only(tmp_path: Path) -> None:
    """Installing the tier machinery changed nothing while 2 and 3 are empty.

    This is the property that let stages 1 and 2 land before any rule moved.
    """
    from rules.stack import RuleStack

    built = h.build_rule_stack()
    baseline = RuleStack.builtin_only(h._LITERAL_TTS_OVERRIDES)
    assert built.literal_pairs() == baseline.literal_pairs()


def test_company_rules_are_shadowed_by_project_duplicates(tmp_path: Path) -> None:
    """A duplicate in a more specific tier makes the company rule dead.

    This is the pre-migration state the lint is meant to surface: the same
    rule in both tier 1 and tier 3 means the tier-3 copy can never fire.
    """
    company = _rule_dir(tmp_path, "company", [{"from": "ACME", "to": "company"}])
    project = tmp_path / "doc.md.tts-overrides.json"
    project.write_text(
        json.dumps({"literal": [{"from": "ACME", "to": "project"}]}), encoding="utf-8"
    )
    stack = h.build_rule_stack(project_path=project, company_rules=str(company))
    messages = stack.lint()
    assert any("can never fire" in m for m in messages)

    text = "ACME"
    for frm, to in stack.literal_pairs():
        text = text.replace(frm, to)
    assert text == "project"
