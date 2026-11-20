"""G2-A — explicit local execution_target routing for shot.generate and
keyframe.generate onto the real sd.cpp adapters.

Covers: backward-compatible defaults (mock / cloud-sim), target
validation, local adapter resolution, missing-model/runtime refusals,
the unsupported local img2img refusal, strict-cast and stale-pin
protection, fail-closed no-silent-fallback dispatch, CAS result linkage
via the apply_command contract, and honest mock-vs-local labeling.

All inference is the fake-worker subprocess (ATOMIC_SDCPP_CMD) or the
existing stubs — no real model ever runs.
"""
from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import (
    _commit, _preview, _job_req, _setup, _shot, GEN)
from tests.test_a3_keyframes import _kfs, _mk_kf
from tests.test_a2_storyboard import _scene2, _shot2
from tests.test_m3 import _install_row, _fake_cmd
from app.adapters.inference import _result_apply_command


# ------------------------------------------------- keyframe.generate

def _gen_kf(client, pid, shot_id, expect=200, **payload):
    return _commit(client, pid, "keyframe.generate",
                   {"keyframe_id": _mk_kf(
                       client, pid, shot_id, "start",
                       prompt="a red door")["id"], **payload},
                   expect=expect)


def test_keyframe_default_is_mock(client):
    """No execution_target → unchanged mock path."""
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    out = _gen_kf(client, pid, sh["shot"]["id"])
    job = wait_for_job(client, pid, out["job_id"])
    assert job["status"] == "succeeded"
    assert job["adapter_id"] == "mock.image"


