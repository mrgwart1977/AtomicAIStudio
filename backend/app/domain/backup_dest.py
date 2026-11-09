"""Backup destination resolution, validation, and sizing.

Resolution order (never a SILENT fallback — an unreachable configured
destination is an error the user must act on, not a reason to quietly
write somewhere else):

1. ``project.backup_dir``   — per-project override, if set
2. ``app_settings["backup_dir"]`` — app-wide default override
3. ``settings.backups_dir`` — the built-in app-data default (always the
   compatibility path for pre-M1.1 backups)

Validation reports structured problems so the UI can show beginner-
friendly causes: missing path, not a directory, not writable,
insufficient free space, and (best-effort) unreachable drive/network
roots. Everything is probed live at validation/commit time — nothing is
cached.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AppSetting, AssetFile, AssetVersion, Asset, Project

SETTING_KEY = "backup_dir"
# Manifest + zip container slack added to raw blob bytes when estimating.
ESTIMATE_OVERHEAD = 16 * 1024 * 1024


class DestinationError(Exception):
    """Beginner-readable destination failure (shown verbatim in the UI)."""


# ---------- resolution ----------

def _app_default(session: Session) -> str | None:
    row = session.get(AppSetting, SETTING_KEY)
    if row is None:
        return None
    return (row.value_json or {}).get("dir") or None


def set_app_default(session: Session, path: str | None) -> None:
    row = session.get(AppSetting, SETTING_KEY)
    if path is None:
        if row is not None:
            session.delete(row)
        return
    if row is None:
        row = AppSetting(key=SETTING_KEY, value_json={})
        session.add(row)
    row.value_json = {"dir": path}


def resolve_dir(session: Session, settings, project: Project | None) -> Path:
    """Effective backup directory for a project (or app default when
    project is None). Always returns a Path; validity is separate."""
    if project is not None and project.backup_dir:
        return Path(project.backup_dir)
    app_dir = _app_default(session)
    if app_dir:
        return Path(app_dir)
    return Path(settings.backups_dir) / (project.id if project else "shared")


def describe_source(session: Session, settings, project: Project | None) -> str:
    """Where the effective dir came from — for honest UI labeling."""
    if project is not None and project.backup_dir:
        return "project override"
    if _app_default(session):
        return "app setting"
    return "built-in default"


# ---------- sizing ----------

def estimate_package_bytes(session: Session, project_id: str) -> int:
    """Bytes the export will stream: unique ready blobs + overhead.
    Deduplicated on sha256 like the package itself."""
    rows = session.execute(
        select(AssetFile.byte_size)
        .join(AssetVersion, AssetVersion.sha256 == AssetFile.sha256)
        .join(Asset, AssetVersion.asset_id == Asset.id)
        .where(Asset.project_id == project_id, Asset.status != "deleted",
               AssetFile.status == "ready")
    ).all()
    seen = set()
    total = 0
    for (size,) in rows:
        total += size
    # Deflate typically shrinks; estimate conservatively (worst-case near-raw).
    return total + ESTIMATE_OVERHEAD


# ---------- validation ----------

def _writable_probe(dir_path: Path) -> tuple[bool, str | None]:
    """Try a real small file create/fsync/delete in the dir."""
    try:
        dir_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"Cannot create the folder: {exc.strerror or exc}"
    try:
        fd, tmp = tempfile.mkstemp(dir=dir_path, prefix=".atomic-probe-")
        try:
            os.write(fd, b"ok")
            os.fsync(fd)
        finally:
            os.close(fd)
            Path(tmp).unlink(missing_ok=True)
        return True, None
    except PermissionError:
        return False, "Permission denied — the folder is not writable for this account"
    except OSError as exc:
        return False, f"Folder is not writable: {exc.strerror or exc}"


# Single-file size ceilings by filesystem — a 40 GB package physically
# cannot land on a FAT32 stick. Probed via psutil when available.
_FAT32_LIMIT = 4 * 1024 ** 3 - 1


def _fstype_of(path: Path) -> str | None:
    """Filesystem type of the mount containing `path` (longest prefix match)."""
    try:
        import psutil
        best: tuple[int, str] | None = None
        drive = str(path).lower()
        for part in psutil.disk_partitions(all=False):
            mp = part.mountpoint.lower()
            if drive.startswith(mp) and (best is None or len(mp) > best[0]):
                best = (len(mp), part.fstype.upper())
        return best[1] if best else None
    except Exception:
        return None


def _root_reachable(path: Path) -> tuple[bool, str | None]:
    """Detect a disconnected drive / dead network share. On Windows a
    missing drive letter or dead UNC root fails `exists()` already —
    this adds a clearer message for roots that don't exist at all."""
    try:
        # Path.drive: 'C:' or '\\\\server\\share' or ''
        drive = path.drive
        if not drive:
            return True, None
        if drive.startswith("\\\\"):
            root = Path(drive + "\\")
            if not root.exists():
                return False, "Network share is not reachable — check the connection or reconnect the share"
            return True, None
        letter = drive.rstrip(":").rstrip("\\")
        if letter and not Path(f"{letter}:\\").exists():
            return False, f"Drive {drive} is not connected — reconnect it or pick another folder"
        return True, None
    except OSError:
        return False, "Destination is not reachable"


