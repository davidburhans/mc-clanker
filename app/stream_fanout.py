"""stream_fanout.py — Process-wide MP3 transcode fan-out for /stream.mp3 (REL-10).

One ffmpeg encodes the mixer's PCM once; every client gets a bounded queue of
the shared MP3 bytes and owns no subprocess. This replaces the legacy
per-client transcoder that spawned one ffmpeg + one feeder thread per listener
and leaked both on abrupt disconnects (REL-10a/b/c): a sync generator abandoned
by Starlette never runs its ``finally``, so cleanup cannot depend on client
cooperation — the pump thread evicts any session whose queue stayed full for
``stale_client_s`` and poisons it with ``_STOP_SENTINEL``, unwinding the parked
frame; the singleton tears down when the last session leaves.

Usage:
    return StreamingResponse(mp3_client_stream(state), media_type="audio/mpeg")

Restart policy: indefinite with capped linear backoff (REL-15's lesson:
permanent give-up is a bug); the loop is bounded because the singleton only
exists while clients exist. Subprocess mechanics live in
``stream_fanout_proc.TranscoderSupervisor``, the launch/telemetry contract in
``stream_fanout_args``.
"""

from __future__ import annotations

import itertools
import logging
import queue
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.stream_fanout_args import (
    FanoutConfig,
    FanoutStatus,
    _FanoutCounters,
    build_mp3_args,
    resolve_ffmpeg_exe,
)
from app.stream_fanout_proc import TranscoderSupervisor

if TYPE_CHECKING:
    from app.framework.framework_state import GlobalState

__all__ = [
    "FanoutConfig",
    "FanoutInactive",
    "FanoutStatus",
    "StreamFanout",
    "acquire_stream_client",
    "build_mp3_args",
    "get_stream_fanout",
    "mp3_client_stream",
    "resolve_ffmpeg_exe",
]

log = logging.getLogger(__name__)

#: Wake-up marker for per-client queues. ``None`` is the trigger_shutdown
#: poison (framework_state); the sentinel is ours (eviction/teardown). Both
#: terminate a client generator; the feeder treats both as stop (decision 9).
_STOP_SENTINEL = object()

_CLIENT_IDS = itertools.count(1)

_FACTORY_ATTEMPTS = 3  # bounded retries past a retiring singleton (decision 2)

#: Singleton creation/retirement on ``state.stream_fanout`` (decision 10).
_fanout_lock = threading.Lock()

class FanoutError(RuntimeError):
    """Lifecycle misuse or spawn failure in the stream fan-out."""


class FanoutInactive(FanoutError):
    """The object entered teardown; the factory must retry a fresh singleton."""


@dataclass(eq=False)
class _ClientSession:
    """One connected client: bounded queue + fullness clock (identity type:
    registry removal is by identity). The pump thread solely writes it."""

    client_id: int
    queue: queue.Queue
    full_since: float | None = None


def _drain_one(client_queue: queue.Queue) -> None:
    """Discard the oldest buffered item (drop-oldest overflow policy)."""
    try:
        client_queue.get_nowait()
    except queue.Empty:
        pass


def _drain_queue(client_queue: queue.Queue) -> None:
    """Empty a bounded queue without blocking (eviction/teardown helper)."""
    while True:
        try:
            client_queue.get_nowait()
        except queue.Empty:
            return


def _residual_blocks(client_queue: queue.Queue) -> Iterator[bytes]:
    """Yield data blocks queued ahead of a stop marker.

    Teardown never discards already-encoded bytes, and the route stays
    deterministic whichever lands last, a data block or the sentinel.
    """
    while True:
        try:
            block = client_queue.get_nowait()
        except queue.Empty:
            return
        if block is None or block is _STOP_SENTINEL:
            return
        yield block


