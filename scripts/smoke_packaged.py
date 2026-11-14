"""Packaged-backend smoke: drives the live sidecar spawned by the built
atomic-dream-engine.exe through the C3-D3/D4 edit flow."""
import io, time, uuid
import httpx
from PIL import Image

BASE = "http://127.0.0.1:59426"
H = {"X-Atomic-Token": "472587f8-34ff-4729-a884-96ca3d2df9fe"}
W, H_ = 64, 48
c = httpx.Client(base_url=BASE, headers=H, timeout=30)


def wait_job(pid, jid, t=60):
    end = time.time() + t
    while time.time() < end:
        jobs = c.get(f"/api/projects/{pid}/jobs").json()
        j = next((x for x in jobs if x["id"] == jid), None)
        if j and j["status"] in {"succeeded", "failed", "cancelled"}:
            return j
        time.sleep(0.3)
    raise TimeoutError(jid)


def commit(pid, ct, payload):
    rev = c.get(f"/api/projects/{pid}").json()["revision"]
    r = c.post(f"/api/projects/{pid}/commands/commit",
               json={"command_type": ct, "payload": payload,
                     "scope": "project", "expected_revision": rev,
                     "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    return r.json()


def png(fill):
    im = Image.new("RGB", (W, H_), fill)
    b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()


def mask_png():
    im = Image.new("L", (W, H_), 0)
    px = im.load()
    for x in range(W // 2):
        for y in range(H_):
            px[x, y] = 255          # left half editable (>=128)
    b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()


def import_img(pid, data, name):
    r = c.post(f"/api/projects/{pid}/assets/upload-source",
               content=data,
               headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    j = commit(pid, "asset.import",
               {"source_path": r.json()["path"], "name": name,
                "kind": "image"})
    assert wait_job(pid, j["job_id"])["status"] == "succeeded"
    items = c.get(f"/api/projects/{pid}/assets").json()["items"]
    return next(a for a in items if a["name"] == name)


p = c.get("/api/projects").json()[0]
pid = p["id"]
print(f"1. project: {p['name']} ({pid})")

tag = uuid.uuid4().hex[:6]
src = import_img(pid, png((40, 90, 160)), f"src-{tag}.png")
src_sha = src["sha256"]
print(f"2. source asset {src['id']} sha={src_sha[:12]}")

msk = import_img(pid, mask_png(), f"mask-{tag}.png")
assert msk["project_id"] == pid
print(f"3-6. mask imported, project-owned: {msk['id']}")

ic = c.get("/api/providers/image-contracts").json()
assert "bfl-flux" in ic["inpaint_implemented"]
print("7. model flux-pro-1.0-fill (inpaint_implemented)")

PAY = {"provider_key": "bfl-flux", "model_id": "flux-pro-1.0-fill",
       "operation": "inpaint", "execution_target": "cloud",
       "prompt": "fill with forest", "source_asset_id": src["id"],
       "mask_asset_id": msk["id"]}
pr = c.post(f"/api/projects/{pid}/commands/preview",
            json={"command_type": "generate.image.edit", "payload": PAY,
                  "scope": "project"})
assert pr.status_code == 200, pr.text
print(f"8. preview admitted: {str(pr.json())[:140]}")

j = commit(pid, "generate.image.edit", PAY)
job = wait_job(pid, j["job_id"])
assert job["status"] == "succeeded", job
assert job["adapter_id"] == "cloud.image.edit.sim"
print(f"9-11. inpaint job succeeded via {job['adapter_id']}")

items = c.get(f"/api/projects/{pid}/assets").json()["items"]
out = next(a for a in items if a["id"] == job["output_asset_id"])
print(f"12-13. output asset {out['id']} derived (job lineage)")

items = c.get(f"/api/projects/{pid}/assets").json()["items"]
assert next(a for a in items if a["id"] == src["id"])["sha256"] == src_sha
print("14. source sha256 unchanged")

for anchor in ("center", "bottom_right"):
    pay = {"provider_key": "bfl-flux", "model_id": "flux-pro-1.0-expand",
           "operation": "outpaint", "execution_target": "cloud",
           "prompt": "extend the scene", "out_width": W * 2,
           "out_height": H_ * 2, "placement": anchor,
           "source_asset_id": src["id"]}
    j = commit(pid, "generate.image.edit", pay)
    job = wait_job(pid, j["job_id"])
    assert job["status"] == "succeeded", (anchor, job)
    print(f"    outpaint '{anchor}' job {job['id']} succeeded")

print("PACKAGED SMOKE: PASS")
