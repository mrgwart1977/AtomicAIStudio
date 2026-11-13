"""C2 exit gate — Stage B2a: single-use DeepSeek validation permit.

Every dispatch here runs through httpx.MockTransport inners and a
dummy-secret RecordingVault — nothing performs a real request, reads
a real credential, or produces a verified_remote artifact. The
shipped build carries ZERO armed permits; arming is the future
owner-approval act, exercised here only to prove the mechanism.
"""
from __future__ import annotations

import itertools
import json

import httpx
import pytest
from sqlalchemy import select

from app.db.models import (AppSetting, AuditEvent, GenerationJob,
                           SpendReservation, new_id)
from app.domain import live_gate as lg
from app.domain import live_permit as lp
from app.domain import spend
from tests.conftest import make_project

_KEYS = itertools.count(1)

DEEPSEEK_OK = {
    "id": "chatcmpl-b2a-1",
    "choices": [{"message": {"content": "A beat."},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 20, "completion_tokens": 8},
}

DUMMY_KEY = "dummy-b2a-key"
PROMPT = "Write one sentence about a lighthouse."
GOOD_RESOLVER = lambda h, p: ["93.184.216.34"]  # noqa: E731


class RecordingVault:
    """Ref-aware vault double — only listed refs report configured, so
    arm's metadata check genuinely distinguishes known refs from typos."""

    def __init__(self, configured=True, refs=("deepseek",)):
        self.get_calls: list[str] = []
        self._configured = configured
        self._refs = set(refs)

    def status(self, key):
        return {"configured": self._configured and key in self._refs,
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
              "idempotency_key": f"b2a-live-{next(_KEYS):04d}"})


def _budget(client, pid):
    _commit(client, pid, "spend.budget_set",
            {"target": "global", "scope": "lifetime",
             "limit_usd": "5.00"})


EN = {"provider_key": "deepseek", "model_id": "deepseek-chat",
      "credential_ref": "deepseek"}
ARM = {"credential_ref": "deepseek", "prompt": PROMPT,
       "max_output_tokens": 32, "max_cost_usd": "0.05"}


def _setup(client, pid, vault):
    """budgets + enable — the precondition chain every arm needs."""
    _budget(client, pid)
    r = _commit(client, pid, "cloud.live.enable", EN)
    assert r.status_code == 200, r.text


def _armed(client, pid):
    r = _commit(client, pid, "cloud.live.validate_arm", ARM)
    assert r.status_code == 200, r.text
    return r


def _run(client, pid, *, inner=None):
    """Direct run_validation call with injected mock inner — the
    command path always uses inner=None (real transport) so it is
    only exercised for pre-transport refusals in tests."""
    return lp.run_validation(
        _sf(client), project_id=pid, job_id=new_id(),
        job_idempotency_key=f"job:b2a-{new_id()[:8]}",
        command_id=None, vault=client.app.state.provider_vault,
        inner=inner or httpx.MockTransport(
            lambda req: httpx.Response(200, json=DEEPSEEK_OK)),
        resolver=GOOD_RESOLVER)


def _tamper(client, field, value):
    """Rewrite one field of the armed record — simulates out-of-band
    state corruption. Must deep-copy: mutating the nested dict in
    place silently no-ops (plain JSON columns don't track it)."""
    with _sf(client)() as s:
        row = s.get(AppSetting, lp.PERMIT_KEY)
        st = dict(row.value_json["value"])
        armed = dict(st["armed"])
        armed[field] = value
        st["armed"] = armed
        row.value_json = {"value": st}
        s.commit()


def _reservation(client, job_id):
    with _sf(client)() as s:
        return s.execute(select(SpendReservation).where(
            SpendReservation.job_id == job_id)).scalar_one_or_none()


# ---------------- arm: the authorization act ----------------

