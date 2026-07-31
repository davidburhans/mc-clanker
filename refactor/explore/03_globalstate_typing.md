# 03 — GlobalState Decomposition (E3) + Typing/Size Inventory (E6)

Exploration artifact for the E1–E6 refactor. Source: `app/framework/framework_state.py`
(GlobalState, module singleton `state`), read in full (488 LOC).

Scope note: the project's "no Dict/List/Any" rule is the convention encoded in
`adversarial_review/00_SYNTHESIS.md` finding **E6** ("51× List, 35× Dict (banned),
94× Optional, 31× Any"). The remediation target is builtin generics (`list`,
`dict`, `tuple`) + PEP-604 (`X | None`) on the py310 runtime.

---

## (A) GlobalState attribute → slice table

**65 `self.*` attributes** are assigned in `__init__` (`framework_state.py:72-192`)
plus the read-only `last_generated_stems` property (`:196`, returns `_stem_cache`).

**Lock legend:** `async` = `state.lock` (asyncio.Lock, event-loop thread);
`sync` = `state.sync_lock` (threading.Lock, Mixer/broadcast thread); `both` =
touched under each in different sites; `none` = unlocked access confirmed.

| Slice | Attribute | Init line | Primary readers / writers | Lock |
| --- | --- | --- | --- | --- |
| **FrameworkInternals** | `lock` | 78 | held by framework_main_async + all routes | async |
| | `sync_lock` | 79 | framework_state methods, shows, playback, config | sync |
| | `is_running` | 110 | app_ui(R/W unlocked `:107,487`), main_async(R unlocked `:269`), config(R lock), trigger_shutdown(W sync) | **none/both** |
| | `shutdown_event` | 172 | main_async(R unlocked `:273,277`), trigger_shutdown(set) | none |
| | `framework_task` | 192 | app_ui(W unlocked `:93`) | none |
| | `active_subprocesses` | 171 | app_ui via register/unregister (sync) | sync |
| **MusicalParams** | `current_bpm` | 82 | main_async(R/W lock), ws/config(R lock), shows(R lock) | async |
| | `current_key` | 83 | same as current_bpm | async |
| | `current_set_name` | 88 | main_async(W lock), ws/config(R lock) | async |
| | `previous_stems` | 84 | main_async(R/W lock), stems(R lock), ws/config(R lock) | async |
| | `active_stems` | 85 | main_async(R/W lock), ws/stems/config(R lock) | async |
| | `next_stems` | 86 | main_async(R/W lock), stems(R/W lock), ws/config(R lock) | async |
| | `stem_history` | 87 | main_async(R/W lock), config/ws(R lock), ConductorPromptBuilder.build(R sync) | both |
| | `llm_reasoning` | 95 | main_async(W lock), ws/config(R lock) | async |
| **GenerationControl** | `is_generating` | 105 | main_async(R unlocked `:273`+lock), mixer(R via snapshot sync), ws/config(R lock), config(W lock), trigger_shutdown(W sync) | **both/none** |
| | `is_show_started` | 106 | shows(W lock), config(R/W lock), ws(R lock) | async |
| | `user_override` | 96 | main_async(R lock), config(R/W lock), ws/shows(R lock) | async |
| | `target_bpm_override` | 97 | main_async(R/W lock), config(R/W lock), ws(R lock) | async |
| | `target_key_override` | 98 | same as target_bpm_override | async |
| | `should_reset` | 99 | main_async(R/W lock), config(W lock) | async |
| | `generation_cfg_scale` | 133 | config(R/W lock) | async |
| | `generation_steps` | 134 | config(R/W lock) | async |
| **LLMConfig** | `llm_base_url` | 101 | main_async(R lock), config(R/W lock), ConductorPromptBuilder.build(R sync) | both |
| | `llm_api_key` | 102 | same as llm_base_url | both |
| | `llm_model` | 103 | same as llm_base_url | both |
| **MixerState** | `stem_volumes` | 113 | mixer(R via snapshot sync), stems(R/W lock), config/ws(R lock) | **both** |
| | `muted_stems` | 114 | same as stem_volumes | **both** |
| | `soloed_stems` | 115 | same as stem_volumes | **both** |
| **LoopCoordination** | `loop_count` | 116 | main_async(W lock), ws/config(R lock) | async |
| | `last_actions` | 117 | main_async(W lock), ws/config(R lock) | async |
| | `currently_playing_loop_index` | 120 | record_loop_transition(W sync), ws/config(R lock) | both |
| | `currently_playing_stems` | 121 | record_loop_transition(W sync), ws/config(R lock) | both |
| | `currently_playing_set_name` | 122 | same | both |
| | `currently_playing_reasoning` | 123 | same | both |
| | `loop_history` | 124 | record_loop_transition(W sync), ws/config(R lock) | both |
| | `current_loop_end_sample` | 132 | **DEAD on state** — only init+reset; mixer owns the real boundary | — |
| **Recording** (export+show) | `is_recording` | 145 | shows(R/W sync), broadcast_audio/trigger(R/W sync) | sync |
| | `recording_format` | 146 | shows(R/W sync) | sync |
| | `recording_file_path` | 147 | shows(R/W sync) | sync |
| | `recording_start_time` | 148 | shows(R/W sync) | sync |
| | `recording_file_handle` | 151 | shows(R/W sync), broadcast/trigger(R/W sync) | sync |
| | `_last_recording_error_handle` | 164 | framework_state internal (sync) | sync |
| | `current_show_id` | 154 | shows(W sync), main_async audit(R lock), flush(R lock) | both |
| | `current_show_start_time` | 155 | shows(W sync), main_async_relative_show_ms(**R unlocked `:916`**) | **sync/none** |
| | `is_show_recording` | 156 | shows(W sync), ws(R lock), broadcast/trigger(R/W sync) | both |
| | `llm_interaction_buffer` | 157 | shows(clear lock), main_async(append lock), flush(lock) | async |
| | `action_buffer` | 158 | same as llm_interaction_buffer | async |
| | `current_show_audio_file` | 159 | shows(R/W sync), broadcast/trigger(R/W sync) | sync |
| **Playback** | `currently_playing_show_id` | 167 | shows(R/W lock), playback(R/W sync), ws(R lock) | both |
| | `is_playback_active` | 168 | shows(R/W lock), playback(R/W sync), ws(R lock) | both |
| **AudioStreaming** | `audio_clients` | 109 | app_ui via add/remove_audio_client (sync), broadcast(snapshot sync) | sync |
| **StemCache** | `_stem_cache` | 142 | cache_stem(sync via method), main_async(call cache_stem under lock), stems(last_generated_stems.get under lock) | both |
| | `last_generated_stems` (property) | :196 | stems(R lock) | async |
| **Instruments** | `instruments_file` | 90 | framework_state internal | sync |
| | `custom_instruments` | 91 | add/get_custom_instrument(sync), config(via methods) | sync |
| | `categorized_instruments` | 92 | add_custom_instrument(sync) | sync |
| | `available_instruments` | 93 | main_async(R lock), config(R/W lock), ws(R lock), ConductorPromptBuilder.build(R sync) | both |
| **ModelMgmt** | `model_states` | 179 | **DEAD on state** — GeneratorRegistry has its own (`framework_generator.py:151`) | — |
| | `model_errors` | 180 | **DEAD on state** — GeneratorRegistry `:152` | — |
| | `download_progress` | 181 | **DEAD on state** — never read anywhere | — |
| | `generator` | 182 | main_async(getattr R unlocked), ConductorPromptBuilder.build(getattr R sync) | none/both |
| **SessionConfig** | `dj_password` | 175 | auth(getattr R `:164`) | none |
| | `audience_password` | 176 | auth(getattr R `:165`), config(W lock) | async/none |
| | `audience_message` | 188 | config(R/W lock), ws(R lock) | async |
| | `audience_message_ts` | 189 | config(R/W lock), ws(R lock) | async |
| | `icecast_enabled` | 185 | config(R/W lock) | async |

