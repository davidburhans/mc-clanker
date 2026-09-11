# Reliability Audit — Music Production Pipeline

Deep reliability pass over the audio-production critical path, targeting the
24/7 requirement: *no memory growth, no performance degradation, no silent
stalls that halt the music over multi-day operation.*

Method: four parallel read-only review lanes (real-time audio path /
orchestration loop / GPU worker / app-server & storage), followed by parent
spot-verification of every Critical and High finding against source. All
file:line evidence below was confirmed or quoted from source; findings found
independently by multiple lanes are marked **[×2]/[×3]**.

**Verdict: NOT 24/7-ready.** The per-tick mixer core is unusually well
hardened (bounded queues, capped histories, careful locking — see §Verified
safe), but there are **5 Critical** and **10 High** findings: three
permanent-silence paths, four unbounded-growth subsystems, and a class of
event-loop stalls that freeze the entire server on routine infrastructure
events. All Criticals have small, local fixes.

---

## Failure theme map

| Theme | Findings | What the operator sees |
|---|---|---|
| **A. Permanent silence** | REL-01, REL-02, REL-03 | Music stops; UI shows "live"; `loop_count` climbs; GPU keeps burning; health stays green |
| **B. Unbounded growth → OOM/ENOSPC** | REL-04, REL-05, REL-12, REL-16 | RSS or disk climbs monotonically; dies in days; audit trail already lost |
| **C. Whole-server stalls** | REL-09, REL-11, REL-20 | All endpoints (incl. `/api/health`, `/stream.mp3`) freeze ~2 min; audio dropouts |
| **D. Slow leaks** | REL-07, REL-08, REL-10, REL-15 | Threads/VRAM/ffmpeg subprocesses creep after faults or audience churn |

---

## P0 — Halts the music or guarantees exhaustion (fix before any unattended run)

### REL-01 [Critical] One exception in the mixer render loop kills audio forever — health stays green
`framework_mixer.py:406-408` — `_stream_loop` calls `self._callback(...)`
with **no try/except anywhere** in the loop or `_callback`. The daemon thread
dies silently; `_running`/`state.is_running`/`is_generating` all stay `True`;
no `is_alive()` check exists anywhere in `app/`; `/api/health` is flag-based;
`_on_framework_task_done` watches the async task, not the thread.
One `MemoryError` from `np.tile` or a future shape/dtype surprise → every
output (MP3, YouTube, recording) goes silent while everything looks alive.
Recovery: process restart.
**Fix:** wrap the `_callback` call in `try/except Exception: log.exception();
outdata.fill(0); continue` + expose `mixer thread alive` in `/api/health`.

**Status: fixed-in rel-01-mixer** — guard landed without the sketch's literal
`continue` (falls through to the deadline/sleep bookkeeping instead, so a
persistently-failing callback cannot busy-spin); `state.mixer_thread` is
registered in `Mixer.start()/stop()` and `/api/health` gained
`mixer_alive: bool | null` (`None` = no thread registered, `False` = died
without `stop()`). Pinned by `tests/test_mixer_resilience.py` and
`tests/test_api.py::test_health_check`.

### REL-02 [Critical] Reset after loop ≥ 2 = permanent silence
`loop_steps.py:372-375` — reset branch calls `self.mixer.clear()` →
`current_loop_end_sample = 0`. Re-priming exists **only** on the loop-1 path
(`loop_steps.py:636-641`); later loops use `set_next_loop`, which never
touches the boundary. Mixer transition logic is gated on
`current_loop_end_sample > 0` (`framework_mixer.py:229`), so the staged loop
is never consumed. DJ hits Reset then Start: generation continues, audience
hears nothing, forever.
**Fix:** in the reset branch (or commit step), when
`mixer.current_loop_end_sample == 0`, force `self._loop_idx = 0` so the next
commit takes the `prime_loop` path.

