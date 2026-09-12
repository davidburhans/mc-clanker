# PLAN — Unit FU-5 `rel-fu-cosmetics`, branch `rel-fu-5-cosmetics`
**Spec:** `refactor/plans/rel-remediation-plan.md` §Follow-ups round 2 closing note — "(2026-09-12: all four FU units landed; remaining ledger entries below are cosmetic/documentation residuals — typed annotations on moved private helpers, stream_fanout 532-line split, worker.py 498/500 headroom note, soak log-episode note — none behavioral.)" — plus this unit's task letter (db.py libpq-kwargs driver gating; audit-doc completion banner).
**Items:** (1) `app/stream_fanout.py` split under 500 LOC (actual 536, not the ledger's stale 532 — FU-4's rollback fix grew it); (2) full annotations on `app/lib/reasoning_stats.py`'s moved private helpers; (3) `app/db.py`: libpq `connect_args` gated on the PG **driver**, so a `postgresql+asyncpg://` URL builds WITHOUT them; (4) `docs/reliability_audit.md` completion banner (plan-doc pointer + soak command, historical verdict preserved); (5) worker.py 498/500 headroom — **decided: document, do not slice** (§1.5); (6) soak log-episode note in `docs/soak_harness.md` (§1.6).
**Baseline gate (verified green at `acc29c3`, HEAD of `main`):** `.venv/bin/python -m ruff check app tests` clean · `.venv/bin/python -m pytest tests/ -q` = **1209 passed / 26 skipped** (~124 s) · `SOAK=1 .venv/bin/python -m pytest -m soak -q` = **9 passed / 1 skipped** (~3 s). Do not regress skips without cause.

