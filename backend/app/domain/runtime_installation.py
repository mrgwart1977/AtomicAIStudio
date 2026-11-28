"""U-5d-0b Phase beta-3-B3.3 — rgen:v0 isolated versioned synthetic
installation generations and failure-safe publication foundation.

Scope (authorized boundary): a completed racq:v0 acquisition result may
become a *versioned synthetic installation generation* inside a
caller-supplied isolated installation root. This module publishes
synthetic generations only — it never selects, enrolls, qualifies or
executes a generation, and it never touches the legacy `runtimes/`
namespace or the `runtime_installs` table.

rgen:v0 contract semantics:

- The canonical generation identity is a deterministic content-derived
  digest over the generation descriptor: {contract, reference_digest,
  runtime_key, flavor, components, assets, members} canonicalized as
  sorted-key compact JSON. `generation_id` is `gen-<64 lowercase hex>`
  — the FULL sha256 of that canonical serialization. `gen-` is
  namespacing OUTSIDE hashed material (same convention as racq:v0:).
  Content-derived identity is NOT claimed mathematically collision-free
  — existing destination content is always independently revalidated.
- The acquisition digest (`racq:v0:<sha256>`) is PROVENANCE, not
  canonical identity: identical authorized generation content must
  produce stable identity across repeated acquisition instances, so
  provenance is bound in the descriptor evidence and the publication
  record but excluded from the hashed descriptor.
- Mutable operational fields (paths, attempt ids, timestamps, lock
  state, publication/cancellation state, refusal reasons, recovery
  observations) never participate in canonical identity.

Filesystem layout (all under the supplied install root):

    <install_root>/
      .gens.lock                         # OS byte-range lock file;
                                         # presence != ownership
      runtime-gens/
        <runtime_key>/
          <flavor>/
            .staging/<attempt-nonce>/
              members/<flat names>
              _staging.json              # owned-attempt sidecar
            .generations/
              gen-<64hex>/
                members/<flat names>
                _generation.json         # immutable descriptor evidence
              gen-<64hex>.published.json  # per-generation commit record

Publication state machine:

    PREPARING -> STAGED -> VERIFYING -> PUBLISHING -> PUBLISHED

  with REFUSED / FAILED / CANCELLED reachable from every non-terminal
  state, and UNCERTAIN for a physically committed generation whose
  commit evidence could not be durably recorded.

  COMMIT POINT: the single `os.rename` of the owned staging directory
  to `.generations/gen-<id>` is the physical namespace commit — the
  generation content is complete BEFORE the rename (members verified,
  `_generation.json` written), so no partially-populated generation is
  ever exposed under its canonical name. The sibling
  `<gen>.published.json` is post-commit durability evidence. If the
  rename succeeds but the record cannot be written the result is
  `uncertain` + `committed_unconfirmed` — never a generic failure that
  conceals a committed publication, and never an unqualified success
  without commit evidence. Cancellation is not consulted after the
  rename: a late cancel can never misreport a committed publication.

Create-only publication semantics: on Windows, `os.rename`/`MoveFile`
refuses to replace an existing destination (FileExistsError) — the
rename is create-only. On POSIX, rename of an empty directory can
replace; the bounded guarantee rests on the pre-rename destination
assessment plus the component-scoped lock — the residual inter-process
gap is a documented limitation, and a foreign destination appearing in
the gap surfaces as a rename collision refusal on Windows or a
post-rename revalidation on POSIX-class platforms.

Persistence is filesystem-only (Option A): no database migration, no
RuntimeInstall rows, no registry extension, no durable-job
integration, no application-startup wiring. `reconcile()` is an
explicit, observational, non-destructive scan callable in synthetic
tests; it never adopts, repairs or deletes.

Locking model (smallest adequate):

- `<install_root>/.gens.lock` — OS-enforced byte-range lock held for
  process lifetime (Windows msvcrt.locking / POSIX flock, nonblocking).
  The lock FILE is just a carrier; ownership is the OS lock itself —
  the file may remain after process death (stale file, never a stale
  lock: the OS releases the byte range).
- Per-(runtime_key, flavor) in-process `threading.Lock`, nonblocking —
  a contested component refuses `component_locked` rather than queue.

Mandatory result declarations (every envelope — same five as racq:v0):

    production_trust: False
    execution_eligible: False
    operational_qualification: "unestablished"
    trusted_enrollment: "unestablished"
    installation_publication: "not_performed"   # PRODUCTION publication
                                                # only — synthetic
                                                # publication state is
                                                # reported separately
                                                # under
                                                # synthetic_publication

These limits bound the isolated contract only — not production. This
module does not claim resistance to arbitrary malicious in-process
Python, tamper-resistant publication, portable directory fsync, or
full crash durability under sudden power loss.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import threading
from pathlib import Path

from app.domain import runtime_integrity as ri
from app.domain import runtime_reference as rr

try:
    import msvcrt
except ImportError:  # pragma: no cover — non-Windows fallback
    msvcrt = None

RGEN_VERSION = "rgen:v0"
CONTRACT = RGEN_VERSION

S_PUBLISHED = "published"
S_REFUSED = "refused"
S_FAILED = "failed"
S_CANCELLED = "cancelled"
S_UNCERTAIN = "uncertain"
S_RECONCILED = "reconciled"
S_REVALIDATED = "revalidated"

# Internal FSM states recorded in _staging.json (never trusted as sole
# ownership evidence — the nonce binding below is authoritative).
_F_PREPARING = "preparing"
_F_STAGED = "staged"
_F_VERIFYING = "verifying"
_F_PUBLISHING = "publishing"
_F_PUBLISHED = "published"

# Synthetic publication vocabulary — intentionally separate from the
# preserved production declaration installation_publication.
PUB_NOT_PERFORMED = "not_performed"
PUB_COMMITTED = "committed"
PUB_UNCONFIRMED = "committed_unconfirmed"
PUB_ALREADY = "already_committed"

# Contract-level constants of the consumed acquisition contract —
# validated by literal, never by importing its implementation.
_RACQ_CONTRACT = "racq:v0"
_SYNTHETIC_AUTHORITY_CLASS = "synthetic_acquisition"

_NON_EXECUTABLE = {
    "production_trust": False,
    "execution_eligible": False,
    "operational_qualification": "unestablished",
    "trusted_enrollment": "unestablished",
    # Refers to PRODUCTION installation publication only. Synthetic
    # publication state is reported under synthetic_publication /
    # publication.state — never folded into this declaration.
    "installation_publication": "not_performed",
    "authority_class": _SYNTHETIC_AUTHORITY_CLASS,
}

# Required declarations on a consumable racq:v0 envelope — an altered
# declaration is a forged result and refuses before any write.
_REQUIRED_ACQ_DECLS = {
    "production_trust": False,
    "execution_eligible": False,
    "operational_qualification": "unestablished",
    "trusted_enrollment": "unestablished",
    "installation_publication": "not_performed",
    "authority_class": _SYNTHETIC_AUTHORITY_CLASS,
}

# Bounds (isolated contract only — same rationale as racq:v0).
_MAX_COMPONENTS = 16
_MAX_MEMBERS = 2048
_MAX_MEMBER_NAME = 240
_MAX_SEGMENT = 128
_MAX_DECLARED_BYTES = 8 * 1024 ** 3     # aggregate declared member bytes
_MAX_OBSERVED_BYTES = 8 * 1024 ** 3     # aggregate observed member bytes
_MAX_METADATA_BYTES = 64 * 1024         # per metadata file (.json sidecars)
_MAX_RECON_ENTRIES = 4096               # reconciliation scan bound
_MAX_REFUSALS = 64
_MAX_REFUSAL_LEN = 160
_IO_CHUNK = 1024 * 1024                 # bounded streaming

_LOCK_FILE = ".gens.lock"
_GENS_DIR = "runtime-gens"
_STAGING_DIR = ".staging"
_GENERATIONS_DIR = ".generations"
_STAGING_META = "_staging.json"
_GENERATION_META = "_generation.json"
_PUB_SUFFIX = ".published.json"

_SEG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# Private inert test seam — never invoked unless a test installs a
# callable; fixed points only (mirrors the racq:v0 precedent):
#   staging_created, member_written, pre_revalidate, pre_rename,
#   post_rename, pre_record, pre_record_link, pre_record_tmp_unlink,
#   post_record_link, post_record
_TEST_SEAM = None
_TEST_SEAM_POINTS = frozenset(
    {"staging_created", "member_written", "pre_revalidate",
     "pre_rename", "post_rename", "pre_record", "pre_record_link",
     "pre_record_tmp_unlink", "post_record_link", "post_record"})

# Inert platform-policy test seam — None means the real os.name is
# consulted. Never a simulated foreign platform execution claim: it
# only exercises the fail-closed policy branch.
_TEST_PLATFORM = None

_ROOT_LOCKS: dict[str, object] = {}
_ROOT_LOCKS_GUARD = threading.Lock()
_COMP_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_COMP_LOCKS_GUARD = threading.Lock()


# ---------------------------------------------------------------- envelope


def _env(result: dict) -> dict:
    """Every result is an rgen:v0 envelope — structurally
    non-executable, never a trust, enrollment, selection or
    qualification claim."""
    return {**_NON_EXECUTABLE, "contract": RGEN_VERSION, **result}


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _is_sha256(s) -> bool:
    return isinstance(s, str) and _HEX64_RE.match(s) is not None


def _is_str(s, n=_MAX_SEGMENT) -> bool:
    return isinstance(s, str) and 1 <= len(s) <= n


def _is_size(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _refusals(refusals: list, *codes) -> list:
    for c in codes:
        if len(refusals) < _MAX_REFUSALS:
            refusals.append(c[:_MAX_REFUSAL_LEN])
    return refusals


def _seam(point, **kw):
    if _TEST_SEAM is not None:
        _TEST_SEAM(point, **kw)


def _check_cancel(cancel_check):
    """Cancellation checkpoints: consulted before each member write,
    after staging, after revalidation and before the rename. NOT
    consulted after the namespace commit — a late cancel can never
    misreport a committed publication."""
    if cancel_check is not None and cancel_check():
        raise _Cancelled()


class _Cancelled(Exception):
    pass


# ---------------------------------------------------------- stage envelopes


def _pub_env(stage: dict, state: str, refusals: list | None = None) -> dict:
    pub = stage.get("publication") or {"state": PUB_NOT_PERFORMED}
    out = {"ok": state in (S_PUBLISHED, S_RECONCILED, S_REVALIDATED),
           "state": state,
           "refusals": refusals if refusals is not None else
           stage.get("refusals", []),
           "generation": stage.get("generation"),
           "reference": stage.get("reference"),
           "acquisition": stage.get("acquisition"),
           "components": stage.get("components"),
           "members": stage.get("members", []),
           "staging": stage.get("staging"),
           "publication": pub,
           "synthetic_publication": pub.get("state",
                                            PUB_NOT_PERFORMED),
           "idempotent": stage.get("idempotent", False),
           "failure": stage.get("failure"),
           "uncertain_cause": stage.get("uncertain_cause"),
           "reconciliation_required": state == S_UNCERTAIN,
           "fsm": stage.get("fsm")}
    return _env(out)


def _stage_refusal(stage: dict, *codes) -> dict:
    _refusals(stage.setdefault("refusals", []), *codes)
    return _pub_env(stage, S_REFUSED)


def _stage_failed(stage: dict, cls: str) -> dict:
    """Failure outcome: refusal evidence is always plain code
    strings (H2 — never mixed shapes); the exception classification
    is reported separately under "failure"."""
    _refusals(stage.setdefault("refusals", []), f"failure:{cls}")
    stage["failure"] = {"class": "io", "code": cls}
    return _pub_env(stage, S_FAILED)


# ------------------------------------------------------- canonical identity


def generation_digest(descriptor: dict) -> str:
    """rgen:v0:<full sha256> of the canonical descriptor. The prefix is
    namespacing outside hashed material."""
    return "rgen:v0:" + hashlib.sha256(
        _canon(descriptor).encode()).hexdigest()


def generation_id(descriptor: dict) -> str:
    """gen-<full sha256> — identical authorized content yields stable
    identity across repeated attempts. NOT claimed collision-free;
    destinations are always independently revalidated."""
    return "gen-" + hashlib.sha256(
        _canon(descriptor).encode()).hexdigest()


_DESCRIPTOR_FIELDS = {"contract", "reference_digest", "runtime_key",
                     "flavor", "components", "assets", "members"}
_DESC_ASSET_FIELDS = {"component", "asset_id", "name", "sha256"}
_DESC_MEMBER_FIELDS = {"name", "component", "size", "sha256"}


def _norm_descriptor(desc) -> tuple[dict | None, list]:
    """Strict schema validation of a candidate canonical descriptor —
    sorted member ordering enforced, duplicates and extra identity
    fields refused."""
    refusals = []
    if not isinstance(desc, dict):
        return None, ["descriptor_not_mapping"]
    if set(desc) != _DESCRIPTOR_FIELDS:
        return None, ["descriptor_fields_invalid"]
    if desc["contract"] != RGEN_VERSION:
        return None, ["descriptor_contract_invalid"]
    rd = desc["reference_digest"]
    if not _is_str(rd, 96) or not rd.startswith("rref:v0:") \
            or not _is_sha256(rd[8:]):
        refusals.append("descriptor_reference_digest_invalid")
    if not _is_str(desc["runtime_key"]) \
            or not _is_str(desc["flavor"]):
        refusals.append("descriptor_component_key_invalid")
    comps = desc["components"]
    if not isinstance(comps, dict) \
            or set(comps) != {"selected", "omitted"} \
            or not isinstance(comps["selected"], list) \
            or not isinstance(comps["omitted"], list) \
            or not comps["selected"] \
            or len(comps["selected"]) > _MAX_COMPONENTS \
            or any(not _is_str(c) for c in comps["selected"]) \
            or any(not _is_str(c) for c in comps["omitted"]) \
            or comps["selected"] != sorted(comps["selected"]) \
            or comps["omitted"] != sorted(comps["omitted"]) \
            or set(comps["selected"]) & set(comps["omitted"]):
        refusals.append("descriptor_components_invalid")
    assets = desc["assets"]
    if not isinstance(assets, list) or not assets \
            or len(assets) > _MAX_COMPONENTS:
        refusals.append("descriptor_assets_invalid")
    else:
        for a in assets:
            if not isinstance(a, dict) or set(a) != _DESC_ASSET_FIELDS \
                    or not _is_str(a["component"]) \
                    or not isinstance(a["asset_id"], int) \
                    or isinstance(a["asset_id"], bool) \
                    or a["asset_id"] <= 0 \
                    or not _is_str(a["name"], 512) \
                    or not _is_sha256(a["sha256"]):
                refusals.append("descriptor_assets_invalid")
                break
        else:
            if [a["component"] for a in assets] \
                    != sorted(a["component"] for a in assets) \
                    or len({a["component"] for a in assets}) \
                    != len(assets):
                refusals.append("descriptor_assets_order_invalid")
    members = desc["members"]
    if not isinstance(members, list) or not members \
            or len(members) > _MAX_MEMBERS:
        refusals.append("descriptor_members_invalid")
    else:
        names = []
        for m in members:
            if not isinstance(m, dict) or set(m) != _DESC_MEMBER_FIELDS \
                    or not isinstance(m["name"], str) \
                    or len(m["name"]) > _MAX_MEMBER_NAME \
                    or not _is_str(m["component"]) \
                    or not _is_size(m["size"]) \
                    or not _is_sha256(m["sha256"]):
                refusals.append("descriptor_members_invalid")
                break
            names.append(m["name"])
        else:
            if names != sorted(names) \
                    or len(set(names)) != len(names):
                refusals.append("descriptor_members_order_invalid")
    if refusals:
        return None, refusals
    return dict(desc), []


# -------------------------------------------------------- path and reparse


def _safe_segment(s) -> bool:
    """Namespace path segment: strict lowercase charset — case-fold
    aliasing between platform-sensitive names is refused outright
    rather than relying on filesystem behavior."""
    return isinstance(s, str) and _SEG_RE.match(s) is not None \
        and not s.startswith(".") and not s.endswith(".") \
        and ".." not in s and len(s) <= _MAX_SEGMENT


def _reparse(path: Path) -> bool:
    """Symlink, junction or any reparse point where the platform can
    report it — mirrors the racq:v0 helper (kept local for module
    self-containment)."""
    try:
        if path.is_symlink():
            return True
        isjunction = getattr(os.path, "isjunction", None)
        if isjunction is not None and isjunction(path):
            return True
        attrs = getattr(path.lstat(), "st_file_attributes", None)
        return bool(attrs is not None
                    and attrs & _FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return False


def _chain_reparse(path: Path, anchor: Path) -> bool:
    """True when `path` or any ancestor up to and including `anchor`
    reports a reparse point."""
    path = Path(path)
    anchor = Path(anchor)
    cur = path
    while True:
        if _reparse(cur):
            return True
        if cur == anchor or cur.parent == cur:
            return False
        try:
            cur.relative_to(anchor)
        except ValueError:
            return False
        cur = cur.parent


def _fsync_dir(path: Path) -> None:
    """Best-effort directory fsync — POSIX only; Windows refuses
    directory handles (NTFS journal + file fsync is the documented
    bound). Never claimed as full crash durability."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _fsync_file(fh) -> None:
    fh.flush()
    os.fsync(fh.fileno())


