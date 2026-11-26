"""Model file management: storage root, resumable verified downloads, removal.

Model weights live OUTSIDE the project CAS — in a dedicated models root
(app setting `models_dir`, else <data_dir>/models) that can be moved to
another drive. Downloads stream to `<file>.part` + a JSON sidecar holding
the source URL and expected size, so an interrupted download resumes with
a Range request instead of restarting. Integrity is checked on finalize:
declared sha256 when the source publishes one, else declared size + the
format's magic bytes (safetensors/GGUF). Nothing is marked 'installed'
that didn't verify.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AppSetting, ModelArtifact, ModelInstall, RuntimeInstall,
    new_id, utcnow)
from app.domain.model_catalog import ModelSpec, RuntimeSpec, serves_adapter

log = logging.getLogger(__name__)

_CHUNK = 4 * 1024 * 1024


class ModelError(Exception):
    pass


# ------------------------------------------------------------------
# U-5d-0 — exact model-execution binding substrate (execbind:v1)
#
# Legacy generation resolves installed models by unordered .first()
# model_key lookups at BOTH command preview and worker dispatch. The
# pinned path replaces that with an exact, versioned, server-built
# binding: one approved ModelInstall row, every required member file,
# and each member's expected SHA-256 — re-validated with streaming
# current-content verification before any external inference spawn.
# Ordinary unpinned generation is unchanged.
# ------------------------------------------------------------------

EXECBIND_VERSION = "execbind:v1"
EXECBIND_INTEGRITY = ("catalog_declared", "install_verified")
_EXECBIND_FIELDS = {"binding_version", "artifact_id", "model_key",
                    "install_id", "runtime_key", "adapter_id", "members"}
_EXECBIND_MEMBER_FIELDS = {"role", "file_name", "expected_sha256",
                           "integrity"}
_HEX64 = frozenset("0123456789abcdef")


class ExecbindError(ModelError):
    """Pinned-execution refusal — `code` is the stable machine reason."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _sha64_ok(v) -> bool:
    return (isinstance(v, str) and len(v) == 64
            and all(c in _HEX64 for c in v))


def resolve_install(session: Session, model_key: str) -> ModelInstall:
    """Exact selection for pinned binding: precisely one eligible
    installed row or a refusal. Unlike installed_file() this NEVER picks
    arbitrarily from an ambiguous set — 0 rows refuses 'not_installed',
    >1 rows refuses 'install_ambiguous'."""
    rows = session.execute(
        select(ModelInstall).where(
            ModelInstall.model_key == model_key,
            ModelInstall.status == "installed")).scalars().all()
    if not rows:
        raise ExecbindError(
            "not_installed",
            f"model '{model_key}' is not installed")
    if len(rows) > 1:
        raise ExecbindError(
            "install_ambiguous",
            f"model '{model_key}' has {len(rows)} eligible installed "
            "rows — exact binding refuses unordered selection")
    return rows[0]


def _expected_member_sha(install: ModelInstall, f) -> tuple[str, str]:
    """(expected_sha256, evidence_class) for one catalog-declared member.
    install.sha256 covers ONLY the primary weight file (install.file_name)
    — it is never reused as evidence for vae/text_encoder/clip_vision.
    Conflicting applicable sources refuse rather than silently prefer one."""
    candidates: dict[str, str] = {}
    if f.sha256:
        candidates["catalog_declared"] = f.sha256
    if (f.name == install.file_name and install.verified
            and install.sha256):
        candidates["install_verified"] = install.sha256
    if not candidates:
        raise ExecbindError(
            "missing_expected_hash",
            f"member '{f.name}' has no expected sha256 evidence")
    values = set(candidates.values())
    if len(values) > 1:
        raise ExecbindError(
            "conflicting_hash_evidence",
            f"member '{f.name}' has conflicting expected sha256 values")
    return values.pop(), ("catalog_declared"
                          if "catalog_declared" in candidates
                          else "install_verified")


def _check_install_artifact(session: Session, spec: ModelSpec,
                            install: ModelInstall, adapter_id: str,
                            artifact_id: str) -> None:
    """Record-level revalidation shared by build, commit validation and
    worker dispatch. The artifact identity is MANDATORY (C2): pinned
    execution is catalog-model execution, whose canonical registry
    identity is 'catalog:{model_key}' — a null or foreign artifact id
    can never satisfy it."""
    if install.model_key != spec.key:
        raise ExecbindError(
            "model_key_mismatch",
            f"installation belongs to '{install.model_key}', not '{spec.key}'")
    if install.status != "installed":
        raise ExecbindError(
            "install_status",
            f"installation status is '{install.status}', not 'installed'")
    if install.adapter_id != spec.adapter_id:
        raise ExecbindError(
            "install_adapter_mismatch",
            f"installation adapter '{install.adapter_id}' != declared "
            f"'{spec.adapter_id}'")
    if not serves_adapter(spec, adapter_id):
        raise ExecbindError(
            "adapter_mismatch",
            f"adapter '{adapter_id}' is not declared compatible with "
            f"'{spec.key}'")
    if artifact_id != f"catalog:{spec.key}":
        raise ExecbindError(
            "artifact_model_mismatch",
            f"artifact '{artifact_id}' is not the canonical registry "
            f"identity of model '{spec.key}'")
    art = session.get(ModelArtifact, artifact_id)
    if art is None:
        raise ExecbindError(
            "artifact_missing",
            f"registry artifact '{artifact_id}' not found")
    if not art.enabled:
        raise ExecbindError(
            "artifact_disabled",
            f"registry artifact '{artifact_id}' is disabled")
    if art.install_id != install.id:
        raise ExecbindError(
            "artifact_install_mismatch",
            f"artifact '{artifact_id}' is linked to a different "
            "installation")


