"""U-5c — canonical read-only hardware evidence snapshot.

Consumes the EXISTING hardware.probe() report (standard cached cadence —
the caller supplies the report; this module never refreshes it) plus the
GpuScheduler's read-only state()/slots() view, and produces a labeled,
fingerprinted evidence snapshot for execution-plan preview. No probing is
added and GpuScheduler.acquire() is never invoked — the scheduler remains
the sole execution-time scheduling authority.

Quantity provenance is kept distinct on every field:
  vram_bytes / vram_free_bytes  — measured (registry | nvidia-smi | wmi)
  usable_bytes                  — derived estimate (compatibility.usable_vram)
  *_reserved_bytes              — scheduler bookkeeping, never physical
  conservative_planning_budget  — derived planning figure (Gate 3): usable
                                  minus scheduler vram_headroom minus
                                  reservations. It is NOT a guarantee of
                                  physically available VRAM — driver
                                  readings can lag real allocation and a
                                  reservation's relation to live driver
                                  free space is unprovable per job.
"""

from __future__ import annotations

import hashlib
import json
import time

from app.domain import compatibility

SNAPSHOT_SCHEMA_VERSION = 1
# hardware.probe() caches per ~5-minute bucket — a snapshot older than
# one bucket is reported stale (disclosed, never hidden).
STALE_SNAPSHOT_SECONDS = 300.0

# Per-device backend evidence grades.
EVIDENCE_MEASURED = "measured"
EVIDENCE_INFERRED = "inferred"
EVIDENCE_UNKNOWN = "unknown"
EVIDENCE_MISMATCH = "mismatch"

# Index values are the probe's enumeration order — stable for the boot
# session only (the scheduler's own device selector). Disclosed, never
# presented as a durable device identity.
INDEX_STABILITY = "per_boot"


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def device_backend_evidence(device: dict, machine_backends: dict,
                            backend: str) -> tuple[str, str]:
    """(grade, reason) — machine + per-device evidence for ONE backend on
    ONE device (Gate 4, F2). A machine-wide Vulkan ICD or the OS-level
    DirectML flag is at most 'inferred' for any single device; 'measured'
    requires driver-level proof tied to this device (today: nvidia-smi
    matched to a NVIDIA row)."""
    info = machine_backends.get(backend) or {}
    vendor = (device.get("vendor") or "").strip()
    if backend == "cpu":
        return (EVIDENCE_INFERRED, "cpu lane — not a device backend")
    if backend == "cuda":
        if vendor == "NVIDIA":
            if device.get("vram_free_bytes") is not None \
                    or info.get("measured"):
                return (EVIDENCE_MEASURED,
                        "NVIDIA device confirmed via nvidia-smi")
            return (EVIDENCE_INFERRED,
                    "NVIDIA device enumerated; no per-device driver probe")
        if vendor:
            return (EVIDENCE_MISMATCH,
                    f"cuda requires an NVIDIA device — this device is {vendor}")
        return (EVIDENCE_UNKNOWN,
                "device vendor could not be established")
    if not info.get("available"):
        return (EVIDENCE_UNKNOWN,
                f"backend '{backend}' is not available on this machine")
    if vendor:
        return (EVIDENCE_INFERRED,
                f"machine-level {backend} runtime present; "
                + "per-device support is inferred, not measured")
    return (EVIDENCE_UNKNOWN, "device vendor could not be established")


