# TTS rule triage

The record of which tier every pronunciation rule belongs to, and why.

Rules are assigned to exactly one tier. Precedence at runtime is
**project → user → company → universal** (most specific first).

| # | Tier | Scope | Lives in |
|---|---|---|---|
| 1 | Project | one document | `<markdown>.tts-overrides.json`, beside the source |
| 2 | User | all my projects, private to me | `${XDG_CONFIG_HOME:-~/.config}/narraoke/rules.d/*.json` |
| 3 | Company | shared with a group | a cloned private repo, split into **3a** (confidential) and **3b** (org defaults, not sensitive) |
| 4 | Universal | everyone | Python literals, packaged with the app |

**Decision rule, applied in order:**

1. Reveals something **internal** — a client name, internal product, Slack
   channel, contact address, or credential identifier → **tier 3a**.
   *Check the `why` text too*, not just `from`/`to`. (Shared-but-public
   material, including our own company name, is tier 3b — see the split below.)
2. Only correct because of this document's subject matter → **tier 1**.
3. A personal preference you would not impose on the team → **tier 2**.
4. Wrong in any American-English technical document regardless of topic → **tier 4**.
5. Fires in neither source document and is not a defensible general rule →
   **drop**.

Before demoting anything, ask whether a **generalizable rule is hiding**. Two
known cases implemented a real pattern as a hardcoded enumeration of the
instances one document happened to use. Those are promotions to tier-4 regexes,
not demotions to tier 1.

## Method

Hit counts below are empirical: each rule's `from` string was counted against
both source documents (`github-org-onboarding.md`, the Claude exam guide) as of
2026-08-03. "Fires nowhere" means zero occurrences in both.

Two corrections to earlier estimates, from that count:

- **`^4.0` does fire** (twice, in onboarding). It is not a drop candidate.
- **`Invalid JSON` never fires.** The only occurrences of "invalid" in either
  document are the lowercase verb "invalidate", which the rule cannot match.

---

## Tier 4 — universal (stays in code)

Genuine Kokoro defects in American English. Correct in any technical document,
regardless of subject. Nothing here is sensitive; this is the tier that ships
in the public package.

| Rule | Fires | Rationale |
|---|---|---|
| `_spell_out_id_suffix` | exam ×6 | **Promoted** from 4 hardcoded literals to `\b(\w+)_id\b` → `\1 I.D.` — see below |
| `_spell_out_assignments` | both | **Promoted** from the `KEY=value` literal. Kokoro drops `=` entirely, so `DEBUG=true` narrated as "DEBUG true" — the assignment was inaudible. Now `LEFT=RIGHT` → "LEFT equals RIGHT", covering env vars, CLI flags, and config pairs. Catches `CLAUDE_HEADLESS=true` in the exam guide, which the literal missed. |
| `_spell_out_versions` | both | **Promoted** from 3 code literals + 16 hand-written tier-1 rules. Any `X.Y.Z` narrates, plus a `v` prefix and `-rc.1` / `-alpha` / `+build.27` suffixes — see below. |
| `_spell_out_wildcard_versions` | onboarding | **Promoted** from the `4.x` literal. Covers `4.x`, `4.2.x`, `v2.14.X`. Safe at two components where the plain version rule needs three, because a trailing `x` can never be a decimal fraction. |
| `_spell_out_hidden_dotted_names` | exam ×11 | **Promoted** from 3 literals. Any hidden config file with an internal dot (`.claude.json`, `.eslintrc.json`), with `~/` narrated as "home". Bare dotfiles stay an explicit list — see below. |
| `JSON` → IPA | exam ×13 | Kokoro spells J-S-O-N in any document |
| `YAML` → IPA | exam ×5 | Reads as letters; convention is "YAM-uhl" |
| `SHA` → `Sha` | onboarding ×4 | Reads as letters; convention is "shah" |
| `lockfile` → `lock file` | onboarding ×6 | One word renders as "lockfull" |
| `TODO` → `to do` | nowhere | All-caps spells letter-by-letter. Universal convention; keep despite no hit — see note below |
| `FIXME` → `fix me` | nowhere | Same as `TODO` |
| `hijacked` → IPA | onboarding ×1 | Kokoro drops the "-ed" — a defect, not a topic choice |
| `_fix_copied` | — | Kokoro splits "-ied" into "cop-ih-ed" — same defect class as `hijacked`. Regex, not a literal: a mid-word escape fails to phonemize, so it must not fire inside "uncopied". Vowel is /ɑ/ (American), not /ɒ/ (British) — both phonemize, so only a test catches it |
| `_fix_retryable` | exam ×4 | Pure pattern, no literals |
| `_fix_transient` | exam ×7 | Pure pattern |
| `_fix_enum` | exam ×4 | Pure pattern; conditional logic keeps it as reviewed code |
| `_spell_out_vs` | both (23) | Pure pattern |
| `_spell_out_dotted_names` | both (49) | Pure pattern, zero literals — the model the others should follow |
| `_spell_out_dotfiles` | onboarding ×11 | Pure pattern |
| `_emphasise_quoted_spans` | both (117) | Pure pattern |
| `_emphasise_parentheticals` | both (294) | Pure pattern |
| `_expand_numeric_ranges` | exam ×8 | Pure pattern |
| `.env` → `dot E N V` | onboarding ×9 | Universal dotfile |
| `.gitignore` → `dot git ignore` | onboarding ×2 | Universal dotfile |
| `README` → `[read](/ɹˈid/) me` | onboarding | **Promoted from tier 1.** Kokoro reads "RED-me"; the file is called "reed-me" in any technical document. A defect, not a topic choice — it sat in tier 1 only because that is where it was first hit. |
| `SemVer` → `Sem-Ver` | onboarding | **Promoted from tier 1.** Kokoro spells it letter-by-letter; the two-syllable reading is correct everywhere. Same accident of placement as `README`. |

