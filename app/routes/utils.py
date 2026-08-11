import secrets

from fastapi import HTTPException, Request, status

from app.auth import get_current_user_from_request
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
