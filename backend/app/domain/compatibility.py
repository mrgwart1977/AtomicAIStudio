"""Model↔hardware compatibility engine.

Classifies each catalog model against the measured hardware probe:
compatible | limited | unsupported | unknown | restricted | unavailable.
Every classification carries human-readable reasons plus honest estimates
(clearly marked "estimate", never presented as guaranteed). Suggestions
point at safer settings or lighter models rather than silently lowering
quality.
"""

from __future__ import annotations

from typing import Any

from app.domain.model_catalog import MODELS, ModelSpec, get

GIB = 1024 * 1024 * 1024

# Fraction of measured total VRAM treated as usable when free VRAM cannot
# be measured — the OS, display, and other apps need their share.
_USABLE_VRAM_FRACTION = 0.85
# Extra headroom kept free during inference for stability.
_HEADROOM_BYTES = int(0.7 * GIB)


def _fmt_gb(b: int | float | None) -> str:
    if b is None:
        return "unknown"
    return f"{b / GIB:.1f} GB"


# ---------------------------------------------------------------- profile

def classify_profile(hw: dict) -> dict:
    """E.1.5 hardware profile from measured capabilities — never from GPU
    names alone. Returns profile key + human label + what it implies."""
    gpus = hw.get("gpus") or []
    measured = [g for g in gpus if g.get("vram_bytes")]
    max_vram = max((g["vram_bytes"] for g in measured), default=0)
    n_gpu = len(gpus)
    if not gpus:
        key, label = "cpu_only", "CPU only"
        detail = "No GPU detected — generation runs on CPU (slow) or not at all."
    elif n_gpu > 1:
        key, label = "workstation_multi", "Multi-GPU workstation"
        detail = f"{n_gpu} GPUs — independent jobs can run on separate GPUs."
    elif max_vram < 6 * GIB:
        key, label = "entry", "Entry-level GPU"
        detail = "Smaller models and resolutions recommended."
    elif max_vram < 12 * GIB:
        key, label = "mid", "Mid-range GPU"
        detail = "Comfortable with SD 1.5-class models at standard sizes."
    elif max_vram < 24 * GIB:
        key, label = "high", "High-end GPU"
        detail = "SDXL-class models run well."
    else:
        key, label = "pro", "Professional GPU"
        detail = "Largest models and highest resolutions supported."
    return {"key": key, "label": label, "detail": detail,
            "gpu_count": n_gpu, "max_vram_bytes": max_vram or None}


# ------------------------------------------------------------- estimation

def estimate_vram(spec: ModelSpec, width: int = 512, height: int = 512,
                  batch: int = 1) -> int | None:
    """Estimated peak VRAM for one generation — ESTIMATE, not a guarantee.

    Model: resident weights (catalog anchor) + activation that scales with
    pixel count relative to each model's native baseline. Batch scales
    activation further (weights load once)."""
    if not spec.vram_baseline_bytes or not spec.vram_weight_bytes:
        return None
    native_px = 1024 * 1024 if spec.family == "sdxl" else 512 * 512
    act_base = spec.vram_baseline_bytes - spec.vram_weight_bytes
    px_ratio = (width * height) / native_px
    act = act_base * px_ratio * max(1, batch)
    return int(spec.vram_weight_bytes + act)


def usable_vram(gpu: dict) -> int | None:
    """Best honest usable-VRAM figure for a GPU: measured free memory if
    the driver reports it, else 85% of measured total, else None."""
    free = gpu.get("vram_free_bytes")
    if free:
        return free
    total = gpu.get("vram_bytes")
    if total:
        return int(total * _USABLE_VRAM_FRACTION)
    return None


# --------------------------------------------------------- classification

def _suggested(spec: ModelSpec, gpus: list[dict], cpu_ok: bool) -> dict:
    out: dict[str, Any] = {}
    caps = spec.capabilities or {}
    sizes = caps.get("sizes") or {}
    if sizes.get("min"):
        out["reduce_size_to"] = sizes["min"]
    if caps.get("cpu_fallback") or cpu_ok:
        out["cpu_fallback"] = True
    lighter = [k for k, s in MODELS.items()
               if s.family == spec.family
               and (s.requirements.get("min_vram_bytes") or 0) < (spec.requirements.get("min_vram_bytes") or 0)]
    if lighter:
        out["lighter_models"] = lighter
    if gpus:
        out["close_gpu_apps"] = True
    return out


