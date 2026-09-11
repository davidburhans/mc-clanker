# Round 3 — Adversarial Bug Hunt: Job Queue / DB / Worker / Cleanup lane

Scope: `app/worker.py`, `app/framework/job_queue.py`, `app/job_waiter.py`, `app/cleanup.py`,
`app/db.py`, `app/models/*.py`, `migrations/*.sql` (plus the submit/await call sites in
`loop_steps.py` / `pregeneration.py` that drive the queue). Prior rounds' findings
(`00_SYNTHESIS.md`, `00_FINAL_REPORT.md`, `tests/test_adversarial_wave1/wave2/leftovers.py`,
`tests/test_queue_lease_and_dedup.py`) were read first; nothing below re-reports a fixed or
pinned item.

---

## CONFIRMED BUGS

### Q1 — HIGH | `app/framework/loop_steps.py:454` (+ `app/framework/pregeneration.py:107`, `app/worker.py:137-143`, `app/job_waiter.py:159-165`) | Fixed 120 s batch-wait timeout is shorter than the single worker's worst-case sequential drain → stems silently dropped and regenerated every loop, forever, with no dedup

**Mechanism.** Three constants combine badly:
- `loop_steps.py:454` and `pregeneration.py:107` wait for the *whole batch* of N jobs with a hard `timeout=120.0` (seconds, wall clock).
- `worker.py:137-143` processes jobs **strictly sequentially** (`while self.running: await self._process_next_job()`; compose runs exactly one worker service, `docker/compose.yaml:75-96`, no replicas).
- `job_waiter.py:159-165`: on timeout the waiter does one final status check **at the timeout instant**; a job that completes 1 second later is lost — returns `None` (proven below).

Downstream, a `None` result is a black hole: `_step_await_jobs_fetch` (`loop_steps.py:456-466`) just prints `"Job ... failed or timed out"` and writes **nothing** to `stem_cache`; `tile_to_loop` then fills that stem with zeros (`domain_audio.py:124-127` "fell back to silence"), while `_step_commit_state` still commits the stem dict into `state.active_stems` (`loop_steps.py:559`). The conductor sees the stem as "playing", retains it, and the next loop's `_step_submit_jobs` cache check (`loop_steps.py:422`) misses → **re-submits an identical job**. There is no content dedup anywhere to stop this: the `content_hash` column exists (`models/generator_job.py:120`, `migrations/002` §2) but **no code path ever writes it** (grep: only the model def + `to_dict`), and no submit-time dedup/partial-unique index exists (migration 002 explicitly deferred it as "follow-up").

**Trigger.** Documented generation latency is 5–30 s/stem and conductor density 4–6 stems (CLAUDE.md). 6 × 30 s = 180 s > 120 s. It also fires deterministically on every worker (re)start: `GeneratorRegistry.load()` only parses config (`framework_generator.py:170-181`); weights load lazily inside the **first** `generate_stem` (`framework_generator.py:348-356`), so the first job takes 30–90 s+ (minutes on cold HF cache, `framework_generator.py:67-70`) while jobs 2..6 sit queued → every batch member after ~job 2 blows the 120 s budget.

**Impact.** Stems appear in the UI/state but are silent; the worker keeps burning GPU regenerating the same prompts every loop while the queue accumulates stale pending jobs (pending jobs never expire from the claim query — `worker.py:161-165` has no `expires_at` bound on `status='pending'`). Self-sustaining churn on any slow-generation deployment; broken first loop after each worker restart.

**Minimal fix.** Scale the wait with batch size/lease, e.g. `timeout = max(120, JOB_LEASE_SECONDS + 30 * len(job_ids))`, and on timeout **re-check late completions** (keep the job ids; at next loop, query `status='completed'` rows for those ids before re-submitting). Longer term, populate `content_hash` at submit and add the deferred partial-unique + `ON CONFLICT` claim.

**Proof.** Waiter loses a completion that lands 0.4 s after the timeout (real `JobWaiter` code):

