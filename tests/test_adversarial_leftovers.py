"""Regression tests for the leftover adversarial-review findings (SEC-5, ASYNC-4,
DATA-6, SEC-6, SEC-7, CONC-2, CONC-4).

Each test is named after the finding ID it pins, so a future regression points
straight back at the review item.
"""

import asyncio
import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

os.environ["DATABASE_URL"] = ""  # Force SQLite for tests

from app.app_ui import app  # noqa: E402  (must import after the env override)
from app.framework.framework_state import state  # noqa: E402
from app.routes import shows as shows_routes  # noqa: E402


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
            username=f"leftover_{suffix}",
            email=f"leftover_{suffix}@example.com",
            password_hash="x",
            is_active=True,
        )
        session.add(user)
        session.flush()
        user_id = user.id
    return SimpleNamespace(id=user_id)


def patch_owner(user):
    """require_show_owner resolves the user via app.routes.utils' own import."""
    return patch("app.routes.utils.get_current_user_from_request", return_value=user)


def _make_show(user_id: int, status: str = "draft") -> int:
    from app.db import DatabaseManager
    from app.models import Show

    db = DatabaseManager.get_instance()
    with db.session() as session:
        show = Show(user_id=user_id, title=f"Leftover {uuid.uuid4().hex[:6]}", status=status)
        session.add(show)
        session.flush()
        return show.id


class TestSec5ShowOwnerJwtBypassesAudienceGate:
    """SEC-5: the per-show audience gate 401'd the show's legitimate JWT owner
    because a Bearer header never yields a Basic password to check."""

    def _make_show_with_audience_password(self, user_id: int) -> int:
        from app.auth import hash_password
        from app.db import DatabaseManager
        from app.models import Show

        db = DatabaseManager.get_instance()
        with db.session() as session:
            show = Show(
                user_id=user_id,
                title=f"Leftover-aud {uuid.uuid4().hex[:6]}",
                status="ended",
                audience_password_hash=hash_password("audpass"),
            )
            session.add(show)
            session.flush()
            return show.id

    def test_owner_jwt_passes_the_show_password_gate(self, app_client, db_user):
        from app.auth import create_access_token

        show_id = self._make_show_with_audience_password(db_user.id)
        headers = {"Authorization": "Bearer " + create_access_token(db_user.id)}

        resp = app_client.get(f"/api/shows/{show_id}/audio", headers=headers)

        # The owner reaches the route, which 404s on the missing audio file.
        # The old middleware gate returned a plain-text 401 before the route.
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Audio file not found"

    def test_non_owner_jwt_still_needs_the_show_password(self, app_client, db_user):
        from app.auth import create_access_token
        from app.db import DatabaseManager
        from app.models import User

        db = DatabaseManager.get_instance()
        suffix = uuid.uuid4().hex[:8]
        with db.session() as session:
            stranger = User(
                username=f"stranger_{suffix}",
                email=f"stranger_{suffix}@example.com",
                password_hash="x",
                is_active=True,
            )
            session.add(stranger)
            session.flush()
            stranger_id = stranger.id
        show_id = self._make_show_with_audience_password(db_user.id)

        headers = {"Authorization": "Bearer " + create_access_token(stranger_id)}
        resp = app_client.get(f"/api/shows/{show_id}/audio", headers=headers)

        # A non-owner without the audience password must still be gated by the
        # middleware (plain-text 401), not waved through to the route.
        assert resp.status_code == 401
        assert resp.text == "Unauthorized"


class TestAsync4PoolClosedOnShutdown:
    """ASYNC-4: close_asyncpg_pool existed but nothing called it — the
    LISTEN/NOTIFY pool leaked on every shutdown."""

    def test_lifespan_shutdown_closes_asyncpg_pool(self, monkeypatch):
        import app.app_ui as app_ui
        import app.garage_client as garage_client
        import app.job_waiter as job_waiter
        import app.onboarding as onboarding

        called = []

        async def fake_close():
            called.append(True)

        async def fake_loop(app_session_id):
            await asyncio.Event().wait()  # cancelled by lifespan shutdown

        async def fake_checks():
            return []

        class FakeGarage:
            async def ensure_bucket_exists(self):
                return None

        monkeypatch.setattr(app_ui, "run_framework_loop_async", fake_loop)
        monkeypatch.setattr(onboarding, "run_onboarding_checks", fake_checks)
        monkeypatch.setattr(garage_client, "create_garage_client_from_env", lambda: FakeGarage())
        monkeypatch.setattr(job_waiter, "close_asyncpg_pool", fake_close)

        async def scenario():
            async with app_ui.lifespan(app_ui.app):
                assert called == []  # not closed while the app is running

        asyncio.run(scenario())

        assert called == [True], "lifespan shutdown must call close_asyncpg_pool"


