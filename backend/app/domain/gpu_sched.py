"""GPU-aware job scheduling.

Single-GPU and multi-GPU machines are both first-class. A7 moves the
scheduler from one-job-per-device busy flags to per-device VRAM
reservation accounting: a job with a known footprint estimate may share
a device with other jobs as long as the reserved total plus the
configured headroom stays under the device's usable VRAM. A job with an
unknown footprint claims its device exclusively — never gamble on an
unmeasured allocation.

Device selection:

- "auto"       → pick the device with the most free-after-reserve VRAM
                 that fits the job's estimate and required backend
- "cpu"        → force CPU lane (no GPU reservation consumed)
- <int>        → a specific detected GPU index; if it can't admit the
                 job right now the job stays queued rather than silently
                 moving to another device

Compatibility: ``requires`` may name backends ("cuda", "vulkan",
"directml") — a device satisfies a backend requirement when the probe
reports that backend available AND, for vendor-bound backends, the
device vendor matches ("cuda" → NVIDIA). Multiple GPUs never pool VRAM:
every admission is per-device.

Fairness: the engine claims queued jobs FIFO and skips a job that can't
be admitted rather than blocking the lane — a big job waiting for a
full device cannot starve smaller jobs that fit elsewhere or alongside
existing reservations.

Device loss: a device that disappears from the probe while holding
reservations is reported via ``lost_devices``/``slots(present=False)``;
nothing new is scheduled onto it and ``acquire`` for a pinned index
refuses "not present". Reconciliation of the running job itself is the
engine/worker's contract (the work is gone with the device); the
scheduler's job is to stop admitting and to surface the loss honestly.

The scheduler is a pure in-process structure — the engine owns
lifetimes and calls acquire/release around execution. GPU identity is
the probe's enumeration index (stable per boot); device env vars are
chosen by the adapter for its runtime.
"""

from __future__ import annotations

from typing import Any

from app.domain import compatibility

# Backend requirements a device vendor can satisfy. "directml" is
# vendor-neutral on Windows; "vulkan" needs an ICD + any GPU; "cuda"
# is NVIDIA-only.
_BACKEND_VENDORS = {"cuda": "NVIDIA"}


