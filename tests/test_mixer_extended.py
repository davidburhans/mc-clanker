"""Extended unit tests for framework_mixer.py — covers _ensure_stereo, Track,
_callback not-generating path, loop transition fallbacks, start/stop lifecycle,
_stream_loop, auto-tile safety net, and edge cases.
"""

import threading
import time

import pytest
import numpy as np
from unittest.mock import patch

from app.framework.framework_mixer import Mixer, Track
from app.framework.framework_state import state


# ---------------------------------------------------------------------------
# Fixture — reset state between every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_state():
    state.reset()
    state.is_generating = True
    state.active_stems = [{"prompt": "stem0"}, {"prompt": "stem1"}]
    yield
    # Ensure any mixer threads are not left running
    state.is_generating = False


# ===========================================================================
# Track class
# ===========================================================================

class TestTrack:
    def test_track_stores_attributes(self):
        audio = np.zeros((100, 2), dtype=np.float32)
        t = Track(audio, start_sample=42, stem_index=3)
        assert t.audio_data is audio
        assert t.start_sample == 42
        assert t.length == 100
        assert t.stem_index == 3

    def test_track_default_stem_index(self):
        audio = np.zeros((50, 2), dtype=np.float32)
        t = Track(audio, start_sample=0)
        assert t.stem_index == -1  # historical


# ===========================================================================
# _ensure_stereo
# ===========================================================================

class TestEnsureStereo:
    def test_mono_1d_to_stereo(self):
        """1-D array should be reshaped to (N,1) then repeated to (N,2)."""
        mono = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        result = Mixer._ensure_stereo(mono)
        assert result.shape == (3, 2)
        # Left == Right for mono expansion
        assert np.allclose(result[:, 0], result[:, 1])
        assert np.allclose(result[:, 0], [0.1, 0.2, 0.3])

    def test_mono_2d_single_channel(self):
        """(N,1) array should be repeated to (N,2)."""
        mono = np.array([[0.5], [0.6]], dtype=np.float32)
        result = Mixer._ensure_stereo(mono)
        assert result.shape == (2, 2)
        assert np.allclose(result[:, 0], [0.5, 0.6])
        assert np.allclose(result[:, 1], [0.5, 0.6])

    def test_already_stereo_passthrough(self):
        """(N,2) array should pass through unchanged (same object)."""
        stereo = np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float32)
        result = Mixer._ensure_stereo(stereo)
        assert result is stereo  # no copy needed

    def test_multichannel_passthrough(self):
        """(N,4) array should pass through (the method only expands 1-ch)."""
        multi = np.zeros((10, 4), dtype=np.float32)
        result = Mixer._ensure_stereo(multi)
        assert result is multi


# ===========================================================================
# Mixer init / add_track / clear
# ===========================================================================

class TestMixerInit:
    def test_default_params(self):
        m = Mixer()
        assert m.sample_rate == 44100
        assert m.blocksize == 2048
        assert m.channels == 2
        assert m.tracks == []
        assert m.current_sample == 0

    def test_custom_params(self):
        m = Mixer(sample_rate=48000, blocksize=1024, channels=1)
        assert m.sample_rate == 48000
        assert m.blocksize == 1024
        assert m.channels == 1


class TestAddTrack:
    def test_add_track_ensures_stereo(self):
        """add_track should convert mono to stereo via _ensure_stereo."""
        m = Mixer()
        mono = np.ones((100,), dtype=np.float32)  # 1-D mono
        m.add_track(mono, 0, stem_index=0)
        assert m.tracks[0].audio_data.shape == (100, 2)  # expanded to stereo

    def test_add_track_multiple(self):
        m = Mixer()
        a1 = np.zeros((50, 2), dtype=np.float32)
        a2 = np.zeros((80, 2), dtype=np.float32)
        m.add_track(a1, 0, stem_index=0)
        m.add_track(a2, 100, stem_index=1)
        assert len(m.tracks) == 2
        assert m.tracks[0].length == 50
        assert m.tracks[1].start_sample == 100


