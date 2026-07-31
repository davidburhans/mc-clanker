import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.routes import api_router as router
from app.framework.framework_state import state
import numpy as np
import json
import time
from unittest.mock import MagicMock, patch

@pytest.fixture
def client():
    from app.app_ui import app
    return TestClient(app)

@pytest.fixture(autouse=True)
def reset_state():
    state.reset()
    state.active_stems = []
    state.stem_volumes = {}
    state.muted_stems = set()
    state.soloed_stems = set()
    yield

def test_get_state(client):
    response = client.get("/api/state")
    assert response.status_code == 200
    data = response.json()
    assert data["current_bpm"] == 120
    assert data["is_generating"] is False

def test_update_state(client):
    response = client.post("/api/state", json={"target_bpm_override": 128, "is_generating": True})
    assert response.status_code == 200
    assert state.target_bpm_override == 128
    assert state.is_generating is True


class TestStateSchemaValidation:
    """Test that API rejects BPM/key values not in the LLM schema enum."""

    def test_state_update_rejects_invalid_bpm(self, client):
        """POST /api/state with BPM not in schema enum → 422."""
        response = client.post("/api/state", json={"target_bpm_override": 135})
        assert response.status_code == 422, f"Expected 422 for BPM 135, got {response.status_code}"

        response = client.post("/api/state", json={"target_bpm_override": 80})
        assert response.status_code == 422

        response = client.post("/api/state", json={"target_bpm_override": 160})
        assert response.status_code == 422

    def test_state_update_accepts_valid_bpm(self, client):
        """POST /api/state with BPM in schema enum → 200."""
        for bpm in [100, 110, 120, 128, 130, 140, 150]:
            response = client.post("/api/state", json={"target_bpm_override": bpm})
            assert response.status_code == 200, f"BPM {bpm} should be accepted"

    def test_state_update_rejects_invalid_key(self, client):
        """POST /api/state with key not in schema enum → 422."""
        response = client.post("/api/state", json={"target_key_override": "X major"})
        assert response.status_code == 422

    def test_state_update_accepts_valid_key(self, client):
        """POST /api/state with valid key → 200."""
        for key in ["C major", "C minor", "C# major", "G# minor", "B major", "B minor"]:
            response = client.post("/api/state", json={"target_key_override": key})
            assert response.status_code == 200, f"Key {key} should be accepted"

    def test_state_update_rejects_bpm_zero(self, client):
        """Zero BPM should be rejected."""
        response = client.post("/api/state", json={"target_bpm_override": 0})
        assert response.status_code == 422

    def test_state_update_rejects_bpm_negative(self, client):
        """Negative BPM should be rejected."""
        response = client.post("/api/state", json={"target_bpm_override": -10})
        assert response.status_code == 422

def test_generation_config(client):
    # Get initial
    response = client.get("/api/generation-config")
    assert response.status_code == 200
    assert response.json()["steps"] == 50
    
    # Update
    response = client.post("/api/generation-config", json={"cfg_scale": 8.5, "steps": 20})
    assert response.status_code == 200
    assert state.generation_cfg_scale == 8.5
    assert state.generation_steps == 20

def test_stem_control(client):
    # Setup some fake stems
    state.active_stems = [{"prompt": "drums"}, {"prompt": "bass"}]
    
    # Get stems
    response = client.get("/api/stems")
    assert response.status_code == 200
    stems = response.json()
    assert len(stems) == 2
    assert stems[0]["prompt"] == "drums"
    assert stems[0]["volume"] == 1.0
    
    # Update volume
    response = client.post("/api/stems/0/volume", json={"volume": 1.5})
    assert response.status_code == 200
    assert state.stem_volumes[0] == 1.5
    
    # Toggle mute
    response = client.post("/api/stems/1/mute")
    assert response.status_code == 200
    assert 1 in state.muted_stems
    
    # Toggle solo
    response = client.post("/api/stems/0/solo")
    assert response.status_code == 200
    assert 0 in state.soloed_stems

def test_download_stem_not_found(client):
    response = client.get("/api/stems/99/download")
    assert response.status_code == 404

