"""REL-11 / REL-20 / REL-22 regression suite — the off-thread recording writer (U9).

Contract under test (refactor/plans/units/rel-11-plan.md §3). The suite is written
TDD-red BEFORE ``app/framework/recording_sink.py`` exists, so every sink-dependent
case fails with ModuleNotFoundError until the implementation stages land:

- REL-11      a stalled recording disk must never delay the mixer tick: the audio
              thread only submit()s into a per-sink bounded queue (drop-oldest +
              dropped-bytes counter); the writer thread owns the handle (T1-T3, T19)
- drain       clean stop = drain -> flush -> finalize by the single owner (the
              writer); a join timeout defers finalize to the writer (T4-T6, T16-T18)
- rel-05      failure surfacing survives the move onto the thread: consecutive
              count, threshold auto-stop, invariant-4 id retention, the
              concurrent-stop race (T7-T9)
- REL-22      the shutdown close path finalizes WAV sizes and ends the live Show
              row, idempotently (T10-T12)
- REL-20      the instruments.json write runs OUTSIDE sync_lock; concurrent adds
              cannot lose updates (T13-T14)
- telemetry   sink.status() and /api/health expose the drop counter (T15)

T14 is a guardrail pin: it may already hold pre-implementation (the current
lock-across-I/O code also serializes adds); its job is to catch a
snapshot-after-release lost-update regression in the new design.
"""

import copy
import io
import json
import os
import queue
import struct
import threading
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = ""  # Force SQLite for tests

from app.app_ui import app  # noqa: E402  (must import after the env override)
from app.framework.framework_mixer import Mixer  # noqa: E402
from app.framework.framework_state import (  # noqa: E402
    DEFAULT_INSTRUMENTS,
    RECORDING_WRITE_FAILURE_STOP_THRESHOLD,
    state,
)
from app.lib.wav import write_wav_header  # noqa: E402
from app.routes import shows as shows_routes  # noqa: E402

# Sinks created by a test; the autouse fixture drains them on teardown. A leaked
# sink is a daemon thread + open fd, so registration here is load-bearing.
_LIVE_SINKS: list = []


# ---------------------------------------------------------------------------
# Fakes (named, per AGENTS.md) — in-memory handles carrying the full
# WAV-finalize surface (tell/seek/flush/close) so the real finalize_wav runs.
# ---------------------------------------------------------------------------


class _MemoryHandle:
    """Shared in-memory recording surface with write/flush/close counters."""

    def __init__(self) -> None:
        self.write_calls = 0
        self.flushes = 0
        self.closes = 0
        self._buf = io.BytesIO()

    def write(self, pcm: bytes) -> int:
        self.write_calls += 1
        self._buf.write(pcm)
        return len(pcm)

    def tell(self) -> int:
        return self._buf.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._buf.seek(offset, whence)

    def flush(self) -> None:
        self.flushes += 1

    def close(self) -> None:
        self.closes += 1

    @property
    def data(self) -> bytes:
        return self._buf.getvalue()


class SlowHandle(_MemoryHandle):
    """write() sleeps first — a slow disk (page-cache flush, cgroup IO throttle)."""

    def __init__(self, write_sleep: float = 0.0) -> None:
        super().__init__()
        self.write_sleep = write_sleep
        self.blocks: list[bytes] = []

    def write(self, pcm: bytes) -> int:
        time.sleep(self.write_sleep)
        self.blocks.append(pcm)
        return super().write(pcm)


class BlockingHandle(_MemoryHandle):
    """write() parks on an Event — a hung disk; unblock it to let the writer finish.

    ``write_calls`` is an ATTEMPT counter (incremented at entry, before the
    park) so tests can gate on "the writer is parked inside write()".
    """

    def __init__(self) -> None:
        super().__init__()
        self.unblock = threading.Event()
        self.write_started = threading.Event()

    def write(self, pcm: bytes) -> int:
        self.write_calls += 1  # attempt counter — see class docstring
        self.write_started.set()
        if not self.unblock.wait(timeout=10.0):
            raise OSError("BlockingHandle.write: unblock was never set within 10s")
        self._buf.write(pcm)
        return len(pcm)


