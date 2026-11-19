"""C2.3 — malicious/malformed backup package refusal.

Every refusal must happen BEFORE extraction commits and before any
project mutation — the ``_db_probe`` session factory asserts the DB
boundary is never crossed on a refused package. Archive attacks are
crafted at zip level; no filesystem outside tmp_path is touched.
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from app.domain.backup import (FORMAT_VERSION, PackageError,
                               import_package, read_manifest)

SHA = "ab" * 32


def _manifest(files=(), **over):
    m = {"format_version": FORMAT_VERSION,
         "project": {"id": "p1", "name": "evil"},
         "entities": {}, "assets": [], "asset_versions": [],
         "files": list(files)}
    m.update(over)
    return m


def _pkg(tmp: Path, name: str, manifest=None,
         members=(), manifest_raw=None) -> Path:
    p = tmp / name
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("manifest.json",
                    manifest_raw if manifest_raw is not None
                    else json.dumps(manifest))
        for arc, data in members:
            zf.writestr(arc, data)
    return p


def _db_probe():
    raise AssertionError("DB session opened — mutation boundary crossed")


def _import(pkg: Path) -> None:
    import_package(lambda: _db_probe(), pkg, storage=None)


def _import_code(pkg: Path) -> str:
    with pytest.raises(PackageError) as e:
        _import(pkg)
    return str(e.value)


# ---------- member-name hygiene ----------

def test_traversal_and_absolute_members_refused(tmp_path):
    for arc in ("../escape.bin", "files/../../escape.bin",
                "files\\..\\escape.bin", "..\\escape.bin",
                "/abs.bin", "\\abs.bin", "C:/abs.bin", "C:\\abs.bin",
                " lead.bin", "trail.bin "):
        pkg = _pkg(tmp_path, f"t-{abs(hash(arc))}.atomicpkg",
                   _manifest(), members=[(arc, b"x")])
        assert "unsafe" in _import_code(pkg), arc


# ---------- manifest field validation ----------

def test_corrupt_manifest_json_refused(tmp_path):
    pkg = _pkg(tmp_path, "corrupt.atomicpkg", manifest_raw=b"{not json")
    with pytest.raises(PackageError):
        _import(pkg)


def test_missing_manifest_refused(tmp_path):
    p = tmp_path / "nomanifest.atomicpkg"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("files/" + SHA, b"x")
    with pytest.raises(PackageError):
        _import(p)


@pytest.mark.parametrize("arc,with_member", [
    ("../x", True), ("other/x", True), ("files/escape", True),
    ("files/", True), ("manifest.json", False)])
def test_noncanonical_arcname_refused(tmp_path, arc, with_member):
    pkg = _pkg(tmp_path, f"arc-{abs(hash(arc))}.atomicpkg",
               _manifest(files=[{"sha256": SHA, "byte_size": 1,
                                 "arcname": arc}]),
               members=[(arc, b"x")] if with_member else [])
    assert "arcname" in _import_code(pkg)


def test_invalid_sha_field_refused(tmp_path):
    pkg = _pkg(tmp_path, "badsha.atomicpkg",
               _manifest(files=[{"sha256": "zz", "byte_size": 1,
                                 "arcname": "files/zz"}]))
    assert "sha256" in _import_code(pkg)


def test_invalid_byte_size_refused(tmp_path):
    for size in (-1, 1.5, "4", True):
        pkg = _pkg(tmp_path, f"bs-{size}.atomicpkg",
                   _manifest(files=[{"sha256": SHA, "byte_size": size,
                                     "arcname": f"files/{SHA}"}]))
        assert "byte_size" in _import_code(pkg)


def test_duplicate_manifest_arcname_refused(tmp_path):
    entry = {"sha256": SHA, "byte_size": 4, "arcname": f"files/{SHA}"}
    pkg = _pkg(tmp_path, "duparc.atomicpkg",
               _manifest(files=[entry, dict(entry)]),
               members=[(f"files/{SHA}", b"data")])
    assert "duplicate" in _import_code(pkg)


def test_duplicate_zip_member_refused(tmp_path):
    p = tmp_path / "dupzip.atomicpkg"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("manifest.json", json.dumps(_manifest()))
        zf.writestr("files/x", b"a")
        zf.writestr("files/x", b"b")  # same name twice
    assert "duplicate" in _import_code(p)


# ---------- size/hash honesty ----------

def test_oversize_blob_refused_mid_stream(tmp_path):
    # declared 4 bytes, actual 64 — the moment the stream exceeds the
    # declaration the write stops; a bomb can't fill the disk.
    pkg = _pkg(tmp_path, "oversize.atomicpkg",
               _manifest(files=[{"sha256": SHA, "byte_size": 4,
                                 "arcname": f"files/{SHA}"}]),
               members=[(f"files/{SHA}", b"x" * 64)])
    assert "exceeds declared" in _import_code(pkg)


def test_undersize_blob_refused(tmp_path):
    pkg = _pkg(tmp_path, "undersize.atomicpkg",
               _manifest(files=[{"sha256": SHA, "byte_size": 100,
                                 "arcname": f"files/{SHA}"}]),
               members=[(f"files/{SHA}", b"x")])
    assert "size mismatch" in _import_code(pkg)


def test_hash_mismatch_refused(tmp_path):
    content = b"honest-looking content"
    pkg = _pkg(tmp_path, "hashmis.atomicpkg",
               _manifest(files=[{"sha256": SHA,  # wrong sha on purpose
                                 "byte_size": len(content),
                                 "arcname": f"files/{SHA}"}]),
               members=[(f"files/{SHA}", content)])
    assert "hash mismatch" in _import_code(pkg)


def test_missing_blob_refused(tmp_path):
    pkg = _pkg(tmp_path, "missblob.atomicpkg",
               _manifest(files=[{"sha256": SHA, "byte_size": 4,
                                 "arcname": f"files/{SHA}"}]))
    assert "missing blob" in _import_code(pkg)


def test_symlink_member_is_plain_file_not_link(tmp_path):
    # A unix symlink-mode member: our extractor writes entry CONTENT —
    # links are never re-created. Content mismatches sha → refused
    # anyway; what matters is no symlink was ever created.
    p = tmp_path / "sym.atomicpkg"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("manifest.json", json.dumps(_manifest(
            files=[{"sha256": SHA, "byte_size": 9,
                    "arcname": f"files/{SHA}"}])))
        info = zipfile.ZipInfo(f"files/{SHA}")
        info.external_attr = 0o120777 << 16   # symlink mode
        info.create_system = 3                # unix
        zf.writestr(info, b"/etc/pass")
    with pytest.raises(PackageError):
        _import(p)
    assert not list(tmp_path.glob("*.staging"))
    # nothing linked outside staging
    assert not (tmp_path / "etc" / "passwd").exists()


def test_staging_removed_on_refusal(tmp_path):
    pkg = _pkg(tmp_path, "stageclean.atomicpkg",
               _manifest(files=[{"sha256": SHA, "byte_size": 4,
                                 "arcname": f"files/{SHA}"}]),
               members=[(f"files/{SHA}", b"data" * 100)])
    with pytest.raises(PackageError):
        _import(pkg)
    assert not (tmp_path / "stageclean.atomicpkg.staging").exists()


def test_zero_byte_blob_ok(tmp_path):
    # Edge: declared 0 bytes, member present but empty — allowed
    # through validation, then hash check governs (sha of b'' would
    # need to match; this sha doesn't → hash-mismatch refusal).
    pkg = _pkg(tmp_path, "zero.atomicpkg",
               _manifest(files=[{"sha256": SHA, "byte_size": 0,
                                 "arcname": f"files/{SHA}"}]),
               members=[(f"files/{SHA}", b"")])
    assert "hash mismatch" in _import_code(pkg)


# ---------- full-path API refusal (no partial mutation) ----------

def test_import_refused_leaves_no_project_trace(client, tmp_path):
    """End-to-end: a hostile package fails the job and creates nothing."""
    from tests.test_a8_wizard import _commit, _rev
    from tests.conftest import wait_for_job
    p = tmp_path / "api-evil.atomicpkg"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("manifest.json", json.dumps(_manifest(
            files=[{"sha256": SHA, "byte_size": 4,
                    "arcname": f"files/{SHA}"}])))
        zf.writestr(f"files/{SHA}", b"x" * 64)
    proj = client.post("/api/projects", json={"name": "Dest"}).json()
    before = client.get("/api/projects").json()
    r = client.post(f"/api/projects/{proj['id']}/commands/commit",
                    json={"command_type": "backup.import",
                          "payload": {"package_path": str(p)},
                          "scope": "project",
                          "expected_revision": _rev(client, proj["id"]),
                          "idempotency_key": "c2-evil-import-1"})
    assert r.status_code == 200, r.text
    job = wait_for_job(client, proj["id"], r.json()["job_id"], timeout=30)
    assert job["status"] == "failed"
    # No project created, and the target gained zero content — the commit
    # row itself may bump revision/updated_at, that's not a mutation leak.
    after = client.get("/api/projects").json()
    assert {p["id"] for p in after} == {p["id"] for p in before}
    after_dest = next(p for p in after if p["id"] == proj["id"])
    assert after_dest["counts"] == {"characters": 0, "worlds": 0,
                                    "locations": 0, "assets": 0}


def test_provider_and_credential_fields_still_stripped(client, tmp_path):
    """A8 sanitizer preserved on the hardened path: a package whose
    settings carry provider/auth material imports clean."""
    # Build a REAL export first so the package is otherwise valid.
    from tests.test_a8_wizard import _commit, _rev
    from tests.conftest import wait_for_job
    src = client.post("/api/projects", json={"name": "Src"}).json()
    r = client.post(f"/api/projects/{src['id']}/commands/commit",
                    json={"command_type": "backup.export",
                          "payload": {"kind": "backup"},
                          "scope": "project",
                          "expected_revision": _rev(client, src["id"]),
                          "idempotency_key": "c2-export-1"})
    job = wait_for_job(client, src["id"], r.json()["job_id"], timeout=30)
    assert job["status"] == "succeeded"
    pkg = Path(job["result_json"]["package_path"])
    # Inject hostile settings + provider block into the manifest copy.
    with zipfile.ZipFile(pkg) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        members = {n: zf.read(n) for n in zf.namelist()
                   if n != "manifest.json"}
    manifest["project"]["settings_json"] = {
        "providers": {"evil": {"enabled": True,
                               "base_url": "https://evil.example"}},
        "api_key": "AKIA-FAKE",
        "custom_pref": {"keep": True}}
    hostile = tmp_path / "hostile.atomicpkg"
    with zipfile.ZipFile(hostile, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for n, data in members.items():
            zf.writestr(n, data)
    r = client.post("/api/packages/import",
                    json={"package_path": str(hostile)})
    assert r.status_code == 200, r.text
    new_pid = r.json()["project_id"]
    wait_for_job(client, new_pid, r.json()["job_id"], timeout=30)
    got = client.get(f"/api/projects/{new_pid}").json()
    sj = got["settings_json"]
    assert "providers" not in sj
    assert "api_key" not in sj
    assert sj["custom_pref"] == {"keep": True}
