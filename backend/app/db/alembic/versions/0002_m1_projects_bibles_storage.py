"""M1: storage locations, CAS asset files/versions, bibles, revisions,
backup runs, shared links, project format columns, job result_json.

NOTE: migration 0001 creates tables from live metadata, so a FRESH
database already contains these tables/columns. Every step here is
conditional, so it upgrades real M0 databases incrementally AND stays a
no-op on fresh ones. Backfills asset_files + asset_versions from M0
`assets.uri/sha256` rows against the seeded default storage location.

Revision: 0002_m1
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0002_m1"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

DEFAULT_LOCATION_ID = "00000000-0000-4000-8000-00000000d001"


def _tables(conn) -> set[str]:
    return set(sa.inspect(conn).get_table_names())


def _cols(conn, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def upgrade() -> None:
    conn = op.get_bind()
    have = _tables(conn)

    if "storage_locations" not in have:
        op.create_table(
            "storage_locations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("label", sa.String(120), nullable=False),
            sa.Column("root_path", sa.Text(), nullable=False),
            sa.Column("kind", sa.String(20), nullable=False, server_default="local"),
            sa.Column("status", sa.String(20), nullable=False, server_default="online"),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "asset_files" not in have:
        op.create_table(
            "asset_files",
            sa.Column("sha256", sa.String(64), primary_key=True),
            sa.Column("storage_location_id", sa.String(36),
                      sa.ForeignKey("storage_locations.id", ondelete="RESTRICT"),
                      nullable=False, index=True),
            sa.Column("rel_path", sa.Text(), nullable=False),
            sa.Column("byte_size", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="ready", index=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "asset_versions" not in have:
        op.create_table(
            "asset_versions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("asset_id", sa.String(36),
                      sa.ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("version_no", sa.Integer(), nullable=False),
            sa.Column("sha256", sa.String(64),
                      sa.ForeignKey("asset_files.sha256", ondelete="RESTRICT"),
                      nullable=False, index=True),
            sa.Column("media_json", sa.JSON(), nullable=False),
            sa.Column("command_id", sa.String(36),
                      sa.ForeignKey("creative_commands.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("asset_id", "version_no", name="uq_asset_version"),
        )
    if "shared_asset_links" not in have:
        op.create_table(
            "shared_asset_links",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36),
                      sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("asset_id", sa.String(36),
                      sa.ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("source_project_id", sa.String(36),
                      sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("mode", sa.String(10), nullable=False, server_default="link"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("project_id", "asset_id", name="uq_shared_link"),
        )
    for table, json_col in (
        ("characters", "profile_json"),
        ("worlds", "lore_json"),
        ("locations", "details_json"),
    ):
        if table in have:
            continue
        cols = [
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36),
                      sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
        ]
        if table == "locations":
            cols.append(
                sa.Column("world_id", sa.String(36),
                          sa.ForeignKey("worlds.id", ondelete="SET NULL"), nullable=True)
            )
        cols += [
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False, server_default=""),
            sa.Column(json_col, sa.JSON(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="active"),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        ]
        op.create_table(table, *cols)
    if "entity_revisions" not in have:
        op.create_table(
            "entity_revisions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36),
                      sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("entity_type", sa.String(30), nullable=False, index=True),
            sa.Column("entity_id", sa.String(36), nullable=False, index=True),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("snapshot_json", sa.JSON(), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("command_id", sa.String(36),
                      sa.ForeignKey("creative_commands.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("entity_type", "entity_id", "revision", name="uq_entity_rev"),
        )
    if "backup_runs" not in have:
        op.create_table(
            "backup_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36),
                      sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("project_name", sa.String(200), nullable=False, server_default=""),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="running", index=True),
            sa.Column("package_path", sa.Text(), nullable=False, server_default=""),
            sa.Column("package_sha256", sa.String(64), nullable=False, server_default=""),
            sa.Column("byte_size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("stats_json", sa.JSON(), nullable=False),
            sa.Column("job_id", sa.String(36),
                      sa.ForeignKey("generation_jobs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )

    proj_cols = _cols(conn, "projects") if "projects" in have else set()
    project_new_cols = [
        ("kind", sa.String(40), "movie"),
        ("aspect_ratio", sa.String(12), "16:9"),
        ("width", sa.Integer(), "1920"),
        ("height", sa.Integer(), "1080"),
        ("fps_num", sa.Integer(), "24"),
        ("fps_den", sa.Integer(), "1"),
        ("style", sa.String(80), "none"),
    ]
    for col, typ, default in project_new_cols:
        if col not in proj_cols:
            with op.batch_alter_table("projects") as b:
                b.add_column(sa.Column(col, typ, server_default=default))
    if "storage_location_id" not in proj_cols:
        with op.batch_alter_table("projects") as b:
            b.add_column(
                sa.Column("storage_location_id", sa.String(36),
                          sa.ForeignKey("storage_locations.id", ondelete="SET NULL"), nullable=True)
            )

    asset_cols = _cols(conn, "assets") if "assets" in have else set()
    for col, typ, default in [
        ("name", sa.String(240), ""),
        ("scope", sa.String(20), "project"),
        ("status", sa.String(20), "ready"),
        ("byte_size", sa.Integer(), "0"),
    ]:
        if col not in asset_cols:
            with op.batch_alter_table("assets") as b:
                b.add_column(sa.Column(col, typ, server_default=default))
    if "current_version_id" not in asset_cols:
        with op.batch_alter_table("assets") as b:
            b.add_column(sa.Column("current_version_id", sa.String(36), nullable=True))

    job_cols = _cols(conn, "generation_jobs") if "generation_jobs" in have else set()
    if "result_json" not in job_cols:
        with op.batch_alter_table("generation_jobs") as b:
            b.add_column(sa.Column("result_json", sa.JSON(), nullable=True))

    # Seed the default storage location (root_path patched at app startup
    # via ensure_default_location — it is machine-specific).
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        sa.text(
            "INSERT OR IGNORE INTO storage_locations (id, label, root_path, kind, status, is_default, created_at)"
            " VALUES (:id, 'Default', '', 'local', 'online', 1, :now)"
        ).bindparams(id=DEFAULT_LOCATION_ID, now=now)
    )

    # Backfill CAS rows for pre-M1 assets only when none exist yet.
    files_empty = not conn.execute(sa.text("SELECT 1 FROM asset_files LIMIT 1")).first()
    if files_empty:
        rows = conn.execute(sa.text("SELECT id, uri, sha256 FROM assets WHERE sha256 != ''")).all()
        for asset_id, uri, sha in rows:
            conn.execute(
                sa.text(
                    "INSERT OR IGNORE INTO asset_files (sha256, storage_location_id, rel_path, byte_size, status, created_at)"
                    " VALUES (:sha, :loc, :rel, 0, 'ready', :now)"
                ).bindparams(sha=sha, loc=DEFAULT_LOCATION_ID, rel=uri, now=now)
            )
            version_id = str(uuid.uuid4())
            conn.execute(
                sa.text(
                    "INSERT OR IGNORE INTO asset_versions (id, asset_id, version_no, sha256, media_json, command_id, created_at)"
                    " VALUES (:vid, :aid, 1, :sha, '{}', NULL, :now)"
                ).bindparams(vid=version_id, aid=asset_id, sha=sha, now=now)
            )
            conn.execute(
                sa.text("UPDATE assets SET current_version_id = :vid, name = uri WHERE id = :aid").bindparams(
                    vid=version_id, aid=asset_id
                )
            )


def downgrade() -> None:
    op.drop_table("backup_runs")
    op.drop_table("entity_revisions")
    op.drop_table("locations")
    op.drop_table("worlds")
    op.drop_table("characters")
    op.drop_table("shared_asset_links")
    op.drop_table("asset_versions")
    op.drop_table("asset_files")
    op.drop_table("storage_locations")
    with op.batch_alter_table("generation_jobs") as b:
        b.drop_column("result_json")
    with op.batch_alter_table("assets") as b:
        b.drop_column("current_version_id")
        b.drop_column("byte_size")
        b.drop_column("status")
        b.drop_column("scope")
        b.drop_column("name")
    with op.batch_alter_table("projects") as b:
        b.drop_column("storage_location_id")
        b.drop_column("style")
        b.drop_column("fps_den")
        b.drop_column("fps_num")
        b.drop_column("height")
        b.drop_column("width")
        b.drop_column("aspect_ratio")
        b.drop_column("kind")
