import asyncio
import json
import logging
import os
import struct
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.auth import get_current_user_from_request, hash_password
from app.db import DatabaseManager
from app.framework.framework_state import state
from app.models import LLMInteraction, Show, ShowAction
from app.playback import ShowPlayback

from .schemas import ExportStartRequest, ShowCreate, ShowUpdate
from .utils import generate_audience_password, require_show_owner

router = APIRouter()

log = logging.getLogger(__name__)

# Live playback players by show id (review AUDIO-2). ShowPlayback used to be
# orphaned: the routes only flipped state flags, so no player ever existed and
# nothing streamed. One playback at a time — the playback flags are singular.
_active_playbacks: dict[int, ShowPlayback] = {}


def _as_naive_utc(value: datetime) -> datetime:
    """Normalize to naive UTC wall time to match the naive DateTime columns.

    Show.started_at/ended_at are ``DateTime`` (no timezone), so DB round-trips
    return naive datetimes while this module writes ``datetime.now(timezone.utc)``.
    Subtracting aware from naive raises TypeError, which crashed every stop_show
    before the recording was finalized or the audit flushed (review DATA-1).
    """
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value

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
            # Round-3 D13: a >4 GiB recording cannot encode its real length in the
            # 32-bit RIFF/data size fields. Leaving the zero placeholders made
            # every size-honoring reader (including Python's own ``wave`` module,
            # which validates the RIFF chunk) reject the WHOLE recording. Write the
            # 0xFFFFFFFF sentinel used by mainstream wav writers instead, so the
            # first 4 GiB stays playable/parseable.
            log.warning("Recording exceeds 4GB WAV limit; writing 0xFFFFFFFF size sentinels")
            handle.seek(4)
            handle.write(struct.pack("<I", 0xFFFFFFFF))
            handle.seek(40)
            handle.write(struct.pack("<I", 0xFFFFFFFF))
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


def _file_holds_bytes(path: str) -> bool:
    """True when ``path`` exists and is non-empty (a recording worth keeping)."""
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _allocate_show_audio_path(show_dir: str, started_at: datetime) -> str:
    """Return a path for THIS run's recording, never reusing a filled one (D2).

    ``shows/{id}/audio.wav`` was reused by every take and opened with ``"wb"``, so
    restarting a show (or reusing the id of a deleted one, SQLite reuses rowids)
    O_TRUNC'd the previous recording before a single new sample existed. The first
    take keeps the canonical name, a later take gets ``audio_<started_at>.wav``.

    Example: second run of show 7 -> ``shows/7/audio_20250101T120000123456.wav``.
    """
    canonical = os.path.join(show_dir, "audio.wav")
    if not _file_holds_bytes(canonical):
        return canonical
    stamped = os.path.join(show_dir, f"audio_{started_at.strftime('%Y%m%dT%H%M%S%f')}.wav")
    if not _file_holds_bytes(stamped):
        return stamped
    return os.path.join(show_dir, f"audio_{uuid.uuid4().hex}.wav")


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


def _stop_show_recording(show_id: int):
    """Clear show-recording flags + detach the handle under sync_lock (A1/B8).

    Returns the detached handle so the caller can finalize/close it OUTSIDE the
    lock (no I/O in the critical section). These fields are sync_lock-protected so
    ``broadcast_audio``'s snapshot is consistent with the close.

    Only detaches when ``show_id`` actually owns the live recording: stopping a
    stale 'live' row must not finalize/close another show's in-flight handle
    (review DATA-5 — starting show B orphaned A's handle, then stopping A
    killed B's recording).
    """
    with state.sync_lock:
        if state.current_show_id != show_id:
            return None
        show_file = state.current_show_audio_file
        state.current_show_audio_file = None
        state.is_show_recording = False
        state.current_show_id = None
        state.current_show_start_time = None
        return show_file


def _current_recording_show_id() -> int | None:
    """Show id currently owning the live recording (sync_lock-protected)."""
    with state.sync_lock:
        return state.current_show_id


async def _release_show_started_flag_if_idle() -> None:
    """Clear the audience-facing "show started" flag only when no show records.

    Round-3 D4: ``stop_show`` used to clear it unconditionally, so stopping a
    stale 'live' row advertised "no show running" while a DIFFERENT show was still
    recording and streaming to the audience.
    """
    if _current_recording_show_id() is not None:
        return
    async with state.lock:
        state.is_show_started = False


