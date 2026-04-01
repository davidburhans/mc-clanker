from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, List
import threading
import os
import time
import json
import uuid

from app.framework.framework_state import state

router = APIRouter()


# Pydantic models for request/response
class StateUpdate(BaseModel):
    is_generating: Optional[bool] = None
    is_show_started: Optional[bool] = None
    should_reset: Optional[bool] = None
    user_override: Optional[str] = None
    target_bpm_override: Optional[int] = None
    target_key_override: Optional[str] = None
    available_instruments: Optional[List[str]] = None


class LLMConfig(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    icecast_enabled: Optional[bool] = None
    audience_password: Optional[str] = None


class GenerationConfig(BaseModel):
    cfg_scale: Optional[float] = None
    steps: Optional[int] = None


class StemVolumeUpdate(BaseModel):
    volume: float


class ExportStartRequest(BaseModel):
    format: str = "wav"


class ExportStopResponse(BaseModel):
    file_path: Optional[str]
    status: str


class CustomStemCreate(BaseModel):
    instrument: str
    prompt: str
    model_id: str = "default"


class AudienceMessage(BaseModel):
    message: str


# =============================================================================
# JOB MODELS
# =============================================================================

class JobSubmission(BaseModel):
    """Request model for submitting a generation job."""
    session_id: uuid.UUID
    instrument: str
    prompt: str
    major_family: Optional[str] = None
    model_id: str = "foundation-1"
    key: Optional[str] = None
    bpm: Optional[int] = None
    timbre_tags: List[str] = []
    bars: int = 4


class JobResponse(BaseModel):
    """Response model for job status."""
    id: str
    session_id: str
    instrument: str
    prompt: str
    major_family: Optional[str]
    model_id: str
    key: Optional[str]
    bpm: Optional[int]
    timbre_tags: List[str]
    bars: int
    status: str
    priority: int
    created_at: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]
    audio_path: Optional[str]
    duration_seconds: Optional[float]
    error_message: Optional[str]
    worker_id: Optional[str]
    expires_at: Optional[str]


class AudioResponse(BaseModel):
    """Response model for audio streaming."""
    audio_url: str
    duration_seconds: Optional[float]


# State endpoint
@router.get("/api/state")
async def get_state():
    """Return full current state (BPM, key, stems, reasoning, etc.)"""
    with state.lock:
        return {
            "current_set_name": getattr(
                state, "current_set_name", "Waiting for track..."
            ),
            "current_bpm": state.current_bpm,
            "current_key": state.current_key,
            "previous_stems": state.previous_stems,
            "active_stems": state.active_stems,
            "next_stems": state.next_stems,
            "llm_reasoning": state.llm_reasoning,
            "is_generating": state.is_generating,
            "is_show_started": state.is_show_started,
            "is_running": state.is_running,
            "is_recording": state.is_recording,
            "available_instruments": state.available_instruments,
            "user_override": state.user_override,
            "target_bpm_override": state.target_bpm_override,
            "target_key_override": state.target_key_override,
            "loop_count": state.loop_count,
            "last_actions": state.last_actions,
            "audience_message": state.audience_message,
            "audience_message_ts": state.audience_message_ts,
        }


# Stem control endpoints
@router.get("/api/stems")
async def get_stems():
    """Return list of active stems with their control states"""
    with state.lock:
        stems = []
        for i, s in enumerate(state.active_stems):
            stems.append(
                {
                    "index": i,
                    "prompt": s.get("prompt", ""),
                    "volume": state.stem_volumes.get(i, 1.0),
                    "is_muted": i in state.muted_stems,
                    "is_soloed": i in state.soloed_stems,
                }
            )
        return stems


@router.post("/api/stems/{index}/volume")
async def set_stem_volume(index: int, update: StemVolumeUpdate):
    """Set volume for a specific stem"""
    with state.lock:
        state.stem_volumes[index] = update.volume
    return {"status": "ok"}


@router.post("/api/stems/{index}/mute")
async def toggle_stem_mute(index: int):
    """Toggle mute for a specific stem"""
    with state.lock:
        if index in state.muted_stems:
            state.muted_stems.remove(index)
        else:
            state.muted_stems.add(index)
    return {"status": "ok"}


