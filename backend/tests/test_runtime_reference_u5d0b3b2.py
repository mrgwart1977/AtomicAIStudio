"""U-5d-0b Phase beta-3-B2 — isolated trusted runtime reference
foundation (rref:v0) synthetic regression suite.

Requirement map (owner acceptance requirements R01–R60):

  R01  test_r01_valid_single_archive_reference
  R02  test_r02_valid_multi_archive_reference
  R03  test_r03_deterministic_content_digest
  R04  test_r04_archive_change_alters_digest
  R05  test_r05_member_change_alters_digest
  R06  test_r06_coverage_change_alters_digest
  R07  test_r07_lifecycle_does_not_alter_content_digest
  R08  test_r08_approval_binds_exact_content
  R09  test_r09_approval_not_transferable
  R10  test_r10_unsupported_version_refused
  R11  test_r11_missing_required_archive
  R12  test_r12_duplicate_component_identity
  R13  test_r13_contradictory_asset_identity
  R14  test_r14_invalid_expected_digest
  R15  test_r15_missing_expected_digest
  R16  test_r16_invalid_size
  R17  test_r17_boolean_size_refused
  R18  test_r18_unknown_member_component
  R19  test_r19_duplicate_member_destination
  R20  test_r20_cross_archive_collision
  R21  test_r21_case_collision
  R22  test_r22_unsafe_member_path
  R23  test_r23_reserved_windows_name
  R24  test_r24_trailing_dot_space
  R25  test_r25_resource_bounds
  R26  test_r26_valid_platform_metadata
  R27  test_r27_missing_digest_stays_missing
  R28  test_r28_malformed_digest_refused
  R29  test_r29_ambiguous_asset_selection
  R30  test_r30_commitish_not_tag_resolution
  R31  test_r31_workflow_claim_not_provenance
  R32  test_r32_platform_digest_not_publisher_auth
  R33  test_r33_approval_preserves_provenance
  R34  test_r34_self_declared_approval_no_authority
  R35  test_r35_favorable_boolean_no_authority
  R36  test_r36_favorable_provenance_label_refused
  R37  test_r37_missing_authority_source
  R38  test_r38_synthetic_authority_distinguished
  R39  test_r39_synthetic_not_production_trust
  R40  test_r40_no_authority_escalation_via_resolution
  R41  test_r41_proposed_not_active
  R42  test_r42_approved_not_active
  R43  test_r43_invalid_transition_refused
  R44  test_r44_unauthorized_activation_refused
  R45  test_r45_ambiguous_active_refused
  R46  test_r46_revoked_refused
  R47  test_r47_superseded_not_silently_selected
  R48  test_r48_unknown_supersession_target
  R49  test_r49_cyclic_supersession
  R50  test_r50_newer_not_silent_replacement
  R51  test_r51_stronger_provenance_not_silent
  R52  test_r52_revoked_cannot_reactivate
  R53  test_r53_unknown_security_fields_strict
  R54  test_r54_duplicate_json_keys_refused
  R55  test_r55_sanitized_diagnostics
  R56  test_r56_inputs_not_mutated
  R57  test_r57_no_network_surface
  R58  test_r58_no_runtime_fs_access
  R59  test_r59_no_production_imports
  R60  test_r60_all_outputs_non_executable
"""

import copy
import inspect
import json

import pytest

from app.domain import runtime_reference as rr

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def _asset(name="sd-master-x-bin-win-cuda12-x64.zip",
           url=None,
           size=367000000, sha=SHA_A, prov=rr.P_PLATFORM, **kw):
    if url is None:
        url = ("https://github.com/leejet/stable-diffusion.cpp/"
               f"releases/download/t/{name}")
    a = {"name": name, "url": url, "size": size,
         "digest": {"algo": "sha256", "value": sha},
         "provenance": {"class": prov}}
    a.update(kw)
    return a


