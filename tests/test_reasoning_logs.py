import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

os.environ["DATABASE_URL"] = ""  # Force SQLite for tests

from fastapi.testclient import TestClient

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


def _make_mock_interaction(
    show_id=1,
    loop_index=1,
    action_type="retain",
    bpm=128.0,
    key="C",
    instruments=None,
    set_name="Verse",
    reasoning="Keeping the groove",
):
    """Helper to create a mock LLMInteraction row."""
    interaction = MagicMock()
    interaction.id = loop_index
    interaction.show_id = show_id
    interaction.loop_index = loop_index
    interaction.timestamp = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    interaction.relative_time_ms = loop_index * 4000
    interaction.prompt_messages = [{"role": "system", "content": "You are a DJ"}]
    interaction.parsed_response = {"master_bpm": 128, "actions": []}
    interaction.reasoning = reasoning
    interaction.error = None
    interaction.was_fallback = False
    interaction.bpm = bpm
    interaction.key = key
    interaction.instruments = instruments or ["Bass", "Drums"]
    interaction.action_type = action_type
    interaction.set_name = set_name
    interaction.to_dict.return_value = {
        "id": loop_index,
        "show_id": show_id,
        "loop_index": loop_index,
        "timestamp": "2024-01-01T12:00:00+00:00",
        "relative_time_ms": loop_index * 4000,
        "prompt_messages": [{"role": "system", "content": "You are a DJ"}],
        "parsed_response": {"master_bpm": 128, "actions": []},
        "reasoning": reasoning,
        "error": None,
        "was_fallback": False,
        "bpm": bpm,
        "key": key,
        "instruments": instruments or ["Bass", "Drums"],
        "action_type": action_type,
        "set_name": set_name,
    }
    interaction.to_reasoning_export_dict.return_value = {
        "id": loop_index,
        "show_id": show_id,
        "loop_index": loop_index,
        "timestamp": "2024-01-01T12:00:00+00:00",
        "relative_time_ms": loop_index * 4000,
        "bpm": bpm,
        "key": key,
        "instruments": instruments or ["Bass", "Drums"],
        "action_type": action_type,
        "set_name": set_name,
        "reasoning": reasoning,
        "was_fallback": False,
        "prompt_messages": [{"role": "system", "content": "You are a DJ"}],
        "parsed_response": {"master_bpm": 128, "actions": []},
    }
    return interaction


