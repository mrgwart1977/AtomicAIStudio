"""C2.0b — durable spend ledger + budget gates.

Covers: micro-USD precision, migration 0008 (upgrade/downgrade,
fresh + existing), atomic admission under concurrent independent
connections, budget boundaries/scopes (session/day/month/lifetime UTC),
forward-only budget edits, project-delete retention, terminal-state
triggers and charge append-only guards, simulated/live isolation,
server-derived environment, reconciliation (charge / no-charge /
still-unknown / overage / replay), and the mock settle/release/hold
lifecycle. Zero network egress; no real billing anywhere.
"""

from __future__ import annotations

import itertools
import sqlite3
import threading
import time
from decimal import Decimal

import pytest
from sqlalchemy import inspect, select, text

from app.db.models import (AuditEvent, GenerationJob, Project,
                           SpendBudget, SpendCharge, SpendReservation)
from app.domain import spend
from app.domain.workers.remote import MockRemoteProvider, RemoteStatus
from tests.conftest import make_project, wait_for_job
from tests.test_c2_cloud_mock import (_LostSubmit, _SlowForever,
                                      _adapter, _sf)

_KEYS = itertools.count()


def _rev(client, pid):
    return client.get(f"/api/projects/{pid}").json()["revision"]


def _commit(client, pid, ctype, payload, key_prefix="c2spend"):
    return client.post(
        f"/api/projects/{pid}/commands/commit",
        json={"command_type": ctype, "payload": payload,
              "scope": "project", "expected_revision": _rev(client, pid),
              "idempotency_key": f"{key_prefix}-{next(_KEYS):04d}"})


def _cloud(client, pid, **kw):
    # rytr routes to the mock adapter (no REST impl) — these tests
    # exercise ledger mechanics on the mock lifecycle. Default
    # max_cost_usd keeps a nonzero reservation (rytr has no cost_table).
    kw.setdefault("max_cost_usd", 0.10)
    payload = {"provider_key": "rytr", "prompt": "Write a beat.",
               "execution_target": "cloud", **kw}
    return _commit(client, pid, "generate.text.cloud", payload)


def _set_budget(client, pid, scope, limit_usd, target="project"):
    return _commit(client, pid, "spend.budget_set",
                   {"scope": scope, "limit_usd": limit_usd,
                    "target": target})


def _reservation(client, job_id) -> SpendReservation | None:
    with _sf(client)() as s:
        return s.execute(
            select(SpendReservation).where(
                SpendReservation.job_id == job_id)).scalar_one_or_none()


def _charges(client, job_id) -> list[SpendCharge]:
    with _sf(client)() as s:
        return s.execute(
            select(SpendCharge).where(
                SpendCharge.job_id == job_id)).scalars().all()


# ---------- money precision ----------

def test_micro_conversion_boundaries():
    assert spend.usd_to_micro(0) == 0
    assert spend.usd_to_micro("0.0000004") == 1          # ceil, never understate
    assert spend.usd_to_micro(0.0000004) == 1           # float str-boundary
    assert spend.usd_to_micro("1.9999995") == 2000000
    assert spend.usd_to_micro(1.50) == 1500000
    assert spend.usd_to_micro(Decimal("0.000001")) == 1
    with pytest.raises(ValueError):
        spend.usd_to_micro(-0.01)
    with pytest.raises(ValueError):
        spend.usd_to_micro("not-a-number")
    with pytest.raises(ValueError):
        spend.usd_to_micro(float("inf"))


def test_signed_conversion_conservative():
    # Negative adjustments round toward zero — never over-credit.
    assert spend.signed_usd_to_micro(-1.5) == -1500000
    assert spend.signed_usd_to_micro("-0.0000004") == 0   # ceil → toward zero
    assert spend.signed_usd_to_micro("-2.9999999") == -2999999
    assert spend.signed_usd_to_micro(2.0) == 2000000


# ---------- migration 0008 ----------

