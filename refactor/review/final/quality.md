# REFACTOR-QUALITY Review — E1–E6 split of `framework_main_async.py`

Branch: `refactor/framework-main-async-e1e6` · scope `906a49b..HEAD` (13 phase commits).
Reviewer scope: result quality + module cohesion (not the per-phase process).

## Summary

The **leaf-module extraction is sound**: 7 new cohesive modules (each <170 LOC, all
<500), a correct re-export shim, frozen-API preserved, string-patch guard tests that
genuinely fire, and 569 tests green (baseline 542). The P11 atomicity + lock-scope
safety nets are real and AST-enforced.

**The two plan goals that drove the whole effort are not actually achieved:**

- **E2 / DoD gate-2 (`_run_loop` decomposition) was never executed** — Phase 7a
  (commit `bdeb918`) only added safety tests. `_run_loop` is still a 467-LOC god-method
  with zero `_step_*` methods, which pushes `loop_orchestrator.py` to **742 LOC** (over
  both the plan's ~480 target and the project's 500-LOC rule).
- **E5 (hexagonal ports) is aspirational** — the 5 `Protocol`s in `ports.py` are never
  referenced outside that file; the constructor is still the old non-injectable
  `__init__(self, session_id)`; the planned injectability tests were never added.

Net: **REFACTOR_HAS_DEBT.** File split good, behavior preserved, debt remains in the
orchestrator file size, the missing decomposition, the unwired ports, and a few
copy-paste/dead-code leftovers.

---

## Findings — `MODULE/AREA | SEVERITY | what | fix`

### loop_orchestrator.py / `_run_loop` | BLOCKER (DoD gate-2) | `_run_loop` is a 467-LOC god-method; the `_step_*` decomposition was never done

- Evidence: `ast` parse → `_run_loop` spans lines **169–635 = 467 LOC**. There are
  **zero** `_step_*` methods in `AsyncFrameworkLoop` (`grep _step_` → no matches).
  Phase 7a commit `bdeb918` is titled "pin _run_loop lock invariants" — i.e. only
  safety tests, not the decomposition. Plan §3 Phase 7a + DoD §2 required a ≤60-LOC
  driver calling ≤50-LOC `_step_*` methods (with the documented `_step_commit_state`
  ≤90-LOC single-lock exception).
- Fix: execute Phase 7a as specified — extract `_step_init`, `_step_wait_for_start`,
  `_step_read_snapshot`, `_step_call_conductor`, `_step_parse_actions`,
  `_step_build_next_stems`, `_step_submit_jobs`, `_step_await_and_fetch`,
  `_step_tile_audio`, `_step_commit_to_mixer`, `_step_commit_state` (single-lock),
  `_step_record_and_pregen`, `_step_await_transition`. This also resolves the file-size
  problem below.

### loop_orchestrator.py | CONCERN | 742 LOC exceeds both the plan target (~480) and CLAUDE.md's 500-LOC rule

- Evidence: `wc -l app/framework/loop_orchestrator.py` → 742. This is a direct
  consequence of BLOCKER above: the undecomposed `_run_loop` is ~63% of the file.
- Fix: same as BLOCKER — `_step_*` extraction brings the file under 500.

### ports.py / E5 | CONCERN | The port story is documentation-only — no adapter implements them, nothing imports them, constructor is not injectable

- Evidence: `grep -rn 'ConductorPort|JobQueuePort|AudioFetchPort|AuditSinkPort|MixerController'`
  outside `ports.py` → **zero** source references. `AsyncFrameworkLoop.__init__` is the
  OLD signature `def __init__(self, session_id: uuid.UUID)` (loop_orchestrator.py:84);
  plan Phase 7b / R14 / A6 required `__init__(self, session_id, *, conductor=None,
  jobs=None, audio=None, audit=None, pregen=None)`. The planned tests
  `test_loop_constructs_with_default_ports` and `test_loop_accepts_injected_fake_ports`
  were never added (`grep` → none). So the dependency-inversion claim in the DoD is not
  realized; tests still rely on `patch.object(loop, '_submit_job')` private patching.
- Fix: either (a) explicitly mark E5 deferred in plan.md (ports are forward-looking
  typing) and drop the "E5 met" claim, or (b) implement the keyword-injectable
  constructor, type the deps as the ports, and add the two injectability tests.

### cache_key duplication | CONCERN (silent-drift hazard) | `{m_id}_{prompt}_{bpm}_{key}_{bars}` duplicated inline at 3 sites; the plan-required `make_cache_key()` + identity test were never created

- Evidence: `grep 'cache_key = f' app/framework/` →
  `domain_audio.py:87`, `pregeneration.py:85`, `loop_orchestrator.py:375`. Plan §3
  Phase 6 explicitly required extracting `make_cache_key(model_id, prompt, bpm, key,
  bars)` and a `test_cache_key_is_identical_across_all_4_sites`. Neither exists.
  Consequence: one site drifting breaks foreground/background cache *hits* silently
  (the background path re-submits a job the foreground already cached → duplicate
  stems / wasted GPU), while the divergence tests still pass green.
