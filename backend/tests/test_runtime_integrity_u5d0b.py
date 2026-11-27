"""U-5d-0b Phase beta — synthetic runtime-integrity tests (rtq:v1).

Every test uses synthetic temporary-file fixtures under tmp_path.
No actual installed runtime or model file is ever read, hashed or
enumerated; no runtime executable is invoked.
"""

from __future__ import annotations

import hashlib
import os

import pytest

from app.domain import runtime_integrity as ri


def _write(root, name, data=b"data"):
    p = root / name
    p.write_bytes(data)
    return p


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _member(name, data):
    return {"name": name, "sha256": _sha(data), "size": len(data)}


def _manifest(root, files: dict, provenance=ri.P_INSTALL_CAPTURE,
              runtime_key="sdcpp-cuda", flavor="cuda"):
    members = [_member(n, d) for n, d in files.items()]
    res = ri.build_reference_manifest(runtime_key, flavor, members,
                                      provenance)
    assert res["ok"], res["refusals"]
    return res["manifest"]


def _tree(tmp_path, files: dict):
    root = tmp_path / "sdcpp-cuda"
    root.mkdir()
    for n, d in files.items():
        _write(root, n, d)
    return root


# ------------------------------------------------- manifest structure


def test_manifest_builds_and_validates(tmp_path):
    m = _manifest(tmp_path, {"sd-cli.exe": b"exe", "ggml.dll": b"dll"})
    assert m["coverage"] == "flat_install_dir"
    assert ri.validate_reference_manifest(m)["ok"]


def test_invalid_sha256_refuses():
    res = ri.build_reference_manifest(
        "k", "cuda", [{"name": "a.exe", "sha256": "zz" * 32,
                       "size": 1}], ri.P_INSTALL_CAPTURE)
    assert not res["ok"]
    assert any("member_sha256_invalid" in r for r in res["refusals"])


def test_uppercase_sha256_normalized():
    res = ri.build_reference_manifest(
        "k", "cuda", [{"name": "a.exe",
                       "sha256": _sha(b"x").upper(), "size": 1}],
        ri.P_INSTALL_CAPTURE)
    assert res["ok"]
    assert res["manifest"]["members"][0]["sha256"] == _sha(b"x")


@pytest.mark.parametrize("bad_size", [True, False, "10", 3.5, -1, None])
def test_invalid_declared_size_refuses(bad_size):
    res = ri.build_reference_manifest(
        "k", "cuda", [{"name": "a.exe", "sha256": _sha(b"x"),
                       "size": bad_size}], ri.P_INSTALL_CAPTURE)
    assert not res["ok"]
    assert any("member_size_invalid" in r for r in res["refusals"])


def test_duplicate_member_casefold_refuses():
    res = ri.build_reference_manifest(
        "k", "cuda", [{"name": "GGML.dll", "sha256": _sha(b"a"),
                       "size": 1},
                      {"name": "ggml.dll", "sha256": _sha(b"b"),
                       "size": 1}], ri.P_INSTALL_CAPTURE)
    assert not res["ok"]
    assert any("duplicate_member" in r for r in res["refusals"])


@pytest.mark.parametrize("bad", [
    "..", ".", "../x.dll", "..\\x.dll", "/abs/x.dll", "sub/x.dll",
    "sub\\x.dll", "C:\\x.dll", "c:x.dll", "\\\\unc\\x.dll",
    "\\\\?\\C:\\x.dll", "file:name.dll", "name.", "name ", " con.txt",
    "CON.dll", "nul", "lpt3.exe", "a\x01b.dll", "", "  ",
])
def test_member_name_unsafe_refuses(bad):
    assert not ri.valid_member_name(bad)
    res = ri.build_reference_manifest(
        "k", "cuda", [{"name": bad, "sha256": _sha(b"x"), "size": 1}],
        ri.P_INSTALL_CAPTURE)
    assert not res["ok"]
    assert any("member_name_invalid" in r for r in res["refusals"])


def test_empty_members_refuses():
    res = ri.build_reference_manifest("k", "cuda", [],
                                      ri.P_INSTALL_CAPTURE)
    assert not res["ok"]
    assert "members_missing" in res["refusals"]


