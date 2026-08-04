"""Migration equivalence: moving a rule between tiers must not change audio.

The assertion is

    rewrite(text, project=pre,  company=EMPTY)
      == rewrite(text, project=post, company=company)

which proves the migration is a no-op *without* recording any expected output,
so it cannot rot the way a golden file can.

**The fixtures are redacted.** The five tier-3a rules are replaced by
placeholders, because these files live in a public repository. The
placeholders preserve the shape of the migration — same rule count, same
tiers, same ordering — which is all the equivalence argument needs. The real
rules live in the private company-rules repo.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.argv = ["pytest"]

import html_to_video as h  # noqa: E402
from rules.stack import RuleStack  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "migration"
PRE = FIXTURES / "pre.tts-overrides.json"
POST = FIXTURES / "post.tts-overrides.json"
COMPANY = FIXTURES / "company.json"

# Text exercising the migrated rules plus ordinary prose around them.
SAMPLES = [
    "Ask in #redacted-channel-a or #redacted-channel-b for help.",
    "Email redacted-contact@example.com to report it.",
    "The Redacted Product suite is maintained by Redactedco.",
    "Set REDACTED_TOKEN_NAME in the environment.",
    "A CVE was filed and 2FA is required.",
    "Check the README and follow SemVer.",
    "Vacation-hotel-room-you will thank past-you.",
    "Upgrade to 4.28.1 from the 4.x line.",
    "Plain prose with no rules at all should pass through.",
]


def _load(path: Path, tier: str):
    return h.load_rule_file(path, tier=tier)


def _rewrite_with(stack: RuleStack, text: str) -> str:
    """Run the real pipeline against an explicit stack, then restore."""
    previous = h.active_rule_stack()
    try:
        h.set_rule_stack(stack)
        return h.rewrite_for_tts(text)
    finally:
        h.set_rule_stack(previous)


def _pre_stack() -> RuleStack:
    """Everything in tier 1, company empty — the state before migration."""
    return RuleStack.builtin_only(h._LITERAL_TTS_OVERRIDES).with_tier(
        "project", _load(PRE, "project")
    )


def _post_stack() -> RuleStack:
    """Rules split across tier 1 and tier 3 — the state after migration."""
    return (
        RuleStack.builtin_only(h._LITERAL_TTS_OVERRIDES)
        .with_tier("project", _load(POST, "project"))
        .with_tier("company", _load(COMPANY, "company"))
    )


@pytest.mark.parametrize("text", SAMPLES, ids=lambda s: s[:32])
def test_migration_is_equivalent(text: str) -> None:
    """Splitting the rules across tiers produces identical narration."""
    assert _rewrite_with(_pre_stack(), text) == _rewrite_with(_post_stack(), text)


def test_fixtures_reconcile() -> None:
    """No rule is lost or duplicated by the split.

    post + company + the two promoted to tier 4 must equal pre.
    """
    def literals(path: Path) -> list[str]:
        raw = re.sub(r"^\s*//.*$", "", path.read_text(encoding="utf-8"), flags=re.M)
        return [e["from"] for e in json.loads(raw)["literal"]]

    pre, post, company = literals(PRE), literals(POST), literals(COMPANY)
    promoted_to_universal = {"README", "SemVer"}

    assert len(post) + len(company) + len(promoted_to_universal) == len(pre)
    assert set(post) | set(company) | promoted_to_universal == set(pre)
    # A rule must land in exactly one destination
    assert not (set(post) & set(company))


def test_company_tier_actually_fires_after_migration() -> None:
    """Guard against the migration "passing" because nothing loaded.

    If the company file silently failed to load, the equivalence test would
    still pass for any text that happens not to use those rules. This asserts
    the tier-3 rules genuinely take effect.
    """
    stack = _post_stack()
    assert not stack.company.is_empty
    out = _rewrite_with(stack, "Ask in #redacted-channel-a today.")
    assert "#redacted-channel-a" not in out, "company rule did not fire"


def test_post_migration_project_file_has_no_company_rules() -> None:
    """After migration the project file must not still carry tier-3 rules.

    Leaving a duplicate behind would shadow the company copy — the rule would
    keep working, so only the lint would notice.
    """
    stack = _post_stack()
    project_froms = {rule.frm for rule in stack.project.literals}
    company_froms = {rule.frm for rule in stack.company.literals}
    assert not (project_froms & company_froms)


def test_no_shadowing_after_migration() -> None:
    """The migrated stack must be lint-clean.

    Before migration the same rules exist in both tiers and the company copies
    are dead; afterwards every rule can fire.
    """
    assert _post_stack().lint() == []
