# Red-Team Review — E1–E6 Refactor Plan (Sequencing & Behavior-Preservation)

**Reviewer:** adversarial code-review subagent
**Sources grounded:** `refactor/plan.md`, `refactor/explore/01–04`, `app/framework/framework_main_async.py` (baseline `906a49b`, 542 passed verified), `CLAUDE.md`, `app/framework/framework_state.py`, test files.
**Method:** read actual source, measured real line counts, verified every cited line anchor.

---

## VERDICT: PLAN_NEEDS_REVISION

Two blockers (Phase 6 stem-cache sharing unspecified; Phase 7a DoD §2 "≤20-line steps" unachievable as written) plus a Phase 7b `__init__` DI ambiguity that determines whether E5 (hexagonal testability) is actually delivered. The risk register is strong on `state.cache_stem` divergence and `_flush_lock` identity but **blind to `self.stem_cache` cross-path coordination and inline cache-key drift** — the more likely silent regressions.

---

## Findings

### PHASE 6 | BLOCKER | pregeneration.py design / framework_main_async.py:203,485–547,1066–1111 | PreGenerator `stem_cache` ownership is unspecified — silent cache-coordination break

The foreground (P7/P8/P9) and background (`_pre_generate_next_loop`) paths share **one** `self.stem_cache` instance dict (declared L203). Both compute an identical inline cache key `f"{m_id}_{prompt}_{bpm}_{key}_{bars}"` (L485 vs L1066) and read/write the same dict. This sharing is **load-bearing**: at L1068 pregen checks `if cache_key in self.stem_cache:` and **skips job submission** (`continue`) when the foreground already cached the audio — this is how pregen avoids redundant GPU work and duplicate stems.

The plan says PreGenerator "composes the Phase 2–5 modules" and is constructed in `__init__`, but **never specifies** whether PreGenerator receives the loop's `self.stem_cache` or gets its own dict. If it gets its own:

- The L1068 check always misses → pregen re-submits every job the foreground already fulfilled.
- Cache-maintenance (stale eviction, L696–698, foreground-only) never touches pregen's cache.
- The two divergence regression tests (`test_pregeneration_does_not_route_through_cache_stem`, `test_foreground_loop_routes_through_cache_stem`) test `state.cache_stem` (the separate `GlobalState._stem_cache` LRU, `framework_state.py:199`), **not** `self.stem_cache` sharing. A separate-cache regression passes green.

**Fix:** Mandate `PreGenerator` is constructed with a reference to the loop's cache (`PreGenerator(stem_cache=self.stem_cache, …)`). Extract the cache-key computation into a single pure function used by **both** paths. Add `test_pregen_skips_job_when_foreground_already_cached` (run a foreground fetch populating `self.stem_cache`, then run pregen for the same stem, assert no new job submitted).

---

### PHASE 7a | BLOCKER | DoD §2 / framework_main_async.py:258–760 | The "≤20-line steps" goal (E2) is unachievable — 13/14 phases exceed 20 lines; sole carve-out estimate off by 2×

Measured against actual source line ranges:

| Phase | Lines | >20? |
| ------- | ------: | :----: |
| P0 init | 2 | |
| P1 wait-for-start | 29 | YES |
| P2 pregen-decision | 41 | YES |
| P3 read-snapshot | 50 | YES |
| P4 call-conductor | 23 | YES |
| P5 parse-actions+audit | 22 | YES |
| P6 build-next-stems | 36 | YES |
| P7 submit-jobs | 28 | YES |
| P8 await+fetch | 24 | YES |
| P9 tile-audio | 28 | YES |
| P10 commit-to-mixer | 47 | YES |
| P11 commit-state | **85** | YES |
| P12 record+cache+pregen | 26 | YES |
| P13 await-transition | 39 | YES |

DoD §2 says "each ≤20 lines EXCEPT `_step_commit_state` (~40)". Reality: P11 is **85 lines** (lock body alone ~74, L616–690), not 40; and **12 other phases** also exceed the cap with no carve-out. Only P0 (2 lines) qualifies. The DoD gate as written **cannot be satisfied**, leaving E2's success metric undefined. Executing the plan "as written" produces 13 violations of the stated criterion.

**Fix:** Revise DoD §2 to a realistic target ("each `_step_*` ≤50 LOC; `_step_commit_state` ≤90 as atomic single-lock") and/or budget further sub-decomposition (e.g., extract the pregen action-log rebuild loop L640–658 into a pure `_build_action_log(actions, stems)` helper, shrinking P11 by ~20 lines). Acknowledge that ≤20 is aspirational, not a gate.

---

### PHASE 7b | CONCERN | loop_orchestrator.py `__init__` / brief-02 §A1 | "injected-port" design is ambiguous — may silently fail to deliver E5

