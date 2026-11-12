"""C2.0a — Mock cloud text generation lifecycle.

Covers the full remote-job contract on `MockRemoteProvider` with ZERO
network egress: intent durability, remote_job_id, polling, success /
failure / cancel / timeout / UNKNOWN, idempotent resubmission, restart
reconciliation, simulated-only provenance (`verified_remote`
unreachable), estimate labeling, eligibility, audit minimization.
"""

from __future__ import annotations

import itertools
import socket
import time
from pathlib import Path

import pytest

from app.db.models import Asset, AuditEvent, GenerationJob
from app.domain.workers import remote as remote_mod
from app.domain.workers.remote import (MockRemoteProvider, RemoteHandle,
                                       RemoteStatus)
from tests.conftest import make_project, wait_for_job

_KEYS = itertools.count()


def _rev(client, pid):
    return client.get(f"/api/projects/{pid}").json()["revision"]


def _commit(client, pid, payload, key_prefix="c2mock"):
    return client.post(
        f"/api/projects/{pid}/commands/commit",
        json={"command_type": "generate.text.cloud", "payload": payload,
              "scope": "project", "expected_revision": _rev(client, pid),
              "idempotency_key": f"{key_prefix}-{next(_KEYS):04d}"})


def _preview(client, pid, payload):
    return client.post(f"/api/projects/{pid}/commands/preview",
                       json={"command_type": "generate.text.cloud",
                             "payload": payload, "scope": "project"})


def _sf(client):
    return client.app.state.session_factory


def _adapter(client):
    return client.app.state.adapter_registry.get("cloud.text.mock")


def _payload(**kw):
    # rytr has a T1 strategy but no REST contract impl, so it keeps
    # routing to the in-memory mock adapter.
    return {"provider_key": "rytr", "prompt": "Write a scene beat.",
            "execution_target": "cloud", **kw}


# ---------- command surface / eligibility ----------

def test_unknown_provider_404(client):
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key="nope"))
    assert r.status_code == 404


def test_provider_without_t1_text_ineligible(client):
    pid = make_project(client)["id"]
    # midjourney: T4/prohibited — no documented T1 text API path.
    r = _commit(client, pid, _payload(provider_key="midjourney"))
    assert r.status_code == 422
    assert "T1" in r.text or "eligible" in r.text


def test_cloud_requires_explicit_target(client):
    pid = make_project(client)["id"]
    missing = _payload()
    del missing["execution_target"]
    for bad in (missing, _payload(execution_target="local"),
                _payload(execution_target="auto")):
        r = _commit(client, pid, bad)
        assert r.status_code == 422, bad
        assert "execution_target" in r.text


def test_prompt_required_and_bounded(client):
    pid = make_project(client)["id"]
    assert _commit(client, pid, _payload(prompt="  ")).status_code == 422
    assert _commit(client, pid, _payload(prompt="x" * 9000)).status_code == 422


def test_unknown_model_id_rejected(client):
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(model_id="not-a-model"))
    assert r.status_code == 422


# ---------- preview: consent + estimate honesty ----------

def test_preview_warns_data_out_estimate_and_mock(client):
    pid = make_project(client)["id"]
    r = _preview(client, pid, _payload())
    assert r.status_code == 200
    warns = " ".join(r.json()["warnings"]).lower()
    assert "sends" in warns and "rytr" in warns            # data-out
    assert "estimate" in warns                            # cost honesty
    assert "mock" in warns and "simulated" in warns       # simulated label
    assert "verified_remote" in warns                     # honest limit


# ---------- happy path: full simulated lifecycle ----------

def test_full_lifecycle_succeeds(client):
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    job = wait_for_job(client, pid, job_id)
    assert job["status"] == "succeeded"

    with _sf(client)() as s:
        row = s.get(GenerationJob, job_id)
        assert row.execution_scope == "remote"
        pj = row.progress_json or {}
        # Intent persisted before submission; remote id recorded.
        assert pj["remote_intent"]["idempotency_key"] == row.idempotency_key
        assert pj["remote_intent"]["simulated"] is True
        assert pj["remote_job_id"]
        assert pj["remote_status"] == "succeeded"

        asset = s.get(Asset, row.output_asset_id)
        prov = asset.provenance_json
        assert prov["remote_job_id"] == pj["remote_job_id"]
        assert prov["remote_id_synthetic"] is True
        assert prov["provenance_confidence"] == "simulated"
        assert "verified_remote" not in str(prov)
        assert asset.kind == "text"


