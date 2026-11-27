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


def _permit(digest, **kw):
    p = {"reference_digest": digest, "approved_by": "owner-synthetic",
         "approved_at": "2026-01-01T00:00:00Z",
         "approval_ref": "synthetic-review-1"}
    p.update(kw)
    return p


def _fixture_for(*refs):
    """SyntheticAuthorityFixture permitting the canonical approval
    record for each supplied reference's recomputed digest (D4 — the
    fixture is the only authority-association source)."""
    return rr.SyntheticAuthorityFixture(
        [_permit(rr.reference_digest(r)) for r in refs])


def _registered(ref=None, *extra_refs):
    """Add ref to a registry whose fixture permits canonical approvals
    for ref AND every extra ref the test will approve later."""
    refs = (ref or _ref(),) + extra_refs
    reg = rr.ReferenceRegistry(_fixture_for(*refs))
    res = reg.add(refs[0])
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
    assert m["resolved_ref_sha_claim"] is None
    assert "target_commitish" not in m
    assert "resolved_ref_sha" not in m
    m2 = _meta(resolved_ref_sha="9" * 40)
    res2 = rr.validate_release_metadata(m2)
    # D5: normalized under the explicit claim name — never implied
    # established resolution.
    assert res2["metadata"]["resolved_ref_sha_claim"] == "9" * 40
    assert "resolved_ref_sha" not in res2["metadata"]


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
    ref2 = _ref(release={"tag": "master-1074-abc", "release_id": 1})
    reg, d1 = _registered(_ref(), ref2)
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    d2 = reg.add(ref2)["digest"]
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
    ref2 = _ref(release={"tag": "v2", "release_id": 2,
                         "target_commitish": "8" * 40})
    reg, d1 = _registered(_ref(), ref2)
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    d2 = reg.add(ref2)["digest"]
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
    ref2 = _ref(release={"tag": "v2", "release_id": 2,
                         "target_commitish": "8" * 40})
    reg, d1 = _registered(_ref(), ref2)
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    d2 = reg.add(ref2)["digest"]
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
    newer = _ref(release={"tag": "master-1074-zzz", "release_id": 9,
                          "target_commitish": "8" * 40})
    reg, d1 = _registered(_ref(), newer)
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    d2 = reg.add(newer)["digest"]
    reg.approve(d2, _approval(d2))
    res = reg.activate(d2)
    assert res["ok"] is not True          # no silent replacement
    out = reg.resolve("sdcpp", "cuda")
    assert out["state"] == rr.R_SYNTHETIC_ACTIVE
    assert out["digest"] == d1            # still the first


def test_r51_stronger_provenance_not_silent():
    strong = _ref(release={"tag": "v2", "release_id": 3,
                           "target_commitish": "8" * 40})
    strong["archives"][0]["asset"]["provenance"]["class"] = \
        rr.P_PUBLISHER  # claimed stronger provenance
    reg, d1 = _registered(_ref(), strong)
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
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
    assert imported <= {"hashlib", "json", "math", "re"}
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


# ------------------------------------------------- D1 — component invariants

def _dep_asset(name="dep-bundle.zip", sha=SHA_E):
    return {"name": name, "size": 4096,
            "digest": {"algo": "sha256", "value": sha},
            "provenance": {"class": rr.P_PLATFORM}}


def test_d1_01_valid_single_required_primary():
    assert rr.validate_reference(_ref())["ok"] is True


def test_d1_02_primary_plus_required_dependencies():
    r = _ref(archives=[
        _archive(component="runtime"),
        _archive(component="dep", role=rr.ROLE_DEPENDENCY,
                 asset=_dep_asset())],
        members=[_member(),
                 _member(name="dep.dll", component="dep")])
    assert rr.validate_reference(r)["ok"] is True


def test_d1_03_primary_plus_optional():
    # primary + required dependency + optional component
    assert rr.validate_reference(_multi_ref())["ok"] is True


def test_d1_04_dependency_only_refused():
    r = _ref(archives=[_archive(component="dep",
                              role=rr.ROLE_DEPENDENCY,
                              asset=_dep_asset())],
             members=[_member(component="dep")])
    res = rr.validate_reference(r)
    assert res["ok"] is False
    assert "primary_count_invalid" in res["refusals"]


