import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch
import os

# Set test environment before importing modules
os.environ["DATABASE_URL"] = ""  # Force SQLite for tests


class TestShowModel:
    """Tests for Show model."""

    def test_show_to_dict_without_password(self):
        from app.models.show import Show

        show = Show(
            id=1,
            user_id=10,
            title="Test Show",
            description="A test show",
            status="draft",
            audio_file_path=None,
            audience_password_hash="$2b$12$hashed",
            config_snapshot={"bpm": 128},
            created_at=datetime(2024, 1, 1, 12, 0, 0),
        )

        result = show.to_dict(include_audience_password=False)

        assert result["id"] == 1
        assert result["user_id"] == 10
        assert result["title"] == "Test Show"
        assert result["status"] == "draft"
        assert "has_audience_password" not in result  # Not included when False

    def test_show_to_dict_with_password(self):
        from app.models.show import Show

        show = Show(
            id=1,
            user_id=10,
            title="Test Show",
            description="A test show",
            status="live",
            audio_file_path="/path/to/audio.wav",
            audience_password_hash="$2b$12$hashed",
            config_snapshot={"bpm": 128},
            created_at=datetime(2024, 1, 1, 12, 0, 0),
        )

        result = show.to_dict(include_audience_password=True)

        assert result["has_audience_password"] is True
        assert "audience_password_hash" not in result  # Never exposed

    def test_show_status_values(self):
        from app.models.show import Show

        # Valid statuses should be stored as-is
        show = Show(
            id=1,
            user_id=10,
            title="Test",
            status="draft",
        )
        assert show.status == "draft"

        show.status = "live"
        assert show.status == "live"

        show.status = "ended"
        assert show.status == "ended"

        show.status = "archived"
        assert show.status == "archived"


class TestShowActionModel:
    """Tests for ShowAction model."""

    def test_show_action_to_dict(self):
        from app.models.show_action import ShowAction

        action = ShowAction(
            id=1,
            show_id=5,
            loop_index=3,
            timestamp=datetime(2024, 1, 1, 12, 0, 0),
            relative_time_ms=5000,
            action_type="add",
            stem_index=None,
            stem_details={"model_id": "test", "major_family": "Drums"},
            action_description="Added Drums",
        )

        result = action.to_dict()

        assert result["id"] == 1
        assert result["show_id"] == 5
        assert result["loop_index"] == 3
        assert result["action_type"] == "add"
        assert result["stem_details"]["model_id"] == "test"


class TestLLMInteractionModel:
    """Tests for LLMInteraction model."""

    def test_llm_interaction_to_dict(self):
        from app.models.llm_interaction import LLMInteraction

        interaction = LLMInteraction(
            id=1,
            show_id=5,
            loop_index=2,
            timestamp=datetime(2024, 1, 1, 12, 0, 0),
            relative_time_ms=3000,
            prompt_messages=[{"role": "system", "content": "You are a DJ"}],
            parsed_response={"master_bpm": 128, "actions": []},
            reasoning="Keeping the groove",
            error=None,
            was_fallback=False,
        )

        result = interaction.to_dict()

        assert result["id"] == 1
        assert result["show_id"] == 5
        assert result["loop_index"] == 2
        assert result["reasoning"] == "Keeping the groove"
        assert result["was_fallback"] is False

    def test_llm_interaction_to_llm_dump_dict(self):
        from app.models.llm_interaction import LLMInteraction

        interaction = LLMInteraction(
            id=1,
            show_id=5,
            loop_index=2,
            relative_time_ms=3000,
            prompt_messages=[{"role": "user", "content": "Hello"}],
            parsed_response={"answer": 42},
            reasoning="Test",
            error=None,
            was_fallback=False,
        )

        result = interaction.to_llm_dump_dict()

        # Should only contain messages and response
        assert "messages" in result
        assert "response" in result
        assert "reasoning" not in result  # Not in dump format
        assert "error" not in result


