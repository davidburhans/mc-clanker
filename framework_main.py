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
            while not state.is_generating and state.is_running:
                time.sleep(0.5)
                
            if not state.is_running: break
            
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
                user_override, available_instruments, stem_history, llm_config
            )
            
            # 2. Deduplicate tracks
            unique_tracks = {}
            for t in next_state.get("tracks", []):
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
                        "bars": t.get("bars", 8)
                    })

            print(f"Target Master BPM: {state.current_bpm} | Master Key: {state.current_key}")
            
            # 3. Preparation for generation
            loop_bars = max([t.get("bars", 8) for t in deduped_tracks] + [8])
            duration_seconds = calc_duration(state.current_bpm, loop_bars)
            loop_duration_samples = int(duration_seconds * mixer.sample_rate)
            
            tracks_data = []
            new_generation_requests = []
            
            for t in state.next_stems:
                prompt = t["prompt"]
                track_bars = t["bars"]
                cache_key = f"{prompt}_{state.current_bpm}_{state.current_key}_{track_bars}"
                
                if cache_key in stem_cache:
                    print(f"Cache HIT: '{prompt}'")
                    tracks_data.append(stem_cache[cache_key]["audio_data"])
                    stem_cache[cache_key]["last_used"] = time.time()
                else:
                    new_generation_requests.append({
                        "prompt": prompt,
                        "bars": track_bars,
                        "duration": calc_duration(state.current_bpm, track_bars),
                        "cache_key": cache_key
                    })
            
            # 4. Generate NEW stems in BATCH
            if new_generation_requests:
                batch_results, sr = generator.generate_batch(
                    new_generation_requests,
                    bpm=state.current_bpm
                )
                for i, audio_data in enumerate(batch_results):
                    tracks_data.append(audio_data)
                    stem_cache[new_generation_requests[i]["cache_key"]] = {"audio_data": audio_data, "last_used": time.time()}
            
            # Cache maintenance
            current_time = time.time()
            stale_keys = [k for k, v in stem_cache.items() if current_time - v["last_used"] > 300]
            for k in stale_keys: del stem_cache[k]
                
            # Reset timeline if first loop or if we fell behind
            if loop_idx == 1 or mixer.current_sample > next_loop_start_sample:
                next_loop_start_sample = mixer.current_sample + int(mixer.sample_rate * 0.1)
                
            for audio_data in tracks_data:
                # Ensure each stem fills the entire master loop duration by tiling if necessary
                if len(audio_data) < loop_duration_samples:
                    # Tile (repeat) the audio until it meets or exceeds the required length
                    repeats = (loop_duration_samples // len(audio_data)) + 1
                    tiled_audio = np.tile(audio_data, (repeats, 1))
                    audio_data = tiled_audio[:loop_duration_samples, :]
                
                mixer.add_track(audio_data, next_loop_start_sample)
            
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
                
            print(f"Loop {loop_idx} is now playing!")
            
            # Move playhead for next loop
            next_loop_start_sample += loop_duration_samples
            
            # 8. Buffer control: only sleep if we are already buffered ahead
            # We want to start the NEXT generation as soon as possible.
            while state.is_running:
                current_ahead = (next_loop_start_sample - mixer.current_sample) / mixer.sample_rate
                if current_ahead > (duration_seconds * 1.1):
                    time.sleep(0.5)
                else:
                    break

    except KeyboardInterrupt:
        print("\nStopping playback...")
    finally:
        state.is_running = False
        mixer.stop()
        print("Done.")

if __name__ == "__main__":
    run_framework_loop()
