import pytest
import numpy as np
from app.framework.framework_mixer import Mixer
from app.framework.framework_state import state


@pytest.fixture(autouse=True)
def reset_state():
    state.reset()
    state.is_generating = True
    state.active_stems = [{"prompt": "stem0"}, {"prompt": "stem1"}]
    yield


def test_mixer_add_track():
    mixer = Mixer()
    audio = np.random.rand(1024, 2).astype(np.float32)
    mixer.add_track(audio, 0, stem_index=0)

    assert len(mixer.tracks) == 1
    assert mixer.tracks[0].stem_index == 0
    assert mixer.tracks[0].length == 1024


def test_mixer_callback_mixing():
    mixer = Mixer(channels=1)
    # Create 100 samples of 0.5 value
    audio = np.ones((100, 1), dtype=np.float32) * 0.5
    mixer.add_track(audio, 0, stem_index=0)

    outdata = np.zeros((100, 1), dtype=np.float32)
    mixer._callback(outdata, 100, None, None)

    # stem_gain_global = 1.0 / sqrt(1) = 1.0
    # Expected output: 0.5 * 1.0 = 0.5
    assert np.allclose(outdata, 0.5)
    assert mixer.current_sample == 100


def test_mixer_volume_control():
    mixer = Mixer(channels=1)
    audio = np.ones((100, 1), dtype=np.float32) * 0.5
    mixer.add_track(audio, 0, stem_index=0)

    # Set volume to 2.0
    state.stem_volumes[0] = 2.0

    outdata = np.zeros((100, 1), dtype=np.float32)
    mixer._callback(outdata, 100, None, None)

    # Expected output: 0.5 * 1.0 (global) * 2.0 (indiv) = 1.0
    assert np.allclose(outdata, 1.0)


def test_mixer_mute_solo():
    mixer = Mixer(channels=1)
    audio0 = np.ones((100, 1), dtype=np.float32) * 0.5
    audio1 = np.ones((100, 1), dtype=np.float32) * 0.5
    mixer.add_track(audio0, 0, stem_index=0)
    mixer.add_track(audio1, 0, stem_index=1)

    # Mute stem 1
    state.muted_stems.add(1)

    outdata = np.zeros((100, 1), dtype=np.float32)
    mixer._callback(outdata, 100, None, None)

    # Only stem 0 should be mixed. global_gain = 1/sqrt(1) = 1.0
    assert np.allclose(outdata, 0.5)

    # Clear and try solo
    mixer.current_sample = 0
    state.muted_stems.clear()
    state.soloed_stems.add(1)

    outdata.fill(0)
    mixer._callback(outdata, 100, None, None)

    # Only stem 1 should be mixed
    assert np.allclose(outdata, 0.5)


def test_mixer_pruning():
    mixer = Mixer()
    # Add a track in the far past
    audio_past = np.zeros((1000, 2), dtype=np.float32)
    mixer.add_track(audio_past, 0)  # Ends at 100

    # Add another track in the past
    mixer.add_track(audio_past, 1000)  # Ends at 2000

    # Add a current track
    mixer.add_track(audio_past, 5000)  # Ends at 6000

    mixer.current_sample = 10000  # Way past all tracks

    outdata = np.zeros((100, 2), dtype=np.float32)
    mixer._callback(outdata, 100, None, None)

    # Pruning should keep 2 historical tracks
    # We added 3 tracks, all are in the past when current_sample=10000
    # Actually, pruning logic:
    # past_tracks = all tracks ending <= current_sample
    # Keep top 2 most recent past tracks
    assert len(mixer.tracks) == 2


def test_mixer_set_next_loop():
    """Test seamless transition setup via set_next_loop."""
    mixer = Mixer()
    audio1 = np.ones((1000, 2), dtype=np.float32) * 0.5
    audio2 = np.ones((1000, 2), dtype=np.float32) * 0.3

    # Set up next loop tracks
    tracks_audio = [(audio1, 0), (audio2, 1)]
    mixer.set_next_loop(tracks_audio, next_loop_duration_samples=10000)

    assert len(mixer.next_loop_audio) == 2
    assert mixer._next_loop_duration == 10000
    # set_next_loop should NOT modify current_loop_end_sample (Bug 1 fix)
    assert mixer.current_loop_end_sample == 0


