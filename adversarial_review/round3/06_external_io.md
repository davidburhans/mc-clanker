# Round 3 — External I/O lane (adversarial bug hunt)

Scope: `app/garage_client.py`, `app/framework/framework_icecast.py`,
`app/framework/framework_conductor_async.py` (HTTP client parts), `app/onboarding.py`,
`app/gpu_monitor.py`, `app/aac_encoder.py`, `app/cleanup.py` (+ the two call sites that
consume onboarding from `app_ui.py`). All line numbers valid at HEAD `04791e4`.

Prior rounds read first and not re-reported: `00_SYNTHESIS.md`, `00_FINAL_REPORT.md`,
`tests/test_adversarial_wave1/wave2/leftovers.py`, round-3 lanes `01`–`05` (in particular
B3/B4/B5 fixes in this lane, Q3 `cleanup.py:203` command_timeout, Q4 S3-orphan ordering,
B12 conductor transport-retry gap, F6 `os.environ[...]` bracket access, E8 print-logging).
Read-only hunt; the only file written is this report; proofs ran from `/tmp` with the repo
venv (`.venv/bin/python`, Python 3.12.13), no repo file touched.

---

## CONFIRMED BUGS

### 1. HIGH | `app/onboarding.py:274-299` (`write_env_file`) + `app/app_ui.py:473-481` | `.env` writer validates KEY names only — a single JSON value smuggles arbitrary NEW environment variables into the host `.env`, defeating the SEC-2 allowlist

**Mechanism.** The SEC-2 fix allowlists keys (`SETUP_ENV_KEYS`, `app_ui.py:439-461`) but
`write_env_file` writes each value raw:

```python
with open(env_path, "w") as f:
    for key, val in merged.items():
        f.write(f"{key}={val}\n")     # onboarding.py:298-299
```

A value containing `\n` terminates the line early and injects additional `KEY=value`
lines that are **not** subject to the allowlist. The route does no value sanitization
either: `values = {k: v for k, v in body.items() if v and v != ""}` (`app_ui.py:473`)
then the key-set check (`:475-479`). Because the middleware's DJ gate is a no-op while
`DJ_PASSWORD` is unset (the default; wave-1 SEC-2 tests exercise this route
unauthenticated), any LAN peer can persist injected variables into the host-mounted
`.env` (`../.env:/app/.env:rw` per the docstring), and `restart_services()` then bounces
web+worker so compose re-reads the poisoned file. Injected keys can be anything compose
or the containers consume — e.g. `DJ_PASSWORD` (attacker-settable auth), `DATABASE_URL`,
`HF_TOKEN`, `POSTGRES_USER`, compose interpolation variables — i.e. the exact takeover
SEC-2 was meant to prevent, re-entering through the value channel. Non-malicious
variant: a pasted API key with a trailing newline corrupts the file the same way.

**Trigger.** `POST /api/setup/config` with body `{"LLM_MODEL": "qwen\nDJ_PASSWORD=x\n..."}`
(anonymous while no DJ password is configured).

**Impact.** Persistent host `.env` corruption / arbitrary env injection surviving container
restarts; silently rewrites auth and storage configuration.

**Minimal fix.** In `write_env_file`, reject (or sanitize) values containing newlines/
`NUL` — e.g. `if "\n" in val or "\r" in val or "=" in val and key in _QUOTING_KEYS: raise
ValueError(...)` — and have the route 422 on the same predicate. Prefer
`shlex.quote`-style single-line quoting for all values.

**Proof (executed at HEAD):**

```
$ ENV_FILE_PATH=/tmp/p06_env/.env .venv/bin/python /tmp/p06_env.py
--- resulting /tmp/p06_env/.env ---
POSTGRES_PASSWORD=keepme
LLM_MODEL=qwen
DJ_PASSWORD=pwn123                      <-- injected line, key NOT in SETUP_ENV_KEYS check
GARAGE_ENDPOINT=http://evil:3900        <-- injected line

restart argv: ['docker', 'compose', '-f', 'compose.yaml', 'restart', 'web', 'worker']
```

