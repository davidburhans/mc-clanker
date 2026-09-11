# Round 3 — Async Lifecycle Lane (loop_orchestrator / loop_steps / framework_main_async / app_ui lifespan / state_slices)

Adversarial bug hunt, lane: asyncio task lifecycle, watchdog/retry, shutdown ordering,
sync-blocking-in-async, lock interleaving, stale state on error paths.
Method: every target file read in full; each candidate falsified against callers, guards,
regression pins (`test_adversarial_wave1/wave2/leftovers`, `test_loop_fixes`,
`test_pregeneration_divergence`, `test_async_framework`) and git history
(`603a541` round-2 fixes, `df34229` Phase-B extraction, `347a215` original migration)
before being reported.

---

## CONFIRMED BUGS

### 1. CRIT | `app/framework/loop_steps.py:656-668` (with `:213-217`, `:696-698`) | Stale-pregen fallback spawns an unpaced, zero-yield replay iteration → permanent event-loop starvation + runaway `loop_count`/audit inflation

**Mechanism — three cooperating lines:**

1. `_step_post_commit` else branch (`loop_steps.py:656-668`): when a pre-gen task is
   **still pending** at commit time (`needs_pregen == False`, `:538`), it sets
   `self._pregen_done.set()` (`:659`) and fabricates
   `self._pregen_results = {"loop_idx": self._loop_idx + 1, ...}` (`:662-668`) —
   a *self-issued* pregen result for the next loop, built from the audio just committed.
2. P13 (`_step_await_pregen`, `loop_steps.py:696-698`) checks
   `if self._pregen_done.is_set(): break` **before** its only `await`
   (`asyncio.sleep(0.25)` at `:709` is below the break). With `_pregen_done` set by the
   else branch, P13 returns without ever suspending.
3. The next `_run_loop` iteration then hits the P2 pregen-ready gate
   (`loop_steps.py:213-217`): `_pregen_results["loop_idx"] == self._loop_idx` is **True**
   → the loop "replays" the same audio (skips P4-P9 entirely), commits again
   (`state.loop_count += 1`), reaches P12, and — because the stale pregen task is *still*
   pending — takes the else branch again, issuing `loop_idx + 2`. Repeat forever.

A full replay iteration contains **no suspension point**: every `async with state.lock:`
acquisition on this path is uncontended (asyncio.Lock uncontended acquire does not yield),
`_step_append_audit` (`:489-493`) delegates to a lock-then-append with no yield when no
other waiter exists, mixer calls are synchronous, and P13 breaks before its sleep. The
framework task therefore monopolizes the event loop **permanently**: nothing else on the
loop — route handlers, WebSocket, `/api/health`, even the *stale pregen task's own wakeup*
— is ever scheduled again.

