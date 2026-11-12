"""C2.0c+ — REST provider contracts over mocked transports ONLY.

Five documented A-class text contracts across two lifecycles:
- OpenAI Responses (background mode → real resp_* operation id →
  documented poll + cancel);
- Anthropic Messages, Gemini generateContent, DeepSeek and xAI chat
  completions (synchronous → the response IS the completion; no
  documented poll/cancel exists for single requests).

Every test uses httpx.MockTransport — zero egress is structural (the
provider constructor refuses anything else). All outputs remain
simulated: mocked response ids/request ids never establish
verified_remote provenance.
"""

from __future__ import annotations

import itertools
import json

import httpx
import pytest
from sqlalchemy import select

from app.db.models import Asset, AuditEvent, SpendCharge, SpendReservation
from app.domain.workers import remote as remote_mod
from app.domain.workers.rest_providers import (
    EgressRefused, REST_PROVIDER_KEYS,
    UnsupportedRemoteOp, make_provider)
from app.domain.workers.rest_simulated import (
    anthropic_message_obj, chat_completion_obj, gemini_generate_obj,
    openai_response_obj, simulated_handler, simulated_transport)
from tests.conftest import make_project, wait_for_job

_KEYS = itertools.count()


def _rev(client, pid):
    return client.get(f"/api/projects/{pid}").json()["revision"]


def _commit(client, pid, payload, key_prefix="c2rest",
            ctype="generate.text.cloud"):
    return client.post(
        f"/api/projects/{pid}/commands/commit",
        json={"command_type": ctype, "payload": payload,
              "scope": "project", "expected_revision": _rev(client, pid),
              "idempotency_key": f"{key_prefix}-{next(_KEYS):04d}"})


def _payload(**kw):
    return {"provider_key": "openai", "prompt": "Write a scene beat.",
            "execution_target": "cloud", **kw}


def _sf(client):
    return client.app.state.session_factory


def _adapter(client):
    return client.app.state.adapter_registry.get("cloud.text.rest")


def _swap(client, monkeypatch, key, provider):
    """Point the registry adapter's provider at a scenario transport."""
    monkeypatch.setitem(_adapter(client).providers, key, provider)


def _detail(client, job_id) -> dict:
    from app.db.models import GenerationJob
    with _sf(client)() as s:
        return dict((s.get(GenerationJob, job_id).progress_json or {}))


def _reservation(client, job_id):
    with _sf(client)() as s:
        return s.execute(select(SpendReservation).where(
            SpendReservation.job_id == job_id)).scalar_one_or_none()


def _charges(client, job_id):
    with _sf(client)() as s:
        return s.execute(select(SpendCharge).where(
            SpendCharge.job_id == job_id)).scalars().all()


def _status_handler(status: int, body: dict | None = None):
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, content=json.dumps(
                body or {"error": {"type": "test_error",
                                   "message": "boom"}}).encode(),
            headers={"Content-Type": "application/json"})
    return h


def _submit_status_transport(key, status, body=None):
    """Default simulated routes except submit returns `status`."""
    return simulated_transport(
        key, {"submit": _status_handler(status, body)})


# ---------- transport guard / construction ----------

def test_no_transport_refused():
    for key in REST_PROVIDER_KEYS:
        with pytest.raises(EgressRefused):
            make_provider(key, transport=None)


def test_live_transport_refused():
    """Accidental live egress construction is rejected at the boundary —
    AsyncHTTPTransport, the pool transport, and any non-mock object."""
    for key in REST_PROVIDER_KEYS:
        with pytest.raises(EgressRefused):
            make_provider(key, transport=httpx.AsyncHTTPTransport())
        with pytest.raises(EgressRefused):
            make_provider(key, transport=httpx.HTTPTransport())
        with pytest.raises(EgressRefused):
            make_provider(key, transport=object())


def test_unknown_provider_key_refused():
    """Catalog-eligible but contract-less providers (rytr) and unknown
    keys both fail closed — no silent fallback to a generic shape."""
    for key in ("rytr", "not-a-provider"):
        with pytest.raises(KeyError):
            make_provider(key,
                          transport=httpx.MockTransport(lambda r: None))


def test_registry_adapter_all_mocked(client):
    ad = _adapter(client)
    assert ad is not None
    assert set(ad.providers) == set(REST_PROVIDER_KEYS)
    for key in REST_PROVIDER_KEYS:
        assert ad.supports_provider(key)
    assert not ad.supports_provider("rytr")
    assert ad.spend_environment == "simulated"
    for p in ad.providers.values():
        assert p.transport_is_simulated


def test_adapter_rejects_non_mock_provider():
    class _Live:
        transport_is_simulated = False
    with pytest.raises(EgressRefused):
        from app.adapters.cloud_rest import RestCloudTextAdapter
        RestCloudTextAdapter({"x": _Live()})


def test_adapter_routing_by_provider(client):
    from app.domain.commands import _cloud_adapter_id
    reg = client.app.state.adapter_registry
    assert _cloud_adapter_id(reg, "openai") == "cloud.text.rest"
    assert _cloud_adapter_id(reg, "anthropic") == "cloud.text.rest"
    assert _cloud_adapter_id(reg, "google-gemini") == "cloud.text.rest"
    assert _cloud_adapter_id(reg, "deepseek") == "cloud.text.rest"
    assert _cloud_adapter_id(reg, "xai") == "cloud.text.rest"
    # rytr stays mock-routed — no REST contract implementation.
    assert _cloud_adapter_id(reg, "rytr") == "cloud.text.mock"
    assert set(REST_PROVIDER_KEYS) == {
        "openai", "anthropic", "google-gemini", "deepseek", "xai"}


# ---------- auth header construction ----------

@pytest.mark.asyncio
async def test_openai_auth_headers():
    seen = {}
    def h(req: httpx.Request) -> httpx.Response:
        seen.update(dict(req.headers))
        return httpx.Response(200, content=json.dumps(
            openai_response_obj(resp_id="r1", status="queued")).encode(),
            headers={"Content-Type": "application/json"})
    p = make_provider("openai", transport=httpx.MockTransport(h),
                      api_key="dummy-test-key")
    rh = await p.submit(remote_mod.SubmissionIntent(
        job_id="j", idempotency_key="k", adapter_id="a",
        request={"model_id": "gpt-5", "prompt": "hi"}))
    assert rh.remote_id == "r1"
    assert seen["authorization"] == "Bearer dummy-test-key"


