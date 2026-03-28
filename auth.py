import os
import bcrypt
import jwt
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from db import DatabaseManager
from models import User


JWT_SECRET = os.environ.get("JWT_SECRET", "change-me-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its bcrypt hash."""
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_access_token(user_id: int) -> str:
    """Generate a JWT token for a user."""
    expire = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS)
    payload = {
        "sub": str(user_id),
        "exp": expire,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT token."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def get_current_user(
    credentials: Optional[HTTPBasicCredentials] = Depends(HTTPBasic(auto_error=False))
) -> Optional[User]:
    """
    Dependency that extracts the current user from JWT Bearer token or HTTP Basic auth.
    Returns None if no valid auth is provided (for routes that allow anonymous access).
    Raises HTTPException if credentials are invalid.
    """
    from fastapi import Request
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

    # This is called as a dependency but we need to check Bearer token too
    # We'll handle this differently - see get_current_user_from_request
    return None


def get_current_user_from_request(request) -> Optional[User]:
    """
    Extract and validate user from request.
    Checks Authorization: Bearer <token> header first, then falls back to HTTP Basic.
    Falls back to env var passwords if no JWT provided (backwards compatibility).
    Returns None if no valid auth, raises HTTPException if invalid.
    """
    from framework_state import state

    auth_header = request.headers.get("Authorization")

    # Try JWT Bearer token first
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
        payload = decode_token(token)
        if payload and "sub" in payload:
            user_id = int(payload["sub"])
            db_manager = DatabaseManager.get_instance()
            with db_manager.session() as session:
                user = session.query(User).filter(User.id == user_id).first()
                if user and user.is_active:
                    return user
        # Invalid or expired token
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Fall back to HTTP Basic auth
    dj_pass = getattr(state, "dj_password", "")
    aud_pass = getattr(state, "audience_password", "")

    if not dj_pass and not aud_pass:
        # No auth configured - allow anonymous
        return None

    provided_pass = None
    if auth_header and auth_header.startswith("Basic "):
        import base64
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            if ":" in decoded:
                _, provided_pass = decoded.split(":", 1)
        except Exception:
            pass

    if provided_pass == dj_pass:
        # Create a pseudo-user for backwards compatibility
        class CompatUser:
            id = 0
            username = "djCompat"
            email = "compat@local"
            is_active = True

            def to_dict(self):
                return {"id": 0, "username": "djCompat", "email": "compat@local"}

        return CompatUser()

    if provided_pass == aud_pass:
        class CompatAudUser:
            id = -1  # Special ID for audience compat
            username = "audienceCompat"
            email = "audience@local"
            is_active = True

            def to_dict(self):
                return {"id": -1, "username": "audienceCompat"}

        return CompatAudUser()

    return None


def get_db():
    """Dependency for SQLAlchemy session."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        yield session


def require_auth(user=None):
    """Decorator-style dependency that requires authentication."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user
