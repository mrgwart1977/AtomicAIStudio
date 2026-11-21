"""B-3c — read-only LoRA resolution preview.

POST /api/projects/{id}/loras/preview runs the SAME B-1 gate
(app/domain/lora_resolve.check_loras) the generation paths enforce and
returns the resolution as data — accepted selections, explicit refusal
reasons, pins/hashes — without queuing a job, writing a row, starting a
subprocess, or staging/downloading anything.
"""
import hashlib

import pytest
from sqlalchemy import func, select

from tests.conftest import make_project
from tests.test_c4b_shot_generate import _commit, _setup, _shot
from tests.test_a2_storyboard import _scene2, _shot2
from tests.test_m3 import _install_row
from tests.test_g2b1_lora import (
    _add_lora, _bind_loras, _install_lora, _lora_spec)
from tests.test_a3_keyframes import _mk_kf


def _preview(client, pid, expect=200, **body):
    r = client.post(f"/api/projects/{pid}/loras/preview", json=body)
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


def _full_setup(client, tmp_path, monkeypatch, *, role="identity",
                key="lora.id"):
    """project + char + shot + installed sd15 + installed lora (bound
    via shot.cast like a real user flow)."""
    from tests.test_c4b_shot_generate import _setup as c4_setup
    spec = _lora_spec(key, role=role)
    _add_lora(monkeypatch, spec)
    _install_row(client, "sd15.fp16", tmp_path)
    _install_lora(client, spec)
    pid, c, sid, src, scene = c4_setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="a red door")
    return pid, c, sh, spec


def _style_setup(client, tmp_path, monkeypatch, pid, shot_id, c_id,
                 *existing):
    spec = _lora_spec("lora.style", role="style")
    _add_lora(monkeypatch, spec)
    # distinct weights — identical bytes would hit the same-sha duplicate
    # refusal in the B-1 gate
    from tests.test_g2b1_lora import _lora_bytes
    _install_lora(client, spec,
                  content=_lora_bytes(keys=("style.a", "style.b")))
    _commit(client, pid, "shot.cast.update",
            {"shot_id": shot_id, "character_id": c_id,
             "state": {"loras": [*existing, spec.key]}})
    return spec


# ------------------------------------------------------- acceptance

