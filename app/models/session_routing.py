"""SessionRouting model — maps session UUIDs to the server that owns them.

Column types are reconciled with migrations/001_jobs_and_routing.sql:78-82 so the
live boot path (`Base.metadata.create_all()`, app/db.py) yields the same schema a
migrated database has (review E3/Q5, mirroring the C3 reconciliation of
generator_jobs):

- `session_id` is a real UUID PK on PostgreSQL (String(36) on SQLite, via the
  shared `_make_uuid_column` helper from app/models/generator_job.py).
- `created_at`/`last_heartbeat` are TIMESTAMPTZ (`DateTime(timezone=True)`); the
  naive variant made `routes/jobs.py` `replace(tzinfo=utc)` heartbeats wrong on
  any non-UTC host.
- `idx_session_routing_server` / `idx_session_routing_heartbeat` back the
  per-request affinity lookup and any stale-session reaper.
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, String

from ..db import Base
from .generator_job import _make_uuid_column


class SessionRouting(Base):
    __tablename__ = "session_routing"

    # UUID PK on PostgreSQL, String(36) on SQLite (matches migration 001).
    session_id = _make_uuid_column()
    server_id = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_heartbeat = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        # Mirrors idx_session_routing_server / idx_session_routing_heartbeat in
        # migrations/001_jobs_and_routing.sql so create_all() is not a seq-scan schema.
        Index("idx_session_routing_server", "server_id"),
        Index("idx_session_routing_heartbeat", "last_heartbeat"),
    )

    def to_dict(self):
        return {
            # str() so a PG UUID object (native on the postgres dialect) serialises.
            "session_id": str(self.session_id) if self.session_id is not None else None,
            "server_id": self.server_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
        }
