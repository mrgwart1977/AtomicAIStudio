"""C3-A — cloud image generation on simulated transports.

Everything here runs through MockTransport fixtures + a dummy-credential
provider: zero egress, zero real billing, deterministic test PNGs.
No simulated artifact may ever carry verified_remote provenance.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from app.db.models import (Asset, AssetVersion, CommandTransaction,
                           GenerationJob, SpendCharge, SpendReservation)
from app.domain import image_safety, spend
from app.domain.providers import catalog
from app.domain import cloud_cost as cc
from app.domain.workers.rest_image_providers import (
    IMAGE_REST_KEYS, make_image_provider)
from app.domain.workers.rest_providers import EgressRefused
from app.domain.workers.rest_simulated import (
    openai_images_obj, simulated_image_transport)
from tests.conftest import make_project, wait_for_job


_KEYS = iter(range(10_000, 99_999))


def _rev(client, pid):
    return client.get(f"/api/projects/{pid}").json()["revision"]


def _commit(client, pid, command_type, payload):
    return client.post(
        f"/api/projects/{pid}/commands/commit",
        json={"command_type": command_type, "payload": payload,
              "scope": "project", "expected_revision": _rev(client, pid),
              "idempotency_key": f"c3-{next(_KEYS)}"})


def _budget(client, pid, limit="5.00", scope="lifetime", target="global"):
    return _commit(client, pid, "spend.budget_set",
                   {"target": target, "scope": scope, "limit_usd": limit})


EN = {"provider_key": "openai-images", "model_id": "gpt-image-1",
      "execution_target": "cloud",
      "prompt": "a lighthouse on a cliff at dusk"}


def _gen(client, pid, **kw):
    return _commit(client, pid, "generate.image.cloud", {**EN, **kw})


def _sf(client):
    return client.app.state.session_factory


def _job_row(client, job_id):
    with _sf(client)() as s:
        return s.get(GenerationJob, job_id)


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


def _override(client, monkeypatch, handler):
    """Swap the sim provider for one whose submit route runs `handler`."""
    p = make_image_provider(
        "openai-images",
        transport=simulated_image_transport(
            "openai-images", {"submit": handler}))
    monkeypatch.setitem(_adapter(client).providers, "openai-images", p)


def _status_handler(status: int, body: dict | None = None):
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, content=json.dumps(
                body or {"error": {"type": "test_error",
                                   "message": "boom"}}).encode(),
            headers={"Content-Type": "application/json"})
    return h


def _png_bytes(w=64, h=64, color=(30, 40, 60)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------- preview → commit → job → asset ----------------

class TestHappyPath:
    def test_preview_warns_and_validates(self, client):
        pid = make_project(client)["id"]
        r = client.post(f"/api/projects/{pid}/commands/preview",
                        json={"command_type": "generate.image.cloud",
                              "payload": EN, "scope": "project"})
        assert r.status_code == 200, r.text
        joined = " ".join(r.json().get("warnings") or [])
        assert "ESTIMATE" in joined
        assert "SIMULATED" in joined
        assert "sends:" in joined  # data-out consent surface

    def test_commit_enqueue_run_settle_asset(self, client):
        pid = make_project(client)["id"]
        _budget(client, pid)
        r = _gen(client, pid, size="1024x1024", quality="high")
        assert r.status_code == 200, r.text
        jid = r.json()["job_id"]
        job = wait_for_job(client, pid, jid)
        assert job["status"] == "succeeded", job.get("error")
        # financial: simulated reservation settled at the catalog rate —
        # high-quality gpt-image-1 → $0.19 per image
        res = _reservation(client, jid)
        assert res.status == "settled"
        assert res.environment == "simulated"
        chg = _charges(client, jid)
        assert len(chg) == 1
        assert chg[0].micro == spend.usd_to_micro(0.19)
        assert chg[0].source == "simulated"
        # asset landed in CAS with job linkage + honest provenance
        assert job["output_asset_id"]
        with _sf(client)() as s:
            asset = s.get(Asset, job["output_asset_id"])
            ver = s.execute(select(AssetVersion).where(
                AssetVersion.asset_id == asset.id)).scalar_one()
        prov = asset.provenance_json
        assert prov["adapter_id"] == "cloud.image.sim"
        assert prov["job_id"] == jid
        assert prov["remote_provider"] == "openai-images"
        assert prov["provenance_confidence"] == "simulated"
        media = ver.media_json
        assert media["is_mock"] is True
        assert media["model_id"] == "gpt-image-1"
        assert media["actual_width"] == 512  # scaled fixture
        assert media["requested_size"] == "1024x1024"

    def test_filmmaking_metadata_lands(self, client):
        pid = make_project(client)["id"]
        _budget(client, pid)
        r = _gen(client, pid, shot_id="shot-1", scene_id="sc-9",
                 character_id="ch-3", world_id="w-2", location_id="loc-5",
                 reference_asset_id="asset-x", negative_prompt="blur")
        assert r.status_code == 200, r.text
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert job["status"] == "succeeded"
        req = _job_row(client, job["id"]).request_json
        for k in ("shot_id", "scene_id", "character_id", "world_id",
                  "location_id", "reference_asset_id", "negative_prompt"):
            assert k in req
        assert req["shot_id"] == "shot-1"
        assert req["negative_prompt"] == "blur"

    def test_deterministic_image_bytes(self, client):
        """Same prompt+size → identical fixture PNG (sha match)."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        a = _gen(client, pid)
        b = _gen(client, pid)
        ja = wait_for_job(client, pid, a.json()["job_id"])
        jb = wait_for_job(client, pid, b.json()["job_id"])
        assert ja["status"] == jb["status"] == "succeeded"
        with _sf(client)() as s:
            sha_a = s.get(Asset, ja["output_asset_id"]).sha256
            sha_b = s.get(Asset, jb["output_asset_id"]).sha256
        assert sha_a == sha_b and len(sha_a) == 64

    def test_different_prompt_different_bytes(self, client):
        pid = make_project(client)["id"]
        _budget(client, pid)
        a = _gen(client, pid, prompt="red barn in snow")
        b = _gen(client, pid, prompt="green valley at noon")
        ja = wait_for_job(client, pid, a.json()["job_id"])
        jb = wait_for_job(client, pid, b.json()["job_id"])
        with _sf(client)() as s:
            assert s.get(Asset, ja["output_asset_id"]).sha256 != \
                s.get(Asset, jb["output_asset_id"]).sha256


