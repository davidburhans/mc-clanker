"""Regression tests for the EXTERNAL-IO adversarial-review fixes.

Covers:
- B3: GarageClient S3 timeouts/retries (no more indefinite hangs).
- B4: Icecast ffmpeg stderr/stdout -> DEVNULL + `-nostats -loglevel error`
      (eliminates the pipe-buffer deadlock).
- B5: aac_encoder ffmpeg subprocess now bounded by ``FFMPEG_TIMEOUT``.

These are self-contained and do NOT depend on ``state.icecast_streamer``
(which is not wired into GlobalState and breaks ``tests/test_icecast.py``).
"""

import subprocess
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from app import aac_encoder
from app.aac_encoder import FFMPEG_TIMEOUT, _normalize_decoded_audio, decode_aac, encode_aac
from app.framework.framework_icecast import IcecastStreamer
from app.garage_client import (
    DEFAULT_S3_TIMEOUTS,
    GarageClient,
    GarageConfig,
    S3Timeouts,
    build_boto3_config,
)


# --------------------------------------------------------------------------- #
# B3 — Garage S3 timeouts / retries
# --------------------------------------------------------------------------- #
class TestS3Timeouts:
    """build_boto3_config must bake in connect/read timeouts + retries."""

    def test_default_timeouts_are_bounded(self):
        """The shipped defaults bound every S3 call instead of hanging forever."""
        assert DEFAULT_S3_TIMEOUTS.connect_timeout == 5.0
        assert DEFAULT_S3_TIMEOUTS.read_timeout == 30.0
        assert DEFAULT_S3_TIMEOUTS.max_attempts == 3

    def test_build_boto3_config_carries_timeouts(self):
        """Config exposes the bounded timeouts botocore will actually enforce."""
        config = build_boto3_config()
        assert config.connect_timeout == 5.0
        assert config.read_timeout == 30.0
        assert config.retries == {"max_attempts": 3, "mode": "adaptive"}

    def test_build_boto3_config_respects_override(self):
        """Custom timeouts propagate into the generated Config."""
        custom = S3Timeouts(connect_timeout=1.0, read_timeout=2.0, max_attempts=7)
        config = build_boto3_config(custom)
        assert config.connect_timeout == 1.0
        assert config.read_timeout == 2.0
        assert config.retries == {"max_attempts": 7, "mode": "adaptive"}

    def test_garage_client_wires_timeouts_into_boto3(self):
        """A real GarageClient hands botocore a Config with bounded timeouts."""
        client = GarageClient(
            GarageConfig(
                endpoint="http://garage.test:3900",
                access_key="ak",
                secret_key="sk",
                bucket="mcclanker",
            )
        )
        boto_config = client._client.meta.config
        assert boto_config.connect_timeout == 5.0
        assert boto_config.read_timeout == 30.0


# --------------------------------------------------------------------------- #
# B5 — aac_encoder bounded ffmpeg timeout
# --------------------------------------------------------------------------- #
class TestAacTimeout:
    """encode_aac/decode_aac must bound ffmpeg and surface clear errors."""

    def test_timeout_constant_is_bounded(self):
        """FFMPEG_TIMEOUT is a sane, finite ceiling."""
        assert isinstance(FFMPEG_TIMEOUT, int)
        assert 10 <= FFMPEG_TIMEOUT <= 300

    def test_run_ffmpeg_translates_timeout_to_runtime_error(self):
        """A hung ffmpeg surfaces as a RuntimeError naming the op + ceiling."""
        with patch.object(
            aac_encoder.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=60)
        ):
            with pytest.raises(RuntimeError, match="timed out after"):
                aac_encoder._run_ffmpeg(["ffmpeg"], "AAC encoding")

    def test_run_ffmpeg_translates_called_process_error(self):
        """A non-zero exit surfaces as a RuntimeError including stderr text."""
        err = subprocess.CalledProcessError(returncode=1, cmd=["ffmpeg"])
        err.stderr = b"Unknown encoder 'bogus'"
        with patch.object(aac_encoder.subprocess, "run", side_effect=err):
            with pytest.raises(RuntimeError, match="Unknown encoder"):
                aac_encoder._run_ffmpeg(["ffmpeg"], "AAC encoding")

    def test_run_ffmpeg_returns_stdout_on_success(self):
        """A clean ffmpeg run returns its stdout bytes."""
        good = MagicMock(stdout=b"AACDATA", returncode=0)
        with patch.object(aac_encoder.subprocess, "run", return_value=good) as mock_run:
            out = aac_encoder._run_ffmpeg(["ffmpeg", "-version"], "AAC encoding")
        assert out == b"AACDATA"
        # The bounded timeout must actually be passed through.
        assert mock_run.call_args.kwargs["timeout"] == FFMPEG_TIMEOUT


