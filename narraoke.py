#!/usr/bin/env -S uv run python
"""
narraoke — Turn a structured markdown doc into a narrated, scrolling video.

One of two entry points, distinguished by how the source is rendered:

  * this module renders **richly formatted documents**. The markdown becomes
    styled HTML, which a headless browser screenshots, so code blocks, tables,
    and typography survive into the video.
  * `narraoke_article.py` handles **plainer prose** — a URL, a text file, or
    pasted input — and draws the text onto generated frames with PIL. No
    browser, and no assumptions about document structure.

Neither supersedes the other: they take different inputs and use different
renderers. Merging them behind one auto-detecting entry point is tracked as a
separate piece of work.

This tool consumes a *local markdown file with known structure*. It:

  1. Splits the markdown into narration phrases (skipping any table sections).
  2. Renders a video-only HTML page where every phrase is wrapped in a
     <span class="narr" id="narr-N"> so we can locate it later.
  3. Synthesises TTS audio for the phrases (reusing tts_engine).
  4. Generates per-phrase timing data (reusing timing).
  5. Renders the whole HTML to one tall PNG via headless chromium, and pulls
     each phrase span's Y-pixel coordinate from the DOM dump.
  6. For each timed phrase, slices a 1280x720 frame out of the tall PNG so
     the active phrase sits ~1/3 down the viewport, with a translucent
     highlight overlay drawn on it.
  7. Stitches the frames with ffmpeg's concat demuxer, applies minterpolate
     to give the appearance of smooth scrolling between keyframes, and muxes
     in the MP3 to produce the final MP4.

Usage
-----
  uv run narraoke <markdown-path> [options]

  uv run narraoke docs/onboarding.md --voice af_heart --output-dir output

Equivalently, without the console script:

  uv run python narraoke.py <markdown-path> [options]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html as html_lib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ── Project root on path ──────────────────────────────────────────────────────
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import docconfig
import rules
from rules import discovery as _discovery
from rules.stack import (
    LiteralRule,
    NamedPronunciation,
    RegexRule,
    RegexRuleError,
    RuleSet,
    RuleStack,
)
from utils import (
    _format_duration,
    check_ffmpeg,
    chunk_sentences,
    collected_warnings,
    finish_stages,
    has_nvenc,
    info,
    slugify,
    split_phrases,
    split_sentences,
    stage_timings,
    step,
    warn,
)

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_VOICE = "af_heart"
DEFAULT_OUTPUT_DIR = Path("output")
DEFAULT_CHUNK_CHARS = 500

# Video parameters — match the on-screen reading experience as closely as the
# format allows. 16:9 at 1280x720 is a sweet spot for file size + clarity.
VIDEO_WIDTH = 1280
VIDEO_HEIGHT = 720
# Where the active phrase should sit vertically inside the viewport. 0.0 is
# the top edge, 1.0 is the bottom. 0.33 (top third) feels like a teleprompter.
READ_ZONE = 0.33
# Output frames per second. We render one keyframe per phrase + let ffmpeg's
# minterpolate fill in smooth motion between them.
FPS = 30

# Sections to drop wholesale, matched against ## headings.
#
# Empty by default: this was previously hardcoded to one specific document's
# section name, so every document rendered with that document's setting. It is
# now a per-document value — set `skip_headings` in `<markdown>.video.json`.
SKIP_HEADINGS: tuple[str, ...] = ()


# ────────────────────────────────────────────────────────────────────────────────
# 1. MARKDOWN LOADING + NARRATION PHRASE EXTRACTION
# ────────────────────────────────────────────────────────────────────────────────

def _default_code_summary(lang: str) -> str:
    """Generic narration line for a code block when no tts-summary precedes it.

    Reads the language tag aloud when present; otherwise a neutral "code block".
    The trailing period gives Kokoro a sentence boundary.
    """
    lang_label = {
        "js": "JavaScript", "ts": "TypeScript", "jsonc": "JSON",
        "yml": "YAML", "py": "Python", "sh": "shell", "bash": "shell",
        "md": "markdown",
    }.get(lang.lower(), lang)
    if lang_label:
        return f"A {lang_label} code block follows."
    return "A code block follows."


def load_narration_blocks(
    md_path: Path,
    skip_headings: tuple[str, ...] | None = None,
) -> list[dict]:
    """
    Parse a markdown file into a sequence of *narration blocks* — semantically
    typed chunks of source content that the rest of the pipeline can process.

    Returns a list of dicts with:
      - "kind": one of "h1", "h2", "h3", "h4", "p", "li", "li_num",
                       "blockquote_label", "blockquote_p"
      - "text": the raw text (markdown still has **bold**, *italic*, `code`)
      - "depth": nesting depth for lists (0 = top-level)

    Sections whose ## heading starts with an entry in *skip_headings* are
    dropped wholesale — they reach neither TTS nor the rendered HTML. The list
    comes from `skip_headings` in the document's `.video.json`; it was once a
    module constant naming one specific document's section, which meant every
    document rendered with that document's setting.
    """
    if skip_headings is None:
        skip_headings = SKIP_HEADINGS
    raw = md_path.read_text(encoding="utf-8")
    lines = raw.splitlines()

    blocks: list[dict] = []
    skip_this_section = False
    in_blockquote = False
    in_code_fence = False
    code_fence_lang = ""        # language tag captured from opening fence
    code_fence_buf: list[str] = []
    pending_tts_summary: str | None = None
    paragraph: list[str] = []
    # Tables: collected line-by-line. The buffer holds raw `| ... |` lines.
    # When the SECOND line of the buffer matches the `|---|---|` separator
    # pattern, we know we're in a table. The buffer is flushed when a
    # non-`|` line is seen.
    table_buf: list[str] = []

    def _is_table_row(s: str) -> bool:
        s = s.strip()
        return s.startswith("|") and s.endswith("|") and len(s) >= 2

    def _is_table_separator(s: str) -> bool:
        s = s.strip()
        if not (s.startswith("|") and s.endswith("|")):
            return False
        # Cell pattern is at least one `-`, optionally surrounded by `:` for
        # alignment, plus optional whitespace.
        cells = [c.strip() for c in s[1:-1].split("|")]
        return all(re.match(r"^:?-+:?$", c) for c in cells) and len(cells) >= 1

    def _parse_table_row(s: str) -> list[str]:
        s = s.strip()
        # Strip leading/trailing |, split on |, strip cells
        return [c.strip() for c in s[1:-1].split("|")]

    def flush_table():
        nonlocal table_buf
        if not table_buf:
            return
        # Need at least header + separator to be a table
        if len(table_buf) < 2 or not _is_table_separator(table_buf[1]):
            # Not a real table — emit as paragraph(s)
            for raw in table_buf:
                paragraph.append(raw.strip())
            table_buf = []
            return
        header = _parse_table_row(table_buf[0])
        rows = [_parse_table_row(r) for r in table_buf[2:] if _is_table_row(r)]
        # Spoken form: "Table. Columns: A, B, C. Row: a1, b1, c1. Row: …"
        def _spoken_cell(c: str) -> str:
            # Strip markdown formatting markers for narration
            c = re.sub(r"\*\*([^*]+)\*\*", r"\1", c)
            c = re.sub(r"\*([^*]+)\*", r"\1", c)
            c = re.sub(r"`([^`]+)`", r"\1", c)
            return c.strip()
        header_spoken = ", ".join(_spoken_cell(h) for h in header)
        rows_spoken = ". ".join(
            "Row: " + ", ".join(_spoken_cell(c) for c in r)
            for r in rows
        )
        spoken = f"Columns: {header_spoken}. {rows_spoken}."
        # Emit a `p` block that narrates the table (acts like tts_summary_for_code)
        # and a `table` block that renders visually.
        blocks.append({
            "kind": "p",
            "text": spoken,
            "depth": 0,
            "tts_summary_for_table": True,
        })
        blocks.append({
            "kind": "table",
            "header": header,
            "rows": rows,
            "depth": 0,
        })
        table_buf = []

    def flush_paragraph():
        nonlocal paragraph
        if paragraph:
            text = " ".join(paragraph).strip()
            if text:
                kind = "blockquote_p" if in_blockquote else "p"
                blocks.append({"kind": kind, "text": text, "depth": 0})
            paragraph = []

    list_stack: list[int] = []  # indentation of each open list level

    for raw_line in lines:
        line = raw_line.rstrip()

        # <!-- tts-summary: ... --> on its own line: a narration-only sentence
        # attached to the upcoming code block. The pipeline narrates it while
        # holding/scrolling on the code block. If no summary precedes a code
        # block, the loader falls back to "A <lang> code block follows."
        ts_m = re.match(r"^\s*<!--\s*tts-summary:\s*(.*?)\s*-->\s*$", line)
        if ts_m and not in_code_fence:
            flush_paragraph()
            pending_tts_summary = ts_m.group(1).strip()
            continue

        # <!-- tts-pause: N --> on its own line: insert N seconds of silence
        # into the audio at this point. The video holds the previous frame
        # for that duration. Used to give the listener time to think (e.g.
        # after multiple-choice question options, before the answer).
        tp_m = re.match(r"^\s*<!--\s*tts-pause:\s*(\d+(?:\.\d+)?)\s*-->\s*$", line)
        if tp_m and not in_code_fence:
            flush_paragraph()
            blocks.append({
                "kind": "pause",
                "text": "",
                "depth": 0,
                "seconds": float(tp_m.group(1)),
            })
            continue

        # <!-- tts-hidden-start --> / <!-- tts-hidden-end --> bracket a region
        # that should not be visible on-screen until narration reaches it.
        # We achieve this by inserting a vertical "spoiler-guard" spacer block
        # immediately at the start sentinel — pushing the wrapped content
        # below the natural scroll position the question phrases anchor to,
        # so the camera only reveals it when narration scrolls down. The end
        # sentinel is parsed (so it doesn't show up as text) but doesn't emit
        # a block.
        ths_m = re.match(r"^\s*<!--\s*tts-hidden-start\s*-->\s*$", line)
        if ths_m and not in_code_fence:
            flush_paragraph()
            blocks.append({
                "kind": "spoiler_guard",
                "text": "",
                "depth": 0,
            })
            continue
        the_m = re.match(r"^\s*<!--\s*tts-hidden-end\s*-->\s*$", line)
        if the_m and not in_code_fence:
            flush_paragraph()
            # No block emitted; the sentinel just terminates the hidden region.
            continue

        # Fenced code blocks: collect the content into its own block kind so
        # the renderer can show it on screen, but skip the content from TTS.
        # The accompanying narration comes from a tts-summary comment if one
        # was just seen; otherwise a generic line based on the language tag.
        fence_m = re.match(r"^\s*```(.*)$", line)
        if fence_m:
            if not in_code_fence:
                # Opening fence — flush any prose, start collecting code.
                flush_paragraph()
                in_code_fence = True
                code_fence_lang = fence_m.group(1).strip()
                code_fence_buf = []
            else:
                # Closing fence — emit the summary (narration) + the code
                # (visual) as two blocks, in that order.
                in_code_fence = False
                # `authored` records whether a human wrote this summary or the
                # generic fallback filled in. The distinction is invisible once
                # the text exists, but it is the difference between narration
                # that explains the code and narration that announces it, so
                # `_check_code_summaries` reports the fallbacks.
                authored = pending_tts_summary is not None
                summary_text = pending_tts_summary or _default_code_summary(code_fence_lang)
                pending_tts_summary = None
                code_text = "\n".join(code_fence_buf)
                blocks.append({
                    "kind": "p",
                    "text": summary_text,
                    "depth": 0,
                    "tts_summary_for_code": True,
                    "authored_summary": authored,
                    "code_lang": code_fence_lang,
                })
                blocks.append({
                    "kind": "code",
                    "text": code_text,
                    "depth": 0,
                    "lang": code_fence_lang,
                })
                code_fence_buf = []
                code_fence_lang = ""
            continue
        if in_code_fence:
            code_fence_buf.append(raw_line)
            continue

        # Markdown tables: collect `| ... |` lines into table_buf. We confirm
        # it's a real table once the second line matches the `|---|...|`
        # separator pattern. Otherwise flush_table emits them as paragraph.
        if _is_table_row(line):
            flush_paragraph()  # any prose before the table
            table_buf.append(line)
            continue
        if table_buf:
            # First non-table line ends the table.
            flush_table()

        # Section dividers (---) flush any open paragraph and reset state
        if line.strip() == "---":
            flush_paragraph()
            in_blockquote = False
            list_stack = []
            continue

        # Headings — also act as section gates
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            flush_paragraph()
            in_blockquote = False
            list_stack = []
            level = len(m.group(1))
            text = m.group(2).strip()

            # Skip whole-section gating: ## headings that match skip_headings
            if level == 2 and any(text.startswith(h) for h in skip_headings):
                skip_this_section = True
                continue
            # Any new ## heading clears the skip flag
            if level == 2:
                skip_this_section = False

            if skip_this_section:
                continue

            blocks.append({"kind": f"h{level}", "text": text, "depth": 0})
            continue

        if skip_this_section:
            continue

        # Blockquote markers (>)
        bq_match = re.match(r"^>\s*(.*)$", line)
        if bq_match:
            flush_paragraph()
            in_blockquote = True
            content = bq_match.group(1).strip()
            if not content:
                # `> ` on its own = soft separator inside the blockquote
                continue
            # Inline bold-label form: "> **Label:** ..." we keep as one block
            paragraph.append(content)
            continue
        # A blank line ends a blockquote
        if in_blockquote and not line.strip():
            flush_paragraph()
            in_blockquote = False
            continue

        # Unordered list items
        ul_m = re.match(r"^(\s*)-\s+(.*)$", line)
        if ul_m:
            flush_paragraph()
            indent = len(ul_m.group(1))
            text = ul_m.group(2).strip()
            depth = indent // 2  # markdown is 2-space-per-level here
            blocks.append({"kind": "li", "text": text, "depth": depth})
            continue

        # Ordered list items
        ol_m = re.match(r"^(\s*)\d+\.\s+(.*)$", line)
        if ol_m:
            flush_paragraph()
            indent = len(ol_m.group(1))
            text = ol_m.group(2).strip()
            depth = indent // 2
            blocks.append({"kind": "li_num", "text": text, "depth": depth})
            continue

        # Continuation of a list item (indented prose under a `- `)
        cont_m = re.match(r"^(\s{2,})(\S.*)$", line)
        if cont_m and blocks and blocks[-1]["kind"] in ("li", "li_num", "p"):
            blocks[-1]["text"] += " " + cont_m.group(2).strip()
            continue

        # Blank line ends a paragraph
        if not line.strip():
            flush_paragraph()
            continue

        # Plain paragraph text
        paragraph.append(line.strip())

    flush_paragraph()
    return blocks


def block_narration_text(block: dict) -> str:
    """Return the *plain text* version of a block for TTS narration.

    Strips markdown syntax (bold/italic/code) without removing the words.
    Handles nested cases like `**Document where it lives, *as you put it there*.**`
    by stripping bold first (non-greedy across `*`s), then italic.
    """
    text = block["text"]
    # Drop link syntax [label](url) → label
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Drop paired bold markers FIRST, non-greedy and allowing inner content
    # (so a nested `*italic*` survives this pass and gets handled below).
    # Repeat until no more substitutions land — this avoids an infinite loop
    # when the source has an orphan `**` that can't be matched as a pair.
    while True:
        new_text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, count=1, flags=re.DOTALL)
        if new_text == text:
            break
        text = new_text
    # Now drop italic markers. The simple `\*[^*]+\*` is OK here because all
    # paired bold markers are gone (any leftover `**` is a single orphan).
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"\b_([^_]+)_\b", r"\1", text)
    # Drop inline code backticks but keep contents (Kokoro pronounces them OK
    # in context — e.g. `main` reads as "main")
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def build_phrase_index(blocks: list[dict]) -> tuple[list[str], list[dict]]:
    """
    Walk the blocks in order, split each block's narration text into phrases
    via the existing pipeline's splitters, and produce:

      - phrases: a flat list of phrase strings (input to TTS + timing)
      - annotated_blocks: each block dict augmented with a "phrase_indices"
        field — the indices into `phrases` that belong to it

    This double-bookkeeping is what lets the rendered HTML link each phrase
    span to the same index the audio timeline uses.
    """
    phrases: list[str] = []
    annotated: list[dict] = []

    code_idx = 0
    table_idx = 0
    for block in blocks:
        # Code blocks are visual-only: they get rendered on screen but no
        # narration phrases (the preceding `tts_summary_for_code` block
        # carries the audio). Annotate with empty phrase lists so downstream
        # rendering still tracks them as part of the block sequence. Each
        # code block gets a sequential id so frame composition can look up
        # its on-page Y position when dwelling on it.
        if block["kind"] == "code":
            annotated.append({
                **block,
                "phrase_indices": [],
                "phrase_texts": [],
                "code_idx": code_idx,
            })
            code_idx += 1
            continue
        # Tables work the same way: visual-only, paired with a preceding
        # tts_summary_for_table block that carries the narration.
        if block["kind"] == "table":
            annotated.append({
                **block,
                "phrase_indices": [],
                "phrase_texts": [],
                "table_idx": table_idx,
            })
            table_idx += 1
            continue
        # Pause sentinel: audio-only silent chunk, no visual or narration.
        # Frame composition holds the previous frame for the pause duration.
        if block["kind"] == "pause":
            annotated.append({
                **block,
                "phrase_indices": [],
                "phrase_texts": [],
            })
            continue
        # Spoiler-guard sentinel: visual-only blank spacer that pushes the
        # following content below the natural scroll target of preceding
        # phrases. No narration, no scrolling decisions.
        if block["kind"] == "spoiler_guard":
            annotated.append({
                **block,
                "phrase_indices": [],
                "phrase_texts": [],
            })
            continue

        narr = block_narration_text(block)
        # Headings are short — keep as a single phrase
        if block["kind"].startswith("h"):
            block_phrases = [narr] if narr else []
        else:
            sentences = split_sentences(narr)
            block_phrases = split_phrases(sentences)

        # Skip empty blocks (e.g. a horizontal rule reduced to nothing)
        if not block_phrases:
            continue

        idx_start = len(phrases)
        phrases.extend(block_phrases)
        annotated.append({
            **block,
            "phrase_indices": list(range(idx_start, len(phrases))),
            "phrase_texts": block_phrases,
        })

    return phrases, annotated


# ── TTS-only text rewrites ────────────────────────────────────────────────────
# These transforms apply ONLY to the text we feed Kokoro. The on-screen HTML and
# the phrase strings used for highlight matching stay unchanged.
#
# Kokoro's misaki frontend honors the markdown-link-style phoneme escape
# `[label](/IPA/)`: the label is what the listener sees in a transcript-style
# tool, the IPA in the parens is what gets synthesised.
#
# Two layers:
#   - Generic rules and a built-in literal list in this file: things that apply
#     to any markdown doc (heteronyms Kokoro mis-stresses, file extensions,
#     SCREAMING_SNAKE, etc.).
#   - Per-doc overrides loaded from a sibling JSONC file
#     (<markdown>.tts-overrides.json): proper-noun pronunciations, doc-specific
#     phrasing fixes. See `load_doc_overrides`.


def _emphasise_quoted_spans(text: str) -> str:
    """Add comma-pauses around MULTI-WORD double-quoted spans.

    `the "All Member" team`            -> `the, "All Member", team`
    `is named "All Member".`           -> `is named, "All Member".`

    Single-word quotes (`"main"`, `"repo"`, `"PR"`) are common shorthand and
    sound stuttery when comma-wrapped, so they're left alone.
    """
    def quote_sub(open_q: str, close_q: str, src: str) -> str:
        pat = re.compile(
            rf'(^|(?<=[A-Za-z0-9.,;:!?\)]))(\s+){re.escape(open_q)}([^{re.escape(close_q)}\n]{{2,60}}){re.escape(close_q)}(\s+|(?=[.,;:!?\)]))',
            re.UNICODE,
        )

        def repl(m: re.Match) -> str:
            head = m.group(1)
            leading_ws = m.group(2)
            span = m.group(3)
            trailing_ws = m.group(4)
            # Only emphasise multi-word spans
            if len(span.split()) < 2:
                return m.group(0)
            new_lead = ", " if leading_ws else leading_ws
            if trailing_ws and trailing_ws.startswith((" ", "\t")):
                new_trail = ", "
            else:
                new_trail = trailing_ws
            return f"{head}{new_lead}{open_q}{span}{close_q}{new_trail}"

        return pat.sub(repl, src)

    text = quote_sub('"', '"', text)
    text = quote_sub('“', '”', text)
    text = re.sub(r",(\s*,)+", ",", text)
    return text


def _emphasise_parentheticals(text: str) -> str:
    """Insert a comma before '(' if not already preceded by one, so the
    parenthetical reads as an aside rather than running into the prior word.
    """
    return re.sub(r"(\w)\s*\(", r"\1, (", text)


def _expand_numeric_ranges(text: str) -> str:
    """'30-45 minutes' -> '30 to 45 minutes' (and en/em-dash variants).

    Only triggers when both sides of the dash are digits, so identifiers like
    'main-branch' or 'pre-commit' stay untouched.
    """
    return re.sub(r"(\d+)\s*[-–—]\s*(\d+)", r"\1 to \2", text)


# Moved to `rules/passes.py` (registered as the "verb-stress-heteronyms"
# pass). The alias keeps the established private name working.
_force_verb_stress_heteronyms = rules.passes.force_verb_stress_heteronyms


# Generic literal-phrase overrides — apply to ANY narrated markdown doc.
# Doc-specific overrides go in a sibling JSONC file; see `load_doc_overrides`.
#
# The rules themselves live in the `rules/` package, split by the defect each
# one fixes, and are assembled in the explicit order named by
# `rules.ORDERED_RULE_SOURCES`. See rules/__init__.py before changing anything
# about their order — it is semantics, not style.
_LITERAL_TTS_OVERRIDES: list[tuple[str, str]] = rules.LITERAL_TTS_OVERRIDES


# ── The active rule stack ────────────────────────────────────────────────────
# Four tiers, resolved project -> user -> company -> universal. Tier 4 is
# populated from the `rules/` package; the rest are filled in as their sources
# are discovered. See rules/stack.py for the precedence rules.
#
# This is module-level state for the same reason `load_doc_overrides` was:
# the CLI renders exactly one document per process. `load_rule_file` itself is
# pure and returns a value, so the stack can be built and tested without any
# globals — only the *active* stack lives here.
_RULE_STACK: RuleStack = RuleStack.builtin_only(_LITERAL_TTS_OVERRIDES)

# Views onto the active stack, kept as module globals so existing call sites
# read unchanged. Rebuilt by `_refresh_rule_views` whenever the stack changes.
_DOC_LITERAL_OVERRIDES: list[tuple[str, str]] = []
_DOC_NAMED_IPA: list[tuple[str, str, str]] = []  # (display, ipa, hint)


def _refresh_rule_views() -> None:
    """Recompute the flat lists the rewrite functions read.

    The tier-4 literals stay in `_LITERAL_TTS_OVERRIDES` and the more specific
    tiers land in `_DOC_LITERAL_OVERRIDES`, preserving the existing two-list
    application order in `_apply_literal_overrides`: specific tiers first, then
    the built-ins.
    """
    global _DOC_LITERAL_OVERRIDES, _DOC_NAMED_IPA
    _DOC_LITERAL_OVERRIDES = [
        rule.as_pair()
        for tier in ("project", "user", "company")
        for rule in getattr(_RULE_STACK, tier).literals
    ]
    _DOC_NAMED_IPA = _RULE_STACK.named_tuples()


def set_rule_stack(stack: RuleStack) -> None:
    """Install *stack* as the active one and refresh the flat views."""
    global _RULE_STACK
    _RULE_STACK = stack
    _refresh_rule_views()


def active_rule_stack() -> RuleStack:
    """The stack currently in effect."""
    return _RULE_STACK


def _strip_jsonc_comments(text: str) -> str:
    """Strip // line and /* block */ comments from JSONC text.

    Skips comments that appear inside string literals. Good enough for
    hand-edited config files; not a robust JSONC parser.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    str_quote = ""
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == str_quote:
                in_str = False
            i += 1
            continue
        if ch in ('"', "'"):
            in_str = True
            str_quote = ch
            out.append(ch)
            i += 1
            continue
        # Line comment //
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            if j == -1:
                break
            i = j
            continue
        # Block comment /* */
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            if j == -1:
                break
            i = j + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def load_rule_file(path: Path, tier: str) -> RuleSet:
    """Parse one JSONC rule file into a `RuleSet`. Pure — returns a value.

    A missing file yields an empty RuleSet rather than an error: for tiers 1
    and 2 an absent file is the normal case. Callers that require a file to
    exist (the company tier, when a path has been explicitly configured)
    check for themselves and fail loudly.

    Unparseable files warn and yield empty, matching the prior behaviour —
    a malformed override file should not abort a 16-minute render.
    """
    if not path.is_file():
        return RuleSet(tier=tier, source=str(path))
    try:
        data = json.loads(_strip_jsonc_comments(path.read_text(encoding="utf-8")))
    except Exception as e:
        warn(f"Could not parse {path}: {e}")
        return RuleSet(tier=tier, source=str(path))

    origin = f"{tier}:{path.name}"
    literals: list[LiteralRule] = []
    for entry in data.get("literal", []):
        src = entry.get("from")
        dst = entry.get("to")
        if isinstance(src, str) and isinstance(dst, str):
            literals.append(
                LiteralRule(frm=src, to=dst, why=entry.get("why", "") or "",
                            origin=origin)
            )

    named: list[NamedPronunciation] = []
    for entry in data.get("named_pronunciations", []):
        name = entry.get("name")
        if isinstance(name, str):
            named.append(
                NamedPronunciation(
                    name=name,
                    ipa=entry.get("ipa", "") or "",
                    hint=entry.get("hint", "") or "",
                    why=entry.get("why", "") or "",
                    origin=origin,
                )
            )

    # Data-driven pattern rules. String replacements only — a rule file may
    # never supply a Python callable, which is what keeps a user config
    # directory or a cloned company repo from becoming a code-execution path.
    regexes: list[RegexRule] = []
    for entry in data.get("regex", []):
        if not isinstance(entry, dict):
            warn(f"  {path.name}: skipping non-object entry in \"regex\"")
            continue
        raw_flags = entry.get("flags", [])
        if isinstance(raw_flags, str):
            raw_flags = [raw_flags]
        try:
            regexes.append(
                RegexRule(
                    pattern=entry.get("pattern", ""),
                    replacement=entry.get("replacement", ""),
                    stage=entry.get("stage", "pre_ipa"),
                    flags=tuple(raw_flags),
                    why=entry.get("why", "") or "",
                    origin=origin,
                )
            )
        except RegexRuleError as e:
            # One bad rule must not cost a 16-minute render.
            warn(f"  skipping invalid regex rule: {e}")

    return RuleSet(
        tier=tier,
        source=str(path),
        literals=tuple(literals),
        named=tuple(named),
        regexes=tuple(regexes),
    )


