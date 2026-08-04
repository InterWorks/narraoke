"""Tier-4 rules for code identifiers.

This module is intentionally empty of literals.

Everything that used to live here was either a pattern hiding inside a
hardcoded enumeration, or a fact about one document:

  * `custom_id` / `customer_id` / `order_id` / `session_id` — four instances of
    a general pattern, now `_spell_out_id_suffix` in narraoke.py:
    `\\b(\\w+)_id\\b` -> `\\1 I.D.`. The regex covers any `*_id` identifier,
    including ones no document has used yet.
  * `isRetryable` / `isError` — camelCase splits. Both are tier 1: they name
    specific API fields from one document. A generic camelCase rule was
    considered and rejected — a regex that fires on any lowercase-word
    followed by a capitalised word is hard to bound against prose, and two
    source documents are too thin an evidence base for a rule that would run
    against every future one.
  * `KEY=value` — a literal from one document's environment-variable example.

Kept as a module rather than deleted: it is the natural home for a future
identifier rule, and its absence would otherwise read as an oversight. The
empty LITERALS list is deliberate and assembles to nothing.
"""
from __future__ import annotations

LITERALS: list[tuple[str, str]] = []
