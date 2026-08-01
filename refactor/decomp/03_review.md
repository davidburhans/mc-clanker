# 03 — Adversarial Review of `_run_loop` Decomposition Plan

**Reviewer:** review subagent (fresh context). **Source of truth:** firsthand reads of
`app/framework/loop_orchestrator.py` `_run_loop` (L177–637), AST verification, and the
two test files. Baseline confirmed: `569 passed, 58 skipped, 9 xfailed, 16 xpassed`.

**Method:** every claim below is grounded in the actual code (line numbers from current
HEAD, verified via `grep -n` and `ast`). Where the plan/map cite line anchors that
differ from current code, the actual line is noted.

---

## Findings

### BLOCKER-1 — P4–P9 pregen guard omitted from skeleton AND hazard tables

**AREA:** SIGNAL/CONTROL-FLOW | BLOCKER | plan §2 skeleton + §4 D4–D9 hazard rows | L291

The current code wraps **ALL of P4–P9** (conductor call, parse-actions, build-next,
submit-jobs, await-jobs, tile-audio) inside a single `if not pregen_ready:` block at
**L291** (confirmed via AST: `If` at L291, body L292–432). On the pregen path this entire
block is skipped — the conductor is never called, no jobs submitted, `state.next_stems`
is not written.

The plan's skeleton (§2) calls all six `_step_*` methods **unconditionally**:

```python
await self._step_call_conductor(ctx)
await self._step_parse_actions(ctx)
await self._step_build_next_stems(ctx)
await self._step_submit_jobs(ctx)
await self._step_await_jobs_fetch(ctx)
await self._step_tile_audio(ctx)
```

The per-extraction hazard tables (§4, §5) for D4–D9 mention nested-try preservation
(D4), cache-HIT continue (D5), cache_stem under lock (D6), etc. — but **none mention the
`if not pregen_ready:` guard**. A mechanical extraction that moves each phase's body into
a method without adding the guard would, on the pregen path: call the LLM, overwrite
`state.next_stems` (P6, under lock), submit jobs (P7), wait for them (P8) — a complete
behavioral regression.

**Mitigation:** the Gap-6 characterization test (`test_run_loop_pregen_and_fresh_paths_state_shaping`)
exercises the pregen path and would likely catch this. But the entire point of a
plan-level review is to catch this **before code is written**.

**Fix:** Add to the plan — either in the skeleton (wrapping P4–P9 calls in
`if not ctx.pregen_ready:`) or, preferably, as an explicit hazard-row note for D4–D9:

> Each of `_step_call_conductor`, `_step_parse_actions`, `_step_build_next_stems`,
> `_step_submit_jobs`, `_step_await_jobs_fetch`, `_step_tile_audio` MUST begin with
> `if ctx.pregen_ready: return` to replicate the single `if not pregen_ready:` guard
> at L291 that wraps all six phases. Currently the guard is shared; extraction into
> separate methods requires replicating it in each.

---

### BLOCKER-2 — D0-3 (`test_p7_cached_stem_skips_job_submission`) is a vacuous pass

**AREA:** D0 TESTS | BLOCKER | plan §3 D0-3 | test would pass for the wrong reason

Three bugs in this sketch:

1. **Vacuous pass.** The stop hook (`stop_on=("add", 1)`) flips `loop.running = False`
   after the first run's first `_add_track_internal`. The second
   `await loop._run_loop()` sees `self.running == False`, so the outer
   `while self.running and state.is_running:` exits **immediately** — the body never
   executes. `submit_mock.assert_not_called()` then passes because the loop didn't run
   at all, NOT because of a cache hit. The test asserts the wrong thing.

2. **`loop._seed_loop_for_run`** (bare expression, with a `# state still has stem_cache
   populated` comment) references a non-existent attribute. `_seed_loop_for_run` is a
   module-level function in the harness, not a method on `AsyncFrameworkLoop`. This line
   raises `AttributeError` (or is a dead no-op if somehow suppressed).

3. **`_instant_sleep`** is undefined (see CONCERN-4).

**Fix:** To genuinely test cache-HIT: run once to populate `self.stem_cache`, then
re-seed `loop.running = True` + `state.is_running = True` + re-wire the stop hook for
the second run. Use the same `loop` object (stem_cache persists on `self`). Verify
`submit_mock.assert_not_called()` only after confirming the second run actually reached
P7 (e.g., assert `mixer.add_track_internal_calls` grew).

