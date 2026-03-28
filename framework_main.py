import time
import numpy as np
from framework_mixer import Mixer
from framework_generator import Generator
from framework_conductor import Conductor
from framework_state import state
from datetime import datetime

def calc_duration(bpm, bars, time_signature=4):
    """Calculate the duration in seconds of a given number of bars at a given BPM."""
    beats = bars * time_signature
    seconds = beats / (bpm / 60.0)
    return seconds


def flush_recording_buffers():
    """Batch write buffered interactions/actions to DB."""
    with state.lock:
        if not state.llm_interaction_buffer and not state.action_buffer:
            return
        if state.current_show_id is None:
            state.llm_interaction_buffer.clear()
            state.action_buffer.clear()
            return

    # Import here to avoid circular imports
    from db import DatabaseManager
    from models import LLMInteraction, ShowAction

    db_manager = DatabaseManager.get_instance()
    try:
        with db_manager.session() as session:
            # Bulk insert LLM interactions
            if state.llm_interaction_buffer:
                session.bulk_insert_mappings(LLMInteraction, state.llm_interaction_buffer)
                state.llm_interaction_buffer.clear()

            # Bulk insert actions
            if state.action_buffer:
                session.bulk_insert_mappings(ShowAction, state.action_buffer)
                state.action_buffer.clear()
        print(f"Flushed recording buffers to DB")
    except Exception as e:
        print(f"Error flushing recording buffers: {e}")

