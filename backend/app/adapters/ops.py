"""Local operation adapters — durable jobs for file/project operations.

These are NOT AI providers. They reuse the job engine so long-running
filesystem work (large-media ingest, package export/import, integrity
scans) gets progress, persistence, and restart reconciliation for free.
Each returns a final output marked no_asset where nothing new should be
registered as a project asset.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

from app.adapters.base import CapabilityProfile, JobHandle
from app.db.models import Asset, AssetVersion, BackupRun, Project, new_id, utcnow
from app.domain import backup as backup_mod


class _OpsBase:
    def __init__(self, session_factory, storage, settings):
        self._sf = session_factory
        self._storage = storage
        self._settings = settings

    async def validate(self, request: dict[str, Any]) -> list[str]:
        return []


class AssetImportAdapter(_OpsBase):
    profile = CapabilityProfile(
        adapter_id="ops.asset.import",
        display_name="Import file",
        adapter_kind="local",
        tasks=("import",),
        supports={},
        limits={},
        requires_cloud=False,
        cost_model="free",
        notes="Local filesystem ingest into content-addressed storage. Not an AI adapter.",
    )

    async def validate(self, request):
        if not (request.get("source_path") or "").strip():
            return ["source_path is required"]
        if not Path(request["source_path"]).is_file():
            return [f"file not found: {request['source_path']}"]
        return []

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        src = Path(handle.request["source_path"])
        yield {"progress": 0.05, "message": f"Reading {src.name}"}
        await asyncio.sleep(0)

        with self._sf() as session:
            project = session.get(Project, handle.project_id)
            loc = self._storage.get_location(session, project.storage_location_id if project else None)
            af = self._storage.ingest(session, src, location=loc)
            session.commit()
            out_path = self._storage.file_path(session, af)
            yield {"progress": 0.9, "message": "Registered in library"}
            yield {
                "done": True,
                "output": {
                    "path": str(out_path),
                    "rel_path": af.rel_path,
                    "sha256": af.sha256,
                    "kind": handle.request.get("kind", "other"),
                    "name": handle.request.get("name") or src.name,
                    "byte_size": af.byte_size,
                    "origin": "imported",
                },
            }


class AssetRelinkAdapter(_OpsBase):
    profile = CapabilityProfile(
        adapter_id="ops.asset.relink",
        display_name="Relink asset file",
        adapter_kind="local",
        tasks=("relink",),
        supports={},
        limits={},
        requires_cloud=False,
        cost_model="free",
        notes="Re-associate an asset with bytes on disk; different content becomes a new version.",
    )

    async def validate(self, request):
        if not (request.get("source_path") or "").strip():
            return ["source_path is required"]
        if not Path(request["source_path"]).is_file():
            return [f"file not found: {request['source_path']}"]
        if not request.get("asset_id"):
            return ["asset_id is required"]
        return []

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        src = Path(handle.request["source_path"])
        asset_id = handle.request["asset_id"]
        yield {"progress": 0.1, "message": "Hashing file"}
        await asyncio.sleep(0)

        with self._sf() as session:
            asset = session.get(Asset, asset_id)
            if asset is None or asset.project_id != handle.project_id:
                raise ValueError("asset not found in this project")
            loc = self._storage.get_location(session, None)
            af = self._storage.ingest(session, src, location=loc)
            session.flush()
            if af.sha256 == asset.sha256:
                # Same bytes — just confirm presence.
                af.status = "ready"
                asset.status = "ready"
                result = {"relinked": "existing", "asset_id": asset.id}
            else:
                maxv = max(
                    (v.version_no for v in self._storage.project_versions(session, asset.id)),
                    default=0,
                )
                v = AssetVersion(
                    id=new_id(), asset_id=asset.id, version_no=maxv + 1,
                    sha256=af.sha256,
                    media_json={"relinked_from": str(src)},
                )
                session.add(v)
                session.flush()
                asset.current_version_id = v.id
                asset.sha256 = af.sha256
                asset.uri = af.rel_path
                asset.byte_size = af.byte_size
                asset.status = "ready"
                result = {"relinked": "new_version", "asset_id": asset.id, "version_no": v.version_no}
            session.commit()
            yield {"done": True, "output": {"no_asset": True, "result": result}}


class AssetsVerifyAdapter(_OpsBase):
    profile = CapabilityProfile(
        adapter_id="ops.assets.verify",
        display_name="Verify asset files",
        adapter_kind="local",
        tasks=("verify",),
        supports={},
        limits={},
        requires_cloud=False,
        cost_model="free",
        notes="Scans asset blob existence on disk and marks missing files.",
    )

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        yield {"progress": 0.1, "message": "Scanning asset files"}
        await asyncio.sleep(0)
        with self._sf() as session:
            counts = self._storage.verify_project(session, handle.project_id)
            session.commit()
        yield {"done": True, "output": {"no_asset": True, "result": counts}}


class BackupExportAdapter(_OpsBase):
    profile = CapabilityProfile(
        adapter_id="ops.backup.export",
        display_name="Export project package",
        adapter_kind="local",
        tasks=("backup", "export"),
        supports={},
        limits={},
        requires_cloud=False,
        cost_model="free",
        notes="Streams a verified .atomicpkg; never buffers whole files.",
    )

    async def validate(self, request):
        kind = request.get("kind", "backup")
        if kind not in ("backup", "export"):
            return ["kind must be 'backup' or 'export'"]
        return []

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        kind = handle.request.get("kind", "backup")
        dest = handle.request.get("dest_path")
        if not dest:
            stamp = utcnow().strftime("%Y%m%d-%H%M%S")
            dest = self._settings.backups_dir / handle.project_id / f"{kind}-{stamp}.atomicpkg"
        dest = Path(dest)

        progress_state = {"p": 0.0, "m": "starting"}

        def progress(p, m):
            progress_state["p"] = p
            progress_state["m"] = m

        yield {"progress": 0.02, "message": "Serializing project"}
        manifest, sha, size = await asyncio.to_thread(
            backup_mod.export_package,
            self._sf, handle.project_id, dest, self._storage, progress,
        )
        yield {"progress": 0.95, "message": progress_state["m"]}
        with self._sf() as session:
            run = BackupRun(
                id=new_id(), project_id=handle.project_id,
                project_name=(session.get(Project, handle.project_id) or Project(name="")).name,
                kind=kind, status="succeeded", package_path=str(dest),
                package_sha256=sha, byte_size=size,
                stats_json={
                    "assets": len(manifest.get("assets", [])),
                    "files": len(manifest.get("files", [])),
                    "characters": len(manifest["entities"].get("characters", [])),
                    "worlds": len(manifest["entities"].get("worlds", [])),
                    "locations": len(manifest["entities"].get("locations", [])),
                },
                job_id=handle.job_id, completed_at=utcnow(),
            )
            session.add(run)
            session.commit()
            yield {
                "done": True,
                "output": {"no_asset": True,
                           "result": {"backup_run_id": run.id, "package_path": str(dest), "byte_size": size}},
            }


class BackupImportAdapter(_OpsBase):
    profile = CapabilityProfile(
        adapter_id="ops.backup.import",
        display_name="Import project package",
        adapter_kind="local",
        tasks=("import",),
        supports={},
        limits={},
        requires_cloud=False,
        cost_model="free",
        notes="Validates + imports an .atomicpkg into this project (id-remapped, hash-verified).",
    )

    async def validate(self, request):
        if not (request.get("package_path") or "").strip():
            return ["package_path is required"]
        if not Path(request["package_path"]).is_file():
            return [f"package not found: {request['package_path']}"]
        try:
            backup_mod.read_manifest(Path(request["package_path"]))
        except backup_mod.PackageError as exc:
            return [str(exc)]
        return []

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        pkg = Path(handle.request["package_path"])
        yield {"progress": 0.05, "message": "Reading manifest"}
        await asyncio.sleep(0)
        stats = await asyncio.to_thread(
            backup_mod.import_package,
            self._sf, pkg, self._storage,
            project_id_override=handle.project_id, name_suffix="",
        )
        # import_package fills the stub project this job is scoped to.
        with self._sf() as session:
            stub = session.get(Project, handle.project_id)
            if stub is not None and stub.status == "importing":
                stub.status = "active"
            run = BackupRun(
                id=new_id(), project_id=handle.project_id,
                project_name=stub.name if stub else "",
                kind="import", status="succeeded", package_path=str(pkg),
                stats_json=stats, job_id=handle.job_id, completed_at=utcnow(),
            )
            session.add(run)
            session.commit()
            yield {"done": True, "output": {"no_asset": True, "result": {**stats, "backup_run_id": run.id}}}


class BackupRestoreAdapter(_OpsBase):
    profile = CapabilityProfile(
        adapter_id="ops.backup.restore",
        display_name="Restore project from backup",
        adapter_kind="local",
        tasks=("restore",),
        supports={},
        limits={},
        requires_cloud=False,
        cost_model="free",
        notes="Creates a NEW project from a verified backup; never mutates the live project.",
    )

    async def validate(self, request):
        if not request.get("backup_run_id"):
            return ["backup_run_id is required"]
        return []

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        run_id = handle.request["backup_run_id"]
        yield {"progress": 0.05, "message": "Loading backup record"}
        await asyncio.sleep(0)
        with self._sf() as session:
            run = session.get(BackupRun, run_id)
            if run is None or run.status != "succeeded":
                raise ValueError("successful backup run not found")
            pkg = Path(run.package_path)
        stats = await asyncio.to_thread(
            backup_mod.import_package,
            self._sf, pkg, self._storage,
            project_id_override=None, name_suffix=" (restored)",
        )
        with self._sf() as session:
            new_run = BackupRun(
                id=new_id(), project_id=handle.project_id,
                project_name=run.project_name, kind="restore", status="succeeded",
                package_path=str(pkg), stats_json=stats,
                job_id=handle.job_id, completed_at=utcnow(),
            )
            session.add(new_run)
            session.commit()
            yield {
                "done": True,
                "output": {"no_asset": True,
                           "result": {**stats, "restored_project_id": stats["project_id"]}},
            }
