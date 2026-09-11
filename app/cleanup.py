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

import asyncpg

from app.garage_client import GarageClient, GarageConfig, create_garage_client_from_env

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# asyncpg command timeout for every pool this module opens (review B3/Q3).
_POOL_COMMAND_TIMEOUT = 60.0


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
        self.db: asyncpg.Pool | None = None
        self.garage: GarageClient | None = None
        # E6: an Event (not a bool) so stop() interrupts the idle wait instead of
        # being noticed only after up to cleanup_interval (300 s) of sleep.
        self._shutdown = asyncio.Event()

    @property
    def running(self) -> bool:
        """True until stop() is called (kept for the pre-Event public surface)."""
        return not self._shutdown.is_set()

    async def start(self):
        """Start the cleanup loop."""
        logger.info("Starting job expiration cleanup...")

        # Create database connection pool
        self.db = await asyncpg.create_pool(
            self.config.pg_dsn,
            min_size=1,
            max_size=5,
            command_timeout=_POOL_COMMAND_TIMEOUT,
        )
        logger.info("Connected to PostgreSQL")

        # Create Garage client
        self.garage = create_garage_client_from_env()
        logger.info("Garage client initialized")

        # Run cleanup loop; the idle wait is interruptible (E6) so a SIGTERM
        # during the (usually 300 s) gap still reaches the graceful shutdown below.
        while not self._shutdown.is_set():
            try:
                await self._run_cleanup()
            except Exception as e:
                logger.error(f"Cleanup error: {e}", exc_info=True)

            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=self.config.cleanup_interval)
            except TimeoutError:
                continue

        # Shutdown
        if self.db:
            await self.db.close()
        logger.info("Cleanup stopped")

    async def _run_cleanup(self) -> int:
        """
        Run a single cleanup cycle.

        First reaps jobs orphaned in 'processing' by a dead worker (lapsed
        lease), then deletes expired terminal jobs: Garage objects first, rows
        afterwards (E2), so a storage failure can never leave an object that no
        row references any more.

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

        Ordering is the whole point (review E2/Q4): objects are deleted FIRST and
        the rows only afterwards. The old CTE deleted rows first and swallowed
        object failures, so any Garage error produced an object that no row would
        ever name again - a permanent orphan with no GC path. Rows whose object
        could not be deleted are left in place (and counted) for a later cycle.
        """
        assert self.db is not None  # initialized in start() before cleanup runs
        async with self.db.acquire() as conn:
            rows = await conn.fetch("""
                SELECT audio_path
                FROM generator_jobs
                WHERE status IN ('completed', 'failed', 'expired')
                  AND expires_at < NOW()
            """)
        if not rows:
            return 0

        audio_paths = [row["audio_path"] for row in rows if row["audio_path"]]
        failed_paths = await self._delete_garage_objects(audio_paths)
        await self._delete_expired_rows(failed_paths)

        deleted_count = len(rows) - len(failed_paths)
        logger.info("Cleaned up %d expired jobs", deleted_count)
        return deleted_count

    async def _delete_expired_rows(self, keep_audio_paths: set[str]) -> None:
        """Delete terminal rows past expiry, keeping those whose object is still there."""
        assert self.db is not None
        async with self.db.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM generator_jobs
                WHERE status IN ('completed', 'failed', 'expired')
                  AND expires_at < NOW()
                  AND (audio_path IS NULL OR NOT (audio_path = ANY($1::text[])))
                """,
                list(keep_audio_paths),
            )

    async def _delete_garage_objects(self, audio_paths: list[str]) -> set[str]:
        """Delete each audio object, returning the paths whose delete FAILED.

        Failures are logged and counted but never raised: cleanup must stay
        resilient, and the caller keeps the matching rows for a retry (E2).
        """
        if not self.garage or not audio_paths:
            return set()
        failed: set[str] = set()
        for audio_path in audio_paths:
            try:
                await self.garage.delete_object(audio_path)
                logger.debug("Deleted audio: %s", audio_path)
            except Exception as e:  # noqa: BLE001 - cleanup must be resilient
                failed.add(audio_path)
                logger.warning("Failed to delete audio %s: %s", audio_path, e)
        if failed:
            logger.warning(
                "Garage delete failed for %d/%d expired audio objects; keeping their rows for retry",
                len(failed),
                len(audio_paths),
            )
        return failed

    def stop(self):
        """Stop the cleanup loop (safe to call from a signal handler)."""
        logger.info("Shutdown requested...")
        self._shutdown.set()


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
    # Q3: this one-shot cron path had the only unbounded pool in the codebase.
    cleanup.db = await asyncpg.create_pool(
        config.pg_dsn,
        min_size=1,
        max_size=5,
        command_timeout=_POOL_COMMAND_TIMEOUT,
    )
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
