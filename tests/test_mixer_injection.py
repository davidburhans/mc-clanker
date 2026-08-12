"""E5/R14 dependency-injection tests for the ``AsyncFrameworkLoop`` mixer factory.

The real-time ``Mixer`` is being made constructor-injectable via a FACTORY (a
zero-arg callable) instead of being hard-coded inside ``start()`` — the practical
CLAUDE.md DI goal ("inject dependencies through constructor/parameter, not
global/import"), mirroring how ``ConductorPort`` is already ctor-injected (see
``test_conductor_injection.py``).

A FACTORY (not a pre-built instance) is injected because ``Mixer`` construction
runs inside a ``ThreadPoolExecutor`` in ``start()`` (a blocking-construction
hedge). The factory preserves that lazy executor construction; the default
factory is the ``Mixer`` class itself, so the real path stays unchanged.

These pin:
- the default factory resolves to the concrete ``Mixer`` class (no behavior
  change for callers that omit the new keyword arg);
- an injected factory is stored and resolves to the injected fake (real DI);
- ``start()`` builds the mixer THROUGH the injected factory (not a real
  ``Mixer``) — the core U4 guard, exercised deterministically with no audio
  hardware and no real loop iteration;
- the pre-existing ``loop.mixer = <fake>`` direct-assignment harness still works
  (the new seam is additive, never breaks it).

STATUS (Phase 11 U4 — GREEN): ``mixer_factory`` is ctor-injected and
``_mixer_factory`` is stored lazily; ``start()`` runs the factory inside its
``ThreadPoolExecutor`` so the real audio path is byte-for-byte unchanged.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.framework.framework_mixer import Mixer
from app.framework.framework_state import state
from app.framework.loop_orchestrator import AsyncFrameworkLoop


class _FakeMixer:
    """No-op mixer stand-in — ``start()`` must NOT spawn the real audio thread."""

    sample_rate = 44100
    current_sample = 0
    current_loop_end_sample = 0

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def test_loop_constructs_default_mixer_factory() -> None:
    """Omitting ``mixer_factory`` yields the concrete ``Mixer`` class (no change)."""
    loop = AsyncFrameworkLoop(uuid4())
    assert loop._mixer_factory is Mixer


def test_loop_accepts_injected_mixer_factory() -> None:
    """An injected factory is stored and resolves to the injected fake (real DI)."""
    sentinel = object()
    loop = AsyncFrameworkLoop(uuid4(), mixer_factory=lambda: sentinel)
    assert loop._mixer_factory() is sentinel


async def test_start_uses_injected_mixer_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """``start()`` builds the mixer via the injected factory, not a real ``Mixer``.

    Neutralizes the loop body (``state.is_running`` False) so ``_run_loop``'s
    ``while self.running and state.is_running:`` exits immediately — no real I/O
    — while still exercising the factory seam inside ``start()``. The fake's
    ``start()``/``stop()`` are no-ops so no real audio thread spawns.
    """
    fake = _FakeMixer()
    loop = AsyncFrameworkLoop(uuid4(), mixer_factory=lambda: fake)
    monkeypatch.setattr(state, "is_running", False)

    await loop.start()
    try:
        assert loop.mixer is fake
        assert not isinstance(loop.mixer, Mixer)
    finally:
        await loop.stop()


def test_mixer_runtime_assignment_still_works() -> None:
    """The pre-existing ``loop.mixer = <fake>`` direct-assignment harness still works.

    Characterization tests substitute the mixer by direct attribute assignment
    (duck-typed). Constructor-injection of the factory is additive and does NOT
    break that harness.
    """
    loop = AsyncFrameworkLoop(uuid4())
    fake = _FakeMixer()
    loop.mixer = fake  # type: ignore[assignment]  # intentional duck-typed fake
    assert loop.mixer is fake