def execbind_roles(spec: ModelSpec, *, adapter_id: str,
                   has_image_input: bool) -> tuple[str, ...]:
    """The exact member roles a pinned generation binds — every member
    the adapter resolves, no assumed extras."""
    if adapter_id == "gen.video.sdcpp":
        roles = ["diffusion", "vae", "text_encoder"]
        if has_image_input:
            # C3 — image-conditioned video requires the vision encoder
            # under the supported adapter contract. A catalog that
            # declares no clip_vision cannot establish that no encoder
            # is needed, so i2v on such a model fails CLOSED at member
            # resolution (member_unresolvable) — it is never silently
            # bound without image conditioning.
            roles.append("clip_vision")
        return tuple(roles)
    return ("diffusion",)


def _member_entries(spec: ModelSpec, install: ModelInstall,
                    roles: tuple[str, ...]) -> list[dict]:
    members: list[dict] = []
    seen: set[str] = set()
    for role in roles:
        cands = [f for f in spec.files if f.role == role]
        if len(cands) != 1:
            raise ExecbindError(
                "member_unresolvable",
                f"role '{role}' resolves to {len(cands)} declared "
                f"files for '{spec.key}' — exact binding requires "
                "precisely one")
        f = cands[0]
        if role == "diffusion" and install.file_name != f.name:
            raise ExecbindError(
                "primary_file_drift",
                f"installed primary '{install.file_name}' differs "
                f"from declared '{f.name}'")
        if f.name in seen:
            raise ExecbindError(
                "duplicate_member",
                f"member file '{f.name}' appears more than once")
        seen.add(f.name)
        sha, klass = _expected_member_sha(install, f)
        members.append({"role": role, "file_name": f.name,
                        "expected_sha256": sha, "integrity": klass})
    return members


def build_execbind(session: Session, spec: ModelSpec,
                   install: ModelInstall, *, adapter_id: str,
                   roles: tuple[str, ...],
                   artifact_id: str) -> dict:
    """Server-side execbind:v1 binding over the APPROVED install row.
    All record and member evidence checks run here; callers must NOT
    accept a client-supplied binding."""
    _check_install_artifact(session, spec, install, adapter_id,
                            artifact_id)
    members = _member_entries(spec, install, roles)
    base = Path(install.install_root) / install.rel_dir
    for m in members:
        if not (base / m["file_name"]).is_file():
            raise ExecbindError(
                "member_missing",
                f"required member '{m['file_name']}' is absent from "
                "the approved installation")
    return {"binding_version": EXECBIND_VERSION,
            "artifact_id": artifact_id,
            "model_key": spec.key,
            "install_id": install.id,
            "runtime_key": "sdcpp",
            "adapter_id": adapter_id,
            "members": members}


def validate_execbind_schema(binding) -> dict:
    """Strict execbind:v1 shape validation — unknown fields, wrong
    types, malformed ids/hashes and incomplete members all refuse."""
    if not isinstance(binding, dict):
        raise ExecbindError("binding_malformed",
                            "execution_binding is not an object")
    extra = set(binding) - _EXECBIND_FIELDS
    if extra:
        raise ExecbindError("binding_unknown_fields",
                            f"unknown binding fields {sorted(extra)}")
    missing = _EXECBIND_FIELDS - set(binding)
    if missing:
        raise ExecbindError("binding_incomplete",
                            f"missing binding fields {sorted(missing)}")
    if binding["binding_version"] != EXECBIND_VERSION:
        raise ExecbindError("binding_version",
                            f"unsupported binding_version "
                            f"'{binding['binding_version']}'")
    for k in ("model_key", "install_id", "runtime_key", "adapter_id"):
        if not isinstance(binding[k], str) or not binding[k]:
            raise ExecbindError("binding_malformed",
                                f"binding field '{k}' must be a "
                                "non-empty string")
    if (not isinstance(binding["artifact_id"], str)
            or not binding["artifact_id"]):
        raise ExecbindError("binding_malformed",
                            "binding field 'artifact_id' must be a "
                            "non-empty string")
    members = binding["members"]
    if not isinstance(members, list) or not members:
        raise ExecbindError("binding_incomplete",
                            "binding members must be a non-empty list")
    seen_names: set[str] = set()
    seen_roles: set[str] = set()
    for m in members:
        if not isinstance(m, dict) or set(m) != _EXECBIND_MEMBER_FIELDS:
            raise ExecbindError("binding_malformed",
                                "each member requires exactly role, "
                                "file_name, expected_sha256, integrity")
        if not isinstance(m["role"], str) or not m["role"]:
            raise ExecbindError("binding_malformed",
                                "member role must be a non-empty string")
        if not isinstance(m["file_name"], str) or not m["file_name"]:
            raise ExecbindError("binding_malformed",
                                "member file_name must be a non-empty "
                                "string")
        if not _sha64_ok(m["expected_sha256"]):
            raise ExecbindError("malformed_hash",
                                "member expected_sha256 must be "
                                "64 lowercase hex chars")
        if m["integrity"] not in EXECBIND_INTEGRITY:
            raise ExecbindError("binding_malformed",
                                f"member integrity '{m['integrity']}' "
                                "is not a supported evidence class")
        if m["file_name"] in seen_names or m["role"] in seen_roles:
            raise ExecbindError("duplicate_member",
                                "duplicate member role or file identity")
        seen_names.add(m["file_name"])
        seen_roles.add(m["role"])
    return binding


