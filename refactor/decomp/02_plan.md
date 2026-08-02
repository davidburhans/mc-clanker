# Implementation Plan: `_run_loop` Decomposition (DoD Gate 2)

> **Source of truth:** `refactor/decomp/01_runloop_map.md` + firsthand reads of `app/framework/loop_orchestrator.py` (L169–636). All line anchors verified against current code (post-Phase-9 ruff fix).

## Goal

Break the ~467-LOC `_run_loop` god-method into ≤50-LOC `_step_*` methods (the explicit ~84-LOC `_step_commit_state` atomic exception), keeping 569 tests green at every commit, behavior byte-identical, with no control-flow or lock-invariant regression.

---

## 1. Data Design

### `_StepResult` enum

```python
class _StepResult(enum.Enum):
    PROCEED = auto()       # continue to the next _step_* call
    RESTART_ITER = auto()  # `continue` the outer while-loop (skip remaining steps)
    EXIT_LOOP = auto()     # `break` the outer while-loop (shutdown)
```

Only 3 call sites emit non-PROCEED (P1 shutdown break → EXIT_LOOP, P1 not-generating → RESTART_ITER, P2 will_call_llm False → RESTART_ITER). All other break/continue are local to their method.

### `StepCtx` dataclass (per-iteration working state)

```python
@dataclass
class StepCtx:
    """Per-iteration working state threaded through _step_* methods.

    `loop_idx` is NOT here — it promotes to self._loop_idx (the only var
    surviving across iterations). Everything else is per-iteration."""
    # P2 pregen-decision outputs
    pregen_ready: bool = False
    conductor_response: dict | None = None
    # P3 read-state snapshot
    active_stems: list = field(default_factory=list)
    user_override: str = ""
    available_instruments: list = field(default_factory=list)
    stem_history: list = field(default_factory=list)
    llm_config: dict = field(default_factory=dict)
    available_models: list = field(default_factory=list)
    bpm_override: int | None = None
    key_override: str | None = None
    current_bpm: int = 120
    current_key: str = "C minor"
    # P5 parse output
    deduped_tracks: list = field(default_factory=list)
    # P6 build-next output
    local_next_stems: list = field(default_factory=list)
    local_current_bpm: int = 120
    local_current_key: str = "C minor"
    # P7 submit output
    pending_jobs: list = field(default_factory=list)
    # P9 tile output
    prepared_tracks: list = field(default_factory=list)
    loop_duration_samples: int = 0
    # P10 commit-to-mixer output
    tracks_to_use: list = field(default_factory=list)
    duration_samples: int = 0
    current_loop_end_sample: int = 0
```

### `CommitResult` (P11 → P12 handoff)

```python
@dataclass
class CommitResult:
    """P11 atomic-commit outputs consumed by _step_post_commit (P12)."""
    needs_pregen: bool
    needs_initial_record: bool
    rec_stems: list
    rec_set_name: str
    rec_reasoning: str
    state_snapshot: dict
```

---

## 2. `_run_loop` Skeleton (target: ≤60 LOC)

```python
async def _run_loop(self):
    """Main async framework loop — thin driver calling _step_* methods."""
    self._loop_idx = 0
    ctx = StepCtx()

    while self.running and state.is_running:
        try:
            ctx = StepCtx()  # fresh per-iteration state

            r = await self._step_wait_for_start(ctx)
            if r is _StepResult.EXIT_LOOP:
                break
            if r is _StepResult.RESTART_ITER:
                continue

            r = await self._step_pregen_decision(ctx)
            if r is _StepResult.RESTART_ITER:
                continue

            await self._step_read_state(ctx)
            await self._step_call_conductor(ctx)
            await self._step_parse_actions(ctx)
            await self._step_build_next_stems(ctx)
            await self._step_submit_jobs(ctx)
            await self._step_await_jobs_fetch(ctx)
            await self._step_tile_audio(ctx)
            await self._step_append_audit(ctx)
            await self._step_commit_to_mixer(ctx)

            commit = await self._step_commit_state(ctx)
            await self._step_post_commit(ctx, commit)
            await self._step_await_pregen(ctx)

        except asyncio.CancelledError:
            self._finish_loop()
            raise
        except Exception as e:
            print(f"[AsyncFrameworkLoop] Loop iteration error (will retry): {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(LOOP_RETRY_BACKOFF_SECONDS)
            continue

    self._finish_loop()
```

