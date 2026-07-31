# Red-Team Audit: Verification Strategy of the E1–E6 Refactor Plan

**Reviewer task:** determine whether the plan's TESTS will actually catch
regressions, and find what the plan STILL leaves untested even after Phase 0.

**Scope read:** `refactor/plan.md` (§3 phases, §4 DoD, §5 risk register);
`refactor/explore/04_test_coverage.md` (brief-04); `refactor/explore/02_contract_blast_radius.md`
(brief-02); `refactor/explore/00_ruff_baseline.md` (brief-00);
`tests/test_loop_fixes.py`, `tests/test_async_framework.py`, `tests/conftest.py`,
`tests/test_worker_fetch_audio.py`; `app/framework/framework_main_async.py`,
`app/framework/framework_state.py`.

**Evidence commands run (all read-only):** `ruff check --select E,F`,
`--select F401` before/after simulated `UP006,UP007,UP045 --fix`,
`--select UP006,UP007,UP045`; greps for `last_actions`, `pregen_ready`,
`_reset_audit_state` save-list. No files staged, no source edited.

---

## A. Structured Findings (PHASE | SEVERITY | UNTESTED / SLIPS THROUGH | TEST TO ADD)

### PHASE 0 | BLOCKER | Safety net covers CRITICAL gaps 1–5 only; the HIGH gaps that guard the HIGH-RISK phases are left bare

Phase 0 writes characterization tests for brief-04 §C CRITICAL gaps 1–5. But
brief-04 §C also lists FIVE HIGH gaps (pregen-vs-fresh state divergence,
`last_actions` building, flush DB-write + failure requeue, loop exit conditions,
transition recording). The plan defers gaps 9 & 10 to Phase 7a and addresses
**none** of gaps 6, 7, 8 anywhere:

- **Gap 6 (pregen-ready vs fresh-LLM state divergence)** — the two branches at
  `framework_main_async.py:266–372` build `conductor_response` / `next_stems` /
  `state` deltas differently. **Confirmed untested**: the only "pregen_ready"
  tests (`tests/test_async_framework.py:191–212, 317–319`) are LOCAL logic-mirror
  functions (`check_pregen_ready`) that *re-implement* the predicate — they never
  drive the real `_run_loop`. No test asserts both branches end with identical
  downstream `state` deltas.
- **Gap 7 (`last_actions` building)** — **confirmed untested**: grep for
  `last_actions` across `tests/` finds only `test_api.py:477` (`assert "last_actions"
  in data`, a key-presence check) and a logic-mirror class docstring. Nothing
  asserts the *content* of `state.last_actions` produced by the real loop's fresh
  branch (`framework_main_async.py:438`) or pregen branch (`:658`).
- **Gap 8 (flush DB-write + failure requeue)** — moved in Phase 3, only
  lock-identity is pinned (see Phase 3 finding).

**Why this is a BLOCKER:** Phase 7a (HIGH RISK) decomposes `_run_loop` into
`_step_*` methods and is the exact code these gaps describe. Decomposing the
pregen/fresh branches without a characterization test that proves their state
deltas are equivalent means a merge/reorder silently desyncs BPM/key/set/
last_actions/active_stems and the suite stays 542 green. **The net is laid
under the wrong gaps.**

**Test to add (Phase 0, not 7a):**
`test_run_loop_pregen_and_fresh_produce_identical_state_deltas` — parametrize
`pregen_ready=True` (pre-seed `_pregen_results`) and `pregen_ready=False` (live
conductor), drive one `_run_loop` iteration each with the SAME conductor
response, then assert byte-identical `state.current_bpm / current_key /
current_set_name / llm_reasoning / last_actions / active_stems`. This pins gap 6
BEFORE 7a touches the code.

### PHASE 7a | BLOCKER | Highest-risk phase; its targeted tests cover only exit + transition, not the behaviors that decompose

7a is self-declared highest-risk (decomposes the 512-LOC `_run_loop`). Its only
NEW targeted tests are `test_run_loop_shutdown_exits_cleanly` (gap 9) and
`test_run_loop_transition_records_under_snapshot` (gap 10). The three behaviors
most likely to regress during `_step_*` extraction have **no test**:

1. **pregen/fresh branch divergence** (gap 6, `framework_main_async.py:266–372`) —
   see Phase 0 finding.
2. **`last_actions` construction** (gap 7) — duplicated at `:438` (fresh) and
   `:658` (pregen). Moving either into a `_step_` method can drop, duplicate, or
   reorder entries with zero detection.
