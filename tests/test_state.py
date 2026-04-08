import pytest
import os
from unittest.mock import MagicMock, patch
from app.framework.framework_state import GlobalState

def test_initial_state():
    state = GlobalState()
    assert state.current_bpm == 120
    assert state.current_key == "C minor"
    assert state.is_generating is False
    assert state.loop_count == 0
    assert state.generation_cfg_scale == 7.0
    assert state.generation_steps == 50

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


def test_add_custom_instrument_with_family(tmp_path):
    """Test adding a custom instrument with an explicit family (new behavior)."""
    with patch("app.framework.framework_state.GlobalState._load_instruments", return_value={"Custom": []}):
        state = GlobalState()
    state.instruments_file = str(tmp_path / "test_instruments.json")

    state.add_custom_instrument("My Synth", "Synth")
    instruments = state.get_custom_instruments()
    assert instruments == {"My Synth": "Synth"}


def test_add_custom_instrument_extends_schema_families(tmp_path):
    """Test that adding an instrument registers its family with the schema."""
    from app.lib.constants import get_all_major_families

    with patch("app.framework.framework_state.GlobalState._load_instruments", return_value={"Custom": []}):
        state = GlobalState()
    state.instruments_file = str(tmp_path / "test_instruments.json")

    state.add_custom_instrument("My Custom", "Brass")
    assert "Brass" in get_all_major_families()


def test_get_custom_instruments_returns_empty_when_none(tmp_path):
    """Test get_custom_instruments returns {} when no custom instruments exist."""
    with patch("app.framework.framework_state.GlobalState._load_instruments", return_value={"Custom": []}):
        state = GlobalState()
    state.instruments_file = str(tmp_path / "test_instruments.json")
    assert state.get_custom_instruments() == {}

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


def test_trigger_shutdown():
    """Test shutdown event is set and is_running is False."""
    state = GlobalState()
    state.is_running = True
    state.shutdown_event.clear()

    mock_process = MagicMock()
    state.active_subprocesses.add(mock_process)
    state.audio_clients = []

    state.trigger_shutdown()

    assert state.shutdown_event.is_set() is True
    assert state.is_running is False
    assert state.is_generating is False


def test_register_subprocess():
    """Test subprocess registration."""
    state = GlobalState()

    mock_process = MagicMock()
    state.register_subprocess(mock_process)

    assert mock_process in state.active_subprocesses


def test_unregister_subprocess():
    """Test subprocess unregistration."""
    state = GlobalState()

    mock_process = MagicMock()
    state.active_subprocesses.add(mock_process)

    state.unregister_subprocess(mock_process)

    assert mock_process not in state.active_subprocesses


def test_broadcast_audio_to_multiple_clients():
    """Test broadcasting to multiple audio clients."""
    state = GlobalState()
    import queue

    q1 = queue.Queue()
    q2 = queue.Queue()
    state.audio_clients = [q1, q2]

    state.broadcast_audio(b"testdata")

    assert q1.get() == b"testdata"
    assert q2.get() == b"testdata"


def test_shutdown_poisons_audio_clients():
    """Test that shutdown sends poison pills to all audio clients."""
    state = GlobalState()
    import queue

    q1 = queue.Queue()
    q2 = queue.Queue()
    state.audio_clients = [q1, q2]
    state.is_running = True
    state.shutdown_event.clear()
    state.active_subprocesses = set()

    state.trigger_shutdown()

    # Check poison pills were sent
    assert q1.get() is None
    assert q2.get() is None


def test_show_recording_state_initialization():
    """Test that show recording state is properly initialized."""
    state = GlobalState()

    # Show recording state
    assert state.current_show_id is None
    assert state.current_show_start_time is None
    assert state.is_show_recording is False
    assert state.llm_interaction_buffer == []
    assert state.action_buffer == []
    assert state.current_show_audio_file is None

    # Playback state
    assert state.currently_playing_show_id is None
    assert state.is_playback_active is False


def test_show_recording_state_transitions():
    """Test show recording state transitions."""
    state = GlobalState()

    # Start recording
    state.is_show_recording = True
    state.current_show_id = 1
    state.current_show_start_time = 1234567890.0
    state.llm_interaction_buffer = [{"test": "interaction"}]
    state.action_buffer = [{"test": "action"}]

    assert state.is_show_recording is True
    assert state.current_show_id == 1
    assert len(state.llm_interaction_buffer) == 1

    # Stop recording
    state.is_show_recording = False
    state.current_show_id = None
    state.current_show_start_time = None
    state.llm_interaction_buffer = []
    state.action_buffer = []

    assert state.is_show_recording is False
    assert state.current_show_id is None
    assert state.llm_interaction_buffer == []


