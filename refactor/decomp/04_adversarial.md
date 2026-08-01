# 04 — Adversarial Regression Review of `_run_loop` Decomposition

Reviewer: code-reviewer subagent (adversarial regression pass). Commit under review:
**fa0ca9b** (`refactor(framework): decompose _run_loop into _step_* methods`).
Ground truth: `git show fa0ca9b` (full diff old-vs-new), current `app/framework/loop_orchestrator.py`,
`refactor/decomp/01_runloop_map.md`, `tests/test_loop_lock_safety.py` + `tests/test_framework_characterization.py`.

Method: line-for-line mapping of the 3 outer-while jumps, the `loop_idx` increment, the
A1 pregen guard, P11 atomicity, P13 inner-while, and B1 try scope — then AST + test-run
verification.

---

## Findings

`AREA | <BLOCKER|CONCERN|SUGGESTION> | old-line -> new-line | <the regression> | <proof>`

### SIGNAL HANDLING (point 1) — VERIFIED CORRECT

SIGNAL | OK | L189/L197/L251(old) -> L213/216/224(new skeleton) | the 3 outer-while jumps map to the right `_StepResult` and the skeleton's break/continue target the OUTER while | **Proof:** `_step_wait_for_start` returns EXIT_LOOP for `not self.running or shutdown_event.is_set()` (old L189 break) and RESTART_ITER for `not still_generating` (old L197 continue); both are returned BEFORE `self._loop_idx += 1`, so the RESTART_ITER does not increment (matches old: L197 continue precedes L204 increment). `_step_pregen_decision` returns RESTART_ITER for `not will_call_llm` (old L251 continue). Skeleton interprets each with `break`/`continue` at `_run_loop` body level inside the `while self.running and state.is_running:` loop → targets the outer while. `continue` after `_step_wait_for_start` re-enters the outer-while body whose FIRST statement is `_step_wait_for_start()`, so it re-enters the wait-while (does NOT skip it).

### LOOP_IDX INCREMENT (point 2) — VERIFIED CORRECT, NO DOUBLE-COUNT

LOOP_IDX | OK | L204(old) -> L320(new) | increment placement identical (after signal checks, before P2) | **Proof:** `grep -n "_loop_idx += 1"` → exactly ONE hit in the file (L320, inside `_step_wait_for_start`), after both early returns. Old increment was also the only one (L204), after L189/L197. Downstream `_step_pregen_decision`/`_step_commit_to_mixer`/`_step_commit_state` read `self._loop_idx` at the same logical point the old code read `loop_idx` (post-increment). A RESTART_ITER at P2 does NOT re-increment (the only increment is in `_step_wait_for_start`, called once per outer iteration); the next outer iteration's PROCEED increments once. EXACTLY matches old (L204 ran once per iteration reaching it, including the L251-continue iteration). No increment-then-restart double-count, no skip.

### PREGEN GUARD A1 (point 3) — VERIFIED CORRECT

PREGEN-GUARD | OK | L289(old `if not pregen_ready:`) -> L240-265(new) | the guard wraps exactly P4-P9 (6 calls); pregen vars flow into C1/P10 | **Proof:** Skeleton wraps `_step_call_conductor` .. `_step_tile_audio` (call_conductor, parse_actions, build_next_stems, submit_jobs, await_jobs_fetch, tile_audio) under `if not pregen_ready:`; `_step_append_audit`/`_step_commit_to_mixer`/`_step_commit_state`/`_step_post_commit`/`_step_await_pregen` are OUTSIDE (identical to old C1/P10/P11/P12/P13). On the pregen path `_step_pregen_decision` returns `(PROCEED, True, conductor_response, prepared_tracks, loop_duration_samples, next_stems)` populated from `self._pregen_results` — not stale/empty. `_step_commit_to_mixer` re-reads `self._pregen_results["prepared_tracks"]`/`["loop_duration_samples"]` exactly as old P10 did; `self._pregen_results` is stable between P2 and P10 (no mutation; P4-P9 skipped).

### P11 ATOMICITY (point 4) — VERIFIED CORRECT

P11-ATOMICITY | OK | L481-554(old single lock) -> `_step_commit_state` single lock | exactly ONE state.lock; record_loop_transition outside; live active_stems read | **Proof:** AST walk of `_step_commit_state` → exactly **1** `async with state.lock:` block containing the entire rotation/history/pregen-metadata/recording-capture/override/snapshot transition. AST walk of `_step_post_commit` → **0** state.lock blocks + **1** `record_loop_transition(1, commit.rec_stems, ...)` call (outside any lock). The else-branch reads `list(state.active_stems)` LIVE (unlocked) — matches old; the `_step_post_commit` docstring explicitly warns against substituting a snapshot (CONCERN-5). `tests/test_loop_lock_safety.py::test_record_loop_transition_runs_outside_state_lock` spies `state.lock.locked()` at call time and asserts False — **PASSES**.

