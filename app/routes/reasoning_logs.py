"""Reasoning log viewer routes - search, filter, export, and timeline
for Conductor LLM decisions.
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.auth import get_current_user_from_request
from app.db import DatabaseManager
from app.models import LLMInteraction, Show

router = APIRouter(prefix="/llm-config")


def _require_show_owner(show_id: int, request, db_session):
    user = get_current_user_from_request(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    show = db_session.query(Show).filter(Show.id == show_id).first()
    if show is None or show.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Show not found")
    return show


def _column(name: str):
    """Return the named LLMInteraction column, or None if absent from the schema.

    The enriched DJ columns (action_type, bpm, key, instruments, set_name) are
    optional and may not exist on every migration of LLMInteraction. Filtering on
    a column that is not part of the table is impossible, so callers must guard a
    None return instead of crashing with AttributeError.
    """
    return getattr(LLMInteraction, name, None)


def _eq_filter(query, name: str, value):
    """Equality-filter on a column only when both the column and value exist."""
    col = _column(name)
    if value is None or col is None:
        return query
    return query.filter(col == value)


@router.get(
    "/reasoning-logs",
    summary="Search reasoning logs",
    description=(
        "Search and filter Conductor LLM reasoning with full-text search, "
        "action type, BPM range, key, instrument, and pagination."
    ),
    responses={
        200: {"description": "Paginated reasoning log entries"},
        401: {"description": "Not authenticated"},
        404: {"description": "Show not found"},
    },
)
async def search_reasoning_logs(
    request: Request,
    show_id: int = Query(..., description="Show ID to query"),
    action_type: str | None = Query(None, description="Filter: retain | add | remove"),
    bpm_min: float | None = Query(None, description="Minimum BPM"),
    bpm_max: float | None = Query(None, description="Maximum BPM"),
    key: str | None = Query(None, description="Musical key (e.g. C, Am, F#)"),
    instrument: str | None = Query(None, description="Instrument name (partial match)"),
    set_name: str | None = Query(None, description="Set/section name (e.g. Verse, Chorus)"),
    was_fallback: bool | None = Query(None, description="Filter fallback responses"),
    q: str | None = Query(None, description="Full-text search in reasoning text"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Search and filter Conductor reasoning logs with pagination."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        _require_show_owner(show_id, request, session)
        query = session.query(LLMInteraction).filter(LLMInteraction.show_id == show_id)
        query = _eq_filter(query, "action_type", action_type)
        bpm_col = _column("bpm")
        if bpm_min is not None and bpm_col is not None:
            query = query.filter(bpm_col >= bpm_min)
        if bpm_max is not None and bpm_col is not None:
            query = query.filter(bpm_col <= bpm_max)
        query = _eq_filter(query, "key", key)
        query = _eq_filter(query, "set_name", set_name)
        query = _eq_filter(query, "was_fallback", was_fallback)
        inst_col = _column("instruments")
        if instrument is not None and inst_col is not None:
            if db_manager.is_postgres:
                query = query.filter(inst_col.op("@>")(f'["{instrument}"]'))
            else:
                query = query.filter(inst_col.like(f"%{instrument}%"))
        reasoning_col = _column("reasoning")
        if q is not None and reasoning_col is not None:
            if db_manager.is_postgres:
                query = query.filter(reasoning_col.ilike(f"%{q}%"))
            else:
                query = query.filter(reasoning_col.like(f"%{q}%"))
        total = query.count()
        interactions = query.order_by(LLMInteraction.timestamp.desc()).limit(limit).offset(offset).all()
        return {"interactions": [i.to_dict() for i in interactions], "total": total, "limit": limit, "offset": offset}


@router.get(
    "/reasoning-logs/export",
    summary="Export reasoning logs (JSONL)",
    description="Export filtered reasoning logs as NDJSON for offline analysis.",
    responses={
        200: {"description": "NDJSON stream", "content": {"application/x-ndjson": {}}},
        401: {"description": "Not authenticated"},
    },
)
async def export_reasoning_logs(
    request: Request,
    show_id: int = Query(..., description="Show ID to export"),
    action_type: str | None = Query(None),
    bpm_min: float | None = Query(None),
    bpm_max: float | None = Query(None),
    key: str | None = Query(None),
    instrument: str | None = Query(None),
    set_name: str | None = Query(None),
):
    """Export filtered reasoning logs as JSONL."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        _require_show_owner(show_id, request, session)
        query = session.query(LLMInteraction).filter(LLMInteraction.show_id == show_id)
        query = _eq_filter(query, "action_type", action_type)
        bpm_col = _column("bpm")
        if bpm_min is not None and bpm_col is not None:
            query = query.filter(bpm_col >= bpm_min)
        if bpm_max is not None and bpm_col is not None:
            query = query.filter(bpm_col <= bpm_max)
        query = _eq_filter(query, "key", key)
        query = _eq_filter(query, "set_name", set_name)
        inst_col = _column("instruments")
        if instrument is not None and inst_col is not None:
            if db_manager.is_postgres:
                query = query.filter(inst_col.op("@>")(f'["{instrument}"]'))
            else:
                query = query.filter(inst_col.like(f"%{instrument}%"))
        interactions = query.order_by(LLMInteraction.loop_index).all()

        async def generate():
            for interaction in interactions:
                dump = interaction.to_reasoning_export_dict()
                yield json.dumps(dump) + "\n"

        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"show_{show_id}_reasoning_{timestamp_str}.jsonl"
        return StreamingResponse(
            generate(),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )


@router.get(
    "/reasoning-timeline",
    summary="Get reasoning timeline",
    description="Aggregates Conductor decisions into time segments for visualization.",
    responses={200: {"description": "Timeline segments"}, 401: {"description": "Not authenticated"}},
)
async def get_reasoning_timeline(
    request: Request,
    show_id: int = Query(..., description="Show ID"),
    segment_seconds: int = Query(30, ge=5, le=300, description="Seconds per segment"),
):
    """Get a timeline of Conductor decisions aggregated into time segments."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        _require_show_owner(show_id, request, session)
        interactions = (
            session.query(LLMInteraction)
            .filter(LLMInteraction.show_id == show_id)
            .order_by(LLMInteraction.relative_time_ms.asc())
            .all()
        )
        if not interactions:
            return {"segments": [], "total_interactions": 0}
        segments = []
        current_segment = None
        for interaction in interactions:
            rel_ms = interaction.relative_time_ms or 0
            seg_index = (rel_ms // 1000) // segment_seconds
            if current_segment is None or current_segment["seg_index"] != seg_index:
                if current_segment is not None:
                    segments.append(current_segment)
                segment_start_ms = seg_index * segment_seconds * 1000
                current_segment = {
                    "seg_index": seg_index,
                    "start_ms": segment_start_ms,
                    "end_ms": segment_start_ms + segment_seconds * 1000,
                    "start_time_formatted": _format_time(segment_start_ms),
                    "action_counts": {"retain": 0, "add": 0, "remove": 0, "other": 0},
                    "avg_bpm": 0.0,
                    "bpm_sum": 0.0,
                    "bpm_count": 0,
                    "instruments_used": set(),
                    "key_changes": [],
                    "reasoning_snippets": [],
                    "interaction_ids": [],
                }
            seg = current_segment
            seg["interaction_ids"].append(interaction.id)
            at = interaction.action_type or "other"
            if at in seg["action_counts"]:
                seg["action_counts"][at] += 1
            else:
                seg["action_counts"]["other"] += 1
            if interaction.bpm is not None:
                seg["bpm_sum"] += interaction.bpm
                seg["bpm_count"] += 1
            if interaction.instruments:
                for inst in interaction.instruments:
                    seg["instruments_used"].add(inst)
            if interaction.key is not None:
                seg["key_changes"].append(
                    {"loop_index": interaction.loop_index, "key": interaction.key, "time_ms": rel_ms}
                )
            if interaction.reasoning:
                seg["reasoning_snippets"].append(
                    {
                        "loop_index": interaction.loop_index,
                        "time_ms": rel_ms,
                        "reasoning": interaction.reasoning[:200],
                        "action_type": interaction.action_type,
                    }
                )
        if current_segment is not None:
            segments.append(current_segment)
        for seg in segments:
            if seg["bpm_count"] > 0:
                seg["avg_bpm"] = round(seg["bpm_sum"] / seg["bpm_count"], 1)
            del seg["bpm_sum"]
            del seg["bpm_count"]
            seg["instruments_used"] = sorted(seg["instruments_used"])
            seg["interaction_count"] = len(seg["interaction_ids"])
        return {
            "segments": segments,
            "total_interactions": len(interactions),
            "segment_seconds": segment_seconds,
            "total_segments": len(segments),
        }


@router.get(
    "/reasoning-logs/stats",
    summary="Get reasoning statistics",
    description=(
        "Returns aggregate statistics for a show Conductor decisions: "
        "action counts, BPM range, instruments, fallback rate."
    ),
    responses={200: {"description": "Aggregate statistics"}, 401: {"description": "Not authenticated"}},
)
async def get_reasoning_stats(
    request: Request,
    show_id: int = Query(..., description="Show ID"),
):
    """Get aggregate statistics for a show Conductor reasoning."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        _require_show_owner(show_id, request, session)
        interactions = session.query(LLMInteraction).filter(LLMInteraction.show_id == show_id).all()
        if not interactions:
            return {
                "total_interactions": 0,
                "action_counts": {},
                "avg_bpm": None,
                "bpm_range": None,
                "keys_used": [],
                "instruments_used": [],
                "fallback_count": 0,
                "avg_reasoning_length": 0,
            }
        action_counts = {}
        bpms = []
        keys_used = set()
        instruments_used = set()
        fallback_count = 0
        reasoning_lengths = []
        for i in interactions:
            at = i.action_type or "unknown"
            action_counts[at] = action_counts.get(at, 0) + 1
            if i.bpm is not None:
                bpms.append(i.bpm)
            if i.key:
                keys_used.add(i.key)
            if i.instruments:
                instruments_used.update(i.instruments)
            if i.was_fallback:
                fallback_count += 1
            if i.reasoning:
                reasoning_lengths.append(len(i.reasoning))
        return {
            "total_interactions": len(interactions),
            "action_counts": action_counts,
            "avg_bpm": round(sum(bpms) / len(bpms), 1) if bpms else None,
            "bpm_range": {"min": min(bpms), "max": max(bpms)} if bpms else None,
            "keys_used": sorted(keys_used),
            "instruments_used": sorted(instruments_used),
            "fallback_count": fallback_count,
            "fallback_rate": round(fallback_count / len(interactions), 3),
            "avg_reasoning_length": round(sum(reasoning_lengths) / len(reasoning_lengths), 1)
            if reasoning_lengths
            else 0,
        }


def _format_time(ms: int) -> str:
    """Format milliseconds as MM:SS."""
    total_seconds = ms // 1000
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes:02d}:{seconds:02d}"
