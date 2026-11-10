"""M4.1 hotfix (#51) — stop()/reap lifecycle regression tests.

Invariant under test: a worker's record and RAM/VRAM reservation may only
be released after CONFIRMED OS process exit. `reaped=True` must never be
set while the process could still be alive (graceful-teardown lag,
taskkill lag, Job-Object-close lag, unkillable process).

Uses the real stdio mock worker; no inference anywhere.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from app.domain import proc_guard
from app.domain.workers.pool import WorkerPool
from app.domain.workers.resources import GiB, ResidencyRegistry
from app.domain.workers.types import WorkerKey, WorkerSpec, WorkerState

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


async def _await_release(reg: ResidencyRegistry, timeout: float = 15.0) -> None:
    """Wait for the pool sweeper to drop confirmed-dead records and
    release their reservations."""
    deadline = time.time() + timeout
    while reg.resident_totals() != (0, 0) and time.time() < deadline:
        await asyncio.sleep(0.2)
    assert reg.resident_totals() == (0, 0)


# ================================================================ exit confirmation

class TestStopReap:

    async def test_delayed_graceful_exit(self):
        """A worker that takes ~1.5s to die on shutdown: stop() must wait
        for real exit, not just the signal."""
        reg = _registry()
        pool = await _pool(reg)
        try:
            async with await pool.acquire(
                    _spec(env={"MOCK_EXIT_DELAY_S": "1.5"})) as w:
                pid = w.proc.pid
            t0 = time.monotonic()
            assert await w.stop(grace_s=10.0) is True
            assert not proc_guard.process_alive(pid)
            assert w.proc.poll() is not None
            assert w.reaped
        finally:
            await pool.stop()

    async def test_process_exit_confirmed(self):
        """stop() returning True guarantees the OS process is gone."""
        reg = _registry()
        pool = await _pool(reg)
        try:
            async with await pool.acquire(_spec()) as w:
                pid = w.proc.pid
            assert await w.stop() is True
            assert w.proc.poll() is not None
            assert not proc_guard.process_alive(pid)
            assert w.state is WorkerState.DEAD and w.reaped
        finally:
            await pool.stop()

    async def test_forced_kill_fallback(self):
        """A worker ignoring the shutdown command is taskkilled and the
        exit confirmed before reaped."""
        reg = _registry()
        pool = await _pool(reg)
        try:
            async with await pool.acquire(
                    _spec(env={"MOCK_IGNORE_SHUTDOWN": "1"})) as w:
                pid = w.proc.pid
            assert await w.stop(grace_s=0.5) is True
            assert w.proc.poll() is not None
            assert not proc_guard.process_alive(pid)
        finally:
            await pool.stop()

    async def test_reservation_held_while_alive(self, monkeypatch):
        """If exit can't be confirmed, the reservation and record stay —
        nothing is released while the process may still be alive."""
        reg = _registry()
        pool = await _pool(reg)
        try:
            async with await pool.acquire(
                    _spec(env={"MOCK_IGNORE_SHUTDOWN": "1"})) as w:
                pid = w.proc.pid
                wid = w.id
            # Make every kill channel a no-op: the process cannot be
            # confirmed dead, so stop() must refuse to reap.
            monkeypatch.setattr(proc_guard, "terminate_tree", lambda *a, **k: None)
            monkeypatch.setattr(proc_guard, "unsupervise", lambda *a, **k: None)
            assert await w.stop(grace_s=0.2) is False
            assert not w.reaped
            assert w.state is WorkerState.DEAD
            assert proc_guard.process_alive(pid)
            assert reg.resident_totals() != (0, 0)   # reservation retained
            monkeypatch.undo()                       # restore real kill paths
            assert await w.stop(grace_s=0.5) is True
            assert w.reaped and w.proc.poll() is not None
            await _await_release(reg)                # released after confirm
            _ = wid
        finally:
            await pool.stop()

    async def test_reservation_released_after_confirm(self):
        """Normal path: confirmed exit → reservation drops to zero."""
        reg = _registry()
        pool = await _pool(reg)
        try:
            async with await pool.acquire(_spec()) as w:
                assert reg.resident_totals() != (0, 0)
            assert await w.stop() is True
            await _await_release(reg)
        finally:
            await pool.stop()

    async def test_repeated_stop_calls(self):
        """stop() is idempotent — repeated and concurrent calls all
        succeed, no double-release, no error."""
        reg = _registry()
        pool = await _pool(reg)
        try:
            async with await pool.acquire(_spec()) as w:
                pid = w.proc.pid
            r1, r2, r3 = await asyncio.gather(
                w.stop(), w.stop(), w.stop())
            assert (r1, r2, r3) == (True, True, True)
            assert await w.stop() is True
            assert not proc_guard.process_alive(pid)
            await _await_release(reg)
        finally:
            await pool.stop()

    async def test_concurrent_stop_and_evict(self):
        """stop() racing pool eviction serializes on the lock — the
        worker is killed once, confirmed, and released once."""
        reg = _registry()
        pool = await _pool(reg)
        try:
            async with await pool.acquire(
                    _spec(env={"MOCK_EXIT_DELAY_S": "1.0"})) as w:
                pid = w.proc.pid
            stopped, _ = await asyncio.gather(
                w.stop(), pool._evict(w, "test concurrent evict"))
            assert stopped is True
            assert w.reaped
            assert not proc_guard.process_alive(pid)
            assert pool.live_count() == 0
            assert reg.resident_totals() == (0, 0)
        finally:
            await pool.stop()

    async def test_evict_while_shutdown_in_flight(self):
        """Eviction during a slow graceful shutdown still ends with a
        confirmed exit before the record/reservation drop."""
        reg = _registry()
        pool = await _pool(reg, idle_timeout_s=0.4)
        try:
            async with await pool.acquire(
                    _spec(env={"MOCK_EXIT_DELAY_S": "1.0"})) as w:
                pid = w.proc.pid
            stop_task = asyncio.create_task(w.stop())
            await asyncio.sleep(0.3)   # shutdown mid-flight when sweeper hits
            deadline = time.time() + 15
            while not w.reaped and time.time() < deadline:
                await asyncio.sleep(0.1)
            await stop_task
            assert w.reaped
            assert not proc_guard.process_alive(pid)
            deadline = time.time() + 15
            while pool.live_count() and time.time() < deadline:
                await asyncio.sleep(0.1)
            assert pool.live_count() == 0
            assert reg.resident_totals() == (0, 0)
        finally:
            await pool.stop()

    async def test_unconfirmed_keeps_record_for_retry(self, monkeypatch):
        """A dead-but-unreaped worker stays in the pool's records (never
        counted live) and the sweeper retries until confirmed."""
        reg = _registry()
        pool = await _pool(reg)
        try:
            async with await pool.acquire(
                    _spec(env={"MOCK_IGNORE_SHUTDOWN": "1"})) as w:
                pid = w.proc.pid
            monkeypatch.setattr(proc_guard, "terminate_tree", lambda *a, **k: None)
            monkeypatch.setattr(proc_guard, "unsupervise", lambda *a, **k: None)
            assert await pool._evict(w, "test") is None  # evict can't confirm
            assert not w.reaped
            assert pool.live_count() == 0              # dead, not live
            assert w.id in pool._workers               # record retained
            assert reg.resident_totals() != (0, 0)
            monkeypatch.undo()
            # Next sweep (or explicit stop) confirms and releases.
            deadline = time.time() + 15
            while reg.resident_totals() != (0, 0) and time.time() < deadline:
                await asyncio.sleep(0.2)
            assert reg.resident_totals() == (0, 0)
            assert not proc_guard.process_alive(pid)
        finally:
            await pool.stop()
