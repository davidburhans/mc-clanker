"""Round-3 lane B (fix_b_loopcore) regression tests.

Pins the six adversarial-review fixes landed in ``app/framework/loop_steps.py``,
``app/framework/loop_orchestrator.py`` and ``app/framework/pregeneration.py``:

  B1  stale/fabricated pre-gen result -> zero-yield replay iteration (starvation)
  B2  P13 broke before the mixer consumed the staged loop (loop silently dropped)
  B3  flat 120 s batch job wait lost late completions (silence + re-submit churn)
  B4  DJ tempo/key override discarded on every pre-generated loop
  B5  explicit JSON null for master_bpm/master_key poisoned state, prompts, jobs
  B6  ``await loop.start()`` outside the try stranded ``state.is_running``

Every test here FAILS against the pre-fix code and PASSES with it.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from app.framework import loop_orchestrator
from app.framework.framework_main_async import AsyncFrameworkLoop
from app.framework.framework_state import state
from app.framework.loop_steps import (
    JOB_LATE_COMPLETION_GRACE_SECONDS,
    JOB_WAIT_TIMEOUT_SECONDS,
    _CommitResult,
    _StepResult,
    sanitize_master_bpm,
    sanitize_master_key,
)

# --------------------------------------------------------------------------- #
# Fixtures + named fakes (no inline stubs, no real mixer / DB / network)
# --------------------------------------------------------------------------- #

_STATE_ATTRS = (
    "current_bpm",
    "current_key",
    "previous_stems",
    "active_stems",
    "next_stems",
    "stem_history",
    "current_set_name",
    "llm_reasoning",
    "last_actions",
    "loop_count",
    "stem_volumes",
    "muted_stems",
    "soloed_stems",
    "target_bpm_override",
    "target_key_override",
    "is_generating",
    "is_running",
    "should_reset",
    "user_override",
    "loop_history",
)


def _copy_state_attr(attr: str):
    value = getattr(state, attr)
    if isinstance(value, (list, dict, set)):
        return type(value)(value)
    return value


@pytest.fixture(autouse=True)
def _isolated_state():
    """Snapshot/restore the state singleton so loop drives cannot leak."""
    state.shutdown_event.clear()
    snapshot = {attr: _copy_state_attr(attr) for attr in _STATE_ATTRS}
    state.is_generating = True
    state.is_running = True
    state.active_stems = []
    state.next_stems = []
    state.previous_stems = []
    state.stem_history = []
    state.loop_count = 0
    state.current_bpm = 128
    state.current_key = "A minor"
    state.target_bpm_override = None
    state.target_key_override = None
    state.should_reset = False
    yield
    for attr, value in snapshot.items():
        setattr(state, attr, value)
    state.shutdown_event.clear()


@pytest.fixture
def sleep_log(monkeypatch):
    """Record every ``asyncio.sleep`` and collapse it (fast, deterministic waits)."""
    calls: list[float | None] = []

    async def _recording_sleep(delay=None):
        calls.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _recording_sleep)
    return calls


class FakeMixer:
    """Mixer stand-in with a playhead that only moves when told to."""

    def __init__(self, *, sample_rate=44100, headroom_seconds=0.0):
        self.sample_rate = sample_rate
        self.headroom_seconds = headroom_seconds
        self.next_loop_audio: list = []
        self.set_next_loop_calls: list[dict] = []
        self.prime_loop_calls: list[dict] = []
        self.staged_loop_idx = 0
        self.transition_events: list[int] = []
        self.transition_fired_for: int | None = None
        self.consumed_tracks = None
        self.current_sample = 0
        self.current_loop_end_sample = 0
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def prime_loop(self, tracks, *, duration_samples):
        self.prime_loop_calls.append({"tracks": list(tracks), "duration_samples": duration_samples})
        # REL-02: mirror the real boundary write so a boundary-less commit can re-prime.
        self.current_loop_end_sample = self.current_sample + duration_samples

    def set_next_loop(self, tracks, next_loop_duration_samples=0, loop_idx=0):
        self.set_next_loop_calls.append(
            {
                "tracks": list(tracks),
                "next_loop_duration_samples": next_loop_duration_samples,
                "loop_idx": loop_idx,
            }
        )
        self.next_loop_audio = list(tracks)
        self.staged_loop_idx = loop_idx

    def pop_transition_event(self):
        return self.transition_events.pop(0) if self.transition_events else None

    def loop_position_seconds(self):
        return self.headroom_seconds

    def clear(self):
        # REL-02: mirror Mixer.clear — the musical reset closes the transition gate.
        self.current_loop_end_sample = 0


class AdvancingMixer(FakeMixer):
    """Fake mixer whose playhead advances on every read and which consumes
    ``next_loop_audio`` at the boundary (1 s lookahead), exactly like the real
    ``Mixer._callback`` loop-transition logic."""

    TICK_SECONDS = 0.5
    LOOKAHEAD_SECONDS = 1.0

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._pending_event: int | None = None

    def loop_position_seconds(self):
        self.headroom_seconds -= self.TICK_SECONDS
        if self.next_loop_audio and self.headroom_seconds <= self.LOOKAHEAD_SECONDS:
            self.transition_fired_for = self.staged_loop_idx
            self.consumed_tracks = self.next_loop_audio
            self.next_loop_audio = []
            self._pending_event = self.staged_loop_idx
            self.headroom_seconds = self.LOOKAHEAD_SECONDS + 8.0  # new loop boundary
        return self.headroom_seconds

    def pop_transition_event(self):
        event, self._pending_event = self._pending_event, None
        return event


class StopAfterNMixer(FakeMixer):
    """Fake mixer that ends a ``_run_loop`` drive after N handoffs."""

    def __init__(self, *, stop_after=4):
        super().__init__(headroom_seconds=0.0)
        self.stop_after = stop_after
        self.handoff_calls = 0
        self._loop = None

    def _maybe_stop(self):
        self.handoff_calls += 1
        if self.handoff_calls >= self.stop_after and self._loop is not None:
            self._loop.running = False
            state.is_running = False

    def prime_loop(self, tracks, *, duration_samples):
        self._maybe_stop()
        super().prime_loop(tracks, duration_samples=duration_samples)

    def set_next_loop(self, tracks, next_loop_duration_samples=0, loop_idx=0):
        self._maybe_stop()
        super().set_next_loop(tracks, next_loop_duration_samples, loop_idx)


def _make_loop(mixer: FakeMixer) -> AsyncFrameworkLoop:
    """A loop wired for an in-memory drive: fake mixer + fake conductor."""
    loop = AsyncFrameworkLoop(uuid.uuid4())
    loop.mixer = mixer  # type: ignore[assignment]
    loop.running = True
    loop.conductor.get_next_state_async = AsyncMock(return_value=_conductor_response())  # type: ignore[method-obj]
    loop._submit_job = AsyncMock(return_value=uuid.uuid4())
    loop._fetch_audio = AsyncMock(return_value=None)
    loop._await_jobs = AsyncMock(return_value={})
    loop._append_loop_audit = AsyncMock()
    loop._pre_generate_next_loop = AsyncMock()
    return loop


def _conductor_response() -> dict:
    return {
        "master_bpm": 128,
        "master_key": "A minor",
        "name": "Round3 Set",
        "reasoning": "hold the groove",
        "actions": [
            {
                "action_type": "add",
                "major_family": "Synth",
                "sub_family": "Synth Pad",
                "model_id": "foundation-1",
                "timbre_tags": ["warm"],
                "notation_tag": "melody",
                "fx_tag": "dry",
                "bars": 4,
            }
        ],
    }


def _pending_pregen_task(loop: AsyncFrameworkLoop) -> asyncio.Task:
    """A never-completing background pre-gen task (the B1 trap trigger)."""

    async def _never_finishes():
        await asyncio.Event().wait()

    task = asyncio.create_task(_never_finishes())
    loop._pregen_task = task
    return task


def _commit_result(*, needs_pregen: bool) -> _CommitResult:
    return _CommitResult(
        needs_pregen=needs_pregen,
        needs_initial_record=False,
        rec_stems=[],
        rec_set_name="",
        rec_reasoning="",
        state_snapshot={"current_bpm": 128, "current_key": "A minor"},
    )


def _audio(seconds: float = 0.1, sample_rate: int = 44100) -> np.ndarray:
    return np.ones((int(seconds * sample_rate), 2), dtype=np.float32) * 0.25


# --------------------------------------------------------------------------- #
# B1 — stale pre-gen must not fabricate a replay result, must not swallow stop
# --------------------------------------------------------------------------- #


async def test_b1_post_commit_does_not_fabricate_result_while_pregen_pending():
    """A still-running pre-gen task must not be papered over with a fake result.

    Pre-fix the else branch set ``_pregen_done`` and fabricated
    ``_pregen_results[loop_idx+1]``, which the next iteration's P2 gate accepted
    -> zero-suspension replay iteration (review 01/1).
    """
    loop = _make_loop(FakeMixer())
    task = _pending_pregen_task(loop)
    loop._loop_idx = 3
    loop._pregen_done.clear()
    loop._pregen_results = None

    try:
        await loop._step_post_commit(_commit_result(needs_pregen=False), [], 0)
    finally:
        task.cancel()

    assert loop._pregen_results is None, "a stale pre-gen must not fabricate results for loop 4"
    assert not loop._pregen_done.is_set(), "the pending task owns _pregen_done; the else branch must not set it"


async def test_b1_loop_one_queued_result_is_still_issued():
    """Loop 1 keeps issuing the queued-loop result (no in-flight task to clobber)."""
    loop = _make_loop(FakeMixer())
    loop._loop_idx = 1
    loop._pregen_done.clear()
    loop._pregen_results = None

    await loop._step_post_commit(_commit_result(needs_pregen=False), [(_audio(), 0)], 4410)

    assert loop._pregen_results is not None
    assert loop._pregen_results["loop_idx"] == 2
    assert loop._pregen_done.is_set()


async def test_b1_pregen_ready_branch_rechecks_is_generating():
    """P2 must not PROCEED on a queued pre-gen result after the set was stopped."""
    loop = _make_loop(FakeMixer())
    loop._loop_idx = 4
    loop._pregen_results = {
        "loop_idx": 4,
        "prepared_tracks": [(_audio(), 0)],
        "loop_duration_samples": 4410,
        "next_stems": [],
    }
    state.is_generating = False

    decision = await loop._step_pregen_decision()

    assert decision.result is _StepResult.RESTART_ITER
    assert decision.pregen_ready is False


async def test_b1_pregen_ready_branch_proceeds_while_generating():
    """Control: an unstopped set still consumes the queued pre-gen result."""
    loop = _make_loop(FakeMixer())
    loop._loop_idx = 4
    loop._pregen_results = {
        "loop_idx": 4,
        "prepared_tracks": [(_audio(), 0)],
        "loop_duration_samples": 4410,
        "next_stems": [],
    }
    state.is_generating = True

    decision = await loop._step_pregen_decision()

    assert decision.result is _StepResult.PROCEED
    assert decision.pregen_ready is True


async def test_b1_pregen_done_break_always_yields_at_least_once(sleep_log):
    """P13's fast path must contain a suspension point (B1a)."""
    loop = _make_loop(FakeMixer(headroom_seconds=10.0))
    loop._loop_idx = 5
    loop._pregen_done.set()

    await asyncio.wait_for(loop._step_await_pregen(), timeout=5.0)

    assert len(sleep_log) >= 1, "returning from P13 without awaiting starves the event loop"


