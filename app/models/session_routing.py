"""SessionRouting model — maps session UUIDs to the server that owns them."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String

from ..db import Base


class SessionRouting(Base):
    __tablename__ = "session_routing"

    session_id = Column(String(36), primary_key=True)
    server_id = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_heartbeat = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self):
        return {
            "session_id": self.session_id,
            "server_id": self.server_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
        }