# ---------------------------------------------------------- metadata files


def _read_meta(path: Path):
    """Bounded metadata read: size ceiling before parse, duplicate-key
    rejection — returns (dict, None) or (None, code)."""
    try:
        st = path.lstat()
    except OSError:
        return None, "metadata_missing"
    if _reparse(path) or not path.is_file():
        return None, "metadata_not_file"
    if st.st_size > _MAX_METADATA_BYTES or st.st_size == 0:
        return None, "metadata_over_bound"
    try:
        raw = path.read_bytes()
    except OSError:
        return None, "metadata_unreadable"
    dup = []

    def _pairs(pairs):
        seen = set()
        for k, v in pairs:
            if k in seen:
                dup.append(k)
            seen.add(k)
        return dict(pairs)

    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, ValueError):
        return None, "metadata_malformed"
    if dup:
        return None, "metadata_duplicate_field"
    if not isinstance(obj, dict):
        return None, "metadata_not_mapping"
    if obj.get("contract") != RGEN_VERSION:
        return None, "metadata_contract_invalid"
    return obj, None


def _write_meta(path: Path, obj: dict):
    """Atomic bounded metadata write: tmp sibling + os.replace + fsync
    + best-effort dir fsync. Raises OSError on failure."""
    raw = _canon(obj).encode()
    if len(raw) > _MAX_METADATA_BYTES:
        raise ValueError("metadata_over_bound")
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "xb") as fh:
        fh.write(raw)
        _fsync_file(fh)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