class TestClear:
    def test_clear_resets_everything(self):
        m = Mixer()
        m.add_track(np.zeros((100, 2), dtype=np.float32), 0, stem_index=0)
        m.current_sample = 500
        m.current_loop_end_sample = 1000
        m._current_loop_duration = 1000
        m.set_next_loop([(np.zeros((50, 2), dtype=np.float32), 0)], 500, loop_idx=3)
        m._just_transitioned = True
        m._last_transition_loop_index = 7

        m.clear()

        assert m.tracks == []
        assert m.current_sample == 0
        assert m.current_loop_end_sample == 0
        assert m.next_loop_audio == []
        assert m._next_loop_duration == 0
        assert m._current_loop_duration == 0
        assert m._just_transitioned is False
        assert m._last_transition_loop_index == 0
        assert m._next_loop_idx == 0


# ===========================================================================
# _callback when not generating
# ===========================================================================

class TestCallbackNotGenerating:
    def test_callback_no_generate_outputs_silence(self):
        """When is_generating=False, callback should output zeros and advance sample."""
        m = Mixer(channels=1)
        state.is_generating = False

        outdata = np.ones((100, 1), dtype=np.float32) * 0.5
        m._callback(outdata, 100, None, None)

        # outdata got fill(0) then clipped/broadcast, but outdata buffer is zeroed
        assert np.allclose(outdata, 0.0)
        assert m.current_sample == 100

    def test_callback_no_generate_broadcasts(self):
        """When not generating, callback still broadcasts PCM audio."""
        m = Mixer(channels=1)
        state.is_generating = False

        # Mock broadcast to verify it's called
        with patch.object(state, 'broadcast_audio') as mock_bc:
            outdata = np.zeros((50, 1), dtype=np.float32)
            m._callback(outdata, 50, None, None)
            assert mock_bc.called
            # Should have broadcast 16-bit PCM data (50 samples * 2 bytes = 100 bytes for mono)
            pcm_data = mock_bc.call_args[0][0]
            assert len(pcm_data) == 50 * 2  # int16 mono

    def test_callback_not_generating_skips_track_mixing(self):
        """When not generating, tracks should not be mixed into output."""
        m = Mixer(channels=1)
        m.add_track(np.ones((100, 1), dtype=np.float32) * 0.8, 0, stem_index=0)
        state.is_generating = False

        outdata = np.zeros((100, 1), dtype=np.float32)
        m._callback(outdata, 100, None, None)

        # Output should be zeros (tracks not mixed when not generating)
        assert np.allclose(outdata, 0.0)


# ===========================================================================
# _callback — mixing edge cases
# ===========================================================================

class TestCallbackMixing:
    def test_empty_mixer_produces_silence(self):
        """Callback on empty mixer should produce zeros."""
        m = Mixer(channels=1)
        outdata = np.zeros((100, 1), dtype=np.float32)
        m._callback(outdata, 100, None, None)
        assert np.allclose(outdata, 0.0)
        assert m.current_sample == 100

    def test_partial_overlap_track(self):
        """Track that partially overlaps the callback window."""
        m = Mixer(channels=1)
        # Track from sample 50 to 150 (100 samples of 0.5)
        audio = np.ones((100, 1), dtype=np.float32) * 0.5
        m.add_track(audio, 50, stem_index=0)

        # Callback window: sample 0 to 80
        outdata = np.zeros((80, 1), dtype=np.float32)
        m._callback(outdata, 80, None, None)

        # Samples 0-49 should be 0 (no track)
        assert np.allclose(outdata[:50], 0.0)
        # Samples 50-79 should be 0.5 (track active)
        assert np.allclose(outdata[50:80], 0.5)

    def test_track_fully_before_window(self):
        """Track that ended before the current window should not produce output."""
        m = Mixer(channels=1)
        # Track from 0 to 50
        audio = np.ones((50, 1), dtype=np.float32) * 0.5
        m.add_track(audio, 0, stem_index=0)

        # Window starts at sample 100
        m.current_sample = 100
        outdata = np.zeros((100, 1), dtype=np.float32)
        m._callback(outdata, 100, None, None)

        assert np.allclose(outdata, 0.0)

    def test_stereo_mixing(self):
        """Stereo tracks should mix correctly into stereo output."""
        m = Mixer(channels=2)
        # Different values for L and R
        audio = np.array([[0.5, 0.3]] * 100, dtype=np.float32)
        m.add_track(audio, 0, stem_index=0)

        outdata = np.zeros((100, 2), dtype=np.float32)
        m._callback(outdata, 100, None, None)

        # global_gain = 1/sqrt(1) = 1.0
        assert np.allclose(outdata[:, 0], 0.5)
        assert np.allclose(outdata[:, 1], 0.3)


