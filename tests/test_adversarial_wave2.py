"""Regression tests for the wave-2 adversarial-review findings (ASYNC-2, ASYNC-3,
DATA-3, DATA-4, DATA-5, AUDIO-2, AUDIO-3, SEC-4).

Each test is named after the finding ID it pins, so a future regression points
straight back at the review item. GPU-stack-dependent worker tests reuse the
framework_generator stubbing pattern from tests/test_queue_lease_and_dedup.py.
"""

import asyncio
import os
import sys
import threading
import types
import uuid
import wave
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

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
    state.currently_playing_show_id = None
    state.is_playback_active = False
    yield
    state.dj_password = ""
    state.audience_password = ""
    state.current_show_id = None
    state.is_show_recording = False
    state.current_show_audio_file = None
    state.currently_playing_show_id = None
    state.is_playback_active = False
    # Stop (not just forget) any player a test left running: a live
    # ShowPlayback rewinds on EOF forever and broadcasts real PCM into every
    # state.audio_clients queue for the rest of the suite (poisons e.g. the
    # youtube relay stdin tests). .clear() alone leaked the thread.
    for leaked_player in list(shows_routes._active_playbacks.values()):
        leaked_player.stop()
    shows_routes._active_playbacks.clear()


@pytest.fixture(scope="module")
def worker_module():
    """Import app.worker with the GPU generator module stubbed out.

    Same pattern as tests/test_queue_lease_and_dedup.py: a module-level stub
    keeps tests/test_worker.py's collection-time skip intact while making
    app.worker importable here.
    """
    saved = sys.modules.get("app.framework.framework_generator")
    fake = types.ModuleType("app.framework.framework_generator")

    class GeneratorRegistry:  # minimal stand-in; worker logic doesn't use it here
        def __init__(self, *args, **kwargs):
            self.models = {}

        def load(self):
            pass

    fake.GeneratorRegistry = GeneratorRegistry
    sys.modules["app.framework.framework_generator"] = fake
    from app import worker  # imported after the stub is in place

    yield worker
    if saved is None:
        sys.modules.pop("app.framework.framework_generator", None)
    else:
        sys.modules["app.framework.framework_generator"] = saved


def _pool_yielding(conn) -> MagicMock:
    """An asyncpg-like pool whose `async with pool.acquire() as c:` yields conn."""
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return pool


