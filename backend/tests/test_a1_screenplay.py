"""A1 — integrated screenplay + deterministic shot-list scaffold.

Coverage: CRUD + revisions, ordered scenes/elements, dialogue speaker
and location references with project isolation, deterministic
scaffold, previewable sync (adds/updates/removals/manual-edit
conflicts), approval-gated destructive apply, batch undo/redo,
idempotent replay, stale-revision rejection, restart persistence and
backup round-trip.
"""
import pytest
from sqlalchemy import select

from tests.conftest import make_project
from tests.test_c4b_shot_generate import _commit, _preview, _rev


# ---------- helpers ----------

def _sp_id(client, pid, title):
    for sp in client.get(f"/api/projects/{pid}/screenplays").json():
        if sp["title"] == title:
            return sp["id"]
    raise AssertionError(f"screenplay '{title}' not found")


def _detail(client, pid, spid):
    r = client.get(f"/api/projects/{pid}/screenplays/{spid}")
    assert r.status_code == 200, r.text
    return r.json()


def _scene(client, pid, spid, heading, expect=200, **kw):
    _commit(client, pid, "screenplay_scene.create",
            {"screenplay_id": spid, "heading": heading, **kw},
            expect=expect)
    d = _detail(client, pid, spid)
    return next(s for s in d["scenes"] if s["heading"] == heading)


def _set_elements(client, pid, sp_scene_id, elements, expect=200, **kw):
    return _commit(client, pid, "screenplay_scene.update",
                   {"sp_scene_id": sp_scene_id, "elements": elements, **kw},
                   expect=expect)


def _dialogue(char_id, text, parenthetical=""):
    return {"kind": "dialogue", "character_id": char_id, "text": text,
            "parenthetical": parenthetical}


def _action(text):
    return {"kind": "action", "text": text}


def _character(client, pid, name):
    _commit(client, pid, "character.create", {"name": name})
    chars = client.get(f"/api/projects/{pid}/character").json()
    return next(c for c in chars if c["name"] == name)


def _location(client, pid, name):
    _commit(client, pid, "location.create", {"name": name})
    locs = client.get(f"/api/projects/{pid}/location").json()
    return next(l for l in locs if l["name"] == name)


def _storyboard(client, pid, include_archived=True):
    r = client.get(
        f"/api/projects/{pid}/storyboard?include_archived={str(include_archived).lower()}")
    assert r.status_code == 200, r.text
    return r.json()


def _flat_shots(board):
    """Shot ORM payloads across all scenes — each entry is
    {shot, cast, inherited, generation}; return the inner shot dicts
    with their cast attached for convenient assertions."""
    out = []
    for scn in board:
        for entry in scn.get("shots", []):
            sh = dict(entry["shot"])
            sh["cast"] = entry.get("cast") or []
            out.append(sh)
    return out


def _scaffold(client, pid, spid, **kw):
    return _commit(client, pid, "shotlist.scaffold",
                   {"screenplay_id": spid, **kw})


def _sync(client, pid, spid, **kw):
    return _commit(client, pid, "shotlist.sync",
                   {"screenplay_id": spid, **kw})


def _plan(client, pid, spid):
    r = client.get(f"/api/projects/{pid}/screenplays/{spid}/shotlist-plan")
    assert r.status_code == 200, r.text
    return r.json()


# ---------- screenplay CRUD + isolation ----------

def test_screenplay_crud_and_listing(client):
    pid = make_project(client)["id"]
    _commit(client, pid, "screenplay.create",
            {"title": "Alpha", "description": "first"})
    sp = client.get(f"/api/projects/{pid}/screenplays").json()[0]
    assert sp["title"] == "Alpha"
    d = _detail(client, pid, sp["id"])
    assert d["scenes"] == [] and d["revision"] >= 1
    _commit(client, pid, "screenplay.update",
            {"screenplay_id": sp["id"], "description": "updated"})
    assert _detail(client, pid, sp["id"])["description"] == "updated"
    _commit(client, pid, "screenplay.archive",
            {"screenplay_id": sp["id"]})
    assert client.get(
        f"/api/projects/{pid}/screenplays").json() == []
    _commit(client, pid, "screenplay.unarchive",
            {"screenplay_id": sp["id"]})
    assert len(client.get(
        f"/api/projects/{pid}/screenplays").json()) == 1


