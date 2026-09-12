"""Shared gate/clock/profile logic for the rel-soak harness (U15).

The audit's acceptance gate: docs/reliability_audit.md §Soak-test spec, points
1-8, as an opt-in pytest suite. This module is deliberately NOT collected (no
``test_`` prefix) — it carries the one spelling of:

* the opt-in gate (registered ``soak`` marker + module-level env skipif; the
  default run reports each soak test as ``SKIPPED`` with the run instructions
  as the reason — see docs/soak_harness.md);
* the two run profiles (``SOAK=1`` → fast CI scale; ``SOAK=1 SOAK_PROFILE=full``
  → the audit's literal numbers under a < 2 min wall budget);
* the virtual clock (plan decision 4): every patched ``asyncio.sleep(delay)``
  advances ``elapsed`` by ``delay`` VIRTUAL seconds and yields exactly once via
  the captured original sleep — a 24 h-equivalent schedule runs at zero wall
  cost. Only the driver's ``asyncio.wait_for`` watchdog runs on real time;
  a wedge is itself a soak failure;
* the shared state-isolation fixture factory (union of the
  test_loop_robustness attribute set with the show/audit-buffer fields the
  soak drives).

A RED soak assertion is a FINDING (a residual or a regression), never a test
bug to loosen — the harness is the whole REL pass's acceptance test.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from app.framework.framework_state import state

SOAK_ENV = "SOAK"
SOAK_PROFILE_ENV = "SOAK_PROFILE"
SOAK_OPT_IN_REASON = "opt-in soak: SOAK=1 .venv/bin/python -m pytest -m soak (SOAK_PROFILE=full = audit-literal 24 h)"


def soak_enabled() -> bool:
    """True when the opt-in env gate is set (evaluated at module import/collection)."""
    return os.environ.get(SOAK_ENV) == "1"


def soak_profile() -> str:
    """'full' selects the audit's literal schedule; anything else is the fast CI profile."""
    return "full" if os.environ.get(SOAK_PROFILE_ENV) == "full" else "fast"


def soak_gate() -> list:
    """Module-level gate applied via ``pytestmark = soak_gate()`` in every soak module.

    The marker makes ``-m soak`` selectable (registered in pyproject.toml); the
    skipif is the actual default-off mechanism — no ``addopts`` mutation of the
    shared CLI (plan decision 2: zero blast radius on existing invocations).
    """
    return [pytest.mark.soak, pytest.mark.skipif(not soak_enabled(), reason=SOAK_OPT_IN_REASON)]


class VirtualClock:
    """Sleep-accumulating virtual clock (plan decision 4).

    ``install(monkeypatch)`` captures the REAL ``asyncio.sleep`` BEFORE the
    patch and installs :meth:`tick` as the module attribute. Every awaited
    ``tick(delay)`` adds ``delay`` to ``elapsed`` (virtual seconds), notifies
    the optional ``on_sleep`` sampler (deterministic per-sleep sampling — the
    loop's real run is far shorter than any wall-tick grid, so sampling at the
    clock gives every fault window hundreds of samples), then yields ONCE via
    the captured original ``asyncio.sleep(0)`` — never a real wait, never a
    recursive call through the patch.

    ``pause`` awaits the captured ORIGINAL for real wall-clock time; only the
    driver watchdog and teardown waits use it. ``elapsed >= 86_400`` IS the
    24 h-equivalent mark (every backoff rung, job-wait timeout and pregen wait
    accumulates at face value in virtual time).
    """

    def __init__(self) -> None:
        self.elapsed = 0.0
        self.on_sleep: Callable[[float], None] | None = None
        self._real_sleep = asyncio.sleep

    async def tick(self, delay: float | None = None) -> None:
        """The patched ``asyncio.sleep``: virtual-time advance + single yield."""
        if delay:
            self.elapsed += delay
            if self.on_sleep is not None:
                self.on_sleep(delay)
        await self._real_sleep(0)

    async def pause(self, seconds: float) -> None:
        """Real wall-clock wait via the captured original (watchdogs only)."""
        await self._real_sleep(seconds)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "VirtualClock":
        monkeypatch.setattr(asyncio, "sleep", self.tick)
        return self


@dataclass(frozen=True)
class SoakParams:
    """Per-profile soak dimensions (plan §2.1) — the one table, no bare literals in test bodies.

    p1 fault windows are virtual-second intervals on the 24 h-equivalent clock
    (full profile keeps the audit's named wall-clock positions: LLM outage
    02:00-03:00, PG restart 05:00-05:10 / 14:00-14:05, worker-down 08:00-09:30,
    stuck generation 11:00). The fast profile compresses the same SHAPES into a
    600 virtual-second run.
    """

    profile: str
    p1_virtual_seconds: float
    p1_wall_budget: float  # real-time watchdog; a wedge is a finding
    p1_loops_cap: int  # safety net only; virtual seconds are the primary stop
    p1_llm_latency_s: float  # per-call virtual LLM latency (paces healthy loops)
    p1_await_latency_s: float  # healthy batch wait in virtual seconds
    p1_llm_outage: tuple[float, float]
    # PG-restart windows must span >= 3 submit attempts INCLUDING the ±25 % jittered
    # B1 backoffs (fast cadence: 3rd failure lands ~255-265) so the rel-18
    # conductor-skip gate actually engages inside the window.
    p1_pg_restart_1: tuple[float, float]
    p1_worker_down: tuple[float, float]
    p1_stuck_job_at: float
    p1_pg_restart_2: tuple[float, float] | None
    p1_min_loops: int  # audit: loop_count must grow, not merely survive
    p1_iteration_floor_s: float  # per-iteration virtual floor (see _FlushingAuditPort.append_loop)
    p2_ticks: int
    p2_fail_every: int
    p2_wall_cap_s: float
    p3_cycles: int
    p4_generations: int  # the audit's literal 300 in BOTH profiles (cheap on fakes)
    p4_evict_every: int
    p5_timeout_s: float
    p6_clients: int
    p6_concurrent_trio: bool
    p7_shows: int
    p7_windows: int
    p8_loops: int


