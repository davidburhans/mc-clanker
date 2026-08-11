import json
import logging
import os
import struct
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.auth import get_current_user_from_request, hash_password
from app.db import DatabaseManager
from app.framework.framework_state import state
from app.models import LLMInteraction, Show, ShowAction

from .schemas import ExportStartRequest, ShowCreate, ShowUpdate
from .utils import generate_audience_password, require_show_owner

router = APIRouter()

log = logging.getLogger(__name__)

# Canonical recording format: the mixer emits stereo 16-bit PCM at 44.1kHz
# (framework_mixer.py: `(pcm * 32767).astype('<i2').tobytes()`). The WAV header
# written here must match so show/export recordings are valid, playable WAVs.
_RECORD_SAMPLE_RATE = 44100
_RECORD_CHANNELS = 2
_RECORD_SAMPLE_WIDTH = 2  # bytes per sample (16-bit)
_WAV_HEADER_SIZE = 44
# data_size + 36 must fit in a 32-bit RIFF size field.
_WAV_MAX_DATA_SIZE = 0xFFFFFFFF - 36


def _write_wav_header(handle) -> None:
    """Write a canonical 44-byte WAV header (PCM/16-bit/stereo/44.1kHz).

    RIFF + data sizes are zero placeholders patched by ``_finalize_wav`` at close.
    ``broadcast_audio`` then streams raw int16 LE PCM straight into the data chunk
    via ``handle.write()``, so the file is a valid, playable WAV with no postprocess
    (review C4 — show/export recordings were previously headerless raw PCM served
    as ``audio/wav``).
    """
    byte_rate = _RECORD_SAMPLE_RATE * _RECORD_CHANNELS * _RECORD_SAMPLE_WIDTH
    block_align = _RECORD_CHANNELS * _RECORD_SAMPLE_WIDTH
    handle.write(b"RIFF")
    handle.write(struct.pack("<I", 0))
    handle.write(b"WAVE")
    handle.write(b"fmt ")
    handle.write(
        struct.pack(
            "<IHHIIHH",
            16,
            1,
            _RECORD_CHANNELS,
            _RECORD_SAMPLE_RATE,
            byte_rate,
            block_align,
            _RECORD_SAMPLE_WIDTH * 8,
        )
    )
    handle.write(b"data")
    handle.write(struct.pack("<I", 0))


def _finalize_wav(handle) -> None:
    """Patch RIFF + data sizes from file length, then flush + close the handle."""
    if handle is None:
        return
    try:
        total = handle.tell()
    except OSError:
        total = _WAV_HEADER_SIZE
    data_size = max(0, total - _WAV_HEADER_SIZE)
    try:
        if data_size <= _WAV_MAX_DATA_SIZE:
            handle.seek(4)
            handle.write(struct.pack("<I", 36 + data_size))
            handle.seek(40)
            handle.write(struct.pack("<I", data_size))
        else:
            log.warning("Recording exceeds 4GB WAV limit; sizes left as placeholders")
    except (OSError, struct.error) as exc:
        log.warning("Could not finalize WAV header sizes: %r", exc)
    try:
        handle.flush()
    except OSError:
        pass
    try:
        handle.close()
    except OSError:
        pass


def _transition_show_to_live(session, show_id, request, audio_file_path, started_at) -> None:
    """Atomically move a show to 'live' (ownership + startable-status guard).

    Raises 404 (not owner) or 400 (not startable). The conditional UPDATE makes
    concurrent ``start_show`` requests race-safe (review C7) instead of leaking
    file handles on a double-start.
    """
    require_show_owner(show_id, request, session)
    updated = (
        session.query(Show)
        .filter(
            Show.id == show_id,
            Show.status.in_(("draft", "ended")),
        )
        .update(
            {"status": "live", "started_at": started_at, "audio_file_path": audio_file_path},
            synchronize_session=False,
        )
    )
    session.flush()
    if updated == 0:
        current = session.query(Show).filter(Show.id == show_id).first()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot start show with status '{current.status}'",
        )


def _stop_show_recording():
    """Clear show-recording flags + detach the handle under sync_lock (A1/B8).

    Returns the detached handle so the caller can finalize/close it OUTSIDE the
    lock (no I/O in the critical section). These fields are sync_lock-protected so
    ``broadcast_audio``'s snapshot is consistent with the close.
    """
    with state.sync_lock:
        show_file = state.current_show_audio_file
        state.current_show_audio_file = None
        state.is_show_recording = False
        state.current_show_id = None
        state.current_show_start_time = None
        return show_file