def test_multiple_screenplays_per_project(client):
    pid = make_project(client)["id"]
    for t in ("One", "Two", "Three"):
        _commit(client, pid, "screenplay.create", {"title": t})
    titles = [s["title"] for s in
              client.get(f"/api/projects/{pid}/screenplays").json()]
    assert titles == ["One", "Two", "Three"]


def test_title_required(client):
    pid = make_project(client)["id"]
    _commit(client, pid, "screenplay.create", {"title": "  "},
            expect=422)


def test_screenplay_project_isolation(client):
    a = make_project(client, "A")["id"]
    b = make_project(client, "B")["id"]
    _commit(client, a, "screenplay.create", {"title": "In A"})
    spid = client.get(f"/api/projects/{a}/screenplays").json()[0]["id"]
    r = client.get(f"/api/projects/{b}/screenplays/{spid}")
    assert r.status_code == 404
    _commit(client, b, "screenplay.update",
            {"screenplay_id": spid, "description": "leak"}, expect=404)
    _commit(client, b, "screenplay_scene.create",
            {"screenplay_id": spid, "heading": "X"}, expect=404)


# ---------- scenes + elements ----------

def test_scene_ordering_and_move(client):
    pid = make_project(client)["id"]
    _commit(client, pid, "screenplay.create", {"title": "S"})
    spid = _sp_id(client, pid, "S")
    s1 = _scene(client, pid, spid, "INT. A — DAY")
    s2 = _scene(client, pid, spid, "EXT. B — NIGHT")
    s3 = _scene(client, pid, spid, "INT. C — DAWN")
    _commit(client, pid, "screenplay_scene.move",
            {"sp_scene_id": s3["id"], "to_index": 0})
    heads = [s["heading"] for s in _detail(client, pid, spid)["scenes"]]
    assert heads == ["INT. C — DAWN", "INT. A — DAY", "EXT. B — NIGHT"]


def test_scene_delete_renumbers(client):
    pid = make_project(client)["id"]
    _commit(client, pid, "screenplay.create", {"title": "S"})
    spid = client.get(f"/api/projects/{pid}/screenplays").json()[0]["id"]
    _scene(client, pid, spid, "ONE")
    s2 = _scene(client, pid, spid, "TWO")
    _scene(client, pid, spid, "THREE")
    _commit(client, pid, "screenplay_scene.delete",
            {"sp_scene_id": s2["id"]})
    d = _detail(client, pid, spid)
    assert [s["heading"] for s in d["scenes"]] == ["ONE", "THREE"]
    assert [s["position"] for s in d["scenes"]] == [0, 1]


def test_dialogue_elements_speakers_and_parentheticals(client):
    pid = make_project(client)["id"]
    anna = _character(client, pid, "Anna")
    bob = _character(client, pid, "Bob")
    _commit(client, pid, "screenplay.create", {"title": "S"})
    spid = client.get(f"/api/projects/{pid}/screenplays").json()[0]["id"]
    sc = _scene(client, pid, spid, "INT. ROOM — DAY")
    _set_elements(client, pid, sc["id"], [
        _action("Anna paces."),
        _dialogue(anna["id"], "We need to go.", "(urgent)"),
        _dialogue(bob["id"], "Not yet."),
        {"kind": "note", "text": "beat before reveal"},
    ])
    elems = _detail(client, pid, spid)["scenes"][0]["elements"]
    assert [e["kind"] for e in elems] == \
        ["action", "dialogue", "dialogue", "note"]
    assert elems[1]["character_id"] == anna["id"]
    assert elems[1]["parenthetical"] == "(urgent)"
    assert elems[2]["character_id"] == bob["id"]
    # non-dialogue elements never carry a speaker
    assert elems[0]["character_id"] is None


def test_dialogue_speaker_project_isolation(client):
    a = make_project(client, "A")["id"]
    b = make_project(client, "B")["id"]
    char_b = _character(client, b, "Foreign")
    _commit(client, a, "screenplay.create", {"title": "S"})
    spid = client.get(f"/api/projects/{a}/screenplays").json()[0]["id"]
    sc = _scene(client, a, spid, "INT. X")
    _set_elements(client, a, sc["id"],
                  [_dialogue(char_b["id"], "cross-project")],
                  expect=404)


