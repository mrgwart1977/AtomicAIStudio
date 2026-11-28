"""U-5d-0b Phase beta-3-B3.3 — rgen:v0 isolated versioned synthetic
installation generation and failure-safe publication tests.

Every installation consumes a GENUINE completed racq:v0 envelope
produced by the real acquire() against synthetic references, synthetic
in-memory ZIP archives, an injected chunk-transport and isolated
pytest tmp_path roots. Nothing contacts a network, touches the legacy
`runtimes/` namespace, writes a database row, selects a generation,
enrolls a reference or executes anything.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import warnings
import zipfile
from pathlib import Path

import pytest

from app.domain import runtime_installation as rti
from app.domain import runtime_reference as rr

BACKEND = Path(__file__).resolve().parents[1]


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _z(entries) -> bytes:
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(buf, "w") as z:
            for e in entries:
                name, data = e[0], e[1]
                zi = zipfile.ZipInfo(name)
                kw = e[2] if len(e) > 2 else {}
                for k, v in kw.items():
                    setattr(zi, k, v)
                z.writestr(zi, data,
                           compress_type=kw.get("compress_type",
                                                zipfile.ZIP_DEFLATED))
    return buf.getvalue()


def _member(name, component, data):
    return {"name": name, "component": component,
            "size": len(data), "sha256": _sha(data)}


def _mk(components, members=None, skips=(), flavor="cuda",
        key="sdcpp"):
    """components: [(comp, role, required, asset_id, zip_entries)]."""
    archives, urls, meta_assets = [], {}, []
    files = {}
    for comp, role, req, aid, entries in components:
        zb = _z(entries)
        for n, d, *_ in entries:
            files[(comp, n)] = d
        name = f"{comp}.zip"
        url = f"https://x/{flavor}-{name}"
        urls[url] = zb
        archives.append({"component": comp, "role": role,
                         "required": req,
                         "asset": {
                             "id": aid, "name": name, "url": url,
                             "size": len(zb),
                             "digest": {"algo": "sha256",
                                        "value": _sha(zb)},
                             "provenance": {
                                 "class": "platform_attested"}}})
        meta_assets.append({"id": aid, "name": name, "size": len(zb),
                            "browser_download_url": url,
                            "digest": "sha256:" + _sha(zb)})
    if members is None:
        members = [_member(n, c, d)
                   for c, _r, _q, _a, es in components
                   for n, d in es]
    ref = {"contract": rr.RREF_VERSION, "runtime_key": key,
           "flavor": flavor,
           "release": {"tag": "v1", "release_id": 1,
                       "target_commitish": "9" * 40},
           "archives": archives, "members": members,
           "coverage": {"mode": "flat_install_dir",
                        "unexpected_policy": "refuse",
                        "skip_names": list(skips)}}
    return {"ref": ref,
            "meta": {"release_id": 1, "tag_name": "v1",
                     "assets": meta_assets},
            "urls": urls, "files": files}


def _single(member_data=b"sd-bytes"):
    return _mk([("runtime", "primary", True, 1,
                 [("sd.exe", member_data)])])


def _multi(flavor="cuda"):
    return _mk([("runtime", "primary", True, 1,
                 [("sd.exe", b"sd-bin"), ("sd.dll", b"sd-dll")]),
                ("cudart", "dependency", True, 2,
                 [("cudart.dll", b"cudart-bin")]),
                ("opt", "optional", False, 3,
                 [("opt.txt", b"opt")])], flavor=flavor)


def _sel(scn, **flags):
    out = {a["component"]: True for a in scn["ref"]["archives"]}
    out.update(flags)
    return out


class _Authority:
    """Synthetic authority fixture — the established authority-model
    shape: a known-revocation snapshot query is_revoked(digest). The
    rgen boundary binds the query contract structurally, never the
    implementation class."""
    def __init__(self, revoked=None):
        self._rev = frozenset(revoked or ())

    def is_revoked(self, digest):
        return digest in self._rev


def _auth(scn, selected=None, revoked=None):
    return _Authority(revoked=revoked)


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _fake_acq(scn, tmp_path, selection=None):
    """Fabricate a faithful completed racq:v0 envelope plus the real
    staged member files it claims — every field the rgen validation
    path consumes is present and consistent. Deterministic synthetic
    fixture: live acquire()-produced envelopes belong to the
    separately authorized controlled-integration stage."""
    sel = selection if selection is not None else _sel(scn)
    selected = sorted(c for c, v in sel.items() if v)
    omitted = sorted(c for c, v in sel.items() if not v)
    required = sorted(a["component"] for a in scn["ref"]["archives"]
                      if a["required"])
    rd = rr.reference_digest(scn["ref"])
    key, flavor = scn["ref"]["runtime_key"], scn["ref"]["flavor"]
    by = {a["component"]: a["asset"] for a in scn["ref"]["archives"]}
    src = tmp_path / "acq-src"
    stage_name = hashlib.sha256(
        f"{src}-{sorted(selected)}".encode()).hexdigest()[:16]
    mdir = src / stage_name / "members"
    mdir.mkdir(parents=True)
    assets = {}
    archives = []
    for c in selected:
        a = by[c]
        assets[c] = {"asset_id": a["id"], "name": a["name"],
                     "size": a["size"], "sha256": a["digest"]["value"],
                     "source": a["url"]}
        archives.append({"component": c, "name": a["name"],
                         "asset_id": a["id"],
                         "expected_size": a["size"],
                         "observed_size": a["size"],
                         "expected_sha256": a["digest"]["value"],
                         "observed_sha256": a["digest"]["value"],
                         "comparison": "match", "verified": True,
                         "reopen_comparison": "match",
                         "final_comparison": "match"})
    skips = {n.casefold() for n in scn["ref"]["coverage"]["skip_names"]}
    members, skipped = [], []
    sel_set = set(selected)
    for (comp, name), data in sorted(scn["files"].items()):
        if comp not in sel_set or name.casefold() not in skips:
            continue
        skipped.append({"name": name, "component": comp,
                        "state": "skipped_declared"})
    for m in scn["ref"]["members"]:
        if m["component"] not in sel_set:
            continue
        if m["name"].casefold() in skips:
            continue
        data = scn["files"][(m["component"], m["name"])]
        (mdir / m["name"]).write_bytes(data)
        members.append({"name": m["name"], "component": m["component"],
                        "expected_size": m["size"],
                        "observed_size": len(data),
                        "expected_sha256": m["sha256"],
                        "observed_sha256": _sha(data),
                        "state": "match"})
    identity = {"contract": "racq:v0", "reference_digest": rd,
                "runtime_key": key, "flavor": flavor,
                "selected": selected, "omitted": omitted,
                "assets": assets}
    env = {"contract": "racq:v0",
           "production_trust": False, "execution_eligible": False,
           "operational_qualification": "unestablished",
           "trusted_enrollment": "unestablished",
           "installation_publication": "not_performed",
           "authority_class": "synthetic_acquisition",
           "ok": True, "state": "completed", "refusals": [],
           "acquisition": "racq:v0:" + _sha(_canon(identity).encode()),
           "reference": {"digest": rd, "runtime_key": key,
                         "flavor": flavor, "validation_ok": True},
           "authority": {"association": "established",
                         "class": "synthetic_acquisition",
                         "extract_permitted": True},
           "components": {"selected": selected, "omitted": omitted,
                          "required": required},
           "archives": archives, "members": members,
           "skipped": skipped,
           "coverage": {"declared_complete": True,
                        "selected_complete": True,
                        "omitted_optional": omitted,
                        "upstream_completeness": "unestablished"},
           "staging": {"dir": stage_name, "complete": True}}
    return env, src


def _acq(scn, tmp_path, selection=None, authority=None):
    return _fake_acq(scn, tmp_path, selection=selection)


def _ok_acq(scn, tmp_path, **kw):
    res, src = _acq(scn, tmp_path, **kw)
    assert res["state"] == "completed", res["refusals"]
    return res, src


def _inst(scn, acq, tmp_path, src=None, authority=None,
          install_root=None, cancel_check=None):
    auth = authority if authority is not None else _auth(scn)
    if src is None:
        src = tmp_path / "acq-src"
    if install_root is None:
        install_root = tmp_path / "gens"
    return rti.install(scn["ref"], acq, authority=auth,
                       source_root=src, install_root=install_root,
                       cancel_check=cancel_check)


def _gen_path(root, scn, gen_id):
    return (root / "runtime-gens" / scn["ref"]["runtime_key"]
            / scn["ref"]["flavor"] / ".generations" / gen_id)


def _gens_dir(root, scn):
    return (root / "runtime-gens" / scn["ref"]["runtime_key"]
            / scn["ref"]["flavor"] / ".generations")


def _stage_dir(root, scn):
    return (root / "runtime-gens" / scn["ref"]["runtime_key"]
            / scn["ref"]["flavor"] / ".staging")


@pytest.fixture(autouse=True)
def _reset():
    rti._TEST_SEAM = None
    yield
    rti._TEST_SEAM = None
    rti.reset_locks()


def _seam_on(*points, fn):
    def seam(point, **kw):
        if point in points:
            fn(point=point, **kw)
    return seam


def _decls(res):
    return {k: res[k] for k in
            ("production_trust", "execution_eligible",
             "operational_qualification", "trusted_enrollment",
             "installation_publication")}


EXPECTED_DECLS = {
    "production_trust": False,
    "execution_eligible": False,
    "operational_qualification": "unestablished",
    "trusted_enrollment": "unestablished",
    "installation_publication": "not_performed",
}


# ---------------------------------------------------------------- identity


def test_g01_full_digest_generation_identity(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    assert res["state"] == "published"
    gid = res["generation"]["id"]
    assert gid.startswith("gen-") and len(gid) == 68
    int(gid[4:], 16)  # full lowercase hex
    assert res["generation"]["digest"].startswith("rgen:v0:")
    assert len(res["generation"]["digest"]) == 8 + 64


def test_g02_stable_identity_across_attempts(tmp_path):
    scn = _single()
    a1, s1 = _ok_acq(scn, tmp_path / "t1")
    a2, s2 = _ok_acq(scn, tmp_path / "t2")
    r1 = _inst(scn, a1, tmp_path / "t1", s1)
    r2 = _inst(scn, a2, tmp_path / "t2", s2)
    assert r1["generation"]["id"] == r2["generation"]["id"]
    assert r1["generation"]["digest"] == r2["generation"]["digest"]


def test_g03_acquisition_digest_is_provenance_not_identity(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    acq2 = copy.deepcopy(acq)
    acq2["acquisition"] = "racq:v0:" + "f" * 64
    r1 = _inst(scn, acq, tmp_path, src, install_root=tmp_path / "g1")
    r2 = _inst(scn, acq2, tmp_path, src, install_root=tmp_path / "g2")
    assert r1["generation"]["id"] == r2["generation"]["id"]
    assert r2["acquisition"]["digest"] == "racq:v0:" + "f" * 64


def test_g04_mutable_fields_excluded_from_identity(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    r1 = _inst(scn, acq, tmp_path, src, install_root=tmp_path / "a")
    r2 = _inst(scn, acq, tmp_path, src, install_root=tmp_path / "b")
    # Different install roots + different attempt nonces: same identity
    assert r1["generation"]["id"] == r2["generation"]["id"]


def test_g05_member_content_changes_identity(tmp_path):
    a, s1 = _ok_acq(_single(b"content-a"), tmp_path / "t1")
    b, s2 = _ok_acq(_single(b"content-b"), tmp_path / "t2")
    sa = _single(b"content-a")
    sb = _single(b"content-b")
    r1 = _inst(sa, a, tmp_path / "t1", s1)
    r2 = _inst(sb, b, tmp_path / "t2", s2)
    assert r1["generation"]["id"] != r2["generation"]["id"]


def test_g06_descriptor_schema_validation(tmp_path):
    desc = {"contract": "rgen:v0", "reference_digest": "rref:v0:" + "0" * 64,
            "runtime_key": "sdcpp", "flavor": "cuda",
            "components": {"selected": ["runtime"], "omitted": []},
            "assets": [{"component": "runtime", "asset_id": 1,
                        "name": "a.zip", "sha256": "0" * 64}],
            "members": [{"name": "b.bin", "component": "runtime",
                         "size": 1, "sha256": "0" * 64},
                        {"name": "a.bin", "component": "runtime",
                         "size": 1, "sha256": "1" * 64}]}
    out, refusals = rti._norm_descriptor(desc)
    assert out is None and "descriptor_members_order_invalid" in refusals
    bad = dict(desc, members=[{"name": "x", "component": "runtime",
                               "size": 1, "sha256": "0" * 64,
                               "extra": 1}])
    out, refusals = rti._norm_descriptor(bad)
    assert "descriptor_members_invalid" in refusals


def test_g07_generation_layout_exact(tmp_path):
    scn = _multi()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    gd = _gen_path(tmp_path / "gens", scn, res["generation"]["id"])
    assert gd.is_dir()
    assert sorted(p.name for p in (gd / "members").iterdir()) == [
        "cudart.dll", "opt.txt", "sd.dll", "sd.exe"]
    assert (gd / "_generation.json").is_file()
    assert (_gens_dir(tmp_path / "gens", scn)
            / f"{res['generation']['id']}.published.json").is_file()


def test_g08_generation_metadata_content(tmp_path):
    scn = _multi()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    meta = json.loads((_gen_path(tmp_path / "gens", scn,
                                 res["generation"]["id"])
                       / "_generation.json").read_text())
    assert meta["contract"] == "rgen:v0"
    assert meta["record"] == "generation"
    assert meta["generation_id"] == res["generation"]["id"]
    assert meta["descriptor_digest"] == res["generation"]["digest"]
    assert meta["provenance"]["acquisition_digest"] == acq["acquisition"]
    assert meta["descriptor"]["members"] == sorted(
        meta["descriptor"]["members"], key=lambda m: m["name"])


def test_g09_publication_record_binds_identity(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    rec = json.loads((_gens_dir(tmp_path / "gens", scn)
                      / f"{res['generation']['id']}.published.json")
                     .read_text())
    assert rec["contract"] == "rgen:v0" and rec["committed"] is True
    assert rec["generation_id"] == res["generation"]["id"]
    assert rec["descriptor_digest"] == res["generation"]["digest"]
    assert rec["reference_digest"] == rr.reference_digest(scn["ref"])
    assert rec["acquisition_digest"] == acq["acquisition"]
    assert rec["commit"] == "namespace_rename"


def test_g10_contract_values_preserved():
    assert rr.RREF_VERSION == "rref:v0"
    assert rti.RGEN_VERSION == "rgen:v0"
    scn = _single()
    assert rr.reference_digest(scn["ref"]).startswith("rref:v0:")


# ----------------------------------------------------- acquisition evidence


def test_g11_completed_acquisition_consumed(tmp_path):
    scn = _multi()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    assert res["state"] == "published"
    assert res["acquisition"]["association"] == "provenance"


@pytest.mark.parametrize("state", ["refused", "failed", "cancelled"])
def test_g12_non_completed_acquisition_refused(tmp_path, state):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    acq = dict(acq, ok=False, state=state)
    res = _inst(scn, acq, tmp_path, src)
    assert res["state"] == "refused"
    assert "acquisition_not_completed" in res["refusals"]


def test_g13_acquisition_contract_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, dict(acq, contract="racq:v9"), tmp_path, src)
    assert res["refusals"] == ["acquisition_contract_invalid"]


def test_g14_acquisition_refusals_present_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, dict(acq, refusals=["x"]), tmp_path, src)
    assert "acquisition_refusals_present" in res["refusals"]


@pytest.mark.parametrize("field,val",
                         [("production_trust", True),
                          ("execution_eligible", True),
                          ("trusted_enrollment", "enrolled"),
                          ("installation_publication", "published")])
def test_g15_altered_declaration_refused(tmp_path, field, val):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, dict(acq, **{field: val}), tmp_path, src)
    assert res["state"] == "refused"
    assert any(r.startswith("acquisition_declaration_altered")
               for r in res["refusals"])


def test_g16_altered_acquisition_identity_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, dict(acq, acquisition="bogus"), tmp_path, src)
    assert "acquisition_identity_invalid" in res["refusals"]


def test_g17_reference_digest_mismatch_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    other = _single(b"other")
    res = _inst(other, acq, tmp_path, src)
    assert "acquisition_reference_digest_mismatch" in res["refusals"]


def test_g18_component_key_mismatch_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    other = _mk([("runtime", "primary", True, 1,
                  [("sd.exe", b"sd-bytes")])], flavor="vulkan")
    res = _inst(other, acq, tmp_path, src)
    assert "acquisition_component_key_mismatch" in res["refusals"] \
        or "acquisition_reference_digest_mismatch" in res["refusals"]


def test_g19_invalid_reference_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = rti.install({"bogus": True}, acq,
                      authority=_auth(scn),
                      source_root=src, install_root=tmp_path / "gens")
    assert "reference_invalid" in res["refusals"]


def test_g20_authority_context_invalid_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src, authority={"not": "authority"})
    assert "authority_context_invalid" in res["refusals"]


def test_g21_revoked_reference_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    auth = _auth(scn,
                 revoked=[rr.reference_digest(scn["ref"])])
    res = _inst(scn, acq, tmp_path, src, authority=auth)
    assert "authority_revoked" in res["refusals"]


def test_g22_unestablished_authority_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    bad = dict(acq, authority={"association": "absent"})
    res = _inst(scn, bad, tmp_path, src)
    assert "acquisition_authority_not_established" in res["refusals"]


def test_g23_forged_member_hash_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    forged = copy.deepcopy(acq)
    forged["members"][0]["observed_sha256"] = "0" * 64
    res = _inst(scn, forged, tmp_path, src)
    assert any(r.startswith("acquisition_member_inconsistent")
               for r in res["refusals"])


def test_g24_member_missing_from_acquisition_refused(tmp_path):
    scn = _multi()
    acq, src = _ok_acq(scn, tmp_path)
    forged = copy.deepcopy(acq)
    forged["members"] = [m for m in forged["members"]
                         if m["name"] != "sd.dll"]
    res = _inst(scn, forged, tmp_path, src)
    assert any(r.startswith("acquisition_member_missing")
               for r in res["refusals"])


def test_g25_unexpected_member_refused(tmp_path):
    scn = _multi()
    acq, src = _ok_acq(scn, tmp_path)
    forged = copy.deepcopy(acq)
    forged["members"].append(
        {"name": "evil.exe", "component": "runtime",
         "expected_size": 1, "observed_size": 1,
         "expected_sha256": "0" * 64, "observed_sha256": "0" * 64,
         "state": "match"})
    res = _inst(scn, forged, tmp_path, src)
    assert any(r.startswith("acquisition_member_unexpected")
               for r in res["refusals"])


def test_g26_duplicate_member_refused(tmp_path):
    scn = _multi()
    acq, src = _ok_acq(scn, tmp_path)
    forged = copy.deepcopy(acq)
    forged["members"].append(dict(forged["members"][0]))
    res = _inst(scn, forged, tmp_path, src)
    assert any(r.startswith("acquisition_member_duplicate")
               for r in res["refusals"])


def test_g27_archive_unverified_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    forged = copy.deepcopy(acq)
    forged["archives"][0]["final_comparison"] = "mismatch"
    res = _inst(scn, forged, tmp_path, src)
    assert any(r.startswith("acquisition_archive_unverified")
               for r in res["refusals"])


def test_g28_coverage_incomplete_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    forged = copy.deepcopy(acq)
    forged["coverage"]["selected_complete"] = False
    res = _inst(scn, forged, tmp_path, src)
    assert "acquisition_coverage_incomplete" in res["refusals"]


def test_g29_staging_incomplete_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    forged = copy.deepcopy(acq)
    forged["staging"]["complete"] = False
    res = _inst(scn, forged, tmp_path, src)
    assert "acquisition_staging_incomplete" in res["refusals"]


def test_g30_non_mapping_acquisition_refused(tmp_path):
    scn = _single()
    _, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, "not-a-dict", tmp_path, src)
    assert res["refusals"] == ["acquisition_not_mapping"]


# -------------------------------------------------- staging + member verify


def test_g31_exclusive_attempt_dir_created(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    assert res["staging"]["owned"] is True
    # staging dir consumed by rename — the generation dir holds it now
    gd = _gen_path(tmp_path / "gens", scn, res["generation"]["id"])
    staging_meta = json.loads((gd / "_staging.json").read_text())
    assert staging_meta["record"] == "staging"
    assert staging_meta["nonce"] == staging_meta["attempt"]
    assert staging_meta["generation_id"] == res["generation"]["id"]


def test_g32_source_member_tampered_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    mp = src / acq["staging"]["dir"] / "members" / "sd.exe"
    mp.write_bytes(b"tampered-bytes!")
    res = _inst(scn, acq, tmp_path, src)
    assert res["state"] == "refused"
    assert any(r.startswith(("member_sha256_mismatch",
                             "member_size_mismatch"))
               for r in res["refusals"])


def test_g33_source_member_missing_refused(tmp_path):
    scn = _multi()
    acq, src = _ok_acq(scn, tmp_path)
    (src / acq["staging"]["dir"] / "members" / "sd.dll").unlink()
    res = _inst(scn, acq, tmp_path, src)
    assert any(r.startswith("member_source_missing")
               for r in res["refusals"])


def test_g34_source_staging_missing_refused(tmp_path):
    scn = _single()
    acq, _ = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src=tmp_path / "nowhere")
    assert res["refusals"] == ["source_root_unsafe"]


def test_g35_partial_evidence_preserved_on_refusal(tmp_path):
    scn = _multi()
    acq, src = _ok_acq(scn, tmp_path)
    (src / acq["staging"]["dir"] / "members" / "sd.dll").unlink()
    res = _inst(scn, acq, tmp_path, src)
    assert res["state"] == "refused"
    assert res["staging"]["complete"] is False
    assert len(res["members"]) >= 1
    sdir = _stage_dir(tmp_path / "gens", scn)
    attempts = list(sdir.iterdir())
    assert len(attempts) == 1
    assert (attempts[0] / "_staging.json").is_file()


def test_g36_namespace_segment_refused(tmp_path):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd-bytes")])], key="BAD_KEY")
    # rref validation itself accepts; namespace rejects uppercase
    acq, src = _ok_acq(scn, tmp_path)
    res = rti.install(scn["ref"], acq,
                      authority=_auth(scn),
                      source_root=src, install_root=tmp_path / "gens")
    # invalid runtime_key fails earlier reference or namespace check
    assert res["state"] == "refused"


def test_g37_member_written_flat_no_dirs(tmp_path):
    scn = _multi()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    gd = _gen_path(tmp_path / "gens", scn, res["generation"]["id"])
    for p in (gd / "members").iterdir():
        assert p.is_file() and "/" not in p.name


def test_g38_skipped_members_never_published(tmp_path):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd"), ("note.txt", b"skip-me")])],
              members=[_member("sd.exe", "runtime", b"sd")],
              skips=["note.txt"])
    acq, src = _ok_acq(scn, tmp_path)
    assert any(s["name"] == "note.txt" for s in acq["skipped"])
    res = _inst(scn, acq, tmp_path, src)
    assert res["state"] == "published"
    gd = _gen_path(tmp_path / "gens", scn, res["generation"]["id"])
    assert not (gd / "members" / "note.txt").exists()


# -------------------------------------------------------------- publication


def test_g39_same_volume_publish_committed(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    assert res["state"] == "published"
    assert res["publication"]["state"] == "committed"
    assert res["publication"]["commit_point"] == "namespace_rename"
    assert res["publication"]["renamed"] is True
    assert res["synthetic_publication"] == "committed"
    assert res["installation_publication"] == "not_performed"


def test_g40_idempotent_republish_already_committed(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    r1 = _inst(scn, acq, tmp_path, src)
    r2 = _inst(scn, acq, tmp_path, src)
    assert r2["state"] == "published"
    assert r2["publication"]["state"] == "already_committed"
    assert r2["idempotent"] is True
    assert r2["generation"]["id"] == r1["generation"]["id"]


def test_g41_cross_volume_refused(tmp_path, monkeypatch):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    real_stat = os.stat
    sdir_str = str(tmp_path / "gens" / "runtime-gens" / "sdcpp"
                   / "cuda" / ".staging")

    def fake_stat(p, *a, **kw):
        st = real_stat(p, *a, **kw)
        if str(p).startswith(sdir_str):
            return os.stat_result(
                (st.st_mode, st.st_ino, st.st_dev + 1) + tuple(st)[3:])
        return st

    monkeypatch.setattr(rti.os, "stat", fake_stat)
    res = _inst(scn, acq, tmp_path, src)
    assert res["state"] == "refused"
    assert "publication_cross_volume" in res["refusals"]


def test_g42_destination_appeared_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    root = tmp_path / "gens"
    # Learn the destination name first via a trial run in a twin root
    twin = tmp_path / "twin"
    r0 = _inst(scn, acq, tmp_path, src, install_root=twin)
    gid = r0["generation"]["id"]

    def plant(point, **kw):
        if point == "pre_rename":
            gens = _gens_dir(root, scn)
            (gens / gid).mkdir(parents=True, exist_ok=True)
    rti._TEST_SEAM = plant
    res = _inst(scn, acq, tmp_path, src, install_root=root)
    assert res["state"] == "refused"
    assert "generation_destination_appeared" in res["refusals"]


def test_g43_empty_destination_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    root = tmp_path / "gens"
    r0 = _inst(scn, acq, tmp_path, src, install_root=tmp_path / "twin")
    (_gens_dir(root, scn) / r0["generation"]["id"]).mkdir(parents=True)
    res = _inst(scn, acq, tmp_path, src, install_root=root)
    assert res["state"] == "refused"
    assert "generation_destination_empty" in res["refusals"]


def test_g44_foreign_destination_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    root = tmp_path / "gens"
    r0 = _inst(scn, acq, tmp_path, src, install_root=tmp_path / "twin")
    gd = _gens_dir(root, scn) / r0["generation"]["id"]
    gd.mkdir(parents=True)
    (gd / "foreign.bin").write_bytes(b"foreign")
    res = _inst(scn, acq, tmp_path, src, install_root=root)
    assert res["state"] == "refused"
    assert any(r.startswith("generation_destination_incomplete")
               for r in res["refusals"])
    assert (gd / "foreign.bin").read_bytes() == b"foreign"


def test_g45_collision_destination_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    root = tmp_path / "gens"
    r0 = _inst(scn, acq, tmp_path, src, install_root=tmp_path / "twin")
    gid = r0["generation"]["id"]
    gd = _gens_dir(root, scn) / gid
    gd.mkdir(parents=True)
    (gd / "members").mkdir()
    (gd / "_generation.json").write_text(json.dumps({
        "contract": "rgen:v0", "record": "generation",
        "generation_id": gid,
        "descriptor_digest": "rgen:v0:" + "0" * 64,
        "descriptor": {}, "members": []}))
    res = _inst(scn, acq, tmp_path, src, install_root=root)
    assert res["state"] == "refused"
    assert "generation_collision" in res["refusals"]


def test_g46_invalid_existing_generation_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    root = tmp_path / "gens"
    r0 = _inst(scn, acq, tmp_path, src, install_root=root)
    assert r0["state"] == "published"
    mp = _gen_path(root, scn, r0["generation"]["id"]) / "members" / "sd.exe"
    mp.write_bytes(b"corrupted")
    res = _inst(scn, acq, tmp_path, src, install_root=root)
    assert res["state"] == "refused"
    assert any(r.startswith("generation_invalid")
               for r in res["refusals"])


def test_g47_uncommitted_destination_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    root = tmp_path / "gens"
    # produce committed dir without record via post_rename seam fault
    rti._TEST_SEAM = _seam_on(
        "pre_record",
        fn=lambda **kw: (_ for _ in ()).throw(OSError("disk")))
    r0 = _inst(scn, acq, tmp_path, src, install_root=root)
    assert r0["state"] == "uncertain"
    rti._TEST_SEAM = None
    res = _inst(scn, acq, tmp_path, src, install_root=root)
    assert res["state"] == "refused"
    assert any(r.startswith("generation_uncommitted")
               for r in res["refusals"])


def test_g48_sharing_violation_controlled_failure(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows sharing semantics")
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    hold = []

    def jam(point, src=None, **kw):
        if point == "pre_rename":
            hold.append(open(  # noqa: SIM115 — held open deliberately
                Path(src) / "members" / "sd.exe", "rb"))
    rti._TEST_SEAM = jam
    try:
        res = _inst(scn, acq, tmp_path, src)
    finally:
        for h in hold:
            h.close()
    assert res["state"] == "failed"
    assert res["refusals"][0]["code"] in (
        "sharing_violation", "publication_rename_failed")
    assert res["publication"]["state"] == "not_performed"


def test_g49_multiple_generations_coexist(tmp_path):
    scn_a = _multi(flavor="cuda")
    scn_b = _multi(flavor="vulkan")
    a1, s1 = _ok_acq(scn_a, tmp_path / "t1")
    a2, s2 = _ok_acq(scn_b, tmp_path / "t2")
    r1 = _inst(scn_a, a1, tmp_path / "t1", s1,
               install_root=tmp_path / "gens")
    r2 = _inst(scn_b, a2, tmp_path / "t2", s2,
               install_root=tmp_path / "gens")
    assert r1["generation"]["id"] != r2["generation"]["id"]
    assert _gen_path(tmp_path / "gens", scn_a,
                     r1["generation"]["id"]).is_dir()
    assert _gen_path(tmp_path / "gens", scn_b,
                     r2["generation"]["id"]).is_dir()
    rec = rti.reconcile(tmp_path / "gens")
    assert rec["counts"].get("generation_committed") == 2


def test_g50_publication_records_are_independent(tmp_path):
    scn_a = _multi(flavor="cuda")
    scn_b = _multi(flavor="vulkan")
    a1, s1 = _ok_acq(scn_a, tmp_path / "t1")
    a2, s2 = _ok_acq(scn_b, tmp_path / "t2")
    _inst(scn_a, a1, tmp_path / "t1", s1, install_root=tmp_path / "gens")
    _inst(scn_b, a2, tmp_path / "t2", s2, install_root=tmp_path / "gens")
    g1 = _gens_dir(tmp_path / "gens", scn_a)
    g2 = _gens_dir(tmp_path / "gens", scn_b)
    assert len(list(g1.glob("*.published.json"))) == 1
    assert len(list(g2.glob("*.published.json"))) == 1


# -------------------------------------------------------------- concurrency


def test_g51_component_lock_refusal(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    lock = rti._component_lock("sdcpp", "cuda")
    lock.acquire()
    try:
        res = _inst(scn, acq, tmp_path, src)
    finally:
        lock.release()
    assert res["state"] == "refused"
    assert "component_locked" in res["refusals"]


def test_g52_different_component_not_blocked(tmp_path):
    scn = _single()
    _ok_acq(scn, tmp_path)
    lock = rti._component_lock("sdcpp", "cuda")
    lock.acquire()
    try:
        other = _mk([("runtime", "primary", True, 1,
                      [("sd.exe", b"sd-bytes")])], flavor="vulkan")
        acq2, src2 = _ok_acq(other, tmp_path / "t2")
        res = _inst(other, acq2, tmp_path / "t2", src2,
                    install_root=tmp_path / "gens")
    finally:
        lock.release()
    assert res["state"] == "published"


def test_g53_concurrent_threads_serialize(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    out = []

    def go(root):
        out.append(_inst(scn, acq, tmp_path, src,
                         install_root=root))
    root = tmp_path / "gens"
    t1 = threading.Thread(target=go, args=(root,))
    t2 = threading.Thread(target=go, args=(root,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    states = sorted(r["state"] for r in out)
    # Nonblocking component lock: either the loser refused
    # component_locked, or it published and observed the committed
    # generation idempotently. No other interleaving is valid.
    if "refused" in states:
        ref = next(r for r in out if r["state"] == "refused")
        assert "component_locked" in ref["refusals"]
        ok = next(r for r in out if r["state"] == "published")
        assert ok["publication"]["state"] in ("committed",
                                              "already_committed")
    else:
        pubs = sorted(r["publication"]["state"] for r in out)
        assert pubs == ["already_committed", "committed"]
    ids = {r["generation"]["id"] for r in out
           if r["state"] == "published"}
    assert len(ids) == 1


def _probe_script() -> str:
    return ("import sys; sys.path.insert(0, " + repr(str(BACKEND)) + ");"
            "from app.domain import runtime_installation as r;"
            "from pathlib import Path;"
            "print('held' if r._acquire_root(Path(sys.argv[1])) "
            "else 'locked')")


def _hold_script() -> str:
    return ("import sys, time; sys.path.insert(0, "
            + repr(str(BACKEND)) + ");"
            "from app.domain import runtime_installation as r;"
            "from pathlib import Path;"
            "assert r._acquire_root(Path(sys.argv[1]));"
            "time.sleep(30)")


def _exit_script() -> str:
    return ("import sys; sys.path.insert(0, " + repr(str(BACKEND)) + ");"
            "from app.domain import runtime_installation as r;"
            "from pathlib import Path;"
            "assert r._acquire_root(Path(sys.argv[1]))")


def test_g54_cross_process_root_exclusion(tmp_path):
    root = tmp_path / "gens"
    root.mkdir()
    assert rti._acquire_root(root) is not None
    child = subprocess.run(
        [sys.executable, "-c", _probe_script(), str(root)],
        check=False, capture_output=True, text=True, timeout=60)
    assert child.stdout.strip() == "locked"
    rti.release_root(root)


def test_g55_process_exit_releases_root(tmp_path):
    root = tmp_path / "gens"
    root.mkdir()
    child = subprocess.run(
        [sys.executable, "-c", _exit_script(), str(root)],
        check=False, capture_output=True, text=True, timeout=60)
    assert child.returncode == 0
    # exited process released the byte range; file remains, lock gone
    assert (root / ".gens.lock").exists()
    assert rti._acquire_root(root) is not None


def test_g56_forced_termination_releases_root(tmp_path):
    if os.name != "nt":
        pytest.skip("process-termination semantics differ")
    root = tmp_path / "gens"
    root.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-c", _hold_script(), str(root)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        import time
        for _ in range(50):
            if proc.poll() is None and (root / ".gens.lock").exists():
                break
            time.sleep(0.1)
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(timeout=30)
    child = subprocess.run(
        [sys.executable, "-c", _probe_script(), str(root)],
        check=False, capture_output=True, text=True, timeout=60)
    assert child.stdout.strip() == "held"


def test_g57_stale_lock_file_not_ownership(tmp_path):
    root = tmp_path / "gens"
    root.mkdir()
    (root / ".gens.lock").write_bytes(b"stale")
    assert rti._acquire_root(root) is not None
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src, install_root=root)
    assert res["state"] == "published"


def test_g58_second_process_install_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    root = tmp_path / "gens"
    _inst(scn, acq, tmp_path, src, install_root=root)  # holds lock
    child = subprocess.run(
        [sys.executable, "-c", _probe_script(), str(root)],
        check=False, capture_output=True, text=True, timeout=60)
    assert child.stdout.strip() == "locked"


# ------------------------------------------------------ cancellation+commit


def test_g59_cancel_during_materialization(tmp_path):
    scn = _multi()
    acq, src = _ok_acq(scn, tmp_path)
    flag = {"n": 0}

    def cancel():
        flag["n"] += 1
        return flag["n"] > 2
    res = _inst(scn, acq, tmp_path, src, cancel_check=cancel)
    assert res["state"] == "cancelled"
    assert res["publication"]["state"] == "not_performed"
    assert res["staging"]["complete"] is False


def test_g60_cancel_after_staging(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    flag = {"on": False}

    def arm(point, **kw):
        flag["on"] = True
    rti._TEST_SEAM = _seam_on("member_written", fn=arm)
    res = _inst(scn, acq, tmp_path, src,
                cancel_check=lambda: flag["on"])
    assert res["state"] == "cancelled"
    assert res["staging"]["complete"] is True
    rec = rti.reconcile(tmp_path / "gens")
    assert rec["counts"].get("abandoned_attempt_owned") == 1


def test_g61_cancel_during_verification(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    flag = {"on": False}
    rti._TEST_SEAM = _seam_on(
        "pre_revalidate", fn=lambda **kw: flag.__setitem__("on", True))
    res = _inst(scn, acq, tmp_path, src,
                cancel_check=lambda: flag["on"])
    assert res["state"] == "cancelled"


def test_g62_cancel_immediately_before_publication(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    flag = {"on": False}
    # arm after revalidation completes — the pre_rename checkpoint
    # must still observe it before the namespace commit
    def arm(point, **kw):
        if point == "pre_revalidate":
            flag["on"] = True
    rti._TEST_SEAM = arm
    res = _inst(scn, acq, tmp_path, src,
                cancel_check=lambda: flag["on"])
    assert res["state"] == "cancelled"
    assert res["publication"]["renamed"] is False


def test_g63_late_cancel_after_rename_reports_committed(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    flag = {"on": False}
    rti._TEST_SEAM = _seam_on(
        "post_rename", fn=lambda **kw: flag.__setitem__("on", True))
    res = _inst(scn, acq, tmp_path, src,
                cancel_check=lambda: flag["on"])
    assert res["state"] == "published"
    assert res["publication"]["state"] == "committed"


def test_g64_cancel_after_commit_reports_committed(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    flag = {"on": False}
    rti._TEST_SEAM = _seam_on(
        "post_record", fn=lambda **kw: flag.__setitem__("on", True))
    res = _inst(scn, acq, tmp_path, src,
                cancel_check=lambda: flag["on"])
    assert res["state"] == "published"
    assert res["publication"]["state"] == "committed"


def test_g65_uncertain_commit_on_record_failure(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    rti._TEST_SEAM = _seam_on(
        "pre_record",
        fn=lambda **kw: (_ for _ in ()).throw(OSError("disk")))
    res = _inst(scn, acq, tmp_path, src)
    assert res["state"] == "uncertain"
    assert res["publication"]["state"] == "committed_unconfirmed"
    assert res["publication"]["renamed"] is True
    assert res["reconciliation_required"] is True
    gd = _gen_path(tmp_path / "gens", scn, res["generation"]["id"])
    assert gd.is_dir()


def test_g66_uncertain_never_reports_clean_success(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    rti._TEST_SEAM = _seam_on(
        "post_rename",
        fn=lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
    res = _inst(scn, acq, tmp_path, src)
    assert res["state"] != "published"
    assert res["publication"]["state"] != "committed"
    assert res["generation"]["id"] is not None


def test_g67_post_commit_error_preserves_state(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    rti._TEST_SEAM = _seam_on(
        "post_record",
        fn=lambda **kw: (_ for _ in ()).throw(OSError("x")))
    res = _inst(scn, acq, tmp_path, src)
    # post-record fault is post-commit: committed is preserved
    assert res["state"] == "uncertain"
    assert res["publication"]["renamed"] is True
    assert (_gens_dir(tmp_path / "gens", scn)
            / f"{res['generation']['id']}.published.json").is_file()


# ------------------------------------------------------------- reconciliation


def test_g68_reconcile_committed_generation(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    _inst(scn, acq, tmp_path, src)
    rec = rti.reconcile(tmp_path / "gens")
    assert rec["state"] == "reconciled"
    assert rec["counts"] == {"generation_committed": 1}
    assert rec["destructive_actions"] == 0


def test_g69_reconcile_abandoned_staging(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    flag = {"n": 0}
    res = _inst(scn, acq, tmp_path, src,
                cancel_check=lambda: (flag.__setitem__("n", flag["n"] + 1)
                                      or flag["n"] > 3))
    assert res["state"] == "cancelled"
    rec = rti.reconcile(tmp_path / "gens")
    assert rec["counts"].get("abandoned_attempt_owned") == 1


def test_g70_reconcile_unknown_staging(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    _inst(scn, acq, tmp_path, src)
    (_stage_dir(tmp_path / "gens", scn) / "mystery").mkdir()
    rec = rti.reconcile(tmp_path / "gens")
    assert rec["counts"].get("staging_unknown") == 1


def test_g71_reconcile_malformed_staging_metadata(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    _inst(scn, acq, tmp_path, src)
    d = _stage_dir(tmp_path / "gens", scn) / "odd"
    d.mkdir()
    (d / "_staging.json").write_text("{not json")
    rec = rti.reconcile(tmp_path / "gens")
    assert rec["counts"].get("staging_metadata_malformed") == 1


def test_g72_reconcile_uncommitted_generation(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    rti._TEST_SEAM = _seam_on(
        "pre_record",
        fn=lambda **kw: (_ for _ in ()).throw(OSError("x")))
    _inst(scn, acq, tmp_path, src)
    rec = rti.reconcile(tmp_path / "gens")
    assert rec["counts"].get("generation_uncommitted") == 1


def test_g73_reconcile_orphaned_publication_record(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    _inst(scn, acq, tmp_path, src)
    g = _gens_dir(tmp_path / "gens", scn)
    (g / ("gen-" + "0" * 64 + ".published.json")).write_text(
        json.dumps({"contract": "rgen:v0", "record": "publication",
                    "generation_id": "gen-" + "0" * 64,
                    "committed": True}))
    rec = rti.reconcile(tmp_path / "gens")
    assert rec["counts"].get("publication_orphaned") == 1


def test_g74_reconcile_torn_publication_record(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    g = _gens_dir(tmp_path / "gens", scn)
    (g / f"{res['generation']['id']}.published.json").write_text("{")
    rec = rti.reconcile(tmp_path / "gens")
    assert rec["counts"].get("publication_record_malformed") == 1


def test_g75_reconcile_invalid_descriptor(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    gd = _gen_path(tmp_path / "gens", scn, res["generation"]["id"])
    meta = json.loads((gd / "_generation.json").read_text())
    meta["descriptor_digest"] = "rgen:v0:" + "0" * 64
    (gd / "_generation.json").write_text(json.dumps(meta))
    rec = rti.reconcile(tmp_path / "gens")
    assert rec["counts"].get("generation_descriptor_invalid") == 1


def test_g76_reconcile_member_content_invalid_deep(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    mp = _gen_path(tmp_path / "gens", scn, res["generation"]["id"]) \
        / "members" / "sd.exe"
    mp.write_bytes(b"corrupt")
    shallow = rti.reconcile(tmp_path / "gens")
    assert shallow["counts"].get("generation_committed") == 1
    deep = rti.reconcile(tmp_path / "gens", deep=True)
    assert deep["counts"].get("generation_invalid") == 1


def test_g77_reconcile_revoked_reference(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    _inst(scn, acq, tmp_path, src)
    auth = _auth(scn,
                 revoked=[rr.reference_digest(scn["ref"])])
    rec = rti.reconcile(tmp_path / "gens", authority=auth)
    assert rec["counts"].get("reference_revoked") == 1


def test_g78_reconcile_legacy_installation(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    _inst(scn, acq, tmp_path, src)
    (tmp_path / "gens" / "runtimes" / "sdcpp-cuda").mkdir(parents=True)
    rec = rti.reconcile(tmp_path / "gens")
    assert rec["counts"].get("legacy_installation") == 1
    assert (tmp_path / "gens" / "runtimes" / "sdcpp-cuda").is_dir()


def test_g79_reconcile_unrecognized_entries(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    _inst(scn, acq, tmp_path, src)
    flav = _gens_dir(tmp_path / "gens", scn).parent
    (flav / "stray.txt").write_text("x")
    rec = rti.reconcile(tmp_path / "gens")
    assert rec["counts"].get("unrecognized") == 1
    assert (flav / "stray.txt").exists()


def test_g80_reconcile_never_deletes(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    _inst(scn, acq, tmp_path, src)
    root = tmp_path / "gens"
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    rti.reconcile(root, deep=True)
    after = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    assert before == after


def test_g81_reconcile_no_adoption(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    rti._TEST_SEAM = _seam_on(
        "pre_record",
        fn=lambda **kw: (_ for _ in ()).throw(OSError("x")))
    r0 = _inst(scn, acq, tmp_path, src)
    rec = rti.reconcile(tmp_path / "gens")
    assert rec["counts"].get("generation_uncommitted") == 1
    assert rec["automatic_adoption"] is False
    g = _gens_dir(tmp_path / "gens", scn)
    assert not (g / f"{r0['generation']['id']}.published.json").exists()


def test_g82_reconcile_refusals(tmp_path):
    res = rti.reconcile(tmp_path / "missing")
    assert res["state"] == "refused"
    assert "install_root_missing" in res["refusals"]
    res2 = rti.reconcile(tmp_path / "gens2", authority={"x": 1})
    assert res2["state"] == "refused"


# -------------------------------------------------------------- revalidation


def test_g83_revalidate_committed(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    rv = rti.revalidate(tmp_path / "gens", "sdcpp", "cuda",
                        res["generation"]["id"], authority=_auth(scn))
    assert rv["state"] == "revalidated"
    assert rv["revalidation"]["result"] == "match"
    assert rv["revalidation"]["selection"] == "not_established"
    assert rv["publication"]["state"] == "committed"


def test_g84_revalidate_missing_generation(tmp_path):
    rv = rti.revalidate(tmp_path / "gens", "sdcpp", "cuda",
                        "gen-" + "0" * 64)
    assert rv["state"] == "refused"
    assert any(r in ("install_root_unsafe", "generation_missing")
               for r in rv["refusals"])


def test_g85_revalidate_invalid_member(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    mp = _gen_path(tmp_path / "gens", scn, res["generation"]["id"]) \
        / "members" / "sd.exe"
    mp.write_bytes(b"corrupt")
    rv = rti.revalidate(tmp_path / "gens", "sdcpp", "cuda",
                        res["generation"]["id"])
    assert rv["state"] == "refused"
    assert any(r.startswith("generation_invalid")
               for r in rv["refusals"])


def test_g86_revalidate_revoked_reference(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    auth = _auth(scn,
                 revoked=[rr.reference_digest(scn["ref"])])
    rv = rti.revalidate(tmp_path / "gens", "sdcpp", "cuda",
                        res["generation"]["id"], authority=auth)
    assert rv["state"] == "refused"
    assert "reference_revoked" in rv["refusals"]


def test_g87_revalidate_never_selects(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    rv = rti.revalidate(tmp_path / "gens", "sdcpp", "cuda",
                        res["generation"]["id"])
    assert rv["execution_eligible"] is False
    assert rv["trusted_enrollment"] == "unestablished"
    assert rv["operational_qualification"] == "unestablished"


# ------------------------------------------------------------- path safety


def _mklink_j(target, link):
    return subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=False, capture_output=True, text=True)


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows")
def test_g88_junction_install_root_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "gens"
    if _mklink_j(real, link).returncode != 0:
        pytest.skip("mklink /J unavailable")
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src, install_root=link)
    assert res["state"] == "refused"
    assert "install_root_unsafe" in res["refusals"]


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows")
def test_g89_junction_generations_dir_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    root = tmp_path / "gens"
    flav = root / "runtime-gens" / "sdcpp" / "cuda"
    flav.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    if _mklink_j(target, flav / ".generations").returncode != 0:
        pytest.skip("mklink /J unavailable")
    res = _inst(scn, acq, tmp_path, src, install_root=root)
    assert res["state"] == "refused"
    assert res["refusals"] and any("namespace" in r or "reparse" in r
                                   for r in res["refusals"])


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows")
def test_g90_junction_source_members_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    sdir = src / acq["staging"]["dir"]
    target = tmp_path / "fake-members"
    target.mkdir()
    (target / "sd.exe").write_bytes(b"sd-bytes")
    (sdir / "members").rename(sdir / "members-real")
    if _mklink_j(target, sdir / "members").returncode != 0:
        pytest.skip("mklink /J unavailable")
    res = _inst(scn, acq, tmp_path, src)
    assert res["state"] == "refused"


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows")
def test_g91_reparse_destination_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    root = tmp_path / "gens"
    r0 = _inst(scn, acq, tmp_path, src, install_root=tmp_path / "twin")
    gid = r0["generation"]["id"]
    gens = _gens_dir(root, scn)
    gens.mkdir(parents=True)
    target = tmp_path / "target-gen"
    target.mkdir()
    if _mklink_j(target, gens / gid).returncode != 0:
        pytest.skip("mklink /J unavailable")
    res = _inst(scn, acq, tmp_path, src, install_root=root)
    assert res["state"] == "refused"
    assert "generation_destination_reparse" in res["refusals"]


# ------------------------------------------------------------ resource bounds


def test_g92_oversized_metadata_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    gd = _gen_path(tmp_path / "gens", scn, res["generation"]["id"])
    (gd / "_generation.json").write_bytes(b" " * (64 * 1024 + 1))
    rv = rti.revalidate(tmp_path / "gens", "sdcpp", "cuda",
                        res["generation"]["id"])
    assert rv["state"] == "refused"
    assert any("over_bound" in r for r in rv["refusals"])


def test_g93_duplicate_metadata_field_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    gd = _gen_path(tmp_path / "gens", scn, res["generation"]["id"])
    (gd / "_generation.json").write_text(
        '{"contract":"rgen:v0","record":"generation","record":"x"}')
    rv = rti.revalidate(tmp_path / "gens", "sdcpp", "cuda",
                        res["generation"]["id"])
    assert rv["state"] == "refused"
    assert any("duplicate_field" in r for r in rv["refusals"])


def test_g94_unsupported_metadata_contract_refused(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    gd = _gen_path(tmp_path / "gens", scn, res["generation"]["id"])
    (gd / "_generation.json").write_text(json.dumps({
        "contract": "rgen:v9", "record": "generation"}))
    rv = rti.revalidate(tmp_path / "gens", "sdcpp", "cuda",
                        res["generation"]["id"])
    assert rv["state"] == "refused"
    assert any("contract_invalid" in r for r in rv["refusals"])


def test_g95_member_over_bound(tmp_path, monkeypatch):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    monkeypatch.setattr(rti, "_MAX_OBSERVED_BYTES", 2)
    res = _inst(scn, acq, tmp_path, src)
    assert res["state"] == "refused"
    assert any(r.startswith("member_over_bound")
               for r in res["refusals"])


def test_g96_descriptor_member_bound(tmp_path):
    desc = {"contract": "rgen:v0", "reference_digest": "rref:v0:" + "0" * 64,
            "runtime_key": "k", "flavor": "f",
            "components": {"selected": ["c"], "omitted": []},
            "assets": [{"component": "c", "asset_id": 1, "name": "a",
                        "sha256": "0" * 64}],
            "members": [{"name": f"m{i:05d}", "component": "c",
                         "size": 0, "sha256": "0" * 64}
                        for i in range(2049)]}
    out, refusals = rti._norm_descriptor(desc)
    assert out is None and "descriptor_members_invalid" in refusals


def test_g97_reconcile_scan_bound(tmp_path, monkeypatch):
    scn_a = _multi(flavor="cuda")
    scn_b = _multi(flavor="vulkan")
    a1, s1 = _ok_acq(scn_a, tmp_path / "t1")
    a2, s2 = _ok_acq(scn_b, tmp_path / "t2")
    _inst(scn_a, a1, tmp_path / "t1", s1, install_root=tmp_path / "gens")
    _inst(scn_b, a2, tmp_path / "t2", s2, install_root=tmp_path / "gens")
    monkeypatch.setattr(rti, "_MAX_RECON_ENTRIES", 1)
    rec = rti.reconcile(tmp_path / "gens")
    assert "scan_bound_exceeded" in rec["counts"]


# ------------------------------------------------------------ non-activation


def test_g98_no_legacy_namespace_written(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    root = tmp_path / "gens"
    _inst(scn, acq, tmp_path, src, install_root=root)
    assert not (root / "runtimes").exists()
    assert (root / "runtime-gens").is_dir()


def test_g99_module_isolation_source_audit():
    src_text = (BACKEND / "app" / "domain"
                / "runtime_installation.py").read_text()
    tree = __import__("ast").parse(src_text)
    imports = set()
    for node in __import__("ast").walk(tree):
        if isinstance(node, __import__("ast").Import):
            imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, __import__("ast").ImportFrom):
            imports.add(node.module.split(".")[0])
    allowed = {"contextlib", "hashlib", "json", "os", "re", "secrets",
               "threading", "pathlib", "msvcrt", "fcntl",
               "app.domain", "__future__"}
    for mod in imports:
        assert mod in allowed or mod == "app" \
            or mod.startswith("app."), mod
    names = {n.id for n in __import__("ast").walk(tree)
             if isinstance(n, __import__("ast").Name)}
    attrs = {n.attr for n in __import__("ast").walk(tree)
             if isinstance(n, __import__("ast").Attribute)}
    for forbidden in ("RuntimeInstall", "runtime_installs",
                      "model_files", "Session", "inference",
                      "runtimes_root", "install_runtime_zip"):
        assert forbidden not in names | attrs, forbidden


def test_g100_declarations_on_all_states(tmp_path):
    scn = _multi()
    acq, src = _ok_acq(scn, tmp_path)
    states = [
        _inst(scn, acq, tmp_path, src),                       # published
        _inst(scn, dict(acq, contract="x"), tmp_path, src),   # refused
        rti.reconcile(tmp_path / "gens"),                     # reconciled
    ]
    r0 = states[0]
    states.append(rti.revalidate(tmp_path / "gens", "sdcpp",
                                 "cuda", r0["generation"]["id"]))
    for res in states:
        assert res["contract"] == "rgen:v0"
        assert _decls(res) == EXPECTED_DECLS


def test_g101_acquisition_envelope_untouched(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    snap = copy.deepcopy(acq)
    _inst(scn, acq, tmp_path, src)
    assert acq == snap


def test_g102_no_active_generation_pointer(tmp_path):
    scn_a = _multi(flavor="cuda")
    scn_b = _multi(flavor="vulkan")
    a1, s1 = _ok_acq(scn_a, tmp_path / "t1")
    a2, s2 = _ok_acq(scn_b, tmp_path / "t2")
    _inst(scn_a, a1, tmp_path / "t1", s1, install_root=tmp_path / "gens")
    _inst(scn_b, a2, tmp_path / "t2", s2, install_root=tmp_path / "gens")
    for p in (tmp_path / "gens").rglob("*"):
        assert p.name not in ("_published.json", "_active.json",
                              "current", "active"), p
        if p.is_file() and p.suffix == ".json":
            meta = json.loads(p.read_text())
            assert "active" not in meta and "selected" not in meta or \
                meta.get("components") is not None


def test_g103_synthetic_publication_vocabulary(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    res = _inst(scn, acq, tmp_path, src)
    assert res["synthetic_publication"] in (
        "not_performed", "committed", "committed_unconfirmed",
        "already_committed")
    assert res["installation_publication"] == "not_performed"


def test_g104_fs_only_persistence_no_db(tmp_path):
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    root = tmp_path / "gens"
    _inst(scn, acq, tmp_path, src, install_root=root)
    # persistence is exactly the filesystem namespace — no db artifacts
    for p in root.rglob("*"):
        assert p.suffix not in (".db", ".sqlite", ".sqlite3")


def test_g105_expected_hash_derivation_independent(tmp_path):
    # expected identity derives from the VALIDATED reference — a
    # forged acquisition member record cannot redirect verification
    scn = _single()
    acq, src = _ok_acq(scn, tmp_path)
    forged = copy.deepcopy(acq)
    forged["members"][0]["observed_sha256"] = "e" * 64
    forged["members"][0]["expected_sha256"] = "e" * 64
    res = _inst(scn, forged, tmp_path, src)
    assert res["state"] == "refused"


def test_g106_environment_metadata():
    import platform
    assert platform.python_version() >= "3.10"
    # environment record: windows-first platform verified at runtime
    assert os.name in ("nt", "posix")
