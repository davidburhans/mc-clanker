# Gap Analysis — slop_harness + mc-clanker

**Task:** t_db57e065 — Analyze test coverage gaps, missing edge cases, and integration points.
**Date:** 2026-06-26
**Codebase:** /mnt/c/slop/mc-clanker-feat-slop-harness (branch feat/slop-harness)

---

## 1. Coverage Summary

### slop_harness/ — 69% overall (149 tests)

| Module | Stmts | Miss | Cover | Verdict |
|--------|-------|------|-------|---------|
| __init__.py | 0 | 0 | 100% | OK |
| checkpoint.py | 20 | 0 | 100% | OK |
| dataset_writer.py | 41 | 2 | 95% | OK (missed: error paths for file I/O exceptions) |
| harness.py | 102 | 72 | **29%** | CRITICAL GAP |
| llm_client.py | 46 | 6 | 87% | Moderate gap |
| models.py | 10 | 0 | 100% | OK |
| prompt_builder.py | 47 | 1 | 98% | OK |
| quality/__init__.py | 0 | 0 | 100% | OK |
| quality/cli.py | 130 | 130 | **0%** | CRITICAL GAP |
| quality/schemas.py | 12 | 0 | 100% | OK |
| quality/validator.py | 385 | 52 | 86% | Moderate gap |
| state_generator.py | 57 | 1 | 98% | OK |
| vibe_prompt_bank.py | 14 | 0 | 100% | OK |

### Broader mc-clanker app — broken tests (pre-existing)

| Test File | Status | Root Cause |
|-----------|--------|------------|
| test_api.py | COLLECTION ERROR | `auth.py` imports `from models import User` but `slop_harness/models.py` shadows `models/` package |
| test_generator.py | COLLECTION ERROR | `stable_audio_tools` not installed |
| test_auth.py | 21 failures | Same `models` shadowing issue |
| test_shows.py | 27 failures, 21 errors | Import chain breaks from `models` shadowing |
| test_framework_main.py | 12 failures | Pre-existing logic bugs (calc_duration, flush_buffers) |

---

## 2. Critical Gaps (slop_harness)

### 2.1 harness.py — 29% coverage (72 lines untested)

**Untested lines:** 84-106, 118-150, 155-205, 209-214, 218

**What's missing:**
- `generate_one()` — no unit test for single interaction generation (lines 84-106)
  - Test: mock LLMClient, verify correct seed → state → prompt → call → record flow
  - Test: vibe override path (rng < vibe_prob)
  - Test: LLM failure returns None
- `run_batch()` — no unit test for batch orchestration (lines 118-150)
  - Test: semaphore limits concurrency
  - Test: mixed success/failure in results
  - Test: exception handling in gather
- `main_async()` — no unit test for the async entrypoint (lines 155-205)
  - Test: checkpoint resume (start_batch > 0)
  - Test: batch completion loop with mock writer/checkpoint
  - Test: vibe_prob=0 never triggers override
  - Test: total interactions limit enforcement
- `main()` — no unit test for CLI arg parsing + KeyboardInterrupt (lines 209-214, 218)
  - Test: parse_args defaults
  - Test: KeyboardInterrupt graceful exit

### 2.2 quality/cli.py — 0% coverage (130 lines, 246 lines total)

**Entire module has zero tests.** Functions: `parse_args()`, `collect_paths()`, `load_records()`, `run_checks()`, `print_report()`, `main()`

**What's missing:**
- `parse_args()` — test all CLI flags and defaults
- `collect_paths()` — test directory globbing, mixed file+dir paths, non-existent paths
- `load_records()` — test JSONL parsing, malformed JSON lines, sample limit, empty files
- `run_checks()` — test threshold violations (duplicate_pct, schema_error_pct, action_error_pct, bpm_entropy, key_coverage)
- `print_report()` — test PASS vs FAIL output formatting, error truncation at 10 items
- `main()` — test exit codes (0=pass, 1=fail, 2=no files), --json output, no paths error

### 2.3 quality/validator.py — 86% coverage (52 lines untested)

**Untested lines:** 36-42, 62-63, 134-141, 174, 179, 185, 204-205, 209-210, 252, 267, 295, 319, 328, 332-333, 337, 346, 352, 356, 421, 428, 432-433, 445, 454, 468, 568, 575, 579-580, 584, 601, 673, 695-696, 699-700

