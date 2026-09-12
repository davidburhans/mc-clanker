import asyncio
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


class TestEngineResilienceRel09:
    """REL-09a: per-dialect engine resilience config (unit plan §3.1, T1-T4).

    The PG branch must gain pool_pre_ping / pool_recycle / libpq connect+statement
    timeouts; the no-DATABASE_URL SQLite fallback must stay byte-identical; an
    explicit sqlite:// DATABASE_URL must keep working — including sessions used
    from asyncio.to_thread worker threads (REL-09 moves DB access off the loop).
    """

    def test_pg_engine_gets_resilience_kwargs(self, monkeypatch):
        """T1: DATABASE_URL=postgresql:// builds the engine with the REL-09 kwargs."""
        from app.db import DatabaseManager

        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
        with patch("app.db.create_engine") as mock_engine, patch("app.db.sessionmaker"):
            DatabaseManager.get_instance()

        mock_engine.assert_called_once()
        call_args, call_kwargs = mock_engine.call_args
        assert call_args == ("postgresql://u:p@h/db",)
        assert call_kwargs["pool_size"] == 10
        assert call_kwargs["max_overflow"] == 20
        assert call_kwargs["pool_pre_ping"] is True
        assert call_kwargs["pool_recycle"] == 1800
        # FU-1 (rel-17 follow-up): exact-dict assert grows the libpq TCP
        # keepalives — a half-open pooled conn must be detected in ~60 s
        # instead of the OS default (often 2 h+); pre_ping only catches conns
        # that error, not ones that silently black-hole.
        assert call_kwargs["connect_args"] == {
            "connect_timeout": 5,
            "options": "-c statement_timeout=10000",
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        }

    def test_sqlite_fallback_engine_unchanged(self, monkeypatch):
        """T2: no DATABASE_URL — the fallback branch stays byte-identical (no PG kwargs)."""
        from app.db import DatabaseManager

        monkeypatch.delenv("DATABASE_URL", raising=False)
        with (
            patch("app.db.create_engine") as mock_engine,
            patch("app.db.sessionmaker"),
            patch("os.makedirs"),
        ):
            DatabaseManager.get_instance()

        call_args, call_kwargs = mock_engine.call_args
        assert "sqlite" in call_args[0]
        # Exact-dict assert: neither pool_pre_ping nor pool_recycle may leak in.
        assert call_kwargs == {"connect_args": {"check_same_thread": False}}

    async def test_sqlite_database_url_routes_to_sqlite_branch(self, monkeypatch, tmp_path):
        """T3: sqlite:// DATABASE_URL is honored verbatim and is cross-thread safe.

        test_db.py:79 precedent: DATABASE_URL may be sqlite, so the PG branch must
        be dialect-gated. The load-bearing assert is the construction contract: the
        non-PG DATABASE_URL branch must carry ``check_same_thread=False`` — REL-09
        runs DB access in asyncio.to_thread workers, and a pooled sqlite connection
        created on the loop thread raises ProgrammingError in a worker without it.
        (File-based sqlite defaults to NullPool, so the flag cannot be observed
        behaviorally — hence the recording spy around the real create_engine.)
        """
        import app.db as db_module
        import app.models  # noqa: F401  — register ORM models with Base.metadata
        from app.db import DatabaseManager
        from app.models import User

        recorded_kwargs: dict = {}
        real_create_engine = db_module.create_engine

        def _recording_create_engine(url, **kwargs):
            recorded_kwargs.update(kwargs)
            return real_create_engine(url, **kwargs)

        monkeypatch.setattr(db_module, "create_engine", _recording_create_engine)

        db_file = tmp_path / "dburl_sqlite.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
        db = DatabaseManager.get_instance()
        assert db.is_sqlite
        assert recorded_kwargs.get("connect_args") == {"check_same_thread": False}, (
            f"sqlite DATABASE_URL engine missing check_same_thread=False: kwargs={sorted(recorded_kwargs)}"
        )
        db.create_tables()

        def insert_then_count() -> int:
            with db.session() as session:
                session.add(User(username="dburl_offloop", email="dburl@example.com", password_hash="x"))
            with db.session() as session:
                return session.query(User).count()

        user_count = await asyncio.to_thread(insert_then_count)
        assert user_count == 1

    def test_pg_driver_suffix_routes_to_pg_branch(self, monkeypatch):
        """T4: postgresql+psycopg2:// normalizes onto the PG branch (make_url gating)."""
        from app.db import DatabaseManager

        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://u:p@h/db")
        with patch("app.db.create_engine") as mock_engine, patch("app.db.sessionmaker"):
            DatabaseManager.get_instance()

        call_kwargs = mock_engine.call_args.kwargs
        assert call_kwargs["pool_pre_ping"] is True
        # FU-1 (rel-17 follow-up): same keepalive growth as T1 — this test pins
        # the PG branch by equality too (postgresql+psycopg2 URL), so it must
        # carry the identical connect_args contract.
        assert call_kwargs["connect_args"] == {
            "connect_timeout": 5,
            "options": "-c statement_timeout=10000",
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        }
