"""framework_mixer.py — Real-time audio mixing thread.

Pulls audio from registered Track objects, applies per-stem gain/mute/solo,
broadcasts PCM bytes to all streaming clients and recording sinks.

Uses sync_lock (threading.Lock) rather than state.lock (asyncio.Lock)
because the _callback runs in a normal daemon thread, never in async context.
"""

import logging
import threading
import time

import numpy as np

from .framework_state import state

log = logging.getLogger(__name__)


class Track:
    def __init__(self, audio_data: np.ndarray, start_sample: int, stem_index: int = -1):
        self.audio_data = audio_data
        self.start_sample = start_sample
        self.length = len(audio_data)
        self.stem_index = stem_index  # index in state.active_stems; -1 = historical


class Mixer:
    def __init__(self, sample_rate=44100, blocksize=2048, channels=2):
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.channels = channels
        self.tracks = []
        self.current_sample = 0
        self.lock = threading.Lock()  # guards self.tracks et al.
        self.stream = None
        self.current_loop_end_sample = 0
        self.next_loop_audio = []  # List of (audio_data, stem_index) ready to play
        self._next_loop_duration = 0  # Duration of the queued next loop in samples
        self._current_loop_duration = 0  # Duration of the CURRENT loop in samples (for bar-aligned extension)
        self.loop_switch_deadline_ms = 1000  # 1s lookahead — headroom for multi-stream
        self._running = False
        self._stream_thread = None
        self._debug_count = 0
        # Loop transition tracking
        self._just_transitioned = False
        self._last_transition_loop_index = 0
        self._next_loop_idx = 0  # Loop index being queued for next transition

    # ------------------------------------------------------------------
    # Public track management
    # ------------------------------------------------------------------

    def add_track(self, audio_data: np.ndarray, start_sample: int, stem_index: int = -1):
        """Add a track. For the first loop start_sample should be 0."""
        with self.lock:
            audio_data = self._ensure_stereo(audio_data)
            self.tracks.append(Track(audio_data, start_sample, stem_index))

    def _add_track_internal(self, audio_data: np.ndarray, start_sample: int, stem_index: int):
        """Add without re-normalizing; caller must hold self.lock."""
        self.tracks.append(Track(audio_data, start_sample, stem_index))

    def set_next_loop(self, tracks_audio: list, next_loop_duration_samples: int = 0, loop_idx: int = 0):
        """Pre-register the next loop's tracks for seamless transition.

        tracks_audio: list of (audio_data, stem_index) tuples.
        next_loop_duration_samples: how long the NEXT loop lasts (used after
            transition fires to set the new current_loop_end_sample). Does NOT
            touch current_loop_end_sample — the current loop's boundary is
            preserved so tracks are not orphaned before the transition.
        loop_idx: the loop index being queued (used to notify state when transition fires).
        """
        with self.lock:
            self.next_loop_audio = tracks_audio
            self._next_loop_duration = next_loop_duration_samples
            self._next_loop_idx = loop_idx

    def clear(self):
        with self.lock:
            self.tracks = []
            self.current_sample = 0
            self.next_loop_audio = []
            self.current_loop_end_sample = 0
            self._next_loop_duration = 0
            self._current_loop_duration = 0
            self._just_transitioned = False
            self._last_transition_loop_index = 0
            self._next_loop_idx = 0

    def pop_transition_event(self):
        """Atomically check and clear transition flag. Returns loop index if transitioned."""
        with self.lock:
            if self._just_transitioned:
                self._just_transitioned = False
                return self._last_transition_loop_index
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_stereo(audio_data: np.ndarray) -> np.ndarray:
        if audio_data.ndim == 1:
            audio_data = audio_data.reshape(-1, 1)
        if audio_data.shape[1] == 1:
            audio_data = np.repeat(audio_data, 2, axis=1)
        return audio_data

    def _extend_tracks_for_loop(self, loop_end_sample: int):
        """Extend current tracks to fill another loop iteration."""
        current_tracks = [
            t
            for t in self.tracks
            if (t.start_sample + t.length) > self.current_sample and t.start_sample < loop_end_sample
        ]
        for track in current_tracks:
            track_end = track.start_sample + track.length
            if track_end > loop_end_sample:
                samples_remaining = track_end - loop_end_sample
                src_audio = track.audio_data[:samples_remaining]
                repeated_audio = np.tile(src_audio, (2, 1))
                self._add_track_internal(repeated_audio, loop_end_sample, track.stem_index)
            elif track_end == loop_end_sample:
                repeated_audio = np.tile(track.audio_data, (2, 1))
                self._add_track_internal(repeated_audio, loop_end_sample, track.stem_index)
            else:
                repeats_needed = (loop_end_sample - track_end) // track.length + 2
                repeated_audio = np.tile(track.audio_data, (repeats_needed, 1))
                self._add_track_internal(repeated_audio, track_end, track.stem_index)

    def _extend_tracks_at_position(self, start_sample: int, gap_samples: int):
        """Add extended tracks starting at start_sample to fill a gap."""
        tracks_to_extend = [
            t
            for t in self.tracks
            if t.start_sample <= start_sample and (t.start_sample + t.length) > self.current_loop_end_sample
        ]
        if not tracks_to_extend:
            candidates = [t for t in self.tracks if t.start_sample + t.length <= start_sample]
            if candidates:
                candidates.sort(key=lambda t: t.start_sample + t.length, reverse=True)
                tracks_to_extend = [candidates[0]]

        for track in tracks_to_extend:
            samples_already_played = start_sample - track.start_sample
            if samples_already_played >= track.length:
                repeated_audio = np.tile(track.audio_data, (2, 1))
                self._add_track_internal(repeated_audio, start_sample, track.stem_index)
            else:
                samples_left_in_track = track.length - samples_already_played
                remaining_audio = track.audio_data[samples_already_played:]
                repeats_needed = (gap_samples // samples_left_in_track) + 2
                extended_audio = np.tile(remaining_audio, (repeats_needed, 1))
                self._add_track_internal(extended_audio, start_sample, track.stem_index)

    # ------------------------------------------------------------------
    # Audio callback
    # ------------------------------------------------------------------

    def _callback(self, outdata: np.ndarray, frames: int, _time, _status):
        outdata.fill(0)

        # Snapshot per-stem mixer state once per tick. These fields are mutated by
        # route handlers; reading them unlocked risks a torn read and a
        # 'Set/dict changed size during iteration' RuntimeError. snapshot_mixer_state
        # copies them under state.sync_lock (the copies are also C-level atomic).
        is_generating, soloed, muted, volumes = state.snapshot_mixer_state()

        if not is_generating:
            pcm_out = np.clip(outdata, -1.0, 1.0)
            state.broadcast_audio((pcm_out * 32767).astype("<i2").tobytes())
            self.current_sample += frames
            return

        with self.lock:
            # Loop transition logic
            if self.current_loop_end_sample > 0:
                samples_until_loop_end = self.current_loop_end_sample - self.current_sample
                samples_needed = frames + self.loop_switch_deadline_ms * (self.sample_rate // 1000)

                if samples_until_loop_end <= samples_needed:
                    if self.next_loop_audio:
                        # Start new tracks at the ALIGNED boundary so there are no
                        # gaps/overlaps regardless of when this callback fires.
                        transition_sample = self.current_loop_end_sample
                        transitioning_loop_idx = self._next_loop_idx
                        log.debug(
                            "Switching to next loop at boundary sample %d (current=%d)",
                            transition_sample,
                            self.current_sample,
                        )
                        for audio_data, stem_index in self.next_loop_audio:
                            self._add_track_internal(audio_data.copy(), transition_sample, stem_index)
                        self.next_loop_audio = []
                        # Set the NEW loop's end boundary instead of resetting to 0
                        if self._next_loop_duration > 0:
                            self.current_loop_end_sample = transition_sample + self._next_loop_duration
                            self._current_loop_duration = self._next_loop_duration  # track for extension fallback
                        else:
                            # Fallback: derive from the longest newly-added future track
                            future = [t for t in self.tracks if t.start_sample >= transition_sample]
                            if future:
                                self.current_loop_end_sample = max(t.start_sample + t.length for t in future)
                                self._current_loop_duration = self.current_loop_end_sample - transition_sample
                            else:
                                self.current_loop_end_sample = 0
                                self._current_loop_duration = 0
                        self._next_loop_duration = 0
                        # Signal that a transition occurred so the async loop can record it in state
                        self._just_transitioned = True
                        self._last_transition_loop_index = transitioning_loop_idx
                    else:
                        # Next audio not ready yet — extend current tracks by exactly one
                        # loop duration so the boundary stays bar-aligned at every BPM.
                        # Falling back to 2 * sample_rate would land mid-bar for most BPMs.
                        extend_by = (
                            self._current_loop_duration
                            if self._current_loop_duration > 0
                            else int(2 * self.sample_rate)
                        )
                        log.debug(
                            "Next loop not ready, extending by %d samples (behind by %d)",
                            extend_by,
                            samples_until_loop_end,
                        )
                        self._extend_tracks_for_loop(self.current_loop_end_sample)
                        self.current_loop_end_sample += extend_by

                    if samples_until_loop_end < 0:
                        gap_samples = -samples_until_loop_end
                        self._extend_tracks_at_position(self.current_sample, gap_samples)

            # Auto-tile safety net: extend any stem whose audio ends before
            # current_loop_end_sample so there is no mid-loop silence.
            #
            # Key invariants that prevent the buzzing bug:
            #  - Only act on *active* tracks (track_end > current_sample). Expired
            #    tracks sit in past_tracks[:2] and would otherwise be re-tiled every
            #    callback tick, producing stacked offset copies that buzz/flange.
            #  - Start the tile at track_end (NOT current_sample) for a gapless seam.
            #  - covered_stems: once we tile a stem, mark it so we don't tile it again
            #    this tick. After appending, the new track's end >= current_loop_end_sample,
            #    so next callback it lands in covered_stems immediately and stops.
            if self.current_loop_end_sample > self.current_sample:
                covered_stems = {
                    t.stem_index for t in self.tracks if (t.start_sample + t.length) >= self.current_loop_end_sample
                }
                for track in list(self.tracks):
                    track_end = track.start_sample + track.length
                    if (
                        track.stem_index in covered_stems
                        or track_end <= self.current_sample
                        or track_end >= self.current_loop_end_sample
                    ):
                        continue
                    gap = self.current_loop_end_sample - track_end
                    repeats = (gap // max(1, track.length)) + 2
                    tiled = np.tile(track.audio_data, (repeats, 1))
                    self._add_track_internal(tiled, track_end, track.stem_index)
                    covered_stems.add(track.stem_index)

            # Pruning: keep 2 recent past tracks + all future tracks
            past_tracks = [t for t in self.tracks if (t.start_sample + t.length) <= self.current_sample]
            past_tracks.sort(key=lambda t: t.start_sample + t.length, reverse=True)
            future_tracks = [t for t in self.tracks if (t.start_sample + t.length) > self.current_sample]
            self.tracks = past_tracks[:2] + future_tracks

            # Build mix list for current window
            tracks_to_mix = [
                t
                for t in self.tracks
                if t.start_sample < (self.current_sample + frames) and (t.start_sample + t.length) > self.current_sample
            ]

            # Mute/solo/volume filtering using this tick's snapshot (see top of
            # _callback). Reading state.soloed_stems etc. directly here would race
            # with concurrent route-handler mutation (review A2).
            solo_active = len(soloed) > 0
            final_mix_list = []
            for track in tracks_to_mix:
                stem_idx = track.stem_index
                if solo_active:
                    if stem_idx in soloed:
                        final_mix_list.append(track)
                elif stem_idx not in muted:
                    final_mix_list.append(track)

            # Dynamic gain to prevent clipping
            num_active = len(final_mix_list)
            stem_gain_global = 1.0 / max(1, np.sqrt(num_active))

            for track in final_mix_list:
                block_start = self.current_sample
                block_end = self.current_sample + frames
                track_start = track.start_sample
                track_end = track.start_sample + track.length

                indiv_gain = volumes.get(track.stem_index, 1.0)
                total_gain = stem_gain_global * indiv_gain

                if track_start < block_end and track_end > block_start:
                    out_start = max(0, track_start - block_start)
                    out_end = min(frames, track_end - block_start)
                    in_start = max(0, block_start - track_start)
                    in_end = min(track.length, block_end - track_start)

                    mix_channels = min(outdata.shape[1], track.audio_data.shape[1])
                    outdata[out_start:out_end, :mix_channels] += (
                        track.audio_data[in_start:in_end, :mix_channels] * total_gain
                    )

        self.current_sample += frames

        # Periodic debug log
        self._debug_count += 1
        if self._debug_count % 200 == 0:
            log.debug(
                "Mixer: is_generating=%s tracks=%d out=[%.4f, %.4f]",
                is_generating,
                len(self.tracks),
                outdata.min(),
                outdata.max(),
            )

        # Clip, convert, broadcast
        pcm_out = np.clip(outdata, -1.0, 1.0)
        state.broadcast_audio((pcm_out * 32767).astype("<i2").tobytes())

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start(self):
        self._running = True
        self._stream_thread = threading.Thread(target=self._stream_loop, daemon=True, name="Mixer")
        self._stream_thread.start()
        log.info("Audio stream loop started")

    def _stream_loop(self):
        """Timing-accurate playback loop using a monotonic deadline."""
        sleep_time = self.blocksize / self.sample_rate  # ~46 ms
        outdata = np.zeros((self.blocksize, self.channels), dtype=np.float32)

        # Use monotonic clock + deadline to compensate for sleep jitter.
        deadline = time.monotonic()
        while self._running:
            deadline += sleep_time
            self._callback(outdata, self.blocksize, None, None)
            # Sleep only the *remaining* time until the next deadline.
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            else:
                # We're running late; skip a sleep cycle to catch up.
                deadline = time.monotonic()

    def stop(self):
        self._running = False
        if self._stream_thread is not None:
            self._stream_thread.join(timeout=2.0)
        with self.lock:
            self.tracks = []
        log.info("Mixer stopped")