async def _teardown_live_recording(show_id: int) -> bool:
    """Finalize + detach the live recording when ``show_id`` owns it (round-3 D1).

    Required by the delete/archive paths: without it, removing or archiving the
    LIVE show left ``state.current_show_id``/``is_show_recording`` set forever —
    ``stop_show`` could no longer match the row, every later ``start_show`` 409'd,
    and the mixer kept writing PCM into an orphaned handle. Only a restart
    recovered.

    Returns True when this show actually owned (and stopped) the recording.
    """
    show_file = _stop_show_recording(show_id)
    owned = show_file is not None
    if owned:
        # Finalize under sync_lock, same precedent as stop_show (CONC-4).
        with state.sync_lock:
            _finalize_wav(show_file)
    await _release_show_started_flag_if_idle()
    return owned


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
    """Delete show + all related data.

    Round-3 D1: tearing down the recording BEFORE deleting the row is mandatory —
    otherwise the row that ``stop_show`` needs in order to clear the recording
    state is gone and the recording subsystem stays wedged until a restart.
    """
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)
        await _teardown_live_recording(show_id)
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
    # Refuse when a recording is already live BEFORE touching the DB (DATA-5):
    # a second live show would overwrite the sync_lock-protected handle slot,
    # orphaning the first show's still-open file.
    with state.sync_lock:
        if state.current_show_id is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Another show (id {state.current_show_id}) is currently recording; stop it first",
            )

    async with state.lock:
        config_snapshot = {
            "bpm": state.current_bpm,
            "key": state.current_key,
            "vibe": state.user_override,
        }

    shows_dir = os.environ.get("SHOWS_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "shows"))
    show_dir = os.path.join(shows_dir, str(show_id))
    os.makedirs(show_dir, exist_ok=True)
    started_at = _as_naive_utc(datetime.now(timezone.utc))
    # Round-3 D2: allocate a fresh path instead of O_TRUNC'ing the previous take.
    audio_file_path = _allocate_show_audio_path(show_dir, started_at)

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
    # Reset the audit buffers BEFORE enabling recording (CONC-2): the old order
    # cleared them in a separate state.lock section AFTER current_show_id went
    # live, so an append_loop_audit landing in that gap — or rows re-queued by a
    # previously failed flush — was silently discarded by the rebinding. No await
    # sits between this reset and the sync_lock enable below, so no append can
    # run in between on the event loop.
    async with state.lock:
        state.llm_interaction_buffer = []
        state.action_buffer = []
    with state.sync_lock:
        state.is_show_recording = True
        state.current_show_id = show_id
        state.current_show_start_time = time.time()
        state.current_show_audio_file = audio_file
    async with state.lock:
        state.is_show_started = True
    return response


@router.post("/shows/{show_id}/stop")
async def stop_show(show_id: int, request: Request):
    """Stop show (status→ended, finalize WAV recording).

    DB commits first; then the recording handle is finalized (valid WAV sizes
    patched, C4) and closed, and the sync_lock-protected flags cleared (A1/B8).
    """
    db_manager = DatabaseManager.get_instance()
    ended_at = _as_naive_utc(datetime.now(timezone.utc))
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
    show_file = _stop_show_recording(show_id)
    if show_file is not None:
        # Finalize under sync_lock (CONC-4): the mixer thread snapshots this
        # handle under the same lock, so finalizing unlocked could interleave a
        # stale tick's pcm write between the header seeks and silently corrupt
        # the RIFF sizes. One-shot stop path — same lock-across-file-I/O
        # precedent as framework_state._close_recording_handles_locked.
        with state.sync_lock:
            _finalize_wav(show_file)
    # Round-3 D4: only clear the audience-facing flag when THIS show owned the
    # recording (or nothing is recording at all).
    await _release_show_started_flag_if_idle()
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

        # Round-3 D1: archiving a LIVE show used to leave the recording live with no
        # reachable way back — stop_show then 400s on status != 'live' forever.
        await _teardown_live_recording(show_id)
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

    # Round-3 D3: the conflict check must happen BEFORE the O_TRUNC open. The
    # second-resolution filename means a duplicate (rejected) start resolved to the
    # ACTIVE export's own path and truncated it before returning 400.
    # The slot is claimed under sync_lock (handle stays None until the open wins),
    # so check+set remains atomic while no rejected request ever touches a file.
    with state.sync_lock:
        if state.is_recording:
            conflict = True
        else:
            conflict = False
            state.recording_file_handle = None
            state.is_recording = True
            state.recording_format = fmt
            state.recording_file_path = file_path
            state.recording_start_time = time.time()
    if conflict:
        raise HTTPException(status_code=400, detail="Already recording")

    try:
        file_handle = open(file_path, "wb")
    except OSError as exc:
        _release_export_claim()
        raise HTTPException(status_code=500, detail=f"Could not open export file {file_path}: {exc}") from exc
    if fmt == "wav":
        _write_wav_header(file_handle)
    with state.sync_lock:
        state.recording_file_handle = file_handle

    return {"status": "started", "file_path": file_path}


def _release_export_claim() -> None:
    """Roll back an export slot claimed under sync_lock when the open fails (D3)."""
    with state.sync_lock:
        state.is_recording = False
        state.recording_file_handle = None
        state.recording_file_path = None
        state.recording_start_time = None


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
    """Start pre-recorded audio playback.

    Instantiates the ShowPlayback player (review AUDIO-2: the routes used to
    only flip state flags, so no player ever existed and nothing streamed).
    """
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)

        if show.status != "ended" and show.status != "archived":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Show must be ended or archived to playback"
            )

        if not show.audio_file_path or not os.path.exists(show.audio_file_path):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio file not found")

        audio_file_path = show.audio_file_path

    # Only one playback at a time; retire EVERY live player first, not just
    # this show's — the playback flags are singular, and a stale player from
    # another show would keep looping forever (ShowPlayback rewinds on EOF),
    # double-broadcasting audio (review AUDIO-2 follow-up). stop() joins the
    # streaming thread, so keep that blocking join off the event loop.
    retiring = list(_active_playbacks.values())
    _active_playbacks.clear()
    for stale_player in retiring:
        await asyncio.get_running_loop().run_in_executor(None, stale_player.stop)

    player = ShowPlayback(show_id=show_id, audio_file_path=audio_file_path)
    result = player.start()
    if result.get("status") != "started":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(result.get("message", "Playback failed to start")),
        )
    _active_playbacks[show_id] = player

    return {"status": "ok", "show_id": show_id}


@router.post("/shows/{show_id}/playback/stop")
async def stop_playback_route(show_id: int, request: Request):
    """Stop playback."""
    player = _active_playbacks.pop(show_id, None)
    if player is not None:
        # stop() joins the playback thread (up to 5s) — run it off the loop.
        await asyncio.get_running_loop().run_in_executor(None, player.stop)
    else:
        # No live player (e.g. stale flags after a restart) — clear directly.
        async with state.lock:
            state.is_playback_active = False
            state.currently_playing_show_id = None

    return {"status": "ok"}