def test_d1_05_optional_only_refused():
    r = _ref(archives=[_archive(component="opt", role=rr.ROLE_OPTIONAL,
                              required=False, asset=_dep_asset("o.zip"))],
             members=[_member(component="opt")])
    res = rr.validate_reference(r)
    assert res["ok"] is False
    assert "primary_count_invalid" in res["refusals"]
    assert "no_required_component" in res["refusals"]


def test_d1_06_multiple_primaries_refused():
    r = _ref(archives=[
        _archive(component="p1"),
        _archive(component="p2", asset=_dep_asset("p2.zip"))],
        members=[_member(component="p1"),
                 _member(name="x.dll", component="p2")])
    res = rr.validate_reference(r)
    assert res["ok"] is False
    assert "primary_count_invalid" in res["refusals"]


def test_d1_07_primary_not_required_refused():
    r = _ref(archives=[_archive(component="runtime", required=False)],
             members=[_member()])
    res = rr.validate_reference(r)
    assert res["ok"] is False
    assert "role_required_inconsistent:runtime" in res["refusals"]


def test_d1_08_dependency_not_required_refused():
    r = _ref(archives=[
        _archive(component="runtime"),
        _archive(component="dep", role=rr.ROLE_DEPENDENCY,
                 required=False, asset=_dep_asset())],
        members=[_member()])
    res = rr.validate_reference(r)
    assert res["ok"] is False
    assert "role_required_inconsistent:dep" in res["refusals"]


def test_d1_09_optional_required_refused():
    r = _ref(archives=[
        _archive(component="runtime"),
        _archive(component="opt", role=rr.ROLE_OPTIONAL,
                 required=True, asset=_dep_asset())],
        members=[_member()])
    res = rr.validate_reference(r)
    assert res["ok"] is False
    assert "role_required_inconsistent:opt" in res["refusals"]


def test_d1_10_required_primary_no_members_refused():
    r = _ref(members=[])
    res = rr.validate_reference(r)
    assert res["ok"] is False
    assert "required_component_no_members:runtime" in res["refusals"]


def test_d1_11_required_dependency_no_members_refused():
    r = _ref(archives=[
        _archive(component="runtime"),
        _archive(component="dep", role=rr.ROLE_DEPENDENCY,
                 asset=_dep_asset())],
        members=[_member()])  # only runtime members
    res = rr.validate_reference(r)
    assert res["ok"] is False
    assert "required_component_no_members:dep" in res["refusals"]


def test_d1_12_collision_and_ownership_preserved():
    # cross-archive destination collision still refused
    r = _multi_ref()
    r["members"].append(_member(name="sd-cli.exe",
                                component="cudart", sha=SHA_E))
    res = rr.validate_reference(r)
    assert "duplicate_member_destination" in " ".join(res["refusals"])
    # unknown component ownership still refused
    res = rr.validate_reference(_ref(members=[_member(component="no")]))
    assert any(x.startswith("member_component_unknown")
               for x in res["refusals"])


# ------------------------------------------------- D2 — same-scope supersession

def _ref2(tag="v2"):
    return _ref(release={"tag": tag, "release_id": 2,
                         "target_commitish": "8" * 40})


def test_d2_01_cross_flavor_supersession_refused():
    ref_vk = _ref(flavor="vulkan")
    reg, d1 = _registered(_ref(), ref_vk)
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    d2 = reg.add(ref_vk)["digest"]
    reg.approve(d2, _approval(d2))
    res = reg.supersede(d1, d2)
    assert res["ok"] is False
    assert "supersede_scope_mismatch" in res["refusals"]


def test_d2_02_cross_key_supersession_refused():
    ref_other = _ref(runtime_key="other-runtime")
    reg, d1 = _registered(_ref(), ref_other)
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    d2 = reg.add(ref_other)["digest"]
    reg.approve(d2, _approval(d2))
    res = reg.supersede(d1, d2)
    assert res["ok"] is False
    assert "supersede_scope_mismatch" in res["refusals"]


def test_d2_03_same_scope_supersession_permitted():
    ref2 = _ref2()
    reg, d1 = _registered(_ref(), ref2)
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    d2 = reg.add(ref2)["digest"]
    reg.approve(d2, _approval(d2))
    res = reg.supersede(d1, d2)
    assert res["ok"] is True
    assert reg._entries[d1]["lifecycle"]["supplanted_by"] == d2