def test_broadcast_audio_writes_to_show_file():
    """Test that broadcast_audio writes to show audio file when recording."""
    state = GlobalState()

    # Create a mock file
    mock_file = MagicMock()
    state.current_show_audio_file = mock_file
    state.is_show_recording = True

    state.broadcast_audio(b"test_audio_data")

    # Verify file.write was called
    mock_file.write.assert_called_once_with(b"test_audio_data")


def test_broadcast_audio_skips_show_file_when_none():
    """Test that broadcast_audio doesn't fail when show file is None."""
    state = GlobalState()

    state.current_show_audio_file = None
    state.is_show_recording = True

    # Should not raise
    state.broadcast_audio(b"test_audio_data")


def test_broadcast_audio_skips_when_not_recording():
    """Test that broadcast_audio doesn't write to show file when not recording."""
    state = GlobalState()

    mock_file = MagicMock()
    state.current_show_audio_file = mock_file
    state.is_show_recording = False

    state.broadcast_audio(b"test_audio_data")

    # Verify file.write was NOT called
    mock_file.write.assert_not_called()


def test_flatten_instruments():
    """Test _flatten_instruments returns all instruments from all categories."""
    state = GlobalState()

    flattened = state._flatten_instruments()

    # Should contain instruments from all default categories
    assert "Electronic Drums" in flattened
    assert "808 Bass" in flattened
    assert "Acoustic Drums" in flattened
    assert "Violin" in flattened


def test_update_available_instruments():
    """Test update_available_instruments sets the list."""
    state = GlobalState()

    new_instruments = ["Synth Lead", "Synth Pad", "808 Bass"]

    state.update_available_instruments(new_instruments)

    assert state.available_instruments == new_instruments


def test_add_audio_client_thread_safety():
    """Test add_audio_client is thread-safe."""
    state = GlobalState()
    import queue

    q1 = queue.Queue()
    q2 = queue.Queue()

    state.add_audio_client(q1)
    state.add_audio_client(q2)

    assert q1 in state.audio_clients
    assert q2 in state.audio_clients
    assert len(state.audio_clients) == 2


def test_remove_audio_client():
    """Test remove_audio_client removes the client."""
    state = GlobalState()
    import queue

    q = queue.Queue()
    state.audio_clients.append(q)

    state.remove_audio_client(q)

    assert q not in state.audio_clients


def test_remove_audio_client_not_in_list():
    """Test remove_audio_client handles client not in list gracefully."""
    state = GlobalState()
    import queue

    q = queue.Queue()

    # Should not raise
    state.remove_audio_client(q)


def test_register_and_unregister_subprocess():
    """Test subprocess registration lifecycle."""
    state = GlobalState()

    mock_process = MagicMock()
    mock_process.pid = 12345

    # Register
    state.register_subprocess(mock_process)
    assert mock_process in state.active_subprocesses

    # Unregister
    state.unregister_subprocess(mock_process)
    assert mock_process not in state.active_subprocesses


def test_broadcast_audio_with_shutdown():
    """Test broadcast_audio returns early when shutdown is set."""
    state = GlobalState()
    state.shutdown_event.set()

    import queue
    q = queue.Queue()
    state.audio_clients = [q]

    # Should return early without sending
    state.broadcast_audio(b"test")

    # Queue should be empty
    assert q.empty()


def test_broadcast_audio_drops_slow_client():
    """Test broadcast_audio drops clients that are too slow."""
    state = GlobalState()
    import queue

    q = MagicMock()
    q.put_nowait.side_effect = queue.Full()

    state.audio_clients = [q]
    state.is_show_recording = False
    state.shutdown_event.clear()

    # Should not raise despite full queue
    state.broadcast_audio(b"test")

    # The client should still be in the list (we don't remove on Full)
    assert q in state.audio_clients


def test_add_custom_instrument_with_existing():
    """Test adding instrument that already exists in Custom category."""
    state = GlobalState()
    import tempfile
    import os

    test_file = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    test_file.write('{"Custom": ["Theremin"]}')
    test_file.close()
    state.instruments_file = test_file.name

    # Try to add existing instrument
    state.categorized_instruments = state._load_instruments()
    original_count = len(state.categorized_instruments["Custom"])

    state.add_custom_instrument("Theremin")
    # Should not add duplicate
    assert len(state.categorized_instruments["Custom"]) == original_count

    os.unlink(test_file.name)


