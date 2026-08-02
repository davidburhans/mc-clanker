"""Postgres job-queue adapter for the framework loop (Phase 5).

Owns the generator-job submission path so the loop depends on one thin function
instead of reaching into SQLAlchemy + the GeneratorJob model inline. The
``await_jobs`` helper wraps ``app.job_waiter.wait_for_multiple_jobs`` so that
Phase 7b can route both submit + await through a single injected ``JobQueuePort``.

NOTE: the foreground ``_run_loop`` and background ``_pre_generate_next_loop``
still call ``wait_for_multiple_jobs`` via the ``framework_main_async`` module
binding (the Gap 4/5/6 characterization tests monkeypatch THAT binding). Routing
those calls through ``await_jobs`` is deferred to Phase 7b, where the port is
injected and the test harness patches the fake adapter instead.
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

    Thin wrapper over the LISTEN/NOTIFY waiter. Not yet wired into the loop
    (see module docstring); provided so Phase 7b's injected port reuses it.
    """
    from app.job_waiter import wait_for_multiple_jobs

    return await wait_for_multiple_jobs(list(job_ids), timeout=timeout)