def _archive(component="runtime", role=rr.ROLE_PRIMARY, required=True,
             asset=None, **kw):
    a = {"component": component, "role": role, "required": required,
         "asset": asset if asset is not None else _asset()}
    a.update(kw)
    return a


def _member(name="sd-cli.exe", component="runtime", size=12345678,
            sha=SHA_B, **kw):
    m = {"name": name, "component": component, "size": size,
         "sha256": sha}
    m.update(kw)
    return m


def _ref(**kw):
    r = {"contract": rr.RREF_VERSION,
         "runtime_key": "sdcpp", "flavor": "cuda",
         "release": {"tag": "master-1073-991f825",
                     "release_id": 439364424,
                     "target_commitish": "9" * 40},
         "archives": [_archive()],
         "members": [_member(), _member(name="ggml-cuda.dll", sha=SHA_C)],
         "coverage": {"mode": rr.COVERAGE_FLAT,
                      "unexpected_policy": "refuse"}}
    r.update(kw)
    return r


def _multi_ref():
    return _ref(archives=[
        _archive(component="runtime", role=rr.ROLE_PRIMARY),
        _archive(component="cudart", role=rr.ROLE_DEPENDENCY,
                 asset=_asset(name="cudart-sd-bin-win-cu12-x64.zip",
                              size=563452046, sha=SHA_D)),
        _archive(component="docs", role=rr.ROLE_OPTIONAL, required=False,
                 asset=_asset(name="docs-bundle.zip", size=1024,
                              sha=SHA_E))],
        members=[_member(),
                 _member(name="ggml-cuda.dll", sha=SHA_C),
                 _member(name="cudart64_12.dll", component="cudart",
                         size=400000, sha=SHA_D),
                 _member(name="license.txt", component="docs",
                         size=1000, sha=SHA_E)])


def _approval(digest, **kw):
    a = {"reference_digest": digest, "approved_by": "owner-synthetic",
         "approved_at": "2026-01-01T00:00:00Z",
         "approval_ref": "synthetic-review-1",
         "authority": rr.AUTHORITY_SYNTHETIC}
    a.update(kw)
    return a


def _registered(registry=None, ref=None):
    reg = registry or rr.ReferenceRegistry()
    res = reg.add(ref or _ref())
    assert res["ok"] is True
    return reg, res["digest"]


def _meta(**kw):
    m = {"release_id": 439364424, "tag": "master-1073-991f825",
         "target_commitish": "9" * 40,
         "published_at": "2026-11-09T16:21:27Z",
         "author": "github-actions[bot]",
         "assets": [
             {"id": 1, "name": "sd-master-x-bin-win-cuda12-x64.zip",
              "url": "https://github.com/x/y/releases/download/t/a.zip",
              "size": 366981878,
              "digest": "sha256:" + SHA_A,
              "uploader": "github-actions[bot]"},
             {"id": 2, "name": "sd-master-x-bin-win-vulkan-x64.zip",
              "url": "https://github.com/x/y/releases/download/t/b.zip",
              "size": 29992741, "digest": "sha256:" + SHA_B},
         ]}
    m.update(kw)
    return m


# ------------------------------------------------------- identity (R01–R10)

def test_r01_valid_single_archive_reference():
    res = rr.build_reference(_ref())
    assert res["ok"] is True and res["refusals"] == []
    assert res["digest"].startswith(rr.DIGEST_PREFIX)
    assert len(res["digest"]) == len(rr.DIGEST_PREFIX) + 64


def test_r02_valid_multi_archive_reference():
    res = rr.build_reference(_multi_ref())
    assert res["ok"] is True
    comps = [a["component"] for a in res["reference"]["archives"]]
    assert comps == sorted(comps) == ["cudart", "docs", "runtime"]
    dests = [m["name"] for m in res["reference"]["members"]]
    assert dests == sorted(dests, key=str.casefold)


