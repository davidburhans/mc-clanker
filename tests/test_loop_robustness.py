"""REL-18/REL-19 (U12) regression tests — loop robustness under a DB outage + scoped startup failure.

Findings (docs/reliability_audit.md P2):

* REL-18: the B1 watchdog sleeps a FLAT 2 s (no escalation, no jitter) and the
  full LLM conductor call is repeated every cycle while every submit fails.
* REL-19: ``run_framework_loop_async``'s startup-failure path calls the
  whole-app ``state.trigger_shutdown()`` — poisoning audience streams,
  finalizing recordings and killing the YouTube relay for a purely MUSICAL
  failure (the process itself is fine).

Contracts pinned here (per refactor/plans/units/rel-17-plan.md §3.2):

* ``loop_retry_backoff_delay``: base 2 s (first failure EXACTLY 2.0), ×2 per
  consecutive failure, cap 30 s, ±25 % uniform jitter (B1);
* the driver escalates across consecutive B1 failures and resets the ladder on
  the next clean pass (B2);
* after 3 consecutive submit failures the conductor call is SKIPPED (retain-all
  fallback keeps the set running from cache) and one cheap recovery probe runs
  per loop so the conductor resumes within one loop of the queue returning
  (B3, B4); the streak is owned by the ``_submit_job`` delegate (B5) and the
  pregen path gets the same gate (B6);
* a startup failure flips ONLY ``state.is_running`` under ``sync_lock`` — no
  kill switch (S1); the D11 done-callback keeps full cleanup for a task that
  DIED (S2, boundary); the existing B6 behavior stays green (S3).

TDD RED expectations at HEAD: B1/B2 (flat 2 s), B3/B4 (no skip/recovery), B6
(pregen calls the conductor) and S1 (trigger_shutdown runs) fail on their
behavioral assertions; B5/S2/S3 pin preserved seams and stay green.
"""

import asyncio
import queue
import time
import uuid
from typing import Any

import numpy as np
import pytest

from app.framework import loop_orchestrator
from app.framework.domain_audio import make_cache_key
from app.framework.framework_state import state
from app.framework.loop_orchestrator import AsyncFrameworkLoop, run_framework_loop_async
from app.framework.pregeneration import run_pregeneration

# --------------------------------------------------------------------------- #
# Named fakes — ctor-injected ports + state hygiene (no real DB / LLM / mixer)
# --------------------------------------------------------------------------- #


