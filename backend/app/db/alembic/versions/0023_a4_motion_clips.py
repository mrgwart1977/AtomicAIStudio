"""A4: `motion_clips` — shot-associated keyframe-to-video beats.

Each motion clip joins optional start/end keyframes, a user-editable
prompt + negative prompt, motion direction (instructions/intensity/
temporal consistency), validated camera movement + follow settings
(same vocabulary as A2 shot planning), a rational duration + frame
rate, resolution, generation settings, and the produced result asset.
Provenance and undo ride entity_revisions like every other entity.

Idempotent + additive; tolerant of stamped minimal fixture DBs (the
shots/scenes/keyframes/assets parents may not exist there — the table
still creates fine since FK targets are referenced lazily by name, but
we guard anyway for consistency with 0021/0022's convention).

Revision: 0023_a4
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_a4"
down_revision = "0022_a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("motion_clips"):
        op.create_table(
            "motion_clips",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36),
                      sa.ForeignKey("projects.id", ondelete="CASCADE"),
                      index=True),
            sa.Column("shot_id", sa.String(36),
                      sa.ForeignKey("shots.id", ondelete="CASCADE"),
                      index=True),
            sa.Column("scene_id", sa.String(36),
                      sa.ForeignKey("scenes.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("start_keyframe_id", sa.String(36),
                      sa.ForeignKey("keyframes.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("end_keyframe_id", sa.String(36),
                      sa.ForeignKey("keyframes.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("position", sa.Integer, server_default="0"),
            sa.Column("prompt", sa.Text, server_default=""),
            sa.Column("negative_prompt", sa.Text, server_default=""),
            sa.Column("motion_json", sa.JSON, server_default="{}"),
            sa.Column("camera_json", sa.JSON, server_default="{}"),
            sa.Column("duration_frames", sa.Integer, nullable=True),
            sa.Column("fps_num", sa.Integer, server_default="24"),
            sa.Column("fps_den", sa.Integer, server_default="1"),
            sa.Column("width", sa.Integer, server_default="512"),
            sa.Column("height", sa.Integer, server_default="512"),
            sa.Column("model_key", sa.String(200), nullable=True),
            sa.Column("seed", sa.Integer, nullable=True),
            sa.Column("settings_json", sa.JSON, server_default="{}"),
            sa.Column("continuity_json", sa.JSON, server_default="{}"),
            sa.Column("result_asset_id", sa.String(36),
                      sa.ForeignKey("assets.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("generate_job_id", sa.String(36),
                      sa.ForeignKey("generation_jobs.id",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("prompt_origin", sa.String(20),
                      server_default="user"),
            sa.Column("status", sa.String(20), server_default="active"),
            sa.Column("revision", sa.Integer, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("motion_clips"):
        op.drop_table("motion_clips")
