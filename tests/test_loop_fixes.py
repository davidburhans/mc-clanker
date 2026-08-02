"""Regression tests for the framework-loop adversarial fixes.

Covers (file: app/framework/framework_main_async.py):
- B10  calc_duration ZeroDivisionError guard
- B11  mono/1-D audio tiling crash + silence warning
- C1   show audit buffers are now populated (action_buffer / llm_interaction_buffer)
- B13  flush_recording_buffers serialization guard
- B1   _run_loop retries after a transient body exception instead of dying
"""

import asyncio
import threading
import uuid

import numpy as np
import pytest

from app.framework.framework_main_async import (
    AsyncFrameworkLoop,
    calc_duration,
    flush_recording_buffers,
    _flush_lock,
    _to_two_channel,
)
from app.framework.framework_state import state

# Exact column sets the buffers must satisfy for bulk_insert_mappings.
_LLM_INTERACTION_COLS = {
    "show_id",
    "loop_index",
    "timestamp",
    "relative_time_ms",
    "prompt_messages",
    "parsed_response",
    "reasoning",
    "error",
    "was_fallback",
    "bpm",
    "key",
    "set_name",
    "instruments",
    "action_type",
}
_SHOW_ACTION_COLS = {
    "show_id",
    "loop_index",
    "timestamp",
    "relative_time_ms",
    "action_type",
    "stem_index",
    "stem_details",
    "action_description",
}


@pytest.fixture(autouse=True)
def _reset_audit_state():
    """Keep the global state singleton clean between tests."""
    saved = (
        state.current_show_id,
        state.current_show_start_time,
        list(state.llm_interaction_buffer),
        list(state.action_buffer),
        state.is_generating,
        state.is_running,
    )
    state.llm_interaction_buffer.clear()
    state.action_buffer.clear()
    state.current_show_id = None
    state.current_show_start_time = None
    yield
    (
        state.current_show_id,
        state.current_show_start_time,
        state.llm_interaction_buffer,
        state.action_buffer,
        state.is_generating,
        state.is_running,
    ) = saved
    state.llm_interaction_buffer.clear()
    state.action_buffer.clear()


# ---------------------------------------------------------------- B10


def test_calc_duration_normal_bpm():
    assert calc_duration(120, 4) == pytest.approx(8.0)


def test_calc_duration_zero_bpm_does_not_raise():
    # B10: previously ZeroDivisionError that killed the whole loop.
    assert calc_duration(0, 4) == pytest.approx(8.0)


def test_calc_duration_negative_bpm_falls_back():
    assert calc_duration(-10, 4) == pytest.approx(8.0)


# ---------------------------------------------------------------- B11


def test_to_two_channel_promotes_mono():
    mono = np.ones(10, dtype=np.float32)
    out = _to_two_channel(mono)
    assert out.shape == (10, 2)


def test_to_two_channel_preserves_stereo():
    stereo = np.ones((10, 2), dtype=np.float32)
    out = _to_two_channel(stereo)
    assert out.shape == (10, 2)


def test_mono_tile_no_longer_raises():
    # B11: np.tile(audio, (repeats, 1)) on 1-D audio used to raise ValueError.
    mono = np.ones(100, dtype=np.float32)
    tiled = np.tile(_to_two_channel(mono), (4, 1))
    assert tiled.shape == (400, 2)


# ---------------------------------------------------------------- C1


def _conductor_response():
    return {
        "master_bpm": 120,
        "master_key": "C minor",
        "name": "Test Set",
        "reasoning": "keep the groove going",
        "actions": [
            {"action_type": "retain", "stem_index": 0},
            {
                "action_type": "add",
                "sub_family": "Synth Lead",
                "major_family": "Synth",
                "model_id": "foundation-1",
                "bars": 4,
            },
        ],
    }


def _active_stems():
    return [
        {
            "prompt": "Drums, Electronic Drums, 120 BPM",
            "instrument": "Electronic Drums",
            "model_id": "foundation-1",
            "bpm": 120,
            "key": "C minor",
            "bars": 4,
        }
    ]


async def test_append_loop_audit_populates_buffers():
    loop = AsyncFrameworkLoop(uuid.uuid4())
    state.current_show_id = 42
    state.current_show_start_time = 1_000_000.0  # deterministic, non-None

    await loop._append_loop_audit(_conductor_response(), _active_stems(), loop_idx=3)

    # One LLM interaction row.
    assert len(state.llm_interaction_buffer) == 1
    li = state.llm_interaction_buffer[0]
    assert set(li.keys()) == _LLM_INTERACTION_COLS
    assert li["show_id"] == 42 and li["loop_index"] == 3
    assert li["reasoning"] == "keep the groove going"
    assert li["was_fallback"] is False
    assert li["relative_time_ms"] >= 0

    # One ShowAction row per action.
    assert len(state.action_buffer) == 2
    for ar in state.action_buffer:
        assert set(ar.keys()) == _SHOW_ACTION_COLS
        assert ar["show_id"] == 42
        assert ar["loop_index"] == 3
        assert ar["action_type"] in ("retain", "add")


