"""Tests for the phrase-coverage check.

This replaced a comparison of coordinate count against phrase count, which
warned on every render of any document containing a table or a code block —
so the warning fired constantly and meant nothing.

Those two numbers were never meant to match. Code-block and table summaries
are narrated but deliberately have no span: `render_video_html` skips them,
and `build_keyframes` points the camera at the code or table being described
instead. Highlighting prose that is not on screen would be wrong.

What matters is that every phrase resolves to *something* — its own span, or
a visual element it is anchored to. A phrase resolving to neither stalls the
highlight, and that is a real defect.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.argv = ["pytest"]

import narraoke as h  # noqa: E402


def _spans(*indices: int) -> list[dict]:
    return [{"id": f"narr-{i}", "top": 0, "height": 10, "line": 0} for i in indices]


def test_every_phrase_with_a_span_is_fine() -> None:
    blocks = [{"kind": "p", "phrase_indices": [0, 1]}]
    assert h._check_phrase_coverage(["a", "b"], blocks, _spans(0, 1)) == []


def test_table_summary_without_a_span_is_not_a_problem() -> None:
    """The case that made the old warning fire on every real document.

    The summary is narrated; the camera dwells on the table it describes.
    No span is expected, and none is missing.
    """
    blocks = [
        {"kind": "p", "tts_summary_for_table": True, "phrase_indices": [0, 1]},
        {"kind": "table", "phrase_indices": []},
    ]
    assert h._check_phrase_coverage(["a", "b"], blocks, []) == []


def test_code_summary_is_treated_the_same_as_a_table() -> None:
    """Both flags follow the same path; neither is special."""
    blocks = [
        {"kind": "p", "tts_summary_for_code": True, "phrase_indices": [0]},
        {"kind": "code", "phrase_indices": []},
    ]
    assert h._check_phrase_coverage(["a"], blocks, []) == []


def test_a_summary_with_no_following_visual_is_reported() -> None:
    """The genuine failure the check exists for.

    build_keyframes anchors a summary to the *next* code or table block and
    resets on anything else. A summary followed by ordinary prose therefore
    has nowhere to point, and its phrases stall the highlight.
    """
    blocks = [
        {"kind": "p", "tts_summary_for_table": True, "phrase_indices": [0, 1]},
        {"kind": "p", "phrase_indices": [2]},
    ]
    messages = h._check_phrase_coverage(["a", "b", "c"], blocks, _spans(2))
    assert len(messages) == 1
    assert "neither a span nor a visual anchor" in messages[0]
    assert "0, 1" in messages[0]


def test_a_plain_phrase_missing_its_span_is_reported() -> None:
    """Real drift in the ordinary path must still be caught."""
    blocks = [{"kind": "p", "phrase_indices": [0, 1]}]
    messages = h._check_phrase_coverage(["a", "b"], blocks, _spans(0))
    assert len(messages) == 1
    assert "1 phrase(s)" in messages[0]


def test_long_orphan_lists_are_truncated() -> None:
    """A document that goes badly wrong should not print hundreds of indices."""
    blocks = [{"kind": "p", "phrase_indices": list(range(40))}]
    messages = h._check_phrase_coverage(["x"] * 40, blocks, [])
    assert "40 total" in messages[0]
    assert messages[0].count(",") < 12


def test_the_real_documents_produce_no_warning() -> None:
    """The onboarding document has two tables and previously warned on every
    render. It must now be silent, because nothing was ever wrong with it."""
    import re
    import tempfile

    import docconfig

    md = Path(
        "/home/matteorr/sandbox/git/github/interworks-morr/vibe_sop/docs/"
        "github-org-onboarding.md"
    )
    if not md.is_file():
        pytest.skip("source document not available on this machine")

    config, _ = docconfig.load(md)
    blocks = h.load_narration_blocks(md, skip_headings=config.skip_headings)
    phrases, annotated = h.build_phrase_index(blocks)
    out = Path(tempfile.mkdtemp()) / "page.html"
    h.render_video_html(annotated, out)
    spans = [
        {"id": f"narr-{i}"}
        for i in re.findall(r'id="narr-(\d+)"', out.read_text(encoding="utf-8"))
    ]
    # Fewer spans than phrases, by design — and that is not a defect.
    assert len(spans) < len(phrases)
    assert h._check_phrase_coverage(phrases, annotated, spans) == []
