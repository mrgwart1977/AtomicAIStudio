"""C4-A — character reference sets + identity guidance.

Named sets group a character's entity-asset reference links
(membership via entity_assets.set_id — links keep role/weight/order).
img2img resolves a set server-side, enforces the existing caps, and
refuses stale approvals when the set changed since preview.
"""
import io

import pytest
from PIL import Image

from tests.conftest import make_project, wait_for_job


def _rev(client, pid):
    return client.get(f"/api/projects/{pid}").json()["revision"]


_KEY = [0]


def _commit(client, pid, ct, payload, expect=200, key=None):
    _KEY[0] += 1
    r = client.post(
        f"/api/projects/{pid}/commands/commit",
        json={"command_type": ct, "payload": payload, "scope": "project",
              "expected_revision": _rev(client, pid),
              "idempotency_key": key or f"c4a-key-{_KEY[0]:04d}"})
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


def _preview(client, pid, ct, payload, expect=200):
    r = client.post(
        f"/api/projects/{pid}/commands/preview",
        json={"command_type": ct, "payload": payload, "scope": "project"})
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


def _png(w=16, h=16, color=(50, 60, 70)):
    im = Image.new("RGB", (w, h), color)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def _import(client, pid, name, tmp_path, color):
    p = tmp_path / name
    p.write_bytes(_png(color=color))
    r = _commit(client, pid, "asset.import",
                {"source_path": str(p), "name": name, "kind": "image"})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded", job
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    return next(a for a in items if a["name"] == name)


def _char_id(client, pid, name="Ada"):
    ents = client.get(f"/api/projects/{pid}/character").json()
    return next(e["id"] for e in ents if e["name"] == name)


def _links(client, pid, cid):
    r = client.get(f"/api/projects/{pid}/entity-assets"
                   f"?entity_type=character&entity_id={cid}")
    assert r.status_code == 200, r.text
    return r.json()


def _mkset(client, pid, cid, name="Hero refs", seed=42):
    return _commit(client, pid, "refset.create",
                   {"character_id": cid, "name": name, "seed": seed},
                   key=f"c4a-set-{name}-{seed}")


def _set_id(client, pid, cid):
    sets = client.get(
        f"/api/projects/{pid}/reference-sets?character_id={cid}").json()
    return sets[0]["id"]


IMG2IMG = {"provider_key": "openai-images", "model_id": "gpt-image-1",
           "operation": "img2img", "execution_target": "cloud",
           "prompt": "same woman, new backdrop"}


def _setup(client, tmp_path, with_weights=False):
    p = make_project(client)
    pid = p["id"]
    src = _import(client, pid, "src.png", tmp_path, (10, 20, 30))
    ref1 = _import(client, pid, "r1.png", tmp_path, (200, 20, 30))
    ref2 = _import(client, pid, "r2.png", tmp_path, (20, 200, 30))
    _commit(client, pid, "character.create", {"name": "Ada"},
            key="c4a-char-1")
    cid = _char_id(client, pid)
    _mkset(client, pid, cid)
    sid = _set_id(client, pid, cid)
    ids = {}
    for i, a in enumerate((ref1, ref2)):
        pay = {"entity_type": "character", "entity_id": cid,
               "asset_id": a["id"], "role": "reference",
               "set_id": sid}
        if with_weights:
            pay["weight"] = 0.5 + i
        _commit(client, pid, "entity.assets.link", pay,
                key=f"c4a-link-{i}")
        ids[a["name"]] = a["id"]
    return pid, src, sid, ids, cid


