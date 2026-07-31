# Adversarial Review + Fix — Final Report

**Codebase:** mc-clanker (FastAPI AI music generator). **Mode:** orchestrator-managed
multi-agent adversarial review (7 lenses) → synthesized findings → fix swarms → verification.
**Security lens excluded per owner** (local-only project).

---

## 1. Review phase — 7 adversarial agents, parallel

Reports in `adversarial_review/0[1-7]_*.md`; synthesis in `00_SYNTHESIS.md`.
**~50 non-security findings** across: concurrency (12), reliability (14), data-integrity (9),
test-quality (13), code-quality (13), ops/build (9).

Top-severity (non-security) findings flagged:

| | Finding |
| --- | --- |
| 🔴 | Two-lock mismatch (`asyncio.Lock` vs `threading.Lock`) on recording handles → silent audio loss |
| 🔴 | Mixer reads `muted/soloed/volumes` with **no lock** every callback tick |
| 🔴 | Blocking `threading.Lock` acquired inside `asyncio.Lock` → event-loop stall / AB-BA risk |
| 🔴 | No watchdog — one exception in `_run_loop` kills music permanently |
| 🔴 | Job dead-letter — crashed worker orphans jobs in `processing` forever (no lease) |
| 🔴 | S3/Garage ops with **no timeout** → indefinite hangs |
| 🔴 | Show audit data **never persisted** (buffers flushed but never appended to) |
| 🔴 | Migrations **never run in prod**; schema drift (naive `DateTime` vs `TIMESTAMPTZ`, no indexes) |
| 🔴 | `uv.lock` gitignored but Dockerfiles `uv sync --frozen` → unbuildable from clone |
| 🟠 | Icecast ffmpeg stderr-PIPE deadlock; `aac_encoder`/`generate_stem` no timeout; WS broadcast `Set changed size during iteration`; LISTEN/NOTIFY lost-notify; raw PCM served as `audio/wav`; false-positive healthchecks |

## 2. Fix phase — two swarms, disjoint file ownership

**Swarm 1 (8 agents):** CONCURRENCY · LOOP · WS · QUEUE · EXTERNAL-IO · SHOWS · OPS · TESTS.
(4 succeeded, 3 timed out after landing most of their work, SHOWS hit a 429.)
**Swarm 2 (3 completion agents + 1 micro-fix):** SHOWS (redo) · WS-hang · test-failures · app_ui-loop.

**Environment repaired first:** the committed `uv.lock` pins `numpy==1.23.5` (needs removed
`distutils`) + torch/wandb/pathtools (need removed `imp`) → broken on Python 3.12+. Built a
clean `.venv` on 3.12 with modern numpy + the web/test stack so fixes could be validated.

### Findings resolved

- **Concurrency (A1,A2,A4,A5,A8,A11,B8,B9):** mixer now snapshots state under `sync_lock`;
  `broadcast_audio` snapshots recording handles + logs (not swallows) write failures;
  `trigger_shutdown` flushes/closes recording handles + flips run flags under lock;
  WS snapshots locked + deep-copied; `broadcast` snapshots the connection set; vestigial
  `next_loop_ready` removed. Recording fields documented as `sync_lock`-protected.
- **Reliability (B1,B3,B4,B5,B7,B10,B11,B13):** inner watchdog retries the loop on transient
  errors; S3 client gets connect/read timeouts + retries; icecast ffmpeg → `stderr=DEVNULL` +
  `-nostats -loglevel error`; `encode/decode_aac` get a 60s `subprocess` timeout; LRU cap
  enforced via `cache_stem` (not direct dict write); `bpm<=0` guarded; mono audio normalized
  before tiling; flush serialization guard.
- **Data (C1,C3,C4,C5,C7,C8):** conductor actions/interactions now **appended to the audit
  buffers** (tables no longer empty); model columns reconciled to `TIMESTAMPTZ`/`JSONB` +
  partial indexes via `migrations/002`; show recording gets a real WAV header + postprocess
  called on stop; upload/commit failure now best-effort deletes the orphaned S3 object;
  show transitions serialized + file opened only after DB commit; job-dedup foundation added.
- **Job queue (B2,C2,A7,A9):** **lease column + heartbeat + stale-`processing` reaper** (jobs
  no longer orphaned forever); LISTEN/NOTIFY double-checked after listener register; UPDATE+NOTIFY
  in one transaction; asyncpg uses raw `DATABASE_URL` (not the broken SQLAlchemy-URL render).
- **Ops/build (F1,F2,F3,F4,F8):** `uv.lock` un-gitignored + `numpy>=1.26` floor (buildable on 3.12+);
  `requirements*.txt` deleted (pyproject is single source of truth); worker HEALTHCHECK now
  pings Postgres; web `/api/health` enriched + new `/api/health/ready` (503 on DB/S3 down);
  phantom `vllm` default → documented `localhost:1234` (LM Studio).
- **Tests (D1,D2,D3,D7,D9,D11,D12 + new regression suites):** suite now **collects cleanly**
  (was 357/577 with 10 errors); pure-math tests no longer poisoned by the autouse DB import;
  no-op/tautology tests replaced with real ones; WSL hardcoded path fixed; `sys.modules['torch']`
  pollution moved to a fixture with teardown; AAC skip made conditional on ffmpeg. **4 new
  regression-test files:** `test_concurrency_fixes`, `test_io_timeouts`, `test_loop_fixes`,
  `test_queue_lease_and_dedup`.

## 3. Verification

```
.venv/bin/python -m pytest tests/ --timeout=20
→ 542 passed · 58 skipped · 9 xfailed · 16 xpassed · 0 failed · 5.98s · exit 0
```

Baseline was: **357 collected, 10 collection errors, full suite could not run.**

## 4. Deferred (recommended separate sequential effort — too risky for a parallel swarm)

- **E1–E5 god-object/file refactors:** `framework_main_async.py` (1036 LOC, `_run_loop` 492 LOC),
  `GlobalState` (69 attrs), 111 functions >20 lines, hexagonal port introduction. Needs the
  test coverage (now in place) before splitting.
- **E6 typing modernization:** 51× `List`, 35× `Dict` (banned), 94× `Optional`, 31× `Any`.
  109 pre-existing ruff errors remain (legacy-typing / blind-except) — **zero new violations
  introduced** by the fix swarms; this is a mechanical `ruff check --fix` + manual pass.
- **Operator action required:** run `uv lock` to regenerate the (still-broken) lockfile from
  the corrected `pyproject.toml` so Docker images build. Source constraint already fixed.

## 5. Residual risks (acknowledged by the agents)

- A2 partial: route handlers still mutate solo/mute/volumes under `state.lock` (asyncio); the
  snapshot eliminates the crash/torn-read risk but full correctness needs the deferred E3
  unified-lock refactor.
- No external supervisor restarts the framework task if the whole process dies (B1's inner
  watchdog handles in-process blips only).
- `recording_postprocess` chapter-marker path depends on the now-populated audit tables —
  worth an end-to-end check once a real show runs.
