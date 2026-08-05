"""Golden-file tests for the TTS rule pipeline.

`rewrite_for_tts` is a pure str -> str function with no I/O, so the entire
rule system is testable in under a second with no render. A full render is
~16 minutes; never re-render to check a string rewrite.

Why this suite exists
---------------------
The rule lists are hand-ordered and the order is *semantics*, not style:
`_apply_literal_overrides` is a sequential `str.replace` over a mutating
buffer, so a rule whose `from` is a substring of a later rule's `from` will
consume it first. The inline comments in narraoke.py ("longer first",
"Order matters") are load-bearing assertions that had no test behind them.
These tests are that missing check.

The golden files are the no-op guard for the four-tier refactor: moving a
rule between tiers must produce byte-identical output. Land this suite
before moving any rule.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.argv = ["pytest"]

import narraoke as h  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def _rewrite_document(text: str) -> str:
    """Apply the pipeline line by line, mirroring regenerate_golden.py."""
    return "".join(
        h.rewrite_for_tts(line) if line.strip() else line
        for line in text.splitlines(keepends=True)
    )


def _fixture_pairs() -> list[tuple[Path, Path]]:
    pairs = []
    for in_path in sorted(FIXTURES.glob("*.in.txt")):
        out_path = in_path.with_name(in_path.name.replace(".in.txt", ".out.txt"))
        pairs.append((in_path, out_path))
    return pairs


# ── Golden-file no-op guard ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "in_path,out_path",
    _fixture_pairs(),
    ids=lambda p: p.name if isinstance(p, Path) else str(p),
)
def test_golden_output_is_byte_identical(in_path: Path, out_path: Path) -> None:
    """Each fixture must rewrite to exactly its recorded output.

    A failure here means the rule pipeline changed behaviour. That is either
    a regression or a deliberate improvement — read the diff and decide.
    Do not regenerate the golden files to make this pass.
    """
    assert out_path.exists(), (
        f"Missing golden file {out_path.name}. "
        f"Generate it with: uv run python tests/regenerate_golden.py"
    )
    expected = out_path.read_text(encoding="utf-8")
    actual = _rewrite_document(in_path.read_text(encoding="utf-8"))
    assert actual == expected


def test_fixture_corpus_is_not_empty() -> None:
    """Guard against the suite silently passing with zero fixtures."""
    assert _fixture_pairs(), "No .in.txt fixtures found — the suite is vacuous."


# ── Purity: the property the whole test strategy depends on ──────────────────

def test_rewrite_is_pure_and_repeatable() -> None:
    """Same input, same output, every time — no hidden state between calls."""
    sample = "Send JSON and YAML to the lockfile."
    first = h.rewrite_for_tts(sample)
    for _ in range(5):
        assert h.rewrite_for_tts(sample) == first


def test_rewrite_does_not_mutate_input() -> None:
    sample = "The SHA of the lockfile is recorded."
    original = str(sample)
    h.rewrite_for_tts(sample)
    assert sample == original


def test_empty_and_whitespace_are_safe() -> None:
    assert h.rewrite_for_tts("") == ""
    assert h.rewrite_for_tts("   ") == "   "


# ── Ordering constraints (the load-bearing inline comments) ──────────────────

def test_longer_dotted_config_name_wins() -> None:
    """`~/.claude.json` must match before the bare `.claude.json`.

    Guards the "longer first" comment in _LITERAL_TTS_OVERRIDES. If the
    shorter rule ran first it would consume the tail of the longer one,
    leaving a stray "~/" fragment in the narration.
    """
    out = h.rewrite_for_tts("Edit ~/.claude.json now.")
    assert "dot claude dot json" in out
    assert "~/dot claude" not in out, "shorter rule consumed the longer one"


def test_retryability_wins_over_retryable() -> None:
    """Longer noun form first, so the bare rule can't match inside it."""
    out = h.rewrite_for_tts("Retryability matters.")
    assert "ɹitɹˌaɪəbˈɪlɪti" in out, "expected the retryability IPA, not retryable's"


