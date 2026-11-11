# Persistent sd-server Workers (M4.1)

Architecture: durable generation job → `gen.image.sdcpp-server`
adapter → `WorkerPool.acquire` → supervised `sd-server.exe` process →
HTTP request/response → decoded PNG → committed output asset.

## Request path

```
generate.image.persistent command ──► durable job (adapter gen.image.sdcpp-server)
   ├─ _check_generation: model known, licensed, INSTALLED, and declared
   │  in spec.compatible_adapters (fail-closed — unverified contracts
   │  are rejected at command validation, not at the wire)
   ├─ SdCppServerAdapter.run()
   │   ├─ resolve approved model + runtime flavor + compat re-check
   │   ├─ WorkerSpec: sd-server.exe -l 127.0.0.1 --listen-port <p>
   │   │              -m <weight> --model-dir <dir> --log-level warn
   │   │              Job-Object supervised, RAM/VRAM reservations
   │   │              charged for worker lifetime
   │   ├─ pool.acquire(spec) — admission-checked; port TOCTOU retried
   │   │                       up to 3 fresh ports
   │   ├─ worker.request("generate") ─► POST /sdapi/v1/txt2img
   │   ├─ response {"images":[b64]} (documented); data[].b64_json kept
   │   │  as fallback parse for endpoint flexibility
   │   └─ base64 → <work_dir>/<job_id>.png → sha256 → output asset
   └─ audit: job row carries model_key + provenance media metadata
```

Worker identity (`WorkerKey` = model + runtime + flavor + GPU +
protocol) gives one resident process per distinct configuration —
repeated jobs reuse the loaded model instead of reloading per job.

## Verified request contract (pinned runtime master-1073-991f825)

Checked against `examples/server/api.md` at that commit. The server
exposes three families: OpenAI `/v1/*`, WebUI-compat `/sdapi/v1/*`,
and native async `/sdcpp/v1/*`.

**Generation uses `POST /sdapi/v1/txt2img`** — the only family whose
documented request fields cover the full parameter set:

| Sent field | Type | Note |
|---|---|---|
| `prompt` | string | required |
| `negative_prompt` | string | |
| `width` / `height` | int | image geometry |
| `steps` | int | sampling steps |
| `cfg_scale` | number | request `cfg` maps here |
| `seed` | int | -1 = random |
| `batch_size` | int | adapter limits max_batch=1 |
| `sampler_name` / `scheduler` | string | only when request supplies them |

Fields from the earlier unverified superset (`model`, `size`, `n`,
`output_format`) are no longer sent. `/v1/images/generations` only
documents `prompt`/`n`/`size`/`output_format` — everything else would
need `sd_cpp_extra_args` embedded inside the prompt, a strictly more
fragile contract.

Response: `{"images": [b64_png...], "parameters": {...}, "info": ""}`.

**Model loading is command-line preloading (`-m <weight>`):** verified
in server `main.cpp` at the pinned commit — the context is created
only when `model_path` is set, synchronously BEFORE `listen()`, and
the process `exit(1)`s on load failure. Consequences: READY implies
weights resident; load failure is an honest dead-process signal; the
readiness timeout must cover model load (~2 GiB on Vulkan can exceed
30s — default bumped to 120s). `--model-dir` alone only defines scan
roots for the recipe/model-file catalog — it never loads a model, and
generation calls would `503 {"error":"No model is loaded",
"code":"model_not_loaded"}` (stub mode `nomodel` covers the path).

**Async alternative (documented, not adopted):** `/sdcpp/v1/img_gen`
→ `GET /sdcpp/v1/jobs/{id}` → `POST /sdcpp/v1/jobs/{id}/cancel`
provides real mid-request cancellation on this build. Adopting the
native async API (job polling + true cancel) is a Phase-2C design
change — the synchronous sdapi path keeps the abandon→recycle
contract for now.

## Bounded behavior

