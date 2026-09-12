"""U14 REL-28 contracts: bounded-concurrency per-stem audio fetch in the loop paths.

Spec: refactor/plans/rel-remediation-plan.md §U14 (REL-28) · Plan:
refactor/plans/units/rel-26-plan.md §3 (T10-T14).

Cases
-----
T10  foreground P8: 6 stem fetches must overlap (bounded, default 4) — serial
     fetch peaks at 1 in-flight and takes ~N×delay; TDD red on both.
T11  results map to their own stems: keyed lookup survives the gather — one
     short/None job must never shift another stem's audio; foreground-only
     ``state.cache_stem`` routing preserved. (Mapping preservation pin: the
     serial code already satisfies it; the fix must not regress it.)
T12  the concurrency bound is the monkeypatchable module constant
     ``loop_steps.STEM_FETCH_CONCURRENCY`` (JOB_PENDING_DEPTH_LIMIT precedent).
T13  the pregen mirror routes its fetch phase through the SAME shared helper
     (``gather_stem_audio``) while keeping the ``state.cache_stem`` divergence.
T14  existing divergence/framework pins stay green (run
     test_pregeneration_divergence.py + test_async_framework.py unchanged).
"""

from __future__ import annotations

import asyncio
import sys
import time
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import numpy as np

import app.framework.loop_steps as loop_steps
from app.framework.framework_main_async import AsyncFrameworkLoop
from app.framework.framework_state import state

# ---------------------------------------------------------------------------
# Named fakes
# ---------------------------------------------------------------------------


class FakeSlowFetch:
    """Named fake audio fetcher: fixed delay, tracks peak in-flight fetches."""

    def __init__(self, delay: float = 0.15):
        self.delay = delay
        self._in_flight = 0
        self.max_in_flight = 0
        self.fetched: list[str] = []

    async def __call__(self, path: str):
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            await asyncio.sleep(self.delay)
            self.fetched.append(path)
            return np.full((2, 2), 1.0, dtype=np.float32)
        finally:
            self._in_flight -= 1


class FakeVariedFetch:
    """Named fake audio fetcher: per-path scripted results (array or None)."""

    def __init__(self, script: dict[str, np.ndarray | None]):
        self.script = dict(script)
        self.calls: list[str] = []

    async def __call__(self, path: str):
        self.calls.append(path)
        return self.script.get(path)


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def _fresh_loop(job_ids: list) -> tuple[AsyncFrameworkLoop, dict]:
    """Loop with ``_await_jobs`` patched to report every job at audio/{i}.aac."""
    loop = AsyncFrameworkLoop(uuid4())
    loop._loop_idx = 1  # normally seeded by the _run_loop driver before P8 runs
    results = {job_id: f"audio/{i}.aac" for i, job_id in enumerate(job_ids)}
    loop._await_jobs = AsyncMock(return_value=results)
    return loop, results


def _pending_and_stems(job_ids: list) -> tuple[list, list]:
    pending_jobs = [(job_ids[i], i, f"cache-key-{i}") for i in range(len(job_ids))]
    local_next_stems = [{"prompt": f"rel28-prompt-{uuid4()}"} for _ in job_ids]
    return pending_jobs, local_next_stems


# ---------------------------------------------------------------------------
# T10 — acceptance: N stems complete in ~1 batch, not N serial batches
# ---------------------------------------------------------------------------


async def test_step_await_jobs_fetch_runs_concurrently():
    job_ids = [uuid4() for _ in range(6)]
    loop, _results = _fresh_loop(job_ids)
    fetch = FakeSlowFetch(delay=0.15)
    loop._fetch_audio = fetch
    pending_jobs, local_next_stems = _pending_and_stems(job_ids)

    started = time.monotonic()
    outcomes = await loop._step_await_jobs_fetch(pending_jobs, local_next_stems)
    elapsed = time.monotonic() - started

    assert outcomes == {i: "generated" for i in range(6)}
    assert fetch.max_in_flight >= 2, (
        f"REL-28: fetches must overlap; peak in-flight was {fetch.max_in_flight} "
        "(serial fetch peaks at 1)"
    )
    assert fetch.max_in_flight <= 4, "REL-28: fetch concurrency must stay bounded (default 4)"
    assert elapsed < 6 * 0.15, (
        f"REL-28: 6 serial fetches at 0.15s would take >= {6 * 0.15:.2f}s; took {elapsed:.2f}s"
    )


# ---------------------------------------------------------------------------
# T11 — acceptance: results map to their own stems (order + routing preserved)
# ---------------------------------------------------------------------------