**Status: fixed-in rel-02-reset** — force landed in the commit step
(`_step_commit_to_mixer`, immediately before the branch), not the reset
branch: at P3 a `_loop_idx = 0` write still lets the current iteration stage
dead audio through `set_next_loop` (P2's pregen decision is already made);
normalizing in P10 primes the same iteration, wasting nothing. Value is
`_loop_idx = 1` (not the sketch's 0), so the prime path is taken now and the
post-reset flow replays the production loop-1→loop-2 sequence; the `<= 0`
guard also covers the second boundary-zeroing site (`Mixer._callback`
no-future-tracks fallback). Post-reset, show-audit `loop_index` restarts at
1 while `state.loop_count` stays monotonic. Pinned by
`tests/test_reset_reprime.py` (reset-then-restart end-to-end on a real
`Mixer`, prime-path force, live-boundary no-over-force, accepted-pregen
reset) plus boundary-mirroring fakes in `tests/test_round3_fix_b.py` and
`tests/test_loop_fixes.py`.

### REL-03 [Critical] Worker generation-timeout leaks non-killable threads holding VRAM; wedged CUDA never self-heals
`worker.py:275-285` — `asyncio.wait_for(..., timeout=600)` around generation
in a private `ThreadPoolExecutor`; on timeout `shutdown(wait=False,
cancel_futures=True)` abandons the *running* thread (Python cannot cancel
it). The zombie keeps the GPU busy / holds the hf_hub `.incomplete` lock
(first cold-cache model download easily exceeds 600 s → every retry blocks
on the lock and times out too, leaking one thread + model refs each cycle).
Container healthcheck (`pgrep` + `SELECT 1`) passes throughout; after `stop()`
the interpreter deadlocks joining non-daemon threads. Silent permanent wedge.
**Fix:** consecutive-timeout circuit breaker — on the 2nd consecutive
generation timeout, structured-log and `os._exit(1)`; Docker restart is the
designed recovery for a wedged CUDA context. Pre-download enabled models in
`start()` outside the timeout window.

**Status: fixed-in rel-03-worker** — breaker landed in
`_generate_with_lease`/`_handle_generation_timeout`: the counter resets only on
a completed generate+upload pipeline (a non-timeout failure still leaves the
timeout-#1 zombie holding VRAM), the exit hook is ctor-injected (defaults to
`os._exit`), and the timeout re-raises as `TimeoutError("generation pipeline
exceeded Ns")` so the row still gets a meaningful `error_message`. Weights
pre-download via `GeneratorRegistry.download_models()` at `start()`, off-loop
and before the pool exists, with per-model failure isolation. Pinned by
`tests/test_worker_vram.py` (B1–B4, P1–P4).

### REL-04 [Critical] Show audit buffers grow in RAM for the whole show; flushed only at stop **[×3 — loop, server, mixer lanes]**
`audit_recording.py:184,199` — `append_loop_audit` appends one LLMInteraction
dict (full conductor JSON) + N ShowAction dicts per loop, no cap. Only
production flush caller is `stop_show` (`shows.py:453-455`); `ports.py:22`
documents "the loop never calls flush"; lifespan shutdown doesn't either.
At ~8 s/loop: ~25–90 MB/day, week-long set = 0.2–0.6 GB → OOM in days. On
flush failure rows are re-prepended and compound (`audit_recording.py:64-69`).
**Entire show's audit trail is lost on any crash** (never touched disk).
**Fix:** periodic flush from `_step_post_commit` when
`len(state.llm_interaction_buffer) > 200` (`AuditAdapter.flush` exists and is
lock-serialized); keep stop-flush; flush in lifespan shutdown.

**Status: fixed-in rel-04-capture** — P12 (`_step_post_commit`) now flushes via
the ctor-injected audit port past `AUDIT_FLUSH_THRESHOLD_ROWS = 200`
(loop_steps.py), after the pregen spawn so a slow flush never eats pre-generation
lead time; the flush's bulk insert runs in a worker thread (`asyncio.to_thread`)
so the per-loop cadence never blocks the event loop. `stop_show`'s flush is kept,
and lifespan shutdown adds a best-effort flush bounded by
`FLUSH_SHUTDOWN_FLUSH_TIMEOUT_SECONDS = 10` (a timed-out flush loses at most the
unflushed tail — the same bound a crash has; documented residual). Same unit
amended REL-14's start/delete paths (see below) so flush failures re-queue
without discarding, and capture gained the real chat + `applied_actions` (U4/DPO
field audit). Pinned by `tests/test_llm_capture.py` (T1–T4, T8, T16).

### REL-05 [Critical] Recordings grow unbounded — 15.2 GB/day per live show; ENOSPC in 1–3 days, then silent corruption
Recordings write 44.1 kHz stereo 16-bit PCM = 635 MB/hr (`shows.py:51-56`;
the >4 GiB RIFF overflow handling shows multi-GB files are an *expected*
state). `delete_show` (`shows.py:324-335`) deletes the row but never unlinks
`audio_file_path` or stamped takes; `/export/start` writes to `EXPORT_DIR`
with no retention; `cleanup.py` covers only `generator_jobs`. On ENOSPC,
`_write_recording_sink` logs the failure **once** and keeps "recording" —
silently corrupt audio, then Garage/DB writes start failing too.
**Fix:** unlink files in `delete_show` + retention pass in
`JobExpirationCleanup` for recordings/exports; surface recording-write
failures to health, not a one-shot log line.

---

## P1 — Degradation, leaks, and stalls (fix before trusting multi-day runs)

### REL-06 [High] `stem_cache` hits never refresh `last_used` → retained stems evicted & regenerated every 300 s
`loop_steps.py:524-527` — cache HIT does `continue` without touching
`last_used`; eviction at `loop_steps.py:768` uses `current_time - last_used >
300`. Any stem retained ≥ 5 min (the "core groove" the conductor is told to
retain) is evicted, cache-missed next loop, and regenerated → **audible
discontinuity in a supposedly-retained layer every 5 minutes, forever**, plus
needless GPU load. During an LLM outage the retain-all fallback converts this
into a permanent full-set regeneration cycle.
**Fix:** one line — refresh `last_used` on hit (and in pregeneration's hit
path); consider an entry cap in addition to TTL.

**Status: fixed-in rel-02-reset** — hit refresh landed in both hit paths
(`_step_submit_jobs` foreground and `run_pregeneration` pregen; a
`loop.stem_cache`-only write). The "entry cap considered" became
`STEM_CACHE_MAX_ENTRIES = 32` alongside the named
`STEM_CACHE_TTL_SECONDS`, with maintenance extracted to `_prune_stem_cache()`
(stale-then-oldest-`last_used` overflow, called from P12 only — single async
owner, no lock). Pinned by `tests/test_reset_reprime.py` (foreground + pregen hit
refresh, retained stem survives the TTL prune, TTL window, entry cap).

### REL-07 [High] No concurrency guard in `GeneratorRegistry` — zombie threads race model loads
`framework_generator.py` contains zero `threading`/`Lock`/`no_grad`/
`inference_mode` (verified by grep). After REL-03 leaks a zombie, the next
job's `generate_batch` lazy-load races it on the same engine; two concurrent
`load_model`s double VRAM → OOM → another timeout → another thread.
**Fix:** `threading.Lock` serializing `load_model`/`generate_batch` (single-GPU
worker gains nothing from concurrency).

**Status: fixed-in rel-03-worker** — one coarse `GeneratorRegistry._generation_lock`
serializes `generate_batch`/`load_model`/`unload_model` (`reload_model` takes
it twice sequentially, never nested; the lazy load rides `_load_model_locked`).
Compositional note: a REL-03 zombie dying inside `generate_batch` now holds
this lock forever — which is exactly what escalates the next job to the
timeout→breaker→restart path instead of racing it for 2× VRAM. Pinned by
`tests/test_worker_vram.py` (L1–L2).

### REL-08 [High] Model load transiently holds ~2× model size in VRAM
`framework_generator.py:93-100` — `load_file(model_path, device=self.device)`
loads weights straight to GPU, then `load_state_dict` + `.to(self.device)`
while the state_dict is still referenced → ~12 GB peak for a 6 GB model.
OOMs on ≤16 GB cards at first second-model load even when steady state fits.
**Fix:** `load_file` on CPU, `del state_dict` after `load_state_dict`, single
`.to(device)`.

**Status: fixed-in rel-03-worker** — weight read is CPU-first
(`load_file(device="cpu")` / `torch.load(map_location="cpu")`), `state_dict`
deleted after `load_state_dict`, then one `.to(device)` move. Peak-VRAM halving
itself is only verifiable on real hardware (U15 soak #4); the captured
device/map_location args + single `.to()` are pinned by `tests/test_worker_vram.py`
(F1–F2).

### REL-09 [High] Sync SQLAlchemy on the event loop + engine without `pool_pre_ping`/`pool_recycle`/timeouts
`db.py:20` — `create_engine(database_url, pool_size=10, max_overflow=20)` and
nothing else. Every route is `async def` doing blocking queries on the loop;
middleware queries the DB **per request** (`app_ui.py:194-195, 304-305,
413-416`). First PG restart/NAT idle-drop (guaranteed within weeks) → burst
of `OperationalError` 500s; if the DB hangs rather than refuses, no
connect/statement timeout → **event loop blocks ~2 min: `/api/health`,
`/stream.mp3`, WebSockets — everything freezes**.
**Fix:** `pool_pre_ping=True, pool_recycle=1800, connect_args=
{"connect_timeout": 5, "options": "-c statement_timeout=10000"}`; move
route/middleware DB work off-loop (`asyncio.to_thread`, pattern already in
`config.py:_ping_database`).

### REL-10 [High] `/stream.mp3` abrupt disconnect leaks one ffmpeg + two threads + a registered client queue
`app_ui.py:713-736` — sync generator blocks in `stdout.read(4096)`; feeder
thread blocks writing ffmpeg stdin under backpressure; the cleanup `finally`
runs only when the generator closes, but Starlette abandoning a threadpool
worker keeps the frame alive indefinitely. Each flaky mobile client leaves a
zombie ffmpeg (encoding silence into a full pipe), 2 threads, 2 pipes, ~0.8 MB
queue. Tens/day on a public stream.
**Fix:** transcode once in a process-wide singleton and fan out MP3 to
per-client bounded queues (clients own no subprocess); interim: watchdog
killing ffmpeg whose client queue has been full >N s.

### REL-11 [High] Recording file writes run on the real-time mixer thread
`framework_state.py:460-479` — `handle.write(pcm_data)` inline in
`broadcast_audio`, called every ~46 ms tick. Any disk stall (page-cache
flush, cgroup IO throttle, ENOSPC-adjacent slowness) delays **every** tick
for **all** listeners — periodic dropouts that correlate with long recording
sessions.
**Fix:** bounded-queue + dedicated writer thread per sink (mirror
`YouTubeRelay`), drop-oldest with a counter.

### REL-12 [High] Abandoned jobs are immortal; `pending` rows never reaped; no submission backpressure
`loop_steps.py:61` — batch budget 600 s + 30 s grace; on expiry the loop
prints "failed or timed out" and moves on — the row is never cancelled, and
the next loop's cache-miss resubmits identical stems while the worker's FIFO
claim still processes every abandoned row (no `expires_at` filter in its
claim; `cleanup.py:107-116` only deletes terminal statuses). Worker outage →
~700 immortal pending rows/day, then hours of wasted GPU on prompts the loop
already abandoned; sustained slow-worker episode → unbounded queue growth.
**Fix:** terminal-abandon losers after the grace pass
(`UPDATE ... SET status='failed', error_message='loop_abandoned'`), extend
reaper to fail stale `pending` rows past `expires_at`, and throttle
submission when observed drain rate falls behind.

### REL-13 [High] Export/stats/timeline endpoints load entire tables; exports are broken on real sessions
`reasoning_logs.py:153,178,188,275` and `shows.py:646,667` — unbounded
`.all()` (a week-long show ≈ 75 k interactions ≈ hundreds of MB per request);
and because the session commits+expires instances **before** Starlette
iterates the response generator, real (unmocked) exports raise
`DetachedInstanceError` after headers are sent → empty/truncated bodies.
**Fix:** serialize rows to dicts inside the session; `yield_per`/keyset
pagination under a fresh session per chunk; SQL `GROUP BY` for stats/timeline.

### REL-14 [High] Deleting a live show poisons the audit flush forever
`shows.py:334-335` tears down recording but leaves `llm_interaction_buffer`
rows referencing the now-deleted `show_id` (cascade delete). Next flush → FK
`IntegrityError` → `audit_recording.py:64-69` re-prepends the batch → every
future flush fails identically while appends continue: unbounded growth +
error-per-flush until restart.
**Fix:** drop buffered rows for the deleted `show_id` in the teardown path
(or flush-then-delete).

**Status: fixed-in rel-04-capture (amended finding)** — the 2026-09-11 U4
verification found the FK-poison loop already unreachable: `start_show` cleared
both buffers first, but that clearing *silently discarded* captured rows —
itself an invariant-4 violation (the buffers ARE the fine-tuning corpus). The
landing: `start_show` now flushes BEFORE the recording flags go live and
RETAINS (logs) any rows a failed flush re-queued instead of discarding them
(buffered rows carry their own `show_id`, so they flush even with
`current_show_id` unset); `delete_show` deliberately drops ONLY the deleted
show's buffered rows after the delete commits, logging the count
(`drop_buffered_rows_for_show`). Documented residual: a process death in the
milliseconds between the delete commit and the buffer drop can still leave
FK-poisoned rows until restart; a self-healing flush (per-row isolation on
IntegrityError) was considered and rejected as scope creep. Pinned by
`tests/test_llm_capture.py` T5/T5b/T6/T7 + the U4-amended CONC-2 test in
`tests/test_adversarial_leftovers.py`.

### REL-15 [High] YouTube relay gives up permanently after 3 restarts; no watchdog; no auto-arm on boot **[×2 — mixer lane + parent code read]**
`youtube_relay.py:70,344-380` — `_give_up()` unregisters and deactivates
after 3 FFmpeg deaths (a 24 h set surviving 4 brief network blips is dead for
the remaining hours). `reset_restart_budget()` has no production caller; and
`YOUTUBE_STREAM_KEY` is only read when an operator POSTs `/stream/start`
(`routes/youtube.py:88`) — app restart = stream stays down until a human
intervenes.
**Fix:** reset `restarts` to 0 after a stability window (e.g. process alive
≥5 min), converting lifetime-count to a rate limit; add a watchdog calling
`reset_restart_budget()` + restart when inactive (with storm guard); auto-arm
on lifespan startup when the env key is present. (Already an item in
`youtube_247_risk_analysis.md` §3.)

---

## P2 — Medium (schedule; each is bounded but real)

| ID | Finding | Evidence | Fix |
|---|---|---|---|
| REL-16 | No retention for `llm_interactions`/`show_actions`; `session_routing` reaper never built | `cleanup.py` (jobs only), `models/session_routing.py:13-14` | retention deletes in cleanup cycle + `last_heartbeat < NOW()-1d` reaper |
| REL-17 | Job-waiter holds PG conn across full 600 s wait; dead conn undetected until timeout; `pool max_size=10` coupling | `job_waiter.py` | poll event in 5 s slices + `conn.is_closed()` check, or asyncpg connection-loss callback |
| REL-18 | Flat 2 s retry backoff, no escalation/jitter; full LLM call repeated every cycle during DB outage | `loop_orchestrator.py:307-313` | exponential backoff w/ cap; skip conductor call after N submit failures |
| REL-19 | Loop startup failure calls whole-app `trigger_shutdown()` — poisons audience streams/recordings/YouTube relay | `loop_orchestrator.py:436-445` | set `is_running=False` only; reserve the kill switch for process shutdown |
| REL-20 | `sync_lock` held across `instruments.json` disk write — stalls every audio tick | `framework_state.py:367-373` | mutate under lock, write outside |
| REL-21 | No NaN/Inf sanitization: `np.clip` preserves NaN; one bad stem poisons the whole mix for a loop | `aac_encoder.py:52-62`, `framework_mixer.py:375-377` | `np.nan_to_num` in decode/normalize — **fixed-in rel-01-mixer** (`Mixer._sanitize_pcm_block` at both broadcast sites + AAC float branch) |
| REL-22 | Unclean shutdown never finalizes WAV headers (sizes stay 0; show row stays `live`) | `framework_state.py` close path | finalize from file length in shutdown close |
| REL-23 | Worker never evicts models; `GPUMonitor` offload is dead code | `worker.py` (no unload refs) | LRU-evict non-default model when VRAM critical between jobs — **fixed-in rel-03-worker** (`GPUMonitor` wired into `GeneratorRegistry` for load/unload attribution; new `model_last_used` + `lru_eviction_candidates()` give true LRU order; worker evicts between jobs via `_maybe_evict_idle_models`, bounded 30 s so a lock-holding zombie can't stall the loop, graceful no-op without CUDA; pinned by `tests/test_worker_vram.py` E1–E7) |
| REL-24 | Upload runs before lease-ownership check — zombie worker can overwrite completed audio or orphan Garage objects | `worker.py:340-341` | re-check `_still_own_job_row` after generation, before upload |
| REL-25 | Worker hard-codes 44.1 kHz, drops engine sample rate; `generation_steps`/`cfg_scale` never reach the worker (config UI is a silent no-op) | `worker.py:337,344`, `framework_generator.py:408-415` | thread `(array, sr)` through; persist cfg/steps on the job row |
| REL-26 | Icecast module is dead code carrying three 24/7 hazards if ever wired (`is_connected` can never be true, no auto-restart, permanent disable on slow first chunk) | `framework_icecast.py` | fix or delete before wiring |

## P3 — Low / hygiene

- REL-27 `encode_aac` orphans temp WAV if `wavfile.write` raises (disk-full moment); `print` logging throughout `framework_generator.py` — `aac_encoder.py:102-105`
- REL-28 Per-stem audio fetch is strictly serial (~5–15 s/batch) — `loop_steps.py:556-570` → `asyncio.gather`
- REL-29 Per-250 ms debug print in pregen wait; tens of thousands of stdout lines/day — `loop_steps.py` → `logger.debug`
- REL-30 List endpoints accept unbounded `limit` — `jobs.py:180`, `shows.py:246,492,513` (clamp like `reasoning_logs.py:96-98`)
- REL-31 `/api/health` builds a fresh boto3 client per probe; `download_stem` WAV-encodes under `state.lock` — `config.py:60-80`, `stems.py:55-85`
- REL-32 `ShowPlayback` broadcasts any WAV format as s16le (24-bit/48 kHz = noise); WS topic broadcast is sequential (one slow client head-of-line-blocks the topic) — `playback.py:84-97`, `routes/ws.py:66-71`

---

## Verified safe (do not re-litigate)

- **Per-client queues are bounded and drop, never block the mixer** — `put_nowait` with per-client drop (`framework_state.py:453-457`), stream 100 blocks, YouTube 512; pinned by `test_state.py`.
- **Rolling histories all capped** — `stem_history` ≤ 8, `loop_history` ≤ 10 (deep copies), download LRU ≤ 16, `stem_cache` TTL-pruned per loop; steady-state audio memory is large-but-bounded.
- **Lease lifecycle is closed-loop** — claim+lease in one transaction (`FOR UPDATE SKIP LOCKED`), 60 s heartbeat, lease-guarded terminal writes with `pg_notify`, stale-`processing` reaper wired into the worker's own cleanup loop; crashed worker strands a job ≤ ~11 min.
- **ffmpeg hygiene in the AAC path** — `subprocess.run(..., capture_output, check, timeout)`; no shell, no zombies, temp files unlinked on success and error paths.
- **One boto3 client per worker process**, bounded retries/timeouts (`test_io_timeouts.py` pins the wiring).
- **Loop-kill containment (B1)** — every cycle wrapped in try/except→backoff→continue; no path where an LLM timeout, bad JSON, S3 error, or DB blip permanently ends the framework task; waiter's missed-notify race is well covered (pre-check, post-subscribe re-check, final status check).
- **Mixer lock discipline on the audio thread** — playhead advances under `self.lock` in both branches; `snapshot_mixer_state` copies under `sync_lock` (the exceptions are REL-11/REL-20).

---

## Soak-test spec (the 24/7 gate — see `youtube_247_risk_analysis.md` §3)

1. **24 h-equivalent fault-injection soak** (fake conductor/jobs/storage):
   scheduled LLM outage (1 h), PG restart, worker-down window, one stuck
   generation. Assert: loop task alive, `loop_count` monotonic,
   `len(llm_interaction_buffer)` bounded, `asyncio.all_tasks()` count flat,
   `generator_jobs` pending count bounded, RSS plateaus. *Fails today on
   REL-04, REL-12.*
2. **Mixer fault survival** — raise from `_callback` every k-th tick: thread
   survives (or health observably degrades), silence duration bounded. *Zero
   coverage today.*
3. **Reset-then-restart regression** — run ≥2 loops, `should_reset`, re-start;
   assert `current_loop_end_sample > 0` and a transition event fires. *Covers
   REL-02.*
4. **VRAM plateau** — 300 sequential generations; `memory_allocated`/
   `memory_reserved` and thread count return to baseline (±5%). *Catches
   REL-03/07/08 retention.*
5. **Timeout circuit-breaker contract** — two consecutive generation
   timeouts → worker exits non-zero (so Docker restarts). *Fails today by
   design.*
6. **Disconnect churn** — K abrupt `/stream.mp3` client kills: zero zombie
   ffmpeg, `len(state.audio_clients)` == live clients, RSS flat. *Covers
   REL-10.*
7. **Storage reconciliation** — randomized DB outages overlapping uploads +
   `delete_show` calls: zero unreferenced Garage objects, zero orphaned
   recording/export files, on-disk bytes bounded by retention config. *Covers
   REL-05/24.*
8. **Real-session export** — no DB mocks: record N loops, flush, GET both
   export endpoints → complete NDJSON; delete-live-show → next flush
   succeeds. *Covers REL-13/14.*

---

## Recommended remediation sequence

| Batch | Items | Why this order |
|---|---|---|
| 1 — stop the silence | REL-01, REL-02, REL-03 (+REL-07 lock), REL-06 | Every one is a small, local fix; REL-06 also fixes an audible product bug (retain-the-groove churn) |
| 2 — stop the growth | REL-04, REL-05, REL-12, REL-14, REL-16 | All small local fixes + one retention pass; unlocks week-scale uptime |
| 3 — stop the stalls | REL-09, REL-10, REL-11, REL-15 | Server-freeze class + stream leak + YouTube watchdog/auto-arm (Stage-2 24/7 gate) |
| 4 — polish | REL-08, REL-13, P2 remainder, P3 | Schedule freely; each bounded |

Batches 1–3 ≈ the engineering gate for Stage 2 of the 24/7 rollout plan;
the soak harness (spec above) is the acceptance test for the whole pass.