def test_spend_tables_indexes_triggers(client):
    insp = inspect(client.app.state.engine)
    tables = set(insp.get_table_names())
    assert {"spend_budgets", "spend_reservations", "spend_charges"} <= tables
    res_idx = {i["name"] for i in insp.get_indexes("spend_reservations")}
    chg_idx = {i["name"] for i in insp.get_indexes("spend_charges")}
    assert {"ix_res_scope", "ix_res_day", "ix_res_month",
            "ix_res_session"} <= res_idx
    assert {"ix_chg_project", "ix_chg_day", "ix_chg_month",
            "ix_chg_session", "uq_chg_res_primary"} <= chg_idx
    with client.app.state.engine.connect() as conn:
        trigs = {r[0] for r in conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
        assert {"trg_spend_res_terminal", "trg_spend_chg_no_update",
                "trg_spend_chg_no_delete"} <= trigs


def test_migration_upgrade_downgrade_cycle(tmp_path):
    """Existing DB (pre-0008 schema): upgrade adds tables; downgrade
    removes; upgrade restores. Idempotent on re-run."""
    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig
    from app.main import _alembic_script_location, run_migrations

    url = f"sqlite:///{tmp_path}/cycle.db"
    run_migrations(url)
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_alembic_script_location()))
    cfg.set_main_option("sqlalchemy.url", url)

    def tables():
        from app.db.session import create_engine_for
        e = create_engine_for(url)
        t = set(inspect(e).get_table_names())
        e.dispose()
        return t

    assert "spend_reservations" in tables()
    alembic_command.downgrade(cfg, "0007_m4")
    assert "spend_reservations" not in tables()
    alembic_command.upgrade(cfg, "head")
    assert "spend_reservations" in tables()
    alembic_command.upgrade(cfg, "head")   # re-run — idempotent
    assert "spend_reservations" in tables()


def test_trigger_guards(client):
    """Direct SQL cannot escape terminal-state or append-only rules."""
    pid = make_project(client)["id"]
    r = _cloud(client, pid)
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "succeeded"        # settled by mock path
    eng = client.app.state.engine
    with eng.connect() as conn:
        with pytest.raises(Exception):       # settled → terminal lock
            conn.execute(text(
                "UPDATE spend_reservations SET status='open' "
                "WHERE status='settled'"))
    with eng.connect() as conn:
        with pytest.raises(Exception):       # charges are append-only
            conn.execute(text(
                "UPDATE spend_charges SET micro=0"))
    with eng.connect() as conn:
        with pytest.raises(Exception):
            conn.execute(text(
                "DELETE FROM spend_charges"))
    with eng.connect() as conn:
        with pytest.raises(Exception):       # negative non-adjustment
            conn.execute(text(
                "INSERT INTO spend_charges (id,reservation_id,job_id,"
                "project_id,provider_key,environment,source,micro,"
                "overage_micro,usage_json,session_id,day_key,month_key,"
                "created_at) VALUES ('x','r','j','p','k','live',"
                "'provider_reported',-5,0,'{}','s','d','m','now')"))


def test_charge_settlement_unique(client):
    """Exactly one primary settlement charge per reservation — a second
    non-adjustment charge is index-rejected even with a DIFFERENT
    source (simulated settle + reconciled settle cannot coexist).
    Adjustment rows remain unrestricted."""
    res = _reservation(client, _commit_cloud_and_wait(client))
    with _sf(client)() as s:
        dup = SpendCharge(
            id="dup", reservation_id=res.id, job_id=res.job_id,
            project_id=res.project_id, provider_key=res.provider_key,
            environment=res.environment, source="reconciled", micro=1,
            usage_json={}, session_id=res.session_id,
            day_key=res.day_key, month_key=res.month_key)
        s.add(dup)
        with pytest.raises(Exception):
            s.flush()
        s.rollback()
        # Multiple adjustment/refund rows on the same reservation are
        # legitimate — the partial unique index does not cover them.
        adj = spend.add_adjustment(s, reservation_id=res.id, micro=-100,
                                   note="partial refund")
        s.commit()
        assert adj.source == "adjustment" and adj.micro == -100


