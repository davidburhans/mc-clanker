"""
Round-3 lane E (external I/O / queue infra) regression tests.

Covers the fixes landed in the round3-fixes branch:

- E1  app/worker.py            : orphan audio delete re-checks row ownership
- E2  app/cleanup.py           : Garage objects are deleted BEFORE the rows
- E3  app/models/session_routing.py : UUID PK / TIMESTAMPTZ / indexes from migration 001
- E4  app/framework/framework_icecast.py : _running cleared when ffmpeg dies
- E5  app/onboarding.py        : docker compose restart is bounded by a timeout
- E6  app/cleanup.py           : asyncio.Event makes SIGTERM interrupt the idle wait
- E7  app/cleanup.py           : the one-shot cron pool gets command_timeout
- E8  app/lib/recording_metadata.py : no destructive wave.open(); >4GiB -> ValueError
- E9  app/lib/recording_metadata.py : CUE quote/newline escaping
- E10 app/lib/recording_metadata.py : LIST/adtl sub-chunks carry a real RIFF header

External I/O (asyncpg pool, Garage, subprocess, ffmpeg) is faked by the named
helper classes below, following the patterns in tests/test_queue_lease_and_dedup.py.
"""

import asyncio
import os
import struct
import subprocess
import sys
import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import postgresql as pg_dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from app.cleanup import _POOL_COMMAND_TIMEOUT, CleanupConfig, JobExpirationCleanup, cleanup_expired_jobs_once
from app.lib import recording_metadata
from app.models.session_routing import SessionRouting

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeConnection:
    """Minimal asyncpg.Connection stand-in: records SQL, returns canned rows."""

    def __init__(self, fetch_rows=None, fetchrow_row=None, log: list | None = None):
        self._fetch_rows = fetch_rows or []
        self._fetchrow_row = fetchrow_row
        self.log = log if log is not None else []

    async def fetch(self, sql, *args):
        self.log.append(("fetch", sql))
        return self._fetch_rows

    async def fetchrow(self, sql, *args):
        self.log.append(("fetchrow", sql))
        return self._fetchrow_row

    async def execute(self, sql, *args):
        self.log.append(("execute", sql, args))
        return "DELETE 1"


class _RaisingConnection(FakeConnection):
    """Connection whose row read blows up (the DB-outage part of E1)."""

    async def fetchrow(self, sql, *args):
        raise RuntimeError("connection closed")