**Consumers that `import state`** (11 modules): `app_ui.py`, `playback.py`,
`auth.py:142` (late import), `routes/{config,ws,stems,jobs,shows,models}.py`
(jobs/models import but never dereference attrs), `framework/framework_mixer.py`,
`framework/framework_main_async.py`.

---

## (B) Proposed slice interfaces

**Pass-1 constraint:** every consumer calls `state.current_bpm`,
`state.active_stems`, `state.stem_volumes`, etc. A repo-wide rename is
high-risk and out of scope for pass 1. The decomposition introduces **typed
slice dataclasses that proxy over the same underlying `state.__dict__`**, exposed
as read views via properties. Storage does NOT move; the 65 attributes stay on
the singleton. New code/tests may use `state.musical.current_bpm`; old code keeps
`state.current_bpm` working unchanged.

```python
# refactor/framework_state_slices.py  (NEW — pass 1, zero-rename)
from __future__ import annotations
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.framework.framework_state import GlobalState


def _view(host: "GlobalState", name: str):
    """Descriptor returning the live attribute off the singleton (no copy)."""
    return lambda self: getattr(self._host, name)


@dataclass
class _Slice:
    _host: "GlobalState"


class MusicalParams(_Slice):
    """current_bpm, current_key, current_set_name, previous/active/next_stems,
    stem_history, llm_reasoning."""

class GenerationControl(_Slice):
    """is_generating, is_show_started, user_override, target_*_override,
    should_reset, generation_cfg_scale, generation_steps."""

class LLMConfig(_Slice):
    """llm_base_url, llm_api_key, llm_model."""

class MixerState(_Slice):
    """stem_volumes, muted_stems, soloed_stems."""

class LoopCoordination(_Slice):
    """loop_count, last_actions, currently_playing_*, loop_history."""

class RecordingState(_Slice):
    """is_recording, recording_*, current_show_*, *_buffer,
    current_show_audio_file (sync_lock-protected)."""

class PlaybackState(_Slice):
    """currently_playing_show_id, is_playback_active."""

class StemCache(_Slice):
    """last_generated_stems (OrderedDict LRU) + cache_stem()."""

class InstrumentCatalog(_Slice):
    """available_instruments, categorized/custom instruments."""

class SessionConfig(_Slice):
    """dj/audience passwords, audience_message, icecast_enabled."""
```

