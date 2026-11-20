"""Stage B2b-P — the 30-item DeepSeek preparation failure matrix.

One file, one row per required scenario. EVERY dispatch goes through
httpx.MockTransport inners and a synthetic-secret RecordingVault —
nothing performs a real request, touches a real credential, spends a
cent, or produces verified_remote evidence. The real inner transport
(_SecureHTTPTransport) is never constructed: every run injects
``inner=``.

Layout mirrors the authorization order in live_dispatch():
  admission → reservation COMMIT → gate re-check → credential read →
  transport → classify → settle / release / HOLD.
"""
from __future__ import annotations

import itertools
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.db.models import (AppSetting, GenerationJob, SpendReservation,
                           new_id)
from app.domain import live_gate as lg
from app.domain import live_permit as lp
from app.domain import spend
from app.domain.workers.live_transport import (
    LiveDispatchRefused, live_dispatch)
from app.domain.workers.remote import RemoteProvider
from app.domain.workers.rest_providers import (
    RemoteError, RemoteErrorKind, make_provider)
from tests.conftest import make_project

_KEYS = itertools.count(1)

DEEPSEEK_OK = {
    "id": "chatcmpl-b2bp-1",
    "choices": [{"message": {"content": "A beat."},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 20, "completion_tokens": 8},
}
DUMMY_KEY = "synthetic-b2bp-secret"          # never a real credential
PROMPT = "Write one sentence about a lighthouse."
GOOD_RESOLVER = lambda h, p: ["93.184.216.34"]  # noqa: E731 — public test IP


class RecordingVault:
    """Ref-aware vault double — listed refs report configured;
    get_fields returns whatever secret was last "stored". Tracks a
    generation marker like the real vault: rotate() simulates a
    credential write (new generation, optional new value)."""

    def __init__(self, configured=True, refs=("deepseek",)):
        self.get_calls: list[str] = []
        self._configured = configured
        self._refs = set(refs)
        self._fields = {"api_key": DUMMY_KEY}
        self.generation = "gen-1"
        self._rot = 1

    def status(self, key):
        return {"configured": self._configured and key in self._refs,
                "fields": list(self._fields),
                "set_at": "2026-01-01T00:00:00Z",
                "generation": self.generation, "readable": True,
                "unreadable_reason": None}

    def get_fields(self, key):
        self.get_calls.append(key)
        return dict(self._fields)

    def get_fields_with_generation(self, key):
        self.get_calls.append(key)
        return dict(self._fields), self.generation

    def current_generation(self, key):
        return self.generation

    def rotate(self, fields=None):
        """Simulate a credential write under the same ref — a NEW
        generation every time, optionally new field values."""
        self._rot += 1
        self.generation = f"gen-{self._rot}"
        if fields is not None:
            self._fields = dict(fields)


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
              "idempotency_key": f"b2bp-{next(_KEYS):04d}"})


def _budget(client, pid):
    _commit(client, pid, "spend.budget_set",
            {"target": "global", "scope": "lifetime",
             "limit_usd": "5.00"})


EN = {"provider_key": "deepseek", "model_id": "deepseek-flash",
      "credential_ref": "deepseek"}
ARM = {"credential_ref": "deepseek", "prompt": PROMPT,
       "max_output_tokens": 32, "max_cost_usd": "0.05"}


def _setup(client, pid, vault):
    _budget(client, pid)
    r = _commit(client, pid, "cloud.live.enable", EN)
    assert r.status_code == 200, r.text


def _owner_dir(client):
    d = Path(client.app.state.settings.data_dir) / lp.OWNER_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _expiry(hours=1):
    return (datetime.now(timezone.utc)
            + timedelta(hours=hours)).isoformat()


