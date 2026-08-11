# Contract & Blast Radius: `app/framework/framework_main_async.py`

Target file: `app/framework/framework_main_async.py` (~1191 LOC).
Goal of this map: enumerate every EXTERNAL coupling so the upcoming split
cannot silently break a caller, test, or patch target.

---

## (A) Public API freeze-list

Every name below is imported or invoked from outside the file's own body
(`grep`-verified, repo-wide). The split MUST keep each available at
`app.framework.framework_main_async` (re-export is acceptable) with an
identical signature, OR migrate every caller in the same change.

| # | Symbol (signature) | Defined at | External callers (file:line — how) |
| --- | -------------------- | ----------- | ------------------------------------- |
| A1 | `class AsyncFrameworkLoop(session_id: uuid.UUID)` | `framework_main_async.py:182`, `__init__` at `:190` | Production: none instantiate directly (only via `run_framework_loop_async`). Tests: `tests/test_async_framework.py:53,68,85,143,361,421`; `tests/test_loop_fixes.py:153,178,190,267`; `tests/test_worker_fetch_audio.py:29,65` |
| A2 | `AsyncFrameworkLoop.start() -> Awaitable[None]` | `:216` | External: **none** (only `run_framework_loop_async:1163` internally). Contracted by tests indirectly. |
| A3 | `AsyncFrameworkLoop.stop() -> Awaitable[None]` | `:232` | External: **none** (internal only). |
| A4 | `AsyncFrameworkLoop.garage` (`@property`) | `:210` | External: **none**. Internal-only lazy init at `:864`. |
| A5 | `async def run_framework_loop_async(session_id: uuid.UUID) -> None` | `:1155` | **🔴 PRODUCTION HOT PATH** `app/app_ui.py:14` (import), `:90` (`asyncio.create_task(run_framework_loop_async(app_session_id))` in FastAPI lifespan). Re-imported in `:1187` self-test. Tested: `tests/test_async_framework.py:34,37,42` |
| A6 | `async def flush_recording_buffers() -> None` | `:74` | **🔴 PRODUCTION** `app/routes/shows.py:298` (lazy import inside endpoint), `:299` (`await flush_recording_buffers()`). Tested: `tests/test_loop_fixes.py:21,209` |
| A7 | `def process_actions(actions: List[Dict[str,Any]], active_stems: List[Dict]) -> List[Dict]` | `:118` | **🟠 SIMULATION** `simulation/session_state.py:13` (import), `:129` (`process_actions(actions, state.active_stems)`). |
| A8 | `def calc_duration(bpm: int, bars: int, time_signature: int = 4) -> float` | `:47` | Tested: `tests/test_loop_fixes.py:20,83,88,92` |
| A9 | `async def main() -> None` (module-local) | `:1179`, guarded by `if __name__ == "__main__"` at `:1177` | External: **none**. CLI-only entry; safe to relocate. |

**Names NOT in the public freeze-list** (only internal/`self.` callers;
safe to rename/move with no external coordination):
`_to_two_channel` (function), `_flush_lock` (module var), `AsyncFrameworkLoop._finish_loop`,
`AsyncFrameworkLoop._run_loop`, `AsyncFrameworkLoop._build_prompt`,
`AsyncFrameworkLoop._submit_job`, `AsyncFrameworkLoop._fetch_audio`,
`AsyncFrameworkLoop._append_loop_audit`, `AsyncFrameworkLoop._pre_generate_next_loop`,
`AsyncFrameworkLoop._relative_show_ms`, `AsyncFrameworkLoop._audit_*` (4 helpers).
⚠️ BUT several of these ARE reached by tests (see section D) — those constrain renames
even though they are not "public API."

---

## (B) `state.*` read/write matrix

Cross-referenced against `app/framework/framework_state.py` `__init__` (lines 78–192)
and `reset()` (215–241). The framework loop touches the global `state` singleton
~95 times. Grouped by access mode (R = read, W = write/mutate, M = method call that mutates):

### Read-only (loop never writes)

