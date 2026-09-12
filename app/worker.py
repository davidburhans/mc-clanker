"""
Generator Worker - Processes generation jobs from PostgreSQL queue.

This module runs in a separate container and:
1. Claims pending jobs via SELECT FOR UPDATE SKIP LOCKED
2. Generates audio using stable-audio-tools
3. Encodes to AAC
4. Uploads to Garage
5. Updates DB with result
6. Cleans up expired jobs periodically

Usage:
    # In container (via docker/Dockerfile.worker)
    python -m app.worker

Environment Variables Required:
    - WORKER_ID: Unique worker identifier
    - DATABASE_URL: PostgreSQL connection string
    - GARAGE_ENDPOINT: S3-compatible endpoint (e.g., http://garage:3900)
    - GARAGE_ACCESS_KEY: Garage access key
    - GARAGE_SECRET_KEY: Garage secret key
    - GARAGE_BUCKET: Bucket name for audio storage
    - GARAGE_BUCKET_REGION: Garage region (default: garage)
"""

import asyncio
import json
import logging
import os
import signal
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import asyncpg
import numpy as np

from app.aac_encoder import encode_aac, get_audio_duration
from app.cleanup import (
    JobExpirationCleanup,
    create_cleanup_config_from_env,
)

# Import generator - same as used by main app
from app.framework.framework_generator import GeneratorRegistry
from app.garage_client import GarageClient, GarageConfig, create_garage_client_from_env

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# A claimed job must finish (or heartbeat) before its lease lapses, else the
# cleanup reaper or another worker's claim reclaims it (review B2/C2).
JOB_LEASE_SECONDS = 600  # 10-minute lease window
JOB_LEASE = timedelta(seconds=JOB_LEASE_SECONDS)  # passed to asyncpg as a PG interval
JOB_LEASE_HEARTBEAT_SECONDS = 60.0  # refresh the lease while generation runs
# Hard cap so a hung model generation cannot wedge a worker slot forever (B6).
GENERATION_TIMEOUT_SECONDS = 600.0  # 10 minutes (normal generation is 5-30s)
# REL-03 circuit breaker: a timed-out generation thread cannot be killed and
# keeps holding VRAM + hf_hub locks, so retrying in-process just leaks another
# one. After this many consecutive timeouts (no successful pipeline between),
# exit non-zero and let Docker restart into a fresh CUDA context.
GENERATION_TIMEOUT_BREAKER_THRESHOLD = 2
# REL-23: bound on one between-jobs VRAM eviction pass — a zombie holding the
# registry lock must not stall the loop (see _maybe_evict_idle_models).
VRAM_EVICTION_TIMEOUT_SECONDS = 30.0
# REL-25a: the entire playback chain assumes 44.1 kHz and nothing downstream
# resamples — GarageAudioAdapter.fetch decodes with decode_aac(sample_rate=44100)
# (which RAISES on mismatch), the Mixer, the MP3 fan-out and the YouTube relay
# are all hard-coded 44100. Engine output is therefore normalized ONCE, here.
MIXER_SAMPLE_RATE = 44100
# REL-25b: worker-side fallbacks for rows predating the cfg/steps columns;
# mirror generate_stem()'s signature defaults and GlobalState's initial values.
DEFAULT_CFG_SCALE = 7.0
DEFAULT_STEPS = 50


class LostLeaseError(RuntimeError):
    """REL-24: the processing lease was lost mid-generation (row reclaimed,
    reaped or deleted).

    The Garage key is deterministic (audio/{job_id}.aac), so uploading would
    overwrite the new owner's completed audio or orphan an unreferenced object
    — the caller must skip upload and stand down.
    """


def _resample_to_mixer_rate(audio: np.ndarray, sample_rate: int | None) -> np.ndarray:
    """REL-25a: normalize engine output to the 44.1 kHz playback chain.

    No-op (same object) when already at the mixer rate — today's common case —
    and for the degenerate unknown-rate batch (None).

    scipy is imported lazily (rel-03 rule extended): the worker's module import
    must stay torch-free, and scipy.signal's import-time array-API probe does
    ``getattr(torch, 'Tensor')`` — which explodes under the fake-torch modules
    the torch-less test harnesses install in sys.modules (test_worker_vram.py).
    Only a non-44.1 kHz engine ever pays this import.

    Usage: ``pcm = _resample_to_mixer_rate(generate_stem(...)[0], sr)``
    """
    if sample_rate == MIXER_SAMPLE_RATE or sample_rate is None:
        return audio
    from scipy.signal import resample_poly  # deferred: see docstring

    return resample_poly(audio, MIXER_SAMPLE_RATE, sample_rate, axis=0).astype(np.float32)


