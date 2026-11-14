"""C4-B — scene/shot generation integration.

Shots bind a character reference set + source image (columns on the
existing M2 `shots` row — membership still lives on entity_assets).
`shot.generate` composes the REAL img2img / cloud-image pipelines
server-side; nothing about the pipeline is duplicated.
"""
import io

import pytest
from PIL import Image

from tests.conftest import make_project, wait_for_job


def _rev(client, pid):
    return client.get(f"/api/projects/{pid}").json()["revision"]


_K = [0]


def _commit(client, pid, ct, payload, expect=200, key=None):
    _K[0] += 1
    r = client.post(
        f"/api/projects/{pid}/commands/commit",
        json={"command_type": ct, "payload": payload, "scope": "project",
              "expected_revision": _rev(client, pid),
              "idempotency_key": key or f"c4b-key-{_K[0]:04d}"})
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


def _preview(client, pid, ct, payload, expect=200):
    r = client.post(
        f"/api/projects/{pid}/commands/preview",
        json={"command_type": ct, "payload": payload, "scope": "project"})
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


def _png(color=(50, 60, 70)):
    im = Image.new("RGB", (16, 16), color)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def _import(client, pid, name, tmp_path, color):
    p = tmp_path / name
    p.write_bytes(_png(color))
    r = _commit(client, pid, "asset.import",
                {"source_path": str(p), "name": name, "kind": "image"})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded", job
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    return next(a for a in items if a["name"] == name)


def _job_req(client, job_id):
    with client.app.state.session_factory() as s:
        from app.db.models import GenerationJob
        return s.get(GenerationJob, job_id).request_json


def _char(client, pid, name="Ada"):
    _commit(client, pid, "character.create", {"name": name})
    return next(c for c in client.get(
        f"/api/projects/{pid}/character").json() if c["name"] == name)


def _board(client, pid):
    return client.get(f"/api/projects/{pid}/storyboard").json()


def _scene(client, pid, title="S1"):
    _commit(client, pid, "scene.create", {"title": title})
    return next(s for s in _board(client, pid) if s["scene"]["title"] == title)


def _shot(client, pid, scene_id, title="wide", **kw):
    _commit(client, pid, "shot.create",
            {"scene_id": scene_id, "title": title, **kw})
    scene = next(s for s in _board(client, pid)
                 if s["scene"]["id"] == scene_id)
    return next(sh for sh in scene["shots"] if sh["shot"]["title"] == title)


def _refset(client, pid, cid, assets, name="Hero refs", seed=7):
    _commit(client, pid, "refset.create",
            {"character_id": cid, "name": name, "seed": seed})
    sid = client.get(
        f"/api/projects/{pid}/reference-sets?character_id={cid}").json()[0]["id"]
    for i, a in enumerate(assets):
        _commit(client, pid, "entity.assets.link",
                {"entity_type": "character", "entity_id": cid,
                 "asset_id": a["id"], "role": "reference",
                 "set_id": sid})
    return sid


GEN = {"provider_key": "openai-images", "model_id": "gpt-image-1",
       "execution_target": "cloud"}


def _setup(client, tmp_path):
    """Project with a character, a 2-ref set, a scene + shot."""
    p = make_project(client)
    pid = p["id"]
    src = _import(client, pid, "src.png", tmp_path, (10, 20, 30))
    r1 = _import(client, pid, "r1.png", tmp_path, (200, 20, 30))
    r2 = _import(client, pid, "r2.png", tmp_path, (20, 200, 30))
    c = _char(client, pid)
    sid = _refset(client, pid, c["id"], [r1, r2])
    scene = _scene(client, pid)
    return pid, c, sid, src, scene


