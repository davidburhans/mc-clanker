# PLAN — Unit FU-3 `rel-fu-worker`, branch `rel-fu-3-worker`
**Spec:** `refactor/plans/rel-remediation-plan.md` §Follow-ups round 2 (FU-3) · follow-up notes "rel-03 (P2, report-only)" + "rel-13 (from rel-24 review): residual …" · `docs/reliability_audit.md` REL-03 status paragraph (breaker semantics live there) · breaker design of record: `refactor/plans/units/rel-03-plan.md` §1 decisions 1–3, 11 (counter = "since the last COMPLETED pipeline"; exit ctor-injected; counter surfaced in `get_stats`)
**Items:** (1) `consecutive_generation_timeouts` in worker `/health` (not just `/stats`); (2) breaker `timeout → non-timeout-failure → timeout` branch pin (rel-03 residual); (3) `_refresh_lease` worker_id-scoped (rel-13-review residual); (4) `_mark_job_complete` 0-rowcount no-op no longer counted as `jobs_processed` (rel-13-review residual); (5) py3.11 `TimeoutError`-alias catch-narrowing + note (rel-03 residual); (6) `app/worker.py` split under 500 LOC (rel-03/24 debt).
**Baseline gate (verified green at `59bae97`, HEAD of `main`):** `1197 passed / 26 skipped` (~32 s), `ruff check app tests` clean, `SOAK=1 pytest -m soak` = 9p/1s. Do not regress skips without cause.

---

## 0. Scope summary

| Item | Root cause today | Fix site |
|---|---|---|
| /health counter | `health_check()` (worker.py L665–691) reports `jobs_processed`/`jobs_failed` but not `consecutive_generation_timeouts` — the early-warning breadcrumb between timeout #1 and the trip is invisible on the endpoint a load balancer/operator polls (the container healthcheck passes while wedged; rel-03 decision 11 put it in `get_stats` only) | `health_check()` healthy + unhealthy branches |
| Breaker branch pin | rel-03 decision 1 already gives the correct semantics (non-timeout failures leave the counter untouched), but NO test covers `timeout → fail → timeout` — the branch is unpinned and a future "reset on any exception" refactor would pass today's suite silently | `tests/test_worker_fu3.py` B5 (test-only; no behavior change) |
| `_refresh_lease` scope | `UPDATE … WHERE id = $2 AND status = 'processing'` (L481–495) — after a reclaim the row's `worker_id` is the NEW owner, yet the zombie's heartbeat keeps extending `lease_expires_at`; if the new owner then dies, the zombie's heartbeats make the row effectively immortal (reaper + reclaim both key on the lease) | `_refresh_lease` SQL: `AND worker_id = $3` |
| 0-rowcount counted | `_mark_job_complete` no-ops on `UPDATE 0` (DATA-4 guard, logs "lost lease") but returns None either way; `_process_claimed_job` unconditionally does `jobs_processed += 1` — a zombie's lost-lease completion inflates the count (cosmetic-but-misleading health signal) | `_mark_job_complete` returns `bool`; caller counts + logs only on True |
| py3.11 alias | `except asyncio.TimeoutError` around `wait_for` (L371–409): on ≥3.11 `asyncio.TimeoutError is builtin TimeoutError`, so a pipeline-internal I/O timeout (upload/socket) escaping `_generate_and_upload` would land in the breaker's handler and count as a stall (false trip / wrong `error_message`). Worker container pins 3.10 today; the dev venv is 3.12 — the alias is LIVE in tests | Provenance re-wrap in `_generate_and_upload` + explicit `asyncio.TimeoutError` catch + WHY notes |
| 745/500 split | `app/worker.py` is **745 lines** (rel-03 said 633; rel-24/25 + FU-era edits grew it — AGENTS.md 500-LOC rule, brownfield debt now retired like FU-2 did for `loop_orchestrator.py`) | pure move of the job-row lifecycle block → new `app/worker_job_rows.py` mixin + relocation of the audio-boundary helper → `app/aac_encoder.py` |

No mixer / loop / capture / storage / claim-SQL policy code is touched (invariants 1, 3, 4 unaffected; invariant 5 affirmed — see §4).

---

## 1. Design decisions (documented reasoning; deviations flagged)

### 1.1 `/health` carries the breaker counter — both branches, dict passthrough

