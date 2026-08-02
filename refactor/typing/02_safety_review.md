# 02 — Adversarial Safety Review (Optional-Narrowing Asserts)

Agent: `safety-reviewer` (fresh context). Mission: independently **falsify** the
claim in `01_narrowing_inventory.md` that `self.mixer` and
`self._pregen_results` are provably non-None at every unguarded use site. If the
claim is wrong, the planned asserts mask real None-deref bugs.

Scope: `app/framework/loop_orchestrator.py`, `app/framework/pregeneration.py`,
and the test harnesses. Evidence is grep-confirmed + control-flow traced.

---

## Attack 1 — MIXER: can `self.mixer` be None at a `_step_*` use site?

**Verdict: CONFIRMED-SAFE.**

Independent evidence:

1. **Only one production writer.** `grep -rn '\.mixer\s='` (whole repo) returns
   exactly three sites: `start()` L187 (the real assignment), and two *tests*
   that inject a fake (`tests/test_loop_fixes.py:268`, `test_framework_characterization.py:251`).
   **No site ever sets `self.mixer = None`** — not `stop()`, not `_finish_loop()`,
   not any error path. The field is monotonic None→Mixer.

2. **`_run_loop` cannot start before the assignment.** In `start()` the order is
   L187 `self.mixer = await run_in_executor(...)` → L188 `self.mixer.start()` →
   L191 `create_task(self._run_loop())`. The assignment is an *awaited* executor
   call, so it completes before `create_task` even schedules the task. By the
   time `_run_loop`'s first line runs, `self.mixer` is bound.

3. **If `Mixer()` raises**, the exception propagates out of `start()`, `loop_task`
   is never created, and **no `_step_*` ever runs**. (`stop()` in the `finally`
   of `run_framework_loop_async` is None-safe: `if self.mixer:` guards L208/216.)
   So a constructor failure cannot reach a `_step_*`.

4. **`_run_loop` has exactly one production caller** — `start()`'s `create_task`
   (grep-confirmed: every other `_run_loop` hit is a comment/docstring). There is
   no second entry point that could invoke a `_step_*` on an un-started loop.

5. **Tests bypass `start()` but set a NON-None fake.** Every `_run_loop`-driving
   test goes through `_seed_loop_for_run` (sets `loop.mixer = _FakeMixer(...)`) or
   `test_loop_fixes`'s inline `loop.mixer = _FakeMixer()`. **No test drives
   `_run_loop` with `mixer` left at its `__init__` None** (verified by reading
   both harnesses). And **no test calls an individual `_step_*` method directly**
   (`grep '_step_read_state|_step_commit_to_mixer|...' tests/` → no matches).

Concrete-None scenario attempted and **failed**: there is no production or test
path that reaches `self.mixer.clear()` (L407), the `_step_commit_to_mixer` mixer
block (L666–676), or the `_step_await_pregen` mixer reads (L838–859) with
`mixer is None`.

---

## Attack 2 — PREGEN_RESULTS: can `self._pregen_results` be None inside `if pregen_ready:`?

**Verdict: CONFIRMED-SAFE.**

### 2a. Predicate + pass-through integrity

The predicate at `_step_pregen_decision` (L350–355) is exactly:

```python
pregen_ready = (
    self._loop_idx > 1
    and self._pregen_results is not None
    and self._pregen_results.get("loop_idx") == self._loop_idx
)
```

Short-circuit guarantees `is not None` before `.get`. In the driver `_run_loop`,
`pregen_ready` is captured once from `pregen.pregen_ready` and **never
reassigned**; it is passed positionally (unmodified) to `_step_commit_to_mixer`
and `_step_commit_state`. Confirmed by reading the driver body — no
`pregen_ready =` between the capture and the two call sites.

### 2b. The nulling window (the crux)

Question: between P2's predicate eval and the P10/P11 reads, do the `await`
points resume a coroutine that nulls `_pregen_results`?

When `pregen_ready` is **True** at P2, the path taken is P2 → P3 → (P4–P9
*skipped*) → append-audit → P10 → P11 → P12. The `await`s in that window are
P3's `async with state.lock:` + `load_available_models()`, and
`_step_append_audit`. **None of those functions writes `_pregen_results`**
(grep `_pregen_results =` repo-wide = exactly 4 writers: `pregeneration.py:128/147`,
`loop_orchestrator.py:809/817`; none is inside P3/read_state, call_conductor,
parse_actions, append_audit).

The two None-writers are both unreachable in that window:

- **`loop_orchestrator.py:809`** (`_step_post_commit`, `self._pregen_results = None`)
  is **P12**, which the driver calls *after* P11. Not in the window.