**On `TODO` / `FIXME`:** these fire in neither source document, which by the
strict decision rule makes them drop candidates. They are kept because they are
unambiguous universal conventions that would be correct in any technical
document — the rule exists to catch a Kokoro defect, not to serve a topic. This
is a deliberate exception to criterion 5, recorded here rather than silent.

### Promotions to tier-4 regexes

Two rules are real patterns implemented as hardcoded enumerations. Promoting
them generalizes the rule *and* removes the doc-specific literals.

| Current | Becomes | Status |
|---|---|---|
| `custom_id`, `customer_id`, `order_id`, `session_id` | `\b(\w+)_id\b` → `\1 I.D.` | **Landed.** Four instances one document used, of a pattern that applies to any `*_id` identifier. Now covers `tenant_id`, `user_id`, and anything else, while leaving `_id`, `some_idea`, and `identity` alone. |
| `KEY=value` | `LEFT=RIGHT` → "LEFT equals RIGHT" | **Landed.** Strictly better than the literal it replaces: the old rule lowercased the key but still dropped the `=`, so the assignment stayed inaudible. The regex excludes every comparison and compound-assignment operator (`==`, `!=`, `<=`, `>=`, `+=`, `-=`, `*=`, `/=`, `%=`, `^=`, `&=`, `\|=`) and leaves spaced `a = b` as prose. |
| `4.28.1`, plus 16 hand-written version rules in the onboarding file | `_spell_out_versions` | **Landed.** The worst instance of the anti-pattern: three code literals *and* sixteen tier-1 rules, five of which (`1.4.2 → 2.0.0`, `2.3.0 → 2.4.0`, …) were pure combinatorics. The regex covers any 3+ component version, a `v` prefix, and `-rc.1` / `-alpha` / `+build.27` suffixes. |
| `.mcp.json`, `.claude.json`, `~/.claude.json` | `_spell_out_hidden_dotted_names` | **Landed.** `_spell_out_dotted_names` already handled non-hidden files generically; its left boundary rejects a leading dot, so hidden files fell through unless hardcoded. The new rule closes that gap and narrates a `~/` prefix as "home". Verified a pure no-op: all three literals reproduce byte-identically and no golden file changed. |

**Version rule — three decisions worth recording.**

