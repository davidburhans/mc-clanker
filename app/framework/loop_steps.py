"""The ``_step_*`` decomposition of ``AsyncFrameworkLoop._run_loop`` (Phase B).

Extracted from ``loop_orchestrator.py`` so the orchestrator file stays under the
project's 500-LOC rule: the 14 per-phase ``_step_*`` methods (each ≤50 LOC,
``_step_commit_state`` the documented ~85-LOC single-lock exception) live here as
a mixin, while the orchestrator keeps the lifecycle (``__init__``/``start``/``stop``),
the thin ``_run_loop`` driver, and the adapter delegates.

The mixin references ``self.*`` instance attributes set in
``AsyncFrameworkLoop.__init__`` (``mixer``/``stem_cache``/``_loop_idx``/
``_pregen_*``/``conductor``/...) and the adapter delegates (``_build_prompt`` /
``_submit_job`` / ``_fetch_audio`` / ``_append_loop_audit`` /
``_pre_generate_next_loop``) defined on the orchestrator — all resolved at runtime
via MRO, so ``patch.object(loop, '_submit_job')`` keeps working unchanged.

SAFETY INVARIANT: the ``async with state.lock:`` blocks (the refactor's #2 risk —
no I/O inside the lock) live in the ``_step_*`` methods HERE. The source-level guard
``test_no_io_inside_state_lock_in_orchestrator`` is scoped to scan BOTH this file
and ``loop_orchestrator.py``; do not weaken that scope.

Result types (``_StepResult``/``_CommitResult``/``_PregenDecision``/``_StateSnapshot``)
are defined here because they are produced and consumed by the ``_step_*`` methods
and imported back into the orchestrator's driver.
"""

from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from app.framework.conductor_interaction import (
    build_fallback_response,
    format_action_log,
    load_available_models,
    process_actions,
)
from app.framework.domain_audio import make_cache_key, tile_to_loop
from app.framework.framework_state import state
from app.job_waiter import wait_for_multiple_jobs

if TYPE_CHECKING:
    import uuid

    import numpy as np

    from app.framework.framework_mixer import Mixer
    from app.framework.ports import ConductorPort

