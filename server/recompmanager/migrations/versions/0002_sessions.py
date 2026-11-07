"""Password login: token kind (api/session) and expiry.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("api_tokens") as batch:
        batch.add_column(sa.Column("kind", sa.String(16), nullable=False, server_default="api"))
        batch.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("api_tokens") as batch:
        batch.drop_column("expires_at")
        batch.drop_column("kind")