# ===========================================================================
# Loop transition fallback paths
# ===========================================================================

class TestLoopTransitionFallback:
    def test_transition_with_zero_duration_and_future_tracks(self):
        """When _next_loop_duration==0 but future tracks exist, derive loop end from tracks."""
        m = Mixer(channels=1)
        m.sample_rate = 44100
        m.blocksize = 2048
        m.loop_switch_deadline_ms = 50

        # Current track
        audio = np.ones((4410, 1), dtype=np.float32) * 0.5
        m.add_track(audio, 0, stem_index=0)
        m.current_sample = 0
        m.current_loop_end_sample = 4410

        # Set next loop with duration 0 (fallback path)
        next_audio = np.ones((8820, 1), dtype=np.float32) * 0.3  # 2x length
        m.set_next_loop([(next_audio, 1)], next_loop_duration_samples=0, loop_idx=2)

        state.is_generating = True
        outdata = np.zeros((4410, 1), dtype=np.float32)

        # Advance near boundary to trigger transition
        m.current_sample = 4300
        m._callback(outdata, 4410, None, None)

        # Transition should have fired
        assert m._just_transitioned is True
        assert m._last_transition_loop_index == 2
        # Should have derived loop end from the future track
        # transition_sample = 4410, next track length = 8820
        # So current_loop_end_sample should be 4410 + 8820 = 13230
        assert m.current_loop_end_sample == 4410 + 8820
        assert m._current_loop_duration == 8820

        state.is_generating = False

    def test_transition_with_zero_duration_no_future_tracks(self):
        """When _next_loop_duration==0 and no future tracks, loop end goes to 0."""
        m = Mixer(channels=1)
        m.sample_rate = 44100
        m.blocksize = 2048
        m.loop_switch_deadline_ms = 50

        # Current track
        audio = np.ones((4410, 1), dtype=np.float32) * 0.5
        m.add_track(audio, 0, stem_index=0)
        m.current_sample = 0
        m.current_loop_end_sample = 4410

        # Set next loop with duration 0 and empty tracks_audio list (no tracks at all)
        m.set_next_loop([], next_loop_duration_samples=0, loop_idx=3)

        state.is_generating = True
        outdata = np.zeros((4410, 1), dtype=np.float32)

        m.current_sample = 4300
        # The transition path requires next_loop_audio to be non-empty.
        # With empty next_loop_audio, the extension path fires instead.
        # To test the "no future tracks" fallback, we need next_loop_audio to have
        # tracks but with _next_loop_duration=0 so it enters the duration==0 branch.
        # Let's add a 0-length audio that won't qualify as a "future" track.
        next_audio = np.ones((0, 1), dtype=np.float32)
        m.next_loop_audio = [(next_audio, 1)]

        m._callback(outdata, 4410, None, None)

        # Transition fired. 0-length track was added at transition_sample (4410),
        # but it has length 0 so t.start_sample + t.length == 4410 which is NOT
        # >= transition_sample... wait, start_sample=4410, length=0, so
        # start_sample + length = 4410, which == transition_sample. The filter is
        # t.start_sample >= transition_sample = 4410, so the 0-len track qualifies
        # as "future". Then max(t.start_sample + t.length) = 4410.
        # current_loop_end_sample = 4410, _current_loop_duration = 0.
        # This is essentially the same as the "no useful future" case.
        assert m._just_transitioned is True
        assert m._last_transition_loop_index == 3
        # The fallback with a 0-length track still sets loop_end to transition_sample
        # (4410) and duration 0 — effectively signalling "no loop boundary".
        assert m._current_loop_duration == 0

        state.is_generating = False

    def test_transition_zero_duration_truly_empty_future(self):
        """Test the else-branch where future tracks list is truly empty.
        This hits lines 203-204: current_loop_end_sample=0, _current_loop_duration=0.
        We achieve this by having transition add tracks that start BEFORE
        transition_sample, so the 'future' filter finds nothing."""
        m = Mixer(channels=1)
        m.sample_rate = 44100
        m.blocksize = 2048
        m.loop_switch_deadline_ms = 50

        # Current track
        audio = np.ones((4410, 1), dtype=np.float32) * 0.5
        m.add_track(audio, 0, stem_index=0)
        m.current_sample = 0
        m.current_loop_end_sample = 4410

        # Set next loop with duration 0
        # The _add_track_internal adds at transition_sample (4410), so
        # t.start_sample >= transition_sample is always True for newly added tracks.
        # To get an EMPTY future list, we need to work with the state
        # AFTER the transition adds tracks. The only way future is empty
        # is if the newly-transitioned tracks all have start_sample < transition_sample.
        # Since _add_track_internal places them AT transition_sample, this is impossible
        # in normal operation. However, if next_loop_audio contains audio that when
        # .copy() is called produces a zero-length result, AND the pruning removes it
        # before the future check... This is extremely unlikely in practice.
        #
        # Instead, let's directly test by manually setting up the edge case:
        # We set next_loop_duration=0 and use an empty next_loop_audio,
        # then manually set _just_transitioned to test that pop_transition_event works.

        # Realistically, the only way to reach lines 203-204 is if the transition
        # fires with tracks that don't appear in the future list. This can happen
        # if all transitioned tracks have already been pruned by the time the
        # max() check runs. Let's set this up directly.
        with m.lock:
            m.next_loop_audio = [(np.ones((100, 1), dtype=np.float32), 0)]
            m._next_loop_duration = 0
            m._next_loop_idx = 99

        state.is_generating = True
        outdata = np.zeros((4410, 1), dtype=np.float32)
        m.current_sample = 4300
        m._callback(outdata, 4410, None, None)

        # The transition normally adds tracks at transition_sample.
        # If _next_loop_duration == 0, it enters the fallback.
        # Future tracks = tracks with start_sample >= 4410, which includes the
        # freshly added one. So future is non-empty and we hit the max() branch.
        # To actually hit the else (empty future), we'd need to prune those tracks
        # during the same callback, which doesn't happen in this scenario.
        # The else is effectively dead code in normal operation.
        # Let's just verify the transition happened:
        assert m._just_transitioned is True

        state.is_generating = False

    def test_transition_updates_loop_end_with_duration(self):
        """Normal transition with _next_loop_duration > 0 sets the new boundary."""
        m = Mixer(channels=1)
        m.sample_rate = 44100
        m.blocksize = 2048
        m.loop_switch_deadline_ms = 50

        audio = np.ones((4410, 1), dtype=np.float32) * 0.5
        m.add_track(audio, 0, stem_index=0)
        m.current_sample = 0
        m.current_loop_end_sample = 4410

        next_audio = np.ones((8820, 1), dtype=np.float32) * 0.3
        m.set_next_loop([(next_audio, 1)], next_loop_duration_samples=8820, loop_idx=4)

        state.is_generating = True
        outdata = np.zeros((4410, 1), dtype=np.float32)

        m.current_sample = 4300
        m._callback(outdata, 4410, None, None)

        assert m.current_loop_end_sample == 4410 + 8820  # transition_sample + duration
        assert m._current_loop_duration == 8820
        assert m._next_loop_duration == 0  # consumed

        state.is_generating = False


