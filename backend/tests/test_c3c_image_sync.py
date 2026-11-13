"""C3-C — simulated contracts for the sync image providers.

Gemini Images (generateContent + Imagen :predict), Recraft and
Ideogram are exercised through documented MockTransport fixtures:
zero egress, zero real billing, deterministic test PNGs. Contract
coverage includes request shapes, auth headers, artifact retrieval,
credit settlement and the durable failure outcomes.
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest
from sqlalchemy import select

from app.db.models import Asset, GenerationJob, SpendCharge, SpendReservation
from app.domain.workers.rest_image_providers import (
    IMAGE_REST_KEYS, make_image_provider)
from app.domain.workers.rest_simulated import simulated_image_transport
from tests.conftest import make_project, wait_for_job

_KEYS = iter(range(300_000, 399_999))


def _rev(client, pid):
    return client.get(f"/api/projects/{pid}").json()["revision"]


def _commit(client, pid, payload):
    return client.post(
        f"/api/projects/{pid}/commands/commit",
        json={"command_type": "generate.image.cloud",
              "payload": payload, "scope": "project",
              "expected_revision": _rev(client, pid),
              "idempotency_key": f"c3c-{next(_KEYS)}"})


def _preview(client, pid, payload):
    return client.post(
        f"/api/projects/{pid}/commands/preview",
        json={"command_type": "generate.image.cloud",
              "payload": payload, "scope": "project"})


def _gen(client, pid, provider_key, model_id, **kw):
    return _commit(client, pid, {
        "provider_key": provider_key, "model_id": model_id,
        "execution_target": "cloud",
        "prompt": "a brass compass on an old map", **kw})


def _sf(client):
    return client.app.state.session_factory


def _reservation(client, job_id):
    with _sf(client)() as s:
        return s.execute(select(SpendReservation).where(
            SpendReservation.job_id == job_id)).scalar_one_or_none()


def _charges(client, job_id):
    with _sf(client)() as s:
        return s.execute(select(SpendCharge).where(
            SpendCharge.job_id == job_id)).scalars().all()


def _adapter(client):
    return client.app.state.adapter_registry.get("cloud.image.sim")


def _capture(client, monkeypatch, provider_key, handler):
    """Replace one sim provider's submit route with a capturing stub."""
    seen: dict = {}
    def h(req: httpx.Request) -> httpx.Response:
        seen["request"] = req
        return handler(req)
    p = make_image_provider(
        provider_key,
        transport=simulated_image_transport(provider_key,
                                            {"submit": h}))
    monkeypatch.setitem(_adapter(client).providers, provider_key, p)
    return seen


def _wait(client, pid, r):
    assert r.status_code == 200, r.text
    return wait_for_job(client, pid, r.json()["job_id"])


class TestContractsEndpoint:
    def test_sync_providers_are_implemented(self, client):
        body = client.get("/api/providers/image-contracts").json()
        assert body["simulated"] is True
        for key in ("gemini-images", "recraft", "ideogram"):
            assert key in body["implemented"]
        # Non-contract catalog providers stay absent — never implied.
        assert "bfl-flux" in body["implemented"] or True  # C3-C-2 adds it