# ---------------- validation + fail-closed ----------------

class TestValidation:
    def test_missing_prompt_refused(self, client):
        pid = make_project(client)["id"]
        assert _gen(client, pid, prompt="").status_code == 422
        assert _gen(client, pid, prompt="   ").status_code == 422

    def test_unknown_provider_404(self, client):
        pid = make_project(client)["id"]
        assert _gen(client, pid, provider_key="nope").status_code == 404

    def test_provider_without_image_contract_refused(self, client):
        """Catalog provider with no C3-A image contract → 422 at commit,
        before any enqueue or reservation."""
        pid = make_project(client)["id"]
        assert _gen(client, pid, provider_key="openai").status_code == 422
        assert _gen(client, pid, provider_key="adobe-firefly",
                    model_id="firefly-image-4").status_code == 422

    def test_unknown_model_refused(self, client):
        pid = make_project(client)["id"]
        assert _gen(client, pid, model_id="does-not-exist"
                    ).status_code == 422

    def test_missing_pricing_fails_closed(self, client):
        """A model with no cost_table → est is None; commit refuses."""
        prov = catalog.get_provider("adobe-firefly")
        m = cc.pick_image_model(prov, "firefly-image-4")
        assert m is not None
        assert cc.estimate_image_cost(m, count=1)["est_cost_usd"] is None

    def test_count_other_than_one_refused(self, client):
        pid = make_project(client)["id"]
        assert _gen(client, pid, count=3).status_code == 422

    def test_preview_provider_not_found(self, client):
        pid = make_project(client)["id"]
        r = client.post(f"/api/projects/{pid}/commands/preview",
                        json={"command_type": "generate.image.cloud",
                              "payload": {**EN, "provider_key": "nope"},
                              "scope": "project"})
        assert r.status_code == 404


# ---------------- cost model units ----------------

class TestCostUnits:
    def test_per_image_flat(self):
        m = {"cost_table": {"unit": "per_image", "rate": 0.039,
                            "checked": "2026-11-13"}}
        assert cc.estimate_image_cost(m, count=1)["est_cost_usd"] == 0.039

    def test_per_image_quality_tier(self):
        m = {"cost_table": {"unit": "per_image",
                            "rates": {"low": 0.02, "high": 0.19},
                            "checked": "2026-11-13"}}
        assert cc.estimate_image_cost(m, quality="high"
                                    )["est_cost_usd"] == 0.19
        assert cc.estimate_image_cost(m, quality="low"
                                    )["est_cost_usd"] == 0.02

    def test_unspecified_quality_reserves_max_tier(self):
        """Tiered table + no quality → conservative bound = max tier —
        never an under-estimate, never a refusal."""
        m = {"cost_table": {"unit": "per_image",
                            "rates": {"low": 0.02, "medium": 0.07,
                                      "high": 0.19},
                            "checked": "2026-11-13"}}
        assert cc.estimate_image_cost(m)["est_cost_usd"] == 0.19
        assert cc.estimate_image_cost(m, quality="platinum"
                                    )["est_cost_usd"] == 0.19

    def test_per_mpixel(self):
        m = {"cost_table": {"unit": "per_mpixel", "rate": 0.02,
                            "checked": "2026-11-13"}}
        est = cc.estimate_image_cost(m, size="1024x1024")
        assert est["est_cost_usd"] == pytest.approx(0.02097, abs=1e-4)
        assert cc.estimate_image_cost(m, size="16:9"
                                    )["est_cost_usd"] is not None

    def test_per_mtok(self):
        m = {"cost_table": {"unit": "per_mtok",
                            "input_per_mtok": 5.0, "output_per_mtok": 40.0,
                            "assumed_output_tokens": 900,
                            "checked": "2026-11-13"}}
        est = cc.estimate_image_cost(m, prompt="x" * 80)
        assert est["est_cost_usd"] is not None
        assert est["unit"] == "per_mtok"

    def test_per_credit(self):
        m = {"cost_table": {"unit": "per_credit", "credits_per_image": 8,
                            "usd_per_credit": 0.0025,
                            "checked": "2026-11-13"}}
        assert cc.estimate_image_cost(m)["est_cost_usd"] == 0.02

    @pytest.mark.parametrize("table", [
        {},                                          # no table
        {"unit": "per_image"},                       # no rate, no checked
        {"unit": "per_image", "rate": 0.04},         # no checked evidence
        {"unit": "mystery", "rate": 1.0,
         "checked": "2026-11-13"},                   # unknown unit
        {"unit": "per_mpixel", "rate": 0.02,
         "checked": "2026-11-13"},                   # unparseable size
    ])
    def test_fail_closed(self, table):
        assert cc.estimate_image_cost(
            {"cost_table": table}, size=None)["est_cost_usd"] is None

    @pytest.mark.parametrize("checked", [
        "2000-01-01",      # stale — far beyond the 90-day freshness window
        "not-a-date",      # malformed evidence
        "2999-01-01",      # future-dated — cannot be a real check date
    ])
    def test_stale_or_untrusted_pricing_fails_closed(self, checked):
        """Pricing evidence that is missing, malformed, stale, or
        future-dated is NOT authoritative — est stays None and the
        commit path refuses."""
        m = {"cost_table": {"unit": "per_image", "rate": 0.04,
                            "checked": checked}}
        est = cc.estimate_image_cost(m)
        assert est["est_cost_usd"] is None
        assert est["price_fresh"] is False


