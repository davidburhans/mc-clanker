"""FU-2 (follow-ups round 2) regression suite — loop epoch gate + submit-side streak reset + orchestrator split.

TDD RED suite for refactor/plans/units/rel-fu-2-plan.md §3.1. Three contracts:

* E1-E4 (rel-02 residual): a pregen result must carry the loop-numbering
  EPOCH it was spawned in. Post-reset numbering restarts at 1 (REL-02 force),
  so a pre-reset in-flight result for loop M was accepted ONCE more when the
  post-reset ``_loop_idx`` revisited M — one loop of stale (pre-reset) audio.
  The fix stamps ``pregen_epoch`` into results at SPAWN time (P11 snapshot),
  bumps ``loop._pregen_epoch`` when P3 consumes ``should_reset`` (monotonic,
  never back to 0), and P2 requires epoch equality.
* O1-O4 (rel-12 / rel-17-review residual): the conductor-skip streak must
  reset ONLY on a successful SUBMIT, never on the read probe. A read-only PG
  hot standby answers every probe while every submit fails — the old
  probe-side reset re-enabled the full LLM call EVERY cycle in that mode. The
  probe now merely gates a one-shot WRITE canary through the ``_submit_job``
  seam (the exact operation whose failure the streak counts).
* S1 (rel-17-review debt): ``loop_orchestrator.py`` (512 lines) must be split
  under the 500-LOC rule — the ``_LoopDelegates`` mixin module must exist and
  both files stay under the AGENTS.md limit.

RED expectations at HEAD: E1/E3/E4 fail on the missing epoch machinery
(``_pregen_epoch`` absent / result unstamped / stale result still accepted);
O1/O2 fail because the first probe success still zeroes the streak (conductor
re-called); O3 fails because no canary submit/abandon exists; O4 fails because
the skip never disengages through the recovery window today; S1 fails because
``loop_delegates.py`` does not exist and the orchestrator is 512 lines. E2
pins the acceptance half of the epoch gate (no over-rejection) and is the
companion keep-green expectation.
"""

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.framework.framework_state import state
from app.framework.loop_orchestrator import AsyncFrameworkLoop
from app.framework.loop_steps import LOOP_CONDUCTOR_SKIP_AFTER_SUBMIT_FAILURES
from app.framework.pregeneration import run_pregeneration

# --------------------------------------------------------------------------- #
# Named fakes — ctor-injected ports + state hygiene (no real DB / LLM / mixer)
# --------------------------------------------------------------------------- #


class _CountingConductor:
    """ConductorPort fake: counts LLM calls, returns a fixed decision.

    Default decision carries ONE ``add`` action (fresh iterations have an
    uncached stem for P7); pass ``actions=[]`` to keep the submit path idle.
    Same shape as test_loop_robustness._CountingConductor.
    """

    def __init__(self, actions: list[dict[str, Any]] | None = None):
        self.calls = 0
        self._actions: list[dict[str, Any]] = (
            [
                {
                    "action_type": "add",
                    "instrument": "Test Synth",
                    "major_family": "Synth",
                    "sub_family": "Test Synth",
                    "model_id": "foundation-1",
                }
            ]
            if actions is None
            else actions
        )

    async def get_next_state_async(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "master_bpm": 120,
            "master_key": "C minor",
            "name": "Conductor Live",
            "reasoning": "live decision",
            "actions": list(self._actions),
        }


