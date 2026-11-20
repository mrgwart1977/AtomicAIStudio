"""Stage B2b-L hardening — credential-generation binding, permit
expiry, audited disarm, and pre-dispatch accounting.

The disclosed gap: approvals bound the credential REFERENCE (a name)
but not the credential itself — rotating the secret under the same
reference left every approval valid. This suite pins the fix:

  * a non-secret ``generation`` marker minted on EVERY credential
    write (never derived from the secret) is bound into the canonical
    request, both approvals, the armed permit, dispatch admission, and
    the final credential retrieval;
  * the three rotation windows refuse closed — before arm approval,
    between arm and dispatch admission, and between the atomic
    credential read and the wire send;
  * armed permits expire with the approval that created them;
  * ``cloud.live.validate_disarm`` voids a stale/expired permit without
    granting authority;
  * proven pre-dispatch refusals release the reservation (nothing was
    sent) while uncertain outcomes keep it held for reconcile.

All credentials and transports are synthetic — RecordingVault doubles
and httpx.MockTransport only. The real ``_SecureHTTPTransport`` is
never constructed; no real request, credential, or spend exists.
"""
from __future__ import annotations

import itertools
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.db.models import (CommandTransaction, GenerationJob,
                           SpendReservation, new_id)
from app.domain import live_gate as lg
from app.domain import live_permit as lp
from app.domain.workers.live_transport import (
    LiveDispatchRefused, LiveTransportRefused)
from app.domain.workers import live_transport as lt
from tests.conftest import make_project

_KEYS = itertools.count(1)

DEEPSEEK_OK = {
    "id": "chatcmpl-c2b3",
    "choices": [{"message": {"content": "READY"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 1},
}
DUMMY_KEY = "synthetic-c2b3-secret"          # never a real credential
PROMPT = "Write one sentence about a lighthouse."
GOOD_RESOLVER = lambda h, p: ["93.184.216.34"]  # noqa: E731 — public test IP


class RecordingVault:
    """Vault double with generation tracking, mirroring the real
    DPAPI vault's contract: every rotate() mints a NEW generation."""

    def __init__(self, configured=True, fields=None):
        self.get_calls: list[str] = []
        self._configured = configured
        self._fields = dict(fields or {"api_key": DUMMY_KEY})
        self.generation = "gen-1"
        self._rot = 1
        # deterministic mid-window rotation switches
        self.rotate_on_read = False   # inside get_fields_with_generation
        self.rotate_on_probe = False  # inside current_generation

    def status(self, key):
        return {"configured": self._configured and key == "deepseek",
                "fields": list(self._fields),
                "set_at": "2026-01-01T00:00:00Z",
                "generation": self.generation, "readable": True,
                "unreadable_reason": None}

    def get_fields(self, key):
        self.get_calls.append(key)
        return dict(self._fields)

    def get_fields_with_generation(self, key):
        self.get_calls.append(key)
        if self.rotate_on_read:
            self.rotate()
        return dict(self._fields), self.generation

    def current_generation(self, key):
        if self.rotate_on_probe:
            self.rotate()
        return self.generation

    def rotate(self, fields=None):
        self._rot += 1
        self.generation = f"gen-{self._rot}"
        if fields is not None:
            self._fields = dict(fields)

    def delete(self, key):
        self._configured = False
        self._fields = {}
        self.generation = None

    def recreate(self, fields=None):
        """Delete-then-write: fresh generation as the real vault does."""
        self._configured = True
        self._fields = dict(fields or {"api_key": DUMMY_KEY})
        self.rotate()


@pytest.fixture(autouse=True)
def _no_real_transport(monkeypatch):
    """Zero-egress guard — any path that would build the real transport
    fails the suite outright."""
    def _boom(*a, **k):
        raise AssertionError(
            "real _SecureHTTPTransport constructed — live egress must "
            "be impossible in this suite")
    monkeypatch.setattr(lt, "_SecureHTTPTransport", _boom)


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
              "idempotency_key": f"c2b3-{next(_KEYS):04d}"})


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


def _approve_exec(client, permit=None, **overrides):
    if permit is None:
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
    with _sf(client)() as s:
        return lp.armed(s)


