# Round 3 — Audio/DSP lane (adversarial bug hunt)

Scope: `app/framework/framework_mixer.py`, `app/framework/domain_audio.py`, `app/aac_encoder.py`,
`app/lib/recording_postprocess.py`, `app/lib/recording_metadata.py`, `app/lib/harmonic.py`,
`app/framework/pregeneration.py`, `app/framework/audio_fetch.py` (+ adjacent call sites where the
defect class belongs to this lane). All line numbers valid at HEAD `04791e4`.
Read-only hunt; nothing in the repo was modified.

---

## CONFIRMED BUGS

### 1. HIGH | `app/routes/schemas.py:78` + `app/framework/framework_mixer.py:340-341,368-369` — NaN stem volume turns the ENTIRE mix into silence (all stems, both channels)

**Mechanism:** `StemVolumeUpdate.volume` is a bare `volume: float` with no bounds and pydantic v2's
default `allow_inf_nan=True`, so the JSON literal `NaN` is accepted and stored
(`routes/stems.py:32-34`). In the mixer tick, `total_gain = stem_gain_global * indiv_gain`
(framework_mixer.py:341) becomes NaN, so `outdata[...] += track_audio * total_gain` (line 351)
poisons **every sample of the overlap block — all stems, both channels**, not just the one stem.
`np.clip(outdata, -1.0, 1.0)` (line 368) does **not** remove NaN (`np.clip(nan)=nan`), and
`(... * 32767).astype("<i2")` (line 369) casts NaN to `0` → the broadcast stream (and any active
show recording, via `broadcast_audio`) receives all-zero PCM.

**Trigger:** one request: `POST /api/stems/0/volume` with raw body `{"volume": NaN}` (what
`curl --data '{"volume": NaN}'` or a JS client doing `parseFloat(undefined)` sends). Default
config has empty `dj_password`, so no auth required; with a DJ password the legit DJ triggers it
equally. Effect lasts until the next loop commit clears volumes (`loop_steps.py:564`) — up to a
full loop of dead air (typically 4–8 s, longer if pregen is slow), recurring on every repeat.

**Impact:** whole live stream goes silent + zeros recorded into the show WAV; user's volume intent
also silently destroyed. Secondary: `{"volume": 1e30}` is also accepted (documented 0.0–2.0 range
unenforced; only `np.clip` bounds the damage to full-scale distortion).

**Minimal fix:** `volume: float = Field(ge=0.0, le=2.0, allow_inf_nan=False)` on
`StemVolumeUpdate`, plus a defensive `if not np.isfinite(indiv_gain): indiv_gain = 1.0` (or
`volumes.get(...) or 1.0`-style guard) at framework_mixer.py:340.

**Proof (run at HEAD):**
```
status: 200 | stored volume: nan
mix with NaN volume -> min: nan max: nan all-NaN: True
broadcast int16 values: [0]
control volume 1.0 -> min: 0.5 max: 0.5
volume 1e30 accepted: 200
```
(snippet: TestClient POST raw `{"volume": NaN}` → real `Mixer._callback` on a 0.5-amplitude stem
→ every output sample NaN → int16 0; identical mixer with volume 1.0 outputs 0.5.)

### 2. MED | `app/framework/framework_mixer.py:160-164` — loop-boundary fallback plays two offset copies of the same stem simultaneously (phasing/double-audio)

**Mechanism:** in `_extend_tracks_for_loop` ("next loop not ready" fallback, called from the
callback at :272), the straddle branch (`track_end > loop_end_sample`) tiles the track's **head**
(`src_audio = track.audio_data[:samples_remaining]`, line 162) and adds it **at the boundary**
while the original track is never truncated — its tail `[loop_end, track_end)` keeps playing.
Result: for `samples_remaining` samples two offset copies of the same stem sum in the mix
(+6 dB comb artifact, audible flange), then the head replays again in
`[track_end, track_end + samples_remaining)`. Contrast with the auto-tile net (:315-322) whose own
comment mandates "Start the tile at track_end (NOT current_sample) for a gapless seam".

