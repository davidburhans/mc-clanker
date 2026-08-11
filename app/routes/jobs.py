from fastapi import APIRouter, HTTPException
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import text

from app.db import DatabaseManager
from app.models.generator_job import GeneratorJob
from .schemas import JobSubmission, SessionHeartbeatRequest, SessionServerResponse

router = APIRouter()


@router.post("/jobs", status_code=201)
async def submit_job(job: JobSubmission):
    """
    Submit a stem generation job to the queue.
    """
    db_manager = DatabaseManager.get_instance()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    with db_manager.session() as session:
        new_job = GeneratorJob(
            session_id=job.session_id,
            instrument=job.instrument,
            prompt=job.prompt,
            major_family=job.major_family,
            model_id=job.model_id,
            key=job.key,
            bpm=job.bpm,
            timbre_tags=job.timbre_tags,
            bars=job.bars,
            status="pending",
            expires_at=expires_at,
        )
        session.add(new_job)
        session.flush()
        session.refresh(new_job)
        job_id = str(new_job.id)

    return {"job_id": job_id, "status": "pending", "message": "Job submitted successfully"}


@router.get("/jobs/{job_id}")
async def get_job(job_id: uuid.UUID):
    """Get job status and audio path if completed."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        job = session.query(GeneratorJob).filter(GeneratorJob.id == job_id).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job.to_dict()


@router.get("/audio/{job_id}")
async def get_audio(job_id: uuid.UUID):
    """Stream audio info (presigned URL or path) for a completed job."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        job = session.query(GeneratorJob).filter(GeneratorJob.id == job_id).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status != "completed":
            raise HTTPException(status_code=400, detail=f"Job status: {job.status}")
        if not job.audio_path:
            raise HTTPException(status_code=404, detail="Audio path not found")

        job.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        session.commit()

        return {
            "audio_path": job.audio_path,
            "duration_seconds": job.duration_seconds,
            "message": "Audio available at audio_path.",
        }


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: uuid.UUID):
    """Cancel a pending job."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        job = session.query(GeneratorJob).filter(GeneratorJob.id == job_id).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status != "pending":
            raise HTTPException(status_code=400, detail=f"Cannot cancel status '{job.status}'")
        job.status = "expired"
        session.commit()
        return {"status": "ok"}


@router.get("/jobs")
async def list_jobs(session_id: uuid.UUID | None = None, status: str | None = None, limit: int = 50, offset: int = 0):
    """List jobs with filtering."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        query = session.query(GeneratorJob)
        if session_id:
            query = query.filter(GeneratorJob.session_id == session_id)
        if status:
            query = query.filter(GeneratorJob.status == status)

        total = query.count()
        jobs = query.order_by(GeneratorJob.created_at.desc()).limit(limit).offset(offset).all()
        return {"jobs": [job.to_dict() for job in jobs], "total": total, "limit": limit, "offset": offset}


# =============================================================================
# SESSION ROUTING
# =============================================================================


@router.post("/sessions/{session_id}/heartbeat")
async def session_heartbeat(session_id: uuid.UUID, request: SessionHeartbeatRequest):
    """Update session routing heartbeat."""
    db_manager = DatabaseManager.get_instance()
    dialect = db_manager.engine.dialect.name

    with db_manager.session() as session:
        if dialect == "postgresql":
            session.execute(
                text("""
                INSERT INTO session_routing (session_id, server_id, last_heartbeat)
                VALUES (:session_id, :server_id, NOW())
                ON CONFLICT (session_id) DO UPDATE SET
                    server_id = EXCLUDED.server_id,
                    last_heartbeat = NOW()
            """),
                {"session_id": str(session_id), "server_id": request.server_id},
            )
        else:
            # SQLite fallback
            result = session.execute(
                text("""
                UPDATE session_routing
                SET server_id = :server_id, last_heartbeat = :heartbeat
                WHERE session_id = :session_id
            """),
                {
                    "session_id": str(session_id),
                    "server_id": request.server_id,
                    "heartbeat": datetime.now(timezone.utc),
                },
            )
            if result.rowcount == 0:
                session.execute(
                    text("""
                    INSERT INTO session_routing (session_id, server_id, last_heartbeat)
                    VALUES (:session_id, :server_id, :heartbeat)
                """),
                    {
                        "session_id": str(session_id),
                        "server_id": request.server_id,
                        "heartbeat": datetime.now(timezone.utc),
                    },
                )

    return {"status": "ok"}


@router.get("/sessions/{session_id}/server", response_model=SessionServerResponse)
async def get_session_server(session_id: uuid.UUID):
    """Get which server handles a given session."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        result = session.execute(
            text("""
            SELECT session_id, server_id, created_at, last_heartbeat
            FROM session_routing
            WHERE session_id = :session_id
        """),
            {"session_id": str(session_id)},
        ).fetchone()

        if result is None:
            raise HTTPException(status_code=404, detail="Session not found")

        return {"session_id": result[0], "server_id": result[1], "created_at": result[2], "last_heartbeat": result[3]}


@router.delete("/sessions/{session_id}/routing")
async def delete_session_routing(session_id: uuid.UUID):
    """Remove a session from routing."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        session.execute(
            text("DELETE FROM session_routing WHERE session_id = :session_id"), {"session_id": str(session_id)}
        )
    return {"status": "ok"}


@router.get("/sessions/{session_id}/heartbeat")
async def get_session_heartbeat(session_id: uuid.UUID):
    """Get last heartbeat time."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        result = session.execute(
            text("SELECT last_heartbeat FROM session_routing WHERE session_id = :session_id"),
            {"session_id": str(session_id)},
        ).fetchone()
        if result is None:
            raise HTTPException(status_code=404, detail="Session not found")

        last_hb = result[0]
        if hasattr(last_hb, "replace"):
            last_hb = last_hb.replace(tzinfo=timezone.utc)

        return {
            "session_id": str(session_id),
            "last_heartbeat": last_hb,
            "is_stale": (datetime.now(timezone.utc) - last_hb) > timedelta(minutes=5),
        }
