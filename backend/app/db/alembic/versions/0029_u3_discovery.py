"""U-3 — model discovery tables.

Additive + idempotent. Two new tables only; nothing existing is
touched:

  discovery_roots      — user-approved scan roots (canonical_path is
                         identity; soft-archive via archived_at only —
                         no hard delete ever exists for provenance).
  discovery_candidates — unverified files observed inside roots.
                         `presence` (filesystem observation) is
                         orthogonal to `linked_artifact_id`
                         (U-2 registration linkage — scans can never
                         unlink it).

Downgrade drops only these two tables.
"""

import sqlalchemy as sa
from alembic import op

revision = "0029_u3"
down_revision = "0028_u2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if not insp.has_table("discovery_roots"):
        op.create_table(
            "discovery_roots",
            sa.Column("root_id", sa.String(40), primary_key=True),
            sa.Column("canonical_path", sa.Text(), nullable=False,
                      unique=True),
            sa.Column("display_path", sa.Text(), nullable=False,
                      server_default=""),
            sa.Column("enabled", sa.Boolean(), nullable=False,
                      server_default="1"),
            sa.Column("max_depth", sa.Integer(), nullable=False,
                      server_default="4"),
            sa.Column("added_at", sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column("archived_at", sa.DateTime(timezone=True),
                      nullable=True),
            sa.Column("last_scan_job_id", sa.String(36),
                      sa.ForeignKey("generation_jobs.id",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("last_scan_outcome", sa.String(20), nullable=True),
            sa.Column("last_code", sa.String(40), nullable=True),
            sa.Column("last_scan_at", sa.DateTime(timezone=True),
                      nullable=True),
            sa.Column("last_entries_seen", sa.Integer(), nullable=True),
            sa.Column("last_candidates", sa.Integer(), nullable=True),
            sa.Column("last_detail_json", sa.JSON(), nullable=True),
        )
    if not insp.has_table("discovery_candidates"):
        op.create_table(
            "discovery_candidates",
            sa.Column("candidate_id", sa.String(40), primary_key=True),
            sa.Column("resolved_path", sa.Text(), nullable=False,
                      unique=True),
            sa.Column("name", sa.String(400), nullable=False,
                      server_default=""),
            sa.Column("fmt_guess", sa.String(30), nullable=False,
                      server_default="unknown"),
            sa.Column("byte_size", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("mtime_ns", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("root_id", sa.String(40),
                      sa.ForeignKey("discovery_roots.root_id",
                                    ondelete="CASCADE"),
                      nullable=False),
            sa.Column("presence", sa.String(12), nullable=False,
                      server_default="present"),
            sa.Column("seen_count", sa.Integer(), nullable=False,
                      server_default="1"),
            sa.Column("first_seen_at", sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column("last_scan_job_id", sa.String(36), nullable=True),
            sa.Column("linked_artifact_id", sa.String(120),
                      sa.ForeignKey("model_artifacts.artifact_id",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("linked_at", sa.DateTime(timezone=True),
                      nullable=True),
            sa.Column("meta_json", sa.JSON(), nullable=False,
                      server_default="{}"),
        )
        op.create_index("ix_cand_root_id", "discovery_candidates",
                        ["root_id"])
        op.create_index("ix_cand_presence", "discovery_candidates",
                        ["presence"])
        op.create_index("ix_cand_linked", "discovery_candidates",
                        ["linked_artifact_id"])


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if insp.has_table("discovery_candidates"):
        op.drop_table("discovery_candidates")
    if insp.has_table("discovery_roots"):
        op.drop_table("discovery_roots")
