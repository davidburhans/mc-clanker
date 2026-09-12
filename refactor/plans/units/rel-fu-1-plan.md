# PLAN — Unit FU-1 `rel-fu-health`, branch `rel-fu-1-health`

**Spec:** `refactor/plans/rel-remediation-plan.md` §Follow-ups round 2 (FU-1) + the review
follow-up notes for rel-01 / rel-04 / rel-11 / rel-12(rel-17 residual) ·
`docs/reliability_audit.md` REL-01, REL-04, REL-11, REL-17 entries.
**Baseline gate (verified on this tree):** `ruff check app tests` clean,
`pytest tests/ -q` → **1178 passed / 26 skipped**. Do not regress.

---

## 0. Scope summary

| # | Item (source) | Root cause today | Fix site |
|---|---|---|---|
| 1 | Mixer consecutive-failure counter + health + log rate-limit (rel-01 follow-up) | `_stream_loop`'s REL-01 guard logs `log.exception` on **every** failing tick — bounded only by tick cadence (~21.7 lines/s under persistent failure); no failure counter exists anywhere; `/api/health` has `mixer_alive` but no degradation signal | `framework_mixer.py` `_stream_loop` + `start`, `framework_state.py` counter slot, `routes/config.py` health |
| 2 | Audit backlog + failed-flush health counters (rel-04 follow-up) | During a sustained DB outage the audit buffers grow without bound (retain-over-drop is the invariant-4-correct choice) and repeated flush failures are invisible — only a `print` in `audit_recording.flush_recording_buffers`'s except branch | `audit_recording.py` module counter + `routes/config.py` health |
| 3 | `trigger_shutdown` subprocess kill outside `sync_lock` (rel-11 follow-up) | The subprocess sweep runs `p.kill(); p.wait(timeout=1)` **while holding `state.sync_lock`** — up to 1 s × N procs of I/O-under-lock stalling every ~46 ms audio tick (and every other `sync_lock` holder) during shutdown | `framework_state.py::trigger_shutdown` |
| 4 | `stems.py` clip-site sanitize (rel-01/21 follow-up) | `routes/stems.py:23` `_encode_wav_response` does `np.clip(...) * 32767` — `np.clip` preserves NaN, so a NaN-poisoned cached stem downloads as platform-defined int16 garbage (the exact REL-21 hazard, third clip site) | `routes/stems.py` + a public alias in `framework_mixer.py` |
| 5 | psycopg2 TCP keepalives (rel-17 follow-up) | The PG engine bounds connect/statement time but a **half-open** pooled conn (NAT/firewall drop, no FIN) stays "healthy" until the OS-level TCP timeout (often 2 h+); `pool_pre_ping` only detects conns that error, not ones that silently black-hole | `db.py` PG `connect_args` |

All changes are additive observability/robustness; no behavior change on any
happy path. Invariant 4 (capture losslessness) is untouched — item 2 only
*observes* the buffers, never drops a row.

---

## 1. Design decisions

### D1 — Mixer failure counters live on **`state` as a `sync_lock`-guarded dict** (`state.mixer_tick_failures`), following the `recording_write_errors` precedent

Recon finding that forces this choice: **`tests/test_state_slices.py:52`
(`test_levels_view_named_not_mixer_to_avoid_clash`) pins
`not hasattr(state, "mixer")`** — the E3 slice naming deliberately reserved
`mixer` (it "clashes with `framework_mixer.Mixer`"). So the tempting design
(register the Mixer instance as `state.mixer`, counters as instance attrs,
mirroring `state.mixer_thread`) is **pinned out by an existing test**.

Chosen design instead:

```python
state.mixer_tick_failures = {"consecutive": int, "total": int}
```

- **Exact precedent:** `state.recording_write_errors` / `recording_stop_reasons`
  (REL-05c) — health counters on state, mutated under `sync_lock`, snapshotted
  by `/api/health` under `sync_lock`, zeroed by `reset()` for test-fixture
  isolation. `mixer_tick_failures` is the same kind of thing (a health
  counter), so it gets the same treatment.
