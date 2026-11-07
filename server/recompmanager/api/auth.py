from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from recompmanager.auth import Principal, require_token
from recompmanager.models import TokenScope

router = APIRouter(prefix="/auth", tags=["auth"])


class Me(BaseModel):
    token_id: int
    token_name: str
    scope: TokenScope


@router.get("/me", response_model=Me, summary="Describe the calling token")
def get_me(principal: Principal = Depends(require_token)) -> Me:
    return Me(token_id=principal.token_id, token_name=principal.name, scope=principal.scope)
