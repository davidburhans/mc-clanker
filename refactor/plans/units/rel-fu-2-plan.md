# PLAN — Unit FU-2 `rel-fu-loop`, branch `rel-fu-2-loop`
**Spec:** `refactor/plans/rel-remediation-plan.md` §Follow-ups round 2 (FU-2) · follow-up notes "rel-02 (P2, report-only)" + "rel-12 (from rel-17 review, report-only)" · `docs/reliability_audit.md` REL-02 / REL-12 rows (status paragraphs carry both residuals) · race analysis: `refactor/plans/units/rel-02-plan.md` §1.3 (decision 3 — in-flight pregen survives reset) and §6
**Items:** (1) pregen epoch/generation counter — stale pre-reset result acceptance (rel-02 residual); (2) outage streak resets on successful SUBMIT, not read-probe (rel-12/rel-17-review residual); (3) `loop_orchestrator.py` split under 500 (rel-17-review debt: 512/500).
**Baseline gate (verified green at `b15a187`):** `1188 passed / 26 skipped` (~32 s), `ruff check app tests` clean; do not regress.

---

## 0. Scope summary

| Item | Root cause today | Fix site |
|---|---|---|
| Epoch counter | P2 accepts a pregen result on `loop_idx` equality alone (`loop_steps.py:475-479`); post-reset numbering restarts at 1 (REL-02 force), so an in-flight pre-reset result for loop M is accepted once when post-reset `_loop_idx` revisits M → one loop of stale (pre-reset) audio | epoch counter stamped into pregen results at spawn time, compared at P2 |
| Streak reset | `_step_call_conductor`'s skip guard (`loop_steps.py:610-614`) resets `_consecutive_submit_failures = 0` when the READ probe `_probe_queue_recovered()` succeeds; on a read-only PG hot standby every probe succeeds while submits fail → streak churns 0→1→2→3→0 and the full conductor LLM call repeats **every cycle** in that mode | probe no longer resets anything; streak resets ONLY on a successful submit (the `_submit_job` seam, already present at `loop_orchestrator.py` `_submit_job`); the probe gates a one-shot WRITE canary |
| 512/500 split | rel-12 unit + rel-18 unit grew `loop_orchestrator.py` past the 500-LOC rule (AGENTS.md style; the module docstring itself promises "stays under the project's 500-LOC rule") | pure move of the adapter-delegates block (L343-467) to a new `_LoopDelegates` mixin module |

No worker / mixer-thread / storage / capture-schema code is touched (invariants 3-5 unaffected; see §6).

---

## 1. Design decisions (documented; deviations called out)

### 1.1 Epoch = "loop-numbering generation" counter