# ------------------------------------------------------------------ locks


def _acquire_root(root: Path):
    """Process-lifetime OS byte-range ownership of the installation
    root. Returns the held handle or None when another process owns
    it. The lock FILE persisting after process death is a stale file,
    never a stale lock — the OS releases the byte range."""
    key = str(root.resolve())
    with _ROOT_LOCKS_GUARD:
        if key in _ROOT_LOCKS:
            return _ROOT_LOCKS[key]["fh"]
        lock_path = root / _LOCK_FILE
        if lock_path.exists() and (_reparse(lock_path)
                                   or not lock_path.is_file()):
            return None
        fh = open(lock_path, "a+b")  # noqa: SIM115 — held open for
        # process lifetime; OS releases the byte range on death
        try:
            fh.seek(0)
            if msvcrt is not None:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(),
                            fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return None
        _ROOT_LOCKS[key] = {"fh": fh, "active": set()}
        return fh


def _root_active_begin(root):
    """Register an active operation against held root ownership —
    returns a per-operation lease token, or None when no ownership is
    held. Synchronized under the root-lock guard so acquisition and
    registration cannot interleave with a release."""
    key = str(Path(root).resolve())
    with _ROOT_LOCKS_GUARD:
        ent = _ROOT_LOCKS.get(key)
        if ent is None:
            return None
        tok = secrets.token_hex(6)
        ent["active"].add(tok)
        return tok


def _root_active_end(root, token) -> None:
    """Release exactly this operation's lease — deterministic on
    success, refusal, failure and cancellation paths."""
    key = str(Path(root).resolve())
    with _ROOT_LOCKS_GUARD:
        ent = _ROOT_LOCKS.get(key)
        if ent is not None:
            ent["active"].discard(token)


def release_root(root) -> bool:
    """Deterministic release of held installation-root ownership —
    refuses while an active operation depends on it. Returns True
    only when the OS lock was actually released."""
    key = str(Path(root).resolve())
    with _ROOT_LOCKS_GUARD:
        ent = _ROOT_LOCKS.get(key)
        if ent is None or ent["active"]:
            return False
        fh = ent["fh"]
        del _ROOT_LOCKS[key]
    try:
        fh.close()
    except OSError:
        pass
    return True


def _component_lock(runtime_key: str, flavor: str):
    """Nonblocking per-component in-process serialization — refusal,
    never queueing."""
    ck = (runtime_key.casefold(), flavor.casefold())
    with _COMP_LOCKS_GUARD:
        lock = _COMP_LOCKS.setdefault(ck, threading.Lock())
    return lock


def reset_locks() -> None:
    """Synthetic-test reset: release only INACTIVE root ownership and
    component locks that are not held — an active operation's
    ownership is never invalidated by a reset."""
    with _ROOT_LOCKS_GUARD:
        held = [_ROOT_LOCKS.pop(k)["fh"] for k in list(_ROOT_LOCKS)
                if not _ROOT_LOCKS[k]["active"]]
    for fh in held:
        try:
            fh.close()
        except OSError:
            pass
    with _COMP_LOCKS_GUARD:
        for ck in [k for k, lk in _COMP_LOCKS.items()
                   if not lk.locked()]:
            del _COMP_LOCKS[ck]


# --------------------------------------------------- platform policy (C1)


def _publication_supported() -> bool:
    """Windows-first publication policy: NEW synthetic generation
    publication is supported only where directory rename is
    established create-only — verified Windows semantics
    (MoveFileW without MOVEFILE_REPLACE_EXISTING fails on any
    existing destination). POSIX rename(2) silently replaces an
    existing EMPTY directory, so pre-check + rename cannot establish
    no-replacement there and publication fails closed. Read-only
    revalidate/reconcile stay available on every platform."""
    plat = _TEST_PLATFORM if _TEST_PLATFORM is not None else os.name
    return plat == "nt"


# ------------------------------------------------- acquisition validation


def _authority_query(authority) -> bool:
    """Established synthetic authority-model shape: the supplied
    context exposes the known-revocation snapshot query
    is_revoked(digest) -> bool. Structural binding only — never a
    production trust root, credential or signature check."""
    return authority is not None and callable(
        getattr(authority, "is_revoked", None))


def _acq_canonical_digest(ref_digest, key, flavor, selected, omitted,
                          ref_comps):
    """Independent recomputation of the racq:v0 canonical acquisition
    identity — same accepted algorithm (sorted-key compact JSON over
    {contract, reference_digest, runtime_key, flavor, selected,
    omitted, assets}) reimplemented literally here; the protected
    implementation is never imported (a87). Every input derives from
    the already-validated reference and selection partition — asset
    identity fields come from ref["archives"][c]["asset"], never from
    caller-supplied acquisition records."""
    assets = {}
    for c in sorted(selected):
        a = ref_comps[c].get("asset") or {}
        dig = a.get("digest") or {}
        if a.get("id") is None or a.get("name") is None \
                or a.get("size") is None or a.get("url") is None \
                or dig.get("value") is None:
            return None, f"acquisition_identity_uncomputable:{c}"
        assets[c] = {"asset_id": a["id"], "name": a["name"],
                     "size": a["size"], "sha256": dig["value"],
                     "source": a["url"]}
    identity = {"contract": _RACQ_CONTRACT,
                "reference_digest": ref_digest,
                "runtime_key": key, "flavor": flavor,
                "selected": sorted(selected),
                "omitted": sorted(omitted), "assets": assets}
    return (_RACQ_CONTRACT + ":" + hashlib.sha256(
        _canon(identity).encode()).hexdigest(), None)


