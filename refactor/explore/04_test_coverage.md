# 04 — Test Coverage Map: Framework Layer (`framework_main_async.py` / `framework_state.py`)

Scope: existing test coverage for mc-clanker's async framework + global-state layer, to seed the red→green TDD phase of the refactor. Reads: `tests/test_async_framework.py`, `tests/test_loop_fixes.py`, `tests/test_concurrency_fixes.py`, `tests/test_simulation.py`, `tests/conftest.py`. Symbols under test live in `app/framework/framework_main_async.py` (MFA) and `app/framework/framework_state.py` (FWS). `test_simulation.py` mostly targets `simulation/`, `slop_harness/`, and `framework_conductor_async.py`; only its conductor-adjacent cases are summarized.

Line refs are to the current tree (MFA = `app/framework/framework_main_async.py`, FWS = `app/framework/framework_state.py`).

---

## (A) Test → Behavior Matrix

Legend for "Mocks" column: **LLM** = conductor / `get_next_state_async` / chat client; **DB** = `DatabaseManager` / `GeneratorJob` / `session()`; **S3** = Garage `_fetch_audio` / `get_object`; **Mixer** = mixer stand-in; **—** = none.

### tests/test_async_framework.py

| # | Test | Asserts | Symbol exercised (file:line) | Mocks |
| --- | ------ | --------- | ------------------------------ | ------- |
| 1 | `test_app_uses_async_framework_not_sync` | `run_framework_loop_async` importable, not None, "async" in its name | MFA:1155 `run_framework_loop_async` | — (imports only) |
| 2 | `TestPreGeneration.test_async_framework_loop_has_pregen_attributes` | loop has `_pregen_task=None`, `_pregen_done` is `asyncio.Event`, `_pregen_loop_idx==0` | MFA:190 `AsyncFrameworkLoop.__init__` (attrs ~199–203) | — |
| 3 | `TestPreGeneration.test_pregen_done_event_is_clearable` | `_pregen_done` set()→is_set(), clear()→not is_set() | MFA:201 `_pregen_done` Event | — |
| 4 | `TestPreGeneration.test_pre_generate_stores_results` | after `_pre_generate_next_loop(2, snapshot)` with mocked conductor: `_pregen_results` not None, `loop_idx==2`, `master_bpm==128`, `_pregen_done.is_set()` | MFA:979 `_pre_generate_next_loop` (full path incl. `process_actions`@118, `_build_prompt`@772) | LLM, DB(`_submit_job` AsyncMock), S3(`_fetch_audio` AsyncMock) |
| 5 | `TestPreGeneration.test_pre_generate_handles_llm_failure` | conductor raises → no raise, `_pregen_done.is_set()`, `_pregen_results` not None (fallback) | MFA:979 `_pre_generate_next_loop` LLM-exception branch (try/except ~1008–1018) | LLM only |
| 6 | `TestPreGenResultsUsage.test_pregen_ready_check_logic` | truth table of `loop_idx>1 and results and results['loop_idx']==loop_idx` | (replicates logic of MFA:266–271 `pregen_ready`) | — (pure-logic, no real symbol) |
| 7 | `TestNextLoopPreGeneration.test_needs_pregen_logic` | truth table of `loop_idx>1 and (no task or task.done())` | (replicates logic of MFA:580 `needs_pregen`) | — (pure-logic) |
| 8 | `TestNextLoopPreGeneration.test_pregen_skip_when_loop_already_queued` | skip-path `_pregen_results` dict has expected keys/values | (documents structure of MFA:589–600 skip branch) | — (local dict, no real symbol) |
| 9 | `TestNextLoopPreGeneration.test_pregen_results_structure` | hardcoded dict has all 9 required fields | (documents contract of MFA:1133–1146 result dict) | — |
| 10 | `TestNextLoopPreGeneration.test_pregen_ready_check_with_matching_loop_idx` | extended truth table of pregen_ready | MFA:266–271 (logic mirror) | — (pure-logic) |
| 11 | `TestNextLoopPreGeneration.test_next_loop_idx_calculation` | `next_loop_idx = loop_idx + 1` | (documents MFA:583) | — (pure arithmetic) |
| 12 | `TestNextLoopPreGeneration.test_pregen_task_stores_results_correctly` | `_pre_generate_next_loop(3, …)` → `loop_idx==3`, `master_bpm==120`, `master_key=="D major"`, done set | MFA:979 `_pre_generate_next_loop` | LLM, DB(`_submit_job`) |
| 13 | `TestNextLoopPreGeneration.test_pregen_handles_conductor_failure` | conductor raises → done set, fallback preserves `master_bpm==128` | MFA:979 LLM-failure branch | LLM only |
| 14 | `TestMixerNextLoopIntegration.test_set_next_loop_called_with_correct_tracks` | local MockMixer receives `(tracks, current_sample+duration)` | (documents intended call of MFA:500–504; uses local mock Mixer, not real `Mixer.set_next_loop`@framework_mixer.py:66) | Mixer (local class) |
| 15 | `TestMixerNextLoopIntegration.test_next_loop_end_sample_calculation` | `end = current_sample + duration` | (documents MFA:502) | — (arithmetic) |
| 16 | `TestNoGapPrevention.test_set_next_loop_populates_next_loop_audio` | local MockMixer stores `next_loop_audio` + `current_loop_end_sample` | (contract mirror; real `set_next_loop`@mixer:66 does NOT set `current_loop_end_sample`) | Mixer (local) |
| 17 | `TestNoGapPrevention.test_transition_uses_next_loop_audio_when_available` | when within deadline, `next_loop_audio` consumed, track added, end reset | (documents mixer._callback transition logic@mixer:~166–200; numpy mock) | Mixer (local) |
| 18 | `TestNoGapPrevention.test_no_gap_when_next_loop_audio_is_empty` | empty next_loop_audio → `_extend_tracks_for_loop` invoked, end extended | (documents mixer:~206 `_extend_tracks_for_loop`) | Mixer (local) |
| 19 | `TestNoGapPrevention.test_loop_switch_deadline_prevents_timing_race` | `deadline_ms=50` ⇒ <5% of a bar buffer | ⚠ **STALE**: real `loop_switch_deadline_ms == 1000` (mixer:43), not 50 | — (arithmetic, wrong constant) |
| 20 | `TestNoGapPrevention.test_next_loop_end_sample_must_be_in_future` | local MockMixer raises ValueError if `end ≤ current_sample` | ⚠ **NOT IMPLEMENTED**: real `set_next_loop` (mixer:66) performs no such guard | Mixer (local) |
| 21 | `TestLastActions.test_safe_prompt_parsing_retains` | `prompt.split(',')[1].strip()` parses sub-family; no-comma falls back to whole | (documents MFA:347,435,461,511 action-log parsing) | — (pure string logic) |