def validate_execbind(session: Session, spec: ModelSpec,
                      expected: dict, *, adapter_id: str,
                      roles: tuple[str, ...]) -> dict:
    """Re-validate an expected binding against CURRENT authoritative
    records. The pinned install_id is the identity — there is no
    re-resolution by model_key, so a replacement installation under the
    same key cannot substitute for the approved row."""
    validate_execbind_schema(expected)
    if expected["model_key"] != spec.key:
        raise ExecbindError("model_key_mismatch",
                            f"binding is for '{expected['model_key']}', "
                            f"not '{spec.key}'")
    if expected["adapter_id"] != adapter_id:
        raise ExecbindError("adapter_mismatch",
                            f"binding is for '{expected['adapter_id']}'"
                            f", not '{adapter_id}'")
    install = session.get(ModelInstall, expected["install_id"])
    if install is None:
        raise ExecbindError(
            "install_missing",
            "the approved installation no longer exists — refusing "
            "rather than substituting another")
    rebuilt = build_execbind(
        session, spec, install, adapter_id=adapter_id, roles=roles,
        artifact_id=expected.get("artifact_id"))
    if rebuilt != expected:
        raise ExecbindError(
            "binding_drift",
            "the approved installation, artifact link, member set or "
            "expected integrity evidence changed since validation")
    return expected


def stream_sha256(path: Path,
                  cancel_check: Callable[[], None] | None = None,
                  *, evidence: dict | None = None) -> str:
    """Bounded streaming SHA-256 of one member file. cancel_check runs
    once per chunk so worker cancellation semantics stay responsive.

    Opened-file drift evidence (F1): the file is opened once; os.fstat
    on the open descriptor captures size/mtime BEFORE and AFTER the
    stream — an in-place write through another handle during hashing is
    detected and refused. A post-close stat of the same path proves it
    still names the verified file: on filesystems that report a nonzero
    st_ino (NTFS, ext4, most POSIX volumes) a dev+ino change means the
    path was swapped mid-verify; where st_ino is unsupported the check
    degrades to size/mtime only (disclosed limitation — metadata equality
    is never claimed as content proof). Access time is never consulted.
    When `evidence` (a dict) is supplied it receives the verified file's
    identity/metadata for the pre-spawn revalidation gate."""
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        pre = os.fstat(fh.fileno())
        while True:
            if cancel_check is not None:
                cancel_check()
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            sha.update(chunk)
        post = os.fstat(fh.fileno())
    if post.st_size != pre.st_size \
            or post.st_mtime_ns != pre.st_mtime_ns:
        raise ExecbindError(
            "member_modified",
            "member changed while its content was being verified")
    try:
        cur = path.stat()
    except OSError as exc:
        raise ExecbindError(
            "member_unavailable",
            "member is no longer a readable file") from exc
    if pre.st_ino and cur.st_ino and \
            (cur.st_dev, cur.st_ino) != (pre.st_dev, pre.st_ino):
        raise ExecbindError(
            "member_replaced",
            "member file was replaced while its content was being "
            "verified")
    if cur.st_size != pre.st_size \
            or cur.st_mtime_ns != pre.st_mtime_ns:
        raise ExecbindError(
            "member_modified",
            "member changed while its content was being verified")
    if evidence is not None:
        evidence.update({"dev": pre.st_dev, "ino": pre.st_ino,
                         "size": pre.st_size,
                         "mtime_ns": pre.st_mtime_ns})
    return sha.hexdigest()


def _member_name_ok(name: str) -> bool:
    return bool(name) and name == Path(name).name \
        and not Path(name).is_absolute() and name not in (".", "..") \
        and "/" not in name and "\\" not in name and ":" not in name


class PinnedPaths(dict):
    """role → verified member Path plus the retained per-member
    verification evidence captured while hashing (U-5d-0 F1).

    revalidate() is the final pre-spawn drift gate: the member name is
    re-resolved inside the approved install dir (a retargeted link or
    escaped path resolves differently and refuses), the path must still
    be a readable regular file, and the current identity (dev+ino where
    the filesystem reports a nonzero index) and size/mtime must equal
    the verified file's. No second hash pass — the gate detects
    observable replacement/disappearance/size/modification drift; the
    residual verify→open race remains disclosed."""

    def revalidate(self, *, cancel_check: Callable[[], None] | None = None
                   ) -> None:
        install_dir = self._install_dir
        for ev in self._evidence:
            if cancel_check is not None:
                cancel_check()
            name = ev["file_name"]
            resolved = (install_dir / name).resolve()
            if resolved != ev["resolved"]:
                raise ExecbindError(
                    "member_replaced",
                    f"member '{name}' no longer resolves to the "
                    "verified file")
            try:
                st = resolved.stat()
            except OSError as exc:
                raise ExecbindError(
                    "member_unavailable",
                    f"member '{name}' is no longer a readable file"
                    ) from exc
            if not resolved.is_file():
                raise ExecbindError(
                    "member_unavailable",
                    f"member '{name}' is no longer a regular file")
            if ev["ino"] and st.st_ino and \
                    (st.st_dev, st.st_ino) != (ev["dev"], ev["ino"]):
                raise ExecbindError(
                    "member_replaced",
                    f"member '{name}' was replaced after verification")
            if st.st_size != ev["size"] \
                    or st.st_mtime_ns != ev["mtime_ns"]:
                raise ExecbindError(
                    "member_modified",
                    f"member '{name}' changed after verification")