@router.post("/api/stems/{index}/solo")
async def toggle_stem_solo(index: int):
    """Toggle solo for a specific stem"""
    with state.lock:
        if index in state.soloed_stems:
            state.soloed_stems.remove(index)
        else:
            state.soloed_stems.add(index)
    return {"status": "ok"}


@router.get("/api/stems/{index}/download")
async def download_stem(index: int, set: str = "active"):
    """Download a single stem as WAV"""
    from fastapi.responses import Response
    import io
    import scipy.io.wavfile as wavfile
    import numpy as np

    with state.lock:
        if set == "previous":
            stem_list = state.previous_stems
            prefix = "prev"
        elif set == "next":
            stem_list = state.next_stems
            prefix = "next"
        else:
            stem_list = state.active_stems
            prefix = "stem"

        if index >= len(stem_list):
            raise HTTPException(status_code=404, detail="Stem not found")

        prompt = stem_list[index].get("prompt")
        audio_data = state.last_generated_stems.get(prompt)

        if audio_data is None:
            raise HTTPException(
                status_code=404, detail="Audio data not found for this stem"
            )

    # Convert numpy array to WAV bytes
    buf = io.BytesIO()
    # Foundation-1 typically outputs float32, convert to int16 for compatibility
    if audio_data.dtype != np.int16:
        audio_int = (np.clip(audio_data, -1.0, 1.0) * 32767).astype(np.int16)
    else:
        audio_int = audio_data

    wavfile.write(buf, 44100, audio_int)

    return Response(
        content=buf.getvalue(),
        media_type="audio/wav",
        headers={"Content-Disposition": f"attachment; filename={prefix}_{index}.wav"},
    )


@router.post("/api/stems/custom")
async def create_custom_stem(stem: CustomStemCreate):
    """Create a custom stem and add it to next_stems"""
    with state.lock:
        # Build stem dict with required fields
        new_stem = {
            "instrument": stem.instrument,
            "prompt": stem.prompt,
            "major_family": stem.instrument,
            "sub_family": stem.instrument,
            "model_id": stem.model_id,
            "timbre_tags": [],
            "_age": 0,
            "_custom": True,
        }
        state.next_stems.append(new_stem)
        stem_index = len(state.next_stems) - 1
    return {"status": "ok", "stem_index": stem_index}


@router.delete("/api/stems/next/{index}")
async def remove_next_stem(index: int):
    """Remove a stem from next_stems before it plays"""
    with state.lock:
        if index < 0 or index >= len(state.next_stems):
            raise HTTPException(status_code=404, detail="Stem not found in next_stems")
        state.next_stems.pop(index)
    return {"status": "ok"}


@router.post("/api/message/audience")
async def send_audience_message(msg: AudienceMessage):
    """Broadcast a message to the audience"""
    with state.lock:
        state.audience_message = msg.message.strip()
        state.audience_message_ts = time.time()
    return {"status": "ok"}


@router.get("/api/message/audience")
async def get_audience_message():
    """Get current audience message for polling"""
    with state.lock:
        return {
            "message": state.audience_message,
            "timestamp": state.audience_message_ts,
        }


# Generation config endpoints
@router.get("/api/generation-config")
async def get_generation_config():
    """Get generation parameters"""
    return {"cfg_scale": state.generation_cfg_scale, "steps": state.generation_steps}


@router.post("/api/generation-config")
async def update_generation_config(config: GenerationConfig):
    """Update generation parameters"""
    with state.lock:
        if config.cfg_scale is not None:
            state.generation_cfg_scale = config.cfg_scale
        if config.steps is not None:
            state.generation_steps = config.steps
    return {"status": "ok"}


@router.post("/api/state")
async def update_state(update: StateUpdate):
    """Update state (vibe, bpm_override, key_override, instruments, start/stop)"""
    with state.lock:
        if update.is_generating is not None:
            state.is_generating = update.is_generating
        if update.is_show_started is not None:
            state.is_show_started = update.is_show_started
        if update.should_reset is not None:
            state.should_reset = update.should_reset
        if update.user_override is not None:
            state.user_override = update.user_override
        if update.target_bpm_override is not None:
            state.target_bpm_override = update.target_bpm_override
            if not state.is_generating:
                state.current_bpm = update.target_bpm_override
        if update.target_key_override is not None:
            state.target_key_override = update.target_key_override
            if not state.is_generating:
                state.current_key = update.target_key_override
        if update.available_instruments is not None:
            state.available_instruments = update.available_instruments

    return {"status": "ok"}


