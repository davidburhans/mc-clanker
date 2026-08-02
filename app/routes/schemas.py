from pydantic import BaseModel, EmailStr, field_validator
import uuid
from datetime import datetime

from app.lib.constants import VALID_BPMS, VALID_KEYS


class UserRegister(BaseModel):
    username: str
    email: EmailStr
    password: str

class UserLogin(BaseModel):
    username: str
    password: str


class CustomInstrumentCreate(BaseModel):
    """Request body for adding a user-defined instrument with a major_family."""
    name: str
    major_family: str

    @field_validator('major_family')
    @classmethod
    def validate_family(cls, v: str) -> str:
        # Allow any string for family so that new ones can be registered.
        # We still trim and ensure it's not empty.
        v = v.strip()
        if not v:
            raise ValueError("Family cannot be empty")
        return v


class StateUpdate(BaseModel):
    is_generating: bool | None = None
    is_show_started: bool | None = None
    should_reset: bool | None = None
    user_override: str | None = None
    target_bpm_override: int | None = None
    target_key_override: str | None = None
    available_instruments: list[str] | None = None

    @field_validator('target_bpm_override')
    @classmethod
    def validate_bpm(cls, v: int | None) -> int | None:
        if v is not None and v not in VALID_BPMS:
            raise ValueError(f"Invalid BPM. Must be one of: {VALID_BPMS}")
        return v

    @field_validator('target_key_override')
    @classmethod
    def validate_key(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_KEYS:
            raise ValueError(f"Invalid key. Must be one of: {VALID_KEYS}")
        return v

class LLMConfig(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    icecast_enabled: bool | None = None
    audience_password: str | None = None

class GenerationConfig(BaseModel):
    cfg_scale: float | None = None
    steps: int | None = None

class StemVolumeUpdate(BaseModel):
    volume: float

class ExportStartRequest(BaseModel):
    format: str = "wav"

class ExportStopResponse(BaseModel):
    file_path: str | None
    status: str

class CustomStemCreate(BaseModel):
    instrument: str
    prompt: str
    model_id: str = "default"

class AudienceMessage(BaseModel):
    message: str

class JobSubmission(BaseModel):
    session_id: uuid.UUID
    instrument: str
    prompt: str
    major_family: str | None = None
    model_id: str = "foundation-1"
    key: str | None = None
    bpm: int | None = None
    timbre_tags: list[str] = []
    bars: int = 4

    @field_validator('bpm')
    @classmethod
    def validate_bpm(cls, v: int | None) -> int | None:
        if v is not None and v not in VALID_BPMS:
            raise ValueError(f"Invalid BPM. Must be one of: {VALID_BPMS}")
        return v

    @field_validator('key')
    @classmethod
    def validate_key(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_KEYS:
            raise ValueError(f"Invalid key. Must be one of: {VALID_KEYS}")
        return v

class JobResponse(BaseModel):
    id: str
    session_id: str
    instrument: str
    prompt: str
    major_family: str | None
    model_id: str
    key: str | None
    bpm: int | None
    timbre_tags: list[str]
    bars: int
    status: str
    priority: int
    created_at: str | None
    started_at: str | None
    completed_at: str | None
    audio_path: str | None
    duration_seconds: float | None
    error_message: str | None
    worker_id: str | None
    expires_at: str | None

class AudioResponse(BaseModel):
    audio_url: str
    duration_seconds: float | None

class ShowCreate(BaseModel):
    title: str
    description: str = ""

class ShowUpdate(BaseModel):
    title: str | None = None
    description: str | None = None

class ShowResponse(BaseModel):
    id: int
    user_id: int
    title: str
    description: str
    status: str
    audio_file_path: str | None
    config_snapshot: dict | None
    started_at: str | None
    ended_at: str | None
    duration_seconds: int | None
    created_at: str | None

class SessionHeartbeatRequest(BaseModel):
    server_id: str

class SessionServerResponse(BaseModel):
    session_id: uuid.UUID
    server_id: str
    created_at: datetime
    last_heartbeat: datetime