class _ReadOnlyStandbyJobQueue:
    """JobQueuePort fake modelling a PG hot standby: READS always succeed
    (``pending_depth`` -> 0) while EVERY write (``submit``) fails — the exact
    mode where the old probe-side streak reset re-enabled the conductor call
    every cycle. ``await_jobs``/``abandon_jobs`` are inert call recorders."""

    def __init__(self) -> None:
        self.probe_calls = 0
        self.submit_calls = 0
        self.submit_failures = 0
        self.await_calls = 0
        self.abandon_calls = 0
        self.abandoned_ids: list[uuid.UUID] = []

    async def submit(self, **kwargs: Any) -> uuid.UUID:
        self.submit_calls += 1
        self.submit_failures += 1
        raise RuntimeError("read-only standby: cannot INSERT (simulated)")

    async def await_jobs(self, job_ids: list[uuid.UUID], timeout: float = 120.0) -> dict[uuid.UUID, str | None]:
        self.await_calls += 1
        return {job_id: None for job_id in job_ids}

    async def abandon_jobs(self, job_ids: list[uuid.UUID]) -> int:
        self.abandon_calls += 1
        self.abandoned_ids.extend(job_ids)
        return len(job_ids)

    async def pending_depth(self) -> int:
        self.probe_calls += 1
        return 0


class _RecoveringJobQueue:
    """JobQueuePort fake whose outage flag flips reads and writes TOGETHER
    (a real PG restart: ``pending_depth`` and ``submit`` fail, then both
    succeed). Recovers after N ``pending_depth`` probes when
    ``recover_after_probes`` is set, or immediately via ``recover()``."""

    def __init__(self, *, recover_after_probes: int | None = None) -> None:
        self.outage = True
        self.recover_after_probes = recover_after_probes
        self.probe_calls = 0
        self.submit_calls = 0
        self.submit_failures = 0
        self.abandon_calls = 0
        self.submitted_ids: list[uuid.UUID] = []
        self.abandoned_ids: list[uuid.UUID] = []

    def recover(self) -> None:
        self.outage = False

    def _maybe_recover(self) -> None:
        if (
            self.outage
            and self.recover_after_probes is not None
            and self.probe_calls >= self.recover_after_probes
        ):
            self.outage = False

    async def submit(self, **kwargs: Any) -> uuid.UUID:
        self.submit_calls += 1
        if self.outage:
            self.submit_failures += 1
            raise RuntimeError("db unreachable (simulated outage)")
        job_id = uuid.uuid4()
        self.submitted_ids.append(job_id)
        return job_id

    async def await_jobs(self, job_ids: list[uuid.UUID], timeout: float = 120.0) -> dict[uuid.UUID, str | None]:
        return {job_id: None for job_id in job_ids}

    async def abandon_jobs(self, job_ids: list[uuid.UUID]) -> int:
        self.abandon_calls += 1
        self.abandoned_ids.extend(job_ids)
        return len(job_ids)

    async def pending_depth(self) -> int:
        self.probe_calls += 1
        self._maybe_recover()
        if self.outage:
            raise RuntimeError("db unreachable (simulated outage)")
        return 0


class _BoundAudit:
    """AuditSinkPort fake: records responses, stops the loop after N appends
    (the _ScriptedAudit shape from test_loop_robustness, without fault
    injection — the stop bound keeps _run_loop drives deterministic)."""

    def __init__(self, stop_after: int) -> None:
        self.stop_after = stop_after
        self.calls = 0
        self.responses: list[dict[str, Any]] = []
        self.bound_loop: AsyncFrameworkLoop | None = None

    async def append_loop(self, conductor_response: dict[str, Any], active_stems: list, loop_idx: int) -> None:
        self.calls += 1
        self.responses.append(conductor_response)
        if self.calls >= self.stop_after and self.bound_loop is not None:
            self.bound_loop.running = False
            state.is_running = False

    async def flush(self) -> None:
        return None


class _FakeMixer:
    """Mixer stand-in (test_loop_robustness._FakeMixerWithPosition shape):
    playhead reads 0.0 so P13 exits immediately; prime_loop mirrors the real
    boundary write (REL-02) so later commits stage instead of re-priming."""

    def __init__(self, *, sample_rate: int = 44100):
        self.sample_rate = sample_rate
        self.current_sample = 0
        self.current_loop_end_sample = 0
        self.started = False
        self.stopped = False
        self.staged: list[dict[str, Any]] = []

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def clear(self):
        self.current_loop_end_sample = 0

    def prime_loop(self, tracks, *, duration_samples):
        self.current_loop_end_sample = self.current_sample + duration_samples

    def set_next_loop(self, tracks, next_loop_duration_samples=0, loop_idx=0):
        self.staged.append({"tracks": list(tracks), "loop_idx": loop_idx})

    def pop_transition_event(self):
        return None

    def loop_position_seconds(self):
        return 0.0

    def _add_track_internal(self, *args, **kwargs):
        return None

    def _ensure_stereo(self, audio):
        return audio


