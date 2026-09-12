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

import logging
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


# ---------------------------------------------------------------------------
# FU-1 — render-tick failure counters + log rate-limit (rel-01 follow-up)
#
# The REL-01 guard bounded the *crash* risk but logged a full traceback on
# EVERY failing tick (~21.7 lines/s under persistent failure) and nothing
# counted the failures, so /api/health could not distinguish a flapping tick
# from a wedged render loop. These tests pin the counter + rate-limit design
# (plan units/rel-fu-1-plan.md D1/D2/D3).
# ---------------------------------------------------------------------------


def _fail_n_ticks(m: Mixer, n: int) -> None:
    """Drive _stream_loop synchronously with a callback that raises exactly n
    times, then stops the loop (the file's synchronous drive pattern)."""
    ticks = {"count": 0}

    def failing_callback(outdata, frames, _time, _status):
        ticks["count"] += 1
        if ticks["count"] >= n:
            m._running = False
        raise RuntimeError("synthetic mixer tick failure")

    m._running = True
    with patch.object(m, "_callback", failing_callback):
        m._stream_loop()


def test_consecutive_tick_failures_counted_and_visible_in_health(client):
    """F1 (FU-1): consecutive raising ticks increment
    state.mixer_tick_failures (no clean tick in between -> consecutive stays
    up) and /api/health surfaces the same numbers."""
    m = Mixer(channels=1)
    m.blocksize = 512
    _fail_n_ticks(m, 7)

    with state.sync_lock:
        assert state.mixer_tick_failures == {"consecutive": 7, "total": 7}

    data = client.get("/api/health").json()
    assert data["mixer_tick_failures"] == {"consecutive": 7, "total": 7}


def test_clean_tick_resets_consecutive_not_total(client):
    """F2 (FU-1): a clean tick zeroes ``consecutive`` (one recovery episode)
    while ``total`` keeps counting every failed tick of the run."""
    m = Mixer(channels=1)
    m.blocksize = 512
    tick_count = {"n": 0}

    def flaky_callback(outdata, frames, _time, _status):
        tick_count["n"] += 1
        if tick_count["n"] <= 3:
            raise RuntimeError("synthetic mixer tick failure")
        if tick_count["n"] >= 6:
            m._running = False
            return
        outdata.fill(0.25)

    m._running = True
    with patch.object(m, "_callback", flaky_callback):
        m._stream_loop()

    with state.sync_lock:
        assert state.mixer_tick_failures == {"consecutive": 0, "total": 3}


def test_tick_failure_log_rate_limited(monkeypatch, caplog):
    """F3 (FU-1): the traceback is logged on the first failure and every Nth
    consecutive one (TICK_FAILURE_LOG_EVERY, pinned at N=5), plus exactly ONE
    recovery INFO line on the first clean tick. Counter-based formula, no
    wall-clock waits."""
    import app.framework.framework_mixer as framework_mixer

    monkeypatch.setattr(framework_mixer, "TICK_FAILURE_LOG_EVERY", 5)

    m = Mixer(channels=1)
    m.blocksize = 512
    tick_count = {"n": 0}

    def flaky_callback(outdata, frames, _time, _status):
        tick_count["n"] += 1
        if tick_count["n"] <= 12:
            raise RuntimeError("synthetic mixer tick failure")
        m._running = False  # tick 13 succeeds -> recovery line, then stop

    m._running = True
    with caplog.at_level(logging.INFO):
        with patch.object(m, "_callback", flaky_callback):
            m._stream_loop()

    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "tick failed" in r.getMessage()]
    assert len(errors) == 3, [r.getMessage() for r in errors]
    assert "1 consecutive" in errors[0].getMessage()
    assert "5 consecutive" in errors[1].getMessage()
    assert "10 consecutive" in errors[2].getMessage()

    recoveries = [r for r in caplog.records if r.levelno == logging.INFO and "recovered" in r.getMessage()]
    assert len(recoveries) == 1, [r.getMessage() for r in recoveries]
    assert "recovered after 12 failing ticks" in recoveries[0].getMessage()


def test_mixer_start_zeroes_failure_counters_and_reset_clears_them(client):
    """F4 (FU-1): counter lifecycle — start() zeroes (fresh run => fresh
    health), stop() PRESERVES (a died-after-failures mixer stays reportable),
    reset() clears (fixture isolation, like recording_write_errors)."""
    m = Mixer(channels=1)
    m.blocksize = 512
    _fail_n_ticks(m, 3)
    with state.sync_lock:
        assert state.mixer_tick_failures == {"consecutive": 3, "total": 3}

    m.stop()
    with state.sync_lock:
        assert state.mixer_tick_failures == {"consecutive": 3, "total": 3}, (
            "stop() must not zero the counters — the last counts stay reportable"
        )

    fresh = Mixer(channels=1)
    fresh.start()
    try:
        with state.sync_lock:
            assert state.mixer_tick_failures == {"consecutive": 0, "total": 0}
    finally:
        fresh.stop()

    state.reset()
    with state.sync_lock:
        assert state.mixer_tick_failures == {"consecutive": 0, "total": 0}


def test_health_tick_failures_zero_when_idle(client):
    """F5 (FU-1): with no mixer ever started, health reports zeroed counters
    (always an object — no None state) while mixer_alive stays None; 'never
    started' is mixer_alive's job, not a second None-semantics on the
    counters (plan D3)."""
    data = client.get("/api/health").json()
    assert data["mixer_alive"] is None
    assert data["mixer_tick_failures"] == {"consecutive": 0, "total": 0}
