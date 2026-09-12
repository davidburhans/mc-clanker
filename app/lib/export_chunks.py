"""Chunked keyset export scans (REL-13): page-bounded SELECTs, one fresh
session per chunk, rows serialized to plain data inside the session.

The export routes used to capture ORM instance lists inside their route
session and let Starlette iterate them after that session committed+closed
(expire_on_commit=True) → DetachedInstanceError after the headers were
already sent → empty/truncated exports (broken fine-tuning extraction).
Shaping rows to plain dicts inside the session makes detachment impossible
by construction; the per-chunk page LIMIT keeps every statement at a few
milliseconds — far under rel-09's engine-wide 10 s statement_timeout — and
returns its connection to the pool before the next chunk opens.
"""

import json
import logging

from sqlalchemy import tuple_

logger = logging.getLogger(__name__)

# Page size for every export scan. Bounds SERVER MEMORY only — the exports
# themselves stay complete (no response limit, invariant 4). Module constant,
# not an env knob; tests monkeypatch it to exercise multi-chunk scans.
EXPORT_CHUNK_ROWS = 500


def chunked_shaped_rows(
    db_manager, build_query, order_cols, cursor_cols, shaper, row_key, page_size=None
):
    """Yield shaper(row) dicts across a show-sized table, one short session per chunk.

    build_query(session) returns the show-scoped (+user-filtered) query WITHOUT
    order/limit; EVERY chunk re-applies it (the classic keyset bug is dropping
    the WHERE after page one), adds the keyset predicate on cursor_cols, orders
    by order_cols and takes page_size rows. The session closes (commit +
    connection back to the pool) before any value is yielded, so no ORM
    instance ever crosses a session boundary (REL-13a) and at most one
    connection is held at any moment (rel-09 pool budget). A failing chunk
    query raises out of the generator — a mid-stream DB blip aborts the
    export loudly instead of silently truncating the corpus.

    Example::

        chunked_shaped_rows(db, lambda s: s.query(LLMInteraction).filter(
                                LLMInteraction.show_id == 7),
            (LLMInteraction.loop_index, LLMInteraction.id),
            (LLMInteraction.loop_index, LLMInteraction.id),
            LLMInteraction.to_llm_dump_dict, lambda r: (r.loop_index, r.id))
    """
    size = page_size or EXPORT_CHUNK_ROWS
    cursor = None
    while True:
        try:
            with db_manager.session() as session:
                query = build_query(session)
                if cursor is not None:
                    query = query.filter(tuple_(*cursor_cols) > cursor)
                # Fetch one row past the page boundary to detect exhaustion inside
                # the SAME session — exactly ceil(N/page) sessions per scan, no
                # extra probe session after an exactly-full last page.
                fetched = query.order_by(*order_cols).limit(size + 1).all()
                has_more = len(fetched) > size
                rows = fetched[:size]
                shaped = [shaper(row) for row in rows]  # in-session: no detach
                cursor = row_key(rows[-1]) if rows else None
        except Exception:
            # Plan decision 8 (review round-1): abort WITH context — a silent
            # mid-stream truncation would corrupt the fine-tuning corpus
            # extraction (invariant 4), so name the cursor + page before re-raising.
            logger.exception(
                "Export chunk failed at cursor=%r page_size=%s — aborting stream", cursor, size
            )
            raise
        if not shaped:
            return
        yield from shaped
        if not has_more:
            return


def ndjson_lines(row_dicts):
    """Render shaped row dicts as one JSON line each (format-stable export)."""
    for row in row_dicts:
        yield json.dumps(row) + "\n"