class _AsyncContext:
    """Wraps a value into the async CM shape of ``pool.acquire()``."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


class FakeGarage:
    """GarageClient stand-in recording deletes; raises for the configured paths."""

    def __init__(self, raise_on=(), log: list | None = None):
        self.raise_on = set(raise_on)
        self.deleted: list[str] = []
        self.log = log

    async def delete_object(self, path):
        self.deleted.append(path)
        if self.log is not None:
            self.log.append(("object", path))
        if path in self.raise_on:
            raise RuntimeError("garage unavailable")


def _pool(connection) -> MagicMock:
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncContext(connection))
    pool.close = AsyncMock()
    return pool


@pytest.fixture(scope="session")
def worker_module():
    """Import app.worker with the GPU generator module stubbed out."""
    saved = sys.modules.get("app.framework.framework_generator")
    fake = types.ModuleType("app.framework.framework_generator")

    class GeneratorRegistry:
        def __init__(self, *args, **kwargs):
            self.models = {}

        def load(self):
            pass

    fake.GeneratorRegistry = GeneratorRegistry
    sys.modules["app.framework.framework_generator"] = fake
    from app import worker

    yield worker
    if saved is None:
        sys.modules.pop("app.framework.framework_generator", None)
    else:
        sys.modules["app.framework.framework_generator"] = saved


# ---------------------------------------------------------------------------
# E1 — orphan delete must not stomp a reclaiming worker's audio
# ---------------------------------------------------------------------------


async def _drive_failed_completion(worker_module, ownership_row):
    """Run a claimed job whose completion write fails; return the worker used."""
    worker = worker_module.GeneratorWorker(
        worker_module.WorkerConfig(
            worker_id="worker-A",
            pg_dsn="postgresql://u:p@localhost/db",
            garage=MagicMock(),
        )
    )
    worker.garage = FakeGarage()
    worker.db = _pool(FakeConnection(fetchrow_row=ownership_row))
    worker._generate_with_lease = AsyncMock(return_value=("audio/job.aac", 4.0))
    worker._mark_job_complete = AsyncMock(side_effect=RuntimeError("db down"))
    worker._mark_job_failed = AsyncMock()
    await worker._process_claimed_job({"id": uuid.uuid4()})
    return worker


async def test_orphan_delete_skipped_when_row_reclaimed(worker_module):
    """E1: a completed-by-another-worker row means the object is NOT ours."""
    worker = await _drive_failed_completion(
        worker_module,
        {"status": "completed", "worker_id": "worker-B"},
    )
    assert worker.garage.deleted == []
    worker._mark_job_failed.assert_awaited_once()


async def test_orphan_delete_still_runs_while_row_is_ours(worker_module):
    """E1: the C5 orphan sweep still fires while we hold the processing lease."""
    worker = await _drive_failed_completion(
        worker_module,
        {"status": "processing", "worker_id": "worker-A"},
    )
    assert worker.garage.deleted == ["audio/job.aac"]


async def test_orphan_delete_skipped_when_ownership_unreadable(worker_module):
    """E1: no proof of ownership -> keep the object instead of deleting blind."""
    worker = worker_module.GeneratorWorker(
        worker_module.WorkerConfig(
            worker_id="worker-A",
            pg_dsn="postgresql://u:p@localhost/db",
            garage=MagicMock(),
        )
    )
    worker.garage = FakeGarage()
    worker.db = _pool(FakeConnection(fetchrow_row=None))
    assert await worker._still_own_job_row(uuid.uuid4()) is True  # row gone -> ours

    broken = _pool(_RaisingConnection())
    worker.db = broken
    assert await worker._still_own_job_row(uuid.uuid4()) is False


# ---------------------------------------------------------------------------
# E2 — objects first, rows second; failures logged + counted, rows kept
# ---------------------------------------------------------------------------


async def _run_delete_expired(rows, raise_on):
    """Run _delete_expired_jobs against fakes; returns (count, ordered call log)."""
    log: list = []
    connection = FakeConnection(fetch_rows=rows, log=log)
    cleanup = JobExpirationCleanup(MagicMock())
    cleanup.db = _pool(connection)
    cleanup.garage = FakeGarage(raise_on=raise_on, log=log)
    count = await cleanup._delete_expired_jobs()
    return count, log


def _kinds_before(log, kind):
    """Indices of the entries preceding the first entry of ``kind``."""
    first = next(i for i, entry in enumerate(log) if entry[0] == kind)
    return log[:first]


async def test_expired_objects_deleted_before_rows():
    """E2: every Garage delete of a cycle precedes the row DELETE."""
    rows = [{"audio_path": "audio/a.aac"}, {"audio_path": "audio/b.aac"}]
    count, log = await _run_delete_expired(rows, raise_on=())

    before_rows = _kinds_before(log, "execute")
    assert [entry[1] for entry in before_rows if entry[0] == "object"] == ["audio/a.aac", "audio/b.aac"]
    assert count == 2


async def test_expired_row_kept_when_object_delete_fails():
    """E2: a failed object delete keeps its row for retry and is counted."""
    rows = [{"audio_path": "audio/a.aac"}, {"audio_path": "audio/b.aac"}]
    count, log = await _run_delete_expired(rows, raise_on={"audio/b.aac"})

    delete_entry = next(entry for entry in log if entry[0] == "execute")
    assert count == 1
    assert "DELETE FROM generator_jobs" in delete_entry[1]
    assert delete_entry[2][0] == ["audio/b.aac"]  # failed key excluded from the delete


# ---------------------------------------------------------------------------
# E3 — session_routing model matches migrations/001 (UUID / TIMESTAMPTZ / indexes)
# ---------------------------------------------------------------------------


def _session_routing_pg_ddl() -> str:
    table = SessionRouting.__table__
    ddl = str(CreateTable(table).compile(dialect=pg_dialect.dialect()))
    return ddl + "".join(str(CreateIndex(i).compile(dialect=pg_dialect.dialect())) for i in table.indexes)


def test_session_routing_timestamps_are_timestamptz_on_postgres():
    """E3: create_all() must render TIMESTAMPTZ, not the old naive TIMESTAMP."""
    ddl = _session_routing_pg_ddl()
    assert "TIMESTAMP WITH TIME ZONE" in ddl
    assert "WITHOUT TIME ZONE" not in ddl
    assert "VARCHAR(255)" in ddl


def test_session_routing_pk_uses_the_shared_uuid_helper():
    """E3: the PK is the UUID-on-PG/String-on-sqlite column, not a plain String(36)."""
    from app.models.generator_job import _make_uuid_column

    expected = _make_uuid_column()
    actual = SessionRouting.__table__.columns["session_id"]
    assert type(actual.type) is type(expected.type)
    assert actual.primary_key is True
    assert actual.default is not None  # helper-generated uuid4 default


def test_uuid_helper_yields_native_uuid_for_postgres_only(monkeypatch):
    """E3: the helper the model relies on really is dialect-dependent."""
    from app.models.generator_job import _make_uuid_column

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    assert isinstance(_make_uuid_column().type, pg_dialect.UUID)

    monkeypatch.setenv("DATABASE_URL", "sqlite:///local.db")
    assert isinstance(_make_uuid_column().type, sqlalchemy.String)


def test_session_routing_declares_migration_indexes():
    """E3: idx_session_routing_server / _heartbeat back the affinity lookups."""
    names = {i.name for i in SessionRouting.__table__.indexes}
    assert {"idx_session_routing_server", "idx_session_routing_heartbeat"} <= names


def test_session_routing_still_creates_on_sqlite():
    """E3: the sqlite test fallback keeps working (String(36) PK variant)."""
    pk_type = SessionRouting.__table__.columns["session_id"].type
    if isinstance(pk_type, pg_dialect.UUID):
        pytest.skip("model imported with a PostgreSQL DATABASE_URL")
    engine = create_engine("sqlite://")
    SessionRouting.__table__.create(engine)
    assert "session_routing" in inspect(engine).get_table_names()


# ---------------------------------------------------------------------------
# E4 — ffmpeg death clears _running so is_running tells the truth
# ---------------------------------------------------------------------------


class DeadFfmpeg:
    """Popen stand-in whose process has already exited."""

    returncode = 1
    stdin = MagicMock()

    def poll(self):
        return self.returncode


def _icecast_with_dead_ffmpeg(monkeypatch):
    from app.framework.framework_icecast import IcecastStreamer

    monkeypatch.setattr("app.framework.framework_icecast.subprocess.Popen", lambda *a, **k: DeadFfmpeg())
    streamer = IcecastStreamer(host="h", port=1, password="p")
    streamer._running = True
    streamer._pcm_queue.put(b"\x00" * 8)  # first chunk (starts ffmpeg)
    streamer._pcm_queue.put(b"\x00" * 8)  # second chunk -> poll() shows the exit
    return streamer


def test_ffmpeg_death_clears_running_state(monkeypatch):
    """E4: after ffmpeg exits the streamer reports down and drops fed PCM."""
    streamer = _icecast_with_dead_ffmpeg(monkeypatch)
    streamer._stream_loop()

    assert streamer.is_running is False
    assert streamer.is_connected is False
    streamer.feed_pcm(b"\x00" * 8)
    assert streamer._pcm_queue.empty()


def test_ffmpeg_death_allows_restart(monkeypatch):
    """E4: start() is no longer refused by a stale 'already running' flag."""
    streamer = _icecast_with_dead_ffmpeg(monkeypatch)
    streamer._stream_loop()

    live = MagicMock()
    live.poll.return_value = None
    live.stdin = MagicMock()
    monkeypatch.setattr("app.framework.framework_icecast.subprocess.Popen", lambda *a, **k: live)

    streamer.start()
    assert streamer.is_running is True
    streamer.stop()


# ---------------------------------------------------------------------------
# E5 — docker compose restart is bounded
# ---------------------------------------------------------------------------


def test_restart_services_passes_timeout(monkeypatch):
    """E5: subprocess.run gets a timeout so a wedged daemon cannot hang the loop."""
    import app.onboarding as onboarding

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(kwargs)
        return MagicMock()

    monkeypatch.setattr(onboarding.subprocess, "run", fake_run)
    onboarding.restart_services()

    assert calls and calls[0]["timeout"] == onboarding._RESTART_TIMEOUT_SECONDS
    assert onboarding._RESTART_TIMEOUT_SECONDS <= 30


def test_restart_services_survives_timeout(monkeypatch, caplog):
    """E5: a wedged docker daemon logs and returns instead of propagating."""
    import app.onboarding as onboarding

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["docker", "compose"], timeout=15)

    monkeypatch.setattr(onboarding.subprocess, "run", hang)
    onboarding.restart_services()  # must not raise

    assert "did not finish within" in caplog.text


# ---------------------------------------------------------------------------
# E6 / E7 — cleanup shutdown event + bounded one-shot pool
# ---------------------------------------------------------------------------


def _cleanup_config(interval: float = 3600.0) -> CleanupConfig:
    return CleanupConfig(pg_dsn="postgresql://u:p@localhost/db", garage=MagicMock(), cleanup_interval=interval)


async def test_stop_interrupts_the_idle_wait(monkeypatch):
    """E6: stop() during the idle wait finishes start() immediately, not 3600 s later."""
    import app.cleanup as cleanup_module

    monkeypatch.setattr(cleanup_module.asyncpg, "create_pool", AsyncMock(return_value=_pool(FakeConnection())))
    monkeypatch.setattr(cleanup_module, "create_garage_client_from_env", lambda: FakeGarage())
    cleanup = JobExpirationCleanup(_cleanup_config())
    cleanup._run_cleanup = AsyncMock(return_value=0)

    task = asyncio.create_task(cleanup.start())
    await asyncio.sleep(0.05)
    cleanup.stop()
    await asyncio.wait_for(task, timeout=2.0)  # times out (fails) without the fix
    assert cleanup.running is False


async def test_one_shot_cleanup_pool_has_command_timeout(monkeypatch):
    """E7: the cron entry point no longer opens the only unbounded pool."""
    import app.cleanup as cleanup_module

    captured = {}

    async def fake_create_pool(dsn, **kwargs):
        captured.update(kwargs)
        return _pool(FakeConnection())

    monkeypatch.setattr(cleanup_module.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(cleanup_module, "create_garage_client_from_env", lambda: FakeGarage())
    for name in ("GARAGE_ENDPOINT", "GARAGE_ACCESS_KEY", "GARAGE_SECRET_KEY", "GARAGE_BUCKET"):
        monkeypatch.setenv(name, "x")

    cleanup = JobExpirationCleanup(_cleanup_config())
    cleanup._run_cleanup = AsyncMock(return_value=0)
    monkeypatch.setattr(cleanup_module, "JobExpirationCleanup", lambda config: cleanup)

    assert await cleanup_expired_jobs_once("postgresql://u:p@localhost/db") == 0
    assert captured.get("command_timeout") == _POOL_COMMAND_TIMEOUT


# ---------------------------------------------------------------------------
# E8 — postprocess rewrite: no destructive truncate, explicit >4GiB error
# ---------------------------------------------------------------------------


def _make_wav(path: str, frames: int = 44100) -> None:
    import wave

    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(44100)
        wf.writeframes(b"\x00\x01\x02\x03" * frames)


def test_embed_metadata_failure_leaves_original_wav_intact(tmp_path, monkeypatch):
    """E8: the vestigial wave.open('wb') used to truncate the recording in place."""
    wav_path = str(tmp_path / "show.wav")
    _make_wav(wav_path, frames=1000)
    before = os.path.getsize(wav_path)

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(recording_metadata, "_write_wav_with_metadata", boom)
    with pytest.raises(RuntimeError):
        recording_metadata.embed_wav_metadata(wav_path, {"title": "t"}, [{"index": 1, "timestamp": 0.0}])

    assert os.path.getsize(wav_path) == before  # 44-byte header without the fix
    _assert_readable_wav(wav_path)


def test_write_wav_rejects_more_than_4gib():
    """E8: >4GiB raises ValueError naming the size instead of struct.error."""
    oversize = 2**32
    with pytest.raises(ValueError, match=str(oversize)):
        recording_metadata._ensure_wav_sizes_fit(data_size=oversize, riff_size=oversize + 40)


def test_write_wav_stays_readable_round_trip(tmp_path):
    """E8: the hand-rolled writer still produces a readable WAV."""
    wav_path = str(tmp_path / "out.wav")
    _make_wav(wav_path, frames=500)
    recording_metadata.embed_wav_metadata(
        wav_path,
        {"title": "Show", "artist": "MC"},
        [{"index": 1, "timestamp": 0.0, "title": "Ch1", "reasoning": "why"}],
    )
    _assert_readable_wav(wav_path)


def _assert_readable_wav(path: str) -> None:
    import wave

    with wave.open(path, "rb") as wf:
        assert wf.getnframes() > 0


# ---------------------------------------------------------------------------
# E9 — CUE escaping
# ---------------------------------------------------------------------------


def _write_cue(tmp_path, title, reasoning):
    cue_path = str(tmp_path / "show.cue")
    recording_metadata.write_cue_sheet(
        cue_path,
        "show.wav",
        [{"index": 1, "timestamp": 0.0, "title": 'Loop 1 "hot"', "reasoning": reasoning}],
        title=title,
    )
    # newline="" keeps the CRLF separators (and any stray CR inside a field) visible.
    return open(cue_path, encoding="utf-8", newline="").read()


def test_cue_sheet_escapes_quotes_and_newlines(tmp_path):
    """E9: quotes are escaped and REM comments can no longer span lines."""
    content = _write_cue(tmp_path, 'Bad " Title', "line one\r\nline two " + '"quoted"')
    lines = [line for line in content.split("\r\n") if line]

    assert 'TITLE "Bad \\" Title"' in content
    assert "REM COMMENT line one line two" in content  # CR/LF collapsed onto one line
    assert sum(1 for line in lines if line.startswith("    REM")) == 1
    for line in lines:
        unescaped = line.count('"') - line.count('\\"')
        assert unescaped % 2 == 0, f"unbalanced quotes: {line}"


def test_cue_sheet_track_titles_are_balanced(tmp_path):
    """E9: chapter titles (LLM-derived) cannot break out of their quoted field."""
    content = _write_cue(tmp_path, "Show", "reasoning")
    assert 'TITLE "Loop 1 \\"hot\\""' in content


# ---------------------------------------------------------------------------
# E10 — LIST/adtl sub-chunks parse with a strict RIFF walker
# ---------------------------------------------------------------------------


def _walk_riff(payload: bytes) -> list[tuple[str, bytes]]:
    """Strict chunk walker: fourcc + uint32 size + (padded) payload, must end at EOF."""
    chunks, offset = [], 0
    while offset < len(payload):
        assert offset + 8 <= len(payload), f"truncated chunk header at {offset}"
        fourcc = payload[offset : offset + 4]
        (size,) = struct.unpack("<I", payload[offset + 4 : offset + 8])
        body = payload[offset + 8 : offset + 8 + size]
        assert len(body) == size, f"chunk {fourcc} claims {size} bytes, only {len(body)} left"
        chunks.append((fourcc.decode("ascii"), body))
        offset += 8 + size + (size % 2)
    assert offset == len(payload), "walker desynced: did not land on the end of the payload"
    return chunks


def test_chapter_list_chunk_uses_real_sub_chunk_headers():
    """E10: LIST/adtl now contains proper labl/note chunks, not a bare id prefix."""
    blob = recording_metadata._build_chapter_list_chunk([{"title": "Ch1", "reasoning": "why"}])
    assert blob[:4] == b"LIST" and blob[8:12] == b"adtl"

    sub_chunks = _walk_riff(blob[12:])
    assert [fourcc for fourcc, _ in sub_chunks] == ["labl", "note"]
    (cue_id,) = struct.unpack("<I", sub_chunks[0][1][:4])
    assert cue_id == 1
    assert sub_chunks[0][1][4:].rstrip(b"\x00") == b"Ch1"


def test_embedded_wav_list_chunks_walk_cleanly(tmp_path):
    """E10: every chunk written between RIFF and data is parseable in sequence."""
    wav_path = str(tmp_path / "chapters.wav")
    _make_wav(wav_path, frames=500)
    recording_metadata.embed_wav_metadata(
        wav_path,
        {"title": "Show"},
        [{"index": 1, "timestamp": 0.0, "title": "Ch1", "reasoning": "why"}],
    )
    raw = open(wav_path, "rb").read()
    assert raw[:4] == b"RIFF"
    chunks = _walk_riff(raw[12:])
    assert [fourcc for fourcc, _ in chunks] == ["fmt ", "LIST", "cue ", "LIST", "data"]


def test_adtl_sizes_are_read_at_end_of_file():
    """Sanity: struct.pack('<I') on a >4GiB length is what E8 replaced with ValueError."""
    with pytest.raises(struct.error):
        struct.pack("<I", 2**32)
