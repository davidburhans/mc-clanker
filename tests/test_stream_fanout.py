"""REL-10 regression suite — process-wide MP3 stream fan-out (unit 8).

Contract under test (TDD red until ``app/stream_fanout.py`` lands):

* ONE ffmpeg transcoder serves every ``/stream.mp3`` client (REL-10a): clients
  acquire a session (a bounded queue) and own no subprocess.
* Abrupt disconnects leak nothing (REL-10b/c): a session whose consumer never
  drains is evicted after ``stale_client_s`` of continuous fullness and
  poisoned with ``_STOP_SENTINEL`` so the parked generator frame unwinds; the
  singleton tears down when the last session leaves.
* Transcoder death mid-stream restarts cleanly and never gives up permanently
  (REL-15 lesson; the restart loop is bounded by the client refcount).
* Shutdown stays zombie-free through three redundant paths: the PCM-queue
  ``None`` poison, the ``is_running`` gate + last-resort teardown, and the
  registered-subprocess kill list (pinned with a FULL PCM queue so the poison
  is dropped — decision 9c of the unit plan).

Fakes mirror ``tests/test_youtube_relay.py``: FakeProc/FakeStdin stand in for
Popen; FakeStdout/FakeStderr model blocking pipes honestly — ``read`` sleeps
in 10 ms slices and returns ``b""`` ONLY on real EOF (``die()``/``kill()``/
stdin close), never on a poll timeout, because a fabricated ``b""`` would fake
a process death (load-bearing for the restart tests T16/T17).

Test-authored seams the implementation must honour (documented latitude):

* ``StreamFanout(cfg, global_state)`` is directly constructible, relay-style;
  ``get_stream_fanout(state, cfg=None)`` / ``acquire_stream_client(state,
  cfg=None)`` accept an optional FanoutConfig — production callers use the
  defaults, tests inject fast configs directly (plan §2.4).
* Teardown must release the last client PROMPTLY even with the pump parked in
  a blocking stdout read (T5 measures < 2 s): a stop-aware ``_read_stdout``
  (poll slices honoring stop_event/is_running) or terminate-before-join both
  satisfy it. A literal join(5)-then-terminate would stall every disconnect by
  ~5 s — exactly the REL-10 latency class this unit removes.
* Closing stdin finalizes the (fake) transcoder: real ffmpeg exits once its
  ``pipe:0`` hits EOF, so teardown's close→wait reaps it without a kill.
"""

import collections
import gc
import itertools
import os
import queue
import threading
import time
from types import SimpleNamespace

import pytest
from app.stream_fanout import (
    _STOP_SENTINEL,
    FanoutConfig,
    FanoutInactive,
    FanoutStatus,
    StreamFanout,
    acquire_stream_client,
    build_mp3_args,
    get_stream_fanout,
    mp3_client_stream,
    resolve_ffmpeg_exe,
)
from fastapi.testclient import TestClient

from app.framework.framework_state import state

# ---------------------------------------------------------------------------
# Fakes (relay-shaped: FakeProc/FakeStdin from tests/test_youtube_relay.py,
# extended with honest blocking FakeStdout/FakeStderr for the pump/drain).
# ---------------------------------------------------------------------------

_FAKE_PID = itertools.count(4000)


