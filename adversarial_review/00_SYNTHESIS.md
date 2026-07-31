# Adversarial Review — Synthesized Master Findings

**Compiled from 7 adversarial review agents** (security lens excluded — project is local-only).
Full per-lens reports: `01_security.md` *(excluded from fix scope)*, `02_concurrency.md`,
`03_reliability.md`, `04_quality.md`, `05_tests.md`, `06_data.md`, `07_ops.md`.

Severity legend: 🔴 CRITICAL · 🟠 HIGH · 🟡 MEDIUM · ⚪ LOW

---

## A. Concurrency & Thread-Safety  (the documented #1 rule is violated broadly)

| ID | Sev | file:line | Problem |
| ---- | ----- | ----------- | --------- |
| A1 | 🔴 | `framework_state.py:329-353` vs `routes/shows.py` | **Two-lock mismatch**: recording file handles written under `threading.Lock` (`sync_lock`) in `broadcast_audio`, but closed/`None`-ed under `asyncio.Lock` (`state.lock`) in show routes. The two locks are different objects → **no mutual exclusion** → write-after-close / `None.write()` → silent audio loss (errors swallowed by `except Exception: pass`). |
| A2 | 🔴 | `framework_mixer.py:167,266-307` | Mixer reads `is_generating`, `soloed_stems`, `muted_stems`, `stem_volumes` with **no lock** on every callback tick (~22–44 Hz); route handlers mutate them under `asyncio.Lock` (which does NOT synchronize the mixer thread). Inline comment "safe for bool/set reads" is factually wrong. |
| A3 | 🔴 | `framework_main_async.py:675-682` → `framework_state.py:238-255` | `record_loop_transition()` acquires **blocking** `threading.Lock` while caller holds `asyncio.Lock` on the event-loop thread → **stalls the whole event loop** while Mixer holds `sync_lock`. AB-BA deadlock risk. |
| A4 | 🔴 | `framework_state.py:371-377` | `trigger_shutdown` writes `is_running`/`is_generating` **unlocked** from multiple threads; ordering not guaranteed. |
| A5 | 🟠 | `routes/ws.py:92-147` | `_state_snapshot`/`_stems_snapshot` read ~25 state fields & mutable containers **without `state.lock`** → torn snapshots, `Set changed size during iteration`. Returns `active_stems` by direct reference (not a copy). |
| A6 | 🟠 | `routes/*`, `app_ui.py:73-115` | **Sync SQLAlchemy I/O inside `async def` handlers** blocks the event loop (AuthMiddleware `dispatch` too). |
| A7 | 🟠 | `job_waiter.py:87-118` | LISTEN/NOTIFY race: NOTIFY fired between status pre-check and `add_listener` is **lost** → up to 120 s latency before timeout re-check. |
| A8 | 🟠 | `routes/ws.py:148-166` | `ConnectionManager.broadcast` iterates the **live** connection set while `await ws.send_text` yields → `RuntimeError: Set changed size during iteration` crashes broadcast to all subscribers. |
| A9 | 🟠 | `job_waiter.py:148-160` | asyncpg DSN derived from `engine.url.render_as_string()` may render `postgresql+psycopg2://` which asyncpg rejects → silent `except Exception: pass` fallback to polling (which shares the SQLAlchemy pool → exhaustion). |
| A10 | 🟡 | `framework_main_async.py:395-430` vs `routes/stems.py:78-86` | TOCTOU: user-added custom stems silently overwritten when loop does `state.next_stems = []`. |
| A11 | 🟡 | `framework_state.py:127` | `next_loop_ready` Event is **vestigial dead code** but CLAUDE.md documents it as active coordination — docs/code drift. |
| A12 | 🟡 | `routes/config.py:155-158` → `framework_state.py:283-300` | `add_custom_instrument` does file I/O while holding `sync_lock`. |

---

## B. Reliability & Resilience  (one blip → permanent outage or silent corruption)

