"""C4-D — shot-level video generation.

`shot.video` composes shot bindings (prompt, source image, continuity
guidance) into the REAL generate.video pipeline server-side — local
installed video models, same durable job engine, no parallel pipeline.
Execution of the local model is out of scope for tests (fake bytes);
these tests cover composition, validation, staleness, provenance, and
storyboard surfacing.
"""
import dataclasses
import io

import pytest
from PIL import Image

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import (
    _board, _char, _commit, _import, _job_req, _preview, _refset, _rev,
    _scene, _shot)
from tests.test_m4 import _install_video

WAN = "wan21.t2v.1_3b"
# wan.2.2.ti2v is declared i2v-capable but planned/comfyui — not
# installable via sdcpp; tests patch modes onto the tested sdcpp model.
WAN_I2V = "wan.2.2.ti2v"


def _install_tested_video(client, monkeypatch, key=WAN):
    """Fake-installed + support='tested' — the enqueue path is real,
    model bytes are fake so execution fails safely afterwards."""
    from app.domain import model_catalog
    spec = model_catalog.get(key)
    monkeypatch.setitem(model_catalog.MODELS, key,
                        dataclasses.replace(spec, support="tested"))
    _install_video(client, key)


def _setup_shot(client, tmp_path, *, prompt="lantern drifts", source=False):
    p = make_project(client)
    pid = p["id"]
    scene = _scene(client, pid)
    kw = {"prompt": prompt} if prompt else {}
    if source:
        src = _import(client, pid, "src.png", tmp_path, (10, 20, 30))
        kw["source_asset_id"] = src["id"]
    sh = _shot(client, pid, scene["scene"]["id"], **kw)
    return pid, scene, sh


def test_shot_video_compose_requires_prompt_or_source(client, tmp_path):
    pid, scene, sh = _setup_shot(client, tmp_path, prompt="")
    _preview(client, pid, "shot.video",
             {"shot_id": sh["shot"]["id"], "model_key": WAN}, expect=422)


def test_shot_video_rejects_unknown_and_image_models(client, tmp_path):
    pid, scene, sh = _setup_shot(client, tmp_path)
    _preview(client, pid, "shot.video",
             {"shot_id": sh["shot"]["id"], "model_key": "nope"},
             expect=422)
    _preview(client, pid, "shot.video",
             {"shot_id": sh["shot"]["id"], "model_key": "sd15.fp16"},
             expect=422)


def test_shot_video_rejects_uninstalled_model(client, tmp_path):
    pid, scene, sh = _setup_shot(client, tmp_path)
    r = _commit(client, pid, "shot.video",
                {"shot_id": sh["shot"]["id"], "model_key": WAN},
                expect=422)
    assert "installed" in str(r.json()) or "not usable" in str(r.json())


def test_shot_video_frames_bounds(client, tmp_path, monkeypatch):
    pid, scene, sh = _setup_shot(client, tmp_path)
    _install_tested_video(client, monkeypatch)
    _commit(client, pid, "shot.video",
            {"shot_id": sh["shot"]["id"], "model_key": WAN,
             "frames": 999}, expect=422)


def test_shot_video_archived_and_isolation(client, tmp_path):
    pid, scene, sh = _setup_shot(client, tmp_path)
    p2 = make_project(client)
    _preview(client, p2["id"], "shot.video",
             {"shot_id": sh["shot"]["id"], "model_key": WAN}, expect=404)
    _commit(client, pid, "shot.archive",
            {"shot_id": sh["shot"]["id"]})
    _preview(client, pid, "shot.video",
             {"shot_id": sh["shot"]["id"], "model_key": WAN}, expect=422)


