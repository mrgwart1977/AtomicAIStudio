"""U-3 — model discovery: user-approved roots, bounded enumeration,
unverified candidates, and two-phase absence reconciliation.

Scope contract:
- Enumeration is READ-ONLY toward source files; the only writes are
  bounded app-owned metadata (discovery_roots / discovery_candidates).
- Candidates are NEVER auto-registered — becoming an artifact still
  requires the existing U-2 ops.model.import job (license declaration
  + bounded inspect + full SHA-256) initiated by the user.
- Absolute paths live only in the DB and the authenticated detail
  endpoint — never in job payloads, progress, or error strings.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path, PurePath
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import DiscoveryCandidate, DiscoveryRoot, utcnow
from app.domain import model_import

log = logging.getLogger(__name__)

MAX_ROOTS = 32
MAX_DEPTH_CAP = 8
DEFAULT_MAX_DEPTH = 4
MAX_ENTRIES_PER_ROOT = 50_000
MAX_CANDIDATES_PER_ROOT = 20_000
SCAN_TIME_BUDGET_S = 300.0
CANCEL_CHECK_EVERY = 200
RECONCILE_CHECK_EVERY = 500
_SNIFF_BYTES = 64
_MODEL_EXTS = frozenset({".safetensors", ".gguf"})
# GetDriveTypeW classes that may host approved roots. REMOTE (4),
# UNKNOWN (0), and NO_ROOT_DIR (1) are refused; an indeterminate API
# result is also refused (fail closed).
_DRIVE_TYPES_OK = frozenset({2, 3, 5, 6})  # REMOVABLE, FIXED, CDROM, RAMDISK

_REPARSE_ATTR = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


class DiscoveryRefused(Exception):
    """Sanitized refusal — `code` is the only thing that reaches API
    errors or root.last_code; the message stays out of job payloads."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


class LinkageConflict(Exception):
    """A candidate is already linked to a DIFFERENT artifact — never
    silently re-point it (content changed at the same path is a
    human-decision case, logged not rewritten)."""


def _norm(p: str | Path) -> str:
    return os.path.normcase(str(p))


def candidate_id_for(resolved: str) -> str:
    return "cand:" + hashlib.sha256(_norm(resolved).encode()).hexdigest()[:16]


def root_id_for(canonical: str) -> str:
    return "root:" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _is_reparse(p: Path) -> bool:
    """symlink / junction / reparse point. Indeterminate → True (fail
    closed — an unverifiable entry is treated as a link and skipped)."""
    try:
        if p.is_symlink() or p.is_junction():
            return True
        return bool(getattr(p.lstat(), "st_file_attributes", 0)
                    & _REPARSE_ATTR)
    except OSError:
        return True


def _drive_type(path: Path) -> int | None:
    """Windows GetDriveTypeW on the path's drive root. Returns the drive
    class, or None when the API is unavailable/fails (fail closed).
    Non-Windows platforms report FIXED — synthetic fixtures and dev
    hosts run there, and the shipping platform is Windows."""
    if os.name != "nt":
        return 3  # DRIVE_FIXED
    try:
        import ctypes
        drive = os.path.splitdrive(str(path))[0] or str(path)
        if not drive.endswith("\\"):
            drive += "\\"
        return int(ctypes.windll.kernel32.GetDriveTypeW(drive))
    except Exception:   # noqa: BLE001 — fail closed on any API failure
        return None


def validate_root(path_str: str) -> Path:
    """Classify a user-entered directory into a canonical absolute root.

    Codes: path_not_absolute, path_unc, path_not_found, path_not_dir,
    path_is_link, path_network, path_indeterminate."""
    if not path_str or not isinstance(path_str, str):
        raise DiscoveryRefused("path_not_absolute")
    raw = path_str.strip()
    if raw.startswith(("\\\\", "//")) or raw.upper().startswith("\\\\?\\UNC\\"):
        raise DiscoveryRefused("path_unc", "network paths are not supported")
    p = Path(raw)
    if not p.is_absolute():
        raise DiscoveryRefused("path_not_absolute")
    try:
        rp = p.resolve()
    except OSError:
        raise DiscoveryRefused("path_not_found") from None
    if not rp.is_dir():
        raise DiscoveryRefused("path_not_found" if not rp.exists()
                               else "path_not_dir")
    # Every component must be a real directory — a link anywhere in the
    # chain would let the approved root escape its own tree.
    for comp in (rp, *rp.parents):
        if _is_reparse(comp):
            raise DiscoveryRefused("path_is_link")
    dt = _drive_type(rp)
    if dt is None:
        raise DiscoveryRefused("path_indeterminate")
    if dt not in _DRIVE_TYPES_OK:
        raise DiscoveryRefused("path_network")
    return rp