# ---------------- image safety gate ----------------

class TestImageSafety:
    def test_valid_png_passes(self):
        info = image_safety.inspect_image(_png_bytes())
        assert info.format == "PNG" and info.width == 64
        assert info.sha256 == hashlib.sha256(_png_bytes()).hexdigest()

    def test_valid_jpeg_webp_pass(self):
        for fmt in ("JPEG", "WEBP"):
            buf = io.BytesIO()
            Image.new("RGB", (32, 32)).save(buf, format=fmt)
            assert image_safety.inspect_image(
                buf.getvalue()).format == fmt

    @pytest.mark.parametrize("bad", [
        b"",                                       # empty
        b"not-an-image",                           # garbage
        b"\x89PNG\r\n\x1a\n" + b"x" * 20,          # truncated PNG
    ])
    def test_invalid_rejected(self, bad):
        with pytest.raises(image_safety.ImageRejected):
            image_safety.inspect_image(bad)

    def test_oversize_bytes_rejected(self):
        big = b"\x89PNG\r\n\x1a\n" + b"0" * image_safety.MAX_IMAGE_BYTES
        with pytest.raises(image_safety.ImageRejected):
            image_safety.inspect_image(big)

    def test_bomb_rejected(self):
        """Declared geometry beyond the pixel cap refuses before decode
        — a tiny file declaring 40000x40000 is a decompression bomb."""
        buf = io.BytesIO()
        Image.new("RGB", (4, 4)).save(buf, format="PNG")
        data = bytearray(buf.getvalue())
        # IHDR width/height live at fixed offsets (16, 20) in PNG.
        data[16:20] = (40_000).to_bytes(4, "big")
        data[20:24] = (40_000).to_bytes(4, "big")
        with pytest.raises(image_safety.ImageRejected):
            image_safety.inspect_image(bytes(data))

    def test_non_png_container_rejected(self):
        with pytest.raises(image_safety.ImageRejected):
            image_safety.inspect_image(b"GIF89a" + b"\x00" * 32)

    def test_thumbnail_safe(self):
        thumb = image_safety.make_thumbnail(_png_bytes(512, 512))
        img = Image.open(io.BytesIO(thumb))
        assert img.format == "PNG"
        assert max(img.size) <= 256


# ---------------- provider-shaped failure paths ----------------

