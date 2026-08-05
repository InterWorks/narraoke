"""Tier-4 rules for ordinary prose: compounds, heteronyms, and word-level fixes.

These are genuine Kokoro defects in American English rather than facts about
any one document.

ORDER IS SEMANTICS. Rules apply sequentially via `str.replace` over a mutating
buffer. Do not sort, dedupe, or reorder this list.

The regex-based counterparts (`fix_retryable`, `fix_transient`, `fix_enum`,
`fix_copied`, `force_verb_stress_heteronyms`) now live in `passes.py`, which
registers them against a named stage in `rewrite_for_tts`. They are functions
with their own hand-tuned ordering rather than data, which is why they are a
sibling module and not entries in this list.

The emphasis and range passes remain in narraoke.py: they rewrite sentence
shape generally rather than fixing a named word, so they are pipeline steps
rather than rules.
"""
from __future__ import annotations

LITERALS: list[tuple[str, str]] = [
    # Hyphenated time-self compounds: Kokoro reads the hyphen as a long pause.
    # A space joins them as one smooth modifier.
    ("past-you", "past you"),
    ("future-you", "future you"),
    ("present-you", "present you"),
    # Kokoro drops the "-ed" on "hijacked" (renders as "hijack"). Force the
    # past-tense form via IPA so the final syllable is audible.
    ("hijacked", "[hijacked](/hˈIʤˌæktɪd/)"),
    # "copied" — see `fix_copied` in passes.py for the regex form. It needs
    # a word boundary: a mid-word escape like "un[copied](/…/)" fails to
    # phonemize entirely, so a bare literal would break "uncopied".
    # "delegates" — Kokoro picks the NOUN pronunciation "DEL-uh-gits"
    # (the people) instead of the VERB "DEL-uh-GAYTS" (the action). Force
    # verb stress with an IPA escape. Only the inflected verb form needs
    # this; the bare infinitive "delegate" already reads as the verb in
    # English (stress is the same /-eɪt/ at the end).
    #
    # Universal despite being a heteronym, because the two failure modes are
    # NOT symmetric (docs/rule-triage.md, D1). Applying verb stress to the
    # noun yields a reading many speakers genuinely use, so it degrades
    # gracefully. Applying noun stress to the verb is wrong to every listener
    # — no one says "the manager DEL-uh-gits the work". Defaulting to the verb
    # is therefore the safer error in a document we have not seen.
    ("delegates", "[delegates](/dˈɛləɡˌeɪts/)"),
    # "enum" / "enums" — see `fix_enum` in passes.py for the regex form
    # (needed to avoid touching "enumerate" and to prevent cascading on the
    # plural).
]