def test_add_custom_instrument_empty_name():
    """Test adding instrument with empty name is ignored."""
    import tempfile
    import os

    state = GlobalState()
    test_file = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    test_file.write('{"Custom": []}')
    test_file.close()
    state.instruments_file = test_file.name

    state.categorized_instruments = state._load_instruments()

    # Try to add empty instrument
    state.add_custom_instrument("")
    # Should not add empty string
    assert "" not in state.categorized_instruments["Custom"]

    os.unlink(test_file.name)


def test_save_instruments_creates_file():
    """Test save_instruments creates the file."""
    import tempfile
    import os

    state = GlobalState()
    test_file = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    test_file.close()
    state.instruments_file = test_file.name
    state.categorized_instruments = {"Custom": ["New Instrument"]}

    state.save_instruments()

    assert os.path.exists(test_file.name)
    os.unlink(test_file.name)


def test_load_instruments_with_invalid_json():
    """Test _load_instruments returns defaults on invalid JSON."""
    import tempfile
    import os

    state = GlobalState()
    test_file = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    test_file.write('not valid json {')
    test_file.close()
    state.instruments_file = test_file.name

    result = state._load_instruments()

    # Should return defaults
    assert "Electronic & Dance" in result
    os.unlink(test_file.name)


def test_broadcast_audio_with_recording():
    """Test broadcast_audio writes to file handle when recording (not buffered list)."""
    state = GlobalState()
    import queue

    mock_handle = MagicMock()
    state.is_recording = True
    state.recording_file_handle = mock_handle
    state.current_show_audio_file = None
    state.shutdown_event.clear()

    q = queue.Queue()
    state.audio_clients = [q]

    state.broadcast_audio(b"test_data")

    # Should write to file handle (streaming, not buffered)
    mock_handle.write.assert_called_once_with(b"test_data")
    # And put in queue
    assert q.get() == b"test_data"


def test_trigger_shutdown_with_no_audio_clients():
    """Test trigger_shutdown doesn't fail when no audio clients."""
    state = GlobalState()
    state.is_running = True
    state.shutdown_event.clear()
    state.audio_clients = []
    state.active_subprocesses = set()

    # Should not raise
    state.trigger_shutdown()

    assert state.shutdown_event.is_set()
    assert state.is_running is False


def test_trigger_shutdown_with_exception_in_subprocess():
    """Test trigger_shutdown handles subprocess exceptions gracefully."""
    state = GlobalState()
    state.is_running = True
    state.shutdown_event.clear()
    state.audio_clients = []

    mock_process = MagicMock()
    mock_process.pid = 12345
    mock_process.kill.side_effect = Exception("Process already dead")
    state.active_subprocesses.add(mock_process)

    # Should not raise
    state.trigger_shutdown()

    # Process should still be removed from set
    assert mock_process not in state.active_subprocesses


