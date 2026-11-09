"""M2: multi-character foundation — scenes, shots, casting, entity assets.

- scenes / scene_characters — ordered scenes with world/location links + cast
- shots / shot_characters — storyboard shots with per-character shot state
- entity_assets — bible entity ↔ asset links (reference images, identity)

NOTE: migration 0001 creates tables from live metadata, so on a FRESH
database all of these already exist. Every step is conditional — real
M1/M1.1 databases are upgraded incrementally, fresh ones are a no-op.

Revision: 0004_m2
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_m2"
down_revision = "0003_m11"
branch_labels = None
depends_on = None


def _tables(conn) -> set[str]:
    return set(sa.inspect(conn).get_table_names())


def upgrade() -> None:
    conn = op.get_bind()
    have = _tables(conn)

    if "scenes" not in have:
        op.create_table(
            "scenes",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), index=True),
            sa.Column("title", sa.String(240), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("intent", sa.Text(), nullable=False, server_default=""),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(20), nullable=False, server_default="active"),
            sa.Column("world_id", sa.String(36), sa.ForeignKey("worlds.id", ondelete="SET NULL"), nullable=True),
            sa.Column("location_id", sa.String(36), sa.ForeignKey("locations.id", ondelete="SET NULL"), nullable=True),
            sa.Column("continuity_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if "scene_characters" not in have:
        op.create_table(
            "scene_characters",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), index=True),
            sa.Column("scene_id", sa.String(36), sa.ForeignKey("scenes.id", ondelete="CASCADE"), index=True),
            sa.Column("character_id", sa.String(36), sa.ForeignKey("characters.id", ondelete="CASCADE"), index=True),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("role", sa.String(120), nullable=False, server_default=""),
            sa.Column("notes_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("scene_id", "character_id", name="uq_scene_character"),
        )

    if "shots" not in have:
        op.create_table(
            "shots",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), index=True),
            sa.Column("scene_id", sa.String(36), sa.ForeignKey("scenes.id", ondelete="CASCADE"), index=True),
            sa.Column("title", sa.String(240), nullable=False, server_default=""),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("intent", sa.Text(), nullable=False, server_default=""),
            sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
            sa.Column("shot_type", sa.String(30), nullable=False, server_default="medium"),
            sa.Column("duration_sec", sa.Float(), nullable=False, server_default="0"),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(20), nullable=False, server_default="active"),
            sa.Column("location_id", sa.String(36), sa.ForeignKey("locations.id", ondelete="SET NULL"), nullable=True),
            sa.Column("thumbnail_asset_id", sa.String(36), sa.ForeignKey("assets.id", ondelete="SET NULL"), nullable=True),
            sa.Column("continuity_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if "shot_characters" not in have:
        op.create_table(
            "shot_characters",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), index=True),
            sa.Column("shot_id", sa.String(36), sa.ForeignKey("shots.id", ondelete="CASCADE"), index=True),
            sa.Column("character_id", sa.String(36), sa.ForeignKey("characters.id", ondelete="CASCADE"), index=True),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("wardrobe_variant", sa.String(200), nullable=False, server_default=""),
            sa.Column("blocking", sa.Text(), nullable=False, server_default=""),
            sa.Column("expression", sa.String(200), nullable=False, server_default=""),
            sa.Column("emotion", sa.String(120), nullable=False, server_default=""),
            sa.Column("action_instructions", sa.Text(), nullable=False, server_default=""),
            sa.Column("dialogue", sa.Text(), nullable=False, server_default=""),
            sa.Column("voice_override", sa.String(200), nullable=False, server_default=""),
            sa.Column("model_ref", sa.String(200), nullable=False, server_default=""),
            sa.Column("lora_json", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("continuity_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("shot_id", "character_id", name="uq_shot_character"),
        )

    if "entity_assets" not in have:
        op.create_table(
            "entity_assets",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), index=True),
            sa.Column("entity_type", sa.String(30), nullable=False, index=True),
            sa.Column("entity_id", sa.String(36), nullable=False, index=True),
            sa.Column("asset_id", sa.String(36), sa.ForeignKey("assets.id", ondelete="CASCADE"), index=True),
            sa.Column("role", sa.String(30), nullable=False, server_default="reference"),
            sa.Column("label", sa.String(200), nullable=False, server_default=""),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("entity_type", "entity_id", "asset_id", "role", name="uq_entity_asset"),
        )


def downgrade() -> None:
    op.drop_table("entity_assets")
    op.drop_table("shot_characters")
    op.drop_table("shots")
    op.drop_table("scene_characters")
    op.drop_table("scenes")
