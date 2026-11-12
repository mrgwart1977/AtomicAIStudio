"""C2 exit gate — Stage A live-connectivity safety infrastructure.

Everything here is exercised against mocked state only: a stub vault
reporting metadata (never secrets), spend rows inserted directly, and
catalog fixtures. No socket, transport, or credential material is
touched anywhere in this file.
"""
from __future__ import annotations

import itertools
import threading
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from app.db.models import AppSetting, SpendReservation, SpendCharge, new_id
from app.domain import live_gate as lg
from app.domain import spend
from app.domain.providers import registry
from app.domain.spend import usd_to_micro
from app.domain.workers.remote import provenance_confidence
from tests.conftest import make_project, wait_for_job

_KEYS = itertools.count(1)


class StubVault:
    """Metadata-only vault double — status() mirrors the real contract
    ({configured, fields, set_at, readable}) and CANNOT return secret
    material because it has none."""
    def __init__(self, configured: bool = True, readable: bool = True):
        self._c, self._r = configured, readable

    def status(self, key):
        return {"configured": self._c,
                "fields": ["api_key"] if self._c else [],
                "set_at": "2026-01-01T00:00:00Z" if self._c else None,
                "readable": self._r and self._c,
                "unreadable_reason": None}


@pytest.fixture()
def vault(client):
    v = StubVault()
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
              "idempotency_key": f"lgate-{next(_KEYS):04d}"})


def _budget(client, pid, limit="5.00", scope="lifetime", target="global"):
    return _commit(client, pid, "spend.budget_set",
                   {"target": target, "scope": scope, "limit_usd": limit})


def _seed_live_exposure(client, provider_key, *, open_micro=0, charged_micro=0,
                        pid="p-seed"):
    with _sf(client)() as s:
        res = SpendReservation(
            id=new_id(), job_id=f"j-{new_id()[:8]}", command_id="c1",
            project_id=pid, provider_key=provider_key,
            environment="live", status="open" if open_micro else "settled",
            reserved_micro=open_micro, session_id="s1",
            day_key="d", month_key="m")
        s.add(res)
        s.flush()
        if charged_micro:
            s.add(SpendCharge(
                id=new_id(), reservation_id=res.id, job_id=res.job_id,
                project_id=pid, provider_key=provider_key,
                environment="live", source="reconciled",
                micro=charged_micro,
                session_id="s1", day_key="d", month_key="m"))
        s.commit()


EN = {"provider_key": "deepseek", "model_id": "deepseek-chat",
      "credential_ref": "deepseek"}


# ---------------- enablement ----------------

