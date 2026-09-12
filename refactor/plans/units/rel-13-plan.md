# PLAN — Unit 11 `rel-exports` (REL-13 + REL-30), branch `rel-13-exports`
**Spec:** `refactor/plans/rel-remediation-plan.md` §U11 (incl. the rel-09 statement_timeout note) · `docs/reliability_audit.md` REL-13 [High], REL-30 [Low] · soak spec #8 (real-session export)
**Baseline gate at HEAD of `main` (after U10 landed):** `.venv/bin/python -m ruff check app tests` → clean (verified). `.venv/bin/python -m pytest tests/ -q` → **1116 passed / 16 skipped** (verified green). Do not regress skips without cause.
Preflight: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q`

Verified against code at HEAD (line refs current):
`app/routes/reasoning_logs.py` 326 L (clamp pattern `limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)` at **:81-82**; search `.all()` at :111 — bounded+clamped, untouched; export route :124 with unbounded `.all()` at **:153**; timeline :175 with `.all()` at **:188**; stats :267 with `.all()` at **:275**) · `app/routes/shows.py` 781 L (unclamped `limit` at **:238** `list_shows`, **:521** `get_show_actions`, **:542** `get_show_llm_interactions`; llm-dump export :643 with `.all()` at **:690**; `/export/full` :705 with `.all()` at **:711-712**) · `app/routes/jobs.py` 325 L (unclamped `limit: int = 50` at **:190**, `.all()` :210 — bounded, needs clamp only) · `app/db.py:70-79` `session()` commits+closes on `with` exit; `sessionmaker` default `expire_on_commit=True` → every attribute read after exit is a `DetachedInstanceError` (mechanism confirmed); **:26** `DB_STATEMENT_TIMEOUT_MS = 10_000` is engine-wide on the PG branch (rel-09) · shapers: `app/models/llm_interaction.py` `llm_dump_row`/`_DUMP_COLUMNS`/`to_llm_dump_dict`/`to_reasoning_export_dict`/`to_dict`, `app/models/show_action.py` `show_action_row`/`to_dict`; composite indexes `ix_llm_interactions_show_loop (show_id, loop_index)` + `ix_show_actions_show_loop (show_id, loop_index)` · training consumers: `training/dpo_pipeline.py` (`_extract_assistant_message` over `messages`, `validate_conductor_schema` over the assistant JSON), `training/convert_to_unsloth_dataset.py` (`messages` rows, inserts system turn) · Starlette 1.3.1: `StreamingResponse.__init__` wraps any non-AsyncIterable in `iterate_in_threadpool` (verified) → **sync generators run their body on anyio worker threads, never the event loop** · SQLAlchemy 2.0.51 + SQLite 3.53.1: `sqlalchemy.tuple_` row-value keyset predicate verified green on the actual stack · tests: `tests/conftest.py` autouse `reset_db_singleton` resets `DatabaseManager._instance` around every test (isolated-engine fixture composes cleanly); `tests/test_llm_capture.py:44` env-pin + T11 real-sqlite round-trip precedent (:867), T12 dump-shape pin (:894); `tests/test_reasoning_logs.py` mock-chain tests (the reason the bug is invisible — rewritten here, see §3.2); `tests/test_auth.py` `create_access_token` precedent; `app/routes/utils.py:17` `require_show_owner`.

---

## 0. Scope summary

| Item | Root cause | Fix site |
|---|---|---|
| REL-13a (exports broken on real sessions) | both export generators capture ORM instance lists inside `with db_manager.session()` and only iterate them **after** the route returns; the session context commits (expiry) + closes, so Starlette's first `next()` hits a detached instance → `DetachedInstanceError` after headers sent → empty/truncated body = **broken fine-tuning extraction** (invariant 4) | serialize rows to plain dicts **inside** each session; hand Starlette pre-shaped data only |
| REL-13b (unbounded `.all()`) | `reasoning_logs.py:153/:188/:275`, `shows.py:690/:711-712` load entire per-show tables (a week-long show ≈ 75 k interactions ≈ hundreds of MB, dominated by the fat `prompt_messages`/`parsed_response` JSON columns) | keyset pagination, fresh short-lived session per chunk, page-bounded SELECTs; timeline/stats stop hydrating the fat columns entirely |
| REL-13c (stats/timeline aggregate in Python) | full-table ORM hydration then Python loops for counts/avg/ranges | SQL `GROUP BY` + aggregate functions; the one non-portable aggregate (JSON-array set-union) via a column-projected chunked scan |
| REL-13d (rel-09 interaction) | engine-wide `statement_timeout=10 s` aborts any export SELECT big enough to matter — unpaginated exports would `QueryCanceled` mid-stream on large corpora | every chunk SELECT touches ≤ `EXPORT_CHUNK_ROWS` index-ordered rows of one show → milliseconds, far under the budget; connection returns to the pool between chunks |
| REL-30 (unclamped `limit`) | `jobs.py:190`, `shows.py:238/:521/:542` accept `limit=10⁹` (and `limit=0`/negative) | mirror the `reasoning_logs.py:81-82` `Query(ge=, le=)` clamp pattern |

**Cold (documented, deliberately NOT touched):** search route `reasoning_logs.py:57-113` (already clamped + bounded); `shows.py` CRUD/start/stop/playback routes (rel-09 §0 cold list — pre-existing sync-DB-in-async-def debt, spec'd out of scope there and here); `app/retention.py` archive fetch (asyncpg on the cleanup service's own connection, not this engine — REL-16 territory); soak-module reuse (U15 will point at this unit's fixture). No mixer, worker, capture-schema, claim-SQL, or retention code is touched (invariants 1, 3, 4, 5).

---

## 1. Design decisions (documented reasoning; deviations from spec letter flagged)

1. **Serialize-in-session + plain sync generator; Starlette's threadpool gives off-loop DB for free.** The export generators become ordinary **sync** generators (not `async def`): Starlette 1.3.1 wraps non-AsyncIterable content in `iterate_in_threadpool`, so each `next()` — and therefore each chunk's blocking SQLAlchemy call — runs on an anyio worker thread, never the event loop (the rel-09 off-loop pattern with zero `to_thread` code). Each chunk: open `with db_manager.session()`, fetch ≤ page-size rows, map to dicts via the existing shaper delegates **inside the session**, exit the context (commit+close → connection back to the pool), *then* `yield` the pre-shaped lines. No ORM instance and no unexpired attribute ever crosses a session boundary → `DetachedInstanceError` is impossible by construction, not by mock.

2. **Keyset pagination on `(loop_index, id)` — not OFFSET, not `yield_per`.** OFFSET scans grow linearly with depth; keyset stays O(page) per chunk and is index-backed by `ix_llm_interactions_show_loop (show_id, loop_index)` with the PK as tiebreaker. `loop_index` is **not unique within a show** (the rel-02 follow-up documents post-reset duplicate `loop_index` rows), so the `id` tiebreaker is mandatory for cursor correctness. Predicate: `tuple_(loop_index, id) > (cursor_loop, cursor_id)` — verified working on the actual SQLite runtime (3.53.1) and standard PG row values. The `show_id` filter is applied by `build_query` in **every** chunk (pinned by test T3 — the classic keyset bug is dropping the WHERE after page one). Why not `yield_per`: it still binds ONE long-running SELECT + ONE held connection for the whole stream; under rel-09's 10 s `statement_timeout` a full-corpus SELECT `QueryCanceled`s mid-stream, and the single connection is held across the entire download (pool pressure). Fresh-session-per-chunk bounds each statement *and* releases the connection between chunks — this is the audit's "fresh session per chunk" letter, and the scout's "per-chunk sessions are mandatory, not optional".

3. **Ordering strengthened, not changed:** `order_by(loop_index, id)` replaces `order_by(loop_index)` (llm-dump, reasoning-logs export, actions). Loop order is preserved; only the previously *arbitrary* tie order within one `loop_index` becomes deterministic. Each NDJSON line is byte-identical to before (`json.dumps(shaped_row) + "\n"`, same defaults). The timeline detail scan keeps today's exact `order_by(relative_time_ms, id)` ordering (see decision 7).

4. **Page size is a module constant, not an env knob.** `EXPORT_CHUNK_ROWS = 500` in the new helper module, read at call time (`page_size: int | None = None` → `or EXPORT_CHUNK_ROWS`) so tests monkeypatch it (same convention as `DB_STATEMENT_TIMEOUT_MS` / `JOB_PENDING_DEPTH_LIMIT`; no speculative config). 500 shaped rows ≈ the same order as the audit flush threshold (200); memory per chunk is bounded regardless of show size.

5. **Row shapes are pinned to the existing shapers — zero format drift (invariant 4 + rel-04/rel-16 NDJSON compatibility).** The chunk helper takes the row serializer as a callable; routes pass the **existing** delegates, so the exported shapes cannot drift from the ORM/archive paths:
   - `/shows/{id}/export/llm-dump` rows: `LLMInteraction.to_llm_dump_dict()` → `llm_dump_row` (`messages` chat + `response` + `meta` with every captured field) — the exact shape `training/dpo_pipeline.py` (`_extract_assistant_message`), `training/convert_to_unsloth_dataset.py` (`messages`), `tests/test_dpo_pipeline.py`, and the rel-16 retention archive consume. Untouched.
   - `/llm-config/reasoning-logs/export` rows: `to_reasoning_export_dict()` (slim viewer shape — same key set, pinned by test).
   - `/export/full` + show-action rows: `to_dict()` (ShowAction delegates to `show_action_row`).
   Exports remain **complete by design** (no `limit` on the export endpoints): they are the fine-tuning extraction path; truncating them would violate invariant 4. `EXPORT_CHUNK_ROWS` bounds server memory only, never the response.

6. **`/export/full` streams its JSON document instead of materializing it.** Its response contract (`{"show": {...}, "actions": [...], "llm_interactions": [...]}`) is preserved exactly — a fragment generator emits the head (`show.to_dict()`), then action rows via the chunk iterator (`to_dict()` shaped in-session, `,`-joined), then interaction rows the same way, then the tail. JSON is whitespace-insensitive, so consumers see an identical document; the server never holds the whole corpus in RAM. (Flagged vs the audit letter: this endpoint is NDJSON-*adjacent* but was never NDJSON; converting its format would break consumers, so it stays a JSON document — only the *loading strategy* changes.)

7. **Stats = SQL aggregates; timeline = SQL GROUP BY for the aggregate half + one slim chunked scan for the detail half.** Deviation from the audit's one-liner ("GROUP BY for stats/timeline"), reasoned:
   - **Stats** (`/reasoning-logs/stats`) goes fully SQL: `count(id)`, `avg/min/max(bpm)`, fallback count via `sum(case((was_fallback.is_(True), 1), else_=0))`, `avg(length(nullif(reasoning, '')))` — `nullif` makes empty-string reasoning NULL so `avg` ignores it, matching today's `if i.reasoning:` guard exactly (plain `length` would count `''` as 0); `action_counts` via one `GROUP BY action_type` (SQL `NULL` key → `"unknown"` in Python, matching today's `or "unknown"`); `keys_used` via `GROUP BY key` (distinct, sorted, NULL excluded — matches today's `if i.key`).
   - **`instruments_used`** is the one aggregate SQL cannot do portably: it is a set-union over a JSON *array* column (PG `jsonb_array_elements` has no SQLite equivalent — forking dialect paths for one tiny aggregate is not worth it). It is computed by a **column-projected keyset scan of the `instruments` column only** (`order_by(id)`, cursor `id > last`) — bounded memory, no fat columns, no ORM hydration.
   - **Timeline** (`/reasoning-timeline`): the response contract embeds per-segment detail lists (`interaction_ids`, `key_changes`, `reasoning_snippets`, `instruments_used`) that `GROUP BY` cannot produce. Design: (a) `total_interactions` = `count(id)` SQL; (b) per-segment aggregates — group key `coalesce(relative_time_ms, 0) / 1000 / :segment_seconds` (integer division on both dialects; `coalesce` preserves today's `or 0`), per-segment `count`, `avg(bpm)`, conditional counts for retain/add/remove/other — one `GROUP BY` query; (c) one **column-projected chunked scan** for the detail lists, reading only `id, loop_index, relative_time_ms, action_type, key, reasoning, instruments` in **today's `order_by(relative_time_ms)` order** (keyset `(relative_time_ms, id)`; NOT loop order — post-reset shows have monotonically growing `relative_time_ms` with restarted `loop_index`, so loop-ordered scans would fragment segments). The fat `prompt_messages`/`parsed_response` columns are never loaded — that hydration was the actual hundreds-of-MB hazard. Note: `(relative_time_ms, id)` is not index-covered beyond the `show_id` prefix, so each chunk re-sorts its filtered set — fine at these sizes; an optional `(show_id, relative_time_ms)` index is a documented follow-up if profiling ever demands it.
   Segment assembly keeps today's semantics verbatim: only non-empty segments exist (GROUP BY naturally skips gaps), `avg_bpm` falls back to `0.0` when a segment has no bpm rows, `round(…, 1)` preserved.

8. **Mid-stream failure semantics: log, abort, never silently truncate.** If a chunk query raises mid-export (DB blip), the generator logs (module logger, statement + show_id) and re-raises → Starlette terminates the stream. Every already-yielded line is complete NDJSON (lines are only appended after full serialization). No sentinel/trailer line is added — that would break format stability — so consumers detect truncation the same way they do today: row count vs expectation. A skip-and-continue would silently drop corpus rows (invariant 4 violation); aborting is the honest failure.

9. **Limit clamps mirror the existing pattern, with one reasoned split.** `jobs.py list_jobs` and `shows.py list_shows` get exactly `reasoning_logs.py:81-82`: `limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)`. `get_show_actions` / `get_show_llm_interactions` keep their **default 1000** (the viewer UI pages at 1000; clamping to 500 would 422 the default request) and gain `Query(1000, ge=1, le=5000)` — 5000 slim `to_dict` rows is a bounded worst case (~single-digit MB). Out-of-range values now return **422** (FastAPI validation) instead of silently honoring `limit=10⁹` — identical contract to the existing reasoning-logs search endpoint. Plain-default → `Query()` conversion is transparent for valid inputs.

10. **The shared chunk primitive lives in a new `app/lib/export_chunks.py`.** Both `reasoning_logs.py` (326 L) and `shows.py` (781 L, already over the 500-L brownfield bound — same disclosed debt as `worker.py`; this unit must not grow it meaningfully) import it. `app/lib/` is the established shared-utility home (`paths.py`, `wav.py`); the module imports only SQLAlchemy + stdlib (no FastAPI) → importable by routes, cleanup, and tests; every function 4–20 lines, no `Any`.

11. **Real-session tests via an isolated engine per test.** Fixture: `monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/rel13.db")` → `DatabaseManager._instance = None` → `get_instance().create_tables()` (the `app/db.py:41` `elif database_url:` branch honors it verbatim; conftest's autouse `reset_db_singleton` restores order afterwards). Real `User` row + `create_access_token(user_id)` Bearer header + real `Show` row → the **entire** request path (auth middleware → `get_current_user_from_request` → `require_show_owner` → route → shaper) runs with **zero DB mocks** — the gap that hid REL-13a. `state.reset()` + empty `dj_password`/`audience_password` in the fixture (test_reasoning_logs pattern) keeps the Basic gate out of the way.

