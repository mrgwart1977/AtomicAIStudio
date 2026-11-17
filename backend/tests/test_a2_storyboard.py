"""A2 — shot planning + storyboard board.

Coverage: planning_json schema validation (all ten camera-follow
modes, enums, rational fps, project-scoped refs), persistence and
merge/clear semantics, revision/undo/duplicate/copy integrity, CAS
media association on shots incl. missing-media flags, screenplay-sync
preservation of planning (A2.6), ordering/duplication workflow and
backup round-trip.
"""
import pytest
from sqlalchemy import select

from tests.conftest import make_project
from tests.test_c4b_shot_generate import _commit, _preview, _rev
from tests.test_a1_screenplay import (
    _character, _location, _film, _scaffold, _sync, _detail,
    _storyboard, _flat_shots, _set_elements, _action, _dialogue)


# ---------- helpers ----------

def _board(client, pid):
    return _storyboard(client, pid)


def _scene2(client, pid, title="S1"):
    _commit(client, pid, "scene.create", {"title": title})
    return next(s for s in _board(client, pid) if s["scene"]["title"] == title)


def _shot2(client, pid, scene_id, title="wide", **kw):
    _commit(client, pid, "shot.create",
            {"scene_id": scene_id, "title": title, **kw})
    scene = next(s for s in _board(client, pid)
                 if s["scene"]["id"] == scene_id)
    return next(sh for sh in scene["shots"] if sh["shot"]["title"] == title)


def _entry(client, pid, shot_id):
    for sc in _board(client, pid):
        for e in sc["shots"]:
            if e["shot"]["id"] == shot_id:
                return e
    raise AssertionError("shot not on board")


def _import_image(client, pid, tmp_path, name="ref"):
    from PIL import Image
    png = tmp_path / f"{name}.png"
    Image.new("RGB", (8, 8), (32, 64, 96)).save(png)
    _commit(client, pid, "asset.import",
            {"source_path": str(png), "name": name, "kind": "image"})
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    return next(a for a in items if a["name"] == name)


def _undo(client, pid, command_id):
    r = client.post(f"/api/projects/{pid}/commands/{command_id}/undo")
    assert r.status_code == 200, r.text
    return r.json()


# ---------- planning schema ----------

def test_planning_persists_and_validates(client):
    pid = make_project(client)["id"]
    anna = _character(client, pid, "Anna")
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    plan = {
        "camera": {
            "angle": "low", "movement": "dolly_in",
            "follow_mode": "follow_behind",
            "follow_params": {"distance_m": 3, "height_m": 1.6},
            "follow_targets": [{"kind": "character", "id": anna["id"]}],
        },
        "framing": "medium_wide",
        "subject_placement": "left_third",
        "lighting": "moon + practical",
        "action_notes": "she waits, then turns",
        "fps": {"num": 24000, "den": 1001, "frames": 96},
        "continuity_refs": [{"kind": "character", "id": anna["id"]}],
        "notes": "board note",
    }
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "planning": plan})
    got = _entry(client, pid, sh["shot"]["id"])["shot"]["planning_json"]
    assert got["camera"]["follow_mode"] == "follow_behind"
    assert got["camera"]["angle"] == "low"
    assert got["camera"]["follow_params"]["distance_m"] == 3
    assert got["camera"]["follow_targets"] == [
        {"kind": "character", "id": anna["id"]}]
    assert got["framing"] == "medium_wide"
    assert got["fps"] == {"num": 24000, "den": 1001, "frames": 96}
    assert got["notes"] == "board note"


