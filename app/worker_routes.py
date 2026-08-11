"""
Worker Routes - Health check and stats endpoints for the worker service.

These endpoints are used by the load balancer and orchestration systems
to verify worker health and gather statistics.

Note: These routes are intended to run on the worker service, not the
main web server. In a multi-container Docker Compose setup, the worker
exposes these ports separately.

Endpoints:
    - GET /api/worker/health - Worker health check
    - GET /api/worker/stats - Worker statistics
    - GET /api/worker/queue-depth - Current queue depth

Usage:
    # Add to worker FastAPI app
    from worker_routes import worker_router
    app.include_router(worker_router, prefix="/api/worker")
"""

from fastapi import APIRouter, HTTPException

from app.worker import get_worker_instance

router = APIRouter()


@router.get("/health")
async def health_check():
    """
    Worker health check endpoint.

    Returns the health status of the worker including:
    - Database connectivity
    - Garage connectivity
    - Jobs processed/failed counts
    - Overall status (healthy/unhealthy)
    """
    worker = get_worker_instance()

    if worker is None:
        return {"status": "not_started", "message": "Worker has not been initialized"}

    return await worker.health_check()


@router.get("/stats")
async def get_stats():
    """
    Get worker statistics.

    Returns cumulative statistics about the worker's operation:
    - worker_id: Unique identifier for this worker
    - jobs_processed: Total number of jobs successfully completed
    - jobs_failed: Total number of jobs that failed
    - is_running: Whether the worker is actively processing
    """
    worker = get_worker_instance()

    if worker is None:
        raise HTTPException(status_code=503, detail="Worker has not been initialized")

    return worker.get_stats()


@router.get("/queue-depth")
async def get_queue_depth():
    """
    Get the current job queue depth.

    Returns the number of pending jobs in the queue.
    This is useful for autoscaling decisions.

    Note: This requires database access and will return an error
    if the worker is not connected to the database.
    """
    worker = get_worker_instance()

    if worker is None:
        raise HTTPException(status_code=503, detail="Worker has not been initialized")

    if worker.db is None:
        raise HTTPException(status_code=503, detail="Worker database not connected")

    try:
        async with worker.db.acquire() as conn:
            pending_count = await conn.fetchval("""
                SELECT COUNT(*) FROM generator_jobs
                WHERE status = 'pending'
            """)
            processing_count = await conn.fetchval("""
                SELECT COUNT(*) FROM generator_jobs
                WHERE status = 'processing'
            """)

        return {
            "pending": pending_count,
            "processing": processing_count,
            "total_active": pending_count + processing_count,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get queue depth: {str(e)}")
