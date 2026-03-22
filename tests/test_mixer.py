import pytest
import numpy as np
from framework_mixer import Mixer, Track
from framework_state import state

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
    mixer.add_track(audio_past, 0) # Ends at 100
    
    # Add another track in the past
    mixer.add_track(audio_past, 1000) # Ends at 2000
    
    # Add a current track
    mixer.add_track(audio_past, 5000) # Ends at 6000
    
    mixer.current_sample = 10000 # Way past all tracks
    
    outdata = np.zeros((100, 2), dtype=np.float32)
    mixer._callback(outdata, 100, None, None)
    
    # Pruning should keep 2 historical tracks
    # We added 3 tracks, all are in the past when current_sample=10000
    # Actually, pruning logic:
    # past_tracks = all tracks ending <= current_sample
    # Keep top 2 most recent past tracks
    assert len(mixer.tracks) == 2