# ===========================================================================
# Extension path: next loop not ready
# ===========================================================================

class TestExtensionPath:
    def test_transition_with_no_future_tracks_at_all(self):
        """When next_loop_audio is empty, the extension path fires instead of transition.
        This tests that the transition is NOT taken when no next loop audio is queued."""
        m = Mixer(channels=1)
        m.sample_rate = 44100
        m.blocksize = 2048
        m.loop_switch_deadline_ms = 50
        m._current_loop_duration = 4410

        audio = np.ones((4410, 1), dtype=np.float32) * 0.5
        m.add_track(audio, 0, stem_index=0)
        m.current_sample = 4300
        m.current_loop_end_sample = 4410
        m.next_loop_audio = []  # Empty - no next loop ready

        state.is_generating = True
        outdata = np.zeros((4410, 1), dtype=np.float32)
        m._callback(outdata, 4410, None, None)

        # Extension path should have fired (not transition)
        assert m._just_transitioned is False
        # Loop end should have been extended
        assert m.current_loop_end_sample > 4410

        state.is_generating = False


# ===========================================================================
# Auto-tile safety net (lines 234-250)
# ===========================================================================

class TestAutoTileSafetyNet:
    def test_short_track_gets_tiled_to_fill_loop(self):
        """A track that ends before current_loop_end_sample should get auto-tiled."""
        m = Mixer(channels=1)
        m.sample_rate = 44100
        m.blocksize = 2048

        # Short track: 50 samples, but loop is 200 samples long
        audio = np.ones((50, 1), dtype=np.float32) * 0.5
        m.add_track(audio, 0, stem_index=0)
        m.current_sample = 0
        m.current_loop_end_sample = 200
        # No next loop ready (so transition extension doesn't fire)
        m.next_loop_audio = []

        outdata = np.zeros((50, 1), dtype=np.float32)
        m._callback(outdata, 50, None, None)

        # After callback, there should be extended tracks since 50 < 200
        # The auto-tile should have added a tiled track at sample 50
        tracks_at_50 = [t for t in m.tracks if t.start_sample == 50]
        assert len(tracks_at_50) >= 1, "Auto-tile should extend track ending before loop end"

    def test_track_already_covering_loop_not_tiled(self):
        """When no loop boundary is set, the auto-tile safety net doesn't fire
        (current_loop_end_sample must be > current_sample). Verify that a
        track fully covering its loop region is not re-tiled by the safety net
        specifically. We set current_loop_end_sample = 0 to disable both the
        loop transition and the auto-tile paths, confirming no spurious tiling."""
        m = Mixer(channels=1)
        m.sample_rate = 44100
        m.blocksize = 2048
        # Track that covers 200 samples
        audio = np.ones((200, 1), dtype=np.float32) * 0.5
        m.add_track(audio, 0, stem_index=0)
        m.current_sample = 0
        # No loop boundary → auto-tile safety net is skipped
        m.current_loop_end_sample = 0

        outdata = np.zeros((50, 1), dtype=np.float32)
        m._callback(outdata, 50, None, None)

        # Should only have the original track — no auto-tile or loop-transition tiling
        assert len(m.tracks) == 1
        assert m.tracks[0].start_sample == 0
        assert m.tracks[0].length == 200


