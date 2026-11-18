"""A6: `assembly_plans` + `assembly_items` — reviewable first-cut
proposals for one timeline.

A plan is a deterministic proposal computed from screenplay scene
order, storyboard shot order, selected motion clips, dialogue
segments, and shot-linked audio assets — never a silent timeline
rewrite. `plan_json` is the previewed proposal verbatim;
`sources_json` pins the source fingerprints so `assembly.apply`
refuses stale source state; `applied_json` records exactly what an
apply created (track ids + per-item timeline clip ids) making apply
idempotent and undo reversible.

Each item keeps its stable source association (scene/shot/dialogue/
motion-clip/asset refs, all SET NULL — an unlinked source keeps the
review record with a missing flag), its computed span, per-item
review status + notes, surfaced flags (missing media, conflicts,
warnings), and the source signature it was computed from.

Idempotent + additive; tolerant of stamped minimal fixture DBs
(same lazy-FK convention as 0021..0024).

Revision: 0025_a6
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_a6"
down_revision = "0024_a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("assembly_plans"):
        op.create_table(
            "assembly_plans",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36),
                      sa.ForeignKey("projects.id", ondelete="CASCADE"),
                      index=True),
            sa.Column("timeline_id", sa.String(36),
                      sa.ForeignKey("timelines.id", ondelete="CASCADE"),
                      index=True),
            sa.Column("name", sa.String(200), server_default="First cut"),
            sa.Column("status", sa.String(20), server_default="draft"),
            sa.Column("plan_json", sa.JSON, server_default="{}"),
            sa.Column("sources_json", sa.JSON, server_default="{}"),
            sa.Column("applied_json", sa.JSON, server_default="{}"),
            sa.Column("revision", sa.Integer, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
    if not insp.has_table("assembly_items"):
        op.create_table(
            "assembly_items",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36),
                      sa.ForeignKey("projects.id", ondelete="CASCADE"),
                      index=True),
            sa.Column("plan_id", sa.String(36),
                      sa.ForeignKey("assembly_plans.id",
                                    ondelete="CASCADE"),
                      index=True),
            sa.Column("position", sa.Integer, server_default="0"),
            sa.Column("item_key", sa.String(80), server_default="",
                      index=True),
            sa.Column("kind", sa.String(20), server_default="shot"),
            sa.Column("scene_id", sa.String(36),
                      sa.ForeignKey("scenes.id", ondelete="SET NULL"),
                      nullable=True, index=True),
            sa.Column("shot_id", sa.String(36),
                      sa.ForeignKey("shots.id", ondelete="SET NULL"),
                      nullable=True, index=True),
            sa.Column("dialogue_id", sa.String(36),
                      sa.ForeignKey("dialogue_segments.id",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("motion_clip_id", sa.String(36),
                      sa.ForeignKey("motion_clips.id",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("asset_id", sa.String(36),
                      sa.ForeignKey("assets.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("track_id", sa.String(36),
                      sa.ForeignKey("timeline_tracks.id",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("timeline_clip_id", sa.String(36),
                      sa.ForeignKey("timeline_clips.id",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("start_frames", sa.Integer, server_default="0"),
            sa.Column("extent_frames", sa.Integer, server_default="0"),
            sa.Column("review_status", sa.String(20),
                      server_default="pending"),
            sa.Column("notes", sa.Text, server_default=""),
            sa.Column("flags_json", sa.JSON, server_default="{}"),
            sa.Column("source_json", sa.JSON, server_default="{}"),
            sa.Column("revision", sa.Integer, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if insp.has_table("assembly_items"):
        op.drop_table("assembly_items")
    if insp.has_table("assembly_plans"):
        op.drop_table("assembly_plans")
