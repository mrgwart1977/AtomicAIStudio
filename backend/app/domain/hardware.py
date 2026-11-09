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

import json
import platform
import shutil
import subprocess
import time
from functools import lru_cache
from importlib.util import find_spec

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


def _device_info() -> dict:
    """Registry VRAM map + Vulkan ICD count. Empty on failure/non-Windows."""
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
    return {"vram_by_device": vram, "vulkan_icd_count": icd_count, "vulkan_dll": dll}


def _nvidia_smi() -> list[dict]:
    """Live NVIDIA memory/driver readings when the driver tool exists.
    Returns [] when absent — NVIDIA is never assumed."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return []
    try:
        out = subprocess.run(
            [smi, "--query-gpu=index,name,memory.total,memory.free,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            return []
        rows = []
        for line in out.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                rows.append({
                    "smi_index": int(parts[0]), "name": parts[1],
                    "vram_bytes": int(parts[2]) * 1024 * 1024,
                    "vram_free_bytes": int(parts[3]) * 1024 * 1024,
                    "driver_version": parts[4],
                })
        return rows
    except (subprocess.SubprocessError, ValueError):
        return []


def _query_gpus(devinfo: dict) -> list[dict]:
    data = _ps_json(_PS_GPU_QUERY)
    rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    vram_map = devinfo["vram_by_device"]
    smi_rows = _nvidia_smi()
    nvidia_gpu_count = sum(1 for r in rows if "VEN_10DE" in (r.get("PNPDeviceID") or "").upper())

    gpus = []
    for i, row in enumerate(rows):
        pnp = (row.get("PNPDeviceID") or "").upper()
        vendor = _vendor_from_pnp(pnp)
        wmi_vram = row.get("AdapterRAM")
        # Prefer the registry QWORD (accurate past the WMI uint32 cap),
        # then nvidia-smi, then WMI with the cap flagged.
        vram = wmi_vram
        vram_source = "wmi_capped" if wmi_vram and wmi_vram >= 0xF0000000 else "wmi"
        reg_hit = next((qw for rid, qw in vram_map.items() if pnp.startswith(rid)), None)
        if reg_hit:
            vram, vram_source = reg_hit, "registry"
        smi_hit = None
        if vendor == "NVIDIA":
            match = [r for r in smi_rows
                     if r["name"].lower() in (row.get("Name") or "").lower()]
            if not match and nvidia_gpu_count == 1:
                match = smi_rows[:1]
            smi_hit = match[0] if match else None
            if smi_hit and (not vram or vram < smi_hit["vram_bytes"]):
                vram, vram_source = smi_hit["vram_bytes"], "nvidia-smi"
        gpus.append(
            {
                "index": i,  # enumeration index — the scheduler's device selector
                "name": row.get("Name"),
                "vendor": vendor,
                "vram_bytes": vram,
                "vram_source": vram_source,
                "vram_free_bytes": smi_hit["vram_free_bytes"] if smi_hit else None,
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
    vm = psutil.virtual_memory()
    devinfo = _device_info()
    gpus = _query_gpus(devinfo)
    return {
        "probed_at": time.time(),
        "os": {"system": platform.system(), "release": platform.release(), "version": platform.version()},
        "cpu": {
            "name": platform.processor() or "unknown",
            "physical_cores": psutil.cpu_count(logical=False),
            "logical_cores": psutil.cpu_count(logical=True),
        },
        "memory": {"total_bytes": vm.total, "available_bytes": vm.available},
        "gpus": gpus,
        "disks": _disks(),
        "backends": _backends(devinfo, gpus),
        "runtimes": _runtimes(),
        "hardware_profile": None,  # filled below from classify_profile
    }


def probe(force: bool = False) -> dict:
    if force:
        _cached_probe.cache_clear()
    report = _cached_probe(int(time.time() // 300))
    if report.get("hardware_profile") is None:
        from app.domain import compatibility
        report = {**report, "hardware_profile": compatibility.classify_profile(report)["key"]}
    return report
