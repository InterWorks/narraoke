"""Tests for the code-block summary advisory.

A code block is never read out line by line. While the camera dwells on it,
narration comes from either a `tts-summary` comment the author wrote or the
generic `_default_code_summary` fallback. Both render successfully, which is
why the fallback is easy to ship by accident: nothing fails, the video just
spends the length of a code block saying nothing about it.

This warning is advisory. It flags a quality gap, not the kind of defect
`_check_phrase_coverage` catches, where the highlight genuinely stalls.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.argv = ["pytest"]

import narraoke as h  # noqa: E402


def _blocks(md: str) -> list[dict]:
    """Parse markdown text through the real loader, via a temp file."""
    import tempfile

    path = Path(tempfile.mkdtemp()) / "doc.md"
    path.write_text(md, encoding="utf-8")
    return h.load_narration_blocks(path)


def test_an_authored_summary_is_silent() -> None:
    blocks = _blocks(
        "# T\n\n"
        "<!-- tts-summary: This installs the dependencies. -->\n"
        "```bash\nuv sync\n```\n"
    )
    assert h._check_code_summaries(blocks) == []


def test_a_code_block_without_a_summary_is_reported() -> None:
    blocks = _blocks("# T\n\n```python\nx = 1\n```\n")
    messages = h._check_code_summaries(blocks)
    assert len(messages) == 1
    assert "1 code block(s)" in messages[0]
    assert "python" in messages[0]


def test_the_language_tag_is_named_so_the_block_can_be_found() -> None:
    """The message has to help locate the block; a bare count would not."""
    blocks = _blocks("# T\n\n```yaml\na: 1\n```\n\n```sql\nSELECT 1\n```\n")
    messages = h._check_code_summaries(blocks)
    assert "sql" in messages[0] and "yaml" in messages[0]
    assert "2 code block(s)" in messages[0]


def test_an_untagged_fence_is_labelled_rather_than_blank() -> None:
    """`_default_code_summary("")` yields "A code block follows." — the
    language is empty, and an empty entry in the list would read as a typo."""
    blocks = _blocks("# T\n\n```\nplain\n```\n")
    assert "untagged" in h._check_code_summaries(blocks)[0]


def test_tables_are_never_reported() -> None:
    """Table narration is built from the table's own cells, so there is no
    authored-versus-fallback distinction to draw and nothing to warn about."""
    # The trailing prose line matters: a table is flushed by the first
    # non-table line after it, so a fixture ending on `| 1 | 2 |` would leave
    # the buffer unflushed and emit no table block at all.
    blocks = _blocks("# T\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nAfter.\n")
    assert any(b.get("tts_summary_for_table") for b in blocks)
    assert h._check_code_summaries(blocks) == []


def test_a_summary_only_attaches_to_the_next_code_block() -> None:
    """The pending summary is consumed by the first fence that closes after
    it. A second block does not inherit it, and must still be reported."""
    blocks = _blocks(
        "# T\n\n"
        "<!-- tts-summary: Explains the first one. -->\n"
        "```py\nfirst = 1\n```\n\n"
        "```py\nsecond = 2\n```\n"
    )
    messages = h._check_code_summaries(blocks)
    assert "1 code block(s)" in messages[0]


def test_a_summary_comment_inside_a_fence_is_not_treated_as_a_summary() -> None:
    """Inside a fence the comment is code content, not a directive — the
    loader guards on `not in_code_fence`. The block is still unsummarised."""
    blocks = _blocks(
        "# T\n\n```md\n<!-- tts-summary: not a directive here -->\n```\n"
    )
    messages = h._check_code_summaries(blocks)
    assert len(messages) == 1
