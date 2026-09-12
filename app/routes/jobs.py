import os
import re
import socket
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

from app.auth import get_current_user_from_request
from app.db import DatabaseManager
from app.models.generator_job import GeneratorJob

from .schemas import JobSubmission, SessionHeartbeatRequest, SessionServerResponse

router = APIRouter()

# A routing server id is used as the HTTP authority of a 307 redirect target, so
# only a bare host[:port] may ever be stored (round-3 D7).
_SERVER_ID_SHAPE = re.compile(r"^[A-Za-z0-9._-]{1,253}(:\d{1,5})?$")


def _require_authenticated(request: Request) -> None:
    """SEC-4 route-level gate for the mutating job routes (round-3 D5).

    The sibling GETs have carried this gate since round 2; without it a JWT-only
    deployment (no DJ_PASSWORD env) let anonymous peers flood the queue and cancel
    other users' pending jobs.
    """
    if get_current_user_from_request(request) is None:
        raise HTTPException(status_code=401, detail="Not authenticated")


def _own_server_ids() -> set[str]:
    """Server ids that name THIS deployment (round-3 D7 anti-poisoning list).

    ``SessionAffinityMiddleware`` 307-redirects every session-scoped request to
    ``{scheme}://{server_id}/...``, so a heartbeat must only ever be able to store
    this instance's own identity — never an arbitrary host supplied by the caller.
    """
    known: set[str] = set()
    for raw in (os.environ.get("SERVER_ID"), os.environ.get("HOSTNAME")):
        if raw:
            known.update({raw, f"server-{raw}"})
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    if hostname:
        known.update({hostname, f"server-{hostname}"})
    try:
        # Lazy import: app_ui imports this module (via app.routes) at module load.
        from app import app_ui

        known.add(getattr(app_ui, "current_server_id", ""))
    except Exception:  # pragma: no cover - app_ui always importable in practice
        pass
    known.discard("")
    return known


def _validate_server_id(server_id: str) -> str:
    """Reject server ids that are not this deployment's own identity (round-3 D7).

    Example: ``evil.example`` -> 422, ``server-host1`` (own id) -> accepted.
    """
    candidate = (server_id or "").strip()
    if not _SERVER_ID_SHAPE.match(candidate):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid server_id {server_id!r}: expected a bare host[:port] naming this server",
        )
    own = _own_server_ids()
    if own and candidate not in own:
        raise HTTPException(
            status_code=422,
            detail=(
                f"server_id {server_id!r} does not match this deployment's server identity "
                f"(expected one of: {', '.join(sorted(own))})"
            ),
        )
    return candidate


@router.post("/jobs", status_code=201)
async def submit_job(job: JobSubmission, request: Request):
    """
    Submit a stem generation job to the queue.

    Authentication required (review SEC-4 / round-3 D5).
    """
    _require_authenticated(request)
    db_manager = DatabaseManager.get_instance()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    with db_manager.session() as session:
        new_job = GeneratorJob(
            # str() on purpose: the SQLite fallback stores uuids in VARCHAR(36)
            # and sqlite3 cannot bind a uuid.UUID object (round-3 D6, POST 500'd).
            session_id=str(job.session_id),
            instrument=job.instrument,
            prompt=job.prompt,
            major_family=job.major_family,
            model_id=job.model_id,
            key=job.key,
            bpm=job.bpm,
            timbre_tags=job.timbre_tags,
            bars=job.bars,
            cfg_scale=job.cfg_scale,
            steps=job.steps,
            status="pending",
            expires_at=expires_at,
        )
        session.add(new_job)
        session.flush()
        session.refresh(new_job)
        job_id = str(new_job.id)

    return {"job_id": job_id, "status": "pending", "message": "Job submitted successfully"}


@router.get("/jobs/{job_id}")
async def get_job(job_id: uuid.UUID, request: Request):
    """Get job status and audio path if completed.

    Authentication required (review SEC-4): job rows carry prompts and queue
    contents that must not be enumerable by anonymous peers.
    """
    _require_authenticated(request)
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        # str comparison: the id column is VARCHAR(36) on the SQLite fallback, so a
        # uuid.UUID comparand never matched and existing rows 404'd (round-3 D6).
        job = session.query(GeneratorJob).filter(GeneratorJob.id == str(job_id)).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job.to_dict()


@router.get("/audio/{job_id}")
async def get_audio(job_id: uuid.UUID, request: Request):
    """Stream audio info (presigned URL or path) for a completed job.

    Authentication required (review SEC-4).
    """
    _require_authenticated(request)
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        job = session.query(GeneratorJob).filter(GeneratorJob.id == str(job_id)).first()
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
async def cancel_job(job_id: uuid.UUID, request: Request):
    """Cancel a pending job.

    Authentication required (review SEC-4 / round-3 D5): anonymous callers could
    otherwise cancel anybody's queued job.
    """
    _require_authenticated(request)
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        job = session.query(GeneratorJob).filter(GeneratorJob.id == str(job_id)).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status != "pending":
            raise HTTPException(status_code=400, detail=f"Cannot cancel status '{job.status}'")
        job.status = "expired"
        session.commit()
        return {"status": "ok"}


@router.get("/jobs")
async def list_jobs(
    request: Request,
    session_id: uuid.UUID | None = None,
    status: str | None = None,
    # REL-30: same clamp contract as the reasoning-logs search route —
    # out-of-range values 422 instead of silently querying limit=10⁹.
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List jobs with filtering.

    Authentication required (review SEC-4): the unscoped listing previously
    exposed every user's generation prompts and queue contents to anyone.
    """
    if get_current_user_from_request(request) is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        query = session.query(GeneratorJob)
        if session_id:
            # str comparison for the VARCHAR(36) SQLite column (round-3 D6).
            query = query.filter(GeneratorJob.session_id == str(session_id))
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
    """Update session routing heartbeat.

    The id is validated against this deployment's own identity first (round-3 D7):
    an arbitrary client-supplied ``server_id`` used to be upserted verbatim, and
    ``SessionAffinityMiddleware`` then 307-redirected every request of that session
    (method + body included) to the attacker's host.
    """
    server_id = _validate_server_id(request.server_id)
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
                {"session_id": str(session_id), "server_id": server_id},
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
                    "server_id": server_id,
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
                        "server_id": server_id,
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