class StreamFanout:
    """One shared MP3 transcoder + the per-client session registry.

    Single-use: ``_start`` once, ``_teardown`` once, then the object retires
    (``state.stream_fanout`` cleared) and the next client gets a fresh one from
    the factory — an abandoned old thread can never resurrect a dead
    generation (decision 2).

    Example:
        fanout = get_stream_fanout(state)
        session = fanout.acquire_client()
        ...
        fanout.release_client(session)
    """

    def __init__(self, cfg: FanoutConfig, global_state: GlobalState) -> None:
        self._cfg = cfg
        self._state = global_state
        self._counters = _FanoutCounters()
        self._pcm_queue: queue.Queue = queue.Queue(maxsize=cfg.pcm_queue_blocks)
        self._clients: list[_ClientSession] = []
        self._clients_lock = threading.Lock()
        self._stopping = False
        self._active = False
        self._start_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._supervisor = TranscoderSupervisor(cfg, self._counters, global_state, self._stop_requested)
        self._feeder_thread: threading.Thread | None = None
        self._pump_thread: threading.Thread | None = None
        self._started_at = 0.0

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def is_stopping(self) -> bool:
        """True once teardown has begun (test-and-set owner: ``_teardown``)."""
        return self._stopping

    @property
    def client_poll_s(self) -> float:
        """Client-generator read poll, exposed so the generator needs no
        private access into this object."""
        return self._cfg.client_poll_s

    def acquire_client(self) -> _ClientSession:
        """Reserve one client session; the first acquisition starts the transcoder.

        Raises:
            FanoutInactive: this object is retiring — the factory retries fresh.
        """
        with self._clients_lock:
            if self._stopping:
                raise FanoutInactive("fanout is shutting down; retry via the factory")
            need_start = not self._active
            session = _ClientSession(
                next(_CLIENT_IDS), queue.Queue(maxsize=self._cfg.client_queue_blocks)
            )
            self._clients.append(session)  # reserve the slot before the expensive start
        if need_start:
            try:
                self._start()
            except Exception:
                with self._clients_lock:
                    self._discard_client_locked(session)  # roll the reservation back
                # FU-4 (rel-10 review residual): _start can raise AFTER
                # supervisor.spawn() — discarding the reservation alone
                # strands a live transcoder on a singleton the factory then
                # merely _retire()s (an immortal orphan nobody references).
                # Funnel through the single idempotent stop path: proc killed
                # and reaped, kill-list entry dropped, PCM queue unregistered,
                # threads joined, singleton retired. Degrades to a no-op when
                # the spawn itself failed (nothing to tear down) and
                # early-returns if a concurrent teardown already began.
                self._teardown()
                raise
            self._teardown_if_start_orphaned()
        return session

    def release_client(self, session: _ClientSession) -> None:
        """Release one session (idempotent); the last release tears the singleton
        down. Runs in the client generator's ``finally`` — close(), GC or the
        post-eviction unwind."""
        with self._clients_lock:
            removed = self._discard_client_locked(session)
            last = removed and self._active and not self._clients
        if last:
            self._teardown()

    def status(self) -> FanoutStatus:
        """Snapshot telemetry (lock-scoped reads; never blocks on I/O)."""
        with self._clients_lock:
            client_count = len(self._clients)
        return FanoutStatus(
            active=self._active and not self._stopping,
            client_count=client_count,
            process_alive=self._supervisor.is_alive(),
            started_at=self._started_at,
            uptime_seconds=(time.time() - self._started_at) if self._started_at else 0.0,
            restarts=self._counters.restarts,
            dropped_pcm_blocks=self._counters.dropped_pcm_blocks,
            dropped_client_blocks=self._counters.dropped_client_blocks,
            evicted_clients=self._counters.evicted_clients,
            bytes_fanned_out=self._counters.bytes_fanned_out,
            last_error=self._counters.last_error,
        )

    # ------------------------------------------------------------------
    # Lifecycle (start / teardown)
    # ------------------------------------------------------------------

    def _stop_requested(self) -> bool:
        """Supervisor respawn guard: any stop signal counts (decision 9)."""
        return bool(
            self._stopping
            or self._stop_event.is_set()
            or not self._state.is_running
            or self._state.shutdown_event.is_set()
        )

    def _start(self) -> None:
        """Spawn the transcoder, register the PCM queue, start the threads.

        Serialized by ``_start_lock``; runs OUTSIDE ``_clients_lock`` so the
        reservation taken by ``acquire_client`` is never held across Popen.
        """
        with self._start_lock:
            if self._active:
                return
            if self._stopping:
                raise FanoutInactive("fanout is shutting down; retry via the factory")
            self._warn_if_no_libmp3lame()
            self._supervisor.spawn()
            self._state.add_audio_client(self._pcm_queue)
            self._active = True
            self._started_at = time.time()
            self._stop_event.clear()
            self._start_threads()

    def _teardown_if_start_orphaned(self) -> None:
        """Tear down when every reservation vanished during a slow ``_start``
        (the last client released while the transcoder was still spawning)."""
        with self._clients_lock:
            orphaned = self._active and not self._stopping and not self._clients
        if orphaned:
            self._teardown()

    def _teardown(self) -> None:
        """Single idempotent stop path; every entry point funnels here: last
        release, eviction-to-zero, feeder/pump loop exit.

        Effect order matters and is load-bearing for observers: the PCM queue
        is unregistered BEFORE the process dies (whoever sees the transcoder
        dead must also see the queue gone), and the registry is cleared LAST
        (an empty registry implies the full teardown — retire included).
        """
        with self._clients_lock:
            if self._stopping:
                return
            self._stopping = True
            sessions = list(self._clients)
        self._active = False
        self._poison_sessions(sessions)
        self._stop_event.set()
        self._wake_feeder()
        self._state.remove_audio_client(self._pcm_queue)
        self._supervisor.terminate()
        self._join_threads()
        self._retire()
        with self._clients_lock:
            self._clients.clear()
        log.info("stream fanout stopped after %ds", int(self.status().uptime_seconds))

    def _poison_sessions(self, sessions: list[_ClientSession]) -> None:
        """Hand every remaining session its stop sentinel (buffered MP3 kept)."""
        for session in sessions:
            self._poison_session(session, drop_buffered=False)

    def _wake_feeder(self) -> None:
        """Accelerant: a sentinel in the PCM queue ends the feeder's wait now;
        the stop flags remain the guaranteed wake-up (decision 9)."""
        try:
            self._pcm_queue.put_nowait(_STOP_SENTINEL)
        except queue.Full:
            pass

    def _join_threads(self) -> None:
        """Join feeder/pump, skipping the calling thread (teardown may run on
        either); bounded so a stuck thread cannot stall a disconnect."""
        current = threading.current_thread()
        for thread in (self._feeder_thread, self._pump_thread):
            if thread is not None and thread is not current:
                thread.join(timeout=5.0)

    def _retire(self) -> None:
        """Clear the singleton slot if this object still owns it (identity
        guard: never evict a factory-fresh replacement)."""
        with _fanout_lock:
            if self._state.stream_fanout is self:
                self._state.stream_fanout = None

    # ------------------------------------------------------------------
    # Process probe + threads
    # ------------------------------------------------------------------

    def _warn_if_no_libmp3lame(self) -> None:
        """One-time encoder probe per singleton (moved from the legacy
        per-client check — strictly fewer ``-codecs`` subprocesses)."""
        try:
            check = subprocess.run(
                [resolve_ffmpeg_exe(), "-codecs"], capture_output=True, text=True, timeout=5
            )
            if "libmp3lame" not in check.stdout:
                log.warning("ffmpeg does not have libmp3lame encoder. MP3 streaming may not work.")
        except Exception as exc:  # noqa: BLE001 - capability probe is best-effort
            log.warning("Could not verify ffmpeg capabilities: %s", exc)

    def _start_threads(self) -> None:
        """Start the feeder + pump daemons (the stderr thread is per-spawn)."""
        self._feeder_thread = threading.Thread(
            target=self._feeder_loop, daemon=True, name="StreamFanoutFeeder"
        )
        self._pump_thread = threading.Thread(
            target=self._pump_loop, daemon=True, name="StreamFanoutPump"
        )
        self._feeder_thread.start()
        self._pump_thread.start()

    def _pump_loop(self) -> None:
        """Read encoded blocks and fan them out; exit for any reason → the
        ``finally`` performs the last-resort teardown (shutdown path 9b)."""
        try:
            while not self._stop_event.is_set() and self._state.is_running:
                if not self._supervisor.ensure_alive():
                    break
                block = self._supervisor.read_mp3()
                if block:
                    self._fanout_block(block)
        finally:
            if not self._stopping:
                self._teardown()

    def _feeder_loop(self) -> None:
        """Drain the registered PCM queue into the transcoder. Never respawns;
        ``None`` (trigger_shutdown poison) and the sentinel both stop it."""
        try:
            while not self._stop_event.is_set() and self._state.is_running:
                try:
                    block = self._pcm_queue.get(timeout=self._cfg.queue_poll_s)
                except queue.Empty:
                    continue
                if block is None or block is _STOP_SENTINEL:
                    break
                self._supervisor.write_pcm(block)
        finally:
            if not self._stopping:
                self._teardown()

    # ------------------------------------------------------------------
    # Session delivery + stale-client reaper
    # ------------------------------------------------------------------

    def _fanout_block(self, block: bytes) -> None:
        """Deliver one encoded block to every session. The registry is
        snapshotted under the lock; delivery happens outside it (decision 6)."""
        with self._clients_lock:
            sessions = list(self._clients)
        for session in sessions:
            self._deliver_block(session, block)

    def _deliver_block(self, session: _ClientSession, block: bytes) -> None:
        """Drop-oldest delivery + stale-eviction bookkeeping. The pump thread
        is the single writer of ``full_since`` (decision 5)."""
        if self._enqueue(session, block):
            session.full_since = None  # consumer drained a slot since the last block
            return
        now = time.monotonic()
        if session.full_since is None:
            session.full_since = now
        elif now - session.full_since > self._cfg.stale_client_s:
            self._evict_client(session, now - session.full_since)

    def _enqueue(self, session: _ClientSession, block: bytes) -> bool:
        """Non-blocking put; on overflow discard the oldest buffered block and
        retry once — a live stream's feeder never blocks on a client (6)."""
        try:
            session.queue.put_nowait(block)
            self._counters.bytes_fanned_out += len(block)
            return True
        except queue.Full:
            pass
        self._counters.dropped_client_blocks += 1
        _drain_one(session.queue)
        try:
            session.queue.put_nowait(block)
            self._counters.bytes_fanned_out += len(block)
        except queue.Full:
            pass  # consumer raced the drop; the next block retries
        return False

    def _evict_client(self, session: _ClientSession, stuck_s: float) -> None:
        """Reap a never-draining session — the abandoned-frame reaper (REL-10b).

        The sentinel ends the parked generator; the registry reference is
        dropped so the queue becomes collectable even when its frame never
        resumes. For the SOLE client the session lingers in the registry until
        ``_teardown``'s final clear, so an observer that sees the registry
        empty also sees the whole teardown (proc reaped, queue unregistered,
        singleton retired) — post-eviction state asserts stay race-free.
        """
        with self._clients_lock:
            if session not in self._clients:
                return  # released concurrently
            self._counters.evicted_clients += 1
            sole = self._active and len(self._clients) == 1
        log.info("stream fanout: evicted client %d (queue full %.1fs)", session.client_id, stuck_s)
        self._poison_session(session, drop_buffered=True)
        if sole:
            self._teardown()  # the final clear removes the lingering session
        else:
            with self._clients_lock:
                self._discard_client_locked(session)

    def _poison_session(self, session: _ClientSession, drop_buffered: bool) -> None:
        """Wake a session's generator with the stop sentinel.

        Eviction drains first (the stale buffered bytes are worthless). Teardown
        keeps buffered bytes — the generator flushes them before honouring the
        stop (``_residual_blocks``).
        """
        if drop_buffered:
            _drain_queue(session.queue)
        try:
            session.queue.put_nowait(_STOP_SENTINEL)
        except queue.Full:
            _drain_queue(session.queue)
            session.queue.put_nowait(_STOP_SENTINEL)

    def _discard_client_locked(self, session: _ClientSession) -> bool:
        """Identity-remove a session; caller holds ``_clients_lock``."""
        try:
            self._clients.remove(session)
            return True
        except ValueError:
            return False


