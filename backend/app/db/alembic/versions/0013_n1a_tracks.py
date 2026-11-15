"""N1-A: multitrack timeline foundation.

`timeline_tracks` — project-scoped lanes (kind/position/name/visible/
locked/muted). `timeline_clips` gains `track_id` (lane) and
`start_frames` (integer timeline-time position at the timeline's
fps_num/fps_den).

BACKFILL — every existing clip joins a per-timeline default video
track ("V1", position 0, visible, unlocked). `start_frames` is the
legacy sequential position converted to timeline frames: each clip's
extent is `(out-in) * src_fps_den * tl_fps_num /
(src_fps_num * tl_fps_den)` — i.e. duration seconds × timeline fps —
as an exact Fraction; each clip's start is the ceiling of the
running exact end — integer extents pack exactly as C5-A did, and a
fractional extent leaves a sub-frame gap rather than an overlap. No
float arithmetic anywhere in the edit data.

Migration is idempotent: table/columns are conditional, the default
track is only created when a timeline lacks a video track, and only
clips with NULL track_id are reassigned.

DOWNGRADE LIMITATION: drops the new column/table. Multi-track
information is lost — clips keep position order, which is all
ade-edl/1 could ever express.

Revision: 0013_n1a
"""
from __future__ import annotations

from fractions import Fraction
from datetime import datetime, timezone
import uuid

import sqlalchemy as sa
from alembic import op

revision = "0013_n1a"
down_revision = "0012_c5a"
branch_labels = None
depends_on = None

_TRACK_COLS = {
    "id", "project_id", "timeline_id", "kind", "position", "name",
    "visible", "locked", "muted", "created_at",
}


def _ensure_tracks_table() -> None:
    op.create_table(
        "timeline_tracks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("timeline_id", sa.String(36),
                  sa.ForeignKey("timelines.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("kind", sa.String(20), nullable=False,
                  server_default="video"),
        sa.Column("position", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("name", sa.String(120), nullable=False,
                  server_default=""),
        sa.Column("visible", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("locked", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("muted", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False),
    )


def _backfill() -> None:
    """Assign every legacy clip to a default V1 track and convert its
    sequence position to timeline frames via exact rationals."""
    conn = op.get_bind()
    now = datetime.now(timezone.utc)
    timelines = conn.execute(sa.text(
        "SELECT id, project_id, fps_num, fps_den FROM timelines"
    )).fetchall()
    for tl in timelines:
        v1 = conn.execute(sa.text(
            "SELECT id FROM timeline_tracks WHERE timeline_id = :t "
            "AND kind = 'video' ORDER BY position, id LIMIT 1"),
            {"t": tl.id}).scalar_one_or_none()
        if v1 is None:
            v1 = str(uuid.uuid4())
            conn.execute(sa.text(
                "INSERT INTO timeline_tracks (id, project_id, "
                "timeline_id, kind, position, name, visible, locked, "
                "muted, created_at) VALUES (:id, :p, :t, 'video', 0, "
                "'V1', :vis, :lock, :mute, :now)"),
                {"id": v1, "p": tl.project_id, "t": tl.id,
                 "vis": True, "lock": False, "mute": False, "now": now})
        clips = conn.execute(sa.text(
            "SELECT id, position, in_frame, out_frame, src_fps_num, "
            "src_fps_den FROM timeline_clips WHERE timeline_id = :t "
            "AND track_id IS NULL ORDER BY position, id"),
            {"t": tl.id}).fetchall()
        tl_fps = Fraction(tl.fps_num, tl.fps_den)
        acc = Fraction(0)
        for c in clips:
            extent = Fraction(c.out_frame - c.in_frame) * Fraction(
                c.src_fps_den, c.src_fps_num) * tl_fps
            start = -(-acc.numerator // acc.denominator)
            conn.execute(sa.text(
                "UPDATE timeline_clips SET track_id = :tr, "
                "start_frames = :s WHERE id = :c"),
                {"tr": v1, "s": start, "c": c.id})
            acc = Fraction(start) + extent


def upgrade() -> None:
    conn = op.get_bind()
    tables = set(sa.inspect(conn).get_table_names())
    # Stamped pre-timeline databases (this migration only extends the
    # timeline subsystem): nothing exists to create or backfill.
    if "timeline_clips" not in tables and "timelines" not in tables:
        return
    if "timeline_tracks" in tables:
        cols = {c["name"] for c in
                sa.inspect(conn).get_columns("timeline_tracks")}
        missing = _TRACK_COLS - cols
        if missing:
            raise RuntimeError(
                f"timeline_tracks is missing columns {sorted(missing)} "
                "— refusing to conceal corruption")
    else:
        _ensure_tracks_table()
    if "timeline_clips" in tables:
        clip_cols = {c["name"] for c in
                     sa.inspect(conn).get_columns("timeline_clips")}
        idx_names = {i["name"] for i in
                     sa.inspect(conn).get_indexes("timeline_clips")}
        # Raw ALTER ADD COLUMN — alembic's op.add_column reroutes the
        # FK through add_constraint (unsupported on SQLite), while
        # batch_alter_table reflects the whole table and resolves every
        # FK target, failing on minimal stamped DBs. Native SQLite DDL
        # emits the same REFERENCES clause fresh DBs get from metadata.
        if "track_id" not in clip_cols:
            conn.execute(sa.text(
                "ALTER TABLE timeline_clips ADD COLUMN track_id "
                "VARCHAR(36) REFERENCES timeline_tracks(id) "
                "ON DELETE SET NULL"))
        if "ix_timeline_clips_track_id" not in idx_names:
            op.create_index("ix_timeline_clips_track_id",
                            "timeline_clips", ["track_id"])
        if "start_frames" not in clip_cols:
            conn.execute(sa.text(
                "ALTER TABLE timeline_clips ADD COLUMN start_frames "
                "INTEGER NOT NULL DEFAULT 0"))
        _backfill()


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())
    if "timeline_clips" in tables:
        clip_cols = {c["name"] for c in
                     insp.get_columns("timeline_clips")}
        idx_names = {i["name"] for i in insp.get_indexes("timeline_clips")}
        # The index must go before batch_alter_table: batch recreates
        # the table and would try to recreate an index on a dropped
        # column.
        if "track_id" in clip_cols and (
                "ix_timeline_clips_track_id" in idx_names):
            op.drop_index("ix_timeline_clips_track_id",
                          table_name="timeline_clips")
        with op.batch_alter_table("timeline_clips") as batch:
            if "start_frames" in clip_cols:
                batch.drop_column("start_frames")
            if "track_id" in clip_cols:
                batch.drop_column("track_id")
    if "timeline_tracks" in tables:
        op.drop_table("timeline_tracks")
