# Implementation Plan: E1–E6 Refactor of `framework_main_async.py`

> **STATUS (2026-08-11 close-out): Phases 0–10 COMPLETE; Phase 11 (MixerController full port) DEFERRED.** The full `E,F,I,UP006,UP007,UP045` ruff rule set is now enforced and clean (0 errors); suite at **655 passed / 16 skipped**. E5 port-wiring: only `ConductorPort` is constructor-injected; the other three ports remain typing-only + delegate-method seams (accepted boundary — see §1.3). Tree is 17 commits ahead of `origin/main` (local-only). Per-phase completion is tracked by the ✅ markers in §4 (Definition of Done).

> **Source of truth:** `refactor/explore/00–04_*.md`. All line anchors below were verified against `app/framework/framework_main_async.py` at baseline commit `906a49b` (542 passed). This plan executes the deferred work catalogued in `adversarial_review/00_FINAL_REPORT.md §4 (E1–E6)`.

## Goal

Split the 1192-LOC god-file `app/framework/framework_main_async.py` into cohesive <500-LOC modules behind hexagonal ports, break the 512-LOC `_run_loop` into small step methods (≤50 LOC each, ≤20 where the logic permits; the single-lock atomic `_step_commit_state` is an explicit ~90-LOC exception — see DoD §2), decompose `GlobalState` into additive typed slices, and modernize typing repo-wide — **keeping 542 tests green at every commit** and shrinking `framework_main_async.py` to a re-export shim.

---

## 1. Target End-State Architecture

### 1.1 Before (current)

```
app/framework/
├── framework_main_async.py   1192 LOC  ← god-file: 38 symbols, _run_loop=512 LOC, _pre_generate_next_loop=187 LOC
├── framework_state.py         488 LOC  ← GlobalState god-object: 65 attrs in one __init__
├── framework_conductor_async.py 436 LOC (~32 typing violations)
├── framework_mixer.py         357 LOC
├── framework_generator.py     407 LOC
└── framework_icecast.py       355 LOC
```

### 1.2 After

```
app/framework/
├── ports.py                   ~70 LOC   [NEW] Protocol declarations (ConductorPort, JobQueuePort,
│                                        AudioFetchPort, AuditSinkPort, MixerController) — E5
├── domain_audio.py            ~60 LOC   [NEW] PURE: calc_duration, to_two_channel, tile_to_loop
├── conductor_interaction.py   ~150 LOC  [NEW] process_actions, build_track_prompt,
│                                        load_available_models, build_fallback_response
├── job_queue.py               ~90 LOC   [NEW] submit_generator_job + await_jobs (wraps job_waiter)
├── audio_fetch.py             ~70 LOC   [NEW] GarageAudioAdapter.fetch (impure) — S3 + AAC decode
├── audit_recording.py         ~200 LOC  [NEW] flush_recording_buffers, _flush_lock,
│                                        append_loop_audit + the 6 _audit_* shapers
├── pregeneration.py           ~150 LOC  [NEW] PreGenerator (runs pipeline N+1; preserves cache divergence)
├── loop_orchestrator.py       ~480 LOC  [NEW] AsyncFrameworkLoop (lifecycle + _run_loop decomposed
│                                        into small ≤50-LOC _step_* methods), run_framework_loop_async, main
├── framework_main_async.py     ~40 LOC  ← RE-EXPORT SHIM only (frozen API preserved)
├── framework_state.py         ~560 LOC  ← GlobalState + additive slice view-properties (E3 pass-1)
├── framework_conductor_async.py 436→~420 LOC (typing modernized, E6)
├── framework_mixer.py         357 LOC   (unused import removed; MixerController satisfied structurally)
├── framework_generator.py     407 LOC   (unchanged this effort)
└── framework_icecast.py       355 LOC   (5× Optional modernized, E6)
```

All new modules are <500 LOC. `framework_main_async.py` becomes a thin shim that re-exports the frozen public/private surface so **zero callers change**.

### 1.3 Port interfaces (E5)

```python
# app/framework/ports.py  (new, modern typing)
from __future__ import annotations
from typing import Protocol, Any
from uuid import UUID
import numpy as np


class ConductorPort(Protocol):  # adapter: ConductorLLMAsync (existing, satisfies structurally)
    async def get_next_state_async(
        self,
        *,
        current_bpm: int,
        current_key: str,
        active_stems: list[dict],
        user_override,
        available_instruments: list,
        stem_history: list,
        llm_config: dict,
        available_models: list[dict],
    ) -> dict[str, Any]: ...


class JobQueuePort(Protocol):  # adapter: PostgresJobQueueAdapter (new, wraps _submit_job + job_waiter)
    async def submit(
        self,
        *,
        session_id: UUID,
        instrument: str,
        prompt: str,
        major_family: str,
        model_id: str,
        key: str,
        bpm: int,
        timbre_tags: list[str],
        bars: int,
    ) -> UUID: ...
    async def await_jobs(self, job_ids: list[UUID], timeout: float = 120.0) -> dict[UUID, str | None]: ...


class AudioFetchPort(Protocol):  # adapter: GarageAudioAdapter (new, wraps _fetch_audio)
    async def fetch(self, audio_path: str) -> np.ndarray | None: ...


class AuditSinkPort(Protocol):  # adapter: PostgresAuditAdapter (new, wraps _append_loop_audit + flush)
    async def append_loop(self, conductor_response: dict, active_stems: list[dict], loop_idx: int) -> None: ...
    async def flush(self) -> None: ...


class MixerController(Protocol):  # concrete Mixer satisfies structurally; PRIVATE-MEMBER
    sample_rate: int  # PROMOTION (Phase 11) DEFAULT-DEFERRED — see risk #5
    current_sample: int
    current_loop_end_sample: int

    def set_next_loop(self, tracks, *, next_loop_duration_samples: int, loop_idx: int) -> None: ...
    def pop_transition_event(self) -> object | None: ...
    def clear(self) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
```

**E5 port-wiring status (as of Phase 10 close-out, commit `aab8b43`, 2026-08-11):** Of the five
ports above, only **`ConductorPort` is constructor-injected** — `AsyncFrameworkLoop.__init__`
accepts `conductor=...` (commit `344a28f`, the E5 driving port). `JobQueuePort`,
`AudioFetchPort`, and `AuditSinkPort` are declared for **typing/documentation only**: the
orchestrator still reaches them through internal delegate methods (`_submit_job`,
`_fetch_audio`, `_append_loop_audit`), which tests exercise via `patch.object(loop, ...)`.
Full keyword-injection of the remaining three ports (the Phase 7b
`__init__(self, session_id, *, conductor, jobs, audio, audit, pregen)` spec) is **deferred**
— the typing-only Protocol + delegate-method seam is accepted as the current boundary
(option (a) of `refactor/review/final/quality.md` §E5). `MixerController` remains
typing-only — see Phase 11 below.

**Decision (flagged for reviewer):** Mixer stays a **concrete dependency** of the orchestrator for this effort. The `MixerController` Protocol is declared for typing/documentation only. The orchestrator continues to reach Mixer's private members (`_add_track_internal`, `_ensure_stereo`, `_current_loop_duration`, `mixer.lock`) at P10/P13 in the short term because promoting them touches the audio-critical real-time callback path (brief-01 risk #5). Full MixerPort extraction is **Phase 11, explicitly optional/deferred**.

