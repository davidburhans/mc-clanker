import json
from collections.abc import Mapping
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import relationship

from ..db import Base

# Every captured column — single source for the retention SELECT (cleanup.py)
# and the ORM delegate below (REL-16/U5 decision 8: one shaper, no drift).
_DUMP_COLUMNS = (
    "id",
    "show_id",
    "loop_index",
    "timestamp",
    "relative_time_ms",
    "action_type",
    "stem_index",
    "stem_details",
    "action_description",
)


def _json_field(value):
    """Normalize a JSON column value: str → parsed JSON, anything else unchanged.

    asyncpg delivers stem_details as str; a non-JSON str degrades to a raw
    passthrough instead of crashing the retention pass.
    """
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def show_action_row(record: Mapping) -> dict:
    """API/archive row for one ShowAction from a raw column Mapping (REL-16/U5).

    Same shape as ``ShowAction.to_dict`` — the ORM method delegates here so the
    asyncpg retention archive cannot drift from the API serialization.

    Example: ``show_action_row({"id": 1, "action_type": "add", ...})`` → dict with
    ``stem_details`` normalized from str to dict.
    """
    timestamp = record["timestamp"]
    return {
        "id": record["id"],
        "show_id": record["show_id"],
        "loop_index": record["loop_index"],
        "timestamp": timestamp.isoformat() if timestamp else None,
        "relative_time_ms": record["relative_time_ms"],
        "action_type": record["action_type"],
        "stem_index": record["stem_index"],
        "stem_details": _json_field(record["stem_details"]),
        "action_description": record["action_description"],
    }


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
        """Archive/API row; delegates to the pure shaper shared with cleanup (REL-16/U5)."""
        return show_action_row({column: getattr(self, column) for column in _DUMP_COLUMNS})