class TestArm:
    def test_arm_requires_enablement(self, client, vault):
        pid = make_project(client)["id"]
        _budget(client, pid)
        r = _commit(client, pid, "cloud.live.validate_arm", ARM)
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "not_enabled"

    def test_arm_requires_budgets(self, client, vault):
        """Enable itself requires budgets — remove them after enable
        and arm must still refuse at its own admission check."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        r = _commit(client, pid, "cloud.live.enable", EN)
        assert r.status_code == 200, r.text
        _commit(client, pid, "spend.budget_remove",
                {"target": "global", "scope": "lifetime"})
        r = _commit(client, pid, "cloud.live.validate_arm", ARM)
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "no_budgets"

    def test_arm_one_at_a_time(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)
        r = _commit(client, pid, "cloud.live.validate_arm", ARM)
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "already_armed"

    def test_arm_field_validation(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        bad = [
            ({**ARM, "prompt": ""}, "prompt"),
            ({**ARM, "prompt": "x" * 501}, "prompt"),
            ({**ARM, "max_output_tokens": 0}, "max_output_tokens"),
            ({**ARM, "max_output_tokens": 65}, "max_output_tokens"),
            ({**ARM, "max_output_tokens": "abc"}, "max_output_tokens"),
            ({**ARM, "max_cost_usd": "0"}, "max_cost_usd"),
            ({**ARM, "max_cost_usd": "0.06"}, "max_cost_usd"),
            ({**ARM, "max_cost_usd": "-1"}, "max_cost_usd"),
            ({**ARM, "credential_ref": ""}, "credential_ref"),
            ({**ARM, "credential_ref": "nope"}, "credential_ref"),
        ]
        for payload, code in bad:
            r = _commit(client, pid, "cloud.live.validate_arm", payload)
            assert r.status_code == 422, payload
            # required-field validation returns plain-string detail;
            # permit-layer refusals return {"error": code}
            detail = r.json()["detail"]
            assert code in json.dumps(detail), (payload, detail)

    def test_arm_only_deepseek_no_generic_switch(self, client, vault):
        """Enabling a different provider/model does NOT allow arming —
        the permit is hardcoded to deepseek/deepseek-chat."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        r = _commit(client, pid, "cloud.live.enable", {
            "provider_key": "xai", "model_id": "grok-3-mini",
            "credential_ref": "xai"})
        if r.status_code != 200:
            pytest.skip("xai model not grantable in this catalog")
        r = _commit(client, pid, "cloud.live.validate_arm", ARM)
        assert r.status_code == 422
        assert r.json()["detail"]["error"] in (
            "not_enabled", "model_not_granted")

    def test_armed_permit_survives_restart(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)
        with _sf(client)() as s:
            p = lp.armed(s)
            assert p is not None
            assert p["provider_key"] == "deepseek"
            assert p["model_id"] == "deepseek-chat"
            assert p["project_id"] == pid

    def test_no_permit_is_shipped_state(self, client):
        with _sf(client)() as s:
            assert lp.armed(s) is None


# ---------------- run: pre-transport refusals via command ----------------

class TestRunCommandRefusals:
    """The command path is exercised only for refusals that happen
    BEFORE any transport exists — a fully-armed+enabled command run
    would build the real transport (that is the B2b-only path)."""

    def test_run_without_permit_refused(self, client, vault):
        pid = make_project(client)["id"]
        r = _commit(client, pid, "cloud.live.validate_run", {})
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "no_permit"

    def test_run_on_wrong_project_refused(self, client, vault):
        a = make_project(client, "PermitA")["id"]
        b = make_project(client, "PermitB")["id"]
        _setup(client, a, vault)
        _armed(client, a)
        r = _commit(client, b, "cloud.live.validate_run", {})
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "mismatch:project_id"
        # permit stays armed — a mismatched call does not consume it
        with _sf(client)() as s:
            assert lp.armed(s) is not None


# ---------------- run: full path through injected transport ----------------

