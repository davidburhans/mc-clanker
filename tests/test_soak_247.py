"""rel-soak P1-P3 — the audit's 24/7 gate for the loop/mixer family (U15).

Audit §Soak-test spec, points 1-3 (docs/reliability_audit.md): (1) 24 h-equivalent
fault-injection soak — LLM outage, PG restart, worker-down window, stuck
generation; loop task alive, ``loop_count`` monotonic, ``len(llm_interaction_buffer)``
bounded, ``asyncio.all_tasks()`` count flat, pending bounded, RSS plateaus.
(2) Mixer fault survival — raise from ``_callback`` every k-th tick: thread
survives (``state.mixer_thread`` seam), silence bounded. (3) Reset-then-restart —
the boundary re-primes and a transition fires, repeated for TWO cycles.

The virtual clock advances in virtual seconds (backoff ladder, job-wait timeout
and flush threshold accumulate at face value; zero wall cost) — only the
driver's ``asyncio.wait_for`` watchdog runs on real time: a wedge is a soak
failure. Scale-to-soak module-attr seams: ``AUDIT_FLUSH_THRESHOLD_ROWS`` lowered
so the REAL P12 trigger fires dozens of times; the fast profile lowers
``JOB_WAIT_TIMEOUT_SECONDS`` (full keeps the audit-literal 600 s). Opt-in run:
``SOAK=1 .venv/bin/python -m pytest -m soak tests/test_soak_247.py -q``.
"""

import asyncio
import time
import uuid
from typing import Any

import numpy as np
import pytest
from soak_helpers import (
    SoakParams,
    VirtualClock,
    make_isolated_state_fixture,
    read_rss_factory,
    soak_gate,
    soak_params,
)
from test_loop_robustness import _FakeMixerWithPosition

from app.framework import loop_steps
from app.framework.framework_state import state
from app.framework.loop_orchestrator import AsyncFrameworkLoop

pytestmark = soak_gate()
_isolated_soak_state = make_isolated_state_fixture()

_SAMPLE_RATE = 44_100
_AWAIT_SLICE_S = 20.0  # the job-wait burn sleeps in <= this virtual slice so the sampler sees window crossings


def _soak_audio(seconds: float = 1.0, channels: int = 2) -> np.ndarray:
    """Non-silent block shaped for the real Mixer under test (all-ones → non-silent broadcasts)."""
    return np.ones((int(seconds * _SAMPLE_RATE), channels), dtype=np.float32) * 0.25


class _SoakMixer(_FakeMixerWithPosition):
    """``_FakeMixerWithPosition`` with the real Mixer's bounded staging slot.

    The parent fake appends every staged loop to ``self.staged`` (fine for
    single-commit drives), so a multi-loop soak reads as unbounded RSS growth:
    each staged loop pins a ~5.6 MB tiled audio array. The real ``Mixer``
    holds ONE next-loop slot replaced per transition — mirror that.
    """

    def set_next_loop(self, tracks, next_loop_duration_samples: int = 0, loop_idx: int = 0) -> None:
        self.staged = [{"tracks": list(tracks), "loop_idx": loop_idx}]


