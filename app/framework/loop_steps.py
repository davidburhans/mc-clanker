"""The ``_step_*`` decomposition of ``AsyncFrameworkLoop._run_loop`` (Phase B).

Extracted from ``loop_orchestrator.py`` so the orchestrator file stays under the
project's 500-LOC rule: the 14 per-phase ``_step_*`` methods (each ≤50 LOC,
``_step_commit_state`` the documented ~85-LOC single-lock exception) live here as
a mixin, while the orchestrator keeps the lifecycle (``__init__``/``start``/``stop``),
the thin ``_run_loop`` driver, and the adapter delegates.

The mixin references ``self.*`` instance attributes set in
``AsyncFrameworkLoop.__init__`` (``mixer``/``stem_cache``/``_loop_idx``/
``_pregen_*``/``conductor``/...) and the adapter delegates (``_build_prompt`` /
``_submit_job`` / ``_await_jobs`` / ``_fetch_audio`` / ``_append_loop_audit`` /
``_pre_generate_next_loop``) defined on the orchestrator — all resolved at runtime
via MRO, so ``patch.object(loop, '_submit_job')`` keeps working unchanged.

SAFETY INVARIANT: the ``async with state.lock:`` blocks (the refactor's #2 risk —
no I/O inside the lock) live in the ``_step_*`` methods HERE. The source-level guard
``test_no_io_inside_state_lock_in_orchestrator`` is scoped to scan BOTH this file
and ``loop_orchestrator.py``; do not weaken that scope.

Result types (``_StepResult``/``_CommitResult``/``_PregenDecision``/``_StateSnapshot``/``_SubmitJobsResult``)
are defined here because they are produced and consumed by the ``_step_*`` methods
and imported back into the orchestrator's driver.
"""

from __future__ import annotations

import asyncio
import enum
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from app.framework.audit_recording import _audit_applied_actions
from app.framework.conductor_interaction import (
    build_fallback_response,
    format_action_log,
    load_available_models,
    process_actions,
)
from app.framework.domain_audio import make_cache_key, tile_to_loop
from app.framework.framework_state import state

if TYPE_CHECKING:
    import uuid

    import numpy as np

    from app.framework.framework_mixer import Mixer
    from app.framework.ports import ConductorPort

# Backoff between loop retries after a transient body error (review B1 watchdog).
# Kept short so the set recovers quickly; overridable by tests / config.
LOOP_RETRY_BACKOFF_SECONDS = 2.0

# REL-18 (U12): the B1 retry backoff escalates exponentially with a cap and
# uniform jitter so a persistent outage backs off instead of hot-looping (and
# synchronized instances don't stampede in lockstep). The FIRST failure still
# waits the flat base — a single transient blip keeps today's fast retry.
LOOP_RETRY_BACKOFF_MAX_SECONDS = 30.0
LOOP_RETRY_BACKOFF_JITTER_FRACTION = 0.25

# REL-18 (U12): once this many consecutive job submits have failed, the loop
# presumes a DB outage and skips the conductor call (audit finding: the full
# LLM call was repeated every cycle while every submit failed). While skipped,
# each pass probes the queue once and resumes the conductor within one loop of
# the DB returning. Module attr so tests monkeypatch it.
LOOP_CONDUCTOR_SKIP_AFTER_SUBMIT_FAILURES = 3


def loop_retry_backoff_delay(consecutive_failures: int) -> float:
    """B1 watchdog sleep for the n-th consecutive failed iteration (REL-18).

    min(cap, base * 2**(n-1)) with uniform ±JITTER_FRACTION jitter; n <= 1
    returns the un-jittered base so the first (common, transient) failure
    keeps the flat 2 s retry. Example::

        loop_retry_backoff_delay(1)  # -> 2.0 exactly
    """
    if consecutive_failures <= 1:
        return LOOP_RETRY_BACKOFF_SECONDS
    exponential = min(
        LOOP_RETRY_BACKOFF_MAX_SECONDS,
        LOOP_RETRY_BACKOFF_SECONDS * 2 ** (consecutive_failures - 1),
    )
    span = exponential * LOOP_RETRY_BACKOFF_JITTER_FRACTION
    return exponential + random.uniform(-span, span)

# Round-3 fix B3 (review 03/Q1): ONE worker drains a 4-6 stem batch strictly
# SEQUENTIALLY at 5-30 s/stem (30-90 s for the first job while the weights load),
# so the old flat 120 s batch wait lost every job queued behind a slow one. A
# lost job meant silence for that stem AND an unconditional re-submit on every
# later loop. 10 minutes covers a full cold batch; the wait stays bounded.
JOB_WAIT_TIMEOUT_SECONDS = 600.0

# Round-3 fix B3: the waiter's final status check happens AT the deadline, so a
# completion landing a fraction of a second late used to be lost forever.
# One extra, much shorter pass recovers those late completions; genuinely
# failed jobs return immediately, so the grace only ever costs still-pending work.
JOB_LATE_COMPLETION_GRACE_SECONDS = 30.0

# REL-12c: submission backpressure bound. Steady state peaks at ONE uncached
# batch (<= 6 jobs — the single worker drains sequentially at 5-30 s/stem), so
# a depth over 10x a full batch means the drain rate has fallen behind by
# design; we skip-and-log submissions for the cycle (prompts stay cache-missed
# and retry next loop) instead of growing the queue without bound. Module attr
# so tests monkeypatch it (JOB_WAIT_TIMEOUT_SECONDS precedent).
JOB_PENDING_DEPTH_LIMIT = 64

# Round-3 fix B5 (review 08/6): an explicit JSON ``null`` for master_bpm /
# master_key slipped past ``.get(default)`` (the default only fires on a MISSING
# key) and poisoned state.current_bpm/current_key, every generation prompt
# ("… None BPM …"), the stem cache key and the persisted job row.
BPM_PLAUSIBLE_MIN = 40
BPM_PLAUSIBLE_MAX = 300
FALLBACK_MASTER_BPM = 128
FALLBACK_MASTER_KEY = "A minor"

