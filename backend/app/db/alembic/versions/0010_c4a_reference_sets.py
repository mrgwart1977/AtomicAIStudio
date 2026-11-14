"""C4-A: character reference sets.

`reference_sets` carries named groupings of a character's entity-asset
reference links plus per-set identity guidance (pinned seed, notes).
Membership lives on `entity_assets.set_id` — the links keep their own
role/weight/ordering, so no membership data is duplicated.

NOTE: migration 0001 creates tables from live metadata, so on a FRESH
database both objects already exist. Every step is
conditional — but a `reference_sets` table that exists WITHOUT the
required columns is treated as genuine corruption and raises rather
than being silently kept.

DOWNGRADE LIMITATION: downgrading drops `entity_assets.set_id` (set
membership is irreversibly forgotten — the links themselves survive)
and drops `reference_sets`. Asset rows, CAS bytes, and historical job
provenance are untouched in both directions.

Revision: 0010_c4a
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_c4a"
down_revision = "0009_c3d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    tables = set(sa.inspect(conn).get_table_names())
    if "reference_sets" not in tables:
        op.create_table(
            "reference_sets",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36),
                      sa.ForeignKey("projects.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("character_id", sa.String(36),
                      sa.ForeignKey("characters.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("seed", sa.Integer(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=False, server_default=""),
            sa.Column("position", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("status", sa.String(20), nullable=False,
                      server_default="active"),
            sa.Column("revision", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      nullable=False),
        )
    else:
        # A present-but-malformed table is schema corruption, not a
        # reason to skip — fail loudly instead of keeping a broken set.
        have = {c["name"] for c in
                sa.inspect(conn).get_columns("reference_sets")}
        required = {"id", "project_id", "character_id", "name", "seed",
                    "revision"}
        missing = required - have
        if missing:
            raise RuntimeError(
                "reference_sets table exists but is missing columns "
                f"{sorted(missing)} — refusing to conceal corruption")
    # entity_assets may be absent on legacy DBs stamped before it was
    # introduced (the table arrives via their own upgrades); skip then.
    if "entity_assets" in tables:
        cols = {c["name"] for c in
                sa.inspect(conn).get_columns("entity_assets")}
        if "set_id" not in cols:
            with op.batch_alter_table("entity_assets") as batch:
                batch.add_column(sa.Column(
                    "set_id", sa.String(36),
                    sa.ForeignKey("reference_sets.id", ondelete="SET NULL"),
                    nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    if "set_id" in {c["name"] for c in
                    sa.inspect(conn).get_columns("entity_assets")}:
        with op.batch_alter_table("entity_assets") as batch:
            batch.drop_column("set_id")
    if "reference_sets" in sa.inspect(conn).get_table_names():
        op.drop_table("reference_sets")
