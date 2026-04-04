import os
import time
import json
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from datetime import datetime, timezone
import threading

from app.framework.framework_state import state
from app.db import DatabaseManager
from app.models import Show, ShowAction, LLMInteraction
from .schemas import (
    ShowCreate, ShowUpdate, ExportStartRequest
)
from .utils import require_show_owner, generate_audience_password
from app.auth import get_current_user_from_request, hash_password

router = APIRouter()

@router.get("/shows")
async def list_shows(request: Request, limit: int = 50, offset: int = 0):
    """List current user's shows (paginated)."""
    user = get_current_user_from_request(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )

    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        shows = session.query(Show).filter(
            Show.user_id == user.id
        ).order_by(Show.created_at.desc()).limit(limit).offset(offset).all()

        total = session.query(Show).filter(Show.user_id == user.id).count()

        return {
            "shows": [s.to_dict(include_audience_password=True) for s in shows],
            "total": total,
            "limit": limit,
            "offset": offset
        }

@router.post("/shows", status_code=status.HTTP_201_CREATED)
async def create_show(show_data: ShowCreate, request: Request):
    """Create a new show (status=draft, auto-generate audience password)."""
    user = get_current_user_from_request(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )

    db_manager = DatabaseManager.get_instance()
    # Generate plaintext password BEFORE hashing so we can return it once
    plaintext_password = generate_audience_password()
    with db_manager.session() as session:
        show = Show(
            user_id=user.id,
            title=show_data.title,
            description=show_data.description,
            status="draft",
            audience_password_hash=hash_password(plaintext_password)
        )
        session.add(show)
        session.flush()
        session.refresh(show)

        response = show.to_dict(include_audience_password=True)
        # Return plaintext password exactly once — user must save it now
        response["audience_password"] = plaintext_password
        return response

@router.get("/shows/{show_id}")
async def get_show(show_id: int, request: Request):
    """Get show details (includes audience password for owner)."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)
        return show.to_dict(include_audience_password=True)

@router.patch("/shows/{show_id}")
async def update_show(show_id: int, update: ShowUpdate, request: Request):
    """Update show metadata."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)

        if update.title is not None:
            show.title = update.title
        if update.description is not None:
            show.description = update.description

        return show.to_dict(include_audience_password=True)

@router.delete("/shows/{show_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_show(show_id: int, request: Request):
    """Delete show + all related data."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)
        session.delete(show)

@router.post("/show/stop")
async def stop_current_show():
    """Global stop — ends any active show in framework."""
    async with state.lock:
        state.is_show_started = False
        # Do not reset everything, just stop the show flags
    return {"status": "ok"}


@router.post("/shows/{show_id}/start")
async def start_show(show_id: int, request: Request):
    """Start show (status→live, begin recording)."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)

        if show.status not in ("draft", "ended"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot start show with status '{show.status}'"
            )

        show.status = "live"
        show.started_at = datetime.now(timezone.utc)

        # Create show directory and audio file
        shows_dir = os.environ.get("SHOWS_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "shows"))
        show_dir = os.path.join(shows_dir, str(show_id))
        os.makedirs(show_dir, exist_ok=True)
        audio_file_path = os.path.join(show_dir, "audio.wav")
        show.audio_file_path = audio_file_path

        # Capture current config snapshot
        async with state.lock:
            show.config_snapshot = {
                "bpm": state.current_bpm,
                "key": state.current_key,
                "vibe": state.user_override,
            }

        # Open audio file for writing (raw PCM, 44100 Hz, 16-bit, stereo)
        audio_file = open(audio_file_path, "wb")

        # Start recording in framework
        async with state.lock:
            state.is_show_recording = True
            state.current_show_id = show_id
            state.current_show_start_time = time.time()
            state.llm_interaction_buffer = []
            state.action_buffer = []
            state.current_show_audio_file = audio_file
            state.is_show_started = True

        return show.to_dict(include_audience_password=True)

@router.post("/shows/{show_id}/stop")
async def stop_show(show_id: int, request: Request):
    """Stop show (status→ended, finalize recording)."""
    db_manager = DatabaseManager.get_instance()

    # Validate ownership BEFORE closing the file
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)

        if show.status != "live":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Show is not live (status: '{show.status}')"
            )

        show.status = "ended"
        show.ended_at = datetime.now(timezone.utc)

        # Calculate duration
        if show.started_at:
            show.duration_seconds = int((show.ended_at - show.started_at).total_seconds())

        # Close audio file
        async with state.lock:
            if state.current_show_audio_file is not None:
                try:
                    state.current_show_audio_file.close()
                except Exception:
                    pass
                state.current_show_audio_file = None
            state.is_show_recording = False
            state.current_show_id = None
            state.current_show_start_time = None
            state.is_show_started = False

        # Flush any remaining buffers
        from app.framework.framework_main_async import flush_recording_buffers
        await flush_recording_buffers()

        return show.to_dict(include_audience_password=True)

@router.post("/shows/{show_id}/archive")
async def archive_show(show_id: int, request: Request):
    """Archive a ended show."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)

        if show.status not in ("ended", "live"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot archive show with status '{show.status}'"
            )

        show.status = "archived"
        return show.to_dict(include_audience_password=True)

@router.post("/shows/{show_id}/regenerate-audience-password")
async def regenerate_audience_password_route(show_id: int, request: Request):
    """Generate new audience password for a show."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)

        new_password = generate_audience_password()
        show.audience_password_hash = hash_password(new_password)

        return {"audience_password": new_password}

