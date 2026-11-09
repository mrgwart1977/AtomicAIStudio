"""M2.1 coverage: structured cast-state validation (schema v1), legacy-state
compatibility, reference-asset scoping, scene→shot cast inheritance + per-shot
overrides, undo, project isolation, migration 0005, and backup round-trip of
the new columns."""

from __future__ import annotations

import io
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


def _shot_cast(client, pid, shot_id, cid):
    board = _board(client, pid)
    for s in board:
        for sh in s["shots"]:
            if sh["shot"]["id"] == shot_id:
                return next((c for c in sh["cast"] if c["character_id"] == cid), None)
    return None


# ---------- A. schema validation ----------

def test_state_defaults_when_missing(client):
    """No state object → safe defaults (empty strings, [], {})."""
    p = make_project(client, "Defaults")
    c = _char(client, p["id"], "Ada", "d-1")
    s = _scene(client, p["id"], "S", "d-2")
    sh = _shot(client, p["id"], s["scene"]["id"], "d-3")
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"]}, "d-4")
    row = _shot_cast(client, p["id"], sh["shot"]["id"], c["id"])
    assert row is not None and row["source"] == "shot"
    for f in ("wardrobe_variant", "blocking", "expression", "emotion",
              "action_instructions", "dialogue", "voice_override", "model_ref"):
        assert row[f] == ""
    assert row["lora_json"] == []
    assert row["continuity_json"] == {}
    assert row["reference_ids_json"] == []
    assert row["ext_json"] == {}


def test_state_rejects_wrong_types(client):
    """Typed fields reject non-strings with a clear message."""
    p = make_project(client, "BadTypes")
    c = _char(client, p["id"], "Ada", "t-1")
    s = _scene(client, p["id"], "S", "t-2")
    sh = _shot(client, p["id"], s["scene"]["id"], "t-3")
    sid, cid = sh["shot"]["id"], c["id"]

    for field, bad in (
        ("expression", 123),
        ("wardrobe_variant", ["x"]),
        ("blocking", {"x": 1}),
        ("dialogue", 4.5),
        ("model_ref", True),
    ):
        r = _commit(client, p["id"], "shot.cast.assign",
                    {"shot_id": sid, "character_id": cid, "state": {field: bad}},
                    f"t-bad-{field}", expect=422)
        assert f"cast.state.{field}" in r.json()["detail"], r.json()

    # state itself must be an object
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sid, "character_id": cid, "state": "defiant"},
            "t-bad-state", expect=422)
    # loras must be list[str]
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sid, "character_id": cid, "state": {"loras": "flux-lora"}},
            "t-bad-loras", expect=422)
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sid, "character_id": cid, "state": {"loras": [1, 2]}},
            "t-bad-loras2", expect=422)
    # continuity must be an object
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sid, "character_id": cid, "state": {"continuity": "rain"}},
            "t-bad-cont", expect=422)
    # reference_asset_ids must be list[str]
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sid, "character_id": cid, "state": {"reference_asset_ids": "abc"}},
            "t-bad-refs", expect=422)
    # position must be int
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sid, "character_id": cid, "state": {"position": "first"}},
            "t-bad-pos", expect=422)
    # schema_version must be a non-negative int
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sid, "character_id": cid, "state": {"schema_version": "v2"}},
            "t-bad-ver", expect=422)


def test_state_legacy_aliases_and_ext(client):
    """Legacy keys normalise; unknown keys survive in ext_json (forward compat)."""
    p = make_project(client, "Legacy")
    c = _char(client, p["id"], "Ada", "lg-1")
    s = _scene(client, p["id"], "S", "lg-2")
    sh = _shot(client, p["id"], s["scene"]["id"], "lg-3")
    # A state object shaped like a pre-schema writer's payload.
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"],
             "state": {
                 "action": "hand on holster",          # → action_instructions
                 "voice": "low, clipped",              # → voice_override
                 "lora_refs": ["ada-face-v2"],         # → loras
                 "continuity_notes": "jacket torn",    # → continuity.notes
                 "emotion": "controlled fury",
                 "future_field": {"nested": True},     # → ext_json (preserved)
                 "another_new_thing": "hello",         # → ext_json
             }}, "lg-4")
    row = _shot_cast(client, p["id"], sh["shot"]["id"], c["id"])
    assert row["action_instructions"] == "hand on holster"
    assert row["voice_override"] == "low, clipped"
    assert row["lora_json"] == ["ada-face-v2"]
    assert row["continuity_json"]["notes"] == "jacket torn"
    assert row["emotion"] == "controlled fury"
    assert row["ext_json"]["future_field"] == {"nested": True}
    assert row["ext_json"]["another_new_thing"] == "hello"
    # Canonical keys are NOT duplicated into ext.
    assert "action" not in row["ext_json"]
    assert "voice" not in row["ext_json"]
    assert "continuity_notes" not in row["ext_json"]


