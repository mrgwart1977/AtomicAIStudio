"""U-5d-0b Phase beta-3-B2 — trusted runtime reference foundation (rref:v0).

Isolated, pure validation and resolution machinery for versioned runtime
references. This module NEVER reads installed runtime files, never
performs IO or network access, and is authorized for synthetic fixtures
only. It establishes MECHANISM: content validation, canonical digests,
provenance classification, approval binding, lifecycle transitions and
active-reference resolution. It establishes NO actual trusted runtime
reference — no production authority root exists in this scope.

Layering model (B2-1) — kept strictly separate, never conflated:

- IMMUTABLE REFERENCE CONTENT — the digest-covered body: contract,
  runtime_key, flavor, release identity, archive components, expected
  managed members and coverage policy. Any material change produces a
  different content digest.
- REFERENCE CONTENT DIGEST — canonical SHA-256 over that body only.
- EVIDENCE PROVENANCE — the declared class of each expected digest
  (part of immutable content: a different declared class is different
  content).
- APPROVAL RECORD — a separate object bound to an exact content digest.
- APPROVAL AUTHORITY — the source the approval is claimed against;
  synthetic-fixture only in this scope.
- LIFECYCLE RECORD — proposed/approved/active/superseded/revoked state
  and supersession links; never part of the content digest.
- ACTIVE RESOLUTION — read-path over the registry; a resolution result
  is evidence about registry state, never execution authority.

Trust rules (B2-2):

- Provenance categories and approval are INDEPENDENT dimensions. An
  owner-approved platform-attested digest remains platform-attested
  evidence; approval never elevates provenance to
  publisher_authenticated.
- A caller-supplied approval record, approved_by/approved_at/
  approval_ref field, lifecycle state or favorable boolean/label can
  NEVER establish authority. Approval is validated structurally and
  bound to the recomputed content digest; authority must name the
  synthetic-fixture source explicitly.
- Structural validity is not approval; approval is not activation;
  activation is not member integrity, runtime behavior or execution
  eligibility.
- 'target_commitish' is release metadata, not git-tag resolution
  evidence; a workflow declaration is build provenance CLAIM, not
  evidence that a given archive was produced by that workflow; a
  populated platform digest is not publisher authentication.

Coverage model: flat install dir (the installer flattens archive
members). Member names reuse the rtq:v1 valid_member_name contract —
plain file names only. All diagnostics are sanitized: bounded field
values and member names only; input objects are never mutated.
"""

from __future__ import annotations

import hashlib
import json
import re

from app.domain.runtime_integrity import valid_member_name

RREF_VERSION = "rref:v0"
CONTRACT = RREF_VERSION
DIGEST_PREFIX = "rref:v0:"

# Evidence provenance classes for expected digests (B1-2 authority
# model). These describe WHAT KIND OF EVIDENCE supplies the expected
# value — never WHO approved the reference. 'owner_approved' and any
# authority-class label are deliberately NOT valid provenance values:
# approval is a separate dimension.
P_PUBLISHER = "publisher_authenticated"
P_PLATFORM = "platform_attested"
P_INDEPENDENT = "independently_obtained"
P_OBSERVED = "observed_first_use"
P_UNVERIFIED = "unverified_claim"
_PROVENANCE_CLASSES = {
    P_PUBLISHER, P_PLATFORM, P_INDEPENDENT, P_OBSERVED, P_UNVERIFIED,
}

# Approval authority sources. In this isolated scope the ONLY accepted
# source is the explicit synthetic fixture token — everything else is
# an unestablished production claim.
AUTHORITY_SYNTHETIC = "synthetic_fixture"
_ALLOWED_AUTHORITY = {AUTHORITY_SYNTHETIC}

# Archive component roles.
ROLE_PRIMARY = "primary"
ROLE_DEPENDENCY = "dependency"
ROLE_OPTIONAL = "optional"
_COMPONENT_ROLES = {ROLE_PRIMARY, ROLE_DEPENDENCY, ROLE_OPTIONAL}

COVERAGE_FLAT = "flat_install_dir"
_COVERAGE_MODES = {COVERAGE_FLAT}
_UNEXPECTED_POLICIES = {"refuse"}

# Lifecycle states and permitted transitions (B2-5). Revoked is
# terminal; superseded may only be re-approved explicitly (rollback is
# never silent reactivation).
L_PROPOSED = "proposed"
L_APPROVED = "approved"
L_ACTIVE = "active"
L_SUPERSEDED = "superseded"
L_REVOKED = "revoked"
_LIFECYCLE_STATES = {
    L_PROPOSED, L_APPROVED, L_ACTIVE, L_SUPERSEDED, L_REVOKED,
}
_TRANSITIONS = {
    L_PROPOSED: {L_APPROVED, L_REVOKED},
    L_APPROVED: {L_ACTIVE, L_REVOKED},
    L_ACTIVE: {L_SUPERSEDED, L_REVOKED},
    L_SUPERSEDED: {L_APPROVED, L_REVOKED},
    L_REVOKED: set(),
}