**Trigger (normal operation):** the background pregen spawned at loop N's commit is still
awaiting its LLM call + generation jobs when the mixer's playback headroom drops below
0.5 s (`current_ahead < 0.5` break, `:703-711`). Pregen latency is 0.1–10 s LLM +
5–30 s+ GPU job vs. a ~15-30 s loop; any GPU-queue congestion that makes pregen outlast
one loop's playback arms the trap. P13's own comment ("Still waiting for pre-gen, but we
need to break…") documents that this break-with-pregen-pending path is expected to happen.

**Impact:** the entire FastAPI app freezes (all HTTP/WS dead) until process restart — the
release of the stuck pregen cannot help because its completion callback is never scheduled.
Meanwhile each spin: `state.loop_count += 1`, `set_next_loop` re-queues identical audio,
`append_loop_audit` buffers another interaction row — during a live show the buffers grow
unboundedly (memory → OOM) with absurd `loop_index` values, and `stem_history` fills with
8 copies of the same set. Music itself keeps playing the same loop (mixer is a separate
thread). Round-2's `test_async_framework.py::test_pregen_skip_when_loop_already_queued`
"pins" the else branch by *re-implementing it inline in the test body* (the D4 anti-pattern
from round 1) and never exercises the iteration loop, so this was never caught.

**Minimal fix:** in the else branch, do **not** fabricate `_pregen_results`/`_pregen_done`
for the next loop; either cancel the stale pregen task and spawn a fresh one, or leave
`_pregen_done` cleared and pace the replay on the mixer boundary (e.g., only issue the
`loop_idx+1` result after `pop_transition_event()` reports the real transition, and add a
`await asyncio.sleep(0.25)` before re-checking so every iteration yields).

**Proof** (fakes only for mixer/conductor/jobs/audio/audit — the loop code is real;
pattern matches the repo's own characterization tests):

```
Scenario: iteration 2 commits, spawns pregen(3) (stubbed to block on an Event),
P13 sees current_ahead < 0.5 → breaks with pregen(3) pending.
Iteration 3 goes fresh-path (conductor stub returns actions=[]), commits, and at P12
the pending pregen forces the else branch → _pregen_results = {"loop_idx": 4}.
Every later iteration: P2 pregen_ready=True (replay) → P12 else → P13 immediate break.

Run A (unthrottled stdout): 1,803,045 "[..] already queued, skipping pre-gen" lines
(≈900k spin iterations, 16 GB log) in <55 s before the harness timeout killed it.
Normal pacing is 1 commit per ~15-30 s of audio.

Run B (stdout silenced): loop.task runs; after the spin starts, the driver awaits
asyncio.sleep(3.0). A threading.Timer at t+3s calls
running_loop.call_soon_threadsafe(release_pregen.set). Result: sleep(3.0) NEVER
resumed — killed at 147 s (exit 124). The threadsafe release was queued but the
starved loop never processed it → the starvation is permanent, not bounded by the
pregen's own 120 s timeout.
```

---

### 2. MED | `app/app_ui.py:550-566` (and `:529-535`, `:594`) | `audio_stream_generator` leaks the ffmpeg process (and, when `Popen` itself raises, the client queue) on the pre-feed failure path

**Mechanism:** `subprocess.Popen(...)` at `:550` spawns ffmpeg; the pre-feed
`process.stdin.write(first_chunk)` at `:559` sits in a try/except whose handler
(`:562-566`) removes the client queue and `return`s — **without** `process.kill()`,
`process.wait()`, or `state.register_subprocess(process)` (registration only happens at
`:594`, after the risky window; the `finally` at `:610-617` that kills/waits/unregisters
is never reached because the `return` precedes it). Additionally `Popen` at `:550` is not
inside any try: if the ffmpeg binary is missing (the capability probe at `:529-535`
merely *warns* when libmp3lame is absent; the binary can also be missing entirely), the
`FileNotFoundError` escapes the generator **before** the client-removal try/finally, so
`client_q` stays registered in `state.audio_clients` forever and the exception surfaces as
a mid-response 500.

**Trigger:** (a) ffmpeg exists but exits instantly on the first write (broken build /
missing libmp3lame — a scenario the code explicitly anticipates with only a warning) →
`BrokenPipeError` on the pre-feed; (b) ffmpeg missing or `Popen` hits an OSError →
uncaught exception. Each `/stream.mp3` request then leaks: (a) one un-reaped ffmpeg child
(zombie until process exit) + two open pipe FDs, or (b) one orphaned `client_q` that
`broadcast_audio` iterates and fills forever.

**Impact:** resource leak per failed stream request; with a broken ffmpeg config every
browser (re)connect leaks another process/FD or queue entry. Over a long-running server
this accumulates until FD/memory exhaustion.

**Minimal fix:** move `state.register_subprocess(process)` (and a try/except that does
`process.kill(); process.wait()`, then `state.remove_audio_client(client_q)`) into a
`try/finally` that spans both the `Popen` call and the pre-feed write.

**Proof** (drives the real `audio_stream_generator` with a stubbed `subprocess.Popen`;
`subprocess.run` stubbed for the codec probe):

```
Case 1 — Popen returns a process, pre-feed write raises (as BrokenPipeError does when
ffmpeg died instantly):
  generator returns after "Warning: Could not pre-feed first chunk to ffmpeg"
  process.kill_called = False, process.wait_called = False
  -> spawned process never killed/reaped/registered (leak).
Case 2 — Popen raises FileNotFoundError:
  exception escapes next(gen) uncaught; leaked_client_queues = 1
  -> client queue remains in state.audio_clients forever.
```

---

## SUSPECTED (UNVERIFIED)

### 3. LOW | `app/framework/loop_orchestrator.py:202-206` (`_finish_loop`) + `:190-200` (`stop`) | `_pregen_task` is cancelled but never awaited; `stop()` returns before the pregen task is quiesced

**Mechanism:** `_finish_loop` calls `self._pregen_task.cancel()` (`:204-205`) without
awaiting it; `stop()` awaits only `loop_task`. Cancellation is delivered at the pregen
task's next suspension, so after `stop()`/lifespan shutdown returns, the task may still be
pending; if the event loop closes first (uvicorn final exit), it dies as "Task was
destroyed but it is pending", and its in-flight executor poll (`wait_for_job_completion`
polling path) can still touch the DB while the lifespan already ran
`close_asyncpg_pool()`.
**Trigger:** shutdown (SIGTERM / lifespan exit) while a pregen task is mid-`_await_jobs`.
**Impact:** log noise + a bounded shutdown race; no state corruption found (the framework
never routes through the asyncpg pool, and `submit` is sync-atomic w.r.t. cancellation).
**What would confirm:** instrument `run_pregeneration` with a pending job-wait at
shutdown and observe the destroyed-pending warning + post-`close_asyncpg_pool` DB call.

### 4. LOW | `app/app_ui.py:98` + `app/framework/loop_orchestrator.py:176-186` | A failure inside `AsyncFrameworkLoop.start()` kills the framework silently; lifespan keeps a dead task and `/api/health` keeps reporting `is_running: true`

**Mechanism:** `run_framework_loop_async` calls `await loop.start()` **outside** any
try/except; `start()` runs the mixer factory and `mixer.start()` (thread spawn) before
`asyncio.create_task(self._run_loop())`. If any of that raises (thread-exhaustion,
`Mixer.start` failure), the exception sits unretrieved in `framework_task`; nothing
restarts the loop and nothing flips `state.is_running`; the B1 watchdog (inside
`_run_loop`) never gets a chance to see it.
**Trigger:** mixer/threading init failure at startup (rare, but the exact gap the B1 fix
did not cover — it only guards `_run_loop` iterations).
**Impact:** music never starts; app appears healthy.
**What would confirm:** patch `mixer_factory` to raise, run the lifespan, observe no log
of framework death and `/api/health` still 200 with `is_running: true`.

---

## CHECKED AND CLEAN

- **`_step_await_jobs_fetch` zip ordering** (`loop_steps.py:404`: `zip(pending_jobs,
  results.values())`): falsified as a misalignment risk — `wait_for_multiple_jobs`
  (`app/job_waiter.py:310-331`) builds the dict by zipping the *same ordered* `job_ids`
  with `asyncio.gather` output (gather preserves input order), so `results.values()`
  matches `pending_jobs` order. The pregen path additionally uses key lookup.
- **B1 watchdog shape** (`loop_orchestrator.py:270-283`): `except Exception` + fixed 2 s
  backoff (`LOOP_RETRY_BACKOFF_SECONDS`) + `continue`. CancelledError is BaseException and
  propagates; the retry sleeps (yields), so no hot-retry; no state is left half-committed
  that the next iteration cannot rebuild (pregen gate is `loop_idx`-equality keyed).
- **Lifespan shutdown ordering** (`app_ui.py:105-120`): `trigger_shutdown` sets
  flags/closes recording handles under `sync_lock` (`framework_state.py:484-525`, the
  round-2 A4/B8 fix), poisons audio queues, kills tracked subprocesses; framework task is
  then cancelled and awaited; `close_asyncpg_pool` suppressed. No double-close or
  handle-after-close window found: `Mixer.stop()` is idempotent under the natural-exit
  `_finish_loop()` + `stop()` double-call (thread already dead → join returns).
- **`run_framework_loop_async`'s `while loop.running` spin** (`loop_orchestrator.py:296`):
  if `_run_loop` ends naturally (`state.is_running` False), the entry task would spin at
  1 Hz without calling `stop()`. Falsified as unreachable in practice: `is_running` is
  only set False by `trigger_shutdown`, which in every entry path (lifespan cancel,
  `__main__` `handle_exit` → process exit) is followed by task cancellation or process
  teardown.
- **`stream_mp3` blocking calls** (`app_ui.py:570-617`): `subprocess.run(timeout=5)`,
  `process.stdout.read`, feeder thread — all inside a *sync* generator, which Starlette
  iterates in a threadpool; none run on the event loop.
- **P10/P11 lock discipline** (`loop_steps.py:474-536`): no I/O inside `async with
  state.lock`; `record_loop_transition` (blocking `sync_lock`) is called outside
  `state.lock` in both P12 (`:640`) and P13 (`:693`), per the round-1 A3 fix; the
  `sync_lock` hold windows it can block on are snapshot-length only (B9 moved file writes
  outside the lock).
- **Loop-1 → loop-2 path**: `_loop_idx == 1` makes `needs_pregen` False → else branch runs
  once, but iteration 2's P12 then finds `_pregen_task is None` → spawns pregen and clears
  `_pregen_done`, so the steady-state P13 wait is properly paced. The hot spin requires
  the stale-pregen race (bug 1), not this path.
- **`state_slices.py`**: pure additive read-views (frozenset `_attrs`, `__getattr__`
  forwarding); no write path, no locks, no lifecycle — nothing to break.
- **AuthMiddleware/SessionAffinityMiddleware sync SQLAlchemy in `async dispatch`**
  (`app_ui.py:131-150, 305-330`): real, but this is round-1 finding A6 (known, unfixed);
  not re-reported per instructions.
- **`first_chunk is None` / `queue.Empty` pre-Popen paths** (`app_ui.py:511-526`): these DO
  remove the client queue before any resource is created — clean; only the post-`Popen`
  paths leak (bug 2).

## VERIFICATION

Independent verification against HEAD (04791e4). Proofs run with `.venv/bin/python` (3.12.13) against the real loop/app_ui code with fakes only at the ports, matching the repo's characterization-test pattern.

FINDING 1 | Verified | quotes match at app/framework/loop_steps.py:656-668 (else branch :659 set/:663 fabricated loop_idx), :213-217 (P2 gate), :696 vs :712 (P13 break-before-await; the sleep is at :712, not :709 — :709 is the `current_ahead < 0.5` break), needs_pregen at :538 keys the spin to the pending stale task; reproduced: 1,589,597 commits in 10 s and the driver's `asyncio.sleep(3.0)` never resumed in 10 s (SIGALRM kill) because the replay iteration's only awaits are uncontended `state.lock` acquires (audit_recording.py:179) and the :712 sleep is below the :696 break — starvation is permanent and actually broader than stated (P2's pregen_ready branch returns PROCEED without checking `state.is_generating`, so even a UI stop won't break the spin); the only "pin", tests/test_async_framework.py:249-271, builds the results dict inline and never runs the iteration loop
FINDING 2 | Verified | lines match exactly in app/app_ui.py — Popen at :550 outside any try, pre-feed write :559, handler :562-566 returning before the :613-617 finally, `register_subprocess` only at :594; proven by execution: case 1 (pre-feed BrokenPipeError) generator returned with kill_called=False, wait_called=False, active_subprocesses=0 (leaked un-reaped ffmpeg + 2 pipe FDs), case 2 (Popen FileNotFoundError) exception escaped uncaught with the client_q left in state.audio_clients (broadcast_audio at framework_state.py:443-448 iterates/fills it forever); tests/test_app_ui.py:149-201 covers only the pre-Popen paths, so nothing pins or fixes this
FINDING 3 | Weakened | the smell is real (`_pregen_task.cancel()` with no awaiter anywhere — loop_orchestrator.py:204-205 is the only touch) but the claimed consequences do not reproduce: in the faithful stop() flow (loop_task CancelledError handler → _finish_loop, loop_orchestrator.py:280-282) `_pregen_task.done()` was already True the moment stop() returned, even with pregen mid-executor-poll, because stop()'s `await self.loop_task` (:190-198) yields a turn and every pregen await is unshielded-cancellable (job_waiter.py:294 run_in_executor, :318 sleep, :331 gather); the surviving post-shutdown DB work is the orphaned executor *thread*, which a proper await would not stop either — stands only as unawaited-cancellation hygiene
FINDING 4 | Verified | `await loop.start()` at app/framework/loop_orchestrator.py:401 sits outside the try (try begins :402) and start() (:176-188) runs the mixer factory + `mixer.start()` before `create_task(self._run_loop())` at :188, so the B1 watchdog (:276-287, inside `_run_loop`) is never spawned; proven by execution: framework_task.done()=True holding the unretrieved RuntimeError while `state.is_running` stays True — exactly what /api/health reports (app/routes/config.py:99-103; default True at framework_state.py:123, only cleared by trigger_shutdown :497 / atexit app_ui.py:124) — and grep shows nothing monitors framework_task (only app_ui.py:98,101,109,111)
