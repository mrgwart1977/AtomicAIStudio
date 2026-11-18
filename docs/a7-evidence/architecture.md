# A7 — multi-GPU scheduling + cloud gates: architecture & guarantees

Scope: orchestration and safety only. No live providers, no real
inference, no credential-value access, no paid activity, no provider
CLIs, no automatic model downloads. B2b stays on hold; issue #96 still
blocks live artifact retrieval.

## GPU scheduler (`backend/app/domain/gpu_sched.py`)

State: `_reserved: {gpu_index: {job_id: bytes}}` replaces binary
per-device busy flags.

- **Admission** (`acquire(job_id, request, est_vram, requires)`)
  - `device: "cpu"` → CPU lane, no reservation.
  - `device: <int>` → pinned index; refuses "not present" when the
    device isn't in the probe, "cannot fit" with the free-after-reserve
    figure when it can't admit — never silently migrates a pinned job.
  - `device: "auto"` (default) → the compatible device with the most
    free-after-reserve VRAM that fits the estimate — a balancing
    spread, not packing.
  - Known footprint → reserves exactly `est_vram`; multiple jobs share
    a device when their reservations + `vram_headroom_bytes` fit inside
    `compatibility.usable_vram` (free VRAM preferred, never pooled
    across devices).
  - Unknown footprint (`est_vram=None`) → exclusive claim of the whole
    usable figure on a device with zero reservations. An unmeasured
    job can never gamble on shared capacity.
- **Compatibility** — `requires` names backends; a device satisfies a
  vendor-bound backend ("cuda"→NVIDIA) only when the probe reports it
  available AND the vendor matches. Unmet requirements refuse with the
  specific reason.
- **Honest refusal** — `last_reason` records why the most recent
  `acquire` returned None; the engine stamps it on the queued job's
  message ("waiting for GPU — …").
- **Fairness** — the engine scans the queue FIFO and skips jobs that
  can't be admitted; a big job waiting for a full device does not
  starve smaller jobs that fit alongside existing reservations or on
  other devices.
- **Device loss** — a device that vanishes while holding reservations
  is reported via `slots(present=False)` + `lost_devices()`; nothing
  new is scheduled onto it, and the held reservation is surfaced, not
  silently freed. Reconciliation of the running job is the engine/
  worker contract (its work died with the device).
- **Lifecycle** — `_execute_guarded` releases the reservation in a
  `finally` on every outcome (success/failure/cancel/crash). Startup
  `reconcile_interrupted` marks abandoned "running" rows
  failed/interrupted for non-resumable jobs (sim.gpu.work follows the
  generation contract — never silently resumed). `reserved_vram_bytes`
  on the job row persists the claim for queue reporting (migration
  `0026_a7`).

## Simulated GPU worker (`backend/app/adapters/gpu_sim.py`)

`mock.gpu.sim` — `adapter_kind="mock"`, deterministic in-process
adapter. Declares `needs_gpu=True` so jobs exercise the real engine
claim/release path. Hooks: `est_vram_bytes`, `device`, `requires`,
`work_ms`, `crash_after_ms`, `fail`, `model_key`. Output provenance
marks `is_mock: true`, `generated: false` — never presented as
inference.

## Simulated hardware (`backend/app/domain/hardware.py`)

`ATOMIC_HW_JSON` (inline JSON or path) overrides the probe report's
keys and marks the result `simulated: true`. Used by tests and the
installed acceptance driver; real probe provenance is untouched.

## Cloud plan gate (`backend/app/domain/cloudplan.py`)

`cloud.plan` is a read-only resolver + auditable approval record:

- provider capability/task compatibility from the catalog, never
  inferred
- provider status (verified / needs_key / plan_required /
  credential_unreadable) and enabled state
- credential **presence** only via `vault.status` — `{configured,
  fields, unreadable}`; secret values are never read