# Instruments endpoint
@router.get("/api/instruments")
async def get_instruments():
    """Return instrument categories"""
    return state.categorized_instruments


# LLM Config endpoints
@router.get("/api/llm-config")
async def get_llm_config():
    """Get LLM configuration"""
    return {
        "base_url": state.llm_base_url,
        "api_key": state.llm_api_key,
        "model": state.llm_model,
        "icecast_enabled": getattr(state, "icecast_enabled", False),
        "audience_password": state.audience_password,
    }


@router.post("/api/llm-config")
async def update_llm_config(config: LLMConfig):
    """Update LLM configuration"""
    with state.lock:
        if config.base_url is not None:
            state.llm_base_url = config.base_url
        if config.api_key is not None:
            state.llm_api_key = config.api_key
        if config.model is not None:
            state.llm_model = config.model
        if config.icecast_enabled is not None:
            state.icecast_enabled = config.icecast_enabled
        if config.audience_password is not None:
            state.audience_password = config.audience_password

    return {"status": "ok"}


# Export endpoints
@router.post("/api/export/start")
async def start_export(req: ExportStartRequest):
    """Start recording to file"""
    export_dir = os.environ.get("EXPORT_DIR", "/exports")
    os.makedirs(export_dir, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"mc_clanker_{timestamp}.{req.format}"
    file_path = os.path.join(export_dir, filename)

    with state.lock:
        if state.is_recording:
            raise HTTPException(status_code=400, detail="Already recording")

        state.is_recording = True
        state.recording_format = req.format
        state.recording_file_path = file_path
        state.recording_start_time = time.time()
        state.recording_chunks = []

    return {"status": "started", "file_path": file_path}


@router.post("/api/export/stop")
async def stop_export():
    """Stop recording and return file path"""
    with state.lock:
        if not state.is_recording:
            raise HTTPException(status_code=400, detail="Not recording")

        file_path = state.recording_file_path
        duration = time.time() - state.recording_start_time
        format = state.recording_format
        chunks = state.recording_chunks[:]  # Copy

        state.is_recording = False
        state.recording_chunks = []

    # Write to file in background to not block
    def write_file():
        try:
            import wave
            import numpy as np
            import subprocess

            sample_rate = 44100
            channels = 2
            sampwidth = 2  # 16-bit

            if format == "mp3":
                # For MP3, write WAV first then convert with ffmpeg
                wav_path = file_path.replace(".mp3", ".wav")
                with wave.open(wav_path, "wb") as wf:
                    wf.setnchannels(channels)
                    wf.setsampwidth(sampwidth)
                    wf.setframerate(sample_rate)

                    for chunk in chunks:
                        audio_data = np.frombuffer(chunk, dtype=np.int16)
                        wf.writeframes(audio_data.tobytes())

                # Convert to MP3 using ffmpeg
                ffmpeg_cmd = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    wav_path,
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    "192k",
                    file_path,
                ]
                subprocess.run(ffmpeg_cmd, check=True, capture_output=True)
                os.remove(wav_path)  # Clean up temp WAV
            else:
                # WAV format - direct write
                with wave.open(file_path, "wb") as wf:
                    wf.setnchannels(channels)
                    wf.setsampwidth(sampwidth)
                    wf.setframerate(sample_rate)

                    for chunk in chunks:
                        audio_data = np.frombuffer(chunk, dtype=np.int16)
                        wf.writeframes(audio_data.tobytes())

            print(f"Recording saved to {file_path} ({duration:.1f}s)")
        except Exception as e:
            print(f"Error writing recording: {e}")

    threading.Thread(target=write_file, daemon=True).start()

    return {"file_path": file_path, "duration": duration}


