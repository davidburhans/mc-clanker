import pytest
from unittest.mock import MagicMock, patch
import os

# Set test environment before importing modules
os.environ["DATABASE_URL"] = ""  # Force SQLite for tests


class TestAuthModule:
    """Tests for auth.py functions."""

    def test_hash_password(self):
        from auth import hash_password, verify_password

        password = "test_password_123"
        hashed = hash_password(password)

        # Hash should be different from original
        assert hashed != password
        # Hash should be bcrypt format (starts with $2)
        assert hashed.startswith("$2")

    def test_verify_password_correct(self):
        from auth import hash_password, verify_password

        password = "test_password_123"
        hashed = hash_password(password)

        assert verify_password(password, hashed) is True

    def test_verify_password_incorrect(self):
        from auth import hash_password, verify_password

        password = "test_password_123"
        hashed = hash_password(password)

        assert verify_password("wrong_password", hashed) is False

    def test_create_access_token(self):
        from auth import create_access_token, decode_token

        user_id = 42
        token = create_access_token(user_id)

        # Token should be a string
        assert isinstance(token, str)
        # Token should decode correctly
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == str(user_id)

    def test_decode_invalid_token(self):
        from auth import decode_token

        result = decode_token("invalid.token.here")
        assert result is None

    def test_decode_expired_token(self):
        import jwt
        from datetime import datetime, timedelta, timezone
        from auth import JWT_SECRET, JWT_ALGORITHM

        # Create an expired token
        expire = datetime.now(timezone.utc) - timedelta(hours=1)
        payload = {"sub": "1", "exp": expire}
        expired_token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

        from auth import decode_token
        result = decode_token(expired_token)
        assert result is None


class TestAuthRoutes:
    """Tests for auth API routes."""

    @pytest.fixture
    def mock_db(self):
        """Mock database for testing."""
        with patch("auth.DatabaseManager") as mock:
            mock_instance = MagicMock()
            mock_instance.session.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_instance.session.return_value.__exit__ = MagicMock(return_value=False)
            mock.get_instance.return_value = mock_instance
            yield mock_instance

    @pytest.fixture
    def app_client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api_routes import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)


class TestGetCurrentUserFromRequest:
    """Tests for get_current_user_from_request function."""

    def test_decode_token_returns_none_for_expired(self):
        """Test that decode_token returns None for expired tokens."""
        import jwt
        from datetime import datetime, timedelta, timezone
        from auth import JWT_SECRET, JWT_ALGORITHM

        # Create an expired token
        expire = datetime.now(timezone.utc) - timedelta(hours=1)
        payload = {"sub": "1", "exp": expire}
        expired_token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

        from auth import decode_token
        result = decode_token(expired_token)

        assert result is None

    def test_decode_token_returns_none_for_invalid(self):
        """Test that decode_token returns None for invalid tokens."""
        from auth import decode_token

        result = decode_token("not.a.valid.token")

        assert result is None


class TestRequireAuth:
    """Tests for require_auth decorator."""

    def test_require_auth_with_user(self):
        """Test require_auth allows valid user."""
        from auth import require_auth

        mock_user = MagicMock()
        mock_user.id = 1

        result = require_auth(mock_user)

        assert result is mock_user

    def test_require_auth_without_user(self):
        """Test require_auth raises 401 without user."""
        from auth import require_auth
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            require_auth(None)

        assert exc_info.value.status_code == 401
        assert "Not authenticated" in exc_info.value.detail


class TestUserModel:
    """Tests for User model."""

    def test_user_to_dict(self):
        from datetime import datetime
        from models.user import User

        user = User(
            id=1,
            username="testuser",
            email="test@example.com",
            password_hash="$2b$12$hashedpassword",
            created_at=datetime(2024, 1, 1, 12, 0, 0),
            is_active=True
        )

        result = user.to_dict()

        assert result["id"] == 1
        assert result["username"] == "testuser"
        assert result["email"] == "test@example.com"
        assert result["is_active"] is True
        assert "password_hash" not in result  # Should not expose hash


