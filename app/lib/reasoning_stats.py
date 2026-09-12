"""Timeline/stats compute for the reasoning-log viewer (FU-4, rel-fu-exports).

FU-4 provenance: this module is a PURE MOVE of the timeline + stats helpers
from app/routes/reasoning_logs.py (which sat at 497/500 lines after rel-13 —
the rel-11 follow-up note from the rel-13 review), plus two public entry
points (``compute_timeline_payload`` / ``compute_stats_payload``) that the
routes run via ``asyncio.to_thread`` so stats/timeline do NO DB work on the
event loop. Previously auth (2 sessions), the aggregates and the per-chunk
detail scans (~150 serial SELECTs on a 75k-row show) ran inline on the loop.

Session discipline (load-bearing): every phase opens its OWN short
``db_manager.session()`` inside the worker thread — count → aggregates →
per-chunk detail scans (timeline); totals → action counts → keys →
instruments scan (stats). Nothing session-bound crosses a phase boundary:
aggregates are column-projected Row tuples, detail rows are column-projected
Rows, the payload is plain dicts. At most ONE connection is open at any
moment (the route used to hold its aggregate session open across the chunk
scans — nested sessions).

No FastAPI imports — importable by routes, cleanup, and tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import case, func

from app.lib.export_chunks import chunked_shaped_rows
from app.models import LLMInteraction

if TYPE_CHECKING:
    from app.db import DatabaseManager


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


def _format_time(ms: int) -> str:
    """Format milliseconds as MM:SS."""
    total_seconds = ms // 1000
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes:02d}:{seconds:02d}"


def compute_timeline_payload(db_manager: DatabaseManager, show_id: int, segment_seconds: int) -> dict:
    """Full timeline payload for one show; every phase uses its OWN short session.

    Runs on a worker thread (the route's ``asyncio.to_thread`` hop) — FU-4's
    whole point is that this function does blocking DB work without touching
    the event loop. Semantics are the pre-split route body verbatim, except
    the phases are sequenced (count session closed before aggregates open,
    aggregate session closed before the chunk scans) so at most ONE
    connection is ever open — the route used to nest the chunk scans inside
    its aggregate session.

    Example::

        payload = compute_timeline_payload(DatabaseManager.get_instance(), 7, 30)
    """
    with db_manager.session() as session:
        total = session.query(func.count(LLMInteraction.id)).filter(LLMInteraction.show_id == show_id).scalar()
    if not total:
        return {"segments": [], "total_interactions": 0}
    with db_manager.session() as session:
        # REL-13c: count + GROUP BY aggregates in SQL; only the slim detail
        # columns stream through a page-bounded keyset scan — never the old
        # full-table ORM hydration of the fat prompt/response columns.
        aggregates = _timeline_segment_aggregates(session, show_id, segment_seconds)
    detail_rows = _timeline_detail_rows(db_manager, show_id)
    segments = _assemble_timeline_segments(aggregates, detail_rows, segment_seconds)
    return {
        "segments": segments,
        "total_interactions": total,
        "segment_seconds": segment_seconds,
        "total_segments": len(segments),
    }


def compute_stats_payload(db_manager: DatabaseManager, show_id: int) -> dict:
    """Full stats payload for one show; every phase uses its OWN short session.

    Thread-contract mirrors ``compute_timeline_payload``: run via
    ``asyncio.to_thread`` — blocking DB work, never on the event loop. The
    empty-show early return keeps the pre-split route's payload verbatim.

    Example::

        payload = compute_stats_payload(DatabaseManager.get_instance(), 7)
    """
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
    with db_manager.session() as session:
        action_counts = _stats_action_counts(session, show_id)
    with db_manager.session() as session:
        keys_used = _stats_keys_used(session, show_id)
    return _stats_response(
        totals,
        action_counts,
        keys_used,
        _stats_instruments_used(db_manager, show_id),
    )
