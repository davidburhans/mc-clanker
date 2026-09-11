"""REL-05 + REL-16 regression tests — storage retention, delete-show GC, session reaper (U5).

Pins the contract in refactor/plans/units/rel-05-plan.md §3.1:

- T1-T4   delete_show unlinks every recording take (persisted path + per-id dir
          sweep), survives missing/None/non-str paths, and retires a live
          playback player before unlinking
- T5-T8   mtime-based retention passes remove only expired, pattern-matched
          files; disabled by default; missing dirs are a no-op
- T9-T10  the session_routing reaper deletes stale rows via the heartbeat index
          predicate (24 h default, 0 disables, env-overridable), tag-parsed
          defensively
- T11-T13 audit retention is opt-in (invariant 4): the default keeps everything
          and issues zero corpus SQL; enabled mode writes an fsync'd NDJSON
          archive first and deletes only the archived ids; an archive failure
          keeps every row
- T14     the Mapping dump shaper matches the ORM dump exactly (asyncpg str-JSON
          normalization pinned)

Cleanup passes run against the FakeConnection/_pool fakes from
tests/test_round3_fix_e.py / tests/test_queue_lease_and_dedup.py.
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = ""  # Force SQLite for tests

from app.app_ui import app  # noqa: E402  (must import after the env override)
from app.cleanup import (  # noqa: E402
    CleanupConfig,
    JobExpirationCleanup,
    create_cleanup_config_from_env,
)
from app.framework.framework_state import state  # noqa: E402
from app.routes import shows as shows_routes  # noqa: E402

# ---------------------------------------------------------------------------
# Route fixtures (test_adversarial_leftovers.py pattern)
# ---------------------------------------------------------------------------


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
    """Reset global state between tests, incl. fields state.reset() keeps."""
    state.reset()
    state.dj_password = ""
    state.audience_password = ""
    state.current_show_id = None
    state.is_show_recording = False
    state.current_show_audio_file = None
    state.is_recording = False
    state.recording_file_handle = None
    shows_routes._active_playbacks.clear()
    yield
    state.dj_password = ""
    state.audience_password = ""
    shows_routes._active_playbacks.clear()


@pytest.fixture
def db_user():
    """A real User row; returns a lightweight id holder (row may expire)."""
    from app.db import DatabaseManager
    from app.models import User

    db = DatabaseManager.get_instance()
    suffix = uuid.uuid4().hex[:8]
    with db.session() as session:
        user = User(
            username=f"retention_{suffix}",
            email=f"retention_{suffix}@example.com",
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
        show = Show(user_id=user_id, title=f"Retention {uuid.uuid4().hex[:6]}", status=status)
        session.add(show)
        session.flush()
        return show.id


def _set_show_audio_path(show_id: int, audio_path) -> None:
    """Persist ``audio_file_path`` on the row, as start_show would."""
    from app.db import DatabaseManager
    from app.models import Show

    db = DatabaseManager.get_instance()
    with db.session() as session:
        session.get(Show, show_id).audio_file_path = str(audio_path)


# ---------------------------------------------------------------------------
# Cleanup fakes (test_round3_fix_e.py pattern)
# ---------------------------------------------------------------------------


class RoutingFakeConnection:
    """Minimal asyncpg.Connection stand-in: records every call; fetch returns the
    canned rows for whichever table the SQL names (asyncpg Record ≈ Mapping)."""

    def __init__(self, fetch_by_table=None, execute_tag="DELETE 1"):
        self.fetch_by_table = fetch_by_table or {}
        self.execute_tag = execute_tag
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        for table, rows in self.fetch_by_table.items():
            if table in sql:
                return rows
        return []

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return self.execute_tag


class _AsyncContext:
    """Wraps a value into the async CM shape of ``pool.acquire()``."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


def _pool(connection) -> MagicMock:
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncContext(connection))
    pool.close = AsyncMock()
    return pool