@pytest.mark.asyncio
async def test_anthropic_auth_headers():
    seen = {}
    def h(req: httpx.Request) -> httpx.Response:
        seen.update(dict(req.headers))
        return httpx.Response(200, content=json.dumps(
            anthropic_message_obj(msg_id="m1")).encode(),
            headers={"Content-Type": "application/json"})
    p = make_provider("anthropic", transport=httpx.MockTransport(h),
                      api_key="dummy-test-key")
    rh = await p.submit(remote_mod.SubmissionIntent(
        job_id="j", idempotency_key="k", adapter_id="a",
        request={"model_id": "claude-sonnet-4-5", "prompt": "hi"}))
    assert rh.remote_id == "m1"
    assert seen["x-api-key"] == "dummy-test-key"
    assert seen["anthropic-version"] == "2023-06-01"
    assert "authorization" not in seen or \
        "dummy" not in seen.get("authorization", "")


@pytest.mark.asyncio
async def test_gemini_auth_headers_and_path():
    """Gemini authenticates via the x-goog-api-key HEADER (never a
    ?key= query param, which would leak into proxy logs) and carries
    the model id in the URL path — unique to this contract."""
    seen = {}
    def h(req: httpx.Request) -> httpx.Response:
        seen.update(dict(req.headers))
        seen["path"] = req.url.path
        seen["query"] = req.url.query.decode()
        return httpx.Response(200, content=json.dumps(
            gemini_generate_obj(model="gemini-2.5-pro")).encode(),
            headers={"Content-Type": "application/json"})
    p = make_provider("google-gemini", transport=httpx.MockTransport(h),
                      api_key="dummy-test-key")
    rh = await p.submit(remote_mod.SubmissionIntent(
        job_id="j", idempotency_key="k", adapter_id="a",
        request={"model_id": "gemini-2.5-pro", "prompt": "hi"}))
    assert seen["path"] == "/v1beta/models/gemini-2.5-pro:generateContent"
    assert seen["x-goog-api-key"] == "dummy-test-key"
    assert "key=" not in seen["query"]            # credential never in URL
    assert "authorization" not in seen
    # generateContent returns no response id — remote_id is honestly
    # None rather than a fabricated handle.
    assert rh.remote_id is None
    assert rh.status == remote_mod.RemoteStatus.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize("key,path,model", [
    ("deepseek", "/chat/completions", "deepseek-chat"),
    ("xai", "/v1/chat/completions", "grok-4")])
async def test_chat_compat_auth_headers_and_path(key, path, model):
    """DeepSeek and xAI both use Bearer auth on their documented
    completions paths — different hosts/paths, same header scheme."""
    seen = {}
    def h(req: httpx.Request) -> httpx.Response:
        seen.update(dict(req.headers))
        seen["path"] = req.url.path
        return httpx.Response(200, content=json.dumps(
            chat_completion_obj(comp_id="c1", model=model)).encode(),
            headers={"Content-Type": "application/json"})
    p = make_provider(key, transport=httpx.MockTransport(h),
                      api_key="dummy-test-key")
    rh = await p.submit(remote_mod.SubmissionIntent(
        job_id="j", idempotency_key="k", adapter_id="a",
        request={"model_id": model, "prompt": "hi"}))
    assert seen["path"] == path
    assert seen["authorization"] == "Bearer dummy-test-key"
    assert "x-goog-api-key" not in seen and "x-api-key" not in seen
    assert rh.remote_id == "c1"
    assert rh.status == remote_mod.RemoteStatus.SUCCEEDED


# ---------- documented lifecycles ----------

def test_openai_background_lifecycle_end_to_end(client):
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    assert r.status_code == 200, r.text
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    detail = _detail(client, job["id"])
    assert detail["remote_job_id"].startswith("resp_")
    assert detail["transport_simulated"] is True
    assert detail["cost_is_provider_billed"] is False
    # usage parsed from the documented response shape
    assert detail["usage"] == {"input_tokens": 17, "output_tokens": 33}
    # gpt-5: 17×1.25 + 33×10 = 351.25 USD/Mtok → $0.00035125 → 352 µUSD
    assert detail["cost_micro"] == 352
    assert detail["cost_basis"] == "usage_x_catalog_rates"
    res = _reservation(client, job["id"])
    assert res.status == "settled" and res.environment == "simulated"
    chg = _charges(client, job["id"])
    assert len(chg) == 1 and chg[0].micro == 352
    assert chg[0].source == "simulated"
    usage = chg[0].usage_json
    assert usage["cost_is_provider_billed"] is False
    assert usage["usage_reported_by"] == "provider_response_simulated"


def test_anthropic_sync_lifecycle_end_to_end(client):
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key="anthropic"))
    assert r.status_code == 200, r.text
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    detail = _detail(client, job["id"])
    assert detail["remote_job_id"].startswith("msg_")
    assert detail["remote_status"] == "succeeded"
    assert detail["usage"] == {"input_tokens": 11, "output_tokens": 7}
    # claude-opus-4-1: 11×15 + 7×75 = 690 USD/Mtok → 690 µUSD
    assert detail["cost_micro"] == 690
    res = _reservation(client, job["id"])
    assert res.status == "settled"
    chg = _charges(client, job["id"])
    assert len(chg) == 1 and chg[0].micro == 690


def test_anthropic_output_text_written(client):
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key="anthropic"))
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "succeeded"
    with _sf(client)() as s:
        assets = s.execute(select(Asset).where(
            Asset.project_id == pid)).scalars().all()
        gen = [a for a in assets
               if a.provenance_json.get("adapter_id") == "cloud.text.rest"]
        assert gen, "expected simulated-transport asset"
        path = client.app.state.storage.asset_path(s, gen[0])
        assert path is not None
        content = path.read_bytes()
    assert b"SIMULATED TRANSPORT OUTPUT" in content
    assert b"simulated transport response" in content