# Resolution result states (B2-6).
R_ABSENT = "absent"
R_INVALID = "invalid"
R_UNAPPROVED = "unapproved"
R_AUTHORITY_UNESTABLISHED = "authority_unestablished"
R_APPROVED_INACTIVE = "approved_inactive"
R_SUPERSEDED = "superseded"
R_REVOKED = "revoked"
R_AMBIGUOUS = "ambiguous"
R_SYNTHETIC_ACTIVE = "synthetic_active"

# Resource bounds (B2-7): every collection and string is capped.
_MAX_ARCHIVES = 16
_MAX_MEMBERS = 2048
_MAX_NAME = 255
_MAX_STRING = 512
_MAX_URL = 1024
_MAX_COMPONENT = 128
_MAX_SIZE = 1 << 60

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_GH_DIGEST_RE = re.compile(r"^sha256:([0-9a-fA-F]{64})$")
_ALPHANUM_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_URL_RE = re.compile(r"^https://[^/@#\s]+/[^\s#]+$")

# Field whitelists — the explicit unknown-field policy (B2-7, R53):
# unknown keys anywhere in security-relevant objects are refused, not
# normalized away.
_REF_FIELDS = {"contract", "runtime_key", "flavor", "release",
               "archives", "members", "coverage"}
# Normalized keys (declared_commitish, *_claim) are accepted as input
# too so that re-validating normalized output is idempotent.
_RELEASE_FIELDS = {"tag", "release_id", "target_commitish",
                   "declared_commitish", "resolved_ref_sha",
                   "published_at"}
_ARCHIVE_FIELDS = {"component", "role", "required", "asset", "source",
                   "license"}
_ASSET_FIELDS = {"id", "name", "url", "size", "digest", "provenance"}
_MEMBER_FIELDS = {"name", "component", "size", "sha256"}
_COVERAGE_FIELDS = {"mode", "unexpected_policy", "skip_names"}
_SOURCE_FIELDS = {"repo", "workflow", "workflow_ref", "build_evidence"}
_LICENSE_FIELDS = {"name", "url", "redistribution"}
_PROV_FIELDS = {"class"}
_APPROVAL_FIELDS = {"reference_digest", "approved_by", "approved_at",
                    "approval_ref", "authority"}
_METADATA_FIELDS = {"release_id", "tag", "target_commitish",
                    "declared_commitish", "resolved_ref_sha",
                    "published_at", "author", "author_claim", "assets"}
_META_ASSET_FIELDS = {"id", "name", "url", "size", "digest",
                      "uploader", "uploader_claim",
                      "created_at", "updated_at"}

_NON_EXECUTABLE = {"execution_eligible": False,
                   "status": "reference_foundation_only",
                   "production_trust": False}


def _env(result: dict) -> dict:
    """Every public output carries the non-executable envelope —
    no validation, resolution or lifecycle result can ever be used
    as execution or production-trust authority (B2-8, R60)."""
    return {**_NON_EXECUTABLE, **result}


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _is_str(v, bound=_MAX_STRING) -> bool:
    return isinstance(v, str) and 0 < len(v) <= bound


def _is_int(v) -> bool:
    # bool is an int subclass — never silently coerced to 1/0.
    return isinstance(v, int) and not isinstance(v, bool)


def _is_size(v) -> bool:
    return _is_int(v) and 0 < v <= _MAX_SIZE


def _canon_sha(v) -> str | None:
    if not isinstance(v, str) or not _SHA256_RE.match(v):
        return None
    return v.lower()


def _canon_sha1(v) -> str | None:
    if not isinstance(v, str) or not _SHA1_RE.match(v):
        return None
    return v.lower()


def _canon_digest(v) -> str | None:
    """GitHub platform asset digest form 'sha256:<64hex>'."""
    if not isinstance(v, str):
        return None
    m = _GH_DIGEST_RE.match(v.strip())
    return m.group(1).lower() if m else None


def _valid_component(v) -> bool:
    return isinstance(v, str) and 0 < len(v) <= _MAX_COMPONENT \
        and _ALPHANUM_RE.match(v) is not None


def _valid_url(v) -> bool:
    return isinstance(v, str) and len(v) <= _MAX_URL \
        and _URL_RE.match(v) is not None


def _unknown_fields(obj: dict, allowed: set, where: str) -> list:
    return [f"unknown_field:{where}:{k}"
            for k in obj if k not in allowed]