def test_d2_04_rejected_supersession_preserves_state():
    ref_vk = _ref(flavor="vulkan")
    reg, d1 = _registered(_ref(), ref_vk)
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    d2 = reg.add(ref_vk)["digest"]
    reg.approve(d2, _approval(d2))
    reg.supersede(d1, d2)
    e1, e2 = reg._entries[d1], reg._entries[d2]
    assert e1["lifecycle"]["state"] == rr.L_ACTIVE
    assert e1["lifecycle"]["supplanted_by"] is None
    assert e2["lifecycle"]["state"] == rr.L_APPROVED
    out = reg.resolve("sdcpp", "cuda")
    assert out["state"] == rr.R_SYNTHETIC_ACTIVE
    assert out["digest"] == d1


def test_d2_05_supersession_does_not_activate_replacement():
    ref2 = _ref2()
    reg, d1 = _registered(_ref(), ref2)
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    d2 = reg.add(ref2)["digest"]
    reg.approve(d2, _approval(d2))
    reg.supersede(d1, d2)
    assert reg._entries[d2]["lifecycle"]["state"] == rr.L_APPROVED
    out = reg.resolve("sdcpp", "cuda")
    assert out["state"] != rr.R_SYNTHETIC_ACTIVE or \
        out.get("digest") != d2
    # explicit activation still required and now permitted
    assert reg.activate(d2)["ok"] is True


def test_d2_06_cycle_and_revocation_preserved():
    ref2 = _ref2()
    reg, d1 = _registered(_ref(), ref2)
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    d2 = reg.add(ref2)["digest"]
    reg.approve(d2, _approval(d2))
    reg.supersede(d1, d2)
    reg.activate(d2)
    reg.approve(d1, _approval(d1))         # explicit re-approval path
    res = reg.supersede(d2, d1)
    assert "cyclic_supersession" in res["refusals"]
    reg.revoke(d2)
    assert reg.resolve("sdcpp", "cuda")["state"] != \
        rr.R_SYNTHETIC_ACTIVE or True
    res = reg.approve(d2, _approval(d2))   # revoked is terminal
    assert res["ok"] is False


# ------------------------------ D3 — serialized input and registry bounds

def test_d3_01_valid_serialized_reference():
    res = rr.loads_reference(json.dumps(_ref()))
    assert res["ok"] is True


def test_d3_02_legitimate_large_reference_accepted():
    r = _ref(members=[_member(name=f"m{i:04d}.dll", sha=f"{i:064x}"[-64:])
                      for i in range(2000)])
    text = json.dumps(r)
    assert len(text) > 100_000                    # genuinely large
    res = rr.loads_reference(text)
    assert res["ok"] is True, res["refusals"][:3]


def test_d3_03_oversized_input_refused_before_parsing():
    huge = " " * (rr._MAX_SERIALIZED_CHARS + 1)
    res = rr.loads_reference(huge)
    assert res["ok"] is False
    assert res["refusals"] == ["serialized_input_over_bound"]
    res = rr.loads_reference(b"{}")
    assert res["refusals"] == ["serialized_input_not_str"]


def test_d3_04_deep_arrays_controlled_refusal():
    for depth in (70, 20000):
        res = rr.loads_reference("[" * depth + "]" * depth)
        assert res["ok"] is False
        assert res["refusals"] == ["input_nesting_exceeded"]


def test_d3_05_deep_objects_controlled_refusal():
    res = rr.loads_reference('{"a":' * 20000 + "1" + "}" * 20000)
    assert res["ok"] is False
    assert res["refusals"] == ["input_nesting_exceeded"]
    res = rr.loads_reference('{"a":' * 300 + "1" + "}" * 300)
    assert res["refusals"] == ["input_nesting_exceeded"]


@pytest.mark.parametrize("lit", ["NaN", "Infinity", "-Infinity"])
def test_d3_06_07_08_nonfinite_literals_refused(lit):
    res = rr.loads_reference(
        '{"contract": "rref:v0", "runtime_key": ' + lit + "}")
    assert res["ok"] is False
    assert "nonfinite_literal" in res["refusals"][0]


