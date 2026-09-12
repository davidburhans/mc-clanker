"""REL-05c regression tests — a failing recording sink must fail loudly, stop cleanly.

Pins the U5 ENOSPC contract (refactor/plans/units/rel-05-plan.md §3.2), re-pinned
through the REL-11 writer-thread sink architecture (refactor/plans/units/rel-11-plan.md
§2.7: the failure machinery moved ONTO the sink's writer thread; the state health
dicts stay the /api/health surface). A disk-full write used to log ONE warning and
then "continue recording" forever: silently corrupt audio, ~635 MB/hr of futile
writes, and a green /api/health. The contract here:

- F1  consecutive write failures are counted per sink and surfaced in /api/health
- F2  a sustained show-sink failure auto-stops that sink cleanly and exactly once,
      keeps current_show_id so the fine-tuning corpus keeps capturing (invariant 4),
      and never touches the closed handle again
- F3  the export sink mirrors the show-sink auto-stop clears exactly
- F4  one successful write resets the consecutive counter (transient hiccups never
      stack up to a stop)
- F5  a concurrent stop_show wins the race: the stale handle's threshold breach is
      a no-op (no double finalize, no flag writes)
- F6  state.reset() clears the recording-health fields (fixture isolation)
- F7  the stop threshold is 32 ticks — ~1.5 s at the mixer's 2048-frame/44.1 kHz
      tick — pinning the window the constant encodes
"""

import asyncio
import io
import os
import time

import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = ""  # Force SQLite for tests

from app.app_ui import app  # noqa: E402  (must import after the env override)
from app.framework import audit_recording  # noqa: E402
from app.framework import recording_sink as recording_sink_module  # noqa: E402
from app.framework.framework_state import state  # noqa: E402
from app.framework.recording_sink import RecordingSink  # noqa: E402
from app.routes import shows as shows_routes  # noqa: E402


