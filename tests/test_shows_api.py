import pytest
from unittest.mock import MagicMock, patch
import os
from fastapi.testclient import TestClient
from app.app_ui import app
from app.framework.framework_state import state

# Set test environment before importing modules
os.environ["DATABASE_URL"] = ""  # Force SQLite for tests

@pytest.fixture
def app_client():
    """Returns a TestClient with the real app."""
    return TestClient(app)

@pytest.fixture(autouse=True)
def init_db():
    """Initialize DB tables."""
    from app.db import DatabaseManager
    db = DatabaseManager.get_instance()
    db.create_tables()

@pytest.fixture(autouse=True)
def reset_state():
    """Reset global state between tests."""
    state.reset()
    # Reset auth passwords to avoid 401s by default
    state.dj_password = ""
    state.audience_password = ""
    yield

@pytest.fixture
def mock_auth_user():
    """Mock authenticated user."""
    user = MagicMock()
    user.id = 1
    user.username = "testuser"
    user.is_active = True
    return user


class TestShowRoutes:
    """Tests for Show API routes."""

    def test_list_shows_unauthenticated(self, app_client):
        """Test listing shows without authentication returns 401 if password is set."""
        # Force authentication requirement by setting a password in state
        with patch.object(state, 'dj_password', 'secret'):
            with patch.object(state, 'audience_password', 'secret'):
                response = app_client.get("/api/shows")
                # Should be 401 because we didn't provide credentials
                assert response.status_code == 401

    def test_get_show_unauthenticated(self, app_client):
        """Test getting a show without authentication returns 401 if password is set."""
        with patch.object(state, 'dj_password', 'secret'):
            response = app_client.get("/api/shows/1")
            assert response.status_code == 401

    def test_create_show_unauthenticated(self, app_client):
        """Test creating a show without authentication returns 401 if password is set."""
        with patch.object(state, 'dj_password', 'secret'):
            response = app_client.post("/api/shows", json={"title": "New Show"})
            assert response.status_code == 401

    def test_list_shows_authenticated_but_empty(self, app_client, mock_auth_user):
        """Test listing shows when user is authenticated via CompatUser or Mock."""
        # In this test, state.dj_password is empty, so AuthMiddleware allows access as CompatUser(id=0)
        # But we patch get_current_user_from_request to return our mock user (id=1)
        
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.order_by.return_value.limit.return_value.offset.return_value.all.return_value = []
        mock_session.query.return_value.filter.return_value.count.return_value = 0

        mock_db_instance = MagicMock()
        mock_db_instance.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db_instance.session.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.routes.shows.get_current_user_from_request", return_value=mock_auth_user):
            with patch("app.db.DatabaseManager.get_instance", return_value=mock_db_instance):
                response = app_client.get("/api/shows")

        assert response.status_code == 200
        data = response.json()
        assert data["shows"] == []
        assert data["total"] == 0

    def test_get_show_authenticated(self, app_client, mock_auth_user):
        """Test getting a show by ID with authentication."""
        mock_show = MagicMock()
        mock_show.id = 1
        mock_show.user_id = mock_auth_user.id
        mock_show.title = "Test Show"
        mock_show.to_dict.return_value = {
            "id": 1,
            "user_id": mock_auth_user.id,
            "title": "Test Show",
            "status": "draft",
        }

        with patch("app.routes.shows.get_current_user_from_request", return_value=mock_auth_user):
            with patch("app.routes.shows.require_show_owner", return_value=mock_show):
                response = app_client.get("/api/shows/1")

        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Test Show"

    def test_get_show_not_owner(self, app_client, mock_auth_user):
        """Test getting a show that belongs to another user returns 404."""
        from fastapi import HTTPException
        
        with patch("app.routes.shows.get_current_user_from_request", return_value=mock_auth_user):
            with patch("app.routes.shows.require_show_owner", side_effect=HTTPException(status_code=404, detail="Show not found")):
                response = app_client.get("/api/shows/1")

        assert response.status_code == 404


class TestPlaybackIntegration:
    """Tests for playback functionality via API."""

    def test_start_playback_unauthenticated(self, app_client):
        """Test starting playback without authentication returns 401 if password is set."""
        with patch.object(state, 'dj_password', 'secret'):
            response = app_client.post("/api/shows/1/playback/start")
            assert response.status_code == 401

    def test_stop_playback_no_auth_required(self, app_client):
        """Test stopping playback still works (no auth required in current impl)."""
        response = app_client.post("/api/shows/1/playback/stop")
        assert response.status_code == 200


class TestRequireShowOwner:
    """Tests for require_show_owner utility."""

    def test_require_show_owner_unauthenticated(self):
        """Test that unauthenticated request raises 401."""
        from app.routes.utils import require_show_owner
        from fastapi import HTTPException

        mock_request = MagicMock()
        mock_db_session = MagicMock()

        with patch("app.routes.utils.get_current_user_from_request", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                require_show_owner(show_id=1, request=mock_request, db_session=mock_db_session)

            assert exc_info.value.status_code == 401