def apply_doc_config(config: "docconfig.DocConfig") -> None:
    """Install *config*'s video and pacing values as the module constants.

    These constants are read from ~30 sites across the capture, keyframe, and
    encode functions. Rebinding them once here, before any render work starts,
    keeps the change small and reviewable; threading a config object through
    every signature would be a large refactor of code whose only end-to-end
    test is a 16-minute render.

    Called exactly once per process from `main`, immediately after the config
    is loaded — narraoke renders one document per run, the same assumption
    `load_doc_overrides` has always made.
    """
    global VIDEO_WIDTH, VIDEO_HEIGHT, FPS, READ_ZONE
    global LEAD_IN_SECONDS, TAIL_OUT_SECONDS, TITLE_CARD_SILENT_SECONDS
    global SCROLL_PX_PER_SECOND, DWELL_BOTTOM_PAUSE_S, DWELL_FITS_PAUSE_S
    global DWELL_MIN_S, DWELL_MAX_S, DEFAULT_CHUNK_CHARS

    VIDEO_WIDTH = config.width
    VIDEO_HEIGHT = config.height
    FPS = config.fps
    READ_ZONE = config.read_zone
    LEAD_IN_SECONDS = config.lead_in_seconds
    TAIL_OUT_SECONDS = config.tail_out_seconds
    TITLE_CARD_SILENT_SECONDS = config.title_card_silent_seconds
    SCROLL_PX_PER_SECOND = config.scroll_px_per_second
    DWELL_BOTTOM_PAUSE_S = config.dwell_bottom_pause_s
    DWELL_FITS_PAUSE_S = config.dwell_fits_pause_s
    DWELL_MIN_S = config.dwell_min_s
    DWELL_MAX_S = config.dwell_max_s
    DEFAULT_CHUNK_CHARS = config.chunk_chars

    # Narration speed lives in tts_engine, which owns synthesis. Imported
    # here rather than at module scope so the 4.2GB torch stack still loads
    # lazily — importing it eagerly would slow every --help by seconds.
    import tts_engine
    tts_engine.NARRATION_SPEED = config.narration_speed


def load_app_config() -> dict:
    """Read `~/.config/narraoke/config.json`, or `{}` when absent.

    JSONC so the same comment convention as the rule files works here too.
    A malformed config warns rather than aborting: the tiers it points at are
    optional, and a render should not die on a stray comma.
    """
    path = _discovery.config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(_strip_jsonc_comments(path.read_text(encoding="utf-8")))
    except Exception as e:
        warn(f"Could not parse {path}: {e}")
        return {}
    if not isinstance(data, dict):
        warn(f"{path} should contain a JSON object; ignoring it")
        return {}
    return data


def _load_rules_from_dir(directory: Path, tier: str) -> RuleSet:
    """Compose every `*.json` in *directory* into one RuleSet.

    Files apply in sorted order, so a `10-`/`20-` prefix convention fixes
    rule order across files the same way list position does within one.
    """
    literals: list[LiteralRule] = []
    named: list[NamedPronunciation] = []
    regexes: list[RegexRule] = []
    sources: list[str] = []
    for path in _discovery.rule_files_in(directory):
        rule_set = load_rule_file(path, tier=tier)
        if rule_set.is_empty:
            continue
        literals.extend(rule_set.literals)
        named.extend(rule_set.named)
        regexes.extend(rule_set.regexes)
        sources.append(path.name)
    return RuleSet(
        tier=tier,
        source=f"{directory} ({', '.join(sources)})" if sources else str(directory),
        literals=tuple(literals),
        named=tuple(named),
        regexes=tuple(regexes),
    )


def build_rule_stack(
    project_path: Path | None = None,
    company_rules: str | None = None,
    user_rules: str | None = None,
) -> RuleStack:
    """Assemble all four tiers into the stack this run will use.

    Tier 4 always loads. Tiers 1-3 are each optional; an absent tier
    contributes nothing, which is why installing this machinery before any
    rule moved was provably a no-op.
    """
    config = load_app_config()
    stack = RuleStack.builtin_only(_LITERAL_TTS_OVERRIDES)

    company_dir = _discovery.resolve_company_rules_dir(company_rules, config)
    if company_dir is not None:
        stack = stack.with_tier("company", _load_rules_from_dir(company_dir, "company"))

    user_dir = _discovery.resolve_user_rules_dir(user_rules, config)
    if user_dir is not None:
        stack = stack.with_tier("user", _load_rules_from_dir(user_dir, "user"))

    if project_path is not None and project_path.is_file():
        stack = stack.with_tier("project", load_rule_file(project_path, "project"))

    return stack


def report_rule_stack(stack: RuleStack) -> None:
    """Log every resolved tier, its source, and its rule count.

    An operator must be able to see at a glance which tiers are live —
    especially that the company tier loaded, since a silently-absent tier-3
    rule means a client name is mispronounced in a delivered video.
    """
    info("Rule tiers:")
    for line in stack.summary():
        info(line)

    for name, first, second in stack.conflicting_names():
        warn(
            f"  '{name}' is defined in both {first.origin or first.name} and "
            f"{second.origin or second.name} with different IPA; "
            f"the more specific tier wins"
        )

    for message in stack.lint():
        warn(f"  rule shadowed: {message}")


def load_doc_overrides(path: Path) -> None:
    """Load per-doc TTS overrides from a JSONC companion file.

    Expected shape (all keys optional):
        {
          "literal": [
            {"from": "places this secret lives",
             "to":   "places this secret [lives](/lˈɪvz/)",
             "why":  "Kokoro picks noun stress; force the verb." }
          ],
          "named_pronunciations": [
            {"name": "Acme", "ipa": "/ˈækmi/",
             "hint": "AK-mee", "why": "..." }
          ]
        }
    """
    rule_set = load_rule_file(path, tier="project")
    set_rule_stack(_RULE_STACK.with_tier("project", rule_set))
    if rule_set.is_empty and not path.is_file():
        return
    info(
        f"  Loaded {len(rule_set.literals)} literal + "
        f"{len(rule_set.named)} named-pronunciation override(s) "
        f"from {path.name}"
    )


def _apply_doc_named_pronunciations(text: str) -> str:
    """Apply per-doc named-pronunciation overrides (proper nouns etc.).

    For each entry, replace the bare CamelCase/literal form of the name with
    misaki's IPA-escape `[Name](/ipa/)` syntax. Matches the same word-boundary
    rule used by the original company-name rewrite: not preceded by a letter,
    slash, dot, or @, and not followed by letters or `/`.
    """
    for name, ipa, _hint in _DOC_NAMED_IPA:
        if not ipa:
            continue
        replacement = f"[{name}]({ipa})"
        pattern = re.compile(
            rf"(?<![A-Za-z/.@]){re.escape(name)}(?![A-Za-z./])"
        )
        text = pattern.sub(replacement, text)
    return text


def _spell_out_vs(text: str) -> str:
    """Rewrite the abbreviation 'vs' / 'vs.' to the full word 'versus'.

    Kokoro normally reads 'vs' as 'versus' but mis-reads it as 'veez' in some
    chunked contexts. The full word is unambiguous and adds negligible duration
    to the audio. Anchored with `\\b` so it doesn't touch words like 'verse'.
    Consumes any trailing period so 'vs.' doesn't leave a stray '.'.
    """
    return re.sub(r"\bvs\b\.?", "versus", text)


# The word-level IPA passes formerly defined here now live in
# `rules/passes.py`, registered in `passes.ORDERED_PASSES` and applied by
# stage. These aliases keep the established private names working for callers
# and tests; the behaviour is identical.
_fix_enum = rules.passes.fix_enum
_fix_transient = rules.passes.fix_transient
_fix_copied = rules.passes.fix_copied
_fix_retryable = rules.passes.fix_retryable


def _apply_literal_overrides(text: str) -> str:
    # Per-doc overrides first (so a doc can shadow a generic rule), then the
    # built-in generic overrides.
    for src, dst in _DOC_LITERAL_OVERRIDES:
        text = text.replace(src, dst)
    for src, dst in _LITERAL_TTS_OVERRIDES:
        text = text.replace(src, dst)
    return text


# Digit -> word for a single leading version component. Kokoro reads a bare
# leading "4" as the homophone "for" ("for dot 28 dot 1"); spelling the first
# component out fixes that, and applying it to every single digit keeps the
# reading consistent rather than special-casing one number.
_VERSION_ONES = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}

# Pre-release / build identifiers that should be narrated as words.
_VERSION_SUFFIX_WORDS = {
    "alpha", "beta", "rc", "build", "dev", "pre", "post", "snapshot", "final",
}

# A version is 3+ dot-separated integers, no whitespace anywhere, with an
# optional leading "v" and an optional -prerelease / +build suffix.
#
#   (?<![\w.])      not preceded by a word char or dot — rejects "x1.2.3" and
#                   the tail of a longer dotted name
#   (\d+(?:\.\d+){2,})  three or more integer components. Requiring 3+ keeps
#                   "section 1.2" and "Python 3.11" out of the rule.
#   (?![\w])(?!\.\d)    not followed by a word char, and not by a dot+digit —
#                   so "1.2.3." at the end of a sentence matches while
#                   "1.2.3.4" is consumed as a single four-part version.
# Named _SEMVER_RE, not _VERSION_RE: the latter is already taken further down
# by the output-directory timestamp pattern (2026-08-03T14-44-05). Reusing the
# name silently shadowed this one and broke --skip-tts version scanning.
_SEMVER_RE = re.compile(
    r"(?<![\w.])(v)?(\d+(?:\.\d+){2,})"
    r"((?:[-+][A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)?)(?![\w])(?!\.\d)"
)


# Wildcard version lines: 4.x, 4.2.x, v2.14.X.
#
# Safe to generalise where a plain two-part version is not, because the "x"
# placeholder cannot be a decimal fraction — "4.x" is unambiguously a version
# line, whereas "4.0" is indistinguishable from the number four point zero and
# is deliberately left alone.
#
#   (?<![\w.])   not preceded by a word char or dot — rejects "app.x", "x.4"
#   (\d+(?:\.\d+)*)\.([xX])   one or more integer components then a literal x
#   (?![\w.])    not followed by a word char or dot — rejects "4.xyz", "4.X1"
_WILDCARD_VERSION_RE = re.compile(
    r"(?<![\w.])(v)?(\d+(?:\.\d+)*)\.([xX])(?![\w.])"
)


