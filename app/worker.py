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
import logging
import os
import signal
import uuid
from dataclasses import dataclass
from typing import Optional

import asyncpg

from app.aac_encoder import encode_aac, get_audio_duration
from app.cleanup import JobExpirationCleanup, create_cleanup_config_from_env
from app.garage_client import GarageClient, GarageConfig, create_garage_client_from_env

# Import generator - same as used by main app
from app.framework.framework_generator import GeneratorRegistry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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

    def __init__(self, config: WorkerConfig):
        self.config = config
        self.db: Optional[asyncpg.Pool] = None
        self.garage: Optional[GarageClient] = None
        self.generators = GeneratorRegistry()
        self.running = True
        self.jobs_processed = 0
        self.jobs_failed = 0

    async def start(self):
        """Main entry point. Creates DB pool and starts worker loops."""
        logger.info(f"Worker {self.config.worker_id} starting...")

        # Load audio generation models from config
        self.generators.load()
        logger.info(f"Loaded {len(self.generators.models)} audio models: {list(self.generators.models.keys())}")

        # Create connection pool (handles concurrent job processing)
        self.db = await asyncpg.create_pool(
            self.config.pg_dsn,
            min_size=2,
            max_size=5,
            command_timeout=300  # 5 minute timeout for queries
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
        """
        Atomically claim and process the next pending job.

        Uses FOR UPDATE SKIP LOCKED to:
        - Prevent two workers from taking the same job
        - Skip jobs that are being processed by other workers
        """
        job = await self._claim_next_job()

        if job is None:
            # No pending jobs, wait before polling again
            await asyncio.sleep(self.config.job_poll_interval)
            return

        logger.info(f"Processing job {job['id']}: {job['instrument']}")

        try:
            # Generate and upload (outside transaction for I/O)
            audio_path, duration = await self._generate_and_upload(job)

            # Mark complete
            await self._mark_job_complete(job['id'], audio_path, duration)
            self.jobs_processed += 1

            logger.info(f"Job {job['id']} completed: {audio_path}")

        except Exception as e:
            logger.error(f"Job {job['id']} failed: {e}")
            await self._mark_job_failed(job['id'], str(e))
            self.jobs_failed += 1

    async def _claim_next_job(self) -> Optional[dict]:
        """
        Atomically claim the highest-priority pending job.

        Returns:
            Job dict if one was claimed, None if queue was empty.
        """
        async with self.db.acquire() as conn:
            async with conn.transaction():
                job = await conn.fetchrow("""
                    SELECT *
                    FROM generator_jobs
                    WHERE status = 'pending'
                    ORDER BY priority DESC, created_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                """)

                if job is None:
                    return None

                # Mark as processing (within same transaction)
                await conn.execute("""
                    UPDATE generator_jobs
                    SET status = 'processing',
                        started_at = NOW(),
                        worker_id = $1
                    WHERE id = $2
                """, self.config.worker_id, job['id'])

                return dict(job)

    async def _generate_and_upload(self, job: dict) -> tuple[str, float]:
        """
        Generate audio for a job and upload to Garage.

        Args:
            job: Job dict with generation parameters

        Returns:
            Tuple of (garage_path, duration_seconds)
        """
        # Generate using stable-audio-tools (blocking, runs in executor)
        loop = asyncio.get_running_loop()
        audio_array = await loop.run_in_executor(
            None,
            lambda: self.generators.generate_stem(
                model_id=job['model_id'],
                prompt=job['prompt'],
                key=job.get('key'),
                bpm=job.get('bpm'),
                bars=job.get('bars', 4),
            )
        )

        # Encode to AAC
        aac_bytes = await loop.run_in_executor(
            None,
            lambda: encode_aac(audio_array, sample_rate=44100)
        )

        # Upload to Garage
        audio_path = f"audio/{job['id']}.aac"
        await self.garage.put_object(audio_path, aac_bytes)

        # Calculate duration
        duration = get_audio_duration(audio_array, sample_rate=44100)

        return audio_path, duration

    async def _mark_job_complete(self, job_id: uuid.UUID, audio_path: str, duration: float):
        """Mark job as completed with result."""
        async with self.db.acquire() as conn:
            await conn.execute("""
                UPDATE generator_jobs
                SET status = 'completed',
                    audio_path = $1,
                    duration_seconds = $2,
                    completed_at = NOW(),
                    expires_at = NOW() + INTERVAL '24 hours'
                WHERE id = $3
            """, audio_path, duration, job_id)

            # Notify listeners
            await conn.execute(f"NOTIFY job_completed, '{job_id}'")

    async def _mark_job_failed(self, job_id: uuid.UUID, error: str):
        """Mark job as failed with error message."""
        async with self.db.acquire() as conn:
            await conn.execute("""
                UPDATE generator_jobs
                SET status = 'failed',
                    error_message = $1,
                    completed_at = NOW(),
                    expires_at = NOW() + INTERVAL '1 hour'
                WHERE id = $2
            """, error, job_id)

    async def _cleanup_loop(self):
        """Periodically clean up expired jobs."""
        cleanup = JobExpirationCleanup(self.config)
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
            "is_running": self.running,
        }

    async def health_check(self) -> dict:
        """Check worker health status."""
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
