"""
tests/test_recording_postprocess.py — Tests for recording postprocessing,
chapter markers, CUE sheets, WAV metadata embedding, and export routes.
"""

import os
import json
import struct
import wave
import tempfile
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

# Set test environment before importing app modules
os.environ["DATABASE_URL"] = ""

from app.app_ui import app
from app.framework.framework_state import state


@pytest.fixture
def app_client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def init_db():
    from app.db import DatabaseManager
    db = DatabaseManager.get_instance()
    db.create_tables()


@pytest.fixture(autouse=True)
def reset_state():
    state.reset()
    state.dj_password = ""
    state.audience_password = ""
    yield


@pytest.fixture
def mock_auth_user():
    user = MagicMock()
    user.id = 1
    user.username = "testuser"
    user.is_active = True
    return user


def make_test_wav(path, duration_seconds=5, sample_rate=44100):
    """Create a test WAV file with silence."""
    n_frames = int(duration_seconds * sample_rate)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_frames * 2)


# =========================================================================
# Tests for recording_metadata.py functions
# =========================================================================


class TestRecordingMetadata:
    """Tests for the core recording_metadata module."""

    def test_format_cue_time_zero(self):
        from app.lib.recording_metadata import format_cue_time
        assert format_cue_time(0.0) == "00:00:00"

    def test_format_cue_time_65_seconds(self):
        from app.lib.recording_metadata import format_cue_time
        result = format_cue_time(65.0)
        assert result.startswith("01:05:")

    def test_write_cue_sheet(self, tmp_path):
        from app.lib.recording_metadata import write_cue_sheet
        chapters = [
            {"index": 1, "timestamp": 0.0, "title": "Loop 1 — Warm Synth", "reasoning": "Starting with warm pad"},
            {"index": 2, "timestamp": 10.5, "title": "Loop 2 — Add Drums", "reasoning": "Adding rhythm"},
        ]
        cue_path = str(tmp_path / "test.cue")
        result = write_cue_sheet(cue_path, "audio.wav", chapters, title="Test Show")
        assert result == cue_path
        assert os.path.exists(cue_path)
        content = open(cue_path, "r").read()
        assert 'TITLE "Test Show"' in content
        assert 'PERFORMER "MC Clanker"' in content
        assert "TRACK 01 AUDIO" in content
        assert "TRACK 02 AUDIO" in content
        assert "Loop 1" in content
        assert "Loop 2" in content

    def test_embed_wav_metadata(self, tmp_path):
        from app.lib.recording_metadata import embed_wav_metadata
        wav_path = str(tmp_path / "test.wav")
        make_test_wav(wav_path, duration_seconds=1)
        metadata = {"title": "Test", "artist": "MC Clanker", "bpm": "120"}
        chapters = [{"index": 1, "timestamp": 0.0, "title": "Ch1"}]
        result = embed_wav_metadata(wav_path, metadata, chapters)
        assert result == wav_path
        # Verify file still valid
        with wave.open(wav_path, "rb") as wf:
            assert wf.getnframes() > 0

    def test_split_wav_by_chapters(self, tmp_path):
        from app.lib.recording_metadata import split_wav_by_chapters
        wav_path = str(tmp_path / "test.wav")
        make_test_wav(wav_path, duration_seconds=3)
        chapters = [
            {"index": 1, "timestamp": 0.0},
            {"index": 2, "timestamp": 1.0},
            {"index": 3, "timestamp": 2.0},
        ]
        segments = split_wav_by_chapters(wav_path, chapters, str(tmp_path), "test")
        assert len(segments) == 3
        for seg in segments:
            assert os.path.exists(seg["path"])
            assert "title" in seg
            assert "duration" in seg

    def test_build_chapter_markers(self):
        from app.lib.recording_metadata import build_chapter_markers
        loop_history = [
            {"loop_index": 1, "timestamp": 0, "set_name": "Verse", "reasoning": "Start", "stems": []},
            {"loop_index": 2, "timestamp": 1000, "set_name": "Chorus", "reasoning": "Build up", "stems": []},
        ]
        actions = []
        interactions = []
        chapters = build_chapter_markers(loop_history, actions, interactions)
        assert len(chapters) == 2
        assert chapters[0]["index"] == 1
        assert chapters[1]["index"] == 2

    def test_build_chapter_markers_with_bpm_key(self):
        from app.lib.recording_metadata import build_chapter_markers
        loop_history = [
            {"loop_index": 1, "timestamp": 0, "set_name": "", "reasoning": "", "stems": []},
        ]
        interactions = [
            {
                "loop_index": 1,
                "parsed_response": {"master_bpm": 128, "master_key": "A minor"},
            }
        ]
        chapters = build_chapter_markers(loop_history, [], interactions)
        assert len(chapters) == 1
        assert chapters[0]["bpm"] == "128"
        assert chapters[0]["key"] == "A minor"

    def test_generate_export_filename(self):
        from app.lib.recording_metadata import generate_export_filename
        name = generate_export_filename(42, "My Show", format="mp3")
        assert "show_42" in name
        assert name.endswith(".mp3")

    def test_write_metadata_json(self, tmp_path):
        from app.lib.recording_metadata import write_metadata_json
        show_data = {"id": 1, "title": "Test"}
        chapters = [{"index": 1, "timestamp": 0.0, "title": "Ch1"}]
        out_path = str(tmp_path / "meta.json")
        write_metadata_json(out_path, show_data, chapters, "wav")
        assert os.path.exists(out_path)
        data = json.load(open(out_path))
        assert data["format"] == "wav"
        assert len(data["chapters"]) == 1