| Attribute | state.py def | Read sites in framework_main_async.py |
| ----------- | ------------- | ---------------------------------------- |
| `state.is_running` | `:110` | `269, 273, 297` |
| `state.shutdown_event` (threading.Event) | `:172` | `273, 277, 723` |
| `state.user_override` | `:96` | `365, 680` |
| `state.available_instruments` | `:93` | `366, 681` |
| `state.llm_base_url` | `:101` | `370, 684` |
| `state.llm_api_key` | `:102` | `371, 685` |
| `state.llm_model` | `:103` | `372, 686` |
| `state.current_show_id` | `:154` | `84, 892` |
| `state.current_show_start_time` | `:155` | `916` |

### Read + Written

| Attribute | state.py def | Read sites | Write/mutate sites |
| ----------- | ------------- | ----------- | -------------------- |
| `state.is_generating` | `:105` | `273, 282, 290, 296, 334, 338` | `1184` (`= True`) |
| `state.current_bpm` | `:82` | `319, 362, 459, 464, 474, 677` | `356, 443, 445, 636, 669` |
| `state.current_key` | `:83` | `320, 363, 459, 465, 475, 678` | `359, 448, 450, 637, 672` |
| `state.current_set_name` | `:88` | `664, 733` | `452, 638` |
| `state.llm_reasoning` | `:95` | `665, 734` | `453, 639` |
| `state.last_actions` | `:117` | — | `438, 658` |
| `state.target_bpm_override` | `:97` | `352, 668` | `357, 670` |
| `state.target_key_override` | `:98` | `353, 671` | `360, 673` |
| `state.active_stems` | `:85` | `321, 364, 617, 647, 654, 663, 679, 718, 732` | `624, 626` |
| `state.next_stems` | `:86` | `473` | `456, 460, 628` |
| `state.previous_stems` | `:84` | `646, 653` | `618` |
| `state.stem_history` | `:87` | `367, 682` | `619, 621` (`.append`/`.pop(0)`) |
| `state.llm_interaction_buffer` | `:157` | `82, 90` | `85, 92, 114, 896` (`.clear`/`.append`) |
| `state.action_buffer` | `:158` | `82, 91` | `86, 93, 115, 910` (`.clear`/`.append`) |
| `state.loop_count` | `:116` | — | `632` (`+= 1`) |
| `state.should_reset` | `:99` | `344` | `348` |
| `state.muted_stems` / `state.soloed_stems` / `state.stem_volumes` | `:114,115,113` | — | `629, 630, 631` (`.clear()`) |

### Method calls (mutating) on the singleton

| Method | state.py def | Call sites |
| -------- | ------------- | ----------- |
| `state.cache_stem(prompt, audio_data)` | `:202` | `527` |
| `state.record_loop_transition(idx, stems, set, reason)` | `:248` | `692, 735` |

### Concurrency primitives (acquired, not data)

| Primitive | state.py def | Acquire sites |
|-----------|-------------|--------------|
| `state.lock` (asyncio.Lock) | `:78` | `81, 113, 281, 289, 295, 318, 333, 343, 421, 441, 526, 616, 731, 891` (14 sites) |

⚠️ **Cross-lock hazard** (adversarial review A3): `record_loop_transition` takes the
blocking `state.sync_lock` (threading.Lock) and is called from inside async code at
`:692, :735`. Any split must preserve the existing "snapshot under `state.lock`,
then call `record_loop_transition` OUTSIDE the lock" pattern (see comment at `:729`).

---

## (C) Outbound dependency graph

```
framework_main_async.py
├── app.framework.framework_state  → state (singleton)   [TIGHT — global mutable, ~95 sites, dual-lock contract]
├── app.framework.framework_mixer  → Mixer               [TIGHT — reaches 11 private attrs, see below]
├── app.framework.framework_conductor_async → ConductorLLMAsync  [port-ready: single call surface get_next_state_async]
├── app.job_waiter → wait_for_multiple_jobs               [I/O adapter — port candidate]
├── app.garage_client → create_garage_client_from_env     [I/O adapter — port candidate; ⚠️ patch-target in tests]
└── app.aac_encoder → decode_aac                          [I/O adapter — port candidate; ⚠️ patch-target in tests]
```