---

### BLOCKER-3 — D0-5 (`test_p13_transition_event_recorded_outside_lock`) has three independent logic bugs

**AREA:** D0 TESTS | BLOCKER | plan §3 D0-5 | test cannot pass as written

1. **Always-false assertion.** `assert any(idx == 2 for idx, _, _, _ in [])` iterates an
   empty list literal `[]`. `any()` over zero elements is `False`. This assertion
   **always fails** regardless of behavior.

2. **Unreachable transition on loop_idx=1.** The test starts at loop_idx=1. On loop 1,
   `needs_pregen = loop_idx > 1 and ...` is `False`, so P12's else-branch runs and calls
   `self._pregen_done.set()`. In P13's inner while, the **first** thing after
   `pop_transition_event()` is `if self._pregen_done.is_set(): break`. Since
   `_pregen_done` is set, the while breaks **before** the 2nd `pop_transition_event()`
   call (the one that would return `2`). The spy (`record_loop_transition`) never fires
   → `lock_states` is empty → `assert lock_states` fails.

3. The `_no_pregen` override doesn't help: it only prevents the *background task* from
   setting `_pregen_done`, but P12's else-branch sets it synchronously.

**Fix:** The P13 transition path requires `loop_idx > 1` AND `needs_pregen = True`
(so the else-branch doesn't set `_pregen_done`). This means the test must reach at least
loop 2 with a live (or mocked) pregen task. This is the "High" untested path from map §E.
Consider: drive two iterations (stop_on for loop 2), mock
`_pre_generate_next_loop` to NOT set `_pregen_done`, and arrange
`pop_transition_event` to return a value. This is non-trivial — the existing
`test_record_loop_transition_runs_outside_state_lock` already covers the loop-1
`needs_initial_record` path; P13's transition path may need a dedicated multi-iteration
harness or be deferred with an explicit "untested" acknowledgment.

---

### CONCERN-1 — `_loop_idx` increment placement not pinned relative to signal returns

**AREA:** SIGNAL SEMANTICS | CONCERN | plan §2 summary | L204 (actual; plan says L202)

Current code: `loop_idx += 1` is at **L204**, AFTER both the shutdown `break` (L189) and
the not-still-generating `continue` (L197). So the counter increments only on the PROCEED
path — exactly once per real iteration.

The plan says `_step_wait_for_start` "increments it" and references "P1 L202" but does
not explicitly state the increment must come **after** the `return EXIT_LOOP` and
`return RESTART_ITER` statements. If an implementer places `self._loop_idx += 1` at the
top of `_step_wait_for_start` (before the wait-while or before the returns), the counter
would grow on every wait/restart cycle. In production (where `is_generating` starts
`False` and the wait-while spins), this would make the **first real iteration** have
`_loop_idx > 1`, breaking:

- `pregen_ready = (loop_idx > 1 and ...)` → wrongly True if `_pregen_results` matches.
- `if loop_idx == 1:` mixer branch → takes the wrong path (set_next_loop instead of
  add-at-current-position).
- `needs_pregen = loop_idx > 1 and ...` → wrongly True.

The characterization tests would NOT catch this: `_seed_loop_for_run` sets
`state.is_generating = True`, so the wait-while exits immediately with zero restart
cycles. This is a **production-only silent regression** risk.

**Fix:** Add explicit note: "`self._loop_idx += 1` must occur AFTER the EXIT_LOOP and
RESTART_ITER returns in `_step_wait_for_start`, matching current L204. Placing it before
the returns breaks the `loop_idx == 1` gate and the pregen-ready gate."

---

### CONCERN-2 — Extraction order requires cascading ctx-conversion not documented

**AREA:** EXTRACTION ORDER | CONCERN | plan §4 D1–D9 | safest-first claim

D1 extracts `_step_tile_audio` (P9) first. P9 reads `local_next_stems`,
`local_current_bpm`, `local_current_key` (set by P6, still inline), `deduped_tracks`
(set by P5, still inline). The extracted method reads `ctx.local_next_stems` etc., but
at D1 the still-inline P6/P5 write to **local variables**, not ctx fields. So
`ctx.local_next_stems` would be the default `[]`.

The implementer must convert producer-phase assignments to ctx writes at each extraction
commit (e.g., P6's `local_next_stems = list(state.next_stems)` →
`ctx.local_next_stems = list(state.next_stems)`). This cascades backward: extracting P9
forces touching P6; extracting C1 (D2, reads `conductor_response` + `active_stems`)
forces touching P2/P3/P4. The plan calls this "safest-first; leaf methods with no
control-flow jumps first" without noting the ctx-bridging requirement.

**Risk:** an implementer who extracts P9's body verbatim (reading `ctx.*`) without
updating the still-inline producers gets `ctx.local_next_stems = []` → `tile_to_loop`
produces empty/wrong tracks. The Gap-4/5 mixer-handoff tests would likely catch wrong
track counts, but the plan should be explicit.

**Fix:** Add note: "Each extraction converts consumed locals to ctx-field reads; the
still-inline producer phases must be updated to write ctx fields at that commit. D1
(P9) requires P5/P6 to write `ctx.deduped_tracks` / `ctx.local_*`; D2 (C1) requires
P2/P3/P4 to write `ctx.conductor_response` / `ctx.active_stems`; etc."

---

### CONCERN-3 — D0 tests lack state-singleton isolation (autouse fixture is module-scoped)

**AREA:** D0 TESTS | CONCERN | plan §3 D0-1 | test isolation

The D0 tests live in a NEW file (`tests/test_runloop_decomp.py`). They import harness
helpers (`_seed_loop_for_run`, `_wire_loop_no_io`, etc.) from
`test_framework_characterization.py`. However, the autouse fixture
`_reset_loop_state` (which snapshots/restores ~21 state attributes) is
**module-scoped** to `test_framework_characterization.py` — importing the helpers does
NOT import the fixture. D0-1 mutates `state.target_bpm_override = 140` and
`state.target_key_override = "F major"` directly (not via monkeypatch) → these leak to
other tests via the `state` singleton.

**Fix:** The D0 test module must define its own autouse state-reset fixture (copy the
~21-attr snapshot/restore from `_reset_loop_state`) or import and apply the fixture
explicitly.

---

### CONCERN-4 — `_instant_sleep` is undefined in D0-2/D0-3/D0-4 sketches

**AREA:** D0 TESTS | CONCERN | plan §3 D0-2, D0-3, D0-4 | NameError at runtime

D0-2, D0-3, D0-4 all call `monkeypatch.setattr(asyncio, "sleep", _instant_sleep)`. But
`_instant_sleep` does not exist. The harness defines `_patch_sleep_instant(monkeypatch)`
which internally defines `async def _instant(_delay=None)`. The sketches should call
`_patch_sleep_instant(monkeypatch)` instead. (D0-1 and D0-5 use `_wire_loop_no_io` which
calls `_patch_sleep_instant` internally — those are fine on this point.)

**Fix:** Replace `monkeypatch.setattr(asyncio, "sleep", _instant_sleep)` with
`_patch_sleep_instant(monkeypatch)` (imported from the harness) in D0-2/D0-3/D0-4.

---

### CONCERN-5 — P12 else-branch live read of `state.active_stems` (pre-existing, but plan must preserve it)

**AREA:** P11/P12 SPLIT | CONCERN | plan §1 CommitResult + §5 D12 | L580

P12's else-branch (`needs_pregen is False`) reads `list(state.active_stems)` **without
`state.lock`** to populate `self._pregen_results["next_stems"]`. This is a pre-existing
unprotected read (not a regression). The plan's `CommitResult` does NOT carry
`active_stems` for this purpose. An implementer might "improve" this by reading from a
lock-snapshotted `CommitResult.state_snapshot["active_stems"]` — which would **change
behavior** (the current code reads live, not snapshotted). The plan should note:
"preserve the live `list(state.active_stems)` read in `_step_post_commit`'s else-branch;
do not substitute a lock-snapshotted value."

---

### CONCERN-6 — Map/plan line anchors off by 1–2 from current code

**AREA:** DOCUMENTATION | CONCERN | map §A, plan §2/§4 |多处

The plan claims "All line anchors verified against current code (post-Phase-9 ruff
fix)." Actual vs cited (verified via `grep -n`):

