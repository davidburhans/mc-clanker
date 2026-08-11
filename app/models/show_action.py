from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import relationship

from ..db import Base


class ShowAction(Base):
    __tablename__ = "show_actions"

    id = Column(Integer, primary_key=True, index=True)
    show_id = Column(Integer, ForeignKey("shows.id"), nullable=False, index=True)
    loop_index = Column(Integer, nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    relative_time_ms = Column(Integer, nullable=False)
    action_type = Column(String(20), nullable=False)  # retain, add, remove
    stem_index = Column(Integer, nullable=True)
    stem_details = Column(JSON, nullable=True)
    action_description = Column(String(500), nullable=True)

    show = relationship("Show", back_populates="actions")

    __table_args__ = (Index("ix_show_actions_show_loop", "show_id", "loop_index"),)

    def to_dict(self):
        return {
            "id": self.id,
            "show_id": self.show_id,
            "loop_index": self.loop_index,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "relative_time_ms": self.relative_time_ms,
            "action_type": self.action_type,
            "stem_index": self.stem_index,
            "stem_details": self.stem_details,
            "action_description": self.action_description,
        }
