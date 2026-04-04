from pydantic import BaseModel, EmailStr
from typing import Optional, List
import uuid
from datetime import datetime

class UserRegister(BaseModel):
    username: str
    email: EmailStr
    password: str

class UserLogin(BaseModel):
    username: str
    password: str

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

class JobSubmission(BaseModel):
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
    audio_url: str
    duration_seconds: Optional[float]

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

class SessionHeartbeatRequest(BaseModel):
    server_id: str

class SessionServerResponse(BaseModel):
    session_id: uuid.UUID
    server_id: str
    created_at: datetime
    last_heartbeat: datetime
