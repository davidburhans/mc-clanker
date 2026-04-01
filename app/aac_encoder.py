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


def encode_aac(
    audio: np.ndarray,
    sample_rate: int = 44100,
    bitrate: str = "192k"
) -> bytes:
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
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        wav_path = Path(f.name)
        wavfile.write(wav_path, sample_rate, audio)

    try:
        # Encode to AAC via ffmpeg
        # -c:a aac: Use AAC codec
        # -b:a 192k: 192kbps bitrate
        # -f adts: ADTS container (raw AAC without muxing)
        # -y: Overwrite output file if exists
        # pipe:1: Output to stdout
        result = subprocess.run(
            [
                'ffmpeg',
                '-i', str(wav_path),
                '-c:a', 'aac',
                '-b:a', bitrate,
                '-y',
                '-f', 'adts',  # ADTS container for raw AAC
                '-'  # Output to stdout
            ],
            capture_output=True,
            check=True
        )

        return result.stdout

    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"AAC encoding failed: {e.stderr.decode()}") from e

    finally:
        # Cleanup temp file
        wav_path.unlink(missing_ok=True)


def decode_aac(
    aac_bytes: bytes,
    sample_rate: int = 44100
) -> np.ndarray:
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
    with tempfile.NamedTemporaryFile(suffix='.aac', delete=False) as f:
        aac_path = Path(f.name)
        aac_path.write_bytes(aac_bytes)

    try:
        # Write decoded WAV to temp file
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            wav_path = Path(f.name)

            subprocess.run(
                [
                    'ffmpeg',
                    '-i', str(aac_path),
                    '-y',
                    str(wav_path)
                ],
                capture_output=True,
                check=True
            )

            # Read decoded audio
            decoded_sample_rate, audio = wavfile.read(wav_path)

            # Validate sample rate
            if decoded_sample_rate != sample_rate:
                raise RuntimeError(
                    f"Decoded sample rate {decoded_sample_rate} "
                    f"doesn't match expected {sample_rate}"
                )

            # Normalize to [-1, 1] range
            # scipy.wavfile returns int16 by default
            if audio.dtype == np.int16:
                audio = audio.astype(np.float32) / 32768.0
            elif audio.dtype == np.int32:
                audio = audio.astype(np.float32) / 2147483648.0
            else:
                audio = audio.astype(np.float32)

            return audio

    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"AAC decoding failed: {e.stderr.decode()}") from e

    finally:
        # Cleanup temp files
        aac_path.unlink(missing_ok=True)
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