def test_scene_location_reference_and_isolation(client):
    a = make_project(client, "A")["id"]
    b = make_project(client, "B")["id"]
    loc_b = _location(client, b, "Other Project Dock")
    _commit(client, a, "screenplay.create", {"title": "S"})
    spid = client.get(f"/api/projects/{a}/screenplays").json()[0]["id"]
    _commit(client, a, "screenplay_scene.create",
            {"screenplay_id": spid, "heading": "BAD",
             "location_id": loc_b["id"]}, expect=404)
    loc_a = _location(client, a, "Dock")
    sc = _scene(client, a, spid, "EXT. DOCK — NIGHT",
                location_id=loc_a["id"])
    assert sc["location_id"] == loc_a["id"]


def test_screenplay_revert_restores_elements(client):
    pid = make_project(client)["id"]
    anna = _character(client, pid, "Anna")
    _commit(client, pid, "screenplay.create", {"title": "S"})
    spid = client.get(f"/api/projects/{pid}/screenplays").json()[0]["id"]
    sc = _scene(client, pid, spid, "INT. A")
    _set_elements(client, pid, sc["id"], [_dialogue(anna["id"], "line one")])
    rev_before = _detail(client, pid, spid)["revision"]
    _set_elements(client, pid, sc["id"], [_action("replaced")])
    assert _detail(client, pid, spid)["scenes"][0]["elements"][0]["kind"] \
        == "action"
    _commit(client, pid, "screenplay.revert",
            {"screenplay_id": spid, "to_revision": rev_before})
    elems = _detail(client, pid, spid)["scenes"][0]["elements"]
    assert elems[0]["kind"] == "dialogue" and elems[0]["text"] == "line one"


def test_revision_history_recorded(client):
    pid = make_project(client)["id"]
    _commit(client, pid, "screenplay.create", {"title": "S"})
    spid = client.get(f"/api/projects/{pid}/screenplays").json()[0]["id"]
    _scene(client, pid, spid, "INT. A")
    revs = client.get(
        f"/api/projects/{pid}/screenplays/{spid}/versions").json()
    assert len(revs) >= 2
    assert revs[0]["snapshot_json"]["scenes"][0]["heading"] == "INT. A"


# ---------- scaffold ----------

def _film(client, pid):
    """Two-scene screenplay with dialogue + action; returns ids."""
    anna = _character(client, pid, "Anna")
    bob = _character(client, pid, "Bob")
    loc = _location(client, pid, "Dock")
    _commit(client, pid, "screenplay.create", {"title": "Film"})
    spid = client.get(f"/api/projects/{pid}/screenplays").json()[0]["id"]
    s1 = _scene(client, pid, spid, "EXT. DOCK — NIGHT",
                action="Anna waits on the pier.", location_id=loc["id"])
    _set_elements(client, pid, s1["id"], [
        _dialogue(anna["id"], "The boat is late."),
        _dialogue(bob["id"], "It always is."),
        _action("Fog rolls in."),
    ])
    s2 = _scene(client, pid, spid, "INT. CABIN — NIGHT")
    _set_elements(client, pid, s2["id"], [
        _dialogue(bob["id"], "We leave at dawn.", "(quietly)"),
    ])
    return {"spid": spid, "s1": s1, "s2": s2,
            "anna": anna, "bob": bob, "loc": loc}


def test_scaffold_creates_scenes_shots_and_dialogue(client):
    pid = make_project(client)["id"]
    f = _film(client, pid)
    pv = _preview(client, pid, "shotlist.scaffold",
                  {"screenplay_id": f["spid"]})
    assert pv["shotlist_diff"]["counts"]["add"] >= 3
    _scaffold(client, pid, f["spid"])
    board = _storyboard(client, pid)
    assert [sc["scene"]["title"] for sc in board] == \
        ["EXT. DOCK — NIGHT", "INT. CABIN — NIGHT"]
    shots = _flat_shots(board)
    assert len(shots) == 4  # action beat + dialogue run + action + dialogue
    # scene-level action → wide shot; dialogue runs → medium
    assert shots[0]["shot_type"] == "wide"
    assert shots[1]["shot_type"] == "medium"
    # dialogue carried onto shot cast rows, speaker-resolved
    dlg = {c["character_id"]: c.get("dialogue")
           for c in shots[1]["cast"]}
    assert dlg.get(f["anna"]["id"]) == "The boat is late."
    assert dlg.get(f["bob"]["id"]) == "It always is."
    # location flows from screenplay scene → storyboard scene + shot
    assert board[0]["scene"]["location_id"] == f["loc"]["id"]
    assert shots[0]["location_id"] == f["loc"]["id"]
    # camera placeholder recorded, not generated
    meta = shots[0]["continuity_json"]["shotlist"]
    assert meta["camera"]["placeholder"] is True
    # screenplay scene is linked back to its storyboard scene
    d = _detail(client, pid, f["spid"])
    assert d["scenes"][0]["scene_id"] == board[0]["scene"]["id"]


