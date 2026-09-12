"""Round-3 lane D — HTTP API / auth / recording-lifecycle regressions.

Pins the fixes from ``adversarial_review/round3/05_http_api.md`` (+ the WAV
size-field defect of ``04_audio_dsp.md`` FINDING 3 and the stream/lifespan leaks
of ``01_async_lifecycle.md``):

* D1  deleting/archiving the LIVE show never tore the recording down → permanent wedge.
* D2  restarting a show truncated the previous recording (O_TRUNC on a fixed path).
* D3  ``POST /export/start`` opened (truncating) the file before the conflict check.
* D4  ``stop_show`` cleared ``is_show_started`` while another show still recorded.
* D5  ``POST``/``DELETE /api/jobs`` had no auth gate while the GETs did.
* D6  uuid.UUID vs VARCHAR(36) on SQLite: POST 500'd, id routes 404'd.
* D7  anonymous heartbeat poisoned ``session_routing`` → 307 open redirect.
* D8  ``StemVolumeUpdate.volume`` accepted NaN/±Inf/unbounded magnitudes.
* D9  DJ gate passed everything through when only AUDIENCE_PASSWORD was set;
      ``GET /api/llm-config`` leaked ``llm_api_key`` to the audience realm.
* D10 failed ``/stream.mp3`` setup leaked the ffmpeg child + the client queue slot.
* D11 a framework loop that failed to start/died left health saying is_running=true.
* D12 ``POST /api/setup/config`` wrote newline-bearing values straight into .env.
* D13 >4 GiB recordings finalized with RIFF/data sizes left at 0 → unreadable.
"""

import asyncio
import base64
import contextlib
import io
import math
import os
import queue
import struct
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

os.environ["DATABASE_URL"] = ""  # Force the SQLite dev fallback (D6 reproduces there)

from app import app_ui  # noqa: E402  (import after the env override)
from app.app_ui import app  # noqa: E402
from app.framework.framework_state import state  # noqa: E402
from app.routes import shows as shows_routes  # noqa: E402
from app.routes.schemas import JobSubmission, StemVolumeUpdate  # noqa: E402


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
    """Reset global state (incl. the recording fields state.reset() keeps)."""
    state.reset()
    state.shutdown_event.clear()
    state.is_running = True
    state.dj_password = ""
    state.audience_password = ""
    state.current_show_id = None
    state.is_show_recording = False
    state.current_show_audio_file = None
    state.is_recording = False
    state.recording_file_handle = None
    state.recording_file_path = None
    state.llm_interaction_buffer = []
    state.action_buffer = []
    state.llm_api_key = "sk-conductor-secret"
    yield
    state.shutdown_event.clear()
    state.is_running = True
    state.dj_password = ""
    state.audience_password = ""
    state.current_show_id = None
    state.is_show_recording = False
    state.current_show_audio_file = None
    state.is_recording = False
    state.recording_file_handle = None
    state.recording_file_path = None
    state.llm_interaction_buffer = []
    state.action_buffer = []


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Keep recordings/exports inside the tmp dir (never /exports)."""
    monkeypatch.setenv("SHOWS_DIR", str(tmp_path / "shows"))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))


@pytest.fixture
def db_user():
    """A real User row; returns a lightweight id holder (row may expire)."""
    from app.db import DatabaseManager
    from app.models import User

    db = DatabaseManager.get_instance()
    suffix = uuid.uuid4().hex[:8]
    with db.session() as session:
        user = User(
            username=f"rnd3d_{suffix}",
            email=f"rnd3d_{suffix}@example.com",
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


def patch_job_auth(user):
    """The jobs routes resolve the caller via app.routes.jobs' own import."""
    return patch("app.routes.jobs.get_current_user_from_request", return_value=user)


def _make_show(user_id: int, status: str = "draft") -> int:
    from app.db import DatabaseManager
    from app.models import Show

    db = DatabaseManager.get_instance()
    with db.session() as session:
        show = Show(user_id=user_id, title=f"Round3D {uuid.uuid4().hex[:6]}", status=status)
        session.add(show)
        session.flush()
        return show.id


