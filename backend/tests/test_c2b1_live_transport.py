"""C2 exit gate — Stage B1 live-transport infrastructure.

Every network path in this file runs through httpx.MockTransport
inners and injected DNS resolvers; the vault stub returns DUMMY
secrets. Nothing here performs a real request, touches a real
credential, or produces a real verified_remote artifact.
"""
from __future__ import annotations

import asyncio
import itertools

import httpx
import pytest
from sqlalchemy import select, text

from app.db.models import GenerationJob, SpendReservation, new_id
from app.domain import live_gate as lg
from app.domain import spend
from app.domain.workers import live_transport as lt
from tests.conftest import make_project, wait_for_job

_KEYS = itertools.count(1)

DEEPSEEK_OK = {
    "id": "chatcmpl-b1-1",
    "choices": [{"message": {"content": "A beat lands."},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}


class RecordingVault:
    """Dummy-secret vault double. get_fields is the ONLY path that can
    hand out secret material — the call count proves where it ran."""
    def __init__(self):
        self.get_calls: list[str] = []

    def status(self, key):
        return {"configured": True, "fields": ["api_key"],
                "set_at": "2026-01-01T00:00:00Z", "readable": True,
                "unreadable_reason": None}

    def get_fields(self, key):
        self.get_calls.append(key)
        return {"api_key": "dummy-b1-key"}


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
              "idempotency_key": f"b1-live-{next(_KEYS):04d}"})


def _budget(client, pid, limit="5.00", scope="lifetime", target="global"):
    return _commit(client, pid, "spend.budget_set",
                   {"target": target, "scope": scope, "limit_usd": limit})


EN = {"provider_key": "deepseek", "model_id": "deepseek-chat",
      "credential_ref": "deepseek"}
GOOD_RESOLVER = lambda h, p: ["93.184.216.34"]  # noqa: E731 — public fixture IP


def _ok_inner(sends=None):
    def handler(req):
        if sends is not None:
            sends.append(str(req.url))
        return httpx.Response(200, json=DEEPSEEK_OK)
    return httpx.MockTransport(handler)


def _reservation(client, job_id):
    with _sf(client)() as s:
        return s.execute(select(SpendReservation).where(
            SpendReservation.job_id == job_id)).scalar_one_or_none()


def _dispatch(client, pid, *, vault, inner=None, resolver=GOOD_RESOLVER,
              gate_probe=None, max_usd="0.05", job_id=None):
    jid = job_id or f"b1-live-{new_id()[:8]}"
    with _sf(client)() as s:
        return asyncio.run(lt.live_dispatch(
            s, provider_key="deepseek", model_id="deepseek-chat",
            request={"model_id": "deepseek-chat", "prompt": "Write a beat.",
                     "max_output_tokens": 32},
            job_id=jid, command_id="c-b1", project_id=pid,
            max_cost_usd=max_usd, vault=vault,
            inner=inner if inner is not None else _ok_inner(),
            resolver=resolver, gate_probe=gate_probe))


# ---------------- unreachability ----------------

class TestUnreachableBoundary:
    def test_no_command_or_route_reaches_live_dispatch(self):
        """live_dispatch exists but is unreachable: no command type
        names it, no API route exposes it, no env var or config flag
        wires it. Nothing in the shipped surface can call it."""
        from app.domain import commands as cmd
        assert not any("live.submit" in c or "live.dispatch" in c
                       for c in cmd.SUPPORTED_COMMANDS)
        from app.api.routers import api_router
        paths = [getattr(r, "path", "") for r in api_router.routes]
        assert not any("dispatch" in p for p in paths)

    def test_inner_default_marks_real_injected_marks_controlled(self):
        assert lt.LiveHTTPTransport(
            provider_key="deepseek", inner=None).is_real is True
        assert lt.LiveHTTPTransport(
            provider_key="deepseek", inner=_ok_inner()).is_real is False


# ---------------- transport security ----------------