def _norm_release(rel, refusals: list) -> dict | None:
    if not isinstance(rel, dict):
        refusals.append("release_not_mapping")
        return None
    refusals.extend(_unknown_fields(rel, _RELEASE_FIELDS, "release"))
    out = {}
    if not _is_str(rel.get("tag"), 128):
        refusals.append("release_tag_invalid")
    else:
        out["tag"] = rel["tag"].strip()
    rid = rel.get("release_id")
    if rid is not None:
        if not _is_int(rid) or rid <= 0:
            refusals.append("release_id_invalid")
        else:
            out["release_id"] = rid
    ci = rel.get("target_commitish")
    if ci is None:
        ci = rel.get("declared_commitish")
    if ci is not None:
        # Declared release metadata only — NOT git-tag resolution
        # evidence. Normalized as declared_commitish so the distinction
        # can never be conflated downstream (R30).
        if not _is_str(ci, 128):
            refusals.append("declared_commitish_invalid")
        else:
            out["declared_commitish"] = ci.strip()
    rs = rel.get("resolved_ref_sha")
    if rs is not None:
        # Actual git-ref resolution evidence — may only arrive through
        # an explicit resolution fixture; absent stays absent.
        sha = _canon_sha1(rs)
        if sha is None:
            refusals.append("resolved_ref_sha_invalid")
        else:
            out["resolved_ref_sha"] = sha
    if rel.get("published_at") is not None:
        if not _is_str(rel["published_at"], 64):
            refusals.append("published_at_invalid")
        else:
            out["published_at"] = rel["published_at"].strip()
    return out


def _norm_asset(asset, comp: str, refusals: list) -> dict | None:
    where = f"archives[{comp}].asset"
    if not isinstance(asset, dict):
        refusals.append(f"asset_not_mapping:{comp}")
        return None
    refusals.extend(_unknown_fields(asset, _ASSET_FIELDS, where))
    out = {}
    aid = asset.get("id")
    if aid is not None:
        if not _is_int(aid) or aid <= 0:
            refusals.append(f"asset_id_invalid:{comp}")
        else:
            out["id"] = aid
    name = asset.get("name")
    if not _is_str(name, _MAX_NAME) or not valid_member_name(name):
        refusals.append(f"asset_name_invalid:{comp}")
    else:
        out["name"] = name
    url = asset.get("url")
    if url is not None:
        if not _valid_url(url):
            refusals.append(f"asset_url_invalid:{comp}")
        else:
            out["url"] = url
    size = asset.get("size")
    if isinstance(size, bool):
        refusals.append(f"asset_size_boolean:{comp}")
    elif not _is_size(size):
        refusals.append(f"asset_size_invalid:{comp}")
    else:
        out["size"] = size
    dig = asset.get("digest")
    prov = asset.get("provenance") or {}
    if isinstance(prov, dict):
        refusals.extend(_unknown_fields(
            prov, _PROV_FIELDS, f"{where}.provenance"))
    else:
        prov = {}
    pcls = prov.get("class")
    if dig is None:
        # Missing expected digest remains missing — never fabricated.
        refusals.append(f"asset_digest_missing:{comp}")
    elif isinstance(dig, dict):
        refusals.extend(_unknown_fields(dig, {"algo", "value"},
                                        f"{where}.digest"))
        dv = _canon_sha(dig.get("value"))
        if dig.get("algo") != "sha256" or dv is None:
            # An 'md5' (or any non-sha256) claim with a hex-looking
            # value is never silently re-labeled as sha256.
            refusals.append(f"asset_digest_invalid:{comp}")
        else:
            out["digest"] = {"algo": "sha256", "value": dv}
    else:
        dv = _canon_sha(dig)
        if dv is None:
            refusals.append(f"asset_digest_invalid:{comp}")
        else:
            out["digest"] = {"algo": "sha256", "value": dv}
    if pcls not in _PROVENANCE_CLASSES:
        refusals.append(f"asset_provenance_invalid:{comp}")
    else:
        out["provenance"] = {"class": pcls}
    return out


def _norm_source(src, comp: str, refusals: list) -> dict:
    # Build-provenance CLAIMS — workflow declaration and commit claims
    # are metadata, never evidence that the archive was produced by it.
    if src is None:
        return {}
    if not isinstance(src, dict):
        refusals.append(f"source_not_mapping:{comp}")
        return {}
    refusals.extend(_unknown_fields(src, _SOURCE_FIELDS,
                                    f"archives[{comp}].source"))
    out = {}
    for k in ("repo", "workflow", "workflow_ref"):
        if src.get(k) is not None:
            if not _is_str(src[k], 256):
                refusals.append(f"source_{k}_invalid:{comp}")
            else:
                out[k] = src[k].strip()
    be = src.get("build_evidence")
    if be is not None:
        if not _is_str(be, _MAX_URL):
            refusals.append(f"source_build_evidence_invalid:{comp}")
        else:
            out["build_evidence"] = be.strip()
    return out


def _norm_license(lic, comp: str, refusals: list) -> dict:
    if lic is None:
        return {}
    if not isinstance(lic, dict):
        refusals.append(f"license_not_mapping:{comp}")
        return {}
    refusals.extend(_unknown_fields(lic, _LICENSE_FIELDS,
                                    f"archives[{comp}].license"))
    out = {}
    for k in ("name", "url", "redistribution"):
        if lic.get(k) is not None:
            if not _is_str(lic[k], 256):
                refusals.append(f"license_{k}_invalid:{comp}")
            else:
                out[k] = lic[k].strip()
    return out


