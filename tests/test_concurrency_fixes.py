"""Regression tests for the adversarial-review CONCURRENCY fixes (findings
A1, A2, A4, A8/B8, B9, A11) in framework_state.py / framework_mixer.py.

These exercise only the behaviour introduced/changed by the fix; existing
coverage in test_state.py / test_mixer(_extended).py is not duplicated.
"""

import logging
import time
from unittest.mock import MagicMock

import pytest

from app.framework.framework_state import GlobalState
from app.framework.recording_sink import RecordingSink

# Sinks a test armed; the autouse fixture stops them on teardown (a leaked sink
# is a daemon thread + open fd — registration here is load-bearing).
_LIVE_SINKS: list = []


def wait_until(cond, timeout: float = 3.0) -> bool:
    """Poll ``cond`` until true — sink writes now land on the writer thread."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _start_sink(handle, sink_name: str, state) -> RecordingSink:
    """Build + start a RecordingSink over ``handle`` and register it for teardown."""
    sink = RecordingSink(handle, sink_name, state)
    sink.start()
    _LIVE_SINKS.append(sink)
    return sink


@pytest.fixture(autouse=True)
def stop_armed_sinks():
    """Stop every sink these tests armed so no writer thread leaks."""
    yield
    for sink in _LIVE_SINKS:
        try:
            sink.stop_and_finalize(timeout=1.0)
        except Exception:
            pass
    _LIVE_SINKS.clear()

# ---------------------------------------------------------------------------
# A11 — vestigial next_loop_ready / next_loop_tracks removed
# ---------------------------------------------------------------------------


def test_vestigial_next_loop_ready_event_removed():
    """The dead `next_loop_ready` Event must no longer exist on state.

    It was never set/waited; real handoff is Mixer.set_next_loop /
    pop_transition_event. Keeping a documented-but-dead Event is a hazard
    (a dev following CLAUDE.md would deadlock waiting on it).
    """
    state = GlobalState()
    assert not hasattr(state, "next_loop_ready")
    assert not hasattr(state, "next_loop_tracks")


def test_reset_does_not_reference_removed_event():
    """reset() must not touch the removed attributes (would AttributeError)."""
    state = GlobalState()
    state.is_generating = True
    state.reset()  # must not raise
    assert state.is_generating is False


# ---------------------------------------------------------------------------
# A2 — snapshot_mixer_state returns independent, consistent copies
# ---------------------------------------------------------------------------


def test_snapshot_mixer_state_returns_independent_copies():
    """Mutating the returned sets/dict must not affect live state.

    The mixer reads these copies per tick; they must be snapshots so a
    callback never iterates a container a route handler is mutating.
    """
    state = GlobalState()
    state.is_generating = True
    state.soloed_stems = {0, 2}
    state.muted_stems = {1}
    state.stem_volumes = {0: 0.5, 1: 0.25}

    is_gen, soloed, muted, volumes = state.snapshot_mixer_state()

    assert is_gen is True
    # Copies are equal in content...
    assert soloed == {0, 2}
    assert muted == {1}
    assert volumes == {0: 0.5, 1: 0.25}
    # ...but independent objects.
    soloed.add(99)
    muted.discard(1)
    volumes[7] = 9.0
    assert state.soloed_stems == {0, 2}
    assert state.muted_stems == {1}
    assert state.stem_volumes == {0: 0.5, 1: 0.25}


# ---------------------------------------------------------------------------
# A1 / B9 — broadcast_audio snapshots handles + logs each distinct failure once
# ---------------------------------------------------------------------------


def test_broadcast_audio_logs_recording_failure_once_per_sink(caplog):
    """A failing recording sink must be logged (not silently swallowed) and at
    most once per distinct sink (no per-PCM-chunk spam). REL-11: the once-flag
    moved onto the sink object (the old once-per-handle state attr is gone)."""
    state = GlobalState()
    state.shutdown_event.clear()
    state.audio_clients = []

    failing_handle = MagicMock()
    failing_handle.tell.return_value = 44  # the writer's finalize_wav packs sizes from it
    failing_handle.write.side_effect = OSError("disk full")
    first_sink = _start_sink(failing_handle, "show", state)
    state.is_show_recording = True
    state.current_show_sink = first_sink  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger="app.framework.recording_sink"):
        state.broadcast_audio(b"chunk1")
        assert wait_until(lambda: failing_handle.write.call_count == 1)
        state.broadcast_audio(b"chunk2")
        state.broadcast_audio(b"chunk3")
        assert wait_until(lambda: failing_handle.write.call_count == 3)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "should log a failing sink at most once"
    assert "show sink" in warnings[0].getMessage()
    # A new distinct sink (fresh handle) logs again.
    second_handle = MagicMock()
    second_handle.tell.return_value = 44  # the writer's finalize_wav packs sizes from it
    second_handle.write.side_effect = OSError("still full")
    second_sink = _start_sink(second_handle, "show", state)
    state.current_show_sink = second_sink  # type: ignore[assignment]
    state.broadcast_audio(b"chunk4")
    assert wait_until(
        lambda: len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2
    ), "the second sink's first failure must be logged too"
    assert second_handle.write.call_count == 1


def test_broadcast_audio_snapshots_recording_sink_under_lock():
    """broadcast_audio must read the sink via the sync_lock snapshot (A1); the
    block reaches the handle through the sink's writer (REL-11 parity)."""
    state = GlobalState()
    state.shutdown_event.clear()
    state.audio_clients = []

    mock_file = MagicMock()
    mock_file.tell.return_value = 44  # the writer's finalize_wav packs sizes from it
    sink = _start_sink(mock_file, "show", state)
    state.current_show_sink = sink  # type: ignore[assignment]
    state.is_show_recording = True

    state.broadcast_audio(b"pcm")

    assert wait_until(lambda: mock_file.write.called), "the block must reach the handle via the writer"
    mock_file.write.assert_called_once_with(b"pcm")


