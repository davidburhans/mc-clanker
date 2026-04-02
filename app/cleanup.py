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
        self.db = await asyncpg.create_pool(
            self.config.pg_dsn,
            min_size=1,
            max_size=5,
            command_timeout=60
        )
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

        Returns:
            Number of jobs cleaned up
        """
        deleted_audio_paths: List[str] = []

        async with self.db.acquire() as conn:
            # Delete expired jobs and get their audio paths
            deleted_count = await conn.fetchval("""
                WITH deleted AS (
                    DELETE FROM generator_jobs
                    WHERE status IN ('completed', 'failed', 'expired')
                      AND expires_at < NOW()
                    RETURNING audio_path
                )
                SELECT COUNT(*) FROM deleted
            """)

            # Get audio paths to delete (only those with valid paths)
            async with self.db.acquire() as conn2:
                audio_paths = await conn2.fetch("""
                    SELECT audio_path FROM generator_jobs
                    WHERE status IN ('completed', 'failed', 'expired')
                      AND expires_at < NOW()
                      AND audio_path IS NOT NULL
                """)

            deleted_audio_paths = [row['audio_path'] for row in audio_paths]

        # Delete audio files from Garage (outside transaction)
        if self.garage and deleted_audio_paths:
            for audio_path in deleted_audio_paths:
                try:
                    await self.garage.delete_object(audio_path)
                    logger.debug(f"Deleted audio: {audio_path}")
                except Exception as e:
                    logger.warning(f"Failed to delete audio {audio_path}: {e}")

        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} expired jobs")

        return deleted_count

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
        )
    )
    cleanup = JobExpirationCleanup(config)
    cleanup.garage = create_garage_client_from_env()
    cleanup.db = await asyncpg.create_pool(
        config.pg_dsn,
        min_size=1,
        max_size=5
    )
    try:
        return await cleanup._run_cleanup()
    finally:
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
        )
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
