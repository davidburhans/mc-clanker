# PLAN — Unit 12 `rel-loop-robustness` (REL-17/18/19), branch `rel-17-19-loop`

**Spec:** `refactor/plans/rel-remediation-plan.md` §Unit specs → U12 (lines 192-200) · `docs/reliability_audit.md` REL-17/REL-18/REL-19 (P2 table) · scout notes `rel-loop-robustness` (context.md, call_eacb711).
**Baseline gate at `1f08c7b` (HEAD of `main`, U11 landed):** `.venv/bin/python -m pytest tests/ -q` → **1129 passed / 16 skipped** (verified, 29.9 s) · `.venv/bin/python -m ruff check app tests` → **All checks passed**. Do not regress skips.

Verified against code at HEAD (line refs current):
`app/job_waiter.py` 330 L (`JobWaiter.wait_for_job_completion` :128-182 — pre-check `_get_job` :146-153, `pool.acquire()` :156, `add_listener` :158, post-subscribe re-check :161-164, single `wait_for(event.wait(), timeout)` :167-172, final checks :167-180 — the TimeoutError branch and the notify branch are **byte-identical bodies**; `finally: remove_listener` :181 + `release` :184; pool `command_timeout=30` :41) · `app/framework/loop_orchestrator.py` 503 L (`__init__` :99-141, `_submit_job` delegate :331-361, `_run_loop` driver :246-330 — B1 except :307-314, `run_framework_loop_async` startup-failure path :462-477 with `state.trigger_shutdown()` :476, dead `__main__` block :489-503) · `app/framework/loop_steps.py` 1053 L (`LOOP_RETRY_BACKOFF_SECONDS = 2.0` :55, `JOB_WAIT_TIMEOUT_SECONDS` :61, `JOB_PENDING_DEPTH_LIMIT` :77, `_step_call_conductor` :491-518 — swallows LLM errors into `build_fallback_response`, `_step_submit_jobs` :583-634 — `_queue_backlogged` probe **fails open** :695-703, host-contract annotations :270-282, `_step_await_pregen` P13 :981+ needs `mixer.loop_position_seconds()`) · `app/framework/pregeneration.py` 197 L (conductor call :53-65 with its own LLM-fallback) · `app/framework/framework_state.py` (`trigger_shutdown` :598-650: shutdown_event + is_running/is_generating under sync_lock + sink finalize + **None-poisons every `audio_clients` queue** + kills every `active_subprocesses` proc) · `app/app_ui.py` (lifespan shutdown `trigger_shutdown()` :121 — correct, stays; D11 done-callback `_on_framework_task_done` :182-189 → `_run_framework_failure_cleanup` :163-173 → `trigger_shutdown` — adjacent hazard, **out of named scope**, see decision 12) · `/api/health` liveness reads `state.is_running` (`app/routes/config.py:140`) · `tests/test_job_waiter.py` (3 DSN tests only — no LISTEN-path fakes exist) · `tests/test_loop_fixes.py:269-327` (canonical failure-injection harness: `_FakeMixer`, patched conductor/audit/pregen/`asyncio.sleep`) · `tests/test_round3_fix_b.py:676-694` (B6 startup-failure test asserts **only** `is_running is False` — compatible with this unit's change; `test_llm_capture.py:759` + `test_adversarial_leftovers.py:196` replace `run_framework_loop_async` wholesale — unaffected).
asyncpg 0.31.0 installed; verified from source: `Connection.remove_listener` **no-ops on a closed conn** (`if self.is_closed(): return`), `Pool.release` handles closed members.

No migration, no compose change, no new env vars, no new ports.

---

## 0. Scope summary

| Item | Root cause | Fix site |
|---|---|---|
| REL-17 (waiter holds conn 600 s) | single `wait_for(event.wait(), 600)` — a conn that died mid-wait is undetected until the full timeout; pooled corpses starve `max_size=10` | `app/job_waiter.py`: slice the wait via new `JobWaiter._wait_for_notify` (`WAITER_SLICE_SECONDS = 5.0`, `conn.is_closed()` per slice) |
| REL-18a (flat 2 s backoff) | B1 handler sleeps a constant — no escalation, no jitter, hot-loop during persistent outage | `app/framework/loop_steps.py`: `loop_retry_backoff_delay()` (base 2 s, ×2, cap 30 s, ±25 % uniform jitter) + counter on the loop |
| REL-18b (LLM call repeated during DB outage) | every cycle calls the conductor even when every submit fails | submit-failure streak on `_submit_job`; skip guard at top of `_step_call_conductor` + pregen gate; `_pending_depth()` recovery probe |
| REL-19 (startup failure = whole-app kill) | `run_framework_loop_async`'s startup-failure path calls `trigger_shutdown()` — poisons audience streams, finalizes recordings, kills the YouTube relay for a *musical* failure | `app/framework/loop_orchestrator.py:476` → `state.is_running = False` under `sync_lock` only |

Untouched: mixer, pregen scheduling, audit flush, worker, routes, lifespan shutdown ordering, `trigger_shutdown` itself, `wait_for_multiple_jobs` exception mapping, D11 done-callback.

---

## 1. Design decisions (documented reasoning; deviations flagged)

1. **REL-17 slice loop returns on dead conn; the caller's final fetch resolves the outcome.** The old code had two post-wait branches (TimeoutError vs notify) whose bodies are *literally identical* (`_get_job` → completed ? path : None). The sliced wait funnels **all three** exits — notify, deadline, dead conn — into that one final `_get_job` (on a *fresh* pool conn). Consequences: (a) a job that completed just as its LISTEN conn died is still honored; (b) no new exception path — `wait_for_multiple_jobs` keeps mapping this job to `None`, and the loop's existing 30 s grace pass + SQLAlchemy polling fallback still apply; (c) the missed-notify race coverage (pre-check :146-153 + post-subscribe re-check :161-164) is untouched — only the wait between them changes.
2. **`is_closed()` checked before every slice (loop condition), deadline clamps the last slice** (`min(WAITER_SLICE_SECONDS, remaining)`) so total wait ≤ `timeout + ε`, byte-compatible with today's single `wait_for`. A conn already closed at subscribe time skips the wait entirely. The event is never consumed by a slice timeout, so a notify landing mid-slice wakes the *current* `wait_for` exactly as before — slicing cannot delay a notify.
3. **No per-slice status re-check** (scout's optional idea — rejected): LISTEN/NOTIFY exists so completion wakes us; polling `_get_job` every 5 s per job (up to 6 jobs/batch) re-adds query load the notify path makes redundant. The slice loop's one job is conn-death detection.
4. **`remove_listener` stays unguarded; `release` stays unconditional.** Verified against installed asyncpg 0.31.0: `remove_listener` no-ops on a closed conn; the pool owns closed-member handling on release. Test fakes mirror both contracts. (Deviation from the scout's "prefer guarded" — the guard would duplicate asyncpg's own contract; one spelling.)
5. **REL-18 backoff: `min(cap, base·2^(n-1)) + U(-j·e, +j·e)`, base stays `LOOP_RETRY_BACKOFF_SECONDS = 2.0`, cap 30 s, jitter fraction 0.25** (`random.uniform`, stdlib). Failure #1 returns the base **un-jittered** — the overwhelmingly common transient blip keeps today's exact timing; escalation starts at #2 (4 s ± 1). Pure module function `loop_retry_backoff_delay(n)` in `loop_steps.py` next to the constants (unit-testable without the driver; `JOB_PENDING_DEPTH_LIMIT` naming precedent).
6. **Two counters, two semantics** — deliberately NOT one shared counter: `_consecutive_loop_errors` (any B1 iteration failure; drives the backoff) and `_consecutive_submit_failures` (submit-path only; drives the conductor skip). Sharing them would let e.g. 3 consecutive *audit* failures skip the conductor — wrong per the audit text ("after N consecutive **submit** failures").
   - `_consecutive_loop_errors`: `+= 1` in the B1 except; reset to 0 at the end of a clean try body (after `_step_await_pregen`). `RESTART_ITER` iterations (generation paused) deliberately do **not** reset it — the DB is likely still down; the cap bounds the wait either way.
   - `_consecutive_submit_failures`: `+= 1` / `= 0` inside the `AsyncFrameworkLoop._submit_job` delegate — ONE seam covering both submit paths (foreground P7 and `run_pregeneration`), the delegate that exists precisely for patchability. Any single successful submit proves the queue writable → reset. A partial batch that fails mid-way counts once (conservative under flapping; the probe recovers).
7. **Skip guard at the top of `_step_call_conductor` (fresh path P4), threshold `LOOP_CONDUCTOR_SKIP_AFTER_SUBMIT_FAILURES = 3`:** streak ≥ 3 → first run ONE recovery probe (`_probe_queue_recovered`, a sibling of `_queue_backlogged` using the existing `_pending_depth()` port — read-only, cheap, once per loop); probe OK → reset streak and fall through to the real LLM; probe fails → return `build_fallback_response(current_bpm, current_key, active_stems, "job-queue submit outage")` — the *existing* retain-all fallback shape, no new response type. `_step_call_conductor` already owns the "LLM failed → fallback" swallow, so the guard lives naturally there and the driver stays untouched.
8. **The recovery probe is required, not gold-plating:** during skip mode the fallback retains *cached* stems → zero uncached → P7 makes no DB call → nothing else can observe the DB returning. Without the probe the streak would never reset (retained stems refresh their TTL on every hit — REL-06 — so they never age out either) and the conductor would stay skipped forever after recovery. With it: conductor resumes within one loop of the DB returning. The probe only runs while streak ≥ 3 (steady state pays zero probes).
9. **Zero-submission iterations do NOT reset the submit streak** (no DB evidence either way). This is what makes the skip actually skip: with a reset-on-any-P7-success rule, the outage cycle would be conductor→fail ×3 → fallback (0 jobs, "success") → reset → conductor again — an LLM call every 4th cycle, i.e. the audit's finding at ¼ strength.
10. **Pregen gate (same finding, same fix):** `run_pregeneration` calls the conductor every cycle too (:53); it gets the same guard (streak ≥ 3 → fallback, **no probe** — the foreground P4 probe owns recovery; pregen runs at most once per loop behind it). Guard reuses the loop instance attr — one spelling.
11. **REL-19: replace `state.trigger_shutdown()` at loop_orchestrator.py:476 with `with state.sync_lock: state.is_running = False`, keep `return`.** Rationale: a *musical* failure (mixer init / thread spawn) must not poison audience streams, finalize recordings, or kill the YouTube relay — the process is fine, only the loop failed to start. `sync_lock` write matches `trigger_shutdown`'s A4-documented pattern (is_running is read by other threads); health stays truthful (`/api/health` reads `is_running`, config.py:140). `state.is_generating` deliberately untouched (user intent flag, not liveness). No re-raise: the lifespan awaits `framework_task` on shutdown. The D11 done-callback then sees a normally-returned task (exception None) and correctly does nothing — health truthfulness is preserved by the flag we just set.
12. **D11 done-callback (`app_ui.py:170`) keeps its `trigger_shutdown` — boundary pinned, not widened.** It fires only when the task itself *died with an exception* (a return-from-startup-failure now exits normally), i.e. the loop blew up past the B1 watchdog — a genuinely broken process where the full cleanup is defensible. The audit's FIX names only `loop_orchestrator.py:436-445`; changing the done-callback is out of U12's named scope. Pinned by test S2 so the boundary is explicit, and noted in §5 as a follow-up candidate.
13. **Line-budget offset:** `loop_orchestrator.py` is 503 L (already over the 500-L rule). This unit adds ~13 lines there (two counter inits, `_submit_job` bookkeeping, except-block delay, REL-19 lines) and removes the dead `if __name__ == "__main__":` scratch block (:494-503, 10 lines) → net ≈ 505. The block is dev-only dead weight; removal is the smallest honest offset.
14. **Logging: keep `print` at the touched lines** (matches every neighboring line in the driver/waiter; REL-29 is the designated print→logger unit — one spelling per concern, no drive-by).

---

## 2. Exact changes per file

### 2.1 `app/job_waiter.py` (330 → ~335 lines)

Module constant (near the top, after the logger):

```python
# REL-17 (U12): the LISTEN connection is held for the whole wait — poll the
# notify event in bounded slices so a connection that died mid-wait (PG
# restart, network partition, pool eviction) is detected within one slice
# instead of silently blocking until the full job timeout (and starving the
# pool's max_size with corpses). Module attr so tests monkeypatch it
# (JOB_WAIT_TIMEOUT_SECONDS precedent in loop_steps).
WAITER_SLICE_SECONDS = 5.0
```

In `wait_for_job_completion`, replace the wait + both final-check branches (:166-180) with the single shared final check:

```python
                # REL-17 (U12): wait in slices; a dead listener conn is caught
                # within one slice. All exits (notify / deadline / dead conn)
                # funnel into the SAME final status fetch on a fresh pool conn,
                # so a job that completed just as its connection died is still
                # honored. The pre-listen check above and the post-subscribe
                # re-check keep the missed-notify race covered (review A7/C6).
                await self._wait_for_notify(conn, event, timeout)
                job = await self._get_job(job_id)
                if job and job["status"] == "completed":
                    return job["audio_path"]
                return None
```

(`finally: remove_listener` / `finally: release` unchanged — decision 4.) New method on `JobWaiter`:

```python
    async def _wait_for_notify(self, conn, event: asyncio.Event, timeout: float) -> None:
        """Wait for the notify event in WAITER_SLICE_SECONDS slices (REL-17).

        Returns on notify, deadline, or listener-conn death — the caller's
        final _get_job fetch resolves the outcome in every case.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not event.is_set() and not conn.is_closed():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(event.wait(), min(WAITER_SLICE_SECONDS, remaining))
            except asyncio.TimeoutError:
                continue
```

### 2.2 `app/framework/loop_steps.py` (1053 → ~1085 lines)

Constants beside `LOOP_RETRY_BACKOFF_SECONDS` (:55):

```python
# REL-18 (U12): the B1 retry backoff escalates exponentially with a cap and
# uniform jitter so a persistent outage backs off instead of hot-looping (and
# synchronized instances don't stampede in lockstep). The FIRST failure still
# waits the flat base — a single transient blip keeps today's fast retry.
LOOP_RETRY_BACKOFF_MAX_SECONDS = 30.0
LOOP_RETRY_BACKOFF_JITTER_FRACTION = 0.25

# REL-18 (U12): once this many consecutive job submits have failed, the loop
# presumes a DB outage and skips the conductor call (audit finding: the full
# LLM call was repeated every cycle while every submit failed). While skipped,
# each pass probes the queue once and resumes the conductor within one loop of
# the DB returning. Module attr so tests monkeypatch it.
LOOP_CONDUCTOR_SKIP_AFTER_SUBMIT_FAILURES = 3
```

Pure function (module level, after the constants):

```python
def loop_retry_backoff_delay(consecutive_failures: int) -> float:
    """B1 watchdog sleep for the n-th consecutive failed iteration (REL-18).

    min(cap, base * 2**(n-1)) with uniform ±JITTER_FRACTION jitter; n <= 1
    returns the un-jittered base so the first (common, transient) failure
    keeps the flat 2 s retry. Example::

        loop_retry_backoff_delay(1)  # -> 2.0 exactly
    """
    if consecutive_failures <= 1:
        return LOOP_RETRY_BACKOFF_SECONDS
    exponential = min(LOOP_RETRY_BACKOFF_MAX_SECONDS, LOOP_RETRY_BACKOFF_SECONDS * 2 ** (consecutive_failures - 1))
    span = exponential * LOOP_RETRY_BACKOFF_JITTER_FRACTION
    return exponential + random.uniform(-span, span)
```

(`import random` added at the top.) Host-contract annotations (:270-282 block) gain:

```python
    # REL-18 (U12): consecutive job-submit failures (drives the conductor skip);
    # owned/mutated by AsyncFrameworkLoop.__init__/_submit_job.
    _consecutive_submit_failures: int
```

`_step_call_conductor` (:491) — guard at the top (decision 7):

```python
        # REL-18 (U12): DB presumed down — skip the LLM call (retain-all
        # fallback keeps the set running from cache) and probe for recovery so
        # the conductor resumes within one loop of the queue returning.
        if self._consecutive_submit_failures >= LOOP_CONDUCTOR_SKIP_AFTER_SUBMIT_FAILURES:
            if await self._probe_queue_recovered():
                self._consecutive_submit_failures = 0
            else:
                return build_fallback_response(current_bpm, current_key, active_stems, "job-queue submit outage")
        try:
            ...  # existing LLM call + fallback, unchanged
```

New sibling of `_queue_backlogged` (:695):

```python
    async def _probe_queue_recovered(self) -> bool:
        """REL-18: one cheap queue round-trip; True resets the submit streak.

        Runs at most once per loop and only while the conductor is being
        skipped — steady state pays nothing.
        """
        try:
            await self._pending_depth()
            return True
        except Exception:  # noqa: BLE001 - a failed probe just keeps the skip
            return False
```

### 2.3 `app/framework/loop_orchestrator.py` (503 → ~505 lines; decision 13)

- Import `loop_retry_backoff_delay` in the existing `loop_steps` import block.
- `__init__` (~:140, beside `_loop_idx`):

```python
        # REL-18 (U12): B1 consecutive-iteration failures (backoff input) and
        # consecutive job-submit failures (conductor-skip input) — two counters,
        # two semantics (a conductor-phase failure is not a submit failure).
        self._consecutive_loop_errors = 0
        self._consecutive_submit_failures = 0
```

- `_submit_job` delegate (:331) — bookkeeping around the port call:

```python
        try:
            job_id = await self._jobs.submit(...)
        except Exception:
            # REL-18 (U12): submit-failure streak — drives the conductor skip.
            self._consecutive_submit_failures += 1
            raise
        self._consecutive_submit_failures = 0  # any successful submit proves the queue writable
        return job_id
```

- `_run_loop` B1 handler (:307-314) + clean-pass reset (end of try body, after `_step_await_pregen()`):

```python
                await self._step_await_pregen()
                self._consecutive_loop_errors = 0  # REL-18: clean pass resets the backoff ladder
                ...  # existing B1 suspension comment + asyncio.sleep(0)
```

```python
            except Exception as e:
                # B1: don't let one bad iteration kill the set permanently.
                # REL-18 (U12): flat 2 s -> exponential with cap + jitter; the
                # ladder resets on the next clean pass.
                self._consecutive_loop_errors += 1
                delay = loop_retry_backoff_delay(self._consecutive_loop_errors)
                print(f"[AsyncFrameworkLoop] Loop iteration error (retry in {delay:.1f}s): {e}")
                import traceback

                traceback.print_exc()
                await asyncio.sleep(delay)
                continue
```

- `run_framework_loop_async` startup-failure path (:476) — **the REL-19 fix**:

```python
        # REL-19 (U12): a MUSICAL failure must not run the whole-app kill
        # switch — trigger_shutdown() would poison audience streams, finalize
        # recordings and kill the YouTube relay; the process is not dying, only
        # the music loop failed to start. Flip the health flag only and keep
        # serving. The exception is NOT re-raised: the lifespan awaits
        # framework_task on shutdown.
        with state.sync_lock:
            state.is_running = False
        return
```

- Delete the dead `if __name__ == "__main__":` block (:489-503).

### 2.4 `app/framework/pregeneration.py` (197 → ~203 lines)

Import `LOOP_CONDUCTOR_SKIP_AFTER_SUBMIT_FAILURES` (existing `loop_steps` import). Wrap the conductor call (:52-65):

```python
        if loop._consecutive_submit_failures >= LOOP_CONDUCTOR_SKIP_AFTER_SUBMIT_FAILURES:
            # REL-18 (U12): same skip as the foreground P4 path — the DB is
            # presumed down, so the background LLM call is skipped too (the
            # foreground probe owns recovery; no probe here).
            conductor_response = build_fallback_response(current_bpm, current_key, active_stems, "job-queue submit outage")
        else:
            try:
                conductor_response = await loop.conductor.get_next_state_async(...)
            except Exception as e:  # noqa: BLE001
                print(f"[AsyncFrameworkLoop] Pre-gen LLM call failed: {e}")
                conductor_response = build_fallback_response(current_bpm, current_key, active_stems, e)
```

### 2.5 NEW `tests/test_job_waiter_slicing.py` (~230 lines) — §3.1

### 2.6 NEW `tests/test_loop_robustness.py` (~300 lines) — §3.2

### 2.7 Docs (docs stage, same unit)

- `docs/reliability_audit.md`: REL-17 / REL-18 / REL-19 rows → `**Status: fixed-in rel-17-19-loop**` + one-line mechanism each (sliced notify wait + is_closed; exponential cap+jitter backoff + conductor skip with recovery probe; is_running-only startup failure).
- `refactor/plans/rel-remediation-plan.md`: unit-queue row 12 → landed.
- `CLAUDE.md`: Testing table — add `test_job_waiter_slicing.py` (REL-17 waiter slice/dead-conn) and `test_loop_robustness.py` (REL-18 backoff/conductor-skip, REL-19 startup failure).

---

## 3. TDD regression tests (write first, confirm red, then implement)

### 3.1 `tests/test_job_waiter_slicing.py` — REL-17

Fakes (named classes, no inline stubs): `_FakeListenerConn` (`is_closed`/flippable `close_after_s`, stores the listener callback, `add_listener`/`remove_listener` no-op when closed — mirroring asyncpg 0.31), `_FakeStatusConn` (scripted `fetchrow` responses popped per call), `_AcquireRouter` (bare-`await` → listener conn, `async with` → status conn — matches the two acquire shapes inside `JobWaiter`), `_FakeJobWaiterPool` (`acquire()`/`release()` recording).

| # | Test | Pins | Core assertions |
|---|---|---|---|
| W1 | `test_waiter_returns_promptly_when_listener_conn_dies` (**acceptance**) | REL-17 | slice 0.05 s, timeout 3.0 s, conn closes at ~0.12 s → returns `None` in < 1.0 s (vs. full 3.0 s unsliced); elapsed ≥ close time; final status fetch happened |
| W2 | `test_waiter_resolves_on_notify_across_slice_boundaries` (**acceptance**) | REL-17 | slice 0.01 s, timeout 1.0 s; fake notify fired at 0.03 s (scheduled via `loop.call_later` invoking the stored callback with `payload=str(job_id)`); final row `completed` → returns `audio_path`; ≥ 2 slices actually elapsed (is_closed call count) |
| W3 | `test_waiter_slices_honor_full_timeout_and_final_status_check` | timeout semantics unchanged | no notify, conn open, timeout 0.1 s, slice 0.02 s → `None`; elapsed within [0.09, 0.6]; status conn saw exactly 3 fetchrows (pre-check, re-check, final) |
| W4 | `test_waiter_post_subscribe_recheck_catches_missed_notify` | A7/C6 preserved | 2nd fetchrow returns `completed` → returns path immediately, elapsed < slice; listener added before re-check, removed after |
| W5 | `test_waiter_pre_check_failed_job_short_circuits` | pre-check preserved | 1st fetchrow `failed` → `None`; listener conn never acquired, no listener registered |
| W6 | `test_waiter_closed_conn_at_subscribe_does_not_wait` | decision 2 | listener conn born closed, timeout 2.0 s → `None` in < 0.5 s; `remove_listener` tolerated (no-op when closed); `release` still called exactly once |
| W7 | `test_waiter_immediate_notify_not_delayed_by_slicing` | common case | notify fires before the first slice expires → path returned; elapsed < slice |
| W8 | `test_waiter_slice_seconds_default_is_five` | spec constant | `WAITER_SLICE_SECONDS == 5.0` |

### 3.2 `tests/test_loop_robustness.py` — REL-18 / REL-19

Fakes: `_SleepRecorder` (patched `asyncio.sleep`, records nonzero delays — pattern from `test_loop_fixes.py:315`), `_OutageJobQueuePort` (`JobQueuePort` fake: `submit` raises / then succeeds via `outage` flag; `pending_depth` follows the same flag; `await_jobs`/`abandon_jobs` inert), `_CountingConductor` (call-counting `get_next_state_async` returning one `{"action_type": "add", ...}` action so every loop has an uncached stem), `_FakeMixerWithPosition` (the `test_loop_fixes` `_FakeMixer` + `loop_position_seconds() -> 0.0` so P13 exits fast on committed iterations), `_FakeTrackedProc` (`kill()` records). State hygiene per test: `state.is_running/is_generating = True`, `shutdown_event.clear()`, explicit teardown (pattern `test_loop_fixes.py:278`).

| # | Test | Pins | Core assertions |
|---|---|---|---|
| B1 | `test_loop_retry_backoff_delay_sequence_and_cap` | REL-18 pure fn | n=1 → exactly 2.0; n=2..4 within [e·0.75, e·1.25] for e ∈ {4, 8, 16}; n=5, n=9 within [22.5, 37.5] (capped); n=0 → 2.0 |
| B2 | `test_backoff_sequence_asserted_and_resets_on_success` (**acceptance**) | REL-18 driver | audit fault ×3 → success → fault → success+stop; recorded backoff sleeps == [2.0], [3,5], [6,10], then [2.0] again (reset pinned); loop returns, no raise (harness: `test_run_loop_retries_after_transient_exception` + recorder) |
| B3 | `test_conductor_skipped_after_three_consecutive_submit_failures` (**acceptance**) | REL-18 skip | outage port; 3 submit-failing iterations call the LLM fake (count 3); then ≥ 3 fallback iterations with the probe still failing → LLM count stays exactly 3; fallback iterations commit (retain-all); audit fake stops the loop |
| B4 | `test_conductor_resumes_after_queue_recovery` (**acceptance**) | REL-18 recovery | same harness; probe flipped healthy at fallback iteration 2 → next iteration calls the LLM fake (count 4); `submit_calls == 4` (3 failed + 1 recovered) |
| B5 | `test_submit_delegate_tracks_failure_streak` | decision 6 | direct: `loop._submit_job` raises (fake port) → `_consecutive_submit_failures == 1`; next call succeeds → `0` |
| B6 | `test_pregeneration_skips_conductor_during_submit_outage` | decision 10 | `loop._consecutive_submit_failures = 3` preset; `run_pregeneration(loop, 2, snapshot)` → conductor fake NOT called, `_pregen_results` carries the fallback shape (`name == "Fallback State"`, retain-all actions for snapshot stems); control with streak 0 → conductor called |
| S1 | `test_startup_failure_sets_is_running_false_without_kill_switch` (**acceptance**) | REL-19 | `AsyncFrameworkLoop.start` patched to raise; `run_framework_loop_async` returns; `state.is_running is False`, `shutdown_event` **clear**, audio-client queue **not** poisoned, `state.youtube_relay` sentinel untouched, tracked proc not killed & still registered; then `state.trigger_shutdown()` still poisons the queue (mechanism intact) |
| S2 | `test_framework_task_done_callback_still_runs_full_cleanup` | decision 12 boundary | a task that dies WITH an exception → `_on_framework_task_done` → clients poisoned + `is_running False` (D11 unchanged — only the startup-failure path softened) |
| S3 | `test_b6_startup_failure_still_green` | no-regression guard | re-run shape of `test_round3_fix_b.py::test_b6_mixer_startup_failure_is_handled_and_clears_is_running` expectations against the new path (existing test also stays untouched and green) |

Red-first: W1/W2/W6 (dead conn undetected today → full timeout), B1/B2 (flat 2 s today), B3/B4 (no skip today), B6 (pregen calls conductor today), S1 (trigger_shutdown runs today → queue poisoned, shutdown_event set) all fail against HEAD; W3/W4/W5/W7/W8, B5, S2, S3 pin unchanged behavior (expected green immediately — guards against regressing the preserved seams).

---

## 4. Verification & rollout

```bash
# per concern, red -> green
.venv/bin/python -m pytest tests/test_job_waiter_slicing.py -v
.venv/bin/python -m pytest tests/test_loop_robustness.py -v
# no-regression gates
.venv/bin/python -m pytest tests/test_job_waiter.py tests/test_loop_fixes.py tests/test_round3_fix_b.py tests/test_async_framework.py -v
.venv/bin/python -m pytest tests/ -q          # expect 1129 + 17 new = 1146 passed / 16 skipped
.venv/bin/python -m ruff check app tests
```

Branch `rel-17-19-loop` off `main`; commit series (each red→green): ① REL-17 waiter slices + W* ② REL-18 backoff + B1/B2 ③ REL-18 skip + B3-B6 ④ REL-19 + S1-S3 ⑤ docs. Review gate: this plan's §1 decisions vs. the diff; check `grep -n "trigger_shutdown" app/framework/loop_orchestrator.py` returns nothing (the only production callers left are `app_ui.py:121` lifespan + `:170` failure-cleanup).

---

## 5. Risks / residual

- **Half-open connections without FIN** (network partition, no TCP reset): `is_closed()` stays False locally — undetected until the OS notices. Full fix would need asyncpg's connection-loss callback or TCP keepalives; the audit offered either remedy, U12 names the is_closed check. Residual documented in the audit row.
- **`_queue_backlogged` fails open** (loop_steps :695): its own DB errors never reach the submit streak — only real `_submit_job` raises count. Deliberate (a broken gauge must not stop the set) but means an outage detected *only* via the depth probe escalates nothing; the first real submit still fails within the same iteration.
- **P8 await/abandon DB failures do not increment the submit streak** (submit-first ordering reaches P7 before P8 every fresh iteration, so an outage is counted there first). If a flapping DB fails only awaits, the conductor keeps running — acceptable, the audit's waste case is submit failure.
- **RESTART_ITER iterations do not reset `_consecutive_loop_errors`** (decision 6) — a paused-then-resumed outage continues the ladder; capped at 30 s.
- **`loop_orchestrator.py` lands ≈ 505 L** — marginally over the 500-L rule (pre-existing 503; offset by the `__main__` removal). Follow-up extraction candidates noted for a future hygiene unit; not this unit's scope.
- **Wall-clock test margins**: W1/W3/B2 use real `asyncio.wait_for` timing with generous bounds (CI slack); slices are monkeypatched small so the assertions never approach their bounds.
- **D11 done-callback still calls `trigger_shutdown`** (decision 12) — same hazard class, out of named scope; flagged here as a follow-up candidate with its pinning test S2 documenting the boundary.

## 6. Acceptance mapping (U12 spec → tests)

- "waiter detects dead conn within one slice" → W1 (+ W6, W8).
- "backoff sequence asserted" → B1, B2 (+ reset-on-success pinned in B2).
- "startup failure leaves app + relay alive" → S1 (+ boundary S2, mechanism-intact tail inside S1).
- "missed-notify coverage not weakened" → W3, W4, W5, W7 (pre-check / post-subscribe re-check / final-status all stay).
- "skip conductor after N submit failures … recovers when DB returns" → B3, B4 (+ B5 streak bookkeeping, B6 pregen gate).
- invariant 6 (every fix lands with a regression test): all three RELs pinned above; invariant 1 (locks): the only new state write is `is_running` under `sync_lock` (REL-19); invariant 2 (hexagonal/fakes): conductor/jobs fakes ctor-injected via the existing ports, waiter tested through a named fake pool.