def test_invalid_json_wins_over_bare_json() -> None:
    """`Invalid JSON` is listed before `JSON`; the compound must win."""
    out = h.rewrite_for_tts("An Invalid JSON payload.")
    assert "ɪnvˈælɪd" in out, "compound rule did not fire"


def test_regex_rules_do_not_double_wrap_ipa() -> None:
    """The (?!\\]\\(/) guards stop an already-wrapped span being rewrapped.

    Scope note — this holds for the *regex* rules, which carry the negative
    lookahead, and NOT for the plain literals in _LITERAL_TTS_OVERRIDES,
    which are applied with `str.replace` and have no such guard. Feeding
    output back in would rewrap `JSON` as `[[JSON](/ipa/)](/ipa/)`.

    That is not a live defect: rewrite_for_tts is called once per phrase and
    never on its own output. The assertion is deliberately limited to the
    guarded rules so it tests the guards rather than a property the pipeline
    was never designed to have.
    """
    for fn, sample in [
        (h._fix_retryable, "A retryable error occurred."),
        (h._fix_transient, "A transient failure occurred."),
        (h._fix_enum, "The enum value is set."),
        (h._fix_copied, "The file was copied."),
        (h._force_verb_stress_heteronyms, "The delegates delegate work."),
    ]:
        once = fn(sample)
        twice = fn(once)
        assert once == twice, (
            f"{fn.__name__} rewrapped its own output — the (?!\\]\\(/) guard "
            f"is missing or ineffective:\n  once:  {once!r}\n  twice: {twice!r}"
        )


def test_copied_gets_a_single_final_syllable() -> None:
    """Kokoro splits "-ied" into "cop-ih-ed"; the escape forces "COP-eed"."""
    out = h.rewrite_for_tts("The file was copied to the cache.")
    assert "[copied](/kˈɒpid/)" in out


def test_copied_is_word_bounded_so_prefixed_forms_survive() -> None:
    """The boundary is load-bearing, not stylistic.

    This rule started life as a plain literal, which rewrote "uncopied" to
    "un[copied](/…/)". misaki phonemizes a mid-word escape to nothing, so the
    literal form silently destroyed the word instead of fixing it. Kokoro
    reads the prefixed forms correctly on its own, so they are left alone.
    """
    for word in ("uncopied", "recopied"):
        assert h.rewrite_for_tts(word) == word


def test_copied_does_not_touch_other_inflections() -> None:
    """Only the "-ied" form is defective; "copies"/"copy" are already right."""
    for word in ("copies", "copy", "copying"):
        assert h.rewrite_for_tts(word) == word


def test_ranges_expand_inside_quotes() -> None:
    """Range expansion runs before quote wrapping, per the docstring.

    '30-45' inside a quoted span must still become '30 to 45'.
    """
    out = h.rewrite_for_tts('The setting "read lines 30-45" applies.')
    assert "30 to 45" in out, "range did not expand inside a quoted span"


def test_dotfile_anchoring_leaves_compounds_alone() -> None:
    """`.env` narrates as a dotfile; `/foo.env` and `.envrc` do not.

    Note the division of labour, which the inline comments do not spell out:
    `_spell_out_dotfiles` is correctly anchored and leaves `/foo.env` alone,
    but `_spell_out_dotted_names` runs afterwards and narrates it as an
    ordinary dotted name ("/foo dot env"). Only `.envrc` survives both.

    The comment at narraoke.py:884 claims `/foo.env` "stays untouched",
    which is true of that rule in isolation and false of the pipeline. See
    the note in docs/PLAN.md follow-ups; recorded here as current behaviour.
    """
    assert "dot E N V" in h.rewrite_for_tts("Copy .env into place.")
    # The dotfile rule itself is properly anchored ...
    assert h._spell_out_dotfiles("/foo.env") == "/foo.env"
    assert h._spell_out_dotfiles(".envrc") == ".envrc"
    # ... and `.envrc` survives the whole pipeline.
    out = h.rewrite_for_tts("Compounds like .envrc stay untouched.")
    assert ".envrc" in out, "a compound dotfile was rewritten"


# ── Rule-list integrity ──────────────────────────────────────────────────────

