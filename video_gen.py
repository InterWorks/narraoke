"""
Karaoke-style video generation.

Strategy
--------
Each sentence is a static frame held for N seconds. Rather than feeding
24 identical copies of a frame through MoviePy, we:

1. Render one PNG per sentence in parallel (ProcessPoolExecutor).
2. Write an ffmpeg concat script that specifies each image + its duration.
3. Call ffmpeg directly to encode — it handles frame duplication internally,
   which is dramatically faster than streaming raw RGB through a pipe.
4. Mux the audio into the video in a second ffmpeg pass.

This avoids MoviePy's write_videofile bottleneck entirely.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from timing import TimedSegment
from utils import has_nvenc, info, step

# ── Video parameters ──────────────────────────────────────────────────────────
VIDEO_WIDTH = 1280
VIDEO_HEIGHT = 720
FPS = 24
BG_COLOR = (0, 0, 0)
ACTIVE_COLOR = (255, 220, 0)
CONTEXT_COLOR = (255, 255, 255)
CONTEXT_OPACITY = 0.35
TITLE_COLOR = (200, 200, 200)

_CONTEXT_SENTENCES = 2
_LINE_SPACING = 14
# Horizontal padding on each side (pixels) — text is constrained to this margin
_H_MARGIN = 80
# Progress bar
_BAR_HEIGHT = 4          # pixels tall
_BAR_MARGIN = 20         # pixels from bottom edge
_BAR_COLOR = (255, 220, 0)        # yellow — matches active highlight
_BAR_BG_COLOR = (50, 50, 50)      # dark grey track


def _wrap_pixels(text: str, font, max_width: int) -> str:
    """Wrap *text* so no rendered line exceeds *max_width* pixels with *font*."""
    from PIL import Image, ImageDraw
    dummy = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(dummy)

    words = text.split()
    lines: list[str] = []
    current: list[str] = []

    for word in words:
        trial = " ".join(current + [word])
        if draw.textlength(trial, font=font) <= max_width:
            current.append(word)
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]

    if current:
        lines.append(" ".join(current))

    return "\n".join(lines) if lines else text


def _find_font(size: int):
    from PIL import ImageFont
    candidates = [
        "arial.ttf",
        "Arial.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\Arial.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except (IOError, OSError):
            continue
    return ImageFont.load_default()


def _font_size_for(text: str, max_px: int = 56, min_px: int = 28) -> int:
    length = len(text)
    if length < 80:
        return max_px
    if length > 300:
        return min_px
    ratio = (length - 80) / (300 - 80)
    return int(max_px - ratio * (max_px - min_px))


def _measure(text: str, font, line_spacing: int = _LINE_SPACING) -> tuple[int, int]:
    """Return (width, height) of *text* rendered with *font*."""
    from PIL import Image, ImageDraw
    dummy = Image.new("RGB", (10, 10))
    d = ImageDraw.Draw(dummy)
    bb = d.multiline_textbbox((0, 0), text, font=font, spacing=line_spacing)
    return bb[2] - bb[0], bb[3] - bb[1]


def _text_block_height(text: str, font, line_spacing: int = _LINE_SPACING) -> int:
    return _measure(text, font, line_spacing)[1] + line_spacing


def _draw_centred(draw, cx: int, cy: int, text: str, font, fill, line_spacing: int = _LINE_SPACING) -> None:
    """Draw *text* centred on pixel (*cx*, *cy*) — works for multiline text."""
    w, h = _measure(text, font, line_spacing)
    draw.multiline_text((cx - w // 2, cy - h // 2), text, font=font,
                        fill=fill, spacing=line_spacing, align="center")


# ── Worker: render one PNG to disk (module-level = picklable) ─────────────────

def _render_frame_png(
    out_path: str,
    seg_texts: list[str],
    active_local: int,
    title: str,
    seg_index: int,
    seg_total: int,
) -> str:
    """Render a single karaoke frame and save it as a PNG. Returns out_path."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), color=BG_COLOR)
    draw = ImageDraw.Draw(img)

    title_font = _find_font(30)
    ctx_font = _find_font(34)
    act_font = _find_font(_font_size_for(seg_texts[active_local]))

    max_text_width = VIDEO_WIDTH - 2 * _H_MARGIN

    title_text = _wrap_pixels(title, title_font, max_text_width)
    _, title_h = _measure(title_text, title_font)
    _draw_centred(draw, VIDEO_WIDTH // 2, 30 + title_h // 2, title_text, title_font, TITLE_COLOR)

    wrapped = [
        _wrap_pixels(t, act_font if i == active_local else ctx_font, max_text_width)
        for i, t in enumerate(seg_texts)
    ]
    fonts = [act_font if i == active_local else ctx_font
             for i in range(len(seg_texts))]
    block_h = sum(_text_block_height(t, f) for t, f in zip(wrapped, fonts))
    y = max(120, (VIDEO_HEIGHT - block_h) // 2)

    for i, (text, font) in enumerate(zip(wrapped, fonts)):
        h = _text_block_height(text, font)
        if i == active_local:
            _draw_centred(draw, VIDEO_WIDTH // 2, y + h // 2, text, font, ACTIVE_COLOR)
        else:
            overlay = Image.new("RGBA", (VIDEO_WIDTH, h), (0, 0, 0, 0))
            odraw = ImageDraw.Draw(overlay)
            _draw_centred(odraw, VIDEO_WIDTH // 2, h // 2, text, font,
                          (*CONTEXT_COLOR, int(255 * CONTEXT_OPACITY)))
            img.paste(overlay, (0, y), mask=overlay)
        y += h

    # ── Progress bar ──────────────────────────────────────────────────────────
    bar_y = VIDEO_HEIGHT - _BAR_MARGIN - _BAR_HEIGHT
    # Track (full width)
    draw.rectangle(
        [_H_MARGIN, bar_y, VIDEO_WIDTH - _H_MARGIN, bar_y + _BAR_HEIGHT],
        fill=_BAR_BG_COLOR,
    )
    # Fill (proportional to progress)
    fill_w = int((VIDEO_WIDTH - 2 * _H_MARGIN) * (seg_index / max(seg_total - 1, 1)))
    if fill_w > 0:
        draw.rectangle(
            [_H_MARGIN, bar_y, _H_MARGIN + fill_w, bar_y + _BAR_HEIGHT],
            fill=_BAR_COLOR,
        )

    img.save(out_path, format="PNG")
    return out_path


# ── ffmpeg helpers ────────────────────────────────────────────────────────────

def _encode_slideshow(
    concat_script: str,
    silent_mp4: Path,
    codec: str,
    ffmpeg_params: list[str],
    total_seconds: float | None = None,
) -> None:
    """Use ffmpeg concat demuxer to encode PNGs → silent MP4.

    If *total_seconds* is provided, prints a live progress line driven by
    ffmpeg's `-progress` output.
    """
    import sys as _sys
    import time as _time

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_script,
        "-vf", f"fps={FPS}",
        "-pix_fmt", "yuv420p",
        *ffmpeg_params,
        # Machine-readable progress on stdout: key=value lines, blank line
        # between snapshots, ending with `progress=end`.
        "-progress", "pipe:1", "-nostats",
        str(silent_mp4),
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    snapshot: dict[str, str] = {}
    last_print = 0.0
    is_tty = _sys.stdout.isatty()
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        key, _, value = line.partition("=")
        snapshot[key] = value
        if key != "progress":
            continue
        # End of one snapshot — render a status line.
        out_us = int(snapshot.get("out_time_us", "0") or "0")
        out_s = out_us / 1_000_000
        fps = snapshot.get("fps", "?")
        speed = snapshot.get("speed", "?")
        if total_seconds and total_seconds > 0:
            pct = min(100.0, 100 * out_s / total_seconds)
            msg = (f"\r[INFO]  Encoding: {pct:5.1f}%  "
                   f"({out_s:7.1f}s / {total_seconds:.1f}s)  "
                   f"fps={fps}  speed={speed}   ")
        else:
            msg = f"\r[INFO]  Encoding: {out_s:7.1f}s  fps={fps}  speed={speed}   "
        now = _time.monotonic()
        # Throttle to ~4 updates/sec; always print the final `end` snapshot.
        if value == "end" or now - last_print >= 0.25:
            if is_tty:
                _sys.stdout.write(msg)
                _sys.stdout.flush()
            else:
                # Non-TTY (logfile): print as normal lines, less frequently.
                if value == "end" or now - last_print >= 2.0:
                    print(msg.lstrip("\r").rstrip(), flush=True)
            last_print = now
        if value == "end":
            break

    stderr = proc.stderr.read() if proc.stderr else ""
    rc = proc.wait()
    if is_tty:
        _sys.stdout.write("\n")
        _sys.stdout.flush()
    if rc != 0:
        raise RuntimeError(f"ffmpeg encode failed:\n{stderr}")


def _mux_audio(silent_mp4: Path, audio_path: Path, final_mp4: Path) -> None:
    """Mux audio into the silent video, trimming to the shorter stream."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(silent_mp4),
        "-i", str(audio_path),
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        str(final_mp4),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg mux failed:\n{result.stderr}")


# ── Public API ────────────────────────────────────────────────────────────────

def generate_video(
    segments: list[TimedSegment],
    audio_path: Path,
    output_dir: Path,
    slug: str,
    title: str,
) -> Path:
    """Build a karaoke-style MP4 and return its path."""
    mp4_path = output_dir / f"{slug}.mp4"
    if mp4_path.exists():
        info(f"Video already exists, skipping: {mp4_path}")
        return mp4_path

    step("Building karaoke video …")

    n_workers = os.cpu_count() or 4
    info(f"  {len(segments)} segments, {n_workers} workers")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # ── Build jobs ────────────────────────────────────────────────────────
        jobs: list[tuple[int, str, list[str], int, float]] = []
        for i, seg in enumerate(segments):
            duration = seg.end - seg.start
            if duration <= 0:
                continue
            start_idx = max(0, i - _CONTEXT_SENTENCES)
            end_idx = min(len(segments), i + _CONTEXT_SENTENCES + 1)
            seg_texts = [s.text for s in segments[start_idx:end_idx]]
            active_local = i - start_idx
            png_path = str(tmp_path / f"frame_{i:04d}.png")
            jobs.append((i, png_path, seg_texts, active_local, duration))

        seg_total = len(jobs)

        # ── Render PNGs in parallel ───────────────────────────────────────────
        png_map: dict[int, tuple[str, float]] = {}
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futures = {
                pool.submit(_render_frame_png, png_path, seg_texts, active_local, title, i, seg_total): (i, duration)
                for i, png_path, seg_texts, active_local, duration in jobs
            }
            done = 0
            for future in as_completed(futures):
                i, duration = futures[future]
                png_map[i] = (future.result(), duration)
                done += 1
                if done % 5 == 0 or done == len(jobs):
                    info(f"  Rendered {done}/{len(jobs)} frames")

        # ── Write ffmpeg concat script ─────────────────────────────────────────
        concat_script = str(tmp_path / "concat.txt")
        with open(concat_script, "w") as f:
            for i in sorted(png_map):
                png_path, duration = png_map[i]
                # ffmpeg concat demuxer syntax
                f.write(f"file '{png_path}'\n")
                f.write(f"duration {duration:.6f}\n")
            # Repeat last frame to avoid truncation
            last_i = max(png_map)
            f.write(f"file '{png_map[last_i][0]}'\n")

        # ── Encode silent video ───────────────────────────────────────────────
        silent_mp4 = tmp_path / "silent.mp4"
        if has_nvenc():
            info("  GPU encoding via NVENC (h264_nvenc)")
            codec = "h264_nvenc"
            ffmpeg_params = ["-c:v", "h264_nvenc", "-rc:v", "vbr", "-cq:v", "24", "-b:v", "0"]
        else:
            info("  CPU encoding via libx264")
            codec = "libx264"
            ffmpeg_params = ["-c:v", "libx264", "-crf", "23", "-preset", "fast", "-threads", "0"]

        total_seconds = sum(d for _, d in png_map.values())
        info(f"  Encoding video … ({total_seconds:.1f}s of footage)")
        _encode_slideshow(concat_script, silent_mp4, codec, ffmpeg_params,
                          total_seconds=total_seconds)

        # ── Mux audio ─────────────────────────────────────────────────────────
        info("  Muxing audio …")
        _mux_audio(silent_mp4, audio_path, mp4_path)

    info(f"Video saved: {mp4_path}")
    return mp4_path
