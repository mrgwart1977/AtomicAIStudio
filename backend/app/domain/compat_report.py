"""C5-E — consolidated model/runtime compatibility reporting.

Builds the single view the Model Compatibility page renders: catalog
metadata + runtime install state + hardware probe + the C4-E classifier,
folded into one availability class per model:

    installed_operational — installed and compatible on measured hardware
    installed_limited     — installed but compat is limited/unknown
    installable           — not installed, nothing blocks an approved install
    planned               — catalog-visible only, never installable
    blocked               — restricted license / unavailable / unsupported

Also provides the read-only validation report (catalog metadata, runtime
binary, model files, hash verification, capability-contract consistency)
and the install preflight (download size, disk, estimates, license).
Nothing here runs inference or downloads anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ModelInstall, RuntimeInstall
from app.domain import compatibility, model_catalog, model_files
from app.domain.model_catalog import ModelSpec

GIB = 1024 * 1024 * 1024

AVAILABILITY_CLASSES = (
    "installed_operational",
    "installed_limited",
    "installable",
    "planned",
    "blocked",
)


def _runtime_state(session: Session, settings) -> dict[str, dict]:
    """All runtime flavors keyed by '<runtime_key>-<flavor>' with install
    + binary-on-disk state."""
    rows = session.execute(select(RuntimeInstall)).scalars().all()
    out: dict[str, dict] = {}
    for r in rows:
        exe_ok = bool(r.exe_path) and Path(r.exe_path).is_file()
        out[r.runtime_key] = {
            "status": r.status,
            "version": r.version,
            "verified": r.verified,
            "exe_path": r.exe_path,
            "exe_present": exe_ok,
            "benchmark": (r.meta_json or {}).get("benchmark"),
            "error": r.error,
        }
    return out


def runtime_view(session: Session, settings, spec, rt_state: dict,
                 hw: dict) -> dict:
    """Per-runtime consolidated view: catalog spec + per-flavor install
    state + which flavors are usable on the probed backends."""
    backends = {k: v for k, v in (hw.get("backends") or {}).items()}
    flavors = []
    for f in spec.backends:
        row = rt_state.get(f"{spec.key}-{f}")
        installed = bool(row and row["status"] == "installed" and row["exe_present"])
        operational = bool(installed and row["verified"]
                           and ((row["error"] is None)
                                or ((row.get("benchmark") or {}).get("ok") is not False)))
        flavors.append({
            "flavor": f,
            "backend_available": bool(backends.get(f, {}).get("available")),
            "installed": installed,
            "operational": operational,
            "error": row["error"] if row else None,
            "benchmark": (row or {}).get("benchmark"),
        })
    return {
        **model_catalog.runtime_to_dict(spec),
        "flavors": flavors,
        "any_operational": any(fl["operational"] for fl in flavors),
        "planned": spec.version == "planned",
    }


def availability_class(spec: ModelSpec, compat: dict, install: dict) -> str:
    """Fold classification + install state into the five-way class the UI
    teaches: can I use it / install it / is it coming / is it blocked."""
    status = compat.get("status") or "unknown"
    installed = install.get("status") == "installed"
    if status == "planned":
        return "planned"
    if installed:
        return "installed_operational" if status == "compatible" else "installed_limited"
    if status in ("compatible", "limited", "unknown"):
        return "installable"
    return "blocked"  # restricted | unavailable | unsupported


def _model_entry(session: Session, settings, spec: ModelSpec, hw: dict,
                 installs: dict[str, dict], rt_state: dict,
                 model_rows: dict[str, ModelInstall]) -> dict:
    install = installs.get(spec.key) or {"status": "not_installed"}
    disk = model_files.disk_free_bytes(
        Path(install["install_root"])
        if install.get("install_root") else model_files.models_root(session, settings))
    compat = compatibility.classify_model(spec, hw, {**install, "disk_free_bytes": disk})
    rt_spec = model_catalog.get_runtime(spec.runtime_key)
    flavors = [f for f in (rt_spec.backends if rt_spec else ())
               if (rt_state.get(f"{spec.runtime_key}-{f}") or {}).get("status") == "installed"
               and rt_state[f"{spec.runtime_key}-{f}"]["exe_present"]]
    caps = spec.capabilities or {}
    row = model_rows.get(spec.key)
    bench = (row.meta_json or {}).get("benchmark") if row else None
    return {
        **model_catalog.spec_to_dict(spec),
        "compatibility": compat,
        "availability": availability_class(spec, compat, install),
        "install": install,
        "runtime": {
            "key": spec.runtime_key,
            "known": rt_spec is not None,
            "installed_flavors": flavors,
            "planned": rt_spec is not None and rt_spec.version == "planned",
        },
        "estimates": {
            "vram_bytes": compatibility.estimate_vram(spec),
            "ram_bytes": _estimate_ram(spec),
            "basis": "estimate",
        },
        "resolutions": caps.get("resolutions") or caps.get("sizes"),
        "fps": caps.get("fps"),
        "durations_s": caps.get("durations_s"),
        "benchmark": bench,
        "benchmarkable": spec.adapter_id == "gen.image.sdcpp"
                         and install.get("status") == "installed",
        "limitations": _limitations(spec, compat),
    }


def _estimate_ram(spec: ModelSpec) -> int | None:
    """Host-RAM estimate at runtime: offloaded weights live in RAM plus a
    runtime overhead floor. None when there's nothing to estimate from."""
    if not spec.files:
        return None
    weights = spec.total_bytes
    caps = spec.capabilities or {}
    return (weights if caps.get("weight_offload_cpu") else int(weights * 0.30)) + int(2 * GIB)