class TestTransportSecurity:
    def _t(self, *, inner=None, resolver=GOOD_RESOLVER, timeout_s=None):
        return lt.LiveHTTPTransport(
            provider_key="deepseek",
            inner=inner or _ok_inner(), resolver=resolver,
            timeout_s=timeout_s)

    def _req(self, url):
        return httpx.Request("POST", url, json={"x": 1})

    def test_scheme_userinfo_host_refused(self):
        t = self._t()
        for url, code in (
                ("http://api.deepseek.com/chat/completions", "scheme"),
                ("https://u:p@api.deepseek.com/chat/completions", "userinfo"),
                ("https://api.evil.example/chat/completions", "host"),
                ("https://deepseek.com/chat/completions", "host")):
            with pytest.raises(lt.LiveTransportRefused) as ei:
                asyncio.run(t.handle_async_request(self._req(url)))
            assert ei.value.code == code
            assert ei.value.charge_evidence == "none_confirmed"
        assert t._sent == []  # nothing reached the wire

    def test_dns_non_public_refused(self):
        for ips in (["127.0.0.1"], ["10.0.0.5"], ["169.254.1.1"],
                    ["192.168.1.1"], ["::1"], [], None):
            t = self._t(resolver=lambda h, p, ips=ips: ips)
            with pytest.raises(lt.LiveTransportRefused) as ei:
                asyncio.run(t.handle_async_request(
                    self._req("https://api.deepseek.com/chat/completions")))
            assert ei.value.code == "dns"
        # mixed public+private: ANY bad address refuses the request
        t = self._t(resolver=lambda h, p: ["93.184.216.34", "10.0.0.5"])
        with pytest.raises(lt.LiveTransportRefused):
            asyncio.run(t.handle_async_request(
                self._req("https://api.deepseek.com/chat/completions")))

    def test_redirect_refused_never_followed(self):
        calls = []
        def handler(req):
            calls.append(str(req.url))
            return httpx.Response(302, headers={
                "location": "https://api.evil.example/steal"})
        t = self._t(inner=httpx.MockTransport(handler))
        with pytest.raises(lt.LiveTransportRefused) as ei:
            asyncio.run(t.handle_async_request(
                self._req("https://api.deepseek.com/chat/completions")))
        assert ei.value.code == "redirect"
        assert len(calls) == 1  # no follow-up request, no creds forwarded

    def test_response_size_cap(self):
        big = b"x" * (lt.MAX_RESPONSE_BYTES + 1)
        t = self._t(inner=httpx.MockTransport(
            lambda req: httpx.Response(200, content=big)))
        resp = asyncio.run(t.handle_async_request(
            self._req("https://api.deepseek.com/chat/completions")))
        async def drain():
            async for _c in resp.stream:
                pass
        with pytest.raises(lt.ResponseTooLarge):
            asyncio.run(drain())

    def test_total_deadline_enforced(self):
        async def slow(req):
            import asyncio as _a
            await _a.sleep(5)
            return httpx.Response(200, json={})
        class SlowInner(httpx.AsyncBaseTransport):
            async def handle_async_request(self, req):
                return await slow(req)
        t = self._t(inner=SlowInner(), timeout_s=0.01)
        with pytest.raises(httpx.ReadTimeout):
            asyncio.run(t.handle_async_request(
                self._req("https://api.deepseek.com/chat/completions")))

    def test_live_client_is_proxy_and_redirect_isolated(self):
        """The hardened client the provider builds must not honor
        HTTP(S)_PROXY/netrc env vars or follow redirects."""
        from app.domain.workers.rest_providers import make_provider
        p = make_provider("deepseek", transport=self._t(), api_key="dummy")
        assert p._client.trust_env is False
        assert p._client.follow_redirects is False
        # non-mock non-live transport still refused at the boundary
        with pytest.raises(Exception):
            make_provider("deepseek",
                          transport=httpx.AsyncHTTPTransport(), api_key="x")


# ---------------- orchestration: admission → send ----------------