class TestEnable:
    def test_enable_persists_and_audits(self, client, vault):
        p = make_project(client)
        _budget(client, p["id"])
        r = _commit(client, p["id"], "cloud.live.enable", EN)
        assert r.status_code == 200, r.text
        with _sf(client)() as s:
            st = lg.state(s)
        e = st["providers"]["deepseek"]
        assert e["enabled"] and e["model_id"] == "deepseek-chat"
        assert e["credential_ref"] == "deepseek"
        assert e["granted_at"] and e["revoked_at"] is None

    def test_enable_unknown_and_non_contract_refused(self, client, vault):
        p = make_project(client)
        _budget(client, p["id"])
        for key in ("nope-provider", "sudowrite", "rytr", "midjourney"):
            r = _commit(client, p["id"], "cloud.live.enable",
                        {**EN, "provider_key": key})
            assert r.status_code == 422, (key, r.text)
            assert r.json()["detail"]["error"] == "no_contract"

    def test_enable_requires_budget(self, client, vault):
        p = make_project(client)  # no budgets configured
        r = _commit(client, p["id"], "cloud.live.enable", EN)
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "no_budgets"

    @pytest.mark.parametrize("patch,code", [
        ({"model_id": "grok-4"}, "model_not_granted"),
        ({"model_id": "not-a-model"}, "model_not_granted"),
        ({"credential_ref": "anthropic"}, "credential"),
        ({"credential_ref": ""}, "credential"),
    ])
    def test_enable_validation_refusals(self, client, vault, patch, code):
        p = make_project(client)
        _budget(client, p["id"])
        pl = {**EN, **patch}
        if not pl["credential_ref"]:
            pl["credential_ref"] = "nonexistent-key"
        r = _commit(client, p["id"], "cloud.live.enable", pl)
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == code

    def test_enable_unconfigured_and_unreadable_credential(self, client, vault):
        p = make_project(client)
        _budget(client, p["id"])
        vault._c = False
        r = _commit(client, p["id"], "cloud.live.enable", EN)
        assert r.status_code == 422 and r.json()["detail"]["error"] == "credential"
        vault._c = True; vault._r = False
        r = _commit(client, p["id"], "cloud.live.enable", EN)
        assert r.status_code == 422 and r.json()["detail"]["error"] == "credential"

    def test_enable_stale_evidence_refused(self, client, vault, monkeypatch):
        monkeypatch.setattr(registry, "STRATEGY_FRESH_DAYS", -1)
        p = make_project(client)
        _budget(client, p["id"])
        r = _commit(client, p["id"], "cloud.live.enable", EN)
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "evidence_stale"

    def test_enable_missing_fields_422(self, client, vault):
        p = make_project(client)
        _budget(client, p["id"])
        for missing in ("provider_key", "model_id", "credential_ref"):
            pl = {k: v for k, v in EN.items() if k != missing}
            assert _commit(client, p["id"], "cloud.live.enable", pl).status_code == 422

    def test_no_secret_material_in_audit(self, client, vault):
        p = make_project(client)
        _budget(client, p["id"])
        _commit(client, p["id"], "cloud.live.enable", EN)
        cmds = client.get(f"/api/projects/{p['id']}/commands").json()
        live = next(c for c in cmds if c["command_type"] == "cloud.live.enable")
        blob = str(live)
        assert '"credential_ref": "deepseek"' in blob.replace("'", '"') or \
               "deepseek" in blob
        assert "sk-" not in blob and "secret" not in blob.lower()

    def test_enable_preview_free(self, client, vault):
        p = make_project(client)
        _budget(client, p["id"])
        r = client.post(f"/api/projects/{p['id']}/commands/preview",
                        json={"command_type": "cloud.live.enable",
                              "payload": EN, "scope": "project"})
        assert r.status_code == 200, r.text
        assert "Stage A" in str(r.json()["warnings"])
        with _sf(client)() as s:
            assert lg.state(s)["providers"] == {}  # preview commits nothing


# ---------------- disable / kill switch ----------------