async def test_append_loop_audit_marks_fallback():
    loop = AsyncFrameworkLoop(uuid.uuid4())
    state.current_show_id = 7
    state.current_show_start_time = 1_000_000.0
    fallback = _conductor_response()
    fallback["name"] = "Fallback State"

    await loop._append_loop_audit(fallback, [], loop_idx=1)

    assert state.llm_interaction_buffer[0]["was_fallback"] is True


async def test_append_loop_audit_noop_without_show():
    loop = AsyncFrameworkLoop(uuid.uuid4())
    state.current_show_id = None  # no show recording

    await loop._append_loop_audit(_conductor_response(), _active_stems(), loop_idx=1)

    assert state.llm_interaction_buffer == []
    assert state.action_buffer == []


# ---------------------------------------------------------------- B13


async def test_flush_recording_buffers_is_gated_by_flush_lock():
    # B13: a second flush must wait for an in-flight flush to finish.
    await _flush_lock.acquire()
    try:
        done = asyncio.Event()

        async def run_flush():
            await flush_recording_buffers()
            done.set()

        task = asyncio.create_task(run_flush())
        await asyncio.sleep(0.05)  # let it attempt to enter
        assert not done.is_set(), "flush should block while _flush_lock is held"
    finally:
        _flush_lock.release()

    await asyncio.wait_for(done.wait(), timeout=1.0)
    assert done.is_set()
    task.cancel()  # already finished; cancel is a no-op


# ---------------------------------------------------------------- B1


class _FakeMixer:
    """Minimal mixer stand-in so _run_loop can run without audio hardware."""

    sample_rate = 44100
    current_sample = 0
    current_loop_end_sample = 0

    def __init__(self):
        self.lock = threading.Lock()

    def clear(self):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def pop_transition_event(self):
        return None

    def set_next_loop(self, *args, **kwargs):
        pass

    def _add_track_internal(self, *args, **kwargs):
        pass

    def _ensure_stereo(self, audio):
        return audio


async def test_run_loop_retries_after_transient_exception(monkeypatch):
    # B1: one bad iteration must not permanently kill the set.
    #
    # The conductor's own errors are caught locally and turned into a fallback,
    # so they never reach the B1 watchdog (the outer per-iteration handler).
    # _append_loop_audit runs inside the loop body but OUTSIDE that local try,
    # so an exception there is what exercises the retry path. We also stub the
    # background pre-generator so it cannot hang on real I/O, and signal
    # shutdown on the successful retry so _run_loop actually returns.
    loop = AsyncFrameworkLoop(uuid.uuid4())
    loop.mixer = _FakeMixer()
    loop.running = True  # normally set by start(); we drive _run_loop directly.
    state.is_generating = True
    state.is_running = True
    state.shutdown_event.clear()

    audit_calls = {"n": 0}

    async def fake_conductor(**kwargs):
        # Return immediately (no real LLM I/O); the watchdog is driven below.
        return {
            "master_bpm": 120,
            "master_key": "C minor",
            "actions": [],
            "reasoning": "recovered",
            "name": "Recovered",
        }

    async def fake_append_audit(conductor_response, active_stems, loop_idx):
        audit_calls["n"] += 1
        if audit_calls["n"] == 1:
            raise RuntimeError("transient blip")
        # Success on the retry: stop the loop after this iteration.
        loop.running = False
        state.is_running = False

    async def fake_pregen(*_args, **_kwargs):
        # Keep the background pre-generator from doing real I/O.
        return None

    monkeypatch.setattr(loop.conductor, "get_next_state_async", fake_conductor)
    monkeypatch.setattr(loop, "_append_loop_audit", fake_append_audit)
    monkeypatch.setattr(loop, "_pre_generate_next_loop", fake_pregen)

    # Keep the test fast: collapse every asyncio.sleep (incl. the watchdog
    # backoff) to an instant.
    async def _instant_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    # The loop must RETURN (not raise) despite the in-body RuntimeError.
    await asyncio.wait_for(loop._run_loop(), timeout=5.0)

    # The audit ran at least twice => the loop retried after the fault.
    assert audit_calls["n"] >= 2, f"expected a retry, got {audit_calls['n']} call(s)"

    if loop._pregen_task is not None:
        loop._pregen_task.cancel()