def test_idempotent_commit_no_duplicate_job(client):
    pid = make_project(client)["id"]
    key = f"c2dup-{next(_KEYS):04d}"
    r1 = client.post(f"/api/projects/{pid}/commands/commit",
                     json={"command_type": "generate.text.cloud",
                           "payload": _payload(), "scope": "project",
                           "expected_revision": _rev(client, pid),
                           "idempotency_key": key})
    r2 = client.post(f"/api/projects/{pid}/commands/commit",
                     json={"command_type": "generate.text.cloud",
                           "payload": _payload(), "scope": "project",
                           "expected_revision": _rev(client, pid),
                           "idempotency_key": key})
    assert r1.status_code == 200 and r2.status_code == 200
    # Exactly one durable job row — the replay created nothing new.
    with _sf(client)() as s:
        jobs = s.query(GenerationJob).filter(
            GenerationJob.idempotency_key == f"job:{key}").all()
        assert len(jobs) == 1
        assert r1.json()["job_id"] == jobs[0].id


def test_remote_resubmit_dedupes_by_idem_key():
    # Contract-level: MockRemoteProvider never creates a second remote
    # id for the same idempotency key — the resubmit-proof primitive.
    import asyncio
    mp = MockRemoteProvider()

    async def go():
        from app.domain.workers.remote import SubmissionIntent
        i1 = SubmissionIntent(job_id="j", idempotency_key="job:k1",
                              adapter_id="a", request={})
        h1 = await mp.submit(i1)
        h2 = await mp.submit(i1)  # retry after crash — same key
        assert h1.remote_id == h2.remote_id
    asyncio.run(go())


# ---------- UNKNOWN / failure / timeout / cancel ----------

class _LostSubmit(MockRemoteProvider):
    """submit() raises after 'accepting' — outcome unknown provider-side."""
    async def submit(self, intent):
        self.submission_log.append(intent)  # record the attempt like the parent
        remote_id = "rmt-lost-1"
        self._handles[remote_id] = RemoteHandle(
            self.name, remote_id, RemoteStatus.RUNNING)
        self._by_idem[intent.idempotency_key] = remote_id
        raise ConnectionError("simulated ambiguous submit")


def test_unknown_submit_outcome_never_resubmits(client, monkeypatch):
    pid = make_project(client)["id"]
    prov = _LostSubmit()
    monkeypatch.setattr(_adapter(client), "_remote", prov)
    r = _commit(client, pid, _payload())
    job_id = r.json()["job_id"]
    job = wait_for_job(client, pid, job_id)
    assert job["status"] == "failed"
    assert "unknown" in (job["error"] or "").lower()
    with _sf(client)() as s:
        pj = s.get(GenerationJob, job_id).progress_json or {}
        assert pj["remote_status"] == "unknown"
        assert pj["reconcile_required"] is True
    # Exactly one submission attempt — the failure did NOT trigger a
    # duplicate (billable) resubmission.
    assert len(prov.submission_log) == 1


class _SlowForever(MockRemoteProvider):
    """Stays RUNNING forever — exercises poll timeout and cancel paths."""
    async def submit(self, intent):
        h = await super().submit(intent)
        self._handles[h.remote_id].status = RemoteStatus.RUNNING
        return h


class _Unreachable(MockRemoteProvider):
    async def query(self, remote_id):
        return None  # remote job became unreachable


def test_unknown_poll_outcome_reconciles_not_resubmits(client, monkeypatch):
    pid = make_project(client)["id"]
    prov = _Unreachable()
    monkeypatch.setattr(_adapter(client), "_remote", prov)
    r = _commit(client, pid, _payload())
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert len(prov.submission_log) == 1


class _Failing(MockRemoteProvider):
    async def submit(self, intent):
        h = await super().submit(intent)
        self._handles[h.remote_id].status = RemoteStatus.FAILED
        self._handles[h.remote_id].detail = {"error": "provider exploded"}
        return h


def test_remote_failure_propagates(client, monkeypatch):
    pid = make_project(client)["id"]
    monkeypatch.setattr(_adapter(client), "_remote", _Failing())
    r = _commit(client, pid, _payload())
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert "provider exploded" in (job["error"] or "")


def test_poll_timeout_never_resubmits(client, monkeypatch):
    pid = make_project(client)["id"]
    prov = _SlowForever()
    monkeypatch.setattr(_adapter(client), "_remote", prov)
    monkeypatch.setattr("app.adapters.cloud_mock._POLL_BUDGET_S", 0.25)
    r = _commit(client, pid, _payload())
    job_id = r.json()["job_id"]
    job = wait_for_job(client, pid, job_id, timeout=30)
    assert job["status"] == "failed"
    assert "unknown" in (job["error"] or "").lower()
    assert len(prov.submission_log) == 1


