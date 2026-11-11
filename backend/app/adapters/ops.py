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
from typing import Any, AsyncIterator, Callable

from app.adapters.base import CapabilityProfile, JobCancelled, JobHandle
from app.db.models import Asset, AssetVersion, BackupRun, Project, new_id, utcnow
from app.domain import backup as backup_mod
from app.domain import backup_dest


def _validate_external_source(ext: Any) -> list[str]:
    """C1.5 §4 ProvenanceTagger — the user asserts where a file came from;
    the provider/strategy/method must be real catalog entries so the tag is
    honest. We record the claim as user_reported — never assert it."""
    if not isinstance(ext, dict):
        return ["external_source must be an object"]
    from app.domain.providers import catalog
    key = (ext.get("provider_key") or "").strip()
    prov = catalog.get_provider(key)
    if not key or prov is None:
        return [f"external_source.provider_key '{key}' is not a catalog provider"]
    sid = ext.get("strategy_id")
    if sid is not None:
        if not any(s.get("strategy_id") == sid for s in prov.get("strategies") or []):
            return [f"strategy_id '{sid}' is not a strategy of provider '{key}'"]
    method = ext.get("method")
    if method is not None and method not in catalog.METHODS:
        return [f"external_source.method '{method}' is not a known access method"]
    if ext.get("note") is not None and not isinstance(ext["note"], str):
        return ["external_source.note must be a string"]
    return []


