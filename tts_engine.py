"""TTS engine abstraction: Kokoro primary, Coqui fallback.

Each engine exposes:
    synthesise(chunks, voice, output_dir, slug) -> list[Path]

Returns a list of per-chunk WAV paths in order.
"""
from __future__ import annotations

import os
import random
import wave
from pathlib import Path

from docconfig import DEFAULT_NARRATION_SPEED
from utils import info, warn, step

KOKORO_HF_REPO = "hexgrad/Kokoro-82M"

# Kokoro synthesis rate. Rebound by narraoke from the document's
# `.video.json` before synthesis starts. This value was previously written out
# twice — here and in narraoke — so changing narration speed meant knowing
# about both places; docconfig is now the single source of truth.
NARRATION_SPEED = DEFAULT_NARRATION_SPEED


def _hf_cache_root() -> Path:
    """Resolve the HF Hub cache root using the same precedence as huggingface_hub.

    Stdlib-only — used before huggingface_hub is imported, since its
    `constants` module reads HF_HUB_OFFLINE at import time.
    """
    cache_root = (
        os.environ.get("HF_HUB_CACHE")
        or (os.environ.get("HF_HOME") and os.path.join(os.environ["HF_HOME"], "hub"))
        or os.path.expanduser("~/.cache/huggingface/hub")
    )
    return Path(cache_root)


def _kokoro_snapshot_dir() -> Path | None:
    """Return the most recent local snapshot dir for KOKORO_HF_REPO, or None."""
    repo_dir = _hf_cache_root() / f"models--{KOKORO_HF_REPO.replace('/', '--')}"
    snapshots = repo_dir / "snapshots"
    if not snapshots.is_dir():
        return None
    entries = [p for p in snapshots.iterdir() if p.is_dir()]
    if not entries:
        return None
    return max(entries, key=lambda p: p.stat().st_mtime)


def _missing_voice_files(voices: list[str]) -> list[str]:
    """Voice names in *voices* that are not present in the local snapshot."""
    snapshot = _kokoro_snapshot_dir()
    if snapshot is None:
        return list(voices)
    return [v for v in voices if not (snapshot / "voices" / f"{v}.pt").exists()]


def _prepare_kokoro_hf_cache(voices: list[str]) -> None:
    """Ensure the Kokoro model + every voice in *voices* is cached, then go offline.

    Must be called before any import of kokoro / huggingface_hub in this
    module's call graph. Two phases:

      1. Online warm-up: if the model snapshot is missing, or any voice file
         is missing, download what's needed via huggingface_hub.
      2. Lock down: flip HF Hub into offline mode for the rest of the
         process so subsequent runs / voice loads don't hit the network.

    Honors a user-set HF_HUB_OFFLINE — we never override an explicit choice.
    """
    if "HF_HUB_OFFLINE" in os.environ:
        return

    snapshot = _kokoro_snapshot_dir()
    missing_voices = _missing_voice_files(voices)
    needs_download = snapshot is None or missing_voices

    if needs_download:
        if snapshot is None:
            warn(
                f"Kokoro model {KOKORO_HF_REPO} not found in HF cache — "
                "downloading (~330 MB) from the Hugging Face Hub."
            )
        if missing_voices:
            warn(
                f"{len(missing_voices)} Kokoro voice file(s) missing from cache "
                f"— downloading: {', '.join(missing_voices)}"
            )
        from huggingface_hub import hf_hub_download
        if snapshot is None:
            # Pull the core model file; kokoro will resolve the rest from the
            # same snapshot on first use.
            hf_hub_download(repo_id=KOKORO_HF_REPO, filename="kokoro-v1_0.pth")
            hf_hub_download(repo_id=KOKORO_HF_REPO, filename="config.json")
        for v in missing_voices:
            hf_hub_download(repo_id=KOKORO_HF_REPO, filename=f"voices/{v}.pt")

    # Phase 2: lock down. Setting the env var alone is not enough — by the
    # time we reach here (especially after `hf_hub_download` above), the
    # huggingface_hub.constants module has already cached HF_HUB_OFFLINE at
    # its import-time value (False). Patch the constant directly so the
    # runtime check actually returns True. Also set the env var so any
    # subprocesses / lazily-imported HF code sees the same choice.
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        import huggingface_hub.constants as _hf_constants
        _hf_constants.HF_HUB_OFFLINE = True
    except ImportError:
        pass  # huggingface_hub not installed yet — env var will take effect

# ── Kokoro English voices ─────────────────────────────────────────────────────
# American English (af_ = female, am_ = male) and British English (bf_, bm_).
# Non-English voices (Japanese, Chinese, etc.) are excluded — accents would be
# difficult for a typical US listener.
ENGLISH_VOICES: list[str] = [
    # American English — female
    "af_heart",
    "af_alloy",
    "af_aoede",
    "af_bella",
    "af_jessica",
    "af_kore",
    "af_nicole",
    "af_nova",
    "af_river",
    "af_sarah",
    "af_sky",
    # American English — male
    "am_adam",
    "am_echo",
    "am_eric",
    "am_fenrir",
    "am_liam",
    "am_michael",
    "am_onyx",
    "am_puck",
    "am_santa",
    # British English — female
    "bf_alice",
    "bf_emma",
    "bf_isabella",
    "bf_lily",
    # British English — male
    "bm_daniel",
    "bm_fable",
    "bm_george",
    "bm_lewis",
]