def test_gemini_sync_lifecycle_end_to_end(client):
    """generateContent is synchronous: submit → candidates +
    usageMetadata in the SAME response. No response id exists in the
    documented shape — remote_job_id stays None rather than invented."""
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key="google-gemini"))
    assert r.status_code == 200, r.text
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    detail = _detail(client, job["id"])
    assert detail["remote_job_id"] is None      # honest — none documented
    assert detail["remote_status"] == "succeeded"
    assert detail["lifecycle"] == "sync"
    # usageMetadata.promptTokenCount=19, candidatesTokenCount=31
    assert detail["usage"] == {"input_tokens": 19, "output_tokens": 31}
    # gemini-2.5-pro: 19×1.25 + 31×10 = 333.75 USD/Mtok → ceil 334 µUSD
    assert detail["cost_micro"] == 334
    assert detail["cost_basis"] == "usage_x_catalog_rates"
    assert detail["cost_is_provider_billed"] is False
    res = _reservation(client, job["id"])
    assert res.status == "settled" and res.environment == "simulated"
    chg = _charges(client, job["id"])
    assert len(chg) == 1 and chg[0].micro == 334
    assert chg[0].usage_json["usage_reported_by"] == \
        "provider_response_simulated"


@pytest.mark.parametrize("key,model,rid,micro", [
    # deepseek-chat: 21×0.27 + 29×1.10 = 37.57 USD/Mtok → 38 µUSD
    ("deepseek", "deepseek-chat", "deepseek-sim-0001", 38),
    # grok-4: 21×3.00 + 29×15.00 = 498 USD/Mtok → 498 µUSD
    ("xai", "grok-4", "xai-sim-0001", 498)])
def test_chat_compat_sync_lifecycle_end_to_end(client, key, model,
                                               rid, micro):
    """DeepSeek/xAI synchronous completions: submit returns the
    finished chat.completion — remote id is the response id, not an
    operation handle; no documented retrieval or cancel follows."""
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key=key, model_id=model))
    assert r.status_code == 200, r.text
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    detail = _detail(client, job["id"])
    assert detail["remote_job_id"] == rid       # response id, not a job
    assert detail["remote_status"] == "succeeded"
    assert detail["lifecycle"] == "sync"
    assert detail["usage"] == {"input_tokens": 21, "output_tokens": 29}
    assert detail["cost_micro"] == micro
    assert detail["cost_basis"] == "usage_x_catalog_rates"
    assert detail["cost_is_provider_billed"] is False
    res = _reservation(client, job["id"])
    assert res.status == "settled" and res.environment == "simulated"
    chg = _charges(client, job["id"])
    assert len(chg) == 1 and chg[0].micro == micro
    assert chg[0].source == "simulated"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["anthropic", "google-gemini",
                                 "deepseek", "xai"])
async def test_sync_providers_no_poll_no_cancel(key):
    """None of the synchronous contracts documents single-request
    retrieval or cancellation — query returns None (outcome unknown →
    reconcile) and cancel raises UnsupportedRemoteOp rather than
    inventing an endpoint."""
    p = make_provider(key, transport=simulated_transport(key))
    assert await p.query("any-id") is None
    with pytest.raises(UnsupportedRemoteOp):
        await p.cancel("any-id")


@pytest.mark.asyncio
async def test_openai_poll_and_cancel_shapes():
    p = make_provider("openai", transport=simulated_transport("openai"))
    rh = await p.submit(remote_mod.SubmissionIntent(
        job_id="j", idempotency_key="k", adapter_id="a",
        request={"model_id": "gpt-5", "prompt": "hi"}))
    assert rh.remote_id == "resp_sim_0001"
    assert rh.status == remote_mod.RemoteStatus.SUBMITTED
    h1 = await p.query(rh.remote_id)
    assert h1.status == remote_mod.RemoteStatus.RUNNING
    h2 = await p.query(rh.remote_id)
    assert h2.status == remote_mod.RemoteStatus.SUCCEEDED
    assert h2.detail["usage"]["output_tokens"] == 33
    hc = await p.cancel(rh.remote_id)
    assert hc.status == remote_mod.RemoteStatus.CANCELLED


# ---------- idempotency: declared "none", no auto-resubmit ----------

def test_idempotency_declared_none():
    """No provider here documents server-side idempotency for the
    contract we use — every one declares 'none'; the durable job key
    is the only dedupe and ambiguous outcomes reconcile, never resubmit."""
    for key in REST_PROVIDER_KEYS:
        p = make_provider(key, transport=simulated_transport(key))
        assert p.idempotency == "none"


def test_ambiguous_submit_never_retried(client, monkeypatch):
    """A 500 on submit holds the reservation and the job dies — the
    transport sees exactly ONE POST (no automatic resubmission)."""
    calls = {"n": 0}
    def h(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, content=b'{"error":{"type":"server"}}',
                              headers={"Content-Type": "application/json"})
    _swap(client, monkeypatch, "openai",
          make_provider("openai", transport=httpx.MockTransport(h)))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert calls["n"] == 1
    assert _detail(client, job["id"])["remote_outcome"] == "ambiguous"
    assert _detail(client, job["id"])["reconcile_required"] is True
    res = _reservation(client, job["id"])
    assert res.status == "open"          # exposure held, not released


# ---------- submit error taxonomy ----------

@pytest.mark.parametrize("status,kind", [
    (400, "validation"), (401, "authentication"), (403, "forbidden"),
    (404, "validation"), (422, "validation"), (429, "rate_limited")])
def test_rejected_submit_releases(client, monkeypatch, status, kind):
    """4xx refusal classes → confirmed no-charge → reservation released,
    outcome 'provider_rejected', error_kind recorded."""
    _swap(client, monkeypatch, "openai",
          make_provider("openai",
                        transport=_submit_status_transport("openai", status)))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert _detail(client, job["id"])["remote_outcome"] == "provider_rejected"
    assert _detail(client, job["id"])["error_kind"] == kind
    assert _detail(client, job["id"])["http_status"] == status
    res = _reservation(client, job["id"])
    assert res.status == "released"