def _cleanup(connection, tmp_path, **retention) -> JobExpirationCleanup:
    """A JobExpirationCleanup over fakes, rooted at tmp dirs (no env coupling)."""
    kwargs = dict(
        pg_dsn="postgresql://u:p@localhost/db",
        garage=MagicMock(),
        shows_dir=str(tmp_path / "shows"),
        export_dir=str(tmp_path / "exports"),
        audit_archive_dir=str(tmp_path / "audit_archive"),
    )
    kwargs.update(retention)
    cleanup = JobExpirationCleanup(CleanupConfig(**kwargs))
    cleanup.db = _pool(connection)
    return cleanup


def _interaction_row(row_id: int, show_id: int = 7) -> dict:
    """One llm_interactions row in asyncpg shape: JSON columns arrive as str."""
    return {
        "id": row_id,
        "show_id": show_id,
        "loop_index": 3,
        "timestamp": datetime(2025, 1, 1, 12, 0, 0),
        "relative_time_ms": 45_000,
        "prompt_messages": json.dumps(
            [
                {"role": "system", "content": "you are the conductor"},
                {"role": "user", "content": "vibe: warehouse, 128 bpm"},
            ]
        ),
        "parsed_response": json.dumps({"actions": [], "reasoning": "lock it in"}),
        "applied_actions": json.dumps([{"instrument": "Synth Pad", "outcome": "generated"}]),
        "reasoning": "lock it in",
        "error": None,
        "was_fallback": False,
        "bpm": 128.0,
        "key": "A minor",
        "instruments": json.dumps(["Synth Pad"]),
        "action_type": "add",
        "set_name": "Peak Time",
    }


def _action_row(row_id: int, show_id: int = 7) -> dict:
    """One show_actions row in asyncpg shape."""
    return {
        "id": row_id,
        "show_id": show_id,
        "loop_index": 3,
        "timestamp": datetime(2025, 1, 1, 12, 0, 0),
        "relative_time_ms": 45_000,
        "action_type": "add",
        "stem_index": 2,
        "stem_details": json.dumps({"instrument": "Synth Pad"}),
        "action_description": "add Synth Pad",
    }


# ---------------------------------------------------------------------------
# REL-05a — delete_show leaves zero orphan files
# ---------------------------------------------------------------------------


def test_delete_show_removes_all_takes_and_dir(app_client, db_user, tmp_path, monkeypatch):
    """T1 (acceptance): DELETE removes the persisted take, the stamped and uuid
    takes, and the show's directory — the stamped/uuid takes were previously
    reachable by nothing."""
    monkeypatch.setenv("SHOWS_DIR", str(tmp_path / "shows"))
    show_id = _make_show(db_user.id, status="ended")
    show_dir = tmp_path / "shows" / str(show_id)
    show_dir.mkdir(parents=True)
    persisted = show_dir / "audio.wav"
    stamped = show_dir / "audio_20240101T120000123456.wav"
    uuid_take = show_dir / "audio_deadbeefdeadbeef.wav"
    for take in (persisted, stamped, uuid_take):
        take.write_bytes(b"\x00" * 32)
    _set_show_audio_path(show_id, persisted)

    with patch_owner(db_user):
        response = app_client.delete(f"/api/shows/{show_id}")

    assert response.status_code == 204
    assert not show_dir.exists(), "every take AND the show dir must be gone"


def test_delete_show_unlinks_persisted_path_outside_current_shows_dir(app_client, db_user, tmp_path, monkeypatch):
    """T2: SHOWS_DIR moved between recording and deleting — the persisted path is
    still unlinked; the sweep in the new root no-ops."""
    old_root = tmp_path / "recorded-here"
    monkeypatch.setenv("SHOWS_DIR", str(old_root))
    show_id = _make_show(db_user.id, status="ended")
    show_dir = old_root / str(show_id)
    show_dir.mkdir(parents=True)
    persisted = show_dir / "audio.wav"
    persisted.write_bytes(b"\x00" * 32)
    _set_show_audio_path(show_id, persisted)
    monkeypatch.setenv("SHOWS_DIR", str(tmp_path / "moved-here"))  # the env changed since the take

    with patch_owner(db_user):
        assert app_client.delete(f"/api/shows/{show_id}").status_code == 204

    assert not persisted.exists(), "the persisted path is unlinked even though SHOWS_DIR moved"
    assert not (tmp_path / "moved-here").exists(), "the sweep in the new root finds nothing to do"


