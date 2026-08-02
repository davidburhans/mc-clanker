# 01 — Anatomy & Hexagonal Boundaries: `framework_main_async.py`

Subject: `app/framework/framework_main_async.py` (1192 LOC, 512-LOC god-method `_run_loop`).
Goal: justify a split into cohesive <500 LOC modules and locate hexagonal port/adapter seams (per CLAUDE.md "Architecture Diagram" + thread-safety rules).

---

## (A) Symbol inventory

Line numbers are authoritative (verified via grep over `def`/`class`/property).

| Symbol | Kind | Lines | One-line responsibility |
| --- | --- | --- | --- |
| `DEFAULT_FALLBACK_BPM` | const | 39 | Tempo used when BPM ≤ 0 (guards ZeroDivisionError). |
| `LOOP_RETRY_BACKOFF_SECONDS` | const | 44 | Sleep before retrying a failed loop iteration (B1). |
| `calc_duration(bpm, bars, time_signature)` | fn (pure) | 47–56 | Convert (BPM, bars) → loop duration in seconds. |
| `_to_two_channel(audio)` | fn (pure) | 58–66 | Coerce mono/1-D audio → 2-D `(samples, 2)` (B11). |
| `_flush_lock` | module `asyncio.Lock` | 70 | Serializes overlapping `flush_recording_buffers` calls (B13). |
| `flush_recording_buffers()` | async fn | 73–115 | Bulk-insert buffered LLM interactions + actions to DB; re-queues on failure. |
| `process_actions(actions, active_stems)` | fn (mostly pure*) | 118–178 | Translate Conductor actions → deduplicated track list; mutates `_original_details["_age"]`. |
| `AsyncFrameworkLoop` | class | 182–1166 | Async DJ-set orchestrator. |
| ↳ `__init__(session_id)` | method | 190–208 | Wire mixer/conductor/garage/cache/pregen handles; init flags. |
| ↳ `garage` (property) | method | 210–214 | Lazy-create Garage client from env on first access. |
| ↳ `start()` | async method | 216–230 | Build mixer in executor, start it, spawn `_run_loop` task. |
| ↳ `stop()` | async method | 232–249 | Cancel loop task, stop mixer in executor. |
| ↳ `_finish_loop()` | method | 251–256 | Mark stopped + tear down mixer (B1 cleanup). |
| ↳ `_run_loop()` | async method | 258–771 | **GOD-METHOD.** Full per-iteration pipeline (see §B). |
| ↳ `_build_prompt(track, key, bpm)` | method (pure) | 773–804 | Format track dict → model prompt via engine `prompt_template`. |
| ↳ `_submit_job(...)` | async method | 806–849 | Insert one `GeneratorJob` row → return job UUID. |
| ↳ `_fetch_audio(audio_path)` | async method | 851–879 | Garage GET → AAC decode → float32 ndarray (executor). |
| ↳ `_append_loop_audit(...)` | async method | 881–912 | Buffer one `LLMInteraction` + N `ShowAction` rows (C1). |
| ↳ `_relative_show_ms()` | method (pure) | 914–917 | Milliseconds since show start (0 if none). |
| ↳ `_audit_prompt_context(...)` | method (pure) | 919–928 | Summarize request context for audit row. |
| ↳ `_audit_action_row(...)` | method (pure) | 930–943 | Shape one action dict for bulk-insert. |
| ↳ `_audit_stem_details(...)` | method (pure) | 945–965 | Build JSON-safe stem descriptor. |
| ↳ `_audit_action_description(...)` | method (pure) | 967–977 | Human-readable one-liner per action. |
| ↳ `_pre_generate_next_loop(idx, snapshot)` | async method | 979–1166 | Run the *entire* pipeline for loop N+1 in background; store results on `self._pregen_results`. |
| `run_framework_loop_async(session_id)` | async fn | 1169–1186 | Entry-point wrapper: build loop, start, babysit, stop. |
| `__main__` block | — | 1189–1192 | Standalone smoke test. |

*`process_actions` is pure w.r.t. I/O but mutates the caller's `active_stems[idx]["_original_details"]` in place — a hidden side effect.

**Total:** 5 module-level functions, 1 module-level async fn, 1 module lock, 2 constants, 1 class with 18 members, 1 entry fn.

---

## (B) `_run_loop` phase decomposition (lines 258–771)