def _limitations(spec: ModelSpec, compat: dict) -> list[str]:
    out = list(compat.get("warnings") or [])
    if spec.notes:
        out.append(spec.notes)
    lic = spec.license or {}
    if not lic.get("commercial_ok", True):
        out.append(f"{lic.get('name', 'license')} restricts commercial use")
    return out


def build_report(session: Session, settings, hw: dict) -> dict:
    """The consolidated compatibility matrix for /api/compatibility."""
    installs = model_files.installs_by_key(session)
    rt_state = _runtime_state(session, settings)
    model_rows = {r.model_key: r for r in session.execute(
        select(ModelInstall).order_by(ModelInstall.created_at.desc())
    ).scalars().all() if r.status == "installed"}
    return {
        "hardware_profile": compatibility.classify_profile(hw),
        "runtimes": [runtime_view(session, settings, rs, rt_state, hw)
                     for rs in model_catalog.RUNTIMES.values()],
        "models": [_model_entry(session, settings, spec, hw, installs,
                                rt_state, model_rows)
                   for spec in model_catalog.catalog()],
    }


# ------------------------------------------------------------- preflight

def preflight(session: Session, settings, spec: ModelSpec, hw: dict) -> dict:
    """Beginner-facing install preflight — everything the approval dialog
    needs to say honestly. Read-only; the download itself still requires
    the explicit install approval that already gates /install."""
    root = model_files.models_root(session, settings)
    free = model_files.disk_free_bytes(root)
    installs = model_files.installs_by_key(session)
    compat = compatibility.classify_model(
        spec, hw, installs.get(spec.key)
        or {"status": "not_installed", "disk_free_bytes": free})
    rt_spec = model_catalog.get_runtime(spec.runtime_key)
    rt_state = _runtime_state(session, settings)
    rt_flavors = []
    if rt_spec:
        for f in rt_spec.backends:
            row = rt_state.get(f"{spec.runtime_key}-{f}")
            rt_flavors.append({
                "flavor": f,
                "installed": bool(row and row["status"] == "installed" and row["exe_present"]),
                "operational": bool(row and row["status"] == "installed"
                                    and row["verified"] and row["exe_present"]),
            })
    need = spec.requirements.get("min_disk_bytes") or spec.total_bytes
    lic = spec.license or {}
    return {
        "model_key": spec.key,
        "display_name": spec.display_name,
        "version": spec.version,
        "support": spec.support,
        "download_bytes": spec.total_bytes,
        "models_root": str(root),
        "disk_free_bytes": free,
        "disk_sufficient": (free is None or need is None or free >= need),
        "est_vram_bytes": compatibility.estimate_vram(spec),
        "est_ram_bytes": _estimate_ram(spec),
        "estimate_basis": "estimate",
        "runtime": {
            "key": spec.runtime_key,
            "display_name": rt_spec.display_name if rt_spec else spec.runtime_key,
            "known": rt_spec is not None,
            "planned": rt_spec is not None and rt_spec.version == "planned",
            "flavors": rt_flavors,
        },
        "license": {
            "name": lic.get("name"),
            "url": lic.get("url"),
            "commercial_ok": lic.get("commercial_ok", True),
            "notes": lic.get("notes"),
        },
        "compatibility": {"status": compat.get("status"),
                          "reasons": compat.get("reasons") or [],
                          "warnings": compat.get("warnings") or [],
                          "suggested": compat.get("suggested") or {}},
        "limitations": _limitations(spec, compat),
        # Nothing downloads without this UI's explicit confirm → install.
        "requires_explicit_approval": True,
        "auto_download": False,
        "installable": availability_class(spec, compat,
                                          installs.get(spec.key)
                                          or {"status": "not_installed"}) == "installable",
    }


