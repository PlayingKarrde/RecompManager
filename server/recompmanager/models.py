"""ORM models. Recipes (YAML) define games; the database holds state."""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from recompmanager.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class TokenScope(enum.StrEnum):
    read = "read"
    admin = "admin"


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    scope: Mapped[TokenScope] = mapped_column(Enum(TokenScope, native_enum=False, length=16))
    # sha256 of the full token. The plaintext is shown once at creation, never stored.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # First characters of the token, so the UI can tell tokens apart.
    prefix: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