def _start_show(app_client, user, show_id: int):
    """Start a show and return the response (``audio_file_path`` is per-run, D2)."""
    with patch_owner(user):
        response = app_client.post(f"/api/shows/{show_id}/start")
    assert response.status_code == 200, response.text
    return response


def _basic(password: str) -> dict:
    token = base64.b64encode(f"caller:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# --------------------------------------------------------------------------- #
# D1 — delete/archive of the LIVE show must tear the recording down
# --------------------------------------------------------------------------- #


class TestD1LiveShowTeardown:
    def test_delete_live_show_releases_recording_so_next_show_starts(self, app_client, db_user):
        doomed = _make_show(db_user.id)
        _start_show(app_client, db_user, doomed)
        assert state.current_show_id == doomed

        with patch_owner(db_user):
            deleted = app_client.delete(f"/api/shows/{doomed}")

        assert deleted.status_code == 204, deleted.text
        assert state.current_show_id is None
        assert state.is_show_recording is False
        assert state.current_show_audio_file is None
        assert state.is_show_started is False

        # The wedge: before the fix EVERY later start 409'd until a restart.
        follower = _make_show(db_user.id)
        _start_show(app_client, db_user, follower)
        assert state.current_show_id == follower

    def test_archive_live_show_releases_recording(self, app_client, db_user):
        doomed = _make_show(db_user.id)
        _start_show(app_client, db_user, doomed)

        with patch_owner(db_user):
            archived = app_client.post(f"/api/shows/{doomed}/archive")

        assert archived.status_code == 200, archived.text
        assert archived.json()["status"] == "archived"
        assert state.current_show_id is None
        assert state.is_show_recording is False
        assert state.is_show_started is False

        follower = _make_show(db_user.id)
        _start_show(app_client, db_user, follower)
        assert state.current_show_id == follower

    def test_delete_non_live_show_leaves_other_shows_recording_alone(self, app_client, db_user):
        recording = _make_show(db_user.id)
        idle = _make_show(db_user.id)
        _start_show(app_client, db_user, recording)

        with patch_owner(db_user):
            deleted = app_client.delete(f"/api/shows/{idle}")

        assert deleted.status_code == 204
        assert state.current_show_id == recording
        assert state.is_show_recording is True


# --------------------------------------------------------------------------- #
# D2 — never truncate an existing recording on (re)start
# --------------------------------------------------------------------------- #


class TestD2NoTruncateOnRestart:
    def test_restart_keeps_the_previous_take_and_records_to_a_fresh_path(self, app_client, db_user):
        show_id = _make_show(db_user.id)
        audio_path = _start_show(app_client, db_user, show_id).json()["audio_file_path"]
        with state.sync_lock:
            state.current_show_audio_file.write(b"\0" * 4096)
        with patch_owner(db_user):
            stopped = app_client.post(f"/api/shows/{show_id}/stop")
        assert stopped.status_code == 200, stopped.text
        recorded_bytes = os.path.getsize(audio_path)
        assert recorded_bytes > 4096

        with patch_owner(db_user):
            restarted = app_client.post(f"/api/shows/{show_id}/start")

        assert restarted.status_code == 200, restarted.text
        assert os.path.getsize(audio_path) == recorded_bytes, "prior recording was truncated"
        new_path = restarted.json()["audio_file_path"]
        assert new_path != audio_path
        with state.sync_lock:
            assert state.current_show_audio_file.name == new_path

    def test_allocator_skips_paths_that_already_hold_audio(self, tmp_path):
        from datetime import datetime, timezone

        taken = tmp_path / "audio.wav"
        taken.write_bytes(b"\0" * 10)
        second_take = shows_routes._allocate_show_audio_path(str(tmp_path), datetime.now(timezone.utc))
        assert second_take != str(taken)
        assert not os.path.exists(second_take)

        fresh = tmp_path / "fresh"
        fresh.mkdir()
        expected = str(fresh / "audio.wav")
        assert shows_routes._allocate_show_audio_path(str(fresh), datetime.now(timezone.utc)) == expected

    def test_draft_show_without_a_file_still_starts(self, app_client, db_user):
        show_id = _make_show(db_user.id)
        _start_show(app_client, db_user, show_id)
        assert state.current_show_id == show_id


# --------------------------------------------------------------------------- #
# D3 — export conflict check must run before the truncating open
# --------------------------------------------------------------------------- #


class TestD3ExportConflictBeforeOpen:
    def test_rejected_duplicate_export_start_does_not_truncate(self, app_client):
        first = app_client.post("/api/export/start", json={"format": "wav"})
        assert first.status_code == 200, first.text
        file_path = first.json()["file_path"]
        with state.sync_lock:
            state.recording_file_handle.write(b"\0" * 8000)
        recorded_bytes = os.path.getsize(file_path)
        assert recorded_bytes > 44

        duplicate = app_client.post("/api/export/start", json={"format": "wav"})

        assert duplicate.status_code == 400, duplicate.text
        assert os.path.getsize(file_path) == recorded_bytes, "active export was truncated by a rejected start"

    def test_rejected_export_start_never_reaches_open(self, app_client, monkeypatch):
        assert app_client.post("/api/export/start", json={"format": "wav"}).status_code == 200

        def trip_up(path, *args, **kwargs):
            raise AssertionError(f"open() must not be reached for a conflicting start: {path}")

        monkeypatch.setattr(shows_routes, "open", trip_up, raising=False)
        duplicate = app_client.post("/api/export/start", json={"format": "wav"})

        assert duplicate.status_code == 400


# --------------------------------------------------------------------------- #
# D4 — stop_show must not clear is_show_started for a foreign recording
# --------------------------------------------------------------------------- #


class TestD4StopKeepsFlagForForeignRecording:
    def test_stopping_a_stale_live_row_keeps_is_show_started(self, app_client, db_user):
        from app.db import DatabaseManager
        from app.models import Show

        recording = _make_show(db_user.id)
        stale = _make_show(db_user.id)
        _start_show(app_client, db_user, recording)
        db = DatabaseManager.get_instance()
        with db.session() as session:
            session.query(Show).filter(Show.id == stale).update({"status": "live"})

        with patch_owner(db_user):
            stopped = app_client.post(f"/api/shows/{stale}/stop")

        assert stopped.status_code == 200, stopped.text
        assert state.current_show_id == recording
        assert state.is_show_started is True, "flag cleared while another show was still recording"

    def test_stopping_the_owning_show_clears_is_show_started(self, app_client, db_user):
        show_id = _make_show(db_user.id)
        _start_show(app_client, db_user, show_id)

        with patch_owner(db_user):
            stopped = app_client.post(f"/api/shows/{show_id}/stop")

        assert stopped.status_code == 200, stopped.text
        assert state.is_show_started is False
        assert state.current_show_id is None


# --------------------------------------------------------------------------- #
# D5 — mutating jobs routes need the SEC-4 gate the GETs already have
# --------------------------------------------------------------------------- #


class TestD5JobsMutationAuth:
    def test_anonymous_submit_and_cancel_are_rejected(self, app_client):
        body = {"session_id": str(uuid.uuid4()), "instrument": "Bass", "prompt": "sub bass"}
        assert app_client.post("/api/jobs", json=body).status_code == 401
        assert app_client.delete(f"/api/jobs/{uuid.uuid4()}").status_code == 401

    def test_authenticated_submit_and_cancel_still_work(self, app_client):
        user = SimpleNamespace(id=7, username="dj", is_active=True)
        body = {"session_id": str(uuid.uuid4()), "instrument": "Bass", "prompt": "sub bass"}
        with patch_job_auth(user):
            created = app_client.post("/api/jobs", json=body)
        assert created.status_code == 201, created.text
        job_id = created.json()["job_id"]

        with patch_job_auth(user):
            fetched = app_client.get(f"/api/jobs/{job_id}", headers={})
        assert fetched.status_code == 200, fetched.text

        with patch_job_auth(user):
            cancelled = app_client.delete(f"/api/jobs/{job_id}")
        assert cancelled.status_code == 200, cancelled.text


# --------------------------------------------------------------------------- #
# D6 — uuid/str boundary on the SQLite dev fallback
# --------------------------------------------------------------------------- #


class TestD6UuidStrBoundary:
    def test_session_id_is_canonicalized_to_str(self):
        raw = "550e8400-e29b-41d4-a716-446655440000"
        assert JobSubmission(session_id=raw, instrument="Bass", prompt="p").session_id == raw
        assert isinstance(JobSubmission(session_id=uuid.UUID(raw), instrument="Bass", prompt="p").session_id, str)

    def test_non_uuid_session_id_still_rejected(self):
        with pytest.raises(ValidationError):
            JobSubmission(session_id="not-a-uuid", instrument="Bass", prompt="p")

    def test_submit_then_read_and_cancel_by_id_on_sqlite(self, app_client):
        """Before the fix: POST → 500 (UUID binding), GET/DELETE → false 404."""
        user = SimpleNamespace(id=8, username="dj", is_active=True)
        body = {"session_id": str(uuid.uuid4()), "instrument": "Kick", "prompt": "punchy kick"}
        with patch_job_auth(user):
            created = app_client.post("/api/jobs", json=body)
            assert created.status_code == 201, created.text
            job_id = created.json()["job_id"]
            fetched = app_client.get(f"/api/jobs/{job_id}")
            assert fetched.status_code == 200, fetched.text
            assert fetched.json()["id"] == job_id
            cancelled = app_client.delete(f"/api/jobs/{job_id}")
            assert cancelled.status_code == 200, cancelled.text
            listing = app_client.get(f"/api/jobs?session_id={body['session_id']}")
        assert listing.status_code == 200, listing.text
        assert listing.json()["total"] == 1


# --------------------------------------------------------------------------- #
# D7 — session routing poisoning / open redirect
# --------------------------------------------------------------------------- #


class TestD7SessionRoutingPoisoning:
    def test_heartbeat_with_foreign_server_id_is_rejected(self, app_client):
        session_id = str(uuid.uuid4())

        response = app_client.post(f"/api/sessions/{session_id}/heartbeat", json={"server_id": "evil.example"})

        assert response.status_code == 422, response.text
        assert app_client.get(f"/api/sessions/{session_id}/server").status_code == 404

    def test_heartbeat_with_this_servers_id_is_accepted(self, app_client):
        session_id = str(uuid.uuid4())

        response = app_client.post(
            f"/api/sessions/{session_id}/heartbeat", json={"server_id": app_ui.current_server_id}
        )

        assert response.status_code == 200, response.text

        from sqlalchemy import text

        from app.db import DatabaseManager

        with DatabaseManager.get_instance().session() as session:
            row = session.execute(
                text("SELECT server_id FROM session_routing WHERE session_id = :sid"),
                {"sid": session_id},
            ).fetchone()
        assert row is not None and row[0] == app_ui.current_server_id

    def test_poisoned_row_never_becomes_a_redirect(self, app_client):
        """A pre-existing poisoned routing row must not 307 the victim elsewhere."""
        from app.db import DatabaseManager
        from app.models import SessionRouting

        session_id = str(uuid.uuid4())
        db = DatabaseManager.get_instance()
        with db.session() as session:
            session.add(SessionRouting(session_id=session_id, server_id="evil.example/attacker/"))

        response = app_client.get(f"/api/sessions/{session_id}/server", follow_redirects=False)

        assert response.status_code != 307, "redirected to a client-controlled authority"
        assert "evil.example" not in response.headers.get("location", "")


# --------------------------------------------------------------------------- #
# D8 — stem volume bounds
# --------------------------------------------------------------------------- #


class TestD8StemVolumeBounds:
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -1.0, 2.5, 1e308])
    def test_out_of_range_gains_rejected_by_schema(self, bad):
        with pytest.raises(ValidationError):
            StemVolumeUpdate(volume=bad)

    @pytest.mark.parametrize("good", [0.0, 1.0, 2.0])
    def test_documented_range_accepted(self, good):
        assert StemVolumeUpdate(volume=good).volume == good

    @pytest.mark.parametrize("payload", ['{"volume": 3.0}', '{"volume": -0.5}', '{"volume": null}'])
    def test_poisoning_request_never_reaches_state(self, app_client, payload):
        state.stem_volumes = {0: 1.0}

        response = app_client.post("/api/stems/0/volume", content=payload, headers={"Content-Type": "application/json"})

        assert response.status_code == 422, response.text
        assert state.stem_volumes == {0: 1.0}

    def test_nan_gain_never_reaches_state(self):
        """NaN is refused outright (D8).

        FastAPI echoes the rejected body inside its 422 payload and a NaN float
        cannot be JSON-serialized there, so the client observes an error status of
        some shape; what matters — and what the fix pins — is that the mixer gain is
        never poisoned.
        """
        strict_client = TestClient(app, raise_server_exceptions=False)
        state.stem_volumes = {0: 1.0}

        response = strict_client.post(
            "/api/stems/0/volume", content='{"volume": NaN}', headers={"Content-Type": "application/json"}
        )

        assert response.status_code >= 400
        assert state.stem_volumes == {0: 1.0}
        assert not math.isnan(state.stem_volumes[0])