### Mixer coupling (deepest internal reach — biggest port candidate)

`start()` constructs `Mixer(sample_rate=44100, channels=2)` at `:225` (via `run_in_executor`).
`_run_loop` then reaches these attributes/methods:

| Member | Sites | Visibility |
| -------- | ------- | ----------- |
| `mixer.lock` | `586, 602, 744` | private (sync lock) |
| `mixer._add_track_internal(...)` | `589` | **private** |
| `mixer._ensure_stereo(audio)` | `590` | **private** |
| `mixer._current_loop_duration = ...` | `593` | **private** |
| `mixer.current_loop_end_sample` (r/w) | `592, 594, 603` | public-ish |
| `mixer.current_sample` | `314, 587, 746, 747` | public-ish |
| `mixer.sample_rate` | `535, 747` | public |
| `mixer.set_next_loop(...)` | `599` | public |
| `mixer.pop_transition_event()` | `726` | public |
| `mixer.clear()` | `346` | public |
| `mixer.start()` / `mixer.stop()` | `226, 247, 255` | public |

→ 5 private Mixer members are touched. Pushing Mixer behind a port requires first
promoting `_add_track_internal`, `_ensure_stereo`, `_current_loop_duration`, and the
`lock` acquisition protocol to a documented interface.

### Conductor coupling (already a clean port)

`__init__` constructs `ConductorLLMAsync()` at `:199`. Only one method is called:
`conductor.get_next_state_async(...)` at `:397` and `:1020`. This is the model port
boundary.

### I/O adapters (port candidates, hexagonal violation E5)

- `wait_for_multiple_jobs(job_ids, timeout=120.0)` at `:512, :1088` (job_waiter).
- `create_garage_client_from_env()` at `:213` (lazy `garage` property); `.get_object(audio_path)` at `:864`.
- `decode_aac(aac_bytes, sample_rate=44100)` at `:872` (via `run_in_executor`).

---

## (D) Private-symbol usage by tests (rename constraints)

These symbols are prefixed `_` but ARE referenced by name in tests. Renaming or
moving them without migrating the tests will break the suite. Listed most→least
constraining.

| Symbol | Test file:line | Usage | Constraint |
| -------- | ---------------- | ------- | ------------ |
| `AsyncFrameworkLoop._pre_generate_next_loop(for_loop_idx, snapshot)` | `tests/test_async_framework.py:128,175,404,453`; `tests/test_loop_fixes.py:300` | Direct `await` call **and** `monkeypatch.setattr(loop, "_pre_generate_next_loop", ...)` | Cannot rename without updating 5 call sites + 1 monkeypatch |
| `AsyncFrameworkLoop._submit_job(...)` | `tests/test_async_framework.py:122,398` | `patch.object(loop, '_submit_job', new_callable=AsyncMock)` | Patch by name; rename breaks patch |
| `AsyncFrameworkLoop._fetch_audio(audio_path)` | `tests/test_async_framework.py:123` (`patch.object`); `tests/test_worker_fetch_audio.py:46,77` (direct `await`) | Patch + direct call | Rename breaks both patch and 2 direct calls |
| `AsyncFrameworkLoop._append_loop_audit(resp, stems, loop_idx)` | `tests/test_loop_fixes.py:157,184,193`; `:299` (monkeypatch) | Direct call + monkeypatch | Rename breaks 3 calls + 1 monkeypatch |
| `AsyncFrameworkLoop._pregen_task` / `_pregen_done` / `_pregen_loop_idx` / `_pregen_results` (instance attrs) | `tests/test_async_framework.py:59,60,61,71,75,131-134,178,180,407-411,458,459`; `tests/test_loop_fixes.py:315,316` | Direct read/set + `.set()/.clear()/.is_set()` | Attribute shape contract; renaming attrs breaks ~15 assertions |
| `_flush_lock` (module-level `asyncio.Lock`) | `tests/test_loop_fixes.py:22,204,216` | Imported, `.acquire()`, `.release()` | If module split moves `_flush_lock`, import at `:22` breaks AND the lock identity the test holds must match the one `flush_recording_buffers` uses |
| `_to_two_channel(audio)` | `tests/test_loop_fixes.py:23,100,106,113` | Imported + called | Rename breaks 4 sites |