3. **P11 atomic state-commit** (`framework_main_async.py:616–697`, ~40 mutations
   under ONE `async with state.lock`) — DoD gate #2 "verifies" atomicity with a
   *comment*, not a test. If a `_step_*` split acquires/releases `state.lock`
   twice between mutations, the race that risk #3 warns about reopens, and no
   test fires.

**Test to add:**
(a) the parametrized divergence test above; (b)
`test_last_actions_built_for_fresh_and_pregen_branches` — assert
`state.last_actions` content + length after both paths; (c) an atomicity test —
instrument `state.lock.acquire`/`release` (or AST-scan `_step_commit_state`) and
assert a single contiguous acquire/release pair with no `await` between
mutations.

### PHASE 9 | BLOCKER | DoD gate #3 baseline (735) is stale and the UP-fix detonates 24 NEW F401 errors invisible to the gate

Two independently-verified defects compound:

1. **Stale baseline.** DoD gate #3 + Phase 9 verification cite a "735 baseline"
   for default ruff `E`+`F`. brief-00 measured 735 at commit `906a49b`, but the
   **measured current count is 194** (`ruff 0.16.0`, `--select E,F app/ tests/`,
   "Found 194 errors"). "≤ 735" is now trivially satisfiable and masks every
   regression in the range 195–735.
2. **UP-fix F401 explosion.** Simulated `ruff --select UP006,UP007,UP045 --fix`
   on a copy of `app/` raises `F401` (unused-import) from **36 → 60** — i.e.
   **+24 NEW F401 errors**. Mechanism: converting `List`→`list`,
   `Optional[X]`→`X|None` leaves `from typing import List/Dict/Optional/Set`
   unused, and the plan selects only `UP006,UP007,UP045` for `--fix` (F401 is
   NOT selected, so the now-unused names are never pruned). The plan's manual
   step 4 ("remove the now-empty `from typing import …` lines") is insufficient:
   most lines are **partially** unused (e.g. `from typing import Optional, List,
   Dict, Any` keeps `Any` → line is not "empty" → 3 NEW F401 survive).

**Slip-through sequence:** Phase 9 gate sees `194 + 24 = 218 < stale 735` →
**false PASS**. The 24 new F401 lie dormant until Phase 10 enables
`select=["E","F",…]`, where `ruff check app/ tests/` now reports them as "new"
items → gate either fails late (after a Phase-9 commit is already in) or is
hand-waved against the stale 735.

**Fix:** (1) replace "735" with the live measured count and re-measure at each
phase gate; (2) in Phase 9 run `ruff --select UP006,UP007,UP045,F401 --fix` (or a
second `--select F401 --fix` pass) so unused imports are pruned in the same
commit; (3) add `assert f401_count_after <= f401_count_before` to the Phase 9
gate.

### PHASE 0 | CONCERN | Gap 4/5 characterization tests (real `_run_loop` mixer handoff) depend on a harness the plan under-specifies — will hang or leak state

Three concrete harness defects for the proposed loop-1 / loop-N tests:

1. **`_FakeMixer` records nothing** (`tests/test_loop_fixes.py:241–262`): every
   method is a no-op `pass` / `return audio`. The gap 4/5 tests must assert what
   `mixer._add_track_internal(start_sample, …)` and
   `mixer.set_next_loop(tracks, next_loop_duration_samples=…, loop_idx=…)`
   *received* — but the fake discards all args. `_current_loop_duration` is not
   even a defined attribute (assignment silently creates it; nothing records it).
