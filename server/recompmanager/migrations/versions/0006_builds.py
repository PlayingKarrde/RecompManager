"""Server-side builds and extractions.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "builds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "recomp_id",
            sa.String(64),
            sa.ForeignKey("recomps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_tag", sa.String(200), nullable=False),
        sa.Column("platform", sa.String(50), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="SET NULL")),
        sa.Column("input_ids", sa.JSON(), nullable=False),
        sa.Column("auto", sa.Boolean(), nullable=False),
        sa.Column("image", sa.String(200)),
        sa.Column("error", sa.Text()),
        sa.Column("log_rel", sa.String(1000)),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("started_at", TS),
        sa.Column("finished_at", TS),
    )
    op.create_index("ix_builds_recomp_id", "builds", ["recomp_id"])
    with op.batch_alter_table("artifacts") as b:
        b.add_column(sa.Column("origin", sa.String(10), nullable=False, server_default="release"))
        b.add_column(sa.Column("build_id", sa.Integer()))
        b.create_foreign_key(
            "fk_artifacts_build", "builds", ["build_id"], ["id"], ondelete="CASCADE"
        )


def downgrade() -> None:
    with op.batch_alter_table("artifacts") as b:
        b.drop_constraint("fk_artifacts_build", type_="foreignkey")
        b.drop_column("build_id")
        b.drop_column("origin")
    op.drop_index("ix_builds_recomp_id", table_name="builds")
    op.drop_table("builds")
