"""
AAC Encoder - Converts numpy audio arrays to AAC format for storage.

Uses ffmpeg subprocess for encoding (stable-audio-tools outputs float32 arrays).
AAC chosen over MP3 for better quality/size ratio at similar bitrates.

Usage:
    audio = np.random.randn(44100 * 4).astype(np.float32)  # 4 seconds stereo
    aac_bytes = encode_aac(audio, sample_rate=44100)

    # Decode for verification
    decoded = decode_aac(aac_bytes, sample_rate=44100)
"""

import subprocess
import tempfile
from pathlib import Path

import numpy as np
from scipy.io import wavfile

#: Hard ceiling on any single ffmpeg encode/decode. Without this, a hung codec
#: loop or missing-codec scenario blocks ``subprocess.run`` forever, wedging a
#: worker executor thread or the framework audio-fetch path.
FFMPEG_TIMEOUT = 60  # seconds


def _run_ffmpeg(cmd: list[str], label: str) -> bytes:
    """Run an ffmpeg subprocess with a bounded timeout; return stdout.

    Args:
        cmd: ffmpeg argument list (never shell-interpolated).
        label: human-readable operation name for error context.

    Returns:
        ffmpeg stdout bytes.

    Raises:
        RuntimeError: on timeout (``FFMPEG_TIMEOUT`` exceeded) or non-zero exit,
            with the ffmpeg stderr (if any) included in the message.
    """
    try:
        result = subprocess.run(cmd, capture_output=True, check=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{label} timed out after {FFMPEG_TIMEOUT}s") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode(errors="replace")
        raise RuntimeError(f"{label} failed: {stderr}") from exc
    return result.stdout


def _normalize_decoded_audio(audio: np.ndarray) -> np.ndarray:
    """Normalize a decoded WAV array to float32 in the [-1.0, 1.0] range.

    ``scipy.io.wavfile`` returns int PCM by default; convert to float using the
    full-scale amplitude of the integer dtype.
    """
    if audio.dtype == np.int16:
        return audio.astype(np.float32) / 32768.0
    if audio.dtype == np.int32:
        return audio.astype(np.float32) / 2147483648.0
    return audio.astype(np.float32)


def encode_aac(audio: np.ndarray, sample_rate: int = 44100, bitrate: str = "192k") -> bytes:
    """
    Encode audio array to AAC format using ffmpeg.

    Args:
        audio: numpy array of audio samples
               - Shape (samples,) for mono
               - Shape (samples, channels) for multi-channel
               - Values should be in range [-1.0, 1.0]
        sample_rate: Sample rate in Hz (default 44100)
        bitrate: Audio bitrate (default 192k)

    Returns:
        AAC-encoded bytes (ADTS container format)

    Raises:
        ValueError: If audio array has invalid dimensions
        RuntimeError: If ffmpeg encoding fails
    """
    # Ensure audio is float32
    audio = audio.astype(np.float32)

    # Handle dimensions
    if audio.ndim == 1:
        # Mono - convert to stereo by duplicating
        audio = np.stack([audio, audio], axis=1)
    elif audio.ndim != 2:
        raise ValueError(f"Expected 1D or 2D array, got {audio.ndim}D")

    num_samples, num_channels = audio.shape
    if num_channels > 2:
        # Limit to stereo max - mix down if more channels
        audio = audio[:, :2]
        num_channels = 2

    # Write temporary WAV file for ffmpeg to process
    # scipy.io.wavfile.write expects (samples, channels) float32 in [-1, 1]
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = Path(f.name)
        wavfile.write(wav_path, sample_rate, audio)

    try:
        # Encode to AAC via ffmpeg
        # -c:a aac: Use AAC codec
        # -b:a 192k: 192kbps bitrate
        # -f adts: ADTS container (raw AAC without muxing)
        # -y: Overwrite output file if exists
        # pipe:1: Output to stdout
        return _run_ffmpeg(
            [
                "ffmpeg",
                "-i",
                str(wav_path),
                "-c:a",
                "aac",
                "-b:a",
                bitrate,
                "-y",
                "-f",
                "adts",  # ADTS container for raw AAC
                "-",  # Output to stdout
            ],
            "AAC encoding",
        )
    finally:
        # Cleanup temp file
        wav_path.unlink(missing_ok=True)


def decode_aac(aac_bytes: bytes, sample_rate: int = 44100) -> np.ndarray:
    """
    Decode AAC bytes back to numpy array for verification/testing.

    Args:
        aac_bytes: AAC-encoded bytes
        sample_rate: Expected output sample rate (used for validation)

    Returns:
        numpy array of audio samples, shape (samples, channels)
        Values normalized to [-1.0, 1.0] range

    Raises:
        RuntimeError: If ffmpeg decoding fails
    """
    # Write AAC bytes to temp file
    with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as f:
        aac_path = Path(f.name)
        aac_path.write_bytes(aac_bytes)

    # Assign wav_path up-front so the ``finally`` cleanup is always defined even
    # if WAV temp-file creation itself raises (avoids UnboundLocalError).
    wav_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = Path(f.name)

            _run_ffmpeg(
                ["ffmpeg", "-i", str(aac_path), "-y", str(wav_path)],
                "AAC decoding",
            )

            # Read decoded audio
            decoded_sample_rate, audio = wavfile.read(wav_path)

            # Validate sample rate
            if decoded_sample_rate != sample_rate:
                raise RuntimeError(f"Decoded sample rate {decoded_sample_rate} doesn't match expected {sample_rate}")

            return _normalize_decoded_audio(audio)
    finally:
        # Cleanup temp files
        aac_path.unlink(missing_ok=True)
        if wav_path is not None:
            wav_path.unlink(missing_ok=True)


def get_audio_duration(audio: np.ndarray, sample_rate: int = 44100) -> float:
    """
    Calculate duration of audio array in seconds.

    Args:
        audio: numpy array of audio samples
        sample_rate: Sample rate in Hz

    Returns:
        Duration in seconds
    """
    if audio.ndim == 1:
        num_samples = len(audio)
    else:
        num_samples = audio.shape[0]
    return num_samples / sample_rate
