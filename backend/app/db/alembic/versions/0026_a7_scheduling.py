"""A7: `generation_jobs.reserved_vram_bytes` — the VRAM reservation a
GPU-bound job holds on its assigned device.

Reservation accounting lives in the scheduler (`_reserved` map); this
column persists each job's claim so the queue view and restart
reconciliation can report what was held. Idempotent + additive.
"""

from alembic import op
import sqlalchemy as sa

revision = "0026_a7"
down_revision = "0025_a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if not insp.has_table("generation_jobs"):
        return
    cols = {r[1] for r in conn.exec_driver_sql(
        "PRAGMA table_info(generation_jobs)")}
    if "reserved_vram_bytes" not in cols:
        op.add_column("generation_jobs", sa.Column(
            "reserved_vram_bytes", sa.Integer(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    if not insp.has_table("generation_jobs"):
        return
    cols = {r[1] for r in conn.exec_driver_sql(
        "PRAGMA table_info(generation_jobs)")}
    if "reserved_vram_bytes" in cols:
        with op.batch_alter_table("generation_jobs") as b:
            b.drop_column("reserved_vram_bytes")
