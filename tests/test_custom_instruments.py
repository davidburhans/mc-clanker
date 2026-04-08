"""Tests for custom instrument API endpoints."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from app.app_ui import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_state():
    from app.framework.framework_state import state
    state.reset()
    state.active_stems = []
    state.stem_volumes = {}
    state.muted_stems = set()
    state.soloed_stems = set()
    yield


class TestCustomInstrumentEndpoints:
    def test_custom_instrument_roundtrip(self, client):
        """Add custom instrument via POST, retrieve via GET."""
        # Add
        response = client.post("/api/instruments/custom", json={
            "name": "My Synth",
            "major_family": "Synth"
        })
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        assert data["name"] == "My Synth"
        assert data["family"] == "Synth"

        # Retrieve
        response = client.get("/api/instruments/custom")
        assert response.status_code == 200
        assert response.json() == {"My Synth": "Synth"}

    def test_custom_instrument_with_new_family(self, client):
        """Adding with a new family dynamically registers it → 200."""
        response = client.post("/api/instruments/custom", json={
            "name": "Bad",
            "major_family": "NotARealFamily"
        })
        assert response.status_code == 200
        
        # Verify the new family is in constants
        constants_resp = client.get("/api/constants")
        assert "NotARealFamily" in constants_resp.json()["valid_major_families"]

    def test_constants_endpoint(self, client):
        """GET /api/constants returns valid BPMs, keys, major families."""
        response = client.get("/api/constants")
        assert response.status_code == 200
        data = response.json()
        assert data["valid_bpms"] == [100, 110, 120, 128, 130, 140, 150]
        assert len(data["valid_major_families"]) > 10

    def test_constants_endpoint_includes_keys(self, client):
        """GET /api/constants includes valid_keys."""
        response = client.get("/api/constants")
        assert response.status_code == 200
        data = response.json()
        assert "C major" in data["valid_keys"]
        assert len(data["valid_keys"]) == 24

    def test_constants_endpoint_includes_dynamic_families(self, client):
        """After adding a custom instrument, /api/constants includes its family."""
        # First add a custom instrument with a family
        response = client.post("/api/instruments/custom", json={
            "name": "Neon Synth",
            "major_family": "Synth"
        })
        assert response.status_code == 200

        # Verify the /constants endpoint now includes Synth
        response = client.get("/api/constants")
        assert response.status_code == 200
        data = response.json()
        assert "Synth" in data["valid_major_families"]

    def test_custom_instrument_with_valid_family_each(self, client):
        """Each VALID_MAJOR_FAMILY should be accepted."""
        from app.lib.constants import VALID_MAJOR_FAMILIES
        for family in VALID_MAJOR_FAMILIES:
            response = client.post("/api/instruments/custom", json={
                "name": f"Test {family}",
                "major_family": family
            })
            assert response.status_code == 200, f"Family {family} should be valid"