### 1.4 GlobalState slice view-properties (E3 pass-1, additive)

Appended to `GlobalState` as `@property` read-views over the SAME `self.__dict__` (zero rename, no `__getattr__`/`__setattr__`):

| Property | Slice class | Attributes it views |
| --- | --- | --- |
| `state.musical` | `MusicalParams` | current_bpm, current_key, current_set_name, previous/active/next_stems, stem_history, llm_reasoning |
| `state.generation` | `GenerationControl` | is_generating, is_show_started, user_override, target_*_override, should_reset, generation_cfg_scale, generation_steps |
| `state.llm` | `LLMConfig` | llm_base_url, llm_api_key, llm_model |
| `state.levels` | `StemLevels` | stem_volumes, muted_stems, soloed_stems *(named `levels`, NOT `mixer` — avoids clash with framework Mixer)* |
| `state.loop_coord` | `LoopCoordination` | loop_count, last_actions, currently_playing_*, loop_history |
| `state.recording` | `RecordingState` | is_recording, recording_*, current_show_*, *_buffer, current_show_audio_file |
| `state.playback` | `PlaybackState` | currently_playing_show_id, is_playback_active |
| `state.stem_cache_view` | `StemCacheView` | last_generated_stems (+ cache_stem) |
| `state.catalog` | `InstrumentCatalog` | available_instruments, categorized/custom |
| `state.session` | `SessionConfig` | dj/audience passwords, audience_message(_ts), icecast_enabled |

The 3 dead ModelMgmt attrs (`model_states`, `model_errors`, `download_progress`) are **deleted** (verified dead, brief-03 §A). High-risk attrs (brief-03 §C ranks 1–8) are NOT relocated in pass-1.

---

## 2. Phased Task List (ordered; each keeps 542 green + independently committable)

**Ordering rationale:** Safety-net tests first (Phase 0) pin behavior so extraction can't silently regress. Then declare ports (Phase 1, pure typing). Then extract modules **lowest-risk-first** per brief-01 §D (audio pure-math → audit → conductor → job-queue → pregen → orchestrator). Each method extraction keeps a **thin delegating method** on `AsyncFrameworkLoop` so the test-reached private surface (`patch.object(loop, '_submit_job')`) keeps working. GlobalState slicing (additive) and typing (mechanical ruff) come last, after structure is stable.

| Phase | Name | Risk | Depends on |
| --- | --- | --- | --- |
| 0 | Red TDD safety net (characterization) | low | — |
| 1 | Declare `ports.py` (Protocols, typing only) | low | — |
| 2 | Extract `domain_audio.py` + `audio_fetch.py` | medium | 0,1 |
| 3 | Extract `audit_recording.py` | low-medium | 0,1 |
| 4 | Extract `conductor_interaction.py` | medium | 0,1 |
| 5 | Extract `job_queue.py` | medium | 1 |
| 6 | Extract `pregeneration.py` (preserve cache divergence) | **high** | 2,3,4,5 |
| 7a | Decompose `_run_loop` into `_step_*` methods (in place) | **high** | 2–6 |
| 7b | Move orchestrator → `loop_orchestrator.py`; shim `framework_main_async.py` | **high** | 7a |
| 8 | GlobalState additive slice view-properties (E3) | medium | — |
| 9 | E6 typing: ruff UP006/UP007/UP045 `--fix` + manual `Any` | medium | 7b |
| 10 | Enable `[tool.ruff.lint]` enforcement | low | 9 |
| 11 | *(optional/deferred)* Full `MixerController` port | **very high** | — |

---

## 3. Per-Phase Specs

> **Universal verification gate (every phase):**
>
> ```bash
> .venv/bin/python -m pytest tests/ --timeout=30 -q          # MUST stay 542 passed (or +new)
> .venv/bin/python -m ruff check --select UP006,UP007,UP045,F401 app/ tests/ 2>&1 | tail -1   # MUST not increase (F401 included: UP --fix orphans `from typing import ...` — Phase 9 cleans in-pass)
> ```
>
> **Universal commit convention:** `refactor(framework): <phase> — <one-line>`; body cites phase # + brief risk #s respected. **Rollback:** every phase is one commit → `git revert <sha>`.

### Phase 0 — Red TDD safety net (characterization tests)

- **Objective:** Pin currently-untested behavior so the extraction phases cannot silently regress it. Tests assert **current** behavior → green immediately. **Covers brief-04 §C CRITICAL gaps 1–5 AND HIGH gaps 6–8.** Gaps 6–8 MUST land here (before Phase 7a), not be deferred: gap 6 covers the EXACT pregen-vs-fresh code that 7a decomposes; gap 7 the `last_actions` content; gap 8 the flush DB-write + failure requeue. Without them a 7a desync stays 542-green while silently changing audible music.
- **Files:** `tests/test_framework_characterization.py` (NEW); `tests/test_frozen_api.py` (NEW).
- **Red/characterization tests (cite brief-04 gap IDs):**
  - **Gap 1** — `test_process_actions_retain_add_remove_dedup`: feed `[{action_type:retain,stem_index:0},{action_type:add,...},{action_type:remove,stem_index:1},{duplicate add}]`; assert ordering, retained `_age` = original `_age`+1, added `_age`=0, dedup key `model_id_major_sub_timbre_notation_fx`, removed excluded. **⚠️ Uses `action_type` key, NOT `action`** (verified L122).
  - **Gap 2** — `test_submit_job_creates_pending_row_with_ttl`: SQLite in-memory `DatabaseManager`, call `loop._submit_job(...)`, assert `GeneratorJob` row `status=="pending"`, `expires_at≈now+24h`, return is a `UUID`.
  - **Gap 3** — `test_fetch_audio_empty_bytes_and_exception_return_none`: patch garage `get_object`→`b""` (⇒ None) and →raise (⇒ None, swallowed); assert no propagation.
  - **Gap 4** — `test_run_loop_loop1_handoff_records_initial`: extend `_FakeMixer` (brief-04 §D); drive one loop-1 iteration; assert tracks added at live `mixer.current_sample`, `_current_loop_duration` set, `record_loop_transition(1,…)` called.
  - **Gap 5** — `test_run_loop_subsequent_set_next_loop_kwargs`: loop_idx>1 path; assert `mixer.set_next_loop(tracks, next_loop_duration_samples=…, loop_idx=…)` receives correct kwargs; **no `current_loop_end_sample` set on this path**.
  - **Gap 6 (HIGH, REQUIRED before 7a)** — `test_run_loop_pregen_and_fresh_paths_produce_equal_state_delta`: drive one loop via the fresh-LLM path AND one via the pregen-ready path (same conductor response); assert identical `state` deltas (bpm, key, set_name, llm_reasoning, active_stems, last_actions). Pins the divergence that 7a will merge.
  - **Gap 7 (HIGH)** — `test_run_loop_populates_last_actions`: after one loop with retain/add/remove actions, assert `state.last_actions` content (Retained/Added/Removed with parsed sub-family) on the REAL loop, for both fresh and pregen branches.
  - **Gap 8 (HIGH)** — `test_flush_recording_buffers_writes_db_and_requeues_on_failure`: populate both buffers, assert `bulk_insert_mappings` called with both tables + buffers cleared; fake a failing session, assert buffers are restored (prepended back).
  - **Frozen-API smoke** — `test_frozen_api_importable`: import all 9 names from `app.framework.framework_main_async` (non-None) AND **instantiate** `AsyncFrameworkLoop(uuid4())` (catches a constructor-signature break the import-only check misses) AND assert `getattr(module, 'decode_aac')` + `create_garage_client_from_env` resolve to callables (catches the string-patch silent-no-op, brief-02 §D).
