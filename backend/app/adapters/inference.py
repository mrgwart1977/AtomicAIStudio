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
import contextvars
import subprocess
import hashlib
import logging
import os
import random
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator

from sqlalchemy import select

from app.adapters.base import CapabilityProfile, JobCancelled, JobHandle
from app.db.models import ModelInstall, RuntimeInstall, utcnow
from app.domain import compatibility, download_security, hardware, model_catalog, model_files, proc_guard
from app.domain.media import engine as media_engine
from app.domain.media import policy as media_policy
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


def _execbind_marker(request: dict) -> dict | None:
    """Persisted execbind state discriminator (U-5d-0).

    Returns the execution_binding dict to honor, or None for a genuine
    legacy unpinned request. Every other combination refuses — a job
    that requires pinned execution never silently becomes unpinned, and
    a stray binding without its required marker is never silently
    treated as legacy.

      binding_required absent  + execution_binding absent  → legacy
      binding_required absent  + execution_binding present → refuse
      binding_required 'execbind:v1' + valid binding dict  → pinned
      any other marker version or malformed binding        → refuse

    Key presence is distinguished from a present-null value (F2): only
    the GENUINE absence of both fields engages the legacy path — a
    persisted 'binding_required': null or 'execution_binding': null is
    tampered state and refuses, never silently unpinned.
    """
    if "binding_required" not in request \
            and "execution_binding" not in request:
        return None
    required = request.get("binding_required")
    binding = request.get("execution_binding")
    if required != model_files.EXECBIND_VERSION:
        raise ValueError(
            "execution-binding marker is absent or unsupported — "
            "refusing rather than resolving unpinned")
    if binding is None:
        raise ValueError(
            "binding_required marker present but execution_binding is "
            "missing — refusing")
    if not isinstance(binding, dict):
        raise TypeError(
            "execution_binding is malformed — refusing")
    return binding


def _pipe_to_queue(pipe, queue: "asyncio.Queue", loop: asyncio.AbstractEventLoop) -> None:
    """Pump worker stdout lines into an asyncio queue from a daemon thread
    (synchronous Popen → async consumer bridge)."""
    try:
        for raw in iter(pipe.readline, b""):
            loop.call_soon_threadsafe(queue.put_nowait, raw)
    except Exception:
        pass
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, None)


def _check_worker_pressure(proc, started: float) -> str:
    """Safety watchdog: returns an abort reason when system pressure is
    critical or the worker exceeded its runtime cap; '' otherwise.
    High CPU alone never triggers — legitimate inference saturates cores."""
    from app.domain import resources  # local import — avoids adapter<->domain cycle
    try:
        state = resources.pressure_state()
        snap = resources.system_memory() if state == "critical" else {}
        cap = resources.safety_margins()["max_worker_seconds"]
    except Exception as exc:
        # fail closed: a broken watchdog must never silently disable safety
        return f"resource watchdog failed ({exc}) — aborting worker"
    if state == "critical":
        return (f"system memory pressure critical (avail RAM "
                f"{snap['avail_phys_bytes'] / resources.GIB:.1f} GiB, commit "
                f"headroom {snap['commit_headroom_bytes'] / resources.GIB:.1f} GiB)")
    if cap and time.time() - started > cap:
        return f"worker exceeded runtime cap ({cap}s)"
    return ""


async def _verify_worker_dead(proc) -> None:
    """Post-job cleanup verification: the process must actually be gone."""
    for _ in range(10):
        if proc.poll() is not None and not proc_guard.process_alive(proc.pid):
            return
        await asyncio.sleep(0.3)
    if proc_guard.process_alive(proc.pid):
        log.error("worker pid %d survived cleanup — forcing tree kill", proc.pid)
        proc_guard.terminate_tree(proc.pid, grace_s=2.0)


def _spawn_supervised_async(argv, *, env, cwd):
    """Spawn via proc_guard (suspended→assign→resume, fail-closed) and
    bridge stdout into an asyncio.Queue. Returns (proc, queue)."""
    import threading
    proc = proc_guard.spawn_supervised(list(argv), env=env, cwd=cwd)
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    threading.Thread(target=_pipe_to_queue, args=(proc.stdout, queue, loop),
                     daemon=True).start()
    return proc, queue