def test_shot_video_composes_request(client, tmp_path, monkeypatch):
    """Commit lands a durable gen.video.sdcpp job whose request IS the
    composed generate.video payload + shot provenance."""
    pid, scene, sh = _setup_shot(client, tmp_path, source=True)
    # WAN is t2v-only — declare i2v on the installed spec so the source
    # path composes (capability gate is separately tested closed below).
    from app.domain import model_catalog
    spec = model_catalog.get(WAN)
    monkeypatch.setitem(model_catalog.MODELS, WAN, dataclasses.replace(
        spec, support="tested", modes=tuple(spec.modes) + ("image2video",)))
    _install_video(client, WAN)
    src_id = next(x for s in _board(client, pid) for x in s["shots"]
                  if x["shot"]["id"] == sh["shot"]["id"])["shot"]["source_asset_id"]
    r = _commit(client, pid, "shot.video",
                {"shot_id": sh["shot"]["id"], "model_key": WAN,
                 "motion_prompt": "slow push in", "frames": 33, "fps": 24})
    job_id = r.get("job_id") or r.get("job", {}).get("id")
    assert job_id
    req = _job_req(client, job_id)
    assert req["origin_command_type"] == "shot.video"
    assert req["model_key"] == WAN
    assert "lantern drifts" in req["prompt"]
    assert "slow push in" in req["prompt"]
    assert req["image_asset_id"] == src_id
    assert req["shot_id"] == sh["shot"]["id"]
    assert req["scene_id"] == scene["scene"]["id"]
    assert req["shot_video"]["shot_id"] == sh["shot"]["id"]
    assert req["shot_video"]["model_key"] == WAN
    assert req["frames"] == 33
    assert req["allow_cpu"] is False
    # Durable identity: the job row itself carries the link.
    job = client.get(f"/api/projects/{pid}/jobs").json()
    mine = next(j for j in job if j["id"] == job_id)
    assert mine["kind"] == "video"
    assert mine["adapter_id"] == "gen.video.sdcpp"
    assert mine["resumable"] is False


def test_shot_video_continuity_merges_into_prompt(client, tmp_path, monkeypatch):
    pid, scene, sh = _setup_shot(client, tmp_path)
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"],
             "continuity": {"lighting": "overcast noon",
                            "wardrobe": "red coat"}})
    _install_tested_video(client, monkeypatch)
    r = _commit(client, pid, "shot.video",
                {"shot_id": sh["shot"]["id"], "model_key": WAN})
    req = _job_req(client, r["job_id"])
    assert "overcast noon" in req["prompt"]
    assert "red coat" in req["prompt"]
    assert "continuity guidance" in req["prompt"]


def test_shot_video_stale_approval_rejected(client, tmp_path, monkeypatch):
    pid, scene, sh = _setup_shot(client, tmp_path)
    _install_tested_video(client, monkeypatch)
    prev = _preview(client, pid, "shot.video",
                    {"shot_id": sh["shot"]["id"], "model_key": WAN})
    assert prev["resolved_shot"]["shot_id"] == sh["shot"]["id"]
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "prompt": "different shot"})
    r = _commit(client, pid, "shot.video",
                {"shot_id": sh["shot"]["id"], "model_key": WAN,
                 "expected_shot": prev["resolved_shot"]},
                expect=422)
    assert "shot changed since preview" in str(r.json())


def test_shot_video_failed_job_on_board(client, tmp_path):
    """A failed video job surfaces on the storyboard as failed, with a
    video kind badge — and no clip association appears."""
    from app.db.models import GenerationJob
    pid, scene, sh = _setup_shot(client, tmp_path)
    with client.app.state.session_factory() as s:
        s.add(GenerationJob(
            id="deadbeef-0000-4000-8000-0000000000f3",
            project_id=pid, adapter_id="gen.video.sdcpp",
            kind="video", status="failed",
            error="worker exited",
            idempotency_key="c4d-fake-failed-video",
            request_json={"shot_video": {"shot_id": sh["shot"]["id"]},
                          "shot_id": sh["shot"]["id"]}))
        s.commit()
    gen = next(x for s in _board(client, pid) for x in s["shots"]
               if x["shot"]["id"] == sh["shot"]["id"])["generation"]
    assert gen["latest_job_status"] == "failed"
    assert gen["latest_job_kind"] == "video"
    assert gen["video_count"] == 0
    assert gen["latest_video_id"] is None


def test_shot_video_clips_not_confused_with_images(client, tmp_path):
    """Video and image outputs stay distinct on the board."""
    from sqlalchemy import select
    from app.db.models import Asset, AssetVersion, new_id, utcnow
    pid, scene, sh = _setup_shot(client, tmp_path)
    with client.app.state.session_factory() as s:
        from app.db.models import AssetFile, StorageLocation
        loc = s.execute(select(StorageLocation).where(
            StorageLocation.is_default.is_(True))).scalar_one_or_none()
        s.add(AssetFile(sha256="f" * 64,
                        storage_location_id=loc.id if loc else None,
                        rel_path="blobs/ff/fake", byte_size=100,
                        status="ready"))
        s.flush()
        a = Asset(id=new_id(), project_id=pid, name="clip.webm",
                  kind="video", origin="generated", uri="blobs/x/y",
                  sha256="f" * 64, byte_size=100,
                  provenance_json={"request": {
                      "shot_video": {"shot_id": sh["shot"]["id"]},
                      "shot_id": sh["shot"]["id"]}})
        s.add(a)
        s.flush()
        v = AssetVersion(id=new_id(), asset_id=a.id, version_no=1,
                         sha256="f" * 64,
                         media_json={"probe": {"duration_s": 4.0}})
        s.add(v)
        s.flush()
        a.current_version_id = v.id
        s.commit()
    gen = next(x for s in _board(client, pid) for x in s["shots"]
               if x["shot"]["id"] == sh["shot"]["id"])["generation"]
    assert gen["video_count"] == 1
    assert gen["latest_video_id"] == a.id
    assert gen["latest_video_duration_s"] == 4.0
    # Not counted as an image output.
    assert gen["output_count"] == 0
    assert gen["latest_asset_id"] is None