def test_unknown_or_missing_provenance_refuses():
    for prov in ["self_measured", "trusted", "", None, True]:
        res = ri.build_reference_manifest(
            "k", "cuda", [{"name": "a", "sha256": _sha(b"x"),
                           "size": 1}], prov)
        assert not res["ok"], prov
        assert "provenance_untrusted_or_unknown" in res["refusals"]


def test_provenance_label_cannot_upgrade_trust(tmp_path):
    """A caller-supplied favorable label never raises the trust class:
    install_capture always classifies observed_first_use."""
    m = _manifest(tmp_path, {"a.dll": b"x"})
    assert ri.trust_classification(m) == ri.TRUST_OBSERVED_FIRST_USE
    forged = dict(m, provenance="self_measured")
    assert ri.trust_classification(forged) is None
    assert not ri.validate_reference_manifest(forged)["ok"]


@pytest.mark.parametrize("prov", ["publisher", "owner_attested"])
def test_recognized_labels_cannot_establish_trust(tmp_path, prov):
    """Adversarial C1: modified executable bytes → self-computed
    hashes → matching manifest → claimed provenance 'publisher' /
    'owner_attested'. The label is schema-valid but can never
    establish trusted reference authority: trust stays
    unverified_claim even when every member matches."""
    root = _tree(tmp_path, {"sd-cli.exe": b"TAMPERED exe bytes"})
    tampered = [{"name": "sd-cli.exe",
                 "sha256": _sha(b"TAMPERED exe bytes"),
                 "size": len(b"TAMPERED exe bytes")}]
    res = ri.build_reference_manifest("sdcpp-cuda", "cuda", tampered,
                                      prov)
    assert res["ok"]                      # schema-valid ≠ trusted
    m = res["manifest"]
    assert ri.trust_classification(m) == ri.TRUST_UNVERIFIED_CLAIM
    assert ri.trust_classification(m) != ri.TRUST_EXTERNAL
    q = ri.qualify(root, m, expected_flavor="cuda")
    # Member comparison succeeds — that is E-vs-O evidence only;
    # it never becomes runtime integrity qualification, operational
    # qualification or execution eligibility.
    assert q["checks"]["all_members_match"]
    assert q["comparison"] == ri.COMPARISON_MATCH
    assert q["trust"] == ri.TRUST_UNVERIFIED_CLAIM
    assert q["integrity_qualification"] == "unestablished"
    assert q["operational_qualification"] == "unestablished"
    assert q["execution_eligible"] is False


def test_install_capture_stays_observed_first_use(tmp_path):
    root = _tree(tmp_path, {"sd-cli.exe": b"exe"})
    m = _manifest(root, {"sd-cli.exe": b"exe"})
    q = ri.qualify(root, m)
    assert q["trust"] == ri.TRUST_OBSERVED_FIRST_USE
    # The module can never return the reserved external class.
    for prov in ("publisher", "owner_attested", "install_capture"):
        assert ri.trust_classification(
            {"provenance": prov}) != ri.TRUST_EXTERNAL


def test_qualify_record_carries_runtime_association(tmp_path):
    files = {"sd-cli.exe": b"exe"}
    root = _tree(tmp_path, files)
    q = ri.qualify(root, _manifest(root, files))
    assert q["runtime_key"] == "sdcpp-cuda"
    assert q["flavor"] == "cuda"


def test_current_bytes_cannot_self_certify(tmp_path):
    """A manifest generated FROM current bytes is still only
    observed-first-use evidence — never a trusted reference."""
    files = {"sd-cli.exe": b"exe-bytes", "ggml.dll": b"dll-bytes"}
    root = _tree(tmp_path, files)
    observed = [
        {"name": n, "sha256": _sha(d), "size": len(d)}
        for n, d in files.items()]  # measured from the tree itself
    res = ri.build_reference_manifest("k", "cuda", observed,
                                      ri.P_INSTALL_CAPTURE)
    assert res["ok"]
    assert ri.trust_classification(res["manifest"]) \
        == ri.TRUST_OBSERVED_FIRST_USE
    q = ri.qualify(root, res["manifest"], expected_flavor="cuda")
    # Members match, but the trust class remains first-use observed —
    # this is never presented as publisher authentication.
    assert q["trust"] == ri.TRUST_OBSERVED_FIRST_USE


