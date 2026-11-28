"""U-5d-0b Phase beta-3-B3.2 — racq:v0 isolated verified runtime
acquisition and safe multi-archive extraction foundation tests.

Every acquisition runs only against synthetic references, synthetic
in-memory ZIP archives, an injected chunk-transport and an isolated
pytest tmp_path staging root. Nothing here contacts a network, reads a
production runtime/model directory, enrolls a reference, publishes an
installation or executes anything."""

from __future__ import annotations

import ast
import copy
import hashlib
import io
import os
import subprocess
import sys
import warnings
import zipfile
from pathlib import Path

import pytest

from app.domain import runtime_acquisition as ra
from app.domain import runtime_reference as rr


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _z(entries) -> bytes:
    """entries: [(name, bytes)] or [(name, bytes, zinfo_overrides)]."""
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


def _mk(components, members=None, skips=(), tag="v1", release_id=1):
    """components: [(comp, role, required, asset_id, zip_entries)].
    Returns dict(ref=..., meta=..., zips=..., urls={url:zip_bytes})."""
    archives, zips, urls, meta_assets = [], {}, {}, []
    for comp, role, req, aid, entries in components:
        zb = _z(entries)
        zips[comp] = zb
        name = f"{comp}.zip"
        url = f"https://x/{name}"
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
    ref = {"contract": rr.RREF_VERSION, "runtime_key": "sdcpp",
           "flavor": "cuda",
           "release": {"tag": tag, "release_id": release_id,
                       "target_commitish": "9" * 40},
           "archives": archives, "members": members,
           "coverage": {"mode": "flat_install_dir",
                        "unexpected_policy": "refuse",
                        "skip_names": list(skips)}}
    return {"ref": ref,
            "meta": {"release_id": release_id, "tag_name": tag,
                     "assets": meta_assets},
            "zips": zips, "urls": urls}


def _single(member_data=b"sd-bytes", member_name="sd.exe"):
    return _mk([("runtime", "primary", True, 1,
                 [(member_name, member_data)])])


def _multi(include_optional=True):
    comps = [("runtime", "primary", True, 1,
              [("sd.exe", b"sd-bin"), ("sd.dll", b"sd-dll")]),
             ("cudart", "dependency", True, 2,
              [("cudart.dll", b"cudart-bin")])]
    if include_optional:
        comps.append(("opt", "optional", False, 3,
                      [("opt.txt", b"opt")]))
    return _mk(comps)


def _sel(scn, **flags):
    out = {a["component"]: True for a in scn["ref"]["archives"]}
    out.update(flags)
    return out


def _permit(scn, selected, ops=("acquire", "extract")):
    by = {a["component"]: a["asset"] for a in scn["ref"]["archives"]}
    return {"reference_digest": rr.reference_digest(scn["ref"]),
            "runtime_key": "sdcpp", "flavor": "cuda",
            "components": selected,
            "release_id": scn["ref"]["release"]["release_id"],
            "tag": scn["ref"]["release"]["tag"],
            "assets": {c: {"asset_id": by[c]["id"],
                           "name": by[c]["name"],
                           "size": by[c]["size"],
                           "sha256": by[c]["digest"]["value"],
                           "source": by[c]["url"]}
                       for c in selected},
            "permitted_ops": list(ops)}


def _auth(scn, selected, ops=("acquire", "extract"), revoked=None):
    return ra.SyntheticAcquisitionAuthority(
        [_permit(scn, selected, ops)], revoked=revoked)


def _transport(scn, tweak=None):
    urls = dict(scn["urls"])
    if tweak:
        urls.update(tweak)
    return lambda url: iter([urls[url]])


def _chunks(data, sizes=(1,)):
    """Split `data` into chunks for multi-chunk stream tests."""
    if sizes == (1,):
        return [data]
    parts = []
    step = max(1, len(data) // len(sizes))
    for i in range(0, len(data), step):
        parts.append(data[i:i + step])
    return parts


def _run(scn, tmp_path, selection=None, authority=None, meta=None,
         transport=None, cancel_check=None):
    sel = selection if selection is not None else _sel(scn)
    known = {a["component"] for a in scn["ref"]["archives"]}
    if authority is not None:
        auth = authority
    elif isinstance(sel, dict) and set(sel) <= known:
        comps = [c for c, v in sel.items() if v]
        auth = _auth(scn, comps) if comps \
            else ra.SyntheticAcquisitionAuthority([])
    else:
        auth = ra.SyntheticAcquisitionAuthority([])
    return ra.acquire(scn["ref"], authority=auth, selection=sel,
                      release_metadata=meta if meta is not None
                      else scn["meta"],
                      transport=transport or _transport(scn),
                      staging_root=tmp_path,
                      cancel_check=cancel_check)


def _staging_dir(res, tmp_path):
    return tmp_path / res["staging"]["dir"]


# ================================================================ A01–A16
# acquisition identity and authority


def test_a01_valid_single_archive(tmp_path):
    res = _run(_single(), tmp_path)
    assert res["ok"] and res["state"] == "completed"
    assert res["contract"] == "racq:v0"
    assert res["acquisition"].startswith("racq:v0:")
    assert res["production_trust"] is False
    assert res["execution_eligible"] is False
    assert res["members"][0]["state"] == "match"
    dest = _staging_dir(res, tmp_path) / "members" / "sd.exe"
    assert dest.read_bytes() == b"sd-bytes"


def test_a02_valid_multi_archive(tmp_path):
    res = _run(_multi(), tmp_path)
    assert res["ok"]
    assert {a["component"] for a in res["archives"]} == \
        {"runtime", "cudart", "opt"}
    assert len(res["members"]) == 4
    members = _staging_dir(res, tmp_path) / "members"
    assert (members / "sd.dll").read_bytes() == b"sd-dll"
    assert (members / "opt.txt").exists()


def test_a03_exact_reference_digest_association(tmp_path):
    scn = _single()
    res = _run(scn, tmp_path)
    assert res["ok"]
    assert res["reference"]["digest"] == rr.reference_digest(scn["ref"])
    identity = {"contract": "racq:v0",
                "reference_digest": res["reference"]["digest"],
                "runtime_key": "sdcpp", "flavor": "cuda",
                "selected": ["runtime"], "omitted": [],
                "assets": {"runtime": {
                    "asset_id": 1, "name": "runtime.zip",
                    "size": len(scn["zips"]["runtime"]),
                    "sha256": _sha(scn["zips"]["runtime"]),
                    "source": "https://x/runtime.zip"}}}
    assert res["acquisition"] == ra.acquisition_digest(identity)


def test_a04_changed_reference_invalidates_association(tmp_path):
    scn = _single()
    changed = copy.deepcopy(scn["ref"])
    changed["members"][0]["sha256"] = "0" * 64
    res = ra.acquire(changed, authority=_auth(scn, ["runtime"]),
                     selection={"runtime": True},
                     release_metadata=scn["meta"],
                     transport=_transport(scn), staging_root=tmp_path)
    assert res["state"] == "refused"
    assert res["refusals"] == ["authority_association_unmatched"]


def test_a05_absent_authority_refused(tmp_path):
    scn = _single()
    res = ra.acquire(scn["ref"], authority=None,
                     selection={"runtime": True},
                     release_metadata=scn["meta"],
                     transport=_transport(scn), staging_root=tmp_path)
    assert res["refusals"] == ["authority_context_absent"]


def test_a06_self_declared_authority_refused(tmp_path):
    scn = _single()
    for bogus in ({"approved": True}, "owner-approved",
                  {"authority": "self"}, 1):
        res = ra.acquire(scn["ref"], authority=bogus,
                         selection={"runtime": True},
                         release_metadata=scn["meta"],
                         transport=_transport(scn),
                         staging_root=tmp_path)
        assert res["refusals"] == ["authority_context_invalid"], bogus


def test_a07_mismatched_permit_refused(tmp_path):
    scn = _single()
    p = _permit(scn, ["runtime"])
    p["assets"]["runtime"]["size"] += 1
    res = _run(scn, tmp_path,
               authority=ra.SyntheticAcquisitionAuthority([p]))
    assert res["refusals"] == ["authority_association_unmatched"]


def test_a08_caller_mutation_cannot_alter_permit(tmp_path):
    scn = _single()
    p = _permit(scn, ["runtime"])
    auth = ra.SyntheticAcquisitionAuthority([p])
    # Mutating the caller-side structures after construction must not
    # change the frozen context.
    p["assets"]["runtime"]["sha256"] = "0" * 64
    p["permitted_ops"].append("publish")
    p["components"].append("evil")
    res = _run(scn, tmp_path, authority=auth)
    assert res["ok"]


def test_a09_acquire_only_permit_cannot_extract(tmp_path):
    scn = _single()
    res = _run(scn, tmp_path,
               authority=_auth(scn, ["runtime"], ops=("acquire",)))
    assert res["state"] == "refused"
    assert res["refusals"] == ["operation_not_permitted:extract"]
    # Archive bytes were still fetched and verified under the permit.
    assert res["archives"][0]["verified"] is True
    assert res["members"] == []


def test_a10_synthetic_authority_no_production_trust(tmp_path):
    res = _run(_single(), tmp_path)
    assert res["ok"]
    assert res["authority"]["class"] == "synthetic_acquisition"
    assert res["production_trust"] is False
    assert res["trusted_enrollment"] == "unestablished"
    assert res["operational_qualification"] == "unestablished"


def test_a11_missing_required_component_refused(tmp_path):
    scn = _multi()
    res = _run(scn, tmp_path,
               selection=_sel(scn, cudart=False))
    assert res["refusals"] == ["required_component_omitted:cudart"]


def test_a12_explicit_optional_omission_reported(tmp_path):
    scn = _multi()
    res = _run(scn, tmp_path, selection=_sel(scn, opt=False))
    assert res["ok"]
    assert res["coverage"]["omitted_optional"] == ["opt"]
    assert {m["name"] for m in res["members"]} == \
        {"sd.exe", "sd.dll", "cudart.dll"}
    assert res["components"]["omitted"] == ["opt"]


def test_a13_selected_optional_fully_verified(tmp_path):
    res = _run(_multi(), tmp_path)
    assert res["ok"]
    assert all(m["state"] == "match" for m in res["members"])
    assert any(m["component"] == "opt" for m in res["members"])


def test_a14_missing_selected_optional_asset_refused(tmp_path):
    scn = _multi()
    meta = copy.deepcopy(scn["meta"])
    meta["assets"] = [a for a in meta["assets"] if a["name"] != "opt.zip"]
    res = _run(scn, tmp_path, meta=meta)
    assert res["refusals"] == ["asset_missing:opt"]


def test_a15_unknown_or_incomplete_selection_refused(tmp_path):
    scn = _single()
    res = _run(scn, tmp_path,
               selection={"runtime": True, "bogus": True})
    assert res["refusals"][0].startswith("selection_unknown_component")
    sel = dict(_sel(scn))
    del sel["runtime"]
    res = _run(scn, tmp_path, selection=sel)
    assert res["refusals"][0].startswith("selection_incomplete")
    res = _run(scn, tmp_path, selection={"runtime": "yes"})
    assert res["refusals"] == ["selection_flag_invalid:runtime"]


def test_a16_unexpected_unselected_component_refused(tmp_path):
    # The opt component exists in the reference but is NOT selected;
    # the runtime archive must not smuggle its member.
    scn = _mk(
        [("runtime", "primary", True, 1,
          [("sd.exe", b"sd"), ("opt.txt", b"opt")]),
         ("opt", "optional", False, 2, [("opt.txt", b"opt")])],
        members=[_member("sd.exe", "runtime", b"sd"),
                 _member("opt.txt", "opt", b"opt")])
    res = _run(scn, tmp_path, selection=_sel(scn, opt=False))
    assert res["refusals"] == ["unexpected_member:opt.txt"]


# ================================================================ A17–A29
# exact asset resolution


def test_a17_exact_release_and_asset_match(tmp_path):
    res = _run(_single(), tmp_path)
    assert res["ok"]
    rec = res["archives"][0]
    assert rec["asset_id"] == 1 and rec["name"] == "runtime.zip"


def test_a18_missing_release_or_asset_refused(tmp_path):
    scn = _single()
    meta = {"release_id": 99, "tag_name": "other", "assets": []}
    res = _run(scn, tmp_path, meta=meta)
    assert res["refusals"] == ["release_missing"]
    meta = copy.deepcopy(scn["meta"])
    meta["assets"] = []
    res = _run(scn, tmp_path, meta=meta)
    assert res["refusals"] == ["asset_missing:runtime"]


def test_a19_ambiguous_release_or_asset_refused(tmp_path):
    scn = _single()
    two = [dict(scn["meta"]), dict(scn["meta"])]
    res = _run(scn, tmp_path, meta=two)
    assert res["refusals"] == ["release_ambiguous"]
    meta = copy.deepcopy(scn["meta"])
    meta["assets"].append(dict(meta["assets"][0]))
    res = _run(scn, tmp_path, meta=meta)
    assert res["refusals"] == [
        "asset_duplicate_identity:runtime.zip"]


def test_a20_unknown_security_relevant_metadata_fields_refused(
        tmp_path):
    scn = _single()
    meta = copy.deepcopy(scn["meta"])
    meta["extra_field"] = 1
    res = _run(scn, tmp_path, meta=meta)
    assert res["refusals"] == ["release_unknown_field:extra_field"]
    meta = copy.deepcopy(scn["meta"])
    meta["assets"][0]["other"] = "x"
    res = _run(scn, tmp_path, meta=meta)
    assert res["refusals"] == ["asset_unknown_field:other"]


def test_a21_changed_asset_identity_refused(tmp_path):
    scn = _single()
    meta = copy.deepcopy(scn["meta"])
    meta["assets"][0]["id"] = 2
    res = _run(scn, tmp_path, meta=meta)
    assert res["refusals"] == ["asset_id_changed:runtime"]


def test_a22_changed_expected_size_refused(tmp_path):
    scn = _single()
    meta = copy.deepcopy(scn["meta"])
    meta["assets"][0]["size"] += 1
    res = _run(scn, tmp_path, meta=meta)
    assert res["refusals"] == ["asset_size_changed:runtime"]


def test_a23_changed_expected_digest_refused(tmp_path):
    scn = _single()
    meta = copy.deepcopy(scn["meta"])
    meta["assets"][0]["digest"] = "sha256:" + "0" * 64
    res = _run(scn, tmp_path, meta=meta)
    assert res["refusals"] == ["asset_digest_changed:runtime"]


def test_a24_missing_digest_refused(tmp_path):
    scn = _single()
    meta = copy.deepcopy(scn["meta"])
    del meta["assets"][0]["digest"]
    res = _run(scn, tmp_path, meta=meta)
    assert res["refusals"] == ["asset_digest_missing:runtime"]


def test_a25_malformed_digest_refused(tmp_path):
    scn = _single()
    for bad in ("sha256:xyz", "md5:" + "0" * 32, "sha256:nothex" + "0" * 56):
        meta = copy.deepcopy(scn["meta"])
        meta["assets"][0]["digest"] = bad
        res = _run(scn, tmp_path, meta=meta)
        assert res["refusals"] == ["asset_digest_malformed:runtime"], bad


def test_a26_invalid_or_boolean_size_refused(tmp_path):
    scn = _single()
    for bad in (True, "123", -5, 1.5):
        meta = copy.deepcopy(scn["meta"])
        meta["assets"][0]["size"] = bad
        res = _run(scn, tmp_path, meta=meta)
        assert res["refusals"] == ["asset_size_invalid:runtime"], bad


def test_a27_unsafe_or_unsupported_url_refused(tmp_path):
    scn = _single()
    for bad in ("http://x/runtime.zip", "ftp://x/a",
                "not a url", "https://other.runtime.zip"):
        meta = copy.deepcopy(scn["meta"])
        meta["assets"][0]["browser_download_url"] = bad
        res = _run(scn, tmp_path, meta=meta)
        assert res["refusals"][0] in (
            "asset_url_unsafe:runtime",
            "asset_url_changed:runtime"), bad


def test_a28_no_newer_version_or_cross_flavor(tmp_path):
    scn = _single()
    meta = copy.deepcopy(scn["meta"])
    meta["release_id"] = 2
    res = _run(scn, tmp_path, meta=meta)
    assert res["refusals"] == ["release_id_mismatch"]
    meta = copy.deepcopy(scn["meta"])
    meta["tag_name"] = "v2"
    res = _run(scn, tmp_path, meta=meta)
    assert res["refusals"] == ["release_tag_mismatch"]
    # Cross-flavor: a permit for a different flavor cannot match.
    p = _permit(scn, ["runtime"])
    p["flavor"] = "vulkan"
    res = _run(scn, tmp_path,
               authority=ra.SyntheticAcquisitionAuthority([p]))
    assert res["refusals"] == ["authority_association_unmatched"]


def test_a29_provenance_claims_not_upgraded(tmp_path):
    res = _run(_single(), tmp_path)
    assert res["ok"]
    # A matching archive digest does not upgrade provenance,
    # authority class or operational state.
    assert res["authority"]["class"] == "synthetic_acquisition"
    assert res["operational_qualification"] == "unestablished"
    assert "provenance" not in res["archives"][0]
    assert res["production_trust"] is False


# ================================================================ A30–A46
# transport, resume and archive verification


def test_a30_valid_bounded_stream(tmp_path):
    scn = _single()
    data = scn["zips"]["runtime"]
    res = _run(scn, tmp_path,
               transport=lambda u: iter(_chunks(data, sizes=(3,))))
    assert res["ok"]


def test_a31_truncated_stream_refused(tmp_path):
    scn = _single()
    short = scn["zips"]["runtime"][:-4]
    res = _run(scn, tmp_path,
               transport=tweak_tr(scn, short))
    assert res["state"] == "refused"
    assert res["refusals"] == ["archive_truncated"]


def test_a32_oversized_stream_refused(tmp_path):
    scn = _single()
    big = scn["zips"]["runtime"] + b"x"
    res = _run(scn, tmp_path, transport=tweak_tr(scn, big))
    assert res["refusals"] == ["archive_over_expected_size"]


def test_a33_invalid_chunk_refused(tmp_path):
    scn = _single()
    res = _run(scn, tmp_path,
               transport=lambda u: iter([b"ab", "notbytes", b"cd"]))
    assert res["refusals"] == ["invalid_chunk"]


def test_a34_nonprogressing_stream_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "_MAX_EMPTY_CHUNKS", 4)
    scn = _single()
    res = _run(scn, tmp_path,
               transport=lambda u: iter([b""] * 10
                                        + [scn["zips"]["runtime"]]))
    assert res["refusals"] == ["stream_not_progressing"]


