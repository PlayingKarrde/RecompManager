"""Library: games, versions, artifacts, jobs, HTTP cache.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "games",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("current_version", sa.String(200)),
        sa.Column("latest_version", sa.String(200)),
        sa.Column("last_checked_at", TS),
        sa.Column("last_check_error", sa.Text()),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_table(
        "versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "game_id", sa.String(64), sa.ForeignKey("games.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("tag", sa.String(200), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("published_at", TS),
        sa.Column("prerelease", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("url", sa.String(500)),
        sa.Column("pinned", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("downloaded_at", TS),
        sa.Column("assets", sa.JSON(), nullable=False),
        sa.UniqueConstraint("game_id", "tag"),
    )
    op.create_index("ix_versions_game_id", "versions", ["game_id"])
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "version_id",
            sa.Integer(),
            sa.ForeignKey("versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("platforms", sa.JSON(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64)),
        sa.Column("upstream_digest", sa.String(100)),
        sa.Column("source_url", sa.String(1000), nullable=False),
        sa.Column("rel_path", sa.String(1000), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("downloaded_at", TS),
        sa.UniqueConstraint("version_id", "name"),
    )
    op.create_index("ix_artifacts_version_id", "artifacts", ["version_id"])
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("game_id", sa.String(64), sa.ForeignKey("games.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("progress", sa.Float()),
        sa.Column("message", sa.String(500)),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("error", sa.Text()),
        sa.Column("log", sa.Text(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("started_at", TS),
        sa.Column("finished_at", TS),
    )
    op.create_index("ix_jobs_kind", "jobs", ["kind"])
    op.create_index("ix_jobs_game_id", "jobs", ["game_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_table(
        "http_cache",
        sa.Column("url", sa.String(1000), primary_key=True),
        sa.Column("etag", sa.String(200)),
        sa.Column("body", sa.JSON()),
        sa.Column("fetched_at", TS, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("http_cache")
    op.drop_table("jobs")
    op.drop_table("artifacts")
    op.drop_table("versions")
    op.drop_table("games")