def test_cancel_calls_remote_cancel(client, monkeypatch):
    pid = make_project(client)["id"]
    prov = _SlowForever()
    cancelled: list[str] = []
    orig_cancel = prov.cancel

    async def spy(rid):
        cancelled.append(rid)
        return await orig_cancel(rid)
    monkeypatch.setattr(prov, "cancel", spy)
    monkeypatch.setattr(_adapter(client), "_remote", prov)
    monkeypatch.setattr("app.adapters.cloud_mock._POLL_INTERVAL_S", 0.2)

    r = _commit(client, pid, _payload())
    job_id = r.json()["job_id"]
    # Let it submit + enter the poll loop, then cancel.
    time.sleep(0.8)
    client.post(f"/api/jobs/{job_id}/cancel")
    job = wait_for_job(client, pid, job_id, timeout=30)
    assert job["status"] == "cancelled"
    assert cancelled  # remote cancel was invoked (best-effort)


# ---------- restart reconciliation ----------

def test_remote_scope_marks_needs_reconciliation(client):
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job_id = r.json()["job_id"]
    wait_for_job(client, pid, job_id)  # settle first — no mid-run race
    with _sf(client)() as s:
        row = s.get(GenerationJob, job_id)
        assert row.execution_scope == "remote"
        row.status = "running"  # simulate mid-flight crash state
        s.commit()
        n = client.app.state.job_engine.reconcile_interrupted(s)
        s.flush()
        s.refresh(row)
        assert row.recovery_state == "needs_reconciliation"
        # Non-resumable: marked failed, never re-queued for resubmit.
        assert row.status == "failed"
    assert n == 0


# ---------- zero egress + provenance contract ----------

def test_no_network_egress(client, monkeypatch):
    """Any real socket connect during the entire flow = test failure."""
    def _boom(*a, **kw):
        raise AssertionError("network egress attempted")
    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket.socket, "connect_ex", _boom)
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    assert r.status_code == 200
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "succeeded"


def test_adapter_imports_no_transport():
    src = Path(__import__("app.adapters.cloud_mock",
                          fromlist=["x"]).__file__).read_text("utf-8")
    for banned in ("httpx", "urllib", "requests", "aiohttp",
                   "import subprocess", "websocket"):
        assert banned not in src, banned


def test_provenance_confidence_contract():
    # verified_remote needs BOTH provider_verified and durable live-transport
    # evidence; neither alone suffices, and remote_job_id alone never did.
    _ev = {"transport": "live", "environment": "live",
           "authenticated": True, "endpoint_host": "api.deepseek.com",
           "correlation": {"job_id": "job-1"},
           "response_fingerprint": "f" * 64, "evidence_url": "e" * 64}
    good = {"provider_key": "deepseek", "remote_job_id": "r1",
            "provider_verified": True, "transport_is_live": True,
            "verification_evidence": _ev}
    assert remote_mod.provenance_confidence(good) == "verified_remote"
    # A remote_job_id is optional — synchronous providers may not return one.
    no_id = dict(good); no_id.pop("remote_job_id")
    assert remote_mod.provenance_confidence(no_id) == "verified_remote"
    for missing in ({}, {"remote_job_id": "r1"},
                    {"provider_verified": True},
                    {"provider_verified": True, "transport_is_live": True,
                     "verification_evidence": _ev},
                    {**good, "transport_is_live": False},
                    {**good, "provider_key": "openai"},
                    {"remote_job_id": "r1", "provider_verified": False},
                    good | {"verification_evidence": _ev | {"authenticated": False}},
                    None):
        assert remote_mod.provenance_confidence(missing) == "simulated", missing


def test_no_verified_remote_in_progress_or_provenance(client):
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "succeeded"
    with _sf(client)() as s:
        row = s.get(GenerationJob, job["id"])
        assert "verified_remote" not in str(row.progress_json)
        prov = s.get(Asset, row.output_asset_id).provenance_json
        assert "verified_remote" not in str(prov)


# ---------- audit minimization + capability honesty ----------

def test_audit_minimization_no_prompt_or_secrets(client):
    pid = make_project(client)["id"]
    secret_prompt = "TOPSECRETPROMPT-DO-NOT-LOG"
    r = _commit(client, pid, _payload(prompt=secret_prompt))
    assert r.status_code == 200
    with _sf(client)() as s:
        ev = s.query(AuditEvent).filter(
            AuditEvent.payload_json.like("%cloud%")).order_by(
            AuditEvent.id.desc()).first()
    assert ev is not None
    assert secret_prompt not in str(ev.payload_json)
    assert "api_key" not in str(ev.payload_json).lower()


