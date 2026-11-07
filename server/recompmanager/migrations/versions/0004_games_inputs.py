"""Game → Recomp split, per-platform artifacts, inputs vault, ROM scan, clients.

- The old ``games`` table (one row per recomp project) becomes ``recomps``.
- A new ``games`` table holds original titles; recomps point at one.
- Artifacts get one row per platform (a file serving two platforms becomes two
  rows). Files are moved into library/<game>/<recomp>/<platform>/<version>/ at
  startup by the relocate job, not here.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-07
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    # --- games → recomps, new games table
    op.drop_index("ix_versions_game_id", table_name="versions")
    op.drop_index("ix_jobs_game_id", table_name="jobs")
    op.rename_table("games", "recomps")
    op.create_table(
        "games",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("system", sa.String(50)),
        sa.Column("identifiers", sa.JSON(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    with op.batch_alter_table("recomps") as b:
        b.add_column(sa.Column("game_id", sa.String(64)))
        b.create_foreign_key("fk_recomps_game", "games", ["game_id"], ["id"], ondelete="SET NULL")
        b.create_index("ix_recomps_game_id", ["game_id"])
    # SQLite (3.25+) renames columns in place, including in constraints.
    op.execute("ALTER TABLE versions RENAME COLUMN game_id TO recomp_id")
    op.create_index("ix_versions_recomp_id", "versions", ["recomp_id"])
    op.execute("ALTER TABLE jobs RENAME COLUMN game_id TO recomp_id")
    with op.batch_alter_table("jobs") as b:
        b.add_column(sa.Column("game_id", sa.String(64)))
        b.create_foreign_key("fk_jobs_game", "games", ["game_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_jobs_recomp_id", "jobs", ["recomp_id"])
    op.create_index("ix_jobs_game_id", "jobs", ["game_id"])

    # --- artifacts: one row per platform
    op.create_table(
        "artifacts_new",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "version_id",
            sa.Integer(),
            sa.ForeignKey("versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(50), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64)),
        sa.Column("upstream_digest", sa.String(100)),
        sa.Column("source_url", sa.String(1000), nullable=False),
        sa.Column("rel_path", sa.String(1000), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("downloaded_at", TS),
        sa.UniqueConstraint("version_id", "platform", "name"),
    )
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT version_id, name, platforms, size, sha256, upstream_digest, source_url, "
            "rel_path, state, downloaded_at FROM artifacts ORDER BY id"
        )
    ).mappings()
    new_table = sa.table(
        "artifacts_new",
        *(
            sa.column(c)
            for c in (
                "version_id",
                "platform",
                "name",
                "size",
                "sha256",
                "upstream_digest",
                "source_url",
                "rel_path",
                "state",
                "downloaded_at",
            )
        ),
    )
    out = []
    for r in rows:
        platforms = r["platforms"]
        if isinstance(platforms, str):
            platforms = json.loads(platforms)
        for p in platforms or ["unknown"]:
            out.append({**{k: r[k] for k in r if k != "platforms"}, "platform": p})
    if out:
        op.bulk_insert(new_table, out)
    op.drop_index("ix_artifacts_version_id", table_name="artifacts")
    op.drop_table("artifacts")
    op.rename_table("artifacts_new", "artifacts")
    op.create_index("ix_artifacts_version_id", "artifacts", ["version_id"])

    # --- inputs vault
    op.create_table(
        "inputs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "game_id", sa.String(64), sa.ForeignKey("games.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("storage", sa.String(16), nullable=False),
        sa.Column("path", sa.String(2000), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("sha1", sa.String(40)),
        sa.Column("md5", sa.String(32)),
        sa.Column("crc32", sa.String(8)),
        sa.Column("sha256", sa.String(64)),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("fingerprint", sa.String(100)),
        sa.Column("hashed_at", TS),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_inputs_game_id", "inputs", ["game_id"])
    op.create_table(
        "input_files",
        sa.Column(
            "input_id",
            sa.Integer(),
            sa.ForeignKey("inputs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("path", sa.String(2000), primary_key=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("mtime", sa.Float(), nullable=False),
        sa.Column("sha256", sa.String(64)),
    )
    op.create_table(
        "uploads",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "game_id", sa.String(64), sa.ForeignKey("games.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("filename", sa.String(300), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("received", sa.Integer(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )

    # --- ROM library scan
    op.create_table(
        "scan_files",
        sa.Column("path", sa.String(2000), primary_key=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("mtime", sa.Float(), nullable=False),
        sa.Column("info", sa.JSON(), nullable=False),
        sa.Column("scanned_at", TS, nullable=False),
    )
    op.create_table(
        "scan_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "game_id", sa.String(64), sa.ForeignKey("games.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("path", sa.String(2000), nullable=False),
        sa.Column("member", sa.String(1000), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("confidence", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("input_id", sa.Integer(), sa.ForeignKey("inputs.id", ondelete="SET NULL")),
        sa.Column("found_at", TS, nullable=False),
        sa.UniqueConstraint("game_id", "path", "member"),
    )
    op.create_index("ix_scan_candidates_game_id", "scan_candidates", ["game_id"])

    # --- clients
    op.create_table(
        "clients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("platform", sa.String(50), nullable=False),
        sa.Column("token_id", sa.Integer(), sa.ForeignKey("api_tokens.id", ondelete="SET NULL")),
        sa.Column("installed", sa.JSON(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
    )


def downgrade() -> None:
    raise NotImplementedError("0004 can't be downgraded; restore a backup of the database")