def test_settle_inherits_reservation_period(client):
    """Settling on a later date must NOT transfer consumption into the
    settlement's day/month — the charge inherits the reservation's
    accounting period so per-period totals are unchanged."""
    pid = make_project(client)["id"]
    from datetime import datetime, timezone, timedelta
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    with _sf(client)() as s:
        sid = spend.billing_session_id(s)
        res = spend.admit(s, job_id="period-1", command_id=None,
                          project_id=pid, provider_key="openai",
                          environment="simulated",
                          est_usd=0.4, max_usd=0.5, now=yesterday)
        spend.settle(s, job_id="period-1", micro=400_000,
                     source="simulated")
        res_d, res_m = res.day_key, res.month_key
        s.commit()
    with _sf(client)() as s:
        chg = s.execute(select(SpendCharge).where(
            SpendCharge.job_id == "period-1")).scalar_one()
        assert chg.day_key == res_d              # reservation's period,
        assert chg.month_key == res_m            # not settlement date's
        today_d, today_m = spend.period_keys()
        # Today's day-scope consumption does NOT include the settled
        # charge — it stayed attributed to the reservation's day.
        today_used = spend.consumed_micro(
            s, environment="simulated", scope="day", project_id=pid,
            session_id=sid, day_key=today_d, month_key=today_m)
        res_day_used = spend.consumed_micro(
            s, environment="simulated", scope="day", project_id=pid,
            session_id=sid, day_key=res_d, month_key=res_m)
        assert today_used == 0 or res_d == today_d
        assert res_day_used == 400_000           # charge replaced hold,
                                                  # same period, no double


def _commit_cloud_and_wait(client):
    pid = make_project(client)["id"]
    r = _cloud(client, pid)
    return wait_for_job(client, pid, r.json()["job_id"])["id"]


# ---------- admission / budgets ----------

def test_reservation_created_open_and_settles(client):
    pid = make_project(client)["id"]
    r = _cloud(client, pid)
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "succeeded"
    res = _reservation(client, job["id"])
    assert res is not None
    assert res.environment == "simulated"      # adapter-derived, never client
    assert res.status == "settled"
    assert res.reserved_micro >= 1
    charges = _charges(client, job["id"])
    assert len(charges) == 1
    assert charges[0].source == "simulated"
    assert charges[0].environment == "simulated"
    assert charges[0].micro <= res.reserved_micro


def test_budget_exact_boundary_admits_then_refuses(client):
    pid = make_project(client)["id"]
    # $1 project lifetime budget; mock est is tiny — set explicit
    # reservation amount via a unit-level admit to control the boundary.
    assert _set_budget(client, pid, "lifetime", 1.0).status_code == 200
    with _sf(client)() as s:
        r1 = spend.admit(s, job_id="edge-1", command_id=None,
                         project_id=pid, provider_key="openai",
                         environment="simulated",
                         est_usd=0.75, max_usd=1.0)
        s.commit()
        assert r1.reserved_micro == 1_000_000  # ceil(max_usd) — worst case
    # consumed == limit now; even 1µ more must refuse.
    with _sf(client)() as s:
        with pytest.raises(spend.BudgetError) as ei:
            spend.admit(s, job_id="edge-2", command_id=None,
                        project_id=pid, provider_key="openai",
                        environment="simulated", est_usd=0.01,
                        max_usd=0.01)
        assert ei.value.detail["error"] == "budget_exceeded"
        assert ei.value.detail["scope"] == "lifetime"


def test_budget_refusal_via_commit(client):
    pid = make_project(client)["id"]
    assert _set_budget(client, pid, "lifetime", 0.000000).status_code == 200
    r = _cloud(client, pid)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "budget_exceeded"
    # No reservation left behind — admission+enqueue rolled back together.
    with _sf(client)() as s:
        assert s.execute(select(SpendReservation)).scalars().all() == []