Verified against code at HEAD (line refs current):
`app/stream_fanout.py` **536 L**: sentinel block `:60-63`; `_CLIENT_IDS` `:65` (stays — `acquire_client` consumes it); `_ClientSession` `:80-88`; `_drain_one` `:90-95`; `_drain_queue` `:98-105`; `_residual_blocks` `:107-122`; `import subprocess` `:27` whose ONLY user is the `_warn_if_no_libmp3lame` probe (`:329-343`, `subprocess.run` `:333`, `resolve_ffmpeg_exe()` `:334`); `__all__` `:41-53`; `mp3_client_stream` `:516-536`. **Load-bearing subtlety:** `monkeypatch.setattr("app.stream_fanout.subprocess.Popen", …)` resolves `app.stream_fanout.subprocess` to the GLOBAL `subprocess` module, so that patch fakes `TranscoderSupervisor.spawn`'s Popen too (the supervisor lives in `stream_fanout_proc.py` and imports subprocess itself) — any split that drops `import subprocess` from stream_fanout.py breaks the fanout fakes in THREE test files · `app/stream_fanout_proc.py` `:27` TYPE_CHECKING `from app.stream_fanout import _FanoutCounters` (import stays — `_FanoutCounters` is still imported there) · **patch-target inventory** (complete; grep over `app/` + `tests/`): `tests/test_stream_fanout.py:51-62` imports `_STOP_SENTINEL, FanoutConfig, FanoutInactive, FanoutStatus, StreamFanout, acquire_stream_client, build_mp3_args, get_stream_fanout, mp3_client_stream, resolve_ffmpeg_exe`; `:228` `app.stream_fanout.subprocess.Popen`; `:235` `app.stream_fanout.resolve_ffmpeg_exe`; `:237` `app.stream_fanout.subprocess.run`; `:248` instance `fanout._teardown()`; `tests/test_fu4_exports.py:42` imports `FanoutInactive, get_stream_fanout`; `:316/:323/:325` the same three module-path patches; `:337` instance `_teardown`; instance-attr `fanout._start_threads` patch (R1) — attribute lookup, layout-agnostic; `tests/test_soak_stream.py:71/:78/:80` the same three patches; `:92` instance `_teardown`; `tests/test_round3_fix_d.py:624` `import app.stream_fanout as sf` → touches only `sf.resolve_ffmpeg_exe` (`:636`), `sf.subprocess.run` (`:639`), `sf.subprocess.Popen` (`patch.object`, `:654`); **`tests/test_p3_hygiene.py` references ZERO stream_fanout symbols** (grep clean — the letter's "p3 patch targets" inventory answer is "none") · `app/lib/reasoning_stats.py` **333 L**: TYPE_CHECKING block `:32-34` (`DatabaseManager` only); un-annotated params/returns on every moved private helper: `_timeline_segment_key` `:36`, `_timeline_segment_aggregates` `:46` (`session`), `_timeline_detail_rows` `:65` (`db_manager`), `_timeline_instruments` `:96`, `_timeline_key_changes` `:105`, `_timeline_reasoning_snippets` `:114`, `_timeline_segment_dict` `:128`, `_assemble_timeline_segments` `:150`, `_stats_core_totals` `:163` (`session`), `_stats_action_counts` `:185`, `_stats_keys_used` `:197`, `_stats_instruments_used` `:208` (`db_manager`), `_stats_response` `:236`; the two public `compute_*_payload` entry points are already fully annotated · `app/lib/export_chunks.py` **105 L** already fully annotated + pinned by FU-4 H1 (`tests/test_fu4_exports.py:278` — the A1 template) · `app/db.py` **107 L**: PG branch `:37-58` gated on `make_url(url).get_backend_name() == "postgresql"` — true for ANY driver, so the libpq kwargs (`connect_timeout`, `options` statement_timeout, 4× keepalives `:42-58`, FU-1 constants `:21-27`) would be passed to a `postgresql+asyncpg://` / `+pg8000` URL whose drivers reject them; `make_url("postgresql://…").get_driver_name()` resolves the default to `"psycopg2"` · `tests/test_db.py` `TestEngineResilienceRel09` `:239-…`: T1 `:247-274` pins `postgresql://` full-kwargs (exact connect_args dict assert), T2/T3 pin the SQLite branches; singleton reset pattern = `DatabaseManager._instance = None` around each construction; `patch("app.db.create_engine")` throughout (mocked engine ⇒ the asyncpg-URL test can exercise the branch without SQLAlchemy's sync/async dialect rejection) · `app/worker.py` **498 L** (`test_worker_fu3.py:347` S1 pins `< 500` for worker.py + worker_job_rows.py): module-attr patch points `worker_module.GENERATION_TIMEOUT_SECONDS` (test_worker_fu3 `:190/:220`, test_worker_vram `:285/:307/:321/:350`), `worker_module.encode_aac` / `get_audio_duration` (test_worker_correctness ×10, test_worker_fu3 `:265-266`, test_worker_vram `:324-325`), `worker_module.asyncpg.create_pool` + `create_garage_client_from_env` (test_worker_vram `:388-389`), `worker_module.VRAM_EVICTION_TIMEOUT_SECONDS` (test_worker_vram `:679`); `get_worker_instance` imported by `app/worker_routes.py:24` AND `app/routes/worker_routes.py:9`; `WorkerConfig` imported from `app.worker` in test_worker.py ×6; `python -m app.worker` is the container entry · `docs/reliability_audit.md` **637 L**: no completion banner — opens straight into "**Verdict: NOT 24/7-ready.**" `:14`; 15 `**Status: fixed-in …**` notes exist per-finding; REL-01..15 are `### [Critical]/[High]` headers, REL-16+ live under `## P1/P2/P3` sections (`:183/:508/:524`) · `docs/soak_harness.md` §Notes `:60-79` — no log-reading note · "soak log-episode note": the phrase exists ONLY in the ledger closing note (git `acc29c3` + repo grep) — interpretation decided in §1.6.

---

## 0. Scope summary

| Item | Root cause today | Fix site |
|---|---|---|
| stream_fanout 536/500 | FU-4's acquire-rollback fix (+10) pushed the file past the ceiling; the queue/session plumbing is a self-contained block | pure move → NEW `app/stream_fanout_sessions.py` (§1.1) |
| reasoning_stats un-annotated helpers | FU-4's pure move carried the pre-split route helpers' bare signatures | TYPE_CHECKING `Session`/`Row`/`ColumnElement` + exact annotations (§1.2) |
| libpq kwargs on any PG driver | `db.py`'s branch tests only the backend name; the FU-1 keepalives are libpq connection parameters a non-libpq driver rejects | `_pg_connect_args(url)` helper gated on driver (§1.3) |
| audit doc reads as open | the doc is the historical snapshot but carries no completion pointer | banner under the title (§1.4) |
| worker.py 498/500 | FU-3's split left 2 lines of headroom | **document, don't slice** (§1.5) |
| soak log-episode note | ledger residual, never written down | `docs/soak_harness.md` §Notes bullet (§1.6) |

**Cold (documented, NOT touched):** fanout behavior (the split is a pure move — zero logic edits); `stream_fanout_proc.py`/`stream_fanout_args.py`; the rel-10 report-only residuals (late-spawn race, teardown-snapshot window); `app/worker.py` and `app/worker_job_rows.py` bodies; `export_chunks.py` (already annotated + pinned); asyncpg-level keepalives for `job_waiter`'s raw LISTEN conns (the FU-1 plan's own residual — behavioral timing work, stays open); reasoning_stats SQL/shaping (annotations only — payloads byte-identical).

---

