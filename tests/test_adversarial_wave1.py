"""Regression tests for the wave-1 adversarial-review findings (DATA-1, DATA-2/CONC-1,
SEC-1, SEC-2, SEC-3, ASYNC-1, AUDIO-1).

Each test is named after the finding ID it pins, so a future regression points
straight back at the review item.
"""

import asyncio
import base64
import io
import os
import threading
import uuid
import wave
from datetime import datetime, timezone
from types import SimpleNamespace

import boto3
import botocore.config
import numpy as np
import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = ""  # Force SQLite for tests

from app.app_ui import app  # noqa: E402  (must import after the env override)
from app.framework.framework_state import state  # noqa: E402
from app.onboarding import check_garage_s3  # noqa: E402
from app.routes.stems import download_stem  # noqa: E402


@pytest.fixture
def app_client():
    """Returns a TestClient with the real app."""
    return TestClient(app)


@pytest.fixture(autouse=True)
def init_db():
    """Initialize DB tables."""
    from app.db import DatabaseManager

    db = DatabaseManager.get_instance()
    db.create_tables()


@pytest.fixture(autouse=True)
def reset_state():
    """Reset global state between tests (incl. fields state.reset() keeps)."""
    state.reset()
    state.dj_password = ""
    state.audience_password = ""
    state.current_show_id = None
    state.is_show_recording = False
    state.current_show_audio_file = None
    state.is_recording = False
    state.recording_file_handle = None
    state.llm_interaction_buffer = []
    state.action_buffer = []
    yield
    # Teardown too: none of these fields may leak into later test modules
    # (state.dj_password left set would 401 every gated request suite-wide).
    state.dj_password = ""
    state.audience_password = ""
    state.current_show_id = None
    state.is_show_recording = False
    state.current_show_audio_file = None
    state.is_recording = False
    state.recording_file_handle = None
    state.llm_interaction_buffer = []
    state.action_buffer = []


@pytest.fixture
def db_user():
    """A real User row; returns a lightweight id holder (row may expire)."""
    from app.db import DatabaseManager
    from app.models import User

    db = DatabaseManager.get_instance()
    suffix = uuid.uuid4().hex[:8]
    with db.session() as session:
        user = User(
            username=f"wave1_{suffix}",
            email=f"wave1_{suffix}@example.com",
            password_hash="x",
            is_active=True,
        )
        session.add(user)
        session.flush()
        user_id = user.id
    return SimpleNamespace(id=user_id)


def _make_show(user_id: int, status: str = "draft") -> int:
    from app.db import DatabaseManager
    from app.models import Show

    db = DatabaseManager.get_instance()
    with db.session() as session:
        show = Show(user_id=user_id, title=f"Wave1 {uuid.uuid4().hex[:6]}", status=status)
        session.add(show)
        session.flush()
        return show.id


def _get_show(show_id: int):
    from app.db import DatabaseManager
    from app.models import Show

    db = DatabaseManager.get_instance()
    with db.session() as session:
        show = session.query(Show).filter(Show.id == show_id).first()
        return show.status, show.duration_seconds


class TestData1StopShowDuration:
    """DATA-1: stop_show crashed with aware-minus-naive TypeError on every stop."""

    def test_stop_show_records_duration_and_ends(self, app_client, db_user, tmp_path, monkeypatch):
        """start→stop round-trip through the DB must end the show and record duration."""
        monkeypatch.setenv("SHOWS_DIR", str(tmp_path))
        show_id = _make_show(db_user.id, status="draft")

        with patch_owner(db_user):
            start_resp = app_client.post(f"/api/shows/{show_id}/start")
            assert start_resp.status_code == 200
            stop_resp = app_client.post(f"/api/shows/{show_id}/stop")

        assert stop_resp.status_code == 200, stop_resp.text
        status, duration = _get_show(show_id)
        assert status == "ended"
        assert isinstance(duration, int) and duration >= 0


def patch_owner(user):
    """require_show_owner resolves the user via app.routes.utils' own import."""
    from unittest.mock import patch

    return patch("app.routes.utils.get_current_user_from_request", return_value=user)


class TestData2AuditFlush:
    """DATA-2/CONC-1: stop_show used to discard the whole audit trail unflushed."""

    def test_stop_show_persists_audit_buffers(self, app_client, db_user):
        show_id = _make_show(db_user.id, status="live")
        # Simulate a live show recording + one buffered loop of audit rows.
        state.current_show_id = show_id
        state.is_show_recording = True
        naive_now = datetime.now(timezone.utc).replace(tzinfo=None)
        state.llm_interaction_buffer = [
            {
                "show_id": show_id,
                "loop_index": 0,
                "timestamp": naive_now,
                "relative_time_ms": 0,
                "prompt_messages": {"loop_index": 0},
                "parsed_response": {},
                "reasoning": "keep the groove",
                "error": None,
                "was_fallback": False,
            }
        ]
        state.action_buffer = [
            {
                "show_id": show_id,
                "loop_index": 0,
                "timestamp": naive_now,
                "relative_time_ms": 0,
                "action_type": "add",
                "stem_index": None,
                "stem_details": {},
                "action_description": "Added pad",
            }
        ]

        with patch_owner(db_user):
            resp = app_client.post(f"/api/shows/{show_id}/stop")

        assert resp.status_code == 200, resp.text

        from app.db import DatabaseManager
        from app.models import LLMInteraction, ShowAction

        db = DatabaseManager.get_instance()
        with db.session() as session:
            assert session.query(LLMInteraction).filter_by(show_id=show_id).count() == 1
            assert session.query(ShowAction).filter_by(show_id=show_id).count() == 1