Phase 7b says `__init__` "now **constructs** `ConductorLLMAsync()`, `PostgresJobQueueAdapter()`, …" — "constructs" implies internal creation, but "injected-port" implies DI parameters. Verified: **12 test sites + `app/app_ui.py:90`** (via `run_framework_loop_async` → `AsyncFrameworkLoop(session_id)`) all call with exactly ONE positional arg.

- If ports become **required** params → loud `TypeError`, caught by suite (not silent).
- If ports are **internal-only** (no params) → signature preserved, BUT ports are never injectable; `ports.py` Protocols (Phase 1) are pure documentation; CLAUDE.md "Inject dependencies through constructor/parameter, not global/import" is violated; E5 hexagonal testability is **silently unmet**.

The DoD does not require backward-compatible constructor signature, and Phase 7b's gate (`test_frozen_api_importable`) checks **imports**, not instantiation — so it would not independently catch an E5 shortfall.

**Fix:** State explicitly: `__init__(self, session_id, *, conductor=None, jobs=None, audio=None, audit=None, pregen=None)` with `None` defaults constructing the real adapter. This preserves the one-arg call AND delivers injectable ports. Add `assert AsyncFrameworkLoop(uuid4()) is not None` to the frozen-API test.

---

### PHASE 2 | CONCERN | framework_main_async.py shim / test_worker_fetch_audio.py:43,44,74,75 | "Belt-and-suspenders" dual-binding does NOT neutralize silent string-patch no-op

After extraction, `decode_aac` / `create_garage_client_from_env` are **called** inside `audio_fetch.py`'s namespace. A patch targeting `app.framework.framework_main_async.decode_aac` (still bound via the top-level re-import the plan keeps) replaces the **shim module's** attribute but does **not** affect the call, which resolves `decode_aac` via `audio_fetch`'s globals → **silent no-op**. Binding the name on both modules is irrelevant; what matters is the namespace of the call-site global lookup. The plan frames the dual-binding as a risk mitigation ("belt-and-suspenders for any other string-patch consumers") — but brief-02 §D verified there are **no** other consumers, so the binding is dead weight that gives **false confidence**. The guard test (`test_decode_aac_patch_actually_applies`) is the *only* real protection.

**Fix:** Downgrade the framing: "kept for import-compatibility only; patch-safety relies solely on migrated patch strings + guard test." Remove any implication that dual-binding neutralizes the no-op risk.

---

### PHASE 6 | CONCERN | pregeneration.py / framework_main_async.py:485,1066 | Two divergence tests miss cache-key drift and silence-fallback drift

The tests check `state.cache_stem` call/no-call, but:

(a) **Cache-key drift:** the key `f"{m_id}_{prompt}_{bpm}_{key}_{bars}"` is duplicated inline in foreground (L485/L544) and pregen (L1066/L1108). If the extraction refactors one path's key (e.g., `_build_prompt` output changes, or a variable rename changes interpolation), keys diverge silently and cross-path cache-hits (the L1068 skip) break. No test covers key consistency.

