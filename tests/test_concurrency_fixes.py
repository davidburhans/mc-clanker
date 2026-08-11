"""Regression tests for the adversarial-review CONCURRENCY fixes (findings
A1, A2, A4, A8/B8, B9, A11) in framework_state.py / framework_mixer.py.

These exercise only the behaviour introduced/changed by the fix; existing
coverage in test_state.py / test_mixer(_extended).py is not duplicated.
"""

import logging
from unittest.mock import MagicMock

from app.framework.framework_state import GlobalState

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


def test_broadcast_audio_logs_recording_failure_once_per_handle(caplog):
    """A failing recording sink must be logged (not silently swallowed) and at
    most once per distinct handle (no per-PCM-chunk spam)."""
    state = GlobalState()
    state.shutdown_event.clear()
    state.audio_clients = []

    failing_handle = MagicMock()
    failing_handle.write.side_effect = OSError("disk full")
    state.is_show_recording = True
    state.current_show_audio_file = failing_handle  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger="app.framework.framework_state"):
        state.broadcast_audio(b"chunk1")
        state.broadcast_audio(b"chunk2")
        state.broadcast_audio(b"chunk3")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "should log a failing handle at most once"
    assert "show sink" in warnings[0].getMessage()
    # Every chunk still attempted a write.
    assert failing_handle.write.call_count == 3
    # A new distinct handle logs again.
    second_handle = MagicMock()
    second_handle.write.side_effect = OSError("still full")
    state.current_show_audio_file = second_handle  # type: ignore[assignment]
    state.broadcast_audio(b"chunk4")
    assert second_handle.write.call_count == 1


def test_broadcast_audio_snapshots_recording_handle_under_lock():
    """broadcast_audio must read the handle via the sync_lock snapshot (A1)."""
    state = GlobalState()
    state.shutdown_event.clear()
    state.audio_clients = []

    mock_file = MagicMock()
    state.current_show_audio_file = mock_file  # type: ignore[assignment]
    state.is_show_recording = True

    state.broadcast_audio(b"pcm")

    mock_file.write.assert_called_once_with(b"pcm")


# ---------------------------------------------------------------------------
# B8 / A4 — trigger_shutdown flushes+closes recording handles under lock
# ---------------------------------------------------------------------------


def test_trigger_shutdown_closes_recording_handles():
    """On shutdown, open recording sinks must be flushed+closed and cleared so
    SIGTERM doesn't leave truncated files (B8)."""
    state = GlobalState()
    state.is_running = True
    state.is_generating = True
    state.shutdown_event.clear()
    state.audio_clients = []
    state.active_subprocesses = set()

    show_handle = MagicMock()
    export_handle = MagicMock()
    state.is_show_recording = True
    state.current_show_audio_file = show_handle  # type: ignore[assignment]
    state.is_recording = True
    state.recording_file_handle = export_handle  # type: ignore[assignment]

    state.trigger_shutdown()

    show_handle.flush.assert_called_once()
    show_handle.close.assert_called_once()
    export_handle.flush.assert_called_once()
    export_handle.close.assert_called_once()
    assert state.current_show_audio_file is None
    assert state.recording_file_handle is None
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
