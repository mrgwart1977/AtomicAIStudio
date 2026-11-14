"""C3-D1 — image reference foundation.

Provider editing-capability declarations (fail-closed catalog
metadata), character reference-image links with ordering + optional
weights, image safety at the link boundary, and provenance/isolation
regressions. All simulated — no provider calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app.domain import image_safety
from app.domain.providers import catalog
from tests.conftest import make_project, wait_for_job


def _png(path: Path, size=(12, 10), color=(40, 120, 200)) -> Path:
    Image.new("RGB", size, color).save(path)
    return path


def _commit(client, pid, command_type, payload, key, expect=200):
    rev = client.get(f"/api/projects/{pid}").json()["revision"]
    r = client.post(
        f"/api/projects/{pid}/commands/commit",
        json={
            "command_type": command_type,
            "payload": payload,
            "scope": "project",
            "origin": "test",
            "expected_revision": rev,
            "idempotency_key": key if len(key) >= 8 else key + "-pad-0000",
        },
    )
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


def _char(client, pid, name, key):
    _commit(client, pid, "character.create", {"name": name}, key)
    chars = client.get(f"/api/projects/{pid}/character").json()
    return next(c for c in chars if c["name"] == name)


def _import(client, pid, path: Path, name=None, kind=None) -> dict:
    import time
    rev = client.get(f"/api/projects/{pid}").json()["revision"]
    r = client.post(
        f"/api/projects/{pid}/commands/commit",
        json={
            "command_type": "asset.import",
            "payload": {"source_path": str(path),
                        **({"name": name} if name else {}),
                        **({"kind": kind} if kind else {})},
            "scope": "project",
            "expected_revision": rev,
            "idempotency_key": f"imp-{path.name}-{rev}-{time.time_ns()}",
        },
    )
    assert r.status_code == 200, r.text
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "succeeded", job
    assets = client.get(f"/api/projects/{pid}/assets").json()["items"]
    return next(a for a in assets if a["name"] == (name or path.name))


def _links(client, pid, entity_id, etype="character"):
    r = client.get(
        f"/api/projects/{pid}/entity-assets"
        f"?entity_type={etype}&entity_id={entity_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _link(client, pid, cid, asset_id, key, expect=200, **kw):
    return _commit(
        client, pid, "entity.assets.link",
        {"entity_type": "character", "entity_id": cid,
         "asset_id": asset_id, **kw}, key, expect)


# =====================================================================
# Capability declarations
# =====================================================================

class TestEditingCapabilities:
    def test_catalog_valid_and_every_image_model_declared(self):
        assert catalog.validate_catalog() == []
        for p in catalog.PROVIDERS:
            for m in p.get("models") or []:
                if (m.get("capability") or "").startswith("image"):
                    assert isinstance(m.get("editing"), dict), \
                        f"{p['key']}/{m.get('id')} missing editing block"

    def test_declared_caps_surface(self):
        c = catalog.model_editing_caps("openai-images", "gpt-image-1")
        assert c["img2img"] is True
        assert c["max_reference_images"] >= 1
        assert c["mask_inpaint"] is True
        assert c["identity_reference"] is False  # never a guarantee claim

    def test_generation_only_models_fail_closed(self):
        c = catalog.model_editing_caps("bfl-flux", "flux-pro-1.1")
        assert c["img2img"] is False
        assert c["max_reference_images"] == 0
        assert c["mask_inpaint"] is False
        assert c["outpaint"] is False

    def test_unknown_model_or_provider_fails_closed(self):
        for caps in (catalog.model_editing_caps("bfl-flux", "no-such"),
                     catalog.model_editing_caps("no-provider", "m"),
                     catalog.editing_caps(None),
                     catalog.editing_caps({"id": "x"})):
            assert caps == dict(catalog.EDITING_DEFAULTS)
            assert caps["img2img"] is False

    def test_no_identity_guarantee_claims(self):
        """Accepting references must never be declared as an identity
        guarantee anywhere in the catalog."""
        for p in catalog.PROVIDERS:
            for m in p.get("models") or []:
                ed = (m.get("editing") or {})
                assert ed.get("identity_reference") in (None, False), \
                    f"{p['key']}/{m.get('id')} claims identity_reference"


# =====================================================================
# Reference links: add / order / weight / update / remove
# =====================================================================

class TestReferenceLinks:
    def test_link_stores_order_and_weight(self, client, tmp_path):
        p = make_project(client, "Order")
        c = _char(client, p["id"], "Ada", "o-1")
        a1 = _import(client, p["id"], _png(tmp_path / "a.png"), kind="image")
        a2 = _import(client, p["id"], _png(tmp_path / "b.png", color=(9, 9, 9)),
                     kind="image")
        _link(client, p["id"], c["id"], a1["id"], "o-2",
              role="reference", weight=0.75, label="first")
        _link(client, p["id"], c["id"], a2["id"], "o-3", role="reference")
        links = _links(client, p["id"], c["id"])
        assert [l["position"] for l in links] == [0, 1]
        assert links[0]["weight"] == pytest.approx(0.75)
        assert links[1]["weight"] is None  # provider default

    def test_reorder_requires_complete_permutation(self, client, tmp_path):
        p = make_project(client, "Perm")
        c = _char(client, p["id"], "Ada", "p-1")
        ids = [_import(client, p["id"], _png(tmp_path / f"{n}.png"),
                       kind="image")["id"] for n in "abc"]
        for i, aid in enumerate(ids):
            _link(client, p["id"], c["id"], aid, f"p-{i + 2}",
                  role="reference")
        links = _links(client, p["id"], c["id"])
        rev_ids = [l["id"] for l in links][::-1]
        _commit(client, p["id"], "entity.assets.reorder",
                {"entity_type": "character", "entity_id": c["id"],
                 "ordered_link_ids": rev_ids}, "p-5")
        after = _links(client, p["id"], c["id"])
        assert [l["id"] for l in after] == rev_ids
        assert [l["position"] for l in after] == [0, 1, 2]

        # Partial order → 422 (rest of the set would be ambiguous).
        _commit(client, p["id"], "entity.assets.reorder",
                {"entity_type": "character", "entity_id": c["id"],
                 "ordered_link_ids": rev_ids[:2]}, "p-6", expect=422)
        # Duplicate ids → 422.
        _commit(client, p["id"], "entity.assets.reorder",
                {"entity_type": "character", "entity_id": c["id"],
                 "ordered_link_ids": [rev_ids[0]] * 3}, "p-7", expect=422)

    def test_reorder_undo_restores_positions(self, client, tmp_path):
        p = make_project(client, "Undo")
        c = _char(client, p["id"], "Ada", "ur-1")
        ids = [_import(client, p["id"], _png(tmp_path / f"{n}.png"),
                       kind="image")["id"] for n in "ab"]
        _link(client, p["id"], c["id"], ids[0], "ur-2", role="reference")
        _link(client, p["id"], c["id"], ids[1], "ur-3", role="reference")
        before = [l["id"] for l in _links(client, p["id"], c["id"])]
        res = _commit(client, p["id"], "entity.assets.reorder",
                      {"entity_type": "character", "entity_id": c["id"],
                       "ordered_link_ids": before[::-1]}, "ur-4")
        assert [l["id"] for l in _links(client, p["id"], c["id"])] == before[::-1]
        r = client.post(
            f"/api/projects/{p['id']}/commands/{res['command_id']}/undo")
        assert r.status_code == 200, r.text
        assert [l["id"] for l in _links(client, p["id"], c["id"])] == before

    def test_update_and_unlink_undo_restore_weight(self, client, tmp_path):
        p = make_project(client, "UndoW")
        c = _char(client, p["id"], "Ada", "uw-1")
        a = _import(client, p["id"], _png(tmp_path / "a.png"), kind="image")
        _link(client, p["id"], c["id"], a["id"], "uw-2",
              role="reference", weight=1.0, label="kept")
        link = _links(client, p["id"], c["id"])[0]

        res = _commit(client, p["id"], "entity.assets.update",
                      {"link_id": link["id"], "weight": 0.2,
                       "label": "faded"}, "uw-3")
        r = client.post(
            f"/api/projects/{p['id']}/commands/{res['command_id']}/undo")
        assert r.status_code == 200, r.text
        back = _links(client, p["id"], c["id"])[0]
        assert back["weight"] == pytest.approx(1.0)
        assert back["label"] == "kept"

        res = _commit(client, p["id"], "entity.assets.unlink",
                      {"link_id": link["id"]}, "uw-4")
        r = client.post(
            f"/api/projects/{p['id']}/commands/{res['command_id']}/undo")
        assert r.status_code == 200, r.text
        restored = _links(client, p["id"], c["id"])
        assert len(restored) == 1
        assert restored[0]["weight"] == pytest.approx(1.0)

    def test_update_weight_and_label(self, client, tmp_path):
        p = make_project(client, "Upd")
        c = _char(client, p["id"], "Ada", "u-1")
        a = _import(client, p["id"], _png(tmp_path / "a.png"), kind="image")
        _link(client, p["id"], c["id"], a["id"], "u-2", role="reference")
        link = _links(client, p["id"], c["id"])[0]

        _commit(client, p["id"], "entity.assets.update",
                {"link_id": link["id"], "weight": 1.5,
                 "label": "strong ref"}, "u-3")
        link = _links(client, p["id"], c["id"])[0]
        assert link["weight"] == pytest.approx(1.5)
        assert link["label"] == "strong ref"

        # Explicit null clears the weight (back to provider default).
        _commit(client, p["id"], "entity.assets.update",
                {"link_id": link["id"], "weight": None}, "u-4")
        assert _links(client, p["id"], c["id"])[0]["weight"] is None

        for bad in (True, "high", 2.5, -0.1):
            _commit(client, p["id"], "entity.assets.update",
                    {"link_id": link["id"], "weight": bad}, "u-5",
                    expect=422)

    def test_unlink_preserves_asset(self, client, tmp_path):
        p = make_project(client, "Unlink")
        c = _char(client, p["id"], "Ada", "r-1")
        a = _import(client, p["id"], _png(tmp_path / "a.png"), kind="image")
        _link(client, p["id"], c["id"], a["id"], "r-2", role="reference")
        link = _links(client, p["id"], c["id"])[0]

        _commit(client, p["id"], "entity.assets.unlink",
                {"link_id": link["id"]}, "r-3")
        assert _links(client, p["id"], c["id"]) == []
        # The underlying asset survives — removal detaches, never deletes.
        assets = client.get(f"/api/projects/{p['id']}/assets").json()["items"]
        assert any(x["id"] == a["id"] for x in assets)

    def test_duplicate_link_is_idempotent_and_updates_weight(
            self, client, tmp_path):
        p = make_project(client, "Dup")
        c = _char(client, p["id"], "Ada", "d-1")
        a = _import(client, p["id"], _png(tmp_path / "a.png"), kind="image")
        _link(client, p["id"], c["id"], a["id"], "d-2",
              role="reference", weight=0.3)
        _link(client, p["id"], c["id"], a["id"], "d-3",
              role="reference", weight=0.9)
        links = _links(client, p["id"], c["id"])
        assert len(links) == 1  # same (entity, asset, role) → one row
        assert links[0]["weight"] == pytest.approx(0.9)
        # Same asset under a different role is a distinct link.
        _link(client, p["id"], c["id"], a["id"], "d-4", role="identity")
        assert len(_links(client, p["id"], c["id"])) == 2

    def test_weight_validation_on_link(self, client, tmp_path):
        p = make_project(client, "WVal")
        c = _char(client, p["id"], "Ada", "w-1")
        a = _import(client, p["id"], _png(tmp_path / "a.png"), kind="image")
        for bad in ("high", True, -0.5, 2.1):
            _link(client, p["id"], c["id"], a["id"], "w-2",
                  role="reference", weight=bad, expect=422)
        _link(client, p["id"], c["id"], a["id"], "w-3",
              role="reference", weight=2.0)
        assert _links(client, p["id"], c["id"])[0]["weight"] == 2.0


# =====================================================================
# Image safety at the link boundary
# =====================================================================

class TestImageSafety:
    def test_visual_role_requires_image_kind(self, client, tmp_path):
        p = make_project(client, "Kinds")
        c = _char(client, p["id"], "Ada", "k-1")
        f = tmp_path / "notes.txt"
        f.write_text("not an image")
        a = _import(client, p["id"], f)  # kind defaults to 'other'
        _link(client, p["id"], c["id"], a["id"], "k-2",
              role="reference", expect=422)
        # A non-visual role is unconstrained by the image gate.
        _link(client, p["id"], c["id"], a["id"], "k-3", role="voice")
        assert len(_links(client, p["id"], c["id"])) == 1

    def test_corrupt_bytes_rejected_at_commit(self, client, tmp_path):
        """kind='image' is caller-declared metadata — the commit gate
        still decodes the actual bytes and refuses garbage."""
        p = make_project(client, "Corrupt")
        c = _char(client, p["id"], "Ada", "c-1")
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"\x89PNG" + b"\x00" * 512)
        a = _import(client, p["id"], bad, kind="image")
        _link(client, p["id"], c["id"], a["id"], "c-2",
              role="reference", expect=422)
        assert _links(client, p["id"], c["id"]) == []

    def test_oversize_rejected(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(image_safety, "MAX_IMAGE_BYTES", 64)
        p = make_project(client, "Big")
        c = _char(client, p["id"], "Ada", "b-1")
        big = tmp_path / "big.png"
        big.write_bytes(b"\x89PNG" + b"\x00" * 128)
        a = _import(client, p["id"], big, kind="image")
        _link(client, p["id"], c["id"], a["id"], "b-2",
              role="reference", expect=422)


# =====================================================================
# Isolation & provenance
# =====================================================================

class TestIsolationAndProvenance:
    def test_cross_project_ids_refused(self, client, tmp_path):
        pa = make_project(client, "ProjA")
        pb = make_project(client, "ProjB")
        ca = _char(client, pa["id"], "Ada", "i-1")
        cb = _char(client, pb["id"], "Bob", "i-2")
        asset_b = _import(client, pb["id"], _png(tmp_path / "b.png"),
                          kind="image")
        # Another project's asset cannot be linked here.
        _link(client, pa["id"], ca["id"], asset_b["id"], "i-3",
              role="reference", expect=404)
        _link(client, pb["id"], cb["id"], asset_b["id"], "i-4",
              role="reference")
        link_b = _links(client, pb["id"], cb["id"])[0]
        # Nor its link rows touched from the other project.
        _commit(client, pa["id"], "entity.assets.unlink",
                {"link_id": link_b["id"]}, "i-5", expect=404)
        _commit(client, pa["id"], "entity.assets.update",
                {"link_id": link_b["id"], "weight": 1.0}, "i-6", expect=404)
        _commit(client, pa["id"], "entity.assets.reorder",
                {"entity_type": "character", "entity_id": ca["id"],
                 "ordered_link_ids": [link_b["id"]]}, "i-7", expect=422)
        # Nor can a foreign entity receive a link.
        _link(client, pa["id"], cb["id"],
              _import(client, pa["id"], _png(tmp_path / "a.png"),
                      kind="image")["id"],
              "i-8", role="reference", expect=404)

    def test_link_leaves_asset_provenance_intact(self, client, tmp_path):
        p = make_project(client, "Prov")
        c = _char(client, p["id"], "Ada", "pr-1")
        a = _import(client, p["id"], _png(tmp_path / "a.png"), kind="image")
        before = next(x for x in client.get(
            f"/api/projects/{p['id']}/assets").json()["items"]
            if x["id"] == a["id"])
        _link(client, p["id"], c["id"], a["id"], "pr-2",
              role="reference", weight=1.0)
        after = next(x for x in client.get(
            f"/api/projects/{p['id']}/assets").json()["items"]
            if x["id"] == a["id"])
        # Linking is metadata on the link row — the asset record
        # (sha256, uri, provenance, version) is untouched.
        assert after["sha256"] == before["sha256"]
        assert after["uri"] == before["uri"]

    def test_character_duplicate_carries_weight(self, client, tmp_path):
        p = make_project(client, "DupChar")
        c = _char(client, p["id"], "Ada", "dc-1")
        a = _import(client, p["id"], _png(tmp_path / "a.png"), kind="image")
        _link(client, p["id"], c["id"], a["id"], "dc-2",
              role="identity", weight=1.25, label="face")
        _commit(client, p["id"], "character.duplicate",
                {"character_id": c["id"]}, "dc-3")
        dup = next(x for x in client.get(
            f"/api/projects/{p['id']}/character").json()
            if x["name"] == "Ada (copy)")
        dlinks = _links(client, p["id"], dup["id"])
        assert len(dlinks) == 1
        assert dlinks[0]["weight"] == pytest.approx(1.25)
        assert dlinks[0]["role"] == "identity"

    def test_character_bible_unaffected_by_links(self, client, tmp_path):
        """Linking references must not disturb the versioned bible
        profile or revision history."""
        p = make_project(client, "Bible")
        c = _char(client, p["id"], "Ada", "bb-1")
        _commit(client, p["id"], "character.update",
                {"character_id": c["id"],
                 "data": {"appearance": "silver bob"}}, "bb-2")
        a = _import(client, p["id"], _png(tmp_path / "a.png"), kind="image")
        _link(client, p["id"], c["id"], a["id"], "bb-3", role="reference")
        c2 = next(x for x in client.get(
            f"/api/projects/{p['id']}/character").json()
            if x["id"] == c["id"])
        assert c2["data"]["appearance"] == "silver bob"
        revs = client.get(
            f"/api/projects/{p['id']}/character/{c['id']}/versions").json()
        assert len(revs) >= 1  # bible revisions intact
