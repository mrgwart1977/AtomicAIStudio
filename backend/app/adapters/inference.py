"""Inference + model-management adapters.

`gen.image.sdcpp` runs real local inference through stable-diffusion.cpp
as a SUBPROCESS WORKER — the engine spawns one `sd.exe` per job, so a
crash/OOM in inference can never corrupt the backend or other jobs, and
cancel = kill the process. Progress is parsed from the worker's stdout
and streamed through the normal job pipeline.

Device selection is runtime-flavor-specific and honest: Vulkan builds
honor GGML_VULKAN_DEVICE; CUDA builds honor CUDA_VISIBLE_DEVICES; CPU
flavors ignore both. Anything the runtime can't do is a validation
error, not a silent fallback.

`ops.model.download` installs runtimes + model weights on explicit user
request — resumable Range downloads with integrity verification before a
model is marked installed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator

from sqlalchemy import select

from app.adapters.base import CapabilityProfile, JobCancelled, JobHandle
from app.db.models import RuntimeInstall, utcnow
from app.domain import compatibility, hardware, media_probe, model_catalog, model_files
from app.domain.model_catalog import ModelSpec
from app.domain.backup import ProgressReporter

log = logging.getLogger(__name__)

_STEP_RE = re.compile(r"(?:step|sampling)\s*[:=]?\s*(\d+)\s*/\s*(\d+)", re.I)
_ANY_FRAC_RE = re.compile(r"\b(\d{1,3})\s*/\s*(\d{1,4})\b")

_OOM_PATTERNS = ("out of memory", "failed to allocate", "vulkan_error", "vk_outofdevice",
                 "ggml_vulkan: no", "cuda_error", "cublas")


def _friendly_error(log_tail: str, model: ModelSpec | None) -> str:
    low = log_tail.lower()
    if any(p in low for p in _OOM_PATTERNS):
        hint = " — try a smaller resolution, fewer steps, or the CPU lane"
        return f"the device ran out of memory during generation{hint}"
    if "no vulkan" in low or "failed to initialize vulkan" in low:
        return "the Vulkan backend failed to initialize — update the GPU driver or use the CPU lane"
    tail = log_tail.strip().splitlines()[-1][:300] if log_tail.strip() else "worker exited with an error"
    return f"generation failed: {tail}"


def _device_env(flavor: str, gpu_index: int | None) -> dict[str, str]:
    """Env vars that pin a worker subprocess to one GPU. -1/None → CPU lane."""
    if gpu_index is None or gpu_index < 0:
        return {}
    idx = str(gpu_index)
    if flavor == "cuda":
        return {"CUDA_VISIBLE_DEVICES": idx}
    # Vulkan + default: GGML_VULKAN_DEVICE selects the physical device.
    return {"GGML_VULKAN_DEVICE": idx, "GGML_VK_VISIBLE_DEVICES": idx}


class SdCppAdapter:
    """Real text-to-image via a stable-diffusion.cpp `sd` worker binary.

    The binary path comes from the installed runtime (runtime_installs /
    runtimes/<key>-<flavor>/sd.exe); tests inject ATOMIC_SDCPP_CMD as a
    command prefix so the same spawn/parse/cancel path is exercised
    without real weights.
    """

    profile = CapabilityProfile(
        adapter_id="gen.image.sdcpp",
        display_name="Local image generation (stable-diffusion.cpp)",
        adapter_kind="local",
        tasks=("text2image",),
        supports={
            "negative_prompt": True, "seed": True, "size": True,
            "steps": True, "cfg": True, "sampler": True, "batch": True,
            "gpu_selection": True, "cpu_fallback": True, "ref_images": False,
            "lora": True,
        },
        limits={"max_batch": 4},
        requires_cloud=False,
        cost_model="free",
        lane="gen",
        needs_gpu=True,
        notes="Real local inference via a subprocess worker (sd.cpp). "
              "Requires an installed model; all downloads are user-approved.",
    )

    def __init__(self, session_factory, settings) -> None:
        self._sf = session_factory
        self._settings = settings

    # ------------------------------------------------------------ helpers

    def _resolve(self, request: dict) -> tuple[ModelSpec, Path, Path | None]:
        key = request.get("model_key")
        spec = model_catalog.get(key or "")
        if spec is None:
            raise ValueError(f"unknown model '{key}'")
        if spec.modality != "image" or "text2image" not in spec.modes or spec.support not in ("production", "tested"):
            raise ValueError(
                f"model '{spec.display_name}' ({spec.modality}, {spec.support}) is not usable for image generation")
        with self._sf() as s:
            weight = model_files.installed_file(s, self._settings, spec)
        if weight is None:
            raise ValueError(f"model '{spec.display_name}' is not installed — install it from the Model Manager first")
        exe = self._exe_path(request)
        return spec, weight, exe

    def _exe_path(self, request: dict) -> Path | None:
        override = getattr(self._settings, "sdcpp_cmd", None) or os.environ.get("ATOMIC_SDCPP_CMD")
        if override:
            self._flavor_reason = "test/override command"
            return None  # command prefix handled in _argv_prefix
        rt_spec = model_catalog.get_runtime(request.get("runtime_key") or "sdcpp")
        flavor, reason = self._pick_flavor(request.get("runtime_flavor"), rt_spec)
        self._flavor_reason = reason
        exe = model_files.runtime_exe(self._settings, rt_spec, flavor)
        return exe

    def _pick_flavor(self, requested: str | None, rt_spec) -> tuple[str, str]:
        """Backend-flavor resolution. Order (B.3 — validated state over
        assumptions): an explicit request wins (with an actionable error when
        the pick isn't operational); then *operational* installed flavors
        ranked by measured benchmark medians on THIS GPU; then operational
        installed flavors in catalog order; then probe order. A flavor is
        operational when its install row is installed+verified and its
        launch smoke check passes (re-checked lazily until it does, so a
        repaired install recovers automatically)."""
        if rt_spec is not None:
            prefix = f"{rt_spec.key}-"
            with self._sf() as s:
                rows = {row.runtime_key: row for row in s.execute(
                    select(RuntimeInstall).where(RuntimeInstall.status.in_(["installed", "error"]))
                ).scalars().all()}
                if requested:
                    row = rows.get(prefix + requested)
                    if row and row.status == "installed":
                        ok, detail = model_files.smoke_check_runtime(row.exe_path)
                        meta = dict(row.meta_json or {})
                        meta["smoke"] = {"ok": ok, "detail": detail[-400:],
                                         "checked_at": utcnow().isoformat()}
                        row.meta_json = meta
                        if not ok:
                            row.status = "error"
                            row.error = ("runtime installed but failed its launch check — "
                                         f"missing dependencies? {detail[:160]}")
                            s.commit()
                    if row and row.status == "error":
                        raise model_files.ModelError(
                            f"runtime {prefix + requested} is installed but not operational — "
                            f"{row.error or 'launch check failed'}")
                operational = []
                for f in rt_spec.backends:
                    row = rows.get(prefix + f)
                    if not row or row.status != "installed" or not row.verified:
                        continue
                    meta = dict(row.meta_json or {})
                    smoke = meta.get("smoke") or {}
                    if not smoke.get("ok"):
                        ok, detail = model_files.smoke_check_runtime(row.exe_path)
                        meta["smoke"] = {"ok": ok, "detail": detail[-400:],
                                         "checked_at": utcnow().isoformat()}
                        row.meta_json = meta
                        if not ok:
                            row.status = "error"
                            row.error = ("runtime installed but failed its launch check — "
                                         f"missing dependencies? {detail[:160]}")
                            continue
                        row.error = None
                    operational.append((f, row))
                s.commit()
            if requested:
                return requested, "manually selected backend"
            if not operational:
                return self._default_flavor(), \
                    "no operational runtime installed — defaulting to detected backend"
            if len(operational) > 1:
                gpu_names = {g.get("name") for g in (hardware.probe().get("gpus") or [])}
                measured = []
                for f, row in operational:
                    bench = ((row.meta_json or {}).get("benchmark") or {})
                    med = bench.get("median_total_ms")
                    # measured data only applies to the GPU it was recorded on
                    if med and (not bench.get("gpu") or bench.get("gpu") in gpu_names):
                        measured.append((float(med), f))
                if measured:
                    measured.sort()
                    best = measured[0]
                    best_bench = ((next(r for f2, r in operational if f2 == best[1]).meta_json or {})
                                  .get("benchmark") or {})
                    return best[1], (
                        f"fastest measured backend (median {best[0] / 1000:.1f}s "
                        f"over {int(best_bench.get('runs') or 0)} runs)")
            return operational[0][0], "only operational backend" if len(operational) == 1 \
                else "installed backend (no benchmark data yet)"
        if requested:
            return requested, "manually selected backend"
        return self._default_flavor(), "detected backend"

    @staticmethod
    def _split_cmd(override: str) -> list[str]:
        """Split an override command line without mangling Windows paths:
        POSIX-mode shlex eats backslashes; posix=False keeps quotes in the
        tokens so we strip them ourselves."""
        import shlex
        parts = shlex.split(override, posix=False)
        return [p[1:-1] if len(p) > 1 and p[0] == p[-1] and p[0] in "\"'" else p
                for p in parts]

    def _argv_prefix(self, request: dict) -> list[str]:
        override = getattr(self._settings, "sdcpp_cmd", None) or os.environ.get("ATOMIC_SDCPP_CMD")
        if override:
            return self._split_cmd(override)
        exe = self._exe_path(request)
        if exe is None or not exe.is_file():
            raise ValueError(
                "inference runtime (stable-diffusion.cpp) is not installed — "
                "install it from the Model Manager")
        return [str(exe)]

    def _default_flavor(self) -> str:
        b = hardware.probe().get("backends") or {}
        if b.get("vulkan", {}).get("available"):
            return "vulkan"
        if b.get("cuda", {}).get("available"):
            return "cuda"
        return "cpu"

    # ------------------------------------------------------------ contract

    async def validate(self, request: dict[str, Any]) -> list[str]:
        errors = []
        if not (request.get("prompt") or "").strip():
            errors.append("prompt is required")
        spec = model_catalog.get(request.get("model_key") or "")
        if spec is None:
            errors.append("model_key is required")
        else:
            caps = spec.capabilities or {}
            sizes = caps.get("sizes") or {}
            w, h = int(request.get("width", 512)), int(request.get("height", 512))
            for label, v in (("width", w), ("height", h)):
                if sizes.get("min") and v < sizes["min"]:
                    errors.append(f"{label} {v} below model minimum {sizes['min']}")
                if sizes.get("max") and v > sizes["max"]:
                    errors.append(f"{label} {v} above model maximum {sizes['max']}")
            steps = request.get("steps")
            rng = caps.get("steps") or {}
            if steps is not None and rng and not (rng.get("min", 1) <= int(steps) <= rng.get("max", 150)):
                errors.append(f"steps {steps} outside {rng.get('min')}–{rng.get('max')} for this model")
            batch = int(request.get("batch", 1))
            if not 1 <= batch <= (caps.get("batch", {}).get("max") or 1):
                errors.append(f"batch {batch} outside supported range")
        return errors

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        spec, weight, _ = self._resolve(handle.request)
        req = handle.request
        seed = req.get("seed", -1)
        if seed in (-1, "random", None):
            seed = random.randint(0, 2**31 - 1)
        steps = int(req.get("steps", spec.capabilities.get("steps", {}).get("default", 25)))
        width = int(req.get("width", 512))
        height = int(req.get("height", 512))
        batch = int(req.get("batch", 1))

        work = Path(handle.work_dir)
        work.mkdir(parents=True, exist_ok=True)
        out_base = work / f"{handle.job_id}"

        argv = self._argv_prefix(req) + [
            "-M", "img_gen",  # valid modes: img_gen, adetailer, vid_gen, convert, upscale, metadata, vid_upscale
            "-m", str(weight),
            "-p", req["prompt"],
            "-o", str(out_base) + ".png",
            "-W", str(width), "-H", str(height),
            "--steps", str(steps),
            "--cfg-scale", str(req.get("cfg", 7.0)),
            "--seed", str(seed),
            "--batch-count", str(batch),
            "-v",
        ]
        if req.get("negative_prompt"):
            argv += ["-n", req["negative_prompt"]]
        if req.get("sampler"):
            argv += ["--sampling-method", req["sampler"]]
        rt_spec = model_catalog.get_runtime(req.get("runtime_key") or "sdcpp")
        flavor, flavor_reason = self._pick_flavor(req.get("runtime_flavor"), rt_spec)
        gpu = req.get("assigned_gpu")
        if gpu in (None, "auto"):
            gpu = None if flavor == "cpu" else 0
        env = dict(os.environ)
        env.update(_device_env(flavor, gpu if isinstance(gpu, int) else None))

        yield {"progress": 0.02, "message": "starting inference worker"}
        started = time.time()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env, cwd=str(work),
        )
        log_tail: list[str] = []
        cancelled = False
        try:
            while True:
                if handle.cancel_event is not None and handle.cancel_event.is_set():
                    cancelled = True
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                    break
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    log_tail.append(text)
                    del log_tail[:-20]
                m = _STEP_RE.search(text) or _ANY_FRAC_RE.search(text)
                if m:
                    cur, tot = int(m.group(1)), int(m.group(2))
                    if tot:
                        yield {"progress": 0.05 + 0.9 * (cur / tot),
                               "message": f"sampling {cur}/{tot}",
                               "detail": {"phase": "sampling", "step": cur, "steps_total": tot}}
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

        if cancelled:
            for f in work.glob(f"{handle.job_id}*.png"):
                f.unlink(missing_ok=True)
            raise JobCancelled()

        duration_ms = int((time.time() - started) * 1000)
        outputs = sorted(work.glob(f"{handle.job_id}*.png"), key=lambda p: p.name)
        if proc.returncode != 0 or not outputs:
            for f in outputs:
                f.unlink(missing_ok=True)
            raise ValueError(_friendly_error("\n".join(log_tail), spec))

        assets = []
        for i, path in enumerate(outputs):
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            assets.append({
                "path": str(path),
                "rel_path": str(path.relative_to(self._settings.data_dir))
                if str(path).startswith(str(self._settings.data_dir)) else None,
                "sha256": sha,
                "name": f"{spec.key}-{seed}-{i + 1}.png",
                "kind": "image",
            })

        media = {
            "source": "generated",
            "generated": True,
            "model_key": spec.key,
            "model_version": spec.version,
            "model_family": spec.family,
            "adapter_id": self.profile.adapter_id,
            "runtime": {"key": "sdcpp", "flavor": flavor, "selection_reason": flavor_reason},
            "prompt": req["prompt"],
            "negative_prompt": req.get("negative_prompt") or "",
            "width": width, "height": height,
            "steps": steps,
            "cfg": req.get("cfg", 7.0),
            "sampler": req.get("sampler") or "euler_a",
            "seed": seed,
            "batch": batch,
            "gpu_index": req.get("assigned_gpu") if isinstance(req.get("assigned_gpu"), int) else None,
            "duration_ms": duration_ms,
            "generated_at": utcnow().isoformat(),
            "is_mock": False,
        }
        out: dict[str, Any] = {
            "done": True, "progress": 1.0,
            "output": {
                "path": assets[0]["path"],
                "sha256": assets[0]["sha256"],
                "name": assets[0]["name"],
                "kind": "image",
                "origin": "generation",
                "media": media,
                "extra_assets": assets[1:],
            },
        }
        if req.get("shot_id") and req.get("set_thumbnail"):
            out["output"]["apply_command"] = {
                "command_type": "shot.update",
                "payload": {"shot_id": req["shot_id"], "thumbnail_asset_id": "__asset__"},
            }
        yield out


class SdCppVideoAdapter(SdCppAdapter):
    """Local video generation via sd.cpp `-M vid_gen` (Wan family).

    Same subprocess-per-job discipline as the image adapter: fresh worker,
    streamed log progress, terminate-on-cancel, validated-output commit.
    Multi-component models (diffusion + VAE + text encoder [+ image
    encoder]) resolve through installed_component().
    """

    profile = CapabilityProfile(
        adapter_id="gen.video.sdcpp",
        display_name="Local video generation (stable-diffusion.cpp)",
        adapter_kind="local",
        tasks=("text2video", "image2video"),
        supports={
            "negative_prompt": True, "seed": True, "size": True,
            "steps": True, "cfg": True, "sampler": True, "batch": False,
            "gpu_selection": True, "cpu_fallback": False, "ref_images": False,
            "frames": True, "fps": True, "init_image": True, "lora": False,
        },
        limits={"max_frames": 129, "batch": 1},
        requires_cloud=False,
        cost_model="free",
        lane="gen",
        needs_gpu=True,
        # M4.1 contracts, declared now so schedulers/UIs can read them:
        worker_model="subprocess-per-job",      # persistent workers NOT claimed
        resumable=False,                        # vid_gen has no checkpoints
        recoverable_outputs="commit-on-success",  # partial output never kept
        notes="Real local video inference via a subprocess worker (sd.cpp "
              "vid_gen). Requires an installed multi-component video model; "
              "all downloads are user-approved.",
    )

    def _resolve(self, request: dict) -> tuple[ModelSpec, Path, Path | None]:
        key = request.get("model_key")
        spec = model_catalog.get(key or "")
        if spec is None:
            raise ValueError(f"unknown model '{key}'")
        allowed = ("production", "tested")
        if os.environ.get("ATOMIC_ALLOW_EXPERIMENTAL_GEN") == "1":
            allowed = allowed + ("experimental",)  # validation runs only; never shipped default
        if spec.modality != "video" or spec.support not in allowed:
            raise ValueError(
                f"model '{spec.display_name}' ({spec.modality}, {spec.support}) is not usable for video generation")
        with self._sf() as s:
            weight = model_files.installed_file(s, self._settings, spec)
            if weight is None:
                raise ValueError(
                    f"model '{spec.display_name}' is not installed — install it from the Model Manager first")
            vae = model_files.installed_component(s, self._settings, spec, "vae")
            if vae is None:
                raise ValueError(f"model '{spec.display_name}' VAE component is missing — reinstall")
            tenc = model_files.installed_component(s, self._settings, spec, "text_encoder")
            if tenc is None:
                raise ValueError(f"model '{spec.display_name}' text-encoder component is missing — reinstall")
            imgenc = model_files.installed_component(s, self._settings, spec, "image_encoder")
        self._components = {"vae": vae, "text_encoder": tenc, "image_encoder": imgenc}
        exe = self._exe_path(request)
        return spec, weight, exe

    async def validate(self, request: dict) -> list[str]:
        errors = []
        if not (request.get("prompt") or "").strip() and not request.get("image_asset_id"):
            errors.append("a prompt or a source image is required")
        try:
            spec = model_catalog.get(request.get("model_key") or "")
            caps = (spec.capabilities if spec else {}).get("frames", {})
            frames = int(request.get("frames") or caps.get("default", 33))
            lo, hi = caps.get("min", 1), caps.get("max", 129)
            if not lo <= frames <= hi:
                errors.append(f"frames {frames} outside {lo}–{hi} for this model")
        except (TypeError, ValueError):
            errors.append("invalid frames value")
        return errors

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        spec, weight, _ = self._resolve(handle.request)
        req = handle.request
        seed = req.get("seed", -1)
        if seed in (-1, "random", None):
            seed = random.randint(0, 2**31 - 1)
        steps = int(req.get("steps", spec.capabilities.get("steps", {}).get("default", 30)))
        width = int(req.get("width", 832))
        height = int(req.get("height", 480))
        frames = int(req.get("frames", spec.capabilities.get("frames", {}).get("default", 33)))
        fps = int(req.get("fps", spec.capabilities.get("fps", {}).get("default", 24)))

        work = Path(handle.work_dir)
        work.mkdir(parents=True, exist_ok=True)
        out_base = work / f"{handle.job_id}"
        comp = self._components

        argv = self._argv_prefix(req) + [
            "-M", "vid_gen",
            "--diffusion-model", str(weight),
            "--vae", str(comp["vae"]),
            "--t5xxl", str(comp["text_encoder"]),
            "-p", req.get("prompt") or "",
            "-o", str(out_base) + ".webm",
            "-W", str(width), "-H", str(height),
            "--steps", str(steps),
            "--cfg-scale", str(req.get("cfg", spec.capabilities.get("cfg", {}).get("default", 6.0))),
            "--seed", str(seed),
            "--video-frames", str(frames),
            "--fps", str(fps),
            # sd.cpp wan.md: all Wan examples pass --flow-shift 3.0
            "--flow-shift", str(req.get("flow_shift", 3.0)),
            # Wan VAE decode is memory-hungry (wan.md VAE caveat) — spatial
            # tiling keeps it inside an 8 GB budget.
            "--vae-tiling",
            "-v",
        ]
        if comp.get("image_encoder"):
            argv += ["--clip_vision", str(comp["image_encoder"])]
        if req.get("image_path"):
            argv += ["-i", req["image_path"]]
            if req.get("end_image_path"):
                argv += ["--end-img", req["end_image_path"]]
        if req.get("negative_prompt"):
            argv += ["-n", req["negative_prompt"]]
        argv += ["--sampling-method", req.get("sampler") or "euler"]
        rt_spec = model_catalog.get_runtime(req.get("runtime_key") or "sdcpp")
        flavor, flavor_reason = self._pick_flavor(req.get("runtime_flavor"), rt_spec)
        # Wan VAE is only reliable on CUDA/CPU per sd.cpp wan.md — on other
        # backends (Vulkan) decode must run on CPU or the clip corrupts.
        if flavor not in ("cuda", "cpu"):
            argv += ["--backend", "vae=cpu"]
        # Catalog-declared 8 GiB strategy: weights live in RAM and stream to
        # the GPU per layer (wan.md offload guidance for big models).
        if spec.capabilities.get("weight_offload_cpu"):
            argv += ["--offload-to-cpu"]
        gpu = req.get("assigned_gpu")
        if gpu in (None, "auto"):
            gpu = None if flavor == "cpu" else 0
        env = dict(os.environ)
        env.update(_device_env(flavor, gpu if isinstance(gpu, int) else None))

        yield {"progress": 0.02, "message": "starting video inference worker"}
        started = time.time()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env, cwd=str(work),
        )
        log_tail: list[str] = []
        cancelled = False
        try:
            while True:
                if handle.cancel_event is not None and handle.cancel_event.is_set():
                    cancelled = True
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                    break
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    log_tail.append(text)
                    del log_tail[:-20]
                m = _STEP_RE.search(text) or _ANY_FRAC_RE.search(text)
                if m:
                    cur, tot = int(m.group(1)), int(m.group(2))
                    if tot:
                        yield {"progress": 0.05 + 0.9 * (cur / tot),
                               "message": f"sampling {cur}/{tot}",
                               "detail": {"phase": "sampling", "step": cur, "steps_total": tot}}
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

        if cancelled:
            for f in work.glob(f"{handle.job_id}*"):
                f.unlink(missing_ok=True)
            raise JobCancelled()

        duration_ms = int((time.time() - started) * 1000)
        outputs = sorted(work.glob(f"{handle.job_id}*.webm"), key=lambda p: p.name)
        if proc.returncode != 0 or not outputs:
            for f in outputs:
                f.unlink(missing_ok=True)
            raise ValueError(_friendly_error("\n".join(log_tail), spec))

        # Validated-output commit: the clip is probed BEFORE it becomes an
        # asset. Unreadable output never registers; an unresolvable probe
        # (ffprobe absent) falls back to a container-signature check and is
        # recorded in provenance rather than silently trusted.
        probe: dict[str, Any] = {}
        for f in outputs:
            probe = media_probe.probe_video(f)
            if not probe.get("ok"):
                if probe.get("probe_unavailable"):
                    if not media_probe.container_signature_ok(f):
                        f.unlink(missing_ok=True)
                        raise ValueError("video output failed validation — unrecognized container")
                else:
                    f.unlink(missing_ok=True)
                    raise ValueError(f"video output failed validation — {probe.get('reason')}")

        assets = []
        for path in outputs:
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            assets.append({
                "path": str(path),
                "rel_path": str(path.relative_to(self._settings.data_dir))
                if str(path).startswith(str(self._settings.data_dir)) else None,
                "sha256": sha,
                "name": f"{spec.key}-{seed}.webm",
                "kind": "video",
            })

        media = {
            "source": "generated",
            "generated": True,
            "model_key": spec.key,
            "model_version": spec.version,
            "model_family": spec.family,
            "adapter_id": self.profile.adapter_id,
            "runtime": {"key": "sdcpp", "flavor": flavor, "selection_reason": flavor_reason},
            "prompt": req.get("prompt") or "",
            "negative_prompt": req.get("negative_prompt") or "",
            "width": width, "height": height,
            "frames": frames, "fps": fps,
            "steps": steps,
            "cfg": req.get("cfg", 7.0),
            "sampler": req.get("sampler") or "euler_a",
            "seed": seed,
            "gpu_index": req.get("assigned_gpu") if isinstance(req.get("assigned_gpu"), int) else None,
            "duration_ms": duration_ms,
            "generated_at": utcnow().isoformat(),
            "is_mock": False,
        }
        if probe:
            media["probe"] = {k: v for k, v in probe.items() if k != "reason"}
            if probe.get("probe_unavailable"):
                media["probe_unavailable"] = True
        yield {
            "done": True, "progress": 1.0,
            "output": {
                "path": assets[0]["path"],
                "sha256": assets[0]["sha256"],
                "name": assets[0]["name"],
                "kind": "video",
                "origin": "generation",
                "media": media,
                "extra_assets": assets[1:],
            },
        }


class ModelDownloadAdapter:
    """Install a model (+ its inference runtime when missing) as a durable
    app-level job. project_id is None — downloads are not project-scoped."""

    profile = CapabilityProfile(
        adapter_id="ops.model.download",
        display_name="Install model",
        adapter_kind="local",
        tasks=("model_download",),
        supports={"resume": True, "verify": True},
        limits={},
        requires_cloud=False,
        cost_model="free",
        lane="ops",
        needs_gpu=False,
        notes="User-approved model/runtime downloads. Resumable; verified before install.",
    )

    def __init__(self, session_factory, settings) -> None:
        self._sf = session_factory
        self._settings = settings

    async def validate(self, request: dict[str, Any]) -> list[str]:
        if request.get("runtime_only"):
            rt_spec = model_catalog.get_runtime(request.get("runtime_key") or "sdcpp")
            flavor = request.get("runtime_flavor") or "vulkan"
            if rt_spec is None:
                return ["unknown runtime_key"]
            if flavor not in rt_spec.backends:
                return [f"runtime '{rt_spec.key}' has no '{flavor}' backend"]
            return []
        spec = model_catalog.get(request.get("model_key") or "")
        if spec is None:
            return ["unknown model_key"]
        if not spec.license.get("commercial_ok", True):
            return [f"{spec.display_name} has a restricted license and cannot be installed"]
        if not spec.files:
            return [f"{spec.display_name} has no downloadable artifact in this build"]
        return []

    def _mark(self, model_key: str, **fields) -> None:
        from app.db.models import ModelInstall
        from sqlalchemy import select
        with self._sf() as s:
            row = s.execute(
                select(ModelInstall).where(ModelInstall.model_key == model_key)
                .order_by(ModelInstall.created_at.desc())
            ).scalars().first()
            if row is not None:
                for k, v in fields.items():
                    setattr(row, k, v)
                s.commit()

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        req = handle.request
        spec = None if req.get("runtime_only") else model_catalog.get(req["model_key"])

        with self._sf() as s:
            root = model_files.models_root(s, self._settings)
            install = model_files.install_row(s, spec, root) if spec else None
            s.commit()
            install_id = install.id if install else None
            disk_free = model_files.disk_free_bytes(root)
        need = (spec.requirements.get("min_disk_bytes") or spec.total_bytes) if spec else 0
        if spec and disk_free is not None and disk_free < need:
            self._mark(spec.key, status="failed",
                       error=f"insufficient disk: {disk_free / 1e9:.1f} GB free, need ~{need / 1e9:.1f} GB")
            raise ValueError(
                f"not enough disk space: {disk_free / 1e9:.1f} GB free on the models drive, "
                f"~{need / 1e9:.1f} GB needed")

        total = spec.files[0].size_bytes or 0 if spec else 0
        state = {"p": 0.0, "m": "preparing", "d": None}
        reporter = ProgressReporter(total or 1, lambda d: state.__setitem__("d", d))
        if spec:
            reporter.set_file(spec.files[0].name)

        def on_bytes(done: int, tot: int) -> None:
            if tot:
                reporter.total = tot
            reporter.done = done
            reporter.tick(0, force=True)
            state["p"] = 0.05 + 0.9 * (done / (tot or total or 1))
            state["m"] = f"downloading {done / 1e9:.2f} / {(tot or total or 0) / 1e9:.2f} GB"

        try:
            # Phase 1: runtime (small) when the model needs it and none is installed.
            rt_key = req.get("runtime_key") or (spec.runtime_key if spec else None)
            rt_spec = model_catalog.get_runtime(rt_key) if rt_key else None
            flavor = req.get("runtime_flavor") or "vulkan"
            if rt_spec is not None and not req.get("skip_runtime"):
                with self._sf() as s:
                    rt = model_files.runtime_row(s, f"{rt_key}-{flavor}")
                if rt is None or rt.status != "installed":
                    yield {"progress": 0.02, "message": f"installing runtime {rt_spec.display_name} ({flavor})"}
                    asset = req.get("release_asset") or model_files.resolve_release_asset(rt_spec, flavor)
                    with self._sf() as s:
                        model_files.install_runtime_zip(
                            s, self._settings, rt_spec, flavor, asset,
                            cancel_check=handle.check_cancel)
                        s.commit()
                else:
                    yield {"progress": 0.5, "message": f"runtime {rt_spec.display_name} ({flavor}) already installed"}

            if req.get("runtime_only"):
                yield {"done": True, "progress": 1.0,
                       "output": {"no_asset": True,
                                  "result": {"runtime_key": f"{rt_key}-{flavor}",
                                             "flavor": flavor}}}
                return

            # Phase 2: model weights — every declared component downloads +
            # verifies; progress weights bytes across the whole set.
            dest_dir = model_files.model_dir(root, spec)
            n_files = len(spec.files)
            total_bytes = 0
            for fi, mf in enumerate(spec.files):
                yield {"progress": 0.05 + 0.9 * (fi / n_files),
                       "message": f"downloading {spec.display_name} ({mf.role} {fi + 1}/{n_files})"}
                dest = dest_dir / mf.name
                url = req.get("source_url") or mf.url
                task = asyncio.create_task(asyncio.to_thread(
                    model_files.download_to, dest, url,
                    expected_size=mf.size_bytes,
                    progress=on_bytes, cancel_check=handle.check_cancel,
                ))
                while not task.done():
                    yield {"progress": state["p"], "message": state["m"], "detail": state["d"]}
                    await asyncio.sleep(0.3)
                res = await task
                total_bytes += res["bytes"]
                yield {"progress": 0.05 + 0.9 * ((fi + 1) / n_files),
                       "message": f"verifying {mf.name}",
                       "detail": state["d"] or reporter.snapshot()}
                ok, why = await asyncio.to_thread(model_files.verify_component, dest, mf, spec.fmt)
                if not ok:
                    self._mark(spec.key, status="failed", error=f"verification failed ({mf.role}): {why}")
                    raise ValueError(f"model failed integrity verification: {why}")
            self._mark(spec.key, status="installed", byte_size=total_bytes,
                       sha256=res["sha256"], verified=True, error=None)
        except JobCancelled:
            if spec:
                self._mark(spec.key, status="failed", error="cancelled by user (partial download kept for resume)")
            raise
        except Exception as exc:
            if spec:
                self._mark(spec.key, status="failed", error=str(exc))
            raise

        yield {
            "done": True, "progress": 1.0,
            "output": {"no_asset": True, "result": {
                "model_key": spec.key, "installed": True,
                "byte_size": res["bytes"], "install_id": install_id,
                "path": str(dest)}},
        }