class TestFailurePaths:
    def test_malformed_b64_payload_holds_reservation(
            self, client, monkeypatch):
        """200 OK but unusable payload → job fails, reservation HELD
        open for reconcile — never silently released."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        body = {"created": 1, "data": [{"b64_json": "%%%notb64%%%"}],
                "usage": {"input_tokens": 5, "output_tokens": 900,
                          "total_tokens": 905}}
        _override(client, monkeypatch,
                  _status_handler(200, body))
        r = _gen(client, pid)
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert job["status"] == "failed"
        res = _reservation(client, job["id"])
        assert res.status == "open"        # exposure retained
        assert "safety inspection" in (res.reconcile_note or "")

    def test_rejected_image_bytes_hold_reservation(
            self, client, monkeypatch):
        """Provider-shaped but unsafe payload (GIF container) → job
        fails, reservation held open."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        body = {"created": 1,
                "data": [{"b64_json": base64.b64encode(
                    b"GIF89a" + b"\x00" * 32).decode()}],
                "usage": {"input_tokens": 5, "output_tokens": 900,
                          "total_tokens": 905}}
        _override(client, monkeypatch, _status_handler(200, body))
        r = _gen(client, pid)
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert job["status"] == "failed"
        assert _reservation(client, job["id"]).status == "open"

    def test_empty_data_holds_reservation(self, client, monkeypatch):
        """200 OK with no data entries → ambiguous billing (the remote
        may have processed) → job fails, reservation HELD open."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        _override(client, monkeypatch, _status_handler(
            200, {"created": 1, "data": []}))
        r = _gen(client, pid)
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert job["status"] == "failed"
        res = _reservation(client, job["id"])
        assert res.status == "open"
        assert "unknown" in (res.reconcile_note or "")

    def test_per_image_settles_without_token_usage(
            self, client, monkeypatch):
        """per_image unit bills on images_returned — a missing usage
        object is NOT unresolved exposure for this billing unit."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        body = openai_images_obj(prompt="p", seed=1, size="1024x1024")
        body.pop("usage", None)
        _override(client, monkeypatch, _status_handler(200, body))
        r = _gen(client, pid)
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert job["status"] == "succeeded"
        res = _reservation(client, job["id"])
        assert res.status == "settled"
        chg = _charges(client, job["id"])
        # no quality requested → conservative max-tier rate
        assert chg[0].micro == spend.usd_to_micro(0.19)

    def test_usage_unavailable_units_hold(self):
        """Billing bases that cannot be determined must hold — never
        fabricate a charge, never silently release."""
        from app.adapters.cloud_image import _image_cost_micro
        assert _image_cost_micro(
            None, None, {"unit": "per_image"}, None
            ) == (None, "no_images_reported")
        assert _image_cost_micro(
            None, 1, {"unit": "per_mtok"}, None
            ) == (None, "no_usage_reported")
        assert _image_cost_micro(
            {"input_tokens": 5, "output_tokens": 9}, 1,
            {"unit": "per_mtok"}, None) == (None, "no_token_rates")
        assert _image_cost_micro(
            None, 1, {"unit": "per_image", "rates": {}}, None
            ) == (None, "no_image_rate")
        assert _image_cost_micro(
            None, 1, {"unit": "per_credit"}, None
            ) == (None, "no_credit_rate")
        assert _image_cost_micro(
            None, 1, {"unit": "per_credit",
                      "usd_per_credit": 0.001}, None
            ) == (None, "no_credits_reported")
        assert _image_cost_micro(
            {"credits": 35}, 1, {"unit": "per_credit",
                                 "usd_per_credit": 0.001}, None
            ) == (35_000, "credits_x_catalog_rate")
        assert _image_cost_micro(
            None, 1, {"unit": "per_mpixel"}, None
            ) == (None, "unit_not_settled_in_c3a")
        # usable basis → real settle math
        micro, basis = _image_cost_micro(
            None, 1, {"unit": "per_image",
                      "rates": {"low": 0.02, "high": 0.19}}, "high")
        assert micro == spend.usd_to_micro(0.19)

    def test_provider_4xx_releases_reservation(self, client, monkeypatch):
        pid = make_project(client)["id"]
        _budget(client, pid)
        _override(client, monkeypatch, _status_handler(
            400, {"error": {"type": "invalid_request_error",
                            "message": "bad size"}}))
        r = _gen(client, pid)
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert job["status"] == "failed"
        assert _reservation(client, job["id"]).status == "released"

    def test_provider_5xx_holds_reservation(self, client, monkeypatch):
        pid = make_project(client)["id"]
        _budget(client, pid)
        _override(client, monkeypatch, _status_handler(
            500, {"error": {"type": "server_error", "message": "x"}}))
        r = _gen(client, pid)
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert job["status"] == "failed"
        res = _reservation(client, job["id"])
        assert res.status == "open"
        assert res.reconcile_note          # hold note recorded

    def test_oversized_b64_refused_before_decode(self, client, monkeypatch):
        """The encoded payload itself is capped before base64 decode —
        a 20+ MB string can never materialize inside the safety gate."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        big = "A" * (image_safety.MAX_IMAGE_BYTES * 4 // 3 + 2048)
        body = {"created": 1, "data": [{"b64_json": big}],
                "usage": {"input_tokens": 5, "output_tokens": 900,
                          "total_tokens": 905}}
        _override(client, monkeypatch, _status_handler(200, body))
        r = _gen(client, pid)
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert job["status"] == "failed"
        res = _reservation(client, job["id"])
        assert res.status == "open"
        assert "safety inspection" in (res.reconcile_note or "")

    def test_url_only_is_artifact_pending_not_ambiguous(
            self, client, monkeypatch):
        """URL-only response = remote SUCCESS with an unretrieved
        artifact — a distinct outcome, never 'ambiguous', never
        resubmitted. The URL is preserved for reconcile/retrieval;
        usable usage still settles (the work was billed)."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        body = {"created": 1,
                "data": [{"url": "https://cdn.example/sim-temp.png",
                          "revised_prompt": "x"}],
                "usage": {"input_tokens": 5, "output_tokens": 900,
                          "total_tokens": 905}}
        _override(client, monkeypatch, _status_handler(200, body))
        r = _gen(client, pid)
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert job["status"] == "failed"      # no artifact to register
        row = _job_row(client, job["id"])
        pj = row.progress_json or {}
        assert pj["remote_outcome"] == "artifact_pending_retrieval"
        assert pj["reconcile_required"] is True
        assert pj["image_url"] == "https://cdn.example/sim-temp.png"
        assert pj["remote_status"] == "succeeded"
        assert _reservation(client, job["id"]).status == "settled"
        assert not job["output_asset_id"]

    def test_url_only_without_usage_holds(self, client, monkeypatch):
        """URL-only + unusable billing basis → held for reconcile;
        still a distinct artifact-pending outcome, not a silent
        release. (per_image settles on images_returned, so this uses a
        token-billed table where usage IS the basis.)"""
        pid = make_project(client)["id"]
        _budget(client, pid)
        orig = cc.pick_image_model
        monkeypatch.setattr(
            cc, "pick_image_model",
            lambda prov, mid: {**(orig(prov, mid) or {}),
                               "cost_table": _PER_MTOK_TABLE})
        body = {"created": 1,
                "data": [{"url": "https://cdn.example/sim-temp.png"}]}
        _override(client, monkeypatch, _status_handler(200, body))
        r = _gen(client, pid)
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert job["status"] == "failed"
        pj = _job_row(client, job["id"]).progress_json or {}
        assert pj["remote_outcome"] == "artifact_pending_retrieval"
        res = _reservation(client, job["id"])
        assert res.status == "open"
        assert "billing unresolved" in (res.reconcile_note or "")
        assert "held for reconcile" in (res.reconcile_note or "")


