import json
from collections.abc import Mapping
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import relationship

from ..db import Base

# Every captured column — the single source for the retention SELECT (cleanup.py
# builds its archive query from this tuple) and for the ORM delegate below
# (REL-16/U5 decision 8: one shaper, no drift between SQLAlchemy and asyncpg rows).
_DUMP_COLUMNS = (
    "id",
    "show_id",
    "loop_index",
    "timestamp",
    "relative_time_ms",
    "prompt_messages",
    "parsed_response",
    "applied_actions",
    "reasoning",
    "error",
    "was_fallback",
    "bpm",
    "key",
    "instruments",
    "action_type",
    "set_name",
)

# Columns stored as JSON: asyncpg delivers these as str, SQLAlchemy as
# list/dict — llm_dump_row normalizes the str form.
_JSON_COLUMNS = ("prompt_messages", "parsed_response", "applied_actions", "instruments")


def _json_field(value):
    """Normalize a JSON column value: str → parsed JSON, anything else unchanged.

    A non-JSON str (legacy context-summary blobs) degrades to a raw passthrough
    instead of crashing the retention pass.
    """
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def llm_dump_row(record: Mapping) -> dict:
    """Training-corpus row (U4/REL-16) from a raw column Mapping (ORM row or asyncpg Record).

    messages follows the {role, content} chat shape the unsloth converter
    and dpo_pipeline consume; the assistant turn carries the response as a
    JSON *string* (their canonical row format, see tests/test_dpo_pipeline.py).
    Legacy rows whose prompt_messages is the old context-summary dict degrade
    to an assistant-only row instead of raising (the unsloth converter inserts
    the system message for assistant-only samples).

    Example: ``llm_dump_row({"prompt_messages": [{...}], "parsed_response": {...}, ...})``
    → ``{"messages": [...], "response": {...}, "meta": {...}}``.
    """
    pm = _json_field(record["prompt_messages"])
    parsed = _json_field(record["parsed_response"])
    chat = list(pm) if isinstance(pm, list) else []
    if parsed:
        chat = chat + [{"role": "assistant", "content": json.dumps(parsed)}]
    result = {"messages": chat}
    if parsed:
        result["response"] = parsed
    # meta carries every remaining captured column — "DPO export contains
    # every captured field" (U4 acceptance); kept OUT of the top level so
    # the dump stays chat+response only for the training tools.
    result["meta"] = {
        "loop_index": record["loop_index"],
        "relative_time_ms": record["relative_time_ms"],
        "bpm": record["bpm"],
        "key": record["key"],
        "set_name": record["set_name"],
        "instruments": _json_field(record["instruments"]),
        "action_type": record["action_type"],
        "applied_actions": _json_field(record["applied_actions"]),
        "reasoning": record["reasoning"],
        "was_fallback": record["was_fallback"],
        "error": record["error"],
    }
    return result


class LLMInteraction(Base):
    __tablename__ = "llm_interactions"

    id = Column(Integer, primary_key=True, index=True)
    show_id = Column(Integer, ForeignKey("shows.id"), nullable=False, index=True)
    loop_index = Column(Integer, nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    relative_time_ms = Column(Integer, nullable=False)
    prompt_messages = Column(JSON, nullable=False)
    parsed_response = Column(JSON, nullable=True)
    # U4 (REL-04 + DPO field audit): the post-dedupe stem set actually enacted
    # this loop, with per-stem outcome ("generated" | "cached" | "failed") —
    # distinct from parsed_response.actions (requested). Additive; see
    # migrations/003_llm_capture_additive.sql for existing deployments.
    applied_actions = Column(JSON, nullable=True)
    reasoning = Column(String(1000), nullable=True)
    error = Column(String(500), nullable=True)
    was_fallback = Column(Boolean, default=False)
    # Conductor context captured per loop for the reasoning-log viewer.
    # Populated by audit_recording.append_loop_audit from conductor_response.
    bpm = Column(Float, nullable=True)
    key = Column(String(50), nullable=True)
    instruments = Column(JSON, nullable=True)
    action_type = Column(String(50), nullable=True)
    set_name = Column(String(255), nullable=True)

    show = relationship("Show", back_populates="llm_interactions")

    __table_args__ = (
        Index("ix_llm_interactions_show_loop", "show_id", "loop_index"),
        # FU-4 (rel-13 §6 residual): covers the timeline detail scan's
        # (show_id → relative_time_ms) filter+order — each chunk previously
        # re-sorted the whole show's rows. Deployed PG: migrations/005.
        Index("ix_llm_interactions_show_rel_time", "show_id", "relative_time_ms"),
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
            "applied_actions": self.applied_actions,
            "reasoning": self.reasoning,
            "error": self.error,
            "was_fallback": self.was_fallback,
            "bpm": self.bpm,
            "key": self.key,
            "instruments": self.instruments,
            "action_type": self.action_type,
            "set_name": self.set_name,
        }

    def to_reasoning_export_dict(self):
        """Structured view for the reasoning-log viewer/export (no raw prompts).

        Used by GET /api/llm-config/reasoning-logs/export as one JSONL row.
        Excludes prompt_messages/parsed_response to keep the export focused on
        the conductor's per-loop musical decisions.
        """
        return {
            "id": self.id,
            "loop_index": self.loop_index,
            "relative_time_ms": self.relative_time_ms,
            "bpm": self.bpm,
            "key": self.key,
            "instruments": self.instruments,
            "action_type": self.action_type,
            "set_name": self.set_name,
            "reasoning": self.reasoning,
            "was_fallback": self.was_fallback,
        }

    def to_llm_dump_dict(self):
        """Training-corpus row (U4): full chat + response + capture metadata.

        Delegates to the pure ``llm_dump_row`` shaper over this row's columns —
        the exact shape the asyncpg retention archive writes (REL-16/U5).
        """
        return llm_dump_row({column: getattr(self, column) for column in _DUMP_COLUMNS})
