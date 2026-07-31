"""Phase 7a safety tests: lock invariants the orchestrator must hold.

The round-1 adversarial review (BLOCKERs A4 + the lock-scope concern) required
that the single-lock atomic commit + the "no I/O inside state.lock" rule be
verified by TESTS, not comments. These two tests pin those invariants for the
orchestrator regardless of how _run_loop is later decomposed into _step_* methods.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from uuid import uuid4

from app.framework.framework_main_async import AsyncFrameworkLoop
from app.framework.framework_state import state

# Reuse the loop-driving harness (brief-04 ssD) instead of duplicating it.
from tests.test_framework_characterization import (  # noqa: E402
    _active_stem_for_retain,
    _conductor_response_with_actions,
    _seed_loop_for_run,
    _wire_loop_no_io,
)


def test_no_io_inside_state_lock_in_orchestrator() -> None:
    """No `await` or `open()` may appear inside any `async with state.lock:` block.

    CLAUDE.md forbids I/O inside the state lock (it stalls the event loop / opens
    races). Source-inspected via AST so it catches regressions even if the lock
    block is later extracted into a _step_* method.
    """
    src = Path("app/framework/loop_orchestrator.py").read_text()
    tree = ast.parse(src)

    def _is_state_lock_ctx(expr: ast.AST) -> bool:
        return (
            isinstance(expr, ast.Attribute)
            and isinstance(expr.value, ast.Name)
            and expr.value.id == "state"
            and expr.attr == "lock"
        )

    violations: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncWith, ast.With)) and any(
            _is_state_lock_ctx(item.context_expr) for item in node.items
        ):
            for child in ast.walk(node):
                if child is node:
                    continue
                if isinstance(child, ast.Await):
                    violations.append((node.lineno, child.lineno, "await"))
                elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "open":
                    violations.append((node.lineno, child.lineno, "open()"))

    assert not violations, f"I/O inside state.lock forbidden (CLAUDE.md / brief-01 risk #2): {violations}"


async def test_record_loop_transition_runs_outside_state_lock(monkeypatch) -> None:
    """P11 atomicity: record_loop_transition (takes blocking sync_lock) must run
    while state.lock (asyncio) is UNlocked — it is called just AFTER the single
    `async with state.lock:` commit block releases (brief-01 risk #3)."""
    loop = AsyncFrameworkLoop(uuid4())
    _seed_loop_for_run(loop, current_sample=0, stop_on=("add", 1))
    state.active_stems = [_active_stem_for_retain()]
    state.current_bpm = 128
    state.current_key = "A minor"
    _wire_loop_no_io(loop, monkeypatch, response=_conductor_response_with_actions())

    lock_states: list[bool] = []

    def _spy(idx: int, stems: object, set_name: object, reason: object) -> None:
        lock_states.append(state.lock.locked())

    monkeypatch.setattr(state, "record_loop_transition", _spy)

    await asyncio.wait_for(loop._run_loop(), timeout=5.0)

    assert lock_states, "expected record_loop_transition to fire for loop 1"
    assert not any(lock_states), (
        f"record_loop_transition must run OUTSIDE state.lock (brief-01 risk #3); locked-at-call={lock_states}"
    )