*Three or more components, deliberately.* Two-part forms are excluded because
"section 1.2" and "Python 3.11" are far more often prose than version strings.

*Wildcards are a separate rule, and can be looser.* `_spell_out_wildcard_versions`
matches at two components (`4.x`) because the trailing `x` is what removes the
ambiguity — a wildcard placeholder can never be a decimal fraction. This is the
distinction that lets `4.x` narrate while `4.0` is left alone: `4.0` is
indistinguishable from "four point zero", and narrating it as a version would
be wrong wherever it is genuinely a number. `^4.0` therefore stays tier 1 as
dependency-constraint syntax from one document's pinning example.

*Leading single digits are spelled as words* — `4.28.1` → "four dot 28 dot 1".
This **overrides** the earlier hand-tuning: of the 16 tier-1 version rules, 15
kept the leading digit as a numeral and only `4.28.1` spelled it out, with the
stated reason that Kokoro reads a bare "4" as the homophone "for". Applying the
spelling uniformly is a deliberate consistency choice, and it changes how the
onboarding video narrates 15 existing version strings (`1 dot 2 dot 3` becomes
`one dot 2 dot 3`). Recorded as an intentional change, not a regression.

*The `version` prefix is not part of the generic rule.* Ten tier-1 rules say
"version 1 dot 0 dot 0" while five say just "1 dot 4 dot 2" — that is a
per-document style choice, so it stays in tier 1. Only an explicit `v` prefix
in the source (`v1.2.3`) produces the spoken word.

**Hidden dotted names — why only half the dotfile problem was generalised.**
Hidden config files split into two shapes, and only one is safely patternable:

- **With an internal dot** (`.claude.json`, `.eslintrc.json`) — the internal
  dot is a structural marker. A leading dot plus two-or-more dot-separated
  segments cannot be ordinary prose, so a regex is safe. **Promoted.**
- **Bare** (`.npmrc`, `.editorconfig`, `.dockerignore`) — no internal
  structure. A rule matching any `.word` would fire on sentence fragments and
  abbreviations, so `_DOTFILE_NARRATION` stays an explicit two-entry list.
  **Enumeration is the correct shape here**, not a failure to generalise.

This is worth stating because the two look like the same problem. The
distinction is the same one that separates `4.x` from `4.0`: generalise where
the text carries an unambiguous marker, enumerate where it does not.

**Naming hazard hit during implementation.** The first version of this rule was
called `_VERSION_RE` — a name already used further down the module for the
output-directory timestamp pattern (`2026-08-03T14-44-05`). The later
definition silently shadowed the earlier one, so the semver rule matched
nothing *and* `--skip-tts` version-directory scanning would have broken. It is
now `_SEMVER_RE`, with a test pinning both patterns.

**Rejected: a generic camelCase splitter.** `isRetryable` and `isError` are
camelCase splits, and a rule like `\b([a-z]{2,})([A-Z][a-z]+)\b` → two words
would have replaced both *and* covered `allowedTools` and `errorCategory`,
which the exam guide contains but nothing currently fixes. It was rejected as
too broad to bound: unlike a literal, a regex fires on text nobody has
reviewed, and two source documents are too thin an evidence base for a rule
that would run against every future one. Both literals go to tier 1 instead.
If a third document brings more camelCase, revisit with a wider corpus.

The `*_id` promotion makes three of the four drop candidates disappear rather
than needing a decision — the pattern covers them.

**Migration caution.** The dotted-config promotion must preserve the existing
ordering constraint (`~/.claude.json` before `.claude.json`, longer first) and
the `~/` prefix drop. Land it with the golden test green, and verify no change
to `tests/fixtures/02-ordering-longer-first.out.txt`.

### Where tier-4 rules live (planned, step 9)

Tier 4 stays **Python source** — that is the structural leak control. Adding a
sensitive rule requires editing code and passing review, which a config tweak
never does. But the rules move out of `narraoke.py` into a `rules/`
package, split by the defect each one fixes:

