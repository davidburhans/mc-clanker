"""Reasoning log viewer routes - search, filter, export, and timeline
for Conductor LLM decisions.

FU-4: the timeline/stats compute lives in app/lib/reasoning_stats.py and runs
via ``asyncio.to_thread`` — no DB statement in this module executes on the
event loop anymore (the rel-11 residual from the rel-13 review); search and
export keep their route bodies here (export's chunks already run on
Starlette's threadpool).
"""

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.auth import get_current_user_from_request
from app.db import DatabaseManager
from app.lib.export_chunks import chunked_shaped_rows, ndjson_lines
from app.lib.reasoning_stats import compute_stats_payload, compute_timeline_payload
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
    # FU-4 (rel-11 residual from the rel-13 review): every DB statement in
    # this route — auth included — runs on a worker thread. Inline, the
    # per-chunk detail scan (~150 serial SELECTs on a 75k-row show) stalled
    # the event loop for the whole request. compute_timeline_payload
    # sequences count → aggregates → chunk scans as short single sessions
    # (never two open at once), preserving the REL-13 sequencing.
    await asyncio.to_thread(fetch_owned_show, db_manager, show_id, request)
    return await asyncio.to_thread(compute_timeline_payload, db_manager, show_id, segment_seconds)


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
    # FU-4 (rel-11 residual from the rel-13 review): auth + the aggregate
    # reads all run on a worker thread — each used to stall the event loop
    # inline. compute_stats_payload sequences the phases as short single
    # sessions (never two open at once), preserving the REL-13 sequencing.
    await asyncio.to_thread(fetch_owned_show, db_manager, show_id, request)
    return await asyncio.to_thread(compute_stats_payload, db_manager, show_id)
