"""Background pre-generation of the next loop (Phase 6).

Lifted out of ``framework_main_async.py``. ``run_pregeneration`` re-runs the
conductor -> job -> fetch -> tile pipeline for loop N+1 while loop N plays,
stashing the result on ``loop._pregen_results`` and signaling ``loop._pregen_done``.

CRITICAL invariant (brief-01 risk #4, pinned by two regression tests): the
background path writes ONLY ``loop.stem_cache[cache_key]`` and NEVER calls
``state.cache_stem`` (the 16-entry LRU). That LRU routing is foreground-only.
Do not "unify" the two paths. ``loop.stem_cache`` is the loop's SINGLE shared
cache dict (R11) — pass the loop, never give pre-gen its own cache.

The patchable dependencies (conductor, _submit_job, _fetch_audio, _build_prompt)
are reached through the ``loop`` instance so ``patch.object(loop, ...)`` in tests
keeps working. ``wait_for_multiple_jobs`` is imported here directly (no test
patches it — same as the foreground path).
"""

from __future__ import annotations

import time
from typing import Any

from app.framework.conductor_interaction import (
    build_fallback_response,
    load_available_models,
    process_actions,
)
from app.framework.domain_audio import make_cache_key, tile_to_loop
from app.job_waiter import wait_for_multiple_jobs


async def run_pregeneration(loop: Any, for_loop_idx: int, snapshot: dict[str, Any]) -> None:
    """Pre-generate loop ``for_loop_idx`` from ``snapshot``; store on the loop."""
    print(f"[AsyncFrameworkLoop] Pre-generating loop {for_loop_idx} in background...")

    try:
        current_bpm = snapshot["current_bpm"]
        current_key = snapshot["current_key"]
        active_stems = snapshot["active_stems"]
        llm_config = snapshot["llm_config"]
        available_models = load_available_models()

        # Call LLM (conductor patched on the loop instance by tests).
        try:
            conductor_response = await loop.conductor.get_next_state_async(
                current_bpm=current_bpm,
                current_key=current_key,
                active_stems=active_stems,
                user_override=snapshot.get("user_override"),
                available_instruments=snapshot.get("available_instruments", []),
                stem_history=snapshot.get("stem_history", []),
                llm_config=llm_config,
                available_models=available_models,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[AsyncFrameworkLoop] Pre-gen LLM call failed: {e}")
            conductor_response = build_fallback_response(current_bpm, current_key, active_stems, e)

        deduped_tracks = process_actions(conductor_response.get("actions", []), active_stems)

        # Build next-stems info.
        next_stems: list[dict[str, Any]] = []
        for t in deduped_tracks:
            m_id = t.get("model_id", "foundation-1")
            prompt = loop._build_prompt(t, current_key, current_bpm)
            next_stems.append(
                {
                    "prompt": prompt,
                    "model_id": m_id,
                    "bpm": current_bpm,
                    "key": current_key,
                    "bars": t.get("bars", 8),
                    "_original_details": t,
                    "_age": t.get("_age", 0),
                }
            )

        # Submit jobs. Shares loop.stem_cache (R11); skips stems already cached.
        pending_jobs: list[tuple[Any, int, str]] = []
        for i, t in enumerate(next_stems):
            prompt = t["prompt"]
            track_bars = t["bars"]
            m_id = t.get("model_id")
            cache_key = make_cache_key(m_id, prompt, current_bpm, current_key, track_bars)

            if cache_key in loop.stem_cache:
                continue

            orig = t.get("_original_details", {})
            job_id = await loop._submit_job(
                session_id=loop.session_id,
                instrument=orig.get("sub_family", "Unknown"),
                prompt=prompt,
                major_family=orig.get("major_family"),
                model_id=m_id,
                key=current_key,
                bpm=current_bpm,
                timbre_tags=orig.get("timbre_tags", []),
                bars=track_bars,
            )
            pending_jobs.append((job_id, i, cache_key))

        # Wait for jobs + fetch audio. NOTE: writes ONLY loop.stem_cache here —
        # state.cache_stem is foreground-only (brief-01 risk #4 divergence).
        if pending_jobs:
            job_ids = [job_id for job_id, _, _ in pending_jobs]
            results = await wait_for_multiple_jobs(job_ids, timeout=120.0)

            for job_id, orig_idx, cache_key in pending_jobs:
                audio_path = results.get(job_id)
                if audio_path:
                    audio_data = await loop._fetch_audio(audio_path)
                    if audio_data is not None:
                        loop.stem_cache[cache_key] = {"audio_data": audio_data, "last_used": time.time()}

        # Tile to loop duration (Phase 2 helper; replaces the inline copy).
        prepared_tracks, loop_duration_samples = tile_to_loop(
            next_stems=next_stems,
            stem_cache=loop.stem_cache,
            bpm=current_bpm,
            key=current_key,
            sample_rate=loop.mixer.sample_rate if loop.mixer else None,
            deduped_tracks=deduped_tracks,
        )

        # Store results for the main loop to consume.
        loop._pregen_results = {
            "prepared_tracks": prepared_tracks,
            "loop_duration_samples": loop_duration_samples,
            "loop_idx": for_loop_idx,
            "next_stems": next_stems,
            "master_bpm": conductor_response.get("master_bpm", current_bpm),
            "master_key": conductor_response.get("master_key", current_key),
            "set_name": conductor_response.get("name", "Unknown Set"),
            "reasoning": conductor_response.get("reasoning", "No reasoning provided."),
            "actions": conductor_response.get("actions", []),
        }
        loop._pregen_done.set()
        print(f"[AsyncFrameworkLoop] Pre-generation for loop {for_loop_idx} complete!")

    except Exception as e:  # noqa: BLE001
        print(f"[AsyncFrameworkLoop] Pre-generation error: {e}")
        import traceback

        traceback.print_exc()
        loop._pregen_results = None
        loop._pregen_done.set()