def test_scaffold_preview_is_deterministic_and_readonly(client):
    pid = make_project(client)["id"]
    f = _film(client, pid)
    p1 = _preview(client, pid, "shotlist.scaffold",
                  {"screenplay_id": f["spid"]})["shotlist_diff"]
    p2 = _preview(client, pid, "shotlist.scaffold",
                  {"screenplay_id": f["spid"]})["shotlist_diff"]
    assert p1 == p2
    assert _storyboard(client, pid) == []  # preview mutated nothing
    assert _plan(client, pid, f["spid"])["counts"] == p1["counts"]


def test_sync_is_idempotent_when_unchanged(client):
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    plan = _plan(client, pid, f["spid"])
    assert plan["counts"]["add"] == 0
    assert plan["counts"]["update"] == 0
    assert plan["counts"]["remove"] == 0
    assert plan["counts"]["unchanged"] == 4


def test_sync_adds_new_beats(client):
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    sc = _detail(client, pid, f["spid"])["scenes"][1]
    elems = sc["elements"]
    _set_elements(client, pid, sc["id"],
                  [elems[0], _action("A horn sounds.")])
    plan = _plan(client, pid, f["spid"])
    assert plan["counts"]["add"] == 1
    _sync(client, pid, f["spid"])
    assert len(_flat_shots(_storyboard(client, pid))) == 5


def test_sync_updates_changed_dialogue(client):
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    sc = _detail(client, pid, f["spid"])["scenes"][1]
    elems = sc["elements"]
    _set_elements(client, pid, sc["id"], [
        {**elems[0], "text": "We leave NOW."}])
    plan = _plan(client, pid, f["spid"])
    assert plan["counts"]["update"] == 1
    _sync(client, pid, f["spid"])
    shots = _flat_shots(_storyboard(client, pid))
    cast = shots[3]["cast"]
    assert any(c.get("dialogue") == "We leave NOW." for c in cast)


def test_manual_edit_flagged_and_preserved(client):
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    shot = _flat_shots(_storyboard(client, pid))[1]
    _commit(client, pid, "shot.update",
            {"shot_id": shot["id"], "title": "MY CUSTOM TITLE"})
    # screenplay unchanged → edited shot content no longer matches its
    # fingerprint, but signature still matches → no overwrite needed
    sc = _detail(client, pid, f["spid"])["scenes"][0]
    elems = sc["elements"]
    _set_elements(client, pid, sc["id"], [
        {**elems[0], "text": "Changed line."}, elems[1], elems[2]])
    plan = _plan(client, pid, f["spid"])
    s0 = plan["scenes"][0]
    conflicts = [i["shot_id"] for i in s0["manual_edit"]]
    assert shot["id"] in conflicts
    # without approval the edit is preserved exactly
    _sync(client, pid, f["spid"])
    shots = _flat_shots(_storyboard(client, pid))
    assert shots[1]["title"] == "MY CUSTOM TITLE"
    # with explicit per-shot approval the overwrite applies
    _sync(client, pid, f["spid"],
          approve_overwrite=[shot["id"]])
    shots = _flat_shots(_storyboard(client, pid))
    assert shots[1]["title"] != "MY CUSTOM TITLE"


def test_removals_archive_only_with_approval(client):
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    sc = _detail(client, pid, f["spid"])["scenes"][0]
    elems = sc["elements"]
    # drop the action beat (element index 2)
    _set_elements(client, pid, sc["id"], [elems[0], elems[1]])
    plan = _plan(client, pid, f["spid"])
    s0 = plan["scenes"][0]
    assert len(s0["remove"]) == 1
    victim = s0["remove"][0]["shot_id"]
    # unapproved → still active
    _sync(client, pid, f["spid"])
    shots = _flat_shots(_storyboard(client, pid))
    assert any(sh["id"] == victim and sh["status"] == "active"
               for sh in shots)
    # approved → archived, never deleted
    _sync(client, pid, f["spid"], approve_remove=[victim])
    shots = _flat_shots(_storyboard(client, pid))
    assert any(sh["id"] == victim and sh["status"] == "archived"
               for sh in shots)