**GlobalState additions (non-breaking):**

```python
# framework_state.py — append inside GlobalState (storage unchanged)
@property
def musical(self) -> "MusicalParams":
    return MusicalParams(self)
@property
def mixer(self) -> "MixerState":      # NOTE: name-clash risk with framework Mixer
    return MixerState(self)
@property
def recording(self) -> "RecordingState":
    return RecordingState(self)
# ... etc. Each slice only forwards reads; writes still go through state.X = ...
```

**Pass-1 risk profile:** additive properties only — no `__getattr__`/`__setattr__`
override (which would break the ~50 `state.X = v` write sites and pickle). The
slices are *read views* for type-checking + testability. Storage migration to
nested dataclasses (with `__getattr__` forwarding) is **pass 2**, after the
synthesis E3 concurrency fixes (A1/A2/A4) land, because those fixes already
restructure lock ownership around these exact fields.

**Naming caveat:** `state.mixer` collides conceptually with `framework_mixer.Mixer`.
Recommend `state.levels` or `state.stem_levels` for the per-stem slice to avoid
confusion; flagged for supervisor decision.

---

## (C) High-risk attributes (do not move in pass 1)

Ranked by blast radius (consumer count × cross-thread exposure × bug history):

| Rank | Attribute | Why high-risk |
| --- | --- | --- |
| 1 | `is_generating` | Read **unlocked** in the framework wait-loop (`main_async:273`), read by **mixer thread** via `snapshot_mixer_state` (sync_lock), written by routes (async lock) AND `trigger_shutdown` (sync_lock). The A2/A4 nexus. Any move must preserve both lock surfaces. |
| 2 | `is_running` | Written **unlocked** at `app_ui.py:107`; read unlocked in streaming/subprocess loops (`app_ui:487,519`); written under sync_lock in `trigger_shutdown`. Tearing risk if relocated. |
| 3 | `stem_volumes`/`muted_stems`/`soloed_stems` | The **A2** bug: mixer thread reads via snapshot (sync_lock), route handlers mutate under asyncio lock — two different locks. `snapshot_mixer_state` (`:355-368`) is the only correct reader. Moving these without first unifying the lock re-opens A2. |
| 4 | `recording_file_handle` / `current_show_audio_file` | **A1/B8/B9** core: written under sync_lock by shows routes + broadcast, closed by `trigger_shutdown`. Half-closed-handle races. |
| 5 | `active_stems` / `next_stems` / `previous_stems` | The stem triple. 8+ consumers, deepcopy snapshots required (`ws.py:117`). `next_stems` is the **A10** TOCTOU site (stems.py appends while loop clears). |
| 6 | `current_show_start_time` | Written under sync_lock (`shows.py:258`) but read **unlocked** at `main_async:916` (`_relative_show_ms`). Silent tearing in audit timestamps. |
| 7 | `llm_base_url`/`api_key`/`model` | Read by framework loop (async lock) AND `ConductorPromptBuilder.build` (sync_lock `:386-396`). Two-lock read path. |
| 8 | `current_show_id` | Written sync_lock (shows), read async lock (framework audit `main_async`, flush `:71`). Mixed-lock. |