class TestReasoningLogsSearch:
    """Tests for GET /api/llm-config/reasoning-logs."""

    def test_search_requires_auth(self, app_client):
        """Unauthenticated request returns 401 when password is set."""
        with patch.object(state, "dj_password", "secret"):
            response = app_client.get("/api/llm-config/reasoning-logs?show_id=1")
            assert response.status_code == 401

    def test_search_returns_empty_when_no_interactions(self, app_client, mock_auth_user):
        """Empty result when show has no interactions."""
        mock_session = MagicMock()
        self._setup_mock_query_chain(mock_session, [], 0)

        mock_db = MagicMock()
        mock_db.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.session.return_value.__exit__ = MagicMock(return_value=False)
        mock_db.is_postgres = False

        with patch("app.routes.reasoning_logs.get_current_user_from_request", return_value=mock_auth_user):
            with patch("app.db.DatabaseManager.get_instance", return_value=mock_db):
                with patch("app.routes.reasoning_logs._require_show_owner", return_value=None):
                    response = app_client.get("/api/llm-config/reasoning-logs?show_id=1")

        assert response.status_code == 200
        data = response.json()
        assert data["interactions"] == []
        assert data["total"] == 0

    def _setup_mock_query_chain(self, mock_session, interactions, total):
        """Set up a mock query where .filter() returns self for chaining."""
        mock_query = MagicMock()
        # filter() returns self so chaining works
        mock_query.filter.return_value = mock_query
        mock_query.count.return_value = total
        mock_query.order_by.return_value.limit.return_value.offset.return_value.all.return_value = interactions
        # session.query(LLMInteraction) returns the chainable query
        mock_session.query.return_value = mock_query
        return mock_session

    def test_search_with_action_type_filter(self, app_client, mock_auth_user):
        """Filter by action_type returns matching interactions."""
        mock_interaction = _make_mock_interaction(action_type="add")

        mock_session = MagicMock()
        self._setup_mock_query_chain(mock_session, [mock_interaction], 1)

        mock_db = MagicMock()
        mock_db.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.session.return_value.__exit__ = MagicMock(return_value=False)
        mock_db.is_postgres = False

        with patch("app.routes.reasoning_logs.get_current_user_from_request", return_value=mock_auth_user):
            with patch("app.db.DatabaseManager.get_instance", return_value=mock_db):
                with patch("app.routes.reasoning_logs._require_show_owner", return_value=None):
                    response = app_client.get("/api/llm-config/reasoning-logs?show_id=1&action_type=add")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["interactions"]) == 1
        assert data["interactions"][0]["action_type"] == "add"

    def test_search_with_bpm_range_filter(self, app_client, mock_auth_user):
        """Filter by bpm_min and bpm_max."""
        mock_interaction = _make_mock_interaction(bpm=130.0)

        mock_session = MagicMock()
        self._setup_mock_query_chain(mock_session, [mock_interaction], 1)

        mock_db = MagicMock()
        mock_db.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.session.return_value.__exit__ = MagicMock(return_value=False)
        mock_db.is_postgres = False

        with patch("app.routes.reasoning_logs.get_current_user_from_request", return_value=mock_auth_user):
            with patch("app.db.DatabaseManager.get_instance", return_value=mock_db):
                with patch("app.routes.reasoning_logs._require_show_owner", return_value=None):
                    response = app_client.get("/api/llm-config/reasoning-logs?show_id=1&bpm_min=120&bpm_max=140")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1

    def test_search_with_q_text_filter(self, app_client, mock_auth_user):
        """Full-text search on reasoning field."""
        mock_interaction = _make_mock_interaction(reasoning="Switching to drop pattern")

        mock_session = MagicMock()
        self._setup_mock_query_chain(mock_session, [mock_interaction], 1)

        mock_db = MagicMock()
        mock_db.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.session.return_value.__exit__ = MagicMock(return_value=False)
        mock_db.is_postgres = False

        with patch("app.routes.reasoning_logs.get_current_user_from_request", return_value=mock_auth_user):
            with patch("app.db.DatabaseManager.get_instance", return_value=mock_db):
                with patch("app.routes.reasoning_logs._require_show_owner", return_value=None):
                    response = app_client.get("/api/llm-config/reasoning-logs?show_id=1&q=drop")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1


class TestReasoningLogsExport:
    """Tests for GET /api/llm-config/reasoning-logs/export."""

    def test_export_requires_auth(self, app_client):
        with patch.object(state, "dj_password", "secret"):
            response = app_client.get("/api/llm-config/reasoning-logs/export?show_id=1")
            assert response.status_code == 401

    def test_export_returns_jsonl_stream(self, app_client, mock_auth_user):
        """Export returns NDJSON content type."""
        mock_interaction = _make_mock_interaction()

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.order_by.return_value.all.return_value = [mock_interaction]

        mock_db = MagicMock()
        mock_db.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.session.return_value.__exit__ = MagicMock(return_value=False)
        mock_db.is_postgres = False

        with patch("app.routes.reasoning_logs.get_current_user_from_request", return_value=mock_auth_user):
            with patch("app.db.DatabaseManager.get_instance", return_value=mock_db):
                with patch("app.routes.reasoning_logs._require_show_owner", return_value=None):
                    response = app_client.get("/api/llm-config/reasoning-logs/export?show_id=1")

        assert response.status_code == 200
        assert "application/x-ndjson" in response.headers.get("content-type", "")
        assert "attachment" in response.headers.get("content-disposition", "")