# Health check
@router.get("/api/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "is_running": state.is_running}


# Show control endpoints
@router.post("/api/show/start")
async def start_show():
    """Start the show - audience can now see/hear the stream"""
    with state.lock:
        state.is_show_started = True
    return {"status": "ok", "is_show_started": True}


@router.post("/api/show/stop")
async def stop_show():
    """Stop the show - audience sees waiting screen"""
    with state.lock:
        state.is_show_started = False
    return {"status": "ok", "is_show_started": False}


class ModelUpdate(BaseModel):
    model_id: str
    enabled: bool


@router.get("/api/models")
async def get_models():
    import json
    import os

    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "models_config.json")
    if not os.path.exists(config_path):
        return {"models": {}}
    with open(config_path, "r") as f:
        return json.load(f)


@router.post("/api/models")
async def update_model(update: ModelUpdate):
    import json
    import os

    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "models_config.json")
    if not os.path.exists(config_path):
        return {"status": "error", "message": "models_config.json not found"}
    with open(config_path, "r") as f:
        config = json.load(f)

    if update.model_id in config.get("models", {}):
        config["models"][update.model_id]["enabled"] = update.enabled
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        return {"status": "ok", "message": "Restart required to apply changes"}
    return {"status": "error", "message": "Model not found"}


# Model management endpoints
@router.get("/api/models/status")
async def get_models_status():
    """Get loading state + VRAM for all models"""
    with state.lock:
        generator = getattr(state, 'generator', None)

    if generator is None:
        return {"error": "Generator not initialized"}

    vram = generator.get_vram_usage()

    # Combine model info with states
    result = {
        "models": {},
        "vram": vram
    }

    for model_id, engine in generator.models.items():
        result["models"][model_id] = {
            "state": generator.model_states.get(model_id, "unknown"),
            "error": generator.model_errors.get(model_id),
            "is_loaded": generator.is_model_loaded(model_id)
        }

    return result


@router.post("/api/models/{model_id}/load")
async def load_model(model_id: str):
    """Load a specific model on-demand"""
    with state.lock:
        generator = getattr(state, 'generator', None)

    if generator is None:
        raise HTTPException(status_code=500, detail="Generator not initialized")

    if model_id not in generator.models:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")

    try:
        generator.load_model(model_id)
        return {"status": "ok", "model_id": model_id, "state": generator.model_states.get(model_id)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/models/{model_id}/unload")
async def unload_model(model_id: str):
    """Unload a specific model"""
    with state.lock:
        generator = getattr(state, 'generator', None)

    if generator is None:
        raise HTTPException(status_code=500, detail="Generator not initialized")

    if model_id not in generator.models:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")

    try:
        generator.unload_model(model_id)
        return {"status": "ok", "model_id": model_id, "state": generator.model_states.get(model_id)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/models/{model_id}/reload")
async def reload_model(model_id: str):
    """Reload a specific model"""
    with state.lock:
        generator = getattr(state, 'generator', None)

    if generator is None:
        raise HTTPException(status_code=500, detail="Generator not initialized")

    if model_id not in generator.models:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")

    try:
        generator.reload_model(model_id)
        return {"status": "ok", "model_id": model_id, "state": generator.model_states.get(model_id)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/download-progress")
async def get_download_progress():
    """Get active downloads"""
    with state.lock:
        return {"downloads": state.download_progress}


@router.get("/api/vram")
async def get_vram():
    """Get VRAM usage summary"""
    with state.lock:
        generator = getattr(state, 'generator', None)

    if generator is None:
        return {"error": "Generator not initialized"}

    return generator.get_vram_usage()


# =============================================================================
# AUTH ROUTES
# =============================================================================

from pydantic import BaseModel, EmailStr
from fastapi import status
from app.auth import hash_password, verify_password, create_access_token, get_current_user_from_request
from app.db import DatabaseManager
from app.models import User


class UserRegister(BaseModel):
    username: str
    email: EmailStr
    password: str


class UserLogin(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    id: int
    username: str
    email: str


@router.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
async def register(user_data: UserRegister):
    """Create a new user account."""
    db_manager = DatabaseManager.get_instance()

    with db_manager.session() as session:
        # Check if username exists
        existing = session.query(User).filter(User.username == user_data.username).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username already taken"
            )

        # Check if email exists
        existing_email = session.query(User).filter(User.email == user_data.email).first()
        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

        # Create user
        user = User(
            username=user_data.username,
            email=user_data.email,
            password_hash=hash_password(user_data.password)
        )
        session.add(user)
        session.flush()

        token = create_access_token(user.id)

        return {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "token": token
        }


@router.post("/api/auth/login")
async def login(user_data: UserLogin):
    """Login and get JWT token."""
    db_manager = DatabaseManager.get_instance()

    with db_manager.session() as session:
        user = session.query(User).filter(User.username == user_data.username).first()

        if not user or not verify_password(user_data.password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password"
            )

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is disabled"
            )

        token = create_access_token(user.id)

        return {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "token": token
        }


@router.get("/api/auth/me")
async def get_me(request: Request):
    """Get current user info."""
    user = get_current_user_from_request(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )
    return user.to_dict()


# =============================================================================
# SHOW ROUTES
# =============================================================================

import secrets
from app.models import Show


def generate_audience_password() -> str:
    """Generate a random audience password."""
    return secrets.token_urlsafe(16)


def require_show_owner(show_id: int, request: Request, db_session):
    """Verify the current user owns the show."""
    user = get_current_user_from_request(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )

    show = db_session.query(Show).filter(Show.id == show_id).first()
    if show is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Show not found"
        )

    if show.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Show not found"
        )

    return show


