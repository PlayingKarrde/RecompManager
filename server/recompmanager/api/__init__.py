from fastapi import APIRouter

from recompmanager.api import auth, system, tokens
from recompmanager.errors import ERROR_RESPONSES

api_router = APIRouter(prefix="/api/v1", responses=ERROR_RESPONSES)
api_router.include_router(system.router)
api_router.include_router(auth.router)
api_router.include_router(tokens.router)