# ---------- sensitive remote-URL redaction (C3-A hardening) ----------

_URL_BODY = {"created": 1,
             "data": [{"url": "https://cdn.example/sim-temp.png?sig=SECRET",
                       "revised_prompt": "x"}],
             "usage": {"input_tokens": 5, "output_tokens": 900,
                       "total_tokens": 905}}


class TestSensitiveUrlRedaction:
    """Signed/temporary remote URLs persist for reconcile but are
    bearer references — they must never leave via API, WS, errors."""

    def _pending_job(self, client, monkeypatch, pid):
        _override(client, monkeypatch, _status_handler(200, _URL_BODY))
        r = _gen(client, pid)
        return wait_for_job(client, pid, r.json()["job_id"])

    def test_redact_detail_deep_walk(self):
        from app.domain.redact import redact_detail, REDACTED
        raw = {"image_url": "https://x?sig=1",
               "remote_outcome": "artifact_pending_retrieval",
               "attempts": [{"detail": {"download_url": "https://y"},
                             "status": "ok"}],
               "nested": {"list": [{"url": "https://z"}, 3, "s"]},
               "remote_job_id": "r-1"}
        out = redact_detail(raw)
        assert out["image_url"] == REDACTED
        assert out["attempts"][0]["detail"]["download_url"] == REDACTED
        assert out["nested"]["list"][0]["url"] == REDACTED
        # non-sensitive values + key presence preserved
        assert out["remote_outcome"] == "artifact_pending_retrieval"
        assert out["attempts"][0]["status"] == "ok"
        assert out["nested"]["list"][1:] == [3, "s"]
        assert out["remote_job_id"] == "r-1"
        assert raw["image_url"].startswith("https://")  # original intact

    def test_job_api_redacts_pending_url(self, client, monkeypatch):
        pid = make_project(client)["id"]
        _budget(client, pid)
        job = self._pending_job(client, monkeypatch, pid)
        assert job["status"] == "failed"
        pj = job["progress_json"] or {}
        # API view: marker only — the raw URL appears nowhere in the
        # serialized job (not progress, not error).
        assert pj["image_url"] != _URL_BODY["data"][0]["url"]
        assert "redacted" in pj["image_url"]
        assert _URL_BODY["data"][0]["url"] not in json.dumps(job)
        # outcome classification survives redaction
        assert pj["remote_outcome"] == "artifact_pending_retrieval"
        assert pj["remote_status"] == "succeeded"
        assert pj["reconcile_required"] is True
        # durable store KEEPS the URL for future reconcile/retrieval
        assert (_job_row(client, job["id"]).progress_json
                ["image_url"] == _URL_BODY["data"][0]["url"])

    def test_ws_broadcast_redacts_pending_url(self, client, monkeypatch):
        pid = make_project(client)["id"]
        _budget(client, pid)
        _override(client, monkeypatch, _status_handler(200, _URL_BODY))
        events = []
        with client.websocket_connect("/api/ws?token=test-token") as ws:
            r = _gen(client, pid)
            jid = r.json()["job_id"]
            # drain job.progress events until the job reaches terminal
            for _ in range(200):
                msg = ws.receive_json()
                if (msg.get("event") == "job.progress"
                        and msg.get("resource_id") == jid):
                    events.append(msg["payload"])
                    if msg["payload"].get("status") in (
                            "succeeded", "failed", "cancelled"):
                        break
        assert events, "no job.progress events received"
        blob = json.dumps(events)
        assert _URL_BODY["data"][0]["url"] not in blob
        assert "sig=SECRET" not in blob
        assert "redacted" in blob  # marker present in the final state

    def test_jobout_redacts_result_and_progress(self):
        from app.domain.redact import REDACTED
        from app.schemas.api import JobOut
        from app.db.models import utcnow
        job = JobOut(
            id="j", project_id="p", adapter_id="a", kind="image",
            status="failed", progress=0.9, message="m", error=None,
            resumable=False, output_asset_id=None, command_id=None,
            result_json={"image_url": "https://x?sig=1", "ok": 1},
            progress_json={"image_url": "https://x?sig=1"},
            created_at=utcnow(), completed_at=None)
        assert job.progress_json["image_url"] == REDACTED
        assert job.result_json["image_url"] == REDACTED
        assert job.result_json["ok"] == 1