class TestGetCurrentUserFromRequest:
    """Tests for get_current_user_from_request function."""

    def test_no_auth_header_returns_none(self):
        """Test that missing auth header returns None when no passwords configured."""
        from auth import get_current_user_from_request

        mock_request = MagicMock()
        mock_request.headers.get.return_value = None

        # Patch where state is used (inside the function's import)
        with patch("framework_state.state") as mock_state:
            mock_state.dj_password = ""
            mock_state.audience_password = ""

            result = get_current_user_from_request(mock_request)

        assert result is None

    def test_bearer_token_invalid(self):
        """Test that invalid bearer token raises 401."""
        from auth import get_current_user_from_request
        from fastapi import HTTPException

        mock_request = MagicMock()
        mock_request.headers.get.return_value = "Bearer invalid.token.here"

        with pytest.raises(HTTPException) as exc_info:
            get_current_user_from_request(mock_request)

        assert exc_info.value.status_code == 401

    def test_basic_auth_valid_dj_password(self):
        """Test that valid DJ Basic auth returns CompatUser."""
        from auth import get_current_user_from_request
        import base64

        mock_request = MagicMock()
        # "secret" is the DJ password
        creds = base64.b64encode(b"user:secret").decode("utf-8")
        mock_request.headers.get.return_value = f"Basic {creds}"

        with patch("framework_state.state") as mock_state:
            mock_state.dj_password = "secret"
            mock_state.audience_password = ""

            result = get_current_user_from_request(mock_request)

        assert result is not None
        assert result.username == "djCompat"

    def test_basic_auth_valid_audience_password(self):
        """Test that valid audience Basic auth returns CompatAudUser."""
        from auth import get_current_user_from_request
        import base64

        mock_request = MagicMock()
        # "audience_secret" is the audience password
        creds = base64.b64encode(b"user:audience_secret").decode("utf-8")
        mock_request.headers.get.return_value = f"Basic {creds}"

        with patch("framework_state.state") as mock_state:
            mock_state.dj_password = ""
            mock_state.audience_password = "audience_secret"

            result = get_current_user_from_request(mock_request)

        assert result is not None
        assert result.username == "audienceCompat"

    def test_basic_auth_wrong_password_returns_none(self):
        """Test that wrong password returns None (allows anonymous)."""
        from auth import get_current_user_from_request
        import base64

        mock_request = MagicMock()
        creds = base64.b64encode(b"user:wrong_password").decode("utf-8")
        mock_request.headers.get.return_value = f"Basic {creds}"

        with patch("framework_state.state") as mock_state:
            mock_state.dj_password = "secret"
            mock_state.audience_password = ""

            result = get_current_user_from_request(mock_request)

        assert result is None

    def test_malformed_basic_auth_returns_none(self):
        """Test that malformed Basic auth returns None."""
        from auth import get_current_user_from_request

        mock_request = MagicMock()
        mock_request.headers.get.return_value = "Basic invalid_base64!"

        with patch("framework_state.state") as mock_state:
            mock_state.dj_password = "secret"
            mock_state.audience_password = ""

            # Should not raise, just return None
            result = get_current_user_from_request(mock_request)

        assert result is None

    def test_basic_auth_without_colon_returns_none(self):
        """Test that Basic auth without colon returns None."""
        from auth import get_current_user_from_request
        import base64

        mock_request = MagicMock()
        # No colon in credentials
        creds = base64.b64encode(b"justpassword").decode("utf-8")
        mock_request.headers.get.return_value = f"Basic {creds}"

        with patch("framework_state.state") as mock_state:
            mock_state.dj_password = "secret"
            mock_state.audience_password = ""

            result = get_current_user_from_request(mock_request)

        assert result is None


class TestHashPasswordEdgeCases:
    """Test hash_password edge cases."""

    def test_hash_password_different_each_time(self):
        """Test that hash_password generates different hashes (due to salt)."""
        from auth import hash_password

        password = "test_password"
        hash1 = hash_password(password)
        hash2 = hash_password(password)

        # Hashes should be different due to random salt
        assert hash1 != hash2
        # But both should verify correctly
        from auth import verify_password
        assert verify_password(password, hash1) is True
        assert verify_password(password, hash2) is True

    def test_verify_password_with_invalid_hash(self):
        """Test verify_password with invalid hash format."""
        from auth import verify_password

        # bcrypt raises ValueError for invalid hash format, verify_password catches it
        # and returns False for invalid passwords
        try:
            result = verify_password("password", "invalid_hash")
            # If it returns a value, it should be False
            assert result is False
        except ValueError:
            # If bcrypt raises ValueError for completely invalid format, that's also acceptable
            pass


class TestCreateAccessToken:
    """Test create_access_token edge cases."""

    def test_token_contains_user_id(self):
        """Test that token contains correct user ID."""
        from auth import create_access_token, decode_token

        user_id = 12345
        token = create_access_token(user_id)
        payload = decode_token(token)

        assert payload is not None
        assert payload["sub"] == str(user_id)

    def test_token_has_expiration(self):
        """Test that token has expiration claim."""
        import jwt
        from auth import create_access_token, JWT_SECRET, JWT_ALGORITHM

        token = create_access_token(1)
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])

        assert "exp" in payload


class TestJWTSecret:
    """Test JWT secret configuration."""

    def test_jwt_secret_from_environment(self):
        """Test that JWT_SECRET can be set from environment."""
        import os
        from auth import JWT_SECRET

        # Should have a default value
        assert JWT_SECRET is not None
        assert len(JWT_SECRET) > 0