The single body value `"qwen\nDJ_PASSWORD=pwn123\nGARAGE_ENDPOINT=http://evil:3900"`
produced three top-level env assignments. (Input file had only `LLM_MODEL`/`POSTGRES_PASSWORD`.)

---

### 2. MEDIUM | `app/framework/loop_steps.py:377` (+ `:235`, `:571`, `app/framework/pregeneration.py:133`) × `app/framework/framework_conductor_async.py:250` | LLM `master_key` is committed to `state.current_key` with no enum validation — one non-enum value permanently bricks the conductor into silent fallback

**Mechanism.** `get_next_state_async` builds the harmonic-neighbor prompt fragment via
`HarmonicHelper.get_harmonic_neighbors(current_key)` (`framework_conductor_async.py:250`),
which raises `ValueError("Invalid key '<key>'. Must be one of: [...]")`
(`app/lib/harmonic.py:52`) for anything outside the 24 `VALID_KEYS`. The loop commits the
LLM's own `master_key` into state with no validation:

```python
state.current_key = conductor_response.get("master_key", current_key)   # loop_steps.py:377
```

The `response_format` JSON schema does declare `master_key: enum(VALID_KEYS)`
(`app/lib/constants.py:314`), but `parse_llm_json_response` accepts free-text JSON from
any backend that ignores/loosely applies `response_format` (plain JSON mode, template-only
servers) — and this codebase explicitly treats LLM output as untrusted everywhere else
(markdown-fence recovery, `build_fallback_response`). Once an invalid key lands in
`state.current_key`, **every** subsequent conductor call raises inside prompt building;
`_step_call_conductor` catches it (`loop_steps.py:344-345`) and substitutes
`build_fallback_response`, whose `"master_key": current_key`
(`app/framework/conductor_interaction.py:63`) is re-committed at `:377` — the poisoned
value perpetuates itself. The same unvalidated echo exists on the pregen paths
(`loop_steps.py:235`, `:571`, `pregeneration.py:133`). Note the contrast: the user-facing
`target_key_override` IS validated (`app/routes/schemas.py:53-57`), so only the LLM channel
is open. (`master_bpm` has the same missing validation at `loop_steps.py:375`, but
`calc_duration`'s `isinstance(...)/(bpm > 0)` guard, `domain_audio.py:24-33`, absorbs
`None`/`0` — key is the exploitable one.)

**Trigger.** One conductor response with `"master_key": "<not in VALID_KEYS>"` (schema-
ignoring LLM backend, quantized model hallucinating "Db minor", prompt-injection via the
user vibe override, etc.). The system already anticipates malformed LLM JSON, so this is
within normal operation.

**Impact.** From that loop on, the LLM DJ is silently dead: every iteration falls back to
retain-all, music stops evolving, and no error surfaces beyond per-loop stdout prints.
No recovery except a manual reset that overwrites the key.

**Minimal fix.** Validate at the commit: only assign `master_key` when it is in
`VALID_KEYS`, else keep `current_key` (one `if` at `loop_steps.py:377` and `:571`; or clamp
inside `process_actions`/`_step_parse_actions`). Optionally also raise-and-fallback once
instead of every loop by sanitizing at read time.

**Proof (executed at HEAD, real `ConductorLLMAsync` + real `build_fallback_response`, only
the OpenAI transport faked):**

```
1. loop-1 response accepted by parse path; master_key = 'Z major'
2. committed to state.current_key: 'Z major'          # loop_steps.py:377, no enum check
3. loop-2 LLM call raises: Invalid key 'Z major'. Must be one of: ['C major', ...]
4. fallback master_key re-committed every loop (loop_steps.py:377): 'Z major'
```

---

### 3. MEDIUM | `app/framework/framework_icecast.py:96-101, 305-321` | `_running` is never cleared when ffmpeg dies mid-stream — `is_running` lies, restart is refused, and the mixer feeds a dead pipeline forever

