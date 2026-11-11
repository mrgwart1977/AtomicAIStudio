"""Persistent job engine.

Jobs are durable rows; the engine is a disposable executor over them.
On startup, jobs left 'running' by a previous process are reconciled:
resumable work is re-queued, non-resumable work is failed with an
honest reason. Submission is idempotent via idempotency_key.

Concurrency model (M3):
- "ops" lane (backups, imports, model downloads): one at a time — same
  semantics as M1.1.
- "gen" lane (generation): up to one running job per GPU. GPU-bound
  adapters additionally acquire a GpuScheduler slot at claim time; a
  busy or missing device leaves the job queued (other lanes still run).
- App-level jobs (project_id NULL — model downloads) are outside the
  per-project family lock.
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
    def __init__(self, session_factory_: sessionmaker[Session], registry: AdapterRegistry,
                 bus: EventBus, data_dir: Path, gpu_scheduler=None):
        self._sf = session_factory_
        self._registry = registry
        self._bus = bus
        self._data_dir = Path(data_dir)
        self._sched = gpu_scheduler
        self._wake = asyncio.Event()
        self._runner_task: asyncio.Task | None = None
        self._stopping = False
        self._cancel_events: dict[str, threading.Event] = {}
        self._running: set[asyncio.Task] = set()
        self._lane_busy = {"ops": 0, "gen": 0}

    # ---------- submission ----------

    def enqueue(
        self,
        session: Session,
        *,
        project_id: str | None,
        adapter_id: str,
        kind: str,
        request: dict[str, Any],
        idempotency_key: str,
        command_id: str | None = None,
        resumable: bool = True,
        model_key: str | None = None,
    ) -> GenerationJob:
        existing = session.execute(
            select(GenerationJob).where(GenerationJob.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing:
            return existing
        family = _op_family(adapter_id)
        if family and project_id:
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
            model_key=model_key,
        )
        session.add(job)
        return job

    # ---------- lifecycle ----------

    def reconcile_interrupted(self, session: Session) -> int:
        """Called once at startup inside a short session; returns requeued count."""
        stuck = session.execute(select(GenerationJob).where(GenerationJob.status == "running")).scalars().all()
        from app.domain.workers import attempts as _attempts_mod
        for job in stuck:
            job.recovery_state = "needs_reconciliation" if job.execution_scope == "remote" else "interrupted"
            # Close open attempt records — a job left "running" at
            # shutdown must not keep an attempt that reads as still
            # in-flight; it was interrupted, not completed.
            _attempts_mod.close_open_attempts(session, job, "interrupted")
            if job.resumable:
                job.status = "queued"
                job.message = "re-queued after restart"
                job.attempt_no = (job.attempt_no or 0) + 1
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
        tasks = [self._runner_task, *self._running]
        for t in tasks:
            if t is not None:
                t.cancel()
        for t in tasks:
            if t is not None:
                try:
                    await t
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

    # ---------- execution ----------

    def _gen_lane_capacity(self) -> int:
        """One inference job per GPU; at least one slot so CPU/mock gen
        still runs on GPU-less machines."""
        if self._sched is None:
            return 1
        return max(1, self._sched.gpu_count())

    def _lane_of(self, adapter) -> str:
        return getattr(adapter.profile, "lane", "ops")

    def _lane_capacity(self, lane: str) -> int:
        return self._gen_lane_capacity() if lane == "gen" else 1

    async def _run_loop(self) -> None:
        while not self._stopping:
            claimed = self._claim_next()
            if claimed is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                continue
            job_id, lane = claimed
            task = asyncio.create_task(self._execute_guarded(job_id, lane))
            self._running.add(task)
            task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._running.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("job task crashed", exc_info=task.exception())
        self._wake.set()

    async def _execute_guarded(self, job_id: str, lane: str) -> None:
        try:
            await self._execute(job_id)
        finally:
            _release_memory_reservation(job_id)
            self._lane_busy[lane] = max(0, self._lane_busy.get(lane, 1) - 1)
            if self._sched is not None:
                self._sched.release(job_id)

    def _claim_next(self) -> tuple[str, str] | None:
        """First queued job (FIFO) that has lane capacity — and a GPU slot
        when its adapter needs one. Scanning continues past blocked jobs
        so a busy GPU doesn't starve the ops lane."""
        with self._sf() as s:
            queued = s.execute(
                select(GenerationJob).where(GenerationJob.status == "queued")
                .order_by(GenerationJob.created_at)
            ).scalars().all()
            for job in queued:
                adapter = self._registry.get(job.adapter_id)
                if adapter is None:
                    job.status = "failed"
                    job.error = f"adapter {job.adapter_id} not registered"
                    job.completed_at = utcnow()
                    s.commit()
                    continue
                lane = self._lane_of(adapter)
                if self._lane_busy.get(lane, 0) >= self._lane_capacity(lane):
                    continue
                gpu_idx = None
                if getattr(adapter.profile, "needs_gpu", False) and self._sched is not None:
                    est = (job.request_json or {}).get("est_vram_bytes")
                    gpu_idx = self._sched.acquire(job.id, dict(job.request_json or {}),
                                                  est_vram=est)
                    if gpu_idx is None:
                        job.message = f"waiting for GPU — {self._sched.last_reason or 'no slot free'}"
                        s.commit()
                        continue
                    job.gpu_index = gpu_idx if gpu_idx >= 0 else None
                    req = dict(job.request_json or {})
                    req["assigned_gpu"] = gpu_idx
                    job.request_json = req
                    gpu_names = {g.get("index"): g.get("name") for g in self._sched.gpus()}
                    job.message = (
                        f"running on CPU" if gpu_idx < 0
                        else f"running on GPU {gpu_idx} ({gpu_names.get(gpu_idx) or 'device'})"
                    )
                job.status = "running"
                s.commit()
                self._lane_busy[lane] = self._lane_busy.get(lane, 0) + 1
                return job.id, lane
            s.commit()
            return None

    async def _execute(self, job_id: str) -> None:
        with self._sf() as s:
            job = s.get(GenerationJob, job_id)
            adapter = self._registry.get(job.adapter_id) if job else None
            if job is None or adapter is None:
                return
            errors = await adapter.validate(job.request_json)
            if errors:
                job.status = "failed"
                job.error = "; ".join(errors)
                job.completed_at = utcnow()
                s.commit()
                await self._publish(job)
                return

            # System-RAM preflight (issue #28): VRAM checks alone are not
            # enough — CPU-offloaded weights live in physical RAM. Block
            # when the estimate can't fit with OS headroom; flag unknowns.
            profile = getattr(adapter, "profile", None)
            if (profile is not None and getattr(profile, "lane", "") == "gen"
                    and getattr(profile, "adapter_kind", "") != "mock"):
                mem_gate = _memory_preflight(job.request_json, profile, job.id)
                if mem_gate["decision"] == "block":
                    job.status = "failed"
                    job.error = f"preflight memory check failed: {mem_gate['reason']}"
                    job.progress_json = {"memory_preflight": mem_gate["snapshot"]}
                    job.completed_at = utcnow()
                    s.commit()
                    await self._publish(job)
                    return
                if mem_gate["decision"] == "warn":
                    if not job.request_json.get("memory_risk_acknowledged"):
                        job.status = "failed"
                        job.error = ("memory warning requires explicit confirmation: "
                                     f"{mem_gate['reason']} — resubmit acknowledging "
                                     "the risk (memory_risk_acknowledged)")
                        job.progress_json = {"memory_preflight": mem_gate["snapshot"]}
                        job.completed_at = utcnow()
                        s.commit()
                        await self._publish(job)
                        return
                    job.message = f"memory warning acknowledged: {mem_gate['reason']}"
                    job.progress_json = {"memory_preflight": mem_gate["snapshot"]}
                    s.commit()

            work_dir = self._data_dir / "jobs" / (job.project_id or "_app")
            # Durable execution-attempt record — written before any work
            # so a crash leaves evidence of what was in flight (M4.1).
            from app.domain.workers import attempts as attempts_mod
            attempt = attempts_mod.start_attempt(s, job)

            cancel_ev = threading.Event()
            self._cancel_events[job.id] = cancel_ev
            if job.cancel_requested:
                cancel_ev.set()
            handle = JobHandle(
                job_id=job.id, project_id=job.project_id or "",
                request=dict(job.request_json), work_dir=str(work_dir),
                cancel_event=cancel_ev,
            )
            await self._publish(job)

            final_output: dict[str, Any] | None = None
            try:
                async for update in adapter.run(handle):
                    job.progress = float(update.get("progress", job.progress))
                    job.message = update.get("message", job.message)
                    if patch := update.get("attempt"):
                        attempts_mod.patch_attempt(s, job, attempt["attempt_id"], patch)
                    detail = update.get("detail")
                    if detail is not None:
                        merged = dict(job.progress_json or {})
                        # Attempt history is engine-owned — adapter detail
                        # may never overwrite it.
                        merged.update({k: v for k, v in detail.items()
                                       if k != "attempts"})
                        job.progress_json = merged
                    if update.get("done"):
                        final_output = update.get("output")
                    s.commit()
                    await self._publish(job)
            except JobCancelled:
                attempts_mod.finish_attempt(s, job, attempt["attempt_id"], "cancelled")
                job.status = "cancelled"
                job.message = "cancelled"
                job.completed_at = utcnow()
                s.commit()
                await self._publish(job)
                self._purge_work_dir(work_dir, job.id)
                return
            except asyncio.CancelledError:
                attempts_mod.finish_attempt(s, job, attempt["attempt_id"], "interrupted")
                job.status = "failed"
                job.error = "cancelled by shutdown"
                s.commit()
                raise
            except Exception as exc:
                from app.domain.workers.supervisor import WorkerDeadError
                attempts_mod.finish_attempt(
                    s, job, attempt["attempt_id"],
                    "worker_dead" if isinstance(exc, WorkerDeadError) else "failed")
                job.status = "failed"
                job.error = str(exc)
                job.completed_at = utcnow()
                s.commit()
                await self._publish(job)
                self._purge_work_dir(work_dir, job.id)
                return
            finally:
                self._cancel_events.pop(job.id, None)

            if final_output is None:
                attempts_mod.finish_attempt(s, job, attempt["attempt_id"], "failed")
                job.status = "failed"
                job.error = "adapter finished without an output record"
                self._purge_work_dir(work_dir, job.id)
            elif final_output.get("no_asset"):
                job.result_json = final_output.get("result")
                attempts_mod.finish_attempt(s, job, attempt["attempt_id"], "succeeded")
                job.status = "succeeded"
                job.progress = 1.0
            else:
                asset = self._register_asset(s, job, final_output)
                job.output_asset_id = asset.id
                for extra in final_output.get("extra_assets") or []:
                    self._register_asset(s, job, {
                        "origin": final_output.get("origin", "generated"),
                        "media": final_output.get("media"),
                        **extra,
                    })
                attempts_mod.finish_attempt(s, job, attempt["attempt_id"], "succeeded")
                job.status = "succeeded"
                job.progress = 1.0
                self._apply_post_command(s, job, final_output, asset)
            job.completed_at = utcnow()
            s.commit()
            await self._publish(job)

    def _apply_post_command(self, s: Session, job: GenerationJob,
                            output: dict[str, Any], asset: Asset) -> None:
        """An adapter may ask the engine to land a follow-up command —
        e.g. generation → `shot.update {thumbnail_asset_id}` — so the
        effect is audited, versioned, and undoable like any mutation."""
        spec = output.get("apply_command")
        if not spec or not job.project_id:
            return
        try:
            from app.domain import commands as cmd
            from app.db.models import Project
            project = s.get(Project, job.project_id)
            if project is None:
                return
            payload = {k: (asset.id if v == "__asset__" else v)
                       for k, v in (spec.get("payload") or {}).items()}
            cmd.commit(
                s, project,
                command_type=spec["command_type"], payload=payload,
                scope="this_take", origin="generation_job",
                target_type="shot", target_id=payload.get("shot_id"),
                expected_revision=project.revision,
                idempotency_key=f"job:{job.id}:apply",
                job_engine=None,
            )
        except Exception as exc:
            log.warning("post-command for job %s failed: %s", job.id, exc)
            job.message = f"succeeded (thumbnail assign failed: {exc})"

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

        provenance = {"adapter_id": job.adapter_id, "job_id": job.id,
                      "model_key": job.model_key, "gpu_index": job.gpu_index,
                      "request": job.request_json}
        ext = output.get("external_source")
        if ext:
            # C1.5 §4 ProvenanceTagger — user-reported external origin.
            # `verified_remote` is reserved for T1 strategies with remote
            # job ids and can never be claimed through the import path.
            provenance.update({
                "provenance_confidence": "user_reported",
                "external_provider": ext.get("provider_key"),
                "external_strategy": ext.get("strategy_id"),
                "external_method": ext.get("method"),
                "external_note": ext.get("note"),
                "external_reported_at": utcnow().isoformat(),
            })
        asset = Asset(
            id=new_id(),
            project_id=job.project_id,
            name=output.get("name") or path.name,
            kind=output.get("kind", "other"),
            origin=output.get("origin", "generated"),
            uri=rel_path,
            sha256=sha,
            byte_size=size,
            provenance_json=provenance,
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

    def _purge_work_dir(self, work_dir: Path, job_id: str) -> None:
        """Failed/cancelled jobs must not leave orphan output files that
        could masquerade as results. Assets only materialize on success, so
        anything left in the work dir at this point is a partial artifact."""
        try:
            for f in work_dir.glob(f"{job_id}*"):
                if f.is_file():
                    f.unlink(missing_ok=True)
        except OSError:
            log.warning("could not purge work-dir outputs for %s", job_id)

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
                "gpu_index": job.gpu_index,
                "model_key": job.model_key,
            },
        )


