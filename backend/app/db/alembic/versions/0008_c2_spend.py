"""C2.0b: durable spend ledger + budget gates.

Three tables, all amounts INTEGER micro-USD (1 USD = 1_000_000 µUSD):

- `spend_budgets` — caps per scope (session|day|month|lifetime);
  project_id NULL = global. One budget per scope per target via two
  partial unique indexes (SQL NULLs never compare equal).
- `spend_reservations` — one open hold per remote job
  (UNIQUE job_id). open → settled | released; ambiguous remote outcomes
  stay open as worst-case exposure until explicit reconciliation.
- `spend_charges` — append-only settled/actual charges; corrections are
  additional source='adjustment' rows (the only rows allowed negative).

Financial rows are the documented exception to the project_id-FK rule:
they must survive `project.delete` (which cascades generation_jobs) and
remain attributable, so project/job/command refs are snapshot values.
reservation_id keeps a real FK — reservations are never deleted.

DB-level guards (triggers): terminal reservation states are immutable;
charges reject UPDATE/DELETE. Partial unique index enforces exactly one
primary (non-adjustment) settlement charge per reservation — engine and
manual reconciliation can never both land a charge.

NOTE: migration 0001 creates tables from live metadata, so on a FRESH
database these tables/indexes/triggers already exist (the models emit
identical DDL on after_create). Every step is conditional.

Revision: 0008_c2
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_c2"
down_revision = "0007_m4"
branch_labels = None
depends_on = None


_TABLES = (
    """CREATE TABLE IF NOT EXISTS spend_budgets (
        id VARCHAR(36) PRIMARY KEY,
        project_id VARCHAR(36),
        scope VARCHAR(12) NOT NULL,
        limit_micro INTEGER NOT NULL,
        label VARCHAR(160) NOT NULL DEFAULT '',
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        CONSTRAINT ck_budget_limit_nonneg CHECK (limit_micro >= 0),
        CONSTRAINT ck_budget_scope CHECK (scope IN ('session','day','month','lifetime'))
    )""",
    """CREATE TABLE IF NOT EXISTS spend_reservations (
        id VARCHAR(36) PRIMARY KEY,
        job_id VARCHAR(36) NOT NULL UNIQUE,
        command_id VARCHAR(36),
        project_id VARCHAR(36) NOT NULL,
        provider_key VARCHAR(60) NOT NULL,
        environment VARCHAR(12) NOT NULL DEFAULT 'live',
        status VARCHAR(10) NOT NULL DEFAULT 'open',
        reserved_micro INTEGER NOT NULL,
        remote_job_id VARCHAR(120),
        session_id VARCHAR(36) NOT NULL,
        day_key VARCHAR(10) NOT NULL,
        month_key VARCHAR(7) NOT NULL,
        reconcile_note TEXT,
        reconcile_count INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        settled_at DATETIME,
        released_at DATETIME,
        CONSTRAINT ck_res_reserved_nonneg CHECK (reserved_micro >= 0),
        CONSTRAINT ck_res_status CHECK (status IN ('open','settled','released')),
        CONSTRAINT ck_res_env CHECK (environment IN ('live','simulated')),
        CONSTRAINT ck_res_reconcile_nonneg CHECK (reconcile_count >= 0)
    )""",
    """CREATE TABLE IF NOT EXISTS spend_charges (
        id VARCHAR(36) PRIMARY KEY,
        reservation_id VARCHAR(36) NOT NULL REFERENCES spend_reservations(id),
        job_id VARCHAR(36) NOT NULL,
        project_id VARCHAR(36) NOT NULL,
        provider_key VARCHAR(60) NOT NULL,
        environment VARCHAR(12) NOT NULL DEFAULT 'live',
        source VARCHAR(20) NOT NULL,
        micro INTEGER NOT NULL,
        overage_micro INTEGER NOT NULL DEFAULT 0,
        usage_json JSON NOT NULL,
        remote_job_id VARCHAR(120),
        session_id VARCHAR(36) NOT NULL,
        day_key VARCHAR(10) NOT NULL,
        month_key VARCHAR(7) NOT NULL,
        created_at DATETIME NOT NULL,
        CONSTRAINT ck_chg_env CHECK (environment IN ('live','simulated')),
        CONSTRAINT ck_chg_source CHECK (source IN
            ('provider_reported','estimate_at_settle','reconciled',
             'simulated','adjustment')),
        CONSTRAINT ck_chg_micro_sign CHECK (micro >= 0 OR source = 'adjustment'),
        CONSTRAINT ck_chg_overage_nonneg CHECK (overage_micro >= 0)
    )""",
)

_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_spend_budget_global"
    " ON spend_budgets(scope) WHERE project_id IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_spend_budget_project"
    " ON spend_budgets(project_id, scope) WHERE project_id IS NOT NULL",
    # Renamed during review: drop the older (reservation_id, source)
    # variant so re-running on a dev DB lands the stricter index.
    "DROP INDEX IF EXISTS uq_chg_res_source",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_chg_res_primary"
    " ON spend_charges(reservation_id)"
    " WHERE spend_charges.source <> 'adjustment'",
    "CREATE INDEX IF NOT EXISTS ix_res_scope"
    " ON spend_reservations(environment, status, project_id)",
    "CREATE INDEX IF NOT EXISTS ix_res_day"
    " ON spend_reservations(environment, status, day_key)",
    "CREATE INDEX IF NOT EXISTS ix_res_month"
    " ON spend_reservations(environment, status, month_key)",
    "CREATE INDEX IF NOT EXISTS ix_res_session"
    " ON spend_reservations(environment, status, session_id)",
    "CREATE INDEX IF NOT EXISTS ix_chg_project"
    " ON spend_charges(environment, project_id)",
    "CREATE INDEX IF NOT EXISTS ix_chg_day ON spend_charges(environment, day_key)",
    "CREATE INDEX IF NOT EXISTS ix_chg_month ON spend_charges(environment, month_key)",
    "CREATE INDEX IF NOT EXISTS ix_chg_session ON spend_charges(environment, session_id)",
)

_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS trg_spend_res_terminal
       BEFORE UPDATE ON spend_reservations
       WHEN OLD.status <> 'open' AND NEW.status <> OLD.status
       BEGIN SELECT RAISE(ABORT, 'settled/released reservations are terminal'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_spend_chg_no_update
       BEFORE UPDATE ON spend_charges
       BEGIN SELECT RAISE(ABORT, 'spend charges are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_spend_chg_no_delete
       BEFORE DELETE ON spend_charges
       BEGIN SELECT RAISE(ABORT, 'spend charges are append-only'); END""",
)


def upgrade() -> None:
    conn = op.get_bind()
    for ddl in _TABLES + _INDEXES + _TRIGGERS:
        conn.execute(sa.text(ddl))


def downgrade() -> None:
    conn = op.get_bind()
    for name in ("trg_spend_res_terminal", "trg_spend_chg_no_update",
                 "trg_spend_chg_no_delete"):
        conn.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
    for name in ("uq_spend_budget_global", "uq_spend_budget_project",
                 "uq_chg_res_primary", "uq_chg_res_source", "ix_res_scope",
                 "ix_res_day",
                 "ix_res_month", "ix_res_session", "ix_chg_project",
                 "ix_chg_day", "ix_chg_month", "ix_chg_session"):
        conn.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
    for name in ("spend_charges", "spend_reservations", "spend_budgets"):
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {name}"))
