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


def _auth(scn, selected, ops=("acquire", "extract")):
    return ra.SyntheticAcquisitionAuthority(
        [_permit(scn, selected, ops)])


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
    p.write_bytes(b"0123456789")
    fh, why = ra._open_archive_assoc(p, observed_size=9)
    assert fh is None and why == "archive_association_size_mismatch"
    fh, why = ra._open_archive_assoc(tmp_path / "missing.zip",
                                     observed_size=9)
    assert fh is None and why == "archive_stat_failed"
    # directory substitution: open succeeds on POSIX but is not a zip —
    # on Windows open() fails outright; either way it must refuse.
    fh, why = ra._open_archive_assoc(tmp_path, observed_size=0)
    assert fh is None


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
    assert res["refusals"] == [
        "member_compression_unsupported:sd.exe"]


def test_a65_corrupt_archive_refused(tmp_path):
    good = _z([("sd.exe", b"sd-bytes")])
    bad = good[:len(good) // 2]
    scn = _mk_from_bytes("runtime", bad,
                         [_member("sd.exe", "runtime", b"sd-bytes")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == ["archive_corrupt:runtime"]


def test_a66_local_central_contradiction_refused(tmp_path):
    zb = _patch_local_name(_z([("sd.exe", b"sd-bytes")]))
    scn = _mk_from_bytes("runtime", zb,
                         [_member("sd.exe", "runtime", b"sd-bytes")])
    res = _run(scn, tmp_path)
    assert res["refusals"] == [
        "archive_local_central_mismatch:sd.exe"]


def test_a67_entry_count_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "_MAX_ARCHIVE_ENTRIES", 1)
    scn = _mk([("runtime", "primary", True, 1,
                [("a.dll", b"a"), ("b.dll", b"b")])])
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
    assert res["refusals"] == ["member_size_mismatch:sd.exe"]


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


def _patch_flag(zb: bytes, bit: int) -> bytes:
    """Set a general-purpose flag bit in both local and central
    headers (deterministic single-entry synthetic patch)."""
    b = bytearray(zb)
    i = b.find(b"PK\x03\x04")
    if i >= 0:
        f = int.from_bytes(b[i + 6:i + 8], "little") | bit
        b[i + 6:i + 8] = f.to_bytes(2, "little")
    j = b.find(b"PK\x01\x02")
    if j >= 0:
        f = int.from_bytes(b[j + 8:j + 10], "little") | bit
        b[j + 8:j + 10] = f.to_bytes(2, "little")
    return bytes(b)


def _patch_local_name(zb: bytes) -> bytes:
    """Flip one byte of the local-header filename so it contradicts
    the central directory entry."""
    b = bytearray(zb)
    i = b.find(b"PK\x03\x04")
    assert i >= 0
    b[i + 30] ^= 0x01
    return bytes(b)