class TestShowRoutes:
    """Tests for Show API routes."""

    @pytest.fixture
    def app_client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api_routes import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    @pytest.fixture(autouse=True)
    def reset_state(self):
        from app.framework.framework_state import state
        state.reset()
        yield

    @pytest.fixture
    def mock_auth_user(self):
        """Mock authenticated user."""
        from unittest.mock import MagicMock
        user = MagicMock()
        user.id = 1
        user.username = "testuser"
        return user

    @pytest.fixture
    def mock_db_with_show(self, mock_auth_user):
        """Mock database with a test show."""
        from unittest.mock import MagicMock, patch
        from datetime import datetime
        from app.models.show import Show

        mock_show = MagicMock(spec=Show)
        mock_show.id = 1
        mock_show.user_id = mock_auth_user.id
        mock_show.title = "Test Show"
        mock_show.description = "Test description"
        mock_show.status = "draft"
        mock_show.audio_file_path = None
        mock_show.audience_password_hash = "$2b$12$hash"
        mock_show.config_snapshot = {"bpm": 128}
        mock_show.created_at = datetime(2024, 1, 1, 12, 0, 0)
        mock_show.started_at = None
        mock_show.ended_at = None
        mock_show.duration_seconds = None
        mock_show.to_dict.return_value = {
            "id": 1,
            "user_id": mock_auth_user.id,
            "title": "Test Show",
            "description": "Test description",
            "status": "draft",
            "audio_file_path": None,
            "has_audience_password": True,
            "config_snapshot": {"bpm": 128},
            "created_at": datetime(2024, 1, 1, 12, 0, 0),
            "started_at": None,
            "ended_at": None,
            "duration_seconds": None,
        }

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = mock_show
        mock_session.query.return_value.filter.return_value.count.return_value = 1
        mock_session.query.return_value.filter.return_value.order_by.return_value.limit.return_value.offset.return_value.all.return_value = [mock_show]

        mock_db = MagicMock()
        mock_db.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.session.return_value.__exit__ = MagicMock(return_value=False)

        return mock_db, mock_session, mock_show

    def test_list_shows_unauthenticated(self, app_client):
        """Test listing shows without authentication returns 401."""
        response = app_client.get("/api/shows")
        assert response.status_code == 401

    def test_get_show_unauthenticated(self, app_client):
        """Test getting a show without authentication returns 401."""
        response = app_client.get("/api/shows/1")
        assert response.status_code == 401

    def test_create_show_unauthenticated(self, app_client):
        """Test creating a show without authentication returns 401."""
        response = app_client.post("/api/shows", json={"title": "New Show"})
        assert response.status_code == 401

    def test_update_show_unauthenticated(self, app_client):
        """Test updating a show without authentication returns 401."""
        response = app_client.patch("/api/shows/1", json={"title": "Updated"})
        assert response.status_code == 401

    def test_delete_show_unauthenticated(self, app_client):
        """Test deleting a show without authentication returns 401."""
        response = app_client.delete("/api/shows/1")
        assert response.status_code == 401

    def test_archive_show_unauthenticated(self, app_client):
        """Test archiving a show without authentication returns 401."""
        response = app_client.post("/api/shows/1/archive")
        assert response.status_code == 401

    def test_start_show_unauthenticated(self, app_client):
        """Test starting a show without authentication returns 401."""
        response = app_client.post("/api/shows/1/start")
        assert response.status_code == 401

    def test_stop_show_unauthenticated(self, app_client):
        """Test stopping a show without authentication returns 401."""
        response = app_client.post("/api/shows/1/stop")
        assert response.status_code == 401

    def test_get_show_actions_unauthenticated(self, app_client):
        """Test getting show actions without authentication returns 401."""
        response = app_client.get("/api/shows/1/actions")
        assert response.status_code == 401

    def test_get_show_llm_interactions_unauthenticated(self, app_client):
        """Test getting show LLM interactions without authentication returns 401."""
        response = app_client.get("/api/shows/1/llm-interactions")
        assert response.status_code == 401

    def test_get_show_audio_unauthenticated(self, app_client):
        """Test getting show audio without authentication returns 401."""
        response = app_client.get("/api/shows/1/audio")
        assert response.status_code == 401

    def test_export_llm_dump_unauthenticated(self, app_client):
        """Test exporting LLM dump without authentication returns 401."""
        response = app_client.get("/api/shows/1/export/llm-dump")
        assert response.status_code == 401

    def test_export_full_show_unauthenticated(self, app_client):
        """Test exporting full show without authentication returns 401."""
        response = app_client.get("/api/shows/1/export/full")
        assert response.status_code == 401

    def test_start_playback_unauthenticated(self, app_client):
        """Test starting playback without authentication returns 401."""
        response = app_client.post("/api/shows/1/playback/start")
        assert response.status_code == 401

    def test_stop_playback_unauthenticated(self, app_client):
        """Test stopping playback without authentication still works (no auth required)."""
        # Note: The stop_playback endpoint doesn't require auth currently
        response = app_client.post("/api/shows/1/playback/stop")
        assert response.status_code == 200

    def test_get_remix_interface_unauthenticated(self, app_client):
        """Test getting remix interface without authentication returns 401."""
        response = app_client.get("/api/shows/1/remix")
        assert response.status_code == 401

    def test_get_audience_token_unauthenticated(self, app_client):
        """Test getting audience token without authentication returns 401."""
        response = app_client.get("/api/shows/1/audience-token")
        assert response.status_code == 401

    def test_regenerate_audience_password_unauthenticated(self, app_client):
        """Test regenerating audience password without authentication returns 401."""
        response = app_client.post("/api/shows/1/regenerate-audience-password")
        assert response.status_code == 401

    def test_list_shows_authenticated_but_empty(self, app_client, mock_auth_user):
        """Test listing shows when user has no shows."""
        from unittest.mock import patch, MagicMock

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.order_by.return_value.limit.return_value.offset.return_value.all.return_value = []
        mock_session.query.return_value.filter.return_value.count.return_value = 0

        mock_db_instance = MagicMock()
        mock_db_instance.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db_instance.session.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.api_routes.get_current_user_from_request", return_value=mock_auth_user):
            with patch("app.api_routes.DatabaseManager") as mock_db_class:
                mock_db_class.get_instance.return_value = mock_db_instance

                response = app_client.get("/api/shows")

        assert response.status_code == 200
        data = response.json()
        assert data["shows"] == []
        assert data["total"] == 0

    def test_get_show_authenticated(self, app_client, mock_auth_user):
        """Test getting a show by ID with authentication."""
        from unittest.mock import patch, MagicMock
        from datetime import datetime

        mock_show = MagicMock()
        mock_show.id = 1
        mock_show.user_id = mock_auth_user.id
        mock_show.title = "Test Show"
        mock_show.description = "Test description"
        mock_show.status = "draft"
        mock_show.audio_file_path = None
        mock_show.audience_password_hash = "$2b$12$hash"
        mock_show.config_snapshot = {"bpm": 128}
        mock_show.created_at = datetime(2024, 1, 1, 12, 0, 0)
        mock_show.started_at = None
        mock_show.ended_at = None
        mock_show.duration_seconds = None
        mock_show.to_dict.return_value = {
            "id": 1,
            "user_id": mock_auth_user.id,
            "title": "Test Show",
            "description": "Test description",
            "status": "draft",
            "has_audience_password": True,
        }

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = mock_show

        mock_db_instance = MagicMock()
        mock_db_instance.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db_instance.session.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.api_routes.get_current_user_from_request", return_value=mock_auth_user):
            with patch("app.api_routes.DatabaseManager") as mock_db_class:
                mock_db_class.get_instance.return_value = mock_db_instance

                response = app_client.get("/api/shows/1")

        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Test Show"

    def test_get_show_not_owner(self, app_client, mock_auth_user):
        """Test getting a show that belongs to another user returns 404."""
        from unittest.mock import patch, MagicMock

        mock_show = MagicMock()
        mock_show.id = 1
        mock_show.user_id = 999  # Different user

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = mock_show

        mock_db_instance = MagicMock()
        mock_db_instance.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db_instance.session.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.api_routes.get_current_user_from_request", return_value=mock_auth_user):
            with patch("app.api_routes.DatabaseManager") as mock_db_class:
                mock_db_class.get_instance.return_value = mock_db_instance

                response = app_client.get("/api/shows/1")

        # Should return 404 because user doesn't own the show
        assert response.status_code == 404


