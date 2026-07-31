from fastapi import APIRouter

from .auth import router as auth_router
from .shows import router as shows_router
from .jobs import router as jobs_router
from .models import router as models_router
from .config import router as config_router
from .stems import router as stems_router
from .reasoning_logs import router as reasoning_logs_router

# Export utilities for tests
from .utils import require_show_owner, generate_audience_password

api_router = APIRouter(prefix="/api")

api_router.include_router(auth_router)
api_router.include_router(shows_router)
api_router.include_router(jobs_router)
api_router.include_router(models_router)
api_router.include_router(config_router)
api_router.include_router(stems_router)
api_router.include_router(reasoning_logs_router)
