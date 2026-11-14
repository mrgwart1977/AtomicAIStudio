"""C3-D2 — simulated image-to-image on documented edits contracts.

Everything is simulated: MockTransport fixtures, dummy credentials,
deterministic test PNGs, synthetic accounting. The suite covers
capability negotiation (catalog declaration AND implemented contract),
source/reference validation, project isolation, cost authority,
idempotency, CAS registration + lineage, provenance, and settlement.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.db.models import Asset, GenerationJob, SpendCharge, SpendReservation
from app.domain.workers.rest_image_providers import IMAGE_SPECS
from app.domain.providers import catalog
from tests.conftest import make_project, wait_for_job


_KEYS = iter(range(20_000, 99_999))


def _png(path: Path, size=(64, 48), color=(90, 40, 200)) -> Path:
    im = Image.new("RGB", size, color)
    im.save(path, format="PNG")
    return path


def _rev(client, pid):
    return client.get(f"/api/projects/{pid}").json()["revision"]


def _commit(client, pid, command_type, payload, expect=200):
    r = client.post(
        f"/api/projects/{pid}/commands/commit",
        json={"command_type": command_type, "payload": payload,
              "scope": "project",
              "expected_revision": _rev(client, pid),
              "idempotency_key": f"c3d2-{next(_KEYS)}"})
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


def _preview(client, pid, command_type, payload, expect=200):
    r = client.post(
        f"/api/projects/{pid}/commands/preview",
        json={"command_type": command_type, "payload": payload,
              "scope": "project"})
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


def _import(client, pid, path: Path, name) -> dict:
    r = _commit(client, pid, "asset.import",
                {"source_path": str(path), "name": name, "kind": "image"})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded", job
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    return next(a for a in items if a["name"] == name)


def _sf(client):
    return client.app.state.session_factory


def _job_row(client, job_id):
    with _sf(client)() as s:
        return s.get(GenerationJob, job_id)


def _assets(client, pid):
    return client.get(f"/api/projects/{pid}/assets").json()["items"]


# gpt-image-1: img2img declared + implemented edits contract (16 refs,
# weights NOT implemented). flux-kontext: implemented, 1 ref, no weights.
# recraftv3: declares img2img + weights but provider has NO edits
# contract → every commit must refuse.
GOOD = {"provider_key": "openai-images", "model_id": "gpt-image-1",
        "execution_target": "cloud", "prompt": "make it a stormy night"}
KONTEXT = {"provider_key": "bfl-flux", "model_id": "flux-kontext",
           "execution_target": "cloud", "prompt": "turn day into night"}
RECRAFT = {"provider_key": "recraft", "model_id": "recraftv3",
           "execution_target": "cloud", "prompt": "restyle as watercolor"}


# =====================================================================
# Capability negotiation — catalog + implemented contract, fail closed
# =====================================================================

def test_contracts_endpoint_reports_img2img_subset(client):
    body = client.get("/api/providers/image-contracts").json()
    assert body["img2img_implemented"] == ["openai-images", "bfl-flux"]
    assert body["img2img_adapter_id"] == "cloud.image.img2img.sim"
    assert set(body["img2img_implemented"]) <= set(body["implemented"])


def test_img2img_happy_path_openai(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "src.png"), "src.png")
    r = _commit(client, p["id"], "generate.image.img2img",
                {**GOOD, "source_asset_id": src["id"]})
    job = wait_for_job(client, p["id"], r["job_id"])
    assert job["status"] == "succeeded", job
    assert job["adapter_id"] == "cloud.image.img2img.sim"
    assert job["output_asset_id"] and job["output_asset_id"] != src["id"]


def test_img2img_model_without_declared_capability_fails(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "s.png")
    # flux-pro-1.1 declares img2img=False → fail closed at preview.
    r = _preview(client, p["id"], "generate.image.img2img",
                 {"provider_key": "bfl-flux", "model_id": "flux-pro-1.1",
                  "execution_target": "cloud", "prompt": "edit",
                  "source_asset_id": src["id"]}, expect=422)
    assert "does not declare img2img" in r.text


def test_catalog_declaration_without_contract_fails(client, tmp_path):
    """recraftv3 DECLARES img2img in the catalog but recraft has no
    implemented edits contract (spec.edit_path is None) — the
    declaration alone must not enable the mode."""
    assert IMAGE_SPECS["recraft"].edit_path is None
    assert catalog.model_editing_caps("recraft", "recraftv3")["img2img"]
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "s.png")
    r = _preview(client, p["id"], "generate.image.img2img",
                 {**RECRAFT, "source_asset_id": src["id"]}, expect=422)
    assert "no img2img edits contract implemented" in r.text


def test_unimplemented_provider_refused(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "s.png")
    _preview(client, p["id"], "generate.image.img2img",
             {"provider_key": "gemini-images",
              "model_id": "gemini-2.5-flash-image",
              "execution_target": "cloud", "prompt": "edit",
              "source_asset_id": src["id"]}, expect=422)


def test_execution_target_must_be_cloud(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "s.png")
    _preview(client, p["id"], "generate.image.img2img",
             {**GOOD, "source_asset_id": src["id"],
              "execution_target": "local"}, expect=422)


# =====================================================================
# Source & reference validation, project isolation
# =====================================================================

def test_missing_source_rejected(client, tmp_path):
    p = make_project(client)
    r = _preview(client, p["id"], "generate.image.img2img",
                 {**GOOD, "source_asset_id": "asset-missing"}, expect=404)
    assert "not found" in r.text


def test_cross_project_source_rejected(client, tmp_path):
    p1, p2 = make_project(client), make_project(client, "Other")
    foreign = _import(client, p2["id"], _png(tmp_path / "f.png"), "f.png")
    _preview(client, p1["id"], "generate.image.img2img",
             {**GOOD, "source_asset_id": foreign["id"]}, expect=404)


def test_non_image_source_rejected(client, tmp_path):
    p = make_project(client)
    txt = tmp_path / "note.txt"
    txt.write_text("not an image")
    r = _commit(client, p["id"], "asset.import",
                {"source_path": str(txt), "name": "note.txt",
                 "kind": "document"})
    wait_for_job(client, p["id"], r["job_id"])
    doc = next(a for a in _assets(client, p["id"])
               if a["name"] == "note.txt")
    _preview(client, p["id"], "generate.image.img2img",
             {**GOOD, "source_asset_id": doc["id"]}, expect=422)


def test_corrupt_source_bytes_rejected_at_commit(client, tmp_path):
    """kind metadata is caller-declared — the byte-level safety gate
    must catch a kind=image asset whose payload is garbage."""
    p = make_project(client)
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"\x89PNG-not-really")
    r = _commit(client, p["id"], "asset.import",
                {"source_path": str(bad), "name": "bad.png",
                 "kind": "image"})
    job = wait_for_job(client, p["id"], r["job_id"])
    if job["status"] != "succeeded":
        pytest.skip("import pipeline already rejected corrupt bytes")
    bad_asset = next(a for a in _assets(client, p["id"])
                     if a["name"] == "bad.png")
    _commit(client, p["id"], "generate.image.img2img",
            {**GOOD, "source_asset_id": bad_asset["id"]}, expect=422)


def test_reference_count_limit_enforced(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "s.png")
    r1 = _import(client, p["id"], _png(tmp_path / "r1.png"), "r1.png")
    r2 = _import(client, p["id"], _png(tmp_path / "r2.png"), "r2.png")
    # flux-kontext allows exactly 1 reference.
    _preview(client, p["id"], "generate.image.img2img",
             {**KONTEXT, "source_asset_id": src["id"],
              "reference_asset_ids": [r1["id"], r2["id"]]}, expect=422)


def test_source_cannot_be_own_reference(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "s.png")
    _preview(client, p["id"], "generate.image.img2img",
             {**GOOD, "source_asset_id": src["id"],
              "reference_asset_ids": [src["id"]]}, expect=422)


def test_weights_rejected_where_unsupported(client, tmp_path):
    """gpt-image-1 implements img2img but not reference weights —
    supplying them must fail closed rather than be silently dropped."""
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "s.png")
    r1 = _import(client, p["id"], _png(tmp_path / "r1.png"), "r1.png")
    _preview(client, p["id"], "generate.image.img2img",
             {**GOOD, "source_asset_id": src["id"],
              "reference_asset_ids": [r1["id"]],
              "reference_weights": [1.5]}, expect=422)


def test_weights_count_must_match_refs(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "s.png")
    _preview(client, p["id"], "generate.image.img2img",
             {**GOOD, "source_asset_id": src["id"],
              "reference_weights": [1.0]}, expect=422)


def test_reference_image_success_and_lineage(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "s.png")
    ref = _import(client, p["id"], _png(tmp_path / "r.png"), "r.png")
    r = _commit(client, p["id"], "generate.image.img2img",
                {**GOOD, "source_asset_id": src["id"],
                 "reference_asset_ids": [ref["id"]]})
    job = wait_for_job(client, p["id"], r["job_id"])
    assert job["status"] == "succeeded", job
    out = next(a for a in _assets(client, p["id"])
               if a["id"] == job["output_asset_id"])
    media = out["media_json"]
    assert media["mode"] == "img2img"
    assert media["derived_from_asset_id"] == src["id"]
    assert media["derived_from_sha256"] == src["sha256"]
    assert media["reference_asset_ids"] == [ref["id"]]
    # Simulated provenance — never verified, always mock-labelled.
    assert media["is_mock"] is True
    assert media["generated"] is False
    assert media["source"] == "cloud-image-simulated"


def test_source_asset_never_modified(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "s.png")
    before = dict(src)
    r = _commit(client, p["id"], "generate.image.img2img",
                {**GOOD, "source_asset_id": src["id"]})
    job = wait_for_job(client, p["id"], r["job_id"])
    assert job["status"] == "succeeded", job
    after = next(a for a in _assets(client, p["id"]) if a["id"] == src["id"])
    assert after["sha256"] == before["sha256"]
    assert after["name"] == before["name"]


# =====================================================================
# Financial + durability
# =====================================================================

def test_cost_estimate_authoritative_and_settled(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "s.png")
    prop = _preview(client, p["id"], "generate.image.img2img",
                    {**GOOD, "source_asset_id": src["id"]})
    est = prop["cost_estimate"]["est_cost_usd"]
    assert est is not None and est > 0
    r = _commit(client, p["id"], "generate.image.img2img",
                {**GOOD, "source_asset_id": src["id"]})
    job = wait_for_job(client, p["id"], r["job_id"])
    assert job["status"] == "succeeded", job
    with _sf(client)() as s:
        charge = s.execute(select(SpendCharge).where(
            SpendCharge.job_id == job["id"])).scalars().all()
    assert charge, "img2img settle must record a synthetic charge"
    assert all(c.environment == "simulated" for c in charge)


def test_budget_refusal_fail_closed(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "s.png")
    _commit(client, p["id"], "spend.budget_set",
            {"target": "global", "scope": "lifetime", "limit_usd": "0.001"})
    _commit(client, p["id"], "generate.image.img2img",
            {**GOOD, "source_asset_id": src["id"]}, expect=422)


def test_idempotent_commit_reuses_job(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "s.png")
    payload = {**GOOD, "source_asset_id": src["id"]}
    key = f"c3d2-idem-{next(_KEYS)}"
    body = {"command_type": "generate.image.img2img", "payload": payload,
            "scope": "project", "expected_revision": _rev(client, p["id"]),
            "idempotency_key": key}
    r1 = client.post(f"/api/projects/{p['id']}/commands/commit", json=body)
    assert r1.status_code == 200, r1.text
    job1 = r1.json()["job_id"]
    body["expected_revision"] = _rev(client, p["id"])
    r2 = client.post(f"/api/projects/{p['id']}/commands/commit", json=body)
    assert r2.status_code == 200, r2.text
    assert r2.json().get("idempotent_replay") is True
    # Exactly one durable img2img job exists — no duplicate submission.
    with _sf(client)() as s:
        rows = s.execute(select(GenerationJob).where(
            GenerationJob.idempotency_key == f"job:{key}")).scalars().all()
    assert [r.id for r in rows] == [job1]


def test_job_row_is_durable_and_simulated(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "s.png")
    r = _commit(client, p["id"], "generate.image.img2img",
                {**GOOD, "source_asset_id": src["id"]})
    job = wait_for_job(client, p["id"], r["job_id"])
    row = _job_row(client, job["id"])
    assert row is not None
    assert row.request_json["mode"] == "img2img"
    assert row.request_json["simulated"] is True
    assert row.request_json["source_asset_id"] == src["id"]
    assert row.request_json["source_sha256"] == src["sha256"]
    assert row.request_json["execution_target"] == "cloud"
    assert row.adapter_id == "cloud.image.img2img.sim"
    # Idempotency key + reservation persisted for recovery/reconcile.
    assert row.idempotency_key.startswith("job:")
    with _sf(client)() as s:
        res = s.execute(select(SpendReservation).where(
            SpendReservation.job_id == job["id"])).scalar_one_or_none()
    assert res is not None


def test_preview_shows_simulated_warnings(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "s.png")
    prop = _preview(client, p["id"], "generate.image.img2img",
                    {**GOOD, "source_asset_id": src["id"]})
    txt = " ".join(prop["warnings"]).lower()
    assert "simulated" in txt
    assert "source image bytes" in txt
    assert "estimate" in txt


def test_prompt_and_payload_validation(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "s.png")
    _preview(client, p["id"], "generate.image.img2img",
             {**GOOD, "prompt": "  ", "source_asset_id": src["id"]},
             expect=422)
    _preview(client, p["id"], "generate.image.img2img",
             {**GOOD, "prompt": "x" * 8001, "source_asset_id": src["id"]},
             expect=422)
    _preview(client, p["id"], "generate.image.img2img",
             {**GOOD, "source_asset_id": src["id"], "count": 2},
             expect=422)