| Module | Holds | Count |
|---|---|---|
| `rules/initialisms.py` | `SHA`, `uvx`, `TODO`, `FIXME`, `Invalid JSON`, `JSON`, `YAML` | 7 |
| `rules/filenames.py` | `lockfile`, the dotted-config literals, `_spell_out_dotfiles`, `_spell_out_dotted_names` | 4 + regexes |
| `rules/identifiers.py` | `KEY=value`, the `*_id` family, `isRetryable`, `isError` | 7 |
| `rules/prose.py` | `past-you` family, `hijacked`, `delegates` | 5 |
| `rules/passes.py` | pattern passes needing real Python: `_fix_retryable`, `_fix_transient`, `_fix_enum`, `_fix_copied`, the verb-stress heteronyms | 5 passes |
| `rules/versions.py` | `4.28.1`, `4.x`, `^4.0` — all leaving for tier 1 anyway | 3 → 0 |

(`TTL` and `XSS` appear in no module — they leave for tier 3b.)

**Order is pinned explicitly, not by import order.** Each module exports its
own ordered list; `rules/__init__.py` holds a single `ORDERED_RULE_SOURCES`
naming the modules in application order, and assembles the flat list from it.
Import statements must never be load-bearing — a linter that reorders them
would otherwise silently change generated audio.

**Verified safe to split this way:** both order-critical substring pairs stay
*within* one module — `Invalid JSON` before `JSON` (initialisms) and
`~/.claude.json` before `.claude.json` (filenames). No ordering constraint
crosses a file boundary, so only intra-file order matters, and that stays
visible where the rules are.

Add a test asserting the assembled sequence matches the current flat list, so
the invariant is checked rather than documented.

---

## Tier 3 — company (private rules repo)

**The tier-3 repo is `InterWorks/narraoke-overrides`** (private), cloned
locally at `../narraoke-overrides`. Its path is set per-machine via
`company_rules_dir` in `narraoke.config.json` at the repo root (gitignored;
`narraoke.config.example.json` is committed as the schema), or
`$NARRAOKE_COMPANY_RULES`, or `--company-rules`.

Tier 3 holds two distinct kinds of rule, and the distinction matters. Keep
them in **separate files** in that repo so the boundary stays legible —
`10-confidential.json` and `20-org-defaults.json`, say. Files in a resolved
rules directory compose in sorted order, so the numeric prefixes also fix
application order.

- **3a — confidential.** Reveals internal structure: internal channels,
  contacts, product names, credential identifiers. Must never be public. This
  is what the leak scan gates on.
- **3b — org defaults.** Not sensitive at all; shared because they suit our
  audience, not because they are secret. Safe to publish, but scoped to the
  org by choice.

The test for 3a is **"does this reveal something internal?"**, not "does this
mention the company?". The company's own name is public — the rule fixing its
*pronunciation* is 3b, not 3a. Confusing the two would put a public fact behind
a private gate, and make "everything in 3a is genuinely secret" false.

Anything in 3a is a leak if it escapes. Nothing in 3b is. Mixing them in one
file makes "everything in tier 3 is sensitive" untrue, which is exactly the
assumption that makes the leak scan meaningful.

> **Operational caveat for 3b.** Tier 3 resolves from a cloned repo whose path
> is set per-machine. A render without it configured silently loses these
> rules. That is the right failure mode for 3a — a missing NDA rule should not
> be papered over — but for 3b it means a fresh laptop or CI box narrates
> "T-T-L" instead of "time to live" with no warning. The startup summary logs
> each tier and its rule count; check it when output sounds wrong.

### 3a — confidential

The genuinely private set. All five currently live in the onboarding tier-1
file, which is why that file cannot be published as-is today.

**Redacted by design.** This file lives in the public repo, so the rules are
described by *kind* and location, never quoted. The rules themselves move to
the private company-rules repo; consult that repo for their contents.

| # | Kind | Source | Why it is tier 3 |
|---|---|---|---|
| C1 | Internal chat channel | onboarding tier-1 file | Names an internal channel |
| C2 | Internal chat channel | onboarding tier-1 file | Names a second internal channel |
| C3 | Internal contact address | onboarding tier-1 file | Names an internal mailbox |
| C4 | Internal product name | onboarding tier-1 file | Names an internal product |
| C5 | Production credential identifier | onboarding tier-1 file | The rewrite is generic, but the identifier names an internal production credential |

