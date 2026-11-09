"""Hardware/environment probe.

Reports what is actually present — never assumes an NVIDIA GPU or any
specific vendor. GPU info comes from WMI via a PowerShell CIM query so
no extra driver dependencies are needed; fields that cannot be measured
are reported as null/unknown rather than guessed.
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


def _vendor_from_pnp(pnp_id: str) -> str:
    pnp = (pnp_id or "").upper()
    for needle, name in (("VEN_10DE", "NVIDIA"), ("VEN_1002", "AMD"), ("VEN_8086", "Intel")):
        if needle in pnp:
            return name
    return "unknown"


def _query_gpus() -> list[dict]:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", _PS_GPU_QUERY],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return []
        data = json.loads(out.stdout)
        if isinstance(data, dict):
            data = [data]
        gpus = []
        for row in data:
            # AdapterRAM is uint32 and under-reports past ~4 GB; flagged, not hidden.
            vram = row.get("AdapterRAM")
            gpus.append(
                {
                    "name": row.get("Name"),
                    "vendor": _vendor_from_pnp(row.get("PNPDeviceID") or ""),
                    "vram_bytes": vram,
                    # WMI caps AdapterRAM near the 4 GiB uint32 boundary; a
                    # reported value in that range may understate real VRAM.
                    "vram_capped_at_4gb": bool(vram and vram >= 0xF0000000),
                    "driver_version": row.get("DriverVersion"),
                    "role": "display",
                    "inference_capable": None,  # decided by adapter checks, not assumed
                }
            )
        return gpus
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        return []


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
        "cuda_detected": None,  # unknown until a runtime that can report it exists
    }


@lru_cache(maxsize=1)
def _cached_probe(_bucket: int) -> dict:
    """Probe results cached per ~5-minute bucket; hardware rarely changes."""
    vm = psutil.virtual_memory()
    return {
        "probed_at": time.time(),
        "os": {"system": platform.system(), "release": platform.release(), "version": platform.version()},
        "cpu": {
            "name": platform.processor() or "unknown",
            "physical_cores": psutil.cpu_count(logical=False),
            "logical_cores": psutil.cpu_count(logical=True),
        },
        "memory": {"total_bytes": vm.total, "available_bytes": vm.available},
        "gpus": _query_gpus(),
        "disks": _disks(),
        "runtimes": _runtimes(),
        "hardware_profile": None,  # Accessible|Creator|Studio classification lands with model registry
    }


def probe() -> dict:
    return _cached_probe(int(time.time() // 300))