async def test_fetch_results_map_to_their_own_stems():
    job_ids = [uuid4() for _ in range(4)]
    arrays = {i: np.full((4, 2), fill_value=float(i + 1), dtype=np.float32) for i in range(4)}
    script = {
        "audio/0.aac": arrays[0],
        # job 1: absent from results entirely (never completed -> no fetch call)
        "audio/2.aac": None,  # job 2: completed but the fetch yielded nothing
        "audio/3.aac": arrays[3],
    }
    loop = AsyncFrameworkLoop(uuid4())
    loop._loop_idx = 1
    results = {job_ids[0]: "audio/0.aac", job_ids[2]: "audio/2.aac", job_ids[3]: "audio/3.aac"}
    loop._await_jobs = AsyncMock(return_value=results)
    fetch = FakeVariedFetch(script)
    loop._fetch_audio = fetch
    pending_jobs = [(job_ids[i], i, f"cache-key-{i}") for i in range(4)]
    local_next_stems = [{"prompt": f"rel28-map-prompt-{i}"} for i in range(4)]

    outcomes = await loop._step_await_jobs_fetch(pending_jobs, local_next_stems)

    assert outcomes == {0: "generated", 1: "failed", 2: "failed", 3: "generated"}
    assert loop.stem_cache["cache-key-0"]["audio_data"] is arrays[0], (
        "stem 0 must hold the audio fetched for ITS path (no shift under the gather)"
    )
    assert loop.stem_cache["cache-key-3"]["audio_data"] is arrays[3]
    assert "cache-key-1" not in loop.stem_cache and "cache-key-2" not in loop.stem_cache
    assert set(fetch.calls) == {"audio/0.aac", "audio/2.aac", "audio/3.aac"}
    # Foreground-only LRU routing: exactly the generated stems' prompts, no shifts.
    lru = state.last_generated_stems
    assert "rel28-map-prompt-0" in lru and "rel28-map-prompt-3" in lru
    assert "rel28-map-prompt-1" not in lru and "rel28-map-prompt-2" not in lru
    assert lru["rel28-map-prompt-0"] is arrays[0]


# ---------------------------------------------------------------------------
# T12 — the bound is the monkeypatchable module constant
# ---------------------------------------------------------------------------


async def test_fetch_concurrency_respects_semaphore(monkeypatch):
    monkeypatch.setattr(loop_steps, "STEM_FETCH_CONCURRENCY", 2)
    job_ids = [uuid4() for _ in range(6)]
    loop, _results = _fresh_loop(job_ids)
    fetch = FakeSlowFetch(delay=0.05)
    loop._fetch_audio = fetch
    pending_jobs, local_next_stems = _pending_and_stems(job_ids)

    outcomes = await loop._step_await_jobs_fetch(pending_jobs, local_next_stems)

    assert outcomes == {i: "generated" for i in range(6)}
    assert fetch.max_in_flight <= 2, (
        f"REL-28: STEM_FETCH_CONCURRENCY=2 must bound in-flight fetches; "
        f"peak was {fetch.max_in_flight}"
    )


# ---------------------------------------------------------------------------
# T13 — pregen mirror uses the same shared bounded gather (divergence kept)
# ---------------------------------------------------------------------------


def _pregen_snapshot() -> dict:
    return {
        "current_bpm": 128,
        "current_key": "A minor",
        "active_stems": [],
        "user_override": "",
        "available_instruments": [],
        "stem_history": [],
        "llm_config": {"base_url": "http://x:1234/v1", "api_key": "k", "model": "m"},
    }


def _pregen_add_response() -> dict:
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


async def test_pregen_path_uses_the_same_bounded_gather(monkeypatch):
    """Pregen's fetch phase must route through the shared gather helper — and still
    NEVER call state.cache_stem (brief-01 risk #4 divergence preserved)."""
    pregen_module = sys.modules["app.framework.pregeneration"]
    real_gather = getattr(loop_steps, "gather_stem_audio", None)
    recorded: dict = {}

    async def recording_gather(fetch, paths, concurrency=None):
        recorded["paths"] = list(paths)
        if real_gather is None:
            raise AssertionError("REL-28 contract missing: loop_steps.gather_stem_audio")
        return await real_gather(fetch, paths, concurrency)

    # Green wiring binds the helper into pregeneration's namespace (plan §2.4);
    # fall back to the loop_steps module attribute so the red failure names the
    # missing contract directly.
    patch_target = pregen_module if hasattr(pregen_module, "gather_stem_audio") else loop_steps
    monkeypatch.setattr(patch_target, "gather_stem_audio", recording_gather)

    loop = AsyncFrameworkLoop(uuid4())
    audio = np.full((1000, 2), 0.5, dtype=np.float32)
    job_id = uuid4()
    cache_stem_mock = MagicMock()
    monkeypatch.setattr(state, "cache_stem", cache_stem_mock)

    conductor = MagicMock()
    conductor.get_next_state_async = AsyncMock(return_value=_pregen_add_response())
    monkeypatch.setattr(loop, "conductor", conductor)
    monkeypatch.setattr(loop, "_submit_job", AsyncMock(return_value=job_id))
    monkeypatch.setattr(loop, "_fetch_audio", AsyncMock(return_value=audio))
    monkeypatch.setattr(loop, "_await_jobs", AsyncMock(return_value={job_id: "audio/x.aac"}))

    await loop._pre_generate_next_loop(2, _pregen_snapshot())

    assert recorded.get("paths") == ["audio/x.aac"], (
        "REL-28: pregen fetch phase must route through the shared gather helper"
    )
    assert not cache_stem_mock.called, "divergence: pregen must NOT route through state.cache_stem"
    assert loop._pregen_results is not None
    assert loop._pregen_results["stem_outcomes"] == {0: "generated"}
    assert loop.stem_cache and any(
        entry["audio_data"] is audio for entry in loop.stem_cache.values()
    )