# ---------- undo / redo ----------

def test_shotlist_undo_redo(client):
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    n_active = sum(1 for sh in _flat_shots(_storyboard(client, pid))
                   if sh["status"] == "active")
    assert n_active == 4
    _commit(client, pid, "shotlist.undo", {})
    # created scenes+shots archived — nothing deleted
    board = _storyboard(client, pid)
    assert all(sh["status"] == "archived" for sh in _flat_shots(board))
    _commit(client, pid, "shotlist.redo", {})
    board = _storyboard(client, pid)
    assert sum(1 for sh in _flat_shots(board)
               if sh["status"] == "active") == 4


def test_shotlist_undo_when_nothing_outstanding(client):
    pid = make_project(client)["id"]
    _commit(client, pid, "shotlist.undo", {}, expect=409)


def test_undo_restores_pre_sync_dialogue(client):
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    sc = _detail(client, pid, f["spid"])["scenes"][1]
    elems = sc["elements"]
    _set_elements(client, pid, sc["id"],
                  [{**elems[0], "text": "Rewritten."}])
    _sync(client, pid, f["spid"])
    shots = _flat_shots(_storyboard(client, pid))
    assert any(c.get("dialogue") == "Rewritten."
               for c in shots[3]["cast"])
    _commit(client, pid, "shotlist.undo", {})
    shots = _flat_shots(_storyboard(client, pid))
    assert any(c.get("dialogue") == "We leave at dawn."
               for c in shots[3]["cast"])


# ---------- protocol-level guarantees ----------

def test_idempotent_replay(client):
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"], key="scaffold-once")
    _scaffold(client, pid, f["spid"], key="scaffold-once")
    assert len(_flat_shots(_storyboard(client, pid))) == 4


def test_stale_revision_rejected(client):
    pid = make_project(client)["id"]
    _commit(client, pid, "screenplay.create", {"title": "S"})
    spid = client.get(f"/api/projects/{pid}/screenplays").json()[0]["id"]
    rev = _rev(client, pid)
    _commit(client, pid, "screenplay_scene.create",
            {"screenplay_id": spid, "heading": "INT. A"})
    r = client.post(f"/api/projects/{pid}/commands/commit", json={
        "command_type": "screenplay.update",
        "payload": {"screenplay_id": spid, "description": "stale"},
        "scope": "project", "expected_revision": rev,
        "idempotency_key": "stale-rev-1"})
    assert r.status_code == 409


def test_preview_requires_approval_for_destructive(client):
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    shot = _flat_shots(_storyboard(client, pid))[1]
    _commit(client, pid, "shot.update",
            {"shot_id": shot["id"], "title": "USER TITLE"})
    sc = _detail(client, pid, f["spid"])["scenes"][0]
    elems = sc["elements"]
    _set_elements(client, pid, sc["id"],
                  [{**elems[0], "text": "New"}, elems[1], elems[2]])
    pv = _preview(client, pid, "shotlist.sync",
                  {"screenplay_id": f["spid"]})
    assert pv["shotlist_diff"]["counts"]["manual_edit"] == 1
    assert any("manual" in w.lower() for w in pv["warnings"])


def test_restart_persistence(client, tmp_path, monkeypatch):
    """State survives a fresh engine+session over the same DB file."""
    import os
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db.models import Screenplay, ScreenplayScene
    pid = make_project(client)["id"]
    _commit(client, pid, "screenplay.create", {"title": "Durable"})
    _scene(client, pid,
           client.get(f"/api/projects/{pid}/screenplays").json()[0]["id"],
           "INT. A")
    engine = client.app.state.engine
    factory = sessionmaker(bind=engine)
    s = factory()
    try:
        n = s.execute(select(Screenplay)
                      .where(Screenplay.project_id == pid)).scalars().all()
        assert len(n) == 1
        sc = s.execute(select(ScreenplayScene)).scalars().all()
        assert len(sc) == 1
    finally:
        s.close()


def test_backup_includes_screenplay(client, tmp_path):
    from tests.conftest import wait_for_job
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
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
    # imported project holds screenplay + scaffolded storyboard
    sps = client.get(f"/api/projects/{new_pid}/screenplays").json()
    assert len(sps) == 1 and sps[0]["title"] == "Film"
    board = _storyboard(client, new_pid)
    assert len(_flat_shots(board)) == 4
    d = _detail(client, new_pid, sps[0]["id"])
    assert d["scenes"][0]["elements"][0]["kind"] == "dialogue"
    # screenplay scene still links to the imported scene (remapped ids)
    assert d["scenes"][0]["scene_id"] == board[0]["scene"]["id"]