# Round-3 fix B2: the mixer fires the loop transition with a ~1 s lookahead, so
# staged audio is still live while the boundary is further away than this. Below
# it the P13 break must win, exactly as before this fix.
BOUNDARY_BREAK_SECONDS = 0.5

# REL-06 (audit High): a retained stem is cache-HIT every loop, so its TTL
# must be measured from the last HIT, not the initial fetch — otherwise the
# "core groove" the conductor is told to retain ages out and is regenerated
# every 300 s (audible churn + needless GPU), forever.
STEM_CACHE_TTL_SECONDS = 300.0

# REL-06 entry cap: TTL bounds age, not count — a retain-all fallback (LLM
# outage) or fast prompt churn adds 4-6 entries/loop for a full TTL window.
# ~5-6 MB per 8-bar 44.1 kHz stereo stem => ~180 MB worst case (sibling
# state.cache_stem LRU caps at 16).
STEM_CACHE_MAX_ENTRIES = 32

# REL-04: the in-RAM audit buffers must not grow for the whole show — past this
# many buffered LLMInteraction rows, P12 flushes via the audit port (the module
# flush serializes on _flush_lock and re-queues on failure). At ~1 interaction
# per loop this flushes every ~200 loops: crash loss bounded to the unflushed
# tail, RAM bounded at ~1 MB. Module attr so tests can monkeypatch it.
AUDIT_FLUSH_THRESHOLD_ROWS = 200


def sanitize_master_bpm(candidate: Any, fallback: int | None) -> int:
    """Coalesce a missing/null/out-of-range conductor BPM to a usable value.

    Example::

        sanitize_master_bpm({"master_bpm": None}.get("master_bpm"), 128)  # -> 128
    """
    if not isinstance(candidate, bool) and isinstance(candidate, int):
        if BPM_PLAUSIBLE_MIN <= candidate <= BPM_PLAUSIBLE_MAX:
            return candidate
    if not isinstance(fallback, bool) and isinstance(fallback, int):
        if BPM_PLAUSIBLE_MIN <= fallback <= BPM_PLAUSIBLE_MAX:
            return fallback
    return FALLBACK_MASTER_BPM


def sanitize_master_key(candidate: Any, fallback: str | None) -> str:
    """Coalesce a missing/null/blank conductor key to a usable non-empty string."""
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()
    return FALLBACK_MASTER_KEY


async def reawait_late_job_completions(
    await_jobs: Callable[[list[Any], float], Awaitable[dict[Any, str | None]]],
    job_ids: list[Any],
    results: dict[Any, str | None],
    *,
    label: str = "",
    grace_seconds: float = JOB_LATE_COMPLETION_GRACE_SECONDS,
) -> dict[Any, str | None]:
    """B3: one bounded extra wait for jobs the first batch pass reported as None.

    Shared by the foreground path (``_step_await_jobs_fetch``) and the background
    path (``pregeneration.run_pregeneration``) — a completion landing just after
    the batch deadline used to be a permanent ``None``: silence for that stem plus
    an identical re-submit on every following loop. Failed jobs report ``None``
    immediately, so the grace only ever delays genuinely pending work.

    Example::

        results = await reawait_late_job_completions(self._await_jobs, job_ids, results)
    """
    missing = [job_id for job_id in job_ids if not results.get(job_id)]
    if not missing:
        return results

    print(
        f"[AsyncLoop-{label}] {len(missing)} job(s) still unfinished after the batch wait; "
        f"waiting a further {grace_seconds:.0f}s grace..."
    )
    late_results = await await_jobs(missing, timeout=grace_seconds)
    merged = dict(results)
    for job_id, audio_path in late_results.items():
        if audio_path:
            merged[job_id] = audio_path
    return merged


def _collect_uncached_stems(
    stems: list[dict[str, Any]],
    current_bpm: int,
    current_key: str,
    stem_cache: dict[str, dict],
) -> list[tuple[int, dict[str, Any], str]]:
    """P7 phase 1 (pure): scan the cache, return uncached ``(idx, stem, cache_key)``.

    Cache HITS refresh ``last_used`` in place (REL-06: the TTL clock restarts
    on every hit — a retained stem must not age out while in active use) and
    are excluded from submission.
    """
    uncached: list[tuple[int, dict[str, Any], str]] = []
    for i, t in enumerate(stems):
        prompt = t["prompt"]
        track_bars = t["bars"]
        m_id = t.get("model_id")
        cache_key = make_cache_key(m_id, prompt, current_bpm, current_key, track_bars)

        if cache_key in stem_cache:
            print(f"Cache HIT: '{prompt}'")
            stem_cache[cache_key]["last_used"] = time.time()
            continue  # Already have audio

        uncached.append((i, t, cache_key))
    return uncached


class _StepResult(enum.Enum):
    """Outer-while control-flow signal for _step_* methods (brief-05 decomp).

    Only 3 phases emit non-PROCEED: P1 shutdown (EXIT_LOOP), P1 not-generating
    (RESTART_ITER), P2 will_call_llm False (RESTART_ITER). Every other
    break/continue is local to its _step_* method.
    """

    PROCEED = enum.auto()
    RESTART_ITER = enum.auto()  # -> `continue` the outer while-loop
    EXIT_LOOP = enum.auto()  # -> `break` the outer while-loop


@dataclass
class _CommitResult:
    """P11 atomic-commit outputs threaded into _step_post_commit (P12)."""

    needs_pregen: bool
    needs_initial_record: bool
    rec_stems: list
    rec_set_name: str
    rec_reasoning: str
    state_snapshot: dict


class _PregenDecision(NamedTuple):
    """P2 decision outputs (named, not a brittle positional 5-tuple unpack)."""

    result: _StepResult
    pregen_ready: bool
    conductor_response: dict | None
    prepared_tracks: list
    loop_duration_samples: int


class _SubmitJobsResult(NamedTuple):
    """P7 output: jobs submitted + stem indexes skipped by the REL-12c throttle."""

    pending_jobs: list  # [(job_id, original_index, cache_key)]
    skipped_idxs: list[int]


class _StateSnapshot(NamedTuple):
    """P3 conductor-prompt snapshot (named, not a brittle positional 10-tuple)."""

    bpm_override: int | None
    key_override: str | None
    current_bpm: int
    current_key: str
    active_stems: list
    user_override: str
    available_instruments: list
    stem_history: list
    llm_config: dict
    available_models: list


