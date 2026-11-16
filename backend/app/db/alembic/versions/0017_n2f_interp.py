"""N2-F: automation interpolation mode + extended fade curves.

`timeline_clips.automation_interp` — 'linear' (dB-ramp between
neighbors, the N2-D behavior) or 'step' (hold each keyframe's value
until the next point).

The fade-curve vocabulary also grows ('s_curve', 'exponential') but
those live in the existing String(20) curve columns — no schema
change needed there.

Idempotent: the column is added only when absent.

Revision: 0017_n2f
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_n2f"
down_revision = "0016_n2d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind)
            .get_columns("timeline_clips")}
    if "automation_interp" not in cols:
        op.add_column("timeline_clips", sa.Column(
            "automation_interp", sa.String(10), nullable=False,
            server_default="'linear'"))


def downgrade() -> None:
    op.drop_column("timeline_clips", "automation_interp")