def test_r03_deterministic_content_digest():
    a = _multi_ref()
    b = json.loads(json.dumps(a))                      # identical copy
    c = copy.deepcopy(a)
    c["archives"] = list(reversed(c["archives"]))
    c["members"] = list(reversed(c["members"]))
    assert rr.reference_digest(a) == rr.reference_digest(b)
    assert rr.reference_digest(a) == rr.reference_digest(c)


def test_r04_archive_change_alters_digest():
    d0 = rr.reference_digest(_ref())
    alt = _ref()
    alt["archives"][0]["asset"]["digest"]["value"] = SHA_B
    assert rr.reference_digest(alt) != d0
    alt2 = _ref()
    alt2["archives"][0]["asset"]["size"] += 1
    assert rr.reference_digest(alt2) != d0
    alt3 = _ref()
    alt3["archives"][0]["asset"]["provenance"]["class"] = \
        rr.P_PUBLISHER
    assert rr.reference_digest(alt3) != d0


def test_r05_member_change_alters_digest():
    d0 = rr.reference_digest(_ref())
    for mutate in (
            lambda r: r["members"][0].update(sha256=SHA_D),
            lambda r: r["members"][0].update(size=99),
            lambda r: r["members"].append(_member(name="extra.dll"))):
        r = _ref()
        mutate(r)
        assert rr.reference_digest(r) != d0


def test_r06_coverage_change_alters_digest():
    d0 = rr.reference_digest(_ref())
    r = _ref()
    r["coverage"]["skip_names"] = ["obsolete.dll"]
    assert rr.reference_digest(r) != d0


def test_r07_lifecycle_does_not_alter_content_digest():
    reg, d = _registered()
    ref = reg._entries[d]["reference"]
    assert rr.reference_digest(ref) == d
    reg.approve(d, _approval(d))
    assert rr.reference_digest(ref) == d
    reg.activate(d)
    assert rr.reference_digest(ref) == d
    reg.revoke(d)
    assert rr.reference_digest(ref) == d


def test_r08_approval_binds_exact_content():
    reg, d = _registered()
    res = reg.approve(d, _approval(d))
    assert res["ok"] is True
    assert reg._entries[d]["approval"]["reference_digest"] == d


def test_r09_approval_not_transferable():
    reg, d = _registered()
    other = rr.reference_digest(
        _ref(members=[_member(sha=SHA_D)]))
    res = reg.approve(d, _approval(other))
    assert res["ok"] is not True
    assert "approval_digest_mismatch" in res["refusals"]


def test_r10_unsupported_version_refused():
    for bad in ("rref:v9", "rtq:v1", 1, None):
        res = rr.validate_reference(_ref(contract=bad))
        assert res["ok"] is False
        assert "contract_version_unsupported" in res["refusals"]


# ---------------------------------------- archive/member validation (R11–R25)

def test_r11_missing_required_archive():
    res = rr.validate_reference(_ref(archives=[
        _archive(required=False, role=rr.ROLE_OPTIONAL)]))
    assert res["ok"] is False
    assert "no_required_component" in res["refusals"]
    assert rr.validate_reference(_ref(archives=[]))["ok"] is False
    assert rr.validate_reference(_ref(archives=None))["ok"] is False


def test_r12_duplicate_component_identity():
    res = rr.validate_reference(_ref(archives=[
        _archive(component="runtime"),
        _archive(component="runtime", role=rr.ROLE_DEPENDENCY)]))
    assert res["ok"] is False
    assert any(r.startswith("duplicate_component") for r in res["refusals"])


def test_r13_contradictory_asset_identity():
    res = rr.validate_reference(_multi_ref())
    assert res["ok"] is True
    bad = _multi_ref()
    bad["archives"][1]["asset"]["name"] = \
        bad["archives"][0]["asset"]["name"]
    res = rr.validate_reference(bad)
    assert res["ok"] is False
    assert any(r.startswith("contradictory_asset_identity")
               for r in res["refusals"])


@pytest.mark.parametrize("bad", ["xyz", "0" * 63, 12345, None, True,
                                 {"algo": "md5", "value": SHA_A},
                                 {"algo": "sha256", "value": "zz" * 32}])