class GpuScheduler:
    def __init__(self, hw_probe: Any = None, prefs: Any = None) -> None:
        # hw_probe: callable -> hardware report dict (hardware.probe) or a
        # plain dict for tests. prefs: dict or callable -> {mode, preferred_gpu}
        # — the app passes a callable that reads live app_settings.
        self._probe = hw_probe
        self._prefs_src = prefs or {}
        # gpu_index -> {job_id: reserved_bytes}. An exclusive (unknown-
        # footprint) claim reserves the device's full usable figure so no
        # other job can share it.
        self._reserved: dict[int, dict[str, int]] = {}
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

    def _backends(self) -> dict:
        return self._hw().get("backends") or {}

    def _prefs(self) -> dict:
        p = self._prefs_src
        return p() if callable(p) else p

    def set_prefs(self, prefs: dict) -> None:
        if callable(self._prefs_src):
            return  # prefs live in app_settings; write through the API
        self._prefs_src.update(prefs)

    # -------------------------------------------------------- compatibility

    def _fits_backend(self, gpu: dict, requires: list[str]) -> str | None:
        """First unmet backend requirement, or None when the device can
        run the job's runtime."""
        backends = self._backends()
        for b in requires:
            info = backends.get(b) or {}
            if not info.get("available"):
                return f"backend '{b}' is not available on this system"
            vendor = _BACKEND_VENDORS.get(b)
            if vendor and gpu.get("vendor") != vendor:
                return (f"backend '{b}' requires a {vendor} device — "
                        f"GPU {gpu.get('index')} is "
                        f"{gpu.get('vendor') or 'unknown'}")
        return None

    def _usable(self, gpu: dict) -> int:
        usable = compatibility.usable_vram(gpu) or 0
        headroom = int(self._prefs().get("vram_headroom_bytes") or 0)
        return max(0, usable - headroom)

    def _reserved_sum(self, index: int) -> int:
        return sum(self._reserved.get(index, {}).values())

    def _free_after(self, gpu: dict) -> int:
        """VRAM free for a new reservation on this device."""
        return max(0, self._usable(gpu)
                   - self._reserved_sum(gpu.get("index")))

    # ------------------------------------------------------------ slotting

    def slots(self) -> list[dict]:
        present = {g.get("index", i) for i, g in enumerate(self.gpus())}
        out = []
        for i, g in enumerate(self.gpus()):
            idx = g.get("index", i)
            jobs = dict(self._reserved.get(idx, {}))
            usable = self._usable(g)
            out.append({
                "index": idx,
                "name": g.get("name"),
                "vendor": g.get("vendor"),
                "vram_bytes": g.get("vram_bytes"),
                "vram_free_bytes": g.get("vram_free_bytes"),
                "usable_bytes": usable,
                "reserved_bytes": sum(jobs.values()),
                "free_after_reserve_bytes": max(0, usable
                                                - sum(jobs.values())),
                "jobs": jobs,
                # Legacy single-job surface for older UIs.
                "busy_job_id": next(iter(jobs), None),
                "present": True,
            })
        # Devices that vanished while holding reservations — surfaced
        # honestly instead of dropping the evidence.
        for idx, jobs in self._reserved.items():
            if idx in present or not jobs:
                continue
            out.append({
                "index": idx, "name": None, "vendor": None,
                "vram_bytes": None, "vram_free_bytes": None,
                "usable_bytes": 0,
                "reserved_bytes": sum(jobs.values()),
                "free_after_reserve_bytes": 0,
                "jobs": jobs, "busy_job_id": next(iter(jobs), None),
                "present": False,
            })
        return out

    def busy(self) -> dict[int, str]:
        return {s["index"]: s["busy_job_id"] for s in self.slots()
                if s["busy_job_id"] is not None}

    def free_count(self) -> int:
        return sum(1 for s in self.slots()
                   if s["present"] and not s["jobs"])

    def lost_devices(self) -> list[int]:
        present = {g.get("index", i) for i, g in enumerate(self.gpus())}
        return sorted(idx for idx, jobs in self._reserved.items()
                      if idx not in present and jobs)

    def reservations(self) -> dict[int, dict[str, int]]:
        return {idx: dict(jobs) for idx, jobs in self._reserved.items()}

    def acquire(self, job_id: str, request: dict, *,
                est_vram: int | None = None,
                requires: list[str] | None = None) -> int | None:
        """Reserve GPU capacity for a job. Returns the assigned index,
        -1 for the CPU lane, or None when nothing suitable can admit
        the job right now. An unknown footprint (est_vram=None) claims
        the device exclusively; a known estimate reserves exactly that
        and may share the device with other known reservations."""
        prefs = self._prefs()
        device = request.get("device", prefs.get("preferred_gpu", "auto"))
        if device in (None, ""):
            device = "auto"
        requires = [r for r in (requires or request.get("requires")
                                or []) if r]
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
            unmet = self._fits_backend(g, requires)
            if unmet:
                self.last_reason = unmet
                return None
            free = self._free_after(g)
            need = est_vram if est_vram else self._usable(g)
            if need > free:
                if self._reserved.get(device):
                    self.last_reason = (
                        f"requested GPU {device} cannot fit the job — "
                        f"needs ~{est_gib or 'exclusive'} VRAM, "
                        f"~{free / 2**30:.1f} GiB free after "
                        "reservations")
                else:
                    self.last_reason = (
                        f"requested GPU {device} cannot fit the job — "
                        f"needs ~{est_gib or 'exclusive'} VRAM, "
                        f"~{free / 2**30:.1f} GiB usable after headroom")
                return None
            self._reserved.setdefault(device, {})[job_id] = need
            return device

        # auto: devices that satisfy the backend requirement and can
        # admit the estimate, most free-after-reserve first.
        if not gpus:
            if request.get("allow_cpu", True):
                return -1
            self.last_reason = ("no GPU detected and this model has no "
                                "CPU path")
            return None
        scored = []
        for g in gpus:
            unmet = self._fits_backend(g, requires)
            if unmet:
                continue
            scored.append((self._free_after(g), g))
        if not scored:
            self.last_reason = ("no compatible GPU — "
                                + (self._fits_backend(gpus[0], requires)
                                   or "devices unavailable"))
            return None
        if est_vram is None:
            # Exclusive claim — only a device with zero reservations
            # can host an unmeasured job.
            fits = [(u, g) for u, g in scored
                    if not self._reserved.get(g.get("index"))]
        else:
            fits = [(u, g) for u, g in scored if u >= est_vram]
        if not fits:
            all_measurable = all(g.get("vram_bytes")
                                 for _, g in scored)
            if est_vram and all_measurable:
                best = max((u for u, _ in scored), default=0) / 2**30
                self.last_reason = (
                    f"no device fits — job needs ~{est_gib} VRAM; "
                    f"best free device offers ~{best:.1f} GiB after "
                    "reservations")
                return None
            if est_vram is None or not all_measurable:
                # Old behavior, kept: when VRAM can't be measured we
                # can't prove a fit — admit the best free device for
                # an estimated job; exclusive claims still refuse.
                if est_vram is None:
                    self.last_reason = "all GPU slots are busy"
                    return None
                pool = scored
                pool.sort(key=lambda t: t[0], reverse=True)
                idx = pool[0][1].get("index")
                self._reserved.setdefault(idx, {})[job_id] = est_vram
                return idx
            self.last_reason = "all GPU slots are busy"
            return None
        fits.sort(key=lambda t: t[0], reverse=True)
        idx = fits[0][1].get("index")
        if idx is None:
            self.last_reason = "no usable GPU index found"
            return None
        claim = est_vram if est_vram else self._usable(fits[0][1])
        self._reserved.setdefault(idx, {})[job_id] = claim
        return idx

    def release(self, job_id: str) -> None:
        for jobs in self._reserved.values():
            jobs.pop(job_id, None)

    # -------------------------------------------------------------- status

    def state(self) -> dict:
        slots = self.slots()
        return {
            "gpu_count": self.gpu_count(),
            "free": self.free_count(),
            "slots": slots,
            "lost_devices": self.lost_devices(),
            "prefs": self._prefs(),
        }