def test_literal_rules_have_no_exact_duplicates() -> None:
    """A duplicated `from` means the second entry is dead code."""
    froms = [frm for frm, _ in h._LITERAL_TTS_OVERRIDES]
    duplicates = {f for f in froms if froms.count(f) > 1}
    assert not duplicates, f"duplicate literal rules: {sorted(duplicates)}"


def test_versions_narrate_dots_generically() -> None:
    """Any 3+ component version must narrate, not just enumerated ones.

    Replaces three built-in literals plus sixteen hand-written rules in one
    document's override file — none of which covered a version string nobody
    had typed out in advance.
    """
    assert h.rewrite_for_tts("1.2.3") == "one dot 2 dot 3"
    assert h.rewrite_for_tts("2.0.0") == "two dot 0 dot 0"
    assert h.rewrite_for_tts("3.11.4") == "three dot 11 dot 4"
    assert h.rewrite_for_tts("10.20.30") == "10 dot 20 dot 30"


def test_version_leading_single_digit_is_spelled_out() -> None:
    """Kokoro reads a bare leading "4" as the homophone "for".

    Spelling the first component fixes that; multi-digit components stay as
    numerals so long versions do not become a wall of words.
    """
    assert h.rewrite_for_tts("4.28.1") == "four dot 28 dot 1"
    assert h.rewrite_for_tts("0.9.1") == "zero dot 9 dot 1"
    # Two-digit leading component keeps its numeral form
    assert h.rewrite_for_tts("10.20.30").startswith("10 dot")


def test_version_prefix_and_suffixes() -> None:
    """`v` prefix and pre-release / build suffixes narrate as words."""
    assert h.rewrite_for_tts("v1.2.3") == "version one dot 2 dot 3"
    assert h.rewrite_for_tts("1.0.0-rc.1") == "one dot 0 dot 0 R.C. one"
    assert h.rewrite_for_tts("1.0.0-alpha") == "one dot 0 dot 0 alpha"
    assert h.rewrite_for_tts("1.0.0+build.27") == "one dot 0 dot 0 plus build 27"


def test_hidden_dotted_configs_narrate_generically() -> None:
    """Hidden config files must narrate whether or not anyone enumerated them.

    Replaces three literals that only ever matched themselves. The non-hidden
    case was already generic; hidden files fell through because the dotted-name
    rule's left boundary rejects a leading dot.
    """
    # The three former literals, byte-identical
    assert h.rewrite_for_tts(".claude.json") == "dot claude dot json"
    assert h.rewrite_for_tts(".mcp.json") == "dot mcp dot json"
    assert h.rewrite_for_tts("~/.claude.json") == "home dot claude dot json"
    # ... and files nobody hardcoded
    assert h.rewrite_for_tts(".eslintrc.json") == "dot eslintrc dot json"
    assert h.rewrite_for_tts(".prettierrc.json") == "dot prettierrc dot json"


def test_home_prefix_is_audibly_distinct() -> None:
    """`~/` narrates as "home", so the two paths do not sound identical.

    A document contrasting the home-directory config with the project-local
    one relies on this distinction being audible.
    """
    home = h.rewrite_for_tts("~/.claude.json")
    local = h.rewrite_for_tts(".claude.json")
    assert home != local
    assert home.startswith("home ")


def test_bare_dotfiles_still_need_explicit_entries() -> None:
    """`.npmrc` has no internal dot, so the generic rule must not claim it.

    Bare dotfiles stay in DOTFILE_NARRATION on purpose: a rule matching any
    ".word" would fire on sentence fragments and abbreviations.
    """
    assert h.rewrite_for_tts(".env") == "dot E N V"
    assert h.rewrite_for_tts(".gitignore") == "dot git ignore"
    for untouched in [".npmrc", ".editorconfig", ".envrc"]:
        assert h.rewrite_for_tts(untouched) == untouched


