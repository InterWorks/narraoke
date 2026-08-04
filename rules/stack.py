"""The four-tier rule stack.

Rules resolve in four tiers, most specific first:

    1 project   <markdown>.tts-overrides.json, beside the source document
    2 user      ${XDG_CONFIG_HOME:-~/.config}/narraoke/rules.d/*.json
    3 company   a cloned private repo, path set in config.json
    4 universal Python literals, packaged with the app

Precedence is **project -> user -> company -> universal**. Each tier can
shadow the more general ones beneath it; none can override the tier above.

Why user beats company: a personal preference should win on your own machine
without editing a shared repo. Why company beats universal: an organisation's
client pronunciation must override a generic guess.

ORDER IS SEMANTICS
------------------
`_apply_literal_overrides` walks the assembled list with sequential
`str.replace` over a mutating buffer, so "precedence" here means *application
order*, and the failure mode is substring interference rather than a clean
override. `RuleStack.lint()` reports rules that can never fire because an
earlier one consumes their text. It warns and never reorders — the tier-4
built-ins are hand-ordered and their comments are load-bearing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Iterable, Iterator

# Tier names, most specific first. This tuple defines precedence.
TIER_ORDER: tuple[str, ...] = ("project", "user", "company", "universal")

# Where a data-driven regex rule may attach in `rewrite_for_tts`.
#
#   pre_ipa  after the literal pass, before any rule emits an IPA escape.
#            The right place for plain text-shape rewrites.
#   post     after the pattern rules, before per-doc named pronunciations.
#            For rules that must see the output of the built-in passes.
#
# Deliberately only two. Exposing every one of the ~15 internal steps as a
# hook would freeze the pipeline's internals into the public file format, and
# the hand-tuned order is exactly the thing that must stay free to change.
#
# **Scope boundary for `pre_ipa`.** It runs *after* the literal pass, so its
# output reaches the pattern rules (versions, retryable, dotted names) but not
# the literals. A pre_ipa rule emitting "JSON" yields the bare word, not the
# IPA escape, because the literal that would have wrapped it already ran. A
# rule needing the literal treatment should emit the final form itself.
REGEX_STAGES: tuple[str, ...] = ("pre_ipa", "post")

# Regex flags a rule file may request, by name.
#
# An allowlist, not a passthrough: `re.DEBUG` prints to stdout, and flags like
# `re.VERBOSE` change how the pattern parses in ways that make a rule file
# harder to review than to write. These four cover real narration needs.
ALLOWED_REGEX_FLAGS: dict[str, int] = {
    "IGNORECASE": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "DOTALL": re.DOTALL,
    "UNICODE": re.UNICODE,
}


@dataclass(frozen=True)
class LiteralRule:
    """A single `str.replace` rewrite, with provenance.

    `origin` records which tier and file the rule came from, so warnings and
    the startup summary can say *which* tier a rule is from — essential once
    four tiers can interfere with one another.
    """

    frm: str
    to: str
    why: str = ""
    origin: str = ""

    def as_pair(self) -> tuple[str, str]:
        return (self.frm, self.to)


class RegexRuleError(ValueError):
    """A regex rule in a data file is malformed or not permitted."""


@dataclass(frozen=True)
class RegexRule:
    """A pattern rewrite loaded from a rule file.

    **String replacements only — never a Python callable.** This is what makes
    rule files from tiers 2 and 3 safe: a user config directory or a cloned
    company repo is a path by which someone else's file reaches this machine.
    Accepting callables would turn either into an arbitrary-code-execution
    vector. Rules that genuinely need conditional logic stay in tier 4 as
    reviewed Python.

    `re.sub` is still given a plain string, so backreferences (`\\1`, `\\g<name>`)
    work as usual — that covers every rewrite the built-in pattern rules do.
    """

    pattern: str
    replacement: str
    stage: str = "pre_ipa"
    flags: tuple[str, ...] = ()
    why: str = ""
    origin: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.pattern, str) or not self.pattern:
            raise RegexRuleError(f"{self.origin or 'rule'}: pattern must be a non-empty string")
        if not isinstance(self.replacement, str):
            raise RegexRuleError(
                f"{self.origin or 'rule'}: replacement must be a string; "
                f"callables are never accepted from a rule file"
            )
        if self.stage not in REGEX_STAGES:
            raise RegexRuleError(
                f"{self.origin or 'rule'}: unknown stage {self.stage!r}; "
                f"expected one of {REGEX_STAGES}"
            )
        for flag in self.flags:
            if flag not in ALLOWED_REGEX_FLAGS:
                raise RegexRuleError(
                    f"{self.origin or 'rule'}: flag {flag!r} is not permitted; "
                    f"allowed flags are {sorted(ALLOWED_REGEX_FLAGS)}"
                )
        # Compile eagerly so a bad pattern fails at load time, naming its
        # source file, rather than mid-render with an opaque traceback.
        try:
            re.compile(self.pattern, self.flag_bits())
        except re.error as e:
            raise RegexRuleError(
                f"{self.origin or 'rule'}: invalid pattern {self.pattern!r}: {e}"
            ) from e

    def flag_bits(self) -> int:
        bits = 0
        for flag in self.flags:
            bits |= ALLOWED_REGEX_FLAGS[flag]
        return bits

    def compiled(self) -> "re.Pattern[str]":
        return re.compile(self.pattern, self.flag_bits())

    def apply(self, text: str) -> str:
        """Rewrite *text*. A runtime failure degrades to a no-op.

        A malformed replacement template (a backreference to a group the
        pattern does not define) would otherwise abort a 16-minute render over
        one bad line in a config file.
        """
        try:
            return self.compiled().sub(self.replacement, text)
        except re.error:
            return text


@dataclass(frozen=True)
class NamedPronunciation:
    """A proper-noun pronunciation, applied as an IPA escape."""

    name: str
    ipa: str = ""
    hint: str = ""
    why: str = ""
    origin: str = ""

    def as_tuple(self) -> tuple[str, str, str]:
        return (self.name, self.ipa, self.hint)


@dataclass(frozen=True)
class RuleSet:
    """Every rule from one tier, in application order."""

    tier: str
    source: str = ""
    literals: tuple[LiteralRule, ...] = ()
    named: tuple[NamedPronunciation, ...] = ()
    regexes: tuple[RegexRule, ...] = ()

    def __len__(self) -> int:
        return len(self.literals) + len(self.named) + len(self.regexes)

    @property
    def is_empty(self) -> bool:
        return not self.literals and not self.named and not self.regexes


def _empty(tier: str) -> RuleSet:
    return RuleSet(tier=tier)


@dataclass(frozen=True)
class RuleStack:
    """Four ordered RuleSets, resolved by precedence.

    With tiers 2 and 3 empty the assembled output is identical to the
    two-layer behaviour that preceded this refactor — the property the golden
    tests lock down.
    """

    project: RuleSet = field(default_factory=lambda: _empty("project"))
    user: RuleSet = field(default_factory=lambda: _empty("user"))
    company: RuleSet = field(default_factory=lambda: _empty("company"))
    universal: RuleSet = field(default_factory=lambda: _empty("universal"))

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def builtin_only(cls, literals: Iterable[tuple[str, str]]) -> "RuleStack":
        """A stack holding only tier 4, built from `(from, to)` pairs.

        This is the universal-only case used by tests and by any run with no
        override files present.
        """
        return cls(
            universal=RuleSet(
                tier="universal",
                source="rules/",
                literals=tuple(
                    LiteralRule(frm=f, to=t, origin="universal")
                    for f, t in literals
                ),
            )
        )

    def with_tier(self, tier: str, rule_set: RuleSet) -> "RuleStack":
        """Return a copy with one tier replaced. The stack stays immutable."""
        if tier not in TIER_ORDER:
            raise ValueError(f"unknown tier {tier!r}; expected one of {TIER_ORDER}")
        return replace(self, **{tier: rule_set})

    # ── resolution ──────────────────────────────────────────────────────────

    def sets_in_order(self) -> Iterator[RuleSet]:
        """Yield each tier's RuleSet in precedence order."""
        for tier in TIER_ORDER:
            yield getattr(self, tier)

    def ordered_literals(self) -> list[LiteralRule]:
        """Every literal rule, most-specific tier first.

        Application order *is* precedence: an earlier rule consumes the text a
        later one would have matched, so a project rule shadows a universal one
        by running first.
        """
        out: list[LiteralRule] = []
        for rule_set in self.sets_in_order():
            out.extend(rule_set.literals)
        return out

    def literal_pairs(self) -> list[tuple[str, str]]:
        """`ordered_literals` as plain `(from, to)` tuples."""
        return [rule.as_pair() for rule in self.ordered_literals()]

    def ordered_regexes(self, stage: str) -> list[RegexRule]:
        """Every regex rule for *stage*, most-specific tier first.

        Same precedence as literals: a project rule runs before a universal
        one, so it gets first claim on the text.
        """
        if stage not in REGEX_STAGES:
            raise ValueError(f"unknown stage {stage!r}; expected one of {REGEX_STAGES}")
        out: list[RegexRule] = []
        for rule_set in self.sets_in_order():
            out.extend(rule for rule in rule_set.regexes if rule.stage == stage)
        return out

    def apply_regexes(self, text: str, stage: str) -> str:
        """Run every rule for *stage* over *text*, in precedence order."""
        for rule in self.ordered_regexes(stage):
            text = rule.apply(text)
        return text

    def merged_named(self) -> list[NamedPronunciation]:
        """Named pronunciations, most-specific tier winning per name.

        Unlike literals, names merge by identity rather than by application
        order — two tiers defining the same name is a genuine conflict, not a
        sequence. The most specific tier wins.
        """
        seen: dict[str, NamedPronunciation] = {}
        for rule_set in self.sets_in_order():
            for entry in rule_set.named:
                if entry.name not in seen:
                    seen[entry.name] = entry
        return list(seen.values())

    def named_tuples(self) -> list[tuple[str, str, str]]:
        """`merged_named` as plain `(name, ipa, hint)` tuples."""
        return [entry.as_tuple() for entry in self.merged_named()]

    # ── diagnostics ─────────────────────────────────────────────────────────

    def conflicting_names(self) -> list[tuple[str, NamedPronunciation, NamedPronunciation]]:
        """Names defined in more than one tier with *different* IPA.

        Identical definitions in two tiers are harmless duplication. Differing
        ones mean one tier is silently overriding another's pronunciation,
        which an operator should know about.
        """
        first: dict[str, NamedPronunciation] = {}
        conflicts: list[tuple[str, NamedPronunciation, NamedPronunciation]] = []
        for rule_set in self.sets_in_order():
            for entry in rule_set.named:
                prior = first.get(entry.name)
                if prior is None:
                    first[entry.name] = entry
                elif prior.ipa != entry.ipa:
                    conflicts.append((entry.name, prior, entry))
        return conflicts

    def lint(self) -> list[str]:
        """Warn where one rule's `from` is a substring of a later rule's.

        The later rule can never fire: the earlier one already consumed the
        text. This is the failure mode the mutating-buffer design invites, and
        it becomes far easier to hit once rules arrive from four separate
        files.

        Warn only — never auto-reorder. The tier-4 built-ins are hand-ordered
        with load-bearing comments.
        """
        rules = self.ordered_literals()
        messages: list[str] = []
        for i, earlier in enumerate(rules):
            for later in rules[i + 1:]:
                if earlier.frm and earlier.frm in later.frm:
                    messages.append(
                        f"{later.frm!r} ({later.origin or '?'}) can never fire: "
                        f"{earlier.frm!r} ({earlier.origin or '?'}) runs first "
                        f"and consumes it"
                    )
        return messages

    def summary(self) -> list[str]:
        """One line per tier: name, source, and rule count.

        An operator must be able to see at a glance which tiers are live —
        especially that the company tier loaded, since a silently-absent tier-3
        rule means a client name is mispronounced in a delivered video.
        """
        lines: list[str] = []
        for tier in TIER_ORDER:
            rule_set = getattr(self, tier)
            if rule_set.is_empty:
                lines.append(f"  {tier:<10} (empty)")
            else:
                source = rule_set.source or "?"
                parts = [f"{len(rule_set.literals)} literal"]
                if rule_set.regexes:
                    parts.append(f"{len(rule_set.regexes)} regex")
                parts.append(f"{len(rule_set.named)} named")
                lines.append(
                    f"  {tier:<10} {' + '.join(parts)}  from {source}"
                )
        return lines