class ShowCreate(BaseModel):
    title: str
    description: str = ""


class ShowUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None


class ShowResponse(BaseModel):
    id: int
    user_id: int
    title: str
    description: str
    status: str
    audio_file_path: Optional[str]
    config_snapshot: Optional[dict]
    started_at: Optional[str]
    ended_at: Optional[str]
    duration_seconds: Optional[int]
    created_at: Optional[str]


@router.get("/api/shows")
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


@router.post("/api/shows", status_code=status.HTTP_201_CREATED)
async def create_show(show_data: ShowCreate, request: Request):
    """Create a new show (status=draft, auto-generate audience password)."""
    user = get_current_user_from_request(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )

    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = Show(
            user_id=user.id,
            title=show_data.title,
            description=show_data.description,
            status="draft",
            audience_password_hash=hash_password(generate_audience_password())
        )
        session.add(show)
        session.flush()
        session.refresh(show)

        return show.to_dict(include_audience_password=True)


@router.get("/api/shows/{show_id}")
async def get_show(show_id: int, request: Request):
    """Get show details (includes audience password for owner)."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)
        return show.to_dict(include_audience_password=True)


@router.patch("/api/shows/{show_id}")
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


@router.delete("/api/shows/{show_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_show(show_id: int, request: Request):
    """Delete show + all related data."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)
        session.delete(show)


@router.post("/api/shows/{show_id}/start")
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

        from datetime import datetime
        show.status = "live"
        show.started_at = datetime.utcnow()

        # Create show directory and audio file
        shows_dir = os.environ.get("SHOWS_DIR", os.path.join(os.path.dirname(__file__), "data", "shows"))
        show_dir = os.path.join(shows_dir, str(show_id))
        os.makedirs(show_dir, exist_ok=True)
        audio_file_path = os.path.join(show_dir, "audio.wav")
        show.audio_file_path = audio_file_path

        # Capture current config snapshot
        with state.lock:
            show.config_snapshot = {
                "bpm": state.current_bpm,
                "key": state.current_key,
                "vibe": state.user_override,
            }

        # Open audio file for writing (raw PCM, 44100 Hz, 16-bit, stereo)
        audio_file = open(audio_file_path, "wb")

        # Start recording in framework
        with state.lock:
            state.is_show_recording = True
            state.current_show_id = show_id
            state.current_show_start_time = time.time()
            state.llm_interaction_buffer = []
            state.action_buffer = []
            state.current_show_audio_file = audio_file

        return show.to_dict(include_audience_password=True)