def _norm_archives(archives, refusals: list) -> list:
    if not isinstance(archives, list) or not archives:
        refusals.append("archives_missing")
        return []
    if len(archives) > _MAX_ARCHIVES:
        refusals.append("archives_over_bound")
        return []
    seen = {}
    names_seen = {}
    urls_seen = {}
    required = 0
    out = []
    for i, a in enumerate(archives):
        if not isinstance(a, dict):
            refusals.append(f"archive_{i}_not_mapping")
            continue
        refusals.extend(_unknown_fields(a, _ARCHIVE_FIELDS,
                                        f"archives[{i}]"))
        comp = a.get("component")
        if not _valid_component(comp):
            refusals.append(f"component_invalid:{i}")
            continue
        if comp in seen:
            refusals.append(f"duplicate_component:{comp}")
            continue
        seen[comp] = True
        role = a.get("role")
        if role not in _COMPONENT_ROLES:
            refusals.append(f"role_invalid:{comp}")
            role = None
        req = a.get("required")
        if not isinstance(req, bool):
            refusals.append(f"required_flag_invalid:{comp}")
            req = False
        asset = _norm_asset(a.get("asset"), comp, refusals)
        if req and role is not None:
            required += 1
        if asset is not None:
            key = asset.get("name", "").casefold()
            if key in names_seen and names_seen[key] != comp:
                refusals.append(
                    f"contradictory_asset_identity:{comp}:{names_seen[key]}")
            else:
                names_seen[key] = comp
            url = asset.get("url")
            if url and url in urls_seen and urls_seen[url] != comp:
                refusals.append(
                    f"duplicate_asset_url:{comp}:{urls_seen[url]}")
            else:
                urls_seen[url] = comp
        out.append({"component": comp, "role": role, "required": req,
                    "asset": asset,
                    "source": _norm_source(a.get("source"), comp,
                                           refusals),
                    "license": _norm_license(a.get("license"), comp,
                                             refusals)})
    if required == 0:
        refusals.append("no_required_component")
    # Deterministic ordering: component id, never caller order.
    out.sort(key=lambda a: a["component"])
    return out


def _norm_members(members, components: set, refusals: list) -> list:
    if not isinstance(members, list) or not members:
        refusals.append("members_missing")
        return []
    if len(members) > _MAX_MEMBERS:
        refusals.append("members_over_bound")
        return []
    dest_seen = {}
    out = []
    for i, m in enumerate(members):
        if not isinstance(m, dict):
            refusals.append(f"member_{i}_not_mapping")
            continue
        refusals.extend(_unknown_fields(m, _MEMBER_FIELDS,
                                        f"members[{i}]"))
        name = m.get("name")
        comp = m.get("component")
        if not _is_str(name, _MAX_NAME) or not valid_member_name(name):
            refusals.append(
                f"member_name_invalid:{name[:80]!r}"
                if isinstance(name, str)
                else "member_name_invalid")
            continue
        if not _valid_component(comp) or comp not in components:
            refusals.append(
                f"member_component_unknown:{name}:{comp!r}"
                if isinstance(comp, str) or comp is None
                else f"member_component_unknown:{name}")
            continue
        folded = name.casefold()
        if folded in dest_seen:
            prev_name = dest_seen[folded][0]
            if prev_name == name:
                refusals.append(
                    f"duplicate_member_destination:{name}:{comp}")
            else:
                refusals.append(
                    f"member_case_collision:{prev_name}:{name}")
            continue
        dest_seen[folded] = (name, comp)
        sha = _canon_sha(m.get("sha256"))
        if sha is None:
            refusals.append(f"member_sha256_invalid:{name}")
            continue
        size = m.get("size")
        if isinstance(size, bool):
            refusals.append(f"member_size_boolean:{name}")
            continue
        if not _is_size(size):
            refusals.append(f"member_size_invalid:{name}")
            continue
        out.append({"name": name, "component": comp,
                    "size": size, "sha256": sha})
    # Deterministic ordering: destination name (casefold), never
    # caller order.
    out.sort(key=lambda m: m["name"].casefold())
    return out


def _norm_coverage(cov, refusals: list) -> dict:
    if not isinstance(cov, dict):
        refusals.append("coverage_not_mapping")
        return {}
    refusals.extend(_unknown_fields(cov, _COVERAGE_FIELDS, "coverage"))
    mode = cov.get("mode")
    if mode not in _COVERAGE_MODES:
        refusals.append("coverage_mode_invalid")
        mode = None
    pol = cov.get("unexpected_policy")
    if pol not in _UNEXPECTED_POLICIES:
        refusals.append("coverage_policy_invalid")
        pol = None
    skips = cov.get("skip_names")
    if skips is None:
        skips = []
    if not isinstance(skips, list) or len(skips) > _MAX_MEMBERS \
            or any(not _is_str(s, _MAX_NAME) or not valid_member_name(s)
                   for s in skips):
        refusals.append("coverage_skip_names_invalid")
        skips = []
    return {"mode": mode, "unexpected_policy": pol,
            "skip_names": sorted({s.casefold(): s for s in skips}.values(),
                                 key=str.casefold)}


