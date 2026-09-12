"""Reasoning log viewer routes - search, filter, export, and timeline
for Conductor LLM decisions.
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func

from app.auth import get_current_user_from_request
from app.db import DatabaseManager
from app.lib.export_chunks import chunked_shaped_rows, ndjson_lines
from app.models import LLMInteraction, Show
from app.routes.utils import fetch_owned_show

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


def _instrument_containment(inst_col, instrument: str):
    """Postgres JSON-containment filter binding for the instruments column.

    json.dumps, not an f-string: an instrument containing a quote or backslash
    used to build invalid JSON (e.g. ["x""]) and 500 the endpoint with
    'invalid input syntax for type json' (review SEC-7).
    """
    return inst_col.op("@>")(json.dumps([instrument]))


def _eq_filter(query, name: str, value):
    """Equality-filter on a column only when both the column and value exist."""
    col = _column(name)
    if value is None or col is None:
        return query
    return query.filter(col == value)


def _apply_export_filters(query, is_postgres, action_type, bpm_min, bpm_max, key, set_name, instrument):
    """Filter chain shared verbatim by the search and export routes.

    Same order/filters both routes used to duplicate inline; the instrument
    containment keeps its dialect split (SEC-7).
    """
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
        if is_postgres:
            query = query.filter(_instrument_containment(inst_col, instrument))
        else:
            query = query.filter(inst_col.like(f"%{instrument}%"))
    return query


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
        query = _apply_export_filters(
            query, db_manager.is_postgres, action_type, bpm_min, bpm_max, key, set_name, instrument
        )
        query = _eq_filter(query, "was_fallback", was_fallback)
        reasoning_col = _column("reasoning")
        if q is not None and reasoning_col is not None:
            if db_manager.is_postgres:
                query = query.filter(reasoning_col.ilike(f"%{q}%"))
            else:
                query = query.filter(reasoning_col.like(f"%{q}%"))
        total = query.count()
        interactions = query.order_by(LLMInteraction.timestamp.desc()).limit(limit).offset(offset).all()
        return {"interactions": [i.to_dict() for i in interactions], "total": total, "limit": limit, "offset": offset}


def _build_export_query(db_manager, show_id, action_type, bpm_min, bpm_max, key, instrument, set_name):
    """Bind the export filter chain at request time; EVERY chunk re-applies it (REL-13).

    Capturing ``is_postgres`` here keeps the dialect choice a request-time
    constant while each chunk gets a fresh session-bound query — the show_id
    scope can never fall off after page one (pinned by test T3).
    """
    is_postgres = db_manager.is_postgres

    def build(session):
        query = session.query(LLMInteraction).filter(LLMInteraction.show_id == show_id)
        return _apply_export_filters(query, is_postgres, action_type, bpm_min, bpm_max, key, set_name, instrument)

    return build


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
    # REL-13: sequenced auth (no nested sessions) — one short session per
    # chunk below, never two open at once.
    fetch_owned_show(db_manager, show_id, request)
    build_query = _build_export_query(
        db_manager, show_id, action_type, bpm_min, bpm_max, key, instrument, set_name
    )

    # REL-13: keyset-paginated scan, (loop_index, id) tiebreak (loop_index is
    # not unique within a show after a musical reset). Rows are shaped to
    # plain dicts inside each chunk's session, so the stream never touches a
    # detached ORM instance; the sync generator runs on Starlette's threadpool
    # (off the event loop) and never holds one session for the whole export.
    rows = chunked_shaped_rows(
        db_manager,
        build_query,
        (LLMInteraction.loop_index, LLMInteraction.id),
        (LLMInteraction.loop_index, LLMInteraction.id),
        LLMInteraction.to_reasoning_export_dict,
        lambda row: (row.loop_index, row.id),
    )
    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"show_{show_id}_reasoning_{timestamp_str}.jsonl"
    return StreamingResponse(
        ndjson_lines(rows),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _timeline_segment_key(segment_seconds: int):
    """Integer-bucketed segment index from relative_time_ms (None coalesced to 0).

    ``//`` (floordiv) renders plain int/int division — integer division on both
    SQLite and PG — matching the old Python ``(rel_ms // 1000) // segment_seconds``
    for the non-negative relative_time_ms the capture writes.
    """
    return func.coalesce(LLMInteraction.relative_time_ms, 0) // 1000 // segment_seconds


def _timeline_segment_aggregates(session, show_id: int, segment_seconds: int):
    """Per-segment count/avg(bpm)/action tallies in ONE GROUP BY (REL-13c)."""
    seg_key = _timeline_segment_key(segment_seconds).label("seg_index")
    return (
        session.query(
            seg_key,
            func.count(LLMInteraction.id).label("interaction_count"),
            func.avg(LLMInteraction.bpm).label("avg_bpm"),
            func.sum(case((LLMInteraction.action_type == "retain", 1), else_=0)).label("retain"),
            func.sum(case((LLMInteraction.action_type == "add", 1), else_=0)).label("add"),
            func.sum(case((LLMInteraction.action_type == "remove", 1), else_=0)).label("remove"),
        )
        .filter(LLMInteraction.show_id == show_id)
        .group_by(seg_key)
        .order_by(seg_key)
        .all()
    )


def _timeline_detail_rows(db_manager, show_id: int):
    """Column-projected keyset scan for the per-segment detail lists (REL-13c).

    Reads only the slim columns — the fat prompt_messages/parsed_response
    hydration was the actual hundreds-of-MB hazard. The scan orders by
    (relative_time_ms, id), NOT loop_index: a musical reset restarts
    loop_index while relative_time_ms keeps growing, so loop order would
    fragment segments.
    """

    def build_query(session):
        return session.query(
            LLMInteraction.id,
            LLMInteraction.loop_index,
            LLMInteraction.relative_time_ms,
            LLMInteraction.action_type,
            LLMInteraction.key,
            LLMInteraction.reasoning,
            LLMInteraction.instruments,
        ).filter(LLMInteraction.show_id == show_id)

    return chunked_shaped_rows(
        db_manager,
        build_query,
        (LLMInteraction.relative_time_ms, LLMInteraction.id),
        (LLMInteraction.relative_time_ms, LLMInteraction.id),
        lambda row: row,
        lambda row: (row.relative_time_ms, row.id),
    )


def _timeline_instruments(detail_rows) -> list[str]:
    """Sorted union of instrument names across one segment's rows."""
    instruments = set()
    for row in detail_rows:
        if row.instruments:
            instruments.update(row.instruments)
    return sorted(instruments)


def _timeline_key_changes(detail_rows) -> list[dict]:
    """Key-change entries (old semantics: key is not None)."""
    return [
        {"loop_index": row.loop_index, "key": row.key, "time_ms": row.relative_time_ms or 0}
        for row in detail_rows
        if row.key is not None
    ]


def _timeline_reasoning_snippets(detail_rows) -> list[dict]:
    """Per-loop reasoning snippets, truncated at 200 chars (old semantics)."""
    return [
        {
            "loop_index": row.loop_index,
            "time_ms": row.relative_time_ms or 0,
            "reasoning": row.reasoning[:200],
            "action_type": row.action_type,
        }
        for row in detail_rows
        if row.reasoning
    ]


def _timeline_segment_dict(agg, detail_rows, segment_seconds: int) -> dict:
    """One timeline segment: SQL aggregates + detail lists from the slim scan."""
    start_ms = agg.seg_index * segment_seconds * 1000
    retain, add, remove = agg.retain or 0, agg.add or 0, agg.remove or 0
    # action_type NULL/anything-not-retain-add-remove rolls up to "other",
    # exactly the old `action_type or "other"` Python branch.
    other = agg.interaction_count - retain - add - remove
    return {
        "seg_index": agg.seg_index,
        "start_ms": start_ms,
        "end_ms": start_ms + segment_seconds * 1000,
        "start_time_formatted": _format_time(start_ms),
        "action_counts": {"retain": retain, "add": add, "remove": remove, "other": other},
        "avg_bpm": round(agg.avg_bpm, 1) if agg.avg_bpm is not None else 0.0,
        "instruments_used": _timeline_instruments(detail_rows),
        "key_changes": _timeline_key_changes(detail_rows),
        "reasoning_snippets": _timeline_reasoning_snippets(detail_rows),
        "interaction_ids": [row.id for row in detail_rows],
        "interaction_count": agg.interaction_count,
    }


def _assemble_timeline_segments(aggregates, detail_rows, segment_seconds: int) -> list[dict]:
    """Merge the SQL per-segment aggregates with the slim detail scan (old row-loop semantics)."""
    details_by_seg: dict[int, list] = {}
    for row in detail_rows:
        rel_ms = row.relative_time_ms or 0
        seg_index = (rel_ms // 1000) // segment_seconds
        details_by_seg.setdefault(seg_index, []).append(row)
    return [
        _timeline_segment_dict(agg, details_by_seg.get(agg.seg_index, []), segment_seconds)
        for agg in aggregates
    ]


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
    # REL-13: sequenced auth (no nested sessions) so every read below holds
    # at most one short-lived session.
    fetch_owned_show(db_manager, show_id, request)
    with db_manager.session() as session:
        # REL-13c: count + GROUP BY aggregates in SQL; only the slim detail
        # columns stream through a page-bounded keyset scan — never the old
        # full-table ORM hydration of the fat prompt/response columns.
        total = session.query(func.count(LLMInteraction.id)).filter(LLMInteraction.show_id == show_id).scalar()
        if not total:
            return {"segments": [], "total_interactions": 0}
        aggregates = _timeline_segment_aggregates(session, show_id, segment_seconds)
        segments = _assemble_timeline_segments(
            aggregates, _timeline_detail_rows(db_manager, show_id), segment_seconds
        )
        return {
            "segments": segments,
            "total_interactions": total,
            "segment_seconds": segment_seconds,
            "total_segments": len(segments),
        }


def _stats_core_totals(session, show_id: int):
    """count/avg/min/max bpm + fallback tally + avg reasoning length in ONE SELECT (REL-13c).

    ``nullif(reasoning, '')`` makes empty-string reasoning NULL so
    ``avg(length(...))`` ignores it — the SQL mirror of the old
    ``if i.reasoning:`` guard; ``sum(case((was_fallback.is_(True), 1), else_=0))``
    treats NULL was_fallback as not-fallback exactly like ``if i.was_fallback:``.
    """
    return (
        session.query(
            func.count(LLMInteraction.id).label("total"),
            func.avg(LLMInteraction.bpm).label("avg_bpm"),
            func.min(LLMInteraction.bpm).label("min_bpm"),
            func.max(LLMInteraction.bpm).label("max_bpm"),
            func.sum(case((LLMInteraction.was_fallback.is_(True), 1), else_=0)).label("fallbacks"),
            func.avg(func.length(func.nullif(LLMInteraction.reasoning, ""))).label("avg_reasoning_length"),
        )
        .filter(LLMInteraction.show_id == show_id)
        .one()
    )


def _stats_action_counts(session, show_id: int) -> dict:
    """Per-action_type counts via GROUP BY; NULL/'' roll up to 'unknown' (old ``or 'unknown'``)."""
    kind = func.coalesce(func.nullif(LLMInteraction.action_type, ""), "unknown")
    rows = (
        session.query(kind.label("kind"), func.count(LLMInteraction.id))
        .filter(LLMInteraction.show_id == show_id)
        .group_by(kind)
        .all()
    )
    return dict(rows)


def _stats_keys_used(session, show_id: int) -> list[str]:
    """Distinct non-empty keys via GROUP BY (old ``if i.key:`` excluded NULL and '')."""
    rows = (
        session.query(LLMInteraction.key)
        .filter(LLMInteraction.show_id == show_id, LLMInteraction.key != "")
        .group_by(LLMInteraction.key)
        .all()
    )
    return sorted(key for (key,) in rows)


def _stats_instruments_used(db_manager, show_id: int) -> list[str]:
    """Set-union over the instruments JSON array via a column-projected keyset scan.

    The one aggregate SQL cannot do portably (PG jsonb_array_elements has no
    SQLite twin) — scan only (id, instruments), never the fat prompt/response
    columns, so the scan stays page-bounded like every other rel-13 read.
    """

    def build_query(session):
        return session.query(LLMInteraction.id, LLMInteraction.instruments).filter(
            LLMInteraction.show_id == show_id
        )

    rows = chunked_shaped_rows(
        db_manager,
        build_query,
        (LLMInteraction.id,),
        (LLMInteraction.id,),
        lambda row: row.instruments,
        lambda row: (row.id,),
    )
    instruments: set[str] = set()
    for value in rows:
        if value:
            instruments.update(value)
    return sorted(instruments)


def _stats_response(totals, action_counts: dict, keys_used: list, instruments_used: list) -> dict:
    """Shape the stats payload (same keys/rounding as the old Python-loop version)."""
    fallbacks = totals.fallbacks or 0
    return {
        "total_interactions": totals.total,
        "action_counts": action_counts,
        "avg_bpm": round(totals.avg_bpm, 1) if totals.avg_bpm is not None else None,
        "bpm_range": {"min": totals.min_bpm, "max": totals.max_bpm}
        if totals.min_bpm is not None
        else None,
        "keys_used": keys_used,
        "instruments_used": instruments_used,
        "fallback_count": fallbacks,
        "fallback_rate": round(fallbacks / totals.total, 3),
        "avg_reasoning_length": round(totals.avg_reasoning_length, 1)
        if totals.avg_reasoning_length is not None
        else 0,
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
    # REL-13: sequenced auth (no nested sessions) so every read below holds
    # at most one short-lived session.
    fetch_owned_show(db_manager, show_id, request)
    with db_manager.session() as session:
        # REL-13c: every read below is an aggregate or page-bounded scan —
        # no full-table ORM hydration of llm_interactions.
        totals = _stats_core_totals(session, show_id)
        if not totals.total:
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
        return _stats_response(
            totals,
            _stats_action_counts(session, show_id),
            _stats_keys_used(session, show_id),
            _stats_instruments_used(db_manager, show_id),
        )


def _format_time(ms: int) -> str:
    """Format milliseconds as MM:SS."""
    total_seconds = ms // 1000
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes:02d}:{seconds:02d}"
