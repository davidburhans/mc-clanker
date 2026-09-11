"""
recording_metadata.py — CUE sheets, WAV metadata embedding, and section splitting
for mc-clanker show recordings.

Provides:
- write_cue_sheet(): Write a CUE file from chapter markers
- embed_wav_metadata(): Embed LIST/INFO chunks into a WAV file
- split_wav_by_chapters(): Split a WAV file at chapter boundaries
- build_chapter_markers(): Build chapter metadata from loop transitions and actions
"""

import json
import os
import struct
import time
import wave
from typing import Any

# RIFF announces both the file and the data chunk size as a uint32 (review E8).
_WAV_MAX_SIZE_FIELD = 0xFFFFFFFF


def format_cue_time(seconds: float) -> str:
    """Format time as MM:SS:FF (CD frames, 75 frames per second)."""
    total_frames = int(seconds * 75)
    frames = total_frames % 75
    total_seconds = int(seconds) % 60
    minutes = int(seconds) // 60
    return f"{minutes:02d}:{total_seconds:02d}:{frames:02d}"


def write_cue_sheet(
    cue_path: str,
    audio_filename: str,
    chapters: list[dict[str, Any]],
    title: str = "",
    performer: str = "MC Clanker",
    metadata: dict | None = None,
) -> str:
    """
    Write a CUE sheet file from chapter markers.

    Each chapter dict should have:
      - index: int (1-based chapter number)
      - timestamp: float (seconds from start)
      - title: str (chapter title, e.g. "Loop 1 — Warm Synth, C minor 120 BPM")
      - reasoning: str (optional Conductor reasoning)

    Returns the path written.
    """
    lines = []
    if title:
        lines.append(f'TITLE "{_cue_text(title)}"')
    lines.append(f'PERFORMER "{_cue_text(performer)}"')
    lines.append(f'FILE "{_cue_text(audio_filename)}" WAVE')

    for chapter in chapters:
        idx = chapter["index"]
        ts = chapter["timestamp"]
        ctitle = chapter.get("title", f"Chapter {idx}")
        lines.append(f"  TRACK {idx:02d} AUDIO")
        lines.append(f'    TITLE "{_cue_text(ctitle)}"')
        lines.append(f"    INDEX 01 {format_cue_time(ts)}")
        if chapter.get("reasoning"):
            # CUE doesn't have a standard comment field for tracks, but we can use REM.
            # REM stays unquoted, so quotes are swapped and CR/LF collapsed (E9).
            reasoning = _cue_comment(chapter["reasoning"])
            lines.append(f"    REM COMMENT {reasoning}")

    with open(cue_path, "w", encoding="utf-8") as f:
        f.write("\r\n".join(lines) + "\r\n")

    return cue_path


def _cue_text(value: str) -> str:
    """Escape free text for a quoted CUE field (TITLE/PERFORMER/FILE), review E9.

    An unescaped double quote terminates the field early and a raw CR/LF splits
    one CUE command into two, so hostile LLM-derived titles used to produce an
    unparseable sheet. Usage: ``f'TITLE "{_cue_text(title)}"'``.
    """
    return " ".join(str(value).split()).replace('"', '\\"')


def _cue_comment(value: str) -> str:
    """Collapse CR/LF (and quote-swap) for an unquoted ``REM`` comment field (E9)."""
    return " ".join(str(value).split()).replace('"', "'")