@router.get("/shows/{show_id}/actions")
async def get_show_actions(show_id: int, request: Request, limit: int = 1000, offset: int = 0):
    """List all actions for a show."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        require_show_owner(show_id, request, session)

        actions = session.query(ShowAction).filter(
            ShowAction.show_id == show_id
        ).order_by(ShowAction.loop_index).limit(limit).offset(offset).all()

        total = session.query(ShowAction).filter(ShowAction.show_id == show_id).count()

        return {
            "actions": [a.to_dict() for a in actions],
            "total": total,
            "limit": limit,
            "offset": offset
        }

@router.get("/shows/{show_id}/llm-interactions")
async def get_show_llm_interactions(show_id: int, request: Request, limit: int = 1000, offset: int = 0):
    """List all LLM interactions for a show."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        require_show_owner(show_id, request, session)

        interactions = session.query(LLMInteraction).filter(
            LLMInteraction.show_id == show_id
        ).order_by(LLMInteraction.loop_index).limit(limit).offset(offset).all()

        total = session.query(LLMInteraction).filter(LLMInteraction.show_id == show_id).count()

        return {
            "interactions": [i.to_dict() for i in interactions],
            "total": total,
            "limit": limit,
            "offset": offset
        }

@router.get("/shows/{show_id}/audio")
async def get_show_audio(show_id: int, request: Request):
    """Download recorded audio file."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)

        if not show.audio_file_path or not os.path.exists(show.audio_file_path):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Audio file not found"
            )

        return FileResponse(
            show.audio_file_path,
            media_type="audio/wav",
            filename=f"show_{show_id}.wav"
        )


# =============================================================================
# EXPORT ROUTES (Fixed Issue 4.4 - No more RAM accumulation)
# =============================================================================

@router.post("/export/start")
async def start_export(req: ExportStartRequest):
    """Start recording to file (direct stream to disk)."""
    export_dir = os.environ.get("EXPORT_DIR", "/exports")
    os.makedirs(export_dir, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"mc_clanker_{timestamp}.{req.format}"
    file_path = os.path.join(export_dir, filename)

    async with state.lock:
        if state.is_recording:
            raise HTTPException(status_code=400, detail="Already recording")

        # Open file immediately (Issue 4.4 fix)
        state.recording_file_handle = open(file_path, "wb")
        state.is_recording = True
        state.recording_format = req.format
        state.recording_file_path = file_path
        state.recording_start_time = time.time()
        state.recording_chunks = [] # Keep for legacy but we will stream

    return {"status": "started", "file_path": file_path}

@router.post("/export/stop")
async def stop_export():
    """Stop recording and return file path"""
    async with state.lock:
        if not state.is_recording:
            raise HTTPException(status_code=400, detail="Not recording")

        file_path = state.recording_file_path
        duration = time.time() - state.recording_start_time
        
        # Close handle (Issue 4.4 fix)
        if hasattr(state, "recording_file_handle") and state.recording_file_handle:
            state.recording_file_handle.close()
            state.recording_file_handle = None

        state.is_recording = False
        state.recording_chunks = []

    return {"file_path": file_path, "duration": duration}

@router.get("/shows/{show_id}/export/llm-dump")
async def export_llm_dump(show_id: int, request: Request):
    """Stream JSONL of prompt+response pairs."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        require_show_owner(show_id, request, session)

        interactions = session.query(LLMInteraction).filter(
            LLMInteraction.show_id == show_id
        ).order_by(LLMInteraction.loop_index).all()

        async def generate():
            for interaction in interactions:
                dump = interaction.to_llm_dump_dict()
                yield json.dumps(dump) + "\n"

        return StreamingResponse(
            generate(),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f"attachment; filename=show_{show_id}_llm_dump.jsonl"}
        )

@router.get("/shows/{show_id}/export/full")
async def export_full_show(show_id: int, request: Request):
    """Download full show (audio + JSON of actions/interactions)."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)
        actions = session.query(ShowAction).filter(ShowAction.show_id == show_id).all()
        interactions = session.query(LLMInteraction).filter(LLMInteraction.show_id == show_id).all()

        export_data = {
            "show": show.to_dict(),
            "actions": [a.to_dict() for a in actions],
            "llm_interactions": [i.to_dict() for i in interactions],
        }

        return JSONResponse(
            export_data,
            headers={"Content-Disposition": f"attachment; filename=show_{show_id}_full.json"}
        )

@router.post("/shows/{show_id}/playback/start")
async def start_playback(show_id: int, request: Request):
    """Start pre-recorded audio playback."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)

        if show.status != "ended" and show.status != "archived":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Show must be ended or archived to playback"
            )

        if not show.audio_file_path or not os.path.exists(show.audio_file_path):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Audio file not found"
            )

        # Set playback state
        async with state.lock:
            state.currently_playing_show_id = show_id
            state.is_playback_active = True

        return {"status": "ok", "show_id": show_id}

@router.post("/shows/{show_id}/playback/stop")
async def stop_playback_route(show_id: int, request: Request):
    """Stop playback."""
    async with state.lock:
        state.is_playback_active = False
        state.currently_playing_show_id = None

    return {"status": "ok"}
