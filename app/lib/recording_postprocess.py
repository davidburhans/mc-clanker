"""
recording_postprocess.py — Post-show recording enrichment for mc-clanker.

Called after a show stops to:
1. Build chapter markers from loop history, actions, and LLM interactions
2. Write a CUE sheet alongside the audio file
3. Embed LIST/INFO + cue chunks into the WAV file
4. Produce a sidecar JSON metadata file
5. Optionally split into per-chapter WAV segments and export MP3/FLAC with ID3 tags
"""

import os
import json
import time
import logging
from typing import List, Dict, Optional, Any

from app.lib.recording_metadata import (
    build_chapter_markers,
    write_cue_sheet,
    embed_wav_metadata,
    split_wav_by_chapters,
    write_metadata_json,
    generate_export_filename,
)

log = logging.getLogger(__name__)


def compute_loop_timestamps(
    actions: List[Dict],
    interactions: List[Dict],
    show_data: Dict,
) -> Dict[int, float]:
    """
    Compute per-loop start timestamps in seconds from show start.

    Uses relative_time_ms from actions/interactions to build a timeline.
    Returns dict mapping loop_index -> seconds_from_start.
    """
    timestamps: Dict[int, float] = {}

    for interaction in interactions:
        li = interaction.get("loop_index", 0)
        rel_ms = interaction.get("relative_time_ms", 0)
        if li not in timestamps:
            timestamps[li] = rel_ms / 1000.0
        else:
            # Use earliest timestamp for each loop
            timestamps[li] = min(timestamps[li], rel_ms / 1000.0)

    for action in actions:
        li = action.get("loop_index", 0)
        rel_ms = action.get("relative_time_ms", 0)
        if li not in timestamps:
            timestamps[li] = rel_ms / 1000.0
        else:
            timestamps[li] = min(timestamps[li], rel_ms / 1000.0)

    # Ensure loop 1 starts at 0 if present
    if 1 in timestamps and timestamps[1] > 0:
        offset = timestamps[1]
        for k in timestamps:
            timestamps[k] -= offset

    return timestamps


def build_loop_history_from_db(
    actions: List[Dict],
    interactions: List[Dict],
    config_snapshot: Optional[Dict] = None,
) -> List[Dict]:
    """
    Reconstruct loop_history format expected by build_chapter_markers
    from DB actions and interactions.

    Each loop entry: {loop_index, timestamp, set_name, reasoning, stems}
    """
    loops: Dict[int, Dict] = {}

    for interaction in interactions:
        li = interaction.get("loop_index", 0)
        if li not in loops:
            loops[li] = {
                "loop_index": li,
                "timestamp": interaction.get("relative_time_ms", 0) / 1000.0,
                "set_name": interaction.get("set_name", ""),
                "reasoning": interaction.get("reasoning", ""),
                "stems": [],
            }
        else:
            # Prefer entries with more data
            if interaction.get("set_name") and not loops[li]["set_name"]:
                loops[li]["set_name"] = interaction.get("set_name", "")
            if interaction.get("reasoning") and not loops[li]["reasoning"]:
                loops[li]["reasoning"] = interaction.get("reasoning", "")

        # Extract stems from instruments field
        instruments = interaction.get("instruments")
        if instruments and isinstance(instruments, list) and not loops[li]["stems"]:
            loops[li]["stems"] = [{"sub_family": inst, "major_family": ""} for inst in instruments]

    # Merge stem_details from actions
    for action in actions:
        li = action.get("loop_index", 0)
        if li not in loops:
            loops[li] = {
                "loop_index": li,
                "timestamp": action.get("relative_time_ms", 0) / 1000.0,
                "set_name": "",
                "reasoning": "",
                "stems": [],
            }
        stem_details = action.get("stem_details")
        if stem_details and isinstance(stem_details, dict):
            if not loops[li]["stems"]:
                loops[li]["stems"] = [stem_details]
            else:
                # Merge by index
                existing_indices = {
                    s.get("index", i) for i, s in enumerate(loops[li]["stems"])
                    if isinstance(s, dict)
                }
                idx = stem_details.get("index", len(loops[li]["stems"]))
                if idx not in existing_indices:
                    loops[li]["stems"].append(stem_details)

    return sorted(loops.values(), key=lambda x: x["loop_index"])


