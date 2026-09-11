# Round 3 — Adversarial Bug Hunt: state / slices / mixer / ws / stems / config lanes

Scope: `app/framework/framework_state.py`, `app/framework/state_slices.py`,
`app/framework/framework_mixer.py`, `app/routes/ws.py`, `app/routes/stems.py`,
`app/routes/config.py` (+ the one schema and one middleware site they feed into).
Baseline: `git log --oneline -30` at `04791e4`; full suite green
(`741 passed, 16 skipped` in 5.43s). Round-1/2 findings and regression pins
(`test_adversarial_wave1/2.py`, `test_adversarial_leftovers.py`,
`test_concurrency_fixes.py`, `test_loop_lock_safety.py`,
`test_framework_characterization.py`) were read first; nothing below re-reports
them. Proofs ran via `/tmp` scripts against the repo venv; the repo was not modified.

---

## CONFIRMED BUGS

### 1. HIGH | `app/routes/stems.py:33` (+ `app/routes/schemas.py:78`) | Unvalidated stem volume: NaN/±Inf/1e308 accepted → entire mixed stream becomes silence (or a full-scale blast) and stays corrupted

- **Mechanism:** `update_stem_volume` stores `update.volume` verbatim:
  `state.stem_volumes[index] = update.volume` (stems.py:33).
  `StemVolumeUpdate.volume` is a bare `volume: float` (schemas.py:78) — pydantic v2
  allows NaN/Infinity by default and Starlette's JSON parser accepts the
  `NaN`/`Infinity` literals. The mixer applies the value directly as per-stem gain
  (`indiv_gain = volumes.get(track.stem_index, 1.0)` → `total_gain`,
  framework_mixer.py:340-344) and sums it into `outdata`; the block is then
  `np.clip(...)` + `(pcm_out * 32767).astype("<i2")` (framework_mixer.py:368-369).
  - NaN gain → every overlapped output sample becomes NaN (0 + NaN = NaN), and the
    float→int16 cast of NaN is a constant (measured: all `0`) → **total digital
    silence** for every WS/MP3 listener and any in-flight show/export recording.
  - `Infinity`/`1e308` gain → every sample clips to ±1.0 → **constant full-scale
    blast** (measured PCM: all `32767`).
  Because the auto-tile safety net keeps the stem audible indefinitely, the
  corruption persists on every subsequent tick until another valid volume write.
- **Trigger:** `POST /api/stems/0/volume` with raw body `{"volume": NaN}` (or
  `Infinity`, `1e308`, `-5`). The DJ gate for `/api/stems` is
  `(is_dj_route and dj_pass and ...)` (app_ui.py:184-192), so with the default
  empty `DJ_PASSWORD` any local/LAN peer can send it. Contrast: round-2 bounded
  `GenerationConfig` for exactly this class of absurd-value hazard
  (schemas.py:71-74), `StemVolumeUpdate` was missed; CLAUDE.md documents the
  `0.0–2.0` gain contract.