- **Writers:** the render thread (`_stream_loop`'s except/else arms) and
  `Mixer.start()` (zeroing) — both under `sync_lock`, so the two-writer
  interaction is fully serialized (unlike an instance-attr design, where a
  `start()` reset racing a stop-join-timed-out zombie thread would need a
  documented benign-race note).
- **Happy path stays lock-free:** the recovery arm first does an *unlocked*
  guard read (`state.mixer_tick_failures["consecutive"]` — a GIL-atomic dict
  read) and only takes `sync_lock` on a tick that actually follows failures.
  The audio thread already takes `sync_lock` 2× per tick
  (`snapshot_mixer_state`, `broadcast_audio`); the failing path adds one
  short memory-only section (~µs, ~21.7/s worst case). This is safe precisely
  **because** item 3 (D5) removes the last I/O-under-`sync_lock` section —
  the two changes are one coherent story.
- **Lifecycle:** `Mixer.start()` zeroes both counters inside its existing
  registration `sync_lock` section (fresh render run ⇒ fresh health);
  `Mixer.stop()` does **not** touch them (a died-after-failures mixer's last
  counts stay reportable — `mixer_alive=False/None` already tells you whether
  a run is live); `reset()` zeroes them (fixture isolation, same comment
  rationale as `recording_write_errors` — unlike `mixer_thread`, counters are
  not a live-resource handle).
- `total` (per-run count of failed ticks) rides beside `consecutive` so
  intermittent single failures that never hold `consecutive > 1` are still
  visible to soak/dashboards.

### D2 — Log rate-limit: counter-based, not wall-clock

Replace the unconditional `log.exception` with:

```python
if failures == 1 or failures % TICK_FAILURE_LOG_EVERY == 0:
    log.exception("Mixer render tick failed (%d consecutive); emitting silence", failures)
```

- `TICK_FAILURE_LOG_EVERY = 100` module constant (~1 traceback per ~4.6 s
  worst case at 21.7 ticks/s, down from 21.7/s). Counter-based is
  deterministic and testable (F.I.R.P. — no wall-clock sleeps in the test);
  wall-clock limiting would add `time.monotonic()` calls to the hot except
  path for no extra information.
- A **clean tick after failures** emits exactly one recovery INFO line and
  zeroes `consecutive` (one line per failure episode, bounded by episode
  count). Logging runs **outside** every lock section (invariant 1).

### D3 — Health payload: additive keys only (existing consumers unaffected)

```json
{
  "status": "healthy",
  "is_running": true,
  "mixer_alive": true,
  "mixer_tick_failures": {"consecutive": 0, "total": 0},
  "audit": {"buffered_interactions": 0, "buffered_actions": 0, "failed_flushes": 1},
  "recording": {"...": "unchanged"},
  "ready": true, "checks": {"...": "unchanged"}, "timestamp": 0
}
```

- `"mixer_tick_failures"` is **always an object** (zeroed dict when idle /
  never started — "never started" is already conveyed by `mixer_alive: null`;
  no second None-semantics to document).
- `"audit"` groups the three audit-health numbers; buffer lengths are read
  under the `state.lock` section `health_check` already takes (async, same
  event-loop thread as every buffer mutation — no new lock section, no I/O).
- Verified: `tests/test_api.py::test_health_check` asserts key *membership*
  only ⇒ additive keys are safe; `"status"` stays `"healthy"` (degradation is
  reported in fields, per the `config.py` health docstring contract).

### D4 — Audit failed-flush counter lives in `audit_recording` module state

- `audit_failed_flushes: int = 0` at module level; incremented in
  `flush_recording_buffers`'s except branch, **monotonic lifetime** (never
  reset — an intermittent flush failure must stay visible even after
  recovery; consumers diff). Rationale for module state, not `state.*`: the
  failure is owned by this module's flush, all flush paths (P12 loop,
  stop_show, lifespan shutdown) funnel through this one function serialized
  by `_flush_lock` (asyncio.Lock ⇒ single mutation site on the event loop),
  and the buffers it describes already live on `state` — health reads two
  homes either way, so the counter sits next to its only writer.
- **Pitfall pinned in the plan:** `config.py` must read it as
  `audit_recording.audit_failed_flushes` (module-attr lookup at request time),
  **not** `from ... import audit_failed_flushes` — the latter binds the
  int value at import and goes stale for a mutable counter (and breaks
  test monkeypatching).
