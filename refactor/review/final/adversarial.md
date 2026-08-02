# Final Adversarial Review — E1–E6 Refactor (CONCURRENCY + CACHE-DIVERGENCE red-team)

Scope: behavior-preservation audit of the extraction of ~450 LOC out of the live
async audio loop (`AsyncFrameworkLoop._run_loop`) into `audio_fetch`,
`pregeneration`, `conductor_interaction`, `domain_audio`, `audit_recording`,
`job_queue`, and the `framework_main_async` re-export shim.

Baseline: `906a49b` (542 passed). Current HEAD: `a7446ff`.
Suite at HEAD: **569 passed, 58 skipped, 9 xfailed, 16 xpassed, 0 failed** (re-run).
Method: every claim cross-checked against the baseline source via `git show`,
plus targeted runtime repros for the suspect paths.

Attack vectors: 1) concurrency/lock-scope, 2) cache divergence, 3) shared stem_cache,
4) frozen API, 5) string-patch no-op, 6) in-place mutation.

---

## Findings

### AUDIO-FETCH | CONCERN | tests/test_framework_characterization.py:509-514 (+ app/framework/loop_orchestrator.py:95,112-125)

**The Gap 3 exception-swallowing assertion is now VACUOUS — the safety net it claims to be has a hole.**

`_fetch_audio` no longer reads `self.garage` (property, re-reads `self._garage` every
call). It now goes through `self._audio` (`loop_orchestrator.py:112-125`), which **caches
the `GarageAudioAdapter`** in `self._audio_adapter` on first access, and the adapter caches
its client in `self._garage_client` (`audio_fetch.py:40-41`).

Gap 3 (`test_fetch_audio_empty_bytes_and_exception_return_none`) uses ONE loop for two
cases. Case A sets `loop._garage = garage_empty`, Case B sets `loop._garage = garage_err`.
Because the adapter is cached after Case A, **Case B reuses `garage_empty`** and never
invokes `garage_err`:

```
Case A (empty) -> None            # garage_empty.get_object -> b""  (correct)
Case B (raise) -> None            # STILL garage_empty.get_object -> b""  (NOT the RuntimeError path!)
garage_err.get_object called? 0          <- the exception mock is never exercised
garage_empty.get_object call count: 2    <- called twice (A and B)
adapter._garage_client is garage_empty? True
```

So Case B passes green for the WRONG reason (empty-bytes, not exception-swallow). The
`except Exception -> None` branch of `audio_fetch.fetch` (`audio_fetch.py:55-57`) — the
exact "fetch/decode failure must not crash the live loop" guarantee — is **no longer
covered by any test**. A future edit that breaks exception swallowing will still pass 569
green.

Production behavior is itself preserved (the `except` still returns `None`); this is a
**test-net regression**, not an audible bug. In the baseline `_fetch_audio` used `self.garage`
(property → `self._garage`), so Case B correctly hit `garage_err` and tested the path.

Fix: reset `loop._audio_adapter = None` between cases (or use a fresh loop per case), so
each injected `_garage` is honored.

### CONCURRENCY | SUGGESTION | tests/test_loop_lock_safety.py:35 (+ app/framework/conductor_interaction.py:37-38, job_queue.py:43-53)

**The "no I/O inside state.lock" AST guard is narrower than the invariant it claims to enforce.**

`test_no_io_inside_state_lock_in_orchestrator` reads ONLY `loop_orchestrator.py`
(`Path("app/framework/loop_orchestrator.py")`) and flags only direct `await` or `open(`
nodes inside a `state.lock` block. It does not cover the extracted modules and would miss
**indirect** I/O moved inside a lock, e.g.:

- `load_available_models()` (`conductor_interaction.py:37-38`, does `open(_MODELS_CONFIG_PATH)` + `json.load`) — a bare `Call`, not `open(`/`await`, so a lock-body call would be invisible to the guard.
- `submit_generator_job()` / `_submit_job()` (`job_queue.py:43-53`, DB `session()`) — same.

**No live regression**: verified that `load_available_models()` is called OUTSIDE every
`state.lock` block on both paths (`loop_orchestrator.py:287`, dedented after the Step-1
lock; `pregeneration.py:42`, no lock acquired at all), and `flush_recording_buffers` /
`append_loop_audit` keep all DB I/O outside `state.lock` (`audit_recording.py:32-62` copies
buffers under lock, releases, then writes). The concern is the guard's false confidence.

Strengthening: walk all seven framework modules and/or maintain an allow-list of
"I/O-bearing" callables the guard also greps for inside lock bodies.