1. **`self._pregen_epoch: int = 0`** (init in `AsyncFrameworkLoop.__init__`). Monotonic; incremented in exactly ONE place: the `should_reset` consumption branch of `_step_read_state` (P3, inside the existing `state.lock` block — a pure int increment next to the already-sanctioned `mixer.clear()`/`stem_cache.clear()`; no I/O, invariant 1 preserved). It NEVER resets to 0 — a second reset yields 1 then 2, so double-reset staleness is caught too (test E3).
2. **Stamped at SPAWN time, not completion time.** The stamp rides the P11 `state_snapshot` (`"pregen_epoch": self._pregen_epoch`), which P12 hands to `_pre_generate_next_loop`, and `run_pregeneration` copies it into `_pregen_results["pregen_epoch"]` via `snapshot.get("pregen_epoch", 0)`. Why not read `loop._pregen_epoch` when building the result: a reset firing MID-pregen would bump the loop attr before the pregen task finishes, and the stale result would stamp itself with the FRESH epoch and self-forgive. The snapshot is immutable from spawn to completion, so a pre-reset spawn can never acquire a post-reset stamp. (Snapshot read and spawn happen in the same iteration, P11→P12, single task — no interleaving; a reset landing between them is only *consumed* at the next iteration's P3, after the spawn already carried the pre-bump epoch → correctly rejected.)
3. **P2 gate adds epoch equality with a total default:**
   ```python
   pregen_ready = (
       self._loop_idx > 1
       and self._pregen_results is not None
       and self._pregen_results.get("loop_idx") == self._loop_idx
       and self._pregen_results.get("pregen_epoch", 0) == self._pregen_epoch
   )
   ```
   The `0` default is a total function, not a compat hack: epoch 0 *is* "before any reset", and every result computed before the first reset is epoch 0 — a missing key means exactly that. Every real FU-2 writer stamps the key explicitly (the P12 loop-1 fabrication stamps `self._pregen_epoch` directly, no snapshot involved); the default only ever matches while `_pregen_epoch == 0`, a regime in which there is no pre-reset history to reject. This keeps every existing hand-crafted `_pregen_results` fixture (none of which reset) byte-identical in behavior — verified sites: `test_round3_fix_b.py:313,331,521`, `test_reset_reprime.py:208` (drives P10, not P2), `test_adversarial_wave2.py:487` (drives P11, not P2).
4. **Same-iteration acceptance before the reset is preserved** (rel-02 plan decision 3, unchanged): P2 runs before P3, so the iteration whose P3 consumes the reset may still commit the pre-reset pregen it accepted at P2 (clearing `_pregen_results` at P3 would trip P10's `assert ... is not None` → B1 watchdog retry — rejected in rel-02 and still wrong). The epoch bump therefore invalidates results only from the NEXT iteration onward — exactly the window in which indices revisit. The in-flight pre-reset TASK may still complete post-reset and overwrite `_pregen_results` with an old-epoch stamp; P2 rejects it (test E4), the stale dict is cleared by the next P12 spawn, and P10's assert can never see it (it only dereferences on `pregen_ready`).
5. **Backward compat with in-flight results at reset** = rejection, not crash: an old-epoch result makes P2 take the fresh path (`conductor_response=None`), never raises, and `run_pregeneration`'s completion path (`loop._pregen_done.set()`) merely releases P13's wait one iteration early. No silence window is introduced (rel-02's forced-prime flow supplies the audio meanwhile).

### 1.2 Streak resets on successful SUBMIT only — the probe becomes a canary gate

6. **The reset call moves (deletes) out of the probe branch entirely.** The ONLY streak reset stays where B5 pinned it (`test_submit_delegate_tracks_failure_streak`): the `_submit_job` delegate's success line (`self._consecutive_submit_failures = 0  # any successful submit proves the queue writable`). Consequence that forces the rest of this design: during a skip, `build_fallback_response` retains all stems, retained stems are cache-HITs, and REL-06's hit-refresh keeps them alive forever — so **no natural submit ever happens again** and the skip would be permanent even after a full writable recovery. An explicit write-side probe is therefore REQUIRED.
7. **The canary: one real submit through the `_submit_job` seam.** New delegate `_canary_submit(active_stems, current_bpm, current_key) -> bool`:
   - Payload `_canary_payload()`: the oldest-by-`_age` **active stem** (the one the conductor would replace first; `prompt`/`model_id`/`bars` are on the stem dict, `instrument`/`major_family` from `_original_details` — same shape P7 reads); if `active_stems` is empty (cold-start outage), a synthetic throwaway payload (instrument "Queue Recovery Canary", bars 1).
   - Success → the seam resets the streak; then best-effort `self._abandon_jobs([canary_id])` so the worker never generates a redundant stem (its audio is already cached or synthetic). A failed abandon is caught — worst case one redundant generation per recovery event (the worker-claim race is sub-second; bounded, rare).
   - Failure → caught locally, returns False; the seam has already incremented the streak, the skip re-engages, and **the exception never escapes P4** (the retain-all fallback iteration must still commit — test O2).
   - cfg/steps pass `None` (the canary is abandoned; worker defaults are moot) — no extra `read_generation_params()` lock take on the skip path.
8. **What still uses the probe:** `_probe_queue_recovered()` stays, at most once per loop, only inside the skip guard — now as the cheap read that *gates the canary* (don't even attempt an INSERT against a fully-dead DB; during a full outage the skip path performs zero writes and zero LLM calls, exactly as today). Its docstring is corrected ("True = queue readable; says nothing about writability").
9. **P4 guard, restructured** (`loop_steps.py:609-614`):
   ```python
   if self._consecutive_submit_failures >= LOOP_CONDUCTOR_SKIP_AFTER_SUBMIT_FAILURES:
       if not await self._probe_queue_recovered():
           return build_fallback_response(current_bpm, current_key, active_stems, "job-queue submit outage")
       # FU-2: a successful read says nothing about writability (a hot-standby
       # PG answers reads while every submit fails — the old probe-side streak
       # reset re-enabled the conductor call every cycle in that mode). Verify
       # with one real canary submit; only its success (via the _submit_job
       # seam) may reset the streak.
       if not await self._canary_submit(active_stems, current_bpm, current_key):
           return build_fallback_response(current_bpm, current_key, active_stems, "job-queue submit outage")
       # writable again — fall through to the real conductor call THIS iteration
   ```
   Mode matrix: **full outage** (probe fails) → identical to today, skip holds. **Writable recovery** (probe + submit succeed) → canary resets the streak, conductor resumes the SAME iteration → REL-18 B4's "within one loop of the queue returning" letter is preserved. **Read-only standby** (probe succeeds, submit fails) → one cheap failed INSERT per loop, streak grows, conductor NEVER re-called, the set keeps playing from cache indefinitely (REL-06 hit-refresh) — the reported bug, fixed.
10. **No new port surface.** `JobQueuePort` gains no `verify_writable()` — every fake in the suite implements `submit`, so reusing it costs zero fake changes, and the canary exercises the exact operation whose failure the streak counts (an INSERT commit). Adding a port method would break every hand-rolled fake on the skip path (blast radius: `_OutageJobQueuePort`, `_SoakJobQueue`, `test_job_queue_lifecycle` fakes, …) to express something `submit` already expresses (decision documented; hexagonal rule kept — the loop still speaks only to the port).
11. **Pregen gate untouched** (`pregeneration.py` REL-18 comment "the foreground probe owns recovery — no probe here"): the background path skips the LLM while the streak ≥ 3; the foreground canary is the single recovery owner. No change.

### 1.3 Extraction: `_LoopDelegates` mixin

12. **Seam = the adapter-delegates block** (`loop_orchestrator.py:343-467`, ~125 lines: `_build_prompt`, `_submit_job`, `_await_jobs`, `_abandon_jobs`, `_pending_depth`, `_fetch_audio`, `_append_loop_audit`, `_pre_generate_next_loop`) → new module **`app/framework/loop_delegates.py`**, class **`_LoopDelegates`**. `AsyncFrameworkLoop(_LoopSteps)` → `AsyncFrameworkLoop(_LoopDelegates, _LoopSteps)` — delegates first in MRO so the real implementations win over `_LoopSteps`' `NotImplementedError` stubs; the stubs stay (they type the mixin standalone, exactly as today).
13. **Why this seam:** it is the only block that is (a) cohesive (pure port-facing glue, zero `state.lock` blocks, zero mixer reach), (b) ~125 lines — enough to clear 512→<500 *with* the FU-2 additions landing in the same unit, and (c) patch-point-free at the module level (§4 proof: every test touches these names via `patch.object(loop, …)` on instances or class attributes — both survive any class-level relocation; there are ZERO string patches into the `loop_orchestrator` module namespace, verified by grep).
14. **`run_framework_loop_async` STAYS in `loop_orchestrator.py`** (deviation from "move the most self-contained thing"): `test_loop_robustness.py::test_b6_startup_failure_still_green` monkeypatches `loop_orchestrator.AsyncFrameworkLoop` and relies on `run_framework_loop_async` resolving the patch through the same module's globals; a moved function would import the real class into the new module and silently ignore the patch. The delegates move has no such coupling.
15. **The canary lives in `loop_delegates.py`** (not `loop_steps.py`, 1183 lines of acknowledged brownfield debt): it is a submit-path concern, sits next to the seam that owns the streak, and keeps `loop_steps` growth to the ~6-line P4 restructure + epoch stamps. The `_audio` property/setter, `start`/`stop`/`_finish_loop`/`_run_loop`, `__init__` stay in the orchestrator (lifecycle).
16. **Lock-safety guards extend, not weaken:** `test_loop_lock_safety.py` scans `[loop_steps.py, loop_orchestrator.py]` in 3 lists (L39-42, L394, L461); each list GAINS `Path("app/framework/loop_delegates.py")` so the no-I/O-under-`state.lock`, no-reverse-lock and no-mixer-privates invariants "follow the code wherever it moves" (the guards' own docstrings). The moved delegates + canary contain none of those hazards; the extension is pure coverage.

---

## 2. Exact changes per file

### 2.1 `app/framework/loop_orchestrator.py` (512 → ~392 lines)
- **Delete** L343-467 (delegates block + its `# ---` banner).
- **Imports:** drop `import numpy as np` (only `_fetch_audio` used it), drop `run_pregeneration` and `build_track_prompt` (moved); keep `process_actions` (frozen re-export, `noqa: F401`) and everything else; ADD `from app.framework.loop_delegates import _LoopDelegates`.
- Class line: `class AsyncFrameworkLoop(_LoopDelegates, _LoopSteps):` (+ docstring note: delegates mixin home).
- `__init__`: add after `self._loop_idx = 0`:
  ```python
  # FU-2 (rel-02 residual): generation of the loop NUMBERING — bumped on every
  # should_reset consumption (P3) because post-reset numbering restarts at 1;
  # pregen results carry their spawn-time stamp so a pre-reset in-flight result
  # can never be accepted again when the indices revisit.
  self._pregen_epoch = 0
  ```

### 2.2 `app/framework/loop_delegates.py` (NEW, ~225 lines)
- Module docstring: purpose (Phase FU-2 extraction; pure-move provenance from `loop_orchestrator.py`; patch-seam guarantee "`patch.object(loop, '_submit_job')` keeps working: methods resolve on the combined class via MRO"); host contract note mirroring `_LoopSteps`.
- Imports: `uuid`, `Any`/`TYPE_CHECKING` (`np`, `AudioFetchPort`, `AuditSinkPort`, `JobQueuePort` under TYPE_CHECKING), `build_track_prompt`, `run_pregeneration`. **Import-graph proof (§5).**
- Host contract declarations (annotation-only, `_LoopSteps` style): `session_id`, `_jobs`, `_audit`, `_consecutive_submit_failures`.
- The 8 moved delegates, byte-identical bodies (docstrings updated only where they said "loop_orchestrator.py" for their own location — none do; they say "kept as a method so patch.object…", which remains true).
- NEW `_canary_submit` (~18 LOC) + module-level `_canary_payload()` (~18 LOC, pure function — oldest-by-`_age` active stem else synthetic throwaway) per §1.2.7; both with intent docstrings.

### 2.3 `app/framework/loop_steps.py` (1183 → ~1192)
- **P2 gate** (~L475): add the epoch conjunct (§1.1.3).
- **P3 reset branch** (~L545-549): after `state.should_reset = False`, add `self._pregen_epoch += 1` with the WHY comment (post-reset numbering revisits indices; see §1.1.1).
- **P4 skip guard** (~L609-614): restructure per §1.2.9 (net +3 lines, stays ≤20 LOC).
- **P11 snapshot** (~L1005): `"pregen_epoch": self._pregen_epoch,` inside `state_snapshot` (plain dict-literal write inside the lock — no I/O, passes the AST guard).
- **P12 loop-1 fabrication** (~L1060): `"pregen_epoch": self._pregen_epoch,` in the fabricated `_pregen_results`.
- `_LoopSteps` host contract: declare `_pregen_epoch: int` + `_canary_submit` delegate stub (raising, like the others).
- `_probe_queue_recovered` docstring fix (§1.2.8).

### 2.4 `app/framework/pregeneration.py` (214 → ~217)
- Result dict: `"pregen_epoch": snapshot.get("pregen_epoch", 0),` with the WHY comment (spawn-time stamp; §1.1.2). `.get` keeps every existing direct-call test snapshot (which lacks the key) at epoch 0.

### 2.5 Test edits (keep-green constraints; NOT part of the red suite)
- `tests/test_loop_robustness.py`:
  - B4 `test_conductor_resumes_after_queue_recovery`: `jobs.submit_calls == 4` → `== 5` (3 failed + 1 canary + 1 recovered real submit); `submit_failures == 3` unchanged; comment block rewritten (the 7th probe now unlocks the canary, whose success falls through to the conductor the same iteration). Trace verified against the fake's `probes_before_recovery=7` accounting.
  - B3 `test_conductor_skipped_after_three_consecutive_submit_failures`: NO change (probe never succeeds → no canary ever → counts identical). Module docstring B3/B4 contract sentence updated: "the streak resets ONLY on a successful submit; the read probe gates a write canary".
- `tests/test_loop_lock_safety.py`: 3 file lists gain `Path("app/framework/loop_delegates.py")` (§1.3.16).
- `tests/test_framework_characterization.py` + `tests/test_pregeneration_divergence.py`: **UNTOUCHED** (§4 proof).

### 2.6 Docs
- `CLAUDE.md` framework table: `loop_orchestrator.py` row mentions the `_LoopDelegates` mixin + FU-2 semantics (epoch-stamped pregen results; submit-canary recovery); add `loop_delegates.py` row.
- `docs/reliability_audit.md`: REL-02 status paragraph — residual (stale pre-reset pregen acceptance) fixed-in rel-fu-2 (epoch counter); REL-12 status paragraph — read-only-standby streak churn fixed-in rel-fu-2 (submit-side reset + canary).
- `refactor/plans/rel-remediation-plan.md`: FU-2 status → landed (at unit close).

---

## 3. TDD tests (write first, confirm RED, then implement)

### 3.1 New file `tests/test_loop_epoch_recovery.py` (~260 lines)
Fixtures: reuse the named-fake shapes already in the suite — `_FakeMixerWithPosition` (test_loop_robustness), `_RecordingMixer` (test_reset_reprime), `_CountingConductor`; NEW `_ReadOnlyStandbyJobQueue` (`pending_depth` → 0 always; `submit` raises always; `await_jobs`/`abandon_jobs` inert, record calls) and `_RecoveringJobQueue` (outage flag flips submit+probe together). Autouse state snapshot/restore (test_loop_robustness `_isolated_state` pattern). Helpers: `_loop_with(mixer)`, `_seed_pregen_result(loop, loop_idx, epoch, **extra)`.

| # | Test | Pins | Core assertions (RED today →) |
|---|---|---|---|
| E1 | `test_stale_pregen_result_old_epoch_rejected_when_indices_revisit` | FU-2 item 1 core | Seed result `{"loop_idx": 7, "pregen_epoch": 0, …}`, `loop._loop_idx = 7`; `state.should_reset = True; await loop._step_read_state()` (epoch → 1); `decision = await loop._step_pregen_decision()` → `decision.pregen_ready is False`, `decision.conductor_response is None` (fresh path). RED: today `pregen_ready is True`. |
| E2 | `test_fresh_epoch_result_accepted_after_reset` | no over-rejection | After the same reset, seed `{"loop_idx": 2, "pregen_epoch": 1, …}`, `_loop_idx = 2` → `pregen_ready is True`, response built from result fields (`set_name`, `master_bpm`). |
| E3 | `test_epoch_monotonic_across_resets_never_returns_to_zero` | "epoch survives should_reset" | `_step_read_state` with `should_reset` twice → epoch 1 then 2 (never 0); results stamped 0 and 1 both rejected at revisiting indices; stamp 2 accepted. |
| E4 | `test_inflight_pregen_completing_after_reset_cannot_re_enter` | in-flight backward compat (rel-02 decision 3 extension) | Drive `run_pregeneration(loop, 7, snapshot_with_epoch_0)` with cached stems (no submits) AFTER the loop's epoch bumped to 1 → `_pregen_results["pregen_epoch"] == 0`; P2 at `_loop_idx = 7` still fresh-path. |
| O1 | `test_read_only_standby_probe_does_not_reset_streak_conductor_stays_skipped` | FU-2 item 2 core | `_ReadOnlyStandbyJobQueue`; streak 3; call `_step_call_conductor(...)` N=3 times → conductor fake `calls == 0` every time, every response is the fallback (`"job-queue submit outage"` in reasoning), and `loop._consecutive_submit_failures == 3 + N` (grew by one failed canary each pass, NEVER reset). RED: today the first probe success zeroes the streak. |
| O2 | `test_canary_failure_is_contained_fallback_iteration_survives` | containment | Same fake, streak 3 → `_step_call_conductor` RETURNS the fallback (no raise escaping P4); canary attempt recorded on the fake (`submit_calls == 1`). |
| O3 | `test_successful_canary_resets_streak_and_resumes_conductor_same_iteration` | resume mechanics | `_RecoveringJobQueue` post-recovery; streak 3 → `_step_call_conductor` → conductor fake called once (NOT fallback), `loop._consecutive_submit_failures == 0`, and the canary row was best-effort abandoned (`abandon_calls == 1`, its id recorded). |
| O4 | `test_full_outage_window_engages_and_disengages_the_skip` | end-to-end outage window | Compact `_run_loop` drive (test_loop_robustness `_drive_loop` pattern, `_fresh_path_pregen_clearer`): outage window → conductor called exactly 3× then skipped (fallback commits, `state.loop_count` advances); recovery → conductor called again, submits succeed; skip never re-engages while submits succeed. |
| S1 | `test_orchestrator_split_files_under_500_lines` | FU-2 item 3 | `len(Path(...).read_text().splitlines()) < 500` for BOTH `loop_orchestrator.py` and `loop_delegates.py` (AGENTS.md rule; the audit tracks this debt explicitly). |

### 3.2 TDD order
1. Write §3.1 + the two keep-green constraint edits (§2.5) → `.venv/bin/python -m pytest tests/test_loop_epoch_recovery.py -q` → **red** (E1 accepted-today; E3/E4 same gate; O1 streak zeroes today; O3 conductor not called today — canary absent; O4 never disengages under read-only… full-outage variant stays red on resume; O2/S1 red: raise/512 lines).
2. Implement §2.3 (epoch) → E1-E4 green; §2.1+§2.2+§2.4 → O2/O3/S1 green; P4 restructure → O1/O4 green.
3. Keep-green sweep (explicit): `pytest tests/test_loop_robustness.py tests/test_loop_lock_safety.py tests/test_framework_characterization.py tests/test_pregeneration_divergence.py tests/test_reset_reprime.py tests/test_round3_fix_b.py tests/test_job_queue_lifecycle.py tests/test_job_queue_params.py tests/test_async_framework.py tests/test_stem_fetch_concurrency.py tests/test_adversarial_wave2.py -q`.
4. Full gate: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q` → **1198 passed / 26 skipped** (1188 + 10 new), zero regressions. Opt-in soak sanity: `SOAK=1 python -m pytest tests/test_soak_247.py -m soak -q` (probe/canary interplay traced green in §4 note).

---

## 4. Test-patch-point inventory (extraction-safety proof)

| Patch style | Sites (grep-verified) | Survives the move? |
|---|---|---|
| `patch.object(loop, "_submit_job" / "_await_jobs" / "_fetch_audio" / "_append_loop_audit" / "conductor")` | test_pregeneration_divergence (all 4 tests), test_reset_reprime, test_job_queue_lifecycle, test_job_queue_params, test_stem_fetch_concurrency, test_async_framework, characterization `_wire_loop_no_io`, test_llm_capture, test_audio_injection… | YES — instance attributes shadow any class resolution; unchanged by relocation |
| Direct assignment `loop._submit_job = AsyncMock()` / `loop._pre_generate_next_loop = …` / `loop._audit = …` | test_reset_reprime, test_loop_robustness (`_fresh_path_pregen_clearer`), test_job_queue_lifecycle | YES — same shadowing argument |
| `patch.object(AsyncFrameworkLoop, "start", …)` (class-level) | test_loop_robustness S1 | YES — `start` stays in loop_orchestrator |
| `monkeypatch.setattr(loop_orchestrator, "AsyncFrameworkLoop", _factory)` | test_loop_robustness S3; test_round3_fix_b B6 calls `loop_orchestrator.run_framework_loop_async` | YES — `run_framework_loop_async` + module binding stay (decision 14) |
| String patches into `app.framework.loop_orchestrator` namespace | **NONE** (grep over tests/: only `framework_state`, `audio_fetch` string patches exist) | N/A — no module-level function patches to break |
| Frozen re-exports on `loop_orchestrator` (`AuditAdapter`, `_flush_lock`, `append_loop_audit`, `flush_recording_buffers`, `process_actions`, `# noqa: F401` block) | routes/shows.py, tests import from here / from `framework_main_async` shim | UNTOUCHED — the import block stays verbatim |
| AST source guards (file lists) | test_loop_lock_safety L39-42, L394, L461 | GAIN `loop_delegates.py` (coverage follows the code; decision 16) |
| Hand-crafted `_pregen_results` dicts without the epoch key | test_round3_fix_b:313,331,521; test_reset_reprime:208; test_adversarial_wave2:487 | GREEN via the epoch-0 default (§1.1.3) — none of these drives reset |
| Soak `_SoakJobQueue` (probe raises exactly in PG windows; submit ditto) | test_soak_247 | GREEN — probe-success ⊆ submit-success in that fake (both keyed on `_in_pg_window`); canary adds one successful submit + one abandon per recovery, and no assertion pins exact `submit_calls` (only `submit_failures >= 3`, `abandon_calls > 0`) |
| Characterization suite + divergence suite | test_framework_characterization.py, test_pregeneration_divergence.py | UNTOUCHED and green (verified: instance-level patches only; no resets; snapshots lack the epoch key → epoch 0) |

## 5. Import-graph proof (no cycles)

```
loop_delegates ──▶ pregeneration ──▶ loop_steps ──▶ framework_state / conductor_interaction /
      │                                                   domain_audio / audit_recording
      └──────────▶ conductor_interaction (build_track_prompt)
loop_orchestrator ──▶ loop_delegates, loop_steps, pregeneration? (NO — dropped), ports, mixer, …
framework_main_async (shim) ──▶ loop_orchestrator (unchanged)
```
Nothing under `app/framework/` imports `loop_orchestrator` or `loop_delegates` except `framework_main_async` (shim) → the new edge `loop_orchestrator → loop_delegates` is a leaf; `loop_delegates → pregeneration → loop_steps` is the existing chain (pregeneration already imports loop_steps today). No cycle; `ruff check` (F401 unused after the move) is part of the gate.

---

## 6. Invariant compliance (plan §Invariants)

| # | How respected |
|---|---|
| 1 — lock discipline | ONE new statement inside an existing `state.lock` block (`self._pregen_epoch += 1` — pure int, no I/O; passes `test_no_io_inside_state_lock_in_orchestrator` by construction). One new dict-literal key inside P11's lock block. The canary performs I/O OUTSIDE any lock (P4 holds none). `sync_lock` untouched. |
| 2 — style / hexagonal | `_canary_submit` ≤ 20 LOC, `_canary_payload` pure ≤ 20; no `Any` added beyond existing signatures' use; one spelling per concept (`_pregen_epoch` / `"pregen_epoch"`, `_canary_submit`); ports untouched (decision 10); both orchestrator files < 500 pinned by S1; loop_steps growth +9 lines (pre-existing 1183 brownfield debt is out of FU-2 scope — the FU row names only loop_orchestrator). |
| 3 — audio thread | Zero mixer-thread code touched; P2/P3/P4/P11/P12 are all loop-task side. |
| 4 — LLM capture | Audit capture paths untouched; canary adds NO audit rows (not a conductor decision); fallback iterations already capture as today. No data dropped/truncated. |
| 5 — worker restart semantics | Untouched (canary abandonment rides the existing REL-12a best-effort `abandon_jobs`; a raced claim just generates one redundant stem — bounded, logged-by-count only). |
| 6 — regression per fix | E1-E4 pin the epoch counter (incl. the audit's exact "accepted once at revisiting index" scenario); O1-O4 pin the streak semantics (incl. the read-only-standby mode that motivated the follow-up); S1 pins the split; §2.5 keeps B3/B4 and the guards honest. |

## 7. Acceptance checklist (maps to the FU-2 row)

- [ ] Stale pre-reset pregen result (matching `loop_idx`, old epoch) rejected at P2 even when indices revisit → E1 (+E3 double-reset, E4 in-flight completion)
- [ ] Fresh-epoch result accepted → E2
- [ ] Epoch survives `should_reset` (monotonic, never back to 0) → E3
- [ ] Streak does NOT reset on read-probe success while submits fail (read-only-PG fake) → O1 (+O2 containment)
- [ ] Streak resets on first successful submit → O3 (mechanism seam already pinned by existing B5)
- [ ] Conductor skip engages + disengages through a full outage window → O4 + rewritten B4
- [ ] Characterization suite untouched + green post-extraction → §3.2 step 3
- [ ] Both files under 500 lines → S1 (`loop_orchestrator` ~392, `loop_delegates` ~225)
- [ ] `ruff check` clean; full gate 1198 passed / 26 skipped

## 8. Risks / out of scope

- **Canary row on a raced claim:** if the worker claims the canary between submit and abandon (sub-second window), one redundant generation runs per recovery event. Bounded and rare; documented residual (same class as REL-12a's best-effort semantics).
- **Read-only mode costs one failed INSERT per loop** while the skip is engaged — deliberate trade (cheap, no LLM call, no stem churn) vs. the previous every-cycle conductor call; noted in the P4 comment.
- **Recovery latency nuance:** resume needs probe success AND canary success; in a writable recovery both fire in the same P4 (one loop). In exotic topologies where reads recover before writes, the skip correctly persists until writes work — the intended semantics.
- **Epoch default-0 gate** relies on the documented invariant that every real writer stamps the key; a future writer of `_pregen_results` must stamp it (the S1/E-suite + the P2 comment enforce; `run_pregeneration` and the P12 fabrication are the only writers — grep-verified).
- Out of scope: `loop_steps.py` split (1183 lines, pre-existing debt, not named by FU-2), asyncpg connection-loss callback / half-open-conn follow-up (keepalives landed in FU-1), C8 content-hash dedup, post-reset audit `loop_index` duplicates (cosmetic, disclosed in rel-02), moving `run_framework_loop_async`.

## 9. Commit shape

One branch `rel-fu-2-loop`, TDD order per §3.2: (1) `test(rel-fu-2): pregen epoch gate + submit-canary recovery + orchestrator split pins — TDD red`; (2) `fix(rel-fu-2): epoch-stamped pregen results; streak resets on submit only (write canary); loop_orchestrator delegates split under 500`; (3) `docs(rel-fu-2): audit/CLAUDE/plan status`. Land on `main` via the parent, Conventional Commits.
