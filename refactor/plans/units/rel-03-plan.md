# PLAN — Unit 3 `rel-worker-vram` (REL-03 + REL-07 + REL-08 + REL-23), branch `rel-03-worker`
**Spec:** `refactor/plans/rel-remediation-plan.md` §U3 · `docs/reliability_audit.md` REL-03 (Critical), REL-07/REL-08 (High), REL-23 (Medium)
**Baseline gate (verified green at `0523aef`, HEAD of `main`):** `949 passed / 16 skipped`, `ruff` clean; do not regress skips without cause.
---
## 0. Scope summary
| Finding | Root cause | Fix site |
|---|---|---|
| REL-03 | `asyncio.wait_for(600 s)` around generation abandons a non-killable thread holding VRAM + hf_hub `.incomplete` locks; healthcheck (pgrep + `SELECT 1`) passes throughout; a cold-cache model download can never fit the 600 s window, so every retry times out too → silent permanent wedge | `worker.py::_generate_with_lease` (consecutive-timeout circuit breaker → `os._exit(1)`) + `worker.py::start` / `framework_generator.py` (pre-download outside the window) |
| REL-07 | `GeneratorRegistry` has zero concurrency guards; after REL-03 leaks a zombie, the next job's lazy `load_model` races it on the same engine → 2× VRAM → OOM → another timeout → another zombie | `framework_generator.py::GeneratorRegistry` (`threading.Lock` serializing `generate_batch`/`load_model`/`unload_model`) |
| REL-08 | `load_file(device="cuda")` + `load_state_dict` + `.to(device)` keeps the state_dict referenced → ~2× model VRAM transient (12 GB peak for a 6 GB model; OOMs ≤16 GB cards) | `framework_generator.py::StableAudioEngine.load` (CPU-first load, `del state_dict`, single `.to(device)`) |
| REL-23 | Worker never evicts models; `GPUMonitor` offload path is dead code (imported only by tests) → loaded models accumulate until OOM at the next lazy load | `worker.py` between jobs (wire the monitor: LRU-evict non-default models when VRAM critical) |
No mixer / loop / capture / storage code is touched (invariants 1, 3, 4 irrelevant). Worker restart semantics are *used*, not changed (invariant 5).
---
## 1. Design decisions (documented reasoning; deviations from spec letter flagged)
1. **Breaker counter semantics: reset on *success* only, not on "any non-timeout outcome".** Increment on `asyncio.TimeoutError`; reset when `_generate_and_upload` completes under `wait_for`; **leave unchanged on non-timeout failures**. Rationale: the hazard is the *abandoned thread* from timeout #1 — it survives an unrelated later failure (e.g. a fast CUDA-OOM, an upload 500) still holding VRAM/hf locks; only a *completed pipeline* proves the context healthy. "Consecutive" here = "since the last success". The acceptance test (timeout→timeout→exit) is unaffected; a timeout→fail→timeout sequence also trips, which is correct (the wedge is real). Documented in the counter's comment.
2. **Catch site: `_generate_with_lease`** (where `wait_for` raises), all breaker logic in one new helper `_handle_generation_timeout(job)`. On the 2nd consecutive timeout: one structured `logger.error` (event-style fields: `worker_id`, `job_id`, `consecutive_timeouts`, `timeout_seconds`, `action=exit(1)`) then the exit hook. The helper **re-raises nothing** — the except block wraps-and-reraises as builtin `TimeoutError("generation pipeline exceeded Ns")` so `_process_claimed_job`'s generic handler still marks the row `failed` with a *meaningful* `error_message` (bare `str(asyncio.TimeoutError())` is often empty). Note `asyncio.TimeoutError` is a distinct class on py3.10 (`requires-python >= 3.10`) and aliases builtin `TimeoutError` on 3.11+; catching the former and raising the latter is correct on both.
3. **Exit is constructor-injected, defaulting to `os._exit`** (AGENTS.md DI rule; the testable seam the task requires): `GeneratorWorker(config, exit_hook=None)` → `self.exit_hook = exit_hook or os._exit`. `os._exit` specifically (not `sys.exit`): the audit notes the interpreter *deadlocks joining the abandoned non-daemon thread* in graceful teardown — skipping cleanup is the point. On the trip the current job row stays `processing` with a live lease → lapses ≤ 600 s → reclaimed by another worker / the stale-processing reaper (audit "Verified safe": stranded ≤ ~11 min); compose `restart: unless-stopped` brings this worker back with a fresh CUDA context (invariant 5). The healthcheck is *deliberately* unchanged — pgrep+`SELECT 1` passes while wedged, which is why the breaker, not the probe, must be the tripwire.
4. **Pre-download seam: extract `StableAudioEngine.download()` from `load()`** (the cache-check + `hf_hub_download` block, verbatim — including its `print`s, which REL-27/U14 will sweep; moving code, not adding style debt). New `GeneratorRegistry.download_models() -> dict[str, str]` loops engines with **per-model try/except**, records failures in `model_errors`, and *returns* the failures dict; the worker logs each failure and proceeds. Worker `start()` calls it via `await asyncio.to_thread(...)` right after `self.generators.load()` (metadata) and **before `asyncpg.create_pool`** — outside any timeout window, and off-loop so SIGTERM handling stays responsive during a multi-GB download (a sync call inside the coroutine would freeze signal dispatch for the whole download). HF cache volume is mounted rw in compose, so the download pays off across restarts. `hf_hub_download` is file-locked: concurrent workers queue, never corrupt.
5. **REL-07 lock: one coarse `threading.Lock` (`self._generation_lock`) around the whole `GeneratorRegistry.generate_batch` body, plus `load_model` and `unload_model`.** Single-GPU worker gains nothing from concurrency (audit's words). Deadlock avoidance: `generate_batch` lazy-loads via a new private `_load_model_locked` (caller holds the lock); the public `load_model` acquires then delegates. `reload_model` (unload→load) takes the lock twice *sequentially*, never nested → safe with a plain `Lock` (no `RLock` needed). `is_model_loaded` stays lock-free (GIL-atomic `engine.model is None` read). **Compositional consequence, handled in decision 8:** a REL-03 zombie that dies *inside* `generate_batch` now holds this lock forever — which is exactly what escalates the *next* job to a 600 s timeout → breaker → restart. The lock converts the silent race into the designed escalation path.
6. **REL-08: CPU-first load.** `load_file(model_path, device="cpu")` / `torch.load(model_path, map_location="cpu")` → `load_state_dict` → `del state_dict` → single `self.model = self.model.to(self.device)`. The `.ckpt` nested-`"state_dict"` unwrap and the `create_model_from_config` retry block are untouched. The `del` is function-local and not externally observable — the pinned contract is the captured `device="cpu"`/`map_location="cpu"` arg plus exactly one `.to(self.device)` call (noted as unpinnable residue in §6).
7. **REL-23: wire the existing `GPUMonitor`, owned by the registry** (not the worker): `GeneratorRegistry.__init__` constructs `self.gpu_monitor = GPUMonitor()`; `load_model` routes `engine.load()` through `self.gpu_monitor.track_model_load(model_id, engine.load)` (per-model VRAM attribution; on CPU-only boxes it just calls the fn); `unload_model` calls `record_model_unload(model_id)` after a real unload. This keeps `routes/models.py`-side loads attributed too, with zero worker-side plumbing. **LRU ordering comes from the registry, not the monitor**: `select_offload_candidates` sorts VRAM-desc (free-most-first), but the spec's contract is *LRU* — new `self.model_last_used: dict[str, float]` (`time.monotonic()`; updated on load success and on each successful engine dispatch inside `generate_batch`; popped on unload) feeds new `lru_eviction_candidates()` = loaded, non-`default_model_id`, ascending by last-use. The monitor supplies only the pressure gate (`should_offload()`, reserved-mem ≥ 90 % default — reserved includes cached blocks, the right pressure signal).
8. **Eviction call site + bounding.** `_process_next_job` gains `await self._maybe_evict_idle_models()` **after** `_process_claimed_job(job)` returns — "between jobs". The idle branch (queue empty) skips it: VRAM pressure cannot grow while idle (no new loads), and the post-job check already ran after the last job. `_maybe_evict_idle_models` wraps a blocking `_evict_idle_models_sync` (torch queries + `.cpu()` model moves ≈ seconds) in `asyncio.to_thread`, **bounded by `asyncio.wait_for(..., VRAM_EVICTION_TIMEOUT_SECONDS=30)`**: a zombie holding the registry lock (decision 5) must not stall the loop — the next job's own 600 s timeout is the designed escalation. Eviction evicts LRU-first, rechecking `should_offload()` after each unload, stopping when pressure clears. **Graceful no-op without torch/CUDA is inherited from `GPUMonitor.should_offload()` → `False`** (`app.gpu_monitor` imports torch, but so does `framework_generator` already — no new import-failure mode; CPU-only dev machines running the worker simply never evict).
9. **Worker LOC debt, disclosed:** `app/worker.py` is already 527 lines (pre-existing >500 violation, brownfield per AGENTS.md). This unit adds ~65 (breaker, pre-download, eviction, stats field) → ~590. Splitting (e.g. extracting the cleanup-loop wiring) is out of scope, matching the rel-02 precedent for `loop_steps.py`; flagged as follow-up. `framework_generator.py` goes 415 → ~465 (< 500 ✓).
10. **Stub-registry tests verified safe (no updates needed):** `tests/test_adversarial_wave2.py`, `tests/test_queue_lease_and_dedup.py`, `tests/test_round3_fix_e.py` replace `app.framework.framework_generator` with minimal fake `GeneratorRegistry` classes. Grepped: they drive only `_claim_next_job` / `_mark_job_*` / `_generate_with_lease` (with `_generate_and_upload` patched) / `_generate_and_upload` (with `worker.generators = MagicMock()`) — never `start()` or `_process_next_job`, so new registry-touching code (`download_models`, `gpu_monitor`, `lru_eviction_candidates`) is unreachable for them. The `_generate_with_lease` restructure keeps the patched-`_generate_and_upload` contract (same args, same return path), so the private-pool assertions there stay green.
11. **Observability breadcrumb:** `consecutive_generation_timeouts` is a public counter (sibling of `jobs_processed`/`jobs_failed`) and is added to `get_stats()` — a stalled-at-1 counter is the early-warning signal between timeout #1 and the trip.
---
## 2. Exact changes per file
### 2.1 `app/framework/framework_generator.py` (415 → ~465 lines)
**(a)** Imports: add `import threading` (`time` already imported); add `from app.gpu_monitor import GPUMonitor` (no circularity — `app.gpu_monitor` imports only logging+torch).
**(b)** `StableAudioEngine.download()` — new method after `_get_cached_model_path` (extracted verbatim from `load()`):
```python
    def download(self):
        """Ensure weights + config are in the local HF cache; return their paths.

        Extracted from load() so the worker can warm the cache at startup,
        OUTSIDE the generation timeout window (REL-03): a cold-cache download
        inside a job cannot fit the 600 s cap, and the abandoned thread keeps
        the hf_hub .incomplete lock. hf_hub_download is file-locked, so
        concurrent workers queue rather than corrupt.
        """
        model_path = self._get_cached_model_path(self.filename)
        config_path = self._get_cached_model_path(self.config_filename)
        if model_path is None or config_path is None:
            print(f"[{self.repo_id}] Model not in cache, downloading...")
            model_path = hf_hub_download(repo_id=self.repo_id, filename=self.filename)
            config_path = hf_hub_download(repo_id=self.repo_id, filename=self.config_filename)
        else:
            print(f"[{self.repo_id}] Loading model from cache: {model_path}")
        return model_path, config_path
```
`load()` head becomes `model_path, config_path = self.download()` (prints in the extract stay `print` — REL-27/U14 sweeps the file later; this unit moves code, it does not add new print sites beyond the moved ones).
**(c)** `StableAudioEngine.load()` weight-read block (REL-08, ~L92-100):
```python
        try:
            if model_path.endswith(".safetensors"):
                # REL-08: load to CPU first — load_file(device=gpu) +
                # load_state_dict + .to() transiently pins ~2x model VRAM.
                state_dict = load_file(model_path, device="cpu")
            else:
                state_dict = torch.load(model_path, map_location="cpu")
                if isinstance(state_dict, dict) and "state_dict" in state_dict:
                    state_dict = state_dict["state_dict"]

            self.model.load_state_dict(state_dict)
            del state_dict  # REL-08: release the CPU copy before the GPU move
            self.model = self.model.to(self.device)  # single device move
            self.sample_rate = self.model.sample_rate
```
**(d)** `GeneratorRegistry.__init__` additions:
```python
        self.model_last_used: dict[str, float] = {}  # REL-23: model_id -> time.monotonic()
        self._generation_lock = threading.Lock()     # REL-07: serialize GPU access
        self.gpu_monitor = GPUMonitor()               # REL-23: per-model VRAM attribution
```
**(e)** `GeneratorRegistry.download_models()` — new method after `load()`:
```python
    def download_models(self) -> dict[str, str]:
        """Pre-download every enabled engine's weights into the HF cache (REL-03).

        Intended for worker startup, outside the generation timeout window.
        Per-model failures are recorded in model_errors and returned — one bad
        repo must not kill the batch or the worker start that calls this.
        """
        failures: dict[str, str] = {}
        for model_id, engine in self.models.items():
            try:
                engine.download()
            except Exception as e:  # noqa: BLE001 - isolate per-model failures
                self.model_errors[model_id] = str(e)
                failures[model_id] = str(e)
        return failures
```
**(f)** `GeneratorRegistry.generate_batch` (~L216): wrap the whole body (after the `if not self.models` guard) in `with self._generation_lock:`; the lazy-load call becomes `self._load_model_locked(model_id)`; after each successful `engine.generate_batch(...)` dispatch add `self.model_last_used[model_id] = time.monotonic()` (recency refreshes only on *use*, so a failing model ages into eviction sooner).
**(g)** `load_model` split (no behavior change beyond lock + monitor + recency):
```python
    def load_model(self, model_id, progress_callback=None):
        """Load a single model on-demand (serialized on _generation_lock)."""
        with self._generation_lock:
            self._load_model_locked(model_id, progress_callback)

    def _load_model_locked(self, model_id, progress_callback=None):
        """load_model body; caller must hold _generation_lock (REL-07)."""
        # ... existing body verbatim, except:
        #   engine.load()  ->  self.gpu_monitor.track_model_load(model_id, engine.load)
        #   on success (after ModelState.LOADED): self.model_last_used[model_id] = time.monotonic()
```
**(h)** `unload_model`: wrap body in `with self._generation_lock:`; after `engine.unload()` add `self.gpu_monitor.record_model_unload(model_id)` and `self.model_last_used.pop(model_id, None)`. The `engine.model is None` early-return stays *before* both (no phantom attribution). Default-reassignment tail untouched (eviction only unloads non-default, but the route-side unload path keeps working).
**(i)** `lru_eviction_candidates()` — new method:
```python
    def lru_eviction_candidates(self) -> list[str]:
        """Loaded non-default model ids, least-recently-used first (REL-23)."""
        loaded = (mid for mid, engine in self.models.items() if engine.model is not None)
        return sorted(
            (mid for mid in loaded if mid != self.default_model_id),
            key=lambda mid: self.model_last_used.get(mid, float("-inf")),
        )
```
### 2.2 `app/worker.py` (527 → ~590 lines; debt note in decision 11)
**(a)** Constants after `GENERATION_TIMEOUT_SECONDS` (~L58):
```python
# REL-03 circuit breaker: a timed-out generation thread cannot be killed and
# keeps holding VRAM + hf_hub locks, so retrying in-process just leaks another
# one. After this many consecutive timeouts (no successful pipeline between),
# exit non-zero and let Docker restart into a fresh CUDA context.
GENERATION_TIMEOUT_BREAKER_THRESHOLD = 2
# REL-23: bound on one between-jobs VRAM eviction pass — a zombie holding the
# registry lock must not stall the loop (see _maybe_evict_idle_models).
VRAM_EVICTION_TIMEOUT_SECONDS = 30.0
```
**(b)** `__init__` (~L101): signature `def __init__(self, config: WorkerConfig, exit_hook: Optional[Callable[[int], None]] = None):` with `self.exit_hook = exit_hook or os._exit` and `self.consecutive_generation_timeouts = 0` (import `Callable` alongside `Optional`).
**(c)** `start()` (~L112, after the "Loaded N audio models" log, before `asyncpg.create_pool`):
```python
        # REL-03 (audit Critical): a cold-cache model download can never fit
        # inside the 600 s generation window, and its abandoned thread wedges
        # the hf_hub lock for every later retry. Warm the cache BEFORE the job
        # loop, outside any timeout. to_thread keeps SIGTERM handling
        # responsive during a multi-GB download. Per-model failures are
        # logged and skipped: one bad repo must not stop the worker serving
        # the other models.
        download_failures = await asyncio.to_thread(self.generators.download_models)
        for model_id, error in download_failures.items():
            logger.error("Pre-download failed for model %s: %s", model_id, error)
```
**(d)** `_process_next_job` (~L153): append `await self._maybe_evict_idle_models()` after `await self._process_claimed_job(job)`.
**(e)** `_generate_with_lease` (~L261) — restructured (try/except/else/finally; finally unchanged):
```python
        try:
            result = await asyncio.wait_for(
                self._generate_and_upload(job, gen_pool),
                timeout=GENERATION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            # REL-03: the abandoned thread keeps holding VRAM / hf locks, so a
            # timeout is not just a failed job — feed the circuit breaker.
            self._handle_generation_timeout(job)
            raise TimeoutError(
                f"generation pipeline exceeded {GENERATION_TIMEOUT_SECONDS:.0f}s"
            ) from exc
        else:
            # Only a completed pipeline proves the CUDA context healthy;
            # non-timeout failures deliberately leave the counter untouched.
            self.consecutive_generation_timeouts = 0
            return result
        finally:
            heartbeat.cancel()
            await _silently_cancel(heartbeat)
            gen_pool.shutdown(wait=False, cancel_futures=True)
```
**(f)** New methods after `_generate_with_lease`:
```python
    def _handle_generation_timeout(self, job: dict) -> None:
        """REL-03 breaker: 2nd consecutive generation timeout -> exit(1).

        os._exit (not sys.exit) deliberately skips graceful teardown: the
        audit found the interpreter deadlocks joining the abandoned
        non-daemon thread. The current job row keeps its live lease and is
        reclaimed/reaped after it lapses (<= ~11 min); compose
        restart=unless-stopped brings this worker back with a fresh context.
        """
        self.consecutive_generation_timeouts += 1
        count = self.consecutive_generation_timeouts
        logger.error("Job %s generation timed out (consecutive=%d)", job["id"], count)
        if count < GENERATION_TIMEOUT_BREAKER_THRESHOLD:
            return
        logger.error(
            "circuit_breaker_open worker_id=%s job_id=%s consecutive_timeouts=%d "
            "timeout_seconds=%.0f action=exit_1 reason=generation_timeout_streak",
            self.config.worker_id,
            job["id"],
            count,
            GENERATION_TIMEOUT_SECONDS,
        )
        self.exit_hook(1)

    async def _maybe_evict_idle_models(self) -> None:
        """REL-23: LRU-evict loaded non-default models when VRAM is critical.

        Between jobs only. Bounded on purpose: a REL-03 zombie can hold the
        registry lock forever, and a stuck eviction must not stall the loop —
        the NEXT job's 600 s timeout is what trips the breaker.
        """
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._evict_idle_models_sync),
                timeout=VRAM_EVICTION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("VRAM eviction timed out (registry lock busy); skipping")

    def _evict_idle_models_sync(self) -> None:
        """Blocking half of _maybe_evict_idle_models (torch queries + .cpu() moves)."""
        monitor = self.generators.gpu_monitor
        if not monitor.should_offload():
            return
        for model_id in self.generators.lru_eviction_candidates():
            logger.info("VRAM critical: unloading idle model %s", model_id)
            self.generators.unload_model(model_id)
            if not monitor.should_offload():
                return
```
**(g)** `get_stats()` (+1 field): `"consecutive_generation_timeouts": self.consecutive_generation_timeouts,`.
### 2.3 Docs (pipeline "docs" stage, same unit)
- `docs/reliability_audit.md`: add `**Status: fixed-in rel-03-worker** — <one-line>` notes to REL-03, REL-07, REL-08, REL-23 (pattern: REL-01/REL-02 entries).
- `refactor/plans/rel-remediation-plan.md`: status-table row 3 → landed (commit filled at merge).
---
## 3. TDD regression tests (write first, confirm red, then implement)
### 3.1 New file `tests/test_worker_vram.py` (~300 lines) — the unit's acceptance suite
Module head mirrors `tests/test_worker.py`: try-import `app.framework.framework_generator`, else `pytest.skip(..., allow_module_level=True)`. Helpers: `_make_worker(exit_hook=None)` → `GeneratorWorker(WorkerConfig(worker_id="t", pg_dsn="postgresql://t", garage=MagicMock()), exit_hook=exit_hook)` with `worker.db` set to a MagicMock pool yielding an `AsyncMock` conn (pattern: `test_worker.py::test_claim_next_job_returns_pending_job`); `_job(**over)` → canned job dict; `_run(coro)` → `asyncio.new_event_loop()` run+close (pattern used across the worker tests). Registry-level tests inject `MagicMock(spec=StableAudioEngine)` engines via `registry.models = {...}` (pattern: `test_generator.py::test_load_model_success`).

| # | Test | Pins | Core assertions |
|---|---|---|---|
| B1 | `test_second_consecutive_generation_timeout_trips_breaker` (**audit soak #5 contract**) | REL-03 trip | `monkeypatch.setattr(app.worker, "GENERATION_TIMEOUT_SECONDS", 0.05)`; `worker.generators.generate_stem = lambda **kw: time.sleep(0.3)`; first `pytest.raises(TimeoutError): _run(worker._generate_with_lease(job))` → `exit_calls == []`, `consecutive_generation_timeouts == 1`; second → `exit_calls == [1]`, counter == 2. |
| B2 | `test_first_timeout_reraises_as_timeouterror_with_message` | wiring | Single timeout → raises builtin `TimeoutError`, message contains "exceeded"; counter == 1; exit hook untouched. Heartbeat task cancelled (no dangling DB call) — implicitly pinned by clean loop close. |
| B3 | `test_counter_resets_on_successful_pipeline` | reset semantics | timeout → success (`generate_stem` returns `np.zeros((8, 2), np.float32)`; patch `app.worker.encode_aac` → `b""`, `app.worker.get_audio_duration` → 1.0; `worker.garage.put_object = AsyncMock()`; `worker.db` mock) → timeout → **no** exit, counter == 1. |
| B4 | `test_timeout_marks_job_failed_through_process_claimed_job` | row hygiene | Mock-db pool; two timeouts via `_process_claimed_job` → `jobs_failed == 2`, `_mark_job_failed` SQL executed with the "exceeded" message (capture via conn.execute calls), exit hook called once. |
| P1 | `test_start_pre_downloads_models_before_job_loop` | REL-03a call | Patch `GeneratorRegistry.download_models` (MagicMock), `app.worker.asyncpg.create_pool` (AsyncMock → pool with `close=AsyncMock()`), `app.worker.create_garage_client_from_env`, `app.worker.create_cleanup_config_from_env`; pre-set `worker.running = False`; `await worker.start()` completes → `download_models` called once, pool `close` awaited (start ran to shutdown — i.e. download happened before the loop could run). |
| P2 | `test_download_models_isolates_per_model_failures` | containment | Real registry, three mock engines, `engine_b.download.side_effect = RuntimeError("net down")` → no raise; `engine_a.download`/`engine_c.download` called; returned failures == `{"model_b": "net down"}`; `registry.model_errors["model_b"]` set. |
| P3 | `test_engine_download_uses_cache_and_skips_network` | extraction fidelity | Patch `engine._get_cached_model_path` → paths; patch `app.framework.framework_generator.hf_hub_download` (fail test if called) → returns the cached tuple. |
| P4 | `test_engine_download_downloads_on_cache_miss` | extraction fidelity | `_get_cached_model_path` → None; patched `hf_hub_download` returns sentinel paths → called with `(repo_id=..., filename=weights)` then config; returns sentinels. |
| L1 | `test_concurrent_generate_batch_calls_serialize` (**RED today: max_active == 2**) | REL-07 | Engine mock with `generate_batch` side-effect tracking concurrent-entry count (`threading.Lock` + counter, `time.sleep(0.05)`); `engine.model = object()` (loaded → no lazy-load path); two threads call `registry.generate_batch([...], 120)`; both get results; `max_concurrent == 1`. |
| L2 | `test_concurrent_load_model_calls_serialize` | REL-07 load path | `engine.model = None`, `engine.load` overlap-counted (monitor's `track_model_load` calls it directly on CPU test envs); two threads `registry.load_model("m")` → `max_concurrent == 1`, final state LOADED once. |
| F1 | `test_safetensors_load_is_cpu_first` (**audit acceptance: mock `load_file` captures device**) | REL-08 | `engine.device` set via patched `torch.cuda.is_available→True`; patch `create_model_from_config` → MagicMock model, patch `...framework_generator.load_file`, patch `engine._get_cached_model_path` → `("/m.safetensors", "/cfg.json")`, `open` → config json (use real temp file or `mock_open`); `engine.load()` → `load_file` kwargs `device == "cpu"`; `model.load_state_dict` once; `model.to` **once**, with `"cuda"`; `engine.sample_rate` taken from model. |
| F2 | `test_ckpt_load_maps_to_cpu_and_unwraps_state_dict` | REL-08 `.ckpt` branch | filename `.ckpt`; patch `...framework_generator.torch.load` returning `{"state_dict": sentinel}` → `map_location == "cpu"`; `load_state_dict` called with `sentinel`; `.to` once. |
| E1 | `test_eviction_unloads_lru_non_default_under_pressure` (**audit acceptance**) | REL-23 order | Real registry: engines `a` (default), `b`, `c` all `model = object()`; seed `model_last_used`: b oldest, c newest; `monkeypatch.setattr(GPUMonitor, "should_offload", lambda self, threshold_pct=90.0: True)`; `await worker._maybe_evict_idle_models()` → `engine_b.unload` called; `engine_a.unload`/`engine_c.unload` not. |
| E2 | `test_eviction_spares_default_even_when_oldest` | REL-23 guard | default has oldest `last_used` + only other loaded model → the *other* one unloads; default never. |
| E3 | `test_eviction_stops_when_pressure_clears` | REL-23 loop | `should_offload` side_effect `[True, False]`; two candidates → exactly one unload, second engine untouched. |
| E4 | `test_eviction_noop_when_vram_not_critical` (**graceful no-op; paired with `test_gpu_monitor.py`'s real no-CUDA pins**) | REL-23 gate | `should_offload → False` → zero unload calls, `_maybe_evict_idle_models` returns promptly. |
| E5 | `test_eviction_is_bounded_when_registry_lock_held` | decision 8 | Main thread acquires `registry._generation_lock`; `monkeypatch` `VRAM_EVICTION_TIMEOUT_SECONDS = 0.05`, `should_offload → True`, one candidate loaded → `_run(worker._maybe_evict_idle_models())` returns (~instantly) without hanging; release lock in `finally` (abandoned to_thread worker then drains harmlessly against mocks). |
| E6 | `test_load_and_unload_route_through_gpu_monitor` | REL-23 wiring | `registry.gpu_monitor = MagicMock(spec=GPUMonitor)`; `load_model("m")` → `track_model_load` called once with `("m", engine.load)` and engine actually loaded; `unload_model("m")` → `record_model_unload` called once; `model_last_used` popped. |
| E7 | `test_generate_batch_refreshes_model_last_used_and_lru_order` | REL-23 recency | Engine mock, `model = object()`; seed `model_last_used["m"] = 0.0`; one `generate_batch` → `model_last_used["m"] > 0`; with two loaded non-default models, `lru_eviction_candidates()` returns least-recent first; failed dispatch (`side_effect = RuntimeError`) leaves the timestamp stale (refresh-on-use-only). |
### 3.2 Keep-green set (must not regress; run explicitly after implementation)
`tests/test_worker.py` (claim/complete/cleanup/health/stats) · `tests/test_generator.py` (registry loads/states/unload/reload — mock engines keep working; `test_load_model_already_loaded` early-return precedes monitor) · `tests/test_gpu_monitor.py` (monitor semantics unchanged; its module-scoped torch mock restores `sys.modules` — no interference) · `tests/test_adversarial_wave2.py` (private-pool + `_generate_with_lease` contract — decision 10) · `tests/test_queue_lease_and_dedup.py` · `tests/test_round3_fix_e.py` (stub registries — decision 10) · `tests/test_worker_fetch_audio.py` · `tests/test_io_timeouts.py`.
### 3.3 TDD order
1. Write `tests/test_worker_vram.py` → `.venv/bin/python -m pytest tests/test_worker_vram.py -q` → **red** (B1/B2/B4 no breaker exists — timeouts propagate as bare `TimeoutError` with empty message, no exit; P1/P2 no `download_models`; L1/L2 `max_concurrent == 2`; F1/F2 `device == "cuda"`; E1–E7 no candidates/monitor wiring, E5 hangs→guard with a per-test `asyncio.wait_for` timeout in the *test harness* so red is a failure, not a hang).
2. Implement §2.1 (generator: download extraction → cpu-first → lock → monitor/lru) → registry-side tests green.
3. Implement §2.2 (worker: constants/ctor → breaker → pre-download → eviction → stats) → worker-side tests green.
4. Keep-green sweep (§3.2 list) → full gate: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q` → **949 + ~20 new passed / 16 skipped**, zero regressions.
5. Docs edits (§2.3).
---
## 4. Invariant compliance (plan §Invariants)
| # | How respected |
|---|---|
| 1 — lock discipline | No `state.lock`/`sync_lock` sections touched. The new `_generation_lock` is a registry-internal GPU mutex (worker/registry concern, not mixer state); held across blocking GPU work by design (single-GPU serialization), never while calling back into framework state. |
| 2 — style / hexagonal | New functions 4–20 lines with intent docstrings (`download`, `download_models`, `_handle_generation_timeout`, `_maybe_evict_idle_models`, `_evict_idle_models_sync`, `lru_eviction_candidates`, `_load_model_locked`); no `Any`; deps injected (`exit_hook` ctor param, monitor on registry); one spelling per concept (`GENERATION_TIMEOUT_BREAKER_THRESHOLD`, `model_last_used`, `lru_eviction_candidates`). Ports untouched (worker is an adapter-side process). File budget: generator ~465 ✓; worker ~590 — pre-existing debt, disclosed (decision 11). |
| 3 — audio thread | Zero mixer/audio-thread changes. Eviction runs between jobs off-loop (`to_thread`); the 30 s bound keeps the worker loop (which feeds nothing audio-side directly) responsive regardless. |
| 4 — LLM capture | Untouched. |
| 5 — worker restart semantics | **Affirmed, not violated:** the breaker's `exit(1)` + compose `restart: unless-stopped` *is* the designed recovery; the lease/reclaim machinery already handles the dead worker's row (audit "Verified safe"). Healthcheck deliberately unchanged. |
| 6 — regression per fix | B1–B4 pin REL-03 (soak #5 contract in miniature; full soak lands in U15 alongside VRAM plateau #4); L1–L2 pin REL-07; F1–F2 pin REL-08; E1–E7 pin REL-23 + monitor wiring; P1–P4 pin pre-download; §3.2 guards the neighbors. |
---
## 5. Acceptance checklist (maps to §U3 spec)
- [ ] Timeout circuit-breaker contract (soak #5): two consecutive timeouts → non-zero exit → **B1** (+B2 message/wiring, B3 reset semantics, B4 row hygiene)
- [ ] Pre-download enabled models in `start()` outside the timeout window; per-model try/except → **P1/P2** (+P3/P4 extraction fidelity)
- [ ] Registry lock test: concurrent generate calls serialize → **L1** (+L2 load path)
- [ ] Load path test asserting CPU-first load (mock `load_file` captures device arg) → **F1** (+F2 `.ckpt` branch)
- [ ] LRU-evict non-default models between jobs when VRAM critical; wire (not remove) `GPUMonitor`; graceful no-op without torch → **E1–E4** (+E5 bounded, E6 wiring, E7 recency)
- [ ] `ruff check` clean; full suite 949 + ~20 new passed / 16 skipped (~8 s)
- [ ] Audit status lines + plan status table updated (§2.3)
## 6. Risks / out of scope
- **`del state_dict` is unpinnable from outside** (function-local): F1/F2 pin the observable contract (`device="cpu"` / `map_location="cpu"`, single `.to(device)`); the peak-VRAM halving itself is only verifiable on real hardware (U15 soak #4 VRAM-plateau).
- **Two workers pre-downloading concurrently** queue on hf_hub file locks — bounded startup delay, no corruption (hf-hub guarantees); single-worker compose today.
- **Eviction ping-pong** (evict model B, next job wants B, reload): bounded by the LRU order (stale-first) and the 90 % reserved gate; reload cost is the pre-downloaded cache + `load_model`, not a network download. Acceptable; revisit with per-model hysteresis only if soak shows churn.
- **Zombie-holding-lock interaction** (decisions 5+8) is the composed-system crux: lock → next job queues → 600 s timeout → breaker → restart. E5 pins the eviction bound; B1 pins the escalation. If a future change shrinks `GENERATION_TIMEOUT_SECONDS`, keep the breaker threshold ≥ 2.
- **Job row on trip stays `processing`** until lease lapse (≤ ~11 min incl. reaper) — deliberate (exit is immediate; the reclaim path is already audited safe). Alternative (mark-failed-then-exit) rejected: adds an await between decision and exit on a suspect process.
- **Worker LOC debt** (527 → ~590, decision 11) — split out of scope; note for follow-ups.
- Out of scope: REL-24 (ownership recheck before upload), REL-25 (sample rate/cfg/steps threading) — unit 13; REL-27 print→logger sweep — unit 14; `worker_routes.py`/healthcheck changes — none needed; `get_vram_usage` attribution rewrite — monitor now *has* the data, surfacing it is polish.
