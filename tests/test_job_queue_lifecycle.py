"""REL-12 (U6 ``rel-job-queue``) regression tests: no more immortal pending jobs.

Three coupled defects, three fixes, one suite (TDD red — every case here fails
against the pre-``rel-12-queue`` code for the missing hook it names):

- REL-12a — the loop (and the pregen mirror path) abandon a job after
  600 s + 30 s grace and move on; the row stays ``pending`` forever while the
  worker's FIFO claim still generates it. Fix: after the grace pass both paths
  terminal-fail still-pending rows via a new ``JobQueuePort.abandon_jobs``
  (``error_message='loop_abandoned'``, guarded to ``status='pending'`` — never
  a claimed/running row).
- REL-12b — ``JobExpirationCleanup`` reaps only stale ``processing`` rows and
  deletes only terminal rows, so pending rows submitted outside a loop (API)
  or orphaned by a web crash sit forever. Fix: a new ``_reap_stale_pending``
  pass behind the U5 ``_run_pass`` isolation + a ``pending_grace_seconds``
  knob (default 86400, ``0`` disables).
- REL-12c — both submit paths INSERT unconditionally, so a slow worker grows
  the queue without bound. Fix: ``JobQueuePort.pending_depth()`` gauge; when
  depth exceeds ``JOB_PENDING_DEPTH_LIMIT`` the whole submit phase is skipped
  and logged (never blocked), and the skipped stems report the documented
  ``"failed"`` audit outcome so ``applied_actions`` stays truthful; the prompts
  stay cache-missed and retry next loop.

Case map (plan §3.1):

======  ====================================================================
T1      P8 abandons only the batch's unreported jobs (loop path, acceptance)
T2      an abandon failure never breaks the loop (best-effort, decision 3)
T3      ``_abandon_jobs`` delegate routes through the injected port
T4      P7 skip-and-log throttle engages over the bound (acceptance)
T5      throttle disengages after drain; prompt stays cache-missed (acceptance)
T6      a failed depth probe fails open (decision 6)
T7      throttled stems report ``"failed"``, never ``"cached"`` (decision 9)
T8      unthrottled P7 result shape unchanged; REL-06 cache-hit refresh intact
T9      pregeneration abandons its losers too (scout risk #4, acceptance)
T10     pregeneration throttles when backlogged
T11     the port declares both new members; adapter satisfies the grown port
T12     ``abandon_generator_jobs`` fails ONLY pending rows (acceptance,
        no-running-job pin: the ``processing`` row is never touched)
T13     abandon is idempotent by predicate and empty-safe (decision 2)
T14a    ``count_pending_jobs`` counts only pending rows
T14b    adapter ``abandon_jobs``/``pending_depth`` delegate to module globals
T15     stale-pending reaper SQL pins ``pending``-only + grace arg (acceptance)
T16     ``pending_grace_seconds=0`` disables the reaper (zero SQL)
T17     the pending-reaper pass is error-isolated inside ``_run_cleanup``
T18     ``PENDING_GRACE_SECONDS`` env plumbing (shared kwargs constructor)
======  ====================================================================

Fakes are named classes (no inline stubs): ``_FakeRel12JobQueue`` stands in for
the ``JobQueuePort``, ``_SqliteJobStore`` stands in for ``DatabaseManager`` with
a tmp-file SQLite engine holding real ``GeneratorJob`` rows (the conftest
singleton reset keeps tests isolated), and the cleanup tests reuse the
``test_queue_lease_and_dedup`` fake-pool shape.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.cleanup import CleanupConfig, JobExpirationCleanup, _retention_kwargs
from app.db import Base, DatabaseManager
from app.framework import job_queue as jq_mod
from app.framework import loop_steps, pregeneration
from app.framework.audit_recording import _audit_applied_actions
from app.framework.domain_audio import make_cache_key
from app.framework.framework_main_async import AsyncFrameworkLoop
from app.framework.framework_state import state
from app.framework.job_queue import PostgresJobQueueAdapter
from app.framework.ports import JobQueuePort
from app.models.generator_job import GeneratorJob

# ---------------------------------------------------------------------------
# Named fakes
# ---------------------------------------------------------------------------


class _FakeRel12JobQueue:
    """In-memory JobQueuePort stand-in (U6): records abandoned batches and
    submits, awaits through a canned result, and answers the depth gauge from
    ``self.depth`` (an int) or raises ``self.probe_error`` when set."""

    def __init__(
        self,
        *,
        await_result: dict[UUID, str | None] | None = None,
        depth: int = 0,
        probe_error: Exception | None = None,
        fail_abandon: bool = False,
    ) -> None:
        self.submitted: list[dict[str, object]] = []
        self.awaited: list[dict[str, object]] = []
        self.abandoned: list[list[UUID]] = []
        self.depth_probes = 0
        self._await_result: dict[UUID, str | None] = {} if await_result is None else await_result
        self.depth = depth
        self.probe_error = probe_error
        self.fail_abandon = fail_abandon

    async def submit(self, **kwargs: object) -> UUID:
        self.submitted.append(kwargs)
        return uuid4()

    async def await_jobs(self, job_ids: list[UUID], timeout: float = 120.0) -> dict[UUID, str | None]:
        self.awaited.append({"job_ids": list(job_ids), "timeout": timeout})
        return self._await_result

    async def abandon_jobs(self, job_ids: list[UUID]) -> int:
        """Terminal-fail the batch — or raise when ``fail_abandon`` is set."""
        if self.fail_abandon:
            raise RuntimeError("abandon down")
        self.abandoned.append(list(job_ids))
        return len(job_ids)

    async def pending_depth(self) -> int:
        self.depth_probes += 1
        if self.probe_error is not None:
            raise self.probe_error
        return self.depth


class _SqliteJobStore:
    """Named DatabaseManager stand-in: a tmp-file SQLite engine with the real
    GeneratorJob schema. Mirrors the interface the job-queue module touches
    (``get_instance`` / ``session()`` / ``create_tables``); counts session
    opens so the empty-abandon fast path is observable."""

    def __init__(self, db_path) -> None:
        self.engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.sessions_opened = 0

    def create_tables(self) -> None:
        Base.metadata.create_all(bind=self.engine)

    @contextmanager
    def session(self):
        self.sessions_opened += 1
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


class _JobRow(NamedTuple):
    """The columns the row-level pins read back from the store."""

    status: str
    error_message: str | None
    completed_at: datetime | None
    worker_id: str | None


def _install_sqlite_store(monkeypatch, tmp_path) -> _SqliteJobStore:
    """Route DatabaseManager.get_instance() at a fresh tmp-file store."""
    store = _SqliteJobStore(tmp_path / "rel12_jobs.db")
    store.create_tables()
    monkeypatch.setattr(DatabaseManager, "_instance", store)
    return store


def _seed_job(
    store: _SqliteJobStore,
    *,
    status: str,
    worker_id: str | None = None,
    audio_path: str | None = None,
    lease_minutes: int | None = None,
) -> str:
    """Insert one real GeneratorJob row; returns its (str) id."""
    now = datetime.now(timezone.utc)
    job_id = str(uuid4())
    with store.session() as session:
        session.add(
            GeneratorJob(
                id=job_id,
                session_id=str(uuid4()),
                instrument="Synth Pad",
                prompt="Synth Pad, A minor, 128 BPM",
                status=status,
                worker_id=worker_id,
                audio_path=audio_path,
                lease_expires_at=now + timedelta(minutes=lease_minutes) if lease_minutes else None,
                expires_at=now + timedelta(hours=24),
            )
        )
    return job_id


def _read_job(store: _SqliteJobStore, job_id: str) -> _JobRow:
    with store.session() as session:
        row = session.get(GeneratorJob, job_id)
        assert row is not None, f"seeded job {job_id} vanished"
        return _JobRow(
            status=row.status,
            error_message=row.error_message,
            completed_at=row.completed_at,
            worker_id=row.worker_id,
        )


def _pool_yielding(conn) -> MagicMock:
    """An asyncpg-like pool whose ``async with pool.acquire() as c:`` yields conn."""
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return pool


def _make_conn() -> MagicMock:
    """An asyncpg-like connection (test_queue_lease_and_dedup shape)."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock()
    conn.fetch = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock()
    return conn