| # | Phase | Lines | `state.*` READ | `state.*` WRITE | External I/O | Produces / returns |
| --- | --- | --- | --- | --- | --- | --- |
| P0 | init | 267–269 | `is_running` | — | — | `loop_idx`, `current_loop_end_sample` |
| P1 | wait-for-start gate | 270–289 | `is_generating`, `is_running`, `shutdown_event` | — | `asyncio.sleep(0.5)` | exits on Start or shutdown |
| P2 | pregen-decision | 300–340 | `current_bpm`, `current_key`, `active_stems`, `is_generating` | — | reads `self._pregen_results` | `pregen_ready` flag + reconstructed `conductor_response` |
| P3 | read-state-snapshot ("Step 1") | 342–391 | `should_reset`, `target_bpm/key_override`, `current_bpm`, `current_key`, `active_stems`, `user_override`, `available_instruments`, `stem_history`, `llm_*`, `generator` | `should_reset=False`, `current_bpm`, `current_key` (overrides applied), `target_*_override=None` | reads `config/models_config.json` | locals: `current_bpm/key`, `active_stems`, `available_models`, `llm_config` |
| P4 | call-conductor ("Step 2") | 393–415 | (none beyond locals) | — | **LLM** via `conductor.get_next_state_async` | `conductor_response` (with fallback dict on error) |
| P5 | parse-actions + audit-log ("Step 3") | 417–438 | `active_stems` (reread) | `last_actions` | — | `deduped_tracks` |
| P6 | build-next-stems / state-write ("Step 5") | 440–475 | (locals) | `current_bpm`, `current_key`, `current_set_name`, `llm_reasoning`, `next_stems` | — | `local_next_stems`, `local_current_bpm/key` |
| P7 | submit-jobs ("Step 6") | 477–504 | (reads `self.stem_cache`) | `cache_stem(...)` later | **DB** via `_submit_job` → `GeneratorJob` rows | `pending_jobs: list[(job_id, idx, cache_key)]` |
| P8 | await-jobs + fetch-audio ("Step 7") | 506–529 | (none) | `stem_cache` (+ `state.cache_stem` B7) | **LISTEN/NOTIFY** `wait_for_multiple_jobs`; **S3** `garage.get_object`; **subprocess** `decode_aac` | populated `self.stem_cache` |
| P9 | tile-audio ("Step 8") | 531–571 | (none) | — | — | `tracks_data`, `loop_duration_samples` |
| P10 | commit-to-mixer ("Step 9/10") | 573–602 | (reads `self.mixer`) | `mixer.current_loop_end_sample`, `_current_loop_duration` (loop 1) | `Mixer` thread (`_add_track_internal` / `set_next_loop`) | `tracks_to_use`, `duration_samples`, `current_loop_end_sample` |
| P11 | update-state-commit ("Step 10") | 608–698 | `active_stems`, `_pregen_results`, `current_set_name`, `llm_reasoning`, `target_*_override` | `previous_stems`, `stem_history`, `active_stems`, `next_stems=[]`, `muted/soloed_stems.clear`, `stem_volumes.clear`, `loop_count++`, `current_bpm/key/set_name/reasoning` (pregen path), `last_actions`, `current_bpm/key` (pending overrides) | — | `state_snapshot` dict for pregen |
| P12 | record-initial + cache-maint + pregen-spawn | 700–720 | `stem_cache` | `state.record_loop_transition` (loop 1) | — | spawns `_pre_generate_next_loop` task OR seeds `_pregen_results` from queued loop |
| P13 | await-pregen / transition-record ("Step 11") | 721–760 | `active_stems`, `current_set_name`, `llm_reasoning` (for recording) | `record_loop_transition` (via sync_lock) | reads `mixer.pop_transition_event()`, `mixer.current_sample/end_sample` | breaks on `_pregen_done` or `current_ahead < 0.5s` |
| P14 | error-handlers | 762–770 | — | — | `traceback`, `asyncio.sleep` | CancelledError → `_finish_loop`+raise; Exception → log+backoff+`continue` |

**I/O inventory inside `_run_loop`:** LLM (P4), DB insert (P7), Postgres LISTEN/NOTIFY (P8), S3 GET (P8), AAC decode subprocess (P8), Mixer thread mutations (P10, P13), filesystem read of `models_config.json` (P3). Every one of those is a candidate adapter seam.

---

## (C) Candidate modules + members

### C1. `loop_orchestrator.py` (~core)