**Note:** `self._loop_idx` replaces the local `loop_idx`. `_step_wait_for_start` increments it. All `_step_*` methods read `self._loop_idx`. This is the only cross-iteration variable.

---

## 3. Phase D0 — Red Tests First (BEFORE extraction)

5 characterization tests pinning CURRENT behavior of the weakly-covered phases (map §E). Added to `tests/test_runloop_decomp.py` (NEW). Reuse the harness from `test_framework_characterization` (`_FakeMixer`, `_seed_loop_for_run`, `_wire_loop_no_io`, `_conductor_response_with_actions`, `_active_stem_for_retain`).

### D0-1: P3 override apply/clear order

```python
async def test_p3_override_applied_then_cleared(monkeypatch):
    """target_bpm_override is applied to current_bpm then cleared to None."""
    loop = AsyncFrameworkLoop(uuid4())
    _seed_loop_for_run(loop, current_sample=0, stop_on=("add", 1))
    state.target_bpm_override = 140
    state.target_key_override = "F major"
    _wire_loop_no_io(loop, monkeypatch, response=_conductor_response_with_actions())
    await asyncio.wait_for(loop._run_loop(), timeout=5.0)
    assert state.current_bpm == 140
    assert state.current_key == "F major"
    assert state.target_bpm_override is None   # cleared
    assert state.target_key_override is None   # cleared
```

### D0-2: P4 fallback through _run_loop

```python
async def test_p4_conductor_failure_uses_fallback(monkeypatch):
    """Conductor raising → _run_loop uses the retain-all fallback (not crash)."""
    loop = AsyncFrameworkLoop(uuid4())
    _seed_loop_for_run(loop, current_sample=0, stop_on=("add", 1))
    state.active_stems = [_active_stem_for_retain()]
    # Wire with a conductor that raises
    loop.conductor.get_next_state_async = AsyncMock(side_effect=RuntimeError("LLM down"))
    loop._submit_job = AsyncMock(return_value=uuid4())
    loop._fetch_audio = AsyncMock(return_value=None)
    loop._append_loop_audit = AsyncMock()
    loop._pre_generate_next_loop = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    import app.framework.loop_orchestrator as orch
    monkeypatch.setattr(orch, "wait_for_multiple_jobs", AsyncMock(return_value={}))
    await asyncio.wait_for(loop._run_loop(), timeout=5.0)
    # Fallback retains all active stems → at least 1 track committed
    assert len(loop.mixer.add_track_internal_calls) >= 1
```

### D0-3: P7 cache-HIT skip

```python
async def test_p7_cached_stem_skips_job_submission(monkeypatch):
    """A stem already in stem_cache → _submit_job NOT called for it."""
    loop = AsyncFrameworkLoop(uuid4())
    _seed_loop_for_run(loop, current_sample=0, stop_on=("add", 1))
    # Pre-seed the cache for the stem the conductor will "add"
    # (run once to discover the key, then run again to verify skip)
    submit_mock = AsyncMock(return_value=uuid4())
    loop.conductor.get_next_state_async = AsyncMock(return_value=_conductor_response_with_actions())
    loop._submit_job = submit_mock
    loop._fetch_audio = AsyncMock(return_value=np.ones((100, 2), dtype=np.float32))
    loop._append_loop_audit = AsyncMock()
    loop._pre_generate_next_loop = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    import app.framework.loop_orchestrator as orch
    monkeypatch.setattr(orch, "wait_for_multiple_jobs",
                        AsyncMock(return_value={submit_mock.return_value: "audio/x.aac"}))
    await asyncio.wait_for(loop._run_loop(), timeout=5.0)
    # First run submitted the job + cached audio. Reset + run again.
    submit_mock.reset_mock()
    loop._pre_generate_next_loop = AsyncMock()
    loop._seed_loop_for_run  # state still has stem_cache populated
    await asyncio.wait_for(loop._run_loop(), timeout=5.0)
    submit_mock.assert_not_called()  # cache HIT → skip
```

### D0-4: P8 foreground cache_stem call (the missing divergence complement)

