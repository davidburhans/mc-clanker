# 01 — `_run_loop` Decomposition Map (current code)

Agent: `codebase-analyzer` (fresh context). Source: `app/framework/loop_orchestrator.py`,
`_run_loop` at **L169–637**. (`framework_main_async.py` is a pure re-export shim.)
Line ranges below are CURRENT (the explore doc `01_anatomy_boundaries.md` cites pre-split numbers).

## (A) Phase table

`try:` opens L181 (12-space), closes at `except` L622. P0 is before the while/try; P1–P13 inside the try; P14 is the except.

| # | Phase | Lines | Spanning READS (earlier) | Spanning WRITES (later) | Control flow (target) | In try? |
| --- | --- | --- | --- | --- | --- | --- |
| P0 | init | 177–178 | — | `loop_idx`, `current_loop_end_sample` | — | No |
| P1 | wait-for-start | 182–210 | `loop_idx` | `loop_idx` (incr 202) | wait-while 183–186; **break 189→outer** (shutdown); **continue 196→outer** (not still_generating) | Yes |
| P2 | pregen-decision | 211–251 | `loop_idx` | `pregen_ready`; pregen branch: `conductor_response`,`prepared_tracks`,`loop_duration_samples`,`next_stems`,`active_stems` | **continue 249→outer** (will_call_llm False) | Yes |
| P3 | read-state-snapshot | 253–288 | `current_loop_end_sample` | `bpm_override`,`key_override`,`current_bpm`,`current_key`,`active_stems`(shadows P2),`user_override`,`available_instruments`,`stem_history`,`llm_config`,`available_models` | none | Yes |
| P4 | call-conductor | 289–305 | `pregen_ready`; P3 snapshot vars | `conductor_response` | **nested local try/except 292–305 swallows→fallback** (never reaches B1) | Yes (nested try) |
| P5 | parse-actions + audit-log | 307–329 | `conductor_response`,`active_stems` | `deduped_tracks` | none | Yes |
| P6 | build-next-stems | 330–365 | `bpm_override/key_override`,`conductor_response`,`current_bpm/key`,`deduped_tracks` | `local_next_stems`,`local_current_bpm`,`local_current_key` | none | Yes |
| P7 | submit-jobs | 367–394 | `local_*` (P6); `self.stem_cache` | `pending_jobs` | **continue 380→for-loop** (cache HIT, local) | Yes |
| P8 | await-jobs + fetch | 396–419 | `pending_jobs`,`local_next_stems`,`self.stem_cache` | mutates `self.stem_cache`, `state.cache_stem` | none | Yes |
| P9 | tile-audio | 421–432 | `local_*`,`self.stem_cache`,`mixer.sample_rate`,`deduped_tracks` | `prepared_tracks`,`loop_duration_samples` | none | Yes |
| C1 | audit append | 434–436 | `conductor_response`,`active_stems`,`loop_idx` | — | (the call the B1 test throws from) | Yes |
| P10 | commit-to-mixer | 438–468 | `pregen_ready`,`prepared_tracks/loop_duration_samples`,`loop_idx`,`self.mixer` | `tracks_to_use`,`duration_samples`,`current_loop_end_sample` | none | Yes |
| P11 | update-state-commit | 470–554 | `pregen_ready`,`_pregen_results`,`loop_idx`,`tracks_to_use/duration_samples` | `needs_pregen`,`needs_initial_record`,`_rec_*`,`state_snapshot` | **ONE `state.lock` 482–554** | Yes |
| P12 | record + cache-maint + pregen-spawn | 555–584 | P11 outputs, `self.stem_cache`,`loop_idx`,`state_snapshot`,`tracks_to_use/duration_samples` | spawns `_pregen_task`; sets `_pregen_done/_pregen_results` | `record_loop_transition` OUTSIDE lock | Yes |
| P13 | await-pregen / transition-record | 586–620 | `self.running`,`shutdown_event`,`mixer`,`loop_idx`,`state.active_stems/...` | `state.record_loop_transition` | **inner while self.running 589**; **break 603→inner** (pregen_done); **break 619→inner** (current_ahead<0.5) | Yes |
| P14 | error-handlers | 622–635 | — | — | **raise 625** (cancel); **continue 635→outer** (B1 retry) | Is the except |
| post | normal exit | 637 | — | — | `self._finish_loop()` | No |

## (B) Spanning-locals graph

`loop_idx` (P0/P1 → 7+ phases), `pregen_ready` (P2 → 9 phases), `active_stems` (P2/P3 → 4), `conductor_response` (P2/P4 → 3), `local_next_stems/bpm/key` (P6 → P7/P8/P9), `deduped_tracks` (P5 → P6/P9), P11 output bundle (→ P12).

→ Collapse into a per-iteration `StepCtx` dataclass. **`loop_idx` is the only one that survives across iterations → promote to `self._loop_idx`.**

## (C) Control-flow hazards

