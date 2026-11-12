"""C1.5 ImportWatcher Stage B — opt-in background polling.

One shared asyncio task polls every provider whose persisted
``settings.import_behavior == "poll"`` (set only via the audited
``bridge.watch.enable`` command — off by default). All filesystem work
runs on a single worker thread, never on the FastAPI event loop, and one
scan per provider may be in flight at a time.

Safety invariants (all Stage A logic reused):
- validate_watch_folder gates EVERY poll — symlink/junction/traversal
  rejection and containment are identical to the manual scan.
- scan_watch_folder is stat-only, non-recursive, and bounded; file
  contents are never read here.
- Folder invalid/missing/inaccessible → ``degraded`` + skip; it resumes
  only when the ORIGINAL designated folder validates again — the service
  never follows a moved or replaced path.
- Each watch session carries a (generation, fingerprint) pair — the
  resolved designated folder. A scan scheduled for an older generation
  or a different folder is discarded at collect time and can never
  publish into a newer session's cache. Disabled or reconfigured
  providers' in-flight scans are moved to ``_orphans`` and tracked to
  completion even though their results are dead.
- Pauses under critical memory pressure (resources.pressure_state) and
  while a generation job is running.
- Candidates are in-memory only and can never become assets without an
  explicit per-file bridge.import — polling changes nothing on disk.

Shutdown: entry/candidate bounds limit WORK ITEMS, not wall-clock — a
wedged filesystem syscall (e.g. a dead network mount) is beyond
cooperative cancellation. stop() therefore waits a bounded grace for
in-flight scans, cancels queued ones, and poisons the worker — a
still-running call finishes on the worker thread with results dead.

Why a raw daemon thread instead of ThreadPoolExecutor:
concurrent.futures registers ``_python_exit``, which JOINS every pool
worker at interpreter shutdown regardless of the worker's daemon flag —
so a stalled scan inside an executor can still wedge process exit even
with shutdown(wait=False). A plain ``threading.Thread(daemon=True)`` is
genuinely abandoned at interpreter exit (``threading._shutdown`` only
joins non-daemon threads), and ``concurrent.futures.Future`` still gives
the same done()/result()/cancel()/callback surface the pool did. This
is the practical bound short of process isolation; per Stage A the
watched surface is one user-designated directory, so the residual risk
is a stalled local stat, not runaway work.
"""

from __future__ import annotations

import asyncio
import itertools
import queue
import threading
from concurrent.futures import Future, wait
from datetime import datetime, timezone
from typing import Any, Callable

from app.db.models import GenerationJob
from app.domain import bridge, resources
from app.domain.providers import catalog, registry

POLL_INTERVAL_S = 60.0
POLL_MIN_INTERVAL_S = 30.0
CACHE_MAX_PER_PROVIDER = 200
CACHE_MAX_TOTAL = 2000
SHUTDOWN_SCAN_GRACE_S = 5.0

_OFF = {"enabled": False, "status": "off", "reason": None,
        "folder_name": None, "candidates": {}, "last_poll": None,
        "paused_reason": None, "fingerprint": None, "gen": 0}