class TestData6AuditClamp:
    """DATA-6: unclamped LLM free text poisoned the whole audit flush batch."""

    def test_oversized_reasoning_and_description_are_clamped_on_append(self):
        from app.framework import audit_recording

        state.current_show_id = 1  # live show so append_loop_audit buffers
        oversized_reasoning = "x" * 5000
        oversized_sub_family = "y" * 600
        conductor_response = {
            "actions": [{"action_type": "add", "sub_family": oversized_sub_family}],
            "reasoning": oversized_reasoning,
        }

        asyncio.run(audit_recording.append_loop_audit(conductor_response, [], 0))

        assert len(state.llm_interaction_buffer) == 1
        assert len(state.llm_interaction_buffer[0]["reasoning"]) == 1000
        assert len(state.action_buffer) == 1
        assert len(state.action_buffer[0]["action_description"]) == 500

    def test_reasoning_none_is_tolerated(self):
        from app.framework import audit_recording

        state.current_show_id = 1
        asyncio.run(audit_recording.append_loop_audit({"actions": [], "reasoning": None}, [], 0))
        assert state.llm_interaction_buffer[0]["reasoning"] == ""


class TestSec6IcecastLogRedaction:
    """SEC-6: the ffmpeg argv log leaked the base64 Icecast source password."""

    def test_log_safe_argv_redacts_authorization_without_touching_the_command(self):
        from app.framework.framework_icecast import IcecastStreamer

        secret = "Basic c291cmNlOnNlY3JldA=="  # base64("source:secret")
        cmd = ["ffmpeg", "-icy_header", f"Authorization: {secret}", "-icy_header", "ice-name: dj"]

        safe = IcecastStreamer._log_safe_argv(cmd)

        assert safe[2] == "Authorization: Basic ***"
        assert safe[4] == "ice-name: dj"
        assert secret not in " ".join(safe)
        # The real command passed to ffmpeg must be untouched.
        assert cmd[2] == f"Authorization: {secret}"


class TestSec7InstrumentJsonFilter:
    """SEC-7: the instrument containment filter built invalid JSON for values
    containing quotes, 500ing the endpoint on Postgres."""

    def test_instrument_containment_binds_valid_json(self):
        from app.db import DatabaseManager
        from app.models import LLMInteraction
        from app.routes.reasoning_logs import _instrument_containment

        instrument = 'weird"name\\'
        db = DatabaseManager.get_instance()
        with db.session() as session:
            query = session.query(LLMInteraction).filter(LLMInteraction.show_id == 1)
            query = query.filter(_instrument_containment(LLMInteraction.instruments, instrument))
            compiled = query.statement.compile(dialect=postgresql.dialect())

        string_params = [v for v in compiled.params.values() if isinstance(v, str)]
        assert any(json.loads(v) == [instrument] for v in string_params), string_params


class TestConc2BufferResetOrder:
    """CONC-2: start_show reset the audit buffers AFTER enabling recording, so
    rows appended in the gap (or re-queued by a failed flush) were discarded."""

    def test_start_show_clears_stale_rows_and_keeps_live_appends(
        self, app_client, db_user, tmp_path, monkeypatch
    ):
        from app.framework import audit_recording

        monkeypatch.setenv("SHOWS_DIR", str(tmp_path))
        show_id = _make_show(db_user.id, status="draft")
        state.llm_interaction_buffer = [{"stale": "previous-show"}]
        state.action_buffer = [{"stale": "previous-show"}]

        with patch_owner(db_user):
            resp = app_client.post(f"/api/shows/{show_id}/start")

        assert resp.status_code == 200, resp.text
        # Stale rows from before the show are gone...
        assert state.llm_interaction_buffer == []
        assert state.action_buffer == []
        assert state.current_show_id == show_id
        # ...while a row appended while the show is live must survive the reset.
        asyncio.run(audit_recording.append_loop_audit({"actions": [], "reasoning": "live"}, [], 0))
        assert len(state.llm_interaction_buffer) == 1
        assert state.llm_interaction_buffer[0]["reasoning"] == "live"


class TestConc4FinalizeUnderSyncLock:
    """CONC-4: _finalize_wav seeked/wrote the recording handle unlocked while
    the mixer thread could interleave a pcm write and corrupt the WAV header."""

    def test_stop_show_finalizes_wav_under_sync_lock(self, app_client, db_user, tmp_path, monkeypatch):
        monkeypatch.setenv("SHOWS_DIR", str(tmp_path))
        show_id = _make_show(db_user.id, status="draft")

        lock_states_during_finalize = []
        real_finalize = shows_routes._finalize_wav

        def spy_finalize(handle):
            lock_states_during_finalize.append(state.sync_lock.locked())
            real_finalize(handle)

        monkeypatch.setattr(shows_routes, "_finalize_wav", spy_finalize)

        with patch_owner(db_user):
            start_resp = app_client.post(f"/api/shows/{show_id}/start")
            assert start_resp.status_code == 200, start_resp.text
            stop_resp = app_client.post(f"/api/shows/{show_id}/stop")

        assert stop_resp.status_code == 200, stop_resp.text
        assert lock_states_during_finalize == [True], (
            "_finalize_wav must run while holding state.sync_lock"
        )