**Safe-to-move first (low-risk, few consumers):** `generation_cfg_scale`,
`generation_steps`, `audience_message(_ts)`, `icecast_enabled`, `dj_password`,
`recording_format`, the 3 **dead** ModelMgmt attrs (can be deleted).

---

## (D) Typing / function-size / file-size inventory

### D.1 app/framework/ — banned-typing counts by file

Counts are annotation-level occurrences of `List`/`Dict`/`Tuple`/`Optional`/`Any`
from `typing`. 0 = already compliant.

| File | LOC | `from typing import` | List | Dict | Tuple | Optional | Any | **total** |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `framework_main_async.py` | 1192 | `Optional,List,Dict,Any` | ~5 | ~9 | 0 | 5 | ~4 | **~23** |
| `framework_conductor_async.py` | 436 | `Optional,Dict,Any,List` | ~9 | ~15 | 0 | 2 | ~6 | **~32** |
| `framework_icecast.py` | 355 | `Optional` | 0 | 0 | 0 | 5 | 0 | **5** |
| `framework_state.py` | 488 | *(none — uses builtin `list`, `OrderedDict`)* | 0 | 0 | 0 | 0 | 0 | **0** |
| `framework_mixer.py` | 357 | `Optional` *(unused import)* | 0 | 0 | 0 | 0 | 0 | **0** |
| `framework_generator.py` | 407 | *(none)* | 0 | 0 | 0 | 0 | 0 | **0** |

### D.2 app/ — repo-wide typing hotspots (outside framework)

| File | LOC | dominant violations |
| --- | ---: | --- |
| `routes/schemas.py` | 167 | ~40× `Optional` (pydantic models) |
| `lib/recording_postprocess.py` | 476 | ~30× `List[Dict]`/`Dict`/`Optional`/`Any` |
| `lib/recording_metadata.py` | 493 | ~25× `List[Dict]`/`Dict`/`Optional`/`Any` |
| `lib/harmonic.py` | 86 | 5× `List`/`Dict`/`Any` |
| `lib/constants.py` | 206 | 12× `List` |
| `routes/reasoning_logs.py` | 311 | ~15× `Optional` |
| `worker.py` | 448 | 5× `Optional` |
| `auth.py` | 222 | 6× `Optional` |
| `job_waiter.py` | 325 | 6× `Optional` |
| `cleanup.py` | 241 | `Optional`+`List` |
| `routes/{ws,stems,jobs}.py`, `gpu_monitor.py` | — | 1–6× each |

tests/ typing is near-clean: only `tests/test_gpu_monitor.py` (`: Any`) and
`tests/slop_harness/test_quality_validator.py` (3× `: Any`).

### D.3 Functions > 20 lines — app/framework/ hit-list