def run_framework_loop():
    print("Initializing Mixer...")
    mixer = Mixer(sample_rate=44100, channels=2)
    mixer.start()

    print("Initializing Generator...")
    generator = Generator()

    generator.load()
    print(f"DEBUG: Generator Registry loaded successfully.")

    # Register generator in state so API routes can access it
    with state.lock:
        state.generator = generator
    if mixer.sample_rate != generator.sample_rate:
        print(f"Warning: Mixer SR ({mixer.sample_rate}) != Generator SR ({generator.sample_rate}). Restarting mixer...")
        mixer.stop()
        mixer = Mixer(sample_rate=generator.sample_rate, channels=2)
        mixer.start()

    print("Initializing Conductor...")
    conductor = Conductor()

    with state.lock:
        state.llm_reasoning = "✅ Engine Ready. Configure your BPM/Key and press Start Engine."

    stem_cache = {} # Dict of { cache_key: {"audio_data": np.ndarray, "last_used": time.time()} }

    print("\n--- COMPOSING CONTINUOUS SONG ---")

    try:
        loop_idx = 0
        current_loop_end_sample = 0
        while state.is_running:
            # Wait for user to hit Start
            while not state.is_generating and state.is_running and not state.shutdown_event.is_set():
                time.sleep(0.5)

            if not state.is_running or state.shutdown_event.is_set(): break

            loop_idx += 1
            print(f"\n[Loop {loop_idx}] Requesting track state from LLM Conductor...")

            with state.lock:
                # Handle System Reset
                if state.should_reset:
                    print("SYSTEM RESET TRIGGERED")
                    mixer.clear()
                    stem_cache.clear()
                    state.should_reset = False
                    current_loop_end_sample = 0

                # Check for active overrides BEFORE LLM call
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
                active_stems = state.active_stems
                user_override = state.user_override
                available_instruments = state.available_instruments
                stem_history = state.stem_history

                llm_config = {
                    'base_url': state.llm_base_url,
                    'api_key': state.llm_api_key,
                    'model': state.llm_model
                }

                # Vibe persists until explicitly cleared (don't clear here)

            print(f"DEBUG: Vibe to conductor: '{user_override}'")

            # Retrieve model infos for conductor
            available_models = []
            for model_id, engine in generator.models.items():
                # For registry, models_config contains descriptions
                import json, os
                desc = "No description"
                if os.path.exists("models_config.json"):
                    with open("models_config.json") as f:
                        cfg = json.load(f)
                        m_info = cfg.get("models", {}).get(model_id, {})
                        desc = m_info.get("description", desc)
                        supported_families = m_info.get("supported_families", ["Any"])
                available_models.append({
                    "id": model_id,
                    "description": desc,
                    "supported_families": supported_families
                })

            # 1. Get next section from LLM
            next_state = conductor.get_next_state(
                current_bpm, current_key, active_stems,
                user_override, available_instruments, stem_history, llm_config, available_models
            )

            # 1.5 Buffer LLM interaction if recording
            if state.is_show_recording and state.current_show_id is not None:
                relative_time_ms = int((time.time() - state.current_show_start_time) * 1000) if state.current_show_start_time else 0
                prompt_messages = [
                    {"role": "system", "content": conductor.system_instruction},
                    {"role": "user", "content": conductor.user_message_template.format(
                        bpm=current_bpm,
                        key=current_key,
                        stems="\n".join([f"Index {idx} (age {s.get('_age', 0)}): {s.get('prompt', 'Unknown')}" for idx, s in enumerate(active_stems)]) if active_stems else "None",
                        history=" | ".join(["+".join([st.get('prompt', '').split(',')[0] for st in loop_stems]) for loop_stems in stem_history[-5:]]) if stem_history else "None",
                        instruments=", ".join(available_instruments),
                        models="\n".join([f"- {m['id']}: {m['description']} (Supported Families: {m.get('supported_families', ['Any'])})" for m in available_models]) if available_models else "None",
                        stem_count=len(active_stems),
                        density_directive="This mix is too sparse for a professional sound. Aim for 4-6 stems." if len(active_stems) < 4 else "The mix density is good. Maintain 4-6 stems for a full sound."
                    )}
                ]
                if user_override:
                    prompt_messages[1]["content"] += f"\nOVERRIDE: {user_override}"

                interaction_record = {
                    "show_id": state.current_show_id,
                    "loop_index": loop_idx,
                    "relative_time_ms": relative_time_ms,
                    "prompt_messages": prompt_messages,
                    "parsed_response": next_state,
                    "reasoning": next_state.get("reasoning", ""),
                    "error": None,
                    "was_fallback": False,
                }
                with state.lock:
                    state.llm_interaction_buffer.append(interaction_record)

            # 2. Process Actions to form the new deduped tracklist
            new_tracks = []
            current_actions_log = []

            with state.lock:
                for action in next_state.get("actions", []):
                    a_type = action.get("action_type")
                    idx = action.get("stem_index")

                    if a_type == "retain" and idx is not None and 0 <= idx < len(active_stems):
                        s = active_stems[idx]
                        orig = s.get('_original_details', {})
                        orig['_age'] = s.get('_age', 0) + 1
                        new_tracks.append(orig)
                        current_actions_log.append(f"Retained {s.get('prompt', '').split(',')[1].strip()} (age {orig['_age']})")

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
                            "_age": 0  # New stem starts at age 0
                        })
                        current_actions_log.append(f"Added {sub}")

                    elif a_type == "remove" and idx is not None and 0 <= idx < len(active_stems):
                        s = active_stems[idx]
                        current_actions_log.append(f"Removed {s.get('prompt', '').split(',')[1].strip()}")

                state.last_actions = current_actions_log

                # Buffer ShowAction records if recording
                if state.is_show_recording and state.current_show_id is not None:
                    relative_time_ms = int((time.time() - state.current_show_start_time) * 1000) if state.current_show_start_time else 0
                    for action_idx, action in enumerate(next_state.get("actions", [])):
                        action_type = action.get("action_type")
                        stem_idx = action.get("stem_index")
                        stem_details = None
                        action_desc = current_actions_log[action_idx] if action_idx < len(current_actions_log) else ""

                        if action_type == "add":
                            stem_details = {
                                "model_id": action.get("model_id"),
                                "major_family": action.get("major_family"),
                                "sub_family": action.get("sub_family"),
                                "timbre_tags": action.get("timbre_tags"),
                                "notation_tag": action.get("notation_tag"),
                                "fx_tag": action.get("fx_tag"),
                                "bars": action.get("bars"),
                            }

                        action_record = {
                            "show_id": state.current_show_id,
                            "loop_index": loop_idx,
                            "relative_time_ms": relative_time_ms,
                            "action_type": action_type,
                            "stem_index": stem_idx,
                            "stem_details": stem_details,
                            "action_description": action_desc,
                        }
                        state.action_buffer.append(action_record)

            # 2.5 Deduplicate tracks
            unique_tracks = {}
            for t in new_tracks:
                if not t: continue
                # Incorporate model_id into deduplication key
                m_id = t.get("model_id", "default")
                t_key = f"{m_id}_{t.get('major_family')}_{t.get('sub_family')}_{'_'.join(t.get('timbre_tags', []))}_{t.get('notation_tag')}_{t.get('fx_tag')}"
                if t_key not in unique_tracks:
                    unique_tracks[t_key] = t
            deduped_tracks = list(unique_tracks.values())

            with state.lock:
                # Handle first loop mixer clear
                if loop_idx == 1: mixer.clear()
                # Apply overrides again in case they were set during LLM call
                if bpm_override:
                    state.current_bpm = bpm_override
                else:
                    state.current_bpm = next_state.get("master_bpm", current_bpm)

                if key_override:
                    state.current_key = key_override
                else:
                    state.current_key = next_state.get("master_key", current_key)

                state.current_set_name = next_state.get("name", "Unknown Set")
                state.llm_reasoning = next_state.get("reasoning", "No reasoning provided.")

                # POPULATE UP NEXT SECTION IMMEDIATELY
                state.next_stems = []
                for t in deduped_tracks:
                    m_id = t.get("model_id", generator.default_model_id)
                    engine = generator.models.get(m_id) if m_id else None
                    prompt_template = engine.prompt_template if engine else "{major_family}, {sub_family}, {timbre_tags}, {notation_tag}, {fx_tag}, {key}, {bpm} BPM, {bars} Bars"

                    major = t.get("major_family", "Synth")
                    sub = t.get("sub_family", "Synth Lead")
                    timbres = " ".join(t.get("timbre_tags", ["Warm"]))
                    notation = t.get("notation_tag", "melody")
                    fx = t.get("fx_tag", "Medium Reverb")
                    bars = t.get("bars") or 8

                    constructed_prompt = prompt_template.format(
                        major_family=major,
                        sub_family=sub,
                        timbre_tags=timbres,
                        notation_tag=notation,
                        fx_tag=fx,
                        key=state.current_key,
                        bpm=state.current_bpm,
                        bars=bars
                    )

                    state.next_stems.append({
                        "prompt": constructed_prompt,
                        "model_id": m_id,
                        "bpm": state.current_bpm,
                        "key": state.current_key,
                        "bars": bars,
                        "_original_details": t,
                        "_age": t.get("_age", 0)
                    })
            print(f"Target Master BPM: {state.current_bpm} | Master Key: {state.current_key}")

            # 3. Preparation for generation
            loop_bars = max([(t.get("bars") or 8) for t in deduped_tracks] + [8])
            if state.shutdown_event.is_set(): break

            duration_seconds = calc_duration(state.current_bpm, loop_bars)
            loop_duration_samples = int(duration_seconds * mixer.sample_rate)

            # Pre-allocate tracks_data and map cache Hits vs Misses
            tracks_data = [None] * len(state.next_stems)
            new_generation_requests = []

            for i, t in enumerate(state.next_stems):
                prompt = t["prompt"]
                track_bars = t["bars"]
                m_id = t.get("model_id")
                cache_key = f"{m_id}_{prompt}_{state.current_bpm}_{state.current_key}_{track_bars}"

                if cache_key in stem_cache:
                    print(f"Cache HIT: '{prompt}'")
                    tracks_data[i] = stem_cache[cache_key]["audio_data"]
                    stem_cache[cache_key]["last_used"] = time.time()
                else:
                    new_generation_requests.append({
                        "prompt": prompt,
                        "model_id": m_id,
                        "bars": track_bars,
                        "duration": calc_duration(state.current_bpm, track_bars),
                        "cache_key": cache_key,
                        "original_index": i
                    })

            # 4. Generate NEW stems in BATCH
            if new_generation_requests:
                batch_results, sr = generator.generate_batch(
                    new_generation_requests,
                    bpm=state.current_bpm,
                    cfg_scale=state.generation_cfg_scale,
                    steps=state.generation_steps
                )
                for i, audio_data in enumerate(batch_results):
                    req = new_generation_requests[i]
                    tracks_data[req["original_index"]] = audio_data
                    stem_cache[req["cache_key"]] = {"audio_data": audio_data, "last_used": time.time()}
                    # Store for download later
                    state.last_generated_stems[req["prompt"]] = audio_data

            # Cache maintenance
            current_time = time.time()
            stale_keys = [k for k, v in stem_cache.items() if current_time - v["last_used"] > 300]
            for k in stale_keys: del stem_cache[k]

            # 5. Prepare audio tracks (tile to proper length)
            prepared_tracks = []
            for stem_idx, audio_data in enumerate(tracks_data):
                if audio_data is None:
                    # Fallback for errors
                    audio_data = np.zeros((loop_duration_samples, 2), dtype=np.float32)

                # Ensure each stem fills the entire master loop duration by tiling if necessary
                if len(audio_data) < loop_duration_samples:
                    repeats = (loop_duration_samples // len(audio_data)) + 1
                    tiled_audio = np.tile(audio_data, (repeats, 1))
                    audio_data = tiled_audio[:loop_duration_samples, :]

                prepared_tracks.append((audio_data, stem_idx))

            # 6. Add tracks and set up loop transition
            if loop_idx == 1:
                # First loop: add tracks immediately at sample 0
                for audio_data, stem_idx in prepared_tracks:
                    mixer.add_track(audio_data, 0, stem_index=stem_idx)
                # Set the loop end marker - mixer will know when to look for next loop
                mixer.current_loop_end_sample = mixer.current_sample + loop_duration_samples
                current_loop_end_sample = mixer.current_loop_end_sample
                print(f"Loop {loop_idx} is now playing (ends at sample {current_loop_end_sample})")
            else:
                # Subsequent loops: use set_next_loop for seamless transition
                # Calculate when this loop should end (relative to now)
                new_loop_end_sample = mixer.current_sample + loop_duration_samples
                mixer.set_next_loop(prepared_tracks, new_loop_end_sample)
                current_loop_end_sample = new_loop_end_sample
                print(f"Loop {loop_idx} registered for transition at sample {current_loop_end_sample}")

            # 7. Update active stems
            with state.lock:
                if state.active_stems:
                    state.previous_stems = list(state.active_stems)
                    state.stem_history.append(state.active_stems)
                    if len(state.stem_history) > 8: state.stem_history.pop(0)
                state.active_stems = list(state.next_stems)
                state.next_stems = []
                state.muted_stems.clear()
                state.soloed_stems.clear()
                state.stem_volumes.clear()
                state.loop_count += 1

            # 7.5 Periodic flush of recording buffers (every 10 loops)
            if loop_idx % 10 == 0:
                flush_recording_buffers()

            # 8. Wait until we're within the switch window before generating next loop
            # This ensures we have the next loop ready before the current one ends
            if state.is_running and not state.shutdown_event.is_set():
                while state.is_running:
                    current_ahead = (current_loop_end_sample - mixer.current_sample) / mixer.sample_rate
                    # Start generating when we have less than 2 seconds of buffer ahead
                    if current_ahead < 2.0:
                        break
                    time.sleep(0.25)

    except KeyboardInterrupt:
        print("\nStopping playback...")
    finally:
        import torch
        state.is_running = False
        mixer.stop()
        if hasattr(generator, 'models'):
            for model_id in generator.models:
                generator.unload_model(model_id)
        stem_cache.clear()
        print("Done.")

if __name__ == "__main__":
    run_framework_loop()
