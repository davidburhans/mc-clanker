# PLAN — Unit 6 `rel-job-queue` (REL-12), branch `rel-12-queue`
**Spec:** `refactor/plans/rel-remediation-plan.md` §U6 · `docs/reliability_audit.md` REL-12 (High)
**Baseline gate at `a479975` (HEAD of `main` after U5 landed):** `.venv/bin/python -m pytest tests/ -q` → `1013 passed / 16 skipped` (verified green). `ruff check app tests` → **2 pre-existing `I001` import-sort errors in `tests/test_recording_fault_stop.py`** (rel-05 leftover, commit `461060e`; nothing else). Preflight for THIS unit must restore the gate: `.venv/bin/python -m ruff check --fix tests/test_recording_fault_stop.py` (mechanical import sort, disclosed as pre-existing debt repair — the unit's own full-gate step 7 requires a clean `ruff`). Do not regress skips without cause.
Preflight: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q`

Verified against code at HEAD (line refs current):
`app/framework/loop_steps.py` 962 L · `app/framework/job_queue.py` 144 L · `app/framework/ports.py` 200 L · `app/framework/loop_orchestrator.py` 485 L · `app/framework/pregeneration.py` 180 L · `app/worker.py` 635 L · `app/cleanup.py` 389 L · `app/models/generator_job.py` 173 L · `tests/test_queue_lease_and_dedup.py` 339 L · `tests/test_jobs_injection.py` 116 L. **No migration needed** — the reaper runs on existing columns (`status`, `created_at`) and is index-backed by `idx_generator_jobs_claiming (priority DESC, created_at ASC) WHERE status='pending'`; the depth COUNT is covered by `idx_generator_jobs_active (status) WHERE status IN ('pending','processing')`.

---

## 0. Scope summary

| Item | Root cause | Fix site |
|---|---|---|
| REL-12a (immortal abandoned jobs) | `_step_await_jobs_fetch` (loop_steps.py:571-620) and `run_pregeneration` (pregeneration.py:112-133) give up on a job after 600 s + 30 s grace and just print "failed or timed out" — the row stays `pending` forever; the worker's FIFO claim still generates it; the next loop's cache-miss resubmits the identical prompt (duplicate rows) | after the grace pass, call a new `JobQueuePort.abandon_jobs(job_ids)` from BOTH paths: `UPDATE ... SET status='failed', error_message='loop_abandoned'` guarded to `status='pending'` |
| REL-12b (pending rows never reaped) | `JobExpirationCleanup` reaps only stale `'processing'` (`_reap_stale_processing`, cleanup.py:192-223) and deletes only terminal rows (`_delete_expired_jobs`); rows submitted by `routes/jobs.py:97` (no loop ever abandons them) or orphaned by a web crash sit `pending` forever | new `_reap_stale_pending` pass in `JobExpirationCleanup`, wired via the U5 `_run_pass` isolation pattern, config knob `pending_grace_seconds` (default 86400, `0` disables) |
| REL-12c (no submission backpressure) | both submit loops (`_step_submit_jobs` :532-570, pregeneration :82-108) INSERT unconditionally; a sustained slow-worker episode grows the queue without bound (audit: ~700 rows/day per outage-day) | new `JobQueuePort.pending_depth()`; one COUNT probe per submit phase; when depth > `JOB_PENDING_DEPTH_LIMIT = 64`: skip-and-log ALL non-cached submissions this cycle (never block/never await drain), record them as outcome `"failed"` so the audit stays truthful; the cache-missed prompts retry next loop |

No worker, mixer, or capture-schema code is touched (invariants 1, 3, 4, 5). The worker claim SQL is deliberately unchanged (decision 8).

---

## 1. Design decisions (documented reasoning; deviations from spec letter flagged)

1. **The terminal-abandon UPDATE predicates on `status = 'pending'` ONLY — never `'processing'`.** The unit acceptance says "no path marks a RUNNING/claimed job failed", and a `'processing'` row is by definition claimed/running. This **deviates from the scout's `status IN ('pending','processing')` suggestion**, deliberately:
   - A live worker on a `'processing'` row heartbeats its lease and *will* terminal it (complete, or lease-reaper fails it) — `'processing'` rows already have bounded liveness; only `'pending'` rows are immortal (no `expires_at` predicate anywhere touches them).
   - Failing a row mid-generation forfeits the scout's own DATA-4 safety analysis: the worker's guarded `_mark_job_complete` (`WHERE status='processing' AND worker_id=$4`) would skip, its uploaded `audio/{job_id}.aac` object would be orphaned in Garage with no row ever naming it (the `_still_own_job_row` guard correctly refuses blind deletion because a *reclaimed* job reuses the same deterministic key), and the abandoned generation thread keeps burning VRAM anyway (REL-03: unkillable).
   - Race safety vs the claim transaction under READ COMMITTED, both directions: claim tx locks the row (`FOR UPDATE SKIP LOCKED`) and the abandon UPDATE re-evaluates `status='pending'` after its own lock wait → if the claim committed first, abandon no-ops. Conversely the claim's SELECT re-checks `status='pending'` under lock → if abandon committed first, the row is skipped and the worker claims the next one. No interleaving can double-own a row.
   - Residual (accepted, disclosed §6): a stem still `'processing'` at loop-expiry completes late; its audio is unused (cache-miss → resubmit). Bounded by the worker's own timeout circuit (invariant 5). The C8 `content_hash` in-flight dedup is the real fix for that waste — flagged out of scope.
2. **Ownership = the batch's ids ∩ non-terminal-pending; idempotent by predicate.** The loop passes exactly the job_ids of *this* batch whose awaited result is falsy (worker-`failed` rows are already terminal — the `status='pending'` guard makes the UPDATE a no-op on them, so no pre-filtering query is needed). Re-running the UPDATE on a later pass matches 0 rows. `error_message` uses `COALESCE(error_message, 'loop_abandoned')` (mirrors `_reap_stale_processing`) so a retry can never clobber a diagnostic. Timestamps (`completed_at`, `expires_at = now + 1 h`) are **computed in Python and bound as params**, not `NOW() + INTERVAL` — dialect-safe on the SQLite fallback (interval arithmetic is not portable) and the exact shape `submit_generator_job` already uses (`datetime.now(timezone.utc) + timedelta(...)`). The 1 h terminal expiry matches the existing reaper, so cleanup reclaims the row+any-audio on a later cycle.
3. **Abandon is best-effort at the call sites** (shared mixin helper `_abandon_missing_jobs`: try/except → one print, return). A failed abandon must never kill the loop or pregen (it's hygiene, not playback correctness) and the stale-pending reaper (fix b) is the backstop. This also keeps existing un-patched loop/pregen tests green: their real default adapter raises on the DB-less test env and the helper swallows it. The port method itself DOES raise on DB error (SQL observable by tests); only the loop-side wrapper catches.
4. **Reaper predicate: `created_at < NOW() - make_interval(secs => $1)`, default `86400`.** **Flagged deviation from the audit letter** ("fail stale `pending` rows past `expires_at`"): for a never-claimed row the two are equivalent at the default (submit sets `expires_at = created_at + 24 h`, and only terminal transitions ever bump it), but a `created_at`-based knob (a) decouples queue hygiene from the 24 h *terminal-retention* horizon (`expires_at`'s designed job), so an operator can reap backlog in an outage sooner than terminal rows expire, and (b) stays parameterizable in seconds while remaining sargable against `idx_generator_jobs_claiming` (`created_at` is in the partial index over exactly `status='pending'`). Default ON at 24 h = the audit's effective horizon; `PENDING_GRACE_SECONDS=0` disables (escape hatch, same convention as every U5 knob). `make_interval(secs => $1)` follows the `retention.py` `make_interval(hours => $1)` precedent.
5. **The reaper registers through `_run_pass("stale pending reaper", ...)`** (U5's error-isolation pattern) rather than the legacy direct calls of `_reap_stale_processing`/`_delete_expired_jobs` — one bad SQL run must not wedge the cycle. Keep-green detail (load-bearing): the disabled-check MUST be written in the `<= 0` form (`if self.config.pending_grace_seconds <= 0: return 0`) — the same convention `retention.py` uses — because tests that build `JobExpirationCleanup(MagicMock())` get a truthy MagicMock for the attr and the comparison-magic path then disables the pass, keeping `test_run_cleanup_reaps_then_deletes` (total == 3) and `test_worker.py`'s cleanup test green without edits. A `> 0`-enabled form would run SQL against those mocks and break the totals.
6. **Backpressure = one depth probe per submit phase, skip-and-log, fail-open, never block.** `JobQueuePort.pending_depth()` → `SELECT COUNT(*) WHERE status='pending'` (index-only on `idx_generator_jobs_active`; ~1-2 probes per 30-90 s loop cycle, none on all-cached loops — see decision 7). Bound: module constant `JOB_PENDING_DEPTH_LIMIT = 64` in `loop_steps.py` (monkeypatchable, `JOB_WAIT_TIMEOUT_SECONDS` precedent; no env knob — no speculative config). Normal steady state peaks at one batch (≤ 6 jobs; the single worker drains sequentially at 5-30 s/stem), so 64 ≈ >10× a full batch ≈ ~20+ min of pure GPU backlog: engaging means the drain rate has genuinely fallen behind. On engage: print, mark every uncached stem of this phase skipped, submit nothing, move on — the prompts stay cache-missed and retry next cycle, so drain naturally disengages the throttle (acceptance test T5). On probe error: warn + proceed (fail-open — a broken gauge must not stop the set; and if the DB is truly down, submit itself will fail into the existing B1 retry). The probe never sleeps, never waits on workers, never touches `state.lock` (invariant 1).
7. **The probe fires only when submission would occur** (two-phase P7: scan cache first, then `if uncached and await self._queue_backlogged()`). Zero DB probes for retained-stem loops (the common case), and existing cache-hit tests never touch the DB.
8. **Worker claim SQL untouched.** The audit's parenthetical "no `expires_at` filter in its claim" describes the symptom; the prescribed fix terminalizes abandoned rows, after which the existing `status='pending'` filter already excludes them. A staleness predicate on the claim would duplicate the reaper's grace knob in a second process and complicate the sargable claim predicate for no remaining immortal-row path (decision 1's race analysis covers the reaper↔claim overlap: claim commits → row `'processing'` → reaper predicate misses; reaper commits → row `'failed'` → claim's SELECT re-check under lock skips it). **Rejected alternative, documented.**
9. **Throttled stems report outcome `"failed"`, not a new `"skipped"` value.** `applied_actions.outcome` is a documented enum (`"generated" | "cached" | "failed"`, CLAUDE.md + `_audit_applied_actions` docstring) consumed by the DPO corpus (invariant 4: exports must round-trip losslessly) — widening it ripples into `parsed_response`-adjacent schema and `training/` consumers for marginal truth gain. `"failed"` is truthful (the stem got no audio); the loop log line carries the *why* (backpressure skip). **Flagged**: if reviewers prefer a 4th value it is a one-line change in the P8 seeding + a docs note, but it is deliberately not taken here.
10. **New adapter DB work runs via `asyncio.to_thread`** (both `abandon_generator_jobs` and `count_pending_jobs` wrap their sync `DatabaseManager.session()` body). This adds no NEW event-loop-blocking debt; the pre-existing submit-path debt is U7's unit (engine config + off-loop move) and is not pre-empted here.
11. **The pregen path mirrors the foreground fix** (scout risk #4: "pregeneration must also abandon its losers, or it re-leaks the bug"). Both the abandon call and the throttle check are reached through loop-owned helpers (`loop._abandon_missing_jobs`, `loop._queue_backlogged`) so the two paths cannot drift — the same MRO-delegate pattern the file's header documents.
12. **`content_hash` in-flight dedup stays out** (C8 scope; the column exists, is never set, and is deliberately not uniquely constrained — migrations/002 comment). Noted as the natural follow-up that would also collapse the duplicate-resubmit behavior on loop expiry.

---

## 2. Exact changes per file

### 2.1 `app/framework/ports.py` (200 → ~220 lines)
`JobQueuePort` grows two methods (docstrings + `...` bodies, file style), and the class docstring gains a U6/REL-12 note:
```python
    async def abandon_jobs(self, job_ids: list[UUID]) -> int:
        """Terminal-fail still-``pending`` jobs the caller has given up on (REL-12a).

        Only rows still 'pending' are touched — never a claimed/running
        ('processing') row. Idempotent: already-terminal rows are no-ops.
        Returns the number of rows abandoned.
        """
        ...

    async def pending_depth(self) -> int:
        """Current number of 'pending' generator jobs (REL-12c backpressure gauge)."""
        ...
```
The Protocol is `runtime_checkable`; the two test fakes (`tests/test_jobs_injection.py`, `tests/test_jobs_await_injection.py`) must grow matching methods (§3.3).

### 2.2 `app/framework/job_queue.py` (144 → ~215 lines)
**(a)** Two module functions beside `submit_generator_job` (lazy `app.db`/model imports, monkeypatch-at-call-time seam preserved):
```python
async def abandon_generator_jobs(job_ids: Sequence[uuid.UUID | str]) -> int:
    """Fail still-pending rows in ``job_ids`` as 'loop_abandoned' (REL-12a).

    Guarded to status='pending' ONLY: a claimed/running ('processing') row is
    never touched (unit acceptance: no path may fail a running job), and
    already-terminal rows are no-ops, so the call is idempotent and safe to
    retry. Timestamps are Python-side binds — interval arithmetic is not
    portable to the SQLite fallback. Prints when count > 0 (module convention).
    """
    ids = list(job_ids)
    if not ids:
        return 0

    def _abandon_sync() -> int:
        from sqlalchemy import update

        from app.db import DatabaseManager
        from app.models.generator_job import GeneratorJob

        now = datetime.now(timezone.utc)
        db_manager = DatabaseManager.get_instance()
        with db_manager.session() as session:
            stmt = (
                update(GeneratorJob)
                .where(GeneratorJob.id.in_(ids), GeneratorJob.status == "pending")
                .values(
                    status="failed",
                    error_message=func.coalesce(GeneratorJob.error_message, "loop_abandoned"),
                    completed_at=now,
                    expires_at=now + timedelta(hours=1),
                )
                .execution_options(synchronize_session=False)
            )
            return int(session.execute(stmt).rowcount or 0)

    count = await asyncio.to_thread(_abandon_sync)
    if count:
        print(f"[AsyncFrameworkLoop] Abandoned {count} job(s) still pending (loop_abandoned)")
    return count


async def count_pending_jobs() -> int:
    """Number of pending generator jobs — the REL-12c backpressure gauge."""
    def _count_sync() -> int:
        from sqlalchemy import func, select

        from app.db import DatabaseManager
        from app.models.generator_job import GeneratorJob

        db_manager = DatabaseManager.get_instance()
        with db_manager.session() as session:
            stmt = select(func.count()).select_from(GeneratorJob).where(GeneratorJob.status == "pending")
            return int(session.execute(stmt).scalar_one())

    return await asyncio.to_thread(_count_sync)
```
(imports: `asyncio`, `func` from sqlalchemy at module top or inside closures — module top for `func` is fine; keep the lazy `app.db` import pattern.)
**(b)** `PostgresJobQueueAdapter` gains `abandon_jobs` / `pending_depth` methods delegating to the module functions (mirroring how `submit`/`await_jobs` delegate — bare names resolve to module globals, never recurse).

### 2.3 `app/framework/loop_steps.py` (962 → ~1010 lines; brownfield >500 disclosed, same precedent as rel-02/03/04/05)
**(a)** Constant beside `JOB_LATE_COMPLETION_GRACE_SECONDS`:
```python
# REL-12c: submission backpressure bound. Steady state peaks at ONE uncached
# batch (<= 6 jobs — the single worker drains sequentially at 5-30 s/stem), so
# a depth over 10x a full batch means the drain rate has fallen behind by
# design; we skip-and-log submissions for the cycle (prompts stay cache-missed
# and retry next loop) instead of growing the queue without bound. Module attr
# so tests monkeypatch it (JOB_WAIT_TIMEOUT_SECONDS precedent).
JOB_PENDING_DEPTH_LIMIT = 64
```
**(b)** Result type beside `_PregenDecision`:
```python
class _SubmitJobsResult(NamedTuple):
    """P7 output: jobs submitted + stem indexes skipped by the REL-12c throttle."""

    pending_jobs: list  # [(job_id, original_index, cache_key)]
    skipped_idxs: list[int]
```
**(c)** Mixin delegate stubs beside `_await_jobs` (the documented "Delegate provided by AsyncFrameworkLoop" pattern):
```python
    async def _abandon_jobs(self, job_ids: list[Any]) -> int:
        """Delegate provided by ``AsyncFrameworkLoop`` (U6/REL-12a)."""
        raise NotImplementedError

    async def _pending_depth(self) -> int:
        """Delegate provided by ``AsyncFrameworkLoop`` (U6/REL-12c)."""
        raise NotImplementedError
```
**(d)** Two mixin helpers (each ≤ 20 lines):
```python
    async def _abandon_missing_jobs(self, job_ids: list[Any]) -> None:
        """REL-12a: terminal-fail still-pending jobs this loop gave up on.

        Best-effort by design: a failed abandon must never kill the loop —
        the stale-pending reaper (REL-12b) is the backstop.
        """
        if not job_ids:
            return
        try:
            count = await self._abandon_jobs(job_ids)
        except Exception as exc:  # noqa: BLE001 - hygiene, never fatal
            print(f"[AsyncLoop-{self._loop_idx}] abandon_jobs failed: {exc}")
            return
        if count:
            print(f"[AsyncLoop-{self._loop_idx}] Abandoned {count} job(s) (loop_abandoned)")

    async def _queue_backlogged(self) -> bool:
        """REL-12c: is the pending backlog over JOB_PENDING_DEPTH_LIMIT? Fail-open."""
        try:
            depth = await self._pending_depth()
        except Exception as exc:  # noqa: BLE001 - a broken gauge must not stop the set
            print(f"[AsyncLoop-{self._loop_idx}] pending-depth probe failed ({exc}); submitting anyway")
            return False
        return depth > JOB_PENDING_DEPTH_LIMIT
```
**(e)** `_step_submit_jobs` rewritten two-phase (docstring updated: mentions the throttle and the new return type):
```python
    async def _step_submit_jobs(self, local_next_stems, local_current_bpm, local_current_key) -> _SubmitJobsResult:
        """P7: submit generation jobs for uncached stems (REL-12c: skip-and-log
        the whole phase when the pending backlog exceeds the bound — never
        block; skipped prompts stay cache-missed and retry next loop)."""
        uncached = _collect_uncached_stems(local_next_stems, local_current_bpm, local_current_key, self.stem_cache)
        if uncached and await self._queue_backlogged():
            skipped = [i for i, _, _ in uncached]
            print(
                f"[AsyncLoop-{self._loop_idx}] Pending backlog over {JOB_PENDING_DEPTH_LIMIT}; "
                f"skipping {len(skipped)} submission(s) this cycle"
            )
            return _SubmitJobsResult([], skipped)
        pending_jobs = []
        for i, t, cache_key in uncached:
            job_id = await self._submit_job(...)  # unchanged kwargs block (session_id..bars)
            pending_jobs.append((job_id, i, cache_key))
        return _SubmitJobsResult(pending_jobs, [])
```
with a module-level `_collect_uncached_stems(stems, bpm, key, stem_cache) -> list[tuple[int, dict, str]]` holding the existing cache-HIT loop verbatim (including the REL-06 `last_used` refresh + `Cache HIT` print). Two-phase ordering note in a comment: the probe fires only when a submission would occur, so retained-stem loops cost zero queries.
**(f)** `_step_await_jobs_fetch` — signature grows `skipped_idxs: list[int] | None = None` (default keeps every existing 2-arg call green); first lines seed the outcomes, and the abandon slots in right after the grace pass:
```python
        outcomes: dict[int, str] = {}
        # REL-12c: a throttle-skipped stem reports "failed" — absent from the
        # map the applied-actions audit would default it to "cached" (a lie).
        for idx in skipped_idxs or ():
            outcomes[idx] = "failed"
        if pending_jobs:
            ...existing wait + reawait...
            # REL-12a: the loop has given up on anything still unreported —
            # terminalize the still-pending rows or they are immortal.
            missing = [job_id for job_id in job_ids if not results.get(job_id)]
            await self._abandon_missing_jobs(missing)
            ...existing keyed-lookup fetch/outcome loop unchanged...
```
**(g)** Docstring touch-ups: the P8 header note becomes `{orig_idx: "generated" | "failed"}` for submitted jobs *and throttle-skipped stems*.

### 2.4 `app/framework/loop_orchestrator.py` (485 → ~515 lines)
**(a)** Delegates beside `_await_jobs` (kept as methods so `patch.object(loop, '_abandon_jobs')` works — documented in both docstrings):
```python
    async def _abandon_jobs(self, job_ids: list[uuid.UUID]) -> int:
        """Fail still-pending jobs; delegates to the injected JobQueuePort (U6)."""
        return await self._jobs.abandon_jobs(job_ids)

    async def _pending_depth(self) -> int:
        """Pending-job count; delegates to the injected JobQueuePort (U6)."""
        return await self._jobs.pending_depth()
```
**(b)** `_run_loop` P7/P8 call sites (the ONLY consumer of the changed return shape):
```python
                    submit = await self._step_submit_jobs(local_next_stems, local_current_bpm, local_current_key)
                    stem_outcomes = await self._step_await_jobs_fetch(
                        submit.pending_jobs, local_next_stems, submit.skipped_idxs
                    )
```

### 2.5 `app/framework/pregeneration.py` (180 → ~205 lines)
Mirror of the foreground fix, reached through the loop's helpers so the paths cannot drift:
- submit phase: collect `uncached` (same cache-key scan, `loop.stem_cache`, REL-06 refresh kept); `if uncached and await loop._queue_backlogged():` → skip-and-print, record `skipped_idxs`; else the existing `loop._submit_job` loop unchanged.
- after `reawait_late_job_completions(...)`:
```python
            # REL-12a: this background path must abandon its losers too, or it
            # re-leaks the immortal-pending bug the foreground path just fixed.
            missing = [job_id for job_id in job_ids if not results.get(job_id)]
            await loop._abandon_missing_jobs(missing)
```
- before publishing `_pregen_results`: `for idx in skipped_idxs: stem_outcomes[idx] = "failed"` (audit truthfulness, same as foreground).

### 2.6 `app/cleanup.py` (389 → ~415 lines)
**(a)** `CleanupConfig` += `pending_grace_seconds: int = 86400` with the decision-4 comment (24 h = the audit's effective horizon; `0` disables; seconds so an outage can be reaped tighter than the terminal-retention horizon).
**(b)** `_retention_kwargs()` += `"pending_grace_seconds": _env_int("PENDING_GRACE_SECONDS", 86400)` — shared by `create_cleanup_config_from_env()` AND the one-shot `cleanup_expired_jobs_once` (the rel-05 review round-1 P2 lesson: never split env parsing between the two config constructors). Docstring gains one line noting it also carries the REL-12 queue-hygiene knob.
**(c)** New pass (the `<= 0` guard form is load-bearing — decision 5):
```python
    async def _reap_stale_pending(self) -> int:
        """Fail 'pending' jobs older than pending_grace_seconds (REL-12b).

        The loop terminal-abandons its own losers (REL-12a); this pass is the
        backstop for rows no loop ever waits on (API submissions, a crashed
        web process) and bounds outage backlog at ~one grace period. NEVER
        touches 'processing' rows — those are claimed/running and owned by
        the lease/reclaim machinery.
        """
        grace = self.config.pending_grace_seconds
        if grace <= 0:
            return 0
        assert self.db is not None
        async with self.db.acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE generator_jobs
                SET status = 'failed',
                    error_message = COALESCE(error_message, 'queue_backlog_reaped'),
                    completed_at = NOW(),
                    expires_at = NOW() + INTERVAL '1 hour'
                WHERE status = 'pending'
                  AND created_at < NOW() - make_interval(secs => $1)
                RETURNING id
                """,
                grace,
            )
        reaped = len(rows)
        if reaped:
            logger.warning("Reaped %d stale 'pending' jobs (backlog past %ds)", reaped, grace)
        return reaped
```
**(d)** `_run_cleanup` (docstring updated):
```python
        reaped = await self._reap_stale_processing()
        backlog = await self._run_pass("stale pending reaper", self._reap_stale_pending)
        deleted_count = await self._delete_expired_jobs()
        ...
        return reaped + backlog + deleted_count + sessions + files + audit
```
**(e)** Module docstring env-var list += `PENDING_GRACE_SECONDS`.

### 2.7 `docker/compose.yaml` (+2 lines)
Cleanup service env block, beside `SESSION_STALE_HOURS`:
```yaml
      # REL-12: fail pending jobs older than this (backstop for rows no loop
      # ever abandons; 0 disables). Default = the audit's 24h horizon.
      - PENDING_GRACE_SECONDS=${PENDING_GRACE_SECONDS:-86400}
```
(No worker-service change: the worker's in-process cleanup loop reads the same env with the same code default — harmless idempotent duplication, both guarded by `status='pending'`.)

### 2.8 `.env.example` (+5 lines)
In the retention block: `PENDING_GRACE_SECONDS` — "age at which never-claimed pending generator jobs are failed with error_message='queue_backlog_reaped' (0 = off; default 86400 = the audit's 24 h horizon). Consumed by the compose cleanup service and the worker's in-process cleanup loop."

### 2.9 Docs (pipeline "docs" stage, same unit)
- `docs/reliability_audit.md`: REL-12 gains a `**Status: fixed-in rel-12-queue**` paragraph (loop+pregen terminal abandon; stale-pending reaper + knob; depth-bounded skip-and-log throttle; worker claim left unchanged — decision 8 rationale in one line).
- `refactor/plans/rel-remediation-plan.md`: status row 6 → landed (commit at merge).

---

## 3. TDD regression tests (write first, confirm red, then implement)

### 3.1 NEW `tests/test_job_queue_lifecycle.py` (~380 lines) — REL-12
Fixtures: loops built as `AsyncFrameworkLoop(uuid4())` with `loop._jobs = _FakeRel12JobQueue()` (a named fake recording `abandoned`/returning `depth`, with real `abandon_jobs`/`pending_depth` methods) or default adapter + `patch.object(loop, ...)` delegates; cleanup tests reuse the `test_queue_lease_and_dedup.py` `_make_conn`/`_pool_yielding` shape; adapter SQL tests use a named `_SqliteJobStore` fake monkeypatched over `app.db.DatabaseManager` (tmp-file engine with `connect_tables` + `check_same_thread=False`, real `GeneratorJob` rows — the conftest singleton reset keeps tests isolated).

| # | Test | Pins | Core assertions |
|---|---|---|---|
| T1 | `test_await_jobs_fetch_abandons_expired_batch_jobs` (**acceptance**) | REL-12a | `_await_jobs` → `{job0: "audio/a.aac", job1: None}`; P8 → `jobs.abandoned == [[job1]]` (only the unreported one), `outcomes == {0: "generated", 1: "failed"}`. |
| T2 | `test_abandon_failure_never_breaks_the_loop` | decision 3 | fake `abandon_jobs` raises RuntimeError → P8 still returns outcomes (`1: "failed"`), no raise. |
| T3 | `test_abandon_delegate_routes_through_injected_port` | hex seam | `await loop._abandon_jobs([...])` → fake awaited with the ids, count returned. |
| T4 | `test_submit_throttles_when_pending_depth_over_limit` (**acceptance**) | REL-12c | fake `pending_depth` → 65, two uncached stems, `loop._submit_job = AsyncMock()` → not awaited; result `.pending_jobs == []`, `.skipped_idxs == [0, 1]`. |
| T5 | `test_submit_throttle_disengages_after_drain` (**acceptance**) | REL-12c | depth sequence `[65, 63]` → first P7 skips, second submits (`_submit_job` awaited once); `loop.stem_cache` untouched (prompt stays cache-missed for retry). |
| T6 | `test_depth_probe_failure_fails_open` | decision 6 | fake `pending_depth` raises → submissions proceed. |
| T7 | `test_throttled_stems_report_failed_not_cached` | decision 9 | P7 over-limit → P8 with `skipped_idxs` → outcomes for skipped idx == `"failed"` (absent would default `"cached"` in `_audit_applied_actions`). |
| T8 | `test_submit_unthrottled_shape_unchanged` | regression pin | depth under limit → `.pending_jobs` triples `(job_id, i, cache_key)` in stem order, `.skipped_idxs == []`; cache-HIT stems still skipped from submission (REL-06 refresh intact). |
| T9 | `test_pregeneration_abandons_its_losers` (**acceptance**) | scout risk 4 | `run_pregeneration` with `_await_jobs` → `{job: None}`; patched `loop._abandon_missing_jobs`/fake port → awaited once with `[job]`; `_pregen_results["stem_outcomes"][idx] == "failed"`. |
| T10 | `test_pregeneration_throttles_when_backlogged` | REL-12c | fake depth high → `loop._submit_job` not awaited; skipped stems → `stem_outcomes[idx] == "failed"`. |
| T11 | `test_default_adapter_satisfies_grown_port` | hex seam | `isinstance(PostgresJobQueueAdapter(), JobQueuePort)` still true (port grew two methods). |
| T12 | `test_abandon_generator_jobs_fails_only_pending_rows` (**acceptance, no-running-job pin**) | REL-12a + acceptance 4 | SQLite store: 2 pending + 1 processing (`worker_id="w1"`, live lease) + 1 completed; `abandon_generator_jobs([all four])` → returns 2; pending rows: `failed` + `error_message='loop_abandoned'` + `completed_at` set; **processing row untouched (still `'processing'`)**; completed untouched. |
| T13 | `test_abandon_is_idempotent_and_empty_safe` | decision 2 | second call on the same ids → 0, rows unchanged; `[]` → 0 and no session opened. |
| T14 | `test_count_pending_jobs_counts_only_pending` + adapter delegation | REL-12c gauge | store: 2 pending + 1 processing + 1 failed → `count_pending_jobs() == 2`; adapter `pending_depth`/`abandon_jobs` delegate to the module globals (monkeypatch sentinel, `test_submit_job_uses_injected_jobs` pattern). |
| T15 | `test_reap_stale_pending_fails_only_past_threshold` (**acceptance**) | REL-12b | fake conn; `pending_grace_seconds=3600` → SQL contains `status = 'pending'`, `created_at < NOW() - make_interval(secs => $1)`, `status = 'failed'`, `'queue_backlog_reaped'`, arg 3600; fetch → 2 ids → returns 2; **SQL must NOT contain `status = 'processing'`** (acceptance 4: the reaper can never fail a claimed row). |
| T16 | `test_reap_stale_pending_disabled_at_zero` | decision 5 | `pending_grace_seconds=0` → zero SQL executed, returns 0. |
| T17 | `test_run_cleanup_pending_reaper_is_error_isolated` | decision 5 | fake conn whose pending-reaper fetch raises → `_run_cleanup` still returns the other passes' total, no raise. |
| T18 | `test_env_pending_grace_plumbing` | decision 4 | `_retention_kwargs()`: env `120` → 120; unset → 86400; `0` → 0; garbage → warning + 86400 (`_env_int` contract). |

### 3.2 Existing-behavior pins that stay green unchanged (verify, no edit)
- `test_queue_lease_and_dedup.py::test_mark_complete_is_atomic_and_notifies` / `test_mark_failed_releases_lease` — worker's own fail paths stay ownership-guarded (acceptance 4's worker half).
- `test_claim_reclaims_stale_processing_and_sets_lease` — claim SQL untouched (decision 8).
- `test_run_cleanup_reaps_then_deletes` (total == 3) — MagicMock config disables the new pass via the `<= 0` guard (decision 5).

### 3.3 Keep-green edits (existing tests, minimal)
- `tests/test_jobs_injection.py::_FakeJobQueue` += `abandon_jobs`/`pending_depth` (its `isinstance(fake, JobQueuePort)` assertions require the grown Protocol).
- `tests/test_jobs_await_injection.py::_FakeJobQueue` += same (documented mirror of the other fake).
- Sweep set to run explicitly: `test_jobs_injection`, `test_jobs_await_injection`, `test_queue_lease_and_dedup`, `test_llm_capture` (T14-style P8 test at :963 now flows through the best-effort abandon — real adapter raises DB-less, helper swallows), `test_round3_fix_b`, `test_pregeneration_divergence`, `test_reset_reprime` (P7 cache-hit tests — two-phase probe never fires), `test_cache_key`, `test_worker`, `test_worker_vram`, `test_storage_retention` (RoutingFakeConnection returns `[]` for unknown fetch SQL → totals 3/0 hold), `test_async_framework`, `test_simulation`, `test_job_waiter`, `test_dpo_pipeline`.

### 3.4 TDD order
0. Preflight: `.venv/bin/python -m ruff check --fix tests/test_recording_fault_stop.py` (pre-existing I001, §Baseline) → gate green before any red.
1. Write `tests/test_job_queue_lifecycle.py` → run → **red** (T1/T3: no `_abandon_jobs` delegate; T4-T7: no throttle; T12-T14: no module functions — import error is expected red; T15-T18: no reaper/knob).
2. Implement §2.1 + §2.2 (port + adapter) → T11–T14 green; §3.3 fake updates land with them.
3. Implement §2.3 + §2.4 (loop_steps + orchestrator delegates) → T1–T8 green; loop/pregen suites in the sweep stay green.
4. Implement §2.5 (pregeneration) → T9–T10 green.
5. Implement §2.6 (reaper + config + env) → T15–T18 green; cleanup/worker suites green.
6. Implement §2.7 + §2.8 (compose + env example) — YAML/docs; `docker compose -f docker/compose.yaml config -q` sanity if docker available.
7. Full gate: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q` → **1013 + ~18 new passed / 16 skipped**, zero regressions.
8. Docs stage (§2.9) → reviewer loop.

---

## 4. Invariant compliance (plan §Invariants)

1. **Lock discipline:** no new `state.lock`/`sync_lock` sections at all — the throttle reads no state, the abandon path touches only the job queue; all DB work happens between the loop's existing lock scopes (P7/P8 hold no lock). Nothing blocking under any lock.
2. **Hexagonal + style:** both new capabilities enter through `JobQueuePort` (driven port) with the loop faked in-memory (T3-T5 need no DB); module functions ≤ 20 lines each; new helpers ≤ 20 lines; files stay under 500 except pre-existing disclosed brownfield debts (`loop_steps.py` 962→~1010, `loop_orchestrator.py` 485→~515, `worker.py`/`cleanup.py` already over) — same disclosed-debt precedent as rel-02/03/04/05. No `Any` added (mixin delegate stub uses `list[Any]` matching the file's existing `Any` usage for job ids).
3. **Audio path:** untouched — no mixer, no per-tick code.
4. **LLM capture:** no flush/buffer/schema change; the throttle deliberately maps skipped → the documented `"failed"` outcome (decision 9) so `applied_actions` never reports a lie; retention of corpus tables untouched.
5. **Worker/restart semantics:** zero worker-code changes (decision 8); the reaper runs in the dedicated cleanup service (and idempotently in the worker's in-process loop), so queue hygiene survives a circuit-broken worker.
6. **Regression tests:** T1, T4/T5, T12, T15 map 1:1 to the U6 acceptance bullets; T2/T6/T13/T16/T17 pin the failure modes.

---

## 5. Acceptance checklist (maps to §U6 spec + task)

- [ ] Expired-batch rows end `failed` with the `loop_abandoned` marker — T1 (loop path), T9 (pregen path), T12 (row-level SQL, idempotent).
- [ ] Stale-pending reaper fails only past-threshold rows — T15 (SQL + arg), T16 (disable knob), T18 (env plumbing).
- [ ] Submission throttle engages over the bound and disengages after drain — T4 (engage, skip-and-log, never block), T5 (disengage after drain, prompt retried), T6 (fail-open), T7 (audit-truthful outcomes).
- [ ] No path marks a RUNNING/claimed job failed — T12 (abandon leaves `'processing'` untouched), T15 (reaper predicates on `'pending'` only), plus existing worker ownership-guard tests (§3.2).
- [ ] Full gate green: ruff + 1013+~18 passed / 16 skipped.

---

## 6. Risks / out of scope / residuals

- **Pre-existing preflight debt (disclosed):** `ruff` reports 2 `I001` import-sort errors in `tests/test_recording_fault_stop.py` at baseline `a479975` (rel-05 leftover). Step 0 of the TDD order fixes them mechanically; if the reviewer prefers zero unrelated edits in this branch, they can be landed separately first — but the unit's own full-gate step must not paper over them.
- **Late-completing `'processing'` stems (accepted):** a stem still generating at loop expiry completes afterwards; its audio is unused and the next loop resubmits the same prompt (duplicate completed row). Bounded by the worker timeout circuit; the real dedup fix is C8 `content_hash` (out of scope, decision 12).
- **Backpressure is depth-bounded, not rate-adaptive:** the spec's "observed drain rate" is satisfied by the depth bound (engagement ⇔ drain behind); an adaptive drain-rate estimator is speculative and deliberately not built.
- **Throttle probe in tests reads a real (SQLite-fallback) DB:** existing un-patched P7/pregen tests fail-open via exception; a *polluted* local `app/data/mc_clanker.db` holding > 64 pending rows could in theory red them (implausible — test rows are terminal; CI is clean). New tests patch the probe.
- **Guard-form coupling (decision 5):** `MagicMock`-config tests stay green only while the disabled-check keeps the `<= 0` form; T16 pins the `0`-disables contract, and the form is called out in §2.6c so a future refactor notices.
- **Two reaper instances (cleanup service + worker loop):** both default-on and idempotent (`status='pending'` guard); worst case is a duplicate WARNING line per cycle. Accepted.
- **24 h default reaper changes API semantics in multi-day outages:** a `POST /api/jobs` row older than the grace comes back `failed` instead of eventually-completed — that is REL-12's intent (backlog past the horizon is worthless work); `PENDING_GRACE_SECONDS=0` restores the old behavior.
- **Pre-existing sync-session submit debt** blocks the event loop (U7 owns it); the NEW adapter methods wrap in `asyncio.to_thread` so this unit adds no fresh debt.
- **Out of scope:** C8 in-flight dedup via `content_hash`; any `expires_at`-at-submit horizon change; a `"skipped"` fourth audit outcome (decision 9); `/api/health` surfacing of throttle state; migration files (none needed — verified).

## 7. Validation commands

```bash
.venv/bin/python -m ruff check app tests
.venv/bin/python -m pytest tests/test_job_queue_lifecycle.py -q          # new suite
.venv/bin/python -m pytest tests/ -q                                      # full gate
docker compose -f docker/compose.yaml config -q                           # YAML sanity (if docker available)
```
