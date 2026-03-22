import numpy as np
import sounddevice as sd
import threading
import sys
import os
from framework_state import state

class Track:
    def __init__(self, audio_data: np.ndarray, start_sample: int, stem_index: int = -1):
        self.audio_data = audio_data
        self.start_sample = start_sample
        self.length = len(audio_data)
        self.stem_index = stem_index  # Index in state.active_stems; -1 = historical/unknown

class Mixer:
    def __init__(self, sample_rate=44100, blocksize=2048, channels=2):
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.channels = channels
        self.tracks = []
        self.current_sample = 0
        self.lock = threading.Lock()
        self.stream = None

    def add_track(self, audio_data: np.ndarray, start_sample: int, stem_index: int = -1):
        with self.lock:
            if audio_data.ndim == 1:
                audio_data = audio_data.reshape(-1, 1)
            # If mono audio but stereo mixer, duplicate to both channels
            if audio_data.shape[1] == 1 and self.channels == 2:
                audio_data = np.repeat(audio_data, 2, axis=1)
                
            self.tracks.append(Track(audio_data, start_sample, stem_index))

    def _callback(self, outdata, frames, time, status):
        if status:
            print(f"Mixer status: {status}")
        
        # Explicitly clear the output buffer to absolute zero
        outdata.fill(0)
        
        # Only mix if the engine is officially 'Generating/Playing'
        if state.is_generating:
            with self.lock:
                tracks_to_mix = []
                tracks_to_keep = []
                
                # Pruning logic: keep all future tracks + current playing + 2 historical tracks
                past_tracks = [t for t in self.tracks if (t.start_sample + t.length) <= self.current_sample]
                past_tracks.sort(key=lambda t: t.start_sample + t.length, reverse=True)
                
                # Keep top 2 most recent past tracks
                history_to_keep = past_tracks[:2]
                
                # Ongoing and future tracks
                future_tracks = [t for t in self.tracks if (t.start_sample + t.length) > self.current_sample]
                
                self.tracks = history_to_keep + future_tracks
                
                # Now identify what to actually MIX (current sample window)
                tracks_to_mix = []
                for i, track in enumerate(self.tracks):
                    if track.start_sample < (self.current_sample + frames) and (track.start_sample + track.length) > self.current_sample:
                        tracks_to_mix.append((i, track))
                
                # Handle Mute/Solo using stem_index (not track list position)
                solo_active = len(state.soloed_stems) > 0
                
                final_mix_list = []
                for i, track in tracks_to_mix:
                    stem_idx = track.stem_index
                    is_muted = stem_idx in state.muted_stems
                    is_soloed = stem_idx in state.soloed_stems
                    
                    if solo_active:
                        if is_soloed:
                            final_mix_list.append((i, track))
                    elif not is_muted:
                        final_mix_list.append((i, track))

                # Dynamic gain based on current mix count to prevent clipping
                num_active = len(final_mix_list)
                stem_gain_global = 1.0 / max(1, np.sqrt(num_active))
                
                for i, track in final_mix_list:
                    block_start = self.current_sample
                    block_end = self.current_sample + frames
                    
                    track_start = track.start_sample
                    track_end = track.start_sample + track.length
                    
                    # Individual stem volume looked up by stem_index
                    indiv_gain = state.stem_volumes.get(track.stem_index, 1.0)
                    total_gain = stem_gain_global * indiv_gain
                    
                    if track_start < block_end and track_end > block_start:
                        out_start = max(0, track_start - block_start)
                        out_end = min(frames, track_end - block_start)
                        
                        in_start = max(0, block_start - track_start)
                        in_end = min(track.length, block_end - track_start)
                        
                        # Perfect channel matching
                        mix_channels = min(outdata.shape[1], track.audio_data.shape[1])
                        outdata[out_start:out_end, :mix_channels] += (track.audio_data[in_start:in_end, :mix_channels] * total_gain)
                        
            self.current_sample += frames

        # Debug logging (every 50 callbacks to avoid spam)
        if not hasattr(self, '_debug_count'):
            self._debug_count = 0
        self._debug_count += 1
        if self._debug_count % 50 == 0:
            is_gen = state.is_generating
            num_tracks = len(self.tracks) if hasattr(self, 'tracks') else 0
            out_min = outdata.min()
            out_max = outdata.max()
            out_mean = outdata.mean()
            print(f"DEBUG Mixer: is_generating={is_gen}, tracks={num_tracks}, outdata range=[{out_min:.4f}, {out_max:.4f}], mean={out_mean:.6f}")

        # Final safety clip and broadcast
        # Explicitly use little-endian byte order to match ffmpeg's -f s16le
        pcm_out = np.clip(outdata, -1.0, 1.0)
        pcm_int16 = (pcm_out * 32767).astype('<i2').tobytes()
        state.broadcast_audio(pcm_int16)

    def start(self):
        # Check if local audio is disabled (useful for containerized/WSL environments)
        if os.environ.get("DISABLE_LOCAL_AUDIO") == "1":
            print("DISABLE_LOCAL_AUDIO is set. Skipping local audio output and using mock playback.")
            self.is_mock = True
            self._running = True
            self.mock_thread = threading.Thread(target=self._mock_playback_loop, daemon=True)
            self.mock_thread.start()
            print("Mock audio playback thread started")
            return

        try:
            print(f"Attempting to open audio output stream (SR={self.sample_rate}, channels={self.channels})...")
            self.stream = sd.OutputStream(
                samplerate=self.sample_rate,
                blocksize=self.blocksize,
                channels=self.channels,
                callback=self._callback
            )
            self.stream.start()
            self.is_mock = False
            print("Audio output stream opened successfully")
        except Exception as e:
            print(f"WARNING: Could not open audio output stream: {e}")
            print("This is expected in containerized environments without audio hardware.")
            print("Falling back to mock audio playback (simulated timing).")
            self.is_mock = True
            self._running = True
            self.mock_thread = threading.Thread(target=self._mock_playback_loop, daemon=True)
            self.mock_thread.start()
            print("Mock audio playback thread started")

    def _mock_playback_loop(self):
        import time
        sleep_time = self.blocksize / self.sample_rate
        outdata = np.zeros((self.blocksize, self.channels), dtype=np.float32)
        while self._running:
            time.sleep(sleep_time)
            # Call the callback to advance frames and clean up old tracks
            self._callback(outdata, self.blocksize, None, None)

    def clear(self):
        with self.lock:
            self.tracks = []
            self.current_sample = 0

    def stop(self):
        if hasattr(self, 'is_mock') and self.is_mock:
            self._running = False
            if hasattr(self, 'mock_thread'):
                self.mock_thread.join(timeout=1.0)
        elif hasattr(self, 'stream') and self.stream:
            self.stream.stop()
            self.stream.close()
        
        with self.lock:
            self.tracks = []
