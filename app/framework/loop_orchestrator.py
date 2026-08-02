"""
Async Framework Loop - Event-driven framework for mc-clanker.

This is the async version of framework_main.py that uses job-based
stem generation instead of synchronous generate_batch() calls.

The async framework loop:
1. Builds Conductor prompt from state
2. Calls LLM async (non-blocking)
3. Parses actions and submits jobs to the queue
4. Waits for job completion via wait_for_job_completion()
5. Fetches audio from Garage
6. Transitions stems in the mixer

This file is designed to be run alongside the existing framework_main.py
for gradual migration to the async architecture.

Usage:
    # In app_ui.py lifespan or when starting a session:
    asyncio.create_task(run_framework_loop_async(session_id))
"""

import asyncio
import enum
import time
import uuid
import numpy as np
from dataclasses import dataclass
from typing import Any, NamedTuple

from app.framework.framework_state import state
from app.framework.framework_mixer import Mixer
from app.framework.framework_conductor_async import ConductorLLMAsync
from app.framework.domain_audio import tile_to_loop
from app.framework.audio_fetch import GarageAudioAdapter
from app.framework.audit_recording import append_loop_audit
from app.framework.audit_recording import (  # noqa: F401  frozen re-exports (routes/shows.py, tests import these from here)
    _flush_lock,
    flush_recording_buffers,
)
from app.framework.conductor_interaction import (
    build_fallback_response,
    build_track_prompt,
    load_available_models,
    process_actions,  # noqa: F401  frozen public API (simulation/session_state imports it from here)
)
from app.framework.job_queue import submit_generator_job
from app.framework.pregeneration import run_pregeneration
from app.job_waiter import wait_for_multiple_jobs

# Kept at module top for import-compatibility: frozen bindings (and tests) still
# import these names from this shim module even though the real fetch call sites
# now live in app.framework.audio_fetch (brief-02 §D). decode_aac is no longer
# referenced directly here; create_garage_client_from_env backs the legacy
# self.garage property below.
from app.garage_client import create_garage_client_from_env
from app.aac_encoder import decode_aac  # noqa: F401  (frozen-binding re-export)


# Backoff between loop retries after a transient body error (review B1 watchdog).
# Kept short so the set recovers quickly; overridable by tests / config.
LOOP_RETRY_BACKOFF_SECONDS = 2.0

# calc_duration / to_two_channel / tile_to_loop (and DEFAULT_FALLBACK_BPM) live in
# domain_audio (Phase 2); re-imported above so frozen bindings and the
# pre-generation path (which still calls them directly) keep working.


# flush_recording_buffers + _flush_lock now live in app.framework.audit_recording
# (Phase 3); re-exported above for frozen-binding compatibility.


# process_actions now lives in app.framework.conductor_interaction (Phase 4);
# re-exported above for frozen-binding compatibility (simulation/session_state).


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


