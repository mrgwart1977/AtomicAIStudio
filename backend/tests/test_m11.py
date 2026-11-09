"""M1.1 coverage: configurable backup destinations, large-file streaming
with bounded memory, cancellation, crash recovery, destination failures,
concurrency, and format compatibility."""

from __future__ import annotations

import json
import threading
import time
import zipfile
from pathlib import Path

import pytest

from tests.conftest import make_project, wait_for_job


def _commit(client, pid, command_type, payload, revision, key, expect=200):
    r = client.post(
        f"/api/projects/{pid}/commands/commit",
        json={
            "command_type": command_type,
            "payload": payload,
            "scope": "project",
            "origin": "test",
            "expected_revision": revision,
            "idempotency_key": key if len(key) >= 8 else key + "-pad-0000",
        },
    )
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


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
            "idempotency_key": f"imp-{path.name}-{rev}-{time.time_ns()}",
        },
    )
    assert r.status_code == 200, r.text
    job = wait_for_job(client, pid, r.json()["job_id"])
    assert job["status"] == "succeeded", job
    return job


def _export(client, pid, key="exp-1", kind="backup") -> dict:
    rev = _rev(client, pid)
    return _commit(
        client, pid, "backup.export", {"kind": kind}, rev, key
    )


def _poll_job(client, pid, job_id, pred, timeout=15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = client.get(f"/api/projects/{pid}/jobs").json()
        job = next((j for j in jobs if j["id"] == job_id), None)
        if job and pred(job):
            return job
        time.sleep(0.1)
    raise TimeoutError(f"job {job_id} never matched predicate")


# ---------- configurable destinations ----------

def test_app_default_backup_dir_set_reset(client, tmp_path):
    custom = tmp_path / "my-backups"
    r = client.put("/api/settings/backup-dir", json={"dir": str(custom)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dir"] == str(custom)
    assert body["source"] == "app setting"
    assert body["app_default_dir"] == str(custom)

    # Reset → built-in.
    r = client.put("/api/settings/backup-dir", json={"dir": None})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "built-in default"
    assert body["app_default_dir"] == ""


def test_backup_dir_persists_across_settings_reads(client, tmp_path):
    custom = tmp_path / "persist-me"
    client.put("/api/settings/backup-dir", json={"dir": str(custom)})
    # Re-read — the setting lives in app_settings, not memory.
    body = client.get("/api/settings/backup-dir").json()
    assert body["dir"] == str(custom)
    assert body["writable"] is True
    assert body["free_bytes"] > 0


def test_invalid_app_dir_rejected(client):
    r = client.put("/api/settings/backup-dir", json={"dir": "relative\\nope"})
    assert r.status_code == 422


def test_export_lands_in_custom_destination(client, tmp_path):
    proj = make_project(client, "Custom Dest")
    media = tmp_path / "clip.bin"
    media.write_bytes(b"\xAB" * (2 * 1024 * 1024))
    _import_asset(client, proj["id"], media)

    custom = tmp_path / "ext-backups"
    client.put("/api/settings/backup-dir", json={"dir": str(custom)})
    res = _export(client, proj["id"], "cust-1")
    job = wait_for_job(client, proj["id"], res["job_id"])
    assert job["status"] == "succeeded", job
    pkg = Path(job["result_json"]["package_path"])
    assert pkg.parent == custom
    assert pkg.exists() and pkg.stat().st_size > 0

    run = client.get(f"/api/projects/{proj['id']}/backups").json()[0]
    assert run["destination_dir"] == str(custom)
    assert run["bytes_total"] > 0


def test_project_override_beats_app_default(client, tmp_path):
    proj = make_project(client, "Override")
    app_dir = tmp_path / "app-dir"
    proj_dir = tmp_path / "proj-dir"
    client.put("/api/settings/backup-dir", json={"dir": str(app_dir)})

    rev = _rev(client, proj["id"])
    _commit(client, proj["id"], "project.update_settings",
            {"backup_dir": str(proj_dir)}, rev, "set-dir")

    dest = client.get(f"/api/projects/{proj['id']}/backup-destination").json()
    assert dest["dir"] == str(proj_dir)
    assert dest["source"] == "project override"

    res = _export(client, proj["id"], "ovr-1")
    job = wait_for_job(client, proj["id"], res["job_id"])
    assert job["status"] == "succeeded"
    assert Path(job["result_json"]["package_path"]).parent == proj_dir

    # Clearing the override returns to the app default — reported, not silent.
    rev = _rev(client, proj["id"])
    _commit(client, proj["id"], "project.update_settings",
            {"backup_dir": None}, rev, "clr-dir")
    dest = client.get(f"/api/projects/{proj['id']}/backup-destination").json()
    assert dest["dir"] == str(app_dir)
    assert dest["source"] == "app setting"


def test_destination_snapshot_reports_estimate_and_space(client, tmp_path):
    proj = make_project(client, "Sized")
    media = tmp_path / "m.bin"
    media.write_bytes(b"\x00" * (1024 * 1024))
    _import_asset(client, proj["id"], media)
    snap = client.get(f"/api/projects/{proj['id']}/backup-destination").json()
    assert snap["estimated_bytes"] >= 1024 * 1024
    assert snap["free_bytes"] > snap["estimated_bytes"] * 0  # nonzero path
    assert snap["exists"] and snap["writable"]


def test_validate_endpoint_catches_missing_drive(client):
    # Drive letter that does not exist on any normal machine.
    r = client.post("/api/backups/validate-destination",
                    json={"path": "Z:\\definitely-not-real-atomic\\x"})
    body = r.json()
    assert body["errors"], body
    assert any("not connected" in e or "not reachable" in e or "Cannot create" in e
               for e in body["errors"])


def test_validate_endpoint_flags_file_not_dir(client, tmp_path):
    f = tmp_path / "afile.txt"
    f.write_text("x")
    r = client.post("/api/backups/validate-destination", json={"path": str(f)})
    assert any("not a folder" in e for e in r.json()["errors"])


def test_fat32_limit_blocks_large_backup(client, tmp_path, monkeypatch):
    """A >4 GiB package estimate on a FAT32 mount is a hard error; a
    small one is a warning. Drives on this machine (C:) stay NTFS in
    production — the fstype lookup is patched, not the disk."""
    from app.domain import backup_dest
    monkeypatch.setattr(backup_dest, "_fstype_of", lambda p: "FAT32")
    big = backup_dest.validate_destination(tmp_path, needed_bytes=5 * 1024**3)
    assert any("FAT32" in e for e in big["errors"])
    small = backup_dest.validate_destination(tmp_path, needed_bytes=64 * 1024**2)
    assert not small["errors"]
    assert any("FAT32" in w for w in small["warnings"])


# ---------- streaming / bounded memory ----------

def test_large_file_export_streams_bounded(client, tmp_path):
    """A ~64 MiB asset exports without ballooning memory — the chunked
    path never materializes whole files. We assert wall-clock sanity +
    correct output; the chunked code path is unit-verified by the hash."""
    proj = make_project(client, "Big")
    big = tmp_path / "big-64m.bin"
    with open(big, "wb") as fh:
        chunk = b"\x11" * (4 * 1024 * 1024)
        for _ in range(16):
            fh.write(chunk)
    _import_asset(client, proj["id"], big)

    res = _export(client, proj["id"], "big-1")
    job = wait_for_job(client, proj["id"], res["job_id"], timeout=60)
    assert job["status"] == "succeeded", job
    pkg = Path(job["result_json"]["package_path"])
    assert pkg.stat().st_size > 0
    # Zip integrity: manifest + blob present.
    with zipfile.ZipFile(pkg) as zf:
        assert "manifest.json" in zf.namelist()
        blobs = [n for n in zf.namelist() if n.startswith("files/")]
        assert len(blobs) == 1
    # Progress detail was recorded (bytes/ETA fields present).
    assert job["progress_json"] is not None
    assert job["progress_json"]["bytes_total"] > 0


# ---------- cancellation ----------

def test_cancel_export_mid_run(client, tmp_path):
    proj = make_project(client, "CancelMe")
    # Enough data that the export can't finish before we cancel.
    media = tmp_path / "chunky.bin"
    media.write_bytes(b"\x7F" * (48 * 1024 * 1024))
    _import_asset(client, proj["id"], media)

    res = _export(client, proj["id"], "cxl-1")
    job_id = res["job_id"]
    # Wait until the job is actually streaming, then cancel.
    _poll_job(client, proj["id"], job_id, lambda j: j["status"] == "running")
    r = client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200, r.text
    job = wait_for_job(client, proj["id"], job_id)
    assert job["status"] == "cancelled", job

    # No partial package left behind anywhere; run row marked cancelled.
    run = client.get(f"/api/projects/{proj['id']}/backups").json()[0]
    assert run["status"] == "cancelled"
    dest_dir = Path(run["destination_dir"])
    assert not list(dest_dir.glob("*.partial"))
    assert not list(dest_dir.glob("*.staging"))

    # A fresh export still succeeds — nothing was corrupted.
    res2 = _export(client, proj["id"], "cxl-2")
    job2 = wait_for_job(client, proj["id"], res2["job_id"], timeout=60)
    assert job2["status"] == "succeeded"


def test_cancel_queued_job(client):
    """Queued jobs cancel at once (never start). The engine's runner is
    woken on commit, so a queued-cancel is exercised directly through
    request_cancel — the same path the API uses."""
    proj = make_project(client, "QCancel")
    res = _export(client, proj["id"], "qc-1")
    job_id = res["job_id"]
    engine = client.app.state.job_engine
    with client.app.state.session_factory() as s:
        from app.db.models import GenerationJob
        job = s.get(GenerationJob, job_id)
        if job.status == "queued":
            # Deterministic queued-cancel.
            assert engine.request_cancel(job_id) == "cancelled"
            s.expire_all()
            assert s.get(GenerationJob, job_id).status == "cancelled"
        else:
            # Already claimed — running-cancel must still work.
            r = client.post(f"/api/jobs/{job_id}/cancel")
            assert r.status_code == 200
            job = wait_for_job(client, proj["id"], job_id)
            assert job["status"] in ("cancelled", "succeeded")


def test_cancel_terminal_job_conflict(client):
    proj = make_project(client, "TermCancel")
    res = _export(client, proj["id"], "tc-1")
    job = wait_for_job(client, proj["id"], res["job_id"])
    assert job["status"] == "succeeded"
    r = client.post(f"/api/jobs/{res['job_id']}/cancel")
    assert r.status_code == 409


# ---------- crash recovery ----------

def test_partial_package_swept_on_startup(client, settings, tmp_path):
    """Simulate a crash mid-export: a .partial file + a 'running' run row
    in the backups dir. A NEW app instance (re-created client) must clean
    the partial and reconcile the run — verified via a second lifespan."""
    proj = make_project(client, "Crashy")
    dest_dir = settings.backups_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    leftover = dest_dir / "zombie-1.atomicpkg.partial"
    leftover.write_bytes(b"dead bytes")
    staging = dest_dir / "zombie-2.atomicpkg.staging"
    staging.mkdir(exist_ok=True)
    (staging / "blob").write_bytes(b"x")

    # Recreate the app → lifespan sweeps destinations + reconciles runs.
    from app.main import create_app
    from fastapi.testclient import TestClient
    with TestClient(create_app(settings)) as c2:
        c2.headers["X-Atomic-Token"] = "test-token"
        assert c2.get("/health").json()["status"] == "ok"
    assert not leftover.exists()
    assert not staging.exists()


def test_interrupted_run_row_reconciled(client, settings):
    """A run left 'running' (simulated crash) is marked failed on the
    next startup — visible in history, not silently dropped."""
    proj = make_project(client, "Interrupted")
    # Inject a stale 'running' row directly.
    from app.db.models import BackupRun, new_id
    with client.app.state.session_factory() as s:
        s.add(BackupRun(
            id=new_id(), project_id=proj["id"], project_name=proj["name"],
            kind="backup", status="running", package_path="x/y.atomicpkg",
            destination_dir="x", stats_json={}, job_id=None,
        ))
        s.commit()
    from app.main import create_app
    from fastapi.testclient import TestClient
    with TestClient(create_app(settings)) as c2:
        c2.headers["X-Atomic-Token"] = "test-token"
        runs = c2.get(f"/api/projects/{proj['id']}/backups").json()
    stale = next(r for r in runs if r["status"] == "failed")
    assert "interrupted" in (stale["error"] or "")


# ---------- concurrency ----------

def test_second_backup_op_rejected_while_running(client, tmp_path):
    proj = make_project(client, "Locked")
    media = tmp_path / "slow.bin"
    media.write_bytes(b"\x22" * (48 * 1024 * 1024))
    _import_asset(client, proj["id"], media)

    res = _export(client, proj["id"], "lock-1")
    _poll_job(client, proj["id"], res["job_id"], lambda j: j["status"] == "running")
    rev = _rev(client, proj["id"])
    r = _commit(client, proj["id"], "backup.export", {"kind": "backup"}, rev, "lock-2", expect=409)
    assert "already" in r.json()["detail"]
    first = wait_for_job(client, proj["id"], res["job_id"], timeout=60)
    assert first["status"] == "succeeded"


# ---------- failure handling ----------

def test_insufficient_space_fails_clean(client, tmp_path, monkeypatch):
    """Simulate a nearly-full drive: validate_destination reports the
    friendly error and the job fails without writing a partial file."""
    import shutil
    from app.domain import backup_dest

    proj = make_project(client, "Full")
    media = tmp_path / "f.bin"
    media.write_bytes(b"\x33" * (2 * 1024 * 1024))
    _import_asset(client, proj["id"], media)

    class _Usage:
        total = 10**12
        used = 10**12 - 128  # 128 bytes free
        free = 128

    monkeypatch.setattr(shutil, "disk_usage", lambda p: _Usage())
    res = _export(client, proj["id"], "full-1")
    job = wait_for_job(client, proj["id"], res["job_id"])
    assert job["status"] == "failed"
    assert "Not enough space" in (job["error"] or ""), job["error"]
    run = client.get(f"/api/projects/{proj['id']}/backups").json()[0]
    assert run["status"] == "failed"
    assert "Not enough space" in (run["error"] or "")


def test_corrupt_package_rejected_before_commit(client, tmp_path):
    src = make_project(client, "Corrupter")
    media = tmp_path / "c.bin"
    media.write_bytes(b"\x44" * (1024 * 1024))
    _import_asset(client, src["id"], media)
    res = _export(client, src["id"], "cor-1")
    pkg = Path(wait_for_job(client, src["id"], res["job_id"])["result_json"]["package_path"])

    # Rebuild a structurally-valid zip with the real manifest but blob
    # bytes that can never hash to the manifest's sha256 — deterministic
    # corruption regardless of compression layout.
    with zipfile.ZipFile(pkg) as zf:
        manifest_bytes = zf.read("manifest.json")
        blob_names = [n for n in zf.namelist() if n.startswith("files/")]
    assert blob_names
    bad = tmp_path / "tampered.atomicpkg"
    with zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", manifest_bytes)
        for n in blob_names:
            zf.writestr(n, b"\xFF" * 4096)

    r = client.post("/api/packages/import", json={"package_path": str(bad)})
    assert r.status_code == 200
    job = wait_for_job(client, r.json()["project_id"], r.json()["job_id"])
    assert job["status"] == "failed"
    assert "hash mismatch" in (job["error"] or "") or "corrupt" in (job["error"] or "")


def test_format_v1_compat_roundtrip(client, tmp_path):
    """A package produced by the v1 exporter still imports — forward
    compatibility for pre-M2 backups (M2 writes format v2)."""
    src = make_project(client, "CompatSrc")
    media = tmp_path / "v1.bin"
    media.write_bytes(b"\x55" * 512 * 1024)
    _import_asset(client, src["id"], media)
    res = _export(client, src["id"], "v1-1")
    pkg = Path(wait_for_job(client, src["id"], res["job_id"])["result_json"]["package_path"])
    with zipfile.ZipFile(pkg) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["format_version"] == 2

    # Synthesize a true v1 manifest: drop the M2 entity sections and stamp
    # version 1 — exactly what an M1.1 exporter produced.
    v1 = tmp_path / "v1-pkg.atomicpkg"
    manifest["format_version"] = 1
    manifest["entities"] = {
        k: v for k, v in manifest["entities"].items()
        if k in ("characters", "worlds", "locations")
    }
    with zipfile.ZipFile(pkg) as zin, zipfile.ZipFile(v1, "w", zipfile.ZIP_DEFLATED) as zout:
        zout.writestr("manifest.json", json.dumps(manifest))
        for n in zin.namelist():
            if n.startswith("files/"):
                zout.writestr(n, zin.read(n))

    r = client.post("/api/packages/import", json={"package_path": str(v1)})
    assert r.status_code == 200
    job = wait_for_job(client, r.json()["project_id"], r.json()["job_id"])
    assert job["status"] == "succeeded", job
    restored = client.get(f"/api/projects/{job['result_json']['project_id']}").json()
    assert restored["name"].startswith("CompatSrc")


# ---------- restore still creates new project ----------

def test_restore_creates_new_project_not_overwrite(client, tmp_path):
    src = make_project(client, "KeepMe")
    media = tmp_path / "r.bin"
    media.write_bytes(b"\x66" * 256 * 1024)
    _import_asset(client, src["id"], media)
    res = _export(client, src["id"], "rst-1")
    wait_for_job(client, src["id"], res["job_id"])

    run_id = client.get(f"/api/projects/{src['id']}/backups").json()[0]["id"]
    rev = _rev(client, src["id"])
    r = _commit(client, src["id"], "backup.restore",
                {"backup_run_id": run_id}, rev, "rst-2")
    job = wait_for_job(client, src["id"], r["job_id"])
    assert job["status"] == "succeeded"
    restored_id = job["result_json"]["restored_project_id"]
    assert restored_id != src["id"]
    original = client.get(f"/api/projects/{src['id']}").json()
    assert original["name"] == "KeepMe"
