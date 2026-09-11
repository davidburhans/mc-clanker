# PLAN — Unit 2 `rel-reset-reprime` (REL-02 + REL-06), branch `rel-02-reset`
**Spec:** `refactor/plans/rel-remediation-plan.md` §U2 · `docs/reliability_audit.md` REL-02 (Critical), REL-06 (High)
**Baseline gate (verified green at `d58d046`):** `939 passed / 16 skipped`, `ruff` clean; do not regress.
---
## 0. Scope summary
| Finding | Root cause | Fix site |
|---|---|---|
| REL-02 | `should_reset` → `Mixer.clear()` zeroes `current_loop_end_sample`; only the loop-1 path ever calls `prime_loop`, and `set_next_loop` never restores the boundary, so the mixer's transition gate (`current_loop_end_sample > 0`) stays closed forever = permanent silence while generation/`loop_count` continue | `loop_steps.py::_step_commit_to_mixer` (force prime when boundary ≤ 0) |
| REL-06 | Cache HIT paths never refresh `last_used`; eviction (`> 300 s`) then evicts+regenerates any retained stem every 5 min forever (audible "core groove" churn + needless GPU) | `loop_steps.py::_step_submit_jobs` + `_prune_stem_cache` (new), `pregeneration.py` hit path |
No worker / LLM-capture / storage code is touched (invariants 4–5 irrelevant). One fake-mixer fixture in each of two test files gains real-mixer boundary semantics (§2.3).
---
## 1. Design decisions (documented deviations from spec letter)
1. **Fix site: the commit step (P10), not the reset branch.** The audit allows either ("in the reset branch (or commit step)"). The reset branch (`_step_read_state`, L372-376) runs at **P3 — after P1 incremented `_loop_idx` and after P2 already made the pregen decision** — so a `_loop_idx = 0` written there still leaves the *current* iteration committing through the `else` (`set_next_loop(loop_idx=0)`) branch: dead staged audio the gate never consumes, one wasted full conductor+generation cycle, P11 rotating `active_stems` for a loop nobody hears, and a stale `next_loop_audio` residue that races the next staging. Normalizing in P10 (immediately before the branch) makes the **same** iteration prime, wastes nothing, leaves no residue — and also fixes the *second* boundary-zeroing site the audit's gate note calls out (`framework_mixer.py` `Mixer._callback` no-future-tracks fallback → `current_loop_end_sample = 0`): any commit into a boundary-less mixer now re-primes instead of staging dead audio.
2. **Force value: `_loop_idx = 1`, not 0.** The audit's literal example (`force self._loop_idx = 0` so "P1's increment makes it 1") pays the wasted-iteration cost above (the current commit still sees 0 ≠ 1). Setting 1 directly takes the prime path *now*; the next P1 increments to 2, and P12's `elif self._loop_idx == 1` branch fabricates the loop-2 pregen result exactly like a cold start — the post-reset flow becomes byte-for-byte the loop-1→loop-2 sequence production already exercises. Downstream semantics of the forced 1 (all verified against the step code): P11 `needs_initial_record = True` (a primed loop fires no transition event, so the initial record is *required* for `loop_history`), P11 `needs_pregen = False`, P12 fabricates loop-2 + sets `_pregen_done`, P13 exits promptly (`_staged_loop_idx == 0`). `state.loop_count` keeps climbing monotonically (soak #1); only the show-audit loop indices restart at 1 (cosmetic, matches loop-1 semantics).
3. **No pregen-state cleanup in the reset branch** (deviation from scout's suggestion, deliberate): clearing `_pregen_results` at P3 after P2 already accepted it would trip P10's `assert self._pregen_results is not None` → B1 watchdog retry — an assert-driven retry is a new failure mode for zero benefit. Leaving a stale in-flight pregen alone is safe: (a) its results carry pre-reset loop indices ≥ 3, which can never equal the post-reset next index (2), so P2's `loop_idx`-equality gate rejects them; (b) P12's loop-1 branch overwrites `_pregen_results` anyway; (c) stray `stem_cache` entries it writes won't key-match post-reset prompts (cache key embeds bpm/key/prompt) and TTL-evict within 300 s; (d) the running task at most delays the next pregen spawn until it finishes (bounded by the 600 s job budget).
4. **Unlocked `current_loop_end_sample` read in P10 is safe and sanctioned.** `test_orchestrator_has_no_private_mixer_reach`'s docstring explicitly declares `current_loop_end_sample`/`current_sample` "intentionally NOT forbidden — both are declared public". GIL-atomic int read; the only writers that go → 0 are `Mixer.clear()` (runs earlier in the *same* event-loop task at P3 — no interleaving possible) and the audio-thread no-future fallback (a stale *positive* read merely skips the force = today's behavior; a stale 0-read is impossible since transitions only replace one positive boundary with another). No `state.lock` is held in P10, so the lock-discipline guard is untouched.
5. **REL-06 refresh writes `last_used = time.time()` directly on both hit paths** (no lock): `stem_cache` is the loop's single-owner dict — the existing write sites (`loop_steps.py:571`, `pregeneration.py:126`) already write it unguarded from the same owners. The pregen refresh writes **only** `loop.stem_cache[key]["last_used"]`, never `state.cache_stem` — the pinned foreground/background divergence (brief-01 risk #4) is preserved and re-asserted by an extended test.
6. **Entry cap implemented** (spec says "consider"): TTL bounds age, not count — during an LLM-outage retain-all fallback or fast prompt churn, 4–6 new entries/loop accumulate for a full 300 s window with no bound. Cap = `STEM_CACHE_MAX_ENTRIES = 32` (~5–6 MB per 8-bar 44.1 kHz stereo stem ⇒ ~180 MB worst case; generous vs the 4–6 stems/loop working set; the sibling `state.cache_stem` LRU caps at 16). TTL (300 s) is lifted into `STEM_CACHE_TTL_SECONDS` — the magic `300` disappears.
7. **AST pins survive by construction** (verified against `tests/test_loop_lock_safety.py`): the force is a separate `if` inserted *before* the `_staged_loop_idx` assignment, so `_if_loop_idx_eq_one` still finds the bare `if self._loop_idx == 1:` Compare; the loop-1 branch stays a single `prime_loop` Expr and the `else` a single `set_next_loop` call; no `with self.mixer.lock:` is added; no await/open inside any `state.lock` block (the reset branch gains nothing).
---
## 2. Exact changes per file
### 2.1 `app/framework/loop_steps.py` (~880 → ~900 lines; see §6 budget note)
**(a)** Constants after `BOUNDARY_BREAK_SECONDS` (~L66):
```python
# REL-06 (audit High): a retained stem is cache-HIT every loop, so its TTL
# must be measured from the last HIT, not the initial fetch — otherwise the
# "core groove" the conductor is told to retain ages out and is regenerated
# every 300 s (audible churn + needless GPU), forever.
STEM_CACHE_TTL_SECONDS = 300.0

# REL-06 entry cap: TTL bounds age, not count — a retain-all fallback (LLM
# outage) or fast prompt churn adds 4-6 entries/loop for a full TTL window.
# ~5-6 MB per 8-bar 44.1 kHz stereo stem => ~180 MB worst case (sibling
# state.cache_stem LRU caps at 16).
STEM_CACHE_MAX_ENTRIES = 32
```
**(b)** `_step_commit_to_mixer` — insert immediately **before** the `# B2: loop>1 hands its audio...` comment / `_staged_loop_idx` assignment (~L628-634). Branch bodies stay untouched (AST pin, decision 7):
```python
        # REL-02 (audit Critical): a boundary-less mixer (reset via
        # Mixer.clear(), or the no-future-tracks fallback in Mixer._callback)
        # can never consume set_next_loop audio — the transition gate is
        # current_loop_end_sample > 0 — so staging there is permanent silence.
        # Re-enter the loop-1 prime path instead: it re-establishes the
        # boundary now, and _loop_idx 1 gives P11/P12/P13 the correct loop-1
        # semantics (initial record, no pregen spawn, prompt P13 exit).
        if self.mixer.current_loop_end_sample <= 0:
            self._loop_idx = 1

        self._staged_loop_idx = self._loop_idx if self._loop_idx > 1 else 0
```
**(c)** `_step_submit_jobs` cache-hit branch (~L521-527):
```python
            if cache_key in self.stem_cache:
                print(f"Cache HIT: '{prompt}'")
                # REL-06: TTL clock restarts on every hit — a retained stem
                # must not age out while in active use.
                self.stem_cache[cache_key]["last_used"] = time.time()
                continue  # Already have audio
```
**(d)** `_step_post_commit` — replace the inline eviction block (~L766-770) with one call:
```python
        # Cache maintenance (REL-06: TTL from last use + entry cap)
        self._prune_stem_cache()
```
and add the method after `_step_post_commit` (13 LOC):
```python
    def _prune_stem_cache(self) -> None:
        """REL-06: drop stale-then-overflow stem-cache entries (TTL + cap).

        TTL first (no hit for STEM_CACHE_TTL_SECONDS), then oldest-last_used
        overflow beyond STEM_CACHE_MAX_ENTRIES. Called from P12 only —
        stem_cache has a single async owner (this loop task), so no lock.
        """
        now = time.time()
        stale_keys = [k for k, v in self.stem_cache.items() if now - v["last_used"] > STEM_CACHE_TTL_SECONDS]
        for key in stale_keys:
            del self.stem_cache[key]
        overflow = len(self.stem_cache) - STEM_CACHE_MAX_ENTRIES
        if overflow <= 0:
            return
        by_age = sorted(self.stem_cache.items(), key=lambda item: item[1]["last_used"])
        for key, _entry in by_age[:overflow]:
            del self.stem_cache[key]
```
### 2.2 `app/framework/pregeneration.py` (163 → ~168 lines)
Hit path (~L86-88):
```python
            if cache_key in loop.stem_cache:
                # REL-06: same TTL-refresh as the foreground hit in
                # _step_submit_jobs. This stays a loop.stem_cache-ONLY write —
                # never state.cache_stem (brief-01 risk #4 divergence).
                loop.stem_cache[cache_key]["last_used"] = time.time()
                continue
```
### 2.3 Test fixtures (fakes gain real-mixer boundary semantics — required because P10 now *reads* the boundary on every commit)
**(a)** `tests/test_loop_fixes.py::_FakeMixer` (L231-261) — currently has `current_loop_end_sample = 0` but **no** `prime_loop` (a forced prime would `AttributeError`). Add:
```python
    def prime_loop(self, tracks, *, duration_samples):
        # REL-02: P10 re-primes whenever the boundary is 0; mirror the real
        # Mixer.prime_loop boundary write so later commits take set_next_loop.
        self.current_loop_end_sample = self.current_sample + duration_samples
```
**(b)** `tests/test_round3_fix_b.py::FakeMixer` (L108-158) — has **no** `current_loop_end_sample` attribute at all (P10's read would `AttributeError`). Add `self.current_loop_end_sample = 0` in `__init__`; set it in `prime_loop` (`= self.current_sample + duration_samples`, `clear()` zeroes it). Then in the two B2 tests that model "loop ≥ 2 over a live boundary", pre-establish it so the force correctly does **not** fire:
```python
    mixer.current_loop_end_sample = 44100 * 8  # loop 1 already primed (REL-02 force must not fire)
```
in `test_b2_pregen_done_does_not_drop_the_staged_loop` (L~384) and `test_b2_staged_wait_releases_when_the_playhead_is_frozen` (L~408). `test_b2_loop_one_priming_stages_nothing` (L423) needs nothing — loop 1 primes anyway. Unaffected fakes (verified): `test_framework_characterization.py::_FakeMixer` (prime_loop already sets the boundary), `test_mixer_controller_characterization.py::_LockSpyMixer` (has the attr; test drives `_loop_idx = 1`), all `test_async_framework.py` MockMixers (drive mixer logic standalone, never `_step_commit_to_mixer`).
---
## 3. TDD regression tests (write first, confirm red, then implement)
### 3.1 New file `tests/test_reset_reprime.py` (~230 lines) — the unit's acceptance suite
Autouse `_clean_state` fixture (pattern: `test_loop_fixes.py::_reset_audit_state` + `test_pregeneration_divergence.py::_new_loop`): save/restore `should_reset`, `is_generating`, `is_running`, `active_stems`, `current_bpm/key`, `loop_count`; seed `state.current_bpm = 128`, `current_key = "A minor"`. Helpers: `_audio()` → 1 s stereo float32; `_loop_with(mixer)` → `AsyncFrameworkLoop(uuid4())` with `loop.mixer = mixer`; `_commit_result()` → `_CommitResult(needs_pregen=False, needs_initial_record=False, rec_stems=[], rec_set_name="", rec_reasoning="", state_snapshot={})`; `_RecordingMixer` — boundary-faithful fake recording `prime_loop_calls` / `set_next_loop_calls`.

| # | Test | Pins | Core assertions |
|---|---|---|---|
| R1 | `test_reset_then_restart_reprimes_boundary_and_transition_fires` (**soak #3 core, real `Mixer(channels=1)`**, pattern `test_mixer_resilience.py` headless `_callback` drives) | REL-02 end-to-end | Loop 1: `prime_loop([(a1,0)], duration_samples=D1)`. Loop 2: `set_next_loop([(a2,0)], D2, loop_idx=2)`; set `current_sample = D1 - frames` (inside the `frames + deadline_ms` window), `state.is_generating = True`, patched `state.broadcast_audio`; `mixer._callback(outdata, frames, None, None)` → `pop_transition_event() == 2` (≥ 2 loops ran). Reset: `state.should_reset = True`; `await loop._step_read_state()` → boundary 0, flag consumed. Re-start: `loop._loop_idx = 3`; `await loop._step_commit_to_mixer(False, [(a3,0)], D3)` → assert `mixer.current_loop_end_sample > 0` (**RED today**: stays 0), `mixer.next_loop_audio == []` (primed, nothing staged dead). Transition fires: `set_next_loop([(a4,0)], D4, loop_idx=4)`; drive `current_sample` to the new boundary window; `_callback` → `pop_transition_event() == 4` (**RED today**: gate closed). |
| R2 | `test_commit_into_boundaryless_mixer_takes_prime_path` | the force itself (covers the `Mixer._callback` no-future fallback class) | `_RecordingMixer` boundary 0, `loop._loop_idx = 5` → commit → `prime_loop_calls == 1`, `set_next_loop_calls == []`, `loop._staged_loop_idx == 0`, `loop._loop_idx == 1`, boundary > 0. |
| R3 | `test_commit_with_live_boundary_keeps_set_next_loop_path` | no over-forcing | boundary 44100, `_loop_idx = 5` → commit → `set_next_loop_calls[0]["loop_idx"] == 5`, `prime_loop_calls == []`, `_staged_loop_idx == 5`. |
| R4 | `test_reset_iteration_with_accepted_pregen_still_primes` | P2-before-P3 ordering safety (decision 3) | `pregen_ready=True` with populated `_pregen_results` (loop_idx 4), boundary 0, `_loop_idx = 4` → commit → no raise, prime called with the pregen's `prepared_tracks`. |
| C1 | `test_foreground_cache_hit_refreshes_last_used` | REL-06 main loop | Seed `loop.stem_cache[key] = {"audio_data": a, "last_used": now - 400}`; `await loop._step_submit_jobs([stem_for(key)], 128, "A minor")` with `_submit_job` AsyncMock → **not awaited** AND `stem_cache[key]["last_used"] > now - 400` (≈ now). |
| C2 | `test_pregen_cache_hit_refreshes_last_used` | REL-06 pregen path | Two `run_pregeneration` drives (mirror `test_pregen_skips_job_when_stem_already_cached`); capture `last_used` after run 1; after run 2 assert it advanced AND `state.cache_stem` never called (divergence pin extended). |
| C3 | `test_retained_stem_survives_ttl_prune_after_hit` (**audit acceptance**) | REL-06 no-eviction | Entry A: hit-refreshed (C1 flow); sibling entry B: never hit, `last_used = now - 400`. `await loop._step_post_commit(_commit_result(), [], 0)` → A present, B evicted. |
| C4 | `test_stem_cache_entry_cap_evicts_oldest` | cap (decision 6) | Seed `STEM_CACHE_MAX_ENTRIES + 3` entries with staggered `last_used` → `_step_post_commit` → `len == STEM_CACHE_MAX_ENTRIES`, oldest 3 keys gone, newest kept. |
| C5 | `test_pregen_hit_refresh_keeps_submit_skipped` | keep-green extension | After C2's second run, a third pregen with a fresh `_submit_job` mock → still not called (the existing divergence pin, re-asserted with refresh in place). |
### 3.2 Keep-green set (must not regress; run explicitly after implementation)
`test_pregen_skips_job_when_stem_already_cached` · `test_pregeneration_does_not_route_through_cache_stem` · `test_foreground_loop_routes_through_cache_stem` · AST/source guards `test_step_commit_to_mixer_loop1_is_single_prime_loop_call`, `test_no_io_inside_state_lock_in_orchestrator`, `test_orchestrator_has_no_private_mixer_reach` · characterization Gap-5 `test_run_loop_subsequent_set_next_loop_kwargs` · B1/B2 drives `test_run_loop_retries_after_transient_exception`, `test_b2_pregen_done_does_not_drop_the_staged_loop`, `test_b2_staged_wait_releases_when_the_playhead_is_frozen`, `test_b2_loop_one_priming_stages_nothing`, `test_b1_stale_pregen_replay_iterations_yield`.
### 3.3 TDD order
1. Fixture updates (§2.3) + new tests → `.venv/bin/python -m pytest tests/test_reset_reprime.py -q` → **red** (R1 boundary stays 0 / transition never fires; C1/C2 `last_used` unchanged; C3 retained entry evicted; R2 prime not taken).
2. Implement §2.1 → §2.2 → same command → **green**.
3. Keep-green sweep: `pytest tests/test_pregeneration_divergence.py tests/test_loop_fixes.py tests/test_round3_fix_b.py tests/test_loop_lock_safety.py tests/test_framework_characterization.py tests/test_mixer_controller_characterization.py -q`.
4. Full gate: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q` → **949 passed / 16 skipped** (939 + 10 new), zero regressions.
---
## 4. Invariant compliance (plan §Invariants)
| # | How respected |
|---|---|
| 1 — lock discipline | No new `state.lock` sections at all. The reset branch gains nothing; P10 holds no lock and reads one public int (decision 4). `_prune_stem_cache` is lock-free on the single-owner dict (same as the code it replaces). Pregen refresh is a dict-item write by the same single async owner that already writes that dict. |
| 2 — style / hexagonal | `_prune_stem_cache` 13 LOC with docstring; the force is 2 lines + comment; explicit types everywhere, no `Any` added; one spelling per concept (`STEM_CACHE_TTL_SECONDS`, `STEM_CACHE_MAX_ENTRIES`). Ports/`MixerController` surface untouched; fakes in tests only. File budget: loop_steps 880→~900, pregeneration 163→168 (< 500; loop_steps is pre-existing acknowledged brownfield debt — its module docstring exists precisely to keep `loop_orchestrator.py` under 500; further splitting out of scope, §6). |
| 3 — audio thread | Zero mixer-thread changes: `prime_loop`/`set_next_loop`/`clear`/`_callback` untouched. The new P10 read is off-thread, GIL-atomic. |
| 4 — LLM capture | `_append_loop_audit` untouched. Cosmetic note: on the reset iteration only, the audit row keeps the pre-force loop index (P4's audit runs before P10's force) — no data dropped/truncated. |
| 5 — worker restart semantics | Not touched. |
| 6 — regression per fix | R1–R4 pin REL-02 (incl. the audit soak #3 scenario in miniature), C1–C5 pin REL-06; §3.2 guards the neighbors. |
---
## 5. Acceptance checklist (maps to §U2 spec)
- [ ] Reset-then-restart regression: run ≥ 2 loops, trigger `should_reset`, re-start → `current_loop_end_sample > 0` and a transition fires → R1 (+ R2/R3 boundary guards, R4 pregen edge)
- [ ] Cache-hit test asserts `last_used` advanced and no eviction of a retained stem → C1/C2/C3
- [ ] Entry cap considered → implemented + pinned (C4, decision 6)
- [ ] `ruff check` clean; full suite 949 passed / 16 skipped (~7–8 s)
## 6. Risks / out of scope
- **Test-fake churn** (§2.3): two fakes gain `current_loop_end_sample` semantics and two B2 tests pre-establish a boundary — mechanical, and it makes those fakes *more* production-faithful (a loop-2 staging only ever happens over a primed boundary in reality).
- **Unlocked boundary read** staleness argument (decision 4) is documented in-code; if a future mixer change adds another →0 writer, the force may skip (today's behavior) — never wrongly fire.
- **Post-reset loop numbering**: show-audit `loop_index` restarts at 1; `state.loop_count` stays monotonic. In-flight pre-reset pregen task survives (decision 3) and can delay the next pregen spawn by ≤ one job budget (600 s) — bounded, no silence (the forced-prime iteration runs the fresh path meanwhile).
- **Cap = 32 is a heuristic constant**, not config (YAGNI; the constant is the seam if tuning is ever needed).
- Out of scope: REL-12 abandoned-job reaping, REL-04 audit-buffer flush, why the mixer's no-future-tracks fallback fires (only its silence consequence is fixed), making `should_reset` clear `active_stems`/audit numbering.
