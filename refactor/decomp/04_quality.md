# 04 — REFACTOR-QUALITY Review of `_run_loop` Decomposition (commit fa0ca9b)

**Reviewer:** review subagent (fresh context). **Scope:** `app/framework/loop_orchestrator.py`
`_run_loop` (L195-292) + 14 `_step_*` methods (L294-867) vs the map/plan/round-1 review.

**Method:** firsthand reads of current HEAD + `git show fa0ca9b` diff; AST verification of
the lock-safety and outside-lock invariants; LOC measured via AST (`span` = def→end incl
docstring; `code` = nonblank non-comment). Baseline re-run: **573 passed, 58 skipped, 9 xfailed,
16 xpassed** (matches commit; 569 baseline + 4 D0 tests). `ruff check` exit 0.

---

## Findings (AREA | SEVERITY | location | what | fix)

| AREA | SEV | location | what | fix |
| --- | --- | --- | --- | --- |
| DRIVER_LOC | CONCERN | `_run_loop` L195-292 | Driver body is **77 nonblank lines** (excl the 12-line docstring); gate-2 target is `<=~70`. Inflation is *entirely* vertical tuple-unpacking: `_step_read_state` returns a 10-tuple → 12-line unpack (L232-242), `_step_pregen_decision` a 6-tuple → 8-line unpack (L219-226). The logical structure is clean (4 statements: wait→pregen→read→[fresh P4-P9]→commit→post→await) but the *letter* of the ~70 gate is missed by ~7 lines. The commit message's "62-LOC" claim does not match the measured 77 nonblank / 84 incl-doc-text. | Collapse the wide unpacks: return a small `NamedTuple`/frozen dataclass (`_StateSnapshot`, `_PregenDecision`) so the driver unpacks in 1 line. This would bring the driver to ~55 nonblank and turn the gate green — without giving up the explicit-params clarity on the call side. |
| DEAD_RETURNS | SUGGESTION | `_run_loop` L224 (`_next_stems`), L272 (`_current_loop_end_sample`) | Two tuple elements are unpacked-with-underscore and **never read**. `_next_stems` (from `_step_pregen_decision`) is dead because the pregen path reads `self._pregen_results["next_stems"]` directly inside `_step_commit_state`. `current_loop_end_sample` (3rd element of `_step_commit_to_mixer`) is dead — `_step_commit_to_mixer` reads `self.mixer.current_loop_end_sample` under `mixer.lock` (L669, L678) *only* to return a value nobody uses. Round-1 SUGGESTION-1 flagged this var as 0-reads; the worker dropped the P3 reset write (good) but kept these two dead threads. | Drop `_next_stems` → `_step_pregen_decision` returns a 5-tuple; drop the 3rd return from `_step_commit_to_mixer` (and the two lock-guarded reads at L669/L678 that exist only to feed it). |
| SIGNATURE_READABILITY | CONCERN | `_step_read_state` L390 (10-tuple return), `_step_pregen_decision` L328 (6-tuple), `_step_call_conductor` L443 (9 positional params) | Positional tuple returns of 6–10 elements are brittle: a silent swap of two same-typed values (e.g. the several `list`s, or the two `int`s `loop_duration_samples`/`current_bpm`) is undetectable. This is the explicit-params-vs-`StepCtx` trade-off (Q3): per-method contracts are transparent (params=spanning reads, returns=spanning writes), but the driver pays for it in fragile positional unpacks. | Same fix as DRIVER_LOC: NamedTuple/dataclass returns for the wide tuples keep the call-side clarity and remove the positional-swap hazard. (Param side is acceptable — `_step_call_conductor`'s 9 params mirror `get_next_state_async`'s signature 1:1.) |
| REDUNDANT_GUARD | SUGGESTION | `_step_call_conductor` L462 `if pregen_ready: return None` | The driver only calls `_step_call_conductor` inside `if not pregen_ready:` (L246), so the internal guard is unreachable dead defense. Harmless but it implies a second, different contract than the driver's guard. | Pick one guard, not both: either drop the internal `if pregen_ready: return None` (the A1 driver guard suffices) or drop the driver guard and rely on the internal one. |
| PREGEN_DECISION_SIZE | CONCERN (borderline) | `_step_pregen_decision` L328-388 (span 61 / code 55) | Marginally over the 50-LOC step gate. It **is** a single cohesive responsibility (decide pregen-vs-fresh + assemble the pregen outputs), so splitting for LOC alone is not warranted; the 55 code lines are 3 debug prints + conductor_response dict assembly + 4 returns. | Acceptable as-is. If <50 is desired: extract the pregen-branch `_assemble_pregen_response()` helper — but that is LOC-driven, not responsibility-driven. |
| READ_STATE_SIZE | NOTE | `_step_read_state` L390-441 (span 52 / code 44) | Marginally over 50 by span only; by executable code it is 44 (under gate). Single cohesive responsibility (reset + override apply/clear + snapshot under one lock). No split needed. | None. |
| DUPLICATED_ACTION_LOG | SUGGESTION | `_step_parse_actions` L480-504 vs `_step_commit_state` pregen branch L730-749 | Two near-identical retain/add/remove → "Retained X / Added Y / Removed Z" log-building loops. **Pre-existing** duplication (both existed in the original god-method), faithfully preserved — not a regression. | Extract a shared `_format_action_log(actions, stems)` helper. Pre-existing debt; safe to defer. |