# ------------------------------------------------------------ validation

def _check(name: str, ok: bool | None, detail: str,
           severity: str | None = None) -> dict:
    """ok True/False/None(=skipped); severity only when ok is False."""
    out = {"name": name, "ok": ok, "detail": detail}
    if ok is False:
        out["severity"] = severity or "error"
    return out


def validate_model(session: Session, settings, spec: ModelSpec,
                   registry, hw: dict, *, verify_hashes: bool = False) -> dict:
    """Read-only validation report for one catalog model.

    Never runs inference, never downloads. Hash checks are opt-in because
    multi-GB weight files take seconds-to-minutes to read; size+magic is
    always checked for installed files."""
    checks: list[dict] = []

    # --- catalog metadata completeness --------------------------------
    missing = []
    if not spec.display_name:
        missing.append("display_name")
    if not spec.version:
        missing.append("version")
    if not (spec.license or {}).get("name"):
        missing.append("license.name")
    if spec.support != "planned" and not spec.files:
        missing.append("files")
    if spec.support != "planned" and not spec.requirements:
        missing.append("requirements")
    checks.append(_check(
        "catalog_metadata", not missing,
        "catalog entry complete" if not missing
        else f"catalog entry missing: {', '.join(missing)}"))

    # --- runtime availability -----------------------------------------
    rt_spec = model_catalog.get_runtime(spec.runtime_key)
    if spec.support == "planned":
        checks.append(_check("runtime_available", None,
                             "planned model — runtime check skipped"))
    elif rt_spec is None:
        checks.append(_check("runtime_available", False,
                             f"runtime '{spec.runtime_key}' is not in the runtime registry"))
    elif rt_spec.version == "planned":
        checks.append(_check("runtime_available", False,
                             f"runtime '{rt_spec.display_name}' is planned — no installable build yet",
                             severity="info"))
    else:
        checks.append(_check("runtime_available", True,
                             f"runtime '{rt_spec.display_name}' ({rt_spec.key}) registered"))

    # --- runtime binary ------------------------------------------------
    installs = model_files.installs_by_key(session)
    install = installs.get(spec.key) or {"status": "not_installed"}
    rt_state = _runtime_state(session, settings)
    installed_flavors = [k.split("-", 1)[1] for k, v in rt_state.items()
                       if k.startswith(f"{spec.runtime_key}-")
                       and v["status"] == "installed"]
    if rt_spec is None or spec.support == "planned":
        checks.append(_check("runtime_binary", None, "skipped — no usable runtime"))
    elif not installed_flavors:
        checks.append(_check(
            "runtime_binary",
            False if install["status"] == "installed" else None,
            "model installed but its runtime is not installed"
            if install["status"] == "installed"
            else "runtime not installed — required before this model can run"))
    else:
        broken = [f for f in installed_flavors
                  if not rt_state[f"{spec.runtime_key}-{f}"]["exe_present"]]
        if broken:
            checks.append(_check("runtime_binary", False,
                                 f"runtime binary missing on disk for: {', '.join(broken)}"))
        else:
            checks.append(_check("runtime_binary", True,
                                 f"runtime binary present ({', '.join(installed_flavors)})"))

    # --- required model files ------------------------------------------
    if install["status"] != "installed":
        checks.append(_check("model_files", None,
                             "model not installed — nothing on disk to check"))
    else:
        base = Path(install["install_root"]) / install["rel_dir"]
        absent, resized = [], []
        for f in spec.files:
            p = base / f.name
            if not p.is_file():
                absent.append(f.name)
            elif f.size_bytes and p.stat().st_size != f.size_bytes:
                resized.append(f.name)
        if absent:
            checks.append(_check("model_files", False,
                                 f"missing files: {', '.join(absent)}"))
        elif resized:
            checks.append(_check("model_files", False,
                                 f"size mismatch vs catalog: {', '.join(resized)}"))
        else:
            checks.append(_check("model_files", True,
                                 f"all {len(spec.files)} file(s) present with expected sizes"))

    # --- hash verification (opt-in for multi-GB files) ------------------
    declared = [f for f in spec.files if f.sha256]
    if install["status"] != "installed":
        checks.append(_check("hash_verification", None, "model not installed"))
    elif not declared:
        checks.append(_check("hash_verification", None,
                             "no published checksums in catalog — size+magic checks only"))
    elif not verify_hashes:
        checks.append(_check("hash_verification", None,
                             f"{len(declared)} file(s) have published checksums — "
                             "verify with ?verify_hashes=true (reads full files)"))
    else:
        base = Path(install["install_root"]) / install["rel_dir"]
        bad, verified = [], 0
        for f in declared:
            p = base / f.name
            if not p.is_file():
                continue
            ok, _msg = model_files.verify_component(p, f, spec.fmt)
            if ok:
                verified += 1
            else:
                bad.append(f.name)
        checks.append(_check(
            "hash_verification", not bad,
            f"{verified}/{len(declared)} checksums verified"
            if not bad else f"checksum mismatch: {', '.join(bad)}"))

    # --- capability-contract consistency --------------------------------
    issues: list[str] = []
    if registry is not None:
        adapters = {a["adapter_id"]: a for a in registry.list_profiles()}
        for aid in (spec.adapter_id, *spec.compatible_adapters):
            prof = adapters.get(aid)
            if prof is None:
                issues.append(f"adapter '{aid}' not registered")
                continue
            tasks = set(prof.get("tasks") or ())
            supports = prof.get("supports") or {}
            for mode in spec.modes:
                # mode names map onto adapter task vocabulary
                task = {"text2image": "text2image", "image2image": "image2image",
                        "text2video": "text2video", "image2video": "image2video",
                        "audio2video": "audio2video"}.get(mode, mode)
                if spec.support in ("production", "tested") and task not in tasks:
                    issues.append(f"mode '{mode}' not in {aid} tasks")
            caps = spec.capabilities or {}
            if caps.get("ref_images") and not supports.get("ref_images"):
                issues.append(f"{aid} does not support ref_images")
            if caps.get("lora") and not supports.get("lora"):
                issues.append(f"{aid} does not support lora")
            if caps.get("audio") and not supports.get("audio"):
                issues.append(f"{aid} does not declare audio support")
    checks.append(_check(
        "capability_contract", not issues,
        "catalog capabilities consistent with registered adapters"
        if not issues else "; ".join(issues),
        severity="warning"))

    # --- unsupported parameter combinations ------------------------------
    caps = spec.capabilities or {}
    combos: list[str] = []
    sizes = caps.get("sizes") or {}
    if sizes.get("min") and sizes.get("max") and sizes["min"] > sizes["max"]:
        combos.append(f"sizes min {sizes['min']} > max {sizes['max']}")
    if "image2video" in spec.modes and not caps.get("ref_images") \
            and not caps.get("image_input"):
        combos.append("image2video declared without an image-input capability")
    if caps.get("fps") and caps.get("durations_s") is None and "video" in spec.modality:
        combos.append("fps declared without supported durations")
    if spec.modality == "video" and spec.support in ("production", "tested") \
            and not caps.get("resolutions") and not caps.get("sizes"):
        combos.append("video model without declared resolutions")
    checks.append(_check(
        "param_combinations", not combos,
        "no contradictory capability declarations"
        if not combos else "; ".join(combos), severity="warning"))

    # --- driver / runtime compatibility ----------------------------------
    if installed_flavors:
        hw_backends = {k for k, v in (hw.get("backends") or {}).items()
                       if v.get("available")}
        unusable = [f for f in installed_flavors
                    if f != "cpu" and f not in hw_backends]
        if unusable:
            checks.append(_check(
                "driver_runtime", False,
                f"installed flavor(s) {', '.join(unusable)} have no detected "
                "backend/driver on this machine", severity="warning"))
        else:
            checks.append(_check("driver_runtime", True,
                                 "installed flavors match detected backends"))
    else:
        checks.append(_check("driver_runtime", None,
                             "no runtime flavor installed to check"))

    return {
        "model_key": spec.key,
        "read_only": True,
        "ran_inference": False,
        "verify_hashes": verify_hashes,
        "checks": checks,
        "ok": all(c["ok"] is not False or c.get("severity") == "warning"
                  for c in checks),
    }