class _LoopSteps:
    """Mixin: the 14 ``_step_*`` phases of ``_run_loop`` (brief-05 decomposition).

    Mixed into ``AsyncFrameworkLoop`` so the orchestrator file stays small while
    every phase keeps its ``self`` binding (instance attrs + adapter delegates
    resolve through MRO). Method order matches the loop's phase order P1→P13.
    """

    # Host contract: ``AsyncFrameworkLoop`` (loop_orchestrator.py) provides these
    # instance attributes in ``__init__`` and these adapter delegates. Declared
    # here so the mixin type-checks standalone; the host OVERRIDES the delegate
    # stubs (most-derived in MRO), so the stubs never run at runtime.
    running: bool
    mixer: Mixer | None
    session_id: uuid.UUID
    conductor: ConductorPort
    stem_cache: dict[str, dict]
    _loop_idx: int  # set by _run_loop / _step_wait_for_start on the orchestrator
    _pregen_results: dict[str, Any] | None
    _pregen_task: asyncio.Task | None
    _pregen_done: asyncio.Event
    # Round-3 fix B2: loop index whose audio P10 handed to Mixer.set_next_loop and
    # which the mixer has NOT yet consumed at its boundary (0 = nothing staged).
    _staged_loop_idx: int
    # REL-18 (U12): consecutive job-submit failures (drives the conductor skip);
    # owned/mutated by AsyncFrameworkLoop.__init__/_submit_job.
    _consecutive_submit_failures: int

    def _build_prompt(self, track: dict, key: str, bpm: int) -> str:
        """Delegate provided by ``AsyncFrameworkLoop``."""
        raise NotImplementedError

    async def _submit_job(
        self,
        session_id: uuid.UUID,
        instrument: str,
        prompt: str,
        major_family: str,
        model_id: str,
        key: str,
        bpm: int,
        timbre_tags: list[str],
        bars: int,
    ) -> uuid.UUID:
        """Delegate provided by ``AsyncFrameworkLoop``."""
        raise NotImplementedError

    async def _await_jobs(
        self,
        job_ids: list[uuid.UUID],
        timeout: float = 120.0,
    ) -> dict[uuid.UUID, str | None]:
        """Delegate provided by ``AsyncFrameworkLoop`` (U4)."""
        raise NotImplementedError

    async def _abandon_jobs(self, job_ids: list[Any]) -> int:
        """Delegate provided by ``AsyncFrameworkLoop`` (U6/REL-12a)."""
        raise NotImplementedError

    async def _pending_depth(self) -> int:
        """Delegate provided by ``AsyncFrameworkLoop`` (U6/REL-12c)."""
        raise NotImplementedError

    async def _fetch_audio(self, audio_path: str) -> np.ndarray | None:
        """Delegate provided by ``AsyncFrameworkLoop``."""
        raise NotImplementedError

    async def _append_loop_audit(self, conductor_response, active_stems, loop_idx) -> None:
        """Delegate provided by ``AsyncFrameworkLoop``."""
        raise NotImplementedError

    async def _pre_generate_next_loop(self, for_loop_idx: int, snapshot: dict[str, Any]) -> None:
        """Delegate provided by ``AsyncFrameworkLoop``."""
        raise NotImplementedError

    async def _step_wait_for_start(self) -> _StepResult:
        """P1: wait until generation starts, then advance the loop counter.

        Returns EXIT_LOOP on shutdown, RESTART_ITER if generation stopped before
        the LLM call, else PROCEED (after incrementing ``self._loop_idx``). The
        counter only advances on a real PROCEED iteration — it is placed AFTER
        the early returns (matches original placement; misplacement breaks the
        ``loop_idx == 1`` and pregen-ready gates in production).
        """
        while not state.is_generating and self.running and state.is_running and not state.shutdown_event.is_set():
            await asyncio.sleep(0.5)

        if not self.running or state.shutdown_event.is_set():
            return _StepResult.EXIT_LOOP

        async with state.lock:
            still_generating = state.is_generating

        if not still_generating:
            print(f"[AsyncLoop-{self._loop_idx or 1}] Stop detected before LLM call, returning to wait")
            return _StepResult.RESTART_ITER

        async with state.lock:
            current_gen = state.is_generating
        print(f"[AsyncLoop-{self._loop_idx}] Exited is_generating wait: is_generating={current_gen}")

        self._loop_idx += 1
        print(f"\n[AsyncLoop-{self._loop_idx}] Starting loop...")
        async with state.lock:
            debug_gen = state.is_generating
            debug_run = state.is_running
        print(f"[AsyncLoop-{self._loop_idx}] DEBUG: is_generating={debug_gen}, is_running={debug_run}")
        return _StepResult.PROCEED

    async def _step_pregen_decision(self) -> _PregenDecision:
        """P2: decide fresh-vs-pregenerated and assemble the pregen outputs.

        Returns a ``_PregenDecision``. On RESTART_ITER (will_call_llm is False)
        the rest are zeroed. The pregen branch fills the pregen vars from
        ``_pregen_results``; the fresh branch leaves them for P3-P9.

        Note: P2's ``active_stems`` read is dropped here — it is overwritten by
        ``_step_read_state`` (P3) which reads the identical value (the map's
        shadowing analysis confirmed both reads see the same state.active_stems
        with no mutation between them).
        """
        pregen_ready = (
            self._loop_idx > 1
            and self._pregen_results is not None
            and self._pregen_results.get("loop_idx") == self._loop_idx
        )

        if pregen_ready:
            # Round-3 fix B1(b): the pre-gen branch used to PROCEED without ever
            # re-reading state.is_generating (P1's check is bypassed once a result
            # is queued), so a stale result kept replaying + re-committing loops
            # that a UI stop could not interrupt.
            async with state.lock:
                generating_with_pregen = state.is_generating
            if not generating_with_pregen:
                print(f"[AsyncLoop-{self._loop_idx}] Stop detected with a pre-gen result queued, returning to wait")
                return _PregenDecision(_StepResult.RESTART_ITER, False, None, [], 0)

            assert self._pregen_results is not None  # pregen_ready gate (P2 predicate)
            print(f"[AsyncLoop-{self._loop_idx}] Using pre-generated audio from background task")
            print(
                f"[AsyncLoop-{self._loop_idx}] DEBUG: pregen_results keys = "
                f"{list(self._pregen_results.keys()) if self._pregen_results else None}"
            )
            print(
                f"[AsyncLoop-{self._loop_idx}] DEBUG: mixer.current_sample = "
                f"{self.mixer.current_sample if self.mixer else None}"
            )
            async with state.lock:
                _pregen_bpm = state.current_bpm
                _pregen_key = state.current_key
            conductor_response = {
                "master_bpm": self._pregen_results.get("master_bpm", _pregen_bpm),
                "master_key": self._pregen_results.get("master_key", _pregen_key),
                "name": self._pregen_results.get("set_name", "Unknown Set"),
                "reasoning": self._pregen_results.get("reasoning", "No reasoning provided."),
                "actions": self._pregen_results.get("actions", []),
                # U4/DPO: the exact chat the pregen conductor sent (transport key;
                # absent on fabricated loop-1 results -> legacy context fallback).
                "_request_messages": self._pregen_results.get("_request_messages"),
            }
            prepared_tracks = self._pregen_results["prepared_tracks"]
            loop_duration_samples = self._pregen_results["loop_duration_samples"]
            return _PregenDecision(
                _StepResult.PROCEED,
                pregen_ready,
                conductor_response,
                prepared_tracks,
                loop_duration_samples,
            )

        async with state.lock:
            will_call_llm = state.is_generating
        if will_call_llm:
            print(f"[AsyncLoop-{self._loop_idx}] Requesting track state from LLM Conductor...")
        else:
            print(f"[AsyncLoop-{self._loop_idx}] Skipping LLM call: is_generating={state.is_generating}")
            # Instead of proceeding, go back to waiting.
            return _PregenDecision(_StepResult.RESTART_ITER, pregen_ready, None, [], 0)

        return _PregenDecision(_StepResult.PROCEED, pregen_ready, None, [], 0)

    async def _step_read_state(self) -> _StateSnapshot:
        """P3: reset handling + override apply/clear + conductor-prompt snapshot.

        ONE ``async with state.lock:`` block holds the reset (mixer.clear +
        stem_cache.clear — sync, allowed) and the override apply/clear, then
        captures the snapshot. Returns the snapshot vars + available_models.
        """
        assert self.mixer is not None  # set in start() before _run_loop spawns
        async with state.lock:
            if state.should_reset:
                print("SYSTEM RESET TRIGGERED")
                self.mixer.clear()
                self.stem_cache.clear()
                state.should_reset = False

            # Round-3 fix B4 (review 08/3): the override used to be applied AND
            # cleared here. On the pre-generated path P11 then overwrote
            # current_bpm/current_key with the pre-gen decision's master_bpm /
            # master_key — a decision taken from the PREVIOUS loop's snapshot —
            # so the DJ's tempo/key change was consumed and silently thrown away,
            # and the P11 "apply pending overrides" net was dead because the flag
            # was already None. The override is now only mirrored into this
            # loop's prompt snapshot; the single clearer stays P11.
            bpm_override = state.target_bpm_override
            key_override = state.target_key_override

            current_bpm = sanitize_master_bpm(bpm_override or state.current_bpm, state.current_bpm)
            current_key = sanitize_master_key(key_override or state.current_key, state.current_key)
            active_stems = list(state.active_stems)
            user_override = state.user_override
            available_instruments = list(state.available_instruments)
            stem_history = list(state.stem_history)

            llm_config = {
                "base_url": state.llm_base_url,
                "api_key": state.llm_api_key,
                "model": state.llm_model,
            }

        # Get available models (Phase 4: shared helper).
        available_models = load_available_models()
        return _StateSnapshot(
            bpm_override,
            key_override,
            current_bpm,
            current_key,
            active_stems,
            user_override,
            available_instruments,
            stem_history,
            llm_config,
            available_models,
        )

    async def _step_call_conductor(
        self,
        current_bpm,
        current_key,
        active_stems,
        user_override,
        available_instruments,
        stem_history,
        llm_config,
        available_models,
    ) -> dict:
        """P4: call the LLM conductor (fresh path only).

        The nested try/except swallows LLM errors into a fallback response; it
        is deliberately NOT merged with B1's outer retry try. The skeleton only
        calls this when ``not pregen_ready``.
        """
        # REL-18 (U12): DB presumed down — skip the LLM call (retain-all
        # fallback keeps the set running from cache) and probe for recovery so
        # the conductor resumes within one loop of the queue returning.
        if self._consecutive_submit_failures >= LOOP_CONDUCTOR_SKIP_AFTER_SUBMIT_FAILURES:
            if await self._probe_queue_recovered():
                self._consecutive_submit_failures = 0
            else:
                return build_fallback_response(current_bpm, current_key, active_stems, "job-queue submit outage")
        try:
            conductor_response = await self.conductor.get_next_state_async(
                current_bpm=current_bpm,
                current_key=current_key,
                active_stems=active_stems,
                user_override=user_override,
                available_instruments=available_instruments,
                stem_history=stem_history,
                llm_config=llm_config,
                available_models=available_models,
            )
        except Exception as e:  # noqa: BLE001
            print(f"LLM call failed: {e}")
            conductor_response = build_fallback_response(current_bpm, current_key, active_stems, e)
        return conductor_response

    async def _step_parse_actions(self, conductor_response, active_stems) -> list:
        """P5: dedupe conductor actions + build the last_actions audit log under lock."""
        deduped_tracks = process_actions(conductor_response.get("actions", []), active_stems)

        # Build action log for debugging/auditing (shared shaper, see format_action_log)
        async with state.lock:
            state.last_actions = format_action_log(conductor_response.get("actions", []), active_stems)

        return deduped_tracks

    async def _step_build_next_stems(
        self,
        bpm_override,
        key_override,
        conductor_response,
        current_bpm,
        current_key,
        deduped_tracks,
    ) -> tuple[list, int, str]:
        """P6: write state.next_stems (bpm/key/set_name/reasoning) under lock; capture locals."""
        async with state.lock:
            if bpm_override:
                state.current_bpm = sanitize_master_bpm(bpm_override, current_bpm)
            else:
                # B5: `.get(default)` does NOT fire on a present-but-null key.
                state.current_bpm = sanitize_master_bpm(conductor_response.get("master_bpm"), current_bpm)

            if key_override:
                state.current_key = sanitize_master_key(key_override, current_key)
            else:
                state.current_key = sanitize_master_key(conductor_response.get("master_key"), current_key)

            state.current_set_name = conductor_response.get("name", "Unknown Set")
            state.llm_reasoning = conductor_response.get("reasoning", "No reasoning provided.")

            # Build next_stems with generation info
            state.next_stems = []
            for t in deduped_tracks:
                m_id = t.get("model_id", "foundation-1")
                prompt = self._build_prompt(t, state.current_key, state.current_bpm)
                state.next_stems.append(
                    {
                        "prompt": prompt,
                        "model_id": m_id,
                        "bpm": state.current_bpm,
                        "key": state.current_key,
                        "bars": t.get("bars", 8),
                        "_original_details": t,
                        "_age": t.get("_age", 0),
                    }
                )

            # Capture as locals while we still hold the lock
            local_next_stems = list(state.next_stems)
            local_current_bpm = state.current_bpm
            local_current_key = state.current_key

        return local_next_stems, local_current_bpm, local_current_key

    async def _step_submit_jobs(self, local_next_stems, local_current_bpm, local_current_key) -> _SubmitJobsResult:
        """P7: submit generation jobs for uncached stems (REL-12c: skip-and-log
        the whole phase when the pending backlog exceeds the bound — never
        block; skipped prompts stay cache-missed and retry next loop).

        Two-phase so the depth probe fires ONLY when a submission would occur:
        phase 1 (``_collect_uncached_stems``) is a pure cache scan, so
        retained-stem loops cost zero DB queries.
        """
        uncached = _collect_uncached_stems(local_next_stems, local_current_bpm, local_current_key, self.stem_cache)

        # REL-12c backpressure: over the bound, skip-and-log EVERY submission
        # this cycle (never block / never await a drain). Skipped indexes ride
        # out so P8 can report them "failed" in the applied-actions audit.
        if uncached and await self._queue_backlogged():
            skipped = [i for i, _t, _cache_key in uncached]
            print(
                f"[AsyncLoop-{self._loop_idx}] Pending backlog over {JOB_PENDING_DEPTH_LIMIT}; "
                f"skipping {len(skipped)} submission(s) this cycle"
            )
            return _SubmitJobsResult([], skipped)

        pending_jobs = []  # List of (job_id, original_index, cache_key)
        for i, t, cache_key in uncached:
            orig = t.get("_original_details", {})
            job_id = await self._submit_job(
                session_id=self.session_id,
                instrument=orig.get("sub_family", "Unknown"),
                prompt=t["prompt"],
                major_family=orig.get("major_family"),
                model_id=t.get("model_id"),
                key=local_current_key,
                bpm=local_current_bpm,
                timbre_tags=orig.get("timbre_tags", []),
                bars=t["bars"],
            )
            pending_jobs.append((job_id, i, cache_key))

        return _SubmitJobsResult(pending_jobs, [])

    async def _step_await_jobs_fetch(
        self, pending_jobs, local_next_stems, skipped_idxs: list[int] | None = None
    ) -> dict[int, str]:
        """P8: wait for jobs, fetch audio, populate stem_cache + state.cache_stem.

        B7: fetched audio is routed through ``state.cache_stem`` (under lock) so
        the 16-entry LRU cap is enforced — the background pregen path never
        calls it (brief-01 risk #4 divergence).

        U4 (DPO field audit): returns ``{orig_idx: "generated" | "failed"}``
        for the submitted jobs AND the REL-12c throttle-skipped stems; stems
        absent from the map were cache hits (the applied-actions builder
        defaults them to "cached").
        """
        outcomes: dict[int, str] = {}
        # REL-12c: a throttle-skipped stem reports "failed" — absent from the
        # map the applied-actions audit would default it to "cached" (a lie).
        for idx in skipped_idxs or ():
            outcomes[idx] = "failed"
        if pending_jobs:
            job_ids = [job_id for job_id, _, _ in pending_jobs]
            print(f"[AsyncLoop-{self._loop_idx}] Waiting for {len(job_ids)} jobs to complete...")
            wait_start = time.time()

            results = await self._await_jobs(job_ids, timeout=JOB_WAIT_TIMEOUT_SECONDS)
            results = await self._reawait_late_completions(job_ids, results)

            # REL-12a: the loop has given up on anything still unreported —
            # terminalize the still-pending rows or they are immortal (the
            # worker's FIFO claim would keep generating them; the next loop's
            # cache-miss would resubmit the identical prompt).
            missing = [job_id for job_id in job_ids if not results.get(job_id)]
            await self._abandon_missing_jobs(missing)

            wait_duration = time.time() - wait_start
            print(f"[AsyncLoop-{self._loop_idx}] Jobs completed in {wait_duration:.2f}s")

            # Process results (keyed lookup: a short/None result for one job must
            # never shift the audio of the jobs after it)
            for job_id, orig_idx, cache_key in pending_jobs:
                audio_path = results.get(job_id)
                if audio_path:
                    # Fetch audio from Garage
                    audio_data = await self._fetch_audio(audio_path)
                    if audio_data is not None:
                        self.stem_cache[cache_key] = {"audio_data": audio_data, "last_used": time.time()}
                        async with state.lock:
                            state.cache_stem(local_next_stems[orig_idx]["prompt"], audio_data)
                        outcomes[orig_idx] = "generated"
                    else:
                        outcomes[orig_idx] = "failed"
                else:
                    print(f"Job {job_id} failed or timed out")
                    outcomes[orig_idx] = "failed"
        return outcomes

    async def _abandon_missing_jobs(self, job_ids: list[Any]) -> None:
        """REL-12a: terminal-fail still-pending jobs this loop gave up on.

        Best-effort by design: a failed abandon must never kill the loop —
        the stale-pending reaper (REL-12b) is the backstop.
        """
        if not job_ids:
            return
        try:
            count = await self._abandon_jobs(job_ids)
        except Exception as exc:  # noqa: BLE001 - hygiene, never fatal
            print(f"[AsyncLoop-{self._loop_idx}] abandon_jobs failed: {exc}")
            return
        if count:
            print(f"[AsyncLoop-{self._loop_idx}] Abandoned {count} job(s) (loop_abandoned)")

    async def _queue_backlogged(self) -> bool:
        """REL-12c: is the pending backlog over JOB_PENDING_DEPTH_LIMIT? Fail-open."""
        try:
            depth = await self._pending_depth()
        except Exception as exc:  # noqa: BLE001 - a broken gauge must not stop the set
            print(f"[AsyncLoop-{self._loop_idx}] pending-depth probe failed ({exc}); submitting anyway")
            return False
        return depth > JOB_PENDING_DEPTH_LIMIT

    async def _probe_queue_recovered(self) -> bool:
        """REL-18: one cheap queue round-trip; True resets the submit streak.

        Runs at most once per loop and only while the conductor is being
        skipped — steady state pays nothing.
        """
        try:
            await self._pending_depth()
            return True
        except Exception:  # noqa: BLE001 - a failed probe just keeps the skip
            return False

    async def _reawait_late_completions(
        self,
        job_ids: list[uuid.UUID],
        results: dict[uuid.UUID, str | None],
    ) -> dict[uuid.UUID, str | None]:
        """B3: foreground hook to the shared late-completion grace pass."""
        return await reawait_late_job_completions(self._await_jobs, job_ids, results, label=str(self._loop_idx))

    async def _step_tile_audio(
        self,
        local_next_stems,
        local_current_bpm,
        local_current_key,
        deduped_tracks,
    ) -> tuple[list, int]:
        """P9: tile cached/decoded stem audio out to the loop duration (pure transform)."""
        prepared_tracks, loop_duration_samples = tile_to_loop(
            next_stems=local_next_stems,
            stem_cache=self.stem_cache,
            bpm=local_current_bpm,
            key=local_current_key,
            sample_rate=self.mixer.sample_rate if self.mixer else None,
            deduped_tracks=deduped_tracks,
        )
        return prepared_tracks, loop_duration_samples

    async def _step_append_audit(self, conductor_response, active_stems, next_stems, outcomes) -> None:
        """C1: buffer this loop's conductor decision + actions for the audit trail.

        U4: the post-dedupe enacted stems + per-stem outcome ride the response
        dict under a transport key so the port-level ``_append_loop_audit``
        signature (patched across the test suite) stays unchanged.
        """
        conductor_response["_applied_actions"] = _audit_applied_actions(next_stems, outcomes)
        await self._append_loop_audit(conductor_response, active_stems, self._loop_idx)

    async def _step_commit_to_mixer(
        self,
        pregen_ready,
        prepared_tracks,
        loop_duration_samples,
    ) -> tuple[list, int]:
        """P10: add tracks to mixer at live position (loop 1) or queue via set_next_loop (>1).

        Loop 1 adds at ``mixer.current_sample`` (not 0) so tracks aren't treated
        as past; loop>1 queues without touching the current boundary.
        """
        assert self.mixer is not None  # set in start() before _run_loop spawns
        if pregen_ready:
            assert self._pregen_results is not None  # pregen_ready gate (P2 predicate)
            tracks_to_use = self._pregen_results["prepared_tracks"]
            duration_samples = self._pregen_results["loop_duration_samples"]
        else:
            tracks_to_use = prepared_tracks
            duration_samples = loop_duration_samples

        # REL-02 (audit Critical): a boundary-less mixer (reset via
        # Mixer.clear(), or the no-future-tracks fallback in Mixer._callback)
        # can never consume set_next_loop audio — the transition gate is
        # current_loop_end_sample > 0 — so staging there is permanent silence.
        # Re-enter the loop-1 prime path instead: it re-establishes the
        # boundary now, and _loop_idx 1 gives P11/P12/P13 the correct loop-1
        # semantics (initial record, no pregen spawn, prompt P13 exit).
        # Unlocked read is safe: GIL-atomic public int (sanctioned by
        # test_orchestrator_has_no_private_mixer_reach); clear() runs earlier
        # in this same task, so no interleaving can produce a stale 0 read.
        if self.mixer.current_loop_end_sample <= 0:
            self._loop_idx = 1

        # B2: loop>1 hands its audio to the mixer's single next_loop_audio slot.
        # Remember the index so P13 refuses to return before the boundary consumes
        # it (the next iteration's set_next_loop would otherwise overwrite a loop
        # that never started playing). Loop 1 primes directly: nothing is staged.
        # Assigned here rather than inside the else because the AST pin
        # test_step_commit_to_mixer_loop1_is_single_prime_loop_call requires that
        # else-branch to stay a single statement.
        self._staged_loop_idx = self._loop_idx if self._loop_idx > 1 else 0

        if self._loop_idx == 1:
            # First loop: add tracks at the mixer's CURRENT position (not 0),
            # otherwise they are immediately treated as past tracks if generation
            # took longer than we assumed. The atomic batch lives inside
            # Mixer.prime_loop (Phase 11 U3a migration off private reach).
            self.mixer.prime_loop(tracks_to_use, duration_samples=duration_samples)
        else:
            # Subsequent loops: queue audio without touching current loop boundary.
            # The mixer will fire the transition when it reaches current_loop_end_sample
            # and then set the new boundary from duration_samples.
            self.mixer.set_next_loop(
                tracks_to_use, next_loop_duration_samples=duration_samples, loop_idx=self._loop_idx
            )

        return tracks_to_use, duration_samples

    async def _step_commit_state(self, pregen_ready, tracks_to_use, duration_samples) -> _CommitResult:
        """P11: atomic single-lock state commit (~84 LOC, the >50 exception).

        ONE ``async with state.lock:`` block performs the previous_stems/active_stems
        rotation, stem_history, pregen metadata application, loop-1 recording
        capture, UI override apply/clear, and the pre-gen snapshot. Returns the
        handoff bundle consumed by ``_step_post_commit`` (P12).
        """
        # PRE-GENERATION: Only start if no pre-gen task is running
        needs_pregen = self._loop_idx > 1 and (self._pregen_task is None or self._pregen_task.done())

        # Step 10: Update state.
        # When using pre-gen, we need to use pregen_results['next_stems'] as our active_stems.
        # For loop_idx == 1 (first loop), record the initial "now playing" state after the
        # lock releases since record_loop_transition acquires sync_lock.
        needs_initial_record = False
        _rec_stems: list = []
        _rec_set_name = ""
        _rec_reasoning = ""
        async with state.lock:
            if state.active_stems:
                state.previous_stems = list(state.active_stems)
                state.stem_history.append(state.active_stems)
                if len(state.stem_history) > 8:
                    state.stem_history.pop(0)

            if pregen_ready:
                assert self._pregen_results is not None  # pregen_ready gate (P2)
                state.active_stems = list(self._pregen_results["next_stems"])
            else:
                state.active_stems = list(state.next_stems)

            state.next_stems = []
            state.muted_stems.clear()
            state.soloed_stems.clear()
            state.stem_volumes.clear()
            state.loop_count += 1

            if pregen_ready:
                assert self._pregen_results is not None  # pregen_ready gate (P2)
                # Update BPM, key, etc. from pre-gen results (B5: coalesce an
                # explicit null so a bad conductor field can never reach state).
                state.current_bpm = sanitize_master_bpm(self._pregen_results.get("master_bpm"), state.current_bpm)
                state.current_key = sanitize_master_key(self._pregen_results.get("master_key"), state.current_key)
                state.current_set_name = self._pregen_results.get("set_name", "Unknown Set")
                state.llm_reasoning = self._pregen_results.get("reasoning", "No reasoning provided.")

                # Mirror the pregen audio into the download LRU (review AUDIO-3):
                # the background pregen path writes ONLY loop.stem_cache
                # (brief-01 risk #4), so state.last_generated_stems froze after
                # loop 1 and stem downloads 404'd for every later loop. The
                # FOREGROUND loop owns LRU routing (same as P8's cache_stem
                # call), so recording what became audible here preserves the
                # pinned divergence. cache_stem is a capped dict op — safe in-lock.
                _pregen_stems = self._pregen_results.get("next_stems", [])
                for _track_audio, _stem_idx in self._pregen_results.get("prepared_tracks", []):
                    if 0 <= _stem_idx < len(_pregen_stems):
                        state.cache_stem(_pregen_stems[_stem_idx].get("prompt", ""), _track_audio)

                # Build action log for pre-generated loop (shared shaper)
                state.last_actions = format_action_log(self._pregen_results.get("actions", []), state.previous_stems)

            # Capture for initial recording (loop_idx == 1 has no mixer transition event)
            if self._loop_idx == 1:
                needs_initial_record = True
                _rec_stems = list(state.active_stems)
                _rec_set_name = state.current_set_name
                _rec_reasoning = state.llm_reasoning

            # Apply pending UI overrides
            if state.target_bpm_override:
                state.current_bpm = state.target_bpm_override
                state.target_bpm_override = None
            if state.target_key_override:
                state.current_key = state.target_key_override
                state.target_key_override = None

            # Take state snapshot for pre-generation (before releasing lock)
            state_snapshot = {
                "current_bpm": state.current_bpm,
                "current_key": state.current_key,
                "active_stems": list(state.active_stems),
                "user_override": state.user_override,
                "available_instruments": list(state.available_instruments),
                "stem_history": list(state.stem_history),
                "llm_config": {
                    "base_url": state.llm_base_url,
                    "api_key": state.llm_api_key,
                    "model": state.llm_model,
                },
            }

        return _CommitResult(
            needs_pregen=needs_pregen,
            needs_initial_record=needs_initial_record,
            rec_stems=_rec_stems,
            rec_set_name=_rec_set_name,
            rec_reasoning=_rec_reasoning,
            state_snapshot=state_snapshot,
        )

    async def _step_post_commit(self, commit: _CommitResult, tracks_to_use, duration_samples) -> None:
        """P12: record initial loop, prune stem cache, spawn/skip pre-generation.

        ``record_loop_transition`` (takes the blocking sync_lock) runs OUTSIDE
        ``state.lock`` — it is called just after ``_step_commit_state``'s lock
        released. The else-branch reads ``state.active_stems`` LIVE (unlocked)
        per CONCERN-5: do NOT substitute a snapshot value (that would change
        behavior).
        """
        # Record initial "now playing" state for first loop (no mixer transition fires for loop 1)
        if commit.needs_initial_record:
            state.record_loop_transition(1, commit.rec_stems, commit.rec_set_name, commit.rec_reasoning)

        # Cache maintenance (REL-06: TTL from last use + entry cap)
        self._prune_stem_cache()

        # PRE-GENERATION: Only start if we don't have a loop already queued
        # and no pre-gen task is running
        if commit.needs_pregen:
            next_loop_idx = self._loop_idx + 1
            print(f"[AsyncLoop-{self._loop_idx}] No loop queued, starting pre-generation for loop {next_loop_idx}...")
            self._pregen_done.clear()
            self._pregen_results = None
            self._pregen_task = asyncio.create_task(self._pre_generate_next_loop(next_loop_idx, commit.state_snapshot))
        elif self._loop_idx == 1:
            print(f"[AsyncLoop-{self._loop_idx}] Loop {self._loop_idx + 1} already queued, skipping pre-gen")
            # Signal that pre-gen is "done" - the loop is queued in the mixer
            self._pregen_done.set()
            # Update _pregen_results to reflect the queued loop.
            # Use active_stems (state.next_stems was already cleared to [] above).
            # Loop 1 only: there is no in-flight pre-gen task whose flags/ownership
            # this could clobber (that is what made the loop>=2 case unsafe below).
            self._pregen_results = {
                "loop_idx": self._loop_idx + 1,
                "prepared_tracks": tracks_to_use,
                "loop_duration_samples": duration_samples,
                "next_stems": list(state.active_stems),
            }
        else:
            # Round-3 fix B1 (review 01/1): a STILL-RUNNING pre-gen task used to be
            # papered over here by setting _pregen_done and fabricating a result for
            # loop_idx+1. The next iteration's P2 gate accepted that fabrication,
            # so the iteration replayed the same audio with ZERO suspension points
            # (P13 broke before its only sleep) -> permanent event-loop starvation
            # plus runaway loop_count/audit inflation. Nothing is fabricated and the
            # pending task's own flags are left untouched: the next iteration simply
            # runs the fresh conductor path.
            print(
                f"[AsyncLoop-{self._loop_idx}] Pre-gen for a later loop still running; "
                f"leaving loop {self._loop_idx + 1} to the fresh conductor path"
            )

        # REL-04: keep the in-RAM audit buffers bounded — flush past the row
        # threshold instead of holding the whole show until stop_show. Routed
        # through the audit port; the module flush serializes on _flush_lock and
        # re-queues its rows on failure, so the loop just retries next iteration.
        # len() is a GIL-atomic read; the mixer thread is never involved (this is
        # the async loop task, post-commit — the DB I/O is threaded off-loop).
        try:
            if len(state.llm_interaction_buffer) > AUDIT_FLUSH_THRESHOLD_ROWS:
                await self._audit.flush()
        except Exception as e:  # noqa: BLE001  # flush re-queues internally; guard is belt-and-braces
            print(f"[AsyncLoop-{self._loop_idx}] Audit flush failed (will retry next loop): {e}")

    def _prune_stem_cache(self) -> None:
        """REL-06: drop stale-then-overflow stem-cache entries (TTL + cap).

        TTL first (no hit for STEM_CACHE_TTL_SECONDS), then oldest-last_used
        overflow beyond STEM_CACHE_MAX_ENTRIES. Called from P12 only —
        stem_cache has a single async owner (this loop task), so no lock.
        """
        now = time.time()
        stale_keys = [k for k, v in self.stem_cache.items() if now - v["last_used"] > STEM_CACHE_TTL_SECONDS]
        for key in stale_keys:
            del self.stem_cache[key]
        overflow = len(self.stem_cache) - STEM_CACHE_MAX_ENTRIES
        if overflow <= 0:
            return
        by_age = sorted(self.stem_cache.items(), key=lambda item: item[1]["last_used"])
        for key, _entry in by_age[:overflow]:
            del self.stem_cache[key]

    async def _step_await_pregen(self) -> None:
        """P13: await pre-generation completion, recording mixer transitions meanwhile.

        Both breaks are LOCAL (they end this iteration, not the outer loop — the
        outer while re-checks ``self.running and state.is_running`` after this
        returns). ``record_loop_transition`` snapshots under ``state.lock`` then
        runs OUTSIDE it (it acquires the blocking sync_lock).

        Round-3 changes: the pre-gen-done break now (a) yields once so no code
        path can return from P13 without ever suspending (B1), and (b) is held
        while P10-staged audio is still waiting for its boundary, so the next
        iteration cannot overwrite a loop that has not started playing yet (B2).
        """
        assert self.mixer is not None  # set in start() before _run_loop spawns
        # Step 11: Wait until we need to generate next loop.
        # Wait for pre-generation to complete (it runs the LLM call for us)
        if self.running and not state.shutdown_event.is_set():
            previous_ahead: float | None = None
            while self.running:
                # Check if mixer transitioned to a new loop and record it
                transitioned_loop_idx = self.mixer.pop_transition_event()
                if transitioned_loop_idx is not None and transitioned_loop_idx > 0:
                    # B2: the boundary fired, so the audio P10 staged for this
                    # loop is now playing and it is safe to queue the next one.
                    if self._staged_loop_idx and transitioned_loop_idx == self._staged_loop_idx:
                        self._staged_loop_idx = 0
                    # A3: record_loop_transition acquires the blocking sync_lock;
                    # snapshot under state.lock, then call it OUTSIDE the lock so
                    # the event loop is never stalled by the Mixer thread.
                    async with state.lock:
                        t_stems = list(state.active_stems)
                        t_set = state.current_set_name
                        t_reason = state.llm_reasoning
                    state.record_loop_transition(transitioned_loop_idx, t_stems, t_set, t_reason)

                # Read current boundary via the public delegation (the mixer
                # takes its own lock internally so we see transitions that may
                # have already fired).
                current_ahead = self.mixer.loop_position_seconds()
                playhead_moving = previous_ahead is None or current_ahead < previous_ahead
                previous_ahead = current_ahead

                # Check if pre-gen is done first
                if self._pregen_done.is_set():
                    # B2: with audio still staged for THIS loop and the boundary
                    # still ahead of us, returning here lets the next iteration's
                    # set_next_loop replace it -> a fully generated loop is never
                    # played. Wait for the boundary instead. A playhead that stops
                    # advancing (stopped/dead mixer) releases the hold so this can
                    # never wait indefinitely.
                    if playhead_moving and self._staged_audio_pending(current_ahead):
                        await asyncio.sleep(0.25)
                        continue
                    print(f"[AsyncLoop-{self._loop_idx}] Pre-generation complete, using results")
                    # B1(a): guarantee a suspension point on this path. Every lock
                    # acquisition on a replay iteration is uncontended, so without
                    # this yield a fast path could monopolise the event loop.
                    await asyncio.sleep(0)
                    break

                if self._loop_idx > 1:
                    print(
                        f"[AsyncLoop-{self._loop_idx}] DEBUG: "
                        f"current_ahead={current_ahead:.2f}s, waiting for pre-gen..."
                    )
                if current_ahead < BOUNDARY_BREAK_SECONDS:
                    # Still waiting for pre-gen, but we need to break to avoid missing the loop transition
                    break
                await asyncio.sleep(0.25)

    def _staged_audio_pending(self, current_ahead: float) -> bool:
        """B2: True while P10-staged audio may still be replaced before it plays."""
        return self._staged_loop_idx != 0 and current_ahead >= BOUNDARY_BREAK_SECONDS
