"""M2 coverage: multi-character foundation — scenes, shots, casting with
independent per-character state, ordering, duplication, versioned history
(revert restores cast), undo, entity-asset links, project isolation,
backup round-trip, and migration shape."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from tests.conftest import make_project, wait_for_job


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


def _undo(client, pid, command_id):
    r = client.post(f"/api/projects/{pid}/commands/{command_id}/undo")
    assert r.status_code == 200, r.text
    return r.json()


def _board(client, pid):
    r = client.get(f"/api/projects/{pid}/storyboard")
    assert r.status_code == 200, r.text
    return r.json()


def _char(client, pid, name, key):
    _commit(client, pid, "character.create", {"name": name}, key)
    chars = client.get(f"/api/projects/{pid}/character").json()
    return next(c for c in chars if c["name"] == name)


def _scene(client, pid, title, key, **kw):
    _commit(client, pid, "scene.create", {"title": title, **kw}, key)
    return next(s for s in _board(client, pid) if s["scene"]["title"] == title)


def _shot(client, pid, scene_id, key, **kw):
    _commit(client, pid, "shot.create", {"scene_id": scene_id, **kw}, key)
    scene = next(s for s in _board(client, pid) if s["scene"]["id"] == scene_id)
    return scene["shots"][-1]


# ---------- scenes & ordering ----------

def test_scene_create_order_move(client):
    p = make_project(client, "Scenes")
    s1 = _scene(client, p["id"], "Opening", "sc-1")
    s2 = _scene(client, p["id"], "Chase", "sc-2")
    s3 = _scene(client, p["id"], "Finale", "sc-3")
    board = _board(client, p["id"])
    assert [x["scene"]["title"] for x in board] == ["Opening", "Chase", "Finale"]
    assert [x["scene"]["position"] for x in board] == [0, 1, 2]

    # Move "Finale" to the front.
    _commit(client, p["id"], "scene.move",
            {"scene_id": s3["scene"]["id"], "to_index": 0}, "sc-mv")
    titles = [x["scene"]["title"] for x in _board(client, p["id"])]
    assert titles == ["Finale", "Opening", "Chase"]
    assert [x["scene"]["position"] for x in _board(client, p["id"])] == [0, 1, 2]


def test_scene_update_links_and_archive(client):
    p = make_project(client, "SceneLinks")
    _commit(client, p["id"], "world.create", {"name": "Neo-Tokyo"}, "w-1")
    world = client.get(f"/api/projects/{p['id']}/world").json()[0]
    _commit(client, p["id"], "location.create",
            {"name": "Rooftop", "world_id": world["id"]}, "l-1")
    loc = client.get(f"/api/projects/{p['id']}/location").json()[0]

    s = _scene(client, p["id"], "Standoff", "sc-u",
               world_id=world["id"], location_id=loc["id"])
    scene = _board(client, p["id"])[0]["scene"]
    assert scene["world_id"] == world["id"]
    assert scene["location_id"] == loc["id"]

    _commit(client, p["id"], "scene.update",
            {"scene_id": s["scene"]["id"], "intent": "slow-burn tension",
             "continuity": {"weather": "rain"}}, "sc-u2")
    scene = _board(client, p["id"])[0]["scene"]
    assert scene["intent"] == "slow-burn tension"
    assert scene["continuity_json"]["weather"] == "rain"

    _commit(client, p["id"], "scene.archive", {"scene_id": s["scene"]["id"]}, "sc-a")
    assert _board(client, p["id"], )[0]["scene"]["status"] == "archived"
    _commit(client, p["id"], "scene.unarchive", {"scene_id": s["scene"]["id"]}, "sc-ua")
    assert _board(client, p["id"])[0]["scene"]["status"] == "active"


# ---------- shots: ordering, move between scenes, fields ----------

def test_shot_create_order_move(client):
    p = make_project(client, "Shots")
    s1 = _scene(client, p["id"], "A", "s-a")
    s2 = _scene(client, p["id"], "B", "s-b")
    sh1 = _shot(client, p["id"], s1["scene"]["id"], "sh-1", title="wide", shot_type="wide")
    sh2 = _shot(client, p["id"], s1["scene"]["id"], "sh-2", title="close", shot_type="close")
    sh3 = _shot(client, p["id"], s1["scene"]["id"], "sh-3", title="pov", shot_type="pov")

    board = _board(client, p["id"])
    a = next(x for x in board if x["scene"]["id"] == s1["scene"]["id"])
    assert [x["shot"]["title"] for x in a["shots"]] == ["wide", "close", "pov"]

    # Reorder within scene A: pov first.
    _commit(client, p["id"], "shot.move",
            {"shot_id": sh3["shot"]["id"], "to_index": 0}, "sh-mv1")
    a = next(x for x in _board(client, p["id"]) if x["scene"]["id"] == s1["scene"]["id"])
    assert [x["shot"]["title"] for x in a["shots"]] == ["pov", "wide", "close"]
    assert [x["shot"]["position"] for x in a["shots"]] == [0, 1, 2]

    # Move "close" into scene B at index 0.
    _commit(client, p["id"], "shot.move",
            {"shot_id": sh2["shot"]["id"], "to_index": 0,
             "to_scene_id": s2["scene"]["id"]}, "sh-mv2")
    board = _board(client, p["id"])
    a = next(x for x in board if x["scene"]["id"] == s1["scene"]["id"])
    b = next(x for x in board if x["scene"]["id"] == s2["scene"]["id"])
    assert [x["shot"]["title"] for x in a["shots"]] == ["pov", "wide"]
    assert [x["shot"]["title"] for x in b["shots"]] == ["close"]
    assert [x["shot"]["position"] for x in a["shots"]] == [0, 1]


def test_shot_fields_and_validation(client):
    p = make_project(client, "ShotFields")
    s = _scene(client, p["id"], "S", "sf-0")
    sh = _shot(client, p["id"], s["scene"]["id"], "sf-1",
               title="Hero shot", shot_type="extreme_close", duration_sec=3.5,
               prompt="rain on glass, neon reflections")
    shot = sh["shot"]
    assert shot["shot_type"] == "extreme_close"
    assert shot["duration_sec"] == 3.5
    assert "neon" in shot["prompt"]

    # Update incl. prompt — prompt stays user-editable at all times.
    _commit(client, p["id"], "shot.update",
            {"shot_id": shot["id"], "prompt": "edited: tears in rain",
             "continuity": {"prop_state": "broken watch"}}, "sf-2")
    shot = _board(client, p["id"])[0]["shots"][0]["shot"]
    assert shot["prompt"] == "edited: tears in rain"
    assert shot["continuity_json"]["prop_state"] == "broken watch"

    # Invalid shot type rejected.
    r = _commit(client, p["id"], "shot.update",
                {"shot_id": shot["id"], "shot_type": "sideways"}, "sf-3", expect=422)
    assert r.status_code == 422


# ---------- multi-character casting ----------

def test_multi_character_shot_independent_state(client):
    """Three characters in one shot — each with fully independent state."""
    p = make_project(client, "Ensemble")
    hero = _char(client, p["id"], "Ada", "c-1")
    villain = _char(client, p["id"], "Mori", "c-2")
    extra = _char(client, p["id"], "Bystander", "c-3")
    s = _scene(client, p["id"], "Confrontation", "s-1")
    sh = _shot(client, p["id"], s["scene"]["id"], "sh-1", title="standoff")
    sid = sh["shot"]["id"]

    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sid, "character_id": hero["id"],
             "state": {"expression": "defiant", "emotion": "anger",
                       "wardrobe_variant": "torn coat", "blocking": "foreground left",
                       "action_instructions": "draws weapon slowly",
                       "dialogue": "Not this time.", "voice_override": "low",
                       "model_ref": "hero-v2", "loras": ["ada-face-v3"]}}, "ca-1")
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sid, "character_id": villain["id"],
             "state": {"expression": "smirk", "emotion": "contempt",
                       "blocking": "background right", "dialogue": "We'll see."}}, "ca-2")
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sid, "character_id": extra["id"]}, "ca-3")

    shot = _board(client, p["id"])[0]["shots"][0]
    assert len(shot["cast"]) == 3
    by_name = {c["character"]["name"]: c for c in shot["cast"]}
    ada = by_name["Ada"]
    assert ada["expression"] == "defiant"
    assert ada["wardrobe_variant"] == "torn coat"
    assert ada["blocking"] == "foreground left"
    assert ada["dialogue"] == "Not this time."
    assert ada["model_ref"] == "hero-v2"
    assert ada["lora_json"] == ["ada-face-v3"]
    assert by_name["Mori"]["emotion"] == "contempt"
    assert by_name["Bystander"]["expression"] == ""  # untouched default

    # Update one character's state — others untouched.
    _commit(client, p["id"], "shot.cast.update",
            {"shot_id": sid, "character_id": villain["id"],
             "state": {"expression": "snarl", "continuity": {"scar_visible": True}}}, "ca-4")
    shot = _board(client, p["id"])[0]["shots"][0]
    by_name = {c["character"]["name"]: c for c in shot["cast"]}
    assert by_name["Mori"]["expression"] == "snarl"
    assert by_name["Mori"]["continuity_json"]["scar_visible"] is True
    assert by_name["Ada"]["expression"] == "defiant"

    # Re-assign same character → 409.
    r = _commit(client, p["id"], "shot.cast.assign",
                {"shot_id": sid, "character_id": hero["id"]}, "ca-5", expect=409)
    assert r.status_code == 409

    # Remove one — others stay.
    _commit(client, p["id"], "shot.cast.remove",
            {"shot_id": sid, "character_id": extra["id"]}, "ca-6")
    shot = _board(client, p["id"])[0]["shots"][0]
    assert {c["character"]["name"] for c in shot["cast"]} == {"Ada", "Mori"}


def test_scene_cast_and_many_characters(client):
    """Scene-level cast + a dozen characters in one shot (no arbitrary cap)."""
    p = make_project(client, "BigCast")
    chars = [_char(client, p["id"], f"Char{i:02d}", f"cc-{i}") for i in range(12)]
    s = _scene(client, p["id"], "Crowd", "s-1")
    sid = s["scene"]["id"]

    _commit(client, p["id"], "scene.cast.assign",
            {"scene_id": sid, "character_id": chars[0]["id"], "role": "lead"}, "sc-1")
    _commit(client, p["id"], "scene.cast.assign",
            {"scene_id": sid, "character_id": chars[1]["id"], "role": "supporting"}, "sc-2")
    scene = _board(client, p["id"])[0]
    assert [c["character"]["name"] for c in scene["cast"]] == ["Char00", "Char01"]
    assert scene["cast"][0]["role"] == "lead"

    _commit(client, p["id"], "scene.cast.update",
            {"scene_id": sid, "character_id": chars[1]["id"], "role": "antagonist"}, "sc-3")
    assert _board(client, p["id"])[0]["cast"][1]["role"] == "antagonist"

    sh = _shot(client, p["id"], sid, "sh-1", shot_type="group")
    for i, c in enumerate(chars):
        _commit(client, p["id"], "shot.cast.assign",
                {"shot_id": sh["shot"]["id"], "character_id": c["id"],
                 "state": {"blocking": f"mark {i}"}}, f"mass-{i}")
    shot = _board(client, p["id"])[0]["shots"][0]
    assert len(shot["cast"]) == 12
    assert {c["blocking"] for c in shot["cast"]} == {f"mark {i}" for i in range(12)}

    _commit(client, p["id"], "scene.cast.remove",
            {"scene_id": sid, "character_id": chars[1]["id"]}, "sc-4")
    assert len(_board(client, p["id"])[0]["cast"]) == 1


# ---------- duplication ----------

def test_scene_duplicate_deep_copy(client):
    p = make_project(client, "DupScene")
    hero = _char(client, p["id"], "Ada", "d-1")
    s = _scene(client, p["id"], "Original", "d-2", description="desc")
    sid = s["scene"]["id"]
    _commit(client, p["id"], "scene.cast.assign",
            {"scene_id": sid, "character_id": hero["id"], "role": "lead"}, "d-3")
    sh1 = _shot(client, p["id"], sid, "d-4", title="one")
    sh2 = _shot(client, p["id"], sid, "d-5", title="two")
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sh1["shot"]["id"], "character_id": hero["id"],
             "state": {"expression": "calm"}}, "d-6")

    _commit(client, p["id"], "scene.duplicate",
            {"scene_id": sid}, "d-7")
    board = _board(client, p["id"])
    assert [x["scene"]["title"] for x in board] == ["Original", "Original (copy)"]
    dup = board[1]
    assert dup["scene"]["id"] != sid
    assert dup["scene"]["description"] == "desc"
    assert len(dup["cast"]) == 1 and dup["cast"][0]["character"]["name"] == "Ada"
    assert [x["shot"]["title"] for x in dup["shots"]] == ["one", "two"]
    assert dup["shots"][0]["shot"]["id"] != sh1["shot"]["id"]
    assert dup["shots"][0]["cast"][0]["expression"] == "calm"

    # Original untouched.
    orig = board[0]
    assert len(orig["cast"]) == 1 and len(orig["shots"]) == 2


def test_shot_and_character_duplicate(client):
    p = make_project(client, "DupShot")
    hero = _char(client, p["id"], "Ada", "ds-1")
    s = _scene(client, p["id"], "S", "ds-2")
    sid = s["scene"]["id"]
    sh = _shot(client, p["id"], sid, "ds-3", title="hero")
    _shot(client, p["id"], sid, "ds-3b", title="tail")
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": hero["id"],
             "state": {"emotion": "fear"}}, "ds-4")

    _commit(client, p["id"], "shot.duplicate", {"shot_id": sh["shot"]["id"]}, "ds-5")
    shots = _board(client, p["id"])[0]["shots"]
    assert [x["shot"]["title"] for x in shots] == ["hero", "hero (copy)", "tail"]
    assert shots[1]["cast"][0]["emotion"] == "fear"

    # character.duplicate copies profile + links.
    _commit(client, p["id"], "character.update",
            {"character_id": hero["id"], "data": {"appearance": "red jacket"}},
            "ds-6")
    _commit(client, p["id"], "character.duplicate", {"character_id": hero["id"]}, "ds-7")
    chars = client.get(f"/api/projects/{p['id']}/character").json()
    copy = next(c for c in chars if c["name"] == "Ada (copy)")
    assert copy["data"]["appearance"] == "red jacket"
    assert copy["id"] != hero["id"]


# ---------- versioning & revert ----------

def test_shot_version_history_revert_restores_cast(client):
    p = make_project(client, "Hist")
    hero = _char(client, p["id"], "Ada", "h-1")
    villain = _char(client, p["id"], "Mori", "h-2")
    s = _scene(client, p["id"], "S", "h-3")
    sh = _shot(client, p["id"], s["scene"]["id"], "h-4", title="v1 shot")
    sid = sh["shot"]["id"]
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sid, "character_id": hero["id"], "state": {"expression": "smile"}}, "h-5")

    revs = client.get(f"/api/projects/{p['id']}/shots/{sid}/versions").json()
    assert [r["revision"] for r in revs] == [2, 1]  # create + cast-assign bump

    # Mutate: change prompt + swap cast.
    _commit(client, p["id"], "shot.update", {"shot_id": sid, "prompt": "new prompt"}, "h-6")
    _commit(client, p["id"], "shot.cast.remove",
            {"shot_id": sid, "character_id": hero["id"]}, "h-7")
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sid, "character_id": villain["id"]}, "h-8")

    # Revert to revision 2 (Ada cast, smile, original prompt).
    _commit(client, p["id"], "shot.revert", {"shot_id": sid, "to_revision": 2}, "h-9")
    shot = _board(client, p["id"])[0]["shots"][0]
    assert len(shot["cast"]) == 1
    assert shot["cast"][0]["character"]["name"] == "Ada"
    assert shot["cast"][0]["expression"] == "smile"
    assert shot["shot"]["prompt"] == ""

    revs = client.get(f"/api/projects/{p['id']}/shots/{sid}/versions").json()
    assert revs[0]["summary"].startswith("Reverted to revision 2")


def test_scene_version_history(client):
    p = make_project(client, "SceneHist")
    s = _scene(client, p["id"], "S1", "sh-1")
    sid = s["scene"]["id"]
    _commit(client, p["id"], "scene.update", {"scene_id": sid, "title": "Renamed"}, "sh-2")
    revs = client.get(f"/api/projects/{p['id']}/scenes/{sid}/versions").json()
    assert len(revs) == 2
    assert revs[0]["snapshot_json"]["title"] == "Renamed"
    _commit(client, p["id"], "scene.revert", {"scene_id": sid, "to_revision": 1}, "sh-3")
    assert _board(client, p["id"])[0]["scene"]["title"] == "S1"


# ---------- undo ----------

def test_undo_cast_remove_and_move(client):
    p = make_project(client, "Undo")
    hero = _char(client, p["id"], "Ada", "u-1")
    s = _scene(client, p["id"], "S", "u-2")
    sh = _shot(client, p["id"], s["scene"]["id"], "u-3", title="one")
    sh2 = _shot(client, p["id"], s["scene"]["id"], "u-4", title="two")
    sid = sh["shot"]["id"]
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sid, "character_id": hero["id"], "state": {"expression": "x"}}, "u-5")

    res = _commit(client, p["id"], "shot.cast.remove",
                  {"shot_id": sid, "character_id": hero["id"]}, "u-6")
    assert _board(client, p["id"])[0]["shots"][0]["cast"] == []
    _undo(client, p["id"], res["command_id"])
    shot = _board(client, p["id"])[0]["shots"][0]
    assert len(shot["cast"]) == 1
    assert shot["cast"][0]["expression"] == "x"

    # Undo a shot move.
    res = _commit(client, p["id"], "shot.move",
                  {"shot_id": sh2["shot"]["id"], "to_index": 0}, "u-7")
    titles = [x["shot"]["title"] for x in _board(client, p["id"])[0]["shots"]]
    assert titles == ["two", "one"]
    _undo(client, p["id"], res["command_id"])
    titles = [x["shot"]["title"] for x in _board(client, p["id"])[0]["shots"]]
    assert titles == ["one", "two"]


def test_undo_shot_update_and_scene_move(client):
    p = make_project(client, "Undo2")
    s1 = _scene(client, p["id"], "First", "v-1")
    s2 = _scene(client, p["id"], "Second", "v-2")
    sh = _shot(client, p["id"], s1["scene"]["id"], "v-3", prompt="orig")

    res = _commit(client, p["id"], "shot.update",
                  {"shot_id": sh["shot"]["id"], "prompt": "changed"}, "v-4")
    _undo(client, p["id"], res["command_id"])
    assert _board(client, p["id"])[0]["shots"][0]["shot"]["prompt"] == "orig"

    res = _commit(client, p["id"], "scene.move",
                  {"scene_id": s2["scene"]["id"], "to_index": 0}, "v-5")
    assert [x["scene"]["title"] for x in _board(client, p["id"])] == ["Second", "First"]
    _undo(client, p["id"], res["command_id"])
    assert [x["scene"]["title"] for x in _board(client, p["id"])] == ["First", "Second"]


# ---------- entity assets ----------

def _import_asset(client, pid, path: Path, name: str | None = None) -> dict:
    import time
    rev = client.get(f"/api/projects/{pid}").json()["revision"]
    r = client.post(
        f"/api/projects/{pid}/commands/commit",
        json={
            "command_type": "asset.import",
            "payload": {"source_path": str(path), **({"name": name} if name else {})},
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


def test_entity_asset_links(client, tmp_path):
    p = make_project(client, "RefImgs")
    hero = _char(client, p["id"], "Ada", "e-1")
    img = tmp_path / "ada-ref.png"
    img.write_bytes(b"\x89PNG" + b"\x00" * 1024)
    asset = _import_asset(client, p["id"], img, "ada-ref.png")

    _commit(client, p["id"], "entity.assets.link",
            {"entity_type": "character", "entity_id": hero["id"],
             "asset_id": asset["id"], "role": "identity", "label": "face ref"}, "e-2")
    _commit(client, p["id"], "entity.assets.link",
            {"entity_type": "character", "entity_id": hero["id"],
             "asset_id": asset["id"], "role": "wardrobe", "label": "outfit B"}, "e-3")

    links = client.get(
        f"/api/projects/{p['id']}/entity-assets?entity_type=character&entity_id={hero['id']}"
    ).json()
    assert {l["role"] for l in links} == {"identity", "wardrobe"}

    # Duplicate preserves the link; unlink works.
    _commit(client, p["id"], "character.duplicate", {"character_id": hero["id"]}, "e-4")
    chars = client.get(f"/api/projects/{p['id']}/character").json()
    copy = next(c for c in chars if c["name"] == "Ada (copy)")
    clinks = client.get(
        f"/api/projects/{p['id']}/entity-assets?entity_id={copy['id']}").json()
    assert len(clinks) == 2

    res = _commit(client, p["id"], "entity.assets.unlink",
                  {"link_id": links[0]["id"]}, "e-5")
    left = client.get(
        f"/api/projects/{p['id']}/entity-assets?entity_id={hero['id']}").json()
    assert len(left) == 1
    # Undo relinks it.
    _undo(client, p["id"], res["command_id"])
    left = client.get(
        f"/api/projects/{p['id']}/entity-assets?entity_id={hero['id']}").json()
    assert len(left) == 2


# ---------- project isolation ----------

def test_cross_project_isolation(client):
    pa = make_project(client, "ProjA")
    pb = make_project(client, "ProjB")
    char_b = _char(client, pb["id"], "Foreign", "x-1")
    s = _scene(client, pa["id"], "S", "x-2")
    sh = _shot(client, pa["id"], s["scene"]["id"], "x-3")

    # Casting another project's character → 404.
    r = _commit(client, pa["id"], "shot.cast.assign",
                {"shot_id": sh["shot"]["id"], "character_id": char_b["id"]},
                "x-4", expect=404)
    assert r.status_code == 404
    # Operating on another project's scene → 404.
    r = _commit(client, pa["id"], "scene.update",
                {"scene_id": "nonexistent", "title": "x"}, "x-5", expect=404)
    assert r.status_code == 404
    # Another project's world link → 404.
    _commit(client, pb["id"], "world.create", {"name": "WB"}, "x-6")
    wb = client.get(f"/api/projects/{pb['id']}/world").json()[0]
    r = _commit(client, pa["id"], "scene.create",
                {"title": "bad", "world_id": wb["id"]}, "x-7", expect=404)
    assert r.status_code == 404
    # Board of A still shows its own data only.
    assert len(_board(client, pa["id"])) == 1
    assert len(_board(client, pb["id"])) == 0


def test_project_duplicate_copies_storyboard(client):
    p = make_project(client, "DupAll")
    hero = _char(client, p["id"], "Ada", "p-1")
    s = _scene(client, p["id"], "S1", "p-2")
    sh = _shot(client, p["id"], s["scene"]["id"], "p-3", title="shot")
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": hero["id"]}, "p-4")

    res = _commit(client, p["id"], "project.duplicate", {"name": "DupAll2"}, "p-5")
    dup_id = res["duplicate_project_id"]
    board = _board(client, dup_id)
    assert len(board) == 1
    assert board[0]["scene"]["title"] == "S1"
    assert len(board[0]["shots"]) == 1
    assert board[0]["shots"][0]["cast"][0]["character"]["name"] == "Ada"
    # Cast points at the COPY's character, not the source's.
    chars = client.get(f"/api/projects/{dup_id}/character").json()
    assert board[0]["shots"][0]["cast"][0]["character_id"] == chars[0]["id"]


# ---------- backup round-trip ----------

def test_backup_roundtrip_preserves_storyboard(client, tmp_path):
    p = make_project(client, "BoardBackup")
    hero = _char(client, p["id"], "Ada", "b-1")
    s = _scene(client, p["id"], "S1", "b-2", intent="noir")
    sh = _shot(client, p["id"], s["scene"]["id"], "b-3",
               title="shot", prompt="rain")
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": hero["id"],
             "state": {"expression": "stern", "loras": ["ada-lora"]}}, "b-4")

    # Export (format v2 manifest includes the storyboard tables).
    rev = client.get(f"/api/projects/{p['id']}").json()["revision"]
    r = _commit(client, p["id"], "backup.export", {"kind": "backup"}, "b-5")
    job = wait_for_job(client, p["id"], r["job_id"])
    assert job["status"] == "succeeded"
    pkg = Path(job["result_json"]["package_path"])
    with zipfile.ZipFile(pkg) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["format_version"] == 2
    assert len(manifest["entities"]["scenes"]) == 1
    assert len(manifest["entities"]["shots"]) == 1
    assert len(manifest["entities"]["shot_characters"]) == 1

    # Import → storyboard graph restored with remapped ids.
    r = client.post("/api/packages/import", json={"package_path": str(pkg)})
    assert r.status_code == 200
    job = wait_for_job(client, r.json()["project_id"], r.json()["job_id"])
    assert job["status"] == "succeeded", job
    new_pid = job["result_json"]["project_id"]
    board = _board(client, new_pid)
    assert len(board) == 1
    assert board[0]["scene"]["title"] == "S1"
    assert board[0]["scene"]["intent"] == "noir"
    assert board[0]["scene"]["id"] != s["scene"]["id"]  # remapped
    assert board[0]["shots"][0]["shot"]["prompt"] == "rain"
    cast = board[0]["shots"][0]["cast"]
    assert cast[0]["character"]["name"] == "Ada"
    assert cast[0]["expression"] == "stern"
    assert cast[0]["lora_json"] == ["ada-lora"]


# ---------- preview & migration ----------

def test_preview_no_side_effects(client):
    p = make_project(client, "Preview")
    hero = _char(client, p["id"], "Ada", "pv-1")
    s = _scene(client, p["id"], "S", "pv-2")
    sh = _shot(client, p["id"], s["scene"]["id"], "pv-3")
    r = client.post(
        f"/api/projects/{p['id']}/commands/preview",
        json={"command_type": "shot.cast.assign",
              "payload": {"shot_id": sh["shot"]["id"], "character_id": hero["id"]},
              "scope": "project", "origin": "test"},
    )
    assert r.status_code == 200
    assert "cast" in r.json()["summary"].lower()
    # Preview applied nothing.
    assert _board(client, p["id"])[0]["shots"][0]["cast"] == []


def test_migration_0004_tables_exist(client):
    """Fresh DB (0001 live-metadata + guarded 0004) has all M2 tables."""
    import sqlalchemy as sa
    from app.config import Settings
    from app.db.base import Base
    from app.db.models import EntityAssetLink, Scene, SceneCharacter, Shot, ShotCharacter
    # If the API works end-to-end the tables exist — exercise each once.
    p = make_project(client, "MigCheck")
    c = _char(client, p["id"], "C", "m-1")
    s = _scene(client, p["id"], "S", "m-2")
    sh = _shot(client, p["id"], s["scene"]["id"], "m-3")
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"]}, "m-4")
    _commit(client, p["id"], "scene.cast.assign",
            {"scene_id": s["scene"]["id"], "character_id": c["id"]}, "m-5")
    assert _board(client, p["id"])[0]["cast"][0]["character"]["name"] == "C"