### tests/test_loop_fixes.py  (autouse fixture `_reset_audit_state` saves/restores `state` singleton)

| # | Test | Asserts | Symbol exercised (file:line) | Mocks |
| --- | ------ | --------- | ------------------------------ | ------- |
| 22 | `test_calc_duration_normal_bpm` | `calc_duration(120,4)==8.0` | MFA:47 `calc_duration` | — |
| 23 | `test_calc_duration_zero_bpm_does_not_raise` | `calc_duration(0,4)==8.0` (B10 guard) | MFA:47 B10 branch (line 54 `safe_bpm` fallback) | — |
| 24 | `test_calc_duration_negative_bpm_falls_back` | `calc_duration(-10,4)==8.0` | MFA:47 B10 branch | — |
| 25 | `test_to_two_channel_promotes_mono` | mono→shape `(10,2)` | MFA:58 `_to_two_channel` (column_stack path@63) | — |
| 26 | `test_to_two_channel_preserves_stereo` | stereo stays `(10,2)` | MFA:58 (no-op path@62) | — |
| 27 | `test_mono_tile_no_longer_raises` | `np.tile(_to_two_channel(mono),(4,1))` shape `(400,2)` | MFA:58 + B11 tiling contract (used @433,692,1110) | — |
| 28 | `test_append_loop_audit_populates_buffers` | 1 llm_interaction row w/ exact `_LLM_INTERACTION_COLS`; 2 action rows w/ `_SHOW_ACTION_COLS`; `show_id/loop_index/reasoning/was_fallback/relative_time_ms` correct | MFA:881 `_append_loop_audit` + helpers `_relative_show_ms`@914, `_audit_prompt_context`@919, `_audit_action_row`@930, `_audit_stem_details`@945, `_audit_action_description`@967; FWS buffers | — (real state, sets `current_show_id`/`start_time` directly) |
| 29 | `test_append_loop_audit_marks_fallback` | `name=="Fallback State"` ⇒ `was_fallback True` | MFA:890 `is_fallback` flag | — |
| 30 | `test_append_loop_audit_noop_without_show` | `current_show_id is None` ⇒ buffers stay empty | MFA:887–889 early-return | — |
| 31 | `test_flush_recording_buffers_is_gated_by_flush_lock` | holding `_flush_lock` blocks a concurrent `flush_recording_buffers`; releasing lets it finish | MFA:71 `_flush_lock`, MFA:74 `flush_recording_buffers` | — (empty buffers ⇒ early-return before DB import) |
| 32 | `test_run_loop_retries_after_transient_exception` | `_run_loop` returns (no raise) after a body `RuntimeError`; audit ran ≥2× ⇒ watchdog retried | MFA:258 `_run_loop` outer try/except (B1 watchdog @701–707), `_finish_loop`@251, CancelledError path@695 | LLM(conductor), `_append_loop_audit`, `_pre_generate_next_loop`, `asyncio.sleep` (monkeypatched instant), Mixer=`_FakeMixer` |