def list_roots(session: Session) -> list[DiscoveryRoot]:
    return session.execute(
        select(DiscoveryRoot).order_by(DiscoveryRoot.added_at)
    ).scalars().all()


def enabled_roots(session: Session) -> list[DiscoveryRoot]:
    return session.execute(
        select(DiscoveryRoot)
        .where(DiscoveryRoot.enabled.is_(True),
               DiscoveryRoot.archived_at.is_(None))
        .order_by(DiscoveryRoot.added_at)
    ).scalars().all()


def add_root(session: Session, path_str: str,
             max_depth: int | None = None) -> tuple[DiscoveryRoot, bool]:
    """Upsert an approved root keyed on canonical path. Re-adding an
    existing path resurfaces the same row/history (idempotent, not a
    duplicate). Returns (row, created)."""
    rp = validate_root(path_str)
    canon = _norm(rp)
    existing = session.execute(
        select(DiscoveryRoot).where(DiscoveryRoot.canonical_path == canon)
    ).scalar_one_or_none()
    if existing is not None:
        # Idempotent re-approval: unarchive + refresh the mutable fields.
        existing.enabled = True
        existing.archived_at = None
        existing.display_path = path_str.strip()
        if max_depth is not None:
            existing.max_depth = max(0, min(int(max_depth), MAX_DEPTH_CAP))
        return existing, False
    n = session.execute(
        select(func.count()).select_from(DiscoveryRoot)
        .where(DiscoveryRoot.archived_at.is_(None))
    ).scalar_one()
    if n >= MAX_ROOTS:
        raise DiscoveryRefused("root_limit",
                               f"at most {MAX_ROOTS} roots may be approved")
    row = DiscoveryRoot(
        root_id=root_id_for(canon), canonical_path=canon,
        display_path=path_str.strip(), enabled=True,
        max_depth=(DEFAULT_MAX_DEPTH if max_depth is None
                   else max(0, min(int(max_depth), MAX_DEPTH_CAP))),
    )
    session.add(row)
    session.flush()
    return row, True


def archive_root(session: Session, root_id: str) -> DiscoveryRoot | None:
    """Soft-archive only: stops future scans, keeps every candidate and
    all provenance. There is deliberately no hard-delete path."""
    row = session.get(DiscoveryRoot, root_id)
    if row is None:
        return None
    row.enabled = False
    row.archived_at = utcnow()
    return row


def candidate_count(session: Session, root_id: str) -> int:
    return session.execute(
        select(func.count()).select_from(DiscoveryCandidate)
        .where(DiscoveryCandidate.root_id == root_id)
    ).scalar_one()


def rel_path(cand: DiscoveryCandidate, root: DiscoveryRoot) -> str:
    """Path relative to the candidate's OWNING root — the stored
    first-discovering root is the stable anchor, so overlapping roots
    never re-anchor the display."""
    try:
        return str(PurePath(cand.resolved_path)
                   .relative_to(root.canonical_path))
    except ValueError:
        return cand.name  # defensive: containment is enforced at scan


def list_candidates(session: Session, *, root_id: str | None = None,
                    presence: str | None = None,
                    limit: int = 200, offset: int = 0
                    ) -> list[tuple[DiscoveryCandidate, DiscoveryRoot]]:
    q = (select(DiscoveryCandidate, DiscoveryRoot)
         .join(DiscoveryRoot,
               DiscoveryCandidate.root_id == DiscoveryRoot.root_id)
         .order_by(DiscoveryCandidate.last_seen_at.desc()))
    if root_id:
        q = q.where(DiscoveryCandidate.root_id == root_id)
    if presence in ("present", "absent"):
        q = q.where(DiscoveryCandidate.presence == presence)
    return session.execute(
        q.limit(max(1, min(limit, 1000))).offset(max(0, offset))
    ).all()


# ------------------------------------------------------------- scanning

def _sniff(path: Path) -> str | None:
    """Tier-1 identification: ≤64-byte magic read, no retries. Returns
    'safetensors'/'gguf' or None (unverified — never a claim of
    validity; real inspection happens only in the U-2 import job)."""
    try:
        with open(path, "rb") as fh:
            fmt = model_import.sniff_format(fh.read(_SNIFF_BYTES))
    except OSError:
        return None
    return fmt if fmt != "unknown" else None


