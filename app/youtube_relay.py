"""youtube_relay.py — Live YouTube RTMP relay for the mixer PCM stream.

Registers a bounded queue as an audio client on GlobalState (the same sink
mechanism the streaming endpoints use), drains it on a writer thread, and
pipes s16le PCM into an FFmpeg subprocess. FFmpeg encodes AAC audio plus an
audio-reactive visualizer (showcqt / showwaves / showspectrum) as H.264 and
pushes RTMP to YouTube Live.

Built for unattended operation: a process that stays up for
``stability_window_s`` earns a fresh restart budget (REL-15 — the restart
limit is a rate limit, not a lifetime count), and the 24/7 watchdog in
``app/youtube_lifecycle.py`` re-arms the relay entirely once a give-up still
happens (rapid-death storm).

Usage:
    relay = YouTubeRelay(cfg, state)
    relay.start()
    ...
    summary = relay.stop()
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: Sentinel pushed by stop() to wake the writer thread immediately.
_STOP_SENTINEL = object()

ALLOWED_RESOLUTIONS = ("1280x720", "1920x1080")
ALLOWED_FPS = (24, 30, 60)
ALLOWED_VISUALIZERS = ("cqt", "waves", "spectrum")

#: Default video bitrate per resolution (YouTube-recommended sustained rates).
_RESOLUTION_BITRATE_KBPS = {"1280x720": 2500, "1920x1080": 4500}

_VISUALIZER_FILTERS = {
    # axis=0 keeps showcqt font-free — no fontconfig dependency in containers.
    "cqt": "showcqt=size={resolution}:rate={fps}:axis=0",
    "waves": "showwaves=size={resolution}:rate={fps}:mode=cline:colors=White|0x6AB0FF",
    # showspectrum has no fps option; the output -r flag enforces CFR instead.
    "spectrum": "showspectrum=size={resolution}:mode=combined:slide=scroll:scale=cbrt",
}


class RelayError(RuntimeError):
    """Raised on invalid relay configuration or lifecycle misuse."""


@dataclass(frozen=True)
class RelayConfig:
    """Immutable launch parameters for one relay session.

    ``stream_key`` is a secret: never log it, never return it from APIs.
    """

    ingest_url: str
    stream_key: str
    resolution: str = "1920x1080"
    fps: int = 30
    visualizer: str = "cqt"
    video_bitrate_kbps: int | None = None  # None → pick per resolution
    audio_bitrate_kbps: int = 160
    sample_rate: int = 44100
    channels: int = 2
    max_restarts: int = 3
    restart_backoff_s: float = 2.0
    stability_window_s: float = 300.0  # REL-15: alive >= this earns a fresh restart budget
    queue_blocks: int = 512  # ~24s of mixer blocks; overflow drops, never blocks
    queue_poll_s: float = 0.25

    def resolved_video_bitrate_kbps(self) -> int:
        """Explicit bitrate if set, else the YouTube default for the resolution."""
        if self.video_bitrate_kbps is not None:
            return self.video_bitrate_kbps
        return _RESOLUTION_BITRATE_KBPS[self.resolution]

    def __post_init__(self) -> None:
        if self.resolution not in ALLOWED_RESOLUTIONS:
            raise RelayError(f"resolution {self.resolution!r} not in {ALLOWED_RESOLUTIONS}")
        if self.fps not in ALLOWED_FPS:
            raise RelayError(f"fps {self.fps!r} not in {ALLOWED_FPS}")
        if self.visualizer not in ALLOWED_VISUALIZERS:
            raise RelayError(f"visualizer {self.visualizer!r} not in {ALLOWED_VISUALIZERS}")
        if not self.stream_key or not self.stream_key.strip():
            raise RelayError("stream_key must be a non-empty string")
        if not self.ingest_url.startswith("rtmp://"):
            raise RelayError(f"ingest_url {self.ingest_url!r} must start with rtmp://")
        if self.sample_rate <= 0 or self.channels <= 0:
            raise RelayError("sample_rate and channels must be positive")
        if self.stability_window_s < 0:
            raise RelayError(f"stability_window_s {self.stability_window_s!r} must be >= 0")


@dataclass
class RelayStatus:
    """Point-in-time relay telemetry. Safe to expose via API (no secrets)."""

    active: bool
    process_alive: bool
    started_at: float
    uptime_seconds: float
    restarts: int
    dropped_blocks: int
    bytes_sent: int
    last_error: str = ""
    visualizer: str = ""
    resolution: str = ""
    fps: int = 0


def build_ffmpeg_args(cfg: RelayConfig) -> list[str]:
    """Build the ffmpeg argv that encodes PCM→AAC+H.264 and pushes RTMP.

    The audio is split pre-encode: one branch feeds AAC, the other drives the
    visualizer, so on-screen motion matches the audible stream exactly.

    Args:
        cfg: validated relay configuration.

    Returns:
        argv list. Contains the stream key (in the RTMP URL) — never log this.
    """
    vis = _VISUALIZER_FILTERS[cfg.visualizer].format(resolution=cfg.resolution, fps=cfg.fps)
    keyframe_interval = cfg.fps * 2  # YouTube wants keyframes every 2s
    video_kbps = cfg.resolved_video_bitrate_kbps()
    rtmp_url = f"{cfg.ingest_url.rstrip('/')}/{cfg.stream_key.strip()}"
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "s16le",
        "-ar",
        str(cfg.sample_rate),
        "-ac",
        str(cfg.channels),
        "-i",
        "pipe:0",
        "-filter_complex",
        f"[0:a]asplit=2[aenc][avis];[avis]{vis}[vout]",
        "-map",
        "[aenc]",
        "-c:a",
        "aac",
        "-b:a",
        f"{cfg.audio_bitrate_kbps}k",
        "-ar",
        str(cfg.sample_rate),
        "-map",
        "[vout]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        f"{video_kbps}k",
        "-maxrate",
        f"{video_kbps}k",
        "-bufsize",
        f"{video_kbps * 2}k",
        "-g",
        str(keyframe_interval),
        "-r",
        str(cfg.fps),
        "-f",
        "flv",
        rtmp_url,
    ]


def _scrub(text: str, secret: str) -> str:
    """Replace any occurrence of ``secret`` so it never reaches logs/APIs."""
    if not secret:
        return text
    return text.replace(secret, "***")


@dataclass
class _RelayCounters:
    restarts: int = 0
    dropped_blocks: int = 0
    bytes_sent: int = 0
    last_error: str = ""
    stderr_tail: deque = field(default_factory=lambda: deque(maxlen=20))


class YouTubeRelay:
    """Feeds mixer PCM into an FFmpeg RTMP subprocess with bounded restarts.

    The queue stays registered as an audio client across process restarts, so
    a respawning FFmpeg only loses the blocks drained while it was down.
    """

    def __init__(self, cfg: RelayConfig, global_state) -> None:
        self._cfg = cfg
        self._state = global_state
        self._counters = _RelayCounters()
        self._queue: queue.Queue = queue.Queue(maxsize=cfg.queue_blocks)
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._writer: threading.Thread | None = None
        self._started_at = 0.0
        self._spawned_at = 0.0  # monotonic spawn time of the current proc; never read before first spawn
        self._active = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> RelayStatus:
        """Spawn FFmpeg, start the writer thread, register the audio client."""
        if self._active:
            raise RelayError("relay already started")
        with self._proc_lock:
            self._spawn_ffmpeg()
        self._state.add_audio_client(self._queue)
        self._active = True
        self._started_at = time.time()
        self._stop_event.clear()
        self._writer = threading.Thread(target=self._writer_loop, daemon=True, name="YouTubeRelay")
        self._writer.start()
        log.info(
            "YouTube relay started (%s @ %s/%dfps)",
            self._cfg.visualizer,
            self._cfg.resolution,
            self._cfg.fps,
        )
        return self.status()

    def stop(self) -> RelayStatus:
        """Unregister the client and shut FFmpeg down. Idempotent."""
        if not self._active:
            return self.status()
        self._state.remove_audio_client(self._queue)
        self._active = False
        self._stop_event.set()
        try:
            self._queue.put_nowait(_STOP_SENTINEL)  # accelerant only: get(timeout) also wakes
        except queue.Full:
            pass
        if self._writer is not None:
            self._writer.join(timeout=5.0)
        self._terminate_ffmpeg()
        log.info("YouTube relay stopped after %ds", int(self.status().uptime_seconds))
        return self.status()

    def reset_restart_budget(self) -> None:
        """Grant a fresh restart budget (rate-limit semantics, REL-15).

        Called by the restart path after a stability window; also usable by
        external watchdogs.
        """
        self._counters.restarts = 0

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def status(self) -> RelayStatus:
        with self._proc_lock:
            proc = self._proc
        alive = self._active and proc is not None and proc.poll() is None
        return RelayStatus(
            active=self._active,
            process_alive=alive,
            started_at=self._started_at,
            uptime_seconds=(time.time() - self._started_at) if self._started_at else 0.0,
            restarts=self._counters.restarts,
            dropped_blocks=self._counters.dropped_blocks,
            bytes_sent=self._counters.bytes_sent,
            last_error=_scrub(self._counters.last_error, self._cfg.stream_key),
            visualizer=self._cfg.visualizer,
            resolution=self._cfg.resolution,
            fps=self._cfg.fps,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _spawn_ffmpeg(self) -> None:
        """Launch the RTMP subprocess + stderr drain thread. Caller holds lock."""
        self._proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            build_ffmpeg_args(self._cfg),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        # REL-15: one site covers initial start + every respawn; a failed spawn
        # must NOT refresh it — no new life started.
        self._spawned_at = time.monotonic()
        threading.Thread(
            target=self._drain_stderr, args=(self._proc,), daemon=True, name="YouTubeRelayStderr"
        ).start()

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        for line in proc.stderr:  # type: ignore[union-attr]
            text = line.decode(errors="replace").rstrip()
            if text:
                self._counters.stderr_tail.append(_scrub(text, self._cfg.stream_key))

    def _writer_loop(self) -> None:
        """Drain the audio queue into FFmpeg stdin; restart on process death."""
        while not self._stop_event.is_set():
            if not self._ensure_process():
                break
            try:
                block = self._queue.get(timeout=self._cfg.queue_poll_s)
            except queue.Empty:
                continue
            if block is _STOP_SENTINEL:
                break
            self._write_block(block)

    def _ensure_process(self) -> bool:
        """Return False when the writer should exit; respawn if FFmpeg died.

        Backoff sleeps happen OUTSIDE _proc_lock so stop()/status() never wait
        on a restart cycle.
        """
        with self._proc_lock:
            proc = self._proc
        if proc is None:
            return False  # stopped concurrently
        if proc.poll() is None:
            return True  # alive
        # REL-15 (U10): restarts is a rate limit, not a lifetime count — a
        # process that stayed up >= stability_window_s earns a fresh budget.
        # The restarts==0 short-circuit keeps the very first death counted.
        if self._counters.restarts and (
            time.monotonic() - self._spawned_at >= self._cfg.stability_window_s
        ):
            self.reset_restart_budget()
        self._counters.restarts += 1
        self._record_death(proc)
        if self._counters.restarts > self._cfg.max_restarts:
            self._give_up()
            return False
        if not self._sleep_backoff():
            return False
        with self._proc_lock:
            if self._stop_event.is_set() or not self._active:
                return False  # stop() raced us — never spawn after shutdown
            self._spawn_ffmpeg()
            return self._proc is not None

    def _write_block(self, block: bytes | None) -> None:
        if block is None:
            # trigger_shutdown poisons client queues with None (REL-10
            # follow-up): drop it, never stdin.write(None). Poison is not
            # audio — counters stay still — and the writer keeps serving.
            return
        with self._proc_lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            self._counters.dropped_blocks += 1
            return
        try:
            proc.stdin.write(block)  # type: ignore[union-attr]
            self._counters.bytes_sent += len(block)
        except (BrokenPipeError, OSError, ValueError):
            # Dead pipe: block is dropped; next loop iteration respawns.
            self._counters.dropped_blocks += 1

    def _record_death(self, proc: subprocess.Popen) -> None:
        tail = "\n".join(self._counters.stderr_tail)
        self._counters.last_error = f"exit={proc.returncode} {tail}".strip()[:500]
        log.warning(
            "YouTube relay ffmpeg exited (rc=%s); restart %d/%d",
            proc.returncode,
            self._counters.restarts,
            self._cfg.max_restarts,
        )

    def _sleep_backoff(self) -> bool:
        """Sleep with linear backoff; False if a stop was requested meanwhile."""
        delay = self._cfg.restart_backoff_s * self._counters.restarts
        deadline = time.time() + delay
        while time.time() < deadline and not self._stop_event.is_set():
            time.sleep(min(0.1, deadline - time.time()))
        return not self._stop_event.is_set()

    def _give_up(self) -> None:
        self._counters.last_error = f"gave up after {self._cfg.max_restarts} restarts: " + (
            self._counters.last_error or "unknown"
        )
        log.error("YouTube relay giving up: %s", self._counters.last_error)
        self._state.remove_audio_client(self._queue)
        self._active = False

    def _terminate_ffmpeg(self) -> None:
        """Close stdin (flush), then wait/kill. Idempotent, lock-held."""
        with self._proc_lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin is not None:
                proc.stdin.close()
                proc.wait(timeout=5.0)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            proc.kill()
