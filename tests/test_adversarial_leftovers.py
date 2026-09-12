"""Regression tests for the leftover adversarial-review findings (SEC-5, ASYNC-4,
DATA-6, SEC-6, SEC-7, CONC-2, CONC-4).

Each test is named after the finding ID it pins, so a future regression points
straight back at the review item.
"""

import asyncio
import json
import os
import time
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

os.environ["DATABASE_URL"] = ""  # Force SQLite for tests

from app.app_ui import app  # noqa: E402  (must import after the env override)
from app.framework.framework_state import state  # noqa: E402


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
    state.current_show_sink = None
    state.is_recording = False
    state.export_sink = None
    state.llm_interaction_buffer = []
    state.action_buffer = []
    # broadcast_audio no-ops once shutdown_event is set; earlier suites leave it
    # set on the global state and the REL-11 tests here drive PCM through
    # broadcast_audio, so clear it (same as test_api's fixture).
    state.shutdown_event.clear()
    state.is_running = True
    yield
    state.dj_password = ""
    state.audience_password = ""
    state.current_show_id = None
    state.is_show_recording = False
    state.current_show_sink = None
    state.is_recording = False
    state.export_sink = None
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
    """CONC-2 (U4-amended, REL-14): start_show used to CLEAR the audit buffers,
    so rows appended in the gap — or re-queued by a failed flush — were silently
    discarded. U4 replaces the clear with flush-first: rows that can persist ARE
    persisted before recording starts (invariant 4: the buffers ARE the
    fine-tuning corpus), and anything a failing flush re-queues is retained
    loudly (never discarded — pinned by test_llm_capture.py::T5b). The
    no-loss-in-the-gap property survives: appends cannot land between the flush
    and the sync_lock enable (append_loop_audit no-ops while current_show_id is
    None)."""

    def test_start_show_flushes_stale_rows_and_keeps_live_appends(
        self, app_client, db_user, tmp_path, monkeypatch
    ):
        from app.db import DatabaseManager
        from app.framework import audit_recording
        from app.models import LLMInteraction

        monkeypatch.setenv("SHOWS_DIR", str(tmp_path))
        prev_show_id = _make_show(db_user.id, status="ended")
        show_id = _make_show(db_user.id, status="draft")
        state.llm_interaction_buffer = [
            {
                "show_id": prev_show_id,
                "loop_index": 1,
                "relative_time_ms": 0,
                "prompt_messages": {"stub": "legacy context-summary dict"},
            }
        ]
        state.action_buffer = [
            {
                "show_id": prev_show_id,
                "loop_index": 1,
                "relative_time_ms": 0,
                "action_type": "retain",
            }
        ]

        with patch_owner(db_user):
            resp = app_client.post(f"/api/shows/{show_id}/start")

        assert resp.status_code == 200, resp.text
        # Stale rows from before the show were PERSISTED (flush-before-start),
        # not silently discarded...
        db = DatabaseManager.get_instance()
        with db.session() as session:
            assert (
                session.query(LLMInteraction).filter(LLMInteraction.show_id == prev_show_id).count() == 1
            )
        assert state.llm_interaction_buffer == []
        assert state.action_buffer == []
        assert state.current_show_id == show_id
        # ...while a row appended while the show is live must survive the start.
        asyncio.run(audit_recording.append_loop_audit({"actions": [], "reasoning": "live"}, [], 0))
        assert len(state.llm_interaction_buffer) == 1
        assert state.llm_interaction_buffer[0]["reasoning"] == "live"


class TestConc4FinalizeUnderSyncLock:
    """CONC-4: _finalize_wav seeked/wrote the recording handle unlocked while
    the mixer thread could interleave a pcm write and corrupt the WAV header.

    REL-11 retires the lock-across-I/O remedy: the WAV finalize now runs on the
    sink's writer thread, which is the SINGLE OWNER of the handle — a stale
    mixer tick can no longer interleave a write because broadcast_audio only
    put_nowait()s into the (detached) sink's queue and submit() drops after
    stop. The corruption-free contract is re-pinned as: finalize happens
    OUTSIDE sync_lock (the audio path stays I/O-free) AND the recording on disk
    is a valid, correctly-sized WAV after stop."""

    def test_stop_show_finalizes_wav_outside_sync_lock_single_owner(
        self, app_client, db_user, tmp_path, monkeypatch
    ):
        import wave

        import app.framework.recording_sink as recording_sink_module
        import app.lib.wav as wav_module

        monkeypatch.setenv("SHOWS_DIR", str(tmp_path))
        show_id = _make_show(db_user.id, status="draft")

        lock_states_during_finalize = []
        real_finalize = wav_module.finalize_wav

        def spy_finalize(handle):
            lock_states_during_finalize.append(state.sync_lock.locked())
            real_finalize(handle)

        monkeypatch.setattr(recording_sink_module, "finalize_wav", spy_finalize)

        with patch_owner(db_user):
            start_resp = app_client.post(f"/api/shows/{show_id}/start")
            assert start_resp.status_code == 200, start_resp.text
            # Drive one block through the mixer-thread path so the recording has
            # real payload, then stop.
            state.broadcast_audio(b"\x00" * 2048)
            sink = state.current_show_sink
            assert sink is not None
            deadline = time.time() + 3.0
            while time.time() < deadline and sink.status().bytes_written < 2048:
                time.sleep(0.01)
            stop_resp = app_client.post(f"/api/shows/{show_id}/stop")

        assert stop_resp.status_code == 200, stop_resp.text
        assert lock_states_during_finalize == [False], (
            "the WAV finalize must run OUTSIDE sync_lock (REL-11 single-owner "
            "writer; the audio path never waits on disk I/O)"
        )
        audio_path = start_resp.json()["audio_file_path"]
        with wave.open(audio_path, "rb") as wav:
            assert wav.getnframes() == 2048 // 4, "the queued block must be drained, not lost"