- Backlog keys read the existing buffers: `len(state.llm_interaction_buffer)`
  / `len(state.action_buffer)` under the lock section health already holds.

### D5 — `trigger_shutdown`: snapshot+clear under `sync_lock`, kill/wait outside — with lock-ordering proof

Rework (replaces the final `with self.sync_lock:` sweep):

```python
# Terminate tracked subprocesses — kill/wait OUTSIDE sync_lock (FU-1, REL-11
# follow-up): p.wait blocks up to 1 s per proc and the audio tick takes
# sync_lock every ~46 ms; under the lock, N slow-dying procs stall the audio
# path and every other sync_lock holder for up to N seconds.
with self.sync_lock:
    procs = list(self.active_subprocesses)
    self.active_subprocesses.clear()
for p in procs:
    try:
        log.info("Killing tracked process %s...", p.pid)
        p.kill()
        p.wait(timeout=1)
    except Exception:
        pass
```

**Lock-ordering / deadlock proof** (the rework changes hold-time, not order):

1. **`sync_lock` is a leaf lock.** Audited acquirers — audio thread
   (`snapshot_mixer_state`, `broadcast_audio`, and after D1 the failure/
   recovery counter arms), sink writer threads (`_note_sink_write_failure`,
   `_reset_sink_write_errors`, `_detach_failing_sink`), routes/health
   snapshots, `Mixer.start/stop` registration, `register/unregister_subprocess`,
   both `trigger_shutdown` sections — acquire `sync_lock` and, while holding
   it, acquire **no other lock**. After this change, none performs I/O either:
   the subprocess sweep was the *last* I/O-under-`sync_lock` anywhere
   (instruments.json moved out in rel-20, sink finalize in rel-11/22, and D2
   keeps the new failure logs outside the lock). A leaf lock held only for
   memory-only sections cannot participate in a wait-for cycle.
2. **Both `trigger_shutdown` lock sections are memory-only** (flags+detach+
   poison; list-copy+clear) and acquire nothing nested. A section that
   acquires no other lock adds no edge to the wait-for graph, so the (already
   acyclic) order graph is unchanged — no inversion is possible.
3. **The kill loop holds no lock.** It waits only on kernel process reaping,
   bounded by `p.wait(timeout=1)` per proc (`SIGKILL` is uncatchable; a
   `TimeoutExpired` from `wait` is swallowed by the existing `except
   Exception` — total sweep ≤ N s with **zero** lock hold).
4. **Reentrancy:** no caller of `trigger_shutdown` holds `sync_lock` when
   calling (audited: `app_ui` lifespan + `CustomServer.handle_exit` +
   `_run_framework_failure_cleanup` D11; the REL-19 startup-failure path
   takes `sync_lock` precisely *instead of* calling it). `threading.Lock` is
   non-reentrant, so this invariant is load-bearing and unchanged.
5. **Races, bounded and no worse than today:** a concurrent
   `unregister_subprocess(p)` after our clear is a no-op discard and we hold a
   strong ref in `procs`, so the kill still lands; a subprocess *registered*
   between snapshot and clear escapes the sweep — identical residual to the
   current code (a register landing after the sweep's clear survives today
   too), and `shutdown_event` is already set, so spawners are on teardown.

Net effect: the audio tick's worst-case `sync_lock` wait during shutdown drops
from `O(N × 1 s)` to the length of a memory-only section (~µs) — which is
also what makes D1's new counter sections on the audio thread unconditionally
safe.

### D6 — Sanitizer reuse: module-level alias in `framework_mixer`, **no new module**

`Mixer._sanitize_pcm_block` is the single implementation (REL-21); the cleanest
reuse without a new shared module (per the FU-1 direction) is a module-level
public alias, following the existing `ensure_stereo = _ensure_stereo`
(P11-U2) precedent:

```python
# Public module alias (FU-1): the stems download route reuses the exact
# mixer sanitizer — np.clip alone preserves NaN (REL-21).
sanitize_pcm_block = Mixer._sanitize_pcm_block
```

