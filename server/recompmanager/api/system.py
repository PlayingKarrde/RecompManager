from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from recompmanager import __version__, app_settings
from recompmanager.auth import Principal, require_admin, require_token
from recompmanager.config import Env
from recompmanager.db import get_session

router = APIRouter(prefix="/system", tags=["system"])


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    version: str


@router.get("/health", response_model=Health, summary="Liveness check (no auth)")
def get_health(session: Session = Depends(get_session)) -> Health:
    try:
        session.execute(text("SELECT 1"))
        status: Literal["ok", "degraded"] = "ok"
    except Exception:
        status = "degraded"
    return Health(status=status, version=__version__)


class SystemInfo(BaseModel):
    version: str
    docker_available: bool = Field(description="Docker socket is mounted; server builds possible")
    builds_disabled_reason: str | None
    host_data_path_set: bool
    import_dir_available: bool
    roms_dir_available: bool = Field(description="A ROM library is mounted at /roms")


@router.get("/info", response_model=SystemInfo, summary="Server capabilities")
def get_system_info(request: Request, _p: Principal = Depends(require_token)) -> SystemInfo:
    env: Env = request.app.state.env
    svc = getattr(request.app.state, "services", None)
    reason = svc.builder.unavailable() if svc is not None else "the server is starting"
    return SystemInfo(
        version=__version__,
        docker_available=env.docker_available,
        builds_disabled_reason=reason,
        host_data_path_set=bool(env.host_data_path),
        import_dir_available=env.import_dir.is_dir(),
        roms_dir_available=env.roms_dir.is_dir(),
    )


class SettingsOut(BaseModel):
    update_check_interval_hours: int
    keep_versions: int
    auto_update_default: bool
    github_token_set: bool
    rom_scan_interval_hours: int = Field(description="0 = scan only when asked")
    auto_accept_scan_matches: bool
    auto_build: bool = Field(description="Start server builds when a version or input arrives")


class SettingsPatch(BaseModel):
    update_check_interval_hours: int | None = Field(default=None, ge=1, le=24 * 7)
    keep_versions: int | None = Field(default=None, ge=1, le=50)
    auto_update_default: bool | None = None
    rom_scan_interval_hours: int | None = Field(default=None, ge=0, le=24 * 30)
    auto_accept_scan_matches: bool | None = None
    auto_build: bool | None = None
    github_token: str | None = Field(
        default=None, description="Set to a token, or to an empty string to clear it"
    )


def _out(s: app_settings.AppSettings) -> SettingsOut:
    return SettingsOut(
        update_check_interval_hours=s.update_check_interval_hours,
        keep_versions=s.keep_versions,
        auto_update_default=s.auto_update_default,
        github_token_set=bool(s.github_token),
        rom_scan_interval_hours=s.rom_scan_interval_hours,
        auto_accept_scan_matches=s.auto_accept_scan_matches,
        auto_build=s.auto_build,
    )


@router.get("/settings", response_model=SettingsOut, summary="Read settings")
def get_settings(
    session: Session = Depends(get_session), _p: Principal = Depends(require_admin)
) -> SettingsOut:
    return _out(app_settings.load(session))


@router.patch("/settings", response_model=SettingsOut, summary="Update settings")
def update_settings(
    patch: SettingsPatch,
    request: Request,
    session: Session = Depends(get_session),
    _p: Principal = Depends(require_admin),
) -> SettingsOut:
    current = app_settings.load(session)
    changes = patch.model_dump(exclude_unset=True)
    if "github_token" in changes:
        changes["github_token"] = (changes["github_token"] or "").strip() or None
    updated = current.model_copy(update=changes)
    app_settings.AppSettings.model_validate(updated.model_dump())
    app_settings.save(session, updated)
    sched = getattr(getattr(request.app.state, "services", None), "scheduler", None)
    if sched and updated.update_check_interval_hours != current.update_check_interval_hours:
        sched.reschedule(updated.update_check_interval_hours)
    if sched and updated.rom_scan_interval_hours != current.rom_scan_interval_hours:
        sched.reschedule_scan(updated.rom_scan_interval_hours)
    return _out(updated)