def test_set_crud_and_membership(client, tmp_path):
    pid, src, sid, ids, cid = _setup(client, tmp_path)
    sets = client.get(f"/api/projects/{pid}/reference-sets").json()
    assert len(sets) == 1 and sets[0]["name"] == "Hero refs"
    assert sets[0]["seed"] == 42 and sets[0]["member_count"] == 2
    # Update metadata.
    _commit(client, pid, "refset.update",
            {"set_id": sid, "notes": "golden-hour look"},
            key="c4a-upd-1")
    assert client.get(f"/api/projects/{pid}/reference-sets").json()[0]["notes"] \
        == "golden-hour look"
    # Delete: set archives, links become ungrouped references — never lost.
    _commit(client, pid, "refset.delete", {"set_id": sid}, key="c4a-del-1")
    assert client.get(f"/api/projects/{pid}/reference-sets").json() == []
    links = client.get(
        f"/api/projects/{pid}/entity-assets?entity_id={ids['r1.png'].split('-')[0]}") \
        .json() if False else client.get(
        f"/api/projects/{pid}/entity-assets").json()
    char_links = [l for l in links if l["asset_id"] in ids.values()]
    assert len(char_links) == 2
    assert all(l["set_id"] is None for l in char_links)


def test_isolation_and_invalid_inputs(client, tmp_path):
    pid, src, sid, ids, cid = _setup(client, tmp_path)
    p2 = make_project(client)
    # Foreign project cannot see or mutate the set.
    assert client.get(f"/api/projects/{p2['id']}/reference-sets").json() == []
    _preview(client, p2["id"], "refset.update", {"set_id": sid, "name": "x"},
             expect=404)
    _preview(client, pid, "refset.create",
             {"character_id": "nope", "name": "y"}, expect=404)
    _preview(client, pid, "refset.create",
             {"character_id": _char_id(client, pid), "name": "  "},
             expect=422)
    _preview(client, pid, "refset.update",
             {"set_id": sid, "seed": -1}, expect=422)
    _preview(client, pid, "refset.update",
             {"set_id": sid, "seed": 1.5}, expect=422)


def test_img2img_resolves_set_and_records_provenance(client, tmp_path):
    pid, src, sid, ids, cid = _setup(client, tmp_path)
    prev = _preview(client, pid, "generate.image.img2img",
                    {**IMG2IMG, "source_asset_id": src["id"],
                     "reference_set_id": sid})
    resolved = prev["resolved_references"]
    assert [r["asset_id"] for r in resolved] == \
        [ids["r1.png"], ids["r2.png"]]
    r = _commit(client, pid, "generate.image.img2img",
                {**IMG2IMG, "source_asset_id": src["id"],
                 "reference_set_id": sid,
                 "expected_references": resolved},
                key="c4a-gen-1")
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded", job
    req = _job_req(client, job["id"])
    assert req["reference_set_id"] == sid
    assert req["reference_asset_ids"] == [ids["r1.png"], ids["r2.png"]]
    assert req["reference_weights"] == []
    assert req["seed"] == 42                 # pinned seed applied
    assert req["simulated"] is True


def _job_req(client, job_id):
    with client.app.state.session_factory() as s:
        from app.db.models import GenerationJob
        return s.get(GenerationJob, job_id).request_json


def test_stale_approval_rejected_when_set_changes(client, tmp_path):
    pid, src, sid, ids, cid = _setup(client, tmp_path)
    prev = _preview(client, pid, "generate.image.img2img",
                    {**IMG2IMG, "source_asset_id": src["id"],
                     "reference_set_id": sid})
    resolved = prev["resolved_references"]
    # Mutate the set: remove a member - resolved contents change.
    links = _links(client, pid, cid)
    lid = next(l["id"] for l in links if l["asset_id"] == ids["r1.png"])
    _commit(client, pid, "entity.assets.unlink",
            {"link_id": lid}, key="c4a-unlink-1")
    # Committing the stale approval is refused — a re-preview is required.
    r = _commit(client, pid, "generate.image.img2img",
                {**IMG2IMG, "source_asset_id": src["id"],
                 "reference_set_id": sid,
                 "expected_references": resolved},
                expect=422, key="c4a-gen-2")
    assert "changed since preview" in str(r.json())


def test_mutual_exclusion_and_caps(client, tmp_path):
    pid, src, sid, ids, cid = _setup(client, tmp_path)
    other = _import(client, pid, "r3.png", tmp_path, (9, 9, 9))
    _preview(client, pid, "generate.image.img2img",
             {**IMG2IMG, "source_asset_id": src["id"],
              "reference_set_id": sid,
              "reference_asset_ids": [other["id"]]}, expect=422)