def _validate_acquisition(acq, ref, authority, stage: dict):
    """I1 — independent validation of a candidate racq:v0 result and
    the supplied canonical reference. Returns the expected member
    inventory + descriptor evidence, or a refusal list."""
    refusals = []
    if not isinstance(acq, dict):
        return None, ["acquisition_not_mapping"]
    if acq.get("contract") != _RACQ_CONTRACT:
        return None, ["acquisition_contract_invalid"]
    if acq.get("ok") is not True or acq.get("state") != "completed":
        refusals.append("acquisition_not_completed")
    if acq.get("refusals"):
        refusals.append("acquisition_refusals_present")
    for k, v in _REQUIRED_ACQ_DECLS.items():
        if acq.get(k) != v:
            refusals.append(f"acquisition_declaration_altered:{k}")
    acq_id = acq.get("acquisition")
    if not _is_str(acq_id, 80) or not acq_id.startswith("racq:v0:") \
            or not _is_sha256(acq_id[8:]):
        refusals.append("acquisition_identity_invalid")

    vres = rr.validate_reference(ref)
    if not vres["ok"]:
        _refusals(refusals, "reference_invalid",
                  *[f"reference_{r[:60]}" for r in vres["refusals"][:4]])
        return None, refusals
    reference = vres["reference"]
    ref_digest = rr.reference_digest(ref)
    key, flavor = reference["runtime_key"], reference["flavor"]
    acq_ref = acq.get("reference")
    if not isinstance(acq_ref, dict):
        refusals.append("acquisition_reference_invalid")
    else:
        if acq_ref.get("digest") != ref_digest:
            refusals.append("acquisition_reference_digest_mismatch")
        if acq_ref.get("runtime_key") != key \
                or acq_ref.get("flavor") != flavor:
            refusals.append("acquisition_component_key_mismatch")
        if acq_ref.get("validation_ok") is not True:
            refusals.append("acquisition_reference_unvalidated")

    if not _authority_query(authority):
        refusals.append("authority_context_invalid")
    elif authority.is_revoked(ref_digest):
        refusals.append("authority_revoked")
    acq_auth = acq.get("authority")
    if not isinstance(acq_auth, dict) \
            or acq_auth.get("association") != "established" \
            or acq_auth.get("class") != _SYNTHETIC_AUTHORITY_CLASS:
        refusals.append("acquisition_authority_not_established")

    ref_comps = {a["component"]: a for a in reference["archives"]}
    comps = acq.get("components")
    if not isinstance(comps, dict) \
            or set(comps) < {"selected", "omitted"} \
            or not isinstance(comps.get("selected"), list) \
            or not isinstance(comps.get("omitted"), list):
        refusals.append("acquisition_components_invalid")
        return None, refusals
    selected, omitted = comps["selected"], comps["omitted"]
    if set(selected) | set(omitted) != set(ref_comps) \
            or set(selected) & set(omitted):
        refusals.append("acquisition_selection_inconsistent")
    required = {c for c, a in ref_comps.items() if a["required"]}
    if not required <= set(selected):
        refusals.append("acquisition_required_omitted")

    # ---- independent canonical acquisition identity (C3-A): digest
    # syntax alone is insufficient — the supplied digest must equal
    # the recomputed canonical identity over validated inputs.
    cdigest, derr = _acq_canonical_digest(ref_digest, key, flavor,
                                          selected, omitted,
                                          ref_comps)
    if derr is not None:
        refusals.append(derr)
    elif cdigest != acq_id:
        refusals.append("acquisition_identity_mismatch")

    archives = acq.get("archives")
    if not isinstance(archives, list):
        refusals.append("acquisition_archives_invalid")
        archives = []
    assets = {}
    by_comp = {}
    for arec in archives:
        if not isinstance(arec, dict):
            refusals.append("acquisition_archive_record_invalid")
            continue
        c = arec.get("component")
        by_comp[c] = by_comp.get(c, 0) + 1
        if c not in ref_comps:
            refusals.append(f"acquisition_archive_unknown:{str(c)[:40]}")
            continue
        if c in selected:
            # Reference-derived asset association (C3-B): internally
            # self-consistent evidence is never accepted when its
            # declared asset identity contradicts the validated
            # reference. Optional source is bound when represented.
            ra_asset = ref_comps[c].get("asset") or {}
            ra_dig = ra_asset.get("digest") or {}
            if arec.get("asset_id") != ra_asset.get("id") \
                    or arec.get("name") != ra_asset.get("name") \
                    or arec.get("expected_size") != ra_asset.get("size") \
                    or arec.get("expected_sha256") != ra_dig.get("value") \
                    or (arec.get("source") is not None
                        and arec.get("source") != ra_asset.get("url")):
                refusals.append(
                    f"acquisition_asset_association_invalid:{c}")
                continue
            # Layered association evidence: the streamed digest must
            # match; reopened-handle and final checks may legitimately
            # report memory_backed for the in-memory path.
            ok = (arec.get("verified") is True
                  and arec.get("comparison") == "match"
                  and arec.get("reopen_comparison")
                  in ("match", "memory_backed")
                  and arec.get("final_comparison")
                  in ("match", "memory_backed")
                  and _is_size(arec.get("expected_size"))
                  and arec.get("expected_size")
                  == arec.get("observed_size")
                  and _is_sha256(arec.get("expected_sha256"))
                  and arec.get("expected_sha256")
                  == arec.get("observed_sha256"))
            if not ok:
                refusals.append(f"acquisition_archive_unverified:{c}")
                continue
            assets[c] = {"component": c,
                         "asset_id": arec.get("asset_id"),
                         "name": arec.get("name"),
                         "sha256": arec["observed_sha256"]}
    for c in selected:
        if by_comp.get(c, 0) != 1:
            refusals.append(f"acquisition_archive_missing:{c}")
    for c in set(by_comp) - set(selected) - set(omitted):
        refusals.append(f"acquisition_archive_unselected:{c}")
    for c in selected:
        if c not in assets and by_comp.get(c, 0) == 1:
            pass  # already refused as unverified
        elif c in assets:
            a = assets[c]
            if not isinstance(a["asset_id"], int) \
                    or isinstance(a["asset_id"], bool) \
                    or a["asset_id"] <= 0 or not _is_str(a["name"], 512):
                refusals.append(f"acquisition_asset_identity_invalid:{c}")

    members = acq.get("members")
    if not isinstance(members, list):
        refusals.append("acquisition_members_invalid")
        members = []
    skipped = acq.get("skipped")
    if not isinstance(skipped, list):
        refusals.append("acquisition_skipped_invalid")
        skipped = []
    coverage = acq.get("coverage")
    if not isinstance(coverage, dict) \
            or coverage.get("declared_complete") is not True \
            or coverage.get("selected_complete") is not True:
        refusals.append("acquisition_coverage_incomplete")

    staging = acq.get("staging")
    if not isinstance(staging, dict) \
            or not _is_str(staging.get("dir"), 256) \
            or staging.get("complete") is not True:
        refusals.append("acquisition_staging_incomplete")
        staging = None

    # Expected managed-member inventory derives from the VALIDATED
    # reference (never solely from caller-supplied observed hashes).
    expected: dict[str, dict] = {}
    sel_set = set(selected)
    for m in reference["members"]:
        if m["component"] not in sel_set:
            continue
        cf = m["name"].casefold()
        if cf in expected:
            refusals.append(f"expected_member_duplicate:{m['name']}")
            continue
        expected[cf] = m
    declared = sum(m["size"] for m in expected.values())
    if len(expected) > _MAX_MEMBERS or declared > _MAX_DECLARED_BYTES:
        refusals.append("expected_members_over_bound")

    seen = set()
    for rec in members:
        if not isinstance(rec, dict):
            refusals.append("acquisition_member_record_invalid")
            continue
        name = rec.get("name")
        if not isinstance(name, str) \
                or not ri.valid_member_name(name) \
                or len(name) > _MAX_MEMBER_NAME:
            refusals.append(f"acquisition_member_name_invalid:{str(name)[:40]}")
            continue
        cf = name.casefold()
        if cf in seen:
            refusals.append(f"acquisition_member_duplicate:{name}")
            continue
        seen.add(cf)
        exp = expected.get(cf)
        if exp is None:
            refusals.append(f"acquisition_member_unexpected:{name}")
            continue
        if rec.get("component") != exp["component"] \
                or rec.get("state") != "match" \
                or rec.get("expected_size") != exp["size"] \
                or rec.get("observed_size") != exp["size"] \
                or rec.get("expected_sha256") != exp["sha256"] \
                or rec.get("observed_sha256") != exp["sha256"]:
            refusals.append(f"acquisition_member_inconsistent:{name}")
    missing = set(expected) - seen
    if missing:
        refusals.append(
            f"acquisition_member_missing:{expected[min(missing)]['name']}")

    skip_names = {n.casefold()
                  for n in reference["coverage"].get("skip_names", [])}
    for s in skipped:
        if not isinstance(s, dict) or not isinstance(s.get("name"), str) \
                or s.get("state") != "skipped_declared" \
                or s.get("name", "").casefold() not in skip_names \
                or s.get("name", "").casefold() in expected:
            refusals.append(
                f"acquisition_skipped_invalid:{str(s)[:60]}")
            break
    if refusals:
        return None, refusals
    return {"reference_digest": ref_digest, "runtime_key": key,
            "flavor": flavor, "selected": sorted(selected),
            "omitted": sorted(omitted), "assets": assets,
            "expected": expected, "acq_id": acq_id,
            "staging_dir": staging["dir"]}, []


# ---------------------------------------------------------- staging/model


def _source_member(root: Path, staging_dir: str, name: str) -> Path:
    return Path(root) / staging_dir / "members" / name