async def test_b1_stale_pregen_replay_iterations_yield(sleep_log):
    """Every iteration of a stale-pregen drive suspends at least once."""
    mixer = StopAfterNMixer(stop_after=5)
    loop = _make_loop(mixer)
    mixer._loop = loop
    task = _pending_pregen_task(loop)

    try:
        await asyncio.wait_for(loop._run_loop(), timeout=10.0)
    finally:
        task.cancel()

    assert state.loop_count >= 2, "the drive never reached the stale-pregen iterations"
    assert len(sleep_log) >= state.loop_count, (
        f"{state.loop_count} committed loops but only {len(sleep_log)} suspension points: "
        "the replay iteration must always yield"
    )


# --------------------------------------------------------------------------- #
# B2 — staged next-loop audio must survive until the mixer consumes it
# --------------------------------------------------------------------------- #


async def test_b2_pregen_done_does_not_drop_the_staged_loop(sleep_log):
    """A fully generated loop already staged in the mixer must reach its boundary.

    Pre-fix ``_step_await_pregen`` broke on ``_pregen_done`` immediately, so the
    next iteration's ``set_next_loop`` replaced the pending loop-2 audio.
    """
    mixer = AdvancingMixer(headroom_seconds=8.0)
    loop = _make_loop(mixer)
    loop._loop_idx = 2
    mixer.current_loop_end_sample = 44100 * 8  # loop 1 already primed (REL-02 force must not fire)
    loop._pregen_done.set()  # pre-gen for loop 3 finished before the boundary
    loop_state_before = mixer.next_loop_audio

    loop_tracks = [(_audio(), 0)]
    await loop._step_commit_to_mixer(
        pregen_ready=False,
        prepared_tracks=loop_tracks,
        loop_duration_samples=44100,
    )
    assert mixer.next_loop_audio == loop_tracks
    assert loop_state_before == []

    await asyncio.wait_for(loop._step_await_pregen(), timeout=5.0)

    assert mixer.transition_fired_for == 2, "P13 returned before the mixer consumed the staged loop"
    assert mixer.consumed_tracks == loop_tracks, "the staged loop-2 audio must be what got played"
    assert len(mixer.set_next_loop_calls) == 1, "loop-2 audio must not have been re-queued/overwritten"