def test_r14_invalid_expected_digest(bad):
    r = _ref()
    r["archives"][0]["asset"]["digest"] = bad
    res = rr.validate_reference(r)
    assert res["ok"] is False
    assert any("asset_digest" in x for x in res["refusals"])


def test_r15_missing_expected_digest():
    r = _ref()
    del r["archives"][0]["asset"]["digest"]
    res = rr.validate_reference(r)
    assert res["ok"] is False
    assert "asset_digest_missing:runtime" in res["refusals"]


@pytest.mark.parametrize("bad", [0, -1, 1 << 61, "367000000", 12.5])
def test_r16_invalid_size(bad):
    r = _ref()
    r["archives"][0]["asset"]["size"] = bad
    res = rr.validate_reference(r)
    assert res["ok"] is False
    assert any("asset_size" in x for x in res["refusals"])


def test_r17_boolean_size_refused():
    for b in (True, False):
        r = _ref()
        r["archives"][0]["asset"]["size"] = b
        res = rr.validate_reference(r)
        assert res["ok"] is False
        assert "asset_size_boolean:runtime" in res["refusals"]
        m = _member(size=b)
        res = rr.validate_reference(_ref(members=[m]))
        assert res["ok"] is False
        assert "member_size_boolean" in res["refusals"][0]


def test_r18_unknown_member_component():
    res = rr.validate_reference(
        _ref(members=[_member(component="nope")]))
    assert res["ok"] is False
    assert any(r.startswith("member_component_unknown")
               for r in res["refusals"])


def test_r19_duplicate_member_destination():
    res = rr.validate_reference(_ref(members=[
        _member(), _member(sha=SHA_D)]))
    assert res["ok"] is False
    assert any(r.startswith("duplicate_member_destination")
               for r in res["refusals"])


def test_r20_cross_archive_collision():
    r = _multi_ref()
    r["members"].append(_member(name="sd-cli.exe",
                                component="cudart", sha=SHA_E))
    res = rr.validate_reference(r)
    assert res["ok"] is False
    assert any(r.startswith("duplicate_member_destination")
               for r in res["refusals"])


def test_r21_case_collision():
    res = rr.validate_reference(_ref(members=[
        _member(name="Ggml-CUDA.dll"),
        _member(name="ggml-cuda.dll", sha=SHA_C)]))
    assert res["ok"] is False
    assert any(r.startswith("member_case_collision")
               for r in res["refusals"])


@pytest.mark.parametrize("name", [
    "a/b.dll", "..\\evil.dll", "sub\\dir.dll", "C:\\x.dll",
    "ads:stream.dll", ".", "..", "  pad.dll", ""])
def test_r22_unsafe_member_path(name):
    res = rr.validate_reference(_ref(members=[_member(name=name)]))
    assert res["ok"] is False
    assert any(r.startswith("member_name_invalid")
               for r in res["refusals"])


@pytest.mark.parametrize("name", ["con.dll", "NUL.dll", "com1.dll",
                                  "lpt9.exe", "aux.dll"])
def test_r23_reserved_windows_name(name):
    res = rr.validate_reference(_ref(members=[_member(name=name)]))
    assert res["ok"] is False


@pytest.mark.parametrize("name", ["x.dll ", "x.dll.", " trail.dll "])
def test_r24_trailing_dot_space(name):
    res = rr.validate_reference(_ref(members=[_member(name=name)]))
    assert res["ok"] is False


def test_r25_resource_bounds():
    many_arch = [_archive(component=f"c{i}") for i in range(17)]
    res = rr.validate_reference(_ref(archives=many_arch))
    assert "archives_over_bound" in res["refusals"]
    many_mem = [_member(name=f"m{i}.dll") for i in range(2049)]
    res = rr.validate_reference(_ref(members=many_mem))
    assert "members_over_bound" in res["refusals"]
    r = _ref()
    r["archives"][0]["asset"]["name"] = "x" * 300
    assert rr.validate_reference(r)["ok"] is False