- Fix: extract `make_cache_key()` (pure, in `domain_audio` or `conductor_interaction`),
  call it at all 3 sites, add the identity test.

### DoD gate-8 | CONCERN (half-pinned) | Only ONE of the two required cache-divergence regression tests exists

- Evidence: `test_pregeneration_does_not_route_through_cache_stem` exists
  (test_pregeneration_divergence.py:62). The complementary
  `test_foreground_loop_routes_through_cache_stem` does NOT
  (`grep` finds only the background-side test). The *behavior* is preserved
  (loop_orchestrator.py:417 calls `state.cache_stem`; pregeneration.py never does), but
  only half of the divergence is pinned.
- Fix: add `test_foreground_loop_routes_through_cache_stem` (drive one foreground loop,
  assert `state.cache_stem` IS called).

### loop_orchestrator.py action-log rebuild | CONCERN (copy-paste) | The Retained/Added/Removed log builder is duplicated in `_run_loop` (fresh path + pregen path)

- Evidence: `grep 'current_actions_log = \[\]'` → line **312** (fresh-LLM path) and
  line **507** (pregen-ready commit path) — near-identical loops. Plan §3 Phase 7a / DoD
  §2 explicitly called for sub-decomposing P11's action-log rebuild into a pure helper.
  Not done (consistent with BLOCKER).
- Fix: extract `_format_action_log(actions, stems) -> list[str]` into
  `conductor_interaction`, call from both paths.

### loop_orchestrator.py `garage` property | SUGGESTION (dead code) | The eager `garage` property (lines 105–109) is never called; `create_garage_client_from_env` import survives only to feed it

- Evidence: `grep '.garage'` over the loop surface → only the comment at line 52. The
  `_audio` property deliberately uses `self._garage` and its docstring explains why it
  avoids `garage` (eager factory raises outside try/except). The property + import are
  extraction leftovers.
- Fix: delete the `garage` property and the `create_garage_client_from_env` import; the
  shim's own re-export remains the real frozen-binding target.

### loop_orchestrator.py re-export layering | SUGGESTION (misleading docs) | The orchestrator re-exports `_flush_lock`/`flush_recording_buffers`/`decode_aac` with `noqa F401`, justified by a claim that's false

- Evidence: loop_orchestrator.py:36–37, 54 keep these as re-exports with comments
  claiming "routes/shows.py, tests import these from here" — but they import from
  `framework_main_async` (the shim), verified by grep over callers. The shim already
  re-exports these directly from the leaf modules, so the orchestrator's second layer is
  redundant.
- Fix: drop the redundant `noqa F401` re-exports from loop_orchestrator; rely on the
  shim for frozen bindings.

### loop_orchestrator.py stale comment | SUGGESTION | P9 tiling comment contradicts the final state

- Evidence: the comment near `tile_to_loop(...)` says "the background pre-generation
  path still inlines this until Phase 6 extracts it." Phase 6 DID extract pregen —
  pregeneration.py:127 calls `tile_to_loop()`. The comment is now wrong.
- Fix: update or remove the stale comment.

---

## What is genuinely good (with evidence)

- **Shim is complete and correct.** `framework_main_async.py` is 50 LOC; `__all__`
  lists all 9 frozen names; `__main__` block runs `run_framework_loop_async`. DoD gate-1 + gate-6 MET.
- **Frozen-API test goes beyond import-only** (test_frozen_api.py): imports all 9 names
  non-None, instantiates `AsyncFrameworkLoop(uuid4())` (catches signature breaks), and
  asserts the two string-patch targets resolve to callables.
- **String-patch guard is durable** (test_audio_fetch_guard.py): patches
  `app.framework.audio_fetch.decode_aac` / `create_garage_client_from_env` and asserts
  `mocked.assert_called_once()` — proves the patch reaches the real call site (no silent no-op).
- **`_flush_lock` identity preserved** — module-level in `audit_recording`, re-exported;
  import from the shim is the same object.
- **Thin delegating methods are a clean, consistent interim pattern.** `_build_prompt`
  → `build_track_prompt`, `_submit_job` → `submit_generator_job`, `_fetch_audio` →
  `self._audio.fetch`, `_append_loop_audit` → `append_loop_audit`,
  `_pre_generate_next_loop` → `run_pregeneration`. Each is 3–27 LOC, each keeps
  `patch.object(loop, …)` working. The delegate→module split is uniform.
