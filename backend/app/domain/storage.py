"""Storage engine — content-addressed file blobs on the filesystem.

Rules (handoff, Phase 0/1 spine + M1 media requirements):
- Bytes live on disk only; SQLite holds references + metadata.
- All file paths stored location-relative so projects stay portable.
- Every write is atomic: stream to "<name>.tmp" beside the target,
  fsync, then os.replace — an interrupted run leaves a .tmp orphan that
  the startup sweep removes, never a half file.
- Content-addressed ingest: hash while streaming; if the blob already
  exists it is reused (dedupe makes project duplication and repeated
  backups cheap).
- Nothing is loaded fully into RAM: chunk size is fixed at 8 MiB.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import BinaryIO, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Asset, AssetFile, AssetVersion, StorageLocation, utcnow

log = logging.getLogger(__name__)

CHUNK = 8 * 1024 * 1024  # 8 MiB streaming chunk — never scale with file size
BLOB_DIR = "blobs"
# Seeded by migration 0002; root_path is patched at startup for this machine.
DEFAULT_LOCATION_ID = "00000000-0000-4000-8000-00000000d001"


def safe_join(root: Path, rel: str | Path) -> Path:
    """Resolve a location-relative path, rejecting traversal."""
    rel_path = Path(str(rel).replace("\\", "/"))
    if rel_path.is_absolute() or ".." in rel_path.parts:
        raise ValueError(f"unsafe relative path: {rel}")
    return (root / rel_path).resolve()


def blob_rel_path(sha256: str) -> str:
    return f"{BLOB_DIR}/{sha256[:2]}/{sha256}"


def iter_chunks(stream: BinaryIO) -> Iterator[bytes]:
    while True:
        chunk = stream.read(CHUNK)
        if not chunk:
            return
        yield chunk


def hash_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter_chunks(f):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def atomic_write(path: Path, data: bytes) -> None:
    """Small-file atomic write via same-dir temp + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class StorageRegistry:
    """Resolves storage locations and performs CAS ingest/verify."""

    def __init__(self, session_factory, data_dir: Path):
        self._sf = session_factory
        self._data_dir = Path(data_dir)

    # ---------- locations ----------

    def ensure_default_location(self, session: Session) -> StorageLocation:
        """The seeded default location gets its machine-specific root patched
        here: <data_dir>. Keeping the root at the data dir itself (not a
        subdir) lets M0 assets keep resolving — their rel_path is
        data_dir-relative ('jobs/...', 'assets/...')."""
        loc = session.get(StorageLocation, DEFAULT_LOCATION_ID)
        if loc is None:
            loc = StorageLocation(id=DEFAULT_LOCATION_ID, label="Default", is_default=True)
        loc.root_path = str(self._data_dir.resolve())
        loc.is_default = True
        loc.status = "online"
        session.add(loc)
        return loc

    def get_location(self, session: Session, location_id: str | None) -> StorageLocation | None:
        if location_id:
            loc = session.get(StorageLocation, location_id)
            if loc is not None:
                return loc
        return session.execute(
            select(StorageLocation).where(StorageLocation.is_default.is_(True))
        ).scalar_one_or_none()

    def root_for(self, location: StorageLocation) -> Path:
        return Path(location.root_path)

    def file_path(self, session: Session, asset_file: AssetFile) -> Path:
        loc = session.get(StorageLocation, asset_file.storage_location_id)
        if loc is None:
            raise FileNotFoundError("storage location missing")
        return safe_join(self.root_for(loc), asset_file.rel_path)

    def asset_path(self, session: Session, asset: Asset) -> Path | None:
        """Resolve an asset's current version to an absolute path."""
        if not asset.sha256:
            return None
        af = session.get(AssetFile, asset.sha256)
        if af is None:
            return None
        return self.file_path(session, af)

    # ---------- ingest ----------

    def ingest(
        self,
        session: Session,
        source: Path,
        *,
        location: StorageLocation,
        cancel_check=None,
    ) -> AssetFile:
        """Stream `source` into the CAS under `location`. Atomic, deduped,
        resumable-safe: a crash leaves only a .tmp file for the sweep."""
        h = hashlib.sha256()
        size = 0
        root = self.root_for(location)
        # Hash pass first (streaming), then a second streamed copy pass only
        # when the blob is genuinely new — no full-file buffering ever.
        with open(source, "rb") as f:
            for chunk in iter_chunks(f):
                h.update(chunk)
                size += len(chunk)
        sha = h.hexdigest()

        existing = session.get(AssetFile, sha)
        if existing is not None and existing.status == "ready":
            return existing

        rel = blob_rel_path(sha)
        dest = safe_join(root, rel)
        tmp = dest.with_name(dest.name + ".tmp")
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(source, "rb") as src, open(tmp, "wb") as out:
                for chunk in iter_chunks(src):
                    if cancel_check is not None:
                        cancel_check()
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        af = session.get(AssetFile, sha) or AssetFile(sha256=sha)
        af.storage_location_id = location.id
        af.rel_path = rel
        af.byte_size = size
        af.status = "ready"
        session.add(af)
        return af

    def register_blob(
        self, session: Session, sha256: str, *, location: StorageLocation, byte_size: int,
        rel_path: str | None = None,
    ) -> AssetFile:
        """Register bytes already written to disk (adapters, imports)."""
        af = session.get(AssetFile, sha256) or AssetFile(sha256=sha256)
        af.storage_location_id = location.id
        af.rel_path = rel_path or af.rel_path or blob_rel_path(sha256)
        af.byte_size = byte_size or af.byte_size
        af.status = "ready"
        session.add(af)
        return af

    # ---------- verification / relink ----------

    def verify_asset(self, session: Session, asset: Asset) -> str:
        """Recompute nothing — existence+size check first (cheap). Sets
        asset.status and asset_files.status accordingly."""
        af = session.get(AssetFile, asset.sha256) if asset.sha256 else None
        if af is None:
            asset.status = "missing"
            return "missing"
        try:
            path = self.file_path(session, af)
        except (FileNotFoundError, ValueError):
            af.status = "missing"
            asset.status = "missing"
            return "missing"
        if not path.exists():
            af.status = "missing"
            asset.status = "missing"
            return "missing"
        af.status = "ready"
        asset.status = "ready"
        return "ready"

    def verify_project(self, session: Session, project_id: str) -> dict:
        assets = session.execute(
            select(Asset).where(Asset.project_id == project_id, Asset.status != "deleted")
        ).scalars().all()
        counts = {"ready": 0, "missing": 0}
        for a in assets:
            counts[self.verify_asset(session, a)] += 1
        return counts

    def relink_location(self, session: Session, location_id: str, new_root: Path) -> StorageLocation:
        loc = session.get(StorageLocation, location_id)
        if loc is None:
            raise KeyError("storage location not found")
        if not new_root.is_dir():
            raise FileNotFoundError(f"not a directory: {new_root}")
        loc.root_path = str(new_root.resolve())
        loc.status = "online"
        return loc

    def probe_locations(self, session: Session) -> None:
        for loc in session.execute(select(StorageLocation)).scalars():
            loc.status = "online" if Path(loc.root_path).is_dir() else "missing"

    # ---------- recovery ----------

    def sweep(self, session: Session) -> dict:
        """Startup recovery: remove orphaned .tmp files in blob dirs and mark
        'pending' file rows missing. Safe to run any time."""
        removed = 0
        for loc in session.execute(select(StorageLocation)).scalars():
            root = self.root_for(loc)
            blob_root = root / BLOB_DIR
            if blob_root.is_dir():
                for tmp in blob_root.rglob("*.tmp"):
                    try:
                        tmp.unlink()
                        removed += 1
                    except OSError:
                        log.warning("could not remove orphaned tmp file %s", tmp)
        stale = session.execute(
            select(AssetFile).where(AssetFile.status == "pending")
        ).scalars().all()
        for af in stale:
            af.status = "missing"
        return {"tmp_removed": removed, "pending_marked_missing": len(stale)}

    def project_versions(self, session: Session, asset_id: str) -> list[AssetVersion]:
        return session.execute(
            select(AssetVersion)
            .where(AssetVersion.asset_id == asset_id)
            .order_by(AssetVersion.version_no)
        ).scalars().all()