# Backoff between loop retries after a transient body error (review B1 watchdog).
# Kept short so the set recovers quickly; overridable by tests / config.
LOOP_RETRY_BACKOFF_SECONDS = 2.0


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

            # Check for active overrides
            bpm_override = state.target_bpm_override
            key_override = state.target_key_override

            if bpm_override:
                state.current_bpm = bpm_override
                state.target_bpm_override = None
            if key_override:
                state.current_key = key_override
                state.target_key_override = None

            current_bpm = state.current_bpm
            current_key = state.current_key
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
                state.current_bpm = bpm_override
            else:
                state.current_bpm = conductor_response.get("master_bpm", current_bpm)

            if key_override:
                state.current_key = key_override
            else:
                state.current_key = conductor_response.get("master_key", current_key)

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

    async def _step_submit_jobs(self, local_next_stems, local_current_bpm, local_current_key) -> list:
        """P7: submit generation jobs for uncached stems.

        The cache-HIT ``continue`` is LOCAL to this for-loop (already have
        audio) — it is not an outer-while jump, so it stays inline.
        """
        pending_jobs = []  # List of (job_id, original_index)

        for i, t in enumerate(local_next_stems):
            prompt = t["prompt"]
            track_bars = t["bars"]
            m_id = t.get("model_id")
            orig = t.get("_original_details", {})
            cache_key = make_cache_key(m_id, prompt, local_current_bpm, local_current_key, track_bars)

            # Check cache
            if cache_key in self.stem_cache:
                print(f"Cache HIT: '{prompt}'")
                continue  # Already have audio

            # Submit job
            job_id = await self._submit_job(
                session_id=self.session_id,
                instrument=orig.get("sub_family", "Unknown"),
                prompt=prompt,
                major_family=orig.get("major_family"),
                model_id=m_id,
                key=local_current_key,
                bpm=local_current_bpm,
                timbre_tags=orig.get("timbre_tags", []),
                bars=track_bars,
            )
            pending_jobs.append((job_id, i, cache_key))

        return pending_jobs

    async def _step_await_jobs_fetch(self, pending_jobs, local_next_stems) -> None:
        """P8: wait for jobs, fetch audio, populate stem_cache + state.cache_stem.

        B7: fetched audio is routed through ``state.cache_stem`` (under lock) so
        the 16-entry LRU cap is enforced — the background pregen path never
        calls it (brief-01 risk #4 divergence).
        """
        if pending_jobs:
            job_ids = [job_id for job_id, _, _ in pending_jobs]
            print(f"[AsyncLoop-{self._loop_idx}] Waiting for {len(job_ids)} jobs to complete...")
            wait_start = time.time()

            results = await wait_for_multiple_jobs(job_ids, timeout=120.0)

            wait_duration = time.time() - wait_start
            print(f"[AsyncLoop-{self._loop_idx}] Jobs completed in {wait_duration:.2f}s")

            # Process results
            for (job_id, orig_idx, cache_key), audio_path in zip(pending_jobs, results.values()):
                if audio_path:
                    # Fetch audio from Garage
                    audio_data = await self._fetch_audio(audio_path)
                    if audio_data is not None:
                        self.stem_cache[cache_key] = {"audio_data": audio_data, "last_used": time.time()}
                        async with state.lock:
                            state.cache_stem(local_next_stems[orig_idx]["prompt"], audio_data)
                else:
                    print(f"Job {job_id} failed or timed out")

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

    async def _step_append_audit(self, conductor_response, active_stems) -> None:
        """C1: buffer this loop's conductor decision + actions for the audit trail."""
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

        if self._loop_idx == 1:
            # First loop: add tracks at the mixer's CURRENT position (not 0),
            # otherwise they are immediately treated as past tracks if generation
            # took longer than we assumed.
            with self.mixer.lock:
                start_sample = self.mixer.current_sample
                for audio_data, stem_idx in tracks_to_use:
                    self.mixer._add_track_internal(self.mixer._ensure_stereo(audio_data), start_sample, stem_idx)
                self.mixer.current_loop_end_sample = start_sample + duration_samples
                self.mixer._current_loop_duration = duration_samples
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
                # Update BPM, key, etc. from pre-gen results
                state.current_bpm = self._pregen_results.get("master_bpm", state.current_bpm)
                state.current_key = self._pregen_results.get("master_key", state.current_key)
                state.current_set_name = self._pregen_results.get("set_name", "Unknown Set")
                state.llm_reasoning = self._pregen_results.get("reasoning", "No reasoning provided.")

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

        # Cache maintenance
        current_time = time.time()
        stale_keys = [k for k, v in self.stem_cache.items() if current_time - v["last_used"] > 300]
        for k in stale_keys:
            del self.stem_cache[k]

        # PRE-GENERATION: Only start if we don't have a loop already queued
        # and no pre-gen task is running
        if commit.needs_pregen:
            next_loop_idx = self._loop_idx + 1
            print(f"[AsyncLoop-{self._loop_idx}] No loop queued, starting pre-generation for loop {next_loop_idx}...")
            self._pregen_done.clear()
            self._pregen_results = None
            self._pregen_task = asyncio.create_task(self._pre_generate_next_loop(next_loop_idx, commit.state_snapshot))
        else:
            print(f"[AsyncLoop-{self._loop_idx}] Loop {self._loop_idx + 1} already queued, skipping pre-gen")
            # Signal that pre-gen is "done" - the loop is queued in the mixer
            self._pregen_done.set()
            # Update _pregen_results to reflect the queued loop.
            # Use active_stems (state.next_stems was already cleared to [] above).
            self._pregen_results = {
                "loop_idx": self._loop_idx + 1,
                "prepared_tracks": tracks_to_use,
                "loop_duration_samples": duration_samples,
                "next_stems": list(state.active_stems),
            }

    async def _step_await_pregen(self) -> None:
        """P13: await pre-generation completion, recording mixer transitions meanwhile.

        The inner ``while self.running:`` is kept verbatim: both breaks are LOCAL
        (they end this iteration, not the outer loop — the outer while re-checks
        ``self.running and state.is_running`` after this returns).
        ``record_loop_transition`` snapshots under ``state.lock`` then runs
        OUTSIDE it (it acquires the blocking sync_lock).
        """
        assert self.mixer is not None  # set in start() before _run_loop spawns
        # Step 11: Wait until we need to generate next loop.
        # Wait for pre-generation to complete (it runs the LLM call for us)
        if self.running and not state.shutdown_event.is_set():
            while self.running:
                # Check if mixer transitioned to a new loop and record it
                transitioned_loop_idx = self.mixer.pop_transition_event()
                if transitioned_loop_idx is not None and transitioned_loop_idx > 0:
                    # A3: record_loop_transition acquires the blocking sync_lock;
                    # snapshot under state.lock, then call it OUTSIDE the lock so
                    # the event loop is never stalled by the Mixer thread.
                    async with state.lock:
                        t_stems = list(state.active_stems)
                        t_set = state.current_set_name
                        t_reason = state.llm_reasoning
                    state.record_loop_transition(transitioned_loop_idx, t_stems, t_set, t_reason)

                # Check if pre-gen is done first
                if self._pregen_done.is_set():
                    print(f"[AsyncLoop-{self._loop_idx}] Pre-generation complete, using results")
                    break

                # Read current boundary from the mixer (under lock so we see
                # transitions that may have already fired).
                with self.mixer.lock:
                    live_end = self.mixer.current_loop_end_sample
                    live_pos = self.mixer.current_sample
                current_ahead = (live_end - live_pos) / self.mixer.sample_rate
                if self._loop_idx > 1:
                    print(
                        f"[AsyncLoop-{self._loop_idx}] DEBUG: "
                        f"current_ahead={current_ahead:.2f}s, waiting for pre-gen..."
                    )
                if current_ahead < 0.5:
                    # Still waiting for pre-gen, but we need to break to avoid missing the loop transition
                    break
                await asyncio.sleep(0.25)