async def test_b2_staged_wait_releases_when_the_playhead_is_frozen(sleep_log):
    """A stopped mixer must not hold P13 hostage (bounded wait, no hang)."""
    mixer = FakeMixer(headroom_seconds=30.0)
    loop = _make_loop(mixer)
    loop._loop_idx = 3
    mixer.current_loop_end_sample = 44100 * 8  # loop 1 already primed (REL-02 force must not fire)
    loop._pregen_done.set()

    await loop._step_commit_to_mixer(pregen_ready=False, prepared_tracks=[(_audio(), 0)], loop_duration_samples=44100)
    await asyncio.wait_for(loop._step_await_pregen(), timeout=5.0)

    # Exactly one extra paced poll, then the frozen-playhead release fires.
    assert mixer.transition_fired_for is None
    assert len(sleep_log) == 2


async def test_b2_loop_one_priming_stages_nothing():
    """The loop-1 prime_loop handoff stages nothing, so P13 keeps its old pace."""
    mixer = FakeMixer(headroom_seconds=30.0)
    loop = _make_loop(mixer)
    loop._loop_idx = 1
    loop._pregen_done.set()

    await loop._step_commit_to_mixer(pregen_ready=False, prepared_tracks=[(_audio(), 0)], loop_duration_samples=44100)
    await asyncio.wait_for(loop._step_await_pregen(), timeout=5.0)

    assert loop._staged_loop_idx == 0
    assert mixer.prime_loop_calls, "loop 1 must prime the mixer directly"