# --------------------------------------------------------------------------- #
# D9 — DJ gate fails closed + llm_api_key masking
# --------------------------------------------------------------------------- #


class TestD9DjGateFailsClosed:
    def test_audience_only_deployment_rejects_anonymous_dj_writes(self, app_client):
        state.audience_password = "audpass"
        state.dj_password = ""

        get_response = app_client.get("/api/llm-config")
        post_response = app_client.post("/api/llm-config", json={"model": "evil-model"})

        assert get_response.status_code == 401
        assert post_response.status_code == 401, "DJ write route reachable anonymously"
        assert state.llm_model != "evil-model"

    def test_audience_only_deployment_rejects_the_audience_password_on_dj_writes(self, app_client):
        state.audience_password = "audpass"
        state.dj_password = ""

        response = app_client.post("/api/llm-config", json={"model": "evil-model"}, headers=_basic("audpass"))

        assert response.status_code == 401
        assert state.llm_model != "evil-model"

    def test_audience_gated_reader_gets_a_masked_api_key(self, app_client):
        state.audience_password = "audpass"
        state.llm_api_key = "sk-super-secret"

        response = app_client.get("/api/llm-config", headers=_basic("audpass"))

        assert response.status_code == 200, response.text
        assert response.json()["api_key"] != "sk-super-secret"

    def test_dj_realm_still_sees_the_api_key(self, app_client):
        # DJ-only deployment: the caller IS the DJ, so the live key stays readable.
        state.dj_password = "dypass"
        state.audience_password = ""
        state.llm_api_key = "sk-super-secret"

        response = app_client.get("/api/llm-config", headers=_basic("dypass"))

        assert response.status_code == 200, response.text
        assert response.json()["api_key"] == "sk-super-secret"

    def test_local_dev_no_passwords_unchanged(self, app_client):
        state.dj_password = ""
        state.audience_password = ""

        post_response = app_client.post("/api/state", json={"user_override": "vibe"})
        get_response = app_client.get("/api/llm-config")

        assert post_response.status_code == 200
        assert state.user_override == "vibe"
        assert get_response.status_code == 200
        assert get_response.json()["api_key"] == "sk-conductor-secret"


