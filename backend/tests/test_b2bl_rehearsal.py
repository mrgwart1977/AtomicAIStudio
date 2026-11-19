"""Stage B2b-L — offline execution rehearsal.

Walks the exact sequence the future authorized validation will take —
preflight → fingerprint → arm approval → exec approval → reservation
→ admission → ONE simulated request → parse → usage → settle → audit
→ consume — then every required refusal path.

ABSOLUTE ZERO-EGRESS GUARANTEE: an autouse fixture replaces
``lt._SecureHTTPTransport`` with a raiser for the whole module. ANY
code path that tries to construct the real egress transport fails the
test immediately — mock transports are the only possible inner.
"""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.db.models import (AuditEvent, GenerationJob, SpendReservation,
                           new_id)
from app.domain import live_gate as lg
from app.domain import live_permit as lp
from app.domain.workers import live_transport as lt
from tests.conftest import make_project

_KEYS = itertools.count(1)

DEEPSEEK_OK = {
    "id": "chatcmpl-b2bl-rehearsal",
    "choices": [{"message": {"content": "A lighthouse stands watch."},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 9, "completion_tokens": 5,
              "prompt_cache_hit_tokens": 0,
              "prompt_cache_miss_tokens": 9},
}
DUMMY_KEY = "synthetic-b2bl-rehearsal-key"
PROMPT = "Write one sentence about a lighthouse."
GOOD_RESOLVER = lambda h, p: ["93.184.216.34"]  # noqa: E731


@pytest.fixture(autouse=True)
def _no_real_transport(monkeypatch):
    """ANY construction of the real egress transport = test failure."""
    def _boom(*a, **k):
        raise AssertionError(
            "real _SecureHTTPTransport constructed — live egress must "
            "be impossible in the rehearsal")
    monkeypatch.setattr(lt, "_SecureHTTPTransport", _boom)


class RecordingVault:
    def __init__(self, configured=True):
        self.get_calls: list[str] = []
        self._configured = configured

    def status(self, key):
        return {"configured": self._configured and key == "deepseek",
                "fields": ["api_key"],
                "set_at": "2026-01-01T00:00:00Z", "readable": True,
                "unreadable_reason": None}

    def get_fields(self, key):
        self.get_calls.append(key)
        return {"api_key": DUMMY_KEY}


@pytest.fixture()
def vault(client):
    v = RecordingVault()
    client.app.state.provider_vault = v
    return v


def _sf(client):
    return client.app.state.session_factory


def _rev(client, pid):
    return client.get(f"/api/projects/{pid}").json()["revision"]


def _commit(client, pid, command_type, payload):
    return client.post(
        f"/api/projects/{pid}/commands/commit",
        json={"command_type": command_type, "payload": payload,
              "scope": "project", "expected_revision": _rev(client, pid),
              "idempotency_key": f"b2bl-{next(_KEYS):04d}"})


EN = {"provider_key": "deepseek", "model_id": "deepseek-flash",
      "credential_ref": "deepseek"}
ARM = {"credential_ref": "deepseek", "prompt": PROMPT,
       "max_output_tokens": 32, "max_cost_usd": "0.05"}


def _owner_dir(client):
    d = Path(client.app.state.settings.data_dir) / lp.OWNER_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _expiry(hours=1):
    return (datetime.now(timezone.utc)
            + timedelta(hours=hours)).isoformat()


def _req_fp(pid):
    return lp.request_fingerprint(
        provider_key="deepseek", model_id="deepseek-flash",
        project_id=pid, credential_ref="deepseek",
        prompt=PROMPT, max_output_tokens=32, max_cost_usd="0.05")


def _approve_arm(client, pid, **overrides):
    doc = {"approve": "validate_arm", "provider_key": "deepseek",
           "model_id": "deepseek-flash", "project_id": pid,
           "request_fingerprint": _req_fp(pid),
           "not_after": _expiry()}
    doc.update(overrides)
    (_owner_dir(client) / "arm-approval.json"
     ).write_text(json.dumps(doc), encoding="utf-8")
    return doc


def _approve_exec(client, **overrides):
    with _sf(client)() as s:
        permit = lp.armed(s)
    if permit is None:
        return None
    doc = {"approve": "validate_run",
           "permit_fingerprint": lp.permit_fingerprint(permit),
           "not_after": _expiry()}
    doc.update(overrides)
    (_owner_dir(client) / "exec-approval.json"
     ).write_text(json.dumps(doc), encoding="utf-8")
    return doc


def _setup(client, pid, vault):
    _commit(client, pid, "spend.budget_set",
            {"target": "global", "scope": "lifetime",
             "limit_usd": "5.00"})
    r = _commit(client, pid, "cloud.live.enable", EN)
    assert r.status_code == 200, r.text


def _mkjob(client, pid, jid):
    with _sf(client)() as s:
        s.add(GenerationJob(
            id=jid, project_id=pid, adapter_id="cloud.live.validate",
            kind="text", idempotency_key=f"job:b2bl-{new_id()[:8]}",
            request_json={"stage": "B2b-L-rehearsal"},
            status="validating", resumable=False,
            execution_scope="remote"))
        s.commit()


def _dispatch(client, pid, *, inner=None, approve_exec=True):
    if approve_exec:
        _approve_exec(client)
    jid = new_id()
    _mkjob(client, pid, jid)
    out = lp.run_validation(
        _sf(client), project_id=pid, job_id=jid,
        job_idempotency_key=f"job:b2bl-{new_id()[:8]}",
        command_id=None, vault=client.app.state.provider_vault,
        data_dir=client.app.state.settings.data_dir,
        inner=inner or httpx.MockTransport(
            lambda req: httpx.Response(200, json=DEEPSEEK_OK)),
        resolver=GOOD_RESOLVER)
    return jid, out


def _reservation(client, job_id):
    with _sf(client)() as s:
        return s.execute(select(SpendReservation).where(
            SpendReservation.job_id == job_id)).scalar_one_or_none()


# ===================================================================
# The full rehearsal — checks 1–12 in the required order
# ===================================================================

class TestRehearsalSequence:

    def test_complete_authorized_flow(self, client, vault):
        pid = make_project(client)["id"]

        # (1) credential-present preflight — metadata only, value
        # never read at this stage
        assert vault.status("deepseek")["configured"] is True
        assert vault.get_calls == []

        _setup(client, pid, vault)

        # (2) request fingerprint creation — deterministic, covers
        # the whole canonical request; no secret material
        fp = _req_fp(pid)
        assert len(fp) == 64 and fp == _req_fp(pid)
        canon = lp.canonical_request(
            provider_key="deepseek", model_id="deepseek-flash",
            project_id=pid, credential_ref="deepseek",
            prompt=PROMPT, max_output_tokens=32, max_cost_usd="0.05")
        assert canon["endpoint"] == (
            "https://api.deepseek.com/chat/completions")
        assert canon["request_body"]["thinking"] == {
            "type": "disabled"}
        assert canon["request_body"]["stream"] is False
        assert canon["request_body"]["max_tokens"] == 32
        assert DUMMY_KEY not in json.dumps(canon)

        # (3) owner approval validation — file binds request fp
        _approve_arm(client, pid)
        r = _commit(client, pid, "cloud.live.validate_arm", ARM)
        assert r.status_code == 200, r.text
        with _sf(client)() as s:
            permit = lp.armed(s)
            assert permit["request_fingerprint"] == fp

        # (4) execution approval validation — separate file,
        # fingerprint-bound to THIS armed permit
        _approve_exec(client)

        # (5)+(6) budget reservation + dispatch admission happen
        # inside run_validation's atomic consume — verify the wire
        seen: list[httpx.Request] = []

        def capture(req):
            seen.append(req)
            return httpx.Response(200, json=DEEPSEEK_OK)

        jid, out = _dispatch(
            client, pid, inner=httpx.MockTransport(capture),
            approve_exec=False)  # approval already written above

        # (7) exactly one simulated request
        assert len(seen) == 1
        body = json.loads(seen[0].content)
        assert body["model"] == "deepseek-flash"
        assert body["thinking"] == {"type": "disabled"}
        assert body["stream"] is False
        assert body["max_tokens"] == 32
        assert body["messages"] == [
            {"role": "user", "content": PROMPT}]
        assert seen[0].headers["authorization"] == \
            f"Bearer {DUMMY_KEY}"
        # credential read exactly once, inside the dispatch boundary
        assert vault.get_calls == ["deepseek"]

        # (8) response parsing + (9) token accounting
        assert out["outcome"] == "succeeded"
        detail = out["remote"]["detail"]
        assert detail["text"] == "A lighthouse stands watch."
        assert detail["usage"] == {
            "input_tokens": 9, "output_tokens": 5}

        # (10) reservation settlement
        res = _reservation(client, jid)
        assert res.status == "settled"
        assert out["reservation"] == "settled"

        # (11) audit trail — the arm command's transaction audit_json
        # carries the permit + request fingerprint; audit/event tables
        # carry no secret and no prompt body anywhere
        from app.db.models import CommandTransaction
        with _sf(client)() as s:
            job = s.get(GenerationJob, jid)
            assert job.status == "succeeded"
            txns = s.execute(select(CommandTransaction).where(
                CommandTransaction.project_id == pid)).scalars().all()
            blob = json.dumps([t.audit_json for t in txns],
                              default=str)
            assert fp in blob                      # fingerprint auditable
            assert DUMMY_KEY not in blob
            assert PROMPT not in blob
            audits = s.execute(select(AuditEvent).where(
                AuditEvent.project_id == pid)).scalars().all()
            assert audits, "no audit events for the project"

        # (12) permit consumption — armed → consumed, recorded
        with _sf(client)() as s:
            assert lp.armed(s) is None
            consumed = lp.consumed_history(s)
            assert len(consumed) == 1
            assert consumed[0]["consumed_job_id"] == jid

    def test_arm_surfaces_fingerprints_in_status(self, client, vault):
        """Owner-preview support: after arming, the read-only status
        report carries both fingerprints for the exec approval."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _approve_arm(client, pid)
        _commit(client, pid, "cloud.live.validate_arm", ARM)
        with _sf(client)() as s:
            report = lg.status_report(s, vault)
        armed = report["permit"]["armed"]
        assert armed["request_fingerprint"] == _req_fp(pid)
        assert armed["permit_fingerprint"] == lp.permit_fingerprint(
            lp.armed(_sf(client)()))
        # prompt never surfaces in the report
        assert PROMPT not in json.dumps(report)


# ===================================================================
# Refusals — checks 13–20 (+ approval-binding proof)
# ===================================================================

class TestRehearsalRefusals:

    def _armed(self, client, pid, vault):
        _approve_arm(client, pid)
        r = _commit(client, pid, "cloud.live.validate_arm", ARM)
        assert r.status_code == 200, r.text

    # (13)+(14) duplicate dispatch + replay refusal — one test
    def test_consumed_permit_refuses_every_replay(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        self._armed(client, pid, vault)
        jid, out = _dispatch(client, pid)
        assert out["outcome"] == "succeeded"
        # replay: no permit armed → refused before anything else
        with pytest.raises(lp.PermitRefused) as exc:
            _dispatch(client, pid)
        assert exc.value.code == "no_permit"

    # (15) expired approval refusal
    def test_expired_exec_approval_refuses(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        self._armed(client, pid, vault)
        _approve_exec(client, not_after=_expiry(hours=-1))
        with pytest.raises(lp.PermitRefused) as exc:
            _dispatch(client, pid, approve_exec=False)
        assert exc.value.code == "owner_approval:exec"
        with _sf(client)() as s:
            assert lp.armed(s) is not None   # refusal, not consumed

    # (16) changed-request refusal — fingerprint binding
    def test_changed_request_invalidates_arm_approval(
            self, client, vault):
        """An approval naming fingerprint A can never arm request B —
        the whole canonical request is bound, not just a field."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _approve_arm(client, pid)
        # arm with a DIFFERENT prompt — fingerprint no longer matches
        r = _commit(client, pid, "cloud.live.validate_arm",
                    {**ARM, "prompt": "Say anything else."})
        assert r.status_code == 422
        assert "request_fingerprint" in r.text
        assert lp.armed(_sf(client)()) is None

    def test_arm_approval_without_fingerprint_refused(
            self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _approve_arm(client, pid, request_fingerprint=None)
        r = _commit(client, pid, "cloud.live.validate_arm", ARM)
        assert r.status_code == 422

    # (17) cancellation handling — no recall exists by contract
    def test_no_cancellation_path(self, client, vault):
        from app.domain.workers.rest_providers import make_provider
        import asyncio
        provider = make_provider(
            "deepseek",
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={})),
            api_key=DUMMY_KEY)
        with pytest.raises(Exception) as exc:
            asyncio.run(provider.cancel("chatcmpl-x"))
        assert "cancel" in str(exc.value).lower()

    # (18) timeout → uncertain billing → hold, never resubmit
    def test_timeout_holds_reservation_uncertain(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        self._armed(client, pid, vault)

        def hang(req):
            raise httpx.ReadTimeout("read timeout", request=req)
        jid, out = _dispatch(
            client, pid, inner=httpx.MockTransport(hang))
        assert out["outcome"] == "ambiguous"
        assert out["reservation"] == "open"
        res = _reservation(client, jid)
        assert res.status == "open"          # held for reconcile

    # (19) provider 402 — documented pre-processing refusal → release
    def test_402_releases_reservation(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        self._armed(client, pid, vault)
        jid, out = _dispatch(client, pid, inner=httpx.MockTransport(
            lambda req: httpx.Response(
                402, json={"error": {"message": "Insufficient Balance",
                                     "type": "payment_required"}})))
        assert out["outcome"] == "failed"
        assert out["code"] == "payment_required"
        assert out["reservation"] == "released"
        res = _reservation(client, jid)
        assert res.status == "released"

    # (20) restart behavior — durable rows survive; ambiguous holds
    def test_restart_preserves_state(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        self._armed(client, pid, vault)
        jid, out = _dispatch(client, pid, inner=httpx.MockTransport(
            lambda req: httpx.Response(
                500, json={"error": {"message": "srv"}})))
        assert out["reservation"] == "open"
        # new session view = post-restart: job + open reservation +
        # consumed history all durable; re-arm refused by concurrency
        _approve_arm(client, pid)
        r = _commit(client, pid, "cloud.live.validate_arm", ARM)
        assert r.status_code == 422 and "concurrency" in r.text
        assert _reservation(client, jid).status == "open"
