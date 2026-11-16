"""N3-A — per-track gain automation on `timeline_tracks`:
`automation_json` (bounded keyframe list [{id, at_tl_frames, gain_db}]
in ABSOLUTE timeline frames) + `automation_interp` ('linear'|'step').

Same stored representation as `timeline_clips` automation — one
vocabulary, one evaluator. Existing lanes start empty ('[]'/'linear'),
which is the neutral envelope, so no backfill is needed.

Idempotent: columns are added only when absent; the raw stamped-schema
path tolerates a table that doesn't exist yet.

Revision: 0019_n3a
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_n3a"
down_revision = "0018_n2f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "timeline_tracks" not in set(
            sa.inspect(bind).get_table_names()):
        return
    cols = {c["name"] for c in sa.inspect(bind)
            .get_columns("timeline_tracks")}
    if "automation_json" not in cols:
        op.add_column("timeline_tracks", sa.Column(
            "automation_json", sa.Text(), nullable=False,
            server_default="[]"))
    if "automation_interp" not in cols:
        op.add_column("timeline_tracks", sa.Column(
            "automation_interp", sa.String(length=10), nullable=False,
            server_default="linear"))


def downgrade() -> None:
    op.drop_column("timeline_tracks", "automation_interp")
    op.drop_column("timeline_tracks", "automation_json")
