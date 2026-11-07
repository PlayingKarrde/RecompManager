"""ORM models. Recipes (YAML) define games; the database holds state."""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from recompmanager.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores UTC; always returns timezone-aware datetimes (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


def _enum(e: type[enum.Enum]) -> Enum:
    return Enum(e, native_enum=False, length=16, validate_strings=True)


# ---------------------------------------------------------------- auth


class TokenScope(enum.StrEnum):
    read = "read"
    admin = "admin"


class TokenKind(enum.StrEnum):
    api = "api"  # created by hand for a client or automation; never expires
    session = "session"  # created by a password login in a browser; expires when unused


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    scope: Mapped[TokenScope] = mapped_column(_enum(TokenScope))
    kind: Mapped[TokenKind] = mapped_column(_enum(TokenKind), default=TokenKind.api)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # sha256 of the full token. The plaintext is shown once at creation, never stored.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # First characters of the token, so the UI can tell tokens apart.
    prefix: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


# ---------------------------------------------------------------- library


class GameStatus(enum.StrEnum):
    draft = "draft"  # added by repo URL; asset rules guessed, waiting for confirmation
    active = "active"


class Game(Base):
    """A game in the library. Its definition is the recipe YAML in /config/recipes/<id>.yaml."""

    __tablename__ = "games"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[GameStatus] = mapped_column(_enum(GameStatus), default=GameStatus.draft)
    # The version clients get by default. Rollback = point this at an older version.
    current_version: Mapped[str | None] = mapped_column(String(200))
    # Newest release on the recipe's channel, as of the last check.
    latest_version: Mapped[str | None] = mapped_column(String(200))
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_check_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    versions: Mapped[list[Version]] = relationship(
        back_populates="game", cascade="all, delete-orphan", passive_deletes=True
    )


class VersionState(enum.StrEnum):
    available = "available"  # known from the source, nothing downloaded
    downloading = "downloading"
    downloaded = "downloaded"
    failed = "failed"
    pruned = "pruned"  # was downloaded, files removed to save space


class Version(Base):
    __tablename__ = "versions"
    __table_args__ = (UniqueConstraint("game_id", "tag"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    tag: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(300), default="")
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    prerelease: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")  # release notes (markdown)
    url: Mapped[str | None] = mapped_column(String(500))
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[VersionState] = mapped_column(_enum(VersionState), default=VersionState.available)
    downloaded_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Upstream assets as last seen: [{id, name, size, digest, url}]
    assets: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    game: Mapped[Game] = relationship(back_populates="versions")
    artifacts: Mapped[list[Artifact]] = relationship(
        back_populates="version", cascade="all, delete-orphan", passive_deletes=True
    )


class ArtifactState(enum.StrEnum):
    pending = "pending"
    downloaded = "downloaded"
    failed = "failed"
    deleted = "deleted"


class Artifact(Base):
    """One stored release file. One file can serve several platforms (e.g. a source zip)."""

    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("version_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("versions.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(300))  # original asset name
    platforms: Mapped[list[str]] = mapped_column(JSON, default=list)
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64))
    upstream_digest: Mapped[str | None] = mapped_column(String(100))
    source_url: Mapped[str] = mapped_column(String(1000))
    rel_path: Mapped[str] = mapped_column(String(1000))  # relative to the data dir
    state: Mapped[ArtifactState] = mapped_column(
        _enum(ArtifactState), default=ArtifactState.pending
    )
    downloaded_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    version: Mapped[Version] = relationship(back_populates="artifacts")


# ---------------------------------------------------------------- jobs


class JobStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"

    @property
    def finished(self) -> bool:
        return self in (JobStatus.succeeded, JobStatus.failed, JobStatus.cancelled)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(50), index=True)
    game_id: Mapped[str | None] = mapped_column(
        ForeignKey("games.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus), default=JobStatus.queued, index=True
    )
    progress: Mapped[float | None] = mapped_column(Float)  # 0..1, None = indeterminate
    message: Mapped[str | None] = mapped_column(String(500))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    log: Mapped[str] = mapped_column(Text, default="")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


# ---------------------------------------------------------------- http cache


class HttpCache(Base):
    """Last response per URL, for conditional requests (ETag / If-None-Match)."""

    __tablename__ = "http_cache"

    url: Mapped[str] = mapped_column(String(1000), primary_key=True)
    etag: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[Any] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
