"""U-2a: `model_artifacts` import provenance columns.

Additive + idempotent. Three nullable/defaulted columns:
  source_uri          — Text; the user's absolute source path
                        (arbitrary-length Windows paths, incl. \\?\
                        extended form). Privacy: never echoed into list
                        views, job events, audit payloads, or logs.
  imported_at         — registration timestamp (NULL on catalog rows).
  import_report_json  — sanitized inspection report (sha, fmt,
                        suggestions, registration_stat, warnings) —
                        contains no path copy.

Downgrade drops only these columns. No existing table, column, or row
is touched; catalog rows get NULL/empty defaults.
"""

import sqlalchemy as sa
from alembic import op

revision = "0028_u2"
down_revision = "0027_u1"
branch_labels = None
depends_on = None

_COLS = (
    ("source_uri", sa.Text()),
    ("imported_at", sa.DateTime(timezone=True)),
    ("import_report_json", sa.JSON()),
)


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if not insp.has_table("model_artifacts"):
        return
    cols = {r[1] for r in conn.exec_driver_sql(
        "PRAGMA table_info(model_artifacts)")}
    for name, typ in _COLS:
        if name not in cols:
            op.add_column("model_artifacts", sa.Column(name, typ,
                                                       nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if not insp.has_table("model_artifacts"):
        return
    cols = {r[1] for r in conn.exec_driver_sql(
        "PRAGMA table_info(model_artifacts)")}
    with op.batch_alter_table("model_artifacts") as b:
        for name, _typ in _COLS:
            if name in cols:
                b.drop_column(name)
