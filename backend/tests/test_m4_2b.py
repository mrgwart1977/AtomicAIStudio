"""M4.1 Phase 2B — sd-server request contract + recovery, NO inference.

The stub server (tests/stub_sd_server.py) now captures the POST body the
adapter sends and exposes it on GET /v1/last_request — making the
request contract a verifiable, regression-protected surface instead of
an assumed superset. Also covered: both accepted response shapes,
crash-mid-request worker recovery, and the no-image-data parse path.

No model weights, no real binary, no inference anywhere.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

from app.adapters.base import JobHandle
from app.adapters.sdcpp_server import SdCppServerAdapter
from app.config import Settings
from app.domain.workers.pool import WorkerPool
from app.domain.workers.resources import GiB, ResidencyRegistry

STUB = str(Path(__file__).resolve().parent / "stub_sd_server.py")
MiB = 1024 * 1024


def _registry(ram_free: int = 32 * GiB,
              gpus: dict[int, int] | None = None) -> ResidencyRegistry:
    gpus = gpus or {0: 8 * GiB}
    return ResidencyRegistry(
        ram_free_bytes=lambda: ram_free,
        gpu_free_bytes=lambda i, _g=gpus: _g.get(i),
    )


async def _pool(registry: ResidencyRegistry, **kw) -> WorkerPool:
    kw.setdefault("idle_timeout_s", 60.0)
    kw.setdefault("monitor_interval_s", 0.1)
    p = WorkerPool(registry, **kw)
    await p.start()
    return p


def _adapter(pool: WorkerPool, tmp_path: Path, **kw) -> SdCppServerAdapter:
    return SdCppServerAdapter(
        None, Settings(data_dir=tmp_path / "data"), pool,
        argv_prefix=[sys.executable, STUB], **kw)


def _handle(tmp_path: Path, request: dict, job_id: str = "job-1") -> JobHandle:
    return JobHandle(job_id=job_id, project_id="p1",
                     request=request, work_dir=str(tmp_path / "work"))


def _request(request: dict | None = None) -> dict:
    r = {"prompt": "a lighthouse", "negative_prompt": "fog",
         "seed": 42, "width": 512, "height": 640,
         "steps": 20, "cfg_scale": 7.5,
         "worker_ram_mb": 64, "worker_vram_mb": 64}
    r.update(request or {})
    return r


async def _drive(adapter, tmp_path, request, job_id="job-1"):
    """Consume run() fully; collect attempt events + final output."""
    attempts, final = [], None
    async for upd in adapter.run(_handle(tmp_path, request, job_id)):
        if upd.get("attempt"):
            attempts.append(upd["attempt"])
        if upd.get("done"):
            final = upd["output"]
    return attempts, final


def _stub_base(pool: WorkerPool) -> str | None:
    """Base URL of the resident worker's HTTP server, if any."""
    for w in pool._workers.values():
        return getattr(getattr(w, "protocol", None), "_base", None)
    return None


def _get_json(url: str, timeout: float = 3.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


# ================================================================ request contract

class TestRequestContract:
    async def test_payload_fields_reach_the_server(self, tmp_path):
        """The exact POST body the adapter emits is captured by the stub —
        pins the OpenAI/A1111 superset field-for-field."""
        pool = await _pool(_registry())
        try:
            adapter = _adapter(pool, tmp_path)
            await _drive(adapter, tmp_path, _request())
            base = _stub_base(pool)
            assert base, "worker should still be resident"
            sent = _get_json(f"{base}/v1/last_request")
            assert sent["prompt"] == "a lighthouse"
            assert sent["negative_prompt"] == "fog"
            assert sent["seed"] == 42
            assert sent["size"] == "512x640"
            assert sent["steps"] == 20
            assert sent["cfg_scale"] == 7.5
            assert sent["n"] == 1
            # Model name is None in stub mode (no resolved weight file).
            assert sent.get("model") is None
        finally:
            await pool.stop()

    async def test_defaults_for_unset_fields(self, tmp_path):
        pool = await _pool(_registry())
        try:
            adapter = _adapter(pool, tmp_path)
            await _drive(adapter, tmp_path,
                         {"prompt": "x", "worker_ram_mb": 64,
                          "worker_vram_mb": 64})
            sent = _get_json(f"{_stub_base(pool)}/v1/last_request")
            assert sent["seed"] == -1
            assert sent["size"] == "512x512"
            assert sent["steps"] == 20
            assert sent["cfg_scale"] == 7.0
        finally:
            await pool.stop()


# ================================================================ response shapes

class TestResponseShapes:
    async def test_a1111_images_shape_accepted(self, tmp_path):
        """A1111-style {"images":[b64]} parses + writes the asset."""
        os.environ["STUB_MODE"] = "a1111"
        pool = await _pool(_registry())
        try:
            adapter = _adapter(pool, tmp_path)
            _, out = await _drive(adapter, tmp_path, _request())
            assert out["sha256"]
            assert Path(out["path"]).exists()
        finally:
            os.environ.pop("STUB_MODE", None)
            await pool.stop()

    async def test_no_image_data_fails_honestly(self, tmp_path):
        """200 with no image payload → RuntimeError, not a silent pass."""
        os.environ["STUB_MODE"] = "noimg"
        pool = await _pool(_registry())
        try:
            adapter = _adapter(pool, tmp_path)
            with pytest.raises(RuntimeError, match="no image data"):
                await _drive(adapter, tmp_path, _request())
        finally:
            os.environ.pop("STUB_MODE", None)
            await pool.stop()


# ================================================================ recovery

class TestWorkerRecovery:
    async def test_crash_mid_request_then_fresh_worker(self, tmp_path):
        """Worker dies mid-request → job fails honestly → next job is
        served by a NEW worker process (no dead-worker reuse)."""
        reg = _registry()
        pool = await _pool(reg)
        try:
            adapter = _adapter(pool, tmp_path)
            os.environ["STUB_MODE"] = "crash"
            with pytest.raises(Exception):
                await _drive(adapter, tmp_path, _request(), "j-crash")
            # The dead worker's reservation is released after reaping.
            deadline = asyncio.get_running_loop().time() + 15
            while reg.resident_totals() != (0, 0) and \
                    asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.2)
            assert reg.resident_totals() == (0, 0)
            # Fresh env → next worker runs in ok mode → job succeeds.
            os.environ["STUB_MODE"] = "ok"
            attempts, out = await _drive(adapter, tmp_path, _request(), "j-ok")
            assert out["sha256"]
            assert attempts and attempts[0]["worker_id"]
        finally:
            os.environ.pop("STUB_MODE", None)
            await pool.stop()

    async def test_cancelled_job_leaves_no_resident_work(self, tmp_path):
        """Cancellation recycles the worker — no request can outlive the
        job that issued it."""
        reg = _registry()
        pool = await _pool(reg)
        try:
            adapter = _adapter(pool, tmp_path)
            cancel_ev = threading.Event()
            os.environ["STUB_MODE"] = "hang"
            try:
                from app.adapters.base import JobCancelled
                h = JobHandle(job_id="jc2", project_id="p",
                              request=_request(),
                              work_dir=str(tmp_path / "w2"),
                              cancel_event=cancel_ev)
                cancel_ev.set()
                with pytest.raises(JobCancelled):
                    async for _ in adapter.run(h):
                        pass
            finally:
                os.environ.pop("STUB_MODE", None)
            deadline = asyncio.get_running_loop().time() + 15
            while reg.resident_totals() != (0, 0) and \
                    asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.2)
            assert reg.resident_totals() == (0, 0)
            assert pool.live_count() == 0
        finally:
            await pool.stop()
