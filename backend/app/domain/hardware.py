"""Hardware/environment probe.

Reports what is actually present — never assumes an NVIDIA GPU or any
specific vendor. GPU info comes from WMI via a PowerShell CIM query;
accurate VRAM (>4 GiB) comes from the display-class registry keys
(HardwareInformation.qwMemorySize), and live free VRAM from nvidia-smi
when present. Every measured value carries its source so the UI can
distinguish measurements from estimates — unknown is reported, never
guessed.
"""

from __future__ import annotations

import copy
import json
import platform
import shutil
import subprocess
import threading
import time
from functools import lru_cache
from importlib.util import find_spec
from pathlib import Path

import psutil

_PS_GPU_QUERY = (
    "Get-CimInstance Win32_VideoController | "
    "Select-Object Name,AdapterRAM,DriverVersion,PNPDeviceID | ConvertTo-Json"
)

# One PS round-trip: display-class registry VRAM (REG_QWORD, real >4 GiB
# values) keyed by MatchingDeviceId, plus whether any Vulkan ICD is
# registered under the Khronos key.
_PS_DEVINFO_QUERY = r"""
$v = @()
foreach ($p in (Get-ItemProperty 'HKLM:\SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\*' -ErrorAction SilentlyContinue)) {
  if ($p.MatchingDeviceId) {
    $v += @{ id = [string]$p.MatchingDeviceId; qw = [int64]$p.'HardwareInformation.qwMemorySize' }
  }
}
$icd = 0
foreach ($k in @('HKLM:\SOFTWARE\Khronos\Vulkan\Drivers', 'HKLM:\SOFTWARE\WOW6432Node\Khronos\Vulkan\Drivers')) {
  $vk = Get-Item $k -ErrorAction SilentlyContinue
  if ($vk) { $icd += ($vk.GetValueNames() | Where-Object { $_ -match '\.json$' }).Count }
}
@{ vram = $v; vulkan_icd_count = $icd; vulkan_dll = (Test-Path "$env:WINDIR\System32\vulkan-1.dll") } | ConvertTo-Json -Depth 3
"""

# U-5d-0b alpha — bounded PCI topology evidence for the SAME display
# devices already enumerated by _PS_GPU_QUERY. Documented device
# properties only: DEVPKEY_Device_BusNumber (bus), DEVPKEY_Device_
# Address (PCI device/function encoding) and DEVPKEY_Device_
# LocationPaths (PCIROOT segment + devfn cross-check). Instance ids are
# passed as cmdlet parameters — never interpolated into the script.
_PS_PCI_QUERY = r"""
$rows = @()
foreach ($d in (Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue)) {
  $id = [string]$d.PNPDeviceID
  if (-not $id) { continue }
  $bus = $null; $addr = $null; $loc = @()
  try {
    $props = Get-PnpDeviceProperty -InstanceId $id -ErrorAction Stop
    foreach ($p in $props) {
      if ($p.KeyName -eq 'DEVPKEY_Device_BusNumber') { $bus = $p.Data }
      elseif ($p.KeyName -eq 'DEVPKEY_Device_Address') { $addr = $p.Data }
      elseif ($p.KeyName -eq 'DEVPKEY_Device_LocationPaths') { $loc = @($p.Data) }
    }
  } catch {}
  $rows += @{ id = $id; bus_number = $bus; address = $addr; location_paths = $loc }
}
@{ pci = $rows } | ConvertTo-Json -Depth 3 -Compress
"""


def _vendor_from_pnp(pnp_id: str) -> str:
    pnp = (pnp_id or "").upper()
    for needle, name in (("VEN_10DE", "NVIDIA"), ("VEN_1002", "AMD"), ("VEN_8086", "Intel")):
        if needle in pnp:
            return name
    return "unknown"


