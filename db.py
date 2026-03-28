import os
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

Base = declarative_base()


class DatabaseManager:
    _instance = None

    def __init__(self):
        database_url = os.environ.get("DATABASE_URL")

        if database_url:
            # PostgreSQL in production
            self.engine = create_engine(database_url, pool_size=10, max_overflow=20)
        else:
            # SQLite fallback for local development
            db_path = os.path.join(os.path.dirname(__file__), "data", "mc_clanker.db")
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            self.engine = create_engine(
                f"sqlite:///{db_path}",
                connect_args={"check_same_thread": False}
            )

        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def create_tables(self):
        Base.metadata.create_all(bind=self.engine)

    @contextmanager
    def session(self):
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def get_db():
    """Dependency for FastAPI routes."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        yield session