def test_admit_idempotent_per_job(client):
    pid = make_project(client)["id"]
    with _sf(client)() as s:
        a = spend.admit(s, job_id="dup-job", command_id=None,
                        project_id=pid, provider_key="openai",
                        environment="simulated", est_usd=0.5, max_usd=0.5)
        b = spend.admit(s, job_id="dup-job", command_id=None,
                        project_id=pid, provider_key="openai",
                        environment="simulated", est_usd=0.5, max_usd=0.5)
        s.commit()
        assert a.id == b.id
    with _sf(client)() as s:
        rows = s.execute(select(SpendReservation).where(
            SpendReservation.job_id == "dup-job")).scalars().all()
        assert len(rows) == 1


def test_global_and_project_budgets_both_enforced(client):
    pid = make_project(client)["id"]
    assert _set_budget(client, pid, "lifetime", 100.0,
                       target="project").status_code == 200
    assert _set_budget(client, pid, "lifetime", 0.5,
                       target="global").status_code == 200
    with _sf(client)() as s:
        with pytest.raises(spend.BudgetError) as ei:
            spend.admit(s, job_id="g-1", command_id=None, project_id=pid,
                        provider_key="openai", environment="simulated",
                        est_usd=0.6, max_usd=0.6)
        assert ei.value.detail["target"] == "global"


def test_budget_lower_is_forward_only(client):
    pid = make_project(client)["id"]
    assert _set_budget(client, pid, "lifetime", 10.0).status_code == 200
    with _sf(client)() as s:
        spend.admit(s, job_id="fwd-1", command_id=None, project_id=pid,
                    provider_key="openai", environment="simulated",
                    est_usd=8.0, max_usd=8.0)
        s.commit()
    # Lower below consumption — open reservation untouched, new admits fail.
    r = _set_budget(client, pid, "lifetime", 5.0)
    assert r.status_code == 200
    with _sf(client)() as s:
        res = s.execute(select(SpendReservation).where(
            SpendReservation.job_id == "fwd-1")).scalar_one()
        assert res.status == "open" and res.reserved_micro == 8_000_000
        with pytest.raises(spend.BudgetError):
            spend.admit(s, job_id="fwd-2", command_id=None,
                        project_id=pid, provider_key="openai",
                        environment="simulated", est_usd=0.1, max_usd=0.1)


def test_concurrent_admission_serializes(client):
    """Two independent connections race admits where only one fits —
    BEGIN IMMEDIATE serializes them; exactly one wins."""
    pid = make_project(client)["id"]
    assert _set_budget(client, pid, "lifetime", 1.0).status_code == 200
    sf = _sf(client)
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def attempt(tag: str):
        with sf() as s:
            barrier.wait(timeout=10)
            # Race the lock: winner commits fast; loser's BEGIN retries
            # under busy_timeout, then sees post-commit consumption.
            s.execute(text("BEGIN IMMEDIATE"))
            try:
                spend.admit(s, job_id=f"race-{tag}", command_id=None,
                            project_id=pid, provider_key="openai",
                            environment="simulated",
                            est_usd=0.75, max_usd=0.75)
                s.commit()
                results[tag] = "admitted"
            except spend.BudgetError as exc:
                s.rollback()
                results[tag] = exc.detail["error"]

    threads = [threading.Thread(target=attempt, args=(t,), daemon=True)
               for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results.values()) == ["admitted", "budget_exceeded"]


def test_unbounded_live_refused(client):
    """Live execution with no enforceable bound is refused outright."""
    with _sf(client)() as s:
        with pytest.raises(spend.BudgetError) as ei:
            spend.admit(s, job_id="live-1", command_id=None,
                        project_id="p", provider_key="openai",
                        environment="live", est_usd=0.5, max_usd=None)
        assert ei.value.detail["error"] == "unbounded_exposure"


def test_environment_derived_not_client_controlled(client):
    """A client-sent simulated/environment flag cannot alter the
    adapter-derived environment."""
    pid = make_project(client)["id"]
    r = _cloud(client, pid, environment="live", simulated=False)
    job = wait_for_job(client, pid, r.json()["job_id"])
    res = _reservation(client, job["id"])
    assert res.environment == "simulated"


# ---------- lifecycle outcomes ----------

