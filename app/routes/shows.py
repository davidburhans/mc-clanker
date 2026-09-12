import asyncio
import fnmatch
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse

from app.auth import get_current_user_from_request, hash_password
from app.db import DatabaseManager
from app.framework.audit_recording import drop_buffered_rows_for_show, flush_recording_buffers
from app.framework.framework_state import state
from app.framework.recording_sink import RecordingSink
from app.lib.export_chunks import chunked_shaped_rows, ndjson_lines
from app.lib.paths import exports_dir, recordings_dir
from app.lib.wav import write_wav_header as _write_wav_header
from app.models import LLMInteraction, Show, ShowAction
from app.playback import ShowPlayback

from .schemas import ExportStartRequest, ShowCreate, ShowUpdate
from .utils import fetch_owned_show, generate_audience_password, require_show_owner

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
    """Clear show-recording flags + detach the sink under sync_lock (A1/B8).

    Returns the detached RecordingSink so the caller can ``stop_and_finalize()``
    it OUTSIDE the lock (REL-11: no I/O in the critical section — the sink's
    writer thread is the single owner of the handle and drains, flushes and
    patches the WAV sizes itself).

    Only detaches when ``show_id`` actually owns the live recording: stopping a
    stale 'live' row must not stop another show's in-flight recording
    (review DATA-5 — starting show B orphaned A's handle, then stopping A
    killed B's recording).
    """
    with state.sync_lock:
        if state.current_show_id != show_id:
            return None
        show_sink = state.current_show_sink
        state.current_show_sink = None
        state.is_show_recording = False
        state.current_show_id = None
        state.current_show_start_time = None
        return show_sink


def _current_recording_show_id() -> int | None:
    """Show id currently owning the live recording (sync_lock-protected)."""
    with state.sync_lock:
        return state.current_show_id


def _unlink_persisted_take(audio_file_path) -> int:
    """Unlink the row-persisted take, if it looks like one of ours (REL-05).

    The basename guard keeps a corrupted row (or a hand-edited path) from making
    the delete route unlink an arbitrary file: only ``audio*.wav`` recordings
    this module writes are eligible. Returns 0/1.
    """
    if not isinstance(audio_file_path, str) or not fnmatch.fnmatch(os.path.basename(audio_file_path), "audio*.wav"):
        return 0
    if not os.path.isfile(audio_file_path):
        return 0
    try:
        os.unlink(audio_file_path)
        return 1
    except OSError as exc:
        print(f"delete_show: could not unlink persisted take {audio_file_path}: {exc}")
        return 0


def _sweep_show_dir(show_id: int) -> int:
    """Unlink every recording take under ``shows/{id}/`` (REL-05).

    Stamped/uuid takes (round-3 D2) are never persisted anywhere, so the sweep
    is the only way to reach them. Per-file OSError isolation: one bad inode
    never prevents the remaining takes from being reclaimed. Returns the count
    removed; a missing dir removes 0.
    """
    show_dir = os.path.join(recordings_dir(), str(show_id))
    removed = 0
    try:
        entries = list(os.scandir(show_dir))
    except OSError:
        return 0  # never created, already gone, or unreadable — nothing to GC
    for entry in entries:
        if not entry.is_file(follow_symlinks=False):
            continue
        try:
            os.unlink(entry.path)
            removed += 1
        except OSError as exc:
            print(f"delete_show: could not unlink {entry.path}: {exc}")
    if removed:
        try:
            os.rmdir(show_dir)  # best-effort: succeeds only when fully emptied
        except OSError:
            pass
    return removed


def _delete_show_audio_files(show_id: int, audio_file_path) -> int:
    """Unlink every recording file owned by a deleted show (REL-05).

    Two sources, unioned: the persisted ``audio_file_path`` (survives a
    SHOWS_DIR change) and a sweep of ``shows/{id}/`` — stamped/uuid takes are
    never persisted anywhere, so the sweep is the only way to reach them.
    Per-file OSError isolation; a missing dir/None path removes 0. Returns the
    file count removed.

    Example: show 7 with audio.wav + audio_20240101T120000123456.wav on disk
    and ``audio.wav`` persisted on the row → 2 removed, ``shows/7/`` pruned.
    """
    removed = _unlink_persisted_take(audio_file_path)
    removed += _sweep_show_dir(show_id)
    if removed:
        print(f"delete_show {show_id}: removed {removed} audio file(s)")
    return removed


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
    show_sink = _stop_show_recording(show_id)
    owned = show_sink is not None
    if owned:
        # REL-11: the sink's writer drains + finalizes; correctness moved from
        # lock-across-I/O (the old CONC-4 argument) to single-owner
        # drain-then-finalize, so no sync_lock wraps this.
        show_sink.stop_and_finalize()
    await _release_show_started_flag_if_idle()
    return owned


