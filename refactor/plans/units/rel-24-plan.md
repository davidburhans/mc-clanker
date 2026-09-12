# PLAN — Unit 13 `rel-worker-correctness` (REL-24/REL-25), branch `rel-24-25-worker`

**Spec:** `refactor/plans/rel-remediation-plan.md` §Unit specs → U13 (lines 202-210) · `docs/reliability_audit.md` REL-24 + REL-25 (P2 table, lines 468-469) · scout notes `rel-24-25 recon` (context.md, call_e49df838).
**Baseline gate at `1a5782d` (HEAD of `main`, U12 landed):** `.venv/bin/python -m pytest tests/ -q` → **1147 passed / 16 skipped** (remediation-plan status table, U12 row) · `.venv/bin/python -m ruff check app tests` → clean. Do not regress skips.

Verified against code at HEAD (line refs current):
`app/worker.py` 635 L (`_process_claimed_job` :191-219 — generic except → `_mark_job_failed` + `jobs_failed`, DB-complete-failure branch calls `_still_own_job_row` :207; `_still_own_job_row` :221-245 — returns **True on row-gone** (delete-safety semantics, pinned by `tests/test_round3_fix_e.py:186,190`), False on read-error/other-owner, True when `self.db is None`; `_claim_next_job` :247-288 `SELECT *` + `FOR UPDATE SKIP LOCKED`; `_generate_with_lease` :290-327 (private pool + heartbeat + `wait_for`; TimeoutError → breaker, counter reset only in the `else` branch); `_heartbeat_loop` :391 / `_refresh_lease` :400-413 — refreshes `WHERE id=$2 AND status='processing'`, **not worker_id-scoped**; `_generate_and_upload` :415-452 — generate (no cfg/steps kwargs) → `encode_aac(..., 44100)` :441 → `audio_path = f"audio/{job['id']}.aac"` :443 → **unguarded** `put_object` :445 → `get_audio_duration(..., 44100)` :448; `_mark_job_complete` :462-494 and `_mark_job_failed` :496-518 — both guarded `WHERE status='processing' AND worker_id=$n`) · `app/framework/framework_generator.py` 490 L (`generate_stem` :436-485 — signature already takes `cfg_scale=7.0, steps=50`, computes `results, sample_rate = self.generate_batch(...)` :481 and **drops the sr**; `generate_batch` :257-266 returns the tuple; `_generate_batch_locked` :268-303; `registry.sample_rate` property :44-49 default 44100) · `app/framework/loop_orchestrator.py` 507 L (`_submit_job` delegate :349-388, REL-18 streak bookkeeping) · `app/framework/loop_steps.py` 1116 L (`_submit_job` protocol stub :330-346; `_step_submit_jobs` :628-672, per-stem `_submit_job` call :659; state import present; `_step_build_prompt_state` captures bpm/key locals under `state.lock`) · `app/framework/pregeneration.py` 197 L (imports `_collect_uncached_stems` etc. from `loop_steps` :29-36; `loop._submit_job` call :117) · `app/framework/job_queue.py` 217 L (`submit_generator_job` :31-78 SQLAlchemy INSERT, no cfg/steps; `PostgresJobQueueAdapter.submit` :164-193 pass-through) · `app/framework/ports.py` (`JobQueuePort.submit` protocol :87-101) · `app/models/generator_job.py` 165 L (no cfg/steps columns; `to_dict` :141-164; TIMESTAMPTZ/JSONB-variant/UUID-pg conventions) · `app/routes/jobs.py` 327 L (second INSERT path :86-113) · `app/routes/schemas.py` (`JobSubmission` :107-120 — no cfg/steps; `GenerationConfig` :70-74 with SEC-1 bounds `cfg_scale ge=0.0 le=20.0`, `steps ge=1 le=100`) · `app/routes/config.py` (:248-264 — GET/POST generation-config touch `state` only) · `app/framework/audio_fetch.py` (:47-51 — `decode_aac(aac_bytes, sample_rate=44100)`; `decode_aac` **raises** on sr mismatch, `aac_encoder.py:169-173`) · `app/framework/framework_mixer.py` (`Mixer(sample_rate=44100)` :30, no resampler) · `app/youtube_relay.py:71` + `app/stream_fanout_args.py:22` (both 44100) · `migrations/002_lease_and_reconciliation.sql` (idempotent `DO $$ IF NOT EXISTS … ADD COLUMN` pattern + DOWN section; next free number 004) · `pyproject.toml:43` `scipy>=1.12.0` core dep (only `scipy.io.wavfile` used today; `scipy.signal` unused, available) · tests: `tests/test_worker.py` (7 tests, whole module skips when torch absent — none touch `_generate_and_upload`/`put_object`/cfg-steps) · torch-less worker harness `tests/test_queue_lease_and_dedup.py:36-57` (`worker_module` fixture stubbing `app.framework.framework_generator`) reused by `test_worker_vram.py` (`_make_worker` :180, `_job` :198, `_run` :211) and `test_round3_fix_e.py` (`FakeConnection`/`FakeGarage`/`_pool`) · `tests/test_adversarial_wave2.py:192-225` (`test_generate_and_upload_runs_generation_in_private_pool` — fakes `generate_stem` returning a **bare array**) · `tests/test_worker_vram.py:328,333` (B3 success-path fake returns bare array) · `tests/test_cache_key.py` (pins `make_cache_key(model_id, prompt, bpm, key, bars)` frozen format; source-scan forbids inline f-string keys) · submit-path fakes all use `async def submit(self, **kwargs)` (`test_job_queue_lifecycle.py:108`, `test_jobs_await_injection.py:47`, `test_jobs_injection.py:39`, `test_loop_robustness.py:121`) — tolerant of added kwargs · one direct `_submit_job(...)` call in tests (`test_framework_characterization.py:487`, omits new kwargs → safe if defaults provided).