**Mechanism.** During streaming, ffmpeg exits whenever the Icecast side drops the source
connection (server restart, auth rejection, mount removal, network blip). The feed loop
detects it (`poll() is not None` → warning → `break`, `:307-312`) and the `finally` runs
`_cleanup()` (`:320-321`) — but unlike every other exit path (`:264`, `:268`, `:287`,
`:297`), this path **never sets `self._running = False`**. Consequences:

- `is_running` keeps returning `True` (`:75-76`) after the streaming thread is dead;
- `start()` (`:96-101`) then refuses with "already running", so the streamer cannot be
  restarted without first calling `stop()` — which nothing does, because status still
  claims it is running;
- `feed_pcm` (`:148-157`) keeps accepting the mixer's PCM into the queue of a dead
  pipeline (bounded at 200 chunks, then silently dropped) — no signal, no log;
- `is_connected` (`:79-85`) is the only truthful indicator, but it is read by nothing
  outside tests, and no reconnect logic exists despite the "handles reconnection" comment
  at `:212-213` (single-ffmpeg design: when ffmpeg dies, streaming is over).

**Reachability caveat (honest):** at current HEAD no production code instantiates
`IcecastStreamer` (`create_icecast_streamer_from_env` has zero app callers; only
`state.icecast_enabled` flag plumbing exists), so today this is a latent defect in the
designated ICECAST_ENABLED entrypoint class, not a live outage. Within the class itself it
is squarely reachable in normal operation (Icecast restarts are routine).

**Impact.** When the module is wired in: one Icecast blip → public stream dead until
manual `stop()`+`start()`, with `is_running` actively lying to any status consumer and the
restart path blocked by the "already running" guard.

**Minimal fix.** Set `self._running = False` in the ffmpeg-exited break path (or after the
`finally` when exiting due to pipe error/ffmpeg death), and either log loudly or add a
bounded reconnect-with-backoff that respawns ffmpeg and re-requests the mount.

**Proof (executed at HEAD, faithful `Popen` fake with `returncode`, real `_stream_loop`):**

```
1. streaming thread dead: True
2. is_running after ffmpeg death: True          <-- lie
3. is_connected: False
4. restart created a live thread: False         <-- start() hit the "already running" guard
5. feed_pcm chunks queued into dead pipeline: True
6. after explicit stop(), is_running: False     <-- only manual stop() clears it
```

**Related (same class, cosmetic):** `bytes_streamed` (`:92-94`) always returns 0 —
`_bytes_streamed` is initialized (`:71`) and logged (`:146`) but never incremented
anywhere, so the stop-line "streamed %d bytes" and the property are always wrong.

---

### 4. MEDIUM | `app/onboarding.py:301-317` (`restart_services`) called from `async def save_setup_config` (`app/app_ui.py:489`) | `subprocess.run` with no timeout, on the event loop, invoking `docker compose restart web` — a wedged docker daemon freezes the whole app indefinitely

**Mechanism.** `restart_services` runs:

```python
subprocess.run(["docker", "compose", "-f", "compose.yaml", "restart", "web", "worker"],
               cwd=compose_dir, check=False)          # onboarding.py:311-315 — no timeout=
```

and the route awaits nothing — `restart_services()` is called synchronously inside the
`async def` handler (`app_ui.py:489`), so this blocks the event loop for the full duration
of the subprocess. `docker compose restart` has no short internal bound: against an
unresponsive/hung daemon (socket mounted, as the feature requires to work at all) the CLI
blocks indefinitely, taking every HTTP/WS request, the framework task's awaits, and health
checks down with it — and if it does proceed, "restart web" kills the very process
executing it before the response is sent. There is no per-call bound anywhere on this
path (contrast: `aac_encoder._run_ffmpeg` enforces 60 s after round-2 B5).