class TestDisable:
    def test_disable_revokes_and_persists(self, client, vault):
        p = make_project(client)
        _budget(client, p["id"])
        _commit(client, p["id"], "cloud.live.enable", EN)
        r = _commit(client, p["id"], "cloud.live.disable",
                    {"provider_key": "deepseek"})
        assert r.status_code == 200, r.text
        with _sf(client)() as s:
            e = lg.state(s)["providers"]["deepseek"]
        assert e["enabled"] is False and e["revoked_at"]
        ok, code = self._adm(client)
        assert not ok and code == "not_enabled"

    def _adm(self, client):
        with _sf(client)() as s:
            return lg.dispatch_admission(
                s, provider_key="deepseek", model_id="deepseek-chat",
                max_cost_usd="0.05",
                vault=client.app.state.provider_vault)

    def test_kill_switch_blocks_and_revokes_all(self, client, vault):
        p = make_project(client)
        _budget(client, p["id"])
        _commit(client, p["id"], "cloud.live.enable", EN)
        r = _commit(client, p["id"], "cloud.live.disable_all", {})
        assert r.status_code == 200, r.text
        with _sf(client)() as s:
            st = lg.state(s)
        assert st["global_disabled"] and st["global_disabled_at"]
        assert st["providers"]["deepseek"]["enabled"] is False
        # New enable is refused while the kill is held
        r = _commit(client, p["id"], "cloud.live.enable", EN)
        assert r.status_code == 422 and r.json()["detail"]["error"] == "global_kill"
        ok, code = self._adm(client)
        assert not ok and code == "global_kill"

    def test_kill_switch_preserves_open_reservations(self, client, vault):
        p = make_project(client)
        _budget(client, p["id"])
        _seed_live_exposure(client, "deepseek", open_micro=5000)
        _commit(client, p["id"], "cloud.live.disable_all", {})
        with _sf(client)() as s:
            rows = s.execute(select(SpendReservation).where(
                SpendReservation.status == "open")).scalars().all()
        assert len(rows) == 1 and rows[0].reserved_micro == 5000

    def test_resume_requires_regrant(self, client, vault):
        p = make_project(client)
        _budget(client, p["id"])
        _commit(client, p["id"], "cloud.live.enable", EN)
        _commit(client, p["id"], "cloud.live.disable_all", {})
        r = _commit(client, p["id"], "cloud.live.resume", {})
        assert r.status_code == 200, r.text
        ok, code = self._adm(client)
        assert not ok and code == "not_enabled"  # provider stays revoked
        r = _commit(client, p["id"], "cloud.live.enable", EN)
        assert r.status_code == 200
        ok, code = self._adm(client)
        assert ok and code == "ok"

    def test_restart_persistence(self, client, vault):
        p = make_project(client)
        _budget(client, p["id"])
        _commit(client, p["id"], "cloud.live.enable", EN)
        _commit(client, p["id"], "cloud.live.disable_all", {})
        # A fresh session sees the durable row — no in-memory caching
        with _sf(client)() as s:
            row = s.get(AppSetting, lg.LIVE_GATE_KEY)
            assert row and row.value_json["value"]["global_disabled"] is True


# ---------------- dispatch admission ----------------

class TestDispatchAdmission:
    def _enable(self, client, pid):
        _budget(client, pid)
        assert _commit(client, pid, "cloud.live.enable", EN).status_code == 200

    def _adm(self, client, **kw):
        args = dict(provider_key="deepseek", model_id="deepseek-chat",
                    max_cost_usd="0.05",
                    vault=client.app.state.provider_vault)
        args.update(kw)
        with _sf(client)() as s:
            return lg.dispatch_admission(s, **args)

    def test_full_pass_then_refusals(self, client, vault):
        p = make_project(client)
        self._enable(client, p["id"])
        assert self._adm(client) == (True, "ok")
        assert self._adm(client, model_id="other-model") == (False, "model_not_granted")
        assert self._adm(client, max_cost_usd=None) == (False, "unbounded")
        assert self._adm(client, max_cost_usd="0.11") == (False, "cap_per_request")

    def test_credential_lost_blocks(self, client, vault):
        p = make_project(client)
        self._enable(client, p["id"])
        vault._c = False
        assert self._adm(client) == (False, "credential")

    def test_stale_blocks_admission(self, client, vault, monkeypatch):
        p = make_project(client)
        self._enable(client, p["id"])
        monkeypatch.setattr(registry, "STRATEGY_FRESH_DAYS", -1)
        assert self._adm(client) == (False, "evidence_stale")

    def test_concurrency_one_in_flight(self, client, vault):
        p = make_project(client)
        self._enable(client, p["id"])
        _seed_live_exposure(client, "deepseek", open_micro=100)
        assert self._adm(client) == (False, "concurrency")
        # other providers unaffected
        assert self._adm(client, provider_key="xai") == (False, "not_enabled")

    def test_provider_session_cap(self, client, vault):
        p = make_project(client)
        self._enable(client, p["id"])
        # $0.14 already exposed → next $0.05 request exceeds $0.15
        _seed_live_exposure(client, "deepseek", charged_micro=usd_to_micro("0.14"))
        assert self._adm(client) == (False, "cap_per_provider")

    def test_global_session_cap(self, client, vault):
        p = make_project(client)
        self._enable(client, p["id"])
        # $0.71 global exposure (another provider) → $0.05 exceeds $0.75
        _seed_live_exposure(client, "openai", charged_micro=usd_to_micro("0.71"))
        assert self._adm(client) == (False, "cap_global")