def _ps_json(script: str, timeout: int = 15):
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return json.loads(out.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        return None


def _pci_properties() -> dict:
    """U-5d-0b alpha — documented PCI topology properties keyed by the
    upper-cased PNPDeviceID of each enumerated display device.
    Windows-gated and bounded to the display class only; {} on timeout,
    nonzero exit, unsupported OS or malformed output — the absence of
    evidence is itself reported, never fabricated."""
    if platform.system() != "Windows":
        return {}
    data = _ps_json(_PS_PCI_QUERY)
    rows = []
    if isinstance(data, dict):
        raw = data.get("pci")
        rows = (raw if isinstance(raw, list)
                else [raw] if isinstance(raw, dict) else [])
    out = {}
    for r in rows:
        pid = (r.get("id") or "").upper()
        if not pid:
            continue
        loc = r.get("location_paths")
        loc = (loc if isinstance(loc, list)
               else [loc] if loc else [])
        out[pid] = {
            "bus_number": r.get("bus_number"),
            "address": r.get("address"),
            "location_paths": [p for p in loc if isinstance(p, str)],
        }
    return out


def _device_info() -> dict:
    """Registry VRAM map + Vulkan ICD count + PCI topology properties.
    Empty on failure/non-Windows."""
    data = _ps_json(_PS_DEVINFO_QUERY)
    vram = {}
    icd_count = 0
    if isinstance(data, dict):
        for row in data.get("vram") or []:
            rid, qw = (row.get("id") or "").upper(), row.get("qw")
            if rid and isinstance(qw, int) and qw > 0:
                vram[rid] = qw
        icd_count = int(data.get("vulkan_icd_count") or 0)
    dll = bool(data.get("vulkan_dll")) if isinstance(data, dict) else False
    return {"vram_by_device": vram, "vulkan_icd_count": icd_count,
            "vulkan_dll": dll, "pci_by_pnp": _pci_properties()}


def _nvidia_smi() -> list[dict]:
    """Live NVIDIA memory/driver readings when the driver tool exists.
    Returns [] when absent — NVIDIA is never assumed."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return []
    try:
        q = ("index,name,memory.total,memory.free,driver_version,"
             "pci.bus_id,uuid")
        out = subprocess.run(
            [smi, f"--query-gpu={q}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            return []
        rows = []
        for line in out.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                try:
                    rows.append({
                        "smi_index": int(parts[0]), "name": parts[1],
                        "vram_bytes": int(parts[2]) * 1024 * 1024,
                        "vram_free_bytes": int(parts[3]) * 1024 * 1024,
                        "driver_version": parts[4],
                        # U-5d-0b alpha — physical identity join keys.
                        # Absent on older drivers — None stays None.
                        "pci_bus_id": parts[5] if len(parts) > 5 else None,
                        "uuid": parts[6] if len(parts) > 6 else None,
                    })
                except ValueError:
                    continue  # malformed row skipped; never fabricated
        return rows
    except (subprocess.SubprocessError, ValueError):
        return []


def _query_gpus(devinfo: dict) -> list[dict]:
    """Public GPU rows — fixed key set (the /api/hardware contract).

    U-5d-0b alpha: every SMI-derived per-device value (total VRAM
    upgrade and free VRAM) now requires a qualified one-to-one
    BDF+UUID correlation via hw_identity. Name-substring matching and
    the single-GPU single-row fallback are removed — unmatched or
    ambiguous evidence yields None, never a guess.
    The correlation evidence itself is handed back through
    ``devinfo["_identity"]`` — an internal out-channel bound to THIS
    collection; it is never placed on the public rows."""
    from app.domain import hw_identity
    data = _ps_json(_PS_GPU_QUERY)
    rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    vram_map = devinfo["vram_by_device"]
    smi_rows = _nvidia_smi()
    meta = [{"index": i, "pnp": (r.get("PNPDeviceID") or "").upper(),
             "vendor": _vendor_from_pnp(r.get("PNPDeviceID") or "")}
            for i, r in enumerate(rows)]
    ident = hw_identity.correlate(
        meta, smi_rows, devinfo.get("pci_by_pnp") or {})
    devinfo["_identity"] = ident

    gpus = []
    for i, row in enumerate(rows):
        pnp = meta[i]["pnp"]
        vendor = meta[i]["vendor"]
        rec = ident["records"][i]
        wmi_vram = row.get("AdapterRAM")
        # Prefer the registry QWORD (accurate past the WMI uint32 cap),
        # then nvidia-smi, then WMI with the cap flagged.
        vram = wmi_vram
        vram_source = "wmi_capped" if wmi_vram and wmi_vram >= 0xF0000000 else "wmi"
        reg_hit = next((qw for rid, qw in vram_map.items() if pnp.startswith(rid)), None)
        if reg_hit:
            vram, vram_source = reg_hit, "registry"
        free = None
        if rec["correlation"] == hw_identity.CORR_QUALIFIED:
            if (rec["smi_vram_bytes"] is not None
                    and (not vram or vram < rec["smi_vram_bytes"])):
                vram, vram_source = rec["smi_vram_bytes"], "nvidia-smi"
            free = rec["smi_vram_free_bytes"]  # measured zero preserved
        gpus.append(
            {
                "index": i,  # enumeration index — the scheduler's device selector
                "name": row.get("Name"),
                "vendor": vendor,
                "vram_bytes": vram,
                "vram_source": vram_source,
                "vram_free_bytes": free,
                "vram_capped_at_4gb": vram_source == "wmi_capped",
                "driver_version": row.get("DriverVersion"),
                "role": "display",
                "inference_capable": None,  # decided by adapter checks, not assumed
            }
        )
    return gpus


def _disks() -> list[dict]:
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        disks.append(
            {
                "mount": part.mountpoint,
                "fstype": part.fstype,
                "total_bytes": usage.total,
                "free_bytes": usage.free,
            }
        )
    return disks


def _backends(devinfo: dict, gpus: list[dict]) -> dict:
    """Inference backends this machine can plausibly run. Each entry notes
    whether it was measured (driver/ICD present) or assumed."""
    any_nvidia = any(g["vendor"] == "NVIDIA" for g in gpus)
    win_major = int((platform.version().split(".") or ["0"])[0] or 0)
    return {
        "cpu": {"available": True, "measured": True},
        "vulkan": {
            # ICD registry entries are authoritative; the loader DLL alone
            # still means a vendor runtime is almost always present.
            "available": devinfo["vulkan_icd_count"] > 0 or devinfo.get("vulkan_dll") is True,
            "icd_count": devinfo["vulkan_icd_count"],
            "dll_present": devinfo.get("vulkan_dll"),
            "measured": True,
        },
        "cuda": {
            "available": bool(_nvidia_smi() or any_nvidia),
            "measured": bool(_nvidia_smi()),
        },
        # DirectML ships with Windows 10+; presence assumed from OS version.
        "directml": {"available": win_major >= 10, "measured": False},
    }


def _runtimes() -> dict:
    ffmpeg_path = shutil.which("ffmpeg")
    ffmpeg_version = None
    if ffmpeg_path:
        try:
            out = subprocess.run([ffmpeg_path, "-version"], capture_output=True, text=True, timeout=10)
            ffmpeg_version = out.stdout.splitlines()[0] if out.stdout else None
        except subprocess.SubprocessError:
            pass
    return {
        "python": platform.python_version(),
        "ffmpeg": {"available": ffmpeg_path is not None, "version": ffmpeg_version, "path": ffmpeg_path},
        "torch_installed": find_spec("torch") is not None,
        "cuda_detected": None,  # superseded by backends.cuda (measured)
    }


@lru_cache(maxsize=1)
def _cached_probe(_bucket: int) -> dict:
    """Probe results cached per ~5-minute bucket; hardware rarely changes.
    Call probe(force=True) for an on-demand refresh."""
    # Physical RAM + commit evidence are sampled together at the START of
    # collection — the report's probed_monotonic postdates them by the
    # full device-query time, so memory carries its own sampling instant
    # and staleness may age from the OLDEST sample, never the newest.
    t_mem = time.monotonic()
    vm = psutil.virtual_memory()
    from app.domain import resources
    commit = resources.commit_evidence()
    devinfo = _device_info()
    gpus = _query_gpus(devinfo)
    return {
        # Internal identity evidence paired with THIS collection —
        # stripped by probe() before publication so raw identifiers
        # never reach the serialized report.
        "_identity": devinfo.get("_identity"),
        "probed_at": time.time(),
        # Monotonic companion — collected at the actual discovery moment;
        # freshness classification must not depend on wall-clock jumps.
        "probed_monotonic": time.monotonic(),
        "os": {"system": platform.system(), "release": platform.release(), "version": platform.version()},
        "cpu": {
            "name": platform.processor() or "unknown",
            "physical_cores": psutil.cpu_count(logical=False),
            "logical_cores": psutil.cpu_count(logical=True),
        },
        "memory": {"total_bytes": vm.total, "available_bytes": vm.available,
                   "sampled_monotonic": t_mem, **commit},
        "gpus": gpus,
        "disks": _disks(),
        "backends": _backends(devinfo, gpus),
        "runtimes": _runtimes(),
        "hardware_profile": None,  # filled below from classify_profile
    }


def _simulated_report() -> dict | None:
    """A7 — deterministic simulated hardware for tests/acceptance.
    ``ATOMIC_HW_JSON`` holds either an inline JSON object or a path to
    a JSON file shaped like a probe report (or a partial report — its
    keys override the real probe's). Clearly marked so simulated
    devices are never mistaken for measured hardware."""
    import os
    raw = os.environ.get("ATOMIC_HW_JSON")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return data
    except json.JSONDecodeError:
        try:
            data = json.loads(Path(raw).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None


# ---------------------------------------------------------------- publication
#
# The last-successfully-published evidence record — the ONLY thing the
# strictly cache-only accessor may read. Publication happens exclusively
# inside probe(): cold start is honestly "unavailable", an expired record
# is still available as stale evidence, and a failed or in-flight refresh
# never disturbs the previous complete record.

_PUBLISH_LOCK = threading.Lock()
_PUBLISHED: dict | None = None


def _publish(report: dict, identity: dict | None = None) -> None:
    """Atomically publish a complete probe report. A private deep copy is
    stored; the ordering key is the report's own collection monotonic so
    an older discovery finishing late can never overwrite newer evidence.
    ``identity`` (U-5d-0b alpha) is the internal identity-evidence payload
    produced by the SAME collection — stored under the same lock so the
    report/identity pair can never diverge across publications."""
    global _PUBLISHED
    record = {
        "report": copy.deepcopy(report),
        "identity": copy.deepcopy(identity),
        "probed_at": report.get("probed_at"),
        "probed_monotonic": report.get("probed_monotonic"),
        "simulated": bool(report.get("simulated")),
    }
    key = record["probed_monotonic"]
    key = key if isinstance(key, (int, float)) else -1.0
    with _PUBLISH_LOCK:
        current_key = ((_PUBLISHED or {}).get("probed_monotonic"))
        current_key = current_key if isinstance(
            current_key, (int, float)) else -1.0
        # Newer collection always wins. An EQUAL collection identity may
        # only republish identical evidence — a same-key record carrying
        # different evidence or a different simulation classification is
        # a conflict and must not silently replace what was published.
        if key > current_key or (
                key == current_key
                and _PUBLISHED["report"] == report):
            record["_order"] = key
            _PUBLISHED = record


def probe_cached() -> dict | None:
    """Strictly cache-only evidence access — returns the last published
    complete report plus its collection metadata, or None when nothing has
    ever been published. NEVER touches the discovery cache, simulation
    sources, the OS, the filesystem or any subprocess; never refreshes
    expired evidence and never rewrites collection timestamps."""
    with _PUBLISH_LOCK:
        rec = _PUBLISHED
    if rec is None:
        return None
    return {
        # Defensive deep copy — callers can never mutate published state.
        "report": copy.deepcopy(rec["report"]),
        "probed_at": rec["probed_at"],
        "probed_monotonic": rec["probed_monotonic"],
        "simulated": rec["simulated"],
        "order": rec["_order"],
    }


def identity_cached() -> dict | None:
    """U-5d-0b alpha — internal, strictly cache-only access to the
    identity-evidence payload published with the last probe report.
    Same guarantees as probe_cached: never touches discovery, the OS,
    simulation sources or a subprocess; never refreshes; returns a
    defensive deep copy (or None when unpaired/absent).
    Internal-only surface — no route may serve it."""
    with _PUBLISH_LOCK:
        rec = _PUBLISHED
    if rec is None:
        return None
    return copy.deepcopy(rec["identity"])


def _reset_published() -> None:
    """Test-only hook: drop the published record (cold-cache state)."""
    global _PUBLISHED
    with _PUBLISH_LOCK:
        _PUBLISHED = None


def probe(force: bool = False) -> dict:
    if force:
        _cached_probe.cache_clear()
    raw = _cached_probe(int(time.time() // 300))
    # Strip the internal identity sideband — the serialized report
    # keeps its exact public shape; raw identifiers never escape.
    ident = raw.get("_identity") if isinstance(raw, dict) else None
    report = {k: v for k, v in raw.items() if k != "_identity"}
    if report.get("hardware_profile") is None:
        from app.domain import compatibility
        report = {**report, "hardware_profile": compatibility.classify_profile(report)["key"]}
    sim = _simulated_report()
    from app.domain import hw_identity
    if sim:
        report = {**report, **sim, "simulated": True}
        identity = hw_identity.simulated_payload(report.get("gpus") or [])
    elif ident is not None:
        identity = hw_identity.finalize(ident, provenance="real")
    else:
        identity = hw_identity.unavailable_payload()
    # Bind the evidence to THIS collection — identity evidence is only
    # ever published against the report whose collection produced it.
    identity["collection"] = {
        "probed_at": report.get("probed_at"),
        "probed_monotonic": report.get("probed_monotonic"),
        "simulated": bool(report.get("simulated")),
    }
    _publish(report, identity=identity)
    return report
