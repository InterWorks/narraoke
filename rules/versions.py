"""Tier-4 rules for version strings.

This module is intentionally empty of literals.

Version numbers are now handled by two patterns in html_to_video.py rather
than an enumeration:

  * `_spell_out_versions` — any `X.Y.Z` with three or more integer
    components, an optional `v` prefix, and `-rc.1` / `-alpha` / `+build.27`
    suffixes. Three components are required so that "section 1.2" and
    "Python 3.11" stay prose.
  * `_spell_out_wildcard_versions` — wildcard lines like `4.x`, `4.2.x`,
    `v2.14.X`. Two components suffice here because a trailing "x" can never
    be a decimal fraction, which is exactly what makes the shorter form
    unambiguous.

What used to live here:

  * `4.28.1` — superseded by `_spell_out_versions`.
  * `4.x` — superseded by `_spell_out_wildcard_versions`.
  * `^4.0` — NOT covered by either, deliberately. `4.0` is indistinguishable
    from the decimal number "four point zero", so narrating it as a version
    would be wrong wherever it is genuinely a number. The caret constraint
    syntax is also specific to one document's dependency-pinning example, so
    it belongs in tier 1 beside that markdown.

Kept as a module rather than deleted: it is the obvious home for a future
version-related rule, and an absent module would read as an oversight rather
than a decision. The empty LITERALS list assembles to nothing.
"""
from __future__ import annotations

LITERALS: list[tuple[str, str]] = []
