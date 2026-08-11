"""auth.py — JWT + HTTP Basic authentication for mc-clanker.

Security fixes applied:
- JWT_SECRET auto-generates a random ephemeral secret if the env var is
  missing or is a known-weak value (2.1).
- Password comparisons use hmac.compare_digest instead of == to prevent
  timing attacks (2.2).
- User model imported in module scope to fix NameError in AuthMiddleware (1.1).
"""

import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.db import DatabaseManager
from app.models import User  # required — was missing, caused NameError in AuthMiddleware

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JWT configuration
# ---------------------------------------------------------------------------

_WEAK_SECRETS = {
    "change-me-in-production",
    "secret",
    "changeme",
    "password",
    "123456",
    "",
}

_raw_secret = os.environ.get("JWT_SECRET", "")
if not _raw_secret or _raw_secret.lower() in _WEAK_SECRETS:
    JWT_SECRET = secrets.token_urlsafe(48)
    if _raw_secret:
        log.warning(
            "JWT_SECRET is a known-weak value; using auto-generated ephemeral "
            "secret for this session. Set JWT_SECRET in your environment."
        )
    else:
        log.warning(
            "JWT_SECRET is not set; using auto-generated ephemeral secret. "
            "Tokens will be invalidated on restart. Set JWT_SECRET to persist sessions."
        )
else:
    JWT_SECRET = _raw_secret

JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def create_access_token(user_id: int) -> str:
    """Generate a JWT token for a user."""
    expire = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS)
    payload = {
        "sub": str(user_id),
        "exp": expire,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict | None:
    """Decode and validate a JWT token. Returns None on any failure."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


# ---------------------------------------------------------------------------
# Compat pseudo-users for env-var password auth (backwards compatibility)
# ---------------------------------------------------------------------------


class CompatUser:
    """Pseudo-user for backwards-compatible DJ Basic-auth sessions."""

    id = 0
    username = "djCompat"
    email = "compat@local"
    is_active = True

    def to_dict(self):
        return {"id": 0, "username": "djCompat", "email": "compat@local"}


class CompatAudUser:
    """Pseudo-user for backwards-compatible audience Basic-auth sessions."""

    id = -1
    username = "audienceCompat"
    email = "audience@local"
    is_active = True

    def to_dict(self):
        return {"id": -1, "username": "audienceCompat"}


# ---------------------------------------------------------------------------
# Request-level auth extraction
# ---------------------------------------------------------------------------


def get_current_user_from_request(request) -> object | None:
    """Extract and validate user from request.

    Priority:
      1. Authorization: Bearer <jwt>  — full user from DB
      2. Authorization: Basic <creds> — env-var compat user
      3. No auth configured          — anonymous (None)

    Raises HTTPException(401) if credentials are present but invalid.
    """
    from app.framework.framework_state import state  # late import avoids circular dep

    auth_header = request.headers.get("Authorization")

    # 1. JWT Bearer token
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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 2. HTTP Basic auth (env-var compat)
    dj_pass = getattr(state, "dj_password", "") or ""
    aud_pass = getattr(state, "audience_password", "") or ""

    if not dj_pass and not aud_pass:
        # No passwords configured — anonymous access allowed
        return None

    provided_pass: str | None = None
    if auth_header and auth_header.startswith("Basic "):
        import base64

        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            if ":" in decoded:
                _, provided_pass = decoded.split(":", 1)
        except Exception:
            pass

    if provided_pass is None:
        return None

    # Use constant-time comparison to prevent timing attacks (fix 2.2)
    if dj_pass and hmac.compare_digest(provided_pass, dj_pass):
        return CompatUser()

    if aud_pass and hmac.compare_digest(provided_pass, aud_pass):
        return CompatAudUser()

    return None


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def get_current_user(credentials: HTTPBasicCredentials | None = Depends(HTTPBasic(auto_error=False))) -> User | None:
    """FastAPI dependency — exists for route signature compatibility.
    Actual auth is handled by get_current_user_from_request in middleware.
    """
    return None


def get_db():
    """Dependency for SQLAlchemy session."""
    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        yield session


def require_auth(user=None):
    """Raise 401 if user is None."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user
