"""S2 — staged-upload retention and cleanup.

The upload-source staging dir is swept conservatively: fresh files and
files still referenced by recoverable jobs are kept; expired orphans are
unlinked individually without recursion, and reports carry counts+bytes
only (no paths).
"""
import io
import os
import time
from datetime import timedelta

from PIL import Image

from app.db.models import GenerationJob
from app.domain import upload_staging
from tests.conftest import make_project, wait_for_job


def _png(w=16, h=16, color=(10, 20, 30)):
    im = Image.new("RGB", (w, h), color)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def _stage(client, pid, data=None):
    data = data or _png()
    r = client.post(f"/api/projects/{pid}/assets/upload-source",
                    content=data,
                    headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    return r.json()


def _age(path, hours=48):
    old = time.time() - hours * 3600
    os.utime(path, (old, old))


def test_fresh_and_referenced_files_survive_expiry(client):
    """Expired files stay when a queued/running job still references
    them; fresh files are never touched."""
    p = make_project(client)
    pid = p["id"]
    body = _stage(client, pid)
    staged = body["path"]

    sf = client.app.state.session_factory
    # Simulate a recoverable job holding this staging path.
    with sf() as s:
        s.add(GenerationJob(
            id="s2-refjob", project_id=pid, kind="import",
            adapter_id="ops.asset.import", status="queued",
            request_json={"command_type": "asset.import",
                          "source_path": staged},
            idempotency_key="s2-ref-01"))
        s.commit()
    _age(staged)

    dd = client.app.state.settings.data_dir
    with sf() as s:
        r = upload_staging.cleanup_project_staged(s, dd, pid)
    assert r["deleted"] == 0 and r["kept"] == 1
    assert os.path.exists(staged)

    # Job gone terminal → next sweep reclaims it.
    with sf() as s:
        j = s.get(GenerationJob, "s2-refjob")
        j.status = "succeeded"
        s.commit()
        r = upload_staging.cleanup_project_staged(s, dd, pid)
    assert r["deleted"] == 1 and not os.path.exists(staged)


def test_retention_window_and_lazy_sweep(client):
    """Files inside the retention window survive; an expired orphan is
    reclaimed by the next upload's lazy sweep — counts only, no paths."""
    p = make_project(client)
    pid = p["id"]
    old = _stage(client, pid, _png(color=(1, 2, 3)))
    _age(old["path"])

    body = _stage(client, pid, _png(color=(9, 9, 9)))
    assert body.get("staging_sweep", {}).get("deleted") == 1
    assert not os.path.exists(old["path"])
    assert os.path.exists(body["path"])
    # No server paths leak through the sweep report.
    sweep = body["staging_sweep"]
    assert set(sweep) == {"deleted", "reclaimed_bytes"}


def test_cleanup_does_not_recurse_or_touch_cas(client, tmp_path):
    """Directories inside staging are ignored (no recursion) and CAS
    blobs elsewhere are never candidates."""
    p = make_project(client)
    pid = p["id"]
    dd = client.app.state.settings.data_dir
    up = upload_staging.uploads_dir(dd, pid)
    up.mkdir(parents=True)
    nested = up / "subdir"
    nested.mkdir()
    victim = nested / "stray.png"
    victim.write_bytes(b"x")
    (up / "old.png").write_bytes(b"x")
    _age(up / "old.png")

    with client.app.state.session_factory() as s:
        r = upload_staging.cleanup_project_staged(s, dd, pid)
    assert r["deleted"] == 1
    assert victim.exists()                    # subdirectory untouched
    assert not (up / "old.png").exists()


def test_upload_import_then_sweep_keeps_cas_asset(client):
    """A staged file may be swept after import — the CAS asset lives on."""
    p = make_project(client)
    pid = p["id"]
    body = _stage(client, pid)
    rev = client.get(f"/api/projects/{pid}").json()["revision"]
    imp = client.post(f"/api/projects/{pid}/commands/commit",
                      json={"command_type": "asset.import",
                            "payload": {"source_path": body["path"],
                                        "name": "s2.png", "kind": "image"},
                            "scope": "project",
                            "expected_revision": rev,
                            "idempotency_key": "s2-imp-01"})
    job = wait_for_job(client, pid, imp.json()["job_id"])
    assert job["status"] == "succeeded"
    _age(body["path"])
    dd = client.app.state.settings.data_dir
    with client.app.state.session_factory() as s:
        r = upload_staging.cleanup_project_staged(s, dd, pid)
    assert r["deleted"] == 1
    # CAS blob is content-addressed — the import result must persist.
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    assert any(a["name"] == "s2.png" for a in items)
    cas = next(a for a in items if a["name"] == "s2.png")
    assert "uploads" not in cas["uri"].replace("\\", "/")


def test_sweep_isolation_between_projects(client):
    p1, p2 = make_project(client), make_project(client)
    a = _stage(client, p1["id"], _png(color=(5, 5, 5)))
    b = _stage(client, p2["id"], _png(color=(6, 6, 6)))
    _age(a["path"])
    dd = client.app.state.settings.data_dir
    with client.app.state.session_factory() as s:
        upload_staging.cleanup_project_staged(s, dd, p1["id"])
    assert not os.path.exists(a["path"])
    assert os.path.exists(b["path"])          # other project untouched


def test_sweep_all_staged_reports_totals(client):
    p = make_project(client)
    a = _stage(client, p["id"], _png(color=(7, 7, 7)))
    _stage(client, p["id"], _png(color=(8, 8, 8)))   # fresh — survives
    _age(a["path"])
    dd = client.app.state.settings.data_dir
    total = upload_staging.sweep_all_staged(
        client.app.state.session_factory, dd)
    assert total["deleted"] == 1 and total["kept"] >= 1
    assert total["reclaimed_bytes"] > 0


def _refjob(s, pid, jid, status, path):
    s.add(GenerationJob(
        id=jid, project_id=pid, kind="import",
        adapter_id="ops.asset.import", status=status,
        request_json={"command_type": "asset.import", "source_path": path},
        idempotency_key=f"k-{jid}"))
    s.commit()


def test_running_job_references_protected(client):
    """'running' is also recoverable — reconcile_interrupted re-queues
    it after a crash, so its staging file must survive."""
    p = make_project(client)
    pid = p["id"]
    staged = _stage(client, pid)["path"]
    _age(staged)
    sf = client.app.state.session_factory
    dd = client.app.state.settings.data_dir
    with sf() as s:
        _refjob(s, pid, "s2-run-1", "running", staged)
        r = upload_staging.cleanup_project_staged(s, dd, pid)
    assert r["deleted"] == 0 and os.path.exists(staged)


def test_unlink_failure_keeps_file_and_counts(client, monkeypatch):
    """A file that refuses unlink (e.g. held open by a live import
    read) is kept this round and counted — the sweep never raises."""
    from pathlib import Path
    p = make_project(client)
    pid = p["id"]
    staged = _stage(client, pid)["path"]
    _age(staged)
    dd = client.app.state.settings.data_dir
    real_unlink = Path.unlink

    def flaky(self, *a, **kw):
        if str(self) == staged:
            raise OSError("file in use")
        return real_unlink(self, *a, **kw)
    monkeypatch.setattr(Path, "unlink", flaky)
    with client.app.state.session_factory() as s:
        r = upload_staging.cleanup_project_staged(s, dd, pid)
    assert r["deleted"] == 0 and r["kept"] == 1
    assert os.path.exists(staged)


def test_symlinks_never_deleted_or_followed(client, tmp_path):
    """A planted symlink inside staging is skipped entirely — neither
    the link nor its outside target is touched by the sweeper."""
    p = make_project(client)
    pid = p["id"]
    dd = client.app.state.settings.data_dir
    up = upload_staging.uploads_dir(dd, pid)
    up.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"keep me")
    link = up / "link.png"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("symlink creation not permitted")
    _age(up / "decoy.png") if (up / "decoy.png").write_bytes(b"x") else None
    with client.app.state.session_factory() as s:
        r = upload_staging.cleanup_project_staged(s, dd, pid)
    assert link.exists() or link.is_symlink()
    assert outside.read_bytes() == b"keep me"


def test_repeated_sweep_is_idempotent(client):
    p = make_project(client)
    pid = p["id"]
    staged = _stage(client, pid)["path"]
    _age(staged)
    dd = client.app.state.settings.data_dir
    with client.app.state.session_factory() as s:
        r1 = upload_staging.cleanup_project_staged(s, dd, pid)
        r2 = upload_staging.cleanup_project_staged(s, dd, pid)
    assert r1["deleted"] == 1 and r2["deleted"] == 0
