"""M2.1: structured cast state — shot_characters gains reference_ids_json + ext_json.

`reference_ids_json` holds the identity/reference asset IDs named in the v1
cast-state schema (validated as project assets at the command boundary).
`ext_json` is the forward-compat bag: schema fields a newer writer sends that
this build doesn't model yet — and legacy keys from pre-schema writers — are
preserved there instead of silently dropped.

NOTE: migration 0001 creates tables from live metadata, so on a FRESH
database these columns already exist. Every step is conditional — real
M2 databases get the columns added, fresh ones are a no-op.

Revision: 0005_m21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_m21"
down_revision = "0004_m2"
branch_labels = None
depends_on = None


def _columns(conn, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def upgrade() -> None:
    conn = op.get_bind()
    have = _columns(conn, "shot_characters")

    if "reference_ids_json" not in have:
        op.add_column(
            "shot_characters",
            sa.Column("reference_ids_json", sa.JSON(), nullable=False, server_default="[]"),
        )
    if "ext_json" not in have:
        op.add_column(
            "shot_characters",
            sa.Column("ext_json", sa.JSON(), nullable=False, server_default="{}"),
        )


def downgrade() -> None:
    conn = op.get_bind()
    have = _columns(conn, "shot_characters")
    if "ext_json" in have:
        op.drop_column("shot_characters", "ext_json")
    if "reference_ids_json" in have:
        op.drop_column("shot_characters", "reference_ids_json")