def test_binding_on_create_and_update(client, tmp_path):
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="misty dawn")
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "reference_set_id": sid,
             "source_asset_id": src["id"]})
    board = _board(client, pid)
    got = next(x for s in board for x in s["shots"]
               if x["shot"]["id"] == sh["shot"]["id"])["shot"]
    assert got["reference_set_id"] == sid
    assert got["source_asset_id"] == src["id"]
    # Create-time binding is equally first-class.
    sh2 = _shot(client, pid, scene["scene"]["id"], "closeup",
                prompt="eyes", reference_set_id=sid)
    assert _board(client, pid)[0]["shots"][-1]["shot"]["reference_set_id"] == sid


def test_cast_membership_enforced(client, tmp_path):
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x")
    # Cast is empty — binding allowed (nothing contradicts it).
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "reference_set_id": sid})
    # Once a DIFFERENT character is cast, the set must belong to cast.
    other = _char(client, pid, "Borin")
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": other["id"]})
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "reference_set_id": sid},
            expect=422)
    # Casting the set's character too makes it legal again.
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"]})
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "reference_set_id": sid})


def test_binding_isolation_and_missing_set(client, tmp_path):
    pid, c, sid, src, scene = _setup(client, tmp_path)
    p2 = make_project(client)
    sc2 = _scene(client, p2["id"])
    sh2 = _shot(client, p2["id"], sc2["scene"]["id"], prompt="x")
    # Cross-project set cannot be bound or resolved.
    _commit(client, p2["id"], "shot.update",
            {"shot_id": sh2["shot"]["id"], "reference_set_id": sid},
            expect=404)
    _commit(client, pid, "shot.update",
            {"shot_id": sh2["shot"]["id"], "reference_set_id": sid},
            expect=404)  # wrong project's shot too
    _commit(client, pid, "shot.update",
            {"shot_id": sh2["shot"]["id"], "source_asset_id": src["id"]},
            expect=404)


def test_deleted_set_fails_closed(client, tmp_path):
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x",
               source_asset_id=src["id"])
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "reference_set_id": sid})
    _commit(client, pid, "refset.delete", {"set_id": sid})
    r = _preview(client, pid, "shot.generate",
                 {**GEN, "shot_id": sh["shot"]["id"]}, expect=404)
    assert "reference set" in str(r.json())


def test_shot_generate_img2img_composition(client, tmp_path):
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="misty dawn",
               source_asset_id=src["id"])
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"]})
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "reference_set_id": sid})
    prev = _preview(client, pid, "shot.generate",
                    {**GEN, "shot_id": sh["shot"]["id"]})
    assert prev["launches_generation"] is True
    assert prev["cost_estimate"]["est_cost_usd"] is not None
    resolved = prev["resolved_references"]
    assert len(resolved) == 2
    r = _commit(client, pid, "shot.generate",
                {**GEN, "shot_id": sh["shot"]["id"],
                 "expected_references": resolved,
                 "expected_set": prev["resolved_set"]})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded", job
    req = _job_req(client, job["id"])
    # Composed img2img request: shot bindings → real pipeline.
    assert req["origin_command_type"] == "shot.generate"
    assert req["mode"] == "img2img"
    assert req["source_asset_id"] == src["id"]
    assert req["reference_set_id"] == sid
    assert req["reference_set_revision"] is not None
    assert len(req["reference_sha256"]) == 2
    assert req["shot_id"] == sh["shot"]["id"]
    assert req["scene_id"] == scene["scene"]["id"]
    assert req["character_id"] == c["id"]
    assert req["seed"] == 7  # set's pinned seed — no explicit seed
    assert req["shot_generate"]["shot_id"] == sh["shot"]["id"]
    assert req["simulated"] is True


def test_shot_generate_text2image_without_source(client, tmp_path):
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="neon alley")
    r = _commit(client, pid, "shot.generate",
                {**GEN, "shot_id": sh["shot"]["id"]})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded", job
    req = _job_req(client, job["id"])
    assert req["origin_command_type"] == "shot.generate"
    assert req.get("mode") != "img2img"
    assert req["shot_id"] == sh["shot"]["id"]
    assert job["adapter_id"] == "cloud.image.sim"


