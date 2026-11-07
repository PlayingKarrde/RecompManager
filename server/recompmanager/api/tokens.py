from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from recompmanager.auth import Principal, create_token, require_admin
from recompmanager.db import get_session
from recompmanager.errors import ApiError, not_found
from recompmanager.models import ApiToken, TokenScope, utcnow

router = APIRouter(prefix="/tokens", tags=["tokens"])


class TokenOut(BaseModel):
    id: int
    name: str
    scope: TokenScope
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None

    model_config = {"from_attributes": True}


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scope: TokenScope = TokenScope.read


class TokenCreated(TokenOut):
    token: str = Field(description="The token itself. Shown only once.")


@router.get("", response_model=list[TokenOut], summary="List API tokens")
def list_tokens(
    session: Session = Depends(get_session), _p: Principal = Depends(require_admin)
) -> list[ApiToken]:
    return list(session.scalars(select(ApiToken).order_by(ApiToken.id)))


@router.post(
    "",
    response_model=TokenCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create an API token",
)
def create_api_token(
    body: TokenCreate,
    session: Session = Depends(get_session),
    _p: Principal = Depends(require_admin),
) -> TokenCreated:
    row, plaintext = create_token(session, body.name.strip(), body.scope)
    return TokenCreated(**TokenOut.model_validate(row).model_dump(), token=plaintext)


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Revoke an API token")
def revoke_token(
    token_id: int,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> None:
    row = session.get(ApiToken, token_id)
    if row is None:
        raise not_found("Token")
    if row.id == principal.token_id:
        raise ApiError(409, "conflict", "You can't revoke the token you are using")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