- **`pregeneration.py:147`** (error path) runs *inside a pregen task*. But when
  `pregen_ready` is True at P2, the loop-N pregen task is **already DONE** (it
  wrote the result we're reading), and the loop-N+1 pregen task **does not exist
  yet** (it is spawned at P12 of the *current* iteration). So **zero pregen tasks
  are live** during P2→P11 when pregen_ready is True. No live task → no
  nulling resume.

### 2c. The break-#2 race (the one scenario that looked promising)

`_step_await_pregen` (P13) can break via `current_ahead < 0.5` **before**
`_pregen_done` is set, letting the *next* iteration's P2 run while a pregen task
is still live. I chased this hard:

- If break-#2 fired in iteration N−1, then at iteration N's P2
  `_pregen_results` is **still None** (set to None at N−1's P12 L809, pregen task
  hasn't overwritten it yet) → `pregen_ready = False` → **fresh path; P10/P11
  take their `else` branches and never read `_pregen_results`**. The live pregen
  task may later null it (error path) or fill it (success), but neither is read.
- The only way to *enter* a `if pregen_ready:` block is for `_pregen_results` to
  be a **completed, matching-loop_idx dict**, which can only exist if its
  producing task finished — which means no task is live to null it.

**Conclusion:** there is no execution where `_pregen_results` is None inside an
`if pregen_ready:` block. Concrete-None scenario attempted and **failed**.

---

## Attack 3 — ERROR-PATH CONSISTENCY in `run_pregeneration`

**Verdict: CONFIRMED-SAFE.**

`pregeneration.py` L147–148:

```python
loop._pregen_results = None   # L147
loop._pregen_done.set()       # L148
```

and the success path L128–129 analogously (dict, then `.set()`). Both pairs are
**consecutive synchronous statements with no `await` between them** and no I/O —
plain attribute write + `Event.set()` (neither can raise in normal CPython). The
whole function is inside one `try/except`, and L147/148 sit in the `except`
handler, so they cannot be preempted by a mid-handler `await`. The
write-before-`.set()` ordering the inventory flagged as fragile is in fact
preserved on both paths. Verified by reading the full function.

---

## Attack 4 — Under `python -O` (asserts stripped)

**Verdict: NOTE, not a falsification.**

`grep` for `-O`/`PYTHONOPTIMIZE` across the project's own runtime config
(`*.toml`, `*.cfg`, `*.sh`, `*.yml`, `Dockerfile*`) finds **no use of `-O`**.
The only hits are numpy's vendored test-suite and prior agent artifacts.
Asserts therefore survive in every actual run. Even hypothetically, since
Attacks 1–2 establish the values are *genuinely never None*, a stripped assert
would not turn into an `AttributeError` — there is nothing to deref-fail.
Acceptable **because** the claim is airtight.

---

## Attack 5 — Any inventory-marked-SAFE site that can return None?

**Verdict: NONE found.**

Exhaustive grep of every `self.mixer` / `self._pregen_results` access in
`loop_orchestrator.py` (33 hits) cross-referenced against the inventory's
use-site table. Every unguarded access falls in one of the 7 assert-target
sites; every other access is already runtime-guarded (`if self.mixer:`,
`… if self.mixer else None`, `if self._pregen_results`). The guarded sites
(L188, L208/216, L360, L364, L635) are correctly *excluded* from the assert plan.

One non-None subtlety worth recording (does **not** affect the asserts): the
`_step_post_commit` else-branch dict (L817) omits `master_bpm`/`master_key`/
`set_name`/`reasoning`/`actions`, but every P2/P11 read of those uses `.get(key,
default)`, so it degrades gracefully — no `KeyError`, certainly no `None`-deref.

---

## Baseline confirmation

`pytest tests/test_framework_characterization.py tests/test_loop_fixes.py
tests/test_loop_lock_safety.py tests/test_pregeneration_divergence.py` →
**29 passed** (venv). Green baseline corroborates that mixer is non-None across
every test-driven `_run_loop` path.

---

## VERDICT: SAFE-TO-ADD-ASSERTS

All 7 sites. The claim in `01_narrowing_inventory.md` is airtight; I could not
falsify it on any of the five attack vectors.

**Mixer (3) — `assert self.mixer is not None` at method top:**

1. `_step_read_state` — guards L407 `self.mixer.clear()`.
2. `_step_commit_to_mixer` — guards L666–676 (mixer is used unconditionally in
   both the `_loop_idx == 1` and `else` arms).
3. `_step_await_pregen` — guards L838–859.

**Pregen (4) — `assert self._pregen_results is not None` as first stmt of each
`if pregen_ready:` block:**
4. `_step_pregen_decision` (~L368).
5. `_step_commit_to_mixer` (~L655).
6. `_step_commit_state` (~L708).
7. `_step_commit_state` (~L720) — second block; needs its own assert because
   pyright widens back to Optional between two separate `if` blocks even though
   no mutation occurs.

**Do-not-assert sites (already guarded):** L188 (post-assign), L208/216
(`if self.mixer:`), L360/364/635 (`… if X else None`), L352/353 (the pregen
gate predicate itself). Adding asserts there is noise; the inventory correctly
excludes them.

**Residual fragility to preserve (unchanged from inventory, independently
confirmed):** `run_pregeneration` must keep write-before-`_pregen_done.set()`
order on both paths; no code may introduce a `_pregen_results = None` between
P2 and P11 of a pregen-ready iteration, or a second live pregen task.