@pytest.mark.parametrize("status", [409, 500, 502, 503])
def test_ambiguous_submit_holds(client, monkeypatch, status):
    """409/5xx → outcome unknown → reservation held for reconciliation."""
    _swap(client, monkeypatch, "openai",
          make_provider("openai",
                        transport=_submit_status_transport("openai", status)))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert _detail(client, job["id"])["remote_outcome"] == "ambiguous"
    res = _reservation(client, job["id"])
    assert res.status == "open"


def test_timeout_submit_holds(client, monkeypatch):
    def h(req):
        raise httpx.ConnectTimeout("simulated timeout")
    _swap(client, monkeypatch, "openai",
          make_provider("openai", transport=httpx.MockTransport(h)))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert _detail(client, job["id"])["remote_outcome"] == "ambiguous"
    assert _detail(client, job["id"])["error_kind"] == "transport"
    res = _reservation(client, job["id"])
    assert res.status == "open"


def test_malformed_200_holds(client, monkeypatch):
    def h(req):
        return httpx.Response(200, content=b"<html>oops</html>")
    _swap(client, monkeypatch, "openai",
          make_provider("openai", transport=httpx.MockTransport(h)))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert _detail(client, job["id"])["remote_outcome"] == "ambiguous"
    assert _detail(client, job["id"])["error_kind"] == "malformed"
    res = _reservation(client, job["id"])
    assert res.status == "open"


def test_anthropic_error_taxonomy(client, monkeypatch):
    """Same taxonomy on the Messages shape — 401 releases, 500 holds."""
    _swap(client, monkeypatch, "anthropic",
          make_provider("anthropic",
                        transport=_submit_status_transport(
                            "anthropic", 401,
                            {"type": "error",
                             "error": {"type": "authentication_error",
                                       "message": "bad key"}})))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key="anthropic"))
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert _detail(client, job["id"])["error_kind"] == "authentication"
    assert _reservation(client, job["id"]).status == "released"


# ---------- poll / cancel edge cases ----------

def test_poll_404_holds(client, monkeypatch):
    """Retrieval 404 after a confirmed submit: the remote may exist but
    is unreachable — ambiguous, exposure held. A poll error must NEVER
    be treated as evidence that no remote work happened."""
    _swap(client, monkeypatch, "openai",
          make_provider("openai", transport=simulated_transport(
              "openai", {"retrieve": _status_handler(404)})))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert _detail(client, job["id"])["remote_outcome"] == "ambiguous"
    assert _reservation(client, job["id"]).status == "open"


@pytest.mark.parametrize("mode", ["status500", "timeout"])
def test_poll_error_holds(client, monkeypatch, mode):
    """Any retrieval failure — 5xx or transport — is 'unreachable',
    never 'confirmed gone': the reservation is held either way."""
    if mode == "timeout":
        def retrieve(req):
            raise httpx.ConnectTimeout("simulated")
    else:
        retrieve = _status_handler(500)
    _swap(client, monkeypatch, "openai",
          make_provider("openai", transport=simulated_transport(
              "openai", {"retrieve": retrieve})))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert _detail(client, job["id"])["remote_outcome"] == "ambiguous"
    assert _reservation(client, job["id"]).status == "open"


def test_submit_unknown_status_holds(client, monkeypatch):
    """A 2xx whose status field isn't a documented value is malformed —
    the request may have been processed → hold, never release."""
    def bogus(req):
        return httpx.Response(200, content=json.dumps(
            {"id": "resp_x", "status": "bogus_state"}).encode(),
            headers={"Content-Type": "application/json"})
    _swap(client, monkeypatch, "openai",
          make_provider("openai", transport=simulated_transport(
              "openai", {"submit": bogus})))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert _detail(client, job["id"])["remote_outcome"] == "ambiguous"
    assert _reservation(client, job["id"]).status == "open"


def test_submit_missing_remote_id_holds(client, monkeypatch):
    """An async submit without a remote operation id is ambiguous —
    no fabricated id is ever polled."""
    def noid(req):
        return httpx.Response(200, content=json.dumps(
            {"status": "queued"}).encode(),
            headers={"Content-Type": "application/json"})
    _swap(client, monkeypatch, "openai",
          make_provider("openai", transport=simulated_transport(
              "openai", {"submit": noid})))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert _detail(client, job["id"])["remote_outcome"] == "ambiguous"
    assert _reservation(client, job["id"]).status == "open"


def test_remote_failed_status_holds(client, monkeypatch):
    """Provider reports status=failed — billing unknown → hold."""
    def retrieve(req):
        rid = req.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=json.dumps(openai_response_obj(
            resp_id=rid, status="failed",
            error={"type": "server_error", "code": "x"})).encode(),
            headers={"Content-Type": "application/json"})
    _swap(client, monkeypatch, "openai",
          make_provider("openai", transport=simulated_transport(
              "openai", {"retrieve": retrieve})))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert _detail(client, job["id"])["remote_outcome"] == "provider_failed"
    assert _reservation(client, job["id"]).status == "open"


def test_user_cancel_releases_via_documented_cancel(client, monkeypatch):
    """Remote stays in_progress; user cancels → documented cancel →
    cancelled w/o usage → reservation released."""
    def running(req):
        rid = req.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=json.dumps(openai_response_obj(
            resp_id=rid, status="in_progress")).encode(),
            headers={"Content-Type": "application/json"})
    _swap(client, monkeypatch, "openai",
          make_provider("openai", transport=simulated_transport(
              "openai", {"retrieve": running})))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job_id = r.json()["job_id"]
    import time as _t
    for _ in range(40):
        j = next((x for x in client.get(
            f"/api/projects/{pid}/jobs").json() if x["id"] == job_id), {})
        if j.get("status") == "running":
            break
        _t.sleep(0.05)
    client.post(f"/api/jobs/{job_id}/cancel")
    job = wait_for_job(client, pid, job_id)
    assert job["status"] == "cancelled"
    res = _reservation(client, job_id)
    assert res.status == "released"