def test_explicit_seed_overrides_pinned(client, tmp_path):
    pid, src, sid, ids, cid = _setup(client, tmp_path)
    r = _commit(client, pid, "generate.image.img2img",
                {**IMG2IMG, "source_asset_id": src["id"],
                 "reference_set_id": sid, "seed": 777},
                key="c4a-gen-3")
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded"
    assert _job_req(client, job["id"])["seed"] == 777


def test_duplicate_character_copies_sets(client, tmp_path):
    pid, src, sid, ids, cid = _setup(client, tmp_path)
    _commit(client, pid, "character.duplicate",
            {"character_id": cid, "name": "Ada copy"}, key="c4a-dup-1")
    dup = client.get(f"/api/projects/{pid}/character").json()
    dup = next(e for e in dup if e["name"] == "Ada copy")
    sets = client.get(
        f"/api/projects/{pid}/reference-sets?character_id={dup['id']}").json()
    assert len(sets) == 1 and sets[0]["member_count"] == 2
    dup_links = _links(client, pid, dup["id"])
    assert all(l["set_id"] == sets[0]["id"] for l in dup_links)


def test_weight_semantics_fail_closed(client, tmp_path):
    """A set carrying weights against a model without weight support is
    refused — the two-layer gate applies to resolved refs too."""
    pid, src, sid, ids, cid = _setup(client, tmp_path, with_weights=True)
    r = _preview(client, pid, "generate.image.img2img",
                 {**IMG2IMG, "source_asset_id": src["id"],
                  "reference_set_id": sid}, expect=422)
    assert "reference weights" in str(r.json())


def test_seed_change_invalidates_via_expected_set(client, tmp_path):
    """A pinned-seed edit changes no member fingerprint — the approval
    must still die via expected_set."""
    pid, src, sid, ids, cid = _setup(client, tmp_path)
    prev = _preview(client, pid, "generate.image.img2img",
                    {**IMG2IMG, "source_asset_id": src["id"],
                     "reference_set_id": sid})
    resolved, rset = prev["resolved_references"], prev["resolved_set"]
    assert rset["seed"] == 42
    _commit(client, pid, "refset.update",
            {"set_id": sid, "seed": 99}, key="c4a-seed-chg")
    r = _commit(client, pid, "generate.image.img2img",
                {**IMG2IMG, "source_asset_id": src["id"],
                 "reference_set_id": sid,
                 "expected_references": resolved,
                 "expected_set": rset},
                expect=422, key="c4a-gen-seed")
    assert "changed since preview" in str(r.json())


def test_resolved_entries_carry_content_hash(client, tmp_path):
    """Approval fingerprints cover asset_id + weight + current sha256
    of every member — a re-versioned asset is a different approval."""
    pid, src, sid, ids, cid = _setup(client, tmp_path)
    prev = _preview(client, pid, "generate.image.img2img",
                    {**IMG2IMG, "source_asset_id": src["id"],
                     "reference_set_id": sid})
    for e in prev["resolved_references"]:
        assert set(e) == {"asset_id", "weight", "sha256"}
        assert len(e["sha256"]) == 64
    # A caller-side lie about content identity is refused, not trusted.
    fake = [dict(e, sha256="0" * 64)
            for e in prev["resolved_references"]]
    _commit(client, pid, "generate.image.img2img",
            {**IMG2IMG, "source_asset_id": src["id"],
             "reference_set_id": sid, "expected_references": fake},
            expect=422, key="c4a-gen-fake")


def test_full_expected_payload_commits(client, tmp_path):
    """Happy path: preview-resolved entries + expected_set accepted."""
    pid, src, sid, ids, cid = _setup(client, tmp_path)
    prev = _preview(client, pid, "generate.image.img2img",
                    {**IMG2IMG, "source_asset_id": src["id"],
                     "reference_set_id": sid})
    r = _commit(client, pid, "generate.image.img2img",
                {**IMG2IMG, "source_asset_id": src["id"],
                 "reference_set_id": sid,
                 "expected_references": prev["resolved_references"],
                 "expected_set": prev["resolved_set"]},
                key="c4a-gen-full")
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded"