def _make_conn() -> MagicMock:
    """An asyncpg-like connection whose transaction() is an async context manager."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock()
    conn.fetch = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)
    return conn


def _make_worker(worker_module):
    worker = worker_module.GeneratorWorker(
        worker_module.WorkerConfig(
            worker_id="wave2-worker",
            pg_dsn="postgresql://localhost/test",
            garage=MagicMock(),
        )
    )
    worker.db = _pool_yielding(_make_conn())
    return worker


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# ASYNC-2: generation timeout must not starve the shared default executor
# ---------------------------------------------------------------------------


class TestAsync2PrivateGenerationPool:
    def test_generate_with_lease_shuts_down_private_pool(self, worker_module):
        """The per-job generation pool must be created, passed to
        _generate_and_upload, and abandoned (shutdown(wait=False,
        cancel_futures=True)) even on success — a timed-out job's hung thread
        must never hold the shared default executor."""
        worker = _make_worker(worker_module)
        created = []
        pools_seen_by_generate = []

        real_executor = worker_module.ThreadPoolExecutor

        class RecordingPool(real_executor):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)

            def shutdown(self, **kwargs):
                self.shutdown_kwargs = kwargs
                super().shutdown(**kwargs)

        async def fake_generate(job, gen_pool=None):
            pools_seen_by_generate.append(gen_pool)
            return "audio/x.aac", 1.0

        with (
            patch.object(worker_module, "ThreadPoolExecutor", RecordingPool),
            patch.object(worker, "_heartbeat_loop", new_callable=AsyncMock),
            patch.object(worker, "_generate_and_upload", side_effect=fake_generate),
        ):
            result = _run(worker._generate_with_lease({"id": uuid.uuid4()}))

        assert result == ("audio/x.aac", 1.0)
        assert len(created) == 1, "exactly one private pool per job"
        assert pools_seen_by_generate == [created[0]], "generation must use the private pool"
        assert created[0].shutdown_kwargs == {"wait": False, "cancel_futures": True}

    def test_generate_and_upload_runs_generation_in_private_pool(self, worker_module):
        """generate_stem must run in the caller-provided pool (thread name has
        the pool's prefix), not the shared default executor."""
        worker = _make_worker(worker_module)
        worker.garage = MagicMock()
        worker.garage.put_object = AsyncMock()
        seen_threads = []
        fake_audio = np.zeros((4, 2), dtype=np.float32)

        def record_thread(**kwargs):
            seen_threads.append(threading.current_thread().name)
            return fake_audio

        worker.generators = MagicMock()
        worker.generators.generate_stem.side_effect = record_thread

        job = {"id": uuid.uuid4(), "model_id": "m", "prompt": "p", "key": "", "bpm": 120, "bars": 4}
        gen_pool = worker_module.ThreadPoolExecutor(max_workers=1, thread_name_prefix="gen-test")
        with (
            patch.object(worker_module, "encode_aac", return_value=b"aac"),
            patch.object(worker_module, "get_audio_duration", return_value=1.0),
        ):
            path, duration = _run(worker._generate_and_upload(job, gen_pool))
        gen_pool.shutdown(wait=True)

        assert path == f"audio/{job['id']}.aac"
        assert duration == 1.0
        assert len(seen_threads) == 1
        assert seen_threads[0].startswith("gen-test"), (
            f"generation must run in the private pool, ran in {seen_threads[0]!r}"
        )


# ---------------------------------------------------------------------------
# ASYNC-3: app-side asyncpg pool needs a bounded command_timeout
# ---------------------------------------------------------------------------


class TestAsync3PoolCommandTimeout:
    def test_module_pool_created_with_command_timeout(self, monkeypatch):
        """_get_asyncpg_pool must bound queries (30s) like worker/cleanup pools."""
        import app.job_waiter as job_waiter

        created_kwargs = {}

        fake_pool = MagicMock()
        fake_pool.close = AsyncMock()

        async def fake_create_pool(dsn, **kwargs):
            created_kwargs.update(kwargs)
            return fake_pool

        monkeypatch.setattr(job_waiter.asyncpg, "create_pool", fake_create_pool)
        monkeypatch.setattr(job_waiter, "_asyncpg_pool", None)

        _run(job_waiter._get_asyncpg_pool("postgresql://localhost/test"))
        assert created_kwargs.get("command_timeout") == 30

        # Restore module singleton so later tests are unaffected.
        _run(job_waiter.close_asyncpg_pool())


# ---------------------------------------------------------------------------
# DATA-3: llm_interactions.action_type rollup read the wrong key
# ---------------------------------------------------------------------------


class TestData3AuditActionRollup:
    def test_audit_loop_meta_rolls_up_action_types(self):
        from app.framework.audit_recording import _audit_loop_meta

        response = {"actions": [{"action_type": "retain"}, {"action_type": "add"}]}
        meta = _audit_loop_meta(response, [])
        assert meta["action_type"] == "add", "add outranks retain in the rollup"

        response = {"actions": [{"action_type": "retain"}, {"action_type": "remove"}]}
        meta = _audit_loop_meta(response, [])
        assert meta["action_type"] == "remove", "remove outranks retain in the rollup"

    def test_audit_loop_meta_action_type_none_without_actions(self):
        from app.framework.audit_recording import _audit_loop_meta

        assert _audit_loop_meta({"actions": []}, [])["action_type"] is None


# ---------------------------------------------------------------------------
# DATA-4: terminal job writes must be lease-guarded
# ---------------------------------------------------------------------------


class TestData4LeaseOwnershipGuards:
    def test_mark_job_complete_guarded_and_notifies_on_success(self, worker_module):
        worker = _make_worker(worker_module)
        conn = _make_conn()
        conn.execute = AsyncMock(side_effect=["UPDATE 1", "UPDATE 1"])
        worker.db = _pool_yielding(conn)

        _run(worker._mark_job_complete(uuid.uuid4(), "audio/x.aac", 1.0))

        update_sql = conn.execute.call_args_list[0].args[0]
        assert "AND status = 'processing'" in update_sql
        assert "AND worker_id = $" in update_sql
        notify_sql = conn.execute.call_args_list[1].args[0]
        assert "pg_notify" in notify_sql

    def test_mark_job_complete_skips_notify_when_lease_lost(self, worker_module):
        worker = _make_worker(worker_module)
        conn = _make_conn()
        conn.execute = AsyncMock(side_effect=["UPDATE 0"])
        worker.db = _pool_yielding(conn)

        _run(worker._mark_job_complete(uuid.uuid4(), "audio/x.aac", 1.0))

        assert conn.execute.await_count == 1, "a zombie worker must not NOTIFY a reclaimed job"

    def test_mark_job_failed_guarded_and_silent_when_lease_lost(self, worker_module):
        worker = _make_worker(worker_module)
        conn = _make_conn()
        conn.execute = AsyncMock(side_effect=["UPDATE 0"])
        worker.db = _pool_yielding(conn)

        # Must not raise when the row is no longer ours.
        _run(worker._mark_job_failed(uuid.uuid4(), "boom"))

        update_sql = conn.execute.await_args.args[0]
        assert "AND status = 'processing'" in update_sql
        assert "AND worker_id = $" in update_sql


# ---------------------------------------------------------------------------
# DATA-5: cross-show start/stop guards
# ---------------------------------------------------------------------------


class TestData5CrossShowRecordingGuards:
    def test_stop_show_recording_ignores_foreign_show(self):
        """Stopping a stale 'live' row must not detach another show's handle."""
        handle = object()
        state.current_show_id = 1
        state.current_show_audio_file = handle
        state.is_show_recording = True

        assert shows_routes._stop_show_recording(2) is None
        assert state.current_show_audio_file is handle, "foreign show's handle must stay attached"
        assert state.is_show_recording is True

        assert shows_routes._stop_show_recording(1) is handle, "owner stop still detaches"
        assert state.current_show_audio_file is None
        assert state.is_show_recording is False

    def test_start_show_conflicts_while_another_recording(self, app_client, tmp_path, monkeypatch):
        """Starting a second show while one records must 409, not overwrite the
        live handle slot."""
        from types import SimpleNamespace
        from unittest.mock import patch as mock_patch

        monkeypatch.setenv("SHOWS_DIR", str(tmp_path))
        owner = SimpleNamespace(id=1, username="u", is_active=True)

        with mock_patch("app.routes.utils.get_current_user_from_request", return_value=owner):
            with mock_patch("app.routes.shows.DatabaseManager") as db_mock:
                db_mock.get_instance.return_value.session.return_value.__enter__.return_value = MagicMock()
                with mock_patch("app.routes.shows._transition_show_to_live") as transition:
                    with mock_patch("app.routes.shows._write_wav_header"):
                        state.current_show_id = 42  # another show is live
                        resp = app_client.post("/api/shows/7/start")

        assert resp.status_code == 409, resp.text
        transition.assert_not_called(), "DB must not move the show to live on conflict"


# ---------------------------------------------------------------------------
# AUDIO-2: playback routes must drive the ShowPlayback player
# ---------------------------------------------------------------------------


class TestAudio2PlaybackWiring:
    def _write_wav(self, path):
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b"\x00\x00\x00\x00" * 100)

    def test_start_and_stop_playback_drive_player(self, app_client, tmp_path):
        from types import SimpleNamespace
        from unittest.mock import patch as mock_patch

        wav_path = tmp_path / "audio.wav"
        self._write_wav(wav_path)
        show = SimpleNamespace(id=5, user_id=1, status="ended", audio_file_path=str(wav_path))
        owner = SimpleNamespace(id=1, username="u", is_active=True)

        with mock_patch.object(shows_routes.state, "broadcast_audio", MagicMock()):
            with mock_patch("app.routes.utils.get_current_user_from_request", return_value=owner):
                with mock_patch.object(shows_routes, "DatabaseManager") as db_mock:
                    session = MagicMock()
                    session.query.return_value.filter.return_value.first.return_value = show
                    db_mock.get_instance.return_value.session.return_value.__enter__.return_value = session

                    start_resp = app_client.post("/api/shows/5/playback/start")
                    assert start_resp.status_code == 200, start_resp.text
                    player = shows_routes._active_playbacks.get(5)
                    assert player is not None, "playback start must register a live player"
                    assert player.is_playing is True
                    streaming_thread = player.playback_thread

                    stop_resp = app_client.post("/api/shows/5/playback/stop")
                    assert stop_resp.status_code == 200, stop_resp.text
                    assert 5 not in shows_routes._active_playbacks
                    assert player.is_playing is False, "stop must terminate the streaming thread"
                    assert streaming_thread is not None and not streaming_thread.is_alive()

    def test_stop_playback_without_player_clears_flags(self, app_client):
        from types import SimpleNamespace
        from unittest.mock import patch as mock_patch

        owner = SimpleNamespace(id=1, username="u", is_active=True)
        state.is_playback_active = True
        state.currently_playing_show_id = 9

        with mock_patch("app.routes.utils.get_current_user_from_request", return_value=owner):
            resp = app_client.post("/api/shows/9/playback/stop")

        assert resp.status_code == 200
        assert state.is_playback_active is False
        assert state.currently_playing_show_id is None

    def test_starting_second_show_stops_first_players_stream(self, app_client, tmp_path):
        """AUDIO-2 follow-up: "one playback at a time" is global, not per-show.
        Starting show B while show A plays must stop A's live player; the old
        code only retired the SAME show's player, leaving A looping forever
        (ShowPlayback rewinds on EOF) and double-broadcasting audio."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from unittest.mock import patch as mock_patch

        wavs = {11: tmp_path / "a.wav", 12: tmp_path / "b.wav"}
        for wav_path in wavs.values():
            self._write_wav(wav_path)

        def fake_require_show_owner(show_id, request, session):
            return SimpleNamespace(id=show_id, user_id=1, status="ended", audio_file_path=str(wavs[show_id]))

        with mock_patch.object(shows_routes.state, "broadcast_audio", MagicMock()):
            with mock_patch("app.routes.shows.require_show_owner", side_effect=fake_require_show_owner):
                start_a = app_client.post("/api/shows/11/playback/start")
                assert start_a.status_code == 200, start_a.text
                player_a = shows_routes._active_playbacks.get(11)
                assert player_a is not None and player_a.is_playing is True
                thread_a = player_a.playback_thread

                start_b = app_client.post("/api/shows/12/playback/start")
                assert start_b.status_code == 200, start_b.text
                player_b = shows_routes._active_playbacks.get(12)

        assert player_b is not None and player_b.is_playing is True
        assert 11 not in shows_routes._active_playbacks, "starting show B must retire show A's player"
        assert player_a.is_playing is False, "show A's stream must be stopped when show B starts"
        assert thread_a is not None and not thread_a.is_alive(), (
            "show A's playback thread must be joined when show B starts"
        )


# ---------------------------------------------------------------------------
# AUDIO-3: pregen loops must repopulate the stem-download LRU
# ---------------------------------------------------------------------------


class TestAudio3PregenLruMirror:
    async def test_commit_state_mirrors_pregen_audio_into_lru(self):
        """The foreground P11 commit (not the background pregen task) records
        pregen audio in state.cache_stem, so stem downloads work past loop 1
        without breaking the brief-01 risk-#4 divergence."""
        from app.framework.framework_main_async import AsyncFrameworkLoop

        loop = AsyncFrameworkLoop(uuid.uuid4())
        loop.mixer = MagicMock()
        loop._loop_idx = 2
        loop._pregen_task = MagicMock()
        loop._pregen_task.done.return_value = True
        audio_a = np.ones((4, 2), dtype=np.float32)
        audio_b = np.zeros((4, 2), dtype=np.float32)
        loop._pregen_results = {
            "loop_idx": 2,
            "prepared_tracks": [(audio_a, 0), (audio_b, 1)],
            "next_stems": [{"prompt": "Pad, A minor, 128"}, {"prompt": "Bass, A minor, 128"}],
        }

        with patch.object(state, "cache_stem") as cache_stem_mock:
            await loop._step_commit_state(True, [], 0)

        calls = {call.args[0]: call.args[1] for call in cache_stem_mock.call_args_list}
        assert calls.get("Pad, A minor, 128") is audio_a
        assert calls.get("Bass, A minor, 128") is audio_b

    async def test_pregeneration_background_still_never_calls_cache_stem(self):
        """Guard the pinned invariant (brief-01 risk #4): the background pregen
        path itself must still not touch the LRU — only P11's foreground mirror."""
        from unittest.mock import patch as mock_patch

        from app.framework.framework_main_async import AsyncFrameworkLoop

        loop = AsyncFrameworkLoop(uuid.uuid4())
        loop._loop_idx = 1
        audio = np.ones((10, 2), dtype=np.float32)

        def _snapshot():
            return {
                "current_bpm": 128,
                "current_key": "A minor",
                "active_stems": [],
                "user_override": "",
                "available_instruments": [],
                "stem_history": [],
                "llm_config": {"base_url": "http://x:1234/v1", "api_key": "k", "model": "m"},
            }

        response = {
            "master_bpm": 128,
            "master_key": "A minor",
            "actions": [
                {
                    "action_type": "add",
                    "sub_family": "Synth Pad",
                    "major_family": "Synth",
                    "model_id": "foundation-1",
                    "bars": 4,
                }
            ],
            "reasoning": "add a pad",
            "name": "Pad Set",
        }

        with (
            mock_patch.object(loop, "conductor") as mc,
            mock_patch.object(loop, "_submit_job", new_callable=AsyncMock, return_value=uuid.uuid4()),
            mock_patch.object(loop, "_fetch_audio", new_callable=AsyncMock, return_value=audio),
            mock_patch.object(loop, "_await_jobs", new_callable=AsyncMock, return_value={}),
            mock_patch.object(state, "cache_stem") as cache_stem_mock,
        ):
            mc.get_next_state_async = AsyncMock(return_value=response)
            from app.framework.pregeneration import run_pregeneration

            await run_pregeneration(loop, 2, _snapshot())

        assert not cache_stem_mock.called, "background pregen must NOT route through state.cache_stem"