def _mkjob(client, pid, jid):
    with _sf(client)() as s:
        s.add(GenerationJob(
            id=jid, project_id=pid, adapter_id="cloud.live.validate",
            kind="text", idempotency_key=f"job:c2b3-{new_id()[:8]}",
            request_json={"stage": "B2b-L-hardening"},
            status="validating", resumable=False,
            execution_scope="remote"))
        s.commit()


def _run(client, pid, *, inner=None, approve_exec=True, vault=None):
    if approve_exec:
        _approve_exec(client)
    jid = new_id()
    _mkjob(client, pid, jid)
    out = lp.run_validation(
        _sf(client), project_id=pid, job_id=jid,
        job_idempotency_key=f"job:c2b3-{new_id()[:8]}",
        command_id=None,
        vault=vault or client.app.state.provider_vault,
        data_dir=client.app.state.settings.data_dir,
        inner=inner or httpx.MockTransport(
            lambda req: httpx.Response(200, json=DEEPSEEK_OK)),
        resolver=GOOD_RESOLVER)
    return jid, out


def _reservation(client, job_id):
    with _sf(client)() as s:
        return s.execute(select(SpendReservation).where(
            SpendReservation.job_id == job_id)).scalar_one_or_none()


def _job(client, job_id):
    with _sf(client)() as s:
        return s.get(GenerationJob, job_id)


def _plant(client, pid, **permit_over):
    """Write an armed permit record directly — the only way to build
    expired/legacy states time can't produce on demand."""
    base = {"permit_id": new_id(), "provider_key": "deepseek",
            "model_id": "deepseek-flash", "project_id": pid,
            "credential_ref": "deepseek", "credential_generation": "gen-1",
            "prompt": PROMPT, "max_output_tokens": 32,
            "max_cost_usd": "0.05", "request_fingerprint": "f" * 64,
            "armed_at": "2026-01-01T00:00:00Z",
            "not_after": _expiry(), "armed_command_id": None}
    base.update(permit_over)
    with _sf(client)() as s:
        st = lp._state(s)
        st["armed"] = base
        lp._save(s, st)
        s.commit()
    return base


# ===================================================================
# rotation windows — all three refuse closed
# ===================================================================

