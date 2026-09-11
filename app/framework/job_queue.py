"""Postgres job-queue adapter for the framework loop (Phase 5).

Owns the generator-job submission path so the loop depends on one thin function
instead of reaching into SQLAlchemy + the GeneratorJob model inline. The
``await_jobs`` helper wraps ``app.job_waiter.wait_for_multiple_jobs`` so that
Phase 7b can route both submit + await through a single injected ``JobQueuePort``.

``PostgresJobQueueAdapter`` is the concrete ``JobQueuePort``: the submit path is
now constructor-injected into ``AsyncFrameworkLoop`` (U2). The module functions
below are kept intact — the adapter WRAPS them (do not delete).

NOTE: the foreground ``_run_loop`` and background ``_pre_generate_next_loop``
both now await through the injected ``JobQueuePort`` via the loop's
``_await_jobs`` delegate (U4 — Phase 7b complete). The Gap 4/5/6
characterization tests now patch the loop delegate (``patch.object(loop,
'_await_jobs')``) instead of the module binding. ``PostgresJobQueueAdapter``
wraps this module's ``await_jobs`` (which still wraps
``wait_for_multiple_jobs``), so the real await path is byte-for-byte unchanged.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy import func


async def submit_generator_job(
    *,
    session_id: uuid.UUID,
    instrument: str,
    prompt: str,
    major_family: str,
    model_id: str,
    key: str,
    bpm: int,
    timbre_tags: list[str],
    bars: int,
) -> uuid.UUID:
    """Insert one pending ``GeneratorJob`` row and return its id.

    Row shape: status="pending", expires_at = now + 24h (the worker reaper +
    cleanup rely on these). Lazy-imports the model + DB manager to avoid circular
    imports and so tests that mock the entry point never touch SQLAlchemy.
    """
    from app.db import DatabaseManager
    from app.models.generator_job import GeneratorJob

    db_manager = DatabaseManager.get_instance()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    with db_manager.session() as session:
        job = GeneratorJob(
            session_id=session_id,
            instrument=instrument,
            prompt=prompt,
            major_family=major_family,
            model_id=model_id,
            key=key,
            bpm=bpm,
            timbre_tags=timbre_tags,
            bars=bars,
            status="pending",
            expires_at=expires_at,
        )
        session.add(job)
        session.flush()
        session.refresh(job)
        job_id = job.id

    print(f"[AsyncFrameworkLoop] Submitted job {job_id}: {instrument}")
    # GeneratorJob.id is typed as Column[UUID]; it is a real UUID at runtime after
    # refresh. The Column-vs-UUID narrowing is pre-existing model typing debt
    # (Phase 9) — surfaced here only because the submit path moved into this module.
    return job_id  # type: ignore[return-value]


async def await_jobs(job_ids: Sequence[uuid.UUID], timeout: float = 120.0) -> dict[uuid.UUID, str | None]:
    """Block until the jobs complete; return ``{job_id: audio_path_or_None}``.

    Thin wrapper over the LISTEN/NOTIFY waiter. Now wired into the loop via
    ``_await_jobs`` (U4); the injected ``JobQueuePort`` reuses it.
    """
    from app.job_waiter import wait_for_multiple_jobs

    return await wait_for_multiple_jobs(list(job_ids), timeout=timeout)


async def abandon_generator_jobs(job_ids: Sequence[uuid.UUID | str]) -> int:
    """Fail still-pending rows in ``job_ids`` as 'loop_abandoned' (REL-12a).

    Guarded to status='pending' ONLY: a claimed/running ('processing') row is
    never touched (unit acceptance: no path may fail a running job), and
    already-terminal rows are no-ops, so the call is idempotent and safe to
    retry. Timestamps are Python-side binds — interval arithmetic is not
    portable to the SQLite fallback. Prints when count > 0 (module convention).
    """
    ids = list(job_ids)
    if not ids:
        return 0

    def _abandon_sync() -> int:
        from sqlalchemy import update

        from app.db import DatabaseManager
        from app.models.generator_job import GeneratorJob

        now = datetime.now(timezone.utc)
        db_manager = DatabaseManager.get_instance()
        with db_manager.session() as session:
            stmt = (
                update(GeneratorJob)
                .where(GeneratorJob.id.in_(ids), GeneratorJob.status == "pending")
                .values(
                    status="failed",
                    error_message=func.coalesce(GeneratorJob.error_message, "loop_abandoned"),
                    completed_at=now,
                    expires_at=now + timedelta(hours=1),
                )
                .execution_options(synchronize_session=False)
            )
            return int(session.execute(stmt).rowcount or 0)

    count = await asyncio.to_thread(_abandon_sync)
    if count:
        print(f"[AsyncFrameworkLoop] Abandoned {count} job(s) still pending (loop_abandoned)")
    return count


async def count_pending_jobs() -> int:
    """Number of pending generator jobs — the REL-12c backpressure gauge."""

    def _count_sync() -> int:
        from sqlalchemy import select

        from app.db import DatabaseManager
        from app.models.generator_job import GeneratorJob

        db_manager = DatabaseManager.get_instance()
        with db_manager.session() as session:
            stmt = select(func.count()).select_from(GeneratorJob).where(GeneratorJob.status == "pending")
            return int(session.execute(stmt).scalar_one())

    return await asyncio.to_thread(_count_sync)


class PostgresJobQueueAdapter:
    """Postgres generator-job adapter: wraps the module submit/await functions.

    The only production ``JobQueuePort`` implementation. Construction is a no-op
    (the DB session is opened lazily inside ``submit_generator_job`` at call
    time), so a default ``PostgresJobQueueAdapter()`` may be eagerly stored in
    ``AsyncFrameworkLoop.__init__`` without touching the DB — unlike the audio
    port, there is no env-client / lazy-Garage path to preserve.

    ``await_jobs`` is included and now WIRED into the loop: the loop's
    ``_await_jobs`` delegate routes through ``self._jobs.await_jobs`` (U4 —
    Phase 7b / R14 complete).
    """

    async def submit(
        self,
        *,
        session_id: uuid.UUID,
        instrument: str,
        prompt: str,
        major_family: str,
        model_id: str,
        key: str,
        bpm: int,
        timbre_tags: list[str],
        bars: int,
    ) -> uuid.UUID:
        """Insert one pending ``GeneratorJob`` row and return its id.

        Delegates to the module function so the lazy ``app.db`` import + the
        existing DB-session shape stay byte-for-byte unchanged.
        """
        return await submit_generator_job(
            session_id=session_id,
            instrument=instrument,
            prompt=prompt,
            major_family=major_family,
            model_id=model_id,
            key=key,
            bpm=bpm,
            timbre_tags=timbre_tags,
            bars=bars,
        )

    async def await_jobs(
        self,
        job_ids: Sequence[uuid.UUID],
        timeout: float = 120.0,
    ) -> dict[uuid.UUID, str | None]:
        """Block until the jobs complete; return ``{job_id: audio_path_or_None}``.

        Bare ``await_jobs`` resolves to the MODULE-LEVEL function below (class
        scope is not an enclosing scope for methods), so this delegates, never
        recurses. Now wired into the loop via ``_await_jobs`` (U4).
        """
        return await await_jobs(job_ids, timeout=timeout)

    async def abandon_jobs(self, job_ids: list[uuid.UUID]) -> int:
        """Terminal-fail still-pending jobs; delegates to the module function (REL-12a).

        Bare-name delegation (same pattern as ``submit``/``await_jobs``): the
        module global is resolved at call time, so tests can monkeypatch it.
        """
        return await abandon_generator_jobs(job_ids)

    async def pending_depth(self) -> int:
        """Pending-job count; delegates to the module function (REL-12c gauge)."""
        return await count_pending_jobs()
