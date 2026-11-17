"""A5: `dialogue_segments` — speakable dialogue beats for audio gen.

Each segment binds a screenplay scene (+ optional dialogue element)
and/or a storyboard scene/shot to a Character speaker with text,
delivery direction, voice-resolution overrides (the Character's
profile_json['voice'] remains the identity-of-record), language and
pronunciation metadata, planned rational timing, generation settings,
and the produced audio result asset. All optional links are SET NULL —
unlinking keeps the segment with an unresolved-ref flag. Provenance
and undo ride entity_revisions like every other entity.

Idempotent + additive; tolerant of stamped minimal fixture DBs (the
parent tables may not exist there — FK targets are referenced lazily
by name, same convention as 0021/0022/0023).

Revision: 0024_a5
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_a5"
down_revision = "0023_a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("dialogue_segments"):
        op.create_table(
            "dialogue_segments",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36),
                      sa.ForeignKey("projects.id", ondelete="CASCADE"),
                      index=True),
            sa.Column("sp_scene_id", sa.String(36),
                      sa.ForeignKey("screenplay_scenes.id",
                                    ondelete="SET NULL"),
                      nullable=True, index=True),
            sa.Column("element_id", sa.String(36),
                      sa.ForeignKey("screenplay_elements.id",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("scene_id", sa.String(36),
                      sa.ForeignKey("scenes.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("shot_id", sa.String(36),
                      sa.ForeignKey("shots.id", ondelete="SET NULL"),
                      nullable=True, index=True),
            sa.Column("character_id", sa.String(36),
                      sa.ForeignKey("characters.id",
                                    ondelete="SET NULL"),
                      nullable=True, index=True),
            sa.Column("position", sa.Integer, server_default="0"),
            sa.Column("text", sa.Text, server_default=""),
            sa.Column("direction", sa.Text, server_default=""),
            sa.Column("voice_json", sa.JSON, server_default="{}"),
            sa.Column("language", sa.String(20), server_default="en"),
            sa.Column("pronunciation_json", sa.JSON,
                      server_default="{}"),
            sa.Column("timing_json", sa.JSON, server_default="{}"),
            sa.Column("seed", sa.Integer, nullable=True),
            sa.Column("model_key", sa.String(200), nullable=True),
            sa.Column("settings_json", sa.JSON, server_default="{}"),
            sa.Column("result_asset_id", sa.String(36),
                      sa.ForeignKey("assets.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("generate_job_id", sa.String(36),
                      sa.ForeignKey("generation_jobs.id",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("status", sa.String(20), server_default="active"),
            sa.Column("revision", sa.Integer, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("dialogue_segments"):
        op.drop_table("dialogue_segments")