def test_all_ten_follow_modes_accepted(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    for mode in ("follow_behind", "follow_front", "follow_side",
                 "follow_shoulder", "orbit_follow", "drone_follow",
                 "vehicle_chase", "lead_and_follow", "group_follow",
                 "target_switch"):
        _commit(client, pid, "shot.update",
                {"shot_id": sh["shot"]["id"],
                 "planning": {"camera": {"follow_mode": mode}}})
    got = _entry(client, pid, sh["shot"]["id"])["shot"]["planning_json"]
    assert got["camera"]["follow_mode"] == "target_switch"


@pytest.mark.parametrize("bad", [
    {"camera": {"follow_mode": "spinny"}},
    {"camera": {"angle": "worm"}},
    {"camera": {"movement": "zoom_zoom"}},
    {"framing": "kinda_close"},
    {"subject_placement": "wherever"},
    {"fps": {"num": 24, "den": 0}},
    {"fps": {"num": 24.5, "den": 1}},
    {"camera": {"follow_targets": [{"kind": "prop", "id": "x"}]}},
    {"camera": {"follow_targets": [{"kind": "character", "id": "ghost"}]}},
    {"continuity_refs": [{"kind": "asset", "id": "ghost"}]},
    "not a dict",
])
def test_planning_rejects_bad_values(client, bad):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "planning": bad}, expect=422)


def test_planning_merge_and_clear_semantics(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "planning": {
                "camera": {"angle": "low", "movement": "pan"},
                "notes": "first"}})
    # partial update: notes + camera subfield merge, siblings kept
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "planning": {
                "camera": {"movement": "tilt"}, "lighting": "dusk"}})
    got = _entry(client, pid, sh["shot"]["id"])["shot"]["planning_json"]
    assert got["camera"] == {"angle": "low", "movement": "tilt"}
    assert got["notes"] == "first" and got["lighting"] == "dusk"
    # explicit null clears a subfield / a top key
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "planning": {
                "camera": {"angle": None}, "notes": None}})
    got = _entry(client, pid, sh["shot"]["id"])["shot"]["planning_json"]
    assert got["camera"] == {"movement": "tilt"}
    assert "notes" not in got and got["lighting"] == "dusk"


def test_cross_project_follow_target_rejected(client):
    pid = make_project(client)["id"]
    other = make_project(client, "Other")["id"]
    foreign = _character(client, other, "Foreign")
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "planning": {
                "camera": {"follow_targets": [
                    {"kind": "character", "id": foreign["id"]}]}}},
            expect=422)


def test_planning_on_create(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    _commit(client, pid, "shot.create",
            {"scene_id": sc["scene"]["id"], "title": "planned",
             "planning": {"camera": {"angle": "high"},
                          "notes": "at birth"}})
    sh = _shot2(client, pid, sc["scene"]["id"], title="planned")
    assert sh["shot"]["planning_json"]["camera"]["angle"] == "high"
    # invalid planning rejects the create at validation time
    _commit(client, pid, "shot.create",
            {"scene_id": sc["scene"]["id"],
             "planning": {"camera": {"angle": "worm"}}}, expect=422)


def test_label_follow_target_for_non_character(client):
    """vehicle_chase targets needn't be characters (spec) — a free
    label covers props/vehicles without inventing entity types."""
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "planning": {
                "camera": {"follow_mode": "vehicle_chase",
                           "follow_targets": [
                               {"kind": "label", "name": "red pickup"}]}}})
    got = _entry(client, pid, sh["shot"]["id"])["shot"]["planning_json"]
    assert got["camera"]["follow_targets"] == [
        {"kind": "label", "name": "red pickup"}]


# ---------- revisions, undo, duplicate ----------

def test_planning_survives_revert_and_undo(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    sid = sh["shot"]["id"]
    _commit(client, pid, "shot.update",
            {"shot_id": sid, "planning": {"notes": "v1"}})
    res = _commit(client, pid, "shot.update",
                  {"shot_id": sid, "planning": {"notes": "v2"}})
    # generic command undo restores planning (and the previously
    # un-restored binding fields — regression for the fixed list)
    _undo(client, pid, res["command_id"])
    assert _entry(client, pid, sid)["shot"]["planning_json"]["notes"] == "v1"
    # shot.revert also restores planning from the revision snapshot
    hist = client.get(
        f"/api/projects/{pid}/shots/{sid}/versions").json()
    rev1 = next(h for h in hist if h["revision"] == 1)
    _commit(client, pid, "shot.revert",
            {"shot_id": sid, "to_revision": rev1["revision"]})
    e = _entry(client, pid, sid)
    assert "notes" not in (e["shot"]["planning_json"] or {})


def test_undo_restores_source_asset_binding(client):
    """Regression: shot.update undo used to skip reference_set_id /
    source_asset_id — they must restore with the snapshot."""
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    sid = sh["shot"]["id"]
    res = _commit(client, pid, "shot.update",
                  {"shot_id": sid, "source_asset_id": None,
                   "planning": {"notes": "keep"}})
    _undo(client, pid, res["command_id"])
    e = _entry(client, pid, sid)
    assert e["shot"]["source_asset_id"] is None
    assert "notes" not in (e["shot"]["planning_json"] or {})


def test_duplicate_copies_planning(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "planning": {
                "camera": {"follow_mode": "orbit_follow"},
                "notes": "dup me"}})
    _commit(client, pid, "shot.duplicate",
            {"shot_id": sh["shot"]["id"], "title": "copy"})
    dup = _shot2(client, pid, sc["scene"]["id"], title="copy")
    assert dup["shot"]["planning_json"]["camera"]["follow_mode"] == \
        "orbit_follow"
    assert dup["shot"]["planning_json"]["notes"] == "dup me"