## 1. Design decisions (documented reasoning)

### 1.1 The fanout split — session plumbing out, everything else stays

**Module:** NEW `app/stream_fanout_sessions.py` (~75 L). **Import direction:** `stream_fanout.py` → `stream_fanout_sessions`; the new module imports stdlib only (`queue`, `dataclasses`, `collections.abc.Iterator`) — no reverse import, cycle-free by construction, and no runtime dependency on `GlobalState`.

**Moved symbols** (byte-identical bodies + docstrings, provenance docstring added):

| Symbol (current line) | Lines | Note |
|---|---|---|
| `_STOP_SENTINEL` + its `#:` comment `:60-63` | 4 | the eviction/teardown poison — identity-stable (ONE object, defined once) |
| `_ClientSession` `:80-88` | 9 | identity dataclass (`eq=False`); the pump thread's write target |
| `_drain_one` `:90-95` | 6 | drop-oldest overflow helper |
| `_drain_queue` `:98-105` | 8 | eviction/teardown drain |
| `_residual_blocks` `:107-122` | 16 | teardown keeps already-encoded bytes |

`stream_fanout.py` gains ONE import line — `from app.stream_fanout_sessions import (_ClientSession, _STOP_SENTINEL, _drain_one, _drain_queue, _residual_blocks)` — all five are used in-module, so no F401; `from dataclasses import dataclass` leaves with `_ClientSession` (ruff confirms).

**LOC math:** 536 − 48 (moved) − 1 (dataclass import) + 1 (sessions import) = **~488** (+1 module docstring tweak = 489). Sessions module ≈ 75 (docstring ~14 + imports ~7 + the moved 43 + blanks). Both < 500, pinned by S1. Disclosed: only ~11 lines of headroom remain in `stream_fanout.py` — same shape FU-3 left worker.py in (498); the next-growth seam is named in §6.