def _verify_file(path: Path, size: int, sha256: str,
                 cancel_check) -> str | None:
    """Streamed verification of a file against expected size+sha256 —
    bounded chunks, never whole-file buffering."""
    try:
        st = path.lstat()
    except OSError:
        return "member_source_missing"
    if _reparse(path) or not path.is_file():
        return "member_source_not_file"
    if st.st_size != size:
        return "member_size_mismatch"
    sha = hashlib.sha256()
    have = 0
    try:
        with open(path, "rb") as fh:
            while True:
                _check_cancel(cancel_check)
                chunk = fh.read(_IO_CHUNK)
                if not chunk:
                    break
                have += len(chunk)
                if have > _MAX_OBSERVED_BYTES:
                    return "member_over_bound"
                sha.update(chunk)
    except OSError:
        return "member_unreadable"
    if have != size:
        return "member_size_mismatch"
    if sha.hexdigest() != sha256:
        return "member_sha256_mismatch"
    return None


def _materialize(src: Path, dst: Path, size: int, sha256: str,
                 cancel_check, stack) -> str | None:
    """Installer-owned member write: exclusive create, streamed copy,
    hash-as-you-copy against the reference-derived expectation."""
    try:
        src.lstat()
    except OSError:
        return "member_source_missing"
    if _reparse(src) or not src.is_file():
        return "member_source_not_file"
    try:
        sfh = open(src, "rb")  # noqa: SIM115 — ExitStack-owned below
    except OSError:
        return "member_source_unreadable"
    stack.enter_context(sfh)
    try:
        dfh = open(dst, "xb")  # noqa: SIM115 — ExitStack-owned below
    except FileExistsError:
        return "member_destination_exists"
    except OSError:
        return "member_destination_unwritable"
    stack.enter_context(dfh)
    sha = hashlib.sha256()
    have = 0
    while True:
        _check_cancel(cancel_check)
        chunk = sfh.read(_IO_CHUNK)
        if not chunk:
            break
        have += len(chunk)
        if have > _MAX_OBSERVED_BYTES:
            return "member_over_bound"
        sha.update(chunk)
        dfh.write(chunk)
    _fsync_file(dfh)
    if have != size:
        return "member_size_mismatch"
    if sha.hexdigest() != sha256:
        return "member_sha256_mismatch"
    return None


def _verify_members_dir(mdir: Path, expected: dict, cancel_check,
                        errors: list | None = None) -> list:
    """Exact-set staged verification: staged members must equal the
    expected inventory — no extras, no missing, correct digests.
    Returns refusal codes (empty when clean)."""
    out = [] if errors is None else errors
    try:
        entries = sorted(mdir.iterdir(), key=lambda p: p.name)
    except OSError:
        out.append("members_dir_unreadable")
        return out
    if len(entries) > _MAX_MEMBERS:
        out.append("members_dir_over_bound")
        return out
    seen = set()
    for p in entries:
        name = p.name
        cf = name.casefold()
        if not ri.valid_member_name(name) \
                or len(name) > _MAX_MEMBER_NAME:
            out.append(f"member_name_invalid:{name}")
            continue
        if cf in seen:
            out.append(f"member_duplicate:{name}")
            continue
        seen.add(cf)
        exp = expected.get(cf)
        if exp is None:
            out.append(f"member_unexpected:{name}")
            continue
        why = _verify_file(p, exp["size"], exp["sha256"], cancel_check)
        if why is not None:
            out.append(f"{why}:{name}")
    missing = set(expected) - seen
    if missing:
        out.append(f"member_missing:{expected[min(missing)]['name']}")
    return out


# ------------------------------------------------------------- namespaces


def _namespaces(root: Path, key: str, flavor: str):
    comp = Path(root) / _GENS_DIR / key / flavor
    return (comp / _STAGING_DIR, comp / _GENERATIONS_DIR, comp)


def _ensure_dirs(root: Path, key: str, flavor: str):
    """Create the managed namespace; every level must be a real
    directory — never reparse, never a file. Foreign objects at
    managed names refuse."""
    staging, gens, comp = _namespaces(root, key, flavor)
    for d in (Path(root), Path(root) / _GENS_DIR,
              Path(root) / _GENS_DIR / key, comp, staging, gens):
        if d.exists():
            if _reparse(d) or not d.is_dir():
                return None, f"namespace_unsafe:{d.name}"
        else:
            try:
                d.mkdir()
            except OSError:
                return None, f"namespace_unwritable:{d.name}"
    if _chain_reparse(comp, Path(root)):
        return None, "namespace_reparse"
    return {"comp": comp, "staging": staging, "gens": gens}, None


def _record_owned_staging(sdir: Path, nonce: str, gen_id: str,
                          digest: str, fsm: str):
    _write_meta(sdir / _STAGING_META,
                {"contract": RGEN_VERSION, "record": "staging",
                 "attempt": nonce, "nonce": nonce,
                 "generation_id": gen_id,
                 "descriptor_digest": digest, "fsm": fsm})


def _staging_owned(sdir: Path, nonce: str) -> bool:
    meta, why = _read_meta(sdir / _STAGING_META)
    return why is None and meta.get("nonce") == nonce \
        and meta.get("record") == "staging"


# -------------------------------------------------- destination assessment


def _record_fields_ok(prec: dict, gen_id: str, digest: str) -> bool:
    """Exact field association for a generation publication record."""
    return isinstance(prec, dict) \
        and prec.get("record") == "publication" \
        and prec.get("generation_id") == gen_id \
        and prec.get("descriptor_digest") == digest \
        and prec.get("committed") is True


def _assess_record_slot(record: Path, gen_id: str, digest: str):
    """Independent publication-record slot assessment used when the
    generation directory itself is absent (C2-A). Any existing slot
    content is orphaned, foreign, contradictory or ambiguous evidence
    — refused, never repaired, replaced, adopted or deleted."""
    if _reparse(record):
        return "publication_record_reparse"
    if not record.exists():
        return None
    if not record.is_file():
        return "publication_record_not_file"
    prec, pwhy = _read_meta(record)
    if pwhy is not None:
        return f"publication_record_orphaned:{pwhy}"
    if not _record_fields_ok(prec, gen_id, digest):
        return "publication_record_foreign"
    # A valid record binding THIS generation identity with the
    # generation directory absent is torn or ambiguous evidence —
    # refused, never silently adopted.
    return "publication_record_orphaned"


def _commit_record(record: Path, prec: dict, gen_id: str,
                   digest: str, nonce: str) -> dict:
    """Create-only publication-record commit (C2-B): exclusive
    nonce-bound temporary file + os.link directory entry — create-only
    on both NTFS and POSIX (the established model_copy primitive);
    os.replace is never used for the final record. A collision
    preserves the existing evidence and reports uncertainty rather
    than attributing it. Returns {"state", "cause", "cleanup"} with
    state committed|collision|unconfirmed — never raises for the
    expected failure classes."""
    cleanup = {"tmp": None, "retained": False, "uncertain": None}
    raw = _canon(prec).encode()
    if len(raw) > _MAX_METADATA_BYTES:
        return {"state": "unconfirmed",
                "cause": "publication_record_over_bound",
                "cleanup": cleanup}
    tmp = record.with_name(f"{record.name}.{nonce}.tmp")
    cleanup["tmp"] = tmp.name
    try:
        with open(tmp, "xb") as fh:
            fh.write(raw)
            _fsync_file(fh)
    except FileExistsError:
        # Foreign object already holds our temporary slot — never
        # delete it.
        cleanup["retained"] = True
        cleanup["uncertain"] = "tmp_slot_foreign"
        return {"state": "unconfirmed",
                "cause": "publication_record_tmp_foreign",
                "cleanup": cleanup}
    except OSError:
        return {"state": "unconfirmed",
                "cause": "publication_record_tmp_failed",
                "cleanup": cleanup}
    try:
        tmp_stat = os.stat(tmp)
    except OSError:
        tmp_stat = None
    _seam("pre_record_link", path=str(tmp), record=str(record))
    outcome = None
    try:
        os.link(tmp, record)   # create-only; never replaces
    except FileExistsError:
        outcome = {"state": "collision",
                   "cause": "publication_record_collision"}
    except OSError:
        # Unsupported filesystem or IO failure — fail closed, no
        # os.replace fallback.
        outcome = {"state": "unconfirmed",
                   "cause": "publication_record_commit_failed"}
    # Deterministically release ONLY demonstrably owned temporary
    # resources — inode identity must match the file we created.
    if tmp_stat is not None:
        try:
            _seam("pre_record_tmp_unlink", path=str(tmp))
            cur = os.stat(tmp)
            if (cur.st_dev, cur.st_ino) == (tmp_stat.st_dev,
                                            tmp_stat.st_ino):
                os.unlink(tmp)
            else:
                cleanup["retained"] = True
                cleanup["uncertain"] = "tmp_replaced_foreign"
        except OSError:
            cleanup["retained"] = True
            cleanup["uncertain"] = "tmp_unlink_failed"
    else:
        cleanup["retained"] = True
        cleanup["uncertain"] = "tmp_identity_unverified"
    if outcome is not None:
        outcome["cleanup"] = cleanup
        return outcome
    _seam("post_record_link", path=str(record))
    # Independent post-commit association revalidation.
    got, why = _read_meta(record)
    if why is not None \
            or not _record_fields_ok(got, gen_id, digest):
        return {"state": "unconfirmed",
                "cause": "publication_record_association_invalid"
                         + (f":{why}" if why else ""),
                "cleanup": cleanup}
    return {"state": "committed", "cause": None,
            "cleanup": cleanup}


