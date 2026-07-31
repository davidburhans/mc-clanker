"""
GeneratorJob model - represents a queued stem generation job.

Column types are reconciled with migrations/001_jobs_and_routing.sql so that
`Base.metadata.create_all()` (used when migrations are not applied) matches the
intended production schema:

- timestamps are timezone-aware (TIMESTAMPTZ / DateTime(timezone=True)); the old
  naive `DateTime` mixed with `NOW()` in cleanup/worker caused off-by-timezone
  comparisons (review finding C3).
- `timbre_tags` is JSONB on PostgreSQL (matches migration), JSON on SQLite.
- a `CHECK` constraint and the partial indexes from the migration are declared
  so create_all() yields the same claim/cleanup performance characteristics.
- `lease_expires_at` supports job reclamation when a worker dies mid-job (B2).
- `content_hash` is the foundation for deduplicating identical in-flight jobs so
  non-deterministic re-generation of "retained" stems does not change their
  audio (C8). The hard unique constraint is intentionally NOT enforced here so
  the existing plain INSERT submit paths keep working; see migrations/002.
"""

import os
import uuid
import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Float,
    Text,
    JSON,
    Index,
    CheckConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from ..db import Base


class JobStatus(enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


# JSONB on PostgreSQL (matches migration 001), JSON on SQLite for local dev.
_JSON_TYPE = JSONB().with_variant(JSON, "sqlite")


def _make_uuid_column():
    """Return a UUID column compatible with both PostgreSQL and SQLite."""
    database_url = os.environ.get("DATABASE_URL", "")
    if "postgres" in database_url or "postgresql" in database_url:
        from sqlalchemy.dialects.postgresql import UUID as PG_UUID

        return Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    else:
        # SQLite: store as String(36)
        return Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))


def _make_session_id_column():
    """Return a session_id column compatible with both PostgreSQL and SQLite."""
    database_url = os.environ.get("DATABASE_URL", "")
    if "postgres" in database_url or "postgresql" in database_url:
        from sqlalchemy.dialects.postgresql import UUID as PG_UUID

        return Column(PG_UUID(as_uuid=True), nullable=False, index=True)
    else:
        return Column(String(36), nullable=False, index=True)


class GeneratorJob(Base):
    """A queued stem-generation job processed by the GPU worker."""

    __tablename__ = "generator_jobs"

    id = _make_uuid_column()
    session_id = _make_session_id_column()

    # Job spec
    instrument = Column(String(255), nullable=False)
    prompt = Column(Text, nullable=False)
    major_family = Column(String(100), nullable=True)
    model_id = Column(String(100), default="foundation-1")
    key = Column(String(50), nullable=True)
    bpm = Column(Integer, nullable=True)
    timbre_tags = Column(_JSON_TYPE, default=list)
    bars = Column(Integer, default=4)

    # Status tracking — timezone-aware to match TIMESTAMPTZ in the migration.
    status = Column(String(20), default=JobStatus.PENDING.value)
    priority = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Result (set by worker)
    audio_path = Column(String(500), nullable=True)
    duration_seconds = Column(Float, nullable=True)
    error_message = Column(Text, nullable=True)

    # Worker that processed this job
    worker_id = Column(String(255), nullable=True)

    # Expiration (timezone-aware). Cleanup deletes terminal rows past this time.
    expires_at = Column(DateTime(timezone=True), nullable=False)

    # Lease: set when a worker claims a job; a lapsed lease lets the job be
    # reclaimed/reaped instead of being orphaned in 'processing' forever (B2).
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)

    # Stable hash of the generation inputs; foundation for dedup of identical
    # pending jobs so non-deterministic models don't change "retained" audio (C8).
    content_hash = Column(String(64), nullable=True, index=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed', 'expired')",
            name="ck_generator_jobs_status",
        ),
        # Partial indexes mirror migrations/001 so create_all() is as fast as a
        # migrated schema for claiming and cleanup. `postgresql_where` is ignored
        # on SQLite (a plain index is created there), which is fine for local dev.
        Index(
            "idx_generator_jobs_claiming",
            "priority",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "idx_generator_jobs_active",
            "status",
            postgresql_where=text("status IN ('pending', 'processing')"),
        ),
        Index("idx_generator_jobs_expires", "expires_at"),
        Index(
            "idx_generator_jobs_lease_reclaim",
            "lease_expires_at",
            postgresql_where=text("status = 'processing'"),
        ),
    )

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
            "created_at": self.created_at.isoformat() if self.created_at is not None else None,
            "started_at": self.started_at.isoformat() if self.started_at is not None else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at is not None else None,
            "audio_path": self.audio_path,
            "duration_seconds": self.duration_seconds,
            "error_message": self.error_message,
            "worker_id": self.worker_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at is not None else None,
            "lease_expires_at": self.lease_expires_at.isoformat() if self.lease_expires_at is not None else None,
            "content_hash": self.content_hash,
        }
