from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from ..db import Base


class Show(Base):
    __tablename__ = "shows"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    description = Column(String(1000), default="")
    status = Column(String(20), default="draft")  # draft, live, ended, archived
    audio_file_path = Column(String(500), nullable=True)
    audience_password_hash = Column(String(255), nullable=True)
    config_snapshot = Column(JSON, nullable=True)  # BPM, key, etc.
    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="shows")
    actions = relationship("ShowAction", back_populates="show", cascade="all, delete-orphan")
    llm_interactions = relationship("LLMInteraction", back_populates="show", cascade="all, delete-orphan")

    def to_dict(self, include_audience_password=False):
        result = {
            "id": self.id,
            "user_id": self.user_id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "audio_file_path": self.audio_file_path,
            "config_snapshot": self.config_snapshot,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_seconds": self.duration_seconds,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_audience_password:
            result["has_audience_password"] = bool(self.audience_password_hash)
        return result
