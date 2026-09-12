"""U14 REL-32a contracts: ShowPlayback broadcasts format-valid s16le.

Spec: refactor/plans/rel-remediation-plan.md §U14 (REL-32) · Plan:
refactor/plans/units/rel-26-plan.md §3 (T18-T22).

Today `_playback_loop` pipes raw `readframes` bytes into a chain that assumes
s16le (youtube_relay `-f s16le`, stream fan-out, WAV sinks) — a 24-bit file is
broadcast as wrap-around garbage. The fix: decode every chunk (sampwidth
1/2/3/4 + float-WAV scipy fallback) to float32, then to s16le via the exact
mixer/stems AUDIO-1 convention ``(clip(x, -1, 1) * 32767).astype('<i2')``.

Cases
-----
T18  pure converter anchors for every int-PCM width (24-bit sign-extended
     unpack, int32 full-scale, unsigned 8-bit) + 16-bit identity fast path.
T19  acceptance: a real 24-bit stereo WAV broadcasts valid s16le
     ([+0.5, -0.5] frames -> [+16383, -16383], no wrap garbage).
T20  acceptance: a 48 kHz s16le WAV broadcasts byte-identical (identity fast
     path; chunk math honored at a non-44.1k framerate).
T21  acceptance: a format-3 float32 WAV broadcasts sane s16le via the decoded
     fallback (stdlib wave rejects non-PCM; scipy decodes; 0.5 sine -> ~16383 peak).
T22  an unsupported sample width raises ValueError naming the offending value.

This module must stay torch-free (playback imports framework_state only).
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile as scipy_wavfile

from app.framework.framework_state import state

# Lazy named-module import: pre-fix, `wav_chunk_to_s16le` does not exist in
# app.playback, so the import fails and every case below FAILS (not skips) —
# the REL-32a contract is the thing under test.
playback_module = None
_playback_import_error: Exception | None = None
try:
    import app.playback as playback_module  # type: ignore[no-redef]
except Exception as exc:  # noqa: BLE001 - mirror the repo's lazy-import guard breadth
    _playback_import_error = exc


def _require_playback() -> None:
    if playback_module is None:
        pytest.fail(f"REL-32a contract not importable from app.playback: {_playback_import_error}")


class RecordingBroadcaster:
    """Named fake for ``state.broadcast_audio``: records chunks, stops the
    playback after the first so the loop exits in ~one chunk duration."""

    def __init__(self, playback):
        self.playback = playback
        self.chunks: list[bytes] = []

    def __call__(self, pcm_data: bytes) -> None:
        self.chunks.append(pcm_data)
        self.playback.is_playing = False


def _write_pcm_wav(path: Path, frames: bytes, *, channels: int, sampwidth: int, rate: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        wf.writeframes(frames)


# ---------------------------------------------------------------------------
# T18 — pure converter anchors per width
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sampwidth", "raw", "expected_s16"),
    [
        # 24-bit: sign-extended unpack then float->s16 (note: full-scale-minus-1
        # truncates to 32766 under the documented (clip*32767) convention).
        pytest.param(3, bytes.fromhex("000000"), 0, id="w24-zero"),
        pytest.param(3, bytes.fromhex("000080"), -32767, id="w24-neg-full"),  # 0x800000 -> -1.0
        pytest.param(3, bytes.fromhex("ffff7f"), 32766, id="w24-pos-full"),  # 0x7FFFFF -> ~+1.0
        pytest.param(3, bytes.fromhex("000040"), 16383, id="w24-half"),  # 0x400000 -> +0.5
        pytest.param(3, bytes.fromhex("0000c0"), -16383, id="w24-neg-half"),  # 0xC00000 -> -0.5
        # int32: float32 rounds INT32_MAX up to 2^31, so full scale maps to 32767.
        pytest.param(4, (2147483647).to_bytes(4, "little"), 32767, id="w32-pos-full"),
        pytest.param(4, (-2147483648).to_bytes(4, "little", signed=True), -32767, id="w32-neg-full"),
        pytest.param(4, (1073741824).to_bytes(4, "little"), 16383, id="w32-half"),
        # 8-bit WAV PCM is UNSIGNED with a 128 bias.
        pytest.param(1, bytes([0x00]), -32767, id="w8-min"),
        pytest.param(1, bytes([0x80]), 0, id="w8-mid"),
        pytest.param(1, bytes([0xFF]), 32511, id="w8-near-full"),  # 127/128 * 32767 -> 32511.0078
        pytest.param(1, bytes([0x40]), -16383, id="w8-neg-half"),
    ],
)
def test_wav_chunk_to_s16le_width_anchors(sampwidth: int, raw: bytes, expected_s16: int):
    _require_playback()
    out = playback_module.wav_chunk_to_s16le(raw, sampwidth)
    decoded = np.frombuffer(out, dtype="<i2")
    assert decoded.size == 1
    assert int(decoded[0]) == expected_s16, (
        f"width {sampwidth} bytes {raw.hex()} must decode to s16 {expected_s16}, got {int(decoded[0])}"
    )


def test_wav_chunk_to_s16le_16bit_is_identity():
    """sampwidth 2 (this app's recording format) must pass through unchanged."""
    _require_playback()
    data = bytes.fromhex("3412fedc8000")
    assert playback_module.wav_chunk_to_s16le(data, 2) == data


# ---------------------------------------------------------------------------
# T19 — acceptance: 24-bit WAV broadcasts valid s16le end-to-end
# ---------------------------------------------------------------------------


def test_playback_24bit_wav_broadcasts_valid_s16le(tmp_path, monkeypatch):
    _require_playback()
    frames = 682  # == chunk_size 4096 // (sampwidth 3 * channels 2): exactly one loop chunk
    half = (0x400000).to_bytes(3, "little")  # +0.5
    neg_half = (0xC00000).to_bytes(3, "little")  # -0.5
    frame_bytes = b"".join(half + neg_half for _ in range(frames))  # stereo L/R interleaved
    wav_path = tmp_path / "show24.wav"
    _write_pcm_wav(wav_path, frame_bytes, channels=2, sampwidth=3, rate=44100)

    pb = playback_module.ShowPlayback(1, str(wav_path))
    broadcaster = RecordingBroadcaster(pb)
    monkeypatch.setattr(state, "broadcast_audio", broadcaster)

    pb.is_playing = True
    pb._playback_loop()

    assert len(broadcaster.chunks) == 1, "the fake broadcaster stops playback after chunk 1"
    chunk = broadcaster.chunks[0]
    assert len(chunk) % 4 == 0, "broadcast chunk must be whole stereo int16 frames (no ragged tail)"
    samples = np.frombuffer(chunk, dtype="<i2")
    assert samples.size == frames * 2
    expected = np.tile(np.array([16383, -16383], dtype=np.int16), frames)
    assert np.array_equal(samples, expected), (
        "REL-32a: 24-bit [+0.5,-0.5] frames must broadcast as [+16383,-16383] s16le "
        f"(got range [{samples.min()}, {samples.max()}] — raw 24-bit bytes would wrap)"
    )
    assert pb.is_playing is False, "playback loop must clear is_playing in its finally"


# ---------------------------------------------------------------------------
# T20 — acceptance: 48 kHz s16le passes through byte-identical
# ---------------------------------------------------------------------------


def test_playback_48k_int16_wav_passes_through(tmp_path, monkeypatch):
    _require_playback()
    rate = 48000
    frames = 1024  # == chunk_size 4096 // (2 * 2)
    rng = np.random.default_rng(26)
    frame_bytes = rng.integers(-30000, 30000, size=(frames, 2), dtype=np.int16).tobytes()
    wav_path = tmp_path / "show48.wav"
    _write_pcm_wav(wav_path, frame_bytes, channels=2, sampwidth=2, rate=rate)

    pb = playback_module.ShowPlayback(1, str(wav_path))
    broadcaster = RecordingBroadcaster(pb)
    monkeypatch.setattr(state, "broadcast_audio", broadcaster)

    pb.is_playing = True
    pb._playback_loop()

    assert broadcaster.chunks, "playback must broadcast at least one chunk"
    assert broadcaster.chunks[0] == frame_bytes, (
        "REL-32a: s16le input must broadcast byte-identical (identity fast path), "
        "chunked at the file's own framerate"
    )


# ---------------------------------------------------------------------------
# T21 — acceptance: float32 (format 3) WAV via the scipy fallback
# ---------------------------------------------------------------------------


def test_playback_float32_wav_broadcasts_valid_s16le(tmp_path, monkeypatch):
    _require_playback()
    rate = 44100
    t = np.arange(rate) / rate  # 1 s
    mono = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    wav_path = tmp_path / "showfloat.wav"
    scipy_wavfile.write(str(wav_path), rate, mono)  # WAV format 3 (float) — stdlib wave rejects it

    pb = playback_module.ShowPlayback(1, str(wav_path))
    broadcaster = RecordingBroadcaster(pb)
    monkeypatch.setattr(state, "broadcast_audio", broadcaster)

    pb.is_playing = True
    pb._playback_loop()

    assert broadcaster.chunks, "REL-32a: float WAV must broadcast via the decoded fallback"
    samples = np.frombuffer(broadcaster.chunks[0], dtype="<i2")
    assert samples.size > 0
    assert samples.max() <= 16400 and samples.min() >= -16400, (
        "a 0.5-bounded float source must broadcast 0.5-bounded s16le (no wrap garbage)"
    )
    assert samples.max() > 16000 and samples.min() < -16000, (
        "a 0.5-amplitude sine must reach ~16383 peak within the first chunk "
        f"(got range [{samples.min()}, {samples.max()}])"
    )


# ---------------------------------------------------------------------------
# T22 — unsupported width rejected with the offending value
# ---------------------------------------------------------------------------


def test_wav_chunk_to_s16le_rejects_unknown_width():
    _require_playback()
    with pytest.raises(ValueError, match="sample width"):
        playback_module.wav_chunk_to_s16le(b"\x00" * 15, 5)
