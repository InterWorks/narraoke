"""Tier-4 (universal) pronunciation rules, assembled in an explicit order.

Tier 4 stays Python source rather than data files on purpose: adding a rule
here requires editing code and passing review, which a config tweak never does.
That is the structural leak control — tiers 1-3 live outside this repo, so a
confidential rule cannot reach the public package through a config directory.

ORDER IS SEMANTICS
------------------
`_apply_literal_overrides` walks the assembled list with sequential
`str.replace` over a mutating buffer, so position determines which rule fires
first. Order comes from ORDERED_RULE_SOURCES below — **never from import
order**. Reordering the imports in this file must stay harmless; a linter that
sorts them must not be able to change generated audio.

Within a module, list position still matters. Two ordering constraints exist
today, and both are contained inside a single module by design:

  * `Invalid JSON` before `JSON`            (initialisms)
  * `~/.claude.json` before `.claude.json`  (filenames)

Verified exhaustively when the rules were split out of html_to_video.py: every
rule in isolation, all 756 ordered pairs, all 756 tight concatenations, and
3000 random multi-rule sentences produce byte-identical output under this
grouped order and the original interleaved one. No interaction crosses a module
boundary. If you add a rule whose `from` is a substring of one in a *different*
module, that guarantee no longer holds — keep interacting rules together.
"""
from __future__ import annotations

from . import filenames, identifiers, initialisms, prose, versions

# The application order of the rule modules. This list — not the import
# statement above — is what defines rule precedence.
#
# The original flat list in html_to_video.py interleaved these groups; that
# interleaving was incidental, not load-bearing (see the module docstring for
# the equivalence proof). Grouping is what makes the rules navigable.
ORDERED_RULE_SOURCES: tuple[str, ...] = (
    "identifiers",
    "prose",
    "versions",
    "initialisms",
    "filenames",
)

_MODULES = {
    "identifiers": identifiers,
    "prose": prose,
    "versions": versions,
    "initialisms": initialisms,
    "filenames": filenames,
}


def _assemble_literals() -> list[tuple[str, str]]:
    """Flatten every module's LITERALS in ORDERED_RULE_SOURCES order."""
    assembled: list[tuple[str, str]] = []
    for name in ORDERED_RULE_SOURCES:
        assembled.extend(_MODULES[name].LITERALS)
    return assembled


# Generic literal-phrase overrides — apply to ANY narrated markdown doc.
# Doc-specific overrides go in a sibling JSONC file; see `load_doc_overrides`.
# Each key here must be unique enough not to false-match elsewhere.
LITERAL_TTS_OVERRIDES: list[tuple[str, str]] = _assemble_literals()

# Dotfile rewrites, applied with anchoring by _spell_out_dotfiles rather than
# by the flat literal pass.
DOTFILE_NARRATION: list[tuple[str, str]] = filenames.DOTFILE_NARRATION

__all__ = [
    "LITERAL_TTS_OVERRIDES",
    "DOTFILE_NARRATION",
    "ORDERED_RULE_SOURCES",
]