# --------------------------------------------------------------------------- #
# B3 — batch job wait must cover a sequential worker drain + recover lateness
# --------------------------------------------------------------------------- #


async def test_b3_batch_wait_covers_the_sequential_worker_drain():
    """The 4-6 stem batch wait must be far larger than the old flat 120 s."""
    assert JOB_WAIT_TIMEOUT_SECONDS == pytest.approx(600.0)
    assert JOB_WAIT_TIMEOUT_SECONDS > 4 * 30.0, "must cover 4+ sequential 30 s generations"
    assert 0 < JOB_LATE_COMPLETION_GRACE_SECONDS < JOB_WAIT_TIMEOUT_SECONDS

    loop = _make_loop(FakeMixer())
    job_id = uuid.uuid4()
    loop._await_jobs = AsyncMock(return_value={job_id: "audio/x.aac"})

    await loop._step_await_jobs_fetch([(job_id, 0, "cache-key")], [{"prompt": "p", "bars": 4}])

    first_call = loop._await_jobs.await_args_list[0]
    assert first_call.args[0] == [job_id]
    assert first_call.kwargs["timeout"] == JOB_WAIT_TIMEOUT_SECONDS


async def test_b3_late_completion_is_recovered_instead_of_lost():
    """A job finishing after the batch deadline must still reach the mixer."""
    loop = _make_loop(FakeMixer())
    job_id = uuid.uuid4()
    audio = _audio()
    late = AsyncMock(side_effect=[{job_id: None}, {job_id: "audio/late.aac"}, {job_id: "audio/late.aac"}])
    loop._await_jobs = late
    loop._fetch_audio = AsyncMock(return_value=audio)

    with patch.object(state, "cache_stem") as cache_stem:
        await loop._step_await_jobs_fetch([(job_id, 0, "cache-key")], [{"prompt": "Pad, A minor", "bars": 4}])

    assert late.await_count == 2, "the grace pass must re-wait for the unfinished job"
    assert late.await_args_list[1].args[0] == [job_id]
    assert late.await_args_list[1].kwargs["timeout"] == JOB_LATE_COMPLETION_GRACE_SECONDS
    loop._fetch_audio.assert_awaited_once_with("audio/late.aac")
    cache_stem.assert_called_once_with("Pad, A minor", audio)
    assert loop.stem_cache["cache-key"]["audio_data"] is audio


