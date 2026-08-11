"""TTS engine abstraction: Kokoro primary, Coqui fallback.

Each engine exposes:
    synthesise(chunks, voice, output_dir, slug) -> list[Path]

Returns a list of per-chunk WAV paths in order.
"""
from __future__ import annotations

import os
import random
import shutil
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


# espeak-ng stores its data path in a fixed 160-byte buffer (`N_PATH_HOME`).
# A longer path is silently truncated by snprintf, fails the directory check
# that follows, and the lookup falls all the way through to the compiled-in
# build default — a GitHub Actions runner path baked into the wheel. The user
# sees a missing-file error naming a directory that never existed on their
# machine, with nothing pointing at path length as the cause.
#
# Measured on espeakng-loader 0.2.4: a 159-character data path initialises
# fine, 160 fails. See docs and the upstream reports for detail.
_ESPEAK_PATH_LIMIT = 160


def _espeak_data_path_is_too_long() -> tuple[bool, str]:
    """Whether the packaged espeak data path exceeds what espeak-ng can hold."""
    try:
        import espeakng_loader
    except ImportError:
        return False, ""
    path = str(espeakng_loader.get_data_path())
    return len(path) >= _ESPEAK_PATH_LIMIT, path


def _shim_espeak_data_path() -> None:
    """Copy espeak's data somewhere short enough for it to actually load.

    Only runs when the packaged path is over the limit, so the common case
    pays nothing. The copy lives under the user's cache directory, costs
    ~12MB, and is reused on later runs.

    **A copy, not a symlink.** phonemizer resolves the path it is given, so a
    symlink expands straight back to the long original and the truncation
    happens anyway.
    """
    too_long, original = _espeak_data_path_is_too_long()
    if not too_long:
        return

    cache_root = Path(
        os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    ) / "narraoke" / "espeak"
    target = cache_root / "espeak-ng-data"

    if len(str(target)) >= _ESPEAK_PATH_LIMIT:
        # Nothing we can do from here; say so rather than failing obscurely.
        warn(
            f"  espeak data path is {len(original)} chars, over the "
            f"{_ESPEAK_PATH_LIMIT}-char limit espeak-ng can hold, and the "
            f"cache location is no shorter. Synthesis will likely fail with "
            f"a missing-file error naming a path that does not exist. "
            f"Move the project to a shorter path."
        )
        return

    if not (target / "phontab").is_file():
        info(f"  espeak data path is {len(original)} chars; copying to a "
             f"shorter path so espeak-ng can load it")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(original, target, dirs_exist_ok=True)

    # Must land after `import misaki.espeak`, which calls set_data_path at
    # import time — otherwise that import overwrites this with the long path.
    try:
        from phonemizer.backend.espeak.wrapper import EspeakWrapper
        EspeakWrapper.set_data_path(str(target))
    except Exception as e:  # pragma: no cover - depends on optional stack
        warn(f"  could not redirect espeak data path: {e}")


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

    # After kokoro's imports (which pull in misaki.espeak and set the long
    # path), before any pipeline is built.
    _shim_espeak_data_path()

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


def _wav_trailing_silence_seconds(wav_path: Path, thresh: float = 0.01) -> float:
    """Return the length of near-silence at the END of a WAV, in seconds.

    Kokoro appends a natural tail of quiet to every spoken chunk. When we then
    splice a fixed dwell-silence after that chunk, the *effective* gap before
    the next chunk is (natural tail + dwell), not just the dwell. The timing
    model must know the full gap so the last summary phrase's highlight ends at
    the true end of speech and the after-block phrase starts on its real audio
    — otherwise the highlight rides ahead of the narration by the tail length.

    Measured on the raw 16-bit PCM: find the last sample whose magnitude
    exceeds `thresh` (fraction of full scale); everything after it is the tail.
    """
    with wave.open(str(wav_path), "rb") as w:
        n_frames = w.getnframes()
        framerate = float(w.getframerate())
        sampwidth = w.getsampwidth()
        nchannels = w.getnchannels()
        raw = w.readframes(n_frames)
    if n_frames == 0 or sampwidth != 2:
        # Only 16-bit PCM is handled precisely; anything else -> assume no tail
        # (the dwell splice still applies; we just don't over-correct).
        return 0.0
    import numpy as np
    samples = np.frombuffer(raw, dtype="<i2")
    if nchannels > 1:
        samples = samples.reshape(-1, nchannels)
        peak = np.abs(samples).max(axis=1)
    else:
        peak = np.abs(samples)
    limit = thresh * 32768.0
    loud = np.nonzero(peak > limit)[0]
    if loud.size == 0:
        return n_frames / framerate  # whole clip is silence
    silent_frames = (len(peak) - 1) - int(loud[-1])
    return silent_frames / framerate


