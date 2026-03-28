import numpy as np
import threading
import time
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
        # Loop transition state
        self.current_loop_end_sample = 0
        self.next_loop_audio = []  # List of (audio_data, stem_index) ready to play
        self.loop_switch_deadline_ms = 50  # Switch within this window before loop end

    def add_track(self, audio_data: np.ndarray, start_sample: int, stem_index: int = -1):
        """Add a track to be mixed. For the first loop, start_sample should be 0."""
        with self.lock:
            if audio_data.ndim == 1:
                audio_data = audio_data.reshape(-1, 1)
            # If mono audio but stereo mixer, duplicate to both channels
            if audio_data.shape[1] == 1 and self.channels == 2:
                audio_data = np.repeat(audio_data, 2, axis=1)

            self.tracks.append(Track(audio_data, start_sample, stem_index))

    def _add_track_internal(self, audio_data: np.ndarray, start_sample: int, stem_index: int):
        """Internal method to add tracks without re-normalizing audio (already normalized)."""
        self.tracks.append(Track(audio_data, start_sample, stem_index))

    def set_next_loop(self, tracks_audio: list, loop_end_sample: int):
        """
        Pre-register the next loop's tracks for seamless transition.
        tracks_audio: list of (audio_data, stem_index) tuples
        loop_end_sample: sample position when current loop ends
        """
        with self.lock:
            self.next_loop_audio = tracks_audio
            self.current_loop_end_sample = loop_end_sample

    def _extend_tracks_for_loop(self, loop_end_sample: int):
        """
        Extend current tracks to fill another loop iteration.
        This is called when the next loop isn't ready yet.
        """
        current_tracks = [t for t in self.tracks
                          if (t.start_sample + t.length) > self.current_sample
                          and t.start_sample < loop_end_sample]

        for track in current_tracks:
            track_end = track.start_sample + track.length
            # If track ends after loop boundary, we need to clip and repeat the overlap
            if track_end > loop_end_sample:
                samples_remaining = track_end - loop_end_sample
                src_audio = track.audio_data[:samples_remaining]
                repeated_audio = np.tile(src_audio, (2, 1))
                self._add_track_internal(repeated_audio, loop_end_sample, track.stem_index)
            elif track_end == loop_end_sample:
                # Track ends exactly at loop boundary - repeat entire track
                repeated_audio = np.tile(track.audio_data, (2, 1))
                self._add_track_internal(repeated_audio, loop_end_sample, track.stem_index)
            else:
                # Track ends BEFORE loop boundary - tile the entire track to fill
                # This handles the case where a track is shorter than the loop duration
                repeats_needed = (loop_end_sample - track_end) // track.length + 2
                repeated_audio = np.tile(track.audio_data, (repeats_needed, 1))
                self._add_track_internal(repeated_audio, track_end, track.stem_index)

    def _extend_tracks_at_position(self, start_sample: int, gap_samples: int):
        """
        Add extended tracks starting at start_sample to fill a gap when we're behind.
        This adds fresh looped tracks at the current position to ensure no silence.
        """
        # Find tracks that would be playing at start_sample, or that we should loop from
        # We need to find tracks that were active at or near the transition point
        tracks_to_extend = [t for t in self.tracks
                           if t.start_sample <= start_sample
                           and (t.start_sample + t.length) > self.current_loop_end_sample]

        # If no tracks found that extend past loop end, find the most recent track
        if not tracks_to_extend:
            # Find the track that ended most recently before or at start_sample
            candidates = [t for t in self.tracks if t.start_sample + t.length <= start_sample]
            if candidates:
                # Sort by end sample (most recent first)
                candidates.sort(key=lambda t: t.start_sample + t.length, reverse=True)
                tracks_to_extend = [candidates[0]]

        for track in tracks_to_extend:
            # Calculate how much of the track's audio has already played
            samples_already_played = start_sample - track.start_sample

            # Handle edge case: if we're at or past the track end
            if samples_already_played >= track.length:
                # Just loop the entire track from the beginning
                repeated_audio = np.tile(track.audio_data, (2, 1))
                self._add_track_internal(repeated_audio, start_sample, track.stem_index)
            else:
                # The remainder of the track (from current position to end)
                samples_left_in_track = track.length - samples_already_played

                # Create a looped continuation: take remaining samples and tile
                remaining_audio = track.audio_data[samples_already_played:]
                # Tile enough to cover the gap plus some buffer
                repeats_needed = (gap_samples // samples_left_in_track) + 2
                extended_audio = np.tile(remaining_audio, (repeats_needed, 1))
                self._add_track_internal(extended_audio, start_sample, track.stem_index)

    def _callback(self, outdata, frames, time, status):
        if status:
            print(f"Mixer status: {status}")

        # Explicitly clear the output buffer to absolute zero
        outdata.fill(0)

        # Only mix if the engine is officially 'Generating/Playing'
        if state.is_generating:
            with self.lock:
                # IMPORTANT: Check loop transition BEFORE pruning, so extended/switched tracks are available for mixing
                if self.current_loop_end_sample > 0:
                    samples_until_loop_end = self.current_loop_end_sample - self.current_sample
                    samples_needed = frames + self.loop_switch_deadline_ms * (self.sample_rate // 1000)

                    # Handle transition if we're at OR PAST the deadline (running late)
                    if samples_until_loop_end <= samples_needed:
                        # We're within the switch window OR past it - handle transition
                        if self.next_loop_audio:
                            # Next loop is ready - switch to it by adding tracks at current position
                            print(f"DEBUG: Switching to next loop at sample {self.current_sample}")
                            for audio_data, stem_index in self.next_loop_audio:
                                self._add_track_internal(audio_data.copy(), self.current_sample, stem_index)
                            self.next_loop_audio = []
                            self.current_loop_end_sample = 0  # Mark as handled
                        else:
                            # Next loop not ready - extend current tracks by looping
                            # Note: samples_until_loop_end may be negative if we're running late
                            print(f"DEBUG: Next loop not ready, extending current loop (behind by {samples_until_loop_end} samples)")
                            self._extend_tracks_for_loop(self.current_loop_end_sample)
                            # Extend the deadline by a full loop duration to keep bridging
                            # We need to extend enough to give generation time to catch up
                            # Use a large enough extension to cover multiple callback periods
                            self.current_loop_end_sample += int(2 * self.sample_rate)  # Add 2 seconds of runway

                        # If we're significantly past the deadline, also add an immediate "catch-up" extension
                        # at current_sample to avoid a gap. This handles the case where we fell behind
                        # by so much that even the extended tracks start in the past.
                        if samples_until_loop_end < 0:
                            gap_samples = -samples_until_loop_end
                            self._extend_tracks_at_position(self.current_sample, gap_samples)

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
        self._running = True
        self._stream_thread = threading.Thread(target=self._stream_loop, daemon=True)
        self._stream_thread.start()
        print("Audio stream loop started")

    def _stream_loop(self):
        sleep_time = self.blocksize / self.sample_rate
        outdata = np.zeros((self.blocksize, self.channels), dtype=np.float32)
        while self._running:
            time.sleep(sleep_time)
            self._callback(outdata, self.blocksize, None, None)

    def clear(self):
        with self.lock:
            self.tracks = []
            self.current_sample = 0
            self.next_loop_audio = []
            self.current_loop_end_sample = 0

    def stop(self):
        self._running = False
        if hasattr(self, '_stream_thread'):
            self._stream_thread.join(timeout=1.0)

        with self.lock:
            self.tracks = []