def test_a35_transport_failure_controlled(tmp_path):
    scn = _single()

    def boom(url):
        raise OSError("net down")
    res = _run(scn, tmp_path / "a", transport=boom)
    assert res["state"] == "failed"
    assert res["refusals"] == ["operation_failed:OSError"]
    res = _run(scn, tmp_path / "b", transport=lambda u: None)
    assert res["refusals"] == ["transport_not_iterable"]


class _StreamCancelled(Exception):
    pass


def test_a36_cancellation_during_acquisition(tmp_path):
    scn = _single()
    data = scn["zips"]["runtime"]
    chunks = _chunks(data, sizes=(4,))
    calls = [0]

    def cc():
        calls[0] += 1
        if calls[0] == 2:
            raise _StreamCancelled("stop")
        return False
    res = _run(scn, tmp_path,
               transport=lambda u: iter(chunks),
               cancel_check=cc)
    assert res["state"] == "cancelled"


def test_a37_cancellation_during_hashing(tmp_path):
    scn = _single()
    calls = [0]

    def cc():
        calls[0] += 1
        return calls[0] >= 2   # cancel on second stream chunk
    data = scn["zips"]["runtime"]
    res = _run(scn, tmp_path,
               transport=lambda u: iter(_chunks(data, sizes=(4,))),
               cancel_check=cc)
    assert res["state"] == "cancelled"


def test_a38_archive_sha_mismatch_refused(tmp_path):
    scn = _single()
    other = bytearray(scn["zips"]["runtime"])
    other[len(other) // 2] ^= 0xFF
    res = _run(scn, tmp_path, transport=tweak_tr(scn, bytes(other)))
    assert res["refusals"] == ["archive_digest_mismatch:runtime"]
    rec = res["archives"][0]
    assert rec["comparison"] == "digest_mismatch"
    assert rec["expected_sha256"] != rec["observed_sha256"]


def test_a39_aggregate_archive_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "_MAX_AGGREGATE_ARCHIVES", 8)
    res = _run(_multi(), tmp_path)
    assert res["refusals"] == ["aggregate_archives_over_bound"]


def test_a40_exact_resume_association_accepted(tmp_path):
    scn = _single()
    res = _run(scn, tmp_path)
    assoc = ra.resume_identity(
        res["acquisition"], "runtime",
        {"asset_id": 1, "name": "runtime.zip",
         "size": len(scn["zips"]["runtime"]),
         "sha256": _sha(scn["zips"]["runtime"]),
         "source": "https://x/runtime.zip"},
        rr.reference_digest(scn["ref"]))
    out = ra.validate_resume(dict(assoc), assoc)
    assert out["ok"]


def test_a41_foreign_resume_identity_refused(tmp_path):
    scn = _single()
    res = _run(scn, tmp_path)
    assoc = ra.resume_identity(
        res["acquisition"], "runtime",
        {"asset_id": 1, "name": "runtime.zip",
         "size": len(scn["zips"]["runtime"]),
         "sha256": _sha(scn["zips"]["runtime"]),
         "source": "https://x/runtime.zip"},
        rr.reference_digest(scn["ref"]))
    foreign = dict(assoc, asset_id=7)
    out = ra.validate_resume(foreign, assoc)
    assert out["refusals"] == ["resume_identity_mismatch:asset_id"]


def test_a42_changed_digest_invalidates_resume(tmp_path):
    scn = _single()
    res = _run(scn, tmp_path)
    assoc = ra.resume_identity(
        res["acquisition"], "runtime",
        {"asset_id": 1, "name": "runtime.zip",
         "size": len(scn["zips"]["runtime"]),
         "sha256": _sha(scn["zips"]["runtime"]),
         "source": "https://x/runtime.zip"},
        rr.reference_digest(scn["ref"]))
    changed = dict(assoc, expected_sha256="1" * 64)
    out = ra.validate_resume(changed, assoc)
    assert out["refusals"] == [
        "resume_identity_mismatch:expected_sha256"]


def test_a43_malformed_resume_metadata_refused(tmp_path):
    for bad in ("x", {"a": 1}, {"reference_digest": "rref:v0:x",
                               "acquisition": "racq:v0:y",
                               "component": "c",
                               "release_id": 1, "asset_id": 1,
                               "asset_name": "n", "expected_size": True,
                               "expected_sha256": "0" * 64,
                               "source": "https://x/a"}):
        out = ra.validate_resume(bad, {})
        assert out["state"] == "refused"
    meta = {"reference_digest": "rref:v0:" + "0" * 64,
            "acquisition": "racq:v0:" + "0" * 64,
            "component": "c", "release_id": 1, "asset_id": 1,
            "asset_name": "n", "expected_size": True,
            "expected_sha256": "0" * 64, "source": "https://x/a"}
    out = ra.validate_resume(meta, dict(meta))
    assert out["refusals"] == ["resume_malformed:expected_size"]


