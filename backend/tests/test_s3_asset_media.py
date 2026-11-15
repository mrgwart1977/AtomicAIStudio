"""S3 — asset media_json dimension correctness.

New imports record width/height at ingestion; legacy rows whose
AssetVersion.media_json lacks dims are backfilled read-only in the list
response — no destructive migration, CAS hashes and identity unchanged.
"""
import io

from PIL import Image

from app.db.models import AssetVersion
from tests.conftest import make_project, wait_for_job


def _png(w=20, h=12):
    im = Image.new("RGB", (w, h), (30, 60, 90))
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def _stage_import(client, pid, name="s3.png"):
    r = client.post(f"/api/projects/{pid}/assets/upload-source",
                    content=_png(),
                    headers={"Content-Type": "application/octet-stream"})
    rev = client.get(f"/api/projects/{pid}").json()["revision"]
    imp = client.post(f"/api/projects/{pid}/commands/commit",
                      json={"command_type": "asset.import",
                            "payload": {"source_path": r.json()["path"],
                                        "name": name, "kind": "image"},
                            "scope": "project",
                            "expected_revision": rev,
                            "idempotency_key": "s3-imp-01"})
    assert wait_for_job(client, pid, imp.json()["job_id"])["status"] == "succeeded"
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    return next(a for a in items if a["name"] == name)


def test_new_import_records_dimensions(client):
    p = make_project(client)
    a = _stage_import(client, p["id"])
    media = a["media_json"]
    assert media["width"] == 20 and media["height"] == 12
    assert media["format"] == "PNG"


def test_legacy_empty_media_backfilled_read_only(client):
    """A pre-S3 row (media_json={}) gets dims in the list response while
    the stored row itself is untouched."""
    p = make_project(client)
    a = _stage_import(client, p["id"])
    sf = client.app.state.session_factory
    with sf() as s:
        v = s.get(AssetVersion, a["current_version_id"])
        v.media_json = {}                     # simulate legacy row
        s.commit()
    items = client.get(f"/api/projects/{p['id']}/assets").json()["items"]
    got = next(x for x in items if x["id"] == a["id"])
    assert got["media_json"]["width"] == 20
    assert got["media_json"]["height"] == 12
    # Stored row remains as-was — the backfill is response-only.
    with sf() as s:
        assert s.get(AssetVersion, a["current_version_id"]).media_json == {}
    # CAS identity never changed.
    assert got["sha256"] == a["sha256"]


def test_missing_file_fails_safe(client):
    """Backfill on an unreadable CAS blob leaves media_json empty rather
    than breaking the asset list."""
    p = make_project(client)
    a = _stage_import(client, p["id"])
    sf = client.app.state.session_factory
    with sf() as s:
        v = s.get(AssetVersion, a["current_version_id"])
        v.media_json = {}
        s.commit()
    # Remove the CAS blob so the probe hits the fail-safe path.
    with sf() as s:
        storage = client.app.state.storage
        from app.db.models import Asset
        asset = s.get(Asset, a["id"])
        path = storage.asset_path(s, asset)
    path.unlink()
    items = client.get(f"/api/projects/{p['id']}/assets").json()["items"]
    got = next(x for x in items if x["id"] == a["id"])
    assert got["media_json"] == {}


def test_video_import_records_probe_failure(client, tmp_path):
    """S-C5 — a non-video file imported as kind=video must not silently
    pass as a usable video: the job output records probe.ok=False and a
    visible warning. The CAS registration itself is legitimate."""
    p = make_project(client)
    fake = tmp_path / "notavideo.mp4"
    fake.write_bytes(b"this is plain text, not a container")
    rev = client.get(f"/api/projects/{p['id']}").json()["revision"]
    imp = client.post(f"/api/projects/{p['id']}/commands/commit",
                      json={"command_type": "asset.import",
                            "payload": {"source_path": str(fake),
                                        "name": "notavideo.mp4", "kind": "video"},
                            "scope": "project",
                            "expected_revision": rev,
                            "idempotency_key": "sc5-badvid-01"})
    assert imp.status_code == 200, imp.text
    job = wait_for_job(client, p["id"], imp.json()["job_id"])
    # registration succeeds (CAS ingest is honest)…
    assert job["status"] == "succeeded"
    # …but the asset must not claim clean media
    items = client.get(f"/api/projects/{p['id']}/assets").json()["items"]
    a = next(x for x in items if x["name"] == "notavideo.mp4")
    probe = ((a.get("media_json") or {}).get("probe") or {})
    assert probe.get("ok") is False, a["media_json"]
    assert a["media_json"].get("warning"), a["media_json"]


def test_existing_media_not_overwritten(client):
    """Generation-style media (prompt/model provenance) is preserved;
    only missing dims are filled."""
    p = make_project(client)
    a = _stage_import(client, p["id"])
    sf = client.app.state.session_factory
    with sf() as s:
        v = s.get(AssetVersion, a["current_version_id"])
        v.media_json = {"adapter_id": "x", "prompt": "y"}   # no width
        s.commit()
    items = client.get(f"/api/projects/{p['id']}/assets").json()["items"]
    got = next(x for x in items if x["id"] == a["id"])
    assert got["media_json"]["width"] == 20
    assert got["media_json"]["prompt"] == "y"