class _CountingConductor:
    """ConductorPort fake: counts LLM calls and returns a fixed decision.

    The default decision carries ONE ``add`` action so every fresh iteration
    has an uncached stem (P7 actually submits); pass ``actions=[]`` to keep P7
    idle (backoff tests). ``stop_after_calls`` is the bound-stop so a RED run
    against skip-less code terminates deterministically instead of looping.
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
        self.stop_after_calls: int | None = None
        self.stop_loop: AsyncFrameworkLoop | None = None

    def bind_stop(self, loop: AsyncFrameworkLoop, stop_after_calls: int) -> None:
        self.stop_loop = loop
        self.stop_after_calls = stop_after_calls

    async def get_next_state_async(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.stop_after_calls is not None and self.calls >= self.stop_after_calls and self.stop_loop is not None:
            self.stop_loop.running = False
            state.is_running = False
        return {
            "master_bpm": 120,
            "master_key": "C minor",
            "name": "Conductor Live",
            "reasoning": "live decision",
            "actions": list(self._actions),
        }


class _OutageJobQueuePort:
    """JobQueuePort fake simulating a DB outage.

    ``submit`` and ``pending_depth`` raise while ``outage`` is set; after
    ``probes_before_recovery`` pending-depth probes the queue "comes back"
    (probes and submits succeed again) — modelling the recovery the P4 guard's
    probe must detect. ``await_jobs``/``abandon_jobs`` are inert.
    """

    def __init__(self, *, probes_before_recovery: int | None = None):
        self.outage = True
        self.probes_before_recovery = probes_before_recovery
        self.probe_calls = 0
        self.submit_calls = 0
        self.submit_failures = 0

    def _maybe_recover(self) -> None:
        if (
            self.outage
            and self.probes_before_recovery is not None
            and self.probe_calls >= self.probes_before_recovery
        ):
            self.outage = False

    async def submit(self, **kwargs: Any) -> uuid.UUID:
        self.submit_calls += 1
        if self.outage:
            self.submit_failures += 1
            raise RuntimeError("db unreachable (simulated outage)")
        return uuid.uuid4()

    async def await_jobs(self, job_ids: list[uuid.UUID], timeout: float = 120.0) -> dict[uuid.UUID, str | None]:
        return {job_id: None for job_id in job_ids}

    async def abandon_jobs(self, job_ids: list[uuid.UUID]) -> int:
        return 0

    async def pending_depth(self) -> int:
        self.probe_calls += 1
        self._maybe_recover()
        if self.outage:
            raise RuntimeError("db unreachable (simulated outage)")
        return 0


class _ScriptedAudit:
    """AuditSinkPort fake: raises on scripted call numbers (B1 fault injection,
    the test_loop_fixes pattern — the audit step runs OUTSIDE P4's local try so
    its exception is what reaches the B1 watchdog) and optionally stops the
    loop after N clean appends."""

    def __init__(self, fail_on: set[int], *, stop_after: int | None = None):
        self.fail_on = fail_on
        self.stop_after = stop_after
        self.calls = 0
        self.responses: list[dict[str, Any]] = []
        self.bound_loop: AsyncFrameworkLoop | None = None

    async def append_loop(self, conductor_response: dict[str, Any], active_stems: list, loop_idx: int) -> None:
        self.calls += 1
        self.responses.append(conductor_response)
        if self.calls in self.fail_on:
            raise RuntimeError("transient audit blip")
        if self.stop_after is not None and self.calls >= self.stop_after and self.bound_loop is not None:
            self.bound_loop.running = False
            state.is_running = False

    async def flush(self) -> None:
        return None


class _FakeMixerWithPosition:
    """Mixer stand-in (test_loop_fixes._FakeMixer shape) whose playhead reads 0.0
    so P13 exits immediately on committed iterations, and whose ``prime_loop``
    mirrors the real boundary write (REL-02) so later commits stage instead of
    re-priming."""

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


class _FakeTrackedProc:
    """Mimics the Popen-like objects registered in ``state.active_subprocesses``
    (e.g. the YouTube relay's ffmpeg) for the kill-switch assertions."""

    def __init__(self):
        self.pid = 4242
        self.kill_calls = 0
        self.wait_calls = 0

    def kill(self):
        self.kill_calls += 1

    def wait(self, timeout=None):
        self.wait_calls += 1
        return 0


def _make_robust_loop(
    conductor: _CountingConductor,
    jobs: _OutageJobQueuePort,
    mixer: _FakeMixerWithPosition | None = None,
) -> AsyncFrameworkLoop:
    """Loop with every port ctor-injected (invariant 2: fakes, not patches)."""
    return AsyncFrameworkLoop(
        uuid.uuid4(),
        conductor=conductor,
        mixer_factory=lambda: mixer if mixer is not None else _FakeMixerWithPosition(),
        jobs=jobs,
        audit=_ScriptedAudit(set()),
    )


# --------------------------------------------------------------------------- #
# State hygiene (pattern: tests/test_round3_fix_b.py::_isolated_state)
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
def sleep_recorder(monkeypatch):
    """Record every ``asyncio.sleep`` and collapse it (fast, deterministic waits).

    The B1 backoff sleeps (≥ the 2 s base) are the only delays ≥ 1.0 s in these
    harnesses — P13's suspension sleeps are 0/0.25 s — so ``backoff_sleeps``
    isolates the watchdog ladder.
    """
    delays: list[float | None] = []

    async def _recording_sleep(delay=None):
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _recording_sleep)
    return delays


def backoff_sleeps(delays: list[float | None]) -> list[float]:
    return [d for d in delays if d is not None and d >= 1.0]


def _fresh_path_pregen_clearer(loop: AsyncFrameworkLoop, spawns: dict[str, int]):
    """Replace the background pre-gen spawn: clear the fabricated results so the
    NEXT iteration runs the FRESH path again (P4's guard + probe own recovery),
    instead of replaying the zero-yield fabrication forever."""

    async def _fake_pregen(for_loop_idx, snapshot):
        spawns["n"] += 1
        loop._pregen_results = None
        loop._pregen_done.set()

    return _fake_pregen


async def _drive_loop(loop: AsyncFrameworkLoop, *, timeout: float = 30.0) -> None:
    """Drive ``_run_loop`` directly (start() never ran — mirror test_loop_fixes)."""
    loop.mixer = _FakeMixerWithPosition()
    loop.running = True
    state.is_generating = True
    state.is_running = True
    state.shutdown_event.clear()
    await asyncio.wait_for(loop._run_loop(), timeout=timeout)