- **Impact:** one request silently destroys the product's entire output (the audio
  stream + recordings) until manual intervention; no crash, no log (broadcast
  errors aren't raised).
- **Minimal fix:** `volume: float = Field(..., ge=0.0, le=2.0)` on
  `StemVolumeUpdate` (ge/le reject NaN and Inf), matching the GenerationConfig
  precedent.
- **Proof:**
  ```
  $ .venv/bin/python - <<'EOF'
  from app.routes.schemas import StemVolumeUpdate
  for raw in ['{"volume": NaN}', '{"volume": Infinity}', '{"volume": 1e308}', '{"volume": -5}']:
      print(raw, "-> accepted:", StemVolumeUpdate.model_validate_json(raw).volume)
  # all four accepted (nan, inf, 1e+308, -5.0)
  EOF
  ```
  Mixer propagation (`.venv/bin/python /tmp/proof_nan_volume.py`, mixer with one
  normal ±0.25 stem, `state.stem_volumes = {0: nan}`):
  ```
  mixed samples nan: 1024 / 1024
  pcm min/max: 0 0        (normal stem was ±0.25 -> ~±8191)
  inf-volume pcm min/max: 32767 32767
  ```
  (numpy also emits `RuntimeWarning: invalid value encountered in cast` at
  framework_mixer.py:369 on every tick.)

### 2. HIGH | `app/framework/loop_steps.py:696-698` + `app/framework/framework_mixer.py:76` | Staged next-loop audio silently overwritten when pre-gen finishes before the boundary → a fully generated loop is never played

- **Mechanism:** iteration N stages loop-N audio via `Mixer.set_next_loop`
  (`self.next_loop_audio = tracks_audio`, framework_mixer.py:76 — an
  **unconditional overwrite**); the mixer consumes it only when the callback
  reaches `current_loop_end_sample` (1 s lookahead). The driver's wait loop
  `_step_await_pregen` breaks as soon as `if self._pregen_done.is_set(): break`
  (loop_steps.py:696-698) — **without requiring that the pending transition was
  consumed** (`pop_transition_event()` is checked opportunistically in the same
  poll, but a not-yet-fired transition does not prevent the break; the
  `current_ahead < 0.5` break at loop_steps.py:700-708 is never reached because
  the pregen-done break fires first). The next iteration then runs
  `_step_commit_to_mixer` → `set_next_loop(loop N+1)`, replacing the still-pending
  loop-N track list: loop N's fetched-from-S3, tiled audio is never added to the
  mixer, `_next_loop_idx` is retagged, and the set jumps from loop N−1 straight to
  loop N+1 at the boundary.
- **Trigger:** ordinary timing — pregen for loop N+1 completing before the current
  boundary. Facilitated by `stem_cache` (300 s TTL, loop_orchestrator.py:131 /
  loop_steps.py:636-639): a retain-heavy conductor decision makes pre-gen ≈
  LLM-latency only, while the remaining tail of the current loop can be seconds
  long. Concrete numbers: 4-bar loop @120 BPM = 8 s; fresh path queues loop 2 at
  t=6 s; pregen 3 (one cache-hit change) completes at t=7 s < boundary t=8 s →
  loop 2 skipped.
- **Impact:** the Conductor's decided loop is audibly skipped; its GPU jobs were
  already paid for (wasted generation); `active_stems`/`last_actions`/`loop_count`
  advance through a loop the audience never heard, so subsequent conductor
  decisions reason from stem history that was never audible.
- **Minimal fix:** in `_step_await_pregen`, only break on `_pregen_done` after the
  pending transition has been popped (or make `Mixer.set_next_loop` refuse/queue
  when `next_loop_audio` is non-empty, e.g. append per `loop_idx`).
- **Proof** (`.venv/bin/python /tmp/proof_skip_loop.py` — real `Mixer` + real
  `_step_await_pregen`/`_step_commit_to_mixer`, loop-2 audio staged, boundary far,
  pregen done):
  ```
  after await_pregen: staged loop idx = 2 | pending loop-2 tracks still queued: 1
  after iteration-3 commit: pending tracks = [((100, 2), 1)] | _next_loop_idx = 3
  LOOP-2 AUDIO DROPPED (never played): True
  ```

---

## SUSPECTED (UNVERIFIED)

### S1. MED | `app/framework/framework_mixer.py:214, 354` | `self.current_sample += frames` runs OUTSIDE `self.lock`, racing `clear()` / `prime_loop()` / `loop_position_seconds()`

- **Mechanism:** the only writer of `current_sample` increments it outside the
  mixer lock (idle branch at :214, after the mixing `with self.lock` block at
  :354), while `clear()` (:83), `prime_loop` (:117) and `loop_position_seconds`
  (:139-141) read/write it under `self.lock`. A callback between lock-release and
  increment can (a) clobber a concurrent `clear()` reset (stale position survives
  a System Reset) or (b) make `prime_loop`'s `start_sample` up to one block
  (46 ms) stale, so freshly primed loop-1 tracks are scheduled slightly in the
  past and their first ≤46 ms is skipped (audible click).
- **Trigger:** `POST /api/state {"should_reset": true}` or Start pressed while the
  46 ms mixer tick is in its release→increment window. Narrow window; bounded
  damage; I could not construct a deterministic interleaving without modifying
  code.
- **Impact:** rare one-block audio seam / un-reset playback position; no crash.
- **Would confirm:** a barrier-instrumented test that holds a thread between the
  `with self.lock` exit and :354 while calling `clear()`, then asserts
  `current_sample == 0`.

### S2. MED (latent) | `app/framework/state_slices.py:30-40` | Slice views silently swallow writes — `state.musical.current_bpm = 130` is a no-op

- **Mechanism:** `_Slice` forwards reads via `__getattr__` but defines no
  `__setattr__` guard, so a write through a view binds the attribute on the
  transient view object and is lost (the view is recreated on every property
  access). Docstring says "READ VIEW ... boundaries are real", but only the read
  boundary is enforced; the write boundary silently corrupts.
- **Trigger:** none today — no production code writes through a slice (grep
  clean), which is why this is not CONFIRMED. Any future pass-2 caller doing
  `state.levels.stem_volumes[0] = 0.5`-style writes gets silent state divergence.
- **Proof (mechanism, executed):**
  ```
  state.musical.current_bpm = 130   # accepted silently
  state.current_bpm                 # -> 120 (unchanged)
  ```
- **Would confirm as reachable:** any caller that writes through a view.

### S3. MED | `app/routes/config.py:325-334` + `app/app_ui.py:184-192` | `/api/llm-config` gate gaps when only `AUDIENCE_PASSWORD` is set: audience password reads `llm_api_key`; POST is entirely ungated and can rewrite `audience_password`

- **Mechanism:** the DJ gate is `(is_dj_route and dj_pass and provided_pass != dj_pass)`;
  with `dj_pass == ""` it no-ops. `/api/llm-config` GET is then only covered by the
  audience GET gate (`is_audience_route`), so an audience-password holder can read
  `state.llm_api_key` (line 330) and `state.audience_password` (line 333) —
  secrets intended for the DJ. POST `/api/llm-config` is `is_dj_route` only, so
  with `dj_pass == ""` an anonymous peer can rewrite the LLM endpoint/key **and
  `audience_password` itself**, defeating the audience gate for shows.
- **Trigger:** deployment with `AUDIENCE_PASSWORD` set, `DJ_PASSWORD` empty
  (defaults are empty for both; onboarding merely warns).
- **Impact:** secret disclosure + auth-setting rewrite in audience-mode
  deployments. Not reported as CONFIRMED because the empty-`dj_pass`-means-open
  posture may be an accepted local-dev tradeoff (security lens excluded in
  round 1) — but SEC-3/SEC-4 fixes show this class is in scope.
- **Would confirm:** TestClient with `state.audience_password = "aud"`,
  `dj_password = ""`; `GET /api/llm-config` with Basic `x:aud` → 200 with
  `api_key`; anonymous `POST /api/llm-config {"audience_password": "x"}` → 200.

### S4. LOW | `app/framework/framework_state.py:325-339` | `_load_instruments` mis-parses an `instruments.json` that lacks `_metadata`

- **Mechanism:** when the file has no `"_metadata"` key, the whole parsed object
  is returned as `categorized_instruments` (line 339) — a documented-format file
  written as `{"instruments": {...}}` without metadata makes `"instruments"` a
  fake family and `_flatten_instruments` extends dict **keys**, so
  `available_instruments` degrades to family names and the conductor/UI catalog
  breaks. Only reachable via a hand-edited/auto-migrated file (the app's own
  `save_instruments` always writes `_metadata`).
- **Would confirm:** write `{"instruments": {"Synth": ["Pad"]}}` to
  `instruments.json`, construct `GlobalState()`, assert
  `state.available_instruments == ["Synth"]` (wrong).

---

## CHECKED AND CLEAN

- **WS snapshot integrity (A5/A8 follow-ups):** `_state_snapshot` /
  `_stems_snapshot` read everything under `state.lock` and deep-copy stems,
  loop history, and queued stems (ws.py:130-176, 185-198); `broadcast` snapshots
  the subscriber set before iterating and cleans stale conns under the manager
  lock (ws.py:82-101) — pinned by `test_broadcast_survives_set_mutation_during_send`.
  The set-vs-JSON pitfall for `muted_stems`/`soloed_stems` is fixed via `sorted()`.
- **Stem index confusion across crossfades:** `_step_commit_state` clears
  `muted_stems`/`soloed_stems`/`stem_volumes` on every commit
  (loop_steps.py:562-564), so per-slot mixer state cannot bleed onto the next
  loop's stems; `Track.stem_index` comes from the same enumeration order that
  becomes `active_stems`. A mid-loop mute maps to the same stem for the rest of
  the loop. Held.
- **Two-lock ordering:** only `state.lock` → `mixer.lock`/`state.sync_lock`
  nestings exist (config.py `to_thread(add_custom_instrument)` under `state.lock`;
  `record_loop_transition` deliberately called outside `state.lock`); the mixer
  thread never acquires `state.lock`; `snapshot_mixer_state`'s `sync_lock` is
  acquired and released before `self.lock`. No AB-BA cycle. (Pinned by
  `test_dual_lock_ordering_state_lock_holds_mixer_clear` /
  `test_no_mixer_lock_nests_state_lock`.)
- **Recording-handle snapshot vs close (B9/A1):** `broadcast_audio` snapshots
  handles under `sync_lock`, writes outside, and a close-after-snapshot write is
  logged once per handle via `_last_recording_error_handle`; `trigger_shutdown`
  closes handles under the same lock. Cross-lock flag reads (bools) are GIL-atomic.
- **REST `GET /api/state` / `GET /api/stems` returning live references**
  (`state.active_stems`, `state.stem_volumes`, `state.next_stems` — config.py:171-204,
  stems.py:17-27) unlike the deep-copied WS payloads: safe in the current runtime
  because FastAPI serializes async-endpoint responses synchronously on the event
  loop before any other coroutine can mutate, and the mixer thread never mutates
  those containers (it only reads copies via `snapshot_mixer_state`). Becomes a
  torn-read bug only if serialization ever moves off the loop thread.
- **Mixer snapshot crash-safety:** `snapshot_mixer_state` copies sets/dicts under
  `sync_lock` (framework_state.py:399-419) — no `Set/dict changed size during
  iteration` is reachable from `_callback` even against unlocked route writers;
  the write-side cross-lock residual is the documented A2 leftover, not re-reported.
- **Auto-tile / extension overshoot:** tiled/extended tracks can overshoot
  `current_loop_end_sample` by up to one track length and briefly double with the
  next loop's tracks at a transition — confined to the not-ready fallback paths,
  with explicit in-code invariants (the "buzzing bug" comment block); gapless
  tradeoff, no crash/data defect.
- **`cache_stem` LRU cap, `bpm<=0` guard, mono→stereo normalization, ping/pong
  keep-alives, `ConnectionManager` stale-discard, `trigger_shutdown` subprocess
  kill loop, `pop_transition_event` stickiness (no lost wakeup between polls),**
  `download_stem` float32→int16 conversion (AUDIO-1 pin), `create_custom_stem` /
  `remove_next_stem` bounds — all re-verified against current HEAD; earlier fixes
  intact.

---

## VERIFICATION

Independent re-verification at HEAD `04791e4` (grep-grounded citations; proofs re-run via `.venv/bin/python` heredocs against the repo venv; repo unmodified except this appended section).

FINDING 1 | Verified | `StemVolumeUpdate.volume` is a bare `float` at app/routes/schemas.py:78 and the handler stores it verbatim at app/routes/stems.py:33 (repro: model_validate_json accepts NaN/Infinity/1e308/-5); mixer applies it as gain at app/framework/framework_mixer.py:340-341 → summed at :351 → cast at :369, and the repro shows 4096/4096 NaN mixed samples casting to PCM all-0 (total silence) and inf/1e308 to all-32767 (full-scale blast), reachable because the DJ gate at app/app_ui.py:210 no-ops with the default empty DJ_PASSWORD and no test pins bounds; only nuance is the per-loop `state.stem_volumes.clear()` at app/framework/loop_steps.py:564 bounds persistence to the current loop — one request still destroys the whole audible mix and any in-flight recording.
FINDING 2 | Verified | reproduced end-to-end with the real `Mixer`, real `_step_await_pregen` and real `_step_commit_to_mixer`: the `_pregen_done` break at app/framework/loop_steps.py:696-698 fires while loop-2's staged tracks are still pending in `next_loop_audio` (transition not fired, `current_ahead` 8.0s), then the unconditional `self.next_loop_audio = tracks_audio` at app/framework/framework_mixer.py:76 replaces them and the boundary callback emits only loop-3 audio (`LOOP-2 AUDIO DROPPED: True`); no guard or test pins this ordering (300s stem_cache TTL confirmed at app/framework/loop_steps.py:649), so a cache-hit pregen finishing before the boundary drops a fully generated loop.
FINDING S1 | Verified | deterministic interleaving constructed against the real `_callback`: a hook on `broadcast_audio` (called at app/framework/framework_mixer.py:213, immediately before the unlocked `self.current_sample += frames` at :214, same unlocked write at :354) held the tick while `clear()` reset the position under `self.lock`, and the resumed increment left `current_sample == 2048` instead of 0 — a reset is clobbered and locked readers (`prime_loop` at app/framework/framework_mixer.py:117, `loop_position_seconds` at :134) see the stale value; window is one 46ms tick, exactly as the finding scoped it.
FINDING S2 | Weakened | mechanism proven — `state.musical.current_bpm = 130` binds on the transient view (each `state.musical` access builds a fresh `MusicalParams` at app/framework/framework_state.py:222-223 and `_Slice` at app/framework/state_slices.py:30-38 defines no `__setattr__`), host stays 120 — but grep shows zero production callers write through any slice today, so the write-swallow is latent-only and MED impact is not currently reachable.
FINDING S3 | Verified | TestClient repro of the aud-only deployment (dj_password defaults to "" per app/framework/framework_state.py:188): audience-cred `GET /api/llm-config` returns 200 leaking `llm_api_key` and `audience_password` (app/routes/config.py:324-334, gated only by the audience clause at app/app_ui.py:211), and anonymous `POST /api/llm-config` returns 200 and rewrote `audience_password`/`llm_api_key` because the dj clause at app/app_ui.py:210 no-ops on empty `dj_pass` for the dj-only route (:196) — anonymous GET is still 401, but the POST rewrite alone defeats the audience gate.
FINDING S4 | Verified | repro confirms `_load_instruments` returns the raw parsed dict when `"_metadata"` is absent (app/framework/framework_state.py:339), so `{"instruments": {...}}` becomes a fake family and `_flatten_instruments`' `flat.extend(items)` over the dict value at :380-384 yields the family key — `available_instruments == ["Synth"]` instead of instrument names; reachable only via a hand-edited instruments.json (the app's own `save_instruments` at :345-348 always writes `_metadata`) and no test pins the malformed-file path, matching the LOW scope claimed.
