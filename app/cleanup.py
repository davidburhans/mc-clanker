"""
Cleanup Module - Job expiration cleanup for mc-clanker.

This module provides cleanup functionality for removing expired jobs
and their associated audio files from Garage object storage, plus the
REL-05/REL-16 (U5) storage-retention passes:

- show-recording + export file retention (mtime-based, config-gated)
- stale session_routing reaper (heartbeat index predicate)
- opt-in llm_interactions/show_actions retention: export-before-delete
  (invariant 4 — the default keeps every row forever)

Usage:
    # From command line (the dedicated compose "cleanup" service runs this)
    python -m app.cleanup

    # Or import for use in worker
    from app.cleanup import cleanup_expired_jobs_once, CleanupConfig, create_cleanup_config_from_env

Environment Variables:
    - DATABASE_URL: PostgreSQL connection string
    - GARAGE_ENDPOINT: S3-compatible endpoint (e.g., http://garage:3900)
    - GARAGE_ACCESS_KEY: Garage access key
    - GARAGE_SECRET_KEY: Garage secret key
    - GARAGE_BUCKET: Bucket name for audio storage
    - SHOW_AUDIO_RETENTION_DAYS: days to keep audio*.wav under SHOWS_DIR (0 = off)
    - EXPORT_RETENTION_DAYS: days to keep mc_clanker_*.{wav,mp3} in EXPORT_DIR (0 = off)
    - SESSION_STALE_HOURS: age at which session_routing rows are reaped (0 = off)
    - LLM_RETENTION_DAYS: days to keep the audit corpus (0 = keep forever, invariant 4)
    - AUDIT_ARCHIVE_DIR: NDJSON export destination for LLM_RETENTION_DAYS — must
      point at persistent storage and be mounted into every container that runs
      cleanup (compose cleanup service + worker in-process loop)
"""

import asyncio
import logging
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass

import asyncpg

from app import retention
from app.garage_client import GarageClient, GarageConfig, create_garage_client_from_env

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# asyncpg command timeout for every pool this module opens (review B3/Q3).
_POOL_COMMAND_TIMEOUT = 60.0

def _env_int(name: str, default: int) -> int:
    """Parse an integer env var; unset/invalid falls back to ``default`` (logged), negatives clamp to 0."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using default %d", name, raw, default)
        return default
    return max(0, value)


def _retention_kwargs() -> dict:
    """Retention env vars as CleanupConfig kwargs — shared by the service and one-shot paths.

    Bare-env defaults are DISABLED (0 days) so existing deployments see zero
    behavior change; the shipped compose sets SHOW_AUDIO_RETENTION_DAYS=14 and
    EXPORT_RETENTION_DAYS=7 explicitly.
    """
    return {
        "show_audio_retention_days": _env_int("SHOW_AUDIO_RETENTION_DAYS", 0),
        "export_retention_days": _env_int("EXPORT_RETENTION_DAYS", 0),
        "session_stale_hours": _env_int("SESSION_STALE_HOURS", 24),
        "llm_retention_days": _env_int("LLM_RETENTION_DAYS", 0),
        "audit_archive_dir": os.environ.get("AUDIT_ARCHIVE_DIR", "/exports/audit_archive"),
    }


@dataclass
class CleanupConfig:
    """Configuration for cleanup operations."""

    pg_dsn: str
    garage: GarageConfig
    cleanup_interval: float = 300.0  # 5 minutes
    # REL-05/REL-16 retention (U5). 0 disables the corresponding pass; the file
    # passes and corpus retention default OFF (invariant 4: never delete the
    # fine-tuning corpus by default), the session reaper defaults ON (24 h).
    show_audio_retention_days: int = 0
    export_retention_days: int = 0
    session_stale_hours: int = 24
    llm_retention_days: int = 0
    audit_archive_dir: str = "/exports/audit_archive"
    # Empty → resolved via app.lib.paths at pass time (respects SHOWS_DIR/EXPORT_DIR).
    shows_dir: str = ""
    export_dir: str = ""


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
        # REL-05/REL-16 (U5): retention passes are error-isolated so a failure
        # can never block job reaping/deletion or a sibling pass.
        sessions = await self._run_pass("session reaper", self._reap_stale_sessions)
        files = await self._run_pass("recording retention", self._sweep_expired_recordings)
        audit = await self._run_pass("audit retention", self._delete_expired_audit_rows)
        return reaped + deleted_count + sessions + files + audit

    async def _run_pass(self, label: str, pass_fn: Callable[[], int]) -> int:
        """Run one cleanup pass in isolation: a failure logs and yields 0 (cycle stays resilient)."""
        try:
            return await pass_fn()
        except Exception:  # noqa: BLE001 - one bad pass must never wedge the loop
            logger.exception("%s pass failed; continuing with sibling passes", label)
            return 0

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

    # ------------------------------------------------------------------
    # REL-05/REL-16 retention passes — delegated to app/retention.py (one
    # responsibility per module, per AGENTS.md file-size guide). Each is
    # config-gated there and error-isolated by _run_pass here.
    # ------------------------------------------------------------------

    async def _reap_stale_sessions(self) -> int:
        """Delete session_routing rows staler than session_stale_hours (REL-16b; 0 disables)."""
        return await retention.reap_stale_sessions(self.db, self.config.session_stale_hours)

    async def _sweep_expired_recordings(self) -> int:
        """Remove expired show recordings + exports per the config days (REL-05b; 0 disables)."""
        return await retention.sweep_expired_recordings(self.config)

    async def _delete_expired_audit_rows(self) -> int:
        """Archive-then-delete corpus rows past llm_retention_days (REL-16a; 0 = keep forever)."""
        return await retention.delete_expired_audit_rows(
            self.db, self.config.llm_retention_days, self.config.audit_archive_dir
        )


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
        # Review round-1 P2: share the service path's env parsing. Without it,
        # SESSION_STALE_HOURS=0 could not disable the reaper here (the 24 h
        # default always ran on this cron path) and the retention envs were
        # silently ignored.
        **_retention_kwargs(),
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
    """Create cleanup config from environment variables (incl. retention envs, U5)."""
    return CleanupConfig(
        pg_dsn=os.environ["DATABASE_URL"],
        garage=GarageConfig(
            endpoint=os.environ["GARAGE_ENDPOINT"],
            access_key=os.environ["GARAGE_ACCESS_KEY"],
            secret_key=os.environ["GARAGE_SECRET_KEY"],
            bucket=os.environ["GARAGE_BUCKET"],
            region=os.environ.get("GARAGE_BUCKET_REGION", "garage"),
        ),
        **_retention_kwargs(),
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
