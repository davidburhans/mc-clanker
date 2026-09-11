# PLAN — Unit 4 `rel-llm-capture` (REL-04 + REL-14-amended + DPO field audit), branch `rel-04-capture`
**Spec:** `refactor/plans/rel-remediation-plan.md` §U4 (amended by the 2026-09-11 verification pass) · `docs/reliability_audit.md` REL-04 (Critical), REL-14 (High, weakened-but-real)
**Baseline gate (green at `aca9d16`, HEAD of `main`):** `968 passed / 16 skipped`, `ruff` clean; do not regress skips without cause.
Preflight: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q`

---

## 0. Scope summary

| Item | Root cause | Fix site |
|---|---|---|
| REL-04 (unbounded buffers) | `append_loop_audit` appends 1 LLMInteraction + N ShowAction dicts per loop, no cap; only flush caller is `stop_show`; a week-long set holds 0.2–0.6 GB in RAM and the whole audit trail dies on any crash (never touched disk) | periodic flush from `_step_post_commit` past a 200-row threshold, routed through the ctor-injected audit port; keep stop-flush; add best-effort bounded flush in lifespan shutdown |
| REL-14 (amended: silent discard) | `start_show` (shows.py:395-405) clears both buffers under `state.lock` to dodge the FK-poison loop — that clearing *silently discards* captured rows, violating invariant 4 (the buffers ARE the fine-tuning corpus) | `start_show`: `await flush_recording_buffers()` BEFORE the clear; the clear degenerates to a no-op on success and *retains* (logs) rows on a failed flush instead of discarding them |
| REL-14 (residual FK poison) | `delete_show` cascades the row away while buffered rows still reference the deleted `show_id` → next flush FK-fails → batch re-prepends → every later flush fails identically (mitigated today only by the start_show clear this unit removes) | `delete_show`: deliberately drop ONLY the deleted show's buffered rows under `state.lock`, log the count (new `drop_buffered_rows_for_show`) |
| DPO field audit (user directive) | The fine-tuning corpus needs the real chat; today `prompt_messages` stores a 5-key context summary ("note: full prompt built in ConductorLLMAsync"), so `/export/llm-dump` user turns are near-empty and the assistant turn isn't even a chat message; no applied-vs-requested action capture | conductor attaches the exact system+user chat to its response; audit persists it as `prompt_messages` (list of `{role, content}`); new additive `applied_actions` JSON column (migration `003`) capturing the post-dedupe enacted stems + generation outcome; `to_llm_dump_dict` emits a training-consumer-shaped row (`messages` incl. assistant JSON-string turn + `meta`) |

No mixer/worker/storage code is touched (invariants 1, 3, 5). Invariant 4 is the unit's *subject*.

---

## 1. Design decisions (documented reasoning; deviations from spec letter flagged)

1. **Threshold flush routes through the audit port, not the module function.** `self._audit.flush()` (the ctor-injected `AuditSinkPort`, default `AuditAdapter()`) — completes the port wiring the U3-audit docstring deferred to U4, keeps testability via ctor injection (fake sinks already implement `flush`), and preserves dual-ownership: routes/shutdown call the module `flush_recording_buffers`, the loop calls the port; both serialize on the same module-level `_flush_lock`, so no interleaving hazard. `ports.py` contract text ("the loop never calls flush") is updated accordingly.
2. **Threshold = `AUDIT_FLUSH_THRESHOLD_ROWS = 200`, checked on `len(state.llm_interaction_buffer)` only** (spec letter). `len()` of a plain list is GIL-atomic — read unlocked from P12; the flush itself takes `_flush_lock` + `state.lock` internally. At ~1 interaction/loop this flushes every ~200 loops (~27 min at 8 s/loop): crash loss ≤ ~201 interaction rows + their actions, RAM bounded at ~1 MB. Call site is the END of `_step_post_commit` (after the pregen spawn) so a slow flush never eats pre-generation lead time, and it runs on the async loop task — the mixer thread is never involved ("off the audio path"). A `try/except Exception` around the call is belt-and-braces: the module flush already re-queues internally, and `except Exception` lets `CancelledError` propagate.
3. **Flush DB I/O moves to `asyncio.to_thread` (one deliberate widening beyond the spec letter).** The new call site makes the flush hot-path (per-loop cadence, not the old one-shot user action): `bulk_insert_mappings` is sync SQLAlchemy, and on the event loop a slow/timing-out DB would stall every route/WS for the duration (REL-09 class). Extraction: sync helper `_insert_audit_batches(llm_buffer, action_buffer)` run via `await asyncio.to_thread(...)` inside the existing `_flush_lock` scope. Semantics unchanged for every caller — same serialization, same copy-under-`state.lock`, same re-prepend-on-failure; test fakes that patch `DatabaseManager.get_instance` keep working (module attr resolved at call time, thread or no thread).
4. **Lifespan shutdown flush: best-effort and bounded.** Inserted after `framework_task.cancel()`/await and before `close_asyncpg_pool()`: `with suppress(Exception): await asyncio.wait_for(flush_recording_buffers(), timeout=FLUSH_SHUTDOWN_FLUSH_TIMEOUT_SECONDS)` (10 s, constant lives in `audit_recording` so tests can pin it). Ordering rationale: the loop task is already dead (no concurrent P12 flush); flush uses the SQLAlchemy engine, unaffected by the asyncpg pool close, but flush-then-close is kept anyway. Disclosed residual: if the timeout fires mid-insert, the in-flight batch's re-prepend never runs (the coroutine is cancelled) — that is exactly the "at most the unflushed tail" bound the acceptance test pins; a hung shutdown is the worse failure.
5. **`start_show`: flush-first, and the clear becomes conditional (deviation from the spec letter "flush BEFORE clearing", flagged).** An *unconditional* clear after a *failed* flush would re-create the silent-discard bug this unit exists to kill (the flush re-prepends its rows, then the clear deletes them). Since `append_loop_audit` no-ops while `state.current_show_id is None` (guaranteed here — the entry 409 refuses when any show records), a successful flush leaves both buffers empty and the clear is a no-op; rows that survive a failed flush still reference an *existing* show (stop, not delete) and are retained for the next periodic/stop flush, with a warning carrying the count. The CONC-2 no-await property between this point and the `sync_lock` enable is preserved (the flush's awaits all happen before the `state.lock` section). The double-start TOCTOU window can only *add* rows here (retained), never lose them — strictly safer than the old clear.
6. **`delete_show` drops the deleted show's buffered rows AFTER the delete transaction commits.** Dropping *before* the commit risks discarding rows for a show that survives a failed delete (an invariant-4 loss); dropping after leaves only a millisecond-scale crash window (commit → drop) in which a poisoned flush could re-queue forever — disclosed in §6, not fixed (a self-healing flush that isolates FK-offending rows would need per-row inserts on `IntegrityError`; out of scope). The drop itself: `drop_buffered_rows_for_show(show_id) -> int` in `audit_recording` (rebuilds both buffers minus that `show_id` under `state.lock`, no I/O); `delete_show` logs the count. `_teardown_live_recording` is deliberately NOT the site — `archive_show` shares it, and archiving must keep its buffered rows (the show row survives).
7. **Prompt capture: the conductor attaches the exact chat under a transport key; no port signature change.** `ConductorLLMAsync.get_next_state_async` sets `response["_request_messages"] = [{"role": "system", ...}, {"role": "user", ...}]` after `call_async` returns. Rationale: the prompt is built *inside* the conductor (two near-duplicate templates already exist — rebuilding it at audit time would add a third drift source); changing `ConductorPort.get_next_state_async`'s return shape would break every fake conductor across ~10 test modules for zero benefit. Convention (documented in `ports.py`): underscore-prefixed response keys are transport metadata, not model output — the audit layer stores them in dedicated fields and strips them from `parsed_response`, so the persisted response stays schema-pure (`training/dpo_pipeline.validate_conductor_schema` sees an unchanged shape). The pre-generation path carries the key through `_pregen_results` (one line in `run_pregeneration`, one in the `_step_pregen_decision` reconstruction) so both loop paths capture identically. Fallback rows (conductor call raised; `build_fallback_response` has no messages) keep the legacy 5-key context summary — `_audit_prompt_context` stays as the fallback, not dead code.
8. **`prompt_messages` becomes the chat list; legacy rows stay readable.** The model tests already assume this shape (`test_shows_model.py` builds `prompt_messages=[{"role": ...}]`), no frontend renders the field (grep of `static/` is clean), and the column is JSON — content change, no DDL. `to_llm_dump_dict` normalizes: list → use as-is; legacy dict → treat as no chat (assistant-only dump; the unsloth converter inserts the system message). Old rows export degraded-but-valid instead of raising.
9. **`applied_actions` = the post-dedupe enacted stem set with generation outcome.** `parsed_response.actions` is what was *requested*; `process_actions` drops malformed/duplicate/remove-conflicting actions and the empty-decision fallback retains everything — the *applied* set is what actually played. Shape (one entry per enacted stem, aligned 1:1 with `next_stems`): `{sub_family, major_family, model_id, bars, age, outcome}` where `outcome ∈ {"generated", "cached", "failed"}`. Outcomes come from `_step_await_jobs_fetch`, whose return changes `None → dict[int, str]` (`orig_idx → "generated"|"failed"`; stems absent from the map were cache hits → `"cached"` default in the builder). Job-level detail (job ids, durations) is deliberately NOT captured — reward heuristics need the tri-state, not the ids. Structured extras (bars/stem_history snapshots) are intentionally skipped: once the real user prompt is captured, the history is *in* the prompt text; a structured duplicate would double-store and drift.
10. **Transport wiring keeps every existing seam stable.** `_append_loop_audit(conductor_response, active_stems, loop_idx)` (the port-level delegate that ~6 test modules `patch.object`) is untouched; `_step_append_audit` grows two params (`next_stems`, `outcomes`) and attaches `conductor_response["_applied_actions"]` before delegating — `_run_loop` feeds it `local_next_stems` + P8's return on the fresh path, `_pregen_results["next_stems"]` + `_pregen_results["stem_outcomes"]` on the pregen path. `AuditSinkPort.append_loop` signature unchanged.
11. **`to_llm_dump_dict` emits a row the training tools can consume directly.** `{"messages": [system, user, assistant], "response": parsed_response, "meta": {...captured columns...}}` where the assistant turn's content is `json.dumps(parsed_response)` — a JSON *string*, matching `tests/test_dpo_pipeline.py`'s canonical row and what `dpo_pipeline._extract_assistant_message` + `convert_to_unsloth_dataset` expect (today's export puts a bare dict there and a stub dict under `messages`, so the corpus was unusable end-to-end). `meta` carries every remaining captured column (loop_index, relative_time_ms, bpm, key, set_name, instruments, action_type, applied_actions, reasoning, was_fallback, error) — "DPO export contains every captured field". Top-level keys stay `messages`/`response` (+`meta`), so the existing `test_shows_model` assertions (`"reasoning" not in result`, `"error" not in result`) remain true.
12. **Logging style: `print`, matching the brownfield modules touched** (`audit_recording`, `loop_steps`, `shows` are print-based; REL-27/U14 sweeps prints later). Counts are always included so the deliberate drop/retain events are observable.

---

## 2. Exact changes per file

### 2.1 `app/framework/framework_conductor_async.py` (473 → ~482 lines)
`get_next_state_async` tail (decision 7):
```python
        # Call LLM async
        response = await self.call_async(user_prompt, llm_config, extra_body=extra_body)
        # U4/REL-04: attach the exact chat sent to the LLM so the audit trail
        # can persist the real user turn — the corpus previously captured only
        # a 5-key context summary, leaving llm-dump user turns near-empty.
        # Underscore-prefixed keys are transport metadata: audit stores them
        # in dedicated fields and strips them from parsed_response, so the
        # persisted schema stays model-output-only.
        response["_request_messages"] = [
            {"role": "system", "content": self.system_instruction},
            {"role": "user", "content": user_prompt},
        ]
        return response