class ImportWatchService:
    def __init__(self, session_factory, *,
                 interval_s: float = POLL_INTERVAL_S,
                 shutdown_grace_s: float = SHUTDOWN_SCAN_GRACE_S,
                 pressure: Callable[[], str] = resources.pressure_state):
        self._sf = session_factory
        self._interval = max(float(interval_s), POLL_MIN_INTERVAL_S)
        self._grace = float(shutdown_grace_s)
        self._pressure = pressure
        self._stop = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task | None = None
        # One worker thread for ALL filesystem access — scans serialize by
        # construction, so two scans can never run concurrently. Raw
        # daemon thread + FIFO queue, NOT ThreadPoolExecutor: see module
        # docstring — _python_exit joins pool workers at interpreter
        # shutdown even when daemon; a raw daemon thread is truly
        # abandoned, so a wedged filesystem call cannot block exit.
        self._work_q: queue.Queue = queue.Queue()
        self._worker = threading.Thread(
            target=self._worker_loop, name="watch-scan", daemon=True)
        self._worker.start()
        # provider_key -> (future, gen, fingerprint). Results publish only
        # if gen+fingerprint still match the live state at collect time.
        self._pending: dict[str, tuple[Any, int, str]] = {}
        # Scans orphaned by disable/reconfigure — tracked to completion
        # even though their results are never published.
        self._orphans: set[Any] = set()
        self._state: dict[str, dict] = {}

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the loop, wait a bounded grace for in-flight scans, then
        cancel anything still queued. An asyncio cancel cannot stop a
        running os call — scans that outlive the grace finish on the
        daemon worker with results discarded."""
        self._stopping = True
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except BaseException:
                # CancelledError is BaseException on modern Python — a
                # cancelled task is the EXPECTED outcome here.
                pass
            self._task = None
        inflight = ([f for f, _g, _p in self._pending.values()]
                    + list(self._orphans))
        self._pending.clear()
        if inflight:
            wait(inflight, timeout=self._grace)
        # Cancel queued-but-unstarted scans (their futures fire done-
        # callbacks → orphan tracking drains), then poison the worker —
        # a running call finishes on the daemon thread, results dead.
        try:
            while True:
                _fn, _args, fut = self._work_q.get_nowait()
                fut.cancel()
        except queue.Empty:
            pass
        self._work_q.put(None)

    # ---- status / candidate surface --------------------------------------

    def status(self, provider_key: str) -> dict:
        st = self._state.get(provider_key)
        if st is None:
            return dict(_OFF, candidates=[])
        out = {**st, "candidates": list(st["candidates"].values())}
        out.pop("fingerprint", None)
        out.pop("gen", None)
        return out

    def _orphan_pending(self, key: str) -> None:
        pend = self._pending.pop(key, None)
        if pend is not None:
            fut = pend[0]
            self._orphans.add(fut)
            fut.add_done_callback(self._orphans.discard)

    def _drop(self, key: str) -> None:
        self._state.pop(key, None)
        self._orphan_pending(key)

    def _worker_loop(self) -> None:
        """Single consumer of the scan queue — every os call the watcher
        makes happens on this thread. Poison-pill exits; a cancelled
        queued item is skipped without running its filesystem op."""
        while True:
            item = self._work_q.get()
            if item is None:                     # poison pill — clean exit
                return
            fn, args, fut = item
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn(*args))
                except BaseException as e:
                    fut.set_exception(e)

    # ---- poll loop --------------------------------------------------------

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                pass  # a tick failure must never kill the service
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        paused: str | None = None
        if self._pressure() == "critical":
            paused = "system memory pressure"
        with self._sf() as s:
            if paused is None:
                # Pause while resource-intensive inference is active —
                # gen.* adapters are the real local/GPU workers (mock.* and
                # ops.* stay cheap enough to keep polling).
                busy = s.query(GenerationJob).filter(
                    GenerationJob.status == "running",
                    GenerationJob.adapter_id.like("gen.%")).first()
                if busy:
                    paused = "generation job running"
            cfg = registry.get_config(s)
            live: set[str] = set()
            for key, entry in cfg.items():
                prov = catalog.get_provider(key)
                if prov is None:
                    continue
                if (entry.get("settings") or {}).get("import_behavior") != "poll":
                    continue
                live.add(key)
                st = self._state.setdefault(
                    key, {**_OFF, "enabled": True, "candidates": {},
                          "gen": 1, "fingerprint": None})
                st["enabled"] = True
                st["paused_reason"] = paused
                if paused is not None:
                    continue                # skip scans — state still honest
                folder, _err = bridge.validate_watch_folder(entry)
                if folder is None:
                    st.update(status="degraded",
                              reason="designated folder is unavailable "
                                     "or no longer valid",
                              folder_name=None)
                    continue
                fp = str(folder)
                if st["fingerprint"] != fp:
                    # Folder (re)designated — everything cached or
                    # in-flight under the old folder is void.
                    self._orphan_pending(key)
                    st.update(fingerprint=fp, candidates={},
                              gen=st["gen"] + 1)
                st["folder_name"] = folder.name
                pend = self._pending.get(key)
                if pend is not None:
                    fut, gen, pfp = pend
                    if not fut.done():
                        continue        # scan still running — no overlap
                    self._pending.pop(key)
                    if gen != st["gen"] or pfp != fp:
                        continue        # stale session — result discarded
                    try:
                        cands, trunc = fut.result()
                        items = {c["path"]: c for c in cands}
                        if len(items) > CACHE_MAX_PER_PROVIDER:
                            items = dict(itertools.islice(
                                items.items(), CACHE_MAX_PER_PROVIDER))
                            trunc = True
                        st.update(status="ok", reason=None,
                                  candidates=items,
                                  last_poll=datetime.now(
                                      timezone.utc).isoformat())
                    except Exception:
                        st.update(status="degraded",
                                  reason="scan failed — will retry")
                    continue
                # Schedule the scan on the single worker thread — results
                # are collected next tick; nothing blocks the event loop.
                # A plain concurrent.futures.Future is the handle: never
                # awaited, only polled — loop-agnostic and thread-safe.
                if self._stopping:
                    continue
                ext_map = bridge.watch_eligible_exts(prov)
                fut: Future = Future()
                self._work_q.put((bridge.scan_watch_folder,
                                  (folder, ext_map, set()), fut))
                self._pending[key] = (fut, st["gen"], fp)
            for key in list(self._state):
                if key not in live:
                    self._drop(key)
            self._enforce_cache_cap()

    def _enforce_cache_cap(self) -> None:
        total = sum(len(st["candidates"]) for st in self._state.values())
        if total <= CACHE_MAX_TOTAL:
            return
        flat = sorted(
            ((c["mtime"], k, p)
             for k, st in self._state.items() for p, c in st["candidates"].items()),
            key=lambda t: -t[0])
        keep = {(k, p) for _, k, p in flat[:CACHE_MAX_TOTAL]}
        for k, st in self._state.items():
            st["candidates"] = {p: c for p, c in st["candidates"].items()
                                if (k, p) in keep}