def validate_reference(ref) -> dict:
    """Strict structural validation of immutable reference CONTENT
    only. Lifecycle, approval and authority live outside this body.
    Returns {"ok","refusals","reference"} — never mutates input."""
    if not isinstance(ref, dict):
        return _env({"ok": False, "refusals": ["reference_not_mapping"],
                     "reference": None})
    refusals = list(_unknown_fields(ref, _REF_FIELDS, "reference"))
    out = {}
    if ref.get("contract") != RREF_VERSION:
        refusals.append("contract_version_unsupported")
    else:
        out["contract"] = RREF_VERSION
    for k, bound in (("runtime_key", 64), ("flavor", 32)):
        if not _is_str(ref.get(k), bound):
            refusals.append(f"{k}_invalid")
        else:
            out[k] = ref[k].strip()
    out["release"] = _norm_release(ref.get("release"), refusals)
    archives = _norm_archives(ref.get("archives"), refusals)
    components = {a["component"] for a in archives}
    out["archives"] = archives
    out["members"] = _norm_members(ref.get("members"), components,
                                  refusals)
    out["coverage"] = _norm_coverage(ref.get("coverage"), refusals)
    if refusals:
        return _env({"ok": False, "refusals": refusals,
                     "reference": None})
    return _env({"ok": True, "refusals": [], "reference": out})


def reference_digest(ref) -> str | None:
    """Canonical SHA-256 over the IMMUTABLE reference content only.
    Lifecycle, approval and authority never enter this digest (B2-1);
    a lifecycle transition therefore cannot change content identity."""
    res = validate_reference(ref)
    if not res["ok"]:
        return None
    return DIGEST_PREFIX + hashlib.sha256(
        _canon(res["reference"]).encode()).hexdigest()


def build_reference(ref: dict) -> dict:
    """Construct + validate a reference candidate. The result is
    structurally normalized content — NOT an approved or active
    reference."""
    res = validate_reference(ref)
    if not res["ok"]:
        return {**res, "digest": None}
    return _env({"ok": True, "refusals": [],
                 "reference": res["reference"],
                 "digest": reference_digest(res["reference"])})


def _reject_dup_keys(pairs):
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"duplicate_json_key:{k}")
        seen[k] = v
    return seen


def loads_reference(text: str) -> dict:
    """Parse serialized reference content with duplicate-key refusal
    (R54), then validate. Returns the validate_reference shape."""
    try:
        obj = json.loads(text, object_pairs_hook=_reject_dup_keys)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        detail = str(exc)
        if "duplicate_json_key" in detail:
            code = detail.split(":", 1)[0]
            return _env({"ok": False, "refusals": [code],
                         "reference": None})
        return _env({"ok": False,
                     "refusals": ["reference_json_invalid"],
                     "reference": None})
    return validate_reference(obj)


