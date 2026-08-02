# 01 — Optional-Narrowing Inventory & Verify-Safety

Agent: `codebase-analyzer` (fresh context). Source: `app/framework/loop_orchestrator.py`.
pyright (basic mode) flags ~30 "possibly None" diagnostics on two Optional fields.

## (A) Field inventory

- `self.mixer: Mixer | None` — declared L143; set non-None in `start()` L187 (via `run_in_executor`) BEFORE `_run_loop` is spawned (L190); **never** set back to None anywhere (grep-confirmed: only writer is L187). `stop()`/`_finish_loop()`/`trigger_shutdown` only call `.stop()` under `if self.mixer:` — no reassignment.
- `self._pregen_results: dict | None` — declared L153; writers: `_step_post_commit` (L809 `=None`, L817 `={...}`), `pregeneration.py` (L128 `={...}` success, L147 `=None` error).

Other Optional fields (`loop_task`, `_pregen_task`, `_audio`, `_garage`, `_audio_adapter`) are **not** flagged (already narrowed at use).

## (B) Runtime-invariant proof (the safety crux)

**B.1 `self.mixer` non-None throughout `_run_loop`:** `start()` assigns mixer (L187) before `create_task(_run_loop)` (L190) — `_run_loop` can't start before the assignment. Monotonic thereafter (no `= None` anywhere). `stop()` cancels + **awaits** `loop_task` before proceeding; `CancelledError` is caught by the B1 except → `_finish_loop` → re-raise; the loop never resumes mid-`_step_*`. Even if it did, the field stays a (stopped) Mixer, never None. ⇒ **provably non-None at every `_step_*` use.**

**B.2 `self._pregen_results` non-None inside every `if pregen_ready:` block:** `pregen_ready` (L350-354) is True only if `self._pregen_results is not None` (short-circuit). No concurrent writer nulls it mid-branch — asyncio single-thread model (context switches only at `await`); the iteration-N pregen task is DONE (it set `_pregen_done` after writing the result) before iteration N+1's P2 reads it; the error path sets `=None` + `_pregen_done.set()` together, so the next P2 sees `is not None`→False→pregen_ready False→dict-reading branches skipped. ⇒ **provably non-None in every `if pregen_ready:` block** (P2 L368, P10 L655, P11 L708 + L720).

**Fragility to preserve:** `run_pregeneration` MUST write `_pregen_results` BEFORE `_pregen_done.set()` (true on both success L128→129 and error L147→148). Inverting this + relaxing the `is not None` gate would silently skip pre-gen.

## (C) Use-site table — 27 unguarded lines, ALL SAFE-TO-NARROW

- `self.mixer.*` (12 lines / 13 accesses): `_step_read_state` L407 `.clear()`; `_step_commit_to_mixer` L666-676 (`.lock`, `.current_sample`, `._add_track_internal`, `._ensure_stereo`, `.current_loop_end_sample=`, `._current_loop_duration=`, `.set_next_loop`); `_step_await_pregen` L838-859 (`.pop_transition_event`, `.lock`, `.current_loop_end_sample`, `.current_sample`, `.sample_rate`).
- `self._pregen_results.*` (15 lines): `_step_pregen_decision` L370-377 (5× `.get`, 2× `[...]`); `_step_commit_to_mixer` L656-657 (2× `[...]`); `_step_commit_state` L709 `[...]`, L721-728 (4× `.get`, 1× `.get` in for).

## (D) Recommended strategy (least-noise, honest)

Keep both types `Optional` (the object legitimately exists pre-`start()`; `__init__` assigns None). Add:

- `assert self.mixer is not None` at the top of the **3** `_step_*` methods that use it unguarded: `_step_read_state`, `_step_commit_to_mixer`, `_step_await_pregen`. (A `_run_loop`-top assert does NOT narrow inside callees — pyright narrows per-function.)
- `assert self._pregen_results is not None` as the first statement of each of the **4** `if pregen_ready:` blocks: `_step_pregen_decision` (~368), `_step_commit_to_mixer` (~655), `_step_commit_state` (~708, ~720).
Rejected: changing the field type to non-Optional (rejected — None is honest pre-start); `_mixer`+property (noise + tests patch `loop.mixer`).

## (E) GENUINELY-POSSIBLY-NONE: **EMPTY (0).**

Every unguarded use is provably non-None (B.1/B.2). The only sites where None can occur are already runtime-guarded (`188` post-assign; `209`/`217` `if self.mixer:`; `364`/`635` `… if self.mixer else None`; `352`/`353`/`360` the pregen gate). No `stop()`/race path can produce a None deref.

## Summary

2 fields, 27 use lines (~28-30 diagnostics), all SAFE-TO-NARROW, 0 genuinely-None. Strategy: keep types Optional; 3 `assert self.mixer` + 4 `assert self._pregen_results`. Preserve the pregen write-before-done ordering.