class _SoakConductor:
    """ConductorPort fake with a virtual-time LLM-outage window.

    Burns the profile's virtual LLM latency FIRST (a real outage is a slow
    failure, and the latency paces every fresh iteration onto the fault
    windows deterministically), then raises inside the outage window.
    Successful calls return one ``add`` action with a UNIQUE per-call
    sub_family so every loop is a fresh cache miss that actually submits (a
    repeated prompt would cache-hit and silently disarm the fault schedule).
    """

    def __init__(self, params: SoakParams, clock: VirtualClock):
        self.params = params
        self.clock = clock
        self.calls = 0
        self.outage_raises = 0
        self._loop: AsyncFrameworkLoop | None = None

    def bind(self, loop: AsyncFrameworkLoop) -> None:
        self._loop = loop

    def _stop(self) -> None:
        if self._loop is not None:
            self._loop.running = False
        state.is_running = False

    def _in_outage(self) -> bool:
        start, end = self.params.p1_llm_outage
        return start <= self.clock.elapsed < end

    async def get_next_state_async(self, **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(self.params.p1_llm_latency_s)
        if self._in_outage():
            self.outage_raises += 1
            raise RuntimeError("llm unavailable (soak window)")
        self.calls += 1
        if self.clock.elapsed >= self.params.p1_virtual_seconds:
            self._stop()
        sub_family = f"Soak Pad {self.calls}"
        return {
            "master_bpm": 120,
            "master_key": "A minor",
            "name": "Conductor Live",
            "reasoning": f"soak decision {self.calls}",
            "actions": [
                {
                    "action_type": "add",
                    "instrument": sub_family,
                    "sub_family": sub_family,
                    "major_family": "Synth",
                    "model_id": "foundation-1",
                }
            ],
        }


class _SoakJobQueue:
    """JobQueuePort fake with the soak's fault schedule (plan §2.2).

    ``submit`` raises during PG-restart windows (streak → the rel-18 conductor
    skip); ``await_jobs`` burns its full timeout and returns ``{id: None}``
    during the worker-down window (the REL-12a abandon path fires); the one
    job submitted at/after ``p1_stuck_job_at`` never completes; ``abandon_jobs``
    raises during PG windows (the "PG restart overlapping the abandon" corner);
    ``pending_depth`` raises there too (the P7 throttle fail-opens, the probe
    keeps the skip).
    """

    def __init__(self, params: SoakParams, clock: VirtualClock):
        self.params = params
        self.clock = clock
        self.pending: set[uuid.UUID] = set()
        self.stuck_id: uuid.UUID | None = None
        self.submit_calls = 0
        self.submit_failures = 0
        self.worker_down_batches = 0
        self.stuck_burns = 0
        self.abandon_calls = 0
        self.abandon_failures = 0
        self.depth_calls = 0
        self.depth_failures = 0

    def _in_pg_window(self) -> bool:
        for window in (self.params.p1_pg_restart_1, self.params.p1_pg_restart_2):
            if window is not None and window[0] <= self.clock.elapsed < window[1]:
                return True
        return False

    def _in_worker_down(self) -> bool:
        start, end = self.params.p1_worker_down
        return start <= self.clock.elapsed < end

    async def submit(self, **kwargs: Any) -> uuid.UUID:
        self.submit_calls += 1
        if self._in_pg_window():
            self.submit_failures += 1
            raise RuntimeError("pg restarting (soak window)")
        job_id = uuid.uuid4()
        self.pending.add(job_id)
        if self.stuck_id is None and self.clock.elapsed >= self.params.p1_stuck_job_at:
            self.stuck_id = job_id
        return job_id

    async def await_jobs(self, job_ids: list[uuid.UUID], timeout: float = 120.0) -> dict[uuid.UUID, str | None]:
        results: dict[uuid.UUID, str | None] = {job_id: None for job_id in job_ids}
        stuck_here = self.stuck_id is not None and self.stuck_id in job_ids
        entry_down = self._in_worker_down()
        if entry_down:
            self.worker_down_batches += 1
        elif stuck_here:
            self.stuck_burns += 1
        else:
            for job_id in job_ids:
                results[job_id] = f"audio/{job_id}.aac"
                self.pending.discard(job_id)  # a completed row is no longer 'pending'
        # A worker-down or stuck batch burns the whole timeout; slice it so the
        # virtual clock (and the sampler/stop check) advances across the burn.
        remaining = timeout if (entry_down or stuck_here) else self.params.p1_await_latency_s
        while remaining > 0:
            step = min(_AWAIT_SLICE_S, remaining)
            await asyncio.sleep(step)
            remaining -= step
        return results

    async def abandon_jobs(self, job_ids: list[uuid.UUID]) -> int:
        self.abandon_calls += 1
        if self._in_pg_window():
            self.abandon_failures += 1
            raise RuntimeError("pg restarting (soak window)")
        for job_id in job_ids:
            self.pending.discard(job_id)
        return len(job_ids)

    async def pending_depth(self) -> int:
        self.depth_calls += 1
        if self._in_pg_window():
            self.depth_failures += 1
            raise RuntimeError("pg restarting (soak window)")
        return len(self.pending)


class _SoakAudioFetch:
    """AudioFetchPort fake: every job 'completed' on the worker returns audio."""

    async def fetch(self, audio_path: str) -> np.ndarray | None:
        return _soak_audio(0.01)


class _FlushingAuditPort:
    """AuditSinkPort fake mirroring the real adapter contract exactly (plan decision 5).

    ``append_loop`` appends into the REAL ``state`` buffers (the P12 threshold
    trigger reads them); ``flush`` copies-under-lock → clears → 'inserts' into
    memory, and during PG windows raises and RE-PREPENDS (invariant 4:
    retain-over-drop — ``flush_recording_buffers`` semantics). The fake asserts
    internally that nothing is ever silently dropped (accounting checked by P1).

    ``append_loop`` also burns the profile's per-iteration virtual FLOOR: it is
    the one seam EVERY iteration passes through, so it guarantees the clock
    advances even when every other phase is a fake-instant (conductor skipped,
    stems cache-hit, P13 instant on the 0.0-position mixer fake) — without it
    a fault window becomes a zero-virtual-time hot spin.
    """

    def __init__(self, params: SoakParams, clock: VirtualClock):
        self.params = params
        self.clock = clock
        self.append_calls = 0
        self.total_interactions = 0
        self.total_actions = 0
        self.persisted_interactions = 0
        self.persisted_actions = 0
        self.flush_calls = 0
        self.flush_failures = 0
        self.responses: list[tuple[float, str]] = []

    def _in_pg_window(self) -> bool:
        for window in (self.params.p1_pg_restart_1, self.params.p1_pg_restart_2):
            if window is not None and window[0] <= self.clock.elapsed < window[1]:
                return True
        return False

    async def append_loop(self, conductor_response: dict[str, Any], active_stems: list, loop_idx: int) -> None:
        self.append_calls += 1
        await asyncio.sleep(self.params.p1_iteration_floor_s)  # pacing floor: see docstring
        actions = conductor_response.get("actions", []) or []
        async with state.lock:
            if state.current_show_id is None:
                return
            self.total_interactions += 1
            self.total_actions += len(actions)
            state.llm_interaction_buffer.append({"loop_index": loop_idx})
            for _action in actions:
                state.action_buffer.append({"loop_index": loop_idx})
        self.responses.append((self.clock.elapsed, conductor_response.get("name", "")))

    async def flush(self) -> None:
        self.flush_calls += 1
        pg_down = self._in_pg_window()
        async with state.lock:
            llm_rows = state.llm_interaction_buffer[:]
            action_rows = state.action_buffer[:]
            state.llm_interaction_buffer.clear()
            state.action_buffer.clear()
        if pg_down:
            self.flush_failures += 1
            async with state.lock:
                state.llm_interaction_buffer = llm_rows + state.llm_interaction_buffer
                state.action_buffer = action_rows + state.action_buffer
            raise RuntimeError("pg restarting (soak window)")
        self.persisted_interactions += len(llm_rows)
        self.persisted_actions += len(action_rows)


def _fresh_path_pregen_clearer(loop: AsyncFrameworkLoop):
    """Replace the background pre-gen spawn (the test_loop_robustness pattern):
    clear the fabricated results so every iteration runs the FRESH conductor
    path — the fault schedule (outage windows, submit streak, job waits) is
    what P1 soaks; pregen gating is pinned by test_loop_robustness."""

    async def _fake_pregen(for_loop_idx: int, snapshot: dict[str, Any]) -> None:
        loop._pregen_results = None
        loop._pregen_done.set()

    return _fake_pregen


# --------------------------------------------------------------------------- #
# P1 — 24 h-equivalent fault-injection soak
# --------------------------------------------------------------------------- #


async def test_p1_fault_injection_soak_24h_equivalent(monkeypatch):
    """Audit point 1: a full virtual day of scheduled faults; the set keeps playing.

    Sampling happens at the virtual clock (every sleep the loop takes) — the
    real run is milliseconds, so a wall-tick grid would miss it entirely; the
    watchdog stays real-time (a wedge is a finding).
    """
    params = soak_params()
    clock = VirtualClock().install(monkeypatch)
    # Scale the REL-04 flush threshold to soak scale so the REAL P12 trigger
    # fires dozens of times (module attr = the documented test seam). The
    # audit's bound keeps its shape: peak <= threshold + one outage window.
    flush_threshold = 12
    monkeypatch.setattr(loop_steps, "AUDIT_FLUSH_THRESHOLD_ROWS", flush_threshold)
    if params.profile == "fast":
        # Fast profile only: fit the worker-down/stuck burns into 600 virtual s.
        # Full keeps the audit-literal 600 s generation window.
        monkeypatch.setattr(loop_steps, "JOB_WAIT_TIMEOUT_SECONDS", 120.0)
    _read_rss = read_rss_factory()

    mixer = _SoakMixer()
    # REL-02 note (test_loop_robustness._drive_loop): pre-establish the mixer
    # boundary or the re-prime fabricates results that steal audit appends.
    mixer.current_loop_end_sample = 1
    conductor = _SoakConductor(params, clock)
    jobs = _SoakJobQueue(params, clock)
    audit = _FlushingAuditPort(params, clock)
    loop = AsyncFrameworkLoop(
        uuid.uuid4(),
        conductor=conductor,
        mixer_factory=lambda: mixer,
        audio=_SoakAudioFetch(),
        jobs=jobs,
        audit=audit,
    )
    loop.mixer = mixer  # type: ignore[assignment]
    conductor.bind(loop)
    loop._pre_generate_next_loop = _fresh_path_pregen_clearer(loop)  # type: ignore[method-assign]

    state.current_show_id = 1
    state.current_show_start_time = time.time()
    loop.running = True

    samples: list[dict[str, Any]] = []
    holder: dict[str, asyncio.Task | None] = {"task": None}

    def _sampler(_delay: float) -> None:
        task = holder["task"]
        samples.append(
            {
                "t": clock.elapsed,
                "loops": state.loop_count,
                "tasks": len(asyncio.all_tasks()),
                "llm_buf": len(state.llm_interaction_buffer),
                "buf_total": len(state.llm_interaction_buffer) + len(state.action_buffer),
                "pending": len(jobs.pending),
                "done": task is None or task.done(),
                "rss": _read_rss(),
            }
        )
        if clock.elapsed >= params.p1_virtual_seconds or state.loop_count >= params.p1_loops_cap:
            loop.running = False
            state.is_running = False

    clock.on_sleep = _sampler

    async def _run_soak() -> None:
        task = asyncio.create_task(loop._run_loop())
        holder["task"] = task
        await asyncio.wait_for(task, timeout=params.p1_wall_budget)

    try:
        await asyncio.wait_for(_run_soak(), timeout=params.p1_wall_budget + 5.0)
    except (TimeoutError, asyncio.TimeoutError):
        pytest.fail(
            f"soak loop wedged at virtual t={clock.elapsed:.0f}s "
            f"(last sample: {samples[-1] if samples else None})"
        )

    # --- the faults must have FIRED (a soak that never hits its windows proves nothing)
    assert conductor.outage_raises > 0, "the scheduled LLM outage window never fired"
    assert jobs.submit_failures >= 3, (
        f"the PG-restart window must fail >= 3 consecutive submits (rel-18 skip gate), got {jobs.submit_failures}"
    )
    assert jobs.worker_down_batches > 0, "the worker-down window never fired"
    assert jobs.stuck_burns >= 1, "the scheduled stuck generation never burned its batch wait"
    assert jobs.abandon_calls > 0, "the loop's REL-12a abandon path never ran"
    assert audit.flush_calls > 0, "the P12 threshold flush never ran"

    # --- loop task alive: every sample before the last saw a live task (scheduled stop only)
    assert samples, "the virtual clock never sampled"
    assert all(not sample["done"] for sample in samples[:-1]), (
        "the loop task died before the scheduled stop "
        f"(first done sample: {next(s for s in samples if s['done'])})"
    )

    # --- loop_count monotonic and growing (audit: the set keeps PLAYING, not merely alive)
    loop_counts = [sample["loops"] for sample in samples]
    assert loop_counts == sorted(loop_counts), "loop_count regressed mid-soak"
    assert state.loop_count >= params.p1_min_loops, (
        f"only {state.loop_count} loops committed in {clock.elapsed:.0f} virtual s "
        f"(expected >= {params.p1_min_loops})"
    )

    # --- len(llm_interaction_buffer) bounded; drains back under the threshold post-window
    # Envelope (plan §2.2): threshold + one outage window of re-prepended rows
    # (the full profile's 600 virtual-s PG window accumulates ~100 fallback loops).
    peaks = [sample["llm_buf"] for sample in samples]
    assert max(peaks) <= flush_threshold + 200, (
        f"audit buffer peaked at {max(peaks)} (threshold {flush_threshold} + one outage window of re-prepends)"
    )
    assert audit.flush_failures > 0, "the flush failure path (re-prepend, invariant 4) never fired"
    drain_windows = [w for w in (params.p1_pg_restart_1, params.p1_pg_restart_2) if w is not None]
    for i, window in enumerate(drain_windows):
        # Scope the drain slice to BEFORE the next PG window opens (a later
        # window legitimately re-accumulates re-prepended rows).
        upper = drain_windows[i + 1][0] if i + 1 < len(drain_windows) else float("inf")
        post = [sample for sample in samples if window[1] + 60.0 < sample["t"] < upper]
        assert post, f"no samples after the PG window {window} closed"
        assert all(sample["llm_buf"] <= flush_threshold for sample in post), (
            f"audit buffer never drained after the PG window {window} (stuck at {max(s['llm_buf'] for s in post)})"
        )

    # --- invariant 4: flush never silently drops (persisted + buffered == appended)
    assert audit.persisted_interactions + len(state.llm_interaction_buffer) == audit.total_interactions
    assert audit.persisted_actions + len(state.action_buffer) == audit.total_actions

    # --- asyncio.all_tasks() count flat (no per-iteration task leakage)
    task_counts = [sample["tasks"] for sample in samples]
    assert max(task_counts) - min(task_counts) <= 4, (
        f"task count drifted: min={min(task_counts)} max={max(task_counts)}"
    )

    # --- generator_jobs pending count bounded (far under JOB_PENDING_DEPTH_LIMIT)
    pending_counts = [sample["pending"] for sample in samples]
    assert max(pending_counts) <= 16, f"pending backlog peaked at {max(pending_counts)}"

    # --- RSS plateaus (only when psutil is importable — decision 6). The audit
    # targets LEAK-class growth: compare the final quarter of samples against
    # the post-warm-up second quarter (the first quarter is the allocator's
    # ramp to the per-loop churn high-water); a per-loop leak blows the bound.
    rss_samples = [sample["rss"] for sample in samples if sample["rss"] is not None]
    if len(rss_samples) >= 8:
        quarter = len(rss_samples) // 4
        warm = sum(rss_samples[quarter : 2 * quarter]) / quarter
        tail = sum(rss_samples[-quarter:]) / quarter
        assert tail <= warm * 1.10, (
            f"RSS did not plateau: warm={warm/1048576:.0f}MB tail={tail/1048576:.0f}MB (bound: warm x 1.10)"
        )

    # --- rel-18: the conductor skip engaged during the PG window (fallback appended
    #     inside the window without a conductor call) and the set recovered after it
    assert any(
        elapsed >= params.p1_pg_restart_1[0] and name == "Fallback State" for elapsed, name in audit.responses
    ), "no Fallback State was committed inside the PG-restart window (conductor skip never engaged)"
    assert any(
        elapsed > params.p1_pg_restart_1[1] and name == "Conductor Live" for elapsed, name in audit.responses
    ), "the conductor never resumed after the PG-restart window"