def resolve_pinned_paths(session: Session, settings, spec: ModelSpec,
                         binding: dict, *, adapter_id: str,
                         roles: tuple[str, ...],
                         cancel_check: Callable[[], None] | None = None
                         ) -> PinnedPaths:
    """Worker-side pinned resolution: revalidate the persisted binding
    against current records, resolve every required member under the
    APPROVED installation directory inside the authorized models root,
    and stream-verify current SHA-256 content — all BEFORE any external
    inference spawn. Any failure refuses; nothing falls back to
    unordered model-key resolution."""
    validate_execbind_schema(binding)
    if binding["model_key"] != spec.key:
        raise ExecbindError("model_key_mismatch",
                            f"binding is for '{binding['model_key']}', "
                            f"not '{spec.key}'")
    if binding["adapter_id"] != adapter_id:
        raise ExecbindError("adapter_mismatch",
                            f"binding is for '{binding['adapter_id']}'"
                            f", not '{adapter_id}'")
    install = session.get(ModelInstall, binding["install_id"])
    if install is None:
        raise ExecbindError("install_missing",
                            "the approved installation no longer "
                            "exists")
    _check_install_artifact(session, spec, install, adapter_id,
                            binding.get("artifact_id"))
    bound = {m["role"] for m in binding["members"]}
    if bound != set(roles):
        raise ExecbindError(
            "member_set_mismatch",
            f"binding members {sorted(bound)} != required members "
            f"{sorted(roles)}")
    # Expected-evidence re-derivation: the pinned expected values must
    # still match what current records produce — no trusting of stale
    # or self-declared binding evidence.
    declared: dict[str, object] = {}
    for m in binding["members"]:
        f = next((x for x in spec.files if x.name == m["file_name"]),
                 None)
        if f is None:
            raise ExecbindError("unexpected_member",
                                f"member '{m['file_name']}' is not a "
                                f"declared file of '{spec.key}'")
        if f.role != m["role"]:
            raise ExecbindError("wrong_member_role",
                                f"member '{f.name}' is role "
                                f"'{f.role}', not '{m['role']}'")
        sha, klass = _expected_member_sha(install, f)
        if sha != m["expected_sha256"] or klass != m["integrity"]:
            raise ExecbindError("hash_evidence_drift",
                                f"expected evidence for '{f.name}' "
                                "changed since the binding was "
                                "approved")
        declared[f.name] = f
    # Containment: authorized root comes from settings, never from the
    # persisted row — a corrupted install path cannot define its own
    # trusted boundary.
    root = models_root(session, settings).resolve()
    install_root = Path(install.install_root).resolve()
    if install_root != root:
        raise ExecbindError("install_root_mismatch",
                            "approved install root is not the "
                            "authorized models root")
    install_dir = (install_root / install.rel_dir).resolve()
    if install_dir != install_root and install_root not in \
            install_dir.parents:
        raise ExecbindError("install_dir_escape",
                            "install directory escapes the authorized "
                            "models root")
    out = PinnedPaths()
    evidence: list[dict] = []
    for m in binding["members"]:
        name = m["file_name"]
        if not _member_name_ok(name):
            raise ExecbindError("member_path_invalid",
                                "member file_name is not a plain "
                                "relative name")
        p = install_dir / name
        resolved = p.resolve()
        if install_dir not in resolved.parents:
            raise ExecbindError("member_escape",
                                f"member '{name}' resolves outside "
                                "the approved installation")
        if not resolved.is_file():
            raise ExecbindError("member_unavailable",
                                f"member '{name}' is not a regular "
                                "readable file")
        # Detectable drift (C5): a changed size refuses before the
        # hash pass — a cheap, portable metadata check. The streaming
        # hash remains the authoritative content proof.
        declared_size = getattr(declared[name], "size_bytes", 0) or 0
        if declared_size and resolved.stat().st_size != declared_size:
            raise ExecbindError(
                "member_size_drift",
                f"member '{name}' is {resolved.stat().st_size} "
                f"bytes, expected the declared {declared_size}")
        # Hash the containment-verified path — the residual
        # verify→open TOCTOU is disclosed in the milestone report.
        # The verified file's opened-descriptor identity/metadata is
        # retained for the final pre-spawn revalidation (F1).
        ev = {"file_name": name, "resolved": resolved}
        digest = stream_sha256(resolved, cancel_check, evidence=ev)
        if "size" not in ev:
            # stream_sha256 patched without evidence forwarding (test
            # spies) — anchor the same fields from a fresh stat.
            st = resolved.stat()
            ev.update({"dev": st.st_dev, "ino": st.st_ino,
                       "size": st.st_size,
                       "mtime_ns": st.st_mtime_ns})
        if digest != m["expected_sha256"]:
            raise ExecbindError("integrity_mismatch",
                                f"current content of '{name}' does "
                                "not match the approved hash")
        evidence.append(ev)
        out[m["role"]] = resolved
    out._install_dir = install_dir
    out._evidence = evidence
    # End-of-resolution gate: a member verified first may have drifted
    # while later members were hashing — refuse before returning the
    # approved map. Callers re-run the same gate at the spawn boundary.
    out.revalidate(cancel_check=cancel_check)
    return out


def _get_setting(session: Session, key: str) -> Any:
    row = session.get(AppSetting, key)
    return (row.value_json or {}).get("value") if row else None