| ID | Sev | file:line | Problem |
| ---- | ----- | ----------- | --------- |
| B1 | 🔴 | `framework_main_async.py:703-710`, `app_ui.py:98` | **No watchdog**: one uncaught exception in `_run_loop` → top-level handler stops the mixer, sets `running=False`; lifespan task never restarts → **music dies until manual restart**. |
| B2 | 🔴 | `worker.py:164-176`, `cleanup.py:88-100` | **No lease/heartbeat/requeue**: worker death between claim & complete orphans the job in `processing` forever (cleanup only touches `completed/failed/expired`; claim query only selects `pending`). |
| B3 | 🔴 | `garage_client.py:56-58` | S3/Garage client has **no connect/read timeout, no retries** → a half-open connection hangs worker executor threads and the framework's `_fetch_audio` **indefinitely**. |
| B4 | 🟠 | `framework_icecast.py:233-263` | ffmpeg `stderr=PIPE` captured but only drained **after** `poll()` returns non-None → stderr buffer fills → ffmpeg blocks on stderr write → stdin pipe fills → `stdin.write` blocks → classic subprocess deadlock. |
| B5 | 🟠 | `aac_encoder.py:75,127` | `subprocess.run(...)` with **no `timeout=`** in `encode_aac`/`decode_aac` → hung ffmpeg wedges worker/loop forever. |
| B6 | 🟠 | `worker.py:193-200` | `generate_stem` executor call has **no timeout** → hung CUDA/gen wedges worker slot forever (compounds B2). |
| B7 | 🟠 | `framework_main_async.py:481` | `state.last_generated_stems[prompt] = audio_data` **bypasses the LRU cap** (`cache_stem`) → unbounded memory growth (~2.8 MB/entry × hundreds of stems) → OOM. |
| B8 | 🟠 | `framework_state.py:225-246` | `trigger_shutdown` **never closes/flushes** `recording_file_handle` / `current_show_audio_file` → truncated/corrupt recordings on SIGTERM. |
| B9 | 🟠 | `framework_state.py:178-196` | `broadcast_audio` swallows recording-write failures (`except Exception: pass`) → **silent data corruption** (disk full, bad handle). |
| B10 | 🟠 | `framework_main_async.py:33` | `calc_duration` divides by `bpm/60` → **`ZeroDivisionError` if BPM is 0** → escapes to B1's handler → kills the loop. |
| B11 | 🟠 | `framework_main_async.py:467` | Mono (1-D) audio → `np.tile(audio, (repeats,1))` raises → swallowed → silent stem. |
| B12 | 🟡 | `framework_conductor_async.py:172-186` | LLM `except Exception: break` doesn't retry transient transport errors (only JSON errors retry). |
| B13 | 🟡 | `framework_main_async.py:45-83` | `flush_recording_buffers` has no concurrency guard → duplicate audit rows on overlapping flush. |
| B14 | 🟡 | `job_waiter.py:16-28` | Module-level asyncpg pool never closed; LISTEN/NOTIFY path effectively dead (framework never passes `db_manager`). |

---

## C. Data Integrity & Database

| ID | Sev | file:line | Problem |
| ---- | ----- | ----------- | --------- |
| C1 | 🔴 | `framework_state.py:150-151` | **Show audit data is NEVER persisted**: `action_buffer`/`llm_interaction_buffer` declared & flushed, but **no `.append()` anywhere writes to them** → `show_actions` & `llm_interactions` tables permanently empty. |
| C2 | 🔴 | = B2 | Job dead-letter (no lease). |
| C3 | 🔴 | `docker/compose.yaml`, `models/*` vs `migrations/001_*` | **Migrations never run in production** (not mounted; only `create_all()`). Models diverge from SQL: `DateTime` (naive) vs `TIMESTAMPTZ`, `JSON` vs `JSONB`, no `CHECK`, missing partial indexes for claiming/cleanup → seq scans + tz-bug in `expires_at < NOW()`. |
| C4 | 🟠 | `routes/shows.py:145,281`; `framework_state.py:343-352` | Show recording written as **raw PCM but served `audio/wav`** → unplayable; `recording_postprocess` (would add header) **never called from `stop_show`**. |
| C5 | 🟠 | `worker.py:130-145,203-218` | Upload-then-commit non-atomic → orphaned Garage objects on crash/DB-write-failure. |
| C6 | 🟠 | = A7 | LISTEN/NOTIFY lost (separate transactions). |
| C7 | 🟡 | `routes/shows.py:105-165` | No optimistic locking on Show; concurrent `start_show` leaks file handles, flips flags inconsistently. |
| C8 | 🟡 | `framework_main_async.py:453-468`, `routes/jobs.py:18-37` | No job idempotency/dedup; 300 s cache eviction → "retained" stem re-submitted → **different audio** (Foundation-1 is non-deterministic). |
| C9 | ⚪ | `app/db.py:19` | No `pool_pre_ping`/`pool_recycle` → stale conns after PG restart. |