def _depth_of(parent: Path, root: Path) -> int | None:
    """Component-aware containment: parent dir's depth inside root, or
    None when outside (never raw string-prefix matching)."""
    try:
        rel = parent.relative_to(root)
    except ValueError:
        return None
    return len(rel.parts)


def managed_exclusions(session: Session, settings) -> frozenset[str]:
    """App-owned storage that must never surface as 'discovered user
    files': models_root/imports (canonical managed copies) and
    models_root/.staging (in-flight publishes)."""
    from app.domain import model_files
    try:
        mroot = model_files.models_root(session, settings)
    except Exception:   # noqa: BLE001 — a models_root failure must
        return frozenset()  #   never block scanning
    return frozenset({_norm(mroot.resolve() / "imports"),
                      _norm(mroot.resolve() / ".staging")})


def scan_root_fs(root: DiscoveryRoot, *, cancel_check,
                 deadline: float,
                 exclude: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Phase A for one root — pure filesystem walk, no DB writes.

    Returns {outcome, code, entries, skipped, candidates: [...]}. The
    caller commits candidates + outcome in one transaction, so a
    mid-root cancel/crash writes nothing for this root."""
    rootp = Path(root.canonical_path)
    max_depth = max(0, min(int(root.max_depth), MAX_DEPTH_CAP))
    out: dict[str, Any] = {"outcome": "complete", "code": None,
                           "entries": 0, "skipped": 0, "candidates": []}
    if _is_reparse(rootp) or not rootp.is_dir():
        out["outcome"], out["code"] = "refused", "path_not_found"
        return out
    stack: list[tuple[Path, int]] = [(rootp, 0)]
    # Cycle guard keyed on normalized resolved paths — st_ino is 0 on
    # some Windows filesystems, which would silently dedupe every dir.
    visited: set[str] = {_norm(rootp)}
    while stack:
        cancel_check()
        if time.monotonic() > deadline:
            out["outcome"], out["code"] = "truncated", "time_budget"
            break
        dirp, depth = stack.pop()
        if depth > 0:
            if _norm(dirp) in exclude:
                continue  # app-owned managed storage — policy skip,
                          # not a traversal failure
            if _is_reparse(dirp):
                continue  # link/junction — policy skip
        try:
            it = os.scandir(dirp)
        except OSError:
            out["skipped"] += 1   # unreadable dir → root is truncated
            continue
        with it:
            for entry in it:
                out["entries"] += 1
                if out["entries"] > MAX_ENTRIES_PER_ROOT:
                    out["outcome"], out["code"] = "truncated", "entries"
                    break
                if out["entries"] % CANCEL_CHECK_EVERY == 0:
                    cancel_check()
                try:
                    if entry.is_symlink() or entry.is_junction():
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if getattr(st, "st_file_attributes", 0) & _REPARSE_ATTR:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        try:
                            key = _norm(Path(entry.path).resolve())
                        except OSError:
                            out["skipped"] += 1
                            continue
                        if key not in visited and depth + 1 <= max_depth:
                            visited.add(key)
                            stack.append((Path(entry.path), depth + 1))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    out["skipped"] += 1
                    continue
                if not entry.name.lower().endswith(tuple(_MODEL_EXTS)):
                    continue
                try:
                    rp = Path(entry.path).resolve()
                except OSError:
                    out["skipped"] += 1
                    continue
                d = _depth_of(rp.parent, rootp)
                if d is None or d > max_depth:
                    continue  # outside containment or beyond depth bound
                fmt = _sniff(rp)
                if fmt is None:
                    out["skipped"] += 1
                    continue
                if len(out["candidates"]) >= MAX_CANDIDATES_PER_ROOT:
                    out["outcome"], out["code"] = "truncated", "candidates"
                    break
                out["candidates"].append({
                    "resolved_path": str(rp), "name": rp.name,
                    "fmt_guess": fmt, "byte_size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                })
            if out["outcome"] != "complete":
                break
    if out["skipped"] and out["outcome"] == "complete":
        # Any unreadable directory/file denies 'complete' — absence
        # proof requires a literally clean traversal.
        out["outcome"], out["code"] = "truncated", "unreadable"
    return out


def commit_root_scan(session: Session, root: DiscoveryRoot, job_id: str,
                     scan: dict[str, Any], ts) -> int:
    """Commit one root's positive observations + outcome atomically.

    Observation idempotency (F1): a candidate is counted once per JOB —
    `last_scan_job_id == job_id` means this job already recorded it
    (retry re-enumeration or nested-root re-observation) and the write
    is a no-op."""
    for c in scan["candidates"]:
        cid = candidate_id_for(c["resolved_path"])
        cand = session.get(DiscoveryCandidate, cid)
        if cand is None:
            session.add(DiscoveryCandidate(
                candidate_id=cid, resolved_path=c["resolved_path"],
                name=c["name"], fmt_guess=c["fmt_guess"],
                byte_size=c["byte_size"], mtime_ns=c["mtime_ns"],
                root_id=root.root_id, presence="present", seen_count=1,
                first_seen_at=ts, last_seen_at=ts,
                last_scan_job_id=job_id))
        elif cand.last_scan_job_id != job_id:
            cand.presence = "present"
            cand.seen_count += 1
            cand.last_seen_at = ts
            cand.last_scan_job_id = job_id
            cand.byte_size = c["byte_size"]
            cand.mtime_ns = c["mtime_ns"]
            cand.fmt_guess = c["fmt_guess"]
    root.last_scan_job_id = job_id
    root.last_scan_outcome = scan["outcome"]
    root.last_code = scan["code"]
    root.last_scan_at = ts
    root.last_entries_seen = scan["entries"]
    root.last_candidates = len(scan["candidates"])
    root.last_detail_json = {"skipped": scan["skipped"]}
    return len(scan["candidates"])


def record_unscanned_roots(session: Session, job_id: str,
                           root_ids: list[str], outcome: str,
                           code: str, ts) -> None:
    """Roots the scan never reached (time budget) get an honest outcome
    so they can never contribute absence evidence for this job."""
    for rid in root_ids:
        root = session.get(DiscoveryRoot, rid)
        if root is not None:
            root.last_scan_job_id = job_id
            root.last_scan_outcome = outcome
            root.last_code = code
            root.last_scan_at = ts
            root.last_entries_seen = 0
            root.last_candidates = 0
            root.last_detail_json = {"skipped": 0}


# ------------------------------------------------- absence reconciliation

def reconcile_absent(session: Session, job_id: str, *, cancel_check,
                     ts) -> int:
    """Phase C — mark `absent` only what this job can prove missing.

    A candidate is marked absent iff it was NOT observed this job AND
    at least one ENABLED root containing it completed cleanly THIS JOB
    with its parent dir inside the depth bound. Truncated/refused/
    unvisited roots provide no evidence; overlapping roots never cause
    false absence because containment is component-aware and any single
    complete covering root is sufficient proof."""
    roots = enabled_roots(session)   # stable snapshot inside this tx
    marked = 0
    candidates = session.execute(select(DiscoveryCandidate)).scalars().all()
    for i, cand in enumerate(candidates):
        if i % RECONCILE_CHECK_EVERY == 0:
            cancel_check()
        if cand.last_scan_job_id == job_id or cand.presence == "absent":
            continue               # positively observed / already absent
        parent = PurePath(cand.resolved_path).parent
        for r in roots:
            if (r.last_scan_job_id != job_id
                    or r.last_scan_outcome != "complete"):
                continue           # no absence evidence from this root
            d = _depth_of(parent, Path(r.canonical_path))
            if d is not None and d <= r.max_depth:
                cand.presence = "absent"
                cand.meta_json = {**(cand.meta_json or {}),
                                  "absent_scan_job_id": job_id}
                marked += 1
                break
    return marked


# --------------------------------------------------- U-2 import linkage

def link_candidate(session: Session, source_uri: str,
                   artifact_id: str) -> str:
    """Link a candidate to the artifact its own import produced.

    Called inside a savepoint by the import adapter — a miss is a
    no-op (hand-entered paths), idempotent on the same artifact, and a
    mismatch raises LinkageConflict rather than silently re-pointing
    the link."""
    cand = session.execute(
        select(DiscoveryCandidate)
        .where(DiscoveryCandidate.candidate_id
               == candidate_id_for(source_uri))
        .with_for_update()
    ).scalar_one_or_none()
    if cand is None:
        return "none"
    if cand.linked_artifact_id is None:
        cand.linked_artifact_id = artifact_id
        cand.linked_at = utcnow()
        cand.presence = "present"   # just inspected — ground truth
        return "linked"
    if cand.linked_artifact_id == artifact_id:
        return "idempotent"
    raise LinkageConflict(
        f"candidate already linked to {cand.linked_artifact_id}")