**Mapped to functions (line ranges in validator.py):**
- `validate_action()` — lines 36-42: action is not a dict
- `validate_action()` — lines 62-63: action_type is None (already tested as missing, but not None)
- `validate_conductor_response()` — lines 134-141: empty reasoning string, empty name string
- `validate_action()` — lines 174, 179, 185: invalid major_family, invalid notation_tag, invalid fx_tag
- `validate_action()` — lines 204-205, 209-210: timbre_tags not a list, empty timbre_tags
- `validate_action_indices()` — line 252: stem_index is None
- `validate_action_indices()` — line 267: stem_index not int
- `validate_action_indices()` — line 295: active_stems_counts provided but record has no messages
- `compute_diversity_metrics()` — lines 319, 328, 332-333, 337, 346, 352, 356: records with empty/missing content, non-dict records
- `validate_vibe_persistence()` — lines 421, 428, 432-433, 445, 454, 468: edge cases with empty actions, non-dict records
- `validate_dataset()` — lines 568, 575, 579-580, 584: vibe_overrides with out-of-range indices, empty records list
- `validate_dataset()` — line 601: all_failing computation with overlapping error sets
- `QualityReport` — lines 673, 695-696, 699-700: mark_failed with specific reason strings, passed property when invalid_records=0 but _passed=False

### 2.4 llm_client.py — 87% coverage (6 lines untested)

**Untested lines:** 32-33, 77-78, 98, 101

- Lines 32-33: base_url/model env var fallback when both base_url and model are None
- Lines 77-78: HTTP non-200/429/503 status raises (only 429/503 tested)
- Line 98: successful call returns content correctly (tested implicitly but not in isolation)
- Line 101: max_retries=0 path (no retry, immediate raise)

---

## 3. Missing Edge Cases (Tested Modules)

### 3.1 state_generator.py
- No test for `_sub_families_for_major()` when major_family has empty sub_families list
- No test for deterministic history generation (same seed → same history)
- No test for stem age distribution (always >= base_age)

### 3.2 prompt_builder.py
- No test for `_format_stems()` with empty stems list
- No test for `_format_history()` with entries missing "stems" key
- No test for `_format_models()` with unknown model IDs (not in MODEL_REPO_IDS)
- No test for `_density_directive()` at boundaries (stem_count=3, stem_count=4, stem_count=6, stem_count=7)

### 3.3 dataset_writer.py
- No test for concurrent writes (thread safety claim)
- No test for write after close (should not crash)
- No test for close() when no writes happened (file is None)

### 3.4 checkpoint.py
- No test for corrupted checkpoint file (invalid JSON)
- No test for missing parent directory
- No test for atomic write failure (temp file exists from crashed write)

### 3.5 vibe_prompt_bank.py
- No test for template content quality (non-empty strings)
- No test that sample() returns different values with different RNG seeds
- No test for thread safety of singleton

---

## 4. Integration Point Gaps

### 4.1 harness → quality validation pipeline
- No test that validates records produced by `generate_one()` pass `validate_dataset()`
- No test for the full flow: state → prompt → mock LLM response → validate

### 4.2 CLI → validator integration
- No test for `python -m slop_harness.quality.cli` end-to-end with real JSONL files
- No test for exit code behavior (0/1/2) based on threshold checks

### 4.3 Cross-module: state_generator → prompt_builder → validator
- No test that state_generator output fields appear correctly in prompt_builder output
- No test that prompt_builder output contains all state information needed for validation

---

## 5. Pre-existing Broken Tests (Broader Codebase)

These are NOT in scope for slop_harness but block the test suite:

### Root Cause: `models` package shadowing
- `slop_harness/models.py` is on Python path and shadows `models/` package
- `auth.py` does `from models import User` → resolves to slop_harness/models.py (no User class)
- **Fix:** Rename `slop_harness/models.py` to `slop_harness/model_defs.py` or add `sys.path` manipulation

### Missing dependency
- `stable_audio_tools` not installed → test_generator.py collection error

### Pre-existing logic bugs
- `test_framework_main.py`: 12 failures in calc_duration and flush_buffers logic

---

## 6. Priority Recommendations

### P0 — Fix broken test infrastructure
1. Resolve `models` package shadowing (rename slop_harness/models.py or fix imports)
2. Install stable_audio_tools or mock it in test_generator.py

### P1 — Fill critical coverage gaps
1. harness.py tests (29% → target 80%+) — mock LLMClient, test generate_one/run_batch/main_async
2. quality/cli.py tests (0% → target 90%+) — test all CLI functions, exit codes, JSON output
3. quality/validator.py edge cases (86% → target 95%+) — empty strings, None values, boundary conditions

### P2 — Improve moderate coverage
1. llm_client.py (87% → target 95%) — env var fallback, HTTP error codes, max_retries edge cases
2. dataset_writer.py thread safety test
3. checkpoint.py error path tests

### P3 — Integration tests
1. End-to-end: generate → validate pipeline
2. CLI integration with real temp files
3. Cross-module data flow verification
