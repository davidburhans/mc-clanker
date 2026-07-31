# Round 1 — Claim Verification

Agent: `claim-verifier` (fresh context, git-analyzer tier + bash for `git show`).
Method: every premise verified against actual source at baseline commit `906a49b`.
Note: agent lacked a file-write tool → returned inline; persisted here by orchestrator.

## Per-premise verdicts

| # | Premise | Verdict | Evidence (file:line) |
| --- | --- | --- | --- |
| 1 | P11 state-commit is ONE `async with state.lock:` block; `record_loop_transition` OUTSIDE the lock | **Verified** | lock block `framework_main_async.py:616`→`:688`; `record_loop_transition(1,…)` at `:692` (16-space indent, outside lock) under `if needs_initial_record:` `:691`; comment `:611-613` states intent. Second call site `:735` also snapshots `:731` then calls outside (comment `:728-730`). Cited range 608–698/~700 off by ≤8 lines; structural fact exact. |
| 2 | `AsyncFrameworkLoop.__init__` takes ONLY `(self, session_id)` | **Verified** | `:191` `def __init__(self, session_id: uuid.UUID):` — exactly two params. ⇒ Phase 7b port-injection IS a true signature change. |
| 3 | Cache-stem divergence: foreground calls `state.cache_stem` (L527), background does NOT | **Verified** | foreground writes `self.stem_cache[cache_key]` `:523`, then under `async with state.lock:` `:526` calls `state.cache_stem(...)` `:527`. Background `_pre_generate_next_loop` (`:979`) writes ONLY `self.stem_cache[cache_key]` `:1096`, never `cache_stem` (grep: only call site is `:527`). Cited bg line ~1155 was wrong — `:1155` is `run_framework_loop_async`; actual is `:1096`. |
| 4 | `decode_aac` + `create_garage_client_from_env` are module-level imports (string-patchable) | **Verified** | `:25` `from app.garage_client import create_garage_client_from_env`; `:26` `from app.aac_encoder import decode_aac`. Tests patch exactly that path (`test_worker_fetch_audio.py:43,44`). |
| 5 | `process_actions` uses key `action_type` (not `action`); mutates `_original_details["_age"]` in place | **Verified** | `:132` `a_type = action.get("action_type")`; `:138` `orig = s.get("_original_details", {})`; `:139` `orig["_age"] = s.get("_age", 0) + 1`. (Mutation at `:139`, not ~131-132.) |
| 6 | Frozen public API = the 9 named symbols, all module-level | **Verified** | `calc_duration :47`, `_to_two_channel :58`, `flush_recording_buffers :74`, `process_actions :118`, `AsyncFrameworkLoop :182`, `run_framework_loop_async :1155`, `_flush_lock :69`, `create_garage_client_from_env :25`, `decode_aac :26`. Only other module-level names: private consts `DEFAULT_FALLBACK_BPM :40`, `LOOP_RETRY_BACKOFF_SECONDS :44` (zero external importers). No `__all__`. |
| 7 | `run_framework_loop_async(session_id: uuid.UUID)` is sole prod entry | **Verified** | `:1155` signature. `app/app_ui.py:14` import, `:90` `create_task(run_framework_loop_async(app_session_id))` (only create_task). Other refs = tests + docstring + `__main__` demo. |
| 8 | `pyproject.toml` has NO `[tool.ruff.lint]`; `asyncio_mode = "auto"` | **Verified** | `[tool.ruff]` line-length=120, target-version=py310 only; no lint table. `[tool.pytest.ini_options]` asyncio_mode="auto". |
| 9 | GlobalState has 65 `self.*` attrs; the 3 ModelMgmt attrs are truly dead | **Verified** | `framework_state.py:72-192` assigns exactly 65 distinct attrs. `model_states :179`, `model_errors :180`, `download_progress :181` — repo-wide grep for reads returns ONLY the assignments (the `framework_generator.py`/`test_generator.py` hits are on a SEPARATE `GeneratorRegistry` class). |
| 10 | Baseline = 542 passed | **Verified** | `adversarial_review/00_FINAL_REPORT.md:75` → 542 passed · 58 skipped · 9 xfailed · 16 xpassed · 0 failed. Not re-run (verifier cannot run pytest). |

## Final tally: **10/10 Verified · 0 Weakened · 0 Falsified**

Line-anchor drift (all ≤8 lines, mechanisms reproduce exactly): premises 1 (616–688 vs 608–698), 3 (L1096 vs ~L1155), 5 (`:139` vs ~131-132). All corrections folded into `refactor/plan.md` (amendment A14).
