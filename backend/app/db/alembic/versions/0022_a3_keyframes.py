"""A3: `keyframes` — shot-associated still-image beats.

Each keyframe pins a role (start/end/reference/intermediate), an
optional rational frame position, a user-editable prompt + negative
prompt, generation settings (seed/model), project-scoped continuity
refs, and the produced result asset. Provenance and undo ride
entity_revisions like every other entity.

Idempotent + additive; tolerant of stamped minimal fixture DBs (the
shots/scenes/assets parents may not exist there — the table still
creates fine since FK targets are referenced lazily by name, but we
guard anyway for consistency with 0021's convention).

Revision: 0022_a3
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_a3"
down_revision = "0021_a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("keyframes"):
        op.create_table(
            "keyframes",
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
            sa.Column("role", sa.String(20), server_default="start"),
            sa.Column("position", sa.Integer, server_default="0"),
            sa.Column("frame_num", sa.Integer, nullable=True),
            sa.Column("frame_den", sa.Integer, nullable=True),
            sa.Column("prompt", sa.Text, server_default=""),
            sa.Column("negative_prompt", sa.Text, server_default=""),
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
    if sa.inspect(op.get_bind()).has_table("keyframes"):
        op.drop_table("keyframes")