def test_ambiguous_outcome_holds_reservation(client, monkeypatch):
    monkeypatch.setattr(_adapter(client), "_remote", _LostSubmit())
    pid = make_project(client)["id"]
    r = _cloud(client, pid)
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    res = _reservation(client, job["id"])
    assert res.status == "open"               # worst-case held
    assert "unknown" in (res.reconcile_note or "")
    assert _charges(client, job["id"]) == []


def test_provider_failure_holds_open(client, monkeypatch):
    from app.domain.workers.remote import RemoteHandle

    class _Failing(MockRemoteProvider):
        async def query(self, remote_id):
            h = self._handles[remote_id]
            return RemoteHandle(self.name, remote_id, RemoteStatus.FAILED,
                                cost_usd=h.cost_usd,
                                detail={"error": "provider exploded"})
    monkeypatch.setattr(_adapter(client), "_remote", _Failing())
    pid = make_project(client)["id"]
    r = _cloud(client, pid)
    job = wait_for_job(client, pid, r.json()["job_id"])
    res = _reservation(client, job["id"])
    assert res.status == "open"               # billing unknown → held
    assert "billing outcome unknown" in res.reconcile_note


def test_confirmed_cancel_releases(client, monkeypatch):
    prov = _SlowForever()
    monkeypatch.setattr(_adapter(client), "_remote", prov)
    pid = make_project(client)["id"]
    r = _cloud(client, pid)
    job_id = r.json()["job_id"]
    time.sleep(0.8)                          # let it enter the poll loop
    client.post(f"/api/jobs/{job_id}/cancel")
    job = wait_for_job(client, pid, job_id, timeout=30)
    assert job["status"] == "cancelled"
    res = _reservation(client, job_id)
    assert res.status == "released"           # confirmed no-charge
    assert _charges(client, job_id) == []


# ---------- reconciliation ----------

def _held_job(client, monkeypatch):
    """A job left with an open reservation via lost-submit."""
    monkeypatch.setattr(_adapter(client), "_remote", _LostSubmit())
    pid = make_project(client)["id"]
    r = _cloud(client, pid)
    return pid, wait_for_job(client, pid, r.json()["job_id"])["id"]


def test_reconcile_confirmed_charge_and_overage(client, monkeypatch):
    pid, job_id = _held_job(client, monkeypatch)
    res = _reservation(client, job_id)
    # Charge above the reservation without accept_overage → refused.
    r = _commit(client, pid, "spend.reconcile",
                {"job_id": job_id, "outcome": "confirmed_charge",
                 "actual_usd": spend.micro_to_usd(res.reserved_micro) + 0.25})
    assert r.status_code == 422
    assert "accept_overage" in r.json()["detail"]
    res = _reservation(client, job_id)
    assert res.status == "open"               # unchanged
    # With explicit acceptance the full amount is recorded uncapped.
    r = _commit(client, pid, "spend.reconcile",
                {"job_id": job_id, "outcome": "confirmed_charge",
                 "actual_usd": spend.micro_to_usd(res.reserved_micro) + 0.25,
                 "accept_overage": True})
    assert r.status_code == 200, r.text
    res = _reservation(client, job_id)
    assert res.status == "settled"
    charge = _charges(client, job_id)[0]
    assert charge.source == "reconciled"
    assert charge.overage_micro == 250000
    assert charge.micro == res.reserved_micro + 250000


def test_reconcile_no_charge_and_replay(client, monkeypatch):
    pid, job_id = _held_job(client, monkeypatch)
    r = _commit(client, pid, "spend.reconcile",
                {"job_id": job_id, "outcome": "confirmed_no_charge"})
    assert r.status_code == 200
    res = _reservation(client, job_id)
    assert res.status == "released"
    # Replay — a second reconcile is an idempotent no-op, not a 409.
    r = _commit(client, pid, "spend.reconcile",
                {"job_id": job_id, "outcome": "confirmed_no_charge"})
    assert r.status_code == 200
    assert _charges(client, job_id) == []