**Why this seam (lowest-entanglement), and the alternatives rejected:**
- The moved block is pure functions + a leaf dataclass: no `self`, no locks, no `GlobalState`, no subprocess. The sentinel is the only cross-cutting concern and identity is preserved by the single definition + re-import (`is` comparisons in `_feeder_loop`/`mp3_client_stream`/`_poison_session` keep working).
- **Rejected — delivery/reaper mixin** (`_fanout_block`…`_discard_client_locked`, ~85 L → ~450 total): more headroom but class-internal entanglement (reaches `self._clients_lock`/`self._counters`/`self._teardown`) — the FU-2/FU-3 mixin pattern exists for METHODS that need `self`; this block doesn't.
- **Rejected — moving the `_warn_if_no_libmp3lame` probe** (or `subprocess` with it): breaks `monkeypatch.setattr("app.stream_fanout.subprocess.run", …)` in THREE test files (the attribute `app.stream_fanout.subprocess` must exist for monkeypatch's getattr chain) and would force test edits. The probe therefore STAYS in `stream_fanout.py`, which also keeps `import subprocess` — preserving the load-bearing fact that `"app.stream_fanout.subprocess.Popen"` patches the GLOBAL subprocess module and thereby fakes the supervisor's spawn.
- **Rejected — moving `mp3_client_stream`/factories:** `app_ui.py:21` imports it from `app.stream_fanout` and `test_round3_fix_d`/`test_stream_fanout` do too; a re-export shim would work but moves the file's public face for no headroom gain (those functions are ~65 L but pull `_fanout_lock`/`_retire` coupling with them).

**Patch-point inventory — every seam the suite touches, and why it survives (ZERO test edits, zero patch-target moves):**

| Seam (exact syntax) | Sites | Survives because |
|---|---|---|
| `from app.stream_fanout import _STOP_SENTINEL, …` (10 names) | test_stream_fanout.py:51 | every name remains a module attribute: the five sessions names via the new import, the rest untouched |
| `monkeypatch.setattr("app.stream_fanout.subprocess.Popen", _popen)` | test_stream_fanout.py:228 · test_soak_stream.py:71 · test_fu4_exports.py:316 | `import subprocess` stays (probe remains); the target is the global subprocess module → still fakes `TranscoderSupervisor.spawn` |
| `monkeypatch.setattr("app.stream_fanout.subprocess.run", …)` | test_stream_fanout.py:237 · test_soak_stream.py:80 · test_fu4_exports.py:325 | probe stays in-module, still calls `subprocess.run` via module globals |
| `monkeypatch.setattr("app.stream_fanout.resolve_ffmpeg_exe", …)` | test_stream_fanout.py:235 | name stays; the probe resolves it from `stream_fanout` globals |
| `monkeypatch.setattr(sf, "resolve_ffmpeg_exe"/"subprocess.run"/"subprocess.Popen")` | test_round3_fix_d.py:636/:639/:654 | same — `sf` touches only probe-adjacent attrs + `mp3_client_stream` (stays) |
| `from app.stream_fanout import FanoutInactive, get_stream_fanout` | test_fu4_exports.py:42 | both stay |
| `fanout._teardown()` / `fanout._start_threads` patch / `fanout._clients` read | test_stream_fanout.py:248 · test_fu4_exports.py:337 · test_soak_stream.py:92 | instance-attribute lookups — methods/fields stay on `StreamFanout` |
| TYPE_CHECKING `from app.stream_fanout import _FanoutCounters` | stream_fanout_proc.py:27 | `_FanoutCounters` re-import from `stream_fanout_args` stays |
| `tests/test_p3_hygiene.py` | — | **zero references** (grep-verified) — keep-green only |
| `state.stream_fanout` slot, `/stream.mp3` route | app_ui.py:21/:588 · soak P6 | route seam unchanged |

### 1.2 Annotation exacts — `reasoning_stats.py` private helpers, no `Any`

Add to the TYPE_CHECKING block: `from sqlalchemy import ColumnElement, Row`, `from sqlalchemy.orm import Session`; add runtime `from collections.abc import Iterable, Iterator, Sequence` (stdlib; annotations are strings under the module's existing `from __future__ import annotations`). `DatabaseManager` is already TYPE_CHECKING-imported. Exact signatures:

| Symbol | Signature after |
|---|---|
| `_timeline_segment_key` | `(segment_seconds: int) -> ColumnElement[int]` |
| `_timeline_segment_aggregates` | `(session: Session, show_id: int, segment_seconds: int) -> list[Row]` |
| `_timeline_detail_rows` | `(db_manager: DatabaseManager, show_id: int) -> Iterator[Row]` |
| `_timeline_instruments` | `(detail_rows: Iterable[Row]) -> list[str]` |
| `_timeline_key_changes` | `(detail_rows: Iterable[Row]) -> list[dict[str, int \| str]]` |
| `_timeline_reasoning_snippets` | `(detail_rows: Iterable[Row]) -> list[dict[str, int \| str \| None]]` |
| `_timeline_segment_dict` | `(agg: Row, detail_rows: Iterable[Row], segment_seconds: int) -> dict` |
| `_assemble_timeline_segments` | `(aggregates: Sequence[Row], detail_rows: Iterable[Row], segment_seconds: int) -> list[dict]` |
| `_stats_core_totals` | `(session: Session, show_id: int) -> Row` |
| `_stats_action_counts` | `(session: Session, show_id: int) -> dict[str, int]` |
| `_stats_keys_used` | `(session: Session, show_id: int) -> list[str]` |
| `_stats_instruments_used` | `(db_manager: DatabaseManager, show_id: int) -> list[str]` |
| `_stats_response` | `(totals: Row, action_counts: dict[str, int], keys_used: list[str], instruments_used: list[str]) -> dict` |

- Bare `Row` (not parameterized) — the FU-4 §1.4 precedent: "bare Column/Query — precise enough for the seam without SQLAlchemy generics gymnastics." Labeled-attribute rows are what the SQL actually returns; a Protocol per row shape would be speculative scaffolding.
- `ColumnElement[int]` for the floordiv expression (`.label()`/`group_by` consumer).
- Zero body changes, zero SQL changes — payloads byte-identical; ruff `UP006/UP007/UP045` clean by construction.
- LOC: 333 → ~341 (TYPE_CHECKING +3, annotations inline +5 wrapping at line-length 120). Well under 500.

### 1.3 db.py driver gating — the libpq kwargs follow the driver, not the backend

**Condition (exact):** the full libpq `connect_args` dict applies iff `url.get_driver_name() in _LIBPQ_DRIVERS` where

```python
_LIBPQ_DRIVERS = frozenset({"psycopg2", "psycopg2cffi", "psycopg"})
```

`make_url("postgresql://…").get_driver_name()` resolves the default to `"psycopg2"` — so T1's `postgresql://` pin and every deployment URL keep the kwargs untouched; explicit `postgresql+psycopg2://` joins the set; `psycopg` (v3) and `psycopg2cffi` are libpq-backed and accept these connection parameters; `asyncpg` and `pg8000` are not, and would raise on `keepalives*`/`options`/`connect_timeout` at first connect.

**Shape:** extract a pure module function (AGENTS: `__init__` stays 4–20 lines; the branch becomes compute + pass):

```python
def _pg_connect_args(url: URL) -> dict:
    """REL-09/FU-1 libpq connect+statement timeouts + FU-1 TCP keepalives —
    libpq drivers ONLY (FU-5): the keepalive keys are libpq connection
    parameters; asyncpg/pg8000 reject them at connect. Non-libpq PG URLs
    build with NO connect_args — SQLAlchemy's sync engine already rejects
    async drivers on its own, so the failure stays the dialect's, never a
    TypeError from our kwargs.
    """
    if url.get_driver_name() not in _LIBPQ_DRIVERS:
        return {}
    return {
        "connect_timeout": DB_CONNECT_TIMEOUT_SECONDS,
        "options": f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}",
        "keepalives": 1,
        "keepalives_idle": DB_KEEPALIVE_IDLE_SECONDS,
        "keepalives_interval": DB_KEEPALIVE_INTERVAL_SECONDS,
        "keepalives_count": DB_KEEPALIVE_COUNT,
    }
```

- `from __future__ import annotations` + runtime `from sqlalchemy.engine import URL` (beside the existing `make_url` import); `import logging` + module logger for the one-shot warning.
- The PG branch: `url = make_url(database_url)` once; `connect_args = _pg_connect_args(url)`; if the backend is PG **and** `connect_args == {}`, `logger.warning("DATABASE_URL uses non-libpq PG driver %r: connect/statement timeouts and TCP keepalives are libpq-only and were NOT applied", url.get_driver_name())` — offending value + expected shape, per AGENTS.
- **What stays for every PG dialect:** `pool_size`/`max_overflow`/`pool_pre_ping`/`pool_recycle` — dialect-agnostic SQLAlchemy pool knobs (D1 pins they survive on the asyncpg URL).
- **Disclosed scope:** this is branch hygiene, not async-engine support — an unmocked sync `create_engine("postgresql+asyncpg://…")` still fails in SQLAlchemy (correctly: misconfiguration). The D-tests use the house `patch("app.db.create_engine")` pattern, which is exactly how the branch contract is pinnable. The FU-1 plan's separate residual (asyncpg-level keepalives for `job_waiter`'s raw LISTEN connections) remains open and out of FU-5's cosmetic letter.
- LOC: 107 → ~127.

### 1.4 Audit-doc banner — exact wording, placement, preserved verdict

Insert directly under the `# Reliability Audit — Music Production Pipeline` title (before the "Deep reliability pass…" paragraph):

```markdown
> **STATUS (2026-09-12): remediated.** All 32 findings (the 5 Critical and
> 10 High below, plus every P1–P3 item) are fixed, regression-pinned, and
> landed on `main`; the ledger and each finding's `Status: fixed-in …` note
> live in `refactor/plans/rel-remediation-plan.md`. The 24/7 acceptance gate
> is the soak suite: `SOAK=1 .venv/bin/python -m pytest -m soak -q`.
> This document is the historical audit snapshot — the verdict below
> describes the pre-remediation tree and is preserved for provenance.
```

Concise (7 lines); references the plan doc + soak command; the historical **"Verdict: NOT 24/7-ready."** paragraph, every finding, and every `Status: fixed-in` note stay byte-identical (B1 pins the verdict line still exists AND the banner sits above it). Severity language matches the doc's own structure (5C+10H headers; REL-16+ under `## P1/P2/P3`) — no invented counts.

### 1.5 Worker headroom — DECISION: document, do not slice

`app/worker.py` sits at **498/500** — under the ceiling, 2 lines of headroom. Slicing now would be a second pure-move split one commit after FU-3's, with real patch-point cost and zero behavioral value; the existing S1 pin (`tests/test_worker_fu3.py:347`, fires the moment the file crosses 500) already enforces the rule. **FU-5 adds ZERO lines to worker.py** — not even a comment (it would eat the headroom). The note lands where editors look:

1. `CLAUDE.md` `app/worker.py` entry-point row gains: "498/500 LOC (FU-5 headroom note): at the ceiling — the next growth must first extract one of the mapped slices" + the candidate map below.
2. `refactor/plans/rel-remediation-plan.md` ledger: the closing note's "worker.py 498/500 headroom note" residual gets the "(DONE in FU-5: …)" annotation.
3. This plan (the map itself, for the future slicer).

**Candidate map (ordered by slice cost), with each one's patch-point cost:**

| Candidate | ~LOC | Patch-point cost (test edits required) |
|---|---|---|
| (a) `/health` + `get_stats` pair (`health_check`/`get_stats` `:402-443`) | 50 | none known — `worker_routes` calls them via `get_worker_instance()`; instance-attr lookups survive a mixin |
| (b) REL-23 VRAM-eviction pair + `VRAM_EVICTION_TIMEOUT_SECONDS` | 35 | one: the module-attr patch `worker_module.VRAM_EVICTION_TIMEOUT_SECONDS` (test_worker_vram.py:679) must retarget the new module — a re-export would NOT work (the method would read its own module's binding) |
| (c) CLI tail (`create_config_from_env`, `_worker_instance` accessors, `main`, `__main__`) | 55 | re-exports for `get_worker_instance` (two importer files) + `WorkerConfig` (test_worker.py ×6) + a `__main__` shim so `python -m app.worker` still runs |
| (d) breaker block (`_generate_with_lease`/`_handle_generation_timeout`/`GenerationIoTimeout`/both constants) | 95 | highest — `GENERATION_TIMEOUT_SECONDS` is module-attr-patched in 7 sites across test_worker_fu3/test_worker_vram/test_soak_worker; avoid |

### 1.6 Soak log-episode note — interpretation disclosed, then the deliverable

The ledger phrase "soak log-episode note" has no other trace in the repo or git history (grep + `git log --all` clean) — it is the round-2 closing note's shorthand for a never-written documentation note about logs during soak fault **episodes**. Interpreted (and flagged here as an interpretation, not a quote): an operator reading soak output needs to know which log markers the injected episodes are *designed* to emit, so designed noise is not mistaken for a regression — and so unbounded per-tick log repetition IS recognized as one. Deliverable — one bullet in `docs/soak_harness.md` §Notes:

> - **Reading soak logs (FU-5):** every fault episode emits a bounded, recognizable marker set; anything repeating per-tick is a finding. LLM-outage window → escalated backoff lines + `conductor call skipped` (submit-failure streak, rel-18), then the one-shot canary on recovery; PG-restart/worker-down windows → `loop_abandoned` terminal-fail markers (rel-12) + pending-depth skip lines; stuck generation → the REL-03 `circuit_breaker_open` JSON line exactly once (P5's contract); a failing mixer callback → ONE guarded-render line per failure EPISODE, not per tick (FU-1's episode-bounded guard). Log volume per episode is part of the soak contract — bounded by design, and a red soak assertion is a finding, not log noise to silence.

Every marker named is landed code (`loop_orchestrator` backoff/skip/canary, rel-12 abandon markers, worker breaker JSON, FU-1 mixer guard) — nothing speculative.

---

## 2. Exact changes per file

1. **NEW `app/stream_fanout_sessions.py` (~75 L)** — §1.1: provenance docstring (FU-5 pure move from `stream_fanout.py`, REL-10 context, "stdlib-only; imported by `stream_fanout`, never the reverse"), the five moved symbols byte-identical.
2. **`app/stream_fanout.py` (536 → ~489)** — delete `:60-63` + `:80-122`; add the one sessions-import line; drop the now-unused `dataclass` import; module docstring gains one line noting the sessions split. NOTHING else — the probe, `subprocess`, `resolve_ffmpeg_exe`, all classes/methods/factories stay.
3. **`app/lib/reasoning_stats.py` (333 → ~341)** — §1.2 table verbatim; TYPE_CHECKING additions; module docstring gains one FU-5 provenance line.
4. **`app/db.py` (107 → ~127)** — §1.3: `from __future__ import annotations`, `URL` import, `logging`, `_LIBPQ_DRIVERS`, `_pg_connect_args`, the warning in the PG branch; SQLite branches byte-identical.
5. **`docs/reliability_audit.md` (+7)** — the §1.4 banner under the title; nothing else.
6. **`docs/soak_harness.md` (+6)** — the §1.6 §Notes bullet.
7. **`tests/test_fu5_cosmetics.py` (NEW ~130 L)** — §3: S1/S2/A1/B1.
8. **`tests/test_db.py` (+~40)** — §3: D1/D2 in a sibling class `TestLibpqDriverGatingFu5` beside `TestEngineResilienceRel09`, same `DatabaseManager._instance = None` reset pattern.
9. **Docs stage:** `refactor/plans/rel-remediation-plan.md` — FU-5 row added to the round-2 table (`rel-fu-5-cosmetics`, **landed** at merge); closing-note residuals annotated "(DONE in FU-5: …)" in the FU-2/3/4 style. `CLAUDE.md` — `db.py` row (driver-gated libpq connect_args, FU-5); `stream_fanout.py` row + a new `app/stream_fanout_sessions.py` lib row; `app/worker.py` row headroom note (§1.5); test-table row for `test_fu5_cosmetics.py`.

## 3. TDD regression tests (write first; confirm red, then implement)

### 3.1 NEW `tests/test_fu5_cosmetics.py`

| # | Test | Pins | Core assertions |
|---|---|---|---|
| S1 | `test_stream_fanout_split_under_500_lines` | item 1 (**RED**) | `app/stream_fanout.py` AND `app/stream_fanout_sessions.py` exist; each `len(read_text().splitlines()) < 500` (FU-2/FU-3/FU-4 S1 loop pattern); message names the 500-LOC rule. Red: the sessions module does not exist. |
| S2 | `test_stream_fanout_sessions_seam_and_sentinel_identity` | item 1 (**RED**) | `from app.stream_fanout_sessions import _ClientSession, _STOP_SENTINEL, _drain_one, _drain_queue, _residual_blocks` imports; `app.stream_fanout._STOP_SENTINEL is app.stream_fanout_sessions._STOP_SENTINEL` (identity — the `is`-comparisons in `_feeder_loop`/`mp3_client_stream` depend on ONE object); `from app.stream_fanout import _STOP_SENTINEL` (the existing test seam) still resolves to the same object. Red: module missing. |
| A1 | `test_reasoning_stats_helpers_fully_annotated` | item 2 (**RED**) | H1 mirror, no `eval_str` (PEP-563 strings reference TYPE_CHECKING-only names — the pin is PRESENCE): for every symbol in the §1.2 table + both `compute_*_payload`, `inspect.signature` has a non-empty return annotation and non-empty annotation on every parameter. Red: today's bare `session`/`db_manager`/`detail_rows` params + missing returns. |
| B1 | `test_reliability_audit_doc_has_completion_banner` | item 4 (**RED**) | read `docs/reliability_audit.md`; in the first 2000 chars: contains `refactor/plans/rel-remediation-plan.md` AND `SOAK=1`; the banner block appears BEFORE the `Verdict: NOT 24/7-ready` line; the verdict line still present in the file (historical verdict preserved). Red: no banner today. |

### 3.2 `tests/test_db.py` — `TestLibpqDriverGatingFu5` (house `patch("app.db.create_engine")` pattern; `DatabaseManager._instance = None` reset around each construction)

| # | Test | Pins | Core assertions |
|---|---|---|---|
| D1 | `test_pg_asyncpg_driver_builds_without_libpq_kwargs` | item 3 (**RED**) | `DATABASE_URL=postgresql+asyncpg://u:p@h/db` → `create_engine` called once with `call_args == ("postgresql+asyncpg://u:p@h/db",)`, `connect_args == {}` (exact-dict: no keepalives, no `options`, no `connect_timeout`), AND `pool_pre_ping is True` + `pool_recycle == 1800` (dialect-agnostic pool protections stay). Red today: the branch passes the full libpq dict. |
| D2 | `test_pg_psycopg2_driver_keeps_libpq_kwargs` | item 3 (**RED**) | `DATABASE_URL=postgresql+psycopg2://u:p@h/db` → `connect_args ==` the exact T1 dict (connect_timeout 5, options statement_timeout 10000, keepalives×4). Red today: nothing (green on both pre/post? — no: **red-on-nothing is fine here**, it is the contract-keeper for D1's gating; if it is already green pre-fix, run it in the red phase anyway and record it as a characterization half of the D pair). |

### 3.3 Keep-green sets (must pass UNTOUCHED — run explicitly after each stage)

- **Stage fanout:** `tests/test_stream_fanout.py` (full file, zero edits — THE patch-point proof), `tests/test_round3_fix_d.py`, `tests/test_fu4_exports.py`, `tests/test_p3_hygiene.py`, `tests/test_stream_fanout_args.py`, `SOAK=1 … -m soak tests/test_soak_stream.py` (P6).
- **Stage annotations:** `tests/test_reasoning_logs.py`, `tests/test_fu4_exports.py` (H1/O1/O2), `tests/test_exports_pagination.py`.
- **Stage db:** `tests/test_db.py` (T1–T3 byte-identical asserts), `tests/test_db_offloop.py`, `tests/test_auth.py`, `tests/test_shows_api.py`.
- **Full gate + soak** after every stage.

### 3.4 TDD order

1. Write `tests/test_fu5_cosmetics.py` + the D pair → run → **S1/S2/A1/B1/D1 red** (module missing ×2; bare signatures; no banner; libpq dict on the asyncpg URL); D2 records green (characterization half).
2. **Fanout split alone, pure move** (§2.1/§2.2): keep-green set green with ZERO test edits → S1/S2 green. Any failure = the §1.1 inventory is wrong: stop and fix the split, not the tests.
3. Annotations (§2.3) → A1 green; ruff clean (F401 on nothing, line length).
4. db gating (§2.4) → D1 green, D2 + T1–T3 green untouched.
5. Banner + soak note (§2.5/§2.6) → B1 green.
6. Full gate: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q` → **1215 passed / 26 skipped** (1209 + 6); `SOAK=1 … -m soak -q` → 9p/1s.
7. Docs stage (§2.9) → reviewer loop.

## 4. Invariant compliance (plan §Invariants)

| # | How respected |
|---|---|
| 1 — lock discipline | No lock sections touched: the moved fanout block is lock-free plumbing; `_clients_lock`/`_fanout_lock` scopes unchanged; db.py adds no locking. |
| 2 — style / hexagonal | Both fanout files < 500 pinned (S1); annotations complete, no `Any` (A1 + review); names grep-unique (`stream_fanout_sessions`, `_pg_connect_args`, `_LIBPQ_DRIVERS` = 0 hits today); pure-move discipline (FU-2/3/4 precedent); `_pg_connect_args` is a pure function (no I/O) — trivially testable; `__init__` branches stay small. |
| 3 — audio thread | Zero mixer/feeder/pump loop edits — the split moves queue helpers only; behavior byte-identical. |
| 4 — LLM capture | reasoning_stats is annotations-only (SQL/payloads untouched); no export/shaper/flush/retention change; exports byte-identical. |
| 5 — worker restart semantics | worker.py untouched (0 lines added — §1.5); breaker/exit behavior unchanged. |
| 6 — regression per fix | S1/S2 pin the split; A1 the annotations; D1/D2 the driver gating; B1 the banner; §3.3 guards every neighboring seam; soak gate re-run. |

## 5. Acceptance checklist (maps to this unit's letter)

- [ ] stream_fanout < 500 with ALL stream/fanout/p3 tests green, zero test edits, every patch target accounted for (§1.1 table) → **S1/S2 + §3.3**
- [ ] moved private helpers fully annotated (TYPE_CHECKING, no `Any`) → **A1**
- [ ] `postgresql+asyncpg://` builds WITHOUT the libpq kwargs; `postgresql://` and `postgresql+psycopg2://` keep them → **D1/D2** (+ T1 untouched)
- [ ] audit doc banner present, concise, references plan doc + soak command, historical verdict preserved → **B1**
- [ ] worker headroom outcome documented (decision + candidate map + enforcement pointer) → §1.5, §2.9
- [ ] soak log-episode note written → §1.6, §2.6
- [ ] ruff clean; full gate 1209 + 6 = **1215 passed / 26 skipped**; soak 9p/1s
- [ ] audit/soak-harness/CLAUDE/plan docs updated (§2.9)

## 6. Risks / out of scope / residuals

- **Split regression risk** — mitigated exactly as FU-4: the move lands alone (stage 2), keep-green set required green with zero test edits; the §1.1 inventory is the proof obligation.
- **`stream_fanout.py` lands at ~489 (≈11 lines headroom)** — disclosed, same posture as worker.py post-FU-3; the next-growth seam is the §1.1-rejected delivery mixin (`_fanout_block`…`_discard_client_locked`, ~85 L, instance-attr-only patch points) — document, don't pre-empt.
- **D2 is green pre-fix by design** (the gating preserves the libpq path) — the D pair's red comes from D1; recorded as characterization, mirroring FU-3's B5.
- **The asyncpg branch is still a misconfiguration end-to-end** (sync engine rejects async drivers) — FU-5 fixes the kwargs leak and the diagnosis, not async-engine support; `job_waiter`'s raw-asyncpg keepalives remain the open FU-1 residual.
- **"Soak log-episode note" is an interpretation** (§1.6) — docs-only; if the parent meant something narrower, the bullet is additive and re-wordable at review.
- **Out of scope:** any fanout behavior change; rel-10's report-only residuals; worker.py slicing; export_chunks (done in FU-4); the audit doc's finding bodies.

## 7. Commit sequence (one branch `rel-fu-5-cosmetics`, Conventional Commits, land on `main` via parent)

1. `test(rel-fu-5): fanout split LOC + sentinel-identity pins, reasoning_stats annotation pin, db libpq driver-gating pins, audit banner pin — TDD red`
2. `refactor(rel-fu-5): stream_fanout session-plumbing split (pure move); annotate reasoning_stats helpers; gate db libpq connect_args on driver`
3. `docs(rel-fu-5): audit completion banner; soak log-episode note; worker headroom map; CLAUDE/plan ledger`

## 8. Validation commands

```bash
.venv/bin/python -m ruff check app tests
.venv/bin/python -m pytest tests/test_fu5_cosmetics.py tests/test_db.py -q
.venv/bin/python -m pytest tests/test_stream_fanout.py tests/test_p3_hygiene.py tests/test_round3_fix_d.py tests/test_fu4_exports.py -q
.venv/bin/python -m pytest tests/ -q                                   # full gate → 1215p/26s
SOAK=1 .venv/bin/python -m pytest -m soak -q                           # 9p/1s
```