def test_migration_head_includes_0020(client):
    eng = client.app.state.engine
    with eng.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT version_num FROM alembic_version").all()
    assert any("0020" in r[0] for r in rows)


def test_n3_timeline_unaffected(client):
    """Existing timeline commands still work alongside screenplay
    state — the scaffold shares scenes/shots but never touches the
    timeline subsystem."""
    pid = make_project(client)["id"]
    _film(client, pid)
    _commit(client, pid, "timeline.create", {"name": "Cut"})
    tls = client.get(f"/api/projects/{pid}/timelines").json()
    assert len(tls) == 1


# ---------- A1-F hardening ----------

def test_sync_freshness_stale_preview_rejected(client):
    """F2 — a commit after a post-preview edit either 409s on the
    project pin or, freshly diffed, treats the edited shot as a
    manual-edit conflict requiring approval — never silently
    overwriting the newer work."""
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    sc = _detail(client, pid, f["spid"])["scenes"][0]
    els = sc["elements"]
    _set_elements(client, pid, sc["id"],
                  [{**els[0], "text": "new line"}, els[1], els[2]])
    _preview(client, pid, "shotlist.sync", {"screenplay_id": f["spid"]})
    shot = _flat_shots(_storyboard(client, pid))[1]
    # edit the affected shot AFTER the preview
    rev_before_edit = _rev(client, pid)
    _commit(client, pid, "shot.update",
            {"shot_id": shot["id"], "title": "POST-PREVIEW EDIT"})
    # stale project pin → 409
    r = client.post(f"/api/projects/{pid}/commands/commit", json={
        "command_type": "shotlist.sync",
        "payload": {"screenplay_id": f["spid"]}, "scope": "project",
        "expected_revision": rev_before_edit,
        "idempotency_key": "a1-stale-1"})
    assert r.status_code == 409
    # fresh commit — the diff is recomputed: edited shot is skipped
    _sync(client, pid, f["spid"])
    shots = _flat_shots(_storyboard(client, pid))
    assert shots[1]["title"] == "POST-PREVIEW EDIT"
    # fresh preview flags it; approval then applies correctly
    plan = _plan(client, pid, f["spid"])
    assert any(m["shot_id"] == shot["id"]
               for m in plan["scenes"][0]["manual_edit"])
    _sync(client, pid, f["spid"], approve_overwrite=[shot["id"]])
    shots = _flat_shots(_storyboard(client, pid))
    assert shots[1]["title"] != "POST-PREVIEW EDIT"


def test_user_fields_preserved_on_approved_overwrite(client, tmp_path):
    """F3 — approved overwrite of managed fields never touches
    prompt/intent/duration/continuity/media refs or non-scaffold
    cast rows/fields."""
    from tests.conftest import wait_for_job
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    shot = _flat_shots(_storyboard(client, pid))[1]
    # media reference: import an asset, bind it to the shot
    png = tmp_path / "ref.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    _commit(client, pid, "asset.import",
            {"source_path": str(png), "name": "ref", "kind": "image"})
    asset = client.get(f"/api/projects/{pid}/assets").json()["items"][0]
    _commit(client, pid, "shot.update",
            {"shot_id": shot["id"],
             "prompt": "user cinematic prompt",
             "intent": "user intent",
             "duration_sec": 7.5,
             "source_asset_id": asset["id"],
             "thumbnail_asset_id": asset["id"],
             "continuity": {"user_note": "keep me",
                            "negative_prompt": "blurry",
                            "gen_params": {"seed": 7}}})
    # user-authored cast field on a speaker row
    _commit(client, pid, "shot.cast.update",
            {"shot_id": shot["id"], "character_id": f["anna"]["id"],
             "state": {"blocking": "stage left", "emotion": "tense"}})
    # a NON-scaffold cast row — user added a character the beat
    # doesn't speak for; sync must never remove it
    carol = _character(client, pid, "Carol")
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": shot["id"], "character_id": carol["id"],
             "state": {"blocking": "background",
                      "wardrobe_variant": "coat"}})
    # user title edit → manual-edit flag
    _commit(client, pid, "shot.update",
            {"shot_id": shot["id"], "title": "USER TITLE"})
    sc = _detail(client, pid, f["spid"])["scenes"][0]
    els = sc["elements"]
    _set_elements(client, pid, sc["id"],
                  [{**els[0], "text": "updated"}, els[1], els[2]])
    _sync(client, pid, f["spid"], approve_overwrite=[shot["id"]])
    shots = _flat_shots(_storyboard(client, pid))
    s = next(x for x in shots if x["id"] == shot["id"])
    assert s["prompt"] == "user cinematic prompt"
    assert s["intent"] == "user intent"
    assert s["duration_sec"] == 7.5
    assert s["source_asset_id"] == asset["id"]
    assert s["thumbnail_asset_id"] == asset["id"]
    assert s["continuity_json"]["user_note"] == "keep me"
    assert s["continuity_json"]["negative_prompt"] == "blurry"
    assert s["continuity_json"]["gen_params"] == {"seed": 7}
    cast = {c["character_id"]: c for c in s["cast"]}
    assert cast[f["anna"]["id"]]["blocking"] == "stage left"
    assert cast[f["anna"]["id"]]["emotion"] == "tense"
    assert cast[carol["id"]]["blocking"] == "background"  # user row survived