**Trigger:** (a) boundary reached while `next_loop_audio` is empty — pregen late (LLM call runs on
every pregen; loops are only ~4–8 s while `_await_jobs` allows 120 s), and (b) a track that
straddles the boundary — which is common, not exotic: the auto-tile net creates tracks of
`repeats = gap//len + 2` tiles (ends overshoot the boundary), and AAC decode padding makes decoded
stems slightly longer than the nominal `loop_duration_samples` (tile_to_loop only tiles shorter
audio up, never truncates longer audio).

**Impact:** audible double-strike/flanging burst at every boundary that coincides with a late
pregen + straddling stem; energy +6 dB → can also push the mix into clip territory.

**Minimal fix:** truncate the original track's contribution at `loop_end_sample` (or start the
extension at `track_end` and only cover `[track_end, new_loop_end)`, like the auto-tile net).

**Proof:** numpy trace at HEAD — one stem `Track[0,100)`, `current_loop_end_sample=80`,
`_current_loop_duration=60`, then `_extend_tracks_for_loop(80)`:
```
track covering [80,100): span=(0, 100) content=[80, 81, 82, 83, 84, 85]
track covering [80,100): span=(80, 120) content=[0, 1, 2, 3, 4, 5]
=> 2 tracks play simultaneously in [80,100): original tail values 80..99 vs restarted head 0..19
```
(`tests/test_mixer.py:279-297` pins only the created track's structure — start/length — not the
overlap, so this is unpinned behavior.)

### 3. MED | `app/routes/shows.py:95-101` (>4GB finalize fallback, defect class belongs to this lane: WAV header size fields for >4GB recordings) — long recordings become unreadable: RIFF/data sizes left at 0

**Mechanism:** `_finalize_wav`'s `else` branch deliberately leaves the size fields as the `0`
placeholders written by `_write_wav_header` when `data_size > _WAV_MAX_DATA_SIZE`. Python's
`wave` module validates the RIFF chunk (`Chunk.read` is bounded by the declared RIFF size), so a
size-0 RIFF is rejected outright with `wave.Error: not a WAVE file`; any size-honoring reader
reports 0 frames / 0 duration. Concretely: a show or export recording longer than
`0xFFFFFFFF - 36` bytes of PCM = 4 294 967 259 B ÷ 176 400 B/s ≈ **6 h 45 m** (2ch/16-bit/44.1kHz)
produces a file that `wave.open`, `split_wav_by_chapters`, `embed_wav_metadata` and other strict
tools cannot open at all, and metadata (duration_seconds consumers, postprocess) reads zero.

**Trigger:** record one show (or export) for ~6.8+ hours, then stop; or hand the file to any
chunk-size-strict parser. Tolerant players (VLC/ffmpeg) still play it — hence MED, not HIGH.

**Minimal fix:** in the `else` branch write `0xFFFFFFFF` to both the RIFF size (offset 4) and data
size (offset 40) — the standard "unknown/streamed length" convention — instead of leaving 0, and
log once.

**Proof (HEAD, else-branch forced on a small file):**
```
Recording exceeds 4GB WAV limit; sizes left as placeholders
RIFF size field: 0 | data size field: 0
wave.open FAILS: not a WAVE file
```

### 4. MED (latent — module currently has no app-code caller) | `app/lib/recording_metadata.py:122-127, 282, 288` — postprocess WAV rewrite has no >4GB guard and truncates the file before rewriting it

**Mechanism:** two defects co-located in `embed_wav_metadata`/`_write_wav_with_metadata`:
(a) the vestigial `with wave.open(wav_path, "wb"):` block at :122-127 (which only sets params then
`pass`es) **truncates the recording file in place** before `_write_wav_with_metadata` rewrites it
from the in-memory copy — any failure between the two points (ENOSPC, crash, kill) destroys the
recording on disk; (b) `struct.pack("<I", len(audio_data))` (:282) and
`struct.pack("<I", file_size - 8)` (:288) raise `struct.error` for data > 4 GiB — exactly the
guard the live sibling has (`shows.py:_WAV_MAX_DATA_SIZE`) but this module lacks, i.e. the C4 fix
was applied to shows.py only. Also `wf.readframes(n_frames)` loads the whole multi-GB file into
RAM.