def _uncached_stem(prompt: str) -> dict[str, Any]:
    return {"prompt": prompt, "bars": 4, "model_id": "foundation-1", "_original_details": {}}


def _pregen_snapshot() -> dict[str, Any]:
    return {
        "current_bpm": 128,
        "current_key": "A minor",
        "active_stems": [],
        "user_override": "",
        "available_instruments": [],
        "stem_history": [],
        "llm_config": {"base_url": "http://x:1234/v1", "api_key": "k", "model": "m"},
    }


def _pregen_add_response() -> dict[str, Any]:
    return {
        "master_bpm": 128,
        "master_key": "A minor",
        "actions": [
            {
                "action_type": "add",
                "sub_family": "Synth Pad",
                "major_family": "Synth",
                "model_id": "foundation-1",
                "timbre_tags": ["warm"],
                "notation_tag": "melody",
                "fx_tag": "dry",
                "bars": 4,
            }
        ],
        "reasoning": "add a pad",
        "name": "Pad Set",
    }


def _loop_with_fake_jobs(**fake_kwargs: Any) -> tuple[AsyncFrameworkLoop, _FakeRel12JobQueue]:
    loop = AsyncFrameworkLoop(uuid4())
    fake = _FakeRel12JobQueue(**fake_kwargs)
    loop._jobs = fake  # type: ignore[assignment]
    return loop, fake