# --------------------------------------------------------------------------- #
# State hygiene + helpers (pattern: tests/test_loop_robustness.py)
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
    "loop_history",
    "stem_volumes",
    "muted_stems",
    "soloed_stems",
    "target_bpm_override",
    "target_key_override",
    "user_override",
    "is_generating",
    "is_running",
    "should_reset",
    "currently_playing_loop_index",
    "currently_playing_stems",
    "currently_playing_set_name",
    "currently_playing_reasoning",
    "audio_clients",
    "active_subprocesses",
    "youtube_relay",
)


def _copy_state_attr(attr: str):
    value = getattr(state, attr)
    if isinstance(value, (list, dict, set)):
        return type(value)(value)
    return value


@pytest.fixture(autouse=True)
def _isolated_state():
    """Snapshot/restore the state singleton so drives and reset consumption
    cannot leak between tests."""
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
def sleep_recorder(monkeypatch):
    """Record every ``asyncio.sleep`` and collapse it (fast, deterministic
    waits) — the B1 backoff sleeps are the only >= 1 s delays in these drives."""
    delays: list[float | None] = []

    async def _recording_sleep(delay=None):
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _recording_sleep)
    return delays


def _loop_with(
    mixer,
    *,
    conductor: _CountingConductor | None = None,
    jobs: Any = None,
    audit: Any = None,
) -> AsyncFrameworkLoop:
    """Loop with every port ctor-injected (fakes, not patches)."""
    loop = AsyncFrameworkLoop(uuid.uuid4(), conductor=conductor, jobs=jobs, audit=audit)
    loop.mixer = mixer  # type: ignore[assignment]  # boundary-faithful fake
    return loop


def _seed_pregen_result(loop: AsyncFrameworkLoop, *, loop_idx: int, epoch: int, **extra: Any) -> None:
    """Hand-craft a ``_pregen_results`` dict with an explicit epoch stamp (the
    shape run_pregeneration writes; extra kwargs override single fields)."""
    result: dict[str, Any] = {
        "loop_idx": loop_idx,
        "pregen_epoch": epoch,
        "prepared_tracks": [("audio", 0)],
        "loop_duration_samples": 44100,
        "next_stems": [],
        "master_bpm": 96,
        "master_key": "C minor",
        "set_name": "Epoch Set",
        "reasoning": "pregen reasoning",
        "actions": [],
    }
    result.update(extra)
    loop._pregen_results = result


def _conductor_args() -> tuple:
    """P4's positional args for a bare session (empty stems, no history)."""
    return (128, "A minor", [], None, [], [], {"base_url": "unused", "api_key": "unused", "model": "unused"}, [])


def _clearing_pregen_spawn(loop: AsyncFrameworkLoop, spawns: dict[str, int]):
    """Replace the background pre-gen spawn: clear fabricated results so the
    NEXT iteration runs the FRESH path again (P4's guard owns recovery)."""

    async def _fake_pregen(for_loop_idx, snapshot):
        spawns["n"] += 1
        loop._pregen_results = None
        loop._pregen_done.set()

    return _fake_pregen


async def _drive_outage_loop(loop: AsyncFrameworkLoop, audit: _BoundAudit, timeout: float = 30.0) -> None:
    """Drive ``_run_loop`` directly (start() never ran — mirror _drive_loop)."""
    loop._audit = audit
    audit.bound_loop = loop
    spawns: dict[str, int] = {"n": 0}
    loop._pre_generate_next_loop = _clearing_pregen_spawn(loop, spawns)  # type: ignore[method-assign]
    loop.mixer = _FakeMixer()
    # Pre-establish the mixer boundary (REL-02): a boundary-0 mixer would make
    # P10 re-prime with loop-1 semantics and steal an iteration's shape.
    loop.mixer.current_loop_end_sample = 1
    loop.running = True
    state.is_generating = True
    state.is_running = True
    state.shutdown_event.clear()
    await asyncio.wait_for(loop._run_loop(), timeout=timeout)


