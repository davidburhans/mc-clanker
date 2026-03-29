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
import time
import uuid
import numpy as np
from typing import Optional, List, Dict, Any

from framework_state import state
from framework_mixer import Mixer
from framework_conductor_async import ConductorLLMAsync
from job_waiter import wait_for_multiple_jobs


def calc_duration(bpm: int, bars: int, time_signature: int = 4) -> float:
    """Calculate duration in seconds for a given number of bars at BPM."""
    beats = bars * time_signature
    seconds = beats / (bpm / 60.0)
    return seconds


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
        self.running = False
        self.loop_task: Optional[asyncio.Task] = None
        self.stem_cache: Dict[str, Dict] = {}  # cache_key -> {audio_data, last_used}

    async def start(self):
        """Start the async framework loop."""
        if self.running:
            return

        self.running = True

        # Initialize mixer in async context
        loop = asyncio.get_event_loop()
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
            await asyncio.get_event_loop().run_in_executor(None, self.mixer.stop)

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

                loop_idx += 1
                print(f"\n[AsyncLoop-{loop_idx}] Requesting track state from LLM Conductor...")

                # Step 1: Build Conductor prompt
                with state.lock:
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
                    import os
                    if os.path.exists("models_config.json"):
                        with open("models_config.json") as f:
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
                with state.lock:
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
                with state.lock:
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
                    print(f"Waiting for {len(job_ids)} jobs to complete...")

                    results = await wait_for_multiple_jobs(job_ids, timeout=120.0)

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
                                with state.lock:
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

                if loop_idx == 1:
                    # First loop: add tracks immediately
                    for audio_data, stem_idx in prepared_tracks:
                        self.mixer.add_track(audio_data, 0, stem_index=stem_idx)
                    self.mixer.current_loop_end_sample = self.mixer.current_sample + loop_duration_samples
                    current_loop_end_sample = self.mixer.current_loop_end_sample
                else:
                    # Subsequent loops: seamless transition
                    new_loop_end_sample = self.mixer.current_sample + loop_duration_samples
                    self.mixer.set_next_loop(prepared_tracks, new_loop_end_sample)
                    current_loop_end_sample = new_loop_end_sample

                # Step 10: Update state
                with state.lock:
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

                # Cache maintenance
                current_time = time.time()
                stale_keys = [k for k, v in self.stem_cache.items() if current_time - v["last_used"] > 300]
                for k in stale_keys:
                    del self.stem_cache[k]

                # Step 11: Wait until we need to generate next loop
                if self.running and not state.shutdown_event.is_set():
                    while self.running:
                        current_ahead = (current_loop_end_sample - self.mixer.current_sample) / self.mixer.sample_rate
                        if current_ahead < 2.0:
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
        from datetime import datetime, timedelta
        from models.generator_job import GeneratorJob
        from db import DatabaseManager

        db_manager = DatabaseManager.get_instance()
        expires_at = datetime.utcnow() + timedelta(hours=24)

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
        Fetch audio from Garage.

        In production, this would download from S3/Garage and return numpy array.
        For now, returns None as Garage integration is not implemented.
        """
        # TODO: Implement Garage fetch
        # For now, this is a placeholder that returns None
        print(f"[AsyncFrameworkLoop] Would fetch audio from: {audio_path}")
        return None


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