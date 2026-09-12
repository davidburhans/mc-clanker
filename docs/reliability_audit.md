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

**Follow-up (P2, report-only → fixed-in rel-fu-2):** post-reset numbering
revisits indices, so a stale in-flight pregen result for a pre-reset loop M
was still accepted once (one loop of stale audio, self-healing). Pregen
results now carry a SPAWN-TIME `pregen_epoch` stamp (the P11 snapshot rides
it into `run_pregeneration`; the loop-1 fabrication stamps directly), P3
bumps the monotonic `loop._pregen_epoch` on every `should_reset` consumption
(never back to 0), and P2 requires epoch equality — the stale-result
acceptance window is closed (pinned by `tests/test_loop_epoch_recovery.py`
E1–E4). The cosmetic post-reset `loop_index` duplicates in the show audit
remain disclosed (audit/commit semantics intentionally keep using `loop_idx`;
the epoch is stale-detection only).

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

**Status: fixed-in rel-05-storage** — `delete_show` now retires any live
playback, then unlinks the persisted take AND sweeps `shows/{id}/` (stamped/
uuid takes were reachable by nothing; per-file OSError isolation; pinned by
`tests/test_storage_retention.py` T1–T4). Retention passes (mtime-based,
config-gated) live in `app/retention.py` behind `JobExpirationCleanup`: show
recordings (`SHOW_AUDIO_RETENTION_DAYS`, 14 d in compose) and exports
(`EXPORT_RETENTION_DAYS`, 7 d in compose) — bare-env default OFF so upgrades
delete nothing unasked; a dedicated compose `cleanup` service runs them
decoupled from worker liveness (invariant 5). ENOSPC no longer "continues
recording": `_write_recording_sink` counts consecutive failures per sink
(`state.recording_write_errors`, surfaced in `/api/health` → `recording.*`)
and auto-stops the sink cleanly past `RECORDING_WRITE_FAILURE_STOP_THRESHOLD =
32` ticks (~1.5 s), setting `recording_stop_reasons[sink] =
"write_failure_threshold"`; the show slot keeps `current_show_id` so the
corpus keeps capturing (invariant 4). One success resets the consecutive
counter, so transient hiccups never stack into a stop (F4).

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

**Status: fixed-in rel-09-db** — the engine kwargs landed on the PostgreSQL
branch only, gated by dialect (`make_url(url).get_backend_name() ==
"postgresql"`, not "DATABASE_URL set" — tests deploy SQLite via DATABASE_URL;
libpq-only `connect_args` would `TypeError` there). An explicit non-PG
DATABASE_URL is honored verbatim plus `check_same_thread=False` (REL-09 runs
DB access in `asyncio.to_thread` workers); the no-DATABASE_URL SQLite fallback
is byte-identical. Budgets live as module constants in `app/db.py`
(`DB_CONNECT_TIMEOUT_SECONDS`/`DB_STATEMENT_TIMEOUT_MS`/`DB_POOL_RECYCLE_SECONDS`,
monkeypatchable, deliberately not env knobs). The three per-request middleware
queries (Bearer user lookup, per-show audience gate, session-routing lookup)
moved into sync helpers in `app/middleware_db.py`, awaited via
`asyncio.to_thread`; `GET /api/shows/{id}/audio` gates+stats off-loop the same
way (FileResponse still streams from Starlette off-loop). Failure semantics
preserved: AuthMiddleware DB errors propagate (500), SessionAffinityMiddleware
stays fail-open with a WARNING log (its `print`s became logger calls).
Documented residuals: the remaining `async def` route bodies with sync DB
(shows CRUD/list/actions, reasoning-logs exports, jobs, config writes,
playback control) stay cold-path on the loop — the full async-ORM migration
is the spec-declared follow-up (U11 also owns paginating the export queries,
which otherwise become `QueryCanceled` victims of the new engine-wide 10 s
statement_timeout on large corpora); DB-error → 503 mapping in auth middleware
was considered and rejected as a semantic change without a spec mandate.
Pinned by `tests/test_db.py::TestEngineResilienceRel09` and
`tests/test_db_offloop.py` (slow-fake + heartbeat loop-starvation asserts).

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