class FailingSinkHandle:
    """Recording sink whose write() always fails (ENOSPC stand-in, errno 28).

    Mirrors tests/test_recording_fault_stop.py's fake of the same name so both
    suites exercise the identical failure surface (plus a close counter — the
    writer must finalize the dead handle exactly once).
    """

    def __init__(self) -> None:
        self.write_attempts = 0
        self.closed = False
        self.close_count = 0

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
        self.close_count += 1


class LockProbe:
    """Duck-typed sync_lock exposing whether it is currently held (REL-20)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.held = False

    def __enter__(self) -> None:
        self._lock.acquire()
        self.held = True

    def __exit__(self, *exc_info) -> None:
        self.held = False
        self._lock.release()


def wait_until(cond, timeout: float = 3.0) -> bool:
    """Poll ``cond`` until true — same shape as tests/test_youtube_relay.py."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# Sink construction + state arming helpers
# ---------------------------------------------------------------------------


def _recording_sink_module():
    """Import the rel-11 sink module — the suite's red seam until U9 lands."""
    import app.framework.recording_sink as recording_sink_module

    return recording_sink_module


def _make_sink(handle, sink_name: str, **sink_kwargs):
    """Build a RecordingSink over ``handle`` and register it for teardown."""
    sink = _recording_sink_module().RecordingSink(handle, sink_name, state, **sink_kwargs)
    _LIVE_SINKS.append(sink)
    return sink


def _start_show_sink(handle, show_id: int = 7, **sink_kwargs):
    """Show-recording setup: started sink + the REL-11 state slots armed."""
    sink = _make_sink(handle, "show", **sink_kwargs)
    sink.start()
    state.current_show_id = show_id
    state.is_show_recording = True
    state.current_show_sink = sink
    return sink


def _start_export_sink(handle, **sink_kwargs):
    """Export-recording setup: started sink + the REL-11 state slots armed."""
    sink = _make_sink(handle, "export", **sink_kwargs)
    sink.start()
    state.is_recording = True
    state.export_sink = sink
    return sink


# ---------------------------------------------------------------------------
# DB helpers (SQLite fallback; conftest resets the DatabaseManager singleton)
# ---------------------------------------------------------------------------