# --------------------------------------------------------------------------- #
# D10 — /stream.mp3 setup failure must not leak ffmpeg or the client queue
# --------------------------------------------------------------------------- #


class _FakePipe:
    def __init__(self, fail_write: bool = False) -> None:
        self.fail_write = fail_write
        self.closed = False

    def write(self, chunk):
        if self.fail_write:
            raise OSError("broken pipe during pre-feed")

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeFFmpegProcess:
    def __init__(self, fail_write: bool = False) -> None:
        self.stdin = _FakePipe(fail_write=fail_write)
        self.stdout = _FakePipe()
        self.killed = False
        self.waited = False

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None) -> None:
        self.waited = True


class _ReplayQueue:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def get(self, timeout=None):
        if self._chunks:
            return self._chunks.pop(0)
        raise queue.Empty

    def put_nowait(self, item) -> None:
        pass


@pytest.fixture
def stream_harness(monkeypatch):
    """Named fakes for the fan-out /stream.mp3 plumbing (REL-10 port of D10).

    The D10 contract survives the architecture change: a stream-setup failure
    must never leak a subprocess nor leave a client registered. Under the
    fan-out, "setup" is the singleton transcoder spawn inside
    acquire_stream_client.
    """

    import app.stream_fanout as sf

    class StreamHarness:
        def __init__(self) -> None:
            self.spawn_calls = 0
            self.retired = []

    harness = StreamHarness()
    monkeypatch.setattr(state, "stream_fanout", None, raising=False)
    monkeypatch.setattr(state, "audio_clients", [])
    monkeypatch.setattr(sf, "resolve_ffmpeg_exe", lambda: "ffmpeg")
    probe = SimpleNamespace(stdout="libmp3lame", returncode=0)
    monkeypatch.setattr(sf.subprocess, "run", lambda *a, **k: probe)
    monkeypatch.setattr(state, "add_audio_client", lambda q: None)
    monkeypatch.setattr(state, "remove_audio_client", lambda q: None)
    return harness


