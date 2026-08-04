"""Per-document render settings, loaded from a JSONC companion file.

Behaviour that used to require editing Python now lives beside the markdown:

    docs/onboarding.md
    docs/onboarding.md.video.json      <- these settings
    docs/onboarding.md.tts-overrides.json

The naming mirrors the `.tts-overrides.json` convention that already works,
so a document's companion files sort together and are obviously related.

JSONC rather than the plan's suggested TOML: `tomllib` is 3.11+ and this
project targets 3.10, so TOML would mean a new dependency. JSON reuses the
comment stripper the rule loader already has, and keeps one config format
across the whole app.

Every field is optional. An absent file means every default applies, which is
why introducing this could not change any existing render.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

# ── Defaults ─────────────────────────────────────────────────────────────────
# These are the values that were hardcoded in narraoke.py. Changing one
# here changes the default for every document; changing it in a document's
# .video.json changes it for that document only.

DEFAULT_VIDEO_WIDTH = 1280
DEFAULT_VIDEO_HEIGHT = 720
# One keyframe per phrase; ffmpeg's minterpolate fills motion between them.
DEFAULT_FPS = 30
# Where the active phrase sits vertically. 0.0 is the top edge, 1.0 the
# bottom. 0.33 (top third) reads like a teleprompter.
DEFAULT_READ_ZONE = 0.33
# Kokoro synthesis rate. Was duplicated in two places, which meant changing
# narration speed required knowing about both.
DEFAULT_NARRATION_SPEED = 1.1

DEFAULT_LEAD_IN_SECONDS = 1.0
DEFAULT_TAIL_OUT_SECONDS = 1.0
DEFAULT_TITLE_CARD_SILENT_SECONDS = 2.5

DEFAULT_SCROLL_PX_PER_SECOND = 75
DEFAULT_DWELL_BOTTOM_PAUSE_S = 1.5
DEFAULT_DWELL_FITS_PAUSE_S = 3.0
DEFAULT_DWELL_MIN_S = 1.5
DEFAULT_DWELL_MAX_S = 60.0

DEFAULT_CHUNK_CHARS = 500


@dataclass(frozen=True)
class DocConfig:
    """Render settings for one document.

    Frozen so a loaded config cannot drift mid-render — the same reasoning
    behind the immutable RuleStack.
    """

    # ── Content ──────────────────────────────────────────────────────────
    # Sections whose ## heading starts with one of these are dropped
    # wholesale — they never reach TTS or the rendered HTML. Was a module
    # constant naming one specific document's section.
    skip_headings: tuple[str, ...] = ()
    # Overrides the <title> and title card text. Defaults to the document's
    # H1, which is almost always what you want.
    title: str = ""

    # ── Video ────────────────────────────────────────────────────────────
    width: int = DEFAULT_VIDEO_WIDTH
    height: int = DEFAULT_VIDEO_HEIGHT
    fps: int = DEFAULT_FPS
    read_zone: float = DEFAULT_READ_ZONE

    # ── Narration ────────────────────────────────────────────────────────
    narration_speed: float = DEFAULT_NARRATION_SPEED
    chunk_chars: int = DEFAULT_CHUNK_CHARS

    # ── Pacing ───────────────────────────────────────────────────────────
    lead_in_seconds: float = DEFAULT_LEAD_IN_SECONDS
    tail_out_seconds: float = DEFAULT_TAIL_OUT_SECONDS
    title_card_silent_seconds: float = DEFAULT_TITLE_CARD_SILENT_SECONDS

    # ── Code-block / table dwell ─────────────────────────────────────────
    scroll_px_per_second: float = DEFAULT_SCROLL_PX_PER_SECOND
    dwell_bottom_pause_s: float = DEFAULT_DWELL_BOTTOM_PAUSE_S
    dwell_fits_pause_s: float = DEFAULT_DWELL_FITS_PAUSE_S
    dwell_min_s: float = DEFAULT_DWELL_MIN_S
    dwell_max_s: float = DEFAULT_DWELL_MAX_S

    # Where this came from, for the startup summary. Empty means defaults.
    source: str = ""

    @property
    def is_default(self) -> bool:
        return not self.source


def config_path_for(md_path: Path) -> Path:
    """The companion config path for *md_path*.

    `docs/onboarding.md` -> `docs/onboarding.md.video.json`, matching how
    `.tts-overrides.json` is derived.
    """
    return md_path.with_suffix(md_path.suffix + ".video.json")


def from_mapping(data: dict, source: str = "") -> tuple[DocConfig, list[str]]:
    """Build a DocConfig from parsed JSON. Returns `(config, warnings)`.

    Unknown keys are ignored.

    Unknown keys are ignored rather than rejected so a config written for a
    newer narraoke still loads on an older one — the same forward/backward
    compatibility the rule files have.

    A value of the wrong type falls back to the default rather than aborting:
    a typo in a config file should not cost a 16-minute render.
    """
    kwargs: dict[str, Any] = {}
    warnings: list[str] = []

    for name, default in (
        (f.name, f.default) for f in fields(DocConfig) if f.name != "source"
    ):
        if name not in data:
            continue
        value = data[name]
        try:
            if name == "skip_headings":
                if isinstance(value, str):
                    value = [value]
                if not isinstance(value, list) or not all(
                    isinstance(v, str) for v in value
                ):
                    raise TypeError("expected a list of strings")
                kwargs[name] = tuple(value)
            elif name == "title":
                if not isinstance(value, str):
                    raise TypeError("expected a string")
                kwargs[name] = value
            elif name in ("width", "height", "fps", "chunk_chars"):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise TypeError("expected an integer")
                if value <= 0:
                    raise ValueError("must be positive")
                kwargs[name] = value
            else:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise TypeError("expected a number")
                if value < 0:
                    raise ValueError("must not be negative")
                kwargs[name] = float(value)
        except (TypeError, ValueError) as e:
            warnings.append(f"{name}: {e}; using default {default!r}")

    return DocConfig(source=source, **kwargs), warnings


def load(md_path: Path, explicit: Path | None = None) -> tuple[DocConfig, list[str]]:
    """Load the config for *md_path*. Returns `(config, warnings)`.

    `explicit` overrides the auto-discovered companion path.
    """
    from narraoke import _strip_jsonc_comments  # local: avoids a cycle

    path = explicit or config_path_for(md_path)
    if not path.is_file():
        return DocConfig(), []

    try:
        data = json.loads(_strip_jsonc_comments(path.read_text(encoding="utf-8")))
    except Exception as e:
        return DocConfig(), [f"could not parse {path}: {e}; using defaults"]

    if not isinstance(data, dict):
        return DocConfig(), [f"{path} should contain a JSON object; using defaults"]

    return from_mapping(data, source=str(path))


def summary_lines(config: DocConfig) -> list[str]:
    """Human-readable lines describing what a config changed.

    Only non-default values are listed: an operator needs to see what this
    document does differently, not re-read every default.
    """
    if config.is_default:
        return ["  (defaults — no .video.json found)"]

    defaults = DocConfig()
    lines = [f"  from {config.source}"]
    for f in fields(DocConfig):
        if f.name == "source":
            continue
        value = getattr(config, f.name)
        if value != getattr(defaults, f.name):
            lines.append(f"    {f.name} = {value!r}")
    if len(lines) == 1:
        lines.append("    (every value matches the defaults)")
    return lines