def _assess_destination(dest: Path, gen_id: str, digest: str,
                        expected: dict, record: Path,
                        cancel_check):
    """Pre-rename destination classification. Returns one of:
    ("absent", None)                      — publish proceeds
    ("identical", None)                   — valid committed generation
    (None, code)                          — refusal"""
    if not dest.exists() and not _reparse(dest):
        rcode = _assess_record_slot(record, gen_id, digest)
        if rcode is not None:
            return None, rcode
        return "absent", None
    if _reparse(dest):
        return None, "generation_destination_reparse"
    if not dest.is_dir():
        return None, "generation_destination_foreign"
    try:
        entries = list(dest.iterdir())
    except OSError:
        return None, "generation_destination_unreadable"
    if not entries:
        return None, "generation_destination_empty"
    meta, why = _read_meta(dest / _GENERATION_META)
    if why is not None:
        return None, f"generation_destination_incomplete:{why}"
    if meta.get("record") != "generation" \
            or meta.get("generation_id") != gen_id \
            or not _is_str(meta.get("descriptor_digest"), 96):
        return None, "generation_destination_foreign"
    if meta["descriptor_digest"] != digest:
        return None, "generation_collision"
    desc, dref = _norm_descriptor(meta.get("descriptor"))
    if dref or generation_digest(desc) != digest:
        return None, "generation_destination_malformed"
    mdir = dest / "members"
    if not mdir.is_dir() or _reparse(mdir):
        return None, "generation_destination_incomplete:members"
    vref = _verify_members_dir(mdir, expected, cancel_check)
    if vref:
        return None, f"generation_invalid:{vref[0]}"
    # An identical valid directory WITHOUT committed publication
    # evidence is never silently adopted — it records an interrupted
    # or foreign commit and requires reconciliation.
    prec, pwhy = _read_meta(record)
    if pwhy is not None:
        return None, f"generation_uncommitted:{pwhy}"
    if not _record_fields_ok(prec, gen_id, digest):
        return None, "publication_record_malformed"
    return "identical", None


# ---------------------------------------------------------------- install


def install(ref, acq, *, authority, source_root, install_root,
            cancel_check=None) -> dict:
    """Publish a completed racq:v0 acquisition as a versioned synthetic
    installation generation. Read-only w.r.t. the acquisition's own
    staging; every mutation stays inside the supplied install root."""
    stage: dict = {"fsm": _F_PREPARING, "refusals": [], "members": [],
                   "publication": {"state": PUB_NOT_PERFORMED,
                                   "commit_point": None,
                                   "record": None, "renamed": False,
                                   "fsynced": False}}
    try:
        # ---- I1: independent acquisition + reference validation
        ev, vref = _validate_acquisition(acq, ref, authority, stage)
        if vref:
            return _stage_refusal(stage, *vref)
        key, flavor = ev["runtime_key"], ev["flavor"]
        stage["reference"] = {"digest": ev["reference_digest"],
                              "runtime_key": key, "flavor": flavor}
        stage["acquisition"] = {"digest": ev["acq_id"],
                                "association": "provenance"}
        stage["components"] = {"selected": ev["selected"],
                               "omitted": ev["omitted"]}
        if not _safe_segment(key) or not _safe_segment(flavor):
            return _stage_refusal(stage, "namespace_segment_invalid")

        # ---- canonical generation identity (full digest)
        descriptor = {
            "contract": RGEN_VERSION,
            "reference_digest": ev["reference_digest"],
            "runtime_key": key, "flavor": flavor,
            "components": {"selected": ev["selected"],
                           "omitted": ev["omitted"]},
            "assets": [ev["assets"][c] for c in
                       sorted(ev["assets"])],
            "members": [{"name": m["name"], "component": m["component"],
                         "size": m["size"], "sha256": m["sha256"]}
                        for m in sorted(ev["expected"].values(),
                                        key=lambda m: m["name"])],
        }
        desc, dref = _norm_descriptor(descriptor)
        if dref:
            return _stage_refusal(stage, *dref)
        digest = generation_digest(desc)
        gen_id = generation_id(desc)
        stage["generation"] = {"id": gen_id, "digest": digest}
        expected = ev["expected"]

        # ---- persistability bound BEFORE any managed write (H3):
        # a descriptor that cannot be persisted within the metadata
        # limit refuses before the staging attempt is created.
        gen_meta = {"contract": RGEN_VERSION, "record": "generation",
                    "generation_id": gen_id,
                    "descriptor_digest": digest,
                    "descriptor": desc,
                    "provenance": {
                        "acquisition_digest": ev["acq_id"],
                        "authority": {
                            "association": "established",
                            "class": _SYNTHETIC_AUTHORITY_CLASS}},
                    "members": [m["name"] for m in desc["members"]]}
        if len(_canon(gen_meta).encode()) > _MAX_METADATA_BYTES:
            return _stage_refusal(stage,
                                  "generation_metadata_over_bound")

        # ---- source staging association (read-only consumption)
        sroot = Path(source_root)
        sdir_name = ev["staging_dir"]
        if not sroot.is_dir() or _reparse(sroot):
            return _stage_refusal(stage, "source_root_unsafe")
        src_members = sroot / sdir_name / "members"
        if _chain_reparse(src_members, sroot):
            return _stage_refusal(stage, "source_staging_reparse")
        if not src_members.is_dir():
            return _stage_refusal(stage, "source_staging_missing")

        # ---- supported-platform publication policy (C1): the
        # fail-closed check runs BEFORE any managed installation
        # write (root dir, lock file, namespace, staging).
        if not _publication_supported():
            return _stage_refusal(stage,
                                  "publication_platform_unsupported")

        # ---- install root + locks
        iroot = Path(install_root)
        if iroot.exists() and (_reparse(iroot) or not iroot.is_dir()):
            return _stage_refusal(stage, "install_root_unsafe")
        try:
            iroot.mkdir(parents=True, exist_ok=True)
        except OSError:
            return _stage_refusal(stage, "install_root_unwritable")
        if _acquire_root(iroot) is None:
            return _stage_refusal(stage, "install_root_locked")
        lease = _root_active_begin(iroot)
        if lease is None:
            return _stage_refusal(stage, "install_root_locked")
        lock = _component_lock(key, flavor)
        if not lock.acquire(blocking=False):
            _root_active_end(iroot, lease)
            return _stage_refusal(stage, "component_locked")
        try:
            return _install_locked(stage, ev, desc, digest, gen_id,
                                   expected, iroot, key, flavor,
                                   src_members, gen_meta, cancel_check)
        finally:
            lock.release()
            _root_active_end(iroot, lease)
    except _Cancelled:
        return _pub_env(stage, S_CANCELLED)
    except Exception as exc:  # noqa: BLE001 — a committed publication
        # is never concealed by a late post-commit error: anything
        # raised after the rename reports an uncertain commit, and
        # independently confirmed logical-commit evidence is never
        # downgraded by a reporting or cleanup failure.
        if stage["publication"].get("renamed"):
            pub = stage["publication"]
            pub["state"] = PUB_UNCONFIRMED
            pub["record"] = None
            pub["fsynced"] = False
            stage["fsm"] = _F_PUBLISHING
            stage["uncertain_cause"] = type(exc).__name__
            cinfo = stage.get("_commit")
            if cinfo is not None:
                got, _why = _read_meta(Path(cinfo["record"]))
                if _record_fields_ok(got or {}, cinfo["gen_id"],
                                     cinfo["digest"]):
                    pub["state"] = PUB_COMMITTED
                    pub["record"] = Path(cinfo["record"]).name
                    pub["fsynced"] = True
            return _pub_env(stage, S_UNCERTAIN)
        return _stage_failed(stage, type(exc).__name__)


