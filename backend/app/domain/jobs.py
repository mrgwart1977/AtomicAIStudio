"""Persistent job engine.

Jobs are durable rows; the engine is a disposable executor over them.
On startup, jobs left 'running' by a previous process are reconciled:
resumable work is re-queued, non-resumable work is failed with an
honest reason. Submission is idempotent via idempotency_key.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.base import JobHandle
from app.adapters.registry import AdapterRegistry
from app.db.models import Asset, GenerationJob, new_id, utcnow
from app.domain.events import EventBus

log = logging.getLogger(__name__)

TERMINAL = {"succeeded", "failed", "cancelled"}


class JobEngine:
    def __init__(self, session_factory_: sessionmaker[Session], registry: AdapterRegistry, bus: EventBus, data_dir: Path):
        self._sf = session_factory_
        self._registry = registry
        self._bus = bus
        self._data_dir = Path(data_dir)
        self._wake = asyncio.Event()
        self._runner_task: asyncio.Task | None = None
        self._stopping = False

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
            handle = JobHandle(job_id=job.id, project_id=job.project_id, request=dict(job.request_json), work_dir=str(work_dir))
            await self._publish(job)

            final_output: dict[str, Any] | None = None
            try:
                async for update in adapter.run(handle):
                    job.progress = float(update.get("progress", job.progress))
                    job.message = update.get("message", job.message)
                    if update.get("done"):
                        final_output = update.get("output")
                    s.commit()
                    await self._publish(job)
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

            if final_output is None:
                job.status = "failed"
                job.error = "adapter finished without an output record"
            else:
                asset = Asset(
                    id=new_id(),
                    project_id=job.project_id,
                    kind=final_output.get("kind", "other"),
                    origin="generated",
                    uri=str(Path(final_output["path"]).relative_to(self._data_dir)) if Path(final_output["path"]).is_relative_to(self._data_dir) else final_output["path"],
                    sha256=final_output.get("sha256", ""),
                    provenance_json={"adapter_id": job.adapter_id, "job_id": job.id, "request": job.request_json},
                )
                s.add(asset)
                s.flush()
                job.output_asset_id = asset.id
                job.status = "succeeded"
                job.progress = 1.0
            job.completed_at = utcnow()
            s.commit()
            await self._publish(job)

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
            },
        )