# ---------------------------------------------------------------------------
# B8 / A4 — trigger_shutdown flushes+closes recording handles under lock
# ---------------------------------------------------------------------------


def test_trigger_shutdown_finalizes_recording_sinks():
    """On shutdown, armed recording sinks must be drained+finalized and cleared
    so SIGTERM doesn't leave truncated files (B8). REL-11/REL-22: trigger_shutdown
    detaches the slots and stops the sinks OUTSIDE the lock — each sink's writer
    thread is the single owner that flushes + closes the handle."""
    state = GlobalState()
    state.is_running = True
    state.is_generating = True
    state.shutdown_event.clear()
    state.audio_clients = []
    state.active_subprocesses = set()

    show_handle = MagicMock()
    show_handle.tell.return_value = 44  # finalize_wav packs sizes from tell()
    export_handle = MagicMock()
    export_handle.tell.return_value = 44
    show_sink = _start_sink(show_handle, "show", state)
    export_sink = _start_sink(export_handle, "export", state)
    state.is_show_recording = True
    state.current_show_sink = show_sink  # type: ignore[assignment]
    state.is_recording = True
    state.export_sink = export_sink  # type: ignore[assignment]

    state.trigger_shutdown()

    # No PCM was queued, so the only flush is the writer's finalize flush.
    show_handle.flush.assert_called_once()
    show_handle.close.assert_called_once()
    export_handle.flush.assert_called_once()
    export_handle.close.assert_called_once()
    assert state.current_show_sink is None
    assert state.export_sink is None
    assert state.is_show_recording is False
    assert state.is_recording is False


def test_trigger_shutdown_sets_run_flags_under_lock():
    """is_running/is_generating flips to False on shutdown (A4)."""
    state = GlobalState()
    state.is_running = True
    state.is_generating = True
    state.shutdown_event.clear()
    state.audio_clients = []
    state.active_subprocesses = set()

    state.trigger_shutdown()

    assert state.is_running is False
    assert state.is_generating is False
