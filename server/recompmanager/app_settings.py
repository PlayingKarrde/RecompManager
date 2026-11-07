"""User-editable settings, stored as one JSON document in the ``settings`` table."""

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from recompmanager.models import Setting

_KEY = "app"


class AppSettings(BaseModel):
    """Stored form. ``github_token`` is never returned by the API."""

    github_token: str | None = None
    update_check_interval_hours: int = Field(default=6, ge=1, le=24 * 7)
    keep_versions: int = Field(default=2, ge=1, le=50)
    auto_update_default: bool = True
    rom_scan_interval_hours: int = Field(default=24, ge=0, le=24 * 30)  # 0 = only on demand
    auto_accept_scan_matches: bool = True  # hash/ID-verified matches are added by reference
    auto_build: bool = True  # start server builds/extractions when a version or input arrives


def load(session: Session) -> AppSettings:
    row = session.get(Setting, _KEY)
    return AppSettings.model_validate(row.value) if row else AppSettings()


def save(session: Session, settings: AppSettings) -> None:
    row = session.get(Setting, _KEY)
    data = settings.model_dump(mode="json")
    if row is None:
        session.add(Setting(key=_KEY, value=data))
    else:
        row.value = data
