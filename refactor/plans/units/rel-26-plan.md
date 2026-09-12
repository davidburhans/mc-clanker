# PLAN — Unit 14 `rel-p3-hygiene` (REL-26, REL-27, REL-28, REL-29, REL-31, REL-32), branch `rel-26-32-p3`

**Spec:** `refactor/plans/rel-remediation-plan.md` §U14 (lines 212–223) · decisions log line 328 ("REL-26 icecast → delete, not fix") · `docs/reliability_audit.md` REL-26 row (:470) + P3 bullets REL-27/28/29/31/32 (:474–484). REL-30 is already fixed (rel-13) — NOT in this unit.

**Baseline gate at HEAD of `main` (verified for this plan):** `.venv/bin/python -m pytest tests/ -q` → **1165 passed / 18 skipped / 0 failed** (30.6 s) · `.venv/bin/python -m ruff check app tests` → **All checks passed**. Preflight before every commit: `cd /home/dave/workspaces/mc-clanker && .venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q`.

**Python:** `requires-python = ">=3.10"` — use `asyncio.wait_for`, NOT `asyncio.timeout` (3.11+).

**Verified against code at HEAD** (line refs current, re-grepped for this plan — the scout's anchors all confirmed, plus 4 reference clusters the scout missed: `static/mc-clanker/app.js` :143/:623/:1998, `static/mc-clanker/index.html` :607–611, `.env.example` :56–57, `tests/test_mixer_resilience.py:94` docstring mention):
`app/framework/framework_icecast.py` 373 L (`IcecastStreamer`, `create_icecast_streamer_from_env` :346 — ZERO non-test callers) · `tests/test_icecast.py` 360 L (2 `@pytest.mark.skip` at :246/:285) · `app/aac_encoder.py` `encode_aac` temp-WAV write at **:108–109** INSIDE `with tempfile.NamedTemporaryFile(..., delete=False)` with `try/finally: wav_path.unlink` starting at **:112–133** · `app/framework/framework_generator.py` 14 `print(` sites at **:76, 80, 101, 122, 147, 195, 215, 279, 287, 311, 352, 356, 375** (no logging import; no test asserts its stdout — verified `grep capsys/stdout tests/test_generator.py` empty) · `app/framework/loop_steps.py` `_step_await_jobs_fetch` at **:692–750** (serial `await self._fetch_audio(audio_path)` at **:735**), `_step_await_pregen` at **:1062**, per-250 ms DEBUG print at **:1123–1126**, once-per-loop "Pre-generation complete" at **:1115**; module has NO `logging` import; constants block at :100–117 (`JOB_PENDING_DEPTH_LIMIT` monkeypatch precedent) · `app/framework/pregeneration.py` mirror serial fetch loop at **:158–168** (module already imports from `loop_steps` :30 — shared helper needs no new import direction) · `app/routes/config.py` `_ping_object_store` **:52–79** (fresh `boto3.client` per probe; already called via `asyncio.to_thread` at :126 → thread-safety required), icecast refs :398/:413–414 · `app/routes/stems.py` `download_stem` **:52–98** (whole clip→WAV→Response inside `async with state.lock:`; AUDIO-1 comment :75–79; identical clip math at stems.py:86 / `app/framework/framework_mixer.py` PCM convention) · `app/playback.py` `_playback_loop` **:64–105** (`sampwidth` read :76, raw `readframes` bytes → `state.broadcast_audio` :90; `import wave` inside method; s16le consumers: `app/youtube_relay.py` `-f s16le` argv, `app/stream_fanout_args.py`, `app/lib/wav.py` recording header) · `app/routes/ws.py` `ConnectionManager.broadcast` **:66–88** (sequential `await ws.send_text`; snapshot-before-iterate comment :74–76 MUST be preserved; `_connections_sync` used from sync contexts; singleton `ws_manager` :93; all broadcast call sites `await` the broadcast before issuing the next per topic) · `state.lock` = `asyncio.Lock` (framework_state.py:98); `last_generated_stems` property returns the LRU `OrderedDict` (:293–300).

---

## 0. Scope summary

| ID | Finding | Fix site |
|---|---|---|
| REL-26 | `framework_icecast.py` dead code, 3 latent 24/7 hazards | DELETE module + test file + every reference (app, static, tests, docker, README, .env.example) + grep-pin test |
| REL-27a | `encode_aac` orphans temp WAV when `wavfile.write` raises (unlink in finally misses the write) | move `wavfile.write` inside the `try` so the existing `finally: unlink` covers it |
| REL-27b | 14 `print(` sites in `framework_generator.py` | module logger; info/warning/error mapped per site + AST no-print pin |
| REL-28 | per-stem Garage fetch strictly serial in the results loop (~5–15 s/batch) | shared bounded `asyncio.gather` helper used by BOTH the foreground P8 path and the pregen mirror |
| REL-29 | per-250 ms `print` in pregen wait (tens of thousands of stdout lines/day) | `log.debug` (+ the once-per-loop :1115 print → `log.info`); add `logging` to `loop_steps` |
| REL-31a | `/api/health` builds a fresh boto3 client per probe | module-level thread-safe cached client keyed on the 4 GARAGE_* env values |
| REL-31b | `download_stem` WAV-encodes under `state.lock` | copy audio under lock; encode + Response outside (extracted `_encode_wav_response` helper) |
| REL-32a | `ShowPlayback` broadcasts any WAV format as s16le (24-bit = noise) | per-chunk decode → float32 → s16le (sampwidth 1/2/3/4 + float-WAV scipy fallback) |
| REL-32b | WS broadcast sequential — one slow client head-of-line-blocks the topic | parallel per-client sends with per-send timeout; stale/timeout → drop subscriber |

**Cold (documented, deliberately NOT touched):** the ~30 other `print(` sites in `loop_steps.py` (REL-27 letter scopes `framework_generator.py`; only REL-29's pregen-wait print is U14's) · sample-RATE resampling in playback (see decision 7) · WS per-client ordering across overlapping broadcasts (unchanged semantics, see decision 9) · mixer, worker, claim SQL, capture schema, retention, fan-out, relay code · `docs/` + `refactor/` historical mentions of icecast (audit + plan docs are records, not live refs) · gitignored local `.env` / `docker/.env` (untracked; note in commit message).

---

## 1. Design decisions (documented reasoning; deviations from spec letter flagged)

1. **REL-26 is a deletion, not a deprecation** (spec decision 2026-09-11). The dead module is git-revivable; every dangling reference goes in ONE commit so the tree never sits in a half-wired state. The tracked grep surface is `app/`, `static/`, `tests/`, `docker/compose.yaml`, `README.md`, `.env.example` — `docs/` and `refactor/` keep their historical mentions (the audit doc is the authoritative finding text; rewriting it would falsify the record). The frontend toggle (`index.html` checkbox + `app.js` load/save) is part of the dead feature: the GET response key disappears, so the checkbox would read permanently-false — delete it with the feature (scout miss, folded in).
2. **REL-26 removal is API-surface-visible and sanctioned**: `GET /api/llm-config` drops the `icecast_enabled` key; `POST /api/llm-config` no longer accepts it (pydantic v2 ignores unknown keys, so a stale client POSTing it gets 200 + no-op — no 422 breakage). `test_api.py:706`'s assert is deleted LAST (it fails the moment the key leaves the response).
3. **REL-27a is the minimal structural fix**: keep the existing correct `finally: wav_path.unlink(missing_ok=True)` exactly as-is; just move the `wavfile.write` call inside the `try` so a raise on write (disk-full) flows through the finally. No new helper, no behavior change on the success path.
4. **REL-28 fixes BOTH loop paths with one shared helper.** The audit evidence cites the foreground serial loop, but `pregeneration.py:158–168` is the identical pattern (CLAUDE.md calls it "the pregen mirror"); fixing one and leaving the other duplicated would re-import the finding next time the mirror drifts (AGENTS.md: no code duplication). The helper does ONLY the fetching (bounded gather, order-preserving); cache writes and the `state.cache_stem` foreground/pregen divergence (brief-01 risk #4, pinned by `test_pregeneration_does_not_route_through_cache_stem` / `test_foreground_loop_routes_through_cache_stem`) stay in each caller's post-gather loop, untouched. Semaphore is created per call (no module-level event-loop binding hazards), sized from a monkeypatchable module constant, `JOB_PENDING_DEPTH_LIMIT` precedent.
5. **REL-29 converts exactly two prints**: the per-250 ms DEBUG line (:1123–1126) → `log.debug` (message text kept, `"DEBUG: "` prefix dropped — the level carries it), and the once-per-loop "Pre-generation complete" (:1115) → `log.info`. The rest of `loop_steps.py`'s prints are explicitly out of REL-27/29 scope (scout + §U14 letter) — no drive-by conversion.
6. **REL-31a cache is keyed on exactly the 4 env values the probe reads** (`GARAGE_ENDPOINT`, `GARAGE_ACCESS_KEY`, `GARAGE_SECRET_KEY`, `GARAGE_BUCKET`-with-default). No route in this app mutates those env vars at runtime (verified: `update_llm_config` only writes `state.*`; no `os.environ[...] =` for GARAGE anywhere in `app/`), so fingerprint-compare-per-probe IS the invalidation design — a changed env (restart, test monkeypatch) rebuilds on the next probe, no explicit flush hook. Thread-safety: `threading.Lock` (the probe already runs via `asyncio.to_thread`, config.py:126). boto3 clients are thread-safe for `head_bucket` reuse. The probe keeps its lazy `import boto3` (module import stays light) but now reuses `app.garage_client.build_boto3_config` with probe timeouts `S3Timeouts(connect_timeout=2, read_timeout=3, max_attempts=1)` — this shares only the pure botocore-Config builder, NOT the `GarageClient` adapter; the docstring's "decoupled from the storage adapter" intent (health never couples to GarageClient state) is preserved and the docstring updated to say so. **Deviation flagged:** same numeric timeouts as today (2/3/1), one behavior delta — `signature_version="s3v4"` + adaptive retry mode come with `build_boto3_config`; both are the project-wide S3 convention (every other S3 client in the tree uses it), and retries stay bounded at max_attempts=1.
7. **REL-32a converts FORMAT only, not sample rate.** The audit defect is garbage bytes (24-bit frames piped into a `-f s16le` chain). Fix: decode every chunk to float32, then `(np.clip(x, -1.0, 1.0) * 32767).astype("<i2")` — the exact mixer/stems.py convention (stems.py:86, `lib/wav.py` header). sampwidth 2 is an identity fast path (recordings this app produces are s16le — byte-for-byte unchanged output). sampwidth 1 (unsigned 8-bit), 3 (24-bit sign-extended unpack), 4 (int32) handled. Float32/format-3 WAVs: stdlib `wave` raises `wave.Error` at `open`, so a scipy.io.wavfile whole-file fallback (core dependency, no new dep) decodes float WAVs and chunks from memory — disclosed trade-off: non-PCM inputs load whole-file RAM (user-supplied files only; this app's recordings are s16le). Sample-RATE mismatch (48 kHz file → 44.1 kHz chain plays ~8.8 % fast) is OUT of scope: the audit's fix letter is "decode … then broadcast [s16le]", chunked resampling needs overlap state that is its own finding; documented as a residual.
8. **REL-31b keeps semantics identical, moves the work.** Under `state.lock`: set resolution, 404s, prompt lookup, and a `.copy()` of the numpy array (detaches from the LRU-shared buffer; the array is immutable-in-practice but a copy makes the outside-lock encode race-free by construction). Outside: the AUDIO-1 comment + the byte-identical clip→`<i2`→WAV→Response pipeline, extracted into `_encode_wav_response(audio, index)` (4–20-line rule; the current route body is ~45 lines). The `.copy()` mirrors `_mixer_thread_liveness`'s copy-under-lock-then-work-outside pattern.
9. **REL-32b: parallel bounded sends, no per-client queue.** One `asyncio.wait_for(ws.send_text(payload), WS_SEND_TIMEOUT_SECONDS)` per subscriber, all scheduled together via `asyncio.gather` (returns within ~one timeout even with a stuck client — never blocks the loop indefinitely); failure OR timeout marks the subscriber stale → discarded under `self._lock` (today's stale semantics, now also covering slow). The snapshot-before-iterate comment/behavior is preserved verbatim. Per-client ORDERING across overlapping broadcasts is unchanged from today: sequential sends inside one broadcast already interleave at every `await` when two broadcast coroutines overlap — a per-client lock/queue would add machinery for a guarantee the current code doesn't make (no speculative scaffolding). Same-topic broadcasts are awaited back-to-back by their callers in practice (state change → broadcast → next change).

---

## 2. Exact changes per file

### 2.1 REL-26 — deletion checklist (order matters; ONE commit)

1. `git rm app/framework/framework_icecast.py` (373 L) — `IcecastStreamer` + `create_icecast_streamer_from_env`, zero non-test callers.
2. `git rm tests/test_icecast.py` (360 L) — imports the module; contains the 2 skip-marked "missing broadcast_audio wiring" tests (:246/:285).
3. `tests/test_round3_fix_e.py` — delete header bullet `- E4 … framework_icecast.py …` (:9), `class DeadFfmpeg` (:298–306), `_icecast_with_dead_ffmpeg` (:308–316), `test_ffmpeg_death_clears_running_state` (:319–328), `test_ffmpeg_death_allows_restart` (:330–341). E5–E10 tests untouched.
4. `tests/test_io_timeouts.py` — delete docstring B4 bullets (:4–5) + the `state.icecast_streamer` note (:9–10), the import `from app.framework.framework_icecast import IcecastStreamer` (:21), and `class TestIcecastNoStderrDeadlock` (:178–222, 3 tests). B3/B5 Garage + AAC tests untouched.
5. `tests/test_adversarial_leftovers.py` — delete `class TestSec6IcecastLogRedaction` (:239–252).
6. `app/framework/framework_state.py` — delete `# Icecast` comment + `self.icecast_enabled = False` (:231–232).
7. `app/framework/state_slices.py` — `SessionConfig`: drop `"icecast_enabled"` from `_attrs` (:158) and fix the docstring (:150) → `"""Auth + audience message."""`.
8. `app/routes/schemas.py` — delete `icecast_enabled: bool | None = None` from `LLMConfig` (:66).
9. `app/routes/config.py` — delete `"icecast_enabled": state.icecast_enabled,` from the GET response (:398) and the `if config.icecast_enabled …` block (:413–414) from `update_llm_config`.
10. `static/mc-clanker/index.html` — delete the Icecast `setting-group` div (:607–611).
11. `static/mc-clanker/app.js` — delete `this.icecastEnabled = …` (:143), `this.icecastEnabled.checked = …` (:623), `icecast_enabled: this.icecastEnabled.checked,` (:1998).
12. `docker/compose.yaml` — delete `- ICECAST_ENABLED=${ICECAST_ENABLED:-false}` (:52).
13. `.env.example` — delete the ICECAST comment + `# ICECAST_ENABLED=false` (:56–57).
14. `README.md` — delete the features bullet (:29), the env-table row (:140), the `### Icecast Streaming (Optional)` section + paragraph (:150–152), and reword the diagram label (:244) `/stream.mp3  •  Icecast (optional)` → `/stream.mp3`.
15. `tests/test_mixer_resilience.py:94` — reword the docstring mention "Icecast/YouTube sink" → "downstream sink (YouTube relay)" (comment-only; keeps the grep surface clean).
16. LAST (it fails the moment step 9 lands): `tests/test_api.py:706` — delete `assert "icecast_enabled" in data`.

NOT touched: `.env` + `docker/.env` (gitignored local files — verified `git check-ignore`), `docs/reliability_audit.md`, `refactor/**` (historical records).

### 2.2 REL-27a — `app/aac_encoder.py` `encode_aac` (:105–133)

```python
    # Write temporary WAV file for ffmpeg to process
    # scipy.io.wavfile.write expects (samples, channels) float32 in [-1, 1]
    # REL-27a: the write sits INSIDE the try so a raise (e.g. disk full)
    # still flows through the finally unlink — the old layout orphaned the
    # temp file whenever wavfile.write failed.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = Path(f.name)
    try:
        wavfile.write(wav_path, sample_rate, audio)
        # Encode to AAC via ffmpeg (argv comment block unchanged)
        return _run_ffmpeg([...unchanged...], "AAC encoding")
    finally:
        wav_path.unlink(missing_ok=True)
```

Everything else in the function is byte-identical.

### 2.3 REL-27b — `app/framework/framework_generator.py` prints → logger

Add at module top: `import logging` … `log = logging.getLogger(__name__)` (after the torch imports, before `class ModelState`). Convert all 14 sites (message text preserved; redundant `"Warning: "` prefixes dropped — the level carries them):

| Line | Was | Now |
|---|---|---|
| :76 | print "Model not in cache, downloading..." | `log.info("[%s] Model not in cache, downloading...", self.repo_id)` |
| :80 | print "Loading model from cache: …" | `log.info("[%s] Loading model from cache: %s", self.repo_id, model_path)` |
| :101 | print "Warning: httpx client closed … Retrying..." | `log.warning("httpx client closed during model loading (attempt %d/%d). Retrying...", attempt + 1, max_retries)` |
| :122 | print "Loaded successfully." | `log.info("[%s] Loaded successfully.", self.repo_id)` |
| :147 | print "Generating stem i/n: '…'" | `log.info("[%s] Generating stem %d/%d: '%s'...", …)` |
| :195 | print "Config file … not found. Proceeding with empty registry." | `log.warning("Config file %s not found. Proceeding with empty registry.", self.config_path)` |
| :215 | print "Warning: Unknown engine type …" | `log.warning("Unknown engine type '%s' for model '%s'", engine_type, model_id)` |
| :279 | print "Warning: Requested model … Falling back to default …" | `log.warning("Requested model '%s' not loaded. Falling back to default '%s'.", …)` |
| :287 | print "Loading model … on-demand..." | `log.info("Loading model '%s' on-demand...", model_id)` |
| :311 | print "Warning: Mismatched sample rates …" | `log.warning("Mismatched sample rates between engines (%s vs %s). Mixer may distort.", common_sr, sr)` |
| :352 | print "Model loaded successfully." | `log.info("[%s] Model loaded successfully.", model_id)` |
| :356 | print "Failed to load model: {e}" | `log.error("[%s] Failed to load model: %s", model_id, e)` |
| :375 | print "Model unloaded." | `log.info("[%s] Model unloaded.", model_id)` |

Lazy %-formatting everywhere (structured-logging rule). No other edits to the module.

### 2.4 REL-28 — `app/framework/loop_steps.py` + `app/framework/pregeneration.py`

New module constant (next to `JOB_PENDING_DEPTH_LIMIT`, :100–117):

```python
# REL-28: bound on concurrent per-stem audio fetches (Garage GET + AAC
# decode each). One uncached batch is 4–6 stems; parallel fetch removes the
# serial 5–15 s/batch stall without hammering the object store. Module attr
# so tests monkeypatch it (JOB_PENDING_DEPTH_LIMIT precedent).
STEM_FETCH_CONCURRENCY = 4
```

New module-level helper (after the constants, before the class):

```python
async def gather_stem_audio(fetch, audio_paths, concurrency=None):
    """REL-28: fetch per-stem audio with bounded concurrency, order-preserving.

    ``fetch`` is the loop's ``_fetch_audio`` port delegate; ``audio_paths`` is
    a list aligned with the caller's pending_jobs (None ⇒ failed job). Returns
    audio-or-None per entry in the SAME order (asyncio.gather order guarantee);
    cache writes and outcome mapping stay with the caller so the
    foreground/pregen state.cache_stem divergence is untouched.
    """
    limit = STEM_FETCH_CONCURRENCY if concurrency is None else concurrency
    semaphore = asyncio.Semaphore(limit)

    async def fetch_one(path):
        if path is None:
            return None
        async with semaphore:
            return await fetch(path)

    return await asyncio.gather(*(fetch_one(p) for p in audio_paths))
```

`_step_await_jobs_fetch` results loop (:733–747) becomes:

```python
            # REL-28: fetch concurrently (bounded) — was one serial
            # Garage GET + AAC decode per stem. Cache writes + the
            # state.cache_stem routing stay here, per stem, AFTER the
            # gather (no await held across the lock).
            audio_paths = [results.get(job_id) for job_id, _, _ in pending_jobs]
            fetched = await gather_stem_audio(self._fetch_audio, audio_paths)
            for (job_id, orig_idx, cache_key), audio_data in zip(pending_jobs, fetched):
                if audio_data is not None:
                    self.stem_cache[cache_key] = {"audio_data": audio_data, "last_used": time.time()}
                    async with state.lock:
                        state.cache_stem(local_next_stems[orig_idx]["prompt"], audio_data)
                    outcomes[orig_idx] = "generated"
                else:
                    # Same failure print the serial loop had, distinguishing
                    # "job never completed" from "fetch returned None".
                    if results.get(job_id):
                        print(f"Job {job_id} fetch returned no audio")
                    else:
                        print(f"Job {job_id} failed or timed out")
                    outcomes[orig_idx] = "failed"
```

(The two failure prints stay `print` — they are loop_steps diagnostics outside REL-27/29 scope, like the rest of the module's prints.) Semantics per stem are IDENTICAL to today: same outcomes keys, same cache writes, same `state.cache_stem` under `state.lock`, keyed lookup so one stem's failure never shifts another's audio, and no lock is ever held across an await.

`app/framework/pregeneration.py` :158–168 — same replacement (`fetched = await gather_stem_audio(loop._fetch_audio, audio_paths)` imported from `loop_steps` alongside the existing :30 import), then its unchanged post-loop (`stem_cache` write only, NO `state.cache_stem` — divergence preserved). `stem_outcomes` mapping identical.

### 2.5 REL-29 — `app/framework/loop_steps.py` pregen wait

Add `import logging` (:28–34 import block) + `log = logging.getLogger(__name__)` after imports. Convert:
- :1115 `print(f"[AsyncLoop-{self._loop_idx}] Pre-generation complete, using results")` → `log.info("[AsyncLoop-%s] Pre-generation complete, using results", self._loop_idx)`
- :1123–1126 the `if self._loop_idx > 1: print(f"… DEBUG: current_ahead=…")` → `log.debug("[AsyncLoop-%s] current_ahead=%.2fs, waiting for pre-gen...", self._loop_idx, current_ahead)` — keep the `> 1` guard (first-loop silence), drop the literal `"DEBUG: "` prefix.

All other prints in the module stay (documented cold).

### 2.6 REL-31a — `app/routes/config.py` cached probe client

Module additions (near `_ping_object_store`):

```python
# REL-31a: /api/health used to build a fresh boto3 client per probe (env
# read + client construction on every request). boto3 clients are
# thread-safe; cache one keyed on the exact env tuple the probe reads, so a
# config change (restart / test env swap) rebuilds on the next probe.
_probe_client_lock = threading.Lock()
_cached_probe_client: tuple[tuple[str, str, str, str], object] | None = None


def _probe_env_fingerprint() -> tuple[str, str, str, str]:
    return (
        os.environ.get("GARAGE_ENDPOINT", ""),
        os.environ.get("GARAGE_ACCESS_KEY", ""),
        os.environ.get("GARAGE_SECRET_KEY", ""),
        os.environ.get("GARAGE_BUCKET", "mcclanker"),
    )


def _build_probe_s3_client(env):
    """Short-timeout probe client; shares only garage_client's pure Config builder."""
    import boto3

    from app.garage_client import S3Timeouts, build_boto3_config

    endpoint, key, secret, bucket = env
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        config=build_boto3_config(S3Timeouts(connect_timeout=2, read_timeout=3, max_attempts=1)),
    )


def _probe_s3_client():
    """Cached probe client, rebuilt when the GARAGE_* env fingerprint changes."""
    global _cached_probe_client
    env = _probe_env_fingerprint()
    with _probe_client_lock:
        cached = _cached_probe_client
        if cached is not None and cached[0] == env:
            return cached[1]
        client = _build_probe_s3_client(env)
        _cached_probe_client = (env, client)
        return client
```

`_ping_object_store` body: keep the `not_configured` early return; then `client = _probe_s3_client(); client.head_bucket(Bucket=env[3])` — read the bucket from the fingerprint (single source). Add `import threading` to the module imports. Update the docstring: "…uses a cached, thread-safe probe client (rebuilt when GARAGE_* env changes); shares only `garage_client`'s pure botocore-Config builder, never the storage adapter." Tests must reset `_cached_probe_client` to `None` (fixture) — the module global is the seam.

### 2.7 REL-31b — `app/routes/stems.py` `download_stem` (:52–98)

Extract (module level, above the route):

```python
def _encode_wav_response(audio_data, index: int) -> Response:
    """AUDIO-1: the cache stores float32 in [-1, 1] but the WAV header below
    declares 16-bit PCM: convert instead of writing raw float32 bit patterns,
    which players decoded as full-scale noise (review AUDIO-1). Same
    conversion as the mixer in framework_mixer.py. Runs OUTSIDE state.lock
    (REL-31b) on a copy taken under the lock.
    """
    pcm = (np.clip(audio_data, -1.0, 1.0) * 32767).astype("<i2")
    channels = int(pcm.shape[1]) if pcm.ndim == 2 else 1
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(44100)
        wf.writeframes(pcm.tobytes())
    return Response(
        content=buf.getvalue(),
        media_type="audio/wav",
        headers={"Content-Disposition": f"attachment; filename=stem_{index}.wav"},
    )
```

Route becomes: under `async with state.lock:` resolve set / index / stem / prompt / 404s, then `audio_data = state.last_generated_stems.get(prompt)`; 404 if None; `audio_copy = audio_data.copy()` (REL-31b: detach the numpy buffer from the LRU-shared array under the lock). Outside the lock: `return _encode_wav_response(audio_copy, index)`. Conversion math and headers byte-identical.

### 2.8 REL-32a — `app/playback.py` format-aware broadcast

Module additions (pure, numpy-only):

```python
import numpy as np  # at module top (torch-free; numpy is a core dep)


def _wav_chunk_to_float(data: bytes, sampwidth: int) -> np.ndarray:
    """Decode one interleaved int-PCM frame block to float32 in [-1, 1]."""
    if sampwidth == 1:  # WAV 8-bit PCM is unsigned
        u8 = np.frombuffer(data, dtype=np.uint8)
        return (u8.astype(np.float32) - 128.0) / 128.0
    if sampwidth == 3:  # 24-bit: pad to int32 with sign extension
        u8 = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3)
        pad = np.where(u8[:, 2] >= 0x80, np.uint8(0xFF), np.uint8(0x00))
        i32 = np.concatenate([u8, pad[:, None]], axis=1).view("<i4").ravel()
        return i32.astype(np.float32) / 8388608.0
    if sampwidth == 4:
        i32 = np.frombuffer(data, dtype="<i4")
        return i32.astype(np.float32) / 2147483648.0
    raise ValueError(f"Unsupported WAV sample width {sampwidth} (expected 1-4)")


def _float_block_to_s16le(block: np.ndarray) -> bytes:
    """Mixer-convention float→s16le (stems.py AUDIO-1 math; REL-21 NaN-safe)."""
    sane = np.nan_to_num(block.astype(np.float32), nan=0.0, posinf=1.0, neginf=-1.0)
    return (np.clip(sane, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def wav_chunk_to_s16le(data: bytes, sampwidth: int) -> bytes:
    """REL-32a: normalize any int-PCM WAV chunk to the s16le the broadcast
    chain assumes (youtube_relay `-f s16le`, stream fan-out, WAV sinks).
    sampwidth==2 (this app's recording format) passes through unchanged.
    """
    if sampwidth == 2:
        return data
    return _float_block_to_s16le(_wav_chunk_to_float(data, sampwidth))
```

`_playback_loop` change: `state.broadcast_audio(data)` (:90) → `state.broadcast_audio(wav_chunk_to_s16le(data, sampwidth))`. Plus a float-WAV (format 3) fallback — stdlib `wave` raises on open for non-PCM, so wrap the open:

```python
        try:
            with wave.open(self.audio_file_path, "rb") as wav_file:
                self._stream_wave(wav_file)
        except wave.Error:
            # REL-32a: stdlib wave rejects non-PCM (e.g. float32, format 3).
            # scipy.io.wavfile decodes them; whole-file in RAM is accepted for
            # these user-supplied inputs (this app records s16le only).
            self._stream_decoded_array()
        except Exception as e:
            print(f"Failed to open audio file for playback: {e}")
        finally:
            ...unchanged...
```

`_stream_wave(wav_file)` = the existing while-loop body verbatim (readframes → convert → broadcast → sleep; rewind-on-EOF; `is_playing` checks; Audit 4.2/4.3/5.4 comments preserved). `_stream_decoded_array()` = `from scipy.io import wavfile as scipy_wavfile` (lazy import), `sample_rate, audio = scipy_wavfile.read(path)`; frames_per_chunk from `audio` shape; loop slices `audio[i:i+n]` → `_float_block_to_s16le(np.asarray(chunk))` → `broadcast_audio` → `time.sleep(...)`, rewind by index reset on EOF. Both streamers 4–20 lines per function (split the chunk loop body if needed). Unsupported sampwidth (>4) raises inside conversion → caught by the existing per-chunk except → logged + break (existing behavior).

### 2.9 REL-32b — `app/routes/ws.py` broadcast

Add near the top: `WS_SEND_TIMEOUT_SECONDS = 5.0` with a comment (REL-32b: one slow subscriber must not head-of-line-block the topic; a send that exceeds this drops the subscriber). `ConnectionManager` gains one method + rewrites `broadcast`:

```python
    async def _send_to_subscriber(self, ws: WebSocket, topic: str, payload: str) -> bool:
        """One bounded send; False ⇒ stale (error or too slow — REL-32b drop)."""
        try:
            await asyncio.wait_for(ws.send_text(payload), timeout=WS_SEND_TIMEOUT_SECONDS)
            return True
        except Exception as exc:  # noqa: BLE001 — a bad socket must not kill the topic
            log.debug("WS send to topic=%s failed/timed out (%s); dropping subscriber", topic, exc)
            return False

    async def broadcast(self, topic: str, message: dict) -> None:
        """Send a JSON payload to all subscribers on a topic.

        REL-32b: sends run concurrently, each bounded by
        WS_SEND_TIMEOUT_SECONDS — one slow client can no longer
        head-of-line-block the topic; stale/timed-out subscribers are
        dropped like broken ones.
        """
        payload = json.dumps(message, default=str)
        # Snapshot the subscriber set BEFORE scheduling: sends await and yield
        # control, during which connect()/disconnect() can mutate the live set
        # and raise 'Set changed size during iteration'.
        conns = list(self._connections_sync(topic))
        results = await asyncio.gather(
            *(self._send_to_subscriber(ws, topic, payload) for ws in conns)
        )
        stale = [ws for ws, ok in zip(conns, results) if not ok]
        if stale:
            async with self._lock:
                for ws in stale:
                    self._connections[topic].discard(ws)
```

`_send_to_subscriber` reads the constant at call time via module attribute → tests monkeypatch `app.routes.ws.WS_SEND_TIMEOUT_SECONDS`. Everything else in the module unchanged.

---

## 3. TDD test plan (write red FIRST, per finding; all named fakes, no inline stubs)

New file **`tests/test_p3_hygiene.py`** (REL-26 pin, REL-27a, REL-27b pin, REL-29, REL-31a):

- **T1 (REL-26, acceptance "icecast refs gone — grep test")** `test_no_icecast_references_remain`: walk `app/`, `static/`, `tests/` (skip `__pycache__`/`node_modules`) for `*.py|*.js|*.html|*.yaml|*.yml|*.md` plus the files `docker/compose.yaml`, `README.md`, `.env.example`; assert no file content contains `"icecast"` case-insensitively; on failure the assert message lists offenders (`path:line`). Docs/refactor dirs deliberately excluded (historical records).
- **T2 (REL-27a, acceptance "wavfile.write raising still unlinks temp")** `test_encode_aac_unlinks_temp_wav_when_write_raises`: monkeypatch `aac_encoder.wavfile.write` with a named fake that records the path then raises `OSError("disk full")`; seed `tempfile.tempdir` to `tmp_path` (or assert on the recorded path); `with pytest.raises(OSError): encode_aac(zeros((4410, 2), float32))`; assert `not recorded_path.exists()`.
- **T3 (REL-27a control)** `test_encode_aac_unlinks_temp_wav_on_ffmpeg_failure`: existing suite already pins the ffmpeg-failure path — keep green (regression only).
- **T4 (REL-27b pin, acceptance evidence for prints→logger)** `test_framework_generator_has_no_print_calls`: AST-walk `app/framework/framework_generator.py`; assert no `ast.Call` whose func is Name/Attribute `"print"`. (Source guard; the behavioral conversion is pinned by T5.)
- **T5 (REL-27b behavior)** `test_generator_logs_unknown_engine_via_logger(caplog)`: with `caplog.at_level(logging.WARNING, logger="app.framework.framework_generator")`, drive `GeneratorRegistry` config-load with an unknown engine entry (existing test_generator.py fixtures); assert the warning record exists and `capsys.readouterr().out == ""`.
- **T6 (REL-29, acceptance "pregen wait emits no stdout (caplog debug instead)")** `test_pregen_wait_logs_debug_and_prints_nothing(caplog, capsys)`: build the loop harness (fake mixer whose `loop_position_seconds()` returns 10.0 then 0.4 → breaks after ≤2 iterations; `loop.running=True`; `_pregen_done` clear; `_loop_idx=2`); `await loop._step_await_pregen()`; assert `caplog` (at DEBUG for `app.framework.loop_steps`) contains `current_ahead=` record; assert `capsys.readouterr().out == ""`. (~0.5 s runtime from the real 0.25 s sleeps — acceptable; no global asyncio patch.)
- **T7 (REL-31a, acceptance "health probe reuses client (boto3 call count via fake)")** `test_ping_object_store_reuses_cached_client`: inject a named `FakeBoto3Module` (MagicMock module whose `.client` returns fresh `FakeS3Client` with working `head_bucket`) via `monkeypatch.setitem(sys.modules, "boto3", fake)`; `monkeypatch.setenv` the 4 GARAGE vars; reset `config._cached_probe_client = None`; call `_ping_object_store()` twice → assert `fake.client.call_count == 1` and both probes `"ok"`.
- **T8 (REL-31a invalidation)** `test_ping_object_store_rebuilds_client_on_env_change`: same harness; probe → change `GARAGE_ENDPOINT` via `monkeypatch.setenv` → probe again → assert `fake.client.call_count == 2` and the second client got the new endpoint (fake records `endpoint_url` kwarg).
- **T9 (REL-31a thread-safety smoke)** `test_probe_client_cache_is_lock_serialized`: 8 threads calling `_probe_s3_client()` concurrently with a slow-ish fake client factory (records entry count); assert exactly 1 construction (the lock prevents a thundering build). Bounded with `ThreadPoolExecutor` + `as_completed` timeout.

New file **`tests/test_stem_fetch_concurrency.py`** (REL-28):

- **T10 (acceptance "N stems complete in ~1 batch not N")** `test_step_await_jobs_fetch_runs_concurrently`: loop harness (`AsyncFrameworkLoop(session_id, jobs=fake_jobs)`); `loop._await_jobs = AsyncMock(return_value={job_id: f"audio/{job_id}.aac" …})`; named `FakeSlowFetch` fake — `async def __call__(path)` sleeps 0.15 s, tracks `max_in_flight`, returns deterministic `zeros((2, 2), float32)` per path; `loop._fetch_audio = fake`; 6 pending_jobs `(job_id, orig_idx, cache_key)`; `await loop._step_await_jobs_fetch(pending_jobs, local_next_stems)`; assert wall time < 6×0.15 (≈ 2 batches at concurrency 4) and `fake.max_in_flight <= 4` and `fake.max_in_flight >= 2` (proves overlap).
- **T11 (acceptance "results in stem order")** `test_fetch_results_map_to_their_own_stems`: `FakeVariedFetch` returns per-path distinct arrays (and `None` for one path, a missing `results` entry for another); assert `stem_cache[cache_key]` holds each path's exact array (keyed lookup — a short/None result for one job never shifts another's audio), `outcomes == {0: "generated", 1: "failed", 2: "failed", 3: "generated"}`, and `state.last_generated_stems` got ONLY the foreground stems' prompts (cache_stem routing preserved).
- **T12 (semaphore respected)** `test_fetch_concurrency_respects_semaphore`: monkeypatch `loop_steps.STEM_FETCH_CONCURRENCY = 2`; 6 fetches; assert `max_in_flight <= 2`.
- **T13 (pregen mirror)** `test_pregen_path_uses_the_same_bounded_gather`: monkeypatch `loop_steps.gather_stem_audio` with a recorder; drive `run_pregeneration` (existing `test_pregeneration_divergence.py` harness); assert the recorder was reached AND `state.cache_stem` NOT called (divergence pin stays green with the new wiring).
- **T14 (existing pins stay green)** `tests/test_pregeneration_divergence.py` + `tests/test_async_framework.py` run unchanged — the `_fetch_audio` patch seam is untouched.

**`tests/test_api.py`** (REL-31b, add next to the existing stem-download tests :141–153):

- **T15 (acceptance "download encodes outside lock — fake lock contention observable")** `test_download_stem_encodes_outside_state_lock(client)`: seed `state.active_stems` + `state.last_generated_stems[prompt]`; monkeypatch `app.routes.stems._encode_wav_response` with a named fake that records `state.lock.locked()` at call time then returns the real result (delegate to the original); `client.get("/api/stems/0/download")`; assert status 200 and `captured["locked"] is False`.
- **T16 (copy-under-lock)** `test_download_stem_copies_audio_before_leaving_lock`: the T15 fake also asserts the array it received is not the same object as the cached one (`arr is not state.last_generated_stems[prompt]`) — mutation outside the lock can never reach the LRU-shared buffer.
- **T17 (behavior unchanged)** existing `test_download_stem_success` / `_previous_set` / `_next_set` / `_out_of_range` / `_not_found` must stay green byte-for-byte (headers + media type pinned by them).

New file **`tests/test_playback_format.py`** (REL-32a):

- **T18 (pure converter)** `test_wav_chunk_to_s16le_all_widths`: known values — 16-bit identity (bytes out == bytes in); 24-bit `0x7FFFFF` → `32767`, `0x800000` → `-32768`, `0x000000` → `0`; int32 full-scale → ±32767; 8-bit `0x00/0x80/0xFF` → `-128/0/127`-ish float mapping → sane int16s. Direct calls to `wav_chunk_to_s16le`.
- **T19 (acceptance "24-bit WAV broadcast as valid s16le")** `test_playback_24bit_wav_broadcasts_s16le(tmp_path)`: write a real 24-bit 44.1 k stereo WAV via `wave` (`setsampwidth(3)`), monkeypatch `state.broadcast_audio` with a named `RecordingBroadcaster` fake that stores chunks and flips `pb.is_playing = False` after the first; run `ShowPlayback(1, path)._playback_loop()`; assert the captured chunk length is even, `int16` reinterpretation is within ±32767 with no wrap-garbage, and matches `_float_block_to_s16le` of the source block.
- **T20 ("48k")** `test_playback_48k_int16_wav_passes_through`: 48 k s16le WAV → broadcast bytes are byte-identical to the file's data chunk (identity fast path + chunking math honored at a non-44.1 k framerate).
- **T21 ("float WAV")** `test_playback_float32_wav_broadcasts_valid_s16le(tmp_path)`: write a format-3 float32 WAV via `scipy.io.wavfile.write`; same fake broadcaster; assert broadcast chunks decode as sane int16 (e.g. a 0.5-amplitude sine → ≈ 16383 peak) — proves the scipy fallback path.
- **T22 (unsupported width)** `test_wav_chunk_to_s16le_rejects_unknown_width`: `pytest.raises(ValueError, match="sample width")` for sampwidth 5 (message includes the offending value per AGENTS.md).

**`tests/test_websocket.py`** (REL-32b, extend `TestConnectionManager`):

- **T23 (acceptance "slow ws client does not delay others")** `test_slow_subscriber_does_not_block_topic`: named fakes — `FastFakeWS.send_text` records `time.monotonic()` and returns; `SlowFakeWS.send_text` sleeps 1.0 s; register both directly into `ws_manager._connections["state"]`; `monkeypatch.setattr("app.routes.ws.WS_SEND_TIMEOUT_SECONDS", 0.1)`; `t0 = monotonic(); await ws_manager.broadcast("state", {"type": "ping"}); elapsed = …`; assert fast fake received the payload, `elapsed < 0.6` (slow send ran in parallel and was cut at the timeout), and the slow fake was discarded from `_connections["state"]` while the fast one remains.
- **T24 (stale error path preserved)** `test_broadcast_drops_failing_subscriber`: `BrokenFakeWS.send_text` raises `RuntimeError`; assert discarded from the set after broadcast (today's semantics, now via the same helper).
- **T25 (snapshot preserved)** `test_broadcast_snapshots_before_scheduling`: a fake whose `send_text` calls `ws_manager.disconnect(other, topic)` mid-send; assert no `RuntimeError: Set changed size during iteration` escapes and broadcast completes (pin for the preserved comment).

Suite delta: −24 collected (18 in `test_icecast.py` [16 passed + 2 skipped], 3 `TestIcecastNoStderrDeadlock`, 1 `TestSec6IcecastLogRedaction`, 2 round-3 E4 tests) + 25 new → target landing state: **1168 passed / 16 skipped / 0 failed** (1184 collected; exact numbers confirmed at execution — the 2 deleted skips are the icecast `broadcast_audio`-wiring placeholders the spec says die with the module).

---

## 4. Execution order (TDD, each step leaves the suite green)

1. **RED T2 → fix 2.2** (REL-27a; isolated, zero deps). Green.
2. **RED T4+T5 → fix 2.3** (REL-27b). Green.
3. **RED T6 → fix 2.5** (REL-29; adds `logging` import loop_steps will also need for nothing else — helper 2.4 doesn't log). Green.
4. **RED T10–T13 → fix 2.4** (REL-28, both paths in one step; run `test_pregeneration_divergence.py` + `test_async_framework.py` + `test_jobs_await_injection.py` immediately after). Green.
5. **RED T7–T9 → fix 2.6** (REL-31a). Green.
6. **RED T15–T17 → fix 2.7** (REL-31b). Green.
7. **RED T18–T22 → fix 2.8** (REL-32a). Green.
8. **RED T23–T25 → fix 2.9** (REL-32b). Green.
9. **REL-26 deletion in §2.1's exact order** (module → test files/classes → state/slice/schema/route/static/compose/env/README/mixer-comment → `test_api.py:706` LAST), then **RED-then-green T1** (the grep pin goes red the moment the module still exists — write it first, watch it fail, delete, watch it pass).
10. Full preflight (`ruff` + full pytest), update the status table row for unit 14 in `rel-remediation-plan.md` + this file's landing note; commit on branch `rel-26-32-p3`.

Commit granularity: one commit per finding group (27a+27b+29 hygiene, 28, 31a+31b, 32a, 32b, 26-deletion) or a single unit commit — follow the repo's recent pattern (single unit commit with the status-table update).

---

## 5. Acceptance (per spec §U14: "focused unit tests; icecast refs gone (grep clean); suite green")

- `grep -ri icecast app/ static/ tests/ docker/compose.yaml README.md .env.example` → **empty** (T1 pins it forever).
- `.venv/bin/python -m pytest tests/ -q` → **0 failed**, skips only where pre-existing minus the deleted icecast skips.
- `.venv/bin/python -m ruff check app tests` → clean (baseline was clean; do not regress).
- Each of the task's 8 TDD acceptance lines maps to: T1 / T2 / T10+T11 / T6 / T7+T8 / T15 / T19+T20+T21 / T23.
- No `git status` staged leftovers; `git rm` used for the two deleted files.

## 6. Risks / residuals

- **REL-26 API change** (GET response key gone) is client-visible; sanctioned by the spec decision (dead feature); stale clients POSTing the key get a silent no-op (pydantic ignores unknowns).
- **REL-28** holds no lock across an await (semaphore + gather only; `state.cache_stem` stays a short locked write per stem after the gather) — invariant 1 preserved. Pregen divergence untouched (T13/14 pin).
- **REL-31a** shares `build_boto3_config` (adds s3v4 + adaptive retry mode to the probe vs the old bare `Config`) — same project-wide convention as every other S3 client; timeouts stay 2/3/1.
- **REL-32a** does NOT resample: a 48 k file broadcasts valid s16le but plays ~8.8 % fast through the 44.1 k chain (documented residual; recordings are 44.1 k). Float-WAV fallback loads whole-file into RAM (user-supplied non-PCM only).
- **REL-32b** per-client ordering across OVERLAPPING broadcasts was never guaranteed (sequential awaits already interleave); unchanged, documented — no per-client queue added (no speculative scaffolding). A timed-out send leaves the socket half-written; we drop the subscriber and the endpoint's receive loop reaps it.
- `.env` / `docker/.env` are gitignored local files that still contain commented ICECAST lines — untouched (untracked); noted in the commit message.

## 7. Landing checklist

- [ ] T1–T25 green, suite green, ruff clean
- [ ] grep clean over the T1 surface
- [ ] status table row "14 rel-p3-hygiene | **landed** (…/…s green)" + commit SHA in `rel-remediation-plan.md`
- [ ] this file's §5 checkboxes ticked with the actual pass/skip counts
