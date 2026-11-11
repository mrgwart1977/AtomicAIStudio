# Persistent sd-server Workers (M4.1 Phase 2B)

Architecture: durable generation job → `gen.image.sdcpp-server`
adapter → `WorkerPool.acquire` → supervised `sd-server.exe` process →
HTTP request/response → decoded PNG → committed output asset.

## Request path

```
JobHandle ──► SdCppServerAdapter.run()
   ├─ resolve approved model + runtime flavor (SdCppAdapter._resolve)
   ├─ WorkerSpec: sd-server.exe -l 127.0.0.1 --listen-port <p>
   │              --model-dir <dir> --log-level warn
   │              protocol="http", Job-Object supervised, RAM/VRAM
   │              reservations charged for worker lifetime
   ├─ pool.acquire(spec) — admission-checked; port TOCTOU retried
   │                       up to 3 fresh ports
   ├─ worker.request("generate", payload) ─► POST /v1/images/generations
   ├─ accept response shape: OpenAI data[].b64_json OR A1111 images[]
   └─ base64 → <work_dir>/<job_id>.png → sha256 → output asset
```

Worker identity (`WorkerKey` = model + runtime + flavor + GPU +
protocol) gives one resident process per distinct configuration —
repeated jobs reuse the loaded model instead of reloading per job.

## Bounded behavior

| Concern | Mechanism |
|---|---|
| Startup bound | `ready_timeout_s` (default 30s); bind/dead-proc fails fast |
| Request bound | `request_timeout_s` (default 300s) → TimeoutError → worker recycled |
| Health | `GET /v1/models` probe every 2s; 8s grace → stale → killed |
| Cancellation | no mid-request cancel exists on this build — `is_cancelled` abandons the request and the supervisor kills+reaps the worker |
| Recovery | dead/crashed workers are never reused; next acquire spawns fresh |
| Cleanup | `stop()`/`_evict` confirm OS exit before releasing the reservation — reaped means reaped |
| Ports | ephemeral loopback only; collision → retry on a different port |

## Resource policy (target box: RTX 3060 Ti 8 GB, 16 GB RAM)

`ResidencyRegistry.admit` is fail-closed: unknown/zero estimates are
rejected, reservations are subtracted from probed free RAM/VRAM for
the worker's whole lifetime, and a 2 GiB OS headroom is always kept.

Current **uncalibrated** estimates (conservative upper bounds — real
numbers must be measured during first-run validation):

| Item | Estimate |
|---|---|
| Worker RAM floor | 4 GiB |
| Worker VRAM | weight bytes + 2 GiB overhead |
| SD 1.5 fp16 GGUF weight | ~2.2 GiB → ~4.2 GiB total reservation |

On an 8 GB card a single resident fp16 worker fits comfortably; a
second concurrent worker for the same GPU would be refused by
admission rather than allowed to OOM.

## Activation gates (no uncontrolled startup)

1. Adapter registration only when
   `ATOMIC_PERSISTENT_INFERENCE_ENABLED=1` — default builds don't
   expose `gen.image.sdcpp-server` at all.
2. A worker spawns only when a real generation job acquires the pool
   lease — never at app start.
3. `sd-server --model-dir` loads weights lazily on first request —
   the server being READY does not mean a model is resident.
4. All model weights are user-approved downloads under the data dir;
   nothing is fetched implicitly.

## Request contract

The adapter POSTs an OpenAI-images/A1111 superset to
`/v1/images/generations`: `model`, `prompt`, `negative_prompt`,
`seed`, `size` ("WxH"), `steps`, `cfg_scale`, `n=1`. Unknown fields
are expected to be ignored server-side. The exact field set accepted
by the pinned runtime is asserted in tests via the stub's
`/v1/last_request` capture — field-level verification against a
**weight-loaded** server is still a first-run acceptance gate.

## Limitations

- One request per worker at a time; `n`/`batch` fixed at 1
- Mid-request cancel cannot stop server-side inference — it abandons
  and recycles the worker instead
- No mid-request progress events — jobs show running + final result
- `resumable=False`: an unknown outcome (transport failure) marks the
  worker dead; the job may retry on a fresh worker per engine policy
- Single GPU pin via `GGML_VULKAN_DEVICE`; multi-GPU untested
- Payload field subset unverified against real weights (first-run gate)

## Prerequisites for first real-inference validation

1. Approved sd.cpp runtime installed (`scripts/fetch-ffmpeg`-style
   manifest-verified component download)
2. User-approved model weights present under the models dir
3. `ATOMIC_PERSISTENT_INFERENCE_ENABLED=1` in the environment
4. A test prompt through the adapter on the target GPU — verify
   payload acceptance, measure real VRAM/RAM residency, and recalibrate
   `_WORKER_VRAM_OVERHEAD`/`_WORKER_RAM_FLOOR` from observation
5. Update this document with measured numbers
