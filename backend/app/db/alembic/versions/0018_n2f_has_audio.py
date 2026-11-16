"""N2-F review: `timeline_clips.src_has_audio` — whether the pinned
source carries an audio stream. The preview engine schedules embedded
audio only for clips whose flag is true; export keeps probing fresh
and ignores the column.

Backfill: audio-lane clips are always audible-kind sources (True);
video-lane clips read `asset_versions.media_json.probe.audio_streams`
from the import-time probe — 0/missing → False.

Idempotent: the column is added only when absent; the backfill only
writes rows that still carry the default.

Revision: 0018_n2f
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0018_n2f"
down_revision = "0017_n2f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind)
            .get_columns("timeline_clips")}
    if "src_has_audio" not in cols:
        op.add_column("timeline_clips", sa.Column(
            "src_has_audio", sa.Boolean(), nullable=False,
            server_default="1"))
    tables = set(sa.inspect(bind).get_table_names())
    if not {"timeline_tracks", "asset_versions"} <= tables:
        return
    rows = bind.execute(sa.text(
        "SELECT c.id, av.media_json FROM timeline_clips c "
        "JOIN timeline_tracks t ON t.id = c.track_id "
        "JOIN asset_versions av ON av.id = c.source_version_id "
        "WHERE t.kind != 'audio'")).fetchall()
    for cid, media_json in rows:
        media = media_json
        if isinstance(media, str):
            try:
                media = json.loads(media)
            except Exception:
                media = {}
        probe = ((media or {}).get("probe") or {})
        if not probe.get("audio_streams"):
            bind.execute(sa.text(
                "UPDATE timeline_clips SET src_has_audio = 0 "
                "WHERE id = :id"), {"id": cid})


def downgrade() -> None:
    op.drop_column("timeline_clips", "src_has_audio")