The bare `GITHUB_TOKEN` rule sitting beneath C5 is **not** tier 3 — it names a
standard GitHub Actions variable, not an internal one.

**The company-name pronunciation is *not* here.** It was initially filed as 3a
by inheritance from the original audit, which listed it among "the genuinely
private material." That was a category error: InterWorks is a public company
with a public GitHub org, and this repo sits inside it. How the name is
*pronounced* reveals nothing internal. It is tier 3b.

The leak scanner still flags the literal string `InterWorks` — deliberately.
Not because the name is secret, but because it is a cheap tripwire for "this
file has company-specific content worth a second look"; internal channels and
contacts tend to appear near it. Flagging the string is useful; treating the
pronunciation rule as confidential was not.

**The `why` fields leak independently.** Two of these five carry rationales that
name confidential material their own `from`/`to` pair does not: C2's rationale
refers to C1 by name while explaining an unrelated fix, and C4's rationale
names the product in prose.

A scanner checking only `from`/`to` would pass both. This is why
`scripts/leak_scan.py` scans raw bytes. Verified: it catches all 8 hits in the
onboarding file, including those two.

> This section was itself caught by the leak scanner during drafting — the
> first version quoted all five rules verbatim. Recorded as evidence the
> control works on exactly the mistake it exists to catch.

### 3b — org defaults (not confidential)

Shared across our projects but not sensitive: initialism expansions chosen for
our audience, plus the pronunciation of our own public company name. Every
entry is safe to publish; they live at tier 3 so the team shares one policy,
not because they are secret.

| Rule | Fires | Rationale |
|---|---|---|
| `TTL` → `time to [live](/lˈɪv/)` | nowhere | Expand for a general audience. Kept though currently unused — a decision, not an oversight. |
| `XSS` → `cross-site scripting` | nowhere | Same. Kept though currently unused. |
| `CVE` → `common vulnerabilities and exposures` | onboarding | Spell out for a lay audience rather than "C-V-E" |
| `2FA` → `two-factor authentication` | onboarding | Speak the initialism in full wherever it stands alone |
| `InterWorks` named pronunciation | onboarding | Kokoro stresses the second syllable; force the first. A public company name — nothing internal is revealed by how it is said. Shared so every InterWorks document pronounces it the same way. |

`TTL` and `XSS` fire in neither source document today. Criterion 5 would drop
them; they are kept deliberately as forward-looking org policy. Recorded here
so the exception is visible rather than silent.

**`2FA` is split across two tiers.** Only the *standalone* expansion is org
policy. The companion ` (2FA)` deletion stays at tier 1 — it removes a
redundant parenthetical only because that document spells out "two-factor
authentication" immediately before each one, which is a fact about that
document's prose, not a policy. The two must keep their relative order: the
deletion runs first, or the standalone rule consumes the text it targets.

---

## Tier 1 — project (stays beside the markdown)

Correct only because of a specific document's subject matter or prose.

### Onboarding document

| Rule | Rationale |
|---|---|
| `run up a cloud bill, read a database` → IPA | Quotes this document's exact sentence |
| `places this secret lives` → IPA | Exact prose from this document |
| `secret usually live` → IPA | Exact prose |
| `stale value live` → IPA | Exact prose |
| `skim — there's` → sentence break | Exact prose |
| `Where is it stored — everywhere it's stored?` | Exact prose |
| `Document where it lives,` | Exact prose |
| `an individual,` | Exact prose |
| `are for — describe what you have` | Exact prose |
| `Vacation-hotel-room-you` (both cases) | This document's coinage |
| `AWS SSO`, `compromised AWS credential` | Collocation fixes tied to this document's sentences |
| `~/.aws/credentials`, `~/.aws` | Specific paths this document discusses |
| `AI-suggested` | This document's hyphenation |
| `GITHUB_TOKEN` → `github_token` | Generic, but only appears here |
| `find-my-way` → `find my way` | A package name this document uses |
| ` (2FA)` → `` (deletion) | Removes a redundant parenthetical **because this document spells out "two-factor authentication" immediately before each one.** Wrong in a document that does not. |
| All SemVer version strings (`1.4.2 → 2.0.0`, `1.0.0-rc.1`, `2.4.1`, etc. — 16 rules) | Enumerated example versions from this document's tables |
| `4.28.1` → `four dot 28 dot 1` | This document's pinning example |