# ---------- CAS media association ----------

def test_shot_asset_link_and_missing_media(client, tmp_path):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    sid = sh["shot"]["id"]
    a = _import_image(client, pid, tmp_path)
    # link a CAS image to the shot (board reference)
    _commit(client, pid, "entity.assets.link",
            {"entity_type": "shot", "entity_id": sid,
             "asset_id": a["id"], "role": "reference",
             "label": "angle ref"})
    e = _entry(client, pid, sid)
    assert e["media"]["assets"][0]["asset_id"] == a["id"]
    assert e["media"]["assets"][0]["missing"] is False
    assert e["media"]["missing"] == []
    # mark the blob missing → card reports it for relinking
    eng = client.app.state.engine
    from app.db.models import Asset
    with eng.begin() as con:
        con.execute(select(Asset).where(Asset.id == a["id"]).with_for_update())
        con.exec_driver_sql(
            "UPDATE assets SET status='missing' WHERE id=:i",
            {"i": a["id"]})
    e = _entry(client, pid, sid)
    assert e["media"]["assets"][0]["missing"] is True
    assert e["media"]["missing"] == [e["media"]["assets"][0]["link_id"]]
    # relink to a fresh asset clears the flag
    b = _import_image(client, pid, tmp_path, name="ref2")
    _commit(client, pid, "entity.assets.unlink",
            {"link_id": e["media"]["assets"][0]["link_id"]})
    _commit(client, pid, "entity.assets.link",
            {"entity_type": "shot", "entity_id": sid,
             "asset_id": b["id"], "role": "reference"})
    e = _entry(client, pid, sid)
    assert e["media"]["assets"][0]["asset_id"] == b["id"]
    assert e["media"]["missing"] == []


