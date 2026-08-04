# narraoke — working notes

Turns a document into a narrated, teleprompter-style scrolling video. The main
path: markdown → styled HTML → tall headless-Chromium screenshot → per-phrase
keyframes → ffmpeg concat, muxed with Kokoro TTS audio.

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

## Two entry points live here

`html_to_video.py` (`narraoke`) handles **richly formatted documents**:
structured markdown, rendered as styled HTML and screenshotted by a headless
browser. `article_to_video.py` (`narraoke-article`, + `extractor.py` and
`video_gen.py`) handles **plainer prose** — a URL, a text file, or pasted
input — behind the `article` optional-dependency extra.

**Neither supersedes the other.** They take different inputs and use
incompatible renderers: `html_to_video` screenshots styled HTML through a
headless browser so code blocks and tables survive; `article_to_video` draws
text onto blank frames with PIL, which is what lets it work on input that has
no structure to preserve. Both share `utils`, `tts_engine`, and `timing`.

Work on whichever matches the input class in question. Merging them behind one
auto-detecting entry point is tracked in [issue #1](https://github.com/InterWorks/narraoke/issues/1), not assumed.

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
| 3 Company | shared with a group | `InterWorks/narraoke-overrides` (private), path set in `narraoke.config.json` |
| 4 Universal | everyone | Python literals in `rules/`, split by defect type |

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

Tier 2 and 3 paths come from `narraoke.config.json` at the repo root —
**gitignored**, with `narraoke.config.example.json` committed alongside it as
the schema. The config is repo-local rather than in `~/.config` so a checkout
is self-contained. Relative paths in it resolve against the config file, not
the working directory, so `"../narraoke-overrides"` holds however narraoke is
invoked. A `--company-rules` / `--user-rules` flag or the `$NARRAOKE_*` env
vars override it.

Choosing a tier: *would this make sense to a stranger who has never heard of
our employer or clients, in any technical document?* If yes, tier 4.

## Two kinds of config, deliberately separate

| File | Scope | Tracked? |
|---|---|---|
| `narraoke.config.json` | the machine — where the rule directories are | no |
| `<markdown>.video.json` | one document — resolution, pacing, skip_headings, title | with the document |

Render settings belong to the *document*, not the machine, so they sit beside
the markdown like `.tts-overrides.json` does. `docconfig.py` owns the defaults;
`apply_doc_config` rebinds the module constants once per run, because those
constants are read from ~30 sites and threading a config object through every
signature would be a large refactor of code whose only end-to-end test is a
16-minute render.

`SKIP_HEADINGS` now defaults to empty. It was hardcoded to one document's
section name, which meant every document rendered with that document's setting.

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