def test_mixer_loop_transition():
    """Test loop boundary detection and transition."""
    mixer = Mixer(channels=1)
    mixer.sample_rate = 44100

    # Add a current track
    audio = np.ones((4410, 1), dtype=np.float32) * 0.5  # 0.1 seconds
    mixer.add_track(audio, 0, stem_index=0)
    mixer.current_sample = 0
    # Set current loop end BEFORE calling set_next_loop (framework manages this)
    mixer.current_loop_end_sample = 4410

    # Set up next loop with its duration
    next_audio = np.ones((4410, 1), dtype=np.float32) * 0.3
    mixer.set_next_loop([(next_audio, 1)], next_loop_duration_samples=4410, loop_idx=1)  # 0.1 seconds

    # Simulate callback at loop boundary
    state.is_generating = True
    outdata = np.zeros((4410, 1), dtype=np.float32)

    # Advance to loop end
    mixer.current_sample = 4400
    samples_until = mixer.current_loop_end_sample - mixer.current_sample
    assert samples_until == 10

    # Callback should trigger transition
    mixer._callback(outdata, 100, None, None)

    # Next loop should have been added
    assert len(mixer.next_loop_audio) == 0  # Consumed
    state.is_generating = False


def test_mixer_callback_with_soloed_and_muted():
    """Test complex solo/mute interaction."""
    mixer = Mixer(channels=1)
    audio0 = np.ones((100, 1), dtype=np.float32) * 0.5
    audio1 = np.ones((100, 1), dtype=np.float32) * 0.5
    audio2 = np.ones((100, 1), dtype=np.float32) * 0.5
    mixer.add_track(audio0, 0, stem_index=0)
    mixer.add_track(audio1, 0, stem_index=1)
    mixer.add_track(audio2, 0, stem_index=2)

    # Solo stem 1 and mute stem 2
    state.soloed_stems.add(1)
    state.muted_stems.add(2)

    outdata = np.zeros((100, 1), dtype=np.float32)
    mixer._callback(outdata, 100, None, None)

    # Only stem 1 should be mixed due to solo
    # stem_gain_global = 1/sqrt(1) = 1.0
    assert np.allclose(outdata, 0.5)

    # Clean up
    state.soloed_stems.clear()
    state.muted_stems.clear()


def test_mixer_gain_scaling():
    """Test global gain formula (1/sqrt(num_tracks))."""
    mixer = Mixer(channels=1)

    # Add 4 tracks
    for i in range(4):
        audio = np.ones((100, 1), dtype=np.float32) * 0.5
        mixer.add_track(audio, 0, stem_index=i)

    state.is_generating = True
    outdata = np.zeros((100, 1), dtype=np.float32)
    mixer._callback(outdata, 100, None, None)

    # global_gain = 1/sqrt(4) = 0.5
    # Expected: 0.5 * 0.5 = 0.25 per track, but summed = 1.0
    # Actually with 4 tracks each at 0.5:
    # Each track contributes 0.5 * 0.5 = 0.25
    # 4 tracks * 0.25 = 1.0
    assert np.allclose(outdata, 1.0)

    state.is_generating = False


def test_mixer_multiple_solo():
    """Test that multiple soloed stems all play."""
    mixer = Mixer(channels=1)
    audio0 = np.ones((100, 1), dtype=np.float32) * 0.5
    audio1 = np.ones((100, 1), dtype=np.float32) * 0.5
    mixer.add_track(audio0, 0, stem_index=0)
    mixer.add_track(audio1, 0, stem_index=1)

    # Solo both stems
    state.soloed_stems.add(0)
    state.soloed_stems.add(1)

    outdata = np.zeros((100, 1), dtype=np.float32)
    mixer._callback(outdata, 100, None, None)

    # Both stems play, global_gain = 1/sqrt(2) ~ 0.707
    # Each stem: 0.5 * 0.707 = 0.3535
    # Summed: 0.707
    expected = 0.5 * (1.0 / np.sqrt(2)) * 2
    assert np.allclose(outdata, expected)

    # Clean up
    state.soloed_stems.clear()


# =============================================================================
# Loop Transition Tests
# =============================================================================