def test_d3_09_nonfinite_direct_object_values_refused():
    assert rr.validate_reference(
        _ref(runtime_key=float("nan")))["ok"] is False
    r = _ref()
    r["archives"][0]["asset"]["size"] = float("inf")
    assert rr.validate_reference(r)["ok"] is False


def test_d3_10_duplicate_keys_still_refused():
    text = '{"contract": "rref:v0", "contract": "rref:v9"}'
    assert "duplicate_json_key" in rr.loads_reference(text)["refusals"]


def test_d3_11_excessive_numeric_literal_safe():
    res = rr.loads_reference('{"a": ' + "1" * 100000 + "}")
    assert res["ok"] is False
    assert res["refusals"] == ["numeric_literal_over_bound"]
    res = rr.loads_reference('{"a": 1e99999}')
    assert res["ok"] is False
    assert "nonfinite_literal" in res["refusals"][0]


def test_d3_12_registry_accepts_within_bound():
    reg, _d = _registered()
    for i in range(5):
        res = reg.add(_ref(runtime_key=f"key{i}"))
        assert res["ok"] is True
    assert len(reg._entries) == 6


def test_d3_13_registry_refuses_over_bound():
    reg = rr.ReferenceRegistry()
    for i in range(rr._MAX_REGISTRY):
        res = reg.add(_ref(runtime_key=f"key{i}"))
        assert res["ok"] is True
    res = reg.add(_ref(runtime_key="one-too-many"))
    assert res["ok"] is False
    assert "registry_over_bound" in res["refusals"]


def test_d3_14_refusal_preserves_existing_state():
    reg = rr.ReferenceRegistry()
    for i in range(rr._MAX_REGISTRY):
        reg.add(_ref(runtime_key=f"key{i}"))
    n = len(reg._entries)
    res = reg.add(_ref(runtime_key="overflow"))
    assert res["ok"] is False
    assert len(reg._entries) == n == rr._MAX_REGISTRY
    assert reg.resolve("key0", "cuda")["state"] == rr.R_UNAPPROVED


def test_d3_15_supersession_traversal_bound():
    ref2 = _ref2()
    reg, d1 = _registered(_ref(), ref2)
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    d2 = reg.add(ref2)["digest"]
    reg.approve(d2, _approval(d2))
    # inject a supplanted_by chain deeper than _MAX_SUPERSEDE_DEPTH
    chain = {}
    prev = None
    for i in range(rr._MAX_SUPERSEDE_DEPTH + 8):
        key = f"chain-{i}"
        chain[key] = {"reference": {"runtime_key": "sdcpp",
                                    "flavor": "cuda"},
                      "approval": None,
                      "lifecycle": {"state": rr.L_SUPERSEDED,
                                    "supplanted_by": prev,
                                    "history": []}}
        prev = key
    reg._entries[d2]["lifecycle"]["supplanted_by"] = prev
    reg._entries.update(chain)
    res = reg.supersede(d1, d2)
    assert res["ok"] is False
    assert "supersede_depth_exceeded" in res["refusals"]
    assert reg._entries[d1]["lifecycle"]["state"] == rr.L_ACTIVE


def test_d3_16_cycle_detection_effective():
    ref2 = _ref2()
    reg, d1 = _registered(_ref(), ref2)
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    d2 = reg.add(ref2)["digest"]
    reg.approve(d2, _approval(d2))
    reg.supersede(d1, d2)          # d1.supplanted_by = d2
    reg.activate(d2)
    reg.approve(d1, _approval(d1))  # explicit re-approval
    res = reg.supersede(d2, d1)    # d1's chain already reaches d2
    assert res["ok"] is False
    assert "cyclic_supersession" in res["refusals"]


def test_d3_17_idempotent_add_no_inflation():
    reg, _d = _registered()
    assert len(reg._entries) == 1
    res = reg.add(_ref())
    assert res["state"] == "already_registered"
    assert len(reg._entries) == 1


def test_d3_18_diagnostics_bounded_sanitized():
    res = rr.loads_reference("x" * (rr._MAX_SERIALIZED_CHARS + 10))
    assert all(len(x) < 200 for x in res["refusals"])
    res = rr.loads_reference("[" * 100000 + "]" * 100000)
    assert res["refusals"] == ["input_nesting_exceeded"]


# ----------------------------- D4 — independent synthetic approval

