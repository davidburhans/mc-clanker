"""PURE audio math for the framework loop (no I/O).

Lifted out of ``framework_main_async.py`` (Phase 2 of the E1–E6 refactor) so the
loop depends on a small pure module instead of inlining numpy/tile logic inside
the 512-LOC ``_run_loop``. Nothing here touches the network, disk, or shared
``state`` — every input is a parameter, every output is a return value.

Frozen public surface (re-exported by ``framework_main_async``):
- ``calc_duration``   — loop length in seconds with a non-positive-BPM guard (B10)
- ``to_two_channel``  — coerce mono/1-D audio to 2-D (samples, 2) (B11)
- ``_to_two_channel`` — alias kept for frozen-binding / import compatibility
- ``make_cache_key``  — single source of truth for the stem-audio cache key (R4)
- ``tile_to_loop``    — tile cached/decoded stem audio out to the loop duration (P9)
"""

from __future__ import annotations

import numpy as np

# Fallback tempo when BPM is missing/non-positive (avoids ZeroDivisionError — review B10).
DEFAULT_FALLBACK_BPM = 120


def calc_duration(bpm: int, bars: int, time_signature: int = 4) -> float:
    """Calculate loop duration in seconds; guard non-positive BPM (B10).

    >>> calc_duration(120, 4)
    8.0
    """
    beats = bars * time_signature
    safe_bpm = bpm if isinstance(bpm, (int, float)) and bpm > 0 else DEFAULT_FALLBACK_BPM
    return beats / (safe_bpm / 60.0)


def to_two_channel(audio) -> np.ndarray:
    """Coerce mono/1-D audio to a 2-D (samples, 2) array (review B11).

    np.atleast_2d would transpose a 1-D array to (1, N); audio needs (N, channels).
    """
    arr = np.asarray(audio)
    if arr.ndim == 1:
        arr = np.column_stack([arr, arr])
    return arr


# Frozen-binding alias: callers/tests import ``_to_two_channel`` from the shim.
_to_two_channel = to_two_channel


def make_cache_key(model_id: object, prompt: str, bpm: int, key: str, bars: int) -> str:
    """Build the stable cache key for a stem's generated audio (R4 divergence guard).

    Single source of truth shared by the foreground path
    (``_step_submit_jobs`` / ``tile_to_loop``) and the background pre-generation
    path (``run_pregeneration``). Centralizing the format means a change here is
    the ONLY way the two paths can agree on a key — preventing the silent
    foreground/background cache drift that would otherwise re-submit a job the
    other path already cached (duplicate stems / wasted GPU) while the
    divergence tests stay green.

    >>> make_cache_key("foundation-1", "Synth, A minor, 128", 128, "A minor", 4)
    'foundation-1_Synth, A minor, 128_128_A minor_4'
    """
    return f"{model_id}_{prompt}_{bpm}_{key}_{bars}"


def tile_to_loop(
    *,
    next_stems: list[dict],
    stem_cache: dict,
    bpm: int,
    key: str,
    sample_rate: int | None,
    deduped_tracks: list[dict],
) -> tuple[list[tuple[np.ndarray, int]], int]:
    """Tile cached/decoded stem audio out to the loop duration (P9 step).

    Mirrors the inline P9 block formerly in ``AsyncFrameworkLoop._run_loop``:
    build ``loop_duration_samples`` from the master BPM + mixer sample rate,
    pull each stem's cached audio, coerce it to 2-D and tile it up to the loop
    length, then fill any missing/silent stem with zeros.

    Args:
        next_stems: ordered stem dicts (``prompt``/``bars``/``model_id``).
        stem_cache: ``cache_key -> {"audio_data", "last_used"}`` (read-only here).
        bpm: master tempo used both for the duration calc and the cache key.
        key: master musical key (part of the cache key).
        sample_rate: mixer sample rate; ``None`` falls back to 44100.
        deduped_tracks: dedup'd track list used only to pick ``loop_bars``.

    Returns:
        ``(prepared_tracks, loop_duration_samples)`` where each prepared track
        is a ``(audio_data, stem_idx)`` tuple with non-``None`` audio.
    """
    loop_bars = max([t.get("bars", 8) for t in deduped_tracks] + [8])
    duration_seconds = calc_duration(bpm, loop_bars)
    loop_duration_samples = int(duration_seconds * (sample_rate or 44100))

    tracks_data: list = [None] * len(next_stems)

    for i, t in enumerate(next_stems):
        prompt = t["prompt"]
        track_bars = t["bars"]
        m_id = t.get("model_id")
        cache_key = make_cache_key(m_id, prompt, bpm, key, track_bars)

        if cache_key in stem_cache:
            audio_data = stem_cache[cache_key]["audio_data"]
        else:
            audio_data = None

        if audio_data is not None:
            # B11: ensure 2-D before tiling (mono 1-D would raise).
            audio_data = _to_two_channel(audio_data)
            # Tile to loop duration
            if len(audio_data) < loop_duration_samples:
                repeats = (loop_duration_samples // len(audio_data)) + 1
                audio_data = np.tile(audio_data, (repeats, 1))[:loop_duration_samples, :]
            tracks_data[i] = audio_data

    # Step 9: assemble mixer tracks, filling silence for any missing stem.
    prepared_tracks: list[tuple[np.ndarray, int]] = []
    for audio_data, stem_idx in zip(tracks_data, range(len(tracks_data))):
        if audio_data is None:
            # B11: a silent stem usually means fetch/decode failed.
            print(f"[AsyncFrameworkLoop] Stem {stem_idx} fell back to silence")
            audio_data = np.zeros((loop_duration_samples, 2), dtype=np.float32)
        prepared_tracks.append((audio_data, stem_idx))

    return prepared_tracks, loop_duration_samples