### tests/test_concurrency_fixes.py  (uses fresh `GlobalState()` instances, NOT the singleton)

| # | Test | Asserts | Symbol exercised (file:line) | Mocks |
| --- | ------ | --------- | ------------------------------ | ------- |
| 33 | `test_vestigial_next_loop_ready_event_removed` | no `next_loop_ready` / `next_loop_tracks` attrs | FWS:72 `__init__` (A11 removal) | — |
| 34 | `test_reset_does_not_reference_removed_event` | `reset()` no raise; `is_generating`→False | FWS:215 `reset` | — |
| 35 | `test_snapshot_mixer_state_returns_independent_copies` | mutating returned soloed/muted/volumes does not touch live state | FWS:339 `snapshot_mixer_state` (A2 copies under sync_lock) | — |
| 36 | `test_broadcast_audio_logs_recording_failure_once_per_handle` | failing show sink logged exactly once across 3 chunks; new distinct handle logs again; write attempted 3× | FWS:362 `broadcast_audio` + FWS:394 `_write_recording_sink` (B9 once-per-handle) | file-handle MagicMock |
| 37 | `test_broadcast_audio_snapshots_recording_handle_under_lock` | `handle.write` called once with `b"pcm"` | FWS:362 snapshot-under-sync_lock (A1) | file-handle MagicMock |
| 38 | `test_trigger_shutdown_closes_recording_handles` | both show + export handles flush()+close(); flags cleared to None/False | FWS:424 `trigger_shutdown` + FWS:461 `_close_recording_handles_locked` (B8/A4) | file-handle MagicMock |
| 39 | `test_trigger_shutdown_sets_run_flags_under_lock` | `is_running`/`is_generating`→False | FWS:424 (A4) | — |

### tests/test_simulation.py  (mostly `simulation/` + `slop_harness/`; only boundary cases summarized)