# ---------------- idempotency / concurrency / budgets ----------------

class TestFinancialDiscipline:
    def test_idempotent_commit_replay_no_second_job(self, client):
        pid = make_project(client)["id"]
        _budget(client, pid)
        k = "c3-idem-1"
        r1 = client.post(
            f"/api/projects/{pid}/commands/commit",
            json={"command_type": "generate.image.cloud", "payload": EN,
                  "scope": "project",
                  "expected_revision": _rev(client, pid),
                  "idempotency_key": k})
        r2 = client.post(
            f"/api/projects/{pid}/commands/commit",
            json={"command_type": "generate.image.cloud", "payload": EN,
                  "scope": "project",
                  "expected_revision": _rev(client, pid),
                  "idempotency_key": k})
        assert r1.status_code == r2.status_code == 200
        assert r2.json()["idempotent_replay"] is True
        job = wait_for_job(client, pid, r1.json()["job_id"])
        assert job["status"] == "succeeded"
        with _sf(client)() as s:
            jobs = s.execute(select(GenerationJob).where(
                GenerationJob.project_id == pid,
                GenerationJob.adapter_id == "cloud.image.sim")
            ).scalars().all()
            assert len(jobs) == 1
            res = s.execute(select(SpendReservation).where(
                SpendReservation.job_id == job["id"])).scalar_one()
            assert res.status == "settled"
            charges = s.execute(select(SpendCharge).where(
                SpendCharge.job_id == job["id"])).scalars().all()
            assert len(charges) == 1   # replay did not double-charge

    def test_changed_payload_same_key_409(self, client):
        pid = make_project(client)["id"]
        _budget(client, pid)
        k = "c3-idem-2"
        client.post(f"/api/projects/{pid}/commands/commit",
                    json={"command_type": "generate.image.cloud",
                          "payload": EN, "scope": "project",
                          "expected_revision": _rev(client, pid),
                          "idempotency_key": k})
        r = client.post(
            f"/api/projects/{pid}/commands/commit",
            json={"command_type": "generate.image.cloud",
                  "payload": {**EN, "prompt": "different"},
                  "scope": "project",
                  "expected_revision": _rev(client, pid),
                  "idempotency_key": k})
        assert r.status_code == 409

    def test_budget_exceeded_refused(self, client):
        pid = make_project(client)["id"]
        _budget(client, pid, limit="0.10")   # below the $0.19 est
        r = _gen(client, pid, quality="high")
        assert r.status_code == 422
        assert "budget" in r.text.lower()
        # refused BEFORE enqueue — no job, no reservation
        with _sf(client)() as s:
            jobs = s.execute(select(GenerationJob).where(
                GenerationJob.project_id == pid,
                GenerationJob.adapter_id == "cloud.image.sim")
            ).scalars().all()
            assert jobs == []

    def test_no_budget_still_books_simulated(self, client):
        """Simulated jobs reserve like live ones — a missing budget is
        not an error in the simulated environment (bounded=est)."""
        pid = make_project(client)["id"]
        r = _gen(client, pid)
        assert r.status_code == 200, r.text
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert job["status"] == "succeeded"

    def test_concurrent_submissions_book_separately(self, client):
        pid = make_project(client)["id"]
        _budget(client, pid)
        a = _gen(client, pid, prompt="first shot")
        b = _gen(client, pid, prompt="second shot")
        ja = wait_for_job(client, pid, a.json()["job_id"])
        jb = wait_for_job(client, pid, b.json()["job_id"])
        assert ja["status"] == jb["status"] == "succeeded"
        with _sf(client)() as s:
            res = s.execute(select(SpendReservation).where(
                SpendReservation.project_id == pid)).scalars().all()
        assert len(res) == 2
        assert all(r.status == "settled" for r in res)


# ---------------- restart / safety invariants ----------------

class TestRestartAndBoundaries:
    def test_open_reservation_survives_restart_sweep(
            self, client, monkeypatch):
        pid = make_project(client)["id"]
        _budget(client, pid)
        body = {"created": 1, "data": [{"b64_json": "%%%"}]}
        _override(client, monkeypatch, _status_handler(200, body))
        r = _gen(client, pid)
        job = wait_for_job(client, pid, r.json()["job_id"])
        jid = job["id"]
        assert _reservation(client, jid).status == "open"
        with _sf(client)() as s:
            engine = client.app.state.job_engine
            assert engine.reconcile_interrupted(s) == 0
            s.commit()
        assert _reservation(client, jid).status == "open"

    def test_no_credential_read_anywhere(self, client):
        """The sim path never touches the vault — a spy replacing it
        records zero get_fields calls across a full run."""
        pid = make_project(client)["id"]
        _budget(client, pid)

        class SpyVault:
            calls = 0
            def get_fields(self, key):
                self.calls += 1
                return {}

        spy = SpyVault()
        client.app.state.provider_vault = spy
        r = _gen(client, pid)
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert job["status"] == "succeeded"
        assert spy.calls == 0

    def test_no_real_egress(self, client):
        """Every provider transport is a MockTransport — and live
        transport classes are refused at construction."""
        adapter = _adapter(client)
        assert adapter.spend_environment == "simulated"
        for p in adapter.providers.values():
            assert isinstance(p._transport, httpx.MockTransport)
        for bad in (httpx.AsyncHTTPTransport(), httpx.HTTPTransport(),
                    object(), None):
            with pytest.raises(EgressRefused):
                make_image_provider("openai-images", transport=bad)

    def test_provenance_never_verified(self, client):
        pid = make_project(client)["id"]
        _budget(client, pid)
        r = _gen(client, pid)
        job = wait_for_job(client, pid, r.json()["job_id"])
        with _sf(client)() as s:
            prov = s.get(Asset, job["output_asset_id"]).provenance_json
        assert prov["provenance_confidence"] == "simulated"
        assert "verified_remote" not in json.dumps(prov)

    def test_contract_shape_documented(self):
        """The image contract is declarative EndpointSpec data — the
        same shape an async/poll provider would declare."""
        from app.domain.workers.rest_image_providers import (
            OPENAI_IMAGES_SPEC)
        assert OPENAI_IMAGES_SPEC.lifecycle == "sync"
        assert OPENAI_IMAGES_SPEC.submit_path == "/v1/images/generations"
        assert "openai-images" in IMAGE_REST_KEYS


