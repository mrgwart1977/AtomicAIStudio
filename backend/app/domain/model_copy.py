"""U-2b-2a — managed-copy pipeline: stage → verify → os.link publish.

Security contract (approved final design):

  - Source files are NEVER written, moved, or deleted. The pipeline opens
    the source read-only and re-binds its fstat identity before and after
    copying — mutation surfaces as path_changed, never a silent drift.
  - Canonical destinations NEVER receive direct writes. All bytes land in
    a unique per-attempt staging file; the canonical name only changes
    via os.link() — atomic, create-only, no-overwrite. os.replace is
    forbidden (on Windows it maps to MoveFileEx(REPLACE_EXISTING)).
  - Filesystems that cannot os.link refuse managed_copy_unsupported —
    there is no degraded canonical-write fallback.
  - An existing canonical destination is adopted ONLY after a full
    content re-hash equals the inspected sha256 — the filename is a
    claim, never proof. Mismatched content refuses managed_dest_corrupt;
    nothing is ever overwritten.
  - Staging ownership is established by exclusive creation + recorded
    fstat identity, never by filename or sidecar. Cleanup deletes only
    this-attempt objects whose live identity still matches; ambiguous or
    replaced objects are left untouched.
  - Physical deletion of canonical managed files does not exist in this
    milestone — release is metadata-only.
  - Exclusive ownership of the models root is required before mutation:
    the same OS byte-range lock used for the data dir (instance_lock)
    is held on a custom models root for the process lifetime.
  - ops-lane serialization (one ops job at a time, in-process) plus the
    os.link collision contract order all writers; the residual
    check→open ancestor TOCTOU is narrowed, not eliminated — Issue #178
    remains open.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.db.models import utcnow
from app.domain import model_files, model_import
from app.domain.instance_lock import (
    DataDirLock,
    DataDirLockError,
    acquire_data_dir_lock,
)
from app.domain.model_import import ImportRefused

STAGING_DIR_NAME = ".staging"
_CHUNK = 1024 * 1024            # cancel granularity for copy streams
_DISK_SLACK = 1024 * 1024       # headroom required beyond byte_size
_MAX_STAGE_ATTEMPTS = 4         # nonce retries on staging name collision

# Failure classes meaning "this filesystem cannot hard-link". Everything
# else is io_error — capability refusals never fall back to direct writes.
_LINK_UNSUPPORTED_ERRNOS = {
    errno.EPERM, errno.EINVAL, errno.ENOSYS, errno.EXDEV,
    getattr(errno, "EOPNOTSUPP", errno.EPERM),
    getattr(errno, "ENOTSUP", errno.EPERM),
}
# winerror: ERROR_INVALID_FUNCTION, ERROR_NOT_SUPPORTED,
# ERROR_INVALID_PARAMETER, ERROR_CALL_NOT_IMPLEMENTED,
# ERROR_NOT_SUPPORTED_REPARSE.
_LINK_UNSUPPORTED_WINERRORS = {1, 50, 87, 120, 4390}


def _new_hasher():
    """Injection point for failure tests — the copy-stream hasher."""
    return hashlib.sha256()


# ------------------------------------------------------------------ layout

def imports_root(models_root: Path) -> Path:
    """<models_root>/imports — canonical managed-import storage, sibling
    of catalog <model_key>/ dirs so catalog sync/cleanup can never see it."""
    return Path(models_root) / "imports"


def staging_dir(imports: Path) -> Path:
    return imports / STAGING_DIR_NAME


def canonical_dest(models_root: Path, sha256: str, fmt: str) -> Path:
    return imports_root(models_root) / f"{sha256}.{fmt}"


# ------------------------------------------------------- models-root lock

# Resolved models-root → held lock handle. Populated lazily on first
# managed mutation and released at shutdown via release_managed_locks();
# the OS also releases on process death, so no stale state can persist.
_MANAGED_LOCKS: dict[Path, DataDirLock] = {}


def ensure_models_root_lock(session, settings) -> Path:
    """Exclusive OS ownership of the resolved models root.

    The data-dir lock held since startup already covers the default
    <data_dir>/models root. A custom models_dir (possibly another volume)
    gets its own held lock — same mechanism, same crash semantics.
    Returns the resolved root; refusal codes are sanitized.
    """
    root = model_files.models_root(session, settings)
    try:
        resolved = root.resolve()
    except OSError:
        raise ImportRefused(
            "io_error", "managed storage root does not resolve") from None
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is not None and Path(data_dir).resolve() == resolved:
        return resolved          # already exclusively owned at startup
    if resolved in _MANAGED_LOCKS:
        return resolved
    try:
        lock = acquire_data_dir_lock(resolved)
    except DataDirLockError:
        raise ImportRefused(
            "managed_copy_unsupported",
            "managed storage is owned by another instance") from None
    except OSError:
        raise ImportRefused(
            "io_error", "managed storage root is not accessible") from None
    _MANAGED_LOCKS[resolved] = lock
    return resolved


def release_managed_locks() -> None:
    """Release lazily-acquired models-root locks at app shutdown."""
    for lock in _MANAGED_LOCKS.values():
        lock.release()
    _MANAGED_LOCKS.clear()


# ------------------------------------------------------- destination safety

def _chain_has_reparse(p: Path, *, file_is_dir: bool) -> bool:
    """Reparse walk: the path itself plus every ancestor. Mirrors
    validate_source_path's lexical walk — missing components count as
    clean (they can't be links yet)."""
    if model_import._is_reparse(p, is_dir=file_is_dir):
        return True
    return any(model_import._is_reparse(a, is_dir=True) for a in p.parents)


def validate_dest_path(imports: Path, dest: Path) -> Path:
    """Containment + reparse validation for a managed write target.

    Lexical walk first (what mkdir/open will traverse), then resolved
    walk (what the filesystem actually reaches). `imports` need not
    exist yet — non-strict resolve handles missing leaves. Returns the
    resolved destination path.
    """
    from app.domain.download_security import EgressRefused, ensure_contained

    try:
        ensure_contained(imports, dest)
    except EgressRefused:
        raise ImportRefused(
            "managed_target_escape",
            "destination escapes the managed storage root") from None
    if _chain_has_reparse(dest, file_is_dir=False):
        raise ImportRefused(
            "managed_target_escape",
            "a destination component is a link or reparse point")
    try:
        rd = dest.resolve(strict=False)
    except (OSError, RuntimeError):
        raise ImportRefused(
            "io_error", "destination does not resolve") from None
    if _chain_has_reparse(rd, file_is_dir=False):
        raise ImportRefused(
            "managed_target_escape",
            "a resolved destination component is a link or reparse point")
    return rd


def _link_unsupported(exc: OSError) -> bool:
    """Classify an os.link failure: capability refusal vs ordinary I/O."""
    if isinstance(exc, PermissionError):
        return True          # fail-closed — never degrade to direct writes
    if exc.errno in _LINK_UNSUPPORTED_ERRNOS:
        return True
    return getattr(exc, "winerror", None) in _LINK_UNSUPPORTED_WINERRORS


def _verify_existing_dest(dest: Path, expected_sha: str,
                          cancel_check: Callable[[], None] | None = None) -> str:
    """Full-content hash of an existing canonical destination.

    The canonical filename is only a claim — adoption requires the live
    bytes to hash to the inspected sha256. Reparse points refuse, non-
    regular objects and content mismatches refuse managed_dest_corrupt.
    """
    if dest.is_symlink() or model_import._is_reparse(dest, is_dir=False):
        raise ImportRefused(
            "managed_target_escape",
            "existing destination is a link or reparse point")
    try:
        fh = open(dest, "rb")  # noqa: SIM115 — manual try/finally close
        # keeps every read-path OSError mapped to a sanitized code.
    except OSError:
        raise ImportRefused(
            "managed_dest_corrupt",
            "existing destination cannot be read") from None
    try:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ImportRefused(
                "managed_dest_corrupt",
                "existing destination is not a regular file")
        h = _new_hasher()
        while True:
            if cancel_check is not None:
                cancel_check()
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    except OSError:
        raise ImportRefused(
            "io_error", "could not read the existing destination") from None
    finally:
        fh.close()
    if h.hexdigest() != expected_sha:
        raise ImportRefused(
            "managed_dest_corrupt",
            "existing destination content does not match the source")
    return expected_sha


def _fsync_dir(path: Path) -> None:
    """Best-effort directory fsync for namespace durability. POSIX only —
    on Windows os.open() refuses directories (the NTFS journal plus the
    file fsync is the documented bound); any failure is harmless here
    because adoption always content-verifies before trusting dest."""
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


# ------------------------------------------------------------- staging

def _create_staging(stage_d: Path, job_id: str, sha256: str, byte_size: int,
                    attempt_no: int) -> tuple[Path, Path, Any, dict, dict]:
    """Exclusive per-attempt staging file + sidecar.

    Returns (staging_path, sidecar_path, open_write_handle,
             staging_identity, sidecar_identity). The nonce makes every
    attempt's name unique — retries never collide, so a leftover file
    from a dead attempt is always foreign to this run and never deleted.
    """
    for _ in range(_MAX_STAGE_ATTEMPTS):
        nonce = secrets.token_hex(4)
        name = f"{job_id}-{nonce}.part"
        stage = stage_d / name
        try:
            fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue                     # nonce collision — fresh nonce
        except OSError:
            raise ImportRefused(
                "io_error", "could not create a staging file") from None
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise ImportRefused(
                "managed_copy_unsupported",
                "staging object is not a regular file")
        # Ownership identity = durable object identity only. ctime/mtime
        # are excluded by design: on Windows, st_ctime_ns legitimately
        # changes on our own writes and on os.link() — only {ino, dev}
        # prove "the same filesystem object we created this run".
        stage_ident = {"ino": st.st_ino, "dev": st.st_dev}
        side = stage_d / f"{name}.json"
        try:
            sfd = os.open(side, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            os.close(fd)
            _unlink_if_owned(stage, stage_ident)
            raise ImportRefused(
                "io_error", "could not create the staging sidecar") from None
        sst = os.fstat(sfd)
        side_ident = {"ino": sst.st_ino, "dev": sst.st_dev}
        try:
            with os.fdopen(sfd, "w") as fh:
                json.dump({"job_id": job_id, "nonce": nonce,
                           "attempt_no": attempt_no,
                           "sha256": sha256, "byte_size": byte_size,
                           "created_at": utcnow().isoformat()}, fh)
        except OSError:
            _unlink_if_owned(stage, stage_ident)
            _unlink_if_owned(side, side_ident)
            raise ImportRefused(
                "io_error", "could not write the staging sidecar") from None
        try:
            fh_out = os.fdopen(fd, "wb")
        except OSError:
            os.close(fd)
            _unlink_if_owned(stage, stage_ident)
            _unlink_if_owned(side, side_ident)
            raise ImportRefused(
                "io_error", "could not open the staging file") from None
        return stage, side, fh_out, stage_ident, side_ident
    raise ImportRefused(
        "io_error", "could not allocate a staging filename")


def _unlink_if_owned(path: Path, identity: dict | None) -> bool:
    """Delete `path` ONLY when its live fstat identity still matches the
    identity recorded at exclusive creation time.

    A filename or sidecar never authorizes deletion — the object must
    prove it is the same filesystem object we created this run. Ambiguous,
    replaced, or unverifiable objects are left untouched (fail-closed).
    """
    if not identity:
        return False
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        st = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(st.st_mode):
        return False
    if (st.st_ino != identity.get("ino")
            or st.st_dev != identity.get("dev")):
        return False
    try:
        if path.is_symlink():
            return False
        path.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- pipeline

def managed_copy(session, settings, *, job_id: str, src_rp: Path,
                 report, attempt_no: int = 1,
                 cancel_check: Callable[[], None] | None = None,
                 progress: Callable[[int, int], None] | None = None) -> dict:
    """Stage → verify → publish-or-adopt one inspected file.

    `report` is the ImportReport produced by inspect_file on the same
    source path — its registration_stat re-binds the copy handle to the
    inspected object, and sha256/byte_size/fmt define the canonical
    destination. Returns the sanitized copy-provenance dict (dest_abs is
    consumed by register_import for the artifact uri; dest_rel is the
    only path component stored in import_report_json).

    Raises ImportRefused (sanitized codes) or JobCancelled via
    cancel_check. Staging objects created by this run are cleaned up on
    every exit path; foreign or ambiguous objects are left untouched.
    """
    fmt = report.fmt
    if fmt not in ("safetensors", "gguf"):
        raise ImportRefused(
            "unsupported_format", "only safetensors and gguf are managed")
    sha = report.sha256
    root = ensure_models_root_lock(session, settings)
    imports = imports_root(root)
    stage_d = staging_dir(imports)
    # Lexical reparse walk on every managed dir BEFORE mkdir — a planted
    # junction at models/imports/.staging must not redirect the writes.
    for d in (imports, stage_d):
        if model_import._is_reparse(d, is_dir=True):
            raise ImportRefused(
                "managed_target_escape",
                "a managed storage directory is a link or reparse point")
    for anc in imports.parents:
        if model_import._is_reparse(anc, is_dir=True):
            raise ImportRefused(
                "managed_target_escape",
                "a managed storage ancestor is a link or reparse point")
    try:
        imports.mkdir(parents=True, exist_ok=True)
        stage_d.mkdir(exist_ok=True)
    except OSError:
        raise ImportRefused(
            "io_error", "could not create managed storage") from None
    dest = canonical_dest(root, sha, fmt)
    dest = validate_dest_path(imports, dest)

    stage = side = None
    stage_fh = None
    stage_ident = side_ident = None
    staging_left = False
    done = False
    try:
        if dest.is_symlink() or model_import._is_reparse(dest, is_dir=False):
            raise ImportRefused(
                "managed_target_escape",
                "existing destination is a link or reparse point")
        if dest.exists():
            # Adoption path — content-verify, never write, never overwrite.
            _verify_existing_dest(dest, sha, cancel_check)
            return {"dest_rel": dest.name, "dest_abs": str(dest),
                    "expected_sha256": sha, "verified_sha256": sha,
                    "bytes_written": 0, "provenance": "adopted",
                    "published_at": None, "staging_left": False}

        # Disk preflight — authoritative byte_size from the inspect fstat,
        # not request input. Unmeasurable free space fails closed.
        free = model_files.disk_free_bytes(imports)
        if free is None or free < report.byte_size + _DISK_SLACK:
            raise ImportRefused(
                "disk_full", "insufficient free space for the managed copy")

        # Source identity re-bind — a second handle must prove it is the
        # SAME object the inspection hashed (check→open gap narrowed to
        # handle lifetime; residual TOCTOU under Issue #178).
        try:
            sfh = open(src_rp, "rb")  # noqa: SIM115 — manual try/finally
            # keeps every open/read OSError mapped to io_error.
        except OSError:
            raise ImportRefused(
                "io_error", "could not open the source file") from None
        try:
            st = os.fstat(sfh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise ImportRefused(
                    "path_not_file", "opened source is not a regular file")
            if model_import._stat_identity(st) != report.registration_stat:
                raise ImportRefused(
                    "path_changed",
                    "source file changed since inspection")

            stage, side, stage_fh, stage_ident, side_ident = _create_staging(
                stage_d, job_id, sha, report.byte_size, attempt_no)
            stage = validate_dest_path(imports, stage)

            h = _new_hasher()
            written = 0
            try:
                while True:
                    if cancel_check is not None:
                        cancel_check()
                    chunk = sfh.read(_CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
                    stage_fh.write(chunk)
                    written += len(chunk)
                    if progress is not None:
                        progress(written, report.byte_size)
            except OSError as exc:
                raise ImportRefused(
                    "disk_full" if exc.errno == errno.ENOSPC else "io_error",
                    "the managed copy could not be written") from None

            # Post-copy source identity — a same-size same-mtime swap is
            # caught by the content hash below; a rename/inode swap is
            # caught here. Both gates run before publication.
            if model_import._stat_identity(
                    os.fstat(sfh.fileno())) != report.registration_stat:
                raise ImportRefused(
                    "path_changed", "source file changed during copying")
        finally:
            sfh.close()

        # Byte durability BEFORE publish — the linked bytes are durable
        # at the moment the canonical name appears.
        try:
            stage_fh.flush()
            os.fsync(stage_fh.fileno())
        except OSError:
            raise ImportRefused(
                "io_error", "could not flush the managed copy") from None
        stage_fh.close()
        stage_fh = None
        if h.hexdigest() != sha or written != report.byte_size:
            raise ImportRefused(
                "copy_mismatch",
                "copied content does not match the inspected source")

        # Atomic no-overwrite publication. FileExistsError is the only
        # losing-race outcome — the winner's bytes are verified and
        # adopted; never an overwrite, never a partial canonical write.
        try:
            os.link(stage, dest)
            published = True
        except FileExistsError:
            published = False
        except OSError as exc:
            if _link_unsupported(exc):
                raise ImportRefused(
                    "managed_copy_unsupported",
                    "the managed storage filesystem does not support "
                    "hard links") from None
            raise ImportRefused(
                "io_error", "could not publish the managed copy") from None

        if published:
            provenance = "published"
            _fsync_dir(imports)
        else:
            _verify_existing_dest(dest, sha, cancel_check)
            provenance = "adopted"
        done = True
        # Own-attempt staging only — live identity must still match the
        # identity recorded at exclusive creation. Ambiguous leftovers
        # stay for the deferred sweep; they are never force-deleted.
        staging_left = not (
            _unlink_if_owned(stage, stage_ident)
            and _unlink_if_owned(side, side_ident))
        return {"dest_rel": dest.name, "dest_abs": str(dest),
                "expected_sha256": sha, "verified_sha256": sha,
                "bytes_written": written, "provenance": provenance,
                "published_at": utcnow().isoformat(),
                "staging_left": staging_left}
    finally:
        if stage_fh is not None:
            try:
                stage_fh.close()
            except OSError:
                pass
        if not done:
            # Failure/cancel path — remove only this attempt's verified
            # objects; anything ambiguous stays for the deferred sweep.
            if stage is not None:
                _unlink_if_owned(stage, stage_ident)
            if side is not None:
                _unlink_if_owned(side, side_ident)