### AUDIO-FETCH | SUGGESTION | app/framework/loop_orchestrator.py:104-110,112-125

**`self.garage` property and `_fetch_audio` no longer share one client (latent inconsistency).**

`fetch` now resolves its client through the cached `GarageAudioAdapter` (own `_garage_client`),
while the legacy `garage` property (`:104-110`) still creates+caches a SEPARATE client in
`self._garage` if ever accessed. Baseline had both paths read the SAME `self._garage`. No
current caller touches `loop.garage` in production (grep: only a comment at `:52`), so this
is latent, not live — but any future code that inspects/closes the client via `loop.garage`
expecting it to be the fetch client would be wrong. Consider deleting the now-orphaned
`garage` property or having the adapter read through it.

---

## Verified-preserved invariants (no regression)

| Vector | Invariant | Verdict | Evidence |
| --- | --- | --- | --- |
| 1 concurrency | No I/O inside any `state.lock` block (all modules) | OK | `grep` of all `async with state.lock` sites in `loop_orchestrator`/`audit_recording`; DB I/O in `flush_recording_buffers` is outside the lock (`audit_recording.py:32-62`); `pregeneration` acquires no `state.lock`; `load_available_models()` called outside lock (`loop_orchestrator.py:287`) |
| 2 cache divergence | foreground calls `state.cache_stem`, background does NOT | OK | `loop_orchestrator.py:414-417` calls `state.cache_stem` inside a lock; `pregeneration.py:104-107` writes ONLY `loop.stem_cache[cache_key]`. `test_pregeneration_does_not_route_through_cache_stem` asserts `cache_stem` NOT called (green) |
| 3 shared stem_cache | pregen uses the loop's SINGLE `stem_cache` (R11) | OK | `pregeneration.run_pregeneration(loop, …)` reads/writes `loop.stem_cache`; constructed via `_pre_generate_next_loop` → `run_pregeneration(self, …)`. `test_pregen_skips_job_when_stem_already_cached` proves the shared cache skip (green) |
| 4 frozen API | 9 names importable + one-arg ctor + wait patches target loop_orchestrator | OK | runtime probe: all 9 names non-None; `AsyncFrameworkLoop(uuid4())` constructs; `_wire_loop_no_io` patches `loop_orchestrator.wait_for_multiple_jobs` (`test_framework_characterization.py:243`) which the foreground path resolves from that module (`loop_orchestrator.py:18` import + call) |
| 5 string-patch | `decode_aac`/`create_garage_client_from_env` resolve where fetch reads them | OK | `audio_fetch.py:21-23` binds both at module top; `test_worker_fetch_audio.py:43-44,74-75` patch `app.framework.audio_fetch.*` (migrated) and assert the mock is invoked; no test still patches the shim path (grep clean) |
| 6 in-place mutation | `process_actions` mutates `_original_details["_age"]` on the live list | OK | `conductor_interaction.py:55-58` mutates `active_stems[idx]…` in place; Gap-1 test asserts `active_stems[0]["_original_details"] is retained` and `_age==3` (`test_framework_characterization.py:395-398`); foreground passes `list(state.active_stems)` (shallow → shared dicts) so mutation reaches state — identical to baseline |

### Additional parity checks (git diff vs `906a49b`)

- `process_actions`: byte-equivalent (dedup key, `_age` mutation, defaults). ✓
- `flush_recording_buffers` / `_append_loop_audit` + 4 `_audit_*` shapers: faithful
  extraction (method → module fn reading the `state` singleton); `_flush_lock` identity
  preserved (`shim._flush_lock is audit_recording._flush_lock` → True). ✓
- `tile_to_loop` (P9 inline block → `domain_audio.tile_to_loop`): same cache-key, tiling,
  silence-fallback, `_to_two_channel` coercion. Foreground + pregen both call it. ✓
- `submit_generator_job` (`_submit_job` body): same row shape (`status="pending"`,
  `expires_at=now+24h`), same lazy model/DB import. ✓
- Pre-existing `zip(pending_jobs, results.values())` positional-alignment (results keyed by
  job_id but matched by position) is **unchanged from baseline** — not introduced by this
  refactor (flagged only for completeness; out of scope here).

---

## VERDICT: MINOR_REGRESSIONS

No production/audible behavior regression — all six load-bearing invariants are preserved
and the suite is 569-green. **One genuine test-net degradation** (Gap 3 exception-swallow
assertion rendered vacuous by the `GarageAudioAdapter` caching) plus two SUGGESTIONs (AST
guard scope, orphaned `garage` property). None block, but the Gap-3 vacuity should be fixed
so the exception path is actually guarded again.