# --------------------------------------------------------------------------- #
# E1-E4 — pregen epoch gate (rel-02 residual: stale pre-reset result)
# --------------------------------------------------------------------------- #


async def test_stale_pregen_result_old_epoch_rejected_when_indices_revisit():
    """E1 (FU-2 item 1 core): a result spawned pre-reset (epoch 0) must be
    REJECTED at P2 once the post-reset loop index revisits its loop_idx."""
    conductor = _CountingConductor(actions=[])
    loop = _loop_with(_FakeMixer(), conductor=conductor)
    loop._loop_idx = 7
    _seed_pregen_result(loop, loop_idx=7, epoch=0)

    state.should_reset = True
    await loop._step_read_state()  # P3 consumes the reset: FU-2 must bump the epoch to 1

    decision = await loop._step_pregen_decision()
    assert decision.pregen_ready is False, (
        "a pre-reset result (epoch 0) must be rejected once post-reset indices revisit its loop_idx"
    )
    assert decision.conductor_response is None, "the loop must take the FRESH path for a stale-epoch result"
    assert loop._pregen_epoch == 1, "consuming should_reset must bump the pregen epoch (FU-2 item 1)"


async def test_fresh_epoch_result_accepted_after_reset():
    """E2 (no over-rejection): a result stamped with the CURRENT epoch is
    accepted at P2 and its fields build the conductor response."""
    loop = _loop_with(_FakeMixer())
    state.should_reset = True
    await loop._step_read_state()  # epoch -> 1

    loop._loop_idx = 2
    _seed_pregen_result(loop, loop_idx=2, epoch=1, master_bpm=96, set_name="Epoch Set")

    decision = await loop._step_pregen_decision()
    assert decision.pregen_ready is True, "a current-epoch result must stay accepted (the gate must not over-reject)"
    assert decision.conductor_response is not None
    assert decision.conductor_response["master_bpm"] == 96, "response must be built from the result's master_bpm"
    assert decision.conductor_response["name"] == "Epoch Set", "response must be built from the result's set_name"
    assert decision.prepared_tracks == [("audio", 0)], "pregen outputs must ride the decision untouched"


async def test_epoch_monotonic_across_resets_never_returns_to_zero():
    """E3: the epoch survives every should_reset — it bumps 1 then 2 and never
    returns to 0, so even double-reset staleness is caught."""
    loop = _loop_with(_FakeMixer())

    state.should_reset = True
    await loop._step_read_state()
    assert loop._pregen_epoch == 1, "first reset bumps 0 -> 1"

    state.should_reset = True
    await loop._step_read_state()
    assert loop._pregen_epoch == 2, "a second reset must bump again (never back to 0)"

    loop._loop_idx = 3
    _seed_pregen_result(loop, loop_idx=3, epoch=0)
    decision = await loop._step_pregen_decision()
    assert decision.pregen_ready is False, "an epoch-0 result predates both resets"

    _seed_pregen_result(loop, loop_idx=3, epoch=1)
    decision = await loop._step_pregen_decision()
    assert decision.pregen_ready is False, "an epoch-1 result predates the second reset"

    _seed_pregen_result(loop, loop_idx=3, epoch=2)
    decision = await loop._step_pregen_decision()
    assert decision.pregen_ready is True, "an epoch-2 (current) result is accepted"