def validate_destination(
    path: str | Path, *, needed_bytes: int = 0, create_ok: bool = True
) -> dict:
    """Probe a candidate backup directory. Returns a structured result;
    `errors` means unusable, `warnings` means usable-but-notable."""
    p = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    free = 0
    reachable_ok, reach_err = _root_reachable(p)
    if not reachable_ok:
        errors.append(reach_err)
    else:
        if not p.exists():
            if create_ok:
                ok, err = _writable_probe(p)
                if not ok:
                    errors.append(err or "Folder cannot be created")
            else:
                errors.append("Folder does not exist")
        elif not p.is_dir():
            errors.append("That path is a file, not a folder")
        else:
            ok, err = _writable_probe(p)
            if not ok:
                errors.append(err or "Folder is not writable")
    if not errors:
        try:
            usage = shutil.disk_usage(p)
            free = usage.free
            if needed_bytes and free < needed_bytes:
                errors.append(
                    f"Not enough space — need about {_fmt_bytes(needed_bytes)}, "
                    f"only {_fmt_bytes(free)} free on this drive"
                )
            elif needed_bytes and free < needed_bytes * 2:
                warnings.append(
                    f"Low headroom — backup needs about {_fmt_bytes(needed_bytes)} "
                    f"and only {_fmt_bytes(free)} is free"
                )
        except OSError as exc:
            errors.append(f"Cannot read free space on this drive: {exc.strerror or exc}")
    if not errors:
        fstype = _fstype_of(p)
        if fstype in ("FAT32", "VFAT") and needed_bytes > _FAT32_LIMIT:
            errors.append(
                f"This drive uses FAT32, which can't hold files over "
                f"{_fmt_bytes(_FAT32_LIMIT)} — the estimated backup is "
                f"{_fmt_bytes(needed_bytes)}. Pick an NTFS or exFAT drive."
            )
        elif fstype in ("FAT32", "VFAT"):
            warnings.append(
                f"This drive uses FAT32 — files over {_fmt_bytes(_FAT32_LIMIT)} "
                "can't be saved there, so very large backups will fail later."
            )
    return {
        "path": str(p),
        "exists": p.exists(),
        "writable": not errors,
        "free_bytes": free,
        "needed_bytes": needed_bytes,
        "errors": [e for e in errors if e],
        "warnings": warnings,
    }


def require_writable_destination(path: Path, needed_bytes: int = 0) -> dict:
    """Hard-fail variant for job time — raises DestinationError with the
    first friendly message. Returns the validate dict on success."""
    result = validate_destination(path, needed_bytes=needed_bytes)
    if result["errors"]:
        raise DestinationError(result["errors"][0])
    return result


def _fmt_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    v = float(n)
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.1f} {units[i]}"


def fmt_bytes(n: int) -> str:  # public alias for routers/UI strings
    return _fmt_bytes(n)


def destination_snapshot(session: Session, settings, project: Project | None) -> dict:
    """Full picture for the Backups UI: effective dir, where it came
    from, validation, and how big this project's package will be."""
    needed = estimate_package_bytes(session, project.id) if project else 0
    target = resolve_dir(session, settings, project)
    result = validate_destination(target, needed_bytes=needed)
    return {
        "dir": str(target),
        "source": describe_source(session, settings, project),
        "app_default_dir": _app_default(session) or "",
        "builtin_dir": str(settings.backups_dir),
        "project_override": (project.backup_dir if project else "") or "",
        "estimated_bytes": needed,
        **result,
    }