def test_manifest_digest_identifies_reference(tmp_path):
    m = _manifest(tmp_path, {"a.dll": b"one"})
    m2 = _manifest(tmp_path, {"a.dll": b"two"})
    assert ri.manifest_digest(m).startswith("sha256:")
    assert ri.manifest_digest(m) != ri.manifest_digest(m2)
    assert ri.manifest_digest({"v": 99}) is None


# ------------------------------------------------------ measurement


def test_qualify_all_members_match(tmp_path):
    files = {"sd-cli.exe": b"exe", "ggml.dll": b"d", "cudart.dll": b"c"}
    root = _tree(tmp_path, files)
    q = ri.qualify(root, _manifest(root, files), expected_flavor="cuda")
    assert q["comparison"] == ri.COMPARISON_MATCH
    assert q["reason"] == "all_members_verified"
    assert {m["state"] for m in q["members"]} == {"match"}
    assert q["checks"]["coverage_closed"]


def test_modified_executable_refuses(tmp_path):
    files = {"sd-cli.exe": b"original", "ggml.dll": b"d"}
    root = _tree(tmp_path, files)
    _write(root, "sd-cli.exe", b"TAMPERED!")
    q = ri.qualify(root, _manifest(root, files), expected_flavor="cuda")
    assert q["comparison"] != ri.COMPARISON_MATCH
    states = {m["name"]: m["state"] for m in q["members"]}
    assert states["sd-cli.exe"] in ("sha256_mismatch", "size_mismatch")


def test_same_path_replacement_refuses(tmp_path):
    files = {"sd-cli.exe": b"original"}
    root = _tree(tmp_path, files)
    root.joinpath("sd-cli.exe").unlink()
    _write(root, "sd-cli.exe", b"replaced")
    q = ri.qualify(root, _manifest(root, files))
    assert q["comparison"] != ri.COMPARISON_MATCH
    assert q["members"][0]["state"] != "match"


def test_modified_dependency_refuses(tmp_path):
    files = {"sd-cli.exe": b"exe", "ggml-vulkan.dll": b"dep"}
    root = _tree(tmp_path, files)
    _write(root, "ggml-vulkan.dll", b"DEX")  # same size → sha check
    q = ri.qualify(root, _manifest(root, files))
    assert q["comparison"] != ri.COMPARISON_MATCH
    states = {m["name"]: m["state"] for m in q["members"]}
    assert states["ggml-vulkan.dll"] == "sha256_mismatch"
    assert states["sd-cli.exe"] == "match"


def test_missing_member_refuses(tmp_path):
    files = {"sd-cli.exe": b"exe", "ggml.dll": b"dep"}
    root = _tree(tmp_path, files)
    root.joinpath("ggml.dll").unlink()
    q = ri.qualify(root, _manifest(root, files))
    assert q["comparison"] != ri.COMPARISON_MATCH
    states = {m["name"]: m["state"] for m in q["members"]}
    assert states["ggml.dll"] == "missing"


def test_non_regular_member_refuses(tmp_path):
    root = _tree(tmp_path, {"sd-cli.exe": b"exe"})
    (root / "ggml.dll").mkdir()
    q = ri.qualify(root, _manifest(root, {
        "sd-cli.exe": b"exe", "ggml.dll": b"dep"}))
    states = {m["name"]: m["state"] for m in q["members"]}
    assert states["ggml.dll"] == "non_regular"
    assert q["comparison"] != ri.COMPARISON_MATCH


def test_containment_violation_refuses(tmp_path, monkeypatch):
    files = {"sd-cli.exe": b"exe"}
    root = _tree(tmp_path, files)
    m = _manifest(root, files)
    outside = tmp_path / "elsewhere" / "sd-cli.exe"
    monkeypatch.setattr(ri.os.path, "realpath",
                        lambda p: (str(outside) if "sd-cli" in str(p)
                                   else str(p)))
    q = ri.qualify(root, m)
    assert q["comparison"] != ri.COMPARISON_MATCH
    assert q["members"][0]["state"] == "containment_violation"