def soak_params() -> SoakParams:
    """The dimension table for the active profile (plan decision 3)."""
    if soak_profile() == "full":
        return SoakParams(
            profile="full",
            p1_virtual_seconds=86_400.0,
            p1_wall_budget=75.0,
            p1_loops_cap=1500,
            p1_llm_latency_s=35.0,
            p1_await_latency_s=75.0,
            p1_llm_outage=(7_200.0, 10_800.0),  # 02:00-03:00 — the audit's named 1 h window
            p1_pg_restart_1=(18_000.0, 18_600.0),  # 05:00-05:10
            p1_worker_down=(28_800.0, 34_200.0),  # 08:00-09:30
            p1_stuck_job_at=39_600.0,  # 11:00
            p1_pg_restart_2=(50_400.0, 50_700.0),  # 14:00-14:05
            p1_min_loops=40,
            p1_iteration_floor_s=5.0,
            p2_ticks=600,
            p2_fail_every=50,
            p2_wall_cap_s=8.0,
            p3_cycles=2,
            p4_generations=300,
            p4_evict_every=30,
            p5_timeout_s=0.05,
            p6_clients=12,
            p6_concurrent_trio=True,
            p7_shows=6,
            p7_windows=5,
            p8_loops=100,
        )
    return SoakParams(
        profile="fast",
        p1_virtual_seconds=600.0,
        p1_wall_budget=15.0,
        p1_loops_cap=120,
        p1_llm_latency_s=5.0,
        p1_await_latency_s=10.0,
        p1_llm_outage=(120.0, 180.0),
        p1_pg_restart_1=(240.0, 280.0),  # spans 3 jittered submit failures + the skip probe
        p1_worker_down=(300.0, 440.0),  # the fast 120 s burns land twice inside
        p1_stuck_job_at=420.0,
        p1_pg_restart_2=None,
        p1_min_loops=15,
        p1_iteration_floor_s=1.0,
        p2_ticks=120,
        p2_fail_every=12,
        p2_wall_cap_s=5.0,
        p3_cycles=2,
        p4_generations=300,
        p4_evict_every=30,
        p5_timeout_s=0.05,
        p6_clients=3,
        p6_concurrent_trio=False,
        p7_shows=6,
        p7_windows=2,
        p8_loops=20,
    )


# --------------------------------------------------------------------------- #
# Shared state isolation (union of test_loop_robustness._STATE_ATTRS with the
# show/audit-buffer fields the soak drives; plan §2.1).
# --------------------------------------------------------------------------- #

_SOAK_STATE_ATTRS = (
    "current_bpm",
    "current_key",
    "previous_stems",
    "active_stems",
    "next_stems",
    "stem_history",
    "current_set_name",
    "llm_reasoning",
    "last_actions",
    "loop_count",
    "loop_history",
    "stem_volumes",
    "muted_stems",
    "soloed_stems",
    "target_bpm_override",
    "target_key_override",
    "user_override",
    "is_generating",
    "is_running",
    "should_reset",
    "currently_playing_loop_index",
    "currently_playing_stems",
    "currently_playing_set_name",
    "currently_playing_reasoning",
    "_stem_cache",  # the last_generated_stems LRU (a read-only property over this)
    "audio_clients",
    "active_subprocesses",
    "youtube_relay",
    "mixer_thread",
    "stream_fanout",
    "current_show_id",
    "current_show_start_time",
    "is_show_recording",
    "llm_interaction_buffer",
    "action_buffer",
)


def _copy_state_attr(attr: str):
    value = getattr(state, attr)
    if isinstance(value, (list, dict, set)):
        return type(value)(value)
    return value


def read_rss_factory():
    """psutil RSS sampler, or a None-returning stub when the dep is missing.

    Decision 6: the import guard lives HERE (never module-level importorskip)
    so the other soak points still run on a bare venv; with the dev group
    installed the RSS plateau assertions are live.
    """
    try:
        import psutil

        process = psutil.Process()
        return lambda: process.memory_info().rss
    except ImportError:
        return lambda: None


def make_isolated_state_fixture():
    """Build the autouse state-isolation fixture for one soak test module.

    Assigned at module level in each soak module (``_isolated_soak_state =
    make_isolated_state_fixture()``): ``tests/`` is not a package, so fixtures
    cannot be imported across sibling modules — the repo already repeats this
    snapshot pattern per module (test_loop_robustness / test_reset_reprime).
    """

    @pytest.fixture(autouse=True)
    def _isolated_soak_state():
        """Snapshot/restore the state singleton so soak drives cannot leak."""
        state.shutdown_event.clear()
        snapshot = {attr: _copy_state_attr(attr) for attr in _SOAK_STATE_ATTRS}
        state.is_generating = True
        state.is_running = True
        state.should_reset = False
        state.loop_count = 0
        state.active_stems = []
        state.previous_stems = []
        state.next_stems = []
        state.stem_history = []
        state.current_show_id = None
        state.current_show_start_time = None
        state.llm_interaction_buffer = []
        state.action_buffer = []
        yield
        for attr, value in snapshot.items():
            setattr(state, attr, value)
        state.shutdown_event.clear()

    return _isolated_soak_state
