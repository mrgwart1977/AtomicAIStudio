# A7 hardening — guarantees and verification

Scope: multi-GPU scheduling + provider-neutral cloud gates. Simulated
devices and mock providers only — no live providers, no real
inference, no credential values, no paid activity.

## F2 — Concurrent GPU admission

**Atomicity.** `GpuScheduler.acquire` performs the check-then-claim
inside `self._lock` (RLock). The engine calls it from the event loop,
but the lock makes admission atomic for ANY caller — a future
off-loop caller cannot observe a torn free-space check. Deterministic
concurrent tests race 16 threads for a single device and 8 threads
for an exclusive claim (`test_concurrent_admission_is_atomic_no_
oversubscribe`, `test_concurrent_exclusive_claims_single_winner`).

**Accounting.** usable = vram − headroom; free = usable −
Σ(reservations). A known estimate admits iff free ≥ estimate.
Boundary is exact: `est == usable` admits, `est + 1` refuses
(`test_headroom_usable_boundary_exact`). Unknown-footprint jobs
require a device with zero reservations and claim `usable` bytes.

**Ownership.** `_reserved[gpu][job_id] = bytes`; `claimed_bytes(job)`
is the ownership read; `GenerationJob.reserved_vram_bytes` persists
the ACTUAL claim (exclusive claims record usable bytes, not NULL).

**Queue reasons.** Refusals are recorded per job
(`reason_for(job_id)`); `last_reason` remains a convenience alias.
Concurrent refusals cannot attribute one job's reason to another
(`test_per_job_refusal_reasons_survive_concurrency`).

**Fairness.** The engine scans queued jobs FIFO and SKIPS a job that
cannot be admitted rather than blocking the lane — a big job waiting
for a full device never starves smaller jobs that fit elsewhere
(`test_small_jobs_not_starved_by_big_waiter`). Admission scoring
picks the most free-after-reserve device (balanced spread); ties
deterministically prefer the lower index.

## F3 — Reservation lifecycle

Release runs in `_execute_guarded`'s `finally` — every terminal path
(succeeded, failed, JobCancelled, CancelledError, generic exception,
asset-registration failure) releases the claim exactly once.
`release(job_id)` is idempotent: duplicate completion callbacks and
releases for jobs that never held capacity are no-ops
(`test_release_idempotent_and_unknown_safe`).

| Path | Mechanism | Test |
|------|-----------|------|
| Success | `_execute_guarded` finally → release | `test_sim_job_runs_on_single_gpu_and_releases` |
| Failure/crash | exception path → finally → release | `test_worker_crash_fails_job_and_releases` |
| Cancellation | JobCancelled → finally → release | `test_cancelled_job_releases_reservation` |
| Worker timeout | supervisor raises WorkerDeadError → same exception path → release | subprocess-supervisor contract (sim adapter is in-process; crash path exercises the same finally) |
| Rejected admission | acquire records nothing on refusal | `test_oversubscription_refused_with_reason` |
| Duplicate finalize | release() is pop-if-present idempotent | `test_release_idempotent_and_unknown_safe` |
| Device disappearance | reservation retained + surfaced `present:false`/`lost_devices` until the job's own finalizer releases | `test_device_disappearance_is_honest` |
| Backend restart | scheduler is in-process → boots empty; `reconcile_interrupted` runs in a session and COMMITS before `job_engine.start()` begins claiming (main.py:191–202) — reconcile-before-admission is startup order, never a race | `test_restart_reconcile_marks_interrupted_terminal` |

Spend holds: a job cancelled while still QUEUED never ran its
adapter, so no release marker could fire — `request_cancel` now
calls `spend.release` in the same transaction
(`test_queued_cancel_releases_spend_reservation`). Running-job
cancel keeps the adapter-marker contract: released when the remote
confirms cancel, held with `reconcile_required` when the outcome is
ambiguous (C2.0b — no silent release).

## F4 — Multi-GPU behavior

One GPU, two heterogeneous GPUs, different capacities and vendors
are all first-class (`_hw([8])`, `_hw([4,8])`, vendor-mixed
fixtures). Device loss keeps the reservation honest and refuses new
work; a returning device whose job ended admits cleanly
(`test_device_return_admits_again`). VRAM is NEVER pooled —
`_hw([8,8])` cannot host a 10 GiB job
(`test_vram_never_pooled_across_devices`). Explicit `device: N`
pins never silently migrate; `device` in the request overrides the
`preferred_gpu` preference, which only applies when the request
names no device (`test_explicit_device_overrides_preference`).
Backend requirements filter devices: cuda→NVIDIA, vulkan→ICD+GPU,
directml→vendor-neutral (`test_heterogeneous_backend_requirements`).
CPU lane (`device:"cpu"` or `allow_cpu` fallback with no GPUs)
reserves nothing (`test_fallback_cpu_when_no_gpu_and_allowed`,
`test_cpu_lane_never_touches_gpu`). No distributed sharding — every
admission is per-device.