def test_wildcard_versions_narrate_the_dot() -> None:
    """`4.x` must narrate as "four dot x", including multi-level forms.

    Two components suffice here — unlike the plain version rule — because a
    trailing "x" can never be a decimal fraction.
    """
    assert h.rewrite_for_tts("4.x") == "four dot x"
    assert h.rewrite_for_tts("10.x") == "10 dot x"
    assert h.rewrite_for_tts("4.2.x") == "four dot 2 dot x"
    assert h.rewrite_for_tts("v4.x") == "version four dot x"
    # Uppercase narrates as lowercase — both are spoken "ex".
    assert h.rewrite_for_tts("4.X") == "four dot x"


def test_decimals_are_never_treated_as_wildcard_versions() -> None:
    """`4.0` is indistinguishable from "four point zero" — leave it alone.

    This is the boundary that makes the wildcard rule safe at two components
    where the plain version rule needs three.
    """
    for decimal in ["4.0", "1.5", "3.14", "0.9"]:
        assert h.rewrite_for_tts(decimal) == decimal


def test_wildcard_rule_rejects_non_version_shapes() -> None:
    """The anchors must keep the rule off filenames and longer tokens."""
    for untouched in ["4.xyz", "x.4", "4.X1"]:
        assert h.rewrite_for_tts(untouched) == untouched


def test_two_part_numbers_are_not_treated_as_versions() -> None:
    """Requiring 3+ components keeps ordinary prose out of the rule.

    "section 1.2" and "Python 3.11" are far more often prose than version
    strings, so the two-part shape is deliberately excluded.
    """
    assert h.rewrite_for_tts("See section 1.2 for details.") == (
        "See section 1.2 for details."
    )
    assert h.rewrite_for_tts("Python 3.11 is required.") == (
        "Python 3.11 is required."
    )


def test_version_rule_does_not_shadow_the_timestamp_pattern() -> None:
    """`_VERSION_RE` is the output-directory timestamp, not the semver rule.

    The semver pattern is `_SEMVER_RE` precisely because reusing `_VERSION_RE`
    silently shadowed the timestamp matcher and broke --skip-tts version-
    directory scanning. This test pins the distinction.
    """
    assert h._VERSION_RE.match("2026-08-03T14-44-05")
    assert not h._VERSION_RE.match("1.2.3")
    assert h._SEMVER_RE.search("1.2.3")


def test_assignments_narrate_the_equals_sign() -> None:
    """`LEFT=RIGHT` must narrate as "LEFT equals RIGHT".

    Kokoro drops `=` entirely, so the assignment — the whole meaning of the
    token — is otherwise inaudible. Generalises a former literal that only
    covered the `KEY=value` placeholder from one document.
    """
    assert h.rewrite_for_tts("KEY=value") == "KEY equals value"
    assert h.rewrite_for_tts("CLAUDE_HEADLESS=true") == "CLAUDE_HEADLESS equals true"
    assert h.rewrite_for_tts("DEBUG=true") == "DEBUG equals true"
    # CLI flags keep their leading dashes
    assert h.rewrite_for_tts("--voice=af_heart") == "--voice equals af_heart"


def test_assignments_leave_operators_alone() -> None:
    """Comparisons and compound assignments must never be rewritten.

    This is the rule's main hazard: `=` appears inside `==`, `!=`, `<=`, `>=`
    and every compound assignment. A false positive here would narrate real
    code as nonsense.
    """
    for expr in [
        "a==b", "x!=y", "i<=10", "n>=3",
        "a+=1", "x-=2", "y*=2", "z/=4", "m%=3", "b^=1", "c&=2", "d|=4",
    ]:
        assert h.rewrite_for_tts(expr) == expr, f"operator rewritten: {expr!r}"


def test_spaced_equals_is_left_as_prose() -> None:
    """`a = b` with spaces already reads correctly; don't touch it."""
    assert h.rewrite_for_tts("a = b") == "a = b"


def test_id_suffix_generalises_beyond_the_original_four() -> None:
    """`\\b(\\w+)_id\\b` replaced a hardcoded list of four `*_id` names.

    The four originals must narrate exactly as before, and identifiers no
    source document has used must get the same treatment — that generality is
    the entire point of promoting the enumeration to a pattern.
    """
    for original in ["custom_id", "customer_id", "order_id", "session_id"]:
        word = original[: -len("_id")]
        assert h.rewrite_for_tts(original) == f"{word} I.D."
    for novel in ["tenant_id", "user_id", "correlation_id"]:
        word = novel[: -len("_id")]
        assert h.rewrite_for_tts(novel) == f"{word} I.D."


