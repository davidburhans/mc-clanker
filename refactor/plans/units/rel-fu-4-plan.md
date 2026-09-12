# PLAN — Unit FU-4 `rel-fu-exports`, branch `rel-fu-4-exports`
**Spec:** `refactor/plans/rel-remediation-plan.md` §Follow-ups round 2 (FU-4 row) · follow-up note "rel-11 (from rel-13 review, report-only): stats/timeline chunk scans still run on the event loop … follow-up: asyncio.to_thread wrap or an (show_id, relative_time_ms) index. reasoning_logs.py at 497/500 lines — split timeline/stats helpers on next touch. export_chunks.py params want TYPE_CHECKING hints" · follow-up note "rel-10 (review P2/P3 residuals): acquire_client rollback after supervisor.spawn leaves orphaned proc on a retired singleton (narrow)" · `refactor/plans/units/rel-13-plan.md` §6 residuals ("(relative_time_ms, id) keyset is not fully index-covered … optional (show_id, relative_time_ms) index documented as a follow-up") · soak harness doc `docs/soak_harness.md` P6 row.
**Items:** (1) stats/timeline DB compute off the event loop (`asyncio.to_thread`); (2) `(show_id, relative_time_ms)` index on `llm_interactions`; (3) `reasoning_logs.py` split under 500 LOC; (4) `export_chunks.py` TYPE_CHECKING param hints; (5) fanout `acquire_client` rollback orphan kill; (6) soak P6 live PCM feed (make `dropped_pcm_blocks == 0` non-vacuous).
**Baseline gate (verified green at `bf70b7a`, HEAD of `main`):** `.venv/bin/python -m ruff check app tests` clean · `.venv/bin/python -m pytest tests/ -q` = **1203 passed / 26 skipped** (~32 s) · `SOAK=1 .venv/bin/python -m pytest -m soak -q` = 9p/1s (~3 s). Do not regress skips without cause.