def _set_setting(session: Session, key: str, value: Any) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value_json={})
        session.add(row)
        session.flush()
    row.value_json = {"value": value}


# ------------------------------------------------------------------ roots

def models_root(session: Session, settings) -> Path:
    custom = _get_setting(session, "models_dir")
    if custom:
        return Path(custom)
    base = getattr(settings, "models_dir", None) or (settings.data_dir / "models")
    return Path(base)


def runtimes_root(settings) -> Path:
    return settings.data_dir / "runtimes"


def set_models_root(session: Session, path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ModelError("models directory must be an absolute path")
    p.mkdir(parents=True, exist_ok=True)
    probe = p / f".atomic-probe-{new_id()[:8]}"
    try:
        probe.write_bytes(b"ok")
    except OSError as exc:
        raise ModelError(f"cannot write to {p}: {exc}")
    finally:
        probe.unlink(missing_ok=True)
    _set_setting(session, "models_dir", str(p))
    return p


def unset_models_root(session: Session) -> None:
    row = session.get(AppSetting, "models_dir")
    if row is not None:
        session.delete(row)


def model_dir(root: Path, spec: ModelSpec) -> Path:
    return root / spec.key


def model_file_path(root: Path, spec: ModelSpec) -> Path:
    return model_dir(root, spec) / spec.files[0].name if spec.files else model_dir(root, spec)


# --------------------------------------------------------------- downloads

def _format_magic_ok(path: Path, fmt: str) -> bool:
    try:
        head = path.read_bytes()[:16]
    except OSError:
        return False
    if fmt == "gguf":
        return head[:4] == b"GGUF"
    if fmt == "safetensors":
        # Header: 8-byte little-endian header length, then '{' JSON.
        return len(head) >= 9 and head[8:9] == b"{"
    return bool(head)  # unknown formats: non-empty is all we can check


def download_to(dest: Path, url: str, *, expected_size: int | None = None,
                progress: Callable[[int, int], None] | None = None,
                cancel_check: Callable[[], None] | None = None,
                timeout: int = 60,
                allowed_hosts: tuple[str, ...] = (),
                deadline_s: float = 3600.0,
                stall_s: float | None = None,
                verify: Callable[[Path], tuple[bool, str]] | None = None,
                resolver=None, connect=None, wrap=None,
                conn_factory=None) -> dict:
    """Stream url -> dest.part with Range-resume, verify, then publish.

    Issue #175 — pinned transport: `url` is the ONLY source (catalog-
    approved; a stored sidecar URL can never replace it). Every hop is
    re-validated through the egress gate + a provider allowlist; only
    validated literal IPs are dialed and TLS still authenticates the
    original hostname. Redirect-target URLs are never persisted — the
    <dest>.part.json sidecar stores the ENTRY url + expected size only.

    Publication order (verify BEFORE the canonical path is touched):
    exact-size check → optional `verify(.part)` (declared sha256 /
    format-magic) → atomic os.replace. A failed verification never
    publishes — a previously valid `dest` is left untouched.

    Explicit recovery policy for partial state:
      - incomplete-but-resumable .part (short / transient failure /
        cancellation): KEPT for resume.
      - .part that can never become the artifact (over-cap stream,
        verify failure — appended bytes cannot repair wrong content):
        DISCARDED.
    Returns {bytes, sha256}."""
    from app.domain import download_security as ds
    from app.net import egress

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    sidecar = part.with_suffix(part.suffix + ".json")
    for p in (part, sidecar, dest):
        ds.ensure_not_symlink(p)

    # Sidecar reconciliation — reuse state only when it agrees with the
    # catalog-approved source; mismatched partial state is discarded.
    stored = None
    if sidecar.exists():
        try:
            stored = json.loads(sidecar.read_text())
        except (OSError, json.JSONDecodeError):
            stored = None
    if not ds.reconcile_sidecar(stored, url=url, expected_size=expected_size):
        part.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
    meta = {"url": url, "expected_size": expected_size}
    sidecar.write_text(json.dumps(meta))

    expected = expected_size
    have = part.stat().st_size if part.exists() else 0
    # Transfer cap is SEPARATE from the exact-size contract: declared size
    # + slack, else the global ceiling when nothing is declared.
    cap = (expected + ds.SIZE_SLACK) if expected else ds.GLOBAL_SIZE_CAP
    stall = stall_s if stall_s is not None else float(timeout)
    t0 = time.monotonic()
    # Bounded retry: multi-GB downloads drop mid-stream; each attempt
    # resumes via Range instead of restarting.
    attempt, max_attempts = 0, 8
    while True:
        attempt += 1
        # Defense in depth: a local attacker could substitute .part/
        # sidecar/dest between attempts — re-check each iteration. This
        # shrinks but does not close the check→open TOCTOU window (a
        # handle-based check would be needed for that).
        for p in (part, sidecar, dest):
            ds.ensure_not_symlink(p)
        conn = None
        try:
            conn, resp, start, total = ds.open_download_stream(
                url, resume_from=have, allowed_hosts=allowed_hosts,
                expected_total=expected, timeout_s=timeout,
                resolver=resolver, connect=connect, wrap=wrap,
                conn_factory=conn_factory)
            if have and start == 0:
                have = 0  # server ignored Range — restart cleanly
            last_data = time.monotonic()
            with open(part, "ab" if have else "wb") as fh:
                while True:
                    if cancel_check is not None:
                        cancel_check()
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    have += len(chunk)
                    now = time.monotonic()
                    if now - last_data > stall:
                        raise ModelError(
                            f"download stalled — no progress for "
                            f"{int(stall)}s, partial kept")
                    last_data = now
                    if have > cap:
                        raise egress.EgressRefused(
                            "size", f"stream exceeds {cap}-byte transfer cap")
                    if now - t0 > deadline_s:
                        raise ModelError(
                            f"download deadline ({int(deadline_s)}s) exceeded "
                            "— partial kept, resumable")
                    if progress:
                        progress(have, total or 0)
            conn.close()
            break  # stream complete
        except egress.EgressRefused as exc:
            if conn is not None:
                conn.close()
            if exc.code == "size" and have > cap:
                # Recovery policy: an over-cap .part can never become the
                # artifact — discard rather than let resume re-fail
                # forever. (Pre-stream "size" refusals leave the valid
                # resumable prefix intact.)
                part.unlink(missing_ok=True)
                sidecar.unlink(missing_ok=True)
            # "resolve" can be a transient DNS flake — retry within the
            # budget; every other egress code is a policy refusal that
            # retrying cannot fix.
            if exc.code != "resolve":
                raise
            sidecar.write_text(json.dumps(meta))
            if attempt >= max_attempts:
                raise
            time.sleep(min(2 ** attempt, 30))
        except ds.RangeUnsatisfiable:
            if conn is not None:
                conn.close()
            # 416 — stored offset refused; discard partial state, restart.
            have = 0
            part.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
            sidecar.write_text(json.dumps(meta))
            if attempt >= max_attempts:
                raise ModelError(
                    "server refused resume (416) repeatedly — download failed")
            time.sleep(min(2 ** attempt, 30))
        except Exception as exc:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            # Cancellation (JobCancelled — imported lazily by name to avoid an
            # adapters→domain import cycle) must never be retried.
            if exc.__class__.__name__ in ("JobCancelled", "ModelError"):
                raise
            sidecar.write_text(json.dumps(meta))
            if attempt >= max_attempts:
                raise
            retry_after = getattr(exc, "retry_after", None)
            time.sleep(retry_after if retry_after is not None
                       else min(2 ** attempt, 30))

    if expected and have != expected:
        sidecar.write_text(json.dumps(meta))
        raise ModelError(
            f"download incomplete: {have} bytes received, {expected} expected — "
            "run again to resume")

    sha = hashlib.sha256()
    with open(part, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            sha.update(chunk)
    if verify is not None:
        ok, why = verify(part)
        if not ok:
            # Verify-before-publish: invalid bytes never reach the
            # canonical path and `dest` (possibly a previously valid
            # artifact) is untouched. Wrong content is NOT resumable —
            # discard partial state per the explicit recovery policy.
            part.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
            raise ModelError(f"verification failed ({dest.name}): {why}")
    part.replace(dest)
    sidecar.unlink(missing_ok=True)
    return {"bytes": have, "sha256": sha.hexdigest()}


def verify_component(path: Path, f: ModelFile, fmt: str) -> tuple[bool, str]:
    """Integrity check on one component file: declared sha256 if present,
    else declared size + format magic bytes."""
    if f.sha256:
        sha = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK), b""):
                sha.update(chunk)
        if sha.hexdigest() != f.sha256:
            return False, "sha256 mismatch with catalog checksum"
        return True, "sha256 verified"
    if f.size_bytes and path.stat().st_size != f.size_bytes:
        return False, (f"size mismatch: {path.stat().st_size} bytes, "
                       f"catalog expected {f.size_bytes}")
    if not _format_magic_ok(path, fmt):
        return False, f"file does not look like a {fmt} model (bad magic bytes)"
    return True, f"size + {fmt} format verified"