def test_extend_tracks_for_loop_basic():
    """Test _extend_tracks_for_loop when track ends before loop boundary."""
    mixer = Mixer(channels=1)
    mixer.sample_rate = 44100

    # Track: samples 0-100, loop boundary at 150
    audio = np.arange(100, dtype=np.float32).reshape(-1, 1)
    mixer.add_track(audio, 0, stem_index=0)
    mixer.current_sample = 50  # We're halfway through

    # Extend for loop at sample 150
    mixer._extend_tracks_for_loop(150)

    # Should have added a new track starting at track_end (100), not loop_boundary (150)
    # Because the track ends at 100, we tile from there to fill the gap
    new_tracks = [t for t in mixer.tracks if t.start_sample == 100]
    assert len(new_tracks) == 1
    # The extended track should be tiled to fill the gap to loop_boundary and beyond
    # We use repeats_needed = (150-100)//100 + 2 = 2
    assert new_tracks[0].length == 200  # Tiled 2x


def test_extend_tracks_for_loop_at_boundary():
    """Test _extend_tracks_for_loop when track ends exactly at loop boundary."""
    mixer = Mixer(channels=1)
    mixer.sample_rate = 44100

    # Track: samples 0-100, loop boundary at 100 (exactly at end)
    audio = np.arange(100, dtype=np.float32).reshape(-1, 1)
    mixer.add_track(audio, 0, stem_index=0)
    mixer.current_sample = 50

    # Extend for loop at sample 100 (exact boundary)
    mixer._extend_tracks_for_loop(100)

    # Should have added an extended track at 100
    new_tracks = [t for t in mixer.tracks if t.start_sample == 100]
    assert len(new_tracks) == 1
    # Should be the full audio tiled
    assert new_tracks[0].length == 200  # 2x tiled


def test_extend_tracks_for_loop_past_boundary():
    """Test _extend_tracks_for_loop when track extends past loop boundary."""
    mixer = Mixer(channels=1)
    mixer.sample_rate = 44100

    # Track: samples 0-100, loop boundary at 80
    audio = np.arange(100, dtype=np.float32).reshape(-1, 1)
    mixer.add_track(audio, 0, stem_index=0)
    mixer.current_sample = 50

    # Extend for loop at sample 80
    mixer._extend_tracks_for_loop(80)

    # Should have added extended track at 80 with only overlap portion
    new_tracks = [t for t in mixer.tracks if t.start_sample == 80]
    assert len(new_tracks) == 1
    # Overlap is 100 - 80 = 20 samples
    # Should repeat those 20 samples
    assert new_tracks[0].length == 40  # 2x tiled from 20 samples


def test_extend_tracks_at_position_basic():
    """Test _extend_tracks_at_position for catch-up when behind."""
    mixer = Mixer(channels=1)
    mixer.sample_rate = 44100

    # Track starting at 0, length 100
    audio = np.arange(100, dtype=np.float32).reshape(-1, 1)
    mixer.add_track(audio, 0, stem_index=0)
    mixer.current_sample = 50  # We're at sample 50

    # Simulate being 20 samples past where we should be
    # We need to fill gap from sample 50 to sample 70 (gap of 20)
    mixer._extend_tracks_at_position(50, gap_samples=20)

    # Should have added a new track at position 50
    new_tracks = [t for t in mixer.tracks if t.start_sample == 50]
    assert len(new_tracks) == 1
    # Should continue from where we were (sample 50 in original = value 50)
    # and tile the remaining samples (50-99 = 50 samples)
    assert new_tracks[0].length >= 20  # Enough to cover the gap


def test_extend_tracks_at_position_end_of_track():
    """Test _extend_tracks_at_position when exactly at track end."""
    mixer = Mixer(channels=1)
    mixer.sample_rate = 44100

    # Track starting at 0, length 100
    audio = np.arange(100, dtype=np.float32).reshape(-1, 1)
    mixer.add_track(audio, 0, stem_index=0)
    mixer.current_sample = 100  # Exactly at track end

    # Gap of 30 samples
    mixer._extend_tracks_at_position(100, gap_samples=30)

    # Should have added a new track at position 100
    new_tracks = [t for t in mixer.tracks if t.start_sample == 100]
    assert len(new_tracks) == 1
    # Should be full track tiled
    assert new_tracks[0].length >= 30


