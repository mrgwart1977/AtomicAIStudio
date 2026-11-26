"""U-5d-0b Phase beta — runtime integrity evidence substrate (rtq:v1).

Qualification-foundation module for application-managed runtime file
integrity. This module NEVER reads actual installed runtime or model
files — all measurement APIs operate on caller-supplied roots, and the
authorized Phase beta usage is synthetic temporary fixtures only.

Evidence classes (F1) — kept strictly separate; never conflated:

- A. Intended release identity — the catalog release tag / pinned
  release endpoint. An intended DISTRIBUTION identifier only; it is
  not cryptographic publisher authentication.
- B. Trusted expected archive integrity — an independently established
  publisher or owner-attested expected hash. UNAVAILABLE today: no
  publisher hash channel exists and no owner attestation mechanism is
  implemented. Its absence is reported honestly.
- C. Observed archive integrity — ``meta_json.resolved.zip_sha256``:
  a hash computed from the bytes actually downloaded. Class
  ``observed_first_use`` — evidence about what arrived, never a
  trusted publisher reference.
- D. Expected extracted-member integrity — the reference manifest
  (this module). Its authority depends on provenance; an install-time
  capture is trust-on-first-use anchored to the observed archive, not
  an independent trusted reference.
- E. Observed current file integrity — measured member state at
  comparison time.
- F. Exact-build behavioral evidence — static source-revision support
  (sd.cpp 991f825, ggml d803c238). Source support is NOT observed
  runtime behavior.
- G. Operational qualification evidence — Phase beta-3 scope; held.
- H. Launch-time validation evidence — a comparison record produced
  at qualification time; no production launch integration exists.

Trust rules:

- Current file bytes can NEVER establish their own trusted expected
  identity. A reference manifest whose members were measured from the
  current tree may only be built under an explicit allowed provenance
  (``install_capture`` at authorized install time — not implemented
  for production; ``owner_attested`` / ``publisher`` — future
  boundaries). The provenance label alone never upgrades the trust
  class: ``install_capture`` always classifies
  ``observed_first_use``; only a future independently delivered
  reference could classify ``trusted_external``.
- A manifest generated from current files is not automatically an
  authorized reference; construction API requires the caller to pass
  member digests — the module decides validity and classification.

Integrity coverage and limitations (documented, not hidden):

- Coverage is the FLAT top level of the runtime install dir (the
  installer flattens archive members). Member names are plain file
  names only — recursion is never performed.
- Verified sibling files (ggml*.dll, cudart*.dll) prove only that the
  recorded members are unmodified on disk. They do NOT prove the
  actual loaded dependency set: DLL search order, OS loader behavior,
  transitive dependencies, and system/driver DLLs are outside
  coverage (OS + driver trust assumption).
- Windows junction/reparse-point handling is limited to refusing
  links/junctions and post-resolve containment checks.
- Comparison narrows but does NOT eliminate the verification-to-
  launch race: bytes can change after validation. No launch-time
  integration exists in this phase.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

RTQ_VERSION = "rtq:v1"
MANIFEST_VERSION = 1
CONTRACT = "rtq:v1"

# Exact pinned upstream revisions (catalog pin master-1073-991f825).
# Source-level support evidence only — class F, never observed behavior.
PINNED_SOURCE = {
    "stable_diffusion_cpp": "991f82558366b35e16ffabf376fa2cba298c12d3",
    "ggml": "d803c238f67915fb825a9aa4df27b99356d51597",
}

# Provenance classes for reference manifests. install_capture is the
# ONLY provenance the bounded qualification stage may produce — and
# only in synthetic fixtures; production capture is not implemented.
P_INSTALL_CAPTURE = "install_capture"
P_OWNER_ATTESTED = "owner_attested"   # future trusted boundary
P_PUBLISHER = "publisher"             # future trusted boundary
_ALLOWED_PROVENANCE = {P_INSTALL_CAPTURE, P_OWNER_ATTESTED, P_PUBLISHER}

TRUST_OBSERVED_FIRST_USE = "observed_first_use"
TRUST_EXTERNAL = "trusted_external"
_PROVENANCE_TRUST = {
    P_INSTALL_CAPTURE: TRUST_OBSERVED_FIRST_USE,
    P_OWNER_ATTESTED: TRUST_EXTERNAL,
    P_PUBLISHER: TRUST_EXTERNAL,
}

QUALIFIED = "qualified"
UNQUALIFIED = "unqualified"

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
# Windows device namespaces that make a member name ambiguous or
# dangerous; matched against the base name (extension ignored).
_WIN_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_CHUNK = 1024 * 1024
_MAX_DIR_ENTRIES = 1024


def _canon_sha(v) -> str | None:
    """Strict SHA-256 representation: exactly 64 hex chars; normalized
    to lowercase. Anything else is invalid, not coerced."""
    if not isinstance(v, str) or not _SHA256_RE.match(v):
        return None
    return v.lower()


def _valid_size(v) -> bool:
    # bool is an int subclass — an explicit True/False declared size is
    # malformed, never silently coerced to 1/0.
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def valid_member_name(name) -> bool:
    """Plain relative file name only. Rejects empty, '.', '..', any
    path separator, drive-qualified or UNC forms, ADS colons, trailing
    dot/space (Windows-normalized), device names and control chars."""
    if not isinstance(name, str) or not name or name != name.strip():
        return False
    if name in (".", "..") or "/" in name or "\\" in name:
        return False
    if _DRIVE_RE.match(name) or ":" in name:
        return False
    if name.endswith((".", " ")):
        return False
    if any(ord(c) < 0x20 for c in name):
        return False
    base = name.split(".", 1)[0].lower()
    return base not in _WIN_RESERVED


def validate_reference_manifest(manifest) -> dict:
    """Strict structural validation of a candidate reference manifest.
    Returns {"ok": bool, "refusals": [codes], "manifest": normalized
    copy} — never mutates the input, never fabricates missing fields."""
    refusals = []
    if not isinstance(manifest, dict):
        return {"ok": False, "refusals": ["manifest_not_mapping"],
                "manifest": None}
    out = dict(manifest)
    if out.get("v") != MANIFEST_VERSION:
        refusals.append("manifest_version_unsupported")
    if not isinstance(out.get("runtime_key"), str) \
            or not out.get("runtime_key"):
        refusals.append("runtime_key_invalid")
    if not isinstance(out.get("flavor"), str) or not out.get("flavor"):
        refusals.append("flavor_invalid")
    prov = out.get("provenance")
    if prov not in _ALLOWED_PROVENANCE:
        refusals.append("provenance_untrusted_or_unknown")
    members = out.get("members")
    if not isinstance(members, list) or not members:
        refusals.append("members_missing")
        members = []
    seen = {}
    norm_members = []
    for i, m in enumerate(members):
        if not isinstance(m, dict):
            refusals.append(f"member_{i}_not_mapping")
            continue
        name = m.get("name")
        if not valid_member_name(name):
            refusals.append(f"member_name_invalid:{name!r}"
                            if isinstance(name, str) else "member_name_invalid")
            continue
        folded = name.casefold()
        if folded in seen:
            refusals.append(f"duplicate_member:{name}")
            continue
        seen[folded] = name
        sha = _canon_sha(m.get("sha256"))
        if sha is None:
            refusals.append(f"member_sha256_invalid:{name}")
            continue
        if not _valid_size(m.get("size")):
            refusals.append(f"member_size_invalid:{name}")
            continue
        norm_members.append(
            {"name": name, "sha256": sha, "size": m["size"]})
    if norm_members is not None:
        out["members"] = norm_members
    return {"ok": not refusals, "refusals": refusals, "manifest": out}


def build_reference_manifest(runtime_key: str, flavor: str,
                             members: list[dict], provenance: str,
                             recorded_at: str | None = None) -> dict:
    """Construct a reference-manifest candidate from caller-supplied
    member digests. This is manifest CONSTRUCTION only — the result is
    not automatically an authorized reference; validation and the
    provenance/trust classification decide what it may be used for.
    Returns {"ok", "refusals", "manifest"} like validation."""
    manifest = {"v": MANIFEST_VERSION, "runtime_key": runtime_key,
                "flavor": flavor, "provenance": provenance,
                "recorded_at": recorded_at,
                "coverage": "flat_install_dir",
                "members": list(members or [])}
    res = validate_reference_manifest(manifest)
    return {"ok": res["ok"], "refusals": res["refusals"],
            "manifest": res["manifest"] if res["ok"] else None}


def manifest_digest(manifest) -> str | None:
    """Canonical digest of the reference member set — identifies WHICH
    reference was used, not whether the files match it."""
    res = validate_reference_manifest(manifest)
    if not res["ok"]:
        return None
    m = res["manifest"]
    body = {"flavor": m["flavor"], "members": m["members"],
            "runtime_key": m["runtime_key"], "v": m["v"]}
    import json
    return "sha256:" + hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"))
        .encode()).hexdigest()


def trust_classification(manifest) -> str | None:
    """The trust class is DERIVED from provenance — a caller-supplied
    favorable label cannot upgrade it. install_capture is always
    observed-first-use evidence; only independently delivered future
    provenance classes can carry trusted_external."""
    prov = (manifest or {}).get("provenance")
    return _PROVENANCE_TRUST.get(prov)


def _stream_sha256(path: Path, cancel=None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(_CHUNK)
            if not b:
                break
            if cancel:
                cancel()
            h.update(b)
    return h.hexdigest()


def measure_members(root: str | Path, manifest: dict) -> dict:
    """Measure the flat install dir against a VALIDATED reference
    manifest. Bounded: one directory listing (capped), one sequential
    pass over declared members, chunked hashing. Diagnostics carry
    member names only — absolute paths never enter the record."""
    root = Path(root)
    valid = validate_reference_manifest(manifest)
    members_out = []
    unexpected = []
    errors = []
    if not valid["ok"]:
        return {"ok": False, "refusals": valid["refusals"],
                "members": [], "unexpected": [], "errors": errors}
    m = valid["manifest"]
    try:
        root_r = Path(os.path.realpath(root))
    except OSError:
        return {"ok": False, "refusals": ["root_unresolvable"],
                "members": [], "unexpected": [], "errors": errors}
    try:
        entries = os.listdir(root)
    except OSError:
        return {"ok": False, "refusals": ["root_unreadable"],
                "members": [], "unexpected": [], "errors": errors}
    if len(entries) > _MAX_DIR_ENTRIES:
        return {"ok": False, "refusals": ["coverage_dir_unbounded"],
                "members": [], "unexpected": [], "errors": errors}

    declared = {mem["name"].casefold(): mem for mem in m["members"]}
    # Closed coverage: every on-disk top-level entry must be declared.
    for entry in sorted(entries):
        if entry.casefold() not in declared:
            unexpected.append(entry)

    for mem in m["members"]:
        name = mem["name"]
        rec = {"name": name, "expected_sha256": mem["sha256"],
               "expected_size": mem["size"],
               "observed_sha256": None, "observed_size": None,
               "state": None}
        cand = root / name
        try:
            resolved = Path(os.path.realpath(cand))
            if resolved.parent != root_r or resolved == root_r:
                rec["state"] = "containment_violation"
            elif cand.is_symlink() or cand.is_junction():
                rec["state"] = "reparse_point"
            elif not cand.exists():
                rec["state"] = "missing"
            elif not cand.is_file():
                rec["state"] = "non_regular"
            else:
                st0 = cand.stat()
                rec["observed_size"] = st0.st_size
                if st0.st_size != mem["size"]:
                    rec["state"] = "size_mismatch"
                else:
                    rec["observed_sha256"] = _stream_sha256(cand)
                    st1 = cand.stat()
                    if st1.st_size != st0.st_size \
                            or st1.st_mtime_ns != st0.st_mtime_ns:
                        rec["state"] = "changed_during_verification"
                    elif rec["observed_sha256"] != mem["sha256"]:
                        rec["state"] = "sha256_mismatch"
                    else:
                        rec["state"] = "match"
        except OSError as exc:
            # Sanitized: the failure class is evidence; the path is not.
            rec["state"] = "io_error"
            errors.append({"member": name,
                           "error_class": type(exc).__name__})
        members_out.append(rec)
    return {"ok": True, "refusals": [], "members": members_out,
            "unexpected": unexpected, "errors": errors}


def qualify(root: str | Path, manifest: dict,
            *, expected_flavor: str | None = None) -> dict:
    """Produce the rtq:v1 qualification record for a runtime tree.
    classification is 'qualified' ONLY when the manifest validates,
    every declared member matches, and coverage is closed (no
    unexpected entries). Any defect yields unqualified + reason."""
    measured = measure_members(root, manifest)
    checks = {"manifest_valid": measured["ok"],
              "all_members_match": False, "coverage_closed": False,
              "flavor_consistent": True}
    if not measured["ok"]:
        return {"contract": RTQ_VERSION, "classification": UNQUALIFIED,
                "reason": "reference_manifest_invalid",
                "refusals": measured["refusals"], "members": [],
                "unexpected": measured["unexpected"],
                "manifest_digest": manifest_digest(manifest),
                "trust": trust_classification(manifest),
                "checks": checks}
    if expected_flavor is not None \
            and manifest.get("flavor") != expected_flavor:
        checks["flavor_consistent"] = False
    states = {mem["state"] for mem in measured["members"]}
    checks["all_members_match"] = states == {"match"}
    checks["coverage_closed"] = not measured["unexpected"]
    ok = (checks["all_members_match"] and checks["coverage_closed"]
          and checks["flavor_consistent"])
    if not checks["flavor_consistent"]:
        reason = "flavor_mismatch"
    elif not checks["all_members_match"]:
        reason = "member_integrity_failure:" + ",".join(sorted(
            s for s in states if s != "match"))
    elif not checks["coverage_closed"]:
        reason = "unexpected_members_in_scope"
    else:
        reason = "all_members_verified"
    return {"contract": RTQ_VERSION, "classification": (
                QUALIFIED if ok else UNQUALIFIED),
            "reason": reason, "refusals": [],
            "members": measured["members"],
            "unexpected": measured["unexpected"],
            "errors": measured["errors"],
            "manifest_digest": manifest_digest(manifest),
            "trust": trust_classification(manifest),
            "checks": checks}


def evidence_classes(resolved_meta: dict | None,
                     manifest: dict | None = None) -> dict:
    """Classify the integrity evidence available from an install
    record's resolved metadata + optional reference manifest.
    Observed values are never promoted to trusted classes."""
    meta = dict(resolved_meta or {})
    zip_sha = meta.get("zip_sha256")
    return {
        "A_intended_release_identity": {
            "tag": meta.get("tag"), "pinned": meta.get("pinned"),
            "class": "intended_distribution_identifier"},
        "B_trusted_archive_integrity": {
            "state": "unavailable",
            "reason": "no_independent_publisher_reference"},
        "C_observed_archive_integrity": {
            "sha256": zip_sha if _canon_sha(zip_sha) else None,
            "class": (TRUST_OBSERVED_FIRST_USE if zip_sha
                      else "absent")},
        "D_expected_extracted_integrity": {
            "state": ("available" if manifest else "unavailable"),
            "manifest_digest": manifest_digest(manifest),
            "trust": trust_classification(manifest)},
        "E_observed_installed_integrity": {
            "state": "measured_at_qualification"},
        "F_exact_build_source": {
            "revisions": dict(PINNED_SOURCE),
            "class": "source_supported_not_observed"},
        "G_operational_qualification": {
            "state": "unavailable_held_beta3"},
        "H_launch_time_validation": {
            "state": "unavailable_no_launch_integration"},
    }


__all__ = [
    "CONTRACT", "MANIFEST_VERSION", "PINNED_SOURCE",
    "P_INSTALL_CAPTURE", "P_OWNER_ATTESTED", "P_PUBLISHER",
    "QUALIFIED", "RTQ_VERSION", "TRUST_EXTERNAL",
    "TRUST_OBSERVED_FIRST_USE", "UNQUALIFIED",
    "build_reference_manifest", "evidence_classes", "manifest_digest",
    "measure_members", "qualify", "trust_classification",
    "valid_member_name", "validate_reference_manifest",
]