class FailingSinkHandle:
    """Recording sink whose write() always fails (ENOSPC stand-in, errno 28).

    Carries the WAV-finalize surface (tell/seek/flush/close) so the real
    ``finalize_wav`` can run over it in the auto-stop tests.
    """

    def __init__(self):
        self.write_attempts = 0
        self.closed = False

    def write(self, pcm_data: bytes) -> int:
        self.write_attempts += 1
        raise OSError(28, "No space left on device")

    def tell(self) -> int:
        return 44

    def seek(self, offset: int, whence: int = 0) -> int:
        return offset

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def wait_until(cond, timeout: float = 3.0) -> bool:
    """Poll ``cond`` until true — same shape as tests/test_youtube_relay.py.

    The failure counters now move on the sink's writer thread, so the
    previously-synchronous asserts must wait for the async write path.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def app_client():
    """Returns a TestClient with the real app."""
    return TestClient(app)


# Sinks a test armed; the fixture stops them on teardown (a leaked sink is a
# daemon thread + open fd — registration here is load-bearing).
_LIVE_SINKS: list = []


@pytest.fixture(autouse=True)
def fresh_state():
    """Reset every recording-relevant state field around each test.

    state.reset() also clears the REL-05c per-sink health dicts once they exist
    (F6 pins that), so this fixture stays valid across the TDD stages.
    """
    state.reset()
    state.dj_password = ""
    state.audience_password = ""
    state.shutdown_event.clear()
    state.current_show_id = None
    state.is_show_recording = False
    state.current_show_sink = None
    state.current_show_start_time = None
    state.is_recording = False
    state.export_sink = None
    state.recording_file_path = None
    state.recording_start_time = None
    _LIVE_SINKS.clear()
    yield
    for sink in _LIVE_SINKS:
        try:
            sink.stop_and_finalize(timeout=1.0)
        except Exception:
            pass
    _LIVE_SINKS.clear()
    state.current_show_sink = None
    state.export_sink = None
    state.dj_password = ""
    state.audience_password = ""


def _arm_show_sink(handle) -> RecordingSink:
    """Arm the show recording slot with a started writer sink over ``handle``."""
    sink = RecordingSink(handle, "show", state)
    sink.start()
    _LIVE_SINKS.append(sink)
    state.current_show_id = 7
    state.is_show_recording = True
    state.current_show_sink = sink
    return sink


def _arm_export_sink(handle, file_path) -> RecordingSink:
    """Arm the export recording slot with a started writer sink (stop_export's field set)."""
    sink = RecordingSink(handle, "export", state)
    sink.start()
    _LIVE_SINKS.append(sink)
    state.is_recording = True
    state.recording_format = "wav"
    state.recording_file_path = str(file_path)
    state.recording_start_time = 1_000.0
    state.export_sink = sink
    return sink


def test_failing_show_sink_counts_errors_and_surfaces_in_health(app_client):
    """F1 (REL-05c acceptance): every failed sink write bumps the per-sink
    consecutive counter, and /api/health shows counters + stop reason (+ the
    REL-11 dropped_bytes counter)."""
    _arm_show_sink(FailingSinkHandle())
    for _ in range(5):
        state.broadcast_audio(b"\x00" * 64)

    assert wait_until(lambda: state.recording_write_errors["show"] == 5), "failures must surface via the writer"
    payload = app_client.get("/api/health").json()
    assert payload["recording"]["show"] == {
        "active": True,
        "write_errors": 5,
        "stopped_reason": None,
        "dropped_bytes": 0,
    }
    assert payload["recording"]["export"]["active"] is False


def test_sustained_failures_stop_show_sink_cleanly(monkeypatch):
    """F2 (REL-05c acceptance): past the threshold the show sink auto-stops once,
    cleanly, and the audit corpus keeps capturing (current_show_id survives)."""
    import app.lib.wav as wav_module  # the finalize the sink's writer calls
    from app.framework.framework_state import RECORDING_WRITE_FAILURE_STOP_THRESHOLD

    handle = FailingSinkHandle()
    _arm_show_sink(handle)
    finalized = []
    real_finalize = wav_module.finalize_wav

    def spy(handle_being_finalized):
        finalized.append(handle_being_finalized)
        real_finalize(handle_being_finalized)

    monkeypatch.setattr(recording_sink_module, "finalize_wav", spy)

    for _ in range(RECORDING_WRITE_FAILURE_STOP_THRESHOLD):
        state.broadcast_audio(b"\x00" * 64)

    assert wait_until(lambda: state.is_show_recording is False), "auto-stop runs on the writer thread"
    assert wait_until(lambda: handle.closed), "the writer finalized the dead handle exactly once"
    assert state.current_show_start_time is None
    assert state.current_show_id == 7, "the audit gate must survive the dead audio sink (invariant 4)"
    assert state.recording_stop_reasons["show"] == "write_failure_threshold"
    assert finalized == [handle]

    writes_after_stop = handle.write_attempts
    for _ in range(3):
        state.broadcast_audio(b"\x00" * 64)
    assert handle.write_attempts == writes_after_stop, "the closed handle is never touched again"
    assert len(finalized) == 1

    asyncio.run(audit_recording.append_loop_audit({"actions": [], "reasoning": "live"}, [], 0))
    assert len(state.llm_interaction_buffer) == 1, "corpus capture outlives the dead audio sink"


def test_sustained_failures_stop_export_sink_cleanly(tmp_path):
    """F3: the export sink mirrors stop_export's clears exactly, then sets the
    same stop reason."""
    from app.framework.framework_state import RECORDING_WRITE_FAILURE_STOP_THRESHOLD

    handle = FailingSinkHandle()
    _arm_export_sink(handle, tmp_path / "mc_clanker_test.wav")
    for _ in range(RECORDING_WRITE_FAILURE_STOP_THRESHOLD):
        state.broadcast_audio(b"\x00" * 64)

    assert wait_until(lambda: state.is_recording is False), "auto-stop runs on the writer thread"
    assert state.export_sink is None
    assert state.recording_file_path is None
    assert state.recording_start_time is None
    assert state.recording_stop_reasons["export"] == "write_failure_threshold"
    assert wait_until(lambda: handle.closed), "the writer finalized the dead export handle"


def test_recovery_resets_consecutive_counter():
    """F4: one good write resets the consecutive counter — transient hiccups
    never accumulate into an auto-stop. A recovered disk is pinned by REBUILDING
    the sink over the healthy handle (REL-11 slots hold sinks, not handles)."""
    bad = FailingSinkHandle()
    _arm_show_sink(bad)
    for _ in range(10):
        state.broadcast_audio(b"\x00" * 64)
    assert wait_until(lambda: state.recording_write_errors["show"] == 10)

    _arm_show_sink(io.BytesIO())  # the disk recovered: a fresh sink over a healthy handle
    state.broadcast_audio(b"\x00" * 64)
    assert wait_until(lambda: state.recording_write_errors["show"] == 0), "one success resets the counter"

    _arm_show_sink(bad)  # the disk regressed: a fresh sink over the bad handle again
    for _ in range(10):
        state.broadcast_audio(b"\x00" * 64)
    assert wait_until(lambda: state.recording_write_errors["show"] == 10)
    assert state.is_show_recording is True, "20 total failures, 10 consecutive — far below the stop threshold"


def test_concurrent_stop_wins_no_double_finalize(monkeypatch):
    """F5: a stop_show that detached the slot wins the race against the writer's
    in-flight threshold breach — the stale handle is never finalized twice."""
    from app.framework.framework_state import RECORDING_WRITE_FAILURE_STOP_THRESHOLD

    stale_handle = FailingSinkHandle()
    sink = _arm_show_sink(stale_handle)
    state.recording_write_errors["show"] = RECORDING_WRITE_FAILURE_STOP_THRESHOLD - 1

    finalized = []
    monkeypatch.setattr(recording_sink_module, "finalize_wav", lambda handle: finalized.append(handle))
    assert shows_routes._stop_show_recording(7) is sink, "stop_show detaches and returns the sink object"
    assert sink.stop_and_finalize() is True, "the detached sink finalizes exactly once via its writer"
    assert finalized == [stale_handle]

    sink._note_failure(OSError(28, "No space left on device"))  # the raced 32nd failure lands late

    assert finalized == [stale_handle], "no double finalize"
    assert state.recording_stop_reasons["show"] is None, "no flag writes after the slot changed hands"
    assert state.is_show_recording is False
    assert state.current_show_sink is None


def test_reset_clears_recording_health_fields():
    """F6: state.reset() restores both health dicts so tests (and user resets)
    start from a clean recording slate."""
    state.recording_write_errors["show"] = 3
    state.recording_stop_reasons["export"] = "write_failure_threshold"

    state.reset()

    assert state.recording_write_errors == {"show": 0, "export": 0}
    assert state.recording_stop_reasons == {"show": None, "export": None}


def test_threshold_constant_documented_by_tick_math():
    """F7: the threshold encodes ~1.5 s of sustained failure at the mixer tick
    (2048 frames @ 44.1 kHz ≈ 46.4 ms ≈ 21.5 ticks/s) — pin both numbers."""
    from app.framework.framework_mixer import Mixer
    from app.framework.framework_state import RECORDING_WRITE_FAILURE_STOP_THRESHOLD

    mixer = Mixer()  # default construction starts no threads
    tick_seconds = mixer.blocksize / mixer.sample_rate
    assert (mixer.sample_rate, mixer.blocksize) == (44100, 2048)
    assert tick_seconds * RECORDING_WRITE_FAILURE_STOP_THRESHOLD == pytest.approx(1.49, abs=0.02)
    assert RECORDING_WRITE_FAILURE_STOP_THRESHOLD == 32
