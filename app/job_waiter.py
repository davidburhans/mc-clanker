"""
Job Waiter - Async utility for waiting on job completion.

Provides wait_for_job_completion() which uses PostgreSQL LISTEN/NOTIFY
for efficient event-driven waiting, with polling fallback.

Usage:
    job_id = uuid.uuid4()
    audio_path = await wait_for_job_completion(job_id, timeout=60.0)
    if audio_path:
        # Job completed successfully
    else:
        # Timeout or error
"""

import asyncio
import uuid
from typing import Optional

# Note: This module is designed to work with asyncpg for PostgreSQL async support.
# For now, we provide a polling-based implementation that can be enhanced with
# LISTEN/NOTIFY when asyncpg is available.


class JobWaiter:
    """
    Async job waiter with LISTEN/NOTIFY support.

    Uses PostgreSQL LISTEN/NOTIFY to efficiently wait for job completion
    without polling.
    """

    def __init__(self, db_pool):
        """
        Initialize with an asyncpg connection pool.

        Args:
            db_pool: asyncpg pool instance
        """
        self.db_pool = db_pool
        self._listeners = {}  # job_id -> asyncio.Event

    async def wait_for_job_completion(
        self,
        job_id: uuid.UUID,
        timeout: float = 60.0
    ) -> Optional[str]:
        """
        Wait for a job to complete using LISTEN/NOTIFY.

        Args:
            job_id: UUID of the job to wait for
            timeout: Maximum seconds to wait (default 60)

        Returns:
            audio_path string if job completed successfully
            None if timeout occurred or job failed
        """
        event = asyncio.Event()
        job_id_str = str(job_id)

        async def on_notify(connection, pid, channel, payload):
            """Handle notification - wake up if it's our job."""
            if payload == job_id_str:
                event.set()

        # Check if already completed before setting up listener
        job = await self._get_job(job_id)
        if job is None:
            return None

        if job["status"] == "completed":
            return job["audio_path"]

        if job["status"] == "failed":
            return None

        # Set up listener
        conn = await self.db_pool.acquire()
        try:
            await conn.add_listener("job_completed", on_notify)

            try:
                # Wait with timeout
                try:
                    await asyncio.wait_for(event.wait(), timeout)
                except asyncio.TimeoutError:
                    # Timeout - check final status
                    job = await self._get_job(job_id)
                    if job and job["status"] == "completed":
                        return job["audio_path"]
                    return None

                # Notification received - fetch final status
                job = await self._get_job(job_id)
                if job and job["status"] == "completed":
                    return job["audio_path"]
                return None

            finally:
                await conn.remove_listener("job_completed", on_notify)

        finally:
            await self.db_pool.release(conn)

    async def _get_job(self, job_id: uuid.UUID) -> Optional[dict]:
        """Fetch job from database."""
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM generator_jobs WHERE id = $1",
                job_id
            )
            if row:
                return dict(row)
            return None


# Polling-based fallback when asyncpg is not available
async def wait_for_job_completion_poll(
    db_session,
    job_id: uuid.UUID,
    timeout: float = 60.0,
    poll_interval: float = 0.5
) -> Optional[str]:
    """
    Polling-based wait_for_job_completion fallback.

    Args:
        db_session: SQLAlchemy session (sync)
        job_id: UUID of the job to wait for
        timeout: Maximum seconds to wait
        poll_interval: Seconds between status checks

    Returns:
        audio_path string if job completed
        None if timeout or error
    """
    from app.models.generator_job import GeneratorJob

    start_time = asyncio.get_running_loop().time()

    while True:
        elapsed = asyncio.get_running_loop().time() - start_time
        if elapsed >= timeout:
            return None

        # Check job status
        job = db_session.query(GeneratorJob).filter(
            GeneratorJob.id == job_id
        ).first()

        if job is None:
            return None

        if job.status == "completed":
            return job.audio_path

        if job.status == "failed":
            return None

        # Wait before next poll
        remaining = timeout - elapsed
        await asyncio.sleep(min(poll_interval, remaining))


# Main function - uses LISTEN/NOTIFY when available, falls back to polling
async def wait_for_job_completion(
    job_id: uuid.UUID,
    timeout: float = 60.0,
    db_manager=None
) -> Optional[str]:
    """
    Wait for a job to complete.

    Uses PostgreSQL LISTEN/NOTIFY when an asyncpg pool is available,
    otherwise falls back to polling with a SQLAlchemy session.

    Args:
        job_id: UUID of the job to wait for
        timeout: Maximum seconds to wait (default 60)
        db_manager: DatabaseManager instance (optional)

    Returns:
        audio_path string if job completed successfully
        None if timeout occurred, job failed, or job not found
    """
    # Check if asyncpg is available for LISTEN/NOTIFY
    try:
        import asyncpg
        has_asyncpg = True
    except ImportError:
        has_asyncpg = False

    if has_asyncpg and db_manager is not None:
        # Use asyncpg pool for LISTEN/NOTIFY
        try:
            pool = await asyncpg.create_pool(
                db_manager.engine.url.render_as_string(hide_password=False),
                min_size=1,
                max_size=5
            )
            waiter = JobWaiter(pool)
            result = await waiter.wait_for_job_completion(job_id, timeout)
            await pool.close()
            return result
        except Exception:
            # Fall back to polling on error
            pass

    # Fall back to polling with SQLAlchemy
    if db_manager is None:
        from app.db import DatabaseManager
        db_manager = DatabaseManager.get_instance()

    loop = asyncio.get_running_loop()

    def _poll_job():
        with db_manager.session() as session:
            from app.models.generator_job import GeneratorJob
            job = session.query(GeneratorJob).filter(
                GeneratorJob.id == job_id
            ).first()

            if job is None:
                return None

            if job.status == "completed":
                return job.audio_path

            if job.status == "failed":
                return None

            return "pending"

    # Polling loop
    start_time = loop.time()
    poll_interval = 0.5

    while True:
        elapsed = loop.time() - start_time
        if elapsed >= timeout:
            return None

        result = await loop.run_in_executor(None, _poll_job)

        if result is None or result != "pending":
            return result

        remaining = timeout - elapsed
        await asyncio.sleep(min(poll_interval, remaining))


async def wait_for_multiple_jobs(
    job_ids: list[uuid.UUID],
    timeout: float = 60.0,
    db_manager=None
) -> dict[uuid.UUID, Optional[str]]:
    """
    Wait for multiple jobs to complete concurrently.

    Args:
        job_ids: List of job UUIDs to wait for
        timeout: Maximum seconds to wait per job

    Returns:
        Dict mapping job_id to audio_path (or None if failed/timeout)
    """
    tasks = [
        wait_for_job_completion(job_id, timeout, db_manager)
        for job_id in job_ids
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    return {
        job_id: result if not isinstance(result, Exception) else None
        for job_id, result in zip(job_ids, results)
    }