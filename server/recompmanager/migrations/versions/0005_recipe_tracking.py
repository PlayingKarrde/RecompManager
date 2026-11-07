"""Track which built-in recipe a library recipe was copied from.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("recomps") as b:
        # sha256 of the recipe file as the server last wrote it (to spot hand edits)
        b.add_column(sa.Column("recipe_hash", sa.String(64)))
        # sha256 of the built-in recipe it was copied from; null once edited
        b.add_column(sa.Column("builtin_hash", sa.String(64)))


def downgrade() -> None:
    with op.batch_alter_table("recomps") as b:
        b.drop_column("builtin_hash")
        b.drop_column("recipe_hash")