# ---------------------------------------------------------------------------
# REL-12a — the loop terminal-abandons its losers after the grace pass
# ---------------------------------------------------------------------------


async def test_await_jobs_fetch_abandons_expired_batch_jobs(monkeypatch):
    """T1 (acceptance): P8 with one completed + one unreported job abandons ONLY
    the unreported job id and reports it 'failed'."""

    monkeypatch.setattr(loop_steps, "JOB_LATE_COMPLETION_GRACE_SECONDS", 0.01)  # grace pass is instant
    loop, fake = _loop_with_fake_jobs()
    loop._loop_idx = 3
    job0, job1 = uuid4(), uuid4()
    loop._await_jobs = AsyncMock(return_value={job0: "audio/a.aac", job1: None})
    loop._fetch_audio = AsyncMock(return_value=np.zeros((4, 2), dtype=np.float32))
    stems = [_uncached_stem("Pad A"), _uncached_stem("Pad B")]

    with patch.object(state, "cache_stem"):
        outcomes = await loop._step_await_jobs_fetch([(job0, 0, "k0"), (job1, 1, "k1")], stems)

    assert outcomes == {0: "generated", 1: "failed"}
    assert fake.abandoned == [[job1]], "only the unreported (still-pending) job may be abandoned"


async def test_abandon_failure_never_breaks_the_loop():
    """T2 (decision 3): a raising port must not kill P8 — hygiene, not playback."""
    loop, fake = _loop_with_fake_jobs(fail_abandon=True)
    job = uuid4()
    loop._await_jobs = AsyncMock(return_value={job: None})
    stems = [_uncached_stem("Pad A")]

    outcomes = await loop._step_await_jobs_fetch([(job, 0, "k0")], stems)

    assert outcomes == {0: "failed"}


async def test_abandon_delegate_routes_through_injected_port():
    """T3 (hex seam): ``loop._abandon_jobs`` delegates to the injected port."""
    loop, fake = _loop_with_fake_jobs()
    job = uuid4()

    count = await loop._abandon_jobs([job])

    assert count == 1
    assert fake.abandoned == [[job]]