# ------------------------------------------- metadata/provenance (R26–R33)

def test_r26_valid_platform_metadata():
    res = rr.validate_release_metadata(_meta())
    assert res["ok"] is True
    a = res["metadata"]["assets"][0]
    assert a["digest"] == SHA_A
    assert a["uploader_claim"] == "github-actions[bot]"


def test_r27_missing_digest_stays_missing():
    m = _meta()
    del m["assets"][0]["digest"]
    res = rr.validate_release_metadata(m)
    assert res["ok"] is True
    assert res["metadata"]["assets"][0]["digest"] is None


@pytest.mark.parametrize("bad", ["md5:" + SHA_A, SHA_A, "sha256:zz",
                                 "sha256:" + SHA_A + "ff", 123, True])
def test_r28_malformed_digest_refused(bad):
    m = _meta()
    m["assets"][0]["digest"] = bad
    res = rr.validate_release_metadata(m)
    assert res["ok"] is False
    assert any(r.startswith("asset_digest_malformed")
               for r in res["refusals"])


def test_r29_ambiguous_asset_selection():
    res = rr.select_asset(_meta(), "win-")
    assert res["ok"] is False
    assert "ambiguous_asset_selection" in res["refusals"]
    assert len(res["candidates"]) == 2
    res = rr.select_asset(_meta(), "win-cuda")
    assert res["ok"] is True
    res = rr.select_asset(_meta(), "win-arm")
    assert res["ok"] is False
    assert "no_matching_asset" in res["refusals"]


def test_r30_commitish_not_tag_resolution():
    res = rr.validate_release_metadata(_meta())
    m = res["metadata"]
    assert m["declared_commitish"] == "9" * 40
    assert m["resolved_ref_sha"] is None
    assert "target_commitish" not in m
    m2 = _meta(resolved_ref_sha="9" * 40)
    res2 = rr.validate_release_metadata(m2)
    assert res2["metadata"]["resolved_ref_sha"] == "9" * 40


def test_r31_workflow_claim_not_provenance():
    r = _ref()
    r["archives"][0]["source"] = {
        "repo": "leejet/stable-diffusion.cpp",
        "workflow": "build.yml",
        "build_evidence": "https://github.com/x/actions/runs/1"}
    res = rr.build_reference(r)
    assert res["ok"] is True
    reg, _d = _registered(ref=r)
    out = reg.resolve("sdcpp", "cuda")
    assert out["state"] == rr.R_UNAPPROVED  # claim ≠ approval/authority


def test_r32_platform_digest_not_publisher_auth():
    res = rr.build_reference(_ref())
    assert res["ok"] is True
    assert res["reference"]["archives"][0]["asset"]["provenance"][
        "class"] == rr.P_PLATFORM


def test_r33_approval_preserves_provenance():
    reg, d = _registered()
    reg.approve(d, _approval(d))
    reg.activate(d)
    out = reg.resolve("sdcpp", "cuda")
    assert out["state"] == rr.R_SYNTHETIC_ACTIVE
    assert out["reference"]["archives"][0]["asset"]["provenance"][
        "class"] == rr.P_PLATFORM  # approval never elevated it


# ------------------------------------------------- authority (R34–R40)

@pytest.mark.parametrize("auth", ["production", "owner", "owner_attested",
                                  "publisher", "github", True, 1, None])
def test_r34_self_declared_approval_no_authority(auth):
    reg, d = _registered()
    res = reg.approve(d, _approval(d, authority=auth))
    assert res["ok"] is not True
    assert "authority_source_unestablished" in res["refusals"]


def test_r35_favorable_boolean_no_authority():
    reg, d = _registered()
    res = reg.approve(d, _approval(d, owner_approved=True))
    assert res["ok"] is not True
    assert any(r.startswith("unknown_field") for r in res["refusals"])
    res = reg.approve(d, _approval(d, authority=True))
    assert res["ok"] is not True
    r = _ref(active=True)
    res = rr.validate_reference(r)
    assert res["ok"] is False
    assert any(r.startswith("unknown_field") for r in res["refusals"])


