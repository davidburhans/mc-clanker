"""Regression tests for REL-01 + REL-21 (unit rel-mixer-resilience).

REL-01: one exception in the mixer render loop must not kill the audio
thread — the loop survives N ticks, emits silence (zeros) for the failed
block, and the thread's liveness is observable via ``state.mixer_thread``
surfaced through ``/api/health`` as ``mixer_alive``.

REL-21: NaN/Inf in stem audio must be sanitized to finite PCM before the
int16 conversion (``np.clip`` preserves NaN and ``astype("<i2")`` of NaN is
platform-defined garbage), both in the mixer broadcast path and in the AAC
decode normalize path (covered in tests/test_io_timeouts.py).

TDD note: these tests were written BEFORE the fix; see the
rel-remediation-plan U1 spec for the acceptance clauses they pin.
"""

import threading
import time
from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.framework.framework_mixer import Mixer
from app.framework.framework_state import state


@pytest.fixture(autouse=True)
def reset_state():
    state.reset()
    # Pin the framework-alive flags the health payload reports: reset() and
    # trigger_shutdown() in earlier tests leave is_running False, and the
    # REL-01 scenario under test is "flags green WHILE the thread is dead".
    state.is_running = True
    state.is_generating = True
    state.active_stems = [{"prompt": "stem0"}, {"prompt": "stem1"}]
    yield
    # Mixer.start() registers the render thread on state; drop any leftover
    # registration so tests stay independent (same seam as youtube_relay).
    with state.sync_lock:
        state.mixer_thread = None


@pytest.fixture
def client():
    from app.app_ui import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# REL-01 — render-tick fault survival
# ---------------------------------------------------------------------------


def test_stream_loop_survives_raising_callback():
    """A raising _callback must not kill _stream_loop (REL-01).

    Deterministic: run the loop synchronously with a fake callback that
    raises on the first tick. The loop must keep ticking (5 ticks total)
    and the buffer handed to the tick after the failure must be zeroed —
    the "emits silence" half of the spec.
    """
    m = Mixer(channels=1)
    m.blocksize = 512
    ticks = []
    tick_count = 0

    def flaky_callback(outdata, frames, _time, _status):
        nonlocal tick_count
        ticks.append(outdata.copy())
        tick_count += 1
        if tick_count == 1:
            outdata.fill(0.7)
            raise RuntimeError("synthetic mixer tick failure")
        if tick_count >= 5:
            m._running = False
            return
        outdata.fill(0.25)

    m._running = True
    with patch.object(m, "_callback", flaky_callback):
        m._stream_loop()  # synchronous, like test_stream_loop_catchup_path

    assert len(ticks) == 5, "loop must survive the exception and keep ticking"
    assert np.allclose(ticks[1], 0.0), "buffer after a failed tick must be zeroed (silence)"


def test_stream_loop_survives_broadcast_failure():
    """The real render thread must survive broadcast_audio blowing up (REL-01).

    Thread-level: two initial broadcast failures (the exact shape of a dead
    downstream sink, e.g. the YouTube relay), then the thread must still be
    alive mid-run and must have kept rendering past the failures.
    """
    m = Mixer(channels=1)
    m.add_track(np.ones((44100, 1), dtype=np.float32) * 0.5, 0, stem_index=0)
    calls = {"count": 0}

    def flaky_broadcast(_pcm_bytes):
        calls["count"] += 1
        if calls["count"] <= 2:
            raise RuntimeError("synthetic broadcast sink failure")

    with patch.object(state, "broadcast_audio", side_effect=flaky_broadcast):
        m.start()
        try:
            time.sleep(0.3)  # ~6 ticks at 2048/44100 ≈ 46 ms; failures land in first ~92 ms
            assert m._stream_thread.is_alive(), "render thread died after broadcast failure"
        finally:
            m.stop()

    assert calls["count"] > 2, "broadcast must be retried after the failures"
    assert m.current_sample > 0, "playhead must have advanced through the failures"


# ---------------------------------------------------------------------------
# REL-01 — liveness registration (state.mixer_thread)
# ---------------------------------------------------------------------------