def test_shot_video_i2v_gate_refuses_t2v_model(client, tmp_path, monkeypatch):
    """Source-bound shot + t2v-only model → refused at validation —
    the model's declared modes gate i2v, never assumed."""
    pid, scene, sh = _setup_shot(client, tmp_path, source=True)
    _install_tested_video(client, monkeypatch)
    r = _commit(client, pid, "shot.video",
                {"shot_id": sh["shot"]["id"], "model_key": WAN},
                expect=422)
    assert "image-to-video" in str(r.json())


def _import_sized(client, pid, name, tmp_path, size):
    img = Image.new("RGB", size, (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    p = tmp_path / name
    p.write_bytes(buf.getvalue())
    r = _commit(client, pid, "asset.import",
                {"source_path": str(p), "name": name, "kind": "image"})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded", job
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    return next(a for a in items if a["name"] == name)


def test_shot_video_param_validation(client, tmp_path, monkeypatch):
    """Review — the declared contract beyond frames is enforced:
    dims must be a catalog preset, fps/steps/cfg/sampler bounded."""
    pid, scene, sh = _setup_shot(client, tmp_path)
    _install_tested_video(client, monkeypatch)
    sid = sh["shot"]["id"]
    # fps must be a declared option (16|24 for wan21).
    _preview(client, pid, "shot.video",
             {"shot_id": sid, "model_key": WAN, "fps": 30}, expect=422)
    # Explicit dims must match a catalog preset — no silent stretch.
    _preview(client, pid, "shot.video",
             {"shot_id": sid, "model_key": WAN,
              "width": 640, "height": 360}, expect=422)
    # Half-specified dims refuse.
    _preview(client, pid, "shot.video",
             {"shot_id": sid, "model_key": WAN, "width": 832}, expect=422)
    # steps/cfg/sampler bounded by the catalog entry.
    _preview(client, pid, "shot.video",
             {"shot_id": sid, "model_key": WAN, "steps": 999}, expect=422)
    _preview(client, pid, "shot.video",
             {"shot_id": sid, "model_key": WAN, "cfg": 99}, expect=422)
    _preview(client, pid, "shot.video",
             {"shot_id": sid, "model_key": WAN,
              "sampler": "dpm++"}, expect=422)
    # A declared preset + option fps passes validation.
    r = _preview(client, pid, "shot.video",
                 {"shot_id": sid, "model_key": WAN,
                  "width": 832, "height": 480, "fps": 24,
                  "steps": 30, "cfg": 6.0, "sampler": "euler"})
    assert r["resolved_params"]["width"] == 832


def test_shot_video_expected_params_rejected(client, tmp_path, monkeypatch):
    """Approving a preview and committing different params refuses."""
    pid, scene, sh = _setup_shot(client, tmp_path)
    _install_tested_video(client, monkeypatch)
    sid = sh["shot"]["id"]
    prev = _preview(client, pid, "shot.video",
                    {"shot_id": sid, "model_key": WAN, "frames": 33})
    assert prev["resolved_params"]["frames"] == 33
    r = _commit(client, pid, "shot.video",
                {"shot_id": sid, "model_key": WAN, "frames": 64,
                 "expected_params": prev["resolved_params"]},
                expect=422)
    assert "parameters changed" in str(r.json())
    # The exact pinned payload commits fine.
    r = _commit(client, pid, "shot.video",
                {"shot_id": sid, "model_key": WAN, "frames": 33,
                 "expected_params": prev["resolved_params"]})
    assert r.get("job_id")


def test_shot_video_i2v_only_model_requires_source(client, tmp_path,
                                                   monkeypatch):
    """A model with no text2video mode refuses prompt-only shots."""
    pid, scene, sh = _setup_shot(client, tmp_path)
    from app.domain import model_catalog
    spec = model_catalog.get(WAN)
    monkeypatch.setitem(model_catalog.MODELS, WAN, dataclasses.replace(
        spec, support="tested", modes=("image2video",)))
    _install_video(client, WAN)
    r = _commit(client, pid, "shot.video",
                {"shot_id": sh["shot"]["id"], "model_key": WAN},
                expect=422)
    assert "text-to-video" in str(r.json())


def test_shot_video_i2v_size_follows_source_aspect(client, tmp_path,
                                                   monkeypatch):
    """i2v with no explicit dims picks the preset nearest the source
    aspect (portrait source → 480×832), never a silent 832×480."""
    pid, scene, sh = _setup_shot(client, tmp_path)
    src = _import_sized(client, pid, "portrait.png", tmp_path, (512, 1024))
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "source_asset_id": src["id"]})
    from app.domain import model_catalog
    spec = model_catalog.get(WAN)
    monkeypatch.setitem(model_catalog.MODELS, WAN, dataclasses.replace(
        spec, support="tested", modes=tuple(spec.modes) + ("image2video",)))
    _install_video(client, WAN)
    r = _commit(client, pid, "shot.video",
                {"shot_id": sh["shot"]["id"], "model_key": WAN})
    req = _job_req(client, r["job_id"])
    assert req["width"] == 480 and req["height"] == 832
    assert req["source_sha256"] == src["sha256"]