def random_voice() -> str:
    """Return a randomly chosen English Kokoro voice."""
    return random.choice(ENGLISH_VOICES)


# ── Kokoro ───────────────────────────────────────────────────────────────────

def _kokoro_available() -> bool:
    try:
        import kokoro  # noqa: F401
        return True
    except ImportError:
        return False


def _synthesise_kokoro(
    chunks: list[str],
    voice: str,
    output_dir: Path,
    slug: str,
) -> list[Path]:
    """Synthesise with Kokoro TTS. Returns per-chunk WAV paths."""
    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline

    step("Initialising Kokoro TTS …")
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    info(f"  Device: {device}")
    # lang_code 'a' = American English
    pipeline = KPipeline(repo_id="hexgrad/Kokoro-82M", lang_code="a", device=device)

    paths: list[Path] = []
    for i, chunk in enumerate(chunks):
        out_path = output_dir / f"{slug}_chunk_{i:04d}.wav"
        if out_path.exists():
            info(f"  chunk {i+1}/{len(chunks)} — cached, skipping")
            paths.append(out_path)
            continue

        info(f"  chunk {i+1}/{len(chunks)} — synthesising …")
        audio_arrays = []
        # KPipeline yields (gs, ps, audio) tuples
        for _, _, audio in pipeline(
            chunk, voice=voice, speed=NARRATION_SPEED, split_pattern=None
        ):
            if audio is not None:
                audio_arrays.append(audio)

        if not audio_arrays:
            warn(f"  chunk {i+1} produced no audio — skipping")
            continue

        combined = np.concatenate(audio_arrays)
        sf.write(str(out_path), combined, samplerate=24000)
        paths.append(out_path)

    return paths


# ── Coqui TTS ────────────────────────────────────────────────────────────────

def _coqui_available() -> bool:
    try:
        from TTS.api import TTS  # noqa: F401
        return True
    except ImportError:
        return False


def _synthesise_coqui(
    chunks: list[str],
    voice: str,
    output_dir: Path,
    slug: str,
) -> list[Path]:
    """Synthesise with Coqui TTS. Returns per-chunk WAV paths."""
    import torch
    from TTS.api import TTS

    step("Initialising Coqui TTS …")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    info(f"  Device: {device}")

    # Use a sensible default if the caller's voice name looks Kokoro-specific
    model_name = "tts_models/en/ljspeech/tacotron2-DDC"
    tts = TTS(model_name).to(device)

    paths: list[Path] = []
    for i, chunk in enumerate(chunks):
        out_path = output_dir / f"{slug}_chunk_{i:04d}.wav"
        if out_path.exists():
            info(f"  chunk {i+1}/{len(chunks)} — cached, skipping")
            paths.append(out_path)
            continue

        info(f"  chunk {i+1}/{len(chunks)} — synthesising …")
        tts.tts_to_file(text=chunk, file_path=str(out_path))
        paths.append(out_path)

    return paths


# ── Concatenation ─────────────────────────────────────────────────────────────

def _write_silence_wav(output_path: Path, duration_s: float, sample_params) -> None:
    """Write a WAV file of `duration_s` seconds of silence matching the
    given wave-module getparams() tuple (nchannels, sampwidth, framerate,
    nframes, comptype, compname)."""
    nchannels, sampwidth, framerate = (
        sample_params.nchannels,
        sample_params.sampwidth,
        sample_params.framerate,
    )
    nframes = int(round(duration_s * framerate))
    with wave.open(str(output_path), "wb") as w:
        w.setnchannels(nchannels)
        w.setsampwidth(sampwidth)
        w.setframerate(framerate)
        w.writeframes(b"\x00" * (nframes * nchannels * sampwidth))


def _concat_wavs(wav_paths: list[Path], output_path: Path) -> None:
    """Concatenate WAV files into a single WAV using the stdlib wave module."""
    if not wav_paths:
        raise RuntimeError("No WAV chunks to concatenate")

    with wave.open(str(wav_paths[0]), "rb") as first:
        params = first.getparams()

    with wave.open(str(output_path), "wb") as out_wav:
        out_wav.setparams(params)
        for p in wav_paths:
            with wave.open(str(p), "rb") as w:
                # Resample check: skip mismatched files with a warning
                if w.getframerate() != params.framerate:
                    warn(f"  {p.name}: sample rate mismatch — skipping")
                    continue
                out_wav.writeframes(w.readframes(w.getnframes()))


def _wav_to_mp3(wav_path: Path, mp3_path: Path) -> None:
    """Convert WAV → MP3 using pydub (requires ffmpeg)."""
    from pydub import AudioSegment
    audio = AudioSegment.from_wav(str(wav_path))
    audio.export(str(mp3_path), format="mp3", bitrate="192k")