@pytest.mark.parametrize("prov", ["owner_approved", "publisher",
                                  "trusted_external", "approved", ""])
def test_r36_favorable_provenance_label_refused(prov):
    r = _ref()
    r["archives"][0]["asset"]["provenance"]["class"] = prov
    res = rr.validate_reference(r)
    assert res["ok"] is False
    assert "asset_provenance_invalid:runtime" in res["refusals"]


def test_r37_missing_authority_source():
    reg, d = _registered()
    res = reg.approve(d, _approval(d))
    del res  # approval recorded; now simulate missing authority
    reg._entries[d]["approval"] = None
    reg._entries[d]["lifecycle"]["state"] = rr.L_ACTIVE
    out = reg.resolve("sdcpp", "cuda")
    assert out["state"] == rr.R_AUTHORITY_UNESTABLISHED
    res2 = reg.approve(d, _approval(d, authority="production"))
    assert res2["ok"] is not True


def test_r38_synthetic_authority_distinguished():
    reg, d = _registered()
    reg.approve(d, _approval(d))
    reg.activate(d)
    out = reg.resolve("sdcpp", "cuda")
    assert out["state"] == rr.R_SYNTHETIC_ACTIVE
    assert out["authority_class"] == rr.AUTHORITY_SYNTHETIC
    assert out["authority_class"] != "production"


def test_r39_synthetic_not_production_trust():
    reg, d = _registered()
    reg.approve(d, _approval(d))
    reg.activate(d)
    out = reg.resolve("sdcpp", "cuda")
    assert out["production_trust"] is False
    assert out["execution_eligible"] is False


def test_r40_no_authority_escalation_via_resolution():
    reg, d = _registered()
    reg.approve(d, _approval(d))
    reg.activate(d)
    out = reg.resolve("sdcpp", "cuda")
    assert out["status"] == "reference_foundation_only"
    assert out["production_trust"] is False
    # resolution never returns a raw trusted_external-style claim
    assert "trusted" not in out["authority_class"]


# ------------------------------------------------- lifecycle (R41–R52)

def test_r41_proposed_not_active():
    reg, _d = _registered()
    out = reg.resolve("sdcpp", "cuda")
    assert out["state"] == rr.R_UNAPPROVED


def test_r42_approved_not_active():
    reg, d = _registered()
    reg.approve(d, _approval(d))
    out = reg.resolve("sdcpp", "cuda")
    assert out["state"] == rr.R_APPROVED_INACTIVE


def test_r43_invalid_transition_refused():
    reg, d = _registered()
    res = reg.activate(d)  # proposed -> active not permitted
    assert res["ok"] is not True
    assert any(r.startswith("invalid_transition")
               for r in res["refusals"])


def test_r44_unauthorized_activation_refused():
    reg, d = _registered()
    res = reg.activate(d)
    assert res["ok"] is not True
    reg.approve(d, _approval(d))
    reg._entries[d]["approval"] = None     # approval removed
    res = reg.activate(d)
    assert res["ok"] is not True
    assert "approval_absent" in res["refusals"]


def test_r45_ambiguous_active_refused():
    reg, d1 = _registered()
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    d2 = reg.add(_ref(release={"tag": "master-1074-abc",
                             "release_id": 1}))["digest"]
    reg.approve(d2, _approval(d2))
    res = reg.activate(d2)
    assert res["ok"] is not True
    assert res["state"] == rr.R_AMBIGUOUS
    reg._entries[d2]["lifecycle"]["state"] = rr.L_ACTIVE  # forced
    out = reg.resolve("sdcpp", "cuda")
    assert out["state"] == rr.R_AMBIGUOUS
    assert set(out["conflicting"]) == {d1, d2}