- cost estimate (`est_cost_usd`, `max_cost_usd`) → `reserved_micro`
- per-scope budget headroom via `spend.summary` in the environment the
  job would book
- **environment** resolved through the adapter that would actually
  execute the task's route (`TASK_ROUTES` task→command, then the
  command's registered adapter): `spend_environment` when the adapter
  can attest its transports, else `adapter_kind` ("mock"→"simulated").
  A task with no execution route plans "unrouted" and cannot admit.
- `availability`: available | disabled | blocked | incompatible |
  unrouted | over_budget — with `unavailable_reason`; `would_admit`
  only when fully clear.
- `plan_fingerprint` — sha256 of the planning inputs (provider, task,
  model, est/max, status, enabled, credential presence, availability,
  environment, routed). A submit carrying `plan_fingerprint`
  recomputes the plan at commit and refuses `plan_stale` on ANY drift
  (provider disabled, credential removed, budget tightened, inputs
  changed). `cloud.plan` commit records the approval in the audit and
  refuses when the pinned `expected_plan_fingerprint` no longer
  matches or the plan would not admit.

Live boundary unchanged: `spend.admit` books the reservation inside
the submit transaction; `env == "live"` still routes through
`live_gate.dispatch_admission` — nothing in A7 bypasses, weakens, or
pre-approves that boundary. All adapters registered today book
"simulated".

## Security posture (A7.5 — unchanged fail-closed)

Live artifact retrieval remains blocked by issue #96 and is not
enabled here. The defenses that exist and stay in force:

- URL/endpoint validation happens only inside documented contract
  adapters (`rest_providers`, `rest_image_providers`) whose transports
  are MockTransport — there is no live egress path in this build.
- Redirect handling, resolved-IP validation, private-network
  restrictions, and DNS/SSRF threat modeling are prerequisite design
  work for any future live retrieval — tracked under issue #96, not
  implemented or needed for simulated transports.
- Credential isolation: the DPAPI vault stores ciphertext only;
  planning paths read `vault.status` metadata exclusively.
- Audit: `cloud.plan` commits write approval records; audit payload
  minimization (no prompt bodies, no secrets) is regression-tested.

## UI (A7.6)

- `ResourceSection` — per-device usable/reserved/free VRAM, job count
  per device, lost-device state; queue wait reasons already surface.
- `CloudPage` — every `generate.text.cloud` submit previews a
  `cloud.plan` first; a non-admitting plan blocks up front with the
  reason; the approval panel shows availability, booking environment,
  and the plan fingerprint; the fingerprint is pinned into the commit.
- `CloudImagePanel` — same plan-gated approval for
  `generate.image.cloud` / `.img2img` / `.edit`.
- `sim.gpu.work` preview exposes `resolved_gpu` (would_schedule,
  device, queue_reason, slots, lost_devices).

## Test coverage (`backend/tests/test_a7_scheduling.py` — 28 tests)

Scheduler unit: shared reservations, multi-GPU spread + accounting,
exclusive claims, oversubscription refusal, pinning, backend/vendor
matching, device disappearance, release on all paths, no-starvation.
Engine: single/multi-GPU assignment, queue-then-admit, cancel release,
crash release, restart reconcile, pin, CPU lane, preview admission,
scheduler API. Cloud gate: preview shape, approval record, stale
fingerprint, disabled provider, budget refusal, credential presence
only, submit pin pass/refuse, simulated-environment audit.

## Known limitations

- `sim.gpu.work` jobs are non-resumable by design (same contract as
  generation) — restart recovery is exercised via reconcile-to-failed,
  not re-queue.
- `cloud.plan` is an approval/planning surface; it does not itself
  create spend reservations — the submit's `spend.admit` remains the
  booking transaction.
- Plan tasks without an execution route (e.g. video/audio cloud)
  resolve "unrouted" and cannot admit — routes arrive with their
  adapters.
- No distributed inference/model sharding — every admission is
  per-device; multiple GPUs never combine VRAM.
