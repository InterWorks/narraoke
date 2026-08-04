#!/usr/bin/env -S uv run python
"""
article_to_video — Convert a web article to TTS audio (+ optional karaoke video).

Usage
-----
  python article_to_video.py <URL> [options]
  python article_to_video.py --text-file article.txt [options]
  python article_to_video.py            # interactive paste (end with Ctrl-D)

See --help for full argument list.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import subprocess
import sys
from pathlib import Path

# ── Ensure project root is on sys.path ───────────────────────────────────────
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from utils import (
    cache_get,
    cache_set,
    check_ffmpeg,
    info,
    slugify,
    split_sentences,
    split_phrases,
    chunk_sentences,
    step,
    warn,
)


# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_VOICE = "random"        # resolved at runtime from ENGLISH_VOICES
DEFAULT_OUTPUT_DIR = Path("output")
DEFAULT_CHUNK_CHARS = 500       # max chars per TTS chunk

# af_nicole is Kokoro's ASMR/whisper voice — unsuitable for narrating articles.
ARTICLE_VOICE_BLACKLIST: frozenset[str] = frozenset({"af_nicole"})


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="article_to_video",
        description="Convert a web article to TTS audio and optional karaoke video.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "url",
        nargs="?",
        help="URL of the article to process (omit to use --text-file or paste interactively)",
    )
    p.add_argument(
        "--text-file",
        metavar="PATH",
        help="Read article text from a local file instead of fetching a URL",
    )
    p.add_argument(
        "--title",
        metavar="TITLE",
        help="Title for pasted/text-file input (defaults to first non-empty line)",
    )
    p.add_argument(
        "--no-video",
        dest="video",
        action="store_false",
        help="Skip karaoke MP4 generation (audio + subtitles + timings only)",
    )
    p.set_defaults(video=True)
    p.add_argument(
        "--voice",
        default=DEFAULT_VOICE,
        metavar="NAME",
        help=f"TTS voice name (default: {DEFAULT_VOICE})",
    )
    p.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        metavar="DIR",
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    p.add_argument(
        "--preview",
        action="store_true",
        help="Open the primary output file when done",
    )
    return p


# ── Helpers ───────────────────────────────────────────────────────────────────

def _open_file(path: Path) -> None:
    """Open *path* with the system default application."""
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(str(path))
        elif system == "Darwin":
            subprocess.run(["open", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])
    except Exception as exc:
        warn(f"Could not open file automatically: {exc}")


def _looks_like_html(s: str) -> bool:
    """Heuristic: treat input as HTML if it has a doctype or several tags."""
    head = s.lstrip()[:2048].lower()
    if head.startswith("<!doctype") or head.startswith("<html"):
        return True
    # Count opening tags in the first chunk; >5 strongly suggests HTML source
    import re as _re
    return len(_re.findall(r"<[a-z][a-z0-9]*[\s>/]", head)) >= 5


def _extract_from_html(html: str) -> tuple[str, str]:
    """Return (title, text) from an HTML string.

    Tries trafilatura with progressively looser settings, then falls back to
    a tag-stripping pass so we still return *something* readable for pages
    trafilatura can't classify.
    """
    import trafilatura

    # Guard: refuse to process Firefox/Chrome `view-source:` saved pages.
    # Those wrap the real source in an HTML viewer with id="viewsource" and
    # &lt;-escaped angle brackets, producing hours of TTS gibberish if we
    # treated the unescaped content as article text.
    head = html[:4096].lower()
    if 'id="viewsource"' in head or "viewsource.css" in head:
        from utils import error
        error(
            "This file is a saved 'view-source:' page, not the page itself.\n"
            "In your browser, navigate to the actual article URL (not "
            "view-source:...), then File → Save Page As → 'Web Page, "
            "HTML Only'.\nOr just pass the URL directly to this script."
        )
        sys.exit(1)

    title = ""
    try:
        meta = trafilatura.extract_metadata(html)
        if meta and meta.title:
            title = meta.title.strip()
    except Exception:
        pass

    # Pass 1: precision-favoured (clean, but can return nothing on odd markup)
    text = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
        output_format="txt",
    ) or ""

    # Pass 2: recall-favoured, no min-length filter
    if not text.strip():
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            no_fallback=False,
            output_format="txt",
        ) or ""

    # Pass 3: Next.js Flight payload (React Server Components stream)
    nextjs_text = _extract_nextjs_flight(html)
    if len(nextjs_text.split()) > len(text.split()):
        text = nextjs_text

    # Pass 4: BeautifulSoup tag-strip fallback
    if not text.strip():
        text = _html_to_text_fallback(html)

    if not title:
        import re as _re
        m = _re.search(r"<title[^>]*>(.*?)</title>", html, _re.IGNORECASE | _re.DOTALL)
        if m:
            title = _re.sub(r"\s+", " ", m.group(1)).strip()

    return title, text.strip()


def _extract_nextjs_flight(html: str) -> str:
    """Extract readable text from Next.js `self.__next_f.push([1, "..."])` payloads.

    Next.js (App Router) streams the rendered tree as escaped JSON inside
    script tags. The static HTML often contains only the shell — the article
    body lives in these payloads as React element tuples like
    ["$","p",null,{"children":"…"}]. We parse each payload, walk the tree,
    and collect children of <p>, <li>, <h1-6>, and <span> (skipping nav/UI
    chrome and meta-only payloads).
    """
    import json
    import re as _re

    payloads = _re.findall(
        r'self\.__next_f\.push\(\[\s*1\s*,\s*"((?:\\.|[^"\\])*)"\s*\]\)',
        html,
    )
    if not payloads:
        return ""

    # Tags whose children we want to read aloud.
    text_tags = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6"}
    # Tags that wrap inline text we should keep (concatenate into parent).
    inline_tags = {"span", "em", "strong", "b", "i", "a", "code"}
    # Tags we should never recurse into (navigation, icons, decorative).
    skip_tags = {"svg", "path", "circle", "polyline", "polygon", "nav",
                 "footer", "header", "aside", "script", "style", "button",
                 "form", "input"}

    def collect_inline(node) -> str:
        """Return concatenated text from an inline subtree."""
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            # React element: ["$", tag, key, props] OR a list of children
            if (len(node) >= 4 and node[0] == "$"
                    and isinstance(node[1], str)):
                tag = node[1]
                if tag in skip_tags:
                    return ""
                props = node[3] if isinstance(node[3], dict) else {}
                return collect_inline(props.get("children", ""))
            return "".join(collect_inline(c) for c in node)
        return ""

    blocks: list[str] = []

    def walk(node) -> None:
        if isinstance(node, list):
            # React element form: ["$", tag, key, props]
            if (len(node) >= 4 and node[0] == "$"
                    and isinstance(node[1], str)
                    and isinstance(node[3], dict)):
                tag = node[1]
                props = node[3]
                if tag in skip_tags:
                    return
                if tag in text_tags:
                    text = collect_inline(props.get("children", "")).strip()
                    if text:
                        blocks.append(text)
                    # Don't recurse — collect_inline already handled descendants.
                    return
                # Otherwise, recurse into children.
                walk(props.get("children", ""))
                return
            # Plain list of children
            for child in node:
                walk(child)
            return
        if isinstance(node, dict):
            for v in node.values():
                walk(v)

    # Each payload is "<id>:<json>\n<id>:<json>\n…" — parse line by line.
    for raw in payloads:
        try:
            chunk = bytes(raw, "utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            continue
        for line in chunk.splitlines():
            _, _, body = line.partition(":")
            body = body.lstrip()
            if not body or body[0] not in "[{\"":
                continue
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                continue
            walk(data)

    # De-duplicate while preserving order (payloads often repeat content).
    seen: set[str] = set()
    unique: list[str] = []
    for b in blocks:
        # Skip very short fragments (UI labels like "Learn", "Profile")
        if len(b) < 20:
            continue
        if b in seen:
            continue
        seen.add(b)
        unique.append(b)

    return "\n\n".join(unique)


def _html_to_text_fallback(html: str) -> str:
    """Crude HTML-to-text: drop scripts/styles/nav/footer, keep paragraph breaks."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        # Last-ditch regex strip if bs4 isn't available
        import re as _re
        text = _re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
        text = _re.sub(r"(?s)<[^>]+>", " ", text)
        return _re.sub(r"\s+", " ", text).strip()

    soup = BeautifulSoup(html, "lxml" if _has_lxml() else "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        tag.decompose()
    # Prefer <article> / <main> if present
    root = soup.find("article") or soup.find("main") or soup.body or soup
    # get_text with a separator preserves paragraph breaks
    text = root.get_text("\n", strip=True)
    return text


def _has_lxml() -> bool:
    try:
        import lxml  # noqa: F401
        return True
    except ImportError:
        return False


def _load_pasted_article(text_file: str | None, title_arg: str | None) -> dict:
    """Build an article dict from --text-file or interactive stdin paste."""
    from utils import clean_text, error, info

    if text_file:
        path = Path(text_file).expanduser()
        if not path.is_file():
            error(f"Text file not found: {path}")
            sys.exit(1)
        raw = path.read_text(encoding="utf-8")
        default_title_source = path.stem.replace("_", " ").replace("-", " ").strip()
    else:
        if sys.stdin.isatty():
            print("Paste article text or HTML below. End with Ctrl-D (Unix) or Ctrl-Z then Enter (Windows):", flush=True)
        raw = sys.stdin.read()
        default_title_source = ""

    html_title = ""
    if _looks_like_html(raw):
        info("Input detected as HTML — extracting article …")
        html_title, extracted = _extract_from_html(raw)
        if not extracted:
            error(
                "Could not extract article text from the pasted HTML.\n"
                "The page may be JS-rendered (article body not in source) or "
                "behind a paywall.\nTry copying the rendered text from the browser "
                "and pasting that instead."
            )
            sys.exit(1)
        raw = extracted

    text = clean_text(raw).strip()
    if not text:
        error("No article text provided.")
        sys.exit(1)

    # Safety: refuse to feed raw markup to TTS (would produce hours of gibberish).
    if _looks_like_html(text) or text.count("<") > 20 or "</" in text[:5000]:
        error(
            "Input text still looks like HTML/markup after extraction.\n"
            "Refusing to send to TTS to avoid synthesizing the markup itself.\n"
            "Try: save the page via browser (Ctrl-S, 'Webpage, HTML Only'), "
            "then run with --text-file PATH."
        )
        sys.exit(1)

    if title_arg:
        title = title_arg.strip()
    elif html_title:
        title = html_title
    else:
        first_line, _, rest = text.partition("\n")
        first_line = first_line.strip()
        # Treat the first line as a title only if it looks like one
        # (short and not ending in sentence-final punctuation).
        if first_line and len(first_line) <= 200 and not first_line.endswith((".", "!", "?")):
            title = first_line
            text = rest.strip() or first_line
        else:
            title = default_title_source or "Pasted Article"

    word_count = len(text.split())
    return {
        "url": "",
        "title": title or "Pasted Article",
        "text": text,
        "word_count": word_count,
    }


def _print_summary(outputs: dict[str, Path], duration_s: float) -> None:
    mins, secs = divmod(int(duration_s), 60)
    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)
    print(f"Audio duration : {mins}m {secs:02d}s")
    for label, path in outputs.items():
        if path and path.exists():
            size_mb = path.stat().st_size / (1024 * 1024)
            print(f"{label:<16}: {path}  ({size_mb:.1f} MB)")
    print("=" * 60)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Startup checks ────────────────────────────────────────────────────────
    step("Checking environment …")
    try:
        check_ffmpeg()
        info("ffmpeg: OK")
    except RuntimeError as exc:
        from utils import error
        error(str(exc))
        sys.exit(1)

    try:
        import torch
        cuda_ok = torch.cuda.is_available()
        device_name = torch.cuda.get_device_name(0) if cuda_ok else "CPU"
        info(f"CUDA   : {'available — ' + device_name if cuda_ok else 'not available (CPU mode)'}")
    except ImportError:
        info("PyTorch not installed — GPU status unknown")

    # ── Resolve input source ──────────────────────────────────────────────────
    if args.url:
        step("Extracting article …")
        from extractor import fetch_article
        article = fetch_article(args.url)
        cache_key = args.url
    else:
        step("Loading pasted article …")
        article = _load_pasted_article(args.text_file, args.title)
        info(f"Title    : {article['title']}")
        info(f"Words    : {article['word_count']:,}  (source: {'text-file' if args.text_file else 'stdin'})")
        cache_key = "text:" + hashlib.sha256(article["text"].encode("utf-8")).hexdigest()

    # ── Check cache ───────────────────────────────────────────────────────────
    cached = cache_get(output_dir, cache_key)
    slug = cached.get("slug") if cached else None
    if not slug:
        slug = slugify(article["title"])
        # For pasted/text-file input, append a short content hash so a future
        # paste of *different* content with the same title doesn't silently
        # reuse stale TTS chunks/MP3.
        if not args.url:
            content_hash = hashlib.sha256(article["text"].encode("utf-8")).hexdigest()[:8]
            slug = f"{slug}-{content_hash}"

    # Resolve voice — pick randomly if not explicitly set by the user
    import random as _random
    from tts_engine import ENGLISH_VOICES
    if args.voice == "random":
        pool = [v for v in ENGLISH_VOICES if v not in ARTICLE_VOICE_BLACKLIST]
        voice = _random.choice(pool)
    else:
        voice = args.voice
    info(f"Voice    : {voice}")

    sentences = split_sentences(article["text"])
    phrases = split_phrases(sentences)
    chunks = chunk_sentences(sentences, max_chars=DEFAULT_CHUNK_CHARS)
    info(f"Sentences: {len(sentences)}, Phrases: {len(phrases)}, TTS chunks: {len(chunks)}")

    # ── TTS ───────────────────────────────────────────────────────────────────
    step("Synthesising audio …")
    from tts_engine import synthesise_article
    audio_path = synthesise_article(
        chunks=chunks,
        voice=voice,
        output_dir=output_dir,
        slug=slug,
    )

    # ── Timings ───────────────────────────────────────────────────────────────
    step("Generating timing data …")
    from timing import generate_timings
    segments, srt_path, json_path = generate_timings(
        phrases=phrases,
        audio_path=audio_path,
        output_dir=output_dir,
        slug=slug,
    )

    # ── Cache result ──────────────────────────────────────────────────────────
    cache_set(output_dir, cache_key, {
        "slug": slug,
        "title": article["title"],
        "audio_path": str(audio_path),
        "srt_path": str(srt_path),
        "json_path": str(json_path),
    })

    outputs: dict[str, Path] = {
        "Audio (MP3)": audio_path,
        "Subtitles": srt_path,
        "Timings (JSON)": json_path,
    }

    # ── Video (optional) ──────────────────────────────────────────────────────
    mp4_path: Path | None = None
    if args.video:
        step("Generating karaoke video …")
        from video_gen import generate_video
        mp4_path = generate_video(
            segments=segments,
            audio_path=audio_path,
            output_dir=output_dir,
            slug=slug,
            title=article["title"],
        )
        outputs["Video (MP4)"] = mp4_path

    # ── Summary ───────────────────────────────────────────────────────────────
    from pydub import AudioSegment
    duration_s = len(AudioSegment.from_mp3(str(audio_path))) / 1000.0

    _print_summary(outputs, duration_s)

    # ── Preview ───────────────────────────────────────────────────────────────
    if args.preview:
        primary = mp4_path if mp4_path else audio_path
        info(f"Opening {primary} …")
        _open_file(primary)


if __name__ == "__main__":
    main()
