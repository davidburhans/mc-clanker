"""recording_sink.py — per-recording writer threads (REL-11 / REL-22).

REL-11: ``broadcast_audio`` used to call ``handle.write(pcm)`` synchronously on
the mixer thread every ~46 ms tick, so any disk stall delayed every listener.
Each recording file is now owned by exactly one :class:`RecordingSink`: the
audio thread only ``submit()``s into a bounded queue (drop-oldest, with a
dropped-bytes counter surfaced via ``/api/health``); a daemon writer thread owns
the handle end-to-end. The shape mirrors YouTubeRelay's writer (bounded queue +
stop sentinel + poll loop) minus respawn — the "resource" is a file handle, so a
dead write path is the rel-05 auto-stop's business, not a restart's.

Ownership invariant (single-owner finalize): ONLY the writer thread ever touches
the handle's data plane, and only the writer thread finalizes/closes it. Stop
paths (stop_show, stop_export, shutdown) call ``stop_and_finalize`` — which
merely sets the stop event, pushes the sentinel and joins bounded. A timed-out
stop never touches the handle; the unstuck writer finalizes it later
(eventual finalize), so two threads can never seek/write one handle and the
rel-05 F5 "concurrent stop wins, no double finalize" race becomes structural.

REL-22: :func:`end_live_show_row` ends a still-'live' Show row on the shutdown
close path, so a killed process never leaves a show stuck 'live' with an
unfinalized WAV.
"""

import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from app.lib.wav import finalize_wav

log = logging.getLogger(__name__)

#: ~12 s of 8 KiB mixer blocks (~2 MB): rides out page-cache hiccups without
#: shedding bytes, while capping the worst-case clean-stop drain window.
RECORDING_SINK_QUEUE_BLOCKS = 256
#: Writer wake-up poll (matches the relay/fan-out default).
RECORDING_SINK_POLL_S = 0.25
#: Bounded join on stop/shutdown paths (plan §1.3).
RECORDING_SINK_JOIN_TIMEOUT_S = 5.0

#: Sentinel pushed by stop paths to wake the writer immediately (accelerant
#: only: the get(timeout) poll also observes the stop event).
_STOP_SENTINEL = object()


@dataclass
class _SinkCounters:
    """Writer-side counters (mirrors the relay's _RelayCounters)."""

    bytes_written: int = 0
    dropped_blocks: int = 0
    dropped_bytes: int = 0


@dataclass
class SinkStatus:
    """Point-in-time sink telemetry. Safe to expose via API (no secrets)."""

    sink_name: str
    active: bool  # writer thread alive and not stopping
    bytes_written: int
    dropped_blocks: int
    dropped_bytes: int


class RecordingSink:
    """One recording file's writer thread (REL-11).

    The mixer thread never touches the handle: it calls :meth:`submit` (bounded
    queue, drop-oldest, counted). The writer thread owns the handle end-to-end
    and is the only thread that finalizes/closes it — rel-05 auto-stop and every
    stop path route through the same exit sequence (drain → flush → finalize).

    ``state`` is the GlobalState instance used for the rel-05 health-dict hooks
    and slot detach (YouTubeRelay precedent: ctor-injected, duck-typed — fakes
    in tests are welcome).

    Example:
        sink = RecordingSink(open(path, "wb"), "show", state)
        sink.start()
        ...
        sink.submit(pcm_block)            # audio thread: never blocks
        ...
        sink.stop_and_finalize()          # stop path: drain + WAV finalize
    """

    def __init__(
        self,
        handle,
        sink_name: str,
        state,
        *,
        wav: bool = True,
        queue_blocks: int = RECORDING_SINK_QUEUE_BLOCKS,
        poll_s: float = RECORDING_SINK_POLL_S,
    ) -> None:
        self._handle = handle
        self._sink_name = sink_name
        self._state = state
        self._wav = wav
        self._poll_s = poll_s
        self._queue: queue.Queue = queue.Queue(maxsize=queue_blocks)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._counters = _SinkCounters()
        self._errors = 0  # consecutive failed writes (rel-05c)
        self._logged_failure = False  # once-per-sink failure log (review B9 lineage)
        self._dead = False  # auto-stopped: stop futile writes, shed queued bytes
        self._finalized = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the daemon writer thread (``RecSink-{sink_name}``)."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._writer_loop, daemon=True, name=f"RecSink-{self._sink_name}"
        )
        self._thread.start()

    def submit(self, pcm: bytes) -> None:
        """Queue one PCM block from the audio thread. Never blocks (REL-11).

        Contract: drop-oldest on overflow — the evicted block's bytes and (on a
        second Full) this block's bytes are counted in ``dropped_bytes`` so the
        U15 soak can see disk stall shedding. A submit after stop is dropped and
        counted too (the handle is closed or closing).
        """
        if self._stop_event.is_set():
            self._count_dropped(len(pcm))
            return
        try:
            self._queue.put_nowait(pcm)
            return
        except queue.Full:
            pass
        try:
            evicted = self._queue.get_nowait()
        except queue.Empty:
            pass
        else:
            self._count_dropped(len(evicted))
        try:
            self._queue.put_nowait(pcm)
        except queue.Full:
            self._count_dropped(len(pcm))

    def stop_and_finalize(self, timeout: float = RECORDING_SINK_JOIN_TIMEOUT_S) -> bool:
        """Request drain+finalize (single owner: the writer). Bounded join.

        Returns True when the writer exited within ``timeout``. On timeout the
        handle is NOT touched from here — the unstuck writer finalizes later
        (plan §1.3). Never blocks on disk I/O itself: only on the bounded join.
        """
        self._stop_event.set()
        try:
            self._queue.put_nowait(_STOP_SENTINEL)  # accelerant only
        except queue.Full:
            pass
        if self._thread is None or self._thread is threading.current_thread():
            # Never started (or the writer calling its own stop edge): the
            # caller becomes the single owner and runs the exit sequence here.
            self._drain_and_finalize()
            return True
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            log.error(
                "Recording %s sink writer did not stop within %.1fs; finalize deferred to the writer thread",
                self._sink_name,
                timeout,
            )
            return False
        return True

    def status(self) -> SinkStatus:
        """Snapshot the counters for /api/health and tests (pure reads, no I/O)."""
        active = self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set()
        return SinkStatus(
            sink_name=self._sink_name,
            active=active,
            bytes_written=self._counters.bytes_written,
            dropped_blocks=self._counters.dropped_blocks,
            dropped_bytes=self._counters.dropped_bytes,
        )

    # ------------------------------------------------------------------
    # Writer thread — the single owner of the handle
    # ------------------------------------------------------------------

    def _writer_loop(self) -> None:
        """Drain the queue into the handle; exit on sentinel or Empty+stop."""
        while True:
            try:
                block = self._queue.get(timeout=self._poll_s)
            except queue.Empty:
                if self._stop_event.is_set():
                    break
                continue
            if block is _STOP_SENTINEL:
                break
            self._write_block(block)
        self._drain_and_finalize()

    def _write_block(self, block: bytes) -> None:
        """Write one block + flush (plan §1.5), surfacing failures (rel-05c).

        The post-write flush bounds a crash's userspace loss to zero and makes
        on-disk sizes deterministic the moment bytes_written reports a block.
        """
        if self._dead:  # rel-05 auto-stopped: stop the futile writes
            self._count_dropped(len(block))
            return
        try:
            self._handle.write(block)
            self._handle.flush()
        except Exception as exc:  # noqa: BLE001 — any write failure is a sink fault
            self._note_failure(exc)
            return
        self._counters.bytes_written += len(block)
        if self._errors or self._state.recording_write_errors.get(self._sink_name, 0):
            self._errors = 0
            self._state._reset_sink_write_errors(self._sink_name)

    def _note_failure(self, exc: Exception) -> None:
        """Log once per sink, count the failure, auto-stop past the threshold."""
        if not self._logged_failure:
            log.warning("Recording write to %s sink failed: %r", self._sink_name, exc)
            self._logged_failure = True
        self._errors += 1
        if self._state._note_sink_write_failure(self, self._sink_name):
            self._auto_stop()

    def _auto_stop(self) -> None:
        """rel-05c threshold breach: detach via state, then take the normal exit.

        The slot must still be ours (rel-05 F5): a concurrent stop_show already
        detached it and finalized — the stale breach is then a no-op (no flag
        writes, no second finalize). On a win, the normal exit sequence runs on
        this writer thread: the remaining loop unrolls, the sweep sheds queued
        blocks as dropped, and finalize closes the dead handle exactly once.
        """
        self._dead = True
        if not self._state._detach_failing_sink(self, self._sink_name):
            return
        log.error(
            "Recording %s sink auto-stopped after %d consecutive write failures",
            self._sink_name,
            self._errors,
        )
        self._stop_event.set()
        try:
            self._queue.put_nowait(_STOP_SENTINEL)
        except queue.Full:
            pass

    def _drain_and_finalize(self) -> None:
        """Exit sequence: sweep stragglers → finalize → mark finalized.

        Everything submitted before the stop request was already written by the
        loop (the sentinel sits behind queued blocks, FIFO); the sweep only
        catches blocks that landed in the sub-µs race window before a concurrent
        stop. Idempotent: a second entry can never re-finalize a closed handle.
        """
        if self._finalized:
            return
        self._sweep_queue()
        self._finalize()
        self._finalized = True

    def _sweep_queue(self) -> None:
        """Write (or, once dead, count as dropped) blocks queued before exit."""
        while True:
            try:
                block = self._queue.get_nowait()
            except queue.Empty:
                return
            if block is not _STOP_SENTINEL:
                self._write_block(block)

    def _finalize(self) -> None:
        """Close out the handle: WAV sizes patched for wav, flush+close otherwise.

        Non-wav export mode must never have a RIFF header patched over raw bytes.
        finalize_wav itself flushes and closes (and swallows per-handle OSError),
        which is what makes the rel-05 ENOSPC fake close exactly once.
        """
        if self._wav:
            finalize_wav(self._handle)
            return
        try:
            self._handle.flush()
        except (OSError, ValueError):
            pass
        try:
            self._handle.close()
        except (OSError, ValueError):
            pass

    def _count_dropped(self, num_bytes: int) -> None:
        self._counters.dropped_blocks += 1
        self._counters.dropped_bytes += num_bytes


def end_live_show_row(show_id: int) -> bool:
    """End a still-'live' Show row on the shutdown close path (REL-22).

    Load-conditional: only rows with ``status == 'live'`` are updated, so a
    repeat ``trigger_shutdown`` or a show already stopped via ``stop_show`` is a
    no-op (returns False and writes nothing). Field writes mirror stop_show:
    naive-UTC ``ended_at`` (DATA-1 contract — the columns are naive) and
    ``duration_seconds`` derived from ``started_at`` when present.

    Returns True when the row was ended. DB adapters are imported lazily (same
    precedent as framework_state's finalize_wav import in rel-05) — the sink
    module stays importable without a database.
    """
    from app.db import DatabaseManager
    from app.models import Show

    ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
    with DatabaseManager.get_instance().session() as session:
        updated = (
            session.query(Show)
            .filter(Show.id == show_id, Show.status == "live")
            .update({"status": "ended", "ended_at": ended_at}, synchronize_session=False)
        )
        if updated == 0:
            return False
        row = session.query(Show).filter(Show.id == show_id).first()
        if row is not None and row.started_at:
            row.duration_seconds = int((ended_at - row.started_at).total_seconds())
    return True