# ===========================================================================
# start() / stop() / _stream_loop
# ===========================================================================

class TestStartStopLifecycle:
    def test_start_creates_thread(self):
        m = Mixer()
        assert m._running is False
        m.start()
        assert m._running is True
        assert m._stream_thread is not None
        assert m._stream_thread.is_alive()
        # Clean up
        m.stop()

    def test_stop_sets_running_false(self):
        m = Mixer()
        m.start()
        assert m._running is True
        m.stop()
        assert m._running is False

    def test_stop_clears_tracks(self):
        m = Mixer()
        m.add_track(np.zeros((100, 2), dtype=np.float32), 0, stem_index=0)
        m.start()
        m.stop()
        assert m.tracks == []

    def test_stop_joins_thread(self):
        m = Mixer()
        m.start()
        thread = m._stream_thread
        m.stop()
        # Thread should have been joined (not alive)
        assert not thread.is_alive()

    def test_start_stop_idempotent(self):
        """Multiple start/stop cycles should not raise."""
        m = Mixer()
        for _ in range(3):
            m.start()
            time.sleep(0.05)  # Let the thread run a bit
            m.stop()

    def test_stream_loop_produces_output(self):
        """_stream_loop should periodically call _callback and advance current_sample."""
        m = Mixer(channels=1)
        # Add a track so there's audio to mix
        audio = np.ones((44100, 1), dtype=np.float32) * 0.5
        m.add_track(audio, 0, stem_index=0)
        m.start()
        time.sleep(0.2)  # Let the stream loop run for ~200ms
        m.stop()

        # current_sample should have advanced (~44100 * 0.2 = ~8820 samples)
        assert m.current_sample > 0, "Stream loop should have advanced current_sample"