**Departures from this file.** Five rules that currently live here move out:
`README` and `SemVer` up to tier 4 (universal defects); `CVE`, the standalone
`2FA` expansion, and the `InterWorks` named pronunciation to tier 3b (org
policy — shared, but not sensitive). The ` (2FA)` deletion stays, as noted
above.

### Claude exam guide

| Rule | Fires | Rationale |
|---|---|---|
| `isRetryable` → `is retryable` | exam ×2 | Names a specific API field in this document. **Order-sensitive:** this must produce `is retryable` *with the space*, because `_fix_retryable` cannot match the camelCase form — the split has to happen first for the generic IPA rule to fire. Verified: with this entry loaded, `isRetryable` → `is [retryable](/ɹitɹˈaɪəbəl/)`. |
| `isError` → `is error` | exam ×2 | Same camelCase split, same document. No downstream IPA rule depends on it. |

The exam guide's override file is an empty scaffold today. These are its first
real entries; the file's `literal` array is where they land.

`delegates` was previously listed here. It stays at tier 4 — see D1 below.

**Note the `4.28.1` duplication.** It exists in *both* the built-in code list
(`4 dot 28 dot 1`) and this file (`four dot 28 dot 1`), with **different
replacements**. The tier-1 entry wins under project-first precedence, so the
built-in is dead for this document. Removing the built-in is safe and is the
correct resolution — the version number is not universal.

### Moved from code to tier 1 (leakage the plan identified)

These are doc-specific rules that ended up in the built-in "generic" list.

| Rule | Fires | Rationale |
|---|---|---|
| `4.28.1` → `4 dot 28 dot 1` | onboarding ×2 | A specific version number, not a general pattern. **Drop instead** — superseded by the tier-1 entry above. |
| `4.x` → `4 dot x` | onboarding ×1 | Same |
| `^4.0` → `caret 4 dot 0` | onboarding ×2 | Same. *Corrects the earlier estimate that this never fires.* |
| `uvx` → `U.V.X.` | onboarding ×2 | Tool-specific to this document's subject |
| `hijacked` | onboarding ×1 | **Kept at tier 4** — a Kokoro defect, not a topic word |
| `past-you` | onboarding ×2 | This document's rhetorical device |
| `~/.claude.json`, `.mcp.json`, `.claude.json` | exam ×11 | **Promote to regex instead** (see above) |
| `_force_verb_stress_heteronyms` (`delegates`) | exam ×3 | **Stays tier 4.** Initially demoted, then revised — the verb default degrades gracefully where the noun default does not. See D1 below. |
| `isRetryable` → `is retryable` | exam ×2 | → **tier 1** (exam guide). Names a specific API field. Note `_fix_retryable` does *not* cover it alone — the camelCase must be split first for the generic rule to fire, so the tier-1 entry must produce `is retryable` for the IPA to land. |
| `isError` → `is error` | exam ×2 | → **tier 1** (exam guide). Same. |
| `KEY=value` → `key=value` | onboarding ×1 | **Dropped** — superseded by `_spell_out_assignments`, which handles it and every other `X=Y` pair. |

---

## Tier 2 — user

**Empty.** The tier exists so personal preferences have a home without
retrofitting a tier later. Nothing in the current rule set is a personal
preference rather than a defect fix or a document fact.

---

## Dropped

Fires in neither source document, and no generalizable rule is hiding. Each
deletion is recorded with a reason so it is reviewable rather than silent.