def build_snapshot(probe: dict, sched_state: dict | None,
                   margins: dict, *, now: float | None = None,
                   monotonic_now: float | None = None) -> dict:
    """Canonicalize probe + scheduler views into one evidence snapshot.

    probe — the report dict from hardware.probe() (already measured; this
        function performs no probing itself).
    sched_state — GpuScheduler.state() output, or None when no scheduler
        is wired (tests); reservations then read as unavailable, not zero.
    margins — resources.safety_margins() output; only
        vram_headroom_bytes feeds the planning budget here. The separate
        0.7 GiB stability margin (compat._HEADROOM_BYTES) is applied once
        in fit classification by the planner — never in this budget.
    """
    now = time.time() if now is None else now
    monotonic_now = (time.monotonic() if monotonic_now is None
                     else monotonic_now)
    headroom = int((margins or {}).get("vram_headroom_bytes") or 0)

    slots: dict[int, dict] = {}
    lost: list[dict] = []
    for s in (sched_state or {}).get("slots") or []:
        idx = s.get("index")
        if idx is None:
            continue
        if s.get("present") is False:
            # A vanished device still holding reservations — evidence is
            # surfaced, never dropped.
            lost.append({"index": idx,
                         "reserved_bytes": int(s.get("reserved_bytes") or 0)})
        else:
            slots[idx] = s
    reservations_captured = sched_state is not None

    devices = []
    for i, g in enumerate(probe.get("gpus") or []):
        idx = g.get("index", i)
        slot = slots.get(idx) or {}
        measured_total = g.get("vram_bytes")
        measured_free = g.get("vram_free_bytes")
        usable = compatibility.usable_vram(g)
        usable_after = (max(0, usable - headroom)
                        if usable is not None else None)
        reserved = int(slot.get("reserved_bytes") or 0)
        cpb = (max(0, usable_after - reserved)
               if usable_after is not None else None)
        devices.append({
            "index": idx,
            "name": g.get("name"),
            "vendor": g.get("vendor"),
            # measured — physical total + provenance
            "vram_bytes": measured_total,
            "vram_source": g.get("vram_source"),
            "vram_capped_at_4gb": bool(g.get("vram_capped_at_4gb")),
            # measured — driver-reported free (NVIDIA only; else None)
            "vram_free_bytes": measured_free,
            "driver_version": g.get("driver_version"),
            # derived — best honest usable estimate (measured free, else
            # 85% of measured total, else unknown — never coerced to 0)
            "usable_bytes": usable,
            "vram_headroom_bytes": headroom,
            "usable_after_headroom_bytes": usable_after,
            # bookkeeping — scheduler claims, NOT physical memory
            "reserved_bytes": reserved,
            # derived — conservative planning figure; may under-report
            # real free space when reservations overlap measured free
            "conservative_planning_budget_bytes": cpb,
            "optimistic_bound_bytes": usable_after,
            "reservation_overlap_possible": bool(
                measured_free is not None and reserved > 0),
            "present": True,
            "index_stability": INDEX_STABILITY,
        })

    backends = {
        k: {"available": bool(v.get("available")),
            "measured": bool(v.get("measured"))}
        for k, v in (probe.get("backends") or {}).items()
    }
    mem = probe.get("memory") or {}
    cpu = probe.get("cpu") or {}
    simulated = bool(probe.get("simulated"))

    unknowns: list[str] = []
    if not reservations_captured:
        unknowns.append("scheduler_reservations_unavailable")
    for d in devices:
        if d["usable_bytes"] is None:
            unknowns.append(f"gpu{d['index']}_vram_unmeasurable")
        if not d["vendor"]:
            unknowns.append(f"gpu{d['index']}_vendor_unknown")

    # Evidence-only fingerprint: timestamps excluded so identical
    # canonical evidence yields an identical snapshot_id across time
    # (e.g. a probe cache-bucket rollover). Reservations ARE evidence —
    # a reservation change is a different snapshot.
    canon = {
        "schema": SNAPSHOT_SCHEMA_VERSION,
        "devices": [[d["index"], d["name"], d["vendor"], d["vram_bytes"],
                     d["vram_source"], d["vram_free_bytes"],
                     d["usable_bytes"], d["reserved_bytes"]]
                    for d in devices],
        "lost": [[d["index"], d["reserved_bytes"]] for d in lost],
        "backends": sorted((k, v["available"], v["measured"])
                           for k, v in backends.items()),
        "cpu": [cpu.get("physical_cores"), cpu.get("logical_cores")],
        "memory": [mem.get("total_bytes"), mem.get("available_bytes")],
        "simulated": simulated,
    }
    snapshot_id = "hw:v1:" + hashlib.sha256(
        _canon(canon).encode()).hexdigest()[:16]

    captured = probe.get("probed_at")
    captured_mono = probe.get("probed_monotonic")
    age_s = (max(0.0, now - captured) if captured is not None else None)
    # Freshness is a monotonic measurement — immune to wall-clock jumps.
    # The wall-clock fallback serves reports collected before the
    # monotonic field existed (e.g. older recorded/simulated evidence).
    if captured_mono is not None:
        stale = bool(monotonic_now - captured_mono > STALE_SNAPSHOT_SECONDS)
    else:
        stale = bool(age_s is not None and age_s > STALE_SNAPSHOT_SECONDS)
    return {
        "snapshot_id": snapshot_id,
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "captured_at": captured,
        "age_s": age_s,
        "stale": stale,
        "simulated": simulated,
        "devices": devices,
        "devices_total": len(devices),
        "lost_devices": lost,
        "backends": backends,
        "cpu": {"name": cpu.get("name"),
                "physical_cores": cpu.get("physical_cores"),
                "logical_cores": cpu.get("logical_cores")},
        "memory": {"total_bytes": mem.get("total_bytes"),
                   "available_bytes": mem.get("available_bytes")},
        "reservations_captured": reservations_captured,
        "unknowns": unknowns,
    }


__all__ = [
    "EVIDENCE_INFERRED",
    "EVIDENCE_MEASURED",
    "EVIDENCE_MISMATCH",
    "EVIDENCE_UNKNOWN",
    "INDEX_STABILITY",
    "SNAPSHOT_SCHEMA_VERSION",
    "STALE_SNAPSHOT_SECONDS",
    "build_snapshot",
    "device_backend_evidence",
]
