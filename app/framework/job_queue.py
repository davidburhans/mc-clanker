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

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone


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