@router.get("/shows")
async def list_shows(request: Request, limit: int = 50, offset: int = 0):
    """List current user's shows (paginated)."""
    user = get_current_user_from_request(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        shows = (
            session.query(Show)
            .filter(Show.user_id == user.id)
            .order_by(Show.created_at.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )

        total = session.query(Show).filter(Show.user_id == user.id).count()

        return {
            "shows": [s.to_dict(include_audience_password=True) for s in shows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


@router.post("/shows", status_code=status.HTTP_201_CREATED)
async def create_show(show_data: ShowCreate, request: Request):
    """Create a new show (status=draft, auto-generate audience password)."""
    user = get_current_user_from_request(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    db_manager = DatabaseManager.get_instance()
    # Generate plaintext password BEFORE hashing so we can return it once
    plaintext_password = generate_audience_password()
    with db_manager.session() as session:
        show = Show(
            user_id=user.id,
            title=show_data.title,
            description=show_data.description,
            status="draft",
            audience_password_hash=hash_password(plaintext_password),
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
    """Start show (status→live, begin recording).

    The DB transition commits BEFORE the recording file is opened or any framework
    flags are set (review C7: a commit failure used to leak the file handle and
    leave inconsistent state). An atomic conditional UPDATE guards against
    concurrent double-starts.
    """
    async with state.lock:
        config_snapshot = {
            "bpm": state.current_bpm,
            "key": state.current_key,
            "vibe": state.user_override,
        }

    shows_dir = os.environ.get("SHOWS_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "shows"))
    show_dir = os.path.join(shows_dir, str(show_id))
    os.makedirs(show_dir, exist_ok=True)
    audio_file_path = os.path.join(show_dir, "audio.wav")
    started_at = datetime.now(timezone.utc)

    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        _transition_show_to_live(session, show_id, request, audio_file_path, started_at)
        show = session.query(Show).filter(Show.id == show_id).first()
        show.config_snapshot = config_snapshot
        response = show.to_dict(include_audience_password=True)
    # COMMIT succeeded → open the recording file (valid WAV header, C4) + set the
    # sync_lock-protected recording flags (A1/B8). Nothing is leaked on commit fail.
    audio_file = open(audio_file_path, "wb")
    _write_wav_header(audio_file)
    with state.sync_lock:
        state.is_show_recording = True
        state.current_show_id = show_id
        state.current_show_start_time = time.time()
        state.current_show_audio_file = audio_file
    async with state.lock:
        state.llm_interaction_buffer = []
        state.action_buffer = []
        state.is_show_started = True
    return response


@router.post("/shows/{show_id}/stop")
async def stop_show(show_id: int, request: Request):
    """Stop show (status→ended, finalize WAV recording).

    DB commits first; then the recording handle is finalized (valid WAV sizes
    patched, C4) and closed, and the sync_lock-protected flags cleared (A1/B8).
    """
    db_manager = DatabaseManager.get_instance()
    ended_at = datetime.now(timezone.utc)
    with db_manager.session() as session:
        require_show_owner(show_id, request, session)
        updated = (
            session.query(Show)
            .filter(
                Show.id == show_id,
                Show.status == "live",
            )
            .update({"status": "ended", "ended_at": ended_at}, synchronize_session=False)
        )
        session.flush()
        if updated == 0:
            current = session.query(Show).filter(Show.id == show_id).first()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Show is not live (status: '{current.status}')",
            )
        show = session.query(Show).filter(Show.id == show_id).first()
        if show.started_at:
            show.duration_seconds = int((ended_at - show.started_at).total_seconds())
        response = show.to_dict(include_audience_password=True)
    # COMMIT succeeded — finalize/close the WAV + clear recording flags (A1/B8/C4).
    show_file = _stop_show_recording()
    if show_file is not None:
        _finalize_wav(show_file)
    async with state.lock:
        state.is_show_started = False
    # Flush any remaining audit buffers now that recording has stopped.
    from app.framework.framework_main_async import flush_recording_buffers

    await flush_recording_buffers()
    return response


@router.post("/shows/{show_id}/archive")
async def archive_show(show_id: int, request: Request):
    """Archive a ended show."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)

        if show.status not in ("ended", "live"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cannot archive show with status '{show.status}'"
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

        actions = (
            session.query(ShowAction)
            .filter(ShowAction.show_id == show_id)
            .order_by(ShowAction.loop_index)
            .limit(limit)
            .offset(offset)
            .all()
        )

        total = session.query(ShowAction).filter(ShowAction.show_id == show_id).count()

        return {"actions": [a.to_dict() for a in actions], "total": total, "limit": limit, "offset": offset}


@router.get("/shows/{show_id}/llm-interactions")
async def get_show_llm_interactions(show_id: int, request: Request, limit: int = 1000, offset: int = 0):
    """List all LLM interactions for a show."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        require_show_owner(show_id, request, session)

        interactions = (
            session.query(LLMInteraction)
            .filter(LLMInteraction.show_id == show_id)
            .order_by(LLMInteraction.loop_index)
            .limit(limit)
            .offset(offset)
            .all()
        )

        total = session.query(LLMInteraction).filter(LLMInteraction.show_id == show_id).count()

        return {"interactions": [i.to_dict() for i in interactions], "total": total, "limit": limit, "offset": offset}


@router.get("/shows/{show_id}/audio")
async def get_show_audio(show_id: int, request: Request):
    """Download recorded audio file."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)

        if not show.audio_file_path or not os.path.exists(show.audio_file_path):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio file not found")

        return FileResponse(show.audio_file_path, media_type="audio/wav", filename=f"show_{show_id}.wav")


# =============================================================================
# EXPORT ROUTES (Fixed Issue 4.4 - No more RAM accumulation)
# =============================================================================


@router.post("/export/start")
async def start_export(req: ExportStartRequest):
    """Start recording to file (direct stream to disk). WAV header for wav (C4).

    The file is opened OUTSIDE the sync_lock (no I/O in the critical section);
    the check+set is atomic under sync_lock so two concurrent starts can't both
    win (A1/B8: is_recording/recording_file_handle are sync_lock-protected).
    """
    fmt = (req.format or "wav").lower()
    export_dir = os.environ.get("EXPORT_DIR", "/exports")
    os.makedirs(export_dir, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    file_path = os.path.join(export_dir, f"mc_clanker_{timestamp}.{fmt}")

    file_handle = open(file_path, "wb")
    if fmt == "wav":
        _write_wav_header(file_handle)

    with state.sync_lock:
        if state.is_recording:
            conflict = True
        else:
            conflict = False
            state.recording_file_handle = file_handle
            state.is_recording = True
            state.recording_format = fmt
            state.recording_file_path = file_path
            state.recording_start_time = time.time()
    if conflict:
        file_handle.close()
        raise HTTPException(status_code=400, detail="Already recording")

    return {"status": "started", "file_path": file_path}


@router.post("/export/stop")
async def stop_export():
    """Stop recording, finalize WAV (if wav), return file path."""
    with state.sync_lock:
        if not state.is_recording:
            raise HTTPException(status_code=400, detail="Not recording")
        handle = state.recording_file_handle
        file_path = state.recording_file_path
        fmt = state.recording_format
        start_time = state.recording_start_time
        state.recording_file_handle = None
        state.is_recording = False
    duration = (time.time() - start_time) if start_time else 0.0
    # Close/finalize OUTSIDE the lock (no I/O in the critical section).
    if handle is not None:
        if fmt == "wav":
            _finalize_wav(handle)
        else:
            try:
                handle.flush()
            except OSError:
                pass
            try:
                handle.close()
            except OSError:
                pass
    return {"file_path": file_path, "duration": duration}


@router.get("/shows/{show_id}/export/llm-dump")
async def export_llm_dump(show_id: int, request: Request):
    """Stream JSONL of prompt+response pairs."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        require_show_owner(show_id, request, session)

        interactions = (
            session.query(LLMInteraction)
            .filter(LLMInteraction.show_id == show_id)
            .order_by(LLMInteraction.loop_index)
            .all()
        )

        async def generate():
            for interaction in interactions:
                dump = interaction.to_llm_dump_dict()
                yield json.dumps(dump) + "\n"

        return StreamingResponse(
            generate(),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f"attachment; filename=show_{show_id}_llm_dump.jsonl"},
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
            export_data, headers={"Content-Disposition": f"attachment; filename=show_{show_id}_full.json"}
        )


@router.post("/shows/{show_id}/playback/start")
async def start_playback(show_id: int, request: Request):
    """Start pre-recorded audio playback."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)

        if show.status != "ended" and show.status != "archived":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Show must be ended or archived to playback"
            )

        if not show.audio_file_path or not os.path.exists(show.audio_file_path):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio file not found")

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