def embed_wav_metadata(
    wav_path: str,
    metadata: dict[str, str],
    chapters: list[dict[str, Any]] | None = None,
) -> str:
    """
    Embed metadata into a WAV file by writing LIST/INFO and chunks before the
    data chunk. Reads the original WAV, rewrites with metadata prepended.

    metadata: dict of key-value pairs (e.g. {"title": "...", "bpm": "120", "key": "C minor"})
    chapters: optional list of chapter dicts with "title", "timestamp" for chapter markers

    Returns the path written (same as input; file is rewritten in place).
    """
    # Read the original WAV
    with wave.open(wav_path, "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        n_frames = wf.getnframes()
        audio_data = wf.readframes(n_frames)

    # Build LIST/INFO chunk
    info_lines = []
    if metadata:
        for key, value in metadata.items():
            tag = key.upper()[:4]
            # Map common keys to standard INFO tags
            tag_map = {
                "TITLE": "INAM",
                "ARTIST": "IART",
                "ALBUM": "IPRD",
                "COMMENT": "ICMT",
                "BPM": "IBPM",
                "KEY": "IGNR",
                "DATE": "ICRD",
                "GENRE": "IGNR",
            }
            info_tag = tag_map.get(key.upper(), tag[:4].ljust(4))
            info_lines.append((info_tag, str(value)))

    list_chunk = _build_list_chunk(info_lines)

    # Build CUE chunk for chapter markers (WAV-native binary cue points)
    cue_chunk = _build_cue_chunk(chapters) if chapters else b""

    # Build a LIST/adtl chapter chunk (labels for cue points)
    chapter_list_chunk = _build_chapter_list_chunk(chapters) if chapters else b""

    # The wave module doesn't support custom chunks, so the file is written by
    # hand below. There used to be a vestigial ``wave.open(wav_path, "wb")`` here
    # that only set parameters and ``pass``ed: it truncated the recording to a
    # 44-byte header before the rewrite, destroying it on any failure in between
    # (review E8).
    _write_wav_with_metadata(
        wav_path, n_channels, sampwidth, framerate, audio_data, list_chunk, cue_chunk, chapter_list_chunk
    )

    return wav_path


def _build_list_chunk(info_lines: list[tuple]) -> bytes:
    """Build a LIST/INFO chunk."""
    data = b""
    for tag, value in info_lines:
        # Null-terminate the value
        value_bytes = value.encode("utf-8") + b"\x00"
        # Pad to even length
        if len(value_bytes) % 2 != 0:
            value_bytes += b"\x00"
        chunk_data = tag.encode("ascii") + struct.pack("<I", len(value_bytes)) + value_bytes
        data += chunk_data

    if not data:
        return b""

    # LIST header + 'INFO' subtype
    header = b"LIST" + struct.pack("<I", len(data) + 4) + b"INFO"
    return header + data


def _build_cue_chunk(chapters: list[dict[str, Any]]) -> bytes:
    """
    Build a WAV 'cue ' chunk with cue points for each chapter.
    Each cue point: (id, position, data_chunk_id, chunk_start, block_start, sample_offset)
    """
    cue_points = []
    for i, chapter in enumerate(chapters):
        cue_points.append(
            {
                "id": i + 1,
                "position": 0,  # Play order position
                "data_chunk_id": b"data",
                "chunk_start": 0,
                "block_start": 0,
                "sample_offset": int(chapter["timestamp"] * 44100),  # assumes 44100 Hz
            }
        )

    # Build cue chunk data
    data = struct.pack("<I", len(cue_points))
    for cp in cue_points:
        data += struct.pack(
            "<IIIIII",
            cp["id"],
            cp["position"],
            int.from_bytes(cp["data_chunk_id"], "little") if isinstance(cp["data_chunk_id"], int) else 0x64617461,
            cp["chunk_start"],
            cp["block_start"],
            cp["sample_offset"],
        )

    # Fix: data_chunk_id should be 'data' = 0x64617461
    data = struct.pack("<I", len(cue_points))
    for cp in cue_points:
        data += struct.pack(
            "<IIIIII",
            cp["id"],
            cp["position"],
            0x64617461,  # 'data'
            cp["chunk_start"],
            cp["block_start"],
            cp["sample_offset"],
        )

    header = b"cue " + struct.pack("<I", len(data))
    return header + data


def _adtl_sub_chunk(fourcc: bytes, cue_id: int, text: str) -> bytes:
    """Build one adtl sub-chunk: ``<fourcc><uint32 size><cue id><NUL text>[pad]``.

    The size field counts the logical payload only; the RIFF word-alignment byte
    is written but never announced (review E10).
    """
    payload = struct.pack("<I", cue_id) + text.encode("utf-8") + b"\x00"
    padded = payload + b"\x00" if len(payload) % 2 else payload
    return fourcc + struct.pack("<I", len(payload)) + padded


def _build_chapter_list_chunk(chapters: list[dict[str, Any]]) -> bytes:
    """Build a LIST/adtl chunk of proper ``labl``/``note`` sub-chunks (review E10).

    The previous layout emitted a bare ``<cue id><0>`` prefix ahead of an
    *embedded* ``labl`` fourcc, so a strict RIFF walker read the 8-byte prefix as
    a chunk header and desynced on the whole LIST/adtl payload.
    """
    data = b""
    for i, chapter in enumerate(chapters):
        data += _adtl_sub_chunk(b"labl", i + 1, chapter.get("title", f"Chapter {i + 1}"))
        if chapter.get("reasoning"):
            data += _adtl_sub_chunk(b"note", i + 1, chapter["reasoning"])

    if not data:
        return b""

    header = b"LIST" + struct.pack("<I", len(data) + 4) + b"adtl"
    return header + data


def _write_wav_with_metadata(
    path: str,
    n_channels: int,
    sampwidth: int,
    framerate: int,
    audio_data: bytes,
    list_chunk: bytes,
    cue_chunk: bytes,
    chapter_list_chunk: bytes,
):
    """Write a complete WAV file with custom metadata chunks.

    Raises:
        ValueError: when the result would overflow the 32-bit RIFF/data size
            fields (>4 GiB) — review E8, mirroring routes/shows.py.
    """
    fmt_data = struct.pack(
        "<HHIIHH",
        1,  # PCM format
        n_channels,
        framerate,
        framerate * n_channels * sampwidth,
        n_channels * sampwidth,
        sampwidth * 8,
    )
    head = b"fmt " + struct.pack("<I", len(fmt_data)) + fmt_data + list_chunk + cue_chunk + chapter_list_chunk
    data_size = len(audio_data)
    riff_size = 4 + len(head) + 8 + data_size  # 'WAVE' + fmt/metadata chunks + data header + data
    _ensure_wav_sizes_fit(data_size, riff_size)

    with open(path, "wb") as f:
        # RIFF header
        f.write(b"RIFF")
        f.write(struct.pack("<I", riff_size))
        f.write(b"WAVE")
        f.write(head)

        # data chunk
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(audio_data)


def _ensure_wav_sizes_fit(data_size: int, riff_size: int) -> None:
    """Refuse to write a WAV whose uint32 size fields would overflow (review E8)."""
    if data_size > _WAV_MAX_SIZE_FIELD or riff_size > _WAV_MAX_SIZE_FIELD:
        raise ValueError(
            f"WAV too large for the 32-bit RIFF header: data={data_size} bytes, "
            f"riff_size={riff_size} bytes, max={_WAV_MAX_SIZE_FIELD} bytes (4 GiB)"
        )


def split_wav_by_chapters(
    wav_path: str,
    chapters: list[dict[str, Any]],
    output_dir: str,
    show_title: str = "show",
) -> list[dict[str, Any]]:
    """
    Split a WAV file at chapter boundaries.

    Each chapter dict must have:
      - index: int
      - timestamp: float (seconds)

    Returns list of dicts with 'path', 'index', 'title', 'start', 'duration'.
    """
    if not chapters:
        return []

    with wave.open(wav_path, "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        n_frames = wf.getnframes()
        audio_data = wf.readframes(n_frames)

    total_duration = n_frames / framerate

    # Sort chapters by timestamp
    sorted_chapters = sorted(chapters, key=lambda c: c["timestamp"])

    segments = []
    for i, chapter in enumerate(sorted_chapters):
        start_time = chapter["timestamp"]
        if i + 1 < len(sorted_chapters):
            end_time = sorted_chapters[i + 1]["timestamp"]
        else:
            end_time = total_duration

        start_sample = int(start_time * framerate)
        end_sample = int(end_time * framerate)

        # Clamp to file bounds
        start_sample = max(0, min(start_sample, n_frames))
        end_sample = max(start_sample, min(end_sample, n_frames))

        byte_offset = start_sample * n_channels * sampwidth
        byte_length = (end_sample - start_sample) * n_channels * sampwidth
        segment_data = audio_data[byte_offset : byte_offset + byte_length]

        safe_title = chapter.get("title", f"chapter_{i + 1}").replace(" ", "_").replace("/", "_")[:50]
        filename = f"show_{show_title}_{i + 1:02d}_{safe_title}.wav"
        seg_path = os.path.join(output_dir, filename)

        with wave.open(seg_path, "wb") as wf:
            wf.setnchannels(n_channels)
            wf.setsampwidth(sampwidth)
            wf.setframerate(framerate)
            wf.writeframes(segment_data)

        segments.append(
            {
                "path": seg_path,
                "index": chapter["index"],
                "title": chapter.get("title", f"Chapter {i + 1}"),
                "start": start_time,
                "duration": end_time - start_time,
            }
        )

    return segments


def build_chapter_markers(
    loop_history: list[dict],
    actions: list[dict],
    llm_interactions: list[dict],
    config_snapshot: dict | None = None,
) -> list[dict[str, Any]]:
    """
    Build chapter marker metadata from loop transitions, actions, and LLM interactions.

    Each chapter represents one loop iteration with:
      - index: loop number (1-based)
      - timestamp: seconds from show start
      - title: descriptive title
      - reasoning: Conductor reasoning text
      - bpm/key: musical context at that point
      - stems: list of active stems
    """
    chapters = []

    # Build a lookup of actions and interactions by loop
    actions_by_loop = {}
    for a in actions:
        li = a.get("loop_index", 0)
        if li not in actions_by_loop:
            actions_by_loop[li] = []
        actions_by_loop[li].append(a)

    interactions_by_loop = {}
    for i in llm_interactions:
        li = i.get("loop_index", 0)
        if li not in interactions_by_loop:
            interactions_by_loop[li] = []
        interactions_by_loop[li].append(i)

    for loop in loop_history:
        loop_idx = loop.get("loop_index", 0)
        timestamp = loop.get("timestamp", 0)
        set_name = loop.get("set_name", "")
        reasoning = loop.get("reasoning", "")
        stems = loop.get("stems", [])

        # Calculate relative timestamp from show start
        if loop_idx == 1:
            ts_seconds = 0.0
        else:
            ts_seconds = timestamp

        # Build a descriptive title
        stem_names = []
        for s in stems:
            if isinstance(s, dict):
                name = s.get("sub_family", s.get("major_family", ""))
                if name:
                    stem_names.append(name)
            elif isinstance(s, str):
                stem_names.append(s)

        stem_summary = ", ".join(stem_names[:3])
        if len(stem_names) > 3:
            stem_summary += f" +{len(stem_names) - 3} more"

        title = f"Loop {loop_idx}"
        if set_name:
            title += f" — {set_name}"
        if stem_summary:
            title += f" [{stem_summary}]"

        # Get BPM/key from config snapshot or interaction
        bpm = ""
        key = ""
        if config_snapshot:
            bpm = str(config_snapshot.get("bpm", ""))
            key = str(config_snapshot.get("key", ""))

        # Check if there's an interaction with updated bpm/key
        for interaction in interactions_by_loop.get(loop_idx, []):
            parsed = interaction.get("parsed_response", {})
            if parsed:
                if parsed.get("master_bpm"):
                    bpm = str(parsed["master_bpm"])
                if parsed.get("master_key"):
                    key = str(parsed["master_key"])

        if bpm and key:
            title += f" — {key} {bpm} BPM"

        chapter = {
            "index": loop_idx,
            "timestamp": ts_seconds,
            "title": title,
            "reasoning": reasoning,
            "bpm": bpm,
            "key": key,
            "stems": stems,
        }
        chapters.append(chapter)

    return chapters


def generate_export_filename(
    show_id: int,
    show_title: str,
    format: str = "wav",
    include_chapters: bool = False,
) -> str:
    """Generate a descriptive filename for exported audio."""
    safe_title = show_title.replace(" ", "_").replace("/", "_")[:40]
    suffix = "_chapters" if include_chapters else ""
    return f"show_{show_id}_{safe_title}{suffix}.{format}"


def write_metadata_json(
    output_path: str,
    show_data: dict,
    chapters: list[dict],
    format: str = "wav",
) -> str:
    """Write a sidecar JSON file with full metadata for the export."""
    export_meta = {
        "format": format,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "show": show_data,
        "chapters": [
            {
                "index": c["index"],
                "timestamp": c["timestamp"],
                "title": c["title"],
                "reasoning": c.get("reasoning", ""),
                "bpm": c.get("bpm", ""),
                "key": c.get("key", ""),
            }
            for c in chapters
        ],
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(export_meta, f, indent=2, ensure_ascii=False)
    return output_path