---

## 2. Exact changes per file

### 2.1 NEW `app/lib/export_chunks.py` (~85 lines)
```python
"""Chunked keyset export scans (REL-13): page-bounded SELECTs, one fresh
session per chunk, rows serialized to plain data inside the session.

The export routes used to capture ORM instance lists and let Starlette
iterate them after the session committed+closed (expire_on_commit=True)
-> DetachedInstanceError after headers were sent; and one unpaginated
.all() would exceed rel-09's engine-wide 10 s statement_timeout on large
corpora. Shape the rows in-session, hand out only plain dicts/Rows.
"""

EXPORT_CHUNK_ROWS = 500  # monkeypatchable; NOT an env knob


def chunked_shaped_rows(db_manager, build_query, order_cols, cursor_cols,
                        shaper, row_key, page_size=None):
    """Yield shaper(row) dicts, one short-lived session per chunk (REL-13).

    build_query(session) returns the show-scoped (+user-filtered) query
    WITHOUT order/limit; each chunk adds the keyset predicate on cursor_cols,
    orders by order_cols and takes page_size rows. row_key(row) extracts the
    cursor tuple from the last raw row of a chunk. The session closes (commit
    + connection back to the pool) before any value is yielded.

    Example: chunked_shaped_rows(db, lambda s: s.query(LLMInteraction)
        .filter(LLMInteraction.show_id == 7), (LLMInteraction.loop_index,
        LLMInteraction.id), (LLMInteraction.loop_index, LLMInteraction.id),
        LLMInteraction.to_llm_dump_dict, lambda r: (r.loop_index, r.id))
    """
    size = page_size or EXPORT_CHUNK_ROWS
    cursor = None
    while True:
        with db_manager.session() as session:
            query = build_query(session)
            if cursor is not None:
                query = query.filter(tuple_(*cursor_cols) > cursor)
            rows = query.order_by(*order_cols).limit(size).all()
            shaped = [shaper(row) for row in rows]   # in-session: no detach
            cursor = row_key(rows[-1]) if rows else None
        if not shaped:
            return
        yield from shaped


def ndjson_lines(row_dicts):
    """Render shaped row dicts as one JSON line each (format-stable export)."""
    for row in row_dicts:
        yield json.dumps(row) + "\n"
```
(`shaper` may be the identity for column-projected scans — SQLAlchemy `Row` objects hold plain values, no expiry risk. Column scans keyset on `id` alone with `order_cols=cursor_cols=(LLMInteraction.id,)`.)

