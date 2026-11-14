"""C5-C — deterministic prompt composition + continuity guidance.

The composer (app/domain/prompt_compose.py) assembles provider prompts
from the user-authored shot text plus project context — character
identity, reference-set guidance, scene style, location, wardrobe,
expression/emotion, action, camera framing, lighting, continuity —
under a model-specific char budget with explicit omissions/conflicts.
"""
import pytest

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import (
    GEN, _commit, _preview, _setup, _shot, _scene, _char, _refset,
    _import, _job_req, _board)


def _cast_update(client, pid, shot_id, cid, **state):
    _commit(client, pid, "shot.cast.update",
            {"shot_id": shot_id, "character_id": cid, "state": state})


def _compose(client, pid, shot_id, extra=None, expect=200):
    """Preview shot.generate and return resolved_prompt."""
    p = _preview(client, pid, "shot.generate",
                 {**GEN, "shot_id": shot_id, **(extra or {})},
                 expect=expect)
    return p if expect != 200 else p.get("resolved_prompt")


def test_deterministic_composition(client, tmp_path):
    """Same inputs → same prompt, same sha, stable section order."""
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="misty dawn",
               source_asset_id=src["id"])
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"]})
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "reference_set_id": sid})
    r1 = _compose(client, pid, sh["shot"]["id"])
    r2 = _compose(client, pid, sh["shot"]["id"])
    assert r1 is not None and r1 == r2
    assert r1["prompt_sha256"] == r2["prompt_sha256"]
    assert r1["prompt"].startswith("misty dawn")
    order = {"scene": 0, "location": 1, "camera": 2, "cast": 3,
             "continuity": 4}
    ids = [order[s["id"]] for s in r1["sections"]]
    assert ids == sorted(ids)


def test_multi_character_separation(client, tmp_path):
    """Each cast member keeps its own identity + wardrobe block."""
    pid, c, sid, src, scene = _setup(client, tmp_path)
    c2 = _char(client, pid, "Mori")
    _commit(client, pid, "character.update",
            {"character_id": c["id"], "data": {"appearance": "short auburn hair"}})
    _commit(client, pid, "character.update",
            {"character_id": c2["id"], "data": {"appearance": "long grey coat"}})
    sh = _shot(client, pid, scene["scene"]["id"], prompt="confrontation")
    for ch in (c, c2):
        _commit(client, pid, "shot.cast.assign",
                {"shot_id": sh["shot"]["id"], "character_id": ch["id"]})
    _cast_update(client, pid, sh["shot"]["id"], c["id"],
                 wardrobe_variant="lab coat")
    _cast_update(client, pid, sh["shot"]["id"], c2["id"],
                 wardrobe_variant="black suit", emotion="contempt")
    rp = _compose(client, pid, sh["shot"]["id"])
    cast = [s for s in rp["sections"] if s["id"] == "cast"]
    assert len(cast) == 2
    ada, mori = cast
    assert ada["character"] == "Ada" and mori["character"] == "Mori"
    ada_text = "; ".join(f["text"] for f in ada["fields"])
    mori_text = "; ".join(f["text"] for f in mori["fields"])
    assert "short auburn hair" in ada_text and "lab coat" in ada_text
    assert "black suit" in mori_text and "contempt" in mori_text
    assert "black suit" not in ada_text and "lab coat" not in mori_text


def test_cast_ordering_deterministic(client, tmp_path):
    """Cast block order follows ShotCharacter.position regardless of
    assign order."""
    pid, c, sid, src, scene = _setup(client, tmp_path)
    c2, c3 = _char(client, pid, "B"), _char(client, pid, "Zed")
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x")
    for ch in (c3, c, c2):  # assign out of order → positions 0,1,2
        _commit(client, pid, "shot.cast.assign",
                {"shot_id": sh["shot"]["id"], "character_id": ch["id"]})
    # Reorder: B→0, Ada→1, Zed→2 — section order must follow position.
    _cast_update(client, pid, sh["shot"]["id"], c2["id"], position=0)
    _cast_update(client, pid, sh["shot"]["id"], c["id"], position=1)
    _cast_update(client, pid, sh["shot"]["id"], c3["id"], position=2)
    rp = _compose(client, pid, sh["shot"]["id"])
    names = [s["character"] for s in rp["sections"] if s["id"] == "cast"]
    assert names == ["B", "Ada", "Zed"]  # position order, not assign order


def test_user_prompt_precedence_and_verbatim(client, tmp_path):
    """User text leads and is never rewritten; context appends."""
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"],
               prompt="  exact user text, punctuation!  ")
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"]})
    rp = _compose(client, pid, sh["shot"]["id"])
    assert rp["user_prompt"] == "exact user text, punctuation!"
    assert rp["prompt"].startswith("exact user text, punctuation!")


def test_conflicting_guidance_surfaced(client, tmp_path):
    """Shot-vs-scene same-key divergence → conflict entry, shot wins."""
    pid, c, sid, src, scene = _setup(client, tmp_path)
    _commit(client, pid, "scene.update",
            {"scene_id": scene["scene"]["id"],
             "continuity": {"lighting": "golden hour"}})
    sh = _shot(client, pid, scene["scene"]["id"], prompt="dusk")
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"],
             "continuity": {"lighting": "overcast noon"}})
    rp = _compose(client, pid, sh["shot"]["id"])
    assert any("lighting" in c_ for c_ in rp["conflicts"])
    assert "overcast noon" in rp["prompt"]
    assert "golden hour" not in rp["prompt"]


