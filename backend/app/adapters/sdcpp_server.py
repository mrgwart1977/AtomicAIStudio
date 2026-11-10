"""Persistent sd.cpp inference via the sd-server HTTP worker (M4.1 Phase 2A).

One supervised ``sd-server.exe`` process per WorkerKey (model, runtime,
flavor, GPU, protocol="http") stays resident in the WorkerPool and serves
many jobs — eliminating per-job model reload once real inference is
validated.

Phase 2A status: lifecycle/protocol/resource plumbing only. Generation
requests are stub-tested; real weight inference is Phase 2B and is NOT
enabled here — the adapter registers only when
``ATOMIC_PERSISTENT_INFERENCE_ENABLED=1``.

Verified launch surface (pinned master-1073):
    sd-server.exe -l 127.0.0.1 --listen-port <p> --model-dir <dir>
    GET /v1/models → 200 even with no weights (lazy --model-dir loading)

Cancel/recycle contract: sd-server exposes no mid-request cancel — an
abandoned or transport-failed request leaves server-side execution
state UNKNOWN, so the worker is always killed and its reservations
released rather than returned to the idle pool.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from app.adapters.base import CapabilityProfile, JobCancelled, JobHandle
from app.adapters.inference import SdCppAdapter, _device_env
from app.domain import model_catalog, model_files
from app.domain.workers.pool import WorkerPool, WorkerUnavailableError
from app.domain.workers.ports import free_tcp_port
from app.domain.workers.protocol import SdServerHttpProtocol
from app.domain.workers.supervisor import WorkerDeadError
from app.domain.workers.types import WorkerKey, WorkerSpec

GiB = 1024 ** 3
SERVER_EXE = "sd-server.exe"
# Conservative uncalibrated residency costs (Phase 2A) — activation/ctx
# buffers on top of weights. Real numbers must be measured in Phase 2B.
_WORKER_VRAM_OVERHEAD = 2 * GiB
_WORKER_RAM_FLOOR = 4 * GiB
_MAX_PORT_ATTEMPTS = 3


class SdCppServerAdapter(SdCppAdapter):
    """Real persistent worker: pool-managed sd-server HTTP process.

    Falls back to nothing — when the pool is unavailable or admission is
    refused the job fails honestly (unlike the mock, there is no silent
    subprocess-per-job path; callers choose the SdCppAdapter adapter_id
    for that execution shape)."""

    profile = CapabilityProfile(
        adapter_id="gen.image.sdcpp-server",
        display_name="Local image generation — persistent server (stable-diffusion.cpp)",
        adapter_kind="local",
        tasks=("text2image",),
        supports={
            "negative_prompt": True, "seed": True, "size": True,
            "steps": True, "cfg": True, "sampler": True, "batch": False,
            "gpu_selection": True, "cpu_fallback": True, "ref_images": False,
            "lora": False,
        },
        limits={"max_batch": 1},
        requires_cloud=False,
        cost_model="free",
        lane="gen",
        needs_gpu=True,
        worker_model="persistent-pool",
        resumable=False,
        notes="EXPERIMENTAL persistent sd-server worker (Phase 2A "
              "infrastructure — not validated for production). Request "
              "payload mapping is an unverified OpenAI/A1111 superset "
              "(Phase 2B must measure the real field set). Mid-request "
              "cancel recycles the worker — it cannot stop server-side "
              "inference on this runtime build.",
    )

    def __init__(self, session_factory, settings,
                 worker_pool: WorkerPool | None = None, *,
                 argv_prefix: list[str] | None = None,
                 port_allocator: Callable[[], int] | None = None) -> None:
        super().__init__(session_factory, settings)
        self._pool = worker_pool
        # Test hooks: argv_prefix replaces the real binary (a stub server
        # script); port_allocator controls port choice (collision tests).
        self._argv_prefix = argv_prefix
        self._alloc_port = port_allocator or free_tcp_port

    # ---------- worker spec ----------

    def _server_exe(self, request: dict) -> tuple[Path | None, str]:
        rt_spec = model_catalog.get_runtime(request.get("runtime_key") or "sdcpp")
        flavor, reason = self._pick_flavor(request.get("runtime_flavor"), rt_spec)
        self._flavor_reason = reason
        if rt_spec is None:
            return None, flavor
        exe = model_files.runtime_dir(self._settings, rt_spec, flavor) / SERVER_EXE
        return (exe if exe.exists() else None), flavor

    def server_argv(self, exe: Path | None, port: int,
                    model_dir: Path) -> list[str]:
        """Launch vector for the real server (or a stub via argv_prefix).
        Loopback only — the server never binds a routable address."""
        prefix = self._argv_prefix or [str(exe)]
        return [*prefix,
                "-l", "127.0.0.1",
                "--listen-port", str(port),
                "--model-dir", str(model_dir),
                "--log-level", "warn"]

    def _spec(self, request: dict, job_id: str, *, port: int,
              model_key: str, weight: Path | None,
              exe: Path | None, flavor: str) -> WorkerSpec:
        gpu = int(request.get("assigned_gpu", -1))
        env = dict(os.environ)
        env.update(_device_env(flavor, gpu if gpu >= 0 else None))
        # Uncalibrated residency estimates — weight bytes + fixed overhead,
        # never zero on the GPU lane (fail-closed admission requires it).
        weight_bytes = weight.stat().st_size if weight and weight.exists() else 0
        est_vram = int(request.get("worker_vram_mb", 0)) * 1024 * 1024 \
            or (weight_bytes + _WORKER_VRAM_OVERHEAD)
        est_ram = int(request.get("worker_ram_mb", 0)) * 1024 * 1024 \
            or max(_WORKER_RAM_FLOOR, weight_bytes)
        model_dir = weight.parent if weight else (
            self._settings.models_dir or self._settings.data_dir / "models")
        return WorkerSpec(
            key=WorkerKey(model_key=model_key, runtime_key="sdcpp",
                          flavor=flavor, gpu_index=gpu, protocol="http"),
            argv=tuple(self.server_argv(exe, port, model_dir)),
            env=env,
            cwd=str(exe.parent) if exe is not None else None,
            est_ram_bytes=est_ram,
            est_vram_bytes=est_vram,
            ready_timeout_s=float(request.get("ready_timeout_s", 30.0)),
            heartbeat_interval_s=2.0,   # HTTP health-probe cadence
            heartbeat_grace_s=8.0,
            protocol_factory=lambda proc: SdServerHttpProtocol(
                f"http://127.0.0.1:{port}", proc=proc),
        )

    # ---------- request mapping (unverified vs real weights — Phase 2B) ----------

    def _payload(self, request: dict, weight: Path | None) -> dict:
        w = int(request.get("width", 512))
        h = int(request.get("height", 512))
        return {
            # OpenAI-images + A1111-style superset — the exact accepted
            # field set must be measured in Phase 2B.
            "model": weight.name if weight else None,
            "prompt": request.get("prompt", ""),
            "negative_prompt": request.get("negative_prompt", ""),
            "seed": int(request.get("seed", -1)),
            "width": w, "height": h, "size": f"{w}x{h}",
            "steps": int(request.get("steps", 20)),
            "cfg_scale": float(request.get("cfg_scale", 7.0)),
            "n": 1,
        }

    # ---------- run ----------

    async def validate(self, request: dict[str, Any]) -> list[str]:
        if not (request.get("prompt") or "").strip():
            return ["prompt is required"]
        if self._pool is None:
            return ["persistent worker pool unavailable"]
        return []

    async def run(self, handle: JobHandle) -> AsyncIterator[dict[str, Any]]:
        request = handle.request
        if self._argv_prefix is not None:
            # Test/stub mode — no model resolution needed.
            model_key = request.get("model_key") or "stub-model"
            weight, exe, flavor = None, None, str(
                request.get("runtime_flavor") or "stub")
        else:
            spec, weight, _ = self._resolve(request)
            model_key = spec.key
            exe, flavor = self._server_exe(request)
            if exe is None:
                raise RuntimeError(
                    f"{SERVER_EXE} not found in installed {flavor} runtime")

        out_dir = Path(handle.work_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = self._payload(request, weight)

        # Port allocation is TOCTOU — a collided port surfaces as a
        # spawn/readiness failure. Retry with a *different* port; admission
        # and cap refusals are honest answers, not bind problems.
        lease = None
        last_err: Exception | None = None
        for _ in range(_MAX_PORT_ATTEMPTS):
            spec = self._spec(request, handle.job_id, port=self._alloc_port(),
                              model_key=model_key, weight=weight,
                              exe=exe, flavor=flavor)
            try:
                lease = await self._pool.acquire(spec)
                break
            except WorkerUnavailableError as e:
                raise RuntimeError(f"persistent worker unavailable: {e}") from None
            except Exception as e:
                last_err = e
        if lease is None:
            raise RuntimeError(
                f"sd-server worker failed to start after {_MAX_PORT_ATTEMPTS} "
                f"attempts: {last_err}")

        try:
            async with lease as worker:
                yield {"attempt": {"worker_id": worker.id},
                       "message": f"sd-server worker {worker.id} "
                                  f"(gpu {spec.key.gpu_index}, port bound)"}
                try:
                    result = await worker.request(
                        "generate", payload, job_id=handle.job_id,
                        is_cancelled=lambda: bool(
                            handle.cancel_event
                            and handle.cancel_event.is_set()),
                        timeout_s=float(request.get("request_timeout_s", 300.0)))
                except TimeoutError as e:
                    # Abandoned by cancellation or timed out — worker is
                    # already dead-marked for recycling by the supervisor.
                    if (handle.cancel_event is not None
                            and handle.cancel_event.is_set()):
                        raise JobCancelled() from e
                    raise WorkerDeadError(
                        f"sd-server request failed: {e}") from e
                except RuntimeError as e:
                    # Transport failure — outcome unknown, worker killed.
                    raise WorkerDeadError(
                        f"sd-server channel failure: {e}") from e
        except JobCancelled:
            raise
        if not result.get("ok"):
            raise RuntimeError(f"sd-server: {result.get('error') or 'request failed'}")

        body = result.get("body") or {}
        data = body.get("data") or []
        b64 = data[0].get("b64_json") if data and isinstance(data[0], dict) else None
        if not b64:
            raise RuntimeError("sd-server returned no image data")
        out_path = out_dir / f"{handle.job_id}.png"
        blob = base64.b64decode(b64)
        out_path.write_bytes(blob)
        import hashlib
        sha = hashlib.sha256(blob).hexdigest()
        yield {
            "done": True,
            "progress": 1.0,
            "output": {
                "path": str(out_path), "sha256": sha, "kind": "image",
                "size": [payload["width"], payload["height"]],
                "name": f"sdsrv-{handle.job_id[:8]}.png", "origin": "generation",
                "media": {
                    "source": "sdcpp-server", "generated": True,
                    "adapter_id": self.profile.adapter_id,
                    "worker_model": "persistent-pool",
                    "model_key": model_key,
                    "prompt": payload["prompt"], "seed": payload["seed"],
                },
            },
        }