```python
async def test_p8_foreground_fetch_routes_through_cache_stem(monkeypatch):
    """Foreground _run_loop calls state.cache_stem when audio is fetched."""
    loop = AsyncFrameworkLoop(uuid4())
    _seed_loop_for_run(loop, current_sample=0, stop_on=("add", 1))
    job_id = uuid4()
    loop.conductor.get_next_state_async = AsyncMock(return_value=_conductor_response_with_actions())
    loop._submit_job = AsyncMock(return_value=job_id)
    loop._fetch_audio = AsyncMock(return_value=np.ones((100, 2), dtype=np.float32))
    loop._append_loop_audit = AsyncMock()
    loop._pre_generate_next_loop = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    import app.framework.loop_orchestrator as orch
    monkeypatch.setattr(orch, "wait_for_multiple_jobs", AsyncMock(return_value={job_id: "audio/x.aac"}))
    with patch.object(state, "cache_stem") as cs_mock:
        await asyncio.wait_for(loop._run_loop(), timeout=5.0)
    cs_mock.assert_called_once()  # foreground DOES route through cache_stem
```

### D0-5: P13 transition recording

```python
async def test_p13_transition_event_recorded_outside_lock(monkeypatch):
    """When the mixer signals a transition, record_loop_transition fires
    (snapshotted under state.lock, called outside it)."""
    loop = AsyncFrameworkLoop(uuid4())
    mixer = _seed_loop_for_run(loop, current_sample=0, stop_on=None)
    # Make pop_transition_event return a transition on the 2nd call
    mixer._transition_calls = [None, 2]  # 2nd poll → transitioned to loop 2
    mixer.pop_transition_event = lambda: mixer._transition_calls.pop(0) if mixer._transition_calls else None
    # Stop after the transition fires (flip running in the record spy)
    lock_states = []
    def spy(idx, stems, s, r):
        lock_states.append(state.lock.locked())
        loop.running = False  # terminate
    monkeypatch.setattr(state, "record_loop_transition", spy)
    _wire_loop_no_io(loop, monkeypatch, response=_conductor_response_with_actions())
    # Need pregen_done to NOT fire first — make pregen a no-op that doesn't set done
    async def _no_pregen(_i, _s):
        pass  # don't set _pregen_done → P13 reaches the transition check
    loop._pre_generate_next_loop = _no_pregen
    await asyncio.wait_for(loop._run_loop(), timeout=5.0)
    assert any(idx == 2 for idx, _, _, _ in []), "transition should fire"  # spy captures via lock_states
    assert lock_states, "record_loop_transition should have fired"
    assert not any(lock_states), "must be outside state.lock"
```

**Verification:** pytest `tests/test_runloop_decomp.py` → all 5 green (characterizing CURRENT behavior). Then full suite ≥574 (569 + 5).

**Commit:** `test(framework): pin weakly-covered _run_loop phases before decomposition (D0)`

---

## 4. Ordered Extraction Plan (safest-first; each a separate commit)

**Ordering rationale:** Leaf methods with no control-flow jumps first (P9/C1/P5 — pure data transforms). Then the I/O steps (P4/P7/P8 — nested try, local continue, cache_stem). Then the state-mutation steps (P6/P3 — overrides, next_stems). Then the mixer step (P10). Then the signal-emitting steps (P1/P2 — outer-while jumps). Finally the atomic core (P11/P12) + P13 (inner while) last, guarded by the safety tests.