def _naive_utc_now() -> datetime:
    """DATA-1 naive-UTC contract: DATETIME columns store naive UTC timestamps."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _create_show(status: str, started_at=None) -> int:
    """Insert one User+Show row into the SQLite fallback DB; returns the show id."""
    from app.db import DatabaseManager
    from app.models import Show, User

    suffix = uuid.uuid4().hex[:8]
    db = DatabaseManager.get_instance()
    db.create_tables()
    with db.session() as session:
        user = User(username=f"rel11_{suffix}", email=f"rel11_{suffix}@example.com", password_hash="x")
        session.add(user)
        session.flush()
        show = Show(user_id=user.id, title=f"rel-11 writer {suffix}", status=status, started_at=started_at)
        session.add(show)
        session.flush()
        return show.id


def _load_show_row(show_id: int):
    """Reload (status, ended_at, duration_seconds) for one show row."""
    from app.db import DatabaseManager
    from app.models import Show

    with DatabaseManager.get_instance().session() as session:
        row = session.query(Show).filter(Show.id == show_id).first()
        assert row is not None, f"show row {show_id} disappeared"
        return row.status, row.ended_at, row.duration_seconds


# ---------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------


def _assert_valid_finalized_wav(path, data_size: int) -> None:
    """The file on disk parses as canonical PCM WAV and carries patched sizes."""
    with wave.open(str(path), "rb") as wf:
        assert (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) == (2, 2, 44100)
        assert wf.getnframes() == data_size // 4
    raw = path.read_bytes()
    assert raw[4:8] == struct.pack("<I", 36 + data_size)
    assert raw[40:44] == struct.pack("<I", data_size)


def _assert_recording_slots_cleared() -> None:
    """Both sink slots + every recording bookkeeping field are detached."""
    assert state.is_show_recording is False
    assert state.current_show_sink is None
    assert state.current_show_id is None
    assert state.current_show_start_time is None
    assert state.is_recording is False
    assert state.export_sink is None
    assert state.recording_file_path is None
    assert state.recording_start_time is None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_recording_state():
    """Reset every recording-relevant state field around each test.

    Mirrors test_recording_fault_stop.fresh_state, retargeted at the REL-11 sink
    slots (current_show_sink / export_sink); live sinks are stopped on teardown.
    """
    state.reset()
    state.dj_password = ""
    state.audience_password = ""
    state.shutdown_event.clear()
    state.is_running = True
    state.audio_clients = []
    state.current_show_id = None
    state.current_show_start_time = None
    state.is_show_recording = False
    state.current_show_sink = None
    state.is_recording = False
    state.recording_format = "wav"
    state.recording_file_path = None
    state.recording_start_time = None
    state.export_sink = None
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
    state.shutdown_event.clear()
    state.dj_password = ""
    state.audience_password = ""


@pytest.fixture
def isolated_instruments(tmp_path):
    """Point state at a tmp instruments.json and restore the catalog afterwards."""
    saved = (
        state.instruments_file,
        state.custom_instruments,
        state.categorized_instruments,
        state.available_instruments,
    )
    state.instruments_file = str(tmp_path / "instruments.json")
    state.custom_instruments = {}
    state.categorized_instruments = copy.deepcopy(DEFAULT_INSTRUMENTS)
    state.available_instruments = state._flatten_instruments()
    yield state.instruments_file
    (
        state.instruments_file,
        state.custom_instruments,
        state.categorized_instruments,
        state.available_instruments,
    ) = saved


# ---------------------------------------------------------------------------
# REL-11 — the audio thread never waits on the recording disk
# ---------------------------------------------------------------------------


def test_slow_sink_does_not_delay_broadcast_ticks():
    """T1 (REL-11 acceptance): 5 broadcast ticks over a 0.2 s/write disk complete
    in < 0.2 s wall (inline-write failure mode = 1.0 s; 5x margin), and the 5
    blocks still all land."""
    sink = _start_show_sink(SlowHandle(write_sleep=0.2))
    block = b"\x00" * 8192
    start = time.perf_counter()
    for _ in range(5):
        state.broadcast_audio(block)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.2, f"5 broadcast ticks took {elapsed:.3f}s — the recording write stalled the audio thread"
    assert wait_until(lambda: sink.status().bytes_written == 5 * len(block)), "all 5 blocks must still land"


def test_submit_never_blocks_when_queue_full():
    """T2: 12 submits into a full 4-block queue return in < 0.1 s; the 8 overflow
    blocks are dropped-oldest and counted in dropped_bytes.

    Determinism: the warm-up submit parks the writer inside write() (gate below),
    so the queue arithmetic 12 - 4 queued = 8 dropped has no racing consumer.
    """
    handle = BlockingHandle()
    sink = _make_sink(handle, "show", queue_blocks=4, poll_s=0.01)
    sink.start()
    sink.submit(b"\x01" * 256)  # warm-up: the writer takes it and parks in write()
    assert wait_until(lambda: handle.write_calls == 1), "the writer must take the warm-up block"
    block = b"\x02" * 256
    start = time.perf_counter()
    for _ in range(12):
        sink.submit(block)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.1, f"12 submits into a full queue took {elapsed:.3f}s — submit blocked the audio thread"
    handle.unblock.set()
    assert sink.stop_and_finalize() is True
    assert sink.status().dropped_bytes == 8 * len(block), "12 submits - 1 written - 4 queued = 8 dropped"


def test_drop_oldest_keeps_newest_in_order():
    """T3 (REL-11 acceptance): on overflow the OLDEST queued block is dropped;
    surviving blocks land in submission FIFO order with no reorder/duplicate, and
    the evicted bytes are counted."""
    handle = BlockingHandle()
    sink = _make_sink(handle, "show", wav=False, queue_blocks=4, poll_s=0.01)
    sink.start()
    blocks = [bytes([65 + index]) * 64 for index in range(6)]  # A..F
    sink.submit(blocks[0])  # A: parked inside write() by the gate below
    assert wait_until(lambda: handle.write_calls == 1), "the writer must park on block A"
    for block in blocks[1:]:
        sink.submit(block)  # B..E fill the queue; F evicts B (oldest)
    handle.unblock.set()
    assert sink.stop_and_finalize() is True
    expected = blocks[0] + blocks[2] + blocks[3] + blocks[4] + blocks[5]  # A then C,D,E,F
    assert handle.data == expected, "FIFO order of the surviving blocks must be exact"
    assert sink.status().dropped_bytes == len(blocks[1]), "exactly B was dropped-oldest and counted"


def test_mixer_callback_tick_not_delayed_by_slow_sink():
    """T19 (REL-11 acceptance, end-to-end tick): Mixer._callback — whose final
    statement is state.broadcast_audio — keeps tick cadence with a 0.2 s/write
    show sink armed, and the rendered blocks still land."""
    mixer = Mixer()
    outdata = np.zeros((mixer.blocksize, mixer.channels), dtype=np.float32)
    sink = _start_show_sink(SlowHandle(write_sleep=0.2))
    start = time.perf_counter()
    for _ in range(5):
        mixer._callback(outdata, mixer.blocksize, None, None)
    elapsed = time.perf_counter() - start
    budget = 5 * (mixer.blocksize / mixer.sample_rate) + 0.15  # tick cadence + slack
    assert elapsed < budget, (
        f"5 mixer callbacks took {elapsed:.3f}s (budget {budget:.3f}s) — the recording sink stalls the render tick"
    )
    block_bytes = mixer.blocksize * mixer.channels * 2  # s16 stereo
    assert wait_until(lambda: sink.status().bytes_written == 5 * block_bytes), "all 5 rendered blocks must land"


# ---------------------------------------------------------------------------
# Drain / finalize — the writer thread is the single owner of the handle
# ---------------------------------------------------------------------------


def test_stop_show_finalizes_valid_wav_end_to_end(tmp_path):
    """T4 (acceptance): start -> N broadcasts -> _stop_show_recording ->
    stop_and_finalize leaves a valid, playable WAV with patched RIFF/data sizes."""
    path = tmp_path / "show_take.wav"
    handle = open(path, "wb")
    write_wav_header(handle)
    sink = _start_show_sink(handle)
    block = b"\x00" * 8192
    for _ in range(5):
        state.broadcast_audio(block)
    assert wait_until(lambda: sink.status().bytes_written == 5 * len(block)), "all blocks must land before stop"
    detached = shows_routes._stop_show_recording(7)
    assert detached is sink, "_stop_show_recording must return the detached sink object"
    assert sink.stop_and_finalize() is True
    _assert_valid_finalized_wav(path, 5 * len(block))


def test_broadcast_feeds_clients_and_sink_together():
    """T5: one broadcast tick feeds the streaming client queue AND the recording
    sink with the same bytes — parity with the pre-writer fan-out."""
    client_queue: queue.Queue = queue.Queue()
    state.add_audio_client(client_queue)
    handle = SlowHandle(0.0)
    _start_show_sink(handle)
    block = b"\x06" * 1024
    state.broadcast_audio(block)
    assert client_queue.get(timeout=1.0) == block, "the client fan-out must be unchanged"
    assert wait_until(lambda: handle.blocks == [block]), "the sink must receive the same block"


def test_export_non_wav_sink_flushes_without_riff_patch(monkeypatch):
    """T6: a wav=False export sink stops with flush+close and never patches a
    RIFF header — raw bytes are preserved verbatim."""
    sink_module = _recording_sink_module()
    finalized = []
    monkeypatch.setattr(sink_module, "finalize_wav", lambda being_finalized: finalized.append(being_finalized))
    handle = SlowHandle(0.0)
    sink = _start_export_sink(handle, wav=False)
    block = b"RAW-PCM-BYTES"
    state.broadcast_audio(block)
    assert wait_until(lambda: sink.status().bytes_written == len(block))
    assert sink.stop_and_finalize() is True
    assert finalized == [], "non-wav export must never run finalize_wav"
    assert handle.data == block, "raw export bytes must be untouched by stop"
    assert handle.flushes >= 1 and handle.closes == 1, "the non-wav path still flushes and closes the handle"


def test_writer_flushes_per_block(tmp_path):
    """T16 (plan §1.5): the writer flushes after every successful block, so the
    size is on disk the moment bytes_written reports it (no userspace lag)."""
    path = tmp_path / "flush_probe.wav"
    handle = open(path, "wb")
    write_wav_header(handle)
    sink = _make_sink(handle, "show")
    sink.start()
    block = b"\x00" * 8192
    sink.submit(block)
    assert wait_until(lambda: sink.status().bytes_written == len(block)), "the block must be written"
    assert os.path.getsize(path) == 44 + len(block), "flush-per-block: bytes must be on disk with no userspace lag"
    assert sink.stop_and_finalize() is True


def test_join_timeout_defers_finalize_to_writer():
    """T17 (plan §1.3): a timed-out stop does NOT touch the handle — the unstuck
    writer finalizes it later (single-owner finalize)."""
    handle = BlockingHandle()
    sink = _make_sink(handle, "show", poll_s=0.01)
    sink.start()
    sink.submit(b"\x03" * 128)
    assert wait_until(lambda: handle.write_calls == 1), "the writer must park inside write()"
    assert sink.stop_and_finalize(timeout=0.1) is False, "the bounded join must report the stuck writer"
    assert handle.closes == 0 and handle.flushes == 0, "the stopper must not touch the handle on timeout"
    handle.unblock.set()
    assert wait_until(lambda: sink._finalized), "the writer finalizes once it unsticks"
    assert handle.closes == 1, "exactly one finalize, by the writer"


def test_late_submit_after_stop_dropped_and_counted():
    """T18 (plan §1.6): submit() after stop_and_finalize returns fast, is dropped,
    counted, and never reaches the closed handle."""
    handle = SlowHandle(0.0)
    sink = _make_sink(handle, "show")
    sink.start()
    sink.submit(b"\x04" * 128)
    assert wait_until(lambda: sink.status().bytes_written == 128)
    assert sink.stop_and_finalize() is True
    data_at_stop = handle.data
    start = time.perf_counter()
    sink.submit(b"\x05" * 64)
    assert time.perf_counter() - start < 0.1, "a late submit must never block"
    assert sink.status().dropped_bytes == 64, "the late block is counted as dropped"
    assert handle.data == data_at_stop, "the late block never reaches the closed handle"
    assert handle.closes == 1, "no second finalize after stop"


# ---------------------------------------------------------------------------
# rel-05 failure surfacing — preserved through the writer thread
# ---------------------------------------------------------------------------


def test_sustained_failures_autostop_through_thread():
    """T7 (acceptance, rel-05 F2): a threshold of failing writes auto-stops the
    show sink through the writer thread; current_show_id survives (invariant 4),
    the stop reason is surfaced, and the dead handle is finalized exactly once."""
    handle = FailingSinkHandle()
    _start_show_sink(handle)
    block = b"\x00" * 64
    for _ in range(RECORDING_WRITE_FAILURE_STOP_THRESHOLD):
        state.broadcast_audio(block)
    assert wait_until(lambda: not state.is_show_recording), "the threshold breach must auto-stop via the writer"
    assert wait_until(lambda: handle.close_count == 1), "the dead sink's handle is finalized exactly once"
    assert state.current_show_id == 7, "the audit gate survives the dead audio sink (invariant 4)"
    assert state.recording_stop_reasons["show"] == "write_failure_threshold"
    attempts_at_stop = handle.write_attempts
    for _ in range(3):
        state.broadcast_audio(block)
    assert handle.write_attempts == attempts_at_stop, "the auto-stopped sink is never written again"
    assert handle.close_count == 1


def test_recovery_resets_consecutive_counter():
    """T8 (rel-05 F4): one successful write resets the consecutive counter — only
    CONSECUTIVE failures stack toward the auto-stop (20 total / 10 consecutive)."""
    block = b"\x00" * 64
    _start_show_sink(FailingSinkHandle())
    for _ in range(10):
        state.broadcast_audio(block)
    assert wait_until(lambda: state.recording_write_errors["show"] == 10)
    _start_show_sink(SlowHandle(0.0))  # the disk recovered: rebuild the sink over a healthy handle
    state.broadcast_audio(block)
    assert wait_until(lambda: state.recording_write_errors["show"] == 0), "one success resets the consecutive counter"
    _start_show_sink(FailingSinkHandle())
    for _ in range(10):
        state.broadcast_audio(block)
    assert wait_until(lambda: state.recording_write_errors["show"] == 10)
    assert state.is_show_recording is True, "20 total / 10 consecutive failures stay below the stop threshold"


def test_concurrent_stop_wins_no_double_finalize(monkeypatch):
    """T9 (rel-05 F5, structural under single-owner finalize): after stop_show
    detached the sink, the raced 32nd failure is still counted but never
    re-finalizes the handle nor writes the stop-reason flag."""
    sink_module = _recording_sink_module()
    handle = FailingSinkHandle()
    sink = _start_show_sink(handle)
    state.recording_write_errors["show"] = RECORDING_WRITE_FAILURE_STOP_THRESHOLD - 1
    finalized = []
    monkeypatch.setattr(sink_module, "finalize_wav", lambda being_finalized: finalized.append(being_finalized))

    assert shows_routes._stop_show_recording(7) is sink, "stop_show detaches and returns the sink object"
    assert sink.stop_and_finalize() is True, "the detached sink finalizes exactly once via its writer"
    assert finalized == [handle]

    sink._note_failure(OSError(28, "No space left on device"))  # the raced 32nd failure lands late
    assert state.recording_write_errors["show"] == RECORDING_WRITE_FAILURE_STOP_THRESHOLD, (
        "the late failure is still counted"
    )
    assert finalized == [handle], "no double finalize — the raced loser never touches the handle again"
    assert state.recording_stop_reasons["show"] is None, "no flag writes after the slot changed hands"
    assert state.is_show_recording is False and state.current_show_sink is None


# ---------------------------------------------------------------------------
# REL-22 — the shutdown close path finalizes WAVs and ends the live Show row
# ---------------------------------------------------------------------------


def test_trigger_shutdown_finalizes_wav_and_ends_show_row(tmp_path):
    """T10 (REL-22 acceptance): trigger_shutdown drains+finalizes the recording
    WAV, ends the live Show row (status/ended_at/duration), clears every slot,
    and a second pass is a no-op on the row."""
    show_id = _create_show(status="live", started_at=_naive_utc_now())
    path = tmp_path / "shutdown_take.wav"
    handle = open(path, "wb")
    write_wav_header(handle)
    sink = _start_show_sink(handle, show_id=show_id)
    block = b"\x00" * 8192
    for _ in range(3):
        state.broadcast_audio(block)
    assert wait_until(lambda: sink.status().bytes_written == 3 * len(block))
    state.trigger_shutdown()
    _assert_valid_finalized_wav(path, 3 * len(block))
    status, ended_at, duration = _load_show_row(show_id)
    assert status == "ended", "the shutdown close path must end the live Show row"
    assert ended_at is not None
    assert duration is not None and duration >= 0, "duration mirrors stop_show's started_at math"
    _assert_recording_slots_cleared()
    snapshot = _load_show_row(show_id)
    state.trigger_shutdown()  # second pass: nothing left to detach or end
    assert _load_show_row(show_id) == snapshot, "a second shutdown must not touch the ended row"


def test_shutdown_without_show_skips_db(monkeypatch):
    """T11: with no current_show_id the shutdown close path never calls
    end_live_show_row — the DB write is skipped entirely."""
    sink_module = _recording_sink_module()
    calls: list[int] = []
    monkeypatch.setattr(sink_module, "end_live_show_row", lambda show_id: calls.append(show_id))
    assert state.current_show_id is None
    state.trigger_shutdown()
    assert calls == [], "no live show id — the DB write must be skipped"
    assert state.shutdown_event.is_set() and state.is_running is False, "shutdown otherwise runs normally"


def test_end_live_show_row_only_touches_live_rows():
    """T12: end_live_show_row is load-conditional — an already-ended row (and a
    nonexistent id) return False with no field writes (idempotency guard)."""
    end_live_show_row = _recording_sink_module().end_live_show_row
    ended_id = _create_show(status="ended")
    assert end_live_show_row(ended_id) is False, "an ended row must not be re-ended"
    assert _load_show_row(ended_id) == ("ended", None, None), "fields unchanged"
    assert end_live_show_row(10**9) is False, "a nonexistent id is a no-op"


# ---------------------------------------------------------------------------
# REL-20 — instruments.json writes run outside sync_lock
# ---------------------------------------------------------------------------


def test_instruments_write_outside_sync_lock(monkeypatch, isolated_instruments):
    """T13 (REL-20 acceptance): json.dump during save_instruments must observe
    sync_lock NOT held — the disk write happens outside the critical section."""
    probe = LockProbe()
    monkeypatch.setattr(state, "sync_lock", probe)
    held_during_dump: list[bool] = []
    real_dumps = json.dumps

    def slow_dump(obj, fh, **kwargs):
        held_during_dump.append(probe.held)
        time.sleep(0.15)  # a slow instruments.json disk write
        fh.write(real_dumps(obj, **kwargs))

    monkeypatch.setattr(json, "dump", slow_dump)

    state.add_custom_instrument("ProbeSynth")

    assert held_during_dump == [False], "the instruments.json write must run OUTSIDE sync_lock (REL-20)"
    content = Path(isolated_instruments).read_text()
    assert "ProbeSynth" in content, "the payload must still be written correctly"
    assert "ProbeSynth" in state.categorized_instruments["Custom"], "the in-memory catalog is updated"


def test_concurrent_custom_instrument_adds_serialize(isolated_instruments):
    """T14 (REL-20 guardrail): 4 concurrent add_custom_instrument calls must all
    survive to the file — payloads snapshot under the lock, so no lost update."""
    names = [f"ThreadSynth{index}" for index in range(4)]
    failures: list[Exception] = []

    def add(name: str) -> None:
        try:
            state.add_custom_instrument(name)
        except Exception as exc:  # noqa: BLE001 — surfaced via the assertion below
            failures.append(exc)

    threads = [threading.Thread(target=add, args=(name,)) for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)
    assert failures == [], f"concurrent adds raised: {failures!r}"
    content = Path(isolated_instruments).read_text()
    for name in names:
        assert name in content, f"lost update: {name} missing from instruments.json"


# ---------------------------------------------------------------------------
# Telemetry — sink.status() and /api/health expose the drop counter
# ---------------------------------------------------------------------------


def test_sink_status_and_health_telemetry():
    """T15: status() reports the per-sink counters, and /api/health's recording
    payload gains the additive dropped_bytes field per sink (REL-11 soak hook)."""
    handle = SlowHandle(0.0)
    sink = _start_show_sink(handle)
    block = b"\x07" * 128
    state.broadcast_audio(block)
    assert wait_until(lambda: sink.status().bytes_written == len(block))
    live = sink.status()
    assert (live.sink_name, live.active, live.bytes_written, live.dropped_blocks, live.dropped_bytes) == (
        "show",
        True,
        len(block),
        0,
        0,
    )
    payload = TestClient(app).get("/api/health").json()
    show_health = payload["recording"]["show"]
    assert show_health["active"] is True
    assert show_health["dropped_bytes"] == 0, "/api/health must expose the REL-11 drop counter"