def test_reconcile_still_unknown_keeps_open(client, monkeypatch):
    pid, job_id = _held_job(client, monkeypatch)
    r = _commit(client, pid, "spend.reconcile",
                {"job_id": job_id, "outcome": "still_unknown",
                 "note": "provider shows nothing yet"})
    assert r.status_code == 200
    res = _reservation(client, job_id)
    assert res.status == "open"
    assert res.reconcile_count == 1
    assert res.reconcile_note == "provider shows nothing yet"


def test_reconcile_terminal_conflict_no_duplicate(client, monkeypatch):
    pid, job_id = _held_job(client, monkeypatch)
    _commit(client, pid, "spend.reconcile",
            {"job_id": job_id, "outcome": "confirmed_charge",
             "actual_usd": 1.0, "accept_overage": True})
    # Second reconcile claiming a different outcome can't produce a
    # second charge or reopen anything.
    r = _commit(client, pid, "spend.reconcile",
                {"job_id": job_id, "outcome": "confirmed_no_charge"})
    assert r.status_code == 200               # idempotent no-op
    assert r.json().get("status") != "failed"
    res = _reservation(client, job_id)
    assert res.status == "settled"
    assert len(_charges(client, job_id)) == 1


def test_reconcile_wrong_project_rejected(client, monkeypatch):
    pid, job_id = _held_job(client, monkeypatch)
    other = make_project(client)["id"]
    r = _commit(client, other, "spend.reconcile",
                {"job_id": job_id, "outcome": "confirmed_no_charge"})
    assert r.status_code == 404


# ---------- scopes / periods / sessions ----------

def test_day_month_keys_utc(client):
    pid = make_project(client)["id"]
    r = _cloud(client, pid)
    job = wait_for_job(client, pid, r.json()["job_id"])
    res = _reservation(client, job["id"])
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    assert res.day_key == now.strftime("%Y-%m-%d")
    assert res.month_key == now.strftime("%Y-%m")


def test_session_rotation_isolates_scope(client):
    """Restart rotates billing.session: ONLY the session scope resets.
    Day/month/lifetime budgets keep counting the prior run's charges
    and open reservations — rows stay attributed to their original
    session for history."""
    pid = make_project(client)["id"]
    with _sf(client)() as s:
        old_sid = spend.billing_session_id(s)
        spend.admit(s, job_id="sess-1", command_id=None, project_id=pid,
                    provider_key="openai", environment="simulated",
                    est_usd=2.0, max_usd=2.0)
        spend.settle(s, job_id="sess-1", micro=1_500_000,
                     source="simulated")
        s.commit()
        # Restart = new session id; old rows stay attributed to old sid.
        new_sid = spend.rotate_billing_session(s)
        s.commit()
        assert new_sid != old_sid
        for scope in ("session", "day", "month", "lifetime"):
            used_old = spend.consumed_micro(
                s, environment="simulated", scope=scope,
                project_id=pid, session_id=old_sid)
            used_new = spend.consumed_micro(
                s, environment="simulated", scope=scope,
                project_id=pid, session_id=new_sid)
            if scope == "session":
                assert used_old == 1_500_000 and used_new == 0
            else:
                # Only the current session id is passed to the query —
                # non-session scopes ignore it and keep counting.
                assert used_new == used_old == 1_500_000


def test_summary_endpoint_environments(client, monkeypatch):
    pid, job_id = _held_job(client, monkeypatch)   # ambiguous → open hold
    _set_budget(client, pid, "lifetime", 5.0)
    # A nonzero simulated charge via the reconcile path — rytr has no
    # cost_table so the mock settle would record zero; the point here
    # is environment separation, not the charge source.
    _commit(client, pid, "spend.reconcile",
            {"job_id": job_id, "outcome": "confirmed_charge",
             "actual_usd": 0.01})
    live = client.get(
        f"/api/projects/{pid}/spend/summary").json()
    sim = client.get(
        f"/api/projects/{pid}/spend/summary?environment=simulated").json()
    live_life = next(s for s in live["scopes"] if s["scope"] == "lifetime")
    sim_life = next(s for s in sim["scopes"] if s["scope"] == "lifetime")
    assert live_life["limit_micro"] == 5_000_000
    assert live_life["consumed_micro"] == 0    # mock charges are simulated
    assert sim_life["consumed_micro"] > 0
    bad = client.get(
        f"/api/projects/{pid}/spend/summary?environment=bogus")
    assert bad.status_code == 422


