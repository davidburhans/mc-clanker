# Adversarial Bug Hunt — Round 3 Report

**Scope:** full-repo adversarial hunt (8 lanes) → independent claim verification (every finding
re-proven by execution) → fix swarm (5 lanes, disjoint file ownership) → suite + ruff gates.
**Baseline:** 04791e4 ("bug hunt round 2 leftovers"). **Suite:** 741 passed → **928 passed** (+187,
16 skipped unchanged).

## Method

- 8 hunter lanes (async lifecycle, state/lock, queue/db, audio DSP, HTTP API, external I/O,
  tests, conductor semantics). Rounds 1–2 findings excluded; sibling round-3 reports cross-
  referenced to avoid duplication.
- Every lane's findings independently verified (re-read at HEAD, guards/callers/tests checked,
  bounded proof snippets executed). Verdicts: Verified / Weakened / Falsified per finding.
- 10+ candidate findings **falsified** by verifiers (e.g. "playback/stop IDOR" is correctly
  gated by SEC-5; icecast stderr deadlock and AAC timeout regressions already fixed in round 1).
- Fixes landed as 6 commits on `round3-fixes`, merged fast-forward to `main` (40359aa).
  Every fix ships with regression tests; lanes A and E include RED-proof runs
  (8 failed / 17 failed respectively with fixes reverted).

## Reports

| File | Lane | Findings (verified / weakened) |
|---|---|---|
| 01_async_lifecycle.md | loop + lifespan | 3 V / 1 W |
| 02_state_lock.md | state, mixer, stems | 5 V / 1 W |
| 03_queue_db.md | worker, queue, cleanup, models | 4 V / 1 W |
| 04_audio_dsp.md | mixer DSP, WAV/CUE metadata | 7 V / 1 W |
| 05_http_api.md | routes, auth, uploads | 10 V / 1 F (falsified) |
| 06_external_io.md | S3, icecast, onboarding, conductor IO | 5 V (all reproduced) |
| 08_conductor.md | conductor parse/actions | 6 V / 2 V (suspected) |
| 07_tests.md | **not run** — see Follow-ups |

## Fixed (all Verified findings; commit per lane)

- **mixer** b941245: playhead increment under lock (reset no longer clobbered); boundary
  tiling cuts straddling tracks (kills +6 dB self-overlap); `tile_to_loop` truncates over-long
  cached stems; loop-transition tracks stereo-normalized.
- **framework** 796b310: stale-pregen replay always yields + pregen branch re-checks
  `is_generating` (permanent event-loop starvation wedge); staged loop no longer dropped when
  pregen finishes early; job wait 120 s → 600 s + 30 s late-completion grace (silent
  regenerate-every-loop gone); DJ bpm/key override survives pregen commit; explicit-null
  master bpm/key sanitized (no more `None BPM` prompts / NULL job rows); mixer start
  failure fails closed (health stops lying).
- **conductor** 91ddeda: non-dict JSON rejected into the fallback path (uncapped hot-retry
  wedge); unknown/empty actions retain-all instead of clearing the mix; full action-shape
  validation (never raises out of the fallback); remove-wins + truthful audit log;
  unknown `master_key` no longer bricks the conductor.
- **api** a5e8364: live-show delete/archive tears down the recording (permanent 409 wedge);
  ended-show restart records to a fresh path (no truncation); export race closed; stale-row
  stop no longer clears the live flag; POST/DELETE /api/jobs auth-gated; sqlite UUID
  coercion; session heartbeat server_id validated (no attacker 307s); volume bounded
  NaN-safe (mix poisoning); DJ gate fails closed in audience-only deployments + api key
  masked; /stream.mp3 teardown kills ffmpeg and unregisters the client; framework task
  failure surfaces in /api/health; .env route rejects line-break/NUL values; >4 GiB RIFF
  sentinels.
- **ops** 233abdd + 40359aa: orphan-audio delete re-checks lease ownership; expired jobs
  delete S3 objects **before** rows (orphaned-object classes closed); session_routing model
  aligned with migration 001 (UUID/TIMESTAMPTZ/indexes); icecast `_running` cleared on
  ffmpeg death; `restart_services` bounded (15 s); SIGTERM-clean shutdown via
  `asyncio.Event`; one-shot pool `command_timeout`; recording_metadata: destructive vestigial
  block removed, >4 GiB guards, CUE escaping, proper adtl sub-chunk headers;
  `write_env_file` rejects line-break/NUL values (writer-side guard behind the route 422).

## Environmental finding

`app/data/mc_clanker.db` was a stale sqlite artifact (pre-lease-migration schema). It never
bit before because POST /api/jobs always failed on UUID binding *before* reaching SQL; the
D6 fix made inserts execute and exposed the drift. Backed up to /tmp and regenerated;
`create_all` then produced the correct schema. Long-term: migrations should run on the
sqlite dev path too (round-1 finding M-something: "migrations never run in prod" — still open
for the sqlite fallback).

## Follow-ups

1. **07_tests lane never ran** (two timeout losses, then provider outage). Re-run the
   test-quality hunt (assertions that can't fail, mock-the-target tests, flaky tests) —
   ideally on glm-5.3-flash after the 2026-09-12 quota reset.
2. Weakened/latent items deliberately not fixed: `worker.py` 44.1 kHz hardcode (no non-44.1k
   model shipped), `state_slices` write-through (no production callers), unawaited
   `_pregen_task.cancel()` hygiene.
3. `framework_state._load_instruments` malformed-JSON fake family (LOW) — file owned by the
   in-flight YouTube WIP at fix time; revisit after it lands.
4. Flaky `tests/test_youtube_relay.py` lifecycle tests (other session's active WIP) — not
   touched per one-writer-per-file discipline.

## Ops notes

- `zai/glm-5.3-flash` (the only remote subagent model) burned its request quota during the
  parallel first wave and was cache-excluded until 2026-09-12T02:19Z; lanes were completed on
  `llama-local/qwen-3.8-125b-q4` (sequential, per-child 30–45 min caps, 20 s proof ceilings).
- A concurrent YouTube Live feature (uncommitted) shared the main checkout throughout; all
  fixes were developed in an isolated worktree (`round3-fixes` off 04791e4) and merged
  file-disjoint, leaving the WIP untouched.
