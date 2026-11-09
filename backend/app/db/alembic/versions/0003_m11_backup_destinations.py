"""M1.1: configurable backup destinations + large-op progress/cancel.

- app_settings — durable app-level key/value settings (default backup dir)
- projects.backup_dir — optional per-project destination override
- backup_runs.destination_dir / bytes_total — where a run wrote + expected size
- generation_jobs.progress_json / cancel_requested — rich progress + cancel flag

NOTE: migration 0001 creates tables from live metadata, so on a FRESH
database all of these already exist. Every step is conditional — real
M0/M1 databases are upgraded incrementally, fresh ones are a no-op.

Revision: 0003_m11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_m11"
down_revision = "0002_m1"
branch_labels = None
depends_on = None


def _tables(conn) -> set[str]:
    return set(sa.inspect(conn).get_table_names())


def _cols(conn, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def upgrade() -> None:
    conn = op.get_bind()
    have = _tables(conn)

    if "app_settings" not in have:
        op.create_table(
            "app_settings",
            sa.Column("key", sa.String(80), primary_key=True),
            sa.Column("value_json", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    proj_cols = _cols(conn, "projects") if "projects" in have else set()
    if "backup_dir" not in proj_cols:
        with op.batch_alter_table("projects") as b:
            b.add_column(sa.Column("backup_dir", sa.Text(), nullable=True))

    run_cols = _cols(conn, "backup_runs") if "backup_runs" in have else set()
    for col, typ, default in [
        ("destination_dir", sa.Text(), ""),
        ("bytes_total", sa.Integer(), "0"),
    ]:
        if col not in run_cols:
            with op.batch_alter_table("backup_runs") as b:
                b.add_column(sa.Column(col, typ, server_default=default))

    job_cols = _cols(conn, "generation_jobs") if "generation_jobs" in have else set()
    if "progress_json" not in job_cols:
        with op.batch_alter_table("generation_jobs") as b:
            b.add_column(sa.Column("progress_json", sa.JSON(), nullable=True))
    if "cancel_requested" not in job_cols:
        with op.batch_alter_table("generation_jobs") as b:
            b.add_column(sa.Column("cancel_requested", sa.Boolean(), server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("generation_jobs") as b:
        b.drop_column("cancel_requested")
        b.drop_column("progress_json")
    with op.batch_alter_table("backup_runs") as b:
        b.drop_column("bytes_total")
        b.drop_column("destination_dir")
    with op.batch_alter_table("projects") as b:
        b.drop_column("backup_dir")
    op.drop_table("app_settings")
