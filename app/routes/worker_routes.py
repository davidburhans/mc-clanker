"""Worker Routes - Health check and stats endpoints for the worker service.

These endpoints are used by the load balancer and orchestration systems
to verify worker health and gather statistics.
"""

from fastapi import APIRouter, HTTPException

from app.worker import get_worker_instance
from .schemas import WorkerHealthResponse, WorkerStatsResponse, WorkerQueueDepthResponse

router = APIRouter()


@router.get(
    "/health",
    response_model=WorkerHealthResponse,
    summary="Worker health check",
    description=(
        "Returns the health status of the worker including DB connectivity, Garage connectivity, and job counts."
    ),
    responses={200: {"description": "Health status"}},
)
async def health_check():
    """Worker health check endpoint."""
    worker = get_worker_instance()
    if worker is None:
        return {"status": "not_started", "message": "Worker has not been initialized"}
    return await worker.health_check()


@router.get(
    "/stats",
    response_model=WorkerStatsResponse,
    summary="Worker statistics",
    description="Returns cumulative statistics: jobs processed, failures, and running state.",
    responses={200: {"description": "Worker stats"}, 503: {"description": "Worker not initialized"}},
)
async def get_stats():
    """Get worker statistics."""
    worker = get_worker_instance()
    if worker is None:
        raise HTTPException(status_code=503, detail="Worker has not been initialized")
    return worker.get_stats()


@router.get(
    "/queue-depth",
    response_model=WorkerQueueDepthResponse,
    summary="Queue depth",
    description="Returns the current number of pending and processing jobs. Useful for autoscaling.",
    responses={
        200: {"description": "Queue depth"},
        503: {"description": "Worker/DB not available"},
        500: {"description": "Query failed"},
    },
)
async def get_queue_depth():
    """Get the current job queue depth."""
    worker = get_worker_instance()
    if worker is None:
        raise HTTPException(status_code=503, detail="Worker has not been initialized")
    if worker.db is None:
        raise HTTPException(status_code=503, detail="Worker database not connected")
    try:
        async with worker.db.acquire() as conn:
            pending_count = await conn.fetchval("SELECT COUNT(*) FROM generator_jobs WHERE status = 'pending'")
            processing_count = await conn.fetchval("SELECT COUNT(*) FROM generator_jobs WHERE status = 'processing'")
        return {
            "pending": pending_count,
            "processing": processing_count,
            "total_active": pending_count + processing_count,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get queue depth: {str(e)}")