def test_a44_no_extraction_before_all_archives_verify(tmp_path):
    scn = _multi()
    bad = bytearray(scn["zips"]["cudart"])
    bad[len(bad) // 2] ^= 0xFF
    urls = dict(scn["urls"])
    urls["https://x/cudart.zip"] = bytes(bad)
    res = _run(scn, tmp_path, transport=lambda u: iter([urls[u]]),
               selection=_sel(scn, opt=False))
    assert res["refusals"] == ["archive_digest_mismatch:cudart"]
    assert res["members"] == []
    members_dir = _staging_dir(res, tmp_path) / "members"
    assert not members_dir.exists() or not any(
        members_dir.iterdir())


def test_a45_verification_extraction_association_file_path(
        tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "_MAX_IN_MEMORY_ARCHIVE", -1)
    res = _run(_multi(), tmp_path)
    assert res["ok"]
    # The staged archive file was reopened through the association
    # check and extracted — members match.
    assert all(m["state"] == "match" for m in res["members"])


def test_a46_replaced_or_contradictory_association_refused(tmp_path):
    p = tmp_path / "a.zip"
    data = b"0123456789"
    p.write_bytes(data)
    with open(p, "rb") as fh0:
        baseline = ra._baseline_of(os.fstat(fh0.fileno()))
    fh, why = ra._open_archive_assoc(p, 9, _sha(data), baseline, None)
    assert fh is None and why == "archive_association_size_mismatch"
    fh, why = ra._open_archive_assoc(tmp_path / "missing.zip",
                                     9, "0" * 64, baseline, None)
    assert fh is None and why == "archive_stat_failed"
    # directory substitution: open succeeds on POSIX but is not a zip —
    # on Windows open() fails outright; either way it must refuse.
    fh, why = ra._open_archive_assoc(tmp_path, 0, "0" * 64,
                                     baseline, None)
    assert fh is None
    # The verified baseline + rehash accept an unchanged file.
    fh, why = ra._open_archive_assoc(p, 10, _sha(data), baseline, None)
    assert fh is not None and why is None
    fh.close()


# ================================================================ A47–A80
# archive preflight and extraction


def test_a47_valid_flat_archive_accepted(tmp_path):
    res = _run(_single(), tmp_path)
    assert res["ok"]


@pytest.mark.parametrize("bad", ["../evil.exe", "..\\evil",
                                 "a/../b", ".. /x"])
def test_a48_traversal_refused(tmp_path, bad):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd"), (bad, b"e")])],
              members=[_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"][0].startswith("member_name_invalid")


@pytest.mark.parametrize("bad", ["/abs/path", "/etc/passwd"])
def test_a49_absolute_path_refused(tmp_path, bad):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd"), (bad, b"e")])],
              members=[_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"][0].startswith("member_name_invalid")


@pytest.mark.parametrize("bad", ["C:\\evil.dll", "C:/evil.dll",
                                 "d:evil"])
def test_a50_drive_qualified_refused(tmp_path, bad):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd"), (bad, b"e")])],
              members=[_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"][0].startswith("member_name_invalid"), bad


def test_a51_unc_path_refused(tmp_path):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd"), ("\\\\srv\\share\\e.dll", b"e")])],
              members=[_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"][0].startswith("member_name_invalid")


def test_a52_alternate_data_stream_refused(tmp_path):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd"), ("file.txt:stream", b"e")])],
              members=[_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"][0].startswith("member_name_invalid")


def test_a53_nested_path_refused(tmp_path):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd"), ("sub/dir/f.dll", b"e")])],
              members=[_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"][0].startswith("member_name_invalid")


@pytest.mark.parametrize("bad", ["con.exe", "COM1.dll", "nul",
                                 "lpt9.dat", "aux.txt"])
def test_a54_reserved_windows_name_refused(tmp_path, bad):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd"), (bad, b"e")])],
              members=[_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"][0].startswith("member_name_invalid"), bad


@pytest.mark.parametrize("bad", ["sd.exe.", "sd.exe ", "note.txt  "])
def test_a55_trailing_dot_or_space_refused(tmp_path, bad):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd"), (bad, b"e")])],
              members=[_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"][0].startswith("member_name_invalid")


def test_a56_duplicate_entry_refused(tmp_path):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd"), ("sd.exe", b"sd2")])],
              members=[_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"][0].startswith("duplicate_member")


def test_a57_case_insensitive_collision_refused(tmp_path):
    scn = _mk([("runtime", "primary", True, 1,
                [("SD.EXE", b"a"), ("sd.exe", b"b")])],
              members=[_member("SD.EXE", "runtime", b"a")])
    res = _run(scn, tmp_path)
    assert res["refusals"][0].startswith("duplicate_member")


def test_a58_cross_archive_collision_refused(tmp_path):
    # runtime declares shared.bin; the dependency archive also carries
    # it — same destination claimed by two archives.
    # Component processing order is the reference's deterministic
    # component-name order - the owner ("art") must preflight first.
    scn = _mk(
        [("art", "primary", True, 1,
          [("shared.bin", b"rt")]),
         ("dep", "dependency", True, 2,
          [("shared.bin", b"dep"), ("dep.dll", b"d")])],
        members=[_member("shared.bin", "art", b"rt"),
                 _member("dep.dll", "dep", b"d")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["cross_archive_collision:shared.bin"]


def test_a59_unexpected_member_refused(tmp_path):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd"), ("evil.dll", b"e")])],
              members=[_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["unexpected_member:evil.dll"]


def test_a60_missing_expected_member_refused(tmp_path):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd")])],
              members=[_member("sd.exe", "runtime", b"sd"),
                       _member("missing.dll", "runtime", b"m")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["missing_expected_member:missing.dll"]


def test_a61_ambiguous_member_ownership_refused(tmp_path):
    # A member declared under runtime but physically present in the
    # dependency archive cannot claim runtime ownership.
    scn = _mk(
        [("runtime", "primary", True, 1, [("sd.exe", b"sd")]),
         ("dep", "dependency", True, 2,
          [("shared.dll", b"dep"), ("dep.dll", b"d")])],
        members=[_member("sd.exe", "runtime", b"sd"),
                 _member("shared.dll", "runtime", b"rt"),
                 _member("dep.dll", "dep", b"d")])
    res = _run(scn, tmp_path)
    assert res["refusals"][0].startswith(
        ("unexpected_member", "cross_archive_collision",
         "missing_expected_member"))


def test_a62_unsupported_special_entry_refused(tmp_path):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd"),
                 ("fifo", b"e", {"create_system": 3,
                                 "external_attr": (0o010644 << 16)})])],
              members=[_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["member_special_entry:fifo"]


def test_a63_symlink_entry_refused(tmp_path):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd"),
                 ("link.dll", b"/t", {"create_system": 3,
                                      "external_attr": (0o120777 << 16)})])],
              members=[_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["member_special_entry:link.dll"]


def test_a64_encrypted_or_unsupported_compression_refused(tmp_path):
    scn = _single()
    zb = _patch_flag(scn["zips"]["runtime"], 0x1)
    scn = _mk_from_bytes("runtime", zb,
                         [_member("sd.exe", "runtime", b"sd-bytes")])
    res = _run(scn, tmp_path)
    assert res["refusals"][0] == "member_encrypted:sd.exe"
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd-bytes",
                  {"compress_type": zipfile.ZIP_BZIP2})])],
              members=[_member("sd.exe", "runtime", b"sd-bytes")])
    res = _run(scn, tmp_path)
    # The refused entry was the expected member — the preflight
    # coverage check reports its absence too.
    assert res["refusals"] == [
        "member_compression_unsupported:sd.exe",
        "missing_expected_member:sd.exe"]


def test_a65_corrupt_archive_refused(tmp_path):
    good = _z([("sd.exe", b"sd-bytes")])
    bad = good[:len(good) // 2]
    scn = _mk_from_bytes("runtime", bad,
                         [_member("sd.exe", "runtime", b"sd-bytes")])
    res = _run(scn, tmp_path)
    # The truncated archive has no locatable EOCD record — the bounded
    # preliminary directory assessment refuses before the parser runs.
    assert res["refusals"] == ["archive_directory_missing"]


def test_a66_local_central_contradiction_refused(tmp_path):
    zb = _patch_local_name(_z([("sd.exe", b"sd-bytes")]))
    scn = _mk_from_bytes("runtime", zb,
                         [_member("sd.exe", "runtime", b"sd-bytes")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == [
        "archive_local_central_mismatch:sd.exe",
        "missing_expected_member:sd.exe"]


def test_a67_entry_count_bound(tmp_path, monkeypatch):
    # The declared EOCD entry count is patched down to slip under the
    # bound so the POST-PARSER actual count check is what fires —
    # the two bounds are enforced independently.
    monkeypatch.setattr(ra, "_MAX_ARCHIVE_ENTRIES", 1)
    zb = _patch_eocd(_z([("a.dll", b"a"), ("b.dll", b"b")]),
                     entries=1)
    scn = _mk_from_bytes("runtime", zb,
                         [_member("a.dll", "runtime", b"a"),
                          _member("b.dll", "runtime", b"b")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["archive_entries_over_bound"]


def test_a68_member_expanded_size_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "_MAX_MEMBER_BYTES", 3)
    res = _run(_single(member_data=b"toolong"), tmp_path)
    assert res["refusals"][0].startswith("member_")


def test_a69_aggregate_expanded_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "_MAX_EXPANDED_BYTES", 10)
    res = _run(_multi(), tmp_path)
    assert res["refusals"] == ["aggregate_expanded_over_bound"]


def test_a70_compression_ratio_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "_MAX_RATIO", 1)
    res = _run(_single(member_data=b"a" * 4096), tmp_path)
    assert res["refusals"][0].startswith("member_ratio_abuse")


def test_a71_explicit_permitted_skip_accepted(tmp_path):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd"), ("notes.txt", b"n")])],
              members=[_member("sd.exe", "runtime", b"sd")],
              skips=["notes.txt"])
    res = _run(scn, tmp_path)
    assert res["ok"]
    assert res["skipped"] == [{"name": "notes.txt",
                               "component": "runtime",
                               "state": "skipped_declared"}]


def test_a72_unexpected_purported_skip_refused(tmp_path):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd"), ("readme.md", b"r")])],
              members=[_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["unexpected_member:readme.md"]


def test_a73_skipped_entry_not_written(tmp_path):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd"), ("notes.txt", b"n")])],
              members=[_member("sd.exe", "runtime", b"sd")],
              skips=["notes.txt"])
    res = _run(scn, tmp_path)
    assert res["ok"]
    members = _staging_dir(res, tmp_path) / "members"
    assert not (members / "notes.txt").exists()
    assert (members / "sd.exe").exists()


def test_a74_existing_destination_not_overwritten(tmp_path):
    members_dir = tmp_path / "members"
    members_dir.mkdir()
    (members_dir / "sd.exe").write_bytes(b"pre-existing")
    scn = _single()
    zf = zipfile.ZipFile(io.BytesIO(scn["zips"]["runtime"]))
    _rec, why = ra._extract_member(
        zf, zf.infolist()[0],
        {"name": "sd.exe", "component": "runtime",
         "size": 8, "sha256": _sha(b"sd-bytes")},
        members_dir, None)
    assert why == "destination_exists"
    assert (members_dir / "sd.exe").read_bytes() == b"pre-existing"


def test_a75_unsafe_destination_association_refused(tmp_path):
    scn = _single()
    res = _run(scn, tmp_path)
    assert res["ok"]
    # Re-running into the same staging root refuses — the controlled
    # destination is never reused or overwritten.
    res2 = _run(scn, tmp_path)
    assert res2["refusals"] == ["staging_destination_exists"]
    res3 = ra.acquire(scn["ref"], authority=_auth(scn, ["runtime"]),
                      selection={"runtime": True},
                      release_metadata=scn["meta"],
                      transport=_transport(scn), staging_root=None)
    assert res3["refusals"] == ["staging_root_absent"]
    f = tmp_path / "not-a-dir"
    f.write_text("x")
    res4 = ra.acquire(scn["ref"], authority=_auth(scn, ["runtime"]),
                      selection={"runtime": True},
                      release_metadata=scn["meta"],
                      transport=_transport(scn), staging_root=f)
    assert res4["refusals"] == ["staging_root_unusable"]


def test_a76_member_size_mismatch_refused(tmp_path):
    m = _member("sd.exe", "runtime", b"sd-bytes")
    m["size"] += 1
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd-bytes")])],
              members=[m])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["member_size_mismatch:sd.exe",
                               "missing_expected_member:sd.exe"]


def test_a77_member_digest_mismatch_refused(tmp_path):
    m = _member("sd.exe", "runtime", b"sd-bytes")
    m["sha256"] = "0" * 64
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd-bytes")])],
              members=[m])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["member_sha256_mismatch:sd.exe"]


def test_a78_cancellation_during_extraction(tmp_path):
    import random
    scn = _single(member_data=random.Random(42).randbytes(
        2 * 1024 * 1024 + 7))
    calls = [0]

    def cc():
        calls[0] += 1
        # stream calls for 1-chunk archive + entry checks pass; cancel
        # inside the member read loop.
        return calls[0] >= 4
    res = _run(scn, tmp_path, cancel_check=cc)
    assert res["state"] == "cancelled"
    assert res["ok"] is False


def test_a79_complete_selected_member_coverage(tmp_path):
    res = _run(_multi(), tmp_path)
    assert res["ok"]
    assert res["coverage"]["selected_complete"] is True
    assert res["coverage"]["declared_complete"] is True
    assert res["coverage"]["upstream_completeness"] == "unestablished"
    assert {m["name"] for m in res["members"]} == \
        {"sd.exe", "sd.dll", "cudart.dll", "opt.txt"}


def test_a80_partial_coverage_cannot_report_success(tmp_path):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd")])],
              members=[_member("sd.exe", "runtime", b"sd"),
                       _member("missing.dll", "runtime", b"m")])
    res = _run(scn, tmp_path)
    assert res["ok"] is False
    assert res["state"] == "refused"
    assert res["coverage"] is None or \
        res["coverage"]["selected_complete"] is not True


# ================================================================ A81–A96
# non-activation and preservation


def test_a81_inputs_not_mutated(tmp_path):
    scn = _multi()
    ref0 = copy.deepcopy(scn["ref"])
    meta0 = copy.deepcopy(scn["meta"])
    sel = _sel(scn)
    sel0 = copy.deepcopy(sel)
    _run(scn, tmp_path, selection=sel)
    assert scn["ref"] == ref0
    assert scn["meta"] == meta0
    assert sel == sel0


def test_a82_diagnostics_deterministic_and_sanitized(tmp_path):
    scn = _single()
    res = _run(scn, tmp_path,
               transport=tweak_tr(scn, b"short"))
    assert res["refusals"] == ["archive_truncated"]
    res2 = _run(scn, tmp_path / "other",
                transport=tweak_tr(scn, b"short"))
    assert res2["refusals"] == res["refusals"]
    for r in res["refusals"]:
        assert len(r) <= 160
        assert str(tmp_path) not in r
        assert "\\" not in r


def test_a83_malformed_and_oversized_inputs_bounded(tmp_path):
    scn = _single()
    for bad_meta in ("x", [1] * 9, [{"release_id": 1}] + ["z"]):
        res = _run(scn, tmp_path / str(len(str(bad_meta))),
                   meta=bad_meta)
        assert res["state"] == "refused", bad_meta
    res = _run(scn, tmp_path / "s1", selection=[True])
    assert res["refusals"] == ["selection_not_mapping"]
    res = ra.acquire("not-a-dict", authority=None, selection={},
                     release_metadata={}, transport=lambda u: iter([]),
                     staging_root=tmp_path / "s2")
    assert res["refusals"][0] == "reference_invalid"


def test_a84_no_production_network_surface():
    src = Path(ra.__file__).read_text()
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    for banned in ("socket", "urllib", "requests", "httpx",
                   "http", "ssl", "aiohttp"):
        assert banned not in imported


def test_a85_no_production_runtime_or_model_fs_access():
    src = Path(ra.__file__).read_text()
    for banned in ("runtimes_root", "models_root", "model_files",
                   "model_catalog", "ModelDownloadAdapter",
                   "RuntimeInstall", "smoke_check_runtime"):
        assert banned not in src


def test_a86_no_runtime_execution_surface():
    src = Path(ra.__file__).read_text()
    for banned in ("subprocess", "os.system", "Popen",
                   "os.exec", "run("):
        assert banned not in src


def test_a87_no_production_import_or_activation():
    app_dir = Path(ra.__file__).resolve().parents[2]
    hits = []
    for f in app_dir.rglob("*.py"):
        if "runtime_acquisition" in f.name:
            continue
        txt = f.read_text(encoding="utf-8", errors="replace")
        if "runtime_acquisition" in txt:
            hits.append(f.name)
    assert hits == []


def test_a88_no_reference_approval_or_activation():
    src = Path(ra.__file__).read_text()
    for banned in ("ReferenceRegistry", "approve(",
                   "approve_ref", "activate"):
        assert banned not in src


def test_a89_no_installation_publication(tmp_path):
    res = _run(_single(), tmp_path)
    assert res["ok"]
    assert res["installation_publication"] == "not_performed"
    members = _staging_dir(res, tmp_path) / "members"
    for f in members.iterdir():
        assert tmp_path in f.parents or f.parent == members


def test_a90_no_automatic_generation_selection(tmp_path):
    res = _run(_single(), tmp_path)
    assert res["ok"]
    assert "generation" not in res
    assert "selected_generation" not in str(res)


def test_a91_no_unauthorized_cleanup():
    src = Path(ra.__file__).read_text()
    for banned in ("shutil", ".unlink", "rmtree", "os.remove",
                   "os.rmdir"):
        assert banned not in src


def test_a92_all_envelopes_non_executable(tmp_path):
    scn = _single()
    ok = _run(scn, tmp_path / "a")
    refused = _run(scn, tmp_path / "b",
                   transport=tweak_tr(scn, b"short"))
    cancelled = _run(scn, tmp_path / "c",
                     cancel_check=lambda: True)
    def boom(u):
        raise RuntimeError("x")
    failed = _run(scn, tmp_path / "d", transport=boom)
    for res in (ok, refused, cancelled, failed):
        assert res["production_trust"] is False
        assert res["execution_eligible"] is False
        assert res["installation_publication"] == "not_performed"
        assert res["trusted_enrollment"] == "unestablished"
        assert res["operational_qualification"] == "unestablished"


def test_a93_rref_v0_contract_preserved(tmp_path):
    assert rr.RREF_VERSION == "rref:v0"
    scn = _single()
    assert rr.reference_digest(scn["ref"]).startswith("rref:v0:")
    # A structurally valid unapproved reference is usable for
    # synthetic comparison but is never promoted.
    res = _run(scn, tmp_path)
    assert res["ok"]
    assert res["trusted_enrollment"] == "unestablished"


def test_a94_rtq_and_lcfg_contracts_preserved():
    src = Path(ra.__file__).read_text()
    # The module may reuse only pure validators — never the integrity
    # qualification engine or device binding.
    assert "device_bind" not in src
    tree = ast.parse(src)
    mods = [n.module for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom)]
    assert set(mods) <= {"__future__", "pathlib",
                         "app.domain.runtime_integrity",
                         "app.domain"}
    from app.domain import runtime_integrity as ri
    assert ri.CONTRACT == "rtq:v1"
    from app.domain import device_bind as db
    assert db.LCFG_CONTRACT == "lcfg:v1"


def test_a95_no_migration_or_persistence_changes():
    src = Path(ra.__file__).read_text()
    for banned in ("sqlalchemy", "Session", "alembic",
                   "create_engine", ".db"):
        assert banned not in src


def test_a96_no_scheduler_watchdog_reservation_changes():
    src = Path(ra.__file__).read_text()
    for banned in ("gpu_sched", "watchdog", "reservation",
                   "persistent_worker", "Scheduler"):
        assert banned not in src


# ================================================================ C1
# verified archive content association


def _force_file(monkeypatch):
    monkeypatch.setattr(ra, "_MAX_IN_MEMORY_ARCHIVE", -1)


def _no_member_writes(res, tmp_path):
    members = tmp_path / res["staging"]["dir"] / "members"
    return not members.exists() or not any(members.iterdir())


def test_c1_01_memory_backed_association_evidence(tmp_path):
    res = _run(_single(), tmp_path)
    assert res["ok"]
    rec = res["archives"][0]
    assert rec["comparison"] == "match"
    assert rec["reopen_comparison"] == "memory_backed"
    assert rec["final_comparison"] == "memory_backed"


def test_c1_02_file_backed_association_evidence(tmp_path, monkeypatch):
    _force_file(monkeypatch)
    res = _run(_single(), tmp_path)
    assert res["ok"]
    rec = res["archives"][0]
    assert rec["comparison"] == "match"
    assert rec["reopen_comparison"] == "match"
    assert rec["final_comparison"] == "match"


def test_c1_03_inplace_same_size_modification_refused(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    monkeypatch.setattr(
        ra, "_TEST_SEAM",
        lambda point, **kw: _flip(kw["path"])
        if point == "stream_closed" else None)
    res = _run(_single(), tmp_path)
    assert res["state"] == "refused"
    assert res["refusals"][0] in ("archive_association_changed",
                                  "archive_rehash_mismatch")
    assert res["production_trust"] is False


def test_c1_04_same_size_replacement_refused(tmp_path, monkeypatch):
    _force_file(monkeypatch)
    scn = _single()
    other = bytearray(scn["zips"]["runtime"])
    other[len(other) // 2] ^= 0xFF
    monkeypatch.setattr(
        ra, "_TEST_SEAM",
        lambda point, **kw: kw["path"].write_bytes(bytes(other))
        if point == "stream_closed" else None)
    res = _run(scn, tmp_path)
    assert res["state"] == "refused"
    assert res["refusals"][0] in ("archive_association_changed",
                                  "archive_rehash_mismatch")


def test_c1_05_replacement_before_reopen_refused(tmp_path, monkeypatch):
    _force_file(monkeypatch)
    scn = _single()
    other = bytearray(scn["zips"]["runtime"])
    other[0] ^= 0x01   # still a zip-ish size-equal replacement
    alt = tmp_path / "alt.zip"
    alt.write_bytes(bytes(other))

    def hook(point, **kw):
        if point == "stream_closed":
            os.replace(alt, kw["path"])   # new inode entirely
    monkeypatch.setattr(ra, "_TEST_SEAM", hook)
    res = _run(scn, tmp_path)
    assert res["state"] == "refused"
    assert res["refusals"][0] in ("archive_association_changed",
                                  "archive_rehash_mismatch")


def test_c1_06_skipped_only_content_modification_refused(
        tmp_path, monkeypatch):
    """A same-size, same-inode, restored-mtime replacement that keeps
    every managed member byte-exact but alters a declared-skipped
    entry is caught by content re-verification — stat evidence alone
    never proves bytes."""
    _force_file(monkeypatch)
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd"), ("notes.txt", b"notes")])],
              members=[_member("sd.exe", "runtime", b"sd")],
              skips=["notes.txt"])
    other = _z([("sd.exe", b"sd"), ("notes.txt", b"NOTES")])
    assert len(other) == len(scn["zips"]["runtime"])

    def hook(point, **kw):
        if point != "stream_closed":
            return
        p = kw["path"]
        st = p.stat()
        p.write_bytes(other)            # same size, same inode
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    monkeypatch.setattr(ra, "_TEST_SEAM", hook)
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["archive_rehash_mismatch"]
    assert res["members"] == []


def test_c1_07_persistent_modification_before_extraction(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    # Member data exceeds the reader's buffer so a persistent mid-
    # member modification is necessarily re-read from disk.
    data = b"sd-bytes" * 3000
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", data,
                  {"compress_type": zipfile.ZIP_STORED})])],
              members=[_member("sd.exe", "runtime", data)])
    data_off = scn["zips"]["runtime"].find(data[:8]) + 10000
    monkeypatch.setattr(
        ra, "_TEST_SEAM",
        lambda point, **kw: _flip(kw["path"], data_off)
        if point == "pre_extract" else None)
    res = _run(scn, tmp_path)
    assert res["state"] == "refused"
    # The buffered member read may legitimately serve the verified
    # bytes — the refusal may then come from the final association or
    # raw-fd content rehash instead of the member digest check.
    assert res["refusals"][0] in (
        "member_sha256_mismatch:sd.exe", "member_corrupt:sd.exe",
        "archive_association_changed", "archive_rehash_mismatch")