def test_shot_video_cancelled_job_creates_no_clip(client, tmp_path):
    """A cancelled video job never produces a clip association."""
    from app.db.models import GenerationJob
    pid, scene, sh = _setup_shot(client, tmp_path)
    with client.app.state.session_factory() as s:
        s.add(GenerationJob(
            id="deadbeef-0000-4000-8000-0000000000f4",
            project_id=pid, adapter_id="gen.video.sdcpp",
            kind="video", status="cancelled",
            idempotency_key="c4d-fake-cancelled-video",
            request_json={"shot_video": {"shot_id": sh["shot"]["id"]},
                          "shot_id": sh["shot"]["id"]}))
        s.commit()
    gen = next(x for s in _board(client, pid) for x in s["shots"]
               if x["shot"]["id"] == sh["shot"]["id"])["generation"]
    assert gen["latest_job_status"] == "cancelled"
    assert gen["video_count"] == 0
    assert gen["latest_video_id"] is None


def test_shot_video_multiple_clips_discoverable(client, tmp_path):
    """Two successful clips: both counted, latest pick is deterministic
    (created_at then id tiebreak)."""
    from sqlalchemy import select
    from app.db.models import (Asset, AssetFile, AssetVersion,
                               StorageLocation, new_id)
    pid, scene, sh = _setup_shot(client, tmp_path)

    def _clip(s, loc, sha, created_at):
        s.add(AssetFile(sha256=sha, storage_location_id=loc.id,
                        rel_path=f"blobs/{sha[:2]}/x", byte_size=100,
                        status="ready"))
        s.flush()
        a = Asset(id=new_id(), project_id=pid, name=f"c{sha[:4]}.webm",
                  kind="video", origin="generated", uri="blobs/x/y",
                  sha256=sha, byte_size=100, created_at=created_at,
                  provenance_json={"request": {
                      "shot_video": {"shot_id": sh["shot"]["id"]},
                      "shot_id": sh["shot"]["id"]}})
        s.add(a)
        s.flush()
        v = AssetVersion(id=new_id(), asset_id=a.id, version_no=1,
                         sha256=sha,
                         media_json={"probe": {"duration_s": 2.0}})
        s.add(v)
        s.flush()
        a.current_version_id = v.id
        return a

    with client.app.state.session_factory() as s:
        loc = s.execute(select(StorageLocation).where(
            StorageLocation.is_default.is_(True))).scalar_one()
        # Same created_at → id tiebreak must pick the higher id.
        from datetime import datetime, timezone
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        older = _clip(s, loc, "a" * 64, ts)
        newer = _clip(s, loc, "b" * 64, ts)
        s.commit()
        expected_latest = max(older.id, newer.id)
    gen = next(x for s in _board(client, pid) for x in s["shots"]
               if x["shot"]["id"] == sh["shot"]["id"])["generation"]
    assert gen["video_count"] == 2
    assert gen["latest_video_id"] == expected_latest
