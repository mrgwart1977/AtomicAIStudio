"""Worker pool: keyed reuse, caps, LRU/idle eviction, admission.

A worker is keyed by (model, runtime, flavor, GPU, protocol) — a lease
never crosses keys. Caps are enforced globally and per-GPU. When the
pool is full or admission fails, the least-recently-used IDLE worker is
evicted first; a BUSY worker is never evicted. If nothing can be
evicted, acquisition raises WorkerUnavailable — the job engine keeps
the job queued at the lane level, so "busy" surfaces honestly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from app.domain.workers.resources import ResidencyRegistry
from app.domain.workers.supervisor import SupervisedWorker, WorkerDeadError
from app.domain.workers.types import WorkerInfo, WorkerSpec, WorkerState

log = logging.getLogger(__name__)


class WorkerUnavailableError(RuntimeError):
    """No worker can serve this request right now and none can be
    spawned (caps/admission) nor evicted."""


class WorkerPool:
    def __init__(
        self,
        registry: ResidencyRegistry,
        *,
        max_total: int = 2,
        max_per_gpu: int = 1,
        idle_timeout_s: float = 300.0,
        monitor_interval_s: float = 1.0,
    ) -> None:
        self._registry = registry
        self._max_total = max_total
        self._max_per_gpu = max_per_gpu
        self._idle_timeout = idle_timeout_s
        self._interval = monitor_interval_s
        self._workers: dict[str, SupervisedWorker] = {}
        self._by_key: dict[tuple, str] = {}
        self._lock = asyncio.Lock()
        self._monitor_task: asyncio.Task | None = None
        self._stopping = False

    # ---------- lifecycle ----------

    async def start(self) -> None:
        self._stopping = False
        self._monitor_task = asyncio.create_task(self._monitor_loop(),
                                                 name="worker-pool")

    async def stop(self) -> None:
        """Drain everything — workers never outlive the backend."""
        self._stopping = True
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task
        async with self._lock:
            workers = list(self._workers.values())
        await asyncio.gather(*(w.stop() for w in workers), return_exceptions=True)
        async with self._lock:
            for w in workers:
                if w.reaped:
                    self._registry.release(w.id)
                    self._workers.pop(w.id, None)
                    self._by_key.pop(self._key(w.spec), None)
                else:
                    # Confirmed-exit failed — keep record + reservation.
                    # The Job Object still guarantees OS-level cleanup
                    # when the backend exits; log honestly.
                    log.error("pool.stop: worker %s exit unconfirmed — "
                              "record retained", w.id)

    # ---------- acquisition ----------

    def _key(self, spec: WorkerSpec) -> tuple:
        k = spec.key
        return (k.model_key, k.runtime_key, k.flavor, k.gpu_index, k.protocol)

    def _live(self, w: SupervisedWorker) -> bool:
        return w.state in (WorkerState.READY, WorkerState.IDLE, WorkerState.BUSY)

    def _count_gpu(self, gpu_index: int) -> int:
        return sum(1 for w in self._workers.values()
                   if self._live(w) and w.key.gpu_index == gpu_index)

    def _live_count(self) -> int:
        return sum(1 for w in self._workers.values() if self._live(w))

    def _lru_idle(self) -> SupervisedWorker | None:
        idle = [w for w in self._workers.values()
                if w.state in (WorkerState.READY, WorkerState.IDLE)]
        if not idle:
            return None
        return min(idle, key=lambda w: w.idle_since or 0.0)

    async def _evict(self, w: SupervisedWorker, reason: str) -> None:
        log.info("evicting worker %s (%s)", w.id, reason)
        if await w.stop():
            self._registry.release(w.id)
            self._workers.pop(w.id, None)
            self._by_key.pop(self._key(w.spec), None)
        # else: exit unconfirmed — the worker stays dead-but-unreaped in
        # the records so its reservation is retained; the monitor's dead-
        # worker branch retries the kill on the next sweep.

    async def acquire(self, spec: WorkerSpec) -> "WorkerLease":
        """Return a lease on a live worker for spec.key, spawning one if
        needed and permitted. Raises WorkerUnavailableError."""
        async with self._lock:
            wid = self._by_key.get(self._key(spec))
            if wid is not None:
                w = self._workers.get(wid)
                if w is not None and self._live(w):
                    return WorkerLease(self, w)
                self._by_key.pop(self._key(spec), None)

            # Reap fully-killed dead workers before counting caps — a
            # dead-marked worker whose monitor is still killing it keeps
            # its record (and its reservation) until reaped=True.
            for w in [w for w in self._workers.values()
                      if w.state is WorkerState.DEAD and w.reaped]:
                self._registry.release(w.id)
                self._workers.pop(w.id, None)
                self._by_key.pop(self._key(w.spec), None)

            attempts = 0
            while attempts < 2:
                attempts += 1
                if self._live_count() >= self._max_total:
                    lru = self._lru_idle()
                    if lru is None:
                        raise WorkerUnavailableError(
                            f"worker pool full ({self._max_total}) and all workers busy")
                    await self._evict(lru, "pool capacity — LRU idle evict")
                if spec.key.gpu_index >= 0 and self._count_gpu(spec.key.gpu_index) >= self._max_per_gpu:
                    lru = self._lru_idle()
                    if lru is None or lru.key.gpu_index != spec.key.gpu_index:
                        raise WorkerUnavailableError(
                            f"GPU {spec.key.gpu_index} worker cap "
                            f"({self._max_per_gpu}) reached and no evictable worker")
                    await self._evict(lru, "per-GPU cap — LRU idle evict")
                ok, reason = self._registry.admit(
                    spec.est_ram_bytes, spec.est_vram_bytes, spec.key.gpu_index)
                if ok:
                    break
                lru = self._lru_idle()
                if lru is not None and attempts < 2:
                    await self._evict(lru, f"admission pressure ({reason})")
                    continue
                raise WorkerUnavailableError(
                    f"admission refused: {reason}") from None

            w = SupervisedWorker(spec)
            self._workers[w.id] = w
            self._by_key[self._key(spec)] = w.id
        try:
            await w.spawn()
        except Exception:
            async with self._lock:
                self._workers.pop(w.id, None)
                self._by_key.pop(self._key(spec), None)
            raise
        self._registry.reserve(w.id, spec.est_ram_bytes,
                               spec.est_vram_bytes, spec.key.gpu_index)
        return WorkerLease(self, w)

    # ---------- lease ----------

    def release_lease(self, worker: SupervisedWorker) -> None:
        if worker.state in (WorkerState.READY, WorkerState.BUSY):
            worker.state = WorkerState.IDLE
            worker.idle_since = time.monotonic()

    # ---------- monitor ----------

    async def _monitor_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._interval)
            async with self._lock:
                now = time.monotonic()
                for w in list(self._workers.values()):
                    if w.state is WorkerState.DEAD:
                        if w.reaped:
                            self._registry.release(w.id)
                            self._workers.pop(w.id, None)
                            self._by_key.pop(self._key(w.spec), None)
                        else:
                            # Dead-marked but process not yet confirmed
                            # gone — retry the kill, keep the reservation.
                            await w._force_kill(2.0)
                        continue
                    if (w.state in (WorkerState.READY, WorkerState.IDLE)
                            and w.idle_since is not None
                            and now - w.idle_since > self._idle_timeout):
                        await self._evict(w, "idle timeout")
                        continue
                if self._registry.under_pressure():
                    lru = self._lru_idle()
                    if lru is not None:
                        await self._evict(lru, "memory pressure")

    # ---------- introspection ----------

    def snapshot(self) -> list[WorkerInfo]:
        return [w.info() for w in self._workers.values()]

    def live_count(self) -> int:
        return self._live_count()


class WorkerLease:
    """Async context manager — marks the worker busy, releases to idle."""

    def __init__(self, pool: WorkerPool, worker: SupervisedWorker) -> None:
        self._pool = pool
        self.worker = worker

    async def __aenter__(self) -> SupervisedWorker:
        return self.worker

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if exc_type is WorkerDeadError or self.worker.state is WorkerState.DEAD:
            # Stop inline — the record (and reservation) is only dropped
            # once the process is confirmed gone; an unconfirmed kill
            # leaves a dead-unreaped record the pool sweeper keeps
            # retrying so nothing is released while the OS process lives.
            if await self.worker.stop():
                self._pool._registry.release(self.worker.id)
                self._pool._workers.pop(self.worker.id, None)
                self._pool._by_key.pop(self._pool._key(self.worker.spec), None)
            return
        self._pool.release_lease(self.worker)
