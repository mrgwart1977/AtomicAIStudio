"""C4-E — resource preflight, admission thresholds, disk checks, and
the unified status endpoint. Reuses gpu_sched, resources, and the
durable job engine — no parallel scheduler is created."""
import dataclasses

import pytest

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit, _preview
from tests.test_m4 import _install_video

_GIB = 1024 ** 3
WAN = "wan21.t2v.1_3b"


def _install_tested_video(client, monkeypatch, key=WAN):
    """Fake-installed + support='tested' — the enqueue path is real,
    model bytes are fake so execution fails safely afterwards."""
    from app.domain import model_catalog
    spec = model_catalog.get(key)
    monkeypatch.setitem(model_catalog.MODELS, key,
                        dataclasses.replace(spec, support="tested"))
    _install_video(client, key)


def test_safety_margins_override_merge():
    from app.domain import resources
    m = resources.safety_margins(
        {"os_headroom_bytes": 8 * _GIB, "min_disk_free_bytes": 1 * _GIB})
    assert m["os_headroom_bytes"] == 8 * _GIB
    assert m["min_disk_free_bytes"] == 1 * _GIB
    # Unset keys keep defaults; unknown keys are ignored.
    assert m["critical_commit_headroom_bytes"] > 0
    assert "bogus" not in m


def test_disk_preflight_decisions():
    from app.domain import resources
    r = resources.preflight_disk_check(
        2 * _GIB, 1 * _GIB, label="work_dir",
        margins={"min_disk_free_bytes": 5 * _GIB})
    assert r["decision"] == "block"
    assert "insufficient disk" in r["reason"]
    r = resources.preflight_disk_check(
        0, None, label="work_dir")
    assert r["decision"] == "warn"  # unprobeable, never silent
    r = resources.preflight_disk_check(
        2 * _GIB, 100 * _GIB, label="work_dir")
    assert r["decision"] == "ok"


def test_gpu_scheduler_vram_headroom():
    """A configured headroom shrinks usable VRAM before admission."""
    from app.domain.gpu_sched import GpuScheduler
    hw = {"gpus": [{"index": 0, "name": "g0",
                    "vram_bytes": 10 * _GIB,
                    "vram_free_bytes": 10 * _GIB}]}
    s = GpuScheduler(hw_probe=hw,
                     prefs={"vram_headroom_bytes": 9 * _GIB})
    # 10 free - 9 headroom = 1 GiB usable < 4 GiB estimate → refuse.
    assert s.acquire("j1", {"device": "auto"},
                     est_vram=4 * _GIB) is None
    assert "no device fits" in s.last_reason
    s2 = GpuScheduler(hw_probe=hw, prefs={})
    assert s2.acquire("j1", {"device": "auto"},
                      est_vram=4 * _GIB) == 0


def test_gpu_scheduler_manual_pin_honors_headroom():
    """A manual device pin is not an escape hatch: a known estimate
    that can't fit the pinned GPU stays queued with a clear reason."""
    from app.domain.gpu_sched import GpuScheduler
    hw = {"gpus": [{"index": 0, "name": "g0",
                    "vram_bytes": 10 * _GIB,
                    "vram_free_bytes": 10 * _GIB},
                   {"index": 1, "name": "g1",
                    "vram_bytes": 24 * _GIB,
                    "vram_free_bytes": 24 * _GIB}]}
    s = GpuScheduler(hw_probe=hw,
                     prefs={"vram_headroom_bytes": 8 * _GIB})
    # GPU 0: 10-8=2 GiB usable < 4 GiB est → queued, no silent move.
    assert s.acquire("j1", {"device": 0},
                     est_vram=4 * _GIB) is None
    assert "cannot fit" in s.last_reason
    # GPU 1 fits fine on the same request.
    assert s.acquire("j1", {"device": 1},
                     est_vram=4 * _GIB) == 1


