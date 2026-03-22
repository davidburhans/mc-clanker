import numpy as np
import sounddevice as sd
import threading
import sys
from framework_state import state

class Track:
    def __init__(self, audio_data: np.ndarray, start_sample: int):
        self.audio_data = audio_data
        self.start_sample = start_sample
        self.length = len(audio_data)

class Mixer:
    def __init__(self, sample_rate=44100, blocksize=2048, channels=2):
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.channels = channels
        self.tracks = []
        self.current_sample = 0
        self.lock = threading.Lock()
        self.stream = None

    def add_track(self, audio_data: np.ndarray, start_sample: int):
        with self.lock:
            if audio_data.ndim == 1:
                audio_data = audio_data.reshape(-1, 1)
            # If mono audio but stereo mixer, duplicate to both channels
            if audio_data.shape[1] == 1 and self.channels == 2:
                audio_data = np.repeat(audio_data, 2, axis=1)
                
            self.tracks.append(Track(audio_data, start_sample))

    def _callback(self, outdata, frames, time, status):
        if status:
            print(f"Mixer status: {status}")
        
        # Explicitly clear the output buffer to absolute zero
        outdata.fill(0)
        
        # Only mix if the engine is officially 'Generating/Playing'
        if state.is_generating:
            with self.lock:
                tracks_to_keep = []
                # Dynamic gain based on current track count to prevent clipping
                # We use a slightly warmer scaling (sqrt) to keep volume high
                num_active = len(self.tracks)
                stem_gain = 1.0 / max(1, np.sqrt(num_active))
                
                for track in self.tracks:
                    block_start = self.current_sample
                    block_end = self.current_sample + frames
                    
                    track_start = track.start_sample
                    track_end = track.start_sample + track.length
                    
                    if track_start < block_end and track_end > block_start:
                        out_start = max(0, track_start - block_start)
                        out_end = min(frames, track_end - block_start)
                        
                        in_start = max(0, block_start - track_start)
                        in_end = min(track.length, block_end - track_start)
                        
                        # Perfect channel matching
                        mix_channels = min(outdata.shape[1], track.audio_data.shape[1])
                        outdata[out_start:out_end, :mix_channels] += (track.audio_data[in_start:in_end, :mix_channels] * stem_gain)
                        
                    if track_end > block_start:
                        tracks_to_keep.append(track)
                        
                self.tracks = tracks_to_keep

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
        elif self.stream:
            self.stream.stop()
            self.stream.close()
