"""B-3e-1 — curated LoRA catalog slice: two metadata-verified experimental
adapters (one identity, one style). Catalog data only — tests never
download, stage, or infer. `experimental` is the honest tier: installable
through the existing route, refused for generation by check_loras until
real LoRA inference is validated."""
import re
import time

import pytest

from tests.conftest import make_project
from tests.test_c4b_shot_generate import (
    _commit, _preview, _job_req, _setup, _shot, GEN)
from tests.test_a3_keyframes import _mk_kf
from tests.test_m3 import _install_row
from tests.test_g2b1_lora import _install_lora, _lora_bytes
from app.domain import model_catalog, model_files

LORA_KEYS = ("lora.id.babylittlelo", "lora.style.pixelart")

_EXPECTED = {
    "lora.id.babylittlelo": {
        "role": "identity",
        "license": "CC0-1.0",
        "file": "BabyLittleLo-SD1.5.safetensors",
        "size": 151_124_544,
        "sha": "15d98a7828936dda787c4e4d2d5ab10f2306a2cf10ab8c75a08a76e844709cbe",
        "revision": "0bad08ebf49d1d31befb8e1c568b00b9650335f8",
        "repo": "AiAF/BabyLittleLo_LoAR_SD1.5",
    },
    "lora.style.pixelart": {
        "role": "style",
        "license": "CreativeML OpenRAIL-M",
        "file": "pytorch_lora_weights.safetensors",
        "size": 3_226_184,
        "sha": "ad5034703699e910d5f9525ea5db64abcbd8d7396ff8f771c09403f3adb048ad",
        "revision": "54ed39696bebc825817a6d9dc630dd4c50edc813",
        "repo": "SedatAl/pixel-art-LoRa",
    },
}

_PINNED = re.compile(
    r"^https://huggingface\.co/[^/]+/[^/]+/resolve/[0-9a-f]{40}/[^/]+$")


# -------------------------------------------------------------- catalog shape

def test_exactly_two_lora_entries():
    loras = {k: s for k, s in model_catalog.MODELS.items()
             if s.modality == "lora"}
    assert set(loras) == set(LORA_KEYS)


@pytest.mark.parametrize("key", LORA_KEYS)
def test_lora_entry_shape(key):
    spec = model_catalog.get(key)
    exp = _EXPECTED[key]
    assert spec is not None
    assert spec.modality == "lora"
    assert spec.family == "sd1.5"
    assert spec.fmt == "safetensors"
    assert spec.support == "experimental"      # never tested/production — unvalidated
    assert spec.adapter_id == "gen.image.sdcpp"
    assert spec.runtime_key == "sdcpp"
    assert spec.modes == ("text2image",)
    assert spec.capabilities["lora_role"] == exp["role"]
    assert spec.license["commercial_ok"] is True
    assert spec.license["name"] == exp["license"]
    assert spec.license["url"].startswith("https://")
    assert spec.notes  # runtime-limitation notes required


@pytest.mark.parametrize("key", LORA_KEYS)
def test_lora_artifact_pin(key):
    spec = model_catalog.get(key)
    exp = _EXPECTED[key]
    assert len(spec.files) == 1
    f = spec.files[0]
    assert f.name == exp["file"]
    assert f.size_bytes == exp["size"]
    assert f.sha256 == exp["sha"] and len(f.sha256) == 64
    assert _PINNED.match(f.url), f"URL not revision-pinned https: {f.url}"
    assert exp["revision"] in f.url
    assert spec.source["provider"] == "huggingface"
    assert spec.source["repo"] == exp["repo"]
    assert spec.source["revision"] == exp["revision"]


@pytest.mark.parametrize("key,expected_tokens", [
    ("lora.id.babylittlelo",
     {"babylittlelo \\(person\\)", "@bblittlelo", "bblittlelo \\(person\\)"}),
    ("lora.style.pixelart", {"pixel art"}),
])
def test_lora_trigger_tokens(key, expected_tokens):
    spec = model_catalog.get(key)
    assert set(spec.capabilities.get("trigger_tokens") or ()) == expected_tokens


def test_loras_visible_in_catalog_api(client):
    models = {m["key"]: m for m in client.get("/api/models").json()}
    for key, exp in _EXPECTED.items():
        assert key in models, f"{key} missing from catalog listing"
        m = models[key]
        assert m["modality"] == "lora"
        assert m["support"] == "experimental"
        assert m["capabilities"]["lora_role"] == exp["role"]
        assert m["install"]["status"] in {"not_installed", None}


# ------------------------------------------------------------- install route

