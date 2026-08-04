"""Tier-4 rules for initialisms and acronyms Kokoro spells out letter by letter.

ORDER IS SEMANTICS. Rules apply sequentially via `str.replace` over a mutating
buffer, so a rule whose `from` is a substring of a later rule's `from` will
consume it first. Within this module: `Invalid JSON` MUST stay before `JSON`,
or the compound never fires.

Do not sort, dedupe, or reorder this list. The inline comments are the only
record of why each rule exists.
"""
from __future__ import annotations

LITERALS: list[tuple[str, str]] = [
    # Bare "SHA" reads as letters; the conventional pronunciation is "shah".
    # Lowercasing to "Sha" gives Kokoro the expected mono-syllable. Bonus:
    # "checksum/Sha" reads the slash aloud, clearer than the silent drop.
    ("SHA", "Sha"),
    # "uvx" reads as one word "uvks"; letter-dotted form spells it U-V-X.
    ("uvx", "U.V.X."),
    # All-caps "TODO" / "FIXME" get spelled letter-by-letter; lowercase
    # phrases read as the intended words.
    ("TODO", "to do"),
    ("FIXME", "fix me"),
    # "XSS" and "TTL" used to live here. Both moved to tier 3b — org defaults
    # (docs/rule-triage.md, D2/D3). Expanding an initialism for a lay audience
    # is an audience judgement, not a Kokoro defect: an infra-only audience
    # would rather hear "T-T-L". Neither is confidential; they are shared, not
    # secret. See the 3a/3b split in the triage doc.
    # When "Invalid" sits next to the IPA-escaped JSON token, Kokoro shifts
    # its stress from "in-VAL-id" (adjective) to "IN-vuh-lid" (the noun).
    # Force the adjective form with our own IPA escape.
    # Order matters: "Invalid JSON" before the bare "JSON" (longer first).
    ("Invalid JSON", "[Invalid](/ɪnvˈælɪd/) JSON"),
    # JSON is conventionally pronounced "JAY-sahn", but Kokoro defaults to
    # spelling it J-S-O-N. Force the spoken pronunciation via IPA. (Plain
    # "Jason" gets close but renders the final 'n' as a softer schwa+n.)
    ("JSON", "[JSON](/ʤˈeɪsˌɑn/)"),
    # "transient" — see _fix_transient in rules/prose.py for the regex form
    # (needed so capitalised "Transient" in headings / table cells gets the
    # same treatment).
    # "YAML" reads as letters; the convention is "YAM-uhl".
    ("YAML", "[YAML](/jˈæmᵊl/)"),
    # "README" is the file; say "reed-me" (forced long-e), not "red-me" or
    # "readm". Promoted from tier 1 (docs/rule-triage.md, D6): the wrong
    # reading is wrong in any technical document, so it is a Kokoro defect
    # rather than a fact about one document. It lived in a project file only
    # because that is where it was first noticed.
    ("README", "[read](/ɹˈid/) me"),
    # "SemVer" — keep it a two-syllable word ("sem-ver") rather than spelled
    # out letter by letter. Promoted from tier 1 for the same reason as
    # README (docs/rule-triage.md, D7).
    ("SemVer", "Sem-Ver"),
]