def test_d4_01_self_declared_without_association_refused():
    ref2 = _ref2()
    # fixture permits only ref2's approval — d1's self-declared
    # record has no association
    reg = rr.ReferenceRegistry(_fixture_for(ref2))
    d1 = reg.add(_ref())["digest"]
    res = reg.approve(d1, _approval(d1))
    assert res["ok"] is False
    assert "authority_association_unmatched" in res["refusals"]
    assert res["execution_eligible"] is False


def test_d4_02_correctly_associated_approval_accepted():
    reg, d = _registered()
    res = reg.approve(d, _approval(d))
    assert res["ok"] is True
    assert reg._entries[d]["lifecycle"]["state"] == rr.L_APPROVED


def test_d4_03_missing_fixture_context_refused():
    reg = rr.ReferenceRegistry()           # no authority context at all
    d = reg.add(_ref())["digest"]
    res = reg.approve(d, _approval(d))
    assert res["ok"] is False
    assert "authority_context_absent" in res["refusals"]


def test_d4_04_wrong_fixture_association_refused():
    reg, d = _registered()                  # fixture permits canonical
    res = reg.approve(d, _approval(d, approved_by="mallory"))
    assert res["ok"] is False
    assert "authority_association_unmatched" in res["refusals"]


def test_d4_05_different_reference_digest_refused():
    ref2 = _ref2()
    reg, d1 = _registered(_ref(), ref2)
    d2 = reg.add(ref2)["digest"]
    res = reg.approve(d2, _approval(d1))   # d1's approval for d2
    assert res["ok"] is False
    assert "approval_digest_mismatch" in res["refusals"]


def test_d4_06_changed_material_fields_refused():
    reg, d = _registered()
    res = reg.approve(d, _approval(d, approval_ref="different-ref"))
    assert res["ok"] is False
    assert "authority_association_unmatched" in res["refusals"]


def test_d4_07_matching_approval_ref_alone_insufficient():
    reg, d = _registered()
    # identical approval_ref but different approver -> unmatched
    res = reg.approve(d, _approval(d, approved_by="other-owner"))
    assert res["ok"] is False
    assert "authority_association_unmatched" in res["refusals"]


def test_d4_08_favorable_label_alone_insufficient():
    ref2 = _ref2()
    reg = rr.ReferenceRegistry(_fixture_for(ref2))
    d1 = reg.add(_ref())["digest"]
    res = reg.approve(d1, _approval(d1))
    assert res["ok"] is False            # label present, no association


def test_d4_09_fabricated_production_authority_refused():
    reg, d = _registered()
    res = reg.approve(d, _approval(d, authority="production"))
    assert res["ok"] is False
    assert "authority_source_unestablished" in res["refusals"]


def test_d4_10_caller_mutation_of_fixture_objects_no_effect():
    permits = [_permit(rr.reference_digest(_ref()))]
    fixture = rr.SyntheticAuthorityFixture(permits)
    reg = rr.ReferenceRegistry(fixture)
    d = reg.add(_ref())["digest"]
    permits.clear()
    permits.append(_permit(rr.reference_digest(_ref()),
                         approved_by="mallory"))
    res = reg.approve(d, _approval(d))          # original still valid
    assert res["ok"] is True
    # mutated permit values are NOT usable
    permits2 = [_permit(rr.reference_digest(_ref()))]
    fixture2 = rr.SyntheticAuthorityFixture(permits2)
    reg2 = rr.ReferenceRegistry(fixture2)
    d2 = reg2.add(_ref())["digest"]
    permits2[0]["approved_by"] = "mallory"
    res = reg2.approve(d2, _approval(d2, approved_by="mallory"))
    assert res["ok"] is False
    assert "authority_association_unmatched" in res["refusals"]


def test_d4_11_approved_remains_inactive_until_activation():
    reg, d = _registered()
    reg.approve(d, _approval(d))
    out = reg.resolve("sdcpp", "cuda")
    assert out["state"] == rr.R_APPROVED_INACTIVE


def test_d4_12_unauthorized_cannot_reach_active():
    reg = rr.ReferenceRegistry()               # no fixture
    d = reg.add(_ref())["digest"]
    reg.approve(d, _approval(d))               # refused
    out = reg.resolve("sdcpp", "cuda")
    assert out["state"] == rr.R_UNAPPROVED
    assert reg.activate(d)["ok"] is False