# --------------------------------------------------------------------------- #
# B1-B6 — REL-18 (backoff escalation + conductor skip)
# --------------------------------------------------------------------------- #


def test_loop_retry_backoff_delay_sequence_and_cap():
    """B1: pure backoff ladder — base 2.0 exact on n<=1, ×2 with ±25 % jitter
    after, capped at 30 s."""
    from app.framework.loop_steps import loop_retry_backoff_delay

    assert loop_retry_backoff_delay(1) == 2.0, "first failure keeps the flat base (today's timing)"
    assert loop_retry_backoff_delay(0) == 2.0

    for n, expected in ((2, 4.0), (3, 8.0), (4, 16.0)):
        delay = loop_retry_backoff_delay(n)
        assert expected * 0.75 <= delay <= expected * 1.25, f"n={n}: {delay} outside ±25 % of {expected}"

    for n in (5, 9):
        delay = loop_retry_backoff_delay(n)
        assert 22.5 <= delay <= 37.5, f"n={n}: {delay} outside the 30 s cap ±25 %"


async def test_backoff_sequence_asserted_and_resets_on_success(sleep_recorder):
    """B2 (REL-18 acceptance): the driver escalates [2.0] → [3,5] → [6,10] and
    RESETS to the flat base after a clean pass."""
    conductor = _CountingConductor(actions=[])  # keep P7 idle: the audit fault drives B1
    jobs = _OutageJobQueuePort()
    loop = _make_robust_loop(conductor, jobs)
    audit = _ScriptedAudit({1, 2, 3, 5}, stop_after=6)
    loop._audit = audit
    audit.bound_loop = loop
    spawns: dict[str, int] = {"n": 0}
    loop._pre_generate_next_loop = _fresh_path_pregen_clearer(loop, spawns)  # type: ignore[method-assign]

    await _drive_loop(loop)

    recorded = backoff_sleeps(sleep_recorder)
    assert len(recorded) == 4, f"expected exactly 4 fault sleeps (fault ×3 → success → fault), got {recorded}"
    assert recorded[0] == 2.0, f"first failure must sleep the exact base, got {recorded[0]}"
    assert 3.0 <= recorded[1] <= 5.0, f"second failure must be 4 s ±25 %, got {recorded[1]}"
    assert 6.0 <= recorded[2] <= 10.0, f"third failure must be 8 s ±25 %, got {recorded[2]}"
    assert recorded[3] == 2.0, (
        f"the ladder must reset to the flat base after the clean pass, got {recorded[3]}"
    )
    assert audit.calls == 6 and conductor.calls == 6, "loop must return cleanly after fault → success → fault → success"


async def test_conductor_skipped_after_three_consecutive_submit_failures(sleep_recorder):
    """B3 (REL-18 acceptance): 3 submit-failing iterations still call the LLM;
    every later iteration skips it (probe failing) and commits the retain-all
    fallback instead."""
    conductor = _CountingConductor()
    jobs = _OutageJobQueuePort()  # never recovers
    loop = _make_robust_loop(conductor, jobs)
    conductor.bind_stop(loop, stop_after_calls=8)  # RED-run bound: skip-less code stops here
    audit = _ScriptedAudit(set(), stop_after=3)
    loop._audit = audit
    audit.bound_loop = loop
    spawns: dict[str, int] = {"n": 0}
    loop._pre_generate_next_loop = _fresh_path_pregen_clearer(loop, spawns)  # type: ignore[method-assign]

    await _drive_loop(loop)

    assert conductor.calls == 3, (
        f"the LLM must be called exactly once per failing iteration (3) and then SKIPPED; got {conductor.calls}"
    )
    assert jobs.submit_failures == 3, "each of the first three iterations must have attempted (and failed) a submit"
    assert len(audit.responses) == 3, "three fallback iterations must have run to the audit step"
    first_skip = audit.responses[0]
    assert first_skip["name"] == "Fallback State", "the skip must produce the existing retain-all fallback shape"
    assert "job-queue submit outage" in first_skip["reasoning"]
    assert state.loop_count == 3, "fallback iterations must COMMIT (the set keeps playing from cache)"