def test_resource_settings_roundtrip(client):
    make_project(client)
    r = client.put("/api/settings/resources",
                   json={"os_headroom_gib": 6.0,
                         "min_disk_free_gib": 2.0,
                         "vram_headroom_gib": 1.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["os_headroom_gib"] == 6.0
    assert body["min_disk_free_gib"] == 2.0
    assert body["vram_headroom_gib"] == 1.0
    st = client.get("/api/system/resources").json()
    assert st["margins"]["effective"]["os_headroom_bytes"] == 6 * _GIB
    assert st["margins"]["effective"]["min_disk_free_bytes"] == 2 * _GIB
    assert st["margins"]["user_overrides"]["os_headroom_bytes"] == 6 * _GIB
    # Negative values refuse.
    assert client.put("/api/settings/resources",
                      json={"os_headroom_gib": -1}).status_code == 422
    # Nonfinite values refuse — raw bodies since httpx won't emit NaN.
    r = client.put("/api/settings/resources",
                   content=b'{"os_headroom_gib": NaN}',
                   headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    r = client.put("/api/settings/resources",
                   content=b'{"vram_headroom_gib": Infinity}',
                   headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    # Absurd magnitude refuses.
    assert client.put("/api/settings/resources",
                      json={"min_disk_free_gib": 99999}
                      ).status_code == 422
    # Critical floors cannot be weakened below the built-in value.
    r = client.put("/api/settings/resources",
                   json={"critical_commit_gib": 0.1})
    assert r.status_code == 422
    assert "floor" in r.text.lower()


def test_system_resources_shape(client):
    p = make_project(client)
    st = client.get("/api/system/resources").json()
    assert "total_phys_bytes" in st["memory"]
    assert st["memory"]["pressure"] in ("ok", "warn", "critical")
    assert "slots" in st["gpus"] and "gpu_count" in st["gpus"]
    assert st["disk"]["work_dir_free_bytes"] is not None
    assert {"queued", "running"} <= set(st["jobs"])
    assert "effective" in st["margins"]


def test_disk_preflight_blocks_video_job(client, tmp_path, monkeypatch):
    """A gen job whose model footprint can't fit fails honestly at
    admission — never starts the worker."""
    from app.domain import resources
    pid = make_project(client)["id"]
    _install_tested_video(client, monkeypatch)
    monkeypatch.setattr(resources, "disk_free_bytes",
                        lambda p: 1)   # one byte free — can't fit 8 GiB
    r = _commit(client, pid, "generate.video",
                {"model_key": WAN, "prompt": "lantern drifts"})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "failed"
    assert "disk" in job["error"].lower()
    assert "preflight" in job["error"].lower()


def test_interrupted_nonresumable_purges_workfiles(client, tmp_path):
    """A crash leaves partial files; reconcile fails the job AND purges
    its work-dir artifacts. A resumable job's files are kept."""
    from app.db.models import GenerationJob
    pid = make_project(client)["id"]
    eng = client.app.state.job_engine
    wdir = client.app.state.settings.data_dir / "jobs" / pid
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "jobNRAA_partial.bin").write_bytes(b"partial")
    (wdir / "jobRSBB_partial.bin").write_bytes(b"partial")
    with client.app.state.session_factory() as s:
        s.add(GenerationJob(
            id="jobNRAA", project_id=pid, adapter_id="gen.video.sdcpp",
            kind="video", status="running", resumable=False,
            idempotency_key="c4e-nr", request_json={}))
        s.add(GenerationJob(
            id="jobRSBB", project_id=pid, adapter_id="gen.img.sdcpp",
            kind="image", status="running", resumable=True,
            idempotency_key="c4e-rs", request_json={}))
        s.commit()
    with client.app.state.session_factory() as s:
        n = eng.reconcile_interrupted(s)
        s.commit()
    assert n == 1                      # only the resumable job requeued
    assert not (wdir / "jobNRAA_partial.bin").exists()
    assert (wdir / "jobRSBB_partial.bin").exists()
    with client.app.state.session_factory() as s:
        assert s.get(GenerationJob, "jobNRAA").status == "failed"
        assert s.get(GenerationJob, "jobRSBB").status == "queued"


def test_queued_wait_reason_visible_in_status(client, tmp_path,
                                              monkeypatch):
    """A GPU-gated job's wait reason lands on the job row AND the
    system status view lists it among queued jobs."""
    from app.domain import resources
    pid = make_project(client)["id"]
    _install_tested_video(client, monkeypatch)
    # Force GPU admission failure: no GPUs + no CPU path → stays queued.
    monkeypatch.setattr(
        client.app.state.gpu_scheduler, "gpus",
        lambda: [])
    monkeypatch.setattr(
        client.app.state.gpu_scheduler, "gpu_count",
        lambda: 0)
    _commit(client, pid, "generate.video",
            {"model_key": WAN, "prompt": "lantern drifts"})
    import time
    time.sleep(0.6)   # let the claim pass record the reason
    st = client.get("/api/system/resources").json()
    queued = st["jobs"]["queued"]
    assert any("video" in j["kind"] for j in queued)
    vj = next(j for j in queued if j["kind"] == "video")
    assert vj["message"] and ("gpu" in vj["message"].lower()
                             or "waiting" in vj["message"].lower())