class TestPlaybackModule:
    """Tests for playback.py."""

    def test_show_playback_init(self):
        from app.playback import ShowPlayback

        playback = ShowPlayback(
            show_id=1,
            audio_file_path="/path/to/audio.wav"
        )

        assert playback.show_id == 1
        assert playback.audio_file_path == "/path/to/audio.wav"
        assert playback.is_playing is False

    def test_show_playback_get_progress(self):
        from app.playback import ShowPlayback
        from app.framework.framework_state import state

        playback = ShowPlayback(show_id=1, audio_file_path="/fake/path.wav")
        result = playback.get_progress()

        assert result["show_id"] == 1
        assert "is_playing" in result
        assert "currently_playing_show_id" in result

    def test_show_playback_start_already_playing(self):
        """Test starting playback when already playing returns early."""
        from app.playback import ShowPlayback

        playback = ShowPlayback(show_id=1, audio_file_path="/fake/path.wav")
        playback.is_playing = True

        result = playback.start()

        assert result["status"] == "already_playing"
        assert result["show_id"] == 1

    def test_show_playback_start_file_not_found(self):
        """Test starting playback with non-existent file returns error."""
        from app.playback import ShowPlayback

        playback = ShowPlayback(show_id=1, audio_file_path="/nonexistent/path.wav")

        result = playback.start()

        assert result["status"] == "error"
        assert "not found" in result["message"]

    def test_show_playback_stop(self):
        """Test stopping playback."""
        from app.playback import ShowPlayback

        playback = ShowPlayback(show_id=1, audio_file_path="/fake/path.wav")
        playback.is_playing = True

        # Mock the state.lock
        from unittest.mock import patch, MagicMock
        with patch("app.playback.state") as mock_state:
            mock_state.lock = MagicMock()
            mock_state.lock.__enter__ = MagicMock(return_value=None)
            mock_state.lock.__exit__ = MagicMock(return_value=False)
            mock_state.is_playback_active = False
            mock_state.currently_playing_show_id = None

            result = playback.stop()

        assert result["status"] == "stopped"
        assert result["show_id"] == 1

    def test_remix_interface_init(self):
        from app.playback import ReMixInterface

        remix = ReMixInterface(show_id=1)

        assert remix.show_id == 1

    def test_remix_interface_get_context(self):
        from app.playback import ReMixInterface

        remix = ReMixInterface(show_id=1)
        result = remix.get_remix_context()

        assert result["show_id"] == 1
        assert "message" in result

    def test_remix_interface_regenerate_stem(self):
        """Test regenerate_stem returns not_implemented."""
        from app.playback import ReMixInterface

        remix = ReMixInterface(show_id=1)
        result = remix.regenerate_stem(loop_index=0, stem_index=1, params={})

        assert result["status"] == "not_implemented"
        assert "message" in result