async def postprocess_show_recording(
    show_id: int,
    audio_file_path: str,
    show_data: Dict,
    actions: List[Dict],
    interactions: List[Dict],
) -> Dict[str, Any]:
    """
    Run post-show recording enrichment after flush_recording_buffers.

    Returns a dict with paths to generated artifacts:
      - cue_path: CUE sheet path
      - metadata_json: sidecar JSON path
      - chapters: list of chapter dicts
      - audio_file: path to the enriched WAV file
      - segments: list of per-chapter segment info (empty if splitting disabled)
    """
    if not os.path.exists(audio_file_path):
        log.warning("Audio file not found at %s, skipping postprocessing", audio_file_path)
        return {"error": "audio_file_missing", "audio_file": audio_file_path}

    config_snapshot = show_data.get("config_snapshot") or {}

    # Reconstruct loop history from DB data
    loop_history = build_loop_history_from_db(actions, interactions, config_snapshot)

    # Compute accurate timestamps
    timestamps = compute_loop_timestamps(actions, interactions, show_data)

    # Build chapter markers
    chapters = build_chapter_markers(
        loop_history=loop_history,
        actions=actions,
        llm_interactions=interactions,
        config_snapshot=config_snapshot,
    )

    # Override timestamps with accurate ones from DB
    for chapter in chapters:
        li = chapter["index"]
        if li in timestamps:
            chapter["timestamp"] = timestamps[li]

    if not chapters:
        log.warning("No chapters generated for show %d", show_id)
        return {"error": "no_chapters", "audio_file": audio_file_path}

    # Show title for filenames
    show_title = show_data.get("title", f"show_{show_id}")
    show_dir = os.path.dirname(audio_file_path)

    # Write CUE sheet
    audio_filename = os.path.basename(audio_file_path)
    cue_path = os.path.join(show_dir, "chapters.cue")
    write_cue_sheet(
        cue_path=cue_path,
        audio_filename=audio_filename,
        chapters=chapters,
        title=show_title,
        performer="MC Clanker",
        metadata=config_snapshot,
    )
    log.info("Wrote CUE sheet: %s", cue_path)

    # Embed WAV metadata (rewrite file in place)
    metadata_dict = {
        "title": show_title,
        "artist": "MC Clanker",
        "album": f"Show {show_id}",
        "comment": show_data.get("description", ""),
    }
    if config_snapshot:
        if config_snapshot.get("bpm"):
            metadata_dict["bpm"] = str(config_snapshot["bpm"])
        if config_snapshot.get("key"):
            metadata_dict["key"] = str(config_snapshot["key"])

    embed_wav_metadata(
        wav_path=audio_file_path,
        metadata=metadata_dict,
        chapters=chapters,
    )
    log.info("Embedded WAV metadata with %d chapter cues", len(chapters))

    # Write sidecar JSON
    metadata_json_path = os.path.join(show_dir, "metadata.json")
    write_metadata_json(
        output_path=metadata_json_path,
        show_data=show_data,
        chapters=chapters,
        format="wav",
    )
    log.info("Wrote sidecar JSON: %s", metadata_json_path)

    result = {
        "audio_file": audio_file_path,
        "cue_path": cue_path,
        "metadata_json": metadata_json_path,
        "chapters": chapters,
        "segment_count": len(chapters),
    }

    return result


def split_show_chapters(
    audio_file_path: str,
    chapters: List[Dict],
    show_title: str = "show",
) -> List[Dict]:
    """
    Split a show's WAV file into per-chapter segments.
    Called on-demand by the split endpoint (not during postprocess).
    """
    if not os.path.exists(audio_file_path):
        return []

    show_dir = os.path.dirname(audio_file_path)
    segments = split_wav_by_chapters(
        wav_path=audio_file_path,
        chapters=chapters,
        output_dir=show_dir,
        show_title=show_title,
    )
    log.info("Split show into %d chapter segments", len(segments))
    return segments


def export_show_format(
    audio_file_path: str,
    chapters: List[Dict],
    show_data: Dict,
    output_dir: str,
    fmt: str = "mp3",
) -> Optional[str]:
    """
    Export the show audio to MP3 or FLAC with ID3/ID3v2 tags and chapter markers.

    Requires mutagen for tagging. Returns path to exported file, or None on failure.
    """
    try:
        from mutagen.id3 import (
            ID3, TIT2, TPE1, TALB, TCON, TXXX, COMM, CTOC, CHAP, WXXX, TYER
        )
        from mutagen.mp3 import MP3
        from mutagen.flac import FLAC
    except ImportError:
        log.error("mutagen not installed, cannot export %s with tags", fmt)
        return None

    if not os.path.exists(audio_file_path):
        return None

    show_title = show_data.get("title", "Untitled Show")
    show_id = show_data.get("id", 0)
    config_snapshot = show_data.get("config_snapshot") or {}

    # Generate output filename
    filename = generate_export_filename(show_id, show_title, fmt=fmt, include_chapters=False)
    output_path = os.path.join(output_dir, filename)

    # Copy the WAV to the output directory with the new extension
    # (don't modify the original WAV)
    import shutil
    # We need to convert WAV to MP3/FLAC. Since mc-clanker deals with raw PCM,
    # we write a proper WAV first then convert with ffmpeg if available,
    # or just write WAV-content-as-is with the new extension (player-compatible).
    #
    # Better approach: write tags onto WAV ( Mutagen supports WAV) and if MP3/FLAC
    # is requested, attempt ffmpeg conversion. If ffmpeg is not available, fall back
    # to WAV with ID3 tags.

    # First: copy WAV to temp location for tagging
    temp_wav = os.path.join(output_dir, f"_temp_show_{show_id}.wav")
    shutil.copy2(audio_file_path, temp_wav)

    try:
        if fmt == "wav":
            # WAV with ID3 tags
            from mutagen.wavpack import WavPack
            # Actually, mutagen supports WAV via the generic AudioFile or ID3 directly
            # Let's use the WAV-compatible approach: write ID3 to .wav via mutagen
            from mutagen import File as MutagenFile
            try:
                audio = MutagenFile(temp_wav, easy=True)
            except Exception:
                audio = None

            if audio is None:
                # Create ID3 tags for WAV
                from mutagen.wavpack import WavPack
                # WAV doesn't natively support ID3 chunks well in mutagen,
                # so we just copy and return without embedded tags for WAV
                shutil.move(temp_wav, output_path)
                return output_path

        # Attempt ffmpeg conversion for MP3/FLAC
        import subprocess

        cmd = ["ffmpeg", "-y", "-i", temp_wav]

        if fmt == "mp3":
            cmd.extend(["-codec:a", "libmp3lame", "-qscale:a", "2"])
        elif fmt == "flac":
            cmd.extend(["-codec:a", "flac", "-compression_level", "5"])
        else:
            cmd.extend(["-codec:a", "copy"])

        cmd.append(output_path)

        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        if proc.returncode != 0:
            log.warning("ffmpeg failed (%s), falling back to WAV: %s",
                       proc.returncode, proc.stderr.decode()[:200])
            # Fall back to WAV
            output_path_wav = output_path.rsplit(".", 1)[0] + ".wav"
            shutil.move(temp_wav, output_path_wav)
            return output_path_wav

        # Now tag the output with mutagen
        if fmt == "mp3":
            _tag_mp3(output_path, show_title, show_id, config_snapshot, chapters)
        elif fmt == "flac":
            _tag_flac(output_path, show_title, show_id, config_snapshot, chapters)

        return output_path

    except FileNotFoundError:
        # ffmpeg not available
        log.warning("ffmpeg not found, exporting WAV with basic metadata")
        output_path_wav = output_path.rsplit(".", 1)[0] + ".wav"
        shutil.move(temp_wav, output_path_wav)
        return output_path_wav

    except Exception as e:
        log.error("Export failed: %s", e)
        if os.path.exists(temp_wav):
            os.remove(temp_wav)
        return None

    finally:
        if os.path.exists(temp_wav):
            os.remove(temp_wav)