# RAM reservations held by gen jobs currently in flight (job_id → bytes).
# Concurrent workers must subtract each other's estimates — one job's
# preflight must account for the other.
_reserved_ram: dict[str, int] = {}


def _estimate_ram_bytes(request: dict[str, Any]) -> tuple[int, bool]:
    """(bytes, known): host-RAM estimate for the requested residency mode.

    residency_mode (request): 'cpu_offload' (default when the model declares
    weight_offload_cpu — all component bytes host-resident), 'gpu' (weights
    on-device; host holds only paging share ~30%), 'te_cpu' (text encoder
    host-resident, diffusion+VAE on GPU), 'disk' (params streamed from disk —
    minimal host residency). +2 GiB runtime overhead in every mode."""
    from app.domain import model_catalog, resources
    spec = model_catalog.get(request.get("model_key") or "")
    if spec is None or not spec.files:
        return 0, False
    by_role: dict[str, int] = {}
    for f in spec.files:
        by_role[f.role or "diffusion"] = by_role.get(f.role or "diffusion", 0) + f.size_bytes
    weights = sum(by_role.values())
    mode = request.get("residency_mode") or (
        "cpu_offload" if (spec.capabilities or {}).get("weight_offload_cpu") else "gpu")
    if mode == "cpu_offload":
        resident = weights
    elif mode == "te_cpu":
        resident = by_role.get("text_encoder", 0) + int(
            (weights - by_role.get("text_encoder", 0)) * 0.15)
    elif mode == "disk":
        resident = int(weights * 0.15)          # mmap'd pages, streaming
    else:  # gpu — host side only pages the runtime touches
        resident = int(weights * 0.30)
    return resident + int(2 * resources.GIB), True


def _memory_preflight(request: dict[str, Any], profile: Any,
                      job_id: str = "") -> dict[str, Any]:
    """Gate a gen job on available RAM INCLUDING other workers' live
    reservations. Reserves the estimate for the job's lifetime."""
    from app.domain import resources
    est, known = _estimate_ram_bytes(request)
    reserved_by_others = sum(v for k, v in _reserved_ram.items() if k != job_id)
    out = resources.preflight_memory_check(est + reserved_by_others, known=known)
    out["snapshot"]["reserved_by_others_bytes"] = reserved_by_others
    if out["decision"] != "block" and job_id:
        _reserved_ram[job_id] = est
    return out


def _release_memory_reservation(job_id: str) -> None:
    _reserved_ram.pop(job_id, None)