---

## D. Test Quality  (≈42% real protection; 10 files fail to collect)

| ID | Sev | location | Problem |
| ---- | ----- | ---------- | --------- |
| D1 | 🟠 | `tests/conftest.py` autouse | `reset_db_singleton` does `from app.db import DatabaseManager` at collection → **every** file fails when sqlalchemy missing (even pure-math `test_harmonic.py`). Only 357/577 collect. |
| D2 | 🟠 | `test_simulation.py::test_serialization_failure_*` | **No `assert` in body** — always passes. |
| D3 | 🟠 | `test_simulation.py::test_response_format_schemas_are_identical` | **Tautology**: calls the same function twice, asserts equal. |
| D4 | 🟠 | `test_async_framework.py` `TestPreGenResultsUsage`/`TestNextLoopPreGeneration` (7) | **Re-implements production logic inline**; never imports `framework_main_async`. Passes even if deleted. |
| D5 | 🟠 | `test_async_framework.py::test_app_uses_async_framework_not_sync` | Asserts only `"async" in __name__`; admits claim "too complex to test". |
| D6 | 🟠 | `test_uat.py` (13 reasoning tests) | Every assert is `status in (200, 401)` — proves nothing about body. |
| D7 | 🟠 | `test_uat.py` hardcoded `/mnt/c/slop/mc-clanker` | WSL path from another machine → passes vacuously everywhere else. |
| D8 | 🟠 | `test_api.py::TestStemIndexValidation` (3) | **Anti-regression tests**: assert out-of-range index returns 200 (defends the missing-validation bug). |
| D9 | 🟠 | `test_db.py::test_create_tables` | Body is `pass` inside a `with` — no assert, no call. |
| D10 | 🟡 | `test_state.py` "thread_safety" tests | Single-threaded / `set.add` of one object → can't distinguish locked from unlocked. |
| D11 | 🟡 | `test_gpu_monitor.py` module top | `sys.modules["torch"] = mock` at module level → **session-wide pollution**. |
| D12 | 🟡 | `test_worker_fetch_audio.py::test_decode_aac_*` | Unconditional `pytest.skip` → AAC bridge has **zero executed tests**. |
| D13 | ⚪ | repo-wide | No test instruments `state.lock` acquisition (50 production sites); no real test of `_run_loop`, `parse_llm_json_response`, LISTEN/NOTIFY, real ffmpeg, real S3. |

---

## E. Code Quality & Architecture  (vs the project's OWN AGENTS.md/CLAUDE.md rules)

| ID | Sev | location | Problem |
| ---- | ----- | ---------- | --------- |
| E1 | 🟠 | `framework_main_async.py` (1036 lines) | GOD FILE >2× the 500-line limit. |
| E2 | 🟠 | `framework_main_async.py:220` `_run_loop` (492 lines) | GOD METHOD >24× the 20-line limit; ≥5 responsibilities. |
| E3 | 🟠 | `framework_state.py:71` `GlobalState` | GOD OBJECT: 109-line `__init__`, 69 attrs, 8 concerns; module-global singleton imported 40× (violates "inject, don't import"). |
| E4 | 🟠 | 111/307 functions | **36% exceed the 20-line limit**. |
| E5 | 🟠 | `framework_main_async.py`, `_conductor_async.py`, `_generator.py` | **Hexagonal violations**: domain imports `job_waiter`/`garage_client`/`openai`/`torch`/`stable_audio_tools` directly — no ports. |
| E6 | 🟠 | 19 modules | **Legacy typing**: 51× `List`, 35× `Dict` (banned), 94× `Optional`, 31× `Any`, 11× bare `List[Dict]`, only 1 PEP-604 usage on a 3.10+ runtime. |
| E7 | 🟠 | `routes/ws.py:31`, `db.py:10` | Banned name `Manager` (`ConnectionManager`, `DatabaseManager`). |
| E8 | 🟠 | 49 sites in framework | `print()` instead of structured logging. |
| E9 | 🟡 | 25 sites | `except Exception: pass/continue` swallows errors silently. |
| E10 | 🟡 | 9+ sites | Hardcoded `44100` sample rate (one even has `or 44100` fallback). |
| E11 | 🟡 | `routes/stems.py:65-83` | WAV encode done **inside** `async with state.lock` (I/O under lock). |
| E12 | 🟡 | duplicated | 3× `wait_for_job_completion` variants; duplicated prompt builders; ~30× DB-session boilerplate. |
| E13 | ⚪ | 53 public fns | Missing docstrings. |