def test_id_suffix_does_not_over_fire() -> None:
    """The anchors must keep the rule off text that merely contains "id"."""
    for untouched in ["_id", "some_idea", "identity", "rapid_ideas"]:
        assert h.rewrite_for_tts(untouched) == untouched


def test_rule_modules_assemble_in_declared_order() -> None:
    """The flat list must come from ORDERED_RULE_SOURCES, not import order.

    Guards the invariant that makes the rules/ package safe to reorganise:
    rule precedence is stated in one visible list, so a linter that sorts the
    imports in rules/__init__.py cannot change generated audio.
    """
    import rules

    expected: list[tuple[str, str]] = []
    for name in rules.ORDERED_RULE_SOURCES:
        expected.extend(rules._MODULES[name].LITERALS)
    assert rules.LITERAL_TTS_OVERRIDES == expected
    assert h._LITERAL_TTS_OVERRIDES == expected


def test_every_rule_module_is_assembled() -> None:
    """A module added to rules/ but left out of ORDERED_RULE_SOURCES is dead.

    Its rules would silently never fire — the failure mode this test exists
    to make loud.
    """
    import rules

    assert set(rules.ORDERED_RULE_SOURCES) == set(rules._MODULES), (
        "ORDERED_RULE_SOURCES and _MODULES disagree; a rule module is either "
        "unassembled (its rules never fire) or named but missing."
    )

    # Every literal-bearing module in rules/ must be assembled. `passes` and
    # the infrastructure modules carry no LITERALS and are exempt — but the
    # check is on the *directory*, so a new rule module left out of
    # ORDERED_RULE_SOURCES is caught rather than merely being absent from
    # _MODULES too.
    import importlib

    on_disk = {
        p.stem for p in Path(rules.__file__).parent.glob("*.py")
        if p.stem not in ("__init__", "stack", "discovery", "passes")
    }
    literal_bearing = {
        name for name in on_disk
        if hasattr(importlib.import_module(f"rules.{name}"), "LITERALS")
    }
    assert literal_bearing <= set(rules.ORDERED_RULE_SOURCES), (
        "a literal-bearing module in rules/ is missing from "
        f"ORDERED_RULE_SOURCES, so its rules never fire: "
        f"{sorted(literal_bearing - set(rules.ORDERED_RULE_SOURCES))}"
    )


def test_interacting_rules_share_a_module() -> None:
    """No substring interaction may cross a module boundary.

    Within a module, order is visible in the list. Across modules it would
    depend on ORDERED_RULE_SOURCES, making the constraint remote from the
    rules it governs. Keeping interacting rules together is what lets the
    modules be reordered safely.
    """
    import rules

    owner = {
        frm: name
        for name in rules.ORDERED_RULE_SOURCES
        for frm, _ in rules._MODULES[name].LITERALS
    }
    crossings = [
        (a, owner[a], b, owner[b])
        for a in owner
        for b in owner
        if a != b and a in b and owner[a] != owner[b]
    ]
    assert not crossings, (
        "substring-interacting rules split across modules — move them into "
        f"the same module: {crossings}"
    )


def test_substring_interference_is_ordered_longest_first() -> None:
    """Report literals shadowed by an earlier rule containing them.

    This is the failure mode the mutating-buffer design invites: if rule A
    appears before rule B and A's `from` is a substring of B's `from`, then
    B can never fire. Ordering, not the presence of overlap, is the bug.

    Warn-only by design — the built-ins are hand-ordered and this test
    documents the current, correct state rather than enforcing a sort.
    """
    froms = [frm for frm, _ in h._LITERAL_TTS_OVERRIDES]
    shadowed = []
    for i, earlier in enumerate(froms):
        for later in froms[i + 1:]:
            if earlier in later:
                shadowed.append((earlier, later))
    assert not shadowed, (
        "these literals can never fire — an earlier rule consumes them first: "
        f"{shadowed}"
    )
