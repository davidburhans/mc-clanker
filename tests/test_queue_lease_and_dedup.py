"""
Regression tests for the job-queue / data-integrity fixes:

- B2/C2  : job lease + stale-'processing' reaper (no more orphaned jobs)
- A7/C6  : atomic UPDATE+NOTIFY in one transaction; post-subscribe status recheck
- A9     : asyncpg DSN prefers DATABASE_URL and strips '+driver' suffix
- C3     : model schema reconciled with the migration (TIMESTAMPTZ, JSONB, indexes)
- C5     : uploaded object deleted when the DB commit fails (no orphaned S3)
- C8     : content_hash column/index foundation for dedup
- B6     : hung generation is bounded by a timeout

The GPU stack (torch/stable-audio-tools) is stubbed via a *session fixture* (not
module-level) so that tests/test_worker.py -- which skips at collection time when
torch is absent -- still skips cleanly: collection runs before this fixture
injects its stub, so app.worker is only importable inside these tests.
"""

import asyncio
import sys
import types
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import DateTime

from app.cleanup import JobExpirationCleanup
from app.job_waiter import JobWaiter, _normalize_dsn, _resolve_asyncpg_dsn
from app.models.generator_job import GeneratorJob

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def worker_module():
    """Import app.worker with the GPU generator module stubbed out."""
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
    config = worker_module.WorkerConfig(
        worker_id="test-worker",
        pg_dsn="postgresql://u:p@localhost/db",
        garage=MagicMock(),
    )
    return worker_module.GeneratorWorker(config)


# ---------------------------------------------------------------------------
# B2 / C2 - lease + stale-'processing' reclaim
# ---------------------------------------------------------------------------


async def test_claim_reclaims_stale_processing_and_sets_lease(worker_module):
    worker = _make_worker(worker_module)
    conn = _make_conn()
    sample = {"id": uuid.uuid4(), "instrument": "pad", "status": "pending"}
    conn.fetchrow = AsyncMock(return_value=sample)
    worker.db = _pool_yielding(conn)

    job = await worker._claim_next_job()

    select_sql = conn.fetchrow.call_args[0][0]
    assert "status = 'pending'" in select_sql
    assert "status = 'processing' AND lease_expires_at < NOW()" in select_sql
    assert "FOR UPDATE SKIP LOCKED" in select_sql

    # The claim UPDATE runs in the same transaction and starts the lease.
    update_sql, worker_id, lease_expiry, claimed_id = conn.execute.call_args[0]
    assert "lease_expires_at = $2" in update_sql
    assert worker_id == "test-worker"
    assert lease_expiry.tzinfo is not None  # timezone-aware, matches TIMESTAMPTZ
    assert claimed_id == sample["id"]
    assert job == sample


async def test_claim_returns_none_when_queue_empty(worker_module):
    worker = _make_worker(worker_module)
    conn = _make_conn()
    conn.fetchrow = AsyncMock(return_value=None)
    worker.db = _pool_yielding(conn)
    assert await worker._claim_next_job() is None


# ---------------------------------------------------------------------------
# A7 / C6 - atomic UPDATE+NOTIFY
# ---------------------------------------------------------------------------


async def test_mark_complete_is_atomic_and_notifies(worker_module):
    worker = _make_worker(worker_module)
    conn = _make_conn()
    worker.db = _pool_yielding(conn)

    job_id = uuid.uuid4()
    await worker._mark_job_complete(job_id, "audio/x.aac", 4.5)

    conn.transaction.assert_called_once()  # UPDATE + NOTIFY share one transaction
    sqls = [c[0][0] for c in conn.execute.call_args_list]
    assert any("status = 'completed'" in s for s in sqls)
    notify = [c for c in conn.execute.call_args_list if "pg_notify" in c[0][0]]
    assert len(notify) == 1
    assert notify[0][0][1] == str(job_id)  # payload parameterized, not f-string


async def test_mark_failed_releases_lease(worker_module):
    worker = _make_worker(worker_module)
    conn = _make_conn()
    worker.db = _pool_yielding(conn)
    await worker._mark_job_failed(uuid.uuid4(), "boom")
    sql = conn.execute.call_args[0][0]
    assert "status = 'failed'" in sql
    assert "lease_expires_at = NULL" in sql


# ---------------------------------------------------------------------------
# C5 - orphan cleanup on DB-commit failure
# ---------------------------------------------------------------------------


async def test_delete_orphan_audio_calls_garage(worker_module):
    worker = _make_worker(worker_module)
    worker.garage = MagicMock()
    worker.garage.delete_object = AsyncMock()
    await worker._delete_orphan_audio("audio/x.aac")
    worker.garage.delete_object.assert_awaited_once_with("audio/x.aac")


async def test_process_claimed_job_cleans_orphan_when_complete_fails(worker_module, monkeypatch):
    worker = _make_worker(worker_module)
    worker.garage = MagicMock()
    worker.garage.delete_object = AsyncMock()
    job = {"id": uuid.uuid4(), "instrument": "x", "model_id": "m", "prompt": "p"}

    monkeypatch.setattr(worker, "_generate_with_lease", AsyncMock(return_value=("audio/x.aac", 4.0)))
    monkeypatch.setattr(worker, "_mark_job_complete", AsyncMock(side_effect=RuntimeError("db down")))
    monkeypatch.setattr(worker, "_mark_job_failed", AsyncMock())

    await worker._process_claimed_job(job)

    worker.garage.delete_object.assert_awaited_once_with("audio/x.aac")
    worker._mark_job_failed.assert_awaited_once()
    assert worker.jobs_failed == 1
    assert worker.jobs_processed == 0


