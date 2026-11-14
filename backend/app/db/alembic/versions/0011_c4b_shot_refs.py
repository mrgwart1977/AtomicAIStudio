"""C4-B: shot-level generation bindings.

`shots.reference_set_id` binds a shot to a character's C4-A reference
set (membership stays on entity_assets — nothing is duplicated into
shot records). `shots.source_asset_id` binds the source image for
img2img shot generation. Both are nullable + ON DELETE SET NULL:
deleting a set or asset unbinds the shot rather than breaking it.

NOTE: migration 0001 creates tables from live metadata, so on a FRESH
database both columns already exist. Steps are conditional, but a
present-but-malformed `shots` table raises rather than silently
continuing.

DOWNGRADE LIMITATION: dropping the columns forgets the bindings;
shots, revisions, and provenance are untouched.

Revision: 0011_c4b
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_c4b"
down_revision = "0010_c4a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    tables = set(sa.inspect(conn).get_table_names())
    if "shots" not in tables:
        return  # legacy stamped DB — arrives via its own upgrades
    cols = {c["name"] for c in sa.inspect(conn).get_columns("shots")}
    required = {"id", "project_id", "scene_id", "position", "revision"}
    missing = required - cols
    if missing:
        raise RuntimeError(
            f"shots table is missing columns {sorted(missing)} — "
            "refusing to conceal corruption")
    with op.batch_alter_table("shots") as batch:
        if "reference_set_id" not in cols:
            batch.add_column(sa.Column(
                "reference_set_id", sa.String(36),
                sa.ForeignKey("reference_sets.id", ondelete="SET NULL"),
                nullable=True))
        if "source_asset_id" not in cols:
            batch.add_column(sa.Column(
                "source_asset_id", sa.String(36),
                sa.ForeignKey("assets.id", ondelete="SET NULL"),
                nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    if "shots" not in set(sa.inspect(conn).get_table_names()):
        return
    cols = {c["name"] for c in sa.inspect(conn).get_columns("shots")}
    with op.batch_alter_table("shots") as batch:
        if "source_asset_id" in cols:
            batch.drop_column("source_asset_id")
        if "reference_set_id" in cols:
            batch.drop_column("reference_set_id")
