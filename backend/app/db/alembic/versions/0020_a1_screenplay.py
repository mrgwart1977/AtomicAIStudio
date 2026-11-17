"""A1 — integrated screenplay + shot-list scaffold:

Three additive tables, all project-scoped with FK isolation:

- `screenplays`: multiple screenplay documents per project (title,
  metadata, dense position, revision for entity_revisions history).
- `screenplay_scenes`: ordered scenes (heading, scene-level action,
  notes, optional location link, optional `scene_id` link to the
  storyboard Scene whose shots the scaffold manages).
- `screenplay_elements`: ordered action/dialogue/note rows inside a
  screenplay scene. Dialogue rows reference existing project
  `characters.id` (SET NULL — deleting a character keeps the text and
  leaves the speaker unresolved, never leaks or duplicates).

No existing table is altered; all FKs cascade on project delete.
Existing projects gain the capability with zero data change.

Idempotent: tables are created only when absent (raw stamped-schema
databases may already have them via create_all).

Revision: 0020_a1
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_a1"
down_revision = "0019_n3a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "screenplays" not in existing:
        op.create_table(
            "screenplays",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("project_id", sa.String(length=36),
                      sa.ForeignKey("projects.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("title", sa.String(length=240), nullable=False),
            sa.Column("description", sa.Text(), nullable=False,
                      server_default=""),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("status", sa.String(length=20), nullable=False,
                      server_default="active"),
            sa.Column("revision", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      nullable=False),
        )

    if "screenplay_scenes" not in existing:
        op.create_table(
            "screenplay_scenes",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("project_id", sa.String(length=36),
                      sa.ForeignKey("projects.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("screenplay_id", sa.String(length=36),
                      sa.ForeignKey("screenplays.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("position", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("heading", sa.String(length=300), nullable=False,
                      server_default=""),
            sa.Column("action", sa.Text(), nullable=False,
                      server_default=""),
            sa.Column("notes", sa.Text(), nullable=False,
                      server_default=""),
            sa.Column("location_id", sa.String(length=36),
                      sa.ForeignKey("locations.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("scene_id", sa.String(length=36),
                      sa.ForeignKey("scenes.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("revision", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      nullable=False),
        )

    if "screenplay_elements" not in existing:
        op.create_table(
            "screenplay_elements",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("project_id", sa.String(length=36),
                      sa.ForeignKey("projects.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("screenplay_scene_id", sa.String(length=36),
                      sa.ForeignKey("screenplay_scenes.id",
                                    ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("position", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("kind", sa.String(length=20), nullable=False,
                      server_default="action"),
            sa.Column("character_id", sa.String(length=36),
                      sa.ForeignKey("characters.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("text", sa.Text(), nullable=False,
                      server_default=""),
            sa.Column("parenthetical", sa.Text(), nullable=False,
                      server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      nullable=False),
        )


def downgrade() -> None:
    op.drop_table("screenplay_elements")
    op.drop_table("screenplay_scenes")
    op.drop_table("screenplays")
