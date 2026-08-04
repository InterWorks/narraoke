"""
Generate word/sentence-level timing data aligned to the audio file.

Strategy
--------
1. If the TTS engine exposes native timestamps we use them directly (future).
2. Otherwise we estimate using average speech rate and the actual audio
   duration, distributing time proportionally to character count.

Output formats: JSON (internal) and SRT subtitle files.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from utils import info

# Average English speech rate (words per minute) – used as fallback
_WPM_DEFAULT = 150.0


@dataclass
class TimedSegment:
    index: int
    text: str
    start: float  # seconds
    end: float    # seconds

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "text": self.text,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
        }


def _audio_duration_seconds(audio_path: Path) -> float:
    """Return the duration of an MP3/WAV file in seconds."""
    from pydub import AudioSegment
    seg = AudioSegment.from_file(str(audio_path))
    return len(seg) / 1000.0


def _estimate_timings(
    sentences: list[str],
    total_duration: float,
) -> list[TimedSegment]:
    """
    Distribute *total_duration* across sentences proportionally to their
    character count (a better proxy than word count for timing).
    """
    char_counts = [max(len(s), 1) for s in sentences]
    total_chars = sum(char_counts)

    segments: list[TimedSegment] = []
    cursor = 0.0
    for i, (sent, chars) in enumerate(zip(sentences, char_counts)):
        duration = total_duration * (chars / total_chars)
        segments.append(TimedSegment(
            index=i,
            text=sent,
            start=cursor,
            end=cursor + duration,
        ))
        cursor += duration

    return segments


def _estimate_timings_chunked(
    phrases: list[str],
    chunk_map: list[dict],
) -> list[TimedSegment]:
    """
    Distribute timings **within each TTS chunk** rather than across the whole
    audio. This anchors the highlight to the real per-chunk audio durations,
    so drift can't accumulate over a 40-minute video.

    Each entry in *chunk_map* is:
        {"phrase_indices": list[int], "duration": float,
         "trailing_silence": float (optional)}

    `duration` is the total chunk timeline (audio + trailing silence). If
    `trailing_silence` is given, that many seconds of silence are concentrated
    on the LAST phrase of the chunk; the remaining "audio" portion is
    distributed across all phrases proportionally to character count. Without
    this split, a long block of trailing silence (e.g. a code-block scroll-pan
    dwell) would be spread evenly across every phrase in the chunk, leaving
    the highlight chasing the speech.
    """
    segments_by_index: dict[int, TimedSegment] = {}
    cursor = 0.0
    for chunk in chunk_map:
        indices = chunk["phrase_indices"]
        chunk_duration = float(chunk["duration"])
        trailing_silence = float(chunk.get("trailing_silence", 0.0) or 0.0)
        if not indices:
            cursor += chunk_duration
            continue
        audio_portion = max(0.0, chunk_duration - trailing_silence)
        char_counts = [max(len(phrases[i]), 1) for i in indices]
        total_chars = sum(char_counts)
        chunk_cursor = cursor
        last_idx = indices[-1]
        for i, chars in zip(indices, char_counts):
            dur = audio_portion * (chars / total_chars)
            if i == last_idx:
                dur += trailing_silence
            segments_by_index[i] = TimedSegment(
                index=i,
                text=phrases[i],
                start=chunk_cursor,
                end=chunk_cursor + dur,
            )
            chunk_cursor += dur
        cursor += chunk_duration

    # Emit in phrase-index order
    return [segments_by_index[i] for i in sorted(segments_by_index)]


def _format_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_srt(segments: list[TimedSegment], path: Path) -> None:
    lines = []
    for seg in segments:
        lines.append(str(seg.index + 1))
        lines.append(
            f"{_format_srt_time(seg.start)} --> {_format_srt_time(seg.end)}"
        )
        lines.append(seg.text)
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_json(segments: list[TimedSegment], path: Path) -> None:
    path.write_text(
        json.dumps([s.to_dict() for s in segments], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def generate_timings(
    phrases: list[str],
    audio_path: Path,
    output_dir: Path,
    slug: str,
    chunk_map: list[dict] | None = None,
) -> tuple[list[TimedSegment], Path, Path]:
    """
    Generate timing data for *phrases* aligned to *audio_path*.

    If *chunk_map* is provided, timings are distributed within each chunk's
    measured audio duration (prevents drift between highlight and speech over
    long videos). Otherwise falls back to character-proportional distribution
    across the full audio duration.

    Returns (segments, srt_path, json_path).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    srt_path = output_dir / f"{slug}.srt"
    json_path = output_dir / f"{slug}_timings.json"

    if srt_path.exists() and json_path.exists():
        info("Timing files already exist, loading from cache …")
        raw = json.loads(json_path.read_text(encoding="utf-8"))
        segments = [
            TimedSegment(r["index"], r["text"], r["start"], r["end"])
            for r in raw
        ]
        return segments, srt_path, json_path

    if chunk_map is not None:
        info("Estimating phrase timings (per-chunk, drift-resistant) …")
        segments = _estimate_timings_chunked(phrases, chunk_map)
    else:
        info("Measuring audio duration …")
        duration = _audio_duration_seconds(audio_path)
        info(f"Audio duration: {duration:.1f}s")
        info("Estimating phrase timings (proportional to character count) …")
        segments = _estimate_timings(phrases, duration)

    _write_json(segments, json_path)
    _write_srt(segments, srt_path)

    info(f"SRT saved : {srt_path}")
    info(f"JSON saved: {json_path}")

    return segments, srt_path, json_path