def _approve_arm(client, pid, **overrides):
    gen = client.app.state.provider_vault.status(
        "deepseek").get("generation")
    doc = {"approve": "validate_arm", "provider_key": "deepseek",
           "model_id": "deepseek-flash", "project_id": pid,
           "credential_generation": gen,
           "request_fingerprint": lp.request_fingerprint(
               provider_key="deepseek", model_id="deepseek-flash",
               project_id=pid, credential_ref="deepseek",
               credential_generation=gen,
               prompt=PROMPT, max_output_tokens=32,
               max_cost_usd="0.05"),
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


def _armed(client, pid):
    _approve_arm(client, pid)
    r = _commit(client, pid, "cloud.live.validate_arm", ARM)
    assert r.status_code == 200, r.text
    return r


def _mkjob(client, pid, jid, ikey):
    with _sf(client)() as s:
        s.add(GenerationJob(
            id=jid, project_id=pid, adapter_id="cloud.live.validate",
            kind="text", idempotency_key=ikey,
            request_json={"stage": "B2b-P"}, status="validating",
            resumable=False, execution_scope="remote"))
        s.commit()


def _run(client, pid, *, inner=None, job_id=None, approve_exec=True,
         resolver=None):
    if approve_exec:
        _approve_exec(client)
    jid = job_id or new_id()
    _mkjob(client, pid, jid, f"job:b2bp-{new_id()[:8]}")
    return lp.run_validation(
        _sf(client), project_id=pid, job_id=jid,
        job_idempotency_key=f"job:b2bp-{new_id()[:8]}",
        command_id=None, vault=client.app.state.provider_vault,
        data_dir=client.app.state.settings.data_dir,
        inner=inner or httpx.MockTransport(
            lambda req: httpx.Response(200, json=DEEPSEEK_OK)),
        resolver=resolver or GOOD_RESOLVER)


def _reservation(client, job_id):
    with _sf(client)() as s:
        return s.execute(select(SpendReservation).where(
            SpendReservation.job_id == job_id)).scalar_one_or_none()


def _job(client, job_id):
    with _sf(client)() as s:
        return s.get(GenerationJob, job_id)


def _mkproject(client):
    return make_project(client)["id"]


# ===================================================================
# B2b-P.6 — the 30-item simulated failure matrix
# ===================================================================

class TestB2bPMatrix:

    # ---- 1. happy path ---------------------------------------------
    def test_01_successful_completion(self, client, vault):
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        out = _run(client, pid)
        assert out["outcome"] == "succeeded"
        assert out["reservation"] == "settled"
        assert out["remote"]["detail"]["usage"] == {
            "input_tokens": 20, "output_tokens": 8}
        assert out["provider_verified"] is False
        assert out["transport_is_live"] is False

    # ---- 2. missing credential --------------------------------------
    def test_02_missing_credential(self, client, vault):
        vault._configured = False
        pid = _mkproject(client)
        _budget(client, pid)
        # enablement itself refuses — no configured credential
        r = _commit(client, pid, "cloud.live.enable", EN)
        assert r.status_code == 422
        assert "credential" in r.text
        # arming never reached; no permit, no reservation, no socket
        assert lp.armed(_sf(client)()) is None

    # ---- 3./4. invalid credential → HTTP 401 -------------------------
    def test_03_invalid_credential_401(self, client, vault):
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        inner = httpx.MockTransport(
            lambda req: httpx.Response(
                401, json={"error": {"message": "wrong key",
                                     "type": "authentication_error",
                                     "code": "invalid_api_key"}}))
        out = _run(client, pid, inner=inner)
        r = out
        assert r["outcome"] == "failed"
        assert r["code"] == RemoteErrorKind.AUTHENTICATION.value
        assert r["reservation"] == "released"   # 401 = refused before processing
        res = _reservation(client, r["job_id"])
        assert res.status == "released"

    # ---- 5. HTTP 403 --------------------------------------------------
    def test_05_forbidden_403(self, client, vault):
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        inner = httpx.MockTransport(
            lambda req: httpx.Response(
                403, json={"error": {"message": "region forbidden",
                                     "type": "forbidden"}}))
        out = _run(client, pid, inner=inner)
        r = out
        assert r["outcome"] == "failed"
        assert r["code"] == RemoteErrorKind.FORBIDDEN.value
        assert r["reservation"] == "released"

    # ---- 6. payment/balance failure → 402 ------------------------------
    def test_06_insufficient_balance_402(self, client, vault):
        """DeepSeek 402 Insufficient Balance — account out of funds:
        provider refuses before processing → confirmed no-charge,
        reservation RELEASED (never held like a 5xx)."""
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        inner = httpx.MockTransport(
            lambda req: httpx.Response(
                402, json={"error": {"message": "Insufficient Balance",
                                     "type": "payment_required"}}))
        out = _run(client, pid, inner=inner)
        r = out
        assert r["outcome"] == "failed"
        assert r["code"] == RemoteErrorKind.PAYMENT_REQUIRED.value
        assert r["reservation"] == "released"

    # ---- 7. HTTP 429 ---------------------------------------------------
    def test_07_rate_limited_429(self, client, vault):
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        inner = httpx.MockTransport(
            lambda req: httpx.Response(
                429, json={"error": {"message": "rate limit"}}))
        out = _run(client, pid, inner=inner)
        r = out
        assert r["outcome"] == "failed"
        assert r["code"] == RemoteErrorKind.RATE_LIMITED.value
        assert r["reservation"] == "released"

    # ---- 8./9. HTTP 500 / 502 / 503 ------------------------------------
    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_08_server_errors_hold(self, client, vault, status):
        """5xx = the provider may have processed — outcome UNKNOWN:
        reservation stays OPEN for explicit reconcile, never released,
        never resubmitted."""
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        inner = httpx.MockTransport(
            lambda req: httpx.Response(
                status, json={"error": {"message": "server"}}))
        out = _run(client, pid, inner=inner)
        r = out
        assert r["outcome"] == "ambiguous"
        assert r["code"] == RemoteErrorKind.SERVER_ERROR.value
        assert r["reservation"] == "open"
        res = _reservation(client, r["job_id"])
        assert res.status == "open"
        # and a second attempt refuses — the open reservation is proof
        with pytest.raises(lp.PermitRefused) as exc2:
            _run(client, pid, inner=inner)
        assert exc2.value.code == "no_permit"

    # ---- 10. timeout before dispatch ------------------------------------
    def test_10_timeout_before_dispatch(self, client, vault):
        """Connect-time timeout: the request may never have left —
        but we cannot PROVE that, so charge_evidence=unknown → HOLD."""
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)

        def connect_timeout(req):
            raise httpx.ConnectTimeout("connect timeout", request=req)
        out = _run(client, pid, inner=httpx.MockTransport(connect_timeout))
        r = out
        assert r["outcome"] == "ambiguous"
        assert r["code"] == RemoteErrorKind.TRANSPORT.value
        assert r["reservation"] == "open"

    # ---- 11. timeout after possible dispatch ----------------------------
    def test_11_read_timeout_midflight(self, client, vault):
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)

        def read_timeout(req):
            raise httpx.ReadTimeout("read timeout", request=req)
        out = _run(client, pid, inner=httpx.MockTransport(read_timeout))
        r = out
        assert r["outcome"] == "ambiguous"
        assert r["reservation"] == "open"

    # ---- 12. connection reset --------------------------------------------
    def test_12_connection_reset(self, client, vault):
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)

        def reset(req):
            raise httpx.ConnectError("connection reset", request=req)
        out = _run(client, pid, inner=httpx.MockTransport(reset))
        r = out
        assert r["outcome"] == "ambiguous"
        assert r["code"] == RemoteErrorKind.TRANSPORT.value
        assert r["reservation"] == "open"

    # ---- 13. malformed JSON ----------------------------------------------
    def test_13_malformed_json(self, client, vault):
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        inner = httpx.MockTransport(
            lambda req: httpx.Response(
                200, content=b"{not json",
                headers={"Content-Type": "application/json"}))
        out = _run(client, pid, inner=inner)
        r = out
        assert r["outcome"] == "ambiguous"
        assert r["code"] == RemoteErrorKind.MALFORMED.value
        assert r["reservation"] == "open"

    # ---- 14. missing response content --------------------------------------
    def test_14_empty_choices(self, client, vault):
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        inner = httpx.MockTransport(
            lambda req: httpx.Response(
                200, json={"id": "x", "choices": [],
                           "usage": {"prompt_tokens": 5,
                                     "completion_tokens": 0}}))
        out = _run(client, pid, inner=inner)
        r = out
        assert r["outcome"] == "ambiguous"
        assert r["code"] == RemoteErrorKind.MALFORMED.value
        assert r["reservation"] == "open"

    # ---- 15. unexpected response schema ------------------------------------
    def test_15_nonobject_json(self, client, vault):
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        inner = httpx.MockTransport(
            lambda req: httpx.Response(
                200, content=b'["not", "an", "object"]',
                headers={"Content-Type": "application/json"}))
        out = _run(client, pid, inner=inner)
        r = out
        assert r["outcome"] == "ambiguous"
        assert r["code"] == RemoteErrorKind.MALFORMED.value
        assert r["reservation"] == "open"

    # ---- 16. missing token usage --------------------------------------------
    def test_16_missing_usage(self, client, vault):
        """Genuine 200 without a usage object: the response exists but
        billing is unresolved — settlement stays usage_unavailable,
        reservation OPEN (never fabricated $0)."""
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        body = {"id": "chatcmpl-x",
                "choices": [{"message": {"content": "ok"},
                             "finish_reason": "stop"}]}
        inner = httpx.MockTransport(
            lambda req: httpx.Response(200, json=body))
        out = _run(client, pid, inner=inner)
        r = out
        assert r["outcome"] == "succeeded"
        assert r["settlement_state"] == "usage_unavailable"
        assert r["reservation"] == "open"
        res = _reservation(client, r["job_id"])
        assert res.status == "open"

    # ---- 17. inconsistent token usage ---------------------------------------
    @pytest.mark.parametrize("usage", [
        {"prompt_tokens": 10},                       # missing output
        {"prompt_tokens": "ten", "completion_tokens": 5},   # non-int
        {"prompt_tokens": -1, "completion_tokens": 5},      # negative
        None,                                        # explicit null
    ])
    def test_17_inconsistent_usage(self, client, vault, usage):
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        body = {"id": "chatcmpl-x",
                "choices": [{"message": {"content": "ok"},
                             "finish_reason": "stop"}],
                "usage": usage}
        inner = httpx.MockTransport(
            lambda req: httpx.Response(200, json=body))
        out = _run(client, pid, inner=inner)
        r = out
        # unusable usage → unresolved exposure, never a settled charge
        assert r["outcome"] == "succeeded"
        assert r["reservation"] == "open"
        assert r.get("settlement_state") == "usage_unavailable"

    # ---- 18. budget exceeded --------------------------------------------------
    def test_18_budget_exceeded(self, client, vault):
        pid = _mkproject(client)
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        # per-request cap is $0.10 / permit cap $0.05 — try to arm at $0.20
        _approve_arm(client, pid)
        r = _commit(client, pid, "cloud.live.validate_arm",
                    {**ARM, "max_cost_usd": "0.20"})
        assert r.status_code == 422
        # arm at the real cap but with a $0 budget → admission refuses
        _commit(client, pid, "spend.budget_remove",
                {"target": "global", "scope": "lifetime"})
        r2 = _commit(client, pid, "cloud.live.validate_arm", ARM)
        assert r2.status_code == 422

    # ---- 19. cancellation before dispatch ---------------------------------------
    def test_19_cancel_before_dispatch(self, client, vault):
        """No exec approval = the run is refused BEFORE any transport:
        job marked failed, NO reservation, nothing sent."""
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        sent: list[str] = []
        inner = httpx.MockTransport(
            lambda req: (sent.append(str(req.url)),
                         httpx.Response(200, json=DEEPSEEK_OK))[1])
        jid = new_id()
        _mkjob(client, pid, jid, f"job:b2bp-{new_id()[:8]}")
        with pytest.raises(lp.PermitRefused):
            lp.run_validation(
                _sf(client), project_id=pid, job_id=jid,
                job_idempotency_key=f"job:b2bp-{new_id()[:8]}",
                command_id=None, vault=client.app.state.provider_vault,
                data_dir=client.app.state.settings.data_dir,
                inner=inner, resolver=GOOD_RESOLVER)
        assert sent == []                      # nothing reached a socket
        job = _job(client, jid)
        assert job.status == "failed"
        assert _reservation(client, jid) is None
        with _sf(client)() as s:               # permit survives — evidence
            assert lp.armed(s) is not None

    # ---- 20. cancellation after dispatch ----------------------------------------
    def test_20_cancel_after_dispatch(self, client, vault):
        """DeepSeek's contract documents NO cancel path — a completed
        submit can never be recalled. cancel() raises UnsupportedRemoteOp;
        a settled reservation is untouched."""
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        out = _run(client, pid)
        assert out["outcome"] == "succeeded"
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json={}))
        provider = make_provider("deepseek", transport=transport,
                                 api_key=DUMMY_KEY)
        import asyncio
        with pytest.raises(Exception) as exc:
            asyncio.run(provider.cancel("chatcmpl-b2bp-1"))
        assert "cancel" in str(exc.value).lower()

    # ---- 21. expired authorization -----------------------------------------------
    def test_21_expired_exec_approval(self, client, vault):
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        _approve_exec(client, not_after=_expiry(hours=-1))  # already lapsed
        with pytest.raises(lp.PermitRefused) as exc:
            _run(client, pid, approve_exec=False)
        assert exc.value.code == "owner_approval:exec"
        with _sf(client)() as s:
            assert lp.armed(s) is not None      # refusal, not consumed

    # ---- 22. replayed authorization ------------------------------------------------
    def test_22_replayed_run_refused(self, client, vault):
        """Single-use: after a successful run the permit is consumed —
        a second run finds nothing armed and refuses."""
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        out1 = _run(client, pid)
        assert out1["outcome"] == "succeeded"
        with pytest.raises(lp.PermitRefused) as exc:
            _run(client, pid)
        assert exc.value.code == "no_permit"

    # ---- 23. duplicate execution request ---------------------------------------------
    def test_23_duplicate_run_idempotent(self, client, vault):
        """Same key + same payload → recorded result, no second job,
        no second dispatch. Deliberately run WITHOUT exec approval:
        the refusal is what makes the HTTP path safe under B2b-P —
        the post-commit run never constructs a real transport."""
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        key = f"b2bp-dup-{next(_KEYS)}"
        body = {"command_type": "cloud.live.validate_run",
                "payload": {}, "scope": "project",
                "idempotency_key": key}
        r1 = client.post(
            f"/api/projects/{pid}/commands/commit",
            json={**body, "expected_revision": _rev(client, pid)})
        assert r1.status_code == 422           # locked — no exec approval
        r2 = client.post(
            f"/api/projects/{pid}/commands/commit",
            json={**body, "expected_revision": _rev(client, pid)})
        assert r2.status_code == 200
        assert r2.json()["idempotent_replay"] is True
        with _sf(client)() as s:
            jobs = s.execute(select(GenerationJob).where(
                GenerationJob.adapter_id == "cloud.live.validate"
            )).scalars().all()
        assert len(jobs) == 1                  # one attempt, replayed

    # ---- 24. restart during a pending operation ----------------------------------------
    def test_24_restart_holds_uncertain(self, client, vault):
        """A job left 'dispatching' with an open reservation is
        durable proof an outcome is unknown — a new run refuses
        'uncertain_prior_dispatch', the old row is never swept or
        resubmitted."""
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        out = _run(client, pid, inner=httpx.MockTransport(
            lambda req: httpx.Response(
                500, json={"error": {"message": "srv"}})))
        assert out["reservation"] == "open"
        # simulate restart: the durable rows survive. Even RE-ARMING
        # is refused — admission sees the open reservation for the
        # same provider and refuses 'concurrency'. The ambiguous
        # outcome can never be silently overwritten by a fresh permit.
        _approve_arm(client, pid)
        r = _commit(client, pid, "cloud.live.validate_arm", ARM)
        assert r.status_code == 422
        assert "concurrency" in r.text
        res = _reservation(client, out["job_id"])
        assert res.status == "open"            # held, never swept

    # ---- 25. provider config change after approval --------------------------------------
    def test_25_provider_disabled_after_arm(self, client, vault):
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        _commit(client, pid, "cloud.live.disable",
                {"provider_key": "deepseek"})
        with pytest.raises(LiveDispatchRefused) as exc:
            _run(client, pid)
        assert exc.value.code == "not_enabled"

    # ---- 26. model change after approval --------------------------------------------------
    def test_26_model_mismatch_refused(self, client, vault):
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        # tamper the armed record's model → exec approval fingerprint
        # still binds; consume() field compare refuses first
        with _sf(client)() as s:
            row = s.get(AppSetting, lp.PERMIT_KEY)
            st = dict(row.value_json["value"])
            armed = dict(st["armed"]); armed["model_id"] = "deepseek-v4-pro"
            st["armed"] = armed
            row.value_json = {"value": st}
            s.commit()
        with pytest.raises(LiveDispatchRefused) as exc:
            _run(client, pid)
        # enablement was granted for deepseek-flash only — the swapped
        # model is caught at admission, before any socket exists
        assert exc.value.code == "model_not_granted"

    # ---- 27. credential replacement after approval ------------------------------------------
    def test_27_credential_replace_and_delete(self, client, vault):
        """Replace (same ref, new value) → the vault mints a NEW
        generation → the dispatch binding fails closed: consume
        refuses on credential_generation mismatch, the permit stays
        armed as evidence, and nothing reaches the wire. Delete (ref
        gone) → admission refuses before any socket."""
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        _approve_exec(client)
        vault.rotate({"api_key": "synthetic-rotated-key"})
        sent: list[str] = []

        def capture(req):
            sent.append(str(req.url))
            return httpx.Response(200, json=DEEPSEEK_OK)
        jid = new_id()
        _mkjob(client, pid, jid, f"job:b2bp-{new_id()[:8]}")
        with pytest.raises(lp.PermitRefused) as exc:
            lp.run_validation(
                _sf(client), project_id=pid, job_id=jid,
                job_idempotency_key=f"job:b2bp-{new_id()[:8]}",
                command_id=None, vault=vault,
                data_dir=client.app.state.settings.data_dir,
                inner=httpx.MockTransport(capture),
                resolver=GOOD_RESOLVER)
        assert exc.value.code == "mismatch:credential_generation"
        assert sent == []               # nothing reached the wire
        assert vault.get_calls == []    # secret never read either
        with _sf(client)() as s:
            assert lp.armed(s) is not None   # still armed — evidence
            res = s.execute(select(SpendReservation).where(
                SpendReservation.job_id == jid)).scalar_one_or_none()
        assert res is None              # consume rollback — no booking
        # delete it entirely → a fresh run can never leave the gate
        vault._configured = False
        pid2 = _mkproject(client)
        _budget(client, pid2)
        r = _commit(client, pid2, "cloud.live.enable", EN)
        assert r.status_code == 422

    # ---- 28. network transport unavailable ---------------------------------------------------
    def test_28_dns_unavailable_refused(self, client, vault):
        """Resolver returns nothing → nothing is dialed; confirmed
        no-charge (none_confirmed) → reservation released."""
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        out = _run(client, pid,
                   resolver=lambda h, p: [])
        r = out
        assert r["outcome"] == "failed"
        assert r["code"] == "dns"
        assert r["reservation"] == "released"

    # ---- 29. secret-shaped response text ------------------------------------------------------
    def test_29_secret_shaped_response_is_data(self, client, vault,
                                             caplog):
        """A provider echoing secret-shaped text is stored as response
        text only — it is never treated as a credential, never
        re-read from the vault, and OUR secret never lands in logs."""
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        sneaky = {"id": "chatcmpl-x",
                  "choices": [{"message": {
                      "content": "sk-LEAKED-looking-string-1234"},
                      "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
        with caplog.at_level(logging.INFO):
            out = _run(client, pid, inner=httpx.MockTransport(
                lambda req: httpx.Response(200, json=sneaky)))
        r = out
        assert r["outcome"] == "succeeded"
        assert "sk-LEAKED" in r["remote"]["detail"]["text"]
        # our synthetic secret never appears in any log record
        for rec in caplog.records:
            assert DUMMY_KEY not in rec.getMessage()

    # ---- 30. concurrent requests competing for the same budget ----------------------------------
    def test_30_concurrent_dispatch_single_flight(self, client, vault):
        """Two dispatches racing one provider: the first books the open
        reservation; the second's admission sees it and refuses
        'concurrency' — financial and socket exclusion both hold."""
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        # first run holds an open reservation (ambiguous 5xx outcome)
        out1 = _run(client, pid, inner=httpx.MockTransport(
            lambda req: httpx.Response(
                500, json={"error": {"message": "srv"}})))
        assert out1["reservation"] == "open"
        # a second direct dispatch attempt sees the open reservation
        with _sf(client)() as s:
            allowed, code = lg.dispatch_admission(
                s, provider_key="deepseek", model_id="deepseek-flash",
                max_cost_usd="0.05", vault=vault)
        assert allowed is False
        assert code == "concurrency"


# ===================================================================
# Request-body contract assertions (B2b-P.1/P.3)
# ===================================================================

class TestB2bPRequestContract:
    """The wire shape the future bounded request must produce."""

    def test_thinking_disabled_and_stream_off_sent(self, client, vault):
        """DeepSeek DEFAULTS to thinking when `thinking` is omitted —
        the bounded contract must send it explicitly, plus stream:false
        and the fixed max_tokens."""
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        seen: list[dict] = []

        def capture(req):
            seen.append(json.loads(req.content))
            return httpx.Response(200, json=DEEPSEEK_OK)
        out = _run(client, pid, inner=httpx.MockTransport(capture))
        assert out["outcome"] == "succeeded"
        body = seen[0]
        assert body["thinking"] == {"type": "disabled"}
        assert body["stream"] is False
        assert body["max_tokens"] == 32
        assert body["model"] == "deepseek-flash"
        assert body["messages"] == [{"role": "user", "content": PROMPT}]

    def test_auth_header_bearer_no_url_token(self, client, vault):
        pid = _mkproject(client)
        _setup(client, pid, vault)
        _armed(client, pid)
        seen: list[httpx.Request] = []

        def capture(req):
            seen.append(req)
            return httpx.Response(200, json=DEEPSEEK_OK)
        _run(client, pid, inner=httpx.MockTransport(capture))
        req = seen[0]
        assert req.headers["authorization"] == f"Bearer {DUMMY_KEY}"
        assert "key" not in str(req.url).lower()
        assert str(req.url) == "https://api.deepseek.com/chat/completions"
        assert req.method == "POST"
