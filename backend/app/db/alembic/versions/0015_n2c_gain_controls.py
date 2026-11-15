"""N2-C: audio gain and track controls.

`timeline_tracks` gains `gain_db` (0.0 = unity) and `solo`;
`timeline_clips` gains `gain_db` (0.0 = unity, audio-source clips
only — video clips keep unity and are unaffected). Values are
decibels; linear amplitude = 10**(gain_db/20), applied by the mixer
at N2-E. Bounds are enforced at the command layer (-60..+12 dB).

Single representation: dB only — no parallel linear column that
could disagree. Existing rows backfill to unity silently.

Idempotent: columns are added only when absent.

Revision: 0015_n2c
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_n2c"
down_revision = "0014_n1e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tcols = {c["name"] for c in sa.inspect(op.get_bind())
             .get_columns("timeline_tracks")}
    if "gain_db" not in tcols:
        op.add_column("timeline_tracks", sa.Column(
            "gain_db", sa.Float(), nullable=False,
            server_default="0.0"))
    if "solo" not in tcols:
        op.add_column("timeline_tracks", sa.Column(
            "solo", sa.Boolean(), nullable=False,
            server_default=sa.false()))
    ccols = {c["name"] for c in sa.inspect(op.get_bind())
             .get_columns("timeline_clips")}
    if "gain_db" not in ccols:
        op.add_column("timeline_clips", sa.Column(
            "gain_db", sa.Float(), nullable=False,
            server_default="0.0"))


def downgrade() -> None:
    op.drop_column("timeline_clips", "gain_db")
    op.drop_column("timeline_tracks", "solo")
    op.drop_column("timeline_tracks", "gain_db")