# ---------------------------------------------------------------------------
# REL-12c — submission backpressure (skip-and-log, fail-open, never block)
# ---------------------------------------------------------------------------


async def test_submit_throttles_when_pending_depth_over_limit():
    """T4 (acceptance): depth over the bound skips the WHOLE submit phase."""

    loop, fake = _loop_with_fake_jobs(depth=loop_steps.JOB_PENDING_DEPTH_LIMIT + 1)
    loop._submit_job = AsyncMock(return_value=uuid4())
    stems = [_uncached_stem("Pad A"), _uncached_stem("Pad B")]

    result = await loop._step_submit_jobs(stems, 128, "A minor")

    assert result.pending_jobs == []
    assert result.skipped_idxs == [0, 1]
    loop._submit_job.assert_not_awaited(), "a throttled phase submits nothing"
    assert fake.depth_probes == 1, "exactly one depth probe per submit phase"


async def test_submit_throttle_disengages_after_drain():
    """T5 (acceptance): once the worker drains below the bound, submission
    resumes and the skipped prompt stayed cache-missed for the retry."""

    loop, fake = _loop_with_fake_jobs(depth=loop_steps.JOB_PENDING_DEPTH_LIMIT + 1)
    job_id = uuid4()
    loop._submit_job = AsyncMock(return_value=job_id)
    stems = [_uncached_stem("Pad A")]
    cache_key = make_cache_key("foundation-1", "Pad A", 128, "A minor", 4)

    first = await loop._step_submit_jobs(stems, 128, "A minor")
    assert first.pending_jobs == [] and first.skipped_idxs == [0]

    fake.depth = loop_steps.JOB_PENDING_DEPTH_LIMIT - 1  # the worker drained
    second = await loop._step_submit_jobs(stems, 128, "A minor")

    assert second.pending_jobs == [(job_id, 0, cache_key)]
    assert second.skipped_idxs == []
    loop._submit_job.assert_awaited_once()
    assert cache_key not in loop.stem_cache, "the throttle must not poison the cache: retry next loop"


async def test_depth_probe_failure_fails_open():
    """T6 (decision 6): a broken gauge must not stop the set."""
    loop, _fake = _loop_with_fake_jobs(probe_error=RuntimeError("gauge down"))
    loop._submit_job = AsyncMock(side_effect=[uuid4(), uuid4()])
    stems = [_uncached_stem("Pad A"), _uncached_stem("Pad B")]

    result = await loop._step_submit_jobs(stems, 128, "A minor")

    assert result.skipped_idxs == []
    assert [(i, k) for _, i, k in result.pending_jobs] == [
        (0, make_cache_key("foundation-1", "Pad A", 128, "A minor", 4)),
        (1, make_cache_key("foundation-1", "Pad B", 128, "A minor", 4)),
    ]


async def test_throttled_stems_report_failed_not_cached():
    """T7 (decision 9): P8 must seed skipped stems as 'failed' — absent from the
    outcomes map the applied-actions audit would default them to 'cached' (a lie)."""

    loop, _fake = _loop_with_fake_jobs(depth=loop_steps.JOB_PENDING_DEPTH_LIMIT + 1)
    loop._submit_job = AsyncMock(return_value=uuid4())
    stems = [_uncached_stem("Pad A"), _uncached_stem("Pad B")]

    submit = await loop._step_submit_jobs(stems, 128, "A minor")
    outcomes = await loop._step_await_jobs_fetch(submit.pending_jobs, stems, submit.skipped_idxs)

    assert outcomes == {0: "failed", 1: "failed"}
    applied = _audit_applied_actions(stems, outcomes)
    assert [row["outcome"] for row in applied] == ["failed", "failed"]