`generate_stem` production callers (grep): `app/worker.py:431` only. No migration runner exists (manual `psql -f`, per 002's header); `Base.metadata.create_all()` covers fresh dev DBs.

No compose change, no new env vars, no new ports, no new dependencies.

---

## 0. Scope summary

| Item | Root cause | Fix site |
|---|---|---|
| REL-24 (zombie upload) | `_generate_and_upload` uploads at the deterministic key `audio/{job_id}.aac` with no ownership check between generation and upload; a worker whose lease lapsed mid-generation (heartbeat dead ≥ 600 s, event-loop stall) can overwrite the reclaiming owner's completed audio or orphan an unreferenced object | `app/worker.py` `_generate_and_upload` + new `_lease_still_held` + `LostLeaseError` handling in `_process_claimed_job` |
| REL-25a (sample rate dropped) | `generate_stem` discards the engine sr; worker hard-codes `encode_aac(..., 44100)` / `get_audio_duration(..., 44100)` — a non-44.1 kHz engine produces AAC the fetch path **rejects** (`decode_aac` raises on sr mismatch) and wrong durations | `framework_generator.generate_stem` → returns `(array, sr)`; worker resamples once to `MIXER_SAMPLE_RATE = 44100` (scipy `resample_poly`) and encodes/dures at that rate |
| REL-25b (config-UI no-op) | `state.generation_cfg_scale/steps` are written by `POST /api/generation-config` but nothing reads them: no submit-path kwargs, no job-row columns, no worker read | `migrations/004_generation_params.sql` + `GeneratorJob` columns + submit chain (`ports.py` → `job_queue.py` → `_submit_job` delegates → both callers) + `JobSubmission` schema + worker reads |

Untouched: mixer, fan-out, YouTube relay, stem_cache/cache key, claim SQL, lease/heartbeat SQL, `_mark_job_complete`/`_mark_job_failed` guards, audit paths, `JobWaiter`, cleanup.

---

## 1. Design decisions (documented reasoning; deviations flagged)

1. **REL-24 check placement: after generation, before `encode_aac`** (`worker.py` between :439 and :441). The dangerous window is generation (5–30 s documented; the heartbeat can be dead across it after an event-loop stall, letting the lease lapse and the row be reclaimed). Checking before the encode also avoids burning encode CPU on an already-lost job. The audit's "before upload" is satisfied; residual: a reclaim landing *during* encode+upload (~1 s) still races — accepted, because (a) the window shrinks from the full generation span to ~1 s and (b) `_mark_job_complete`'s `WHERE status='processing' AND worker_id=$n` guard already makes the ROW unclobberable; the row is the source of truth for cleanup. A second check right before `put_object` was considered and rejected — one predicate seam, one spelling, diminishing returns.
2. **New strict predicate `_lease_still_held`, NOT a reuse of `_still_own_job_row`.** The existing method returns **True when the row is gone** — correct for its delete-orphan purpose ("no competing owner may be harmed by our delete") but WRONG for the upload guard: with the row deleted, uploading orphans an unreferenced Garage object forever (cleanup deletes objects via rows, so nothing can ever find it). `_lease_still_held` requires row exists ∧ `status == 'processing'` ∧ `worker_id == ours`, returns False on read error (cannot prove ownership → must not write). Both predicates share one extracted `_read_job_ownership` helper (same `SELECT status, worker_id …`); `_still_own_job_row`'s pinned behavior (`test_round3_fix_e.py:186-190`: row-gone→True, read-error→False) is preserved byte-for-byte. `self.db is None` → True in both (no competing owner possible; matches existing test harnesses that leave `db` unset).
3. **Lapse semantics: raise a module-level `LostLeaseError(RuntimeError)`; `_process_claimed_job` catches it BEFORE the generic handler and returns early** — logs a warning naming the job and the current owner (from the recheck row when available), does NOT upload, does NOT call `_mark_job_failed` (it would be an ownership-guarded no-op plus a misleading "lost lease" warning line), does NOT increment `jobs_failed` (the job isn't failing — it continues under its new owner). "Delete the local temp file" from the generic lease-hygiene playbook degenerates here: there is no persistent temp file today (`encode_aac` writes/unlinks its own temp WAV; AAC bytes live in memory and are dropped when the coroutine unwinds) — skipping encode+upload IS the cleanup. The distinct exception type (vs a bare `RuntimeError`) is required so the generic `except Exception` path (which would mark-fail + count) cannot swallow it by accident.
4. **`LostLeaseError` does not reset the REL-03 breaker counter.** `_generate_with_lease` resets `consecutive_generation_timeouts` only in its `else` branch (a fully completed pipeline). A lost-lease path completed *generation* but skipped encode/upload, and — more importantly — a lease loss usually means this worker's event loop stalled ≥ 600 s, which is exactly the wedged-process shape the breaker exists to catch. Conservative: counter untouched, error propagates through `wait_for` unchanged (it is not a `TimeoutError`). Pinned by test.
5. **REL-25a decision: resample ONCE, worker-side, to `MIXER_SAMPLE_RATE = 44100`** — chosen over "thread the native sr through and resample at fetch". Evidence (scout-verified): the entire playback chain assumes 44.1 kHz and nothing anywhere resamples — `GarageAudioAdapter.fetch` decodes with hard-coded `decode_aac(aac_bytes, sample_rate=44100)` and `decode_aac` **raises** when the container's sr differs (`aac_encoder.py:169-173`); `Mixer(sample_rate=44100)` does all timing math on that rate; the stream fanout (`stream_fanout_args.py:22`) and the YouTube relay (`youtube_relay.py:71`) are hard-coded 44100. Threading native sr would mean changing fetch + mixer + fanout + relay + the stem_cache contract (cached arrays are consumed by the mixer at 44.1 kHz) — a four-module blast radius for zero audible benefit. Worker-side normalization is one seam, keeps every downstream assumption true, and makes the stored object self-consistent (`decode_aac@44100` succeeds). `scipy.signal.resample_poly(audio, 44100, sr, axis=0)` — scipy ≥1.12 is a core dep (`pyproject.toml:43`), reduces the fraction internally (48000→44100 = 147/160), handles mono/stereo via `axis=0`. A no-op fast path (`sr == 44100 → return audio unchanged`, zero copy) keeps the today-common case byte-identical (Foundation-1 runs 44.1 kHz).
6. **`generate_stem` returns `(results[0], sample_rate)`** — the sr is already computed at `:481` and dropped; `generate_batch` already returns the tuple, so the wrapper just stops discarding it. Grep-verified caller inventory: production = `app/worker.py:431` only; tests with bare-array fakes that must be updated in the same commit: `tests/test_worker_vram.py:328` (B3 success-path fake) and `tests/test_adversarial_wave2.py:206-215` (`record_thread` returns `fake_audio`). Docstring updated to the tuple return with one usage example (AGENTS.md). A `sample_rate is None` guard in the worker (`sr or MIXER_SAMPLE_RATE`) covers the degenerate empty-batch shape.
7. **Duration from the array/rate actually encoded:** post-resample array at `MIXER_SAMPLE_RATE` (mathematically identical to pre-resample `len/native_sr` modulo resample rounding; using the encoded pair guarantees `duration_seconds` matches the stored object exactly).
8. **REL-25b storage: two NULLABLE columns** `cfg_scale DOUBLE PRECISION`, `steps INTEGER` — NULL means "legacy row / submitter omitted" and the worker falls back. Migration `migrations/004_generation_params.sql` follows 002's idempotent `DO $$ IF NOT EXISTS (information_schema.columns …) ADD COLUMN` blocks + a commented DOWN section; ORM gets the same two `nullable=True` columns + `to_dict` entries so `Base.metadata.create_all()` parity holds (the CLAUDE.md `003_llm_capture_additive.sql` precedent: existing PG deployments only gain columns via the migration). No index (never queried by these dims).
9. **Worker fallback uses explicit `None` checks, not `or`:** `job.get("cfg_scale")` — `0.0` is a *legal* value (`GenerationConfig` bounds are `ge=0.0`), so `x or 7.0` would silently coerce it. Fallback constants `DEFAULT_CFG_SCALE = 7.0` / `DEFAULT_STEPS = 50` in `worker.py`, commented as mirroring `generate_stem`'s signature defaults and `GlobalState`'s initial values (that triple spelling already exists today; one comment, no refactor).
10. **Submit chain: keyword params WITH defaults (`cfg_scale: float | None = None, steps: int | None = None`) end-to-end** — `JobQueuePort.submit` protocol → `PostgresJobQueueAdapter.submit` → `submit_generator_job` → both `_submit_job` delegates → the two production callers. Defaults keep every existing seam green: all four test fakes take `**kwargs`; the one direct call (`test_framework_characterization.py:487`) omits them. `None` propagates to a NULL column (API submitters that omit stay byte-identical with old rows); the two loop callers always pass concrete values read from state.
11. **cfg/steps are read at submit time, once per submit phase, under `state.lock` via one shared helper** — new `async def read_generation_params() -> tuple[float, int]` in `loop_steps.py` (short lock section, two attribute reads, no I/O — invariant 1), imported by `pregeneration.py` (which already imports from `loop_steps`). Chosen over adding the pair to the pregen `state_snapshot` dict: fresher (a config change takes effect on the next submitted job, not one loop later), one source of truth for both submit paths, and it leaves the snapshot shape (pinned by characterization tests) untouched.
12. **`JobSubmission` gets the same SEC-1 bounds as `GenerationConfig`** (`cfg_scale: float | None = Field(default=None, ge=0.0, le=20.0)`, `steps: int | None = Field(default=None, ge=1, le=100)`) — the API submit path must not become the unbounded backdoor the config route closed (schemas.py:71-74 comment names exactly this hazard). Loop-side values need no re-validation: they can only come from `state`, whose only writers are the bounded config route and the bounded defaults.
13. **Cache key deliberately NOT extended with cfg/steps** (`make_cache_key(model_id, prompt, bpm, key, bars)` stays frozen — pinned format in `test_cache_key.py`; adding dims would invalidate every cached stem AND is a separate product decision). Accepted semantic, documented: a stem already in `stem_cache` ignores later cfg/steps changes until TTL eviction — the same staleness class the cache already has for prompt-family dims. No source-scan violation (no inline key f-strings introduced).
14. **`_refresh_lease` worker_id scoping is OUT of scope** (scout residual: a zombie heartbeat can refresh a reclaimed row's lease because the UPDATE is not worker_id-scoped). One-line hardening candidate, but the audit's REL-24 fix is the upload recheck and the lease SQL is pinned by `tests/test_job_queue_lifecycle.py`; noted as a follow-up, not smuggled in.
15. **Line budget:** `worker.py` is 635 L (pre-existing >500 debt, flagged at rel-03). This unit adds ~35 net lines (error class, two predicates minus the extracted helper dedup, resample fn, kwargs) → ~670 L; the shared `_read_job_ownership` extraction keeps the two predicates from duplicating the SELECT. Split of worker.py stays deferred (follow-ups list). `loop_steps.py` 1116→~1125, `pregeneration.py` ~+3, `job_queue.py` ~+8, `ports.py` ~+4, `framework_generator.py` ~+3, `routes/jobs.py`/`schemas.py` ~+8, `models/generator_job.py` ~+6.

---

## 2. Exact changes per file

### 2.1 `app/worker.py` (635 → ~670 lines)

Imports: add `import numpy as np` and `from scipy.signal import resample_poly` beside `asyncpg` (scipy/numpy are core deps; module-top so `worker_module`-fixture tests can string-patch). Constants near `VRAM_EVICTION_TIMEOUT_SECONDS` (:66):

```python
# REL-25a: the entire playback chain assumes 44.1 kHz and nothing downstream
# resamples — GarageAudioAdapter.fetch decodes with decode_aac(sample_rate=44100)
# (which RAISES on mismatch), the Mixer, the MP3 fan-out and the YouTube relay
# are all hard-coded 44100. Engine output is therefore normalized ONCE, here.
MIXER_SAMPLE_RATE = 44100
# Worker-side fallbacks for rows predating the cfg/steps columns (REL-25b);
# mirror generate_stem()'s signature defaults and GlobalState's initial values.
DEFAULT_CFG_SCALE = 7.0
DEFAULT_STEPS = 50
```

Module-level (after the constants, before `WorkerConfig`):

```python
class LostLeaseError(RuntimeError):
    """REL-24: the processing lease was lost mid-generation (row reclaimed,
    reaped or deleted). The Garage key is deterministic (audio/{job_id}.aac),
    so uploading would overwrite the new owner's completed audio or orphan an
    unreferenced object — the caller must skip upload and stand down."""


def _resample_to_mixer_rate(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """REL-25a: normalize engine output to the 44.1 kHz playback chain.

    No-op (same object) when already at the mixer rate — today's common case.
    Usage: ``pcm = _resample_to_mixer_rate(generate_stem(...)[0], sr)``
    """
    if sample_rate == MIXER_SAMPLE_RATE or sample_rate is None:
        return audio
    return resample_poly(audio, MIXER_SAMPLE_RATE, sample_rate, axis=0).astype(np.float32)
```

Ownership helpers — extract the shared SELECT and add the strict predicate (keeping `_still_own_job_row` behavior identical, :221-245):

```python
    async def _read_job_ownership(self, job_id: uuid.UUID):
        """SELECT (status, worker_id) for a job row; None when the row is gone.

        Raises on read failure — each caller applies its own conservative policy.
        """
        assert self.db is not None
        async with self.db.acquire() as conn:
            return await conn.fetchrow(
                "SELECT status, worker_id FROM generator_jobs WHERE id = $1", job_id
            )

    # _still_own_job_row: body now calls _read_job_ownership inside its existing
    # try/except; True-on-row-gone / False-on-error semantics unchanged (pinned
    # by tests/test_round3_fix_e.py E1).

    async def _lease_still_held(self, job_id: uuid.UUID) -> bool:
        """REL-24 upload-guard predicate: strictly 'we still hold the lease'.

        Unlike _still_own_job_row (delete-safety: a GONE row means no competing
        owner), a gone row here means there is no job left to complete — the
        upload would orphan an unreferenced object. False on read error too:
        unprovable ownership must never translate into a write.
        """
        if self.db is None:
            return True  # no competing owner possible (matches test harnesses)
        try:
            row = await self._read_job_ownership(job_id)
        except Exception as e:  # noqa: BLE001 - cannot prove ownership -> skip upload
            logger.warning("Could not verify lease for job %s: %s", job_id, e)
            return False
        return (
            row is not None
            and row["status"] == "processing"
            and row["worker_id"] == self.config.worker_id
        )
```

`_generate_and_upload` (:415-452) — new body core:

```python
        loop = asyncio.get_running_loop()
        audio_array, sample_rate = await loop.run_in_executor(
            gen_pool,
            lambda: self.generators.generate_stem(
                model_id=job["model_id"],
                prompt=job["prompt"],
                key=job.get("key") or "",
                bpm=job.get("bpm") or 120,
                bars=job.get("bars", 4),
                # REL-25b: NULL/absent on pre-migration rows -> defaults
                cfg_scale=job.get("cfg_scale") if job.get("cfg_scale") is not None else DEFAULT_CFG_SCALE,
                steps=job.get("steps") if job.get("steps") is not None else DEFAULT_STEPS,
            ),
        )
        # REL-24: generation is the long window (5-30 s) across which the lease
        # can lapse (heartbeat dead after an event-loop stall) and the row be
        # reclaimed. Re-verify BEFORE spending encode CPU or writing the
        # deterministic Garage key; _mark_job_complete's guarded UPDATE cannot
        # save the OBJECT once clobbered.
        if not await self._lease_still_held(job["id"]):
            logger.warning(
                "Job %s: lease lost during generation; skipping upload (row no longer ours)",
                job["id"],
            )
            raise LostLeaseError(f"job {job['id']} reclaimed mid-generation")

        pcm = await loop.run_in_executor(
            None, lambda: _resample_to_mixer_rate(audio_array, sample_rate)
        )
        aac_bytes = await loop.run_in_executor(None, lambda: encode_aac(pcm, sample_rate=MIXER_SAMPLE_RATE))
        audio_path = f"audio/{job['id']}.aac"
        await self.garage.put_object(audio_path, aac_bytes)
        duration = get_audio_duration(pcm, sample_rate=MIXER_SAMPLE_RATE)
        return audio_path, duration
```

`_process_claimed_job` (:191-219) — add the specific catch before the generic one:

```python
        try:
            audio_path, duration = await self._generate_with_lease(job)
        except LostLeaseError:
            # REL-24: not this job's failure — it continues under its new owner.
            # No upload happened, so there is no temp/orphan to clean; do not
            # mark-fail (ownership-guarded no-op anyway) and do not count it.
            logger.warning("Job %s stood down: lease lost, new owner active", job["id"])
            return
        except Exception as e:  # noqa: BLE001 - generation/upload failed
            ...  # existing body unchanged
```

### 2.2 `app/framework/framework_generator.py` (490 → ~493 lines)

`generate_stem` (:436-485): return annotation `-> tuple[np.ndarray, int]`; final line `return results[0], sample_rate`; docstring "Returns:" section updated to the tuple with a one-line worker usage example; note in the docstring that the sr is the ENGINE's native rate and callers feeding the 44.1 kHz playback chain normalize once (worker-side, REL-25a). Signature order unchanged (`cfg_scale`/`steps` already exist).

### 2.3 NEW `migrations/004_generation_params.sql` (~35 lines)

Header comment (REL-25b; companion to the `GeneratorJob` column additions; idempotent; apply with `psql "$DATABASE_URL" -f migrations/004_generation_params.sql`), then two blocks mirroring 002:

```sql
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'generator_jobs' AND column_name = 'cfg_scale'
    ) THEN
        ALTER TABLE generator_jobs ADD COLUMN cfg_scale DOUBLE PRECISION;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'generator_jobs' AND column_name = 'steps'
    ) THEN
        ALTER TABLE generator_jobs ADD COLUMN steps INTEGER;
    END IF;
END $$;
```

NULLable by design: rows predating REL-25b (and API submitters that omit the fields) stay valid; the worker falls back to `DEFAULT_CFG_SCALE`/`DEFAULT_STEPS`. DOWN section: `ALTER TABLE generator_jobs DROP COLUMN IF EXISTS cfg_scale; DROP COLUMN IF EXISTS steps;`.

### 2.4 `app/models/generator_job.py` (165 → ~171 lines)

In the Job-spec column block (after `bars`, :93):

```python
    # REL-25b: diffusion params captured at submit so the config UI reaches the
    # worker. Nullable = pre-migration rows / submitters that omit; worker falls
    # back to generate_stem's defaults (worker.py DEFAULT_CFG_SCALE/DEFAULT_STEPS).
    cfg_scale = Column(Float, nullable=True)
    steps = Column(Integer, nullable=True)
```

(`Float`/`Integer` already imported.) `to_dict` gains `"cfg_scale": self.cfg_scale, "steps": self.steps` (after `"bars"`).

### 2.5 `app/framework/ports.py` (~+4 lines)

`JobQueuePort.submit` (:87-101): add `cfg_scale: float | None = None,` and `steps: int | None = None,` keyword params (before the `-> UUID`); docstring line: "Diffusion params captured at submit (REL-25b); None -> NULL column, worker falls back to defaults."

### 2.6 `app/framework/job_queue.py` (217 → ~225 lines)

`submit_generator_job` (:31-78): add the two keyword params (defaults `None`); pass to the `GeneratorJob(...)` constructor. `PostgresJobQueueAdapter.submit` (:164-193): add + forward the two params. (Bare-name delegation stays, so tests can still monkeypatch the module function.)

### 2.7 `app/framework/loop_orchestrator.py` (507 → ~511 lines)

`AsyncFrameworkLoop._submit_job` (:349-388): add the two keyword params to the signature; forward to `self._jobs.submit(...)` (inside the existing try so the REL-18 streak bookkeeping is untouched).

### 2.8 `app/framework/loop_steps.py` (1116 → ~1126 lines)

New module-level helper (near `_collect_uncached_stems`; `state` already imported):

```python
async def read_generation_params() -> tuple[float, int]:
    """Snapshot the DJ's cfg/steps for job submission (REL-25b).

    Short lock section, two attribute reads, no I/O (invariant 1). Both submit
    paths (P7 foreground + pregeneration) read here so a config change takes
    effect on the next submitted job — one spelling, one source of truth.
    """
    async with state.lock:
        return state.generation_cfg_scale, state.generation_steps
```

`_submit_job` protocol stub (:330-346): add the two keyword params. `_step_submit_jobs` (:628-672): `cfg_scale, steps = await read_generation_params()` before the per-stem loop (after the backpressure early-return, so a skipped cycle costs zero lock takes); pass both into the `self._submit_job(...)` call (:659).

### 2.9 `app/framework/pregeneration.py` (197 → ~200 lines)

Import `read_generation_params` in the existing `loop_steps` import block (:29-36); call it once before the submit loop (mirroring P7's placement after its own backpressure gate); pass both into `loop._submit_job(...)` (:117).

### 2.10 `app/routes/schemas.py` + `app/routes/jobs.py` (~+8 lines)

`JobSubmission` (:107-120): add

```python
    # REL-25b: same SEC-1 bounds as GenerationConfig — the job-submit route must
    # not become the unbounded backdoor the config route closed.
    cfg_scale: float | None = Field(default=None, ge=0.0, le=20.0)
    steps: int | None = Field(default=None, ge=1, le=100)
```

`submit_job` INSERT (:97-111): pass `cfg_scale=job.cfg_scale, steps=job.steps` to `GeneratorJob(...)` (None → NULL column, old-row compatible).

### 2.11 Tests updated for the tuple return (same commit, red otherwise)

- `tests/test_worker_vram.py:328` — success-path fake `lambda **_kwargs: np.zeros((8, 2), dtype=np.float32)` → returns `(np.zeros((8, 2), dtype=np.float32), 44100)`.
- `tests/test_adversarial_wave2.py` `record_thread` (:206-215) — `return fake_audio` → `return fake_audio, 44100`.
- Timeout-path fakes (`time.sleep` lambdas) are unaffected (their return value is never unpacked — the coroutine is cancelled).

### 2.12 NEW `tests/test_worker_correctness.py` (~260 lines) — §3.1/§3.2

### 2.13 Submit-chain test additions — §3.3 (`tests/test_job_queue_params.py` new, + 2 tests in `tests/test_generator.py`)

### 2.14 Docs (docs stage, same unit)

- `docs/reliability_audit.md`: REL-24 + REL-25 rows → `**Status: fixed-in rel-24-25-worker**` + one-line mechanism each (post-generation `_lease_still_held` recheck → `LostLeaseError` stand-down; `(array, sr)` tuple + one-shot worker-side `resample_poly` to 44.1 kHz; nullable `cfg_scale`/`steps` columns threaded state → submit → row → worker).
- `refactor/plans/rel-remediation-plan.md`: status row 13 → landed.
- `CLAUDE.md`: testing table + `test_worker_correctness.py` / `test_job_queue_params.py` rows; `framework_generator.py` table row — note `generate_stem()` returns `(audio, sample_rate)`; worker section note: uploads are lease-guarded, output normalized to 44.1 kHz once.

---

## 3. TDD regression tests (write first, confirm red, then implement)

### 3.1 `tests/test_worker_correctness.py` — REL-24 (torch-less: `worker_module` fixture stubbing `app.framework.framework_generator`, pattern `test_queue_lease_and_dedup.py:36-57`; `_make_worker`/`_job`/`_run` helpers mirrored from `test_worker_vram.py`; `_pool_yielding(conn)` with `fetchrow` scripted per test; `worker.garage.put_object = AsyncMock()`; `worker_module.encode_aac`/`get_audio_duration` patched with recorders)

| # | Test | Pins | Core assertions |
|---|---|---|---|
| L1 | `test_lost_lease_after_generation_skips_upload` (**acceptance**) | REL-24 | fake `generate_stem` → `(zeros(8,2), 44100)`; ownership row `{"status": "completed", "worker_id": "worker-B"}` → `_generate_and_upload` raises `LostLeaseError`; `put_object` **not** awaited; patched `encode_aac` **not** called (recheck precedes encode — no wasted CPU) |
| L2 | `test_held_lease_uploads_after_generation` | REL-24 happy path | row `{"status": "processing", "worker_id": "vram-worker"}` → returns `(f"audio/{id}.aac", 1.0)`; `put_object` awaited once; `encode_aac` called with `sample_rate == 44100` |
| L3 | `test_gone_row_is_lost_for_upload_guard` | decision 2 | `fetchrow → None` → `LostLeaseError` (upload-guard ≠ delete-guard: row-gone must NOT upload) — the semantic that makes `_lease_still_held` a separate predicate |
| L4 | `test_ownership_read_error_skips_upload` | decision 2 | raising conn (fetchrow side_effect=OSError) → `LostLeaseError`, `put_object` not awaited |
| L5 | `test_lost_lease_stands_down_without_fail_or_count` (**acceptance**) | decision 3 | `_generate_with_lease = AsyncMock(side_effect=LostLeaseError(...))`; run `_process_claimed_job` → `_mark_job_failed` **not** awaited, `jobs_failed == 0`, `jobs_processed == 0`, garage untouched; control run (normal return) still marks complete |
| L6 | `test_lost_lease_does_not_reset_timeout_breaker_counter` | decision 4 | preset `consecutive_generation_timeouts = 1`; LostLeaseError through `_generate_with_lease` (fake `_generate_and_upload`) → counter still 1 (only a completed pipeline resets) |
| L7 | `test_still_own_job_row_semantics_unchanged` | refactor guard | mirror of `test_round3_fix_e.py:186-190` against the extracted `_read_job_ownership`: row-gone → True, read-error → False, other-owner → False (delete-guard byte-identical after the extraction) |

### 3.2 `tests/test_worker_correctness.py` — REL-25 worker side (same harness)

| # | Test | Pins | Core assertions |
|---|---|---|---|
| S1 | `test_job_cfg_steps_reach_generator` (**acceptance**) | REL-25b | job `{"cfg_scale": 9.5, "steps": 30, …}`; fake generators record kwargs → `generate_stem` called with `cfg_scale=9.5, steps=30` |
| S2 | `test_old_rows_fall_back_to_default_cfg_steps` (**acceptance**) | REL-25b back-compat | job without the keys AND job with `None` values → called with `cfg_scale=7.0, steps=50` (explicit-None check: also `cfg_scale=0.0` passes through as `0.0`, not coerced to 7.0) |
| S3 | `test_native_rate_output_resampled_once_to_mixer_rate` (**acceptance**) | REL-25a | fake `generate_stem` → `(sine(480 samples, stereo) @ 48000, 48000)`; recorder `encode_aac` sees `sample_rate == 44100` and `len(audio) == 441` (480·147/160); `put_object` once; `get_audio_duration` called with 44100 → returned duration ≈ 0.01 |
| S4 | `test_44100_output_passes_through_unresampled` | decision 5 fast path | `id(encoded_arg) == id(generated_array)` — no copy, no resample call (patch `resample_poly` recorder asserting 0 calls) |
| S5 | `test_resample_to_mixer_rate_pure_function` | REL-25a pure fn | `_resample_to_mixer_rate(x, 44100) is x`; `(x, 48000)` → length ratio 147/160, dtype float32, `np.isfinite(...).all()`; `(x, None)` → identity (degenerate-batch guard) |

### 3.3 REL-25b submit chain — NEW `tests/test_job_queue_params.py` (SQLite-real via `DatabaseManager` `init_db` fixture, `test_adversarial_wave2.py` pattern) + `tests/test_generator.py` additions (torch-gated, `_require_generator()`)

| # | Test | File | Pins | Core assertions |
|---|---|---|---|---|
| C1 | `test_submit_generator_job_persists_cfg_steps` | test_job_queue_params | REL-25b row | real session; `submit_generator_job(..., cfg_scale=8.5, steps=20)` → reloaded row `cfg_scale == 8.5`, `steps == 20` |
| C2 | `test_submit_omitted_cfg_steps_writes_null` | test_job_queue_params | old-row shape | no kwargs → row `cfg_scale is None`, `steps is None` (worker-fallback contract holds for API rows) |
| C3 | `test_step_submit_jobs_reads_state_generation_params` | test_job_queue_params | config-UI closure | set `state.generation_cfg_scale/steps = 8.5/20` under lock (test_reset_reprime.py:238 harness: `loop._submit_job = AsyncMock`, one uncached stem) → delegate awaited with `cfg_scale=8.5, steps=20`; backpressure-skipped cycle (`_queue_backlogged` True) → delegate not awaited (helper not on the skip path) |
| C4 | `test_pregeneration_passes_generation_params` | test_job_queue_params | background path | `run_pregeneration(loop, 2, snapshot)` with patched conductor (`{"action_type": "add", …}`) + `loop._submit_job = AsyncMock` → kwargs include state's cfg/steps |
| C5 | `test_api_jobs_post_persists_cfg_steps_and_rejects_out_of_range` | test_job_queue_params | SEC-1 parity | auth'd `POST /api/jobs` with `cfg_scale=9.0, steps=40` → 201, row carries both; `cfg_scale=99.0` → 422; `steps=0` → 422 |
| C6 | `test_config_ui_change_reaches_next_submitted_job` (**acceptance**) | test_job_queue_params | REL-25b end-to-end closure | `POST /api/generation-config {"cfg_scale": 8.5, "steps": 20}` (real route) → then C3's `_step_submit_jobs` harness → delegate kwargs exactly `(8.5, 20)` — the previously-silent no-op now observable at the submit seam |
| G1 | `test_generate_stem_returns_audio_and_engine_sample_rate` | test_generator | REL-25a tuple | fake engine `generate_batch → (["audio"], 44100)` → `generate_stem(...) == ("audio", 44100)` |
| G2 | `test_generate_stem_forwards_cfg_scale_and_steps` | test_generator | REL-25b forwarding | `patch.object(registry, "generate_batch")` recorder → called with `cfg_scale=9.5, steps=30` |

Red-first: L1/L3/L4/L5 (no recheck today → upload fires), L6 (counter reset today on any non-timeout completion path), S1/S2 (kwargs not passed today), S3 (44100 hard-coded today → encode sees 44100 but array NOT resampled: length assertion fails), S5 (function absent), C1/C3/C4/C5/C6 (kwargs not threaded today → TypeError on unexpected kwarg / row lacks values), G1 (returns bare array today). L2/L7, S4, C2, G2 pin preserved seams (expected green immediately).

---

## 4. Verification & rollout

```bash
# per concern, red -> green
.venv/bin/python -m pytest tests/test_worker_correctness.py -v
.venv/bin/python -m pytest tests/test_job_queue_params.py tests/test_generator.py -v
# no-regression gates (touched seams)
.venv/bin/python -m pytest tests/test_worker_vram.py tests/test_adversarial_wave2.py tests/test_round3_fix_e.py tests/test_queue_lease_and_dedup.py tests/test_worker.py -v
.venv/bin/python -m pytest tests/test_job_queue_lifecycle.py tests/test_jobs_injection.py tests/test_jobs_await_injection.py tests/test_loop_robustness.py tests/test_reset_reprime.py tests/test_cache_key.py tests/test_api.py -v
# full gates
.venv/bin/python -m pytest tests/ -q          # expect 1147 + ~19 new = ~1166 passed / 16 skipped
.venv/bin/python -m ruff check app tests
```

Branch `rel-24-25-worker` off `main`; commit series (each red→green): ① REL-24 recheck + `LostLeaseError` + L1-L7 (+ §2.11 tuple-return test updates so ② stays isolated) ② REL-25a tuple + resample + S3-S5 + G1-G2 ③ REL-25b columns/migration/ORM + C1/C2 ④ REL-25b submit chain + worker reads + S1/S2 + C3-C6 ⑤ docs. Review gate: §1 decisions vs diff; `grep -n "44100" app/worker.py` → only the `MIXER_SAMPLE_RATE` definition + comments; `grep -rn "generate_stem(" app/` → single production caller, unpacking the tuple.

---

## 5. Risks / residual

- **Reclaim during encode+upload (~1 s residual window)**: row is safe (`_mark_job_complete` guard); only the Garage object could be double-written within that window. Accepted (decision 1) — the guarded window shrinks from the full 5-30 s generation span.
- **`_refresh_lease` not worker_id-scoped** (scout residual): a zombie heartbeat can keep a reclaimed row's lease alive. The upload recheck closes the audio-overwrite hazard regardless (it reads status+owner fresh); scoping the heartbeat UPDATE is a one-line follow-up deliberately left out (decision 14, claim SQL pinned elsewhere).
- **cfg/steps not in the stem cache key** (decision 13): a cached stem ignores later cfg/steps changes until TTL eviction — pre-existing staleness class, now documented; changing the key is a separate reviewed decision (pinned format test).
- **`decode_aac` still hard-codes 44100 at fetch** — intentional: with worker-side normalization the invariant "stored AAC is 44.1 kHz" now actually holds; loosening fetch would reopen the mixer-rate question for zero benefit.
- **Old pending rows at deploy time** carry NULL cfg/steps → worker defaults 7.0/50 — identical to today's behavior (kwargs were never passed), so no behavior cliff across the migration.
- **`resample_poly` on very long buffers** is CPU-bound but runs in the default executor off the event loop (same as encode); only fires for non-44.1 kHz engines (none shipped today — the fix is latent-rate-mismatch insurance, exactly the audit's finding).
- **worker.py lands ≈ 670 L** (pre-existing >500 debt, decision 15); split deferred — follow-ups list, not this unit.
- **`jobs_failed` no longer counts lost-lease stand-downs** (decision 3): a lease-churn-heavy deployment won't see it in stats; the per-incident warning log is the observability seam (folding a counter into `/stats` is a rel-03 follow-up already listed).

## 6. Acceptance mapping (U13 spec → tests)

- "ownership-recheck test" → L1 (skip), L2 (proceed), L3/L4 (strict semantics), L5 (stand-down), L7 (delete-guard unchanged), L6 (breaker boundary).
- "job row carries cfg/steps end-to-end" → C1 (row), C3/C4 (loop + pregen submit), C5 (API submit), C6 (config-UI → submit seam closure), S1 (worker read → generator call), S2/C2 (old-row/NULL fallback).
- "non-44.1 kHz engine output resampled (or stored) at its native rate" → decision 5 (resample ONCE worker-side to the 44.1 kHz chain, documented with the fetch/mixer/fanout/relay evidence) pinned by S3 (resampled exactly once, correct ratio), S4 (44.1 kHz passthrough), S5 (pure fn), G1 (sr no longer dropped).
- invariant 1 (locks): the only new lock section is `read_generation_params` (two attribute reads, no I/O); invariant 2 (fakes, 4-20-line functions): all new tests use named fakes; invariant 6 (regression test per fix): every behavior change above is pinned.