| File | Function | Approx span | Lines |
| --- | --- | --- | ---: |
| `framework_main_async.py` | `AsyncFrameworkLoop._run_loop` | `:252–~700` | **~448** (E2 god method, ≥5 responsibilities) |
| `framework_main_async.py` | `_pre_generate_next_loop` | `:979–1098` | ~120 |
| `framework_main_async.py` | `process_actions` | `:118–180` | ~63 |
| `framework_main_async.py` | `flush_recording_buffers` | `:63–101` | ~39 |
| `framework_main_async.py` | `_build_prompt` | `:772–810` | ~39 |
| `framework_main_async.py` | `_submit_job` | `:812–848` | ~37 |
| `framework_main_async.py` | `_fetch_audio` / `_append_loop_audit` | `:851`/`:880` | ~30 ea. |
| `framework_state.py` | `GlobalState.__init__` | `:72–192` | ~120 (E3 god **init**) |
| `framework_state.py` | `trigger_shutdown` / `_close_recording_handles_locked` | `:424–482` | ~33 ea. |
| `framework_state.py` | `reset` / `broadcast_audio` / `add_custom_instrument` / `snapshot_mixer_state` / `_load_instruments` | various | ~22–27 ea. |
| `framework_mixer.py` | `Mixer._callback` | `:168–321` | ~150 (audio path) |
| `framework_mixer.py` | `_extend_tracks_for_loop` / `_extend_tracks_at_position` / `__init__` | various | ~22–28 ea. |
| `framework_conductor_async.py` | `ConductorPromptBuilder.build_prompt` | `:266–360` | ~95 |
| `framework_conductor_async.py` | `get_next_state_async` / `ConductorPromptBuilder.build` / `__init__` | various | ~45–60 ea. |

Synthesis E4 repo-wide baseline: **111 / 307 functions exceed 20 lines (36%)**.

### D.4 Files > 500 LOC

| File | LOC |
| --- | ---: |
| `app/framework/framework_main_async.py` | **1192** (E1 god file) |
| `app/app_ui.py` | 573 |
| `app/routes/shows.py` | 533 |
| `tests/test_api.py` | 705 |
| `tests/test_state.py` | 704 |
| `tests/test_async_framework.py` | 689 |
| `tests/test_mixer.py` | 597 |

(Next tier under 500: `lib/recording_metadata.py` 493, `lib/recording_postprocess.py` 476, `worker.py` 448, `framework_conductor_async.py` 436.)

### D.5 Prioritized refactor hit-list (E1–E6 touch order)

1. **`framework_main_async.py`** (1192 LOC) — E1 god file, E2 `_run_loop` (~448
   lines), ~23 typing violations, 8 functions >20 lines. Highest payoff.
2. **`framework_state.py`** (488 LOC) — E3 god object; slicing target (this
   doc). 0 typing violations (clean), but `__init__` is ~120 lines.
3. **`framework_conductor_async.py`** (436 LOC) — ~32 typing violations (worst
   density in framework); `build_prompt` ~95 lines.
4. **`framework_mixer.py`** (357 LOC) — `_callback` ~150 lines; A2 lock owner.
   Remove unused `Optional` import. 0 typing violations otherwise.
5. **`garage_client.py`** (236 LOC) — already typing-clean; only E5 (port) +
   B3 (timeouts, already fixed). Low E6 effort.
6. **`job_waiter.py`** (325 LOC) — 6× `Optional` → `X | None`; E12 dedup of the
   3 `wait_for_job_completion` variants lives here.

---

## (E) Ruff baseline

**`ruff check app/ tests/` could NOT be executed** by the read-only exploration
agent (no shell tool). The baseline error/warning count therefore could not be
captured at runtime.

Static findings relevant to a future ruff run:

- `pyproject.toml` `[tool.ruff]` sets only `line-length = 120` and
  `target-version = "py310"` — there is **no `[tool.ruff.lint]` section**, so the
  default rule set is `E` (pycodestyle errors) + `F` (pyflakes). `UP` (pyupgrade)
  and `I` (import sorting) are **not enabled**, which is why the `typing.List`/
  `Dict`/`Optional` violations (E6) are **not currently flagged by ruff** — they
  are convention violations, not lint errors under the current config.
- Confirmed pyflakes (`F`) findings that default ruff *would* surface:
  `framework_mixer.py:13` — `from typing import Optional` is **imported but
  unused** (`F401`).
- To make E6 enforceable, the refactor should add to `pyproject.toml`:
  `UP006` (use `list`/`dict` not `typing.List`/`Dict`), `UP045` (`X | None` not
  `Optional`), and `UP007` — then `ruff check --select UP` produces the real
  baseline.

**Recommended command to run once a shell is available:**

```bash
ruff check app/ tests/ 2>/dev/null | tail -1            # current baseline
ruff check --select UP,F app/ tests/ 2>/dev/null | tail -1  # E6 baseline
```