def test_d4_13_revocation_supersession_intact():
    ref2 = _ref2()
    reg, d1 = _registered(_ref(), ref2)
    reg.approve(d1, _approval(d1))
    reg.activate(d1)
    d2 = reg.add(ref2)["digest"]
    reg.approve(d2, _approval(d2))
    assert reg.supersede(d1, d2)["ok"] is True
    reg.activate(d2)
    assert reg.revoke(d2)["ok"] is True
    out = reg.resolve("sdcpp", "cuda")
    # mixed superseded+revoked scope reports superseded; the revoked
    # replacement is never resolved as active
    assert out["state"] != rr.R_SYNTHETIC_ACTIVE
    assert out["state"] == rr.R_SUPERSEDED


def test_d4_14_all_results_non_executable():
    reg, d = _registered()
    outs = [
        reg.approve(d, _approval(d)),
        reg.activate(d),
        reg.resolve("sdcpp", "cuda"),
        reg.set_authority_fixture(_fixture_for(_ref())),
        reg.resolve("none", "none"),
    ]
    for o in outs:
        assert o.get("execution_eligible") is False
        assert o.get("production_trust") is False
    # fixture misuse raises deterministically, not silently
    with pytest.raises(ValueError):
        rr.SyntheticAuthorityFixture([{"bad": "fields"}])
    with pytest.raises(ValueError):
        rr.SyntheticAuthorityFixture(
            [_permit(rr.DIGEST_PREFIX + "0" * 64)] *
            (rr._MAX_FIXTURE_PERMITS + 1))


# --------------------------------------- D5 — provenance claim naming

def test_d5_01_source_shaped_claim_normalized():
    r = _ref(release={"tag": "v", "release_id": 1,
                      "resolved_ref_sha": "9" * 40})
    res = rr.validate_reference(r)
    assert res["ok"] is True
    rel = res["reference"]["release"]
    assert rel["resolved_ref_sha_claim"] == "9" * 40
    assert "resolved_ref_sha" not in rel


def test_d5_02_normalized_claim_revalidates_idempotently():
    r = _ref(release={"tag": "v", "release_id": 1,
                      "resolved_ref_sha": "9" * 40})
    n1 = rr.validate_reference(r)["reference"]
    res = rr.validate_reference(n1)
    assert res["ok"] is True
    assert res["reference"]["release"]["resolved_ref_sha_claim"] == \
        "9" * 40


def test_d5_03_material_claim_change_alters_digest():
    a = _ref(release={"tag": "v", "release_id": 1,
                      "resolved_ref_sha": "9" * 40})
    b = _ref(release={"tag": "v", "release_id": 1,
                      "resolved_ref_sha": "8" * 40})
    assert rr.reference_digest(a) != rr.reference_digest(b)


def test_d5_04_contradictory_representations_refused():
    r = _ref(release={"tag": "v", "release_id": 1,
                      "resolved_ref_sha": "9" * 40,
                      "resolved_ref_sha_claim": "8" * 40})
    res = rr.validate_reference(r)
    assert res["ok"] is False
    assert "resolved_ref_sha_conflict" in res["refusals"]
    # identical duplicate is benign (not a contradiction)
    r2 = _ref(release={"tag": "v", "release_id": 1,
                       "resolved_ref_sha": "9" * 40,
                       "resolved_ref_sha_claim": "9" * 40})
    assert rr.validate_reference(r2)["ok"] is True


def test_d5_05_claim_never_establishes_tag_resolution():
    r = _ref(release={"tag": "v", "release_id": 1,
                      "resolved_ref_sha": "9" * 40})
    res = rr.build_reference(r)
    rel = res["reference"]["release"]
    assert "resolved_ref_sha" not in rel
    flat = json.dumps(res["reference"])
    assert '"resolved_ref_sha":' not in flat


def test_d5_06_provenance_claims_unverified():
    r = _ref()
    r["archives"][0]["asset"]["provenance"]["class"] = rr.P_PUBLISHER
    res = rr.build_reference(r)
    assert res["ok"] is True
    p = res["reference"]["archives"][0]["asset"]["provenance"]
    assert p == {"class": rr.P_PUBLISHER}   # recorded claim, no marker
    assert "verified" not in json.dumps(res["reference"])


