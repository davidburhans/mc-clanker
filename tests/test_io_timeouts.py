"""Regression tests for the EXTERNAL-IO adversarial-review fixes.

Covers:
- B3: GarageClient S3 timeouts/retries (no more indefinite hangs).
- B5: aac_encoder ffmpeg subprocess now bounded by ``FFMPEG_TIMEOUT``.
"""

import subprocess
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from app import aac_encoder
from app.aac_encoder import FFMPEG_TIMEOUT, _normalize_decoded_audio, decode_aac, encode_aac
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
# REL-21 — AAC decode float-branch NaN/Inf sanitization (no ffmpeg needed)
# --------------------------------------------------------------------------- #
class TestNormalizeDecodedAudioSanitization:
    """_normalize_decoded_audio must sanitize the float default branch (REL-21).

    Float WAVs can carry NaN/Inf from a corrupt stem; ``astype`` preserves
    them and a downstream clip would too. The int branches cannot contain
    NaN/Inf and must keep their exact full-scale mapping.
    """

    @pytest.mark.parametrize("dtype", [np.float32, np.float64])
    def test_normalize_decoded_audio_sanitizes_float_nan_inf(self, dtype):
        """float32/float64 inputs map NaN→0.0, ±inf→±1.0, keep finite values."""
        poisoned = np.array([[np.nan, np.inf, -np.inf, 0.25]], dtype=dtype)
        norm = _normalize_decoded_audio(poisoned)
        assert norm.dtype == np.float32
        assert np.isfinite(norm).all(), "NaN/Inf must not survive normalization"
        assert norm[0][0] == 0.0, "NaN must map to 0.0"
        assert norm[0][1] == 1.0, "+inf must clamp to 1.0"
        assert norm[0][2] == -1.0, "-inf must clamp to -1.0"
        assert norm[0][3] == 0.25, "finite values must pass through unchanged"

    def test_normalize_decoded_audio_int_branches_unchanged(self):
        """int16 full-scale mapping is untouched by the float sanitization."""
        samples = np.array([[-32768, 32767]], dtype=np.int16)
        norm = _normalize_decoded_audio(samples)
        assert norm.dtype == np.float32
        assert norm[0][0] == -1.0
        assert pytest.approx(float(norm[0][1]), abs=1e-5) == 0.99997