def test_cancel_with_usage_settles_partial(client, monkeypatch):
    """Documented cancel returning partial usage → settle that usage
    (locally calculated, still source=simulated)."""
    def running(req):
        rid = req.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=json.dumps(openai_response_obj(
            resp_id=rid, status="in_progress")).encode(),
            headers={"Content-Type": "application/json"})
    def cancel_used(req):
        rid = req.url.path.split("/")[-2]
        return httpx.Response(200, content=json.dumps(openai_response_obj(
            resp_id=rid, status="cancelled",
            usage={"input_tokens": 100, "output_tokens": 50})).encode(),
            headers={"Content-Type": "application/json"})
    _swap(client, monkeypatch, "openai",
          make_provider("openai", transport=simulated_transport(
              "openai", {"retrieve": running, "cancel": cancel_used})))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job_id = r.json()["job_id"]
    import time as _t
    for _ in range(40):
        j = next((x for x in client.get(
            f"/api/projects/{pid}/jobs").json() if x["id"] == job_id), {})
        if j.get("status") == "running":
            break
        _t.sleep(0.05)
    client.post(f"/api/jobs/{job_id}/cancel")
    job = wait_for_job(client, pid, job_id)
    assert job["status"] == "cancelled"
    res = _reservation(client, job_id)
    # 100×1.25 + 50×10 = 625 USD/Mtok → 625 µUSD
    assert res.status == "settled"
    chg = _charges(client, job_id)
    assert len(chg) == 1 and chg[0].micro == 625


@pytest.mark.parametrize("cancel_status", [404, 500])
def test_cancel_error_holds(client, monkeypatch, cancel_status):
    """A failed cancel — even 404 — must not establish that the remote
    operation didn't run or won't bill: outcome stays ambiguous, held."""
    def running(req):
        rid = req.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=json.dumps(openai_response_obj(
            resp_id=rid, status="in_progress")).encode(),
            headers={"Content-Type": "application/json"})
    _swap(client, monkeypatch, "openai",
          make_provider("openai", transport=simulated_transport(
              "openai", {"retrieve": running,
                         "cancel": _status_handler(cancel_status)})))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job_id = r.json()["job_id"]
    import time as _t
    for _ in range(40):
        j = next((x for x in client.get(
            f"/api/projects/{pid}/jobs").json() if x["id"] == job_id), {})
        if j.get("status") == "running":
            break
        _t.sleep(0.05)
    client.post(f"/api/jobs/{job_id}/cancel")
    job = wait_for_job(client, pid, job_id)
    assert job["status"] == "cancelled"
    assert _detail(client, job_id)["remote_outcome"] == "ambiguous"
    assert _reservation(client, job_id).status == "open"


def test_cancel_reports_completed_holds(client, monkeypatch):
    """Cancel answered 'completed' — the operation ran to term; billing
    outcome is unknown, never auto-released (hold for reconcile)."""
    def running(req):
        rid = req.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=json.dumps(openai_response_obj(
            resp_id=rid, status="in_progress")).encode(),
            headers={"Content-Type": "application/json"})
    def cancel_completed(req):
        rid = req.url.path.split("/")[-2]
        return httpx.Response(200, content=json.dumps(openai_response_obj(
            resp_id=rid, status="completed")).encode(),
            headers={"Content-Type": "application/json"})
    _swap(client, monkeypatch, "openai",
          make_provider("openai", transport=simulated_transport(
              "openai", {"retrieve": running,
                         "cancel": cancel_completed})))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job_id = r.json()["job_id"]
    import time as _t
    for _ in range(40):
        j = next((x for x in client.get(
            f"/api/projects/{pid}/jobs").json() if x["id"] == job_id), {})
        if j.get("status") == "running":
            break
        _t.sleep(0.05)
    client.post(f"/api/jobs/{job_id}/cancel")
    job = wait_for_job(client, pid, job_id)
    assert job["status"] == "cancelled"
    assert _detail(client, job_id)["remote_outcome"] == "ambiguous"
    assert _reservation(client, job_id).status == "open"


def test_cancel_unconfirmed_holds(client, monkeypatch):
    """Cancel endpoint errors → outcome stays ambiguous, held."""
    def running(req):
        rid = req.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=json.dumps(openai_response_obj(
            resp_id=rid, status="in_progress")).encode(),
            headers={"Content-Type": "application/json"})
    _swap(client, monkeypatch, "openai",
          make_provider("openai", transport=simulated_transport(
              "openai", {"retrieve": running,
                         "cancel": _status_handler(503)})))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job_id = r.json()["job_id"]
    import time as _t
    for _ in range(40):
        j = next((x for x in client.get(
            f"/api/projects/{pid}/jobs").json() if x["id"] == job_id), {})
        if j.get("status") == "running":
            break
        _t.sleep(0.05)
    client.post(f"/api/jobs/{job_id}/cancel")
    job = wait_for_job(client, pid, job_id)
    assert job["status"] == "cancelled"
    assert _detail(client, job["id"])["remote_outcome"] == "ambiguous"
    assert _reservation(client, job_id).status == "open"


@pytest.mark.asyncio
async def test_openai_submit_body_is_background():
    """Poll/cancel are documented ONLY for background responses — the
    submit body must carry background:true so every subsequent GET/cancel
    targets a documented lifecycle."""
    seen = {}
    def h(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=json.dumps(
            openai_response_obj(resp_id="r1", status="queued")).encode(),
            headers={"Content-Type": "application/json"})
    p = make_provider("openai", transport=httpx.MockTransport(h))
    await p.submit(remote_mod.SubmissionIntent(
        job_id="j", idempotency_key="k", adapter_id="a",
        request={"model_id": "gpt-5", "prompt": "hi"}))
    assert seen["body"]["background"] is True
    assert seen["body"]["model"] == "gpt-5"
    assert seen["body"]["input"] == "hi"


@pytest.mark.asyncio
async def test_anthropic_submit_body_shape():
    """Messages contract: {model, max_tokens, messages:[user]} — no
    background flag, no invented async params."""
    seen = {}
    def h(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=json.dumps(
            anthropic_message_obj(msg_id="m1")).encode(),
            headers={"Content-Type": "application/json"})
    p = make_provider("anthropic", transport=httpx.MockTransport(h))
    await p.submit(remote_mod.SubmissionIntent(
        job_id="j", idempotency_key="k", adapter_id="a",
        request={"model_id": "claude-sonnet-4-5", "prompt": "hi"}))
    body = seen["body"]
    assert body["model"] == "claude-sonnet-4-5"
    assert body["max_tokens"] == 1024
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert "background" not in body