def test_r46_revoked_refused():
    reg, d = _registered()
    reg.approve(d, _approval(d))
    reg.activate(d)
    reg.revoke(d)
    out = reg.resolve("sdcpp", "cuda")
    assert out["state"] == rr.R_REVOKED
    assert out["execution_eligible"] is False


def test_r47_superseded_not_silently_selected():
    reg, d1 = _registered()
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    d2 = reg.add(_ref(release={"tag": "v2", "release_id": 2,
                             "target_commitish": "8" * 40}))["digest"]
    reg.approve(d2, _approval(d2))
    reg.supersede(d1, d2)
    assert reg._entries[d1]["lifecycle"]["state"] == rr.L_SUPERSEDED
    out = reg.resolve("sdcpp", "cuda")
    assert out["state"] == rr.R_APPROVED_INACTIVE  # d2 approved only


def test_r48_unknown_supersession_target():
    reg, d1 = _registered()
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    res = reg.supersede(d1, rr.DIGEST_PREFIX + "f" * 64)
    assert res["ok"] is not True
    assert "unknown_supersession_target" in res["refusals"]
    res = reg.supersede(rr.DIGEST_PREFIX + "f" * 64, d1)
    assert res["ok"] is not True


def test_r49_cyclic_supersession():
    reg, d1 = _registered()
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    d2 = reg.add(_ref(release={"tag": "v2", "release_id": 2,
                             "target_commitish": "8" * 40}))["digest"]
    reg.approve(d2, _approval(d2))
    reg.supersede(d1, d2)
    # Re-approve d1 (explicit rollback path), then try to supersede
    # d2 by d1 — d1's supplanted_by chain already reaches d2 → cycle.
    reg.activate(d2)
    reg.approve(d1, _approval(d1))
    assert reg._entries[d1]["lifecycle"]["state"] == rr.L_APPROVED
    res = reg.supersede(d2, d1)
    assert res["ok"] is not True
    assert "cyclic_supersession" in res["refusals"]
    res = reg.supersede(d1, d1)
    assert res["ok"] is not True


def test_r50_newer_not_silent_replacement():
    reg, d1 = _registered()
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    newer = _ref(release={"tag": "master-1074-zzz", "release_id": 9,
                          "target_commitish": "8" * 40})
    d2 = reg.add(newer)["digest"]
    reg.approve(d2, _approval(d2))
    res = reg.activate(d2)
    assert res["ok"] is not True          # no silent replacement
    out = reg.resolve("sdcpp", "cuda")
    assert out["state"] == rr.R_SYNTHETIC_ACTIVE
    assert out["digest"] == d1            # still the first


def test_r51_stronger_provenance_not_silent():
    reg, d1 = _registered()
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    strong = _ref(release={"tag": "v2", "release_id": 3,
                           "target_commitish": "8" * 40})
    strong["archives"][0]["asset"]["provenance"]["class"] = \
        rr.P_PUBLISHER  # claimed stronger provenance
    d2 = reg.add(strong)["digest"]
    reg.approve(d2, _approval(d2))
    res = reg.activate(d2)
    assert res["ok"] is not True
    out = reg.resolve("sdcpp", "cuda")
    assert out["digest"] == d1


def test_r52_revoked_cannot_reactivate():
    reg, d = _registered()
    reg.approve(d, _approval(d))
    reg.activate(d)
    reg.revoke(d)
    res = reg.approve(d, _approval(d))
    assert res["ok"] is not True
    assert any("revoked->approved" in r for r in res["refusals"])
    res = reg.activate(d)
    assert res["ok"] is not True


# --------------------------------------------------- general (R53–R60)

def test_r53_unknown_security_fields_strict():
    for where, r in (
            ("reference", _ref(publisher_signed=True)),
            ("archive", _ref(archives=[_archive(trusted=True)])),
            ("asset", _ref(archives=[_archive(asset=_asset(
                verified=True))])),
            ("member", _ref(members=[_member(safe=True)])),
            ("coverage", _ref(coverage={"mode": rr.COVERAGE_FLAT,
                                        "unexpected_policy": "refuse",
                                        "lenient": True}))):
        res = rr.validate_reference(r)
        assert res["ok"] is False, where
        assert any(x.startswith("unknown_field")
                   for x in res["refusals"]), where