def test_mixer_start_registers_thread_on_state():
    """Mixer.start registers the render thread on state; stop clears it."""
    m = Mixer()
    m.start()
    try:
        with state.sync_lock:
            assert state.mixer_thread is m._stream_thread
    finally:
        m.stop()
    with state.sync_lock:
        assert state.mixer_thread is None


# ---------------------------------------------------------------------------
# REL-21 — NaN/Inf sanitization in the mixer broadcast path
# ---------------------------------------------------------------------------


def test_callback_sanitizes_nan_stem_to_finite_pcm():
    """One NaN/Inf-poisoned stem must not garbage-encode the whole mix (REL-21).

    Full e2e through _callback: nan→0, +inf→32767, -inf→-32767, 0.5→16383
    after the int16 conversion that np.clip alone cannot make safe.
    """
    m = Mixer(channels=1)
    audio = np.array([[np.nan], [np.inf], [-np.inf], [0.5]], dtype=np.float32)
    m.add_track(audio, 0, stem_index=0)

    captured = []
    with patch.object(state, "broadcast_audio", side_effect=captured.append):
        outdata = np.zeros((4, 1), dtype=np.float32)
        m._callback(outdata, 4, None, None)

    assert len(captured) == 1
    pcm = captured[0]
    assert len(pcm) == 4 * 2  # int16 mono
    samples = np.frombuffer(pcm, dtype="<i2")
    assert np.isfinite(samples.astype(np.float64)).all()
    assert samples[0] == 0, "NaN must map to 0, not platform garbage"
    assert samples[1] == 32767, "+inf must clamp to full scale"
    assert samples[2] == -32767, "-inf must clamp to negative full scale"
    assert samples[3] == 16383, "0.5 must survive as 16383"


def test_callback_not_generating_sanitizes_broadcast():
    """The not-generating early broadcast path stays all-zeros PCM (REL-21 site 1).

    Pins that site 1 routes through the same sanitize+encode contract and
    still broadcasts every tick while idle.
    """
    m = Mixer(channels=1)
    state.is_generating = False

    captured = []
    with patch.object(state, "broadcast_audio", side_effect=captured.append):
        outdata = np.zeros((50, 1), dtype=np.float32)
        m._callback(outdata, 50, None, None)

    assert len(captured) == 1
    samples = np.frombuffer(captured[0], dtype="<i2")
    assert len(samples) == 50
    assert np.all(samples == 0)


def test_sanitize_pcm_block_maps_nan_and_infinity():
    """Unit pin for the shared sanitizer: NaN→0, ±inf→±1, values preserved."""
    block = np.array([[np.nan, np.inf], [-np.inf, 0.5]], dtype=np.float32)
    result = Mixer._sanitize_pcm_block(block)
    expected = np.array([[0.0, 1.0], [-1.0, 0.5]], dtype=np.float32)
    assert result.dtype == np.float32
    assert np.isfinite(result).all()
    assert np.array_equal(result, expected)


# ---------------------------------------------------------------------------
# REL-01 — /api/health exposes mixer thread liveness
# ---------------------------------------------------------------------------


def test_health_reports_mixer_lifecycle(client):
    """/api/health must report mixer_alive=True while running, None after stop."""
    m = Mixer()
    m.start()
    try:
        data = client.get("/api/health").json()
        assert data["mixer_alive"] is True
        assert data["status"] == "healthy"
    finally:
        m.stop()
    data = client.get("/api/health").json()
    assert data["mixer_alive"] is None, "no registered thread must report None"


def test_health_detects_dead_mixer_thread(client):
    """The REL-01 silent-death signature must be observable: a registered but
    dead mixer thread reports mixer_alive=False while is_running stays True."""
    dead_thread = threading.Thread(target=lambda: None, name="dead-mixer-probe")
    dead_thread.start()
    dead_thread.join()
    try:
        with state.sync_lock:
            state.mixer_thread = dead_thread
        data = client.get("/api/health").json()
        assert data["mixer_alive"] is False
        assert data["is_running"] is True, "REL-01: flags stay green while the thread is dead"
    finally:
        with state.sync_lock:
            state.mixer_thread = None