# ---------- retention ----------

def test_project_delete_retains_ledger(client):
    pid = make_project(client)["id"]
    r = _cloud(client, pid)
    job = wait_for_job(client, pid, r.json()["job_id"])
    res_id = _reservation(client, job["id"]).id
    r = client.post(f"/api/projects/{pid}/commands/commit",
                    json={"command_type": "project.delete",
                          "payload": {"confirm_name": "Test Project"},
                          "scope": "project",
                          "expected_revision": _rev(client, pid),
                          "idempotency_key": f"del-{next(_KEYS):04d}"})
    assert r.status_code == 200, r.text
    with _sf(client)() as s:
        assert s.get(Project, pid) is None
        assert s.get(GenerationJob, job["id"]) is None
        res = s.get(SpendReservation, res_id)
        assert res is not None                  # history survives
        assert res.project_id == pid            # snapshot attribution
        assert s.execute(select(SpendCharge).where(
            SpendCharge.job_id == job["id"])).scalar_one() is not None


# ---------- audit / minimization ----------

def test_spend_audit_minimization(client):
    pid = make_project(client)["id"]
    _set_budget(client, pid, "lifetime", 2.0)
    r = _cloud(client, pid)
    job = wait_for_job(client, pid, r.json()["job_id"])
    with _sf(client)() as s:
        events = s.execute(select(AuditEvent).where(
            AuditEvent.event_type == "command.committed")).scalars().all()
        blob = repr([e.payload_json for e in events])
        assert "Write a beat" not in blob          # no prompts
        assert "api_key" not in blob.lower()
        assert "secret" not in blob.lower()
    res = _reservation(client, job["id"])
    assert res.reserved_micro > 0


def test_budget_commands_audited(client):
    pid = make_project(client)["id"]
    _set_budget(client, pid, "lifetime", 9.0)
    with _sf(client)() as s:
        ev = s.execute(select(AuditEvent).where(
            AuditEvent.event_type == "command.committed").order_by(
            AuditEvent.created_at.desc())).scalars().first()
    cmds = client.get(f"/api/projects/{pid}/commands").json()
    spend_cmds = [c for c in cmds if c["command_type"].startswith("spend.")]
    assert spend_cmds and spend_cmds[0]["status"] == "committed"


# ---------- settlement concurrency ----------