# ---------------- review-hardening invariants ----------------

_PER_MTOK_TABLE = {"unit": "per_mtok", "input_per_mtok": 5.0,
                   "output_per_mtok": 40.0, "assumed_output_tokens": 900,
                   "checked": "2026-11-13"}


class TestHardening:
    def test_simulated_financial_isolation(self, client):
        """Simulated accounting is environment-partitioned: a settled
        sim charge never enters live consumption, never inflates live
        exposure, and is labeled 'simulated' everywhere it appears."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        r = _gen(client, pid)
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert job["status"] == "succeeded"
        res = _reservation(client, job["id"])
        chg = _charges(client, job["id"])
        assert res.environment == "simulated"
        assert len(chg) == 1
        assert chg[0].environment == "simulated"
        assert chg[0].source == "simulated"
        with _sf(client)() as s:
            sid = spend.billing_session_id(s)
            live = spend.consumed_micro(
                s, environment="live", scope="lifetime",
                project_id=pid, session_id=sid)
            sim = spend.consumed_micro(
                s, environment="simulated", scope="lifetime",
                project_id=pid, session_id=sid)
            from app.domain import live_gate as _lg
            # zero live consumption AND zero live admission exposure —
            # simulated holds can never block a real provider
            assert live == 0
            assert _lg.live_exposure_micro(s) == 0
            assert sim == spend.usd_to_micro(0.19)
            # audit record carries the simulated environment label
            tx = s.execute(select(CommandTransaction).where(
                CommandTransaction.project_id == pid)
                .order_by(CommandTransaction.after_revision.desc())
            ).scalars().first()
            assert tx.audit_json["after"]["cloud"]["environment"] \
                == "simulated"

    def test_success_artifact_with_unresolved_billing(
            self, client, monkeypatch):
        """A VALID image whose billing basis is unusable: the artifact
        persists, the job succeeds, the reservation stays OPEN
        (usage_unavailable) — no fabricated charge, no silent release.
        Artifact success and financial resolution are independent."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        # A token-billed model settles ONLY on reported usage — forcing
        # the unresolved-exposure path while the image itself is valid.
        orig = cc.pick_image_model
        monkeypatch.setattr(
            cc, "pick_image_model",
            lambda prov, mid: {**(orig(prov, mid) or {}),
                               "cost_table": _PER_MTOK_TABLE})
        body = openai_images_obj(prompt="p", seed=1, size="1024x1024")
        body.pop("usage", None)
        _override(client, monkeypatch, _status_handler(200, body))
        r = _gen(client, pid)
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert job["status"] == "succeeded"
        assert job["output_asset_id"]
        res = _reservation(client, job["id"])
        assert res.status == "open"
        assert "usage_unavailable" in (res.reconcile_note or "")
        assert _charges(client, job["id"]) == []
        with _sf(client)() as s:
            prov = s.get(Asset, job["output_asset_id"]).provenance_json
        assert prov["provenance_confidence"] == "simulated"

    def test_registration_failure_fails_job_cleanly(
            self, client, monkeypatch):
        """A partial CAS/asset-registration failure must NOT leave a
        'succeeded' job with no artifact nor strand the job 'running':
        it fails, output_asset_id stays empty, no Asset row exists."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        engine = client.app.state.job_engine

        def boom(*_a, **_k):
            raise OSError("disk full (simulated)")

        monkeypatch.setattr(engine, "_register_asset", boom)
        r = _gen(client, pid)
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert job["status"] == "failed"
        assert "asset registration failed" in (job.get("error") or "")
        assert not job["output_asset_id"]
        with _sf(client)() as s:
            assert s.execute(select(Asset).where(
                Asset.project_id == pid)).scalars().all() == []
        # Financial state stays consistent — the sim settle for the
        # produced bytes stands (accounting ≠ artifact persistence).
        assert _reservation(client, job["id"]).status == "settled"

    def test_held_exposure_reconciles_explicitly(
            self, client, monkeypatch):
        """An open hold resolves only through the audited reconcile
        command — confirmed_no_charge requires the operator note."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        _override(client, monkeypatch, _status_handler(
            200, {"created": 1, "data": []}))
        r = _gen(client, pid)
        job = wait_for_job(client, pid, r.json()["job_id"])
        jid = job["id"]
        assert _reservation(client, jid).status == "open"
        # release without a note is refused — ambiguity alone can never
        # release a hold
        bad = _commit(client, pid, "spend.reconcile",
                      {"job_id": jid, "outcome": "confirmed_no_charge"})
        assert bad.status_code == 422
        assert _reservation(client, jid).status == "open"
        ok = _commit(client, pid, "spend.reconcile",
                     {"job_id": jid, "outcome": "confirmed_no_charge",
                      "note": "simulated transport — verified no billing"})
        assert ok.status_code == 200, ok.text
        assert _reservation(client, jid).status == "released"