@pytest.mark.asyncio
async def test_gemini_submit_body_shape():
    """generateContent contract: {contents:[{role,parts:[{text}]}],
    generationConfig:{maxOutputTokens}} — the model is NOT in the body
    (it travels in the URL) and no OpenAI-shaped fields leak in."""
    seen = {}
    def h(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=json.dumps(
            gemini_generate_obj(model="m")).encode(),
            headers={"Content-Type": "application/json"})
    p = make_provider("google-gemini", transport=httpx.MockTransport(h))
    await p.submit(remote_mod.SubmissionIntent(
        job_id="j", idempotency_key="k", adapter_id="a",
        request={"model_id": "gemini-2.5-flash", "prompt": "hi",
                 "max_output_tokens": 256}))
    body = seen["body"]
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "hi"}]}]
    assert body["generationConfig"] == {"maxOutputTokens": 256}
    assert "model" not in body and "messages" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("key,model", [("deepseek", "deepseek-reasoner"),
                                       ("xai", "grok-4")])
async def test_chat_compat_submit_body_shape(key, model):
    """Documented chat-completions body: {model, messages, max_tokens,
    stream:false} — explicitly non-streaming so the response is the
    full completion object."""
    seen = {}
    def h(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=json.dumps(
            chat_completion_obj(comp_id="c1", model=model)).encode(),
            headers={"Content-Type": "application/json"})
    p = make_provider(key, transport=httpx.MockTransport(h))
    await p.submit(remote_mod.SubmissionIntent(
        job_id="j", idempotency_key="k", adapter_id="a",
        request={"model_id": model, "prompt": "hi",
                 "max_output_tokens": 512}))
    body = seen["body"]
    assert body["model"] == model
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["max_tokens"] == 512
    assert body["stream"] is False
    assert "background" not in body and "input" not in body


# ---------- new-provider error taxonomy ----------

@pytest.mark.parametrize("key", ["google-gemini", "deepseek", "xai"])
def test_new_provider_rejected_submit_releases(client, monkeypatch, key):
    """A 4xx refusal on any new provider is confirmed pre-processing —
    reservation released, outcome provider_rejected."""
    _swap(client, monkeypatch, key,
          make_provider(key, transport=_submit_status_transport(key, 401)))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key=key))
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert _detail(client, job["id"])["remote_outcome"] == "provider_rejected"
    assert _detail(client, job["id"])["error_kind"] == "authentication"
    assert _reservation(client, job["id"]).status == "released"


@pytest.mark.parametrize("key,status", [
    ("google-gemini", 500), ("deepseek", 503), ("xai", 500),
    ("google-gemini", 429)])
def test_new_provider_ambiguous_submit_holds(client, monkeypatch,
                                             key, status):
    """5xx (and 429→release only via documented none_confirmed — here
    429 is a refusal so it releases; keep the two hold cases honest) —
    server-error submissions hold exposure for reconciliation."""
    _swap(client, monkeypatch, key,
          make_provider(key, transport=_submit_status_transport(key, status)))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key=key))
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    detail = _detail(client, job["id"])
    if status >= 500:
        assert detail["remote_outcome"] == "ambiguous"
        assert _reservation(client, job["id"]).status == "open"
    else:
        assert detail["remote_outcome"] == "provider_rejected"
        assert _reservation(client, job["id"]).status == "released"


def test_gemini_timeout_submit_holds(client, monkeypatch):
    def h(req):
        raise httpx.ConnectTimeout("simulated timeout")
    _swap(client, monkeypatch, "google-gemini",
          make_provider("google-gemini",
                        transport=httpx.MockTransport(h)))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key="google-gemini"))
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert _detail(client, job["id"])["remote_outcome"] == "ambiguous"
    assert _detail(client, job["id"])["error_kind"] == "transport"
    assert _reservation(client, job["id"]).status == "open"


def test_gemini_error_object_holds(client, monkeypatch):
    """A 200 carrying a documented Gemini error object means the
    outcome is unverifiable → provider_failed hold, never release."""
    def h(req):
        return httpx.Response(200, content=json.dumps(
            {"error": {"code": 500, "status": "INTERNAL",
                       "message": "backend error"}}).encode(),
            headers={"Content-Type": "application/json"})
    _swap(client, monkeypatch, "google-gemini",
          make_provider("google-gemini",
                        transport=httpx.MockTransport(h)))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key="google-gemini"))
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert _detail(client, job["id"])["remote_outcome"] == "ambiguous"
    assert _reservation(client, job["id"]).status == "open"


def test_gemini_no_candidates_malformed_holds(client, monkeypatch):
    """A 200 with no candidates is unparseable — the request may have
    been processed → malformed, hold."""
    def h(req):
        return httpx.Response(200, content=json.dumps(
            {"modelVersion": "m"}).encode(),
            headers={"Content-Type": "application/json"})
    _swap(client, monkeypatch, "google-gemini",
          make_provider("google-gemini",
                        transport=httpx.MockTransport(h)))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key="google-gemini"))
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert _detail(client, job["id"])["remote_outcome"] == "ambiguous"
    assert _detail(client, job["id"])["error_kind"] == "malformed"
    assert _reservation(client, job["id"]).status == "open"


@pytest.mark.parametrize("key", ["deepseek", "xai"])
def test_chat_compat_no_choices_malformed_holds(client, monkeypatch, key):
    def h(req):
        return httpx.Response(200, content=json.dumps(
            {"id": "c1", "choices": []}).encode(),
            headers={"Content-Type": "application/json"})
    _swap(client, monkeypatch, key,
          make_provider(key, transport=httpx.MockTransport(h)))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key=key))
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert _detail(client, job["id"])["remote_outcome"] == "ambiguous"
    assert _detail(client, job["id"])["error_kind"] == "malformed"
    assert _reservation(client, job["id"]).status == "open"