# ---------------- egress allowlist ----------------

class TestEgress:
    @pytest.mark.parametrize("key,url,ok,code", [
        ("deepseek", "https://api.deepseek.com/chat/completions", True, "ok"),
        ("openai", "https://api.openai.com/v1/responses", True, "ok"),
        ("google-gemini",
         "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
         True, "ok"),
        ("xai", "https://api.x.ai/v1/chat/completions", True, "ok"),
        ("anthropic", "https://api.anthropic.com/v1/messages", True, "ok"),
        ("deepseek", "http://api.deepseek.com/x", False, "scheme"),
        ("deepseek", "https://evil.deepseek.com/x", False, "host"),
        ("deepseek", "https://api.deepseek.com.evil.com/x", False, "host"),
        ("deepseek", "https://user:pw@api.deepseek.com/x", False, "userinfo"),
        ("sudowrite", "https://anything.example/x", False, "host"),
        ("openai", "https://api.openai.com:443/v1/x", True, "ok"),
    ])
    def test_egress_allowed(self, key, url, ok, code):
        assert lg.egress_allowed(key, url) == (ok, code)

    @pytest.mark.parametrize("location,ok,code", [
        ("https://api.deepseek.com/other", True, "ok"),
        ("https://evil.example/x", False, "host"),
        ("http://api.deepseek.com/x", False, "scheme"),
        ("//api.deepseek.com/x", False, "scheme"),
    ])
    def test_redirect_gate(self, location, ok, code):
        assert lg.redirect_allowed("deepseek", location) == (ok, code)

    @pytest.mark.parametrize("headers,want", [
        ({"Retry-After": "5"}, 5.0), ({"retry-after": "0.5"}, 0.5),
        ({"Retry-After": "120"}, 60.0), ({"Retry-After": "abc"}, None),
        ({"Retry-After": "-3"}, None), ({}, None), (None, None),
    ])
    def test_retry_after(self, headers, want):
        assert lg.retry_after_s(headers) == want

    def test_timeouts_cover_every_contract(self):
        for key in ("openai", "anthropic", "google-gemini", "deepseek", "xai"):
            t = lg.LIVE_TIMEOUTS[key]
            assert t["connect_s"] > 0 and t["total_s"] >= t["connect_s"]
        assert lg.LIVE_TIMEOUTS["openai"]["poll_deadline_s"] > 0


# ---------------- provenance isolation ----------------

class TestProvenanceGate:
    # B1 corrected contract: endpoint_host + correlation + fingerprint +
    # evidence_url required; remote_job_id OPTIONAL (recorded when the
    # provider supplies one, never fabricated).
    FULL_EV = {"transport": "live", "authenticated": True,
               "environment": "live", "endpoint_host": "api.deepseek.com",
               "correlation": {"job_id": "j1", "command_id": "c1",
                               "remote_job_id": "resp_1"},
               "response_fingerprint": "ab12", "evidence_url": "https://x"}

    def test_evidence_never_valid_on_non_live_transport(self):
        # Stage A: transport_is_live is always False — unreachable.
        assert not lg.verification_evidence_valid(
            self.FULL_EV, transport_is_live=False, provider_key="deepseek")
        assert not lg.verification_evidence_valid(
            self.FULL_EV, transport_is_live=False, provider_key="deepseek")

    def test_evidence_contract(self):
        assert lg.verification_evidence_valid(
            self.FULL_EV, transport_is_live=True, provider_key="deepseek")
        # remote_job_id absent entirely — still valid (sync responses
        # may expose none).
        ev = {**self.FULL_EV,
              "correlation": {"job_id": "j1", "command_id": "c1"}}
        assert lg.verification_evidence_valid(
            ev, transport_is_live=True, provider_key="deepseek")
        for missing in ("authenticated", "response_fingerprint",
                        "evidence_url", "endpoint_host"):
            ev = {**self.FULL_EV, missing: None}
            assert not lg.verification_evidence_valid(
                ev, transport_is_live=True, provider_key="deepseek")
        # job correlation is mandatory — an unattributed response can't
        # be verified.
        ev = {**self.FULL_EV,
              "correlation": {"command_id": "c1"}}
        assert not lg.verification_evidence_valid(
            ev, transport_is_live=True, provider_key="deepseek")
        # Host must be the documented endpoint — a foreign host fails.
        ev = {**self.FULL_EV, "endpoint_host": "api.evil.example"}
        assert not lg.verification_evidence_valid(
            ev, transport_is_live=True, provider_key="deepseek")

    def test_simulated_remote_never_verified(self):
        assert provenance_confidence({"remote_job_id": "r1"}) == "simulated"
        assert provenance_confidence(None) == "simulated"
        # provider_verified alone proves nothing — valid live evidence
        # on a real transport is mandatory.
        assert provenance_confidence(
            {"remote_job_id": "r1", "provider_verified": True}
        ) == "simulated"
        assert provenance_confidence(
            {"provider_key": "deepseek", "provider_verified": True,
             "transport_is_live": True,
             "verification_evidence": self.FULL_EV}
        ) == "verified_remote"