def test_download_stem_success(client):
    # Setup stem and fake audio data
    prompt = "synth"
    state.active_stems = [{"prompt": prompt}]
    audio_data = np.zeros(44100, dtype=np.int16)
    state.last_generated_stems[prompt] = audio_data
    
    response = client.get("/api/stems/0/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert len(response.content) > 0

def test_get_models(client):
    response = client.get("/api/models")
    assert response.status_code == 200
    data = response.json()
    assert "models" in data

def test_update_models(client):
    # Test with a model that exists in the config
    response = client.post("/api/models", json={"model_id": "foundation-1", "enabled": True})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


def test_get_instruments(client):
    """Test /api/instruments returns categorized instruments."""
    response = client.get("/api/instruments")
    assert response.status_code == 200
    data = response.json()
    # Should have instrument categories
    assert isinstance(data, dict)


def test_get_llm_config(client):
    """Test /api/llm-config returns config."""
    response = client.get("/api/llm-config")
    assert response.status_code == 200
    data = response.json()
    assert "base_url" in data
    assert "api_key" in data
    assert "model" in data


def test_update_llm_config(client):
    """Test /api/llm-config POST updates state."""
    response = client.post("/api/llm-config", json={
        "base_url": "http://localhost:9999/v1",
        "model": "test-model"
    })
    assert response.status_code == 200
    assert state.llm_base_url == "http://localhost:9999/v1"
    assert state.llm_model == "test-model"


def test_export_start(client, monkeypatch, tmp_path):
    """Test export start endpoint + that a full start→stream→stop yields a valid WAV.

    Regression for review C4: recordings were headerless raw PCM served as
    audio/wav (unplayable). A WAV header is now written at open and sizes patched
    at close, so the stopped file is a valid, playable WAV.
    """
    import wave
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    try:
        response = client.post("/api/export/start", json={"format": "wav"})
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "started"
        assert "file_path" in data
        file_path = data["file_path"]

        # Simulate broadcast_audio streaming 1000 frames of stereo int16 LE PCM
        # straight into the data chunk via handle.write().
        handle = state.recording_file_handle
        assert handle is not None, "export start must open a recording handle"
        handle.write(b"\x00\x00" * (1000 * 2))

        stop = client.post("/api/export/stop")
        assert stop.status_code == 200
        # The stopped file must be a valid, playable WAV (RIFF header + patched sizes).
        with wave.open(file_path, "rb") as wf:
            assert wf.getnchannels() == 2
            assert wf.getframerate() == 44100
            assert wf.getsampwidth() == 2
            assert wf.getnframes() == 1000
    finally:
        # reset_state autouse does not reset recording flags; clean up explicitly
        # so a leaked open handle does not poison later tests.
        if getattr(state, "recording_file_handle", None) is not None:
            try:
                state.recording_file_handle.close()
            except OSError:
                pass
        state.recording_file_handle = None
        state.is_recording = False


def test_export_start_already_recording(client, monkeypatch, tmp_path):
    """Test export start returns 400 when already recording."""
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    state.is_recording = True
    try:
        response = client.post("/api/export/start", json={"format": "wav"})
        assert response.status_code == 400
    finally:
        state.is_recording = False


def test_export_stop(client):
    """Test export stop endpoint."""
    state.is_recording = True
    state.recording_file_path = "/exports/test.wav"
    state.recording_format = "wav"
    state.recording_start_time = time.time() - 10

    with patch("threading.Thread"):
        response = client.post("/api/export/stop")
    assert response.status_code == 200
    data = response.json()
    assert "file_path" in data
    assert "duration" in data
    state.is_recording = False


def test_export_stop_not_recording(client):
    """Test export stop returns 400 when not recording."""
    state.is_recording = False
    response = client.post("/api/export/stop")
    assert response.status_code == 400


def test_show_stop(client):
    """Test show stop endpoint."""
    state.is_show_started = True
    response = client.post("/api/show/stop")
    assert response.status_code == 200
    assert state.is_show_started is False


# Removed model status/vram tests


def test_custom_stem_create(client):
    """Test /api/stems/custom POST."""
    response = client.post("/api/stems/custom", json={
        "instrument": "Synth",
        "prompt": "Warm pad",
        "model_id": "test-model"
    })
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "stem_index" in data
    # Verify stem was added to next_stems
    assert len(state.next_stems) == 1
    assert state.next_stems[0]["instrument"] == "Synth"


def test_remove_next_stem(client):
    """Test /api/stems/next/{index} DELETE."""
    state.next_stems = [{"prompt": "stem1"}, {"prompt": "stem2"}, {"prompt": "stem3"}]

    response = client.delete("/api/stems/next/1")
    assert response.status_code == 200
    assert len(state.next_stems) == 2
    assert state.next_stems[1]["prompt"] == "stem3"


def test_remove_next_stem_out_of_range(client):
    """Test 404 when removing out-of-range stem."""
    state.next_stems = [{"prompt": "stem1"}]

    response = client.delete("/api/stems/next/5")
    assert response.status_code == 404


def test_get_audience_message(client):
    """Test /api/message/audience GET."""
    state.audience_message = "Test message"
    state.audience_message_ts = 1234567890

    response = client.get("/api/message/audience")
    assert response.status_code == 200
    data = response.json()
    assert data["message"] == "Test message"
    assert data["timestamp"] == 1234567890


def test_send_audience_message(client):
    """Test /api/message/audience POST."""
    response = client.post("/api/message/audience", json={"message": "Hello audience!"})
    assert response.status_code == 200
    assert state.audience_message == "Hello audience!"


def test_health_check(client):
    """Test /api/health endpoint."""
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "is_running" in data


def test_download_stem_previous_set(client):
    """Test downloading from previous set."""
    prompt = "previous synth"
    state.previous_stems = [{"prompt": prompt}]
    audio_data = np.zeros(44100, dtype=np.int16)
    state.last_generated_stems[prompt] = audio_data

    response = client.get("/api/stems/0/download?set=previous")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"


def test_download_stem_next_set(client):
    """Test downloading from next set."""
    prompt = "next synth"
    state.next_stems = [{"prompt": prompt}]
    audio_data = np.zeros(44100, dtype=np.int16)
    state.last_generated_stems[prompt] = audio_data

    response = client.get("/api/stems/0/download?set=next")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"


def test_update_model_not_found(client):
    """Test updating non-existent model returns 404 error."""
    with patch("os.path.exists", return_value=False):
        response = client.post("/api/models", json={"model_id": "nonexistent", "enabled": True})
    # File doesn't exist, returns 404
    assert response.status_code == 404


def test_update_state_with_bpm_override(client):
    """Test bpm override is applied immediately when not generating."""
    state.is_generating = False
    state.current_bpm = 120

    response = client.post("/api/state", json={"target_bpm_override": 140})
    assert response.status_code == 200

    # When not generating, bpm should be applied immediately
    assert state.target_bpm_override == 140
    assert state.current_bpm == 140


def test_update_state_with_key_override(client):
    """Test key override is applied immediately when not generating."""
    state.is_generating = False
    state.current_key = "C minor"

    response = client.post("/api/state", json={"target_key_override": "G major"})
    assert response.status_code == 200

    # When not generating, key should be applied immediately
    assert state.target_key_override == "G major"
    assert state.current_key == "G major"


def test_update_state_with_user_override(client):
    """Test user override vibe is set."""
    response = client.post("/api/state", json={"user_override": "Make it darker"})
    assert response.status_code == 200

    assert state.user_override == "Make it darker"


def test_update_state_with_should_reset(client):
    """Test should_reset flag is set."""
    response = client.post("/api/state", json={"should_reset": True})
    assert response.status_code == 200

    assert state.should_reset is True


def test_update_state_with_available_instruments(client):
    """Test available instruments list is updated."""
    new_instruments = ["Synth", "Drums", "Bass"]

    response = client.post("/api/state", json={"available_instruments": new_instruments})
    assert response.status_code == 200

    assert state.available_instruments == new_instruments


def test_stem_volume_update_multiple(client):
    """Test updating volume for multiple stems."""
    state.active_stems = [{"prompt": "drums"}, {"prompt": "bass"}, {"prompt": "synth"}]

    # Update volumes for multiple stems
    response = client.post("/api/stems/0/volume", json={"volume": 0.5})
    assert response.status_code == 200
    assert state.stem_volumes[0] == 0.5

    response = client.post("/api/stems/1/volume", json={"volume": 1.5})
    assert response.status_code == 200
    assert state.stem_volumes[1] == 1.5

    response = client.post("/api/stems/2/volume", json={"volume": 0.8})
    assert response.status_code == 200
    assert state.stem_volumes[2] == 0.8


def test_stem_toggle_mute_off(client):
    """Test toggling mute off for a stem."""
    state.active_stems = [{"prompt": "drums"}]
    state.muted_stems = {0}  # Already muted

    response = client.post("/api/stems/0/mute")
    assert response.status_code == 200
    assert 0 not in state.muted_stems


def test_stem_toggle_solo_off(client):
    """Test toggling solo off for a stem."""
    state.active_stems = [{"prompt": "drums"}]
    state.soloed_stems = {0}  # Already soloed

    response = client.post("/api/stems/0/solo")
    assert response.status_code == 200
    assert 0 not in state.soloed_stems


def test_download_stem_out_of_range(client):
    """Test download returns 404 when stem index is out of range."""
    state.active_stems = [{"prompt": "synth"}]
    state.cache_stem("synth", np.zeros(44100, dtype=np.int16))

    response = client.get("/api/stems/99/download")
    assert response.status_code == 404


def test_get_state_returns_all_fields(client):
    """Test get_state returns all expected fields."""
    state.active_stems = [{"prompt": "drums"}]
    state.llm_reasoning = "Test reasoning"
    state.loop_count = 5

    response = client.get("/api/state")
    assert response.status_code == 200
    data = response.json()

    assert "current_set_name" in data
    assert "current_bpm" in data
    assert "current_key" in data
    assert "active_stems" in data
    assert "llm_reasoning" in data
    assert "is_generating" in data
    assert "loop_count" in data
    assert "last_actions" in data


def test_generation_config_update_partial(client):
    """Test partial update of generation config."""
    # First set both
    response = client.post("/api/generation-config", json={"cfg_scale": 8.0, "steps": 40})
    assert response.status_code == 200

    # Then update just steps
    response = client.post("/api/generation-config", json={"steps": 60})
    assert response.status_code == 200

    assert state.generation_cfg_scale == 8.0  # Unchanged
    assert state.generation_steps == 60


def test_llm_config_update_partial(client):
    """Test partial update of LLM config."""
    response = client.post("/api/llm-config", json={"model": "new-model"})
    assert response.status_code == 200

    assert state.llm_model == "new-model"
    # base_url and api_key should be unchanged (or defaults)
    assert state.llm_base_url is not None


def test_export_start_default_format(client, monkeypatch, tmp_path):
    """Test export start uses default wav format."""
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    try:
        response = client.post("/api/export/start", json={})
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "started"
    finally:
        if getattr(state, "recording_file_handle", None) is not None:
            try:
                state.recording_file_handle.close()
            except OSError:
                pass
        state.recording_file_handle = None
        state.is_recording = False


def test_show_stop_when_not_started(client):
    """Test stopping show when not started."""
    state.is_show_started = False

    response = client.post("/api/show/stop")
    # Should still return ok (idempotent)
    assert response.status_code == 200


class TestAuthRoutes:
    """Test auth API endpoints."""

    @pytest.fixture(autouse=True)
    def reset_state(self):
        state.reset()
        state.active_stems = []
        state.stem_volumes = {}
        state.muted_stems = set()
        state.soloed_stems = set()
        yield

    @pytest.fixture
    def mock_db_session(self):
        """Mock database session for auth routes."""
        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_session.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.first.return_value = None
        return mock_session

    def test_register_missing_fields(self, client):
        """Test register with missing fields returns 422."""
        response = client.post("/api/auth/register", json={})
        assert response.status_code == 422

    def test_login_missing_fields(self, client):
        """Test login with missing fields returns 422."""
        response = client.post("/api/auth/login", json={})
        assert response.status_code == 422

    def test_me_unauthenticated(self, client):
        """Test /api/auth/me returns 401 when not authenticated."""
        response = client.get("/api/auth/me")
        assert response.status_code == 401


class TestGenerateAudiencePassword:
    """Test generate_audience_password function."""

    def test_generate_password_length(self):
        """Test that generated password has sufficient length."""
        from app.routes.utils import generate_audience_password

        password = generate_audience_password()

        # URL-safe base64 token of 16 bytes = ~22 characters
        assert len(password) >= 20

    def test_generate_password_uniqueness(self):
        """Test that generated passwords are unique."""
        from app.routes.utils import generate_audience_password

        passwords = [generate_audience_password() for _ in range(10)]

        # All should be unique
        assert len(set(passwords)) == 10


class TestStemIndexValidation:
    """Test stem index validation for stem control endpoints."""

    def test_stem_volume_index_out_of_range(self, client):
        """Test volume update with out-of-range index still succeeds (no validation)."""
        state.active_stems = [{"prompt": "drums"}]

        response = client.post("/api/stems/99/volume", json={"volume": 1.0})
        # API doesn't validate index, just sets the volume
        assert response.status_code == 200
        # The volume is set in state
        assert state.stem_volumes.get(99) == 1.0

    def test_stem_mute_index_out_of_range(self, client):
        """Test mute with out-of-range index still succeeds (no validation)."""
        state.active_stems = [{"prompt": "drums"}]

        response = client.post("/api/stems/99/mute")
        # API doesn't validate index, just toggles
        assert response.status_code == 200
        assert 99 in state.muted_stems

    def test_stem_solo_index_out_of_range(self, client):
        """Test solo with out-of-range index still succeeds (no validation)."""
        state.active_stems = [{"prompt": "drums"}]

        response = client.post("/api/stems/99/solo")
        # API doesn't validate index, just toggles
        assert response.status_code == 200
        assert 99 in state.soloed_stems

    def test_stem_download_index_out_of_range(self, client):
        """Test download with out-of-range index."""
        state.active_stems = [{"prompt": "drums"}]

        response = client.get("/api/stems/99/download")
        assert response.status_code == 404


class TestStateEdgeCases:
    """Test state management edge cases."""

    def test_update_state_empty_body(self, client):
        """Test state update with empty body returns current values."""
        response = client.post("/api/state", json={})
        assert response.status_code == 200

    def test_get_state_with_all_fields(self, client):
        """Test get_state returns all required fields."""
        state.llm_reasoning = "Test reasoning"
        state.audience_message = "Hello"
        state.audience_message_ts = 1234567890
        state.loop_count = 5

        response = client.get("/api/state")
        assert response.status_code == 200
        data = response.json()

        assert "llm_reasoning" in data
        assert "audience_message" in data
        assert "loop_count" in data

    def test_update_state_with_partial_bpm_override(self, client):
        """Test partial BPM override doesn't affect other state."""
        state.target_bpm_override = 120
        state.current_bpm = 120

        response = client.post("/api/state", json={"target_bpm_override": 140})
        assert response.status_code == 200

        # Only BPM should change
        assert state.target_bpm_override == 140
        assert state.current_bpm == 140


class TestLLMConfigEdgeCases:
    """Test LLM config edge cases."""

    def test_llm_config_get_returns_all_fields(self, client):
        """Test that LLM config returns all configuration fields."""
        response = client.get("/api/llm-config")
        assert response.status_code == 200
        data = response.json()

        assert "base_url" in data
        assert "api_key" in data
        assert "model" in data
        assert "icecast_enabled" in data
        assert "audience_password" in data

    def test_llm_config_update_with_api_key(self, client):
        """Test LLM config update with api_key."""
        response = client.post("/api/llm-config", json={"api_key": "new-key"})
        assert response.status_code == 200
        assert state.llm_api_key == "new-key"


class TestGenerationConfigEdgeCases:
    """Test generation config edge cases."""

    def test_generation_config_update_negative_steps(self, client):
        """Test that negative steps is handled (may be clamped by API)."""
        response = client.post("/api/generation-config", json={"steps": -10})
        # API may reject or clamp
        assert response.status_code in (200, 422)

    def test_generation_config_get_returns_all_fields(self, client):
        """Test that generation config returns all fields."""
        response = client.get("/api/generation-config")
        assert response.status_code == 200
        data = response.json()

        assert "steps" in data
        assert "cfg_scale" in data