# ── Public API ────────────────────────────────────────────────────────────────

def _wav_duration_seconds(wav_path: Path) -> float:
    """Return the duration of a WAV file in seconds (stdlib only)."""
    with wave.open(str(wav_path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def synthesise_article(
    chunks: list[str],
    voice: str,
    output_dir: Path,
    slug: str,
    return_durations: bool = False,
    silence_after_chunk: list[float] | None = None,
) -> Path | tuple[Path, list[float]]:
    """
    Synthesise *chunks* with the best available TTS engine.

    *chunks* is a list of strings (the text for each TTS pass).

    If *silence_after_chunk* is provided, it must be the same length as
    *chunks*. After each chunk's audio, that many seconds of silence are
    spliced into the final MP3. The returned per-chunk durations include
    that silence so the timing pipeline correctly accounts for it.

    Returns the path to the final MP3 file. If *return_durations* is True,
    returns (mp3_path, per_chunk_durations_seconds) so callers can distribute
    timing within each chunk and avoid global drift between highlight and
    spoken word.
    """
    if silence_after_chunk is not None and len(silence_after_chunk) != len(chunks):
        raise ValueError(
            f"silence_after_chunk length {len(silence_after_chunk)} "
            f"!= chunks length {len(chunks)}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    mp3_path = output_dir / f"{slug}.mp3"
    durations_path = output_dir / f"{slug}_chunk_durations.json"

    if mp3_path.exists():
        info(f"Audio already exists, skipping TTS: {mp3_path}")
        if return_durations:
            import json as _json
            if durations_path.exists():
                durations = _json.loads(durations_path.read_text(encoding="utf-8"))
                return mp3_path, durations
            # Fall back to a single bucket spanning the whole MP3 if we don't
            # have a cached durations file (older runs predate this feature).
            from pydub import AudioSegment
            seg = AudioSegment.from_file(str(mp3_path))
            return mp3_path, [len(seg) / 1000.0]
        return mp3_path

    # Warm the HF cache (model + every English voice) before any kokoro
    # import, then lock the runtime into offline mode. Covering all voices
    # — not just `voice` — keeps future random picks fully offline.
    _prepare_kokoro_hf_cache(ENGLISH_VOICES)

    # ── Pick engine ──────────────────────────────────────────────────────────
    if _kokoro_available():
        info("TTS engine: Kokoro")
        chunk_paths = _synthesise_kokoro(chunks, voice, output_dir, slug)
    elif _coqui_available():
        warn("Kokoro not available — falling back to Coqui TTS")
        chunk_paths = _synthesise_coqui(chunks, voice, output_dir, slug)
    else:
        raise RuntimeError(
            "No TTS engine found. Install kokoro:\n"
            "  pip install kokoro soundfile\n"
            "Or Coqui TTS:\n"
            "  pip install TTS"
        )

    if not chunk_paths:
        raise RuntimeError("TTS produced no audio chunks")

    # Measure each chunk's duration BEFORE cleanup — these underpin the
    # per-chunk timing distribution that keeps highlight in sync with speech.
    audio_durations = [_wav_duration_seconds(p) for p in chunk_paths]

    # Build the final chunk path list, interleaving silent WAVs where the
    # caller asked for trailing silence after a chunk. The reported chunk
    # duration becomes (audio + trailing silence) so the timing pipeline
    # gives that chunk's phrase(s) the right share of the timeline.
    if silence_after_chunk is None:
        merged_chunk_paths = list(chunk_paths)
        durations = list(audio_durations)
    else:
        # Use the first chunk's sample params as the silence template.
        with wave.open(str(chunk_paths[0]), "rb") as w0:
            template_params = w0.getparams()
        merged_chunk_paths = []
        durations = []
        silences_inserted = 0
        for i, p in enumerate(chunk_paths):
            merged_chunk_paths.append(p)
            dwell = float(silence_after_chunk[i] or 0.0)
            if dwell > 0.01:
                silent_path = output_dir / f"{slug}_silence_{i:04d}.wav"
                _write_silence_wav(silent_path, dwell, template_params)
                merged_chunk_paths.append(silent_path)
                silences_inserted += 1
                durations.append(audio_durations[i] + dwell)
            else:
                durations.append(audio_durations[i])
        if silences_inserted:
            info(f"  Inserted {silences_inserted} dwell-silence segments into audio")

    import json as _json
    durations_path.write_text(_json.dumps(durations), encoding="utf-8")

    # ── Merge ────────────────────────────────────────────────────────────────
    step("Concatenating audio chunks …")
    combined_wav = output_dir / f"{slug}_combined.wav"
    _concat_wavs(merged_chunk_paths, combined_wav)

    step("Converting to MP3 …")
    _wav_to_mp3(combined_wav, mp3_path)

    # Clean up intermediate files (both speech chunks and any silence inserts)
    combined_wav.unlink(missing_ok=True)
    for p in merged_chunk_paths:
        p.unlink(missing_ok=True)

    info(f"Audio saved: {mp3_path}")
    if return_durations:
        return mp3_path, durations
    return mp3_path