# ---------------- zero egress ----------------

class TestZeroEgress:
    def test_mock_transport_still_mandatory(self):
        import httpx
        from app.domain.workers.rest_providers import (
            EgressRefused, make_provider)
        from app.domain.workers.rest_simulated import simulated_handler
        for transport in (None, httpx.HTTPTransport(), httpx.AsyncHTTPTransport()):
            for key in ("openai", "anthropic", "google-gemini",
                        "deepseek", "xai"):
                with pytest.raises(EgressRefused):
                    make_provider(key, api_key="dummy", transport=transport)
        # mock works
        p = make_provider("deepseek", api_key="dummy",
                          transport=httpx.MockTransport(
                              simulated_handler("deepseek")))
        assert p is not None


# ---------------- status endpoint ----------------

class TestStatusEndpoint:
    def test_status_report_shape(self, client, vault):
        p = make_project(client)
        _budget(client, p["id"])
        _commit(client, p["id"], "cloud.live.enable", EN)
        r = client.get(f"/api/projects/{p['id']}/cloud/live-status")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "safety gates" in body["stage"]
        assert body["global_disabled"] is False
        assert body["caps_usd"] == {"per_request": "0.10",
                                  "per_provider": "0.15", "global": "0.75"}
        ds = body["providers"]["deepseek"]
        assert ds["enabled"] and ds["dispatch_ready"] and ds["blocked_reason"] is None
        assert ds["credential_configured"] is True
        xai = body["providers"]["xai"]
        assert xai["dispatch_ready"] is False
        assert xai["blocked_reason"] == "not_enabled"

    def test_status_reflects_kill(self, client, vault):
        p = make_project(client)
        _budget(client, p["id"])
        _commit(client, p["id"], "cloud.live.disable_all", {})
        body = client.get(f"/api/projects/{p['id']}/cloud/live-status").json()
        assert body["global_disabled"] is True
        assert body["providers"]["deepseek"]["blocked_reason"] == "global_kill"


# ---------------- independent execution boundary ----------------
# cloud.live.enable configures eligibility ONLY. Nothing — stored
# opt-in, restored DB, env var, config, or client payload — can make a
# job account as "live" or reach a transport. The flip below simulates
# a hypothetical live adapter to prove the independent gate engages.

