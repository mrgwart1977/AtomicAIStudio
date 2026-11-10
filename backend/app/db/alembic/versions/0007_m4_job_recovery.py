"""M4.0: job recovery contracts — attempt tracking + local/remote scope.

- `generation_jobs.attempt_no`: execution-attempt counter; each retry is a
  distinct attempt so recovery can reconcile "which run produced this".
- `generation_jobs.execution_scope`: "local" (subprocess worker) vs future
  "remote" (provider job) — remote rows will additionally carry provider
  identity/billing state in M4.1; the scope field lands now so every job
  created from M4.0 onward is already classified.
- `generation_jobs.recovery_state`: NULL (clean) or "needs_reconciliation"
  — set when a restart finds the job mid-execution with unknown outcome.

NOTE: migration 0001 creates tables from live metadata, so on a FRESH
database the columns already exist. Every step is conditional.

Revision: 0007_m4
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_m4"
down_revision = "0006_m3"
branch_labels = None
depends_on = None


def _columns(conn, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(conn).get_columns(table)}


def upgrade() -> None:
    conn = op.get_bind()
    if "generation_jobs" not in set(sa.inspect(conn).get_table_names()):
        return
    cols = _columns(conn, "generation_jobs")

    if "attempt_no" not in cols:
        op.add_column("generation_jobs", sa.Column("attempt_no", sa.Integer(), server_default="1"))
    if "execution_scope" not in cols:
        op.add_column("generation_jobs",
                      sa.Column("execution_scope", sa.String(16), server_default="local"))
    if "recovery_state" not in cols:
        op.add_column("generation_jobs", sa.Column("recovery_state", sa.String(32), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    if "generation_jobs" not in set(sa.inspect(conn).get_table_names()):
        return
    cols = _columns(conn, "generation_jobs")
    for col in ("recovery_state", "execution_scope", "attempt_no"):
        if col in cols:
            op.drop_column("generation_jobs", col)