class _OpsBase:
    def __init__(self, session_factory, storage, settings):
        self._sf = session_factory
        self._storage = storage
        self._settings = settings

    async def validate(self, request: dict[str, Any]) -> list[str]:
        return []

    def _mark_run(self, run_id: str, status: str, **fields) -> None:
        """Update a BackupRun created at job start (its own short txn)."""
        with self._sf() as session:
            run = session.get(BackupRun, run_id)
            if run is not None:
                run.status = status
                for k, v in fields.items():
                    setattr(run, k, v)
                if status in ("succeeded", "failed", "cancelled"):
                    run.completed_at = utcnow()
                session.commit()


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
        ext = request.get("external_source")
        if ext is not None:
            errs = _validate_external_source(ext)
            if errs:
                return errs
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
            if handle.request.get("delete_source_after"):
                src.unlink(missing_ok=True)
            out_path = self._storage.file_path(session, af)
            yield {"progress": 0.9, "message": "Registered in library"}
            ext = handle.request.get("external_source")
            output = {
                "path": str(out_path),
                "rel_path": af.rel_path,
                "sha256": af.sha256,
                "kind": handle.request.get("kind", "other"),
                "name": handle.request.get("name") or src.name,
                "byte_size": af.byte_size,
                "origin": "external" if ext else "imported",
            }
            if ext:
                output["external_source"] = {
                    "provider_key": ext.get("provider_key"),
                    "strategy_id": ext.get("strategy_id"),
                    "method": ext.get("method"),
                    "note": (ext.get("note") or "")[:200],
                }
            yield {"done": True, "output": output}


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

        # Resolve destination NOW (project override → app default → built-in)
        # — never fall back silently when a configured dir is unreachable.
        with self._sf() as session:
            project = session.get(Project, handle.project_id)
            dest_dir = Path(handle.request["dest_path"]).parent \
                if handle.request.get("dest_path") \
                else backup_dest.resolve_dir(session, self._settings, project)
            needed = backup_dest.estimate_package_bytes(session, handle.project_id)

        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        dest = Path(handle.request["dest_path"]) if handle.request.get("dest_path") \
            else dest_dir / f"{kind}-{stamp}.atomicpkg"

        # Run row exists from the START (even before preflight): a crash
        # leaves it 'running' and startup reconciliation marks it honestly
        # interrupted; a rejected destination is recorded as a failed run
        # so the history shows the attempt.
        with self._sf() as session:
            project = session.get(Project, handle.project_id)
            run = BackupRun(
                id=new_id(), project_id=handle.project_id,
                project_name=project.name if project else "",
                kind=kind, status="running", package_path=str(dest),
                destination_dir=str(dest.parent), bytes_total=needed,
                stats_json={}, job_id=handle.job_id,
            )
            session.add(run)
            session.commit()
            run_id = run.id

        try:
            backup_dest.require_writable_destination(dest_dir, needed_bytes=needed)
        except Exception as exc:
            self._mark_run(run_id, "failed", error=str(exc))
            raise

        state = {"p": 0.0, "m": "starting"}
        detail_box: dict[str, Any] = {"d": None}

        def progress(p, m):
            state["p"], state["m"] = p, m

        def on_detail(d):
            detail_box["d"] = d

        try:
            reporter = backup_mod.ProgressReporter(needed, on_detail)
            task = asyncio.create_task(asyncio.to_thread(
                backup_mod.export_package,
                self._sf, handle.project_id, dest, self._storage,
                progress=progress, cancel_check=handle.check_cancel,
                reporter=reporter,
            ))
            while not task.done():
                yield {"progress": state["p"], "message": state["m"], "detail": detail_box["d"]}
                await asyncio.sleep(0.3)
            manifest, sha, size = await task
            # Guaranteed final detail — even for exports too fast to emit.
            yield {"progress": 0.95, "message": "Finalizing",
                   "detail": detail_box["d"] or reporter.snapshot()}
        except JobCancelled:
            self._mark_run(run_id, "cancelled", error="cancelled by user")
            raise
        except Exception as exc:
            self._mark_run(run_id, "failed", error=str(exc))
            raise

        with self._sf() as session:
            run = session.get(BackupRun, run_id)
            run.status = "succeeded"
            run.package_sha256 = sha
            run.byte_size = size
            run.stats_json = {
                "assets": len(manifest.get("assets", [])),
                "files": len(manifest.get("files", [])),
                "characters": len(manifest["entities"].get("characters", [])),
                "worlds": len(manifest["entities"].get("worlds", [])),
                "locations": len(manifest["entities"].get("locations", [])),
            }
            run.completed_at = utcnow()
            session.commit()
        yield {
            "done": True,
            "output": {"no_asset": True,
                       "result": {"backup_run_id": run_id, "package_path": str(dest), "byte_size": size}},
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
        total_bytes = 0
        try:
            manifest = backup_mod.read_manifest(pkg)
            total_bytes = sum(f["byte_size"] for f in manifest.get("files", []))
        except backup_mod.PackageError:
            pass  # validate() already checked; re-raised in the worker

        with self._sf() as session:
            stub = session.get(Project, handle.project_id)
            run = BackupRun(
                id=new_id(), project_id=handle.project_id,
                project_name=stub.name if stub else "",
                kind="import", status="running", package_path=str(pkg),
                bytes_total=total_bytes, stats_json={}, job_id=handle.job_id,
            )
            session.add(run)
            session.commit()
            run_id = run.id

        state = {"p": 0.05, "m": "Reading manifest"}
        detail_box: dict[str, Any] = {"d": None}

        def progress(p, m):
            state["p"], state["m"] = p, m

        def on_detail(d):
            detail_box["d"] = d

        try:
            reporter = backup_mod.ProgressReporter(total_bytes or 1, on_detail)
            task = asyncio.create_task(asyncio.to_thread(
                backup_mod.import_package,
                self._sf, pkg, self._storage,
                project_id_override=handle.project_id, name_suffix="",
                progress=progress, cancel_check=handle.check_cancel,
                reporter=reporter,
            ))
            while not task.done():
                yield {"progress": state["p"], "message": state["m"], "detail": detail_box["d"]}
                await asyncio.sleep(0.3)
            stats = await task
            yield {"progress": 0.95, "message": "Committing project",
                   "detail": detail_box["d"] or reporter.snapshot()}
        except JobCancelled:
            self._mark_run(run_id, "cancelled", error="cancelled by user")
            raise
        except Exception as exc:
            self._mark_run(run_id, "failed", error=str(exc))
            raise

        # import_package fills the stub project this job is scoped to.
        with self._sf() as session:
            stub = session.get(Project, handle.project_id)
            if stub is not None and stub.status == "importing":
                stub.status = "active"
            run = session.get(BackupRun, run_id)
            run.status = "succeeded"
            run.stats_json = stats
            run.completed_at = utcnow()
            session.commit()
            yield {"done": True, "output": {"no_asset": True, "result": {**stats, "backup_run_id": run_id}}}


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
        src_run_id = handle.request["backup_run_id"]
        with self._sf() as session:
            src_run = session.get(BackupRun, src_run_id)
            if src_run is None or src_run.status != "succeeded":
                raise ValueError("successful backup run not found")
            pkg = Path(src_run.package_path)
            project_name = src_run.project_name
            bytes_total = src_run.bytes_total or 0
        if not pkg.is_file():
            raise ValueError(
                f"backup package is missing: {pkg} — it may have been moved "
                "or the drive disconnected. Restore from another backup."
            )

        with self._sf() as session:
            new_run = BackupRun(
                id=new_id(), project_id=handle.project_id,
                project_name=project_name, kind="restore", status="running",
                package_path=str(pkg), bytes_total=bytes_total,
                stats_json={}, job_id=handle.job_id,
            )
            session.add(new_run)
            session.commit()
            new_run_id = new_run.id

        state = {"p": 0.05, "m": "Loading backup"}
        detail_box: dict[str, Any] = {"d": None}

        def progress(p, m):
            state["p"], state["m"] = p, m

        def on_detail(d):
            detail_box["d"] = d

        try:
            reporter = backup_mod.ProgressReporter(bytes_total or 1, on_detail)
            task = asyncio.create_task(asyncio.to_thread(
                backup_mod.import_package,
                self._sf, pkg, self._storage,
                project_id_override=None, name_suffix=" (restored)",
                progress=progress, cancel_check=handle.check_cancel,
                reporter=reporter,
            ))
            while not task.done():
                yield {"progress": state["p"], "message": state["m"], "detail": detail_box["d"]}
                await asyncio.sleep(0.3)
            stats = await task
            yield {"progress": 0.95, "message": "Committing project",
                   "detail": detail_box["d"] or reporter.snapshot()}
        except JobCancelled:
            self._mark_run(new_run_id, "cancelled", error="cancelled by user")
            raise
        except Exception as exc:
            self._mark_run(new_run_id, "failed", error=str(exc))
            raise

        self._mark_run(new_run_id, "succeeded", stats_json=stats)
        yield {
            "done": True,
            "output": {"no_asset": True,
                       "result": {**stats, "restored_project_id": stats["project_id"]}},
        }
