import pytest
from framework_state import GlobalState

def test_initial_state():
    state = GlobalState()
    assert state.current_bpm == 120
    assert state.current_key == "C minor"
    assert state.is_generating is False
    assert state.loop_count == 0
    assert state.generation_cfg_scale == 7.0
    assert state.generation_steps == 15

def test_state_reset():
    state = GlobalState()
    state.current_bpm = 140
    state.current_key = "G major"
    state.is_generating = True
    state.loop_count = 5
    state.stem_volumes[0] = 0.5
    state.muted_stems.add(1)
    
    state.reset()
    
    assert state.current_bpm == 120
    assert state.current_key == "C minor"
    assert state.is_generating is False
    assert state.current_set_name == "System Reset"
    assert state.stem_volumes == {}
    assert state.muted_stems == set()

def test_add_custom_instrument(tmp_path):
    # Mocking instruments file path to use temp dir
    state = GlobalState()
    test_file = tmp_path / "test_instruments.json"
    state.instruments_file = str(test_file)
    
    state.add_custom_instrument("Theremin")
    assert "Theremin" in state.categorized_instruments["Custom"]
    
    # Verify persistence
    new_state = GlobalState()
    new_state.instruments_file = str(test_file)
    new_state.categorized_instruments = new_state._load_instruments()
    assert "Theremin" in new_state.categorized_instruments["Custom"]

def test_audio_clients():
    state = GlobalState()
    import queue
    q = queue.Queue()
    
    state.add_audio_client(q)
    assert q in state.audio_clients
    
    state.broadcast_audio(b"testdata")
    assert q.get() == b"testdata"
    
    state.remove_audio_client(q)
    assert q not in state.audio_clients
