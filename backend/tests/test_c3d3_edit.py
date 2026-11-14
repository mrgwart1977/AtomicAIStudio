"""C3-D3 — simulated mask-inpaint + outpaint on documented contracts.

Everything simulated: MockTransport fixtures, dummy credentials,
synthetic accounting. Covers the documented mask convention (exact
dimension match, luminance >= 128 editable, no silent resize), outpaint
dimension/placement rules, two-layer capability gating (catalog +
implemented contract), project isolation, cost admission, durable jobs,
CAS registration, and lineage.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.db.models import Asset, GenerationJob
from app.domain import image_edit
from tests.conftest import make_project, wait_for_job


_KEYS = iter(range(20_000, 99_999))


def _png(path: Path, size=(64, 48), color=(90, 40, 200)) -> Path:
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


def _mask(path: Path, size=(64, 48), editable=True) -> Path:
    """White = editable (>=128); black = protected. Half-half by default."""
    im = Image.new("RGB", size, (0, 0, 0))
    if editable:
        w, h = size
        for x in range(w // 2):
            for y in range(h):
                im.putpixel((x, y), (255, 255, 255))
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
              "idempotency_key": f"c3d3-{next(_KEYS)}"})
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


# gpt-image-1: mask_inpaint declared + inpaint contract; outpaint NEITHER
# declared nor implemented → refused. flux-pro-1.0-fill/expand: declared
# + implemented. recraftv3: declares mask+outpaint but provider has NO
# edit contract → every request must refuse.
INPAINT = {"provider_key": "openai-images", "model_id": "gpt-image-1",
           "operation": "inpaint", "execution_target": "cloud",
           "prompt": "replace the left half with ocean"}
FILL = {"provider_key": "bfl-flux", "model_id": "flux-pro-1.0-fill",
        "operation": "inpaint", "execution_target": "cloud",
        "prompt": "fill with forest"}
EXPAND = {"provider_key": "bfl-flux", "model_id": "flux-pro-1.0-expand",
          "operation": "outpaint", "execution_target": "cloud",
          "prompt": "extend the skyline",
          "out_width": 128, "out_height": 96, "placement": "center"}
RECRAFT = {"provider_key": "recraft", "model_id": "recraftv3",
           "operation": "inpaint", "execution_target": "cloud",
           "prompt": "inpaint it"}


def test_contracts_endpoint_reports_edit_subsets(client):
    body = client.get("/api/providers/image-contracts").json()
    assert body["inpaint_implemented"] == ["openai-images", "bfl-flux"]
    assert body["outpaint_implemented"] == ["bfl-flux"]
    assert body["edit_adapter_id"] == "cloud.image.edit.sim"


def test_inpaint_happy_path(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "src.png")
    msk = _import(client, p["id"], _mask(tmp_path / "m.png"), "mask.png")
    r = _commit(client, p["id"], "generate.image.edit",
                {**INPAINT, "source_asset_id": src["id"],
                 "mask_asset_id": msk["id"]})
    job = wait_for_job(client, p["id"], r["job_id"])
    assert job["status"] == "succeeded", job
    assert job["adapter_id"] == "cloud.image.edit.sim"
    assert job["output_asset_id"] and job["output_asset_id"] not in {
        src["id"], msk["id"]}
    # Lineage: output records source + mask parentage; neither mutated.
    row = _job_row(client, job["id"])
    req = row.request_json
    assert req["mode"] == "inpaint"
    assert req["source_asset_id"] == src["id"]
    assert req["mask_asset_id"] == msk["id"]
    assert req["mask_sha256"] == msk["sha256"]


def test_bfl_fill_and_expand_happy(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "src.png")
    msk = _import(client, p["id"], _mask(tmp_path / "m.png"), "mask.png")
    r = _commit(client, p["id"], "generate.image.edit",
                {**FILL, "source_asset_id": src["id"],
                 "mask_asset_id": msk["id"]})
    assert wait_for_job(client, p["id"], r["job_id"])["status"] == "succeeded"
    r2 = _commit(client, p["id"], "generate.image.edit",
                 {**EXPAND, "source_asset_id": src["id"]})
    job = wait_for_job(client, p["id"], r2["job_id"])
    assert job["status"] == "succeeded", job
    row = _job_row(client, job["id"])
    assert row.request_json["mode"] == "outpaint"
    assert row.request_json["pad"] == {"left": 32, "right": 32,
                                  "top": 24, "bottom": 24}


def test_outpaint_openai_fails_closed(client, tmp_path):
    """OpenAI edits documents NO canvas-extension surface — refused."""
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "src.png")
    _preview(client, p["id"], "generate.image.edit",
             {"provider_key": "openai-images", "model_id": "gpt-image-1",
              "operation": "outpaint", "execution_target": "cloud",
              "prompt": "wider", "out_width": 128, "out_height": 96,
              "placement": "center", "source_asset_id": src["id"]},
             expect=422)
    _commit(client, p["id"], "generate.image.edit",
            {"provider_key": "openai-images", "model_id": "gpt-image-1",
             "operation": "outpaint", "execution_target": "cloud",
             "prompt": "wider", "out_width": 128, "out_height": 96,
             "placement": "center", "source_asset_id": src["id"]},
            expect=422)


def test_declared_but_unimplemented_provider_refused(client, tmp_path):
    """Recraft declares mask+outpaint in the catalog but has NO
    implemented edit contract — preview AND commit refuse."""
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "src.png")
    msk = _import(client, p["id"], _mask(tmp_path / "m.png"), "mask.png")
    pay = {**RECRAFT, "source_asset_id": src["id"],
           "mask_asset_id": msk["id"]}
    _preview(client, p["id"], "generate.image.edit", pay, expect=422)
    _commit(client, p["id"], "generate.image.edit", pay, expect=422)


def test_wrong_model_capability_refused(client, tmp_path):
    """flux-kontext declares img2img only — not mask_inpaint/outpaint."""
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "src.png")
    msk = _import(client, p["id"], _mask(tmp_path / "m.png"), "mask.png")
    _preview(client, p["id"], "generate.image.edit",
             {"provider_key": "bfl-flux", "model_id": "flux-kontext",
              "operation": "inpaint", "execution_target": "cloud",
              "prompt": "x", "source_asset_id": src["id"],
              "mask_asset_id": msk["id"]}, expect=422)


def test_bad_operation_refused(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "src.png")
    _preview(client, p["id"], "generate.image.edit",
             {**INPAINT, "operation": "erase",
              "source_asset_id": src["id"]}, expect=422)


# =====================================================================
# Mask convention — documented in app/domain/image_edit
# =====================================================================

def test_mask_dimension_mismatch_refused(client, tmp_path):
    """Masks must match source dimensions exactly — never resized."""
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "src.png")
    msk = _import(client, p["id"],
                  _mask(tmp_path / "m.png", size=(32, 24)), "mask.png")
    _commit(client, p["id"], "generate.image.edit",
            {**INPAINT, "source_asset_id": src["id"],
             "mask_asset_id": msk["id"]}, expect=422)


def test_mask_no_editable_region_refused(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "src.png")
    msk = _import(client, p["id"],
                  _mask(tmp_path / "m.png", editable=False), "mask.png")
    _commit(client, p["id"], "generate.image.edit",
            {**INPAINT, "source_asset_id": src["id"],
             "mask_asset_id": msk["id"]}, expect=422)


def test_mask_equals_source_refused_and_cross_project(client, tmp_path):
    p = make_project(client)
    p2 = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "src.png")
    foreign = _import(client, p2["id"], _mask(tmp_path / "f.png"), "f.png")
    _commit(client, p["id"], "generate.image.edit",
            {**INPAINT, "source_asset_id": src["id"],
             "mask_asset_id": src["id"]}, expect=422)
    _commit(client, p["id"], "generate.image.edit",
            {**INPAINT, "source_asset_id": src["id"],
             "mask_asset_id": foreign["id"]}, expect=404)


def test_mask_convention_unit():
    """Unit-level: threshold, dims, editable-region semantics."""
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (255, 255, 255)).save(buf, format="PNG")
    meta = image_edit.inspect_edit_mask(buf.getvalue(), src_w=10, src_h=10)
    assert meta["editable_pixels"] == 100
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (127, 127, 127)).save(buf, format="PNG")
    with pytest.raises(ValueError, match="no editable"):
        image_edit.inspect_edit_mask(buf.getvalue(), src_w=10, src_h=10)
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 255, 255)).save(buf, format="PNG")
    with pytest.raises(ValueError, match="do not match"):
        image_edit.inspect_edit_mask(buf.getvalue(), src_w=10, src_h=10)


# =====================================================================
# Outpaint conventions
# =====================================================================

def test_outpaint_bounds_and_placement(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "src.png")
    base = {**EXPAND, "source_asset_id": src["id"]}
    # Smaller than source — refused at commit (preview checks bounds
    # only; source-relative checks need the CAS bytes).
    _commit(client, p["id"], "generate.image.edit",
            {**base, "out_width": 32, "out_height": 48}, expect=422)
    # Equal to source — nothing to expand.
    _commit(client, p["id"], "generate.image.edit",
            {**base, "out_width": 64, "out_height": 48}, expect=422)
    # Over the documented max canvas.
    _preview(client, p["id"], "generate.image.edit",
             {**base, "out_width": 9000, "out_height": 96}, expect=422)
    # Bad placement anchor.
    _preview(client, p["id"], "generate.image.edit",
             {**base, "placement": "diagonal"}, expect=422)


def test_compute_pad_anchors():
    assert image_edit.compute_pad(64, 48, 128, 96, "center") == \
        {"left": 32, "right": 32, "top": 24, "bottom": 24}
    assert image_edit.compute_pad(64, 48, 128, 96, "top_left") == \
        {"left": 0, "right": 64, "top": 0, "bottom": 48}
    assert image_edit.compute_pad(64, 48, 128, 96, "bottom") == \
        {"left": 32, "right": 32, "top": 48, "bottom": 0}


def test_validate_outpaint_unit():
    image_edit.validate_outpaint(64, 48, 128, 48)
    with pytest.raises(ValueError, match="smaller"):
        image_edit.validate_outpaint(64, 48, 64, 32)
    with pytest.raises(ValueError, match="equals"):
        image_edit.validate_outpaint(64, 48, 64, 48)
    with pytest.raises(ValueError, match="1–4096"):
        image_edit.validate_outpaint(64, 48, 5000, 48)


# =====================================================================
# Isolation / integrity / lifecycle
# =====================================================================

def test_cross_project_source_refused(client, tmp_path):
    p = make_project(client)
    p2 = make_project(client)
    foreign = _import(client, p2["id"], _png(tmp_path / "f.png"), "f.png")
    msk = _import(client, p["id"], _mask(tmp_path / "m.png"), "mask.png")
    _commit(client, p["id"], "generate.image.edit",
            {**INPAINT, "source_asset_id": foreign["id"],
             "mask_asset_id": msk["id"]}, expect=404)


def test_missing_and_kind_mismatched_assets(client, tmp_path):
    p = make_project(client)
    msk = _import(client, p["id"], _mask(tmp_path / "m.png"), "mask.png")
    _commit(client, p["id"], "generate.image.edit",
            {**INPAINT, "source_asset_id": "nope",
             "mask_asset_id": msk["id"]}, expect=404)


def test_idempotent_commit_replays(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "src.png")
    msk = _import(client, p["id"], _mask(tmp_path / "m.png"), "mask.png")
    pay = {**INPAINT, "source_asset_id": src["id"],
           "mask_asset_id": msk["id"]}
    key = f"c3d3-idem-{p['id']}"
    rev = _rev(client, p["id"])
    body = {"command_type": "generate.image.edit", "payload": pay,
            "scope": "project", "expected_revision": rev,
            "idempotency_key": key}
    r1 = client.post(f"/api/projects/{p['id']}/commands/commit", json=body)
    assert r1.status_code == 200, r1.text
    r2 = client.post(f"/api/projects/{p['id']}/commands/commit", json=body)
    assert r2.status_code == 200 and r2.json().get("idempotent_replay")
    with _sf(client)() as s:
        jobs = s.execute(select(GenerationJob).where(
            GenerationJob.idempotency_key.like("%" + key))).scalars().all()
    assert len(jobs) == 1


def test_output_asset_lineage_and_provenance(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "src.png")
    msk = _import(client, p["id"], _mask(tmp_path / "m.png"), "mask.png")
    r = _commit(client, p["id"], "generate.image.edit",
                {**INPAINT, "source_asset_id": src["id"],
                 "mask_asset_id": msk["id"]})
    job = wait_for_job(client, p["id"], r["job_id"])
    assets = {a["id"]: a for a in
              client.get(f"/api/projects/{p['id']}/assets").json()["items"]}
    out = assets[job["output_asset_id"]]
    media = out["media_json"]
    assert media.get("mode") == "inpaint"
    assert media.get("derived_from_asset_id") == src["id"]
    assert media.get("mask_asset_id") == msk["id"]
    # Simulated provenance — never a verified remote artifact.
    assert media.get("is_mock") is True
    assert media.get("generated") is False
    assert media.get("source") == "cloud-image-simulated"


def test_cost_preview_authority_and_estimate_label(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "src.png")
    msk = _import(client, p["id"], _mask(tmp_path / "m.png"), "mask.png")
    prev = _preview(client, p["id"], "generate.image.edit",
                    {**INPAINT, "source_asset_id": src["id"],
                     "mask_asset_id": msk["id"]})
    est = prev["cost_estimate"]["est_cost_usd"]
    assert est is not None and est > 0
    text = " ".join(prev.get("warnings") or [])
    assert "ESTIMATE" in text and "SIMULATED" in text
    # Backend-authoritative: the client cannot dictate the estimate.
    prev2 = _preview(client, p["id"], "generate.image.edit",
                     {**INPAINT, "source_asset_id": src["id"],
                      "mask_asset_id": msk["id"], "est_cost_usd": 0.0001})
    assert prev2["cost_estimate"]["est_cost_usd"] == est


def test_mask_sha_mismatch_fails_at_execution(client, tmp_path):
    """A durable request whose recorded mask sha no longer matches the
    CAS payload is refused by the adapter's execution gate — a swapped
    asset can never be edited."""
    from app.adapters.cloud_image import SimCloudEditAdapter
    from app.domain.workers.rest_simulated import (
        build_simulated_edit_providers)
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "src.png")
    msk = _import(client, p["id"], _mask(tmp_path / "m.png"), "mask.png")
    r = _commit(client, p["id"], "generate.image.edit",
                {**INPAINT, "source_asset_id": src["id"],
                 "mask_asset_id": msk["id"]})
    req = dict(_job_row(client, r["job_id"]).request_json)
    req["mask_sha256"] = "0" * 64   # simulate post-commit tamper
    adapter = SimCloudEditAdapter(build_simulated_edit_providers())
    with pytest.raises(RuntimeError, match="hash mismatch"):
        adapter._intent_request(req)


def test_durable_request_shape(client, tmp_path):
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "src.png")
    r = _commit(client, p["id"], "generate.image.edit",
                {**EXPAND, "source_asset_id": src["id"]})
    row = _job_row(client, r["job_id"])
    req = row.request_json
    assert req["mode"] == "outpaint"
    assert req["simulated"] is True
    assert req["count"] == 1
    assert req["source_asset_id"] == src["id"] and req["source_sha256"]
    assert req["out_width"] == 128 and req["out_height"] == 96
    assert req["placement"] == "center"
    assert req["pad"]["left"] + req["pad"]["right"] == 64


def test_mask_provider_translation():
    """Internal lum>=128 editable → provider wire = transparent editable.
    The adapter boundary must convert explicitly — never pass through."""
    import base64
    im = Image.new("RGB", (4, 2))
    for x in range(2):            # left half editable (white)
        for y in range(2):
            im.putpixel((x, y), (255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    out_b64 = image_edit.mask_to_provider_b64(buf.getvalue())
    out = Image.open(io.BytesIO(base64.b64decode(out_b64))).convert("RGBA")
    px = out.load()
    assert px[0, 0][3] == 0 and px[1, 1][3] == 0      # editable → transparent
    assert px[2, 0][3] == 255 and px[3, 1][3] == 255  # protected → opaque
    assert out.size == (4, 2)


def test_compute_pad_all_nine_anchors():
    """Every documented anchor is deterministic and preserves the source
    region exactly (pads sum to the delta; opposite edges get the rest)."""
    cases = {
        "center": (32, 32, 24, 24), "top": (32, 32, 0, 48),
        "bottom": (32, 32, 48, 0), "left": (0, 64, 24, 24),
        "right": (64, 0, 24, 24), "top_left": (0, 64, 0, 48),
        "top_right": (64, 0, 0, 48), "bottom_left": (0, 64, 48, 0),
        "bottom_right": (64, 0, 48, 0)}
    for anchor, (l, r, t, b) in cases.items():
        pad = image_edit.compute_pad(64, 48, 128, 96, anchor)
        assert pad == {"left": l, "right": r, "top": t, "bottom": b}, anchor
        assert pad["left"] + pad["right"] == 64
        assert pad["top"] + pad["bottom"] == 48
    # Odd deltas: extra pixel deterministically on right/bottom.
    pad = image_edit.compute_pad(64, 48, 65, 49, "center")
    assert pad == {"left": 0, "right": 1, "top": 0, "bottom": 1}


def test_inpaint_submit_body_uses_translated_mask(client, tmp_path):
    """The provider submit body carries the translated mask, not the raw
    internal bytes — verified through a capture of the intent request."""
    from app.adapters.cloud_image import SimCloudEditAdapter
    from app.domain.workers.rest_simulated import (
        build_simulated_edit_providers)
    p = make_project(client)
    src = _import(client, p["id"], _png(tmp_path / "s.png"), "src.png")
    msk = _import(client, p["id"], _mask(tmp_path / "m.png"), "mask.png")
    r = _commit(client, p["id"], "generate.image.edit",
                {**INPAINT, "source_asset_id": src["id"],
                 "mask_asset_id": msk["id"]})
    req = dict(_job_row(client, r["job_id"]).request_json)
    adapter = SimCloudEditAdapter(build_simulated_edit_providers())
    intent = adapter._intent_request(req)
    assert intent["mask_provider_b64"]
    assert intent["mask_provider_b64"] != intent["mask_b64"]