def _spell_out_wildcard_versions(text: str) -> str:
    """Narrate wildcard version lines like "4.x" as "four dot x".

    Kokoro drops the dot, collapsing "4.x" into something like "four ex". The
    dot is what marks it as a version line rather than a stray letter.

    Kept separate from `_spell_out_versions` because the matching rules differ:
    that rule requires three or more integer components to stay clear of
    "section 1.2", while this one is safe with just two, since a trailing "x"
    can never be a decimal fraction.

    An uppercase "X" narrates as lowercase — both are spoken "ex", and the
    lowercase form is the conventional spelling of a version wildcard.
    """
    def _replace(match: re.Match) -> str:
        v_prefix, core, wildcard = match.group(1), match.group(2), match.group(3)
        spoken = " dot ".join(
            (_VERSION_ONES[part] if i == 0 and len(part) == 1 else part)
            for i, part in enumerate(core.split("."))
        )
        spoken += " dot " + wildcard.lower()
        return ("version " + spoken) if v_prefix else spoken

    return _WILDCARD_VERSION_RE.sub(_replace, text)


def _spell_out_versions(text: str) -> str:
    """Narrate dotted version numbers so the separators are audible.

    Kokoro silently drops the dots in "1.2.3", collapsing a version into a
    run of bare numerals. Spelling each dot restores it.

    Replaces a hardcoded enumeration: three literals lived in the built-in
    list and sixteen more were hand-written in one document's override file,
    including five arrow-pair rules that were pure combinatorics. None of them
    covered a version string no one had typed out in advance.

    Handles the shapes that actually occur in changelogs and dependency docs:
    a "v" prefix, and `-rc.1` / `-alpha` / `+build.27` suffixes.

    Deliberately narrow — three or more integer components with no whitespace.
    Two-part forms are excluded because "section 1.2" and "Python 3.11" are far
    more common in prose than two-part version strings.
    """
    def _component(token: str, is_first: bool = False) -> str:
        if is_first and len(token) == 1:
            return _VERSION_ONES[token]
        return token

    def _replace(match: re.Match) -> str:
        v_prefix, core, suffix = match.group(1), match.group(2), match.group(3)
        spoken = " dot ".join(
            _component(part, i == 0) for i, part in enumerate(core.split("."))
        )
        if v_prefix:
            # A bare "v" is read as the letter; say the word instead.
            spoken = "version " + spoken
        if suffix:
            separator, remainder = suffix[0], suffix[1:]
            tokens: list[str] = []
            for token in remainder.split("."):
                lowered = token.lower()
                if lowered == "rc":
                    tokens.append("R.C.")
                elif lowered in _VERSION_SUFFIX_WORDS:
                    tokens.append(lowered)
                elif token.isdigit():
                    tokens.append(_component(token, True))
                else:
                    tokens.append(token)
            spoken += (" plus " if separator == "+" else " ") + " ".join(tokens)
        return spoken

    return _SEMVER_RE.sub(_replace, text)


def _spell_out_assignments(text: str) -> str:
    """Narrate `LEFT=RIGHT` as "LEFT equals RIGHT".

    Kokoro drops `=` entirely, so `DEBUG=true` is read as "DEBUG true" — the
    assignment, which is the whole point of the token, is inaudible. Speaking
    the operator restores it for env vars, CLI flags, and config pairs alike.

    This generalises a former literal that only covered the placeholder
    `KEY=value` from one document, and so missed real pairs like
    `CLAUDE_HEADLESS=true`.

    Deliberately narrow to avoid touching operators:

      * The lookbehind rejects a `=` preceded by any of `= ! < > ~ + * / % ^ & | -`,
        so compound assignments (`+=`, `-=`, `*=`) and comparisons (`==`,
        `!=`, `<=`, `>=`) are left alone.
      * `(?!=)` after the `=` rejects `==` from the other side.
      * Hyphens are excluded from the identifier class (they would swallow the
        `-` of `x-=2`), but an optional leading `--` or `-` is allowed so CLI
        flags like `--voice=af_heart` still narrate.
      * A spaced `a = b` is left alone — Kokoro already reads that as prose.
    """
    return _ASSIGNMENT_RE.sub(
        lambda m: (m.group(1) or "") + m.group(2) + " equals " + m.group(3),
        text,
    )


# Compiled once: the assignment pattern is applied to every narrated phrase.
_ASSIGNMENT_RE = re.compile(
    r"(?<![=!<>~+*/%^&|-])(--?)?\b([A-Za-z_][A-Za-z0-9_.]*)=(?!=)([A-Za-z0-9_./+-]+)"
)


def _spell_out_id_suffix(text: str) -> str:
    """Rewrite `<word>_id` so Kokoro says "<word> I-D" rather than one word.

    Snake-case identifiers ending in `_id` read as a single mushed word
    ("custom-id", "order-id"); letter-dotting the "I.D." gives the intended
    two-beat reading.

    This replaces a hardcoded enumeration of the four `*_id` names one source
    document happened to use. The pattern is the rule — any `*_id` identifier
    gets the same treatment, including ones no document has used yet.

    Anchored on `\\w+` before the underscore so a bare `_id` is left alone,
    and on `\\b` after so `some_idea` is untouched.
    """
    return re.sub(r"\b(\w+)_id\b", r"\1 I.D.", text)


# Dotfile rewrites — defined in rules/filenames.py alongside the other
# filename rules. Anchored so paths like "/foo.env" and compounds like
# ".envrc" stay untouched.
_DOTFILE_NARRATION: list[tuple[str, str]] = rules.DOTFILE_NARRATION


# Hidden dotted config files: an optional "~/" home prefix, a leading dot, and
# two or more dot-separated segments (.claude.json, .eslintrc.json).
#
#   (?<![\w.])   left boundary — not after a word char or dot, so the tail of
#                a longer path like "/foo.bar.json" is not claimed
#   (~/)?        optional home prefix, narrated as the word "home"
#   \.([A-Za-z]…(?:\.…)+)   leading dot then 2+ segments, each starting with a
#                letter. Requiring an internal dot is what separates this from
#                bare dotfiles (.npmrc, .editorconfig), which stay in
#                _DOTFILE_NARRATION as explicit entries — a rule matching any
#                ".word" would fire on sentence fragments and abbreviations.
#   (?!\w)(?!\.[A-Za-z0-9])   right boundary, allowing a sentence-ending
#                period but rejecting a continuation dot
_HIDDEN_DOTTED_RE = re.compile(
    r"(?<![\w.])(~/)?"
    r"\.([A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z][A-Za-z0-9_-]*)+)"
    r"(?!\w)(?!\.[A-Za-z0-9])"
)


def _spell_out_hidden_dotted_names(text: str) -> str:
    """Narrate hidden dotted config files: ".claude.json" -> "dot claude dot json".

    Kokoro drops the dots and mushes the parts together (".mcp.json" reads as
    "MCP-jay-sahn"), so the listener never hears the actual filename.

    `_spell_out_dotted_names` already covers the non-hidden case generically —
    "app.yaml", "config.json", anything. Its left boundary deliberately rejects
    a leading dot, so hidden files fell through unless someone hardcoded them.
    This rule closes that gap: it replaces three literals (`~/.claude.json`,
    `.mcp.json`, `.claude.json`) that only ever matched themselves, and now
    covers `.eslintrc.json`, `.prettierrc.json`, and any other hidden config
    file no document has used yet.

    A "~/" prefix narrates as the word "home". Without it the path would sound
    identical to the non-home variant, which is a real distinction when a
    document contrasts the two.
    """
    def _replace(match: re.Match) -> str:
        home_prefix, remainder = match.group(1), match.group(2)
        spoken = "dot " + remainder.replace(".", " dot ")
        return ("home " + spoken) if home_prefix else spoken

    return _HIDDEN_DOTTED_RE.sub(_replace, text)


def _spell_out_dotted_names(text: str) -> str:
    """Rewrite filename-style dotted names so Kokoro speaks the dots aloud.

    Catches things like:
      package-lock.json  -> "package-lock dot json"
      composer.lock      -> "composer dot lock"
      requirements.txt   -> "requirements dot txt"
      uv.lock            -> "uv dot lock"
      example.com        -> "example dot com"

    Avoids:
      ~/.aws/credentials (preceded by /)
      e.g. / i.e.        (both halves are single letters — abbreviation)
      4.x / 4.28.1       (leading digit — version string; handled by
                          _spell_out_wildcard_versions / _spell_out_versions)
      .env / .gitignore  (handled by _spell_out_dotfiles)
      Section 7.         (sentence-ending period)
    """
    # Word starts with a letter, contains letters/digits/underscore/hyphen.
    # Dot must be flanked by such word characters on each side; both halves
    # must contain at least one letter (so pure-numeric versions are skipped).
    # Right anchor rejects a continuation dot like ".ts.1" (would indicate
    # the match is actually a longer dotted name) but ALLOWS a sentence-end
    # dot like "auth.ts." (the period after .ts is plain English punctuation).
    pat = re.compile(
        r"(?<![\w.])"                                              # left boundary: not after word/dot
        r"([A-Za-z][A-Za-z0-9_-]*\.[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z][A-Za-z0-9_-]*)*)"
        r"(?!\w)"                                                  # right boundary: not before word
        r"(?!\.[A-Za-z0-9])"                                       # not followed by a continuation dot
    )

    def repl(m: re.Match) -> str:
        name = m.group(1)
        # Skip abbreviations where every segment is a single letter (e.g, i.e)
        parts = name.split(".")
        if all(len(p) == 1 for p in parts):
            return name
        return name.replace(".", " dot ")

    return pat.sub(repl, text)


def _spell_out_dotfiles(text: str) -> str:
    """Rewrite standalone dotfile names to forms Kokoro reads as distinct
    words rather than one mushy run-on syllable.

    Anchor: preceded by start, whitespace, or punctuation (NOT a letter/digit
    or another dot, so paths and compound names stay untouched). Followed by
    end, whitespace, or punctuation (NOT a letter/digit, so "..gitignore-like"
    extensions aren't matched).
    """
    for src, dst in _DOTFILE_NARRATION:
        # Build a regex specific to this dotfile so each can have a different
        # anchor character class if we ever need it.
        pat = re.compile(
            rf"(^|(?<=[\s\(\[\"'/`—–-])){re.escape(src)}(?=$|[\s\)\]\"',.;:!?`—–])",
        )
        text = pat.sub(lambda m, d=dst: f"{m.group(1)}{d}", text)
    return text


def rewrite_for_tts(text: str) -> str:
    """Apply all TTS-only rewrites in a fixed order.

    Order matters: range expansion before quote/paren wrapping (so '30-45' in
    a quote still becomes '30 to 45'), and IPA escapes last (the IPA tokens
    contain brackets and slashes we don't want re-processed).
    """
    text = _apply_literal_overrides(text)
    # Data-driven regexes, stage 1: after the literal pass, before any rule
    # below emits an IPA escape. See rules/stack.py REGEX_STAGES.
    text = _RULE_STACK.apply_regexes(text, "pre_ipa")
    # Wildcard lines before plain versions: "4.2.x" starts with a version-
    # shaped prefix, so the stricter rule would otherwise claim part of it.
    text = _spell_out_wildcard_versions(text)
    # Versions before dotted names: "1.2.3" must be read as a version, not as
    # a filename-style dotted token by _spell_out_dotted_names below.
    text = _spell_out_versions(text)
    text = _spell_out_assignments(text)
    text = _spell_out_id_suffix(text)
    # Tier-4 word-level IPA passes, in the order declared by
    # rules/passes.ORDERED_PASSES. Adding one is a registration there, not an
    # edit here — but the *stage's* position in this sequence is still
    # hand-tuned and load-bearing: it runs after the version/identifier passes
    # and before the dotted-name passes.
    text = rules.apply_passes(text, "word_ipa")
    text = _spell_out_vs(text)
    # Hidden dotted names first: ".claude.json" must be claimed as a whole
    # before _spell_out_dotfiles or _spell_out_dotted_names see part of it.
    text = _spell_out_hidden_dotted_names(text)
    text = _spell_out_dotfiles(text)
    text = _spell_out_dotted_names(text)
    text = _expand_numeric_ranges(text)
    text = _emphasise_quoted_spans(text)
    text = _emphasise_parentheticals(text)
    # Tier-4 passes that must see the emphasised form (quote and paren
    # wrapping insert punctuation their anchors match against).
    text = rules.apply_passes(text, "emphasis")
    # Data-driven regexes, stage 2: after every built-in pattern rule, for
    # rules that need to see their output.
    text = _RULE_STACK.apply_regexes(text, "post")
    # Per-doc named pronunciations land last so their IPA tokens aren't
    # re-processed by earlier passes.
    text = _apply_doc_named_pronunciations(text)
    return text


def build_tts_chunks(
    annotated_blocks: list[dict],
    phrases: list[str],
    max_chars: int = DEFAULT_CHUNK_CHARS,
) -> list[dict]:
    """
    Build a list of TTS chunks that preserves heading boundaries.

    Each chunk is a dict:
        {"text": str, "phrase_indices": list[int]}

    Rules:
      - Every heading block (h1..h4) becomes its OWN chunk. The chunk text has
        a period appended (heading prose lacks terminal punctuation, which made
        Kokoro glue headings to the following sentence). Putting the heading in
        a dedicated chunk also forces a chunk-boundary silence after it.
      - Non-heading phrases are packed greedily up to `max_chars` per chunk.
      - Phrase index continuity is preserved end-to-end so highlight timing can
        be distributed *within* each chunk's measured audio duration.
    """
    chunks: list[dict] = []
    cur_text: list[str] = []
    cur_indices: list[int] = []
    cur_chars = 0

    def flush():
        nonlocal cur_text, cur_indices, cur_chars
        if cur_text:
            joined = " ".join(cur_text)
            chunks.append({
                "text": rewrite_for_tts(joined),
                "phrase_indices": cur_indices,
            })
            cur_text = []
            cur_indices = []
            cur_chars = 0

    for block in annotated_blocks:
        kind = block["kind"]
        indices = block["phrase_indices"]

        # Pause sentinel: attach the requested silence to the END of the
        # most-recently-emitted chunk via a "trailing_pause_seconds" tag.
        # main() folds that into silence_after_chunk[] so synthesise_article
        # splices the silence into the audio after that chunk's last phrase.
        # We never emit a chunk for the pause itself — it'd be empty-text
        # which Kokoro can't synthesise.
        if kind == "pause":
            flush()
            if chunks:
                chunks[-1]["trailing_pause_seconds"] = (
                    chunks[-1].get("trailing_pause_seconds", 0.0)
                    + float(block.get("seconds", 0.0))
                )
            continue

        if not indices:
            continue

        if kind.startswith("h"):
            # Heading: flush whatever's queued, then emit it as its own chunk.
            # Append a period so TTS treats it as a sentence and pauses after.
            flush()
            heading_text = " ".join(phrases[i] for i in indices).strip()
            # For h2 headings that lead with a numeric section prefix, speak as
            # "Section N: Title." Two forms:
            #   "## 3. Title"      -> "Section 3: Title."
            #   "## 1.1 Title"     -> "Section 1 dot 1: Title."   (no period
            #                          required after dotted-number prefix)
            # Only TTS-narration text is rewritten; on-screen text + phrase
            # index used for highlight matching are unchanged.
            if kind == "h2":
                # Dotted-number form first, so "1.1" doesn't get partially
                # eaten by the bare-N rule below.
                m = re.match(
                    r"^\s*(\d+(?:\.\d+)+)\.?\s+(.+?)\s*$",
                    heading_text,
                )
                if m:
                    spoken_num = m.group(1).replace(".", " dot ")
                    heading_text = f"Section {spoken_num}: {m.group(2)}"
                else:
                    # Single integer "N. Title". Require whitespace OR end of
                    # string after the period so "1.1" isn't matched as N=1.
                    m = re.match(
                        r"^\s*(\d+)\.(?:\s+|$)(.*?)\s*$",
                        heading_text,
                    )
                    if m:
                        heading_text = f"Section {m.group(1)}: {m.group(2)}"
            if heading_text and heading_text[-1] not in ".!?:":
                heading_text = heading_text + "."
            chunks.append({
                "text": rewrite_for_tts(heading_text),
                "phrase_indices": list(indices),
            })
            continue

        # Audio-only summary blocks (for code blocks or tables): each summary
        # gets its OWN chunk with a spoken prefix so the listener knows what's
        # happening when the camera pans to the visual element. Don't pack
        # with surrounding content.
        if block.get("tts_summary_for_code") or block.get("tts_summary_for_table"):
            flush()
            prefix = (
                "Code Block Summary: "
                if block.get("tts_summary_for_code")
                else "Table Summary: "
            )
            body = " ".join(phrases[i] for i in indices).strip()
            spoken = prefix + body
            if spoken and spoken[-1] not in ".!?:":
                spoken = spoken + "."
            chunks.append({
                "text": rewrite_for_tts(spoken),
                "phrase_indices": list(indices),
            })
            continue

        # Non-heading: pack phrase-by-phrase up to max_chars. Char accounting
        # uses RAW phrase lengths (pre-rewrite) so chunk packing is stable;
        # the rewrite (which lengthens a name → IPA) only happens at flush.
        #
        # For list items, ensure the LAST phrase ends with terminal punctuation
        # — bullets in the source often omit periods, which causes Kokoro to
        # run consecutive list items together without a pause. Adding a period
        # gives the prosody a real sentence boundary at the end of each bullet.
        is_list_item = kind in ("li", "li_num")
        last_block_phrase_idx = indices[-1]
        for i in indices:
            phrase_text = phrases[i]
            if is_list_item and i == last_block_phrase_idx:
                stripped = phrase_text.rstrip()
                if stripped and stripped[-1] not in ".!?:;":
                    phrase_text = stripped + "."
            add_len = len(phrase_text) + (1 if cur_text else 0)
            if cur_chars + add_len > max_chars and cur_text:
                flush()
                add_len = len(phrase_text)
            cur_text.append(phrase_text)
            cur_indices.append(i)
            cur_chars += add_len

    flush()
    return chunks


# ────────────────────────────────────────────────────────────────────────────────
# 2. RENDER-COPY HTML GENERATION
# ────────────────────────────────────────────────────────────────────────────────