class TestShowPlaybackWithMockedAudio:
    """Test ShowPlayback with mocked audio file operations."""

    def test_show_playback_start_sets_state(self):
        """Test that start() sets is_playing and state correctly."""
        import tempfile
        import os
        from app.playback import ShowPlayback

        # Create a valid WAV file for testing
        import wave
        temp_path = tempfile.mktemp(suffix='.wav')
        with wave.open(temp_path, 'wb') as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(44100)
            wav.writeframes(b'\x00' * 1000)  # Minimal audio data

        try:
            playback = ShowPlayback(show_id=1, audio_file_path=temp_path)

            # Mock state.lock
            from unittest.mock import patch, MagicMock
            with patch("app.playback.state") as mock_state:
                mock_state.lock = MagicMock()
                mock_state.lock.__enter__ = MagicMock(return_value=None)
                mock_state.lock.__exit__ = MagicMock(return_value=False)
                mock_state.currently_playing_show_id = None
                mock_state.is_playback_active = False

                result = playback.start()

            assert result["status"] == "started"
            assert playback.is_playing is True
        finally:
            # Wait a bit for file to be released
            import time
            time.sleep(0.1)
            try:
                os.unlink(temp_path)
            except:
                pass

    def test_show_playback_stop_clears_state(self):
        """Test that stop() clears is_playing and state."""
        from app.playback import ShowPlayback
        from unittest.mock import patch, MagicMock

        playback = ShowPlayback(show_id=1, audio_file_path="/fake/path.wav")
        playback.is_playing = True
        playback.playback_thread = MagicMock()

        with patch("app.playback.state") as mock_state:
            mock_state.lock = MagicMock()
            mock_state.lock.__enter__ = MagicMock(return_value=None)
            mock_state.lock.__exit__ = MagicMock(return_value=False)
            mock_state.is_playback_active = True
            mock_state.currently_playing_show_id = 1

            result = playback.stop()

        assert result["status"] == "stopped"
        assert playback.is_playing is False