def _install_locked(stage, ev, desc, digest, gen_id, expected, iroot,
                    key, flavor, src_members, gen_meta,
                    cancel_check) -> dict:
    ns, nerr = _ensure_dirs(iroot, key, flavor)
    if nerr:
        return _stage_refusal(stage, nerr)
    staging_root, gens_dir = ns["staging"], ns["gens"]

    # ---- exclusive owned attempt dir (never adopt existing objects)
    nonce = secrets.token_hex(8)
    sdir = None
    for _ in range(8):
        cand = staging_root / nonce
        try:
            cand.mkdir()
            sdir = cand
            break
        except FileExistsError:
            nonce = secrets.token_hex(8)
        except OSError:
            return _stage_failed(stage, "staging_unwritable")
    if sdir is None:
        return _stage_failed(stage, "staging_attempts_exhausted")
    if _chain_reparse(sdir, iroot):
        return _stage_refusal(stage, "staging_reparse")
    stage["staging"] = {"dir": sdir.name, "complete": False,
                        "owned": True}
    mdir = sdir / "members"
    try:
        mdir.mkdir()
    except OSError:
        return _stage_failed(stage, "staging_unwritable")

    try:
        _record_owned_staging(sdir, nonce, gen_id, digest, _F_PREPARING)
    except OSError:
        return _stage_failed(stage, "staging_metadata_unwritable")
    _seam("staging_created", attempt=nonce, path=str(sdir))

    # ---- I3: installer-owned materialization, exact expected set.
    # The ExitStack unwinds BEFORE the namespace rename — Windows
    # refuses to rename a directory containing open member handles.
    with contextlib.ExitStack() as stack:
        members_out = []
        for cf in sorted(expected):
            _check_cancel(cancel_check)
            m = expected[cf]
            name = m["name"]
            src = src_members / name
            dst = mdir / name
            why = _materialize(src, dst, m["size"], m["sha256"],
                               cancel_check, stack)
            members_out.append({"name": name, "component":
                                m["component"]})
            stage["members"] = members_out
            if why is not None:
                return _stage_refusal(stage, f"{why}:{name}")
            _seam("member_written", name=name, path=str(dst))
    stage["staging"]["complete"] = True
    stage["fsm"] = _F_STAGED
    _record_owned_staging(sdir, nonce, gen_id, digest, _F_STAGED)
    _check_cancel(cancel_check)

    # ---- verify: revalidate the staged set before publication
    stage["fsm"] = _F_VERIFYING
    _seam("pre_revalidate", path=str(sdir))
    vref = _verify_members_dir(mdir, expected, cancel_check)
    if vref:
        return _stage_refusal(stage, *vref)
    _check_cancel(cancel_check)

    # ---- immutable generation evidence inside the staged dir
    # (gen_meta built + bound-checked before staging — H3)
    try:
        _write_meta(sdir / _GENERATION_META, gen_meta)
    except (OSError, ValueError):
        return _stage_failed(stage, "generation_metadata_unwritable")

    # ---- I4: create-only same-volume namespace publication
    stage["fsm"] = _F_PUBLISHING
    dest = gens_dir / gen_id
    record = gens_dir / f"{gen_id}{_PUB_SUFFIX}"
    assess, acode = _assess_destination(
        dest, gen_id, digest, expected, record, cancel_check)
    if acode is not None:
        return _stage_refusal(stage, acode)
    if assess == "identical":
        stage["idempotent"] = True
        stage["publication"] = {"state": PUB_ALREADY,
                                "commit_point": None,
                                "record": record.name,
                                "renamed": False, "fsynced": True}
        stage["fsm"] = _F_PUBLISHED
        return _pub_env(stage, S_PUBLISHED)
    try:
        if os.stat(sdir).st_dev != os.stat(gens_dir).st_dev:
            return _stage_refusal(stage,
                                  "publication_cross_volume")
    except OSError:
        return _stage_failed(stage, "volume_assessment_failed")
    stage["publication"]["commit_point"] = "namespace_rename"
    stage["_commit"] = {"record": str(record), "gen_id": gen_id,
                        "digest": digest}
    _check_cancel(cancel_check)
    _seam("pre_rename", src=str(sdir), dst=str(dest))
    # Final pre-commit association recheck (C2-A): a record slot that
    # appeared since the initial assessment is foreign evidence —
    # refuse rather than commit over it.
    if _reparse(record) or record.exists():
        return _stage_refusal(stage, "publication_record_appeared")
    try:
        os.rename(sdir, dest)          # COMMIT POINT
    except FileExistsError:
        return _stage_refusal(
            stage, "generation_destination_appeared")
    except OSError as exc:
        if getattr(exc, "winerror", None) == 5:
            return _stage_failed(stage, "sharing_violation")
        return _stage_failed(stage, "publication_rename_failed")
    stage["publication"]["renamed"] = True
    _seam("post_rename", path=str(dest))
    # Post-commit: cancellation is NOT consulted — the generation
    # is committed; only accurate commit evidence remains.
    _fsync_dir(gens_dir)

    # ---- commit evidence: create-only per-generation record (C2-B)
    prec = {"contract": RGEN_VERSION, "record": "publication",
            "generation_id": gen_id,
            "descriptor_digest": digest,
            "reference_digest": ev["reference_digest"],
            "acquisition_digest": ev["acq_id"],
            "destination": f"{_GENS_DIR}/{key}/{flavor}/"
                           f"{_GENERATIONS_DIR}/{gen_id}",
            "commit": "namespace_rename",
            "committed": True,
            "member_count": len(desc["members"])}
    _seam("pre_record", path=str(record))
    rc = _commit_record(record, prec, gen_id, digest, nonce)
    if rc["state"] != "committed":
        # Physically committed, evidence unconfirmed — never a
        # generic failure concealing the commit, never a clean
        # success missing evidence. Furthest confirmed FSM progress
        # is publishing, NOT published (H1).
        stage["publication"] = {
            "state": PUB_UNCONFIRMED,
            "commit_point": "namespace_rename",
            "record": None, "renamed": True, "fsynced": False,
            "cause": rc["cause"], "cleanup": rc["cleanup"]}
        stage["fsm"] = _F_PUBLISHING
        return _pub_env(stage, S_UNCERTAIN)
    stage["publication"] = {"state": PUB_COMMITTED,
                            "commit_point": "namespace_rename",
                            "record": record.name,
                            "renamed": True, "fsynced": True,
                            "cleanup": rc["cleanup"]}
    _seam("post_record", path=str(record))
    stage["fsm"] = _F_PUBLISHED
    return _pub_env(stage, S_PUBLISHED)


# ------------------------------------------------------------ revalidation


def revalidate(install_root, runtime_key, flavor, gen_id, *,
               authority=None, cancel_check=None) -> dict:
    """Explicit generation revalidation — independent verification of
    descriptor + members. Establishes NO selection, enrollment or
    execution eligibility. Observational: it does not take the
    installation locks, so a concurrent in-flight install may yield
    transient mid-commit observations — classifications describe
    what was present at read time, never a stable snapshot."""
    stage: dict = {"fsm": _F_VERIFYING, "refusals": [], "members": [],
                   "publication": {"state": PUB_NOT_PERFORMED}}
    if not _is_str(runtime_key) or not _is_str(flavor) \
            or not _safe_segment(runtime_key) \
            or not _safe_segment(flavor):
        return _stage_refusal(stage, "component_identity_invalid")
    if not isinstance(gen_id, str) \
            or not gen_id.startswith("gen-") \
            or not _is_sha256(gen_id[4:]):
        return _stage_refusal(stage, "generation_id_invalid")
    iroot = Path(install_root)
    if not iroot.is_dir() or _reparse(iroot):
        return _stage_refusal(stage, "install_root_unsafe")
    _staging, gens, _comp = _namespaces(iroot, runtime_key, flavor)
    dest = gens / gen_id
    if _chain_reparse(dest, iroot):
        return _stage_refusal(stage, "generation_reparse")
    if not dest.is_dir():
        return _stage_refusal(stage, "generation_missing")
    meta, why = _read_meta(dest / _GENERATION_META)
    if why is not None:
        return _stage_refusal(stage, f"generation_incomplete:{why}")
    if meta.get("record") != "generation" \
            or meta.get("generation_id") != gen_id:
        return _stage_refusal(stage, "generation_foreign")
    digest = meta.get("descriptor_digest")
    desc, dref = _norm_descriptor(meta.get("descriptor"))
    if dref or not _is_str(digest, 96) \
            or generation_digest(desc) != digest \
            or generation_id(desc) != gen_id:
        return _stage_refusal(stage, "generation_descriptor_invalid")
    rd = desc["reference_digest"]
    if authority is not None:
        if not _authority_query(authority):
            return _stage_refusal(stage, "authority_context_invalid")
        if authority.is_revoked(rd):
            return _stage_refusal(stage, "reference_revoked")
    expected = {m["name"].casefold(): m for m in desc["members"]}
    mdir = dest / "members"
    if not mdir.is_dir() or _reparse(mdir):
        return _stage_refusal(stage, "generation_incomplete:members")
    vref = _verify_members_dir(mdir, expected, cancel_check)
    if vref:
        return _stage_refusal(stage, *[f"generation_invalid:{v}"
                                       for v in vref])
    record = gens / f"{gen_id}{_PUB_SUFFIX}"
    prec, pwhy = _read_meta(record)
    pub = PUB_NOT_PERFORMED
    if pwhy is None and prec.get("record") == "publication" \
            and prec.get("generation_id") == gen_id \
            and prec.get("descriptor_digest") == digest \
            and prec.get("committed") is True:
        pub = PUB_COMMITTED
    stage["generation"] = {"id": gen_id, "digest": digest}
    stage["reference"] = {"digest": rd,
                          "runtime_key": desc["runtime_key"],
                          "flavor": desc["flavor"]}
    stage["members"] = [m["name"] for m in desc["members"]]
    stage["publication"] = {"state": pub,
                            "commit_point": "namespace_rename"
                            if pub == PUB_COMMITTED else None,
                            "record": record.name
                            if pub == PUB_COMMITTED else None,
                            "renamed": pub == PUB_COMMITTED,
                            "fsynced": pub == PUB_COMMITTED}
    stage["revalidation"] = {"result": "match",
                             "selection": "not_established"}
    stage["fsm"] = _F_PUBLISHED
    out = _pub_env(stage, S_REVALIDATED)
    out["revalidation"] = stage["revalidation"]
    return out


