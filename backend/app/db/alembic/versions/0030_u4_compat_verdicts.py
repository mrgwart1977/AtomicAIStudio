"""U-4b — compat_verdicts: latest five-axis compatibility verdict per
(artifact, runtime) pair.

Additive + idempotent. One new table only; nothing existing is touched.
The (artifact_id, runtime_key) unique constraint enforces the
latest-verdict model — one row per pair, rewritten by each evaluation.
artifact_id cascades to model_artifacts so a deleted artifact can never
leave orphan verdicts.

Downgrade drops only this table.
"""

import sqlalchemy as sa
from alembic import op

revision = "0030_u4"
down_revision = "0029_u3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if not insp.has_table("compat_verdicts"):
        op.create_table(
            "compat_verdicts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("artifact_id", sa.String(120),
                      sa.ForeignKey("model_artifacts.artifact_id",
                                    ondelete="CASCADE"),
                      nullable=False),
            sa.Column("runtime_key", sa.String(80), nullable=False),
            sa.Column("arch_axis", sa.String(20), nullable=False),
            sa.Column("arch_confidence", sa.String(20), nullable=True),
            sa.Column("runtime_axis", sa.String(24), nullable=False),
            sa.Column("hardware_axis", sa.String(20), nullable=False),
            sa.Column("policy_axis", sa.String(20), nullable=False),
            sa.Column("level", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("domain_levels_json", sa.JSON(), nullable=False,
                      server_default="{}"),
            sa.Column("overall", sa.String(20), nullable=False),
            sa.Column("blocking_axes_json", sa.JSON(), nullable=False,
                      server_default="[]"),
            sa.Column("confidence", sa.String(20), nullable=False,
                      server_default="unknown"),
            sa.Column("reasons_json", sa.JSON(), nullable=False,
                      server_default="[]"),
            sa.Column("warnings_json", sa.JSON(), nullable=False,
                      server_default="[]"),
            sa.Column("missing_json", sa.JSON(), nullable=False,
                      server_default="[]"),
            sa.Column("hardware_limits_json", sa.JSON(), nullable=False,
                      server_default="[]"),
            sa.Column("actions_json", sa.JSON(), nullable=False,
                      server_default="[]"),
            sa.Column("evidence_json", sa.JSON(), nullable=False,
                      server_default="{}"),
            sa.Column("inputs_fingerprint", sa.String(64), nullable=False,
                      server_default=""),
            sa.Column("ruleset_version", sa.String(40), nullable=False,
                      server_default=""),
            sa.Column("job_id", sa.String(36), nullable=True),
            sa.Column("evaluated_at", sa.DateTime(timezone=True),
                      nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      nullable=False),
            sa.UniqueConstraint("artifact_id", "runtime_key",
                                name="uq_compat_artifact_runtime"),
        )
        op.create_index("ix_compat_artifact", "compat_verdicts",
                        ["artifact_id"])
        op.create_index("ix_compat_overall", "compat_verdicts",
                        ["overall"])


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if insp.has_table("compat_verdicts"):
        op.drop_table("compat_verdicts")