def test_state_explicit_canonical_wins_over_alias(client):
    """If both canonical + alias are sent, the canonical value wins."""
    p = make_project(client, "CanonWins")
    c = _char(client, p["id"], "Ada", "cw-1")
    s = _scene(client, p["id"], "S", "cw-2")
    sh = _shot(client, p["id"], s["scene"]["id"], "cw-3")
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"],
             "state": {"action": "WRONG", "action_instructions": "right"}},
            "cw-4")
    row = _shot_cast(client, p["id"], sh["shot"]["id"], c["id"])
    assert row["action_instructions"] == "right"


def test_state_partial_update_preserves_unset(client):
    """Update only touches provided keys — unset fields keep stored values."""
    p = make_project(client, "Partial")
    c = _char(client, p["id"], "Ada", "pa-1")
    s = _scene(client, p["id"], "S", "pa-2")
    sh = _shot(client, p["id"], s["scene"]["id"], "pa-3")
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"],
             "state": {"expression": "defiant", "wardrobe_variant": "torn coat",
                       "custom_key": "keep me"}}, "pa-4")
    _commit(client, p["id"], "shot.cast.update",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"],
             "state": {"emotion": "cold"}}, "pa-5")
    row = _shot_cast(client, p["id"], sh["shot"]["id"], c["id"])
    assert row["expression"] == "defiant"        # untouched
    assert row["wardrobe_variant"] == "torn coat"  # untouched
    assert row["emotion"] == "cold"              # updated
    assert row["ext_json"]["custom_key"] == "keep me"  # untouched

    # An explicitly empty string clears a field.
    _commit(client, p["id"], "shot.cast.update",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"],
             "state": {"expression": ""}}, "pa-6")
    assert _shot_cast(client, p["id"], sh["shot"]["id"], c["id"])["expression"] == ""

    # ext merges, doesn't clobber
    _commit(client, p["id"], "shot.cast.update",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"],
             "state": {"another_key": 7}}, "pa-7")
    row = _shot_cast(client, p["id"], sh["shot"]["id"], c["id"])
    assert row["ext_json"]["custom_key"] == "keep me"
    assert row["ext_json"]["another_key"] == 7


def test_state_newer_schema_version_tolerated(client):
    """schema_version > current parses tolerantly — fields still apply."""
    p = make_project(client, "FutureVer")
    c = _char(client, p["id"], "Ada", "fv-1")
    s = _scene(client, p["id"], "S", "fv-2")
    sh = _shot(client, p["id"], s["scene"]["id"], "fv-3")
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"],
             "state": {"schema_version": 99, "expression": "smirk",
                       "v99_field": "preserved"}}, "fv-4")
    row = _shot_cast(client, p["id"], sh["shot"]["id"], c["id"])
    assert row["expression"] == "smirk"
    assert row["ext_json"]["v99_field"] == "preserved"


# ---------- reference asset IDs (identity refs, project-scoped) ----------

def test_reference_asset_ids_validated(client, tmp_path):
    """reference_asset_ids must be real assets in the same project."""
    p = make_project(client, "RefIds")
    p2 = make_project(client, "OtherProj")
    c = _char(client, p["id"], "Ada", "ri-1")
    s = _scene(client, p["id"], "S", "ri-2")
    sh = _shot(client, p["id"], s["scene"]["id"], "ri-3")
    sid, cid = sh["shot"]["id"], c["id"]

    img = tmp_path / "ref.png"
    img.write_bytes(b"\x89PNG" + b"\x00" * 512)
    mine = _import_asset(client, p["id"], img, "mine.png")
    img2 = tmp_path / "ref2.png"
    img2.write_bytes(b"\x89PNG" + b"\x01" * 512)
    theirs = _import_asset(client, p2["id"], img2, "theirs.png")

    # Cross-project reference → rejected.
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sid, "character_id": cid,
             "state": {"reference_asset_ids": [theirs["id"]]}},
            "ri-4", expect=404)
    # Dead ID → rejected.
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sid, "character_id": cid,
             "state": {"reference_asset_ids": ["does-not-exist"]}},
            "ri-5", expect=404)
    # Own project → stored.
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sid, "character_id": cid,
             "state": {"reference_asset_ids": [mine["id"]]}}, "ri-6")
    row = _shot_cast(client, p["id"], sid, cid)
    assert row["reference_ids_json"] == [mine["id"]]

    # Update replaces the list.
    _commit(client, p["id"], "shot.cast.update",
            {"shot_id": sid, "character_id": cid,
             "state": {"reference_asset_ids": []}}, "ri-7")
    assert _shot_cast(client, p["id"], sid, cid)["reference_ids_json"] == []