- **Harness:** bare `async def` (auto mode); **STRENGTHEN `_FakeMixer`** — record every call (`set_next_loop`, `_add_track_internal`, `pop_transition_event`, `clear`), add a `stop_after_n_steps` hook so full `_run_loop` tests terminate instead of hanging, and stub `lock` ctx-manager, `_current_loop_duration`, `sample_rate`, `current_sample`, `current_loop_end_sample`. monkeypatch `asyncio.sleep`→instant for `_run_loop` tests. **EXPAND the `state`-singleton save/restore** beyond `test_loop_fixes._reset_audit_state`'s 6 attrs to the ~15 attrs a real loop mutates (active/next/previous_stems, stem_history, loop_count, last_actions, current_bpm/key/set_name, llm_reasoning, muted/soloed/volumes, target_*_override) — else cross-test leakage/flakes. Prefer fresh `GlobalState()` per test for pure-state cases.
- **Verification:** pytest green at ≥542+N. **Risks respected:** none yet (pure test addition).
- **Commit:** `test(framework): add characterization net for E1-E6 refactor (brief-04 gaps 1-8 + frozen-API)`

### Phase 1 — Declare `ports.py`

- **Objective:** Lay the E5 port foundation as pure typing (Protocol = structural, no runtime change).
- **Files:** `app/framework/ports.py` (NEW).
- **Code:** the 5 Protocols in §1.3 (modern typing).
- **Verification:** pytest green; `python -c "import app.framework.ports"` succeeds. No source module references it yet → zero behavior change.
- **Commit:** `refactor(framework): add hexagonal port protocols (E5) — no behavior change`

### Phase 2 — Extract `domain_audio.py` + `audio_fetch.py`

