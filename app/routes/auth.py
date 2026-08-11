from fastapi import APIRouter, HTTPException, Request, status

from app.auth import create_access_token, get_current_user_from_request, hash_password, verify_password
from app.db import DatabaseManager
from app.models import User

from .schemas import UserLogin, UserRegister

router = APIRouter()


@router.post("/auth/register", status_code=status.HTTP_201_CREATED)
async def register(user_data: UserRegister):
    """Create a new user account."""
    db_manager = DatabaseManager.get_instance()

    with db_manager.session() as session:
        # Check if username exists
        existing = session.query(User).filter(User.username == user_data.username).first()
        if existing:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username already taken")

        # Check if email exists
        existing_email = session.query(User).filter(User.email == user_data.email).first()
        if existing_email:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered")

        # Create user
        user = User(username=user_data.username, email=user_data.email, password_hash=hash_password(user_data.password))
        session.add(user)
        session.flush()

        token = create_access_token(user.id)

        return {"id": user.id, "username": user.username, "email": user.email, "token": token}


@router.post("/auth/login")
async def login(user_data: UserLogin):
    """Login and get JWT token."""
    db_manager = DatabaseManager.get_instance()

    with db_manager.session() as session:
        user = session.query(User).filter(User.username == user_data.username).first()

        if not user or not verify_password(user_data.password, user.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

        if not user.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")

        token = create_access_token(user.id)

        return {"id": user.id, "username": user.username, "email": user.email, "token": token}


@router.get("/auth/me")
async def get_me(request: Request):
    """Get current user info."""
    user = get_current_user_from_request(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user.to_dict()