```

### 2.2 `app/framework/audit_recording.py` (234 → ~300 lines)
**(a)** Module constant: `FLUSH_SHUTDOWN_FLUSH_TIMEOUT_SECONDS = 10.0` (consumed by app_ui; monkeypatchable seam).
**(b)** `flush_recording_buffers`: extract the DB block into sync `_insert_audit_batches(llm_buffer, action_buffer)` and `await asyncio.to_thread(_insert_audit_batches, llm_buffer, action_buffer)` (decision 3). Copy/clear under `state.lock`, re-prepend-on-failure, `_flush_lock` scope, empty-check — all unchanged. Docstring gains the to_thread rationale.
**(c)** New `drop_buffered_rows_for_show(show_id: int) -> int` (decision 6):
```python
async def drop_buffered_rows_for_show(show_id: int) -> int:
    """Deliberately drop buffered audit rows referencing a deleted show (REL-14).

    The show row (and its audit history, by cascade) is gone, so these rows
    can never insert — they would FK-fail every future flush, and the failed
    batch re-prepends, poisoning flushes until restart. Caller logs the count
    (invariant 4: the drop must be loud, never silent). Lock section is pure
    list filtering — no I/O.
    """
    async with state.lock:
        llm_keep = [r for r in state.llm_interaction_buffer if r.get("show_id") != show_id]
        act_keep = [r for r in state.action_buffer if r.get("show_id") != show_id]
        dropped = (len(state.llm_interaction_buffer) - len(llm_keep)) + (
            len(state.action_buffer) - len(act_keep)
        )
        state.llm_interaction_buffer = llm_keep
        state.action_buffer = act_keep
    return dropped