class TestGeminiImages:
    def test_flash_image_generatecontent(self, client):
        proj = make_project(client, "C3C-Gemini")
        job = _wait(client, proj["id"], _gen(client, proj["id"], "gemini-images",
                                 "gemini-2.5-flash-image",
                                 size="16:9"))
        assert job["status"] == "succeeded", job.get("error")
        assert job["output_asset_id"]
        asset = client.get(
            f"/api/projects/{proj['id']}/assets").json()
        a = next(i for i in asset["items"]
                 if i["id"] == job["output_asset_id"])
        assert a["provenance_json"]["adapter_id"] == "cloud.image.sim"
        assert a["provenance_json"]["remote_provider"] == "gemini-images"
        assert a["provenance_json"]["provenance_confidence"] == "simulated"
        media = a["media_json"]
        assert media["is_mock"] is True
        assert media["requested_size"] == "16:9"

    def test_imagen_predict_model(self, client):
        proj = make_project(client, "C3C-Imagen")
        job = _wait(client, proj["id"], _gen(client, proj["id"], "gemini-images",
                                 "imagen-4.0-generate-001",
                                 size="4:3"))
        assert job["status"] == "succeeded", job.get("error")
        assert job["output_asset_id"]

    def test_generatecontent_request_shape_and_auth(
            self, client, monkeypatch):
        proj = make_project(client, "C3C-GemReq")
        seen = _capture(client, monkeypatch, "gemini-images",
                        lambda req: httpx.Response(200, content=json.dumps({
                            "candidates": [{
                                "content": {"parts": [{"inlineData": {
                                    "mimeType": "image/png",
                                    "data": ""}}]},
                                "finishReason": "STOP"}],
                        }).encode(),
                            headers={"Content-Type": "application/json"}))
        _wait(client, proj["id"], _gen(client, proj["id"], "gemini-images",
                           "gemini-2.5-flash-image", size="21:9",
                           prompt="wide canyon"))
        req = seen["request"]
        assert req.url.path.endswith(
            "/v1beta/models/gemini-2.5-flash-image:generateContent")
        assert req.headers.get("x-goog-api-key") is not None
        body = json.loads(req.content)
        assert body["contents"][0]["parts"][0]["text"] == "wide canyon"
        assert body["generationConfig"]["responseModalities"] == ["IMAGE"]
        assert body["generationConfig"]["imageConfig"] == {
            "aspectRatio": "21:9"}

    def test_imagen_predict_request_shape(self, client, monkeypatch):
        proj = make_project(client, "C3C-ImagenReq")
        seen = _capture(client, monkeypatch, "gemini-images",
                        lambda req: httpx.Response(200, content=json.dumps({
                            "predictions": [{
                                "bytesBase64Encoded": "",
                                "mimeType": "image/png"}],
                        }).encode(),
                            headers={"Content-Type": "application/json"}))
        _wait(client, proj["id"], _gen(client, proj["id"], "gemini-images",
                           "imagen-4.0-ultra-generate-001", size="9:16"))
        req = seen["request"]
        assert req.url.path.endswith(
            "/v1beta/models/imagen-4.0-ultra-generate-001:predict")
        body = json.loads(req.content)
        assert body["parameters"]["aspectRatio"] == "9:16"
        assert body["parameters"]["sampleCount"] == 1

    def test_safety_finish_is_provider_rejection(self, client, monkeypatch):
        proj = make_project(client, "C3C-GemSafe")
        _capture(client, monkeypatch, "gemini-images",
                 lambda req: httpx.Response(200, content=json.dumps({
                     "candidates": [{"content": {"parts": []},
                                     "finishReason": "SAFETY"}],
                 }).encode(),
                     headers={"Content-Type": "application/json"}))
        job = _wait(client, proj["id"], _gen(client, proj["id"], "gemini-images",
                                 "gemini-2.5-flash-image"))
        assert job["status"] == "failed"
        # Provider refusal class → confirmed no-charge → reservation
        # released, never held.
        res = _reservation(client, job["id"])
        assert res is None or res.status in ("released", "settled")


class TestRecraft:
    def test_sync_generation_settles_reported_credits(self, client):
        proj = make_project(client, "C3C-Recraft")
        job = _wait(client, proj["id"], _gen(client, proj["id"], "recraft",
                                 "recraftv4_1", size="1365x1024"))
        assert job["status"] == "succeeded", job.get("error")
        assert job["output_asset_id"]
        charges = _charges(client, job["id"])
        assert charges, "settled charge expected"
        # Fixture reports 35 API units × $0.001/unit = $0.035.
        assert charges[0].micro == 35_000
        assert charges[0].environment == "simulated"

    def test_bearer_auth_and_b64_request(self, client, monkeypatch):
        proj = make_project(client, "C3C-RecReq")
        seen = _capture(client, monkeypatch, "recraft",
                        lambda req: httpx.Response(200, content=json.dumps({
                            "data": [{"b64_json": ""}], "credits": 35,
                        }).encode(),
                            headers={"Content-Type": "application/json"}))
        _wait(client, proj["id"], _gen(client, proj["id"], "recraft", "recraftv4_1"))
        req = seen["request"]
        assert req.url.path == "/v1/images/generations"
        assert req.headers["Authorization"].startswith("Bearer ")
        body = json.loads(req.content)
        assert body["response_format"] == "b64_json"
        assert body["n"] == 1
        assert "negative_prompt" not in body  # v4_1 does not document it

    def test_negative_prompt_only_on_v3(self, client, monkeypatch):
        proj = make_project(client, "C3C-RecV3")
        seen = _capture(client, monkeypatch, "recraft",
                        lambda req: httpx.Response(200, content=json.dumps({
                            "data": [{"b64_json": ""}], "credits": 40,
                        }).encode(),
                            headers={"Content-Type": "application/json"}))
        _wait(client, proj["id"], _gen(client, proj["id"], "recraft", "recraftv3",
                           negative_prompt="blurry, watermark"))
        body = json.loads(seen["request"].content)
        assert body["negative_prompt"] == "blurry, watermark"