### P13 INNER WHILE (point 5) — VERIFIED CORRECT (verbatim)

P13 | OK | L586-620(old) -> `_step_await_pregen` | inner `while self.running:` + 2 breaks preserved verbatim; outer re-check unchanged | **Proof:** `_step_await_pregen` is byte-for-byte the old P13 block (guard `if self.running and not state.shutdown_event.is_set():`, inner `while self.running:`, the transition-recording lock+record, `_pregen_done` break, mixer.lock current_ahead read, `current_ahead < 0.5` break, `await asyncio.sleep(0.25)`). Both breaks are LOCAL (end the inner while → method returns); the OUTER while re-checks `self.running and state.is_running` after `_step_await_pregen()` returns as the try block's last statement. Identical to old.

### B1 TRY SCOPE (point 6) — VERIFIED CORRECT

B1-SCOPE | OK | L181-622(old try) -> L210-289(new try) | all _step_* calls inside B1; P4 nested try preserved | **Proof:** AST walk of `_run_loop` → the B1 `try` contains all **14** `_step_*` awaited calls (`_step_wait_for_start` through `_step_await_pregen`); **0** `_step_*` calls outside the try. `_step_call_conductor` contains exactly **1** nested `try/except` (the swallow→`build_fallback_response`), so a conductor exception is caught by the NESTED except (fallback), NOT propagated to B1 (which would retry). `tests/test_framework_characterization.py::test_d0_p4_conductor_failure_uses_fallback` drives this — **PASSES** (tracks committed, no crash/retry).

### Items the 573-test net CANNOT catch (point 7)

UNTESTED-PATHS | SUGGESTION | L586-620(old) → `_step_await_pregen` | P13 transition-recording + `current_ahead<0.5` break remain untested | **Proof:** the fake mixer's `pop_transition_event()` returns None and `_pregen_done` fires first, so the `if transitioned_loop_idx is not None` branch and the `current_ahead < 0.5` break never execute in tests. This is a PRE-EXISTING coverage gap (map §E: "High"), NOT a regression — the refactor preserved P13 verbatim. The outside-lock invariant for that branch is pinned indirectly by `test_record_loop_transition_runs_outside_state_lock` (same call shape). Recommend: a fake mixer that emits a transition event to exercise it.

### Pre-existing dead code preserved faithfully (observation)

DEAD-CODE | SUGGESTION | L260(old) -> dropped in `_step_read_state` | reset branch no longer assigns `current_loop_end_sample = 0` | **Proof:** grep shows the spanning LOCAL `current_loop_end_sample` was WRITE-ONLY in the old code (L178/L260/L459/L468 all writes; the only READS at L457/L610 are `self.mixer.current_loop_end_sample`, the mixer attribute, not the local). Dropping the dead `= 0` is a no-op. NOTE: this means `state.should_reset` never actually reset the mixer's live `current_loop_end_sample` (the reset cleared only the dead local) — a latent pre-existing bug, faithfully carried forward, not introduced here. New `_step_commit_to_mixer` still computes and returns `current_loop_end_sample` (now discarded as `_current_loop_end_sample`), preserving the `mixer.lock`-wrapped reads identically.

### Minor observation (the P2 `active_stems` read dropped)

SHADOW-LOCAL | SUGGESTION | L229(old pregen `active_stems = list(state.active_stems)`) -> dropped | no behavior change | **Proof:** the old pregen-branch `active_stems` was overwritten by P3's `active_stems = list(state.active_stems)` (under a fresh lock) before any use — dead. `_step_pregen_decision` correctly drops it; `_step_read_state` supplies the value all downstream `_step_*` consume. The `_step_pregen_decision` docstring documents this explicitly.

---

## Verdict

**NO_REGRESSIONS.** The decomposition preserves EXACT semantics across all 7 attack
vectors. Every outer-while jump, the loop_idx increment placement, the A1 pregen guard,
the P11 single-lock atomicity, the P13 inner-while, and the B1 try scope are faithful to
the original. The `_CommitResult` dataclass threads P11→P12 handoff correctly;
`record_loop_transition` stays outside the lock; `_step_post_commit` reads `state.active_stems`
live. The two untested-but-preserved paths (P13 transition recording, P13 current_ahead break)
are pre-existing gaps, faithfully carried verbatim — not regressions.

Test evidence (run on current working tree, post-fa0ca9b):
`test_loop_lock_safety.py + test_framework_characterization.py` → 15 passed.
`+ test_state.py + test_async_framework.py + test_mixer.py` → 109 passed.