def test_budget_overflow_omits_optional(client, tmp_path):
    """Tight model budget → lowest-precedence fields omitted + recorded,
    user prompt preserved in full."""
    pid, c, sid, src, scene = _setup(client, tmp_path)
    _commit(client, pid, "character.update",
            {"character_id": c["id"],
             "data": {"appearance": "a" * 400}})
    sh = _shot(client, pid, scene["scene"]["id"], prompt="keep me")
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"]})
    _cast_update(client, pid, sh["shot"]["id"], c["id"],
                 wardrobe_variant="w" * 400)
    rp = _compose(client, pid, sh["shot"]["id"],
                  extra={"model_id": "gpt-image-1"})
    # Simulate a tight budget via the composer directly for precision.
    from app.db.models import Project, Shot
    from app.domain import prompt_compose
    with client.app.state.session_factory() as s:
        shot_row = s.get(Shot, sh["shot"]["id"])
        comp = prompt_compose.compose_shot_prompt(
            s, s.get(Project, pid), shot_row, user_prompt="keep me",
            model_max_chars=120)
    assert comp["prompt"].startswith("keep me")
    assert comp["omitted"] and comp["budget"]["used"] <= 120
    assert any("omitted" in w for w in comp["warnings"])


def test_missing_metadata_safe(client, tmp_path):
    """Empty profile/cast fields → sections skipped, no placeholders."""
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="bare")
    rp = _compose(client, pid, sh["shot"]["id"])
    assert rp["prompt"] == "bare" or rp["prompt"].startswith("bare")
    assert rp["omitted"] == [] or all(isinstance(o, str)
                                      for o in rp["omitted"])


def test_refset_guidance_only_for_owning_character(client, tmp_path):
    """A bound set's notes appear only in its own character's block."""
    pid, c, sid, src, scene = _setup(client, tmp_path)
    c2 = _char(client, pid, "Mori")
    from app.db.models import ReferenceSet
    with client.app.state.session_factory() as s:
        s.get(ReferenceSet, sid).notes = "same face, silver eyes"
        s.commit()
    sh = _shot(client, pid, scene["scene"]["id"], prompt="duo")
    for ch in (c, c2):
        _commit(client, pid, "shot.cast.assign",
                {"shot_id": sh["shot"]["id"], "character_id": ch["id"]})
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "reference_set_id": sid})
    rp = _compose(client, pid, sh["shot"]["id"])
    cast = {s["character"]: s for s in rp["sections"] if s["id"] == "cast"}
    ada_fields = "; ".join(f["text"] for f in cast["Ada"]["fields"])
    assert "silver eyes" in ada_fields
    if "Mori" in cast:
        mori_fields = "; ".join(f["text"] for f in cast["Mori"]["fields"])
        assert "silver eyes" not in mori_fields


def test_expected_prompt_stale_rejection(client, tmp_path):
    """Character edit between preview and commit → expected_prompt
    mismatch refuses; re-preview produces a new sha."""
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="pinned")
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"]})
    prev = _preview(client, pid, "shot.generate",
                    {**GEN, "shot_id": sh["shot"]["id"]})
    ep = prev["resolved_prompt"]["prompt_sha256"]
    _commit(client, pid, "character.update",
            {"character_id": c["id"],
             "data": {"appearance": "now has a scar"}})
    r = _commit(client, pid, "shot.generate",
                {**GEN, "shot_id": sh["shot"]["id"],
                 "expected_prompt": ep,
                 "expected_shot": prev["resolved_shot"],
                 "expected_references": prev["resolved_references"],
                 "expected_set": prev["resolved_set"]},
                expect=422)
    assert "prompt context changed" in str(r.json())
    rp2 = _compose(client, pid, sh["shot"]["id"])
    assert rp2["prompt_sha256"] != ep


def test_compose_prompt_false_is_verbatim(client, tmp_path):
    """compose_prompt=False → provider sees exactly the user text."""
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="raw only")
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"]})
    prev = _preview(client, pid, "shot.generate",
                    {**GEN, "shot_id": sh["shot"]["id"],
                     "compose_prompt": False})
    assert prev.get("resolved_prompt") is None
    r = _commit(client, pid, "shot.generate",
                {**GEN, "shot_id": sh["shot"]["id"],
                 "compose_prompt": False})
    req = _job_req(client, r["job_id"])
    assert req["prompt"] == "raw only"


def test_prompt_context_in_provenance(client, tmp_path):
    """Job request carries the composition record for audit."""
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="prov")
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"]})
    r = _commit(client, pid, "shot.generate",
                {**GEN, "shot_id": sh["shot"]["id"]})
    req = _job_req(client, r["job_id"])
    pc = req["shot_generate"]["prompt_context"]
    assert pc["prompt_sha256"] and pc["sections"]
    assert req["shot_generate"]["user_prompt"] == "prov"
    assert req["prompt"].startswith("prov")


def test_cross_project_shot_rejected(client, tmp_path):
    pid, c, sid, src, scene = _setup(client, tmp_path)
    other = make_project(client, name="Other")
    scene2 = _scene(client, other["id"], "Foreign")
    sh2 = _shot(client, other["id"], scene2["scene"]["id"], prompt="x")
    r = _preview(client, pid, "shot.generate",
                 {**GEN, "shot_id": sh2["shot"]["id"]}, expect=404)
    assert "not found" in str(r.json()).lower()