class TestD10StreamSetupFailure:
    def test_popen_failure_retires_fanout_and_leaks_no_client(self, stream_harness):
        """D10 (REL-10 port): spawn failure → empty stream, no client, no zombie."""
        import app.stream_fanout as sf
        from app.stream_fanout import mp3_client_stream

        def _boom(*_args, **_kwargs):
            raise OSError("no ffmpeg")

        with patch.object(sf.subprocess, "Popen", side_effect=OSError("no ffmpeg")):
            chunks = list(mp3_client_stream(state))

        assert chunks == [], "spawn failure must serve an empty stream, never hang"
        fanout = getattr(state, "stream_fanout", None)
        alive_clients = getattr(fanout, "_clients", None) if fanout else []
        assert not alive_clients, "a client session survived a failed spawn"
        assert state.audio_clients == []


# --------------------------------------------------------------------------- #
# D11 — framework start/loop failure must be visible to /api/health
# --------------------------------------------------------------------------- #


class TestD11FrameworkFailureReflectedInHealth:
    def test_crashed_framework_task_clears_is_running(self):
        state.is_running = True

        async def _crash():
            raise RuntimeError("framework loop died")

        async def _run():
            task = asyncio.create_task(_crash())
            with contextlib.suppress(RuntimeError):
                await task
            return task

        crashed = asyncio.run(_run())
        assert state.is_running is True

        app_ui._on_framework_task_done(crashed)

        assert state.is_running is False
        assert state.shutdown_event.is_set()

    def test_cancelled_framework_task_is_not_treated_as_failure(self):
        state.is_running = True

        async def _never_ends():
            await asyncio.sleep(30)

        async def _run():
            task = asyncio.create_task(_never_ends())
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return task

        cancelled = asyncio.run(_run())

        app_ui._on_framework_task_done(cancelled)

        assert state.is_running is True

    def test_start_failure_cleanup_path_runs(self):
        state.is_running = True

        app_ui._run_framework_failure_cleanup()

        assert state.is_running is False