- **Members:** `AsyncFrameworkLoop.__init__`, `start`, `stop`, `_finish_loop`, `_run_loop` orchestration skeleton (P0–P2, P10 skeleton, P11–P14), `run_framework_loop_async`.
- **Imports:** `asyncio`, `state`, `Mixer`, the 4 adapter ports.
- **State attrs:** `is_running`, `is_generating`, `shutdown_event`, `active_stems`, `next_stems`, `previous_stems`, `loop_count`, `stem_history`.
- **Role:** owns lifecycle + sequencing; delegates each phase to an injected port. This is the only place that holds the `while` loop.

### C2. `conductor_interaction.py`

- **Members:** `process_actions`, `_build_prompt`, the `_load_available_models()` helper extracted from P3 (duplicated in `_pre_generate_next_loop` at 1117–1136), plus the fallback-response builder (P4 & 1117–1149).
- **Imports:** `os`, `json`, `state.generator`, `config/models_config.json`.
- **State attrs (read-only):** `generator`, `current_bpm`, `current_key`, `active_stems`, `user_override`, `stem_history`.
- **Role:** pure prompt/action shaping; no I/O except a config-file read.

### C3. `job_queue_adapter.py`

- **Members:** `_submit_job`; wraps external `wait_for_multiple_jobs` (from `app.job_waiter`).
- **Imports:** `uuid`, `datetime`, `DatabaseManager`, `models.generator_job.GeneratorJob`.
- **State attrs:** none.
- **Role:** the only Postgres-queue touchpoint in the file.

### C4. `audio_pipeline.py`

- **Members:** `_fetch_audio` (impure), `_to_two_channel` (pure), `calc_duration` (pure), the P9 tiling block extracted as `tile_to_loop(audio, duration_samples)`.
- **Imports:** `garage_client`, `aac_encoder.decode_aac`, `numpy`.
- **State attrs:** none.
- **Role:** storage fetch + numerical shaping. Splits cleanly into pure (`calc_duration`, `_to_two_channel`, `tile_to_loop`) vs impure (`_fetch_audio`).

### C5. `audit_recording.py`

- **Members:** `flush_recording_buffers`, `_append_loop_audit`, `_relative_show_ms`, `_audit_prompt_context`, `_audit_action_row`, `_audit_stem_details`, `_audit_action_description`, `_flush_lock`.
- **Imports:** `time`, `datetime`, `DatabaseManager`, `models.{LLMInteraction, ShowAction}`.
- **State attrs:** `current_show_id`, `current_show_start_time`, `llm_interaction_buffer`, `action_buffer`, `lock`.
- **Role:** all show-audit persistence. Self-contained; only cross-cutting concern is the `state.lock` discipline it shares with `flush_recording_buffers`.

### C6. `pregeneration_worker.py`

- **Members:** `_pre_generate_next_loop`, pregen task/event fields on the orchestrator (`_pregen_task`, `_pregen_done`, `_pregen_loop_idx`, `_pregen_results`), and P12 spawn logic.
- **Imports:** reuses C2 + C3 + C4.
- **State attrs:** `generator` (for available_models), `state_snapshot`.
- **Role:** a *second* orchestrator that re-runs C2/C3/C4 ahead of time. Largely duplicate of P3–P9 — strongest extraction ROI (kills ~190 LOC of copy-paste).

> LOC after split (est.): orchestrator ~450, conductor_interaction ~150, job_queue ~60, audio_pipeline ~110, audit_recording ~180, pregeneration ~220. **All < 500.**

---

## (D) Proposed ports vs adapters (hexagonal)

Core (`loop_orchestrator`) should depend only on **Ports** (abstract interfaces it owns). Each Port today has exactly one Adapter.

