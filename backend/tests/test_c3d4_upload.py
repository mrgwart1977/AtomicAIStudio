"""C3-D4 — editor mask upload staging endpoint.

upload-source stages painted image bytes to a server-owned path so the
standard asset.import command can ingest them through CAS — no parallel
ingest path, no caller-controlled filename.
"""
from __future__ import annotations

import io

from PIL import Image

from tests.conftest import make_project, wait_for_job


def _png_bytes(size=(32, 24), color=(255, 255, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _rev(client, pid):
    return client.get(f"/api/projects/{pid}").json()["revision"]


def test_upload_source_stages_bytes_and_import_roundtrip(client, tmp_path):
    p = make_project(client)
    data = _png_bytes()
    r = client.post(f"/api/projects/{p['id']}/assets/upload-source",
                    content=data,
                    headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["width"] == 32 and body["height"] == 24
    assert body["sha256"]
    # Path is server-owned under the project's uploads dir.
    assert "uploads" in body["path"].replace("\\", "/")
    # Idempotent on identical bytes — same staged path.
    r2 = client.post(f"/api/projects/{p['id']}/assets/upload-source",
                     content=data,
                     headers={"Content-Type": "application/octet-stream"})
    assert r2.json()["path"] == body["path"]
    # The staged path feeds the normal import job.
    imp = client.post(f"/api/projects/{p['id']}/commands/commit",
                      json={"command_type": "asset.import",
                            "payload": {"source_path": body["path"],
                                        "name": "painted-mask.png",
                                        "kind": "image"},
                            "scope": "project",
                            "expected_revision": _rev(client, p["id"]),
                            "idempotency_key": "c3d4-up-1"})
    assert imp.status_code == 200, imp.text
    job = wait_for_job(client, p["id"], imp.json()["job_id"])
    assert job["status"] == "succeeded", job
    assets = client.get(f"/api/projects/{p['id']}/assets").json()["items"]
    assert any(a["name"] == "painted-mask.png" for a in assets)


def test_upload_source_rejects_non_image(client):
    p = make_project(client)
    r = client.post(f"/api/projects/{p['id']}/assets/upload-source",
                    content=b"not an image at all",
                    headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 422


def test_upload_source_project_isolation(client):
    p = make_project(client)
    p2 = make_project(client)
    r = client.post(f"/api/projects/{p['id']}/assets/upload-source",
                    content=_png_bytes(),
                    headers={"Content-Type": "application/octet-stream"})
    path = r.json()["path"]
    # Another project's upload staging is namespaced per project — the
    # same bytes land in p2's own dir, never shared.
    r2 = client.post(f"/api/projects/{p2['id']}/assets/upload-source",
                     content=_png_bytes(),
                     headers={"Content-Type": "application/octet-stream"})
    assert r2.json()["path"] != path