```
Awaited by `delete_show` (decision 6).
**(d)** New pure `_audit_applied_actions(next_stems, outcomes) -> list[dict[str, Any]]` (decision 9): one row per stem from `s.get("_original_details", {})` + `s` — `{sub_family, major_family, model_id, bars, age, outcome: outcomes.get(i, "cached")}`.
**(e)** `append_loop_audit`: extract row-building into `_audit_interaction_row(show_id, loop_idx, ts, relative_ms, conductor_response, active_stems)` (keeps functions ≤20 lines) which:
```python
    request_messages = conductor_response.get("_request_messages")
    applied = conductor_response.get("_applied_actions")
    # Transport keys never persist as model output (ports.py convention).
    parsed = {k: v for k, v in conductor_response.items() if not k.startswith("_")}
    ...
    "prompt_messages": request_messages if request_messages else _audit_prompt_context(...),
    "parsed_response": parsed,
    "applied_actions": applied,
```
**(f)** `AuditAdapter` docstring: `flush` is now wired into the loop (P12 threshold) — drop the "will be wired in U4" note.

### 2.3 `app/framework/loop_steps.py` (923 → ~955 lines)
**(a)** Constant beside the STEM_CACHE block: `AUDIT_FLUSH_THRESHOLD_ROWS = 200` with the REL-04 comment.
**(b)** `_step_await_jobs_fetch` returns `dict[int, str]`: initialize `outcomes: dict[int, str] = {}`; in the result loop set `outcomes[orig_idx] = "generated"` when audio fetched, else `"failed"` (the existing `print(f"Job {job_id} failed...")` branch); return it. Docstring updated.
**(c)** `_step_append_audit(self, conductor_response, active_stems, next_stems, outcomes)`:
```python
        # U4: the post-dedupe enacted stems + outcomes ride the response dict
        # under a transport key so the port-level _append_loop_audit signature
        # (patched across the test suite) stays unchanged.
        conductor_response["_applied_actions"] = _audit_applied_actions(next_stems, outcomes)
        await self._append_loop_audit(conductor_response, active_stems, self._loop_idx)