class TestRunDispatch:
    def test_run_consumes_and_settles(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)
        sends = []
        def handler(req):
            sends.append(str(req.url))
            assert req.headers["authorization"] == f"Bearer {DUMMY_KEY}"
            return httpx.Response(200, json=DEEPSEEK_OK)
        out = _run(client, pid,
                   inner=httpx.MockTransport(handler))
        assert out["outcome"] == "succeeded"
        assert out["reservation"] == "settled"
        assert sends == ["https://api.deepseek.com/chat/completions"]
        assert vault.get_calls == ["deepseek"]
        # permit burned — history keeps the record
        with _sf(client)() as s:
            assert lp.armed(s) is None
            hist = lp.consumed_history(s)
            assert len(hist) == 1 and hist[0]["provider_key"] == "deepseek"
        # durable job row finished
        with _sf(client)() as s:
            job = s.get(GenerationJob, out["job_id"])
            assert job is not None and job.status == "succeeded"
            assert job.resumable is False

    def test_single_use_no_replay(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)
        out = _run(client, pid)
        assert out["outcome"] == "succeeded"
        with pytest.raises(lp.PermitRefused) as ei:
            _run(client, pid)
        assert ei.value.code == "no_permit"
        # and the command path sees the same burn
        r = _commit(client, pid, "cloud.live.validate_run", {})
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "no_permit"

    def test_mock_run_never_verified_remote(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)
        out = _run(client, pid)
        assert out["transport_is_live"] is False
        assert out["provider_verified"] is False
        assert out["remote"]["remote_job_id"] == "chatcmpl-b2a-1"

    def test_tampered_permit_fails_closed_without_consuming(
            self, client, vault):
        """Rewrite the armed credential_ref — the run reads the
        credential ref from the GRANT entry, not the permit, so the
        two disagree → consume refuses and leaves the record."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)
        _tamper(client, "credential_ref", "tampered-ref")
        with pytest.raises(lp.PermitRefused) as ei:
            _run(client, pid)
        assert ei.value.code == "mismatch:credential_ref"
        with _sf(client)() as s:
            assert lp.armed(s) is not None  # still armed, not consumed

    def test_kill_switch_blocks_run_permit_stays_armed(
            self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)
        _commit(client, pid, "cloud.live.disable_all", {})
        sends = []
        from app.domain.workers.live_transport import LiveDispatchRefused
        with pytest.raises(LiveDispatchRefused) as ei:
            _run(client, pid, inner=httpx.MockTransport(
                lambda req: sends.append(str(req.url))))
        assert ei.value.code == "global_kill"
        assert sends == []
        with _sf(client)() as s:
            assert lp.armed(s) is not None  # admission refused pre-consume

    def test_crash_after_consume_cannot_replay(self, client, vault):
        """Kill lands between admission-commit and send: permit is
        consumed durably, reservation held — a retry can never replay
        the authorization."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)

        def killer(session):
            st = lg.state(session)
            st["global_disabled"] = True
            return st

        from app.domain.workers.live_transport import LiveDispatchRefused
        with pytest.raises(LiveDispatchRefused) as ei:
            lp.run_validation(
                _sf(client), project_id=pid, job_id=new_id(),
                job_idempotency_key=f"job:b2a-{new_id()[:8]}",
                command_id=None, vault=vault,
                inner=httpx.MockTransport(
                    lambda req: httpx.Response(200, json=DEEPSEEK_OK)),
                resolver=GOOD_RESOLVER, gate_probe=killer)
        assert ei.value.code == "global_kill"
        with _sf(client)() as s:
            assert lp.armed(s) is None
            assert len(lp.consumed_history(s)) == 1
        # consumed permanently — no replay path exists
        with pytest.raises(lp.PermitRefused) as ei2:
            _run(client, pid)
        assert ei2.value.code == "no_permit"

    def test_mismatch_leaves_no_reservation(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)
        _tamper(client, "credential_ref", "different-ref")
        with pytest.raises(lp.PermitRefused):
            _run(client, pid)
        # consume raised inside the admit transaction → rolled back
        with _sf(client)() as s:
            n = s.execute(select(SpendReservation).where(
                SpendReservation.environment == "live")).scalars().all()
            assert n == []

    def test_dummy_secret_never_persisted(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)
        out = _run(client, pid)
        assert out["outcome"] == "succeeded"
        with _sf(client)() as s:
            job = s.get(GenerationJob, out["job_id"])
            audit_rows = s.execute(select(AuditEvent)).scalars().all()
        haystacks = [
            str(job.request_json), str(job.result_json or ""),
            str(job.error or ""),
            str([a.payload_json for a in audit_rows]),
            str(out.get("verification_evidence") or ""),
            str(out.get("remote") or ""),
        ]
        for h in haystacks:
            assert DUMMY_KEY not in h

    def test_ambiguous_outcome_holds_and_job_marked(
            self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)
        jid = new_id()
        out = lp.run_validation(
            _sf(client), project_id=pid, job_id=jid,
            job_idempotency_key=f"job:b2a-{new_id()[:8]}",
            command_id=None, vault=vault,
            inner=httpx.MockTransport(
                lambda req: (_ for _ in ()).throw(
                    httpx.ConnectError("dropped"))),
            resolver=GOOD_RESOLVER)
        assert out["outcome"] == "ambiguous"
        assert out["reservation"] == "open"
        res = _reservation(client, jid)
        assert res.status == "open"
        with _sf(client)() as s:
            job = s.get(GenerationJob, jid)
            assert job.status == "failed"  # honest, not resumable
            assert job.resumable is False
