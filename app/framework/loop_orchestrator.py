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

This file holds ONLY the loop lifecycle (``__init__``/``start``/``stop``), the
thin ``_run_loop`` driver, and the adapter delegates. The 14 per-phase
``_step_*`` methods live in ``app.framework.loop_steps`` (``_LoopSteps`` mixin,
Phase B of the E1–E6 refactor) so this file stays under the project's 500-LOC
rule. ``patch.object(loop, '_submit_job')`` etc. keep working: the delegates are
defined here and the ``_step_*`` methods resolve on the combined class via MRO.

Usage:
    # In app_ui.py lifespan or when starting a session:
    asyncio.create_task(run_framework_loop_async(session_id))
"""

import asyncio
import uuid
from typing import Any

import numpy as np

from app.framework.audio_fetch import GarageAudioAdapter
from app.framework.audit_recording import append_loop_audit
from app.framework.audit_recording import (  # noqa: F401  frozen re-exports (routes/shows.py, tests import these from here)
    _flush_lock,
    flush_recording_buffers,
)
from app.framework.conductor_interaction import (
    build_track_prompt,
    process_actions,  # noqa: F401  frozen public API (simulation/session_state imports it from here)
)
from app.framework.framework_conductor_async import ConductorLLMAsync
from app.framework.framework_mixer import Mixer
from app.framework.framework_state import state
from app.framework.job_queue import submit_generator_job
from app.framework.loop_steps import (
    LOOP_RETRY_BACKOFF_SECONDS,
    _LoopSteps,
    _StepResult,
)
from app.framework.pregeneration import run_pregeneration


class AsyncFrameworkLoop(_LoopSteps):
    """Event-driven DJ-set orchestrator: lifecycle + driver + adapter delegates.

    The per-phase ``_step_*`` bodies are mixed in from ``_LoopSteps``
    (``app/framework.loop_steps``); see that module's docstring for the safety
    invariants (single-lock P11 commit, no I/O inside ``state.lock``).
    """

    def __init__(self, session_id: uuid.UUID, *, conductor: ConductorLLMAsync | None = None):
        """
        Initialize the async framework loop.

        Args:
            session_id: UUID of the session this loop handles
            conductor: optional conductor override (E5 dependency injection).
                Defaults to a real ``ConductorLLMAsync``; inject another instance
                (or substitute via ``patch.object(loop, 'conductor')``) for tests.
        """
        self.session_id = session_id
        self.mixer: Mixer | None = None
        # Driving adapter (E5): constructor-injectable instead of hard-coded.
        self.conductor: ConductorLLMAsync = conductor if conductor is not None else ConductorLLMAsync()
        self._garage = None  # Lazy initialization on first use
        self._audio_adapter: GarageAudioAdapter | None = None  # Lazy GarageAudioAdapter
        self.running = False
        self.loop_task: asyncio.Task | None = None
        self.stem_cache: dict[str, dict] = {}  # cache_key -> {audio_data, last_used}
        self._pregen_task: asyncio.Task | None = None  # Background pre-generation task
        self._pregen_done = asyncio.Event()  # Signaled when pre-gen is complete
        self._pregen_loop_idx = 0  # Which loop we're pre-generating for
        self._pregen_results: dict[str, Any] | None = None  # Results from pre-generation
        self._loop_idx = 0  # Advanced by _step_wait_for_start (P1) on each PROCEED iteration

    @property
    def _audio(self) -> GarageAudioAdapter:
        """Lazily create the Garage audio-fetch adapter (Phase 2).

        Reads ``self._garage`` (the raw, possibly-None instance attr) on purpose,
        not an eager ``create_garage_client_from_env()`` call: a test may inject a
        preset client (Gap 3 sets ``loop._garage``), AND client creation must happen
        inside the adapter's fetch try/except via
        ``audio_fetch.create_garage_client_from_env`` — calling the factory eagerly
        here would raise KeyError when Garage env is unset and break the migrated
        string-patches / the empty-bytes / exception None paths.
        """
        if self._audio_adapter is None:
            self._audio_adapter = GarageAudioAdapter(self._garage)
        return self._audio_adapter

    async def start(self):
        """Start the mixer thread + the async generation loop task."""
        from concurrent.futures import ThreadPoolExecutor

        # Mixer needs its own thread; construct it in an executor so the event
        # loop is never blocked by sounddevice init.
        assert self.mixer is None  # set in start() before _run_loop spawns
        with ThreadPoolExecutor(max_workers=1) as ex:
            self.mixer = await asyncio.get_running_loop().run_in_executor(ex, Mixer)
        assert self.mixer is not None  # just assigned above (run_in_executor returns Any)
        self.mixer.start()
        self.running = True
        self.loop_task = asyncio.create_task(self._run_loop())

    async def stop(self):
        """Stop the async loop + the mixer thread (idempotent)."""
        self.running = False
        if self.loop_task is not None:
            self.loop_task.cancel()
            try:
                await self.loop_task
            except asyncio.CancelledError:
                pass
        if self.mixer is not None:
            self.mixer.stop()

    def _finish_loop(self):
        """Clean up after the loop exits (cancel pending pre-gen, stop mixer)."""
        if self._pregen_task is not None and not self._pregen_task.done():
            self._pregen_task.cancel()
        if self.mixer is not None:
            self.mixer.stop()

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

    # --- Adapter delegates (kept as methods so patch.object(loop, ...) works) ---

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
