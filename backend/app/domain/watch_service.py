"""C1.5 ImportWatcher Stage B — opt-in background polling.

One shared asyncio task polls every provider whose persisted
``settings.import_behavior == "poll"`` (set only via the audited
``bridge.watch.enable`` command — off by default). All filesystem work
runs on a single worker thread, never on the FastAPI event loop, and one
scan per provider may be in flight at a time (fire-and-collect: a tick
never overlaps a still-running previous scan).

Safety invariants (all Stage A logic reused):
- validate_watch_folder gates EVERY poll — symlink/junction/traversal
  rejection and containment are identical to the manual scan.
- scan_watch_folder is stat-only, non-recursive, and bounded; file
  contents are never read here.
- Folder invalid/missing/inaccessible → ``degraded`` + skip; it resumes
  only when the ORIGINAL designated folder validates again — the service
  never follows a moved or replaced path.
- Pauses under critical memory pressure (resources.pressure_state) and
  while a generation job is running.
- Candidates are in-memory only and can never become assets without an
  explicit per-file bridge.import — polling changes nothing on disk.
"""

from __future__ import annotations

import asyncio
import itertools
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from app.db.models import GenerationJob
from app.domain import bridge, resources
from app.domain.providers import catalog, registry

POLL_INTERVAL_S = 60.0
POLL_MIN_INTERVAL_S = 30.0
CACHE_MAX_PER_PROVIDER = 200
CACHE_MAX_TOTAL = 2000

_OFF = {"enabled": False, "status": "off", "reason": None,
        "folder_name": None, "candidates": {}, "last_poll": None,
        "paused_reason": None}


class ImportWatchService:
    def __init__(self, session_factory, *,
                 interval_s: float = POLL_INTERVAL_S,
                 pressure: Callable[[], str] = resources.pressure_state):
        self._sf = session_factory
        self._interval = max(float(interval_s), POLL_MIN_INTERVAL_S)
        self._pressure = pressure
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        # One worker thread for ALL filesystem access — scans serialize by
        # construction, so two scans can never run concurrently.
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="watch-scan")
        self._pending: dict[str, asyncio.Future] = {}
        self._state: dict[str, dict] = {}

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the loop and let any in-flight filesystem op finish on
        its thread — an asyncio cancel cannot stop a running os call, so
        we wait it out (bounded by WATCH_MAX_ENTRIES) rather than leak it."""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except BaseException:
                # CancelledError is BaseException on modern Python — a
                # cancelled task is the EXPECTED outcome here, not a failure.
                pass
            self._task = None
        self._executor.shutdown(wait=True)

    # ---- status / candidate surface --------------------------------------

    def status(self, provider_key: str) -> dict:
        st = self._state.get(provider_key)
        if st is None:
            return dict(_OFF, candidates=[])
        return {**st, "candidates": list(st["candidates"].values())}

    def _drop(self, key: str) -> None:
        self._state.pop(key, None)
        self._pending.pop(key, None)

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
                    key, {**_OFF, "enabled": True, "candidates": {}})
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
                st["folder_name"] = folder.name
                fut = self._pending.get(key)
                if fut is not None:
                    if not fut.done():
                        continue  # previous scan still running — no overlap
                    self._pending.pop(key)
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
                # A plain concurrent future is used (not run_in_executor):
                # it is never awaited, only polled — so it is loop-agnostic
                # and thread-safe across ticks.
                ext_map = bridge.watch_eligible_exts(prov)
                self._pending[key] = self._executor.submit(
                    bridge.scan_watch_folder, folder, ext_map, set())
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
