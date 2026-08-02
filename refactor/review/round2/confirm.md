# Round-2 Confirm Pass — E1–E6 Refactor Plan (amended)

**Scope:** confirm ONLY that round-1 BLOCKERs A1–A5 are closed in the amended
plan text, scoped to phases 0, 6, 7a, 7b, 9 + DoD §2/§3 + risk register R11–R14

+ §6 reconciliation table. Then hunt for NEW issues introduced by the amendments.
**Not** a fresh critique.

**Ground evidence read:** `refactor/plan.md`; `refactor/review/round1/redteam_sequencing.md`;
`refactor/review/round1/redteam_verification.md`; `app/framework/framework_main_async.py`
(L194–213 init; L523–547 fg cache; L616–692 P11 commit; L1066–1111 pregen cache;
L70–118 flush). Read-only commands only; nothing staged.

---

## BLOCKER reconciliation

| BLOCKER | STATUS | Evidence (plan.md section) | Residual concern |
| --- | --- | --- | — |
| A1 — safety net must include HIGH gaps 6/7/8 before 7a | **RESOLVED** | Phase 0 lists Gap 6 `test_run_loop_pregen_and_fresh_paths_produce_equal_state_delta` ("REQUIRED before 7a"), Gap 7 `test_run_loop_populates_last_actions`, Gap 8 `test_flush_recording_buffers_writes_db_and_requeues_on_failure`. Harness spec strengthened (`_FakeMixer` records + stop-after-N, `asyncio.sleep`→instant, ~15-attr state save/restore). Gap 8 is satisfiable at Phase 0: `flush_recording_buffers` (still module fn pre-Phase-3) gets its session via `DatabaseManager.get_instance().session()` (L96) → injectable via monkeypatch, no ports needed. | None |
| A2 — PreGenerator stem_cache ownership | **RESOLVED** | Phase 6 mandates `PreGenerator(stem_cache=self.stem_cache, …)` "MUST share the loop's SINGLE self.stem_cache dict (L203), NOT get its own". Adds the 3 tests: (a) `test_pregen_skips_job_when_foreground_already_cached`, (b) `test_cache_key_is_identical_across_all_4_sites` (pure `make_cache_key`), (c) `np.any(track != 0)` non-silence. R11 row mirrors. Verified: 4 inline key sites exist (L485/544/1066/1108); fg write L523 vs pregen write L1096 both target `self.stem_cache`; `state.cache_stem` has exactly ONE call site L527 (fg only). | None |
| A3 — ≤20-line-step goal unachievable | **RESOLVED** | DoD §2 revised to "≤50 lines (≤20 where achievable) EXCEPT `_step_commit_state` (~85–90, single-lock atomic)". R13 row mirrors. Measured achievability: all non-commit steps ≤50 (P3=50 boundary, but `_load_available_models` lifts to Phase 4 → shrinks); commit lock body ~73 LOC (L616–688), ≤90 holds. | None |
| A4 — P11 atomicity verified by test, not comment | **RESOLVED** | Phase 7a RISK #3: "VERIFY WITH A TEST, not just a comment: `test_step_commit_state_is_single_lock_block` — patch `state.record_loop_transition` to assert `state.lock.locked()` is False at call time … and assert the full mutation set applied atomically." R3 row mirrors. Verified: `record_loop_transition` (L692) is lexically OUTSIDE the `async with state.lock:` block (ends ~L688); comment at L611 already documents sync_lock reason. | None |
| A5 — ruff F401 + stale 735 baseline | **RESOLVED** | Phase 9 step (1) captures dynamic re-baseline to `refactor/ruff_baseline.txt` ("do NOT trust a hardcoded number"); step (2) `--select UP006,UP007,UP045,F401 --fix` includes F401 in the SAME pass; DoD §3 "must not increase vs the re-baseline … do NOT hardcode 735"; universal per-phase gate now includes `F401`; R12 row mirrors. | None |

---

## NEW issues introduced by the amendments

1. **[NOTE, non-blocking] Phase 7b default-port construction must be side-effect-free at `__init__` time.**
   Current `__init__` (L194–207) sets `self._garage = None` and lazily creates the
   garage client only on first use (`garage` property, L209–213). The amended
   Phase 7b signature `__init__(self, session_id, *, … audio=None …)` states each
   `None` default "constructs the real adapter" (`GarageAudioAdapter()`). If
   `GarageAdapter.__init__` (written in Phase 2) eagerly calls
   `create_garage_client_from_env()`, then `AsyncFrameworkLoop(session_id)` would
   now eagerly create an S3 client at construction — a behavioral change. Verified
   all 14 call sites use exactly one arg (`app_ui` L1164 + 13 tests incl.
   `session_id=None` at `test_worker_fetch_audio.py:65`), so the keyword-default
   signature itself preserves them; the risk is only an eager adapter. The
   frozen-API instantiation test (`AsyncFrameworkLoop(uuid4())`, Phase 0) is a
   safety net if the adapter crashes, but a silently-degraded client would not be
   caught until a fetch. *Recommendation: state in Phase 2/7b that
   `GarageAudioAdapter()` construction is lazy/no-op (mirrors current property).
   Same ordering care applies to `PreGenerator(stem_cache=self.stem_cache)` — the
   plan already says "the stem_cache/*pregen** instance attrs are still set in
   **init**"; ensure `self.stem_cache` is initialized BEFORE the PreGenerator
   default is constructed in the **init** body (default-arg `None` is evaluated at
   def time, so the construction must be in the body, not the signature).*

2. **[NOTE, non-blocking] Phase 0 frozen-API parenthetical over-claims no-op coverage.**
   Phase 0 says the frozen-API test asserts `getattr(module,'decode_aac')` +
   `create_garage_client_from_env` "resolve to callables (catches the string-patch
   silent-no-op, brief-02 §D)". Callable-resolution does NOT detect the no-op
   (round-1 red-team established this: the re-export stays callable while the real
   binding moves). The REAL protection is the Phase 2 guard test
   `test_decode_aac_patch_actually_applies` + migrated patch strings, which the
   plan correctly attributes in reconciliation A7/A10. Wording-only inaccuracy in
   one parenthetical; protection exists and is correctly placed.

No further new issues found. The amendments are internally consistent: §6
reconciliation A1–A5 map 1:1 to the phase-spec changes and the R11–R14 risk rows;
line-ref drift (A14) is corrected (P11 L616–688 ✓, cache write L1096 ✓, verified
against source).

**New-issue check on the three explicit hunt questions:**
+ Injectable keyword-default preserves the one-arg call (`*` after `session_id`
  keeps `AsyncFrameworkLoop(session_id)` / `session_id=None` working) — **YES**,
  subject to NOTE 1 (lazy adapter).
+ Gaps 6/7/8 in Phase 0 reference only the CURRENT monolithic `_run_loop` and
  `flush_recording_buffers` — **no dependency on ports that don't exist until 7b**.
+ DoD §2 "≤50 / commit ≤90" + P11 action-log sub-decomposition is **achievable**
  and does not merely relocate line count — the pure `_build_action_log` helper
  moves ~19 LOC OUT of the atomic lock body (the thing R3 protects) and is
  independently testable; commit_state is explicitly exempted at ≤90.

---

## VERDICT: ROUND2_CLEAR (proceed to implementation)

Both NEW issues are non-blocking implementer NOTEs (lazy-adapter wording +
one over-stated parenthetical); the actual protection/tests exist. All five
round-1 BLOCKERs are closed in the plan text with correct line anchors.