def test_settle_vs_reconcile_race_exactly_one_charge(client, monkeypatch):
    """Engine-path settle and a manual spend.reconcile race on the same
    open reservation from INDEPENDENT connections. The guarded
    transition + single-writer lock mean exactly one settlement lands;
    the loser observes the terminal state and inserts nothing. The
    partial unique index is the last line of defense."""
    pid, job_id = _held_job(client, monkeypatch)   # open hold, failed job
    sf = _sf(client)
    barrier = threading.Barrier(2)
    outcome: dict[str, object] = {}

    def engine_path():
        # Engine settle runs in a DEFERRED txn — the guarded UPDATE is
        # its first write and takes the lock there.
        with sf() as s:
            barrier.wait(timeout=10)
            spend.settle(s, job_id=job_id, micro=1_000_000,
                         source="simulated")
            s.commit()
            outcome["engine"] = "committed"

    def reconcile_path():
        # Command path holds BEGIN IMMEDIATE from before its reads.
        with sf() as s:
            barrier.wait(timeout=10)
            s.execute(text("BEGIN IMMEDIATE"))
            try:
                r = spend.reconcile(
                    s, project_id=pid, job_id=job_id,
                    outcome="confirmed_charge", actual_usd=2.0,
                    accept_overage=True)
                s.commit()
                outcome["reconcile"] = r.get("status")
            except Exception as exc:                # busy-timeout or
                s.rollback()                        # guard — still safe
                outcome["reconcile"] = f"error:{type(exc).__name__}"

    threads = [threading.Thread(target=engine_path, daemon=True),
               threading.Thread(target=reconcile_path, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    charges = _charges(client, job_id)
    assert len(charges) == 1                        # exactly one settle
    assert charges[0].source in ("simulated", "reconciled")
    assert _reservation(client, job_id).status == "settled"


# ---------- delete safety ----------

def test_project_delete_refused_with_open_reservation(client, monkeypatch):
    """Deleting a project with an unresolved spend hold is refused —
    the financial rows outlive the project, so the hold would count
    against global budgets forever with no reconcile route."""
    pid, job_id = _held_job(client, monkeypatch)   # open hold
    name = client.get(f"/api/projects/{pid}").json()["name"]
    r = client.post(f"/api/projects/{pid}/commands/commit",
                    json={"command_type": "project.delete",
                          "payload": {"confirm_name": name},
                          "scope": "project",
                          "expected_revision": _rev(client, pid),
                          "idempotency_key": f"delhold-{next(_KEYS)}"})
    assert r.status_code == 422
    assert "spend reservation" in r.json()["detail"]
    # Reconcile the hold away → deletion proceeds.
    r2 = _commit(client, pid, "spend.reconcile",
                 {"job_id": job_id, "outcome": "confirmed_no_charge"})
    assert r2.status_code == 200
    r3 = client.post(f"/api/projects/{pid}/commands/commit",
                     json={"command_type": "project.delete",
                           "payload": {"confirm_name": name},
                           "scope": "project",
                           "expected_revision": _rev(client, pid),
                           "idempotency_key": f"delok-{next(_KEYS)}"})
    assert r3.status_code == 200, r3.text


# ---------- read-only reporting surface (C2.0d UI) ----------

def test_summary_lists_budget_rows_with_target(client):
    """The UI needs to distinguish a project cap from the global
    baseline it overrides — summary carries raw budget rows."""
    pid = make_project(client)["id"]
    assert _set_budget(client, pid, "day", 1.5).status_code == 200
    assert _set_budget(client, pid, "month", 9.0,
                       target="global").status_code == 200
    s = client.get(f"/api/projects/{pid}/spend/summary"
                   "?environment=simulated").json()
    by_key = {(b["target"], b["scope"]): b for b in s["budgets"]}
    assert by_key[("project", "day")]["limit_micro"] == 1_500_000
    assert by_key[("global", "month")]["limit_micro"] == 9_000_000
    # Effective limits: project day overrides nothing global, global
    # month applies as baseline.
    scopes = {r["scope"]: r for r in s["scopes"]}
    assert scopes["day"]["limit_micro"] == 1_500_000
    assert scopes["month"]["limit_micro"] == 9_000_000


def test_reservations_endpoint_open_first_and_fields(client, monkeypatch):
    """Read-only reservation listing for the reconciliation queue —
    open holds sort first and carry job/provider/exposure."""
    pid, job_id = _held_job(client, monkeypatch)     # ambiguous → open hold
    rows = client.get(f"/api/projects/{pid}/spend/reservations").json()
    assert rows and rows[0]["job_id"] == job_id
    row = rows[0]
    assert row["status"] == "open"
    assert row["provider_key"] == "rytr"
    assert row["environment"] == "simulated"
    assert row["reserved_micro"] > 0
    # Reconcile → row becomes terminal but stays listed.
    _commit(client, pid, "spend.reconcile",
            {"job_id": job_id, "outcome": "confirmed_no_charge"})
    rows2 = client.get(f"/api/projects/{pid}/spend/reservations").json()
    assert rows2[0]["status"] == "released"


def test_reservations_endpoint_project_isolated(client):
    """Reservations never leak across projects."""
    pid1 = make_project(client)["id"]
    pid2 = make_project(client)["id"]
    r = _cloud(client, pid1)
    wait_for_job(client, pid1, r.json()["job_id"])
    rows2 = client.get(f"/api/projects/{pid2}/spend/reservations").json()
    assert rows2 == []
