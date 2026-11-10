"""M4.1 Phase 2A — real sd-server integration, NO model inference.

Two layers:

- Stub-server tests: an in-process HTTP stub (tests/stub_sd_server.py)
  spawned as a supervised worker — exercises the real adapter, HTTP
  protocol, pool, supervisor, and reservation plumbing end-to-end.
- Real-binary tests: the pinned sd-server.exe itself, launched WITHOUT
  model weights — proves process supervision, readiness, health probing,
  port collision, and tree-kill on the actual runtime.

No generation request ever reaches a model-loaded server.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from app.adapters.base import JobCancelled, JobHandle
from app.adapters.sdcpp_server import SdCppServerAdapter
from app.config import Settings
from app.db.models import GenerationJob
from app.domain import proc_guard
from app.domain.workers import attempts as attempts_mod
from app.domain.workers.pool import WorkerPool
from app.domain.workers.ports import free_tcp_port
from app.domain.workers.protocol import SdServerHttpProtocol
from app.domain.workers.resources import GiB, ResidencyRegistry
from app.domain.workers.types import WorkerKey, WorkerSpec, WorkerState
from tests.conftest import make_project

STUB = str(Path(__file__).resolve().parent / "stub_sd_server.py")
MiB = 1024 * 1024

# Real pinned runtime — skip real-binary tests when it's not installed
# (CI has no runtimes; they live under the dev data dir).
_RUNTIME_ROOT = Path(os.environ.get("LOCALAPPDATA", ".")) / "AtomicAIStudio" / "runtimes"
SD_SERVER_EXES = sorted(_RUNTIME_ROOT.rglob("sd-server.exe")) if _RUNTIME_ROOT.exists() else []
requires_sd_server = pytest.mark.skipif(
    not SD_SERVER_EXES, reason="sd-server.exe not installed under runtimes/")


# ================================================================ helpers

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


def _stub_spec(port: int, gpu: int = -1, env: dict | None = None,
               model: str = "stub-model", ready_s: float = 10.0,
               hb_s: float = 1.0, grace_s: float = 4.0,
               ram_mb: int = 64, vram_mb: int = 64) -> WorkerSpec:
    e = dict(os.environ)
    e.update(env or {})
    return WorkerSpec(
        key=WorkerKey(model, "sdcpp", "stub", gpu, protocol="http"),
        argv=(sys.executable, STUB, "-l", "127.0.0.1",
              "--listen-port", str(port)),
        env=e,
        est_ram_bytes=ram_mb * MiB,
        est_vram_bytes=vram_mb * MiB,
        ready_timeout_s=ready_s,
        heartbeat_interval_s=hb_s,
        heartbeat_grace_s=grace_s,
        protocol_factory=lambda proc: SdServerHttpProtocol(
            f"http://127.0.0.1:{port}", proc=proc),
    )


def _real_spec(port: int, model_dir: Path, env: dict | None = None,
               ready_s: float = 20.0, grace_s: float = 8.0) -> WorkerSpec:
    exe = SD_SERVER_EXES[0]
    model_dir.mkdir(parents=True, exist_ok=True)
    e = dict(os.environ)
    e.update(env or {})
    return WorkerSpec(
        key=WorkerKey("none", "sdcpp", exe.parent.name, 0, protocol="http"),
        argv=(str(exe), "-l", "127.0.0.1", "--listen-port", str(port),
              "--model-dir", str(model_dir),
              "--log-level", "warn"),
        env=e,
        cwd=str(exe.parent),
        est_ram_bytes=1 * GiB,
        est_vram_bytes=1 * GiB,
        ready_timeout_s=ready_s,
        heartbeat_interval_s=1.0,
        heartbeat_grace_s=grace_s,
        protocol_factory=lambda proc: SdServerHttpProtocol(
            f"http://127.0.0.1:{port}", proc=proc),
    )


def _http_get(path_url: str, timeout: float = 3.0):
    try:
        with urllib.request.urlopen(path_url, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except Exception as e:
        return 0, {"error": str(e)}


def _adapter(pool: WorkerPool, tmp_path: Path, **kw) -> SdCppServerAdapter:
    return SdCppServerAdapter(
        None, Settings(data_dir=tmp_path / "data"), pool,
        argv_prefix=[sys.executable, STUB], **kw)


def _handle(tmp_path: Path, request: dict, job_id: str = "job-1") -> JobHandle:
    return JobHandle(job_id=job_id, project_id="p1",
                     request=request, work_dir=str(tmp_path / "work"))


async def _drive(adapter, tmp_path, request, job_id="job-1"):
    """Consume an adapter.run() generator to its final output or error."""
    final = None
    async for upd in adapter.run(_handle(tmp_path, request, job_id)):
        if upd.get("done"):
            final = upd["output"]
    return final


# ================================================================ stub: lifecycle + health

class TestStubWorkerLifecycle:
    async def test_start_ready_request_idle(self, tmp_path):
        pool = await _pool(_registry())
        try:
            port = free_tcp_port()
            async with await pool.acquire(_stub_spec(port)) as w:
                assert w.state is WorkerState.READY
                code, body = _http_get(f"http://127.0.0.1:{port}/v1/models")
                assert code == 200 and body["data"]
                r = await w.request("generate", {"prompt": "x"})
                assert r["ok"] is True
                assert r["body"]["data"][0]["b64_json"]
        finally:
            await pool.stop()

    async def test_health_probe_failure_kills_worker(self, tmp_path):
        """After readiness, /v1/models starts failing → heartbeat goes
        stale → worker is dead AND its process is actually terminated."""
        pool = await _pool(_registry())
        try:
            spec = _stub_spec(free_tcp_port(),
                              env={"STUB_MODELS_FAIL_AFTER": "2"},
                              hb_s=0.5, grace_s=1.5)
            async with await pool.acquire(spec) as w:
                pid = w.proc.pid
                assert await w.wait_dead(15.0)
                assert "heartbeat" in (w.death_reason or "")
                assert not proc_guard.process_alive(pid)
        finally:
            await pool.stop()

    async def test_unexpected_process_exit_detected(self, tmp_path):
        pool = await _pool(_registry())
        try:
            async with await pool.acquire(_stub_spec(free_tcp_port())) as w:
                pid = w.proc.pid
                w.proc.kill()  # simulate crash
                assert await w.wait_dead(10.0)
                assert w.state is WorkerState.DEAD
                assert not proc_guard.process_alive(pid)
        finally:
            await pool.stop()

    async def test_graceful_stop_kills_server(self, tmp_path):
        """sd-server has no shutdown endpoint — stop() must force-kill
        and actually reap the process."""
        pool = await _pool(_registry())
        try:
            w = await (await pool.acquire(_stub_spec(free_tcp_port()))).__aenter__()
            pid = w.proc.pid
            await w.stop()
            assert w.state is WorkerState.DEAD
            assert not proc_guard.process_alive(pid)
        finally:
            await pool.stop()

    async def test_process_tree_termination(self, tmp_path):
        pool = await _pool(_registry())
        try:
            async with await pool.acquire(
                    _stub_spec(free_tcp_port(), env={"STUB_CHILD": "1"})) as w:
                await asyncio.sleep(0.3)
                import psutil
                kids = psutil.Process(w.proc.pid).children(recursive=True)
                assert kids, "stub should have spawned a child"
                child_pid = kids[0].pid
            # lease released → idle; stop it explicitly — the Job Object
            # kills the child asynchronously, so wait for confirmation
            # rather than asserting on the same tick.
            await w.stop()
            deadline = time.time() + 10
            while proc_guard.process_alive(child_pid) and time.time() < deadline:
                await asyncio.sleep(0.1)
            assert not proc_guard.process_alive(child_pid)
        finally:
            await pool.stop()


# ================================================================ stub: port + protocol faults

class TestPortAndProtocol:
    async def test_port_collision_fails_spawn_fast(self, tmp_path):
        """Occupying the port → server exits immediately → readiness
        fails fast (proc-death check), spawn raises, record dropped."""
        import socket
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        pool = await _pool(_registry())
        try:
            with pytest.raises((TimeoutError, Exception)):
                await pool.acquire(_stub_spec(port, ready_s=5.0))
            assert pool.live_count() == 0
        finally:
            blocker.close()
            await pool.stop()

    async def test_adapter_retries_on_fresh_port(self, tmp_path):
        """Adapter allocates a different port per attempt — a collided
        first port must not kill the job."""
        import socket
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        bad_port = blocker.getsockname()[1]
        good_port = free_tcp_port()
        ports = iter([bad_port, good_port, free_tcp_port()])
        pool = await _pool(_registry())
        try:
            adapter = _adapter(pool, tmp_path,
                               port_allocator=lambda: next(ports))
            out = await _drive(adapter, tmp_path,
                               {"prompt": "x", "ready_timeout_s": 5.0})
            assert Path(out["path"]).exists()
        finally:
            blocker.close()
            await pool.stop()

    async def test_http_request_timeout_recycles(self, tmp_path):
        """A hung request → TimeoutError → worker dead + process killed
        + reservations released — never returned to idle."""
        reg = _registry()
        pool = await _pool(reg)
        try:
            spec = _stub_spec(free_tcp_port(), env={"STUB_MODE": "hang"})
            async with await pool.acquire(spec) as w:
                pid = w.proc.pid
                with pytest.raises(TimeoutError):
                    await w.request("generate", {"prompt": "x"},
                                    timeout_s=1.0)
                assert w.state is WorkerState.DEAD
            assert await w.wait_dead(10.0)
            assert not proc_guard.process_alive(pid)
            assert reg.resident_totals() == (0, 0)
            assert pool.live_count() == 0
        finally:
            await pool.stop()

    async def test_malformed_response_recycles(self, tmp_path):
        """200 + non-JSON body → transport-level failure → worker
        recycled (outcome cannot be trusted)."""
        pool = await _pool(_registry())
        try:
            spec = _stub_spec(free_tcp_port(), env={"STUB_MODE": "malformed"})
            async with await pool.acquire(spec) as w:
                pid = w.proc.pid
                with pytest.raises(RuntimeError):
                    await w.request("generate", {"prompt": "x"},
                                    timeout_s=5.0)
            assert await w.wait_dead(10.0)
            assert not proc_guard.process_alive(pid)
        finally:
            await pool.stop()

    async def test_coherent_error_worker_survives(self, tmp_path):
        """HTTP 500 with a JSON body is a REAL answer — the worker stays
        alive and is reused for the next request."""
        pool = await _pool(_registry())
        try:
            port = free_tcp_port()
            spec = _stub_spec(port, env={"STUB_MODE": "error"})
            async with await pool.acquire(spec) as w:
                pid = w.proc.pid
                r = await w.request("generate", {"prompt": "x"})
                assert r["ok"] is False and r["http"] == 500
                assert w.is_alive()
                # Still the same worker — coherent errors don't recycle.
                r2 = await w.request("generate", {"prompt": "y"})
                assert r2["ok"] is False
                assert w.proc.pid == pid
        finally:
            await pool.stop()

    async def test_dropped_connection_recycles(self, tmp_path):
        """Connection closed mid-request with no response → transport
        failure → recycle."""
        pool = await _pool(_registry())
        try:
            spec = _stub_spec(free_tcp_port(), env={"STUB_MODE": "drop"})
            async with await pool.acquire(spec) as w:
                pid = w.proc.pid
                with pytest.raises(RuntimeError):
                    await w.request("generate", {"prompt": "x"},
                                    timeout_s=5.0)
            assert await w.wait_dead(10.0)
            assert not proc_guard.process_alive(pid)
        finally:
            await pool.stop()


# ================================================================ stub: adapter end-to-end

class TestAdapterEndToEnd:
    async def test_job_produces_output_and_attempt(self, tmp_path):
        pool = await _pool(_registry())
        try:
            adapter = _adapter(pool, tmp_path)
            out = await _drive(adapter, tmp_path, {"prompt": "hello"})
            assert Path(out["path"]).exists()
            assert out["sha256"]
            assert out["media"]["adapter_id"] == "gen.image.sdcpp-server"
            assert out["media"]["worker_model"] == "persistent-pool"
        finally:
            await pool.stop()

    async def test_cancel_abandons_and_recycles(self, tmp_path):
        """JobCancelled → worker killed; late response can't commit."""
        reg = _registry()
        pool = await _pool(reg)
        try:
            import threading as _t
            cancel_ev = _t.Event()
            adapter = _adapter(pool, tmp_path)
            # The stub inherits os.environ through the spec env — hang the
            # request so the cancel poll fires first.
            os.environ["STUB_MODE"] = "hang"
            try:
                async def run_job():
                    cancel_ev.set()  # cancelled before it can answer
                    async for _ in adapter.run(
                            JobHandle(job_id="jc", project_id="p",
                                      request={"prompt": "x",
                                               "worker_ram_mb": 64,
                                               "worker_vram_mb": 64},
                                      work_dir=str(tmp_path / "w"),
                                      cancel_event=cancel_ev)):
                        pass
                with pytest.raises(JobCancelled):
                    await run_job()
            finally:
                os.environ.pop("STUB_MODE", None)
            assert reg.resident_totals() == (0, 0)
            assert pool.live_count() == 0
        finally:
            await pool.stop()

    async def test_reservation_lifetime_and_cleanup(self, tmp_path):
        reg = _registry()
        pool = await _pool(reg)
        try:
            adapter = _adapter(pool, tmp_path)
            async def go(jid):
                async for _ in adapter.run(
                        _handle(tmp_path, {"prompt": "x", "worker_ram_mb": 64,
                                           "worker_vram_mb": 64}, jid)):
                    pass
            await go("j1")
            # Worker still resident → reservation still charged.
            ram, vram = reg.resident_totals()
            assert ram == 64 * MiB and vram == 64 * MiB
            await go("j2")  # same key → worker reuse, not second reservation
            assert reg.resident_totals() == (64 * MiB, 64 * MiB)
            await pool.stop()
            assert reg.resident_totals() == (0, 0)
        finally:
            await pool.stop()

    async def test_gpu_identity_isolated(self, tmp_path):
        """Different assigned_gpu → different WorkerKey → separate
        workers; GPU pin lands in the worker env."""
        reg = _registry(gpus={0: 8 * GiB, 1: 8 * GiB})
        pool = await _pool(reg, max_per_gpu=2)
        try:
            adapter = _adapter(pool, tmp_path)
            specs = [adapter._spec({"assigned_gpu": g, "worker_ram_mb": 64,
                                    "worker_vram_mb": 64},
                                   "j", port=free_tcp_port(),
                                   model_key="m", weight=None,
                                   exe=None, flavor="vulkan")
                     for g in (0, 1)]
            assert specs[0].key != specs[1].key
            assert specs[0].env.get("GGML_VULKAN_DEVICE") == "0"
            assert specs[1].env.get("GGML_VULKAN_DEVICE") == "1"
            async with await pool.acquire(specs[0]):
                pass
            async with await pool.acquire(specs[1]):
                pass
            gpus_seen = {i.key.gpu_index for i in pool.snapshot()}
            assert gpus_seen == {0, 1}
        finally:
            await pool.stop()

    async def test_admission_refusal_is_honest(self, tmp_path):
        reg = _registry(ram_free=512 * MiB)  # not enough for 4 GiB floor
        pool = await _pool(reg)
        try:
            adapter = _adapter(pool, tmp_path)
            with pytest.raises(RuntimeError, match="worker unavailable"):
                await _drive(adapter, tmp_path, {"prompt": "x"})
        finally:
            await pool.stop()


