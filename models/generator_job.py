"""
GeneratorJob model - represents a queued stem generation job.
"""
from sqlalchemy import Column, Integer, String, DateTime, Float, Text, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from datetime import datetime
import uuid
import enum

from db import Base


class JobStatus(enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class GeneratorJob(Base):
    __tablename__ = "generator_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(UUID(as_uuid=True), nullable=False, index=True)

    # Job spec
    instrument = Column(String(255), nullable=False)
    prompt = Column(Text, nullable=False)
    major_family = Column(String(100), nullable=True)
    model_id = Column(String(100), default="foundation-1")
    key = Column(String(50), nullable=True)
    bpm = Column(Integer, nullable=True)
    timbre_tags = Column(JSONB, default=[])
    bars = Column(Integer, default=4)

    # Status
    status = Column(String(20), default=JobStatus.PENDING.value)
    priority = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # Result (set by worker)
    audio_path = Column(String(500), nullable=True)
    duration_seconds = Column(Float, nullable=True)
    error_message = Column(Text, nullable=True)

    # Worker that processed this job
    worker_id = Column(String(255), nullable=True)

    # Expiration
    expires_at = Column(DateTime, nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "session_id": str(self.session_id),
            "instrument": self.instrument,
            "prompt": self.prompt,
            "major_family": self.major_family,
            "model_id": self.model_id,
            "key": self.key,
            "bpm": self.bpm,
            "timbre_tags": self.timbre_tags,
            "bars": self.bars,
            "status": self.status,
            "priority": self.priority,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "audio_path": self.audio_path,
            "duration_seconds": self.duration_seconds,
            "error_message": self.error_message,
            "worker_id": self.worker_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }