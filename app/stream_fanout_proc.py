"""stream_fanout_proc.py — transcoder subprocess supervision for the MP3 fan-out.

Owns the ONE shared ffmpeg process lifecycle: spawn (Popen + stderr drain +
kill-list registration), respawn with capped linear backoff and NO permanent
give-up (REL-15's lesson — the restart loop is bounded by the client refcount
at the fanout layer, not here), and terminate-with-escalation (stdin EOF →
wait → kill → reap; no subprocess may outlive its owner).

``should_stop`` is injected by the orchestrator so a stop racing a restart can
never spawn after shutdown (decision 9 of the unit plan).
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import TYPE_CHECKING

from app.stream_fanout_args import FanoutConfig, build_mp3_args

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.framework.framework_state import GlobalState
    from app.stream_fanout import _FanoutCounters

log = logging.getLogger(__name__)


class TranscoderSupervisor:
    """Lifecycle owner of the single shared MP3 transcoder subprocess.

    The fanout drives it from its feeder/pump threads and shares one
    ``_FanoutCounters`` instance; subprocess bookkeeping in GlobalState
    (kill list) is kept in sync here.

    Example:
        supervisor = TranscoderSupervisor(cfg, counters, state, fanout.stop_requested)
        supervisor.spawn()
        ...
        supervisor.terminate()
    """

    def __init__(
        self,
        cfg: FanoutConfig,
        counters: _FanoutCounters,
        global_state: GlobalState,
        should_stop: Callable[[], bool],
    ) -> None:
        self._cfg = cfg
        self._counters = counters
        self._state = global_state
        self._should_stop = should_stop
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()

    def is_alive(self) -> bool:
        """True while a transcoder exists and has not exited."""
        with self._proc_lock:
            proc = self._proc
        return proc is not None and proc.poll() is None

    def spawn(self) -> None:
        """Popen the transcoder + stderr drain; swap the tracked process.

        Callers: the fanout's ``_start`` (first spawn) and its pump thread via
        ``ensure_alive`` (respawns) — never two owners at once.
        """
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            build_mp3_args(self._cfg),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # drained by a thread (pipe-buffer deadlock fix)
        )
        with self._proc_lock:
            old = self._proc
            self._proc = proc
        if old is not None:
            self._state.unregister_subprocess(old)
        self._state.register_subprocess(proc)
        threading.Thread(
            target=self._drain_stderr, args=(proc,), daemon=True, name="StreamFanoutStderr"
        ).start()

    def ensure_alive(self) -> bool:
        """True keeps the pump reading; False exits it. Respawns after backoff."""
        with self._proc_lock:
            proc = self._proc
        if proc is None:
            return False  # stopped concurrently
        if proc.poll() is None:
            return True  # alive
        self._counters.restarts += 1
        self._record_death(proc)
        if not self._sleep_backoff():
            return False
        if self._should_stop():
            return False  # stop raced the restart — never spawn after shutdown
        self.spawn()
        # True EVEN IF the new process is already dead: a client churn-kill can
        # land between the spawn and this return, and a fast death is not a
        # stop — the read loop sees the EOF and re-enters here to respawn.
        # Returning False here would tear the singleton down with clients still
        # attached (the T17 churn race).
        return True

    def read_mp3(self) -> bytes:
        """One 4096-byte MP3 block; ``b""`` means EOF (death or teardown)."""
        with self._proc_lock:
            proc = self._proc
        if proc is None or proc.stdout is None:
            return b""
        try:
            return proc.stdout.read(4096)
        except (OSError, ValueError):
            return b""

    def write_pcm(self, block: bytes) -> None:
        """One PCM block into stdin; dead-pipe writes are dropped + counted
        (the pump owns respawning)."""
        with self._proc_lock:
            proc = self._proc
        stdin = proc.stdin if proc is not None else None
        if proc is None or proc.poll() is not None or stdin is None:
            self._counters.dropped_pcm_blocks += 1
            return
        try:
            stdin.write(block)
            stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            self._counters.dropped_pcm_blocks += 1

    def terminate(self) -> None:
        """Take the process down and unregister it from the kill list.

        Terminate-before-join: the pump is parked in a blocking stdout read, so
        the EOF must be delivered BEFORE joining it — a blind join would stall
        every last disconnect by a read timeout (the REL-10 latency class).
        """
        with self._proc_lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        self._shutdown_proc(proc)
        self._state.unregister_subprocess(proc)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _record_death(self, proc: subprocess.Popen) -> None:
        """Fold the exit code + bounded stderr tail into ``last_error``."""
        tail = "\n".join(self._counters.stderr_tail)
        self._counters.last_error = f"exit={proc.returncode} {tail}".strip()[:500]
        log.warning(
            "stream fanout: transcoder exited (rc=%s); restart #%d",
            proc.returncode,
            self._counters.restarts,
        )

    def _sleep_backoff(self) -> bool:
        """Linear backoff capped at ``restart_backoff_cap_s``; False if a stop
        was requested meanwhile (relay mirror, cap added)."""
        delay = min(
            self._cfg.restart_backoff_s * self._counters.restarts, self._cfg.restart_backoff_cap_s
        )
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline and not self._should_stop():
            time.sleep(min(0.05, deadline - time.monotonic()))
        return not self._should_stop()

    def _shutdown_proc(self, proc: subprocess.Popen) -> None:
        """Terminate one transcoder: stdin EOF → wait → kill escalation.

        Hard rule: no subprocess outlives its owner — the final wait reaps even
        a killed process so no zombie is left behind.
        """
        try:
            if proc.poll() is None and proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            proc.kill()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            log.error("stream fanout: transcoder %s unkillable", getattr(proc, "pid", "?"))
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except (OSError, ValueError):
            pass

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        """Keep the stderr pipe drained (deadlock fix) and pocket a bounded
        tail so a transcoder death can be diagnosed (decision 3)."""
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                text = line.decode(errors="replace").strip()
                if text:
                    self._counters.stderr_tail.append(text)
        except (OSError, ValueError):
            pass
