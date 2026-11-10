"""M4.1 mock adapter on the persistent-worker path.

Proves pool → supervisor → protocol → output → commit end-to-end with
zero inference. Clearly labeled ``adapter_kind="mock"`` like every
other mock. When no WorkerPool is injected it falls back to a transient
subprocess-per-job worker — the same execution shape the runtime used
before M4.1.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, AsyncIterator

from app.adapters.base import CapabilityProfile, JobCancelled, JobHandle
from app.domain.workers.pool import WorkerPool, WorkerUnavailableError
from app.domain.workers.supervisor import SupervisedWorker, WorkerDeadError
from app.domain.workers.types import WorkerKey, WorkerSpec

_MOCK_WORKER = Path(__file__).resolve().parent.parent.parent / "tests" / "mock_worker.py"


class PersistentMockAdapter:
    profile = CapabilityProfile(
        adapter_id="mock.persistent-image",
        display_name="Mock Persistent Worker (M4.1)",
        adapter_kind="mock",
        tasks=("text2image",),
        supports={"seed": True, "size": True},
        limits={"width": 512, "height": 512},
        requires_cloud=False,
        cost_model="free",
        lane="gen",
        # The mock worker is a Python subprocess — it never allocates a
        # GPU. Declaring needs_gpu would make the scheduler stamp an
        # assigned_gpu and fail-closed VRAM admission rightly refuses on
        # machines whose GPU VRAM can't be probed (e.g. GPU-less CI).
        needs_gpu=False,
        worker_model="persistent-pool",
        resumable=False,
        notes="M4.1 mock: persistent-worker path with subprocess fallback. Not AI inference.",
    )

    def __init__(self, pool: WorkerPool | None = None,
                 worker_argv: list[str] | None = None) -> None:
        self._pool = pool
        self._worker_argv = worker_argv or [sys.executable, str(_MOCK_WORKER)]

    async def validate(self, request: dict[str, Any]) -> list[str]:
        if not (request.get("prompt") or "").strip():
            return ["prompt is required"]
        return []

    # ---------- worker spec ----------

    def _spec(self, request: dict[str, Any]) -> WorkerSpec:
        gpu = int(request.get("assigned_gpu", -1))
        env = dict(os.environ)
        # Test hooks the mock worker understands — passed through only
        # because this adapter is itself a test double.
        for k in ("MOCK_NO_HB", "MOCK_CRASH_AFTER", "MOCK_CHILD",
                  "MOCK_GEN_DELAY_S", "MOCK_NO_READY"):
            if k in request:
                env[k] = str(request[k])
        return WorkerSpec(
            key=WorkerKey(
                model_key=request.get("model_key") or "mock-model",
                runtime_key="mock-stdio",
                flavor="mock",
                gpu_index=gpu,
            ),
            argv=tuple(self._worker_argv),
            env=env,
            est_ram_bytes=int(request.get("worker_ram_mb", 64)) * 1024 * 1024,
            # Nonzero default: a GPU worker with an unknown VRAM cost is
            # refused by fail-closed admission (as designed).
            est_vram_bytes=int(request.get("worker_vram_mb", 64)) * 1024 * 1024,
            ready_timeout_s=float(request.get("ready_timeout_s", 15.0)),
            heartbeat_grace_s=float(request.get("heartbeat_grace_s", 8.0)),
        )

    def _fallback_spec(self, spec: WorkerSpec) -> WorkerSpec:
        """Transient one-shot worker — same supervision, no residency."""
        return spec

    # ---------- run ----------

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        spec = self._spec(handle.request)
        out_dir = Path(handle.work_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{handle.job_id}.png"
        payload = {
            "out_path": str(out_path),
            "prompt": handle.request.get("prompt", ""),
            "seed": handle.request.get("seed", 0),
            "steps": int(handle.request.get("steps", 4)),
        }
        progress_events: list[dict] = []

        async def drive(worker: SupervisedWorker) -> dict:
            result = await worker.request(
                "generate", payload, job_id=handle.job_id,
                on_progress=progress_events.append,
                is_cancelled=lambda: bool(
                    handle.cancel_event and handle.cancel_event.is_set()),
                timeout_s=float(handle.request.get("request_timeout_s", 120.0)))
            return result

        if self._pool is not None:
            try:
                lease = await self._pool.acquire(spec)
            except WorkerUnavailableError as e:
                raise RuntimeError(f"persistent worker unavailable: {e}") from e
            async with lease as worker:
                yield {"attempt": {"worker_id": worker.id},
                       "message": f"persistent worker {worker.id} (gpu {spec.key.gpu_index})"}
                if detail := handle.request.get("emit_detail"):
                    yield {"detail": detail}  # test hook: hostile detail merge
                result = await drive(worker)
        else:
            worker = SupervisedWorker(self._fallback_spec(spec))
            try:
                await worker.spawn()
                yield {"attempt": {"worker_id": worker.id},
                       "message": "transient worker (subprocess-per-job fallback)"}
                result = await drive(worker)
            finally:
                await worker.stop()

        if not result.get("ok"):
            err = result.get("error") or "worker request failed"
            if err == "cancelled":
                raise JobCancelled()
            raise RuntimeError(f"mock worker: {err}")

        sha = result.get("sha256") or ""
        yield {
            "done": True,
            "progress": 1.0,
            "output": {
                "path": str(out_path), "sha256": sha, "kind": "image",
                "size": [512, 512],
                "name": f"mockpw-{handle.job_id[:8]}.png", "origin": "generation",
                "media": {
                    "source": "mock", "generated": False, "is_mock": True,
                    "adapter_id": self.profile.adapter_id,
                    "worker_model": "persistent-pool" if self._pool else "subprocess-per-job",
                    "prompt": payload["prompt"], "seed": payload["seed"],
                },
            },
        }
