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

This file holds ONLY the loop lifecycle (``__init__``/``start``/``stop``) and the
thin ``_run_loop`` driver. The 14 per-phase ``_step_*`` methods live in
``app.framework.loop_steps`` (``_LoopSteps`` mixin, Phase B of the E1–E6
refactor) and the adapter delegates live in ``app.framework.loop_delegates``
(``_LoopDelegates`` mixin, Phase FU-2 extraction) so this file stays under the
project's 500-LOC rule. ``patch.object(loop, '_submit_job')`` etc. keep working:
both mixins resolve on the combined class via MRO.

Usage:
    # In app_ui.py lifespan or when starting a session:
    asyncio.create_task(run_framework_loop_async(session_id))
"""

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

from app.framework.audio_fetch import GarageAudioAdapter
from app.framework.audit_recording import (  # noqa: F401  frozen re-exports (routes/shows.py, tests import these from here)
    AuditAdapter,
    _flush_lock,
    append_loop_audit,
    flush_recording_buffers,
)
from app.framework.conductor_interaction import (
    process_actions,  # noqa: F401  frozen public API (simulation/session_state imports it from here)
)
from app.framework.framework_conductor_async import ConductorLLMAsync
from app.framework.framework_mixer import Mixer
from app.framework.framework_state import state
from app.framework.job_queue import PostgresJobQueueAdapter
from app.framework.loop_delegates import _LoopDelegates
from app.framework.loop_steps import (
    _LoopSteps,
    _StepResult,
    loop_retry_backoff_delay,
)
from app.framework.ports import AudioFetchPort, AuditSinkPort, ConductorPort, JobQueuePort
from app.garage_client import GarageClient


class AsyncFrameworkLoop(_LoopDelegates, _LoopSteps):
    """Event-driven DJ-set orchestrator: lifecycle + driver.

    The per-phase ``_step_*`` bodies are mixed in from ``_LoopSteps``
    (``app/framework.loop_steps``) and the adapter delegates from
    ``_LoopDelegates`` (``app/framework.loop_delegates``, FU-2 pure move —
    first in the MRO so the real delegates win over ``_LoopSteps``' raising
    stubs); see those modules' docstrings for the safety invariants
    (single-lock P11 commit, no I/O inside ``state.lock``).
    """

    def __init__(
        self,
        session_id: uuid.UUID,
        *,
        conductor: ConductorPort | None = None,
        mixer_factory: Callable[[], Mixer] | None = None,
        audio: AudioFetchPort | None = None,
        jobs: JobQueuePort | None = None,
        audit: AuditSinkPort | None = None,
    ):
        """
        Initialize the async framework loop.

        Args:
            session_id: UUID of the session this loop handles
            conductor: optional driving-port override (E5 dependency injection).
                Defaults to a real ``ConductorLLMAsync`` (which structurally
                satisfies ``ConductorPort``); inject any ``ConductorPort`` fake
                for in-memory testing.
            mixer_factory: optional zero-arg callable that builds the ``Mixer``
                (E5/R14 dependency injection). Defaults to the ``Mixer`` class
                itself (callable as ``Mixer()``). Construction stays LAZY — the
                factory runs inside ``start()``'s ``ThreadPoolExecutor``, not
                here, so the event loop is never blocked by mixer init.
            audio: optional audio-fetch port override (E5/U1 dependency
                injection), inject any ``AudioFetchPort`` fake for in-memory
                testing. Defaults to None: the ``_audio`` property then LAZILY
                builds ``GarageAudioAdapter(self._garage)`` — NOT an eager
                ``GarageAudioAdapter(None)``, because reading ``self._garage``
                inside the property preserves the Gap-3 ``_garage``-injection +
                lazy-env path (test_audio_fetch_guard / characterization Gap 3).
                An eager adapter stored here would bypass ``self._garage`` and
                silently break those tests; see the ``_audio`` docstring.
            jobs: optional job-queue port override (E5/U2 dependency injection),
                inject any ``JobQueuePort`` fake for in-memory testing. Defaults
                to a real ``PostgresJobQueueAdapter()`` (EAGER — its constructor
                is a no-op; the DB session opens lazily inside submit at call
                time, so unlike the audio port there is no lazy/env path to
                preserve). Existing callers omitting it are unchanged.
            audit: optional audit-sink port override (E5/U3 dependency
                injection), inject any ``AuditSinkPort`` fake for in-memory
                testing. Defaults to a real ``AuditAdapter()`` (EAGER — its
                constructor is a no-op; the DB session opens lazily inside
                ``flush_recording_buffers`` at call time, and ``_flush_lock`` is
                module-level, so there is no lazy/env path to preserve — unlike
                ``_audio``). Existing callers omitting it are unchanged.
        """
        self.session_id = session_id
        self.mixer: Mixer | None = None
        # Mixer factory (E5/R14): injectable for fakes; defaults to the real Mixer
        # class. Construction stays LAZY — built in start()'s executor, not here.
        self._mixer_factory: Callable[[], Mixer] = mixer_factory if mixer_factory is not None else Mixer
        # Driving port (E5): injectable for fakes; defaults to the real conductor.
        self.conductor: ConductorPort = conductor if conductor is not None else ConductorLLMAsync()
        self._garage: GarageClient | None = None  # Lazy GarageClient (injected or env-built)
        # Audio-fetch port (U1-audio, E5 DI): injectable for fakes. When None the
        # _audio property lazily builds GarageAudioAdapter(self._garage) —
        # PRESERVING the Gap-3 _garage injection + lazy-env path. An eager
        # GarageAudioAdapter(None) here would bypass self._garage and silently
        # break test_audio_fetch_guard / characterization Gap 3, so the default
        # stays None (lazy). See the _audio property docstring.
        self._audio_adapter: AudioFetchPort | None = audio
        # Job-queue port (U2-jobs, E5 DI): injectable for fakes; defaults to the
        # real PostgresJobQueueAdapter (eager — its ctor is a no-op; the DB
        # session is opened lazily inside submit_generator_job at call time, so
        # there is no Gap-3-style lazy path to preserve — unlike _audio).
        self._jobs: JobQueuePort = jobs if jobs is not None else PostgresJobQueueAdapter()
        # Audit-sink port (U3-audit, E5 DI): injectable for fakes; defaults to
        # the real AuditAdapter (eager — its constructor is a no-op; the DB
        # session opens lazily inside flush_recording_buffers at call time, and
        # _flush_lock is module-level so there is no lazy/env path to preserve
        # — unlike _audio).
        self._audit: AuditSinkPort = audit if audit is not None else AuditAdapter()
        self.running = False
        self.loop_task: asyncio.Task | None = None
        self.stem_cache: dict[str, dict] = {}  # cache_key -> {audio_data, last_used}
        self._pregen_task: asyncio.Task | None = None  # Background pre-generation task
        self._pregen_done = asyncio.Event()  # Signaled when pre-gen is complete
        self._pregen_loop_idx = 0  # Which loop we're pre-generating for
        self._pregen_results: dict[str, Any] | None = None  # Results from pre-generation
        # Round-3 fix B2: loop index whose audio P10 queued via set_next_loop and
        # which the mixer has not consumed at its boundary yet (0 = nothing staged).
        self._staged_loop_idx = 0
        self._loop_idx = 0  # Advanced by _step_wait_for_start (P1) on each PROCEED iteration
        # FU-2 (rel-02 residual): generation of the loop NUMBERING — bumped on every
        # should_reset consumption (P3) because post-reset numbering restarts at 1;
        # pregen results carry their spawn-time stamp so a pre-reset in-flight result
        # can never be accepted again when the indices revisit.
        self._pregen_epoch = 0
        # REL-18 (U12): B1 consecutive-iteration failures (backoff input) and
        # consecutive job-submit failures (conductor-skip input) — two counters,
        # two semantics (a conductor-phase failure is not a submit failure).
        self._consecutive_loop_errors = 0
        self._consecutive_submit_failures = 0

    @property
    def _audio(self) -> AudioFetchPort:
        """Resolve the audio-fetch port, building the default lazily (Phase 2).

        When ``audio`` was ctor-injected, ``self._audio_adapter`` holds it and is
        returned verbatim (real DI). When omitted, ``_audio_adapter`` is None and
        this builds ``GarageAudioAdapter(self._garage)`` LAZILY — reading the raw
        ``_garage`` attr (not an eager ``create_garage_client_from_env()``), so a
        test may preset ``loop._garage`` AND client creation happens inside
        ``fetch``'s try/except via ``audio_fetch.create_garage_client_from_env``
        (the Gap-3 / test_audio_fetch_guard invariant). Calling the factory eagerly
        here would raise KeyError when Garage env is unset and break the migrated
        string-patches / the empty-bytes / exception None paths.
        """
        if self._audio_adapter is None:
            self._audio_adapter = GarageAudioAdapter(self._garage)
        return self._audio_adapter

    @_audio.setter
    def _audio(self, value: AudioFetchPort | None) -> None:
        """Inject/replace the audio-fetch port (additive seam; mirrors mixer).

        Writing None resets to the lazy default (the property then rebuilds from
        ``_garage``), matching the Gap-3 ``loop._audio_adapter = None`` reset.
        """
        self._audio_adapter = value

    async def start(self):
        """Start the mixer thread + the async generation loop task.

        Round-3 fix B6: mixer construction / ``mixer.start()`` failures now leave
        ``self.running`` False and the mixer cleanly stopped, so the caller
        (``run_framework_loop_async``) can run its own failure path instead of
        stranding a half-started loop.
        """
        from concurrent.futures import ThreadPoolExecutor

        # Mixer needs its own thread; construct it in an executor so the event
        # loop is never blocked by sounddevice init.
        assert self.mixer is None  # set in start() before _run_loop spawns
        with ThreadPoolExecutor(max_workers=1) as ex:
            self.mixer = await asyncio.get_running_loop().run_in_executor(ex, self._mixer_factory)
        assert self.mixer is not None  # just assigned above (run_in_executor returns Any)
        try:
            self.mixer.start()
        except Exception:
            # B6: never leave a started-but-broken mixer running with the loop
            # task never spawned; stop the half-built mixer, then re-raise so the
            # caller logs + shuts down rather than reporting a healthy set.
            self.running = False
            try:
                self.mixer.stop()
            except Exception as stop_error:  # noqa: BLE001 - report the original failure
                print(f"[AsyncFrameworkLoop] Mixer stop after failed start also failed: {stop_error}")
            raise
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
        self._staged_loop_idx = 0  # B2: nothing staged until P10 queues it

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

                # U4 (REL-04/DPO): audit capture inputs — the enacted stems +
                # per-stem generation outcome. Pregen path: carried on the pregen
                # results; fresh path: captured below from P6/P8. The fabricated
                # loop-1 result carries no outcomes (empty dict is fine: the
                # applied-actions builder defaults missing stems to "cached").
                audit_stems: list = []
                audit_outcomes: dict[int, str] = {}
                if pregen_ready:
                    assert self._pregen_results is not None  # pregen_ready gate (P2)
                    audit_stems = self._pregen_results.get("next_stems", [])
                    audit_outcomes = self._pregen_results.get("stem_outcomes") or {}

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
                    submit = await self._step_submit_jobs(local_next_stems, local_current_bpm, local_current_key)
                    stem_outcomes = await self._step_await_jobs_fetch(
                        submit.pending_jobs, local_next_stems, submit.skipped_idxs
                    )
                    prepared_tracks, loop_duration_samples = await self._step_tile_audio(
                        local_next_stems, local_current_bpm, local_current_key, deduped_tracks
                    )
                    audit_stems, audit_outcomes = local_next_stems, stem_outcomes

                await self._step_append_audit(conductor_response, snap.active_stems, audit_stems, audit_outcomes)
                tracks_to_use, duration_samples = await self._step_commit_to_mixer(
                    pregen_ready, prepared_tracks, loop_duration_samples
                )
                commit = await self._step_commit_state(pregen_ready, tracks_to_use, duration_samples)
                await self._step_post_commit(commit, tracks_to_use, duration_samples)
                await self._step_await_pregen()
                self._consecutive_loop_errors = 0  # REL-18: clean pass resets the backoff ladder
                # B1 (round 3): unconditional suspension point per iteration, so no
                # combination of fast paths can ever turn the driver into a busy
                # spin that starves the event loop (routes, WS, the pre-gen task).
                await asyncio.sleep(0)

            except asyncio.CancelledError:
                # Cancellation (stop/shutdown): clean up, then propagate.
                self._finish_loop()
                raise
            except Exception as e:
                # B1: don't let one bad iteration kill the set permanently.
                # REL-18 (U12): flat 2 s -> exponential with cap + jitter; the
                # ladder resets on the next clean pass.
                self._consecutive_loop_errors += 1
                delay = loop_retry_backoff_delay(self._consecutive_loop_errors)
                print(f"[AsyncFrameworkLoop] Loop iteration error (retry in {delay:.1f}s): {e}")
                import traceback

                traceback.print_exc()
                await asyncio.sleep(delay)
                continue

        self._finish_loop()


async def run_framework_loop_async(session_id: uuid.UUID):
    """
    Async framework loop entry point.

    This is a convenience function that creates and runs an AsyncFrameworkLoop.

    Args:
        session_id: UUID of the session to run
    """
    loop = AsyncFrameworkLoop(session_id)

    try:
        # Round-3 fix B6 (review 01/4): ``await loop.start()`` used to sit OUTSIDE
        # this try, so a mixer init/thread-spawn failure escaped with the framework
        # task finished + an unretrieved exception, state.is_running still True
        # (/api/health lied) and the B1 watchdog (inside _run_loop) never spawned.
        await loop.start()
    except asyncio.CancelledError:
        await loop.stop()
        raise
    except Exception as e:
        import traceback

        print(f"[AsyncFrameworkLoop] Framework startup failed, music loop never started: {e}")
        traceback.print_exc()
        await loop.stop()
        # Flip is_running so /api/health stops claiming a live framework.
        # REL-19 (U12): a MUSICAL failure must not run the whole-app kill
        # switch — trigger_shutdown() would poison audience streams, finalize
        # recordings and kill the YouTube relay; the process is not dying, only
        # the music loop failed to start. Flip the health flag only and keep
        # serving. The exception is NOT re-raised: the lifespan awaits
        # framework_task on shutdown.
        with state.sync_lock:
            state.is_running = False
        return

    try:
        while loop.running:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await loop.stop()