| Rule | Note |
|---|---|
| `customer_id`, `order_id`, `session_id` | **Superseded by the `\b(\w+)_id\b` promotion** — the regex covers them, so no decision was needed |
| `Invalid JSON` | Fires nowhere. Only "invalidate" (lowercase verb) appears in either document, which this rule cannot match. |
| `future-you`, `present-you` | Fire nowhere. `past-you` is the only one used, and it is tier 1. |
| `4.28.1` (built-in copy) | Duplicated in the onboarding tier-1 file with a *different* replacement, which already wins on precedence. See the duplication note above. |

`TTL` and `XSS` were drop candidates on the fire-count criterion but are
**kept** at tier 3b as forward-looking org policy — a deliberate exception,
recorded in that section.

---

## Resolved judgment calls

The seven cases that were genuinely ambiguous, and how they were decided
(2026-08-03). Recorded so a future reader sees the reasoning, not just the
outcome.

| # | Rule | Decision | Reasoning |
|---|---|---|---|
| D1 | `delegates` → verb IPA | **tier 4** (universal) | *Revised — see below.* The two failure modes are not symmetric: verb stress on the noun is a reading many speakers genuinely use, while noun stress on the verb is wrong to everyone. Defaulting to the verb degrades gracefully. |
| D2 | `TTL` → `time to live` | **tier 3b** | Expand for a general audience. Org policy, not a defect — an infra-only audience would prefer "T-T-L". Kept despite firing nowhere. |
| D3 | `XSS` → `cross-site scripting` | **tier 3b** | Same shape as D2. Kept despite firing nowhere. |
| D4 | `CVE` → full expansion | **tier 3b** | Same shape. Fires in onboarding. |
| D5 | `2FA` | **split: 3b + tier 1** | The standalone expansion is org policy (3b). The ` (2FA)` deletion stays tier 1 — it depends on that document spelling out the term immediately before each parenthetical. |
| D6 | `README` → `[read](/ɹˈid/) me` | **tier 4** | "RED-me" is wrong in any technical document. A defect that sat in tier 1 by accident of where it was first hit. |
| D7 | `SemVer` → `Sem-Ver` | **tier 4** | Same as D6 — letter-by-letter spelling is wrong everywhere. |

D2–D5 land in tier 3b (org defaults) rather than tier 4 because they encode an
*audience* judgment, not a pronunciation defect: expanding every initialism is
right for our audience and would be presumptuous to impose on every user of a
public tool. Nothing about them is confidential — see the 3a/3b split.

### D1 revised: prefer the failure that degrades gracefully

`delegates` was first assigned to tier 1 on the reasoning that a heteronym is
only safe where the document's usage is known. That treated the two possible
errors as equivalent. They are not:

- **Verb stress on the noun** (`/dˈɛləɡˌeɪts/` for "the delegates arrived") is
  a pronunciation many English speakers actually use. A listener hears a
  variant, not a mistake.
- **Noun stress on the verb** (`/ˈdɛləɡəts/` for "the manager delegates work")
  is wrong to every listener. No one says it that way.

Since one error is survivable and the other is not, the universal default
should be the survivable one — and that holds in any document, which is what
makes it tier 4 rather than a per-document judgment.

**The general principle, worth applying to future heteronyms:** when a rule
must guess, prefer the guess whose failure mode a listener can absorb. Ask not
"which reading is correct more often?" but "which mistake is worse when it
happens?" Rules where both errors are equally jarring stay tier 1, where the
document settles the ambiguity.

---

## Counts

Counted, not estimated — every one of the 85 rules in play today was traced to
exactly one destination.

Each figure is the count of rules whose **destination** is that tier, wherever
they sit today.

| Destination | Count | Notes |
|---|---|---|
| 1 project | **41** | 35 onboarding + 2 exam guide (`isRetryable`, `isError`) + 4 doc-specific rules leaving code (`4.x`, `^4.0`, `uvx`, `past-you`) |
| 2 user | **0** | tier defined but empty by design |
| 3a company (confidential) | **5** | all from the onboarding file; the leak-scan gate |
| 3b org defaults (public) | **5** | `CVE`, `2FA`, `InterWorks` from onboarding; `TTL`, `XSS` from code |
| 4 universal | **25** | includes `README` and `SemVer` promoted up from onboarding, and `delegates` retained (D1) |
| promoted into regexes | **5** | 4 `*_id` literals → `_spell_out_id_suffix`; `KEY=value` → `_spell_out_assignments` |
| dropped | **4** | `Invalid JSON`, `future-you`, `present-you`, duplicate `4.28.1` |