### 2.2 `app/routes/reasoning_logs.py` (326 → ~340 lines)
**(a)** Imports: `from sqlalchemy import case, func, tuple_` (tuple_ only if not re-exported by the helper — import from `sqlalchemy` here for the local scans) + `from app.lib.export_chunks import EXPORT_CHUNK_ROWS, chunked_shaped_rows, ndjson_lines`.
**(b)** `export_reasoning_logs` (:124-167): keep auth + filter building verbatim (extract the filter chain into a `_export_filter(query, filters...)` helper reused verbatim — it is duplicated today between search/export; extracting it is the no-duplication rule, not a behavior change). Replace the `.all()` + closure generator with:
```python
        db_manager = DatabaseManager.get_instance()
        with db_manager.session() as session:
            _require_show_owner(show_id, request, session)   # short session, unchanged
        lines = ndjson_lines(chunked_shaped_rows(
            db_manager, _build_export_query(db_manager, show_id, action_type=..., ...),
            (LLMInteraction.loop_index, LLMInteraction.id),
            (LLMInteraction.loop_index, LLMInteraction.id),
            LLMInteraction.to_reasoning_export_dict,
            lambda r: (r.loop_index, r.id),
        ))
        return StreamingResponse(lines, media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{filename}'})  # filename built as today
```
(`_build_export_query(db_manager, show_id, ...)` returns `lambda session: <filtered query>` applying exactly today's `_eq_filter`/bpm/instrument filters — `db_manager.is_postgres` captured at build time. Sync generator → threadpool iteration per decision 1.)
**(c)** `get_reasoning_stats` (:267-319): replace the full-table loop with the decision-7 SQL block — one aggregate query, one `GROUP BY action_type`, one `GROUP BY key`, plus `chunked_shaped_rows(..., shaper=lambda r: r.instruments, row_key=lambda r: (r.id,), order_cols=cursor_cols=(LLMInteraction.id,))` for the instruments union (first non-None `instruments` value drives the set-union; `build_query` = show filter). Response keys/ordering/rounding identical (incl. `fallback_rate` only when `total > 0` — today's zero-division guard via early empty-return).
**(d)** `get_reasoning_timeline` (:175-256): `total = session.query(func.count(LLMInteraction.id)).filter(show).scalar()`; per-segment aggregate via `GROUP BY (coalesce(relative_time_ms,0)/1000)/:seg_s` with `count`, `avg(bpm)`, conditional action counts; detail lists via one column-projected `chunked_shaped_rows` scan (`id, loop_index, relative_time_ms, action_type, key, reasoning, instruments`; `order_by(relative_time_ms, id)`, cursor `(relative_time_ms, id)`); segment assembly loops over the detail scan merging the SQL aggregates by `seg_index` — segment dict shape, `_format_time`, `reasoning[:200]` truncation, `instruments_used` sorting, `interaction_count`, and the empty-show early return all verbatim.

### 2.3 `app/routes/shows.py` (781 → ~795 lines)
**(a)** Import `chunked_shaped_rows, ndjson_lines` from `app.lib.export_chunks`.
**(b)** `export_llm_dump` (:643-703): same shape as §2.2(b) with `LLMInteraction.to_llm_dump_dict` as the shaper and today's filename header.
**(c)** `export_full_show` (:705-727): replace both `.all()`s with a fragment generator over two chunk iterators:
```python
def _full_show_json_fragments(db_manager, show_id, show_dict):
    """Stream the /export/full JSON doc: head, chunked actions, chunked interactions, tail (REL-13)."""
    yield '{"show": ' + json.dumps(show_dict) + ', "actions": ['
    yield from _json_array_fragments(chunked_shaped_rows(db_manager,
        _actions_query(show_id), (ShowAction.loop_index, ShowAction.id), ...,
        ShowAction.to_dict, lambda r: (r.loop_index, r.id)))
    yield '], "llm_interactions": ['
    yield from _json_array_fragments(chunked_shaped_rows(db_manager,
        _interactions_query(show_id), (LLMInteraction.loop_index, LLMInteraction.id), ...,
        LLMInteraction.to_dict, lambda r: (r.loop_index, r.id)))
    yield ']}'
```
(`_json_array_fragments` yields `json.dumps(row)` pieces joined with `,` — 6 lines; `show.to_dict()` serialized in the ownership-check session; media type stays `application/json` via `StreamingResponse`.)
**(d)** Clamps (decision 9): `list_shows` → `limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)`; `get_show_actions` / `get_show_llm_interactions` → `limit: int = Query(1000, ge=1, le=5000), offset: int = Query(0, ge=0)` (+ `from fastapi import Query` already present? — verify; add if missing).

### 2.4 `app/routes/jobs.py` (325 → 326 lines)
`list_jobs` (:190-191): `limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)` (add `Query` import).

### 2.5 Tests — see §3.

### 2.6 Docs (pipeline "docs" stage, same unit)
- `docs/reliability_audit.md`: REL-13 gains `**Status: fixed-in rel-13-exports**` (chunked keyset exports, in-session shaping, SQL stats/timeline, clamps; mid-stream failure semantics; timeline hybrid rationale); REL-30 gains the same with the 422-clamp contract.
- `refactor/plans/rel-remediation-plan.md`: status row 11 → landed (commit at merge).
- `CLAUDE.md` API-layer table: add `app/lib/export_chunks.py` row (one line).

---

## 3. TDD regression tests (write first, confirm red, then implement)

### 3.1 NEW `tests/test_exports_pagination.py` (~420 lines) — real sessions, zero DB mocks
Shared fixture (decision 11): `_isolated_db(tmp_path, monkeypatch)` sets `DATABASE_URL=sqlite:///{tmp}/rel13.db`, resets + rebuilds the singleton, `create_tables()`, seeds one real `User` (BCRYPT-free: `hash_password("pw")`), returns `(db, user, auth_headers)` via `create_access_token(user.id)`; `_make_show(db, user_id, title)` inserts a real `Show`; `_insert_interactions(db, show_id, n, **overrides)` bulk-inserts real `LLMInteraction` rows (full captured-field payloads — reuse the `test_llm_capture.py` `_request_messages`/`_applied_actions`/`_valid_parsed_response` builders, imported or lifted); `state.reset()` + empty passwords autouse. `monkeypatch.setattr(export_chunks, "EXPORT_CHUNK_ROWS", 7)` where chunking is exercised.

| # | Test | Pins | Core assertions |
|---|---|---|---|
| T1 | `test_reasoning_export_real_session_complete_ndjson` (**acceptance, regression for DetachedInstanceError**) | REL-13a/b | 21 rows (> 3 chunks of 7); GET `/api/llm-config/reasoning-logs/export?show_id=…` → 200; exactly 21 parseable NDJSON lines; `loop_index` non-decreasing; every row's key set == `to_reasoning_export_dict` keys (format stability). **Red today**: TestClient raises `DetachedInstanceError` mid-stream. |
| T2 | `test_llm_dump_export_real_session_complete_ndjson` (**acceptance**) | REL-13a/b | same shape for `/api/shows/{id}/export/llm-dump`; every line has `messages`/`response`/`meta`; assistant turn is `json.dumps(parsed_response)` exactly. Red today (same mechanism). |
| T3 | `test_export_does_not_mix_concurrent_shows` | keyset WHERE retention | two shows, interleaved rows with **disjoint data markers** (`set_name`/`bpm`/`reasoning` differ per show, same loop_index ranges, N > page); export show A → exactly A's row count and **every** row carries only A's markers (meta carries no show_id by design — do NOT add one; data markers prove isolation). Engine event listener additionally asserts every captured chunk SQL contains the `show_id` predicate. |
| T4 | `test_export_roundtrips_every_captured_field` (**acceptance, invariant 4**) | capture→export losslessness | drive the REAL capture path: `append_loop_audit` × 2 loops (valid response + `_request_messages` + `_applied_actions`, page size 1 → chunked), `flush_recording_buffers()`, then export; per row: `messages == chat + [{"role": "assistant", "content": json.dumps(parsed)}]`, `response == parsed`, and `meta` == every captured field with the captured values (`loop_index, relative_time_ms, bpm, key, set_name, instruments, action_type, applied_actions, reasoning, was_fallback, error`); assistant turn passes `training.dpo_pipeline.validate_conductor_schema` (test_llm_capture T12 precedent). |
| T5 | `test_chunked_sessions_released_between_chunks` (**acceptance, pool**) | connection release | counting wrapper around `DatabaseManager.session` (contextmanager delegating to the original, +1 on enter / −1 on exit); drive the export generator to exhaustion → max concurrent open sessions == 1, count returns to 0 between chunks (sampled per yielded row), total sessions == ceil(N/page); after exhaustion `db.engine.pool.checkedout() == 0` (QueuePool on file SQLite in SA 2.0; guarded `hasattr`). |
| T6 | `test_stats_computed_in_sql_not_python_iteration` (**acceptance**) | REL-13c | known fixture (3 rows, 2 bpms, fallback, empty+None reasoning edge, 2 keys, JSON instruments); engine `before_cursor_execute` listener captures every `llm_interactions` SELECT during the stats call → **none** is a bare unbounded full-entity SELECT (each carries `GROUP BY`, an aggregate, or `LIMIT`); response values equal the hand-computed expectation (incl. `avg_bpm` rounding, `fallback_rate`, `avg_reasoning_length` ignoring empty-string reasoning). |
| T7 | `test_timeline_computed_without_full_table_hydration` (**acceptance**) | REL-13c | fixture spanning 2+ segments (30 s window) incl. a `relative_time_ms=None` row (coalesce edge) and a post-reset duplicate `loop_index`; same SQL-spy discipline; segments match hand-computed counts/avg/`interaction_ids`/`key_changes`/`reasoning_snippets` (200-char truncation pinned); `total_interactions` correct. |
| T8 | `test_export_full_streams_complete_document` | REL-13b | show + N>page actions + M>page interactions; GET `/api/shows/{id}/export/full` → 200 `application/json`; body parses to `{"show", "actions", "llm_interactions"}` with exact counts and per-row key sets == `to_dict()` shapes. |
| T9 | `test_jobs_limit_clamped` | REL-30 | `/api/jobs?limit=999999999` → 422; `limit=0` → 422; `limit=-1` → 422; `limit=500` → 200; default 50 → 200. |
| T10 | `test_shows_limit_clamped` | REL-30 | `/api/shows?limit=501` → 422; `limit=500` → 200. |
| T11 | `test_show_actions_limit_clamped` | REL-30 | `limit=5001` → 422; `limit=5000` → 200; `limit=0` → 422; default 1000 → 200 (returns N≤1000 rows). |
| T12 | `test_show_llm_interactions_limit_clamped` | REL-30 | same as T11 on `/shows/{id}/llm-interactions`. |
| T13 | `test_midstream_chunk_failure_aborts_loudly` | decision 8 | drive generator directly; after chunk 1, swap `db_manager.session` for one that raises on entry → `pytest.raises` on the next `next()`; lines already yielded are complete JSON. |

### 3.2 `tests/test_reasoning_logs.py` — rewrite the DB-mocked classes (the gap that hid REL-13a)
- **Keep unchanged:** `test_search_requires_auth`, `TestReasoningLogsSearch` (search route is untouched; its mocks stay valid), the export/timeline/stats 401 tests.
- **Rewrite onto the real-session fixture:** `TestReasoningLogsExport::test_export_returns_jsonl_stream`, `TestReasoningTimeline::test_timeline_empty_show`/`test_timeline_segments`, `TestReasoningStats::test_stats_empty_show`/`test_stats_aggregates` — the old MagicMock query chains cannot express the chunked path (and are exactly why DetachedInstanceError was invisible). The `_make_mock_interaction` helper is deleted with them. Net: file shrinks; assertions preserved (content-type, empty-show shapes, segment/count/avg expectations) but now against real rows.

### 3.3 Existing-behavior pins that stay green unchanged (verify, no edit)
- `test_llm_capture.py` T11/T12 (shaper delegates + real-sqlite round-trip — the shapers are untouched), the whole DPO suite (`test_dpo_pipeline.py` — row format unchanged), `test_shows_api.py` (no export-endpoint tests; Basic-gate 401s unaffected), `test_shows_model.py` (model-level), `test_db.py` (engine untouched), `test_db_offloop.py` (middleware untouched), `test_storage_retention.py` (archive path untouched), `test_api.py` (SQLite fallback end-to-end), `test_frozen_api.py` (route paths/verbs unchanged).
- Sweep set to run explicitly after implementation: `test_exports_pagination`, `test_reasoning_logs`, `test_llm_capture`, `test_shows_api`, `test_jobs_injection`, `test_jobs_await_injection`, `test_api`, `test_dpo_pipeline`, `test_storage_retention`, `test_db_offloop`.

### 3.4 TDD order
1. Write `tests/test_exports_pagination.py` T1–T13 + the §3.2 rewrites → run → **red** (T1/T2 raise `DetachedInstanceError`; T5/T6/T7 fail against the `.all()` paths; T8 loads everything but the SQL-shape/limit assertions of the suite pin the rest; T9–T12 get 200, not 422).
2. Implement §2.1 (`export_chunks.py`) → T5 direct-generator parts green.
3. Implement §2.2b (reasoning export) → T1/T3/T4 green; §3.2 rewrites green.
4. Implement §2.3b (llm-dump) → T2 green; §2.3c (full) → T8 green.
5. Implement §2.2c/d (stats/timeline SQL) → T6/T7 green.
6. Implement §2.3d + §2.4 (clamps) → T9–T12 green.
7. T13 (failure semantics) green with the generator contract.
8. Full gate: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q` → **1116 + ~13 new passed / 16 skipped** (net: +~13 new tests, −5 rewritten), zero regressions.
9. Docs stage (§2.6) → reviewer loop.

---

## 4. Invariant compliance (plan §Invariants)

1. **Lock discipline:** no `state.lock`/`sync_lock` section is added or touched; export routes read no framework state.
2. **Hexagonal + style:** the chunk primitive is a pure infra adapter in `app/lib/` consumed by thin route callers; shapers stay the single source of row shape; functions 4–20 lines; `export_chunks.py` ~85 L; `reasoning_logs.py` stays ~340 L; `shows.py` grows ~14 L (pre-existing >500 brownfield debt, disclosed, not compounded — the export bodies it gains are smaller than the `.all()` blocks they replace); no `Any`; named test fakes/builders.
3. **Audio path:** nothing near the mixer/audio thread.
4. **LLM capture (product-critical):** no schema, shaper, flush, buffer, or retention change; exports remain complete (no response limit), row-for-row identical, round-trip pinned by T4; mid-stream failures abort loudly (never silent truncation); default keep-forever retention untouched.
5. **Worker/restart semantics:** zero worker changes.
6. **Regression tests:** T1/T2 (real-session export), T5 (memory/connection bound), T9–T12 (clamps) map 1:1 to the U11 acceptance bullets; T3/T4/T6/T7/T8/T13 pin the surrounding contract.

---

## 5. Acceptance checklist (maps to §U11 spec + task)

- [ ] Real-session export (no DB mocks) yields complete NDJSON for N > page-size rows, no DetachedInstanceError, no truncation — T1/T2 (red→green on the actual bug).
- [ ] Concurrent show rows not mixed — T3.
- [ ] Round-trip: exported fields == captured fields (invariant 4) — T4.
- [ ] Chunked sessions released; one export cannot exhaust the pool — T5.
- [ ] Stats/timeline computed via SQL; no `.all()` of a full table (SQL spy) — T6/T7.
- [ ] `export/full` streams its document without materializing the corpus — T8.
- [ ] Mid-stream DB failure aborts loudly with complete lines so far — T13.
- [ ] Limit clamps enforced on jobs + shows (mirror `Query(50, ge=1, le=500)`; 1000-default endpoints `le=5000`) — T9–T12.
- [ ] Full gate green, 16 skips unchanged, ruff clean.

---

## 6. Risks / out of scope / residuals

- **Timeline detail scan is O(N) in the response** (interaction_ids/snippets per segment are the API contract) — server memory is chunk-bounded and the fat columns never load, but the response itself grows with show length; a leaner timeline contract is a product decision, out of scope.
- **`(relative_time_ms, id)` keyset is not fully index-covered** (per-chunk sort over the show's rows) — fine at 75 k-row scale with the 10 s budget; optional `(show_id, relative_time_ms)` index documented as a follow-up.
- **422 on out-of-range `limit` is a client-visible contract change** (was: silently honored) — intended REL-30 semantics, identical to the existing reasoning-logs search endpoint; flagged for the docs stage.
- **Tie order within one `loop_index` becomes deterministic** (`id` tiebreak) — was arbitrary; strengthens, never reorders, loop-level order.
- **`test_reasoning_logs.py` mock rewrites:** five tests move to the real-session fixture — the brittle MagicMock chains are deleted rather than extended (they are the reason REL-13a was invisible); search-route mocks remain.
- **`sum(case(...))`/`nullif` portability:** verified semantics on SQLite; PG identical (`length`/`nullif`/`case` are ANSI). Integer division for the segment key matches Python `//` for non-negative values — `relative_time_ms` is non-negative by construction (coalesced 0).
- **Sync generator iteration thread:** each `next()` may run on a different anyio worker thread — safe because every chunk owns its session (SQLite paths already carry `check_same_thread=False`; PG QueuePool connections are checked out per chunk).
- **Out of scope:** async-ORM migration of cold routes (rel-09 follow-up), retention/archive chunking (asyncpg, REL-16), leaner timeline contract, soak module (U15 reuses this fixture).

## 7. Validation commands

```bash
.venv/bin/python -m ruff check app tests
.venv/bin/python -m pytest tests/test_exports_pagination.py tests/test_reasoning_logs.py -q   # new + rewritten
.venv/bin/python -m pytest tests/ -q                                                          # full gate (SQLite fallback)
```