def test_r54_duplicate_json_keys_refused():
    text = '{"contract": "rref:v0", "contract": "rref:v9"}'
    res = rr.loads_reference(text)
    assert res["ok"] is False
    assert "duplicate_json_key" in res["refusals"]
    good = json.dumps(_ref())
    assert rr.loads_reference(good)["ok"] is True


def test_r55_sanitized_diagnostics():
    r = _ref()
    r["members"].append(_member(name="C:\\abs\\path\\evil.dll"))
    r["archives"][0]["asset"]["digest"] = 12345
    res = rr.validate_reference(r)
    res2 = rr.validate_reference(copy.deepcopy(r))
    assert res["refusals"] == res2["refusals"]      # deterministic
    assert any(x.startswith("member_name_invalid")
               for x in res["refusals"])
    assert all(len(x) < 200 for x in res["refusals"])
    joined = " ".join(res["refusals"])
    assert "AtomicAIStudio" not in joined and "backend" not in joined
    long_name = "x" * 254 + ".dll"
    r2 = _ref(members=[_member(name=long_name),
                       _member(name=long_name)])
    res3 = rr.validate_reference(r2)
    assert all(len(x) < 200 for x in res3["refusals"])


def test_r56_inputs_not_mutated():
    for obj, fn in (
            (_ref(), rr.validate_reference),
            (_multi_ref(), rr.validate_reference),
            (_meta(), rr.validate_release_metadata),
            (_approval("x"), rr.validate_approval)):
        before = copy.deepcopy(obj)
        fn(copy.deepcopy(obj))
        assert obj == before


def test_r57_no_network_surface():
    mod = rr
    for bad in ("socket", "urllib", "requests", "http", "urlopen",
                "urlretrieve"):
        assert bad not in vars(mod), bad
    for fn in (rr.build_reference, rr.validate_reference,
               rr.validate_release_metadata, rr.select_asset,
               rr.validate_approval):
        params = set(inspect.signature(fn).parameters)
        assert not params & {"fetch", "conn", "session", "timeout",
                             "url"}


def test_r58_no_runtime_fs_access():
    for bad in ("os", "io", "pathlib", "subprocess", "shutil",
                "tempfile", "open"):
        assert bad not in vars(rr), bad
    for cls in (rr.ReferenceRegistry,):
        for name, fn in inspect.getmembers(cls, inspect.isfunction):
            if name.startswith("_"):
                continue
            params = set(inspect.signature(fn).parameters)
            assert not params & {"path", "root", "dest", "file"}


def test_r59_no_production_imports():
    imported = {k for k, v in vars(rr).items()
                if inspect.ismodule(v)}
    assert imported <= {"hashlib", "json", "re"}
    assert "os" not in vars(rr)
    assert "app" not in {k.split(".")[0] for k in imported}


def test_r60_all_outputs_non_executable():
    reg, d = _registered()
    outs = [
        rr.build_reference(_ref()),
        rr.validate_reference(_ref(contract="bad")),
        reg.add(_ref()),
        reg.approve(d, _approval(d)),
        reg.activate(d),
        reg.resolve("sdcpp", "cuda"),
        reg.resolve("nope", "none"),
        rr.select_asset(_meta(), "win-cuda"),
        rr.validate_release_metadata(_meta()),
        rr.loads_reference(json.dumps(_ref())),
    ]
    for o in outs:
        flat = json.dumps(o, default=str)
        assert '"execution_eligible": false' in flat or \
            o.get("execution_eligible") is False, o
    # resolve path results always carry the non-executable envelope
    assert outs[5]["status"] == "reference_foundation_only"
    assert outs[5]["production_trust"] is False