| Anchor | Plan/Map says | Actual |
| -------- | -------------- | -------- |
| `loop_idx += 1` | L202 | **L204** |
| not-still-generating `continue` | L196 | **L197** |
| will_call_llm `continue` | L249 | **L251** |

Minor, but a line-based mechanical extraction could target the wrong statement.

**Fix:** Re-verify anchors against current HEAD before extraction, or rely on semantic
matching (variable names, not line numbers).

---

### SUGGESTION-1 — Dead locals: `current_loop_end_sample` and bare `next_stems`

**AREA:** StepCtx DESIGN | SUGGESTION | plan §1 StepCtx | AST-verified

AST analysis of `_run_loop` confirms:

- `current_loop_end_sample`: **0 reads**, 4 writes (L178, L260, L459, L468). Dead local.
- Bare-name `next_stems` (the pregen-branch assignment at L242): **0 reads**, 1 write.
  (P11 reads `self._pregen_results["next_stems"]`, not the local.) Dead local.

The plan's StepCtx carries `current_loop_end_sample: int = 0`. Since it's dead (never
read), the fresh-ctx-per-iteration reset is harmless. But an implementer seeing it in
StepCtx might assume it's meaningful and add reads. Consider dropping it from StepCtx
and noting both as dead.

---

### SUGGESTION-2 — Redundant `ctx = StepCtx()` before the while loop

