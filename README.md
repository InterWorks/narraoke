# narraoke

**Karaoke for your docs.**

Turn a document into a narrated, teleprompter-style scrolling video. The
active phrase is highlighted in sync with the narration — *narrator* +
*karaoke*, hence the name.

Two entry points, for two kinds of source:

| | Input | Rendering |
|---|---|---|
| **`narraoke`** | structured markdown | styled HTML, screenshotted by a headless browser — keeps code blocks, tables, and typography |
| **`narraoke-article`** | a URL, text file, or pasted prose | text drawn onto generated frames with PIL — no browser, no structure assumed |

Most of this README covers `narraoke`; see [the article path](#the-article-path)
for the other.

---

## How it works

```
markdown → styled HTML → tall headless-Chromium screenshot
        → per-phrase keyframes sliced from that screenshot
        → ffmpeg concat, muxed with Kokoro TTS audio
```

1. Split the markdown into narration phrases. Code blocks and tables are not
   read out; each gets a spoken summary, then the camera dwells on it or
   scrolls through it while you read.
2. Render a video-only HTML page wrapping every phrase in a locatable span.
3. Synthesise TTS audio with Kokoro.
4. Generate per-phrase timing data.
5. Screenshot the whole page to one tall PNG; pull each phrase's Y coordinate.
6. Slice a 1280x720 frame per phrase so the active line sits ~1/3 down the
   viewport, with a translucent highlight overlay.
7. Stitch with ffmpeg's concat demuxer and mux in the audio.

---

## Requirements

| Requirement | Version | Managed by |
|---|---|---|
| Python | 3.10 | `mise` |
| uv | pinned | `mise` |
| ffmpeg | 4.4+ with `h264_nvenc` **and** `libx264` | system |
| chromium | any recent | system |
| espeak-ng | any | system |
| fonts-dejavu-core | — | system |
| NVIDIA GPU + CUDA 12.x | optional but strongly recommended | system |

**The GPU is not optional in practice.** Kokoro TTS is roughly **160x** slower
on CPU — a 2-minute stage becomes about 5 hours. Encoding, by contrast, is only
~1.4x faster on GPU, because the bottleneck is PNG decode rather than the
encoder itself.

---

## Installation

```bash
mise install
uv sync
```

`mise` pins the toolchain (`uv`, `python`); `uv` owns Python dependencies via
`uv.lock`, which pins exact versions and verifies every wheel's sha256.

The four system prerequisites above are **not** managed by mise. On Debian or
Ubuntu:

```bash
sudo apt install ffmpeg chromium espeak-ng fonts-dejavu-core
```

Verify the encoders — both must be present, or encodes silently fall back to
the slower CPU path:

```bash
ffmpeg -encoders | grep -E 'h264_nvenc|libx264'
```

The Kokoro model (~327MB) downloads to the Hugging Face cache on first run and
is reused offline thereafter.

To also use the article path, install its extraction stack:

```bash
uv sync --extra article
```

Common commands are defined as mise tasks, so the invocation is
version-controlled rather than living in shell history. mise requires a
config to be trusted before it will run anything from it:

```bash
mise trust
mise run test
mise run leak-scan
```

---

## Usage

```bash
uv run narraoke <markdown-path> [options]
```

### Options

| Option | Default | Description |
|---|---|---|
| `markdown` | — | Path to the source markdown file (positional) |
| `--voice` | `af_heart` | Kokoro voice |
| `--output-dir` | `output` | Output directory |
| `--slug` | derived from filename | Custom slug for output filenames |
| `--smooth` | off | ffmpeg `minterpolate` between keyframes (slow; can artifact at low scroll speeds) |
| `--skip-tts` | off | Reuse audio and timings from the most recent prior run |
| `--sections` | all | Render only these sections, e.g. `0,3-5`. Skips the full-length video |
| `--section-workers` | 4 | How many section MP4s to encode at once; `1` is sequential |
| `--preview` | off | Open the primary output when the run finishes |
| `--overrides` | auto-discovered sibling | Path to a `.tts-overrides.json` rule file |
| `--video-config` | auto-discovered sibling | Path to a `.video.json` render-settings file |
| `--company-rules` | from `narraoke.config.json` | Directory of shared company rules (tier 3) |
| `--user-rules` | from `narraoke.config.json` | Directory of personal rules (tier 2) |
| `--no-split-sections` | off | Skip per-section MP4s |

### Examples

```bash
uv run narraoke docs/onboarding.md --voice af_heart
```

Iterating on visuals? Skip the expensive TTS stage — it reuses the previous
run's audio:

```bash
uv run narraoke docs/onboarding.md --skip-tts
```

Checking one part of a long document? Render just those sections and skip the
full-length encode, which is the most expensive single step:

```bash
uv run narraoke docs/onboarding.md --skip-tts --sections 0,3-5 --preview
```

Sections are independent and render concurrently by default. On the reference
document this is the largest single cost in a run, so `--section-workers`
is where the wall-clock time goes if you want to tune it.

Every stage reports its elapsed time as the next one begins, and the run ends
with a breakdown of where the time went plus any warnings raised — so a real
defect is the last thing on screen rather than buried mid-log:

```
Time: 16m 02s total
    6m 52s    43%  Rendering 11 per-section MP4(s)
    6m 09s    38%  Encoding video
    1m 53s    12%  Synthesising audio

1 warning(s) during this run:
  ! Coord count (1044) doesn't match phrase count (1071). Highlight alignment may drift.
```

Each run writes to a fresh timestamped version directory under
`output/<slug>/`, so prior renders are never overwritten.

---

## Per-document settings

Resolution, pacing, narration speed, and which sections to skip live in a
companion file beside the markdown — no code edit required:

```
docs/onboarding.md
docs/onboarding.md.video.json
docs/onboarding.md.tts-overrides.json
```

```jsonc
{
  // Sections to drop wholesale, matched against ## headings. They reach
  // neither the narration nor the rendered page.
  "skip_headings": ["Quick reference card"],

  // Overrides the <title> and title cards. Defaults to the document's H1.
  "title": "Working Safely in the Org",

  "width": 1280,
  "height": 720,
  "fps": 30,
  "read_zone": 0.33,          // 0.0 top edge, 1.0 bottom; 0.33 reads as a teleprompter
  "narration_speed": 1.1,

  "lead_in_seconds": 1.0,     // silence before the first word
  "tail_out_seconds": 1.0,    // silence after the last
  "scroll_px_per_second": 75, // pace when scrolling tall code blocks
  "dwell_max_s": 60.0         // cap so one huge block cannot stall the video
}
```

Every field is optional, and the file itself is optional — omit anything and
the default applies. A bad value warns and falls back rather than aborting a
16-minute render. `--video-config PATH` overrides the auto-discovered file.

## Pronunciation rules

Kokoro mispronounces plenty of technical vocabulary. narraoke fixes this with
layered rule files that rewrite narration text before synthesis — for example
`JSON` → `[JSON](/ʤˈeɪsˌɑn/)` so it is spoken rather than spelled.

Rules resolve in four tiers, most specific first:

| Tier | Scope | Lives in |
|---|---|---|
| **1 Project** | one document | `<markdown>.tts-overrides.json`, beside the source |
| **2 User** | all your projects, private to you | any directory of `*.json`, set as `user_rules_dir` |
| **3 Company** | shared with a group | a cloned private repo, set as `company_rules_dir` |
| **4 Universal** | everyone | Python literals in `rules/`, packaged with the app |

Precedence is **project → user → company → universal**. Each tier can shadow
the more general ones beneath it.

### Pointing at your rule directories

Tiers 2 and 3 live outside this repo, so their paths come from a config file at
the repo root. Copy the committed example and edit it:

```bash
cp narraoke.config.example.json narraoke.config.json
```

```jsonc
{
  "company_rules_dir": "../narraoke-overrides",
  "user_rules_dir": "~/.config/narraoke/rules.d"
}
```

The real file is gitignored — it holds paths specific to your machine. Both
keys are optional; omit one and that tier is simply empty.

**Relative paths resolve against the config file**, not your working directory,
so `"../narraoke-overrides"` means "a sibling of this checkout" wherever you
run narraoke from. `--company-rules` / `--user-rules` and the
`$NARRAOKE_COMPANY_RULES` / `$NARRAOKE_USER_RULES` env vars override it.

If a company path is set but does not exist, narraoke **fails** rather than
rendering without it — a silently-absent rule means a name gets mispronounced
in a delivered video. Leaving the key out entirely is fine and silent.

Choosing a tier — *would this rule make sense to a stranger who has never heard
of your employer or clients, in any technical document?* If yes, it is
universal. If it names a company, client, product, internal channel, or
contact, it belongs in the private company tier and **nowhere else**.

> **The `why` field is part of the rule.** If your rationale mentions something
> confidential, the rule is company-tier even when `from` and `to` look
> innocuous.

### What a rule file can contain

Three optional sections:

```jsonc
{
  "literal": [
    {"from": "SHA", "to": "Sha", "why": "reads as letters otherwise"}
  ],
  "named_pronunciations": [
    {"name": "Acme", "ipa": "/ˈækmi/", "hint": "AK-mee"}
  ],
  "regex": [
    {"pattern": "\\bCI/CD\\b", "replacement": "C.I. C.D.",
     "stage": "pre_ipa", "flags": ["IGNORECASE"]}
  ]
}
```

**`regex` takes string replacements only — never code.** Backreferences
(`\1`, `\g<name>`) work as usual, but a rule file can never supply a Python
callable. That restriction is what makes it safe to load a rules directory
someone else maintains: without it, a shared repo would be an
arbitrary-code-execution path.

`stage` is `pre_ipa` (default, before the pattern rules) or `post` (after
them). `flags` accepts `IGNORECASE`, `MULTILINE`, `DOTALL`, and `UNICODE` —
an allowlist, so a rule file stays reviewable by reading it. An invalid
pattern is reported at load time and that one rule is skipped; the rest of
the file still loads.

### Rule ordering is semantics

Rules apply sequentially over a mutating buffer, so **order is meaning, not
style**. Longer patterns must precede shorter ones they contain
(`~/.claude.json` before `.claude.json`). Never sort or dedupe these lists.

Files in a rules directory load in sorted order, so a `10-`/`20-` numeric
prefix fixes application order across files the same way list position does
within one.

---

## Development

`rewrite_for_tts` is a pure `str -> str` function, so the entire rule system is
testable in under a second with no render:

```bash
uv run python -m pytest tests/ -q
```

A full render takes **~16 minutes**; never re-render to verify a string
rewrite. When you deliberately change rule behaviour, regenerate the golden
files and review the diff:

```bash
uv run python tests/regenerate_golden.py
```

This repository is public. Scan for confidential material before pushing:

```bash
uv run python scripts/leak_scan.py
```

### Dependencies

Dependencies are pinned in `uv.lock` and updated by
[Renovate](https://docs.renovatebot.com/), extending the shared
`InterWorks/renovate-config` preset. Releases are held for a cooldown period
before a PR opens, so young releases have time to be caught and yanked.

`torch` is held for manual approval on the dependency dashboard: it resolves
through the pinned `pytorch-cu124` index, and a CUDA-variant change can
silently drop TTS to the 160x-slower CPU path without failing any test.

---

## Project structure

```
narraoke/
├── html_to_video.py   # Rich documents — CLI entry point (`narraoke`)
├── article_to_video.py # Plain prose — CLI entry point (`narraoke-article`)
├── extractor.py       #   its URL fetching + boilerplate stripping
├── video_gen.py       #   its PIL text-frame renderer
├── docconfig.py       # Per-document render settings
├── rules/             # Tier-4 pronunciation rules + the tier stack
├── tts_engine.py      # Kokoro TTS synthesis          (shared)
├── timing.py          # Phrase timing + SRT/JSON output (shared)
├── utils.py           # Slugify, ffmpeg checks, logging (shared)
├── scripts/
│   └── leak_scan.py   # Raw-bytes denylist scan for confidential material
├── tests/             # Golden-file tests for the rule pipeline
├── docs/
│   └── rule-triage.md # Which tier every rule belongs to, and why
├── narraoke.config.example.json  # Rule-directory paths — copy, don't edit
├── .mise.toml         # Toolchain pins (uv, python) + render/test/lint tasks
├── renovate.json      # Dependency automation
├── pyproject.toml     # Project metadata + uv config
└── uv.lock            # Pinned versions + sha256 hashes (source of truth)
```

### The article path

`article_to_video.py` (with `extractor.py` and `video_gen.py`) narrates
**plainer prose** from anywhere — a URL, a text file, or pasted input:

```bash
uv sync --extra article
uv run narraoke-article https://example.com/some-article
uv run narraoke-article --text-file notes.txt
```

It fetches and strips the article, then draws the text onto generated frames
with PIL. No browser, and no assumptions about document structure — which is
exactly what makes it work on input `narraoke` cannot handle.

Its dependencies are an optional extra so that rendering a local markdown
file, the common case, does not pull in an HTML-extraction stack.

Merging the two behind one auto-detecting entry point is tracked in
[issue #1](https://github.com/InterWorks/narraoke/issues/1).

---

## License

No license is granted. All rights reserved.
