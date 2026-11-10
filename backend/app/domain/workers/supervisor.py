"""Supervised worker lifecycle (M4.1 Phase 1).

One SupervisedWorker owns exactly one OS process, always under
proc_guard Job-Object supervision (spawn suspended → assign → resume,
kill-on-close). No worker outlives its owning backend: the Job Object
guarantees tree-kill when the backend dies, however it dies.

States: starting → ready → busy → idle → draining → dead.
Dead is terminal — a crashed worker is never reused; the pool decides
whether to spawn a replacement.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
import uuid

from app.domain import proc_guard
from app.domain.workers.protocol import StdioJsonLinesProtocol, WorkerProtocol
from app.domain.workers.types import WorkerInfo, WorkerSpec, WorkerState

log = logging.getLogger(__name__)


class WorkerDeadError(RuntimeError):
    """The worker process is dead or failed health checks."""


class SupervisedWorker:
    def __init__(self, spec: WorkerSpec, *,
                 protocol: WorkerProtocol | None = None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.spec = spec
        self.key = spec.key
        self.state = WorkerState.STARTING
        self.proc: subprocess.Popen | None = None
        self.protocol = protocol
        self.started_monotonic: float | None = None
        self.idle_since: float | None = None
        self.current_job_id: str | None = None
        self.requests_completed = 0
        self.death_reason: str | None = None
        # reaped == CONFIRMED OS exit. Never set while the process could
        # still be alive — records/reservations may only drop after this.
        self.reaped = False
        self._ready_at: float | None = None
        self._monitor_task: asyncio.Task | None = None
        self._dead_ev = asyncio.Event()
        self._next_probe: float = 0.0
        self._stop_lock = asyncio.Lock()

    # ---------- lifecycle ----------

    async def spawn(self) -> None:
        """Start the OS process, supervised, and wait for protocol
        readiness. Failure → dead, process terminated, raises."""
        try:
            self.proc = proc_guard.spawn_supervised(
                list(self.spec.argv), env=self.spec.env or None,
                cwd=self.spec.cwd, stdin=subprocess.PIPE,
            )
        except Exception:
            self.state = WorkerState.DEAD
            self.death_reason = "spawn failed"
            raise
        self.started_monotonic = time.monotonic()
        if self.protocol is None:
            factory = self.spec.protocol_factory
            self.protocol = (factory(self.proc) if factory is not None
                             else StdioJsonLinesProtocol(self.proc))
        self._monitor_task = asyncio.create_task(
            self._monitor(), name=f"worker-mon-{self.id}")
        try:
            ready = await self.protocol.wait_ready(self.spec.ready_timeout_s)
        except Exception as e:
            ready = False
            log.warning("worker %s readiness error: %s", self.id, e)
        if not ready:
            self.mark_dead("readiness timeout")
            await self._force_kill()
            raise TimeoutError(
                f"worker {self.id} not ready after {self.spec.ready_timeout_s}s")
        self.state = WorkerState.READY
        self._ready_at = time.monotonic()
        self.idle_since = time.monotonic()

    def mark_dead(self, reason: str) -> None:
        if self.state is not WorkerState.DEAD:
            self.state = WorkerState.DEAD
            self.death_reason = reason
            log.warning("worker %s (pid %s) dead: %s",
                        self.id, self.proc.pid if self.proc else None, reason)
        self._dead_ev.set()

    def is_alive(self) -> bool:
        return (self.state not in (WorkerState.DEAD, WorkerState.DRAINING)
                and self.proc is not None and self.proc.poll() is None)

    # ---------- work ----------

    async def request(self, cmd: str, payload: dict, *,
                      job_id: str | None = None,
                      on_progress=None, is_cancelled=None,
                      timeout_s: float = 600.0) -> dict:
        if not self.is_alive():
            raise WorkerDeadError(f"worker {self.id} not alive ({self.death_reason or self.state})")
        if self.protocol is None:
            raise WorkerDeadError("worker has no protocol channel")
        self.state = WorkerState.BUSY
        self.current_job_id = job_id
        self.idle_since = None
        try:
            result = await self.protocol.request(
                cmd, payload, on_progress=on_progress,
                is_cancelled=is_cancelled, timeout_s=timeout_s)
        except (TimeoutError, RuntimeError):
            # Timeout or broken channel: the worker's execution state is
            # now UNKNOWN — it may still be running the request. Never
            # return it to the idle pool; mark dead so the monitor kills
            # it and the lease drops the record (recycle, don't reuse).
            self.mark_dead("request timed out or channel broke — recycling")
            raise
        finally:
            self.current_job_id = None
            if self.state is WorkerState.BUSY:
                self.state = WorkerState.IDLE
                self.idle_since = time.monotonic()
                self.requests_completed += 1
        return result

    # ---------- monitoring ----------

    async def _monitor(self) -> None:
        """Watchdog: process liveness + heartbeat deadline. The pool
        reads state; this task only marks the worker dead."""
        while self.state is not WorkerState.DEAD:
            await asyncio.sleep(0.5)
            if self.proc is not None and self.proc.poll() is not None:
                self.mark_dead(f"process exited (code {self.proc.returncode})")
                break
            # Heartbeat enforcement applies when the spec declares one —
            # a worker that never sends its first hb after readiness is
            # just as dead as one whose heartbeats stop. Protocols without
            # push heartbeats (HTTP) get an active probe at the declared
            # cadence; a successful probe refreshes last_heartbeat.
            if (self.protocol is not None
                    and self.spec.heartbeat_interval_s > 0
                    and self.state not in (WorkerState.STARTING,)):
                now = time.monotonic()
                if now >= self._next_probe:
                    self._next_probe = now + self.spec.heartbeat_interval_s
                    try:
                        await self.protocol.health_probe()
                    except Exception:
                        pass  # probe failure just doesn't refresh the hb
                base = self.protocol.last_heartbeat() or self._ready_at
                if base is not None and now - base > self.spec.heartbeat_grace_s:
                    self.mark_dead(
                        f"heartbeat stale ({now - base:.1f}s "
                        f"> {self.spec.heartbeat_grace_s}s)")
                    break
        # A dead-marked worker must actually be dead — heartbeat-stale
        # processes are still alive and must be terminated before the
        # pool reaps the record. Retry until exit is confirmed; if it can
        # never be confirmed the record stays dead-but-unreaped (its
        # reservation is retained) and the pool sweeper keeps retrying.
        for _ in range(3):
            async with self._stop_lock:
                if self.reaped or await self._force_kill(2.0):
                    break
            await asyncio.sleep(2.0)
        self._dead_ev.set()

    async def wait_dead(self, timeout_s: float = 10.0) -> bool:
        """True once the worker is marked dead AND its process has
        actually exited — a dead-marked-but-still-running process is
        killed by the monitor, and callers must be able to rely on the
        OS process being gone when this returns."""
        try:
            await asyncio.wait_for(self._dead_ev.wait(), timeout_s)
        except asyncio.TimeoutError:
            return False
        if self.proc is not None and self.proc.poll() is None:
            try:
                await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        None, self.proc.wait), timeout=5.0)
            except asyncio.TimeoutError:
                return False
        return True

    # ---------- shutdown ----------

    async def stop(self, grace_s: float = 5.0) -> bool:
        """Graceful protocol shutdown → wait → forced tree kill → CONFIRM.

        Returns True only when the OS process is verifiably gone. If exit
        cannot be confirmed within the bounded sequence, returns False and
        leaves the worker dead-but-unreaped: the caller must keep its
        record and reservation, and the pool sweeper will retry the kill.
        Idempotent — concurrent/repeat stops serialize on the lock.
        """
        async with self._stop_lock:
            if self.reaped:
                return True
            if self.state is WorkerState.DEAD and self.proc is None:
                self.reaped = True
                return True
            self.state = WorkerState.DRAINING
            if self.protocol is not None:
                try:
                    await self.protocol.shutdown(grace_s)
                except Exception:
                    pass
            confirmed = await self._force_kill(grace_s)
            self.mark_dead("stopped" if confirmed
                           else "stop: exit unconfirmed — retaining")
            if confirmed:
                if self._monitor_task is not None:
                    self._monitor_task.cancel()
                    try:
                        await self._monitor_task
                    except asyncio.CancelledError:
                        pass
            return confirmed

    async def _await_exit(self, timeout_s: float) -> bool:
        """Wait up to timeout for the process to exit. True = confirmed.

        Polls rather than awaiting proc.wait in an executor: a blocked
        Popen.wait on an undead process would strand a thread-pool worker
        forever, and enough of those exhaust the default executor and
        stall the whole event loop's executor work."""
        if self.proc is None:
            return True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            if self.proc.poll() is not None:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)

    async def _force_kill(self, grace_s: float = 5.0) -> bool:
        """Terminate and CONFIRM exit. reaped=True only once the process
        is verifiably dead — a timed-out kill leaves reaped=False so the
        record/reservation is retained and the kill retried later."""
        if self.proc is None:
            self.reaped = True
            return True
        pid = self.proc.pid
        # 1) graceful/shutdown-driven exit window
        if await self._await_exit(grace_s):
            self._reap()
            return True
        # 2) forced termination; taskkill returns before OS teardown
        #    completes, so confirm exit after each attempt
        import functools
        loop = asyncio.get_running_loop()
        for _ in range(2):
            await loop.run_in_executor(
                None, functools.partial(proc_guard.terminate_tree,
                                        pid, grace_s=grace_s))
            if await self._await_exit(5.0):
                self._reap()
                return True
        # 3) last resort: closing the Job Object handle kills the tree
        #    (KILL_ON_JOB_CLOSE) — then still require confirmed exit.
        proc_guard.unsupervise(pid)
        if await self._await_exit(5.0):
            self._reap(unsupervised=True)
            return True
        log.error("worker %s (pid %d) unkillable — keeping record "
                  "and reservation", self.id, pid)
        return False

    def _reap(self, *, unsupervised: bool = False) -> None:
        """Final teardown bookkeeping — ONLY after confirmed OS exit."""
        pid = self.proc.pid if self.proc else None
        if pid is not None and not unsupervised:
            proc_guard.unsupervise(pid)
        try:
            if self.proc is not None and self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        self.reaped = True

    # ---------- introspection ----------

    def info(self) -> WorkerInfo:
        return WorkerInfo(
            worker_id=self.id, key=self.key, state=self.state,
            pid=self.proc.pid if self.proc else None,
            gpu_index=self.key.gpu_index,
            ram_bytes=self.spec.est_ram_bytes,
            vram_bytes=self.spec.est_vram_bytes,
            current_job_id=self.current_job_id,
            idle_since=self.idle_since,
            last_heartbeat=self.protocol.last_heartbeat() if self.protocol else None,
            requests_completed=self.requests_completed,
        )
