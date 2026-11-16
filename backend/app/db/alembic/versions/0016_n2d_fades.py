"""N2-D: audio fades, crossfades, and bounded gain automation.

`timeline_clips` gains:
- `fade_in_units` / `fade_out_units` — fade lengths in SOURCE units
  (samples for audio clips, matching the N2-A unit contract;
  0 = no fade)
- `fade_in_curve` / `fade_out_curve` — 'linear' | 'equal_power'
- `automation_json` — bounded keyframe list:
  [{id, tl_frame, gain_db}] sorted by tl_frame, <=64 points,
  linear-in-dB interpolation (rendered at N2-E)

New table `timeline_crossfades` — an explicit overlap record
between two clips on the same audio lane. Lanes still refuse
grid overlap; the crossfade window borrows source HANDLES at
render time (out-clip tail extension + in-clip head reach), so
occupancy, ripple, and packing semantics are unchanged.
Feasibility is re-evaluated at read time and reported via
`satisfied` — records are never silently dropped.

Idempotent: columns/table added only when absent.

Revision: 0016_n2d
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_n2d"
down_revision = "0015_n2c"
branch_labels = None
depends_on = None

CURVES = ("linear", "equal_power")


def upgrade() -> None:
    bind = op.get_bind()
    ccols = {c["name"] for c in sa.inspect(bind)
             .get_columns("timeline_clips")}
    adds = {
        "fade_in_units": sa.Column("fade_in_units", sa.Integer(),
                                   nullable=False, server_default="0"),
        "fade_in_curve": sa.Column("fade_in_curve", sa.String(20),
                                   nullable=False,
                                   server_default="'linear'"),
        "fade_out_units": sa.Column("fade_out_units", sa.Integer(),
                                    nullable=False, server_default="0"),
        "fade_out_curve": sa.Column("fade_out_curve", sa.String(20),
                                    nullable=False,
                                    server_default="'linear'"),
        "automation_json": sa.Column("automation_json", sa.Text(),
                                     nullable=False,
                                     server_default="'[]'"),
    }
    for name, col in adds.items():
        if name not in ccols:
            op.add_column("timeline_clips", col)

    tables = set(sa.inspect(bind).get_table_names())
    if "timeline_crossfades" not in tables:
        op.create_table(
            "timeline_crossfades",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36),
                      sa.ForeignKey("projects.id",
                                    ondelete="CASCADE"),
                      index=True),
            sa.Column("timeline_id", sa.String(36),
                      sa.ForeignKey("timelines.id",
                                    ondelete="CASCADE"),
                      index=True),
            sa.Column("out_clip_id", sa.String(36),
                      sa.ForeignKey("timeline_clips.id",
                                    ondelete="CASCADE"),
                      index=True),
            sa.Column("in_clip_id", sa.String(36),
                      sa.ForeignKey("timeline_clips.id",
                                    ondelete="CASCADE"),
                      index=True),
            sa.Column("tl_start", sa.Integer(), nullable=False),
            sa.Column("tl_frames", sa.Integer(), nullable=False),
            sa.Column("curve", sa.String(20), nullable=False,
                      server_default="'equal_power'"),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False),
            sa.UniqueConstraint("out_clip_id", "in_clip_id",
                                name="uq_crossfade_pair"),
        )


def downgrade() -> None:
    op.drop_table("timeline_crossfades")
    for name in ("automation_json", "fade_out_curve", "fade_out_units",
                 "fade_in_curve", "fade_in_units"):
        op.drop_column("timeline_clips", name)