def _result_apply_command(req: dict) -> dict | None:
    """G2-A — the audited result-landing command a completed generation
    emits, same contract the mock adapter uses: a pinned keyframe.result
    takes precedence over a plain shot thumbnail update. ``__asset__``
    and ``__job__`` are placeholders the job engine substitutes."""
    if req.get("keyframe_id"):
        return {"command_type": "keyframe.result",
                "payload": {
                    "keyframe_id": req["keyframe_id"],
                    "shot_id": req.get("shot_id"),
                    "result_asset_id": "__asset__",
                    "job_id": "__job__",
                    "expected_keyframe_revision":
                        req.get("expected_result_revision"),
                    "expected_planning_sha":
                        req.get("expected_planning_sha")}}
    if req.get("shot_id") and req.get("set_thumbnail"):
        return {"command_type": "shot.update",
                "payload": {"shot_id": req["shot_id"],
                            "thumbnail_asset_id": "__asset__"}}
    return None


# E3 — pinned verification evidence is request-local, never adapter
# state. Adapter instances are registry singletons shared across jobs
# and the engine dispatches each job as its own asyncio task, so a
# ContextVar gives every invocation its own evidence slot: concurrent
# invocations can neither read nor overwrite each other's verified
# paths, and a fresh task starts empty (no inherited pinned state, no
# persistence across unrelated executions). See SdCppAdapter._pinned.
_PINNED_EVIDENCE: contextvars.ContextVar = contextvars.ContextVar(
    "execbind_pinned_evidence", default=None)


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

    @property
    def _pinned(self):
        """Pinned verification evidence for THIS invocation (E3).

        Adapter instances are registry singletons shared across jobs,
        and the engine dispatches jobs as concurrent asyncio tasks —
        plain instance state could be overwritten by another invocation
        between _resolve and the pre-spawn gate (a skipped or foreign
        revalidation). A ContextVar gives every task its own slot:
        concurrent invocations can neither read nor overwrite each
        other's evidence, a fresh task starts empty (no inherited
        pinned state, no persistence across executions), and an
        unpinned request can never reuse another request's evidence.
        """
        return _PINNED_EVIDENCE.get()

    @_pinned.setter
    def _pinned(self, value) -> None:
        _PINNED_EVIDENCE.set(value)

    def __init__(self, session_factory, settings) -> None:
        self._sf = session_factory
        self._settings = settings
        self._pinned = None  # PinnedPaths when the last _resolve was pinned

    # ------------------------------------------------------------ helpers

    def _resolve(self, request: dict, *, cancel_check=None
                 ) -> tuple[ModelSpec, Path, Path | None]:
        key = request.get("model_key")
        spec = model_catalog.get(key or "")
        if spec is None:
            raise ValueError(f"unknown model '{key}'")
        if spec.modality != "image" or "text2image" not in spec.modes or spec.support not in ("production", "tested"):
            raise ValueError(
                f"model '{spec.display_name}' ({spec.modality}, {spec.support}) is not usable for image generation")
        binding = _execbind_marker(request)
        self._pinned = None
        with self._sf() as s:
            if binding is not None:
                # E1 — pinned execution is a subprocess-adapter
                # contract. An unsupported (persistent) adapter refuses
                # HERE: _resolve runs before _server_exe, pool acquire,
                # model load/reuse and the HTTP submit on the persistent
                # run() path, so this precedes every server interaction.
                if self.profile.adapter_id not in \
                        model_files.EXECBIND_SUPPORTED_ADAPTERS:
                    raise ValueError(
                        "execbind:persistent_runtime_unsupported — "
                        "pinned execution is supported on standard "
                        "subprocess adapters only; a persistent runtime "
                        "cannot prove loaded-model identity at its "
                        "execution boundary")
                # U-5d-0 — pinned path: exact approved installation +
                # every required member hash-verified BEFORE spawn. The
                # unordered installed_file() lookup is never used.
                try:
                    paths = model_files.resolve_pinned_paths(
                        s, self._settings, spec, binding,
                        adapter_id=self.profile.adapter_id,
                        roles=model_files.execbind_roles(
                            spec, adapter_id=self.profile.adapter_id,
                            has_image_input=False),
                        cancel_check=cancel_check)
                except model_files.ExecbindError as exc:
                    raise ValueError(
                        f"execbind:{exc.code}: {exc}") from exc
                self._pinned = paths
                weight = paths["diffusion"]
            else:
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

    # G2-B — adapter-owned constant (never a request field). at_runtime
    # is the mode designed for prompt-tag-driven adapter selection; a
    # change needs verified evidence, not a config toggle.
    LORA_APPLY_MODE = "at_runtime"

    def _stage_loras(self, req: dict, staged_dir: Path) -> str:
        """G2-B/B-1 — verify the pinned loras selections and snapshot
        each adapter file into the job-private staging dir.

        A staged file is an independent byte COPY: source mutation or
        path replacement after staging cannot reach the runtime. Rows
        are re-locked to the queued pins (install_row_id + sha256) so a
        job can never silently shift onto different weights. Returns
        the runtime prompt (composed prompt + approved tags)."""
        from app.domain import lora_tags
        if staged_dir.exists():
            model_files.force_rmtree(staged_dir)  # attempt re-run
        staged_dir.mkdir(parents=True)
        try:
            with self._sf() as s:
                for entry in req["loras"]:
                    self._stage_one_lora(s, entry, staged_dir)
        except Exception:
            # Never hand sd.cpp a partially staged directory.
            model_files.force_rmtree(staged_dir)
            raise
        prompt = lora_tags.apply_lora_tags(req["prompt"], req["loras"])
        pinned = req.get("runtime_prompt_sha256")
        if pinned and hashlib.sha256(
                prompt.encode("utf-8")).hexdigest() != pinned:
            model_files.force_rmtree(staged_dir)
            raise ValueError(
                "runtime prompt drifted since the job was queued — "
                "refusing")
        return prompt

    def _stage_one_lora(self, s, entry: dict, staged_dir: Path) -> None:
        """Stage one pinned lora: re-lock the install row to the queued
        pin, re-verify containment/structure/sha, copy, then RE-HASH the
        staged bytes and mark them read-only. The staged file the
        runtime reads must equal the queue-pinned sha256 — verified on
        the copy, not assumed from the source."""
        from app.domain import lora_tags
        staged_name = lora_tags.validate_staged_name(
            entry.get("staged_name"))
        row = s.get(ModelInstall, entry.get("install_row_id"))
        if row is None or row.status != "installed" \
                or row.model_key != entry["key"]:
            raise ValueError(
                f"LoRA '{entry['key']}' was removed or changed "
                "since the job was queued")
        root = Path(row.install_root).resolve()
        raw = Path(row.install_root) / row.rel_dir / row.file_name
        src = raw.resolve()
        if not src.is_relative_to(root) or not src.is_file():
            raise ValueError(
                f"LoRA '{entry['key']}' resolves outside its "
                "install root — refused")
        if raw.is_symlink() or (
                hasattr(raw, "is_junction")
                and raw.is_junction()):
            raise ValueError(
                f"LoRA '{entry['key']}' is a link/reparse point "
                "— refused")
        sha = hashlib.sha256()
        with open(src, "rb") as fh:
            for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
                sha.update(chunk)
        if sha.hexdigest() != entry["sha256"]:
            raise ValueError(
                f"LoRA '{entry['key']}' content changed since "
                "the job was queued — refusing")
        lspec = model_catalog.get(entry["key"])
        ok, why = (model_files.verify_lora_file(src, lspec)
                   if lspec else (False, "spec removed"))
        if not ok:
            raise ValueError(
                f"LoRA '{entry['key']}' failed structural "
                f"verification: {why}")
        staged = staged_dir / staged_name
        shutil.copyfile(src, staged)
        # Post-copy verification — closes the verify→copy window: the
        # staged bytes are what sd.cpp actually reads and they must equal
        # the queue-pinned sha256 exactly.
        digest = hashlib.sha256()
        with open(staged, "rb") as fh:
            for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != entry["sha256"]:
            raise ValueError(
                f"staged LoRA '{entry['key']}' does not match its "
                "pinned sha256 — refusing")
        # Read-only: no application path may rewrite a staged artifact
        # before launch. (This blocks app-level writes; a hostile
        # process with filesystem write access can clear the bit —
        # that boundary is documented, not claimed.)
        os.chmod(staged, 0o444)

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
        """Lifecycle wrapper — owns the per-job LoRA staging dir so every
        exit path (success, failure, cancel, abandoned generator) leaves
        nothing behind."""
        req = handle.request
        staged_dir = (Path(handle.work_dir) / f"{handle.job_id}-loras"
                      if req.get("loras") else None)
        try:
            async for u in self._run(handle, staged_dir):
                yield u
        finally:
            if staged_dir is not None:
                model_files.force_rmtree(staged_dir)

    async def _run(self, handle: JobHandle,
                   staged_dir: Path | None) -> AsyncIterator[dict[str, Any]]:
        spec, weight, _ = self._resolve(
            handle.request, cancel_check=handle.check_cancel)
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

        prompt = req["prompt"]
        if req.get("loras"):
            # Last-line injection defense over the runtime-bound text —
            # the compose layer already scanned; anything that reached a
            # request without compose gets stopped here.
            from app.domain import lora_tags
            lora_tags.reject_lora_syntax(prompt, field="prompt")
            lora_tags.reject_lora_syntax(
                req.get("negative_prompt"), field="negative_prompt")
            prompt = self._stage_loras(req, staged_dir)

        argv = self._argv_prefix(req) + [
            "-M", "img_gen",  # valid modes: img_gen, adetailer, vid_gen, convert, upscale, metadata, vid_upscale
            "-m", str(weight),
            "-p", prompt,
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
        if req.get("loras"):
            argv += ["--lora-model-dir", str(staged_dir),
                     "--lora-apply-mode", self.LORA_APPLY_MODE]
        rt_spec = model_catalog.get_runtime(req.get("runtime_key") or "sdcpp")
        flavor, flavor_reason = self._pick_flavor(req.get("runtime_flavor"), rt_spec)
        gpu = req.get("assigned_gpu")
        if gpu in (None, "auto"):
            gpu = None if flavor == "cpu" else 0
        env = dict(os.environ)
        env.update(_device_env(flavor, gpu if isinstance(gpu, int) else None))

        if self._pinned is not None:
            # U-5d-0 F1 — final all-member drift revalidation AFTER all
            # preparation (LoRA staging, argv, flavor, env), immediately
            # before the external inference spawn. No second hash pass;
            # any detected replacement/disappearance/modification is a
            # controlled refusal, never a fallback or substitution.
            try:
                self._pinned.revalidate(
                    cancel_check=handle.check_cancel)
            except model_files.ExecbindError as exc:
                raise ValueError(
                    f"execbind:{exc.code}: {exc}") from exc
        yield {"progress": 0.02, "message": "starting inference worker"}
        started = time.time()
        proc, lines = _spawn_supervised_async(argv, env=env, cwd=str(work))
        log_tail: list[str] = []
        cancelled = False
        pressure_abort = ""
        last_mem_check = 0.0
        try:
            while True:
                if handle.cancel_event is not None and handle.cancel_event.is_set():
                    cancelled = True
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                    break
                now = time.time()
                if now - last_mem_check >= 5.0:
                    last_mem_check = now
                    pressure_abort = _check_worker_pressure(proc, started)
                    if pressure_abort:
                        log.error("worker abort: %s", pressure_abort)
                        proc_guard.terminate_tree(proc.pid)
                        break
                try:
                    line = await asyncio.wait_for(lines.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                if line is None:
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
                await asyncio.get_running_loop().run_in_executor(None, proc.wait, 5)
            except subprocess.TimeoutExpired:
                proc.kill()
                await asyncio.get_running_loop().run_in_executor(None, proc.wait)
        finally:
            if proc.poll() is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await _verify_worker_dead(proc)
            proc_guard.unsupervise(proc.pid)

        if pressure_abort:
            raise ValueError(f"worker aborted for system safety: {pressure_abort}")

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
        _apply = _result_apply_command(req)
        if _apply:
            out["output"]["apply_command"] = _apply
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

    def _resolve(self, request: dict, *, cancel_check=None
                 ) -> tuple[ModelSpec, Path, Path | None]:
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
        binding = _execbind_marker(request)
        self._pinned = None
        with self._sf() as s:
            if binding is not None:
                # E1 — same contract as the image adapter: pinned
                # execution is supported on standard subprocess
                # adapters only; a persistent runtime refuses before
                # any server/pool interaction.
                if self.profile.adapter_id not in \
                        model_files.EXECBIND_SUPPORTED_ADAPTERS:
                    raise ValueError(
                        "execbind:persistent_runtime_unsupported — "
                        "pinned execution is supported on standard "
                        "subprocess adapters only; a persistent runtime "
                        "cannot prove loaded-model identity at its "
                        "execution boundary")
                # U-5d-0 — pinned path: exact approved installation and
                # EVERY required member (weights + vae + text_encoder +
                # clip_vision when declared and i2v) hash-verified
                # BEFORE spawn. installed_file/installed_component are
                # never used on this path.
                try:
                    paths = model_files.resolve_pinned_paths(
                        s, self._settings, spec, binding,
                        adapter_id=self.profile.adapter_id,
                        roles=model_files.execbind_roles(
                            spec, adapter_id=self.profile.adapter_id,
                            has_image_input=bool(
                                request.get("image_path"))),
                        cancel_check=cancel_check)
                except model_files.ExecbindError as exc:
                    raise ValueError(
                        f"execbind:{exc.code}: {exc}") from exc
                self._pinned = paths
                weight = paths["diffusion"]
                self._components = {
                    "vae": paths["vae"],
                    "text_encoder": paths["text_encoder"],
                    "image_encoder": paths.get("clip_vision")}
            else:
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
        spec, weight, _ = self._resolve(
            handle.request, cancel_check=handle.check_cancel)
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
        backend_mods: list[str] = []
        if flavor not in ("cuda", "cpu"):
            backend_mods.append("vae=cpu")
        # Residency mode (issue #28 Option B): per-module placement.
        #   cpu_offload — weights in RAM, stream to GPU (Wan catalog default)
        #   gpu         — everything device-resident (B1; needs big VRAM)
        #   te_cpu      — text encoder on host, diffusion+VAE on GPU (B2)
        #   disk        — params streamed from disk + --max-vram budget (B3)
        mode = req.get("residency_mode") or (
            "cpu_offload" if spec.capabilities.get("weight_offload_cpu") else "gpu")
        if mode == "cpu_offload":
            argv += ["--offload-to-cpu"]
        elif mode == "te_cpu":
            backend_mods.append("te=cpu")
        elif mode == "disk":
            argv += ["--params-backend", "disk", "--mmap"]
            budget = req.get("max_vram_gib") or 6
            argv += ["--max-vram", str(budget)]
        if backend_mods:
            argv += ["--backend", ",".join(backend_mods)]
        gpu = req.get("assigned_gpu")
        if gpu in (None, "auto"):
            gpu = None if flavor == "cpu" else 0
        env = dict(os.environ)
        env.update(_device_env(flavor, gpu if isinstance(gpu, int) else None))

        if self._pinned is not None:
            # U-5d-0 F1 — final all-member drift revalidation AFTER all
            # preparation, immediately before the external inference
            # spawn. No second hash pass.
            try:
                self._pinned.revalidate(
                    cancel_check=handle.check_cancel)
            except model_files.ExecbindError as exc:
                raise ValueError(
                    f"execbind:{exc.code}: {exc}") from exc
        yield {"progress": 0.02, "message": "starting video inference worker"}
        started = time.time()
        proc, lines = _spawn_supervised_async(argv, env=env, cwd=str(work))
        log_tail: list[str] = []
        cancelled = False
        pressure_abort = ""
        last_mem_check = 0.0
        try:
            while True:
                if handle.cancel_event is not None and handle.cancel_event.is_set():
                    cancelled = True
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                    break
                now = time.time()
                if now - last_mem_check >= 5.0:
                    last_mem_check = now
                    pressure_abort = _check_worker_pressure(proc, started)
                    if pressure_abort:
                        log.error("worker abort: %s", pressure_abort)
                        proc_guard.terminate_tree(proc.pid)
                        break
                try:
                    line = await asyncio.wait_for(lines.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                if line is None:
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
                await asyncio.get_running_loop().run_in_executor(None, proc.wait, 5)
            except subprocess.TimeoutExpired:
                proc.kill()
                await asyncio.get_running_loop().run_in_executor(None, proc.wait)
        finally:
            if proc.poll() is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            # post-job verification: process must be dead before we report
            await _verify_worker_dead(proc)
            proc_guard.unsupervise(proc.pid)

        if pressure_abort:
            raise ValueError(f"worker aborted for system safety: {pressure_abort}")

        if cancelled:
            for f in work.glob(f"{handle.job_id}*"):
                f.unlink(missing_ok=True)
            raise JobCancelled()

        duration_ms = int((time.time() - started) * 1000)
        outputs = sorted(work.glob(f"{handle.job_id}*.webm"), key=lambda p: p.name)
        if proc.returncode != 0 or not outputs:
            for f in outputs:
                f.unlink(missing_ok=True)
            raise ValueError(f"[{flavor}/{mode}] "
                             + _friendly_error("\n".join(log_tail), spec))

        # Validated-output commit: the clip must pass full media-engine
        # validation BEFORE it becomes an asset — ffprobe metadata +
        # complete ffmpeg decode + exact decoded-frame count. A container
        # signature alone can never mark output fully validated; degraded
        # modes (no binaries — dev only) are flagged in provenance. Frozen
        # builds REQUIRE full validation: a missing bundled decoder is a
        # packaging bug, not a reason to register unchecked media.
        vspec = media_policy.ValidationSpec(
            width=width, height=height, fps=float(fps) if fps else None,
            frames=int(frames) if frames else None,
            duration_s=(frames / fps) if (frames and fps) else None)
        validation: dict[str, Any] = {}
        probe: dict[str, Any] = {}
        for f in outputs:
            validation = media_engine.validate(
                f, vspec, cancel=handle.cancel_event)
            probe = validation.get("probe") or {}
            if validation.get("cancelled"):
                f.unlink(missing_ok=True)
                raise JobCancelled()
            if not validation.get("ok"):
                f.unlink(missing_ok=True)
                raise ValueError(f"video output failed validation — {validation.get('reason')}")
            if getattr(sys, "frozen", False) and not validation.get("fully_validated"):
                f.unlink(missing_ok=True)
                raise ValueError("video output could not be fully validated "
                                 f"(mode: {validation.get('validation_mode')})")

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
        if validation:
            media["validation"] = {
                "mode": validation.get("validation_mode"),
                "fully_validated": validation.get("fully_validated"),
                "checks": validation.get("checks"),
                "decode": {k: v for k, v in (validation.get("decode") or {}).items()
                           if k in ("ran", "ok", "exit", "elapsed_ms")},
                "frame_check": validation.get("frame_check"),
                "toolchain": {k: {kk: vv for kk, vv in v.items()
                                  if kk in ("available", "source", "version")}
                              for k, v in (validation.get("toolchain") or {}).items()},
            }
            if validation.get("probe_unavailable"):
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


class ModelBenchmarkAdapter:
    """C5-E — explicit, user-initiated local benchmark for an INSTALLED
    model. Runs N minimal generations through the same supervised sd.cpp
    spawn path as real inference and measures wall time + worker RSS.

    Hard guarantees: no downloads, no cloud, installed models only, and
    cancellation kills the worker tree via the job cancel_event — the same
    machinery generation jobs use. Results land durably on the job row
    (result_json) and on the model's install row (meta_json.benchmark).
    Peak RSS is the host working-set of the worker process — VRAM peaks
    are not portably measurable, so results always carry the
    estimated-vs-measured warning."""

    profile = CapabilityProfile(
        adapter_id="ops.model.benchmark",
        display_name="Model benchmark",
        adapter_kind="local",
        tasks=("model_benchmark",),
        supports={"cancel": True},
        limits={"max_runs": 5},
        requires_cloud=False,
        cost_model="free",
        # Gen lane, needs_gpu: benchmarks acquire a GPU scheduler slot and
        # pass the same system-RAM admission as generation jobs — the
        # fairness policy is "same queue, same slots": a benchmark never
        # bypasses the resource gates a real generation obeys, and a busy
        # GPU leaves the benchmark queued, not the other way round.
        lane="gen",
        needs_gpu=True,
        notes="Explicit opt-in benchmark of an installed model — never runs "
              "at startup, never downloads, never calls a cloud provider. "
              "Shares the gen-lane GPU scheduler and memory admission with "
              "generation jobs (documented fairness policy).",
    )

    def __init__(self, session_factory, settings) -> None:
        self._sf = session_factory
        self._settings = settings
        self._gen = SdCppAdapter(session_factory, settings)

    async def validate(self, request: dict[str, Any]) -> list[str]:
        errors = []
        spec = model_catalog.get(request.get("model_key") or "")
        if spec is None:
            return ["model_key is required"]
        if spec.adapter_id != "gen.image.sdcpp":
            errors.append(
                f"benchmark harness not available for '{spec.adapter_id}' models yet")
        if spec.support == "planned":
            errors.append("planned models cannot be benchmarked")
        runs = int(request.get("runs", 3) or 3)
        if not 1 <= runs <= 5:
            errors.append("runs must be between 1 and 5")
        try:
            self._gen._resolve({"model_key": spec.key,
                                **{k: v for k, v in request.items()
                                   if k in ("runtime_key", "runtime_flavor")}})
        except Exception as exc:
            errors.append(str(exc))
        return errors

    async def _run_once(self, handle: JobHandle, spec: ModelSpec,
                        weight: Path, argv_prefix: list[str],
                        req: dict, run_ix: int, work: Path,
                        side: int, steps: int):
        """One measured spawn — returns (duration_ms, peak_rss_bytes|None)."""
        out_png = work / f"bench_{handle.job_id}_{run_ix}.png"
        argv = argv_prefix + [
            "-M", "img_gen", "-m", str(weight),
            "-p", "benchmark calibration frame",
            "-o", str(out_png),
            "-W", str(side), "-H", str(side),
            "--steps", str(steps),
            "--cfg-scale", "1.0",
            "--seed", "1",
            "--batch-count", "1",
            "-v",
        ]
        rt_spec = model_catalog.get_runtime(req.get("runtime_key") or "sdcpp")
        flavor, _reason = self._gen._pick_flavor(req.get("runtime_flavor"), rt_spec)
        env = dict(os.environ)
        env.update(_device_env(flavor, req.get("assigned_gpu")
                               if isinstance(req.get("assigned_gpu"), int) else None))
        if self._gen._pinned is not None:
            # U-5d-0 F1 — same final drift gate as generation paths:
            # every pinned member revalidated before this spawn.
            try:
                self._gen._pinned.revalidate(
                    cancel_check=handle.check_cancel)
            except model_files.ExecbindError as exc:
                raise ValueError(
                    f"execbind:{exc.code}: {exc}") from exc
        proc, lines = _spawn_supervised_async(argv, env=env, cwd=str(work))
        peak_rss: int | None = None
        cancelled = False
        started = time.time()
        last_mem_check = 0.0
        try:
            while True:
                if handle.cancel_event is not None and handle.cancel_event.is_set():
                    cancelled = True
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                    break
                rss = proc_guard.process_rss_bytes(proc.pid)
                if rss is not None:
                    peak_rss = max(peak_rss or 0, rss)
                now = time.time()
                if now - last_mem_check >= 5.0:
                    last_mem_check = now
                    if _check_worker_pressure(proc, started):
                        proc_guard.terminate_tree(proc.pid)
                        raise ValueError("benchmark aborted for system safety")
                try:
                    line = await asyncio.wait_for(lines.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                if line is None:
                    break
            try:
                await asyncio.get_running_loop().run_in_executor(None, proc.wait, 5)
            except subprocess.TimeoutExpired:
                proc.kill()
                await asyncio.get_running_loop().run_in_executor(None, proc.wait)
        finally:
            if proc.poll() is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await _verify_worker_dead(proc)
            proc_guard.unsupervise(proc.pid)
            out_png.unlink(missing_ok=True)
        if cancelled:
            raise JobCancelled()
        duration_ms = int((time.time() - started) * 1000)
        if proc.returncode != 0:
            raise ValueError(f"benchmark run {run_ix + 1} failed "
                             f"(worker exit {proc.returncode})")
        return duration_ms, peak_rss

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        req = handle.request
        spec, weight, _exe = self._gen._resolve(
            req, cancel_check=handle.check_cancel)
        argv_prefix = self._gen._argv_prefix(req)
        runs = max(1, min(5, int(req.get("runs", 3) or 3)))
        work = Path(handle.work_dir)
        work.mkdir(parents=True, exist_ok=True)

        caps = spec.capabilities or {}
        sizes = caps.get("sizes") or {}
        side = int(sizes.get("min") or 512)
        steps = int((caps.get("steps") or {}).get("min") or 4)

        rt_spec = model_catalog.get_runtime(req.get("runtime_key") or "sdcpp")
        flavor, flavor_reason = self._gen._pick_flavor(
            req.get("runtime_flavor"), rt_spec)
        gpu_ix = req.get("assigned_gpu") if isinstance(req.get("assigned_gpu"), int) else None
        hw = hardware.probe()
        gpus = hw.get("gpus") or []
        gpu_row = next((g for g in gpus
                        if isinstance(g.get("index"), int)
                        and g["index"] == (gpu_ix if gpu_ix and gpu_ix >= 0 else 0)),
                       None) if gpus else None
        gpu_name = (gpu_row or {}).get("name") if gpu_ix is None or gpu_ix >= 0 else None

        # Reproducibility identity: which weight file, which runtime build,
        # which device/driver. File hashing stays with the install-time
        # checksum — re-hashing multi-GB weights on every benchmark would
        # dominate the measurement; the catalog-declared sha256 (and the
        # install row's verified value) is the honest identity.
        with self._sf() as s:
            inst = s.execute(
                select(ModelInstall).where(
                    ModelInstall.model_key == spec.key,
                    ModelInstall.status == "installed")
            ).scalars().first()
        file_sha = (inst.sha256 if inst and inst.sha256
                    else (spec.files[0].sha256 or None))
        rt_row_key = f"sdcpp-{flavor}"
        with self._sf() as s:
            rt_row = s.execute(
                select(RuntimeInstall).where(
                    RuntimeInstall.runtime_key == rt_row_key)
            ).scalar_one_or_none()

        results: list[dict[str, Any]] = []
        for i in range(runs):
            yield {"progress": 0.05 + 0.9 * (i / runs),
                   "message": f"benchmark run {i + 1}/{runs}"}
            dur, rss = await self._run_once(
                handle, spec, weight, argv_prefix, req, i, work,
                side, steps)
            results.append({"run": i + 1,
                            # run 1 carries cold weight-load cost; later
                            # runs may benefit from OS/runtime caches
                            "condition": "cold" if i == 0 else "warm",
                            "duration_ms": dur,
                            "peak_worker_rss_bytes": rss})

        durations = sorted(r["duration_ms"] for r in results)
        median = durations[len(durations) // 2]
        peaks = [r["peak_worker_rss_bytes"] for r in results
                 if r["peak_worker_rss_bytes"] is not None]
        est = compatibility.estimate_vram(spec)
        result = {
            "model_key": spec.key,
            "model_version": spec.version,
            "model_file_sha256": file_sha,
            "adapter_id": self.profile.adapter_id,
            "runtime": {"key": "sdcpp", "flavor": flavor,
                        "version": rt_row.version if rt_row else None,
                        "selection_reason": flavor_reason},
            "gpu": gpu_name, "gpu_index": gpu_ix,
            "driver_version": (gpu_row or {}).get("driver_version"),
            "runs": results,
            "median_duration_ms": median,
            "peak_worker_rss_bytes": max(peaks) if peaks else None,
            "peak_memory_note": "worker-process host RAM peak (Windows "
                "working set); VRAM peaks are not portably measurable — "
                "use the VRAM estimate as guidance, not a measurement",
            "params": {"width": side, "height": side, "steps": steps,
                       "batch": 1, "seed": 1, "cfg_scale": 1.0},
            "estimate_vs_measured": {
                "estimated_vram_bytes": est,
                "warning": "Catalog figures are ESTIMATES from model "
                           "anchors; measured numbers here reflect this "
                           "hardware only and will vary run to run.",
            },
            "measurement_limitations": [
                "host RSS peak excludes GPU VRAM entirely",
                "minimal settings (model min size/steps) — not representative "
                "of production workloads",
                "single-process timing; OS cache state differs between "
                "cold (first) and warm runs",
                "results are valid only for this GPU/driver/runtime build — "
                "do not extrapolate to other hardware",
            ],
            "measured_at": utcnow().isoformat(),
        }
        # durable stamp on the install row — survives restarts, visible in
        # /api/compatibility without re-running anything
        with self._sf() as s:
            row = s.execute(
                select(ModelInstall).where(
                    ModelInstall.model_key == spec.key,
                    ModelInstall.status == "installed")
            ).scalars().first()
            if row is not None:
                meta = dict(row.meta_json or {})
                meta["benchmark"] = result
                row.meta_json = meta
                s.commit()
        yield {"done": True, "progress": 1.0,
               "output": {"no_asset": True, "result": result}}


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
            # #175: catalog-declared URLs are the ONLY download sources —
            # legacy job requests carrying 'source_url' are ignored, never
            # used (the override field was unreachable and is removed).
            if req.get("source_url"):
                log.warning("ops.model.download: ignoring unexpected "
                            "'source_url' in job request")
            allowed_hosts = download_security.allowed_hosts_for(
                (spec.source or {}).get("provider"))
            for fi, mf in enumerate(spec.files):
                yield {"progress": 0.05 + 0.9 * (fi / n_files),
                       "message": f"downloading {spec.display_name} ({mf.role} {fi + 1}/{n_files})"}
                download_security.ensure_component_name(mf.name)
                dest = dest_dir / mf.name
                download_security.ensure_contained(root, dest)
                task = asyncio.create_task(asyncio.to_thread(
                    model_files.download_to, dest, mf.url,
                    expected_size=mf.size_bytes,
                    allowed_hosts=allowed_hosts,
                    progress=on_bytes, cancel_check=handle.check_cancel,
                    # verify-before-publish: the .part is checked against
                    # the declared sha256/format BEFORE it is promoted —
                    # invalid bytes never occupy the canonical path.
                    verify=lambda p, _mf=mf: model_files.verify_component(
                        p, _mf, spec.fmt),
                ))
                while not task.done():
                    yield {"progress": state["p"], "message": state["m"], "detail": state["d"]}
                    await asyncio.sleep(0.3)
                res = await task
                total_bytes += res["bytes"]
                yield {"progress": 0.05 + 0.9 * ((fi + 1) / n_files),
                       "message": f"verified {mf.name}",
                       "detail": state["d"] or reporter.snapshot()}
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
