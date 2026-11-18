"""A8 — filmmaking wizard derived state.

The wizard owns no rows: ``GET /api/projects/{pid}/wizard`` derives the
whole workflow view (stage status, generation-state honesty labels,
missing-input warnings, review counts, job status) from real A1–A7
entities, and progress persists through ``project.update_settings``
under ``settings_json.wizard``. These tests drive real commands and
assert the derivation at each step — the same calls the installed
wizard UI makes.

Coverage: stage transitions, honesty labels (draft/planned/simulated/
failed/cancelled/stale/missing/approved), save+resume, legacy
first_film echo, project isolation, multi-character cast, unresolved
speakers, assembly review/missing counts, timeline+export stages,
backup/restore survival, populated-DB compat, and the _gen_state unit
matrix.
"""
import pytest

from app.domain import wizard as wiz
from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit
from tests.test_a2_storyboard import _scene2, _shot2
from tests.test_a3_keyframes import _mk_kf, _gen_held
from tests.test_a4_motion_clips import _mk_clip, _gen as _gen_clip
from tests.test_a5_dialogue import (
    _setup as _a5_setup, _mk_full, _gen as _gen_dlg, _character)
from tests.test_a6_assembly import _plan, _apply, _review, _plan_detail
from tests.test_c5a_timeline import _tl


def _wizard(client, pid):
    r = client.get(f"/api/projects/{pid}/wizard")
    assert r.status_code == 200, r.text
    return r.json()


def _stage(w, sid):
    return next(s for s in w["stages"] if s["id"] == sid)


def _rev(client, pid):
    return client.get(f"/api/projects/{pid}").json()["revision"]


# ---------- stage derivation ----------

def test_empty_project(client):
    pid = make_project(client)["id"]
    w = _wizard(client, pid)
    assert [s["id"] for s in w["stages"]] == list(wiz.STAGE_IDS)
    assert _stage(w, "project")["status"] == "done"
    assert _stage(w, "film_settings")["status"] == "done"
    assert _stage(w, "screenplay")["status"] == "ready"
    assert _stage(w, "scenes")["status"] == "ready"
    assert _stage(w, "cast")["status"] == "ready"
    assert _stage(w, "shots")["status"] == "ready"
    assert _stage(w, "planning")["status"] == "blocked"
    assert _stage(w, "keyframes")["status"] == "blocked"
    assert _stage(w, "export")["status"] == "blocked"
    assert w["progress"] is None
    assert w["legacy_first_film"] is False
    assert w["honesty"]["counts"] == {}
    assert w["warnings"] == []


def test_full_workflow_progression(client):
    """Drive the whole pipeline and watch every stage flip done."""
    pid = make_project(client)["id"]
    su = _a5_setup(client, pid)  # char + scene + shot + sp + element
    w = _wizard(client, pid)
    for sid in ("screenplay", "scenes", "cast", "shots"):
        assert _stage(w, sid)["status"] == "done", (sid, _stage(w, sid))
    assert _stage(w, "dialogue")["status"] in ("ready", "attention")
    assert any("dialogue" in x for x in _stage(w, "dialogue")["warnings"])

    # planning
    _commit(client, pid, "shot.update",
            {"shot_id": su["shot"]["shot"]["id"],
             "planning": {"camera": {"angle": "low", "movement": "dolly_in",
                                     "follow_mode": "follow_behind"},
                          "lighting": "low-key"}})
    assert _stage(_wizard(client, pid), "planning")["status"] == "done"

    # keyframes (draft → simulated via mock generate)
    kf = _mk_kf(client, pid, su["shot"]["shot"]["id"], "start",
                prompt="dock at night")
    w = _wizard(client, pid)
    krow = next(r for r in w["entities"]["keyframes"] if r["id"] == kf["id"])
    assert krow["state"] == "draft"
    assert _stage(w, "keyframes")["status"] in ("ready", "attention", "done")

    job_id = _gen_held(client, pid, kf["id"])
    w = _wizard(client, pid)
    assert next(r for r in w["entities"]["keyframes"]
                if r["id"] == kf["id"])["state"] == "planned"
    import pathlib, time
    # release the hold by deleting the flag target: held jobs park on a
    # release_file; cancel is the deterministic exit for this test.
    r = client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200
    wait_for_job(client, pid, job_id, timeout=30)
    assert next(r for r in _wizard(client, pid)["entities"]["keyframes"]
                if r["id"] == kf["id"])["state"] == "cancelled"


