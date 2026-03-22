import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from api_routes import router
from framework_state import state
import numpy as np

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
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
    response = client.post("/api/state", json={"target_bpm_override": 135, "is_generating": True})
    assert response.status_code == 200
    assert state.target_bpm_override == 135
    assert state.is_generating is True

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