```
**(d)** `_run_loop`: before the `if not pregen_ready:` block, seed `audit_stems`/`audit_outcomes` from `self._pregen_results` (`next_stems`, `stem_outcomes`); inside the fresh block capture `stem_outcomes = await self._step_await_jobs_fetch(...)` and set `audit_stems, audit_outcomes = local_next_stems, stem_outcomes`; call `await self._step_append_audit(conductor_response, snap.active_stems, audit_stems, audit_outcomes)`.
**(e)** `_step_post_commit` tail (after the pregen if/elif/else, decision 2):
```python
        # REL-04: keep the in-RAM audit buffers bounded — flush past the row
        # threshold instead of holding the whole show until stop_show. Routed
        # through the audit port; the module flush serializes on _flush_lock
        # and re-queues its rows on failure, so the loop just retries next
        # iteration. len() is a GIL-atomic read; the mixer thread is never
        # involved (this is the async loop task, post-commit).
        try:
            if len(state.llm_interaction_buffer) > AUDIT_FLUSH_THRESHOLD_ROWS:
                await self._audit.flush()
        except Exception as e:  # noqa: BLE001  # flush re-queues internally; guard is belt-and-braces
            print(f"[AsyncLoop-{self._loop_idx}] Audit flush failed (will retry next loop): {e}")
```
**(f)** `_step_pregen_decision` reconstruction adds `"_request_messages": self._pregen_results.get("_request_messages")` to the rebuilt `conductor_response` dict.

### 2.4 `app/framework/pregeneration.py` (167 → ~178 lines)
In `run_pregeneration`: track `stem_outcomes` in the pending-results loop (same tri-state as the foreground); store both new keys in `loop._pregen_results`: `"_request_messages": conductor_response.get("_request_messages")`, `"stem_outcomes": stem_outcomes`.

### 2.5 `app/framework/ports.py`
- `AuditSinkPort` docstring: replace "the loop never calls flush — zero behavior change; dual-ownership preserved" with the U4 contract: the loop calls `flush` from P12 past `AUDIT_FLUSH_THRESHOLD_ROWS`; routes keep the stop/shutdown flushes; both paths share the module `_flush_lock`, so ownership stays dual but serialized.
- `ConductorPort` docstring: document the `_request_messages` transport-key convention (implementations MAY attach it; consumers must not treat it as model output).

### 2.6 `app/routes/shows.py` (737 → ~755 lines)
**(a)** Module-top import: `from app.framework.audit_recording import drop_buffered_rows_for_show, flush_recording_buffers` (audit_recording imports only `framework_state` at module level — no cycle); delete the local import in `stop_show` (behavior unchanged).
**(b)** `start_show` — replace the clear block (shows.py:390-399) per decision 5:
```python
    # REL-14 (amended, U4): persist the previous show's pending rows instead of
    # silently discarding them — the buffers are the fine-tuning corpus
    # (invariant 4). Buffered rows carry their own show_id, so they flush even
    # though current_show_id is still unset here. A successful flush empties
    # both buffers (append_loop_audit no-ops while no show records), so the old
    # clear is a no-op on success; rows surviving a FAILED flush were
    # re-queued by the flush itself and still reference an existing show, so
    # they are retained for the next periodic/stop flush, never dropped.
    # No await sits between this and the sync_lock enable below (CONC-2).
    await flush_recording_buffers()
    async with state.lock:
        retained = len(state.llm_interaction_buffer) + len(state.action_buffer)
    if retained:
        print(
            f"start_show: retaining {retained} buffered audit rows after a failed flush "
            f"(they will persist on the next flush)"
        )