def verify_model_file(path: Path, spec: ModelSpec) -> tuple[bool, str]:
    return verify_component(path, spec.files[0], spec.fmt)


# ------------------------------------------------------------------- LoRA

def force_rmtree(path: Path) -> None:
    """Best-effort tree removal that tolerates read-only members.

    G2-B staged LoRAs are chmod'd read-only after verification; on
    Windows a read-only attribute blocks plain ``rmtree``. The retry
    clears the bit and re-attempts; any residual failure is swallowed —
    cleanup must never mask the job's real outcome."""
    def _retry(func, p, _exc):
        try:
            os.chmod(p, 0o666)
            func(p)
        except OSError:
            pass
    try:
        shutil.rmtree(path, onexc=_retry)
    except OSError:
        pass


_MAX_LORA_HEADER = 64 * 1024 * 1024
# Tensor-name families every supported LoRA layout uses at least one of —
# kohya/diffusers (lora_down/lora_up or lora_A/lora_B) plus the alpha scale.
_LORA_KEY_HINTS = ("lora_down", "lora_up", "lora_a", "lora_b", ".lora.",
                   "alpha")


def verify_lora_file(path: Path, spec: ModelSpec) -> tuple[bool, str]:
    """Structural LoRA validation — a ``.safetensors`` EXTENSION alone
    proves nothing. Parses the 8-byte length + JSON header and requires
    LoRA-shaped tensor keys, then the declared sha256/size integrity.
    gguf LoRAs stay refused until their metadata contract is verified
    against the pinned runtime."""
    if spec.fmt != "safetensors":
        return False, (f"LoRA format '{spec.fmt}' is not enabled — only "
                       "safetensors adapters are verified on this runtime")
    if path.suffix.lower() != ".safetensors":
        return False, "LoRA file must have a .safetensors extension"
    try:
        with open(path, "rb") as fh:
            n = int.from_bytes(fh.read(8), "little")
            if n <= 0 or n > _MAX_LORA_HEADER:
                return False, "safetensors header length out of range"
            header = json.loads(fh.read(n))
    except (OSError, ValueError, json.JSONDecodeError):
        return False, "file is not a parseable safetensors container"
    if not isinstance(header, dict):
        return False, "safetensors header is not a tensor map"
    lower = {k.lower() for k in header}
    if not any(any(h in k for h in _LORA_KEY_HINTS) for k in lower):
        return False, ("no LoRA tensor names found — file parses as "
                       "safetensors but does not look like a LoRA adapter")
    ok, why = verify_component(path, spec.files[0], spec.fmt) \
        if spec.files else (False, "LoRA spec declares no file")
    return ok, why