2. **No deterministic stop-after-N-loops hook is specified.** The watchdog test
   (#32) stops the loop by raising inside `fake_append_audit` and setting
   `running=False` on retry — not reusable for a happy-path handoff test. Driving
   `_run_loop` past loop 1 requires the background pregen *task* to complete on
   the event loop between iterations; with mocked I/O this is timing-sensitive
   and will flake. The plan does not specify how gap 5 reaches `loop_idx>1`
   (full second iteration vs pre-seeding — neither is spelled out).
3. **State leakage.** The autouse `_reset_audit_state` fixture saves only 6 attrs
   (`current_show_id`, `current_show_start_time`, both buffers, `is_generating`,
   `is_running`). A real `_run_loop` iteration mutates ~15 more — confirmed:
   `loop_history`, `currently_playing_loop_index/stems/set_name/reasoning`
   (written by `state.record_loop_transition`, `framework_state.py:248–260`),
   `active_stems`, `previous_stems`, `next_stems`, `stem_history`, `loop_count`,
   `stem_volumes/muted_stems/soloed_stems`. None are in the save list, so a
   gap-4/5 test poisons every sibling test that reads the `state` singleton.

(`record_loop_transition` itself is safe — `sync_lock` + `deepcopy` + append, no
I/O — so it will not hang; the hang risk is the pregen-task coordination in (2),
not this call.)

**Fix:** extend `_FakeMixer` to record calls (explicit lists or `MagicMock`);
either replace `_reset_audit_state` with a full-singleton snapshot/restore (or
use fresh `GlobalState()` and inject it — currently `_run_loop` hard-binds the
module singleton, so the snapshot approach is required); add an explicit
deterministic stop hook (e.g. conductor mock sets `state.shutdown_event` on the
Nth call) so loop-1 and loop-N tests are single-shot, not task-coordination races.

### PHASE 2 / 7b | CONCERN | `test_frozen_api_importable` catches ImportError only — NOT the brief-02 §D silent string-patch no-op

The proposed guard `test_frozen_api_importable` asserts the 10 frozen names are
importable & non-None. **It does not detect the silent no-op.** Confirmed
mechanism: after Phase 2 moves `decode_aac`/`create_garage_client_from_env` into
`audio_fetch.py`, the plan keeps `from app.aac_encoder import decode_aac` at the
top of `framework_main_async.py` (belt-and-suspenders), so the name stays
importable → test green. But `tests/test_worker_fetch_audio.py:43,44,74,75` patch
by string path `app.framework.framework_main_async.decode_aac`, while the real
`_fetch_audio` (now a delegate into `audio_fetch`) resolves `decode_aac` from
`audio_fetch`'s namespace — **the patch becomes a no-op and the test passes
vacuously or fails confusingly.** DoD gate #6 treats `test_frozen_api_importable`
as sufficient frozen-API proof; it is not.

The Phase 2 plan DOES mitigate this with `test_decode_aac_patch_actually_applies`
- migrating the patch strings — good — but: (a) that guard covers only the
audio_fetch pair, not every re-exported name; (b) Phase 7b repeats the claim
("no patch-string changes needed since all names are re-exported") which is true
only for INSTANCE-level `patch.object(loop, …)`, not module-string patches; (c)
gate #6 gives false confidence.

**Fix:** add a guard that round-trips a string patch through each re-exported
name and asserts the patched callable is actually *invoked* by the code path that
consumes it (generalize `test_decode_aac_patch_actually_applies`). Separately,
grep the test suite for `patch('app.framework.framework_main_async.<name>')` and
assert each target module is where the name's real definition lives, not merely a
re-export.

### PHASE 3 | CONCERN | Phase 3 moves `flush_recording_buffers` DB-write + failure-requeue; only lock identity is pinned

Phase 3's only targeted test is `_flush_lock is audit_recording._flush_lock`
(identity). The behavior the phase *moves* — `bulk_insert_mappings` of both
buffers (`framework_main_async.py:104–107`) and the failure requeue that
prepends buffers back (`:113–118`) — is untested (brief-04 HIGH gap 8). Existing
test #31 short-circuits on empty buffers and never reaches the DB code. A
refactor that drops or reorders the requeue silently loses audit rows on DB
failure, and the suite stays green.

**Fix:** add `test_flush_recording_buffers_writes_then_clears` (populate both
buffers, inject a fake session, assert `bulk_insert_mappings` called with the
right rows + buffers cleared) and
`test_flush_recording_buffers_restores_buffers_on_db_failure` (fake session
raises, assert buffers prepended back unchanged).

### PHASE 9 | CONCERN | pydantic-regression mitigation for `routes/schemas.py` is a manual smoke, not a committed test

Phase 9's pydantic caution (risk #8) for `routes/schemas.py` (~40 `Optional`→
`X|None`) is mitigated by "run `test_api.py` + a quick model-instantiation smoke
before committing." That smoke is a HUMAN step, not committed to CI. If no
existing test instantiates every affected schema, a pydantic v2 regression on an
untested model slips past Phase 9 and is caught (maybe) only by Phase 10 ruff or
never.

**Fix:** add a committed test that imports and instantiates every model in
`routes/schemas.py` with representative values (or at least asserts
`__annotations__` resolve without `TypeError`) so the regression is enforced in
CI.

### PHASE 4 / 3 | SUGGESTION | Moved private helpers' edge cases ride on a single happy-path test

Phase 3 (`_relative_show_ms`, `_audit_*`) and Phase 4 (`_build_prompt`) move
module-private helpers whose edge cases are only partially covered (brief-04
MEDIUM gaps 2: add-action details, out-of-range `idx`, `_relative_show_ms` when
`start is None`⇒0, remove/unknown descriptions, engine `prompt_template` vs
default). Each phase's parity test asserts importability, not edge behavior. The
happy-path audit test #28 does not cover these.

