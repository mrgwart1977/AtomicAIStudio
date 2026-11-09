"""M1 coverage: storage/CAS, bibles + version history, project ops
(duplicate/delete/settings), dashboard queries, backup roundtrip and
failure recovery, sharing/isolation, pagination."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import os
import pytest

from tests.conftest import make_project, wait_for_job


def _commit(client, pid, command_type, payload, revision, key, origin="test"):
    r = client.post(
        f"/api/projects/{pid}/commands/commit",
        json={
            "command_type": command_type,
            "payload": payload,
            "scope": "project",
            "origin": origin,
            "expected_revision": revision,
            "idempotency_key": key if len(key) >= 8 else key + "-pad-0000",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _rev(client, pid) -> int:
    return client.get(f"/api/projects/{pid}").json()["revision"]


def _import_asset(client, pid, path: Path, name: str | None = None) -> dict:
    rev = _rev(client, pid)
    r = client.post(
        f"/api/projects/{pid}/commands/commit",
        json={
            "command_type": "asset.import",
            "payload": {"source_path": str(path), **({"name": name} if name else {})},
            "scope": "project",
            "expected_revision": rev,
            "idempotency_key": f"imp-{path.name}-{rev}",
        },
    )
    assert r.status_code == 200, r.text
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "succeeded", job
    return job


# ---------- project creation / dashboard ----------

def test_wizard_create_and_dashboard_fields(client):
    r = client.post(
        "/api/projects",
        json={
            "name": "Harbor Film",
            "kind": "short",
            "aspect_ratio": "2.39:1",
            "width": 2048,
            "height": 858,
            "fps_num": 24000,
            "fps_den": 1001,
            "style": "cinematic",
        },
    )
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["kind"] == "short" and p["aspect_ratio"] == "2.39:1"
    assert p["fps_num"] == 24000 and p["fps_den"] == 1001

    cards = client.get("/api/projects?search=harbor").json()
    assert len(cards) == 1
    card = cards[0]
    assert card["name"] == "Harbor Film"
    assert card["counts"]["assets"] == 0

    assert client.get("/api/projects?search=nomatch").json() == []
    assert client.get("/api/projects?kind=movie").json() == []
    assert len(client.get("/api/projects?kind=short").json()) == 1


def test_dashboard_counts_and_sort(client):
    a = make_project(client, "Alpha")
    make_project(client, "Beta")
    _commit(client, a["id"], "character.create", {"name": "Mara"}, _rev(client, a["id"]), "dc-1")
    cards = {c["name"]: c for c in client.get("/api/projects?sort=name&order=asc").json()}
    assert list(cards) == ["Alpha", "Beta"]
    assert cards["Alpha"]["counts"]["characters"] == 1


# ---------- bibles + versions ----------

def test_character_crud_versions_and_revert(client):
    p = make_project(client)
    r = _commit(client, p["id"], "character.create",
                {"name": "Ilya", "summary": "A keeper of lighthouses",
                 "data": {"appearance": "weathered coat", "personality": "quiet"}},
                _rev(client, p["id"]), "cc-1")
    chars = client.get(f"/api/projects/{p['id']}/character").json()
    assert len(chars) == 1
    ch = chars[0]
    assert ch["revision"] == 1 and ch["data"]["personality"] == "quiet"

    _commit(client, p["id"], "character.update",
            {"character_id": ch["id"], "summary": "Keeper of two lighthouses"},
            _rev(client, p["id"]), "cu-1")
    ch2 = client.get(f"/api/projects/{p['id']}/character/{ch['id']}").json()
    assert ch2["revision"] == 2 and "two lighthouses" in ch2["summary"]

    versions = client.get(f"/api/projects/{p['id']}/character/{ch['id']}/versions").json()
    assert [v["revision"] for v in versions] == [2, 1]

    _commit(client, p["id"], "character.revert",
            {"character_id": ch["id"], "to_revision": 1},
            _rev(client, p["id"]), "cr-1")
    ch3 = client.get(f"/api/projects/{p['id']}/character/{ch['id']}").json()
    assert ch3["revision"] == 3 and ch3["summary"] == "A keeper of lighthouses"

    # Archive hides from default list.
    _commit(client, p["id"], "character.archive", {"character_id": ch["id"]},
            _rev(client, p["id"]), "ca-1")
    assert client.get(f"/api/projects/{p['id']}/character").json() == []
    assert len(client.get(f"/api/projects/{p['id']}/character?include_archived=true").json()) == 1


def test_world_and_location_crud_with_world_link(client):
    p = make_project(client)
    _commit(client, p["id"], "world.create",
            {"name": "Marrow Sea", "data": {"tone": "cold", "era": "age of steam"}},
            _rev(client, p["id"]), "wc-1")
    w = client.get(f"/api/projects/{p['id']}/world").json()[0]
    _commit(client, p["id"], "location.create",
            {"name": "Salt Quay", "world_id": w["id"], "data": {"mood": "fog"}},
            _rev(client, p["id"]), "lc-1")
    loc = client.get(f"/api/projects/{p['id']}/location").json()[0]
    assert loc["world_id"] == w["id"]


def test_entities_are_isolated(client):
    a = make_project(client, "A")
    b = make_project(client, "B")
    _commit(client, a["id"], "character.create", {"name": "OnlyInA"},
            _rev(client, a["id"]), "iso-c1")
    assert client.get(f"/api/projects/{b['id']}/character").json() == []
    ch_a = client.get(f"/api/projects/{a['id']}/character").json()[0]
    r = client.get(f"/api/projects/{b['id']}/character/{ch_a['id']}")
    assert r.status_code == 404
    # B cannot mutate A's character through the command path either.
    r = client.post(
        f"/api/projects/{b['id']}/commands/commit",
        json={"command_type": "character.update",
              "payload": {"character_id": ch_a["id"], "summary": "hijack"},
              "expected_revision": _rev(client, b["id"]),
              "idempotency_key": "iso-bad-1x"},
    )
    assert r.status_code in (404, 422)


# ---------- settings / duplicate / delete ----------

def test_update_settings_and_undo(client):
    p = make_project(client)
    _commit(client, p["id"], "project.update_settings",
            {"fps_num": 30, "style": "noir"}, _rev(client, p["id"]), "us-1")
    p2 = client.get(f"/api/projects/{p['id']}").json()
    assert p2["fps_num"] == 30 and p2["style"] == "noir"

    cmds = client.get(f"/api/projects/{p['id']}/commands").json()
    set_cmd = next(c for c in cmds if c["command_type"] == "project.update_settings")
    r = client.post(f"/api/projects/{p['id']}/commands/{set_cmd['id']}/undo")
    assert r.status_code == 200, r.text
    p3 = client.get(f"/api/projects/{p['id']}").json()
    assert p3["fps_num"] == 24 and p3["style"] == "none"


def test_duplicate_shares_blobs_no_copy(client, settings):
    p = make_project(client)
    src = settings.data_dir / "in.txt"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    payload = b"x" * 10000
    src.write_bytes(payload)
    _import_asset(client, p["id"], src)

    files_before = list((settings.data_dir / "blobs").rglob("*"))
    _commit(client, p["id"], "project.duplicate", {"name": "Copy of T"},
            _rev(client, p["id"]), "dup-1")
    projs = {pr["name"]: pr for pr in client.get("/api/projects").json()}
    dup = projs["Copy of T"]
    assert dup["counts"]["assets"] == 1
    files_after = list((settings.data_dir / "blobs").rglob("*"))
    assert len(files_before) == len(files_after)  # CAS dedupe — zero bytes copied
    assets = client.get(f"/api/projects/{dup['id']}/assets").json()["items"]
    assert assets[0]["sha256"] == hashlib.sha256(payload).hexdigest()


def test_delete_requires_confirm_name_and_removes(client):
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/commands/commit",
        json={"command_type": "project.delete",
              "payload": {"confirm_name": "wrong"},
              "expected_revision": _rev(client, p["id"]),
              "idempotency_key": "del-bad-1"},
    )
    assert r.status_code == 422
    _commit(client, p["id"], "project.delete", {"confirm_name": p["name"]},
            _rev(client, p["id"]), "del-ok")
    assert client.get(f"/api/projects/{p['id']}").status_code == 404


# ---------- assets: import / verify / relink / sharing ----------

def test_import_verify_missing_and_relink(client, settings):
    p = make_project(client)
    src = settings.data_dir / "pic.bin"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"\x89PNGfake" * 100)
    _import_asset(client, p["id"], src, name="pic")
    items = client.get(f"/api/projects/{p['id']}/assets").json()["items"]
    assert items[0]["name"] == "pic" and items[0]["status"] == "ready"
    f = client.get(f"/api/projects/{p['id']}/assets/{items[0]['id']}/file")
    assert f.status_code == 200 and f.content == b"\x89PNGfake" * 100

    # Move the blob away → verify marks missing.
    blob = next((settings.data_dir / "blobs").rglob("????" * 16), None)
    blob = next(p_ for p_ in (settings.data_dir / "blobs").rglob("*") if p_.is_file())
    moved = blob.with_suffix(".moved")
    blob.rename(moved)
    _commit(client, p["id"], "assets.verify", {}, _rev(client, p["id"]), "ver-1")
    wait_id = client.get(f"/api/projects/{p['id']}/jobs").json()[0]["id"]
    wait_for_job(client, p["id"], wait_id)
    item = client.get(f"/api/projects/{p['id']}/assets").json()["items"][0]
    assert item["status"] == "missing"

    # Relink to the moved file → ready again (same content, same version).
    _commit(client, p["id"], "asset.relink",
            {"asset_id": item["id"], "source_path": str(moved)},
            _rev(client, p["id"]), "rel-1")
    job = client.get(f"/api/projects/{p['id']}/jobs").json()[0]
    wait_for_job(client, p["id"], job["id"])
    item2 = client.get(f"/api/projects/{p['id']}/assets").json()["items"][0]
    assert item2["status"] == "ready"


def test_asset_share_and_explicit_link(client):
    a = make_project(client, "Origin")
    b = make_project(client, "Consumer")
    src = Path("C:/Windows/Web/4K/Wallpaper/Windows/img0_1920x1200.jpg")
    if not src.is_file():
        src = Path("C:/Windows/Web/wallpaper/windows/img0_1024x768.jpg")
    if not src.is_file():
        pytest.skip("no sample image on this machine")
    _import_asset(client, a["id"], src, name="wall")
    asset = client.get(f"/api/projects/{a['id']}/assets").json()["items"][0]

    # B cannot see it yet (no shared assets in its scope).
    assert client.get(f"/api/projects/{b['id']}/shared-assets").json() == []

    _commit(client, a["id"], "asset.share", {"asset_id": asset["id"]},
            _rev(client, a["id"]), "shr-1")
    shared = client.get(f"/api/projects/{b['id']}/shared-assets").json()
    assert len(shared) == 1 and shared[0]["source_project_name"] == "Origin"

    _commit(client, b["id"], "asset.link",
            {"asset_id": asset["id"], "mode": "copy"},
            _rev(client, b["id"]), "lnk-1")
    b_assets = client.get(f"/api/projects/{b['id']}/assets").json()["items"]
    assert len(b_assets) == 1
    assert b_assets[0]["id"] != asset["id"]  # copy = new row in B
    f = client.get(f"/api/projects/{b['id']}/assets/{b_assets[0]['id']}/file")
    assert f.status_code == 200


# ---------- backups: export / import / restore / failures ----------

def _export(client, pid, kind="backup", key="bk-1"):
    _commit(client, pid, "backup.export", {"kind": kind}, _rev(client, pid), key)
    job = client.get(f"/api/projects/{pid}/jobs").json()[0]
    done = wait_for_job(client, pid, job["id"])
    assert done["status"] == "succeeded", done
    return done["result_json"]


def test_backup_export_import_restore_roundtrip(client, settings, tmp_path):
    p = make_project(client, "Roundtrip")
    _commit(client, p["id"], "character.create",
            {"name": "Sena", "data": {"personality": "bold"}},
            _rev(client, p["id"]), "rt-c1")
    src = settings.data_dir / "frame.bin"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    payload = os.urandom(5000)
    src.write_bytes(payload)
    _import_asset(client, p["id"], src, name="frame")

    result = _export(client, p["id"])
    pkg = Path(result["package_path"])
    assert pkg.is_file() and result["byte_size"] == pkg.stat().st_size

    # Import creates a new project with identical content.
    r = client.post("/api/packages/import", json={"package_path": str(pkg)})
    assert r.status_code == 200, r.text
    new_pid = r.json()["project_id"]
    wait_for_job(client, new_pid, r.json()["job_id"], timeout=30)

    imported = client.get(f"/api/projects/{new_pid}").json()
    assert imported["name"] == "Roundtrip" and imported["status"] == "active"
    chars = client.get(f"/api/projects/{new_pid}/character").json()
    assert chars[0]["name"] == "Sena" and chars[0]["data"]["personality"] == "bold"
    assets = client.get(f"/api/projects/{new_pid}/assets").json()["items"]
    assert len(assets) == 1 and assets[0]["status"] == "ready"
    f = client.get(f"/api/projects/{new_pid}/assets/{assets[0]['id']}/file")
    assert f.content == payload

    # Restore creates ANOTHER project, never mutates the original.
    runs = client.get(f"/api/projects/{p['id']}/backups").json()
    _commit(client, p["id"], "backup.restore", {"backup_run_id": runs[0]["id"]},
            _rev(client, p["id"]), "rst-1")
    job = client.get(f"/api/projects/{p['id']}/jobs").json()[0]
    done = wait_for_job(client, p["id"], job["id"], timeout=30)
    restored_pid = done["result_json"]["restored_project_id"]
    restored = client.get(f"/api/projects/{restored_pid}").json()
    assert "(restored)" in restored["name"]
    assert len(client.get(f"/api/projects/{restored_pid}/character").json()) == 1


def test_package_preview_and_invalid_import_rejected(client, settings, tmp_path):
    bad = tmp_path / "bad.atomicpkg"
    bad.write_bytes(b"not a zip")
    r = client.post("/api/packages/preview", json={"package_path": str(bad)})
    assert r.status_code == 422

    # Traversal attempt inside an otherwise-valid zip.
    evil = tmp_path / "evil.atomicpkg"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "format_version": 1, "project": {"name": "x", "id": "p1"},
            "entities": {}, "assets": [], "asset_versions": [],
            "files": [{"sha256": "ab" * 32, "byte_size": 4, "arcname": "../escape.bin"}],
        }))
        zf.writestr("../escape.bin", b"evil")
    preview = client.post("/api/packages/preview", json={"package_path": str(evil)})
    assert preview.status_code == 200  # preview parses; import must reject
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/commands/commit",
        json={"command_type": "backup.import",
              "payload": {"package_path": str(evil)},
              "expected_revision": _rev(client, p["id"]),
              "idempotency_key": "imp-evil-1x"},
    )
    assert r.status_code == 200
    job = wait_for_job(client, p["id"], r.json()["job_id"], timeout=30)
    assert job["status"] == "failed"
    assert not (tmp_path / "escape.bin").exists()
    assert not (Path(tmp_path) / ".." / "escape.bin").exists()


def test_missing_blob_marks_asset_missing_on_import(client, settings, tmp_path):
    p = make_project(client, "WithBlob")
    src = settings.data_dir / "a.bin"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"data" * 100)
    _import_asset(client, p["id"], src)
    result = _export(client, p["id"], key="mb-1")
    pkg = Path(result["package_path"])

    # Rebuild package without the blob member (simulates partial copy).
    with zipfile.ZipFile(pkg) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    broken = tmp_path / "partial.atomicpkg"
    with zipfile.ZipFile(broken, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
    r = client.post("/api/packages/import", json={"package_path": str(broken)})
    assert r.status_code == 200
    job = wait_for_job(client, r.json()["project_id"], r.json()["job_id"], timeout=30)
    assert job["status"] == "failed"  # hash-verified file missing → whole import rejected
    assert client.get(f"/api/projects/{r.json()['project_id']}/assets").json()["total"] == 0


# ---------- storage internals ----------

def test_atomic_write_and_sweep(client, settings):
    from app.domain.storage import StorageRegistry, atomic_write
    from app.db.models import AssetFile

    target = settings.data_dir / "blobs" / "tt" / "deadbeef"
    orphan = target.with_name("deadbeef.tmp")
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"partial")
    atomic_write(target, b"complete")
    assert target.read_bytes() == b"complete"
    assert not list(target.parent.glob("*.tmp.tmp"))

    # Startup sweep removes .tmp orphans.
    orphan.write_bytes(b"partial")
    engine = client.app.state.engine
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        reg = StorageRegistry(client.app.state.session_factory, settings.data_dir)
        out = reg.sweep(s)
        s.commit()
    assert out["tmp_removed"] >= 1
    assert not orphan.exists()


def test_paginated_assets_performance_shape(client, settings):
    p = make_project(client)
    # Seed 60 asset rows directly (no files) — listing must stay paginated.
    engine = client.app.state.engine
    from sqlalchemy.orm import Session
    from app.db.models import Asset
    with Session(engine) as s:
        for i in range(60):
            s.add(Asset(id=f"a{i:03d}", project_id=p["id"], name=f"a{i}", kind="image",
                        uri=f"x/{i}", sha256=f"{i:064d}"[:64]))
        s.commit()
    page1 = client.get(f"/api/projects/{p['id']}/assets?limit=25&offset=0").json()
    page2 = client.get(f"/api/projects/{p['id']}/assets?limit=25&offset=25").json()
    assert page1["total"] == 60 and len(page1["items"]) == 25
    assert len(page2["items"]) == 25
    assert page1["items"][0]["id"] != page2["items"][0]["id"]
    assert client.get(
        f"/api/projects/{p['id']}/assets?kind=video"
    ).json()["total"] == 0