- **Objective:** Lift pure audio math + the S3/AAC fetch out. Eliminates the dormant string-patch risk (brief-02 §D ⚠️).
- **Files:** `app/framework/domain_audio.py` (NEW, pure), `app/framework/audio_fetch.py` (NEW, impure), `app/framework/framework_main_async.py` (modified), `tests/test_worker_fetch_audio.py` (migrate patch strings), `tests/test_framework_characterization.py` (add guard test).
- **Red tests (new location):** `from app.framework.domain_audio import calc_duration, to_two_channel, tile_to_loop`; assert parity with the old `_to_two_channel`/`calc_duration` (re-exported) → identity. `from app.framework.audio_fetch import GarageAudioAdapter`.
- **Code moves:**
  - `calc_duration` (L47–56) → `domain_audio.calc_duration`.
  - `_to_two_channel` (L58–66) → `domain_audio.to_two_channel` (rename internal; keep `_to_two_channel` alias re-exported).
  - Extract the P9 tiling block (L531–571) → `domain_audio.tile_to_loop(audio, loop_duration_samples)`.
  - `_fetch_audio` body (L851–879) → `audio_fetch.GarageAudioAdapter.fetch`. **Keep `AsyncFrameworkLoop._fetch_audio` as a thin `async def _fetch_audio(self, audio_path): return await self._audio.fetch(audio_path)`** so `patch.object(loop,'_fetch_audio')` and `test_worker_fetch_audio` direct calls still work.
  - **String-patch migration (brief-02 §D ⚠️):** move `decode_aac`/`create_garage_client_from_env` imports into `audio_fetch.py`; update `tests/test_worker_fetch_audio.py:43,44,74,75` patch strings to `app.framework.audio_fetch.decode_aac` / `create_garage_client_from_env`; **add a guard test** `test_decode_aac_patch_actually_applies` that asserts the patched mock is invoked (prevents silent no-op).
  - `framework_main_async.py` keeps `from app.garage_client import create_garage_client_from_env` and `from app.aac_encoder import decode_aac` AT TOP LEVEL too (**import-compatibility only** — this does NOT neutralize the silent string-patch no-op: a patch replaces the name on the SHIM module while the real call resolves in `audio_fetch`'s namespace. The guard test `test_decode_aac_patch_actually_applies`, asserting the patched mock is invoked at the call site, is the actual protection. Migrating the test patch strings to `app.framework.audio_fetch.*` is the durable fix.)
- **Re-exports added to `framework_main_async.py`:** `from .domain_audio import calc_duration, _to_two_channel` (alias).
- **Risks respected:** risk #4 (cache divergence) — N/A here (this is fetch, not cache_stem). String-patch risk (brief-02 §D) — mitigated by migration + guard test.
- **Verification:** pytest green; `import app.framework.audio_fetch` works.
- **Commit:** `refactor(framework): extract pure audio math + garage fetch (Phase 2)`

### Phase 3 — Extract `audit_recording.py`

- **Objective:** Move all show-audit persistence (already isolated by `_flush_lock`) behind `AuditSinkPort`.
- **Files:** `app/framework/audit_recording.py` (NEW), `framework_main_async.py` (modified).
- **Red tests:** `from app.framework.audit_recording import flush_recording_buffers, append_loop_audit, _flush_lock`; assert `framework_main_async._flush_lock is audit_recording._flush_lock` (identity — brief-02 §E).
- **Code moves:** `flush_recording_buffers` (L73–115), `_flush_lock` (L70), `_append_loop_audit` (L881–912), `_relative_show_ms` (L914–917), `_audit_prompt_context`/`_audit_action_row`/`_audit_stem_details`/`_audit_action_description` (L919–977) → `audit_recording.py`. `append_loop_audit` becomes a module function `(conductor_response, active_stems, loop_idx)` taking an explicit `state`/snapshot rather than `self` (decouple from the class). **Keep `AsyncFrameworkLoop._append_loop_audit` as thin delegate** (`patch.object(loop,'_append_loop_audit')` + direct calls in `test_loop_fixes` #28–30 still work).
- **Re-exports:** `from .audit_recording import flush_recording_buffers, _flush_lock` in `framework_main_async.py` (preserves `routes/shows.py:298` lazy import + `test_loop_fixes` import + lock identity).
- **Risks respected:** brief-01 risk #2 — `append_loop_audit` takes a snapshot, never live `state`; the orchestrator's `state.lock` scope is unchanged.
- **Verification:** pytest green; lock-identity test green.
- **Commit:** `refactor(framework): extract audit/recording persistence behind AuditSinkPort (Phase 3)`

### Phase 4 — Extract `conductor_interaction.py`

- **Objective:** Move prompt building + action transformation. Removes the duplicated `load_available_models` block (P3 L? and pregen L1117–1136).
- **Files:** `app/framework/conductor_interaction.py` (NEW), `framework_main_async.py` (modified).
- **Red tests:** `from app.framework.conductor_interaction import process_actions, build_track_prompt, load_available_models, build_fallback_response`; assert `process_actions` parity with Phase-0 gap-1 test.
- **Code moves:** `process_actions` (L118–178) → module fn; `_build_prompt` (L773–804) → `build_track_prompt(track, key, bpm)`; extract `load_available_models()` (the `generator.models` + `models_config.json` read duplicated at P3 and pregen L1117–1136) → single module fn; extract the fallback-response builder (P4 + pregen L1117–1149) → `build_fallback_response(current_bpm, current_key, active_stems, err)`. **Keep `AsyncFrameworkLoop._build_prompt` thin delegate.**
- **Re-exports:** `from .conductor_interaction import process_actions` (preserves `simulation/session_state.py:13` import — brief-02 A7).
- **Risks respected:** **brief-01 risk #1 (hidden mutation)** — `process_actions` mutates `active_stems[idx]["_original_details"]["_age"]` in place (verified L139; key is `action_type` at L132). The orchestrator must pass the **same live list** (not a copy) so `_age` accounting flows to P6/P11; **document this with a comment**. Do NOT "fix" the mutation. **Gap-1 test must assert mutation on the INPUT list** using stems that carry `_original_details` — a defensive `deepcopy` refactor would pass a return-value test but silently break P6/P11 `_age` accounting.
- **Verification:** pytest green; gap-1 characterization test still green.
- **Commit:** `refactor(framework): extract conductor prompt/action shaping (Phase 4)`

### Phase 5 — Extract `job_queue.py`

- **Objective:** Single Postgres-queue touchpoint behind `JobQueuePort`.
- **Files:** `app/framework/job_queue.py` (NEW), `framework_main_async.py` (modified).
- **Red tests:** `from app.framework.job_queue import PostgresJobQueueAdapter`; assert `submit(...)` parity with Phase-0 gap-2 test.
- **Code moves:** `_submit_job` body (L806–849) → `PostgresJobQueueAdapter.submit`; wrap `wait_for_multiple_jobs` (job_waiter) → `PostgresJobQueueAdapter.await_jobs`. **Keep `AsyncFrameworkLoop._submit_job` thin delegate** → `await self._jobs.submit(...)` (so `patch.object(loop,'_submit_job')` still works — brief-02 §D).
- **Risks respected:** none new (DB insert only).
- **Verification:** pytest green; gap-2 test green.
- **Commit:** `refactor(framework): extract job-queue adapter behind JobQueuePort (Phase 5)`

### Phase 6 — Extract `pregeneration.py` 🔴 HIGH RISK

- **Objective:** Extract `_pre_generate_next_loop` (L979–1166, the second orchestrator) into `PreGenerator` composing the Phase 2–5 modules.
- **Files:** `app/framework/pregeneration.py` (NEW), `framework_main_async.py` (modified).
- **Red tests:** existing `test_async_framework` pregen tests (#4,#5,#12,#13) + `test_loop_fixes:#300` pin `_pre_generate_next_loop`; ensure they stay green. **ADD:** (a) `test_pregen_skips_job_when_foreground_already_cached` — seed `self.stem_cache[cache_key]`, run pregen, assert `_submit_job` NOT called for that key (pins the L1068 skip); (b) `test_cache_key_is_identical_across_all_4_sites` — extract the key into ONE pure fn `make_cache_key(model_id, prompt, bpm, key, bars)` and assert all foreground+background sites build the same key (the key is currently duplicated inline at 4 sites — drifting one breaks cross-path cache-hits silently); (c) assert `np.any(track != 0)` on pregen result tracks (pregen tests assert keys/loop_idx but not non-silence — a dropped `self.stem_cache` write at L1096 yields silent zero arrays and passes green).
- **Code moves:** `_pre_generate_next_loop` body → `PreGenerator.generate(for_loop_idx, snapshot)`. **Construct `PreGenerator(stem_cache=self.stem_cache, jobs=…, audio=…, conductor=…, audit=…)` — it MUST share the loop's SINGLE `self.stem_cache` dict (L203), NOT get its own** (a separate cache makes pregen re-submit every job + duplicate stems while the two `state.cache_stem` divergence tests still pass green). **Keep `AsyncFrameworkLoop._pre_generate_next_loop(for_loop_idx, snapshot)` thin delegate** → stores result on `self._pregen_results`, sets `self._pregen_done` (preserves the ~15 test assertions on `_pregen_*` attrs — brief-02 §D).
- **🔴 RISK #4 (LOAD-BEARING DIVERGENCE — verified L527 vs L1096):** `state.cache_stem` (the GlobalState 16-entry LRU) is called ONLY on the foreground path (P8, L527, under `state.lock`); the background `_pre_generate_next_loop` writes ONLY `self.stem_cache[cache_key]` (L1096) and NEVER calls `state.cache_stem` (verified by grep — `cache_stem` has exactly one call site, L527). The shared audio-fetch step does the common `self.stem_cache[cache_key]` write; the orchestrator's foreground step calls `state.cache_stem` after. **Do not "unify" these — replicate exactly.** Add `test_pregeneration_does_not_route_through_cache_stem` (patch `state.cache_stem`, run pregen, assert NOT called) AND `test_foreground_loop_routes_through_cache_stem` (assert called). *(Line ref corrected from L1155→L1096 per round-1 claim-verify; L1155 is `run_framework_loop_async`.)*
- **Risks respected:** risk #4 (cache divergence) — primary focus.
- **Verification:** pytest green; BOTH divergence regression tests green.
- **Commit:** `refactor(framework): extract pregeneration worker, PRESERVE cache_stem divergence (Phase 6)`

### Phase 7a — Decompose `_run_loop` into `_step_*` methods 🔴 HIGH RISK

- **Objective:** E2 — break the 512-LOC `_run_loop` into small step methods, **in place** (still in `framework_main_async.py`). **Line budget (measured against baseline):** target ≤50 LOC/step, ≤20 where the logic permits; the largest phases as-is are P3(~50), P10(~47), P11(~85). Sub-decompose P3 (`_load_available_models` already lifts to Phase 4) and P11's action-log rebuild (L640–658) into a pure helper so the atomic lock body itself stays tight. **`_step_commit_state` is the explicit ≤90-LOC single-lock exception** (measured ~85) — see risk #3.
- **Files:** `framework_main_async.py` (modified only).
- **Red tests:** the Phase-0 characterization tests (gaps 4,5) + watchdog test (#32) pin end-to-end behavior; add `test_run_loop_shutdown_exits_cleanly` (brief-04 HIGH gap 4) and `test_run_loop_transition_records_under_snapshot` (HIGH gap 5).
- **Code moves:** extract each `_run_loop` phase (P0–P14, brief-01 §B) into a method:
  `_step_init`, `_step_wait_for_start`, `_step_decide_pregen`, `_step_read_snapshot` (P3), `_step_call_conductor` (P4), `_step_parse_actions` (P5), `_step_build_next_stems` (P6), `_step_submit_jobs` (P7), `_step_await_and_fetch` (P8), `_step_tile_audio` (P9), `_step_commit_to_mixer` (P10), `_step_commit_state` (P11), `_step_record_and_pregen` (P12), `_step_await_transition` (P13). `_run_loop` becomes a ≤60-LOC driver calling these in sequence + the P14 try/except watchdog.
- **🔴 RISK #3 (P11 ATOMICITY — verified L616–688, record_loop_transition OUTSIDE at L692):** `_step_commit_state` MUST remain a **single `async with state.lock:` block** holding all ~40 mutations (previous_stems, stem_history, active_stems, next_stems=[], muted/soloed/volumes.clear(), loop_count++, pregen BPM/key/set/reasoning, last_actions rebuild, override application, state_snapshot capture). It is the explicit ≤90-LOC exception with a comment: *"Atomic single-lock transition — do not split (brief-01 risk #3)."* `record_loop_transition` (takes blocking `sync_lock`) stays **outside** `state.lock` (L692, ~L735). **VERIFY WITH A TEST, not just a comment:** `test_step_commit_state_is_single_lock_block` — patch `state.record_loop_transition` to assert `state.lock.locked()` is False at call time (proves it's outside the lock) and assert the full mutation set applied atomically.
- **🔴 RISK #2 (LOCK SCOPE):** no step may acquire `state.lock` across I/O (LLM/DB/S3/file). Snapshots in, I/O out, commit back under lock. **NOTE `_step_read_snapshot` (P3):** today it reads `config/models_config.json` (L377–391) deliberately OUTSIDE the lock — keep that. **Gate = a source-inspection test** `test_no_io_inside_state_lock` (grep `_run_loop`/`_step_*` for `open(` / `await` inside any `async with state.lock:` → zero hits) covering ALL steps incl. `_step_read_snapshot`, not just `_step_call_conductor`/`_step_submit_jobs`.
- **Verification:** pytest green; new shutdown + transition tests green.
- **Commit:** `refactor(framework): decompose _run_loop into _step_* methods, preserve P11 atomicity (Phase 7a)`

### Phase 7b — Move orchestrator → `loop_orchestrator.py`; shim `framework_main_async.py` 🔴 HIGH RISK

- **Objective:** Achieve the E1 end-state: `framework_main_async.py` < 500 LOC as a re-export shim.
- **Files:** `app/framework/loop_orchestrator.py` (NEW), `app/framework/framework_main_async.py` (→ shim), `tests/test_async_framework.py` + `tests/test_loop_fixes.py` (update imports to verify shim re-exports still resolve — **no patch-string changes needed** since all names are re-exported).
- **Code moves:** move `AsyncFrameworkLoop`, `run_framework_loop_async` (L1169–1186), `main` + `__main__` (L1179–1192) into `loop_orchestrator.py`. **Constructor MUST stay injectable to satisfy E5/CLAUDE.md DI AND preserve the one-arg call:** `def __init__(self, session_id, *, conductor=None, jobs=None, audio=None, audit=None, pregen=None)` where each `None` default constructs the real adapter (`ConductorLLMAsync()`, `PostgresJobQueueAdapter()`, `GarageAudioAdapter()`, `PostgresAuditAdapter()`, `PreGenerator(stem_cache=self.stem_cache, …)`). This keeps `AsyncFrameworkLoop(session_id)` working for `app_ui.py:90` + 12 test sites AND makes ports injectable for tests (cleaner than `patch.object` private-method patching — a genuine E5 win). The `stem_cache`/`_pregen_*` instance attrs are still set in `__init__` (preserves the ~15 test assertions).
- **`framework_main_async.py` shim** (≈40 LOC):

  ```python
  from app.framework.loop_orchestrator import AsyncFrameworkLoop, run_framework_loop_async, main
  from app.framework.audit_recording import flush_recording_buffers, _flush_lock
  from app.framework.conductor_interaction import process_actions
  from app.framework.domain_audio import calc_duration, _to_two_channel
  from app.garage_client import create_garage_client_from_env  # string-patch target (brief-02 §D)
  from app.aac_encoder import decode_aac  # string-patch target (brief-02 §D)

  __all__ = [
      "AsyncFrameworkLoop",
      "run_framework_loop_async",
      "flush_recording_buffers",
      "process_actions",
      "calc_duration",
      "_to_two_channel",
      "_flush_lock",
      "create_garage_client_from_env",
      "decode_aac",
      "main",
  ]
  ```

- **Risks respected:** frozen-API re-export contract (brief-02 §E); all test-reached private surface intact via the class living in `loop_orchestrator` (patches use `patch.object(loop, …)` = instance-level, unaffected by module move; string patches now target `loop_orchestrator` only where Phase 2 migrated them).
- **Verification:** pytest green; **`test_frozen_api_importable` (Phase 0, now incl. instantiation) green**; add `test_loop_constructs_with_default_ports` + `test_loop_accepts_injected_fake_ports` to the frozen-API suite; `wc -l app/framework/framework_main_async.py` < 500. **Add a `__main__` stub to the shim** (`python -m app.framework.framework_main_async` must not break) or document the entry moved to `loop_orchestrator`.
- **Commit:** `refactor(framework): relocate orchestrator, reduce framework_main_async to re-export shim (Phase 7b)`

### Phase 8 — GlobalState additive slice view-properties (E3)

- **Objective:** Introduce typed slice views; zero rename.
- **Files:** `app/framework/framework_state.py` (modified — append properties + slice classes), `app/framework/state_slices.py` (NEW — the slice classes), `tests/test_state_slices.py` (NEW).
- **Red tests:** `assert state.musical.current_bpm is state.current_bpm` (live view, same object); mutating `state.current_bpm` reflects in `state.musical.current_bpm`; each slice exposes its documented attrs; `state.levels` (not `mixer`) exists.
- **Code:** slice classes (`MusicalParams`, etc.) as read-view dataclasses/property-forwarders over `state.__dict__` (brief-03 §B pattern); 10 `@property` accessors on `GlobalState`. **Delete** `model_states`, `model_errors`, `download_progress` (verified dead). Add `test_state_slices.py` asserting identity-view + no `__getattr__`/`__setattr__` override.
- **Risks respected:** brief-03 §C — high-risk attrs NOT relocated; additive only; no repo-wide migration.
- **Verification:** pytest green (existing `test_concurrency_fixes` #33–39 + `test_state.py` unaffected).
- **Commit:** `refactor(framework): add additive GlobalState slice views (E3 pass-1) (Phase 8)`

### Phase 9 — E6 typing modernization

- **Objective:** Eliminate banned `typing.List/Dict/Tuple/Optional`; manual `Any` pass.
- **Files:** repo-wide (mechanical), then `framework_conductor_async.py`, `lib/recording_*.py`, `routes/schemas.py` (manual).
- **Red test / guard:** `test_no_banned_typing_in_framework` — grep `app/framework/` for `typing.List|typing.Dict|typing.Tuple|typing.Optional|: List|: Dict` → assert zero hits.
- **Code:** (1) **FIRST capture the re-baseline:** `.venv/bin/python -m ruff check app/ tests/ > refactor/ruff_baseline.txt` (the count is ruff-version/scope-sensitive — do NOT trust a hardcoded number). (2) `.venv/bin/python -m ruff check --select UP006,UP007,UP045,F401 --fix app/ tests/` — **include F401**: converting `List`→`list` orphans `from typing import List` (verified empirically: F401 36→60, +24 on app/ alone); F401 must be cleaned in the SAME pass or the 24 new unused-import errors detonate at Phase 10. (3) Manual `Any`→concrete/`object` in `framework_conductor_async.py` (~6), `lib/recording_postprocess.py`, `lib/recording_metadata.py`. (4) `routes/schemas.py` (~40 `Optional`→`X|None`): **add a COMMITTED test** `test_schemas_accept_optional_as_union` that instantiates every pydantic model with a None field (not a manual smoke) — pydantic v2 accepts `X|None` on ≥3.10 but verify; revert schemas.py + flag if any regression. (5) Remove now-empty `from typing import …` lines.
- **🔴 pydantic caution:** `routes/schemas.py` (~40 `Optional`) — UP045 converts `Optional[X]`→`X | None`; verify pydantic v2 accepts it (it does on ≥3.10, but **run `tests/test_api.py` + a quick model-instantiation smoke** before committing). If any pydantic regression, revert `schemas.py` and flag.
- **Verification:** pytest green; `.venv/bin/python -m ruff check --select UP006,UP007,UP045,F401 app/ tests/` → **0**; default ruff (E+F) **not increased** vs `refactor/ruff_baseline.txt` (re-measured, not the stale 735).
- **Commit:** `style(types): modernize typing List/Dict/Optional→builtin/PEP-604 (E6) (Phase 9)`

### Phase 10 — Enable `[tool.ruff.lint]` enforcement

- **Objective:** Lock in E6 so regressions are caught.
- **Files:** `pyproject.toml`.
- **Code:** add `[tool.ruff.lint]` with `select = ["E","F","UP006","UP007","UP045"]` (`F` already includes F401 — catches orphaned-import regressions going forward). **Stage `I` (import sorting) separately** — run `--select I --diff` first; if it churns >a handful of files, defer `I` to its own commit (it is orthogonal to E6).
- **Verification:** `.venv/bin/python -m ruff check app/ tests/` exits 0 (or only pre-existing baseline items, **not increased**). pytest green.
- **Commit:** `chore(ruff): enable UP006/UP007/UP045 lint enforcement (Phase 10)`
- **Status: COMPLETE (2026-08-11 close-out).** The enforced set is the FULL `select = ["E","F","I","UP006","UP007","UP045"]` — `I` (import sorting) was staged separately per the spec above and landed in the close-out (commit `aab8b43`, `style(e10): enforce I (import sort)`). E/F landed in `b2999cc` (`style(e10): enforce E,F lint rules`); the repo-wide format sweep that greens `ruff format --check` landed in `1d77398`. `ruff check app/ tests/` → **"All checks passed!"** (0 debt). Regression checkpoint: `refactor/ruff_baseline.txt`.

### Phase 11 — *(OPTIONAL / DEFAULT-SKIPPED)* Full `MixerController` port 🔴 VERY HIGH RISK

- **Objective:** Promote Mixer's private members (`_add_track_internal`, `_ensure_stereo`, `_current_loop_duration`, `mixer.lock`) to a public `MixerController`-satisfying surface and remove direct private reach from the orchestrator.
- **Status:** **DEFERRED — U1 (pinning) LANDED, U2 (additive promotion) LANDED, U3 (migration) LANDED, U4 (ctor-injection) still pending (NOT COMPLETE).** U1 froze the four audio-path invariants a future port must preserve; U2 then promoted them to a public `MixerController`-satisfying surface **additively** (zero behavior change); U3 rewired the orchestrator off its private `Mixer` reach so it now calls `prime_loop` + `loop_position_seconds` exclusively, and the U1 pins evolved to follow the atomicity into its new home. The remaining step (U4 — constructor-inject the `Mixer` behind the `MixerController` port, matching the Phase 7b `__init__(session_id, *, conductor, …)` seam) now has a green structural+behavioral guard rail and a live public surface. Still touches the real-time `_callback` audio path (brief-01 risk #5); the dual-lock coordination (state.lock vs mixer.lock) at P10/P13 can deadlock or drop crossfade events if done wrong. The `MixerController` Protocol from Phase 1 gives typing now; U4 is a separate, dedicated effort with its own adversarial audio-path review.
- **U1 pinning — LANDED (branch `refactor/mixer-pin-lock-atomicity`):** characterization tests freeze the orchestrator→`Mixer` private-member reach (`_add_track_internal`/`_ensure_stereo`/`current_loop_end_sample`/`_current_loop_duration`/`mixer.lock`) so a port cannot silently regress audio. Four invariants pinned, all GREEN at baseline (no production code changed). *(In U3 the invariant-1 + invariant-3 pins below EVOLVED — retargeted to follow the atomicity into `Mixer.prime_loop` / the `loop_position_seconds` delegation; their CURRENT test names are in the U3 bullet. Invariants 2 + 4 are unchanged.)*
  1. **P10 atomicity (structural)** — `tests/test_loop_lock_safety.py::test_step_commit_to_mixer_loop1_is_single_lock_batch`: the loop-1 handoff is ONE atomic sync `with self.mixer.lock:` batch of exactly 4 statements (live `current_sample` read → per-track `_add_track_internal(_ensure_stereo(audio), …)` → `current_loop_end_sample` write → `_current_loop_duration` write), with no `await`/`open()` inside the lock. The load-bearing guard against a U2 promotion splitting the batch.
  2. **P10 lock-held (behavioral)** — `tests/test_mixer_controller_characterization.py::test_p10_loop1_holds_lock_during_every_batch_write`: a deterministic lock-spy (no racing threads) records `mixer.lock.locked()==True` at each of the three mutating writes, and that the boundary is computed from the LIVE `current_sample`.
  3. **Dual-lock ordering (structural)** — `tests/test_loop_lock_safety.py`: forward `state.lock→mixer.lock` only (`test_dual_lock_ordering_state_lock_holds_mixer_clear` holds state.lock across `mixer.clear()`), no reverse nesting `mixer.lock→state.lock` (`test_no_mixer_lock_nests_state_lock` — the R5 deadlock direction), and P13 sequential siblings never nested (`test_p13_state_lock_released_before_mixer_lock_read`).
  4. **Crossfade (behavioral)** — `tests/test_mixer_controller_characterization.py::test_crossfade_transition_event_round_trip`: `set_next_loop` preserves the current boundary (Bug-1), the real `_callback` fires the transition at the aligned boundary advancing `current_loop_end_sample`, and `pop_transition_event` returns the loop idx then atomically clears (no double-fire).
- **U2 additive promotion — LANDED (branch `refactor/mixer-controller-surface`):** promoted the orchestrator→`Mixer` private reach to a public `MixerController`-satisfying surface **additively** — zero behavior change: the privates (`_add_track_internal`/`_ensure_stereo`/`_current_loop_duration`/`mixer.lock`) still coexist and the orchestrator still reaches them inline; migrating the orchestrator to *call* the new surface is U3. New public surface on concrete `Mixer`:
  1. **`prime_loop(tracks, *, duration_samples)`** — byte-for-byte delegation of the P10 loop-1 batch (`loop_steps._step_commit_to_mixer`, the `_loop_idx == 1` branch): reads `current_sample` INSIDE `self.lock`, adds each track (mono-coerced via `_ensure_stereo`) at that live position, then sets `current_loop_end_sample` and `_current_loop_duration` from the live position.
  2. **`loop_position_seconds() -> float`** — byte-for-byte delegation of the P13 `current_ahead` read (`loop_steps._step_await_pregen`): snapshots the live boundary + position under one `self.lock`, then divides by `sample_rate` (positive = ahead of boundary; negative = behind schedule).
  3. **`ensure_stereo`** — public alias bound to the existing `_ensure_stereo` `@staticmethod` (the same staticmethod object; NOT `domain_audio.to_two_channel`).
  Extended `ports.MixerController` Protocol members: `prime_loop` + `loop_position_seconds` (the docstring was updated in U3 to note the privates now remain solely as the backing implementation inside `Mixer` — the orchestrator no longer reaching them directly). TDD-red suite `tests/test_mixer_controller_surface.py` pins the public surface against the privates: `prime_loop` reproduces the loop-1 batch byte-for-byte; the mono-coercion matrix is shared so the surface can never silently swap to `to_two_channel`; `loop_position_seconds` matches the live-boundary arithmetic. The lock acquired inside both new methods is the SAME `threading.Lock` the daemon `_callback` holds during the crossfade — dual-lock timing is preserved, so the U1 P10 atomicity pin still holds.
  U3 (rewire the orchestrator to call the new surface) has now **LANDED** — see below. U4 (constructor-inject the `Mixer`) remains **pending**.
- **U3 migration — LANDED (branch `refactor/mixer-controller-migrate`):** the orchestrator now reaches `Mixer` ONLY via the `MixerController` surface — `_step_commit_to_mixer`'s loop-1 branch is a single `self.mixer.prime_loop(tracks_to_use, duration_samples=…)` call (U3a), and `_step_await_pregen`'s P13 boundary read is a single `self.mixer.loop_position_seconds()` call (U3b). Zero `with self.mixer.lock:` / `_add_track_internal` / `_ensure_stereo` / `_current_loop_duration` reach remains in `loop_steps.py` or `loop_orchestrator.py`; that private reach now lives solely inside `Mixer`. **The U1 pins EVOLVED (retargeted), never deleted, so the atomicity invariant moved WITH the code:** invariant 1 (P10 atomicity) is now TWO pins — `test_step_commit_to_mixer_loop1_is_single_prime_loop_call` asserts the loop-1 branch is a single `prime_loop` delegation with zero `mixer.lock` in the orchestrator, and `test_prime_loop_is_single_lock_batch` pins the SAME 4-statement sync `with self.lock:` batch inside `Mixer.prime_loop`; invariant 3 (P13) is now `test_p13_delegates_boundary_read_no_mixer_lock` (asserts the `loop_position_seconds()` delegation + zero `mixer.lock`). A new permanent source-guard, `test_orchestrator_has_no_private_mixer_reach` (AST scan of `loop_steps.py` + `loop_orchestrator.py`), makes the zero-private-reach migration non-regressable: any reintroduction of `_add_track_internal`/`_ensure_stereo`/`_current_loop_duration` or `with self.mixer.lock:` fails fast. `test_mixer_controller_characterization.py::test_p10_loop1_holds_lock_during_every_batch_write` now drives the delegation through `_LockSpyMixer.prime_loop` and asserts `prime_loop_calls == 1`. Invariants 2 (lock-held) + 4 (crossfade round-trip) are unchanged. The lock acquired inside both methods is the SAME `threading.Lock` the daemon `_callback` holds during the crossfade, so dual-lock timing and the crossfade round-trip are preserved.
  U4 (constructor-inject the `Mixer` behind the `MixerController` port — `__init__(session_id, *, conductor, mixer, …)` matching the Phase 7b port-wiring seam, so the orchestrator takes the mixer as an injected dependency rather than constructing it inline via `Mixer()` in `start()`) remains **pending**.
- **Risks respected:** risk #5 — load-bearing invariants now PINNED by the U1 structural+behavioral tests; the deadlock surface itself is unchanged (mixer still concrete). Mitigation status: **deferred → guarded**.

---

## 4. Definition of Done (measurable gates)

1. `wc -l app/framework/framework_main_async.py` → **< 500** (target ~40-LOC re-export shim). ✅ Phase 7b.
2. `_run_loop` → **≤ ~60 LOC** orchestrator calling `_step_*` methods, each **≤50 lines** (≤20 where achievable) EXCEPT `_step_commit_state` (~85–90, single-lock atomic — measured P11 body ~85; risk #3). Sub-decompose P11's action-log rebuild into a pure helper to keep the lock body itself tight. ✅ Phase 7a.
3. `ruff check --select UP006,UP007,UP045,F401 app/ tests/` → **0**; default ruff (E+F) **must not increase** vs the re-baseline captured at the START of Phase 9 into `refactor/ruff_baseline.txt` (do NOT hardcode 735 — the count is ruff-version/scope-sensitive; re-measure with `.venv/bin/python -m ruff`). ✅ Phase 9–10.
4. **No `typing.List/Dict/Tuple/Optional` in `app/framework/`** (guard test green). ✅ Phase 9.
5. `.venv/bin/python -m pytest tests/ --timeout=30 -q` → **542 passed + all new characterization/regression tests green**, 0 failed. Every phase. ✅ Close-out: **655 passed / 16 skipped / 0 failed** (113 net-new tests vs the 542 baseline).
6. **Frozen API still importable** at `app.framework.framework_main_async` (`test_frozen_api_importable` green). ✅ Phase 7b.
7. GlobalState slice views additive: `state.X` legacy access unchanged; `state.musical.X` live-view works; high-risk attrs not relocated. ✅ Phase 8.
8. Cache divergence preserved: `test_pregeneration_does_not_route_through_cache_stem` + `test_foreground_loop_routes_through_cache_stem` both green. ✅ Phase 6.

---

## 5. Risk Register

| # | Risk (source) | Severity | Mitigated in phase | Verification of mitigation |
| --- | --- | --- | --- | --- |
| R1 | `process_actions` hidden `_age` mutation (brief-01 #1) — extraction could drop it, breaking P6/P11 | high | Phase 4 | gap-1 characterization test asserts `_age` mutation; comment documents the live-list contract |
| R2 | `state.lock` scope spans modules → holding lock across I/O reopens races (brief-01 #2) | high | Phase 7a | `_step_*` methods snapshot-in / I/O-out; grep `_step_call_conductor\|_step_submit_jobs` for `state.lock` acquisition inside = none |
| R3 | P11 non-atomic if split (brief-01 #3, verified L616–688) | critical | Phase 7a | `_step_commit_state` stays one `async with state.lock`; `test_step_commit_state_is_single_lock_block` asserts `record_loop_transition` called while `state.lock` is UNlocked |
| R4 | Foreground/background cache-stem divergence (brief-01 #4, verified L527≠L1155) | critical | Phase 6 | two divergence regression tests (one asserts NOT called, one asserts called) |
| R5 | MixerPort dual-lock deadlock (brief-01 #5) | critical | Phase 11 (deferred → guarded via U1) | Mixer kept concrete; Protocol typing-only; P10/P13 audio behavior unchanged (the U3 migration rewired the call sites to `prime_loop`/`loop_position_seconds` byte-for-byte, zero audible change). **U1 (branch `refactor/mixer-pin-lock-atomicity`) PINNS the atomicity invariant by a structural test** — P10 single-lock batch + dual-lock ordering enforced in `tests/test_loop_lock_safety.py`; lock-held + crossfade round-trip in `tests/test_mixer_controller_characterization.py`. A port that splits the atomic batch or reverses lock nesting fails fast; status moved deferred → guarded. Phase 11 extraction: U1–U3 LANDED, U4 (ctor-injection) pending (NOT COMPLETE). |
| R6 | String-patch silent no-op when moving `decode_aac`/`create_garage_client_from_env` (brief-02 §D ⚠️) | high | Phase 2 | patch strings migrated + guard test asserts patch applies |
| R7 | `_flush_lock` identity breaks if split (brief-02 §D/#6) | medium | Phase 3 | lock-identity assertion (`is`) in red test |
| R8 | pydantic regression from `Optional`→`X\|None` in `routes/schemas.py` (Phase 9) | medium | Phase 9 | run `test_api.py` + model-instantiation smoke; revert+flag if regression |
| R9 | High-risk GlobalState attrs relocated prematurely (brief-03 §C) | high | Phase 8 | slicing additive-only; high-risk attrs untouched; existing concurrency tests #33–39 green |
| R10 | `_run_loop` watchdog/exit paths regressed (brief-04 gaps) | high | Phase 0 + 7a | characterization tests gaps 4,5 + new shutdown/transition tests |
| R11 | PreGenerator gets a SEPARATE `stem_cache` → re-submits every job + duplicate stems (round-1 red-team) | high | Phase 6 | `PreGenerator(stem_cache=self.stem_cache, …)` shared; `test_pregen_skips_job_when_foreground_already_cached` |
| R12 | `ruff UP --fix` orphans `from typing import …` → +24 F401 detonate at Phase 10 (round-1, verified empirically) | high | Phase 9 | fix command includes `F401`; gate `--select UP006,UP007,UP045,F401` → 0 |
| R13 | "≤20-line steps" goal unachievable (measured P3=50,P10=47,P11=85) → hollow DoD (round-1) | medium | Phase 7a | DoD §2 revised to ≤50 / `_step_commit_state` ≤90; P11 action-log sub-decomposed |
| R14 | Ports non-injectable (internal-only construction) → E5/CLAUDE.md DI goal silently unmet (round-1) | medium | Phase 7b | keyword-default `__init__(session_id, *, conductor=None, …)`; `test_loop_accepts_injected_fake_ports` |

---

### Summary

- **Phases:** 11 core (0–10) + 1 optional (11, default-deferred). Each is one commit, each keeps the suite green.
- **Estimated touch-file count:** ~18 — 7 new framework modules (`ports`, `domain_audio`, `audio_fetch`, `audit_recording`, `conductor_interaction`, `job_queue`, `pregeneration`, `loop_orchestrator`, `state_slices` ≈ 9) + `framework_main_async.py` (→shim) + `framework_state.py` + `framework_conductor_async.py`/`framework_mixer.py`/`framework_icecast.py` (typing) + `pyproject.toml` + ~4 new test files + ~2 migrated test files + repo-wide ruff `--fix`.
- **3 highest-risk phases:**
  1. **Phase 7a** — `_run_loop` decomposition (must preserve P11 single-lock atomicity, risk #3, and lock-scope discipline, risk #2).
  2. **Phase 6** — pregeneration extraction (must preserve the load-bearing cache-stem divergence exactly, risk #4).
  3. **Phase 7b** — orchestrator relocation + shim (frozen-API re-export + all instance-level patches intact); plus **Phase 9** (pydantic typing regression, risk #8) as a close runner-up.

---

## 6. Round 1 Adversarial Review — Reconciliation

Round-1 fanout: `reviewer` (red-team sequencing) + `claim-verifier` (10/10 premises Verified, minor line-anchor drift) + `reviewer` (verification-strategy audit). Verdicts: `PLAN_NEEDS_REVISION` / `VERIFICATION_HAS_GAPS`. **No fatal findings; all BLOCKERs folded into the phase specs above.** Amendments applied to this plan:

| # | Finding (source) | Severity | Phase | Amendment applied |
| --- | --- | --- | --- | --- |
| A1 | Safety net missing HIGH gaps 6–8 (pregen-vs-fresh divergence = the exact code 7a decomposes) | BLOCKER | 0 | gaps 6,7,8 red tests added to Phase 0, before 7a |
| A2 | PreGenerator `stem_cache` ownership unspecified → silent cache-coordination break | BLOCKER | 6 | shared `self.stem_cache`; +skip-when-cached, cache-key-pure-fn, non-silence tests (R11) |
| A3 | "≤20-line steps" unachievable (P3=50,P10=47,P11=85) → hollow E2 | BLOCKER | 7a / DoD §2 | budget revised to ≤50 / commit_state ≤90; P11 action-log sub-decomposed (R13) |
| A4 | P11 atomicity "verified" by comment, not test | BLOCKER | 7a | `test_step_commit_state_is_single_lock_block` asserts record_loop_transition runs UNlocked |
| A5 | ruff `UP --fix` spawns +24 F401 (verified) → detonates at Phase 10; DoD §3 hardcoded stale 735 | BLOCKER | 9/10/DoD §3 | fix includes F401; dynamic re-baseline to `refactor/ruff_baseline.txt` (R12) |
| A6 | Ports non-injectable → E5/CLAUDE.md DI silently unmet | CONCERN | 7b | keyword-default injectable `__init__`; +construct/inject tests (R14) |
| A7 | `test_frozen_api_importable` import-only; misses signature change + string-patch no-op | CONCERN | 0 | frozen-API test now instantiates + asserts callables resolve |
| A8 | Loop harness under-specified → hangs/flakes | CONCERN | 0 | `_FakeMixer` records calls + stop-after-N; `_reset_audit_state` covers ~15 attrs |
| A9 | gap-1 test must assert in-place mutation on INPUT list (deepcopy refactor slips green) | CONCERN | 4 | gap-1 uses stems w/ `_original_details`; asserts input mutation |
| A10 | "belt-and-suspenders" dual-binding framed as risk mitigation (false confidence) | CONCERN | 2 | reframed as import-compatibility only; guard test is the real protection |
| A11 | `_step_read_snapshot` not in lock-grep gate (reads models_config.json outside lock) | CONCERN | 7a | `test_no_io_inside_state_lock` covers ALL steps incl. read_snapshot |
| A12 | pydantic Optional→X\|None verified by manual smoke only | CONCERN | 9 | committed `test_schemas_accept_optional_as_union` |
| A13 | `python -m …framework_main_async` breaks (lost `__main__`) | CONCERN | 7b | `__main__` stub on shim or document moved entry |
| A14 | Line-ref drift: P11 L616–688 (not 608–698); cache write L1096 (not 1155) | minor | 6/7a | corrected throughout |

**Round-1 reviewer artifacts:** `refactor/review/round1/redteam_sequencing.md`, `claim_verification.md`, `redteam_verification.md`. Plan now targets a **single round-2 confirm pass** before implementation (re-review only the amended phases 0, 6, 7a, 7b, 9).