### ⚠️ String-based patch targets (silent-failure risk — highest priority)

`tests/test_worker_fetch_audio.py:43,44,74,75` patch by **module-path string**:

```python
patch("app.framework.framework_main_async.create_garage_client_from_env", ...)
patch("app.framework.framework_main_async.decode_aac", ...)
```

If the refactor moves these imports into a sub-module (e.g. a new `audio_io.py`),
these patches will silently target a no-longer-existing attribute on
`framework_main_async` and the patched mocks will NOT be applied — tests may
pass vacuously or fail with confusing errors. The module path
`app.framework.framework_main_async` and the attribute names
`create_garage_client_from_env` + `decode_aac` MUST remain bound there, or these
patch strings MUST be migrated in lockstep.

Note: `tests/test_worker_fetch_audio.py::test_decode_aac_*` are currently
unconditionally `pytest.skip` (adversarial review D12), so breakage here is
dormant — but the import/patch lines still execute at collection time.

### Tests that DO NOT import the module (per adversarial review D4)

`tests/test_async_framework.py` classes `TestPreGenResultsUsage`,
`TestNextLoopPreGeneration`, `TestMixerNextLoopIntegration`, `TestNoGapPrevention`,
`TestLastActions` re-implement production logic inline (mirror `_run_loop` step 5,
etc.) and assert against local copies. They will NOT detect a regression in the
real file. They are not part of the constraint set for renames, but they are a
coverage gap to flag.

---

## (E) Call-site blast radius — ranked by fragility

1. 🔴 **`app/app_ui.py:90`** — `asyncio.create_task(run_framework_loop_async(app_session_id))`
   in the FastAPI lifespan. The single entry point for all music playback.
   Signature MUST stay `run_framework_loop_async(session_id: uuid.UUID) -> Awaitable[None]`.
   No graceful fallback; a broken import here kills the app at startup.
2. 🔴 **`app/routes/shows.py:298-299`** — lazy import of `flush_recording_buffers`
   inside an endpoint handler. Breaks only the show-end recording flush endpoint
   at runtime (not startup). Signature: zero-arg async.
3. 🟠 **`simulation/session_state.py:13,129`** — `process_actions(actions, active_stems)`.
   Off-the-request-path but a hard import at module load; rename breaks
   simulation import entirely.
4. 🟠 **`tests/test_worker_fetch_audio.py:43,44,74,75`** — string-path patches of
   `create_garage_client_from_env` / `decode_aac` on the module. Highest *silent*
   breakage risk: a split that relocates these imports makes the patches no-ops
   without raising an ImportError.
5. 🟡 **`AsyncFrameworkLoop` private surface** — `_pre_generate_next_loop`,
   `_submit_job`, `_fetch_audio`, `_append_loop_audit`, and the `_pregen_*`
   instance attributes are reached by name across 2 test files (~15 sites).
   Renaming any of these requires coordinated test edits.
6. 🟡 **`_flush_lock` + `_to_two_channel`** — module-level privates imported by
   `tests/test_loop_fixes.py`. If split, the test's imported `_flush_lock` must be
   the SAME object the relocated `flush_recording_buffers` acquires, or the
   serialization-guard test becomes a false positive.
7. ⚪ **`main()` (`:1179`)** — CLI-only, `__main__`-guarded, no external callers.
   Safe to relocate freely.

### Re-export contract for the split

After splitting, `app/framework/framework_main_async.py` MUST still export (or
re-export via `from ._new_module import X`):
`AsyncFrameworkLoop`, `run_framework_loop_async`, `flush_recording_buffers`,
`process_actions`, `calc_duration`, `_to_two_channel`, `_flush_lock`,
`create_garage_client_from_env`, `decode_aac` (last two to keep string-patch
targets valid). Anything else may move.