def test_shot_asset_link_isolation(client, tmp_path):
    pid = make_project(client)["id"]
    other = make_project(client, "Other")["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    a = _import_image(client, pid, tmp_path)
    # foreign shot id
    _commit(client, pid, "entity.assets.link",
            {"entity_type": "shot", "entity_id": "nope",
             "asset_id": a["id"]}, expect=404)
    # foreign asset id
    b = _import_image(client, other, tmp_path, name="other-ref")
    _commit(client, pid, "entity.assets.link",
            {"entity_type": "shot", "entity_id": sh["shot"]["id"],
             "asset_id": b["id"]}, expect=404)


def test_thumbnail_binding_missing_flag(client, tmp_path):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    sid = sh["shot"]["id"]
    a = _import_image(client, pid, tmp_path)
    _commit(client, pid, "shot.update",
            {"shot_id": sid, "thumbnail_asset_id": a["id"]})
    assert _entry(client, pid, sid)["media"]["missing"] == []
    eng = client.app.state.engine
    with eng.begin() as con:
        con.exec_driver_sql(
            "UPDATE assets SET status='deleted' WHERE id=:i", {"i": a["id"]})
    assert _entry(client, pid, sid)["media"]["missing"] == ["thumbnail"]


# ---------- ordering workflow ----------

def test_move_and_duplicate_ordering(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sid = sc["scene"]["id"]
    a = _shot2(client, pid, sid, title="a")
    _shot2(client, pid, sid, title="b")
    _shot2(client, pid, sid, title="c")
    _commit(client, pid, "shot.move",
            {"shot_id": a["shot"]["id"], "to_index": 2})
    scene = next(s for s in _board(client, pid)
                 if s["scene"]["id"] == sid)
    assert [e["shot"]["title"] for e in scene["shots"]] == ["b", "c", "a"]
    # undo the move via generic command undo
    cmds = client.get(f"/api/projects/{pid}/commands").json()
    mv = next(c for c in cmds if c["command_type"] == "shot.move")
    _undo(client, pid, mv["id"])
    scene = next(s for s in _board(client, pid)
                 if s["scene"]["id"] == sid)
    assert [e["shot"]["title"] for e in scene["shots"]] == ["a", "b", "c"]


# ---------- A2.6 screenplay integration ----------

def test_sync_preserves_planning_on_scaffolded_shots(client):
    """Planning is outside the A1 managed set: an APPROVED overwrite of
    scaffold fields still leaves camera/framing/notes untouched."""
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    shot = _flat_shots(_storyboard(client, pid))[1]
    _commit(client, pid, "shot.update",
            {"shot_id": shot["id"], "planning": {
                "camera": {"follow_mode": "follow_side",
                           "follow_params": {"lateral_m": 2}},
                "framing": "close_up", "notes": "manual plan"},
             "title": "USER TITLE"})  # managed-field edit → manual flag
    sc = _detail(client, pid, f["spid"])["scenes"][0]
    els = sc["elements"]
    _set_elements(client, pid, sc["id"],
                  [{**els[0], "text": "new line"}, els[1], els[2]])
    _sync(client, pid, f["spid"], approve_overwrite=[shot["id"]])
    s = next(x for x in _flat_shots(_storyboard(client, pid))
             if x["id"] == shot["id"])
    pj = s["planning_json"]
    assert pj["camera"]["follow_mode"] == "follow_side"
    assert pj["camera"]["follow_params"] == {"lateral_m": 2}
    assert pj["framing"] == "close_up" and pj["notes"] == "manual plan"
    assert s["title"] != "USER TITLE"  # approved managed overwrite ran


def test_board_reorder_keeps_screenplay_associations(client):
    """Shot reorder/duplicate never touches the screenplay-scene →
    scene link or the shotlist group keys."""
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    doc_before = _detail(client, pid, f["spid"])
    shots = _flat_shots(_storyboard(client, pid))
    _commit(client, pid, "shot.move",
            {"shot_id": shots[1]["id"], "to_index": 0})
    _commit(client, pid, "shot.duplicate", {"shot_id": shots[2]["id"]})
    doc_after = _detail(client, pid, f["spid"])
    assert [s["scene_id"] for s in doc_after["scenes"]] == \
        [s["scene_id"] for s in doc_before["scenes"]]
    # moved shot still carries its shotlist identity metadata
    moved = next(x for x in _flat_shots(_storyboard(client, pid))
                 if x["id"] == shots[1]["id"])
    assert (moved["continuity_json"] or {}).get("shotlist", {}).get(
        "group_key")


# ---------- backup + restart ----------

def test_planning_in_backup_round_trip(client, tmp_path):
    from tests.conftest import wait_for_job
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "planning": {
                "camera": {"follow_mode": "drone_follow"},
                "notes": "backup me"}})
    _commit(client, pid, "backup.export", {"kind": "backup"})
    job = client.get(f"/api/projects/{pid}/jobs").json()[0]
    done = wait_for_job(client, pid, job["id"], timeout=30)
    assert done["status"] == "succeeded", done
    pkg = done["result_json"]["package_path"]
    r = client.post("/api/packages/import",
                    json={"package_path": str(pkg)})
    assert r.status_code == 200, r.text
    new_pid = r.json()["project_id"]
    wait_for_job(client, new_pid, r.json()["job_id"], timeout=30)
    board = client.get(
        f"/api/projects/{new_pid}/storyboard").json()
    imp = board[0]["shots"][0]["shot"]["planning_json"]
    assert imp["camera"]["follow_mode"] == "drone_follow"
    assert imp["notes"] == "backup me"