**Trigger.** Any successful `POST /api/setup/config` (which is the route's happy path)
while the docker CLI/daemon is slow or hung; also normal (slow) restarts block the loop
for seconds.

**Impact.** Whole-app freeze of unbounded duration on a plausible ops failure; request
that can never return its own response by design.

**Minimal fix.** `subprocess.run(..., timeout=15)` with `TimeoutExpired` handled, and run
it off the loop (`await asyncio.to_thread(restart_services)`) — or fire it as a detached
`Popen` so the response returns first.

**Proof (executed at HEAD — argv capture):**

```
restart argv: ['docker', 'compose', '-f', 'compose.yaml', 'restart', 'web', 'worker']
timeout kwarg present: False
```

(route path cited: `app_ui.py:464-489`; handler is `async def`.)

---

### 5. LOW | `app/cleanup.py:76-82, 173-174, 233` | SIGTERM handler cannot stop the cleanup loop — `stop()` flips a flag that is only read after up to `cleanup_interval` (300 s) of `asyncio.sleep`, so shutdown always ends in SIGKILL and the pool is never closed gracefully

**Mechanism.** `main()` registers `loop.add_signal_handler(sig, cleanup.stop)`
(`cleanup.py:233`). `stop()` (`:173-174`) only sets `self.running = False`. The loop is

```python
while self.running:
    ...await self._run_cleanup()...
    await asyncio.sleep(self.config.cleanup_interval)   # cleanup.py:82 — 300 s default
```

so a SIGTERM that arrives during the (typical) sleep leaves the task sleeping for the
remainder of the interval; `db.close()` at `:87-89` runs only after that. Containers
SIGKILL at the grace period (~10 s default), so the documented graceful shutdown never
actually happens: the asyncpg pool is always torn down by SIGKILL.

**Trigger.** `docker stop`/SIGTERM of the standalone cleanup service at any point outside
the instant between two `_run_cleanup` calls (i.e. essentially always).

**Impact.** No data corruption (cleanup is idempotent), but the graceful-shutdown path is
dead code in practice; log lines/pool close never run; relies on SIGKILL every time.

**Minimal fix.** Use an `asyncio.Event`: `stop()` sets it; loop waits
`await asyncio.wait_for(shutdown_event.wait(), timeout=cleanup_interval)` instead of bare
`sleep`, breaking immediately on signal.

**Proof (executed at HEAD, `cleanup_interval=3600` to make the window observable; only the
pool/`_run_cleanup` faked):**

```
INFO:app.cleanup:Shutdown requested...
cleanup task done 1s after SIGTERM-handler stop(): False
```

---

## SUSPECTED (UNVERIFIED)

### S1. LOW | `app/framework/framework_conductor_async.py:125-138` | Abandoned `AsyncOpenAI` clients are never closed when the LLM config changes; `config=None` path can return a client built for a different base URL

Each distinct `(base_url, api_key, model)` tuple replaces `self._async_client`
(`:128-134`) without `await old.close()`; each instance owns an `httpx.AsyncClient` whose
pool/sockets live until GC (unclosed-client warnings on loop shutdown). Reachable via
`POST /api/llm-config` churn — bounded in practice, unbounded only under config flapping.
Additionally, a `call_async(..., llm_config=None)` after any config-keyed call silently
reuses the config-keyed client (`:136-138` only creates when `None`), which is a stale-
endpoint hazard rather than a today-bug (the loop always passes a config). Would confirm:
open FD count while flipping `llm_base_url` N times on a live loop.

### S2. LOW | `app/onboarding.py:52-79` | `check_database` leaks the asyncpg connection when `execute("SELECT 1")` raises

`conn = await asyncpg.connect(...)` is only followed by `await conn.close()` on the
success path; an `execute` failure jumps to the generic `except` and the socket is left to
GC. Reachable from `/api/health/ready` polling (checks run per readiness request), so a
DB that accepts connects but fails queries leaks one socket per probe until GC. Would
confirm: `asyncpg`-logged "unclosed connection" warnings under a failing-DB readiness poll.

---

## CHECKED AND CLEAN

- **`app/garage_client.py` — B3 fix intact:** every client gets `connect_timeout=5`,
  `read_timeout=30`, adaptive retries `max_attempts=3` (`S3Timeouts`/`build_boto3_config`,
  `:24-45`); worst-case bounded (~2 min), no indefinite hang. Retry duplication check:
  the only retried mutating op is `put_object`, which overwrites the same key — idempotent.
  `get_object` reads the full body or raises (no partial-read-as-success). `exists()`'s
  `except ClientError → False` conflates 403/500 with 404, but it has zero app callers.
  `ensure_bucket_exists`'s swallowed failure at `app_ui.py:75` is F4/F5-class (known).
  `create_garage_client_from_env` bracket-access KeyErrors = F6 (known).
- **`app/aac_encoder.py` — B5 fix intact:** single `_run_ffmpeg` chokepoint with
  `subprocess.run(..., capture_output=True, timeout=60)` (uses `communicate` internally —
  no PIPE-buffer deadlock); `TimeoutExpired`/`CalledProcessError` mapped to `RuntimeError`
  with stderr; temp files unlinked in `finally` (incl. the `wav_path=None` guard);
  `decode_aac` validates the sample rate and coerces mono/>2ch inputs. No new defects.
- **`app/gpu_monitor.py`:** module has **zero production callers** (grep: nothing outside
  the file imports `GPUMonitor` or any of its methods) — dead code; internally the
  exception paths all return safe dicts, history is capped at 100, `track_model_load`
  re-raises load failures. `get_degradation_actions` always returning
  `offload_candidates: []` despite `select_offload_candidates` existing is inert for the
  same reason. Nothing reachable to break.
- **`app/cleanup.py` reaper/CTE logic:** `_reap_stale_processing` lease predicate and
  `_delete_expired_jobs` single-CTE delete-then-delete ordering, `expires_at` budgets, and
  the once-path `command_timeout` gap (Q3) were judged by lane 03 — re-verified the
  statements compile-shaped and the CTE returns only rows deleted in the same statement;
  nothing new beyond findings 5/S-above.
- **Conductor HTTP client retry/timeout shape:** per-request `timeout=60.0`
  (`framework_conductor_async.py:186`), JSON-parse failures retried up to
  `max_retries=3`, `_get_async_client` keyed on the full config tuple; the no-retry-on-
  transport-error gap is B12 (round 1, known — not re-reported). `_get_async_client` is
  only touched from the event loop (no cross-thread race).
- **Icecast B4/SEC-6 fixes intact:** `stdout/stderr=DEVNULL` + `-nostats -loglevel error`
  (pinned by `tests/test_io_timeouts.py`); `_log_safe_argv` redacts the Authorization
  header without mutating the real argv (pinned by `tests/test_adversarial_leftovers.py`).
  `stop()` kill path holds `self._lock` across `wait(timeout=2)` — bounded, and the
  stream thread never takes the lock while writing (no deadlock found); `_http_proc` is
  vestigial-None but harmless.
- **`run_onboarding_checks` (`onboarding.py:213-242`):** async checks gathered with
  `return_exceptions=True` (no one failing check kills the batch); the Garage probe runs
  off-loop with `retries={"max_attempts": 1}` (wave-1 ASYNC-1 pin intact); httpx probe has
  a 5 s timeout; sync checks are pure.

## VERIFICATION

All five CONFIRMED findings reproduced by execution at HEAD `04791e4` with `.venv/bin/python`
(3.12.13); scripts under `/tmp/p06_*.py`, repo unmodified. Falsification notes: the
`target_key_override` invalid-key chain was **rejected** (schemas.py:53-57 validates against
`VALID_KEYS`); `master_bpm=None` poisoning was **rejected** as a crash (`calc_duration`
isinstance guard, domain_audio.py:31-32); `gpu_monitor` candidates were **rejected** on
reachability (no callers); S3 put-retry duplication was **rejected** (idempotent same-key
overwrite); icecast stderr deadlock and AAC timeout regressions were **rejected** (B4/B5
fixes present and pinned).