def _inline_md_to_html(text: str, phrase_texts: list[str], phrase_start_idx: int) -> str:
    """
    Convert an inline-markdown string to HTML, while wrapping each *narrated
    phrase* in <span class="narr" id="narr-N">.

    The phrases are themselves narration-plain (markdown stripped). We match
    them against the rendered HTML text by re-stripping the HTML temporarily
    to find their offsets, then weave wrappers back in.

    This is the trickiest piece, because in a block like
        "We pin to **exact versions, always.** A pinned version can ..."
    the phrase split is on the narration-plain version:
        "We pin to exact versions, always."
        "A pinned version can ..."
    and the markdown-aware HTML interleaves <strong> tags through the first one.
    """
    if not phrase_texts:
        return _md_inline_to_html_only(text)

    # Build the HTML *with* inline markdown applied, but preserve a position
    # map back to the narration-plain string.
    html_with_map = _md_inline_with_position_map(text)
    html_str = html_with_map["html"]
    plain_str = html_with_map["plain"]
    plain_to_html = html_with_map["plain_to_html"]  # plain_idx -> html_idx

    # Walk the phrases in order, locate each in the plain string, then
    # insert <span> open/close at the corresponding html positions.
    cursor_plain = 0
    insertions: list[tuple[int, str]] = []  # (html_pos, snippet)

    for i, ph in enumerate(phrase_texts):
        # Normalise whitespace for matching (same as block_narration_text did
        # to the raw markdown — so both sides are collapsed)
        needle = re.sub(r"\s+", " ", ph).strip()
        if not needle:
            continue
        # Try exact match from cursor; fall back to a softer regex match
        idx = plain_str.find(needle, cursor_plain)
        if idx == -1:
            # Fall back: match the first 30 chars of the phrase, skipping ws
            short = needle[:30]
            idx = plain_str.find(short, cursor_plain)
            if idx == -1:
                # Last resort: just position at cursor — visual highlight will
                # still land on the right block, scroll position will be close
                idx = cursor_plain

        end = idx + len(needle)
        # Clamp end to remain within plain text
        end = min(end, len(plain_str))

        html_open_pos = plain_to_html[idx]
        html_close_pos = plain_to_html[min(end, len(plain_to_html) - 1)]

        insertions.append((html_open_pos, f'<span class="narr" id="narr-{phrase_start_idx + i}">'))
        insertions.append((html_close_pos, "</span>"))
        cursor_plain = end

    # Apply insertions from right to left so earlier offsets stay valid
    insertions.sort(key=lambda t: t[0], reverse=True)
    out = html_str
    for pos, snippet in insertions:
        out = out[:pos] + snippet + out[pos:]
    return out


def _md_inline_with_position_map(text: str) -> dict:
    """
    Convert inline markdown to HTML AND track a per-plain-character map back
    into the produced HTML string.

    Returns dict with:
      - html: the HTML string (with <strong>, <em>, <code>, <a>)
      - plain: the narration-plain string (no markdown markers)
      - plain_to_html: list where plain_to_html[i] gives the html offset of
        the i-th plain character. Length == len(plain) + 1 (sentinel at end).
    """
    html_chars: list[str] = []
    plain_chars: list[str] = []
    plain_to_html: list[int] = []

    i = 0
    n = len(text)

    def emit_html_only(s: str):
        # HTML-only emission (tags): don't advance plain
        html_chars.append(s)

    def emit_both(s: str):
        # Each char appears in both streams; record the mapping
        for ch in s:
            plain_to_html.append(sum(len(x) for x in html_chars))
            html_chars.append(html_lib.escape(ch))
            plain_chars.append(ch)

    while i < n:
        # ` ... ` (inline code)
        if text[i] == "`":
            j = text.find("`", i + 1)
            if j != -1:
                emit_html_only("<code>")
                emit_both(text[i + 1:j])
                emit_html_only("</code>")
                i = j + 1
                continue

        # [label](url) — link
        if text[i] == "[":
            m = re.match(r"\[([^\]]+)\]\(([^)]+)\)", text[i:])
            if m:
                label, url = m.group(1), m.group(2)
                emit_html_only(f'<a href="{html_lib.escape(url, quote=True)}">')
                emit_both(label)
                emit_html_only("</a>")
                i += m.end()
                continue

        # **bold**
        if text[i:i + 2] == "**":
            j = text.find("**", i + 2)
            if j != -1:
                emit_html_only("<strong>")
                # Recurse-lite: inner can contain code/italic
                inner = text[i + 2:j]
                inner_map = _md_inline_with_position_map(inner)
                # Re-emit inner content while updating maps
                # Adjust the inner plain_to_html by current html length
                html_offset = sum(len(x) for x in html_chars)
                html_chars.append(inner_map["html"])
                plain_chars.append(inner_map["plain"])
                plain_to_html.extend(p + html_offset for p in inner_map["plain_to_html"][:-1])
                emit_html_only("</strong>")
                i = j + 2
                continue

        # *italic*
        if text[i] == "*" and (i + 1 < n and text[i + 1] != "*"):
            j = text.find("*", i + 1)
            if j != -1:
                emit_html_only("<em>")
                inner = text[i + 1:j]
                inner_map = _md_inline_with_position_map(inner)
                html_offset = sum(len(x) for x in html_chars)
                html_chars.append(inner_map["html"])
                plain_chars.append(inner_map["plain"])
                plain_to_html.extend(p + html_offset for p in inner_map["plain_to_html"][:-1])
                emit_html_only("</em>")
                i = j + 1
                continue

        emit_both(text[i])
        i += 1

    # Sentinel
    plain_to_html.append(sum(len(x) for x in html_chars))

    return {
        "html": "".join(html_chars),
        "plain": "".join(plain_chars),
        "plain_to_html": plain_to_html,
    }


def _md_inline_to_html_only(text: str) -> str:
    """Simpler inline markdown → HTML (no position tracking)."""
    return _md_inline_with_position_map(text)["html"]


def render_video_html(
    annotated_blocks: list[dict],
    out_path: Path,
    title_override: str = "",
) -> None:
    """
    Generate the video-only HTML file: same visual language as the polished
    on-screen doc, but with every narrated phrase wrapped in a known span,
    no collapsibles, no chrome, no Quick Reference.
    """
    parts: list[str] = []
    # The <title> is a placeholder in the template so every render carries its
    # OWN document title. It was previously hardcoded, which stamped one
    # document's title into the metadata of every video ever produced.
    # str.replace rather than .format(): the CSS below is full of literal
    # braces that would need escaping.
    parts.append(_HTML_HEADER.replace(
        "__DOC_TITLE__",
        html_lib.escape(_doc_title(annotated_blocks, title_override)),
    ))
    parts.append('<main class="page">\n')

    # Track open list context so we can emit <ul>/<ol> correctly
    list_stack: list[tuple[str, int]] = []  # (tag, depth)

    def close_lists_to(target_depth: int):
        while list_stack and list_stack[-1][1] >= target_depth:
            tag, _ = list_stack.pop()
            parts.append(f"</{tag}>\n")

    for block in annotated_blocks:
        kind = block["kind"]
        depth = block.get("depth", 0)
        phrase_texts = block.get("phrase_texts", [])
        phrase_start = block["phrase_indices"][0] if block["phrase_indices"] else 0

        # List handling
        if kind in ("li", "li_num"):
            wanted_tag = "ul" if kind == "li" else "ol"
            # Close lists deeper than this one
            close_lists_to(depth + 1)
            # If top of stack is the same tag at same depth, continue;
            # otherwise open a new list
            if not list_stack or list_stack[-1] != (wanted_tag, depth):
                # If a different list type is open at this depth, close it
                if list_stack and list_stack[-1][1] == depth:
                    tag, _ = list_stack.pop()
                    parts.append(f"</{tag}>\n")
                parts.append(f"<{wanted_tag}>\n")
                list_stack.append((wanted_tag, depth))
            inner = _inline_md_to_html(block["text"], phrase_texts, phrase_start)
            parts.append(f"  <li>{inner}</li>\n")
            continue

        # Anything that isn't a list item: close all open lists
        close_lists_to(0)

        if kind == "h1":
            inner = _inline_md_to_html(block["text"], phrase_texts, phrase_start)
            parts.append(f'<h1 class="title">{inner}</h1>\n')
        elif kind == "h2":
            inner = _inline_md_to_html(block["text"], phrase_texts, phrase_start)
            parts.append(f'<h2 class="section">{inner}</h2>\n')
        elif kind == "h3":
            inner = _inline_md_to_html(block["text"], phrase_texts, phrase_start)
            parts.append(f'<h3>{inner}</h3>\n')
        elif kind == "h4":
            inner = _inline_md_to_html(block["text"], phrase_texts, phrase_start)
            parts.append(f'<h4>{inner}</h4>\n')
        elif kind == "p":
            # Summary paragraphs (for code blocks or tables) are audio-only:
            # don't render them to HTML. Their narration plays while the
            # camera holds on the next code/table block.
            if block.get("tts_summary_for_code") or block.get("tts_summary_for_table"):
                continue
            inner = _inline_md_to_html(block["text"], phrase_texts, phrase_start)
            parts.append(f"<p>{inner}</p>\n")
        elif kind == "blockquote_p":
            inner = _inline_md_to_html(block["text"], phrase_texts, phrase_start)
            parts.append(f'<blockquote><p>{inner}</p></blockquote>\n')
        elif kind == "code":
            # Visual-only: no narr spans, raw text is HTML-escaped and
            # wrapped in <pre><code>. The block carries an id so frame
            # composition can look up its on-page Y position for dwell.
            lang = block.get("lang", "")
            cls = f' class="lang-{html_lib.escape(lang)}"' if lang else ""
            # Use the block's position in the annotated list as the id.
            # phrase_indices is empty for code so we have to stamp something.
            code_id = f'code-{block.get("code_idx", 0)}'
            escaped = html_lib.escape(block["text"])
            parts.append(
                f'<pre class="code-block" id="{code_id}"><code{cls}>{escaped}</code></pre>\n'
            )
        elif kind == "table":
            # Visual-only (same pattern as code blocks). The preceding
            # tts_summary_for_table block carries the narration.
            table_id = f'table-{block.get("table_idx", 0)}'
            header = block.get("header", [])
            rows = block.get("rows", [])
            parts.append(f'<table class="md-table" id="{table_id}">\n')
            parts.append('<thead><tr>')
            for h in header:
                parts.append(f'<th>{_md_inline_to_html_only(h)}</th>')
            parts.append('</tr></thead>\n<tbody>\n')
            for row in rows:
                parts.append('<tr>')
                for cell in row:
                    parts.append(f'<td>{_md_inline_to_html_only(cell)}</td>')
                parts.append('</tr>\n')
            parts.append('</tbody></table>\n')
        elif kind == "spoiler_guard":
            # Blank vertical spacer that pushes the following content below
            # the natural scroll target of the preceding narrated phrases.
            # Height ≈ one viewport so the answer never appears while the
            # question is being read aloud.
            parts.append('<div class="spoiler-guard" aria-hidden="true"></div>\n')

    close_lists_to(0)
    parts.append("</main>\n")
    parts.append(_HTML_FOOTER)

    out_path.write_text("".join(parts), encoding="utf-8")


# ─── HTML template fragments ──────────────────────────────────────────────────

_HTML_HEADER = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__DOC_TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=Inter+Tight:wght@400;500;600;700&family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;0,6..72,600;1,6..72,400;1,6..72,500&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; }
  :root {
    --canvas: #0d1117;
    --subtle: #161b22;
    --border: #30363d;
    --fg: #c9d1d9;
    --fg-strong: #f0f6fc;
    --fg-muted: #8b949e;
    --accent: #58a6ff;
    --highlight: rgba(210, 153, 34, 0.30);
    --code-bg: rgba(110, 118, 129, 0.30);
  }
  body {
    margin: 0;
    background: var(--canvas);
    color: var(--fg);
    font-family: "Newsreader", Georgia, serif;
    font-size: 22px;
    line-height: 1.7;
    -webkit-font-smoothing: antialiased;
  }
  .page {
    max-width: 920px;
    margin: 0 auto;
    padding: 4rem 3rem 6rem;
  }
  h1.title {
    font-family: "Fraunces", Georgia, serif;
    font-weight: 500;
    font-size: 3.4rem;
    line-height: 1.05;
    letter-spacing: -0.02em;
    color: var(--fg-strong);
    margin: 0 0 2rem;
  }
  h2.section {
    font-family: "Inter Tight", system-ui, sans-serif;
    font-weight: 600;
    font-size: 2.1rem;
    line-height: 1.2;
    letter-spacing: -0.015em;
    color: var(--fg-strong);
    margin: 4rem 0 1.5rem;
    padding-top: 2rem;
    border-top: 1px solid var(--border);
  }
  h3 {
    font-family: "Inter Tight", system-ui, sans-serif;
    font-weight: 600;
    font-size: 1.5rem;
    color: var(--fg-strong);
    margin: 2.5rem 0 1rem;
  }
  h4 {
    font-family: "Inter Tight", system-ui, sans-serif;
    font-weight: 600;
    font-size: 1.2rem;
    color: var(--fg-strong);
    margin: 2rem 0 0.75rem;
  }
  p { margin: 0 0 1.2rem; }
  strong { color: var(--fg-strong); font-weight: 600; }
  em { font-style: italic; }
  code {
    font-family: "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
    font-size: 0.88em;
    background: var(--code-bg);
    padding: 0.15em 0.4em;
    border-radius: 6px;
    color: var(--fg-strong);
  }
  a { color: var(--accent); text-decoration: none; }
  ul, ol { padding-left: 1.6rem; margin: 0 0 1.2rem; }
  li { margin: 0.5rem 0; }
  blockquote {
    margin: 1.4rem 0;
    padding: 1rem 1.25rem;
    border-left: 4px solid var(--accent);
    background: var(--subtle);
    border-radius: 0 6px 6px 0;
  }
  blockquote p { margin: 0; }

  /* Markdown tables: visual-only, narrated by the preceding tts_summary_for_table block. */
  table.md-table {
    border-collapse: collapse;
    margin: 1.4rem 0;
    width: 100%;
    font-family: "Inter Tight", system-ui, sans-serif;
    font-size: 0.95em;
    background: var(--subtle);
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
  }
  table.md-table th,
  table.md-table td {
    padding: 0.6rem 0.9rem;
    border-bottom: 1px solid var(--border);
    text-align: left;
    vertical-align: top;
  }
  table.md-table th {
    background: rgba(110, 118, 129, 0.15);
    color: var(--fg-strong);
    font-weight: 600;
    border-bottom: 2px solid var(--border);
  }
  table.md-table tr:last-child td { border-bottom: 0; }

  /* Fenced code blocks: visual-only, never narrated. The pipeline pauses or
     slow-scrolls on these while a hand-written tts-summary plays. */
  /* Spoiler guard: vertical spacer that hides the answer block until the
     narration scrolls down past the question + options. Tall enough to push
     the answer below the viewport when the camera is centered on the
     question's last option. */
  .spoiler-guard {
    height: 760px;
    width: 100%;
    background: var(--canvas);
  }

  pre.code-block {
    background: var(--subtle);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 1rem 1.25rem;
    margin: 1.4rem 0;
    overflow-x: auto;
    line-height: 1.45;
  }
  pre.code-block code {
    font-family: "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace;
    font-size: 0.85em;
    background: transparent;
    padding: 0;
    color: var(--fg);
    white-space: pre;
    display: block;
  }

  /* Narration phrase span — invisible by default; highlight applied by the
     frame compositor as a colored overlay rectangle, not via CSS, so the
     highlight color/opacity is controllable at composite time. */
  .narr { /* no visual styling — used only for coordinate extraction */ }
  /* Coordinate-dump <pre>s: the footer script removes their [hidden] attr
     so --dump-dom can grep them; force them invisible so they never leak
     into the screenshot/video. dump-dom reads textContent regardless. */
  #narr-coords, #code-coords, #table-coords { display: none !important; }
</style>
</head><body>
"""

# Script appended at end of body: computes each .narr span's document-Y plus
# height (and the same for any code block) and embeds the data as JSON inside
# known <pre> elements so we can grep them out of `chromium --dump-dom`.
_HTML_FOOTER = """
<pre id="narr-coords" hidden></pre>
<pre id="code-coords" hidden></pre>
<pre id="table-coords" hidden></pre>
<script>
  (function () {
    function rectInfo(el) {
      const r = el.getBoundingClientRect();
      return {
        id: el.id,
        top: Math.round(r.top + window.scrollY),
        height: Math.round(r.height),
      };
    }
    function emit() {
      const spans = Array.from(document.querySelectorAll('.narr'));
      const out = spans.map(function (s) {
        const r = s.getBoundingClientRect();
        const top = r.top + window.scrollY;
        return {
          id: s.id,
          top: Math.round(top),
          height: Math.round(r.height),
          // For multi-line phrases, the bounding rect height is the full block
          // height. We want the *first line's* y-center; fall back to a
          // sensible default if line-height isn't available.
          line: Math.round(parseFloat(getComputedStyle(s).lineHeight) || r.height)
        };
      });
      const narrPre = document.getElementById('narr-coords');
      narrPre.textContent = JSON.stringify(out);
      narrPre.removeAttribute('hidden');

      // Code-block coords (id, top, height) for dwell timing.
      const codeBlocks = Array.from(document.querySelectorAll('.code-block'));
      const codeOut = codeBlocks.map(rectInfo);
      const codePre = document.getElementById('code-coords');
      codePre.textContent = JSON.stringify(codeOut);
      codePre.removeAttribute('hidden');

      // Table coords — same pattern, used to anchor the camera on a table
      // while its narration plays.
      const tables = Array.from(document.querySelectorAll('table.md-table'));
      const tableOut = tables.map(rectInfo);
      const tablePre = document.getElementById('table-coords');
      tablePre.textContent = JSON.stringify(tableOut);
      tablePre.removeAttribute('hidden');

      // Marker for the screenshot tool: once this element appears, the page
      // is ready (fonts loaded, layout settled).
      narrPre.setAttribute('data-ready', '1');
    }
    // Wait for fonts so positions are stable
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(emit);
    } else {
      window.addEventListener('load', emit);
    }
  })();