```
**(c)** `delete_show` — after the `with db_manager.session()` block (delete committed), before returning 204 (decision 6):
```python
    # REL-14: buffered rows still referencing the deleted show can never
    # insert and would FK-poison every future flush (the failed batch
    # re-prepends). Deliberately drop ONLY this show's rows, loudly.
    dropped = await drop_buffered_rows_for_show(show_id)
    if dropped:
        print(f"delete_show {show_id}: dropped {dropped} buffered audit rows referencing the deleted show")
```

### 2.7 `app/app_ui.py` (769 → ~782 lines)
Lifespan shutdown, between the framework-task await and `close_asyncpg_pool` (decision 4):
```python
    # REL-04: last-chance audit flush so shutdown doesn't lose the unflushed
    # tail of the capture buffers (invariant 4). Best-effort and bounded: a DB
    # unreachable within the timeout costs at most the unflushed tail
    # (<= threshold + one loop's rows), never a hung shutdown.
    from app.framework.audit_recording import FLUSH_SHUTDOWN_FLUSH_TIMEOUT_SECONDS, flush_recording_buffers

    with suppress(Exception):
        await asyncio.wait_for(flush_recording_buffers(), timeout=FLUSH_SHUTDOWN_FLUSH_TIMEOUT_SECONDS)
```

### 2.8 `app/models/llm_interaction.py` (78 → ~100 lines)
**(a)** `import json`; **(b)** new column after `parsed_response`:
```python
    # U4 (REL-04 + DPO field audit): the post-dedupe stem set actually enacted
    # this loop, with per-stem outcome ("generated" | "cached" | "failed") —
    # distinct from parsed_response.actions (requested). Additive; see
    # migrations/003_llm_capture_additive.sql for existing deployments.
    applied_actions = Column(JSON, nullable=True)
```
**(c)** `to_dict` += `"applied_actions": self.applied_actions`.
**(d)** `to_llm_dump_dict` rewrite (decision 11):
```python
    def to_llm_dump_dict(self):
        """Training-corpus row (U4): full chat + response + capture metadata.

        messages follows the {role, content} chat shape the unsloth converter
        and dpo_pipeline consume; the assistant turn carries the response as a
        JSON string (their canonical row format). Legacy rows whose
        prompt_messages is the old context-summary dict degrade to an
        assistant-only row instead of raising.
        """
        pm = self.prompt_messages
        chat = list(pm) if isinstance(pm, list) else []
        if self.parsed_response:
            chat = chat + [{"role": "assistant", "content": json.dumps(self.parsed_response)}]
        result: dict = {"messages": chat}
        if self.parsed_response:
            result["response"] = self.parsed_response
        result["meta"] = {
            "loop_index": self.loop_index,
            "relative_time_ms": self.relative_time_ms,
            "bpm": self.bpm,
            "key": self.key,
            "set_name": self.set_name,
            "instruments": self.instruments,
            "action_type": self.action_type,
            "applied_actions": self.applied_actions,
            "reasoning": self.reasoning,
            "was_fallback": self.was_fallback,
            "error": self.error,
        }
        return result