class TestSec1ExportFormat:
    """SEC-1: export `format` was interpolated into a filename unvalidated."""

    def test_export_start_rejects_path_traversal_format(self, app_client, tmp_path, monkeypatch):
        monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
        resp = app_client.post("/api/export/start", json={"format": "../../data/mc_clanker.db"})
        assert resp.status_code == 422
        # Nothing may have been created outside the export dir.
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("fmt", ["wav", "mp3"])
    def test_export_start_accepts_allowlisted_formats(self, app_client, tmp_path, monkeypatch, fmt):
        monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
        resp = app_client.post("/api/export/start", json={"format": fmt})
        assert resp.status_code == 200
        assert resp.json()["file_path"].endswith(f".{fmt}")
        stop_resp = app_client.post("/api/export/stop")
        assert stop_resp.status_code == 200


class TestAsync1GarageProbe:
    """ASYNC-1: the Garage health probe blocked the event loop with botocore."""

    def test_check_garage_s3_offloads_blocking_probe(self, monkeypatch):
        monkeypatch.setenv("GARAGE_ENDPOINT", "http://localhost:3900")
        monkeypatch.setenv("GARAGE_ACCESS_KEY", "k")
        monkeypatch.setenv("GARAGE_SECRET_KEY", "s")

        captured: dict = {}
        loop_thread = threading.get_ident()

        class FakeS3Client:
            def list_buckets(self):
                captured["probe_thread"] = threading.get_ident()

        real_config = botocore.config.Config

        def fake_config(*args, **kwargs):
            captured["config_kwargs"] = kwargs
            return real_config(*args, **kwargs)

        monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeS3Client())
        monkeypatch.setattr(botocore.config, "Config", fake_config)

        result = asyncio.run(check_garage_s3())

        assert result.passed is True
        # The blocking botocore call must run off the event-loop thread.
        assert captured["probe_thread"] != loop_thread
        # A health probe must not multiply its timeout window via botocore retries.
        assert captured["config_kwargs"]["retries"] == {"max_attempts": 1}


class TestAudio1StemDownload:
    """AUDIO-1: stem downloads served float32 bits under a 16-bit PCM header."""

    def test_download_stem_converts_float32_to_int16(self):
        # 1.5 exceeds the float range and must clip, not alias into int16 bits.
        audio = np.full((100, 2), 1.5, dtype=np.float32)
        state.active_stems = [{"prompt": "wave1-test-prompt"}]
        state.cache_stem("wave1-test-prompt", audio)

        response = asyncio.run(download_stem(0))

        buf = io.BytesIO(response.body)
        with wave.open(buf, "rb") as wf:
            assert wf.getsampwidth() == 2
            assert wf.getnchannels() == 2
            assert wf.getframerate() == 44100
            frames = wf.readframes(wf.getnframes())
        samples = np.frombuffer(frames, dtype="<i2")
        assert len(samples) == 200
        assert int(np.abs(samples).max()) <= 32767


class TestSec2SetupConfig:
    """SEC-2: /api/setup/config wrote arbitrary env keys to the host .env."""

    def test_setup_config_rejects_non_allowlisted_keys(self, app_client, monkeypatch):
        written: dict = {}
        monkeypatch.setattr("app.onboarding.write_env_file", lambda values: written.update(values))
        monkeypatch.setattr("app.onboarding.restart_services", lambda: None)

        resp = app_client.post("/api/setup/config", json={"LLM_MODEL": "qwen", "ATTACKER_KEY": "x"})

        assert resp.status_code == 400
        assert written == {}

    def test_setup_config_accepts_allowlisted_keys(self, app_client, monkeypatch):
        written: dict = {}
        monkeypatch.setattr("app.onboarding.write_env_file", lambda values: written.update(values))
        monkeypatch.setattr("app.onboarding.restart_services", lambda: None)

        resp = app_client.post("/api/setup/config", json={"LLM_MODEL": "qwen", "JWT_SECRET": "s3cret"})

        assert resp.status_code == 200
        assert written == {"LLM_MODEL": "qwen", "JWT_SECRET": "s3cret"}


class TestSec3MiddlewareMethodGate:
    """SEC-3: only POST was DJ-gated and Basic passwords were never verified,
    so DELETE bypassed auth and any password string was accepted."""

    def _basic(self, user: str, password: str) -> str:
        return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def test_delete_is_rejected_without_credentials(self, app_client):
        state.dj_password = "pw"
        resp = app_client.delete("/api/message/audience")
        assert resp.status_code == 401

    def test_delete_with_wrong_password_is_rejected(self, app_client):
        state.dj_password = "pw"
        resp = app_client.delete("/api/message/audience", headers={"Authorization": self._basic("dj", "wrong")})
        assert resp.status_code == 401

    def test_delete_with_dj_password_proceeds(self, app_client):
        state.dj_password = "pw"
        resp = app_client.delete("/api/message/audience", headers={"Authorization": self._basic("dj", "pw")})
        assert resp.status_code == 200

    def test_gate_passing_request_gets_state_user_attached(self, app_client):
        """SEC-3 follow-up: the compat block used to sit INSIDE the rejection
        `if` after its `return`, so gate-passing Basic-auth requests never got
        request.state.user attached (unreachable dead code)."""
        from fastapi import Request as FastAPIRequest

        from app.app_ui import app as fastapi_app

        @fastapi_app.api_route("/api/_compat_user_probe", methods=["GET", "DELETE"])
        async def _compat_user_probe(request: FastAPIRequest):
            user = getattr(request.state, "user", None)
            return {"username": getattr(user, "username", None)}

        state.dj_password = "pw"
        resp = app_client.delete("/api/_compat_user_probe", headers={"Authorization": self._basic("dj", "pw")})
        assert resp.status_code == 200, resp.text
        assert resp.json()["username"] == "djCompat", (
            "a request that passes the Basic-auth gate must get request.state.user attached"
        )