def validate_release_metadata(rel) -> dict:
    """Pure GitHub-release-shaped metadata parsing (B2-4) — synthetic
    fixtures only. Claims stay claims: target_commitish normalizes to
    declared_commitish (not git-ref resolution), uploader/workflow are
    recorded claims, a populated digest is recorded as the platform-
    attested class never as publisher authentication."""
    if not isinstance(rel, dict):
        return _env({"ok": False,
                     "refusals": ["metadata_not_mapping"],
                     "metadata": None})
    refusals = list(_unknown_fields(rel, _METADATA_FIELDS, "metadata"))
    out = {}
    rid = rel.get("release_id")
    if not _is_int(rid) or rid <= 0:
        refusals.append("release_id_invalid")
    else:
        out["release_id"] = rid
    if not _is_str(rel.get("tag"), 128):
        refusals.append("tag_invalid")
    else:
        out["tag"] = rel["tag"].strip()
    ci = rel.get("target_commitish")
    if ci is None:
        ci = rel.get("declared_commitish")
    if ci is not None:
        if not _is_str(ci, 128):
            refusals.append("declared_commitish_invalid")
        else:
            out["declared_commitish"] = ci.strip()
    else:
        out["declared_commitish"] = None
    rs = rel.get("resolved_ref_sha")
    if rs is not None:
        sha = _canon_sha1(rs)
        if sha is None:
            refusals.append("resolved_ref_sha_invalid")
        else:
            out["resolved_ref_sha"] = sha
    else:
        out["resolved_ref_sha"] = None
    if rel.get("published_at") is not None:
        if not _is_str(rel["published_at"], 64):
            refusals.append("published_at_invalid")
        else:
            out["published_at"] = rel["published_at"].strip()
    author = rel.get("author")
    if author is None:
        author = rel.get("author_claim")
    if author is not None:
        if not _is_str(author, 128):
            refusals.append("author_invalid")
        else:
            # Uploader/author labels are CLAIMS — recorded, never
            # authority.
            out["author_claim"] = author.strip()
    assets = rel.get("assets")
    if not isinstance(assets, list) or len(assets) > 64:
        refusals.append("assets_missing_or_unbounded")
        assets = []
    norm_assets = []
    for i, a in enumerate(assets):
        if not isinstance(a, dict):
            refusals.append(f"asset_{i}_not_mapping")
            continue
        refusals.extend(_unknown_fields(a, _META_ASSET_FIELDS,
                                        f"assets[{i}]"))
        na = {}
        if not _is_str(a.get("name"), _MAX_NAME):
            refusals.append(f"asset_name_invalid:{i}")
            continue
        na["name"] = a["name"]
        if a.get("id") is not None:
            if not _is_int(a["id"]) or a["id"] <= 0:
                refusals.append(f"asset_id_invalid:{na['name']}")
            else:
                na["id"] = a["id"]
        if a.get("url") is not None:
            if not _valid_url(a["url"]):
                refusals.append(f"asset_url_invalid:{na['name']}")
            else:
                na["url"] = a["url"]
        if a.get("size") is not None:
            if not _is_size(a["size"]):
                refusals.append(f"asset_size_invalid:{na['name']}")
            else:
                na["size"] = a["size"]
        dv = a.get("digest")
        if dv is None:
            na["digest"] = None  # missing stays missing (R27)
        else:
            cv = _canon_digest(dv)
            if cv is None:
                refusals.append(f"asset_digest_malformed:{na['name']}")
            else:
                na["digest"] = cv
        upl = a.get("uploader")
        if upl is None:
            upl = a.get("uploader_claim")
        if upl is not None:
            if not _is_str(upl, 128):
                refusals.append(f"asset_uploader_invalid:{na['name']}")
            else:
                na["uploader_claim"] = upl.strip()
        for k in ("created_at", "updated_at"):
            if a.get(k) is not None:
                if not _is_str(a[k], 64):
                    refusals.append(f"asset_{k}_invalid:{na['name']}")
                else:
                    na[k] = a[k].strip()
        norm_assets.append(na)
    out["assets"] = norm_assets
    if refusals:
        return _env({"ok": False, "refusals": refusals,
                     "metadata": None})
    return _env({"ok": True, "refusals": [], "metadata": out})


def select_asset(metadata: dict, needle: str) -> dict:
    """Select exactly one asset whose name contains needle
    (case-insensitive) AND ends .zip. Ambiguous selection — zero or
    more than one match — refuses; the first candidate is never
    silently taken."""
    res = validate_release_metadata(metadata)
    if not res["ok"]:
        return {**res, "asset": None}
    if not _is_str(needle, 128):
        return _env({"ok": False, "refusals": ["needle_invalid"],
                     "asset": None})
    n = needle.strip().lower()
    hits = [a for a in res["metadata"]["assets"]
            if n in a["name"].lower() and a["name"].lower().endswith(".zip")]
    if not hits:
        return _env({"ok": False, "refusals": ["no_matching_asset"],
                     "asset": None})
    if len(hits) > 1:
        return _env({"ok": False,
                     "refusals": ["ambiguous_asset_selection"],
                     "candidates": sorted(h["name"] for h in hits),
                     "asset": None})
    return _env({"ok": True, "refusals": [], "asset": hits[0]})


def validate_approval(appr, stored_reference=None) -> dict:
    """Validate an approval RECORD (B2-2). A record is only ever
    structurally checked and bound to the RECOMPUTED content digest of
    the stored reference — caller-supplied authority claims are never
    trusted. authority must be the explicit synthetic fixture token."""
    if not isinstance(appr, dict):
        return _env({"ok": False,
                     "refusals": ["approval_not_mapping"],
                     "approval": None})
    refusals = list(_unknown_fields(appr, _APPROVAL_FIELDS, "approval"))
    out = {}
    auth = appr.get("authority")
    if auth not in _ALLOWED_AUTHORITY:
        refusals.append("authority_source_unestablished")
    else:
        out["authority"] = AUTHORITY_SYNTHETIC
    for k in ("approved_by", "approved_at", "approval_ref"):
        if not _is_str(appr.get(k), 256):
            refusals.append(f"{k}_invalid")
        else:
            out[k] = appr[k].strip()
    rd = appr.get("reference_digest")
    if not isinstance(rd, str) or not rd.startswith(DIGEST_PREFIX) \
            or _canon_sha(rd[len(DIGEST_PREFIX):]) is None:
        refusals.append("approval_digest_invalid")
    else:
        out["reference_digest"] = rd
    if stored_reference is not None:
        actual = reference_digest(stored_reference)
        if actual is not None and out.get("reference_digest") != actual:
            # The approval claims a different content digest than the
            # stored reference's recomputed identity — approval can
            # never transfer to changed content (R09).
            refusals.append("approval_digest_mismatch")
    if refusals:
        return _env({"ok": False, "refusals": refusals,
                     "approval": None})
    return _env({"ok": True, "refusals": [], "approval": out})