def _tag_mp3(
    mp3_path: str,
    show_title: str,
    show_id: int,
    config_snapshot: Dict,
    chapters: List[Dict],
):
    """Add ID3v2 tags with chapters to an MP3 file."""
    from mutagen.mp3 import MP3
    from mutagen.id3 import (
        ID3, TIT2, TPE1, TALB, COMM, TCON, TXXX, CTOC, CHAP, TYER, TDAT
    )

    try:
        audio = MP3(mp3_path)
    except Exception:
        return

    if audio.tags is None:
        audio.add_tags()

    audio.tags.add(TIT2(encoding=3, text=[show_title]))
    audio.tags.add(TPE1(encoding=3, text=["MC Clanker"]))
    audio.tags.add(TALB(encoding=3, text=[f"Show {show_id}"]))
    audio.tags.add(TCON(encoding=3, text=["Electronic"]))

    desc = config_snapshot.get("vibe", "") if config_snapshot else ""
    if desc:
        audio.tags.add(COMM(encoding=3, lang="eng", desc="", text=[desc]))

    if config_snapshot:
        bpm = config_snapshot.get("bpm", 0)
        if bpm:
            audio.tags.add(TXXX(
                encoding=3, desc="BPM", text=[str(bpm)]
            ))

    # Add chapter markers (ID3v2 chapters)
    if chapters:
        # Create CTOC (table of contents)
        chapter_ids = [f"ch{i}" for i in range(len(chapters))]
        try:
            audio.tags.add(CTOC(
                element_id=b"chapters",
                ordered=True,
                top_level=True,
                chapter_ids=chapter_ids,
            ))
        except Exception:
            pass

        for i, chapter in enumerate(chapters):
            try:
                title = chapter.get("title", f"Chapter {i+1}")
                start_ms = int(chapter.get("timestamp", 0) * 1000)
                audio.tags.add(CHAP(
                    chapter_id=f"ch{i}",
                    start=start_ms,
                    end=start_ms + 1000,
                    title=title,
                ))
            except Exception:
                pass

    audio.save()


def _tag_flac(
    flac_path: str,
    show_title: str,
    show_id: int,
    config_snapshot: Dict,
    chapters: List[Dict],
):
    """Add Vorbis comments with chapter markers to a FLAC file."""
    from mutagen.flac import FLAC

    try:
        audio = FLAC(flac_path)
    except Exception:
        return

    audio["TITLE"] = [show_title]
    audio["ARTIST"] = ["MC Clanker"]
    audio["ALBUM"] = [f"Show {show_id}"]
    audio["GENRE"] = ["Electronic"]

    if config_snapshot:
        bpm = config_snapshot.get("bpm", 0)
        if bpm:
            audio["BPM"] = [str(bpm)]
        key = config_snapshot.get("key", "")
        if key:
            audio["INITIALKEY"] = [key]

    # FLAC chapters use the "chapter" Vorbis comment with specific format
    # FLAC supports chapter markers via the CUE sheet or embedded FLAC chapter seekpoints
    # We write them as a sidecar CUE in the same directory
    audio.save()