class TestShowPlaybackEdgeCases:
    """Test ShowPlayback edge cases."""

    def test_playback_init_with_db_session(self):
        """Test ShowPlayback initialization with db_session."""
        from app.playback import ShowPlayback
        from unittest.mock import MagicMock

        mock_session = MagicMock()
        playback = ShowPlayback(show_id=1, audio_file_path="/path.wav", db_session=mock_session)

        assert playback.show_id == 1
        assert playback.audio_file_path == "/path.wav"
        assert playback.db_session is mock_session

    def test_playback_progress_while_not_started(self):
        """Test get_progress when playback hasn't started."""
        from app.playback import ShowPlayback
        from unittest.mock import patch, MagicMock

        playback = ShowPlayback(show_id=1, audio_file_path="/fake.wav")
        playback.is_playing = False

        with patch("app.playback.state") as mock_state:
            mock_state.lock = MagicMock()
            mock_state.lock.__enter__ = MagicMock(return_value=None)
            mock_state.lock.__exit__ = MagicMock(return_value=False)
            mock_state.currently_playing_show_id = None

            result = playback.get_progress()

        assert result["is_playing"] is False


class TestPlaybackStartStop:
    """Test playback start/stop state transitions."""

    def test_playback_start_updates_state(self):
        """Test that start() properly updates playback state."""
        from app.playback import ShowPlayback
        from unittest.mock import patch, MagicMock
        import tempfile
        import wave

        # Create a valid WAV file
        temp_path = tempfile.mktemp(suffix='.wav')
        with wave.open(temp_path, 'wb') as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(44100)
            wav.writeframes(b'\x00' * 1000)

        try:
            playback = ShowPlayback(show_id=1, audio_file_path=temp_path)

            with patch("app.playback.state") as mock_state:
                mock_state.lock = MagicMock()
                mock_state.lock.__enter__ = MagicMock(return_value=None)
                mock_state.lock.__exit__ = MagicMock(return_value=False)
                mock_state.currently_playing_show_id = None
                mock_state.is_playback_active = False

                result = playback.start()

            assert result["status"] == "started"
            assert playback.is_playing is True
        finally:
            import time
            time.sleep(0.1)
            try:
                os.unlink(temp_path)
            except:
                pass

    def test_playback_stop_resets_state(self):
        """Test that stop() properly resets playback state."""
        from app.playback import ShowPlayback
        from unittest.mock import patch, MagicMock

        playback = ShowPlayback(show_id=1, audio_file_path="/fake.wav")
        playback.is_playing = True
        playback.playback_thread = MagicMock()

        with patch("app.playback.state") as mock_state:
            mock_state.lock = MagicMock()
            mock_state.lock.__enter__ = MagicMock(return_value=None)
            mock_state.lock.__exit__ = MagicMock(return_value=False)
            mock_state.is_playback_active = True
            mock_state.currently_playing_show_id = 1

            result = playback.stop()

        assert result["status"] == "stopped"
        assert playback.is_playing is False


