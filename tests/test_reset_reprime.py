"""REL-02 + REL-06 regression suite (unit rel-reset-reprime), written TDD-first.

REL-02: after ``should_reset`` zeroes ``Mixer.current_loop_end_sample``, a
commit into the boundary-less mixer must take the ``prime_loop`` path again;
otherwise ``set_next_loop`` audio can never be consumed and the set is silent
forever while generation keeps running.

REL-06: a cache HIT must refresh ``last_used`` on both the foreground and
pre-generation paths, and cache maintenance must prune by last use plus an
entry cap — a retained stem must not be evicted (and regenerated) every 300 s.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import numpy as np
import pytest

from app.framework.domain_audio import make_cache_key
from app.framework.framework_main_async import AsyncFrameworkLoop
from app.framework.framework_mixer import Mixer
from app.framework.framework_state import state
from app.framework.loop_steps import _CommitResult
from app.framework.pregeneration import run_pregeneration

_SAMPLE_RATE = 44100


_STATE_ATTRS = (
    "current_bpm",
    "current_key",
    "active_stems",
    "previous_stems",
    "next_stems",
    "loop_count",
    "loop_history",
    "is_generating",
    "is_running",
    "should_reset",
)


def _copy_state_attr(attr: str):
    value = getattr(state, attr)
    if isinstance(value, (list, dict, set)):
        return type(value)(value)
    return value


@pytest.fixture(autouse=True)
def _isolated_framework_state():
    """Snapshot the shared state fields these drives mutate."""
    snapshot = {attr: _copy_state_attr(attr) for attr in _STATE_ATTRS}
    state.current_bpm = 128
    state.current_key = "A minor"
    state.active_stems = []
    state.previous_stems = []
    state.next_stems = []
    state.loop_count = 0
    state.loop_history = []
    state.is_generating = True
    state.is_running = True
    state.should_reset = False
    yield
    for attr, value in snapshot.items():
        setattr(state, attr, value)


def _audio(seconds: float = 1.0, channels: int = 2) -> np.ndarray:
    """Non-silent mono/stereo block shaped for the real Mixer under test."""
    return np.ones((int(seconds * _SAMPLE_RATE), channels), dtype=np.float32) * 0.25


def _loop_with(mixer) -> AsyncFrameworkLoop:
    loop = AsyncFrameworkLoop(uuid4())
    loop.mixer = mixer  # type: ignore[assignment]  # tests inject the boundary-faithful fake
    return loop


def _commit_result() -> _CommitResult:
    return _CommitResult(
        needs_pregen=False,
        needs_initial_record=False,
        rec_stems=[],
        rec_set_name="",
        rec_reasoning="",
        state_snapshot={"current_bpm": 128, "current_key": "A minor"},
    )


class _RecordingMixer:
    """Boundary-faithful handoff spy: prime restores the transition gate."""

    def __init__(self, *, current_loop_end_sample: int = 0) -> None:
        self.sample_rate = _SAMPLE_RATE
        self.current_sample = 0
        self.current_loop_end_sample = current_loop_end_sample
        self.next_loop_audio: list = []
        self.prime_loop_calls: list[dict] = []
        self.set_next_loop_calls: list[dict] = []

    def prime_loop(self, tracks, *, duration_samples: int) -> None:
        self.prime_loop_calls.append({"tracks": list(tracks), "duration_samples": duration_samples})
        self.current_loop_end_sample = self.current_sample + duration_samples
        self.next_loop_audio = []

    def set_next_loop(self, tracks, next_loop_duration_samples: int = 0, loop_idx: int = 0) -> None:
        self.set_next_loop_calls.append(
            {
                "tracks": list(tracks),
                "next_loop_duration_samples": next_loop_duration_samples,
                "loop_idx": loop_idx,
            }
        )
        self.next_loop_audio = list(tracks)


# ---------------------------------------------------------------------------
# REL-02 — a reset mixer must be re-primed, not handed dead staged audio
# ---------------------------------------------------------------------------


async def test_reset_then_restart_reprimes_boundary_and_transition_fires():
    """Soak #3 in miniature: loops, reset, restart — the gate reopens and plays.

    Real-mixer end-to-end: loop 1 primes, loop 2 transitions at the boundary,
    ``should_reset`` clears that boundary, and the next commit re-establishes it
    so a later queued loop transitions again instead of remaining staged forever.
    """
    mixer = Mixer(channels=1)
    loop = _loop_with(mixer)
    frames = mixer.blocksize
    first_boundary = _SAMPLE_RATE
    second_boundary = 2 * _SAMPLE_RATE

    # Loop 1 handoff (P10 prime path), then queue + consume loop 2.
    mixer.prime_loop([(_audio(channels=1), 0)], duration_samples=first_boundary)
    mixer.set_next_loop([(_audio(channels=1), 0)], next_loop_duration_samples=second_boundary, loop_idx=2)
    mixer.current_sample = first_boundary - frames
    broadcast: list[bytes] = []
    outdata = np.zeros((frames, 1), dtype=np.float32)
    with patch.object(state, "broadcast_audio", side_effect=broadcast.append):
        mixer._callback(outdata, frames, None, None)
    assert mixer.pop_transition_event() == 2, "precondition: the set was playing before reset"

    # The user-visible reset consumes the flag and zeroes the transition gate.
    state.should_reset = True
    await loop._step_read_state()
    assert state.should_reset is False
    assert mixer.current_loop_end_sample == 0, "reset precondition: the mixer is boundary-less"

    # Post-reset restart: this commit must prime now, not stage a dead loop.
    loop._loop_idx = 3
    await loop._step_commit_to_mixer(False, [(_audio(channels=1), 0)], first_boundary)
    assert mixer.current_loop_end_sample > 0, "REL-02: a boundary-less commit must re-prime the gate"
    assert mixer.next_loop_audio == [], "a primed loop has no dead set_next_loop residue"

    # Music resumed: a later queued loop is consumed at the restored boundary.
    mixer.set_next_loop([(_audio(channels=1), 0)], next_loop_duration_samples=second_boundary, loop_idx=4)
    mixer.current_sample = mixer.current_loop_end_sample - frames
    with patch.object(state, "broadcast_audio", side_effect=broadcast.append):
        mixer._callback(outdata, frames, None, None)
    assert mixer.pop_transition_event() == 4, "REL-02: music must transition again after reset+restart"


async def test_commit_into_boundaryless_mixer_takes_prime_path():
    """A zero boundary means prime_loop, regardless of the stale loop counter."""
    mixer = _RecordingMixer()
    loop = _loop_with(mixer)
    loop._loop_idx = 5
    tracks = [(_audio(0.1), 0)]

    await loop._step_commit_to_mixer(False, tracks, _SAMPLE_RATE)

    assert len(mixer.prime_loop_calls) == 1, "REL-02: boundary 0 must take the prime path"
    assert mixer.set_next_loop_calls == [], "staging into a closed gate is permanent silence"
    assert mixer.prime_loop_calls[0]["tracks"] == tracks
    assert mixer.current_loop_end_sample > 0, "prime_loop must reopen the transition gate"
    assert loop._loop_idx == 1, "the forced value gives later phases loop-1 semantics"
    assert loop._staged_loop_idx == 0, "nothing is staged when the loop was primed directly"


async def test_commit_with_live_boundary_keeps_set_next_loop_path():
    """No over-forcing: a live boundary still uses the normal crossfade handoff."""
    mixer = _RecordingMixer(current_loop_end_sample=_SAMPLE_RATE)
    loop = _loop_with(mixer)
    loop._loop_idx = 5
    tracks = [(_audio(0.1), 0)]

    await loop._step_commit_to_mixer(False, tracks, _SAMPLE_RATE)

    assert mixer.prime_loop_calls == [], "a live boundary must not restart the set"
    assert len(mixer.set_next_loop_calls) == 1
    assert mixer.set_next_loop_calls[0]["loop_idx"] == 5
    assert mixer.next_loop_audio == tracks
    assert loop._staged_loop_idx == 5


async def test_reset_iteration_with_accepted_pregen_still_primes():
    """The force happens before the loop-1/else branch, so pregen output is used."""
    mixer = _RecordingMixer()
    loop = _loop_with(mixer)
    loop._loop_idx = 4
    prepared_tracks = [(_audio(0.1), 0)]
    loop._pregen_results = {
        "loop_idx": 4,
        "prepared_tracks": prepared_tracks,
        "loop_duration_samples": _SAMPLE_RATE,
        "next_stems": [],
    }

    tracks_to_use, duration = await loop._step_commit_to_mixer(True, [], 0)

    assert mixer.set_next_loop_calls == [], "accepted pregen audio must not be staged dead"
    assert len(mixer.prime_loop_calls) == 1
    assert mixer.prime_loop_calls[0]["tracks"] == prepared_tracks
    assert tracks_to_use == prepared_tracks
    assert duration == _SAMPLE_RATE
    assert loop._staged_loop_idx == 0


# ---------------------------------------------------------------------------
# REL-06 — cache HIT refreshes the TTL clock; pruning is age + capped
# ---------------------------------------------------------------------------


async def test_foreground_cache_hit_refreshes_last_used():
    """P7 hit: keep the entry and restart its TTL; do not submit the job again."""
    loop = _loop_with(_RecordingMixer())
    prompt = "Synth Pad, A minor, 128"
    cache_key = make_cache_key("foundation-1", prompt, 128, "A minor", 4)
    stale_used = time.time() - 400
    loop.stem_cache[cache_key] = {"audio_data": _audio(0.1), "last_used": stale_used}
    stem = {"prompt": prompt, "bars": 4, "model_id": "foundation-1", "_original_details": {}}
    loop._submit_job = AsyncMock(return_value=uuid4())

    await loop._step_submit_jobs([stem], 128, "A minor")

    loop._submit_job.assert_not_awaited()
    assert loop.stem_cache[cache_key]["last_used"] > stale_used, (
        "REL-06: a foreground cache HIT must refresh last_used (TTL measures active use)"
    )


def _pregen_fixture():
    loop = _loop_with(_RecordingMixer())
    response = {
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
    snapshot = {
        "current_bpm": 128,
        "current_key": "A minor",
        "active_stems": [],
        "user_override": "",
        "available_instruments": [],
        "stem_history": [],
        "llm_config": {"base_url": "http://x:1234/v1", "api_key": "k", "model": "m"},
    }
    return loop, response, snapshot


async def _run_cached_pregen(loop, response, snapshot):
    loop.conductor.get_next_state_async = AsyncMock(return_value=response)  # type: ignore[method-obj]
    loop._submit_job = AsyncMock(return_value=uuid4())
    # The wait delegate resolves exactly the job ids this run's fake submit produced.
    loop._await_jobs = AsyncMock(side_effect=lambda job_ids, timeout=0: {job_id: "audio/x.aac" for job_id in job_ids})
    loop._fetch_audio = AsyncMock(return_value=_audio(0.1))
    await run_pregeneration(loop, 2, snapshot)


async def test_pregen_cache_hit_refreshes_last_used():
    """Background pregen hit: same refresh contract, still no LRU routing."""
    loop, response, snapshot = _pregen_fixture()
    prompt = loop._build_prompt(response["actions"][0], "A minor", 128)
    cache_key = make_cache_key("foundation-1", prompt, 128, "A minor", 4)

    with patch.object(state, "cache_stem") as cache_stem:
        await _run_cached_pregen(loop, response, snapshot)
        assert cache_key in loop.stem_cache, "precondition: the first run populated loop.stem_cache"
        stale_used = time.time() - 400
        loop.stem_cache[cache_key]["last_used"] = stale_used

        second_submit = AsyncMock(return_value=uuid4())
        loop._submit_job = second_submit
        await run_pregeneration(loop, 3, snapshot)

    second_submit.assert_not_awaited()
    cache_stem.assert_not_called()
    assert loop.stem_cache[cache_key]["last_used"] > stale_used, (
        "REL-06: the pregen HIT path must refresh last_used in loop.stem_cache only"
    )


async def test_pregen_hit_refresh_keeps_submit_skipped():
    """Refreshing the pregen hit must not turn it back into duplicate work."""
    loop, response, snapshot = _pregen_fixture()
    with patch.object(state, "cache_stem"):
        await _run_cached_pregen(loop, response, snapshot)

        third_submit = AsyncMock(return_value=uuid4())
        loop._submit_job = third_submit
        await run_pregeneration(loop, 4, snapshot)

    third_submit.assert_not_awaited()


async def test_retained_stem_survives_ttl_prune_after_hit():
    """Audit acceptance: TTL pruning uses last use, so the retained groove stays."""
    loop = _loop_with(_RecordingMixer())
    loop._loop_idx = 2
    prompt = "Retained Drums, A minor, 128"
    retained_key = make_cache_key("foundation-1", prompt, 128, "A minor", 4)
    loop.stem_cache[retained_key] = {"audio_data": _audio(0.1), "last_used": time.time() - 400}
    stem = {"prompt": prompt, "bars": 4, "model_id": "foundation-1", "_original_details": {}}
    loop._submit_job = AsyncMock(return_value=uuid4())
    await loop._step_submit_jobs([stem], 128, "A minor")

    abandoned_key = "foundation-1_Abandoned Pad, A minor, 128_128_A minor_4"
    loop.stem_cache[abandoned_key] = {"audio_data": _audio(0.1), "last_used": time.time() - 400}

    await loop._step_post_commit(_commit_result(), [], 0)

    assert retained_key in loop.stem_cache, "REL-06: the just-used retained stem must survive the 300 s prune"
    assert abandoned_key not in loop.stem_cache, "the genuinely stale sibling must still be evicted"


async def test_stem_cache_ttl_window_keeps_recent_and_evicts_stale():
    """REL-06: the 300 s boundary is TTL-from-last-use, expressed by the named seam."""
    from app.framework.loop_steps import STEM_CACHE_TTL_SECONDS

    assert STEM_CACHE_TTL_SECONDS == pytest.approx(300.0)
    loop = _loop_with(_RecordingMixer())
    loop._loop_idx = 2
    now = time.time()
    fresh_key = "foundation-1_Used 299 s Ago_128_A minor_4"
    stale_key = "foundation-1_Used 301 s Ago_128_A minor_4"
    loop.stem_cache[fresh_key] = {"audio_data": _audio(0.1), "last_used": now - (STEM_CACHE_TTL_SECONDS - 1)}
    loop.stem_cache[stale_key] = {"audio_data": _audio(0.1), "last_used": now - (STEM_CACHE_TTL_SECONDS + 1)}

    await loop._step_post_commit(_commit_result(), [], 0)

    assert fresh_key in loop.stem_cache
    assert stale_key not in loop.stem_cache


async def test_stem_cache_entry_cap_evicts_oldest():
    """REL-06 cap: fresh entries cannot grow without bound before the TTL."""
    from app.framework.loop_steps import STEM_CACHE_MAX_ENTRIES

    loop = _loop_with(_RecordingMixer())
    loop._loop_idx = 2
    now = time.time() - 100
    keys = [f"foundation-1_Cache Stem {index}_128_A minor_4" for index in range(STEM_CACHE_MAX_ENTRIES + 3)]
    for index, cache_key in enumerate(keys):
        loop.stem_cache[cache_key] = {"audio_data": _audio(0.1), "last_used": now + index}

    await loop._step_post_commit(_commit_result(), [], 0)

    assert len(loop.stem_cache) == STEM_CACHE_MAX_ENTRIES
    assert all(cache_key not in loop.stem_cache for cache_key in keys[:3]), "oldest entries must be evicted first"
    assert all(cache_key in loop.stem_cache for cache_key in keys[3:]), "newest entries must be retained"
