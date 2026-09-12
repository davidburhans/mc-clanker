# Soak harness (rel-soak, U15) — the 24/7 acceptance gate

The soak harness is the audit's acceptance test for the whole REL pass: every
assertion is phrased from `docs/reliability_audit.md` §Soak-test spec, points
1–8, at soak scale against fakes/real SQLite (no GPU, no Postgres, no ffmpeg,
no LLM required).

**Policy: a RED soak assertion is a FINDING (a residual or a regression), not a
test bug — never loosen an assertion.** Units 1–14 are landed, so the harness
is expected GREEN on `main`; that green-on-main is the TDD red-phase inversion
(the assertions would fail against a pre-fix tree).

## What each point pins

| # | Audit point | Test |
|---|-------------|------|
| 1 | 24 h-equivalent fault-injection soak (LLM outage, PG restart, worker-down, stuck generation) → loop task alive, `loop_count` monotonic, audit buffer bounded, task count flat, pending bounded, RSS plateaus | `test_soak_247.py::test_p1_fault_injection_soak_24h_equivalent` |
| 2 | Mixer fault survival — raise from `_callback` every k-th tick; thread survives, silence bounded | `test_soak_mixer.py::test_p2_mixer_fault_survival` |
| 3 | Reset-then-restart — boundary re-primed + transition fires, TWO cycles | `test_soak_mixer.py::test_p3_reset_then_restart_two_cycles` |
| 4 | VRAM plateau — 300 generations, counters + threads return to baseline | `test_soak_worker.py::test_p4_vram_and_thread_plateau_after_300_generations` |
| 5 | Timeout circuit-breaker — t→s→t→t trips exactly once; completed pipeline resets | `test_soak_worker.py::test_p5_consecutive_timeout_breaker_contract_under_mixed_schedule` |
| 6 | Disconnect churn — K abrupt `/stream.mp3` kills: zero zombie ffmpeg, clients bounded, RSS flat | `test_soak_stream.py::test_p6_disconnect_churn_zero_zombies` |
| 7 | Storage reconciliation — outages + delete_show: zero unreferenced objects / orphan files, bytes bounded by retention | `test_soak_storage_export.py::test_p7_storage_reconciliation_under_db_outages` |
| 8 | Real-session export — real capture+flush, delete-live-show, complete NDJSON from both endpoints | `test_soak_storage_export.py::test_p8_real_session_capture_and_export_roundtrip` |

## How to run

```bash
# Soak suite, fast profile (CI/nightly friendly, ~15 s)
SOAK=1 .venv/bin/python -m pytest -m soak -q

# Soak suite, full 24h-equivalent profile (audit-literal windows, < 60 s)
SOAK=1 SOAK_PROFILE=full .venv/bin/python -m pytest -m soak -q

# One family at a time
SOAK=1 .venv/bin/python -m pytest -m soak tests/test_soak_247.py -q           # P1
SOAK=1 .venv/bin/python -m pytest -m soak tests/test_soak_mixer.py -q         # P2-P3
SOAK=1 .venv/bin/python -m pytest -m soak tests/test_soak_worker.py -q        # P4-P5
SOAK=1 .venv/bin/python -m pytest -m soak tests/test_soak_stream.py -q        # P6
SOAK=1 .venv/bin/python -m pytest -m soak tests/test_soak_storage_export.py -q # P7-P8
```

Without `SOAK=1`, `pytest -m soak` still selects the tests but every one
reports `SKIPPED: opt-in soak: SOAK=1 ...` — the reason string doubles as the
run instructions.

## Notes

- **Normal-suite skip count**: the regular `pytest tests/` run grows by 9
  skips (one per soak test — P1–P8 plus the P7 total-outage guard) plus the
  usual 17. That is the designed default-off behavior — do not "fix" it.
- **psutil** (RSS plateau assertions in P1/P6) lives in the dev dependency
  group (`uv pip install --group dev`). Without it, P1/P6 still run; only the
  RSS assertion degrades to a skip-shaped no-op.
- **Two profiles**: `SOAK=1` runs a compressed schedule (fast); `SOAK=1
  SOAK_PROFILE=full` runs the audit's literal numbers (24 h virtual schedule
  with the 02:00–03:00 LLM-outage window and the 600 s generation timeout,
  K=12 churn, N=100 export loops). The virtual clock advances in virtual
  seconds, so the 24 h schedule costs seconds of wall time; only the driver
  watchdog runs on real time — a wedge is itself a soak failure.
- **Scope-to-soak seams** (module-attr monkeypatches, restored per test):
  `AUDIT_FLUSH_THRESHOLD_ROWS` is lowered in P1 so the real P12 flush trigger
  fires dozens of times, and the fast profile lowers `JOB_WAIT_TIMEOUT_SECONDS`
  so the worker-down/stuck burns fit the compressed schedule. The full profile
  keeps the audit-literal 600 s.
