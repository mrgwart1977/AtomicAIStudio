"""C5-A: timeline assembly foundation.

`timelines` — project-scoped edit decision lists with a rational
timebase (fps_num/fps_den). `timeline_clips` — ordered, pinned
references to a specific asset version + sha256 with integer
source-frame trims.

NOTE: migration 0001 creates tables from live metadata, so on a FRESH
database both tables already exist. Steps are conditional, but a
present-but-malformed table raises rather than silently continuing.

DOWNGRADE LIMITATION: drops the timeline tables and their contents;
source assets, revisions, and provenance are untouched.

Revision: 0012_c5a
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_c5a"
down_revision = "0011_c4b"
branch_labels = None
depends_on = None

_TIMELINE_COLS = {
    "id", "project_id", "name", "description", "fps_num", "fps_den",
    "status", "revision", "created_at", "updated_at",
}
_CLIP_COLS = {
    "id", "project_id", "timeline_id", "position",
    "source_asset_id", "source_version_id", "source_sha256",
    "in_frame", "out_frame", "src_frames",
    "src_fps_num", "src_fps_den", "src_duration_s",
    "shot_id", "scene_id", "created_at",
}


def upgrade() -> None:
    conn = op.get_bind()
    tables = set(sa.inspect(conn).get_table_names())
    if "timelines" in tables:
        cols = {c["name"] for c in sa.inspect(conn).get_columns("timelines")}
        missing = _TIMELINE_COLS - cols
        if missing:
            raise RuntimeError(
                f"timelines table is missing columns {sorted(missing)} — "
                "refusing to conceal corruption")
    else:
        op.create_table(
            "timelines",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36),
                      sa.ForeignKey("projects.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("name", sa.String(240), nullable=False),
            sa.Column("description", sa.Text(), nullable=False,
                      server_default=""),
            sa.Column("fps_num", sa.Integer(), nullable=False,
                      server_default="24"),
            sa.Column("fps_den", sa.Integer(), nullable=False,
                      server_default="1"),
            sa.Column("status", sa.String(20), nullable=False,
                      server_default="active"),
            sa.Column("revision", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      nullable=False),
        )
    if "timeline_clips" in tables:
        cols = {c["name"] for c in
                sa.inspect(conn).get_columns("timeline_clips")}
        missing = _CLIP_COLS - cols
        if missing:
            raise RuntimeError(
                f"timeline_clips table is missing columns "
                f"{sorted(missing)} — refusing to conceal corruption")
    else:
        op.create_table(
            "timeline_clips",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36),
                      sa.ForeignKey("projects.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("timeline_id", sa.String(36),
                      sa.ForeignKey("timelines.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("position", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("source_asset_id", sa.String(36),
                      sa.ForeignKey("assets.id", ondelete="RESTRICT"),
                      nullable=False),
            sa.Column("source_version_id", sa.String(36),
                      sa.ForeignKey("asset_versions.id",
                                    ondelete="RESTRICT"),
                      nullable=False),
            sa.Column("source_sha256", sa.String(64), nullable=False),
            sa.Column("in_frame", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("out_frame", sa.Integer(), nullable=False),
            sa.Column("src_frames", sa.Integer(), nullable=False),
            sa.Column("src_fps_num", sa.Integer(), nullable=False,
                      server_default="24"),
            sa.Column("src_fps_den", sa.Integer(), nullable=False,
                      server_default="1"),
            sa.Column("src_duration_s", sa.Float(), nullable=False,
                      server_default="0"),
            sa.Column("shot_id", sa.String(36),
                      sa.ForeignKey("shots.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("scene_id", sa.String(36),
                      sa.ForeignKey("scenes.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False),
        )


def downgrade() -> None:
    conn = op.get_bind()
    tables = set(sa.inspect(conn).get_table_names())
    if "timeline_clips" in tables:
        op.drop_table("timeline_clips")
    if "timelines" in tables:
        op.drop_table("timelines")