# ---------------------------------------------------------------------------
# SEC-4: jobs endpoints must require authentication
# ---------------------------------------------------------------------------


class TestSec4JobsAuth:
    def test_job_endpoints_reject_anonymous(self, app_client):
        """GET /api/jobs, /api/jobs/{id} and /api/audio/{id} previously leaked
        every user's queue contents to anonymous peers."""
        job_id = str(uuid.uuid4())
        assert app_client.get("/api/jobs").status_code == 401
        assert app_client.get(f"/api/jobs/{job_id}").status_code == 401
        assert app_client.get(f"/api/audio/{job_id}").status_code == 401

    def test_job_endpoints_allow_authenticated(self, app_client):
        from types import SimpleNamespace
        from unittest.mock import patch as mock_patch

        owner = SimpleNamespace(id=1, username="u", is_active=True)
        with mock_patch("app.routes.jobs.get_current_user_from_request", return_value=owner):
            with mock_patch("app.routes.jobs.DatabaseManager") as db_mock:
                session = MagicMock()
                query = session.query.return_value
                query.count.return_value = 0
                query.order_by.return_value.limit.return_value.offset.return_value.all.return_value = []
                query.filter.return_value.first.return_value = None
                db_mock.get_instance.return_value.session.return_value.__enter__.return_value = session

                assert app_client.get("/api/jobs").status_code == 200
                assert app_client.get(f"/api/jobs/{uuid.uuid4()}").status_code == 404  # authed, then not found
                assert app_client.get(f"/api/audio/{uuid.uuid4()}").status_code == 404  # authed, then not found
