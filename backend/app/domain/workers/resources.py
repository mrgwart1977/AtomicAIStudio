"""Resident-worker resource accounting (M4.1 Phase 1).

A live worker holds RAM + VRAM hostage whether or not it is executing.
The ResidencyRegistry tracks every resident reservation and answers the
admission question: "can a new reservation of (ram, vram on gpu N) be
made without violating headroom?"

Conservative by design: reservations are subtracted from the probed
free values for the worker's entire lifetime. On real hardware a
resident worker's usage is already reflected in the probe — so a
realized reservation may be double-counted. That direction is always
fail-closed (stricter), never fail-open. Phase 2 can refine this with a
"realized" flag once residency is measured per-runtime.

Fail-closed policy: unknown/zero estimates for a declared need are
rejected rather than guessed.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

GiB = 1024 ** 3
DEFAULT_RAM_HEADROOM = 2 * GiB


@dataclass(frozen=True)
class Reservation:
    worker_id: str
    ram_bytes: int
    vram_bytes: int
    gpu_index: int  # -1 = CPU worker (no VRAM charge)


class ResidencyRegistry:
    """Thread-safe reservation ledger + admission control."""

    def __init__(
        self,
        ram_free_bytes: Callable[[], int],
        gpu_free_bytes: Callable[[int], int | None],
        *,
        ram_headroom_bytes: int = DEFAULT_RAM_HEADROOM,
        fail_closed_unknown: bool = True,
    ) -> None:
        self._ram_free = ram_free_bytes
        self._gpu_free = gpu_free_bytes
        self._headroom = ram_headroom_bytes
        self._fail_unknown = fail_closed_unknown
        self._res: dict[str, Reservation] = {}
        self._lock = threading.Lock()

    # ---------- ledger ----------

    def reserve(self, worker_id: str, ram_bytes: int, vram_bytes: int,
                gpu_index: int) -> Reservation:
        res = Reservation(worker_id, max(0, ram_bytes), max(0, vram_bytes), gpu_index)
        with self._lock:
            self._res[worker_id] = res
        return res

    def release(self, worker_id: str) -> None:
        with self._lock:
            self._res.pop(worker_id, None)

    def resident_totals(self, gpu_index: int | None = None) -> tuple[int, int]:
        """(ram_bytes, vram_bytes) currently reserved. gpu_index=None sums
        all VRAM; a specific index sums only that GPU — VRAM never pools."""
        ram = vram = 0
        with self._lock:
            for r in self._res.values():
                ram += r.ram_bytes
                if gpu_index is None or r.gpu_index == gpu_index:
                    vram += r.vram_bytes
        return ram, vram

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [
                {"worker_id": r.worker_id, "ram_bytes": r.ram_bytes,
                 "vram_bytes": r.vram_bytes, "gpu_index": r.gpu_index}
                for r in self._res.values()
            ]

    # ---------- admission ----------

    def admit(self, ram_est: int | None, vram_est: int | None,
              gpu_index: int) -> tuple[bool, str]:
        """(allowed, reason). RAM is machine-wide; VRAM is per-GPU."""
        if gpu_index >= 0:
            if not vram_est or vram_est <= 0:
                if self._fail_unknown:
                    return False, ("worker VRAM estimate is unknown — "
                                   "fail-closed, cannot admit a resident worker")
                vram_est = 0
            gpu_free = self._gpu_free(gpu_index)
            if gpu_free is None:
                return False, f"GPU {gpu_index} free VRAM could not be probed"
            _, res_vram = self.resident_totals(gpu_index)
            vram_avail = gpu_free - res_vram
            if vram_est > vram_avail:
                return False, (f"insufficient free VRAM on GPU {gpu_index}: "
                               f"worker needs ~{vram_est / GiB:.2f} GiB, "
                               f"~{vram_avail / GiB:.2f} GiB available after "
                               "resident reservations")
        if not ram_est or ram_est <= 0:
            if self._fail_unknown:
                return False, ("worker RAM estimate is unknown — "
                               "fail-closed, cannot admit a resident worker")
            ram_est = 0
        res_ram, _ = self.resident_totals()
        ram_avail = self._ram_free() - res_ram - self._headroom
        if ram_est > ram_avail:
            return False, (f"insufficient system RAM for a resident worker: "
                           f"needs ~{ram_est / GiB:.2f} GiB, "
                           f"~{ram_avail / GiB:.2f} GiB available after "
                           "reservations and OS headroom")
        return True, "fits"

    # ---------- pressure ----------

    def under_pressure(self) -> bool:
        """True when free RAM (minus reservations) is inside headroom —
        the pool uses this to evict idle workers proactively."""
        res_ram, _ = self.resident_totals()
        return self._ram_free() - res_ram < self._headroom