# ---------- scene-cast field validation ----------

def test_scene_cast_field_validation(client):
    p = make_project(client, "SceneVal")
    c = _char(client, p["id"], "Ada", "sv-1")
    s = _scene(client, p["id"], "S", "sv-2")
    _commit(client, p["id"], "scene.cast.assign",
            {"scene_id": s["scene"]["id"], "character_id": c["id"], "role": 42},
            "sv-3", expect=422)
    _commit(client, p["id"], "scene.cast.assign",
            {"scene_id": s["scene"]["id"], "character_id": c["id"], "notes": "text"},
            "sv-4", expect=422)
    _commit(client, p["id"], "scene.cast.assign",
            {"scene_id": s["scene"]["id"], "character_id": c["id"],
             "role": "lead", "notes": {"arc": "vengeance"}}, "sv-5")
    row = next(x for x in _board(client, p["id"])[0]["cast"]
               if x["character_id"] == c["id"])
    assert row["role"] == "lead" and row["notes_json"]["arc"] == "vengeance"


# ---------- B. scene → shot inheritance ----------

def test_scene_cast_inherited_by_shots(client):
    """Scene cast flows to every shot; own row = override, not duplication."""
    p = make_project(client, "Inherit")
    ada = _char(client, p["id"], "Ada", "ih-1")
    mori = _char(client, p["id"], "Mori", "ih-2")
    s = _scene(client, p["id"], "Standoff", "ih-3")
    sid = s["scene"]["id"]
    _commit(client, p["id"], "scene.cast.assign",
            {"scene_id": sid, "character_id": ada["id"], "role": "lead"}, "ih-4")
    _commit(client, p["id"], "scene.cast.assign",
            {"scene_id": sid, "character_id": mori["id"], "role": "villain"}, "ih-5")

    sh1 = _shot(client, p["id"], sid, "ih-6", title="wide")
    sh2 = _shot(client, p["id"], sid, "ih-7", title="close")
    board = _board(client, p["id"])
    for entry in board[0]["shots"]:
        assert {c["character_id"] for c in entry["inherited"]} == {ada["id"], mori["id"]}
        assert all(c["source"] == "scene" for c in entry["inherited"])
        assert entry["cast"] == []  # no own rows yet

    # Override Ada in sh1 — she moves from `inherited` to `cast` there only.
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sh1["shot"]["id"], "character_id": ada["id"],
             "state": {"expression": "defiant"}}, "ih-8")
    board = _board(client, p["id"])
    e1 = next(x for x in board[0]["shots"] if x["shot"]["id"] == sh1["shot"]["id"])
    e2 = next(x for x in board[0]["shots"] if x["shot"]["id"] == sh2["shot"]["id"])
    assert {c["character_id"] for c in e1["cast"]} == {ada["id"]}
    assert e1["cast"][0]["source"] == "shot"
    assert e1["cast"][0]["expression"] == "defiant"
    assert {c["character_id"] for c in e1["inherited"]} == {mori["id"]}
    # sh2 untouched — both still inherited.
    assert e2["cast"] == []
    assert {c["character_id"] for c in e2["inherited"]} == {ada["id"], mori["id"]}

    # Removing the override returns Ada to `inherited` (scene assignment intact).
    _commit(client, p["id"], "shot.cast.remove",
            {"shot_id": sh1["shot"]["id"], "character_id": ada["id"]}, "ih-9")
    e1 = next(x for x in _board(client, p["id"])[0]["shots"]
              if x["shot"]["id"] == sh1["shot"]["id"])
    assert e1["cast"] == []
    assert {c["character_id"] for c in e1["inherited"]} == {ada["id"], mori["id"]}