def test_delete_show_missing_or_nonstr_audio_path_is_safe(app_client, db_user, tmp_path, monkeypatch):
    """T3 keep-green guard: NULL paths, takes whose file vanished, and corrupted
    rows carrying a non-str path all delete cleanly (204, zero files removed)."""
    monkeypatch.setenv("SHOWS_DIR", str(tmp_path / "shows"))

    null_path_id = _make_show(db_user.id, status="ended")  # audio_file_path stays NULL
    with patch_owner(db_user):
        assert app_client.delete(f"/api/shows/{null_path_id}").status_code == 204

    missing_id = _make_show(db_user.id, status="ended")
    missing_path = tmp_path / "shows" / str(missing_id) / "audio.wav"
    _set_show_audio_path(missing_id, missing_path)  # basename matches; the file does not exist
    with patch_owner(db_user):
        assert app_client.delete(f"/api/shows/{missing_id}").status_code == 204

    corrupted = SimpleNamespace(user_id=db_user.id, audio_file_path=object())  # not a str, not None
    session_mock = MagicMock()
    session_mock.query.return_value.filter.return_value.first.return_value = corrupted
    with patch("app.routes.shows.DatabaseManager") as db_mock:
        db_mock.get_instance.return_value.session.return_value.__enter__.return_value = session_mock
        with patch_owner(db_user):
            assert app_client.delete("/api/shows/424242").status_code == 204


def test_delete_show_retires_live_playback_before_unlink(app_client, db_user, tmp_path, monkeypatch):
    """T4: a live ShowPlayback for the deleted show is stopped and dropped from
    the registry before its audio is unlinked (no zombie looping a deleted file)."""
    monkeypatch.setenv("SHOWS_DIR", str(tmp_path / "shows"))
    show_id = _make_show(db_user.id, status="ended")
    audio = tmp_path / "shows" / str(show_id) / "audio.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"\x00" * 32)
    _set_show_audio_path(show_id, audio)
    player = SimpleNamespace(stop=MagicMock())
    shows_routes._active_playbacks[show_id] = player

    with patch_owner(db_user):
        assert app_client.delete(f"/api/shows/{show_id}").status_code == 204

    player.stop.assert_called_once()
    assert show_id not in shows_routes._active_playbacks
    assert not audio.exists()


# ---------------------------------------------------------------------------
# REL-05b — mtime-based retention passes
# ---------------------------------------------------------------------------


async def test_recordings_retention_removes_only_expired(tmp_path):
    """T5 (acceptance): only audio*.wav older than the window go; fresh files and
    non-audio neighbors stay; a fully-emptied show dir is pruned, a non-empty one is not."""
    old_mtime = time.time() - 15 * 86400  # older than the 14-day window
    old_dir = tmp_path / "shows" / "7"
    fresh_dir = tmp_path / "shows" / "8"
    pruned_dir = tmp_path / "shows" / "9"
    for directory in (old_dir, fresh_dir, pruned_dir):
        directory.mkdir(parents=True)
    old_canonical = old_dir / "audio.wav"
    old_stamped = old_dir / "audio_20240101T000000000000.wav"
    keeper = old_dir / "notes.txt"
    fresh = fresh_dir / "audio.wav"
    last_take = pruned_dir / "audio.wav"
    for path in (old_canonical, old_stamped, keeper, fresh, last_take):
        path.write_bytes(b"\x00")
    for expired in (old_canonical, old_stamped, last_take):
        os.utime(expired, (old_mtime, old_mtime))

    cleanup = _cleanup(RoutingFakeConnection(), tmp_path, show_audio_retention_days=14)
    removed = await cleanup._sweep_expired_recordings()

    assert removed == 3
    assert not old_canonical.exists() and not old_stamped.exists() and not last_take.exists()
    assert keeper.exists() and old_dir.exists(), "non-audio neighbors keep the show dir alive"
    assert fresh.exists() and fresh_dir.exists()
    assert not pruned_dir.exists(), "a fully-emptied show dir is pruned"