async def test_submit_unthrottled_shape_unchanged():
    """T8 (regression pin): under the bound P7 still returns (job_id, i,
    cache_key) triples in stem order, cache hits are never submitted, and the
    REL-06 last_used refresh survives the rewrite."""
    loop, _fake = _loop_with_fake_jobs(depth=0)
    job0, job1 = uuid4(), uuid4()
    loop._submit_job = AsyncMock(side_effect=[job0, job1])
    stems = [_uncached_stem("Pad A"), _uncached_stem("Pad B"), _uncached_stem("Cached Pad")]
    hit_key = make_cache_key("foundation-1", "Cached Pad", 128, "A minor", 4)
    stale_used = time.time() - 400
    loop.stem_cache[hit_key] = {"audio_data": np.zeros((4, 2), dtype=np.float32), "last_used": stale_used}

    result = await loop._step_submit_jobs(stems, 128, "A minor")

    assert result.pending_jobs == [
        (job0, 0, make_cache_key("foundation-1", "Pad A", 128, "A minor", 4)),
        (job1, 1, make_cache_key("foundation-1", "Pad B", 128, "A minor", 4)),
    ]
    assert result.skipped_idxs == []
    assert loop.stem_cache[hit_key]["last_used"] > stale_used, "REL-06: a hit must refresh last_used"


# ---------------------------------------------------------------------------
# REL-12a/c — the pregen mirror path (reached through the loop's helpers)
# ---------------------------------------------------------------------------


async def test_pregeneration_abandons_its_losers():
    """T9 (acceptance): the background path must abandon its losers too, or it
    re-leaks the immortal-pending bug; the skipped-outcome rule mirrors P8."""
    loop = AsyncFrameworkLoop(uuid4())
    job_id = uuid4()

    with (
        patch.object(loop, "conductor") as conductor_mock,
        patch.object(loop, "_submit_job", new_callable=AsyncMock, return_value=job_id),
        patch.object(loop, "_await_jobs", new_callable=AsyncMock, return_value={job_id: None}),
        patch.object(loop, "_fetch_audio", new_callable=AsyncMock),
        patch.object(loop, "_abandon_missing_jobs", new_callable=AsyncMock) as abandon_mock,
    ):
        conductor_mock.get_next_state_async = AsyncMock(return_value=_pregen_add_response())
        await pregeneration.run_pregeneration(loop, 2, _pregen_snapshot())

    assert loop._pregen_results is not None, "pregeneration crashed: REL-12 hooks are missing"
    abandon_mock.assert_awaited_once_with([job_id])
    assert loop._pregen_results["stem_outcomes"] == {0: "failed"}
    assert loop._pregen_done.is_set()


async def test_pregeneration_throttles_when_backlogged():
    """T10: the pregen submit phase skip-and-logs over the bound, and the
    skipped stem reports 'failed' exactly like the foreground path."""

    loop = AsyncFrameworkLoop(uuid4())
    fake = _FakeRel12JobQueue(depth=loop_steps.JOB_PENDING_DEPTH_LIMIT + 1)
    loop._jobs = fake  # type: ignore[assignment]

    with (
        patch.object(loop, "conductor") as conductor_mock,
        patch.object(loop, "_submit_job", new_callable=AsyncMock) as submit_mock,
        patch.object(loop, "_fetch_audio", new_callable=AsyncMock),
    ):
        conductor_mock.get_next_state_async = AsyncMock(return_value=_pregen_add_response())
        await pregeneration.run_pregeneration(loop, 2, _pregen_snapshot())

    assert loop._pregen_results is not None, "pregeneration crashed: REL-12 hooks are missing"
    submit_mock.assert_not_awaited()
    assert fake.depth_probes == 1
    assert loop._pregen_results["stem_outcomes"] == {0: "failed"}


# ---------------------------------------------------------------------------
# Port + adapter (hexagonal seams over the new capabilities)
# ---------------------------------------------------------------------------


def test_default_adapter_satisfies_grown_port():
    """T11: the port declares abandon_jobs + pending_depth and the concrete
    adapter structurally satisfies the GROWN protocol."""
    for member in ("abandon_jobs", "pending_depth"):
        assert callable(getattr(JobQueuePort, member)), f"JobQueuePort.{member} missing (REL-12 port growth)"
    assert isinstance(PostgresJobQueueAdapter(), JobQueuePort)


