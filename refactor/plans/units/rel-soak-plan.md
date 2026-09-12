# PLAN — Unit 15 `rel-soak-harness` (audit §Soak-test spec, points 1–8), branch `rel-soak`

**Spec:** `refactor/plans/rel-remediation-plan.md` §Unit specs → U15 (lines 225-234) · `docs/reliability_audit.md` §Soak-test spec (lines 530-560, the 8 points) · scout notes `rel-soak` (context.md, call_a3821996da1).
**Baseline gate at `db915d7` (HEAD of `main`, U14 landed):** `.venv/bin/python -m pytest tests/ -q` → **1178 passed / 17 skipped** (verified, 31.6 s). No production code changes in this unit — test harness + one pytest-config line + one dev dep only. Normal-suite impact must stay: 1178 passed, skip count grows by the new module's test count (all reported `SKIPPED` with the opt-in reason).

Verified against code at HEAD (line refs current):
`pyproject.toml` (`[tool.pytest.ini_options]` holds ONLY `asyncio_mode = "auto"` + `testpaths = ["tests"]` — **no `markers` key exists**; dev group has no `psutil`) · `psutil` **not installed** (ModuleNotFoundError verified) · `tests/` has **no `__init__.py`** (sibling test modules import as top-level names under pytest's prepend import mode) · root `conftest.py` + `tests/conftest.py` (autouse `reset_db_singleton`; shared `isolated_export_db` fixture explicitly reserved "U15's soak module is expected to reuse this") · `tests/test_loop_robustness.py` (`_CountingConductor` :52, `_OutageJobQueuePort` :97, `_ScriptedAudit`, `_FakeMixerWithPosition`, `_make_robust_loop` :231, `_drive_loop` :340, `sleep_recorder` :306, `_isolated_state` autouse :283) · `app/framework/loop_orchestrator.py` (`AsyncFrameworkLoop.__init__(session_id, *, conductor, mixer_factory, audio, jobs, audit)` :99-141 — full port injection) · `app/framework/loop_steps.py` (`AUDIT_FLUSH_THRESHOLD_ROWS = 200` :154, `JOB_PENDING_DEPTH_LIMIT = 64` :121, P12 threshold flush `if len(state.llm_interaction_buffer) > AUDIT_FLUSH_THRESHOLD_ROWS: await self._audit.flush()` :1087-1088 — the **real** seam reads the **real** state buffer; the flush runs through the injected port) · `app/framework/audit_recording.py` (`append_loop_audit` no-ops without `state.current_show_id` :288-291; `flush_recording_buffers` copies-under-lock → clears → insert → **re-prepends on failure** :52-98, invariant 4) · `app/framework/framework_mixer.py` (`Mixer(sample_rate=44100, blocksize=2048, channels=2)` ctor takes no sounddevice; REL-01 guard lives in `_stream_loop` :414-436 — `except Exception: log.exception; outdata.fill(0)`, cadence-preserving; `start()` spawns the `Mixer` thread and registers `state.mixer_thread` :397-410) · `tests/test_mixer_extended.py` (thread lifecycle tests drive `mixer.start()`/`time.sleep`/`mixer.stop()` directly :514-580) · `tests/test_reset_reprime.py` (real-mixer end-to-end reset regression `_audio()` :72, `_loop_with(mixer)` :77, `_STATE_ATTRS` snapshot fixture :32) · `tests/test_worker_vram.py` (`_install_fake_gpu_modules` :64 — **plain function, no fixture-bound globals, importable**; fake `torch.cuda.memory_allocated/memory_reserved` are zero-returning lambdas :73-74; module-scoped `_gpu_stack` :112 restores `sys.modules` exactly; `_make_worker(exit_hook=...)` :184; `TestRel03TimeoutCircuitBreaker` :279) · `tests/test_stream_fanout.py` (`FakeProc/FakeStdout/FakeStderr/PopenRecorder` :72-211 — plain classes; `fake_popen` :215 + `fake_ffmpeg_exe` :232 fixtures; `reset_fanout_state` autouse :241; `make_cfg/wait_until/queue_items/fanout_threads_alive/force_stale_eviction/wait_for_teardown` :267-322 — all plain importable helpers; `TestStreamRoute` direct-ASGI drive :828 — TestClient buffers infinite streams and hangs; stateful `receive` MUST park on a disconnect Event; `test_k_abrupt_kills_leave_no_zombies` :610) · `app/stream_fanout_args.py` (`FanoutStatus` fields: active/client_count/restarts/dropped_pcm_blocks/dropped_client_blocks/evicted_clients/bytes_fanned_out/last_error :36-48) · `tests/test_storage_retention.py` (`app_client`/`init_db`/`reset_state`/`db_user` fixtures :52-104, `patch_owner` :106, `_make_show` :111, `_set_show_audio_path` :123, `RoutingFakeConnection` :138, `_pool` :172, `_cleanup` :179) · `app/cleanup.py` (`_run_cleanup` drives isolated passes; `_run_pass` :198 — **a failing pass logs and yields 0, never wedges the cycle**; `_sweep_expired_recordings` :352, `_delete_expired_audit_rows` :356, `_delete_garage_objects` :313) · `tests/conftest.py` (`isolated_export_db` :145 — real SQLite engine per test + seeded owner User + Bearer headers + `IsolatedExportDb.make_show/insert_interactions/insert_actions`) · `app/routes/reasoning_logs.py` (`GET /api/reasoning-logs/export` :146 — chunked keyset via `app/lib/export_chunks.py`, streams NDJSON) · `app/routes/shows.py` (`GET /api/shows/{id}/export/llm-dump` :739 — the second chunked NDJSON export; `delete_show` :325 calls `drop_buffered_rows_for_show` :352).

No migration, no compose change, no production-file edits.

---

## 0. Scope summary

U15 = the audit's **acceptance gate** ("the soak harness (spec above) is the acceptance test for the whole pass"). Every REL fix from units 1–14 already landed with its own regression test; this unit adds the *soak-scale* harness: long schedules, plateaus, churn counts — assertions phrased exactly per the audit wording.

| Audit point | Test family | Primary template | Runtime profile |
|---|---|---|---|
| 1. 24h-equivalent fault-injection soak | `test_soak_247.py` | `test_loop_robustness.py` | fast ≈ 8 s / full ≤ 60 s |
| 2. Mixer fault survival | `test_soak_247.py` | `test_mixer_extended.py` + `_audio()` | ≈ 1 s / 4 s |
| 3. Reset-then-restart regression | `test_soak_247.py` | `test_reset_reprime.py` | ≈ 1 s |
| 4. VRAM plateau (300 generations) | `test_soak_worker.py` | `test_worker_vram.py::_gpu_stack` | ≈ 5 s (both) |
| 5. Timeout circuit-breaker contract | `test_soak_worker.py` | `TestRel03TimeoutCircuitBreaker` | ≈ 2 s |
| 6. Disconnect churn (K client kills) | `test_soak_stream.py` | `TestStreamRoute` + `test_k_abrupt_kills…` | ≈ 4 s / 15 s |
| 7. Storage reconciliation | `test_soak_storage_export.py` | `test_storage_retention.py` | ≈ 6 s / 10 s |
| 8. Real-session export | `test_soak_storage_export.py` | `isolated_export_db` (rel-13) | ≈ 8 s / 25 s |
| **Total** | | | **fast ≈ 35 s / full ≤ 120 s** |

**Expected result at HEAD: GREEN.** Units 1–14 are landed; every audit assertion should pass. A RED soak assertion is a *finding to report* (a residual or a regression), never something to loosen — the harness IS the gate.

---

## 1. Design decisions (documented reasoning; deviations flagged)

1. **Four test modules + one shared helper, not one file.** The remediation plan names `tests/test_soak_247.py` (singular). Eight points with point-local fakes ≈ 900-1000 lines — over AGENTS.md's 500-line rule and mixing four unrelated fake families in one module. **Deviation from the spec's literal filename, flagged:** the spec's name is kept for the primary module (P1–P3, the loop/mixer family); the worker/stream/storage families get sibling modules; shared gate/clock/param logic lives in a non-collected `tests/soak_helpers.py`. The U15 acceptance ("`pytest -m soak` runs the suite against fakes; documented how to run it") does not depend on a single filename. All five modules carry the identical gate via `pytestmark = soak_gate()` (one spelling, in the helper).
2. **Gate = registered `soak` marker + module-level env skipif; NO `addopts`.** `pyproject.toml` gains `markers = ["soak: …"]` (silences `PytestUnknownMarkWarning`; makes `-m soak` selectable) but NOT `addopts = "-m 'not soak'"`. Reason (scout gotcha, agreed): addopts silently changes default CLI behavior for every existing invocation (`-m` flag precedence rules, coverage jobs, IDE runners); the module-level `pytestmark = [pytest.mark.soak, pytest.mark.skipif(os.environ.get("SOAK") != "1", reason=…)]` alone guarantees "SKIPPED in normal runs" with zero blast radius. Belt+braces: marker enables selection, skipif is the actual default-off mechanism. Normal runs report the new tests as `SKIPPED (opt-in soak: SOAK=1 pytest -m soak)` — visible, collectible, cheap.
3. **Two run profiles behind one gate.** `SOAK=1` (gate) selects the **fast** profile by default — every point runs at CI-friendly scale (~35 s total, nightly-job friendly). `SOAK=1 SOAK_PROFILE=full` selects the audit's literal numbers (24 h-equivalent schedule, K=12+ churn, N=100 export loops) under the same < 2 min budget. Rationale: the audit's point 1 is literally "24 h-equivalent" while points 2–5 are seconds-scale; a single profile would either under-run P1 in CI or blow the budget in full mode. `soak_profile() -> str` reads the env once in `soak_helpers` (one spelling); every long dimension is a helper-returned table row, never a bare literal in a test body.
4. **Virtual clock by sleep-accumulation, no scale factor.** The soak patches `asyncio.sleep` (the `sleep_recorder` pattern, `test_loop_robustness.py:306`) with a clock that adds each requested `delay` to `clock.elapsed` and then yields once via the **captured original** `asyncio.sleep(0)`. "24 h-equivalent" = `clock.elapsed ≥ 86400` — every backoff rung (2→4→…→30 s cap, rel-18), pregen wait and job-wait timeout accumulates at face value in virtual time, so the ladder semantics are exercised without wall-clock cost. Real-time elements are exactly two, both intentional: (a) the driver's `asyncio.wait_for(..., timeout=SOAK_WALL_BUDGET)` watchdog (the last line of defense against a hang — a wedge is itself a soak failure), and (b) nothing else — the conductor/jobs/audit/mixer are all injected fakes with no internal `wait_for` (verified: the loop's awaits all delegate to port calls). `time.time()`-based TTLs (stem cache) barely advance under virtual time — irrelevant here; REL-06 behavior is pinned by `test_reset_reprime.py`.
5. **P1 exercises the REAL P12 flush seam via a flushing fake.** The audit asserts "`len(llm_interaction_buffer)` bounded". `_step_post_commit` (loop_steps.py:1087) reads the **real** `state.llm_interaction_buffer` and calls the **injected** `self._audit.flush()`. So the soak's `_FlushingAuditPort` mirrors the real adapter contract exactly: `append_loop` appends 1 interaction + N action dicts into the real state buffers (with `state.current_show_id` set so the shape matches the real append's precondition), `flush` copies-under-lock → clears → "inserts" into an in-memory list, and during scripted PG windows raises and **re-prepends** (invariant 4: retain-over-drop — `flush_recording_buffers` semantics, audit_recording.py:88-97). This pins the real threshold trigger with a fake DB — the hexagonal pattern (invariant 2), no production seam bypassed.
6. **`psutil` added to the dev dependency group.** RSS assertions are in the audit wording ("RSS plateaus") and psutil is not installed today. Add `psutil>=5.9.0` to `[dependency-groups].dev` (pure-python, tiny). RSS checks still use `psutil = pytest.importorskip("psutil")` **inside the RSS-specific test functions only** (scout gotcha, agreed: never module-level — the other 7 points must run even if the dep is missing). With the dep in the group, CI runs them; a bare venv degrades to a skip, not a failure. RSS plateau bound: final ≤ first-sample × 1.10 (±5 % is inside GC/allocator noise for Python; 10 % headroom is the honest "plateau" — documented in the assertion message).
7. **P4/P5 sibling-import the plain fakes; fixture-bound globals stay local.** `_install_fake_gpu_modules` and `_FAKE_MODULE_NAMES` in `test_worker_vram.py` are plain module-level functions (no fixture binding) — `from test_worker_vram import _install_fake_gpu_modules, _FAKE_MODULE_NAMES` works under pytest's default prepend import mode (tests/ is inserted at collection; module names are top-level). `_engine`/`_make_worker` there reference module globals bound by *that module's* fixture, so the soak defines its own ~50-line local factories (`tests/` is not a package; fixtures cannot be imported across sibling modules). Pre-implementation check (see §9): confirm the sibling import resolves under `SOAK=1 pytest -m soak tests/test_soak_worker.py`; if an import-mode change ever breaks it, fall back to a trimmed local installer in `soak_helpers` (torch + safetensors + stable_audio_tools + huggingface_hub only) with a cross-reference comment. Same rule for P6: `FakeProc`, `PopenRecorder`, `wait_until`, `fanout_threads_alive`, `force_stale_eviction`, `wait_for_teardown`, `make_cfg` import from `test_stream_fanout` (all plain); the `fake_popen`/`fake_ffmpeg_exe` fixtures and the `_isolate_fanout` autouse are redefined locally (fixture bodies are test-local glue — the repo already repeats `_STATE_ATTRS` isolation across `test_loop_robustness`/`test_reset_reprime`; one-spelling-per-concern applies to production paths).
8. **P6 churn drives the ROUTE, not the singleton API.** `test_k_abrupt_kills_leave_no_zombies` already pins K abandon-evict cycles at the fanout level; the audit point is "`K` abrupt **/stream.mp3 client kills**" — so the soak drives the real ASGI app with the `TestStreamRoute` scope/receive/send pattern (direct ASGI is mandatory: TestClient buffers infinite streams and hangs — documented at `test_stream_fanout.py:834`). Each cycle: create the app task → wait for the first body chunk → **`task.cancel()`** (the abrupt kill; the response generator's close path releases the client, pinned by `test_generator_gc_close_releases`) → assert teardown. The stateful `receive` (one `http.request`, then park on a disconnect Event) is copied verbatim — Starlette 1.0 raises `Unexpected message received` otherwise.
9. **P7 mixes a real SQLite DB (routes) with the fake asyncpg conn (retention passes)** — exactly the `test_storage_retention.py` split: `DELETE /api/shows/{id}` and row seeding go through the real app + real DB; `JobExpirationCleanup` passes run over `RoutingFakeConnection`/`_pool` fakes whose canned rows reference the seeded files. DB-outage windows are RNG-seeded (`random.Random(20260915)` — fixed seed, reproducible) intervals during which the fake conn's `fetch`/`execute` raise. `_run_pass` (cleanup.py:198) provably isolates pass failures (logs + yields 0), so an outage-overlap pass cannot wedge the cycle — the soak asserts reconciliation AFTER recovery, i.e. eventual consistency, which is the property the audit asks for.
10. **P8 uses the real capture path end-to-end.** Not seed-only (scout note, agreed): set `state.current_show_id` + `current_show_start_time`, then the REAL `append_loop_audit` → REAL `AuditAdapter.flush` against the `isolated_export_db` SQLite engine; then GET both chunked export endpoints (`/api/reasoning-logs/export` and `/api/shows/{id}/export/llm-dump`) via TestClient (finite streams — TestClient is safe here, unlike P6). "Both export endpoints" = the two chunked-keyset NDJSON streams (rel-13's two shapes: reasoning export and llm-dump); `/reasoning-timeline` + `/reasoning-logs/stats` get one smoke GET each since they share the aggregate path. Delete-live-show-then-flush uses TWO shows: buffered rows for the deleted show are dropped (`drop_buffered_rows_for_show`, wired at `shows.py:352`), the surviving show's rows still flush — pins rel-14's amended contract.
11. **Point 3 (reset) is a repetition of the landed regression, not a copy-paste.** `test_reset_then_restart_reprimes_boundary_and_transition_fires` pins one reset cycle. The soak runs TWO full prime→transition→reset→recommit→transition cycles against the real `Mixer` — the "soak" dimension is repetition (does the re-prime survive a second reset?). Keep it small (≈ 30 lines over the template).
12. **No production edits.** If any soak assertion fails at HEAD, the harness lands with the failure documented in the status row (report-first, per the remediation plan's review discipline); a fix would be its own unit or a follow-up commit on this branch — never a loosened assertion.

---

## 2. Module layout & exact contents

```
tests/soak_helpers.py               (new, ~140 L — NOT collected: no test_ prefix)
tests/test_soak_247.py              (new, ~430 L — P1 fault-injection + P2 mixer + P3 reset)
tests/test_soak_worker.py           (new, ~230 L — P4 VRAM plateau + P5 circuit-breaker)
tests/test_soak_stream.py           (new, ~180 L — P6 disconnect churn)
tests/test_soak_storage_export.py   (new, ~330 L — P7 reconciliation + P8 real-session export)
docs/soak_harness.md                (new, ~60 L — how to run)
pyproject.toml                      (+ markers registration, + psutil in dev group)
```

### 2.1 `tests/soak_helpers.py` — gate, profiles, clock, state isolation

```python
SOAK_ENV = "SOAK"
SOAK_PROFILE_ENV = "SOAK_PROFILE"

def soak_enabled() -> bool: ...          # os.environ.get(SOAK_ENV) == "1"
def soak_profile() -> str: ...           # "full" if SOAK_PROFILE=full else "fast"
def soak_gate() -> list:                 # [pytest.mark.soak, pytest.mark.skipif(not soak_enabled(), reason="opt-in soak: SOAK=1 pytest -m soak")]
    ...

class VirtualClock:
    """Sleep-accumulating clock: every asyncio.sleep(delay) advances `elapsed`
    by `delay` virtual seconds and yields once via the ORIGINAL asyncio.sleep(0).
    24h-equivalent == elapsed >= 86_400. Used with monkeypatched asyncio.sleep."""

class SoakParams:                          # per-profile dimensions, one table:
    # point            fast            full
    p1_virtual_seconds   600             86_400
    p1_wall_budget       15.0            75.0
    p1_loops_cap         40              700
    p2_ticks             120             600
    p2_fail_every        12              50 (+ a 5-tick burst in full)
    p3_cycles            2               2
    p4_generations       300             300      # audit's literal number, cheap on fakes
    p5_timeout_s         0.05            0.05
    p6_clients           3               12 (+3 concurrent)
    p7_windows           2               5
    p8_loops             20              100
def soak_params() -> SoakParams: ...     # soak_profile() -> dataclass row
```

Plus `_copy_state_attr` + `_STATE_ATTRS` snapshot/restore fixture factory `isolated_soak_state()` (union of `test_loop_robustness._STATE_ATTRS` with `should_reset`, `current_show_id`, `current_show_start_time`, `llm_interaction_buffer`, `action_buffer`, `mixer_thread`, `stream_fanout` — buffers/lists copied, restored after each test). AGENTS.md style: functions 4–20 lines, explicit types, no `Any` in new signatures beyond what the port protocols demand.

### 2.2 `tests/test_soak_247.py` — P1, P2, P3

Header docstring: the audit §Soak-test spec points 1–3 verbatim mapping + how-to-run pointer to `docs/soak_harness.md`. `pytestmark = soak_gate()`.

**Shared P1 fakes (point-local, ~150 L):**

- `_SoakConductor` — `_CountingConductor` shape + clock reference: raises `RuntimeError("llm unavailable")` while the clock sits in an LLM-outage window (the P4 fallback path must absorb it); stops the loop (`loop.running = False; state.is_running = False`) once `clock.elapsed >= params.p1_virtual_seconds`.
- `_SoakJobQueue` — `_OutageJobQueuePort` extended with a `_FaultSchedule`:
  ```python
  # fast profile (600 virtual s)          # full profile (86 400 virtual s = 24 h)
  LLM_OUTAGE    (120, 180)                (7_200, 10_800)   # 02:00-03:00 — audit's named 1 h window
  PG_RESTART_1  (240, 250)                (18_000, 18_600)  # 05:00-05:10 — submit/depth/abandon raise
  WORKER_DOWN   (300, 360)                (28_800, 34_200)  # 08:00-09:30 — submit OK, await returns pending
  STUCK_JOB_AT  420                       39_600            # 11:00 — one job's await runs the full virtual timeout
  PG_RESTART_2  —                         (50_400, 50_700)  # 14:00-14:05 — second short restart (full only)
  ```
  Behaviors: `submit` → raises during PG windows, else returns a uuid and adds it to `self.pending`; `await_jobs(ids, timeout)` → during WORKER_DOWN sleeps the timeout virtually and returns `{id: None}` (loop's abandon path fires); the one job scheduled at `STUCK_JOB_AT` never completes even after the window (its await always burns the timeout); `abandon_jobs` → raises during PG windows (the audit's "PG restart overlapping the abandon" corner), else clears the ids from `pending`; `pending_depth()` → `len(self.pending)` (raises during PG windows) — the loop's P7 throttle reads it.
- `_FlushingAuditPort` — decision 5: appends into `state.llm_interaction_buffer`/`state.action_buffer` (requires `state.current_show_id` set by the test), `flush()` clears into `self.persisted` or, during PG windows, raises and re-prepends. Mirrors `flush_recording_buffers` exactly.

**P1 test — `test_p1_fault_injection_soak_24h_equivalent` (async, `sleep_recorder`-style clock patch):**

1. `state.current_show_id = 1`; build `AsyncFrameworkLoop` via the `_make_robust_loop` pattern with the three fakes + `_FakeMixerWithPosition` (mixer boundary pre-set to 1 — the `_drive_loop` note about REL-02 re-prime stealing audit appends applies).
2. Start `task = asyncio.create_task(loop._run_loop())` and a monitor coroutine that, on every real 1 ms tick, records `(clock.elapsed, task.done(), state.loop_count, len(asyncio.all_tasks()) - baseline, len(state.llm_interaction_buffer), len(state.llm_interaction_buffer)+len(state.action_buffer), len(jobs.pending), rss)` — rss via `psutil.Process()` only if importable (decision 6; `None` samples otherwise).
3. `await asyncio.wait_for(task, timeout=params.p1_wall_budget)` — the loop self-stops at virtual 24 h.
4. Assertions, per the audit wording exactly:
   - **loop task alive**: every sample before the last has `task.done() is False` (i.e. the task only ended by the scheduled stop); if the watchdog fired instead → `pytest.fail("soak loop wedged at virtual t=…")` with the last sample attached.
   - **`loop_count` monotonic**: the sampled sequence is non-decreasing and the final `loop_count > 0` (≥ 15 fast / ≥ 40 full).
   - **buffer bounded**: `max(sampled len(llm_interaction_buffer)) <= AUDIT_FLUSH_THRESHOLD_ROWS + 200` (threshold 200 + one outage window of re-prepended rows — envelope math in a comment: ≤ ~20 fallback loops × ~7 rows = 140 during the 10-min PG window); after each PG window closes, the next samples drain back `<= AUDIT_FLUSH_THRESHOLD_ROWS`; `_FlushingAuditPort.persisted` row count == total appended − dropped-on-nothing (flush never silently drops — invariant 4; the fake asserts internally).
   - **task count flat**: `max(sampled len(asyncio.all_tasks())) - min(...) <= 4` (the loop's own task + monitor + transient).
   - **pending bounded**: `max(sampled len(jobs.pending)) <= 16` (a per-loop batch-scale bound — far under `JOB_PENDING_DEPTH_LIMIT = 64`; the abandon path clears worker-down batches, the stuck job contributes exactly 1).
   - **RSS plateaus** (only if psutil imported): `final_rss <= first_rss * 1.10`.
   - the conductor skip engaged during PG windows (rel-18): `_SoakConductor.calls` plateaus while submits fail — sampled indirectly via audit responses named `"Fallback State"` during the window.

**P2 test — `test_p2_mixer_fault_survival` (sync, real thread):**

1. `mixer = Mixer(sample_rate=44_100, blocksize=256, channels=1)` (≈ 5.8 ms/tick — 120 ticks ≈ 0.7 s).
2. `mixer.prime_loop([(_audio(4.0, channels=1), 0)], duration_samples=4*44_100)`; patch `state.broadcast_audio` to record `(timestamp, bytes)`; wrap `mixer._callback` so every `params.p2_fail_every`-th call raises `RuntimeError("tick exploded")` BEFORE delegating (the guard is in `_stream_loop`, which zeroes `outdata` and keeps cadence — REL-01).
3. `mixer.start()`; poll until the wrapper counted `params.p2_ticks` calls (wall cap 5 s); snapshot `alive = mixer._stream_thread.is_alive()`; `mixer.stop()`.
4. Assertions: `alive is True` (thread survived every injected failure); `state.mixer_thread is mixer._stream_thread` and was alive (the `/api/health` observability seam); broadcasts resumed after each failure — the longest run of consecutive failing ticks (no broadcast between successful ones) is `<= params.p2_fail_every`; the final broadcast is non-silent (all-ones audio → non-zero bytes). Silence-duration bound: a failing tick broadcasts nothing (the exception precedes the broadcast call), so "silence ≤ k ticks" is exactly the max-gap assertion above.

**P3 test — `test_p3_reset_then_restart_two_cycles` (async):** the `test_reset_reprime` end-to-end shape, run twice: prime → queue+consume a transition (`pop_transition_event()` fires) → `state.should_reset = True` → `_step_read_state` consumes it → `_step_commit_to_mixer` into the boundary-less mixer → assert `mixer.current_loop_end_sample > 0` and a second queued loop transitions again. Repeat the whole block; assertions identical on cycle 2 (the "soak" dimension: re-prime survives repetition — decision 11).

### 2.3 `tests/test_soak_worker.py` — P4, P5

Module-scoped `_soak_gpu_stack` autouse fixture: `from test_worker_vram import _install_fake_gpu_modules, _FAKE_MODULE_NAMES`; same managed-module/restore dance as `_gpu_stack` (decision 7). After install, **replace the fake cuda lambdas with a mutable `_VramCounters`** (`allocated: int`, `reserved: int`, `reset()`) — the audit's plateau reads become meaningful: the local `_SoakEngine` fake's `generate_batch` side-effect bumps `allocated += 1_000`, `reserved += 1_600` then returns `(audio, 44100)`; `unload` zeroes them. Local `_make_worker`/`_make_conn`/`_pool_yielding` copies (~50 L; the originals are fixture-bound globals — decision 7).

**P4 — `test_p4_vram_and_thread_plateau_after_300_generations`:** registry with a default engine + one extra engine; baseline snapshot `counters.reset(); threads_before = threading.active_count()`. Loop `params.p4_generations` (300 — the audit's literal number, both profiles) times: `registry.generate_batch([{"prompt": f"pad {i}", …, "model_id": …}], 128)`; every 30th cycle load/re-load the extra model and run one eviction pass with the monitor reporting critical (the `_eviction_unloads_lru_non_default_under_pressure` pattern) so the models dict returns to the steady set. Assertions: `counters.allocated == 0 and counters.reserved == 0` (±5 % of baseline 0 = exactly 0 — every allocation paired with an unload/empty-cache); `len(registry.models)` == steady set (LRU evicted the extra; no unbounded retention — REL-23); `threading.active_count() <= threads_before + 2` (no thread accumulation across 300 cycles — the abandoned-thread class REL-03 exists to break lives in P5, not here).

**P5 — `test_p5_consecutive_timeout_breaker_contract_under_mixed_schedule`:** `_make_worker(exit_hook=exit_calls.append)`; `GENERATION_TIMEOUT_SECONDS = 0.05` (monkeypatched module attr — fake-fast, never 600 s); sequence: timeout → **success** (complete pipeline: garage `put_object` AsyncMock + `encode_aac`/`get_audio_duration` monkeypatched — the `test_counter_resets_on_successful_pipeline` recipe) → timeout → timeout. Assertions: the breaker did NOT trip on the first pair (counter reset by the completed pipeline), `exit_calls == [1]` exactly once on the second consecutive timeout, `worker.get_stats()["consecutive_generation_timeouts"] == 2` (the health breadcrumb), and both timed-out rows routed through `_process_claimed_job`'s failure path (`jobs_failed == 2`, error message contains "exceeded"). Simulated restart: construct a fresh worker (Docker `restart=unless-stopped` is the designed recovery — invariant 5) and assert its counter starts at 0.

### 2.4 `tests/test_soak_stream.py` — P6

Imports from `test_stream_fanout` (decision 7) + local `fake_popen`/`fake_ffmpeg_exe` fixtures and an autouse `_isolate_fanout` (the `reset_fanout_state` body). One helper `_drive_stream_client_once(recorder) -> None` implementing the `TestStreamRoute` direct-ASGI drive: raw scope for `GET /stream.mp3`, stateful `receive` (one `http.request` then park on a disconnect Event), `send` collecting messages; wait `len(recorder) == 1`; `recorder[0].stdout.push(b"SOAK-CHUNK")`; await the first body chunk; then **`task.cancel()`** (abrupt kill — no `trigger_shutdown`, no clean release; the generator-close path releases the client); await suppression of CancelledError; set the disconnect Event.

**`test_p6_disconnect_churn_zero_zombies`:** for `k in range(params.p6_clients)`: drive once; then `wait_until(state.audio_clients == [])` and `wait_until(fanout_threads_alive() == 0)` (the last release tears the singleton down — assert `state.stream_fanout is None` between cycles). Full profile appends one **concurrent trio** (three app tasks sharing the singleton, all cancelled simultaneously) before the sequential loop. Assertions per the audit: `all(proc.poll() is not None for proc in fake_popen.created)` (zero zombie ffmpeg) and `all(proc.stdin.closed …)`; `len(state.audio_clients) == 0` at rest (== live clients mid-drive: assert `fanout.status().client_count == expected` inside the concurrent phase); queue count == live clients (`client_count` from `FanoutStatus`); `fanout_threads_alive() == 0` at rest; RSS flat across the churn if psutil present (first vs last sample, × 1.10); the singleton's `dropped_pcm_blocks == 0` (churn never stalled the pump).

### 2.5 `tests/test_soak_storage_export.py` — P7, P8

**P7 — `test_p7_storage_reconciliation_under_db_outages`** (fixtures: `isolated_export_db`-style real SQLite via monkeypatched `DATABASE_URL` + TestClient(app) + local `db_user`; decision 9):

1. Seed `S = 6` real Shows (statuses ended/live mix) via `IsolatedExportDb.make_show`-style inserts; for each, write a real file under `tmp_path/shows/{id}/rec.wav` (sized 1–4 KB) and register a fake-garage object `audio/{uuid}.aac` in a `_FakeGarage` dict; `_set_show_audio_path` links the row.
2. `rng = random.Random(20260915)`; pick `params.p7_windows` outage windows (start, duration ≤ 2 s real) over the drive.
3. `_OutageRoutingConnection(RoutingFakeConnection)` — canned `fetch` rows for the retention passes built from the seeded shows with staggered `created_at` ages (some past the 7-day retention, some inside); `fetch`/`execute` raise `RuntimeError("pg restarting")` while `monotonic()` is inside a window.
4. Drive: `await cleanup._run_cleanup()` (passes internally isolated — verified `_run_pass` :198); then `DELETE /api/shows/{id}` for two shows — one attempt lands inside a window (route returns 5xx; assert the row + file + object SURVIVE — the outage must not half-delete); after the window, retry → 204. Re-run `_run_cleanup()` clean.
5. Assertions per the audit: **zero unreferenced Garage objects** — every key in `_FakeGarage` is referenced by a surviving show row's `audio_file_path` (deleted shows' objects were removed by the successful delete/cleanup path); **zero orphaned recording/export files** — every file under `tmp_path/shows` maps to a surviving row; **on-disk bytes bounded by retention config** — `sum(file sizes) == expected_live_bytes` computed from the seed ages + `show_audio_retention_days=7` (equality, stronger than ≤); the DB holds exactly the surviving shows.

**P8 — `test_p8_real_session_capture_and_export_roundtrip`** (`isolated_export_db` fixture, real `AuditAdapter` — decision 10):

1. Shows A and B (`sandbox.make_show` × 2). Set `state.current_show_id = A`, `state.current_show_start_time = time.time()` (restored by the state fixture).
2. `for i in range(params.p8_loops): await append_loop_audit(response_i, stems_i, i)` with realistic payloads (1 add + 2 retain actions each, alternating fallback flags); `await AuditAdapter().flush()`; assert both state buffers empty and a direct SQL count == N interactions + 3N actions.
3. Switch `current_show_id = B`; append `params.p8_loops` more (buffered, unflushed); `DELETE /api/shows/{A}` with the sandbox headers → `drop_buffered_rows_for_show(A)` runs (assert the response/side effect: A's buffered rows dropped — count them); `await AuditAdapter().flush()` **succeeds** with B's rows intact (rel-14 amended contract: flush-after-delete works, nothing of B's silently lost).
4. GET `/api/reasoning-logs/export?show_id={B}` and `/api/shows/{B}/export/llm-dump` via TestClient (finite streams). Assertions: both bodies are complete NDJSON — line counts == N exactly (no truncation — consumers detect truncation by row count, per rel-13's export contract); every line `json.loads`-parses; one round-trip field spot-check (`parsed_response` of interaction 0 equals the captured dict — lossless, invariant 4); smoke GET `/api/reasoning-logs/stats?show_id={B}` and `/api/reasoning-timeline?show_id={B}` → 200.

### 2.6 `pyproject.toml`

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
    "soak: 24h-equivalent fault-injection soak (opt-in: SOAK=1 pytest -m soak; SOAK_PROFILE=full for the audit-literal schedule)",
]
```

and `"psutil>=5.9.0"` appended to `[dependency-groups].dev` (comment: `# RSS plateau assertions in the rel-soak harness (U15)`). **No `addopts`** (decision 2).

### 2.7 `docs/soak_harness.md`

Short: what the harness gates (the 8 audit points → module/test table), the two run profiles, the exact commands (below), the psutil note, the skip-count note (normal runs grow skips by the module's test count), and "a RED soak assertion is a finding, not a test bug" policy.

---

## 3. How to run (the acceptance doc)

```bash
# Normal suite — the soak modules are collected but SKIPPED (zero impact beyond skip count)
.venv/bin/python -m pytest tests/ -q          # 1178 passed, 17+~14 skipped

# Soak suite, fast profile (CI/nightly, ~35 s total)
SOAK=1 .venv/bin/python -m pytest -m soak -q

# Soak suite, full 24h-equivalent profile (audit-literal, ≤ 2 min total)
SOAK=1 SOAK_PROFILE=full .venv/bin/python -m pytest -m soak -q

# One point at a time
SOAK=1 .venv/bin/python -m pytest -m soak tests/test_soak_247.py -q        # P1-P3
SOAK=1 .venv/bin/python -m pytest -m soak tests/test_soak_worker.py -q     # P4-P5
SOAK=1 .venv/bin/python -m pytest -m soak tests/test_soak_stream.py -q     # P6
SOAK=1 .venv/bin/python -m pytest -m soak tests/test_soak_storage_export.py -q  # P7-P8

# psutil (RSS assertions) lives in the dev group:
uv pip install --group dev
```

Without `SOAK=1`, `pytest -m soak` still selects the tests but every one reports `SKIPPED: opt-in soak: SOAK=1 pytest -m soak` (the reason string doubles as the run instructions).

---

## 4. Runtime budget (hard < 2 min for the full profile)

| Point | fast | full | Guard |
|---|---|---|---|
| P1 | ~8 s | ≤ 60 s | `p1_wall_budget` watchdog (15 s / 75 s) fails the test on wall-clock overrun — a wedge is a finding |
| P2 | ~0.7 s | ~3.5 s | 5 s wall cap in the tick-wait loop |
| P3 | ~1 s | ~1 s | awaits only, no sleeps |
| P4 | ~5 s | ~5 s | 300 fake cycles ≈ µs each |
| P5 | ~2 s | ~2 s | two 0.05 s timeouts + mocks |
| P6 | ~4 s | ~15 s | `wait_until` timeouts (5–8 s each) |
| P7 | ~6 s | ~10 s | outage windows ≤ 2 s real each |
| P8 | ~8 s | ~25 s | N loops of pure awaits + finite streams |
| **Total** | **~35 s** | **~120 s** | |

---

## 5. Acceptance mapping (audit wording → assertion)

| Audit phrase (§Soak-test spec) | Test | Assertion |
|---|---|---|
| P1 "loop task alive" | `test_p1_…` | no sample `task.done()` before the scheduled stop; watchdog not hit |
| P1 "`loop_count` monotonic" | `test_p1_…` | sampled sequence non-decreasing, final > 0 |
| P1 "`len(llm_interaction_buffer)` bounded" | `test_p1_…` | peak ≤ `AUDIT_FLUSH_THRESHOLD_ROWS + 200`; drains ≤ threshold post-window |
| P1 "`asyncio.all_tasks()` count flat" | `test_p1_…` | max−min ≤ 4 over samples |
| P1 "`generator_jobs` pending count bounded" | `test_p1_…` | peak ≤ 16 (≪ `JOB_PENDING_DEPTH_LIMIT` 64) |
| P1 "RSS plateaus" | `test_p1_…` (+P6) | psutil-guarded final ≤ first × 1.10 |
| P1 faults: LLM outage / PG restart / worker-down / stuck generation | `test_p1_…` | the five-window `_FaultSchedule` (fast: 4 windows) |
| P2 "thread survives / health observably degrades" | `test_p2_…` | `_stream_thread.is_alive()` after every k-th failure + `state.mixer_thread` seam |
| P2 "silence duration bounded" | `test_p2_…` | max consecutive no-broadcast ticks ≤ k |
| P3 "`current_loop_end_sample > 0` + transition fires" | `test_p3_…` | both, after each of 2 reset cycles |
| P4 "300 generations; memory + threads → baseline ±5 %" | `test_p4_…` | fake counters == 0 exactly; threads ≤ baseline+2 |
| P5 "two consecutive timeouts → non-zero exit" | `test_p5_…` | `exit_calls == [1]`, exactly once, not on timeout→success→timeout |
| P6 "zero zombie ffmpeg, `len(audio_clients)` == live, RSS flat" | `test_p6_…` | all procs reaped + stdin closed; client_count matches; RSS × 1.10 |
| P7 "zero unreferenced Garage objects, zero orphan files, bytes ≤ retention" | `test_p7_…` | set equalities on garage/files; bytes == live-bytes (≤ retention) |
| P8 "complete NDJSON; delete-live-show → next flush succeeds" | `test_p8_…` | line counts == N both endpoints; B's rows flush after A's delete |

---

## 6. Validation plan (this unit's own gate)

1. `.venv/bin/python -m pytest tests/ -q` → **1178 passed**, skips = 17 + new test count, 0 failed, no new warnings (marker registered).
2. `SOAK=1 .venv/bin/python -m pytest -m soak -q` → all soak tests pass, ≤ 45 s.
3. `SOAK=1 SOAK_PROFILE=full .venv/bin/python -m pytest -m soak -q` → all pass, ≤ 120 s (timed, recorded in the land note).
4. `.venv/bin/python -m pytest -m soak -q` (no SOAK env) → all selected tests SKIPPED with the opt-in reason.
5. `.venv/bin/python -m ruff check tests/soak_helpers.py tests/test_soak_247.py tests/test_soak_worker.py tests/test_soak_stream.py tests/test_soak_storage_export.py` → clean.
6. Repeat run 2 three times — no flakiness (RNG-seeded P7, jittered backoffs under the virtual clock must be deterministic in pass/fail).

## 7. Risks / pre-implementation checks (do these first while writing)

- **Sibling import (decision 7):** confirm `from test_worker_vram import _install_fake_gpu_modules` and `from test_stream_fanout import FakeProc, …` resolve when running `SOAK=1 pytest -m soak tests/test_soak_worker.py` standalone AND in the full-suite order. Fallback documented in decision 7.
- **P1 audit↔conductor 1:1 mapping:** the `_drive_loop` comment (REL-02 boundary) applies — pre-set `mixer.current_loop_end_sample = 1` or the re-prime fabricates results that steal audit appends.
- **P1 `asyncio.sleep` patch scope:** patch via `monkeypatch.setattr(asyncio, "sleep", clock_sleep)` where `clock_sleep` calls the captured original for the yield — NEVER `asyncio.sleep(0)` recursively through the patch.
- **P2 tick pacing:** `blocksize=256` at 44.1 kHz ≈ 5.8 ms/tick; verify no per-tick `broadcast_audio` consumer (state.audio_clients empty — the isolation fixture clears it) so ticks stay cheap.
- **P6 cancellation hygiene:** after `task.cancel()`, also set the disconnect Event and `await` the task with `suppress(CancelledError)` — a pending receive coroutine otherwise leaks the sample (the TestStreamRoute finally-block pattern, minus trigger_shutdown).
- **P7 route-vs-cleanup DB split:** the DELETE route reads the real SQLite; `_OutageRoutingConnection` windows must therefore gate ONLY the cleanup pass (the fake conn), never the app's engine — the real DB "outage" for the route leg is simulated by choosing window timing so one DELETE attempt lands while the fake conn is down is impossible for the route; instead simulate the route-level outage by pointing one DELETE at a monkeypatched `DatabaseManager.session` that raises once (the existing `test_storage_retention` mock patterns) — decide while implementing; the invariant under test (no half-delete) is unchanged.
- **P8 delete-show auth:** `delete_show` uses `require_show_owner` → the sandbox Bearer headers must resolve against the isolated DB (they do — `isolated_export_db` seeds the user and `reset_db_singleton` is order-independent per its docstring).
- **Skip-count optics:** normal-run skip line grows by ~14 (one per soak test); note in `docs/soak_harness.md` so nobody "fixes" it.

## 8. Files changed (this unit)

| File | Δ |
|---|---|
| `pyproject.toml` | + markers block (3 lines), + psutil dev dep |
| `tests/soak_helpers.py` | new ~140 L |
| `tests/test_soak_247.py` | new ~430 L |
| `tests/test_soak_worker.py` | new ~230 L |
| `tests/test_soak_stream.py` | new ~180 L |
| `tests/test_soak_storage_export.py` | new ~330 L |
| `docs/soak_harness.md` | new ~60 L |

No production files. `refactor/plans/rel-remediation-plan.md` status row updated at land time (per repo convention).