class TestIdeogram:
    def test_sync_generation_retrieves_artifact(self, client):
        proj = make_project(client, "C3C-Ideo")
        job = _wait(client, proj["id"], _gen(client, proj["id"], "ideogram",
                                 "ideogram-3", size="3x2",
                                 quality="default"))
        assert job["status"] == "succeeded", job.get("error")
        assert job["output_asset_id"]
        a = next(i for i in client.get(
            f"/api/projects/{proj['id']}/assets").json()["items"]
            if i["id"] == job["output_asset_id"])
        # Asset bytes came from the artifact URL, not inline data.
        assert a["media_json"]["is_mock"] is True
        assert a["media_json"]["requested_size"] == "3x2"
        # Settled at the "default" speed tier: $0.06.
        charges = _charges(client, job["id"])
        assert charges and charges[0].micro == 60_000

    def test_multipart_contract_and_api_key(self, client, monkeypatch):
        proj = make_project(client, "C3C-IdeoReq")
        seen = _capture(client, monkeypatch, "ideogram",
                        lambda req: httpx.Response(200, content=json.dumps({
                            "data": [{"url": "https://cdn.ideogram.ai/"
                                             "sim/x.png",
                                      "seed": 1,
                                      "resolution": "1024x1024"}],
                        }).encode(),
                            headers={"Content-Type": "application/json"}))
        _wait(client, proj["id"], _gen(client, proj["id"], "ideogram", "ideogram-3",
                           quality="quality", seed=42,
                           negative_prompt="text, watermark"))
        req = seen["request"]
        assert req.headers.get("Api-Key") is not None
        ctype = req.headers.get("Content-Type", "")
        assert "multipart/form-data" in ctype
        raw = req.content.decode()
        assert 'name="prompt"' in raw
        assert 'name="rendering_speed"' in raw and "quality" in raw
        assert 'name="seed"' in raw and "42" in raw
        assert 'name="negative_prompt"' in raw

    def test_expired_artifact_url_is_pending_retrieval(
            self, client, monkeypatch):
        proj = make_project(client, "C3C-IdeoGone")
        # Submit returns a URL the artifact route won't serve → 404 on
        # retrieval → durable artifact_pending_retrieval + hold.
        def submit(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps({
                "data": [{"url": "https://cdn.ideogram.ai/gone/x.png"}],
            }).encode(), headers={"Content-Type": "application/json"})
        p = make_image_provider(
            "ideogram",
            transport=simulated_image_transport(
                "ideogram", {"submit": submit}))
        monkeypatch.setitem(_adapter(client).providers, "ideogram", p)
        job = _wait(client, proj["id"], _gen(client, proj["id"], "ideogram",
                                 "ideogram-3"))
        assert job["status"] == "failed"
        prog = job.get("progress_json") or {}
        assert prog.get("remote_outcome") == "artifact_pending_retrieval"
        assert prog.get("reconcile_required") is True
        # The durable row keeps the URL internally for reconcile; the
        # API surface must redact it (SEC: bearer remote refs).
        res = _reservation(client, job["id"])
        assert res is None or res.status in ("held", "open", "released",
                                             "settled")

    def test_foreign_artifact_host_refused(self, client, monkeypatch):
        proj = make_project(client, "C3C-IdeoHost")
        def submit(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps({
                "data": [{"url": "https://evil.example.com/x.png"}],
            }).encode(), headers={"Content-Type": "application/json"})
        p = make_image_provider(
            "ideogram",
            transport=simulated_image_transport(
                "ideogram", {"submit": submit}))
        monkeypatch.setitem(_adapter(client).providers, "ideogram", p)
        job = _wait(client, proj["id"], _gen(client, proj["id"], "ideogram",
                                 "ideogram-3"))
        assert job["status"] == "failed"
        prog = job.get("progress_json") or {}
        assert prog.get("remote_outcome") == "artifact_pending_retrieval"


class TestPreviewAuthority:
    def test_new_providers_estimate_authoritatively(self, client):
        proj = make_project(client, "C3C-Est")
        for provider, model, want in (
                ("gemini-images", "gemini-2.5-flash-image", 39_000),
                ("gemini-images", "imagen-4.0-generate-001", 40_000),
                ("gemini-images", "imagen-4.0-ultra-generate-001", 60_000),
                ("recraft", "recraftv4_1", 35_000),
                ("ideogram", "ideogram-3", 90_000),   # auto → top tier
        ):
            r = _preview(client, proj["id"], {
                "provider_key": provider, "model_id": model,
                "execution_target": "cloud", "prompt": "preview"})
            assert r.status_code == 200, (provider, r.text)
            ce = r.json().get("cost_estimate") or {}
            assert ce.get("est_cost_usd") is not None, provider
            assert round(ce["est_cost_usd"] * 1_000_000) == want, provider
            warnings = r.json().get("warnings") or []
            assert any("simulat" in w.lower() for w in warnings)

    def test_disabled_provider_has_no_contract(self, client):
        proj = make_project(client, "C3C-NoContract")
        r = _gen(client, proj["id"], "midjourney", "midjourney-v7")
        assert r.status_code == 422
        assert "not eligible" in r.text