| Port (owned by core) | Method(s) | Today's Adapter (infra impl) | Member(s) replaced | I/O hidden |
| --- | --- | --- | --- | --- |
| `ConductorPort` | `async get_next_state_async(ctx) -> ConductorResponse` | `ConductorLLMAsync` (existing, `app/framework/framework_conductor_async.py`) | P4 call + `_build_prompt` fallback | LLM HTTP |
| `JobQueuePort` | `async submit(spec) -> JobId` ; `async await_many(ids, timeout) -> dict[JobId, path]` | `PostgresJobQueueAdapter` = `_submit_job` + `app.job_waiter.wait_for_multiple_jobs` | P7 + P8-wait | DB insert, LISTEN/NOTIFY |
| `AudioStoragePort` | `async get_object(path) -> bytes` | `GarageAudioAdapter` = `create_garage_client_from_env` + `_fetch_audio` | P8 fetch | S3 GET |
| `AudioCodecPort` | `decode(bytes, sr) -> ndarray` | `AacCodecAdapter` = `app.aac_encoder.decode_aac` (executor-wrapped) | P8 decode | FFmpeg subprocess |
| `MixerPort` | `set_next_loop(...)`, `add_track_now(...)`, `clear()`, `pop_transition_event()`, `current_sample`/`current_loop_end_sample` | `Mixer` (existing, `framework_mixer.py`) | P10, P13 | audio thread |
| `AuditSinkPort` | `append_loop(...)`, `async flush()` | `PostgresAuditAdapter` = `_append_loop_audit` + `flush_recording_buffers` | P5-log, P11, C5 | DB bulk insert |
| `StateRepository` *(shared kernel, optional)* | `snapshot()`, `commit_next(...)`, `record_loop_transition(...)` | `GlobalState`/`state` (existing, `framework_state.py`) | P3, P6, P11, P12 | in-process `asyncio.Lock` |

**Pure / domain (no port needed):** `calc_duration`, `_to_two_channel`, `tile_to_loop`, `process_actions`, `build_prompt`, the `_audit_*` shapers. These belong in a `domain/` package the core imports directly.

**Impure / adapter (must sit behind a port):** everything that touches LLM, Postgres, S3, FFmpeg, the audio thread, or the filesystem config read.

**Seam order of attack (lowest risk → highest):**

1. `audit_recording` (C5) — already isolated by `_flush_lock`; no behavior change.
2. `job_queue` (C3) — single row insert + external waiter.
3. `audio_pipeline` (C4) — pure math lifts out trivially; `_fetch_audio` wraps cleanly.
4. `conductor_interaction` (C2) — moves prompt building + dedup; double-check `process_actions` mutation of `_original_details`.
5. `pregeneration` (C6) — extract last, after C2/C3/C4 ports exist; it then becomes a thin composition.
6. `loop_orchestrator` (C1) — last; pure coordination once ports exist.

---

## (E) Top 5 risks of splitting

1. **Hidden mutation in `process_actions`** (L130–132): it writes `_original_details["_age"]` into the caller's `active_stems` entry. If C2 is extracted, the orchestrator must still pass a copy or the `_age` accounting (consumed by P6/P11) silently breaks.
2. **`state.lock` discipline spans modules.** CLAUDE.md mandates lock-scoped, I/O-free critical sections (P3, P6, P11 each acquire `state.lock`). After the split, multiple modules share the same lock — a regression in lock scope (e.g. holding it across an LLM call) reintroduces the exact races the doc warns about. Lock ownership must stay in the orchestrator; extracted modules receive *snapshots*, not live `state`.
3. **P11 is a 90-line non-atomic state transition** that writes `previous_stems`, `active_stems`, `next_stems`, `stem_history`, `loop_count`, plus pregen-only re-derivation of `last_actions`. Splitting it across `StateRepository` + `AuditSinkPort` risks losing the single-locked atomicity; any refactor must keep it under one `async with state.lock`.
4. **C6 duplicates C2/C3/C4 logic verbatim** (`_pre_generate_next_loop` L1117–1166 mirrors P3–P9). Extracting ports lets both call sites share code — but a behavioral divergence between the foreground and background paths (e.g. cache-stem routing via `state.cache_stem` happens only in the foreground P8, *not* in C6 at L1155) is load-bearing and must be preserved, not "fixed."
5. **Mixer-coupling in the orchestrator** (P10, P13) reads `mixer.current_sample`, `current_loop_end_sample`, and `pop_transition_event()` under `mixer.lock`, not `state.lock`. A naive `MixerPort` abstraction must expose the same two-lock coordination (state.lock vs mixer.lock) or the crossfade-timing + transition-recording (P13) will deadlock or drop events. This is the riskiest port to introduce.

---

### Appendix: verified line anchors

- `class AsyncFrameworkLoop`: **L182** · `_run_loop`: **L258–771** · `_build_prompt`: **L773** · `_submit_job`: **L806** · `_fetch_audio`: **L851** · `_append_loop_audit`: **L881** · `_pre_generate_next_loop`: **L979–1166** · `run_framework_loop_async`: **L1169** · `__main__`: **L1189**.
- Phase step comments: L300 (pregen), L342 (Step 1), L393 (Step 2), L417 (Step 3), L440 (Step 5), L477 (Step 6), L506 (Step 7), L531 (Step 8), L560 (Step 9), L608 (Step 10), L721 (Step 11).