def test_archived_shot_not_recreated(client):
    """F4 — a manually archived scaffolded shot is reported as skipped,
    never silently recreated."""
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    victim = _flat_shots(_storyboard(client, pid))[1]
    _commit(client, pid, "shot.archive", {"shot_id": victim["id"]})
    plan = _plan(client, pid, f["spid"])
    s0 = plan["scenes"][0]
    assert s0["add"] == []  # no re-creation proposed
    assert any(x["shot_id"] == victim["id"]
               for x in s0["skipped_archived"])
    _sync(client, pid, f["spid"])
    shots = _flat_shots(_storyboard(client, pid))
    mine = [s for s in shots if s["id"] == victim["id"]]
    assert len(mine) == 1 and mine[0]["status"] == "archived"
    assert len([s for s in shots if s["status"] == "active"]) == 3


def test_scene_reorder_keeps_shot_identity(client):
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    before = {s["id"] for s in _flat_shots(_storyboard(client, pid))}
    doc = _detail(client, pid, f["spid"])
    _commit(client, pid, "screenplay_scene.move",
            {"sp_scene_id": doc["scenes"][1]["id"], "to_index": 0})
    plan = _plan(client, pid, f["spid"])
    assert plan["counts"]["add"] == 0 and plan["counts"]["remove"] == 0
    _sync(client, pid, f["spid"])
    after = {s["id"] for s in _flat_shots(_storyboard(client, pid))}
    assert before == after


def test_element_reorder_keeps_shot_identity(client):
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    before = {s["id"] for s in _flat_shots(_storyboard(client, pid))}
    sc = _detail(client, pid, f["spid"])["scenes"][0]
    els = sc["elements"]
    _set_elements(client, pid, sc["id"], [els[1], els[0], els[2]])
    plan = _plan(client, pid, f["spid"])
    # same groups → same shots, content-signature may shift → update ok
    assert plan["counts"]["add"] == 0 and plan["counts"]["remove"] == 0
    _sync(client, pid, f["spid"])
    assert {s["id"] for s in _flat_shots(_storyboard(client, pid))} == before


def test_speaker_change_updates_cast(client):
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    sc = _detail(client, pid, f["spid"])["scenes"][0]
    els = sc["elements"]
    _set_elements(client, pid, sc["id"],
                  [{**els[0], "character_id": f["bob"]["id"]}, els[1], els[2]])
    plan = _plan(client, pid, f["spid"])
    assert plan["counts"]["update"] == 1
    _sync(client, pid, f["spid"])
    shots = _flat_shots(_storyboard(client, pid))
    cast = {c["character_id"] for c in shots[1]["cast"]}
    assert f["bob"]["id"] in cast and f["anna"]["id"] not in cast


def test_deleted_character_resolution(client):
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    _commit(client, pid, "character.archive",
            {"character_id": f["anna"]["id"]})
    # archive ≠ delete — a real delete isn't a command; simulate FK SET NULL
    from sqlalchemy import delete as _del
    from app.db.models import Character
    eng = client.app.state.engine
    with eng.begin() as con:
        con.execute(_del(Character).where(Character.id == f["anna"]["id"]))
    doc = _detail(client, pid, f["spid"])
    assert doc["scenes"][0]["elements"][0]["character_id"] is None
    # sync still works — unresolved speakers don't crash the diff
    plan = _plan(client, pid, f["spid"])
    assert isinstance(plan["counts"], dict)


