"""GPU-aware job scheduling.

Single-GPU and multi-GPU machines are both first-class. One inference
worker slot per GPU keeps VRAM budgets honest; the scheduler assigns each
GPU-bound job to a free compatible device. Device selection:

- "auto"       → pick the free GPU with the most usable VRAM that fits the
                 job's estimate (falls back to the largest free device, or
                 to CPU only when no GPU exists and the adapter allows it)
- "cpu"        → force CPU lane (no GPU slot consumed)
- <int>        → a specific detected GPU index; if it's busy the job stays
                 queued rather than silently moving to another device

The scheduler is a pure in-process structure — the engine owns lifetimes
and calls acquire/release around execution. GPU identity is the probe's
enumeration index (stable per boot); device env vars are chosen by the
adapter for its runtime.
"""

from __future__ import annotations

from typing import Any

from app.domain import compatibility


class GpuScheduler:
    def __init__(self, hw_probe: Any = None, prefs: Any = None) -> None:
        # hw_probe: callable -> hardware report dict (hardware.probe) or a
        # plain dict for tests. prefs: dict or callable -> {mode, preferred_gpu}
        # — the app passes a callable that reads live app_settings.
        self._probe = hw_probe
        self._prefs_src = prefs or {}
        self._busy: dict[int, str] = {}  # gpu_index -> job_id
        # Why the most recent acquire() refused — the engine surfaces this
        # on the queued job so a stuck job always explains itself.
        self.last_reason: str | None = None

    # ----------------------------------------------------------- hardware

    def _hw(self) -> dict:
        p = self._probe
        return p() if callable(p) else (p or {})

    def gpus(self) -> list[dict]:
        return self._hw().get("gpus") or []

    def gpu_count(self) -> int:
        return len(self.gpus())

    def _prefs(self) -> dict:
        p = self._prefs_src
        return p() if callable(p) else p

    def set_prefs(self, prefs: dict) -> None:
        if callable(self._prefs_src):
            return  # prefs live in app_settings; write through the API
        self._prefs_src.update(prefs)

    # ------------------------------------------------------------ slotting

    def slots(self) -> list[dict]:
        busy = dict(self._busy)
        return [
            {
                "index": g.get("index", i),
                "name": g.get("name"),
                "vendor": g.get("vendor"),
                "vram_bytes": g.get("vram_bytes"),
                "vram_free_bytes": g.get("vram_free_bytes"),
                "busy_job_id": busy.get(g.get("index", i)),
            }
            for i, g in enumerate(self.gpus())
        ]

    def busy(self) -> dict[int, str]:
        return dict(self._busy)

    def free_count(self) -> int:
        return sum(1 for s in self.slots() if s["busy_job_id"] is None)

    def acquire(self, job_id: str, request: dict, *, est_vram: int | None = None) -> int | None:
        """Reserve a GPU for a job. Returns the assigned index, -1 for the
        CPU lane, or None when nothing suitable is free right now."""
        prefs = self._prefs()
        device = request.get("device", prefs.get("preferred_gpu", "auto"))
        if device in (None, ""):
            device = "auto"
        gpus = self.gpus()
        est_gib = f"{est_vram / 2**30:.1f} GiB" if est_vram else None
        self.last_reason = None

        if isinstance(device, str) and device == "cpu":
            return -1
        if isinstance(device, int) and not isinstance(device, bool):
            g = next((x for x in gpus if x.get("index") == device), None)
            if g is None:
                self.last_reason = f"requested GPU {device} is not present"
                return None
            if device in self._busy:
                self.last_reason = f"requested GPU {device} is busy"
                return None
            self._busy[device] = job_id
            return device

        # auto: free GPUs that plausibly fit, best usable VRAM first.
        if not gpus:
            if request.get("allow_cpu", True):
                return -1
            self.last_reason = "no GPU detected and this model has no CPU path"
            return None
        free = [g for g in gpus if g.get("index") not in self._busy]
        if not free:
            self.last_reason = "all GPU slots are busy"
            return None
        # C4-E — a configurable headroom subtracts from the driver-
        # reported usable figure before admission (driver free-memory
        # numbers lag real allocation).
        headroom = int(prefs.get("vram_headroom_bytes") or 0)
        scored = []
        for g in free:
            usable = compatibility.usable_vram(g) or 0
            scored.append((max(0, usable - headroom), g))
        # Prefer devices that fit the estimate; if none measurably fit and
        # every GPU is measurable, refuse rather than risk a hard OOM.
        fits = [(u, g) for u, g in scored if est_vram is None or u >= est_vram]
        if not fits and est_vram and all(g.get("vram_bytes") for _, g in scored):
            best = max((u for u, _ in scored), default=0) / 2**30
            self.last_reason = (
                f"no device fits — job needs ~{est_gib} VRAM; "
                f"best free device offers ~{best:.1f} GiB")
            return None
        pool = fits or scored
        pool.sort(key=lambda t: t[0], reverse=True)
        idx = pool[0][1].get("index")
        if idx is None:
            self.last_reason = "no usable GPU index found"
            return None
        self._busy[idx] = job_id
        return idx

    def release(self, job_id: str) -> None:
        for idx, jid in list(self._busy.items()):
            if jid == job_id:
                self._busy.pop(idx, None)

    # -------------------------------------------------------------- status

    def state(self) -> dict:
        return {
            "gpu_count": self.gpu_count(),
            "free": self.free_count(),
            "slots": self.slots(),
            "prefs": self._prefs(),
        }