- **Leaf modules are cohesive and small:** domain_audio 112, audio_fetch 58,
  conductor_interaction 160, audit_recording 170, job_queue 80, pregeneration 148,
  state_slices 153 — all single-responsibility, all <500, all documented with
  provenance/risk references (brief-01 risk #1/4, etc.).
- **P11 atomicity + lock-scope safety nets are real, not comments:**
  `test_record_loop_transition_runs_outside_state_lock` asserts `state.lock.locked()`
  is False at call time; `test_no_io_inside_state_lock_in_orchestrator` AST-walks the
  file for `await`/`open()` inside any `state.lock` block → zero. These hold regardless
  of the missing `_step_*` decomposition.
- **Cache divergence preserved at the behavior level** (R4): foreground path
  (loop_orchestrator.py:417) calls `state.cache_stem` under lock; pregeneration.py never
  does. Pinned by `test_pregeneration_does_not_route_through_cache_stem`.
- **Typing modernization is genuinely done** (E6): `ruff --select UP006,UP007,UP045,F401`
  → all-passed; default E+F went from the captured 450-fixable baseline → all-passed.
  DoD gate-3 + gate-4 MET (the only "Optional" hits are docstring prose, not annotations).
- **Tests green:** 569 passed, 58 skipped, 9 xfailed, 0 failed. DoD gate-5 MET.
- **GlobalState slice views are additive and safe** (E3): read-only `_Slice` forwards,
  `__getattr__` raises `AttributeError` for out-of-slice names, no `__setattr__`/rename,
  tested (test_state_slices.py). DoD gate-7 MET.

---

## DoD scorecard — plan.md §4 gates 1–8

| # | Gate | Status | Evidence |
| --- | ------ | -------- | ---------- |
| 1 | `framework_main_async.py` < 500 LOC (~40 shim) | **MET** | 50 LOC; `__all__` complete; `__main__` works |
| 2 | `_run_loop` ≤60-LOC driver + ≤50-LOC `_step_*` (`_step_commit_state` ≤90) | **NOT-MET** | `_run_loop` = 467 LOC (lines 169–635); zero `_step_*` methods; Phase 7a commit added only safety tests |
| 3 | `ruff UP006/UP007/UP045/F401` = 0; default E+F not increased | **MET** | `All checks passed!`; baseline 450-fixable → 0 |
| 4 | No `typing.List/Dict/Tuple/Optional` in `app/framework/` | **MET** | Only docstring prose hits, no annotations |
| 5 | 542+ tests green, 0 failed | **MET** | 569 passed, 0 failed |
| 6 | Frozen API importable (`test_frozen_api_importable`) | **MET** | imports + instantiation + callables verified |
| 7 | GlobalState slice views additive | **MET** | read-only `_Slice`, tested |
| 8 | Both cache-divergence regression tests green | **PARTIAL** | background-side test present; foreground-side test missing (behavior still preserved) |

### Risk-register reconciliation

- R1 (`_age` mutation): preserved in `process_actions`, documented (conductor_interaction.py docstring). ✓
- R2 (lock scope over I/O): enforced by `test_no_io_inside_state_lock_in_orchestrator` (AST). ✓
- R3 (P11 atomicity): partially enforced — `test_record_loop_transition_runs_outside_state_lock` proves the call is unlocked, but the planned `test_step_commit_state_is_single_lock_block` was never added because there is no `_step_commit_state`. PARTIAL.
- R4 (cache divergence): behavior preserved; ONE of two required tests present. PARTIAL.
- R5 (MixerPort dual-lock): correctly deferred; `MixerController` declared typing-only. ✓ (as planned)
- R6 (string-patch no-op): durably fixed (guard test fires). ✓
- R7 (`_flush_lock` identity): preserved + re-exported. ✓
- R11 (shared `stem_cache`): `run_pregeneration(loop, …)` takes the loop's cache; `test_pregen_skips_job_when_stem_already_cached` pins it. ✓
- R14 (ports injectable): NOT addressed — constructor unchanged, injectability tests missing. NOT-MET.

---

## VERDICT: REFACTOR_HAS_DEBT

The refactor's *primary structural win* — splitting a 1192-LOC god-file into cohesive
leaf modules behind a correct re-export shim, with zero behavioral regressions (569
green, frozen API intact) and genuine safety nets — is delivered. The remaining debt is
concentrated in **loop_orchestrator.py**:

1. **`_run_loop` was never decomposed** (BLOCKER vs DoD gate-2) → the orchestrator is a
   742-LOC file and `_run_loop` a 467-LOC method, the exact god-method the E2 goal
   targeted. This is the headline gap.
2. **E5 ports are declared but not wired/injected** → dependency inversion is
   aspirational, not met.
3. **Minor debt:** cache-key string duplicated at 3 sites (drift hazard), action-log
   rebuild copy-pasted, `garage` property dead, redundant re-export layer, one missing
   divergence test, one stale comment.

None of the debt is a behavioral regression — the suite proves behavior is preserved.
But the effort's own Definition of Done is not satisfied on gates 2 and 8, and the E5
claim is overstated. Recommend a follow-up phase that executes the `_step_*`
decomposition (which also resolves the file-size breach) and either wires the ports or
honestly marks E5 deferred.
