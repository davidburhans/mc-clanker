import os
from unittest.mock import MagicMock, patch


class TestDatabaseManager:
    """Tests for DatabaseManager class."""

    def test_database_manager_singleton(self):
        """Test that DatabaseManager is a singleton."""
        # Reset singleton for testing
        from app.db import DatabaseManager

        DatabaseManager._instance = None

        with patch.dict(os.environ, {}, clear=True):
            with patch("app.db.create_engine"):
                with patch("app.db.sessionmaker"):
                    # First call should create instance
                    db1 = DatabaseManager.get_instance()
                    db2 = DatabaseManager.get_instance()

                    assert db1 is db2

        # Reset for other tests
        DatabaseManager._instance = None

    def test_database_url_postgresql(self):
        """Test PostgreSQL database URL configuration."""
        from app.db import DatabaseManager

        DatabaseManager._instance = None

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:pass@localhost/db"}):
            with patch("app.db.create_engine") as mock_engine:
                with patch("app.db.sessionmaker"):
                    DatabaseManager.get_instance()
                    mock_engine.assert_called_once()
                    call_args = mock_engine.call_args[0][0]
                    assert call_args == "postgresql://user:pass@localhost/db"

        DatabaseManager._instance = None

    def test_database_url_sqlite_fallback(self):
        """Test SQLite fallback when no DATABASE_URL."""
        from app.db import DatabaseManager

        DatabaseManager._instance = None

        # Clear DATABASE_URL
        env = os.environ.copy()
        env.pop("DATABASE_URL", None)

        with patch.dict(os.environ, env, clear=True):
            with patch("app.db.create_engine") as mock_engine:
                with patch("app.db.sessionmaker"):
                    with patch("os.makedirs"):
                        with patch("os.path.dirname", return_value="/tmp"):
                            DatabaseManager.get_instance()
                            call_args = mock_engine.call_args[0][0]
                            assert "sqlite" in call_args

        DatabaseManager._instance = None

    def test_create_tables(self, monkeypatch, tmp_path):
        """create_tables() issues DDL for every ORM model.

        D9: the previous body was `pass` inside a mocked-sys.modules block — it
        never called create_tables and had no assertion. This drives the REAL
        Base.metadata.create_all against a throwaway sqlite file and asserts the
        expected tables are materialized.
        """
        from sqlalchemy import inspect

        import app.models  # noqa: F401  — register ORM models with Base.metadata
        from app.db import DatabaseManager

        DatabaseManager._instance = None
        db_file = tmp_path / "create_tables_test.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
        try:
            db = DatabaseManager.get_instance()
            db.create_tables()

            table_names = set(inspect(db.engine).get_table_names())
        finally:
            DatabaseManager._instance = None

        expected = {
            "users",
            "shows",
            "show_actions",
            "llm_interactions",
            "generator_jobs",
            "session_routing",
        }
        missing = expected - table_names
        assert not missing, f"create_tables() did not create tables: {missing}"

    def test_session_context_manager(self):
        """Test session context manager."""
        from app.db import DatabaseManager

        DatabaseManager._instance = None

        with patch.dict(os.environ, {}, clear=True):
            with patch("app.db.create_engine"):
                mock_session = MagicMock()
                mock_session_maker = MagicMock(return_value=mock_session)
                with patch("app.db.sessionmaker", return_value=mock_session_maker):
                    with patch("os.makedirs"):
                        with patch("os.path.dirname", return_value="/tmp"):
                            db = DatabaseManager.get_instance()

                            # Test session context manager
                            with db.session() as session:
                                assert session is mock_session

                            # Verify commit was called
                            mock_session.commit.assert_called()

    def test_session_context_manager_rollback_on_exception(self):
        """Test session rollback when exception occurs in context manager."""
        from app.db import DatabaseManager

        DatabaseManager._instance = None

        with patch.dict(os.environ, {}, clear=True):
            with patch("app.db.create_engine"):
                mock_session = MagicMock()
                mock_session_maker = MagicMock(return_value=mock_session)
                with patch("app.db.sessionmaker", return_value=mock_session_maker):
                    with patch("os.makedirs"):
                        with patch("os.path.dirname", return_value="/tmp"):
                            db = DatabaseManager.get_instance()

                            # Test that rollback is called when exception occurs
                            try:
                                with db.session() as session:
                                    assert session is mock_session
                                    raise ValueError("Test exception")
                            except ValueError:
                                pass

                            # Verify rollback was called
                            mock_session.rollback.assert_called()
                            # Verify close was called
                            mock_session.close.assert_called()
                            # Verify commit was NOT called after exception
                            mock_session.commit.assert_not_called()

    def test_session_context_manager_close_always_called(self):
        """Test that session close is always called even without exception."""
        from app.db import DatabaseManager

        DatabaseManager._instance = None

        with patch.dict(os.environ, {}, clear=True):
            with patch("app.db.create_engine"):
                mock_session = MagicMock()
                mock_session_maker = MagicMock(return_value=mock_session)
                with patch("app.db.sessionmaker", return_value=mock_session_maker):
                    with patch("os.makedirs"):
                        with patch("os.path.dirname", return_value="/tmp"):
                            db = DatabaseManager.get_instance()

                            with db.session() as session:
                                assert session is mock_session

                            # Verify close was called after normal exit
                            mock_session.close.assert_called()


class TestModelsIntegration:
    """Integration tests for models with database."""

    def test_user_model_fields(self):
        """Test User model has required fields."""
        from app.models.user import User

        fields = [c.name for c in User.__table__.columns]
        assert "id" in fields
        assert "username" in fields
        assert "email" in fields
        assert "password_hash" in fields
        assert "created_at" in fields
        assert "is_active" in fields

    def test_show_model_fields(self):
        """Test Show model has required fields."""
        from app.models.show import Show

        fields = [c.name for c in Show.__table__.columns]
        assert "id" in fields
        assert "user_id" in fields
        assert "title" in fields
        assert "description" in fields
        assert "status" in fields
        assert "audio_file_path" in fields
        assert "audience_password_hash" in fields
        assert "config_snapshot" in fields
        assert "started_at" in fields
        assert "ended_at" in fields
        assert "duration_seconds" in fields

    def test_show_action_model_fields(self):
        """Test ShowAction model has required fields."""
        from app.models.show_action import ShowAction

        fields = [c.name for c in ShowAction.__table__.columns]
        assert "id" in fields
        assert "show_id" in fields
        assert "loop_index" in fields
        assert "timestamp" in fields
        assert "relative_time_ms" in fields
        assert "action_type" in fields
        assert "stem_index" in fields
        assert "stem_details" in fields
        assert "action_description" in fields

    def test_llm_interaction_model_fields(self):
        """Test LLMInteraction model has required fields."""
        from app.models.llm_interaction import LLMInteraction

        fields = [c.name for c in LLMInteraction.__table__.columns]
        assert "id" in fields
        assert "show_id" in fields
        assert "loop_index" in fields
        assert "timestamp" in fields
        assert "relative_time_ms" in fields
        assert "prompt_messages" in fields
        assert "parsed_response" in fields
        assert "reasoning" in fields
        assert "error" in fields
        assert "was_fallback" in fields