```

### 2.9 `migrations/003_llm_capture_additive.sql` (new)
Header mirrors 001/002 (comment banner, idempotence note, `psql "$DATABASE_URL" -f` line). Body: `DO $$ ... ALTER TABLE llm_interactions ADD COLUMN applied_actions JSON ... $$` guarded on `information_schema.columns` (002 pattern). Closing comment notes: (1) `prompt_messages` content-shape change is app-level (JSON column, no DDL); (2) fresh installs get the column via `Base.metadata.create_all` — this migration is for existing PG deployments, where `create_all` does NOT add columns.

### 2.10 Docs (pipeline "docs" stage, same unit)
- `docs/reliability_audit.md`: `**Status: fixed-in rel-04-capture** — ...` one-paragraph notes on REL-04 (threshold flush via audit port at P12 + shutdown flush + start_show flush-before-clear + delete-path drop) and REL-14 (amended finding text already reflects the silent-discard bug; note the delete-drop + start-retain fixes and the residual crash window).
- `refactor/plans/rel-remediation-plan.md`: status row 4 → landed (commit filled at merge).

---

## 3. TDD regression tests (write first, confirm red, then implement)

### 3.1 New file `tests/test_llm_capture.py` (~380 lines) — the unit's acceptance suite
Fixtures: `os.environ["DATABASE_URL"] = ""` (SQLite) + `DatabaseManager.get_instance().create_tables()` (real-session rows, `test_shows_api.py` pattern) for the route/flush-roundtrip tests; `_reset_audit_state` autouse (save/clear buffers + show flags, `test_loop_fixes.py` pattern); `_FakeMixer` + instant-sleep `_run_loop` driver (`test_loop_fixes.py::test_run_loop_retries_after_transient_exception` pattern) for loop-cadence tests; `_FakeSession`/`_make_fake_db` (`test_framework_characterization.py` Gap 8 pattern) where a failing DB is needed.

| # | Test | Pins | Core assertions |
|---|---|---|---|
| T1 | `test_post_commit_flushes_past_threshold_via_audit_port` (**RED: no flush call**) | REL-04 wiring | ctor-inject spy `AuditSinkPort` fake; seed buffer with 201 stub rows → `_step_post_commit(commit, [], 0)` → `fake.flush` awaited once; with 200 rows → not called; with 201 rows but `fake.flush` raising → no exception escapes. |
| T2 | `test_flush_failure_requeues_without_loss` (**task list**) | re-queue | Real buffers + fake DB whose commit raises: seed 205 llm + 60 action rows → flush → all 265 rows still buffered, ORDER preserved (re-prepended before any newer append); append 1 newer row; swap in working fake DB → flush → all 266 rows in `bulk_insert_mappings`, buffers empty. |
| T3 | `test_buffer_bounded_across_loops` (**acceptance**) | REL-04 bound | Drive `_run_loop` (fake mixer/conductor/jobs/audio, instant sleeps, fake DB capturing bulk calls, threshold monkeypatched to 3); conductor flips `loop.running = False` after N=12 iterations; assert: DB received exactly N interaction rows total; every flushed llm batch ≤ threshold + 2; final `len(state.llm_interaction_buffer)` ≤ threshold + 2. |
| T4 | `test_crash_loses_at_most_unflushed_tail` (**acceptance**) | REL-04 durability | Same harness; after K loops `task.cancel()` + suppress CancelledError ("crash"); assert `rows_in_db ≥ loops_completed − (threshold + 1)` and `rows_in_db + len(buffer) == loops_completed` (nothing else lost). |
| T5 | `test_start_show_flushes_before_clearing_no_silent_discard` (**acceptance, REL-14**) | start path | Real SQLite; shows A (ended) + B (draft) rows; mock `get_current_user_from_request`; seed buffers with 5 A-rows; POST `/api/shows/{B}/start` → 201; assert `LLMInteraction` count for A == 5 in DB and both buffers empty. |
| T5b | `test_start_show_retains_rows_when_flush_fails` | decision 5 | Same, but `DatabaseManager.get_instance` patched to a raising fake (monkeypatch `app.db`); POST start → 201; buffers still hold the 5 A-rows (NOT discarded); warning printed (capsys contains "retaining 5"). |
| T6 | `test_delete_live_show_drops_only_its_buffered_rows` (**REL-14**) | delete path | Real SQLite; show A live (state flags set); buffers seeded with 4 A-rows + 3 B-rows; DELETE `/api/shows/{A}` → 204; buffers contain exactly the 3 B-rows; print contains "dropped 4". |
| T7 | `test_delete_live_show_then_more_loops_and_flush_succeeds` (**acceptance**) | REL-14 end-state | Continue from T6: set `state.current_show_id = B`, run 3 `append_loop_audit` calls, `await flush_recording_buffers()` → no exception; DB has 3+3 B-rows, zero A-rows. |
| T8 | `test_shutdown_lifespan_flushes_audit_tail_bounded` | decision 4 | Monkeypatch `app.framework.audit_recording.flush_recording_buffers` → AsyncMock + `run_framework_loop_async`/garage mocks (`test_app_ui.py` pattern); `with TestClient(app): pass` → flush awaited once. Variant: AsyncMock side_effect `RuntimeError` (or a never-returning coroutine with timeout=0.05 monkeypatched) → context exit still completes. |
| T9 | `test_conductor_attaches_exact_request_messages` | decision 7 | `ConductorLLMAsync` with mocked `_get_async_client` (`test_simulation.py` pattern); `get_next_state_async(...)` → returned dict has `_request_messages` = [system, user] with user content containing "Master BPM: 128", the stems block, "OVERRIDE:" when passed. |
| T10 | `test_append_loop_audit_stores_chat_strips_transport_keys` | decisions 7/9 | Response carrying `_request_messages` + `_applied_actions` → buffered row: `prompt_messages` IS the chat list; `parsed_response` has NO underscore keys; `applied_actions` stored verbatim. Response without transport keys (fallback/fakes) → `prompt_messages` is the legacy context dict, `applied_actions` None. |
| T11 | `test_flush_roundtrips_all_captured_fields_sqlite` | capture fidelity | Real SQLite; set show flags; `append_loop_audit` with chat + applied; flush; query `LLMInteraction` → every column populated (`applied_actions` JSON list, `prompt_messages` list of 2 messages, bpm/key/set_name/instruments/action_type rollup). |
| T12 | `test_llm_dump_contains_every_captured_field_and_validates` (**acceptance, DPO**) | decision 11 | Row from T11 → `to_llm_dump_dict()`: `messages` roles == [system, user, assistant]; assistant content `json.loads` → `training.dpo_pipeline.validate_conductor_schema(...) is True`; `meta` has all 11 captured fields; `response` == parsed_response. |
| T13 | `test_llm_dump_tolerates_legacy_prompt_messages` | decision 8 | Row with old stub-dict `prompt_messages` → dump messages == [assistant] only; no raise. |
| T14 | `test_await_jobs_fetch_returns_stem_outcomes` | decision 9 | Two pending jobs (one audio path + fetch ok, one None) + one cache-hit stem → returns `{0: "generated", 2: "failed"}`; `_audit_applied_actions` marks the missing index "cached". |
| T15 | `test_pregen_results_carry_capture_fields` | decision 7/9 | `run_pregeneration` with fake conductor returning `_request_messages` → `_pregen_results` has `_request_messages` + `stem_outcomes`; `_step_pregen_decision`-shaped reconstruction feeds `_step_append_audit` → row carries the chat (not the fallback stub). |
| T16 | `test_stop_show_flush_persists_remaining_rows` | keep-green pin | Start → append 2 loops → POST stop → DB has both rows (existing stop-flush behavior, now alongside the threshold path). |

### 3.2 Keep-green updates (existing tests, minimal edits)
- `tests/test_loop_fixes.py`: `_LLM_INTERACTION_COLS` += `"applied_actions"` (T10's fallback path keeps the rest of `test_append_loop_audit_populates_buffers` green).
- `tests/test_shows_model.py`: `to_llm_dump_dict` tests stay green (top-level keys unchanged, `"reasoning"`/`"error"` still absent at top level); extend the plain test with a `meta` presence assertion if trivial — otherwise leave.
- `tests/test_async_framework.py::test_pregen_results_structure` pins a subset (`field in pregen_results`) — new keys safe, no edit.
- `tests/test_framework_characterization.py` Gap 8 (flush success/failure): green under to_thread (fakes patch `DatabaseManager.get_instance`; the closure resolves them from either thread).
- Sweep set to run explicitly: `test_loop_fixes.py`, `test_framework_characterization.py`, `test_audit_injection.py`, `test_async_framework.py`, `test_shows_api.py`, `test_shows_model.py`, `test_app_ui.py`, `test_simulation.py`, `test_dpo_pipeline.py`, `test_slop_models_exports.py`, `test_frozen_api.py`, `test_state.py`.

### 3.3 TDD order
1. Write `tests/test_llm_capture.py` → run → **red** (T1 no threshold flush; T2 order-preservation holds but T5b discards today — the start_show clear deletes the rows; T6 leaves A-rows behind; T9 no `_request_messages`; T10 stores stub prompt_messages + no applied_actions; T12 dump lacks assistant/meta; T14 P8 returns None).
2. Implement §2.1 + §2.2 (conductor attach; audit row/drop/to_thread) → T2/T9/T10/T13 green.
3. Implement §2.3 + §2.4 + §2.5 (loop wiring + pregen carry + port docs) → T1/T3/T4/T14/T15 green.
4. Implement §2.6 + §2.7 (routes + lifespan) → T5/T5b/T6/T7/T8/T16 green.
5. Implement §2.8 + §2.9 + §2.10 (model + migration + docs) → T11/T12 green; update §3.2 pins.
6. Full gate: `.venv/bin/python -m ruff check app tests && .venv/bin/python -m pytest tests/ -q` → **968 + ~16 new passed / 16 skipped**, zero regressions.

---

## 4. Invariant compliance (plan §Invariants)

1. **Lock discipline:** every new `state.lock` section is copy/filter/len only — no I/O (drop helper: list comprehensions; start_show retain check: two `len()`s; flush: unchanged copy/clear + re-prepend). The flush's DB I/O moves OFF the event loop entirely (to_thread). No framework function is called while holding `state.lock`. `sync_lock` untouched.
2. **Hexagonal + style:** the new flush trigger routes through `AuditSinkPort` (core testable with the existing fake); `_audit_applied_actions`/`_audit_interaction_row`/`_insert_audit_batches`/`drop_buffered_rows_for_show` are small single-purpose functions; no `Any` additions beyond existing signatures; files stay <500 lines except the pre-existing brownfield debts (`loop_steps.py` 923→~955, `shows.py` 737→~755, `app_ui.py` 769→~782 — same disclosed-debt precedent as rel-02/rel-03).
3. **Audio path:** nothing new runs on the mixer thread; the threshold flush is on the async loop task post-commit, and its DB I/O is threaded off-loop.
4. **LLM capture (the unit's subject):** no path silently drops rows — start_show flushes then retains-on-failure; delete drops loudly and only the undeletable rows; flush failure re-queues; shutdown flush is best-effort-bounded (documented tail bound); no truncation is added (existing `[:1000]`/`[:500]` clamps untouched; chat + applied_actions stored whole); exports round-trip (T12 pins the DPO consumer contract).
5. **Worker/restart semantics:** untouched.
6. **Regression tests:** every fix above is pinned (T1–T16 map 1:1 to the acceptance bullets).

---

## 5. Acceptance checklist (maps to §U4 spec + task list)

- [ ] Buffer stays bounded across N loops — T3 (threshold flush via audit port; batch ≤ threshold + per-loop growth).
- [ ] Crash-mid-show loses at most the unflushed tail — T4 (+ shutdown bound T8, decision 4 residual).
- [ ] `start_show` flushes before clearing, no silent discard — T5/T5b.
- [ ] Delete of a live show followed by more loops + flush succeeds — T6/T7.
- [ ] DPO export contains every captured field — T12 (chat incl. system+user+assistant-JSON-string, `meta` with all captured columns; `validate_conductor_schema` passes on the assistant turn).
- [ ] Flush failure re-queues without loss, loop survives — T2/T1(variant).
- [ ] REL-04 letter: threshold `> 200` from `_step_post_commit` via lock-serialized flush, off the audio path — T1/T3.
- [ ] Additive schema (migration + model + write path + export) SQLite/PG compatible — T11/T13 + §2.9.

---

## 6. Risks / out of scope / residuals

- **Shutdown-timeout tail loss (decision 4):** a flush cancelled by the 10 s timeout mid-insert cannot re-prepend; worst case one batch (≤ threshold + one loop's rows). Bounded shutdown was the task requirement; documented here and in the code comment.
- **delete_show crash window (decision 6):** process death between the delete commit and the buffer drop (milliseconds) leaves FK-poisoned rows that fail flushes until restart. A self-healing flush (per-row isolation on IntegrityError) was considered and rejected as scope creep.
- **DB growth from full-chat capture:** ~4–6 KB/row (system+user prompt) vs ~1 KB today ≈ +100 MB/week per 24/7 show. Invariant 4 mandates capture; retention is U5 (opt-in, default keep).
- **`prompt_messages` shape change** is content-only (JSON column) but old rows export assistant-only via llm-dump — acceptable degradation, pinned by T13; the reasoning-log viewer (`to_reasoning_export_dict`) never read the field; no frontend consumer (grep clean).
- **Existing dev SQLite files** won't gain `applied_actions` from `create_all` (columns are not ALTERed); tests create fresh schemas. PG deployments run migration 003. Noted in the migration header.
- **Fallback rows** (conductor call failed) keep the legacy context-summary `prompt_messages` — the request prompt died inside the exception; capturing it would require hoisting prompt construction out of the conductor (out of scope, disclosed).
- **Out of scope:** REL-13 export unbounded `.all()` (unit 11), REL-16 retention (unit 5), REL-09 off-loop DB for routes (unit 7 — this unit only threads the flush's own I/O), per-action job ids in `applied_actions`, stem-history as structured JSON (it is in the captured user prompt).
- **Pre-existing print logging** in touched modules is matched, not converted (REL-27/U14 owns that sweep).

## 7. Validation commands

```bash
.venv/bin/python -m ruff check app tests
.venv/bin/python -m pytest tests/test_llm_capture.py -q          # new suite
.venv/bin/python -m pytest tests/ -q                              # full gate
psql "$DATABASE_URL" -f migrations/003_llm_capture_additive.sql  # deploy step (idempotent)
```