# ================================================================ engine-level

class TestEngineIntegration:
    def test_registered_behind_flag(self, client):
        """Default builds must NOT expose the persistent adapter."""
        assert client.app.state.adapter_registry.get(
            "gen.image.sdcpp-server") is None

    def test_engine_job_through_stub_worker(self, client, tmp_path):
        """Injected stub-argv adapter → enqueue → pool worker → output
        asset committed + attempt recorded. device=cpu pins the lane."""
        adapter = _adapter(
            client.app.state.worker_pool, tmp_path)
        reg = client.app.state.adapter_registry
        reg._adapters["gen.image.sdcpp-server"] = adapter
        proj = make_project(client, "M42A")
        eng = client.app.state.job_engine
        with client.app.state.session_factory() as s:
            job = eng.enqueue(
                s, project_id=proj["id"], adapter_id="gen.image.sdcpp-server",
                kind="image",
                request={"prompt": "stub", "device": "cpu",
                         "worker_ram_mb": 64, "worker_vram_mb": 64,
                         # M4.0 preflight applies to adapter_kind="local"
                         # — acknowledge like a real request would.
                         "memory_risk_acknowledged": True},
                idempotency_key="m42a-stub-0001")
            s.commit()
            eng.wake()
            jid = job.id
        deadline = time.time() + 45
        status = None
        while time.time() < deadline:
            row = next((j for j in client.get("/api/jobs").json()
                        if j["id"] == jid), None)
            if row and row["status"] in ("succeeded", "failed", "cancelled"):
                status = row
                break
            time.sleep(0.3)
        assert status is not None and status["status"] == "succeeded", \
            (status or {}).get("error")
        assert status["output_asset_id"]
        with client.app.state.session_factory() as s:
            attempts = attempts_mod.list_attempts(s.get(GenerationJob, jid))
        assert len(attempts) == 1
        assert attempts[0]["outcome"] == "succeeded"
        assert attempts[0]["worker_id"]