## F5 — Simulation isolation (`ATOMIC_HW_JSON`)

**Activation rules — the ONLY permitted path:**

- Set `ATOMIC_HW_JSON` in the backend process environment BEFORE
  launch, to either an inline JSON object or a path to a JSON file
  shaped like a hardware-probe report.
- Permitted environments: pytest runs, dev-backend runs, and the
  installed-acceptance driver under an operator-controlled shell.
  It must NEVER be set on a production install — it is a testing
  hook, not a feature.
- The file-path form is re-read on every probe call so an
  acceptance driver can mutate the simulated inventory mid-run
  (device appearance/loss) without restart.
- Nothing in project data, the database, HTTP payloads, or app
  settings can set or influence it — `os.environ` is the only
  source (`hardware._simulated_report`).

**Isolations verified** (`test_simulated_inventory_marked_and_no_bypass`,
`test_malformed_hw_json_falls_back_to_real_probe`,
`test_scheduler_state_reports_simulated_flag`):

- Every probe report carrying simulated devices sets
  `simulated: true`; `/api/hardware` and `/api/gpu/scheduler`
  (`state()["simulated"]`) both surface it — simulated devices can
  never masquerade as measured hardware.
- Malformed env values (non-JSON, missing file, non-object) fall
  back to the real probe — never phantom devices, never a crash.
- Simulated devices pass through the SAME `_fits_backend` /
  `_usable` / reservation accounting as real devices — the hook
  changes the inventory, never the rules. Vendor mismatches still
  refuse; capacity limits still bind.
- It cannot trigger inference: scheduling selects a device index;
  adapters come from the registry. `mock.gpu.sim` is
  `adapter_kind="mock"`, in-process, no subprocess, no network —
  deterministic sleep-based work only
  (`test_sim_adapter_is_in_process_and_mock_kind`).

## F6 — Cloud authorization

- `cloud.plan` is read-only: zero writes, zero sockets
  (`test_plan_is_read_only_and_touches_no_network` — socket.connect
  is rigged to raise during `resolved_plan`; `session.new`/`dirty`
  stay empty).
- Credential inspection is presence-only: `vault.status()` metadata
  (`configured`/`fields`/`unreadable`) — the vault's value-returning
  methods are never called on the plan path.
- `plan_fingerprint` binds the approval to provider, enabled state,
  credential presence, capability, task, model, and cost inputs —
  drift refuses `plan_stale` at commit AND at pinned submit.
- A pinned submit also requires `would_admit` on the recomputed
  plan — pinning the fingerprint of a NON-admitting plan (missing
  credential, over budget, disabled, unrouted) refuses with the
  availability reason (`test_pinned_submit_refuses_nonadmitting_
  plan`). Approval of a non-admitting plan is not authorization.
- Cost figures are estimates (`is_estimate` in audit); limits are
  enforced by `spend.admit` inside the commit transaction for
  simulated bookings identically to the live path.
- `cloud.plan` commit is idempotent through the command system;
  `spend.admit` returns the existing reservation for the same job —
  retries cannot double-book.
- Queued-cancel releases the hold (see F3). Missing credentials fail
  closed: `needs_key`/`credential_unreadable` → `would_admit=false`
  → commit and pinned submit both refuse.
- Approval to plan is not approval to dispatch — dispatch still
  requires the normal submit path with its own gates.

## F7 — Security boundaries

- `live_gate.dispatch_admission` is preserved verbatim — zero diff
  vs the A6 merge. The A7 environment derivation uses the same
  `spend_environment`/`adapter_kind` truth the submit path uses, so
  a future adapter reporting a live transport still hits the
  complete C2 policy gate at booking time.
- Issue #96 (live artifact retrieval) remains unresolved and
  untouched — no retrieval path was added.
- The planning path cannot bypass: credential isolation (presence
  only, proven above), URL validation / DNS / SSRF / redirect
  restrictions (the planner performs NO requests — no URL is ever
  constructed; mock transports are in-process), provider
  authorization (enabled state + capability are plan inputs),
  budget authorization (`spend.admit` in the submit transaction),
  audit requirements (plan commits write audit rows; submits are
  ordinary audited commands).

## F8 — Installed acceptance

17-step driver against the installed NSIS build; evidence in
`a7-acceptance.txt` (tested commit + installer stamped at run time).
Device loss/return is driven by rewriting the `ATOMIC_HW_JSON` file
mid-run — no restart needed except for the genuine restart-reconcile
step.