**Fix:** add targeted edge-case tests in the phase that moves each helper rather
than trusting the audit happy path.

---

## B. STILL-UNTESTED-AFTER-PHASE-0 (highest-risk behaviors with no test, even after the net is laid)

Ordered by (regression likelihood × blast radius) during Phases 6/7a/7b:

1. **pregen-ready vs fresh-LLM branch state divergence** (gap 6,
   `framework_main_async.py:266–372`). The EXACT code 7a decomposes. No test in
   ANY phase. **Highest risk: a desync here changes the audible music silently.**
2. **`last_actions` content from the real loop** (gap 7, fresh `:438` + pregen
   `:658`). No test in any phase; duplicated construction is prime refactor
   bait.
3. **P11 atomic state-commit** (`:616–697`). "Verified" by comment only; a
   `_step_*` split reopening `state.lock` between mutations is a concurrency
   regression with no test (risk #3).
4. **`flush_recording_buffers` DB write + failure requeue** (gap 8,
   `:102–118`). Moved in Phase 3; only lock identity tested; failure path can
   silently lose audit rows.
5. **`_fetch_audio` empty-bytes / exception ⇒ None** (gap 3) — *is* pinned by
   Phase 0, BUT the silence-stem fallback (`:577–580`, `np.zeros`) that consumes
   the `None` is not; a refactor that drops the silence fallback changes audio
   silently.
6. **`_run_loop` end-of-iteration transition poll + `current_ahead < 0.5` break
   - pregen-done break** (`:717–757`). Gap 10 is only PARTIALLY covered by 7a's
   single transition-event test; the `current_ahead` deadline and the
   pregen-not-done-but-deadline-hit break are untested.
7. **`process_actions` live-list `_age` mutation** (risk #1, `:131–132`). Gap 1
   characterization asserts the outcome, but the *live-list contract*
   (orchestrator must pass the SAME list, not a copy, so `_age` flows to P6/P11)
   is a code comment, not an enforced test; Phase 4 extraction can break it by
   passing a copy.

---

## C. Direct answers to the five pressure-test questions

1. **Gap where a Phase-7a/7b regression has no test until too late?** YES — gap 6
   (pregen/fresh divergence) and gap 7 (`last_actions`). Both are the code 7a
   restructures, neither is tested in Phase 0 or 7a. A desynced merge of the
   pregen/fresh branches stays 542-green. Gap 8 (flush requeue) is the Phase-3
   equivalent: moved with only lock-identity pinned.
2. **Does `test_frozen_api_importable` catch the string-patch no-op?** NO. It
   catches `ImportError` only. The re-export keeps the name importable while the
   real binding moves; string patches no-op silently. DoD gate #6 over-trusts it.
3. **Is the harness sufficient for a full `_run_loop` end-to-end test?** NO.
   `_FakeMixer` records nothing; no stop-after-N hook is specified for happy-path
   loops; `_reset_audit_state` saves 6 attrs but a real loop mutates ~15 → state
   leakage + pregen-task-coordination flake risk.
4. **Will DoD gate #3 falsely fail or pass?** BOTH, at different phases. Baseline
   735 is stale (actual 194) → Phase 9 false PASS (218 < 735); the 24 NEW F401
   from the UP-fix then detonate at Phase 10's E+F enforcement. Baseline is NOT
   re-established correctly post-fix.
5. **Phase whose ONLY verification is "pytest green" with no targeted test for the
   moved behavior?** Phase 3 (flush DB-write/requeue moved, only lock-identity
   tested) and Phase 7a (highest-risk decomposition, targeted tests miss gaps
   6/7/P11). Phase 7b leans on the insufficient `test_frozen_api_importable`.
   Phase 9's pydantic check is manual, not committed.

---

## VERDICT

**VERIFICATION_HAS_GAPS** — Phase 0 is sound for CRITICAL gaps 1–5, but three
HIGH-risk behaviors that the HIGH-RISK phases (6/7a/7b) restructure have NO test
in any phase (pregen/fresh divergence, `last_actions`, P11 atomicity), the
frozen-API guard catches the wrong failure mode, the loop-end-to-end harness is
under-specified (will leak state / flake), and the DoD ruff gate #3 operates on a
stale baseline while the UP-fix secretly adds 24 lint errors. None of these are
intrinsic to the refactor — each is a targeted test or gate correction away from
closed.