def test_experimental_lora_installable_via_route(client, monkeypatch):
    """Experimental tier is installable: the route's only support gate is
    'planned'. Download + verify are faked — CI never touches the network."""
    monkeypatch.setattr(model_files, "verify_component",
                        lambda path, f, fmt: (True, "ok"))

    def fake_dl(dest, url, *, expected_size=None, progress=None,
                cancel_check=None, timeout=60):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"FAKE" * 1024)
        if progress:
            progress(dest.stat().st_size, expected_size or dest.stat().st_size)
        return {"bytes": dest.stat().st_size, "sha256": "0" * 64}
    monkeypatch.setattr(model_files, "download_to", fake_dl)

    # The download job also ensures the declared runtime — fake release
    # resolution + install exactly like test_m3's install-job coverage.
    from app.db.models import RuntimeInstall, new_id, utcnow

    def fake_resolve(spec, flavor, *, fetch=None):
        return {"url": "https://example.com/runtime.zip",
                "name": "runtime-win.zip", "size": 1024,
                "tag": "test-tag", "pinned": False,
                "resolved_from": "mock", "resolved_at": "2026-01-01T00:00:00"}
    monkeypatch.setattr(model_files, "resolve_release_asset", fake_resolve)

    def fake_rt_install(session, settings, spec, flavor, asset,
                        cancel_check=None):
        d = settings.data_dir / "runtimes" / f"{spec.key}-{flavor}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "sd-cli.exe").write_bytes(b"MZ")
        row = RuntimeInstall(
            id=new_id(), runtime_key=f"{spec.key}-{flavor}",
            version=spec.version, status="installed", install_dir=str(d),
            exe_path=str(d / "sd-cli.exe"), byte_size=2, verified=True,
            meta_json={}, created_at=utcnow(), updated_at=utcnow())
        session.add(row)
        return row
    monkeypatch.setattr(model_files, "install_runtime_zip", fake_rt_install)

    r = client.post("/api/models/lora.style.pixelart/install", json={})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    deadline = time.time() + 15
    job = None
    while time.time() < deadline:
        job = next((j for j in client.get("/api/jobs").json()
                    if j["id"] == job_id), None)
        if job and job["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.2)
    assert job is not None and job["status"] == "succeeded", \
        f"install job failed: {job}"
    models = {m["key"]: m for m in client.get("/api/models").json()}
    assert models["lora.style.pixelart"]["install"]["status"] == "installed"
    assert client.post("/api/models/lora.style.pixelart/install",
                       json={}).status_code == 409


# ------------------------------------------------- generation eligibility

def _kf_with_lora(client, pid, sh, key, expect=422):
    return _commit(client, pid, "keyframe.generate", {
        "keyframe_id": _mk_kf(client, pid, sh["shot"]["id"], "start",
                              prompt="a red door")["id"],
        "execution_target": "local", "local_adapter": "sdcpp",
        "model_key": "sd15.fp16", "loras": [{"key": key}]}, expect=expect)


@pytest.mark.parametrize("key", LORA_KEYS)
def test_experimental_lora_refused_at_generation(client, tmp_path, key):
    """Installed is not enough — check_loras refuses experimental adapters
    before any role/binding check, even with a real install row on disk."""
    spec = model_catalog.get(key)
    _install_row(client, "sd15.fp16", tmp_path)
    _install_lora(client, spec)  # real catalog spec → structurally valid file + row
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="a red door")
    r = _kf_with_lora(client, pid, sh, key)
    detail = r.json()["detail"]
    assert "experimental" in detail
    assert "catalog-visible" in detail


@pytest.mark.parametrize("key", LORA_KEYS)
def test_experimental_lora_refused_at_preview(client, tmp_path, key):
    """Same shared resolver on the preview path — identical refusal."""
    spec = model_catalog.get(key)
    _install_row(client, "sd15.fp16", tmp_path)
    _install_lora(client, spec)
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="a red door")
    p = _preview(client, pid, "keyframe.generate", {
        "keyframe_id": _mk_kf(client, pid, sh["shot"]["id"], "start",
                              prompt="a red door")["id"],
        "execution_target": "local", "local_adapter": "sdcpp",
        "model_key": "sd15.fp16", "loras": [{"key": key}]}, expect=422)
    assert "experimental" in p.json()["detail"]


# ------------------------------------------------ structural file verifier

def _spec_with_file(spec, path):
    """The real catalog spec with its file declaration swapped for the
    actual bytes on disk — exercises the genuine integrity path while the
    layout-hint check is what is under test."""
    import dataclasses
    import hashlib
    data = path.read_bytes()
    f = model_catalog.ModelFile(
        name=spec.files[0].name, size_bytes=len(data), url="",
        sha256=hashlib.sha256(data).hexdigest())
    return dataclasses.replace(spec, files=(f,))


def test_kohya_layout_passes_local_verifier(tmp_path):
    """BabyLittleLo's real layout (lora_*_…lora_down/lora_up + alpha) is
    recognized by verify_lora_file — file-level only, not an sd.cpp claim."""
    p = tmp_path / "kohya.safetensors"
    p.write_bytes(_lora_bytes((
        "lora_unet_down_blocks_0_attentions_0_to_k.lora_down.weight",
        "lora_unet_down_blocks_0_attentions_0_to_k.lora_up.weight",
        "lora_te_text_model_encoder_layers_0_mlp_fc1.alpha")))
    spec = _spec_with_file(model_catalog.get("lora.id.babylittlelo"), p)
    ok, reason = model_files.verify_lora_file(p, spec)
    assert ok, reason


def test_diffusers_dotted_layout_passes_local_verifier(tmp_path):
    """Pixel-art's real layout (unet.*.attn*.to_*.lora.down/lora.up dotted
    names) also passes verify_lora_file via the '.lora.' hint — still not
    an sd.cpp-compatibility claim."""
    p = tmp_path / "diffusers.safetensors"
    p.write_bytes(_lora_bytes((
        "unet.down_blocks.0.attentions.0.transformer_blocks.0.attn1.to_k.lora.down.weight",
        "unet.down_blocks.0.attentions.0.transformer_blocks.0.attn1.to_k.lora.up.weight")))
    spec = _spec_with_file(model_catalog.get("lora.style.pixelart"), p)
    ok, reason = model_files.verify_lora_file(p, spec)
    assert ok, reason
