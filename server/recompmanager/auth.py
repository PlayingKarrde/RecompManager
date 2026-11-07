"""Bearer-token auth with two scopes: ``read`` (clients) and ``admin`` (UI, automation)."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from recompmanager.db import get_session
from recompmanager.errors import ApiError
from recompmanager.models import ApiToken, TokenScope, utcnow

TOKEN_PREFIX = "rm_"
_LAST_USED_RESOLUTION = timedelta(minutes=1)

bearer = HTTPBearer(auto_error=False, description="API token created in the web UI")


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(session: Session, name: str, scope: TokenScope) -> tuple[ApiToken, str]:
    """Create a token. Returns the row and the plaintext, which is never stored."""
    plaintext = generate_token()
    row = ApiToken(name=name, scope=scope, token_hash=hash_token(plaintext), prefix=plaintext[:10])
    session.add(row)
    session.flush()
    return row, plaintext


@dataclass(frozen=True)
class Principal:
    token_id: int
    name: str
    scope: TokenScope

    @property
    def is_admin(self) -> bool:
        return self.scope == TokenScope.admin


def _unauthorized(message: str) -> ApiError:
    err = ApiError(401, "unauthorized", message)
    return err


def require_token(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: Session = Depends(get_session),
) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise _unauthorized("Missing bearer token")
    row = session.scalar(
        select(ApiToken).where(ApiToken.token_hash == hash_token(creds.credentials))
    )
    if row is None or row.revoked_at is not None:
        raise _unauthorized("Invalid or revoked token")
    now = utcnow()
    last = row.last_used_at
    if last is not None and last.tzinfo is None:  # SQLite drops tzinfo
        last = last.replace(tzinfo=now.tzinfo)
    if last is None or now - last > _LAST_USED_RESOLUTION:
        row.last_used_at = now
    return Principal(token_id=row.id, name=row.name, scope=row.scope)


def require_admin(principal: Principal = Depends(require_token)) -> Principal:
    if not principal.is_admin:
        raise ApiError(403, "forbidden", "This action needs an admin token")
    return principal
