# narraoke

**Karaoke for your docs.**

Turn a structured markdown document into a narrated, teleprompter-style
scrolling video. The active phrase is highlighted in sync with the narration —
*narrator* + *karaoke*, hence the name.

---

## How it works

```
markdown → styled HTML → tall headless-Chromium screenshot
        → per-phrase keyframes sliced from that screenshot
        → ffmpeg concat, muxed with Kokoro TTS audio
```

1. Split the markdown into narration phrases (skipping table sections).
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
| `--overrides` | auto-discovered sibling | Path to a `.tts-overrides.json` rule file |
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
| **2 User** | all your projects, private to you | `${XDG_CONFIG_HOME:-~/.config}/narraoke/rules.d/*.json` |
| **3 Company** | shared with a group | a cloned private repo, path set in `narraoke.config.json` |
| **4 Universal** | everyone | Python literals, packaged with the app |

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

### Rule ordering is semantics

Rules apply sequentially over a mutating buffer, so **order is meaning, not
style**. Longer patterns must precede shorter ones they contain
(`~/.claude.json` before `.claude.json`). Never sort or dedupe these lists.

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
├── html_to_video.py   # The live tool — CLI entry point (`narraoke`)
├── tts_engine.py      # Kokoro TTS synthesis
├── timing.py          # Phrase timing + SRT/JSON output
├── utils.py           # Shared helpers (slugify, ffmpeg checks, logging)
├── scripts/
│   └── leak_scan.py   # Raw-bytes denylist scan for confidential material
├── tests/             # Golden-file tests for the rule pipeline
├── .mise.toml         # Toolchain pins (uv, python)
├── renovate.json      # Dependency automation
├── pyproject.toml     # Project metadata + uv config
└── uv.lock            # Pinned versions + sha256 hashes (source of truth)
```

### Legacy path

`article_to_video.py` (with `extractor.py` and `video_gen.py`) is a superseded
URL→video tool kept behind an optional extra:

```bash
uv sync --extra legacy
```

It is not the product. Changes belong in `html_to_video.py`.

---

## License

No license is granted. All rights reserved.
