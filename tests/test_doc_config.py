"""Tests for per-document render settings.

The property that matters most: an absent config file must leave every render
exactly as it was, since that is what let this land without re-verifying every
existing video.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.argv = ["pytest"]

import docconfig  # noqa: E402
import html_to_video as h  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_module_constants():
    """apply_doc_config rebinds module state; put it back after each test."""
    yield
    h.apply_doc_config(docconfig.DocConfig())


def _write(tmp_path: Path, data: dict, name: str = "doc.md") -> Path:
    md = tmp_path / name
    md.write_text("# Title\n\nProse.\n", encoding="utf-8")
    docconfig.config_path_for(md).write_text(json.dumps(data), encoding="utf-8")
    return md


# ── defaults are the old hardcoded values ────────────────────────────────────

def test_defaults_match_the_previously_hardcoded_values() -> None:
    """Introducing the config file could not change any existing render."""
    h.apply_doc_config(docconfig.DocConfig())
    assert (h.VIDEO_WIDTH, h.VIDEO_HEIGHT) == (1280, 720)
    assert h.FPS == 30
    assert h.READ_ZONE == 0.33
    assert h.LEAD_IN_SECONDS == 1.0
    assert h.TAIL_OUT_SECONDS == 1.0
    assert h.TITLE_CARD_SILENT_SECONDS == 2.5
    assert h.SCROLL_PX_PER_SECOND == 75
    assert h.DWELL_MAX_S == 60.0
    assert h.DEFAULT_CHUNK_CHARS == 500


def test_missing_config_file_yields_defaults(tmp_path: Path) -> None:
    md = tmp_path / "no-config.md"
    md.write_text("# T\n", encoding="utf-8")
    config, warnings = docconfig.load(md)
    assert config == docconfig.DocConfig()
    assert config.is_default
    assert warnings == []


def test_narration_speed_has_one_source_of_truth() -> None:
    """The value was written out twice, so changing it meant knowing both."""
    import tts_engine

    assert tts_engine.NARRATION_SPEED == docconfig.DEFAULT_NARRATION_SPEED
    h.apply_doc_config(docconfig.DocConfig(narration_speed=0.9))
    assert tts_engine.NARRATION_SPEED == 0.9


# ── settings take effect ─────────────────────────────────────────────────────

def test_video_settings_are_applied(tmp_path: Path) -> None:
    md = _write(tmp_path, {"width": 1920, "height": 1080, "fps": 24,
                           "read_zone": 0.5})
    config, _ = docconfig.load(md)
    h.apply_doc_config(config)
    assert (h.VIDEO_WIDTH, h.VIDEO_HEIGHT) == (1920, 1080)
    assert h.FPS == 24
    assert h.READ_ZONE == 0.5


def test_pacing_settings_are_applied(tmp_path: Path) -> None:
    md = _write(tmp_path, {"lead_in_seconds": 2.5, "dwell_max_s": 30})
    config, _ = docconfig.load(md)
    h.apply_doc_config(config)
    assert h.LEAD_IN_SECONDS == 2.5
    assert h.DWELL_MAX_S == 30.0


def test_config_path_matches_the_overrides_convention(tmp_path: Path) -> None:
    md = tmp_path / "docs" / "onboarding.md"
    assert docconfig.config_path_for(md).name == "onboarding.md.video.json"


# ── skip_headings: the step-10 fix ───────────────────────────────────────────

def test_skip_headings_drops_a_section(tmp_path: Path) -> None:
    """A ## section named in skip_headings reaches neither TTS nor the page."""
    md = tmp_path / "doc.md"
    md.write_text(
        "# Title\n\nIntro prose.\n\n"
        "## Keep me\n\nKept prose.\n\n"
        "## Quick reference card\n\nDropped prose.\n",
        encoding="utf-8",
    )
    kept = h.load_narration_blocks(md, skip_headings=("Quick reference card",))
    everything = h.load_narration_blocks(md, skip_headings=())

    text = " ".join(b.get("text", "") for b in kept)
    assert "Dropped prose" not in text
    assert "Kept prose" in text
    assert len(kept) < len(everything)


def test_skip_headings_defaults_to_empty(tmp_path: Path) -> None:
    """The default must not name any one document's section.

    It was hardcoded to "Quick reference card", so every document rendered
    with the onboarding document's setting.
    """
    assert h.SKIP_HEADINGS == ()
    md = tmp_path / "doc.md"
    md.write_text("# T\n\n## Quick reference card\n\nProse.\n", encoding="utf-8")
    blocks = h.load_narration_blocks(md)
    assert any("Prose" in b.get("text", "") for b in blocks)


# ── title override ───────────────────────────────────────────────────────────

def test_title_defaults_to_the_h1(tmp_path: Path) -> None:
    md = tmp_path / "doc.md"
    md.write_text("# The Real Heading\n\nProse.\n", encoding="utf-8")
    blocks = h.load_narration_blocks(md)
    _, annotated = h.build_phrase_index(blocks)
    assert h._doc_title(annotated) == "The Real Heading"


def test_title_override_wins(tmp_path: Path) -> None:
    md = tmp_path / "doc.md"
    md.write_text("# The Real Heading\n\nProse.\n", encoding="utf-8")
    blocks = h.load_narration_blocks(md)
    _, annotated = h.build_phrase_index(blocks)
    assert h._doc_title(annotated, "Spoken Title") == "Spoken Title"


# ── malformed input degrades, never aborts ───────────────────────────────────

def test_bad_values_fall_back_with_a_warning(tmp_path: Path) -> None:
    """A typo in a config file must not cost a 16-minute render."""
    md = _write(tmp_path, {"width": "wide", "fps": -5, "skip_headings": 42})
    config, warnings = docconfig.load(md)
    assert config.width == 1280
    assert config.fps == 30
    assert config.skip_headings == ()
    assert len(warnings) == 3


def test_unparseable_file_falls_back(tmp_path: Path) -> None:
    md = tmp_path / "doc.md"
    md.write_text("# T\n", encoding="utf-8")
    docconfig.config_path_for(md).write_text("{ not json", encoding="utf-8")
    config, warnings = docconfig.load(md)
    assert config == docconfig.DocConfig()
    assert warnings and "could not parse" in warnings[0]


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    """Forward compatible: a config for a newer narraoke still loads."""
    md = _write(tmp_path, {"width": 1920, "some_future_option": True})
    config, warnings = docconfig.load(md)
    assert config.width == 1920
    assert warnings == []


def test_comments_are_supported(tmp_path: Path) -> None:
    """JSONC, so a config can explain itself the way rule files do."""
    md = tmp_path / "doc.md"
    md.write_text("# T\n", encoding="utf-8")
    docconfig.config_path_for(md).write_text(
        '// why this document is wider\n{ "width": 1920 }\n', encoding="utf-8"
    )
    config, warnings = docconfig.load(md)
    assert config.width == 1920
    assert warnings == []


def test_skip_headings_accepts_a_bare_string(tmp_path: Path) -> None:
    """`"skip_headings": "Appendix"` is the obvious mistake; accept it."""
    md = _write(tmp_path, {"skip_headings": "Appendix"})
    config, _ = docconfig.load(md)
    assert config.skip_headings == ("Appendix",)


def test_summary_lists_only_non_default_values(tmp_path: Path) -> None:
    """An operator needs to see what this document does differently."""
    md = _write(tmp_path, {"width": 1920})
    config, _ = docconfig.load(md)
    lines = "\n".join(docconfig.summary_lines(config))
    assert "width" in lines
    assert "dwell_max_s" not in lines