def test_loop_transition_next_loop_ready():
    """Test seamless transition when next loop is ready."""
    mixer = Mixer(channels=1)
    mixer.sample_rate = 44100
    mixer.blocksize = 2048
    mixer.loop_switch_deadline_ms = 50

    # Add current track: 0.1 seconds at 44100 sample rate = 4410 samples
    audio = np.ones((4410, 1), dtype=np.float32) * 0.5
    mixer.add_track(audio, 0, stem_index=0)
    mixer.current_sample = 0
    mixer.current_loop_end_sample = 4410

    # Set up next loop (ready before deadline)
    next_audio = np.ones((4410, 1), dtype=np.float32) * 0.3
    mixer.set_next_loop([(next_audio, 1)], next_loop_duration_samples=4410)

    state.is_generating = True
    outdata = np.zeros((4410, 1), dtype=np.float32)

    # Simulate: we're just before the switch window
    # samples_until = 4410 - 4300 = 110
    # samples_needed = 4410 + ~46 = ~4456 (way larger than 110, so we should switch)
    mixer.current_sample = 4300
    mixer._callback(outdata, 4410, None, None)

    # Next loop audio should be consumed
    assert len(mixer.next_loop_audio) == 0
    # New tracks should be added (one from next loop + original still mixing)
    # Original track ends at 4410, we're mixing [4300, 8710] with 4410 samples
    # So we should have both original and new tracks
    tracks_after = [t for t in mixer.tracks]
    assert len(tracks_after) >= 1

    state.is_generating = False


def test_loop_transition_next_loop_not_ready_extends():
    """Test that when next loop isn't ready, current loop is extended."""
    mixer = Mixer(channels=1)
    mixer.sample_rate = 44100
    mixer.blocksize = 2048
    mixer.loop_switch_deadline_ms = 50

    # Track: samples 0-100, ends at 100
    audio = np.arange(100, dtype=np.float32).reshape(-1, 1)
    mixer.add_track(audio, 0, stem_index=0)
    mixer.current_sample = 90
    mixer.current_loop_end_sample = 100  # Loop boundary
    mixer.next_loop_audio = []  # Not ready

    state.is_generating = True
    outdata = np.zeros((10, 1), dtype=np.float32)

    # At sample 90, with loop_end 100, samples_needed = 10 + ~2 = 12
    # samples_until = 100 - 90 = 10, which is <= 12, so we enter transition
    mixer._callback(outdata, 10, None, None)

    # Should have extended the track
    extended_tracks = [t for t in mixer.tracks if t.start_sample >= 100]
    assert len(extended_tracks) >= 1

    # Deadline should be extended
    assert mixer.current_loop_end_sample > 100

    state.is_generating = False


def test_loop_transition_behind_schedule_catches_up():
    """Test catch-up when significantly behind schedule."""
    mixer = Mixer(channels=1)
    mixer.sample_rate = 44100
    mixer.blocksize = 2048
    mixer.loop_switch_deadline_ms = 50

    # Track: samples 0-100
    audio = np.arange(100, dtype=np.float32).reshape(-1, 1)
    mixer.add_track(audio, 0, stem_index=0)
    mixer.current_sample = 50
    mixer.current_loop_end_sample = 100  # We're supposed to be at 100, but we're at 50!
    # Wait, this doesn't make sense. If current_sample is 50 and loop_end is 100,
    # we're NOT behind, we're ahead. Let me reconsider...

    # Actually, the issue is when we're PAST the deadline
    # Let's say we're at sample 120 but loop_end is 100
    mixer.current_sample = 120
    mixer.current_loop_end_sample = 100
    mixer.next_loop_audio = []  # Not ready

    state.is_generating = True
    outdata = np.zeros((10, 1), dtype=np.float32)

    # samples_until = 100 - 120 = -20 (NEGATIVE = we're behind)
    # samples_needed = 10 + ~2 = 12
    # -20 <= 12, so we enter transition
    mixer._callback(outdata, 10, None, None)

    # Should have catch-up tracks at current_sample (120)
    catchup_tracks = [t for t in mixer.tracks if t.start_sample >= 120]
    assert len(catchup_tracks) >= 1

    state.is_generating = False


def test_loop_transition_no_silence_when_behind():
    """Test that there's no silence even when running behind schedule."""
    mixer = Mixer(channels=1)
    mixer.sample_rate = 44100
    mixer.blocksize = 2048
    mixer.loop_switch_deadline_ms = 50

    # Create a simple repeating pattern: [1, 2, 3, 4]
    audio = np.array([[1], [2], [3], [4]], dtype=np.float32)
    mixer.add_track(audio, 0, stem_index=0)
    mixer.current_sample = 0

    # We're at the very end of the loop and next loop isn't ready
    mixer.current_loop_end_sample = 4  # Loop boundary
    mixer.next_loop_audio = []  # Not ready

    state.is_generating = True

    # First callback at sample 0, frames=4 (covers full track)
    outdata = np.zeros((4, 1), dtype=np.float32)
    mixer._callback(outdata, 4, None, None)

    # Check we got audio (not silence)
    assert np.any(outdata != 0), "Should have audio output, not silence"

    # Now simulate being behind: current_sample=4, loop_end=4, not ready
    # samples_until = 0, which triggers transition
    mixer.current_sample = 4
    outdata.fill(0)
    mixer._callback(outdata, 4, None, None)

    # We should have output (extended audio), not silence
    assert np.any(outdata != 0), "Should have extended audio, not silence"

    state.is_generating = False