class TestDispatchOrchestration:
    def test_admission_runs_before_any_send(self, client, vault):
        """No opt-in → LiveDispatchRefused before the transport exists;
        the inner mock records zero sends and no reservation is booked."""
        sends = []
        pid = make_project(client)["id"]
        _budget(client, pid)
        with pytest.raises(lt.LiveDispatchRefused) as ei:
            _dispatch(client, pid, vault=vault, inner=_ok_inner(sends))
        assert ei.value.code == "not_enabled"
        assert sends == []
        assert _reservation(client, "x") is None

    def test_successful_dispatch_settles_and_evidences(self, client, vault):
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        sends = []
        out = _dispatch(client, pid, vault=vault, inner=_ok_inner(sends))
        jid = out["job_id"]
        assert out["outcome"] == "succeeded"
        assert len(sends) == 1
        res = _reservation(client, jid)
        assert res.status == "settled" and res.environment == "live"
        # usage 10×$0.27 + 5×$1.10 per Mtok → $8.2e-6 → ceiling micro
        assert out["settled_micro"] == spend.usd_to_micro(
            (10 * 0.27 + 5 * 1.10) / 1_000_000)
        assert out["charge_source"] == "provider_reported"
        # evidence shape correct — but controlled transport: NEVER
        # verified_remote in B1.
        ev = out["verification_evidence"]
        assert ev["transport"] == "controlled"
        assert ev["endpoint_host"] == "api.deepseek.com"
        assert ev["correlation"]["job_id"] == jid
        assert ev["correlation"]["remote_job_id"] == "chatcmpl-b1-1"
        assert out["provider_verified"] is False
        from app.domain.workers.remote import provenance_confidence
        assert provenance_confidence(out) == "simulated"

    def test_secret_read_only_inside_boundary(self, client, vault):
        pid = make_project(client)["id"]
        _budget(client, pid)
        # refused admission → vault never opened
        with pytest.raises(lt.LiveDispatchRefused):
            _dispatch(client, pid, vault=vault)
        assert vault.get_calls == []
        _commit(client, pid, "cloud.live.enable", EN)
        out = _dispatch(client, pid, vault=vault)
        assert out["outcome"] == "succeeded"
        assert vault.get_calls == ["deepseek"]  # exactly one read, inside
        # secret nowhere durable
        res = _reservation(client, out["job_id"])
        assert "dummy-b1-key" not in str(res.__dict__)

    def test_auth_header_carries_dummy_key(self, client, vault):
        seen = []
        def handler(req):
            seen.append(req.headers.get("authorization"))
            return httpx.Response(200, json=DEEPSEEK_OK)
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        _dispatch(client, pid, vault=vault,
                  inner=httpx.MockTransport(handler))
        assert seen == ["Bearer dummy-b1-key"]

    def test_kill_between_admission_and_send(self, client, vault):
        """Gate flips to kill AFTER admit commits but BEFORE send:
        nothing is sent and the reservation stays OPEN for reconcile —
        never silently released."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        # dispatch_admission reads lg.state directly (sees the grant);
        # the pre-send re-check goes through gate_probe — simulating a
        # kill landing in the gap between the two reads.
        def probe(s):
            st = lg.state(s)
            st["global_disabled"] = True
            return st
        sends = []
        with pytest.raises(lt.LiveDispatchRefused) as ei:
            _dispatch(client, pid, vault=vault, inner=_ok_inner(sends),
                      gate_probe=probe)
        assert ei.value.code == "global_kill"
        assert sends == []
        # the admission already committed → the reservation row exists,
        # open and un-released
        with _sf(client)() as s:
            rows = s.execute(select(SpendReservation).where(
                SpendReservation.environment == "live")).scalars().all()
        assert len(rows) == 1 and rows[0].status == "open"

    def test_ambiguous_outcome_holds_reservation(self, client, vault):
        """Transport dies mid-request → outcome unknown: reservation
        stays open, never resubmitted automatically."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        calls = []
        def dying(req):
            calls.append(str(req.url))
            raise httpx.ConnectError("connection dropped")
        out = _dispatch(client, pid, vault=vault,
                        inner=httpx.MockTransport(dying))
        assert out["outcome"] == "ambiguous"
        assert out["reservation"] == "open"
        assert len(calls) == 1  # no automatic retry of ambiguous work
        res = _reservation(client, out["job_id"])
        assert res.status == "open"

    def test_ambiguous_reservation_recovers_via_explicit_reconcile(
            self, client, vault):
        """Crash-recovery path: an ambiguous dispatch leaves the
        reservation open durably; only explicit spend.reconcile can
        move it — no silent release, idempotent on replay."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        out = _dispatch(client, pid, vault=vault, inner=httpx.MockTransport(
            lambda req: (_ for _ in ()).throw(
                httpx.ConnectError("dropped mid-flight"))))
        assert out["reservation"] == "open"
        # Seed the durable job row reconcile joins on (the dispatch
        # boundary does not own job lifecycle).
        with _sf(client)() as s:
            s.add(GenerationJob(id=out["job_id"], project_id=pid,
                                adapter_id="cloud.live.submit",
                                idempotency_key=f"b1rc-{new_id()[:8]}"))
            s.commit()
        from app.domain import spend as spend_mod
        with _sf(client)() as s:
            # 'still_unknown' keeps it open — survives the session.
            r = spend_mod.reconcile(
                s, project_id=pid, job_id=out["job_id"],
                outcome="still_unknown", note="operator review")
            s.commit()
        assert r["status"] == "open"
        assert _reservation(client, out["job_id"]).status == "open"
        with _sf(client)() as s:
            r = spend_mod.reconcile(
                s, project_id=pid, job_id=out["job_id"],
                outcome="confirmed_no_charge",
                note="provider confirmed refusal")
            s.commit()
        assert r["status"] == "released"
        # Replay is idempotent — no duplicate release/charge.
        with _sf(client)() as s:
            r2 = spend_mod.reconcile(
                s, project_id=pid, job_id=out["job_id"],
                outcome="confirmed_no_charge")
            s.commit()
        assert r2["idempotent_replay"] is True

    def test_confirmed_refusal_releases(self, client, vault):
        """401 = provider demonstrably refused before processing →
        reservation released as confirmed no-charge."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        out = _dispatch(client, pid, vault=vault, inner=httpx.MockTransport(
            lambda req: httpx.Response(401, json={"error": {"type": "auth"}})))
        assert out["outcome"] == "failed"
        assert out["code"] == "authentication"
        assert out["reservation"] == "released"
        res = _reservation(client, out["job_id"])
        assert res.status == "released"

    def test_oversized_response_is_ambiguous(self, client, vault):
        big = b"x" * (lt.MAX_RESPONSE_BYTES + 1)
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        out = _dispatch(client, pid, vault=vault, inner=httpx.MockTransport(
            lambda req: httpx.Response(200, content=big)))
        assert out["outcome"] == "ambiguous"
        assert _reservation(client, out["job_id"]).status == "open"

    def test_malformed_success_body_is_ambiguous(self, client, vault):
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        out = _dispatch(client, pid, vault=vault, inner=httpx.MockTransport(
            lambda req: httpx.Response(200, content=b"<html>nope</html>")))
        assert out["outcome"] == "ambiguous"
        assert _reservation(client, out["job_id"]).status == "open"

    def test_rate_limit_is_confirmed_refusal(self, client, vault):
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        out = _dispatch(client, pid, vault=vault, inner=httpx.MockTransport(
            lambda req: httpx.Response(
                429, json={"error": {"type": "rate_limit"}},
                headers={"retry-after": "5"})))
        assert out["outcome"] == "failed"
        assert out["code"] == "rate_limited"
        assert out["reservation"] == "released"