| Step | Method | Phase | Hazard (map §C) | Why this order |
| ------ | -------- | ------- | ----------------- | ---------------- |
| D1 | `_step_tile_audio(ctx)` | P9 | none (pure, calls tile_to_loop) | Easiest: 1 call, no lock, no control flow |
| D2 | `_step_append_audit(ctx)` | C1 | none (delegates to _append_loop_audit) | 1-line delegate |
| D3 | `_step_parse_actions(ctx)` | P5 | state.lock for action log | Small lock block, no await inside |
| D4 | `_step_call_conductor(ctx)` | P4 | **nested try/except must stay nested** | Isolate the LLM try before I/O steps |
| D5 | `_step_submit_jobs(ctx)` | P7 | local `continue` (cache-HIT) | for-loop with local continue only |
| D6 | `_step_await_jobs_fetch(ctx)` | P8 | state.lock + state.cache_stem | The divergence site (D0-4 guards it) |
| D7 | `_step_build_next_stems(ctx)` | P6 | state.lock for next_stems write | State mutation, no control flow |
| D8 | `_step_read_state(ctx)` | P3 | state.lock (reset+overrides+snapshot) | D0-1 guards override order |
| D9 | `_step_commit_to_mixer(ctx)` | P10 | mixer.lock + loop-1 vs loop>1 branch | D0-3/Gaps 4-5 guard it |
| D10 | `_step_wait_for_start(ctx)` | P1 | **EXIT_LOOP + RESTART_ITER signals** | First signal emitter |
| D11 | `_step_pregen_decision(ctx)` | P2 | **RESTART_ITER signal** + 5 pregen vars | Second signal emitter |
| D12 | `_step_commit_state(ctx) -> CommitResult` + `_step_post_commit(ctx, commit)` | P11+P12 | **atomic single lock** + record_loop_transition outside | Atomic core, last; AST + outside-lock tests guard |
| D13 | `_step_await_pregen(ctx)` | P13 | **inner while self.running** + transition recording | Last; D0-5 + outside-lock test guard |

Each step: extract the phase into a `_step_*` method, replace the inline code with a call, verify 574+ green + AST lock test green + record_loop_transition-outside-lock green, commit.

**Commit msg pattern:** `refactor(framework): extract _step_<name> from _run_loop (D<N>)`

---

## 5. Per-Extraction Hazard Callouts

| Step | Hazard | Preservation strategy |
| ------ | -------- | ---------------------- |
| D1 P9 | reads `mixer.sample_rate` (Optional) | pass via ctx; `self.mixer.sample_rate if self.mixer else None` |
| D3 P5 | `state.lock` for action log | keep the `async with state.lock:` block inside the method; AST test guards no-await |
| D4 P4 | **nested try swallows LLM error → fallback** | keep the `try/except Exception` INSIDE `_step_call_conductor`; do NOT merge with B1's outer try |
| D5 P7 | `continue` at L380 (cache-HIT) | this is a for-loop continue (local) → stays inside the method; no signal needed |
| D6 P8 | `state.cache_stem` under `state.lock` | keep `async with state.lock: state.cache_stem(...)` inside the method; D0-4 + divergence test guard |
| D7 P6 | `state.next_stems` write under `state.lock` | keep the lock block inside; write `local_*` to ctx |
| D8 P3 | reset + overrides + snapshot under `state.lock`; `mixer.clear()` + `stem_cache.clear()` inside lock (sync, allowed) | keep the full `async with state.lock:` block; D0-1 guards override order |
| D9 P10 | `mixer.lock` (threading); loop-1 writes `current_loop_end_sample` + `_current_loop_duration`; loop>1 calls `set_next_loop` | keep `with self.mixer.lock:` block; write `tracks_to_use`/`duration_samples` to ctx |
| D10 P1 | **3 control-flow jumps** (wait-while, break→outer, continue→outer) | wait-while stays inline; `break` → `return _StepResult.EXIT_LOOP`; `continue` → `return _StepResult.RESTART_ITER`; skeleton interprets |
| D11 P2 | **continue→outer** (L249 will_call_llm False); pregen vars span 5 later phases | `continue` → `return _StepResult.RESTART_ITER`; pregen vars → ctx fields |
| D12 P11+P12 | **atomic single lock** (~84 LOC); `record_loop_transition` outside lock | `_step_commit_state` = ONE `async with state.lock:` block, returns `CommitResult`; `_step_post_commit` calls `record_loop_transition(1,...)` AFTER the method returns (lock already released); AST test + outside-lock test guard |
| D13 P13 | **inner `while self.running:`** (must not convert to signal); transition recording under `state.lock` then `record_loop_transition` outside | keep `while self.running:` verbatim inside the method; breaks are local (end the iteration, not the outer loop); `record_loop_transition` called after the inner `async with state.lock:` snapshot block releases |

---

## 6. Definition of Done (measurable gates)

