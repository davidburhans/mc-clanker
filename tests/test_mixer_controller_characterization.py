"""Characterization tests pinning the MixerController private-member reach.

Phase 11 (P11-U1) prep: these tests freeze the CURRENT orchestrator→Mixer
handoff behavior so a future MixerController port (refactor/plan.md Phase 11,
default-deferred) cannot silently change audio behavior. They characterize
CURRENT behavior against the real code — GREEN at baseline — and must NOT
change any production code.

Two of the four P11-U1 contract invariants live here (the behavioral ones):
  * Invariant 2 — P10 loop-1 lock-held spy: every batch write happens while
    ``mixer.lock`` is held (deterministic, no racing threads).
  * Invariant 4 — crossfade event round-trip: ``set_next_loop`` → real
    ``_callback`` fires the transition → ``pop_transition_event`` returns it.

The structural AST invariants (1 + 3) live in ``tests/test_loop_lock_safety.py``.
"""

from __future__ import annotations

import threading
from uuid import uuid4

import numpy as np
import pytest

from app.framework.framework_main_async import AsyncFrameworkLoop
from app.framework.framework_mixer import Mixer
from app.framework.framework_state import state


@pytest.fixture(autouse=True)
def _reset_mixer_state():
    """Snapshot/restore the state singleton + set the flags _callback needs.

    ``Mixer._callback`` reads ``state.snapshot_mixer_state()`` (is_generating +
    soloed/muted/volumes) under sync_lock, so a real-Mixer test must leave
    ``is_generating`` True for the callback to mix rather than emit silence.
    Mirrors the ``reset_state`` fixture in ``tests/test_mixer.py``.
    """
    state.reset()
    state.is_generating = True
    state.active_stems = [{"prompt": "s0"}, {"prompt": "s1"}]
    yield
    state.is_generating = False
    state.soloed_stems.clear()
    state.muted_stems.clear()
    state.stem_volumes.clear()


# --------------------------------------------------------------------------- #
# Invariant 2 — P10 loop-1 lock-held spy (deterministic, no racing threads)
# --------------------------------------------------------------------------- #


class _LockSpyMixer:
    """Fake mixer recording whether ``mixer.lock`` is held at each P10 batch write.

    P10's loop-1 branch performs three mutating operations under one
    ``with self.mixer.lock:``: a ``_add_track_internal`` call and two attribute
    assignments (``current_loop_end_sample`` / ``_current_loop_duration``). This
    spy records ``lock.locked()`` at each via ``__setattr__`` + the call, so a
    future port that splits the lock or reorders the writes fails the pin.

    Deterministic: the test drives ``_step_commit_to_mixer`` directly on one
    coroutine — no real racing threads — so the lock-held reading is unambiguous.
    """

    _SPY_TARGETS = ("current_loop_end_sample", "_current_loop_duration")

    def __init__(self, *, current_sample: int = 0) -> None:
        # Arm the __setattr__ spy only after the init-time attribute writes so the
        # constructor's own assignments aren't mistaken for P10 batch writes.
        object.__setattr__(self, "_armed", False)
        self.lock = threading.Lock()
        self.sample_rate = 44100
        self.current_sample = current_sample
        self.current_loop_end_sample = 0
        self._current_loop_duration = None
        self.add_locked: list[bool] = []
        self.write_locked: dict[str, list[bool]] = {k: [] for k in self._SPY_TARGETS}
        object.__setattr__(self, "_armed", True)

    def _ensure_stereo(self, audio):
        return audio

    def _add_track_internal(self, audio, start_sample, stem_idx):
        self.add_locked.append(self.lock.locked())

    def __setattr__(self, name, value):
        if getattr(self, "_armed", False) and name in self._SPY_TARGETS:
            self.write_locked[name].append(self.lock.locked())
        object.__setattr__(self, name, value)