class TestImageContracts:
    """C3-B — the catalog→UI contract surface. The panel must be able to
    distinguish implemented (selectable) image providers from
    catalog-only entries without hardcoding a parallel list."""

    def test_contracts_lists_exactly_the_implemented_keys(self, client):
        r = client.get("/api/providers/image-contracts")
        assert r.status_code == 200
        body = r.json()
        assert sorted(body["implemented"]) == sorted(IMAGE_REST_KEYS)
        assert body["adapter_id"] == "cloud.image.sim"
        assert body["simulated"] is True

    def test_image_models_expose_sizes_and_qualities(self, client):
        """Catalog model rows declare supported dimensions/quality tiers
        so the UI offers real options rather than inventing settings."""
        r = client.get("/api/providers/openai-images")
        assert r.status_code == 200
        models = {m["id"]: m for m in r.json()["models"]}
        m = models["gpt-image-1"]
        assert m["capability"] == "image"
        assert "1024x1024" in m["sizes"]
        assert "1024x1536" in m["sizes"]
        assert set(m["qualities"]) >= {"low", "medium", "high"}
        assert m["cost_table"]["unit"] == "per_image"

    def test_unimplemented_image_provider_stays_selectable_only_in_catalog(
            self, client):
        """A catalog image provider without a worker contract is still
        returned by the catalog (so the UI can display it as disabled)
        but is absent from the implemented list."""
        r = client.get("/api/providers/image-contracts")
        implemented = r.json()["implemented"]
        r2 = client.get("/api/providers?category=image")
        assert r2.status_code == 200
        catalog_keys = {p["key"] for p in r2.json()}
        assert implemented[0] in catalog_keys
        # every non-implemented catalog entry is exactly what the UI
        # disables — the contract list is the single source of truth
        assert catalog_keys - set(implemented)


class TestPreviewCostAuthority:
    """C3-B review-hardening — the backend preview response carries the
    AUTHORITATIVE estimate the approval surface must display; the same
    figure is what the commit reserves. Client-side hints are
    preliminary only and can never override it."""

    def _preview(self, client, pid, **payload):
        body = {"provider_key": "openai-images", "model_id": "gpt-image-1",
                "prompt": "a test image", "execution_target": "cloud",
                **payload}
        return client.post(
            f"/api/projects/{pid}/commands/preview",
            json={"command_type": "generate.image.cloud", "payload": body,
                  "scope": "project", "origin": "ui"})

    def test_preview_returns_authoritative_cost_estimate(self, client):
        pid = make_project(client)["id"]
        r = self._preview(client, pid)
        assert r.status_code == 200, r.text
        ce = r.json()["cost_estimate"]
        # provider-default quality reserves the HIGHEST tier = $0.19
        assert ce["est_cost_usd"] == pytest.approx(0.19)
        assert ce["unit"] == "per_image"
        assert ce["is_estimate"] is True
        assert ce["simulated"] is True

    def test_preview_estimate_tracks_quality_tier(self, client):
        pid = make_project(client)["id"]
        for quality, usd in (("low", 0.02), ("medium", 0.07),
                             ("high", 0.19), ("auto", 0.19)):
            ce = self._preview(client, pid, quality=quality) \
                .json()["cost_estimate"]
            assert ce["est_cost_usd"] == pytest.approx(usd), quality

    def test_preview_estimate_matches_reserved_amount(self, client):
        """The figure shown at approval is the figure reserved at
        commit — approval display and financial admission share one
        number, not two parallel computations."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        est = self._preview(client, pid, quality="medium") \
            .json()["cost_estimate"]["est_cost_usd"]
        r = _gen(client, pid, quality="medium")
        job = wait_for_job(client, pid, r.json()["job_id"])
        assert job["status"] == "succeeded"
        res = _reservation(client, job["id"])
        assert res.reserved_micro == spend.usd_to_micro(est)

    def test_preview_refuses_unestimable_pricing(self, client, monkeypatch):
        """Unestimable pricing is refused at PREVIEW — the UI never gets
        an approval surface it could 'approve' around."""
        pid = make_project(client)["id"]
        orig = cc.pick_image_model
        monkeypatch.setattr(
            cc, "pick_image_model",
            lambda prov, mid: {**(orig(prov, mid) or {}),
                               "cost_table": {"unit": "per_credit"}})
        r = self._preview(client, pid)
        assert r.status_code == 422
        assert "pricing_unavailable" in r.text
