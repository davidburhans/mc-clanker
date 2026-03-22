import time
import numpy as np
from framework_mixer import Mixer
from framework_generator import Generator
from framework_conductor import Conductor
from framework_state import state

def calc_duration(bpm, bars, time_signature=4):
    """Calculate the duration in seconds of a given number of bars at a given BPM."""
    beats = bars * time_signature
    seconds = beats / (bpm / 60.0)
    return seconds

def run_framework_loop():
    print("Initializing Mixer...")
    mixer = Mixer(sample_rate=44100, channels=2)
    mixer.start()
    
    print("Initializing Generator...")
    generator = Generator()

    generator.load()
    print(f"DEBUG: Foundation-1 model loaded successfully on device: {generator.device}")
    if mixer.sample_rate != generator.sample_rate:
        print(f"Warning: Mixer SR ({mixer.sample_rate}) != Generator SR ({generator.sample_rate}). Restarting mixer...")
        mixer.stop()
        mixer = Mixer(sample_rate=generator.sample_rate, channels=2)
        mixer.start()
        
    print("Initializing Conductor...")
    conductor = Conductor()
    
    with state.lock:
        state.llm_reasoning = "✅ Engine Ready. Configure your BPM/Key and press Start Engine."

    next_loop_start_sample = 0
    stem_cache = {} # Dict of { cache_key: {"audio_data": np.ndarray, "last_used": time.time()} }

    print("\n--- COMPOSING CONTINUOUS SONG ---")
    
    try:
        loop_idx = 0
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
                    next_loop_start_sample = mixer.current_sample + int(mixer.sample_rate * 0.1)
                
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
                
                state.user_override = ""
                
            # 1. Get next section from LLM
            next_state = conductor.get_next_state(
                current_bpm, current_key, active_stems,
                user_override, available_instruments, stem_history, llm_config,
                energy_level=state.energy_level
            )

            # 2. Process Actions to form the new deduped tracklist
            new_tracks = []
            current_actions_log = []

            with state.lock:
                state.energy_level = next_state.get("next_energy_level", state.energy_level)

                for action in next_state.get("actions", []):
                    a_type = action.get("action_type")
                    idx = action.get("stem_index")

                    if a_type == "retain" and idx is not None and 0 <= idx < len(active_stems):
                        # Use the original generated details or reconstruct a partial object
                        s = active_stems[idx]
                        new_tracks.append(s.get('_original_details', {}))
                        current_actions_log.append(f"Retained {s.get('prompt', '').split(',')[1].strip()}")

                    elif a_type == "add":
                        major = action.get("major_family", "Synth")
                        sub = action.get("sub_family", "Synth Lead")
                        new_tracks.append({
                            "major_family": major,
                            "sub_family": sub,
                            "timbre_tags": action.get("timbre_tags", ["Warm"]),
                            "notation_tag": action.get("notation_tag", "melody"),
                            "fx_tag": action.get("fx_tag", "Medium Reverb"),
                            "bars": action.get("bars", 4)
                        })
                        current_actions_log.append(f"Added {sub}")

                    elif a_type == "mix" and idx is not None and 0 <= idx < len(active_stems):
                        s = active_stems[idx]
                        vol = action.get("target_volume")
                        log_msg = f"Mixed {s.get('prompt', '').split(',')[1].strip()}"

                        if vol is not None:
                            state.stem_volumes[idx] = max(0.0, min(2.0, vol))
                            log_msg += f" to {int(vol*100)}%"

                        muted = action.get("is_muted")
                        if muted is True:
                            state.muted_stems.add(idx)
                            log_msg += " (Muted)"
                        elif muted is False and idx in state.muted_stems:
                            state.muted_stems.remove(idx)
                            log_msg += " (Unmuted)"

                        current_actions_log.append(log_msg)
                        new_tracks.append(s.get('_original_details', {}))

                    elif a_type == "remove" and idx is not None and 0 <= idx < len(active_stems):
                        s = active_stems[idx]
                        current_actions_log.append(f"Removed {s.get('prompt', '').split(',')[1].strip()}")

                state.last_actions = current_actions_log
            # 2.5 Deduplicate tracks
            unique_tracks = {}
            for t in new_tracks:
                if not t: continue
                t_key = f"{t.get('major_family')}_{t.get('sub_family')}_{'_'.join(t.get('timbre_tags', []))}_{t.get('notation_tag')}_{t.get('fx_tag')}"
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
                    major = t.get("major_family", "Synth")
                    sub = t.get("sub_family", "Synth Lead")
                    timbres = ", ".join(t.get("timbre_tags", ["Warm"]))
                    notation = t.get("notation_tag", "melody")
                    fx = t.get("fx_tag", "Medium Reverb")
                    constructed_prompt = f"{major}, {sub}, {timbres}, {notation}, {fx}, {state.current_key}"

                    state.next_stems.append({
                        "prompt": constructed_prompt,
                        "bpm": state.current_bpm,
                        "key": state.current_key,
                        "bars": t.get("bars", 8),
                        "_original_details": t # Save for retain actions
                    })
            print(f"Target Master BPM: {state.current_bpm} | Master Key: {state.current_key}")
            
            # 3. Preparation for generation
            loop_bars = max([t.get("bars", 8) for t in deduped_tracks] + [8])
            if state.shutdown_event.is_set(): break
            
            duration_seconds = calc_duration(state.current_bpm, loop_bars)
            loop_duration_samples = int(duration_seconds * mixer.sample_rate)
            
            # Pre-allocate tracks_data and map cache Hits vs Misses
            tracks_data = [None] * len(state.next_stems)
            new_generation_requests = []
            
            for i, t in enumerate(state.next_stems):
                prompt = t["prompt"]
                track_bars = t["bars"]
                cache_key = f"{prompt}_{state.current_bpm}_{state.current_key}_{track_bars}"
                
                if cache_key in stem_cache:
                    print(f"Cache HIT: '{prompt}'")
                    tracks_data[i] = stem_cache[cache_key]["audio_data"]
                    stem_cache[cache_key]["last_used"] = time.time()
                else:
                    new_generation_requests.append({
                        "prompt": prompt,
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
                
            # Reset timeline if first loop or if we fell behind
            if loop_idx == 1 or mixer.current_sample > next_loop_start_sample:
                next_loop_start_sample = mixer.current_sample + int(mixer.sample_rate * 0.1)
                
            for stem_idx, audio_data in enumerate(tracks_data):
                # Ensure each stem fills the entire master loop duration by tiling if necessary
                if len(audio_data) < loop_duration_samples:
                    # Tile (repeat) the audio until it meets or exceeds the required length
                    repeats = (loop_duration_samples // len(audio_data)) + 1
                    tiled_audio = np.tile(audio_data, (repeats, 1))
                    audio_data = tiled_audio[:loop_duration_samples, :]
                
                mixer.add_track(audio_data, next_loop_start_sample, stem_index=stem_idx)
            
            # 6. Wait for the loop to actually start playing
            samples_until_start = next_loop_start_sample - mixer.current_sample
            if samples_until_start > 0:
                time.sleep(samples_until_start / mixer.sample_rate)
                
            # 7. Update active stems ONLY when the loop starts playing
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
                
            print(f"Loop {loop_idx} is now playing!")
            
            # Move playhead for next loop
            next_loop_start_sample += loop_duration_samples
            
            # 8. Buffer control: only sleep if we are already buffered ahead
            # We want to start the NEXT generation as soon as possible.
            with state.lock:
                state.loop_count += 1
            while state.is_running:
                current_ahead = (next_loop_start_sample - mixer.current_sample) / mixer.sample_rate
                if current_ahead > (duration_seconds * 1.1):
                    time.sleep(0.5)
                else:
                    break

    except KeyboardInterrupt:
        print("\nStopping playback...")
    finally:
        import torch
        state.is_running = False
        mixer.stop()
        if hasattr(generator, 'model') and generator.model is not None:
            del generator.model
            torch.cuda.empty_cache()
        stem_cache.clear()
        print("Done.")

if __name__ == "__main__":
    run_framework_loop()