# ================================================================ real binary (no weights)

@requires_sd_server
class TestRealSdServer:
    async def test_no_model_startup_and_readiness(self, tmp_path):
        """The real sd-server binds and answers /v1/models with no
        weights loaded — lazy model-dir mode is a valid worker state."""
        pool = await _pool(_registry())
        try:
            port = free_tcp_port()
            spec = _real_spec(port, tmp_path / "md")
            async with await pool.acquire(spec) as w:
                assert w.state is WorkerState.READY
                code, body = _http_get(f"http://127.0.0.1:{port}/v1/models")
                assert code == 200
                assert isinstance(body.get("data"), list)
        finally:
            await pool.stop()

    async def test_real_forced_exit_detected_and_reaped(self, tmp_path):
        pool = await _pool(_registry())
        try:
            async with await pool.acquire(_real_spec(free_tcp_port(), tmp_path / "md")) as w:
                pid = w.proc.pid
                proc_guard.terminate_tree(pid, grace_s=3.0)
                assert await w.wait_dead(10.0)
                assert not proc_guard.process_alive(pid)
        finally:
            await pool.stop()

    async def test_real_graceful_stop(self, tmp_path):
        pool = await _pool(_registry())
        try:
            w = await (await pool.acquire(
                _real_spec(free_tcp_port(), tmp_path / "md"))).__aenter__()
            pid = w.proc.pid
            await w.stop()
            assert not proc_guard.process_alive(pid)
        finally:
            await pool.stop()

    async def test_real_port_collision_fails(self, tmp_path):
        import socket
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        pool = await _pool(_registry())
        try:
            with pytest.raises(Exception):
                await pool.acquire(_real_spec(port, tmp_path / "md", ready_s=15.0))
            assert pool.live_count() == 0
        finally:
            blocker.close()
            await pool.stop()

    async def test_real_stdout_is_drained(self, tmp_path):
        """sd-server logs to stdout — the drain thread must capture it
        (a full pipe would otherwise deadlock the server)."""
        pool = await _pool(_registry())
        try:
            spec = _real_spec(free_tcp_port(), tmp_path / "md")
            async with await pool.acquire(spec) as w:
                await asyncio.sleep(0.5)
                tail = w.protocol.log_tail(50)
                assert tail, "expected sd-server startup logs on stdout"
        finally:
            await pool.stop()