Verified against code at HEAD (line refs current):
`app/routes/reasoning_logs.py` **497 L** (timeline helpers `_timeline_segment_key` :196 … `_assemble_timeline_segments` :310-321; timeline route :323/:329-356 — note its `with db_manager.session()` block holds the aggregate session OPEN across `_timeline_detail_rows`' per-chunk sessions = nested sessions, ≤2 connections; stats helpers `_stats_core_totals` :358 … `_stats_response` :431-449; stats route :451/:460-490; `_format_time` :492-497) · `app/lib/export_chunks.py` 84 L — **zero annotations** on `chunked_shaped_rows` (7 params) and `ndjson_lines` · `app/models/llm_interaction.py` :119 `__table_args__ = (Index("ix_llm_interactions_show_loop", "show_id", "loop_index"),)` — the sibling index precedent · migrations: `001`/`002` use plain `CREATE INDEX IF NOT EXISTS idx_…` (idempotent, no DO-block); `003`/`004` add columns for existing PG deployments with the "create_all does not alter existing tables" note · `app/db.py` :42-55 PG branch QueuePool(10+20); :61/:66 SQLite branches both `check_same_thread=False` (worker-thread sessions safe on every dialect) · `app/stream_fanout.py` 526 L: `acquire_client` :169-190 (the rollback path), `_start` :234-252 (`supervisor.spawn()` :246 → `add_audio_client` :247 → `_active = True` :248 → `_start_threads` :251 — anything raising at :247/:251 strands a live spawned proc), `_teardown` :261-282 (the single idempotent stop path), `_retire` :308-312 (clears `state.stream_fanout` ONLY — no proc kill, no queue unregister, no thread join), `acquire_stream_client` :480-501 (generic except → `fanout._retire()` + return None = the orphan window) · `app/stream_fanout_proc.py` `terminate()` :117-131 early-returns on `_proc=None`; `_shutdown_proc` closes stdin → wait → kill escalation · `tests/test_stream_fanout.py` :747-757 T22 spawn-failure test (rollback path today); `make_cfg`/:267, `make_fanout`/:282, `wait_until`/:286, `fanout_threads_alive`/:304, `wait_for_teardown`/:317 · `tests/test_soak_stream.py` — `_push_until_first_chunk` :121-131 fabricates MP3 chunks straight into `FakeStdout` (the PCM queue NEVER receives a block → the `dropped_pcm_blocks == 0` assert at :309 is vacuous) · `tests/test_db_offloop.py` — the off-loop acceptance precedent: `SLOW_DB_SLEEP_SECONDS=0.5` / `RESPONSIVE_BUDGET_SECONDS=0.25` / `HEARTBEAT_INTERVAL_SECONDS=0.01` (:36-39), `_heartbeat`/:185, `_measure_with_heartbeat`/:192 · `tests/conftest.py` `isolated_export_db` fixture :146 + `_ExportSandbox.make_show`/`insert_interactions` · `tests/test_exports_pagination.py` `_shrink_export_page` (:64 monkeypatches `export_chunks.EXPORT_CHUNK_ROWS`), `_CapturedSelects` engine spy, `install_session_counter` (engine/class-level — thread-agnostic) · `app/framework/framework_state.py` `broadcast_audio` :505-530 (snapshot `audio_clients` under `sync_lock`, `put_nowait` outside — the production PCM feed shape the soak harness must mirror) · `FanoutConfig` (`app/stream_fanout_args.py` :18-32: `pcm_queue_blocks: 256  # ~12 s of mixer blocks (~8 KiB each)` → real-time block cadence ≈ 47 ms) · `app/routes/utils.py` `fetch_owned_show` :31-56 (sequenced auth; returns an expunged `Show`) · ruff `select = ["E","F","I","UP006","UP007","UP045"]`, `target-version = py310`, line-length 120; `asyncio_mode = "auto"`.

---

## 0. Scope summary

| Item | Root cause today | Fix site |
|---|---|---|
| stats/timeline on the loop (rel-11-from-rel-13 note) | `get_reasoning_stats`/`get_reasoning_timeline` are `async def` doing every DB call inline: `fetch_owned_show` (2 sessions) + aggregates + the ~1-session-per-chunk detail/instruments scans (~150 serial SELECTs for a 75 k-row show) — each blocks the loop tens of ms; the timeline route additionally holds its aggregate session open across the chunk scans (nested sessions, ≤2 pool connections) | both routes: `await asyncio.to_thread(...)` for auth + a new pure compute entry point; each phase gets its own short session **inside the worker thread** |
| `(show_id, relative_time_ms)` index (rel-13 §6 residual) | the timeline detail scan keysets on `(relative_time_ms, id)` under the `show_id` filter — no index covers it, so every chunk re-sorts the show's rows | `app/models/llm_interaction.py` `__table_args__` + `migrations/005_llm_timeline_index.sql` |
| reasoning_logs.py 497/500 | rel-13 added the timeline/stats helpers onto a file already at the LOC ceiling | pure move of the timeline+stats compute → new `app/lib/reasoning_stats.py` (FU-2/FU-3 split pattern) |
| export_chunks params untyped | `chunked_shaped_rows(db_manager, build_query, order_cols, cursor_cols, shaper, row_key, page_size=None)` — zero annotations on the unit's most-reused adapter seam | `from __future__ import annotations` + TYPE_CHECKING imports + TypeVars; zero behavior change |
| acquire rollback orphan (rel-10 review residual) | `acquire_client`'s except branch discards the reservation but never funnels into teardown; if `_start` raised AFTER `supervisor.spawn()`, `acquire_stream_client` then calls only `_retire()` — clearing `state.stream_fanout` while the spawned ffmpeg stays alive + kill-listed + the PCM queue stays registered (immortal orphan nobody references) | `acquire_client` except branch: `self._teardown()` between discard and re-raise |
| soak P6 vacuous zero-drop | the harness never feeds the PCM queue (MP3 chunks are fabricated into `FakeStdout`), so `dropped_pcm_blocks == 0` asserts nothing | per-cycle PCM feed task mirroring `broadcast_audio` + a "PCM reached the transcoder" assertion |

**Cold (documented, NOT touched):** `search_reasoning_logs` + `_require_show_owner`/`_column`/`_instrument_containment`/`_eq_filter`/`_apply_export_filters` (stays in reasoning_logs.py; clamped + bounded since rel-13); the export routes' sync-generator/threadpool iteration (already off-loop per rel-13 decision 1); `app/routes/shows.py` (untouched in FU-4); the timeline/stats **response payloads** (byte-identical — pure move); `stream_fanout.py`'s other residuals (late-spawn race, teardown-snapshot window — still report-only). No mixer, worker, capture-schema, shaper, flush, or retention code is touched (invariants 1, 3, 4, 5 unaffected).

---

## 1. Design decisions (documented reasoning; deviations from spec letter flagged)

### 1.1 Off-loop mechanics — two `to_thread` hops, fresh session per phase, nothing session-bound crosses the thread

1. **Route shape.** Both routes keep FastAPI param parsing + `DatabaseManager.get_instance()` on the loop, then:
   ```python
   db_manager = DatabaseManager.get_instance()
   # FU-4 (rel-11 residual): every DB statement below runs on a worker thread —
   # auth (2 sessions) + aggregates + the per-chunk scans used to run inline and
   # stall the loop for the duration of ~150 serial SELECTs on a 75 k-row show.
   await asyncio.to_thread(fetch_owned_show, db_manager, show_id, request)
   return await asyncio.to_thread(compute_timeline_payload, db_manager, show_id, segment_seconds)
   ```
   (`compute_stats_payload` for stats.) Two hops, not one: keeping auth out of the compute function leaves the compute entry points `Request`-free — pure `(DatabaseManager, show_id, …) -> dict`, callable from the soak harness or future jobs without HTTP.
2. **Why auth moves too.** The acceptance letter is "stats/timeline do no DB work on the event loop" — `fetch_owned_show` opens 2 sessions per request; leaving it inline fails O1/O2 below and keeps a per-request stall. Precedent: rel-09 moved `fetch_bearer_user(request)` off-loop the same way (`app/middleware_db.py`), and `shows.py:598` runs ownership + DB inside one `to_thread`.
3. **Session discipline inside the thread — the load-bearing rule.** Every phase opens its OWN short `with db_manager.session()` **inside the worker thread**: count → aggregates → per-chunk detail scans (timeline); totals → action counts → keys → instruments chunk scan (stats). Nothing session-bound crosses a phase boundary or the thread boundary: aggregates are column-projected `Row` tuples (plain values, no ORM identity), detail rows are column-projected `Row`s, the payload is plain dicts. This is the rel-13 decision-1 discipline applied one level up.
4. **The nested-session shape goes away (flagged improvement, not in the letter).** Today the timeline route holds its count+aggregate session open across `_timeline_detail_rows`' chunk sessions (≤2 pool connections). The compute entry point sequences phases instead — at most ONE connection open at any moment, matching rel-13's pool-budget intent. No behavior change: the SQL per phase is identical.
5. **Thread-safety on both dialects.** PG branch is QueuePool (a worker thread borrows a connection per session — same as Starlette's threadpool already does for the export generators); SQLite branches are created with `check_same_thread=False` (`db.py:61/:66`). `HTTPException` raised inside `to_thread` propagates unchanged through the `await` — the 401/404 contract is preserved.
6. **Payloads byte-identical.** The compute functions move the route bodies verbatim (same SQL, same `_stats_response`/`_assemble_timeline_segments` shaping, same empty-show early returns). Only WHERE they run changes.

### 1.2 The split — `app/lib/reasoning_stats.py`, pure move, FU-2/FU-3 discipline

**Module:** `app/lib/reasoning_stats.py` (`app/lib/` is the established shared-utility home per rel-13 decision 10; the module imports only SQLAlchemy + `app.lib.export_chunks` — no FastAPI — so it stays importable by routes, cleanup, and tests). Name is grep-unique (0 hits today).

**Moved symbols** (byte-identical except the two route-body extractions noted):

| Symbol (current line) | Note |
|---|---|
| `_timeline_segment_key` :196 · `_timeline_segment_aggregates` :206 · `_timeline_detail_rows` :225 · `_timeline_instruments` :256 · `_timeline_key_changes` :265 · `_timeline_reasoning_snippets` :274 · `_timeline_segment_dict` :288 · `_assemble_timeline_segments` :310 | timeline compute, verbatim |
| `_stats_core_totals` :358 · `_stats_action_counts` :380 · `_stats_keys_used` :392 · `_stats_instruments_used` :403 · `_stats_response` :431 | stats compute, verbatim |
| `_format_time` :492 | used only by `_timeline_segment_dict` |

**NEW public entry points** (the `to_thread` bodies, fully typed, 4–20 lines each):
- `compute_timeline_payload(db_manager: DatabaseManager, show_id: int, segment_seconds: int) -> dict` — count phase (early empty return `{"segments": [], "total_interactions": 0}`), aggregates phase, detail chunk scan, assembly, final payload dict.
- `compute_stats_payload(db_manager: DatabaseManager, show_id: int) -> dict` — totals phase (early empty-return payload identical to today's), action counts, keys, instruments scan, `_stats_response`.

**Stays in `app/routes/reasoning_logs.py`:** module docstring (updated), router, search route + `_require_show_owner`/`_column`/`_instrument_containment`/`_eq_filter`/`_apply_export_filters`, `_build_export_query`, export route, the two thin to_thread route handlers. Adds `import asyncio` + the `compute_*` import.

**LOC math:** 497 − 224 (moved blocks :196-321, :358-449, :492-497) − ~38 (route bodies shrink :329-356/:460-490 → ~10 lines each) + ~5 (imports/docstring) ≈ **~240**. New module ≈ 224 + ~14 (docstring) + ~10 (imports) + ~30 (two compute entry points) ≈ **~280**. Both < 500, pinned by S1.

**Patch-point inventory — every seam the suite touches, and why it survives:**

| Seam (exact test syntax) | Used by | Reader lives in (after split) | Survives because |
|---|---|---|---|
| `patch("app.routes.reasoning_logs.get_current_user_from_request", …)` | `test_reasoning_logs.py` search tests ×4 | search route + `_require_show_owner`, unchanged file | readers stay in reasoning_logs.py |
| `patch("app.routes.reasoning_logs._require_show_owner", …)` | same ×4 | unchanged file | unchanged |
| `monkeypatch.setattr(export_chunks, "EXPORT_CHUNK_ROWS", n)` (`_shrink_export_page`) | `test_exports_pagination.py` T5/T7 | `app/lib/export_chunks.py` module constant, read at call time by `chunked_shaped_rows` | the moved `_timeline_detail_rows`/`_stats_instruments_used` call `chunked_shaped_rows` unchanged — the patched default still applies |
| `_CapturedSelects` engine spy + `install_session_counter` | `test_exports_pagination.py` T5/T6/T7 | engine/class-level instrumentation — thread-agnostic | `to_thread` sessions hit the same engine/sessionmaker; phases are sequential → max_open stays 1 |
| route paths/verbs | `test_frozen_api.py` | routes unchanged | unchanged |
| real-session export/timeline/stats assertions | `test_reasoning_logs.py`, `test_exports_pagination.py`, soak P8 | routes still return identical payloads | pure move (T6/T7's SQL-shape asserts also still hold — same statements, new thread) |

Grep-verified NOT patched anywhere: `_timeline_*`, `_stats_*`, `_format_time` — safe to relocate with their readers.

### 1.3 The index — model-declared, migration-deployed, one name on both creation paths

1. **DDL:** `Index("ix_llm_interactions_show_rel_time", "show_id", "relative_time_ms")` added to `LLMInteraction.__table_args__` beside the existing `ix_llm_interactions_show_loop`. Fresh installs (and every test schema) get it from `Base.metadata.create_all()`.
2. **Migration `migrations/005_llm_timeline_index.sql`:** plain `CREATE INDEX IF NOT EXISTS ix_llm_interactions_show_rel_time ON llm_interactions (show_id, relative_time_ms);` — the `001`/`002` idempotent precedent (PG supports `IF NOT EXISTS`; no DO-block needed for an index), with the `003`/`004` deployment note (existing PG deployments apply the file — `create_all` does not alter existing tables; SQLite dev DBs: recreate or live without it — the index is performance-only, no query depends on it for correctness) + a DOWN section (`DROP INDEX IF EXISTS …`).
3. **Naming:** `ix_` prefix matches the sibling model-declared `ix_llm_interactions_show_loop`; migrations 001/002's `idx_generator_jobs_*` names live on a different table with no model-declared counterpart. The migration MUST use the exact model name so the two creation paths converge (`IF NOT EXISTS` dedupes whichever ran first).
4. **What it covers:** the timeline detail scan (`filter(show_id).order_by(relative_time_ms, id)` keyset) — the exact rel-13 §6 residual ("not fully index-covered … optional follow-up"). Any other show-scoped read on `llm_interactions` gets the `show_id` prefix too. **Residual disclosed:** the `id` tiebreaker inside equal `(show_id, relative_time_ms)` groups still sorts — those groups are bounded by rows sharing the exact same `relative_time_ms` (rare; capture writes one row per loop). Stats paths keyset on `id` alone — index unused there, no harm.
5. **No other query plans change:** no query rewrites in FU-4; the index is additive.

### 1.4 `export_chunks.py` TYPE_CHECKING hints — annotated, no `Any`, runtime-import-free

```python
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from sqlalchemy import Column
    from sqlalchemy.orm import Query, Session

    from app.db import DatabaseManager

RowT = TypeVar("RowT")      # raw row handed to the shaper (ORM entity or projected Row)
ShapedT = TypeVar("ShapedT")  # shaper output (dict, list, or the Row itself)

def chunked_shaped_rows(
    db_manager: DatabaseManager,
    build_query: Callable[[Session], Query],
    order_cols: Sequence[Column],
    cursor_cols: Sequence[Column],
    shaper: Callable[[RowT], ShapedT],
    row_key: Callable[[RowT], tuple],
    page_size: int | None = None,
) -> Iterator[ShapedT]: ...

def ndjson_lines(row_dicts: Iterable[dict]) -> Iterator[str]: ...
```

- No `Any` (AGENTS rule) — the two shapes that genuinely vary are TypeVars.
- `from __future__ import annotations` is REQUIRED (py3.10 target + TYPE_CHECKING-only names in signatures); the module gains zero runtime imports (`app.db` stays un-imported — cycle-free by construction).
- Bare `Column`/`Query` (not parameterized) — precise enough for the seam without SQLAlchemy generics gymnastics.
- Zero behavior change; docstrings untouched.

### 1.5 `acquire_client` rollback — funnel into `_teardown`, not just discard

**What `_retire` does vs what the orphan path skips today:**

| Effect | `_retire()` (today's only rollback companion) | `_teardown()` |
|---|---|---|
| clears `state.stream_fanout` (identity-guarded) | ✔ | ✔ (via `_retire`) |
| kills + reaps the spawned proc, unregisters kill list | ✘ | ✔ (`supervisor.terminate()`) |
| unregisters the PCM queue from `state.audio_clients` | ✘ | ✔ |
| sets stop_event, wakes feeder, poisons sessions | ✘ | ✔ |
| joins feeder/pump threads | ✘ | ✔ |

**The orphan window:** `_start` (:234-252) raises only after `supervisor.spawn()` (:246) if `add_audio_client` (:247) or `_start_threads` (:251) raise (e.g. `Thread.start()` RuntimeError under thread exhaustion). The current rollback discards the session and re-raises; `acquire_stream_client`'s generic `except Exception` then calls ONLY `fanout._retire()` → the singleton slot is cleared while the ffmpeg stays alive + kill-listed and the PCM queue stays registered — an immortal orphan nobody references (narrow, exactly the rel-10 review's wording).

**Fix** — one line of behavior in `acquire_client`:
```python
        if need_start:
            try:
                self._start()
            except Exception:
                with self._clients_lock:
                    self._discard_client_locked(session)  # roll the reservation back
                # FU-4 (rel-10 review residual): _start can raise AFTER
                # supervisor.spawn() — discarding the reservation alone strands
                # a live transcoder on a singleton the factory then _retire()s.
                # Funnel through the single idempotent stop path: proc killed
                # and reaped, kill-list entry dropped, PCM queue unregistered,
                # threads joined, singleton retired.
                self._teardown()
                raise
            self._teardown_if_start_orphaned()
```

**Race/idempotence audit (all paths verified against the code):**
- `_start` raised `FanoutInactive` (teardown already running, `_stopping` set): `_teardown()` early-returns at its first guard — no double teardown.
- Concurrent second acquirer parked on `_start_lock`: sees `_stopping` → raises `FanoutInactive` → its own rollback discards + `_teardown()` early-returns → `acquire_stream_client`'s `FanoutInactive` branch retries a fresh singleton. No resurrection (single-use objects, decision 2).
- Never-started fanout (spawn itself raised — today's T22 path): `_teardown()` degrades gracefully — `terminate()` returns on `_proc=None`, `remove_audio_client` guards membership, `_join_threads` skips `None`, `_wake_feeder` swallows `queue.Full`. T22 stays green.
- Lock discipline: the `_clients_lock` block closes BEFORE `_teardown()` — no I/O under the lock (teardown joins with timeouts).
- `acquire_stream_client`'s post-failure `_retire()` stays: idempotent (identity guard), now a no-op.

### 1.6 Soak P6 live PCM feed — mirror `broadcast_audio`, prove flow, keep the zero-drop honest

1. **Today's vacuity:** `_push_until_first_chunk` fabricates MP3 chunks directly into `FakeStdout`; the fanout's PCM queue never receives a block, so the feeder's `write_pcm` never runs and `dropped_pcm_blocks == 0` (:309) asserts nothing about churn.
2. **The feed:** a per-cycle asyncio task mirroring production `broadcast_audio` (`framework_state.py:505-530`): each beat snapshots `list(state.audio_clients)` and `put_nowait`s one silence block into every queue, swallowing `queue.Full` exactly like the production path. Block = `b"\x00" * 8192` (the ~8 KiB mixer block per the `FanoutConfig.pcm_queue_blocks` comment); cadence = `PCM_BLOCK_INTERVAL_S = 0.04` (256 blocks ≈ 12 s of real-time buffer per the config comment ⇒ ~47 ms real cadence; 40 ms keeps the queue mildly ahead without overflowing — drop-oldest makes over-push harmless anyway). Named constants at module top, not magic numbers.
3. **Wiring:** `_drive_stream_client_once` and `_drive_concurrent_trio` start the feed once the client(s) have their first chunk (singleton live), and cancel+await it (suppressing `CancelledError`) after the kill. Bounded total blocks per cycle as a belt-and-braces stop condition.
4. **New assertions (per cycle / trio):**
   - `fake_popen[proc_index].stdin.buffer` non-empty — PCM actually flowed queue → feeder thread → transcoder stdin. This makes the zero-drop non-vacuous in BOTH directions (a feed that never flows can't prove the feeder kept up).
   - the existing `dropped_pcm_blocks == 0` now means: the feeder drained every pushed block through a live pipe under client churn.
5. **Why `dropped_pcm_blocks` cannot false-increment in this harness:** the counter increments only inside `write_pcm` on a dead proc/pipe or a write failure; P6 kills clients, never the transcoder (`FakeProc` stays alive until teardown), and after `_kill_client` sets `is_running=False` the feeder exits at its next loop check (≤ `queue_poll_s`) before draining further — pushed-but-undrained blocks simply orphan in the queue, incrementing nothing.
6. **Scope note (disclosed):** the stdout side stays fabricated — a fake ffmpeg that actually transcodes is out of scope (the argv contract is pinned by `test_stream_fanout_args`). "Real PCM flowing" = real-cadence PCM through the registered queue → feeder thread → transcoder stdin.
7. **Red-by-construction:** the `stdin.buffer` assertion fails against the pre-FU-4 harness (nothing fed PCM, buffer empty); during implementation, run P6 once with the feed disabled to see the red, then enable it (documented in the TDD order).

---

## 2. Exact changes per file

### 2.1 NEW `app/lib/reasoning_stats.py` (~280 lines)
- Module docstring: FU-4 provenance (rel-11-from-rel-13 residual: stats/timeline scans off the loop + the reasoning_logs.py 497/500 split), the session-per-phase rule, "no FastAPI imports — importable by routes/cleanup/tests".
- `from __future__ import annotations`; runtime imports: `sqlalchemy.case`, `sqlalchemy.func`, `from app.lib.export_chunks import chunked_shaped_rows`; TYPE_CHECKING: `from app.db import DatabaseManager`.
- The 14 helpers moved verbatim (§1.2 table).
- `compute_timeline_payload` / `compute_stats_payload`: phase-sequenced short sessions, route-body semantics verbatim (incl. both early empty returns and the final payload dicts).

### 2.2 `app/routes/reasoning_logs.py` (497 → ~240)
- Docstring: note the split + off-loop compute.
- Imports: +`asyncio`; +`from app.lib.reasoning_stats import compute_stats_payload, compute_timeline_payload`; − `case, func` (no remaining user — ruff will confirm).
- Delete the moved blocks (:196-321 timeline helpers, :358-449 stats helpers, :492-497 `_format_time`).
- `get_reasoning_timeline` / `get_reasoning_stats` bodies → the §1.1.1 to_thread shape.

### 2.3 `app/models/llm_interaction.py`
```python
    __table_args__ = (
        Index("ix_llm_interactions_show_loop", "show_id", "loop_index"),
        # FU-4 (rel-13 §6 residual): covers the timeline detail scan's
        # (show_id -> relative_time_ms) filter+order — each chunk previously
        # re-sorted the whole show's rows. Deployed PG: migrations/005.
        Index("ix_llm_interactions_show_rel_time", "show_id", "relative_time_ms"),
    )
```

### 2.4 NEW `migrations/005_llm_timeline_index.sql` (~30 lines)
- Header: FU-4/rel-13 provenance, idempotent, DOWN section, apply command (the 003/004 pattern); note that `create_all` covers fresh installs and SQLite dev DBs may recreate (index is performance-only).
- `CREATE INDEX IF NOT EXISTS ix_llm_interactions_show_rel_time ON llm_interactions (show_id, relative_time_ms);`

### 2.5 `app/lib/export_chunks.py` (84 → ~95)
- §1.4 verbatim: future-annotations header, TYPE_CHECKING block, TypeVars, full signatures on both functions. No docstring/body changes.

### 2.6 `app/stream_fanout.py` (526 → ~533)
- `acquire_client` except branch gains `self._teardown()` + the §1.5 WHY comment. Nothing else.

### 2.7 `tests/test_soak_stream.py` (~+45 lines)
- `PCM_SILENCE_BLOCK = b"\x00" * 8192`, `PCM_BLOCK_INTERVAL_S = 0.04`, `PCM_FEED_MAX_BLOCKS = 512` constants; `_feed_pcm_blocks()` async task (broadcast_audio mirror); wiring in `_drive_stream_client_once` + `_drive_concurrent_trio`; the two new assertions (§1.6.4) in the per-cycle loop and after the trio; module docstring notes the FU-4 upgrade.

### 2.8 NEW `tests/test_fu4_exports.py` (~230 lines) — §3

### 2.9 Docs (pipeline "docs" stage, same unit)
- `docs/reliability_audit.md`: REL-13 status paragraph gains the FU-4 note (off-loop stats/timeline + the index landed); REL-10 status gains the acquire-rollback note.
- `docs/soak_harness.md`: P6 row gains "+ live PCM feed (FU-4): real-cadence PCM through the registered queue; dropped_pcm_blocks == 0 is non-vacuous".
- `refactor/plans/rel-remediation-plan.md`: FU-4 status row → landed (commit at merge); annotate the "rel-11 (from rel-13 review)" bullet and the rel-10 residual bullet with "(DONE in FU-4: …)" — the FU-2/FU-3 annotation style.
- `CLAUDE.md`: `app/routes/reasoning_logs.py` row (compute split into `app/lib/reasoning_stats.py`, off-loop via to_thread); API-layer table row for `app/lib/reasoning_stats.py`; lib-table `export_chunks.py` row gains "fully annotated (FU-4)"; test-table rows for `test_fu4_exports.py` + updated `test_soak_stream.py` row; `app/models/llm_interaction.py` notes the second index; `stream_fanout.py` row mentions the rollback teardown.

---

## 3. TDD regression tests (write first; confirm expected red/green, then implement)

### 3.1 NEW `tests/test_fu4_exports.py` (~230 lines) — the unit's acceptance suite
Fixtures: `isolated_export_db` (conftest) for everything DB-backed; a local `_BearerRequest` SimpleNamespace fake (`headers={"Authorization": f"Bearer {token}"}` — `get_current_user_from_request` reads only `.headers.get`); a locally mirrored 12-line `_measure_with_heartbeat` (the `test_db_offloop.py:192` helper — local mirror per the repo's fixture-glue pattern, attribution comment); constants `SLOW_STATEMENT_SECONDS = 0.1`, `RESPONSIVE_BUDGET_SECONDS = 0.05`, `HEARTBEAT_INTERVAL_SECONDS = 0.01` (2× headroom, the rel-09 tuning note). Slow-fake = a `before_cursor_execute` engine listener that `time.sleep`s — works on real sessions regardless of query shape, thread-agnostic. Fanout tests reuse `test_stream_fanout`'s `make_cfg`/`PopenRecorder`/`FakeProc` + the locally repeated `fake_popen`/`fake_ffmpeg_exe` fixtures (test_soak_stream glue pattern).

| # | Test | Pins | Core assertions |
|---|---|---|---|
| O1 | `test_stats_route_does_no_db_work_on_event_loop` | item 1 (**RED**) | sandbox + 3 rows; slow listener on the engine; drive `await get_reasoning_stats(request=_BearerRequest(…), show_id=…)` beside the heartbeat; assert `max_gap < RESPONSIVE_BUDGET_SECONDS` AND `elapsed >= SLOW_STATEMENT_SECONDS` (the sleep really ran — off-loop, not skipped); response payload equals the expected stats (same SQL). Red today: every inline statement stalls the loop ≥ 0.1 s. |
| O2 | `test_timeline_route_does_no_db_work_on_event_loop` | item 1 (**RED**) | `_shrink_export_page(monkeypatch, 2)`-style page shrink + 5 rows (count + aggregates + 3 detail chunks ≈ 7+ slow statements); drive `get_reasoning_timeline` the same way; same gap/elapsed asserts + payload shape. Red today: the chunk-scan loop (the exact rel-11 hazard) stalls the loop per statement. |
| I1 | `test_llm_interactions_show_rel_time_index_exists` | item 2 (**RED**) | `sqlalchemy.inspect(sandbox.db.engine).get_indexes("llm_interactions")` → contains `{"name": "ix_llm_interactions_show_rel_time", "column_names": ["show_id", "relative_time_ms"]}`; sibling `ix_llm_interactions_show_loop` still present. |
| S1 | `test_reasoning_logs_split_under_500_lines` | item 3 (**RED** — module missing) | `app/routes/reasoning_logs.py` AND `app/lib/reasoning_stats.py` exist, each `len(read_text().splitlines()) < 500`; `from app.lib.reasoning_stats import compute_stats_payload, compute_timeline_payload` imports (FU-2/FU-3 S1 pattern). |
| H1 | `test_export_chunks_params_fully_annotated` | item 4 (**RED**) | `inspect.signature` on `chunked_shaped_rows` + `ndjson_lines`: every param annotation and the return annotation non-empty. Docstring documents WHY no `eval_str=True`: PEP-563 string annotations reference TYPE_CHECKING-only names, which are deliberately absent at runtime — the pin is presence; type-correctness is the reviewer's check. |
| R1 | `test_acquire_rollback_kills_orphaned_transcoder` | item 5 (**RED**) | `fanout = get_stream_fanout(state, make_cfg())`; `monkeypatch.setattr(fanout, "_start_threads", _raise_thread_exhaustion)` (post-spawn failure); `pytest.raises(RuntimeError)` on `fanout.acquire_client()`; then: `fake_popen[0].poll() is not None` + `stdin.closed` (reaped, not orphaned), `state.active_subprocesses == set()`, `state.audio_clients == []`, `state.stream_fanout is None`, `fanout.status().active is False`, and a second `acquire_client()` on the dead object raises `FanoutInactive` (retired, never resurrected). Red today: proc alive (poll None), kill-list non-empty, queue registered. |

### 3.2 Soak P6 upgrade (in place, `tests/test_soak_stream.py`) — §1.6/§2.7
Validated by `SOAK=1 .venv/bin/python -m pytest -m soak tests/test_soak_stream.py -q` — the assertions run and fail loudly if the feed breaks or the feeder drops blocks. Red-by-construction of the `stdin.buffer` assertion against the pre-feed harness (run once with the feed disabled during implementation to see it).

### 3.3 Keep-green set (must pass UNTOUCHED; run explicitly after each stage)
`tests/test_reasoning_logs.py` (search mocks + real-session rewrites — the split's patch-point proof) · `tests/test_exports_pagination.py` (T1–T13: shapers, page-size monkeypatch, session counter, SQL spy, clamps) · `tests/test_stream_fanout.py` (incl. T22 spawn-failure — the teardown-no-op path — and the T23 hammer) · `tests/test_frozen_api.py` · `tests/test_db_offloop.py` (off-loop precedent untouched) · `tests/test_db.py` · `tests/test_llm_capture.py` · `tests/test_dpo_pipeline.py` · `tests/test_shows_api.py` · `tests/test_shows_model.py` · `tests/test_auth.py` (auth helper untouched).

### 3.4 TDD order
1. Write `tests/test_fu4_exports.py` → run → **O1/O2/I1/S1/H1/R1 red** (documented reds: loop gaps ≥ 0.1 s; index absent; module missing; annotations empty; orphan survives).
2. **Split first, no behavior change** (§2.1 move + §2.2 deletions): full keep-green set green with ZERO test edits → S1 green; O1/O2/I1/H1/R1 still red. (This stage alone proves the patch-point inventory — any failure means the inventory is wrong: stop and fix the split, not the tests.)
3. to_thread edits (§1.1) → O1/O2 green; keep-green set re-run.
4. Index (§2.3 model + §2.4 migration) → I1 green.
5. export_chunks annotations (§2.5) → H1 green.
6. Fanout rollback (§2.6) → R1 green; `test_stream_fanout.py` full file re-run (T22/T23 critical).
7. Soak P6 harness upgrade (§2.7): run once feed-disabled (red proof of the new assertion), then enabled → `SOAK=1 .venv/bin/python -m pytest -m soak -q` = 9p/1s.
8. Full gate: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q` → **1203 + ~6 new passed / 26 skipped**, zero regressions.
9. Docs stage (§2.9) → reviewer loop.

---

## 4. Invariant compliance (plan §Invariants)

| # | How respected |
|---|---|
| 1 — lock discipline | `_teardown` runs with `_clients_lock` released (short lock scopes only, no I/O under lock — audited in §1.5); no `state.lock`/`sync_lock` section added anywhere; the soak feed mirrors `broadcast_audio`'s snapshot-outside-lock shape. |
| 2 — style / hexagonal | Both files < 500 pinned by S1; new code 4–20 lines, WHY docstrings, fully typed; no `Any` (H1 + review); names grep-unique before landing (`reasoning_stats` / `compute_*_payload` / `ix_llm_interactions_show_rel_time` = 0 hits); the compute module is a pure infra adapter (no FastAPI) behind thin route callers — rel-13 decision 10's placement; pure-move split discipline (FU-2/FU-3 precedent). |
| 3 — audio thread | Zero mixer/audio-thread changes. The fanout edit touches only the client-acquire failure path (request thread), never the feeder/pump loops. |
| 4 — LLM capture | Index is additive metadata (no column, shaper, flush, buffer, or retention change); exports untouched — row shapes byte-identical; stats/timeline payloads byte-identical (pure move); default keep-forever untouched. |
| 5 — worker restart semantics | Zero worker changes. |
| 6 — regression per fix | O1/O2 pin item 1; I1 item 2; S1 item 3; H1 item 4; R1 item 5; the P6 upgrade item 6; §3.3 guards every neighboring seam. |

## 5. Acceptance checklist (maps to the FU-4 row)

- [ ] stats/timeline do no DB work on the event loop (slow-fake + heartbeat; auth included) → **O1/O2**
- [ ] `(show_id, relative_time_ms)` index exists (sqlite introspection) + PG migration 005 → **I1** + §2.4
- [ ] `reasoning_logs.py` split: both files < 500, routes still work, existing tests green untouched → **S1** + §3.3
- [ ] `export_chunks.py` params fully annotated (TYPE_CHECKING, no `Any`) → **H1**
- [ ] failed acquire after spawn leaves NO live proc + singleton retired cleanly → **R1** (+ T22 keep-green)
- [ ] soak P6 runs with real PCM flowing; `dropped_pcm_blocks == 0` non-vacuous → §2.7, `SOAK=1` green
- [ ] ruff clean; full gate 1203 + ~6 new passed / 26 skipped; soak 9p/1s
- [ ] audit/soak-harness/CLAUDE/plan docs updated (§2.9)

## 6. Risks / out of scope / residuals

- **Split regression risk** is the unit's main hazard → mitigated by ordering (split lands alone, stage 2, keep-green set required green, zero test edits) and the §1.2 patch-point inventory. Disclosed cosmetic delta: `reasoning_logs.py`'s import surface shrinks (`case`/`func` leave with their readers).
- **Timing-based tests (O1/O2):** 2× headroom budget (rel-09 precedent constants); a pathologically slow CI box tunes ONE constant pair at the top of the file.
- **`stream_fanout.py` grows 526 → ~533** — pre-existing > 500 brownfield debt (the rel-13 `shows.py` treatment: disclosed, not compounded; a split is out of FU-4's letter, flagged for a future hygiene FU).
- **SQLite dev databases won't self-heal the index** (no migration runner for sqlite) — harmless: the index is performance-only; tests and fresh installs get it from `create_all`.
- **P6 feed is a harness upgrade, not a production red→green** — its failure mode is "assertion goes vacuous again", caught by the `stdin.buffer` assertion and the SOAK=1 run.
- **`FakeStdin` never blocks** → `dropped_pcm_blocks == 0` is deterministic under the 40 ms cadence; a real-ffmpeg variant remains out of scope (disclosed in §1.6.6).
- **Out of scope:** rel-10's other residuals (late-spawn race, teardown-snapshot window — still report-only); search/export route auth (`fetch_owned_show` on the loop there — bounded 2 sessions, export chunks already threadpool-driven); `shows.py` LOC debt; async-ORM migration; leaner timeline contract.

## 7. Commit sequence (one branch `rel-fu-4-exports`, Conventional Commits, land on `main` via parent)

1. `test(rel-fu-4): off-loop stats/timeline heartbeat pins, timeline index introspection, reasoning split LOC pin, export_chunks annotation pin, fanout acquire-rollback orphan pin — TDD red`
2. `fix(rel-fu-4): stats/timeline compute off-loop via to_thread (reasoning_stats split); (show_id, relative_time_ms) index; annotated export_chunks; fanout acquire rollback teardown; soak P6 live PCM feed`
3. `docs(rel-fu-4): audit/CLAUDE/soak-harness/plan status`

## 8. Validation commands

```bash
.venv/bin/python -m ruff check app tests
.venv/bin/python -m pytest tests/test_fu4_exports.py tests/test_reasoning_logs.py tests/test_exports_pagination.py tests/test_stream_fanout.py -q
.venv/bin/python -m pytest tests/ -q                                   # full gate
SOAK=1 .venv/bin/python -m pytest -m soak tests/test_soak_stream.py -q # P6 with live PCM
SOAK=1 .venv/bin/python -m pytest -m soak -q                           # whole soak gate
```