@pytest.mark.parametrize("key", ["deepseek", "xai"])
def test_chat_compat_error_object_holds(client, monkeypatch, key):
    """A 200 whose body carries a documented error object instead of
    choices — the request may have been processed → hold, never
    release on the strength of the HTTP status alone."""
    def h(req):
        return httpx.Response(200, content=json.dumps(
            {"error": {"message": "backend failure",
                       "type": "server_error", "code": "500"}}).encode(),
            headers={"Content-Type": "application/json"})
    _swap(client, monkeypatch, key,
          make_provider(key, transport=httpx.MockTransport(h)))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key=key))
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert _detail(client, job["id"])["remote_outcome"] == "ambiguous"
    assert _reservation(client, job["id"]).status == "open"


@pytest.mark.parametrize("key", ["google-gemini", "deepseek", "xai"])
def test_new_provider_single_submit_no_retry(client, monkeypatch, key):
    """Ambiguous submit on a new provider hits the wire exactly once —
    idempotency is 'none' so there is no safe automatic resubmit."""
    calls = {"n": 0}
    def h(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, content=b'{"error":{"status":"INTERNAL"}}',
                              headers={"Content-Type": "application/json"})
    _swap(client, monkeypatch, key,
          make_provider(key, transport=httpx.MockTransport(h)))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key=key))
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "failed"
    assert calls["n"] == 1                     # never resubmitted
    assert _detail(client, job["id"])["remote_outcome"] == "ambiguous"
    assert _reservation(client, job["id"]).status == "open"


@pytest.mark.parametrize("key", ["google-gemini", "deepseek", "xai"])
def test_sync_success_single_http_request(client, monkeypatch, key):
    """A successful sync lifecycle issues EXACTLY ONE HTTP request —
    no hidden retrieval/polling call follows the submit, because no
    such endpoint is documented for these providers."""
    calls = []
    base = simulated_handler(key)
    def h(req: httpx.Request) -> httpx.Response:
        calls.append(f"{req.method} {req.url.path}")
        return base(req)
    _swap(client, monkeypatch, key,
          make_provider(key, transport=httpx.MockTransport(h)))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key=key))
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    assert len(calls) == 1, calls
    assert calls[0].startswith("POST ")


# ---------- catalog / model eligibility ----------

@pytest.mark.parametrize("key,model", [
    ("google-gemini", "gpt-5"),           # model belongs to openai
    ("deepseek", "grok-4"),               # model belongs to xai
    ("xai", "deepseek-chat"),             # model belongs to deepseek
    ("google-gemini", "not-a-model")])
def test_rest_provider_model_fails_closed(client, key, model):
    """A model that isn't a text-capable catalog model OF THE CHOSEN
    provider is refused at validation — nothing is admitted: no job
    row, no reservation, no HTTP call."""
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key=key, model_id=model))
    assert r.status_code == 422
    with _sf(client)() as s:
        from app.db.models import GenerationJob
        assert s.execute(select(GenerationJob).where(
            GenerationJob.project_id == pid)).scalars().all() == []
        assert s.execute(select(SpendReservation).where(
            SpendReservation.project_id == pid)).scalars().all() == []


def test_ineligible_provider_fails_closed(client):
    """No documented T1 official-API text strategy (sudowrite: T2/T3
    assisted only) → refused before admission — provider integration
    does not widen eligibility."""
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key="sudowrite"))
    assert r.status_code == 422
    assert "eligible" in r.text
    with _sf(client)() as s:
        from app.db.models import GenerationJob
        assert s.execute(select(GenerationJob).where(
            GenerationJob.project_id == pid)).scalars().all() == []
        assert s.execute(select(SpendReservation).where(
            SpendReservation.project_id == pid)).scalars().all() == []


def test_rest_contract_does_not_widen_model_surface(client):
    """REST routing is keyed to the provider, not the model list —
    a catalog provider WITHOUT a REST contract (rytr) still resolves
    through its own models; a model id from another provider never
    piggybacks on the integration."""
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key="rytr",
                                    model_id="gemini-2.5-pro"))
    assert r.status_code == 422
    r = _commit(client, pid, _payload(provider_key="rytr",
                                    model_id="rytr-default"))
    assert r.status_code == 200, r.text


def test_new_provider_provenance_stays_simulated(client):
    """A Gemini-shaped response with usage + modelVersion can never
    promote provenance — no verified_remote anywhere."""
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key="google-gemini"))
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "succeeded"
    with _sf(client)() as s:
        assets = s.execute(select(Asset).where(
            Asset.project_id == pid)).scalars().all()
    gen = [a for a in assets
           if a.provenance_json.get("adapter_id") == "cloud.text.rest"]
    assert gen
    prov = gen[0].provenance_json
    assert prov["provenance_confidence"] == "simulated"
    assert prov["remote_id_synthetic"] is True
    assert "verified_remote" not in str(prov)


def test_new_provider_credentials_never_logged(client, monkeypatch):
    key = "x-goog-sim-AAAA-secret"
    _swap(client, monkeypatch, "google-gemini",
          make_provider("google-gemini",
                        transport=simulated_transport("google-gemini"),
                        api_key=key))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload(provider_key="google-gemini"))
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "succeeded"
    blob = json.dumps(job, default=str)
    assert key not in blob
    with _sf(client)() as s:
        audits = s.execute(select(AuditEvent).where(
            AuditEvent.project_id == pid)).scalars().all()
    audit_blob = json.dumps([a.payload_json for a in audits], default=str)
    assert key not in audit_blob
    assert "x-goog-api-key" not in audit_blob


# ---------- synchronous crash window ----------

@pytest.mark.parametrize("key,remote_id", [
    ("anthropic", "msg_sim_0001"),
    ("google-gemini", None),          # generateContent returns no id
    ("deepseek", "deepseek-sim-0001"),
    ("xai", "xai-sim-0001")])