class TestReasoningTimeline:
    """Tests for GET /api/llm-config/reasoning-timeline."""

    def test_timeline_requires_auth(self, app_client):
        with patch.object(state, "dj_password", "secret"):
            response = app_client.get("/api/llm-config/reasoning-timeline?show_id=1")
            assert response.status_code == 401

    def test_timeline_empty_show(self, app_client, mock_auth_user):
        """Empty timeline when no interactions."""
        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value.all.return_value = []
        mock_session.query.return_value = mock_query

        mock_db = MagicMock()
        mock_db.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.session.return_value.__exit__ = MagicMock(return_value=False)
        mock_db.is_postgres = False

        with patch("app.routes.reasoning_logs.get_current_user_from_request", return_value=mock_auth_user):
            with patch("app.db.DatabaseManager.get_instance", return_value=mock_db):
                with patch("app.routes.reasoning_logs._require_show_owner", return_value=None):
                    response = app_client.get("/api/llm-config/reasoning-timeline?show_id=1")

        assert response.status_code == 200
        data = response.json()
        assert data["segments"] == []
        assert data["total_interactions"] == 0

    def test_timeline_segments(self, app_client, mock_auth_user):
        """Timeline returns segments with aggregated data."""
        interactions = [
            _make_mock_interaction(
                loop_index=1, action_type="retain", bpm=128.0, instruments=["Bass"], reasoning="Keep bass"
            ),
            _make_mock_interaction(
                loop_index=2, action_type="add", bpm=130.0, instruments=["Bass", "Drums"], reasoning="Add drums"
            ),
        ]

        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value.all.return_value = interactions
        mock_session.query.return_value = mock_query

        mock_db = MagicMock()
        mock_db.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.session.return_value.__exit__ = MagicMock(return_value=False)
        mock_db.is_postgres = False

        with patch("app.routes.reasoning_logs.get_current_user_from_request", return_value=mock_auth_user):
            with patch("app.db.DatabaseManager.get_instance", return_value=mock_db):
                with patch("app.routes.reasoning_logs._require_show_owner", return_value=None):
                    response = app_client.get("/api/llm-config/reasoning-timeline?show_id=1&segment_seconds=30")

        assert response.status_code == 200
        data = response.json()
        assert data["total_interactions"] == 2
        assert data["segment_seconds"] == 30
        assert len(data["segments"]) >= 1


class TestReasoningStats:
    """Tests for GET /api/llm-config/reasoning-logs/stats."""

    def test_stats_requires_auth(self, app_client):
        with patch.object(state, "dj_password", "secret"):
            response = app_client.get("/api/llm-config/reasoning-logs/stats?show_id=1")
            assert response.status_code == 401

    def test_stats_empty_show(self, app_client, mock_auth_user):
        """Stats with no interactions returns zeros."""
        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.all.return_value = []
        mock_session.query.return_value = mock_query

        mock_db = MagicMock()
        mock_db.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.session.return_value.__exit__ = MagicMock(return_value=False)
        mock_db.is_postgres = False

        with patch("app.routes.reasoning_logs.get_current_user_from_request", return_value=mock_auth_user):
            with patch("app.db.DatabaseManager.get_instance", return_value=mock_db):
                with patch("app.routes.reasoning_logs._require_show_owner", return_value=None):
                    response = app_client.get("/api/llm-config/reasoning-logs/stats?show_id=1")

        assert response.status_code == 200
        data = response.json()
        assert data["total_interactions"] == 0
        assert data["avg_bpm"] is None

    def test_stats_aggregates(self, app_client, mock_auth_user):
        """Stats correctly aggregate interaction data."""
        interactions = [
            _make_mock_interaction(action_type="retain", bpm=128.0, key="C", instruments=["Bass"], reasoning="Keep it"),
            _make_mock_interaction(
                action_type="add", bpm=132.0, key="C", instruments=["Bass", "Drums"], reasoning="Add drums"
            ),
            _make_mock_interaction(
                action_type="remove", bpm=130.0, key="C", instruments=["Drums"], reasoning="Remove bass"
            ),
        ]

        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.all.return_value = interactions
        mock_session.query.return_value = mock_query

        mock_db = MagicMock()
        mock_db.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db.session.return_value.__exit__ = MagicMock(return_value=False)
        mock_db.is_postgres = False

        with patch("app.routes.reasoning_logs.get_current_user_from_request", return_value=mock_auth_user):
            with patch("app.db.DatabaseManager.get_instance", return_value=mock_db):
                with patch("app.routes.reasoning_logs._require_show_owner", return_value=None):
                    response = app_client.get("/api/llm-config/reasoning-logs/stats?show_id=1")

        assert response.status_code == 200
        data = response.json()
        assert data["total_interactions"] == 3
        assert data["action_counts"]["retain"] == 1
        assert data["action_counts"]["add"] == 1
        assert data["action_counts"]["remove"] == 1
        assert "Bass" in data["instruments_used"]
        assert "Drums" in data["instruments_used"]
        assert data["avg_bpm"] == pytest.approx(130.0, rel=0.1)