class FakeStdin:
    """Collects written bytes; close() finalizes the fake process (EOF exit)."""

    def __init__(self, proc: "FakeProc") -> None:
        self.buffer = bytearray()
        self.closed = False
        self.fail_writes = False
        self._proc = proc

    def write(self, data: bytes) -> None:
        if self.closed or self.fail_writes:
            raise BrokenPipeError("fake broken pipe")
        self.buffer.extend(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        # Real ffmpeg finalizes and exits once its stdin hits EOF (pipe:0).
        self._proc._mark_dead(0)


class FakeStdout:
    """Blocking pipe stand-in: read(n) parks in 10 ms slices until data/EOF.

    EOF is honest: only end_output() (die/kill/stdin-close) yields ``b""``; a
    poll timeout never fabricates a death.
    """

    def __init__(self) -> None:
        self._blocks: collections.deque[bytes] = collections.deque()
        self._lock = threading.Lock()
        self._eof = threading.Event()
        self.closed = False

    def push(self, block: bytes) -> None:
        with self._lock:
            self._blocks.append(block)

    def end_output(self) -> None:
        self._eof.set()

    def close(self) -> None:
        self.closed = True
        self.end_output()

    def read(self, n: int = -1) -> bytes:
        while True:
            with self._lock:
                if self._blocks:
                    block = self._blocks.popleft()
                    if 0 <= n < len(block):
                        self._blocks.appendleft(block[n:])
                        return block[:n]
                    return block
                if self._eof.is_set():
                    return b""
            time.sleep(0.01)


class FakeStderr:
    """Line-oriented pipe stand-in for the stderr drain thread."""

    def __init__(self, text: bytes = b"") -> None:
        self._lines: collections.deque[bytes] = collections.deque(text.splitlines(keepends=True))
        self._lock = threading.Lock()
        self._eof = threading.Event()

    def end_output(self) -> None:
        self._eof.set()

    def readline(self) -> bytes:
        while True:
            with self._lock:
                if self._lines:
                    return self._lines.popleft()
                if self._eof.is_set():
                    return b""
            time.sleep(0.01)

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        line = self.readline()
        if line == b"":
            raise StopIteration
        return line


class FakeProc:
    """Popen stand-in; die()/kill() end the output pipes like a real crash."""

    def __init__(self, argv, stderr_text: bytes = b"", **kwargs) -> None:
        self.argv = argv
        self.pid = next(_FAKE_PID)
        self.stdin = FakeStdin(self)
        self.stdout = FakeStdout()
        self.stderr = FakeStderr(stderr_text)
        self.returncode = None
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None) -> int:
        return self.returncode if self.returncode is not None else 0

    def kill(self) -> None:
        self.killed = True
        self._mark_dead(-9)

    def die(self, code: int = 1) -> None:
        """Simulate a mid-stream crash: broken stdin + EOF on both outputs."""
        self._mark_dead(code)
        self.stdin.fail_writes = True

    def _mark_dead(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
        self.stdout.end_output()
        self.stderr.end_output()


class PopenRecorder:
    """List-like recorder of created FakeProcs + knobs for the next spawn."""

    def __init__(self) -> None:
        self.created: list[FakeProc] = []
        self.stderr_text: bytes = b""
        self.fail_next_spawn = False
        self.spawn_error: Exception = OSError("no ffmpeg")

    def __getitem__(self, index: int) -> FakeProc:
        return self.created[index]

    def __len__(self) -> int:
        return len(self.created)


@pytest.fixture
def fake_popen(monkeypatch):
    """Patch Popen inside the fanout module; returns the created-proc recorder."""
    recorder = PopenRecorder()

    def _popen(argv, **kwargs) -> FakeProc:
        if recorder.fail_next_spawn:
            recorder.fail_next_spawn = False
            raise recorder.spawn_error
        proc = FakeProc(argv, stderr_text=recorder.stderr_text, **kwargs)
        recorder.created.append(proc)
        return proc

    monkeypatch.setattr("app.stream_fanout.subprocess.Popen", _popen)
    return recorder


@pytest.fixture
def fake_ffmpeg_exe(monkeypatch):
    """Hermetic ffmpeg: fixed resolved name, libmp3lame probe answers success."""
    monkeypatch.setattr("app.stream_fanout.resolve_ffmpeg_exe", lambda: "ffmpeg")
    probe = SimpleNamespace(stdout="... libmp3lame ...", returncode=0)
    monkeypatch.setattr("app.stream_fanout.subprocess.run", lambda *args, **kwargs: probe)
    return "ffmpeg"


@pytest.fixture(autouse=True)
def reset_fanout_state():
    """Isolate stream state; stop any fanout left running by a test."""

    def _isolate() -> None:
        fanout = getattr(state, "stream_fanout", None)
        if fanout is not None:
            fanout._teardown()
        state.stream_fanout = None
        state.audio_clients = []
        state.is_running = True
        state.shutdown_event.clear()
        state.active_subprocesses.clear()
        state.dj_password = ""
        state.audience_password = ""

    _isolate()
    yield
    _isolate()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_cfg(**overrides) -> FanoutConfig:
    """Fast test defaults; production defaults live in FanoutConfig itself."""
    params = dict(
        pcm_queue_blocks=8,
        client_queue_blocks=4,
        client_poll_s=0.02,
        queue_poll_s=0.02,
        stale_client_s=10.0,
        restart_backoff_s=0.0,
        restart_backoff_cap_s=0.5,
    )
    params.update(overrides)
    return FanoutConfig(**params)


def make_fanout(**overrides) -> StreamFanout:
    return StreamFanout(make_cfg(**overrides), state)


def wait_until(cond, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def queue_items(q) -> list:
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            return items


def fanout_threads_alive() -> int:
    return sum(1 for thread in threading.enumerate() if thread.name.startswith("StreamFanout"))


def force_stale_eviction(fanout: StreamFanout, proc, timeout: float = 5.0) -> None:
    """Keep the sole client's queue full until the stale reaper evicts it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and fanout.status().client_count == 1:
        proc.stdout.push(b"stale-bait\x00")
        time.sleep(0.01)
    assert fanout.status().client_count == 0, "sole client was not evicted within the stale window"


def wait_for_teardown(fanout: StreamFanout, proc, timeout: float = 8.0) -> None:
    """Assert the full post-teardown state (proc reaped, queues/threads gone)."""
    assert wait_until(lambda: proc.poll() is not None, timeout), "transcoder outlived teardown"
    assert proc.stdin.closed, "teardown left the transcoder stdin open"
    assert state.audio_clients == [], "PCM queue left registered after teardown"
    assert wait_until(lambda: fanout_threads_alive() == 0, timeout), "fanout threads outlived teardown"
    assert not fanout.status().active
    assert fanout.is_stopping


def push_first_block_later(recorder: PopenRecorder, block: bytes) -> threading.Timer:
    """Timer thread that pushes once the fake transcoder exists (never hangs)."""

    def _push() -> None:
        if wait_until(lambda: len(recorder) >= 1, timeout=5.0):
            recorder[0].stdout.push(block)

    timer = threading.Timer(0.0, _push)
    timer.daemon = True
    return timer


# ---------------------------------------------------------------------------
# Argv / exe parity with the legacy per-client generator
# ---------------------------------------------------------------------------


class TestMp3ArgsParity:
    def test_mp3_args_match_legacy_argv(self):
        """T1: default cfg reproduces today's per-client ffmpeg argv exactly."""
        assert build_mp3_args(FanoutConfig()) == [
            "ffmpeg",
            "-y",
            "-f",
            "s16le",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-i",
            "pipe:0",
            "-f",
            "mp3",
            "-acodec",
            "libmp3lame",
            "-b:a",
            "192k",
            "pipe:1",
        ]

    def test_mp3_args_follow_config(self):
        """The cfg fields, not hardcoding, drive -ar/-ac/-b:a."""
        args = build_mp3_args(FanoutConfig(bitrate_kbps=128, sample_rate=22050, channels=1))
        assert args[args.index("-ar") + 1] == "22050"
        assert args[args.index("-ac") + 1] == "1"
        assert args[args.index("-b:a") + 1] == "128k"

    def test_resolve_ffmpeg_exe_fallback(self, monkeypatch):
        """T2: legacy discovery — /usr/bin/ffmpeg when present, PATH name else."""
        monkeypatch.setattr(os.path, "exists", lambda path: False)
        assert resolve_ffmpeg_exe() == "ffmpeg"
        monkeypatch.setattr(os.path, "exists", lambda path: True)
        assert resolve_ffmpeg_exe() == "/usr/bin/ffmpeg"


# ---------------------------------------------------------------------------
# Singleton lifecycle (REL-10a / REL-10c)
# ---------------------------------------------------------------------------


class TestSingletonLifecycle:
    def test_first_acquire_starts_one_ffmpeg(self, fake_popen, fake_ffmpeg_exe):
        """T3: the first client's acquire spawns the shared transcoder."""
        fanout = make_fanout()
        session = fanout.acquire_client()
        try:
            assert len(fake_popen) == 1
            assert fake_popen[0].argv[-1] == "pipe:1"
            assert fake_popen[0] in state.active_subprocesses
            assert fanout._pcm_queue in state.audio_clients
            status = fanout.status()
            assert status.active
            assert status.client_count == 1
            assert status.process_alive
            assert not fanout.is_stopping
        finally:
            fanout.release_client(session)

    def test_n_clients_share_one_subprocess(self, fake_popen, fake_ffmpeg_exe):
        """T4 (acceptance): three clients, ONE transcoder, identical bytes each."""
        fanout = make_fanout()
        sessions = [fanout.acquire_client() for _ in range(3)]
        try:
            assert len(fake_popen) == 1, "each client spawned its own ffmpeg (REL-10a)"
            fake_popen[0].stdout.push(b"MP3BLOCK")
            for session in sessions:
                assert wait_until(lambda s=session: s.queue.qsize() >= 1)
                assert session.queue.get_nowait() == b"MP3BLOCK"
            assert fanout.status().client_count == 3
        finally:
            for session in sessions:
                fanout.release_client(session)

    def test_release_last_tears_down_singleton(self, fake_popen, fake_ffmpeg_exe):
        """T5 (acceptance): last release reaps proc, queues, threads, singleton."""
        fanout = get_stream_fanout(state, cfg=make_cfg())
        first = fanout.acquire_client()
        second = fanout.acquire_client()
        fanout.release_client(first)
        assert len(fake_popen) == 1, "intermediate release must not tear down"
        assert fanout.status().active
        proc = fake_popen[0]
        started = time.monotonic()
        fanout.release_client(second)
        elapsed = time.monotonic() - started
        # A disconnect must not stall the closing thread: the pump may be parked
        # in a blocking stdout read, so teardown has to wake it (stop-aware read
        # or terminate-before-join) instead of joining it blind for 5 s.
        assert elapsed < 2.0, f"last release stalled {elapsed:.2f}s with the pump parked"
        assert state.stream_fanout is None, "retired singleton left on state"
        wait_for_teardown(fanout, proc)

    def test_release_is_idempotent_per_session(self, fake_popen, fake_ffmpeg_exe):
        """T6: double release and release-after-eviction are silent no-ops."""
        fanout = make_fanout(stale_client_s=0.05)
        first = fanout.acquire_client()
        second = fanout.acquire_client()
        fanout.release_client(first)
        fanout.release_client(first)  # must not raise / must not re-teardown
        assert fanout.status().active
        assert fanout.status().client_count == 1
        force_stale_eviction(fanout, fake_popen[0])
        fanout.release_client(second)  # already evicted — no-op, no raise
        wait_for_teardown(fanout, fake_popen[0])

    def test_acquire_on_stopping_object_retries_factory(self, fake_popen, fake_ffmpeg_exe):
        """T7 / decision 2: FanoutInactive → retire → a fresh singleton wins."""
        stale = get_stream_fanout(state, cfg=make_cfg())
        stale._stopping = True  # force the teardown-in-progress race window
        with pytest.raises(FanoutInactive):
            stale.acquire_client()
        acquired = acquire_stream_client(state, cfg=make_cfg())
        assert acquired is not None, "factory retry exhausted on a live system"
        fresh, session = acquired
        assert fresh is not stale, "factory handed back the retiring object"
        assert state.stream_fanout is fresh
        assert fresh.status().client_count == 1
        assert len(fake_popen) == 1, "retiring a never-started fanout must not spawn"
        fresh.release_client(session)


# ---------------------------------------------------------------------------
# PCM → MP3 data flow
# ---------------------------------------------------------------------------


class TestPcmToMp3Flow:
    def test_pcm_flows_from_broadcast_to_ffmpeg_stdin(self, fake_popen, fake_ffmpeg_exe):
        """T8: mixer broadcast_audio is the PCM source (registered queue)."""
        fanout = make_fanout()
        session = fanout.acquire_client()
        try:
            pcm = b"\xde\xad\xbe\xef" * 4
            state.broadcast_audio(pcm)
            assert wait_until(lambda: pcm in bytes(fake_popen[0].stdin.buffer))
        finally:
            fanout.release_client(session)

    def test_transcoder_death_respawns_and_stream_continues(self, fake_popen, fake_ffmpeg_exe):
        """T16 (acceptance): death mid-stream respawns; clients keep their bytes."""
        fanout = make_fanout()
        session = fanout.acquire_client()
        try:
            fake_popen[0].stdout.push(b"mp3-pre")
            assert wait_until(lambda: session.queue.qsize() >= 1)
            assert session.queue.get_nowait() == b"mp3-pre"
            fake_popen[0].die(code=1)
            assert wait_until(lambda: len(fake_popen) == 2), "dead transcoder did not respawn"
            state.broadcast_audio(b"pcm-after-restart")
            assert wait_until(lambda: b"pcm-after-restart" in bytes(fake_popen[1].stdin.buffer))
            fake_popen[1].stdout.push(b"mp3-post")
            assert wait_until(lambda: session.queue.qsize() >= 1)
            assert session.queue.get_nowait() == b"mp3-post"
            assert fanout.status().restarts == 1
            assert fanout.status().client_count == 1, "respawn must not disturb attached clients"
        finally:
            fanout.release_client(session)


# ---------------------------------------------------------------------------
# Client generator contract (clean close, GC close, parked frames)
# ---------------------------------------------------------------------------


class TestClientGeneratorContract:
    def test_clean_generator_close_releases(self, fake_popen, fake_ffmpeg_exe):
        """T9 (acceptance): closing the generator releases the session cleanly."""
        fanout = get_stream_fanout(state, cfg=make_cfg(client_poll_s=0.05))
        pusher = push_first_block_later(fake_popen, b"mp3-1")
        pusher.start()
        gen = mp3_client_stream(state)  # joins the pre-made singleton
        try:
            assert next(gen) == b"mp3-1"
        finally:
            gen.close()
        assert state.stream_fanout is None, "last client left → singleton must retire"
        wait_for_teardown(fanout, fake_popen[0])

    def test_generator_gc_close_releases(self, fake_popen, fake_ffmpeg_exe):
        """T10: a dropped, unclosed generator releases via GC close (abandon path)."""
        fanout = get_stream_fanout(state, cfg=make_cfg(client_poll_s=0.05))
        pusher = push_first_block_later(fake_popen, b"mp3-gc")
        pusher.start()
        gen = mp3_client_stream(state)
        assert next(gen) == b"mp3-gc"
        del gen
        gc.collect()
        assert wait_until(lambda: state.stream_fanout is None, timeout=8), "GC'd generator never released"
        wait_for_teardown(fanout, fake_popen[0])

    def test_parked_generator_unwinds_on_eviction_sentinel(self, fake_popen, fake_ffmpeg_exe):
        """T12 / decision 5: the eviction sentinel ends an abandoned client frame."""
        fanout = get_stream_fanout(state, cfg=make_cfg(client_poll_s=0.05, stale_client_s=0.05))
        finished = threading.Event()

        def abandoned_client():
            gen = mp3_client_stream(state)
            next(gen)  # consume one block, then abandon the frame like Starlette does
            if wait_until(lambda: fanout.status().client_count == 0, timeout=5):
                try:
                    next(gen)  # the late resume: sentinel must end the loop, not hang it
                except StopIteration:
                    pass
            finished.set()

        pusher = push_first_block_later(fake_popen, b"first")
        pusher.start()
        thread = threading.Thread(target=abandoned_client, daemon=True)
        thread.start()
        assert wait_until(lambda: len(fake_popen) == 1)
        assert wait_until(lambda: fanout.status().client_count == 1)
        force_stale_eviction(fanout, fake_popen[0])
        assert finished.wait(5.0), "abandoned generator did not unwind on the sentinel"
        thread.join(timeout=2.0)
        assert not thread.is_alive(), "abandoned generator thread is still parked"
        wait_for_teardown(fanout, fake_popen[0])

    def test_is_running_false_exits_and_tears_down_with_clients_attached(
        self, fake_popen, fake_ffmpeg_exe
    ):
        """T19 / decision 9b: is_running=False tears down under attached clients."""
        fanout = get_stream_fanout(state, cfg=make_cfg(client_poll_s=0.05))
        pusher = push_first_block_later(fake_popen, b"pre")
        pusher.start()
        gen = mp3_client_stream(state)
        assert next(gen) == b"pre"
        state.is_running = False
        assert wait_until(lambda: state.stream_fanout is None, timeout=8)
        wait_for_teardown(fanout, fake_popen[0])
        # the still-attached generator unwinds instead of hanging forever
        with pytest.raises(StopIteration):
            next(gen)
        assert wait_until(lambda: fanout_threads_alive() == 0, timeout=8)


# ---------------------------------------------------------------------------
# Stale-client reaper (REL-10b/c): the abandoned-frame watchdog, absorbed
# ---------------------------------------------------------------------------


class TestStaleClientReaper:
    def test_abandoned_session_evicted_after_stale_window(self, fake_popen, fake_ffmpeg_exe):
        """T11 (acceptance): a full, never-drained session is evicted and poisoned."""
        fanout = get_stream_fanout(state, cfg=make_cfg(stale_client_s=0.05))
        session = fanout.acquire_client()  # never released — the parked frame
        assert wait_until(lambda: len(fake_popen) == 1)
        force_stale_eviction(fanout, fake_popen[0])
        assert fanout.status().evicted_clients == 1
        assert session.queue.get_nowait() is _STOP_SENTINEL, "eviction must push the stop sentinel"
        assert state.stream_fanout is None, "last client evicted → singleton must retire"
        wait_for_teardown(fanout, fake_popen[0])

    def test_k_abrupt_kills_leave_no_zombies(self, fake_popen, fake_ffmpeg_exe):
        """T13 (acceptance, soak #6 proxy): K abandon-evict cycles stay clean."""
        k = 5
        for cycle in range(k):
            acquired = acquire_stream_client(state, cfg=make_cfg(stale_client_s=0.05))
            assert acquired is not None
            fanout, session = acquired
            assert wait_until(lambda c=cycle: len(fake_popen) == c + 1)
            force_stale_eviction(fanout, fake_popen[cycle])
            assert wait_until(lambda: state.audio_clients == [], timeout=8)
            assert fanout.status().client_count == 0
            assert session.queue.get_nowait() is _STOP_SENTINEL
        assert len(fake_popen) == k, "each generation needs exactly one transcoder"
        for proc in fake_popen.created:
            assert proc.poll() is not None, "zombie transcoder survived a cycle"
            assert proc.stdin.closed
        assert state.audio_clients == []
        assert state.active_subprocesses == set()
        assert wait_until(lambda: fanout_threads_alive() == 0, timeout=8)

    def test_slow_draining_client_not_evicted(self, fake_popen, fake_ffmpeg_exe):
        """T14: a consumer that keeps draining resets the stale clock (no false kill)."""
        fanout = make_fanout(stale_client_s=0.2)
        session = fanout.acquire_client()
        try:
            started = time.monotonic()
            for _ in range(15):
                fake_popen[0].stdout.push(b"trickle\x00")
                assert wait_until(lambda: session.queue.qsize() >= 1, timeout=2)
                session.queue.get_nowait()  # the "slow" consumer drains its block
                time.sleep(0.03)
            assert time.monotonic() - started > 0.2, "test never crossed the stale window"
            assert fanout.status().client_count == 1, "live consumer was evicted"
            assert fanout.status().evicted_clients == 0
        finally:
            fanout.release_client(session)

    def test_drop_oldest_never_blocks_and_keeps_newest(self, fake_popen, fake_ffmpeg_exe):
        """T15 (acceptance): overflow drops oldest inline; the pump never blocks."""
        fanout = make_fanout(client_queue_blocks=4)
        session = fanout.acquire_client()
        try:
            blocks = [bytes([65 + i]) * 8 for i in range(8)]  # 2 × maxsize
            started = time.monotonic()
            for block in blocks:
                fake_popen[0].stdout.push(block)
            assert wait_until(lambda: session.queue.qsize() == 4, timeout=5)
            elapsed = time.monotonic() - started
            assert elapsed < 1.0, f"drop-oldest delivery stalled the pump {elapsed:.2f}s"
            assert queue_items(session.queue) == blocks[4:], "oldest must go, newest kept in order"
            assert fanout.status().dropped_client_blocks == 4
        finally:
            fanout.release_client(session)


# ---------------------------------------------------------------------------
# Transcoder resilience: respawn, no permanent give-up (REL-15 lesson), stderr
# ---------------------------------------------------------------------------


class TestTranscoderResilience:
    def test_no_permanent_give_up(self, fake_popen, fake_ffmpeg_exe):
        """T17 / decision 4: restarts are never exhausted while clients remain."""
        fanout = make_fanout(restart_backoff_s=0.0, restart_backoff_cap_s=0.1)
        session = fanout.acquire_client()
        try:
            for expected in range(1, 6):
                fake_popen[expected - 1].die(code=1)
                assert wait_until(lambda e=expected: len(fake_popen) == e + 1)
            status = fanout.status()
            assert status.active
            assert status.restarts == 5
            assert status.client_count == 1, "restarts must not disturb clients"
            assert "gave up" not in status.last_error
            fake_popen[5].stdout.push(b"still-streaming")
            assert wait_until(lambda: session.queue.qsize() >= 1)
        finally:
            fanout.release_client(session)

    def test_stderr_tail_kept_on_death(self, fake_popen, fake_ffmpeg_exe):
        """T26 / decision 3: the stderr tail explains WHY the transcoder died."""
        fake_popen.stderr_text = b"[mp3lame @ 0x1f] encoder exploded\n"
        fanout = make_fanout()
        session = fanout.acquire_client()
        try:
            time.sleep(0.05)  # let the drain thread pocket the stderr line
            fake_popen[0].die(code=1)
            assert wait_until(lambda: "encoder exploded" in fanout.status().last_error, timeout=5)
        finally:
            fanout.release_client(session)


# ---------------------------------------------------------------------------
# Shutdown matrix — any single dropped signal must still reap the transcoder
# ---------------------------------------------------------------------------


class TestShutdownMatrix:
    def test_poison_pill_stops_feeder_and_tears_down(self, fake_popen, fake_ffmpeg_exe):
        """T18 / decision 9a: a None poison in the PCM queue is sufficient."""
        fanout = make_fanout()
        session = fanout.acquire_client()
        fanout._pcm_queue.put_nowait(None)
        assert wait_until(lambda: state.audio_clients == [], timeout=8)
        wait_for_teardown(fanout, fake_popen[0])
        assert wait_until(lambda: session.queue.qsize() >= 1, timeout=2)
        assert session.queue.get_nowait() is _STOP_SENTINEL, "teardown must poison remaining sessions"

    def test_trigger_shutdown_with_full_pcm_queue_still_no_zombie(self, fake_popen, fake_ffmpeg_exe):
        """T20 / decision 9c: the kill list reaps even when the poison is dropped."""
        fanout = make_fanout(pcm_queue_blocks=2)
        fanout.acquire_client()
        assert wait_until(lambda: len(fake_popen) == 1)
        proc = fake_popen[0]
        for _ in range(6):  # keep the PCM queue full so the None poison is dropped
            state.broadcast_audio(b"\x01" * 64)
        state.trigger_shutdown()
        assert wait_until(lambda: proc.poll() is not None, timeout=8), "zombie transcoder"
        assert wait_until(lambda: state.audio_clients == [], timeout=8)
        assert state.is_running is False

    def test_proc_unregistered_from_kill_list_on_teardown(self, fake_popen, fake_ffmpeg_exe):
        """T21: teardown unregisters the transcoder from the shutdown kill list."""
        fanout = make_fanout()
        session = fanout.acquire_client()
        assert fake_popen[0] in state.active_subprocesses
        fanout.release_client(session)
        assert wait_until(lambda: state.active_subprocesses == set(), timeout=8)


# ---------------------------------------------------------------------------
# Spawn failure (round-3 D10 successor), concurrency hammer, telemetry
# ---------------------------------------------------------------------------


class TestSpawnFailureAndConcurrency:
    def test_spawn_failure_yields_empty_stream_and_leaks_nothing(self, fake_popen, fake_ffmpeg_exe):
        """T22 / D10 successor: Popen failure → empty body, zero leaks, retired."""
        fake_popen.fail_next_spawn = True
        assert list(mp3_client_stream(state)) == []
        assert len(fake_popen) == 0
        assert state.audio_clients == []
        assert state.stream_fanout is None, "failed singleton must not stay on state"
        assert fanout_threads_alive() == 0
        assert state.active_subprocesses == set()

    def test_concurrent_acquire_release_hammer(self, fake_popen, fake_ffmpeg_exe):
        """T23 / decision 2: the factory retry closes the retire/replace race."""
        cfg = make_cfg(client_poll_s=0.01, queue_poll_s=0.01, stale_client_s=0.5)
        errors: list[str] = []
        stats = {"max_live": 0}
        stats_lock = threading.Lock()

        def worker() -> None:
            try:
                for _ in range(10):
                    acquired = acquire_stream_client(state, cfg=cfg)
                    if acquired is None:
                        errors.append("acquire_stream_client returned None")
                        return
                    fanout, session = acquired
                    with stats_lock:
                        live = sum(1 for proc in fake_popen.created if proc.poll() is None)
                        stats["max_live"] = max(stats["max_live"], live)
                    time.sleep(0.001)
                    fanout.release_client(session)
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30.0)
        assert not any(thread.is_alive() for thread in threads), "hammer deadlocked"
        assert errors == []
        assert stats["max_live"] <= 1, "more than one live transcoder existed"
        assert state.audio_clients == []
        assert all(proc.poll() is not None for proc in fake_popen.created)
        assert wait_until(lambda: fanout_threads_alive() == 0, timeout=8)

    def test_status_telemetry_shape(self, fake_popen, fake_ffmpeg_exe):
        """T24: FanoutStatus carries the REL-10 observability contract."""
        fanout = make_fanout()
        session = fanout.acquire_client()
        try:
            clean = fanout.status()
            assert isinstance(clean, FanoutStatus)
            assert clean.active
            assert clean.client_count == 1
            assert clean.process_alive
            assert clean.started_at > 0
            assert clean.uptime_seconds >= 0
            assert clean.restarts == 0
            assert clean.dropped_pcm_blocks == 0
            assert clean.dropped_client_blocks == 0
            assert clean.evicted_clients == 0
            assert clean.bytes_fanned_out == 0
            assert clean.last_error == ""
            payload = b"counted-bytes"
            fake_popen[0].stdout.push(payload)
            assert wait_until(lambda: session.queue.qsize() >= 1)
            assert fanout.status().bytes_fanned_out == len(payload)
            fake_popen[0].die(code=1)
            assert wait_until(lambda: len(fake_popen) == 2)
            after = fanout.status()
            assert after.restarts == 1
            assert "exit=1" in after.last_error, "death must record the exit code"
            assert after.process_alive
        finally:
            fanout.release_client(session)


# ---------------------------------------------------------------------------
# Route-level regression guard: the live stream still serves bytes
# ---------------------------------------------------------------------------


class TestStreamRoute:
    def test_stream_route_serves_bytes_and_headers(self, fake_popen, fake_ffmpeg_exe):
        """T25 (acceptance): /stream.mp3 keeps its headers and body contract."""
        from app.app_ui import app as ui_app

        client = TestClient(ui_app)
        with client.stream("GET", "/stream.mp3") as response:
            assert response.status_code == 200
            assert response.headers["content-type"] == "audio/mpeg"
            assert response.headers["cache-control"] == "no-cache, no-store, must-revalidate"
            assert response.headers["pragma"] == "no-cache"
            assert response.headers["expires"] == "0"
            assert response.headers["accept-ranges"] == "bytes"
            assert wait_until(lambda: len(fake_popen) == 1, timeout=5), "route did not start the fanout"
            fake_popen[0].stdout.push(b"ROUTE-MP3-BYTES")
            state.trigger_shutdown()  # deterministic end: poison → generator break
            body = b"".join(response.iter_bytes())
        assert b"ROUTE-MP3-BYTES" in body
        assert wait_until(lambda: state.audio_clients == [], timeout=8)
        assert wait_until(lambda: state.stream_fanout is None, timeout=8)
        assert wait_until(lambda: fanout_threads_alive() == 0, timeout=8)