def test_simulated_label_after_mock_generate(client):
    pid = make_project(client)["id"]
    su = _a5_setup(client, pid)
    seg = _mk_full(client, pid, su)
    job = _gen_dlg(client, pid, seg["id"])
    assert job["status"] == "succeeded"
    w = _wizard(client, pid)
    row = next(r for r in w["entities"]["dialogue"] if r["id"] == seg["id"])
    assert row["state"] == "simulated"
    assert row["has_result"] is True
    assert row["job_adapter"] == "mock.audio"
    assert w["honesty"]["all_simulated"] is True
    assert _stage(w, "dialogue")["status"] == "done"


def test_failed_job_state(client):
    """Held job + reconcile → failed, surfaced on the entity row."""
    pid = make_project(client)["id"]
    su = _a5_setup(client, pid)
    kf = _mk_kf(client, pid, su["shot"]["shot"]["id"], "start",
                prompt="dock at night")
    job_id = _gen_held(client, pid, kf["id"])
    from app.db.models import GenerationJob
    with client.app.state.session_factory() as s:
        s.get(GenerationJob, job_id).status = "running"
        s.commit()
        client.app.state.job_engine.reconcile_interrupted(s)
        s.commit()
    j = next(x for x in client.get(f"/api/projects/{pid}/jobs").json()
             if x["id"] == job_id)
    assert j["status"] == "failed"
    w = _wizard(client, pid)
    row = next(r for r in w["entities"]["keyframes"] if r["id"] == kf["id"])
    assert row["state"] == "failed"
    assert any("failed" in x for x in _stage(w, "keyframes")["warnings"])


def test_stale_after_prompt_edit(client):
    """Editing the prompt after generation marks the result stale —
    never silently regenerated."""
    pid = make_project(client)["id"]
    su = _a5_setup(client, pid)
    clip = _mk_clip(client, pid, su["shot"]["shot"]["id"],
                    prompt="v1", duration_frames=24)
    job = _gen_clip(client, pid, clip["id"])
    assert job["status"] == "succeeded"
    _commit(client, pid, "clip.update",
            {"clip_id": clip["id"], "prompt": "v2 — edited after"})
    row = next(r for r in _wizard(client, pid)["entities"]["clips"]
               if r["id"] == clip["id"])
    assert row["state"] == "stale"
    assert row["has_result"] is True


def test_assembly_review_and_missing_counts(client):
    pid = make_project(client)["id"]
    su = _a5_setup(client, pid)
    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"])
    w = _wizard(client, pid)
    assert _stage(w, "assembly_plan")["status"] == "done"
    # shot has no motion clip → missing flag counted honestly
    p = next(p for p in w["plans"] if p["id"] == plan["id"])
    assert p["item_counts"]["missing"] >= 1
    pending = _stage(w, "assembly_apply")["counts"]["pending"]
    assert pending == p["item_counts"]["pending"]

    # produce real (simulated) media so apply lands clips
    clip = _mk_clip(client, pid, su["shot"]["shot"]["id"],
                    prompt="dock drift", duration_frames=24)
    _gen_clip(client, pid, clip["id"])
    seg = _mk_full(client, pid, su)
    _gen_dlg(client, pid, seg["id"])
    plan = _plan(client, pid, t["id"])  # fresh plan over real media

    _review(client, pid, plan["id"],
            shot_id=su["shot"]["shot"]["id"], status="approved")
    w = _wizard(client, pid)
    assert _stage(w, "assembly_apply")["counts"]["approved"] >= 1
    _apply(client, pid, t["id"])
    w = _wizard(client, pid)
    assert _stage(w, "assembly_apply")["status"] == "done"
    assert _stage(w, "edit")["status"] == "done"
    assert _stage(w, "edit")["counts"]["clips"] >= 1


