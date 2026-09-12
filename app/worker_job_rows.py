"""Job-row lifecycle adapter of ``GeneratorWorker`` (FU-3 extraction).

Pure move from ``worker.py`` (refactor/plans/units/rel-fu-3-plan.md §2.1): the
rel-03 + rel-24/25 units grew the worker file past the project's 500-LOC rule
(AGENTS.md), so the job-ROW lifecycle block — claim, ownership predicates,
lease heartbeat/refresh, terminal writes, orphan-audio cleanup — moved here as
the ``_JobRowLifecycle`` mixin, mirroring FU-2's ``_LoopDelegates`` pattern.
``GeneratorWorker(_JobRowLifecycle)`` (mixin first in the MRO): the methods
resolve on the combined class, so instance-attribute patches
(``patch.object(worker, '_mark_job_complete', ...)`` etc.) keep working
unchanged. Module-level name patches in the tests all target readers that
STAYED in ``app/worker.py`` (GENERATION_TIMEOUT_SECONDS, encode_aac, ...) —
a function reads module globals from the module where it is DEFINED, which is
why those readers did not move.

Host contract: ``GeneratorWorker.__init__`` provides ``config`` (worker_id),
``db`` (asyncpg pool), ``garage`` (client), the ``jobs_processed`` /
``jobs_failed`` counters, and the generation seam ``_generate_with_lease``.

FU-3 behavior edits riding the move (everything else byte-identical):
- ``_refresh_lease``: the UPDATE is scoped to ``worker_id`` (R1) — after a
  reclaim the row's worker_id is the NEW owner, and a zombie's heartbeat must
  not extend a lease it no longer owns.
- ``_mark_job_complete`` returns ``bool`` and ``_process_claimed_job`` counts +
  logs only a LANDED completion (C1) — a 0-rowcount (lost-lease) write is not
  a processed job.

Torch-free import rule (rel-03): this module imports stdlib only — the worker
module import must stay importable without torch (the torch-less test
harnesses stub ``app.framework.framework_generator`` and import app.worker).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# A claimed job must finish (or heartbeat) before its lease lapses, else the
# cleanup reaper or another worker's claim reclaims it (review B2/C2).
JOB_LEASE_SECONDS = 600  # 10-minute lease window
JOB_LEASE = timedelta(seconds=JOB_LEASE_SECONDS)  # passed to asyncpg as a PG interval
JOB_LEASE_HEARTBEAT_SECONDS = 60.0  # refresh the lease while generation runs


class LostLeaseError(RuntimeError):
    """REL-24: the processing lease was lost mid-generation (row reclaimed,
    reaped or deleted).

    The Garage key is deterministic (audio/{job_id}.aac), so uploading would
    overwrite the new owner's completed audio or orphan an unreferenced object
    — the caller must skip upload and stand down.
    """


def _update_rowcount(command_tag: object) -> int:
    """Parse an asyncpg command tag like 'UPDATE 3' into its rowcount.

    Returns 1 for unparseable tags: asyncpg always sends 'UPDATE n' for UPDATE
    statements, so the fallback only triggers for test doubles, where the
    legacy behavior (assume the update landed) is the safe default.
    """
    try:
        return int(str(command_tag).split()[-1])
    except (ValueError, IndexError):
        return 1


class _JobRowLifecycle:
    """Lifecycle of a generator_jobs ROW from claim to terminal state.

    Concrete methods only (no abstract stubs): the host
    ``GeneratorWorker`` inherits this FIRST in its MRO, so these
    implementations are the ones ``self.<name>`` resolves.
    """

    async def _process_claimed_job(self, job: dict):
        """Generate, upload, and complete a claimed job; clean up orphans on failure."""
        try:
            audio_path, duration = await self._generate_with_lease(job)
        except LostLeaseError:
            # REL-24: not this job's failure — it continues under its new owner.
            # No upload happened, so there is no temp/orphan to clean; do not
            # mark-fail (ownership-guarded no-op anyway) and do not count it.
            logger.warning("Job %s stood down: lease lost, new owner active", job["id"])
            return
        except Exception as e:  # noqa: BLE001 - generation/upload failed
            logger.error("Job %s failed during generation: %s", job["id"], e)
            await self._mark_job_failed(job["id"], str(e))
            self.jobs_failed += 1
            return
        try:
            completed = await self._mark_job_complete(job["id"], audio_path, duration)
        except Exception as e:  # noqa: BLE001 - upload ok, DB commit failed -> orphan
            logger.error("Job %s DB-complete failed: %s; reclaiming audio", job["id"], e)
            # E1/Q2: the object key is deterministic (audio/{job_id}.aac), so a
            # zombie whose lease lapsed would delete the object a reclaiming
            # worker just completed with. Only reclaim while the row is ours.
            if await self._still_own_job_row(job["id"]):
                await self._delete_orphan_audio(audio_path)
            else:
                logger.warning(
                    "Job %s no longer owned by %s; leaving %s for the current owner",
                    job["id"],
                    self.config.worker_id,
                    audio_path,
                )
            await self._mark_job_failed(job["id"], str(e))
            self.jobs_failed += 1
            return
        if not completed:
            # FU-3 (C1): a 0-rowcount completion is a lost lease, not a
            # processed job — counting it inflates the /health signal the
            # breaker diagnosis reads.
            logger.warning("Job %s not counted as processed: completion skipped (lease lost)", job["id"])
            return
        self.jobs_processed += 1
        logger.info("Job %s completed: %s", job["id"], audio_path)

    async def _read_job_ownership(self, job_id: uuid.UUID) -> dict | None:
        """SELECT (status, worker_id) for one job row; None when the row is gone.

        Raises on read failure — each caller applies its own conservative policy
        (delete-guard: keep the object; upload-guard: skip the write).
        """
        assert self.db is not None
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, worker_id FROM generator_jobs WHERE id = $1",
                job_id,
            )
        return dict(row) if row is not None else None

    async def _still_own_job_row(self, job_id: uuid.UUID) -> bool:
        """Whether ``audio/{job_id}.aac`` is still ours to delete (review E1/Q2).

        True when the row is gone, or is still 'processing' owned by this worker;
        False when another worker already terminalled/reclaimed it, or when the
        row cannot be read (deleting blind is what destroyed the new owner's
        audio). With no pool at all there is no competing owner, so fall back to
        the pre-existing C5 orphan sweep.

        Usage: ``if await self._still_own_job_row(job["id"]): await self._delete_orphan_audio(p)``
        """
        if self.db is None:
            return True
        try:
            row = await self._read_job_ownership(job_id)
        except Exception as e:  # noqa: BLE001 - cannot prove ownership -> keep the object
            logger.warning("Could not re-check ownership of job %s: %s", job_id, e)
            return False
        if row is None:
            return True
        return row["status"] == "processing" and row["worker_id"] == self.config.worker_id

    async def _lease_still_held(self, job_id: uuid.UUID) -> bool:
        """REL-24 upload-guard predicate: strictly "we still hold the lease".

        Unlike _still_own_job_row (delete-safety: a GONE row means no competing
        owner), a gone row here means there is no job left to complete — the
        upload would orphan an unreferenced object (cleanup deletes objects via
        rows, so nothing could ever find it). False on read error too:
        unprovable ownership must never translate into a Garage write.

        Usage: ``if not await self._lease_still_held(job["id"]): raise LostLeaseError(...)``
        """
        if self.db is None:
            return True  # no competing owner possible (matches _still_own_job_row)
        try:
            row = await self._read_job_ownership(job_id)
        except Exception as e:  # noqa: BLE001 - cannot prove ownership -> skip upload
            logger.warning("Could not verify lease for job %s: %s", job_id, e)
            return False
        return (
            row is not None
            and row["status"] == "processing"
            and row["worker_id"] == self.config.worker_id
        )

    async def _claim_next_job(self) -> dict | None:
        """
        Atomically claim the next pending job, or reclaim one whose lease expired.

        FOR UPDATE SKIP LOCKED keeps two workers from taking the same row. We also
        pick up 'processing' rows whose lease_expires_at has lapsed, so a worker
        that died mid-generation no longer orphans its job forever (review B2/C2).

        Returns:
            Job dict if one was claimed, None if the queue is empty.
        """
        assert self.db is not None  # set in start() before the job loop runs
        async with self.db.acquire() as conn:
            async with conn.transaction():
                job = await conn.fetchrow("""
                    SELECT *
                    FROM generator_jobs
                    WHERE status = 'pending'
                       OR (status = 'processing' AND lease_expires_at < NOW())
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                """)
                if job is None:
                    return None
                # Mark claimed within the same transaction that locked the row,
                # and start the lease (B2). started_at is preserved on a reclaim.
                lease_expiry = datetime.now(timezone.utc) + JOB_LEASE
                await conn.execute(
                    """
                    UPDATE generator_jobs
                    SET status = 'processing',
                        started_at = COALESCE(started_at, NOW()),
                        worker_id = $1,
                        lease_expires_at = $2
                    WHERE id = $3
                """,
                    self.config.worker_id,
                    lease_expiry,
                    job["id"],
                )
                return dict(job)

    async def _heartbeat_loop(self, job_id: uuid.UUID) -> None:
        """Periodically extend the lease while generation runs."""
        while True:
            await asyncio.sleep(JOB_LEASE_HEARTBEAT_SECONDS)
            try:
                await self._refresh_lease(job_id)
            except Exception as e:  # noqa: BLE001 - keep generating; lease will warn
                logger.warning("Lease heartbeat failed for %s: %s", job_id, e)

    async def _refresh_lease(self, job_id: uuid.UUID) -> None:
        """Extend lease_expires_at for an in-progress job.

        FU-3 (R1): scoped to worker_id, the same predicate shape as the DATA-4
        terminal writes — after a reclaim the row's worker_id is the NEW owner,
        and this (zombie's) heartbeat must not extend a lease it no longer
        owns. Worst case unscoped: the reclaimed row becomes effectively
        immortal if the new owner then dies, since the reaper + reclaim both
        key on the lease.
        """
        assert self.db is not None
        lease_expiry = datetime.now(timezone.utc) + JOB_LEASE
        async with self.db.acquire() as conn:
            await conn.execute(
                """
                UPDATE generator_jobs
                SET lease_expires_at = $1
                WHERE id = $2 AND status = 'processing' AND worker_id = $3
            """,
                lease_expiry,
                job_id,
                self.config.worker_id,
            )

    async def _delete_orphan_audio(self, audio_path: str) -> None:
        """Best-effort delete of an uploaded object whose DB row failed to commit (C5)."""
        if not audio_path or self.garage is None:
            return
        try:
            await self.garage.delete_object(audio_path)
            logger.info("Deleted orphaned audio %s", audio_path)
        except Exception as e:  # noqa: BLE001 - orphan cleanup must not mask the real error
            logger.warning("Could not delete orphan audio %s: %s", audio_path, e)

    async def _mark_job_complete(self, job_id: uuid.UUID, audio_path: str, duration: float) -> bool:
        """Mark job completed and NOTIFY listeners in ONE transaction (A7/C6).

        The UPDATE is guarded by lease ownership: a zombie worker whose lease
        lapsed and whose job was reaped/re-claimed must not clobber the new
        owner's row or resurrect a reaped job with a late NOTIFY (review DATA-4).

        FU-3 (C1): returns whether the completion LANDED — a 0-rowcount UPDATE
        is a lost lease, not a processed job; the caller owns counting.
        """
        assert self.db is not None
        async with self.db.acquire() as conn:  # noqa: SIM117 - acquire+tx can't be one CM
            async with conn.transaction():
                tag = await conn.execute(
                    """
                    UPDATE generator_jobs
                    SET status = 'completed',
                        audio_path = $1,
                        duration_seconds = $2,
                        completed_at = NOW(),
                        expires_at = NOW() + INTERVAL '24 hours',
                        lease_expires_at = NULL
                    WHERE id = $3 AND status = 'processing' AND worker_id = $4
                """,
                    audio_path,
                    duration,
                    job_id,
                    self.config.worker_id,
                )
                if _update_rowcount(tag) == 0:
                    logger.warning("Job %s lost lease; skipping completion + NOTIFY", job_id)
                    return False
                # NOTIFY inside the same transaction: a crash between UPDATE and
                # NOTIFY can no longer drop the notification (review A7/C6).
                # pg_notify() is fully parameterized (no f-string payload).
                await conn.execute("SELECT pg_notify('job_completed', $1)", str(job_id))
                return True

    async def _mark_job_failed(self, job_id: uuid.UUID, error: str) -> None:
        """Mark job as failed with error message and release its lease.

        Guarded like _mark_job_complete: only the current lease owner may fail
        the row, so a zombie worker cannot flip a reclaimed job to 'failed'
        (review DATA-4).
        """
        assert self.db is not None
        async with self.db.acquire() as conn:
            tag = await conn.execute(
                """
                UPDATE generator_jobs
                SET status = 'failed',
                    error_message = $1,
                    completed_at = NOW(),
                    expires_at = NOW() + INTERVAL '1 hour',
                    lease_expires_at = NULL
                WHERE id = $2 AND status = 'processing' AND worker_id = $3
            """,
                error,
                job_id,
                self.config.worker_id,
            )
            if _update_rowcount(tag) == 0:
                logger.warning("Job %s lost lease; skipping failure write", job_id)