def test_extend_tracks_multiple_stems():
    """Test extending multiple stems simultaneously."""
    mixer = Mixer(channels=1)
    mixer.sample_rate = 44100

    # Add two tracks
    audio1 = np.ones((100, 1), dtype=np.float32) * 1.0
    audio2 = np.ones((100, 1), dtype=np.float32) * 2.0
    mixer.add_track(audio1, 0, stem_index=0)
    mixer.add_track(audio2, 0, stem_index=1)
    mixer.current_sample = 50

    # Extend at loop boundary 100
    mixer._extend_tracks_for_loop(100)

    # Should have added 2 extended tracks
    extended = [t for t in mixer.tracks if t.start_sample == 100]
    assert len(extended) == 2


def test_clear_resets_transition_state():
    """Test that clear() properly resets loop transition state."""
    mixer = Mixer()
    mixer.current_loop_end_sample = 12345
    mixer.next_loop_audio = [("fake", 0)]

    mixer.clear()

    assert mixer.current_loop_end_sample == 0
    assert len(mixer.next_loop_audio) == 0


def test_pop_transition_event_no_transition():
    """Test pop_transition_event returns None when no transition occurred."""
    mixer = Mixer()
    mixer._just_transitioned = False

    result = mixer.pop_transition_event()

    assert result is None
    # Flag should remain False
    assert mixer._just_transitioned is False


def test_pop_transition_event_with_transition():
    """Test pop_transition_event returns index and clears flag."""
    mixer = Mixer()
    mixer._just_transitioned = True
    mixer._last_transition_loop_index = 5

    result = mixer.pop_transition_event()

    assert result == 5
    # Flag should now be cleared
    assert mixer._just_transitioned is False


def test_pop_transition_event_clears_flag_atomically():
    """Test that pop_transition_event clears flag after returning."""
    mixer = Mixer()
    mixer._just_transitioned = True
    mixer._last_transition_loop_index = 3

    result1 = mixer.pop_transition_event()
    result2 = mixer.pop_transition_event()

    assert result1 == 3
    assert result2 is None


def test_clear_resets_transition_flags():
    """Test that clear() resets transition tracking flags."""
    mixer = Mixer()
    mixer._just_transitioned = True
    mixer._last_transition_loop_index = 7
    mixer._next_loop_idx = 5

    mixer.clear()

    assert mixer._just_transitioned is False
    assert mixer._last_transition_loop_index == 0
    assert mixer._next_loop_idx == 0


def test_set_next_loop_accepts_loop_idx():
    """Test that set_next_loop accepts and stores loop_idx."""
    mixer = Mixer()
    audio = np.ones((1000, 2), dtype=np.float32)
    mixer.set_next_loop([(audio, 0)], next_loop_duration_samples=10000, loop_idx=42)

    assert mixer._next_loop_idx == 42


def test_transition_sets_flag_and_index():
    """Test that transition in callback sets _just_transitioned and _last_transition_loop_index."""
    mixer = Mixer(channels=1)
    mixer.sample_rate = 44100
    mixer.blocksize = 2048
    mixer.loop_switch_deadline_ms = 50

    # Add current track
    audio = np.ones((4410, 1), dtype=np.float32) * 0.5
    mixer.add_track(audio, 0, stem_index=0)
    mixer.current_sample = 0
    mixer.current_loop_end_sample = 4410

    # Set up next loop with loop_idx = 5
    next_audio = np.ones((4410, 1), dtype=np.float32) * 0.3
    mixer.set_next_loop([(next_audio, 1)], next_loop_duration_samples=4410, loop_idx=5)

    state.is_generating = True
    outdata = np.zeros((4410, 1), dtype=np.float32)

    # Advance to trigger transition
    mixer.current_sample = 4300
    mixer._callback(outdata, 4410, None, None)

    # Transition flag should be set
    assert mixer._just_transitioned is True
    assert mixer._last_transition_loop_index == 5

    state.is_generating = False