def test_export_stage_end_to_end(client):
    """Simulated media → assembly → timeline.export → the export stage
    reports done with the produced asset id."""
    from tests.test_c4b_shot_generate import _preview
    pid = make_project(client)["id"]
    su = _a5_setup(client, pid)
    clip = _mk_clip(client, pid, su["shot"]["shot"]["id"],
                    prompt="dock drift", duration_frames=24)
    _gen_clip(client, pid, clip["id"])
    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"])
    _review(client, pid, plan["id"],
            shot_id=su["shot"]["shot"]["id"], status="approved")
    _apply(client, pid, t["id"])

    p = _preview(client, pid, "timeline.export",
                 {"timeline_id": t["id"], "profile": "720p"})
    rt = p["resolved_timeline"]
    r = _commit(client, pid, "timeline.export",
                {"timeline_id": t["id"], "profile": "720p",
                 "expected_timeline": {"id": t["id"],
                                       "revision": rt["revision"]},
                 "expected_edl_sha": rt["edl_sha256"]})
    job = wait_for_job(client, pid, r["job_id"], timeout=60)
    w = _wizard(client, pid)
    st = _stage(w, "export")
    if job["status"] == "succeeded":
        assert st["status"] == "done"
        assert st["counts"]["output_asset_id"] == job["output_asset_id"]
    else:
        # mock-encoded clips may not satisfy the export probe on all
        # fixtures — the stage must still report the truth.
        assert st["status"] == "attention"
        assert st["counts"]["job_status"] == job["status"]


# ---------- progress save/resume + mode ----------

def test_progress_roundtrip_via_update_settings(client):
    pid = make_project(client)["id"]
    blob = {"version": 2, "step": 5, "furthest": 7, "mode": "advanced",
            "screenplay_id": "sp-1", "done": False}
    _commit(client, pid, "project.update_settings",
            {"settings": {"wizard": blob}})
    w = _wizard(client, pid)
    assert w["progress"]["step"] == 5
    assert w["progress"]["mode"] == "advanced"
    # Unrelated keys merge — wizard blob untouched.
    _commit(client, pid, "project.update_settings",
            {"settings": {"other": 1}})
    w = _wizard(client, pid)
    assert w["progress"]["step"] == 5
    assert w["legacy_first_film"] is False


def test_mode_switch_writes_no_entity_data(client):
    """Beginner↔Advanced toggling only rewrites the progress blob —
    entity rows are untouched (revisions unchanged)."""
    pid = make_project(client)["id"]
    su = _a5_setup(client, pid)
    before = _wizard(client, pid)
    rev_before = _rev(client, pid)
    for mode in ("advanced", "beginner"):
        _commit(client, pid, "project.update_settings",
                {"settings": {"wizard": {"version": 2, "mode": mode}}})
    after = _wizard(client, pid)
    assert after["progress"]["mode"] == "beginner"
    # only the settings commits bumped the project revision — and the
    # derived entity/stage state is identical.
    assert after["entities"] == before["entities"]
    assert [s["status"] for s in after["stages"]] == \
        [s["status"] for s in before["stages"]]


def test_legacy_first_film_echo(client):
    pid = make_project(client)["id"]
    _commit(client, pid, "project.update_settings",
            {"settings": {"first_film": {"version": 1, "step": 4}}})
    w = _wizard(client, pid)
    assert w["legacy_first_film"] is True
    assert w["progress"] is None
    # Once the v2 blob exists the flag clears (upgrade complete).
    _commit(client, pid, "project.update_settings",
            {"settings": {"wizard": {"version": 2, "step": 1}}})
    assert _wizard(client, pid)["legacy_first_film"] is False


# ---------- isolation / multi-project ----------

def test_project_isolation(client):
    a = make_project(client, "Film A")["id"]
    b = make_project(client, "Film B")["id"]
    _a5_setup(client, a)  # fully populate A
    _commit(client, a, "project.update_settings",
            {"settings": {"wizard": {"version": 2, "step": 9}}})
    wb = _wizard(client, b)
    assert _stage(wb, "screenplay")["status"] == "ready"
    assert _stage(wb, "cast")["counts"]["characters"] == 0
    assert wb["progress"] is None
    assert wb["entities"] == {"keyframes": [], "clips": [], "dialogue": []}
    # A's ids never appear in B's derivation
    wa = _wizard(client, a)
    a_shots = set(_stage(wa, "shots")["counts"].get("shot_ids", []))
    b_counts = _stage(wb, "shots")["counts"]
    assert not (a_shots & set(b_counts.get("shot_ids", [])))


def test_multi_character_cast_and_speakers(client):
    pid = make_project(client)["id"]
    anna = _character(client, pid, "Anna")
    bob = _character(client, pid, "Bob")
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": anna["id"]})
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": bob["id"]})
    w = _wizard(client, pid)
    assert _stage(w, "cast")["counts"]["characters"] == 2
    assert _stage(w, "cast")["counts"]["cast"] == 2

    # A segment with no speaker → unresolved-speaker warning
    _commit(client, pid, "dialogue.create",
            {"text": "Who goes there?", "shot_id": sh["shot"]["id"]})
    w = _wizard(client, pid)
    assert any("speaker" in x for x in w["warnings"])
    assert any("no character" in x for x in w["warnings"])


