import os
import threading
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

# REL-09: bounds on every pooled PG connection so a hung/NAT-idled database
# degrades (one cancelled statement / one pre-ping reconnect) instead of
# freezing the event loop for the OS-level TCP timeout (~2 min). Constants,
# not env knobs — promote only when a deployment needs a different budget.
DB_CONNECT_TIMEOUT_SECONDS = 5
DB_STATEMENT_TIMEOUT_MS = 10_000
DB_POOL_RECYCLE_SECONDS = 1800

# FU-1 (rel-17 follow-up): TCP keepalives so a silently dropped (half-open,
# no FIN) pooled conn is detected in ~idle + count*interval ≈ 60 s instead of
# the OS default (often 2 h+). pre_ping covers conns that error; keepalives
# cover the black-holed ones. libpq-only — never passed to SQLite paths
# (SQLite conninfo would reject libpq params). Constants, not env knobs —
# matching the REL-09 DB_* contract above.
DB_KEEPALIVE_IDLE_SECONDS = 30
DB_KEEPALIVE_INTERVAL_SECONDS = 10
DB_KEEPALIVE_COUNT = 3


class DatabaseManager:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        database_url = os.environ.get("DATABASE_URL")

        if database_url and make_url(database_url).get_backend_name() == "postgresql":
            # PostgreSQL in production (REL-09): pre-ping recovers NAT-idled
            # conns, recycle bounds pool age, connect/statement timeouts bound
            # a hung DB so middleware/route queries can never block the loop
            # past ~10 s. libpq-only args — never passed to the SQLite paths.
            self.engine = create_engine(
                database_url,
                pool_size=10,
                max_overflow=20,
                pool_pre_ping=True,
                pool_recycle=DB_POOL_RECYCLE_SECONDS,
                connect_args={
                    "connect_timeout": DB_CONNECT_TIMEOUT_SECONDS,
                    "options": f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}",
                    "keepalives": 1,
                    "keepalives_idle": DB_KEEPALIVE_IDLE_SECONDS,
                    "keepalives_interval": DB_KEEPALIVE_INTERVAL_SECONDS,
                    "keepalives_count": DB_KEEPALIVE_COUNT,
                },
            )
        elif database_url:
            # Explicit non-PG DATABASE_URL (tests set sqlite:///...): honor it
            # verbatim, plus the SQLite cross-thread flag the fallback uses —
            # REL-09 moves DB access into asyncio.to_thread worker threads.
            self.engine = create_engine(database_url, connect_args={"check_same_thread": False})
        else:
            # SQLite fallback for local development
            db_path = os.path.join(os.path.dirname(__file__), "data", "mc_clanker.db")
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            self.engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    @classmethod
    def get_instance(cls) -> "DatabaseManager":
        """Thread-safe singleton using double-checked locking."""
        if cls._instance is None:
            with cls._lock:
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

    @property
    def is_sqlite(self) -> bool:
        return self.engine.dialect.name == "sqlite"

    @property
    def is_postgres(self) -> bool:
        return self.engine.dialect.name == "postgresql"


def get_db():
    """Dependency for FastAPI routes."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        yield session
