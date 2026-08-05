"""Tier-4 rules for ordinary prose: compounds, heteronyms, and word-level fixes.

These are genuine Kokoro defects in American English rather than facts about
any one document.

ORDER IS SEMANTICS. Rules apply sequentially via `str.replace` over a mutating
buffer. Do not sort, dedupe, or reorder this list.

The regex-based counterparts (_fix_retryable, _fix_transient, _fix_enum,
_force_verb_stress_heteronyms, and the emphasis/range passes) still live in
narraoke.py, where the 12-step `rewrite_for_tts` sequence orders them.
Moving those is a later step: they are functions with their own hand-tuned
ordering, not data.
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
    # "copied" — see _fix_copied in narraoke.py for the regex form. It needs
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
    # "enum" / "enums" — see _fix_enum in narraoke.py for the regex form
    # (needed to avoid touching "enumerate" and to prevent cascading on the
    # plural).
]
