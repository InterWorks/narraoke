"""Shared utilities: slugify, caching, ffmpeg check, console output."""
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


# ── Console helpers ──────────────────────────────────────────────────────────

def info(msg: str) -> None:
    print(f"[INFO]  {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[WARN]  {msg}", flush=True)


def error(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr, flush=True)


def step(msg: str) -> None:
    print(f"\n>>> {msg}", flush=True)


# ── Text helpers ─────────────────────────────────────────────────────────────

def slugify(text: str, max_len: int = 80) -> str:
    """Convert a title string to a safe filename slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    text = text.strip("-")
    return text[:max_len] or "article"


def clean_text(text: str) -> str:
    """Normalise whitespace and fix common encoding artefacts."""
    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Remove non-breaking spaces and other weird Unicode spaces
    text = re.sub(r"[\u00a0\u200b\u200c\u200d\ufeff]", " ", text)
    # Collapse runs of spaces/tabs (but preserve newlines)
    text = re.sub(r"[ \t]+", " ", text)
    # Strip leading/trailing whitespace per line
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(lines)
    return text.strip()


def split_sentences(text: str) -> list[str]:
    """Split text into sentences using simple regex heuristics."""
    # Treat every line break in the source as a hard boundary first — headings
    # rarely end with sentence punctuation, so without this they get glued to
    # the first sentence of the section that follows.
    sentence_pattern = r"(?<=[.!?])\s+(?=[A-Z\"])"
    result: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for part in re.split(sentence_pattern, line):
            part = part.strip()
            if not part:
                continue
            # Further split very long sentences at commas/semicolons if > 300 chars
            if len(part) > 300:
                sub = re.split(r"(?<=[,;])\s+", part)
                result.extend(s.strip() for s in sub if s.strip())
            else:
                result.append(part)
    return result


def split_phrases(sentences: list[str], min_chars: int = 20) -> list[str]:
    """
    Split sentences further at natural vocal pause points:
    commas, semicolons, colons, em-dashes, en-dashes, and parentheses.

    Phrases shorter than *min_chars* are merged with the next phrase to avoid
    very short flashes of highlighted text.
    """
    # Punctuation that signals a natural pause
    _PAUSE_PATTERN = re.compile(r"(?<=[,;:\u2014\u2013])\s+|(?<=\))\s+|(?<=—)\s+")

    raw: list[str] = []
    for sentence in sentences:
        parts = _PAUSE_PATTERN.split(sentence)
        raw.extend(p.strip() for p in parts if p.strip())

    # Merge phrases that are too short into the following one
    merged: list[str] = []
    carry = ""
    for phrase in raw:
        combined = (carry + " " + phrase).strip() if carry else phrase
        if len(combined) < min_chars:
            carry = combined
        else:
            merged.append(combined)
            carry = ""
    if carry:
        if merged:
            merged[-1] = (merged[-1] + " " + carry).strip()
        else:
            merged.append(carry)

    return merged


def chunk_sentences(sentences: list[str], max_chars: int = 500) -> list[str]:
    """Group sentences into chunks that stay under *max_chars* each."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for sent in sentences:
        if current_len + len(sent) + 1 > max_chars and current:
            chunks.append(" ".join(current))
            current = [sent]
            current_len = len(sent)
        else:
            current.append(sent)
            current_len += len(sent) + 1
    if current:
        chunks.append(" ".join(current))
    return chunks


# ── ffmpeg ───────────────────────────────────────────────────────────────────

def check_ffmpeg() -> str:
    """Return the path to ffmpeg or raise RuntimeError with instructions."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    raise RuntimeError(
        "ffmpeg not found on PATH.\n"
        "Windows: download from https://www.gyan.dev/ffmpeg/builds/ and add to PATH.\n"
        "Or install via: winget install ffmpeg\n"
        "Then restart your terminal."
    )


def has_nvenc() -> bool:
    """Return True if ffmpeg can actually encode with h264_nvenc (not just list it)."""
    try:
        # Do a real encode dry-run with h264_nvenc.
        # Frame must be >= NVENC's minimum (typically 145x49); use 256x256.
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=black:s=256x256:d=0.1",
                "-vcodec", "h264_nvenc", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


# ── Caching ──────────────────────────────────────────────────────────────────

_CACHE_FILE_NAME = ".article_cache.json"


def _load_cache(output_dir: Path) -> dict:
    cache_path = output_dir / _CACHE_FILE_NAME
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(output_dir: Path, cache: dict) -> None:
    cache_path = output_dir / _CACHE_FILE_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def url_cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def cache_get(output_dir: Path, url: str) -> dict | None:
    cache = _load_cache(output_dir)
    return cache.get(url_cache_key(url))


def cache_set(output_dir: Path, url: str, data: dict) -> None:
    cache = _load_cache(output_dir)
    cache[url_cache_key(url)] = data
    _save_cache(output_dir, cache)