def test_prompt_fallback_and_archived_shot(client, tmp_path):
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"])  # no prompt
    _preview(client, pid, "shot.generate",
             {**GEN, "shot_id": sh["shot"]["id"]}, expect=422)
    _commit(client, pid, "shot.archive",
            {"shot_id": sh["shot"]["id"]})
    _preview(client, pid, "shot.generate",
             {**GEN, "shot_id": sh["shot"]["id"]}, expect=422)


def test_stale_approval_rejected(client, tmp_path):
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x",
               source_asset_id=src["id"])
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"]})
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "reference_set_id": sid})
    prev = _preview(client, pid, "shot.generate",
                    {**GEN, "shot_id": sh["shot"]["id"]})
    _commit(client, pid, "refset.update", {"set_id": sid, "seed": 99})
    r = _commit(client, pid, "shot.generate",
                {**GEN, "shot_id": sh["shot"]["id"],
                 "expected_references": prev["resolved_references"],
                 "expected_set": prev["resolved_set"]},
                expect=422)
    assert "changed since preview" in str(r.json())


def test_duplicate_and_revert_preserve_bindings(client, tmp_path):
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x",
               source_asset_id=src["id"])
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "reference_set_id": sid})
    _commit(client, pid, "shot.duplicate",
            {"shot_id": sh["shot"]["id"]})
    board = _board(client, pid)
    dups = [x["shot"] for x in board[0]["shots"] if x["shot"]["id"] != sh["shot"]["id"]]
    assert dups and dups[0]["reference_set_id"] == sid
    assert dups[0]["source_asset_id"] == src["id"]
    # Revert to pre-binding revision clears it; reverting forward is
    # itself a new revision (existing revision semantics preserved).
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "reference_set_id": None})
    cur = next(x for s in _board(client, pid) for x in s["shots"]
               if x["shot"]["id"] == sh["shot"]["id"])["shot"]
    assert cur["reference_set_id"] is None
    revs = client.get(
        f"/api/projects/{pid}/shots/{sh['shot']['id']}/versions").json()
    bound = next(r for r in revs if
                 r["snapshot_json"].get("reference_set_id") == sid)
    _commit(client, pid, "shot.revert",
            {"shot_id": sh["shot"]["id"], "to_revision": bound["revision"]})
    cur = next(x for s in _board(client, pid) for x in s["shots"]
               if x["shot"]["id"] == sh["shot"]["id"])["shot"]
    assert cur["reference_set_id"] == sid


def test_continuity_merges_not_replaces(client, tmp_path):
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x")
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"],
             "continuity": {"appearance": "silver jacket"}})
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"],
             "continuity": {"lighting": "overcast noon"}})
    cur = next(x for s in _board(client, pid) for x in s["shots"]
               if x["shot"]["id"] == sh["shot"]["id"])["shot"]
    assert cur["continuity_json"] == {
        "appearance": "silver jacket", "lighting": "overcast noon"}


def test_capability_and_cost_gates_apply(client, tmp_path):
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x",
               source_asset_id=src["id"])
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "reference_set_id": sid})
    # recraft is declared-only — refused through the SAME gate as
    # direct img2img; shot.generate adds no bypass.
    r = _preview(client, pid, "shot.generate",
                 {"provider_key": "recraft", "model_id": "recraftv4",
                  "execution_target": "cloud",
                  "shot_id": sh["shot"]["id"]}, expect=422)
    assert r.json()