# ---------------------------------------------------------------- install

def install_row(session: Session, spec: ModelSpec, root: Path) -> ModelInstall:
    """Create-or-reuse the install row for a model being (re)downloaded."""
    row = session.execute(
        select(ModelInstall).where(ModelInstall.model_key == spec.key)
        .order_by(ModelInstall.created_at.desc())
    ).scalars().first()
    if row is None or row.status in ("installed", "removed"):
        row = ModelInstall(
            id=new_id(), model_key=spec.key, adapter_id=spec.adapter_id,
            version=spec.version, license=(spec.license or {}).get("name", ""),
            status="downloading", install_root=str(root), rel_dir=spec.key,
            file_name=spec.files[0].name if spec.files else "",
            meta_json={"source": spec.source},
        )
        session.add(row)
    else:
        row.status = "downloading"
        row.error = None
        row.install_root = str(root)
        # a stale row may predate catalog changes (renamed artifact, new
        # version) — always refresh spec-derived fields on reuse
        row.rel_dir = spec.key
        row.file_name = spec.files[0].name if spec.files else ""
        row.version = spec.version
        row.adapter_id = spec.adapter_id
        row.license = (spec.license or {}).get("name", "")
        row.meta_json = {"source": spec.source}
    session.flush()
    return row


def installed_component(session: Session, settings, spec: ModelSpec, role: str) -> Path | None:
    """Path to an installed component by role (vae / text_encoder / …)."""
    row = session.execute(
        select(ModelInstall).where(ModelInstall.model_key == spec.key,
                                   ModelInstall.status == "installed")
    ).scalars().first()
    if row is None:
        return None
    for f in spec.files:
        if f.role == role:
            p = Path(row.install_root) / row.rel_dir / f.name
            return p if p.exists() else None
    return None


def installed_file(session: Session, settings, spec: ModelSpec) -> Path | None:
    """Path to the verified installed weight file, or None. For multi-
    component models every declared file must exist on disk."""
    row = session.execute(
        select(ModelInstall).where(ModelInstall.model_key == spec.key,
                                   ModelInstall.status == "installed")
    ).scalars().first()
    if row is None or not row.file_name:
        return None
    base = Path(row.install_root) / row.rel_dir
    if len(spec.files) > 1 and not all((base / f.name).is_file() for f in spec.files):
        return None
    path = base / row.file_name
    return path if path.is_file() else None


