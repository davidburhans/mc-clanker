"""Background pre-generation of the next loop (Phase 6).

Lifted out of ``framework_main_async.py``. ``run_pregeneration`` re-runs the
conductor -> job -> fetch -> tile pipeline for loop N+1 while loop N plays,
stashing the result on ``loop._pregen_results`` and signaling ``loop._pregen_done``.

CRITICAL invariant (brief-01 risk #4, pinned by two regression tests): the
background path writes ONLY ``loop.stem_cache[cache_key]`` and NEVER calls
``state.cache_stem`` (the 16-entry LRU). That LRU routing is foreground-only.
Do not "unify" the two paths. ``loop.stem_cache`` is the loop's SINGLE shared
cache dict (R11) — pass the loop, never give pre-gen its own cache.

The patchable dependencies (conductor, _submit_job, _await_jobs, _fetch_audio,
_build_prompt) are reached through the ``loop`` instance so ``patch.object(loop,
...)`` in tests keeps working. The await path routes through ``loop._await_jobs``
(U4); tests patch the loop delegate.
"""

from __future__ import annotations

import time
from typing import Any

from app.framework.conductor_interaction import (
    build_fallback_response,
    load_available_models,
    process_actions,
)
from app.framework.domain_audio import tile_to_loop
from app.framework.loop_steps import (
    JOB_PENDING_DEPTH_LIMIT,
    JOB_WAIT_TIMEOUT_SECONDS,
    LOOP_CONDUCTOR_SKIP_AFTER_SUBMIT_FAILURES,
    _collect_uncached_stems,
    gather_stem_audio,
    read_generation_params,
    reawait_late_job_completions,
    sanitize_master_bpm,
    sanitize_master_key,
)


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
        # REL-18 (U12): same gate as the foreground P4 path — once the submit
        # streak says the DB is down, the background LLM call is skipped too
        # (retain-all fallback; the foreground probe owns recovery — no probe here).
        if loop._consecutive_submit_failures >= LOOP_CONDUCTOR_SKIP_AFTER_SUBMIT_FAILURES:
            conductor_response = build_fallback_response(
                current_bpm, current_key, active_stems, "job-queue submit outage"
            )
        else:
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

        # Submit jobs. Shares loop.stem_cache (R11); skips stems already cached
        # (the same _collect_uncached_stems scan the foreground P7 uses, so the
        # REL-06 hit-refresh cannot drift between the paths).
        # REL-12c: the same depth-gauge throttle as P7 — when the pending
        # backlog is over the bound, skip-and-log the whole phase (never block);
        # the skipped prompts stay cache-missed and retry on a later cycle.
        skipped_idxs: list[int] = []
        pending_jobs: list[tuple[Any, int, str]] = []
        uncached = _collect_uncached_stems(next_stems, current_bpm, current_key, loop.stem_cache)
        if uncached and await loop._queue_backlogged():
            skipped_idxs = [i for i, _t, _cache_key in uncached]
            print(
                f"[AsyncFrameworkLoop] Pre-gen: pending backlog over {JOB_PENDING_DEPTH_LIMIT}; "
                f"skipping {len(skipped_idxs)} submission(s) this cycle"
            )
        else:
            # REL-25b: same shared state snapshot as the foreground P7 — one
            # read per submit phase, after the throttle gate (zero lock takes
            # on the skip path).
            cfg_scale, steps = await read_generation_params()
            for i, t, cache_key in uncached:
                prompt = t["prompt"]
                track_bars = t["bars"]
                m_id = t.get("model_id")

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
                    cfg_scale=cfg_scale,
                    steps=steps,
                )
                pending_jobs.append((job_id, i, cache_key))

        # Wait for jobs + fetch audio. NOTE: writes ONLY loop.stem_cache here —
        # state.cache_stem is foreground-only (brief-01 risk #4 divergence).
        # U4: stem_outcomes mirrors the foreground P8 tri-state so both loop
        # paths capture identical applied_actions data.
        stem_outcomes: dict[int, str] = {}
        # REL-12c: a throttle-skipped stem reports "failed" — absent from the
        # map the applied-actions audit would default it to "cached" (a lie).
        for idx in skipped_idxs:
            stem_outcomes[idx] = "failed"
        if pending_jobs:
            job_ids = [job_id for job_id, _, _ in pending_jobs]
            # B3: same batch budget as the foreground path — one worker drains a
            # 4-6 stem batch sequentially, so the old flat 120 s lost the stems
            # queued behind a slow job (silence + identical re-submit forever).
            results = await loop._await_jobs(job_ids, timeout=JOB_WAIT_TIMEOUT_SECONDS)
            results = await reawait_late_job_completions(
                loop._await_jobs, job_ids, results, label=f"pregen-{for_loop_idx}"
            )

            # REL-12a: this background path must abandon its losers too, or it
            # re-leaks the immortal-pending bug the foreground path just fixed.
            missing = [job_id for job_id in job_ids if not results.get(job_id)]
            await loop._abandon_missing_jobs(missing)

            # REL-28: same bounded concurrent fetch as the foreground P8 path
            # (shared helper); the cache write below stays stem_cache-ONLY —
            # state.cache_stem is foreground-only (brief-01 risk #4 divergence).
            audio_paths = [results.get(job_id) for job_id, _, _ in pending_jobs]
            fetched = await gather_stem_audio(loop._fetch_audio, audio_paths)
            for (job_id, orig_idx, cache_key), audio_data in zip(pending_jobs, fetched):
                if audio_data is not None:
                    loop.stem_cache[cache_key] = {"audio_data": audio_data, "last_used": time.time()}
                    stem_outcomes[orig_idx] = "generated"
                else:
                    stem_outcomes[orig_idx] = "failed"

        # Tile to loop duration (Phase 2 helper; replaces the inline copy).
        prepared_tracks, loop_duration_samples = tile_to_loop(
            next_stems=next_stems,
            stem_cache=loop.stem_cache,
            bpm=current_bpm,
            key=current_key,
            sample_rate=loop.mixer.sample_rate if loop.mixer else None,
            deduped_tracks=deduped_tracks,
        )

        # Store results for the main loop to consume. B5: an explicit JSON null
        # for master_bpm/master_key must not poison state / prompts / job rows,
        # so both fields are coalesced here exactly like the foreground commit.
        pregen_master_bpm = sanitize_master_bpm(conductor_response.get("master_bpm"), current_bpm)
        pregen_master_key = sanitize_master_key(conductor_response.get("master_key"), current_key)
        loop._pregen_results = {
            "prepared_tracks": prepared_tracks,
            "loop_duration_samples": loop_duration_samples,
            "loop_idx": for_loop_idx,
            "next_stems": next_stems,
            "master_bpm": pregen_master_bpm,
            "master_key": pregen_master_key,
            "set_name": conductor_response.get("name", "Unknown Set"),
            "reasoning": conductor_response.get("reasoning", "No reasoning provided."),
            "actions": conductor_response.get("actions", []),
            # U4/DPO capture: the exact conductor chat + the enacted stems'
            # generation outcome, so the pregen path captures identically to the
            # fresh path (P2 reconstructs _request_messages into its response).
            "_request_messages": conductor_response.get("_request_messages"),
            "stem_outcomes": stem_outcomes,
        }
        loop._pregen_done.set()
        print(f"[AsyncFrameworkLoop] Pre-generation for loop {for_loop_idx} complete!")

    except Exception as e:  # noqa: BLE001
        print(f"[AsyncFrameworkLoop] Pre-generation error: {e}")
        import traceback

        traceback.print_exc()
        loop._pregen_results = None
        loop._pregen_done.set()
