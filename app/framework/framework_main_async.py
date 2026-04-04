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
import os
import time
import uuid
import numpy as np
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from app.framework.framework_state import state
from app.framework.framework_mixer import Mixer
from app.framework.framework_conductor_async import ConductorLLMAsync
from app.job_waiter import wait_for_multiple_jobs
from app.garage_client import create_garage_client_from_env
from app.aac_encoder import decode_aac


def calc_duration(bpm: int, bars: int, time_signature: int = 4) -> float:
    """Calculate duration in seconds for a given number of bars at BPM."""
    beats = bars * time_signature
    seconds = beats / (bpm / 60.0)
    return seconds


async def flush_recording_buffers():
    """Batch write buffered interactions/actions to DB.

    Called from api_routes.py when a show is stopped to persist
    buffered LLM interactions and actions to the database.
    """
    async with state.lock:
        if not state.llm_interaction_buffer and not state.action_buffer:
            return
        if state.current_show_id is None:
            state.llm_interaction_buffer.clear()
            state.action_buffer.clear()
            return

        # Copy buffers under lock, then release lock before DB I/O
        llm_buffer = state.llm_interaction_buffer[:]
        action_buffer = state.action_buffer[:]
        state.llm_interaction_buffer.clear()
        state.action_buffer.clear()

    if not llm_buffer and not action_buffer:
        return

    # Import here to avoid circular imports
    from app.db import DatabaseManager
    from app.models import LLMInteraction, ShowAction

    db_manager = DatabaseManager.get_instance()
    try:
        with db_manager.session() as session:
            if llm_buffer:
                session.bulk_insert_mappings(LLMInteraction, llm_buffer)
            if action_buffer:
                session.bulk_insert_mappings(ShowAction, action_buffer)
        print("Flushed recording buffers to DB")
    except Exception as e:
        print(f"Error flushing recording buffers: {e}")
        # Put buffers back on failure
        async with state.lock:
            state.llm_interaction_buffer = llm_buffer + state.llm_interaction_buffer
            state.action_buffer = action_buffer + state.action_buffer