1. **Outer `while`** (180): `while self.running and state.is_running:`. Breaks: L189 (shutdown), normal exit when flags flip. The test stop hook flips **`self.running`** (P13 spins on it — setting only `state.is_running` does NOT break P13).
2. **P1 wait inner-while** (183–186): no break; exits on condition. `await` must stay in B1 try.
3. **P13 inner-while** (589): `while self.running:`; break L603 (pregen_done), L619 (current_ahead<0.5). Both end the iteration.
4. **B1 watchdog try** (181–622): **all P1–P13 inside**. `_step_*` calls MUST stay textually inside it or retry is lost. P4's nested try swallows → must NOT be hoisted/merged with B1.
5. **Cross-phase jumps** (only 3, all → outer while): `break` L189, `continue` L196, `continue` L249. These cannot be plain `return` → need a `_StepResult` signal enum. All other breaks/continues are local.
6. **Two-lock discipline**: `state.lock` (asyncio, no await/open inside — AST-enforced file-wide); `mixer.lock` (threading, sync reads); `state.sync_lock` (threading, inside `record_loop_transition`) → must run OUTSIDE `state.lock`.

## (D) Proposed `_step_*` decomposition

Signal: `_StepResult{PROCEED, RESTART_ITER, EXIT_LOOP}`. Skeleton stays in `_run_loop`: P0 init, outer while, B1 try, sequence of `_step_*` calls interpreting the enum, P14 excepts.

| # | method | phases | lines | reads (params) | writes (return/side-fx) | LOC |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `_step_wait_for_start(ctx)` | P1 | 182–210 | ctx.loop_idx | incr loop_idx; RESTART_ITER/EXIT_LOOP/PROCEED | ~29 |
| 2 | `_step_pregen_decision(ctx)` | P2 | 211–251 | ctx.loop_idx | pregen_ready + 5 pregen vars; RESTART_ITER/PROCEED | ~41 |
| 3 | `_step_read_state(ctx)` | P3 | 253–288 | ctx.loop_idx, current_loop_end_sample | snapshot vars | ~36 |
| 4 | `_step_call_conductor(ctx)` | P4 | 289–305 | pregen_ready + snapshot | conductor_response (only if !pregen); **keep nested try** | ~17 |
| 5 | `_step_parse_actions(ctx)` | P5 | 307–329 | conductor_response, active_stems | deduped_tracks; state.last_actions | ~23 |
| 6 | `_step_build_next_stems(ctx)` | P6 | 330–365 | overrides, conductor_response, bpm/key, deduped | local_next_stems/bpm/key | ~36 |
| 7 | `_step_submit_jobs(ctx)` | P7 | 367–394 | local_*, stem_cache | pending_jobs (for-continue local) | ~28 |
| 8 | `_step_await_jobs_fetch(ctx)` | P8 | 396–419 | pending_jobs, local_next_stems | stem_cache, state.cache_stem | ~24 |
| 9 | `_step_tile_audio(ctx)` | P9 | 421–432 | local_*, stem_cache, mixer.sr, deduped | prepared_tracks, loop_duration_samples | ~12 |
| 10 | `_step_append_audit(ctx)` | C1 | 434–436 | conductor_response, active_stems, loop_idx | — | ~3 |
| 11 | `_step_commit_to_mixer(ctx)` | P10 | 438–468 | pregen_ready, prepared_tracks, loop_idx, mixer | tracks_to_use, duration_samples, current_loop_end_sample | ~31 |
| 12 | **`_step_commit_state(ctx) -> CommitResult`** | P11 | 470–554 | pregen_ready, loop_idx, tracks_to_use, duration_samples | {needs_pregen, needs_initial_record, _rec_*, state_snapshot}; **ONE state.lock** | **~84** ⚠️ |
| 13 | `_step_post_commit(ctx, commit)` | P12 | 555–584 | commit.*, stem_cache, loop_idx, tracks | spawn pregen; record_loop_transition OUTSIDE lock | ~30 |
| 14 | `_step_await_pregen(ctx)` | P13 | 586–620 | self.running, shutdown, mixer, loop_idx | record_loop_transition (outside lock) | ~35 |
| — | P14 stays inline | P14 | 622–635 | — | B1 retry continue / cancel raise | ~14 |

All ≤50 LOC except `_step_commit_state` (~84) — the explicit atomic-lock exception.

**Hardest extractions:** (1) P2 owns an outer-while `continue` (L249) → signal; (2) P11 atomic lock — keep as one method/one lock block, AST test still guards it; (3) P13 inner `while self.running:` + untested transition path — extract verbatim.

## (E) Verification (test coverage per phase)

Pinned: P5/P10/P12 (Gap 4/5/7), P2/P11 (Gap 6), P8 cache-stem divergence, P14/C1 (watchdog), all state.lock blocks (AST), record_loop_transition-outside-lock.

**Weak / NO targeted test (silent-regression risk):**

- P3 override apply/clear order — no test. **Medium.**
- P4 fallback branch — never driven through `_run_loop`. **Medium.**
- P7 cache-HIT `continue` (L380) — never hit (empty cache). **Medium.**
- P8 real fetch+decode — never exercised ({} results). **Medium.**
- P13 transition recording + `current_ahead<0.5` break — unreachable (fake mixer pop→None; pregen_done fires first). **High.**

**Extractions that could pass green while silently changing behavior:** P3 override order; P7 cache-HIT drop; P13 transition/snapshot-outside-lock; P4 fallback hoist; P2 L249 continue.