def test_keyframe_invalid_target_refused(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    r = _gen_kf(client, pid, sh["shot"]["id"], expect=422,
                execution_target="banana")
    assert "execution_target" in r.json()["detail"]


def test_keyframe_local_requires_model(client):
    """local without a model_key/keyframe model setting refuses —
    it can never silently degrade to mock."""
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    r = _gen_kf(client, pid, sh["shot"]["id"], expect=422,
                execution_target="local")
    assert "model" in r.json()["detail"].lower()


def test_keyframe_local_not_installed_refused(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    r = _gen_kf(client, pid, sh["shot"]["id"], expect=422,
                execution_target="local", model_key="sd15.fp16",
                local_adapter="sdcpp")
    assert "not installed" in r.json()["detail"]


def test_keyframe_local_invalid_adapter_refused(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    r = _gen_kf(client, pid, sh["shot"]["id"], expect=422,
                execution_target="local", model_key="sd15.fp16",
                local_adapter="bogus")
    assert "local_adapter" in r.json()["detail"]


def test_keyframe_local_mock_hold_refused(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    r = _gen_kf(client, pid, sh["shot"]["id"], expect=422,
                execution_target="local", model_key="sd15.fp16",
                local_adapter="sdcpp", mock_hold=2)
    assert "mock_hold" in r.json()["detail"]


def test_keyframe_local_stale_prompt_pin_refused(client, tmp_path):
    _install_row(client, "sd15.fp16", tmp_path)
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    r = _gen_kf(client, pid, sh["shot"]["id"], expect=422,
                execution_target="local", model_key="sd15.fp16",
                local_adapter="sdcpp", expected_prompt="deadbeef")
    assert "prompt" in r.json()["detail"].lower()


def test_keyframe_local_subprocess_endtoend(client, tmp_path, monkeypatch):
    """The REAL gen.image.sdcpp adapter (fake worker binary) lands the
    CAS asset AND applies keyframe.result with the stale pins — the
    same apply_command contract the mock uses."""
    _fake_cmd(monkeypatch, tmp_path)
    _install_row(client, "sd15.fp16", tmp_path)
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    kf_id = _mk_kf(client, pid, sh["shot"]["id"], "start",
                   prompt="a red door")["id"]
    out = _commit(client, pid, "keyframe.generate", {
        "keyframe_id": kf_id, "execution_target": "local",
        "local_adapter": "sdcpp", "model_key": "sd15.fp16",
        "device": "cpu"})
    job = wait_for_job(client, pid, out["job_id"])
    assert job["status"] == "succeeded", job
    assert job["adapter_id"] == "gen.image.sdcpp"
    # CAS linkage: the pinned result lands on the SAME keyframe
    kf = next(k for k in _kfs(client, pid, sh["shot"]["id"])
              if k["id"] == kf_id)
    assert kf["result_asset_id"] == job["output_asset_id"]
    # provenance honesty — real local, never labeled mock
    from app.db.models import Asset, AssetVersion
    with client.app.state.session_factory() as s:
        a = s.get(Asset, job["output_asset_id"])
        prov = a.provenance_json or {}
        assert prov.get("request", {}).get("execution_target") == "local"
        assert prov.get("request", {}).get("origin_command_type") \
            == "keyframe.generate"
        ver = s.get(AssetVersion, a.current_version_id)
        assert (ver.media_json or {}).get("is_mock") is False
    # request carried the resolved adapter + result pins
    req = _job_req(client, job["id"])
    assert req["local_adapter_id"] == "gen.image.sdcpp"
    assert "expected_result_revision" in req


def test_keyframe_local_persistent_default_adapter(client, tmp_path):
    """execution_target='local' defaults to the persistent worker and
    stamps it into the request; with the adapter unregistered in this
    build the commit refuses cleanly — never a mock fallback."""
    _install_row(client, "sd15.fp16", tmp_path)
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    r = _gen_kf(client, pid, sh["shot"]["id"], expect=422,
                execution_target="local", model_key="sd15.fp16")
    assert "not available" in r.json()["detail"] \
        or "persistent" in r.json()["detail"].lower()


# ------------------------------------------------- shot.generate

def test_shot_default_is_cloud_sim(client, tmp_path):
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="a wide alley")
    out = _commit(client, pid, "shot.generate",
                  {**GEN, "shot_id": sh["shot"]["id"]})
    job = wait_for_job(client, pid, out["job_id"])
    assert job["adapter_id"] == "cloud.image.sim"


def test_shot_invalid_target_refused(client, tmp_path):
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x")
    for bad in ("mock", "banana"):
        r = _commit(client, pid, "shot.generate",
                    {"shot_id": sh["shot"]["id"], "execution_target": bad},
                    expect=422)
        assert "execution_target" in r.json()["detail"]


def test_shot_local_source_asset_refused(client, tmp_path):
    """Bound source image + local → clean refusal: local img2img is
    unverified, never silently simulated."""
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x",
               source_asset_id=src["id"])
    r = _commit(client, pid, "shot.generate", {
        "shot_id": sh["shot"]["id"], "execution_target": "local",
        "local_adapter": "sdcpp", "model_key": "sd15.fp16"}, expect=422)
    assert "image-to-image" in r.json()["detail"].lower() \
        or "source" in r.json()["detail"].lower()


def test_shot_local_subprocess_endtoend(client, tmp_path, monkeypatch):
    _fake_cmd(monkeypatch, tmp_path)
    _install_row(client, "sd15.fp16", tmp_path)
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="a wide alley")
    out = _commit(client, pid, "shot.generate", {
        "shot_id": sh["shot"]["id"], "execution_target": "local",
        "local_adapter": "sdcpp", "model_key": "sd15.fp16",
        "device": "cpu"})
    job = wait_for_job(client, pid, out["job_id"])
    assert job["status"] == "succeeded", job
    assert job["adapter_id"] == "gen.image.sdcpp"
    req = _job_req(client, job["id"])
    assert req["execution_target"] == "local"
    assert req["local_adapter_id"] == "gen.image.sdcpp"
    assert req["origin_command_type"] == "shot.generate"
    assert req["shot_generate"]["shot_id"] == sh["shot"]["id"]


def test_shot_local_persistent_default_refuses_cleanly(client, tmp_path):
    _install_row(client, "sd15.fp16", tmp_path)
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x")
    r = _commit(client, pid, "shot.generate", {
        "shot_id": sh["shot"]["id"], "execution_target": "local",
        "model_key": "sd15.fp16"}, expect=422)
    assert "not available" in r.json()["detail"]


def test_shot_local_strict_cast_still_enforced(client, tmp_path):
    """A ref-set bound to a character not in this shot's cast refuses on
    the local path exactly like the simulated one."""
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x")
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "reference_set_id": sid})
    r = _commit(client, pid, "shot.generate", {
        "shot_id": sh["shot"]["id"], "execution_target": "local",
        "local_adapter": "sdcpp", "model_key": "sd15.fp16"}, expect=422)
    assert "cast" in r.json()["detail"].lower()


# ------------------------------------------------- apply_command unit

def test_result_apply_command_keyframe_precedence():
    req = {"keyframe_id": "k1", "shot_id": "s1", "set_thumbnail": True,
           "expected_result_revision": 7, "expected_planning_sha": "abc"}
    ac = _result_apply_command(req)
    assert ac["command_type"] == "keyframe.result"
    assert ac["payload"]["keyframe_id"] == "k1"
    assert ac["payload"]["expected_keyframe_revision"] == 7
    assert ac["payload"]["expected_planning_sha"] == "abc"


def test_result_apply_command_thumbnail_only():
    ac = _result_apply_command({"shot_id": "s1", "set_thumbnail": True})
    assert ac["command_type"] == "shot.update"
    assert _result_apply_command({"shot_id": "s1"}) is None
    assert _result_apply_command({}) is None


def test_preview_shows_local_target_honesty(client, tmp_path):
    """Preview surfaces the real-inference warning + resolved adapter."""
    _install_row(client, "sd15.fp16", tmp_path)
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start", prompt="x")
    prop = _preview(client, pid, "keyframe.generate", {
        "keyframe_id": kf["id"], "execution_target": "local",
        "local_adapter": "sdcpp", "model_key": "sd15.fp16"})
    assert any("REAL local inference" in w for w in prop["warnings"])
