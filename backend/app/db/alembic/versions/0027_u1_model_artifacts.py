"""U-1: `model_artifacts` — the Universal Model and LoRA Manager registry.

Additive + idempotent. The table is a read-only projection seeded from
the in-code catalog (`model_catalog`) and associated to `model_installs`
rows at startup; `model_installs` remains authoritative. No existing
table, column, row, or file is touched by this migration — creation is
the only operation, guarded by has_table for repeat safety.
"""

from alembic import op
import sqlalchemy as sa

revision = "0027_u1"
down_revision = "0026_a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if insp.has_table("model_artifacts"):
        return
    op.create_table(
        "model_artifacts",
        sa.Column("artifact_id", sa.String(120), primary_key=True),
        sa.Column("display_name", sa.String(200), nullable=False,
                  server_default=""),
        sa.Column("kind", sa.String(20), nullable=False,
                  server_default="model"),
        sa.Column("arch", sa.String(40), nullable=False,
                  server_default="unknown"),
        sa.Column("family", sa.String(40), nullable=False, server_default=""),
        sa.Column("modality", sa.String(20), nullable=False,
                  server_default="image"),
        sa.Column("fmt", sa.String(30), nullable=False,
                  server_default="unknown"),
        sa.Column("role", sa.String(60), nullable=False, server_default=""),
        sa.Column("version", sa.String(60), nullable=False, server_default=""),
        sa.Column("source_type", sa.String(20), nullable=False,
                  server_default="catalog"),
        sa.Column("managed_by", sa.String(20), nullable=False,
                  server_default="atomic"),
        sa.Column("uri", sa.Text, nullable=True),
        sa.Column("content_sha256", sa.String(64), nullable=True),
        sa.Column("byte_size", sa.Integer, nullable=False, server_default="0"),
        sa.Column("license_json", sa.JSON, nullable=False,
                  server_default="{}"),
        sa.Column("support_tier", sa.String(20), nullable=False,
                  server_default="unknown"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("tags_json", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("meta_json", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("install_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_model_artifacts_arch", "model_artifacts", ["arch"])
    op.create_index("ix_model_artifacts_content_sha256",
                    "model_artifacts", ["content_sha256"])
    op.create_index("ix_model_artifacts_support_tier",
                    "model_artifacts", ["support_tier"])
    op.create_index("ix_model_artifacts_install_id",
                    "model_artifacts", ["install_id"])


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if not insp.has_table("model_artifacts"):
        return
    for ix in ("ix_model_artifacts_install_id",
               "ix_model_artifacts_support_tier",
               "ix_model_artifacts_content_sha256",
               "ix_model_artifacts_arch"):
        op.drop_index(ix, table_name="model_artifacts")
    op.drop_table("model_artifacts")
