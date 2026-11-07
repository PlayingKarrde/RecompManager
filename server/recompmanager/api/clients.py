"""Install plans and client devices."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from recompmanager.auth import Principal, require_admin, require_token
from recompmanager.db import get_session
from recompmanager.errors import ApiError, not_found
from recompmanager.models import Client, Recomp, utcnow
from recompmanager.plan import InstallPlan, build_plan
from recompmanager.platforms import is_platform
from recompmanager.recipes.store import RecipeError
from recompmanager.services import Services, get_services

router = APIRouter(tags=["clients"])


@router.get(
    "/recomps/{recomp_id}/install-plan",
    response_model=InstallPlan,
    summary="What a client must do to install a recomp",
    description="Ordered steps of a few fixed types (download_artifact, download_input, "
    "write_file, chmod, run, instructions) plus launch and save metadata. Placeholders "
    "{install}, {data} and {home} are for the client to fill in. Ask for the most specific "
    "platform key (e.g. linux-x64-steamdeck); the server falls back as needed.",
    tags=["recomps"],
)
def get_install_plan(
    recomp_id: str,
    platform: str = Query(description="e.g. windows-x64, linux-x64-steamdeck, macos-arm64"),
    version: str | None = Query(default=None, description="Default: the current version"),
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
) -> InstallPlan:
    if not is_platform(platform):
        raise ApiError(422, "invalid_platform", f"Unknown platform key {platform!r}")
    recomp = session.get(Recomp, recomp_id)
    if recomp is None:
        raise not_found("Recomp")
    try:
        recipe = svc.recipes.load(recomp_id)
    except RecipeError as e:
        raise ApiError(409, "recipe_error", str(e)) from e
    return build_plan(session, svc.env, recomp, recipe, platform, version)


# ---------------------------------------------------------------- clients


class ClientRegister(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="e.g. Steam Deck")
    platform: str = Field(description="The device's most specific platform key")


class InstalledReport(BaseModel):
    installed: dict[str, str] = Field(description="recomp id → installed version tag")


class ClientOut(BaseModel):
    id: int
    name: str
    platform: str
    installed: dict[str, str]
    out_of_date: list[str] = Field(description="Recomps whose current version differs")
    created_at: datetime
    last_seen_at: datetime


def _out(session: Session, c: Client) -> ClientOut:
    stale = []
    for rid, tag in (c.installed or {}).items():
        r = session.get(Recomp, rid)
        if r is not None and r.current_version and r.current_version != tag:
            stale.append(rid)
    return ClientOut(
        id=c.id,
        name=c.name,
        platform=c.platform,
        installed=c.installed or {},
        out_of_date=sorted(stale),
        created_at=c.created_at,
        last_seen_at=c.last_seen_at,
    )


@router.post(
    "/clients/register",
    response_model=ClientOut,
    summary="Register this device (idempotent per token and name)",
)
def register_client(
    body: ClientRegister,
    session: Session = Depends(get_session),
    p: Principal = Depends(require_token),
) -> ClientOut:
    if not is_platform(body.platform):
        raise ApiError(422, "invalid_platform", f"Unknown platform key {body.platform!r}")
    c = session.scalar(
        select(Client).where(Client.token_id == p.token_id, Client.name == body.name)
    )
    if c is None:
        c = Client(name=body.name, platform=body.platform, token_id=p.token_id, installed={})
        session.add(c)
    c.platform = body.platform
    c.last_seen_at = utcnow()
    session.commit()
    return _out(session, c)


def _client(session: Session, client_id: int, p: Principal) -> Client:
    c = session.get(Client, client_id)
    if c is None or (c.token_id != p.token_id and not p.is_admin):
        raise not_found("Client")
    return c


@router.put(
    "/clients/{client_id}/installed",
    response_model=ClientOut,
    summary="Report what this device has installed",
)
def report_installed(
    client_id: int,
    body: InstalledReport,
    session: Session = Depends(get_session),
    p: Principal = Depends(require_token),
) -> ClientOut:
    c = _client(session, client_id, p)
    c.installed = dict(body.installed)
    c.last_seen_at = utcnow()
    session.commit()
    return _out(session, c)


@router.get("/clients", response_model=list[ClientOut], summary="List devices")
def list_clients(
    session: Session = Depends(get_session), _p: Principal = Depends(require_admin)
) -> list[ClientOut]:
    return [_out(session, c) for c in session.scalars(select(Client).order_by(Client.name))]


@router.delete(
    "/clients/{client_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Forget a device"
)
def delete_client(
    client_id: int,
    session: Session = Depends(get_session),
    p: Principal = Depends(require_admin),
) -> None:
    session.delete(_client(session, client_id, p))
    session.commit()