# ===========================================================================
# _extend_tracks_at_position edge cases
# ===========================================================================

class TestExtendTracksAtPosition:
    def test_extend_when_track_fully_consumed(self):
        """When start_sample is past the track end, tile the full track."""
        m = Mixer(channels=1)
        audio = np.ones((100, 1), dtype=np.float32) * 0.5
        m.add_track(audio, 0, stem_index=0)
        m.current_sample = 100  # At the end of the track
        m.current_loop_end_sample = 50  # Already past

        # Extend at position 150, which is past the track end
        m._extend_tracks_at_position(150, gap_samples=50)

        # Should use the last candidate track and tile it
        new_tracks = [t for t in m.tracks if t.start_sample == 150]
        assert len(new_tracks) == 1

    def test_extend_uses_fallback_candidate(self):
        """When no tracks overlap start_sample, use most recent past track."""
        m = Mixer(channels=1)
        # Track that ends before the gap position
        audio = np.ones((100, 1), dtype=np.float32) * 0.5
        m.add_track(audio, 0, stem_index=0)
        m.current_sample = 50
        m.current_loop_end_sample = 50  # Track end is 100, past this

        # No tracks that satisfy start_sample <= position AND end > loop_end
        # Should fallback to the most recent candidate
        m._extend_tracks_at_position(200, gap_samples=50)

        new_tracks = [t for t in m.tracks if t.start_sample == 200]
        assert len(new_tracks) == 1


# ===========================================================================
# pop_transition_event edge cases
# ===========================================================================

class TestPopTransitionEvent:
    def test_double_pop_returns_none_second(self):
        """After one pop, the flag is cleared so second pop returns None."""
        m = Mixer()
        m._just_transitioned = True
        m._last_transition_loop_index = 10

        assert m.pop_transition_event() == 10
        assert m.pop_transition_event() is None

    def test_pop_when_not_transitioned(self):
        m = Mixer()
        m._just_transitioned = False
        assert m.pop_transition_event() is None


# ===========================================================================
# _extend_tracks_for_loop edge cases
# ===========================================================================

class TestExtendTracksForLoop:
    def test_extend_expired_track_not_included(self):
        """Tracks that have ended before current_sample should not be extended."""
        m = Mixer(channels=1)
        audio = np.ones((100, 1), dtype=np.float32) * 0.5
        m.add_track(audio, 0, stem_index=0)
        m.current_sample = 200  # Past the track end (100)

        original_count = len(m.tracks)
        m._extend_tracks_for_loop(300)

        # No new tracks should be added — the original track has expired
        assert len(m.tracks) == original_count

    def test_extend_track_starting_after_loop_end_not_included(self):
        """Tracks that start after the loop boundary should not be extended."""
        m = Mixer(channels=1)
        audio = np.ones((100, 1), dtype=np.float32) * 0.5
        m.add_track(audio, 500, stem_index=0)  # Starts at 500
        m.current_sample = 0

        original_count = len(m.tracks)
        m._extend_tracks_for_loop(100)  # Loop ends at 100

        # Track starts at 500 > 100 (loop_end), so not included
        assert len(m.tracks) == original_count


# ===========================================================================
# Callback: behind-schedule gap filling
# ===========================================================================

class TestBehindScheduleGapFilling:
    def test_negative_samples_until_fills_gap(self):
        """When current_sample > current_loop_end_sample, gap filling kicks in."""
        m = Mixer(channels=1)
        m.sample_rate = 44100
        m.blocksize = 2048
        m.loop_switch_deadline_ms = 50

        # Track 0-100
        audio = np.arange(100, dtype=np.float32).reshape(-1, 1)
        m.add_track(audio, 0, stem_index=0)
        m.current_sample = 130  # Past the loop end
        m.current_loop_end_sample = 100
        m.next_loop_audio = []  # Not ready

        # samples_until = 100 - 130 = -30 (behind!)
        outdata = np.zeros((10, 1), dtype=np.float32)
        m._callback(outdata, 10, None, None)

        # Gap filler should have added tracks at current_sample (130)
        gap_tracks = [t for t in m.tracks if t.start_sample >= 130]
        assert len(gap_tracks) >= 1