def test_register_subprocess_thread_safety():
    """Test subprocess registration is thread-safe via lock."""
    import threading
    state = GlobalState()

    mock_process = MagicMock()
    mock_process.pid = 12345

    # Register from multiple threads
    def register():
        state.register_subprocess(mock_process)

    threads = [threading.Thread(target=register) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert mock_process in state.active_subprocesses


def test_unregister_subprocess_thread_safety():
    """Test subprocess unregistration is thread-safe."""
    import threading
    state = GlobalState()

    mock_process = MagicMock()
    mock_process.pid = 12345
    state.active_subprocesses.add(mock_process)

    def unregister():
        state.unregister_subprocess(mock_process)

    threads = [threading.Thread(target=unregister) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert mock_process not in state.active_subprocesses


def test_broadcast_audio_to_multiple_clients_with_full_queue():
    """Test broadcast_audio handles one full queue but continues to others."""
    import queue
    state = GlobalState()

    q1 = MagicMock()
    q1.put_nowait.side_effect = None  # Success

    q2 = MagicMock()
    q2.put_nowait.side_effect = queue.Full()  # Full

    state.audio_clients = [q1, q2]
    state.is_recording = False
    state.shutdown_event.clear()

    # Should not raise even though q2 is full
    state.broadcast_audio(b"test")

    # q1 should have received the data
    q1.put_nowait.assert_called_once_with(b"test")


class TestStateDefaults:
    """Test state default values."""

    @patch.dict(os.environ, {}, clear=True)
    def test_default_llm_config(self):
        """Test default LLM configuration values."""
        state = GlobalState()

        assert state.llm_base_url == "http://localhost:1234/v1"
        assert state.llm_api_key == "not-needed"
        assert state.llm_model == "local-model"

    def test_default_generation_config(self):
        """Test default generation configuration."""
        state = GlobalState()

        assert state.generation_cfg_scale == 7.0
        assert state.generation_steps == 50

    def test_initial_recording_state(self):
        """Test initial recording state is False."""
        state = GlobalState()

        assert state.is_recording is False
        assert state.recording_file_path is None
        assert state.recording_format == "wav"
        # recording_chunks removed; data streams to recording_file_handle instead
        assert state.recording_file_handle is None
        assert not hasattr(state, "recording_chunks")


class TestStateLockBehavior:
    """Test state lock behavior."""

    def test_lock_is_asyncio_lock(self):
        """state.lock must be asyncio.Lock so it doesn't block the event loop."""
        import asyncio
        state = GlobalState()
        assert isinstance(state.lock, asyncio.Lock), (
            f"state.lock should be asyncio.Lock, got {type(state.lock)}"
        )

    def test_sync_lock_is_threading_lock(self):
        """state.sync_lock must be threading.Lock for the Mixer and broadcast_audio."""
        import threading
        state = GlobalState()
        assert hasattr(state, "sync_lock")
        # threading.Lock() returns a _thread.lock
        lock_type_name = type(state.sync_lock).__name__
        assert "lock" in lock_type_name.lower()

    def test_state_resets_lock_unchanged(self):
        """Test that reset() doesn't replace the lock object itself."""
        state = GlobalState()
        original_lock = state.lock
        state.reset()
        assert state.lock is original_lock


class TestLoopSyncState:
    """Test the loop synchronization state fields."""

    def test_initial_loop_sync_fields(self):
        """Test that loop sync fields are properly initialized."""
        state = GlobalState()

        assert state.currently_playing_loop_index == 0
        assert state.currently_playing_stems == []
        assert state.currently_playing_set_name == ""
        assert state.currently_playing_reasoning == ""
        assert state.loop_history == []

    def test_record_loop_transition(self):
        """Test record_loop_transition updates state correctly."""
        state = GlobalState()

        stems = [{"prompt": "stem1"}, {"prompt": "stem2"}]
        state.record_loop_transition(1, stems, "Test Set", "Test reasoning")

        assert state.currently_playing_loop_index == 1
        assert state.currently_playing_stems == stems
        assert state.currently_playing_set_name == "Test Set"
        assert state.currently_playing_reasoning == "Test reasoning"
        assert len(state.loop_history) == 1
        assert state.loop_history[0]["loop_index"] == 1
        assert state.loop_history[0]["set_name"] == "Test Set"
        assert state.loop_history[0]["stems"] == stems

    def test_record_loop_transition_rolling_history(self):
        """Test that loop_history is capped at 10 entries."""
        state = GlobalState()

        # Add 12 transitions
        for i in range(1, 13):
            stems = [{"prompt": f"stem{j}"} for j in range(3)]
            state.record_loop_transition(i, stems, f"Set {i}", f"Reasoning {i}")

        # Should only keep last 10
        assert len(state.loop_history) == 10
        assert state.loop_history[0]["loop_index"] == 3  # First one was dropped
        assert state.loop_history[9]["loop_index"] == 12  # Last one kept

    def test_reset_clears_loop_sync_fields(self):
        """Test that reset() clears all loop sync fields."""
        state = GlobalState()

        state.currently_playing_loop_index = 5
        state.currently_playing_stems = [{"prompt": "test"}]
        state.currently_playing_set_name = "Test"
        state.currently_playing_reasoning = "Test reason"
        state.loop_history.append({"loop_index": 1})

        state.reset()

        assert state.currently_playing_loop_index == 0
        assert state.currently_playing_stems == []
        assert state.currently_playing_set_name == ""
        assert state.currently_playing_reasoning == ""
        assert state.loop_history == []

    def test_record_loop_transition_thread_safety(self):
        """Test that record_loop_transition uses sync_lock."""
        import threading
        state = GlobalState()

        results = []

        def record(idx):
            state.record_loop_transition(idx, [{"prompt": f"stem{idx}"}], f"Set {idx}", f"Reason {idx}")
            results.append(idx)

        threads = [threading.Thread(target=record, args=(i,)) for i in range(1, 6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All should have recorded successfully
        assert len(state.loop_history) == 5
        assert sorted(results) == [1, 2, 3, 4, 5]