class ReferenceRegistry:
    """In-memory synthetic registry (B2-5/B2-6) — mechanism only.
    Entries carry {reference, approval, lifecycle}; nothing here is a
    production authority source, and no resolved reference is
    executable."""

    def __init__(self):
        self._entries: dict[str, dict] = {}

    def _result(self, state: str, **extra) -> dict:
        out = {**_NON_EXECUTABLE,
               "authority_class": AUTHORITY_SYNTHETIC,
               "state": state, **extra}
        out.setdefault("ok", False)
        return out

    def add(self, reference: dict) -> dict:
        """Register immutable reference content under its content
        digest. Lifecycle begins at 'proposed'; a second identical
        add is idempotent, a digest collision with different content
        is impossible by construction."""
        res = build_reference(reference)
        if not res["ok"]:
            return self._result(R_INVALID, refusals=res["refusals"])
        digest = res["digest"]
        if digest is None:
            return self._result(R_INVALID,
                                refusals=["digest_uncomputable"])
        if digest in self._entries:
            return self._result("already_registered", digest=digest,
                                ok=True)
        self._entries[digest] = {
            "reference": res["reference"], "approval": None,
            "lifecycle": {"state": L_PROPOSED, "supplanted_by": None,
                          "history": [L_PROPOSED]}}
        return self._result(L_PROPOSED, digest=digest, ok=True)

    def _entry(self, digest: str):
        return self._entries.get(digest)

    def _transition(self, entry, to_state: str, refusals: list) -> None:
        entry["lifecycle"]["state"] = to_state
        entry["lifecycle"]["history"].append(to_state)

    def approve(self, digest: str, approval: dict) -> dict:
        entry = self._entry(digest)
        if entry is None:
            return self._result(R_ABSENT, digest=digest)
        st = entry["lifecycle"]["state"]
        if st not in (L_PROPOSED, L_SUPERSEDED):
            return self._result("transition_refused",
                                digest=digest,
                                refusals=[f"invalid_transition:{st}->approved"])
        res = validate_approval(approval, entry["reference"])
        if not res["ok"]:
            return self._result(R_AUTHORITY_UNESTABLISHED,
                                digest=digest, refusals=res["refusals"])
        if res["approval"]["reference_digest"] != digest:
            return self._result(R_AUTHORITY_UNESTABLISHED,
                                digest=digest,
                                refusals=["approval_digest_mismatch"])
        entry["approval"] = res["approval"]
        self._transition(entry, L_APPROVED, [])
        return self._result(L_APPROVED, digest=digest, ok=True)

    def _active_for(self, key: str, flavor: str) -> list:
        return [d for d, e in self._entries.items()
                if e["lifecycle"]["state"] == L_ACTIVE
                and e["reference"]["runtime_key"] == key
                and e["reference"]["flavor"] == flavor]

    def activate(self, digest: str) -> dict:
        """approved -> active. Requires the entry's own bound approval
        and no other active reference for the same key+flavor —
        supersession is always an explicit prior operation, never
        implicit replacement (R44/R45/R50/R51)."""
        entry = self._entry(digest)
        if entry is None:
            return self._result(R_ABSENT, digest=digest)
        st = entry["lifecycle"]["state"]
        if st != L_APPROVED:
            return self._result("transition_refused", digest=digest,
                                refusals=[
                                    f"invalid_transition:{st}->active"])
        if entry["approval"] is None:
            return self._result(R_AUTHORITY_UNESTABLISHED, digest=digest,
                                refusals=["approval_absent"])
        ref = entry["reference"]
        others = self._active_for(ref["runtime_key"], ref["flavor"])
        if others:
            return self._result(R_AMBIGUOUS, digest=digest,
                                refusals=[
                                    "activation_conflicts_with_active"],
                                conflicting=sorted(others))
        self._transition(entry, L_ACTIVE, [])
        return self._result(L_ACTIVE, digest=digest, ok=True)

    def supersede(self, old_digest: str, new_digest: str) -> dict:
        """Explicit supersession: old must be active, new must be
        approved or active, and the supplanted_by chain must not
        cycle (R48/R49)."""
        old = self._entry(old_digest)
        new = self._entry(new_digest)
        if old is None or new is None:
            return self._result("transition_refused",
                                refusals=["unknown_supersession_target"],
                                old=old_digest, new=new_digest)
        if old_digest == new_digest:
            return self._result("transition_refused",
                                refusals=["cyclic_supersession"],
                                digest=old_digest)
        if old["lifecycle"]["state"] != L_ACTIVE:
            return self._result("transition_refused",
                                digest=old_digest,
                                refusals=[
                                    "supersede_requires_active_source"])
        if new["lifecycle"]["state"] not in (L_APPROVED, L_ACTIVE):
            return self._result("transition_refused",
                                digest=new_digest,
                                refusals=[
                                    "supersede_target_not_approved"])
        # Cycle check: walk new's supplanted_by chain — reaching old
        # would close a loop.
        seen, cur = set(), new_digest
        while cur is not None:
            if cur == old_digest:
                return self._result("transition_refused",
                                    digest=new_digest,
                                    refusals=["cyclic_supersession"])
            if cur in seen:
                break
            seen.add(cur)
            e = self._entries.get(cur)
            cur = e["lifecycle"]["supplanted_by"] if e else None
        self._transition(old, L_SUPERSEDED, [])
        old["lifecycle"]["supplanted_by"] = new_digest
        return self._result(L_SUPERSEDED, digest=old_digest,
                            supplanted_by=new_digest, ok=True)

    def revoke(self, digest: str) -> dict:
        entry = self._entry(digest)
        if entry is None:
            return self._result(R_ABSENT, digest=digest)
        st = entry["lifecycle"]["state"]
        if L_REVOKED not in _TRANSITIONS[st]:
            return self._result("transition_refused", digest=digest,
                                refusals=[
                                    f"invalid_transition:{st}->revoked"])
        self._transition(entry, L_REVOKED, [])
        return self._result(L_REVOKED, digest=digest, ok=True)

    def resolve(self, runtime_key: str, flavor: str) -> dict:
        """Read-path resolution (B2-6). Reports the registry's state
        for the requested identity — never production authority and
        never executable."""
        cands = {d: e for d, e in self._entries.items()
                 if e["reference"]["runtime_key"] == runtime_key
                 and e["reference"]["flavor"] == flavor}
        if not cands:
            return self._result(R_ABSENT, runtime_key=runtime_key,
                                flavor=flavor)
        active = [d for d, e in cands.items()
                  if e["lifecycle"]["state"] == L_ACTIVE]
        if len(active) > 1:
            return self._result(R_AMBIGUOUS, runtime_key=runtime_key,
                                flavor=flavor, conflicting=sorted(active))
        if active:
            d = active[0]
            appr = cands[d]["approval"]
            if appr is None or appr.get("reference_digest") != d:
                return self._result(R_AUTHORITY_UNESTABLISHED,
                                    digest=d, runtime_key=runtime_key,
                                    flavor=flavor)
            return self._result(R_SYNTHETIC_ACTIVE, digest=d,
                                reference=cands[d]["reference"],
                                approval=appr,
                                runtime_key=runtime_key, flavor=flavor)
        states = {e["lifecycle"]["state"] for e in cands.values()}
        if states == {L_REVOKED}:
            return self._result(R_REVOKED, runtime_key=runtime_key,
                                flavor=flavor)
        if L_APPROVED in states or L_SUPERSEDED in states:
            superseded = [d for d, e in cands.items()
                          if e["lifecycle"]["state"] == L_SUPERSEDED]
            if superseded and L_APPROVED not in states:
                return self._result(R_SUPERSEDED,
                                    superseded_by=sorted({
                                        e["lifecycle"]["supplanted_by"]
                                        for e in cands.values()
                                        if e["lifecycle"]["supplanted_by"]}),
                                    runtime_key=runtime_key, flavor=flavor)
            return self._result(R_APPROVED_INACTIVE,
                                runtime_key=runtime_key, flavor=flavor)
        if states == {L_PROPOSED}:
            return self._result(R_UNAPPROVED, runtime_key=runtime_key,
                                flavor=flavor)
        return self._result(R_UNAPPROVED,
                            runtime_key=runtime_key, flavor=flavor)


__all__ = [
    "AUTHORITY_SYNTHETIC",
    "CONTRACT",
    "COVERAGE_FLAT",
    "DIGEST_PREFIX",
    "L_ACTIVE",
    "L_APPROVED",
    "L_PROPOSED",
    "L_REVOKED",
    "L_SUPERSEDED",
    "P_INDEPENDENT",
    "P_OBSERVED",
    "P_PLATFORM",
    "P_PUBLISHER",
    "P_UNVERIFIED",
    "ROLE_DEPENDENCY",
    "ROLE_OPTIONAL",
    "ROLE_PRIMARY",
    "RREF_VERSION",
    "R_ABSENT",
    "R_AMBIGUOUS",
    "R_APPROVED_INACTIVE",
    "R_AUTHORITY_UNESTABLISHED",
    "R_INVALID",
    "R_REVOKED",
    "R_SUPERSEDED",
    "R_SYNTHETIC_ACTIVE",
    "R_UNAPPROVED",
    "ReferenceRegistry",
    "build_reference",
    "loads_reference",
    "reference_digest",
    "select_asset",
    "validate_approval",
    "validate_reference",
    "validate_release_metadata",
]