async def _silently_cancel(task: "asyncio.Task") -> None:
    """Await a cancelled task, swallowing CancelledError and other errors."""
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110 - intentional teardown swallow
        pass


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


@dataclass
class WorkerConfig:
    """Configuration for a worker instance."""

    worker_id: str
    pg_dsn: str
    garage: GarageConfig
    job_poll_interval: float = 1.0  # seconds
    cleanup_interval: float = 300.0  # 5 minutes


class GeneratorWorker:
    """
    Async worker that processes generation jobs from PostgreSQL.

    Uses FOR UPDATE SKIP LOCKED to safely claim jobs without conflicts
    between multiple workers.
    """

    def __init__(self, config: WorkerConfig, exit_hook: Callable[[int], None] | None = None):
        self.config = config
        self.db: asyncpg.Pool | None = None
        self.garage: GarageClient | None = None
        self.generators = GeneratorRegistry()
        self.running = True
        self.jobs_processed = 0
        self.jobs_failed = 0
        # REL-03 early-warning breadcrumb (surfaced in get_stats); reset only
        # by a completed generate+upload pipeline, not by non-timeout failures.
        self.consecutive_generation_timeouts = 0
        # REL-03: os._exit (not sys.exit) deliberately skips graceful teardown
        # — the interpreter deadlocks joining the abandoned non-daemon thread.
        # Injectable so tests can observe the trip without dying.
        self.exit_hook = exit_hook or os._exit

    async def start(self):
        """Main entry point. Creates DB pool and starts worker loops."""
        logger.info(f"Worker {self.config.worker_id} starting...")

        # Load audio generation models from config
        self.generators.load()
        logger.info(f"Loaded {len(self.generators.models)} audio models: {list(self.generators.models.keys())}")

        # REL-03 (audit Critical): a cold-cache model download can never fit
        # inside the 600 s generation window, and its abandoned thread wedges
        # the hf_hub lock for every later retry. Warm the cache BEFORE the job
        # loop, outside any timeout. to_thread keeps SIGTERM handling
        # responsive during a multi-GB download. Per-model failures are
        # logged and skipped: one bad repo must not stop the worker serving
        # the other models.
        download_failures = await asyncio.to_thread(self.generators.download_models)
        for model_id, error in download_failures.items():
            logger.error("Pre-download failed for model %s: %s", model_id, error)

        # Create connection pool (handles concurrent job processing)
        self.db = await asyncpg.create_pool(
            self.config.pg_dsn,
            min_size=2,
            max_size=5,
            command_timeout=300,  # 5 minute timeout for queries
        )
        logger.info("Connected to PostgreSQL")

        # Create Garage client
        self.garage = create_garage_client_from_env()
        logger.info("Garage client initialized")

        # Start cleanup task
        cleanup_config = create_cleanup_config_from_env()
        cleanup_config.cleanup_interval = self.config.cleanup_interval
        cleanup_task = asyncio.create_task(self._cleanup_loop())

        # Main job processing loop
        while self.running:
            try:
                await self._process_next_job()
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                await asyncio.sleep(5)  # Back off on error

        # Shutdown
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        await self.db.close()
        logger.info(f"Worker {self.config.worker_id} stopped")

    async def _process_next_job(self):
        """Claim the next job and run it to completion, sleeping if the queue is idle."""
        job = await self._claim_next_job()
        if job is None:
            await asyncio.sleep(self.config.job_poll_interval)
            return
        logger.info("Processing job %s: %s", job["id"], job["instrument"])
        await self._process_claimed_job(job)
        # REL-23: between-jobs VRAM eviction (idle branch skips it — no new
        # loads can happen while idle, and the post-job check already ran).
        await self._maybe_evict_idle_models()

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
            await self._mark_job_complete(job["id"], audio_path, duration)
            self.jobs_processed += 1
            logger.info("Job %s completed: %s", job["id"], audio_path)
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

    async def _generate_with_lease(self, job: dict) -> tuple[str, float]:
        """
        Generate+upload while heartbeating the lease, under a hard timeout.

        The heartbeat refreshes lease_expires_at so an in-progress (but slow)
        job is not reaped; the timeout (review B6) prevents a hung model from
        wedging a worker slot forever.
        """
        # Generation runs in a PRIVATE single-thread pool per job: cancelling the
        # await on timeout cannot interrupt an already-running thread, so a hung
        # generation would otherwise keep holding the SHARED default executor
        # until every run_in_encoder slot queued forever and the worker wedged
        # again (review ASYNC-2). We abandon at most one private thread per
        # timed-out job instead of starving encode/upload.
        gen_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"gen-{job['id']}")
        heartbeat = asyncio.create_task(self._heartbeat_loop(job["id"]))
        try:
            result = await asyncio.wait_for(
                self._generate_and_upload(job, gen_pool),
                timeout=GENERATION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            # REL-03: the abandoned thread keeps holding VRAM / hf locks, so a
            # timeout is not just a failed job — feed the circuit breaker.
            self._handle_generation_timeout(job)
            # Wrap-and-reraise so _process_claimed_job's generic handler marks
            # the row failed with a MEANINGFUL message (bare str(
            # asyncio.TimeoutError) is often empty).
            raise TimeoutError(f"generation pipeline exceeded {GENERATION_TIMEOUT_SECONDS:.0f}s") from exc
        else:
            # Only a completed pipeline proves the CUDA context healthy;
            # non-timeout failures deliberately leave the counter untouched.
            self.consecutive_generation_timeouts = 0
            return result
        finally:
            heartbeat.cancel()
            await _silently_cancel(heartbeat)
            gen_pool.shutdown(wait=False, cancel_futures=True)

    def _handle_generation_timeout(self, job: dict) -> None:
        """REL-03 breaker: 2nd consecutive generation timeout -> exit(1).

        os._exit (not sys.exit) deliberately skips graceful teardown: the
        audit found the interpreter deadlocks joining the abandoned
        non-daemon thread. The current job row keeps its live lease and is
        reclaimed/reaped after it lapses (<= ~11 min); compose
        restart=unless-stopped brings this worker back with a fresh context.

        "Consecutive" counts since the last COMPLETED pipeline, not since the
        last non-timeout failure: the thread abandoned by timeout #1 survives
        an unrelated later failure (fast CUDA-OOM, upload 500) still holding
        VRAM/hf locks, so only a completed generate+upload proves health.
        """
        self.consecutive_generation_timeouts += 1
        count = self.consecutive_generation_timeouts
        logger.error("Job %s generation timed out (consecutive=%d)", job["id"], count)
        if count < GENERATION_TIMEOUT_BREAKER_THRESHOLD:
            return
        # Structured JSON for the trip line (AGENTS.md observability rule).
        logger.error(
            "circuit_breaker_open %s",
            json.dumps(
                {
                    "event": "circuit_breaker_open",
                    "worker_id": self.config.worker_id,
                    "job_id": str(job["id"]),
                    "consecutive_timeouts": count,
                    "timeout_seconds": GENERATION_TIMEOUT_SECONDS,
                    "action": "exit_1",
                    "reason": "generation_timeout_streak",
                }
            ),
        )
        self.exit_hook(1)

    async def _maybe_evict_idle_models(self) -> None:
        """REL-23: LRU-evict loaded non-default models when VRAM is critical.

        Between jobs only. Bounded on purpose: a REL-03 zombie can hold the
        registry lock forever, and a stuck eviction must not stall the loop —
        the NEXT job's 600 s timeout is what trips the breaker.
        """
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._evict_idle_models_sync),
                timeout=VRAM_EVICTION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("VRAM eviction timed out (registry lock busy); skipping")

    def _evict_idle_models_sync(self) -> None:
        """Blocking half of _maybe_evict_idle_models (torch queries + .cpu() moves)."""
        monitor = self.generators.gpu_monitor
        if not monitor.should_offload():
            return
        for model_id in self.generators.lru_eviction_candidates():
            logger.info("VRAM critical: unloading idle model %s", model_id)
            self.generators.unload_model(model_id)
            if not monitor.should_offload():
                return

    async def _heartbeat_loop(self, job_id: uuid.UUID) -> None:
        """Periodically extend the lease while generation runs."""
        while True:
            await asyncio.sleep(JOB_LEASE_HEARTBEAT_SECONDS)
            try:
                await self._refresh_lease(job_id)
            except Exception as e:  # noqa: BLE001 - keep generating; lease will warn
                logger.warning("Lease heartbeat failed for %s: %s", job_id, e)

    async def _refresh_lease(self, job_id: uuid.UUID) -> None:
        """Extend lease_expires_at for an in-progress job."""
        assert self.db is not None
        lease_expiry = datetime.now(timezone.utc) + JOB_LEASE
        async with self.db.acquire() as conn:
            await conn.execute(
                """
                UPDATE generator_jobs
                SET lease_expires_at = $1
                WHERE id = $2 AND status = 'processing'
            """,
                lease_expiry,
                job_id,
            )

    def _generate_stem_for_job(self, job: dict) -> tuple[np.ndarray, int]:
        """Blocking engine call for one job (runs in the caller's private pool).

        REL-25b: cfg/steps columns are NULLable — absent/None (a pre-migration
        row) falls back to the engine defaults; cfg_scale=0.0 is a LEGAL value
        (GenerationConfig ge=0.0) and must pass through uncoerced, so the
        fallback is an explicit None check, never ``or``.
        """
        cfg_scale = job.get("cfg_scale")
        steps = job.get("steps")
        return self.generators.generate_stem(
            model_id=job["model_id"],
            prompt=job["prompt"],
            key=job.get("key") or "",
            bpm=job.get("bpm") or 120,
            bars=job.get("bars", 4),
            cfg_scale=DEFAULT_CFG_SCALE if cfg_scale is None else cfg_scale,
            steps=DEFAULT_STEPS if steps is None else steps,
        )

    async def _generate_and_upload(self, job: dict, gen_pool: ThreadPoolExecutor | None = None) -> tuple[str, float]:
        """
        Generate audio for a job and upload to Garage.

        The blocking generate_stem call runs in ``gen_pool`` (the caller's
        per-job pool, see _generate_with_lease); encode/upload stay on the
        shared default executor (bounded operations).

        REL-24: generation is the long window (5-30 s) across which the lease
        can lapse (heartbeat dead after an event-loop stall) and the row be
        reclaimed. The lease is re-verified AFTER generating and BEFORE any
        write: the Garage key is deterministic, so encoding/uploading blind
        would clobber the new owner's object or orphan it forever.

        REL-25a: engine output is normalized ONCE to MIXER_SAMPLE_RATE here,
        so every downstream consumer (decode_aac@44100, mixer, fan-out,
        relay) keeps its hard-coded 44.1 kHz assumption true.

        Returns:
            Tuple of (garage_path, duration_seconds) — duration from the
            array/rate actually encoded, so it always matches the object.
        """
        assert self.garage is not None
        loop = asyncio.get_running_loop()
        audio_array, sample_rate = await loop.run_in_executor(gen_pool, lambda: self._generate_stem_for_job(job))

        # REL-24: no encode CPU, no write on a job we no longer own.
        if not await self._lease_still_held(job["id"]):
            logger.warning(
                "Job %s: lease lost during generation; skipping upload (row no longer ours)",
                job["id"],
            )
            raise LostLeaseError(f"job {job['id']} reclaimed mid-generation")

        pcm = await loop.run_in_executor(None, lambda: _resample_to_mixer_rate(audio_array, sample_rate))
        aac_bytes = await loop.run_in_executor(None, lambda: encode_aac(pcm, sample_rate=MIXER_SAMPLE_RATE))

        # Upload to Garage
        audio_path = f"audio/{job['id']}.aac"
        await self.garage.put_object(audio_path, aac_bytes)

        # Calculate duration (from the encoded pair, not the native rate)
        duration = get_audio_duration(pcm, sample_rate=MIXER_SAMPLE_RATE)

        return audio_path, duration

    async def _delete_orphan_audio(self, audio_path: str) -> None:
        """Best-effort delete of an uploaded object whose DB row failed to commit (C5)."""
        if not audio_path or self.garage is None:
            return
        try:
            await self.garage.delete_object(audio_path)
            logger.info("Deleted orphaned audio %s", audio_path)
        except Exception as e:  # noqa: BLE001 - orphan cleanup must not mask the real error
            logger.warning("Could not delete orphan audio %s: %s", audio_path, e)

    async def _mark_job_complete(self, job_id: uuid.UUID, audio_path: str, duration: float) -> None:
        """Mark job completed and NOTIFY listeners in ONE transaction (A7/C6).

        The UPDATE is guarded by lease ownership: a zombie worker whose lease
        lapsed and whose job was reaped/re-claimed must not clobber the new
        owner's row or resurrect a reaped job with a late NOTIFY (review DATA-4).
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
                    return
                # NOTIFY inside the same transaction: a crash between UPDATE and
                # NOTIFY can no longer drop the notification (review A7/C6).
                # pg_notify() is fully parameterized (no f-string payload).
                await conn.execute("SELECT pg_notify('job_completed', $1)", str(job_id))

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

    async def _cleanup_loop(self):
        """Periodically clean up expired jobs + run retention passes (U5).

        The config comes from create_cleanup_config_from_env() (not a 3-field
        CleanupConfig) so worker-side cleanup honors the same retention envs as
        the dedicated compose cleanup service. The worker container ships none
        of the file envs → those passes disable themselves there; production
        retention is owned by the dedicated service (decision 2).
        """
        config = create_cleanup_config_from_env()
        config.cleanup_interval = self.config.cleanup_interval
        cleanup = JobExpirationCleanup(config)
        cleanup.db = self.db
        cleanup.garage = self.garage

        while self.running:
            await asyncio.sleep(self.config.cleanup_interval)

            try:
                await cleanup._run_cleanup()
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

    def get_stats(self) -> dict:
        """Get worker statistics."""
        return {
            "worker_id": self.config.worker_id,
            "jobs_processed": self.jobs_processed,
            "jobs_failed": self.jobs_failed,
            "consecutive_generation_timeouts": self.consecutive_generation_timeouts,
            "is_running": self.running,
        }

    async def health_check(self) -> dict:
        """Check worker health status."""
        assert self.db is not None
        try:
            # Check database connectivity
            async with self.db.acquire() as conn:
                await conn.fetchval("SELECT 1")

            # Check Garage connectivity
            # We can't easily check without an object, so just check client exists
            garage_ok = self.garage is not None

            return {
                "status": "healthy",
                "worker_id": self.config.worker_id,
                "database": "connected",
                "garage": "connected" if garage_ok else "disconnected",
                "jobs_processed": self.jobs_processed,
                "jobs_failed": self.jobs_failed,
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "worker_id": self.config.worker_id,
                "error": str(e),
            }

    def stop(self):
        """Graceful shutdown."""
        logger.info("Shutdown requested...")
        self.running = False


def create_config_from_env() -> WorkerConfig:
    """Create worker config from environment variables."""
    garage_config = GarageConfig(
        endpoint=os.environ["GARAGE_ENDPOINT"],
        access_key=os.environ["GARAGE_ACCESS_KEY"],
        secret_key=os.environ["GARAGE_SECRET_KEY"],
        bucket=os.environ["GARAGE_BUCKET"],
        region=os.environ.get("GARAGE_BUCKET_REGION", "garage"),
    )

    return WorkerConfig(
        worker_id=os.environ.get("WORKER_ID", f"worker-{uuid.uuid4().hex[:8]}"),
        pg_dsn=os.environ["DATABASE_URL"],
        garage=garage_config,
    )


# Global worker instance for health check endpoint
_worker_instance: Optional["GeneratorWorker"] = None


def get_worker_instance() -> Optional["GeneratorWorker"]:
    """Get the global worker instance."""
    return _worker_instance


def set_worker_instance(worker: "GeneratorWorker"):
    """Set the global worker instance."""
    global _worker_instance
    _worker_instance = worker


async def main():
    """Entry point for worker process."""
    config = create_config_from_env()
    worker = GeneratorWorker(config)
    set_worker_instance(worker)

    # Handle graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stop)

    await worker.start()


if __name__ == "__main__":
    asyncio.run(main())