**Reconciliation:** 41 + 0 + 5 + 5 + 25 + 5 + 4 = **85** — the exact count of
rules in play at the start (28 code literals + 2 dotfile entries + 10 regex
functions + 44 onboarding literals + 1 named pronunciation).

**Tier 4 as it stands in code today:** **15 literals** + 2 dotfile entries,
with the `rewrite_for_tts` pipeline grown to ~19 passes.

The literal count fell from 28 as each enumeration became a pattern:

| Promotion | Literals removed |
|---|---|
| `_spell_out_id_suffix` | 4 (`custom_id` and friends) |
| `_spell_out_assignments` | 1 (`KEY=value`) |
| `_spell_out_versions` | 1 (`4.28.1`) |
| `_spell_out_wildcard_versions` | 1 (`4.x`) |
| `_spell_out_hidden_dotted_names` | 3 (`.mcp.json` and friends) |

Plus 2 camelCase literals to tier 1 and 1 duplicate dropped. The package is
smaller *and* more general: every promotion covers inputs no source document
has used yet, which is the whole point of preferring a pattern to a list.

Every rule appears in exactly one section above, and each dropped rule carries
a reason.

## Migration record (2026-08-03)

The migration has been applied. What actually happened, including the one thing
that did not go to plan:

| File | Before | After |
|---|---|---|
| onboarding project file | 44 literals + 1 named | **34 literals + 0 named** |
| exam-guide project file | empty scaffold | **2 literals** (`isRetryable`, `isError`) |
| `narraoke-overrides` | seeded, unused | **10 literals + 1 named**, all firing |
| tier 4 (`rules/`) | 18 literals | **15 literals** |

**Verified against the real documents, not just fixtures.** Narrating all 1071
onboarding phrases produces byte-identical output before and after. The exam
guide changes in 5 phrases, all improvements: `isRetryable` and `isError` were
tier-4 literals for a document that had no override file, so they had never
fired; now they do, and `_fix_retryable` chains onto the split correctly.

**The one real bug — a cross-tier ordering break.** Rule C5 (the credential
identifier) is a *longer* name whose prefix is a separate, non-confidential
rule. The original file listed them longer-first, with a `why` reading
*"Longer name first to avoid partial-match."* Migrating only the longer name to
tier 3 split that pair across tiers — and because project runs before company,
the shorter project rule began matching first, leaving the tail of the longer
name unnarrated.

The fix was to move both rules to tier 3 so the pair stays adjacent. The
general lesson, worth applying to any future migration:

> **A substring pair must not be split across tiers.** Precedence is
> application order, so moving one half of a longer-first pair to a less
> specific tier inverts the order that made it correct. Move the whole pair or
> neither.

The lint reports this class of break (`can never fire`), but only *after* the
damage is visible in output — it flags the dead rule, not the corrupted text.
The full-document narration diff is what actually caught it.

**Backups.** Neither content directory is under version control, so
`<file>.bak` copies were written before editing. The backups still contain all
five tier-3a rules; the migrated files are leak-scan clean. That difference is
the proof the rules moved rather than being deleted.

## Verification

Moving a rule must not change audio. Both checks are required:

1. **Golden test** — `uv run python -m pytest tests/ -q`, sub-second.
2. **Migration equivalence** — `rewrite(text, project=pre, company=EMPTY)`
   must equal `rewrite(text, project=post, company=company)`. Keep the "pre"
   fixture permanently as the migration witness, **with the five tier-3a rules
   redacted to placeholders**, since it lives in the public repo. The tier-3b
   rules need no redaction.
3. **Full-render backstop, once per course** — diff `<slug>.srt` and
   `<slug>_timings.json` against the current `latest/` run.
