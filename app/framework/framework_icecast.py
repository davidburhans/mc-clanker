"""framework_icecast.py — Icecast/Shoutcast streaming source.

When ICECAST_ENABLED=true, the mixed audio is streamed to an Icecast server
for web radio distribution. Uses ffmpeg to transcode PCM -> MP3 and pipes
the output via HTTP PUT (Icecast source protocol).

Environment variables:
    ICECAST_ENABLED   — "true" to enable
    ICECAST_HOST      — Icecast server hostname
    ICECAST_PORT      — Icecast server port (default 8000)
    ICECAST_PASSWORD  — Source password
    ICECAST_MOUNT     — Mount point (default /stream)
    ICECAST_NAME      — Stream name (shown in player)
    ICECAST_GENRE     — Stream genre
    ICECAST_DESCRIPTION — Stream description
    ICECAST_URL       — Stream info URL
"""

import base64
import logging
import os
import queue
import subprocess
import threading
import time

log = logging.getLogger(__name__)


class IcecastStreamer:
    """Streams mixed PCM audio to an Icecast/Shoutcast server.

    Runs a background thread that:
    1. Receives PCM data via feed_pcm() (called from Mixer thread via broadcast_audio)
    2. Transcodes PCM -> MP3 using ffmpeg
    3. Sends MP3 data to Icecast via HTTP PUT (source protocol)
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8000,
        password: str = "",
        mount: str = "/stream",
        name: str = "MC Clanker",
        genre: str = "AI Generated",
        description: str = "AI-powered continuous music stream",
        url: str = "",
        bitrate: int = 192,
        sample_rate: int = 44100,
        channels: int = 2,
    ):
        self.host = host
        self.port = port
        self.password = password
        self.mount = mount if mount.startswith("/") else f"/{mount}"
        self.name = name
        self.genre = genre
        self.description = description
        self.url = url
        self.bitrate = bitrate
        self.sample_rate = sample_rate
        self.channels = channels

        self._pcm_queue: queue.Queue = queue.Queue(maxsize=200)
        self._running = False
        self._thread: threading.Thread | None = None
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._http_proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._bytes_streamed = 0
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_connected(self) -> bool:
        """True if ffmpeg and http processes are alive."""
        with self._lock:
            if self._ffmpeg_proc is None or self._http_proc is None:
                return False
            return self._ffmpeg_proc.poll() is None and self._http_proc.poll() is None

    @property
    def uptime_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return time.time() - self._started_at

    @property
    def bytes_streamed(self) -> int:
        return self._bytes_streamed

    def start(self):
        """Start the Icecast streaming thread."""
        if self._running:
            log.warning("Icecast streamer already running")
            return

        self._running = True
        self._started_at = time.time()
        self._thread = threading.Thread(
            target=self._stream_loop,
            daemon=True,
            name="IcecastStreamer",
        )
        self._thread.start()
        log.info(
            "Icecast streamer started: streaming to %s:%d%s",
            self.host,
            self.port,
            self.mount,
        )

    def stop(self):
        """Stop the Icecast streaming thread."""
        if not self._running:
            return

        self._running = False

        # Poison the queue to unblock the stream loop
        try:
            self._pcm_queue.put_nowait(None)
        except Exception:
            pass

        # Terminate subprocesses
        with self._lock:
            for proc in (self._ffmpeg_proc, self._http_proc):
                if proc is not None:
                    try:
                        proc.kill()
                        proc.wait(timeout=2)
                    except Exception:
                        pass
            self._ffmpeg_proc = None
            self._http_proc = None

        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

        log.info("Icecast streamer stopped (streamed %d bytes)", self._bytes_streamed)

    def feed_pcm(self, pcm_data: bytes):
        """Feed PCM audio data to the Icecast stream.

        Called from the Mixer thread via broadcast_audio().
        Non-blocking: drops data if queue is full (backpressure).
        """
        if not self._running:
            return
        try:
            self._pcm_queue.put_nowait(pcm_data)
        except queue.Full:
            pass  # Drop chunk — better to skip than block the audio thread

    def _ffmpeg_stdin(self):
        """Return ffmpeg's stdin pipe, raising if it is unavailable.

        ``Popen.stdin`` is typed ``Optional``; we always start ffmpeg with
        ``stdin=PIPE`` so it is present at runtime. Centralising the access
        here narrows the type and turns a latent ``AttributeError`` into an
        explicit, debuggable failure.
        """
        proc = self._ffmpeg_proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("ffmpeg stdin pipe is unavailable")
        return proc.stdin

    def _build_auth_header(self) -> str:
        """Build HTTP Basic Auth header value for Icecast source."""
        credentials = f"source:{self.password}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

    def _build_icecast_headers(self) -> list:
        """Build ffmpeg icy_headers for Icecast metadata."""
        headers = []
        headers.append(f"Authorization: {self._build_auth_header()}")
        headers.append("Content-Type: audio/mpeg")
        headers.append(f"ice-name: {self.name}")
        headers.append(f"ice-genre: {self.genre}")
        headers.append(f"ice-description: {self.description}")
        if self.url:
            headers.append(f"ice-url: {self.url}")
        headers.append("ice-public: 0")
        return headers

    def _stream_loop(self):
        """Main streaming loop: PCM queue -> ffmpeg (PCM->MP3) -> ffmpeg (MP3->Icecast HTTP PUT)."""
        ffmpeg_exe = "/usr/bin/ffmpeg"
        if not os.path.exists(ffmpeg_exe):
            ffmpeg_exe = "ffmpeg"

        # Single ffmpeg process: reads PCM from stdin, outputs MP3 via HTTP PUT to Icecast
        # This avoids piping between two processes and handles reconnection.
        ffmpeg_cmd = [
            ffmpeg_exe,
            "-nostats",  # suppress periodic progress/stat lines
            "-loglevel",
            "error",  # only errors -> keep stderr volume near zero
            "-y",
            "-f",
            "s16le",
            "-ar",
            str(self.sample_rate),
            "-ac",
            str(self.channels),
            "-i",
            "pipe:0",  # Read PCM from stdin
            "-f",
            "mp3",
            "-acodec",
            "libmp3lame",
            "-b:a",
            f"{self.bitrate}k",
            "-bufsize",
            f"{self.bitrate * 2}k",
            # HTTP PUT output to Icecast
            "-content_type",
            "audio/mpeg",
            "-icy_header",
            f"Authorization: {self._build_auth_header()}",
            "-icy_header",
            f"ice-name: {self.name}",
            "-icy_header",
            f"ice-genre: {self.genre}",
            "-icy_header",
            f"ice-description: {self.description}",
            "-icy_header",
            "ice-public: 0",
        ]
        if self.url:
            ffmpeg_cmd.extend(["-icy_header", f"ice-url: {self.url}"])

        icecast_url = f"http://{self.host}:{self.port}{self.mount}"
        ffmpeg_cmd.append(icecast_url)

        log.info("Icecast ffmpeg command: %s", " ".join(ffmpeg_cmd))

        # Wait for first PCM data before starting ffmpeg
        # (avoids connecting to Icecast with no audio)
        try:
            first_chunk = self._pcm_queue.get(timeout=300.0)
            if first_chunk is None:
                log.info("Icecast streamer: received poison pill before first chunk")
                self._running = False
                return
        except queue.Empty:
            log.warning("Icecast streamer: timed out waiting for first audio chunk")
            self._running = False
            return

        # Start ffmpeg process
        try:
            with self._lock:
                self._ffmpeg_proc = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=subprocess.PIPE,
                    # Output goes to the Icecast URL over HTTP, not stdout/stderr,
                    # so both are discarded. Capturing them to PIPE without a
                    # draining reader deadlocks once the ~64KB OS pipe buffer
                    # fills (ffmpeg blocks on stderr, stops reading stdin, and
                    # our stdin.write then blocks forever).
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception as e:
            log.error("Icecast streamer: failed to start ffmpeg: %s", e)
            self._running = False
            return

        # Pre-feed first chunk
        try:
            self._ffmpeg_stdin().write(first_chunk)
            self._ffmpeg_stdin().flush()
        except Exception as e:
            log.warning("Icecast streamer: could not pre-feed first chunk: %s", e)
            self._cleanup()
            self._running = False
            return

        # Feed PCM data to ffmpeg stdin
        try:
            while self._running:
                try:
                    chunk = self._pcm_queue.get(timeout=1.0)
                    if chunk is None:
                        break  # Poison pill
                    if self._ffmpeg_proc.poll() is not None:
                        log.warning(
                            "Icecast ffmpeg exited with code %d",
                            self._ffmpeg_proc.returncode,
                        )
                        break
                    self._ffmpeg_stdin().write(chunk)
                    self._ffmpeg_stdin().flush()
                except queue.Empty:
                    continue
                except (BrokenPipeError, OSError) as e:
                    log.warning("Icecast streamer: pipe error: %s", e)
                    break
        finally:
            self._cleanup()

    def _cleanup(self):
        """Clean up ffmpeg process."""
        with self._lock:
            if self._ffmpeg_proc is not None:
                try:
                    self._ffmpeg_stdin().close()
                except Exception:
                    pass
                try:
                    self._ffmpeg_proc.kill()
                    self._ffmpeg_proc.wait(timeout=2)
                except Exception:
                    pass
                self._ffmpeg_proc = None


def create_icecast_streamer_from_env() -> IcecastStreamer | None:
    """Create an IcecastStreamer from environment variables, or None if disabled."""
    enabled = os.environ.get("ICECAST_ENABLED", "false").lower() in ("true", "1", "yes")
    if not enabled:
        return None

    host = os.environ.get("ICECAST_HOST", "localhost")
    port = int(os.environ.get("ICECAST_PORT", "8000"))
    password = os.environ.get("ICECAST_PASSWORD", "")
    mount = os.environ.get("ICECAST_MOUNT", "/stream")
    name = os.environ.get("ICECAST_NAME", "MC Clanker")
    genre = os.environ.get("ICECAST_GENRE", "AI Generated")
    description = os.environ.get("ICECAST_DESCRIPTION", "AI-powered continuous music stream")
    url = os.environ.get("ICECAST_URL", "")

    if not password:
        log.warning("ICECAST_ENABLED=true but ICECAST_PASSWORD is not set")

    return IcecastStreamer(
        host=host,
        port=port,
        password=password,
        mount=mount,
        name=name,
        genre=genre,
        description=description,
        url=url,
    )