def test_cast_removal_invalidates_generate(client, tmp_path):
    """Removing the bound character from the effective cast must refuse
    generation — including the empty-cast edge that binding-time
    deliberately permits."""
    pid, c, sid, src, scene = _setup(client, tmp_path)
    _commit(client, pid, "scene.cast.assign",
            {"scene_id": scene["scene"]["id"], "character_id": c["id"]})
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x")
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "reference_set_id": sid})
    # Inherited membership satisfies the cast check at generate time.
    prev = _preview(client, pid, "shot.generate",
                    {**GEN, "shot_id": sh["shot"]["id"]})
    assert prev["resolved_shot"]["cast"] == [c["id"]]
    # Removing the character empties the cast — bound set must refuse,
    # not fall back to unrelated references.
    _commit(client, pid, "scene.cast.remove",
            {"scene_id": scene["scene"]["id"], "character_id": c["id"]})
    r = _preview(client, pid, "shot.generate",
                 {**GEN, "shot_id": sh["shot"]["id"]}, expect=422)
    assert "cast" in str(r.json())


def test_shot_state_change_invalidates_preview(client, tmp_path):
    """Prompt/source edits after preview refuse the pinned commit —
    the set fingerprints alone cannot see shot-level changes."""
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="misty dawn",
               source_asset_id=src["id"])
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"]})
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "reference_set_id": sid})
    prev = _preview(client, pid, "shot.generate",
                    {**GEN, "shot_id": sh["shot"]["id"]})
    fp = prev["resolved_shot"]
    assert fp["shot_id"] == sh["shot"]["id"]
    assert fp["prompt"] == "misty dawn"
    assert fp["source_asset_id"] == src["id"]
    assert fp["reference_set_id"] == sid
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "prompt": "sunny noon"})
    r = _commit(client, pid, "shot.generate",
                {**GEN, "shot_id": sh["shot"]["id"],
                 "expected_shot": fp,
                 "expected_references": prev["resolved_references"],
                 "expected_set": prev["resolved_set"]},
                expect=422)
    assert "shot changed since preview" in str(r.json())


def test_cast_change_invalidates_preview(client, tmp_path):
    """Adding/removing cast members doesn't bump the shot row — the
    fingerprint carries membership explicitly."""
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x")
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "reference_set_id": sid})
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"]})
    prev = _preview(client, pid, "shot.generate",
                    {**GEN, "shot_id": sh["shot"]["id"]})
    other = _char(client, pid, "Borin")
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": other["id"]})
    r = _commit(client, pid, "shot.generate",
                {**GEN, "shot_id": sh["shot"]["id"],
                 "expected_shot": prev["resolved_shot"]},
                expect=422)
    assert "shot changed since preview" in str(r.json())


def test_expected_shot_pins_clean_commit(client, tmp_path):
    """expected_shot is not just a tripwire: an unchanged shot commits
    fine with the pin attached."""
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="misty dawn",
               source_asset_id=src["id"])
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"]})
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "reference_set_id": sid})
    prev = _preview(client, pid, "shot.generate",
                    {**GEN, "shot_id": sh["shot"]["id"]})
    r = _commit(client, pid, "shot.generate",
                {**GEN, "shot_id": sh["shot"]["id"],
                 "expected_shot": prev["resolved_shot"],
                 "expected_references": prev["resolved_references"],
                 "expected_set": prev["resolved_set"]})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded", job


def test_output_queryable_by_shot_provenance(client, tmp_path):
    """Generated assets carry the originating shot id in durable
    provenance — discoverable via a fresh query (post-restart shape),
    and a failed job must not fake an association."""
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="misty dawn",
               source_asset_id=src["id"])
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"]})
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "reference_set_id": sid})
    r = _commit(client, pid, "shot.generate",
                {**GEN, "shot_id": sh["shot"]["id"]})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded", job
    # Fresh list query — provenance_json.request.shot_generate is the
    # restart-proof association, not transient UI state.
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    mine = [a for a in items
            if ((a.get("provenance_json") or {}).get("request") or {})
                .get("shot_generate", {}).get("shot_id") == sh["shot"]["id"]]
    assert len(mine) >= 1  # sim adapter may emit intermediate + final
    # And the job row itself retains the originating shot id.
    req = _job_req(client, job["id"])
    assert req["shot_generate"]["shot_id"] == sh["shot"]["id"]