| Concern | Mechanism |
|---|---|
| Startup bound | `ready_timeout_s` (default 30s); bind/dead-proc fails fast |
| Request bound | `request_timeout_s` (default 300s) → TimeoutError → worker recycled |
| Health | `GET /v1/models` probe every 2s; 8s grace → stale → killed |
| Cancellation | sync sdapi has no mid-request cancel — `is_cancelled` abandons the request and the supervisor kills+reaps the worker |
| Recovery | dead/crashed workers are never reused; next acquire spawns fresh |
| Cleanup | `stop()`/`_evict` confirm OS exit before releasing the reservation |
| Ports | ephemeral loopback only; collision → retry on a different port |
| Model gate | `compatible_adapters` declaration required at BOTH command validation and adapter run (defense in depth) |

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

**Abort conditions (first-run validation):** admission refusal,
`503 model_not_loaded`, startup-timeout (model load pathology),
request timeout, transport failure/crash (worker recycled), free
VRAM probe < reservation, system RAM < floor + 2 GiB headroom.

## Activation gates (no uncontrolled startup)

1. Adapter registration only when
   `ATOMIC_PERSISTENT_INFERENCE_ENABLED=1` — default builds don't
   expose `gen.image.sdcpp-server` at all.
2. The `generate.image.persistent` command exists at the API layer
   unconditionally, but only validates+enqueues; without the gate no
   adapter serves the job and it fails honestly.
3. A worker spawns only when a real generation job acquires the pool
   lease — never at app start.
4. `-m` preloads weights at spawn — READY means the model IS resident
   (and load failure exits the process honestly).
5. All model weights are user-approved downloads under the data dir;
   nothing is fetched implicitly.

## Limitations

- One request per worker at a time; `batch_size` fixed at 1
- Mid-request cancel cannot stop server-side inference on the sync
  endpoint — it abandons and recycles the worker instead (the async
  `/sdcpp/v1/jobs/{id}/cancel` path is documented, not adopted)
- No mid-request progress events — jobs show running + final result
  (`/sdapi/v1/progress` exists; adoption is a Phase-2C candidate)
- `resumable=False`: an unknown outcome (transport failure) marks the
  worker dead; the job may retry on a fresh worker per engine policy
- Single GPU pin via `GGML_VULKAN_DEVICE`; multi-GPU untested
- Model selection per request is not supported — `--model-dir` is
  fixed at spawn; a different model_key is a different WorkerKey →
  a different resident process (admission caps prevent stacking)
- `sampler_name`/`scheduler` accepted values come from
  `/sdapi/v1/samplers` at runtime — invalid names surface as honest
  4xx job errors, but the valid-name list is unverified until the
  first weight-loaded run

## Remaining uncertainties (require a weight-loaded server)

- Field-level semantics under real weights (documented ≠ behavior-verified)
- Real VRAM/RAM residency per worker → recalibrate
  `_WORKER_VRAM_OVERHEAD` / `_WORKER_RAM_FLOOR`
- Whether `503 model_loading` needs distinct UX vs `model_not_loaded`
- `/sdcpp/v1/models/load` recipe surface — alternative to
  `--model-dir` lazy loading, unassessed

## Prerequisites for first real-inference validation

1. Approved sd.cpp runtime installed (`sd-server.exe` present in the
   runtime dir — Vulkan flavor for the 3060 Ti)
2. User-approved SD 1.5 weights (fp16 GGUF preferred:
   `sd15.fp16`, sha256-pinned in the catalog) under the models dir
3. `ATOMIC_PERSISTENT_INFERENCE_ENABLED=1` in the environment
4. A single 512×512, 20-step, fixed-seed generation through
   `generate.image.persistent` — verify the documented fields produce
   a sane image, measure real residency, recalibrate constants
5. Second identical job → assert worker REUSE (same worker_id, no
   reload); cancellation test → assert worker recycle; shutdown →
   assert reservations released
6. Update this document with measured numbers