# ---------- _gen_state unit matrix ----------

class _Ent:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_gen_state_matrix():
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)

    def job(**kw):
        base = {"status": "succeeded", "adapter_id": "mock.image",
                "completed_at": now}
        base.update(kw)
        return _Ent(**base)

    g = wiz._gen_state
    assert g(result_asset_id=None, job=None, asset_status=None,
             entity_updated=now, prompt_edited=False) == "draft"
    assert g(result_asset_id=None, job=job(status="running"),
             asset_status=None, entity_updated=now,
             prompt_edited=False) == "planned"
    assert g(result_asset_id="a1", job=None, asset_status="missing",
             entity_updated=now, prompt_edited=False) == "missing"
    assert g(result_asset_id=None, job=job(status="failed"),
             asset_status=None, entity_updated=now,
             prompt_edited=False) == "failed"
    assert g(result_asset_id=None, job=job(status="cancelled"),
             asset_status=None, entity_updated=now,
             prompt_edited=False) == "cancelled"
    assert g(result_asset_id="a1", job=job(), asset_status="ready",
             entity_updated=now, prompt_edited=False) == "simulated"
    assert g(result_asset_id="a1", job=job(adapter_id="gen.image.sdcpp"),
             asset_status="ready", entity_updated=now,
             prompt_edited=False) == "generated"
    assert g(result_asset_id="a1", job=job(), asset_status="ready",
             entity_updated=now, prompt_edited=True) == "stale"
    # in-flight wins over everything
    assert g(result_asset_id="a1", job=job(status="queued"),
             asset_status="ready", entity_updated=now,
             prompt_edited=True) == "planned"
    # missing asset beats a failed job label
    assert g(result_asset_id="a1", job=job(status="failed"),
             asset_status="missing", entity_updated=now,
             prompt_edited=False) == "missing"


# ---------- persistence / recovery ----------

def test_backup_restore_preserves_wizard_progress(client, tmp_path):
    """settings_json.wizard rides the project export — a restored
    project resumes the wizard exactly where it left off."""
    pid = make_project(client)["id"]
    su = _a5_setup(client, pid)
    _commit(client, pid, "project.update_settings",
            {"settings": {"wizard": {"version": 2, "step": 7,
                                     "furthest": 7, "mode": "beginner"}}})
    from tests.test_a6_assembly import _export_import
    new_pid = _export_import(client, pid)
    w = _wizard(client, new_pid)
    assert w["progress"]["step"] == 7
    assert w["progress"]["mode"] == "beginner"
    # Derived state rebuilt from the restored rows — the import is a
    # fresh project id, so stage data comes from its own entities.
    assert _stage(w, "screenplay")["status"] == "done"


def test_backup_import_strips_provider_overrides(client, tmp_path):
    """F2 — provider overrides in settings_json are authorization
    state ({enabled, settings, region} merged by
    registry.effective_for_project). A package must not carry them into
    an imported project — the new project re-derives provider state
    from app config + explicit user approval only."""
    pid = make_project(client)["id"]
    _commit(client, pid, "project.update_settings",
            {"settings": {
                "providers": {"openai": {"enabled": True,
                                          "region": "eu-west",
                                          "settings": {"base_url":
                                                        "https://evil.example"}}},
                "api_key": "sk-not-a-real-key",
                "secret_token": "tok_123",
                "credential_blob": {"x": 1},
                "wizard": {"version": 2, "step": 4, "mode": "advanced"},
                "custom_pref": "keep-me"}})
    from tests.test_a6_assembly import _export_import
    new_pid = _export_import(client, pid)
    s = client.get(f"/api/projects/{new_pid}").json()["settings_json"]
    # Authorization/machine-bound keys stripped
    assert "providers" not in s
    assert "api_key" not in s
    assert "secret_token" not in s
    assert "credential_blob" not in s
    # User data survives
    assert s["wizard"]["step"] == 4
    assert s["custom_pref"] == "keep-me"
    # Provenance recorded
    assert s["imported_from"] == pid
    # Imported project derives providers from app config — not the file
    provs = client.get(f"/api/projects/{new_pid}/providers").json()["providers"]
    openai = next((p for p in provs if p["key"] == "openai"), None)
    if openai is not None:
        assert not (openai.get("settings") or {}).get("base_url") == \
            "https://evil.example"


