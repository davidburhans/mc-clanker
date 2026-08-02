"""Phase 6 regression tests: pregeneration cache invariants.

Pins the load-bearing behaviors the E1-E6 refactor must NOT regress:
- R11: pregeneration shares the loop's SINGLE stem_cache (skips already-cached stems).
- brief-01 risk #4: the background pre-gen path writes ONLY loop.stem_cache and
  NEVER calls state.cache_stem (the 16-entry LRU). That routing is foreground-only.
- non-silence: a fetched stem yields non-zero audio in the pre-gen result.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import numpy as np

from app.framework.framework_main_async import AsyncFrameworkLoop
from app.framework.framework_state import state


def _snapshot() -> dict:
    return {
        "current_bpm": 128,
        "current_key": "A minor",
        "active_stems": [],
        "user_override": "",
        "available_instruments": [],
        "stem_history": [],
        "llm_config": {"base_url": "http://x:1234/v1", "api_key": "k", "model": "m"},
    }


def _add_response() -> dict:
    return {
        "master_bpm": 128,
        "master_key": "A minor",
        "actions": [
            {
                "action_type": "add",
                "sub_family": "Synth Pad",
                "major_family": "Synth",
                "model_id": "foundation-1",
                "timbre_tags": ["warm"],
                "notation_tag": "melody",
                "fx_tag": "dry",
                "bars": 4,
            }
        ],
        "reasoning": "add a pad",
        "name": "Pad Set",
    }


def _new_loop() -> AsyncFrameworkLoop:
    """Fresh loop with state fields the pre-gen snapshot path reads."""
    state.current_bpm = 128
    state.current_key = "A minor"
    state.active_stems = []
    return AsyncFrameworkLoop(uuid4())


async def test_pregeneration_does_not_route_through_cache_stem() -> None:
    """Background pre-gen must NOT call state.cache_stem (foreground-only LRU)."""
    loop = _new_loop()
    audio = np.ones((1000, 2), dtype=np.float32)

    with (
        patch.object(loop, "conductor") as mc,
        patch.object(loop, "_submit_job", new_callable=AsyncMock),
        patch.object(loop, "_fetch_audio", new_callable=AsyncMock, return_value=audio),
        patch("app.framework.pregeneration.wait_for_multiple_jobs", new_callable=AsyncMock, return_value={}),
        patch.object(state, "cache_stem") as cache_stem_mock,
    ):
        mc.get_next_state_async = AsyncMock(return_value=_add_response())
        await loop._pre_generate_next_loop(2, _snapshot())

    assert not cache_stem_mock.called, "pre-gen must NOT route through state.cache_stem (foreground-only)"


async def test_foreground_loop_routes_through_cache_stem() -> None:
    """Complementary half of brief-01 risk #4: the FOREGROUND path MUST route
    fetched audio through ``state.cache_stem`` (the 16-entry LRU).

    The sibling test above pins that the background pre-gen path does NOT call
    it; this pins that the foreground P8 path (``_step_await_jobs_fetch``) DOES.
    Together they pin the full foreground/background divergence and close DoD
    gate-8.
    """
    loop = _new_loop()
    loop._loop_idx = 1  # normally seeded by the _run_loop driver before P8 runs
    job_id = uuid4()
    audio = np.ones((1000, 2), dtype=np.float32)
    # Stem the foreground P8 path reads ``local_next_stems[orig_idx]["prompt"]`` from.
    local_next_stems = [
        {"prompt": "Synth Pad, A minor, 128", "bars": 4, "model_id": "foundation-1"}
    ]
    pending_jobs = [(job_id, 0, "foundation-1_Synth Pad, A minor, 128_128_A minor_4")]

    with (
        patch.object(loop, "_fetch_audio", new_callable=AsyncMock, return_value=audio) as fetch_mock,
        patch(
            "app.framework.loop_orchestrator.wait_for_multiple_jobs",
            new_callable=AsyncMock,
            return_value={job_id: "audio/x.aac"},
        ),
        patch.object(state, "cache_stem") as cache_stem_mock,
    ):
        await loop._step_await_jobs_fetch(pending_jobs, local_next_stems)

    fetch_mock.assert_awaited_once_with("audio/x.aac")
    cache_stem_mock.assert_called_once_with("Synth Pad, A minor, 128", audio)


async def test_pregen_result_tracks_are_non_silent_when_audio_fetched() -> None:
    """A fetched stem must yield non-zero audio in the pre-gen result (no silent fallback)."""
    loop = _new_loop()
    audio = np.ones((1000, 2), dtype=np.float32) * 0.5
    job_id = uuid4()

    with (
        patch.object(loop, "conductor") as mc,
        patch.object(loop, "_submit_job", new_callable=AsyncMock, return_value=job_id),
        patch.object(loop, "_fetch_audio", new_callable=AsyncMock, return_value=audio),
        patch(
            "app.framework.pregeneration.wait_for_multiple_jobs",
            new_callable=AsyncMock,
            return_value={job_id: "audio/x.aac"},
        ),
        patch.object(state, "cache_stem"),
    ):
        mc.get_next_state_async = AsyncMock(return_value=_add_response())
        await loop._pre_generate_next_loop(2, _snapshot())

    assert loop._pregen_results is not None
    prepared = loop._pregen_results["prepared_tracks"]
    assert prepared, "expected at least one prepared track"
    track_audio, _stem_idx = prepared[0]
    assert np.any(track_audio != 0), "fetched stem must not fall back to silence"


async def test_pregen_skips_job_when_stem_already_cached() -> None:
    """R11: pre-gen shares the loop's stem_cache -> skips submit for cached stems."""
    loop = _new_loop()
    job_id = uuid4()

    with (
        patch.object(loop, "conductor") as mc,
        patch.object(loop, "_submit_job", new_callable=AsyncMock, return_value=job_id) as submit_a,
        patch.object(loop, "_fetch_audio", new_callable=AsyncMock, return_value=np.ones((10, 2), dtype=np.float32)),
        patch(
            "app.framework.pregeneration.wait_for_multiple_jobs",
            new_callable=AsyncMock,
            return_value={job_id: "audio/x.aac"},
        ),
        patch.object(state, "cache_stem"),
    ):
        mc.get_next_state_async = AsyncMock(return_value=_add_response())
        # First pre-gen: submits the job + caches the fetched audio in loop.stem_cache.
        await loop._pre_generate_next_loop(2, _snapshot())
        assert submit_a.called, "first pre-gen should submit the job"

        # Second pre-gen (same stem): the cache_key is now in loop.stem_cache, so
        # _submit_job must NOT be called again (shared cache -> skip).
        submit_b = AsyncMock(return_value=job_id)
        loop._submit_job = submit_b
        await loop._pre_generate_next_loop(3, _snapshot())
        assert not submit_b.called, "pre-gen must skip job submission for a stem already in the shared stem_cache"