# ---------------------------------------------------------------------------
# B6 - generation timeout
# ---------------------------------------------------------------------------


async def test_generate_with_lease_times_out(worker_module, monkeypatch):
    monkeypatch.setattr(worker_module, "GENERATION_TIMEOUT_SECONDS", 0.05)
    worker = _make_worker(worker_module)

    async def slow_generate(job):
        await asyncio.sleep(10)
        return ("audio/x", 1.0)

    monkeypatch.setattr(worker, "_generate_and_upload", slow_generate)
    with pytest.raises(asyncio.TimeoutError):
        await worker._generate_with_lease({"id": uuid.uuid4()})


# ---------------------------------------------------------------------------
# B2 reaper in cleanup
# ---------------------------------------------------------------------------


async def test_reap_stale_processing_marks_failed():
    cleanup = JobExpirationCleanup(MagicMock())
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=[{"id": uuid.uuid4()}])
    cleanup.db = _pool_yielding(conn)

    reaped = await cleanup._reap_stale_processing()

    assert reaped == 1
    sql = conn.fetch.call_args[0][0]
    assert "status = 'processing'" in sql
    assert "lease_expires_at < NOW()" in sql
    assert "status = 'failed'" in sql


async def test_run_cleanup_reaps_then_deletes():
    cleanup = JobExpirationCleanup(MagicMock())
    conn = _make_conn()

    async def fake_fetch(sql, *args):
        if "status = 'processing'" in sql:
            return [{"id": uuid.uuid4()}, {"id": uuid.uuid4()}]  # 2 reaped
        return [{"audio_path": "audio/expired.aac"}]  # 1 expired deleted

    conn.fetch = AsyncMock(side_effect=fake_fetch)
    cleanup.db = _pool_yielding(conn)
    cleanup.garage = MagicMock()
    cleanup.garage.delete_object = AsyncMock()

    total = await cleanup._run_cleanup()

    assert total == 3  # 2 reaped + 1 deleted
    cleanup.garage.delete_object.assert_awaited_once_with("audio/expired.aac")


# ---------------------------------------------------------------------------
# A7 - post-subscribe status recheck
# ---------------------------------------------------------------------------


async def test_waiter_rechecks_status_after_subscribing():
    # First (pre-listen) check: still processing. Post-subscribe recheck: completed.
    states = iter(
        [
            {"status": "processing"},
            {"status": "completed", "audio_path": "audio/a.aac"},
        ]
    )
    waiter = JobWaiter(db_pool=MagicMock())

    async def fake_get(job_id):
        try:
            return next(states)
        except StopIteration:
            return None

    waiter._get_job = fake_get
    conn = AsyncMock()
    conn.add_listener = AsyncMock()
    conn.remove_listener = AsyncMock()
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=conn)
    pool.release = AsyncMock()
    waiter.db_pool = pool

    result = await waiter.wait_for_job_completion(uuid.uuid4(), timeout=1.0)

    assert result == "audio/a.aac"
    conn.add_listener.assert_awaited_once()  # subscribed...
    # ...and never had to wait the full timeout because of the post-subscribe recheck.


# ---------------------------------------------------------------------------
# A9 - DSN resolution
# ---------------------------------------------------------------------------


def test_normalize_dsn_strips_driver_suffix():
    assert _normalize_dsn("postgresql+psycopg2://u:p@h/db") == "postgresql://u:p@h/db"
    assert _normalize_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"
    assert _normalize_dsn("postgres://u:p@h/db") == "postgres://u:p@h/db"


def test_resolve_dsn_prefers_env_over_engine(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://env:env@host/envdb")
    dbm = MagicMock()
    dbm.engine.url.render_as_string.return_value = "postgresql+psycopg2://other@other/other"
    assert _resolve_asyncpg_dsn(dbm) == "postgresql://env:env@host/envdb"


def test_resolve_dsn_falls_back_to_engine_and_normalizes(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    dbm = MagicMock()
    dbm.engine.url.render_as_string.return_value = "postgresql+psycopg2://u:p@h/db"
    assert _resolve_asyncpg_dsn(dbm) == "postgresql://u:p@h/db"


def test_resolve_dsn_none_when_nothing_available(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert _resolve_asyncpg_dsn(None) is None


# ---------------------------------------------------------------------------
# C3 / C8 - model reconciliation
# ---------------------------------------------------------------------------


def test_model_timestamps_are_timezone_aware():
    cols = GeneratorJob.__table__.columns
    for name in ("created_at", "started_at", "completed_at", "expires_at", "lease_expires_at"):
        col_type = cols[name].type
        assert isinstance(col_type, DateTime), name
        assert col_type.timezone is True, name


def test_model_has_lease_and_content_hash_and_indexes():
    cols = GeneratorJob.__table__.columns
    assert "lease_expires_at" in cols
    assert "content_hash" in cols
    index_names = {i.name for i in GeneratorJob.__table__.indexes}
    assert "idx_generator_jobs_claiming" in index_names
    assert "idx_generator_jobs_lease_reclaim" in index_names
    assert "idx_generator_jobs_active" in index_names


def test_model_has_status_check_constraint():
    constraint_names = {c.name for c in GeneratorJob.__table__.constraints}
    assert "ck_generator_jobs_status" in constraint_names