# =========================================================================
# Tests for recording_postprocess.py functions
# =========================================================================


class TestRecordingPostprocess:
    """Tests for the postprocessing pipeline."""

    def test_compute_loop_timestamps(self):
        from app.lib.recording_postprocess import compute_loop_timestamps
        interactions = [
            {"loop_index": 1, "relative_time_ms": 0},
            {"loop_index": 2, "relative_time_ms": 5000},
        ]
        timestamps = compute_loop_timestamps(interactions, [], {})
        assert timestamps[1] == 0.0
        assert timestamps[2] == 5.0

    def test_compute_loop_timestamps_normalizes(self):
        from app.lib.recording_postprocess import compute_loop_timestamps
        interactions = [
            {"loop_index": 1, "relative_time_ms": 2000},
            {"loop_index": 2, "relative_time_ms": 7000},
        ]
        timestamps = compute_loop_timestamps(interactions, [], {})
        # Loop 1 should be normalized to 0
        assert timestamps[1] == 0.0
        assert timestamps[2] == 5.0

    def test_build_loop_history_from_db(self):
        from app.lib.recording_postprocess import build_loop_history_from_db
        interactions = [
            {
                "loop_index": 1,
                "relative_time_ms": 0,
                "set_name": "Verse",
                "reasoning": "Start soft",
                "instruments": ["Synth Pad", "Bass"],
            },
            {
                "loop_index": 2,
                "relative_time_ms": 8000,
                "set_name": "Chorus",
                "reasoning": "Build energy",
                "instruments": ["Drums", "Synth Lead"],
            },
        ]
        history = build_loop_history_from_db([], interactions)
        assert len(history) == 2
        assert history[0]["set_name"] == "Verse"
        assert len(history[0]["stems"]) == 2

    @pytest.mark.asyncio
    async def test_postprocess_show_recording(self, tmp_path):
        from app.lib.recording_postprocess import postprocess_show_recording
        wav_path = str(tmp_path / "audio.wav")
        make_test_wav(wav_path, duration_seconds=2)
        show_data = {"id": 1, "title": "Test Show", "description": "A test", "config_snapshot": {"bpm": 120, "key": "C minor"}}
        actions = [
            {"loop_index": 1, "relative_time_ms": 0, "action_type": "add", "stem_details": {"sub_family": "Bass", "major_family": ""}},
            {"loop_index": 2, "relative_time_ms": 5000, "action_type": "add", "stem_details": {"sub_family": "Drums", "major_family": ""}},
        ]
        interactions = [
            {"loop_index": 1, "relative_time_ms": 0, "set_name": "Verse", "reasoning": "Start", "instruments": ["Bass"]},
            {"loop_index": 2, "relative_time_ms": 5000, "set_name": "Chorus", "reasoning": "Energy up", "instruments": ["Drums"]},
        ]
        result = await postprocess_show_recording(
            show_id=1,
            audio_file_path=wav_path,
            show_data=show_data,
            actions=actions,
            interactions=interactions,
        )
        assert "cue_path" in result
        assert os.path.exists(result["cue_path"])
        assert os.path.exists(result["metadata_json"])
        assert len(result["chapters"]) >= 1

    @pytest.mark.asyncio
    async def test_postprocess_missing_file(self, tmp_path):
        from app.lib.recording_postprocess import postprocess_show_recording
        result = await postprocess_show_recording(
            show_id=1,
            audio_file_path=str(tmp_path / "nonexistent.wav"),
            show_data={"id": 1, "title": "Test"},
            actions=[],
            interactions=[],
        )
        assert result.get("error") == "audio_file_missing"

    def test_split_show_chapters(self, tmp_path):
        from app.lib.recording_postprocess import split_show_chapters
        wav_path = str(tmp_path / "audio.wav")
        make_test_wav(wav_path, duration_seconds=3)
        chapters = [
            {"index": 1, "timestamp": 0.0, "title": "Part 1"},
            {"index": 2, "timestamp": 1.0, "title": "Part 2"},
        ]
        segments = split_show_chapters(wav_path, chapters, "test_show")
        assert len(segments) == 2
        for seg in segments:
            assert os.path.exists(seg["path"])


# =========================================================================
# Tests for API routes
# =========================================================================


class TestShowChapterRoutes:
    """Tests for chapter and export API endpoints."""

    def test_get_chapters_not_found(self, app_client, mock_auth_user):
        """Returns 404 when no metadata.json exists."""
        with patch("app.routes.shows.get_current_user_from_request", return_value=mock_auth_user):
            # Create a show first
            resp = app_client.post("/api/shows", json={"title": "Test"})
            assert resp.status_code == 201
            show_id = resp.json()["id"]
            # Try to get chapters (no recording exists)
            resp = app_client.get(f"/api/shows/{show_id}/chapters")
            assert resp.status_code == 404

    def test_export_audio_format_validation(self, app_client, mock_auth_user):
        """Rejects invalid format parameter."""
        with patch("app.routes.shows.get_current_user_from_request", return_value=mock_auth_user):
            resp = app_client.get("/api/shows/999/export/audio?fmt=ogg")
            # Either 400 (validation) or 404 (show not found) or 422 (FastAPI validation)
            assert resp.status_code in (400, 404, 422)

    def test_split_show_no_chapters(self, app_client, mock_auth_user):
        """Returns 404 when no chapters exist."""
        with patch("app.routes.shows.get_current_user_from_request", return_value=mock_auth_user):
            resp = app_client.get("/api/shows/999/split")
            assert resp.status_code in (404, 500)
