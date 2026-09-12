"""rel-soak P2-P3 — the audit's real-mixer family (U15).

Audit §Soak-test spec, points 2-3 (docs/reliability_audit.md): (2) Mixer fault
survival — raise from ``_callback`` every k-th tick: the render thread survives
(REL-01 guard), ``/api/health`` observably degrades via the ``state.mixer_thread``
seam, and the silence gap is bounded by k ticks. (3) Reset-then-restart — the
landed REL-02 regression run as a soak: TWO full prime → transition →
``should_reset`` → re-prime → transition cycles against a REAL ``Mixer``.

Split from tests/test_soak_247.py (the plan's own restructuring logic: the
loop-fault fakes of P1 and the real-mixer drives here are different fake
families, and AGENTS.md's 500-line rule applies to new files).

Opt-in (default SKIPPED in normal runs); see docs/soak_harness.md:

    SOAK=1 .venv/bin/python -m pytest -m soak tests/test_soak_mixer.py -q
"""

import time
from unittest.mock import patch
from uuid import uuid4

import numpy as np
from soak_helpers import make_isolated_state_fixture, soak_gate, soak_params
from test_soak_247 import _SAMPLE_RATE, _soak_audio

from app.framework.framework_mixer import Mixer
from app.framework.framework_state import state
from app.framework.loop_orchestrator import AsyncFrameworkLoop

pytestmark = soak_gate()
_isolated_soak_state = make_isolated_state_fixture()


# --------------------------------------------------------------------------- #
# P2 — mixer fault survival (real thread)
# --------------------------------------------------------------------------- #


def test_p2_mixer_fault_survival(monkeypatch):
    """Audit point 2: raise from ``_callback`` every k-th tick — the render
    thread survives (REL-01 guard), the health seam observes it, and the
    silence gap is bounded by k ticks."""
    params = soak_params()
    broadcasts: list[bytes] = []
    monkeypatch.setattr(state, "broadcast_audio", broadcasts.append)

    mixer = Mixer(sample_rate=_SAMPLE_RATE, blocksize=256, channels=1)  # ~5.8 ms/tick
    mixer.prime_loop([(_soak_audio(4.0, channels=1), 0)], duration_samples=4 * _SAMPLE_RATE)
    original_callback = mixer._callback
    ticks = {"n": 0}

    def exploding_callback(outdata, frames, time_info, status):
        ticks["n"] += 1
        if ticks["n"] % params.p2_fail_every == 0:
            raise RuntimeError("tick exploded (soak fault injection)")
        return original_callback(outdata, frames, time_info, status)

    mixer._callback = exploding_callback  # type: ignore[method-assign]  # instance attr shadows the bound method

    mixer.start()
    deadline = time.monotonic() + params.p2_wall_cap_s
    while ticks["n"] < params.p2_ticks and time.monotonic() < deadline:
        time.sleep(0.01)
    alive_at_snapshot = mixer._stream_thread.is_alive()
    registered_midrun = state.mixer_thread is mixer._stream_thread
    mixer.stop()

    assert ticks["n"] >= params.p2_ticks, (
        f"the render loop wedged after {ticks['n']} ticks (< {params.p2_ticks}); a wedge is a soak failure"
    )
    assert alive_at_snapshot, "the render thread died from injected callback faults (REL-01 regression)"
    assert registered_midrun, "state.mixer_thread must register the live render thread (/api/health seam)"
    expected_broadcasts = ticks["n"] - ticks["n"] // params.p2_fail_every
    assert len(broadcasts) >= expected_broadcasts, (
        f"only {len(broadcasts)} of {expected_broadcasts} expected post-fault ticks broadcast"
    )
    # Silence bound: a failing tick emits nothing (the exception precedes the
    # broadcast), so the longest run of consecutive no-broadcast ticks is
    # exactly the every-k-th fault cadence.
    assert len(broadcasts) * params.p2_fail_every >= ticks["n"], (
        "silence gap exceeded k failing ticks (broadcast cadence broken)"
    )
    assert any(block != b"\x00" * len(block) for block in broadcasts[-1:]), "the stream ended silent"


# --------------------------------------------------------------------------- #
# P3 — reset-then-restart regression (the landed REL-02 test, repeated)
# --------------------------------------------------------------------------- #


async def test_p3_reset_then_restart_two_cycles():
    """Audit point 3: prime → transition → ``should_reset`` → restart must
    re-prime the boundary and transition again — repeated for a SECOND full
    cycle against a fresh real mixer (the soak dimension is repetition)."""
    params = soak_params()
    for cycle in range(1, params.p3_cycles + 1):
        mixer = Mixer(channels=1)
        loop = AsyncFrameworkLoop(uuid4())
        loop.mixer = mixer  # type: ignore[assignment]
        frames = mixer.blocksize
        first_boundary = _SAMPLE_RATE
        second_boundary = 2 * _SAMPLE_RATE
        base = cycle * 10
        outdata = np.zeros((frames, 1), dtype=np.float32)
        broadcast: list[bytes] = []

        # Loop 1 handoff (P10 prime path), then queue + consume the next loop.
        mixer.prime_loop([(_soak_audio(1.0, channels=1), 0)], duration_samples=first_boundary)
        mixer.set_next_loop(
            [(_soak_audio(1.0, channels=1), 0)], next_loop_duration_samples=second_boundary, loop_idx=base + 2
        )
        mixer.current_sample = first_boundary - frames
        with patch.object(state, "broadcast_audio", side_effect=broadcast.append):
            mixer._callback(outdata, frames, None, None)
        assert mixer.pop_transition_event() == base + 2, f"cycle {cycle}: precondition — the set was playing"

        # The user-visible reset consumes the flag and zeroes the transition gate.
        state.should_reset = True
        await loop._step_read_state()
        assert state.should_reset is False, f"cycle {cycle}: reset flag not consumed"
        assert mixer.current_loop_end_sample == 0, f"cycle {cycle}: reset precondition — the mixer is boundary-less"

        # Post-reset restart: this commit must prime now, not stage a dead loop.
        loop._loop_idx = base + 3
        await loop._step_commit_to_mixer(False, [(_soak_audio(1.0, channels=1), 0)], first_boundary)
        assert mixer.current_loop_end_sample > 0, (
            f"cycle {cycle}: REL-02 — a boundary-less commit must re-prime the gate"
        )
        assert mixer.next_loop_audio == [], f"cycle {cycle}: a primed loop has no dead set_next_loop residue"

        # Music resumed: a later queued loop is consumed at the restored boundary.
        mixer.set_next_loop(
            [(_soak_audio(1.0, channels=1), 0)], next_loop_duration_samples=second_boundary, loop_idx=base + 4
        )
        mixer.current_sample = mixer.current_loop_end_sample - frames
        with patch.object(state, "broadcast_audio", side_effect=broadcast.append):
            mixer._callback(outdata, frames, None, None)
        assert mixer.pop_transition_event() == base + 4, (
            f"cycle {cycle}: REL-02 — music must transition again after reset+restart"
        )