**Trigger:** today: none — `postprocess_show_recording` / `embed_wav_metadata` / `split_show_chapters`
/ `export_show_format` are called by **no app code** (grep: only `tests/test_recording_postprocess.py`;
shows.py:59 documents the header-now-written-incrementally design that replaced the postprocess
call the round-1 report assumed). If anyone wires postprocess back into `stop_show` (as
`00_FINAL_REPORT.md` claims was done — it isn't), this arms a data-destroying path for long shows.

**Minimal fix:** delete the dead `wave.open(wav_path, "wb")` block; add the
`data_size <= _WAV_MAX_DATA_SIZE` guard (refuse + log, mirroring `_finalize_wav`); stream
copy instead of full in-RAM read.

**Proof:** `struct.pack("<I", 2**32)` → `struct.error: 'I' format requires 0 <= number <=
4294967295`; the dead wave.open block shrinks a valid 176 444-byte WAV to 44 bytes on disk
(demonstrated), and the rewrite happens only afterwards.

---

## SUSPECTED (UNVERIFIED)

### 5. MED if reproducible | `app/framework/domain_audio.py:40-44` + `app/framework/framework_mixer.py:296` — `(N,1)` mono arrays slip through `to_two_channel`, and the loop-switch path never re-coerces channels
`to_two_channel` only fixes 1-D input; a 2-D `(N,1)` array passes through unchanged although the
docstring/contract (and the B11 fix intent) says "(samples, 2)". `Mixer.prime_loop` and `add_track`
coerce via `_ensure_stereo`, but the loop>1 transition adds next-loop tracks via
`_add_track_internal(audio_data.copy(), ...)` (:296) with **no** channel coercion, so a `(N,1)`
stem would mix via `mix_channels = min(2, 1)` → left channel only (right silent). No current
producer emits `(N,1)` (scipy returns 1-D for mono WAVs; the AAC round-trip yields 1-D or `(N,2)`),
so this is a latent shape inconsistency between the two mixer insertion paths. **Confirm by:**
asserting `audio.ndim == 2 and audio.shape[1] == 2` at every `stem_cache[...] =` /
`cache_stem(...)` insert and running a set; fix is `_ensure_stereo` in the transition path (or
`column_stack` in `to_two_channel` for `shape[1]==1`).

### 6. MED if any model ≠ 44.1 kHz | `app/worker.py:300,307` — AAC container rate and duration math hardcode 44100 regardless of the generator's real rate
`GeneratorRegistry.sample_rate` is taken from each model's config (`framework_generator.py:101`),
but `_generate_and_upload` encodes with `encode_aac(audio_array, sample_rate=44100)` and computes
`get_audio_duration(audio_array, sample_rate=44100)`. If any of the three HF repos
(`config/models_config.json` itself carries no `sample_rate`) declares e.g. 48000, the WAV/AAC
container would misdeclare the rate (pitch/tempo shifted on playback, loop lengths wrong) and the
`generator_jobs.duration_seconds` column would be off by 44100/rate. **Confirm by:** reading
`sample_rate` from each repo's `model_config.json` (needs network/HF cache); today decode and
encode agree on 44100, so the loop is self-consistent for 44.1 kHz models.

### 7. LOW | `app/lib/recording_metadata.py:52-56` — CUE sheet quoting/injection
Track titles (LLM-derived `set_name` flows into `title`) are interpolated into
`TITLE "{ctitle}"` without escaping `"` (only `reasoning` gets quote-swapped), and
`REM COMMENT {reasoning}` is unquoted and may contain raw CRLF → malformed CUE for hostile/odd
LLM output. Latent: same unwired-module status as finding 4. **Confirm by:** calling
`write_cue_sheet` with `title='a" b'` and parsing the result.

### 8. LOW | `app/lib/recording_metadata.py:139-146` — malformed WAV LIST/adtl sub-chunk
`_build_chapter_list_chunk` writes an 8-byte `ltxt`-style prefix (`<cue id><0>`) followed by an
*embedded* `labl` chunk, instead of a real `ltxt` (id, sample_length, purpose, country, language,
dialect, codepage) or a plain `labl`. Strict adtl parsers mis-skip or reject the chunk; lenient
ones ignore LIST/adtl entirely. Latent (unwired). **Confirm by:** parsing the produced LIST chunk
with a strict RIFF walker (e.g. `ffmpeg -v debug -i` chunk dump).

---

## CHECKED AND CLEAN

- **int16 conversion overflow:** mixer clips to [-1,1] then multiplies by 32767 → max 32767, no
  int16 overflow; stem download uses the identical conversion (AUDIO-1 pin holds). Only NaN
  defeats `np.clip` (finding 1).
- **Cache-key collisions (`make_cache_key`):** the `_`-joined f-string is ambiguous in principle,
  but with `model_id` from the fixed 3-value set, `bpm`/`bars` ints and `key` from the 24 validated
  keys, I could not construct two distinct field tuples that render the same key string —
  falsified, not reported.
- **Harmonic wheel:** all 24 `VALID_KEYS` map 1:1 onto `KEY_TO_CAMELOT` (verified programmatically:
  `get_harmonic_map()` covers exactly VALID_KEYS); the N−1/N+1 wrap formulas produce the correct
  Camelot neighbors and the relation is symmetric for all 24 keys.
- **Duration drift across loops:** `tile_to_loop`/`prime_loop`/`set_next_loop` all use the same
  integer `loop_duration_samples`; boundaries are set from that constant, so int-truncation error
  does not accumulate loop-to-loop. `calc_duration` guards bpm ≤ 0 and NaN (falls back to 120).
- **`tile_to_loop` edge cases:** shorter stems are ceil-tiled exactly to loop length
  (`len//len+1` then slice); missing stems become silence; `t["bars"]` KeyError falsified —
  `process_actions` defaults bars (`conductor_interaction.py:142`), as does pregeneration.
- **`_extend_tracks_at_position`:** zero-length division unreachable (branch order guarantees
  `samples_left_in_track > 0`); `repeats_needed` ceil-math safe; auto-tile uses `max(1, length)`.
- **Empty/short arrays:** a 0-length Track's `track.length // ...` would raise, but no producer
  can emit one (AAC decode of empty bytes → RuntimeError → `fetch` returns None → silence fill).
- **Sample-rate consistency (live path):** worker encodes and declares 44100; `decode_aac`
  validates 44100 and raises loudly → `audio_fetch.fetch` maps to logged `None` → silence fill,
  never a crash (only the model-rate question of finding 6 remains).
- **`snapshot_mixer_state`:** copies solo/muted/volumes under `sync_lock` — the A2 torn-read fix
  is in place; the mixer no longer iterates live containers.
- **Transition boundary math:** `set_next_loop` preserves the current boundary; the switch aligns
  new tracks to `current_loop_end_sample`; `_next_loop_duration` fallback derives from the longest
  future track; `pop_transition_event` is atomic under `self.lock`; `loop_position_seconds`
  snapshots boundary+position under one lock.
- **`aac_encoder`:** 60 s subprocess timeout present (`_run_ffmpeg`), temp files cleaned via
  `finally` with `missing_ok`, `decode_aac` validates rate, mono/`>2ch` inputs coerced; B5 pin
  holds.
- **`pregeneration`:** failure path always sets `_pregen_results=None` + `_pregen_done`; the P2
  predicate (`loop_steps.py:213-216`) tolerates `None` and stale `loop_idx`; background path still
  never touches `state.cache_stem` (wave2 pin holds); `loop.stem_cache` is evicted (300 s stale,
  `loop_steps.py:644-646`) — no unbounded growth.
- **`split_wav_by_chapters` math:** start/end clamped to file bounds, `end ≥ start` enforced,
  byte offsets computed from channels×sampwidth correctly (its unsanitized `show_title` filename
  interpolation is unreachable today — no app caller).

---

## VERIFICATION (independent verifier pass — HEAD 04791e4, proofs run at HEAD)

FINDING 1 | Verified | `POST /api/stems/0/volume` with raw `{"volume": NaN}` returned 200 and stored `nan` (app/routes/stems.py:33, unbounded `volume: float` at app/routes/schemas.py:78, anonymous allowed by app/app_ui.py:168-171 with no passwords set), and the real `Mixer._callback` then emitted an all-NaN block whose broadcast int16 bytes were all `0x00` (app/framework/framework_mixer.py:340-351, 368-369; control volume 1.0 finite; only app/routes-level test is tests/test_api.py:601 which pins index, not value).
FINDING 2 | Verified | reproduced `_extend_tracks_for_loop(80)` on a Track spanning [0,100) leaves two tracks sounding at sample 90 — original tail plus a head-restart track added at the boundary (app/framework/framework_mixer.py:160-164), reachable because the empty-`next_loop_audio` fallback runs from the callback (framework_mixer.py:265-272) and `tile_to_loop` never truncates audio longer than `loop_duration_samples` (app/framework/domain_audio.py:116-118), while tests/test_mixer.py:276-296 pins only the new track's start/length, not the overlap.
FINDING 3 | Verified | forcing the `else` branch (app/routes/shows.py:100-101) of a real `_write_wav_header`/`_finalize_wav` cycle left RIFF size 0 and data size 0 and `wave.open` raised `Error: not a WAVE file`, on both recording (shows.py:304/363) and export (shows.py:479/514) paths, with no test pinning the >4 GiB case.
FINDING 4 | Verified | the vestigial `with wave.open(wav_path, "wb")` block (app/lib/recording_metadata.py:122-129) shrank a valid 4044-byte WAV to 44 bytes before `_write_wav_with_metadata` rewrites from the in-RAM `readframes` copy at :92, and `struct.pack("<I", len(audio_data))`/`struct.pack("<I", file_size-8)` at :282/:288 raise `struct.error` for >4 GiB (no `_WAV_MAX_DATA_SIZE` guard as shows.py:51 has), with grep confirming the only callers are tests/test_recording_postprocess.py so the data-destroying path is latent as scoped.
FINDING 5 | Verified | `to_two_channel` passes a `(10,1)` array through unchanged (app/framework/domain_audio.py:40-43) and a Track added through the loop>1 transition path — `_add_track_internal(audio_data.copy(), ...)` at app/framework/framework_mixer.py:235 (report cited :296), which lacks the `_ensure_stereo` used by add_track/prime_loop at :58/:119 — mixed into the left channel only (proof: out[:,0]=1.0, out[:,1]=0.0), latent as the finding itself states since no in-repo producer emits `(N,1)`.
FINDING 6 | Weakened | the hardcode is real (app/worker.py:300,307 encode/duration at 44100 while the model rate is read at app/framework/framework_generator.py:101 and discarded by `generate_stem` at :406-411, and config/models_config.json carries no `sample_rate`), but nothing in the repo or its 44100-only decode validation (app/aac_encoder.py:170) / 44100-only mixer (app/framework/framework_mixer.py:27) establishes that any shipped model is not 44.1 kHz, so the defect narrows to "latent rate-mismatch if a non-44.1 kHz model is ever added".
FINDING 7 | Verified | `write_cue_sheet` emitted `TITLE "S"et"` / `TITLE "A" B"` with the quote unescaped and passed a raw CRLF-bearing reasoning straight into `REM COMMENT line1\r\nFAKE INDEX 01 00:00:00` (app/lib/recording_metadata.py:51,56,62-63), latent as scoped because no app code calls it (only tests/test_recording_postprocess.py).
FINDING 8 | Verified | `_build_chapter_list_chunk` writes a bare `<cue_id><0>` 8-byte prefix instead of a chunk header (app/lib/recording_metadata.py:215-221 — report's :139-146 is the adjacent rewrite site), and a strict RIFF walk of the produced `LIST/adtl` payload desyncs: `[b'\x01\x00\x00\x00' (size 0), b'labl' (8), b'\x01\x00\x00\x00' (size 1702129518)]`.

VERDICT: 7 verified, 1 weakened, 0 falsified.