async def test_inflight_pregen_completing_after_reset_cannot_re_enter():
    """E4 (rel-02 decision 3 extension): a pregen task spawned BEFORE the reset
    (snapshot carries epoch 0) completes AFTER the loop's epoch bumped to 1 —
    the result must keep its SPAWN-TIME stamp and stay rejected at P2."""
    conductor = _CountingConductor(actions=[])  # no actions -> no submits, pure in-memory pregen
    loop = _loop_with(_FakeMixer(), conductor=conductor)

    state.should_reset = True
    await loop._step_read_state()

    snapshot = {
        "current_bpm": 128,
        "current_key": "A minor",
        "active_stems": [],
        "user_override": None,
        "available_instruments": [],
        "stem_history": [],
        "llm_config": {"base_url": "unused", "api_key": "unused", "model": "unused"},
        # The spawn-time stamp: this snapshot was taken BEFORE the reset above
        # was consumed, so it carries the pre-reset epoch.
        "pregen_epoch": 0,
    }
    await run_pregeneration(loop, 7, snapshot)

    assert conductor.calls == 1, "the background pregen conductor call is unaffected by the epoch work"
    assert loop._pregen_results is not None, "run_pregeneration must still publish its result"
    assert loop._pregen_results["loop_idx"] == 7
    assert loop._pregen_results.get("pregen_epoch") == 0, (
        "the result must carry its SPAWN-TIME epoch (from the snapshot), not the loop's post-reset epoch"
    )
    assert loop._pregen_epoch == 1, "the reset consumed mid-pregen must have bumped the loop's epoch"

    loop._loop_idx = 7
    decision = await loop._step_pregen_decision()
    assert decision.pregen_ready is False, "a pre-reset in-flight result must never re-enter at the revisited index"
    assert decision.conductor_response is None


# --------------------------------------------------------------------------- #
# O1-O4 — streak resets on successful SUBMIT only (rel-12 residual)
# --------------------------------------------------------------------------- #


async def test_read_only_standby_probe_does_not_reset_streak_conductor_stays_skipped():
    """O1 (FU-2 item 2 core): on a read-only standby (every probe succeeds,
    every submit fails) the streak must NEVER reset — the conductor stays
    skipped and each pass grows the streak by exactly one failed canary."""
    conductor = _CountingConductor()
    jobs = _ReadOnlyStandbyJobQueue()
    loop = _loop_with(_FakeMixer(), conductor=conductor, jobs=jobs)
    loop._consecutive_submit_failures = LOOP_CONDUCTOR_SKIP_AFTER_SUBMIT_FAILURES

    for pass_num in range(1, 4):
        response = await loop._step_call_conductor(*_conductor_args())
        assert conductor.calls == 0, f"pass {pass_num}: the conductor must stay skipped (probe success proves nothing)"
        assert response["name"] == "Fallback State", f"pass {pass_num}: the retain-all fallback must be returned"
        assert "job-queue submit outage" in response["reasoning"], f"pass {pass_num}: fallback must cite the outage"

    expected = LOOP_CONDUCTOR_SKIP_AFTER_SUBMIT_FAILURES + 3
    assert loop._consecutive_submit_failures == expected, (
        f"each pass must add ONE failed canary submit; the streak must NEVER reset (expected {expected})"
    )
    assert jobs.submit_calls == 3, "exactly one canary submit attempt per skip pass"
    assert jobs.probe_calls == 3, "exactly one read probe per skip pass (the canary gate)"


async def test_canary_failure_is_contained_fallback_iteration_survives():
    """O2 (containment): a failed canary submit must be caught INSIDE the skip
    guard — _step_call_conductor returns the fallback and never raises out of
    P4, so the retain-all fallback iteration can still commit."""
    conductor = _CountingConductor()
    jobs = _ReadOnlyStandbyJobQueue()
    loop = _loop_with(_FakeMixer(), conductor=conductor, jobs=jobs)
    loop._consecutive_submit_failures = LOOP_CONDUCTOR_SKIP_AFTER_SUBMIT_FAILURES

    response = await loop._step_call_conductor(*_conductor_args())  # must NOT raise

    assert response["name"] == "Fallback State", "the fallback iteration must survive the canary failure"
    assert "job-queue submit outage" in response["reasoning"]
    assert conductor.calls == 0, "the conductor must not be called when the canary fails"
    assert jobs.submit_calls == 1, "exactly one canary submit was attempted through the _submit_job seam"