def test_sync_crash_window_reservation_held(client, key, remote_id):
    """Sync providers return success in the submit response — if the
    app crashes before result+settlement are durable, startup recovery
    must mark needs_reconciliation, KEEP the reservation open, and
    never resubmit (idempotency 'none' → no safe retry)."""
    from app.db.models import GenerationJob
    from app.domain import spend as _spend
    pid = make_project(client)["id"]
    job_id = f"crash-sync-{key}"
    with _sf(client)() as s:
        # Mid-flight snapshot: submit succeeded (last persisted progress
        # carries the response id where one exists), settlement never
        # reached.
        job = GenerationJob(
            id=job_id, project_id=pid, adapter_id="cloud.text.rest",
            kind="text.generate", status="running",
            request_json={"provider_key": key,
                          "command_type": "generate.text.cloud"},
            idempotency_key=f"crash-sync-key-{key}",
            progress_json={
                "remote_intent": {"provider_key": key,
                                  "idempotency_key": f"crash-sync-key-{key}",
                                  "lifecycle": "sync", "simulated": True},
                "remote_job_id": remote_id,
                "remote_status": "succeeded"},
            execution_scope="remote", resumable=False)
        s.add(job)
        res = _spend.admit(s, job_id=job_id, command_id=None,
                           project_id=pid, provider_key=key,
                           environment="simulated",
                           est_usd=0.001, max_usd=0.001)
        res_id = res.id
        s.commit()
        n = client.app.state.job_engine.reconcile_interrupted(s)
        s.flush()
        s.refresh(job)
        assert job.recovery_state == "needs_reconciliation"
        assert job.status == "failed"      # terminal, ambiguous — never re-queued
        s.commit()
    assert n == 0                           # nothing resubmitted
    res = _reservation(client, job_id)
    assert res.id == res_id and res.status == "open"   # exposure held
    # The held reservation stays open until explicit reconciliation.
    r = _commit(client, pid, {"job_id": job_id,
                              "outcome": "confirmed_charge",
                              "actual_usd": 0.001,
                              "accept_overage": True}, "c2crash",
                ctype="spend.reconcile")
    assert r.status_code == 200
    assert _reservation(client, job_id).status == "settled"


# ---------- usage→cost precision ----------

def test_usage_cost_micro_exact():
    from app.adapters.cloud_rest import _usage_cost_micro
    # 12345 in @ $3.00/Mtok + 678 out @ $15.00/Mtok
    # = 37035 + 10170 = 47205 USD-µ → exactly 47205 µUSD
    micro, basis = _usage_cost_micro(
        {"input_tokens": 12345, "output_tokens": 678},
        {"input_per_mtok": 3.0, "output_per_mtok": 15.0})
    assert micro == 47205 and basis == "usage_x_catalog_rates"
    # fractional cent rounds UP, never down
    micro, _ = _usage_cost_micro(
        {"input_tokens": 1, "output_tokens": 0},
        {"input_per_mtok": 0.000001, "output_per_mtok": 0})
    assert micro == 1
    # no usage or no rates → no micro (falls back to est at settle)
    micro, basis = _usage_cost_micro(None, {"input_per_mtok": 1})
    assert micro is None and basis == "no_usage_or_rates"


# ---------- ledger / reconciliation ----------

def test_hold_then_reconcile(client, monkeypatch):
    """Ambiguous submit → hold → spend.reconcile releases explicitly."""
    _swap(client, monkeypatch, "openai",
          make_provider("openai",
                        transport=_submit_status_transport("openai", 500)))
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job = wait_for_job(client, pid, r.json()["job_id"])
    res = _reservation(client, job["id"])
    assert res.status == "open"
    # still_unknown keeps the hold
    r1 = _commit(client, pid, {"job_id": job["id"],
                               "outcome": "still_unknown"}, "c2recon",
                 ctype="spend.reconcile")
    assert r1.status_code == 200
    assert _reservation(client, job["id"]).status == "open"
    # confirmed_no_charge releases
    r2 = _commit(client, pid, {"job_id": job["id"],
                               "outcome": "confirmed_no_charge"}, "c2recon",
                 ctype="spend.reconcile")
    assert r2.status_code == 200
    assert _reservation(client, job["id"]).status == "released"


# ---------- provenance / egress / secrecy ----------

def test_no_verified_remote_promotion(client):
    """A mocked resp_* id + x-request-id can never promote provenance."""
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "succeeded"
    with _sf(client)() as s:
        assets = s.execute(select(Asset).where(
            Asset.project_id == pid)).scalars().all()
    gen = [a for a in assets
           if a.provenance_json.get("adapter_id") == "cloud.text.rest"]
    assert gen, "expected simulated-transport asset"
    prov = gen[0].provenance_json
    assert prov["remote_job_id"].startswith("resp_")
    assert prov["remote_id_synthetic"] is True
    assert prov["provenance_confidence"] == "simulated"
    assert "verified_remote" not in str(prov)


def test_credentials_and_prompt_never_logged(client, monkeypatch):
    key = "sk-test-AAAA-BBBB-CCCC-secret"
    _swap(client, monkeypatch, "openai",
          make_provider("openai",
                        transport=simulated_transport("openai"),
                        api_key=key))
    pid = make_project(client)["id"]
    secret_prompt = "PROMPT-MARKER-xyzzy-42"
    r = _commit(client, pid, _payload(prompt=secret_prompt))
    job = wait_for_job(client, pid, r.json()["job_id"])
    blob = json.dumps(job, default=str)
    assert key not in blob
    with _sf(client)() as s:
        audits = s.execute(select(AuditEvent).where(
            AuditEvent.project_id == pid)).scalars().all()
    audit_blob = json.dumps(
        [a.payload_json for a in audits], default=str)
    assert key not in audit_blob
    assert secret_prompt not in audit_blob   # audit stays minimized
    assert "authorization" not in audit_blob.lower()


def test_request_id_recorded_not_verified(client):
    pid = make_project(client)["id"]
    r = _commit(client, pid, _payload())
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "succeeded"
    assert _detail(client, job["id"])["request_id"] == "req_simulated"
    assert _detail(client, job["id"])["transport_simulated"] is True


def test_preview_warns_simulated_transport(client):
    pid = make_project(client)["id"]
    r = client.post(f"/api/projects/{pid}/commands/preview",
                    json={"command_type": "generate.text.cloud",
                          "payload": _payload(), "scope": "project"})
    warns = " ".join(r.json().get("warnings", [])).lower()
    assert "simulated" in warns and "verified_remote" in warns
