import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

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

    @field_validator("major_family")
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

    @field_validator("target_bpm_override")
    @classmethod
    def validate_bpm(cls, v: int | None) -> int | None:
        if v is not None and v not in VALID_BPMS:
            raise ValueError(f"Invalid BPM. Must be one of: {VALID_BPMS}")
        return v

    @field_validator("target_key_override")
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
    # SEC-1: bound generation params so absurd client values (cfg_scale=9999,
    # steps=9999) cannot reach the GPU and trigger runaway compute / NaN audio.
    cfg_scale: float | None = Field(default=None, ge=0.0, le=20.0)
    steps: int | None = Field(default=None, ge=1, le=100)


class StemVolumeUpdate(BaseModel):
    # Round-3 D8: a bare float accepted NaN/±Inf/1e308, and one such request poisoned
    # the whole mix (NaN gain collapses the stem to silence, huge gains to
    # full-scale garbage) with a 200 OK. 0.0–2.0 is the documented range.
    volume: float = Field(ge=0.0, le=2.0, allow_inf_nan=False)


class ExportStartRequest(BaseModel):
    # Allowlist the recording container extension: the format string is
    # interpolated into the export filename, so an unvalidated value enabled
    # path traversal / arbitrary file truncation via POST /api/export/start
    # (review SEC-1).
    format: Literal["wav", "mp3"] = "wav"


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
    # Coerced to str at the boundary (round-3 D6): the documented SQLite dev
    # fallback stores uuids in VARCHAR(36) and sqlite3 raises ProgrammingError when
    # binding a uuid.UUID object, so POST /api/jobs 500'd. Input is still validated
    # as a UUID — non-UUID bodies keep returning 422.
    session_id: str
    instrument: str
    prompt: str
    major_family: str | None = None
    model_id: str = "foundation-1"
    key: str | None = None
    bpm: int | None = None
    timbre_tags: list[str] = []
    bars: int = Field(default=4, ge=1, le=32)
    # REL-25b: same SEC-1 bounds as GenerationConfig — the job-submit route must
    # not become the unbounded backdoor the config route closed.
    cfg_scale: float | None = Field(default=None, ge=0.0, le=20.0)
    steps: int | None = Field(default=None, ge=1, le=100)

    @field_validator("session_id", mode="before")
    @classmethod
    def validate_session_id(cls, v: object) -> str:
        """Canonicalize the session id to its str form, rejecting non-UUIDs."""
        return _as_uuid_str(v)

    @field_validator("bpm")
    @classmethod
    def validate_bpm(cls, v: int | None) -> int | None:
        if v is not None and v not in VALID_BPMS:
            raise ValueError(f"Invalid BPM. Must be one of: {VALID_BPMS}")
        return v

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_KEYS:
            raise ValueError(f"Invalid key. Must be one of: {VALID_KEYS}")
        return v


def _as_uuid_str(value: object) -> str:
    """Return the canonical UUID string for ``value`` or raise ValueError.

    Example: ``"550e8400-e29b-41d4-a716-446655440000"`` and the equivalent
    ``uuid.UUID`` both yield the lowercase hyphenated string; ``"nope"`` raises.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str):
        raise ValueError(f"Expected a UUID string, got {type(value).__name__}: {value!r}")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError(f"Invalid UUID {value!r}: expected e.g. 550e8400-e29b-41d4-a716-446655440000") from exc


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