class TestRotationWindows:

    def test_rotation_before_arm_invalidates_approval(
            self, client, vault):
        """Window 1: approval written for gen-1, credential rotated to
        gen-2 before the arm lands → BOTH the explicit generation field
        and the fingerprint no longer match. The refusal names the
        generation binding and echoes the NEW expected fingerprint so
        the owner can approve the rotated credential consciously."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _approve_arm(client, pid)          # approval binds gen-1
        vault.rotate()                     # → gen-2
        r = _commit(client, pid, "cloud.live.validate_arm", ARM)
        assert r.status_code == 422
        assert "credential_generation" in r.text
        with _sf(client)() as s:
            assert lp.armed(s) is None     # refused, nothing armed

    def test_rotation_between_arm_and_run(self, client, vault):
        """Window 2: armed under gen-1, rotated to gen-2 before the
        run → consume's field compare refuses, rolled back atomically:
        permit stays armed as evidence, NO reservation commits, the
        secret is never read, nothing reaches the wire."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)
        _approve_exec(client)
        vault.rotate()
        sent: list[str] = []
        jid = new_id()
        _mkjob(client, pid, jid)
        with pytest.raises(lp.PermitRefused) as exc:
            lp.run_validation(
                _sf(client), project_id=pid, job_id=jid,
                job_idempotency_key=f"job:c2b3-{new_id()[:8]}",
                command_id=None, vault=vault,
                data_dir=client.app.state.settings.data_dir,
                inner=httpx.MockTransport(
                    lambda req: sent.append(str(req.url)) or
                    httpx.Response(200, json=DEEPSEEK_OK)),
                resolver=GOOD_RESOLVER)
        assert exc.value.code == "mismatch:credential_generation"
        assert sent == []
        assert vault.get_calls == []        # secret never read
        with _sf(client)() as s:
            assert lp.armed(s) is not None
        assert _reservation(client, jid) is None

    def test_rotation_between_consume_and_read(self, client, vault):
        """Window 3: consume passes on gen-1, then rotation lands in
        the gap before the atomic credential read — the read returns
        gen-2 → proven pre-dispatch refusal: nothing sent, reservation
        RELEASED (not held — no provider request ever existed)."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)
        _approve_exec(client)
        vault.rotate_on_read = True          # flips inside the atomic read
        sent: list[str] = []
        jid = new_id()
        _mkjob(client, pid, jid)
        with pytest.raises(LiveDispatchRefused) as exc:
            lp.run_validation(
                _sf(client), project_id=pid, job_id=jid,
                job_idempotency_key=f"job:c2b3-{new_id()[:8]}",
                command_id=None, vault=vault,
                data_dir=client.app.state.settings.data_dir,
                inner=httpx.MockTransport(
                    lambda req: sent.append(str(req.url)) or
                    httpx.Response(200, json=DEEPSEEK_OK)),
                resolver=GOOD_RESOLVER)
        assert exc.value.code == "credential_rotated"
        assert sent == []
        res = _reservation(client, jid)
        assert res is not None and res.status == "released"
        assert "credential_rotated" in (res.reconcile_note or "")
        assert _job(client, jid).status == "failed"

    def test_rotation_between_read_and_send(self, client, vault):
        """Window 4: read passes, rotation lands before the wire —
        the transport's generation probe (last possible check) refuses
        inside the send path. none_confirmed → released; the secret
        was read but never left the process."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)
        _approve_exec(client)
        vault.rotate_on_probe = True          # flips at send-time probe
        jid, out = _run(client, pid)
        assert out["outcome"] == "failed"
        assert out["code"] == "credential_rotated"
        assert out["reservation"] == "released"
        res = _reservation(client, jid)
        assert res.status == "released"

    def test_rotation_delete_and_recreate_invalidates(
            self, client, vault):
        """Delete + re-create mints a FRESH generation — approvals
        bound to the old one are dead even though the ref, the status
        fields, and even the secret VALUE could be identical."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _approve_arm(client, pid)
        old_gen = vault.generation
        vault.delete("deepseek")
        vault.recreate()                      # same value, new gen
        assert vault.generation != old_gen
        r = _commit(client, pid, "cloud.live.validate_arm", ARM)
        assert r.status_code == 422
        assert "credential_generation" in r.text

    def test_resave_same_value_still_invalidates(self, client, vault):
        """Conservative semantics: ANY write rotates the generation,
        even re-saving the identical value. The vault deliberately
        cannot prove 'same key' — every write is a rotation event."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _approve_arm(client, pid)
        vault.rotate({"api_key": DUMMY_KEY})  # identical value
        r = _commit(client, pid, "cloud.live.validate_arm", ARM)
        assert r.status_code == 422

    def test_delete_blocks_entirely(self, client, vault):
        """Deleted ref → unconfigured: arm and admission refuse before
        generation is even consulted."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        vault.delete("deepseek")
        r = _commit(client, pid, "cloud.live.enable", EN)
        assert r.status_code == 422


# ===================================================================
# disarm — audited void, no authority granted
# ===================================================================

def _disarm(client, pid, permit, **overrides):
    payload = {"permit_id": permit["permit_id"],
               "permit_fingerprint": lp.permit_fingerprint(permit)}
    payload.update(overrides)
    return _commit(client, pid, "cloud.live.validate_disarm", payload)


class TestDisarm:

    def test_disarm_voids_and_rearm_succeeds(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        permit = _armed(client, pid)
        r = _disarm(client, pid, permit)
        assert r.status_code == 200, r.text
        with _sf(client)() as s:
            assert lp.armed(s) is None
            voided = lp.voided_history(s)
            assert len(voided) == 1
            assert voided[0]["permit_id"] == permit["permit_id"]
            assert voided[0]["voided_at"]
        # a fresh arm under a new approval proceeds normally
        _approve_exec(client)
        permit2 = _armed(client, pid)
        assert permit2["permit_id"] != permit["permit_id"]

    def test_disarm_idempotent_replay(self, client, vault):
        """The same disarm issued twice: first voids, second returns
        the existing voided record — no error, no double void."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        permit = _armed(client, pid)
        assert _disarm(client, pid, permit).status_code == 200
        r = _disarm(client, pid, permit)
        assert r.status_code == 200, r.text
        with _sf(client)() as s:
            assert len(lp.voided_history(s)) == 1

    def test_stale_disarm_cannot_void_newer_permit(
            self, client, vault):
        """Disarm naming permit A cannot touch armed permit B — the
        target must match id AND fingerprint."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        a = _armed(client, pid)
        assert _disarm(client, pid, a).status_code == 200
        b = _armed(client, pid)
        r = _disarm(client, pid, a)   # stale disarm targets A
        assert r.status_code == 422
        assert "stale_target" in r.text
        with _sf(client)() as s:
            assert lp.armed(s)["permit_id"] == b["permit_id"]

    def test_disarm_wrong_fingerprint_refused(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        permit = _armed(client, pid)
        r = _disarm(client, pid, permit,
                    permit_fingerprint="0" * 64)
        assert r.status_code == 422
        assert "fingerprint_mismatch" in r.text
        with _sf(client)() as s:
            assert lp.armed(s) is not None

    def test_disarm_after_consume_refused(self, client, vault):
        """Consume wins the race → the permit is already permanently
        non-consumable; disarm surfaces 'already_consumed' rather than
        implying it voided anything."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        permit = _armed(client, pid)
        jid, out = _run(client, pid)
        assert out["outcome"] == "succeeded"
        r = _disarm(client, pid, permit)
        assert r.status_code == 422
        assert "already_consumed" in r.text

    def test_run_after_disarm_refused(self, client, vault):
        """Disarm wins the race → consume finds nothing armed:
        'no_permit', deterministic, nothing dispatched."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        permit = _armed(client, pid)
        assert _disarm(client, pid, permit).status_code == 200
        _approve_exec(client)
        jid = new_id()
        _mkjob(client, pid, jid)
        with pytest.raises(lp.PermitRefused) as exc:
            lp.run_validation(
                _sf(client), project_id=pid, job_id=jid,
                job_idempotency_key=f"job:c2b3-{new_id()[:8]}",
                command_id=None, vault=vault,
                data_dir=client.app.state.settings.data_dir,
                inner=httpx.MockTransport(
                    lambda req: httpx.Response(200, json=DEEPSEEK_OK)),
                resolver=GOOD_RESOLVER)
        assert exc.value.code == "no_permit"

    def test_disarm_audit_records_evidence(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        permit = _armed(client, pid)
        r = _disarm(client, pid, permit, reason="superseded by re-arm")
        assert r.status_code == 200
        with _sf(client)() as s:
            row = s.execute(
                select(CommandTransaction).order_by(
                    CommandTransaction.created_at.desc())
            ).scalars().first()
        after = row.audit_json["after"]["disarm"]
        assert after["permit_id"] == permit["permit_id"]
        assert after["permit_fingerprint"] == \
            lp.permit_fingerprint(permit)
        assert after["reason"] == "superseded by re-arm"
        assert PROMPT not in json.dumps(row.audit_json)
        assert DUMMY_KEY not in json.dumps(row.audit_json)

    def test_disarm_requires_no_owner_approval(self, client, vault):
        """De-authorizing never needs an approval file — with NO
        owner/ directory at all, disarm still succeeds."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        permit = _armed(client, pid)
        owner = _owner_dir(client)
        for f in owner.iterdir():
            f.unlink()
        assert _disarm(client, pid, permit).status_code == 200

    def test_disarm_unknown_target_refused(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        permit = _armed(client, pid)
        _disarm(client, pid, permit)
        r = _disarm(client, pid, permit, permit_id=new_id())
        assert r.status_code == 422
        assert "no_permit" in r.text


# ===================================================================
# armed-permit expiry — carried from the arm approval, fail closed
# ===================================================================

class TestPermitExpiry:

    def test_arm_carries_approval_expiry(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        doc = _approve_arm(client, pid)
        r = _commit(client, pid, "cloud.live.validate_arm", ARM)
        assert r.status_code == 200, r.text
        with _sf(client)() as s:
            permit = lp.armed(s)
        assert permit["not_after"] is not None
        # the stored expiry IS the approval's, normalized to ISO
        assert datetime.fromisoformat(
            permit["not_after"]) == datetime.fromisoformat(
            doc["not_after"])

    def test_expired_permit_never_consumed(self, client, vault):
        """not_after already lapsed → consume refuses 'permit_expired'
        WITHOUT consuming — the dead permit stays as evidence until
        disarmed."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _plant(client, pid, not_after=(
            datetime.now(timezone.utc)
            - timedelta(seconds=1)).isoformat())
        _approve_exec(client)
        jid = new_id()
        _mkjob(client, pid, jid)
        with pytest.raises(lp.PermitRefused) as exc:
            lp.run_validation(
                _sf(client), project_id=pid, job_id=jid,
                job_idempotency_key=f"job:c2b3-{new_id()[:8]}",
                command_id=None, vault=vault,
                data_dir=client.app.state.settings.data_dir,
                inner=httpx.MockTransport(
                    lambda req: httpx.Response(200, json=DEEPSEEK_OK)),
                resolver=GOOD_RESOLVER)
        assert exc.value.code == "permit_expired"
        with _sf(client)() as s:
            assert lp.armed(s) is not None   # NOT consumed
        assert _reservation(client, jid) is None

    def test_expiry_boundary_unexpired_side(self, client, vault):
        """Just inside the boundary: not_after comfortably in the
        future → consume proceeds to a normal successful dispatch."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        permit = _plant(client, pid)
        _approve_exec(client, permit)
        jid, out = _run(client, pid, approve_exec=False)
        assert out["outcome"] == "succeeded"

    def test_expired_permit_disarmable(self, client, vault):
        """A dead permit doesn't deadlock the lane — disarm clears it
        (expiry is not checked on the void path) and re-arm works."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        permit = _plant(client, pid, not_after=(
            datetime.now(timezone.utc)
            - timedelta(hours=1)).isoformat())
        r = _disarm(client, pid, permit)
        assert r.status_code == 200, r.text
        _armed(client, pid)

    def test_legacy_permit_without_expiry_refused(self, client, vault):
        """Old-format armed record (no not_after, no generation):
        consume fails CLOSED — never silently valid."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        legacy = _plant(client, pid)
        del legacy["not_after"]
        del legacy["credential_generation"]
        with _sf(client)() as s:
            st = lp._state(s)
            st["armed"] = legacy
            lp._save(s, st)
            s.commit()
        _approve_exec(client)
        with pytest.raises(lp.PermitRefused) as exc:
            _run(client, pid, approve_exec=False)
        assert exc.value.code in ("permit_expiry", "permit_expired")
        # and disarm remains available to clear it
        assert _disarm(client, pid, legacy).status_code == 200

    def test_malformed_expiry_refused(self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _plant(client, pid, not_after="not-a-date")
        _approve_exec(client)
        with pytest.raises(lp.PermitRefused) as exc:
            _run(client, pid, approve_exec=False)
        assert exc.value.code == "permit_expiry"

    def test_expiry_survives_restart(self, client, vault):
        """Expiry lives in the durable AppSetting row — a fresh session
        (post-restart) sees the same not_after and still refuses."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        permit = _plant(client, pid, not_after=(
            datetime.now(timezone.utc)
            - timedelta(minutes=5)).isoformat())
        # "restart" = a brand-new session on the same durable DB
        with _sf(client)() as fresh:
            rearmed = lp.armed(fresh)
        assert rearmed["not_after"] == permit["not_after"]
        _approve_exec(client)
        with pytest.raises(lp.PermitRefused) as exc:
            _run(client, pid, approve_exec=False)
        assert exc.value.code == "permit_expired"

    def test_no_replay_after_expiry(self, client, vault):
        """An expired permit refuses on EVERY consume attempt — the
        refusal doesn't consume it but it also never lapses open."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _plant(client, pid, not_after=(
            datetime.now(timezone.utc)
            - timedelta(minutes=5)).isoformat())
        _approve_exec(client)
        for _ in range(2):
            with pytest.raises(lp.PermitRefused) as exc:
                _run(client, pid, approve_exec=False)
            assert exc.value.code == "permit_expired"


# ===================================================================
# strict credential-field selection
# ===================================================================

class TestCredentialField:

    def test_missing_api_key_refused_before_transport(
            self, client, vault):
        """A vault holding OTHER fields but no 'api_key' → refusal
        before transport construction; proven non-send → released.
        An arbitrary vault field is NEVER promoted to Bearer."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)
        vault.rotate({"token": "not-an-api-key"})
        # bind the permit to the new generation so the FIELD check —
        # not the generation check — is what fires
        with _sf(client)() as s:
            st = lp._state(s)
            st["armed"]["credential_generation"] = vault.generation
            lp._save(s, st)
            s.commit()
        _approve_exec(client)
        jid = new_id()
        _mkjob(client, pid, jid)
        with pytest.raises(LiveDispatchRefused) as exc:
            lp.run_validation(
                _sf(client), project_id=pid, job_id=jid,
                job_idempotency_key=f"job:c2b3-{new_id()[:8]}",
                command_id=None, vault=vault,
                data_dir=client.app.state.settings.data_dir,
                inner=httpx.MockTransport(
                    lambda req: httpx.Response(200, json=DEEPSEEK_OK)),
                resolver=GOOD_RESOLVER)
        assert exc.value.code == "credential_field"
        res = _reservation(client, jid)
        assert res is not None and res.status == "released"
        assert "token" not in str(res.reconcile_note or "")

    def test_empty_vault_refused_before_transport(self, client, vault):
        """Configured by metadata but fields are empty → same refusal;
        no field exists to substitute."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)
        vault.rotate({})  # rotation leaves zero stored fields
        with _sf(client)() as s:
            st = lp._state(s)
            st["armed"]["credential_generation"] = vault.generation
            lp._save(s, st)
            s.commit()
        _approve_exec(client)
        jid = new_id()
        _mkjob(client, pid, jid)
        with pytest.raises(LiveDispatchRefused) as exc:
            lp.run_validation(
                _sf(client), project_id=pid, job_id=jid,
                job_idempotency_key=f"job:c2b3-{new_id()[:8]}",
                command_id=None, vault=vault,
                data_dir=client.app.state.settings.data_dir,
                inner=httpx.MockTransport(
                    lambda req: httpx.Response(200, json=DEEPSEEK_OK)),
                resolver=GOOD_RESOLVER)
        assert exc.value.code == "credential_field"
        res = _reservation(client, jid)
        assert res is not None and res.status == "released"


# ===================================================================
# accounting taxonomy — proven non-send releases, ambiguity holds
# ===================================================================

class TestAccountingTaxonomy:

    def test_predispatch_refusal_releases_with_reason(
            self, client, vault):
        """Rotation caught at the atomic read → released, and the
        reconcile_note carries the refusal code as auditable evidence
        — the reservation does not permanently consume budget."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)
        _approve_exec(client)
        vault.rotate_on_read = True
        jid = new_id()
        _mkjob(client, pid, jid)
        with pytest.raises(LiveDispatchRefused):
            lp.run_validation(
                _sf(client), project_id=pid, job_id=jid,
                job_idempotency_key=f"job:c2b3-{new_id()[:8]}",
                command_id=None, vault=vault,
                data_dir=client.app.state.settings.data_dir,
                inner=httpx.MockTransport(
                    lambda req: httpx.Response(200, json=DEEPSEEK_OK)),
                resolver=GOOD_RESOLVER)
        res = _reservation(client, jid)
        assert res.status == "released"
        assert "pre-dispatch refusal" in (res.reconcile_note or "")
        assert res.released_at is not None

    def test_uncertain_outcome_still_holds(self, client, vault):
        """Contrast: a timeout AFTER the send boundary keeps the
        reservation OPEN — billing can no longer be ruled out, so
        exposure is held for explicit reconcile, never auto-released
        or resubmitted."""
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)
        _approve_exec(client)

        def timeout(req):
            raise httpx.ReadTimeout("stall", request=req)
        jid, out = _run(
            client, pid,
            inner=httpx.MockTransport(timeout), approve_exec=False)
        assert out["outcome"] == "ambiguous"
        assert out["reservation"] == "open"
        res = _reservation(client, jid)
        assert res.status == "open"          # held for reconcile
        assert _job(client, jid).status == "unknown"


# ===================================================================
# non-secret surfaces — generation visible, secrets never
# ===================================================================

class TestNonSecretSurfaces:

    def test_status_report_exposes_binding_not_secret(
            self, client, vault):
        pid = make_project(client)["id"]
        _setup(client, pid, vault)
        _armed(client, pid)
        with _sf(client)() as s:
            report = lg.status_report(s, vault)
        armed = report["permit"]["armed"]
        assert armed["credential_generation"] == "gen-1"
        assert armed["not_after"]
        assert armed["expired"] is False
        blob = json.dumps(report)
        assert DUMMY_KEY not in blob and PROMPT not in blob

    def test_vault_status_has_generation_no_secret(self, vault):
        st = vault.status("deepseek")
        assert st["generation"] == "gen-1"
        assert DUMMY_KEY not in json.dumps(st)


# ===================================================================
# real vault file — DPAPI-backed behavior (Windows only)
# ===================================================================

from app.domain.providers.credentials import CredentialVault  # noqa: E402


@pytest.mark.skipif(os.name != "nt",
                    reason="DPAPI vault is Windows-only")
class TestVaultFileGeneration:

    def _v(self, tmp_path):
        return CredentialVault(tmp_path / "credentials.vault")

    def test_generation_minted_and_persisted(self, tmp_path):
        v = self._v(tmp_path)
        v.set_fields("deepseek", {"api_key": "synthetic-one"})
        st = v.status("deepseek")
        assert st["generation"] and len(st["generation"]) == 32
        # a fresh vault object on the same file sees the same gen
        assert self._v(tmp_path).status(
            "deepseek")["generation"] == st["generation"]

    def test_every_write_rotates_even_same_value(self, tmp_path):
        """g2 != g1 proves generation is NOT value-derived — a pure
        function of the secret would be identical on re-save."""
        v = self._v(tmp_path)
        v.set_fields("deepseek", {"api_key": "same-value"})
        g1 = v.status("deepseek")["generation"]
        v.set_fields("deepseek", {"api_key": "same-value"})
        g2 = v.status("deepseek")["generation"]
        assert g1 != g2

    def test_delete_recreate_mints_fresh(self, tmp_path):
        v = self._v(tmp_path)
        v.set_fields("deepseek", {"api_key": "x"})
        g1 = v.status("deepseek")["generation"]
        v.delete("deepseek")
        assert v.status("deepseek")["generation"] is None
        v.set_fields("deepseek", {"api_key": "x"})
        g2 = v.status("deepseek")["generation"]
        assert g2 and g2 != g1

    def test_empty_write_preserves_generation(self, tmp_path):
        """All-empty values store nothing — no rotation, no set_at."""
        v = self._v(tmp_path)
        v.set_fields("deepseek", {"api_key": "keep"})
        g1 = v.status("deepseek")["generation"]
        t1 = v.status("deepseek")["set_at"]
        v.set_fields("deepseek", {"api_key": ""})
        assert v.status("deepseek")["generation"] == g1
        assert v.status("deepseek")["set_at"] == t1

    def test_failed_write_preserves_generation_and_fields(
            self, tmp_path, monkeypatch):
        """Encryption failure mid-write: nothing is stored, the prior
        generation and prior ciphertext stay intact — never a
        half-rotated state."""
        import app.domain.providers.credentials as cred
        v = self._v(tmp_path)
        v.set_fields("deepseek", {"api_key": "original"})
        g1 = v.status("deepseek")["generation"]

        def boom(data):
            raise cred.VaultError("simulated protect failure")
        monkeypatch.setattr(cred, "_protect", boom)
        with pytest.raises(cred.VaultError):
            v.set_fields("deepseek", {"api_key": "replaced"})
        assert v.status("deepseek")["generation"] == g1
        assert v.get_fields("deepseek")["api_key"] == "original"

    def test_legacy_entry_lazy_migration(self, tmp_path):
        """A vault file written before generation tracking gets one
        minted on first load — ciphertext untouched, persisted once."""
        path = tmp_path / "credentials.vault"
        v = self._v(tmp_path)
        v.set_fields("deepseek", {"api_key": "legacy"})
        state = json.loads(path.read_text())
        del state["providers"]["deepseek"]["generation"]
        path.write_text(json.dumps(state))
        v2 = self._v(tmp_path)
        gen = v2.status("deepseek")["generation"]
        assert gen
        # persisted — subsequent loads see the same marker
        assert self._v(tmp_path).status(
            "deepseek")["generation"] == gen
        # and the credential itself still decrypts
        assert v2.get_fields("deepseek")["api_key"] == "legacy"

    def test_atomic_read_pairs_fields_and_generation(self, tmp_path):
        v = self._v(tmp_path)
        v.set_fields("deepseek", {"api_key": "paired"})
        fields, gen = v.get_fields_with_generation("deepseek")
        assert fields["api_key"] == "paired"
        assert gen == v.status("deepseek")["generation"]

    def test_status_never_leaks_secret(self, tmp_path):
        v = self._v(tmp_path)
        v.set_fields("deepseek", {"api_key": "sk-synthetic-secret"})
        st = v.status("deepseek")
        assert "sk-synthetic-secret" not in json.dumps(st)
        assert st["generation"] != "sk-synthetic-secret"
