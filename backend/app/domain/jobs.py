"""Persistent job engine.

Jobs are durable rows; the engine is a disposable executor over them.
On startup, jobs left 'running' by a previous process are reconciled:
resumable work is re-queued, non-resumable work is failed with an
honest reason. Submission is idempotent via idempotency_key.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.base import JobCancelled, JobHandle
from app.adapters.registry import AdapterRegistry
from app.db.models import Asset, GenerationJob, new_id, utcnow
from app.domain.events import EventBus

log = logging.getLogger(__name__)

TERMINAL = {"succeeded", "failed", "cancelled"}
# Op families that must not run concurrently against the same project —
# two exports to the same destination or an import during a restore
# could interleave writes to the same files.
LOCKED_FAMILIES = ("backup", "asset")


class OpBusyError(Exception):
    """A same-family operation is already queued/running for the project."""


def _op_family(adapter_id: str) -> str | None:
    parts = adapter_id.split(".")
    if len(parts) >= 2 and parts[0] == "ops" and parts[1] in LOCKED_FAMILIES:
        return parts[1]
    return None


class JobEngine:
    def __init__(self, session_factory_: sessionmaker[Session], registry: AdapterRegistry, bus: EventBus, data_dir: Path):
        self._sf = session_factory_
        self._registry = registry
        self._bus = bus
        self._data_dir = Path(data_dir)
        self._wake = asyncio.Event()
        self._runner_task: asyncio.Task | None = None
        self._stopping = False
        self._cancel_events: dict[str, threading.Event] = {}

    # ---------- submission ----------

    def enqueue(
        self,
        session: Session,
        *,
        project_id: str,
        adapter_id: str,
        kind: str,
        request: dict[str, Any],
        idempotency_key: str,
        command_id: str | None = None,
        resumable: bool = True,
    ) -> GenerationJob:
        existing = session.execute(
            select(GenerationJob).where(GenerationJob.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing:
            return existing
        family = _op_family(adapter_id)
        if family:
            busy = session.execute(
                select(GenerationJob).where(
                    GenerationJob.project_id == project_id,
                    GenerationJob.adapter_id.like(f"ops.{family}.%"),
                    GenerationJob.status.in_(("queued", "running")),
                )
            ).scalars().first()
            if busy is not None:
                raise OpBusyError(
                    f"a {family} operation is already {busy.status} for this project — "
                    "wait for it to finish or cancel it first"
                )
        job = GenerationJob(
            id=new_id(),
            project_id=project_id,
            adapter_id=adapter_id,
            kind=kind,
            request_json=request,
            status="queued",
            idempotency_key=idempotency_key,
            command_id=command_id,
            resumable=resumable,
        )
        session.add(job)
        return job

    # ---------- lifecycle ----------

    def reconcile_interrupted(self, session: Session) -> int:
        """Called once at startup inside a short session; returns requeued count."""
        stuck = session.execute(select(GenerationJob).where(GenerationJob.status == "running")).scalars().all()
        for job in stuck:
            if job.resumable:
                job.status = "queued"
                job.message = "re-queued after restart"
            else:
                job.status = "failed"
                job.error = "interrupted by application stop; not resumable"
        return sum(1 for j in stuck if j.status == "queued")

    async def start(self) -> None:
        self._stopping = False
        self._runner_task = asyncio.create_task(self._run_loop(), name="job-engine")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._runner_task:
            self._runner_task.cancel()
            try:
                await self._runner_task
            except asyncio.CancelledError:
                pass

    def wake(self) -> None:
        self._wake.set()

    # ---------- cancellation ----------

    def request_cancel(self, job_id: str) -> str:
        """Mark a job for cancellation. Queued jobs cancel at claim time;
        running jobs cancel at the adapter's next checkpoint. Returns the
        resulting status hint ('cancelling' | 'cancelled' | 'terminal')."""
        ev = self._cancel_events.get(job_id)
        if ev is not None:
            ev.set()
        with self._sf() as s:
            job = s.get(GenerationJob, job_id)
            if job is None:
                return "not_found"
            if job.status in TERMINAL:
                return "terminal"
            job.cancel_requested = True
            if job.status == "queued":
                job.status = "cancelled"
                job.message = "cancelled before it started"
                job.completed_at = utcnow()
                s.commit()
                return "cancelled"
            s.commit()
            return "cancelling"

    def _check_cancelled(self, job: GenerationJob) -> bool:
        """Honor a cancel flag persisted before the job starts running."""
        return bool(job.cancel_requested)

    # ---------- execution ----------

    async def _run_loop(self) -> None:
        while not self._stopping:
            job_id = self._claim_next()
            if job_id is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self._execute(job_id)
            except Exception:
                log.exception("job %s crashed", job_id)
                self._finalize_crash(job_id)

    def _claim_next(self) -> str | None:
        with self._sf() as s:
            job = s.execute(
                select(GenerationJob).where(GenerationJob.status == "queued").order_by(GenerationJob.created_at)
            ).scalars().first()
            if job is None:
                return None
            job.status = "running"
            job.message = "started"
            s.commit()
            return job.id

    async def _execute(self, job_id: str) -> None:
        with self._sf() as s:
            job = s.get(GenerationJob, job_id)
            adapter = self._registry.get(job.adapter_id) if job else None
            if job is None or adapter is None:
                if job:
                    job.status = "failed"
                    job.error = f"adapter {job.adapter_id} not registered"
                    job.completed_at = utcnow()
                    s.commit()
                return
            errors = await adapter.validate(job.request_json)
            if errors:
                job.status = "failed"
                job.error = "; ".join(errors)
                job.completed_at = utcnow()
                s.commit()
                await self._publish(job)
                return

            work_dir = self._data_dir / "jobs" / job.project_id
            cancel_ev = threading.Event()
            self._cancel_events[job.id] = cancel_ev
            if job.cancel_requested:
                cancel_ev.set()
            handle = JobHandle(
                job_id=job.id, project_id=job.project_id,
                request=dict(job.request_json), work_dir=str(work_dir),
                cancel_event=cancel_ev,
            )
            await self._publish(job)

            final_output: dict[str, Any] | None = None
            try:
                async for update in adapter.run(handle):
                    job.progress = float(update.get("progress", job.progress))
                    job.message = update.get("message", job.message)
                    detail = update.get("detail")
                    if detail is not None:
                        job.progress_json = detail
                    if update.get("done"):
                        final_output = update.get("output")
                    s.commit()
                    await self._publish(job)
            except JobCancelled:
                job.status = "cancelled"
                job.message = "cancelled"
                job.completed_at = utcnow()
                s.commit()
                await self._publish(job)
                return
            except asyncio.CancelledError:
                job.status = "failed"
                job.error = "cancelled by shutdown"
                s.commit()
                raise
            except Exception as exc:
                job.status = "failed"
                job.error = str(exc)
                job.completed_at = utcnow()
                s.commit()
                await self._publish(job)
                return
            finally:
                self._cancel_events.pop(job.id, None)

            if final_output is None:
                job.status = "failed"
                job.error = "adapter finished without an output record"
            elif final_output.get("no_asset"):
                job.result_json = final_output.get("result")
                job.status = "succeeded"
                job.progress = 1.0
            else:
                asset = self._register_asset(s, job, final_output)
                job.output_asset_id = asset.id
                job.status = "succeeded"
                job.progress = 1.0
            job.completed_at = utcnow()
            s.commit()
            await self._publish(job)

    def _register_asset(self, s: Session, job: GenerationJob, output: dict[str, Any]) -> Asset:
        """Register a produced/imported file: CAS blob row + asset + version 1."""
        from app.db.models import AssetFile, AssetVersion, StorageLocation

        path = Path(output["path"])
        sha = output.get("sha256") or ""
        if not sha and path.exists():
            from app.domain.storage import hash_file
            sha, _ = hash_file(path)
        size = output.get("byte_size") or (path.stat().st_size if path.exists() else 0)

        default_loc = s.execute(
            select(StorageLocation).where(StorageLocation.is_default.is_(True))
        ).scalar_one_or_none()
        rel_path = output.get("rel_path")
        if not rel_path:
            try:
                rel_path = str(path.relative_to(self._data_dir))
            except ValueError:
                rel_path = f"blobs/{sha[:2]}/{sha}"
        af = s.get(AssetFile, sha) or AssetFile(sha256=sha)
        if default_loc is not None:
            af.storage_location_id = default_loc.id
        af.rel_path = rel_path
        af.byte_size = size
        af.status = "ready"
        s.add(af)

        asset = Asset(
            id=new_id(),
            project_id=job.project_id,
            name=output.get("name") or path.name,
            kind=output.get("kind", "other"),
            origin=output.get("origin", "generated"),
            uri=rel_path,
            sha256=sha,
            byte_size=size,
            provenance_json={"adapter_id": job.adapter_id, "job_id": job.id, "request": job.request_json},
        )
        s.add(asset)
        s.flush()
        version = AssetVersion(
            id=new_id(), asset_id=asset.id, version_no=1, sha256=sha,
            media_json=output.get("media") or {},
        )
        s.add(version)
        s.flush()
        asset.current_version_id = version.id
        return asset

    def _finalize_crash(self, job_id: str) -> None:
        try:
            with self._sf() as s:
                job = s.get(GenerationJob, job_id)
                if job and job.status not in TERMINAL:
                    job.status = "failed"
                    job.error = "engine crash"
                    job.completed_at = utcnow()
                    s.commit()
        except Exception:
            log.exception("failed to finalize crashed job %s", job_id)

    async def _publish(self, job: GenerationJob) -> None:
        await self._bus.publish(
            "job.progress",
            job.project_id,
            job.id,
            {
                "status": job.status,
                "progress": job.progress,
                "message": job.message,
                "adapter_id": job.adapter_id,
                "output_asset_id": job.output_asset_id,
                "error": job.error,
                "progress_json": job.progress_json,
                "cancel_requested": job.cancel_requested,
            },
        )
