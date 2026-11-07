from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from recompmanager import passwords
from recompmanager.auth import (
    Principal,
    as_utc,
    create_token,
    require_admin,
    require_token,
    revoke_sessions,
)
from recompmanager.db import get_session
from recompmanager.errors import ApiError, not_found
from recompmanager.models import ApiToken, TokenKind, TokenScope, utcnow

router = APIRouter(prefix="/auth", tags=["auth"])

MIN_PASSWORD = 8


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


class LoginResult(BaseModel):
    token: str = Field(description="Session token. Send it as a bearer token.")
    expires_at: datetime = Field(description="Moves forward each time the session is used")


def _device_name(user_agent: str) -> str:
    ua = user_agent or ""
    browser = next(
        (
            name
            for pat, name in [
                (r"Edg/", "Edge"),
                (r"OPR/|Opera", "Opera"),
                (r"Firefox/", "Firefox"),
                (r"Chrome/|CriOS/", "Chrome"),
                (r"Safari/", "Safari"),
            ]
            if re.search(pat, ua)
        ),
        "Browser",
    )
    os_name = next(
        (
            name
            for pat, name in [
                (r"iPhone|iPad", "iOS"),
                (r"Android", "Android"),
                (r"Windows", "Windows"),
                (r"Mac OS X|Macintosh", "macOS"),
                (r"SteamOS|Linux", "Linux"),
            ]
            if re.search(pat, ua)
        ),
        None,
    )
    return f"{browser} on {os_name}" if os_name else browser


@router.post(
    "/login",
    response_model=LoginResult,
    summary="Sign in with username and password (no auth)",
    description="Returns an admin session token for the web UI. "
    "Repeated failures from one address are locked out for 15 minutes.",
)
def login(
    body: LoginRequest, request: Request, session: Session = Depends(get_session)
) -> LoginResult:
    throttle: passwords.LoginThrottle = request.app.state.login_throttle
    key = request.client.host if request.client else "unknown"
    wait = throttle.retry_after(key)
    if wait is not None:
        raise ApiError(
            429,
            "rate_limited",
            f"Too many failed sign-ins. Try again in {wait // 60 + 1} minutes.",
            {"retry_after_seconds": wait},
        )
    if passwords.load(session) is None:
        raise ApiError(
            409,
            "password_not_set",
            "Password sign-in isn't set up yet. Sign in with an admin token, then set a "
            "password in Settings.",
        )
    if not passwords.check(session, body.username, body.password):
        throttle.fail(key)
        raise ApiError(401, "invalid_credentials", "Wrong username or password")
    throttle.succeed(key)
    row, plaintext = create_token(
        session,
        _device_name(request.headers.get("user-agent", "")),
        TokenScope.admin,
        TokenKind.session,
    )
    return LoginResult(token=plaintext, expires_at=row.expires_at)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="End the current session")
def logout(
    principal: Principal = Depends(require_token), session: Session = Depends(get_session)
) -> None:
    """Revokes the calling session token. API tokens are left alone."""
    if principal.kind == TokenKind.session:
        row = session.get(ApiToken, principal.token_id)
        if row is not None and row.revoked_at is None:
            row.revoked_at = utcnow()


class Me(BaseModel):
    token_id: int
    token_name: str
    scope: TokenScope
    kind: TokenKind
    username: str | None = Field(description="Web login username, if a password is set")
    password_set: bool


@router.get("/me", response_model=Me, summary="Describe the caller")
def get_me(
    principal: Principal = Depends(require_token), session: Session = Depends(get_session)
) -> Me:
    creds = passwords.load(session) if principal.is_admin else None
    return Me(
        token_id=principal.token_id,
        token_name=principal.name,
        scope=principal.scope,
        kind=principal.kind,
        username=creds.username if creds else None,
        password_set=creds is not None,
    )


class PasswordChange(BaseModel):
    username: str = Field(min_length=1, max_length=100, pattern=r"^\S(.*\S)?$")
    new_password: str = Field(min_length=MIN_PASSWORD, max_length=1024)
    current_password: str | None = Field(
        default=None,
        description="Required when changing an existing password from a session. "
        "Not needed with an admin API token, which is the recovery path.",
    )


@router.put(
    "/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set or change the web login",
    description="Signs out every other session.",
)
def set_password(
    body: PasswordChange,
    principal: Principal = Depends(require_admin),
    session: Session = Depends(get_session),
) -> None:
    existing = passwords.load(session)
    needs_current = existing is not None and principal.kind == TokenKind.session
    if needs_current and not (
        body.current_password
        and passwords.verify_password(body.current_password, existing.password_hash)
    ):
        raise ApiError(403, "wrong_password", "Current password is wrong")
    passwords.save(session, body.username, body.new_password)
    keep = principal.token_id if principal.kind == TokenKind.session else None
    revoke_sessions(session, except_id=keep)


class SessionOut(BaseModel):
    id: int
    name: str = Field(description="Browser and OS, from the user agent at sign-in")
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime
    current: bool = Field(description="This is the session making the request")


@router.get("/sessions", response_model=list[SessionOut], summary="List signed-in browsers")
def list_sessions(
    principal: Principal = Depends(require_admin), session: Session = Depends(get_session)
) -> list[SessionOut]:
    now = utcnow()
    rows = session.scalars(
        select(ApiToken)
        .where(ApiToken.kind == TokenKind.session, ApiToken.revoked_at.is_(None))
        .order_by(ApiToken.last_used_at.desc())
    )
    out = []
    for r in rows:
        expires = as_utc(r.expires_at)
        if expires is not None and expires > now:
            out.append(
                SessionOut(
                    id=r.id,
                    name=r.name,
                    created_at=as_utc(r.created_at),
                    last_used_at=as_utc(r.last_used_at),
                    expires_at=expires,
                    current=r.id == principal.token_id,
                )
            )
    return out


@router.delete(
    "/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Sign out a browser"
)
def revoke_session(
    session_id: int,
    _p: Principal = Depends(require_admin),
    session: Session = Depends(get_session),
) -> None:
    row = session.get(ApiToken, session_id)
    if row is None or row.kind != TokenKind.session:
        raise not_found("Session")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
