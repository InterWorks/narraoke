# narraoke — working notes

Turns a structured markdown document into a narrated, teleprompter-style
scrolling video: markdown → styled HTML → tall headless-Chromium screenshot →
per-phrase keyframes → ffmpeg concat, muxed with Kokoro TTS audio.

## Never sort, dedupe, or reorder the TTS rule lists

**Order is semantics, not style.** `_apply_literal_overrides` is a sequential
`str.replace` over a mutating buffer, and `rewrite_for_tts` runs a hand-tuned
12-step sequence: ranges expand before quote/paren wrapping, IPA escapes land
last so their brackets and slashes are not reprocessed. Several regexes carry
`(?!\]\(/)` guards against double-wrapping.

Inline comments like "longer first" are **load-bearing**. `~/.claude.json` must
precede `.claude.json`; `retryability` must precede `retryable`; `Invalid JSON`
must precede `JSON`. Reordering silently changes generated audio.

This is the mistake most likely to be made by a well-meaning linter or a
tidy-up commit. `tests/test_rewrite_for_tts.py` will catch it — run it.

Once the rules live in the `rules/` package, the same warning extends to
**import statements**: order comes from the explicit `ORDERED_RULE_SOURCES`
list in `rules/__init__.py`, never from import order. Reordering imports must
stay harmless.

## Two apps live here

`html_to_video.py` is the live tool and the product. `article_to_video.py`
(+ `extractor.py`, `video_gen.py`) is a **superseded** URL→video path, kept
behind the `legacy` optional-dependency extra. Changes belong in
`html_to_video.py` unless the task is explicitly about the legacy path.

## Verification shortcut

`rewrite_for_tts` is a pure `str -> str` function with no I/O, so rule changes
are testable in **under a second**:

```bash
uv run python -m pytest tests/ -q
```

A full render is **~16 minutes**. Never re-render to check a string rewrite.
Use `--skip-tts` for visual iteration. When you deliberately change rule
behaviour, regenerate the golden files with
`uv run python tests/regenerate_golden.py` and *read the diff* — never
regenerate to make a failing test pass.

## The four rule tiers

| Tier | Scope | Lives in |
|---|---|---|
| 1 Project | one document | `<markdown>.tts-overrides.json`, beside the source |
| 2 User | all my projects, private | `${XDG_CONFIG_HOME:-~/.config}/narraoke/rules.d/*.json` |
| 3 Company | shared with a group | `InterWorks/narraoke-overrides` (private), path set in `config.json` |
| 4 Universal | everyone | Python literals in `html_to_video.py` |

Tier 3 splits into **3a** (confidential — the leak-scan gate) and **3b** (org
defaults: shared, but public technology terms). Keeping them in separate files
is what makes "everything in the confidential file is confidential" true.

Rule files carry `literal`, `named_pronunciations`, and `regex` sections.
**`regex` accepts string replacements only — never a Python callable.** That
restriction is what keeps a user config directory or a cloned company repo
from becoming an arbitrary-code-execution path. Rules needing conditional
logic stay in tier 4 as reviewed Python.

Full tier assignment for every rule, with reasoning: [docs/rule-triage.md](docs/rule-triage.md).

Precedence: **project → user → company → universal** (most specific first).

Choosing a tier: *would this make sense to a stranger who has never heard of
our employer or clients, in any technical document?* If yes, tier 4.

## Confidentiality

Never put company or client names, internal channels, or contacts in tier-4
(in-repo) rules. **The `why` field is part of the rule** — a rationale can leak
an internal name even when `from` and `to` look innocuous. This is not
hypothetical; it is why `scripts/leak_scan.py` scans raw bytes rather than
parsed fields.

```bash
uv run python scripts/leak_scan.py
```

**This repository is public.** Run the scan before pushing.

## Never commit `output/`, `.venv/`, `.venv-win/`

33.4GB of a 34GB tree. `output/` also holds full narration transcripts
(`.srt`), so excluding it is a confidentiality control, not just hygiene.

## Performance shape (counterintuitive)

TTS needs the GPU — roughly **160x** slower on CPU, turning a 2-minute stage
into ~5 hours. Encoding barely benefits — about **1.4x** — because the
bottleneck is PNG decode, not the encoder. Do not move encode work to the GPU
expecting a win. ~78% of a run is ffmpeg.

`torch` resolves through the pinned `pytorch-cu124` index, so a CUDA-variant
bump can silently drop TTS to the CPU path with no error. Renovate holds torch
for dashboard approval for exactly this reason.

## External prerequisites

`mise` pins `uv` and `python` only. `ffmpeg`, `chromium`, `espeak-ng`, and
`fonts-dejavu-core` are system-managed. Before adopting a mise-provided ffmpeg,
`ffmpeg -encoders | grep -E 'h264_nvenc|libx264'` must show **both** — a build
without NVENC falls back to libx264 silently.

`_find_font` falls back to a tiny bitmap font rather than crashing, so missing
fonts degrade title cards **silently**.

---

A working plan may exist at `docs/PLAN.md`. It is untracked and may be absent.
