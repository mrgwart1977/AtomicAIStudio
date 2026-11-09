"""M3: real inference foundation — model/runtime installs + GPU job fields.

- `model_installs`: installed model weights (app-level, outside project CAS).
- `runtime_installs`: installed inference runtime binaries (e.g. sd.cpp).
- `generation_jobs.project_id` becomes nullable → app-level jobs (model
  downloads) broadcast with project_id=null.
- `generation_jobs.gpu_index` / `model_key`: scheduling + provenance.

NOTE: migration 0001 creates tables from live metadata, so on a FRESH
database everything already exists. Every step is conditional.

Revision: 0006_m3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_m3"
down_revision = "0005_m21"
branch_labels = None
depends_on = None


def _tables(conn) -> set[str]:
    return set(sa.inspect(conn).get_table_names())


def _columns(conn, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def upgrade() -> None:
    conn = op.get_bind()
    tables = _tables(conn)

    if "model_installs" not in tables:
        op.create_table(
            "model_installs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("model_key", sa.String(80), index=True),
            sa.Column("adapter_id", sa.String(80), nullable=False),
            sa.Column("version", sa.String(60), server_default=""),
            sa.Column("status", sa.String(20), server_default="downloading", index=True),
            sa.Column("install_root", sa.Text(), server_default=""),
            sa.Column("rel_dir", sa.Text(), server_default=""),
            sa.Column("file_name", sa.String(240), server_default=""),
            sa.Column("byte_size", sa.Integer(), server_default="0"),
            sa.Column("sha256", sa.String(64), server_default=""),
            sa.Column("verified", sa.Boolean(), server_default="0"),
            sa.Column("license", sa.String(120), server_default=""),
            sa.Column("meta_json", sa.JSON(), server_default="{}"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )

    if "runtime_installs" not in tables:
        op.create_table(
            "runtime_installs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("runtime_key", sa.String(80), index=True),
            sa.Column("version", sa.String(60), server_default=""),
            sa.Column("status", sa.String(20), server_default="downloading", index=True),
            sa.Column("install_dir", sa.Text(), server_default=""),
            sa.Column("exe_path", sa.Text(), server_default=""),
            sa.Column("byte_size", sa.Integer(), server_default="0"),
            sa.Column("verified", sa.Boolean(), server_default="0"),
            sa.Column("meta_json", sa.JSON(), server_default="{}"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )

    if "generation_jobs" in tables:
        job_cols = _columns(conn, "generation_jobs")
        if "gpu_index" not in job_cols:
            op.add_column("generation_jobs", sa.Column("gpu_index", sa.Integer(), nullable=True))
        if "model_key" not in job_cols:
            op.add_column("generation_jobs", sa.Column("model_key", sa.String(80), nullable=True))

        # App-level jobs: project_id nullable. Fresh databases already have it
        # nullable (0001 builds from live metadata) — only rebuild when it isn't.
        proj_col = next((c for c in sa.inspect(conn).get_columns("generation_jobs")
                         if c["name"] == "project_id"), None)
        if proj_col is not None and not proj_col.get("nullable", True):
            with op.batch_alter_table("generation_jobs") as batch:
                batch.alter_column("project_id", existing_type=sa.String(36), nullable=True)


def downgrade() -> None:
    conn = op.get_bind()
    tables = _tables(conn)
    job_cols = _columns(conn, "generation_jobs") if "generation_jobs" in tables else set()
    if "model_key" in job_cols:
        op.drop_column("generation_jobs", "model_key")
    if "gpu_index" in job_cols:
        op.drop_column("generation_jobs", "gpu_index")
    if "runtime_installs" in tables:
        op.drop_table("runtime_installs")
    if "model_installs" in tables:
        op.drop_table("model_installs")
