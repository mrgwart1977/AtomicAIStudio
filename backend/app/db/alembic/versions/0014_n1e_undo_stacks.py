"""N1-E: per-timeline undo/redo stacks.

`timelines` gains `undo_stack_json` / `redo_stack_json` — JSON lists
of {rev, label} entries pointing at entity_revisions snapshots. The
stacks are append-only pointers managed inside the same transaction
as every timeline mutation; no separate history database.

Idempotent: columns are added only when absent.

Revision: 0014_n1e
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_n1e"
down_revision = "0013_n1a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind())
            .get_columns("timelines")}
    if "undo_stack_json" not in cols:
        op.add_column("timelines", sa.Column(
            "undo_stack_json", sa.JSON(), nullable=True))
    if "redo_stack_json" not in cols:
        op.add_column("timelines", sa.Column(
            "redo_stack_json", sa.JSON(), nullable=True))
    op.execute("UPDATE timelines SET undo_stack_json='[]' "
               "WHERE undo_stack_json IS NULL")
    op.execute("UPDATE timelines SET redo_stack_json='[]' "
               "WHERE redo_stack_json IS NULL")


def downgrade() -> None:
    op.drop_column("timelines", "redo_stack_json")
    op.drop_column("timelines", "undo_stack_json")
