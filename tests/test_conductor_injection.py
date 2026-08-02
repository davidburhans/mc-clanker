"""E5 dependency-injection tests for ``AsyncFrameworkLoop``.

The conductor (the LLM driving adapter) is now constructor-injectable instead of
hard-coded — the practical CLAUDE.md DI goal ("inject dependencies through
constructor/parameter, not global/import"). These pin:

- the default is a real ``ConductorLLMAsync`` (no behavior change for callers
  that omit the new keyword arg);
- an injected instance is wired straight through (the injection point is real);
- the pre-existing ``patch.object(loop, 'conductor')`` runtime substitution
  still works (backward compatibility for the existing test harness).

Note on the ``ConductorPort`` Protocol in ``app/framework/ports.py``: the default
``ConductorLLMAsync`` now STRUCTURALLY satisfies it (its ``param = None`` type
lies were corrected), so injection is typed against the Protocol — a fake
implementing ``ConductorPort`` can be passed directly. These tests pin that.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.framework.framework_conductor_async import ConductorLLMAsync
from app.framework.loop_orchestrator import AsyncFrameworkLoop
from app.framework.ports import ConductorPort


def test_default_conductor_satisfies_conductor_port() -> None:
    """The default conductor structurally satisfies the ConductorPort Protocol."""
    loop = AsyncFrameworkLoop(uuid4())
    assert isinstance(loop.conductor, ConductorPort)


def test_loop_constructs_with_default_conductor() -> None:
    """Omitting ``conductor`` yields a real ConductorLLMAsync (no behavior change)."""
    loop = AsyncFrameworkLoop(uuid4())
    assert isinstance(loop.conductor, ConductorLLMAsync)


def test_loop_accepts_injected_conductor() -> None:
    """An injected conductor instance is wired straight through (real DI)."""
    custom = ConductorLLMAsync()
    loop = AsyncFrameworkLoop(uuid4(), conductor=custom)
    assert loop.conductor is custom


def test_conductor_runtime_substitution_still_works() -> None:
    """The pre-existing patch.object(loop, 'conductor') harness keeps working.

    Existing characterization tests substitute the conductor by patching the
    instance attribute (duck-typed). Constructor injection does NOT break that.
    """
    loop = AsyncFrameworkLoop(uuid4())
    fake = SimpleNamespace(get_next_state_async=AsyncMock())
    with patch.object(loop, "conductor", fake):
        assert loop.conductor is fake