def _effective_silence_from_mp3(
    mp3_path: Path,
    durations: list[float],
    silence_after_chunk: list[float],
) -> list[float]:
    """Reconstruct per-chunk effective silence from an already-merged MP3.

    Used only on the --skip-tts path, where the individual chunk WAVs no
    longer exist. `durations[i]` is the cached (audio + spliced-dwell) length
    of chunk i, so the running sum gives each chunk's end offset in the
    combined audio. For chunks that had a dwell spliced, measure the quiet
    that ends at that boundary; for the rest, report 0.0 (their natural tail
    is an ordinary inter-sentence pause, not a summary hold).
    """
    from pydub import AudioSegment, silence as _silence
    audio = AudioSegment.from_file(str(mp3_path))
    thresh = audio.dBFS - 16
    eff: list[float] = []
    cursor_ms = 0.0
    for i, d in enumerate(durations):
        end_ms = cursor_ms + d * 1000.0
        dwell = float(silence_after_chunk[i] or 0.0) if i < len(silence_after_chunk) else 0.0
        if dwell > 0.01:
            # Scan a window ending at the boundary for the trailing silence
            # run. Look back a little more than the spliced dwell so the
            # natural tail is included.
            win_start = max(0.0, end_ms - (dwell * 1000.0 + 2500.0))
            window = audio[win_start:end_ms]
            runs = _silence.detect_silence(
                window, min_silence_len=400, silence_thresh=thresh
            )
            tail = 0.0
            if runs:
                s, e = runs[-1]
                # Only count it if the silence run reaches the boundary.
                if (len(window) - e) < 250:
                    tail = (e - s) / 1000.0
            eff.append(tail if tail > 0 else dwell)
        else:
            eff.append(0.0)
        cursor_ms = end_ms
    return eff


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
                # Ensure the effective-silence sidecar exists and matches the
                # cached audio. On a --skip-tts run the per-chunk WAVs are gone,
                # so measure the trailing quiet at each chunk boundary directly
                # from the combined MP3 using the cached cumulative durations.
                eff_path = output_dir / f"{slug}_effective_silence.json"
                if not eff_path.exists() and silence_after_chunk is not None:
                    eff = _effective_silence_from_mp3(
                        mp3_path, durations, silence_after_chunk
                    )
                    eff_path.write_text(_json.dumps(eff), encoding="utf-8")
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
    #
    # effective_silence[i] is the FULL quiet gap after chunk i's last spoken
    # word = the natural tail Kokoro appends to the speech PLUS the dwell we
    # splice. The timing pipeline needs this (not the spliced dwell alone) so
    # the summary phrase's highlight ends at the true end of speech; see
    # _wav_trailing_silence_seconds.
    effective_silence: list[float] = []
    if silence_after_chunk is None:
        merged_chunk_paths = list(chunk_paths)
        durations = list(audio_durations)
        effective_silence = [
            _wav_trailing_silence_seconds(p) for p in chunk_paths
        ]
    else:
        # Use the first chunk's sample params as the silence template.
        with wave.open(str(chunk_paths[0]), "rb") as w0:
            template_params = w0.getparams()
        merged_chunk_paths = []
        durations = []
        silences_inserted = 0
        for i, p in enumerate(chunk_paths):
            merged_chunk_paths.append(p)
            natural_tail = _wav_trailing_silence_seconds(p)
            dwell = float(silence_after_chunk[i] or 0.0)
            if dwell > 0.01:
                silent_path = output_dir / f"{slug}_silence_{i:04d}.wav"
                _write_silence_wav(silent_path, dwell, template_params)
                merged_chunk_paths.append(silent_path)
                silences_inserted += 1
                durations.append(audio_durations[i] + dwell)
                effective_silence.append(natural_tail + dwell)
            else:
                durations.append(audio_durations[i])
                # No dwell splice: the natural tail is a normal inter-sentence
                # pause, not a summary hold. Leave it as speech-share time
                # (0 here) so ordinary phrases aren't retimed.
                effective_silence.append(0.0)
        if silences_inserted:
            info(f"  Inserted {silences_inserted} dwell-silence segments into audio")

    import json as _json
    durations_path.write_text(_json.dumps(durations), encoding="utf-8")
    effective_silence_path = output_dir / f"{slug}_effective_silence.json"
    effective_silence_path.write_text(_json.dumps(effective_silence), encoding="utf-8")

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
