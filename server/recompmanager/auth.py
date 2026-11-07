"""Bearer-token auth.

Two kinds of token, both sent as ``Authorization: Bearer <token>``:
- ``api`` tokens, made by hand for clients (scope ``read``) or automation (``admin``).
  They never expire.
- ``session`` tokens, made by a password login in a browser. Always ``admin``. They
  expire after ``SESSION_IDLE`` without use.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from recompmanager.db import get_session
from recompmanager.errors import ApiError
from recompmanager.models import ApiToken, TokenKind, TokenScope, utcnow

TOKEN_PREFIX = "rm_"
SESSION_PREFIX = "rms_"
SESSION_IDLE = timedelta(days=30)
_LAST_USED_RESOLUTION = timedelta(minutes=1)

bearer = HTTPBearer(
    auto_error=False, description="API token, or a session token from POST /auth/login"
)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def as_utc(dt: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes; everything we store is UTC.
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=utcnow().tzinfo)
    return dt


def create_token(
    session: Session, name: str, scope: TokenScope, kind: TokenKind = TokenKind.api
) -> tuple[ApiToken, str]:
    """Create a token. Returns the row and the plaintext, which is never stored."""
    prefix = SESSION_PREFIX if kind == TokenKind.session else TOKEN_PREFIX
    plaintext = prefix + secrets.token_urlsafe(32)
    row = ApiToken(
        name=name,
        scope=scope,
        kind=kind,
        token_hash=hash_token(plaintext),
        prefix=plaintext[:10],
        expires_at=utcnow() + SESSION_IDLE if kind == TokenKind.session else None,
    )
    session.add(row)
    session.flush()
    return row, plaintext


def revoke_sessions(session: Session, except_id: int | None = None) -> None:
    now = utcnow()
    rows = session.scalars(
        select(ApiToken).where(ApiToken.kind == TokenKind.session, ApiToken.revoked_at.is_(None))
    )
    for row in rows:
        if row.id != except_id:
            row.revoked_at = now


@dataclass(frozen=True)
class Principal:
    token_id: int
    name: str
    scope: TokenScope
    kind: TokenKind

    @property
    def is_admin(self) -> bool:
        return self.scope == TokenScope.admin


def _unauthorized(message: str) -> ApiError:
    return ApiError(401, "unauthorized", message)


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
    expires = as_utc(row.expires_at)
    if expires is not None and expires <= now:
        raise _unauthorized("Session expired. Sign in again.")
    last = as_utc(row.last_used_at)
    if last is None or now - last > _LAST_USED_RESOLUTION:
        row.last_used_at = now
        if row.kind == TokenKind.session:
            row.expires_at = now + SESSION_IDLE  # sliding expiry
    return Principal(token_id=row.id, name=row.name, scope=row.scope, kind=row.kind)


def require_admin(principal: Principal = Depends(require_token)) -> Principal:
    if not principal.is_admin:
        raise ApiError(403, "forbidden", "This action needs an admin token")
    return principal