def process_actions(actions: List[Dict[str, Any]], active_stems: List[Dict]) -> List[Dict]:
    """Process Conductor DJ actions and return deduplicated track list.

    Mirrors the action processing logic from AsyncFrameworkLoop._run_loop().

    Actions:
    - retain: keep stem, _age+1 (in-place mutation of _original_details)
    - add: new stem with _age=0
    - remove: stem is excluded

    Deduplication key: model_id_major_family_sub_family_timbre_tags_notation_fx

    Returns:
        List of deduplicated track dicts (each with _age set appropriately).
    """
    new_tracks: List[Dict] = []

    for action in actions:
        a_type = action.get("action_type")
        idx = action.get("stem_index")

        if a_type == "retain" and idx is not None and 0 <= idx < len(active_stems):
            s = active_stems[idx]
            orig = s.get('_original_details', {})
            orig['_age'] = s.get('_age', 0) + 1
            new_tracks.append(orig)

        elif a_type == "add":
            major = action.get("major_family", "Synth")
            sub = action.get("sub_family", "Synth Lead")
            new_tracks.append({
                "model_id": action.get("model_id"),
                "major_family": major,
                "sub_family": sub,
                "timbre_tags": action.get("timbre_tags", ["Warm"]),
                "notation_tag": action.get("notation_tag", "melody"),
                "fx_tag": action.get("fx_tag", "Medium Reverb"),
                "bars": action.get("bars", 4),
                "_age": 0
            })

        elif a_type == "remove" and idx is not None and 0 <= idx < len(active_stems):
            # Remove: excluded from new_tracks (no action needed)
            pass

    # Deduplicate
    unique_tracks: Dict[str, Dict] = {}
    for t in new_tracks:
        if not t:
            continue
        m_id = t.get("model_id", "default")
        t_key = (
            f"{m_id}_{t.get('major_family')}_{t.get('sub_family')}_"
            f"{'_'.join(t.get('timbre_tags', []))}_{t.get('notation_tag')}_"
            f"{t.get('fx_tag')}"
        )
        if t_key not in unique_tracks:
            unique_tracks[t_key] = t

    return list(unique_tracks.values())


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
        self.mixer: Optional[Mixer] = None
        self.conductor = ConductorLLMAsync()
        self._garage = None  # Lazy initialization on first use
        self.running = False
        self.loop_task: Optional[asyncio.Task] = None
        self.stem_cache: Dict[str, Dict] = {}  # cache_key -> {audio_data, last_used}
        self._pregen_task: Optional[asyncio.Task] = None  # Background pre-generation task
        self._pregen_done = asyncio.Event()  # Signaled when pre-gen is complete
        self._pregen_loop_idx = 0  # Which loop we're pre-generating for
        self._pregen_results: Optional[Dict[str, Any]] = None  # Results from pre-generation

    @property
    def garage(self):
        """Lazily create and return the Garage client."""
        if self._garage is None:
            self._garage = create_garage_client_from_env()
        return self._garage

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

    async def _run_loop(self):
        """Main async framework loop."""
        loop_idx = 0
        current_loop_end_sample = 0

        try:
            while self.running and state.is_running:
                # Wait for user to hit Start
                while not state.is_generating and self.running and state.is_running and not state.shutdown_event.is_set():
                    await asyncio.sleep(0.5)

                if not self.running or state.shutdown_event.is_set():
                    break

                # Double-check: is_generating might have gone False while we were waiting
                async with state.lock:
                    still_generating = state.is_generating

                if not still_generating:
                    print(f"[AsyncLoop-{loop_idx or 1}] Stop detected before LLM call, returning to wait")
                    continue

                # After exiting wait loop, log why we exited
                async with state.lock:
                    current_gen = state.is_generating
                print(f"[AsyncLoop-{loop_idx}] Exited is_generating wait: is_generating={current_gen}")

                loop_idx += 1
                print(f"\n[AsyncLoop-{loop_idx}] Starting loop...")
                async with state.lock:
                    debug_gen = state.is_generating
                    debug_run = state.is_running
                print(f"[AsyncLoop-{loop_idx}] DEBUG: is_generating={debug_gen}, is_running={debug_run}")

                # PRE-GENERATION PIPELINE: Check if we already have pre-generated results
                # from the previous iteration's background task
                pregen_ready = (
                    loop_idx > 1 and
                    self._pregen_results is not None and
                    self._pregen_results.get('loop_idx') == loop_idx
                )

                if pregen_ready:
                    print(f"[AsyncLoop-{loop_idx}] Using pre-generated audio from background task")
                    print(f"[AsyncLoop-{loop_idx}] DEBUG: pregen_results keys = {list(self._pregen_results.keys()) if self._pregen_results else None}")
                    print(f"[AsyncLoop-{loop_idx}] DEBUG: mixer.current_sample = {self.mixer.current_sample if self.mixer else None}")
                    print(f"[AsyncLoop-{loop_idx}] Using pre-generated audio from background task")
                    # Use the pre-generated results directly
                    # Extract conductor_response-like data from pre_gen_results
                    conductor_response = {
                        'master_bpm': self._pregen_results.get('master_bpm', state.current_bpm),
                        'master_key': self._pregen_results.get('master_key', state.current_key),
                        'name': self._pregen_results.get('set_name', 'Unknown Set'),
                        'reasoning': self._pregen_results.get('reasoning', 'No reasoning provided.'),
                        'actions': self._pregen_results.get('actions', []),
                    }
                    prepared_tracks = self._pregen_results['prepared_tracks']
                    loop_duration_samples = self._pregen_results['loop_duration_samples']
                    next_stems = self._pregen_results['next_stems']
                    active_stems = list(state.active_stems)  # Pre-gen used the previous active_stems
                    # Skip to the section after job submission
                    goto_step_9 = True
                else:
                    async with state.lock:
                        will_call_llm = state.is_generating
                    if will_call_llm:
                        print(f"[AsyncLoop-{loop_idx}] Requesting track state from LLM Conductor...")
                    else:
                        print(f"[AsyncLoop-{loop_idx}] Skipping LLM call: is_generating={state.is_generating}")
                        # Instead of proceeding, go back to waiting
                        continue
                    goto_step_9 = False

                # Step 1: Build Conductor prompt (only if not using pre-gen)
                async with state.lock:
                    if state.should_reset:
                        print("SYSTEM RESET TRIGGERED")
                        self.mixer.clear()
                        self.stem_cache.clear()
                        state.should_reset = False
                        current_loop_end_sample = 0

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
                        'base_url': state.llm_base_url,
                        'api_key': state.llm_api_key,
                        'model': state.llm_model
                    }

                # Get available models
                available_models = []
                generator = getattr(state, 'generator', None)
                if generator and hasattr(generator, 'models'):
                    import json as json_module
                    _config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "models_config.json")
                    if os.path.exists(_config_path):
                        with open(_config_path) as f:
                            cfg = json_module.load(f)
                            for model_id in generator.models:
                                m_info = cfg.get("models", {}).get(model_id, {})
                                desc = m_info.get("description", "No description")
                                supported_families = m_info.get("supported_families", ["Any"])
                                available_models.append({
                                    "id": model_id,
                                    "description": desc,
                                    "supported_families": supported_families
                                })

                # Step 2: Call LLM Conductor (async)
                # Only do this if we don't have pre-generated results
                if not pregen_ready:
                    try:
                        conductor_response = await self.conductor.get_next_state_async(
                            current_bpm=current_bpm,
                            current_key=current_key,
                            active_stems=active_stems,
                            user_override=user_override,
                            available_instruments=available_instruments,
                            stem_history=stem_history,
                            llm_config=llm_config,
                            available_models=available_models
                        )
                    except Exception as e:
                        print(f"LLM call failed: {e}")
                        conductor_response = {
                            "master_bpm": current_bpm,
                            "master_key": current_key,
                            "actions": [{"action_type": "retain", "stem_index": i} for i in range(len(active_stems))],
                            "reasoning": f"LLM failed ({e}). Retaining current groove.",
                            "name": "Fallback State"
                        }

                    # Step 3: Process actions and submit jobs
                    deduped_tracks = process_actions(
                        conductor_response.get("actions", []),
                        active_stems
                    )

                    # Build action log for debugging/auditing
                    async with state.lock:
                        current_actions_log = []
                        for action in conductor_response.get("actions", []):
                            a_type = action.get("action_type")
                            idx = action.get("stem_index")
                            if a_type == "retain" and idx is not None and 0 <= idx < len(active_stems):
                                s = active_stems[idx]
                                current_actions_log.append(f"Retained {s.get('prompt', '').split(',')[1].strip()}")
                            elif a_type == "add":
                                current_actions_log.append(f"Added {action.get('sub_family', '')}")
                            elif a_type == "remove" and idx is not None and 0 <= idx < len(active_stems):
                                s = active_stems[idx]
                                current_actions_log.append(f"Removed {s.get('prompt', '').split(',')[1].strip()}")
                        state.last_actions = current_actions_log

                    # Step 5: Update state with next stems info (before generation)
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
                            state.next_stems.append({
                                "prompt": prompt,
                                "model_id": m_id,
                                "bpm": state.current_bpm,
                                "key": state.current_key,
                                "bars": t.get("bars", 8),
                                "_original_details": t,
                                "_age": t.get("_age", 0)
                            })

                    # Step 6: Submit jobs for new stems
                    pending_jobs = []  # List of (job_id, original_index)

                    for i, t in enumerate(state.next_stems):
                        prompt = t["prompt"]
                        track_bars = t["bars"]
                        m_id = t.get("model_id")
                        cache_key = f"{m_id}_{prompt}_{state.current_bpm}_{state.current_key}_{track_bars}"

                        # Check cache
                        if cache_key in self.stem_cache:
                            print(f"Cache HIT: '{prompt}'")
                            continue  # Already have audio

                        # Submit job
                        job_id = await self._submit_job(
                            session_id=self.session_id,
                            instrument=t.get("sub_family", "Unknown"),
                            prompt=prompt,
                            major_family=t.get("major_family"),
                            model_id=m_id,
                            key=state.current_key,
                            bpm=state.current_bpm,
                            timbre_tags=t.get("timbre_tags", []),
                            bars=track_bars
                        )
                        pending_jobs.append((job_id, i, cache_key))

                    # Step 7: Wait for jobs and collect audio
                    if pending_jobs:
                        job_ids = [job_id for job_id, _, _ in pending_jobs]
                        print(f"[AsyncLoop-{loop_idx}] Waiting for {len(job_ids)} jobs to complete...")
                        wait_start = time.time()

                        results = await wait_for_multiple_jobs(job_ids, timeout=120.0)

                        wait_duration = time.time() - wait_start
                        print(f"[AsyncLoop-{loop_idx}] Jobs completed in {wait_duration:.2f}s")

                        # Process results
                        for (job_id, orig_idx, cache_key), audio_path in zip(pending_jobs, results.values()):
                            if audio_path:
                                # Fetch audio from Garage
                                audio_data = await self._fetch_audio(audio_path)
                                if audio_data is not None:
                                    self.stem_cache[cache_key] = {
                                        "audio_data": audio_data,
                                        "last_used": time.time()
                                    }
                                    # Store in last_generated_stems for download
                                    async with state.lock:
                                        state.last_generated_stems[state.next_stems[orig_idx]["prompt"]] = audio_data
                            else:
                                print(f"Job {job_id} failed or timed out")

                    # Step 8: Prepare audio tracks (tile to length)
                    loop_bars = max([t.get("bars", 8) for t in deduped_tracks] + [8])
                    duration_seconds = calc_duration(state.current_bpm, loop_bars)
                    loop_duration_samples = int(duration_seconds * (self.mixer.sample_rate or 44100))

                    tracks_data = [None] * len(state.next_stems)

                    for i, t in enumerate(state.next_stems):
                        prompt = t["prompt"]
                        track_bars = t["bars"]
                        m_id = t.get("model_id")
                        cache_key = f"{m_id}_{prompt}_{state.current_bpm}_{state.current_key}_{track_bars}"

                        if cache_key in self.stem_cache:
                            audio_data = self.stem_cache[cache_key]["audio_data"]
                        else:
                            audio_data = None

                        if audio_data is not None:
                            # Tile to loop duration
                            if len(audio_data) < loop_duration_samples:
                                repeats = (loop_duration_samples // len(audio_data)) + 1
                                audio_data = np.tile(audio_data, (repeats, 1))[:loop_duration_samples, :]
                            tracks_data[i] = audio_data

                    # Step 9: Add tracks to mixer
                    prepared_tracks = []
                    for audio_data, stem_idx in zip(tracks_data, range(len(tracks_data))):
                        if audio_data is None:
                            audio_data = np.zeros((loop_duration_samples, 2), dtype=np.float32)
                        prepared_tracks.append((audio_data, stem_idx))

                # Step 9/10: Add tracks to mixer (or use pre-generated tracks)
                # When using pre-gen, get prepared_tracks from pregen_results
                if pregen_ready:
                    tracks_to_use = self._pregen_results['prepared_tracks']
                    duration_samples = self._pregen_results['loop_duration_samples']
                else:
                    tracks_to_use = prepared_tracks
                    duration_samples = loop_duration_samples

                if loop_idx == 1:
                    # First loop: add tracks at the mixer's CURRENT position (not 0),
                    # otherwise they are immediately treated as past tracks if generation
                    # took longer than we assumed.
                    with self.mixer.lock:
                        start_sample = self.mixer.current_sample
                        for audio_data, stem_idx in tracks_to_use:
                            self.mixer._add_track_internal(
                                self.mixer._ensure_stereo(audio_data), start_sample, stem_idx
                            )
                        self.mixer.current_loop_end_sample = start_sample + duration_samples
                        self.mixer._current_loop_duration = duration_samples
                    current_loop_end_sample = self.mixer.current_loop_end_sample
                else:
                    # Subsequent loops: queue audio without touching current loop boundary.
                    # The mixer will fire the transition when it reaches current_loop_end_sample
                    # and then set the new boundary from duration_samples.
                    self.mixer.set_next_loop(tracks_to_use, next_loop_duration_samples=duration_samples)
                    with self.mixer.lock:
                        current_loop_end_sample = self.mixer.current_loop_end_sample

                # PRE-GENERATION: Only start if no pre-gen task is running
                needs_pregen = (
                    loop_idx > 1 and
                    (self._pregen_task is None or self._pregen_task.done())
                )

                # Step 10: Update state
                # When using pre-gen, we need to use pregen_results['next_stems'] as our active_stems
                if pregen_ready:
                    # Update state from pre-gen results
                    async with state.lock:
                        if state.active_stems:
                            state.previous_stems = list(state.active_stems)
                            state.stem_history.append(state.active_stems)
                            if len(state.stem_history) > 8:
                                state.stem_history.pop(0)

                        state.active_stems = list(self._pregen_results['next_stems'])
                        state.next_stems = []
                        state.muted_stems.clear()
                        state.soloed_stems.clear()
                        state.stem_volumes.clear()
                        state.loop_count += 1

                        # Update BPM, key, etc. from pre-gen results
                        state.current_bpm = self._pregen_results.get('master_bpm', state.current_bpm)
                        state.current_key = self._pregen_results.get('master_key', state.current_key)
                        state.current_set_name = self._pregen_results.get('set_name', 'Unknown Set')
                        state.llm_reasoning = self._pregen_results.get('reasoning', 'No reasoning provided.')

                        # Take state snapshot for pre-generation (before releasing lock)
                        state_snapshot = {
                            'current_bpm': state.current_bpm,
                            'current_key': state.current_key,
                            'active_stems': list(state.active_stems),
                            'user_override': state.user_override,
                            'available_instruments': list(state.available_instruments),
                            'stem_history': list(state.stem_history),
                            'llm_config': {
                                'base_url': state.llm_base_url,
                                'api_key': state.llm_api_key,
                                'model': state.llm_model
                            }
                        }
                else:
                    async with state.lock:
                        if state.active_stems:
                            state.previous_stems = list(state.active_stems)
                            state.stem_history.append(state.active_stems)
                            if len(state.stem_history) > 8:
                                state.stem_history.pop(0)

                        state.active_stems = list(state.next_stems)
                        state.next_stems = []
                        state.muted_stems.clear()
                        state.soloed_stems.clear()
                        state.stem_volumes.clear()
                        state.loop_count += 1

                        # Take state snapshot for pre-generation (before releasing lock)
                        state_snapshot = {
                            'current_bpm': state.current_bpm,
                            'current_key': state.current_key,
                            'active_stems': list(state.active_stems),
                            'user_override': state.user_override,
                            'available_instruments': list(state.available_instruments),
                            'stem_history': list(state.stem_history),
                            'llm_config': {
                                'base_url': state.llm_base_url,
                                'api_key': state.llm_api_key,
                                'model': state.llm_model
                            }
                        }

                # Cache maintenance
                current_time = time.time()
                stale_keys = [k for k, v in self.stem_cache.items() if current_time - v["last_used"] > 300]
                for k in stale_keys:
                    del self.stem_cache[k]

                # PRE-GENERATION: Only start if we don't have a loop already queued
                # and no pre-gen task is running
                if needs_pregen:
                    next_loop_idx = loop_idx + 1
                    print(f"[AsyncLoop-{loop_idx}] No loop queued, starting pre-generation for loop {next_loop_idx}...")
                    self._pregen_done.clear()
                    self._pregen_results = None
                    self._pregen_task = asyncio.create_task(
                        self._pre_generate_next_loop(next_loop_idx, state_snapshot)
                    )
                else:
                    print(f"[AsyncLoop-{loop_idx}] Loop {loop_idx + 1} already queued, skipping pre-gen")
                    # Signal that pre-gen is "done" - the loop is queued in the mixer
                    self._pregen_done.set()
                    # Update _pregen_results to reflect the queued loop
                    # Use state.next_stems which has the stems for the queued loop
                    self._pregen_results = {
                        'loop_idx': loop_idx + 1,
                        'prepared_tracks': tracks_to_use,
                        'loop_duration_samples': duration_samples,
                        'next_stems': list(state.next_stems),
                    }

                # Step 11: Wait until we need to generate next loop
                # Wait for pre-generation to complete (it runs the LLM call for us)
                if self.running and not state.shutdown_event.is_set():
                    while self.running:
                        # Check if pre-gen is done first
                        if self._pregen_done.is_set():
                            print(f"[AsyncLoop-{loop_idx}] Pre-generation complete, using results")
                            break

                        # Read current boundary from the mixer (under lock so we see
                        # transitions that may have already fired).
                        with self.mixer.lock:
                            live_end = self.mixer.current_loop_end_sample
                            live_pos = self.mixer.current_sample
                        current_ahead = (live_end - live_pos) / self.mixer.sample_rate
                        if loop_idx > 1:
                            print(f"[AsyncLoop-{loop_idx}] DEBUG: current_ahead={current_ahead:.2f}s, waiting for pre-gen...")
                        if current_ahead < 0.5:
                            # Still waiting for pre-gen, but we need to break to avoid missing the loop transition
                            break
                        await asyncio.sleep(0.25)

        except asyncio.CancelledError:
            print("\n[AsyncFrameworkLoop] Cancelled")
        except Exception as e:
            print(f"\n[AsyncFrameworkLoop] Error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.running = False
            if self.mixer:
                self.mixer.stop()
            print("[AsyncFrameworkLoop] Done")

    def _build_prompt(self, track: Dict, key: str, bpm: int) -> str:
        """Build generation prompt from track details."""
        generator = getattr(state, 'generator', None)
        m_id = track.get("model_id", "foundation-1")

        if generator and m_id in generator.models:
            engine = generator.models[m_id]
            prompt_template = getattr(engine, 'prompt_template', None)
        else:
            prompt_template = None

        if not prompt_template:
            prompt_template = "{major_family}, {sub_family}, {timbre_tags}, {notation_tag}, {fx_tag}, {key}, {bpm} BPM, {bars} Bars"

        major = track.get("major_family", "Synth")
        sub = track.get("sub_family", "Synth Lead")
        timbres = " ".join(track.get("timbre_tags", ["Warm"]))
        notation = track.get("notation_tag", "melody")
        fx = track.get("fx_tag", "Medium Reverb")
        bars = track.get("bars", 8)

        return prompt_template.format(
            major_family=major,
            sub_family=sub,
            timbre_tags=timbres,
            notation_tag=notation,
            fx_tag=fx,
            key=key,
            bpm=bpm,
            bars=bars
        )

    async def _submit_job(
        self,
        session_id: uuid.UUID,
        instrument: str,
        prompt: str,
        major_family: str,
        model_id: str,
        key: str,
        bpm: int,
        timbre_tags: List[str],
        bars: int
    ) -> uuid.UUID:
        """
        Submit a generation job to the queue.

        Returns the job UUID.
        """
        from app.models.generator_job import GeneratorJob
        from app.db import DatabaseManager

        db_manager = DatabaseManager.get_instance()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

        with db_manager.session() as session:
            job = GeneratorJob(
                session_id=session_id,
                instrument=instrument,
                prompt=prompt,
                major_family=major_family,
                model_id=model_id,
                key=key,
                bpm=bpm,
                timbre_tags=timbre_tags,
                bars=bars,
                status="pending",
                expires_at=expires_at
            )
            session.add(job)
            session.flush()
            session.refresh(job)
            job_id = job.id

        print(f"[AsyncFrameworkLoop] Submitted job {job_id}: {instrument}")
        return job_id

    async def _fetch_audio(self, audio_path: str) -> Optional[np.ndarray]:
        """
        Fetch audio from Garage and decode to numpy array.

        Args:
            audio_path: Garage S3 path (e.g., "audio/{job_id}.aac")

        Returns:
            numpy array of audio samples (float32, shape [samples, channels])
            or None if fetch/decode fails
        """
        try:
            # Reuse Garage client from self.garage (created once in __init__)
            aac_bytes = await self.garage.get_object(audio_path)

            if not aac_bytes:
                print(f"[AsyncFrameworkLoop] No audio data received from Garage: {audio_path}")
                return None

            # Decode AAC to numpy array (runs in thread pool since it's blocking)
            loop = asyncio.get_running_loop()
            audio_data = await loop.run_in_executor(
                None,
                lambda: decode_aac(aac_bytes, sample_rate=44100)
            )

            print(f"[AsyncFrameworkLoop] Fetched and decoded audio from: {audio_path}")
            return audio_data

        except Exception as e:
            print(f"[AsyncFrameworkLoop] Failed to fetch audio from Garage: {e}")
            return None


    async def _pre_generate_next_loop(self, for_loop_idx: int, snapshot: Dict[str, Any]):
        """
        Pre-generate the next loop's audio in the background.

        This runs the LLM call and job submission for the next loop while
        the current loop is playing. This allows us to have the next loop's
        audio ready before the current loop ends.

        Args:
            for_loop_idx: The loop index this pre-generation is for
            snapshot: State snapshot taken at the time of pre-gen initiation
        """
        print(f"[AsyncFrameworkLoop] Pre-generating loop {for_loop_idx} in background...")

        try:
            # Build prompt from snapshot
            current_bpm = snapshot['current_bpm']
            current_key = snapshot['current_key']
            active_stems = snapshot['active_stems']
            llm_config = snapshot['llm_config']

            # Get available models
            available_models = []
            generator = getattr(state, 'generator', None)
            if generator and hasattr(generator, 'models'):
                import json as json_module
                _config_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "models_config.json")
                if os.path.exists(_config_path):
                    with open(_config_path) as f:
                        cfg = json_module.load(f)
                        for model_id in generator.models:
                            m_info = cfg.get("models", {}).get(model_id, {})
                            desc = m_info.get("description", "No description")
                            supported_families = m_info.get("supported_families", ["Any"])
                            available_models.append({
                                "id": model_id,
                                "description": desc,
                                "supported_families": supported_families
                            })

            # Call LLM
            try:
                conductor_response = await self.conductor.get_next_state_async(
                    current_bpm=current_bpm,
                    current_key=current_key,
                    active_stems=active_stems,
                    user_override=snapshot.get('user_override'),
                    available_instruments=snapshot.get('available_instruments', []),
                    stem_history=snapshot.get('stem_history', []),
                    llm_config=llm_config,
                    available_models=available_models
                )
            except Exception as e:
                print(f"[AsyncFrameworkLoop] Pre-gen LLM call failed: {e}")
                conductor_response = {
                    "master_bpm": current_bpm,
                    "master_key": current_key,
                    "actions": [{"action_type": "retain", "stem_index": i} for i in range(len(active_stems))],
                    "reasoning": f"LLM failed ({e}). Retaining current groove.",
                    "name": "Fallback State"
                }

            # Process actions
            deduped_tracks = process_actions(
                conductor_response.get("actions", []),
                active_stems
            )

            # Build next stems info
            next_stems = []
            for t in deduped_tracks:
                m_id = t.get("model_id", "foundation-1")
                prompt = self._build_prompt(t, current_key, current_bpm)
                next_stems.append({
                    "prompt": prompt,
                    "model_id": m_id,
                    "bpm": current_bpm,
                    "key": current_key,
                    "bars": t.get("bars", 8),
                    "_original_details": t,
                    "_age": t.get("_age", 0)
                })

            # Submit jobs
            pending_jobs = []
            for i, t in enumerate(next_stems):
                prompt = t["prompt"]
                track_bars = t["bars"]
                m_id = t.get("model_id")
                cache_key = f"{m_id}_{prompt}_{current_bpm}_{current_key}_{track_bars}"

                if cache_key in self.stem_cache:
                    continue

                job_id = await self._submit_job(
                    session_id=self.session_id,
                    instrument=t.get("sub_family", "Unknown"),
                    prompt=prompt,
                    major_family=t.get("major_family"),
                    model_id=m_id,
                    key=current_key,
                    bpm=current_bpm,
                    timbre_tags=t.get("timbre_tags", []),
                    bars=track_bars
                )
                pending_jobs.append((job_id, i, cache_key))

            # Wait for jobs
            if pending_jobs:
                job_ids = [job_id for job_id, _, _ in pending_jobs]
                results = await wait_for_multiple_jobs(job_ids, timeout=120.0)

                # Fetch audio
                for (job_id, orig_idx, cache_key) in pending_jobs:
                    audio_path = results.get(job_id)
                    if audio_path:
                        audio_data = await self._fetch_audio(audio_path)
                        if audio_data is not None:
                            self.stem_cache[cache_key] = {
                                "audio_data": audio_data,
                                "last_used": time.time()
                            }

            # Prepare tracks
            loop_bars = max([t.get("bars", 8) for t in deduped_tracks] + [8])
            duration_seconds = calc_duration(current_bpm, loop_bars)
            loop_duration_samples = int(duration_seconds * 44100)

            tracks_data = [None] * len(next_stems)
            for i, t in enumerate(next_stems):
                prompt = t["prompt"]
                track_bars = t["bars"]
                m_id = t.get("model_id")
                cache_key = f"{m_id}_{prompt}_{current_bpm}_{current_key}_{track_bars}"

                if cache_key in self.stem_cache:
                    audio_data = self.stem_cache[cache_key]["audio_data"]
                else:
                    audio_data = None

                if audio_data is not None:
                    if len(audio_data) < loop_duration_samples:
                        repeats = (loop_duration_samples // len(audio_data)) + 1
                        audio_data = np.tile(audio_data, (repeats, 1))[:loop_duration_samples, :]
                    tracks_data[i] = audio_data

            # Build prepared tracks
            prepared_tracks = []
            for audio_data, stem_idx in zip(tracks_data, range(len(tracks_data))):
                if audio_data is None:
                    audio_data = np.zeros((loop_duration_samples, 2), dtype=np.float32)
                prepared_tracks.append((audio_data, stem_idx))

            # Store results for the main loop to use
            self._pregen_results = {
                'prepared_tracks': prepared_tracks,
                'loop_duration_samples': loop_duration_samples,
                'loop_idx': for_loop_idx,
                'next_stems': next_stems,
                'master_bpm': conductor_response.get("master_bpm", current_bpm),
                'master_key': conductor_response.get("master_key", current_key),
                'set_name': conductor_response.get("name", "Unknown Set"),
                'reasoning': conductor_response.get("reasoning", "No reasoning provided."),
                'actions': conductor_response.get("actions", []),
            }
            self._pregen_done.set()
            print(f"[AsyncFrameworkLoop] Pre-generation for loop {for_loop_idx} complete!")

        except Exception as e:
            print(f"[AsyncFrameworkLoop] Pre-generation error: {e}")
            import traceback
            traceback.print_exc()
            self._pregen_results = None
            self._pregen_done.set()


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