@pytest.mark.skipif(
    not __import__("shutil").which("ffmpeg"),
    reason="ffmpeg not on PATH",
)
class TestAacRoundtrip:
    """Real ffmpeg encode/decode roundtrip (fixes the previously-skipped AAC test)."""

    def _sine(self, seconds: float = 0.5, sr: int = 44100, freq: float = 440.0) -> np.ndarray:
        t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
        tone = (np.sin(2 * np.pi * freq * t) * 0.3).astype(np.float32)
        return np.stack([tone, tone], axis=1)

    def test_encode_then_decode_preserves_shape_and_range(self):
        """An encoded/decoded stereo buffer keeps channel count and stays in range."""
        sr = 44100
        original = self._sine(sr=sr)
        aac = encode_aac(original, sample_rate=sr)
        assert len(aac) > 0
        decoded = decode_aac(aac, sample_rate=sr)
        assert decoded.ndim == 2
        assert decoded.shape[1] == 2  # stereo preserved
        assert decoded.dtype == np.float32
        assert float(decoded.min()) >= -1.0 and float(decoded.max()) <= 1.0

    def test_normalize_decoded_audio_int16(self):
        """int16 PCM maps onto [-1, 1] using its full scale."""
        samples = np.array([[0, -32768, 32767]], dtype=np.int16)
        norm = _normalize_decoded_audio(samples)
        assert norm.dtype == np.float32
        assert pytest.approx(float(norm[0][0]), abs=1e-6) == 0.0
        assert pytest.approx(float(norm[0][1]), abs=1e-3) == -1.0


# --------------------------------------------------------------------------- #
# B4 — Icecast ffmpeg pipe-buffer deadlock
# --------------------------------------------------------------------------- #
class TestIcecastNoStderrDeadlock:
    """ffmpeg must discard stdout/stderr (no undrained pipe) and run quietly."""

    def _streamer(self) -> IcecastStreamer:
        return IcecastStreamer(
            host="localhost",
            port=9999,
            password="test",
            mount="/test",
            bitrate=128,
            sample_rate=44100,
            channels=2,
        )

    def test_stream_loop_builds_quiet_command(self):
        """The streaming command includes `-nostats` + `-loglevel error`."""
        import inspect

        body = inspect.getsource(IcecastStreamer._stream_loop)
        assert "-nostats" in body
        # Check the two tokens semantically (they sit on adjacent list entries,
        # possibly across lines/comments, so don't assert a single substring).
        assert '"-loglevel"' in body
        assert '"error"' in body

    def test_popen_uses_devnull_not_pipe(self):
        """ffmpeg is started with stdout=DEVNULL and stderr=DEVNULL (no deadlock)."""
        streamer = self._streamer()
        with patch("app.framework.framework_icecast.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None  # ffmpeg "running"
            mock_proc.stdin = MagicMock()
            mock_popen.return_value = mock_proc

            streamer.start()
            streamer.feed_pcm(b"\x00" * 1024)  # unblock the queue.get wait-for-first-chunk
            streamer.stop()

            assert mock_popen.called
            kwargs = mock_popen.call_args.kwargs
            assert kwargs["stdin"] == subprocess.PIPE
            assert kwargs["stdout"] == subprocess.DEVNULL
            assert kwargs["stderr"] == subprocess.DEVNULL
            # The exact condition the deadlock bug violated:
            assert kwargs["stderr"] != subprocess.PIPE

    def test_stdin_narrowing_helper_raises_when_unavailable(self):
        """_ffmpeg_stdin raises an explicit error instead of an AttributeError."""
        streamer = self._streamer()
        streamer._ffmpeg_proc = None
        with pytest.raises(RuntimeError, match="stdin pipe is unavailable"):
            streamer._ffmpeg_stdin()