async def test_abandon_generator_jobs_fails_only_pending_rows(monkeypatch, tmp_path):
    """T12 (acceptance, no-running-job pin): the UPDATE terminalizes ONLY
    status='pending' rows — a claimed/running row is never failed."""
    store = _install_sqlite_store(monkeypatch, tmp_path)
    pend_a = _seed_job(store, status="pending")
    pend_b = _seed_job(store, status="pending")
    running = _seed_job(store, status="processing", worker_id="w1", lease_minutes=5)
    done = _seed_job(store, status="completed", audio_path="audio/done.aac")

    count = await jq_mod.abandon_generator_jobs([pend_a, pend_b, running, done])

    assert count == 2
    for job_id in (pend_a, pend_b):
        row = _read_job(store, job_id)
        assert row.status == "failed"
        assert row.error_message == "loop_abandoned"
        assert row.completed_at is not None
    survivor = _read_job(store, running)
    assert survivor.status == "processing", "a claimed/running row must NEVER be failed by abandon"
    assert survivor.worker_id == "w1" and survivor.error_message is None and survivor.completed_at is None
    assert _read_job(store, done).status == "completed"


async def test_abandon_is_idempotent_and_empty_safe(monkeypatch, tmp_path):
    """T13 (decision 2): re-running the abandon matches 0 rows; an empty id
    list opens no session at all."""
    store = _install_sqlite_store(monkeypatch, tmp_path)
    pend = _seed_job(store, status="pending")
    running = _seed_job(store, status="processing", worker_id="w1")
    batch = [pend, running]

    assert await jq_mod.abandon_generator_jobs(batch) == 1
    opened_after_first = store.sessions_opened

    assert await jq_mod.abandon_generator_jobs(batch) == 0, "idempotent: terminal rows are no-ops"
    assert store.sessions_opened > opened_after_first, "the re-run executes (and matches nothing)"
    assert _read_job(store, pend).status == "failed"
    assert _read_job(store, running).status == "processing"

    opened_before_empty = store.sessions_opened
    assert await jq_mod.abandon_generator_jobs([]) == 0
    assert store.sessions_opened == opened_before_empty, "an empty batch must not touch the DB"


async def test_count_pending_jobs_counts_only_pending(monkeypatch, tmp_path):
    """T14a: the gauge counts only 'pending' rows."""
    store = _install_sqlite_store(monkeypatch, tmp_path)
    _seed_job(store, status="pending")
    _seed_job(store, status="pending")
    _seed_job(store, status="processing", worker_id="w1", lease_minutes=5)
    _seed_job(store, status="failed")

    assert await jq_mod.count_pending_jobs() == 2


async def test_adapter_delegates_lifecycle_calls_to_module_functions(monkeypatch):
    """T14b: the adapter's new methods wrap the module globals (the same
    bare-name delegation the submit/await methods use — never recursion)."""
    abandon_sentinel = AsyncMock(return_value=7)
    depth_sentinel = AsyncMock(return_value=9)
    monkeypatch.setattr(jq_mod, "abandon_generator_jobs", abandon_sentinel)
    monkeypatch.setattr(jq_mod, "count_pending_jobs", depth_sentinel)
    adapter = PostgresJobQueueAdapter()
    job = uuid4()

    assert await adapter.abandon_jobs([job]) == 7
    abandon_sentinel.assert_awaited_once_with([job])
    assert await adapter.pending_depth() == 9
    depth_sentinel.assert_awaited_once_with()


# ---------------------------------------------------------------------------
# REL-12b — the stale-pending reaper (JobExpirationCleanup)
# ---------------------------------------------------------------------------


def _cleanup_with_grace(grace_seconds: int) -> JobExpirationCleanup:
    config = CleanupConfig(pg_dsn="postgresql://u:p@localhost/db", garage=MagicMock())
    config.pending_grace_seconds = grace_seconds
    return JobExpirationCleanup(config)