```
$ .venv/bin/python - <<'EOF'
import asyncio, uuid
from unittest.mock import AsyncMock, MagicMock
from app.job_waiter import JobWaiter
async def main():
    start = asyncio.get_event_loop().time()
    async def fake_get(job_id):   # job completes at t=0.9s
        if asyncio.get_event_loop().time() - start < 0.9:
            return {"status": "processing", "audio_path": None}
        return {"status": "completed", "audio_path": "audio/late.aac"}
    waiter = JobWaiter(db_pool=MagicMock()); waiter._get_job = fake_get
    conn = AsyncMock(); pool = MagicMock()
    pool.acquire = AsyncMock(return_value=conn); pool.release = AsyncMock()
    waiter.db_pool = pool
    print(await waiter.wait_for_job_completion(uuid.uuid4(), timeout=0.5))
asyncio.run(main())
EOF
→ None        # completed at t=0.9s, waiter gave up at t=0.5s and never looks again
```

The regeneration loop is then mechanical: no `stem_cache` write (loop_steps.py:461-466) → cache miss → `_submit_job` again (loop_steps.py:425-440); `content_hash` has zero writers (grep over `app/`).

### Q2 — MED | `app/worker.py:172-178` (with `:321-345`, `:311-318`) | Orphan-audio cleanup after a failed completion write deletes `audio/{job_id}.aac` unconditionally — the same deterministic path a reclaiming worker may have already completed with its own upload; no ownership/status re-check