async def test_b3_no_grace_pass_when_every_job_completed():
    """Completed batches are not waited on twice."""
    loop = _make_loop(FakeMixer())
    job_id = uuid.uuid4()
    loop._await_jobs = AsyncMock(return_value={job_id: "audio/x.aac"})

    await loop._step_await_jobs_fetch([(job_id, 0, "cache-key")], [{"prompt": "p", "bars": 4}])

    assert loop._await_jobs.await_count == 1


async def test_b3_pregen_path_uses_the_same_larger_wait():
    """The background pre-gen path shares the foreground batch budget (B3)."""
    from app.framework.pregeneration import run_pregeneration

    loop = _make_loop(FakeMixer())
    job_id = uuid.uuid4()
    loop._await_jobs = AsyncMock(return_value={job_id: None})

    snapshot = {
        "current_bpm": 128,
        "current_key": "A minor",
        "active_stems": [],
        "llm_config": {"base_url": "http://x", "api_key": "k", "model": "m"},
    }
    await run_pregeneration(loop, 2, snapshot)

    first_call = loop._await_jobs.await_args_list[0]
    assert first_call.kwargs["timeout"] == JOB_WAIT_TIMEOUT_SECONDS
    grace_call = loop._await_jobs.await_args_list[1]
    assert grace_call.kwargs["timeout"] == JOB_LATE_COMPLETION_GRACE_SECONDS


# --------------------------------------------------------------------------- #
# B4 — the DJ's tempo/key override must survive the pre-gen commit
# --------------------------------------------------------------------------- #