**AREA:** SKELETON | SUGGESTION | plan §2

The skeleton creates `ctx = StepCtx()` once before the while loop AND again inside the
while/try on every iteration. The pre-while instance is immediately shadowed and never
used. Harmless but confusing — drop the pre-while line.

---

## Verified Safe (no action needed)

- **Signal mapping (attack pt 1):** `EXIT_LOOP → break`, `RESTART_ITER → continue`
  correctly mapped in the skeleton. Confirmed via AST that the wait-while is the first
  statement in the try body, so `continue` (from L197 or L251) re-enters the wait-while
  on the next outer iteration — same as today. ✓
- **`active_stems` shadowing (attack pt 2):** P2 (L226) and P3 (L275) both read
  `state.active_stems` under lock with no mutation between them → identical values. P3
  always overwrites. A single `ctx.active_stems` populated by `_step_read_state` carries
  the correct value into C1/P5/P6. ✓
- **B1 watchdog coverage (attack pt 3):** all `_step_*` calls are inside B1's try.
  P4's nested try stays inside `_step_call_conductor` (plan §5 D4 hazard explicitly
  says "do NOT merge with B1"). No `await` escapes B1's protection. ✓
- **P11/P12 lock boundary (attack pt 4):** `_step_commit_state` acquires/releases
  `state.lock`, returns `CommitResult`; `_step_post_commit` runs after return (lock
  released). `CommitResult` carries all P11-under-lock outputs (`needs_initial_record`,
  `_rec_*`, `state_snapshot`). ✓
- **P13 inner while (attack pt 5):** P13 is the last phase in the try body. Extracting
  `while self.running:` verbatim into `_step_await_pregen` means: inner while exits →
  method returns → try body ends → outer while re-checks `self.running and
  state.is_running`. Same as today (where P13's inner while exit ends the try). ✓

---

## VERDICT: PLAN_NEEDS_FIX

**3 fixes required before extraction begins:**

1. **BLOCKER-1:** Add the `if ctx.pregen_ready: return` guard requirement to D4–D9
   (or wrap P4–P9 calls in the skeleton). This is the single most dangerous omission —
   without it, the pregen path calls the LLM and overwrites state.
2. **BLOCKER-2 + BLOCKER-3:** Rewrite D0-3 and D0-5 (or mark them as "to be redesigned
   during implementation" with the specific bugs noted). D0-3 passes vacuously; D0-5
   cannot pass at all. D0-1/D0-2/D0-4 are fixable with CONCERN-3 (isolation fixture)
   and CONCERN-4 (`_instant_sleep` → `_patch_sleep_instant`).
3. **CONCERN-1:** Pin `self._loop_idx += 1` placement to AFTER the signal returns in
   `_step_wait_for_start`, with a note that misplacement causes a production-only silent
   regression invisible to the test harness.

CONCERN-2 (cascading ctx-conversion) and CONCERN-5 (live-read preservation) should also
be documented but are lower risk — a competent implementer following "verify 574+ green
at every commit" would surface them through test failures.