1. `wc -l` of `_run_loop` body (between `async def _run_loop` and the next `def`) → **≤ ~60 LOC** (the skeleton driver). ✅
2. Each `_step_*` method → **≤50 LOC** EXCEPT `_step_commit_state` (~84, the single-lock atomic exception). ✅
3. `.venv/bin/python -m pytest tests/ --timeout=30 -q` → **≥574 passed** (569 + 5 D0 tests), 0 failed. ✅ Every step.
4. `test_no_io_inside_state_lock_in_orchestrator` (AST) → **passes** (file-wide, guards extracted methods). ✅
5. `test_record_loop_transition_runs_outside_state_lock` → **passes**. ✅
6. D0 characterization tests (P3/P4/P7/P8/P13) → **all green** (behavior unchanged). ✅
7. Frozen API untouched (`test_frozen_api_importable` green). ✅
8. `ruff check app/ tests/` → exit 0 (no new violations). ✅

---

## Summary

- **Step count:** 1 D0 (red tests) + 13 extractions (D1-D13) = **14 commits**, each keeping the suite green.
- **3 highest-risk extractions:** (1) **D12** `_step_commit_state` + `_step_post_commit` — the ~84-LOC atomic single-lock commit + the record_loop_transition-outside-lock handoff (the core invariant); (2) **D13** `_step_await_pregen` — the untested P13 inner-while transition path; (3) **D10/D11** `_step_wait_for_start`/`_step_pregen_decision` — the only methods emitting `_StepResult` signals (converting break/continue to return values).
- **`loop_idx` → `self._loop_idx`:** no cross-iteration subtlety. It's initialized to 0 in the skeleton (P0), incremented exactly once per iteration in `_step_wait_for_start` (P1 L202), read by 7 phases, and never persists meaningful state across iterations (each iteration starts fresh — it's just a counter for logging/mixer handoff). The `_pregen_loop_idx` field (separate) tracks which loop pre-gen is FOR; it's unaffected. The only risk: if a future refactor adds early-return paths between the increment and its use, the counter could skip — but the D0 tests + characterization net would catch that.

---

## Round-1 Adversarial Review — Amendments (03_review.md)

Verdict PLAN_NEEDS_FIX. 3 BLOCKERs folded in; 5 hardest design points verified safe
(signal mapping, active_stems shadowing, B1 watchdog, P11/P12 lock boundary, P13 inner while).

**A1 (BLOCKER, control-flow):** the `if not pregen_ready:` guard (L291) wraps ALL of P4–P9.
Each of the 6 methods covering P4–P9 — `_step_call_conductor`, `_step_parse_actions`,
`_step_build_next_stems`, `_step_submit_jobs`, `_step_await_jobs_fetch`, `_step_tile_audio` —
MUST begin with `if ctx.pregen_ready: return` (the pregen path already has conductor_response
- prepared_tracks from P2). Without it the pregen branch re-calls the LLM + re-submits jobs.

**A2 (BLOCKER, D0-3):** the cache-HIT sketch is vacuous (stop hook flips running=False after
run 1 → run 2 exits immediately) + references non-existent `loop._seed_loop_for_run` +
undefined `_instant_sleep`. Rewrite: seed `loop.stem_cache` with the exact cache_key
(compute it via `loop._build_prompt(track,key,bpm)` → `f"{m_id}_{prompt}_{bpm}_{key}_{bars}"`),
run ONE iteration, assert `_submit_job` not called for that stem.

**A3 (BLOCKER, D0-5):** the transition-recording sketch CANNOT pass: loop_idx=1 → P12
else-branch sets `_pregen_done` → P13 breaks before the transition check fires; plus the
`assert any(... in [])` is always False. DEFER: the record-outside-lock invariant is already
pinned by `test_record_loop_transition_runs_outside_state_lock` (loop-1 path). The P13
*transition-event* path stays acknowledged-untested (as it was pre-refactor); the AST
lock-safety test still guards it structurally.

**CONCERNs adopted:** pin `_loop_idx` increment AFTER the signal returns in D10/D11; note
each extraction converts producer locals → ctx writes (cascading); D0 tests go in
`test_framework_characterization.py` to reuse its autouse `_reset_audit_state` + harness +
`_patch_sleep_instant`; preserve P12's LIVE `state.active_stems` read (don't substitute a
CommitResult snapshot — that changes behavior); drop dead StepCtx fields
(`current_loop_end_sample` has 0 reads/4 writes; bare `next_stems` 0 reads/1 write).