async def test_reap_stale_pending_fails_only_past_threshold():
    """T15 (acceptance): the pass fails pending rows older than the grace with
    the 'queue_backlog_reaped' marker — and NEVER mentions 'processing'."""
    cleanup = _cleanup_with_grace(3600)
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=[{"id": uuid4()}, {"id": uuid4()}])
    cleanup.db = _pool_yielding(conn)

    reaped = await cleanup._reap_stale_pending()

    assert reaped == 2
    sql = conn.fetch.call_args[0][0]
    assert "status = 'pending'" in sql
    assert "created_at < NOW() - make_interval(secs => $1)" in sql
    assert "status = 'failed'" in sql
    assert "COALESCE(error_message" in sql, "a retry must never clobber an existing diagnostic"
    assert "queue_backlog_reaped" in sql
    assert conn.fetch.call_args[0][1] == 3600, "the grace knob is bound as the SQL argument"
    assert "status = 'processing'" not in sql, "the reaper can never fail a claimed/running row"


async def test_reap_stale_pending_disabled_at_zero():
    """T16 (decision 5): pending_grace_seconds=0 disables the pass with zero SQL."""
    cleanup = _cleanup_with_grace(0)
    conn = _make_conn()
    cleanup.db = _pool_yielding(conn)

    assert await cleanup._reap_stale_pending() == 0
    conn.fetch.assert_not_called()
    conn.execute.assert_not_called()


async def test_run_cleanup_pending_reaper_is_error_isolated():
    """T17 (decision 5): the pending-reaper pass IS wired into the cycle, and a
    failing run degrades to 0 — the cycle still reaps stale processing rows and
    deletes expired terminal rows."""
    cleanup = JobExpirationCleanup(MagicMock())
    cleanup.config.pending_grace_seconds = 3600  # enable ONLY the new pass
    conn = _make_conn()

    async def flaky_fetch(sql, *args):
        if "status = 'pending'" in sql:
            raise RuntimeError("pending reaper down")
        if "status = 'processing'" in sql:
            return [{"id": uuid4()}]  # 1 stale-processing reaped
        return [{"audio_path": "audio/expired.aac"}]  # 1 expired terminal deleted

    conn.fetch = AsyncMock(side_effect=flaky_fetch)
    cleanup.db = _pool_yielding(conn)
    cleanup.garage = MagicMock()
    cleanup.garage.delete_object = AsyncMock()

    total = await cleanup._run_cleanup()  # must not raise

    pending_calls = [c for c in conn.fetch.await_args_list if "status = 'pending'" in c.args[0]]
    assert pending_calls, "the stale-pending reaper pass never ran (REL-12b unwired)"
    assert total == 2, "1 processing reaped + 1 expired deleted; the failed pass contributes 0"


def test_env_pending_grace_plumbing(monkeypatch):
    """T18 (decision 4): PENDING_GRACE_SECONDS flows through the SHARED kwargs
    constructor (service + one-shot paths) with the documented _env_int contract."""
    import dataclasses

    assert "pending_grace_seconds" in {f.name for f in dataclasses.fields(CleanupConfig)}
    assert CleanupConfig(pg_dsn="x", garage=MagicMock()).pending_grace_seconds == 86400

    monkeypatch.setenv("PENDING_GRACE_SECONDS", "120")
    assert _retention_kwargs()["pending_grace_seconds"] == 120
    monkeypatch.setenv("PENDING_GRACE_SECONDS", "0")
    assert _retention_kwargs()["pending_grace_seconds"] == 0, "0 disables the reaper"
    monkeypatch.delenv("PENDING_GRACE_SECONDS", raising=False)
    assert _retention_kwargs()["pending_grace_seconds"] == 86400, "unset defaults to the 24 h horizon"
    monkeypatch.setenv("PENDING_GRACE_SECONDS", "garbage")
    assert _retention_kwargs()["pending_grace_seconds"] == 86400, "invalid values fall back (logged)"