**Status: fixed-in rel-10-stream** — the full fix landed, not the interim
watchdog. One shared ffmpeg lives in a process-wide singleton on
`state.stream_fanout` (`app/stream_fanout.py`; launch contract in
`stream_fanout_args.py`, subprocess lifecycle in
`stream_fanout_proc.TranscoderSupervisor`); clients get bounded queues of the
cleaned MP3 bytes (drop-oldest) and own no subprocess — the per-client
generator path is gone from `app_ui.py`. Cleanup no longer depends on the
abandoned generator's `finally`: the fan-out pump is the single writer of each
session's fullness clock and evicts any client whose queue stayed full
>`stale_client_s` (default 10 s), poisoning the parked frame with a stop
sentinel — abandoned generators are reaped, not parked forever, so the leak
is closed regardless of Starlette's threadpool behavior. Subprocess hard rule:
terminate-before-join escalation (stdin EOF → wait → kill → reap) so no ffmpeg
outlives its owner. Respawn is indefinite with capped linear backoff — no
permanent give-up by design (REL-15's lesson); the loop is bounded because the
singleton only exists while clients do (last release tears it down and retires
the slot; deliberately NOT cleared by `reset()` — a musical reset must not
kill the audience stream). The argv is byte-identical to the legacy per-client
transcoder (`build_mp3_args`, pinned by test); spawn failure serves an empty
stream, never a hang. `FanoutStatus` telemetry exists on the singleton for
tests/soak introspection — deliberately no new endpoint (scope discipline).
Documented residual: a slow-but-alive consumer that falls behind with zero
consumption is evicted once its queue stays full past the staleness window —
deliberate live-latency bound; it reconnects. Adjacent relay `None`-poison
`TypeError` (`YouTubeRelay._write_block`) discovered during this unit is out
of scope — noted as a U10 follow-up. Pinned by `tests/test_stream_fanout.py`.

### REL-11 [High] Recording file writes run on the real-time mixer thread
`framework_state.py:460-479` — `handle.write(pcm_data)` inline in
`broadcast_audio`, called every ~46 ms tick. Any disk stall (page-cache
flush, cgroup IO throttle, ENOSPC-adjacent slowness) delays **every** tick
for **all** listeners — periodic dropouts that correlate with long recording
sessions.
**Fix:** bounded-queue + dedicated writer thread per sink (mirror
`YouTubeRelay`), drop-oldest with a counter.
**Status: fixed-in rel-11-recwriter** — `app/framework/recording_sink.py:
RecordingSink`: the state slots (`current_show_sink`/`export_sink`) hold
writer-thread sink objects; `broadcast_audio` only snapshots them under
`sync_lock` and `put_nowait()`s outside it (256-block queue ≈ 12 s grace,
drop-oldest, per-sink `dropped_bytes` surfaced in `/api/health`). The writer
thread owns the handle end-to-end and is the ONLY thread that finalizes it
(single-owner finalize: drain → flush → finalize; a timed-out stop defers to
the writer, so two threads can never seek/write one handle and rel-05 F5's
concurrent-stop race became structural). The rel-05 failure machinery moved
onto the writer (consecutive counter, threshold auto-stop through
`_note_sink_write_failure`/`_detach_failing_sink` hooks; the health dicts
stay the `/api/health` surface). Flush-per-block bounds a crash's userspace
loss to zero and makes on-disk sizes deterministic. Residuals: a sustained
disk stall truncates the recording by design (drop-oldest; the live mix is
never held hostage — surfaced via `dropped_bytes`); a sub-µs
submit/finalize race can leave ≤ 1 block (~46 ms) uncounted per stop. Pinned
by `tests/test_recording_writer.py` (T1–T3, T15–T19) + the re-pinned
`tests/test_recording_fault_stop.py` F1–F7.

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

**Status: fixed-in rel-12-queue** — both submit→await paths (foreground
`_step_await_jobs_fetch` and the pregen mirror) now terminal-abandon their
losers after the grace pass via the new `JobQueuePort.abandon_jobs`
(`error_message='loop_abandoned'`, guarded to `status='pending'` ONLY — a
claimed/running `'processing'` row is never failed; its lease machinery owns
it). `JobExpirationCleanup` gains the error-isolated `_reap_stale_pending`
backstop for rows no loop ever waits on (API submissions, a crashed web
process), knobbed by `PENDING_GRACE_SECONDS` (default 86400 = the audit's
effective 24 h horizon; `0` disables) and predicate on `created_at` rather
than `expires_at` so queue hygiene decouples from the terminal-retention
horizon. Submission backpressure: a `JobQueuePort.pending_depth()` gauge and
`JOB_PENDING_DEPTH_LIMIT = 64` in `loop_steps.py`; over the bound the whole
submit phase skip-and-logs for the cycle (never blocks; prompts stay
cache-missed and retry next loop) and the skipped stems report the documented
`"failed"` applied-actions outcome. The worker's claim SQL is deliberately
unchanged: once abandoned rows are terminal, the existing `status='pending'`
filter already excludes them, and a staleness predicate there would duplicate
the grace knob in a second process. Documented residuals: a stem still
`'processing'` at loop expiry completes late and its audio goes unused
(bounded by the worker's timeout circuit; the C8 `content_hash` in-flight
dedup is the follow-up that collapses the duplicate row); API-submitted rows
older than the grace now come back `failed` (that is the intent;
`PENDING_GRACE_SECONDS=0` restores the old wait-forever behavior). Pinned by
`tests/test_job_queue_lifecycle.py`.

**Follow-up (from rel-17 review, report-only → fixed-in rel-fu-2):** a
read-only PG (hot standby) used to make the REL-18 recovery probe reset the
submit streak while every INSERT still failed — the full conductor LLM call
repeated every cycle in that mode. The streak now resets ONLY on a successful
submit (the `_submit_job` seam); the once-per-loop read probe merely gates a
one-shot WRITE canary through that same seam (best-effort abandoned so the
worker never generates it), and the conductor resumes the same iteration the
canary succeeds (pinned by `tests/test_loop_epoch_recovery.py` O1–O4).

### REL-13 [High] Export/stats/timeline endpoints load entire tables; exports are broken on real sessions
`reasoning_logs.py:153,178,188,275` and `shows.py:646,667` — unbounded
`.all()` (a week-long show ≈ 75 k interactions ≈ hundreds of MB per request);
and because the session commits+expires instances **before** Starlette
iterates the response generator, real (unmocked) exports raise
`DetachedInstanceError` after headers are sent → empty/truncated bodies.
**Fix:** serialize rows to dicts inside the session; `yield_per`/keyset
pagination under a fresh session per chunk; SQL `GROUP BY` for stats/timeline.

**Status: fixed-in rel-13-exports** — export generators serialize every row
through the existing shapers INSIDE each chunk's session (plain dicts cross
session boundaries; detachment impossible by construction, pinned by
real-session tests with zero DB mocks). Scans are keyset-paginated on
`(loop_index, id)` via `app/lib/export_chunks.chunked_shaped_rows` — one
fresh short session per chunk, page-bounded SELECTs (milliseconds, far under
rel-09's engine-wide 10 s statement_timeout), connection returned to the pool
between chunks; NOT `yield_per` (one long-held SELECT would hit the timeout
mid-stream). Stats run as SQL aggregates (`count/avg/min/max`, `sum(case)`
fallbacks, `avg(length(nullif(reasoning,'')))` matching the old
`if i.reasoning:` guard); timeline = one SQL GROUP BY for per-segment
count/avg/action tallies + one column-projected slim scan in
`(relative_time_ms, id)` order for the detail lists (fat prompt/response
columns never load; `instruments_used` is the one JSON-array set-union SQL
cannot do portably — a column-projected scan instead of dialect forks).
`/shows/{id}/export/full` streams its exact JSON document fragment-by-fragment
instead of materializing both tables. Mid-stream chunk failures abort loudly
(raise → Starlette terminates; every already-yielded line is complete NDJSON —
consumers detect truncation by row count as before, no sentinel lines).
Exports stay complete by design (no response limit — the fine-tuning
corpus is never truncated, invariant 4).

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
**Status: fixed-in rel-15-youtube** — all three legs landed plus the rel-10
follow-up None-poison guard. (a) Rate limit: `RelayConfig.stability_window_s`
(default 300 s; `>= 0` validated) — `_spawn_ffmpeg` stamps
`_spawned_at = time.monotonic()`, and the death branch of `_ensure_process`
calls `reset_restart_budget()` (its first production caller) when the dead
proc lived ≥ the window, so `max_restarts` now means "3 rapid deaths", never
"3 deaths ever" (a fresh-budget death still counts itself). (b) 24/7
supervision: new `app/youtube_lifecycle.py` — lifespan asyncio task
`youtube_watchdog_loop` (60 s cadence, exit on `shutdown_event` in ≤0.5 s
slices) re-arms an inactive, non-disarmed relay via `arm_relay_from_state`
(a FRESH relay built from *current* state: fresh counters + a key fixed via
PUT /config heals on the next arm). Storm guard: a give-up before the relay
earned trust (survived `storm_window_s` alive) is a fast failure; 3
consecutive → one `log.error` alert + 15 min idle backoff, then retry — a
bad key yields ~3 arms/15 min with an alert, never a spawn storm. (c)
Auto-arm: `start_relay_services` in the lifespan arms the relay when
`YOUTUBE_STREAM_KEY` is set; every failure path is caught (optional infra
must never kill startup — even a coding bug in auto-arm is logged, and the
watchdog retries). (d) Operator kill switch: `state.youtube_relay_disarmed`
set by POST /stream/stop, cleared by /stream/start and any successful arm;
the watchdog checks it under `state.lock` (stop-vs-arm race closed) and
`reset()` deliberately does not clear it. (e) Shutdown: `stop_relay_services`
cancels the watchdog, detaches the slot under `state.lock` and stops the
relay outside it (graceful stdin EOF → ffmpeg flush beats docker SIGKILL).
(f) `_write_block` returns early on a `None` poison (rel-10 follow-up:
`trigger_shutdown` used to kill the writer thread with a `TypeError` outside
its except tuple). Pinned by `tests/test_youtube_lifecycle.py` (T1–T21:
spaced-blip survival, budget renewal, watchdog re-arm of both inactive
shapes, storm backoff + recovery, auto-arm/no-key/failure-safe, None poison,
disarm toggle, config-heal, key-never-in-logs-or-responses).

---

## P2 — Medium (schedule; each is bounded but real)

| ID | Finding | Evidence | Fix |
|---|---|---|---|
| REL-16 | No retention for `llm_interactions`/`show_actions`; `session_routing` reaper never built | `cleanup.py` (jobs only), `models/session_routing.py:13-14` | retention deletes in cleanup cycle + `last_heartbeat < NOW()-1d` reaper — **fixed-in rel-05-storage** (invariant 4 first: corpus retention is OPT-IN — `LLM_RETENTION_DAYS=0` keeps everything forever and issues zero corpus SQL; enabled, rows are streamed to an fsync'd NDJSON archive in `AUDIT_ARCHIVE_DIR` in the exact `to_llm_dump_dict`/`to_dict` shape (shared pure shapers `llm_dump_row`/`show_action_row`) and only the archived ids are deleted; a failed archive keeps every row). Session reaper defaults ON (`SESSION_STALE_HOURS=24`, `0` disables) with a sargable `make_interval` predicate against `idx_session_routing_heartbeat`; pinned by `tests/test_storage_retention.py` T9–T14 |
| REL-17 | Job-waiter holds PG conn across full 600 s wait; dead conn undetected until timeout; `pool max_size=10` coupling | `job_waiter.py` | poll event in 5 s slices + `conn.is_closed()` check, or asyncpg connection-loss callback — **fixed-in rel-17-19-loop** (`JobWaiter._wait_for_notify` waits in `WAITER_SLICE_SECONDS = 5.0` slices with a per-slice `is_closed()` check, last slice clamped to the deadline; all three exits — notify / deadline / dead conn — funnel into ONE final `_get_job` on a fresh pool conn, so a job completing as its LISTEN conn dies is still honored and the missed-notify race coverage (pre-check + post-subscribe re-check) is untouched; residual: a half-open conn without FIN stays undetected until the OS notices — asyncpg connection-loss callback/TCP keepalives remain the full fix; pinned by `tests/test_job_waiter_slicing.py` W1–W8) |
| REL-18 | Flat 2 s retry backoff, no escalation/jitter; full LLM call repeated every cycle during DB outage | `loop_orchestrator.py:307-313` | exponential backoff w/ cap; skip conductor call after N submit failures — **fixed-in rel-17-19-loop** (`loop_retry_backoff_delay(n)`: the first failure keeps the exact 2 s base, then ×2 with ±25 % uniform jitter capped at 30 s, ladder reset on the next clean pass; a `_consecutive_submit_failures` streak owned by the `_submit_job` delegate (one seam for foreground P7 + pregen) skips the conductor call after 3 consecutive submit failures — the retain-all fallback keeps the set running from cache — while a once-per-loop `pending_depth()` read probe gates a `_submit_job`-seam write canary whose success resets the streak and resumes the conductor within one loop of the DB becoming WRITABLE (FU-2: probe success alone no longer resets anything — a read-only hot standby cannot re-enable the LLM call); the pregen path gets the same gate without a probe; pinned by `tests/test_loop_robustness.py` B1–B6 + `tests/test_loop_epoch_recovery.py` O1–O4) |
| REL-19 | Loop startup failure calls whole-app `trigger_shutdown()` — poisons audience streams/recordings/YouTube relay | `loop_orchestrator.py:436-445` | set `is_running=False` only; reserve the kill switch for process shutdown — **fixed-in rel-17-19-loop** (the startup-failure path flips only `state.is_running` under `sync_lock` and returns — no shutdown event, no poisoned `audio_clients`, no killed subprocesses, no relay harm; `is_generating` untouched (user intent, not liveness); the kill switch remains for lifespan shutdown and for the D11 done-callback, which still runs full cleanup when the task DIES with an exception — boundary pinned by S2 and flagged as a follow-up candidate; pinned by `tests/test_loop_robustness.py` S1–S3) |
| REL-20 | `sync_lock` held across `instruments.json` disk write — stalls every audio tick | `framework_state.py:367-373` | mutate under lock, write outside — **fixed-in rel-11-recwriter** (`save_instruments` snapshots the payload (deepcopy) under `sync_lock` then delegates to `_write_instruments_payload`, which runs outside it serialized by a private `_instruments_io_lock` the audio path never touches — writers can't interleave a torn file and can't lose updates since payloads are snapshotted after mutation under the same lock; `add_custom_instrument` + `add_custom_major_family` also moved outside; pinned by `tests/test_recording_writer.py` T13–T14) |
| REL-21 | No NaN/Inf sanitization: `np.clip` preserves NaN; one bad stem poisons the whole mix for a loop | `aac_encoder.py:52-62`, `framework_mixer.py:375-377` | `np.nan_to_num` in decode/normalize — **fixed-in rel-01-mixer** (`Mixer._sanitize_pcm_block` at both broadcast sites + AAC float branch) |
| REL-22 | Unclean shutdown never finalizes WAV headers (sizes stay 0; show row stays `live`) | `framework_state.py` close path | finalize from file length in shutdown close — **fixed-in rel-11-recwriter** (`trigger_shutdown` detaches both sinks under `sync_lock` via `_detach_recording_sinks_locked` — no handle I/O — then runs `stop_and_finalize()` on each outside the lock: the writers drain, flush and patch the RIFF/data sizes; then `end_live_show_row(show_id)` (recording_sink.py) load-conditionally ends a still-`live` row (naive-UTC `ended_at` per DATA-1, `duration_seconds` from `started_at`, idempotent — a second shutdown or an already-stopped show is a no-op returning False). Shutdown also clears `current_show_id`/`current_show_start_time` + export bookkeeping (the process is ending — contrast the rel-05 auto-stop which keeps the id). DB failure is a logged, non-fatal line (rel-09 engine timeouts bound it). Pinned by `tests/test_recording_writer.py` T10–T12 |
| REL-23 | Worker never evicts models; `GPUMonitor` offload is dead code | `worker.py` (no unload refs) | LRU-evict non-default model when VRAM critical between jobs — **fixed-in rel-03-worker** (`GPUMonitor` wired into `GeneratorRegistry` for load/unload attribution; new `model_last_used` + `lru_eviction_candidates()` give true LRU order; worker evicts between jobs via `_maybe_evict_idle_models`, bounded 30 s so a lock-holding zombie can't stall the loop, graceful no-op without CUDA; pinned by `tests/test_worker_vram.py` E1–E7) |
| REL-24 | Upload runs before lease-ownership check — zombie worker can overwrite completed audio or orphan Garage objects | `worker.py:340-341` | re-check ownership after generation, before upload — **fixed-in rel-24-25-worker** (new strict `_lease_still_held` predicate runs after the generate await and BEFORE encode: row must exist ∧ `status='processing'` ∧ `worker_id` ours — unlike the delete-guard `_still_own_job_row`, a GONE row or an unreadable row here means NO upload (the object would be orphaned with no row left to delete it); a lost lease raises `LostLeaseError` and `_process_claimed_job` stands down — no mark-failed, no `jobs_failed`, no breaker-counter reset; residual ~1 s race inside encode+upload accepted, the row itself stays unclobberable via the guarded `WHERE status='processing' AND worker_id=$n` complete UPDATE; pinned by `tests/test_worker_correctness.py` L1–L7) |
| REL-25 | Worker hard-codes 44.1 kHz, drops engine sample rate; `generation_steps`/`cfg_scale` never reach the worker (config UI is a silent no-op) | `worker.py:337,344`, `framework_generator.py:408-415` | thread `(array, sr)` through; persist cfg/steps on the job row — **fixed-in rel-24-25-worker** (`generate_stem` returns `(audio, engine_sample_rate)`; the worker normalizes ONCE to `MIXER_SAMPLE_RATE = 44100` via `resample_poly` — the whole playback chain (fetch-decode, mixer, fan-out, relay) is hard-coded 44.1 kHz and nothing resamples, so worker-side normalization keeps the "stored AAC is 44.1 kHz" invariant actually true; 44.1 kHz output passes through by identity; NULLable `generator_jobs.cfg_scale/steps` columns (`migrations/004_generation_params.sql`) are threaded from `state` through `JobQueuePort.submit`/both submit paths + `POST /api/jobs` (same SEC-1 bounds as `GenerationConfig`) to the worker, which falls back to 7.0/50 on NULL/absent — `cfg_scale=0.0` passes uncoerced; pinned by `tests/test_worker_correctness.py` S1–S5, `tests/test_job_queue_params.py` C1–C6, `tests/test_generator.py` G1–G2) |
| REL-26 | Icecast module is dead code carrying three 24/7 hazards if ever wired (`is_connected` can never be true, no auto-restart, permanent disable on slow first chunk) | `framework_icecast.py` | fix or delete before wiring — **fixed-in rel-26-32-p3** (DELETED per the 2026-09-11 decision log, not fixed: module (373 L) + `tests/test_icecast.py` + every live-surface reference removed in one commit — `app/`, `static/`, `tests/`, `docker/compose.yaml`, README, `.env.example`, `state.icecast_enabled` and the `icecast_enabled` key in the llm-config GET/POST; dead code is git-revivable; `docs/` + `refactor/` keep the historical record; grep-pinned by `tests/test_p3_hygiene.py` T1) |

## P3 — Low / hygiene

- REL-27 `encode_aac` orphans temp WAV if `wavfile.write` raises (disk-full moment); `print` logging throughout `framework_generator.py` — `aac_encoder.py:102-105`
  **Status: fixed-in rel-26-32-p3** — `wavfile.write` moved inside the
  existing `try` so the `finally` unlink covers a disk-full write
  (REL-27a, pinned `tests/test_p3_hygiene.py` T2–T3); all 14
  `framework_generator.py` prints became a module logger with
  info/warning/error mapped per site (REL-27b, AST no-print pin T4).
- REL-28 Per-stem audio fetch is strictly serial (~5–15 s/batch) — `loop_steps.py:556-570` → `asyncio.gather`
  **Status: fixed-in rel-26-32-p3** — shared bounded `gather_stem_audio`
  (`STEM_FETCH_CONCURRENCY = 4`, order-preserving, per-call semaphore)
  used by BOTH the foreground results loop and the pregen mirror; cache
  writes and the `state.cache_stem` foreground-only divergence stay in
  the callers; pinned by `tests/test_stem_fetch_concurrency.py`.
- REL-29 Per-250 ms debug print in pregen wait; tens of thousands of stdout lines/day — `loop_steps.py` → `logger.debug`
  **Status: fixed-in rel-26-32-p3** — the per-250 ms wait line is now
  `log.debug` and the once-per-loop completion line `log.info`
  (`loop_steps` gained a module logger); no stdout on the wait path,
  pinned by `tests/test_p3_hygiene.py` T6.
- REL-30 List endpoints accept unbounded `limit` — `jobs.py:180`, `shows.py:246,492,513` (clamp like `reasoning_logs.py:96-98`)
  **Status: fixed-in rel-13-exports** — `Query(ge=, le=)` clamps on
  `/api/jobs` (50/500), `/api/shows` (50/500), and the per-show
  `/actions` + `/llm-interactions` (default 1000 kept for the viewer,
  clamp 5000); out-of-range values now 422 like the reasoning-logs search
  route (client-visible contract change, intended).
- REL-31 `/api/health` builds a fresh boto3 client per probe; `download_stem` WAV-encodes under `state.lock` — `config.py:60-80`, `stems.py:55-85`
  **Status: fixed-in rel-26-32-p3** — `/api/health` caches one probe
  client keyed on the GARAGE_* env fingerprint (`threading.Lock` — the
  probe runs via `asyncio.to_thread`), rebuilt automatically when env
  changes; shares only the pure botocore-Config builder, never the
  storage adapter (REL-31a, pinned T7–T9); `download_stem` copies the
  audio under `state.lock` and encodes the WAV response outside it
  (`_encode_wav_response`, REL-31b).
- REL-32 `ShowPlayback` broadcasts any WAV format as s16le (24-bit/48 kHz = noise); WS topic broadcast is sequential (one slow client head-of-line-blocks the topic) — `playback.py:84-97`, `routes/ws.py:66-71`
  **Status: fixed-in rel-26-32-p3** — `ShowPlayback` normalizes every
  broadcast chunk to s16le via `wav_chunk_to_s16le` (sampwidth 2 identity
  fast path; 8/24/32-bit decoded through float32 with the REL-21
  NaN-safety; non-PCM/float WAVs that stdlib `wave` rejects at open fall
  back to a scipy whole-file streamer; sample-RATE mismatch remains out
  of scope) (REL-32a, pinned by `tests/test_playback_format.py`); WS
  topic broadcast sends concurrently, each send bounded by
  `WS_SEND_TIMEOUT_SECONDS = 5.0`, stale/timed-out subscribers dropped
  like broken sockets (REL-32b, pinned by `tests/test_websocket.py`).

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

> **Implemented — rel-soak (U15).** The harness lives in `tests/test_soak_247.py`
> (point 1) plus the sibling soak files (`test_soak_mixer.py`,
> `test_soak_worker.py`, `test_soak_stream.py`, `test_soak_storage_export.py`,
> shared clock/gate/profile logic in `tests/soak_helpers.py`) — one test per
> point 1–8, fakes/real SQLite only (no GPU/Postgres/ffmpeg/LLM). Run:
> `SOAK=1 pytest -m soak` (fast profile, ~3 s) or `SOAK=1 SOAK_PROFILE=full
> pytest -m soak` (audit-literal 24 h-equivalent schedule, ~10 s); without
> `SOAK=1` the soak tests skip by default. Point mapping and caveats:
> `docs/soak_harness.md`. The per-point "*fails today*" notes below are the
> pre-remediation audit findings — units 1–14 are landed, so the harness is
> expected green and now stands as the standing regression gate.

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