# ------------------------------------------------------------ reconciliation


def _classify_staging(sdir: Path) -> tuple[str, dict]:
    meta, why = _read_meta(sdir / _STAGING_META)
    if why is not None:
        if why == "metadata_missing":
            return "staging_unknown", {}
        return "staging_metadata_malformed", {"cause": why}
    if meta.get("record") != "staging":
        return "staging_unknown", {}
    return "abandoned_attempt_owned", {
        "generation_id": meta.get("generation_id"),
        "fsm": meta.get("fsm"),
        "attempt": meta.get("attempt")}


def _classify_generation(gdir: Path, gens: Path, deep: bool,
                         authority, cancel_check,
                         counts) -> tuple[str, dict]:
    gen_id = gdir.name
    meta, why = _read_meta(gdir / _GENERATION_META)
    if why is not None:
        return ("generation_incomplete", {"cause": why})
    if meta.get("record") != "generation" \
            or meta.get("generation_id") != gen_id:
        return "generation_foreign", {}
    digest = meta.get("descriptor_digest")
    desc, dref = _norm_descriptor(meta.get("descriptor"))
    if dref or not _is_str(digest, 96) \
            or generation_digest(desc) != digest \
            or generation_id(desc) != gen_id:
        return "generation_descriptor_invalid", {}
    rd = desc["reference_digest"]
    if authority is not None and _authority_query(authority) \
            and authority.is_revoked(rd):
        return "reference_revoked", {"reference_digest": rd}
    ev = {"reference_digest": rd,
          "runtime_key": desc["runtime_key"],
          "flavor": desc["flavor"]}
    if deep:
        expected = {m["name"].casefold(): m for m in desc["members"]}
        vref = _verify_members_dir(gdir / "members", expected,
                                   cancel_check)
        if vref:
            return "generation_invalid", {**ev, "cause": vref[0]}
    record = gens / f"{gen_id}{_PUB_SUFFIX}"
    prec, pwhy = _read_meta(record)
    if pwhy is not None:
        if pwhy == "metadata_missing":
            return "generation_uncommitted", ev
        return "publication_record_malformed", {**ev, "cause": pwhy}
    if prec.get("record") != "publication" \
            or prec.get("generation_id") != gen_id \
            or prec.get("descriptor_digest") != digest \
            or prec.get("committed") is not True:
        return "publication_record_malformed", ev
    return "generation_committed", ev


def reconcile(install_root, *, runtime_key=None, flavor=None,
              authority=None, deep=False, cancel_check=None) -> dict:
    """Observational restart-style reconciliation — non-destructive,
    never adopts, repairs or deletes. Classifies every entry under the
    managed namespace; ambiguous, foreign or user-owned content is
    only reported. It does not coordinate with in-flight installs —
    transient mid-commit states may be classified; results describe
    read-time presence, not a stable post-commit snapshot."""
    iroot = Path(install_root)
    entries = []
    counts: dict[str, int] = {}

    def _note(cls, rel, ev=None):
        counts[cls] = counts.get(cls, 0) + 1
        e = {"path": rel, "classification": cls}
        if ev:
            e["evidence"] = ev
        entries.append(e)

    if not iroot.is_dir():
        return _env({"ok": False, "state": S_REFUSED,
                     "refusals": ["install_root_missing"],
                     "entries": [], "counts": {}})
    if _reparse(iroot):
        return _env({"ok": False, "state": S_REFUSED,
                     "refusals": ["install_root_reparse"],
                     "entries": [], "counts": {}})
    if authority is not None \
            and not _authority_query(authority):
        return _env({"ok": False, "state": S_REFUSED,
                     "refusals": ["authority_context_invalid"],
                     "entries": [], "counts": {}})

    legacy = iroot / "runtimes"
    if legacy.exists() and not _reparse(legacy):
        _note("legacy_installation", "runtimes")

    gens_root = iroot / _GENS_DIR
    key_dirs = []
    if gens_root.is_dir() and not _reparse(gens_root):
        try:
            key_dirs = sorted(p for p in gens_root.iterdir())
        except OSError:
            pass
    scanned = 0
    for kdir in key_dirs:
        if scanned >= _MAX_RECON_ENTRIES:
            _note("scan_bound_exceeded", str(kdir))
            break
        if runtime_key is not None and kdir.name != runtime_key:
            continue
        if _reparse(kdir) or not kdir.is_dir():
            _note("unrecognized", f"{_GENS_DIR}/{kdir.name}")
            continue
        try:
            fdirs = sorted(kdir.iterdir())
        except OSError:
            _note("unreadable", f"{_GENS_DIR}/{kdir.name}")
            continue
        for fdir in fdirs:
            if scanned >= _MAX_RECON_ENTRIES:
                _note("scan_bound_exceeded", str(fdir))
                break
            if flavor is not None and fdir.name != flavor:
                continue
            scanned += 1
            rel = f"{_GENS_DIR}/{kdir.name}/{fdir.name}"
            if _reparse(fdir) or not fdir.is_dir():
                _note("unrecognized", rel)
                continue
            sdir_root = fdir / _STAGING_DIR
            if sdir_root.is_dir() and not _reparse(sdir_root):
                try:
                    attempts = sorted(sdir_root.iterdir())
                except OSError:
                    attempts = []
                for a in attempts[:_MAX_RECON_ENTRIES]:
                    cls, evd = _classify_staging(a)
                    _note(cls, f"{rel}/{_STAGING_DIR}/{a.name}", evd)
            gdir = fdir / _GENERATIONS_DIR
            if gdir.is_dir() and not _reparse(gdir):
                try:
                    gentries = sorted(gdir.iterdir())
                except OSError:
                    gentries = []
                seen_gen = set()
                for g in gentries[:_MAX_RECON_ENTRIES]:
                    if g.name.endswith(_PUB_SUFFIX):
                        gid = g.name[:-len(_PUB_SUFFIX)]
                        if gid not in seen_gen \
                                and not (gdir / gid).is_dir():
                            _note("publication_orphaned",
                                  f"{rel}/{_GENERATIONS_DIR}/{g.name}")
                        continue
                    if not g.name.startswith("gen-"):
                        _note("unrecognized",
                              f"{rel}/{_GENERATIONS_DIR}/{g.name}")
                        continue
                    seen_gen.add(g.name)
                    cls, evd = _classify_generation(
                        g, gdir, deep, authority, cancel_check, counts)
                    _note(cls, f"{rel}/{_GENERATIONS_DIR}/{g.name}", evd)
            for extra in fdir.iterdir():
                if extra.name not in (_STAGING_DIR, _GENERATIONS_DIR):
                    _note("unrecognized", f"{rel}/{extra.name}")
    out = _env({"ok": True, "state": S_RECONCILED,
                "refusals": [], "entries": entries,
                "counts": counts,
                "destructive_actions": 0,
                "automatic_adoption": False})
    return out


__all__ = [
    "CONTRACT",
    "PUB_ALREADY",
    "PUB_COMMITTED",
    "PUB_NOT_PERFORMED",
    "PUB_UNCONFIRMED",
    "RGEN_VERSION",
    "S_CANCELLED",
    "S_FAILED",
    "S_PUBLISHED",
    "S_RECONCILED",
    "S_REFUSED",
    "S_REVALIDATED",
    "S_UNCERTAIN",
    "generation_digest",
    "generation_id",
    "install",
    "reconcile",
    "release_root",
    "reset_locks",
    "revalidate",
]