async def test_p10_loop1_holds_lock_during_every_batch_write() -> None:
    """Invariant 2: the loop-1 P10 batch holds ``mixer.lock`` across all writes.

    Drives ``_step_commit_to_mixer`` (loop_idx==1) directly so the lock-held
    check is deterministic (no racing threads). The single
    ``with self.mixer.lock:`` must still be held at the moment of EACH mutating
    write — the ``_add_track_internal`` call, the ``current_loop_end_sample`` set,
    and the ``_current_loop_duration`` set. A split-lock promotion would record a
    ``False`` here.
    """
    loop = AsyncFrameworkLoop(uuid4())
    spy = _LockSpyMixer(current_sample=7777)
    loop.mixer = spy  # type: ignore[assignment]  # intentional duck-typed spy
    loop._loop_idx = 1
    tracks = [(np.ones((16, 2), dtype=np.float32), 0)]

    await loop._step_commit_to_mixer(False, tracks, 44100)

    assert spy.add_locked == [True], "mixer.lock not held during _add_track_internal"
    assert spy.write_locked["current_loop_end_sample"] == [True], (
        "mixer.lock not held during current_loop_end_sample write"
    )
    assert spy.write_locked["_current_loop_duration"] == [True], (
        "mixer.lock not held during _current_loop_duration write"
    )
    # The batch computes the boundary from the LIVE current_sample (not 0), so a
    # port that moves the read outside the lock (or hardcodes 0) shifts the boundary.
    assert spy.current_loop_end_sample == 7777 + 44100
    assert spy._current_loop_duration == 44100


# --------------------------------------------------------------------------- #
# Invariant 4 — crossfade transition event round-trip (real Mixer)
# --------------------------------------------------------------------------- #


def test_crossfade_transition_event_round_trip() -> None:
    """Invariant 4: set_next_loop → _callback transition → pop_transition_event round-trip.

    Pins the full event-delivery contract a MixerController port must keep on the
    real concrete ``Mixer`` (synchronous ``_callback`` drive — no stream thread):

      * ``set_next_loop`` does NOT touch ``current_loop_end_sample`` (Bug-1
        invariant — the current loop's boundary is preserved);
      * the real-time ``_callback`` fires the transition at the aligned boundary,
        setting ``_just_transitioned`` + ``_last_transition_loop_index`` and
        advancing ``current_loop_end_sample`` to ``transition_sample +
        _next_loop_duration``;
      * ``pop_transition_event`` returns the loop index AND atomically clears the
        flag (a second pop returns None — no double-fire).
    """
    mixer = Mixer(channels=1)
    mixer.sample_rate = 44100
    mixer.loop_switch_deadline_ms = 50

    current = np.ones((4410, 1), dtype=np.float32) * 0.5
    mixer.add_track(current, 0, stem_index=0)
    mixer.current_sample = 0
    mixer.current_loop_end_sample = 4410

    next_audio = np.ones((4410, 1), dtype=np.float32) * 0.3
    mixer.set_next_loop([(next_audio, 1)], next_loop_duration_samples=4410, loop_idx=7)

    # Bug-1 invariant: set_next_loop preserves the current boundary + queues idx.
    assert mixer.current_loop_end_sample == 4410
    assert mixer._next_loop_idx == 7

    outdata = np.zeros((4410, 1), dtype=np.float32)
    mixer.current_sample = 4300  # inside the loop_switch_deadline window
    mixer._callback(outdata, 4410, None, None)

    # Transition fired at the aligned boundary (4410); new boundary = 4410 + 4410.
    assert mixer._just_transitioned is True
    assert mixer._last_transition_loop_index == 7
    assert mixer.current_loop_end_sample == 4410 + 4410
    assert mixer.next_loop_audio == []

    # pop_transition_event returns the loop index and clears the flag atomically.
    assert mixer.pop_transition_event() == 7
    assert mixer._just_transitioned is False
    # Second pop returns None — the flag was cleared atomically (no double-fire).
    assert mixer.pop_transition_event() is None