async def test_conductor_resumes_after_queue_recovery(sleep_recorder):
    """B4 (REL-18 acceptance): once the recovery probe sees the queue healthy,
    the NEXT iteration calls the conductor and submits again."""
    conductor = _CountingConductor()
    # 6 failing probes: 3 P7 backlog gauges + guard probes at fallback iters 1-2;
    # the guard probe at fallback iter 3 is the one that observes recovery.
    jobs = _OutageJobQueuePort(probes_before_recovery=6)
    loop = _make_robust_loop(conductor, jobs)
    conductor.bind_stop(loop, stop_after_calls=10)
    audit = _ScriptedAudit(set(), stop_after=4)
    loop._audit = audit
    audit.bound_loop = loop
    spawns: dict[str, int] = {"n": 0}
    loop._pre_generate_next_loop = _fresh_path_pregen_clearer(loop, spawns)  # type: ignore[method-assign]

    await _drive_loop(loop)

    assert conductor.calls == 4, f"3 outage calls + 1 recovered call expected, got {conductor.calls}"
    assert jobs.submit_calls == 4, f"3 failed + 1 recovered submit expected, got {jobs.submit_calls}"
    assert jobs.submit_failures == 3
    assert len(audit.responses) == 4
    assert audit.responses[3]["name"] == "Conductor Live", (
        "the recovered iteration must carry the REAL conductor decision"
    )


async def test_submit_delegate_tracks_failure_streak():
    """B5 (decision 6): the streak lives in the ``_submit_job`` delegate — one
    seam covering both submit paths; success resets it."""
    conductor = _CountingConductor()
    jobs = _OutageJobQueuePort()
    loop = _make_robust_loop(conductor, jobs)
    submit_kwargs = dict(
        instrument="Test Synth",
        prompt="Test Synth, A minor, 128 BPM",
        major_family="Synth",
        model_id="foundation-1",
        key="A minor",
        bpm=128,
        timbre_tags=[],
        bars=8,
    )

    with pytest.raises(RuntimeError):
        await loop._submit_job(loop.session_id, **submit_kwargs)
    assert loop._consecutive_submit_failures == 1, "a failed submit must increment the streak"

    jobs.outage = False
    await loop._submit_job(loop.session_id, **submit_kwargs)
    assert loop._consecutive_submit_failures == 0, (
        "any successful submit proves the queue writable and resets the streak"
    )


async def test_pregeneration_skips_conductor_during_submit_outage():
    """B6 (decision 10): ``run_pregeneration`` gets the same gate — streak ≥ 3
    skips the background LLM call (no probe; the foreground owns recovery)."""
    seeded_stem = {
        "prompt": None,  # filled from _original_details below
        "_original_details": {
            "model_id": "foundation-1",
            "major_family": "Synth",
            "sub_family": "Seed Lead",
            "timbre_tags": [],
            "notation_tag": "4/4",
            "fx_tag": "dry",
            "bars": 8,
        },
        "_age": 1,
    }

    def _make_snapshot() -> dict[str, Any]:
        return {
            "current_bpm": 128,
            "current_key": "A minor",
            "active_stems": [dict(seeded_stem, _original_details=dict(seeded_stem["_original_details"]))],
            "llm_config": {"base_url": "http://llm", "api_key": "k", "model": "m"},
        }

    # --- control: streak 0 → the conductor IS called -------------------------
    healthy = _OutageJobQueuePort()
    healthy.outage = False
    control_conductor = _CountingConductor()
    control_loop = _make_robust_loop(control_conductor, healthy)
    snapshot = _make_snapshot()
    control_loop.mixer = _FakeMixerWithPosition()
    orig = snapshot["active_stems"][0]["_original_details"]
    prompt = control_loop._build_prompt(orig, "A minor", 128)
    control_loop.stem_cache[make_cache_key("foundation-1", prompt, 128, "A minor", 8)] = {
        "audio_data": np.zeros((2, 441), dtype=np.float32),
        "last_used": time.time(),
    }

    await run_pregeneration(control_loop, 2, snapshot)

    assert control_conductor.calls == 1, "control: with no submit-failure streak the pregen conductor runs"
    assert control_loop._pregen_results is not None
    assert control_loop._pregen_results["set_name"] == "Conductor Live"

    # --- outage: streak 3 → the conductor is NOT called ----------------------
    jobs = _OutageJobQueuePort()
    outage_conductor = _CountingConductor()
    outage_loop = _make_robust_loop(outage_conductor, jobs)
    outage_loop.mixer = _FakeMixerWithPosition()
    outage_loop._consecutive_submit_failures = 3
    snapshot = _make_snapshot()
    orig = snapshot["active_stems"][0]["_original_details"]
    prompt = outage_loop._build_prompt(orig, "A minor", 128)
    outage_loop.stem_cache[make_cache_key("foundation-1", prompt, 128, "A minor", 8)] = {
        "audio_data": np.zeros((2, 441), dtype=np.float32),
        "last_used": time.time(),
    }

    await run_pregeneration(outage_loop, 2, snapshot)

    assert outage_conductor.calls == 0, "the pregen LLM call must be skipped while the submit streak ≥ 3"
    assert outage_loop._pregen_results is not None, "the skip must still publish usable pregen results"
    assert outage_loop._pregen_results["set_name"] == "Fallback State"
    assert outage_loop._pregen_results["actions"] == [{"action_type": "retain", "stem_index": 0}]
    assert outage_loop._pregen_done.is_set()