---

## F. Ops / Build / Config  (non-security subset)

| ID | Sev | location | Problem |
| ---- | ----- | ---------- | --------- |
| F1 | 🔴 | `.gitignore:23` vs `Dockerfile.web:21` / `Dockerfile.worker:27` | **`uv.lock` gitignored but both Dockerfiles `uv sync --frozen`** → fresh clone/CI can't build images. *(Lock exists locally only.)* |
| F2 | 🟠 | `requirements*.txt` vs `pyproject.toml` | **Three divergent dep manifests**: `requirements.txt` unpins `stable-audio-tools` & is missing `asyncpg/boto3/httpx/starlette` → `pip install -r requirements.txt` yields a broken env. |
| F3 | 🟠 | `Dockerfile.worker:36-37` | Worker HEALTHCHECK is `pgrep` only — ignores DB/S3. |
| F4 | 🟠 | `routes/config.py:11-20`, `Dockerfile.web:30-31` | Web `/api/health` only checks `state.is_running` — false-positive during DB/S3 outages. |
| F5 | 🟠 | `worker.py:270-296` | `health_check()` reports Garage "connected" based on client existing, no round-trip → lies. |
| F6 | 🟡 | 20 sites | `os.environ["X"]` bracket access → unhelpful `KeyError` on missing config. |
| F7 | 🟡 | `framework_state.py:102` | `llm_api_key = "not-needed"` hardcoded, ignores `LLM_API_KEY` env. |
| F8 | 🟡 | `compose.yaml:45` | Default `LLM_BASE_URL=http://vllm:8000/v1` but no `vllm` service defined → broken first-run. |
| F9 | ⚪ | `pyproject.toml` | All deps floating `>=` (non-reproducible even with lock committed). |

---

## Fix-Swarm Partition (disjoint file ownership to avoid conflicts)

| Agent | Owns (files) | Fixes |
| ------- | -------------- | ------- |
| **CONCURRENCY** | `framework_state.py`, `framework_mixer.py` | A1, A2, A4, A8(ws-broadcast is ws.py→ see WS agent), B8, B9, A11, A12 |
| **LOOP** | `framework_main_async.py` | A3, B1, B7, B10, B11, C1(append side), B13 |
| **WS** | `routes/ws.py` | A5, A8 |
| **QUEUE** | `worker.py`, `job_waiter.py`, `cleanup.py`, `models/generator_job.py`, `migrations/` | B2/C2, A7/C6, A9, C3, C5, C8 |
| **EXTERNAL-IO** | `garage_client.py`, `aac_encoder.py`, `framework_icecast.py` | B3, B4, B5, B6 |
| **SHOWS/DATA** | `routes/shows.py`, `lib/recording_postprocess.py` | C4, C7, A1(show-side handle close) |
| **OPS** | `docker/`, `.gitignore`, `requirements*.txt`, `onboarding.py`, `routes/config.py`(health only) | F1, F2, F3, F4, F5, F6, F8 |
| **QUALITY** | `lib/constants.py` + targeted edits (typing/constants) across files *via disjoint symbols* | E6(partial), E10, E12(job_waiter dedup) |
| **TESTS** | `tests/` | D1–D13 |

**Deferred (high-risk, need sequential care, not parallel-swarm):** E1–E5 god-file/object refactors, E5 hexagonal port introduction. Recommend a follow-up dedicated effort with test coverage first.