def test_catalog_capabilities_not_promoted(client):
    # Mock lifecycle success must not alter strategy tiers/caps or any
    # honesty labels — the capability report stays catalog-driven.
    r = _commit(client, make_project(client)["id"], _payload())
    caps = client.get("/api/providers/openai/capabilities").json()
    t1 = [c for c in caps["capabilities"] if c["tier"] == "T1"]
    # T1 stays honestly blocked/not-implemented — a mock run proves
    # nothing about real connectivity; labels stay catalog_documented.
    assert t1, "expected T1 capabilities for openai"
    assert all(c["dispatch"] == [] for c in t1)
    assert all(c["dispatch_blocked"] == "t1_not_implemented" for c in t1)
    assert all(c["verification"] == "catalog_documented" for c in t1)


def test_adapter_profile_is_honest_mock():
    from app.adapters.cloud_mock import MockCloudTextAdapter
    p = MockCloudTextAdapter.profile
    assert p.adapter_kind == "mock"
    assert p.requires_cloud is False
    assert p.cost_model == "free"
    assert p.execution_scope == "remote"


# ---------- persistence & reconciliation review (pre-merge) ----------

class _IntentGate(MockRemoteProvider):
    """Asserts the submission intent is COMMITTED (visible from an
    independent session) before submit() is ever invoked."""
    def __init__(self, sf):
        super().__init__()
        self._sf = sf

    async def submit(self, intent):
        with self._sf() as s:
            row = s.query(GenerationJob).filter(
                GenerationJob.idempotency_key
                == intent.idempotency_key).one_or_none()
            pj = (row.progress_json or {}) if row else {}
            ri = pj.get("remote_intent") or {}
            assert ri.get("idempotency_key") == intent.idempotency_key, (
                "submit invoked before intent was durable — pending "
                "transaction or missing commit")
            assert ri.get("simulated") is True
        return await super().submit(intent)


def test_intent_is_committed_before_submit(client, monkeypatch):
    pid = make_project(client)["id"]
    monkeypatch.setattr(_adapter(client), "_remote",
                        _IntentGate(_sf(client)))
    r = _commit(client, pid, _payload())
    job = wait_for_job(client, pid, r.json()["job_id"])
    # The gate raised inside submit if the intent weren't durable.
    assert job["status"] == "succeeded"


def test_crash_window_ambiguous_reconcile_no_resubmit(client):
    """Crash between submit() success and remote_job_id persistence:
    intent durable, no remote id — reconcile marks it ambiguous, and
    NOTHING resubmits (adapter never re-runs for the row)."""
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job_id = r.json()["job_id"]
    wait_for_job(client, pid, job_id)
    with _sf(client)() as s:
        row = s.get(GenerationJob, job_id)
        # Simulate the crash window: intent persisted, remote id lost.
        pj = dict(row.progress_json or {})
        assert pj.get("remote_intent")
        pj.pop("remote_job_id", None)
        pj.pop("remote_status", None)
        row.progress_json = pj
        row.status = "running"
        s.commit()
        n = client.app.state.job_engine.reconcile_interrupted(s)
        s.flush(); s.refresh(row)
        assert row.recovery_state == "needs_reconciliation"
        assert row.status == "failed"        # terminal, but ambiguous
        assert (row.progress_json or {}).get("remote_job_id") is None
        assert (row.progress_json or {}).get("remote_intent") \
            ["idempotency_key"] == row.idempotency_key
    assert n == 0  # nothing re-queued — no resubmission possible


def test_ambiguous_vs_confirmed_failure_distinguished(client, monkeypatch):
    # Confirmed provider failure records a distinct durable outcome.
    monkeypatch.setattr(_adapter(client), "_remote", _Failing())
    r = _commit(client, make_project(client)["id"], _payload())
    job = wait_for_job(client, r.json()["project_id"]
                     if "project_id" in r.json() else
                     _latest_project(client), r.json()["job_id"])
    assert job["status"] == "failed"
    with _sf(client)() as s:
        pj = s.get(GenerationJob, job["id"]).progress_json or {}
        assert pj["remote_outcome"] == "provider_failed"
        assert pj.get("reconcile_required") is not True

    # Ambiguous outcome records ambiguity — never reads as provider fail.
    prov = _LostSubmit()
    monkeypatch.setattr(_adapter(client), "_remote", prov)
    r2 = _commit(client, make_project(client)["id"], _payload())
    job2 = wait_for_job(client, r2.json().get("project_id") or
                        _latest_project(client), r2.json()["job_id"])
    with _sf(client)() as s:
        pj2 = s.get(GenerationJob, job2["id"]).progress_json or {}
        assert pj2["remote_outcome"] == "ambiguous"
        assert pj2["reconcile_required"] is True
        assert pj2["remote_status"] == "unknown"


def _latest_project(client):
    return client.get("/api/projects").json()[0]["id"]
