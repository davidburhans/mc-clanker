"""
Tests for GeneratorWorker - job claiming, completion, and cleanup.

These tests use mocking to test worker behavior without requiring
a real PostgreSQL database.

Note: These tests require torch/statable-audio-tools which may not be
available in all environments. They are designed to run in the Docker
container where the worker runs.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock
import uuid

# Skip entire module if framework_generator can't be imported (torch/torchaudio issues)
# This happens at module load time, before any tests can run
try:
    from app.framework.framework_generator import GeneratorRegistry  # noqa: F401
except Exception:
    pytest.skip("framework_generator not available (torch/torchaudio issue)", allow_module_level=True)


class TestWorkerJobClaiming:
    """Tests for job claiming via FOR UPDATE SKIP LOCKED."""

    def test_claim_next_job_returns_pending_job(self):
        """
        Worker should claim pending jobs atomically.
        Uses FOR UPDATE SKIP LOCKED to prevent conflicts.
        """
        from app.worker import GeneratorWorker, WorkerConfig

        config = WorkerConfig(
            worker_id="test-worker",
            pg_dsn="postgresql://localhost/test",
            garage=MagicMock(),
        )
        worker = GeneratorWorker(config)
        worker.db = MagicMock()

        # Mock the connection pool
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "id": uuid.uuid4(),
                "instrument": "Synth Pad",
                "prompt": "atmospheric pad",
                "status": "pending",
                "model_id": "foundation-1",
                "key": "C minor",
                "bpm": 128,
                "bars": 4,
            }
        )
        mock_conn.execute = AsyncMock()

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock()
        worker.db = mock_pool

        # Run the claim
        import asyncio

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(worker._claim_next_job())
        loop.close()

        # Should have returned a job
        assert result is not None
        assert result["instrument"] == "Synth Pad"
        assert result["status"] == "pending"

    def test_claim_next_job_returns_none_when_queue_empty(self):
        """Worker returns None when no pending jobs are available."""
        from app.worker import GeneratorWorker, WorkerConfig

        config = WorkerConfig(
            worker_id="test-worker",
            pg_dsn="postgresql://localhost/test",
            garage=MagicMock(),
        )
        worker = GeneratorWorker(config)

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)  # No jobs

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock()
        worker.db = mock_pool

        import asyncio

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(worker._claim_next_job())
        loop.close()

        assert result is None


class TestWorkerJobCompletion:
    """Tests for job completion marking."""

    def test_mark_job_complete_updates_status(self):
        """Worker marks job as completed with audio path."""
        from app.worker import GeneratorWorker, WorkerConfig

        config = WorkerConfig(
            worker_id="test-worker",
            pg_dsn="postgresql://localhost/test",
            garage=MagicMock(),
        )
        worker = GeneratorWorker(config)

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock()
        worker.db = mock_pool

        job_id = uuid.uuid4()
        audio_path = "audio/test-job.aac"
        duration = 4.5

        import asyncio

        loop = asyncio.new_event_loop()
        loop.run_until_complete(worker._mark_job_complete(job_id, audio_path, duration))
        loop.close()

        # Verify execute was called with correct SQL
        mock_conn.execute.assert_called_once()
        call_args = mock_conn.execute.call_args[0]
        assert "completed" in call_args[0]
        assert audio_path in call_args

    def test_mark_job_failed_sets_error(self):
        """Worker marks job as failed with error message."""
        from app.worker import GeneratorWorker, WorkerConfig

        config = WorkerConfig(
            worker_id="test-worker",
            pg_dsn="postgresql://localhost/test",
            garage=MagicMock(),
        )
        worker = GeneratorWorker(config)

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock()
        worker.db = mock_pool

        job_id = uuid.uuid4()
        error_msg = "Generation failed: out of memory"

        import asyncio

        loop = asyncio.new_event_loop()
        loop.run_until_complete(worker._mark_job_failed(job_id, error_msg))
        loop.close()

        # Verify execute was called with failed status
        mock_conn.execute.assert_called_once()
        call_args = mock_conn.execute.call_args[0]
        assert "failed" in call_args[0]
        assert error_msg in call_args


class TestWorkerCleanup:
    """Tests for job cleanup."""

    def test_cleanup_deletes_expired_jobs(self):
        """Cleanup removes jobs past their expires_at timestamp."""
        from app.cleanup import JobExpirationCleanup

        cleanup = JobExpirationCleanup(MagicMock())
        cleanup.db = MagicMock()

        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=5)  # 5 jobs deleted

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock()
        cleanup.db = mock_pool

        import asyncio

        loop = asyncio.new_event_loop()
        deleted = loop.run_until_complete(cleanup._run_cleanup())
        loop.close()

        assert deleted == 5


class TestWorkerHealthCheck:
    """Tests for worker health check."""

    def test_health_check_returns_healthy_status(self):
        """Health check verifies DB and Garage connectivity."""
        from app.worker import GeneratorWorker, WorkerConfig

        config = WorkerConfig(
            worker_id="test-worker",
            pg_dsn="postgresql://localhost/test",
            garage=MagicMock(),
        )
        worker = GeneratorWorker(config)

        # Mock database connection that responds to SELECT 1
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=1)

        mock_pool = MagicMock()
        mock_pool.acquire = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock()
        worker.db = mock_pool
        worker.garage = MagicMock()

        import asyncio

        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(worker.health_check())
        loop.close()

        assert result["status"] == "healthy"
        assert result["database"] == "connected"
        assert result["garage"] == "connected"


class TestWorkerStats:
    """Tests for worker statistics."""

    def test_get_stats_returns_processing_counts(self):
        """Stats track jobs processed and failed."""
        from app.worker import GeneratorWorker, WorkerConfig

        config = WorkerConfig(
            worker_id="test-worker",
            pg_dsn="postgresql://localhost/test",
            garage=MagicMock(),
        )
        worker = GeneratorWorker(config)
        worker.jobs_processed = 10
        worker.jobs_failed = 2

        stats = worker.get_stats()

        assert stats["worker_id"] == "test-worker"
        assert stats["jobs_processed"] == 10
        assert stats["jobs_failed"] == 2
        assert stats["is_running"] is True