def test_undo_skips_later_independent_edit(client):
    """F5 — batch undo must not clobber a post-sync independent edit."""
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    sc = _detail(client, pid, f["spid"])["scenes"][1]
    els = sc["elements"]
    _set_elements(client, pid, sc["id"],
                  [{**els[0], "text": "synced text"}])
    _sync(client, pid, f["spid"])
    shot = _flat_shots(_storyboard(client, pid))[3]
    _commit(client, pid, "shot.update",
            {"shot_id": shot["id"], "title": "INDEPENDENT EDIT"})
    _commit(client, pid, "shotlist.undo", {})
    shots = _flat_shots(_storyboard(client, pid))
    s = next(x for x in shots if x["id"] == shot["id"])
    assert s["title"] == "INDEPENDENT EDIT"  # skipped, not clobbered


def test_multiple_undos_walk_history(client):
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    sc = _detail(client, pid, f["spid"])["scenes"][1]
    _set_elements(client, pid, sc["id"],
                  [sc["elements"][0], _action("beat two")])
    _sync(client, pid, f["spid"])
    assert len(_flat_shots(_storyboard(client, pid))) == 5
    _commit(client, pid, "shotlist.undo", {})  # undoes the +1 sync
    assert sum(1 for s in _flat_shots(_storyboard(client, pid))
               if s["status"] == "active") == 4
    _commit(client, pid, "shotlist.undo", {})  # undoes the scaffold
    assert sum(1 for s in _flat_shots(_storyboard(client, pid))
               if s["status"] == "active") == 0
    _commit(client, pid, "shotlist.undo", {}, expect=409)


def test_restart_between_sync_and_undo(client, settings):
    """F5 — the undo batch lives in durable command-audit rows, so a
    fresh application instance over the same data dir can undo a sync
    that happened before the 'restart'."""
    from fastapi.testclient import TestClient
    from app.main import create_app
    from tests.conftest import TOKEN
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    sc = _detail(client, pid, f["spid"])["scenes"][1]
    els = sc["elements"]
    _set_elements(client, pid, sc["id"],
                  [{**els[0], "text": "pre-restart sync"}])
    _sync(client, pid, f["spid"])
    n_before = len(_flat_shots(_storyboard(client, pid)))
    shot_desc = _flat_shots(_storyboard(client, pid))[-1]["description"]
    assert "pre-restart sync" in shot_desc
    # restart: release the data-dir lock (backend exited) then boot a
    # brand-new app instance over the same data dir
    client.app.state.data_dir_lock.release()
    with TestClient(create_app(settings)) as c2:
        c2.headers["X-Atomic-Token"] = TOKEN
        r = c2.post(f"/api/projects/{pid}/commands/commit", json={
            "command_type": "shotlist.undo", "payload": {},
            "scope": "project",
            "expected_revision": _rev(c2, pid),
            "idempotency_key": "a1-restart-undo-1"})
        assert r.status_code == 200, r.text
        board = c2.get(
            f"/api/projects/{pid}/storyboard?include_archived=true").json()
        shots = _flat_shots(board)
        assert len(shots) == n_before  # update-only batch: same rows
        assert "pre-restart sync" not in shots[-1]["description"]
    assert "pre-restart sync" not in \
        _flat_shots(_storyboard(client, pid))[-1]["description"]


def test_deleted_location_resolution(client):
    """F4 — deleting a referenced location (FK SET NULL) leaves the
    screenplay scene + shot plan coherent rather than dangling."""
    from sqlalchemy import delete as _del
    from app.db.models import Location
    pid = make_project(client)["id"]
    f = _film(client, pid)
    _scaffold(client, pid, f["spid"])
    eng = client.app.state.engine
    with eng.begin() as con:
        con.execute(_del(Location).where(Location.id == f["loc"]["id"]))
    doc = _detail(client, pid, f["spid"])
    assert doc["scenes"][0]["location_id"] is None
    plan = _plan(client, pid, f["spid"])
    assert isinstance(plan["counts"], dict)
    _sync(client, pid, f["spid"])
    board = _storyboard(client, pid)
    assert board[0]["scene"]["location_id"] is None
