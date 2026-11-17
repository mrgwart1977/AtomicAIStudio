"""A2: `shots.planning_json` — shot-planning metadata (camera angle /
movement / follow-mode + params, framing, subject placement, lighting /
action notes, optional rational fps + frames, continuity references,
free notes).

Separate from `continuity_json` on purpose: continuity is the
user-authored narrative-state blob (and A1's scaffold fingerprint
reads only title/type/description/location/cast — planning is outside
the scaffold-managed set by construction, so screenplay sync never
touches it).

Idempotent: the column is added only when absent; existing rows get
an empty object.

Revision: 0021_a2
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_a2"
down_revision = "0020_a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    # Minimal stamped-fixture DBs (and any exotic pre-scene schema) may
    # lack `shots` entirely — nothing to plan without the table.
    if not insp.has_table("shots"):
        return
    cols = {c["name"] for c in insp.get_columns("shots")}
    if "planning_json" not in cols:
        op.add_column("shots", sa.Column(
            "planning_json", sa.JSON(), nullable=False,
            server_default=sa.text("'{}'")))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("shots") and "planning_json" in {
            c["name"] for c in insp.get_columns("shots")}:
        op.drop_column("shots", "planning_json")