def test_scene_cast_remove_drops_inheritance_not_overrides(client):
    """Removing a scene member drops inheritance but never touches own rows."""
    p = make_project(client, "DropInh")
    ada = _char(client, p["id"], "Ada", "di-1")
    s = _scene(client, p["id"], "S", "di-2")
    sid = s["scene"]["id"]
    _commit(client, p["id"], "scene.cast.assign",
            {"scene_id": sid, "character_id": ada["id"]}, "di-3")
    sh1 = _shot(client, p["id"], sid, "di-4")
    sh2 = _shot(client, p["id"], sid, "di-5")
    # sh2 gets an OWN row (override).
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sh2["shot"]["id"], "character_id": ada["id"],
             "state": {"expression": "overridden"}}, "di-6")

    _commit(client, p["id"], "scene.cast.remove",
            {"scene_id": sid, "character_id": ada["id"]}, "di-7")
    board = _board(client, p["id"])
    e1 = next(x for x in board[0]["shots"] if x["shot"]["id"] == sh1["shot"]["id"])
    e2 = next(x for x in board[0]["shots"] if x["shot"]["id"] == sh2["shot"]["id"])
    assert e1["inherited"] == [] and e1["cast"] == []
    # The override row survives — it's an independent shot-level row.
    assert {c["character_id"] for c in e2["cast"]} == {ada["id"]}
    assert e2["cast"][0]["expression"] == "overridden"


def test_scene_cast_undo_restores_inheritance(client):
    p = make_project(client, "UndoInh")
    ada = _char(client, p["id"], "Ada", "ui-1")
    s = _scene(client, p["id"], "S", "ui-2")
    sid = s["scene"]["id"]
    res = _commit(client, p["id"], "scene.cast.assign",
                  {"scene_id": sid, "character_id": ada["id"], "role": "lead"}, "ui-3")
    sh = _shot(client, p["id"], sid, "ui-4")
    assert _board(client, p["id"])[0]["shots"][0]["inherited"]

    _undo(client, p["id"], res["command_id"])
    scene = _board(client, p["id"])[0]
    assert scene["cast"] == []
    assert scene["shots"][0]["inherited"] == []


# ---------- duplication copies new columns ----------

def test_shot_duplicate_copies_state_columns(client, tmp_path):
    p = make_project(client, "DupCols")
    c = _char(client, p["id"], "Ada", "dc-1")
    s = _scene(client, p["id"], "S", "dc-2")
    sh = _shot(client, p["id"], s["scene"]["id"], "dc-3")
    img = tmp_path / "r.png"
    img.write_bytes(b"\x89PNG" + b"\x00" * 128)
    a = _import_asset(client, p["id"], img, "r.png")
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"],
             "state": {"expression": "smirk", "reference_asset_ids": [a["id"]],
                       "custom": "x"}}, "dc-4")
    _commit(client, p["id"], "shot.duplicate", {"shot_id": sh["shot"]["id"]}, "dc-5")
    board = _board(client, p["id"])
    dup = next(x for x in board[0]["shots"] if x["shot"]["id"] != sh["shot"]["id"])
    row = dup["cast"][0]
    assert row["expression"] == "smirk"
    assert row["reference_ids_json"] == [a["id"]]
    assert row["ext_json"]["custom"] == "x"


# ---------- revert restores new columns ----------

def test_shot_revert_restores_state_columns(client, tmp_path):
    p = make_project(client, "RevCols")
    c = _char(client, p["id"], "Ada", "rc-1")
    s = _scene(client, p["id"], "S", "rc-2")
    sh = _shot(client, p["id"], s["scene"]["id"], "rc-3")
    img = tmp_path / "r.png"
    img.write_bytes(b"\x89PNG" + b"\x00" * 128)
    a = _import_asset(client, p["id"], img, "r.png")
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"],
             "state": {"expression": "v1", "reference_asset_ids": [a["id"]],
                       "ext1": True}}, "rc-4")
    _commit(client, p["id"], "shot.cast.update",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"],
             "state": {"expression": "v2", "reference_asset_ids": [], "ext2": 2}},
            "rc-5")
    # rev 2 = assign, rev 3 = update (shot create is rev 1)
    _commit(client, p["id"], "shot.revert",
            {"shot_id": sh["shot"]["id"], "to_revision": 2}, "rc-6")
    row = _shot_cast(client, p["id"], sh["shot"]["id"], c["id"])
    assert row["expression"] == "v1"
    assert row["reference_ids_json"] == [a["id"]]
    assert row["ext_json"]["ext1"] is True
    assert "ext2" not in row["ext_json"]