async def _prime_pregen_commit(loop: AsyncFrameworkLoop, *, master_bpm, master_key) -> None:
    loop._loop_idx = 2
    loop._pregen_results = {
        "loop_idx": 2,
        "prepared_tracks": [],
        "loop_duration_samples": 4410,
        "next_stems": [],
        "master_bpm": master_bpm,
        "master_key": master_key,
    }


async def test_b4_override_is_applied_after_pregen_values_are_committed():
    """A user override must beat the pre-gen decision taken before it existed."""
    loop = _make_loop(FakeMixer())
    state.current_bpm = 128
    state.current_key = "A minor"
    state.target_bpm_override = 150
    state.target_key_override = "F# minor"
    await _prime_pregen_commit(loop, master_bpm=128, master_key="A minor")

    snapshot = await loop._step_read_state()
    commit = await loop._step_commit_state(pregen_ready=True, tracks_to_use=[], duration_samples=4410)

    assert snapshot.current_bpm == 150, "the override must already inform the conductor prompt"
    assert snapshot.current_key == "F# minor"
    assert state.current_bpm == 150, "the pre-gen commit reverted the DJ's tempo"
    assert state.current_key == "F# minor", "the pre-gen commit reverted the DJ's key"
    assert commit.state_snapshot["current_bpm"] == 150, "the next pre-gen must inherit the override"
    assert commit.state_snapshot["current_key"] == "F# minor"
    assert state.target_bpm_override is None, "the override must be cleared once applied"
    assert state.target_key_override is None


async def test_b4_pregen_values_win_when_no_override_is_pending():
    """Control: without a pending override the pre-gen decision still applies."""
    loop = _make_loop(FakeMixer())
    state.current_bpm = 128
    state.current_key = "A minor"
    await _prime_pregen_commit(loop, master_bpm=140, master_key="D major")

    await loop._step_read_state()
    await loop._step_commit_state(pregen_ready=True, tracks_to_use=[], duration_samples=4410)

    assert state.current_bpm == 140
    assert state.current_key == "D major"


async def test_b4_fresh_path_override_still_lands():
    """The fresh (non-pregen) path keeps applying + clearing the override."""
    loop = _make_loop(FakeMixer())
    loop._loop_idx = 1
    state.target_bpm_override = 150
    state.target_key_override = "F# minor"

    snapshot = await loop._step_read_state()
    deduped = await loop._step_parse_actions(_conductor_response(), [])
    await loop._step_build_next_stems(
        snapshot.bpm_override,
        snapshot.key_override,
        _conductor_response(),
        snapshot.current_bpm,
        snapshot.current_key,
        deduped,
    )
    await loop._step_commit_state(pregen_ready=False, tracks_to_use=[], duration_samples=4410)

    assert state.current_bpm == 150
    assert state.current_key == "F# minor"
    assert state.target_bpm_override is None
    assert state.target_key_override is None


# --------------------------------------------------------------------------- #
# B5 — null / invalid master_bpm + master_key must never reach state
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [(None, 128), ("128", 128), (0, 128), (39, 128), (301, 128), (True, 128), (150.5, 128), (140, 140)],
)
def test_b5_sanitize_master_bpm_coalesces_invalid_values(candidate, expected):
    assert sanitize_master_bpm(candidate, 128) == expected


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [(None, "A minor"), ("", "A minor"), ("   ", "A minor"), (7, "A minor"), ("F# minor", "F# minor")],
)
def test_b5_sanitize_master_key_coalesces_invalid_values(candidate, expected):
    assert sanitize_master_key(candidate, "A minor") == expected


async def test_b5_null_master_bpm_does_not_poison_state_or_job_rows():
    """``{"master_bpm": null}`` used to defeat ``.get(default)`` -> None everywhere."""
    loop = _make_loop(FakeMixer())
    loop._loop_idx = 2
    response = dict(_conductor_response(), master_bpm=None, master_key=None)
    deduped = await loop._step_parse_actions(response, [])

    next_stems, bpm, key = await loop._step_build_next_stems(None, None, response, 128, "A minor", deduped)

    assert bpm == 128 and key == "A minor"
    assert state.current_bpm == 128 and state.current_key == "A minor"
    assert next_stems and all(s["bpm"] == 128 and s["key"] == "A minor" for s in next_stems)
    assert "None" not in next_stems[0]["prompt"]