### Verified-safe deviations (the 3 the worker flagged) — no behavior change

| Deviation | location | verdict |
| --- | --- | --- |
| (a) Dropped P2 pregen-branch `active_stems = list(state.active_stems)` read | `_step_pregen_decision` (docstring L338 documents it) | **SAFE.** P3 (`_step_read_state`) reads the identical `state.active_stems` with no mutation between them (round-1 "active_stems shadowing" verified), and P3 runs **unconditionally** in the driver (L243), so the value is always re-captured. P2's read was dead. |
| (b) Dropped P3 `current_loop_end_sample = 0` reset write | `_step_read_state` reset block | **SAFE.** The local had 0 reads (AST-verified dead in round-1 SUGGESTION-1). The two surviving P10 writes + driver thread are *also* dead — see DEAD_RETURNS. |
| (c) Renamed `CommitResult` → `_CommitResult` | L91 | **SAFE / improvement.** Leading underscore marks the internal threading type module-private. Naming only. |

### Invariants re-verified (AST, file-wide)

- **Lock-safety:** AST walk confirms **no `await` / `async-for` / nested `async-with` inside any
  `state.lock` block** across all 14 `_step_*` methods. `test_no_io_inside_state_lock_in_orchestrator` passes.
- **record_loop_transition outside lock:** both call sites (L795 `_step_post_commit`,
  L847 `_step_await_pregen`) snapshot under `state.lock` then call **outside** it. Verified by AST
  - `test_record_loop_transition_runs_outside_state_lock` (passing).
- **B1 watchdog:** every `_step_*` call is textually inside B1's `try:` (L211-289) → retry-on-error preserved.
- **P4 nested try:** `_step_call_conductor` keeps its own `try/except` → fallback (L461-467), NOT merged with B1. ✓
- **`_loop_idx` increment placement:** AFTER the P1 EXIT_LOOP/RESTART_ITER returns (L318), matching
  original L204. The P2 RESTART_ITER correctly fires *after* the increment (matches original — the
  will_call_llm-False `continue` was after `loop_idx += 1` in the god-method too). No production-only
  regression (the round-1 CONCERN-1 risk).
- **A1 guard:** P4-P9 wrapped in `if not pregen_ready:` (L246-271) — round-1 BLOCKER-1 addressed.

### Method-size table (AST)

| method | span | code | gate |
| --- | --- | --- | --- |
| `_run_loop` | 98 | 77 (body, excl 12-line doc) | **marginal miss** (~70 gate) |
| `_step_wait_for_start` | 33 | 27 | ok |
| `_step_pregen_decision` | 61 | 55 | **marginal miss** (cohesive) |
| `_step_read_state` | 52 | 44 | ok (code<50) |
| `_step_call_conductor` | 36 | 35 | ok |
| `_step_parse_actions` | 25 | 22 | ok |
| `_step_build_next_stems` | 47 | 40 | ok |
| `_step_submit_jobs` | 35 | 28 | ok |
| `_step_await_jobs_fetch` | 28 | 22 | ok |
| `_step_tile_audio` | 17 | 17 | ok |
| `_step_append_audit` | 3 | 3 | ok |
| `_step_commit_to_mixer` | 40 | 31 | ok |
| `_step_commit_state` | 101 | 81 | **exception** (acknowledged) |
| `_step_post_commit` | 39 | 29 | ok |
| `_step_await_pregen` | 44 | 31 | ok |

---

## DoD gate-2 verdict: **PARTIAL**

- (a) `_run_loop <=~70` LOC driver: **marginally NOT-MET** (77 nonblank). Structurally a clean
  driver; the overshoot is formatting-only (tuple unpacking). A defender reading "~70" generously
  could call it MET-marginal; the strict count is over by ~7 lines.
- (b) `_step_* <=50` (commit_state excepted): **MOSTLY MET.** `_step_pregen_decision` (55 code) is
  marginally over; both over-50 methods are cohesive SRP and the overage is docstring + debug-print,
  not mixed responsibility.

No BLOCKER. All three deviations are behavior-preserving; all invariants (lock-safety,
outside-lock recording, B1 watchdog, nested-try, A1 guard) are intact; 573 tests green; ruff clean.

**VERDICT: DECOMP_HAS_DEBT** — structurally sound and behavior-identical (not a blocker in sight),
but carries low-effort cleanup debt: a marginally-overlong driver (tuple-unpack inflation), two dead
return values (`_next_stems`, `_current_loop_end_sample`), a redundant defensive guard, and
pre-existing duplicated action-log code. Highest-leverage single fix: NamedTuple/dataclass returns for
the wide tuples (`_StateSnapshot`, `_PregenDecision`) → collapses the driver to ~55 LOC, removes the
positional-swap hazard, and turns both gate-2 sub-gates green.
