"""C2.0b — durable spend ledger + budget gates.

Accounting model (migration 0008):

- Authoritative money is INTEGER micro-USD (1 USD = 1_000_000 µUSD).
  USD crosses the boundary once, via usd_to_micro() — Decimal(str(x))
  so binary float repr never leaks in, ROUND_CEILING so exposure is
  never understated (for negative values ceiling is toward zero —
  conservative for refunds/credits too).
- Estimates are informational; only a reservation (its ceiling-rounded
  `reserved_micro`, capped by max_cost_usd) holds exposure.
- Consumption per (environment, scope) =
    SUM(reserved_micro WHERE status='open' [scope-match])
  + SUM(micro FROM spend_charges [scope-match])
  Settled reservations consume via their charge — never twice.
- Ambiguous remote outcomes keep the reservation open (worst case)
  until the user reconciles. No silent release path exists.
- environment is derived from the trusted adapter profile
  ('mock' → 'simulated'), NEVER from client input.

Transaction model: admission/settlement run inside the command commit
transaction. The commit endpoint begins spend commands as
BEGIN IMMEDIATE (scoped — see api/routers.py _spend_isolation), so the
budget-check SELECT runs after the write lock is held; concurrent
admissions serialize on SQLite's single-writer lock +
busy_timeout=5000. Job-engine settlement uses the same guarded
transitions inside the engine's per-update commit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import (AppSetting, AuditEvent, GenerationJob,
                           SpendBudget, SpendCharge, SpendReservation,
                           new_id, utcnow)

MICRO_PER_USD = 1_000_000
SCOPES = ("session", "day", "month", "lifetime")
BUDGET_TARGET_GLOBAL = None  # spend_budgets.project_id sentinel


class BudgetError(Exception):
    """422-able budget/admission failure. detail is a dict (unlike
    CommandError's string) so clients can render structured refusals."""

    def __init__(self, detail: dict[str, Any]):
        super().__init__(str(detail))
        self.detail = detail


# ---------- money ----------

def usd_to_micro(usd: Any) -> int:
    """USD → micro-USD integer. Decimal(str) boundary, ROUND_CEILING
    (never understate exposure; for negatives ceiling is toward zero —
    under-crediting refunds is the conservative direction)."""
    try:
        d = Decimal(str(usd))
    except (InvalidOperation, ValueError):
        raise ValueError(f"invalid amount {usd!r}") from None
    if not d.is_finite() or d < 0:
        raise ValueError(f"amount must be a finite nonnegative number, got {usd!r}")
    return int((d * MICRO_PER_USD).to_integral_value(rounding=ROUND_CEILING))


def signed_usd_to_micro(usd: Any) -> int:
    """Signed variant for adjustments/refunds. Same CEILING rule:
    negative values round toward zero (never over-credit)."""
    try:
        d = Decimal(str(usd))
    except (InvalidOperation, ValueError):
        raise ValueError(f"invalid amount {usd!r}") from None
    if not d.is_finite():
        raise ValueError(f"amount must be finite, got {usd!r}")
    return int((d * MICRO_PER_USD).to_integral_value(rounding=ROUND_CEILING))


def micro_to_usd(micro: int) -> float:
    """Display conversion only — never used in admission math."""
    return float(Decimal(micro) / MICRO_PER_USD)


# ---------- periods / session ----------

def period_keys(now: datetime | None = None) -> tuple[str, str]:
    """(day_key 'YYYY-MM-DD', month_key 'YYYY-MM') in UTC. Day scope is
    [00:00Z, +24h); month is [first-of-month 00:00Z, next first)."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")


def billing_session_id(session: Session, app_state: Any | None = None) -> str:
    """Durable id for the CURRENT app run. Minted at startup into
    app.state.billing_session_id AND persisted to app_settings so rows
    stay attributable after restart. Rotates every launch — a 'session'
    budget therefore scopes to this run only; prior runs' rows keep
    their original session_id for history. Falls back to mint+store
    for non-lifespan contexts."""
    sid = getattr(app_state, "billing_session_id", None) if app_state else None
    if sid:
        return sid
    row = session.get(AppSetting, "billing.session")
    if row and (row.value_json or {}).get("id"):
        return row.value_json["id"]
    sid = str(uuid.uuid4())
    session.merge(AppSetting(key="billing.session",
                             value_json={"id": sid,
                                         "started_at": utcnow().isoformat()}))
    session.flush()
    return sid


def rotate_billing_session(session: Session) -> str:
    """Mint a fresh session id at app startup (also called in tests to
    simulate restart)."""
    sid = str(uuid.uuid4())
    session.merge(AppSetting(key="billing.session",
                             value_json={"id": sid,
                                         "started_at": utcnow().isoformat()}))
    session.flush()
    return sid


# ---------- consumption ----------

def _scope_clause(model: Any, scope: str, *, session_id: str,
                  day_key: str, month_key: str):
    if scope == "session":
        return model.session_id == session_id
    if scope == "day":
        return model.day_key == day_key
    if scope == "month":
        return model.month_key == month_key
    return True  # lifetime


def consumed_micro(session: Session, *, environment: str, scope: str,
                   project_id: str | None, session_id: str,
                   day_key: str | None = None,
                   month_key: str | None = None) -> int:
    """Open reservations + all charges for one (env, scope[, project]).
    project_id=None → global consumption (all projects)."""
    if day_key is None or month_key is None:
        day_key, month_key = period_keys()
    clause = _scope_clause(SpendReservation, scope,
                           session_id=session_id,
                           day_key=day_key, month_key=month_key)
    q = select(func.coalesce(func.sum(SpendReservation.reserved_micro), 0)).where(
        SpendReservation.environment == environment,
        SpendReservation.status == "open", clause)
    if project_id is not None:
        q = q.where(SpendReservation.project_id == project_id)
    res_total = session.execute(q).scalar_one()

    clause = _scope_clause(SpendCharge, scope, session_id=session_id,
                           day_key=day_key, month_key=month_key)
    q = select(func.coalesce(func.sum(SpendCharge.micro), 0)).where(
        SpendCharge.environment == environment, clause)
    if project_id is not None:
        q = q.where(SpendCharge.project_id == project_id)
    return res_total + session.execute(q).scalar_one()


# ---------- admission ----------

def admit(session: Session, *, job_id: str, command_id: str | None,
          project_id: str, provider_key: str, environment: str,
          est_usd: Any, max_usd: Any, app_state: Any | None = None,
          now: datetime | None = None) -> SpendReservation:
    """Budget check + open reservation, in the caller's transaction.

    reserved = ceiling(max_usd) when bounded, else ceiling(est_usd).
    Live execution with no enforceable bound (max_usd=None) is refused —
    exposure must be worst-case. Returns an existing reservation for the
    same job (idempotent — commit replays cannot double-book)."""
    existing = session.execute(
        select(SpendReservation).where(SpendReservation.job_id == job_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    if environment not in ("live", "simulated"):
        raise BudgetError({"error": "invalid_environment",
                           "environment": environment})
    if max_usd is not None:
        reserved = usd_to_micro(max_usd)
        bounded = True
    else:
        if environment == "live":
            raise BudgetError({
                "error": "unbounded_exposure",
                "detail": "live cloud execution requires max_cost_usd — "
                          "maximum financial exposure must be bounded"})
        reserved = usd_to_micro(est_usd if est_usd is not None else 0)
        bounded = False

    now = now or datetime.now(timezone.utc)
    day_key, month_key = period_keys(now)
    sid = billing_session_id(session, app_state)

    budgets = session.execute(
        select(SpendBudget).where(
            (SpendBudget.project_id == project_id)
            | SpendBudget.project_id.is_(None))
    ).scalars().all()
    for b in budgets:
        scope_pid = None if b.project_id is None else project_id
        used = consumed_micro(session, environment=environment,
                              scope=b.scope, project_id=scope_pid,
                              session_id=sid,
                              day_key=day_key, month_key=month_key)
        if used + reserved > b.limit_micro:
            raise BudgetError({
                "error": "budget_exceeded",
                "scope": b.scope,
                "target": "global" if b.project_id is None else "project",
                "limit_micro": b.limit_micro, "limit_usd": micro_to_usd(b.limit_micro),
                "consumed_micro": used, "consumed_usd": micro_to_usd(used),
                "requested_micro": reserved,
                "requested_usd": micro_to_usd(reserved),
                "environment": environment,
            })
    res = SpendReservation(
        id=new_id(), job_id=job_id, command_id=command_id,
        project_id=project_id, provider_key=provider_key,
        environment=environment, status="open",
        reserved_micro=reserved, session_id=sid,
        day_key=day_key, month_key=month_key,
    )
    session.add(res)
    session.flush()
    return res


# ---------- transitions (all status-guarded → idempotent) ----------

def settle(session: Session, *, job_id: str, micro: int, source: str,
           overage_micro: int = 0, usage: dict | None = None,
           remote_job_id: str | None = None) -> SpendReservation | None:
    """open → settled + charge row, atomically in caller's txn.
    Replay-safe: the guarded UPDATE no-ops on a second call and no
    charge is inserted. source='adjustment' not allowed here."""
    assert source in ("provider_reported", "estimate_at_settle",
                      "reconciled", "simulated"), source
    rows = session.execute(
        update(SpendReservation)
        .where(SpendReservation.job_id == job_id,
               SpendReservation.status == "open")
        .values(status="settled", settled_at=utcnow(),
                remote_job_id=remote_job_id or SpendReservation.remote_job_id)
        .execution_options(synchronize_session="fetch")
    ).rowcount
    res = session.execute(
        select(SpendReservation).where(SpendReservation.job_id == job_id)
    ).scalar_one_or_none()
    if res is None or rows == 0:
        return res  # nothing to settle — replay or never reserved
    if micro < 0:
        raise ValueError("settlement micro must be nonnegative")
    # The charge inherits the reservation's accounting period — settling
    # on a later date must not shift consumption into a different
    # day/month/session. The reservation's hold is replaced by the
    # charge inside the SAME period keys, so per-period totals are
    # unchanged by settlement.
    session.add(SpendCharge(
        id=new_id(), reservation_id=res.id, job_id=job_id,
        project_id=res.project_id, provider_key=res.provider_key,
        environment=res.environment, source=source, micro=micro,
        overage_micro=overage_micro, usage_json=usage or {},
        remote_job_id=remote_job_id or res.remote_job_id,
        session_id=res.session_id, day_key=res.day_key,
        month_key=res.month_key,
    ))
    session.flush()
    return res


def release(session: Session, *, job_id: str, reason: str) -> SpendReservation | None:
    """open → released (confirmed no-charge). Idempotent."""
    session.execute(
        update(SpendReservation)
        .where(SpendReservation.job_id == job_id,
               SpendReservation.status == "open")
        .values(status="released", released_at=utcnow(),
                reconcile_note=reason)
        .execution_options(synchronize_session="fetch"))
    return session.execute(
        select(SpendReservation).where(SpendReservation.job_id == job_id)
    ).scalar_one_or_none()


def hold_note(session: Session, *, job_id: str, note: str) -> None:
    """Update the reconcile note on an open reservation — used when the
    engine marks an ambiguous remote outcome. Stays open (worst case)."""
    session.execute(
        update(SpendReservation)
        .where(SpendReservation.job_id == job_id,
               SpendReservation.status == "open")
        .values(reconcile_note=note)
        .execution_options(synchronize_session="fetch"))


def apply_action(session: Session, *, job: GenerationJob,
                 action: dict[str, Any]) -> None:
    """Engine-side spend marker from an adapter progress detail.
    Types: settle | release | hold."""
    kind = action.get("type")
    if kind == "settle":
        # USD crosses the boundary exactly once — ceiling-rounded into
        # authoritative micro here, never float arithmetic.
        micro = (int(action["micro"]) if action.get("micro") is not None
                 else usd_to_micro(action.get("usd") or 0))
        settle(session, job_id=job.id,
               micro=micro,
               source=action.get("source", "simulated"),
               overage_micro=int(action.get("overage_micro") or 0),
               usage=action.get("usage") or {},
               remote_job_id=action.get("remote_job_id"))
    elif kind == "release":
        release(session, job_id=job.id,
                reason=action.get("reason", "confirmed no-charge"))
    elif kind == "hold":
        hold_note(session, job_id=job.id,
                  note=action.get("note", "ambiguous remote outcome — "
                                          "exposure held open"))


# ---------- reconcile command ----------

def reconcile(session: Session, *, project_id: str, job_id: str,
              outcome: str, actual_usd: Any = None,
              accept_overage: bool = False,
              note: str | None = None) -> dict[str, Any]:
    """Explicit user reconciliation of an open reservation.

    confirmed_charge    — settle at actual; overage requires
                          accept_overage and is recorded uncapped.
    confirmed_no_charge — release.
    still_unknown       — keep open; note + count (exposure retained).

    Idempotent: if the reservation already left 'open', a replayed same
    outcome is a no-op; a conflicting outcome is a 409."""
    res = session.execute(
        select(SpendReservation).where(SpendReservation.job_id == job_id)
    ).scalar_one_or_none()
    job = session.get(GenerationJob, job_id)
    if job is None or job.project_id != project_id:
        from app.domain.commands import CommandError
        raise CommandError(404, "job not found in this project")
    if res is None:
        from app.domain.commands import CommandError
        raise CommandError(422, "job has no spend reservation")

    prev = res.status
    if prev != "open":
        return {"reservation_id": res.id, "job_id": job_id,
                "previous_status": prev, "status": prev,
                "idempotent_replay": True,
                "detail": "reservation already terminal — no duplicate "
                          "charge or release created"}

    day_key, month_key = period_keys()
    if outcome == "confirmed_no_charge":
        release(session, job_id=job_id,
                reason=note or "user reconciled: confirmed no charge")
    elif outcome == "still_unknown":
        session.execute(
            update(SpendReservation)
            .where(SpendReservation.id == res.id)
            .values(reconcile_note=note or "user checked: outcome still "
                                         "unknown — exposure retained",
                    reconcile_count=SpendReservation.reconcile_count + 1)
            .execution_options(synchronize_session="fetch"))
        session.flush()
    elif outcome == "confirmed_charge":
        if actual_usd is None:
            from app.domain.commands import CommandError
            raise CommandError(422, "confirmed_charge requires actual_usd")
        try:
            micro = signed_usd_to_micro(actual_usd)
        except ValueError as exc:
            from app.domain.commands import CommandError
            raise CommandError(422, str(exc)) from exc
        if micro < 0:
            from app.domain.commands import CommandError
            raise CommandError(422, "actual_usd must be nonnegative")
        overage = max(0, micro - res.reserved_micro)
        if overage and not accept_overage:
            from app.domain.commands import CommandError
            raise CommandError(
                422, f"actual ${micro / MICRO_PER_USD:.6f} exceeds the "
                     f"reservation ${res.reserved_micro / MICRO_PER_USD:.6f} "
                     f"by ${overage / MICRO_PER_USD:.6f} — resend with "
                     "accept_overage true to record the full exposure")
        settle(session, job_id=job_id, micro=micro, source="reconciled",
               overage_micro=overage,
               usage={"user_reported": True,
                      **({"note": note} if note else {})})
    else:
        from app.domain.commands import CommandError
        raise CommandError(422, "outcome must be confirmed_charge, "
                                "confirmed_no_charge, or still_unknown")
    session.refresh(res)
    session.add(AuditEvent(
        id=new_id(), project_id=project_id, event_type="spend.reconciled",
        payload_json={"job_id": job_id, "outcome": outcome,
                      "reservation_id": res.id,
                      "from_status": prev, "to_status": res.status},
    ))
    return {"reservation_id": res.id, "job_id": job_id,
            "from_status": prev, "status": res.status,
            "environment": res.environment}


# ---------- budgets ----------

def set_budget(session: Session, *, project_id: str | None, scope: str,
               limit_usd: Any, label: str = "") -> tuple[SpendBudget, int | None]:
    """Upsert one budget row. Forward-only semantics — open reservations
    are never touched; a lowered limit simply refuses new admissions
    until consumption drains. Returns (row, old_limit_micro)."""
    if scope not in SCOPES:
        from app.domain.commands import CommandError
        raise CommandError(422, f"scope must be one of {SCOPES}")
    try:
        limit = usd_to_micro(limit_usd)
    except ValueError as exc:
        from app.domain.commands import CommandError
        raise CommandError(422, f"invalid limit_usd: {exc}") from exc
    q = select(SpendBudget).where(SpendBudget.scope == scope)
    q = q.where(SpendBudget.project_id == project_id) if project_id \
        else q.where(SpendBudget.project_id.is_(None))
    row = session.execute(q).scalar_one_or_none()
    old = row.limit_micro if row else None
    if row is None:
        row = SpendBudget(id=new_id(), project_id=project_id, scope=scope,
                          limit_micro=limit, label=label)
        session.add(row)
    else:
        row.limit_micro = limit
        if label:
            row.label = label
    session.flush()
    return row, old


def remove_budget(session: Session, *, project_id: str | None,
                  scope: str) -> SpendBudget | None:
    q = select(SpendBudget).where(SpendBudget.scope == scope)
    q = q.where(SpendBudget.project_id == project_id) if project_id \
        else q.where(SpendBudget.project_id.is_(None))
    row = session.execute(q).scalar_one_or_none()
    if row is not None:
        session.delete(row)
        session.flush()
    return row


def add_adjustment(session: Session, *, reservation_id: str,
                   micro: int, note: str,
                   remote_job_id: str | None = None) -> SpendCharge:
    """Signed correction row (refund/credit). Negative is allowed only
    here; magnitude rounding was applied at the USD boundary."""
    res = session.get(SpendReservation, reservation_id)
    if res is None:
        from app.domain.commands import CommandError
        raise CommandError(404, "reservation not found")
    day_key, month_key = period_keys()
    ch = SpendCharge(
        id=new_id(), reservation_id=res.id, job_id=res.job_id,
        project_id=res.project_id, provider_key=res.provider_key,
        environment=res.environment, source="adjustment", micro=micro,
        usage_json={"note": note}, remote_job_id=remote_job_id,
        session_id=res.session_id, day_key=day_key, month_key=month_key,
    )
    session.add(ch)
    session.flush()
    return ch


# ---------- reporting ----------

@dataclass
class ScopeReport:
    scope: str
    limit_micro: int | None
    consumed_micro: int
    open_micro: int
    charged_micro: int


def summary(session: Session, *, project_id: str | None,
            environment: str = "live",
            app_state: Any | None = None) -> dict[str, Any]:
    """Per-scope consumption vs limits. environment is a reporting
    filter only — admission always sums within the job's own env."""
    sid = billing_session_id(session, app_state)
    day_key, month_key = period_keys()
    if project_id:
        budgets = session.execute(
            select(SpendBudget).where(
                (SpendBudget.project_id == project_id)
                | SpendBudget.project_id.is_(None))).scalars().all()
    else:
        budgets = session.execute(select(SpendBudget)).scalars().all()
    rows: list[dict] = []
    for scope in SCOPES:
        limit = None
        for b in budgets:
            if b.scope == scope and b.project_id is None:
                limit = b.limit_micro  # global baseline
        for b in budgets:
            if b.scope == scope and b.project_id == project_id:
                limit = b.limit_micro  # project row overrides global
        open_m = session.execute(
            select(func.coalesce(func.sum(SpendReservation.reserved_micro), 0))
            .where(SpendReservation.environment == environment,
                   SpendReservation.status == "open",
                   _scope_clause(SpendReservation, scope, session_id=sid,
                                 day_key=day_key, month_key=month_key),
                   (SpendReservation.project_id == project_id)
                   if project_id else True)).scalar_one()
        chg_m = session.execute(
            select(func.coalesce(func.sum(SpendCharge.micro), 0))
            .where(SpendCharge.environment == environment,
                   _scope_clause(SpendCharge, scope, session_id=sid,
                                 day_key=day_key, month_key=month_key),
                   (SpendCharge.project_id == project_id)
                   if project_id else True)).scalar_one()
        rows.append({"scope": scope, "limit_micro": limit,
                     "consumed_micro": open_m + chg_m,
                     "open_micro": open_m, "charged_micro": chg_m})
    return {"environment": environment, "session_id": sid,
            "scopes": rows,
            # Raw budget rows so callers can distinguish a project cap
            # from the global baseline it overrides (read-only view).
            "budgets": [{"id": b.id,
                         "target": "project" if b.project_id else "global",
                         "scope": b.scope, "limit_micro": b.limit_micro,
                         "label": b.label} for b in budgets]}


def reservation_list(session: Session, *, project_id: str,
                     limit: int = 200) -> list[dict]:
    """Spend reservations for one project — open holds first, then
    newest. Read-only; powers the reconciliation queue."""
    rows = session.execute(
        select(SpendReservation)
        .where(SpendReservation.project_id == project_id)
        .order_by(SpendReservation.status != "open",
                  SpendReservation.created_at.desc())
        .limit(limit)).scalars().all()
    return [{"id": r.id, "job_id": r.job_id, "command_id": r.command_id,
             "project_id": r.project_id, "provider_key": r.provider_key,
             "environment": r.environment, "status": r.status,
             "reserved_micro": r.reserved_micro,
             "remote_job_id": r.remote_job_id,
             "reconcile_count": r.reconcile_count,
             "reconcile_note": r.reconcile_note,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows]