# ---------- isolation ----------

def test_cross_project_cast_still_rejected(client):
    p = make_project(client, "IsoA")
    p2 = make_project(client, "IsoB")
    c2 = _char(client, p2["id"], "Foreign", "ix-1")
    s = _scene(client, p["id"], "S", "ix-2")
    sh = _shot(client, p["id"], s["scene"]["id"], "ix-3")
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c2["id"]},
            "ix-4", expect=404)
    _commit(client, p["id"], "scene.cast.assign",
            {"scene_id": s["scene"]["id"], "character_id": c2["id"]},
            "ix-5", expect=404)


# ---------- migration 0005 ----------

def test_migration_0005_columns_exist(client):
    """Fresh DB has the new columns; assign→read proves the wire path."""
    p = make_project(client, "Mig5")
    c = _char(client, p["id"], "Ada", "m5-1")
    s = _scene(client, p["id"], "S", "m5-2")
    sh = _shot(client, p["id"], s["scene"]["id"], "m5-3")
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"],
             "state": {"reference_asset_ids": [], "future": {"v": 1}}}, "m5-4")
    row = _shot_cast(client, p["id"], sh["shot"]["id"], c["id"])
    assert "reference_ids_json" in row and "ext_json" in row


def test_alembic_0005_upgrades_0004_db(tmp_path):
    """A real 0004-shaped DB gains both columns via the guarded migration."""
    import sqlalchemy as sa
    from app.main import run_migrations

    db = tmp_path / "old.db"
    url = f"sqlite:///{db.as_posix()}"
    eng = sa.create_engine(url)
    with eng.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        conn.execute(sa.text("INSERT INTO alembic_version VALUES ('0004_m2')"))
        # 0004-shaped shot_characters — no M2.1 columns.
        conn.execute(sa.text(
            """CREATE TABLE shot_characters (
                id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36),
                shot_id VARCHAR(36), character_id VARCHAR(36), position INTEGER,
                wardrobe_variant VARCHAR(200), blocking TEXT,
                expression VARCHAR(200), emotion VARCHAR(120),
                action_instructions TEXT, dialogue TEXT,
                voice_override VARCHAR(200), model_ref VARCHAR(200),
                lora_json JSON, continuity_json JSON,
                created_at DATETIME, updated_at DATETIME)"""))
    eng.dispose()

    run_migrations(url)
    cols = {c["name"] for c in sa.inspect(sa.create_engine(url)).get_columns("shot_characters")}
    assert {"reference_ids_json", "ext_json"} <= cols


# ---------- backup round-trip of new columns ----------

def test_backup_roundtrip_preserves_state_columns(client, tmp_path):
    """ext_json + reference_ids_json survive export → import."""
    p = make_project(client, "BkCols")
    c = _char(client, p["id"], "Ada", "bk-1")
    s = _scene(client, p["id"], "S", "bk-2")
    sh = _shot(client, p["id"], s["scene"]["id"], "bk-3")
    img = tmp_path / "r.png"
    img.write_bytes(b"\x89PNG" + b"\x00" * 128)
    a = _import_asset(client, p["id"], img, "r.png")
    _commit(client, p["id"], "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"],
             "state": {"expression": "smirk", "reference_asset_ids": [a["id"]],
                       "ext_key": "value"}}, "bk-4")

    res = _commit(client, p["id"], "backup.export", {}, "bk-export")
    job = wait_for_job(client, p["id"], res["job_id"])
    assert job["status"] == "succeeded", job
    pkg = job["result_json"]["package_path"]

    r = client.post("/api/packages/import", json={"package_path": pkg})
    assert r.status_code == 200, r.text
    job = wait_for_job(client, r.json()["project_id"], r.json()["job_id"])
    assert job["status"] == "succeeded", job
    new_pid = job["result_json"]["project_id"]

    board = _board(client, new_pid)
    row = board[0]["shots"][0]["cast"][0]
    assert row["expression"] == "smirk"
    # IDs were remapped — the reference points at the new project's asset copy.
    old_ids = {a["id"]}
    new_ids = set(row["reference_ids_json"])
    assert new_ids and not (new_ids & old_ids)
    assert row["ext_json"]["ext_key"] == "value"
