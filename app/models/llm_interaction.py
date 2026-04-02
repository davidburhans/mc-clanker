from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, JSON, Index, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from ..db import Base


class LLMInteraction(Base):
    __tablename__ = "llm_interactions"

    id = Column(Integer, primary_key=True, index=True)
    show_id = Column(Integer, ForeignKey("shows.id"), nullable=False, index=True)
    loop_index = Column(Integer, nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    relative_time_ms = Column(Integer, nullable=False)
    prompt_messages = Column(JSON, nullable=False)
    parsed_response = Column(JSON, nullable=True)
    reasoning = Column(String(1000), nullable=True)
    error = Column(String(500), nullable=True)
    was_fallback = Column(Boolean, default=False)

    show = relationship("Show", back_populates="llm_interactions")

    __table_args__ = (
        Index("ix_llm_interactions_show_loop", "show_id", "loop_index"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "show_id": self.show_id,
            "loop_index": self.loop_index,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "relative_time_ms": self.relative_time_ms,
            "prompt_messages": self.prompt_messages,
            "parsed_response": self.parsed_response,
            "reasoning": self.reasoning,
            "error": self.error,
            "was_fallback": self.was_fallback,
        }

    def to_llm_dump_dict(self):
        """Format for LLM dump export (prompt + response only)."""
        result = {"messages": self.prompt_messages}
        if self.parsed_response:
            result["response"] = self.parsed_response
        return result
