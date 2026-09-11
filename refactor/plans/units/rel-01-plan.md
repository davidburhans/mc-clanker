# PLAN — Unit 1 `rel-mixer-resilience` (REL-01 + REL-21), branch `rel-01-mixer`
**Spec:** `refactor/plans/rel-remediation-plan.md` §U1 · `docs/reliability_audit.md` REL-01 (Critical), REL-21 (P2)
**Baseline gate:** `e125167` — 928 passed / 16 skipped; do not regress.
---
## 0. Scope summary
| Finding | Root cause | Fix site |
|---|---|---|
| REL-01 | `_stream_loop` calls `_callback` with no guard → one exception kills the audio thread forever while `is_running`/`is_generating` stay green; no `is_alive()` anywhere | `framework_mixer.py` `_stream_loop` + `/api/health` payload |
| REL-21 | `np.clip` preserves NaN; `astype("<i2")` of NaN = platform-defined garbage → one bad stem poisons the whole mix for a loop | `framework_mixer.py` both broadcast sites + `aac_encoder.py::_normalize_decoded_audio` float branch |
No behavior change on the happy path. Worker, LLM capture, storage untouched (invariants 4–5 irrelevant here).
---
## 1. Design decisions (documented deviations from spec letter)
1. **Guard shape: no `continue`.** Spec says `except: log.exception(); outdata.fill(0); continue`. A literal `continue` skips the monotonic-deadline/sleep bookkeeping that follows the callback call → under a *persistent* failure the loop busy-spins, emitting `log.exception` at max rate — violating invariant 3 (nothing heavy on the audio path). Decision: wrap **only** the `self._callback(...)` call; the except branch does `log.exception` + `outdata.fill(0)` and **falls through** to the shared deadline/sleep logic, so tick cadence (~46 ms) and the catch-up branch (`deadline = time.monotonic()` when late) remain intact. Spec intent (thread survives, zeros emitted, timing preserved) fully honored.
2. **No re-broadcast of zeros in the except branch.** `_callback` broadcasts internally as its last step; if it raised mid-tick, one ~46 ms chunk is dropped (stream hiccup / recording hole), bounded per failure. Re-broadcasting would need a second nested try/except (the broadcast may itself be the failing call) — spec says fill+continue only. The failure is loudly logged instead.
3. **Liveness plumbing: `state.mixer_thread`.** Chosen over a module-level mixer registry because `state` is the codebase's shared-state seam and `state.framework_task` (set by lifespan) sets the precedent. Written by `Mixer.start()` / cleared by `Mixer.stop()` under `state.sync_lock` (both are invoked from the event-loop thread via `AsyncFrameworkLoop.start/stop/_finish_loop` — never from the audio thread). Deliberately **not** cleared by `state.reset()` (same rationale as `youtube_relay`: a musical reset must not confuse process-liveness reporting; the mixer thread survives `reset()`).
4. **Payload semantics: `"mixer_alive": bool | None`.** `None` = no thread registered (never started / cleanly stopped); `False` = registered thread died without `stop()` — exactly REL-01's silent-death signature, now observable while `is_running` stays `true`. `"status"` stays `"healthy"` per the `config.py` docstring contract (degradation goes into fields; verified no test asserts an exact key set — additive key is safe, `test_health_check` only asserts `status` + `is_running`).
5. **Liveness computed in `routes/config.py`**, next to `_ping_database`/`_ping_object_store` (health concerns colocated; avoids adding a method to the already-557-line `framework_state.py`). Pattern mirrors `broadcast_audio`: copy the thread reference under `sync_lock`, call `is_alive()` (non-blocking C call) **outside** the lock so a health probe can never delay an audio tick.
6. **Mixer sanitization via one shared static helper** (AGENTS.md: no duplication — the two `np.clip` sites are byte-identical). `np.nan_to_num(x, nan=0.0, posinf=1.0, neginf=-1.0)` **before** `np.clip` — sanitization must precede clip because clip propagates NaN. O(blocksize) vectorized math, same cost order as the existing clip → invariant 3 safe.
7. **AAC: sanitize only the float default branch.** int16/int32 branches cannot contain NaN/Inf; wrapping all returns would be dead code. Pre-existing non-normalization of uint8 WAVs is out of scope (REL-21 is NaN/Inf poisoning only).
---
## 2. Exact changes per file
### 2.1 `app/framework/framework_state.py` (+4 lines → 561)
In `__init__`, next to the lifespan-set `framework_task` reference:
```python
        # Mixer render thread (registered by Mixer.start, cleared by Mixer.stop) —
        # exposes is_alive() liveness to /api/health (REL-01). Guarded by
        # sync_lock; deliberately NOT cleared by reset() (see youtube_relay).
        self.mixer_thread = None
```
`reset()` untouched. `threading` already imported.
### 2.2 `app/framework/framework_mixer.py` (~+18 lines → ~441; headroom OK)
**(a)** New static helper beside `_ensure_stereo`:
```python
    @staticmethod
    def _sanitize_pcm_block(outdata: np.ndarray) -> np.ndarray:
        """NaN/Inf-safe float block, clamped to [-1, 1] (REL-21).
        np.clip preserves NaN and astype("<i2") of NaN is platform-defined
        garbage — sanitize first so one poisoned stem can't corrupt the
        broadcast PCM. Vectorized O(blocksize); safe for the audio tick.
        """
        return np.clip(np.nan_to_num(outdata, nan=0.0, posinf=1.0, neginf=-1.0), -1.0, 1.0)
```
**(b)** Replace both broadcast sites (not-generating early path ~L218 and main path ~L386):
```python
        pcm_out = self._sanitize_pcm_block(outdata)
        state.broadcast_audio((pcm_out * 32767).astype("<i2").tobytes())
```
**(c)** `_stream_loop` — guard the tick (deadline/sleep logic untouched below it):
```python
        while self._running:
            deadline += sleep_time
            # REL-01: one exploding tick must not kill the render thread —
            # log, emit silence for this block, keep the deadline cadence.
            # (No `continue`: skipping the sleep would busy-spin a
            # persistently-failing callback at max log rate.)
            try:
                self._callback(outdata, self.blocksize, None, None)
            except Exception:
                log.exception("Mixer render tick failed; emitting silence")
                outdata.fill(0)
            # Sleep only the *remaining* time until the next deadline.
            remaining = deadline - time.monotonic()
            ...
```
**(d)** `start()` — register the thread before starting it:
```python
    def start(self):
        self._running = True
        self._stream_thread = threading.Thread(target=self._stream_loop, daemon=True, name="Mixer")
        with state.sync_lock:
            state.mixer_thread = self._stream_thread
        self._stream_thread.start()
        log.info("Audio stream loop started")
```
**(e)** `stop()` — clear the registration only if it still points at this mixer's thread (identity check keeps a racing new `start()` registration intact):
```python
    def stop(self):
        self._running = False
        if self._stream_thread is not None:
            self._stream_thread.join(timeout=2.0)
        with state.sync_lock:
            if state.mixer_thread is self._stream_thread:
                state.mixer_thread = None
        with self.lock:
            self.tracks = []
        log.info("Mixer stopped")
```
### 2.3 `app/aac_encoder.py` (+3 lines → ~199)
`_normalize_decoded_audio` default branch (+ docstring note):
```python
    if audio.dtype == np.int32:
        return audio.astype(np.float32) / 2147483648.0
    # REL-21: float WAVs can carry NaN/Inf (corrupt stem); astype preserves
    # them and downstream clip would too. Int branches cannot contain NaN.
    return np.nan_to_num(audio.astype(np.float32), nan=0.0, posinf=1.0, neginf=-1.0)
```
### 2.4 `app/routes/config.py` (~+16 lines → ~390)
**(a)** Helper beside the other probes:
```python
def _mixer_thread_liveness() -> bool | None:
    """Mixer render-thread liveness for /api/health (REL-01).
    None = no thread registered (never started or cleanly stopped); False =
    registered thread died without stop() — the silent-death state REL-01
    makes observable. Copy the reference under sync_lock, then call
    is_alive() outside it so the ~46 ms audio tick never waits on a probe.
    """
    with state.sync_lock:
        mixer_thread = state.mixer_thread
    return None if mixer_thread is None else mixer_thread.is_alive()
```
**(b)** `health_check()` payload gains one key (status/shape otherwise unchanged):
```python
    return {
        "status": "healthy",
        "is_running": is_running,
        "mixer_alive": _mixer_thread_liveness(),
        "ready": checks["ready"],
        ...
    }
```
---
## 3. TDD regression tests (write first, confirm red, then implement)
### 3.1 New file `tests/test_mixer_resilience.py` (~190 lines) — the unit's acceptance suite
Autouse fixture mirrors `test_mixer.py` (`state.reset(); state.is_generating = True; state.active_stems = [...]`) + a local `client` fixture (`TestClient(app)`, pattern from `test_api.py:13-18`).
| Test | Pins | Core assertions |
|---|---|---|
| `test_stream_loop_survives_raising_callback` (REL-01, **deterministic**) | Guard | `m.blocksize = 512`; replace `m._callback` with a fake that writes `0.7` into `outdata` then raises on tick 1, snapshots `outdata.copy()` each tick, and sets `m._running = False` on tick 5. Call `m._stream_loop()` synchronously (pattern: `test_stream_loop_catchup_path`). Assert `len(ticks) == 5` (loop survived the exception) and `np.allclose(ticks[1], 0.0)` (silence carried into the next tick — spec: "emits zeros afterwards"). |
| `test_stream_loop_survives_broadcast_failure` (REL-01, thread-level) | Real path | `Mixer(channels=1)` + real track; `patch.object(state, "broadcast_audio")` whose side effect raises `RuntimeError` on the first 2 calls (pattern: `test_mixer_extended.py:174-179`). `m.start(); time.sleep(0.3)` (≈6 ticks; failures land in first ~92 ms). Assert `m._stream_thread.is_alive()` **mid-run**, record call count, `m.stop()`, assert count grew past the failures and `m.current_sample > 0`. |
| `test_mixer_start_registers_thread_on_state` | Lifecycle | `m.start()` → `state.mixer_thread is m._stream_thread`; `m.stop()` → `state.mixer_thread is None`. |
| `test_callback_sanitizes_nan_stem_to_finite_pcm` (REL-21, e2e) | Generating path | Add a track whose audio mixes `[nan, inf, -inf, 0.5]` rows; patch `state.broadcast_audio` to capture bytes; call `m._callback(outdata, N, None, None)`; decode `np.frombuffer(pcm, dtype="<i2")`. Assert finite, NaN→`0`, `+inf`→`32767`, `-inf`→`-32767`, `0.5`→`16383`. |
| `test_callback_not_generating_sanitizes_broadcast` (REL-21, site 1) | Dedup site | Same capture pattern with `state.is_generating = False`; assert broadcast int16 decodes to all zeros (site 1 routes through the same helper). |
| `test_sanitize_pcm_block_maps_nan_and_infinity` (REL-21, unit) | Helper | Direct call on a 2×2 float32 array `[[nan, inf], [-inf, 0.5]]` → exactly `[[0, 1], [-1, 0.5]]`, `np.isfinite(...).all()`, dtype float32. |
| `test_health_reports_mixer_lifecycle` (REL-01) | Route payload | `Mixer().start()` → `GET /api/health` → `data["mixer_alive"] is True` (and `"status" == "healthy"`); `m.stop()` → `mixer_alive is None`. |
| `test_health_detects_dead_mixer_thread` (REL-01, the money shot) | Degradation visibility | Create + join a throwaway thread, register it via `with state.sync_lock: state.mixer_thread = dead_thread` (finally: restore `None`); `GET /api/health` → `mixer_alive is False` while `is_running is True` — the exact signature REL-01 hid. Needs `import threading`. |
### 3.2 `tests/test_api.py` (+1 line)
Extend existing `test_health_check` (L326-334) with `assert "mixer_alive" in data` — pins the contract where health tests live.
### 3.3 `tests/test_io_timeouts.py` (+~20 lines, no ffmpeg needed)
New class `TestNormalizeDecodedAudioSanitization` (outside the `skipif(no-ffmpeg)` class, next to it):
- `test_normalize_decoded_audio_sanitizes_float_nan_inf` — float32 **and** float64 inputs containing `[nan, inf, -inf, 0.25]` → dtype float32, `np.isfinite().all()`, exact mapping `0.0 / 1.0 / -1.0 / 0.25`.
- `test_normalize_decoded_audio_int_branches_unchanged` — int16 `[-32768, 32767]` still maps to `[-1.0, ≈0.99997]` (guards against over-eager sanitization breaking scale).
### 3.4 TDD order
1. Add all tests above → run `.venv/bin/python -m pytest tests/test_mixer_resilience.py tests/test_io_timeouts.py::TestNormalizeDecodedAudioSanitization tests/test_api.py::test_health_check -q` → **red** (A1 errors with the propagated `RuntimeError`; A2 fails `is_alive()`; health tests fail on missing `mixer_alive`; NaN tests fail on garbage int16 / non-finite floats).
2. Implement §2.1 → §2.4 → same command → **green**.
3. Full gate: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q` → 936 passed / 16 skipped (928 + 8 new + 2 new), zero regressions.
---
## 4. Invariant compliance (plan §Invariants)
| # | How respected |
|---|---|
| 1 — lock discipline | `sync_lock` holds are copy/assign only (µs, no I/O): `start()`/`stop()` write the thread ref; `_mixer_thread_liveness()` copies the ref and calls `is_alive()` **outside** the lock; `broadcast_audio`'s snapshot-then-act pattern mirrored. Health handler keeps its existing short `state.lock` read; no framework function called under any lock. The audio tick itself takes no new locks. |
| 2 — style / hexagonal | `_sanitize_pcm_block` 3-line body, `_mixer_thread_liveness` 4-line body; explicit `np.ndarray`/`bool | None` types, no `Any`; no duplication (shared helper replaces two identical sites). File budgets: framework_mixer 441, config 390, aac_encoder 199 (<500). framework_state 557→561 (already over budget, brownfield debt — split out of scope, noted). Ports/`MixerController` surface untouched; factory injection unaffected (fake mixers that never call `Mixer.start()` simply leave `mixer_alive` `None`). |
| 3 — audio thread | Happy path adds one vectorized `np.nan_to_num` per tick (µs, same order as existing clip) — no I/O, no locks, no logging. Failure path only: `log.exception` + `fill(0)`; **no** busy-spin (decision 1), **no** re-broadcast (decision 2). |
| 4/5 — LLM capture / worker | No code in those paths is touched. |
| 6 — regression per fix | 8 new mixer/resilience tests + 2 AAC tests + 1 health-contract extension, each mapping 1:1 to a spec acceptance clause. |
---
## 5. Acceptance checklist (maps to §U1 spec)
- [ ] A raising `_callback` survives N ticks and emits zeros afterwards → 3.1 tests 1–2
- [ ] NaN-containing stem buffers produce finite output → 3.1 tests 4–6 + 3.3
- [ ] Health payload includes thread liveness → 3.1 tests 7–8 + 3.2
- [ ] `ruff check` clean; full suite 936 passed / 16 skipped (~7–8 s; A2 adds ~0.35 s)
## 6. Risks / out of scope
- **Timing-based A2** uses generous sleep margins (2 failures vs ~6 ticks); deterministic A1 is the primary pin, so CI jitter cannot hide a regression.
- Pre-fix NaN→int16 garbage is platform-defined (x86-64: `0x8000`); the red-phase failure message may show `-32768` — that *is* the poison being pinned.
- One dropped ~46 ms broadcast chunk per failing tick (spec-accepted tradeoff, decision 2).
- REL-31 (`/api/health` boto3 client caching) is U14's, not this unit's — `_ping_object_store` untouched.
- Follow-up noted for U15: the soak harness (audit soak #2) will re-cover fault survival at scale with `raise every k-th tick`.