async def test_exports_retention_touches_only_mc_clanker_files(tmp_path):
    """T6 (acceptance): the exports sweep is flat and pattern-scoped — notes.txt,
    a fresh export, and the nested shows/ dir (which shares the /exports root in
    compose) all survive."""
    old_mtime = time.time() - 8 * 86400  # older than the 7-day window
    old_wav = tmp_path / "exports" / "mc_clanker_20250101_120000.wav"
    old_mp3 = tmp_path / "exports" / "mc_clanker_20250102_120000.mp3"
    fresh = tmp_path / "exports" / "mc_clanker_live.wav"
    keeper = tmp_path / "exports" / "notes.txt"
    nested_dir = tmp_path / "exports" / "shows" / "9"
    nested = nested_dir / "audio.wav"
    for path in (old_wav, old_mp3, fresh, keeper, nested):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00")
    for expired in (old_wav, old_mp3, nested):
        os.utime(expired, (old_mtime, old_mtime))

    cleanup = _cleanup(RoutingFakeConnection(), tmp_path, export_retention_days=7)
    removed = await cleanup._sweep_expired_recordings()

    assert removed == 2
    assert not old_wav.exists() and not old_mp3.exists()
    assert fresh.exists() and keeper.exists()
    assert nested.exists(), "the sweep must never recurse into the nested shows/ dir"
    assert nested_dir.exists()