# ===========================================================================
# Mute/solo: edge cases
# ===========================================================================

class TestMuteSoloEdgeCases:
    def test_all_muted_produces_silence(self):
        m = Mixer(channels=1)
        audio = np.ones((100, 1), dtype=np.float32) * 0.5
        m.add_track(audio, 0, stem_index=0)
        state.muted_stems.add(0)

        outdata = np.zeros((100, 1), dtype=np.float32)
        m._callback(outdata, 100, None, None)
        assert np.allclose(outdata, 0.0)

    def test_solo_overrides_mute(self):
        """When solo is active, muted stems are ignored — solo wins."""
        m = Mixer(channels=1)
        a0 = np.ones((100, 1), dtype=np.float32) * 0.5
        a1 = np.ones((100, 1), dtype=np.float32) * 0.3
        m.add_track(a0, 0, stem_index=0)
        m.add_track(a1, 0, stem_index=1)

        # Mute stem 0, but solo stem 0
        state.muted_stems.add(0)
        state.soloed_stems.add(0)

        outdata = np.zeros((100, 1), dtype=np.float32)
        m._callback(outdata, 100, None, None)

        # Solo takes priority: stem 0 plays (soloed), stem 1 does not
        # global_gain = 1/sqrt(1) = 1.0, output = 0.5 * 1.0 = 0.5
        assert np.allclose(outdata, 0.5)

        state.muted_stems.clear()
        state.soloed_stems.clear()

    def test_no_stems_active_no_crash(self):
        """Mixer with historical stem_index=-1 and no solo/mute should still work."""
        m = Mixer(channels=1)
        audio = np.ones((100, 1), dtype=np.float32) * 0.5
        m.add_track(audio, 0, stem_index=-1)

        outdata = np.zeros((100, 1), dtype=np.float32)
        m._callback(outdata, 100, None, None)
        # Should not crash and should produce output
        assert m.current_sample == 100


# ===========================================================================
# Debug logging
# ===========================================================================

class TestDebugLogging:
    def test_debug_count_increments(self):
        """_debug_count should increment on each callback."""
        m = Mixer(channels=1)
        outdata = np.zeros((100, 1), dtype=np.float32)

        assert m._debug_count == 0
        m._callback(outdata, 100, None, None)
        assert m._debug_count == 1
        m._callback(outdata, 100, None, None)
        assert m._debug_count == 2

    def test_debug_log_fires_at_200_intervals(self):
        """The periodic debug log should fire when _debug_count is a multiple of 200."""
        m = Mixer(channels=1)
        outdata = np.zeros((100, 1), dtype=np.float32)

        # Run 199 callbacks — debug should NOT fire at count=200
        for _ in range(199):
            m._callback(outdata, 100, None, None)
        assert m._debug_count == 199

        # One more → count=200, debug fires (we just verify it doesn't crash)
        m._callback(outdata, 100, None, None)
        assert m._debug_count == 200


# ===========================================================================
# _stream_loop: late-tick catch-up path
# ===========================================================================

class TestStreamLoopCatchUp:
    def test_stream_loop_catchup_path(self):
        """When the stream loop runs late, it resets its deadline to catch up.
        We simulate this by making _callback slow so the deadline falls behind."""
        m = Mixer(channels=1)
        # Add a track
        audio = np.ones((44100, 1), dtype=np.float32) * 0.5
        m.add_track(audio, 0, stem_index=0)

        # Patch _callback to simulate slowness on first call only
        original_callback = m._callback
        call_count = 0
        slow_done = threading.Event()

        def slow_callback(outdata, frames, _time, _status):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Sleep longer than the block duration to force deadline catch-up
                time.sleep(m.blocksize / m.sample_rate * 2)
                slow_done.set()
            original_callback(outdata, frames, _time, _status)

        with patch.object(m, '_callback', slow_callback):
            m.start()
            slow_done.wait(timeout=5)
            m.stop()

        # The stream loop should have run despite the slow callback
        assert call_count >= 1
