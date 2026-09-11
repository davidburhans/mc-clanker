# REL Remediation Plan — executing docs/reliability_audit.md

Drives all 32 findings from `docs/reliability_audit.md` through test-first
subagent pipelines (scout → plan → TDD red → implement → docs → review↔fix),
one unit per branch, parent-landed on `main` (Conventional Commits).

## Commands (verified green at baseline e125167)

- Preflight gate: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q`
- Baseline: 928 passed, 16 skipped, ~7 s. Do not regress skips without cause.

## Invariants (every unit must respect)

1. `state.lock` discipline (CLAUDE.md): lock sections short, no I/O under lock,
   never call framework functions while holding it. `sync_lock` likewise.
2. Hexagonal boundaries: ports in `app/framework/ports.py`; domain logic
   testable with fakes. AGENTS.md style rules apply (functions 4–20 lines,
   files < 500 lines, no `Any`, early returns, regression test per bug fix).
3. The per-tick mixer core is timing-critical: nothing blocking or
   disk-bound may run on the audio thread (the whole point of REL-11/20).
4. **LLM capture is product-critical** (user directive): conductor
   prompt/response/raw_json are the fine-tuning corpus (see `training/`,
   `tests/test_dpo_pipeline.py`). No unit may silently drop, truncate, or
   delete `llm_interactions` data. Retention for those tables is opt-in
   config, default keep-forever. Exports must round-trip losslessly.
5. Docker restart is the designed recovery for a wedged CUDA context
   (REL-03): a worker that exits non-zero after consecutive timeouts is
   correct behavior, not a bug.
6. Every fix lands with a regression test pinning it (audit soak spec is the
   final acceptance gate, unit 15).

## Unit queue

Ordered by the audit's remediation sequence (stop the silence → stop the
growth → stop the stalls → polish). Each unit cites audit IDs; the audit doc
is the authoritative finding text.

| # | Unit | Findings | Branch |
|---|------|----------|--------|
| 1 | rel-mixer-resilience | REL-01, REL-21 | `rel-01-mixer` |
| 2 | rel-reset-reprime | REL-02, REL-06 | `rel-02-reset` |
| 3 | rel-worker-vram | REL-03, REL-07, REL-08, REL-23 | `rel-03-worker` |
| 4 | rel-llm-capture | REL-04, REL-14, + DPO field audit | `rel-04-capture` |
| 5 | rel-storage-retention | REL-05, REL-16 | `rel-05-storage` |
| 6 | rel-job-queue | REL-12 | `rel-12-queue` |
| 7 | rel-db-offloop | REL-09 | `rel-09-db` |
| 8 | rel-stream-fanout | REL-10 | `rel-10-stream` |
| 9 | rel-recording-writer | REL-11, REL-20, REL-22 | `rel-11-recwriter` |
| 10 | rel-youtube-247 | REL-15 | `rel-15-youtube` |
| 11 | rel-exports | REL-13, REL-30 | `rel-13-exports` |
| 12 | rel-loop-robustness | REL-17, REL-18, REL-19 | `rel-17-19-loop` |
| 13 | rel-worker-correctness | REL-24, REL-25 | `rel-24-25-worker` |
| 14 | rel-p3-hygiene | REL-26 (delete), REL-27–32 | `rel-26-32-p3` |
| 15 | rel-soak-harness | audit §Soak-test spec (8 points) | `rel-soak` |

### Unit specs

**U1 rel-mixer-resilience** — REL-01: `_stream_loop` calls `_callback` with no
exception guard; one exception permanently kills audio while flags stay green.
Fix: wrap the `_callback` call — `except Exception: log.exception();
outdata.fill(0); continue` — and surface mixer-thread liveness
(`is_alive()`) in `/api/health` so degradation is observable. REL-21: add
`np.nan_to_num` sanitization in the mixer normalize path and AAC decode path
so one bad stem can't NaN-poison a whole loop (np.clip preserves NaN).
Acceptance: test that a raising `_callback` survives N ticks and emits zeros
afterwards; test NaN-containing stem buffers produce finite output; health
payload includes thread liveness.

**U2 rel-reset-reprime** — REL-02: reset after loop ≥ 2 zeroes
`current_loop_end_sample` via `mixer.clear()` and nothing ever re-primes
(only the loop-1 path calls `prime_loop`), so transitions stay gated off
forever = permanent silence. Fix: when committing after a reset with
`mixer.current_loop_end_sample == 0`, force the `prime_loop` path (e.g.
`_loop_idx = 0`). REL-06: cache HIT path never refreshes `last_used`, so any
stem retained ≥ 300 s is evicted+regenerated (audible churn of the "core
groove", every 5 min, forever). Fix: refresh `last_used` on hit (main loop +
pregeneration hit path); consider an entry cap in addition to TTL.
Acceptance: reset-then-restart regression (audit soak #3): run ≥ 2 loops,
trigger `should_reset`, re-start, assert `current_loop_end_sample > 0` and a
transition fires; cache-hit test asserts `last_used` advanced and no eviction
of a retained stem.

**U3 rel-worker-vram** — REL-03: generation timeout abandons a
non-killable thread holding VRAM/hf locks; no circuit breaker; cold-cache
model download can never fit the 600 s window. Fix: (a) pre-download enabled
models in worker `start()` outside the timeout window; (b) on the 2nd
consecutive generation timeout, structured-log and `os._exit(1)` — Docker
restart is the designed recovery. REL-07: no concurrency guard in
`GeneratorRegistry` — add a `threading.Lock` serializing
`load_model`/`generate_batch`. REL-08: `load_file(device=gpu)` +
`load_state_dict` + `.to(device)` holds ~2× model VRAM — load on CPU, `del`
state_dict, single `.to(device)`. REL-23: never-evicted models — LRU-evict
non-default models between jobs when VRAM is critical (wire or remove the
dead `GPUMonitor` offload path). Acceptance: timeout circuit-breaker contract
(audit soak #5): two consecutive timeouts → non-zero exit; load path test
asserting CPU-first load (mock `load_file` captures device arg); registry
lock test (concurrent generate calls serialize).

**U4 rel-llm-capture** — VERIFIED: REL-04 fully holds; REL-14 Weakened: the
FK-poison loop is unreachable because `start_show` clears both buffers
first — but that clearing *silently discards* captured rows, which violates
invariant 4 (training-data loss). REL-04 fix as planned: periodic flush from
`_step_post_commit` when `len(state.llm_interaction_buffer) > 200`
(lock-serialized `AuditAdapter.flush` exists); keep stop-flush; flush in
lifespan shutdown. REL-14 fix, amended to the real bug: on `start_show`,
flush pending buffers BEFORE clearing (never silently discard); on
`delete_show` teardown, deliberately drop buffered rows for the deleted
`show_id` (or flush-then-delete) and log the count. PLUS (user
directive): audit the capture schema against what the DPO pipeline consumes
(`training/dpo_pipeline.py`, `training/convert_to_unsloth_dataset.py`,
`tests/test_dpo_pipeline.py`) — if fields the fine-tuning needs are missing
(e.g. loop context, bpm/key, actions actually applied vs requested), add
them to the LLMInteraction write path. Acceptance: buffer stays bounded
across N loops; crash-mid-show loses at most the unflushed tail; delete of a
live show followed by more loops + flush succeeds; DPO export contains every
captured field.

**U5 rel-storage-retention** — REL-05: recordings grow 635 MB/hr;
`delete_show` never unlinks `audio_file_path` or stamped takes; exports have
no retention; ENOSPC corrupts silently (write failure logged once, recording
"continues"). Fix: unlink files in `delete_show`; retention pass in
`JobExpirationCleanup` for recordings/exports (configurable max-age);
recording-write failures surface in health + stop the recording cleanly.
REL-16: retention for `llm_interactions`/`show_actions` — **opt-in config,
default keep** (invariant 4); `session_routing` reaper: delete sessions with
`last_heartbeat < NOW()-1d`. Acceptance: delete_show leaves zero orphan
files; retention pass removes only expired recordings/exports; session
reaper only touches stale sessions; llm_interactions untouched by default.

**U6 rel-job-queue** — REL-12: abandoned jobs are immortal (loop moves on,
row stays pending; worker's FIFO claim still processes them; no `expires_at`
filter; cleanup only deletes terminal rows). Fix: after the grace pass,
terminal-abandon losers (`UPDATE ... SET status='failed',
error_message='loop_abandoned'`); extend the reaper to fail stale `pending`
rows past `expires_at`; throttle submission when observed drain rate falls
behind (bounded queue depth). Acceptance: expired-batch rows end `failed`
with the marker; stale-pending reaper test; submission throttle engages when
queue depth exceeds the bound.

**U7 rel-db-offloop** — REL-09: sync SQLAlchemy on the event loop, engine
without `pool_pre_ping`/`pool_recycle`/timeouts; middleware queries per
request. Fix: `pool_pre_ping=True, pool_recycle=1800, connect_args=
{"connect_timeout": 5, "options": "-c statement_timeout=10000"}` (PG branch
only — keep SQLite fallback working); move middleware + hot-route DB work
off-loop via `asyncio.to_thread` (pattern: `config.py:_ping_database`).
Acceptance: engine config unit test; middleware does no blocking DB call on
the loop (test with a slow fake session asserts event loop not starved);
suite green on SQLite fallback.

**U8 rel-stream-fanout** — REL-10: each `/stream.mp3` client owns an ffmpeg
transcode + feeder thread; abrupt disconnect leaks the subprocess + threads +
queue. Fix (audit-preferred): transcode once in a process-wide singleton and
fan out MP3 bytes to per-client bounded queues (clients own no subprocess);
drop-oldest per client; singleton tears down when last client leaves. If the
singleton proves too invasive for one unit, land the interim watchdog
(kill ffmpeg whose client queue has been full > N s) and note the follow-up.
Acceptance: K abrupt client kills leave zero zombie ffmpeg, zero leaked
client queues, RSS flat (audit soak #6); live stream still serves bytes.

**U9 rel-recording-writer** — REL-11: `handle.write(pcm_data)` inline in
`broadcast_audio` on the real-time thread. Fix: bounded-queue + dedicated
writer thread per recording sink (mirror `YouTubeRelay`), drop-oldest with a
dropped-bytes counter. REL-20: `sync_lock` held across the
`instruments.json` disk write — mutate under lock, write outside. REL-22:
unclean shutdown never finalizes WAV headers (sizes stay 0; show stays
`live`) — finalize from file length in the shutdown close path.
Acceptance: recording-write stall test does not delay mixer ticks (tick
timing asserted); instruments.json write happens outside the lock; killed
process leaves a finalized WAV with correct sizes + show row not `live`.

**U10 rel-youtube-247** — REL-15: relay gives up permanently after 3 restarts;
`reset_restart_budget()` has no caller; no auto-arm from env on boot. Fix:
reset `restarts` to 0 after a stability window (alive ≥ 5 min) — rate limit,
not lifetime count; watchdog restarts when inactive (storm-guarded); auto-arm
on lifespan startup when `YOUTUBE_STREAM_KEY` is present (mask key in all
logs/responses). Acceptance: 4 blips within budget survive; budget resets
after stability; boot with env key arms the relay; boot without key does
nothing.

**U11 rel-exports** — REL-13: export/stats/timeline endpoints load entire
tables (`.all()`); export generators iterate ORM instances after session
commit/expiry → `DetachedInstanceError` on real sessions = broken
fine-tuning extraction. Fix: serialize rows to dicts inside the session;
`yield_per`/keyset pagination under a fresh session per chunk; SQL
`GROUP BY` for stats/timeline. REL-30: clamp `limit` params on list
endpoints (mirror `reasoning_logs.py:96-98`) — `jobs.py`, `shows.py`.
Acceptance: real-session export (no DB mocks) yields complete NDJSON (audit
soak #8); memory-bounded pagination test; limit clamps enforced.

**U12 rel-loop-robustness** — REL-17: job-waiter holds a PG conn across the
full 600 s wait — poll in ~5 s slices + `conn.is_closed()` check (or asyncpg
connection-loss callback). REL-18: flat 2 s backoff — exponential with cap +
jitter; skip the conductor call after N consecutive submit failures during a
DB outage. REL-19: loop startup failure calls whole-app `trigger_shutdown()`
— set `is_running=False` only; reserve the kill switch for process shutdown
(a musical failure must not kill audience streams/recordings/YouTube).
Acceptance: waiter detects dead conn within one slice; backoff sequence
asserted; startup failure leaves app + relay alive.

**U13 rel-worker-correctness** — REL-24: upload runs before lease-ownership
recheck — re-check `_still_own_job_row` after generation, before upload
(zombie must not overwrite completed audio / orphan Garage objects).
REL-25: worker hard-codes 44.1 kHz and drops the engine sample rate;
`generation_steps`/`cfg_scale` never reach the worker (config UI silent
no-op). Fix: thread `(array, sr)` through; persist cfg/steps on the job row
at submit; worker reads them. Acceptance: ownership-recheck test; job row
carries cfg/steps end-to-end; non-44.1 kHz engine output resampled (or
stored) at its native rate.

**U14 rel-p3-hygiene** — REL-26: DELETE `app/framework/framework_icecast.py`
+ its tests (dead code carrying 3 hazards; revivable from git if ever
wired) — remove any dangling references. REL-27: `encode_aac` temp-WAV
orphan on `wavfile.write` raise (unlink in finally); `print` logging in
`framework_generator.py` → `logger`. REL-28: parallel per-stem audio fetch
via `asyncio.gather` (bounded). REL-29: per-250 ms pregen-wait print →
`logger.debug`. REL-31: `/api/health` reuses a cached boto3 client;
`download_stem` WAV-encodes outside `state.lock` (copy under lock).
REL-32: `ShowPlayback` detects WAV format (24-bit/48 kHz ≠ s16le noise —
decode via soundfile/wave to float then broadcast); WS topic broadcast no
longer head-of-line-blocks on one slow client. Acceptance per item: focused
unit tests; icecast refs gone (grep clean); suite green.

**U15 rel-soak-harness** — the audit §Soak-test spec, points 1–8, as an
opt-in test module (`tests/test_soak_247.py`, marked `soak`, skipped in
normal runs): fault-injection soak with fake conductor/jobs/storage
(scheduled LLM outage, PG restart, worker-down window, stuck generation);
mixer fault survival; reset regression; VRAM plateau; timeout
circuit-breaker; disconnect churn; storage reconciliation; real-session
export. Assertions per the audit: loop task alive, `loop_count` monotonic,
buffer bounded, task count flat, pending bounded, RSS plateaus, zero zombie
ffmpeg, zero orphan objects. Acceptance: `pytest -m soak` runs the suite
against fakes; documented how to run it.

## Status tracking

| Unit | Status | Commit |
|------|--------|--------|
| baseline | landed | e125167 |
| 1 rel-mixer-resilience | **landed** (939p/16s green; reviewer safe-to-land, 0 blockers) | 46e9d13 |
| 2 rel-reset-reprime | **landed** (949p/16s green; reviewer safe-to-land, 0 blockers) | eda6ede |
| 3 rel-worker-vram | **landed** (968p/16s green; reviewer safe-to-land, 0 blockers) | e28eee7 |
| 4 rel-llm-capture | **landed** (987p/16s green; land-with-fixes applied: doc drift; DPO contract pinned) | 9268e69 |
| 5 rel-storage-retention | **landed** (1013p/16s green after round-1 fixes; invariant 4 preserved) | b53a981 |
| 6 rel-job-queue | **landed** (1032p/16s green; pending-only abandon + reaper + depth throttle; claim SQL untouched) | b0e794c |
| 7 rel-db-offloop | **landed** (1047p/16s green; dialect-gated PG engine resilience; middleware + audio-route DB off-loop via to_thread; SQLite fallback byte-identical; T6 fast-subcase test corrected to its documented Basic-auth intent) |  |
| 8 rel-stream-fanout | pending | — |
| 9 rel-recording-writer | pending | — |
| 10 rel-youtube-247 | pending | — |
| 11 rel-exports | pending | — |
| 12 rel-loop-robustness | pending | — |
| 13 rel-worker-correctness | pending | — |
| 14 rel-p3-hygiene | pending | — |
| 15 rel-soak-harness | pending | — |

## Follow-ups (from unit reviews)

- rel-01: guard log is tick-cadence-bounded (~21.7 lines/s under persistent
  failure) but not rate-limited — consider a consecutive-failure counter in
  /api/health during U15 soak. routes/stems.py:86 third clip site feeds only
  sanitized decode output now — fold into U14/REL-31 touch.

- rel-02 (P2, report-only): post-reset numbering revisits indices, so a stale
  in-flight pregen result for a pre-reset loop M can be accepted once (one
  loop of stale audio, self-heals; no silence/crash/leak). Clean fix later:
  monotonic generation/epoch counter on pregen results instead of loop_idx.
  Also: post-reset audit rows keep pre-force loop_index → duplicates within
  a show (cosmetic, disclosed).

- rel-03 (P2, report-only): worker.py now 633 lines (pre-existing >500
  brownfield debt extended; split deferred). Suggestion backlog: expose
  consecutive_generation_timeouts in worker /health (not just /stats); pin
  the timeout->non-timeout-failure->timeout breaker branch with a test;
  py3.11+ note — builtin TimeoutError aliases asyncio.TimeoutError so a
  socket/upload timeout would also feed the breaker (worker pins 3.10 today).

- rel-04 (P2, report-only): during a sustained DB outage, audit buffers grow
  without bound (retain-over-drop is the invariant-4-correct choice) —
  surface a failed-flush/backlog counter in /api/health during U15.

## Decisions log

- 2026-09-11: independent claim-verification pass (2 read-only agents, all 32
  findings): 31 Verified, REL-14 Weakened — spec amended in U4 (silent
discard on start_show is the real bug, worse given invariant 4).
- 2026-09-11: scope = everything P0–P3 + soak harness (user).
- 2026-09-11: baseline commit of in-flight YouTube work first (user).
- 2026-09-11: LLM capture durability/losslessness promoted to invariant 4
  (user directive: fine-tuning corpus must be complete).
- 2026-09-11: REL-26 icecast → delete, not fix (dead code, git-revivable).
- 2026-09-11: implementation stages run on GLM-5.3-Flash (user, token
  economy); scout/planner/reviewer stay on the session model.