async def test_file_retention_disabled_by_default(tmp_path, monkeypatch):
    """T7 (decision 3): bare defaults never delete user recordings — retention
    fields are 0 and a full cycle issues zero filesystem unlinks."""
    config = CleanupConfig(pg_dsn="postgresql://u:p@localhost/db", garage=MagicMock())
    assert config.show_audio_retention_days == 0
    assert config.export_retention_days == 0
    assert config.llm_retention_days == 0
    assert config.session_stale_hours == 24
    assert config.audit_archive_dir == "/exports/audit_archive"

    for name in (
        "SHOW_AUDIO_RETENTION_DAYS",
        "EXPORT_RETENTION_DAYS",
        "SESSION_STALE_HOURS",
        "LLM_RETENTION_DAYS",
        "AUDIT_ARCHIVE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    for name in ("GARAGE_ENDPOINT", "GARAGE_ACCESS_KEY", "GARAGE_SECRET_KEY", "GARAGE_BUCKET"):
        monkeypatch.setenv(name, "x")
    env_config = create_cleanup_config_from_env()
    assert (env_config.show_audio_retention_days, env_config.export_retention_days) == (0, 0)

    ancient = tmp_path / "shows" / "3" / "audio.wav"
    ancient.parent.mkdir(parents=True)
    ancient.write_bytes(b"\x00")
    os.utime(ancient, (time.time() - 400 * 86400,) * 2)
    unlink_calls = []
    monkeypatch.setattr(os, "unlink", lambda path: unlink_calls.append(path))

    cleanup = _cleanup(RoutingFakeConnection(), tmp_path)
    await cleanup._run_cleanup()

    assert ancient.exists(), "the default config must not touch a single recording"
    assert unlink_calls == []


async def test_file_retention_missing_dirs_noop(tmp_path):
    """T8 (worker-env guard): retention enabled but the dirs are absent (the
    worker container ships no SHOWS_DIR/EXPORT_DIR) → 0 removed, no raise."""
    cleanup = _cleanup(
        RoutingFakeConnection(),
        tmp_path,
        show_audio_retention_days=14,
        export_retention_days=7,
        shows_dir=str(tmp_path / "missing-shows"),
        export_dir=str(tmp_path / "missing-exports"),
    )
    assert await cleanup._sweep_expired_recordings() == 0


# ---------------------------------------------------------------------------
# REL-16b — session_routing reaper
# ---------------------------------------------------------------------------


async def test_session_reaper_deletes_only_stale_rows(tmp_path):
    """T9 (acceptance): one sargable DELETE against the heartbeat index, aged by
    the config (24 h default), with the command tag parsed defensively."""
    conn = RoutingFakeConnection(execute_tag="DELETE 3")
    cleanup = _cleanup(conn, tmp_path)

    reaped = await cleanup._reap_stale_sessions()

    assert reaped == 3
    execute_calls = [entry for entry in conn.calls if entry[0] == "execute"]
    assert len(execute_calls) == 1
    sql, args = execute_calls[0][1], execute_calls[0][2]
    flat_sql = " ".join(sql.split())
    assert "DELETE FROM session_routing" in flat_sql
    assert "last_heartbeat < NOW() - make_interval(hours => $1)" in flat_sql
    assert list(args) == [24]


async def test_session_reaper_survives_nonstr_command_tag(tmp_path):
    """T9 guard: the existing cleanup tests inject MagicMock connections whose
    execute() returns a MagicMock — the tag parse degrades to 0 instead of raising."""
    conn = RoutingFakeConnection(execute_tag=MagicMock())
    cleanup = _cleanup(conn, tmp_path)
    assert await cleanup._reap_stale_sessions() == 0


async def test_session_reaper_disabled_and_overridden(tmp_path, monkeypatch):
    """T10 (decision 3): 0 disables the reaper entirely (zero SQL);
    SESSION_STALE_HOURS=48 overrides the 24 h default through the env config."""
    conn = RoutingFakeConnection()
    cleanup = _cleanup(conn, tmp_path, session_stale_hours=0)

    assert await cleanup._reap_stale_sessions() == 0
    assert conn.calls == [], "a disabled reaper must issue no SQL at all"

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    for name in ("GARAGE_ENDPOINT", "GARAGE_ACCESS_KEY", "GARAGE_SECRET_KEY", "GARAGE_BUCKET"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("SESSION_STALE_HOURS", "48")
    assert create_cleanup_config_from_env().session_stale_hours == 48


# ---------------------------------------------------------------------------
# REL-16a — audit corpus retention (opt-in, export-before-delete)
# ---------------------------------------------------------------------------


async def test_audit_retention_default_keeps_everything(tmp_path):
    """T11 (acceptance, invariant 4): the default config must issue zero SQL
    against the corpus tables and never unlink a file — keep-forever."""
    ancient = tmp_path / "shows" / "1" / "audio.wav"
    ancient.parent.mkdir(parents=True)
    ancient.write_bytes(b"\x00")
    os.utime(ancient, (time.time() - 400 * 86400,) * 2)

    conn = RoutingFakeConnection()
    cleanup = _cleanup(conn, tmp_path)  # every retention knob at its default
    await cleanup._run_cleanup()

    corpus_sql = [entry for entry in conn.calls if "llm_interactions" in entry[1] or "show_actions" in entry[1]]
    assert corpus_sql == []
    assert ancient.exists()


async def test_audit_retention_archives_then_deletes_only_exported_ids(tmp_path):
    """T12 (acceptance): enabled retention first writes the NDJSON archive (dump
    shape, str-JSON normalized) and only then deletes exactly the archived ids."""
    conn = RoutingFakeConnection(
        fetch_by_table={
            "llm_interactions": [_interaction_row(101), _interaction_row(102)],
            "show_actions": [_action_row(201)],
        }
    )
    cleanup = _cleanup(conn, tmp_path, llm_retention_days=30, session_stale_hours=0)

    total = await cleanup._run_cleanup()

    assert total == 3
    archive_dir = tmp_path / "audit_archive"
    llm_files = list(archive_dir.glob("llm_interactions_*.ndjson"))
    action_files = list(archive_dir.glob("show_actions_*.ndjson"))
    assert len(llm_files) == 1 and len(action_files) == 1

    llm_rows = [json.loads(line) for line in llm_files[0].read_text().splitlines() if line]
    assert len(llm_rows) == 2
    first = llm_rows[0]
    assert [message["role"] for message in first["messages"]] == ["system", "user", "assistant"]
    assert json.loads(first["messages"][-1]["content"]) == {"actions": [], "reasoning": "lock it in"}
    assert isinstance(first["meta"]["applied_actions"], list), "normalized from the asyncpg str, not a str itself"
    assert first["meta"]["bpm"] == 128.0

    action_rows = [json.loads(line) for line in action_files[0].read_text().splitlines() if line]
    assert len(action_rows) == 1
    assert action_rows[0]["action_type"] == "add"
    assert action_rows[0]["stem_details"] == {"instrument": "Synth Pad"}, "normalized, not str"

    deletes = [entry for entry in conn.calls if entry[0] == "execute" and "DELETE" in entry[1].upper()]
    assert len(deletes) == 2, "the session reaper is disabled here, so exactly the two corpus DELETEs ran"
    ids_by_table = {}
    for _, sql, args in deletes:
        for table in ("llm_interactions", "show_actions"):
            if table in sql:
                ids_by_table[table] = [list(arg) for arg in args]
    assert ids_by_table["llm_interactions"] == [[101, 102]]
    assert ids_by_table["show_actions"] == [[201]]


async def test_audit_retention_archive_failure_keeps_rows(tmp_path, caplog):
    """T13 (decision 7): an archive write failure keeps every row this cycle —
    a DELETE may only ever follow a successful export."""
    blocked_dir = tmp_path / "blocked"
    blocked_dir.write_text("a file, not a directory")  # mkdir on this path fails
    conn = RoutingFakeConnection(
        fetch_by_table={
            "llm_interactions": [_interaction_row(101)],
            "show_actions": [_action_row(201)],
        }
    )
    cleanup = _cleanup(conn, tmp_path, llm_retention_days=30, session_stale_hours=0, audit_archive_dir=str(blocked_dir))

    with caplog.at_level(logging.ERROR):
        total = await cleanup._run_cleanup()

    assert total == 0
    deletes = [entry for entry in conn.calls if entry[0] == "execute" and "DELETE" in entry[1].upper()]
    assert deletes == [], "no row may be deleted when its archive failed"
    assert any(record.levelno >= logging.ERROR for record in caplog.records)


# ---------------------------------------------------------------------------
# Dump shaper — one source of truth for ORM + asyncpg rows
# ---------------------------------------------------------------------------


def test_llm_dump_row_matches_orm_dump():
    """T14 (decision 8): the pure Mapping shaper and the ORM dump are one shape —
    asyncpg's str-JSON columns normalize to the same structured values."""
    from app.models.llm_interaction import LLMInteraction, llm_dump_row

    dump_ts = datetime(2025, 3, 1, 10, 30, 0)
    messages = [
        {"role": "system", "content": "you are the conductor"},
        {"role": "user", "content": "vibe: warehouse"},
    ]
    parsed = {"actions": [], "reasoning": "hold the groove"}
    applied = [{"instrument": "Bass", "outcome": "cached"}]
    interaction = LLMInteraction(
        id=9,
        show_id=7,
        loop_index=2,
        timestamp=dump_ts,
        relative_time_ms=12_345,
        prompt_messages=messages,
        parsed_response=parsed,
        applied_actions=applied,
        reasoning="hold the groove",
        error=None,
        was_fallback=False,
        bpm=124.0,
        key="F minor",
        instruments=["Bass"],
        action_type="retain",
        set_name="Warmup",
    )
    raw_record = {
        "id": 9,
        "show_id": 7,
        "loop_index": 2,
        "timestamp": dump_ts,
        "relative_time_ms": 12_345,
        "prompt_messages": json.dumps(messages),
        "parsed_response": json.dumps(parsed),
        "applied_actions": json.dumps(applied),
        "reasoning": "hold the groove",
        "error": None,
        "was_fallback": False,
        "bpm": 124.0,
        "key": "F minor",
        "instruments": json.dumps(["Bass"]),
        "action_type": "retain",
        "set_name": "Warmup",
    }

    assert llm_dump_row(raw_record) == interaction.to_llm_dump_dict()

    # A non-JSON str column degrades to a raw passthrough instead of crashing
    # the retention pass (plan decision 7 guard).
    degraded = llm_dump_row(dict(raw_record, prompt_messages="legacy context-summary blob"))
    assert [message["role"] for message in degraded["messages"]] == ["assistant"]