class TestPlaybackInternal:
    """Test ShowPlayback internal methods."""

    def test_playback_loop_with_missing_file(self):
        """Test playback loop handles missing file gracefully."""
        from app.playback import ShowPlayback
        from unittest.mock import patch, MagicMock

        playback = ShowPlayback(show_id=1, audio_file_path="/nonexistent/file.wav")

        # Mock the wave.open to raise an exception
        with patch("wave.open", side_effect=Exception("File not found")):
            with patch("app.playback.state"):
                # The _playback_loop should handle the exception and set is_playing to False
                import threading
                playback.is_playing = True
                thread = threading.Thread(target=playback._playback_loop)
                thread.daemon = True
                thread.start()
                thread.join(timeout=1)

                # is_playing should be False after the loop exits
                assert playback.is_playing is False

    def test_playback_audio_queue_creation(self):
        """Test that audio queue is initialized correctly."""
        from app.playback import ShowPlayback
        import queue

        playback = ShowPlayback(show_id=1, audio_file_path="/fake.wav")

        assert isinstance(playback.audio_queue, queue.Queue)
        assert playback.audio_queue.maxsize == 100


class TestReMixInterfaceEdgeCases:
    """Test ReMixInterface edge cases."""

    def test_remix_interface_with_db_session(self):
        """Test ReMixInterface initialization with db_session."""
        from app.playback import ReMixInterface
        from unittest.mock import MagicMock

        mock_session = MagicMock()
        remix = ReMixInterface(show_id=1, db_session=mock_session)

        assert remix.show_id == 1
        assert remix.db_session is mock_session


class TestShowModelEdgeCases:
    """Test Show model edge cases."""

    def test_show_to_dict_with_all_fields(self):
        from datetime import datetime
        from app.models.show import Show

        show = Show(
            id=1,
            user_id=10,
            title="Test Show",
            description="A detailed description",
            status="live",
            audio_file_path="/path/to/audio.wav",
            audience_password_hash="$2b$12$hashed",
            config_snapshot={"bpm": 128, "key": "C minor"},
            created_at=datetime(2024, 1, 1, 12, 0, 0),
            started_at=datetime(2024, 1, 1, 12, 0, 0),
            ended_at=None,
            duration_seconds=None
        )

        result = show.to_dict(include_audience_password=True)

        assert result["id"] == 1
        assert result["title"] == "Test Show"
        assert result["status"] == "live"
        assert result["config_snapshot"]["bpm"] == 128
        assert "has_audience_password" in result

    def test_show_to_dict_includes_timestamps(self):
        from datetime import datetime
        from app.models.show import Show

        show = Show(
            id=1,
            user_id=10,
            title="Test",
            status="ended",
            started_at=datetime(2024, 1, 1, 12, 0, 0),
            ended_at=datetime(2024, 1, 1, 13, 0, 0),
            duration_seconds=3600
        )

        result = show.to_dict(include_audience_password=False)

        assert result["duration_seconds"] == 3600


class TestShowActionModelEdgeCases:
    """Test ShowAction model edge cases."""

    def test_show_action_with_none_stem_details(self):
        from datetime import datetime
        from app.models.show_action import ShowAction

        action = ShowAction(
            id=1,
            show_id=5,
            loop_index=3,
            timestamp=datetime(2024, 1, 1, 12, 0, 0),
            relative_time_ms=5000,
            action_type="remove",
            stem_index=0,
            stem_details=None,
            action_description="Removed old synth"
        )

        result = action.to_dict()

        assert result["stem_details"] is None
        assert result["action_description"] == "Removed old synth"


