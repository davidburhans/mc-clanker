"""stream_fanout_args.py — ffmpeg launch contract for the MP3 fan-out (REL-10).

Everything needed to launch the shared MP3 transcoder exactly like the legacy
per-client generator did: binary discovery, the argv (byte-identical parity is
pinned by test), the tunable ``FanoutConfig``, and the ``FanoutStatus``
telemetry shape. Split from ``stream_fanout.py`` to keep every module under
the 500-line style limit; the orchestrator re-exports the public names.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class FanoutConfig:
    """Immutable fan-out parameters; tests inject fast values directly."""

    bitrate_kbps: int = 192
    sample_rate: int = 44100
    channels: int = 2
    pcm_queue_blocks: int = 256  # ~12 s of mixer blocks (~8 KiB each)
    client_queue_blocks: int = 100  # ~17 s of MP3 in 4 KiB blocks @192 kbps
    client_poll_s: float = 1.0  # client-generator read poll (is_running re-check)
    queue_poll_s: float = 0.25  # feeder PCM poll (relay default)
    stale_client_s: float = 10.0  # evict a client whose queue stayed full this long
    restart_backoff_s: float = 2.0
    restart_backoff_cap_s: float = 10.0
    # NOTE: no max_restarts — REL-15's lesson (decision 4); refcount teardown
    # bounds the restart loop instead.


@dataclass
class FanoutStatus:
    """Point-in-time fan-out telemetry. No secrets exist in this path."""
    active: bool
    client_count: int
    process_alive: bool
    started_at: float
    uptime_seconds: float
    restarts: int
    dropped_pcm_blocks: int
    dropped_client_blocks: int
    evicted_clients: int
    bytes_fanned_out: int
    last_error: str = ""


@dataclass
class _FanoutCounters:
    """Shared telemetry counters + the bounded stderr tail for death diagnosis.

    One instance is created by the orchestrator and shared with the
    ``TranscoderSupervisor``; plain int/str fields only (GIL-atomic updates).
    """

    restarts: int = 0
    dropped_pcm_blocks: int = 0
    dropped_client_blocks: int = 0
    evicted_clients: int = 0
    bytes_fanned_out: int = 0
    last_error: str = ""
    stderr_tail: deque = field(default_factory=lambda: deque(maxlen=20))


def resolve_ffmpeg_exe() -> str:
    """Legacy binary discovery, moved verbatim from app_ui.

    Returns "/usr/bin/ffmpeg" when that path exists, else the PATH name.
    """
    if os.path.exists("/usr/bin/ffmpeg"):
        return "/usr/bin/ffmpeg"
    return "ffmpeg"


def build_mp3_args(cfg: FanoutConfig) -> list[str]:
    """Build the transcoder argv — identical to the legacy per-client one.

    Args:
        cfg: fan-out configuration (rate/channels/bitrate).

    Returns:
        argv list ending in "pipe:1" (MP3 on stdout). argv[0] is the
        RESOLVED exe (review P2: a hardcoded "ffmpeg" breaks PATH-less
        hosts while the -codecs probe still succeeds → silent empty
        stream; resolve_ffmpeg_exe prefers /usr/bin/ffmpeg).
    """
    return [
        resolve_ffmpeg_exe(),
        "-y",
        "-f",
        "s16le",
        "-ar",
        str(cfg.sample_rate),
        "-ac",
        str(cfg.channels),
        "-i",
        "pipe:0",
        "-f",
        "mp3",
        "-acodec",
        "libmp3lame",
        "-b:a",
        f"{cfg.bitrate_kbps}k",
        "pipe:1",
    ]