def test_c1_08_persistent_modification_during_extraction(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    data = b"sd-bytes" * 3000
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", data,
                  {"compress_type": zipfile.ZIP_STORED})])],
              members=[_member("sd.exe", "runtime", data)])
    data_off = scn["zips"]["runtime"].find(data[:8]) + 10000
    monkeypatch.setattr(
        ra, "_TEST_SEAM",
        lambda point, **kw: _flip(kw["path"], data_off)
        if point == "pre_member" else None)
    res = _run(scn, tmp_path)
    assert res["state"] == "refused"
    assert res["refusals"][0] in (
        "member_sha256_mismatch:sd.exe", "member_corrupt:sd.exe",
        "archive_association_changed", "archive_rehash_mismatch")


def test_c1_09_zero_inode_does_not_bypass_content_check(tmp_path):
    """Degraded stat evidence (st_ino == 0) never bypasses content
    verification — the rehash still catches a same-size, restored-
    mtime modification."""
    p = tmp_path / "a.zip"
    data = b"0123456789"
    p.write_bytes(data)
    with open(p, "rb") as fh0:
        baseline = ra._baseline_of(os.fstat(fh0.fileno()))
    baseline["ino"] = 0     # simulate unavailable inode evidence
    st = p.stat()
    _flip(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    fh, why = ra._open_archive_assoc(p, 10, _sha(data), baseline, None)
    assert fh is None and why == "archive_rehash_mismatch"


def test_c1_10_cancel_during_reopened_rehash(tmp_path, monkeypatch):
    _force_file(monkeypatch)
    calls = [0]

    def cc():
        calls[0] += 1
        # stream chunk check is call 1; the reopened-handle rehash
        # loop starts at call 2.
        return calls[0] >= 2
    res = _run(_single(), tmp_path, cancel_check=cc)
    assert res["state"] == "cancelled" and res["ok"] is False


def test_c1_11_cancel_during_final_verification(tmp_path, monkeypatch):
    _force_file(monkeypatch)
    scn = _single()
    calls = [0]

    def count():
        calls[0] += 1
        return False
    res = _run(scn, tmp_path / "a", cancel_check=count)
    assert res["ok"]
    total = calls[0]
    calls2 = [0]

    def cc():
        calls2[0] += 1
        return calls2[0] >= total   # the last check call sits inside
        # the final association rehash
    res2 = _run(scn, tmp_path / "b", cancel_check=cc)
    assert res2["state"] == "cancelled" and res2["ok"] is False


def test_c1_12_final_failure_preserves_partial_evidence(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    monkeypatch.setattr(
        ra, "_TEST_SEAM",
        lambda point, **kw: _flip(kw["path"], -1)
        if point == "pre_final" else None)
    res = _run(_single(), tmp_path)
    assert res["state"] == "refused"
    assert res["refusals"][0] in ("archive_association_changed",
                                  "archive_rehash_mismatch")
    # The member was fully written and verified before the final
    # association check failed — evidence must not hide that.
    assert [m["name"] for m in res["members"]] == ["sd.exe"]
    rec = res["archives"][0]
    assert rec["final_comparison"] in ("archive_association_changed",
                                     "archive_rehash_mismatch")
    assert res["staging"]["complete"] is False


def test_c1_13_same_handle_preflight_and_extraction(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    scn = _single()
    seen = {}
    orig_assoc = ra._open_archive_assoc

    def assoc_spy(*a, **k):
        fh, why = orig_assoc(*a, **k)
        seen["fh"] = fh
        return fh, why
    monkeypatch.setattr(ra, "_open_archive_assoc", assoc_spy)
    orig_zf = zipfile.ZipFile

    class _ZSpy(orig_zf):
        def __init__(self, f, *a, **k):
            super().__init__(f, *a, **k)
            seen.setdefault("zf_fp", f)
    monkeypatch.setattr(zipfile, "ZipFile", _ZSpy)
    res = _run(scn, tmp_path)
    assert res["ok"]
    # The parser was constructed over exactly the verified handle.
    assert seen["zf_fp"] is seen["fh"]


def test_c1_14_member_sha256_mandatory_on_file_path(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    m = _member("sd.exe", "runtime", b"sd-bytes")
    m["sha256"] = "0" * 64
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd-bytes")])], members=[m])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["member_sha256_mismatch:sd.exe"]


# ================================================================ C2
# known revocation and synthetic authority


def test_c2_01_revoked_with_matching_permit_refused(tmp_path):
    scn = _single()
    auth = _auth(scn, ["runtime"],
                 revoked=[rr.reference_digest(scn["ref"])])
    res = _run(scn, tmp_path, authority=auth)
    assert res["refusals"] == ["authority_revoked"]
    assert res["authority"]["association"] == "revoked"
    assert res["production_trust"] is False


def test_c2_02_revoked_without_permit_precedence(tmp_path):
    scn = _single()
    auth = ra.SyntheticAcquisitionAuthority(
        [], revoked=[rr.reference_digest(scn["ref"])])
    res = _run(scn, tmp_path, authority=auth)
    # Revocation precedes permit association — never "unmatched".
    assert res["refusals"] == ["authority_revoked"]


def test_c2_03_04_revoked_no_transport_no_artifacts(tmp_path):
    scn = _single()
    calls = []
    auth = _auth(scn, ["runtime"],
                 revoked=[rr.reference_digest(scn["ref"])])

    def spy(url):
        calls.append(url)
        return iter([])
    res = _run(scn, tmp_path, authority=auth, transport=spy)
    assert res["refusals"] == ["authority_revoked"]
    assert calls == []
    assert not any(tmp_path.iterdir())


def test_c2_05_other_digest_unaffected(tmp_path):
    scn = _single()
    other = _mk_from_bytes("runtime", _z([("x.bin", b"x")]),
                           [_member("x.bin", "runtime", b"x")])
    auth = _auth(scn, ["runtime"],
                 revoked=[rr.reference_digest(other["ref"])])
    res = _run(scn, tmp_path, authority=auth)
    assert res["ok"]
    # A structurally valid unapproved expectation set remains usable
    # when independently permitted and not known revoked.
    assert res["authority"]["association"] == "established"


def test_c2_07_caller_mutation_cannot_unrevoke(tmp_path):
    scn = _single()
    dig = rr.reference_digest(scn["ref"])
    rv = [dig]
    auth = _auth(scn, ["runtime"], revoked=rv)
    rv.clear()
    res = _run(scn, tmp_path, authority=auth)
    assert res["refusals"] == ["authority_revoked"]


def test_c2_08_malformed_revoked_digest_refused():
    for bad in ("x", "rref:v0:nothex", 42,
                "rref:v0:" + "0" * 64 + "z"):
        with pytest.raises(ValueError):
            ra.SyntheticAcquisitionAuthority([], revoked=[bad])
    with pytest.raises(TypeError):
        ra.SyntheticAcquisitionAuthority(
            [], revoked="rref:v0:" + "0" * 64)


def test_c2_09_revoked_bound_enforced(monkeypatch):
    monkeypatch.setattr(ra, "_MAX_REVOKED", 2)
    with pytest.raises(ValueError):
        ra.SyntheticAcquisitionAuthority(
            [], revoked=["rref:v0:" + "0" * 64] * 3)


def test_c2_10_no_self_declared_or_permit_carried_revocation():
    scn = _single()
    # A permit cannot smuggle revocation semantics — field set is
    # exact.
    p = _permit(scn, ["runtime"])
    p["revoked"] = True
    with pytest.raises(ValueError):
        ra.SyntheticAcquisitionAuthority([p])
    # Nor can a favorable authority-shaped flag bypass the context.
    res = ra.acquire(scn["ref"], authority={"revoked": []},
                     selection={"runtime": True},
                     release_metadata=scn["meta"],
                     transport=_transport(scn), staging_root=None)
    assert res["refusals"] == ["authority_context_invalid"]


# ================================================================ C3
# junction / reparse-point safety


def test_c3_01_ordinary_staging_root_accepted(tmp_path):
    res = _run(_single(), tmp_path)
    assert res["ok"]


def test_c3_02_root_symlink_refused_mocked(tmp_path, monkeypatch):
    # Mocked metadata coverage — no link-creation privilege needed.
    monkeypatch.setattr(Path, "is_symlink", lambda s: True)
    res = _run(_single(), tmp_path)
    assert res["refusals"] == ["staging_root_reparse"]
    # No output outside the owned workspace was produced.
    assert not any(tmp_path.iterdir())


def test_c3_03_root_junction_refused_mocked(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "is_symlink", lambda s: False)
    monkeypatch.setattr(os.path, "isjunction", lambda p: True)
    res = _run(_single(), tmp_path)
    assert res["refusals"] == ["staging_root_reparse"]


def test_c3_04_reparse_ancestor_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "_path_reparse",
                        lambda p: Path(p) == tmp_path.parent)
    res = _run(_single(), tmp_path)
    assert res["refusals"] == ["staging_root_reparse"]
    assert not any(tmp_path.iterdir())


def test_c3_05_06_reopened_archive_reparse_refused(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    monkeypatch.setattr(ra, "_path_reparse",
                        lambda p: str(p).endswith(".zip"))
    res = _run(_single(), tmp_path)
    assert res["refusals"] == ["archive_association_reparse"]
    assert res["archives"][0]["reopen_comparison"] == \
        "archive_association_reparse"


def test_c3_07_unsafe_member_directory_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "_path_reparse",
                        lambda p: Path(p).name == "members")
    res = _run(_single(), tmp_path)
    assert res["refusals"] == ["staging_dir_reparse"]
    assert res["members"] == []
    assert _no_member_writes(res, tmp_path)


@pytest.mark.parametrize("attr,why", [
    ("dest", "destination_reparse"),
    ("root", "destination_association_lost")])
def test_c3_08_destination_reparse_refused(tmp_path, monkeypatch,
                                           attr, why):
    members_dir = tmp_path / "members"
    members_dir.mkdir()
    if attr == "dest":
        monkeypatch.setattr(ra, "_path_reparse",
                            lambda p: Path(p) == members_dir / "sd.exe")
    else:
        monkeypatch.setattr(ra, "_path_reparse",
                            lambda p: Path(p) == members_dir)
    scn = _single()
    zf = zipfile.ZipFile(io.BytesIO(scn["zips"]["runtime"]))
    rec, got = ra._extract_member(
        zf, zf.infolist()[0],
        {"name": "sd.exe", "component": "runtime",
         "size": 8, "sha256": _sha(b"sd-bytes")},
        members_dir, None)
    assert rec is None and got == why
    # Foreign destination content is never touched — nothing created.
    assert not (members_dir / "sd.exe").exists()


def test_c3_11_reparse_metadata_not_ignored(tmp_path, monkeypatch):
    """When is_symlink()/isjunction() report False, lstat reparse
    attributes are still consulted — a non-symlink reparse tag is not
    silently accepted."""
    class _St:
        st_file_attributes = 0x400     # FILE_ATTRIBUTE_REPARSE_POINT
        st_mode = 0o40777
    monkeypatch.setattr(Path, "is_symlink", lambda s: False)
    if hasattr(os.path, "isjunction"):
        monkeypatch.setattr(os.path, "isjunction", lambda p: False)
    monkeypatch.setattr(Path, "lstat", lambda s: _St())
    assert ra._path_reparse(tmp_path)


def test_c3_junction_actual(tmp_path):
    """Actual filesystem junction integration — environment
    conditioned. Mocked coverage above is authoritative; this only
    runs where junction creation is available (Windows, no admin
    required for directory junctions)."""
    if os.name != "nt" or not hasattr(os.path, "isjunction"):
        pytest.skip("actual junction coverage requires Windows")
    target = tmp_path / "real"
    target.mkdir()
    junc = tmp_path / "junc"
    # test-side only — the module itself never spawns processes (a86)
    r = subprocess.run(["cmd", "/c", "mklink", "/J",
                        str(junc), str(target)], check=False,
                       capture_output=True)
    if r.returncode != 0 or not os.path.isjunction(junc):
        pytest.skip("junction creation unavailable in this environment")
    res = _run(_single(), junc)
    assert res["refusals"] == ["staging_root_reparse"]


# ================================================================ C4
# exact asset identity uniqueness


def test_c4_01_duplicate_id_different_names_refused(tmp_path):
    scn = _single()
    meta = copy.deepcopy(scn["meta"])
    meta["assets"].append(dict(meta["assets"][0], name="other.zip"))
    res = _run(scn, tmp_path, meta=meta)
    assert res["refusals"] == ["asset_duplicate_id:1"]


def test_c4_02_duplicate_id_same_name_refused(tmp_path):
    scn = _single()
    meta = copy.deepcopy(scn["meta"])
    meta["assets"].append(dict(meta["assets"][0]))
    res = _run(scn, tmp_path, meta=meta)
    assert res["refusals"] == ["asset_duplicate_identity:runtime.zip"]


def test_c4_03_duplicate_name_different_ids_refused(tmp_path):
    scn = _single()
    meta = copy.deepcopy(scn["meta"])
    meta["assets"].append(dict(meta["assets"][0], id=9))
    res = _run(scn, tmp_path, meta=meta)
    assert res["refusals"] == ["asset_duplicate_identity:runtime.zip"]


def test_c4_04_contradictory_url_identity_refused(tmp_path):
    scn = _single()
    meta = copy.deepcopy(scn["meta"])
    meta["assets"].append(dict(meta["assets"][0],
                               name="b.zip", id=9))
    res = _run(scn, tmp_path, meta=meta)
    assert res["refusals"] == ["asset_duplicate_url:b.zip"]


def test_c4_05_unselected_inventory_conflict_refused(tmp_path):
    scn = _multi()
    meta = copy.deepcopy(scn["meta"])
    # opt is unselected; a contradictory duplicate of its asset id in
    # the supplied inventory still refuses — the whole inventory is
    # identity-checked, not only the selected names.
    meta["assets"].append(dict(meta["assets"][2], name="evil.zip"))
    res = _run(scn, tmp_path, selection=_sel(scn, opt=False),
               meta=meta)
    assert res["refusals"] == ["asset_duplicate_id:3"]


def test_c4_06_malformed_id_refused_before_indexing(tmp_path):
    scn = _single()
    meta = copy.deepcopy(scn["meta"])
    meta["assets"].append({"id": "x", "name": "z.zip", "size": 1,
                           "browser_download_url": "https://x/z.zip",
                           "digest": "sha256:" + "0" * 64})
    res = _run(scn, tmp_path, meta=meta)
    assert res["refusals"] == ["asset_id_invalid:1"]


def test_c4_07_distinct_multi_archive_identities_accepted(tmp_path):
    res = _run(_multi(), tmp_path)
    assert res["ok"]
    assert len({a["asset_id"] for a in res["archives"]}) == 3


def test_c4_08_contradiction_invokes_no_transport(tmp_path):
    scn = _single()
    meta = copy.deepcopy(scn["meta"])
    meta["assets"].append(dict(meta["assets"][0], name="other.zip"))
    calls = []
    res = _run(scn, tmp_path, meta=meta,
               transport=lambda u: calls.append(u) or iter([]))
    assert res["refusals"] == ["asset_duplicate_id:1"]
    assert calls == []


# ================================================================ C5
# complete metadata-detectable preflight


def test_c5_03_missing_member_first_archive_no_writes(tmp_path):
    scn = _mk(
        [("art", "primary", True, 1, [("a.dll", b"a")]),
         ("dep", "dependency", True, 2, [("d.dll", b"d")])],
        members=[_member("a.dll", "art", b"a"),
                 _member("missing.dll", "art", b"m"),
                 _member("d.dll", "dep", b"d")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["missing_expected_member:missing.dll"]
    assert res["members"] == []
    assert _no_member_writes(res, tmp_path)


def test_c5_04_missing_member_final_archive_no_writes(tmp_path):
    scn = _mk(
        [("art", "primary", True, 1, [("a.dll", b"a")]),
         ("dep", "dependency", True, 2, [("d.dll", b"d")])],
        members=[_member("a.dll", "art", b"a"),
                 _member("d.dll", "dep", b"d"),
                 _member("missing.dll", "dep", b"m")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["missing_expected_member:missing.dll"]
    assert res["members"] == []
    # The FIRST archive's members were never written either — zero
    # managed-member output across the whole acquisition.
    assert _no_member_writes(res, tmp_path)


def test_c5_05_missing_member_optional_archive_no_writes(tmp_path):
    scn = _mk(
        [("runtime", "primary", True, 1, [("sd.exe", b"sd")]),
         ("opt", "optional", False, 2, [("opt.txt", b"o")])],
        members=[_member("sd.exe", "runtime", b"sd"),
                 _member("opt.txt", "opt", b"o"),
                 _member("gone.dll", "opt", b"g")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["missing_expected_member:gone.dll"]
    assert res["members"] == []
    assert _no_member_writes(res, tmp_path)


def test_c5_07_skip_managed_contradiction_refused(tmp_path):
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd")])],
              members=[_member("sd.exe", "runtime", b"sd")],
              skips=["sd.exe"])
    res = _run(scn, tmp_path)
    assert res["refusals"][0] == "member_skip_conflict:sd.exe"
    assert res["ok"] is False
    assert _no_member_writes(res, tmp_path)


def test_c5_08_wrong_component_ownership_no_writes(tmp_path):
    scn = _mk(
        [("runtime", "primary", True, 1, [("sd.exe", b"sd")]),
         ("dep", "dependency", True, 2,
          [("shared.dll", b"dep"), ("dep.dll", b"d")])],
        members=[_member("sd.exe", "runtime", b"sd"),
                 _member("shared.dll", "runtime", b"rt"),
                 _member("dep.dll", "dep", b"d")])
    res = _run(scn, tmp_path)
    assert res["refusals"][0].startswith(
        ("unexpected_member", "missing_expected_member"))
    assert _no_member_writes(res, tmp_path)


def test_c5_09_10_later_preflight_failure_no_writes(tmp_path):
    # dep prefights after art (deterministic component order); its
    # unexpected entry refuses before art's members are ever written.
    scn = _mk(
        [("art", "primary", True, 1, [("a.dll", b"a")]),
         ("dep", "dependency", True, 2,
          [("d.dll", b"d"), ("evil.bin", b"e")])],
        members=[_member("a.dll", "art", b"a"),
                 _member("d.dll", "dep", b"d")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["unexpected_member:evil.bin"]
    assert res["members"] == []
    assert _no_member_writes(res, tmp_path)


# ================================================================ C6
# bounded resource and ZIP parser hardening


def test_c6_01_valid_bounded_directory_accepted(tmp_path):
    scn = _single()
    bio = io.BytesIO(scn["zips"]["runtime"])
    info, why = ra._preflight_zip_dir(
        bio, len(scn["zips"]["runtime"]), None)
    assert why is None
    assert info["entries"] == 1 and info["cd_size"] > 0
    res = _run(scn, tmp_path)
    assert res["ok"]


def test_c6_02_oversized_declared_directory_refused(
        tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "_MAX_CENTRAL_DIR_BYTES", 1)
    res = _run(_single(), tmp_path)
    assert res["refusals"] == ["archive_directory_over_bound"]


def test_c6_03_excessive_declared_count_refused(tmp_path):
    zb = _patch_eocd(_z([("sd.exe", b"sd")]), entries=3000)
    scn = _mk_from_bytes("runtime", zb,
                         [_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["archive_directory_entries_over_bound"]


def test_c6_04_contradictory_directory_ranges_refused(tmp_path):
    zb = _patch_eocd(_z([("sd.exe", b"sd")]), cd_off=0x7FFFFFFF)
    scn = _mk_from_bytes("runtime", zb,
                         [_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["archive_directory_range_invalid"]
    # Declared count contradicting the actual central records also
    # refuses.
    zb2 = _patch_eocd(_z([("sd.exe", b"sd")]), entries=7)
    scn2 = _mk_from_bytes("runtime", zb2,
                          [_member("sd.exe", "runtime", b"sd")])
    res2 = _run(scn2, tmp_path / "b")
    assert res2["refusals"] == ["archive_directory_count_mismatch"]


def test_c6_06_aggregate_directory_budget_enforced(
        tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "_MAX_AGGREGATE_DIR_BYTES", 100)
    res = _run(_multi(), tmp_path)
    assert res["refusals"] == ["aggregate_directory_over_bound"]


def test_c6_07_unsupported_zip_structures_refused(tmp_path):
    zb = _patch_eocd(_z([("sd.exe", b"sd")]), disk=1)
    scn = _mk_from_bytes("runtime", zb,
                         [_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["archive_directory_multidisk"]
    zb2 = _patch_eocd(_z([("sd.exe", b"sd")]), entries=0xFFFF)
    scn2 = _mk_from_bytes("runtime", zb2,
                          [_member("sd.exe", "runtime", b"sd")])
    res2 = _run(scn2, tmp_path / "b")
    assert res2["refusals"] == ["archive_zip64_unsupported"]


def test_c6_08_09_10_retained_memory_budget(tmp_path, monkeypatch):
    scn = _multi()
    total = sum(len(z) for z in scn["zips"].values())
    # Budget fits all but the last archive (declaration order is the
    # deterministic component order) — the last falls back to the
    # bounded file-backed path rather than silently expanding.
    monkeypatch.setattr(ra, "_MAX_RETAINED_MEM", total - 1)
    res = _run(scn, tmp_path)
    assert res["ok"]
    bycomp = {a["component"]: a for a in res["archives"]}
    assert bycomp["runtime"]["reopen_comparison"] == "match"
    assert bycomp["cudart"]["reopen_comparison"] == "memory_backed"


def test_c6_11_ratio_exactly_at_limit_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "_MAX_RATIO", 1)
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd-bytes",
                  {"compress_type": zipfile.ZIP_STORED})])],
              members=[_member("sd.exe", "runtime", b"sd-bytes")])
    res = _run(scn, tmp_path)
    assert res["ok"]


def test_c6_13_zero_compressed_size_edges(tmp_path):
    # A genuinely empty entry remains valid (both sizes zero) — as a
    # declared skip, since rref:v0 itself rejects zero-size members.
    scn = _mk([("runtime", "primary", True, 1,
                [("sd.exe", b"sd"), ("e.txt", b"")])],
              members=[_member("sd.exe", "runtime", b"sd")],
              skips=["e.txt"])
    res = _run(scn, tmp_path)
    assert res["ok"]
    assert res["skipped"][0]["name"] == "e.txt"
    # Zero compressed size with nonzero expanded is an unbounded
    # ratio and refuses.
    zb = _z([("sd.exe", b"sd")])
    b = bytearray(zb)
    j = _nth_sig(zb, b"PK\x01\x02", 0)
    b[j + 20:j + 24] = (0).to_bytes(4, "little")
    scn2 = _mk_from_bytes("runtime", bytes(b),
                          [_member("sd.exe", "runtime", b"sd")])
    res2 = _run(scn2, tmp_path / "b")
    assert res2["refusals"] == ["member_ratio_abuse:sd.exe",
                                "missing_expected_member:sd.exe"]


def test_c6_14_cancellation_during_preflight(tmp_path):
    calls = [0]

    def cc():
        calls[0] += 1
        # stream chunk (1), directory assessment (2), first preflight
        # entry check (3).
        return calls[0] >= 3
    res = _run(_single(), tmp_path, cancel_check=cc)
    assert res["state"] == "cancelled"
    assert res["members"] == []


def test_c6_16_embedded_nul_in_original_name(tmp_path):
    zb = _patch_entry_name(_z([("sd.exe", b"s"),
                               ("aXb.dll", b"e")]),
                           b"aXb.dll", b"a\x00b.dll")
    scn = _mk_from_bytes("runtime", zb,
                         [_member("sd.exe", "runtime", b"s")])
    res = _run(scn, tmp_path)
    assert res["refusals"][0].startswith("member_name_invalid")


def test_c6_17_local_central_method_contradiction(tmp_path):
    zb = _patch_local_method(_z([("sd.exe", b"sd")]), 0)
    scn = _mk_from_bytes("runtime", zb,
                         [_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == [
        "archive_local_central_mismatch:sd.exe",
        "missing_expected_member:sd.exe"]


def test_c6_18_local_central_flag_contradiction(tmp_path):
    zb = _patch_local_flag(_z([("sd.exe", b"sd")]), 0x1)
    scn = _mk_from_bytes("runtime", zb,
                         [_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == [
        "archive_local_central_mismatch:sd.exe",
        "missing_expected_member:sd.exe"]


def test_c6_19_unsupported_security_flags_refused(tmp_path):
    zb = _patch_flag(_z([("sd.exe", b"sd")]), 0x40)
    scn = _mk_from_bytes("runtime", zb,
                         [_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == [
        "member_security_flag_unsupported:sd.exe",
        "missing_expected_member:sd.exe"]


def test_c6_20_size_semantics(tmp_path):
    # Committed local size contradicting the central entry refuses.
    zb = _patch_local_csize(_z([("sd.exe", b"sd")]), 0)
    scn = _mk_from_bytes("runtime", zb,
                         [_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == [
        "archive_local_central_mismatch:sd.exe",
        "missing_expected_member:sd.exe"]
    # Data-descriptor (bit 3) entries are UNSUPPORTED under the
    # authorized narrowing — the formerly accepted bounded descriptor
    # form now refuses deterministically (N1).
    zd = _z_descriptor([("sd.exe", b"sd-bytes")])
    scn2 = _mk_from_bytes("runtime", zd,
                          [_member("sd.exe", "runtime", b"sd-bytes")])
    res2 = _run(scn2, tmp_path / "b")
    assert res2["refusals"] == [
        "member_flags_unsupported:sd.exe",
        "missing_expected_member:sd.exe"]


def test_c6_21_overlapping_entry_ranges_refused(tmp_path):
    # Grow entry 0's claimed compressed size so its data range runs
    # 4 bytes into entry 1's local header — consistent local/central
    # fields, contradictory layout.
    zb = _patch_csize_both(
        _z([("a.dll", b"aa"), ("b.dll", b"bb")]), extra=4)
    scn = _mk_from_bytes("runtime", zb,
                         [_member("a.dll", "runtime", b"aa"),
                          _member("b.dll", "runtime", b"bb")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["archive_entry_overlap:b.dll"]


def test_c6_22_skipped_entry_cannot_bypass_structure(tmp_path):
    # The declared-skipped entry carries an encryption flag — the
    # structural refusal fires even though its bytes are never read.
    zb = _patch_flag(_z([("sd.exe", b"s"), ("notes.txt", b"n")]),
                     0x1, which=1)
    scn = _mk_from_bytes(
        "runtime", zb,
        [_member("sd.exe", "runtime", b"s")],
        tag="v1")
    scn["ref"]["coverage"]["skip_names"] = ["notes.txt"]
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["member_encrypted:notes.txt"]


def test_c6_23_malformed_filename_encoding_controlled(tmp_path):
    # A name that is undecodable under its declared UTF-8 flag is a
    # controlled refusal, never a crash.
    zb = _patch_flag(_patch_entry_name(
        _z([("sd.exe", b"sd")]), b"sd.exe", b"sd.ex\xff"), 0x800)
    scn = _mk_from_bytes("runtime", zb,
                         [_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["state"] == "refused"
    assert res["refusals"] == ["archive_corrupt:runtime"]
    assert res["production_trust"] is False
    assert res["execution_eligible"] is False


# ================================================================ N1
# data-descriptor (general-purpose bit 3) entries unsupported


def test_n1_01_managed_bit3_refused(tmp_path):
    # An otherwise valid managed member carrying the descriptor bit
    # refuses during metadata preflight — no descriptor form is
    # supported under the authorized narrowing.
    zd = _z_descriptor([("sd.exe", b"sd-bytes")])
    scn = _mk_from_bytes("runtime", zd,
                         [_member("sd.exe", "runtime", b"sd-bytes")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["member_flags_unsupported:sd.exe",
                               "missing_expected_member:sd.exe"]
    assert _no_member_writes(res, tmp_path)


def test_n1_02_declared_skip_bit3_refused(tmp_path):
    # A declared-skipped entry with bit 3 gets the same structural
    # flag refusal — the skip status cannot bypass it.
    zd = _z_descriptor([("notes.txt", b"n"), ("sd.exe", b"sd")])
    scn = _mk_from_bytes("runtime", zd,
                         [_member("sd.exe", "runtime", b"sd")])
    scn["ref"]["coverage"]["skip_names"] = ["notes.txt"]
    res = _run(scn, tmp_path)
    assert res["refusals"] == [
        "member_flags_unsupported:notes.txt",
        "member_flags_unsupported:sd.exe",
        "missing_expected_member:sd.exe"]


def test_n1_03_local_central_bit3_disagreement_refused(tmp_path):
    # Bit 3 set in the local header only — the flag-consistency
    # check refuses even though the central entry hides it.
    zb = _patch_local_flag(_z([("sd.exe", b"sd")]), 0x8)
    scn = _mk_from_bytes("runtime", zb,
                         [_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == [
        "archive_local_central_mismatch:sd.exe",
        "missing_expected_member:sd.exe"]


def test_n1_04_descriptor_tail_overlap_refused(tmp_path):
    # A descriptor-form fixture whose claimed data range is grown so
    # its trailing bytes would collide with the next structure —
    # the bit-3 refusal fires before range assessment is needed.
    zb = _patch_csize_both(
        _z_descriptor([("sd.exe", b"sd")]), extra=4)
    scn = _mk_from_bytes("runtime", zb,
                         [_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == [
        "member_flags_unsupported:sd.exe",
        "missing_expected_member:sd.exe"]


def test_n1_05_descriptor_tail_dir_intrusion_refused(tmp_path):
    # Descriptor-form fixture whose declared central-directory offset
    # is shifted so entry data would intrude into the directory
    # region — refused on the unsupported flag first.
    zb = _patch_eocd(_z_descriptor([("sd.exe", b"sd")]), cd_off=8)
    scn = _mk_from_bytes("runtime", zb,
                         [_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == [
        "member_flags_unsupported:sd.exe",
        "missing_expected_member:sd.exe"]


def test_n1_06_final_archive_bit3_zero_writes(tmp_path):
    # The descriptor-bit refusal lands in the FINAL selected
    # archive's preflight — zero managed-member writes across the
    # entire acquisition, including earlier clean archives.
    scn = _mk(
        [("runtime", "primary", True, 1, [("sd.exe", b"sd")]),
         ("opt", "optional", False, 3, [("opt.txt", b"opt")])],
        members=[_member("sd.exe", "runtime", b"sd"),
                 _member("opt.txt", "opt", b"opt")])
    scn = _swap_zip(scn, "opt",
                    _z_descriptor([("opt.txt", b"opt")]))
    res = _run(scn, tmp_path)
    assert res["refusals"] == [
        "member_flags_unsupported:opt.txt",
        "missing_expected_member:opt.txt"]
    assert res["members"] == []
    assert _no_member_writes(res, tmp_path)


def test_n1_07_ordinary_archives_accepted(tmp_path):
    res = _run(_single(), tmp_path)
    assert res["ok"]
    res2 = _run(_multi(), tmp_path / "m")
    assert res2["ok"]


def test_n1_08_refusal_envelope_declarations(tmp_path):
    zd = _z_descriptor([("sd.exe", b"sd")])
    scn = _mk_from_bytes("runtime", zd,
                         [_member("sd.exe", "runtime", b"sd")])
    res = _run(scn, tmp_path)
    assert res["state"] == "refused"
    assert res["production_trust"] is False
    assert res["execution_eligible"] is False
    assert res["operational_qualification"] == "unestablished"
    assert res["trusted_enrollment"] == "unestablished"
    assert res["installation_publication"] == "not_performed"


# ================================================================ N2
# deterministic archive resource ownership


def test_n2_01_success_file_backed_all_closed(tmp_path, monkeypatch):
    _force_file(monkeypatch)
    scn = _single()
    log, got = _n2_track(monkeypatch)
    res = _run(scn, tmp_path)
    assert res["ok"]
    assert got["fh"] and got["zf"]
    assert log == ["zf", "fh"]
    _n2_assert_closed(got)


def test_n2_02_success_memory_backed_all_closed(
        tmp_path, monkeypatch):
    scn = _single()
    log, got = _n2_track(monkeypatch)
    res = _run(scn, tmp_path)
    assert res["ok"]
    assert got["bio"] and got["zf"]
    assert log == ["zf", "bio"]
    _n2_assert_closed(got)


def test_n2_03_association_refusal_releases_retained(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    scn = _mk(
        [("art", "primary", True, 1, [("a.dll", b"a")]),
         ("dep", "dependency", True, 2, [("d.dll", b"d")])])
    _, got = _n2_track(monkeypatch)
    orig = ra._open_archive_assoc

    # Corrupt the second archive right before its verified reopen —
    # the first archive's retained handle must still be released.
    def wrap2(path, *a, **k):
        if getattr(path, "name", "") == "dep.zip":
            _flip(path, 0)
        return orig(path, *a, **k)

    monkeypatch.setattr(ra, "_open_archive_assoc", wrap2)
    res = _run(scn, tmp_path)
    assert res["refusals"][0] in ("archive_association_changed",
                                  "archive_rehash_mismatch")
    assert got["fh"]
    _n2_assert_closed(got)


def test_n2_04_zipfile_ctor_failure_releases_handle(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    scn = _single()
    _, got = _n2_track(monkeypatch)

    def boom(*a, **k):
        raise zipfile.BadZipFile("synthetic ctor failure")

    monkeypatch.setattr(zipfile, "ZipFile", boom)
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["archive_corrupt:runtime"]
    assert got["fh"]
    _n2_assert_closed(got)


def test_n2_05_preflight_refusal_releases_resources(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    zb = _patch_flag(_z([("sd.exe", b"sd")]), 0x40)
    scn = _mk_from_bytes("runtime", zb,
                         [_member("sd.exe", "runtime", b"sd")])
    _, got = _n2_track(monkeypatch)
    res = _run(scn, tmp_path)
    assert res["refusals"] == [
        "member_security_flag_unsupported:sd.exe",
        "missing_expected_member:sd.exe"]
    assert got["fh"] and got["zf"]
    _n2_assert_closed(got)


def test_n2_06_later_archive_failure_releases_earlier(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    scn = _mk(
        [("art", "primary", True, 1, [("a.dll", b"a")]),
         ("dep", "dependency", True, 2, [("d.dll", b"d")])])
    scn = _swap_zip(scn, "dep",
                    _z_descriptor([("d.dll", b"d")]))
    _, got = _n2_track(monkeypatch)
    res = _run(scn, tmp_path)
    assert res["refusals"] == [
        "member_flags_unsupported:d.dll",
        "missing_expected_member:d.dll"]
    # art's fh + zf were retained before dep failed.
    assert got["fh"] and got["zf"]
    _n2_assert_closed(got)


def test_n2_07_extraction_refusal_releases_resources(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    scn = _single()
    # A deterministic mid-extraction refusal — the lifecycle
    # property under test is identical for every refusal origin.
    monkeypatch.setattr(
        ra, "_extract_member", lambda *a, **k: (None, "member_corrupt"))
    _, got = _n2_track(monkeypatch)
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["member_corrupt:sd.exe"]
    assert got["fh"] and got["zf"]
    _n2_assert_closed(got)


def test_n2_08_member_digest_mismatch_releases_resources(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    scn = _single()
    scn["ref"]["members"][0]["sha256"] = "0" * 64
    _, got = _n2_track(monkeypatch)
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["member_sha256_mismatch:sd.exe"]
    _n2_assert_closed(got)


def test_n2_09_cancel_during_preflight_releases_resources(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    scn = _single()
    _, got = _n2_track(monkeypatch)
    inside = [False]
    orig = ra.preflight_archive

    def w(*a, **k):
        inside[0] = True
        return orig(*a, **k)

    monkeypatch.setattr(ra, "preflight_archive", w)
    res = _run(scn, tmp_path, cancel_check=lambda: inside[0])
    assert res["state"] == "cancelled" and res["ok"] is False
    assert got["fh"] and got["zf"]
    _n2_assert_closed(got)


def test_n2_10_cancel_during_extraction_releases_resources(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    scn = _single()
    _, got = _n2_track(monkeypatch)
    inside = [False]
    orig = ra._extract_member

    def w(*a, **k):
        inside[0] = True
        return orig(*a, **k)

    monkeypatch.setattr(ra, "_extract_member", w)
    res = _run(scn, tmp_path, cancel_check=lambda: inside[0])
    assert res["state"] == "cancelled" and res["ok"] is False
    _n2_assert_closed(got)


def test_n2_11_cancel_during_final_releases_resources(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    scn = _single()
    _, got = _n2_track(monkeypatch)
    inside = [False]
    orig = ra._final_archive_assoc

    def w(*a, **k):
        inside[0] = True
        return orig(*a, **k)

    monkeypatch.setattr(ra, "_final_archive_assoc", w)
    res = _run(scn, tmp_path, cancel_check=lambda: inside[0])
    assert res["state"] == "cancelled" and res["ok"] is False
    _n2_assert_closed(got)


def test_n2_12_controlled_exception_releases_resources(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    scn = _single()
    _, got = _n2_track(monkeypatch)

    def boom(*a, **k):
        raise OSError("synthetic assessment failure")

    monkeypatch.setattr(ra, "_preflight_zip_dir", boom)
    res = _run(scn, tmp_path)
    assert res["state"] == "failed"
    assert got["fh"]
    _n2_assert_closed(got)


def test_n2_13_parser_closed_before_underlying_handle(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    scn = _single()
    log, _ = _n2_track(monkeypatch)
    res = _run(scn, tmp_path)
    assert res["ok"]
    assert log == ["zf", "fh"]


def test_n2_14_partial_evidence_preserved_after_cleanup(
        tmp_path, monkeypatch):
    _force_file(monkeypatch)
    monkeypatch.setattr(
        ra, "_TEST_SEAM",
        lambda point, **kw: _flip(kw["path"], -1)
        if point == "pre_final" else None)
    scn = _single()
    _, got = _n2_track(monkeypatch)
    res = _run(scn, tmp_path)
    assert res["state"] == "refused"
    assert res["refusals"][0] in ("archive_association_changed",
                                  "archive_rehash_mismatch")
    # The completed member write is preserved as evidence — and the
    # resources are still deterministically released.
    assert [m["name"] for m in res["members"]] == ["sd.exe"]
    assert res["staging"]["complete"] is False
    assert got["fh"] and got["zf"]
    _n2_assert_closed(got)


# ============================================================ helpers


def tweak_tr(scn, data):
    urls = dict(scn["urls"])
    urls["https://x/runtime.zip"] = data
    return lambda u: iter([urls[u]])


def _mk_from_bytes(comp, zb, members, role="primary", required=True,
                   asset_id=1, tag="v1", release_id=1):
    """Scenario whose declared archive digest matches arbitrary (e.g.
    corrupted or patched) zip bytes."""
    name, url = f"{comp}.zip", f"https://x/{comp}.zip"
    ref = {"contract": rr.RREF_VERSION, "runtime_key": "sdcpp",
           "flavor": "cuda",
           "release": {"tag": tag, "release_id": release_id,
                       "target_commitish": "9" * 40},
           "archives": [{"component": comp, "role": role,
                         "required": required,
                         "asset": {"id": asset_id, "name": name,
                                   "url": url, "size": len(zb),
                                   "digest": {"algo": "sha256",
                                              "value": _sha(zb)},
                                   "provenance": {
                                       "class": "platform_attested"}}}],
           "members": members,
           "coverage": {"mode": "flat_install_dir",
                        "unexpected_policy": "refuse"}}
    return {"ref": ref,
            "meta": {"release_id": release_id, "tag_name": tag,
                     "assets": [{"id": asset_id, "name": name,
                                 "size": len(zb),
                                 "browser_download_url": url,
                                 "digest": "sha256:" + _sha(zb)}]},
            "zips": {comp: zb}, "urls": {url: zb}}


def _nth_sig(zb: bytes, sig: bytes, n: int) -> int:
    """Byte offset of the nth occurrence of a ZIP record signature."""
    i = 0
    for _ in range(n + 1):
        j = zb.find(sig, i)
        assert j >= 0, (sig, n)
        i = j + 1
    return j


def _patch_flag(zb: bytes, bit: int, which: int = 0) -> bytes:
    """Set a general-purpose flag bit in both local and central
    headers of entry `which` (deterministic synthetic patch)."""
    b = bytearray(zb)
    i = _nth_sig(zb, b"PK\x03\x04", which)
    f = int.from_bytes(b[i + 6:i + 8], "little") | bit
    b[i + 6:i + 8] = f.to_bytes(2, "little")
    j = _nth_sig(zb, b"PK\x01\x02", which)
    f = int.from_bytes(b[j + 8:j + 10], "little") | bit
    b[j + 8:j + 10] = f.to_bytes(2, "little")
    return bytes(b)


def _patch_local_flag(zb: bytes, bit: int, which: int = 0) -> bytes:
    """Set a flag bit in the LOCAL header only — a local-vs-central
    contradiction the central directory does not show."""
    b = bytearray(zb)
    i = _nth_sig(zb, b"PK\x03\x04", which)
    f = int.from_bytes(b[i + 6:i + 8], "little") | bit
    b[i + 6:i + 8] = f.to_bytes(2, "little")
    return bytes(b)


def _patch_local_method(zb: bytes, method: int,
                        which: int = 0) -> bytes:
    """Rewrite the LOCAL compression method field only."""
    b = bytearray(zb)
    i = _nth_sig(zb, b"PK\x03\x04", which)
    b[i + 8:i + 10] = method.to_bytes(2, "little")
    return bytes(b)


def _patch_local_csize(zb: bytes, val: int, which: int = 0) -> bytes:
    """Rewrite the LOCAL compressed-size field only."""
    b = bytearray(zb)
    i = _nth_sig(zb, b"PK\x03\x04", which)
    b[i + 18:i + 22] = val.to_bytes(4, "little")
    return bytes(b)


def _patch_csize_both(zb: bytes, extra: int, which: int = 0) -> bytes:
    """Grow the compressed-size fields of entry `which` in BOTH local
    and central headers — mutually consistent but inflating the
    claimed data range beyond the real bytes."""
    b = bytearray(zb)
    i = _nth_sig(zb, b"PK\x03\x04", which)
    cs = int.from_bytes(b[i + 18:i + 22], "little") + extra
    b[i + 18:i + 22] = cs.to_bytes(4, "little")
    j = _nth_sig(zb, b"PK\x01\x02", which)
    cs2 = int.from_bytes(b[j + 20:j + 24], "little") + extra
    b[j + 20:j + 24] = cs2.to_bytes(4, "little")
    return bytes(b)


def _find_eocd(zb: bytes) -> int:
    i = zb.rfind(b"PK\x05\x06")
    assert i >= 0
    return i


def _patch_eocd(zb: bytes, entries=None, cd_size=None, cd_off=None,
                disk=None) -> bytes:
    """Patch EOCD record fields deterministically."""
    b = bytearray(zb)
    i = _find_eocd(zb)
    if disk is not None:
        b[i + 4:i + 6] = disk.to_bytes(2, "little")
        b[i + 6:i + 8] = disk.to_bytes(2, "little")
    if entries is not None:
        b[i + 8:i + 10] = entries.to_bytes(2, "little")
        b[i + 10:i + 12] = entries.to_bytes(2, "little")
    if cd_size is not None:
        b[i + 12:i + 16] = cd_size.to_bytes(4, "little")
    if cd_off is not None:
        b[i + 16:i + 20] = cd_off.to_bytes(4, "little")
    return bytes(b)


def _patch_entry_name(zb: bytes, old: bytes, new: bytes) -> bytes:
    """Replace a member name's bytes in BOTH local and central
    headers (same-length substitution)."""
    assert len(old) == len(new)
    return bytes(zb).replace(old, new)


def _patch_local_name(zb: bytes) -> bytes:
    """Flip one byte of the local-header filename so it contradicts
    the central directory entry."""
    b = bytearray(zb)
    i = b.find(b"PK\x03\x04")
    assert i >= 0
    b[i + 30] ^= 0x01
    return bytes(b)


def _z_descriptor(entries):
    """ZIP written through an unseekable sink — the stdlib sets the
    data-descriptor flag bit and commits member sizes in trailing
    descriptors instead of the local headers (supported subset)."""
    class _Unseekable(io.BytesIO):
        def seekable(self):
            return False

        def seek(self, *a, **k):
            raise io.UnsupportedOperation("seek")

        def tell(self):
            raise io.UnsupportedOperation("tell")

    buf = _Unseekable()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


def _flip(path, off=None):
    """Deterministic in-place same-size byte modification."""
    data = bytearray(Path(path).read_bytes())
    i = off if off is not None else len(data) // 2
    data[i] ^= 0xFF
    Path(path).write_bytes(bytes(data))


def _swap_zip(scn, comp, zb):
    """Replace one component's zip bytes and rebind the declared
    size/digest in ref + metadata + transport URL — identities
    otherwise unchanged."""
    scn = copy.deepcopy(scn)
    for a in scn["ref"]["archives"]:
        if a["component"] == comp:
            a["asset"]["size"] = len(zb)
            a["asset"]["digest"]["value"] = _sha(zb)
            url = a["asset"]["url"]
    for m in scn["meta"]["assets"]:
        if m["name"] == f"{comp}.zip":
            m["size"] = len(zb)
            m["digest"] = "sha256:" + _sha(zb)
    scn["zips"][comp] = zb
    scn["urls"][url] = zb
    return scn


class _TrackedFh:
    """File-handle proxy that records explicit close() calls. The
    test retains a reference, so an interpreter finalizer can NEVER
    satisfy a `closed` assertion — only explicit close() can.
    Attributes are forwarded to the real handle so os.fstat, os.read
    (via fileno()), seek/read and the ZipFile parser all operate on
    the same underlying descriptor."""

    def __init__(self, fh, log, tag="fh"):
        self._f = fh
        self._log = log
        self._tag = tag

    def close(self):
        if not self._f.closed:
            self._log.append(self._tag)
        self._f.close()

    @property
    def closed(self):
        return self._f.closed

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def __getattr__(self, n):
        return getattr(self._f, n)


class _TrackedIO(io.BytesIO):
    """BytesIO that records its explicit close() in the order log."""

    def __init__(self, *a, _log=None, **k):
        self._n2log = _log
        super().__init__(*a, **k)

    def close(self):
        if not self.closed and self._n2log is not None:
            self._n2log.append("bio")
        super().close()


class _TrackedZip(zipfile.ZipFile):
    """ZipFile that records its explicit close() in the order log.
    The parser object is closed BEFORE its underlying archive handle
    on every exit path (ExitStack unwinds last-in-first-out)."""

    def __init__(self, *a, _log=None, **k):
        self._n2log = _log
        super().__init__(*a, **k)

    def close(self):
        if self.fp is not None and self._n2log is not None:
            self._n2log.append("zf")
        super().close()


def _n2_track(monkeypatch):
    """Instrument every retained archive resource so tests can
    establish EXPLICIT deterministic cleanup — not eventual GC:

    - `_open_archive_assoc` results are wrapped in `_TrackedFh`
      proxies that record close order.
    - `io.BytesIO` construction is routed through `_TrackedIO`
      (records "bio" closes and instances).
    - `zipfile.ZipFile` construction is routed through `_TrackedZip`
      (records "zf" closes; `zf.fp is None` once closed).

    Returns (order_log, got) where got has keys fh/bio/zf → lists of
    retained objects. Install AFTER fixture construction so helper
    archives do not pollute the log."""
    log = []
    got = {"fh": [], "bio": [], "zf": []}
    orig_open = ra._open_archive_assoc
    orig_bio = io.BytesIO
    orig_zf = zipfile.ZipFile

    def wrap(path, *a, **k):
        fh, why = orig_open(path, *a, **k)
        if fh is not None:
            fh = _TrackedFh(fh, log, "fh")
            got["fh"].append(fh)
        return fh, why

    def _ours():
        # Only objects constructed BY the acquisition module are its
        # owned resources — stdlib internals (e.g. the parser's own
        # central-directory BytesIO) are parser temporaries, not
        # retained archive resources.
        f = sys._getframe(2)
        return f.f_code.co_filename == ra.__file__

    def bio_factory(*a, **k):
        if not _ours():
            return orig_bio(*a, **k)
        b = _TrackedIO(*a, _log=log, **k)
        got["bio"].append(b)
        return b

    def zf_factory(*a, **k):
        if not _ours():
            return orig_zf(*a, **k)
        z = _TrackedZip(*a, _log=log, **k)
        got["zf"].append(z)
        return z

    monkeypatch.setattr(ra, "_open_archive_assoc", wrap)
    monkeypatch.setattr(io, "BytesIO", bio_factory)
    monkeypatch.setattr(zipfile, "ZipFile", zf_factory)
    return log, got


def _n2_assert_closed(got):
    """Every retained resource explicitly closed — retained test
    references rule out interpreter teardown as the mechanism."""
    assert got["fh"] or got["bio"] or got["zf"], "nothing retained"
    assert all(f.closed for f in got["fh"])
    assert all(b.closed for b in got["bio"])
    assert all(z.fp is None for z in got["zf"])
