"""
Cleanup Module - Job expiration cleanup for mc-clanker.

This module provides cleanup functionality for removing expired jobs
and their associated audio files from Garage object storage.

Usage:
    # From command line
    python -m cleanup

    # Or import for use in worker
    from cleanup import cleanup_expired_jobs, CleanupConfig, create_cleanup_from_env

Environment Variables:
    - DATABASE_URL: PostgreSQL connection string
    - GARAGE_ENDPOINT: S3-compatible endpoint (e.g., http://garage:3900)
    - GARAGE_ACCESS_KEY: Garage access key
    - GARAGE_SECRET_KEY: Garage secret key
    - GARAGE_BUCKET: Bucket name for audio storage
"""

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from typing import Optional, List

import asyncpg

from app.garage_client import GarageClient, GarageConfig, create_garage_client_from_env

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class CleanupConfig:
    """Configuration for cleanup operations."""

    pg_dsn: str
    garage: GarageConfig
    cleanup_interval: float = 300.0  # 5 minutes


class JobExpirationCleanup:
    """
    Handles cleanup of expired jobs and their audio files.

    Jobs are considered expired when:
    - status is 'completed', 'failed', or 'expired'
    - expires_at < NOW()

    This class can run as a standalone cleanup service or be
    used by the worker process.
    """

    def __init__(self, config: CleanupConfig):
        self.config = config
        self.db: Optional[asyncpg.Pool] = None
        self.garage: Optional[GarageClient] = None
        self.running = True

    async def start(self):
        """Start the cleanup loop."""
        logger.info("Starting job expiration cleanup...")

        # Create database connection pool
        self.db = await asyncpg.create_pool(self.config.pg_dsn, min_size=1, max_size=5, command_timeout=60)
        logger.info("Connected to PostgreSQL")

        # Create Garage client
        self.garage = create_garage_client_from_env()
        logger.info("Garage client initialized")

        # Run cleanup loop
        while self.running:
            try:
                await self._run_cleanup()
            except Exception as e:
                logger.error(f"Cleanup error: {e}", exc_info=True)

            await asyncio.sleep(self.config.cleanup_interval)

        # Shutdown
        if self.db:
            await self.db.close()
        logger.info("Cleanup stopped")

    async def _run_cleanup(self) -> int:
        """
        Run a single cleanup cycle.

        First reaps jobs orphaned in 'processing' by a dead worker (lapsed
        lease), then atomically deletes expired terminal jobs and their audio
        objects in one CTE so we never query rows already deleted.

        Returns:
            Number of jobs acted on this cycle (reaped + deleted).
        """
        reaped = await self._reap_stale_processing()
        deleted_count = await self._delete_expired_jobs()
        return reaped + deleted_count

    async def _reap_stale_processing(self) -> int:
        """
        Fail jobs whose processing lease has expired.

        Without this, a worker that crashed between claim and completion would
        leave the job in 'processing' forever (the claim query only selects
        'pending', and the expired-job DELETE only touches terminal statuses).
        Reaped rows are given a short expiry so the expired-job deletion reclaims
        their storage on a later cycle while keeping the failure record briefly.

        Returns:
            Number of jobs reaped.
        """
        assert self.db is not None  # initialized in start() before cleanup runs
        async with self.db.acquire() as conn:
            rows = await conn.fetch("""
                UPDATE generator_jobs
                SET status = 'failed',
                    error_message = COALESCE(error_message,
                                            'Worker lease expired (stale)'),
                    completed_at = NOW(),
                    expires_at = NOW() + INTERVAL '1 hour'
                WHERE status = 'processing'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < NOW()
                RETURNING id
            """)
        reaped = len(rows)
        if reaped:
            logger.warning("Reaped %d stale 'processing' jobs (expired lease)", reaped)
        return reaped

    async def _delete_expired_jobs(self) -> int:
        """
        Delete expired terminal jobs and their Garage audio objects.

        Uses a single DELETE ... RETURNING CTE so the audio paths we delete from
        Garage always correspond to rows that still existed at delete time.
        """
        assert self.db is not None  # initialized in start() before cleanup runs
        async with self.db.acquire() as conn:
            rows = await conn.fetch("""
                WITH deleted AS (
                    DELETE FROM generator_jobs
                    WHERE status IN ('completed', 'failed', 'expired')
                      AND expires_at < NOW()
                    RETURNING audio_path
                )
                SELECT audio_path FROM deleted WHERE audio_path IS NOT NULL
            """)

        deleted_audio_paths = [row["audio_path"] for row in rows]
        if self.garage and deleted_audio_paths:
            await self._delete_garage_objects(deleted_audio_paths)

        if rows:
            logger.info("Cleaned up %d expired jobs", len(rows))
        return len(rows)

    async def _delete_garage_objects(self, audio_paths: list[str]) -> None:
        """Best-effort delete of each audio object; failures are logged, not fatal."""
        assert self.garage is not None  # caller guards on truthiness
        for audio_path in audio_paths:
            try:
                await self.garage.delete_object(audio_path)
                logger.debug("Deleted audio: %s", audio_path)
            except Exception as e:  # noqa: BLE001 - cleanup must be resilient
                logger.warning("Failed to delete audio %s: %s", audio_path, e)

    def stop(self):
        """Stop the cleanup loop."""
        logger.info("Shutdown requested...")
        self.running = False


async def cleanup_expired_jobs_once(pg_dsn: str) -> int:
    """
    Run a single cleanup cycle and return the count of deleted jobs.

    This is a convenience function for running cleanup as a cron job
    or one-shot operation.

    Args:
        pg_dsn: PostgreSQL connection string

    Returns:
        Number of jobs cleaned up
    """
    config = CleanupConfig(
        pg_dsn=pg_dsn,
        garage=GarageConfig(
            endpoint=os.environ["GARAGE_ENDPOINT"],
            access_key=os.environ["GARAGE_ACCESS_KEY"],
            secret_key=os.environ["GARAGE_SECRET_KEY"],
            bucket=os.environ["GARAGE_BUCKET"],
        ),
    )
    cleanup = JobExpirationCleanup(config)
    cleanup.garage = create_garage_client_from_env()
    cleanup.db = await asyncpg.create_pool(config.pg_dsn, min_size=1, max_size=5)
    try:
        return await cleanup._run_cleanup()
    finally:
        if cleanup.db is not None:
            await cleanup.db.close()


def create_cleanup_config_from_env() -> CleanupConfig:
    """Create cleanup config from environment variables."""
    return CleanupConfig(
        pg_dsn=os.environ["DATABASE_URL"],
        garage=GarageConfig(
            endpoint=os.environ["GARAGE_ENDPOINT"],
            access_key=os.environ["GARAGE_ACCESS_KEY"],
            secret_key=os.environ["GARAGE_SECRET_KEY"],
            bucket=os.environ["GARAGE_BUCKET"],
            region=os.environ.get("GARAGE_BUCKET_REGION", "garage"),
        ),
    )


async def main():
    """Entry point for standalone cleanup service."""
    config = create_cleanup_config_from_env()
    cleanup = JobExpirationCleanup(config)

    # Handle graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, cleanup.stop)

    await cleanup.start()


if __name__ == "__main__":
    asyncio.run(main())
