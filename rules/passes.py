"""Tier-4 pattern passes: word-level rewrites that need real Python.

Most tier-4 rules are data — a `from`/`to` pair in one of the sibling modules,
applied by `str.replace`. A pass belongs *here* instead when it needs
something a literal cannot express:

  * a word boundary — "copied" must not fire inside "uncopied"
  * a conditional replacement — "enum" and "enums" take different IPA
  * a guard against re-processing text an earlier pass produced

These stay Python for the same reason all of tier 4 does: they require a code
edit and review, so they cannot become an execution path for a config file
(see the module docstring in `rules/__init__.py`). Tiers 1-3 remain
string-replacement only.

ORDER IS SEMANTICS
------------------
Passes run in the order declared by `ORDERED_PASSES` at the bottom of this
file — **never in definition order, and never in import order**. Moving a
function within this file must stay harmless; only editing that list changes
behaviour.

Each pass declares the *stage* it runs in, which is what positions it relative
to the built-in passes still in `narraoke.py`. Adding a pass is a one-line
registration, not an edit to `rewrite_for_tts`.

WHY THIS IS NOT THE `REGEX_STAGES` MECHANISM
--------------------------------------------
`rules/stack.py` exposes exactly two stages (`pre_ipa`, `post`) to tier 1-3
*data* rules, and deliberately no more: every hook exposed to a public file
format freezes an internal step into that format. That reasoning is about
untrusted data. These passes are reviewed in-repo code in the same trust
domain as `rewrite_for_tts` itself, so they may attach at finer-grained
stages without exposing anything to a rule file. The two mechanisms stay
separate on purpose.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

# Where a tier-4 pass may attach, in application order.
#
# These name positions in `rewrite_for_tts`'s hand-tuned sequence. They are
# internal: no rule file can reference them, so they stay free to change.
#
#   word_ipa    word-level IPA escapes, after the version/identifier passes
#               and before the dotted-name passes. The default, and where
#               anything of the "Kokoro mispronounces this word" kind goes.
#   emphasis    after range expansion and quote/paren wrapping, for passes
#               that must see the emphasised form.
PASS_STAGES: tuple[str, ...] = ("word_ipa", "emphasis")


@dataclass(frozen=True)
class Pass:
    """One registered rewrite pass.

    `name` is used in warnings and by the tests that assert the declared
    order, so it must stay stable. `why` documents the Kokoro defect being
    worked around — the same role the inline comments played when these were
    hard-coded calls, kept next to the code rather than at the call site.
    """

    name: str
    fn: Callable[[str], str]
    stage: str = "word_ipa"
    why: str = ""

    def __post_init__(self) -> None:
        if self.stage not in PASS_STAGES:
            raise ValueError(
                f"pass {self.name!r}: unknown stage {self.stage!r}; "
                f"expected one of {PASS_STAGES}"
            )


# ── The passes ──────────────────────────────────────────────────────────────


def fix_copied(text: str) -> str:
    """Wrap "copied" so Kokoro reads the two-syllable "COP-eed", not
    "cop-ih-ed".

    Kokoro splits the "-ied" suffix into a spurious extra syllable. The word
    boundary is load-bearing rather than stylistic: misaki phonemizes a
    mid-word escape to nothing, so a bare-substring version of this rule
    turns "uncopied" into "un[copied](/…/)" and loses the word entirely.
    Prefixed forms are left to Kokoro, which reads them correctly; a hyphen
    is a word boundary, so the real word inside "hand-copied" still fires.

    The vowel is /ɑ/, not /ɒ/. Both phonemize cleanly, so this is not caught
    by a smoke test — but the pipeline runs misaki with `british=False`, and
    /ɒ/ is the British vowel. It would put one subtly British word in an
    otherwise American voice.
    """
    pat = re.compile(r"\b(copied)\b(?!\]\(/)", re.IGNORECASE)
    return pat.sub(r"[\1](/kˈɑpid/)", text)


def fix_transient(text: str) -> str:
    """Wrap "transient" so Kokoro reads "TRAN-zee-ent" (standard American)
    instead of "TRAN-chent".

    Case-insensitive so "Transient" in headings and table cells gets the same
    treatment. The negative lookahead guards against double-wrapping.
    """
    pat = re.compile(r"\b(transient)\b(?!\]\(/)", re.IGNORECASE)
    return pat.sub(r"[\1](/tɹˈænziənt/)", text)


def fix_enum(text: str) -> str:
    """Wrap "enum"/"enums" so Kokoro reads "EE-num", not "in-UM".

    Conditional on the plural, which is why this is a pass and not a literal:
    the two forms take different IPA. Whole-word boundary leaves "enumerate"
    alone, and the negative lookahead stops the literal pass double-wrapping.
    """
    def repl(m: re.Match) -> str:
        word = m.group(1)
        ipa = "/ˈinʌmz/" if word.endswith("s") else "/ˈinʌm/"
        return f"[{word}]({ipa})"
    return re.compile(r"\b(enums?)\b(?!\]\(/)").sub(repl, text)


def fix_retryable(text: str) -> str:
    """Wrap "retryable"/"retriable" and their "-bility" noun forms so Kokoro
    reads "re-TRY-uh-bul" rather than "re-TREE-uh-bul".

    Internal order is load-bearing: the longer noun forms are handled first so
    the bare "retryable" rule cannot partially match inside "retryability".
    Case-insensitive for table headers; lookaheads guard double-wrapping.
    """
    text = re.sub(
        r"\b(retryability|retriability)\b(?!\]\(/)",
        r"[\1](/ɹitɹˌaɪəbˈɪlɪti/)",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"\b(retryable|retriable)\b(?!\]\(/)",
        r"[\1](/ɹitɹˈaɪəbəl/)",
        text,
        flags=re.IGNORECASE,
    )


def say_at_symbol(text: str) -> str:
    """Speak "@" as the word "at" in the two unambiguous positions.

    Kokoro voices a bare "@" inconsistently — often skipping it — so it is
    forced to the word "at" here. Only the two shapes the symbol reliably
    *means* "at" are rewritten:

      * surrounded by whitespace  ``a @ b``  → ``a at b``
      * flanked by non-whitespace ``a@b``    → ``a at b``  (e.g. a handle or
        an ``user@host``)

    A one-sided ``@`` (``a@ b`` or ``a @b``) is left alone: it is neither of
    the requested shapes and is more likely a typo or markup artefact than a
    spoken "at".

    Whitespace collapses to a single space so ``a  @  b`` does not leave a
    double space. The two-sided-text case inserts spaces so the surrounding
    words stay separate tokens for the phonemizer.
    """
    text = re.sub(r"\s+@\s+", " at ", text)
    return re.sub(r"(?<=\S)@(?=\S)", " at ", text)


# Heteronyms Kokoro stresses as a noun by default but which read as a verb.
# Capitalisation (or following a colon/heading lead-in) biases the noun
# reading; mid-sentence lowercase "records" already renders correctly. Use
# misaki's native verb phonemization so it matches the prosody of unaltered
# mid-sentence "records" elsewhere in the audio.
#   ɹəkˈɔɹdz = "re-CORDS" (verb, matches misaki's natural rendering)
#   ɹˈɛkəɹdz = "RE-cords" (noun — what we're overriding)
_RECORDS_VERB_IPA = "/ɹəkˈɔɹdz/"


def force_verb_stress_heteronyms(text: str) -> str:
    """Apply IPA escapes for heteronyms Kokoro mis-stresses.

    Targets "Records" (capital R), which biases Kokoro toward the noun form
    regardless of mid-sentence position. Specifically fires at: start of
    string, after `.!?` + space, after a numbered-list marker (`1. `), or
    after a colon + space (the "verb-list lead-in" pattern: "..., which:
    Records ...").

    Mid-sentence lowercase "records" already renders correctly via Kokoro's
    own context handling, so we leave it untouched.
    """
    return re.sub(
        r"(^|[.!?]\s|^\d+\.\s|\n\d+\.\s|:\s)Records\b",
        lambda m: f"{m.group(1)}[Records]({_RECORDS_VERB_IPA})",
        text,
        flags=re.MULTILINE,
    )


# ── Application order ───────────────────────────────────────────────────────
#
# THIS LIST, and nothing else, defines when each pass runs. Reordering the
# function definitions above is a no-op; reordering these entries changes
# generated audio.
#
# Within `word_ipa` the current four are mutually independent — no pattern
# here matches inside another's match — so their relative order is not
# load-bearing today. That is a property to re-check when adding a pass, not
# a licence to sort the list.
ORDERED_PASSES: tuple[Pass, ...] = (
    Pass(
        name="retryable",
        fn=fix_retryable,
        stage="word_ipa",
        why='Kokoro reads "retryable" as "re-TREE-uh-bul"',
    ),
    Pass(
        name="transient",
        fn=fix_transient,
        stage="word_ipa",
        why='Kokoro reads "transient" as "TRAN-chent"',
    ),
    Pass(
        name="enum",
        fn=fix_enum,
        stage="word_ipa",
        why='Kokoro reads "enum" as "in-UM"',
    ),
    Pass(
        name="copied",
        fn=fix_copied,
        stage="word_ipa",
        why='Kokoro splits "-ied", giving "cop-ih-ed"',
    ),
    Pass(
        name="at-symbol",
        fn=say_at_symbol,
        stage="word_ipa",
        why='Kokoro voices a bare "@" inconsistently, often skipping it',
    ),
    # Runs in `emphasis` because it must see the text *after* quote and paren
    # wrapping: those passes insert punctuation that its sentence-start and
    # lead-in anchors match against.
    Pass(
        name="verb-stress-heteronyms",
        fn=force_verb_stress_heteronyms,
        stage="emphasis",
        why='Kokoro reads "Records" as the noun "RE-cords"',
    ),
)


def passes_for(stage: str) -> tuple[Pass, ...]:
    """The registered passes for *stage*, in declared order."""
    if stage not in PASS_STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {PASS_STAGES}")
    return tuple(p for p in ORDERED_PASSES if p.stage == stage)


def apply_passes(text: str, stage: str) -> str:
    """Run every pass registered for *stage*, in declared order."""
    for entry in passes_for(stage):
        text = entry.fn(text)
    return text