async def test_b5_null_pregen_master_values_are_coalesced_at_commit():
    """The pre-gen commit path is symmetric with the foreground one."""
    loop = _make_loop(FakeMixer())
    state.current_bpm = 120
    state.current_key = "C minor"
    await _prime_pregen_commit(loop, master_bpm=None, master_key=None)

    await loop._step_read_state()
    await loop._step_commit_state(pregen_ready=True, tracks_to_use=[], duration_samples=4410)

    assert state.current_bpm == 120
    assert state.current_key == "C minor"


async def test_b5_pregenerated_results_never_store_null_master_fields():
    """``run_pregeneration`` coalesces before publishing ``_pregen_results``."""
    from app.framework.pregeneration import run_pregeneration

    loop = _make_loop(FakeMixer())
    loop.conductor.get_next_state_async = AsyncMock(  # type: ignore[method-obj]
        return_value=dict(_conductor_response(), master_bpm=None, master_key=None)
    )
    snapshot = {
        "current_bpm": 120,
        "current_key": "C minor",
        "active_stems": [],
        "llm_config": {"base_url": "http://x", "api_key": "k", "model": "m"},
    }

    await run_pregeneration(loop, 2, snapshot)

    assert loop._pregen_results is not None
    assert loop._pregen_results["master_bpm"] == 120
    assert loop._pregen_results["master_key"] == "C minor"


# --------------------------------------------------------------------------- #
# B6 — startup failure must not strand state.is_running
# --------------------------------------------------------------------------- #


async def test_b6_mixer_startup_failure_is_handled_and_clears_is_running(monkeypatch):
    """``start()`` blowing up must run the shutdown path, not die silently."""
    built: dict[str, AsyncFrameworkLoop] = {}

    def _boom() -> FakeMixer:
        raise RuntimeError("sounddevice init failed")

    def _factory(session_id: uuid.UUID) -> AsyncFrameworkLoop:
        loop = AsyncFrameworkLoop(session_id, mixer_factory=_boom)
        built["loop"] = loop
        return loop

    monkeypatch.setattr(loop_orchestrator, "AsyncFrameworkLoop", _factory)
    state.is_running = True
    state.shutdown_event.clear()

    await asyncio.wait_for(loop_orchestrator.run_framework_loop_async(uuid.uuid4()), timeout=10.0)

    assert built, "the framework never built its loop"
    assert built["loop"].loop_task is None, "no run-loop task may exist after a failed start"
    assert built["loop"].running is False
    assert state.is_running is False, "/api/health must stop reporting a live framework"


async def test_b6_successful_start_still_runs_the_loop_task(monkeypatch):
    """Control: a healthy start spawns the loop task and keeps is_running True."""
    built: dict[str, AsyncFrameworkLoop] = {}

    def _factory(session_id: uuid.UUID) -> AsyncFrameworkLoop:
        loop = _make_loop(FakeMixer())
        built["loop"] = loop
        return loop

    async def _healthy_start(self):  # mirrors the real start() without a real Mixer
        self.running = True

        async def _finish_immediately():
            self.running = False

        self.loop_task = asyncio.create_task(_finish_immediately())

    monkeypatch.setattr(loop_orchestrator, "AsyncFrameworkLoop", _factory)
    monkeypatch.setattr(AsyncFrameworkLoop, "start", _healthy_start)
    state.is_running = True

    await asyncio.wait_for(loop_orchestrator.run_framework_loop_async(uuid.uuid4()), timeout=10.0)

    assert built["loop"].loop_task is not None
    assert state.is_running is True