def installs_by_key(session: Session) -> dict[str, dict]:
    """Latest install row per model_key, serialized."""
    rows = session.execute(select(ModelInstall).order_by(ModelInstall.created_at.desc())).scalars().all()
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r.model_key, {
            "id": r.id, "status": r.status, "install_root": r.install_root,
            "rel_dir": r.rel_dir, "file_name": r.file_name, "byte_size": r.byte_size,
            "verified": r.verified, "version": r.version, "error": r.error,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return out


def remove_model(session: Session, spec: ModelSpec) -> dict:
    """Delete an installed model's files (inside its install root only) and
    mark the row removed. Refuses while a job is using it (caller checks)."""
    row = session.execute(
        select(ModelInstall).where(ModelInstall.model_key == spec.key)
        .order_by(ModelInstall.created_at.desc())
    ).scalars().first()
    if row is None or row.status != "installed":
        raise ModelError("model is not installed")
    target = (Path(row.install_root) / row.rel_dir).resolve()
    root = Path(row.install_root).resolve()
    if root not in target.parents:
        raise ModelError("refusing to remove a path outside the models root")
    freed = 0
    if target.is_dir():
        for f in target.rglob("*"):
            if f.is_file():
                freed += f.stat().st_size
        shutil.rmtree(target)
    row.status = "removed"
    row.byte_size = 0
    row.verified = False
    return {"removed": spec.key, "bytes_freed": freed}


# ---------------------------------------------------------------- runtime

def runtime_dir(settings, spec: RuntimeSpec, flavor: str) -> Path:
    return runtimes_root(settings) / f"{spec.key}-{flavor}"


def runtime_exe(settings, spec: RuntimeSpec, flavor: str) -> Path:
    return runtime_dir(settings, spec, flavor) / spec.exe_name


def runtime_row(session: Session, key: str) -> RuntimeInstall | None:
    return session.execute(
        select(RuntimeInstall).where(RuntimeInstall.runtime_key == key)
        .order_by(RuntimeInstall.created_at.desc())
    ).scalars().first()


def runtime_rows(session: Session) -> dict[str, dict]:
    rows = session.execute(select(RuntimeInstall)).scalars().all()
    return {r.runtime_key: {
        "status": r.status, "install_dir": r.install_dir, "exe_path": r.exe_path,
        "version": r.version, "verified": r.verified, "error": r.error,
    } for r in rows}


def resolve_release_asset(spec: RuntimeSpec, flavor: str, *,
                          fetch: Callable[[str], dict] | None = None) -> dict:
    """Pick the release asset for a backend flavor from the GitHub releases
    API. `fetch` injectable for tests (no network in CI).

    When the runtime spec pins `release_tag`, the pinned-tag endpoint is
    queried so every install resolves the SAME artifacts; otherwise the
    floating `latest` endpoint is used and whatever it resolves to is
    recorded on the install row."""
    api = spec.release_api
    pinned = bool(getattr(spec, "release_tag", None))
    if pinned:
        api = api.rsplit("/", 1)[0] + f"/tags/{spec.release_tag}" \
            if api.endswith("/latest") else api
    # The release metadata fetch uses the same pinned/allowlisted
    # transport as artifact downloads — a plain urlopen would re-open
    # scheme/redirect/DNS gaps the artifact path just closed.
    if fetch is None:
        from app.domain import download_security as ds
        fetch = lambda url: json.loads(ds.fetch_bytes(
            url, allowed_hosts=ds.HOST_ALLOWLISTS["github"],
            max_bytes=4 * 1024 * 1024, timeout_s=30))
    rel = fetch(api)
    needle = spec.asset_patterns.get(flavor, flavor).lower()
    for asset in rel.get("assets", []):
        name = (asset.get("name") or "").lower()
        if needle in name and "win" in name and name.endswith(".zip"):
            return {"url": asset["browser_download_url"], "name": asset["name"],
                    "size": asset.get("size") or 0, "tag": rel.get("tag_name", ""),
                    "pinned": pinned, "resolved_from": api,
                    "resolved_at": utcnow().isoformat()}
    raise ModelError(f"no Windows '{flavor}' asset found in {spec.key} release")


def smoke_check_runtime(exe_path: str | Path, timeout: int = 20) -> tuple[bool, str]:
    """Launch the runtime CLI with --help: ok = exits 0 with output.
    Catches installed-but-broken runtimes (e.g. the CUDA build missing
    cudart64_12.dll exits instantly, silently)."""
    import subprocess
    try:
        r = subprocess.run([str(exe_path), "--help"], capture_output=True,
                           text=True, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0 and out.strip():
            return True, out.strip()[:200]
        return False, f"exit {r.returncode}: {out.strip()[:200] or 'no output'}"
    except Exception as exc:  # timeout, OSError (missing loader deps)
        return False, f"{type(exc).__name__}: {exc}"


def install_runtime_zip(session: Session, settings, spec: RuntimeSpec, flavor: str,
                        asset: dict, *, progress=None, cancel_check=None) -> RuntimeInstall:
    """Download + extract a runtime zip into runtimes/<key>-<flavor>/."""
    import io
    import zipfile

    dest_dir = runtime_dir(settings, spec, flavor)
    tmp_zip = dest_dir.parent / f"{spec.key}-{flavor}.download.zip"

    row = RuntimeInstall(
        id=new_id(), runtime_key=f"{spec.key}-{flavor}",
        version=asset.get("tag") or spec.version,
        status="downloading", install_dir=str(dest_dir),
        exe_path=str(dest_dir / spec.exe_name),
        meta_json={"flavor": flavor, "resolved": {
            "name": asset.get("name"), "url": asset.get("url"),
            "size": asset.get("size"), "tag": asset.get("tag"),
            "pinned": asset.get("pinned", False),
            "resolved_from": asset.get("resolved_from"),
            "resolved_at": asset.get("resolved_at")}},
    )
    session.add(row)
    session.flush()

    try:
        from app.domain import download_security as ds
        ds.ensure_contained(runtimes_root(settings), tmp_zip)
        res = download_to(tmp_zip, asset["url"], expected_size=asset.get("size") or None,
                          allowed_hosts=ds.HOST_ALLOWLISTS["github"],
                          progress=progress, cancel_check=cancel_check)
        if cancel_check:
            cancel_check()
        with zipfile.ZipFile(tmp_zip) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if spec.exe_name not in {Path(n).name for n in names}:
                raise ModelError(f"{spec.exe_name} not found inside {asset.get('name')}")
            dest_dir.mkdir(parents=True, exist_ok=True)
            for n in names:
                out = dest_dir / Path(n).name  # flatten: release zips nest dirs
                if cancel_check:
                    cancel_check()
                with zf.open(n) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=_CHUNK)
        tmp_zip.unlink(missing_ok=True)
        row.byte_size = res["bytes"]
        row.verified = True
        meta = dict(row.meta_json or {})
        meta.setdefault("resolved", {})["zip_sha256"] = res.get("sha256")
        row.meta_json = meta
        ok, detail = smoke_check_runtime(row.exe_path)
        meta["smoke"] = {"ok": ok, "detail": detail[-400:],
                         "checked_at": utcnow().isoformat()}
        row.meta_json = meta
        if ok:
            row.status = "installed"
            row.error = None
        else:
            row.status = "error"
            row.error = ("runtime installed but failed its launch check — "
                         f"missing dependencies? {detail[:160]}")
    except Exception as exc:
        row.status = "failed"
        row.error = str(exc)
        session.flush()
        raise
    session.flush()
    return row


def sweep_partial_downloads(root: Path) -> int:
    """Leftover '.part' files are resumable, so they are kept — only stale
    probe/lock files are swept. Returns count removed."""
    return 0


def disk_free_bytes(path: Path) -> int | None:
    try:
        probe = path if path.is_dir() else path.parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return shutil.disk_usage(probe).free
    except OSError:
        return None