async def test_successful_canary_resets_streak_and_resumes_conductor_same_iteration():
    """O3 (resume mechanics): after a writable recovery, the canary submit
    succeeds — the seam resets the streak, the canary row is best-effort
    abandoned, and the REAL conductor decision is built the SAME iteration."""
    conductor = _CountingConductor()
    jobs = _RecoveringJobQueue()
    jobs.recover()  # writable again (reads and writes both work)
    loop = _loop_with(_FakeMixer(), conductor=conductor, jobs=jobs)
    loop._consecutive_submit_failures = LOOP_CONDUCTOR_SKIP_AFTER_SUBMIT_FAILURES

    response = await loop._step_call_conductor(*_conductor_args())

    assert conductor.calls == 1, "the REAL conductor decision must be built the same iteration the canary succeeds"
    assert response["name"] == "Conductor Live", "the live decision — not the fallback — must be returned"
    assert loop._consecutive_submit_failures == 0, "only a successful SUBMIT (the canary's) may reset the streak"
    assert jobs.submit_calls == 1, "the canary is the only submit on this path"
    assert jobs.abandon_calls == 1, "the canary row must be best-effort abandoned so the worker never generates it"
    assert jobs.abandoned_ids == [jobs.submitted_ids[0]], "the abandoned row must be the canary's own job id"


async def test_full_outage_window_engages_and_disengages_the_skip(sleep_recorder):
    """O4 (end-to-end outage window): an outage engages the skip after 3
    failed submit iterations (fallbacks commit from cache); when the queue
    comes back the canary disengages the skip — the conductor resumes, submits
    succeed, and the skip never re-engages while submits keep succeeding."""
    conductor = _CountingConductor()
    # Recovery on the 7th pending_depth probe: 3 P7 backlog gauges (the outage
    # iterations) + guard probes at fallback iterations 1-3; the guard probe at
    # fallback iteration 4 is the one that observes recovery.
    jobs = _RecoveringJobQueue(recover_after_probes=7)
    loop = _loop_with(_FakeMixer(), conductor=conductor, jobs=jobs)
    audit = _BoundAudit(stop_after=5)

    await _drive_outage_loop(loop, audit)

    assert conductor.calls == 5, f"3 outage calls + 2 recovered calls expected, got {conductor.calls}"
    assert jobs.submit_failures == 3, "only the 3 outage iterations may fail a submit"
    assert jobs.submit_calls == 6, "3 failed + 1 canary + 2 recovered real submits expected"
    assert state.loop_count == 5, "every fallback/recovered iteration must COMMIT (outage retries do not)"
    assert audit.calls == 5
    assert audit.responses[0]["name"] == "Fallback State", "the skip must engage after the 3 outage iterations"
    assert audit.responses[2]["name"] == "Fallback State"
    assert audit.responses[3]["name"] == "Conductor Live", "the recovered iteration must carry the REAL decision"
    assert audit.responses[4]["name"] == "Conductor Live", "the skip must NOT re-engage while submits succeed"


# --------------------------------------------------------------------------- #
# S1 — orchestrator split under the 500-LOC rule (rel-17-review debt)
# --------------------------------------------------------------------------- #


def test_orchestrator_split_files_under_500_lines():
    """S1 (FU-2 item 3): both orchestrator files must exist and stay under the
    project's 500-LOC rule (AGENTS.md); the _LoopDelegates extraction is the
    sanctioned split seam (pure port-facing glue)."""
    for rel in ("app/framework/loop_orchestrator.py", "app/framework/loop_delegates.py"):
        path = Path(rel)
        assert path.exists(), f"{rel} missing — the _LoopDelegates extraction (FU-2 item 3) has not landed"
        line_count = len(path.read_text().splitlines())
        assert line_count < 500, f"{rel} is {line_count} lines — over the project's 500-LOC rule (AGENTS.md)"
