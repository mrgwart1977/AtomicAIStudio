"""M4.1 Phase 1 — persistent worker foundation tests.

Two layers:

- Unit: WorkerPool / SupervisedWorker / ResidencyRegistry driven with
  the real stdio mock worker and fake resource probes.
- Engine: the real app (TestClient) running jobs through the real pool
  wired in lifespan — end-to-end proof the durable attempt + commit
  boundary behaves.

No AI inference anywhere; the mock worker is a Python stdio process.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from app.db.models import GenerationJob
from app.domain import proc_guard
from app.domain.workers import attempts as attempts_mod
from app.domain.workers.pool import WorkerPool, WorkerUnavailableError
from app.domain.workers.resources import GiB, ResidencyRegistry
from app.domain.workers.supervisor import WorkerDeadError
from app.domain.workers.types import WorkerKey, WorkerSpec, WorkerState
from tests.conftest import make_project

WORKER = str(Path(__file__).resolve().parent / "mock_worker.py")
MiB = 1024 * 1024


def _spec(gpu=0, env=None, model="mock-model", ram_mb=64, vram_mb=64,
          ready_s=15.0, grace_s=8.0, hb_s=1.0, flavor="mock") -> WorkerSpec:
    e = dict(os.environ)
    e.update(env or {})
    return WorkerSpec(
        key=WorkerKey(model, "mock-stdio", flavor, gpu),
        argv=(sys.executable, WORKER),
        env=e,
        est_ram_bytes=ram_mb * MiB,
        est_vram_bytes=vram_mb * MiB,
        ready_timeout_s=ready_s,
        heartbeat_interval_s=hb_s,
        heartbeat_grace_s=grace_s,
    )


def _registry(ram_free: int = 32 * GiB,
              gpus: dict[int, int] | None = None) -> ResidencyRegistry:
    gpus = gpus or {0: 8 * GiB}
    return ResidencyRegistry(
        ram_free_bytes=lambda: ram_free,
        gpu_free_bytes=lambda i, _g=gpus: _g.get(i),
    )


async def _pool(registry: ResidencyRegistry, **kw) -> WorkerPool:
    kw.setdefault("idle_timeout_s", 2.0)
    kw.setdefault("monitor_interval_s", 0.1)
    p = WorkerPool(registry, **kw)
    await p.start()
    return p


def _gen_payload(tmp_path: Path, name: str = "out.png") -> dict:
    return {"out_path": str(tmp_path / name), "prompt": "t",
            "seed": 1, "steps": 2}


def _enqueue(client, project_id: str | None, key: str,
             request: dict | None = None) -> str:
    eng = client.app.state.job_engine
    with client.app.state.session_factory() as s:
        job = eng.enqueue(
            s, project_id=project_id, adapter_id="mock.persistent-image",
            kind="image", request=request or {"prompt": "m4.1 test"},
            idempotency_key=key)
        s.commit()
        eng.wake()
        return job.id


def _wait_job(client, jid: str, timeout: float = 45.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = next((j for j in client.get("/api/jobs").json()
                    if j["id"] == jid), None)
        if job and job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.3)
    raise AssertionError("job did not reach a terminal state")


def _attempts(client, jid: str) -> list[dict]:
    with client.app.state.session_factory() as s:
        job = s.get(GenerationJob, jid)
        return attempts_mod.list_attempts(job)


# ================================================================ unit: lifecycle

class TestWorkerLifecycle:
    async def test_startup_and_readiness(self, tmp_path):
        pool = await _pool(_registry())
        try:
            async with await pool.acquire(_spec()) as w:
                assert w.state is WorkerState.READY
                assert w.proc is not None and w.proc.poll() is None
                r = await w.request("generate", _gen_payload(tmp_path))
                assert r["ok"] is True
                assert Path(r["path"]).exists()
        finally:
            await pool.stop()

    async def test_readiness_timeout_fails_closed(self, tmp_path):
        pool = await _pool(_registry())
        try:
            with pytest.raises(TimeoutError):
                await pool.acquire(_spec(env={"MOCK_NO_READY": "1"}, ready_s=1.0))
            assert pool.live_count() == 0
        finally:
            await pool.stop()

    async def test_heartbeat_failure_marks_dead(self, tmp_path):
        pool = await _pool(_registry())
        try:
            async with await pool.acquire(
                    _spec(env={"MOCK_NO_HB": "1"}, grace_s=1.5)) as w:
                pid = w.proc.pid
                assert await w.wait_dead(10.0)
                assert w.state is WorkerState.DEAD
                assert "heartbeat" in (w.death_reason or "")
                assert not proc_guard.process_alive(pid)
        finally:
            await pool.stop()

    async def test_worker_crash_detected_and_reaped(self, tmp_path):
        pool = await _pool(_registry())
        try:
            async with await pool.acquire(_spec(env={"MOCK_CRASH_AFTER": "1"})) as w:
                pid = w.proc.pid
                with pytest.raises((RuntimeError, WorkerDeadError)):
                    await w.request("generate", _gen_payload(tmp_path))
                assert await w.wait_dead(10.0)
                assert not proc_guard.process_alive(pid)
            await asyncio.sleep(0.3)
            # Dead record reaped — a fresh worker serves the next acquire.
            async with await pool.acquire(_spec()) as w2:
                assert w2.id != w.id
                assert (await w2.request("generate", _gen_payload(tmp_path)))["ok"]
        finally:
            await pool.stop()

    async def test_worker_reuse_across_requests(self, tmp_path):
        pool = await _pool(_registry())
        try:
            async with await pool.acquire(_spec()) as w1:
                wid1 = w1.id
                await w1.request("generate", _gen_payload(tmp_path, "a.png"))
            async with await pool.acquire(_spec()) as w2:
                assert w2.id == wid1  # resident worker reused, no respawn
                await w2.request("generate", _gen_payload(tmp_path, "b.png"))
        finally:
            await pool.stop()

    async def test_graceful_shutdown_releases_process(self, tmp_path):
        pool = await _pool(_registry())
        w = await (await pool.acquire(_spec())).__aenter__()
        pid = w.proc.pid
        await w.stop()
        assert not proc_guard.process_alive(pid)
        assert w.state is WorkerState.DEAD
        await pool.stop()

    async def test_process_tree_termination(self, tmp_path):
        """Worker that forked a child: both die when the worker is
        terminated — Job Object kill-on-close owns the whole tree."""
        pool = await _pool(_registry())
        try:
            async with await pool.acquire(_spec(env={"MOCK_CHILD": "1"})) as w:
                child_pid = None
                # child pid was reported in the ready event — pull it via
                # a ping and the protocol's ready payload is already gone;
                # instead, find any python child of the worker process.
                await asyncio.sleep(0.3)
                import psutil
                kids = psutil.Process(w.proc.pid).children(recursive=True)
                assert kids, "worker should have spawned a child"
                child_pid = kids[0].pid
                assert proc_guard.process_alive(child_pid)
            # Releasing the lease leaves it idle — force eviction to kill.
            await w.stop()
            await asyncio.sleep(0.5)
            assert not proc_guard.process_alive(child_pid)
        finally:
            await pool.stop()


# ================================================================ unit: cancel + evict

class TestCancelAndEviction:
    async def test_cancel_mid_request_worker_survives(self, tmp_path):
        pool = await _pool(_registry())
        try:
            spec = _spec(env={"MOCK_GEN_DELAY_S": "0.2"})
            async with await pool.acquire(spec) as w:
                flag = {"set": False}
                task = asyncio.create_task(w.request(
                    "generate",
                    {"out_path": str(tmp_path / "o.png"), "prompt": "t",
                     "steps": 12},
                    is_cancelled=lambda: flag["set"]))
                await asyncio.sleep(0.45)
                flag["set"] = True
                result = await task
                assert result["ok"] is False
                assert result.get("error") == "cancelled"
                assert w.is_alive()
                assert (await w.request("generate", _gen_payload(tmp_path)))["ok"]
        finally:
            await pool.stop()

    async def test_request_timeout_recycles_worker(self, tmp_path):
        """A timed-out request leaves worker state UNKNOWN — the worker
        may still be executing. It is killed + dropped, never returned
        to the idle pool for reuse."""
        pool = await _pool(_registry())
        try:
            async with await pool.acquire(_spec()) as w:
                pid = w.proc.pid
                with pytest.raises(TimeoutError):
                    await w.request(
                        "generate",
                        {"out_path": str(tmp_path / "o.png"), "prompt": "t",
                         "steps": 40},          # ~8s of work
                        timeout_s=0.5)
                assert w.state is WorkerState.DEAD
            assert await w.wait_dead(10.0)
            assert not proc_guard.process_alive(pid)
            assert pool.live_count() == 0
        finally:
            await pool.stop()

    async def test_idle_eviction(self, tmp_path):
        pool = await _pool(_registry(), idle_timeout_s=0.5)
        try:
            async with await pool.acquire(_spec()) as w:
                pid = w.proc.pid
            deadline = time.time() + 8
            while pool.live_count() and time.time() < deadline:
                await asyncio.sleep(0.2)
            assert pool.live_count() == 0
            assert not proc_guard.process_alive(pid)
        finally:
            await pool.stop()


# ================================================================ unit: admission

class TestAdmission:
    async def test_vram_refusal(self, tmp_path):
        reg = _registry(gpus={0: 200 * MiB})
        pool = await _pool(reg)
        try:
            with pytest.raises(WorkerUnavailableError, match="VRAM"):
                await pool.acquire(_spec(vram_mb=4096))
        finally:
            await pool.stop()

    async def test_ram_refusal(self, tmp_path):
        reg = _registry(ram_free=512 * MiB)
        pool = await _pool(reg)
        try:
            with pytest.raises(WorkerUnavailableError, match="RAM"):
                await pool.acquire(_spec(ram_mb=8192))
        finally:
            await pool.stop()

    async def test_unknown_estimate_fail_closed(self, tmp_path):
        reg = _registry()
        pool = await _pool(reg)
        try:
            with pytest.raises(WorkerUnavailableError, match="unknown"):
                await pool.acquire(_spec(vram_mb=0))
        finally:
            await pool.stop()

    async def test_reservation_charges_and_cleanup(self, tmp_path):
        reg = _registry()
        pool = await _pool(reg)
        try:
            async with await pool.acquire(_spec(ram_mb=128, vram_mb=256)) as w:
                ram, vram = reg.resident_totals(0)
                assert ram == 128 * MiB and vram == 256 * MiB
            # Still resident — lease release does not free residency.
            assert reg.resident_totals(0) == (128 * MiB, 256 * MiB)
            await pool.stop()
            assert reg.resident_totals() == (0, 0)
        finally:
            await pool.stop()

    async def test_admission_evicts_lru_idle(self, tmp_path):
        # 3.5 GiB free − 2 GiB headroom: 1 GiB worker fits; a second
        # 768 MiB worker does not (0.5 GiB left) until m1 is evicted.
        reg = _registry(ram_free=int(3.5 * GiB))
        pool = await _pool(reg)
        try:
            async with await pool.acquire(_spec(model="m1", ram_mb=1024)):
                pass  # resident: 1 GiB charged
            async with await pool.acquire(_spec(model="m2", ram_mb=768)):
                pass
            snap = {i.key.model_key for i in pool.snapshot()}
            assert snap == {"m2"}
        finally:
            await pool.stop()


# ================================================================ unit: concurrency + multi-GPU

class TestConcurrencyAndMultiGpu:
    async def test_per_gpu_cap_evicts_idle_for_new_key(self, tmp_path):
        pool = await _pool(_registry(gpus={0: 8 * GiB}), max_per_gpu=1)
        try:
            async with await pool.acquire(_spec(model="a")) as w1:
                # Same GPU, different key — the resident is idle, so the
                # pool evicts it to satisfy the cap, not refuses.
                async with await pool.acquire(_spec(model="b")) as w2:
                    assert w2.key.gpu_index == 0
            assert pool.live_count() == 1
        finally:
            await pool.stop()

    async def test_multi_gpu_independent_pools(self, tmp_path):
        pool = await _pool(
            _registry(gpus={0: 8 * GiB, 1: 8 * GiB}), max_per_gpu=1)
        try:
            async with await pool.acquire(_spec(gpu=0, model="a")):
                pass
            async with await pool.acquire(_spec(gpu=1, model="a")):
                pass
            states = {(i.key.gpu_index, i.state) for i in pool.snapshot()}
            assert states == {(0, WorkerState.IDLE), (1, WorkerState.IDLE)}
        finally:
            await pool.stop()

    async def test_busy_pool_reports_unavailable(self, tmp_path):
        pool = await _pool(_registry(), max_total=1)
        try:
            async with await pool.acquire(_spec()) as w:
                flag = {"set": False}
                task = asyncio.create_task(w.request(
                    "generate",
                    {"out_path": str(tmp_path / "o.png"), "prompt": "t",
                     "steps": 40},
                    is_cancelled=lambda: flag["set"]))
                await asyncio.sleep(0.4)
                with pytest.raises(WorkerUnavailableError, match="busy"):
                    await pool.acquire(_spec(model="other"))
                flag["set"] = True
                await task
        finally:
            await pool.stop()


# ================================================================ remote provider mock

class TestRemoteProviderContract:
    async def test_idempotent_submit_dedupes(self):
        from app.domain.workers.remote import MockRemoteProvider, SubmissionIntent
        p = MockRemoteProvider()
        intent = SubmissionIntent("j1", "ok-key-1", "cloud.img", {"prompt": "x"},
                                  est_cost_usd=0.01)
        h1 = await p.submit(intent)
        h2 = await p.submit(intent)
        assert h1.remote_id == h2.remote_id
        assert len(p.submission_log) == 2  # both calls recorded…
        assert len(p._handles) == 1        # …but only one billable handle

    async def test_unknown_outcome_never_resubmits_silently(self):
        from app.domain.workers.remote import MockRemoteProvider, SubmissionIntent
        p = MockRemoteProvider()
        intent = SubmissionIntent("j2", "lost-1", "cloud.img", {}, 0.02)
        with pytest.raises(ConnectionError):
            await p.submit(intent)
        # Provider-side the handle MAY exist; the same idempotency key
        # still dedupes — the contract never creates a second handle.
        h = await p.submit(intent)
        assert h.remote_id is not None
        assert len(p._handles) == 1

    async def test_cancel_is_best_effort(self):
        from app.domain.workers.remote import (
            MockRemoteProvider, RemoteStatus, SubmissionIntent)
        p = MockRemoteProvider()
        h = await p.submit(SubmissionIntent("j3", "slow-1", "cloud.img", {}))
        assert h.status == RemoteStatus.RUNNING
        cancelled = await p.cancel(h.remote_id)
        assert cancelled.status == RemoteStatus.CANCELLED


# ================================================================ engine-level

class TestEngineIntegration:
    def test_persistent_mock_job_succeeds_with_attempt(self, client):
        proj = make_project(client, "M41")
        jid = _enqueue(client, proj["id"], "m41-persistent-ok-0001")
        job = _wait_job(client, jid)
        assert job["status"] == "succeeded", job.get("error")
        assert job["output_asset_id"]
        attempts = _attempts(client, jid)
        assert len(attempts) == 1
        assert attempts[0]["outcome"] == "succeeded"
        assert attempts[0]["worker_id"]

    def test_adapter_detail_cannot_clobber_attempts(self, client):
        """Adapter-emitted progress detail must never overwrite the
        engine-owned attempt history."""
        proj = make_project(client, "M41Clobber")
        jid = _enqueue(client, proj["id"], "m41-clobber-0001",
                       {"prompt": "x",
                        "emit_detail": {"attempts": [{"attempt_id": "forged"}],
                                        "note": "detail"}})
        job = _wait_job(client, jid)
        assert job["status"] == "succeeded"
        attempts = _attempts(client, jid)
        assert len(attempts) == 1
        assert attempts[0]["attempt_id"] != "forged"
        assert attempts[0]["outcome"] == "succeeded"
        with client.app.state.session_factory() as s:
            row = s.get(GenerationJob, jid)
            assert row.progress_json.get("note") == "detail"

    def test_fallback_when_no_pool(self, client):
        """Adapter without a pool runs a transient subprocess worker —
        the pre-M4.1 execution shape stays intact."""
        from app.adapters.persistent_mock import PersistentMockAdapter
        eng = client.app.state.job_engine
        original = eng._registry.get("mock.persistent-image")
        eng._registry._adapters["mock.persistent-image"] = PersistentMockAdapter(None)
        try:
            proj = make_project(client, "M41FB")
            jid = _enqueue(client, proj["id"], "m41-fallback-0001")
            job = _wait_job(client, jid)
            assert job["status"] == "succeeded", job.get("error")
        finally:
            eng._registry._adapters["mock.persistent-image"] = original

    def test_duplicate_execution_prevented(self, client):
        proj = make_project(client, "M41Dup")
        key = "m41-dup-key-0001"
        j1 = _enqueue(client, proj["id"], key)
        j2 = _enqueue(client, proj["id"], key)
        assert j1 == j2
        job = _wait_job(client, j1)
        assert job["status"] == "succeeded"
        assert len(_attempts(client, j1)) == 1

    def test_worker_dead_attempt_and_job_failure(self, client):
        proj = make_project(client, "M41Crash")
        jid = _enqueue(client, proj["id"], "m41-crash-0001",
                       {"prompt": "boom", "MOCK_CRASH_AFTER": "1"})
        job = _wait_job(client, jid)
        assert job["status"] == "failed"
        assert len(_attempts(client, jid)) >= 1

    def test_restart_reconciliation_records_new_attempt(self, client):
        """A job left 'running' (crash simulation) reconciles to queued
        with attempt_no bumped; the next run records a second attempt."""
        proj = make_project(client, "M41Restart")
        jid = _enqueue(client, proj["id"], "m41-restart-0001")
        job = _wait_job(client, jid)
        assert job["status"] == "succeeded"
        with client.app.state.session_factory() as s:
            row = s.get(GenerationJob, jid)
            row.status = "running"
            row.completed_at = None
            # Simulate a crash mid-execution: the open attempt still
            # reads 'running' — reconciliation must close it honestly.
            pj = dict(row.progress_json or {})
            pj["attempts"][0]["outcome"] = "running"
            pj["attempts"][0]["ended_at"] = None
            row.progress_json = pj
            s.commit()
            requeued = client.app.state.job_engine.reconcile_interrupted(s)
            s.commit()
            row = s.get(GenerationJob, jid)
            assert row.status == "queued"
            assert row.attempt_no == 2
            assert requeued == 1
            interrupted = attempts_mod.list_attempts(row)
            assert interrupted[0]["outcome"] == "interrupted"
            assert interrupted[0]["ended_at"]
        client.app.state.job_engine.wake()
        job2 = _wait_job(client, jid)
        assert job2["status"] == "succeeded"
        attempts = _attempts(client, jid)
        assert len(attempts) == 2
        assert attempts[0]["outcome"] == "interrupted"
        assert attempts[1]["attempt_no"] == 2
        assert attempts[1]["outcome"] == "succeeded"

    def test_pool_respects_data_dir_lock(self, client):
        """The pool lives inside the backend that owns the data-dir
        lock — workers can't outlive it (Job Objects cover the OS side)."""
        pool = client.app.state.worker_pool
        assert pool is not None
        assert client.app.state.data_dir_lock.path.exists()