def test_preview_identity_only_accepted(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    _bind_loras(client, pid, sh["shot"]["id"], c["id"], spec.key)
    out = _preview(client, pid, model_key="sd15.fp16",
                   shot_id=sh["shot"]["id"],
                   loras=[{"key": spec.key, "strength": 0.7}])
    assert out["accepted"] is True
    assert out["errors"] == []
    a, = out["resolved"]
    assert a["key"] == spec.key and a["role"] == "identity"
    assert a["strength"] == 0.7
    assert a["bound_character_id"] == c["id"]
    assert a["staged_name"].startswith(f"{spec.key}-")
    assert a["sha256"] in out["hashes"]["adapter_sha256"].values()
    # internal row pointer never leaks
    assert "install_row_id" not in a


def test_preview_style_only_accepted(client, tmp_path, monkeypatch):
    pid, c, sh, _ = _full_setup(client, tmp_path, monkeypatch)
    _bind_loras(client, pid, sh["shot"]["id"], c["id"])  # bare cast row
    sty = _style_setup(client, tmp_path, monkeypatch, pid,
                       sh["shot"]["id"], c["id"])
    out = _preview(client, pid, model_key="sd15.fp16",
                   shot_id=sh["shot"]["id"],
                   loras=[{"key": sty.key}])
    assert out["accepted"] is True
    assert out["resolved"][0]["role"] == "style"
    assert out["resolved"][0]["strength"] == 1.0  # default


def test_preview_identity_plus_style(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    sid = sh["shot"]["id"]
    _bind_loras(client, pid, sid, c["id"], spec.key)
    sty = _style_setup(client, tmp_path, monkeypatch, pid, sid, c["id"],
                       spec.key)
    out = _preview(client, pid, model_key="sd15.fp16", shot_id=sid,
                   loras=[spec.key, {"key": sty.key, "strength": 0.5}],
                   prompt="a red door")
    assert out["accepted"] is True
    roles = {a["role"] for a in out["resolved"]}
    assert roles == {"identity", "style"}
    # prompt was supplied → real prompt + runtime hashes
    assert out["hashes"]["prompt_sha256"]
    assert out["hashes"]["runtime_prompt_sha256"]
    assert out["est_vram_lora_overhead_bytes"] == 2 * 768 * 1024 * 1024


def test_preview_empty_selection(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    out = _preview(client, pid, model_key="sd15.fp16",
                   shot_id=sh["shot"]["id"], loras=[])
    assert out["accepted"] is True
    assert out["resolved"] == []
    assert out["est_vram_lora_overhead_bytes"] == 0


def test_preview_derives_cast_bindings(client, tmp_path, monkeypatch):
    """loras omitted → the shot's stored cast lora_json is the proposal."""
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    sid = sh["shot"]["id"]
    _bind_loras(client, pid, sid, c["id"],
                spec.key, {"key": "ghost.style", "strength": 0.5})
    out = _preview(client, pid, model_key="sd15.fp16", shot_id=sid)
    assert out["context"]["proposed_from_cast"] is True
    assert [r["key"] for r in out["requested"]] == [spec.key, "ghost.style"]
    # the unknown stored key surfaces as an honest refusal, not a filter
    assert out["accepted"] is False
    assert any("unknown LoRA 'ghost.style'" in e for e in out["errors"])


def test_preview_keyframe_context(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    sid = sh["shot"]["id"]
    _bind_loras(client, pid, sid, c["id"], spec.key)
    kf = _mk_kf(client, pid, sid, "start", prompt="a red door")
    out = _preview(client, pid, model_key="sd15.fp16",
                   keyframe_id=kf["id"], loras=[spec.key])
    assert out["accepted"] is True
    assert out["context"]["shot_id"] == sid


# ------------------------------------------------------- refusals

def test_preview_duplicate_role_refused(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    other = _lora_spec("lora.id2")
    _add_lora(monkeypatch, other)
    from tests.test_g2b1_lora import _lora_bytes
    _install_lora(client, other, content=_lora_bytes(keys=("b.d", "b.u")))
    sid = sh["shot"]["id"]
    _bind_loras(client, pid, sid, c["id"], spec.key, other.key)
    out = _preview(client, pid, model_key="sd15.fp16", shot_id=sid,
                   loras=[spec.key, other.key])
    assert out["accepted"] is False
    assert any("at most one identity" in e for e in out["errors"])


def test_preview_unbound_identity_refused(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    out = _preview(client, pid, model_key="sd15.fp16",
                   shot_id=sh["shot"]["id"], loras=[spec.key])
    assert out["accepted"] is False
    assert any("not bound to a character" in e for e in out["errors"])


def test_preview_wrong_project_shot(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    other_pid = make_project(client)["id"]
    # shot belongs to pid — previewing it under another project refuses
    out = _preview(client, other_pid, model_key="sd15.fp16",
                   shot_id=sh["shot"]["id"], loras=[spec.key])
    assert out["accepted"] is False
    assert any("shot not found in this project" in e
               for e in out["errors"])


def test_preview_family_mismatch(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    bad = _lora_spec("lora.sdxl", family="sdxl")
    _add_lora(monkeypatch, bad)
    _install_lora(client, bad)
    sid = sh["shot"]["id"]
    _bind_loras(client, pid, sid, c["id"], bad.key)
    out = _preview(client, pid, model_key="sd15.fp16", shot_id=sid,
                   loras=[bad.key])
    assert out["accepted"] is False
    assert any("incompatible with 'sd1.5'" in e for e in out["errors"])


def test_preview_unsupported_role_and_format(client, tmp_path,
                                             monkeypatch):
    pid, c, sh, _ = _full_setup(client, tmp_path, monkeypatch)
    sid = sh["shot"]["id"]
    roleless = _lora_spec("lora.norole", role="fx")
    _add_lora(monkeypatch, roleless)
    _install_lora(client, roleless)
    gguf = _lora_spec("lora.gguf", fmt="gguf")
    _add_lora(monkeypatch, gguf)
    _install_lora(client, gguf)
    _bind_loras(client, pid, sid, c["id"], roleless.key, gguf.key)
    out = _preview(client, pid, model_key="sd15.fp16", shot_id=sid,
                   loras=[roleless.key])
    assert any("no lora_role" in e for e in out["errors"])
    out = _preview(client, pid, model_key="sd15.fp16", shot_id=sid,
                   loras=[gguf.key])
    assert any("safetensors" in e for e in out["errors"])


def test_preview_restricted_license(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    spec.license["commercial_ok"] = False
    sid = sh["shot"]["id"]
    _bind_loras(client, pid, sid, c["id"], spec.key)
    out = _preview(client, pid, model_key="sd15.fp16", shot_id=sid,
                   loras=[spec.key])
    assert out["accepted"] is False
    assert any("restricted license" in e for e in out["errors"])


def test_preview_not_installed_and_incomplete(client, tmp_path,
                                              monkeypatch):
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    sid = sh["shot"]["id"]
    missing = _lora_spec("lora.notinstalled")
    _add_lora(monkeypatch, missing)          # catalog only — no install
    _bind_loras(client, pid, sid, c["id"], missing.key)
    out = _preview(client, pid, model_key="sd15.fp16", shot_id=sid,
                   loras=[missing.key])
    assert any("not installed" in e for e in out["errors"])
    # installed row but bytes deleted → "install is incomplete"
    spec2 = _lora_spec("lora.deleted")
    _add_lora(monkeypatch, spec2)
    dest = _install_lora(client, spec2)
    dest.unlink()
    _commit(client, pid, "shot.cast.update",
            {"shot_id": sid, "character_id": c["id"],
             "state": {"loras": [missing.key, spec2.key]}})
    out = _preview(client, pid, model_key="sd15.fp16", shot_id=sid,
                   loras=[spec2.key])
    assert any("incomplete" in e for e in out["errors"])


@pytest.mark.parametrize("strength", [0, -0.5, 2.1, "strong", True])
def test_preview_invalid_strength(client, tmp_path, monkeypatch,
                                  strength):
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    sid = sh["shot"]["id"]
    _bind_loras(client, pid, sid, c["id"], spec.key)
    out = _preview(client, pid, model_key="sd15.fp16", shot_id=sid,
                   loras=[{"key": spec.key, "strength": strength}])
    assert out["accepted"] is False
    assert any("strength" in e for e in out["errors"])


def test_preview_missing_sha_refused(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _full_setup(
        client, tmp_path, monkeypatch, key="lora.nosha")
    # catalog spec carries no pinned sha (_lora_spec sha=None default);
    # blank the install row's recorded sha too → nothing left to pin
    from app.db.models import ModelInstall
    with client.app.state.session_factory() as s:
        row = s.execute(select(ModelInstall).where(
            ModelInstall.model_key == spec.key)).scalar_one()
        row.sha256 = ""
        s.commit()
    sid = sh["shot"]["id"]
    _bind_loras(client, pid, sid, c["id"], spec.key)
    out = _preview(client, pid, model_key="sd15.fp16", shot_id=sid,
                   loras=[spec.key])
    assert out["accepted"] is False
    assert any("no verifiable sha256" in e for e in out["errors"])


def test_preview_malformed_entry_refused(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    out = _preview(client, pid, model_key="sd15.fp16",
                   shot_id=sh["shot"]["id"], loras=[42])
    assert out["accepted"] is False
    assert out["requested"][0]["shape"] == "invalid"
    assert any("loras[0] must be" in e for e in out["errors"])


def test_preview_too_many_refused(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    out = _preview(client, pid, model_key="sd15.fp16",
                   shot_id=sh["shot"]["id"],
                   loras=[{"key": "a"}, {"key": "b"}, {"key": "c"}])
    assert any("at most 2 adapters" in e for e in out["errors"])


def test_preview_prompt_guard(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    sid = sh["shot"]["id"]
    _bind_loras(client, pid, sid, c["id"], spec.key)
    out = _preview(client, pid, model_key="sd15.fp16", shot_id=sid,
                   loras=[spec.key], prompt="a door <lora:evil:1>")
    assert out["accepted"] is False
    assert out["errors"] == []
    assert any("LoRA control tag" in e for e in out["prompt_errors"])
    # refused selection → runtime prompt hash reported unavailable
    assert out["hashes"]["runtime_prompt_sha256"] is None


# ------------------------------------------------------- semantics

def test_preview_is_side_effect_free(client, tmp_path, monkeypatch):
    """No command rows, no jobs, no revision bumps — SELECT only."""
    from app.db.models import CreativeCommand, GenerationJob
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    sid = sh["shot"]["id"]
    _bind_loras(client, pid, sid, c["id"], spec.key)
    with client.app.state.session_factory() as s:
        cmds = s.execute(
            select(func.count()).select_from(CreativeCommand)).scalar()
        jobs = s.execute(
            select(func.count()).select_from(GenerationJob)).scalar()
    _preview(client, pid, model_key="sd15.fp16", shot_id=sid,
             loras=[spec.key], prompt="a red door")
    _preview(client, pid, model_key="sd15.fp16", shot_id=sid,
             loras=[spec.key])  # accepted + refused paths alike
    with client.app.state.session_factory() as s:
        assert s.execute(
            select(func.count()).select_from(CreativeCommand)
            ).scalar() == cmds
        assert s.execute(
            select(func.count()).select_from(GenerationJob)
            ).scalar() == jobs


def test_preview_result_is_not_authorization(client, tmp_path,
                                             monkeypatch):
    """Stale-proofing: unbinding between preview and generation makes
    the same request refuse — the preview result carries no authority."""
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    sid = sh["shot"]["id"]
    _bind_loras(client, pid, sid, c["id"], spec.key)
    assert _preview(client, pid, model_key="sd15.fp16", shot_id=sid,
                    loras=[spec.key])["accepted"] is True
    _commit(client, pid, "shot.cast.update",
            {"shot_id": sid, "character_id": c["id"],
             "state": {"loras": []}})  # explicit unbind
    out = _preview(client, pid, model_key="sd15.fp16", shot_id=sid,
                   loras=[spec.key])
    assert out["accepted"] is False
    assert any("not bound" in e for e in out["errors"])


def test_preview_unavailable_hashes_named(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    sid = sh["shot"]["id"]
    _bind_loras(client, pid, sid, c["id"], spec.key)
    out = _preview(client, pid, model_key="sd15.fp16", shot_id=sid,
                   loras=[spec.key])          # no prompt supplied
    assert out["hashes"]["prompt_sha256"] is None
    assert any("no prompt supplied" in u
               for u in out["hashes"]["unavailable"])
    assert any("worker" in u and "job start" in u
               for u in out["hashes"]["unavailable"])
    assert any("advisory" in n for n in out["notes"])


# ------------------------------------------------------- context edges

def test_preview_wrong_project_keyframe(client, tmp_path, monkeypatch):
    """A keyframe from another project must not resolve its shot here."""
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    sid = sh["shot"]["id"]
    _bind_loras(client, pid, sid, c["id"], spec.key)
    kf = _mk_kf(client, pid, sid, "start", prompt="a red door")
    other_pid = make_project(client)["id"]
    out = _preview(client, other_pid, model_key="sd15.fp16",
                   keyframe_id=kf["id"], loras=[spec.key])
    assert out["accepted"] is False
    assert out["resolved"] == []
    assert any("keyframe not found in this project" in e
               for e in out["errors"])


def test_preview_conflicting_context(client, tmp_path, monkeypatch):
    """shot_id and keyframe_id resolving to different shots is an
    explicit context error — never a silent pick-one."""
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    sid = sh["shot"]["id"]
    _bind_loras(client, pid, sid, c["id"], spec.key)
    sh2 = _shot(client, pid, sh["shot"]["scene_id"],
                title="other", prompt="y")
    kf = _mk_kf(client, pid, sh2["shot"]["id"], "start", prompt="y")
    out = _preview(client, pid, model_key="sd15.fp16",
                   shot_id=sid, keyframe_id=kf["id"], loras=[spec.key])
    assert out["accepted"] is False
    assert out["resolved"] == []
    assert any("different shots" in e for e in out["errors"])


def test_preview_no_context_identity_refused(client, tmp_path,
                                             monkeypatch):
    """No shot/keyframe context: identity adapters refuse outright —
    there is nothing to bind them to — and the note says why."""
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    sid = sh["shot"]["id"]
    _bind_loras(client, pid, sid, c["id"], spec.key)   # bound, but no ctx
    out = _preview(client, pid, model_key="sd15.fp16",
                   loras=[spec.key])
    assert out["accepted"] is False
    assert any("not bound to a character" in e for e in out["errors"])
    assert any("no shot context" in n for n in out["notes"])
    assert out["context"]["shot_id"] is None


def test_preview_inherited_cast_authorized(client, tmp_path,
                                           monkeypatch):
    """A scene-level cast member the shot inherits satisfies the
    identity binding when a shot-level override row carries it."""
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    sid = sh["shot"]["id"]
    _commit(client, pid, "scene.cast.assign",
            {"scene_id": sh["shot"]["scene_id"],
             "character_id": c["id"]})
    # shot-level override row for the inherited member carries loras
    _bind_loras(client, pid, sid, c["id"], spec.key)
    out = _preview(client, pid, model_key="sd15.fp16", shot_id=sid,
                   loras=[spec.key])
    assert out["accepted"] is True
    assert out["resolved"][0]["bound_character_id"] == c["id"]


def test_preview_style_project_linkage(client, tmp_path, monkeypatch):
    """Documents existing B-1 style semantics: a style LoRA is
    authorized by project-level cast linkage, not this shot's row."""
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    sid_a = sh["shot"]["id"]
    sh_b = _shot(client, pid, sh["shot"]["scene_id"],
                 title="other", prompt="y")
    sty = _lora_spec("lora.style", role="style")
    _add_lora(monkeypatch, sty)
    _install_lora(client, sty)
    # bound on shot B's cast row — NOT shot A's
    _bind_loras(client, pid, sh_b["shot"]["id"], c["id"], sty.key)
    out = _preview(client, pid, model_key="sd15.fp16", shot_id=sid_a,
                   loras=[sty.key])
    assert out["accepted"] is True
    assert out["resolved"][0]["role"] == "style"


def test_preview_hash_provenance_semantics(client, tmp_path,
                                           monkeypatch):
    """Preview hashes cover the SUPPLIED prompt text + resolved tags —
    the response says so and never implies job-pin equivalence."""
    from app.domain import lora_tags
    pid, c, sh, spec = _full_setup(client, tmp_path, monkeypatch)
    sid = sh["shot"]["id"]
    _bind_loras(client, pid, sid, c["id"], spec.key)
    prompt = "a red door at dusk"
    out = _preview(client, pid, model_key="sd15.fp16", shot_id=sid,
                   loras=[spec.key], prompt=prompt)
    assert out["accepted"] is True
    h = out["hashes"]
    # derived from actual inputs — no invented values
    assert h["prompt_sha256"] == hashlib.sha256(
        prompt.encode("utf-8")).hexdigest()
    a = out["resolved"][0]
    assert h["runtime_prompt_sha256"] == lora_tags.runtime_prompt_sha256(
        prompt, [{"staged_name": a["staged_name"],
                  "strength": a["strength"]}])
    # supplied-vs-composed disclosure is rendered
    assert any("supplied" in n and "composed" in n
               for n in out["notes"])
    # the worker-side re-hash is still named unavailable
    assert any("worker" in u for u in h["unavailable"])
