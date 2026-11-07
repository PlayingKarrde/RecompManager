"""ORM models. Recipes (YAML) define recomps; the database holds state."""

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
#
# Game (the original title, e.g. Ocarina of Time) → Recomp (a project that runs it,
# e.g. Ship of Harkinian) → Version → Artifact (one stored file per platform).
# Inputs (the user's dumps) belong to the game, so every recomp of it can use them.


class Game(Base):
    __tablename__ = "games"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    system: Mapped[str | None] = mapped_column(String(50))  # n64, arcade, ps4, switch, xbox…
    # Exact identifiers: {"ps4_title_id": "CUSA03173", "switch_program_id": "0100…"}
    identifiers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    recomps: Mapped[list[Recomp]] = relationship(back_populates="game")
    inputs: Mapped[list[Input]] = relationship(
        back_populates="game", cascade="all, delete-orphan", passive_deletes=True
    )


class RecompStatus(enum.StrEnum):
    draft = "draft"  # added by repo URL; asset rules guessed, waiting for confirmation
    active = "active"


class Recomp(Base):
    """A recomp/port project. Its definition is the recipe YAML in /config/recipes/<id>.yaml."""

    __tablename__ = "recomps"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    game_id: Mapped[str | None] = mapped_column(
        ForeignKey("games.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[RecompStatus] = mapped_column(_enum(RecompStatus), default=RecompStatus.draft)
    # The version clients get by default. Rollback = point this at an older version.
    current_version: Mapped[str | None] = mapped_column(String(200))
    # Newest release on the recipe's channel, as of the last check.
    latest_version: Mapped[str | None] = mapped_column(String(200))
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_check_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    game: Mapped[Game | None] = relationship(back_populates="recomps")
    versions: Mapped[list[Version]] = relationship(
        back_populates="recomp", cascade="all, delete-orphan", passive_deletes=True
    )


class VersionState(enum.StrEnum):
    available = "available"  # known from the source, nothing downloaded
    downloading = "downloading"
    downloaded = "downloaded"
    failed = "failed"
    pruned = "pruned"  # was downloaded, files removed to save space


class Version(Base):
    __tablename__ = "versions"
    __table_args__ = (UniqueConstraint("recomp_id", "tag"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    recomp_id: Mapped[str] = mapped_column(ForeignKey("recomps.id", ondelete="CASCADE"), index=True)
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

    recomp: Mapped[Recomp] = relationship(back_populates="versions")
    artifacts: Mapped[list[Artifact]] = relationship(
        back_populates="version", cascade="all, delete-orphan", passive_deletes=True
    )


class ArtifactState(enum.StrEnum):
    pending = "pending"
    downloaded = "downloaded"
    failed = "failed"
    deleted = "deleted"


class Artifact(Base):
    """A release file stored for one platform.

    Stored at library/<game>/<recomp>/<platform>/<version>/<asset>. A file that
    serves several platforms has one row (and one hardlink or copy) per platform.
    """

    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("version_id", "platform", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("versions.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(300))  # original asset name
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


# ---------------------------------------------------------------- inputs


class InputKind(enum.StrEnum):
    file = "file"
    folder = "folder"


class InputStorage(enum.StrEnum):
    vault = "vault"  # copied into /data/inputs
    reference = "reference"  # left where it is (under /roms or /import), read in place


class InputState(enum.StrEnum):
    pending = "pending"  # being copied, extracted or hashed
    ready = "ready"
    missing = "missing"  # a referenced file or folder is gone
    failed = "failed"


class Input(Base):
    """One of the user's game dumps: a file (ROM, ISO, MAME set) or a folder."""

    __tablename__ = "inputs"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    kind: Mapped[InputKind] = mapped_column(_enum(InputKind))
    storage: Mapped[InputStorage] = mapped_column(_enum(InputStorage))
    # vault: relative to the data dir; reference: absolute path inside the container
    path: Mapped[str] = mapped_column(String(2000))
    state: Mapped[InputState] = mapped_column(_enum(InputState), default=InputState.pending)
    error: Mapped[str | None] = mapped_column(Text)
    size: Mapped[int] = mapped_column(Integer, default=0)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    sha1: Mapped[str | None] = mapped_column(String(40))
    md5: Mapped[str | None] = mapped_column(String(32))
    crc32: Mapped[str | None] = mapped_column(String(8))
    sha256: Mapped[str | None] = mapped_column(String(64))
    # What we learned: {"param_sfo": {...}, "npdm": {...}, "zip_entries": {...}, ...}
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(20))  # upload | import | scan
    # Size/mtime summary at the last hash, to know when to hash again
    fingerprint: Mapped[str | None] = mapped_column(String(100))
    hashed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    game: Mapped[Game] = relationship(back_populates="inputs")


class InputFile(Base):
    """Per-file hash cache and download manifest for folder inputs."""

    __tablename__ = "input_files"

    input_id: Mapped[int] = mapped_column(
        ForeignKey("inputs.id", ondelete="CASCADE"), primary_key=True
    )
    path: Mapped[str] = mapped_column(String(2000), primary_key=True)  # relative, "/"-separated
    size: Mapped[int] = mapped_column(Integer)
    mtime: Mapped[float] = mapped_column(Float)
    sha256: Mapped[str | None] = mapped_column(String(64))


class Upload(Base):
    """A resumable browser upload in progress."""

    __tablename__ = "uploads"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(300))
    size: Mapped[int] = mapped_column(Integer)
    received: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# ---------------------------------------------------------------- ROM library scan


class ScanFile(Base):
    """What the scanner learned about a file or folder, reused until it changes."""

    __tablename__ = "scan_files"

    path: Mapped[str] = mapped_column(String(2000), primary_key=True)
    size: Mapped[int] = mapped_column(Integer)
    mtime: Mapped[float] = mapped_column(Float)
    info: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    scanned_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class CandidateState(enum.StrEnum):
    new = "new"
    accepted = "accepted"
    dismissed = "dismissed"


class ScanCandidate(Base):
    """A file or folder in the ROM library that looks like a game's input."""

    __tablename__ = "scan_candidates"
    __table_args__ = (UniqueConstraint("game_id", "path", "member"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[str] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(2000))
    member: Mapped[str] = mapped_column(String(1000), default="")  # entry inside an archive
    kind: Mapped[str] = mapped_column(String(20))  # file | folder | archive_member | package
    # verified: hash or title ID matches; likely: name/structure matches; unusable: e.g. encrypted
    confidence: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(500))
    size: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[CandidateState] = mapped_column(_enum(CandidateState), default=CandidateState.new)
    input_id: Mapped[int | None] = mapped_column(ForeignKey("inputs.id", ondelete="SET NULL"))
    found_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# ---------------------------------------------------------------- clients


class Client(Base):
    """A device running a client (Steam Deck, a PC…) and what it has installed."""

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    platform: Mapped[str] = mapped_column(String(50))
    token_id: Mapped[int | None] = mapped_column(ForeignKey("api_tokens.id", ondelete="SET NULL"))
    installed: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)  # {recomp_id: tag}
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


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
    recomp_id: Mapped[str | None] = mapped_column(
        ForeignKey("recomps.id", ondelete="SET NULL"), index=True
    )
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