**Mechanism.** `_process_claimed_job` catches **any** exception from `_mark_job_complete` and calls `_delete_orphan_audio(audio_path)` (worker.py:177). `_delete_orphan_audio` (worker.py:311-318) deletes the path with no DB re-check. The lease-ownership guard added in round 2 (DATA-4, worker.py:330-343) correctly prevents the *row* write from a zombie worker, but the *object* delete on the **exception** path (e.g. the worker's DB connection is down — exactly the condition that let its lease lapse in the first place) still runs against the shared path `audio/{job_id}.aac` (worker.py:296). If worker B reclaimed the lapsed-lease job (worker.py:161-165) and completed it, B's live, DB-referenced audio object is destroyed; the completed row's `audio_path` now 404s and `_fetch_audio` silently returns `None` → the stem is dropped.

Note the asymmetry that proves the gap: the rowcount-0 "lost lease" branch (worker.py:340-341) deliberately skips the delete, but the exception branch — which is precisely the branch that co-occurs with a lapsed lease during a DB outage — does not distinguish "my row" from "a reclaimed row" before deleting.

**Trigger.** Worker A's heartbeats fail ≥ ~9.5 min (DB outage/partition) while its Garage link stays up → lease lapses → B claims and completes (uploads `audio/{id}.aac`, row `completed`) → A's upload already replaced/preceded B's bytes at the same key → A's `_mark_job_complete` raises on its dead DB conn → A deletes B's object.

**Impact.** A `completed` job whose audio file no longer exists; framework fetch returns `None`, stem silently missing; no reconciliation path ever notices (row says completed + audio_path set).

**Minimal fix.** In the exception handler, re-read the row first (e.g. `SELECT status, worker_id FROM generator_jobs WHERE id=$1`) and only delete when the row is still `'processing'` owned by `self.config.worker_id` (or gone); otherwise leave the object for the new owner / cleanup.

**Proof** (delete fires with no ownership re-check; mechanism executed at HEAD):

```
$ .venv/bin/python - <<'EOF'
import asyncio, sys, types, uuid, logging
from unittest.mock import AsyncMock, MagicMock
logging.basicConfig(level=logging.CRITICAL)
fake = types.ModuleType("app.framework.framework_generator")
class GeneratorRegistry:
    def __init__(self, *a, **k): self.models = {}
    def load(self): pass
fake.GeneratorRegistry = GeneratorRegistry
sys.modules["app.framework.framework_generator"] = fake
from app import worker as worker_module
async def main():
    w = worker_module.GeneratorWorker(worker_module.WorkerConfig(
        worker_id="worker-A", pg_dsn="postgresql://u:p@localhost/db", garage=MagicMock()))
    job_id = uuid.uuid4()
    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=[RuntimeError("connection closed"), Exception("down")])
    tx = MagicMock(); tx.__aenter__ = AsyncMock(return_value=None); tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)
    pool = MagicMock(); pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    w.db = pool
    deleted = []
    class Garage:
        async def delete_object(self, p): deleted.append(p)
    w.garage = Garage()
    async def fake_gen(job): return f"audio/{job_id}.aac", 4.0   # A already uploaded here
    w._generate_with_lease = fake_gen
    try:
        await w._process_claimed_job({"id": job_id, "instrument": "pad",
                                      "model_id": "foundation-1", "prompt": "p",
                                      "key": "", "bpm": 120, "bars": 4})
    except Exception as e:
        print("propagated after cleanup:", type(e).__name__)
    print("deleted:", deleted)
asyncio.run(main())
EOF
→ deleted: ['audio/271b...aac']   # the reclaiming worker B's live path — no guard consulted
```

(The delete-on-exception behavior itself is pinned as intended by `test_process_claimed_job_cleans_orphan_when_complete_fails`, `tests/test_queue_lease_and_dedup.py:171`; what is **not** covered is the reclaimed-owner collision above.)

### Q3 — LOW | `app/cleanup.py:203` | `cleanup_expired_jobs_once()` creates its asyncpg pool without `command_timeout` → the one-shot cron path can hang forever on a half-open connection

**Mechanism.** The ASYNC-3 fix bounded every other pool (`JobExpirationCleanup.start` → 60 s, cleanup.py:68; worker → 300 s, worker.py:100; job_waiter → 30 s, job_waiter.py:33), but the standalone one-shot entry point at cleanup.py:203 calls `asyncpg.create_pool(config.pg_dsn, min_size=1, max_size=5)` with **no `command_timeout`**. Per review B3, a half-open TCP connection makes the first query hang indefinitely; `_run_cleanup` has no outer timeout.

**Trigger.** Run `cleanup_expired_jobs_once()` (documented "cron job or one-shot operation", cleanup.py:176-178) while the DB connection dies mid-query.

**Impact.** The cleanup cron run wedges forever (silent — no watchdog in that path); expired jobs/audio stop being reclaimed.

**Minimal fix.** `create_pool(..., command_timeout=60)` — one line, matching cleanup.py:68.

---

## SUSPECTED (UNVERIFIED)

### Q4 — LOW | `app/cleanup.py:106-127` + `app/worker.py:286-308` | Permanent S3 orphans: rows are deleted before objects, and zombie uploads can leave objects no row references

**Mechanism.** (a) `_delete_expired_jobs` deletes the rows via CTE and only *then* deletes the Garage objects, with failures merely logged (cleanup.py:118-127) — a crash/Garage outage between the two steps leaves objects that **no row will ever point at again** (the row is gone), so no future sweep can find them. (b) A worker whose lease lapsed can still complete its `_generate_and_upload` (upload happens before any ownership-guaranteed DB write); if it then never terminal-writes the row (guard skips, or `_mark_job_failed` also fails), the row keeps `audio_path IS NULL` while the object `audio/{id}.aac` exists — `_delete_expired_jobs` selects `audio_path` **from rows**, so the object is unreachable forever.
**Trigger.** Garage outage during a cleanup cycle; or worker DB-outage ≥ lease window with Garage still up.
**Impact.** Slow unbounded bucket growth (no GC exists for unreferenced objects).
**What would confirm.** Fault-inject `delete_object` in `_delete_expired_jobs` and diff bucket keys vs `generator_jobs.audio_path` afterwards; simulate a zombie upload with `status='failed', audio_path IS NULL` and show no cleanup path can reach the key.

### Q5 — LOW | `app/models/session_routing.py:11-20` vs `migrations/001_jobs_and_routing.sql:55-72` | Remaining model↔migration drift on `session_routing` (the C3 reconciliation covered only `generator_jobs`)

**Mechanism.** Migration 001 declares `session_id UUID PRIMARY KEY`, `created_at/last_heartbeat TIMESTAMPTZ`, plus `idx_session_routing_server` and `idx_session_routing_heartbeat`. The model declares `String(36)` PK and **naive** `DateTime`, and declares no indexes. On the documented production path (`Base.metadata.create_all()`), the table is created as VARCHAR(36)/naive-TIMESTAMP with zero secondary indexes; on a migration-initialized DB (docker-compose.test.yml mounts migrations into `docker-entrypoint-initdb.d`) it is UUID/TIMESTAMPTZ. Today this is inert for writes because `routes/jobs.py` and `app_ui.py:356-366` use raw SQL, but: the naive-TIMESTAMP path makes `get_session_heartbeat`'s `replace(tzinfo=utc)` (routes/jobs.py:232) wrong on any host whose PG timezone is not UTC, and the missing indexes turn the per-request middleware lookup (app_ui.py:358-365) and any future stale-session reaper into seq scans.
**What would confirm.** Boot the app against a `create_all()`-only PG schema with `timezone != 'UTC'`, POST a heartbeat, and observe `is_stale`/serialized offsets; `EXPLAIN` the middleware SELECT.

---

## CHECKED AND CLEAN

Tried to break, and why it held:

- **FOR UPDATE SKIP LOCKED correctness** — claim's SELECT+UPDATE share one transaction (worker.py:161-186; pinned by `test_claim_reclaims_stale_processing_and_sets_lease`). Reaper-vs-claim interleavings are safe under READ COMMITTED: the reaper's UPDATE re-evaluates `lease_expires_at < NOW()` against the claimant's new row version (EvalPlanQual) and skips; the claimant's `SKIP LOCKED` skips rows the reaper has locked.
- **Expired-lease reaper vs slow-but-alive worker (double generation)** — heartbeat every 60 s keeps a 600 s lease fresh (worker.py:253-262); lapse requires ≥ ~9.5 min of *consecutive* heartbeat failures, at which point at-least-once re-execution is the correct design. Zombie terminal writes are lease+owner guarded (worker.py:330-343, 352-369; pinned by wave2 `TestData4LeaseOwnershipGuards`). Skew between worker clock (lease written in Python) and DB `NOW()` is absorbed by the 540 s heartbeat margin.
- **NOTIFY visibility / commit-before-notify** — `_mark_job_complete` does UPDATE + `pg_notify` in one transaction (worker.py:325-344; pinned). Waiter double-checks status after `add_listener` (job_waiter.py:137-146; pinned `test_waiter_rechecks_status_after_subscribing`). Reaper/`_mark_job_failed` send no NOTIFY, but that only adds bounded ≤120 s latency before the waiter's final check — acknowledged round-2 residual.
- **Requeue storms at claim time** — the reaper sets `expires_at = NOW()+1h` so reaped rows are deleted by the next `_delete_expired_jobs` cycle; claimed rows get fresh future leases so the reaper can't fail an in-flight reclaim.
- **Transaction boundaries / rollback** — `DatabaseManager.session()` commits on success, rolls back and re-raises on error, closes in `finally` (db.py:34-44); every lane call site uses it or an `async with acquire()` CM; `JobWaiter` releases its listener connection in a nested finally even if `add_listener` raises (job_waiter.py:127-160). No session-leak path found in lane files.
- **Naive vs aware datetimes** — `GeneratorJob` columns are `DateTime(timezone=True)`; submit (`job_queue.py:50`, `routes/jobs.py:22,81`), claim/heartbeat (`worker.py:208,262`) all pass `datetime.now(timezone.utc)`; cleanup compares against PG `NOW()`; pinned by `test_model_timestamps_are_timezone_aware`. Shows-side aware/naive was normalized in round 2 (`_as_naive_utc`).
- **Lease/cleanup unit confusion** — lease 600 s ≫ heartbeat 60 s; `GENERATION_TIMEOUT_SECONDS == JOB_LEASE_SECONDS == 600` with heartbeats refreshing to `now+600` every 60 s, so a healthy worker can never time out its own lease; expires_at budgets (24 h terminal/complete, 1 h failed/reaped) are consistent across worker/cleanup.
- **Cleanup deleting audio still referenced by live state** — loop audio is fetched from S3 **once** into memory (`audio_fetch.py`); stem downloads serve from the in-memory LRU; nothing re-fetches `audio/{id}.aac` after the loop that consumed it, and terminal jobs keep audio ≥1–24 h after completion. No reachable deletion of in-use audio.
- **Dedup key vs unique constraint** — no unique constraint on `content_hash` (intentional, documented in migrations/002 §2); foreground and pregen share one cache-key function (`make_cache_key`, domain_audio.py:50-64) covering exactly the worker's generation inputs (prompt/model/bpm/key/bars).
- **`wait_for_multiple_jobs` zip alignment** — dict keys are fresh `uuid4`s per submit, so duplicates (which would misalign `zip(pending_jobs, results.values())` in loop_steps.py:460) are impossible.
- **Pool exhaustion** — waiter holds ≤1 listener conn per job (4–6 typical, ~12 worst-case pregen+foreground overlap) against `max_size=10`; stalls self-resolve because every holder finishes within its bounded 120 s wait / 30 s command timeout. asyncpg DSN resolution prefers raw `DATABASE_URL` and strips `+driver` (pinned by 4 tests).
- **`_update_rowcount` fallback** — returns 1 only for unparseable tags, which real asyncpg never emits (`UPDATE n` always).
- **Jobs marked complete without audio / crash mid-upload** — `audio_path` is only written from a successful `put_object` return value; between upload and the guarded complete there is no raisable statement except `get_audio_duration` (pure arithmetic). A crash there leaves the job processing → lease reclaim handles the row (the orphaned-object half is Q4).
- **`generator_job` model↔migration reconciliation** — columns/constraints/partial indexes match 001+002 and are pinned (`test_model_has_lease_and_content_hash_and_indexes`, `test_model_has_status_check_constraint`); both schema-boot paths (create_all-only vs migrations-init) produce compatible shapes for the raw-SQL and ORM consumers.

## VERIFICATION

FINDING Q1 | Verified | `timeout=120.0` is at app/framework/loop_steps.py:454 and app/framework/pregeneration.py:107 with the worker draining strictly sequentially (app/worker.py:137-142) under a single no-replica compose worker service, and the lossy wait reproduces on the executing waiter path (app/job_waiter.py:323 gathers the whole batch under one 120 s budget, returns None at app/job_waiter.py:293-297 — the cited app/job_waiter.py:159-165 JobWaiter branch is only entered when db_manager is passed, but my re-run returned None for a completion 0.4 s late either way), while the downstream chain holds (no stem_cache write at loop_steps.py:461-466 → silence fill at domain_audio.py:124-127 → active_stems commit at loop_steps.py:559 → unconditional re-insert at job_queue.py:56-68 with zero content_hash writers beyond app/models/generator_job.py:120,172, and no expires_at bound on pending at app/worker.py:195-200), with no test pinning late-completion recovery.

FINDING Q2 | Verified | The exception branch at app/worker.py:175-178 calls `_delete_orphan_audio` which issues `delete_object` with no DB read of any kind (app/worker.py:311-319) while B can reclaim the lapsed row (app/worker.py:195-216) and completes under the identical key `audio/{job_id}.aac` (app/worker.py:303), and the asymmetry is real because the rowcount-0 lost-lease path returns without deleting (app/worker.py:347-349); the hunter's snippet re-run at HEAD printed `deleted: ['audio/<uuid>.aac']` with no ownership query executed, and only the non-colliding case is pinned (tests/test_queue_lease_and_dedup.py:171).

FINDING Q3 | Weakened | The missing bound is exactly as cited (AST of app/cleanup.py:203 gives keywords `['min_size','max_size']` vs `[…,'command_timeout']` at app/cleanup.py:68, and `_run_cleanup` has no outer timeout), but `cleanup_expired_jobs_once` has zero callers in the repo — every deployed cycle runs through the worker's cleanup loop on the 300 s pool (app/worker.py:119) or the un-deployed `python -m app.cleanup` → start() path already bounded at 60 s — so the indefinite hang is latent in an uncalled entry point, not in a live cron.

FINDING Q4 | Verified | The CTE deletes the rows inside the statement and only then best-effort deletes objects with per-object failures swallowed (app/cleanup.py:145-157, 165-173; re-run returned `2` while `delete_object` raised, leaving nothing recording the key), and the zombie-upload half holds because `audio_path` is written only by the lease-guarded complete (app/worker.py:303-304, 347-349) while `_delete_expired_jobs` selects paths FROM rows (app/cleanup.py:152) and no key-sweep/GC exists anywhere in app/ (no `list_objects`).

FINDING Q5 | Verified | Compiling the model for the postgres dialect yields `session_id VARCHAR(36)` PK plus `TIMESTAMP WITHOUT TIME ZONE` created_at/last_heartbeat with `table.indexes == []` (app/models/session_routing.py:13-19) against migration 001's UUID PK/TIMESTAMPTZ plus idx_session_routing_server (migrations/001_jobs_and_routing.sql:78) and idx_session_routing_heartbeat (:82), create_all is the boot path (app/db.py:39, called at app/app_ui.py:68), and the raw-SQL consumers that make the naive column observable are live at app/routes/jobs.py:224-231 (`replace(tzinfo=utc)`) and app/app_ui.py:359-362, with only a table-existence pin in tests (tests/test_db.py:94).

VERDICT: 4 verified, 1 weakened, 0 falsified