</script>
</body></html>
"""


# ────────────────────────────────────────────────────────────────────────────────
# 3. CHROMIUM CAPTURE
# ────────────────────────────────────────────────────────────────────────────────

def _chromium_bin() -> str:
    """Locate a chromium binary, honouring $NARRAOKE_CHROMIUM first.

    The env override exists because `chromium-browser` is often a snap shim
    on Ubuntu/WSL. Snap confinement blocks reads outside $HOME, so rendering a
    markdown file from an arbitrary path fails in ways that look like a
    Chromium bug rather than a sandboxing one. Point this at a real binary
    (e.g. /usr/bin/chromium) to sidestep it.
    """
    override = os.environ.get("NARRAOKE_CHROMIUM", "").strip()
    if override:
        path = shutil.which(override) or (override if Path(override).is_file() else None)
        if not path:
            raise RuntimeError(
                f"$NARRAOKE_CHROMIUM is set to {override!r} but no such "
                f"executable was found. Unset it or correct the path."
            )
        return path
    for name in ("chromium-browser", "chromium", "google-chrome", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError(
        "No chromium-compatible browser found on PATH. "
        "Install chromium, chromium-browser, or google-chrome, "
        "or set $NARRAOKE_CHROMIUM to a specific binary."
    )


# Chromium's --screenshot silently truncates very tall pages (empirically
# starts going blank around 32k px, hard ceiling around 16k on some builds).
# We slice the page into windows of at most CHROMIUM_SCREENSHOT_SLICE_H pixels
# and stitch them with PIL. 8000 is well under the safe limit. The slicer
# avoids cutting through any code/table block by adjusting boundaries up to
# CHROMIUM_SCREENSHOT_SLICE_FLEX pixels — so a block that would otherwise
# straddle slice N/N+1 is fully captured in one of them. Any block taller
# than (slice_h - flex) still gets split because there's no boundary that
# avoids it.
CHROMIUM_SCREENSHOT_SLICE_H = 8000
CHROMIUM_SCREENSHOT_SLICE_FLEX = 1500


def _check_phrase_coverage(
    phrases: list[str],
    annotated_blocks: list[dict],
    coords: list[dict],
) -> list[str]:
    """Warn only about phrases that will genuinely have nowhere to point.

    This replaces a comparison of `len(coords)` against `len(phrases)`, which
    warned on every render of any document containing a table or a code block.
    Those two numbers were never meant to be equal.

    Code-block and table summaries are narrated but **deliberately have no
    span of their own** — `render_video_html` skips them, and `build_keyframes`
    points the camera at the *following* code/table element instead, so the
    reader sees the thing being described rather than a highlight on prose
    that is not on screen. Every such phrase is accounted for; none is
    dropped.

    What actually matters is that each phrase resolves to *something*: its own
    span, or a visual element it is paired with. A phrase that resolves to
    neither is a real defect — `build_keyframes` skips it and the karaoke
    highlight stalls. That is what this checks.
    """
    have_span = {
        int(entry["id"].split("-")[1])
        for entry in coords
        if isinstance(entry.get("id"), str) and "-" in entry["id"]
    }

    # Mirror build_keyframes' pairing: a summary block's phrases anchor to the
    # next code/table block. The pairing resets on any other intervening
    # block, so a summary without its visual is exactly the orphan case.
    anchored: set[int] = set()
    pending: list[int] = []
    for block in annotated_blocks:
        if block.get("tts_summary_for_code") or block.get("tts_summary_for_table"):
            pending.extend(block.get("phrase_indices", []))
            continue
        if pending and block.get("kind") in ("code", "table"):
            anchored.update(pending)
        pending = []

    orphans = [
        i for i in range(len(phrases))
        if i not in have_span and i not in anchored
    ]
    if not orphans:
        return []

    shown = ", ".join(str(i) for i in orphans[:8])
    if len(orphans) > 8:
        shown += f", … ({len(orphans)} total)"
    return [
        f"{len(orphans)} phrase(s) have neither a span nor a visual anchor, so "
        f"the highlight will stall on them: {shown}"
    ]


def _check_code_summaries(annotated_blocks: list[dict]) -> list[str]:
    """Report code blocks narrated by the generic fallback line.

    A code block is never read out line by line. Something is narrated while
    the camera dwells on it, and that something is either a `tts-summary`
    comment the author wrote or `_default_code_summary`'s "A Python code block
    follows." The render succeeds either way, which is exactly why this is
    easy to miss: nothing fails, the video just spends the length of a code
    block saying nothing about it.

    Tables are deliberately excluded. `flush_table` builds their narration
    from the table's own cells, so a table always has real content to speak
    and there is no authored-versus-fallback distinction to draw.

    This is advisory, not a defect report — unlike `_check_phrase_coverage`,
    which flags phrases that genuinely break the highlight.
    """
    missing = [
        block for block in annotated_blocks
        if block.get("tts_summary_for_code") and not block.get("authored_summary")
    ]
    if not missing:
        return []

    langs = sorted({(b.get("code_lang") or "untagged") for b in missing})
    return [
        f"{len(missing)} code block(s) have no <!-- tts-summary: … --> and will "
        f"be narrated with a generic line (\"A code block follows.\"), so the "
        f"camera dwells on them while the audio says nothing about them. "
        f"Languages: {', '.join(langs)}."
    ]


def _diagnose_dom_failure(dom: str, html_path: Path, chromium: str) -> str:
    """Explain why a DOM dump has no coordinates, as specifically as possible.

    The generic message ("the coordinate-extraction script may have failed")
    is wrong in the most common case: Chromium never loaded the page at all,
    and what came back is its own error page. Blaming the script sends people
    looking in the wrong place.
    """
    # Chromium's error pages carry an ERR_* code. A page may mention several
    # — a failed font fetch reports ERR_INTERNET_DISCONNECTED even when the
    # page itself loaded fine — so look for the one describing the *document*
    # first, and only then fall back to whatever else is present.
    error_code = ""
    if "ERR_FILE_NOT_FOUND" in dom:
        error_code = "ERR_FILE_NOT_FOUND"
    elif "ERR_ACCESS_DENIED" in dom:
        error_code = "ERR_ACCESS_DENIED"
    else:
        match = re.search(r"\bERR_[A-Z_]+", dom)
        if match:
            error_code = match.group(0)

    if error_code == "ERR_FILE_NOT_FOUND":
        exists = html_path.is_file()
        lines = [
            f"Chromium reported {error_code} for the page narraoke just wrote:",
            f"  {html_path}",
        ]
        if exists:
            # The file is there, so this is an access problem, not a missing
            # file. Snap confinement is by far the most common cause.
            lines += [
                "",
                "That file exists, so Chromium could not *read* it rather than",
                "not find it. The usual cause is a sandboxed Chromium: snap",
                "builds are confined to $HOME and cannot read anywhere else.",
                "",
                f"Chromium in use: {chromium}",
                "",
                "Fixes, easiest first:",
                "  * render to a path under your home directory",
                "    (--output-dir ~/somewhere), or move the source document there",
                "  * install a non-snap chromium and point narraoke at it:",
                "    NARRAOKE_CHROMIUM=/path/to/chromium",
            ]
        else:
            lines += ["", "The file is missing, which is a narraoke bug — "
                          "please report it."]
        return "\n".join(lines)

    if error_code:
        return (
            f"Chromium reported {error_code} while loading:\n"
            f"  {html_path}\n\n"
            f"Chromium in use: {chromium}\n"
            f"No phrase coordinates could be extracted."
        )

    # No error page: the page really did load, so the script is the suspect.
    return (
        "Could not find narr-coords element in DOM dump — the page loaded but "
        "its coordinate-extraction script did not produce output.\n"
        f"  page: {html_path}\n"
        f"  dump: {html_path.parent / 'page.html'}"
    )


def capture_page(html_path: Path, output_dir: Path) -> tuple[Path, list[dict], list[dict], list[dict]]:
    """
    Render the video HTML at fixed width × tall natural height and capture
    four artefacts:

      1. A full-page PNG screenshot at VIDEO_WIDTH wide (slice-and-stitched
         if the page exceeds CHROMIUM_SCREENSHOT_SLICE_H pixels).
      2. The phrase Y-coordinates JSON that the page's own script computed.
      3. The code-block Y-coordinates JSON (empty if no fenced code blocks).
      4. The table Y-coordinates JSON (empty if no tables).

    Returns (screenshot_path, narr_coords, code_coords, table_coords).
    """
    chromium = _chromium_bin()
    screenshot = output_dir / "page.png"
    dom_dump = output_dir / "page.html"

    # Pass A: --dump-dom to get the narr-coords JSON and the doc height.
    # Pass A's coordinates are authoritative, so we don't have to guess from
    # screenshot dimensions.
    common = [
        chromium,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--virtual-time-budget=15000",  # let fonts + script settle
        f"--window-size={VIDEO_WIDTH},2000",
        f"file://{html_path.resolve()}",
    ]

    step("Capturing DOM coordinates …")
    proc = subprocess.run(
        common + ["--dump-dom"],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"chromium --dump-dom failed: {proc.stderr[:500]}")

    dom = proc.stdout
    dom_dump.write_text(dom, encoding="utf-8")

    # Pull the JSON out of the <pre id="narr-coords"> element
    m = re.search(
        r'<pre[^>]*id="narr-coords"[^>]*data-ready="1"[^>]*>([\s\S]*?)</pre>',
        dom,
    )
    if not m:
        # Try without data-ready (in case the attribute order changed)
        m = re.search(
            r'<pre[^>]*id="narr-coords"[^>]*>([\s\S]*?)</pre>',
            dom,
        )
    if not m:
        raise RuntimeError(_diagnose_dom_failure(dom, html_path, chromium))
    coords_raw = html_lib.unescape(m.group(1).strip())
    if not coords_raw:
        raise RuntimeError("narr-coords element was empty.")
    coords = json.loads(coords_raw)
    info(f"  Extracted {len(coords)} phrase coordinates")

    # Optional: code-block coordinates (for dwelling on code-block summaries).
    # Older HTML output didn't include this element; missing is not an error.
    code_coords: list[dict] = []
    cm = re.search(r'<pre[^>]*id="code-coords"[^>]*>([\s\S]*?)</pre>', dom)
    if cm:
        cc_raw = html_lib.unescape(cm.group(1).strip())
        if cc_raw:
            try:
                code_coords = json.loads(cc_raw)
                info(f"  Extracted {len(code_coords)} code-block coordinates")
            except Exception as e:
                warn(f"  Could not parse code-coords JSON: {e}")

    # Optional: table coordinates (for dwelling on table summaries).
    table_coords: list[dict] = []
    tm = re.search(r'<pre[^>]*id="table-coords"[^>]*>([\s\S]*?)</pre>', dom)
    if tm:
        tc_raw = html_lib.unescape(tm.group(1).strip())
        if tc_raw:
            try:
                table_coords = json.loads(tc_raw)
                info(f"  Extracted {len(table_coords)} table coordinates")
            except Exception as e:
                warn(f"  Could not parse table-coords JSON: {e}")

    # Total document height: max of top + height across all phrases + footer
    # padding. Add buffer for safety.
    doc_height = max(c["top"] + c["height"] for c in coords) + 200

    # Pass B: capture the page. If it fits in a single screenshot, do it the
    # simple way. Otherwise, slice into multiple chromium passes and stitch.
    if doc_height <= CHROMIUM_SCREENSHOT_SLICE_H:
        step(f"Capturing tall screenshot ({VIDEO_WIDTH}×{doc_height}) …")
        screenshot_cmd = [
            chromium,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--virtual-time-budget=15000",
            f"--window-size={VIDEO_WIDTH},{doc_height}",
            f"--screenshot={screenshot.resolve()}",
            f"file://{html_path.resolve()}",
        ]
        proc = subprocess.run(
            screenshot_cmd,
            capture_output=True, text=True, timeout=180,
        )
        if not screenshot.exists():
            raise RuntimeError(
                f"chromium failed to write screenshot: {proc.stderr[:500]}"
            )
    else:
        # Build the "do not split through these" list from code + table extents
        # so slice boundaries shift to avoid cutting through any of them.
        avoid = [(c["top"], c["height"]) for c in code_coords] + \
                [(c["top"], c["height"]) for c in table_coords]
        _capture_sliced(
            chromium=chromium,
            html_path=html_path,
            output_dir=output_dir,
            doc_height=doc_height,
            out_path=screenshot,
            avoid_split_extents=avoid,
        )

    info(f"  Screenshot: {screenshot} ({screenshot.stat().st_size // 1024} KB)")

    return screenshot, coords, code_coords, table_coords


def _plan_slice_boundaries(
    doc_height: int,
    block_extents: list[tuple[int, int]],
    slice_h: int = CHROMIUM_SCREENSHOT_SLICE_H,
    flex: int = CHROMIUM_SCREENSHOT_SLICE_FLEX,
) -> list[tuple[int, int]]:
    """Return a list of (offset, height) pairs covering [0, doc_height).

    Each slice spans up to `slice_h` pixels. Boundaries are shifted backward
    by up to `flex` pixels to avoid cutting through any (top, height) in
    `block_extents`. Adjacent slices are perfectly contiguous (no overlap,
    no gap) so PIL stitch lines up exactly.

    If a block is taller than `slice_h - flex` it's impossible to avoid
    cutting it; in that case we accept the cut at the natural boundary.
    """
    # Sort + dedupe block extents for fast lookup.
    blocks = sorted({(int(t), int(h)) for t, h in block_extents if h > 0})

    def cut_would_split(boundary: int) -> tuple[int, int] | None:
        """Return the offending block if boundary cuts through one, else None."""
        for t, h in blocks:
            if t < boundary < t + h:
                return (t, h)
            if t >= boundary:
                break  # blocks sorted; nothing further can match
        return None

    plans: list[tuple[int, int]] = []
    offset = 0
    while offset < doc_height:
        # Natural max-boundary for this slice.
        nominal_end = min(offset + slice_h, doc_height)
        chosen_end = nominal_end
        # Walk backward up to `flex` pixels looking for a clean boundary.
        if chosen_end < doc_height:
            best = None
            for trial in range(nominal_end, max(offset + 1, nominal_end - flex) - 1, -1):
                if cut_would_split(trial) is None:
                    best = trial
                    break
            if best is not None:
                chosen_end = best
            # else: keep nominal_end and accept the cut
        plans.append((offset, chosen_end - offset))
        offset = chosen_end
    return plans


def _capture_sliced(
    chromium: str,
    html_path: Path,
    output_dir: Path,
    doc_height: int,
    out_path: Path,
    avoid_split_extents: list[tuple[int, int]] | None = None,
) -> None:
    """Capture a tall page in N slices and stitch them.

    Chromium's --screenshot silently truncates around 16k-32k px. We render
    the original HTML inside a wrapper that translates the body up by the
    slice offset, then screenshot only the top CHROMIUM_SCREENSHOT_SLICE_H
    pixels. Repeat for each slice, then stitch via PIL.

    If *avoid_split_extents* is provided, slice boundaries are nudged
    backward (up to CHROMIUM_SCREENSHOT_SLICE_FLEX pixels) to avoid cutting
    through any of the listed (top, height) regions. Used to keep code blocks
    and tables intact across slice boundaries.

    The wrapper HTML lives in a tmp file next to the source so relative
    asset paths (none in our case, but future-proof) still resolve.
    """
    from PIL import Image

    # Disable PIL's decompression-bomb guard; we trust our own screenshots.
    Image.MAX_IMAGE_PIXELS = None

    plans = _plan_slice_boundaries(
        doc_height=doc_height,
        block_extents=avoid_split_extents or [],
    )
    n_slices = len(plans)
    step(
        f"Capturing tall screenshot ({VIDEO_WIDTH}×{doc_height}) "
        f"in {n_slices} slice(s) …"
    )

    tmp_dir = output_dir / "_slice_tmp"
    tmp_dir.mkdir(exist_ok=True)
    source_uri = f"file://{html_path.resolve()}"

    # Read the source HTML once; we'll inject it into each wrapper rather than
    # iframe it. Avoids cross-document font/layout races that were leaving
    # paragraphs blank near slice boundaries.
    source_html = html_path.read_text(encoding="utf-8")
    # Strip the <!DOCTYPE> + outer <html>/<body> wrappers — we keep just the
    # <head> contents (fonts, styles) and <body> contents to merge.
    head_m = re.search(r"<head[^>]*>([\s\S]*?)</head>", source_html, re.IGNORECASE)
    body_m = re.search(r"<body[^>]*>([\s\S]*?)</body>", source_html, re.IGNORECASE)
    source_head = head_m.group(1) if head_m else ""
    source_body = body_m.group(1) if body_m else source_html

    slice_paths: list[tuple[Path, int, int]] = []  # (path, offset, height)
    for i, (offset, this_slice_h) in enumerate(plans):
        # Wrapper inlines the source page directly, then shifts the .page
        # container up by `offset` pixels and clips to `this_slice_h`. This
        # gives us identical layout to the source page (no iframe-context
        # font/layout races) while still capturing only the slice we want.
        wrapper_html = f"""<!DOCTYPE html><html><head>
{source_head}
<style>
  /* Slice-specific overrides on top of the source page's styles */
  html, body {{ margin: 0; padding: 0; background: #0d1117; overflow: hidden; }}
  body {{ width: {VIDEO_WIDTH}px; height: {this_slice_h}px; }}
  .page {{ margin-top: -{offset}px !important; }}
</style>
</head><body>
{source_body}
</body></html>"""
        wrapper_path = tmp_dir / f"wrapper_{i:03d}.html"
        wrapper_path.write_text(wrapper_html, encoding="utf-8")
        slice_path = tmp_dir / f"slice_{i:03d}.png"
        cmd = [
            chromium,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--virtual-time-budget=15000",
            f"--window-size={VIDEO_WIDTH},{this_slice_h}",
            f"--screenshot={slice_path.resolve()}",
            f"file://{wrapper_path.resolve()}",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if not slice_path.exists():
            raise RuntimeError(
                f"chromium failed to write slice {i}: {proc.stderr[:500]}"
            )
        info(f"  slice {i + 1}/{n_slices} captured (offset={offset}, h={this_slice_h})")
        slice_paths.append((slice_path, offset, this_slice_h))

    # Stitch into one tall PNG of exactly doc_height pixels. Each slice's
    # captured PNG is `this_slice_h` tall and pastes at its `offset`.
    step("Stitching slices …")
    stitched = Image.new("RGB", (VIDEO_WIDTH, doc_height), (13, 17, 23))
    for sp, offset, this_slice_h in slice_paths:
        s = Image.open(sp).convert("RGB")
        sw, sh = s.size
        usable_h = min(sh, doc_height - offset)
        if usable_h <= 0:
            continue
        stitched.paste(s.crop((0, 0, sw, usable_h)), (0, offset))

    stitched.save(out_path, "PNG")

    # Clean up intermediates
    for sp, _o, _h in slice_paths:
        sp.unlink(missing_ok=True)
    for p in tmp_dir.glob("wrapper_*.html"):
        p.unlink(missing_ok=True)
    try:
        tmp_dir.rmdir()
    except OSError:
        pass


# ────────────────────────────────────────────────────────────────────────────────
# 4. FRAME COMPOSITION
# ────────────────────────────────────────────────────────────────────────────────

def build_keyframes(
    screenshot_path: Path,
    coords: list[dict],
    segments: list,
    output_dir: Path,
    annotated_blocks: list[dict] | None = None,
    code_coords: list[dict] | None = None,
    table_coords: list[dict] | None = None,
    dwell_by_phrase: dict[int, float] | None = None,
) -> list[tuple[Path, float]]:
    """
    For each timed phrase, build a single keyframe PNG: a VIDEO_WIDTH ×
    VIDEO_HEIGHT slice of the tall screenshot, scrolled so the active phrase
    sits at READ_ZONE of the viewport, with a translucent highlight rect
    overlaid on the phrase's vertical extent.

    For phrases that are TTS summaries of an upcoming code block or table,
    the camera is panned to that block's top instead of the summary's own
    location — so the listener sees the visual element while hearing it.

    Returns a list of (frame_path, duration_seconds) tuples in order.
    """
    from PIL import Image

    # PIL refuses to load images above ~179M pixels by default (decompression
    # bomb protection). Our tall-page screenshots can be 1280 × 200,000+ on
    # code-dense docs. Disable the limit before opening; we trust our own
    # screenshots.
    Image.MAX_IMAGE_PIXELS = None

    step("Composing keyframes …")
    page = Image.open(screenshot_path).convert("RGBA")
    page_w, page_h = page.size
    info(f"  Page image: {page_w}×{page_h}")

    # Build a lookup id -> coord
    by_id = {c["id"]: c for c in coords}
    code_by_id = {c["id"]: c for c in (code_coords or [])}
    table_by_id = {c["id"]: c for c in (table_coords or [])}

    # Map phrase index -> id of the code block or table this phrase's
    # summary belongs to (only for phrases inside a tts_summary_for_code /
    # tts_summary_for_table paragraph). All such phrases share the same
    # visual-anchor lookup.
    summary_to_visual: dict[int, dict] = {}
    if annotated_blocks:
        prev_was_summary: dict | None = None
        for block in annotated_blocks:
            if block.get("tts_summary_for_code") or block.get("tts_summary_for_table"):
                prev_was_summary = block
                continue
            if prev_was_summary is None:
                continue
            if block.get("kind") == "code":
                visual_id = f'code-{block.get("code_idx", 0)}'
                visual = code_by_id.get(visual_id)
            elif block.get("kind") == "table":
                visual_id = f'table-{block.get("table_idx", 0)}'
                visual = table_by_id.get(visual_id)
            else:
                visual = None
            if visual is not None:
                for phrase_idx in prev_was_summary.get("phrase_indices", []):
                    summary_to_visual[phrase_idx] = visual
            prev_was_summary = None

    frames_dir = output_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    # Clear stale frames (a stale frame here would silently propagate to MP4)
    for old in frames_dir.glob("frame_*.png"):
        old.unlink()

    target_y = int(VIDEO_HEIGHT * READ_ZONE)
    max_scroll = max(0, page_h - VIDEO_HEIGHT)

    out: list[tuple[Path, float]] = []
    for seg in segments:
        # If this phrase narrates a code-block or table summary, point camera
        # at the top of that visual element. The summary's own `narr` spans
        # are NOT rendered to the page (summaries are audio-only), so we
        # skip the by_id lookup entirely for these segments.
        visual_c = summary_to_visual.get(seg.index)
        if visual_c is not None:
            seg_total_dur = max(0.05, seg.end - seg.start)
            dwell = float((dwell_by_phrase or {}).get(seg.index, 0.0))
            # Summary-audio portion = total - dwell. While audio plays, hold
            # at the top of the visual element so the reader can start reading.
            summary_dur = max(0.05, seg_total_dur - dwell)

            block_top = visual_c["top"]
            block_h = visual_c.get("height", 0)
            top_scroll_y = max(0, min(block_top - 80, max_scroll))
            # Final scroll position: bottom of block visible at bottom of
            # viewport. If the block fits in the viewport, no scroll needed.
            if block_h + 80 <= VIDEO_HEIGHT:
                bottom_scroll_y = top_scroll_y
            else:
                bottom_scroll_y = max(
                    0,
                    min(block_top + block_h + 40 - VIDEO_HEIGHT, max_scroll),
                )

            def _emit(scroll_y: int, dur: float, sub_idx: int = 0):
                crop_box = (0, scroll_y, VIDEO_WIDTH, scroll_y + VIDEO_HEIGHT)
                fr = page.crop(crop_box).copy()
                stem = f"frame_{seg.index:05d}" if sub_idx == 0 else f"frame_{seg.index:05d}_{sub_idx:03d}"
                out_path = frames_dir / f"{stem}.png"
                fr.convert("RGB").save(out_path, "PNG", optimize=False)
                out.append((out_path, max(0.05, dur)))

            # Frame 1: hold at top of block for the duration of the summary
            # narration audio.
            _emit(top_scroll_y, summary_dur, sub_idx=0)

            # Scroll + bottom-pause portion happens during the dwell.
            if dwell > 0.05:
                if bottom_scroll_y == top_scroll_y:
                    # Block fits — just hold the same frame for the dwell.
                    _emit(top_scroll_y, dwell, sub_idx=1)
                else:
                    # Slow-scroll then hold at bottom for DWELL_BOTTOM_PAUSE_S.
                    scroll_time = max(0.1, dwell - DWELL_BOTTOM_PAUSE_S)
                    scroll_distance = bottom_scroll_y - top_scroll_y
                    # Time-based step rule: ~6 fps during scroll, capped to a
                    # reasonable max so a very long scroll doesn't generate
                    # thousands of frames. At 75 px/s and 6 fps that's about
                    # 12.5 px per frame — visually smooth.
                    n_scroll_steps = max(
                        2,
                        min(int(scroll_time * 6), 300),
                    )
                    per_step_dur = scroll_time / n_scroll_steps
                    for i in range(1, n_scroll_steps + 1):
                        # Linear interpolation. i=n_scroll_steps lands on bottom.
                        t = i / n_scroll_steps
                        y = int(round(top_scroll_y + t * scroll_distance))
                        _emit(y, per_step_dur, sub_idx=i)
                    # Hold at bottom for the bottom-pause portion.
                    _emit(bottom_scroll_y, DWELL_BOTTOM_PAUSE_S, sub_idx=n_scroll_steps + 1)
            continue

        narr_id = f"narr-{seg.index}"
        c = by_id.get(narr_id)
        if c is None:
            warn(f"  Segment {seg.index} ({seg.text[:40]!r}) has no coords — skipping")
            continue

        draw_highlight = True

        phrase_top = c["top"]
        # We want the *middle* of the first line at target_y. For multi-line
        # phrases, the bounding rect height covers all lines; we approximate
        # the first line center as phrase_top + line/2.
        first_line_center = phrase_top + int(c["line"] * 0.5)
        scroll_y = first_line_center - target_y
        scroll_y = max(0, min(scroll_y, max_scroll))

        # Crop a VIDEO_WIDTH × VIDEO_HEIGHT window
        crop_box = (0, scroll_y, VIDEO_WIDTH, scroll_y + VIDEO_HEIGHT)
        frame = page.crop(crop_box).copy()

        if draw_highlight:
            phrase_top = c["top"]
            phrase_h = max(c["height"], int(c["line"]))
            highlight_top = phrase_top - scroll_y
            highlight_h = max(phrase_h, int(c["line"]))
            if 0 <= highlight_top + highlight_h and highlight_top < VIDEO_HEIGHT:
                overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
                from PIL import ImageDraw
                draw = ImageDraw.Draw(overlay)
                # Pad highlight horizontally to match the page's content column
                # (the .page is centered, max-width 920px, padding 3rem ≈ 48px).
                left = (VIDEO_WIDTH - 920) // 2 + 32
                right = VIDEO_WIDTH - left
                top = max(0, highlight_top - 4)
                bot = min(VIDEO_HEIGHT, highlight_top + highlight_h + 4)
                draw.rectangle(
                    [(left, top), (right, bot)],
                    fill=(210, 153, 34, 70),  # subtle warm yellow, low alpha
                )
                frame = Image.alpha_composite(frame, overlay)

        out_path = frames_dir / f"frame_{seg.index:05d}.png"
        frame.convert("RGB").save(out_path, "PNG", optimize=False)
        duration = max(0.05, seg.end - seg.start)
        out.append((out_path, duration))

    info(f"  Wrote {len(out)} keyframes")
    return out


# ────────────────────────────────────────────────────────────────────────────────
# 5. FFMPEG COMPOSITION
# ────────────────────────────────────────────────────────────────────────────────

def encode_video(
    keyframes: list[tuple[Path, float]],
    audio_path: Path,
    output_dir: Path,
    slug: str,
    smooth: bool = True,
) -> Path:
    """
    Stitch keyframes into an MP4 with the MP3 muxed in.

    If `smooth` is True, apply ffmpeg's minterpolate to ease scroll motion
    between keyframes; otherwise each keyframe is held for its full duration.
    """
    step("Encoding video …")
    if not keyframes:
        raise RuntimeError("No keyframes to encode.")

    # Use ffmpeg's concat demuxer: one entry per keyframe with its duration.
    # The last entry needs to be listed twice (concat demuxer quirk).
    # We also pre-pend the first frame held for LEAD_IN_SECONDS so the video
    # opens with a brief silent beat — many players take a fraction of a
    # second to begin the audio stream, which would otherwise clip the first
    # spoken word — and append an extra hold of the last frame for
    # TAIL_OUT_SECONDS so the video doesn't end on the final consonant of the
    # last spoken word. Audio is delayed (lead-in) and padded (tail-out) to
    # match.
    concat_lines: list[str] = []
    concat_lines.append(f"file '{keyframes[0][0].resolve()}'")
    concat_lines.append(f"duration {LEAD_IN_SECONDS:.4f}")
    for path, dur in keyframes:
        concat_lines.append(f"file '{path.resolve()}'")
        concat_lines.append(f"duration {dur:.4f}")
    # Tail-out hold (extra time on the last frame before the closing repeat)
    concat_lines.append(f"file '{keyframes[-1][0].resolve()}'")
    concat_lines.append(f"duration {TAIL_OUT_SECONDS:.4f}")
    # Repeat last file (concat demuxer requirement — last entry has no duration)
    concat_lines.append(f"file '{keyframes[-1][0].resolve()}'")

    concat_path = output_dir / "concat.txt"
    concat_path.write_text("\n".join(concat_lines), encoding="utf-8")

    mp4_path = output_dir / f"{slug}.mp4"
    if mp4_path.exists():
        mp4_path.unlink()

    use_nvenc = has_nvenc()
    vcodec = "h264_nvenc" if use_nvenc else "libx264"
    info(f"  Video codec: {vcodec}")

    # Build filter chain. With smooth=True, scale the framerate up and use
    # minterpolate (motion-compensated interpolation) to create the illusion
    # of smooth scroll between keyframes.
    if smooth:
        vf = (
            f"fps={FPS},minterpolate="
            "fps=30:mi_mode=blend:mc_mode=aobmc:vsbmf_mode=bs:scd=none"
        )
    else:
        vf = f"fps={FPS}"

    lead_in_ms = int(round(LEAD_IN_SECONDS * 1000))
    tail_pad_s = TAIL_OUT_SECONDS
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_path),
        "-i", str(audio_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-vf", vf,
        # adelay = lead-in silence; apad = tail-out silence (whole_dur in seconds)
        "-af", f"adelay=delays={lead_in_ms}:all=1,apad=pad_dur={tail_pad_s:.3f}",
        "-c:v", vcodec,
        "-preset", "p4" if use_nvenc else "medium",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(mp4_path),
    ]

    info(f"  Encoding {len(keyframes)} keyframes → {mp4_path.name}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # Surface a useful tail of ffmpeg's stderr
        raise RuntimeError(
            f"ffmpeg failed (exit {proc.returncode}):\n{proc.stderr[-1500:]}"
        )

    info(f"  Video: {mp4_path}  ({mp4_path.stat().st_size // (1024 * 1024)} MB)")
    return mp4_path


# ────────────────────────────────────────────────────────────────────────────────
# 6. PER-SECTION MP4 EXPORT
# ────────────────────────────────────────────────────────────────────────────────

def _section_slug(heading_text: str) -> str:
    """Slug for a section heading, preserving the leading numeric prefix.

    'Why this training exists' -> 'why-this-training-exists'
    '1. A short primer on git'  -> '01-a-short-primer-on-git'
    """
    text = heading_text.strip()
    m = re.match(r"^(\d+)\.\s*(.*)$", text)
    if m:
        num = int(m.group(1))
        rest = slugify(m.group(2))
        return f"{num:02d}-{rest}" if rest else f"{num:02d}"
    return slugify(text)


def _doc_title(annotated_blocks: list[dict], override: str = "") -> str:
    """The document title, used in the <title>, spoken intros, and title cards.

    Defaults to the document's h1, which is almost always right. An explicit
    `title` in the document's `.video.json` wins — useful when the on-screen
    heading is not what you want spoken or shown on a card.
    """
    if override:
        return override
    for b in annotated_blocks:
        if b["kind"] == "h1":
            return block_narration_text(b)
    return ""


def _section_number(heading_text: str) -> int | None:
    """Extract a leading integer from '0. Why this training exists' -> 0."""
    m = re.match(r"^\s*(\d+)\.\s*", heading_text)
    return int(m.group(1)) if m else None


def _section_title_only(heading_text: str) -> str:
    """Strip a leading 'N.' from a heading: '3. Client data and NDAs' -> 'Client data and NDAs'."""
    return re.sub(r"^\s*\d+\.\s*", "", heading_text).strip()


# ─── Title card image ─────────────────────────────────────────────────────────
# Same colour palette as the page template. Held on screen for the spoken
# intro's duration (or `TITLE_CARD_SILENT_SECONDS` for section 0, which has
# no spoken intro).
TITLE_CARD_SILENT_SECONDS = 2.5
# Lead-in silence prepended to the start of every output video. Many players
# take a fraction of a second to start the audio stream, which would otherwise
# clip the first spoken word.
LEAD_IN_SECONDS = 1.0
# Tail-out silence appended to the end of every output video so videos don't
# end on the final consonant of the last spoken word.
TAIL_OUT_SECONDS = 1.0

# Code-block / table dwell parameters. After each summary's narration ends,
# the camera scrolls through the visual element (or holds it if it fits in
# the viewport). Tunable via the constants below.
#
# Scroll speed is calibrated so a viewport-height (720px) of new content
# crosses the screen in ~10s — comfortable reading pace for code/tables.
SCROLL_PX_PER_SECOND = 75      # speed when slow-scrolling tall code blocks
DWELL_BOTTOM_PAUSE_S = 1.5     # hold-at-bottom time after scroll finishes
DWELL_FITS_PAUSE_S = 3.0       # hold time when the block already fits viewport
DWELL_MIN_S = 1.5              # minimum dwell even for tiny blocks
DWELL_MAX_S = 60.0             # cap so a giant block doesn't stall the video
_CARD_BG = (13, 17, 23)         # var(--canvas)
_CARD_FG_STRONG = (240, 246, 252)  # var(--fg-strong)
_CARD_FG_MUTED = (139, 148, 158)   # var(--fg-muted)
_CARD_ACCENT = (88, 166, 255)      # var(--accent)


def _find_font(family_keywords: tuple[str, ...], size: int):
    """Best-effort font lookup. Falls back to a known-good system font.

    We don't bundle font files — Pillow walks the system font dirs. On WSL
    this finds DejaVu/Liberation; on a Mac it picks up the system family.

    Filename matching is exact-stem (case-insensitive). A previous version
    used naive substring matching and accidentally picked up
    NotoSansGeorgian.ttf for "Georgia" — that font has no Latin glyphs and
    rendered everything as .notdef tofu boxes.
    """
    from PIL import ImageFont
    # Try font names directly first (works when fontconfig knows them)
    for kw in family_keywords:
        try:
            return ImageFont.truetype(kw, size)
        except OSError:
            pass

    # Build a stem -> path index from common system font dirs
    candidates: list[Path] = []
    for root in (
        "/usr/share/fonts", "/usr/local/share/fonts",
        str(Path.home() / ".local/share/fonts"),
        str(Path.home() / ".fonts"),
    ):
        rp = Path(root)
        if rp.is_dir():
            for ext in ("*.ttf", "*.otf"):
                candidates.extend(rp.rglob(ext))

    def stem_norm(p: Path) -> str:
        return p.stem.lower().replace(" ", "").replace("-", "").replace("_", "")

    by_stem = {stem_norm(p): p for p in candidates}

    # Phase 1: exact normalised-stem matches against the keywords.
    for kw in family_keywords:
        key = kw.lower().replace(" ", "").replace("-", "").replace("_", "")
        if key in by_stem:
            try:
                return ImageFont.truetype(str(by_stem[key]), size)
            except OSError:
                continue

    # Phase 2: pick a Latin-script font from common families. We pin to a
    # closed list rather than substring-matching, so we don't accidentally
    # grab a non-Latin font like NotoSansGeorgian for "Georgia".
    fallbacks_serif = [
        "dejavuserif", "liberationserif", "freeserif",
        "dejavuserifcondensed", "lmromancaps10regular",
    ]
    fallbacks_sans = [
        "dejavusans", "liberationsans", "freesans", "ubuntu",
        "notosans", "dejavusanscondensed",
    ]
    # If any of the requested keywords are serif-leaning, prefer serif fallbacks.
    serif_keywords = {"fraunces", "georgia", "times", "serif", "newsreader"}
    requested = {k.lower() for k in family_keywords}
    prefer = fallbacks_serif if requested & serif_keywords else fallbacks_sans
    for fb in prefer + (fallbacks_sans if prefer is fallbacks_serif else fallbacks_serif):
        if fb in by_stem:
            try:
                return ImageFont.truetype(str(by_stem[fb]), size)
            except OSError:
                continue

    warn(
        f"_find_font: no usable font found for {family_keywords!r}; "
        "falling back to PIL default (will look small)."
    )
    return ImageFont.load_default()


def _render_title_card(
    doc_title: str,
    section_label: str | None,
    section_heading: str | None,
    out_path: Path,
) -> None:
    """Render a 1280x720 title card PNG.

    Layout:
      [doc title, smaller, muted]                  <- top third
      [Section N: Heading, large, strong colour]   <- middle, only if labeled
    For section 0 we pass section_label=None so only the doc title shows.
    """
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), _CARD_BG)
    draw = ImageDraw.Draw(img)

    title_font = _find_font(("Fraunces", "Georgia", "DejaVu Serif", "Liberation Serif"), 44)
    section_font = _find_font(("Inter Tight", "Inter", "DejaVu Sans", "Liberation Sans"), 72)
    label_font = _find_font(("Inter Tight", "Inter", "DejaVu Sans"), 32)

    # Wrap helper using textbbox to keep lines under ~80% of width
    def wrap(text: str, font, max_width: int) -> list[str]:
        words = text.split()
        lines: list[str] = []
        line = ""
        for w in words:
            cand = (line + " " + w).strip()
            bbox = draw.textbbox((0, 0), cand, font=font)
            if bbox[2] - bbox[0] <= max_width:
                line = cand
            else:
                if line:
                    lines.append(line)
                line = w
        if line:
            lines.append(line)
        return lines

    max_w = int(VIDEO_WIDTH * 0.85)

    if section_label:
        title_lines = wrap(doc_title, title_font, max_w)
        section_lines = wrap(section_heading or "", section_font, max_w)
        # Vertical layout: doc title near top-third, label below, heading centred
        y = int(VIDEO_HEIGHT * 0.22)
        for line in title_lines:
            bbox = draw.textbbox((0, 0), line, font=title_font)
            w = bbox[2] - bbox[0]
            draw.text(((VIDEO_WIDTH - w) // 2, y), line, font=title_font, fill=_CARD_FG_MUTED)
            y += (bbox[3] - bbox[1]) + 8
        y += 40
        bbox = draw.textbbox((0, 0), section_label, font=label_font)
        w = bbox[2] - bbox[0]
        draw.text(((VIDEO_WIDTH - w) // 2, y), section_label, font=label_font, fill=_CARD_ACCENT)
        y += (bbox[3] - bbox[1]) + 24
        for line in section_lines:
            bbox = draw.textbbox((0, 0), line, font=section_font)
            w = bbox[2] - bbox[0]
            draw.text(((VIDEO_WIDTH - w) // 2, y), line, font=section_font, fill=_CARD_FG_STRONG)
            y += (bbox[3] - bbox[1]) + 12
    else:
        # Section 0: only the doc title, large and centered.
        # Auto-fit: pick the largest font size (up to 140px) that keeps the
        # title within max_w on at most 3 lines. The on-page h1 uses Fraunces
        # at 3.4rem (~74px in this 22px-base layout); we go larger here since
        # the card has no other content competing for space.
        size = 140
        while size > 60:
            title_only_font = _find_font(
                ("Fraunces", "Georgia", "DejaVu Serif", "Liberation Serif"), size
            )
            lines = wrap(doc_title, title_only_font, max_w)
            if len(lines) <= 3:
                break
            size -= 8
        else:
            title_only_font = _find_font(
                ("Fraunces", "Georgia", "DejaVu Serif", "Liberation Serif"), size
            )
            lines = wrap(doc_title, title_only_font, max_w)
        line_h = draw.textbbox((0, 0), "Mg", font=title_only_font)
        line_height = (line_h[3] - line_h[1]) + 18
        total_h = line_height * len(lines)
        y = (VIDEO_HEIGHT - total_h) // 2
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=title_only_font)
            w = bbox[2] - bbox[0]
            draw.text(((VIDEO_WIDTH - w) // 2, y), line,
                      font=title_only_font, fill=_CARD_FG_STRONG)
            y += line_height

    img.save(out_path, "PNG")


# ─── Per-section spoken intro ─────────────────────────────────────────────────

def _synthesise_section_intros(
    intro_specs: list[dict],
    voice: str,
    output_dir: Path,
) -> dict[int, tuple[Path, float]]:
    """Synthesise one short Kokoro chunk per non-zero section.

    intro_specs items: {"section_idx": int, "text": str}
    Returns: {section_idx: (wav_path, duration_seconds)}
    Cached: re-using an existing WAV is keyed only on the file name, which
    includes the section index, so changing intro phrasing requires manually
    clearing intros/.
    """
    intros_dir = output_dir / "intros"
    intros_dir.mkdir(exist_ok=True)

    if not intro_specs:
        return {}

    # Lazy import — same path the main TTS uses
    import tts_engine
    from tts_engine import _prepare_kokoro_hf_cache, ENGLISH_VOICES, _wav_duration_seconds
    _prepare_kokoro_hf_cache(ENGLISH_VOICES)
    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    pipeline = KPipeline(repo_id="hexgrad/Kokoro-82M", lang_code="a", device=device)

    out: dict[int, tuple[Path, float]] = {}
    for spec in intro_specs:
        idx = spec["section_idx"]
        wav = intros_dir / f"section-{idx:02d}.wav"
        if wav.exists():
            out[idx] = (wav, _wav_duration_seconds(wav))
            continue
        audio_arrays = []
        for _, _, audio in pipeline(
            spec["text"], voice=voice,
            speed=tts_engine.NARRATION_SPEED, split_pattern=None,
        ):
            if audio is not None:
                audio_arrays.append(audio)
        if not audio_arrays:
            warn(f"  Intro for section {idx} produced no audio; skipping")
            continue
        combined = np.concatenate(audio_arrays)
        sf.write(str(wav), combined, samplerate=24000)
        out[idx] = (wav, _wav_duration_seconds(wav))
    return out


# ─── Per-section video render ─────────────────────────────────────────────────

def _open_file(path: Path) -> None:
    """Open *path* with the system default application, for --preview."""
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.run(["open", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])
    except Exception as exc:
        warn(f"Could not open file automatically: {exc}")


def _parse_section_spec(spec: str | None) -> set[int] | None:
    """Parse `--sections` into a set of indices. None means "all".

    Accepts comma-separated 0-based indices and inclusive ranges:
    `"0,3-5"` -> {0, 3, 4, 5}. Invalid fragments warn and are skipped rather
    than aborting, so a typo costs one section, not the run.
    """
    if not spec:
        return None
    wanted: set[int] = set()
    for fragment in spec.split(","):
        fragment = fragment.strip()
        if not fragment:
            continue
        try:
            if "-" in fragment.lstrip("-"):
                start_text, _, end_text = fragment.partition("-")
                start, end = int(start_text), int(end_text)
                if start > end:
                    start, end = end, start
                wanted.update(range(start, end + 1))
            else:
                wanted.add(int(fragment))
        except ValueError:
            warn(f"  ignoring unparseable --sections fragment {fragment!r}")
    return wanted or None


def _default_section_workers() -> int:
    """How many sections to encode at once when the caller does not say.

    Bounded by NVENC's concurrent-session limit on consumer cards rather than
    by core count: an RTX A1000 handles 6 simultaneous sessions, and each
    ffmpeg is already multi-threaded, so more workers mostly means slower
    individual encodes. Four is a deliberate compromise that leaves headroom
    on smaller GPUs and still cuts the wall clock substantially.
    """
    return min(4, max(1, (os.cpu_count() or 4) // 2))


def render_section_videos(
    annotated_blocks: list[dict],
    segments,
    keyframes: list[tuple[Path, float]],
    audio_path: Path,
    output_dir: Path,
    slug: str,
    voice: str,
    title_override: str = "",
    max_workers: int | None = None,
    only_sections: set[int] | None = None,
) -> list[Path]:
    """
    Render each ## section as an independent MP4: title-card intro + spoken
    intro (for non-zero sections) + the section's own audio slice + its
    keyframes.

    Why not stream-copy from the full MP4? Stream copy snaps to the nearest
    preceding keyframe and produces a ~1-2 sec bleed from the prior section.
    Re-encoding per section avoids that, and lets us prepend a section-specific
    title card + spoken intro so each video stands alone.

    Audio is extracted from the existing full MP3 via ffmpeg (no re-TTS of
    section content). Only the section intros are freshly synthesised.
    """
    if not annotated_blocks or not segments or not keyframes:
        return []

    h2_blocks = [b for b in annotated_blocks if b["kind"] == "h2"]
    if not h2_blocks:
        info("No ## headings found — skipping per-section render.")
        return []

    sections_dir = output_dir / "sections"
    sections_dir.mkdir(exist_ok=True)

    start_by_index = {seg.index: seg.start for seg in segments}
    end_by_index = {seg.index: seg.end for seg in segments}
    audio_total_dur = max(end_by_index.values()) if end_by_index else 0.0

    # Look up frames by phrase index. Some segments may have been skipped if
    # they had no DOM coords, so build a dict rather than a list.
    frame_by_index: dict[int, tuple[Path, float]] = {}
    for path, dur in keyframes:
        m = re.match(r"frame_(\d+)\.png$", path.name)
        if m:
            frame_by_index[int(m.group(1))] = (path, dur)

    doc_title = _doc_title(annotated_blocks, title_override)

    # ── 1. Plan all sections (boundaries + intro text) ───────────────────────
    # For sections 1+, we DROP the h2 heading from both audio and frames: the
    # title card + spoken intro already says "Section N: Heading." Including
    # the heading in the section MP4's content track would speak it twice and
    # double-highlight it. So:
    #   start_phrase  -> first phrase AFTER the heading's last phrase index
    #   start_t       -> end time of the heading's last phrase
    # Section 0 keeps its original behaviour (start at phrase 0, t=0) since
    # the title card just shows the doc title, not a "Section 0:" intro.
    plans: list[dict] = []
    for i, block in enumerate(h2_blocks):
        heading_indices = block["phrase_indices"]
        first_heading_idx = heading_indices[0]
        last_heading_idx = heading_indices[-1]
        heading_t = start_by_index.get(first_heading_idx)
        heading_end_t = end_by_index.get(last_heading_idx)
        if heading_t is None:
            continue

        if i == 0:
            start_phrase = 0
            start_t = 0.0
        else:
            start_phrase = last_heading_idx + 1
            start_t = heading_end_t if heading_end_t is not None else heading_t

        if i + 1 < len(h2_blocks):
            next_first = h2_blocks[i + 1]["phrase_indices"][0]
            end_t = start_by_index.get(next_first, audio_total_dur)
            end_phrase_exclusive = next_first
        else:
            end_t = audio_total_dur
            end_phrase_exclusive = max(end_by_index) + 1

        sec_num = _section_number(block["text"])
        sec_title = _section_title_only(block["text"])
        if i == 0:
            section_label = None
            intro_text = None
        else:
            section_label = f"Section {sec_num}" if sec_num is not None else f"Section {i}"
            intro_text = (
                f"{doc_title}. {section_label}: {sec_title}."
                if doc_title else f"{section_label}: {sec_title}."
            )
            intro_text = rewrite_for_tts(intro_text)

        plans.append({
            "index": i,
            "heading": block["text"],
            "section_label": section_label,
            "section_title": sec_title,
            "intro_text": intro_text,
            "start_t": start_t,
            "end_t": end_t,
            "start_phrase": start_phrase,
            "end_phrase_exclusive": end_phrase_exclusive,
        })

    # Restrict to the requested sections before any work happens, so --sections
    # skips title cards and intro synthesis too, not just the encode.
    if only_sections is not None:
        requested = sorted(only_sections)
        plans = [p for p in plans if p["index"] in only_sections]
        if not plans:
            warn(f"  --sections {requested} matched no sections; nothing to render")
            return []
        info(f"  --sections: rendering {len(plans)} of the document's sections")

    # ── 2. Render all title cards (cheap, do them up-front) ──────────────────
    cards_dir = output_dir / "title_cards"
    cards_dir.mkdir(exist_ok=True)
    for plan in plans:
        plan["card_path"] = cards_dir / f"section-{plan['index']:02d}.png"
        _render_title_card(
            doc_title=doc_title,
            section_label=plan["section_label"],
            section_heading=plan["section_title"],
            out_path=plan["card_path"],
        )

    # ── 3. Synthesise spoken intros (skips section 0 since intro_text is None) ─
    intro_specs = [
        {"section_idx": p["index"], "text": p["intro_text"]}
        for p in plans if p["intro_text"]
    ]
    step(f"Synthesising {len(intro_specs)} section intro(s) …")
    intros = _synthesise_section_intros(intro_specs, voice, output_dir)

    # ── 4. Render each section MP4 ───────────────────────────────────────────
    step(f"Rendering {len(plans)} per-section MP4(s) …")
    use_nvenc = has_nvenc()
    vcodec = "h264_nvenc" if use_nvenc else "libx264"

    def _render_one(plan: dict) -> Path | None:
        """Render a single section MP4. Returns its path, or None on failure.

        Independent of every other section: each writes uniquely-named
        intermediates (`.section_NN_*`) and its own output, reads shared state
        read-only, and shells out to ffmpeg. That is what makes the loop below
        safe to run concurrently.
        """
        idx = plan["index"]
        section_slug = _section_slug(plan["heading"])
        out_path = sections_dir / f"{slug}-{section_slug}.mp4"
        if out_path.exists():
            out_path.unlink()

        # 4a. Audio: build a per-section WAV/MP3. Use ffmpeg to slice the
        # main audio, concatenated with the intro WAV if any.
        section_audio = output_dir / f".section_{idx:02d}_audio.m4a"
        if section_audio.exists():
            section_audio.unlink()

        intro_path: Path | None = None
        intro_dur = 0.0
        if idx in intros:
            intro_path, intro_dur = intros[idx]

        lead_in_ms = int(round(LEAD_IN_SECONDS * 1000))
        tail_pad_s = TAIL_OUT_SECONDS
        if intro_path is not None:
            # Two-input concat: intro WAV + sliced main audio, then adelay the
            # whole thing by LEAD_IN_SECONDS so the video opens with a brief
            # silent beat (avoids first-word clipping on slow-starting players),
            # and apad to add TAIL_OUT_SECONDS of silence so it doesn't end on
            # the final consonant of the last spoken word.
            audio_cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(intro_path),
                "-ss", f"{plan['start_t']:.3f}",
                "-to", f"{plan['end_t']:.3f}",
                "-i", str(audio_path),
                "-filter_complex",
                f"[0:a][1:a]concat=n=2:v=0:a=1,"
                f"adelay=delays={lead_in_ms}:all=1,"
                f"apad=pad_dur={tail_pad_s:.3f}[outa]",
                "-map", "[outa]",
                "-c:a", "aac", "-b:a", "192k",
                str(section_audio),
            ]
        else:
            # Section 0: prepend TITLE_CARD_SILENT_SECONDS of silence so the
            # silent title-card hold doesn't desync the audio from the frames.
            # Then add LEAD_IN_SECONDS more so the video opens with a silent
            # beat, and apad TAIL_OUT_SECONDS at the end. Use `adelay`
            # (single-source filter) rather than concat'ing an anullsrc stream
            # — concat across two sources introduced an audible hiss from AAC
            # encoder priming at the silence→speech edge.
            delay_ms = int(round(TITLE_CARD_SILENT_SECONDS * 1000)) + lead_in_ms
            audio_cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{plan['start_t']:.3f}",
                "-to", f"{plan['end_t']:.3f}",
                "-i", str(audio_path),
                "-af", f"adelay=delays={delay_ms}:all=1,apad=pad_dur={tail_pad_s:.3f}",
                "-c:a", "aac", "-b:a", "192k",
                str(section_audio),
            ]
        proc = subprocess.run(audio_cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not section_audio.exists():
            warn(f"  [{idx+1}/{len(plans)}] audio prep failed: {proc.stderr[-300:].strip()}")
            return None

        # 4b. Video: build a concat list — title card held for the lead-in
        # silence + (intro_dur or silent fallback) + each in-range frame at
        # its original duration. The lead-in matches the adelay above so the
        # title card stays on screen during that initial silent beat.
        concat_lines: list[str] = []
        card_hold = LEAD_IN_SECONDS + (intro_dur if intro_dur > 0 else TITLE_CARD_SILENT_SECONDS)
        concat_lines.append(f"file '{plan['card_path'].resolve()}'")
        concat_lines.append(f"duration {card_hold:.4f}")

        in_range_frames = [
            (i_idx, frame_by_index[i_idx])
            for i_idx in range(plan["start_phrase"], plan["end_phrase_exclusive"])
            if i_idx in frame_by_index
        ]
        if not in_range_frames:
            warn(f"  [{idx+1}/{len(plans)}] no in-range frames; skipping")
            section_audio.unlink(missing_ok=True)
            return None

        content_dur = sum(d for _, (_, d) in in_range_frames)
        for _, (fpath, dur) in in_range_frames:
            concat_lines.append(f"file '{fpath.resolve()}'")
            concat_lines.append(f"duration {dur:.4f}")
        # Tail-out: hold the last frame long enough that the VIDEO track is
        # always >= the AUDIO track, so the final `-shortest` mux trims a few
        # ms of held last frame instead of clipping the last spoken word and
        # the tail-pad silence off the audio. Probe the real audio duration
        # rather than trusting estimated frame durations to sum to the slice.
        try:
            _ad = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(section_audio)],
                capture_output=True, text=True,
            )
            audio_dur = float(_ad.stdout.strip())
        except (ValueError, OSError):
            audio_dur = 0.0
        tail_hold = TAIL_OUT_SECONDS
        if audio_dur:
            tail_hold = max(TAIL_OUT_SECONDS, audio_dur - (card_hold + content_dur) + 0.5)
        concat_lines.append(f"file '{in_range_frames[-1][1][0].resolve()}'")
        concat_lines.append(f"duration {tail_hold:.4f}")
        concat_lines.append(f"file '{in_range_frames[-1][1][0].resolve()}'")

        concat_path = output_dir / f".section_{idx:02d}_concat.txt"
        concat_path.write_text("\n".join(concat_lines), encoding="utf-8")

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_path),
            "-i", str(section_audio),
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", f"fps={FPS}",
            "-c:v", vcodec,
            "-preset", "p4" if use_nvenc else "medium",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        # Clean intermediates regardless of outcome
        concat_path.unlink(missing_ok=True)
        section_audio.unlink(missing_ok=True)

        if proc.returncode != 0 or not out_path.exists():
            warn(
                f"  [{idx+1}/{len(plans)}] {plan['heading'][:60]!r} failed: "
                f"{proc.stderr[-400:].strip()}"
            )
            return None

        size_mb = out_path.stat().st_size / (1024 * 1024)
        info(f"  [{idx+1}/{len(plans)}] {out_path.name}  ({size_mb:.1f} MB)")
        return out_path

    # Sections are independent, so render them concurrently. This is the
    # single largest cost in a run — 412s sequentially on the reference
    # document — and the work is almost entirely inside ffmpeg subprocesses,
    # so threads are enough; no GIL contention and no pickling of plan dicts.
    #
    # Worker count is capped for two reasons: NVENC allows a limited number of
    # concurrent sessions on consumer cards (6 verified fine on an RTX A1000),
    # and each ffmpeg is itself multi-threaded, so oversubscribing slows every
    # section down rather than speeding the batch up.
    workers = max(1, min(max_workers or _default_section_workers(), len(plans)))
    if workers == 1:
        results = [_render_one(plan) for plan in plans]
    else:
        info(f"  rendering {len(plans)} sections, {workers} at a time")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # Submitted in plan order and read back in the same order, so the
            # returned list matches the sequential version exactly regardless
            # of which section finishes first.
            results = list(pool.map(_render_one, plans))

    return [path for path in results if path is not None]


# ────────────────────────────────────────────────────────────────────────────────
# 7. CLI
# ────────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="narraoke",
        description="Turn a structured markdown doc into a narrated, scrolling video.",
    )
    p.add_argument("markdown", help="Path to the source markdown file")
    p.add_argument(
        "--voice",
        default=DEFAULT_VOICE,
        help=f"Kokoro voice (default: {DEFAULT_VOICE})",
    )
    p.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    p.add_argument(
        "--slug",
        help="Custom slug for output filenames (default: derived from filename)",
    )
    p.add_argument(
        "--smooth",
        action="store_true",
        help="Apply ffmpeg minterpolate between keyframes (slow encode, can produce artifacts at low scroll speeds; default is snap-to-phrase which looks fine when phrases are close together)",
    )
    p.set_defaults(smooth=False)
    p.add_argument(
        "--skip-tts",
        action="store_true",
        help="Reuse existing audio/timing files (debug: skip slow TTS step)",
    )
    p.add_argument(
        "--overrides",
        default=None,
        help="Path to a per-doc TTS overrides JSONC file. If omitted, "
             "<markdown>.tts-overrides.json is auto-loaded when present.",
    )
    p.add_argument(
        "--video-config",
        default=None,
        metavar="PATH",
        help="Path to a per-document render-settings JSONC file. If omitted, "
             "<markdown>.video.json is auto-loaded when present.",
    )
    p.add_argument(
        "--company-rules",
        default=None,
        metavar="DIR",
        help="Directory of shared company rule files (tier 3). Overrides "
             f"${_discovery.ENV_COMPANY_RULES} and "
             f"{_discovery.CONFIG_FILE_NAME}. A configured but missing "
             "directory is an error, not a warning.",
    )
    p.add_argument(
        "--user-rules",
        default=None,
        metavar="DIR",
        help="Directory of personal rule files (tier 2). Overrides "
             f"${_discovery.ENV_USER_RULES} and "
             f"{_discovery.CONFIG_FILE_NAME}.",
    )
    p.add_argument(
        "--section-workers",
        type=int,
        default=None,
        metavar="N",
        help=f"How many per-section MP4s to encode at once "
             f"(default: {_default_section_workers()}). 1 renders "
             f"sequentially. Sections are independent, so this is the "
             f"largest single speedup available in a run.",
    )
    p.add_argument(
        "--sections",
        default=None,
        metavar="SPEC",
        help="Render only these sections: a comma-separated list of 0-based "
             "indices and ranges, e.g. '0,3-5'. Skips the full-length video "
             "entirely, so a single section can be checked in a fraction of "
             "the time a whole render takes.",
    )
    p.add_argument(
        "--preview",
        action="store_true",
        help="Open the primary output when the run finishes.",
    )
    p.add_argument(
        "--no-split-sections",
        action="store_true",
        help="Skip the per-section MP4 export step (full video only)",
    )
    return p


# ── Versioned output directories ──────────────────────────────────────────────
# Layout:
#   <output_dir>/<slug>/<YYYY-MM-DDTHH-MM-SS>/  -- this run's artifacts
#   <output_dir>/<slug>/latest                  -- symlink to the most recent run
# `latest` is a relative symlink so the slug directory remains portable.

_VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}$")


def _slug_root(output_dir: Path, slug: str) -> Path:
    return output_dir / slug


def _new_version_dir(output_dir: Path, slug: str) -> Path:
    """Create and return a fresh timestamped version directory for this slug."""
    stamp = _dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    root = _slug_root(output_dir, slug)
    version_dir = root / stamp
    # Disambiguate sub-second collisions
    suffix = 0
    while version_dir.exists():
        suffix += 1
        version_dir = root / f"{stamp}-{suffix}"
    version_dir.mkdir(parents=True)
    return version_dir


def _list_prior_versions(output_dir: Path, slug: str) -> list[Path]:
    """Return existing version directories for this slug, newest last."""
    root = _slug_root(output_dir, slug)
    if not root.is_dir():
        return []
    entries = [
        p for p in root.iterdir()
        if p.is_dir() and not p.is_symlink() and _VERSION_RE.match(p.name[:19])
    ]
    return sorted(entries, key=lambda p: p.name)


def _update_latest_symlink(output_dir: Path, slug: str, version_dir: Path) -> None:
    """Point <slug>/latest at *version_dir* (relative). Best-effort: warns
    rather than failing on platforms / filesystems where symlinks are
    unavailable (e.g. some Windows configurations)."""
    root = _slug_root(output_dir, slug)
    link = root / "latest"
    target_rel = version_dir.name
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(target_rel, link, target_is_directory=True)
        info(f"  latest -> {target_rel}")
    except OSError as e:
        warn(f"  Could not update 'latest' symlink: {e}")


# Files copied from a prior version when --skip-tts is set, so the new run can
# reuse the slow TTS step. Anything not in this list is regenerated.
_REUSABLE_FILENAMES_BY_SUFFIX = [
    ".mp3",                 # main audio
    ".srt",                 # subtitles
    "_timings.json",        # phrase timings
    "_chunk_durations.json",
]


def _seed_from_prior_version(prior: Path, dest: Path, slug: str) -> bool:
    """Copy reusable artifacts from *prior* into *dest*. Returns True if all
    of mp3 + timings + chunk_durations were found and copied (the trio needed
    to actually skip TTS + drift-aware timing); False otherwise."""
    missing: list[str] = []
    copied: list[str] = []
    for suffix in _REUSABLE_FILENAMES_BY_SUFFIX:
        src = prior / f"{slug}{suffix}"
        if not src.exists():
            missing.append(src.name)
            continue
        shutil.copy2(src, dest / src.name)
        copied.append(src.name)
    if copied:
        info(f"  Reused from {prior.name}: {', '.join(copied)}")
    if missing:
        warn(f"  Missing in prior version: {', '.join(missing)}")
    # Drift-aware timing needs chunk durations; without them we can't skip TTS
    # cleanly. Treat missing trio as failure to seed.
    required = {f"{slug}.mp3", f"{slug}_timings.json", f"{slug}_chunk_durations.json"}
    return required.issubset(set(copied))


def _compute_summary_dwell(
    structured_chunks: list[dict],
    annotated_blocks: list[dict],
    code_coords: list[dict],
    table_coords: list[dict],
) -> tuple[list[float], dict[int, float]]:
    """For each summary chunk, compute trailing-silence seconds + a per-phrase
    dwell map for build_keyframes to consume.

    Returns:
      silence_after_chunk -- list parallel to structured_chunks; values are
        seconds of silence to insert after each chunk's audio.
      dwell_by_phrase -- dict mapping the summary phrase index to its dwell
        seconds, used by build_keyframes to allocate scroll-pan frames.
    """
    # Map block position -> (kind, visual element coord dict)
    code_by_id = {c["id"]: c for c in (code_coords or [])}
    table_by_id = {c["id"]: c for c in (table_coords or [])}

    # Walk annotated_blocks to pair each summary block with its visual element.
    # Same logic as build_keyframes' summary_to_visual but we also need the
    # visual element height.
    summary_phrase_to_visual: dict[int, dict] = {}
    prev_summary: dict | None = None
    for block in annotated_blocks:
        if block.get("tts_summary_for_code") or block.get("tts_summary_for_table"):
            prev_summary = block
            continue
        if prev_summary is None:
            continue
        visual = None
        if block.get("kind") == "code":
            visual = code_by_id.get(f'code-{block.get("code_idx", 0)}')
        elif block.get("kind") == "table":
            visual = table_by_id.get(f'table-{block.get("table_idx", 0)}')
        if visual is not None:
            for pi in prev_summary.get("phrase_indices", []):
                summary_phrase_to_visual[pi] = visual
        prev_summary = None

    def dwell_for_block(visual: dict) -> float:
        block_h = visual.get("height", 0)
        # How many pixels of scroll are needed? If the block fits in the
        # viewport (minus the top margin), no scroll — just a short hold.
        usable_viewport = VIDEO_HEIGHT - 80
        if block_h <= usable_viewport:
            return max(DWELL_MIN_S, DWELL_FITS_PAUSE_S)
        scroll_px = block_h - usable_viewport
        scroll_s = scroll_px / SCROLL_PX_PER_SECOND
        return max(DWELL_MIN_S, min(DWELL_MAX_S, scroll_s + DWELL_BOTTOM_PAUSE_S))

    silence_after_chunk: list[float] = []
    dwell_by_phrase: dict[int, float] = {}
    for chunk in structured_chunks:
        indices = chunk.get("phrase_indices", [])
        dwell = 0.0
        visual = None
        # A chunk's phrases all share the same visual element when this chunk
        # is a code/table summary. Look up once.
        for pi in indices:
            v = summary_phrase_to_visual.get(pi)
            if v is not None:
                visual = v
                break
        if visual is not None:
            dwell = dwell_for_block(visual)
            # Apply dwell to ONLY the last phrase of the chunk — that's the
            # segment that owns the trailing silence + scroll-pan time. The
            # earlier phrases in the chunk just narrate (camera anchored at
            # the visual element's top for the whole chunk).
            dwell_by_phrase[indices[-1]] = dwell
        # Add any tts-pause sentinel time attached to this chunk. Pause
        # silence is "dead air" (no scroll motion); it just extends the
        # last phrase's segment so the audio + video timelines stay aligned.
        pause = float(chunk.get("trailing_pause_seconds", 0.0))
        if pause and indices:
            dwell_by_phrase[indices[-1]] = dwell_by_phrase.get(indices[-1], 0.0) + pause
        silence_after_chunk.append(dwell + pause)

    n_with_dwell = sum(1 for d in silence_after_chunk if d > 0)
    if n_with_dwell:
        total = sum(silence_after_chunk)
        info(
            f"  Summary dwell: {n_with_dwell} chunks need scroll/hold time, "
            f"adding {total:.1f}s of silent dwell total"
        )
    return silence_after_chunk, dwell_by_phrase


def main() -> None:
    args = build_parser().parse_args()

    md_path = Path(args.markdown).expanduser().resolve()
    if not md_path.is_file():
        from utils import error
        error(f"Markdown file not found: {md_path}")
        sys.exit(1)

    # Resolve and load per-doc TTS overrides.
    # Explicit --overrides wins; otherwise auto-load <markdown>.tts-overrides.json.
    if args.overrides:
        overrides_path = Path(args.overrides).expanduser().resolve()
        if not overrides_path.is_file():
            from utils import error
            error(f"--overrides file not found: {overrides_path}")
            sys.exit(1)
    else:
        overrides_path = md_path.with_suffix(md_path.suffix + ".tts-overrides.json")
    if overrides_path.is_file():
        info(f"Overrides: {overrides_path}")
    else:
        info("Overrides: (none — no companion file found)")

    # Assemble all four rule tiers. A configured-but-missing company directory
    # is fatal: rendering with a silently-absent NDA rule would mispronounce a
    # client name in a delivered video.
    try:
        stack = build_rule_stack(
            project_path=overrides_path,
            company_rules=args.company_rules,
            user_rules=args.user_rules,
        )
    except _discovery.CompanyRulesMissing as e:
        from utils import error
        error(str(e))
        sys.exit(1)
    set_rule_stack(stack)
    report_rule_stack(stack)

    # Per-document render settings. Absent is normal — every field defaults to
    # what used to be hardcoded, so a document without one renders unchanged.
    doc_config, config_warnings = docconfig.load(
        md_path, Path(args.video_config).expanduser() if args.video_config else None
    )
    for message in config_warnings:
        warn(f"  {message}")
    apply_doc_config(doc_config)
    info("Render settings:")
    for line in docconfig.summary_lines(doc_config):
        info(line)

    base_output_dir = Path(args.output_dir).expanduser().resolve()
    base_output_dir.mkdir(parents=True, exist_ok=True)

    slug = args.slug or slugify(md_path.stem)
    info(f"Slug: {slug}")

    # Snapshot prior versions BEFORE creating this run's directory. Listing
    # afterwards would include the empty directory we just made, so --skip-tts
    # would "seed" from it, find nothing, and silently run full TTS anyway.
    prior_versions = _list_prior_versions(base_output_dir, slug)

    # New timestamped version dir for this run. All subsystems write into it.
    output_dir = _new_version_dir(base_output_dir, slug)
    info(f"Version dir: {output_dir.relative_to(base_output_dir)}")

    # If --skip-tts is set, seed the new version with audio + timings from the
    # most recent prior run so the cache short-circuits trigger.
    if args.skip_tts:
        if not prior_versions:
            warn("--skip-tts set but no prior versions exist; will synthesise fresh.")
        else:
            most_recent = prior_versions[-1]
            info(f"Seeding from prior version: {most_recent.name}")
            ok = _seed_from_prior_version(most_recent, output_dir, slug)
            if not ok:
                warn("Prior version was incomplete; TTS will run for missing pieces.")

    step("Environment checks …")
    check_ffmpeg()
    info("  ffmpeg: OK")

    # ── 1. Parse markdown into blocks + phrases ───────────────────────────────
    step(f"Loading {md_path.name} …")
    blocks = load_narration_blocks(md_path, skip_headings=doc_config.skip_headings)
    phrases, annotated = build_phrase_index(blocks)
    info(f"  Blocks: {len(annotated)}, Narration phrases: {len(phrases)}")
    # Advisory: code blocks falling back to generic narration. Reported here
    # rather than after the screenshot because it depends only on the parse,
    # and the author can act on it before spending ~16 minutes rendering.
    for message in _check_code_summaries(annotated):
        warn(message)

    # ── 2. Generate video-only HTML ───────────────────────────────────────────
    step("Rendering video HTML …")
    html_path = output_dir / f"{slug}_video.html"
    render_video_html(annotated, html_path, title_override=doc_config.title)
    info(f"  HTML: {html_path}")

    # ── 3. Capture screenshot + phrase coordinates ────────────────────────────
    screenshot, coords, code_coords, table_coords = capture_page(html_path, output_dir)
    for message in _check_phrase_coverage(phrases, annotated, coords):
        warn(message)

    # ── 4. TTS ────────────────────────────────────────────────────────────────
    step("Synthesising audio …")
    from tts_engine import synthesise_article
    structured_chunks = build_tts_chunks(annotated, phrases, max_chars=DEFAULT_CHUNK_CHARS)
    chunk_texts = [c["text"] for c in structured_chunks]
    info(f"  TTS chunks: {len(chunk_texts)} "
         f"(headings: {sum(1 for c in structured_chunks if len(c['phrase_indices']) == 1 and c['text'].endswith('.'))})")

    # For each summary chunk, compute trailing dwell silence so the camera
    # has time to scroll through the corresponding code/table block. The
    # silence becomes part of the chunk's reported duration so the timing
    # pipeline gives that phrase a longer slot, and build_keyframes uses the
    # extra time to emit scroll-pan frames.
    silence_after_chunk, dwell_by_phrase = _compute_summary_dwell(
        structured_chunks, annotated, code_coords, table_coords,
    )

    # If --skip-tts seeded MP3+chunk_durations into this version dir, the call
    # below short-circuits and returns them; otherwise Kokoro runs.
    audio_path, chunk_durations = synthesise_article(
        chunks=chunk_texts,
        voice=args.voice,
        output_dir=output_dir,
        slug=slug,
        return_durations=True,
        silence_after_chunk=silence_after_chunk,
    )

    # ── 5. Timing data ────────────────────────────────────────────────────────
    step("Generating timing data …")
    from timing import generate_timings
    # Pair each chunk's phrase indices with its measured audio duration so
    # timings can be distributed *within* the chunk (prevents drift).
    if len(chunk_durations) != len(structured_chunks):
        warn(
            f"Chunk-duration count ({len(chunk_durations)}) doesn't match "
            f"chunk count ({len(structured_chunks)}) — timing may drift."
        )
    # silence_after_chunk[i] is the trailing dwell silence baked into chunk i.
    # Pass it to timing.py so the dwell goes ONLY to the chunk's last phrase
    # instead of being spread proportionally across all phrases.
    chunk_map = [
        {
            "phrase_indices": c["phrase_indices"],
            "duration": d,
            "trailing_silence": silence_after_chunk[i] if silence_after_chunk else 0.0,
        }
        for i, (c, d) in enumerate(zip(structured_chunks, chunk_durations))
    ]
    segments, srt_path, json_path = generate_timings(
        phrases=phrases,
        audio_path=audio_path,
        output_dir=output_dir,
        slug=slug,
        chunk_map=chunk_map,
    )

    # ── 6. Build keyframes ────────────────────────────────────────────────────
    keyframes = build_keyframes(
        screenshot, coords, segments, output_dir,
        annotated_blocks=annotated,
        code_coords=code_coords, table_coords=table_coords,
        dwell_by_phrase=dwell_by_phrase,
    )

    # ── 7. Encode MP4 ─────────────────────────────────────────────────────────
    # --sections exists to check one part quickly, and the full-length encode
    # is the most expensive single step, so skip it entirely in that mode.
    if args.sections:
        info("Skipping the full-length video (--sections given)")
        mp4_path = output_dir / f"{slug}.mp4"
    else:
        mp4_path = encode_video(
            keyframes, audio_path, output_dir, slug, smooth=args.smooth,
        )

    # ── 8. Per-section MP4s ───────────────────────────────────────────────────
    if args.no_split_sections:
        section_paths: list[Path] = []
    else:
        section_paths = render_section_videos(
            annotated_blocks=annotated,
            segments=segments,
            keyframes=keyframes,
            audio_path=audio_path,
            output_dir=output_dir,
            slug=slug,
            voice=args.voice,
            title_override=doc_config.title,
            max_workers=args.section_workers,
            only_sections=_parse_section_spec(args.sections),
        )

    # ── Summary ───────────────────────────────────────────────────────────────
    finish_stages()
    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)

    for label, path in [
        ("HTML", html_path),
        ("Audio (MP3)", audio_path),
        ("Subtitles", srt_path),
        ("Timings (JSON)", json_path),
        ("Video (MP4)", mp4_path),
    ]:
        if path.exists():
            size = path.stat().st_size
            unit = "MB" if size > 1024 * 1024 else "KB"
            value = size / (1024 * 1024) if unit == "MB" else size / 1024
            print(f"{label:<16}: {path}  ({value:.1f} {unit})")
    if section_paths:
        print(f"\nPer-section MP4s ({len(section_paths)}):")
        for p in section_paths:
            size_mb = p.stat().st_size / (1024 * 1024)
            print(f"  {p.name}  ({size_mb:.1f} MB)")

    # Mark this run as the latest version for this slug.
    _update_latest_symlink(base_output_dir, slug, output_dir)
    print(f"\nVersion: {output_dir}")
    print(f"Latest:  {base_output_dir / slug / 'latest'}")

    timings = stage_timings()
    if timings:
        total = sum(seconds for _, seconds in timings)
        print(f"\nTime: {_format_duration(total)} total")
        # Slowest first: on a ~16 minute run the interesting question is
        # always "what dominated?".
        for label, seconds in sorted(timings, key=lambda t: -t[1])[:6]:
            share = (seconds / total * 100) if total else 0
            print(f"  {_format_duration(seconds):>8}  {share:4.0f}%  {label}")

    # Warnings land last, after the paths, so they are the final thing on
    # screen. A real defect would otherwise be buried mid-log among hundreds
    # of routine lines in a ~16 minute run.
    warnings = collected_warnings()
    if warnings:
        print(f"\n{len(warnings)} warning(s) during this run:")
        for message in warnings:
            print(f"  ! {message}")

    print("=" * 60)

    if args.preview:
        # With --sections there is no full-length video, so the first
        # rendered section is the thing worth looking at.
        primary = mp4_path if mp4_path.exists() else (
            section_paths[0] if section_paths else audio_path
        )
        info(f"Opening {primary} …")
        _open_file(primary)


if __name__ == "__main__":
    main()
