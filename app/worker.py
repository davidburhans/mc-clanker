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
from typing import Callable, Optional

import asyncpg
import numpy as np

from app.aac_encoder import MIXER_SAMPLE_RATE, _resample_to_mixer_rate, encode_aac, get_audio_duration
from app.cleanup import (
    JobExpirationCleanup,
    create_cleanup_config_from_env,
)

# Import generator - same as used by main app
from app.framework.framework_generator import GeneratorRegistry
from app.garage_client import GarageClient, GarageConfig, create_garage_client_from_env
from app.worker_job_rows import LostLeaseError, _JobRowLifecycle

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Hard cap so a hung model generation cannot wedge a worker slot forever (B6).
GENERATION_TIMEOUT_SECONDS = 600.0  # 10 minutes (normal generation is 5-30s)
# REL-03 circuit breaker: a timed-out generation thread cannot be killed and
# keeps holding VRAM + hf_hub locks, so retrying in-process just leaks another
# one. After this many consecutive timeouts (no successful pipeline between),
# exit non-zero and let Docker restart into a fresh CUDA context.
GENERATION_TIMEOUT_BREAKER_THRESHOLD = 2


class GenerationIoTimeout(RuntimeError):
    """FU-3: a pipeline-INTERNAL I/O timeout (garage upload, socket) escaping
    ``_run_generation_pipeline``.

    On py3.11+ builtin ``TimeoutError`` ALIASES ``asyncio.TimeoutError``, so an
    escape would otherwise feed the REL-03 breaker via
    ``_generate_with_lease``'s handler — a false trip: the breaker counts only
    ``wait_for``'s own deadline (the abandoned-thread stall it exists for).
    Sibling of ``LostLeaseError``.
    """
# REL-23: bound on one between-jobs VRAM eviction pass — a zombie holding the
# registry lock must not stall the loop (see _maybe_evict_idle_models).
VRAM_EVICTION_TIMEOUT_SECONDS = 30.0
# REL-25b: worker-side fallbacks for rows predating the cfg/steps columns;
# mirror generate_stem()'s signature defaults and GlobalState's initial values.
DEFAULT_CFG_SCALE = 7.0
DEFAULT_STEPS = 50


async def _silently_cancel(task: "asyncio.Task") -> None:
    """Await a cancelled task, swallowing CancelledError and other errors."""
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110 - intentional teardown swallow
        pass


@dataclass
class WorkerConfig:
    """Configuration for a worker instance."""

    worker_id: str
    pg_dsn: str
    garage: GarageConfig
    job_poll_interval: float = 1.0  # seconds
    cleanup_interval: float = 300.0  # 5 minutes


class GeneratorWorker(_JobRowLifecycle):
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
            # FU-3: ONLY wait_for's own deadline may reach this handler — on
            # py3.11+ builtin TimeoutError ALIASES asyncio.TimeoutError, so
            # pipeline I/O timeouts are re-wrapped at the _generate_and_upload
            # boundary (GenerationIoTimeout) to keep them out of here.
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
        """Generate audio for a job and upload to Garage (FU-3 provenance
        wrapper: the body lives in ``_run_generation_pipeline``; only the
        TimeoutError re-wrap sits here)."""
        try:
            return await self._run_generation_pipeline(job, gen_pool)
        except TimeoutError as exc:
            # FU-3: on py3.11+ builtin TimeoutError ALIASES asyncio.TimeoutError,
            # so a pipeline-internal I/O timeout (garage upload, socket) would
            # otherwise land in _generate_with_lease's asyncio.TimeoutError
            # handler and feed the REL-03 breaker. Re-wrap so it counts as an
            # ordinary failure (jobs_failed), never as an abandoned-thread stall.
            # (On py3.10 the builtin does not match asyncio.TimeoutError at all,
            # and this wrapper awaits no wait_for — exact on every version.)
            raise GenerationIoTimeout(f"generation pipeline I/O timeout: {exc}") from exc

    async def _run_generation_pipeline(
        self, job: dict, gen_pool: ThreadPoolExecutor | None = None
    ) -> tuple[str, float]:
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
                # FU-3 (H1): REL-03's early-warning breadcrumb must be visible on
                # the endpoint operators poll — between timeout #1 and the trip
                # the container healthcheck passes while wedged.
                "consecutive_generation_timeouts": self.consecutive_generation_timeouts,
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "worker_id": self.config.worker_id,
                "error": str(e),
                # FU-3 (H1): the wedged-ish state where the breadcrumb matters.
                "consecutive_generation_timeouts": self.consecutive_generation_timeouts,
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
