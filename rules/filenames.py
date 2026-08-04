"""Tier-4 rules for filenames, dotfiles, and dotted config paths.

ORDER IS SEMANTICS. Rules apply sequentially via `str.replace` over a mutating
buffer, so list position determines which rule fires first. Do not sort,
dedupe, or reorder these lists.

No substring pairs remain in this module today — the `~/.claude.json` before
`.claude.json` constraint retired with those literals (see below). That is a
property of the current contents, not a guarantee: check before adding.
"""
from __future__ import annotations

LITERALS: list[tuple[str, str]] = [
    # "lockfile" as one word renders with kerned secondary stress so it sounds
    # like "lockfull". Two words gives clear primary stress on each.
    ("lockfile", "lock file"),
    # Hidden dotted config files (`.claude.json`, `.mcp.json`, `~/.claude.json`)
    # used to be three literals here. They are now handled generically by
    # `_spell_out_hidden_dotted_names` in html_to_video.py, which also covers
    # `.eslintrc.json`, `.prettierrc.json`, and any other hidden config file,
    # and narrates a `~/` prefix as the word "home".
]

# Bare dotfile rewrites: each entry pairs a literal dotfile name with the
# narration form Kokoro reads clearly. Anchored so paths like "/foo.env" and
# compounds like ".envrc" stay untouched. Add new entries as you find them.
#
# These stay an explicit list on purpose. Unlike hidden files WITH an internal
# dot (`.claude.json`), a bare `.word` has no structural marker distinguishing
# it from prose, so a generic rule would fire on sentence fragments and
# abbreviations. Enumeration is the right shape here.
#
# Consumed by _spell_out_dotfiles, not by the flat literal pass — these are
# applied with anchoring rather than a bare str.replace.
DOTFILE_NARRATION: list[tuple[str, str]] = [
    (".env", "dot E N V"),
    (".gitignore", "dot git ignore"),
]