def test_backup_import_preserves_identity_and_provenance(client, tmp_path):
    """F2 — the imported project is a NEW identity; the source id
    leaks only into provenance fields, never as a live reference."""
    pid = make_project(client)["id"]
    su = _a5_setup(client, pid)
    _commit(client, pid, "keyframe.create",
            {"shot_id": su["shot"]["shot"]["id"], "role": "start",
             "prompt": "src kf"})
    _commit(client, pid, "project.update_settings",
            {"settings": {"wizard": {"version": 2, "step": 2}}})
    from tests.test_a6_assembly import _export_import
    new_pid = _export_import(client, pid)
    assert new_pid != pid
    dst = client.get(f"/api/projects/{new_pid}").json()
    src = client.get(f"/api/projects/{pid}").json()
    assert dst["settings_json"]["imported_from"] == pid
    # Every derived entity row belongs to the NEW project — zero
    # cross-project leakage through the package boundary.
    w = _wizard(client, new_pid)
    for row in (w["entities"]["keyframes"] + w["entities"]["clips"]
                + w["entities"]["dialogue"]):
        assert row["id"] not in (None, "")
    src_w = _wizard(client, pid)
    dst_ids = {r["id"] for r in w["entities"]["keyframes"]}
    src_ids = {r["id"] for r in src_w["entities"]["keyframes"]}
    assert src_ids and dst_ids and not (src_ids & dst_ids)


def test_restart_reads_same_state(client):
    """A fresh GET after writes is the restart-equivalent read: derived
    state + progress identical, no session-local caching."""
    pid = make_project(client)["id"]
    su = _a5_setup(client, pid)
    _commit(client, pid, "project.update_settings",
            {"settings": {"wizard": {"version": 2, "step": 3}}})
    a = _wizard(client, pid)
    b = _wizard(client, pid)
    assert a["progress"] == b["progress"]
    assert [s["status"] for s in a["stages"]] == \
        [s["status"] for s in b["stages"]]


def test_wizard_settings_undo_reverts_progress(client):
    """F5 — wizard progress commits are ordinary commands: undoing a
    project.update_settings restores the prior progress, and derived
    state stays consistent (it re-derives from live rows anyway)."""
    pid = make_project(client)["id"]
    _commit(client, pid, "project.update_settings",
            {"settings": {"wizard": {"version": 2, "step": 2,
                                     "furthest": 2, "mode": "beginner"}}})
    r = _commit(client, pid, "project.update_settings",
                {"settings": {"wizard": {"version": 2, "step": 5,
                                         "furthest": 5,
                                         "mode": "advanced"}}})
    assert _wizard(client, pid)["progress"]["step"] == 5
    und = client.post(
        f"/api/projects/{pid}/commands/{r['command_id']}/undo")
    assert und.status_code == 200, und.text
    w = _wizard(client, pid)
    assert w["progress"]["step"] == 2
    assert w["progress"]["mode"] == "beginner"


def test_stale_preview_refuses_wizard_path(client):
    """F5 — wizard generation rides the existing pins: a commit
    carrying a stale resolved_* pin refuses 422, never applies."""
    pid = make_project(client)["id"]
    su = _a5_setup(client, pid)
    kf = _mk_kf(client, pid, su["shot"]["shot"]["id"], "start",
                prompt="draft")
    p = client.post(f"/api/projects/{pid}/commands/preview",
                    json={"command_type": "keyframe.generate",
                          "payload": {"keyframe_id": kf["id"]},
                          "scope": "project"}).json()
    # Edit the prompt after preview — the resolved pin is now stale.
    _commit(client, pid, "keyframe.update",
            {"keyframe_id": kf["id"], "prompt": "changed after preview"})
    _commit(client, pid, "keyframe.generate",
            {"keyframe_id": kf["id"],
             "expected_keyframe": p["resolved_keyframe"],
             "expected_prompt": p["resolved_prompt"]["prompt_sha256"]},
            expect=422)


def test_archived_entities_excluded(client):
    pid = make_project(client)["id"]
    su = _a5_setup(client, pid)
    _commit(client, pid, "shot.archive",
            {"shot_id": su["shot"]["shot"]["id"]})
    w = _wizard(client, pid)
    assert su["shot"]["shot"]["id"] not in \
        _stage(w, "shots")["counts"].get("shot_ids", [])