(b) **Silence-fallback drift:** if the refactor drops the `self.stem_cache[cache_key]` write in pregen (L1096), `tracks_data[i]` stays `None` → the silence fallback at L~1140 produces all-zero arrays. The pregen tests (#4/#12) assert `_pregen_results` keys/`loop_idx`/`master_bpm` but **not** that `prepared_tracks` audio is non-zero → test passes with silent stems.

**Fix:** Add `test_foreground_and_pregen_produce_identical_cache_key` (same inputs → same key string). Assert `np.any(prepared_tracks[i][0] != 0)` in the pregen result-shape test.

---

### PHASE 7a | CONCERN | framework_main_async.py:342–391 | `_step_read_snapshot` risks re-introducing lock-across-file-I/O (CLAUDE.md + brief-02 §B)

P3 currently acquires `state.lock` (L343), reads ~10 state attrs, **releases lock** (L382), **then** reads `config/models_config.json` via `open(_config_path)` (L377–391) — file I/O deliberately **outside** the lock. The plan's verification gate greps for `state.lock` only inside `_step_call_conductor` and `_step_submit_jobs`; it does **not** check `_step_read_snapshot`, which is the phase that interleaves lock + file-read. A careless decomposition that nests `open()` inside the `async with state.lock:` block introduces lock-held-across-file-I/O — exactly the race CLAUDE.md forbids.

**Fix:** Add `_step_read_snapshot` to the lock-scope grep gate; add a guard test asserting no `open(` call is lexically nested inside any `async with state.lock:` block across all `_step_*` methods (source-inspection test, like `test_safe_prompt_parsing_retains`).

---

### PHASE 7b | CONCERN | framework_main_async.py:1189–1192 | `python -m app.framework.framework_main_async` breaks after shim conversion

The `if __name__ == "__main__":` smoke-test block + `main()` coroutine move to `loop_orchestrator.py`. The shim re-exports `main` but cannot re-export a `__main__` guard. Running `python -m app.framework.framework_main_async` after Phase 7b executes nothing. Blast radius is low (CLAUDE.md lists `python -m app.app_ui` as the production entry), but the plan should document it.

**Fix:** Add `if __name__ == "__main__": asyncio.run(main())` stub to the shim, or document that the standalone smoke entry is now `python -m app.framework.loop_orchestrator`.

---

### PHASE 4 | CONCERN | conductor_interaction.py:process_actions / framework_main_async.py:131–132 | Gap-1 test must assert IN-PLACE mutation flow, not just return-value `_age`

`process_actions` does `orig = s.get("_original_details", {}); orig["_age"] = …`. If `s` has **no** `_original_details` key, `orig` is a fresh `{}` and the mutation is lost (appended track = `{"_age": N}` only). The plan's gap-1 test asserts "retained `_age` = original `_age`+1" on the **returned** tracks, but does not verify the mutation propagated into the **caller's live `active_stems`** list-objects (consumed by P6/P11 via `_original_details`). A refactor that defensively `copy.deepcopy`s `active_stems` would pass the return-value test but break the live-mutation contract silently.

**Fix:** Gap-1 test must assert `active_stems[idx]["_original_details"]["_age"]` was mutated **in-place on the input list** after the call (using stems that already carry `_original_details`).

---

### PHASE 3 | SUGGESTION | audit_recording.py | Clarify show_id read: snapshot vs live-under-lock

The plan says `append_loop_audit` "takes an explicit state/snapshot rather than `self`," but the current code reads `state.current_show_id` **inside** `state.lock` (L892) to decide early-return. If the extracted function checks show_id from a **pre-lock snapshot** but writes buffers under a **later** lock, there is a TOCTOU window (show stops between snapshot and write). Since the delegate still acquires the lock and the plan says "lock scope unchanged," this is likely fine — but the "snapshot" language contradicts the lock-internal show_id check.

**Fix:** State that `append_loop_audit` reads `current_show_id` under its own `state.lock` acquisition (as today); "snapshot" refers only to the `active_stems` parameter, not show_id.

---

### PHASE 0 | SUGGESTION | test_frozen_api.py | Frozen-API smoke checks imports, not constructor signature

`test_frozen_api_importable` imports names and asserts non-None. It does **not** instantiate `AsyncFrameworkLoop(uuid4())`. A required-param signature change passes this gate (caught later by the 12 instantiation sites in the broader suite, but not by this specific gate).

**Fix:** Add `AsyncFrameworkLoop(uuid4())` instantiation to the frozen-API test so the gate independently catches constructor-signature drift.

---

## Per-phase audit answers (a–d)

| Phase | (a) drops behavior? | (b) violates frozen constraint? | (c) transient broken state? | (d) gate sufficient? |
| ------- | :---: | :---: | :---: | :---: |
| 6 | **YES** — stem_cache sharing untested | No | No (1 commit) | **NO** — misses stem_cache + cache-key drift |
| 7a | No (if atomicity preserved) | No | No (1 commit) | **NO** — DoD §2 unachievable; lock-scope grep misses P3 |
| 7b | `__main__` entry lost; E5 maybe unmet | Constructor-sig ambiguity (A1) | No (1 commit) | Partial — import gate misses signature/E5 |
| 2 | No | No (if strings migrated) | No (1 commit) | YES w/ guard test (belt-and-suspenders is not the protection) |
| 4 | Risk: deepcopy breaks mutation | No | No | **NO** unless gap-1 tests in-place mutation |
| 3 | No | `_flush_lock` identity preserved | No | YES |
| 5 | No | No | No | YES |
| 8–10 | N/A (additive/mechanical) | No | No | YES |

---

## Residual risks after addressing findings

1. The cache-key inline duplication (4 sites: L485/544/1066/1108) remains a drift hazard even with a shared `stem_cache` — extraction into one function is the durable fix.
2. Phase 11 (deferred Mixer port) leaves 5 private-member reaches (`_add_track_internal`, `_ensure_stereo`, `_current_loop_duration`, `mixer.lock`) — correctly deferred but the typing-only `MixerController` Protocol may give false confidence that the boundary is clean.
3. No characterization test covers the `_run_loop` pregen-ready vs fresh-LLM branch producing **identical downstream state deltas** (brief-04 HIGH gap 1) — a Phase 7a merge of these branches could desync without detection.
