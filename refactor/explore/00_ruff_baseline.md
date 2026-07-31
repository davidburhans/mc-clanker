# 00 — Ruff & Typing Baseline (E6)

Captured at baseline commit `906a49b` (Python 3.12 `.venv`).

## Current ruff config

`pyproject.toml [tool.ruff]` sets only `line-length = 120` + `target-version = "py310"`.
**No `[tool.ruff.lint]` section** → effective rules are default `E` (pycodestyle) + `F` (pyflakes).
`UP` (pyupgrade) and `I` (import sorting) are NOT enabled, so `typing.List`/`Dict`/`Optional`
violations are convention-only today, not lint errors.

## Baseline counts

| Rule set | Errors | Auto-fixable |
| --- | ---: | ---: |
| default (`E`+`F`) over `app/ tests/` | **735** | 433 (+41 w/ unsafe-fixes) |
| `UP,F` over `app/ tests/` | **363** | 311 (+19 w/ unsafe-fixes) |
| `UP006,UP007,UP045` (List→list, Dict→dict, Optional→X\|None) | **223** | **223 (100%)** |

## Confirmed pyflakes (F) finding

- `framework_mixer.py:13` — `from typing import Optional` imported but unused (`F401`).

## E6 remediation approach (for the planner)

1. `ruff check --select UP006,UP007,UP045 --fix app/ tests/` → eliminates 223 banned-typing uses mechanically, zero behavior change.
2. Manual pass for remaining `Any` (UP rule does not touch `Any`) — replace with concrete types or `object` where truly dynamic; the conductor + recording lib are the hotspots.
3. Enable enforcement: add `[tool.ruff.lint] select = ["E","F","UP","I"]` (or a subset) to `pyproject.toml` so regressions are caught. Confirm `I` (import sorting) doesn't churn unrelated files — scope or stage separately if so.
4. Re-baseline after each phase; assert "no NEW ruff errors introduced" as a gate.