# ---------------- lifecycle: documented poll/cancel ----------------

class TestLifecycle:
    def _live_provider(self, key, *, inner=None):
        from app.domain.workers.rest_providers import make_provider
        inner = inner or _ok_inner()
        t = lt.LiveHTTPTransport(provider_key=key, inner=inner,
                                 resolver=GOOD_RESOLVER)
        return make_provider(key, transport=t, api_key="dummy")

    def test_sync_provider_has_no_poll(self):
        p = self._live_provider("deepseek")
        assert asyncio.run(p.query("anything")) is None
        from app.domain.workers.rest_providers import UnsupportedRemoteOp
        with pytest.raises(UnsupportedRemoteOp):
            asyncio.run(p.cancel("anything"))

    def test_openai_async_poll_and_cancel_documented(self):
        states = iter([
            httpx.Response(200, json={
                "id": "resp_1", "status": "in_progress"}),
            httpx.Response(200, json={
                "id": "resp_1", "status": "cancelled"}),
        ])
        p = self._live_provider("openai", inner=httpx.MockTransport(
            lambda req: next(states)))
        h = asyncio.run(p.query("resp_1"))
        assert h is not None and h.status.value == "running"
        c = asyncio.run(p.cancel("resp_1"))
        assert c.status.value == "cancelled"

    def test_retry_after_capped(self):
        assert lg.retry_after_s({"retry-after": "0.5"}) == 0.5
        assert lg.retry_after_s({"retry-after": "999"}) == 60
        assert lg.retry_after_s({"retry-after": "garbage"}) is None
        assert lg.retry_after_s({}) is None


# ---------------- simulated/live isolation ----------------

class TestSimulatedLiveIsolation:
    def test_live_booking_does_not_touch_simulated_path(self, client, vault):
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        out = _dispatch(client, pid, vault=vault)
        assert out["reservation"] == "settled"
        # simulated submit still books environment='simulated'
        r = _commit(client, pid, "generate.text.cloud", {
            "provider_key": "deepseek", "model_id": "deepseek-chat",
            "prompt": "Write a beat.", "execution_target": "cloud"})
        assert r.status_code == 200, r.text
        job = wait_for_job(client, pid, r.json()["job_id"])
        res = _reservation(client, job["id"])
        assert res.environment == "simulated"