class AsyncFrameworkLoop:
    """
    Async framework loop for event-driven stem generation.

    This class manages the async framework loop lifecycle and provides
    a cleaner interface for starting/stopping the loop.
    """

    def __init__(self, session_id: uuid.UUID):
        """
        Initialize the async framework loop.

        Args:
            session_id: UUID of the session this loop handles
        """
        self.session_id = session_id
        self.mixer: Mixer | None = None
        self.conductor = ConductorLLMAsync()
        self._garage = None  # Lazy initialization on first use
        self._audio_adapter: GarageAudioAdapter | None = None  # Lazy GarageAudioAdapter
        self.running = False
        self.loop_task: asyncio.Task | None = None
        self.stem_cache: dict[str, dict] = {}  # cache_key -> {audio_data, last_used}
        self._pregen_task: asyncio.Task | None = None  # Background pre-generation task
        self._pregen_done = asyncio.Event()  # Signaled when pre-gen is complete
        self._pregen_loop_idx = 0  # Which loop we're pre-generating for
        self._pregen_results: dict[str, Any] | None = None  # Results from pre-generation

    @property
    def garage(self):
        """Lazily create and return the Garage client."""
        if self._garage is None:
            self._garage = create_garage_client_from_env()
        return self._garage

    @property
    def _audio(self) -> GarageAudioAdapter:
        """Lazily create the Garage audio-fetch adapter (Phase 2).

        Reads ``self._garage`` (not the ``garage`` property) on purpose: a test
        may inject a preset client (Gap 3 sets ``loop._garage``), AND client
        creation must happen inside the adapter's fetch try/except via
        ``audio_fetch.create_garage_client_from_env``. The ``garage`` property
        eagerly calls this module's factory name (outside any try/except), which
        raises KeyError when Garage env is unset — so using it here would break
        the migrated string-patches and the empty-bytes / exception None paths.
        """
        if self._audio_adapter is None:
            self._audio_adapter = GarageAudioAdapter(self._garage)
        return self._audio_adapter

    async def start(self):
        """Start the async framework loop."""
        if self.running:
            return

        self.running = True

        # Initialize mixer in async context
        loop = asyncio.get_running_loop()
        self.mixer = await loop.run_in_executor(None, lambda: Mixer(sample_rate=44100, channels=2))
        self.mixer.start()

        # Start the loop
        self.loop_task = asyncio.create_task(self._run_loop())
        print(f"[AsyncFrameworkLoop-{self.session_id}] Started")

    async def stop(self):
        """Stop the async framework loop."""
        if not self.running:
            return

        self.running = False

        if self.loop_task:
            self.loop_task.cancel()
            try:
                await self.loop_task
            except asyncio.CancelledError:
                pass

        if self.mixer:
            await asyncio.get_running_loop().run_in_executor(None, self.mixer.stop)

        print(f"[AsyncFrameworkLoop-{self.session_id}] Stopped")

    def _finish_loop(self):
        """Mark the loop stopped and tear down the mixer (B1 cleanup path)."""
        self.running = False
        if self.mixer:
            self.mixer.stop()
        print("[AsyncFrameworkLoop] Done")

    async def _run_loop(self):
        """Main async framework loop.

        Resilience (review B1): the loop body is wrapped in a per-iteration
        try/except so a single unexpected error logs, backs off, and retries
        rather than terminating the whole set until a manual restart. A true
        external supervisor (recreating the task) is still recommended.

        Decomposed into ``_step_*`` methods (brief-05): each phase takes its
        spanning-read locals as params and returns its spanning-write locals;
        this driver threads them between calls and interprets the 3 outer-while
        control-flow jumps via ``_StepResult``.
        """
        self._loop_idx = 0

        while self.running and state.is_running:
            try:
                r = await self._step_wait_for_start()
                if r is _StepResult.EXIT_LOOP:
                    break
                if r is _StepResult.RESTART_ITER:
                    continue

                pregen = await self._step_pregen_decision()
                if pregen.result is _StepResult.RESTART_ITER:
                    continue
                pregen_ready = pregen.pregen_ready
                conductor_response = pregen.conductor_response
                prepared_tracks = pregen.prepared_tracks
                loop_duration_samples = pregen.loop_duration_samples

                snap = await self._step_read_state()

                # P4-P9 run only on the fresh path: pregen already has
                # conductor_response + prepared_tracks from P2 (review A1).
                if not pregen_ready:
                    conductor_response = await self._step_call_conductor(
                        snap.current_bpm,
                        snap.current_key,
                        snap.active_stems,
                        snap.user_override,
                        snap.available_instruments,
                        snap.stem_history,
                        snap.llm_config,
                        snap.available_models,
                    )
                    deduped_tracks = await self._step_parse_actions(conductor_response, snap.active_stems)
                    local_next_stems, local_current_bpm, local_current_key = await self._step_build_next_stems(
                        snap.bpm_override,
                        snap.key_override,
                        conductor_response,
                        snap.current_bpm,
                        snap.current_key,
                        deduped_tracks,
                    )
                    pending_jobs = await self._step_submit_jobs(local_next_stems, local_current_bpm, local_current_key)
                    await self._step_await_jobs_fetch(pending_jobs, local_next_stems)
                    prepared_tracks, loop_duration_samples = await self._step_tile_audio(
                        local_next_stems, local_current_bpm, local_current_key, deduped_tracks
                    )

                await self._step_append_audit(conductor_response, snap.active_stems)
                tracks_to_use, duration_samples = await self._step_commit_to_mixer(
                    pregen_ready, prepared_tracks, loop_duration_samples
                )
                commit = await self._step_commit_state(pregen_ready, tracks_to_use, duration_samples)
                await self._step_post_commit(commit, tracks_to_use, duration_samples)
                await self._step_await_pregen()

            except asyncio.CancelledError:
                # Cancellation (stop/shutdown): clean up, then propagate.
                self._finish_loop()
                raise
            except Exception as e:
                # B1: don't let one bad iteration kill the set permanently.
                print(f"[AsyncFrameworkLoop] Loop iteration error (will retry): {e}")
                import traceback

                traceback.print_exc()
                await asyncio.sleep(LOOP_RETRY_BACKOFF_SECONDS)
                continue

        self._finish_loop()

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

        # Build action log for debugging/auditing
        async with state.lock:
            current_actions_log = []
            for action in conductor_response.get("actions", []):
                a_type = action.get("action_type")
                idx = action.get("stem_index")
                if a_type == "retain" and idx is not None and 0 <= idx < len(active_stems):
                    s = active_stems[idx]
                    prompt = s.get("prompt", "")
                    prompt_part = prompt.split(",")[1].strip() if len(prompt.split(",")) > 1 else prompt
                    current_actions_log.append(f"Retained {prompt_part}")
                elif a_type == "add":
                    current_actions_log.append(f"Added {action.get('sub_family', '')}")
                elif a_type == "remove" and idx is not None and 0 <= idx < len(active_stems):
                    s = active_stems[idx]
                    prompt = s.get("prompt", "")
                    prompt_part = prompt.split(",")[1].strip() if len(prompt.split(",")) > 1 else prompt
                    current_actions_log.append(f"Removed {prompt_part}")
            state.last_actions = current_actions_log

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
            cache_key = f"{m_id}_{prompt}_{local_current_bpm}_{local_current_key}_{track_bars}"

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
        if pregen_ready:
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
                state.active_stems = list(self._pregen_results["next_stems"])
            else:
                state.active_stems = list(state.next_stems)

            state.next_stems = []
            state.muted_stems.clear()
            state.soloed_stems.clear()
            state.stem_volumes.clear()
            state.loop_count += 1

            if pregen_ready:
                # Update BPM, key, etc. from pre-gen results
                state.current_bpm = self._pregen_results.get("master_bpm", state.current_bpm)
                state.current_key = self._pregen_results.get("master_key", state.current_key)
                state.current_set_name = self._pregen_results.get("set_name", "Unknown Set")
                state.llm_reasoning = self._pregen_results.get("reasoning", "No reasoning provided.")

                # Build action log for pre-generated loop
                current_actions_log = []
                for action in self._pregen_results.get("actions", []):
                    a_type = action.get("action_type")
                    idx = action.get("stem_index")
                    if a_type == "retain" and idx is not None and 0 <= idx < len(state.previous_stems):
                        s = state.previous_stems[idx]
                        prompt = s.get("prompt", "")
                        prompt_part = prompt.split(",")[1].strip() if len(prompt.split(",")) > 1 else prompt
                        current_actions_log.append(f"Retained {prompt_part}")
                    elif a_type == "add":
                        current_actions_log.append(f"Added {action.get('sub_family', '')}")
                    elif a_type == "remove" and idx is not None and 0 <= idx < len(state.previous_stems):
                        s = state.previous_stems[idx]
                        prompt = s.get("prompt", "")
                        prompt_part = prompt.split(",")[1].strip() if len(prompt.split(",")) > 1 else prompt
                        current_actions_log.append(f"Removed {prompt_part}")
                state.last_actions = current_actions_log

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
                        f"[AsyncLoop-{self._loop_idx}] DEBUG: current_ahead={current_ahead:.2f}s, waiting for pre-gen..."
                    )
                if current_ahead < 0.5:
                    # Still waiting for pre-gen, but we need to break to avoid missing the loop transition
                    break
                await asyncio.sleep(0.25)

    def _build_prompt(self, track: dict, key: str, bpm: int) -> str:
        """Build a generation prompt; delegates to conductor_interaction (Phase 4)."""
        return build_track_prompt(track, key, bpm)

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
        """Submit a generation job; delegates to job_queue (Phase 5).

        Kept as a method so ``patch.object(loop, '_submit_job')`` keeps working.
        """
        return await submit_generator_job(
            session_id=session_id,
            instrument=instrument,
            prompt=prompt,
            major_family=major_family,
            model_id=model_id,
            key=key,
            bpm=bpm,
            timbre_tags=timbre_tags,
            bars=bars,
        )

    async def _fetch_audio(self, audio_path: str) -> np.ndarray | None:
        """
        Fetch audio from Garage and decode to numpy array.

        Args:
            audio_path: Garage S3 path (e.g., "audio/{job_id}.aac")

        Returns:
            numpy array of audio samples (float32, shape [samples, channels])
            or None if fetch/decode fails
        """
        # Phase 2: delegate to GarageAudioAdapter (app.framework.audio_fetch).
        # Preserves exact behavior: empty bytes -> None, AAC decode in executor,
        # any fetch/decode error swallowed -> None. Test string-patches now target
        # app.framework.audio_fetch (where decode_aac is actually resolved).
        return await self._audio.fetch(audio_path)

    async def _append_loop_audit(self, conductor_response, active_stems, loop_idx):
        """Buffer one loop's audit rows; delegates to audit_recording (Phase 3).

        Kept as a method so ``patch.object(loop, '_append_loop_audit')`` and
        direct test calls keep working (brief-02 ssD).
        """
        await append_loop_audit(conductor_response, active_stems, loop_idx)

    async def _pre_generate_next_loop(self, for_loop_idx: int, snapshot: dict[str, Any]):
        """Pre-generate the next loop; delegates to pregeneration (Phase 6).

        Kept as a method so ``patch.object(loop, '_pre_generate_next_loop')`` and
        the ``_pregen_*`` attribute assertions in tests keep working. The body
        lives in app.framework.pregeneration.run_pregeneration, which shares
        this loop's ``stem_cache`` (R11) and preserves the cache_stem divergence
        (brief-01 risk #4: background path never calls state.cache_stem).
        """
        await run_pregeneration(self, for_loop_idx, snapshot)


async def run_framework_loop_async(session_id: uuid.UUID):
    """
    Async framework loop entry point.

    This is a convenience function that creates and runs an AsyncFrameworkLoop.

    Args:
        session_id: UUID of the session to run
    """
    loop = AsyncFrameworkLoop(session_id)
    await loop.start()

    try:
        while loop.running:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await loop.stop()


# Standalone test
if __name__ == "__main__":

    async def main():
        session_id = uuid.uuid4()
        print(f"Starting async framework loop for session: {session_id}")

        # For testing without actual generation
        state.is_generating = True

        try:
            await run_framework_loop_async(session_id)
        except KeyboardInterrupt:
            print("\nShutdown requested...")

    asyncio.run(main())