# ----------------------------------------------------------------------
# Module factories + the per-client generator
# ----------------------------------------------------------------------


def get_stream_fanout(global_state: GlobalState, cfg: FanoutConfig | None = None) -> StreamFanout:
    """Return the process-wide fanout singleton, creating it if absent/retired.

    Example:
        fanout = get_stream_fanout(state)
    """
    with _fanout_lock:
        fanout = getattr(global_state, "stream_fanout", None)
        if fanout is None:
            fanout = StreamFanout(cfg or FanoutConfig(), global_state)
            global_state.stream_fanout = fanout
        return fanout


def acquire_stream_client(
    global_state: GlobalState, cfg: FanoutConfig | None = None
) -> tuple[StreamFanout, _ClientSession] | None:
    """Acquire ``(fanout, session)``; bounded retries past a retiring singleton.

    Returns:
        (StreamFanout, _ClientSession), or None when the transcoder cannot
        start — the caller serves an empty stream, never a hang.
    """
    for _ in range(_FACTORY_ATTEMPTS):
        fanout = get_stream_fanout(global_state, cfg)
        try:
            return fanout, fanout.acquire_client()
        except FanoutInactive:
            fanout._retire()  # same-module factory: retire early, retry fresh
        except Exception:  # noqa: BLE001 - spawn failure → empty stream (D10)
            fanout._retire()
            log.exception("stream fanout: transcoder startup failed")
            return None
    return None


def mp3_client_stream(global_state: GlobalState) -> Iterator[bytes]:
    """Sync generator yielding the shared MP3 stream for one client (REL-10).

    The client owns no subprocess; an abandoned frame is reaped by stale
    eviction + sentinel, not by its ``finally``.

    Example:
        return StreamingResponse(mp3_client_stream(state), media_type="audio/mpeg")
    """
    acquired = acquire_stream_client(global_state)
    if acquired is None:
        return  # spawn failed: an empty body beats a hung request (round-3 D10)
    fanout, session = acquired
    try:
        while global_state.is_running:
            try:
                block = session.queue.get(timeout=fanout.client_poll_s)
            except queue.Empty:
                continue
            if block is None or block is _STOP_SENTINEL:
                yield from _residual_blocks(session.queue)
                break
            yield block
    finally:
        fanout.release_client(session)
