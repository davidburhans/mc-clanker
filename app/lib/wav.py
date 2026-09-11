"""Canonical WAV recording header handling (REL-05/U5).

Extracted verbatim from app/routes/shows.py so the REL-05c recording auto-stop
can live in framework_state (routes -> framework_state is the only legal import
direction) without framework_state importing from the routes layer.

Canonical recording format: the mixer emits stereo 16-bit PCM at 44.1kHz
(framework_mixer.py: `(pcm * 32767).astype('<i2').tobytes()`). The WAV header
written here must match so show/export recordings are valid, playable WAVs.
"""

import logging
import struct

log = logging.getLogger(__name__)

RECORD_SAMPLE_RATE = 44100
RECORD_CHANNELS = 2
RECORD_SAMPLE_WIDTH = 2  # bytes per sample (16-bit)
WAV_HEADER_SIZE = 44
# data_size + 36 must fit in a 32-bit RIFF size field.
WAV_MAX_DATA_SIZE = 0xFFFFFFFF - 36


def write_wav_header(handle) -> None:
    """Write a canonical 44-byte WAV header (PCM/16-bit/stereo/44.1kHz).

    RIFF + data sizes are zero placeholders patched by ``finalize_wav`` at close.
    ``broadcast_audio`` then streams raw int16 LE PCM straight into the data chunk
    via ``handle.write()``, so the file is a valid, playable WAV with no postprocess
    (review C4 — show/export recordings were previously headerless raw PCM served
    as ``audio/wav``).
    """
    byte_rate = RECORD_SAMPLE_RATE * RECORD_CHANNELS * RECORD_SAMPLE_WIDTH
    block_align = RECORD_CHANNELS * RECORD_SAMPLE_WIDTH
    handle.write(b"RIFF")
    handle.write(struct.pack("<I", 0))
    handle.write(b"WAVE")
    handle.write(b"fmt ")
    handle.write(
        struct.pack(
            "<IHHIIHH",
            16,
            1,
            RECORD_CHANNELS,
            RECORD_SAMPLE_RATE,
            byte_rate,
            block_align,
            RECORD_SAMPLE_WIDTH * 8,
        )
    )
    handle.write(b"data")
    handle.write(struct.pack("<I", 0))


def finalize_wav(handle) -> None:
    """Patch RIFF + data sizes from file length, then flush + close the handle."""
    if handle is None:
        return
    try:
        total = handle.tell()
    except OSError:
        total = WAV_HEADER_SIZE
    data_size = max(0, total - WAV_HEADER_SIZE)
    try:
        if data_size <= WAV_MAX_DATA_SIZE:
            handle.seek(4)
            handle.write(struct.pack("<I", 36 + data_size))
            handle.seek(40)
            handle.write(struct.pack("<I", data_size))
        else:
            # Round-3 D13: a >4 GiB recording cannot encode its real length in the
            # 32-bit RIFF/data size fields. Leaving the zero placeholders made
            # every size-honoring reader (including Python's own ``wave`` module,
            # which validates the RIFF chunk) reject the WHOLE recording. Write the
            # 0xFFFFFFFF sentinel used by mainstream wav writers instead, so the
            # first 4 GiB stays playable/parseable.
            log.warning("Recording exceeds 4GB WAV limit; writing 0xFFFFFFFF size sentinels")
            handle.seek(4)
            handle.write(struct.pack("<I", 0xFFFFFFFF))
            handle.seek(40)
            handle.write(struct.pack("<I", 0xFFFFFFFF))
    except (OSError, struct.error) as exc:
        log.warning("Could not finalize WAV header sizes: %r", exc)
    try:
        handle.flush()
    except OSError:
        pass
    try:
        handle.close()
    except OSError:
        pass