| # | Test | Asserts | Symbol exercised | Mocks |
| --- | ------ | --------- | ------------------ | ------- |
| 40–42 | `TestExtraBodyParameter.*` | `call_async` & `get_next_state_async` accept `extra_body`; extra_body reaches `chat.completions.create` | `framework_conductor_async.ConductorLLMAsync` (not MFA/FWS) | LLM client (42) |
| 43 | `TestDebugPrintRemoved.test_get_next_state_async_has_no_debug_print` | no `print(` in source | conductor source inspection | — |
| 44,47 | Jockey source-inspection tests | `run_loop` calls `call_async`, no `"{}"`, has `apply_actions` | `simulation/jockey` | — |
| 48 | `TestSerializationFailure.test_serialization_failure_does_not_corrupt_state` | real `SlopJockey.run_loop` w/ non-serializable response ⇒ `was_applied True`, retain present, `loops_completed==1` | `simulation.jockey.run_loop` | LLM(conductor.call_async) |
| 45,46 | `TestVibePromptBankThreadSafety.*` | source has lock/class-level templates; 100 concurrent `sample()` no error | `slop_harness.vibe_prompt_bank` | rng MagicMock |
| 49,50 | `TestEnableThinkingCLI.*` | CLI has `--enable-thinking`; `SlopJockey.__init__` accepts enable_thinking/extra_body | `simulation/cli`/`jockey` | — |
| 51,52 | Schema-drift tests | production & harness action_types == `{retain, add, remove}` | `app.lib.constants.get_response_format_schema` + `slop_harness.llm_client` | — |