class TestExecutionBoundary:
    def _rest_payload(self, **kw):
        return {"provider_key": "deepseek", "model_id": "deepseek-chat",
                "prompt": "Write a scene beat.",
                "execution_target": "cloud", **kw}

    def _res_env(self, client, job_id):
        with _sf(client)() as s:
            return s.execute(select(SpendReservation).where(
                SpendReservation.job_id == job_id)).scalar_one().environment

    def test_enable_does_not_unlock_live_accounting(self, client, vault):
        """Full opt-in granted; the all-mock adapter still books
        environment='simulated' — eligibility is not dispatch."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        assert _commit(client, pid, "cloud.live.enable", EN).status_code == 200
        r = _commit(client, pid, "generate.text.cloud",
                    self._rest_payload(max_cost_usd="0.05"))
        assert r.status_code == 200, r.text
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        assert self._res_env(client, job["id"]) == "simulated"

    def test_payload_cannot_force_live_environment(self, client, vault):
        """Client-supplied environment/spend_environment keys are
        ignored — environment derives from the wired transports only."""
        pid = make_project(client)["id"]
        r = _commit(client, pid, "generate.text.cloud",
                    self._rest_payload(environment="live",
                                       spend_environment="live",
                                       simulated=False,
                                       max_cost_usd="0.05"))
        assert r.status_code == 200, r.text
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert self._res_env(client, job["id"]) == "simulated"

    def test_live_reporting_adapter_refused_without_optin(self, client,
                                                        vault, monkeypatch):
        """A hypothetical live transport alone cannot book: the
        independent gate refuses with not_enabled."""
        adapter = client.app.state.adapter_registry.get("cloud.text.rest")
        prov = adapter.providers["deepseek"]
        monkeypatch.setattr(type(prov), "transport_is_simulated",
                            property(lambda s: False))
        pid = make_project(client)["id"]
        _budget(client, pid)
        r = _commit(client, pid, "generate.text.cloud",
                    self._rest_payload(max_cost_usd="0.05"))
        assert r.status_code == 422, r.text
        assert "not_enabled" in r.text

    def test_live_reporting_adapter_full_gate_then_mock_run(self, client,
                                                          vault, monkeypatch):
        """Opt-in + live flag: admission passes inside the caps and the
        reservation books env='live' — but the job still executes
        against MockTransport (zero egress) and provenance stays
        simulated. Nothing dispatched for real."""
        adapter = client.app.state.adapter_registry.get("cloud.text.rest")
        prov = adapter.providers["deepseek"]
        monkeypatch.setattr(type(prov), "transport_is_simulated",
                            property(lambda s: False))
        pid = make_project(client)["id"]
        _budget(client, pid)
        assert _commit(client, pid, "cloud.live.enable", EN).status_code == 200
        r = _commit(client, pid, "generate.text.cloud",
                    self._rest_payload(max_cost_usd="0.05"))
        assert r.status_code == 200, r.text
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert job["status"] == "succeeded", job.get("error")
        assert self._res_env(client, job["id"]) == "live"
        with _sf(client)() as s:
            from app.db.models import GenerationJob
            detail = (s.get(GenerationJob, job["id"]).progress_json or {})
        assert provenance_confidence(detail) == "simulated"


# ---------------- atomic financial admission ----------------
# The caps are enforced inside spend.admit under BEGIN IMMEDIATE — the
# write lock serializes check-then-act so no race overbooks exposure.

class TestAtomicFinancialAdmission:
    def _live_admit(self, s, pid, usd="0.05"):
        return spend.admit(s, job_id=f"lg-{new_id()[:8]}",
                           command_id=None, project_id=pid,
                           provider_key="deepseek", environment="live",
                           est_usd=usd, max_usd=usd)

    def test_booking_layer_enforces_gate(self, client, vault):
        """spend.admit(environment='live') refuses without a grant, while
        one is in flight, and under the kill switch — independent of any
        upstream pre-flight."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        with _sf(client)() as s:
            with pytest.raises(spend.BudgetError) as ei:
                self._live_admit(s, pid)
            assert ei.value.detail["error"] == "not_enabled"
        assert _commit(client, pid, "cloud.live.enable", EN).status_code == 200
        with _sf(client)() as s:
            self._live_admit(s, pid)          # books an OPEN reservation
            s.commit()
        with _sf(client)() as s:
            with pytest.raises(spend.BudgetError) as ei:
                self._live_admit(s, pid)      # one in flight → refused
            assert ei.value.detail["error"] == "concurrency"
        _commit(client, pid, "cloud.live.disable_all", {})
        with _sf(client)() as s:
            with pytest.raises(spend.BudgetError) as ei:
                self._live_admit(s, pid)
            assert ei.value.detail["error"] == "global_kill"

    def test_admit_caps_atomic(self, client, vault):
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        _seed_live_exposure(client, "deepseek",
                            charged_micro=usd_to_micro("0.12"))
        with _sf(client)() as s:
            with pytest.raises(spend.BudgetError) as ei:
                self._live_admit(s, pid, usd="0.11")  # > $0.10/request
            assert ei.value.detail["error"] == "cap_per_request"
            with pytest.raises(spend.BudgetError) as ei:
                self._live_admit(s, pid)              # 0.12+0.05 > $0.15
            assert ei.value.detail["error"] == "cap_per_provider"

    def test_concurrent_independent_connections(self, client, vault):
        """Two separate DB connections race the remaining provider cap:
        BEGIN IMMEDIATE serializes them — exactly one books, the loser
        re-reads post-commit consumption and is refused. Session ids
        rotate per connection yet exposure is all-time: rotation cannot
        discount the cap."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        _seed_live_exposure(client, "deepseek",
                            charged_micro=usd_to_micro("0.07"))
        barrier = threading.Barrier(2)
        results: dict[str, str] = {}

        def attempt(tag: str):
            with _sf(client)() as s:
                barrier.wait(timeout=10)
                # Winner commits fast; loser's BEGIN retries under
                # busy_timeout, then sees post-commit consumption.
                s.execute(text("BEGIN IMMEDIATE"))
                try:
                    self._live_admit(s, pid)          # 0.07+0.05 ≤ 0.15
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
        # Loser: 0.07 settled + 0.05 open + 0.05 requested > $0.15 cap.
        assert sorted(results.values()) == ["admitted", "cap_per_provider"]


# ---------------- durable authorization state ----------------

class TestDurabilityBoundaries:
    def test_catalog_refresh_preserves_gate_state(self, client, vault):
        """Registry config writes share the app_settings table but a
        different key — a catalog refresh/merge cannot reset the kill
        switch or resurrect a revoked grant."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        _commit(client, pid, "cloud.live.disable_all", {})
        with _sf(client)() as s:
            registry.set_provider_config(s, "deepseek",
                                         {"enabled": True,
                                          "settings": {"x": 1}})
            registry.set_default(s, "text", "deepseek")
            s.commit()
        with _sf(client)() as s:
            st = lg.state(s)
        assert st["global_disabled"] is True
        entry = st["providers"]["deepseek"]
        assert entry["enabled"] is False
        assert entry["revoked_reason"] == "kill_switch"

    def test_credential_removal_invalidates_eligibility(self, client, vault):
        """Deleting the referenced credential leaves the grant row but
        blocks all future admission — eligibility, never dispatch."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        vault._c = False  # credential deleted underneath the grant
        with _sf(client)() as s:
            allowed, code = lg.dispatch_admission(
                s, provider_key="deepseek", model_id="deepseek-chat",
                max_cost_usd="0.05", vault=vault)
        assert (allowed, code) == (False, "credential")
        body = client.get(f"/api/projects/{pid}/cloud/live-status").json()
        ds = body["providers"]["deepseek"]
        assert ds["credential_configured"] is False
        assert ds["blocked_reason"] == "credential"

    def test_backup_cannot_carry_authorization(self):
        """Project export/import whitelists entity tables only —
        app_settings (gate + billing session), spend rows and provider
        config never ride a package, so restoring or importing a project
        cannot grant or resurrect live authorization."""
        from app.domain import backup
        from app.db import models as m
        exported = set(backup.ENTITY_TABLES.values())
        for forbidden in (m.AppSetting, m.SpendReservation, m.SpendCharge,
                          m.SpendBudget):
            assert forbidden not in exported
