"""Sync DB lookups the HTTP middlewares run off the event loop (REL-09).

Each helper opens its own short-lived session and returns detached-safe data
(expunged instance or scalars) — the middlewares await these via
``asyncio.to_thread`` (pattern: routes/config._ping_database), so a hung
database bounds at the engine's connect/statement timeouts instead of
freezing the event loop. Imports stay lazy inside the bodies so tests can
patch ``app.db.DatabaseManager`` (the seam test_shows_api/test_db_offloop use).
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import User


def fetch_bearer_user(user_id: int) -> "User | None":
    """Active User for a Bearer-token id, expunged from its session, or None.

    request.state.user outlives this session (SEC-5), hence the expunge.

    Example:
        user = await asyncio.to_thread(fetch_bearer_user, 42)
    """
    from app.db import DatabaseManager
    from app.models import User

    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        user = session.query(User).filter(User.id == user_id).first()
        if user and user.is_active:
            session.expunge(user)
            return user
    return None


def fetch_show_gate_fields(show_id: int) -> tuple[int | None, str | None]:
    """(user_id, audience_password_hash) for the per-show audience gate.

    (None, None) = show not found; (user_id, None) = show has no password.
    Scalars only: the ORM row would detach at session close.

    Example:
        user_id, pw_hash = await asyncio.to_thread(fetch_show_gate_fields, 1)
    """
    from app.db import DatabaseManager
    from app.models import Show

    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        show = session.query(Show).filter(Show.id == show_id).first()
        if show is None:
            return None, None
        return show.user_id, show.audience_password_hash


def lookup_session_server(session_id: str) -> str | None:
    """server_id that owns session_id, or None when unrouted (REL-09).

    Example:
        server = await asyncio.to_thread(lookup_session_server, session_id)
    """
    from sqlalchemy import text

    from app.db import DatabaseManager

    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        row = session.execute(
            text("SELECT server_id FROM session_routing WHERE session_id = :session_id"),
            {"session_id": session_id},
        ).fetchone()
        return None if row is None else str(row[0])