class TestLLMInteractionModelEdgeCases:
    """Test LLMInteraction model edge cases."""

    def test_llm_interaction_with_error(self):
        from datetime import datetime
        from app.models.llm_interaction import LLMInteraction

        interaction = LLMInteraction(
            id=1,
            show_id=5,
            loop_index=2,
            timestamp=datetime(2024, 1, 1, 12, 0, 0),
            relative_time_ms=3000,
            prompt_messages=[{"role": "system", "content": "You are a DJ"}],
            parsed_response=None,
            reasoning="Test reasoning",
            error="GPU out of memory",
            was_fallback=True
        )

        result = interaction.to_dict()

        assert result["error"] == "GPU out of memory"
        assert result["was_fallback"] is True

    def test_llm_interaction_to_llm_dump_dict_with_error(self):
        from datetime import datetime
        from app.models.llm_interaction import LLMInteraction

        interaction = LLMInteraction(
            id=1,
            show_id=5,
            loop_index=2,
            relative_time_ms=3000,
            prompt_messages=[{"role": "user", "content": "Hello"}],
            parsed_response={"answer": 42},
            reasoning="Test",
            error="Some error",
            was_fallback=False
        )

        result = interaction.to_llm_dump_dict()

        # Error should not be in dump format
        assert "error" not in result
        assert "reasoning" not in result


class TestRequireShowOwner:
    """Tests for require_show_owner function."""

    def test_require_show_owner_unauthenticated(self):
        """Test that unauthenticated request raises 401."""
        from app.api_routes import require_show_owner
        from fastapi import HTTPException

        mock_request = MagicMock()
        mock_request.headers.get.return_value = None
        mock_db_session = MagicMock()

        with patch("app.api_routes.get_current_user_from_request", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                require_show_owner(show_id=1, request=mock_request, db_session=mock_db_session)

            assert exc_info.value.status_code == 401
            assert "Not authenticated" in exc_info.value.detail

    def test_require_show_owner_show_not_found(self):
        """Test that non-existent show raises 404."""
        from app.api_routes import require_show_owner
        from fastapi import HTTPException

        mock_user = MagicMock()
        mock_user.id = 1

        mock_request = MagicMock()
        mock_db_session = MagicMock()
        mock_db_session.query.return_value.filter.return_value.first.return_value = None

        with patch("app.api_routes.get_current_user_from_request", return_value=mock_user):
            with pytest.raises(HTTPException) as exc_info:
                require_show_owner(show_id=999, request=mock_request, db_session=mock_db_session)

            assert exc_info.value.status_code == 404
            assert "Show not found" in exc_info.value.detail

    def test_require_show_owner_not_owner(self):
        """Test that accessing another user's show raises 404."""
        from app.api_routes import require_show_owner
        from fastapi import HTTPException

        mock_user = MagicMock()
        mock_user.id = 1

        mock_show = MagicMock()
        mock_show.id = 1
        mock_show.user_id = 999  # Different user

        mock_request = MagicMock()
        mock_db_session = MagicMock()
        mock_db_session.query.return_value.filter.return_value.first.return_value = mock_show

        with patch("app.api_routes.get_current_user_from_request", return_value=mock_user):
            with pytest.raises(HTTPException) as exc_info:
                require_show_owner(show_id=1, request=mock_request, db_session=mock_db_session)

            assert exc_info.value.status_code == 404
            assert "Show not found" in exc_info.value.detail

    def test_require_show_owner_success(self):
        """Test that owner can access their show."""
        from app.api_routes import require_show_owner

        mock_user = MagicMock()
        mock_user.id = 1

        mock_show = MagicMock()
        mock_show.id = 1
        mock_show.user_id = 1

        mock_request = MagicMock()
        mock_db_session = MagicMock()
        mock_db_session.query.return_value.filter.return_value.first.return_value = mock_show

        with patch("app.api_routes.get_current_user_from_request", return_value=mock_user):
            result = require_show_owner(show_id=1, request=mock_request, db_session=mock_db_session)

            assert result is mock_show
