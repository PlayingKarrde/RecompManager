from fastapi import APIRouter

from recompmanager.api import auth, events, games, inputs, jobs, recomps, system, tokens
from recompmanager.errors import ERROR_RESPONSES

api_router = APIRouter(prefix="/api/v1", responses=ERROR_RESPONSES)
api_router.include_router(system.router)
api_router.include_router(auth.router)
api_router.include_router(tokens.router)
api_router.include_router(games.router)
api_router.include_router(recomps.router)
api_router.include_router(inputs.router)
api_router.include_router(jobs.router)
api_router.include_router(events.router)