def classify_model(spec: ModelSpec, hw: dict, install: dict | None = None) -> dict:
    """Classify one model against the probe. install (model_installs row as
    dict) adds installed-state info to the same answer shape."""
    reasons: list[str] = []
    warnings: list[str] = []
    req = spec.requirements or {}
    lic = spec.license or {}
    backends_needed = set(req.get("backends") or ["cpu"])
    hw_backends = {k for k, v in (hw.get("backends") or {}).items() if v.get("available")}
    gpus = hw.get("gpus") or []

    if not lic.get("commercial_ok", True):
        return {
            "status": "restricted",
            "reasons": [f"{lic.get('name', 'license')} does not allow production/commercial use."],
            "warnings": [], "suggested": {},
            "estimate": {"vram_bytes": estimate_vram(spec), "basis": "estimate"},
        }
    if not spec.files:
        return {
            "status": "unavailable",
            "reasons": ["no verified downloadable artifact in this build"],
            "warnings": [], "suggested": {},
            "estimate": {"vram_bytes": estimate_vram(spec), "basis": "estimate"},
        }
    if not backends_needed & hw_backends:
        return {
            "status": "unsupported",
            "reasons": ["no usable inference backend detected "
                        f"(model needs one of: {', '.join(sorted(backends_needed))})"],
            "warnings": [], "suggested": _suggested(spec, gpus, cpu_ok=False),
            "estimate": {"vram_bytes": estimate_vram(spec), "basis": "estimate"},
        }

    est = estimate_vram(spec)
    any_vram_measured = any(g.get("vram_bytes") for g in gpus)
    fitting_gpus: list[dict] = []
    tight_gpus: list[dict] = []
    for g in gpus:
        usable = usable_vram(g)
        if usable is None or est is None:
            continue
        if est + _HEADROOM_BYTES <= usable:
            fitting_gpus.append(g)
        elif est <= usable:
            tight_gpus.append(g)

    status = "compatible"
    cpu_only = not gpus
    if cpu_only:
        if "cpu" in backends_needed:
            status = "limited"
            reasons.append("no GPU detected — model can run on CPU but will be slow")
        else:
            status = "unsupported"
            reasons.append("no GPU detected and this model has no CPU path")
    elif not fitting_gpus and not tight_gpus:
        if not any_vram_measured:
            status = "unknown"
            reasons.append("GPU present but VRAM could not be measured — "
                           "compatibility cannot be established")
        else:
            min_vram = req.get("min_vram_bytes")
            best = max((usable_vram(g) or 0) for g in gpus)
            if min_vram and best < min_vram:
                status = "unsupported"
                reasons.append(f"needs ~{_fmt_gb(min_vram)} VRAM; "
                               f"best usable device has ~{_fmt_gb(best)}")
            elif est and best < est:
                status = "limited"
                reasons.append("estimated memory need exceeds usable VRAM at default settings")
            else:
                status = "limited"
                reasons.append("may fit only with reduced settings")
    elif tight_gpus and not fitting_gpus:
        status = "limited"
        warnings.append("fits, but with little headroom — reduce size or close other GPU apps")

    ram = (hw.get("memory") or {}).get("total_bytes")
    min_ram = req.get("min_ram_bytes")
    if min_ram and ram and ram < min_ram:
        if status == "compatible":
            status = "limited"
        warnings.append(f"system RAM {_fmt_gb(ram)} is below the recommended {_fmt_gb(min_ram)}")

    disk_hint = None
    install_disk = (install or {}).get("disk_free_bytes")
    need = req.get("min_disk_bytes") or spec.total_bytes
    if install_disk is not None and need and install_disk < need:
        disk_hint = f"install drive has {_fmt_gb(install_disk)} free; needs ~{_fmt_gb(need)}"
        warnings.append(disk_hint)

    if status == "compatible" and not reasons:
        gpu_names = ", ".join(g["name"] or "GPU" for g in (fitting_gpus or tight_gpus))
        reasons.append(f"fits within measured resources{f' on {gpu_names}' if gpu_names else ''}")

    return {
        "status": status,
        "reasons": reasons,
        "warnings": warnings,
        "suggested": _suggested(spec, gpus, cpu_ok="cpu" in backends_needed)
        if status in ("limited", "unsupported", "unknown") else {},
        "estimate": {"vram_bytes": est, "basis": "estimate"},
        "install": install or None,
    }


def classify_all(hw: dict, installs: dict[str, dict]) -> dict[str, dict]:
    return {key: classify_model(spec, hw, installs.get(key)) for key, spec in MODELS.items()}