@router.get("/shows")
async def list_shows(
    request: Request,
    # REL-30: same clamp contract as the reasoning-logs search route —
    # out-of-range values 422 instead of silently querying limit=10⁹.
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
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
        audio_file_path = show.audio_file_path  # read pre-commit; row expires after
        session.delete(show)
    # REL-14 (U4): buffered rows still referencing the deleted show can never
    # insert (its audit history was cascade-deleted) and would FK-poison every
    # future flush — the failed batch re-prepends, so flushes fail identically
    # until restart. Deliberately drop ONLY this show's rows, loudly (invariant
    # 4: every drop is a counted, logged decision — never silent). After the
    # delete COMMIT so a failed delete never discards recoverable rows.
    # REL-05a: the row is gone — its audio must go too, or 635 MB/hr of takes
    # orphan on disk forever (stamped/uuid takes were reachable by nothing).
    # Retire a live playback FIRST: unlinking under a zombie player would leave
    # it looping a deleted file (stop_playback_route's executor-stop pattern).
    player = _active_playbacks.pop(show_id, None)
    if player is not None:
        await asyncio.get_running_loop().run_in_executor(None, player.stop)
    _delete_show_audio_files(show_id, audio_file_path)
    dropped = await drop_buffered_rows_for_show(show_id)
    if dropped:
        print(f"delete_show {show_id}: dropped {dropped} buffered audit rows referencing the deleted show")


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

    shows_dir = recordings_dir()  # SHOWS_DIR at call time (app.lib.paths)
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
    # REL-14 (amended, U4): persist the previous show's pending rows instead of
    # silently discarding them — the buffers ARE the fine-tuning corpus
    # (invariant 4). Buffered rows carry their own show_id, so they flush even
    # though current_show_id is still unset here. A successful flush empties
    # both buffers (append_loop_audit no-ops while no show records), so the old
    # unconditional clear would be a no-op on success — but after a FAILED
    # flush it re-deleted the rows the flush had just re-queued (recreating the
    # silent-discard bug this code replaces). Rows surviving a failed flush
    # still reference an existing show, so they are RETAINED (loudly) for the
    # next periodic/stop flush. No await sits between this point and the
    # sync_lock enable below (CONC-2): appends cannot land in the gap because
    # append_loop_audit no-ops while current_show_id is None.
    await flush_recording_buffers()
    async with state.lock:
        retained_llm = len(state.llm_interaction_buffer)
        retained_actions = len(state.action_buffer)
    if retained_llm or retained_actions:
        print(
            f"start_show: retaining {retained_llm} buffered llm rows + {retained_actions} action rows "
            f"after a failed flush (they will persist on the next flush)"
        )
    # REL-11: arm the sink writer over the opened file BEFORE the flags go live —
    # the flags still gate broadcast_audio's snapshot, so the missed-tick window
    # is byte-identical to the pre-writer code (flags set last, under the lock).
    show_sink = RecordingSink(audio_file, "show", state)
    show_sink.start()
    with state.sync_lock:
        state.is_show_recording = True
        state.current_show_id = show_id
        state.current_show_start_time = time.time()
        state.current_show_sink = show_sink
        # REL-05c: a new recording starts from a clean per-sink fault slate.
        state.recording_write_errors["show"] = 0
        state.recording_stop_reasons["show"] = None
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
    # COMMIT succeeded — stop the recording + clear the flags (A1/B8/C4).
    show_sink = _stop_show_recording(show_id)
    if show_sink is not None:
        # REL-11: the sink's writer thread is the single owner — it drains the
        # queued blocks, flushes and patches the WAV sizes. No sync_lock here:
        # the old CONC-4 lock-across-finalize is retired by single-owner
        # finalize (submit-drops-after-stop make a stale tick's interleave
        # impossible, and only the writer ever touches the handle).
        show_sink.stop_and_finalize()
    # Round-3 D4: only clear the audience-facing flag when THIS show owned the
    # recording (or nothing is recording at all).
    await _release_show_started_flag_if_idle()
    # Flush any remaining audit buffers now that recording has stopped.
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
async def get_show_actions(
    show_id: int,
    request: Request,
    # REL-30: viewer default 1000 preserved; clamp bounds the worst case.
    limit: int = Query(1000, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
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
async def get_show_llm_interactions(
    show_id: int,
    request: Request,
    # REL-30: viewer default 1000 preserved; clamp bounds the worst case.
    limit: int = Query(1000, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
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


def _resolve_owned_show_audio_path(show_id: int, request: Request) -> str:
    """Owner-gated audio path for GET /shows/{show_id}/audio (REL-09: worker thread).

    Raises the same HTTPExceptions (401/404) require_show_owner does; to_thread
    re-raises them at the await point for FastAPI to convert.
    """
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)
        if not show.audio_file_path or not os.path.exists(show.audio_file_path):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio file not found")
        return show.audio_file_path


@router.get("/shows/{show_id}/audio")
async def get_show_audio(show_id: int, request: Request):
    """Download recorded audio file (REL-09: gate+stat off-loop; FileResponse streams off-loop)."""
    audio_path = await asyncio.to_thread(_resolve_owned_show_audio_path, show_id, request)
    return FileResponse(audio_path, media_type="audio/wav", filename=f"show_{show_id}.wav")


# =============================================================================
# EXPORT ROUTES (Fixed Issue 4.4 - No more RAM accumulation)
# =============================================================================


@router.post("/export/start")
async def start_export(req: ExportStartRequest):
    """Start recording to file (direct stream to disk). WAV header for wav (C4).

    The file is opened OUTSIDE the sync_lock (no I/O in the critical section);
    the check+set is atomic under sync_lock so two concurrent starts can't both
    win (A1/B8: is_recording/export_sink are sync_lock-protected).
    """
    fmt = (req.format or "wav").lower()
    export_dir = exports_dir()  # EXPORT_DIR at call time (app.lib.paths)
    os.makedirs(export_dir, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    file_path = os.path.join(export_dir, f"mc_clanker_{timestamp}.{fmt}")

    # Round-3 D3: the conflict check must happen BEFORE the O_TRUNC open. The
    # second-resolution filename means a duplicate (rejected) start resolved to the
    # ACTIVE export's own path and truncated it before returning 400.
    # The slot is claimed under sync_lock (the sink is armed only after the open
    # wins), so check+set remains atomic while no rejected request ever touches a
    # file.
    with state.sync_lock:
        if state.is_recording:
            conflict = True
        else:
            conflict = False
            state.export_sink = None
            state.is_recording = True
            state.recording_format = fmt
            state.recording_file_path = file_path
            state.recording_start_time = time.time()
            # REL-05c: a new recording starts from a clean per-sink fault slate.
            state.recording_write_errors["export"] = 0
            state.recording_stop_reasons["export"] = None
    if conflict:
        raise HTTPException(status_code=400, detail="Already recording")

    try:
        file_handle = open(file_path, "wb")
    except OSError as exc:
        _release_export_claim()
        raise HTTPException(status_code=500, detail=f"Could not open export file {file_path}: {exc}") from exc
    if fmt == "wav":
        _write_wav_header(file_handle)
    # REL-11: hand the handle to a writer-thread sink; broadcast_audio only
    # submit()s into its bounded queue (wav=False exports must never get a RIFF
    # header patched, so the mode rides on the sink).
    export_sink = RecordingSink(file_handle, "export", state, wav=(fmt == "wav"))
    export_sink.start()
    with state.sync_lock:
        state.export_sink = export_sink

    return {"status": "started", "file_path": file_path}


def _release_export_claim() -> None:
    """Roll back an export slot claimed under sync_lock when the open fails (D3)."""
    with state.sync_lock:
        state.is_recording = False
        state.export_sink = None
        state.recording_file_path = None
        state.recording_start_time = None


@router.post("/export/stop")
async def stop_export():
    """Stop recording, finalize WAV (if wav), return file path.

    REL-11: the detached sink's writer drains, flushes and — for wav — patches
    the RIFF sizes, so the route's manual finalize branch is gone (the sink
    knows its own wav mode). Stop runs OUTSIDE the lock (no I/O in the critical
    section).
    """
    with state.sync_lock:
        if not state.is_recording:
            raise HTTPException(status_code=400, detail="Not recording")
        export_sink = state.export_sink
        file_path = state.recording_file_path
        start_time = state.recording_start_time
        state.export_sink = None
        state.is_recording = False
        # Review P2: mirror the other detach paths — clear the stale path/time
        # so nothing downstream can mistake a stopped export for a live one.
        state.recording_file_path = None
        state.recording_start_time = None
    duration = (time.time() - start_time) if start_time else 0.0
    if export_sink is not None:
        export_sink.stop_and_finalize()
    return {"file_path": file_path, "duration": duration}


def _chunked_show_rows(db_manager, model, show_id: int, shaper):
    """Page-bounded keyset scan of one show's rows, ``shaper``-shaped in-session (REL-13).

    (loop_index, id) order keeps loop order while making the previously
    arbitrary within-loop tie order deterministic; every chunk re-applies the
    show_id scope (test T3). The shaper is the row's single source of truth
    (to_dict for API rows, to_llm_dump_dict for the training corpus).
    """
    return chunked_shaped_rows(
        db_manager,
        lambda session: session.query(model).filter(model.show_id == show_id),
        (model.loop_index, model.id),
        (model.loop_index, model.id),
        shaper,
        lambda row: (row.loop_index, row.id),
    )


def _json_array_fragments(row_dicts):
    """Yield a JSON array body piecewise (comma-joined items, no full materialize)."""
    separator = ""
    for row in row_dicts:
        yield f"{separator}{json.dumps(row)}"
        separator = ","


def _full_show_json_fragments(db_manager, show_id: int, show_dict: dict):
    """Stream the /export/full JSON document chunk-by-chunk (REL-13b).

    Same document contract as the old materialized JSONResponse —
    {"show", "actions", "llm_interactions"} — only the loading strategy
    changed: each table streams through page-bounded scans instead of two
    corpus-sized .all() calls.
    """
    yield '{"show": ' + json.dumps(show_dict) + ', "actions": ['
    yield from _json_array_fragments(_chunked_show_rows(db_manager, ShowAction, show_id, ShowAction.to_dict))
    yield '], "llm_interactions": ['
    yield from _json_array_fragments(_chunked_show_rows(db_manager, LLMInteraction, show_id, LLMInteraction.to_dict))
    yield "]}"


@router.get("/shows/{show_id}/export/llm-dump")
async def export_llm_dump(show_id: int, request: Request):
    """Stream JSONL of prompt+response pairs."""
    db_manager = DatabaseManager.get_instance()
    # REL-13: sequenced auth — the ownership gate closes its session before
    # the chunks below each open their own (never two open at once).
    fetch_owned_show(db_manager, show_id, request)

    # REL-13: rows are shaped to plain dicts inside each chunk's session (the
    # old code iterated ORM instances after the session committed+expired them
    # → DetachedInstanceError mid-stream, truncating the training corpus).
    rows = _chunked_show_rows(db_manager, LLMInteraction, show_id, LLMInteraction.to_llm_dump_dict)
    return StreamingResponse(
        ndjson_lines(rows),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f"attachment; filename=show_{show_id}_llm_dump.jsonl"},
    )


@router.get("/shows/{show_id}/export/full")
async def export_full_show(show_id: int, request: Request):
    """Download full show (audio + JSON of actions/interactions)."""
    db_manager = DatabaseManager.get_instance()
    # REL-13: sequenced auth; the show row is expunged with all columns
    # loaded, so this detached read is safe (SEC-5 precedent).
    show = fetch_owned_show(db_manager, show_id, request)
    show_dict = show.to_dict()

    return StreamingResponse(
        _full_show_json_fragments(db_manager, show_id, show_dict),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=show_{show_id}_full.json"},
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