@router.post("/api/shows/{show_id}/stop")
async def stop_show(show_id: int, request: Request):
    """Stop show (status→ended, finalize recording)."""
    db_manager = DatabaseManager.get_instance()

    # Close audio file first (outside of db session)
    with state.lock:
        if state.current_show_audio_file is not None:
            try:
                state.current_show_audio_file.close()
            except Exception:
                pass
            state.current_show_audio_file = None

    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)

        if show.status != "live":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Show is not live (status: '{show.status}')"
            )

        from datetime import datetime
        show.status = "ended"
        show.ended_at = datetime.utcnow()

        # Calculate duration
        if show.started_at:
            show.duration_seconds = int((show.ended_at - show.started_at).total_seconds())

        # Stop recording in framework and flush buffers
        with state.lock:
            state.is_show_recording = False
            state.current_show_id = None
            if state.current_show_start_time:
                elapsed = time.time() - state.current_show_start_time
                state.current_show_start_time = None

        # Flush any remaining buffers
        from app.framework.framework_main import flush_recording_buffers
        flush_recording_buffers()

        return show.to_dict(include_audience_password=True)


@router.post("/api/shows/{show_id}/archive")
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


@router.post("/api/shows/{show_id}/regenerate-audience-password")
async def regenerate_audience_password(show_id: int, request: Request):
    """Generate new audience password for a show."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)

        new_password = generate_audience_password()
        show.audience_password_hash = hash_password(new_password)

        return {"audience_password": new_password}


@router.get("/api/shows/{show_id}/actions")
async def get_show_actions(show_id: int, request: Request, limit: int = 1000, offset: int = 0):
    """List all actions for a show."""
    from app.models import ShowAction

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


@router.get("/api/shows/{show_id}/llm-interactions")
async def get_show_llm_interactions(show_id: int, request: Request, limit: int = 1000, offset: int = 0):
    """List all LLM interactions for a show."""
    from app.models import LLMInteraction

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


@router.get("/api/shows/{show_id}/audio")
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

        from fastapi.responses import FileResponse
        return FileResponse(
            show.audio_file_path,
            media_type="audio/wav",
            filename=f"show_{show_id}.wav"
        )


# =============================================================================
# EXPORT ROUTES
# =============================================================================

@router.get("/api/shows/{show_id}/export/llm-dump")
async def export_llm_dump(show_id: int, request: Request):
    """Stream JSONL of prompt+response pairs."""
    from app.models import LLMInteraction

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

        from fastapi.responses import StreamingResponse
        return StreamingResponse(
            generate(),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f"attachment; filename=show_{show_id}_llm_dump.jsonl"}
        )


@router.get("/api/shows/{show_id}/export/full")
async def export_full_show(show_id: int, request: Request):
    """Download full show (audio + JSON of actions/interactions)."""
    from app.models import ShowAction, LLMInteraction

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

        from fastapi.responses import JSONResponse
        return JSONResponse(
            export_data,
            headers={"Content-Disposition": f"attachment; filename=show_{show_id}_full.json"}
        )


# =============================================================================
# PLAYBACK & REMIX ROUTES
# =============================================================================

@router.post("/api/shows/{show_id}/playback/start")
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
        with state.lock:
            state.currently_playing_show_id = show_id
            state.is_playback_active = True

        return {"status": "ok", "show_id": show_id}


@router.post("/api/shows/{show_id}/playback/stop")
async def stop_playback(show_id: int, request: Request):
    """Stop playback."""
    with state.lock:
        state.is_playback_active = False
        state.currently_playing_show_id = None

    return {"status": "ok"}


@router.get("/api/shows/{show_id}/remix")
async def get_remix_interface(show_id: int, request: Request):
    """Remix interface - regenerate stems via AI with user controls."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)

        return {
            "show_id": show_id,
            "title": show.title,
            "status": show.status,
            "config_snapshot": show.config_snapshot,
            "message": "Remix interface - full implementation in future phase"
        }


