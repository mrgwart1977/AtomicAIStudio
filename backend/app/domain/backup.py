"""Project backup / export / import as .atomicpkg packages.

Format (zip):
    manifest.json   — format_version, project row, all owned entity rows,
                      prompt draft, asset+version rows, per-file sha256/size
    files/<sha256>  — deduplicated immutable blobs, one per asset_file

Guarantees:
- Export streams files in 8 MiB chunks; nothing scales with project size.
- Import validates the manifest + verifies every blob's sha256 while it
  streams into staging, then commits all DB rows in ONE transaction —
  a failed or interrupted import leaves no half-project, only a
  `<name>.partial` staging dir the startup sweep removes.
- Zip entries are whitelisted; no path traversal (extract() is never used).
- Every imported entity id is remapped through a fresh map; embedded id
  references inside JSON blobs are rewritten by walking the structure.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Asset,
    AssetFile,
    AssetVersion,
    Character,
    EntityAssetLink,
    Location,
    Project,
    PromptDraft,
    Scene,
    SceneCharacter,
    Shot,
    ShotCharacter,
    World,
    new_id,
    utcnow,
)
from app.domain.storage import iter_chunks

# v2 adds scenes/shots/cast/entity-asset sections. v1 packages still import —
# missing sections restore as empty.
FORMAT_VERSION = 2
# Iteration order is the restore order — dependencies come first
# (assets are restored separately BEFORE this loop because
# shots.thumbnail_asset_id and entity_assets.asset_id point at them).
ENTITY_TABLES = {
    "characters": Character,
    "worlds": World,
    "locations": Location,
    "scenes": Scene,
    "scene_characters": SceneCharacter,
    "shots": Shot,
    "shot_characters": ShotCharacter,
    "entity_assets": EntityAssetLink,
}


class PackageError(Exception):
    """Raised for invalid/corrupt/incomplete packages."""


class ProgressReporter:
    """Byte-level progress for a streaming file operation.

    The adapter calls `tick(n)` after every chunk; the reporter emits a
    throttled detail dict (bytes_done, bytes_total, current_file,
    rate_bps, eta_seconds, elapsed_seconds) at most every `interval`
    seconds — plus a final forced emit at 100%. ETA is only reported
    once enough bytes have flowed to make the rate meaningful."""

    def __init__(self, total_bytes: int, cb: Callable[[dict], None] | None = None,
                 interval: float = 0.25):
        self.total = max(int(total_bytes), 1)
        self.done = 0
        self.current_file = ""
        self._cb = cb
        self._interval = interval
        self._start = time.monotonic()
        self._last_emit = 0.0

    def set_file(self, name: str) -> None:
        self.current_file = name

    def snapshot(self) -> dict:
        """Current progress detail — safe to call from any thread."""
        elapsed = max(time.monotonic() - self._start, 1e-6)
        rate = self.done / elapsed
        remaining = max(self.total - self.done, 0)
        eta = remaining / rate if (rate > 0 and self.done > 1_000_000) else None
        return {
            "bytes_done": self.done,
            "bytes_total": self.total,
            "current_file": self.current_file,
            "rate_bps": round(rate),
            "eta_seconds": round(eta, 1) if eta is not None else None,
            "elapsed_seconds": round(elapsed, 1),
        }

    def tick(self, n: int = 0, *, force: bool = False) -> None:
        self.done += n
        if self._cb is None:
            return
        now = time.monotonic()
        if not force and now - self._last_emit < self._interval:
            return
        self._last_emit = now
        self._cb(self.snapshot())


# ---------- serialization ----------

def _row_dict(row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name)
        if hasattr(val, "isoformat"):
            val = val.isoformat()
        out[col.name] = val
    return out


def project_manifest(session: Session, project) -> tuple[dict, list[AssetFile]]:
    """Serialize the whole project. Returns (manifest, files-to-include)."""
    entities: dict[str, list[dict]] = {}
    for key, model in ENTITY_TABLES.items():
        entities[key] = [
            _row_dict(r)
            for r in session.execute(
                select(model).where(model.project_id == project.id)
            ).scalars()
        ]
    draft = session.execute(
        select(PromptDraft).where(PromptDraft.project_id == project.id)
    ).scalar_one_or_none()

    assets = session.execute(
        select(Asset).where(Asset.project_id == project.id, Asset.status != "deleted")
    ).scalars().all()
    versions = session.execute(
        select(AssetVersion)
        .join(Asset, AssetVersion.asset_id == Asset.id)
        .where(Asset.project_id == project.id)
    ).scalars().all()

    files: dict[str, AssetFile] = {}
    for v in versions:
        af = session.get(AssetFile, v.sha256)
        if af is not None and af.status == "ready":
            files[af.sha256] = af

    manifest = {
        "format_version": FORMAT_VERSION,
        "app": "atomic-ai-studio",
        "created_at": utcnow().isoformat(),
        "project": _row_dict(project),
        "prompt_draft": _row_dict(draft) if draft else None,
        "entities": entities,
        "assets": [_row_dict(a) for a in assets],
        "asset_versions": [_row_dict(v) for v in versions],
        "files": [
            {"sha256": af.sha256, "byte_size": af.byte_size, "arcname": f"files/{af.sha256}"}
            for af in files.values()
        ],
    }
    return manifest, list(files.values())


def export_package(
    session_factory, project_id: str, dest: Path, storage,
    progress: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
    reporter: ProgressReporter | None = None,
) -> tuple[dict, str, int]:
    """Stream a .atomicpkg to `dest` atomically (tmp → replace).
    Returns (manifest, package_sha256, byte_size).
    cancel_check() is invoked between chunks — raising aborts cleanly."""
    with session_factory() as session:
        project = session.get(Project, project_id)
        manifest, files = project_manifest(session, project)
        file_paths = {af.sha256: storage.file_path(session, af) for af in files}

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    total = sum(af.byte_size for af in files) or 1
    done = 0
    h = hashlib.sha256()
    try:
        with open(tmp, "wb") as raw:

            class _Tee:
                def write(self, b):
                    raw.write(b)
                    h.update(b)
                    return len(b)

                def flush(self):
                    raw.flush()

            with zipfile.ZipFile(_Tee(), "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                zf.writestr("manifest.json", json.dumps(manifest, indent=1, default=str))
                for af in files:
                    if cancel_check:
                        cancel_check()
                    src = file_paths[af.sha256]
                    if progress:
                        progress(done / total, f"Packing {af.sha256[:8]}…")
                    if reporter:
                        reporter.set_file(af.sha256[:12])
                    with zf.open(f"files/{af.sha256}", "w") as out, open(src, "rb") as srcf:
                        for chunk in iter_chunks(srcf):
                            if cancel_check:
                                cancel_check()
                            out.write(chunk)
                            done += len(chunk)
                            if reporter:
                                reporter.tick(len(chunk))
            raw.flush()
            try:
                os.fsync(raw.fileno())
            except OSError:
                pass
        tmp.replace(dest)
        if reporter:
            reporter.tick(0, force=True)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return manifest, h.hexdigest(), dest.stat().st_size


# ---------- import ----------

def read_manifest(pkg: Path) -> dict:
    try:
        with zipfile.ZipFile(pkg) as zf:
            if "manifest.json" not in zf.namelist():
                raise PackageError("not an Atomic package (missing manifest.json)")
            data = json.loads(zf.read("manifest.json"))
    except zipfile.BadZipFile:
        raise PackageError("not a zip package") from None
    except (KeyError, json.JSONDecodeError):
        raise PackageError("corrupt manifest.json") from None
    fv = data.get("format_version")
    if not isinstance(fv, int) or fv < 1 or fv > FORMAT_VERSION:
        raise PackageError(f"unsupported format_version {data.get('format_version')}")
    if not data.get("project"):
        raise PackageError("manifest has no project section")
    return data


def _remap_ids(obj: Any, id_map: dict[str, str]) -> Any:
    """Walk JSON-like structure; replace any string equal to a mapped id."""
    if isinstance(obj, dict):
        return {k: _remap_ids(v, id_map) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_remap_ids(v, id_map) for v in obj]
    if isinstance(obj, str):
        return id_map.get(obj, obj)
    return obj


def import_package(
    session_factory, pkg: Path, storage, *,
    project_id_override: str | None = None,
    name_suffix: str = "",
    progress: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
    reporter: ProgressReporter | None = None,
) -> dict:
    """Import a package into a NEW project (or into `project_id_override`
    for restore). Returns stats + new project id.
    cancel_check() between chunks — a cancel leaves the staging dir
    removed and no partial project committed."""
    manifest = read_manifest(pkg)
    files_meta = {f["sha256"]: f for f in manifest.get("files", [])}

    staging = pkg.parent / (pkg.name + ".staging")
    staging.mkdir(parents=True, exist_ok=True)
    try:
        extracted: dict[str, Path] = {}
        total = sum(f["byte_size"] for f in manifest.get("files", [])) or 1
        done = 0
        with zipfile.ZipFile(pkg) as zf:
            names = set(zf.namelist())
            for entry in names:
                if entry != "manifest.json" and not entry.startswith("files/"):
                    continue  # whitelist — ignore unexpected members
                if ".." in Path(entry).parts or Path(entry).is_absolute():
                    raise PackageError(f"unsafe archive member: {entry}")
            for sha, meta in files_meta.items():
                if cancel_check:
                    cancel_check()
                arc = meta["arcname"]
                if arc not in names:
                    raise PackageError(f"package is missing blob {sha[:8]}")
                out_path = staging / sha
                h = hashlib.sha256()
                size = 0
                if reporter:
                    reporter.set_file(sha[:12])
                with zf.open(arc) as src, open(out_path, "wb") as out:
                    for chunk in iter_chunks(src):
                        if cancel_check:
                            cancel_check()
                        h.update(chunk)
                        size += len(chunk)
                        out.write(chunk)
                        if reporter:
                            reporter.tick(len(chunk))
                if h.hexdigest() != sha:
                    raise PackageError(f"hash mismatch on blob {sha[:8]} (corrupt package)")
                extracted[sha] = out_path
                done += meta["byte_size"]
                if progress:
                    progress(min(done / total, 0.9), f"Verified {sha[:8]}…")

        with session_factory() as session:
            src_project = manifest["project"]
            id_map: dict[str, str] = {}

            def map_id(old: str | None) -> str | None:
                if old is None:
                    return None
                return id_map.setdefault(old, new_id())

            if project_id_override:
                new_project = session.get(Project, project_id_override)
                if new_project is None:
                    raise PackageError("target project for restore not found")
                # Fill the stub with the packaged project's fields.
                for f in ("name", "template", "kind", "aspect_ratio", "width",
                          "height", "fps_num", "fps_den", "style"):
                    setattr(new_project, f, src_project.get(f) or getattr(new_project, f))
            else:
                new_project = Project(
                    id=new_id(),
                    name=(src_project["name"] + name_suffix)[:200],
                    status="active",
                    template=src_project.get("template", "blank"),
                    kind=src_project.get("kind", "movie"),
                    aspect_ratio=src_project.get("aspect_ratio", "16:9"),
                    width=src_project.get("width", 1920),
                    height=src_project.get("height", 1080),
                    fps_num=src_project.get("fps_num", 24),
                    fps_den=src_project.get("fps_den", 1),
                    style=src_project.get("style", "none"),
                    settings_json={**(src_project.get("settings_json") or {}),
                                   "imported_from": src_project.get("id")},
                    revision=1,
                )
                session.add(new_project)
            session.flush()

            # Files → CAS FIRST (dedupe: existing shas are reused without
            # copy), then asset rows/versions — entity tables restored later
            # reference them (shots.thumbnail_asset_id, entity_assets).
            default_loc = storage.get_location(session, None)
            asset_id_map: dict[str, str] = {}
            version_id_map: dict[str, str] = {}
            for v in manifest.get("asset_versions", []):
                sha = v["sha256"]
                if sha in extracted and session.get(AssetFile, sha) is None:
                    storage.ingest(session, extracted[sha], location=default_loc)
            for a in manifest.get("assets", []):
                a = dict(a)
                old_id = a.pop("id")
                a["id"] = map_id(old_id)
                asset_id_map[old_id] = a["id"]
                a["project_id"] = new_project.id
                cur = a.pop("current_version_id", None)
                a.pop("created_at", None)
                a = _remap_ids(a, id_map)
                if not a.get("sha256") or (a["sha256"] not in extracted and session.get(AssetFile, a["sha256"]) is None):
                    a["status"] = "missing"
                session.add(Asset(**a))
                session.flush()
                versions = [v for v in manifest["asset_versions"] if v["asset_id"] == old_id]
                for v in versions:
                    v = dict(v)
                    old_vid = v.pop("id")
                    v["id"] = map_id(old_vid)
                    version_id_map[old_vid] = v["id"]
                    v["asset_id"] = a["id"]
                    v.pop("created_at", None)
                    session.add(AssetVersion(**v))
                session.flush()
                if cur and cur in version_id_map:
                    session.get(Asset, a["id"]).current_version_id = version_id_map[cur]

            # Entity rows — remap every id, then rewrite embedded refs.
            # ENTITY_TABLES order is FK-dependency order; flush per table
            # so parents exist before children insert.
            for key, model in ENTITY_TABLES.items():
                for row in manifest["entities"].get(key, []):
                    row = dict(row)
                    old_id = row.pop("id")
                    row["id"] = map_id(old_id)
                    row["project_id"] = new_project.id
                    for col in ("created_at", "updated_at"):
                        row.pop(col, None)
                    row = _remap_ids(row, id_map)
                    session.add(model(**row))
                session.flush()

            if manifest.get("prompt_draft") and not project_id_override:
                d = dict(manifest["prompt_draft"])
                session.add(PromptDraft(
                    id=new_id(), project_id=new_project.id,
                    text=d.get("text", ""), scope=d.get("scope", "this_take"),
                ))

            if cancel_check:
                cancel_check()
            session.commit()
            stats = {
                "project_id": new_project.id,
                "characters": len(manifest["entities"].get("characters", [])),
                "worlds": len(manifest["entities"].get("worlds", [])),
                "locations": len(manifest["entities"].get("locations", [])),
                "scenes": len(manifest["entities"].get("scenes", [])),
                "shots": len(manifest["entities"].get("shots", [])),
                "assets": len(manifest.get("assets", [])),
                "files_verified": len(extracted),
            }
        return stats
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _sweep_dir(base: Path) -> int:
    """Remove *.staging / *.partial leftovers directly inside `base`."""
    removed = 0
    for pattern in ("*.staging", "*.partial"):
        for p in base.glob(pattern):
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def sweep_staging(base: Path) -> int:
    """Remove *.staging / *.partial leftovers anywhere under `base`."""
    removed = _sweep_dir(base)
    for pattern in ("*.staging", "*.partial"):
        for p in base.rglob(pattern):
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def sweep_backup_destinations(session, settings) -> int:
    """Crash-recovery sweep over every directory a backup op could have
    left temp artifacts in: the built-in dir, the app-default override,
    every per-project override, and the data dir. Never touches files
    that don't match the *.staging / *.partial patterns."""
    from app.domain.backup_dest import _app_default
    dirs: set[Path] = {Path(settings.backups_dir), Path(settings.data_dir)}
    app_dir = _app_default(session)
    if app_dir:
        dirs.add(Path(app_dir))
    for (bd,) in session.execute(
        select(Project.backup_dir).where(Project.backup_dir.is_not(None))
    ).all():
        dirs.add(Path(bd))
    removed = 0
    for d in dirs:
        try:
            if d.is_dir():
                removed += _sweep_dir(d)
        except OSError:
            continue
    return removed