1. **Add `consecutive_generation_timeouts` to the healthy AND the unhealthy branch** of `GeneratorWorker.health_check()`. The unhealthy branch (DB fetch raised) is exactly the wedged-ish state where the counter matters, and reading it is a memory access — no I/O, no new failure mode. This mirrors FU-1's "observable health" posture (mixer failure counter in `/api/health`).
2. **`get_stats()` already carries it** (rel-03 decision 11) — unchanged.
3. **No schema edit needed:** the live worker routes (`app/worker_routes.py`) are plain-dict passthroughs (`return await worker.health_check()`). NOTE: `app/routes/worker_routes.py` (the `response_model=WorkerHealthResponse` variant) is **dead code** — it imports `WorkerHealthResponse`/`WorkerStatsResponse` from `app/routes/schemas.py`, which defines no such classes (would ImportError), and nothing imports the module. Left untouched, flagged in §6.

### 1.2 Breaker `timeout → fail → timeout` — pin, don't change

The semantics of record (rel-03 decision 1, quoted in `_handle_generation_timeout`'s docstring): increment on `asyncio.TimeoutError` from `wait_for`; reset ONLY in the `else:` (completed pipeline); **non-timeout failures deliberately leave the counter untouched** — the thread abandoned by timeout #1 survives an unrelated later failure still holding VRAM/hf locks. Therefore `timeout → RuntimeError → timeout` already trips at the second timeout. FU-3 adds the missing TEST (B5) — no production change. Injection uses only the existing fakes (swapping `worker.generators.generate_stem` between a sleeper and a raiser, the `test_worker_vram.py` B-series pattern).

### 1.3 `_refresh_lease` worker_id scoping — same predicate shape as the DATA-4 guards

```sql
UPDATE generator_jobs
SET lease_expires_at = $1
WHERE id = $2 AND status = 'processing' AND worker_id = $3
```
with params `(lease_expiry, job_id, self.config.worker_id)`. Rationale: `_claim_next_job`'s reclaim path and the stale-processing reaper re-assign ownership; a zombie worker's heartbeat (its event loop still alive enough to sleep+UPDATE, generation thread wedged) must not extend the NEW owner's lease — worst case that makes a reclaimed row immortal if the new owner dies. `_mark_job_complete`/`_mark_job_failed` already use exactly this `status='processing' AND worker_id=$n` shape; this closes the one remaining unscoped write. Heartbeat error handling is unchanged (`_heartbeat_loop` already wraps failures in a warning + keeps generating; a 0-rowcount UPDATE is not an error).

### 1.4 `_mark_job_complete` returns `bool`; the caller owns counting + the loss log

- `_mark_job_complete` keeps its current warning on `UPDATE 0` and `return False` there; `return True` after the NOTIFY. Signature `-> bool`.
- `_process_claimed_job` restructures the success block:
  ```python
  try:
      completed = await self._mark_job_complete(job["id"], audio_path, duration)
  except Exception as e:  # noqa: BLE001 - upload ok, DB commit failed -> orphan
      …existing orphan-reclaim/mark-failed block, unchanged…
  if not completed:
      # FU-3: a 0-rowcount completion is a lost lease, not a processed job —
      # counting it inflates the /health signal the breaker diagnosis reads.
      logger.warning("Job %s not counted as processed: completion skipped (lease lost)", job["id"])
      return
  self.jobs_processed += 1
  logger.info("Job %s completed: %s", job["id"], audio_path)
  ```
- `_mark_job_failed` keeps its void contract: a failed pipeline DID fail locally; `jobs_failed` counts local outcomes (both the row-write and the no-write zombie case). Out of FU-3's letter, unchanged.
- Backward-compat audit (done): `test_adversarial_wave2.py` DATA-4 tests ignore the return value; `test_queue_lease_and_dedup.py::test_mark_complete_is_atomic_and_notifies` ignores it; `test_worker_correctness.py::test_lost_lease_stands_down_without_fail_or_count` mocks `_mark_job_complete = AsyncMock()` → truthy MagicMock → control run still counts (`jobs_processed == 1` assertion preserved); `test_round3_fix_e.py` drives it with `side_effect=RuntimeError` → exception path unchanged. `test_worker.py`'s AsyncMock `execute` returns an unparseable tag → `_update_rowcount` falls back to 1 → True. Zero test edits needed.

### 1.5 py3.11 `TimeoutError`-alias catch-narrowing — narrow by provenance, not type

1. **The type alone cannot distinguish on ≥3.11** (`asyncio.TimeoutError is TimeoutError`), so narrowing happens by PROVENANCE: only `wait_for`'s own deadline may feed the breaker. `_generate_and_upload` becomes a thin wrapper that re-wraps any builtin `TimeoutError` escaping the pipeline into a new `GenerationIoTimeout(RuntimeError)`; the existing body moves verbatim to `_run_generation_pipeline(job, gen_pool)`.
   ```python
   async def _generate_and_upload(self, job, gen_pool=None):
       """<existing docstring stays here (public seam, test-patched name)>"""
       try:
           return await self._run_generation_pipeline(job, gen_pool)
       except TimeoutError as exc:
           # FU-3: on py3.11+ builtin TimeoutError aliases asyncio.TimeoutError,
           # so a pipeline-internal I/O timeout (garage upload, socket) would
           # otherwise land in _generate_with_lease's asyncio.TimeoutError
           # handler and feed the REL-03 breaker. Re-wrap so it counts as an
           # ordinary failure (jobs_failed), never as an abandoned-thread stall.
           raise GenerationIoTimeout(f"generation pipeline I/O timeout: {exc}") from exc
   ```
   On 3.10 `except TimeoutError` (builtin) does not match `asyncio.TimeoutError` at all — and `_generate_and_upload` awaits no `wait_for` internally — so the wrap is exact on every version: the only `asyncio.TimeoutError` reaching `_generate_with_lease`'s handler is `wait_for`'s deadline.
2. **`_generate_with_lease` keeps `except asyncio.TimeoutError`** (the explicit asyncio class — never a bare `TimeoutError`), now with the alias note:
   ```python
   except asyncio.TimeoutError as exc:  # ONLY wait_for's own deadline (see
       # _generate_and_upload's GenerationIoTimeout re-wrap: on py3.11+ the
       # builtin TimeoutError ALIASES asyncio.TimeoutError, so pipeline I/O
       # timeouts are re-wrapped at the boundary to keep them out of here).
   ```
3. `GenerationIoTimeout(RuntimeError)` — sibling of `LostLeaseError(RuntimeError)`; message includes the offending value ("generation pipeline I/O timeout: <original>"), per AGENTS.md exception rule. Propagates through `_process_claimed_job`'s generic handler → `jobs_failed`, counter untouched, no exit.
4. `_maybe_evict_idle_models`' own `except asyncio.TimeoutError` is untouched: its inner is `to_thread(...)` (raises no TimeoutError) and its timeout does not feed the breaker.
5. Dev venv is 3.12 → the narrowing test (B6) is a genuine red today (bare `TimeoutError` from `garage.put_object` currently lands in the breaker: counter→1, message "exceeded"). On 3.10 the test passes trivially pre-fix — documented in the test docstring, same asymmetry the audit note describes.

### 1.6 The split — target module, moved symbols, patch-point proof

**Hard constraint driving the design:** several tests monkeypatch names in `app.worker`'s *module namespace* (`monkeypatch.setattr(worker_module, "GENERATION_TIMEOUT_SECONDS", 0.05)` …). A function reads module globals from the module where it is DEFINED, so any patched-name reader that moves to a new module would silently stop seeing the patch. **Every patched-name reader therefore stays in `app/worker.py`.**

**A. New module `app/worker_job_rows.py` (~305 lines incl. FU-3 edits), mixin `_JobRowLifecycle`** — the FU-2 `loop_delegates.py` pattern: pure move (byte-identical except §1.3/§1.4 edits), module docstring documents the host contract (`GeneratorWorker.__init__` provides `config`, `db`, `garage`, counters, `_generate_with_lease`), `from __future__ import annotations`, **torch-free / framework_generator-free** (imports only `asyncio`, `logging`, `uuid`, `datetime`) — the worker import-time torch rule is preserved. `class GeneratorWorker(_JobRowLifecycle):` (mixin first in MRO, mirroring `_LoopDelegates`; only concrete methods, no stub collisions).

Moved symbols (owner: "the lifecycle of a job ROW from claim to terminal state"):
| Symbol | LOC | Note |
|---|---|---|
| `JOB_LEASE_SECONDS`, `JOB_LEASE`, `JOB_LEASE_HEARTBEAT_SECONDS` (+comment) | 5 | no test patches these (grep-verified) |
| `LostLeaseError` | 10 | raised by `_generate_and_upload` (stays in worker.py) → **re-exported** via `from app.worker_job_rows import LostLeaseError, _JobRowLifecycle` |
| `_update_rowcount` | 14 | internal to the two terminal writes; no test patches it |
| `_process_claimed_job` | 36 | §1.4 edit rides the move; calls `self._generate_with_lease` (host method, call-time resolution — same as `_LoopDelegates`→orchestrator) |
| `_read_job_ownership` | 14 | |
| `_still_own_job_row` | 22 | delete-guard semantics byte-identical (L7 pins them) |
| `_lease_still_held` | 24 | |
| `_claim_next_job` | 43 | claim SQL deliberately unchanged (rel-12 decision) |
| `_heartbeat_loop` | 9 | instance-patched in `test_adversarial_wave2` — see table below |
| `_refresh_lease` | 15 | §1.3 edit rides the move |
| `_delete_orphan_audio` | 10 | |
| `_mark_job_complete` | 34 | §1.4 edit rides the move |
| `_mark_job_failed` | 26 | |

**B. Relocation to `app/aac_encoder.py` (201 → ~225):** `_resample_to_mixer_rate` + `MIXER_SAMPLE_RATE` move to the audio-codec boundary module (REL-25a's resample is PCM math beside `encode_aac`/`decode_aac`/`_normalize_decoded_audio`; `aac_encoder` is already on worker.py's import path). worker.py imports both back (`from app.aac_encoder import MIXER_SAMPLE_RATE, _resample_to_mixer_rate, encode_aac, get_audio_duration`) — keeping `worker_module._resample_to_mixer_rate` alive for the UNTOUCHED S5 test. Pure move; the lazy `scipy.signal` import and its fake-torch docstring stay verbatim. This relocation is what buys LOC headroom (see math below): the mixin alone lands worker.py at ~503 after the FU-3 additions — over the line.

**C. Stays in `app/worker.py`** (≈477 after all edits): module docstring/imports; `GENERATION_TIMEOUT_SECONDS`, `GENERATION_TIMEOUT_BREAKER_THRESHOLD`, `VRAM_EVICTION_TIMEOUT_SECONDS`, `DEFAULT_CFG_SCALE`, `DEFAULT_STEPS`; `GenerationIoTimeout` (new); `_silently_cancel`; `WorkerConfig`; `GeneratorWorker` (`__init__`, `start`, `_process_next_job`, `_generate_with_lease`, `_handle_generation_timeout`, `_maybe_evict_idle_models`, `_evict_idle_models_sync`, `_generate_stem_for_job`, `_generate_and_upload` + `_run_generation_pipeline`, `get_stats`, `health_check`, `stop`, `_cleanup_loop`); `create_config_from_env`; module singleton; `main`.

**D. LOC math:** 745 − 262 (moved block) + 1 (mixin/Error import) − 26 (`_resample_to_mixer_rate` + `MIXER_SAMPLE_RATE` block) + 19 (FU-3 additions: health +2, `GenerationIoTimeout` +6, wrapper +8, alias comments +3) ≈ **477** (23-line headroom). `worker_job_rows.py` ≈ 262 + ~35 (docstring/imports) + 8 (§1.3/§1.4 edits) ≈ **305**. `aac_encoder.py` ≈ **225**. All <500, pinned by S1.

**E. Patch-point inventory — every seam the suite touches, and why it survives:**

| Seam (exact test syntax) | Used by | Reader lives in (after split) | Survives because |
|---|---|---|---|
| `monkeypatch.setattr(worker_module, "GENERATION_TIMEOUT_SECONDS", 0.05)` | `test_worker_vram` B1–B4; `test_queue_lease_and_dedup::test_generate_with_lease_times_out`; `test_soak_worker` P5 | `app/worker.py` (`_generate_with_lease`, `_handle_generation_timeout`) | reader unchanged file |
| `monkeypatch.setattr(worker_module, "VRAM_EVICTION_TIMEOUT_SECONDS", 0.05)` | `test_worker_vram` E5 | `app/worker.py` (`_maybe_evict_idle_models`) | unchanged |
| `monkeypatch.setattr(worker_module, "encode_aac" / "get_audio_duration", …)` · `patch.object(worker_module, "encode_aac"/"get_audio_duration", …)` | `test_worker_vram` B3; `test_worker_correctness` L1/L2/S1–S4; `test_adversarial_wave2` private-pool test; `test_soak_worker` P5 | `app/worker.py` (`_generate_and_upload` body → `_run_generation_pipeline`) | unchanged file; wrapper delegates to it |
| `patch.object(worker_module, "ThreadPoolExecutor", RecordingPool)` | `test_adversarial_wave2` ASYNC-2 | `app/worker.py` (`_generate_with_lease`) | unchanged |
| `monkeypatch.setattr(worker_module.asyncpg, "create_pool", …)` · `setattr(worker_module, "create_garage_client_from_env"/"create_cleanup_config_from_env", …)` | `test_worker_vram` P1 | `app/worker.py` (`start`) | unchanged |
| attribute reads `worker_module.GeneratorWorker` / `WorkerConfig` / `LostLeaseError` / `_resample_to_mixer_rate` | `test_worker.py` (function-body imports); `test_worker_correctness` (`pytest.raises(worker_module.LostLeaseError)`, S5) | worker.py (`LostLeaseError`, `_resample_to_mixer_rate` as imported re-exports — same objects) | imported names ARE module attributes |
| instance-attr patches `patch.object(worker, "_heartbeat_loop"/"_generate_and_upload", …)` · `monkeypatch.setattr(worker, "_generate_with_lease"/"_mark_job_complete"/"_mark_job_failed", …)` · `worker.generators.generate_stem = …` · `worker.garage.*` | `test_adversarial_wave2`; `test_queue_lease_and_dedup`; `test_round3_fix_e`; `test_worker_correctness` L5/L6; `test_worker_vram`; `test_soak_worker` P5 | definition site irrelevant | instance attrs shadow class/mixin methods; `self.<name>` resolves the instance attr first via MRO |
| instance attrs `jobs_processed` / `jobs_failed` / `consecutive_generation_timeouts` / `db` / `garage` / `generators` | many | `__init__` stays in worker.py | unchanged |

Known cosmetic deltas of the move (disclosed, no test asserts them): log records emitted by moved methods change logger name `app.worker` → `app.worker_job_rows`; `LostLeaseError.__module__` changes (same class object, `repr` text only).

**F. Verified NOT patched anywhere** (grep over `tests/`): `JOB_LEASE*`, `MIXER_SAMPLE_RATE`, `_update_rowcount`, `_silently_cancel`, `DEFAULT_CFG_SCALE`, `DEFAULT_STEPS` — safe to relocate with their readers.

---

## 2. Exact changes per file

### 2.1 NEW `app/worker_job_rows.py` (~305 lines)
- Module docstring: purpose ("job-row lifecycle adapter of `GeneratorWorker` — claim, ownership predicates, lease heartbeat/refresh, terminal writes, orphan audio; FU-3 pure move from `worker.py` under the 500-LOC rule, `_LoopDelegates` pattern"), host contract, FU-3 edits called out (§1.3/§1.4).
- Imports: `from __future__ import annotations`; `asyncio`, `logging`, `uuid`, `datetime`/`timedelta`/`timezone`. **No torch, no framework_generator.**
- Constants + `LostLeaseError` + `_update_rowcount` moved verbatim (§1.6 A table).
- `class _JobRowLifecycle:` with the ten methods moved verbatim EXCEPT:
  - `_refresh_lease`: SQL gains `AND worker_id = $3`; execute params `(lease_expiry, job_id, self.config.worker_id)` (§1.3).
  - `_mark_job_complete`: `-> bool`; `return False` in the 0-rowcount branch (existing warning kept); `return True` after `pg_notify`.
  - `_process_claimed_job`: success block per §1.4 (completed flag; not-counted warning; early return before `jobs_processed += 1`).

### 2.2 `app/worker.py` (745 → ~477)
- Imports: `from app.worker_job_rows import LostLeaseError, _JobRowLifecycle`; `from app.aac_encoder import MIXER_SAMPLE_RATE, _resample_to_mixer_rate, encode_aac, get_audio_duration` (replacing the 2-name import); delete `from datetime import datetime, timedelta, timezone` (its only users, `_claim_next_job` L355 + `_refresh_lease` L484, move — grep-verified; ruff will flag if anything else regresses). `json`/`signal`/`uuid`/`np`/`asyncpg`/`ThreadPoolExecutor`/cleanup imports stay.
- Delete the moved block (§1.6 A) and the `_resample_to_mixer_rate`/`MIXER_SAMPLE_RATE` block.
- `class GeneratorWorker(_JobRowLifecycle):`
- NEW after `LostLeaseError` import site:
  ```python
  class GenerationIoTimeout(RuntimeError):
      """FU-3: a pipeline-internal I/O timeout (upload/socket) re-wrapped at
      the _generate_and_upload boundary. On py3.11+ builtin TimeoutError
      aliases asyncio.TimeoutError, so without this re-wrap such a failure
      would be caught by _generate_with_lease's asyncio.TimeoutError handler
      and fed to the REL-03 breaker (an I/O failure is not an abandoned-
      thread stall)."""
  ```
- `_generate_and_upload` → wrapper + `_run_generation_pipeline` (body verbatim) per §1.5.1; alias note at the `except asyncio.TimeoutError` site in `_generate_with_lease` (§1.5.2).
- `health_check()`: `"consecutive_generation_timeouts": self.consecutive_generation_timeouts,` in BOTH branches.
- `get_stats()`: unchanged.

### 2.3 `app/aac_encoder.py` (201 → ~225)
- `MIXER_SAMPLE_RATE = 44100` constant (with the REL-25a chain comment moved verbatim from worker.py) near `FFMPEG_TIMEOUT`.
- `_resample_to_mixer_rate` moved verbatim after `_normalize_decoded_audio` (lazy `scipy.signal` import + docstring unchanged).

### 2.4 `app/worker_routes.py`
- Docstring only: add `consecutive_generation_timeouts` to the /health bullet list (payload is a passthrough dict — no code change).

### 2.5 Docs (pipeline "docs" stage)
- `docs/reliability_audit.md` REL-03 status paragraph: append FU-3 note (counter in /health; branch pinned; py3.11 narrowing landed, not just noted; split).
- `refactor/plans/rel-remediation-plan.md`: FU-3 status row → landed (commit at merge); annotate the "rel-03 (P2, report-only)" bullet with "(DONE in FU-3: …)" covering health/branch-pin/py3.11/split, and the "rel-13 (from rel-24 review)" bullet with "(refresh-lease scoping + 0-rowcount counting DONE in FU-3; encode-window residual remains)" — the FU-2 annotation style.
- `CLAUDE.md`: `app/worker.py` row notes the split (`job-row lifecycle in worker_job_rows.py`); NEW row for `app/worker_job_rows.py`; `app/aac_encoder.py` row mentions the 44.1 kHz normalization helper; "Upload guard & audio normalization (rel-24/25-worker)" section gains one FU-3 line (health counter + worker_id-scoped lease refresh + narrowed breaker catch).

---

## 3. TDD regression tests (write first; confirm expected red/green, then implement)

### 3.1 NEW `tests/test_worker_fu3.py` (~210 lines) — the unit's acceptance suite
Harness mirrors `tests/test_worker_correctness.py`: session-scoped `worker_module` fixture stubbing `app.framework.framework_generator` before importing `app.worker` (torch-less; the suite's 3.12 venv makes B6's alias live); `_pool_yielding`/`_make_conn`/`_make_worker(exit_hook=…)`/`_job`/`_run` helpers copied from that file (named fakes per AGENTS.md).

| # | Test | Pins | Core assertions |
|---|---|---|---|
| H1 | `test_health_carries_breaker_counter_zero_then_after_one_timeout` | FU-3 item 1 | fresh worker + db/garage mocks → `health_check()["consecutive_generation_timeouts"] == 0` (healthy branch); drive ONE timeout via `_generate_with_lease` (`GENERATION_TIMEOUT_SECONDS`→0.05, sleeping `generate_stem`, `pytest.raises(TimeoutError)`) → `== 1`; unhealthy branch (conn.fetchval raises) also carries `== 1`. |
| B5 | `test_timeout_fail_timeout_still_trips_breaker` | FU-3 item 2 (rel-03 branch pin; **green today by design — characterization pin**) | seq via `_process_claimed_job`: sleep→timeout (counter 1, `exit_calls == []`) → `generate_stem` raises `RuntimeError("cuda oom")` (jobs_failed 1, counter STILL 1) → sleep→timeout (counter 2, `exit_calls == [1]`); failure SQL messages: 2×"exceeded" + 1×"cuda oom"; total `jobs_failed == 3`. |
| B6 | `test_builtin_timeout_from_pipeline_does_not_feed_breaker` | FU-3 item 5 (**RED on 3.12**) | fast `generate_stem` returns `(zeros, 44100)`; `worker.garage.put_object = AsyncMock(side_effect=TimeoutError("upload timed out"))`; lease held (default owned-processing fetchrow); drive `_process_claimed_job` → `consecutive_generation_timeouts == 0`, `exit_calls == []`, `jobs_failed == 1`, mark-failed SQL args carry "upload timed out" (NOT "exceeded"). Docstring notes: on 3.10 the bare builtin already misses the asyncio handler — the pin is 3.11+-live. |
| R1 | `test_refresh_lease_scoped_to_worker_id` | FU-3 item 3 (**RED**) | `_run(worker._refresh_lease(uuid.uuid4()))` on fake pool; `conn.execute.call_args[0]` → sql contains `worker_id = $3` and `status = 'processing'`; params `(lease_expiry, job_id, "vram-worker")` in order; `lease_expiry.tzinfo` not None. |
| C1 | `test_lost_lease_completion_not_counted_and_logged` | FU-3 item 4 (**RED**) | real `_mark_job_complete`; `_generate_with_lease` mocked → `("audio/x.aac", 1.0)`; `conn.execute` side_effect `["UPDATE 0"]` → `jobs_processed == 0`, `jobs_failed == 0`, `caplog` (WARNING) contains "not counted as processed"; control run side_effect `["UPDATE 1", "UPDATE 1"]` → `jobs_processed == 1`. |
| S1 | `test_worker_split_files_under_500_lines` | FU-3 item 6 (**RED** — file missing) | FU-2 S1 pattern: both `app/worker.py` and `app/worker_job_rows.py` exist and `len(read_text().splitlines()) < 500`; plus `issubclass(GeneratorWorker, _JobRowLifecycle)` via `worker_module._JobRowLifecycle` re-export check. |

### 3.2 Keep-green set (must pass UNTOUCHED; run explicitly after each stage)
`tests/test_worker.py` (claim/complete/cleanup/health/stats — function-body imports) · `tests/test_worker_vram.py` (B1–B4/P/L/F/E — the module-attr patch center of gravity) · `tests/test_worker_correctness.py` (L1–L7/S1–S5 — `_generate_and_upload` contract incl. S5's `worker_module._resample_to_mixer_rate`) · `tests/test_queue_lease_and_dedup.py` (B2/C2/B6-timeout, `asyncio.TimeoutError` raise on the 3.12 alias) · `tests/test_adversarial_wave2.py` (ASYNC-2 pools, DATA-4 guards) · `tests/test_round3_fix_e.py` (E1 delete-guard) · `tests/test_worker_fetch_audio.py` · `tests/test_io_timeouts.py` (aac_encoder touched — S3-timeout pins intact) · `tests/test_generator.py` · `SOAK=1 .venv/bin/python -m pytest -m soak` (P4/P5 touch the split surface).

### 3.3 TDD order
1. Write `tests/test_worker_fu3.py` → run → **H1/B6/R1/C1 red** (no health key; alias feeds breaker; no `$3` scope; counted processed), **B5 green** (documented pin), **S1 red** (`worker_job_rows.py` missing).
2. **Split first, no behavior change** (§2.1 move + §2.2 deletions + §2.3 relocation): full keep-green set green with ZERO test edits → S1 green, H1/B6/R1/C1 still red. (This stage alone proves the patch-point inventory; any failure here means the inventory is wrong — stop and fix the split, not the tests.)
3. Behavior edits, one green each: health field (H1) → `_refresh_lease` SQL (R1) → `_mark_job_complete` bool + caller (C1) → `GenerationIoTimeout` narrowing (B6).
4. Full gate: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q` → **1197 + 6 new passed / 26 skipped**; then `SOAK=1 … -m soak` → 9p/1s.
5. Docs (§2.5).

---

## 4. Invariant compliance (plan §Invariants)

| # | How respected |
|---|---|
| 1 — lock discipline | No `state.lock`/`sync_lock` sections exist or are touched (worker is a separate process/adapter). |
| 2 — style / hexagonal | Both files <500 pinned by S1 (aac_encoder ~225); new code 4–20 lines with WHY docstrings; no `Any`; names grep-unique before landing (`worker_job_rows` 0 hits, `GenerationIoTimeout` 0 hits, `_JobRowLifecycle` 0 hits); deps still ctor-injected (`exit_hook` unchanged); ports untouched; pure-move discipline for the split (FU-2 precedent). |
| 3 — audio thread | Zero mixer/audio-thread changes. `aac_encoder` gains a pure function on its existing import edge (already imported by worker and framework code). |
| 4 — LLM capture | Untouched. |
| 5 — worker restart semantics | **Affirmed:** breaker trip condition is unchanged for genuine stalls (B1/B4/P5 keep-green + new B5 pin); the §1.5 narrowing only REMOVES a false-feed path (I/O timeouts become ordinary failures). No change to `os._exit`/compose recovery. |
| 6 — regression per fix | H1 pins item 1; B5 pins item 2; B6 pins item 5; R1 pins item 3; C1 pins item 4; S1 pins item 6; §3.2 guards every neighboring seam. |

---

## 5. Acceptance checklist (maps to the FU-3 row)

- [ ] `consecutive_generation_timeouts` visible in worker `/health` (healthy + unhealthy) → **H1**
- [ ] breaker `timeout → fail → timeout` branch pinned → **B5** (green-pin, semantics of record unchanged)
- [ ] `_refresh_lease` scoped to `worker_id` (SQL + params) → **R1**
- [ ] lost-lease completion not counted processed + logged → **C1**
- [ ] py3.11 alias: pipeline `TimeoutError` re-wrapped, breaker fed only by `wait_for`'s deadline, notes at both sites → **B6** (+ §1.5 comments)
- [ ] `worker.py` + new module both <500 LOC, split survives every patch point, suite green untouched → **S1** + §3.2
- [ ] `ruff` clean; full gate 1197 + 6 new passed / 26 skipped; soak 9p/1s
- [ ] Audit status + remediation-plan follow-up annotations + CLAUDE.md rows updated (§2.5)

## 6. Risks / out of scope

- **Split regression risk** is the unit's main hazard → mitigated by ordering (split lands alone, stage 2, with the full keep-green set required green and zero test edits) and the §1.6 E inventory; disclosed cosmetic deltas: logger name for moved methods, `LostLeaseError.__module__`.
- **B6 is only red on ≥3.11** (the dev venv is 3.12; the container pins 3.10 today). If the container image moves to ≥3.11 this pin becomes the tripwire that matters — that is precisely the audit note's scenario. No version pin change in this unit.
- **`_refresh_lease` untestable against real PG in unit tests** — pinned on SQL text + param order (typo-class integration risk accepted, same as the existing claim/complete SQL tests).
- **`error_message` text change** for pipeline I/O timeouts ("upload timed out" → "generation pipeline I/O timeout: upload timed out") — no test pins the old text; disclosed.
- **`app/routes/worker_routes.py` is dead code** (imports symbols missing from `app/routes/schemas.py`; imported by nothing). NOT removed here (out of FU-3's letter); flagged for the next hygiene FU.
- Headroom after split is ~23 lines in `worker.py`; further growth re-trips S1 by design.
- Out of scope: `_mark_job_failed` counting semantics (FU-3 letter covers complete only), claim-SQL policy (rel-12 frozen), encode-window lost-lease race residual (rel-24 review, disclosed), `asyncio` connection-loss callback, worker-container Python bump.

## 7. Commit sequence (one branch `rel-fu-3-worker`, Conventional Commits, land on `main` via parent)

1. `test(rel-fu-3): worker health breaker counter, breaker fail-branch pin, lease-refresh scoping, lost-lease counting, py3.11 timeout narrowing, split LOC pins — TDD red`
2. `fix(rel-fu-3): breaker counter in /health; worker_id-scoped lease refresh; 0-rowcount completions uncounted+logged; pipeline TimeoutErrors re-wrapped off the breaker; worker job-row lifecycle split under 500`
3. `docs(rel-fu-3): audit/CLAUDE/plan status`