# --------------------------------------------------------------------------- #
# S1-S3 — REL-19 (scoped startup failure)
# --------------------------------------------------------------------------- #


async def test_startup_failure_sets_is_running_false_without_kill_switch(monkeypatch):
    """S1 (REL-19 acceptance): a startup failure flips ONLY ``state.is_running``
    — no shutdown event, no poisoned clients, no killed procs, no relay harm.
    The real kill switch must still work (mechanism-intact tail)."""

    async def _boom_start(self):
        raise RuntimeError("mixer init exploded")

    monkeypatch.setattr(AsyncFrameworkLoop, "start", _boom_start)
    poison_checker: queue.Queue = queue.Queue()
    state.is_running = True
    state.is_generating = True
    state.shutdown_event.clear()
    state.audio_clients = [poison_checker]
    relay_sentinel = object()
    state.youtube_relay = relay_sentinel
    tracked = _FakeTrackedProc()
    state.active_subprocesses = {tracked}

    # Must RETURN (not raise): the lifespan awaits framework_task on shutdown.
    await asyncio.wait_for(run_framework_loop_async(uuid.uuid4()), timeout=10.0)

    assert state.is_running is False, "/api/health must stop claiming a live framework"
    assert state.shutdown_event.is_set() is False, "a musical startup failure must not run the process kill switch"
    assert poison_checker.empty(), "audience audio clients must not be poisoned by a startup failure"
    assert state.is_generating is True, "is_generating is user intent, not liveness — REL-19 must not touch it"
    assert state.youtube_relay is relay_sentinel, "the relay must be untouched"
    assert tracked.kill_calls == 0, "tracked subprocesses (relay ffmpeg) must not be killed"
    assert tracked in state.active_subprocesses, "tracked subprocesses must stay registered"

    # Mechanism intact: the real kill switch still poisons everything.
    state.trigger_shutdown()
    assert state.shutdown_event.is_set() is True
    assert poison_checker.get_nowait() is None, "trigger_shutdown still poisons audio clients"


async def test_framework_task_done_callback_still_runs_full_cleanup():
    """S2 (decision 12 boundary): a framework task that DIES with an exception
    still runs the D11 full cleanup — only the startup-failure path softened."""
    from app import app_ui

    poison_checker: queue.Queue = queue.Queue()
    state.is_running = True
    state.is_generating = True
    state.shutdown_event.clear()
    state.audio_clients = [poison_checker]

    async def _explode():
        raise RuntimeError("the loop blew up past the B1 watchdog")

    task = asyncio.create_task(_explode())
    task.add_done_callback(app_ui._on_framework_task_done)
    with pytest.raises(RuntimeError):
        await task
    for _ in range(10):
        await asyncio.sleep(0)  # let the done-callback run

    assert state.shutdown_event.is_set() is True, "a task that DIED must still run the full cleanup"
    assert state.is_running is False
    assert poison_checker.get_nowait() is None, "a dead loop's clients are poisoned (unchanged D11 boundary)"


async def test_b6_startup_failure_still_green(monkeypatch):
    """S3 (no-regression guard): the round-3 B6 expectations hold against the
    new startup-failure path (mirrors test_round3_fix_b's B6 test)."""
    built: dict[str, AsyncFrameworkLoop] = {}

    def _boom() -> _FakeMixerWithPosition:
        raise RuntimeError("sounddevice init failed")

    def _factory(session_id: uuid.UUID) -> AsyncFrameworkLoop:
        loop_obj = AsyncFrameworkLoop(session_id, mixer_factory=_boom)
        built["loop"] = loop_obj
        return loop_obj

    monkeypatch.setattr(loop_orchestrator, "AsyncFrameworkLoop", _factory)
    state.is_running = True
    state.shutdown_event.clear()

    await asyncio.wait_for(run_framework_loop_async(uuid.uuid4()), timeout=10.0)

    assert built["loop"].running is False
    assert built["loop"].loop_task is None, "no run-loop task may exist after a failed start"
    assert state.is_running is False, "/api/health must stop reporting a live framework"