# --------------------------------------------------------------------------- #
# D12 — .env injection through POST /api/setup/config
# --------------------------------------------------------------------------- #


class TestD12EnvInjection:
    @pytest.mark.parametrize(
        "value",
        ["gpt\nJWT_SECRET=pwned", "gpt\r\nDJ_PASSWORD=", "gpt\x00secret"],
    )
    def test_newline_or_nul_value_is_rejected_before_writing(self, app_client, value):
        writer = MagicMock()
        with patch("app.onboarding.write_env_file", writer), patch("app.onboarding.restart_services"):
            response = app_client.post("/api/setup/config", json={"LLM_MODEL": value})

        assert response.status_code == 422, response.text
        writer.assert_not_called()

    def test_clean_values_are_written(self, app_client):
        writer = MagicMock()
        with patch("app.onboarding.write_env_file", writer), patch("app.onboarding.restart_services"):
            response = app_client.post("/api/setup/config", json={"LLM_MODEL": "gpt-4o"})

        assert response.status_code == 200, response.text
        writer.assert_called_once_with({"LLM_MODEL": "gpt-4o"})


# --------------------------------------------------------------------------- #
# D13 — >4 GiB WAV finalize must not leave zero RIFF/data sizes
# --------------------------------------------------------------------------- #


class TestD13WavOverFourGiB:
    def test_oversized_recording_writes_the_riff_sentinel(self, tmp_path, monkeypatch):
        # REL-05/U5: the WAV finalize logic (and its max-data-size constant) moved
        # to app.lib.wav — patch the constant at its new home to force the branch.
        import app.lib.wav as wav_module

        monkeypatch.setattr(wav_module, "WAV_MAX_DATA_SIZE", 0)  # force the >4 GiB branch
        path = tmp_path / "huge.wav"
        handle = open(path, "wb")
        shows_routes._write_wav_header(handle)
        handle.write(b"\0" * 1024)

        shows_routes._finalize_wav(handle)

        raw = path.read_bytes()
        assert struct.unpack("<I", raw[4:8])[0] == 0xFFFFFFFF, "RIFF size left at 0"
        assert struct.unpack("<I", raw[40:44])[0] == 0xFFFFFFFF, "data size left at 0"

    def test_normal_recording_still_gets_exact_sizes(self, tmp_path):
        path = tmp_path / "small.wav"
        handle = open(path, "wb")
        shows_routes._write_wav_header(handle)
        handle.write(b"\0" * 1000)

        shows_routes._finalize_wav(handle)

        raw = path.read_bytes()
        assert struct.unpack("<I", raw[4:8])[0] == 36 + 1000
        assert struct.unpack("<I", raw[40:44])[0] == 1000

    def test_show_recording_stop_writes_a_readable_wav(self, app_client, db_user):
        import wave
        from pathlib import Path

        show_id = _make_show(db_user.id)
        audio_path = _start_show(app_client, db_user, show_id).json()["audio_file_path"]
        with state.sync_lock:
            state.current_show_audio_file.write(b"\0" * 2048)

        with patch_owner(db_user):
            assert app_client.post(f"/api/shows/{show_id}/stop").status_code == 200

        raw = Path(audio_path).read_bytes()
        with wave.open(io.BytesIO(raw), "rb") as wav:
            frames = wav.getnframes()
        # A mixer thread in this process can push extra frames through
        # GlobalState between start and stop, so pin the D13 invariant on one
        # snapshot instead of an exact byte count: the RIFF/data sizes must describe
        # the payload actually on disk (before the fix they stayed 0) and must cover
        # the bytes we wrote.
        assert frames >= 2048 // 4, f"header claims {frames} frames, expected at least 512"
        assert struct.unpack("<I", raw[4:8])[0] == len(raw) - 8, "RIFF size does not match the file"
        assert struct.unpack("<I", raw[40:44])[0] == len(raw) - 44, "data size does not match the file"
