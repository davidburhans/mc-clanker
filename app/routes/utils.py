import secrets

from fastapi import HTTPException, Request, status

from app.auth import get_current_user_from_request
from app.db import DatabaseManager
from app.models import Show


def generate_audience_password() -> str:
    """Generate a random audience password."""
    return secrets.token_urlsafe(16)


def require_show_owner(show_id: int, request: Request, db_session):
    """Verify the current user owns the show."""
    user = get_current_user_from_request(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    show = db_session.query(Show).filter(Show.id == show_id).first()
    if show is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Show not found")

    if show.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Show not found")

    return show


def fetch_owned_show(db_manager: DatabaseManager, show_id: int, request: Request) -> Show:
    """Auth + ownership gate for the streaming export routes (REL-13), sequenced
    so the request path holds at most ONE session at a time.

    ``require_show_owner`` callers nest: the route session stays open while
    ``get_current_user_from_request`` opens a second one. Export streams must
    never overlap sessions (one short session per chunk, plan T5), so the
    user lookup closes its session BEFORE the show query opens this one.
    Same outcome order as ``require_show_owner``: 401 anonymous/invalid
    credentials, 404 missing/foreign show. The row is expunged pre-commit
    with all columns loaded (SEC-5 precedent) — callers may read to_dict()
    detached.

    Example::

        show_dict = fetch_owned_show(db_manager, show_id, request).to_dict()
    """
    user = get_current_user_from_request(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    with db_manager.session() as session:
        show = session.query(Show).filter(Show.id == show_id).first()
        if show is None or show.user_id != user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Show not found")
        session.expunge(show)
        return show