> **Observation:** `test_simulation.py` does **not** directly exercise `_run_loop`, `_submit_job`, `_fetch_audio`, `_append_loop_audit`, or `GlobalState` locking. Its framework relevance is limited to the conductor boundary (#42). Do **not** count it as framework-layer coverage.

---

## (B) Behavioral Contracts Currently Locked by Tests

Grouped by subsystem. "Lock strength" = how directly the real production symbol is exercised.

### B1 — Duration / audio-shape math (LOCKED, strong)

- `calc_duration(bpm,bars)` returns `beats/(bpm/60)`; **non-positive/NaN BPM falls back to 120** (no ZeroDivisionError) — MFA:54 `safe_bpm`. (#22–24)
- `_to_two_channel` promotes 1-D → `(N,2)` via `column_stack`, passes 2-D through unchanged — MFA:58. (#25–26)
- Mono audio is safely tileable: `np.tile(_to_two_channel(mono),(repeats,1))` never raises — B11 contract used at MFA:433,692,1110. (#27)

### B2 — Pre-generation result shape & lifecycle (LOCKED, medium — real method but helpers mocked)

- `_pre_generate_next_loop(idx, snapshot)` populates `_pregen_results` with required fields (`prepared_tracks, loop_duration_samples, loop_idx, next_stems, master_bpm, master_key, set_name, reasoning, actions`) and sets `_pregen_done`. — MFA:1133–1146. (#4,#9,#12)
- `loop_idx` stored equals the `for_loop_idx` argument. — MFA:1138. (#4,#12)
- LLM failure inside pre-gen is caught: fallback response built (retain-all), `_pregen_results` still set, `_pregen_done` still signaled, original BPM preserved. — MFA:1008–1018. (#5,#13)
- `AsyncFrameworkLoop.__init__` exposes `_pregen_task`, `_pregen_done` (Event), `_pregen_loop_idx`, `_pregen_results`. — MFA:199–203. (#2,#3)

### B3 — Pre-gen gating predicates (LOCKED, weak — logic mirrored in local fns, NOT the real expressions)

- `pregen_ready = loop_idx>1 and _pregen_results is not None and _pregen_results['loop_idx']==loop_idx`. — MFA:266–271. (#6,#10)
- `needs_pregen = loop_idx>1 and (_pregen_task is None or _pregen_task.done())`. — MFA:580. (#7)
- `next_loop_idx = loop_idx + 1` for both start & skip paths. — MFA:583,602. (#11)
- Skip-path (loop already queued) result dict shape. — MFA:589–600. (#8)

### B4 — Audit buffering / show trail (LOCKED, strong — real `_append_loop_audit`, no mocks)

- With `current_show_id` set, one `llm_interaction_buffer` row + one `action_buffer` row **per action** are appended, each matching the exact bulk-insert column set. — MFA:881–912. (#28)
- `was_fallback` True iff `conductor_response["name"]=="Fallback State"`. — MFA:890. (#29)
- No show (`current_show_id is None`) ⇒ no-op, buffers untouched. — MFA:887–889. (#30)
- `relative_time_ms ≥ 0` and derived from `current_show_start_time`. — MFA:914. (#28)

### B5 — Flush serialization (LOCKED, medium)

- `flush_recording_buffers` is serialized by module-level `_flush_lock`; a second flush blocks while the lock is held. — MFA:71,74. (#31)

### B6 — Loop watchdog / resilience (LOCKED, medium — single end-to-end path)

- `_run_loop` does **not** permanently die on a transient body exception: it logs, backs off (`LOOP_RETRY_BACKOFF_SECONDS`), and retries the next iteration. — MFA:701–707 (outer except), MFA:44 backoff. (#32)
- `asyncio.CancelledError` cleans up via `_finish_loop` and re-raises. — MFA:695–699. (#32 path, not directly asserted)

### B7 — GlobalState concurrency fixes (LOCKED, strong)

- No vestigial `next_loop_ready`/`next_loop_tracks`; `reset()` doesn't touch them. — FWS:72,215. (#33,#34)
- `snapshot_mixer_state` returns independent copies (set()/dict() under sync_lock). — FWS:339–360. (#35)
- `broadcast_audio` snapshots handles under sync_lock; writes outside lock. — FWS:362–392. (#37)
- Failing recording sink logged **once per distinct handle**, but write attempted every chunk. — FWS:394–410, `_last_recording_error_handle`. (#36)
- `trigger_shutdown` flushes+closes both recording handles, clears flags, flips `is_running`/`is_generating` under sync_lock. — FWS:424–486. (#38,#39)

### B8 — Simulation/jockey contracts (LOCKED, adjacent)

- extra_body flows conductor→API; debug print absent; serialization failure applies retain-fallback; schema action types `{retain,add,remove}` align prod↔harness. — (#40–52). Outside MFA/FWS.

---

## (C) Coverage Gap Candidates for red-TDD (ranked by refactor-break risk)

"Risk" = likelihood the refactor silently regresses this behavior × blast radius. Each is a concrete red-test target.

### 🔴 CRITICAL (untested, high regression risk)

1. **`process_actions` end-to-end retain/add/remove/dedup (MFA:118–178).**
   No test calls the real `process_actions`. It is the core action-transformation: retain ages stems (`_age+1`), add creates new stems, remove drops, and a composite key dedups. The only exercise is indirect via `_pre_generate_next_loop` with **empty actions** (so the branch bodies never run). A refactor that changes the dedup key or `_age` handling would pass all current tests. **Red test:** feed `{retain idx0, add Synth Lead, remove idx1, add-duplicate}` → assert ordering, `_age`, dedup.

2. **`_submit_job` real DB path (MFA:806–849).**
   Always mocked (`AsyncMock`). Never asserted: it builds a `GeneratorJob` with `status="pending"`, `expires_at=now+24h`, returns the flushed `job.id`, and uses `DatabaseManager.get_instance()`. No test verifies the row shape, status default, TTL, or return value type. **Red test:** in-memory/SQLite session, assert created row fields + returned UUID.

3. **`_fetch_audio` decode/error path (MFA:851–879).**
   Always mocked. Never asserted: empty bytes ⇒ `None`; `decode_aac` runs in executor; any exception ⇒ returns `None` (swallowed, logged) — a silent silence-stem path. **Red test:** patch `garage.get_object` to return `b""` (⇒ None) and to raise (⇒ None), assert no propagation.

4. **`_run_loop` first-loop branch (loop_idx==1) mixer handoff (MFA:555–565).**
   Untested. For loop 1, tracks are added at `mixer.current_sample` (not 0) and `current_loop_end_sample`/`_current_loop_duration` are set; `needs_initial_record` triggers `record_loop_transition(1,…)`. The watchdog test only ever reaches loop_idx via the mocked conductor and stops before this. **Red test:** drive one full loop-1 iteration with fakes, assert mixer received tracks at live position + `record_loop_transition` called.

5. **`_run_loop` subsequent-loop `set_next_loop` handoff (MFA:571–577).**
   Untested. For loop_idx>1 it calls `mixer.set_next_loop(tracks, next_loop_duration_samples=duration_samples, loop_idx=loop_idx)` — note the **keyword args and that it does NOT set `current_loop_end_sample` here**. The NoGap tests use local mocks with a *different* (positional) signature and a stale 50ms deadline. **Red test:** assert real `set_next_loop` receives the right tracks + duration_samples + loop_idx.

### 🟠 HIGH (untested, medium blast radius)

1. **`_run_loop` pregen-ready consumption vs fresh-LLM branch divergence (MFA:266–372).**
   Untested. The two branches build `conductor_response`/`next_stems` differently (pregen reads from `_pregen_results`; fresh branch calls conductor + `process_actions` + builds `state.next_stems`). No test covers that both branches end with identical downstream state mutations (BPM/key/set_name/last_actions/active_stems). A refactor merging these could desync. **Red test:** parametrize both paths, assert identical `state` deltas.

2. **`_run_loop` action-log building incl. retain/remove/add (MFA:343–359, 435–461, 506–522).**
   `test_safe_prompt_parsing_retains` only tests the string split. The full `last_actions` list construction (Retained/Added/Removed with parsed sub-family) on the real loop is untested, for both fresh and pregen branches. **Red test:** assert `state.last_actions` content after a loop.

3. **`flush_recording_buffers` DB write + failure re-queue (MFA:74–119).**
   Only the lock-gating is tested (#31) and it short-circuits on empty buffers so the DB code never runs. Untested: `bulk_insert_mappings` of both buffers, the `current_show_id is None ⇒ clear-and-return` branch (MFA:88–90), and the **failure re-queue** that prepends buffers back (`state.llm_interaction_buffer = llm_buffer + …`, MFA:117–118). **Red test:** populate buffers, fake a failing `session`, assert buffers restored.

4. **`_run_loop` exit conditions (MFA:264–265, 273–275, 684–685).**
   Untested: the `while not is_generating` idle-wait, the `shutdown_event.is_set()` break, the `not still_generating` early-continue, and the `_finish_loop` normal-exit call (MFA:709). Only the watchdog exception path is covered. **Red test:** start loop, set `shutdown_event` during idle wait, assert clean exit without raising.

5. **`_run_loop` end-of-iteration wait + transition recording (MFA:617–657).**
    Untested: the inner `while self.running` poll that calls `mixer.pop_transition_event()` and `record_loop_transition` under snapshot-outside-lock (A3 fix, MFA:624–633), the `current_ahead < 0.5` break, and pregen-done break. **Red test:** fake mixer returning a transition event, assert `record_loop_transition` invoked with snapshotted stems.

### 🟡 MEDIUM (untested helpers / smaller surface)

1. **`_build_prompt` template resolution (MFA:772–804).** Untested: engine `prompt_template` path vs default template, and the `.format(...)` substitution (major/sub/timbres/notation/fx/key/bpm/bars). Refactor of prompt shape is invisible to tests.

2. **`_relative_show_ms` / `_audit_stem_details` / `_audit_action_description` edge cases (MFA:914,945,967).** Partially covered by #28's happy path. Untested: `add` action details, out-of-range `idx`, `_relative_show_ms` when `start is None` (⇒ 0), remove/unknown action descriptions.

3. **`trigger_shutdown` subprocess kill + audio-client poison (FWS:442–459).** `test_state.py` covers some; concurrency tests (#38,#39) do **not** assert `active_subprocesses` cleared or client queues poisoned.

4. **`cache_stem` LRU eviction (`_MAX_STEM_CACHE=16`, FWS:202–213) + B7 route-through (MFA:451–452).** No test verifies eviction or that the loop path (MFA:451) actually uses it.

5. **`AsyncFrameworkLoop.garage` lazy singleton (MFA:210–214).** Untested lazy `create_garage_client_from_env()`; refactor could double-create or drop the guard.

### Notes on already-"locked" but weakly-covered logic (re-pin in green)

- #6,#7,#10 are **logic mirrors**, not the real MFA expressions. After refactor, replace these with tests calling the real `_run_loop`/predicates or import the exact expression to avoid drift.
- #19 (`deadline_ms=50`) and #20 (`set_next_loop` ValueError) encode contracts the **real Mixer violates** (deadline=1000; no future-check). Decide intent before TDD: either fix Mixer to match or rewrite tests to match Mixer.

---

## (D) Test Harness Conventions (house style for new red tests)

### Async driving

- **`pyproject.toml:66` sets `asyncio_mode = "auto"`** ⇒ bare `async def test_…` runs without `@pytest.mark.asyncio` (this is how `test_loop_fixes.py` and parts of `test_concurrency_fixes.py` work). `test_async_framework.py` still sprinkles explicit `@pytest.mark.asyncio` markers (harmless under auto mode). **Convention:** either style is accepted; bare `async def` is the dominant house style.
- `asyncio.sleep` is monkeypatched to instant in the watchdog test (`test_loop_fixes.py` #32) to collapse backoff + idle waits. **Reuse this pattern** for any `_run_loop` test or it will hang on the 0.5s/0.25s sleeps.

### State reset between tests

- **`conftest.py` (root)** has an **autouse `reset_db_singleton`** fixture that sets `DatabaseManager._instance = None` before+after each test, **guarded** so a missing optional dep (sqlalchemy) degrades to a no-op (D1).
- **`test_loop_fixes.py`** defines its own **autouse `_reset_audit_state`** that saves/restores the `state` singleton's `current_show_id`, `current_show_start_time`, `llm_interaction_buffer`, `action_buffer`, `is_generating`, `is_running`, and clears the two buffers before+after. **Any new test touching the `state` singleton must follow this save/restore pattern** (or use fresh `GlobalState()` instances like `test_concurrency_fixes.py`).
- **`test_concurrency_fixes.py`** avoids singleton leakage entirely by constructing **fresh `GlobalState()` per test** — preferred for pure state-behavior tests.

### How LLM / DB / S3 are faked

- **LLM:** patch the conductor at two levels:
  - On a loop instance: `with patch.object(loop, 'conductor') as mc: mc.get_next_state_async = AsyncMock(return_value={…})` (test_async_framework), or `monkeypatch.setattr(loop.conductor, "get_next_state_async", fake_async)`.
  - On the conductor directly: `patch.object(conductor, '_get_async_client')` returning a `MagicMock` whose `chat.completions.create` is an async fn (test_simulation #42).
  - Fallback shape: `{"master_bpm","master_key","actions","reasoning","name"}` with `name=="Fallback State"` to mark fallback.
- **DB:** two strategies:
  - **Avoid it:** patch `_submit_job` with `AsyncMock` so `GeneratorJob`/`DatabaseManager` never import (test_async_framework #4,#12).
  - **Reset singleton:** `conftest.reset_db_singleton` + lazy imports inside `flush_recording_buffers`/`_submit_job` mean tests that keep buffers empty or mock the entry point never hit a real session.
- **S3/Garage:** patch `_fetch_audio` with `AsyncMock` (test_async_framework #4). The lazy `self.garage` property (MFA:210) means no client is created unless accessed.
- **Mixer:** either a tiny `_FakeMixer` stand-in (test_loop_fixes `_FakeMixer`: `sample_rate`, `current_sample`, `current_loop_end_sample`, no-op `clear/start/stop/pop_transition_event/set_next_loop/_add_track_internal`, `_ensure_stereo` passthrough) or a local `MockMixer` class per test (test_async_framework NoGap). **New `_run_loop` tests should extend `_FakeMixer`** to also stub `pop_transition_event`, `set_next_loop(tracks, next_loop_duration_samples, loop_idx)`, `_current_loop_duration`, and the `lock` context manager.

### Misc conventions

- Column-set constants are defined as module-level `set(...)` literals at the top of `test_loop_fixes.py` (`_LLM_INTERACTION_COLS`, `_SHOW_ACTION_COLS`) and asserted via `set(row.keys()) == _COLS` — **reuse for any new audit-row test.**
- `caplog` is used with `caplog.at_level(logging.WARNING, logger="app.framework.framework_state")` for once-per-handle logging (#36) — match the logger name for new FWS log assertions.
- `pytest.approx(...)` for float duration asserts (#22–24); `numpy` `dtype=np.float32` arrays for audio-shape tests.
- Conductor response factory `_conductor_response()` + `_active_stems()` helpers (test_loop_fixes) are the canonical fixtures for audit/pregen tests — reuse rather than re-rolling.