@router.get("/api/shows/{show_id}/audience-token")
async def get_audience_token(show_id: int, request: Request):
    """Get/show the audience password for a show (owner only)."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = require_show_owner(show_id, request, session)

        # Return a message that the password is in the show details
        # The actual password is not stored in plain text
        return {
            "message": "Audience password is set for this show. Share the password with your audience.",
            "has_password": bool(show.audience_password_hash)
        }


# =============================================================================
# JOB API ENDPOINTS (Phase 2: Async Framework Loop)
# =============================================================================

@router.post("/api/jobs", status_code=201)
async def submit_job(job: JobSubmission):
    """
    Submit a stem generation job to the queue.

    The job will be processed by a worker, and the audio will be stored in Garage.
    Use GET /api/jobs/{job_id} to poll for completion.
    """
    from datetime import datetime, timedelta
    from app.models.generator_job import GeneratorJob

    db_manager = DatabaseManager.get_instance()

    # Calculate expiration (24 hours from now)
    expires_at = datetime.utcnow() + timedelta(hours=24)

    with db_manager.session() as session:
        # Create new job
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

    # Notify workers that a new job is available (if using LISTEN/NOTIFY)
    # This is optional - workers can also poll the queue

    return {
        "job_id": job_id,
        "status": "pending",
        "message": "Job submitted successfully"
    }


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: uuid.UUID):
    """
    Get job status and audio path if completed.

    Returns the full job object including status, audio_path (if completed),
    or error_message (if failed).
    """
    from app.models.generator_job import GeneratorJob

    db_manager = DatabaseManager.get_instance()

    with db_manager.session() as session:
        job = session.query(GeneratorJob).filter(GeneratorJob.id == job_id).first()

        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        return job.to_dict()


@router.get("/api/audio/{job_id}")
async def get_audio(job_id: uuid.UUID):
    """
    Stream audio from Garage for a completed job.

    Redirects to a presigned URL for the audio file.
    The job must be in 'completed' status with a valid audio_path.
    """
    from app.models.generator_job import GeneratorJob
    from datetime import datetime, timedelta

    db_manager = DatabaseManager.get_instance()

    with db_manager.session() as session:
        job = session.query(GeneratorJob).filter(GeneratorJob.id == job_id).first()

        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        if job.status != "completed":
            raise HTTPException(
                status_code=400,
                detail=f"Job is not completed. Current status: {job.status}"
            )

        if not job.audio_path:
            raise HTTPException(status_code=404, detail="Audio path not found")

        # Refresh expiration on access
        job.expires_at = datetime.utcnow() + timedelta(hours=1)
        session.commit()

        # For now, return the audio_path directly
        # In production, this would generate a presigned URL from Garage
        # and redirect to it
        return {
            "audio_path": job.audio_path,
            "duration_seconds": job.duration_seconds,
            "message": "Audio available at audio_path. In production, this would redirect to a presigned Garage URL."
        }


@router.delete("/api/jobs/{job_id}")
async def cancel_job(job_id: uuid.UUID):
    """
    Cancel a pending job.

    Only jobs in 'pending' status can be cancelled.
    """
    from app.models.generator_job import GeneratorJob

    db_manager = DatabaseManager.get_instance()

    with db_manager.session() as session:
        job = session.query(GeneratorJob).filter(GeneratorJob.id == job_id).first()

        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        if job.status != "pending":
            raise HTTPException(
                status_code=400,
                detail=f"Cannot cancel job with status '{job.status}'. Only pending jobs can be cancelled."
            )

        job.status = "expired"
        session.commit()

        return {"status": "ok", "message": "Job cancelled"}


@router.get("/api/jobs")
async def list_jobs(
    session_id: Optional[uuid.UUID] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
):
    """
    List jobs with optional filtering by session_id and status.
    """
    from app.models.generator_job import GeneratorJob

    db_manager = DatabaseManager.get_instance()

    with db_manager.session() as session:
        query = session.query(GeneratorJob)

        if session_id:
            query = query.filter(GeneratorJob.session_id == session_id)

        if status:
            query = query.filter(GeneratorJob.status == status)

        total = query.count()
        jobs = query.order_by(GeneratorJob.created_at.desc()).limit(limit).offset(offset).all()

        return {
            "jobs": [job.to_dict() for job in jobs],
            "total": total,
            "limit": limit,
            "offset": offset
        }


# =============================================================================
# SESSION ROUTING ENDPOINTS (Phase 3: Session Affinity)
# =============================================================================

from uuid import UUID
from datetime import datetime, timedelta
from pydantic import BaseModel


class SessionHeartbeatRequest(BaseModel):
    server_id: str


class SessionServerResponse(BaseModel):
    session_id: UUID
    server_id: str
    created_at: datetime
    last_heartbeat: datetime


@router.post("/api/sessions/{session_id}/heartbeat")
async def session_heartbeat(session_id: UUID, request: SessionHeartbeatRequest):
    """
    Update session routing heartbeat.

    When a DJ session is running on a server, it should call this endpoint
    periodically to maintain the routing entry. Uses ON CONFLICT to handle
    both insert and update in a single query.
    """
    db_manager = DatabaseManager.get_instance()

    # Use raw SQL for ON CONFLICT support (SQLAlchemy's upsert is more verbose)
    # For SQLite compatibility, we use INSERT OR REPLACE pattern
    with db_manager.session() as session:
        # Check if the database supports ON CONFLICT (PostgreSQL)
        dialect = db_manager.engine.dialect.name

        if dialect == 'postgresql':
            # PostgreSQL: use ON CONFLICT DO UPDATE
            from sqlalchemy import text
            session.execute(text("""
                INSERT INTO session_routing (session_id, server_id, last_heartbeat)
                VALUES (:session_id, :server_id, NOW())
                ON CONFLICT (session_id) DO UPDATE SET
                    server_id = EXCLUDED.server_id,
                    last_heartbeat = NOW()
            """), {"session_id": str(session_id), "server_id": request.server_id})
        else:
            # SQLite fallback: try update first, then insert if no rows affected
            from sqlalchemy import text
            result = session.execute(text("""
                UPDATE session_routing
                SET server_id = :server_id, last_heartbeat = :heartbeat
                WHERE session_id = :session_id
            """), {"session_id": str(session_id), "server_id": request.server_id, "heartbeat": datetime.utcnow()})

            if result.rowcount == 0:
                session.execute(text("""
                    INSERT INTO session_routing (session_id, server_id, last_heartbeat)
                    VALUES (:session_id, :server_id, :heartbeat)
                """), {"session_id": str(session_id), "server_id": request.server_id, "heartbeat": datetime.utcnow()})

    return {"status": "ok", "session_id": str(session_id), "server_id": request.server_id}


@router.get("/api/sessions/{session_id}/server", response_model=SessionServerResponse)
async def get_session_server(session_id: UUID):
    """
    Get which server handles a given session.

    Used by the session affinity middleware to determine if a request
    should be redirected to a different server.
    """
    db_manager = DatabaseManager.get_instance()

    with db_manager.session() as session:
        from sqlalchemy import text
        result = session.execute(text("""
            SELECT session_id, server_id, created_at, last_heartbeat
            FROM session_routing
            WHERE session_id = :session_id
        """), {"session_id": str(session_id)}).fetchone()

        if result is None:
            raise HTTPException(status_code=404, detail="Session not found in routing table")

        return {
            "session_id": result[0],
            "server_id": result[1],
            "created_at": result[2],
            "last_heartbeat": result[3]
        }


@router.delete("/api/sessions/{session_id}/routing")
async def delete_session_routing(session_id: UUID):
    """
    Remove a session from the routing table.

    Called when a DJ session ends to clean up the routing entry.
    """
    db_manager = DatabaseManager.get_instance()

    with db_manager.session() as session:
        from sqlalchemy import text
        session.execute(text("""
            DELETE FROM session_routing WHERE session_id = :session_id
        """), {"session_id": str(session_id)})

    return {"status": "ok", "session_id": str(session_id)}


@router.get("/api/sessions/{session_id}/heartbeat")
async def get_session_heartbeat(session_id: UUID):
    """
    Get the last heartbeat time for a session.

    Used for debugging and monitoring session health.
    """
    db_manager = DatabaseManager.get_instance()

    with db_manager.session() as session:
        from sqlalchemy import text
        result = session.execute(text("""
            SELECT last_heartbeat FROM session_routing WHERE session_id = :session_id
        """), {"session_id": str(session_id)}).fetchone()

        if result is None:
            raise HTTPException(status_code=404, detail="Session not found in routing table")

        return {
            "session_id": str(session_id),
            "last_heartbeat": result[0],
            "is_stale": (datetime.utcnow() - result[0]) > timedelta(minutes=5)
        }