def test_reparse_point_refused(tmp_path):
    files = {"sd-cli.exe": b"exe", "ggml.dll": b"dep"}
    root = _tree(tmp_path, files)
    m = _manifest(root, files)
    link = root / "ggml.dll"
    link.unlink()
    try:
        # Same-root redirect — resolves inside the coverage dir but is
        # still a reparse point and must be refused.
        os.symlink(root / "sd-cli.exe", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink privilege unavailable")
    q = ri.qualify(root, m)
    states = {m_["name"]: m_["state"] for m_ in q["members"]}
    assert states["ggml.dll"] == "reparse_point"


def test_changed_during_verification_refuses(tmp_path, monkeypatch):
    files = {"sd-cli.exe": b"exe"}
    root = _tree(tmp_path, files)
    m = _manifest(root, files)
    real_stream = ri._stream_sha256_checked

    def racing(path, st0):
        digest, same = real_stream(path, st0)
        path.write_bytes(b"post-check mutation")
        return digest, same

    monkeypatch.setattr(ri, "_stream_sha256_checked", racing)
    q = ri.qualify(root, m)
    assert q["comparison"] != ri.COMPARISON_MATCH
    assert q["members"][0]["state"] == "changed_during_verification"


def test_io_failure_sanitized(tmp_path, monkeypatch):
    files = {"sd-cli.exe": b"exe"}
    root = _tree(tmp_path, files)
    m = _manifest(root, files)

    def deny(path, st0):
        raise OSError("access denied")

    monkeypatch.setattr(ri, "_stream_sha256_checked", deny)
    q = ri.qualify(root, m)
    assert q["comparison"] != ri.COMPARISON_MATCH
    assert q["members"][0]["state"] == "io_error"
    assert q["errors"][0]["member"] == "sd-cli.exe"
    assert str(root) not in repr(q)  # absolute paths never leak


def test_opened_file_identity_swap_refuses(tmp_path, monkeypatch):
    """Same-path swap between stat and open: the opened handle's
    st_dev/st_ino differs from the pre-read stat → refusal."""
    files = {"sd-cli.exe": b"exe"}
    root = _tree(tmp_path, files)
    m = _manifest(root, files)

    class FakeStat:
        st_dev, st_ino, st_size = 999, 999, 3

    monkeypatch.setattr(ri.os, "fstat", lambda fd: FakeStat())
    q = ri.qualify(root, m)
    assert q["comparison"] != ri.COMPARISON_MATCH
    assert q["members"][0]["state"] == "opened_file_identity_changed"


def test_post_verify_association_lost_refuses(tmp_path, monkeypatch):
    """Path swapped to a reparse/redirect AFTER the hash: the
    post-close association check catches it."""
    files = {"sd-cli.exe": b"exe"}
    root = _tree(tmp_path, files)
    m = _manifest(root, files)
    real_realpath = ri.os.path.realpath
    member_calls = [0]

    def flipflop(p):
        if "sd-cli" in str(p):
            member_calls[0] += 1
            if member_calls[0] > 1:
                # First resolution (containment pre-check) stayed
                # inside; every later association check escapes.
                return str(tmp_path / "elsewhere" / "sd-cli.exe")
        return real_realpath(p)

    monkeypatch.setattr(ri.os.path, "realpath", flipflop)
    q = ri.qualify(root, m)
    assert q["comparison"] != ri.COMPARISON_MATCH
    assert q["members"][0]["state"] == "post_verify_association_lost"


def test_unexpected_member_closed_coverage(tmp_path):
    files = {"sd-cli.exe": b"exe", "ggml.dll": b"d"}
    root = _tree(tmp_path, files)
    _write(root, "intruder.dll", b"unexpected")
    q = ri.qualify(root, _manifest(root, files))
    assert q["comparison"] != ri.COMPARISON_MATCH
    assert q["reason"] == "unexpected_members_in_scope"
    assert q["unexpected"] == ["intruder.dll"]


def test_flavor_mismatch_refuses(tmp_path):
    files = {"sd-cli.exe": b"exe"}
    root = _tree(tmp_path, files)
    q = ri.qualify(root, _manifest(root, files),
                   expected_flavor="vulkan")
    assert q["comparison"] != ri.COMPARISON_MATCH
    assert q["reason"] == "flavor_mismatch"


def test_invalid_manifest_qualification_refuses(tmp_path):
    root = _tree(tmp_path, {"sd-cli.exe": b"exe"})
    q = ri.qualify(root, {"v": 1, "members": "notalist"})
    assert q["comparison"] == ri.COMPARISON_INVALID
    assert q["reason"] == "reference_manifest_invalid"
    assert q["refusals"]


def test_missing_root_refuses(tmp_path):
    q = ri.qualify(tmp_path / "nonexistent",
                   _manifest(tmp_path, {"a.dll": b"x"}))
    assert q["comparison"] == ri.COMPARISON_UNREADABLE
    assert "root_unreadable" in q["refusals"]


def test_sibling_verification_not_loaded_set_claim(tmp_path):
    """Verified sibling files prove on-disk member integrity only —
    the record never claims the actual loaded dependency set."""
    files = {"sd-cli.exe": b"exe", "ggml.dll": b"d"}
    root = _tree(tmp_path, files)
    q = ri.qualify(root, _manifest(root, files))
    assert q["comparison"] == ri.COMPARISON_MATCH
    assert "loaded" not in repr(q).lower()


# -------------------------------------------------- evidence classes


def test_evidence_classes_observation_not_trust():
    meta = {"tag": "master-1073-991f825", "pinned": True,
            "zip_sha256": _sha(b"archive")}
    ev = ri.evidence_classes(meta)
    assert ev["A_intended_release_identity"]["class"] == \
        "intended_distribution_identifier"
    assert ev["B_trusted_archive_integrity"]["state"] == "unavailable"
    assert ev["C_observed_archive_integrity"]["class"] == \
        ri.TRUST_OBSERVED_FIRST_USE
    assert ev["C_observed_archive_integrity"]["sha256"] == _sha(
        b"archive")
    assert ev["F_exact_build_source"]["revisions"][
        "stable_diffusion_cpp"] == ri.PINNED_SOURCE[
        "stable_diffusion_cpp"]
    assert ev["G_operational_qualification"]["state"] == \
        "unavailable_held_beta3"
    assert ev["H_launch_time_validation"]["state"] == \
        "unavailable_no_launch_integration"


def test_evidence_classes_absent_observed_hash():
    ev = ri.evidence_classes({"tag": "t"})
    assert ev["C_observed_archive_integrity"]["class"] == "absent"
    assert ev["D_expected_extracted_integrity"]["state"] == \
        "unavailable"


def test_qualify_record_has_no_qualification_claim(tmp_path):
    """P1: the rtq:v1 record exposes a comparison result only — no
    'qualified' classification exists, and every qualification
    dimension is an unelevatable constant."""
    files = {"sd-cli.exe": b"exe", "ggml.dll": b"d"}
    root = _tree(tmp_path, files)
    q = ri.qualify(root, _manifest(root, files),
                   expected_flavor="cuda")
    assert q["comparison"] == ri.COMPARISON_MATCH
    assert "classification" not in q
    assert q["integrity_qualification"] == ri.QUAL_UNESTABLISHED
    assert q["operational_qualification"] == ri.QUAL_UNESTABLISHED
    assert q["execution_eligible"] is False
    assert q["trust"] != ri.TRUST_EXTERNAL


def test_tampered_install_capture_match_is_not_qualification(tmp_path):
    """P1: tampered bytes self-hashed under install_capture — every
    member matches the generated reference, yet integrity
    qualification is unestablished and execution ineligible."""
    root = _tree(tmp_path, {"sd-cli.exe": b"TAMPERED"})
    members = [{"name": "sd-cli.exe", "sha256": _sha(b"TAMPERED"),
                "size": len(b"TAMPERED")}]
    res = ri.build_reference_manifest(
        "sdcpp-cuda", "cuda", members, ri.P_INSTALL_CAPTURE)
    q = ri.qualify(root, res["manifest"])
    assert q["comparison"] == ri.COMPARISON_MATCH
    assert q["trust"] == ri.TRUST_OBSERVED_FIRST_USE
    assert q["integrity_qualification"] == "unestablished"
    assert q["execution_eligible"] is False


def test_runtime_update_invalidates_manifest(tmp_path):
    """A runtime update changes observed member digests — the previous
    reference no longer qualifies (qualification cannot carry over)."""
    root = _tree(tmp_path, {"sd-cli.exe": b"exe-v1"})
    m = _manifest(root, {"sd-cli.exe": b"exe-v1"})
    assert ri.qualify(root, m)["comparison"] == ri.COMPARISON_MATCH
    _write(root, "sd-cli.exe", b"exe-v2-updated")
    q = ri.qualify(root, m)
    assert q["comparison"] != ri.COMPARISON_MATCH
    assert q["members"][0]["state"] in ("sha256_mismatch",
                                       "size_mismatch")
