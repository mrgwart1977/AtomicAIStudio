"""U-5d-0b Phase beta-3-B3.2 — isolated verified runtime acquisition and
safe multi-archive extraction foundation (racq:v0).

ISOLATED FOUNDATION. Nothing in this module contacts a network, reads a
production runtime/model directory, publishes an installation, enrolls a
reference, or executes anything. It exists so the multi-archive
verified-acquisition mechanics can be tested honestly with synthetic
references, synthetic archives and injected transports.

Contract racq:v0 is provisional — it has no production consumers.

Acquisition evidence contract (racq:v0):

- The envelope binds an EXACT immutable rref:v0 reference digest; the
  reference is validated via runtime_reference.validate_reference and
  never re-interpreted here.
- A separate SyntheticAcquisitionAuthority holds explicit permits
  declaring the exact permitted acquisition: reference digest, runtime
  identity, selected component set, release and asset identities,
  expected sizes/digests and permitted operations. A submitted
  acquisition can never authorize itself; a favorable label, boolean or
  token is never sufficient. Synthetic authority is NOT production
  authority — the fixture is a test context, not a security boundary
  against arbitrary in-process Python.
- Acquisition-before-enrollment: a structurally valid UNAPPROVED
  reference may serve as the declared expectation set for synthetic
  comparison. It is never promoted to approved or active. Workflow 1
  (controlled member discovery from digest-pinned assets) remains held;
  only workflow 2 (reference-bound acquisition of a complete declared
  expectation set) is implemented.
- Exact component selection: all required components must be selected;
  optional components must be explicitly selected or explicitly omitted.
  Unexpected bytes or members from unselected components refuse.
- Exact asset resolution: release/asset metadata must match the
  reference-declared identities exactly — no substring-first-match, no
  newer-version or cross-flavor substitution, no silent hash
  replacement.
- Injected transport ONLY. There is no default live transport, no
  socket, no DNS — a caller-supplied iterable of byte chunks models the
  stream. Production transport integration remains deferred.
- Archive verification precedes extraction: every selected archive is
  streamed with exact-size and SHA-256 enforcement before any
  managed-member output is written.
- Archive preflight is strict flat-install semantics: no silent
  flattening, no Path(name).name repair, no extractall.
- Extraction is authorized only into an explicitly supplied isolated
  staging root; destinations are created exclusively and never
  overwritten. Extracted bytes are inert test content.
- Publication, generation selection, trusted enrollment and execution
  are never performed here. All results carry
  production_trust:false and execution_eligible:false.

Canonical identity (racq:v0 digest):
  acquisition_digest = "racq:v0:" + sha256(_canon(identity))
  where identity = {"contract","reference_digest","runtime_key",
  "flavor","selected","omitted","assets"} canonicalized as sorted-key
  compact JSON. The "racq:v0:" prefix is a namespacing label OUTSIDE the
  hashed bytes — it is not cryptographic domain separation.

Future corrections documented but NOT implemented here (B3.2-N):
  N1 publication recovery requires exact reference/generation
     association + complete member comparison — directory names and
     file counts are not content identity.
  N2 publication and generation selection are separate decisions — no
     automatic newest-valid selection, no silent replacement, no
     fallback to older generations, rollback needs explicit
     authorization and complete revalidation.
  N3 legacy isolation: new status values alone are not proof —
     generation-aware keys vs dedicated persistence must be decided
     after exact consumer assessment; no no-migration preapproval.
  N4 concurrency/lifetime: generation-level coordination, ownership and
     conflict handling are future work; logical immutability is not
     tamper resistance; active process paths prove nothing about
     dependency lifetime or loaded identity.
  N5 real-acquisition budget must separately bound transferred bytes,
     retries, retained archives, expanded members and temporary
     storage; ~1 GB transfer / 4 GB staging figures are NOT approved.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat as _statmod
import zipfile
from pathlib import Path

from app.domain import runtime_reference as rr
from app.domain.runtime_integrity import valid_member_name

RACQ_VERSION = "racq:v0"
CONTRACT = RACQ_VERSION

# Result states — a completed synthetic acquisition is never "trusted",
# "installed", "ready" or "qualified".
S_COMPLETED = "completed"
S_REFUSED = "refused"
S_CANCELLED = "cancelled"
S_FAILED = "failed"

# Permitted synthetic operations — acquisition permission is separate
# from extraction permission; neither implies publication, enrollment
# or execution.
OP_ACQUIRE = "acquire"
OP_EXTRACT = "extract"
_OPS = {OP_ACQUIRE, OP_EXTRACT}

AUTHORITY_CLASS = "synthetic_acquisition"

# ---- Resource bounds (synthetic-foundation policy, B3.2-F) ----
# These bound the isolated contract only; they are not production
# limits. Where they mirror rref:v0 component/member bounds the same
# rationale applies.
_MAX_PERMITS = 64                  # synthetic authority permit count
_MAX_PERMIT_ASSETS = 16            # assets declared per permit
_MAX_COMPONENTS = 16               # = rref:v0 _MAX_ARCHIVES
_MAX_RELEASES = 8                  # releases in synthetic metadata
_MAX_RELEASE_ASSETS = 256          # assets in one synthetic release
_MAX_META_STRING = 512             # metadata string bound
_MAX_ARCHIVE_BYTES = 4 * 1024 ** 3     # per-archive compressed ceiling
_MAX_AGGREGATE_ARCHIVES = 8 * 1024 ** 3  # aggregate compressed ceiling
_MAX_CHUNK = 4 * 1024 * 1024       # per-transport-chunk bound
_MAX_CHUNKS = 1 << 16              # bounded progress policy
_MAX_EMPTY_CHUNKS = 1024           # non-progressing stream bound
_MAX_ARCHIVE_ENTRIES = 2048        # ZIP entries per archive
_MAX_MEMBER_BYTES = 4 * 1024 ** 3  # per-member expanded ceiling
_MAX_EXPANDED_BYTES = 8 * 1024 ** 3    # aggregate expanded ceiling
_MAX_RATIO = 100                   # expanded/compressed per deflated entry
_MAX_IN_MEMORY_ARCHIVE = 64 * 1024 * 1024  # BytesIO association path
_MAX_REFUSALS = 64
_MAX_REFUSAL_LEN = 160
_MAX_SKIP = 2048                   # = rref:v0 _MAX_MEMBERS
_IO_CHUNK = 1024 * 1024

_SHA256_RE_CHARS = frozenset("0123456789abcdef")
_DIGEST_PREFIX = "racq:v0:"

_NON_EXECUTABLE = {
    "production_trust": False,
    "execution_eligible": False,
    "operational_qualification": "unestablished",
    "trusted_enrollment": "unestablished",
    "installation_publication": "not_performed",
    "authority_class": AUTHORITY_CLASS,
}

# ------------------------------------------------------------------ utils


def _env(result: dict) -> dict:
    """Every result is a racq:v0 envelope — structurally non-executable,
    never a trust, enrollment, installation or qualification claim."""
    return {**_NON_EXECUTABLE, "contract": RACQ_VERSION, **result}


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_str(v, bound=_MAX_META_STRING) -> bool:
    return isinstance(v, str) and 0 < len(v) <= bound and v == v.strip()


def _is_sha256(v) -> bool:
    return (isinstance(v, str) and len(v) == 64
            and all(c in _SHA256_RE_CHARS for c in v.lower()))


def _is_size(v) -> bool:
    return _is_int(v) and 0 <= v


def _cancelled(res: dict | None = None) -> dict:
    out = res or {}
    return _env({"ok": False, "state": S_CANCELLED,
                 "refusals": out.get("refusals", ["cancelled"]),
                 "reference": out.get("reference"),
                 "authority": out.get("authority"),
                 "components": out.get("components"),
                 "archives": out.get("archives", []),
                 "members": out.get("members", []),
                 "skipped": out.get("skipped", []),
                 "coverage": out.get("coverage"),
                 "staging": out.get("staging")})


def _stage_refusal(stage: dict, *codes: str) -> dict:
    """Refused envelope carrying accurate partial evidence — a refusal
    never reports success but must not hide what was already
    established."""
    return _env({"ok": False, "state": S_REFUSED,
                 "refusals": [str(c)[:_MAX_REFUSAL_LEN]
                              for c in codes][:_MAX_REFUSALS],
                 "reference": stage.get("reference"),
                 "authority": stage.get("authority"),
                 "components": stage.get("components"),
                 "archives": stage.get("archives", []),
                 "members": stage.get("members", []),
                 "skipped": stage.get("skipped", []),
                 "coverage": stage.get("coverage"),
                 "staging": stage.get("staging")})


def _failed(error_class: str, res: dict | None = None) -> dict:
    out = res or {}
    return _env({"ok": False, "state": S_FAILED,
                 "refusals": [f"operation_failed:{error_class}"],
                 "reference": out.get("reference"),
                 "authority": out.get("authority"),
                 "components": out.get("components"),
                 "archives": out.get("archives", []),
                 "members": out.get("members", []),
                 "skipped": out.get("skipped", []),
                 "coverage": out.get("coverage"),
                 "staging": out.get("staging")})


class _Cancelled(Exception):
    pass


def _check_cancel(cancel_check) -> None:
    if cancel_check is None:
        return
    try:
        flag = cancel_check()
    except Exception as exc:
        if "ancel" in type(exc).__name__:
            raise _Cancelled() from exc
        raise
    if flag:
        raise _Cancelled()


# ------------------------------------------------------- authority (B3.2-B)

_PERMIT_FIELDS = {"reference_digest", "runtime_key", "flavor",
                  "components", "release_id", "tag", "assets",
                  "permitted_ops"}
_PERMIT_ASSET_FIELDS = {"asset_id", "name", "size", "sha256", "source"}


class SyntheticAcquisitionAuthority:
    """Explicitly separate synthetic acquisition-authority context.

    The acquisition may ONLY proceed when the registry-style fixture
    declares an exact permit for the submitted plan — a caller-supplied
    authorized/approved flag, authority label, favorable provenance or
    token is never sufficient. Permits are deep-frozen into canonical
    form at construction; caller mutation of the original dicts cannot
    alter the established context.

    Acquisition and extraction are SEPARATE operations in the permit:
    an acquire-only permit cannot authorize extraction. Neither
    operation authorizes publication, enrollment or execution.

    This models the synthetic-approval association boundary only — not
    a production trust root, credential, signature check or defense
    against arbitrary in-process Python."""

    def __init__(self, permits):
        """permits: iterable of exact permit contents —
        {reference_digest, runtime_key, flavor, components,
         release_id, tag, assets: {component: {asset_id, name, size,
         sha256, source}}, permitted_ops: subset of {acquire,extract}}."""
        if not isinstance(permits, (list, tuple)):
            raise TypeError("acq_permits_invalid")
        if len(permits) > _MAX_PERMITS:
            raise ValueError("acq_permits_over_bound")
        frozen = set()
        for p in permits:
            if not isinstance(p, dict):
                raise TypeError("acq_permit_not_mapping")
            if set(p) != _PERMIT_FIELDS:
                raise ValueError("acq_permit_fields_invalid")
            rd = p["reference_digest"]
            if not isinstance(rd, str) \
                    or not rd.startswith(rr.DIGEST_PREFIX):
                raise ValueError("acq_permit_digest_invalid")
            for k in ("runtime_key", "tag", "flavor"):
                if not _is_str(p[k], 128):
                    raise ValueError("acq_permit_field_invalid")
            comps = p["components"]
            if not isinstance(comps, list) or not comps \
                    or len(comps) > _MAX_COMPONENTS \
                    or any(not _is_str(c, 128) for c in comps):
                raise ValueError("acq_permit_components_invalid")
            if len(set(comps)) != len(comps):
                raise ValueError("acq_permit_components_duplicate")
            rid = p["release_id"]
            if not _is_int(rid) or rid <= 0:
                raise ValueError("acq_permit_release_invalid")
            assets = p["assets"]
            if not isinstance(assets, dict) or not assets \
                    or len(assets) > _MAX_PERMIT_ASSETS:
                raise ValueError("acq_permit_assets_invalid")
            if set(assets) != set(comps):
                raise ValueError("acq_permit_assets_scope_invalid")
            for a in assets.values():
                if not isinstance(a, dict) \
                        or set(a) != _PERMIT_ASSET_FIELDS:
                    raise ValueError("acq_permit_asset_fields_invalid")
                if not _is_int(a["asset_id"]) or a["asset_id"] <= 0:
                    raise ValueError("acq_permit_asset_id_invalid")
                if not _is_str(a["name"]) \
                        or not _is_str(a["source"], 1024):
                    raise ValueError("acq_permit_asset_field_invalid")
                if not _is_size(a["size"]) \
                        or a["size"] > _MAX_ARCHIVE_BYTES:
                    raise ValueError("acq_permit_asset_size_invalid")
                if not _is_sha256(a["sha256"]):
                    raise ValueError("acq_permit_asset_sha_invalid")
            ops = p["permitted_ops"]
            if not isinstance(ops, list) or not ops \
                    or not set(ops) <= _OPS:
                raise ValueError("acq_permit_ops_invalid")
            norm = dict(p)
            norm["components"] = sorted(comps)
            norm["permitted_ops"] = sorted(set(ops))
            norm["assets"] = {c: dict(a) for c, a in sorted(assets.items())}
            frozen.add(_canon(norm))
        self._permits = frozenset(frozen)

    def permits(self, permit: dict) -> bool:
        """Exact-association query: the canonicalized candidate permit
        must be a member of the frozen set."""
        try:
            norm = dict(permit)
            norm["components"] = sorted(permit["components"])
            norm["permitted_ops"] = sorted(set(permit["permitted_ops"]))
            norm["assets"] = {c: dict(a)
                              for c, a in sorted(permit["assets"].items())}
        except Exception:  # noqa: BLE001 — fail closed on any
            # malformed candidate rather than surfacing a type error
            return False
        return _canon(norm) in self._permits


# ---------------------------------------------------- acquisition identity


def acquisition_digest(identity: dict) -> str:
    """Deterministic acquisition-plan identity. Hashed bytes are the
    canonical JSON of {contract, reference_digest, runtime_key, flavor,
    selected, omitted, assets}; the racq:v0: prefix is namespacing
    OUTSIDE hashed material, not domain separation."""
    return _DIGEST_PREFIX + hashlib.sha256(
        _canon(identity).encode()).hexdigest()


# -------------------------------------------------------- selection (B3.2-D)


def _norm_selection(reference: dict, selection) -> tuple[dict | None, list]:
    """Bind the explicit selected component set to the validated
    reference. Every component must appear in `selection` — required
    must be selected; optional explicitly selected or omitted."""
    refusals = []
    comps = {a["component"]: a for a in reference["archives"]}
    if not isinstance(selection, dict):
        return None, ["selection_not_mapping"]
    unknown = set(selection) - set(comps)
    if unknown:
        refusals.append(
            f"selection_unknown_component:{min(unknown)}")
    missing = set(comps) - set(selection)
    if missing:
        refusals.append(
            f"selection_incomplete:{min(missing)}")
    for comp, flag in selection.items():
        if comp not in comps:
            continue
        if not isinstance(flag, bool):
            refusals.append(f"selection_flag_invalid:{comp}")
            continue
        if comps[comp]["required"] and not flag:
            refusals.append(f"required_component_omitted:{comp}")
    if refusals:
        return None, refusals
    return {"selected": sorted(c for c in comps if selection[c]),
            "omitted": sorted(c for c in comps if not selection[c]),
            "required": sorted(c for c in comps
                               if comps[c]["required"])}, []


# -------------------------------------------------- asset resolver (B3.2-E)

_RELEASE_FIELDS = {"release_id", "tag_name", "assets"}
_META_ASSET_FIELDS = {"id", "name", "size", "browser_download_url",
                      "digest"}


def _valid_url(url) -> bool:
    return (isinstance(url, str) and len(url) <= 1024
            and url.startswith("https://") and " " not in url
            and "//" not in url[8:])


def resolve_assets(reference: dict, selected: list,
                   metadata) -> tuple[dict | None, list]:
    """Bounded pure resolver over synthetic release metadata — exact
    identity association, never substring-first-match."""
    refusals = []
    rel = reference["release"]
    releases = metadata if isinstance(metadata, list) else [metadata]
    if len(releases) > _MAX_RELEASES:
        return None, ["release_metadata_over_bound"]
    matched = []
    for i, m in enumerate(releases):
        if not isinstance(m, dict):
            refusals.append(f"release_{i}_not_mapping")
            continue
        extra = set(m) - _RELEASE_FIELDS
        if extra:
            refusals.append(
                f"release_unknown_field:{min(extra)}")
            continue
        if m.get("release_id") == rel["release_id"] \
                or m.get("tag_name") == rel["tag"]:
            matched.append(m)
    if not matched:
        if not refusals:
            refusals.append("release_missing")
        return None, refusals
    if len(matched) > 1:
        refusals.append("release_ambiguous")
        return None, refusals
    rel_meta = matched[0]
    if rel_meta.get("release_id") != rel["release_id"]:
        refusals.append("release_id_mismatch")
    if rel_meta.get("tag_name") != rel["tag"]:
        refusals.append("release_tag_mismatch")
    assets = rel_meta.get("assets")
    if not isinstance(assets, list) or len(assets) > _MAX_RELEASE_ASSETS:
        refusals.append("release_assets_invalid")
        return None, refusals
    names = {}
    for i, a in enumerate(assets):
        if not isinstance(a, dict):
            refusals.append(f"asset_{i}_not_mapping")
            continue
        extra = set(a) - _META_ASSET_FIELDS
        if extra:
            refusals.append(f"asset_unknown_field:{min(extra)}")
            continue
        n = a.get("name")
        if not _is_str(n):
            refusals.append(f"asset_name_invalid:{i}")
            continue
        if n in names:
            refusals.append(f"asset_duplicate_identity:{n}")
        names[n] = a
    if refusals:
        return None, refusals

    out = {}
    by_comp = {a["component"]: a for a in reference["archives"]}
    for comp in selected:
        expected = by_comp[comp]["asset"]
        cand = names.get(expected["name"])
        if cand is None:
            refusals.append(f"asset_missing:{comp}")
            continue
        # The bound asset identity must be a positive integer whenever
        # it exists in the metadata — and always, since the plan binds
        # it. When the reference also declares an expected id the two
        # must match exactly.
        cid = cand.get("id")
        if not _is_int(cid) or cid <= 0:
            refusals.append(f"asset_id_invalid:{comp}")
        elif "id" in expected and cid != expected["id"]:
            refusals.append(f"asset_id_changed:{comp}")
        size = cand.get("size")
        if not _is_size(size):
            refusals.append(f"asset_size_invalid:{comp}")
        elif size != expected["size"]:
            refusals.append(f"asset_size_changed:{comp}")
        digest = cand.get("digest")
        algo, _, val = (digest or "").partition(":") \
            if isinstance(digest, str) else ("", "", "")
        if not isinstance(digest, str) or not digest:
            refusals.append(f"asset_digest_missing:{comp}")
        elif algo != "sha256" or not _is_sha256(val):
            refusals.append(f"asset_digest_malformed:{comp}")
        elif val.lower() != expected["digest"]["value"]:
            refusals.append(f"asset_digest_changed:{comp}")
        url = cand.get("browser_download_url")
        if not _valid_url(url):
            refusals.append(f"asset_url_unsafe:{comp}")
        elif url != expected["url"]:
            refusals.append(f"asset_url_changed:{comp}")
        if any(r.endswith(f":{comp}") for r in refusals):
            continue
        out[comp] = {"component": comp,
                     "asset_id": cid,
                     "name": expected["name"],
                     "size": expected["size"],
                     "sha256": expected["digest"]["value"],
                     "source": expected["url"]}
    if refusals:
        return None, refusals
    return out, []


# ------------------------------------------------- resume association (B3.2-G)

_RESUME_FIELDS = {"reference_digest", "acquisition", "component",
                  "release_id", "asset_id", "asset_name",
                  "expected_size", "expected_sha256", "source"}


def resume_identity(identity_digest: str, component: str,
                    resolution: dict, reference_digest: str) -> dict:
    """The exact identity a partial transfer may legitimately resume —
    a matching URL and size alone is never sufficient."""
    return {"reference_digest": reference_digest,
            "acquisition": identity_digest,
            "component": component,
            "release_id": None,  # filled by caller if recorded
            "asset_id": resolution["asset_id"],
            "asset_name": resolution["name"],
            "expected_size": resolution["size"],
            "expected_sha256": resolution["sha256"],
            "source": resolution["source"]}


def validate_resume(meta, assoc: dict) -> dict:
    """Pure resume-association validator. `assoc` is the authoritative
    identity; `meta` is stored resume metadata. Stale, foreign,
    malformed or contradictory metadata refuses — a partial file is
    never appended against a changed expected digest.

    Scope: the association validator only. Production Range/retry/
    redirect and persistent sidecar integration remain deferred."""
    if not isinstance(meta, dict):
        return _env({"ok": False, "state": S_REFUSED,
                     "refusals": ["resume_metadata_not_mapping"]})
    extra = set(meta) - _RESUME_FIELDS
    if extra:
        return _env({"ok": False, "state": S_REFUSED,
                     "refusals": [
                         f"resume_unknown_field:{min(extra)}"]})
    if set(meta) != _RESUME_FIELDS:
        return _env({"ok": False, "state": S_REFUSED,
                     "refusals": ["resume_metadata_incomplete"]})
    malformed = []
    if not isinstance(meta["reference_digest"], str) \
            or not meta["reference_digest"].startswith(rr.DIGEST_PREFIX):
        malformed.append("reference_digest")
    if not isinstance(meta["acquisition"], str) \
            or not meta["acquisition"].startswith(_DIGEST_PREFIX):
        malformed.append("acquisition")
    if not _is_str(meta["component"], 128):
        malformed.append("component")
    if meta["release_id"] is not None \
            and (not _is_int(meta["release_id"])
                 or meta["release_id"] <= 0):
        malformed.append("release_id")
    if not _is_int(meta["asset_id"]) or meta["asset_id"] <= 0:
        malformed.append("asset_id")
    if not _is_str(meta["asset_name"]):
        malformed.append("asset_name")
    if not _is_size(meta["expected_size"]):
        malformed.append("expected_size")
    if not _is_sha256(meta["expected_sha256"]):
        malformed.append("expected_sha256")
    if not _valid_url(meta["source"]):
        malformed.append("source")
    if malformed:
        return _env({"ok": False, "state": S_REFUSED,
                     "refusals": [
                         f"resume_malformed:{malformed[0]}"]})
    mismatch = [k for k in _RESUME_FIELDS
                if meta[k] != assoc.get(k)]
    if mismatch:
        return _env({"ok": False, "state": S_REFUSED,
                     "refusals": [
                         f"resume_identity_mismatch:{min(mismatch)}"]})
    return _env({"ok": True, "state": S_COMPLETED, "refusals": [],
                 "resume": dict(meta)})


# ----------------------------------------------- archive verification (B3.2-H)


def _stream_archive(chunks, dest_fh, expected_size: int,
                    cancel_check) -> tuple[dict | None, str | None]:
    """Bounded streaming write+hash. Returns ({bytes,sha256}, None) or
    (None, refusal). Early refusal on overrun; truncation and
    non-progressing streams refuse; cancellation propagates."""
    sha = hashlib.sha256()
    have = 0
    n_chunks = 0
    empty = 0
    for chunk in chunks:
        _check_cancel(cancel_check)
        n_chunks += 1
        if n_chunks > _MAX_CHUNKS:
            return None, "stream_chunk_bound_exceeded"
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            return None, "invalid_chunk"
        b = bytes(chunk)
        if not b:
            empty += 1
            if empty > _MAX_EMPTY_CHUNKS:
                return None, "stream_not_progressing"
            continue
        if len(b) > _MAX_CHUNK:
            return None, "chunk_over_bound"
        have += len(b)
        if have > expected_size:
            return None, "archive_over_expected_size"
        sha.update(b)
        dest_fh.write(b)
    if have != expected_size:
        return None, "archive_truncated"
    return {"bytes": have, "sha256": sha.hexdigest()}, None


# ------------------------------------- verification-to-extraction association


def _open_archive_assoc(path: Path, observed_size: int) -> tuple[object | None, str | None]:
    """Reopen a staged archive for extraction with association checks:
    the opened file's identity is compared to the verified stat (st_dev,
    st_ino where reported, st_size). The SAME opened handle is handed to
    the ZIP parser — a later unrelated or replaced path cannot be
    silently substituted. Platform limitation: st_ino may be 0 on some
    Windows filesystems, in which case the check degrades to
    device+size — documented, never hidden."""
    try:
        st0 = path.stat()
    except OSError:
        return None, "archive_stat_failed"
    if not _statmod.S_ISREG(st0.st_mode):
        return None, "archive_not_regular"
    if st0.st_size != observed_size:
        return None, "archive_association_size_mismatch"
    if path.is_symlink():
        return None, "archive_association_reparse"
    try:
        fh = open(path, "rb")  # noqa: SIM115 — the verified handle is
        # returned open and handed to the ZIP parser for extraction
    except OSError:
        return None, "archive_open_failed"
    fst = os.fstat(fh.fileno())
    same = (fst.st_dev == st0.st_dev and fst.st_size == st0.st_size
            and (fst.st_ino == st0.st_ino or not st0.st_ino))
    if not same:
        fh.close()
        return None, "archive_association_changed"
    return fh, None


# -------------------------------------------------- archive preflight (B3.2-J)

_LOCAL_SIG = b"PK\x03\x04"
_SUPPORTED_METHODS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_SPECIAL_TYPES = {0o120000, 0o140000, 0o060000, 0o020000, 0o010000,
                  0o040000}


def _local_header_consistent(fh, info) -> bool:
    """Detectable local-vs-central contradiction check: the local
    header's filename bytes must equal the central entry's. Bounded —
    reads only the fixed header plus declared name length. Fields the
    stdlib parser does not surface (e.g. full flag semantics) are not
    claimed validated beyond this comparison."""
    try:
        fh.seek(info.header_offset)
        head = fh.read(30)
        if len(head) < 30 or head[:4] != _LOCAL_SIG:
            return False
        nlen = int.from_bytes(head[26:28], "little")
        name = fh.read(nlen)
        return name == info.orig_filename.encode("utf-8") \
            if (info.flag_bits & 0x800) else \
            name == info.orig_filename.encode("cp437", errors="replace")
    except (OSError, ValueError, KeyError):
        return False


def preflight_archive(zf, fh, component: str, expected: dict,
                      skip_names: set, dest_seen: set) -> tuple[list, list]:
    """Strict flat-install preflight over one archive's central
    directory BEFORE any member is written. Returns (entries_out,
    refusals). `expected` = {name: member} for THIS component only;
    `dest_seen` = casefolded destination names already claimed by
    earlier archives (cross-archive collision)."""
    refusals = []
    try:
        infos = zf.infolist()
    except zipfile.BadZipFile:
        return [], ["archive_corrupt_central_directory"]
    if len(infos) > _MAX_ARCHIVE_ENTRIES:
        return [], ["archive_entries_over_bound"]
    seen = {}
    entries = []
    for info in infos:
        name = info.filename
        if "\x00" in name:
            refusals.append(f"member_name_invalid:{name[:40]!r}")
            continue
        if not valid_member_name(name):
            # valid_member_name rejects separators, drives, UNC, ADS,
            # reserved devices, trailing dot/space and control chars —
            # nested paths are refused, never flattened.
            refusals.append(f"member_name_invalid:{name[:40]!r}")
            continue
        folded = name.casefold()
        if folded in seen:
            refusals.append(f"duplicate_member:{name}")
            continue
        seen[folded] = name
        if info.is_dir():
            refusals.append(f"member_name_invalid:{name}")
            continue
        if info.flag_bits & 0x1:
            refusals.append(f"member_encrypted:{name}")
            continue
        if info.compress_type not in _SUPPORTED_METHODS:
            refusals.append(f"member_compression_unsupported:{name}")
            continue
        mode = (info.external_attr >> 16) & 0o170000
        if mode in _SPECIAL_TYPES:
            refusals.append(f"member_special_entry:{name}")
            continue
        if folded in dest_seen:
            refusals.append(f"cross_archive_collision:{name}")
            continue
        exp = expected.get(folded)
        skipped = folded in skip_names
        if exp is None and not skipped:
            refusals.append(f"unexpected_member:{name}")
            continue
        if exp is not None:
            if not _is_size(info.file_size):
                refusals.append(f"member_size_invalid:{name}")
                continue
            if info.file_size != exp["size"]:
                refusals.append(f"member_size_mismatch:{name}")
                continue
            if info.file_size > _MAX_MEMBER_BYTES:
                refusals.append(f"member_over_bound:{name}")
                continue
        comp_size = info.compress_size
        if comp_size is not None and info.file_size is not None \
                and comp_size > 0 and info.compress_type \
                == zipfile.ZIP_DEFLATED \
                and info.file_size // comp_size > _MAX_RATIO:
            refusals.append(f"member_ratio_abuse:{name}")
            continue
        if fh is not None and not _local_header_consistent(fh, info):
            refusals.append(f"archive_local_central_mismatch:{name}")
            continue
        entries.append({"info": info, "name": name,
                        "member": exp, "skipped": skipped})
        if not skipped:
            dest_seen.add(folded)
    return entries, refusals


# --------------------------------------------------------- extraction (B3.2-L)


def _extract_member(zf, info, member: dict, dest_root: Path,
                    cancel_check) -> tuple[dict | None, str | None]:
    """Exclusive-create streaming extraction with single-pass size +
    sha256 comparison against the expected member."""
    dest = dest_root / member["name"]
    try:
        if dest.resolve().parent != dest_root.resolve():
            return None, "destination_containment_violation"
    except OSError:
        return None, "destination_unresolvable"
    if dest.is_symlink() or dest.exists():
        return None, "destination_exists"
    sha = hashlib.sha256()
    have = 0
    try:
        with zf.open(info) as src, open(dest, "xb") as dst:
            while True:
                _check_cancel(cancel_check)
                chunk = src.read(_IO_CHUNK)
                if not chunk:
                    break
                have += len(chunk)
                if have > member["size"] or have > _MAX_MEMBER_BYTES:
                    return None, "member_over_bound"
                sha.update(chunk)
                dst.write(chunk)
    except zipfile.BadZipFile:
        return None, "member_corrupt"
    except NotImplementedError:
        return None, "member_compression_unsupported"
    except RuntimeError:
        return None, "member_encrypted"
    if have != member["size"]:
        return None, "member_size_mismatch"
    if sha.hexdigest() != member["sha256"]:
        return None, "member_sha256_mismatch"
    return {"name": member["name"], "component": member["component"],
            "expected_size": member["size"], "observed_size": have,
            "expected_sha256": member["sha256"],
            "observed_sha256": sha.hexdigest(),
            "state": "match"}, None


# ------------------------------------------------------------ orchestration


def acquire(ref, *, authority, selection, release_metadata,
            transport, staging_root, cancel_check=None) -> dict:
    """Bounded synthetic acquisition of the declared expectation set.

    ref             — rref:v0-shaped reference (unapproved is permitted;
                      it is never promoted).
    authority       — SyntheticAcquisitionAuthority instance (required;
                      a caller-supplied label or flag is not authority).
    selection       — {component: bool} explicit per-component choice.
    release_metadata— synthetic release dict (or list) shaped
                      {release_id, tag_name, assets:[...]}.
    transport       — callable url -> iterable[bytes]; the ONLY byte
                      source. There is no default or live transport.
    staging_root    — caller-supplied isolated test directory; the
                      acquisition writes exclusively beneath it.
    cancel_check    — optional callable; truthy return or a *Cancelled
                      exception yields the cancelled envelope.

    Returns a racq:v0 envelope — never a trust, installation or
    qualification claim."""
    stage: dict = {"refusals": []}
    try:
        return _acquire_inner(ref, authority=authority,
                              selection=selection,
                              release_metadata=release_metadata,
                              transport=transport,
                              staging_root=staging_root,
                              cancel_check=cancel_check, stage=stage)
    except _Cancelled:
        return _cancelled(stage)
    except Exception as exc:  # noqa: BLE001 — the orchestrator is
        # the fail-closed boundary; unexpected failures never report
        # success
        return _failed(type(exc).__name__, stage)


def _acquire_inner(ref, *, authority, selection, release_metadata,
                   transport, staging_root, cancel_check, stage) -> dict:
    # ---- reference validation (rref:v0 reused unchanged)
    vres = rr.validate_reference(ref)
    if not vres["ok"]:
        return _stage_refusal(stage, *["reference_invalid"]
                         + [r[:100] for r in vres["refusals"][:4]])
    reference = vres["reference"]
    ref_digest = rr.reference_digest(ref)
    stage["reference"] = {"digest": ref_digest,
                          "runtime_key": reference["runtime_key"],
                          "flavor": reference["flavor"],
                          "validation_ok": True}

    # ---- authority association (separate context; never self-declared)
    if authority is None:
        stage["authority"] = {"association": "absent"}
        return _stage_refusal(stage, "authority_context_absent")
    if not isinstance(authority, SyntheticAcquisitionAuthority):
        stage["authority"] = {"association": "invalid"}
        return _stage_refusal(stage, "authority_context_invalid")

    # ---- component selection
    sel, srefusals = _norm_selection(reference, selection)
    if srefusals:
        stage["authority"] = {"association": "unchecked"}
        return _stage_refusal(stage, *srefusals)
    stage["components"] = sel
    selected, omitted = sel["selected"], sel["omitted"]

    # ---- exact asset resolution
    resolutions, rrefusals = resolve_assets(reference, selected,
                                            release_metadata)
    if rrefusals:
        stage["authority"] = {"association": "unchecked"}
        return _stage_refusal(stage, *rrefusals)

    # ---- acquisition identity + permit association
    assets_plan = {c: {"asset_id": r["asset_id"], "name": r["name"],
                       "size": r["size"], "sha256": r["sha256"],
                       "source": r["source"]}
                   for c, r in sorted(resolutions.items())}
    rel = reference["release"]
    identity = {"contract": RACQ_VERSION,
                "reference_digest": ref_digest,
                "runtime_key": reference["runtime_key"],
                "flavor": reference["flavor"],
                "selected": selected, "omitted": omitted,
                "assets": assets_plan}
    acq_id = acquisition_digest(identity)
    permit = {"reference_digest": ref_digest,
              "runtime_key": reference["runtime_key"],
              "flavor": reference["flavor"],
              "components": selected,
              "release_id": rel["release_id"],
              "tag": rel["tag"],
              "assets": assets_plan,
              "permitted_ops": [OP_ACQUIRE, OP_EXTRACT]}
    if not authority.permits(permit):
        # An acquire-only permit may still authorize the download
        # phase; extraction is refused separately below.
        acq_permit = dict(permit, permitted_ops=[OP_ACQUIRE])
        if not authority.permits(acq_permit):
            stage["authority"] = {"association": "unmatched"}
            return _stage_refusal(stage, "authority_association_unmatched")
        extract_permitted = False
    else:
        extract_permitted = True
    stage["authority"] = {"association": "established",
                          "class": AUTHORITY_CLASS,
                          "extract_permitted": extract_permitted}

    # ---- staging root validation (isolated caller-supplied workspace)
    if staging_root is None:
        return _stage_refusal(stage, "staging_root_absent")
    try:
        root = Path(staging_root)
        if root.is_symlink():
            return _stage_refusal(stage, "staging_root_unsafe")
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return _stage_refusal(stage, "staging_root_unusable")
        if not root.is_dir():
            return _stage_refusal(stage, "staging_root_unusable")
        acq_dir = root / f"racq-{acq_id[len(_DIGEST_PREFIX):][:16]}"
        acq_dir.mkdir(exist_ok=False)
    except FileExistsError:
        return _stage_refusal(stage, "staging_destination_exists")
    except OSError:
        return _stage_refusal(stage, "staging_root_unusable")
    archives_dir = acq_dir / "archives"
    members_dir = acq_dir / "members"
    archives_dir.mkdir()
    stage["staging"] = {"dir": acq_dir.name, "complete": False}

    # ---- archive verification: ALL selected archives before any
    # extraction (B3.2-H) — expected vs observed kept separate.
    archives_out = []
    total_compressed = 0
    if not callable(transport):
        return _stage_refusal(stage, "transport_not_callable")
    verified: dict[str, dict] = {}
    sel_set = set(selected)
    ordered = [a["component"] for a in reference["archives"]
               if a["component"] in sel_set]
    for comp in ordered:
        res = resolutions[comp]
        if res["size"] > _MAX_ARCHIVE_BYTES:
            return _stage_refusal(stage, f"archive_over_bound:{comp}")
        total_compressed += res["size"]
        if total_compressed > _MAX_AGGREGATE_ARCHIVES:
            return _stage_refusal(stage, "aggregate_archives_over_bound")
        dest = archives_dir / f"{comp}.zip"
        try:
            chunks = transport(res["source"])
        except Exception as exc:  # noqa: BLE001 — a synthetic
            # transport failure is a controlled failed state
            stage["archives"] = archives_out
            return _failed(type(exc).__name__, stage)
        if chunks is None or not hasattr(chunks, "__iter__"):
            return _stage_refusal(stage, "transport_not_iterable")
        use_memory = res["size"] <= _MAX_IN_MEMORY_ARCHIVE
        sink = io.BytesIO() if use_memory \
            else open(dest, "xb")  # noqa: SIM115 — closed in finally
        try:
            obs, why = _stream_archive(iter(chunks), sink,
                                       res["size"], cancel_check)
        finally:
            if not use_memory:
                sink.close()
        if why is not None:
            stage["archives"] = archives_out
            return _stage_refusal(stage, why)
        arec = {"component": comp, "name": res["name"],
                "asset_id": res["asset_id"],
                "expected_size": res["size"],
                "observed_size": obs["bytes"],
                "expected_sha256": res["sha256"],
                "observed_sha256": obs["sha256"],
                "comparison": None, "verified": False}
        if obs["sha256"] != res["sha256"]:
            arec["comparison"] = "digest_mismatch"
            archives_out.append(arec)
            stage["archives"] = archives_out
            return _stage_refusal(stage, f"archive_digest_mismatch:{comp}")
        arec["comparison"] = "match"
        arec["verified"] = True
        archives_out.append(arec)
        verified[comp] = {"record": arec,
                          "bytes": sink.getvalue() if use_memory
                          else None,
                          "path": None if use_memory else dest}
    stage["archives"] = archives_out

    # ---- extraction permission: an acquire-only permit stops here
    if not extract_permitted:
        return _stage_refusal(stage, "operation_not_permitted:extract")

    # ---- complete archive preflight: ALL archives before ANY member
    # write (B3.2-J).
    members_expected = {m["name"].casefold(): m
                        for m in reference["members"]
                        if m["component"] in set(selected)}
    owned = {}
    for m in reference["members"]:
        owned.setdefault(m["name"].casefold(), set()).add(m["component"])
    for fold, owners in owned.items():
        if len(owners) > 1:
            return _stage_refusal(stage, 
                f"member_ownership_ambiguous:{fold}")
    skip_names = {s.casefold()
                  for s in reference["coverage"].get("skip_names", [])}
    if len(skip_names) > _MAX_SKIP:
        return _stage_refusal(stage, "coverage_skip_over_bound")
    dest_seen: set = set()
    prepared: dict[str, dict] = {}
    total_expanded = 0
    for comp in ordered:
        v = verified[comp]
        try:
            if v["bytes"] is not None:
                bio = io.BytesIO(v["bytes"])
                zsrc = zipfile.ZipFile(bio)
                fh_for_local = bio
            else:
                fh, why = _open_archive_assoc(
                    v["path"], v["record"]["observed_size"])
                if why is not None:
                    return _stage_refusal(stage, why)
                zsrc = zipfile.ZipFile(fh)
                fh_for_local = fh
        except zipfile.BadZipFile:
            return _stage_refusal(stage, f"archive_corrupt:{comp}")
        try:
            comp_expected = {k: m for k, m in members_expected.items()
                             if m["component"] == comp}
            entries, pref = preflight_archive(
                zsrc, fh_for_local, comp, comp_expected, skip_names,
                dest_seen)
            if pref:
                return _stage_refusal(stage, *pref)
            for e in entries:
                if e["member"] is not None:
                    total_expanded += e["member"]["size"]
            if total_expanded > _MAX_EXPANDED_BYTES:
                return _stage_refusal(stage, "aggregate_expanded_over_bound")
            prepared[comp] = {"zf": zsrc, "fh": fh_for_local,
                              "entries": entries}
        except zipfile.BadZipFile:
            return _stage_refusal(stage, f"archive_corrupt:{comp}")

    # ---- extraction permission already established; write members
    # exclusively into the isolated staging dir.
    members_dir.mkdir()
    members_out = []
    skipped_out = []
    for comp in ordered:
        p = prepared[comp]
        written = set()
        for e in p["entries"]:
            _check_cancel(cancel_check)
            if e["skipped"]:
                skipped_out.append({"name": e["name"],
                                    "component": comp,
                                    "state": "skipped_declared"})
                continue
            rec, why = _extract_member(p["zf"], e["info"], e["member"],
                                       members_dir, cancel_check)
            if why is not None:
                stage["members"] = members_out
                return _stage_refusal(
                    stage, f"{why}:{e['member']['name']}")
            written.add(e["name"].casefold())
            members_out.append(rec)
        # complete selected-component coverage
        missing = [m["name"] for m in
                   members_expected.values()
                   if m["component"] == comp
                   and m["name"].casefold() not in written]
        if missing:
            stage["members"] = members_out
            return _stage_refusal(stage, 
                f"missing_expected_member:{min(missing)}")
    for comp in ordered:
        p = prepared[comp]
        for h in (p["zf"], p["fh"]):
            if h is not None:
                with contextlib.suppress(Exception):
                    h.close()

    # ---- coverage: distinguish declared vs selected vs upstream truth
    coverage = {"declared_complete": True,
                "selected_complete": True,
                "omitted_optional": omitted,
                "upstream_completeness": "unestablished"}
    stage["staging"]["complete"] = True
    stage["coverage"] = coverage
    stage["members"] = members_out
    stage["skipped"] = skipped_out
    return _env({"ok": True, "state": S_COMPLETED, "refusals": [],
                 "acquisition": acq_id,
                 "reference": stage["reference"],
                 "authority": stage["authority"],
                 "components": stage["components"],
                 "archives": archives_out,
                 "members": members_out,
                 "skipped": skipped_out,
                 "coverage": coverage,
                 "staging": stage["staging"]})


__all__ = [
    "AUTHORITY_CLASS",
    "CONTRACT",
    "OP_ACQUIRE",
    "OP_EXTRACT",
    "RACQ_VERSION",
    "S_CANCELLED",
    "S_COMPLETED",
    "S_FAILED",
    "S_REFUSED",
    "SyntheticAcquisitionAuthority",
    "acquire",
    "acquisition_digest",
    "preflight_archive",
    "resolve_assets",
    "resume_identity",
    "validate_resume",
]
