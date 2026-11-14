"""C3-D1: entity_assets gains an optional reference weight.

`weight` records per-link guidance strength (0.0–2.0; NULL = provider
default). It is honored only by models whose catalog `editing` block
declares `reference_weights` — stored intent for every link, no-op for
providers that do not support weighting.

NOTE: migration 0001 creates tables from live metadata, so on a FRESH
database this column already exists. The step is conditional — real
databases get the column added, fresh ones are a no-op.

Revision: 0009_c3d1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_c3d1"
down_revision = "0008_c2"
branch_labels = None
depends_on = None


def _columns(conn, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def upgrade() -> None:
    conn = op.get_bind()
    # Minimal/partial databases (e.g. a bare 0004-shaped shot_characters
    # test DB) may lack the table entirely — nothing to do then.
    if "entity_assets" not in sa.inspect(conn).get_table_names():
        return
    if "weight" not in _columns(conn, "entity_assets"):
        op.add_column(
            "entity_assets",
            sa.Column("weight", sa.Float(), nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()
    if "entity_assets" not in sa.inspect(conn).get_table_names():
        return
    if "weight" in _columns(conn, "entity_assets"):
        op.drop_column("entity_assets", "weight")