`routes/stems.py` then imports `from app.framework.framework_mixer import
sanitize_pcm_block`. Import graph is clean (routes → framework only; framework
never imports routes; `framework_mixer` already imports only numpy +
`framework_state`, both already in stems.py's import graph) — no cycle, no new
module, one spelling of the concept. Verified during recon: `np` is used in
stems.py **only** at the clip line, so `import numpy as np` is removed with
it (ruff F401 would flag it otherwise). `app/playback.py:59` keeps its own
inline sanitize (already NaN-safe via its `sane` pre-step, REL-32a) —
unifying it is out of scope (see §6).

### D7 — Keepalive kwargs: the exact libpq set, PG branch only

`db.py` PG `connect_args` gains the four standard libpq keepalive parameters
(all accepted by psycopg2 via conninfo; libpq ≥ 9.0):

```python
"keepalives": 1,                    # enable (libpq default varies by platform)
"keepalives_idle": 30,              # start probing after 30 s of silence
"keepalives_interval": 10,          # probe every 10 s
"keepalives_count": 3,              # 3 unanswered probes = dead conn
```

as named constants beside the REL-09 ones (`DB_KEEPALIVE_IDLE_SECONDS = 30`,
`DB_KEEPALIVE_INTERVAL_SECONDS = 10`, `DB_KEEPALIVE_COUNT = 3`) — constants,
not env knobs, matching the existing `DB_*` comment contract. Detection bound
for a silently dropped conn: ~30 + 3×10 = **≤ 60 s** (vs OS default, commonly
2 h+). `pool_pre_ping` already covers the FIN'd/erroring conn; keepalives
cover the black-holed one. Dialect gate unchanged: the SQLite fallback and the
explicit non-PG `DATABASE_URL` branch keep `{"check_same_thread": False}`
exactly (SQLite conninfo would reject libpq params).
**Scope boundary:** `job_waiter.py`'s asyncpg pool is deliberately untouched —
FU-1 scopes db.py/psycopg2 only, and the waiter already bounds its own
half-open exposure (5 s `is_closed()` slices + `command_timeout=30` on every
query); asyncpg-level keepalives remain the rel-17 residual note.

---

## 2. Exact changes per file

### 2.1 `app/framework/framework_mixer.py` (~+16 lines → ~467)

**(a)** Module constant near the top:
```python
# FU-1 (rel-01 follow-up): log the render-tick failure traceback on the first
# failure and every Nth consecutive one — the per-tick guard bounded log
# volume only by tick cadence (~21.7/s under persistent failure).
TICK_FAILURE_LOG_EVERY = 100
```

**(b)** `start()` — zero the counters inside the existing registration section:
```python
        with state.sync_lock:
            state.mixer_thread = self._stream_thread
            # FU-1: fresh render run reports fresh failure counters (D1).
            state.mixer_tick_failures = {"consecutive": 0, "total": 0}
```
`stop()` unchanged (last counts stay reportable — D1 lifecycle).

**(c)** `_stream_loop` — counter + rate-limited logging (deadline/sleep logic
below the guard untouched; the REL-01 "no `continue`" comment stays):
```python
            try:
                self._callback(outdata, self.blocksize, None, None)
            except Exception:
                with state.sync_lock:
                    failures = state.mixer_tick_failures["consecutive"] = (
                        state.mixer_tick_failures["consecutive"] + 1
                    )
                    state.mixer_tick_failures["total"] += 1
                # FU-1: rate-limit the traceback — first failure + every
                # TICK_FAILURE_LOG_EVERY-th consecutive one (D2). Log OUTSIDE
                # the lock (invariant 1).
                if failures == 1 or failures % TICK_FAILURE_LOG_EVERY == 0:
                    log.exception(
                        "Mixer render tick failed (%d consecutive); emitting silence",
                        failures,
                    )
                outdata.fill(0)
            else:
                # Unlocked guard read (GIL-atomic dict read): the lock is
                # taken only on a tick that actually follows failures, so the
                # healthy per-tick path gains nothing but this check (D1).
                if state.mixer_tick_failures["consecutive"]:
                    with state.sync_lock:
                        recovered = state.mixer_tick_failures["consecutive"]
                        state.mixer_tick_failures["consecutive"] = 0
                    log.info("Mixer render recovered after %d failing ticks", recovered)
```

**(d)** After the `Mixer` class, the public alias (D6):
```python
sanitize_pcm_block = Mixer._sanitize_pcm_block  # public alias (FU-1), see D6
```

### 2.2 `app/framework/framework_state.py` (~+9 lines → ~689; already >500 brownfield — additions kept to slot + reset line + shutdown rework)

**(a)** `__init__`, directly under `self.mixer_thread = None`:
```python
        # Render-tick failure counters surfaced to /api/health (FU-1, rel-01
        # follow-up). Written by the mixer render thread + Mixer.start under
        # sync_lock; zeroed by reset() like recording_write_errors (health
        # counters, not a live-resource handle — unlike mixer_thread).
        self.mixer_tick_failures: dict[str, int] = {"consecutive": 0, "total": 0}
```

**(b)** `reset()` — beside the `recording_write_errors` reset (same fixture-
isolation rationale/comment block):
```python
        self.mixer_tick_failures = {"consecutive": 0, "total": 0}
```

**(c)** `trigger_shutdown` — replace the final subprocess sweep per D5
(snapshot+clear under the lock, kill/wait loop outside; the `log.info` line
moves out with the loop, text unchanged; the `except Exception: pass` stays so
an already-dead proc or a `wait` timeout is non-fatal, exactly as today).

### 2.3 `app/framework/audit_recording.py` (~+8 lines → ~344)

Module level, above `_insert_audit_batches`:
```python
# FU-1 (rel-04 follow-up): lifetime count of failed audit flushes, surfaced in
# /api/health. Module-level because flush_recording_buffers is the single
# writer (all flush paths serialize on _flush_lock); monotonic by design —
# intermittent failures stay visible after recovery. Readers must use the
# module attribute (audit_recording.audit_failed_flushes), never a from-import
# (the int binding would go stale).
audit_failed_flushes = 0
```
In `flush_recording_buffers`'s `except` branch, one line before the re-prepend:
```python
            audit_failed_flushes += 1  # FU-1: health counter (module attr, D4)
```
(The existing `print` lines in that function stay — converting them to
`logger` is out of scope for FU-1; noted in §6.)

### 2.4 `app/routes/config.py` (~+22 lines → ~487)

**(a)** Top imports: `from app.framework import audit_recording` (module-attr
reads, D4's pitfall). No cycle (`audit_recording` imports only
`framework_state` at module level; db/models are call-time).

**(b)** Beside `_mixer_thread_liveness`:
```python
def _mixer_tick_failures() -> dict:
    """Mixer render-tick failure counters for /api/health (FU-1, rel-01).

    Copy under sync_lock (zero I/O, same pattern as _recording_sink_status)
    so a health probe never delays an audio tick. Always an object — zeroed
    when idle/never started; "never started" is mixer_alive's null job.
    """
    with state.sync_lock:
        return dict(state.mixer_tick_failures)
```

**(c)** `health_check` — extend the existing lock section + payload:
```python
    async with state.lock:
        is_running = state.is_running
        buffered_interactions = len(state.llm_interaction_buffer)
        buffered_actions = len(state.action_buffer)
    checks = await _readiness_checks()
    return {
        "status": "healthy",
        "is_running": is_running,
        "mixer_alive": _mixer_thread_liveness(),
        "mixer_tick_failures": _mixer_tick_failures(),
        "audit": {
            "buffered_interactions": buffered_interactions,
            "buffered_actions": buffered_actions,
            "failed_flushes": audit_recording.audit_failed_flushes,
        },
        "recording": _recording_sink_status(),
        "ready": checks["ready"],
        "checks": checks,
        "timestamp": int(time.time()),
    }
```

### 2.5 `app/routes/stems.py` (~+2/−2 lines → ~140)

Import `sanitize_pcm_block` (2.1d); **remove `import numpy as np`** (recon-
verified: `np` is used only at the clip line); replace line 23:
```python
    pcm = (sanitize_pcm_block(audio_data) * 32767).astype("<i2")
```
Update `_encode_wav_response`'s docstring line from "Same conversion as the
mixer in framework_mixer.py" to "Same *sanitizer* as the mixer — literally
the shared helper (REL-21/FU-1)."

### 2.6 `app/db.py` (~+10 lines → ~103)

Constants beside the REL-09 block:
```python
# FU-1 (rel-17 follow-up): TCP keepalives so a silently dropped (half-open,
# no FIN) pooled conn is detected in ~idle + count*interval ≈ 60 s instead of
# the OS default (often 2 h+). pre_ping covers conns that error; keepalives
# cover the black-holed ones. libpq-only — never passed to SQLite paths.
DB_KEEPALIVE_IDLE_SECONDS = 30
DB_KEEPALIVE_INTERVAL_SECONDS = 10
DB_KEEPALIVE_COUNT = 3
```
PG branch `connect_args` becomes (exact final dict — this is the pinned shape):
```python
                connect_args={
                    "connect_timeout": DB_CONNECT_TIMEOUT_SECONDS,
                    "options": f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}",
                    "keepalives": 1,
                    "keepalives_idle": DB_KEEPALIVE_IDLE_SECONDS,
                    "keepalives_interval": DB_KEEPALIVE_INTERVAL_SECONDS,
                    "keepalives_count": DB_KEEPALIVE_COUNT,
                },
```

---

## 3. TDD regression tests (write first, confirm red, then implement)

Order: all of §3.1–3.6 red → implement §2 → green → full preflight gate.

### 3.1 `tests/test_mixer_resilience.py` — FU-1 section (mixer counters + rate limit)

Harness: the file's existing synchronous `_stream_loop` drive
(patch `_callback`, flip `_running`) + `client` fixture. The `autouse`
fixture already calls `state.reset()`, which (after 2.2b) zeroes the
counters per test — no extra cleanup needed.

- **F1 `test_consecutive_tick_failures_counted_and_visible_in_health`** —
  callback raises on ticks 1..7 then stops the loop; after `_stream_loop`
  returns: `state.mixer_tick_failures == {"consecutive": 7, "total": 7}`
  (no clean tick ⇒ no reset). Then `GET /api/health` via the `client`
  fixture: `data["mixer_tick_failures"] == {"consecutive": 7, "total": 7}`.
- **F2 `test_clean_tick_resets_consecutive_not_total`** — callback raises
  ticks 1..3, succeeds ticks 4..6, stops: `{"consecutive": 0, "total": 3}`.
- **F3 `test_tick_failure_log_rate_limited`** — `monkeypatch.setattr(
  framework_mixer, "TICK_FAILURE_LOG_EVERY", 5)`; callback raises ticks 1..12,
  succeeds tick 13, stops. With `caplog.at_level(...)`: exactly **3 ERROR**
  records ("tick failed", at consecutive 1, 5, 10) and exactly **1 INFO**
  recovery record. Pins D2's formula without an 11 s real-time run.
- **F4 `test_mixer_start_zeroes_failure_counters_and_reset_clears_them`** —
  fail 3 ticks via a manual `_stream_loop` run; `Mixer().start()` ⇒ counters
  zeroed under the registration lock (then `stop()`); a bare `state.reset()`
  also zeroes them (2.2b) — both lifecycle pins in one test. Also assert
  `stop()` alone does **not** zero them (fail 3 ticks, `stop()`, counters
  still `{"consecutive": 3, "total": 3}`).
- **F5 `test_health_tick_failures_zero_when_idle`** — no mixer started:
  `data["mixer_tick_failures"] == {"consecutive": 0, "total": 0}` while
  `mixer_alive is None` (D3's division of labor).

### 3.2 `tests/test_llm_capture.py` — FU-1 section (audit counters)

Fixture: reset the counter per test (`monkeypatch.setattr(audit_recording,
"audit_failed_flushes", 0)`); reuse the file's `_install_fake_db` /
async-test machinery.

- **C1 `test_failed_flush_counter_and_backlog_visible_in_health`** — seed
  buffers (e.g. 2 interactions + 3 actions) with a show id; fake DB raises on
  session use (or monkeypatch `audit_recording._insert_audit_batches` to
  raise — the name is resolved at call time, so the patch lands); await
  `flush_recording_buffers()`; assert `audit_recording.audit_failed_flushes
  == 1`, buffers re-prepended in order (existing T2 seam); then
  `GET /api/health`: `data["audit"] == {"buffered_interactions": 2,
  "buffered_actions": 3, "failed_flushes": 1}`.
- **C2 `test_successful_flush_does_not_increment_counter`** — happy-path fake
  DB: counter stays 0; health shows `buffered_* == 0` after the flush
  (backlog drains).
- **C3 `test_failed_flush_counter_is_monotonic`** — two consecutive failures:
  counter reaches 2 (no reset on the retry failure).

### 3.3 `tests/test_state.py` — FU-1 section (shutdown lock discipline)

- **S1 `test_trigger_shutdown_does_not_hold_sync_lock_across_subprocess_wait`**
  — fully deterministic (no timing margins):
  ```python
  kill_started, release = threading.Event(), threading.Event()
  class SlowKillProc:
      pid = 4242
      def __init__(self): self.killed = False
      def kill(self):
          self.killed = True
          kill_started.set()
      def wait(self, timeout=None):
          release.wait(timeout=5)  # blocks exactly like a slow-dying proc
  ```
  Register the proc on a fresh `GlobalState()` (file's existing pattern),
  run `trigger_shutdown()` in a thread, `kill_started.wait(timeout=2)`, then
  **`assert state.sync_lock.acquire(timeout=1.0)`** — under the old code this
  acquire times out (the lock is held across `wait`); under D5 it succeeds
  immediately. Release, `release.set()`, join; assert `killed` and
  `active_subprocesses` empty.
- **S2** — existing `test_trigger_shutdown_with_exception_in_subprocess` and
  the other `test_trigger_shutdown*` tests stay green unchanged (the
  `except Exception: pass` contract and the set-clearing are preserved by D5).

### 3.4 `tests/test_api.py` — FU-1 section (stems download sanitize)

- **D1 `test_download_stem_sanitizes_nan_buffer`** — mirror
  `test_download_stem_success`, seeding a **float32** poisoned entry:
  `np.array([[np.nan], [np.inf], [-np.inf], [0.5]], dtype=np.float32)` under
  the stem's prompt. `GET /api/stems/0/download` ⇒ 200; decode the WAV
  payload (`io.BytesIO` + `wave`, mono 16-bit 44.1 kHz) ⇒ samples
  `[0, 32767, -32767, 16383]` and `np.isfinite(...).all()`. A helper-level
  twin may call `_encode_wav_response` directly — the route test is the
  acceptance pin.

### 3.5 `tests/test_db.py` — FU-1 section (keepalives)

- **E1 `test_pg_engine_tcp_keepalives`** — extend the REL-09 T1
  `test_pg_engine_gets_resilience_kwargs` **exact-dict** assert (it pins
  `connect_args` by equality, so it MUST grow the four keys in the same
  commit) to:
  ```python
  assert call_kwargs["connect_args"] == {
      "connect_timeout": 5,
      "options": "-c statement_timeout=10000",
      "keepalives": 1,
      "keepalives_idle": 30,
      "keepalives_interval": 10,
      "keepalives_count": 3,
  }
  ```
- **E2 (absence pin)** — `test_sqlite_fallback_engine_unchanged` and the T3
  real-`create_engine` sqlite recording test are **left byte-identical and
  must stay green** — their exact-dict asserts prove no keepalive kwarg leaks
  to the SQLite branches (the FU-1 "absence on SQLite" clause).

### 3.6 `tests/test_api.py::test_health_check` — two additive lines

`assert "audit" in data` + `assert "mixer_tick_failures" in data` (membership
only — F1/F5/C1 carry the value pins). Also pinned green-by-construction:
`tests/test_state_slices.py::test_levels_view_named_not_mixer_to_avoid_clash`
— the recon constraint that forced D1's design; no new `state.mixer` attr is
created, so it must stay green untouched.

---

## 4. Invariant compliance (plan §Invariants)

1. **Lock discipline:** every new/edited lock section is memory-only
   (counter RMW arms, counter snapshot, buffer-length read under the existing
   `state.lock` block, subprocess snapshot+clear); the rework *removes* the
   last I/O-under-`sync_lock` anywhere (D5 proof); all logging happens
   outside lock sections. No framework function is called while holding a lock.
2. **Hexagonal / AGENTS style:** no new port needed (observability reads
   module/state data); functions stay 4–20 lines; no `Any`; explicit types
   (`dict[str, int]`); comments explain WHY. File sizes after edits: mixer
   ~467, config ~487, stems ~140, db ~103, audit_recording ~344 — all < 500.
   `framework_state.py` (~689) is pre-existing >500 brownfield debt (like
   worker.py); FU-1's additions there are the minimal sites the spec names,
   and no split is silently smuggled in (FU-2/FU-3 own their splits).
3. **Audio-thread purity:** the healthy per-tick path gains one unlocked dict
   read (cheaper than the existing `_debug_count` bookkeeping); the failing
   path adds one µs-scale lock section + two comparisons + (rate-limited)
   logging; no allocation-heavy work, no I/O, no framework calls. The
   sanitizer reuse changes nothing on the mixer's own path.
4. **LLM capture:** item 2 observes only. No row is dropped, truncated, or
   reordered; the failure path still re-prepends under `state.lock`
   (invariant 4 — retain-over-drop — unchanged; the counter just makes the
   retain visible).
5. **Worker/Docker restart:** untouched.
6. **Regression test per fix:** F1–F5, C1–C3, S1, D1, E1/E2 pin every item;
   each fails red against the current tree (F1: `state.mixer_tick_failures`
   does not exist → AttributeError; F3: 13 ERROR records today; C1: no
   counter/backlog keys → AttributeError/KeyError; S1: acquire times out
   today; D1: NaN garbage today; E1: dict mismatch today).

## 5. Acceptance checklist (maps to the FU-1 row)

- [ ] k consecutive raising ticks increment the counter, visible in `/api/health` (F1)
- [ ] clean tick resets consecutive, total persists (F2)
- [ ] log rate-limit honored — caplog record count exact (F3)
- [ ] audit backlog + failed-flush counters in health after a forced flush failure (C1; C2/C3 semantics)
- [ ] `trigger_shutdown` with a fake slow-kill subprocess does not hold `sync_lock` — concurrent acquire succeeds (S1)
- [ ] stems download of a NaN-containing buffer yields finite PCM (D1)
- [ ] engine test asserts keepalive `connect_args` on PG + absence on SQLite (E1/E2)
- [ ] counter lifecycle: `start()` zeroes, `stop()` preserves, `reset()` clears (F4)
- [ ] health payload keys purely additive; `test_health_check`,
      `test_levels_view_named_not_mixer_to_avoid_clash`, and every existing
      health/mixer/shutdown/db test green unchanged (except T1's mandated
      exact-dict growth)
- [ ] full gate: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q`
      ≥ 1178 passed / 26 skipped

## 6. Risks / out of scope

- **Counter freshness:** the unlocked guard read in the recovery arm can miss
  the last failure episode after a concurrent `reset()` dict swap —
  observability-only (same staleness class as `mixer_alive`); the locked RMW
  keeps the counter itself consistent.
- **`audit_failed_flushes` monotonic:** intentional (D4); if a future
  dashboard wants rate, diff it. No reset hook added (no speculative API).
- **Keepalive values are constants**, not env knobs — matches the REL-09
  `DB_*` contract ("promote only when a deployment needs a different
  budget"). Detection math (~60 s) documented in the comment.
- **`stems.py` drops `import numpy as np`** — recon-verified it has no other
  use; `ruff` guards the removal.
- **Out of scope, explicitly:** converting `audit_recording`'s `print`s to
  `logger`; unifying `playback.py`'s inline sanitize onto the shared alias;
  asyncpg-pool keepalives (`job_waiter` — rel-17 residual, existing 5 s
  slices + `command_timeout` bound it); soak-harness assertions over the new
  counters (a later FU can extend `test_soak_247.py` if wanted); any
  `framework_state.py` split (FU-2/FU-3 territory).
- **Commit shape:** one branch `rel-fu-1-health`, TDD order per §3; Conventional
  Commit on land (parent-landed), matching the round-1 units.
