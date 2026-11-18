"""A7 simulated GPU-bound worker — deterministic, in-process, zero
inference.

``mock.gpu.sim`` exists so multi-GPU scheduling, VRAM reservation
accounting, queueing, cancellation, crash handling, and restart
reconciliation can be exercised end-to-end without a real GPU or real
inference. Clearly labeled ``adapter_kind="mock"`` — it is a scheduler
test double, never presented as a model.

Request hooks (all test-facing, deterministic):
- ``est_vram_bytes`` — footprint estimate fed to scheduler admission
- ``requires``     — backend requirements ("cuda"/"vulkan"/"directml")
- ``device``       — "auto" | "cpu" | <index> pin (scheduler contract)
- ``work_ms``      — simulated work duration (default 200 ms)
- ``crash_after_ms`` — raise mid-run (worker-crash path)
- ``fail``         — fail immediately at run start
- ``model_key``    — reported in output provenance
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, AsyncIterator

from app.adapters.base import CapabilityProfile, JobCancelled, JobHandle


class GpuSimAdapter:
    profile = CapabilityProfile(
        adapter_id="mock.gpu.sim",
        display_name="Simulated GPU Worker (A7)",
        adapter_kind="mock",
        tasks=("sim.gpu.work",),
        supports={"device": True, "est_vram_bytes": True, "requires": True},
        limits={"work_ms": 60000},
        requires_cloud=False,
        cost_model="free",
        lane="gen",
        # The whole point: this adapter acquires a scheduler reservation
        # so multi-GPU admission/queueing/release can be exercised for
        # real — while the "work" itself is a deterministic sleep.
        needs_gpu=True,
        worker_model="in-process-sim",
        # Matches the generation contract — an interrupted job is never
        # silently resumed; reconcile marks it failed/interrupted.
        resumable=False,
        notes="A7 mock: deterministic simulated GPU work. Not inference.",
    )

    async def validate(self, request: dict[str, Any]) -> list[str]:
        errs = []
        if request.get("est_vram_mb") is not None and \
                int(request["est_vram_mb"]) <= 0:
            errs.append("est_vram_mb must be positive")
        if request.get("device") is not None and not (
                request["device"] == "auto" or request["device"] == "cpu"
                or isinstance(request["device"], int)):
            errs.append("device must be 'auto', 'cpu', or a GPU index")
        return errs

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        req = handle.request
        work_ms = int(req.get("work_ms", 200))
        crash_after = req.get("crash_after_ms")
        gpu = req.get("assigned_gpu", -1)
        started = time.monotonic()

        if req.get("fail"):
            raise RuntimeError("simulated adapter failure (request.fail)")

        tick = 50
        elapsed = 0
        while elapsed < work_ms:
            if handle.cancel_event is not None and \
                    handle.cancel_event.is_set():
                raise JobCancelled()
            if crash_after is not None and \
                    elapsed >= int(crash_after):
                raise RuntimeError("simulated worker crash "
                                   f"(crash_after_ms={crash_after})")
            await asyncio.sleep(min(tick, work_ms - elapsed) / 1000)
            elapsed = int((time.monotonic() - started) * 1000)
            yield {"progress": min(0.99, elapsed / max(1, work_ms)),
                   "message": f"simulated work on "
                              f"{'CPU' if gpu is None or gpu < 0 else f'GPU {gpu}'}"}

        out_dir = Path(handle.work_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{handle.job_id}.txt"
        out_path.write_text(
            f"mock.gpu.sim job={handle.job_id} gpu={gpu} "
            f"work_ms={work_ms}\n", encoding="utf-8")
        yield {
            "done": True,
            "progress": 1.0,
            "output": {
                "path": str(out_path), "kind": "text",
                "name": f"gpusim-{handle.job_id[:8]}.txt",
                "origin": "generation",
                "media": {
                    "source": "mock", "generated": False, "is_mock": True,
                    "adapter_id": self.profile.adapter_id,
                    "worker_model": "in-process-sim",
                    "assigned_gpu": gpu,
                    "device_request": req.get("device"),
                    "est_vram_bytes": req.get("est_vram_bytes"),
                    "model_key": req.get("model_key"),
                },
            },
        }
